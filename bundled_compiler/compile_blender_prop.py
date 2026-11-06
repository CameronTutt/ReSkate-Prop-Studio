#!/usr/bin/env python3
"""
compile_blender_prop.py
================================================================================
Standalone CLI Compiler for ReSkate Blender 5.3 Skate Prop Studio.
Bridges intermediate JSON manifests (single-prop and multi-prop pack queue)
into genuine Frostbite zero-donor standalone mods (Bundle 39).

Emits structured progress lines on stdout:
  STEP <i/n> <label>
  PROGRESS <0..100> <msg>
  ERROR <msg>
  DONE <output_path>

Usage:
  python compile_blender_prop.py --manifest path/to/prop_manifest.json
  python compile_blender_prop.py --pack-manifest path/to/pack_manifest.json
  python compile_blender_prop.py path/to/manifest.json
================================================================================
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import filecmp
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
import site
import unicodedata
import zlib

COMPILER_VERSION = "0.33.0"
os.environ["RESKATE_COMPILER_VERSION"] = COMPILER_VERSION
PIPELINE_MODE = os.environ.get("RESKATE_PIPELINE_MODE", "FAST_OPTIMIZED").strip().upper()
LOGGED_INPUT_READS: list[str] = []


def log_input_read(path: str | Path, coexist_mode: bool = True) -> Path:
    """Logs an input file path read during compilation and forbids Mods\\ reads in coexist_mode."""
    p = Path(path).resolve()
    p_str = str(p)
    if p_str not in LOGGED_INPUT_READS:
        LOGGED_INPUT_READS.append(p_str)
        print(f"  [INPUT-READ] {p_str}", flush=True)
    if coexist_mode and "\\mods\\" in p_str.lower():
        raise RuntimeError(f"Coexist mode violation: compiler attempted to read from Mods\\ folder: {p_str}")
    return p


def coexist_sha256_u64(seed_str: str, force_odd_rid: bool = False) -> int:
    """Derives a 64-bit integer from the first 8 bytes (little-endian) of SHA-256(seed_str)."""
    val = int.from_bytes(hashlib.sha256(seed_str.encode("utf-8")).digest()[:8], "little")
    return (val | 1) if force_odd_rid else val


def coexist_uuid5_bytes(seed_str: str, prefix_2b: bytes | None = None) -> bytes:
    """Derives a 16-byte GUID via UUIDv5 (NAMESPACE_OID) from seed_str, optionally preserving a 2-byte rank prefix."""
    raw = uuid.uuid5(uuid.NAMESPACE_OID, seed_str).bytes
    if prefix_2b is not None:
        return prefix_2b + raw[2:]
    return raw


def coexist_root_hash_pair(seed_prefix: str, forbidden_hi32: set[int] | None = None) -> tuple[int, int]:
    """
    Derives a deterministic (rh_even, rh_odd) root-hash pair from seed_prefix where:
      - Upper 32 bits are derived from SHA-256(f"{seed_prefix}/mat_0/seed32"), avoiding 0 and forbidden_hi32.
      - Low 32 bits are 0x26C6D084 (even) and 0x4C323888 (odd), preserving (rh_odd - rh_even) == 0x256B6804.
    """
    forb = forbidden_hi32 or set()
    salt = 0
    while True:
        s = f"{seed_prefix}/mat_0/seed32" if salt == 0 else f"{seed_prefix}/mat_0/seed32#{salt}"
        hi32 = int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little") & 0xFFFFFFFF
        if hi32 != 0 and hi32 not in forb:
            break
        salt += 1
    rh_seed = hi32 << 32
    rh_even = (rh_seed | 0x26C6D084) & 0xFFFFFFFFFFFFFFFF
    rh_odd = (rh_seed | 0x4C323888) & 0xFFFFFFFFFFFFFFFF
    return rh_even, rh_odd


def coexist_root_hash_pair_771(seed_prefix: str, forbidden_hi32: set[int] | None = None) -> tuple[int, int]:
    """
    Derives a deterministic (rh771_even, rh771_odd) root-hash pair for the 0x771 (1,905-byte)
    Full Multi-Layer PBR shader block at RES[18][0x2C9:0xA3A] where:
      - Upper 32 bits are derived from SHA-256(f"{seed_prefix}/mat_771/seed32"), avoiding 0 and forbidden_hi32.
      - Low 32 bits are 0x4A3CB802 (even) and 0x6FA82006 (odd), preserving (rh771_odd - rh771_even) == 0x256B6804.
    """
    forb = forbidden_hi32 or set()
    salt = 0
    while True:
        s = f"{seed_prefix}/mat_771/seed32" if salt == 0 else f"{seed_prefix}/mat_771/seed32#{salt}"
        hi32 = int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little") & 0xFFFFFFFF
        if hi32 != 0 and hi32 not in forb:
            break
        salt += 1
    rh_seed = hi32 << 32
    rh_even = (rh_seed | 0x4A3CB802) & 0xFFFFFFFFFFFFFFFF
    rh_odd = (rh_seed | 0x6FA82006) & 0xFFFFFFFFFFFFFFFF
    return rh_even, rh_odd


def _load_vanilla_root_hi32s(tpl: dict, coexist_mode: bool = True) -> set[int]:
    """Collects all upper 32-bit values of shader root hashes in vanilla Bundle 39 RES[18] and globals.toc RES[1130]."""
    hi32s: set[int] = {0, 0xF136CD5C, 0x5959DE71, 0x7A20336B}
    try:
        r18 = tpl.get("res18", b"")
        for k in range(6):
            if 0x10 + (k + 1) * 16 <= len(r18):
                rh = struct.unpack_from("<Q", r18, 0x10 + k * 16)[0]
                hi32s.add((rh >> 32) & 0xFFFFFFFF)
        g_path = GAME_DIR / "Data" / "Win32" / "globals.toc"
        if g_path.exists():
            log_input_read(g_path, coexist_mode=coexist_mode)
            g_pl = g_path.read_bytes()[556:]
            gw = struct.unpack_from(">12I", g_pl, 0)
            _, g_tsz, g_doff = struct.unpack_from(">IIQ", g_pl, gw[1])
            g_streams = bsp.parse_bundle_desc(g_pl[g_doff : g_doff + (g_tsz & 0x3FFFFFFF)])
            gs0 = g_streams[0]
            g_sb = rpf.read_cas_raw(0 if gs0[1] == 3162564249 else 1, gs0[2], gs0[3], gs0[4])
            _, g_ebxs, _, _, _ = bsp.parse_sbdata_full(g_sb)
            st1130 = g_streams[1 + len(g_ebxs) + 1130]
            v1130 = rpf.decompress_cas(0 if st1130[1] == 3162564249 else 1, st1130[2], st1130[3], st1130[4])
            for i in range(len(v1130) // 16):
                rh = struct.unpack_from("<Q", v1130, i * 16)[0]
                hi32s.add((rh >> 32) & 0xFFFFFFFF)
    except Exception:
        pass
    return hi32s


def _resolve_qd_tab_slug(category: str | None, custom_qd_tab: str | None = None) -> str:
    """
    Maps a prop's category enum/label (or custom_qd_tab string) to the Frostbite/Quick Drop
    category folder and item-key prefix (`Items/<slug>/own_<slug>_...`), matching Noble Lights
    (`bklights`, `bkaudio`, `bkledges`, `bkrails`, `bkramps`, `bkpads`, `bkcustomprops`, etc.).
    """
    cat_raw = str(category or "").strip()
    cat_up = cat_raw.upper()
    if cat_up in ("CUSTOM_TAB", "CUSTOM") or (custom_qd_tab and str(custom_qd_tab).strip() and cat_up == "CUSTOM_TAB"):
        c_slug = re.sub(r"[^a-z0-9]+", "", str(custom_qd_tab or "customprops").lower())
        if not c_slug:
            return "bkcustomprops"
        return c_slug if c_slug.startswith("bk") else f"bk{c_slug}"

    known_map = {
        "BK_CUSTOM": "bkcustomprops",
        "CUSTOM PROPS": "bkcustomprops",
        "BKCUSTOMPROPS": "bkcustomprops",
        "BK_RAILS": "bkrails",
        "RAILS": "bkrails",
        "BKRAILS": "bkrails",
        "BK_LEDGES": "bkledges",
        "LEDGES": "bkledges",
        "BKLEDGES": "bkledges",
        "BK_RAMPS": "bkramps",
        "RAMPS": "bkramps",
        "BKRAMPS": "bkramps",
        "BK_PADS": "bkpads",
        "MANUAL PADS": "bkpads",
        "BKPADS": "bkpads",
        "BK_FUNBOXES": "bkfunboxes",
        "FUNBOXES": "bkfunboxes",
        "BKFUNBOXES": "bkfunboxes",
        "BK_QUARTERPIPES": "bkquarterpipes",
        "QUARTERPIPES": "bkquarterpipes",
        "BKQUARTERPIPES": "bkquarterpipes",
        "BK_LIGHTS": "bklights",
        "LIGHTS": "bklights",
        "BKLIGHTS": "bklights",
        "BK_AUDIO": "bkaudio",
        "AUDIO": "bkaudio",
        "BKAUDIO": "bkaudio",
        "BK_CONSTRUCTION": "bkpropsconstruction",
        "BK_EVENT": "bkpropsevent",
        "BK_FURNITURE": "bkpropsfurniture",
        "BK_INDUSTRIAL": "bkpropsindustrial",
        "BK_NATURE": "bkpropsnature",
        "BK_URBAN": "bkpropsurban",
        "BK_WALL": "bkpropswall",
        "BK_RECREATION": "bkpropsrecreation",
        "BK_PROPS": "bkprops",
        "BUILD KIT": "bkprops",
        "BK_INTERACTIVE": "bkinteractive",
        "DYNAMIC PROPS": "bkinteractive",
    }
    if cat_up in known_map:
        return known_map[cat_up]
    c_clean = re.sub(r"[^a-z0-9]+", "", cat_raw.lower())
    if c_clean.startswith("bk") and len(c_clean) > 2:
        return c_clean
    return "bkcustomprops"


def _patch_coexist_item_ebx(
    custom_item_raw: bytes,
    seed_prefix: str,
    item_path: str,
    key_str: str,
    display_name: str,
    bundle_str: str,
    prefab_str: str,
) -> tuple[bytes, bytes, bytes]:
    """
    Patches DelMarBaseItemAsset in coexist_mode with UUIDv5 GUIDs derived from seed_prefix
    and explicit bundle_str / prefab_str pointers.
    """
    form, chunks = demux_riff(custom_item_raw)
    cmap = {cid: bytearray(payload) for cid, payload in chunks}
    ebxd = cmap[b"EBXD"]
    efix = cmap[b"EFIX"]

    file_guid = coexist_uuid5_bytes(f"{seed_prefix}/item_fg")
    inst0_guid = coexist_uuid5_bytes(f"{seed_prefix}/item_ig")
    inst1_guid = coexist_uuid5_bytes(f"{seed_prefix}/item_inst1_ig")
    inst2_guid = coexist_uuid5_bytes(f"{seed_prefix}/item_inst2_ig")

    efix[0:16] = file_guid
    ebxd[12:28] = inst0_guid
    ebxd[124:140] = inst1_guid
    ebxd[220:236] = inst2_guid
    struct.pack_into("<I", ebxd, 100, fnv1_32_lower(key_str))

    cat_map = {
        "bkfunboxes": 0,
        "bkramps": 1,
        "bkrails": 2,
        "bkledges": 2,
        "bkpropsconstruction": 5,
        "bkpropsevent": 6,
        "bkpropsfurniture": 7,
        "bkpropsindustrial": 8,
        "bkprops": 10,
        "bkcustomprops": 10,
        "bklights": 10,
        "bkaudio": 10,
        "bkpropsnature": 10,
        "bkpads": 11,
        "bkquarterpipes": 12,
        "bkpropsrecreation": 13,
        "bkpropsurban": 15,
        "bkpropswall": 16,
    }
    parts_lower = item_path.lower().replace("\\", "/").split("/")
    cat_folder = parts_lower[1] if len(parts_lower) >= 2 else ""
    if cat_folder in cat_map and len(ebxd) >= 356:
        struct.pack_into("<I", ebxd, 352, cat_map[cat_folder])

    s1 = item_path.encode("ascii", errors="replace") + b"\x00"
    s2 = key_str.encode("ascii", errors="replace") + b"\x00"
    s3 = display_name.encode("ascii", errors="replace") + b"\x00"
    s4 = bundle_str.encode("ascii", errors="replace") + b"\x00"
    s5 = prefab_str.encode("ascii", errors="replace") + b"\x00"

    off1 = 412
    off2 = off1 + len(s1)
    off3 = off2 + len(s2)
    off4 = off3 + len(s3)
    off5 = off4 + len(s4)

    ebxd[412:] = s1 + s2 + s3 + s4 + s5
    struct.pack_into("<Q", ebxd, 52, off1 - 52)
    struct.pack_into("<Q", ebxd, 84, off2 - 84)
    struct.pack_into("<Q", ebxd, 260, off3 - 260)
    struct.pack_into("<Q", ebxd, 324, off4 - 324)
    struct.pack_into("<Q", ebxd, 340, off5 - 340)

    rebuilt = remux_riff(
        form,
        [(cid, ebxd if cid == b"EBXD" else (efix if cid == b"EFIX" else pl)) for cid, pl in chunks],
    )
    return rebuilt, file_guid, inst0_guid

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def emit_step(idx: int, total: int, label: str):
    print(f"STEP {idx}/{total} {label}", flush=True)


def emit_progress(pct: int, msg: str):
    pct_clamped = max(0, min(100, int(pct)))
    print(f"PROGRESS {pct_clamped} {msg}", flush=True)


def emit_prop_substep(idx: int, total: int, prop_name: str, substep: int, msg: str):
    """Emits granular 5-substep per-prop progress lines for Blender's non-blocking progress UI."""
    print(f"PROGRESS [{idx}/{total}] {prop_name} — {substep}/5 {msg}", flush=True)


def emit_prop_cached(idx: int, total: int, prop_name: str):
    """Emits cached prop bundle reuse notification for unchanged props in a multi-prop pack."""
    print(f"PROGRESS [{idx}/{total}] {prop_name} — ⚡ Reusing cached prop bundle (unchanged)", flush=True)


def emit_error(msg: str):
    print(f"ERROR {msg}", flush=True)


def emit_done(path: str | Path):
    print(f"DONE {path}", flush=True)


def clamp_slug(slug: str, max_len: int = 36) -> str:
    """Clamps generated slug length <= max_len (default 36) using deterministic CRC32 truncation."""
    if len(slug) <= max_len:
        return slug
    trunc_len = max_len - 9  # 27 chars for 36 max_len
    crc = zlib.crc32(slug.encode("utf-8", errors="replace")) & 0xFFFFFFFF
    return f"{slug[:trunc_len]}_{crc:08x}"


def sanitize_display_name(name: str | None) -> str:
    """Sanitizes display name with transliteration/safe ASCII fallback and strips redundant category/author prefixes."""
    if not name or not str(name).strip():
        return "Custom Prop"
    nfkd = unicodedata.normalize("NFKD", str(name))
    ascii_clean = nfkd.encode("ascii", "ignore").decode("ascii").strip()
    ascii_clean = re.sub(r"^own_[a-z0-9]+_", "", ascii_clean, flags=re.IGNORECASE)
    ascii_clean = " ".join(ascii_clean.replace("_", " ").split())
    changed = True
    while changed and ascii_clean:
        changed = False
        words = ascii_clean.split()
        dedup_words: list[str] = []
        for w in words:
            if not dedup_words or dedup_words[-1].lower() != w.lower():
                dedup_words.append(w)
        dedup_str = " ".join(dedup_words).strip()
        if dedup_str != ascii_clean:
            ascii_clean = dedup_str
            changed = True
        for pat in (
            r"^bk\s+custom\s+props?\s+",
            r"^bkcustomprops?\s+",
            r"^custom\s+props?\s+",
        ):
            m = re.match(pat, ascii_clean, flags=re.IGNORECASE)
            if m and len(ascii_clean) > m.end():
                ascii_clean = ascii_clean[m.end():].strip(" -_:")
                changed = True
                break
    if not ascii_clean:
        return "Custom Prop"
    return ascii_clean


def _clean_prop_stem(raw_key: str, extra_prefixes: list[str] | None = None) -> str:
    """Strips repeated category/pack/author prefixes ('own_', 'bkcustomprops_', 'bk_custom_props_', etc.) and collapses duplicate adjacent tokens."""
    slug = re.sub(r"[^a-z0-9_]+", "_", str(raw_key or "prop").strip().lower()).strip("_")
    slug = re.sub(r"^own_", "", slug).strip("_")
    prefixes = [
        "bk_custom_props_",
        "bk_custom_prop_",
        "bkcustomprops_",
        "bkcustomprop_",
        "custom_props_",
        "custom_prop_",
        "customprops_",
        "bkcustom_",
    ]
    if extra_prefixes:
        for ep in extra_prefixes:
            ep_clean = re.sub(r"[^a-z0-9_]+", "_", str(ep or "").strip().lower()).strip("_")
            if ep_clean:
                prefixes.append(f"{ep_clean}_")
                if ep_clean.startswith("bk") and len(ep_clean) > 2:
                    prefixes.append(f"bk_{ep_clean[2:]}_")
    prefixes.sort(key=len, reverse=True)
    changed = True
    while changed and slug:
        changed = False
        for pfx in prefixes:
            if slug.startswith(pfx) and len(slug) > len(pfx):
                slug = slug[len(pfx):].strip("_")
                changed = True
                break
        if not changed:
            m = re.match(r"^bk[a-z0-9]{2,18}_(.+)$", slug)
            if m and m.group(1):
                slug = m.group(1).strip("_")
                changed = True
    parts = [p for p in slug.split("_") if p]
    dedup: list[str] = []
    for p in parts:
        if not dedup or dedup[-1] != p:
            dedup.append(p)
    return "_".join(dedup) or "prop"


# Add support paths for user site-packages (trimesh, PIL, pygltflib, etc.) without shadowing Blender's bundled numpy
try:
    _USER_SITE = site.getusersitepackages()
    if _USER_SITE and os.path.exists(_USER_SITE) and _USER_SITE not in sys.path:
        sys.path.append(_USER_SITE)
except Exception:
    pass

import numpy as np
from PIL import Image


def _get_staging_dir() -> Path:
    env_dir = os.environ.get("RESKATE_STAGING_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "ReSkate_Prop_Studio" / "Staging"
    return Path.home() / "AppData" / "Local" / "ReSkate_Prop_Studio" / "Staging"


# Ensure Studio and PropForge directories are in path (respecting user preferences via env vars or __file__)
_SCRIPT_DIR = Path(__file__).resolve().parent
STUDIO_DIR = Path(os.environ.get("RESKATE_STUDIO_DIR") or _SCRIPT_DIR)
_TEMPLATE_DIR = _SCRIPT_DIR
PROPFORGE_DIR = _SCRIPT_DIR
_DEFAULT_GAME_CAND = STUDIO_DIR.parent if (STUDIO_DIR.parent / "Data" / "Win32").exists() else STUDIO_DIR
GAME_DIR = Path(os.environ.get("RESKATE_GAME_DIR") or _DEFAULT_GAME_CAND)


def _resolve_reskate_cli_exe(game_dir: Path | None = None) -> Path:
    """
    Resolves reskate_cli.exe strictly from the user's Skate folder:
      1. Recommended: <Skate>\\ReSkate Studio\\reskate_cli.exe
      2. Accepted:    <Skate>\\reskate_cli.exe
      3. Explicit RESKATE_CLI_EXE or STUDIO_DIR / reskate_cli.exe
    No drive-wide guessing or hardcoded fallbacks.
    """
    gdir = Path(game_dir) if game_dir else GAME_DIR
    if gdir:
        rec_cand = gdir / "ReSkate Studio" / "reskate_cli.exe"
        if rec_cand.is_file():
            return rec_cand
        alt_cand = gdir / "reskate_cli.exe"
        if alt_cand.is_file():
            return alt_cand
    env_cli = os.environ.get("RESKATE_CLI_EXE", "").strip()
    if env_cli and Path(env_cli).is_file():
        return Path(env_cli)
    for cand in (
        STUDIO_DIR / "reskate_cli.exe",
        _SCRIPT_DIR / "reskate_cli.exe",
    ):
        if cand.is_file():
            return cand
    if gdir:
        return gdir / "ReSkate Studio" / "reskate_cli.exe"
    return STUDIO_DIR / "reskate_cli.exe"


CLI_EXE = _resolve_reskate_cli_exe(GAME_DIR)
os.environ["RESKATE_CLI_EXE"] = str(CLI_EXE)
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
DEFAULT_STAGING_DIR = _get_staging_dir()
DB_PATH = LOCALAPPDATA / "ReSkate" / "profiles" / "offline" / "reskate.sqlite3"
for p in [str(_TEMPLATE_DIR), str(STUDIO_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import ReSkatePropForge as rpf
bsp = rpf
from ReSkatePropForge import (
    SHARED_BUNDLE_HASH,
    clean_items_toc,
    load_templates,
    apply_grind_mode_to_packed,
    build_custom_item_ebx,
    build_patched_imc_ebx,
    build_tube_prism_from_segments,
    compile_contoured_pphys_via_cli,
    compile_mesh_into_donor,
    compile_multishape_havok_batch_via_cli,
    compile_multishape_havok_via_cli,
    compute_mesh_volume_moments,
    convert_havok_to_pphys_mv0,
    demux_riff,
    deterministic_guid_bytes,
    encode_rgba_to_bc7_mipchain,
    fit_havok_box_hull,
    fit_havok_polytope_to_bounds,
    fnv1_32_lower,
    is_mesh_axis_aligned_box,
    patch_physics_ebx_volume_moments,
    patch_prefab_ebx_materials,
    patch_shader_block_depot_rgb,
    remux_riff,
    render_fallback_thumbnail_png,
    simplify_collision_mesh_for_havok,
    split_mesh_into_pphys_islands,
    write_fbmod_v6_full,
)

# Force both modules to read TOC and CAS from the same GAME_DIR
rpf.GAME_DIR = GAME_DIR
bsp.GAME_DIR = GAME_DIR

# ==============================================================================
# PACK-LEVEL TEXTURE DEDUPLICATION CACHE
# ==============================================================================
import threading
_PACK_TEXTURE_CACHE: dict[str, dict] = {}  # sha1_key -> dict(bc7, guid)
_PACK_TEXTURE_LOCK = threading.Lock()
_SEEN_CHUNK_GUIDS: set[str] = set()        # chunk_guid_str


def make_chunk_guid(purpose: str, seed: str, prefix_2b: bytes | None = None) -> bytes:
    """Generates a deterministic 16-byte chunk GUID for texture chunks."""
    raw_g = deterministic_guid_bytes(f"{purpose}:{seed}")
    return (prefix_2b + raw_g[2:]) if prefix_2b is not None else raw_g


def _apply_game_and_staging_runtime_paths(
    game_dir_cand: str | Path | None = None,
    staging_dir_cand: str | Path | None = None,
) -> None:
    """Updates GAME_DIR, CLI_EXE, rpf.GAME_DIR, bsp.GAME_DIR, rpf.CLI_EXE, and rpf.STAGING_DIR after manifest loads."""
    global GAME_DIR, CLI_EXE, DEFAULT_STAGING_DIR
    if game_dir_cand and str(game_dir_cand).strip():
        GAME_DIR = Path(str(game_dir_cand).strip())
        os.environ["RESKATE_GAME_DIR"] = str(GAME_DIR)
    elif os.environ.get("RESKATE_GAME_DIR", "").strip():
        GAME_DIR = Path(os.environ["RESKATE_GAME_DIR"].strip())

    rpf.GAME_DIR = GAME_DIR
    bsp.GAME_DIR = GAME_DIR
    rpf.OODLE_DLL_PATH = GAME_DIR / "oo2core_9_win64.dll"
    CLI_EXE = _resolve_reskate_cli_exe(GAME_DIR)
    rpf.CLI_EXE = CLI_EXE
    os.environ["RESKATE_CLI_EXE"] = str(CLI_EXE)

    if staging_dir_cand and str(staging_dir_cand).strip():
        st_path = Path(str(staging_dir_cand).strip())
    else:
        st_path = _get_staging_dir()
    DEFAULT_STAGING_DIR = st_path
    rpf.STAGING_DIR = st_path
    bsp.STAGING_DIR = st_path
    os.environ["RESKATE_STAGING_DIR"] = str(st_path)

try:
    import trimesh
    HAS_TRIMESH = True
except ImportError:
    HAS_TRIMESH = False


def parse_fbmod_v6(data: bytes) -> list[dict]:
    """Parses a Frosty v6 .fbmod archive into a list of record dicts (with decompressed raw bytes)."""
    assert data[:8] == b"FROSTY\x00\x01", "Invalid FROSTY v6 magic"
    data_off = struct.unpack_from("<Q", data, 12)[0]
    n_recs = struct.unpack_from("<I", data, 20)[0]
    prof_len = data[24]
    pos = 25 + prof_len + 4
    for _ in range(6):
        z = data.index(b"\x00", pos)
        pos = z + 1
    pos += 4  # n_emb
    for _ in range(5):
        pos += 5
        z = data.index(b"\x00", pos)
        pos = z + 1 + 4

    recs = []
    for _ in range(n_recs):
        rtype, idx = struct.unpack_from("<Bi", data, pos)
        pos += 5
        z = data.index(b"\x00", pos)
        name = data[pos:z].decode("utf-8", errors="replace")
        pos = z + 1
        sha1 = data[pos : pos + 20]
        pos += 20
        raw_len = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
        flags = data[pos]
        pos += 1 + 4
        z = data.index(b"\x00", pos)
        tag = data[pos:z].decode("utf-8", errors="replace")
        pos = z + 1
        n_bun = struct.unpack_from("<I", data, pos)[0]
        bundles = [struct.unpack_from("<I", data, pos + 4 + 4 * b)[0] for b in range(n_bun)]
        pos += 4 + 4 * n_bun
        res_type = 0
        res_rid = 0
        res_meta = b""
        res_hash = 0
        if rtype == 2:
            res_type, res_rid, meta_len = struct.unpack_from("<IQI", data, pos)
            res_meta = data[pos + 16 : pos + 16 + meta_len]
            pos += 16 + meta_len
        elif rtype == 3:
            res_hash = struct.unpack_from("<I", data, pos + 16)[0]
            pos += 28
        recs.append({
            "rtype": rtype,
            "idx": idx,
            "name": name,
            "sha1": sha1,
            "raw_len": raw_len,
            "is_added": bool(flags & 0x08),
            "tag": tag,
            "bundles": bundles,
            "res_type": res_type,
            "res_rid": res_rid,
            "res_meta": res_meta,
            "res_hash": res_hash,
        })

    payload_base = data_off + n_recs * 16
    oodle = rpf.get_oodle()
    for i, r in enumerate(recs):
        p_off, p_len = struct.unpack_from("<QQ", data, data_off + i * 16)
        comp = data[payload_base + p_off : payload_base + p_off + p_len]
        raw_out = bytearray()
        cpos = 0
        while cpos + 8 <= len(comp):
            unc_size, flag_type, c_size = struct.unpack_from(">IHH", comp, cpos)
            cpos += 8
            comp_data = comp[cpos : cpos + c_size]
            cpos += c_size
            if flag_type in (0x0070, 0x0000) or unc_size == c_size:
                raw_out.extend(comp_data)
            else:
                out_buf = rpf.ctypes.create_string_buffer(unc_size)
                ret = oodle.OodleLZ_Decompress(
                    comp_data, c_size, out_buf, unc_size, 1, 1, 0, None, 0, None, None, None, 0, 3
                )
                if ret > 0:
                    raw_out.extend(out_buf.raw[:ret])
        r["raw"] = bytes(raw_out)
    return recs


def parse_manifest(manifest_path: Path) -> tuple[list[dict], dict]:
    """Loads manifest JSON and normalizes both single-prop and multi-prop schemas."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    log_input_read(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    coexist_mode = bool(data.get("coexist_mode", True))
    author_name = str(data.get("author_name") or os.environ.get("RESKATE_AUTHOR_NAME") or "").strip()
    manifest_game_dir = str(data.get("game_dir") or os.environ.get("RESKATE_GAME_DIR") or "").strip()
    manifest_staging_dir = data.get("output_staging_dir") or _get_staging_dir()
    _apply_game_and_staging_runtime_paths(manifest_game_dir, manifest_staging_dir)

    if "props" in data and isinstance(data["props"], list):
        # Multi-prop pack queue schema
        props_list = data["props"]
        raw_pack = str(data.get("target_mod_name") or data.get("pack_name") or data.get("mod_name") or "BlenderAuthoredProps").strip()
        if author_name and not raw_pack.lower().startswith(f"{author_name.lower()}-"):
            target_mod = f"{author_name}-{raw_pack}" if "-" not in raw_pack else raw_pack
        else:
            target_mod = raw_pack
        default_ns = f"{author_name}/{target_mod}" if author_name else target_mod
        meta = {
            "pack_name": data.get("pack_name", target_mod),
            "target_mod_name": target_mod,
            "author_name": author_name,
            "game_dir": str(GAME_DIR),
            "output_staging_dir": Path(manifest_staging_dir),
            "include_2a_variant": bool(data.get("include_2a_variant", False)),
            "export_fbmod_only": bool(data.get("export_fbmod_only", False)),
            "merge_existing_mod": bool(data.get("merge_existing_mod", True)),
            "auto_disable_old_mod_versions": bool(data.get("auto_disable_old_mod_versions", True)),
            "atlas_resolution": int(data.get("atlas_resolution", 2048) or 2048),
            "remove_prop_keys": list(data.get("remove_prop_keys", [])),
            "coexist_mode": coexist_mode,
            "pack_version": str(data.get("pack_version", "0.1.0" if coexist_mode else "1.5.0")),
            "addon_version": data.get("addon_version", None),
            "coexist_namespace": str(data.get("coexist_namespace") or default_ns),
            "pack_slug": data.get("pack_slug", None),
            "bundle_stem_base": str(data.get("bundle_stem_base", "items/customprops")),
            "build_note": str(data.get("build_note", "")),
            "description": str(data.get("pack_description") or data.get("description") or "").strip(),
            "deploy_to_mods": bool(data.get("deploy_to_mods", coexist_mode)),
            "mod_icon_path": str(data.get("mod_icon_path") or "").strip(),
            "has_custom_pack_icon": bool(data.get("has_custom_pack_icon", False)),
            "use_pack_icon_as_first_prop": bool(data.get("use_pack_icon_as_first_prop", False)),
            "mod_readme_path": str(data.get("mod_readme_path") or "").strip(),
            "compiler_backend": str(data.get("compiler_backend") or "CPU").upper(),
            "compile_pipeline_mode": str(
                data.get("compile_pipeline_mode") or os.environ.get("RESKATE_PIPELINE_MODE") or "FAST_OPTIMIZED"
            ).strip().upper(),
        }
    else:
        # Single-prop schema
        props_list = [data]
        raw_pack = str(data.get("target_mod_name") or "BlenderAuthoredProps").strip()
        if author_name and not raw_pack.lower().startswith(f"{author_name.lower()}-"):
            target_mod = f"{author_name}-{raw_pack}" if "-" not in raw_pack else raw_pack
        else:
            target_mod = raw_pack
        default_ns = f"{author_name}/{target_mod}" if author_name else target_mod
        meta = {
            "pack_name": target_mod,
            "target_mod_name": target_mod,
            "author_name": author_name,
            "game_dir": str(GAME_DIR),
            "output_staging_dir": Path(manifest_staging_dir),
            "include_2a_variant": bool(data.get("include_2a_variant", False)),
            "export_fbmod_only": bool(data.get("export_fbmod_only", False)),
            "merge_existing_mod": bool(data.get("merge_existing_mod", True)),
            "auto_disable_old_mod_versions": bool(data.get("auto_disable_old_mod_versions", True)),
            "atlas_resolution": int(data.get("atlas_resolution", 2048) or 2048),
            "remove_prop_keys": list(data.get("remove_prop_keys", [])),
            "coexist_mode": coexist_mode,
            "pack_version": str(data.get("pack_version", "0.1.0" if coexist_mode else "1.5.0")),
            "addon_version": data.get("addon_version", None),
            "coexist_namespace": str(data.get("coexist_namespace") or default_ns),
            "pack_slug": data.get("pack_slug", None),
            "bundle_stem_base": str(data.get("bundle_stem_base", "items/customprops")),
            "build_note": str(data.get("build_note", "")),
            "description": str(data.get("pack_description") or data.get("description") or "").strip(),
            "deploy_to_mods": bool(data.get("deploy_to_mods", coexist_mode)),
            "mod_icon_path": str(data.get("mod_icon_path") or "").strip(),
            "has_custom_pack_icon": bool(data.get("has_custom_pack_icon", False)),
            "use_pack_icon_as_first_prop": bool(data.get("use_pack_icon_as_first_prop", False)),
            "mod_readme_path": str(data.get("mod_readme_path") or "").strip(),
            "compiler_backend": str(data.get("compiler_backend") or "CPU").upper(),
            "compile_pipeline_mode": str(
                data.get("compile_pipeline_mode") or os.environ.get("RESKATE_PIPELINE_MODE") or "FAST_OPTIMIZED"
            ).strip().upper(),
        }

    return props_list, meta


def _normalize_prop_key(raw_key: str) -> str:
    slug = _clean_prop_stem(raw_key)
    return clamp_slug(f"own_customprops_{slug}", 36)


def _get_pack_storage_dir(staging_dir: Path, mod_name: str) -> Path:
    """Returns the persistent pack storage directory (`Staging_BlenderPropTest/Packs/<mod_name>`)."""
    return Path(staging_dir) / "Packs" / mod_name


def _file_needs_copy(src: Path, dst: Path) -> bool:
    """Returns True if dst does not exist or differs in size or byte content from src."""
    if not dst.exists() or not dst.is_file():
        return True
    try:
        if src.stat().st_size != dst.stat().st_size:
            return True
        return not filecmp.cmp(src, dst, shallow=False)
    except Exception:
        return True


def _refresh_pack_asset_cache_for_prop(prop_entry: dict, staging_dir: Path | None = None, mod_name: str = "") -> None:
    """
    Ensures that whenever a prop has freshly staged textures in `staging_dir` or `staging_dir/Pack_Queue`,
    any corresponding cached copy inside `Packs/<PackName>/assets/` is refreshed if different,
    preventing stale maps while strictly isolating the refresh to the target mod pack.
    """
    try:
        base_staging = Path(staging_dir or prop_entry.get("output_staging_dir") or DEFAULT_STAGING_DIR)
        if base_staging.name == "Pack_Queue":
            base_staging = base_staging.parent
        pk = _normalize_prop_key(prop_entry.get("prop_id") or prop_entry.get("prop_key", "own_customprops_prop"))
        target_mod = (mod_name or prop_entry.get("target_mod_name") or "").strip()
        if not target_mod:
            return
        packs_root = base_staging / "Packs"
        if not packs_root.is_dir():
            return
        target_ad = packs_root / target_mod / "assets"
        if not target_ad.is_dir():
            if (packs_root / target_mod).is_dir():
                target_ad.mkdir(parents=True, exist_ok=True)
            else:
                return

        mat_tex_list = prop_entry.get("material_textures") or []
        for idx, mt in enumerate(mat_tex_list):
            if not isinstance(mt, dict):
                continue
            for mt_key, staged_name, pack_suf in (
                ("texture_path", f"ReSkate_Mat_{pk}_{idx}.png", f"{pk}_mat_{idx}.png"),
                ("normal_texture_path", f"ReSkate_Mat_{pk}_{idx}_n.png", f"{pk}_mat_{idx}_n.png"),
                ("mask_texture_path", f"ReSkate_Mat_{pk}_{idx}_msk.png", f"{pk}_mat_{idx}_msk.png"),
            ):
                cur_p_str = mt.get(mt_key)
                cur_p = Path(cur_p_str) if cur_p_str else None
                # Check if a fresher staged file exists in base_staging or Pack_Queue
                freshest_src = None
                for cand in (base_staging / "Pack_Queue" / staged_name, base_staging / staged_name, cur_p):
                    if cand is not None and cand.exists() and cand.is_file():
                        if freshest_src is None or cand.stat().st_mtime_ns > freshest_src.stat().st_mtime_ns:
                            freshest_src = cand
                if freshest_src is not None:
                    for dst_name in (pack_suf, staged_name):
                        dst_p = target_ad / dst_name
                        if freshest_src.resolve() != dst_p.resolve() and _file_needs_copy(freshest_src, dst_p):
                            try:
                                shutil.copy2(freshest_src, dst_p)
                            except Exception:
                                pass
                    if cur_p is not None and cur_p.exists() and freshest_src.resolve() != cur_p.resolve():
                        if _file_needs_copy(freshest_src, cur_p):
                            try:
                                shutil.copy2(freshest_src, cur_p)
                            except Exception:
                                pass
    except Exception:
        pass


def _merge_existing_mod_props_if_requested(props_list: list[dict], meta: dict) -> list[dict]:
    """
    When `merge_existing_mod` is True in `meta` (default True), reads the persistent pack registry
    at `Staging_BlenderPropTest/Packs/<mod_name>/pack_registry.json` (or `.reskate_source/props_registry.json`),
    never reading from `Mods\\`, and merges existing prop entries with `props_list`.
    """
    if not meta.get("merge_existing_mod", True):
        return props_list

    mod_name = meta.get("target_mod_name", "")
    staging_dir = Path(meta.get("output_staging_dir", DEFAULT_STAGING_DIR))
    if not mod_name:
        return props_list

    pack_dir = _get_pack_storage_dir(staging_dir, mod_name)
    reg_candidates = [
        pack_dir / "pack_registry.json",
        staging_dir / mod_name / ".reskate_source" / "props_registry.json",
        staging_dir / "mods" / mod_name / ".reskate_source" / "props_registry.json",
    ]
    if not meta.get("coexist_mode", True):
        reg_candidates.insert(0, GAME_DIR / "Mods" / mod_name / ".reskate_source" / "props_registry.json")
    reg_file = next((p for p in reg_candidates if p.exists()), None)
    if reg_file is None:
        return props_list

    try:
        log_input_read(reg_file, coexist_mode=bool(meta.get("coexist_mode", True)))
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        existing_entries = reg_data.get("props", [])
    except Exception as ex:
        print(f"  [!] Could not read existing pack registry ({ex}); proceeding with new props.", flush=True)
        return props_list

    remove_set = {_normalize_prop_key(k) for k in meta.get("remove_prop_keys", []) if k}

    new_by_key: dict[str, dict] = {}
    new_order: list[str] = []
    for p in props_list:
        pk = _normalize_prop_key(p.get("prop_id") or p.get("prop_key", "own_customprops_prop"))
        if pk in remove_set:
            continue
        new_by_key[pk] = p
        if pk not in new_order:
            new_order.append(pk)

    merged: list[dict] = []
    seen_keys: set[str] = set()
    kept_existing = 0

    # Place newly compiled/ordered props FIRST in their exact user-specified order (`new_order`)
    for pk in new_order:
        if pk not in seen_keys:
            merged.append(new_by_key[pk])
            seen_keys.add(pk)

    # Append any kept existing props from pack_registry.json that were not in the current selection
    for old_p in existing_entries:
        if not isinstance(old_p, dict) or bool(old_p.get("is_pack_icon_prop", False)):
            continue
        pk = _normalize_prop_key(old_p.get("prop_id") or old_p.get("prop_key", ""))
        if pk in seen_keys or pk in remove_set or pk.endswith("_pack_icon_tile"):
            continue
        glb_p = old_p.get("glb_path")
        if glb_p and Path(glb_p).exists():
            merged.append(old_p)
            seen_keys.add(pk)
            kept_existing += 1

    for mp in merged:
        if isinstance(mp, dict):
            _refresh_pack_asset_cache_for_prop(mp, staging_dir=staging_dir, mod_name=mod_name)

    if kept_existing > 0:
        print(
            f"  [+] Additive Mod Merge: preserved {kept_existing} existing prop(s) + {len(new_order)} new/updated prop(s) -> {len(merged)} total props in '{mod_name}'",
            flush=True,
        )
    return merged


def _backup_pack_build_if_exists(staging_dir: Path, target_mod_dir: Path, mod_name: str, max_backups: int = 5) -> Path | None:
    """
    Copies the previous staged build (`target_mod_dir` and `pack_registry.json`) into
    `Staging_BlenderPropTest/Packs/<mod_name>/backups/<timestamp>/` before overwriting,
    keeping up to `max_backups` rolling backups. Never reads from `Mods\\`.
    """
    try:
        pack_dir = _get_pack_storage_dir(staging_dir, mod_name)
        reg_file = pack_dir / "pack_registry.json"
        if not target_mod_dir.exists() and not reg_file.exists():
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bkp_root = pack_dir / "backups"
        bkp_dir = bkp_root / ts
        bkp_dir.mkdir(parents=True, exist_ok=True)
        if target_mod_dir.exists():
            shutil.copytree(target_mod_dir, bkp_dir / "compiled_mod", dirs_exist_ok=True)
        if reg_file.exists():
            shutil.copy2(reg_file, bkp_dir / "pack_registry.json")
        # Prune oldest backups beyond max_backups
        existing_bkps = sorted([d for d in bkp_root.iterdir() if d.is_dir()], key=lambda d: d.name)
        while len(existing_bkps) > max_backups:
            oldest = existing_bkps.pop(0)
            shutil.rmtree(oldest, ignore_errors=True)
        return bkp_dir
    except Exception as ex:
        print(f"  [!] Warning: Could not create rolling backup for '{mod_name}': {ex}", flush=True)
        return None


def _persist_mod_props_registry(
    mod_dir: Path,
    props_list: list[dict],
    mod_name: str,
    staging_dir: Path = DEFAULT_STAGING_DIR,
    meta: dict | None = None,
) -> None:
    """
    Saves self-contained source assets and `pack_registry.json` inside
    `Staging_BlenderPropTest/Packs/<mod_name>/` (and `.reskate_source/props_registry.json` inside `<mod_dir>`)
    so future compilations targeting this pack can add/update/remove props without ever reading `Mods\\`.
    """
    try:
        pack_dir = _get_pack_storage_dir(staging_dir, mod_name)
        assets_dir = pack_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        src_dir = Path(mod_dir) / ".reskate_source"
        src_assets_dir = src_dir / "assets"
        src_assets_dir.mkdir(parents=True, exist_ok=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        stored_props = []
        for entry in props_list:
            if isinstance(entry, dict) and bool(entry.get("is_pack_icon_prop", False)):
                continue
            p_copy = copy.deepcopy(entry)
            raw_id = p_copy.get("prop_id") or p_copy.get("prop_key", "own_customprops_prop")
            pk = _normalize_prop_key(raw_id)
            prop_id_clean = pk.replace("own_customprops_", "")
            p_copy["prop_key"] = pk
            p_copy["prop_id"] = prop_id_clean
            p_copy["built_at"] = now_iso
            p_copy["compiler_version"] = COMPILER_VERSION

            for path_key, suffix in (
                ("glb_path", f"{pk}.glb"),
                ("col_glb_path", f"{pk}_col.glb"),
                ("pphys_glb_path", f"{pk}_pphys.glb"),
                ("texture_path", f"{pk}_atlas.png"),
                ("thumb_256_path", f"{pk}_thumb_256.png"),
                ("thumb_768_path", f"{pk}_thumb_768.png"),
            ):
                orig_val = p_copy.get(path_key)
                if orig_val and Path(orig_val).exists():
                    dst = assets_dir / suffix
                    if Path(orig_val).resolve() != dst.resolve():
                        shutil.copy2(orig_val, dst)
                    dst_src = src_assets_dir / suffix
                    if Path(orig_val).resolve() != dst_src.resolve():
                        shutil.copy2(orig_val, dst_src)
                    p_copy[path_key] = str(dst)

            mat_tex_list = p_copy.get("material_textures")
            if isinstance(mat_tex_list, list):
                for idx, mt in enumerate(mat_tex_list):
                    if isinstance(mt, dict):
                        for mt_key, mt_suf in (
                            ("texture_path", f"{pk}_mat_{idx}.png"),
                            ("normal_texture_path", f"{pk}_mat_{idx}_n.png"),
                            ("mask_texture_path", f"{pk}_mat_{idx}_msk.png"),
                        ):
                            mt_p = mt.get(mt_key)
                            if mt_p and Path(mt_p).exists():
                                dst_mt = assets_dir / mt_suf
                                if Path(mt_p).resolve() != dst_mt.resolve():
                                    shutil.copy2(mt_p, dst_mt)
                                dst_mt_src = src_assets_dir / mt_suf
                                if Path(mt_p).resolve() != dst_mt_src.resolve():
                                    shutil.copy2(mt_p, dst_mt_src)
                                mt[mt_key] = str(dst_mt)

            stored_props.append(p_copy)

        reg_payload = {
            "schema": 2,
            "mod_name": mod_name,
            "author_name": (meta or {}).get("author_name", ""),
            "pack_version": (meta or {}).get("pack_version", "0.1.0"),
            "compiler_version": COMPILER_VERSION,
            "addon_version": (meta or {}).get("addon_version", None),
            "updated": now_iso,
            "props_count": len(stored_props),
            "props": stored_props,
        }
        reg_json = json.dumps(reg_payload, indent=2)
        (pack_dir / "pack_registry.json").write_text(reg_json, encoding="utf-8")
        (src_dir / "props_registry.json").write_text(reg_json, encoding="utf-8")
    except Exception as ex:
        print(f"  [!] Warning: Failed to persist pack registry for {mod_name}: {ex}", flush=True)


def _build_pack_icon_tile_glb(glb_path: Path, pphys_glb_path: Path) -> None:
    """
    Synthesizes a 0.60m x 0.025m x 0.60m flat square tile GLB in glTF Y-up space
    (X in [-0.30, +0.30], Y in [0.00, +0.025], Z in [-0.30, +0.30]) with 1:1 UVs (0..1)
    on the top (+Y) face for the Pack Icon texture, plus a 0.60m x 0.30m x 0.60m
    (30cm tall, Y in [0.00, +0.30]) 3D box GLB for its Quick Drop placement proxy (`_pphys`).
    """
    glb_path.parent.mkdir(parents=True, exist_ok=True)
    hx = 0.30
    hz = 0.30
    y0 = 0.00
    y1 = 0.025

    verts = np.array([
        # Top face (+Y = y1), normal (0, 1, 0): X left-to-right (-hx..+hx), Z back-to-front (-hz..+hz)
        [-hx, y1, +hz], [+hx, y1, +hz], [+hx, y1, -hz], [-hx, y1, -hz],
        # Bottom face (-Y = y0)
        [-hx, y0, -hz], [+hx, y0, -hz], [+hx, y0, +hz], [-hx, y0, +hz],
        # Front face (+Z = +hz)
        [-hx, y0, +hz], [+hx, y0, +hz], [+hx, y1, +hz], [-hx, y1, +hz],
        # Back face (-Z = -hz)
        [+hx, y0, -hz], [-hx, y0, -hz], [-hx, y1, -hz], [+hx, y1, -hz],
        # Right face (+X = +hx)
        [+hx, y0, +hz], [+hx, y0, -hz], [+hx, y1, -hz], [+hx, y1, +hz],
        # Left face (-X = -hx)
        [-hx, y0, -hz], [-hx, y0, +hz], [-hx, y1, +hz], [-hx, y1, -hz],
    ], dtype=np.float32)

    uvs = np.array([
        # Top face: full 0..1 Pack Icon image
        [0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0],
        # Bottom & thin border faces
        [0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0],
        [0.0, 0.0], [1.0, 0.0], [1.0, 0.05], [0.0, 0.05],
        [0.0, 0.95], [1.0, 0.95], [1.0, 1.0], [0.0, 1.0],
        [0.95, 0.0], [1.0, 0.0], [1.0, 1.0], [0.95, 1.0],
        [0.0, 0.0], [0.05, 0.0], [0.05, 1.0], [0.0, 1.0],
    ], dtype=np.float32)

    faces = np.array([
        [0, 1, 2], [0, 2, 3],       # Top (+Y)
        [4, 5, 6], [4, 6, 7],       # Bottom (-Y)
        [8, 9, 10], [8, 10, 11],    # Front (+Z)
        [12, 13, 14], [12, 14, 15], # Back (-Z)
        [16, 17, 18], [16, 18, 19], # Right (+X)
        [20, 21, 22], [20, 22, 23], # Left (-X)
    ], dtype=np.int32)

    visual = trimesh.visual.TextureVisuals(uv=uvs)
    tile_tm = trimesh.Trimesh(vertices=verts, faces=faces, visual=visual, process=False)
    tile_tm.export(str(glb_path))

    # Build a 0.60m x 0.30m x 0.60m (30cm tall) 3D placement proxy box (Y = 0.00m .. +0.30m)
    pphys_tm = trimesh.creation.box(extents=[0.60, 0.30, 0.60])
    pphys_tm.apply_translation([0.0, 0.15, 0.0])
    pphys_tm.export(str(pphys_glb_path))


def _ensure_pack_icon_first_prop(props_list: list[dict], meta: dict, staging_dir: Path) -> list[dict]:
    """
    When `use_pack_icon_as_first_prop` and `has_custom_pack_icon` are True in `meta` and `mod_icon_path`
    exists, auto-generates a small square Pack Icon tile prop (`0.60m x 0.60m` flat surface with a
    `0.60m x 0.30m x 0.60m` 30cm tall Quick Drop placement/grab proxy) and prepends it at index 0 (#1)
    of `props_list` so Skate. uses the custom Pack Icon as the Quick Drop Tab Icon.
    """
    cleaned = [p for p in props_list if isinstance(p, dict) and not bool(p.get("is_pack_icon_prop", False))]
    if not cleaned:
        return props_list
    if not bool(meta.get("use_pack_icon_as_first_prop", False)) or not bool(meta.get("has_custom_pack_icon", False)):
        return cleaned

    icon_p_str = str(meta.get("mod_icon_path") or "").strip()
    if not icon_p_str or not Path(icon_p_str).is_file():
        return cleaned

    try:
        mod_name = str(meta.get("target_mod_name") or meta.get("pack_name") or "CustomProps").strip()
        pack_dir = _get_pack_storage_dir(staging_dir, mod_name)
        icon_prop_dir = pack_dir / "pack_icon_prop"
        icon_prop_dir.mkdir(parents=True, exist_ok=True)

        glb_path = icon_prop_dir / "pack_icon_tile.glb"
        pphys_glb_path = icon_prop_dir / "pack_icon_tile_pphys.glb"
        _build_pack_icon_tile_glb(glb_path, pphys_glb_path)

        tex_path = icon_prop_dir / "pack_icon_tex_1024.png"
        t256_path = icon_prop_dir / "pack_icon_thumb_256.png"
        t768_path = icon_prop_dir / "pack_icon_thumb_768.png"

        with Image.open(icon_p_str) as raw_im:
            rgba = raw_im.convert("RGBA")
            rgba.resize((1024, 1024), Image.Resampling.LANCZOS).save(str(tex_path), format="PNG")
            rgba.resize((256, 256), Image.Resampling.LANCZOS).save(str(t256_path), format="PNG")
            rgba.resize((768, 768), Image.Resampling.LANCZOS).save(str(t768_path), format="PNG")

        first_user_prop = cleaned[0]
        custom_qd_tab = str(first_user_prop.get("custom_qd_tab") or "").strip()
        category = str(first_user_prop.get("category") or "Custom Props").strip()

        clean_mod_title = re.sub(r"^[^-]+-", "", mod_name).replace("_", " ").strip() or mod_name
        mod_slug = re.sub(r"[^a-z0-9_]+", "_", mod_name.lower()).strip("_")[:16] or "pack"
        prop_id = clamp_slug(f"{mod_slug}_pack_icon_tile", 22)
        prop_key = clamp_slug(f"own_customprops_{prop_id}", 36)

        icon_prop_entry = {
            "name": f"{clean_mod_title} Pack Icon",
            "prop_id": prop_id,
            "prop_key": prop_key,
            "display_name": f"{clean_mod_title} Icon Tile",
            "family_title": "",
            "category": category,
            "custom_qd_tab": custom_qd_tab,
            "glb_path": str(glb_path),
            "col_glb_path": str(pphys_glb_path),
            "col_thickened": False,
            "pphys_glb_path": str(pphys_glb_path),
            "texture_path": str(tex_path),
            "material_textures": [],
            "look_mode": "AS_IS",
            "atlas_resolution": 1024,
            "thumb_256_path": str(t256_path),
            "thumb_768_path": str(t768_path),
            "collision_mode": "NO_COLLISION",
            "no_collision_placement_mode": "MATCH_MESH",
            "placement_proxy_mode": "BOUNDING_BOX",
            "pphys_target_tris": 12,
            "max_col_tris": 12,
            "anti_lip_enabled": False,
            "anti_lip_sink_m": 0.0,
            "surface_material_code": 96,
            "surface_id": "Concrete_Solid",
            "surface_label": "Pack Icon Tile (No Collision)",
            "material_surfaces": [],
            "face_surface_groups": [],
            "grind_paths": [],
            "round_rail": False,
            "enable_custom_textures": True,
            "roughness_multiplier": 0.65,
            "metallic_multiplier": 0.0,
            "specular_multiplier": 0.35,
            "roughness_map_mix": 0.0,
            "metallic_map_mix": 0.0,
            "use_native_771_detail_slots": False,
            "use_2x2m_detail_shader": False,
            "albedo_tint": [1.0, 1.0, 1.0],
            "lights": [],
            "experimental": {"no_skater_collision": True, "no_collision_placement_mode": "MATCH_MESH"},
            "decal_shader_mode": "CUTOUT_273",
            "is_pack_icon_prop": True,
            "tris": 12,
        }
        print(
            f"  [Pack Icon #1] Prepended '{icon_prop_entry['display_name']}' ({prop_key}) at #1 in pack "
            f"(0.60x0.60m tile + 30cm tall Quick Drop placement/grab proxy).",
            flush=True,
        )
        return [icon_prop_entry] + cleaned
    except Exception as ex:
        print(f"  [!] Warning: Could not synthesize Pack Icon #1 prop ({ex}); continuing without it.", flush=True)
        return cleaned


def _apply_node_transform_safe(g_copy: trimesh.Trimesh, mat: np.ndarray | None) -> None:
    """Applies a 4x4 scene-graph transform to a Trimesh, falling back to pure numpy if scipy/trimesh raises."""
    if mat is None:
        return
    mat_arr = np.asarray(mat, dtype=np.float64)
    if np.allclose(mat_arr, np.eye(4)):
        return
    neg_det = float(np.linalg.det(mat_arr[:3, :3])) < 0.0
    try:
        g_copy.apply_transform(mat_arr)
        if neg_det:
            g_copy.invert()
        return
    except Exception:
        pass
    v_arr = np.asarray(g_copy.vertices, dtype=np.float64)
    if len(v_arr) > 0:
        v_h = np.hstack([v_arr, np.ones((len(v_arr), 1), dtype=np.float64)])
        g_copy.vertices = (v_h @ mat_arr.T)[:, :3]
    if neg_det and len(g_copy.faces) > 0:
        g_copy.faces = np.asarray(g_copy.faces)[:, ::-1]


def _compute_vertex_normals_safe(sm: trimesh.Trimesh, sv: np.ndarray, sf: np.ndarray) -> np.ndarray:
    """Extracts vertex normals from Trimesh, falling back to pure-numpy face-normal accumulation if scipy fails."""
    try:
        sn = np.asarray(sm.vertex_normals, dtype=np.float32)
        if len(sn) == len(sv) and not np.isnan(sn).any():
            return sn
    except Exception:
        pass
    sn = np.zeros_like(sv, dtype=np.float32)
    if len(sv) > 0 and len(sf) > 0:
        v0 = sv[sf[:, 0]]
        v1 = sv[sf[:, 1]]
        v2 = sv[sf[:, 2]]
        fn = np.cross(v1 - v0, v2 - v0)
        np.add.at(sn, sf[:, 0], fn)
        np.add.at(sn, sf[:, 1], fn)
        np.add.at(sn, sf[:, 2], fn)
        norms = np.linalg.norm(sn, axis=1, keepdims=True)
        valid = norms[:, 0] > 1e-8
        sn[valid] /= norms[valid]
        sn[~valid, 1] = 1.0
    elif len(sn) > 0:
        sn[:, 1] = 1.0
    return sn


def load_mesh_geometry_and_submeshes(glb_path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[trimesh.Trimesh]]:
    """Loads concatenated vertices, normals, UVs, faces, and individual sub-meshes (for multi-material surface overrides) from GLB, applying full glTF scene-graph node transforms while preserving per-submesh UVs."""
    p = Path(glb_path)
    if not p.exists():
        raise FileNotFoundError(f"GLB not found: {p}")
    log_input_read(p)

    if not HAS_TRIMESH:
        raise RuntimeError("trimesh package is required for GLB mesh loading")

    loaded = trimesh.load(str(p), force=None, process=False)
    submeshes = []
    if isinstance(loaded, trimesh.Scene):
        if len(loaded.geometry) == 0:
            raise ValueError(f"GLB scene has no geometry: {p}")
        # Apply scene-graph node transforms (export_yup rotation, object/parent scale, etc.) to each geometry node
        geom_order = {gname: idx for idx, gname in enumerate(loaded.geometry.keys())}
        try:
            nodes_geom = sorted(
                list(loaded.graph.nodes_geometry),
                key=lambda n: geom_order.get(loaded.graph.get(n)[1], 999999),
            )
        except Exception:
            nodes_geom = []
        if nodes_geom:
            for node_name in nodes_geom:
                try:
                    mat, geom_name = loaded.graph.get(node_name)
                    g = loaded.geometry.get(geom_name)
                    if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0:
                        g_copy = g.copy()
                        _apply_node_transform_safe(g_copy, mat)
                        submeshes.append(g_copy)
                except Exception:
                    pass
        if not submeshes:
            for gname, g in loaded.geometry.items():
                if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0:
                    g_copy = g.copy()
                    # Try finding any node referencing this geometry to still apply its transform
                    try:
                        for node_name in loaded.graph.nodes:
                            mat, gn = loaded.graph.get(node_name)
                            if gn == gname and mat is not None:
                                _apply_node_transform_safe(g_copy, mat)
                                break
                    except Exception:
                        pass
                    submeshes.append(g_copy)
        if not submeshes:
            raise ValueError(f"GLB scene has no valid triangle meshes: {p}")
    elif isinstance(loaded, trimesh.Trimesh):
        submeshes = [loaded]
    else:
        mesh = trimesh.load(str(p), force="mesh", process=False)
        submeshes = [mesh]

    # Concatenate vertices, normals, UVs, and faces directly from submeshes so trimesh.util.concatenate
    # never strips visual.uv when submeshes have distinct PBRMaterial visuals.
    all_v = []
    all_n = []
    all_uv = []
    all_f = []
    v_offset = 0
    for sm in submeshes:
        sv = np.asarray(sm.vertices, dtype=np.float32)
        sf = np.asarray(sm.faces, dtype=np.int32)
        sn = _compute_vertex_normals_safe(sm, sv, sf)
        if hasattr(sm, "visual") and hasattr(sm.visual, "uv") and sm.visual.uv is not None and len(sm.visual.uv) == len(sv):
            suv = np.asarray(sm.visual.uv, dtype=np.float32)
        else:
            suv = np.zeros((len(sv), 2), dtype=np.float32)
        all_v.append(sv)
        all_n.append(sn)
        all_uv.append(suv)
        all_f.append(sf + v_offset)
        v_offset += len(sv)

    verts = np.vstack(all_v) if all_v else np.zeros((0, 3), dtype=np.float32)
    normals = np.vstack(all_n) if all_n else np.zeros((0, 3), dtype=np.float32)
    uvs = np.vstack(all_uv) if all_uv else np.zeros((0, 2), dtype=np.float32)
    faces = np.vstack(all_f) if all_f else np.zeros((0, 3), dtype=np.int32)

    return verts, normals, uvs, faces, submeshes


def _snap_to_power_of_two(val: int, min_val: int = 64, max_val: int = 4096) -> int:
    v = max(min_val, min(max_val, int(val)))
    best = min_val
    for p in (64, 128, 256, 512, 1024, 2048, 4096):
        if min_val <= p <= max_val and abs(v - p) <= abs(v - best):
            best = p
    return best


def _get_bc7_mip_spec(tex_w_or_res: int, tex_h: int | None = None) -> tuple[int, int, int, list[int], int]:
    """
    Returns `(tex_w, tex_h, n_mips, mip_sizes, total_sz)` for any power-of-two BC7 texture chain
    (square OR rectangular, such as `256x4096`, `512x4096`, `1024x1024`, `2048x2048`, `4096x4096`).
    """
    w = _snap_to_power_of_two(tex_w_or_res, 64, 4096)
    h = _snap_to_power_of_two(tex_h if tex_h is not None else tex_w_or_res, 64, 4096)
    n_mips = int(round(math.log2(max(w, h)))) + 1
    mips: list[int] = []
    for m in range(n_mips):
        mw = max(1, w >> m)
        mh = max(1, h >> m)
        bw = max(1, (mw + 3) // 4)
        bh = max(1, (mh + 3) // 4)
        mips.append(bw * bh * 16)
    return w, h, n_mips, mips, sum(mips)


def _get_bc7_mip_spec_from_bytes_len(
    raw_bc7_len: int,
    hint_dims: tuple[int, int] | None = None,
) -> tuple[int, int, int, list[int], int]:
    """Infers `(tex_w, tex_h, n_mips, mip_sizes, total_sz)` from `hint_dims` or an encoded BC7 mipchain byte length."""
    if hint_dims is not None and len(hint_dims) == 2:
        hw, hh = int(hint_dims[0]), int(hint_dims[1])
        if hw >= 64 and hh >= 64:
            spec = _get_bc7_mip_spec(hw, hh)
            if spec[4] == int(raw_bc7_len):
                return spec
    # Exact match against all power-of-two square & rectangular combinations in {64..4096}
    pots = (1024, 2048, 4096, 512, 256, 128, 64)
    for w in pots:
        for h in pots:
            spec = _get_bc7_mip_spec(w, h)
            if spec[4] == int(raw_bc7_len):
                return spec
    if raw_bc7_len >= 20000000:
        return _get_bc7_mip_spec(4096, 4096)
    if raw_bc7_len >= 5000000:
        return _get_bc7_mip_spec(2048, 2048)
    return _get_bc7_mip_spec(1024, 1024)


def _resolve_submesh_material_entry(sm, idx: int, entries: list) -> tuple[int, dict | None]:
    """
    Resolves the matching dict in `entries` (`material_textures` or `material_surfaces`)
    for submesh `sm` by matching `sm.visual.material.name` first, falling back to `entries[idx]`.
    """
    if not isinstance(entries, list) or not entries:
        return idx, None
    sm_mat = getattr(getattr(sm, "visual", None), "material", None)
    sm_mat_name = str(getattr(sm_mat, "name", "") or "").strip() if sm_mat is not None else ""
    if sm_mat_name:
        if idx < len(entries) and isinstance(entries[idx], dict):
            cand_nm = str(entries[idx].get("material_name") or entries[idx].get("name") or "").strip()
            if cand_nm == sm_mat_name:
                return idx, entries[idx]
        for e_i, ent in enumerate(entries):
            if isinstance(ent, dict):
                cand_nm = str(ent.get("material_name") or ent.get("name") or "").strip()
                if cand_nm == sm_mat_name:
                    return e_i, ent
        sm_low = sm_mat_name.lower()
        for e_i, ent in enumerate(entries):
            if isinstance(ent, dict):
                cand_nm = str(ent.get("material_name") or ent.get("name") or "").strip().lower()
                if cand_nm == sm_low:
                    return e_i, ent
    if idx < len(entries) and isinstance(entries[idx], dict):
        return idx, entries[idx]
    return idx, None


def build_multi_material_atlas_safe(
    parts: list[dict],
    atlas_size: int = 1024,
    preserve_alpha: bool = False,
) -> tuple[Image.Image, list[np.ndarray]]:
    """
    Packs 1..N material sub-meshes into a single `atlas_size x atlas_size` RGBA image.
    - When N == 1 (or all parts share the exact same image): returns the single texture and
      leaves the authored UVs 100% untouched (preserving native hardware UV tiling).
    - When N > 1 with distinct textures/colors: packs each part's texture into a quadrant grid:
      * Decal overlay slots (`is_decal=True`) are NEVER tiled (`np.tile` is suppressed);
        when `preserve_alpha=True` (Shader 0x273 Cutout), transparent pixels and out-of-[0..1] CLIP
        borders remain transparent (alpha=0) with foreground RGB dilated outward to prevent mip bleeding.
      * Opaque or tiled cutout slots compute true UV span (`u_max_raw - u_min_raw`) and cyclically phase-align (`np.roll`)
        the source image at `(u_min_raw, v_max_raw)` so UV islands crossing 0.0 never inflate `rep_u` or shift.
    """
    n = len(parts)
    if n == 0:
        img = Image.new("RGBA", (atlas_size, atlas_size), (185, 185, 185, 255))
        return img, []

    if n == 1:
        p0 = parts[0]
        im = p0.get("tex_img")
        if im is None:
            rgb = [int(max(0, min(255, round(c * 255)))) for c in p0.get("avg_rgb", (0.75, 0.75, 0.75))]
            im = Image.new("RGBA", (atlas_size, atlas_size), (rgb[0], rgb[1], rgb[2], 255))
        else:
            im = im.convert("RGBA").resize((atlas_size, atlas_size), Image.Resampling.LANCZOS)
        return im, [np.asarray(p0["uvs"], dtype=np.float32).copy()]

    cols = 2 if n <= 4 else (3 if n <= 9 else 4)
    rows = int(math.ceil(n / cols))
    tile_w = atlas_size // cols
    tile_h = atlas_size // rows

    # Identify primary opaque backing image/color for compositing behind any joined decal slots when preserve_alpha=False
    primary_opaque_im = None
    primary_opaque_rgb = (180, 180, 180, 255)
    for p_cand in parts:
        if not bool(p_cand.get("is_decal", False)) and not bool(p_cand.get("is_cutout", False)):
            if p_cand.get("tex_img") is not None:
                primary_opaque_im = p_cand["tex_img"].convert("RGBA")
                c_rgb = [int(max(0, min(255, round(c * 255)))) for c in p_cand.get("avg_rgb", (0.72, 0.72, 0.72))]
                primary_opaque_rgb = (c_rgb[0], c_rgb[1], c_rgb[2], 255)
                break
            c_rgb = [int(max(0, min(255, round(c * 255)))) for c in p_cand.get("avg_rgb", (0.72, 0.72, 0.72))]
            primary_opaque_rgb = (c_rgb[0], c_rgb[1], c_rgb[2], 255)

    atlas = Image.new("RGBA", (atlas_size, atlas_size), primary_opaque_rgb)
    remapped_uvs = []
    pad = max(2, atlas_size // 512)  # Scaled gutter (2px at 1K, 4px at 2K, 8px at 4K) to prevent mip bleeding

    for idx, p in enumerate(parts):
        col = idx % cols
        row = idx // cols
        x0 = col * tile_w
        y0 = row * tile_h

        uv = np.asarray(p["uvs"], dtype=np.float32).copy()
        if len(uv) > 0:
            u_min_raw, v_min_raw = float(uv[:, 0].min()), float(uv[:, 1].min())
            u_max_raw, v_max_raw = float(uv[:, 0].max()), float(uv[:, 1].max())
        else:
            u_min_raw = v_min_raw = 0.0
            u_max_raw = v_max_raw = 1.0

        is_decal = bool(p.get("is_decal", False))
        is_cutout = bool(p.get("is_cutout", False)) or is_decal
        im = p.get("tex_img")
        backing_im = p.get("backing_img")
        if backing_im is None:
            backing_im = primary_opaque_im

        if is_decal:
            # Joined Decal Overlay Slot: NEVER tile (`np.tile`)!
            if preserve_alpha:
                bg_tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
            elif backing_im is not None:
                bg_tile = backing_im.convert("RGBA").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                bg_arr = np.asarray(bg_tile, dtype=np.uint8).copy()
                bg_arr[:, :, 3] = 255
                bg_tile = Image.fromarray(bg_arr, mode="RGBA")
            else:
                bg_tile = Image.new("RGBA", (tile_w, tile_h), primary_opaque_rgb)

            if u_min_raw >= -0.02 and u_max_raw <= 1.02 and v_min_raw >= -0.02 and v_max_raw <= 1.02:
                u_01 = np.clip(uv[:, 0], 0.0, 1.0)
                v_01 = np.clip(uv[:, 1], 0.0, 1.0)
                if im is not None:
                    fg_tile = im.convert("RGBA").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                    tile_im = fg_tile if preserve_alpha else Image.alpha_composite(bg_tile, fg_tile)
                else:
                    tile_im = bg_tile
            else:
                # UVs extend outside [0..1] (CLIP border padding around the decal on the floating quad)
                u_lo = min(0.0, u_min_raw)
                u_hi = max(1.0, u_max_raw)
                v_lo = min(0.0, v_min_raw)
                v_hi = max(1.0, v_max_raw)
                span_u = max(1e-5, u_hi - u_lo)
                span_v = max(1e-5, v_hi - v_lo)
                u_01 = np.clip((uv[:, 0] - u_lo) / span_u, 0.0, 1.0)
                v_01 = np.clip((uv[:, 1] - v_lo) / span_v, 0.0, 1.0)
                tile_im = bg_tile.copy()
                if im is not None:
                    dx0 = int(round(((0.0 - u_lo) / span_u) * tile_w))
                    dx1 = int(round(((1.0 - u_lo) / span_u) * tile_w))
                    dy0 = int(round(((v_hi - 1.0) / span_v) * tile_h))
                    dy1 = int(round(((v_hi - 0.0) / span_v) * tile_h))
                    sub_w = max(1, dx1 - dx0)
                    sub_h = max(1, dy1 - dy0)
                    fg_sub = im.convert("RGBA").resize((sub_w, sub_h), Image.Resampling.LANCZOS)
                    overlay_layer = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
                    overlay_layer.paste(fg_sub, (dx0, dy0))
                    tile_im = overlay_layer if preserve_alpha else Image.alpha_composite(tile_im, overlay_layer)

            if not preserve_alpha:
                # Guarantee 100% opaque alpha in the composited quadrant when not using 0x273 cutout shader
                t_arr = np.asarray(tile_im, dtype=np.uint8).copy()
                t_arr[:, :, 3] = 255
                tile_im = Image.fromarray(t_arr, mode="RGBA")
        else:
            # Opaque or Tiled Cutout Submesh Slot
            span_u = max(1e-5, u_max_raw - u_min_raw)
            span_v = max(1e-5, v_max_raw - v_min_raw)
            is_standard_01 = (u_min_raw >= -0.15 and u_max_raw <= 1.15 and v_min_raw >= -0.15 and v_max_raw <= 1.15)

            if is_standard_01:
                rep_u, rep_v = 1, 1
                u_01 = np.clip(uv[:, 0], 0.0, 1.0)
                v_01 = np.clip(uv[:, 1], 0.0, 1.0)
                phase_u = 0.0
                phase_v = 0.0
            else:
                rep_u = 1 if span_u <= 1.15 else max(1, min(8, int(math.ceil(span_u - 0.05))))
                rep_v = 1 if span_v <= 1.15 else max(1, min(8, int(math.ceil(span_v - 0.05))))
                if rep_u == 1 and rep_v == 1:
                    # Non-tiled UV islands (e.g. UDIM tile [1..2] or slightly outside [0..1]): wrap into [0..1] without shifting relative island positions
                    u_01 = np.clip(np.mod(uv[:, 0], 1.0), 0.0, 1.0)
                    v_01 = np.clip(np.mod(uv[:, 1], 1.0), 0.0, 1.0)
                    phase_u = 0.0
                    phase_v = 0.0
                else:
                    u_rel = uv[:, 0] - u_min_raw
                    v_rel = uv[:, 1] - v_min_raw
                    denom_u = float(rep_u) if span_u > 1.15 else max(1.0, span_u)
                    denom_v = float(rep_v) if span_v > 1.15 else max(1.0, span_v)
                    u_01 = np.clip(u_rel / denom_u, 0.0, 1.0)
                    v_01 = np.clip(v_rel / denom_v, 0.0, 1.0)
                    phase_u = u_min_raw % 1.0
                    phase_v = (1.0 - (v_max_raw % 1.0)) % 1.0

            if im is None:
                rgb = [int(max(0, min(255, round(c * 255)))) for c in p.get("avg_rgb", (0.75, 0.75, 0.75))]
                tile_im = Image.new("RGBA", (tile_w, tile_h), (rgb[0], rgb[1], rgb[2], 255))
            else:
                rgba_im = im.convert("RGBA")
                if abs(phase_u) > 1e-3 or abs(phase_v) > 1e-3:
                    arr_roll = np.asarray(rgba_im, dtype=np.uint8)
                    sx = -int(round(phase_u * arr_roll.shape[1]))
                    sy = -int(round(phase_v * arr_roll.shape[0]))
                    arr_roll = np.roll(arr_roll, shift=(sy, sx), axis=(0, 1))
                    rgba_im = Image.fromarray(arr_roll, mode="RGBA")
                if rep_u > 1 or rep_v > 1:
                    base_step_w = max(64, tile_w // rep_u)
                    base_step_h = max(64, tile_h // rep_v)
                    sub_tile = rgba_im.resize((base_step_w, base_step_h), Image.Resampling.LANCZOS)
                    tiled_arr = np.tile(np.asarray(sub_tile, dtype=np.uint8), (rep_v, rep_u, 1))
                    tile_im = Image.fromarray(tiled_arr, mode="RGBA").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                else:
                    tile_im = rgba_im.resize((tile_w, tile_h), Image.Resampling.LANCZOS)

        if preserve_alpha and is_cutout:
            # Dilate foreground RGB outward into transparent pixels inside this quadrant so mipmaps never bleed dark borders
            t_arr = np.asarray(tile_im, dtype=np.uint8).copy()
            t_alpha = t_arr[:, :, 3]
            fg_m = t_alpha >= 16
            fg_f = float(fg_m.mean())
            if 0.001 < fg_f < 0.995:
                valid_m = fg_m.copy()
                for _ in range(6):
                    inv_v = ~valid_m
                    if not np.any(inv_v):
                        break
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                        sh_v = np.roll(np.roll(valid_m, dy, axis=0), dx, axis=1)
                        sh_rgb = np.roll(np.roll(t_arr[:, :, :3], dy, axis=0), dx, axis=1)
                        fill = inv_v & sh_v
                        if np.any(fill):
                            t_arr[:, :, :3][fill] = sh_rgb[fill]
                            valid_m[fill] = True
                            inv_v = ~valid_m
                tile_im = Image.fromarray(t_arr, mode="RGBA")

        # Paste quadrant directly into atlas (preserving alpha channel when preserve_alpha=True)
        atlas.paste(tile_im, (x0, y0))

        u_min = (x0 + pad) / float(atlas_size)
        u_max = (x0 + tile_w - pad) / float(atlas_size)
        v_top_fb = (y0 + pad) / float(atlas_size)
        v_bot_fb = (y0 + tile_h - pad) / float(atlas_size)
        gltf_v_min = 1.0 - v_bot_fb
        gltf_v_max = 1.0 - v_top_fb

        uv_new = np.zeros_like(uv)
        uv_new[:, 0] = u_min + u_01 * (u_max - u_min)
        uv_new[:, 1] = gltf_v_min + v_01 * (gltf_v_max - gltf_v_min)
        remapped_uvs.append(uv_new)

    return atlas, remapped_uvs


def load_mesh_geometry(glb_path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    verts, normals, uvs, faces, _ = load_mesh_geometry_and_submeshes(glb_path)
    return verts, normals, uvs, faces


def decompose_mesh_into_hulls(tm: trimesh.Trimesh, max_hulls: int = 8) -> list[trimesh.Trimesh]:
    """Splits a mesh into 1..max_hulls convex hulls for SIMPLE_HULLS / CONVEX_HULL collision mode."""
    max_h = max(1, min(12, int(max_hulls or 8)))
    if max_h == 1 or len(tm.faces) <= 32:
        return [simplify_collision_mesh_for_havok(tm, max_faces=48, force_convex=True)]

    try:
        parts = tm.split(only_watertight=False)
        valid_parts = [p for p in parts if len(p.faces) >= 4 and len(p.vertices) >= 4]
        if len(valid_parts) > 1:
            valid_parts.sort(key=lambda m: len(m.faces), reverse=True)
            selected = valid_parts[:max_h]
            return [simplify_collision_mesh_for_havok(p, max_faces=48, force_convex=True) for p in selected]
    except Exception:
        pass

    # Spatial slicing along dominant axes into up to max_h convex hulls
    v = np.asarray(tm.vertices, dtype=np.float32)
    f = np.asarray(tm.faces, dtype=np.int32)
    tri_centers = v[f].mean(axis=1)
    spans = v.max(axis=0) - v.min(axis=0)
    dom_axis = int(np.argmax(spans))

    n_slices = min(max_h, max(2, min(6, len(f) // 24 + 1)))
    quantiles = np.linspace(0.0, 1.0, n_slices + 1)
    bounds = np.quantile(tri_centers[:, dom_axis], quantiles)

    hulls = []
    for i in range(n_slices):
        lo, hi = bounds[i], bounds[i + 1]
        if i == n_slices - 1:
            mask = (tri_centers[:, dom_axis] >= lo) & (tri_centers[:, dom_axis] <= hi)
        else:
            mask = (tri_centers[:, dom_axis] >= lo) & (tri_centers[:, dom_axis] < hi)
        sub_f = f[mask]
        if len(sub_f) >= 4:
            sub_v = v[np.unique(sub_f)]
            if len(sub_v) >= 4:
                try:
                    ch = trimesh.points.PointCloud(sub_v).convex_hull
                    if len(ch.faces) >= 4:
                        hulls.append(simplify_collision_mesh_for_havok(ch, max_faces=48, force_convex=True))
                except Exception:
                    pass

    if not hulls:
        hulls = [simplify_collision_mesh_for_havok(tm, max_faces=48, force_convex=True)]
    return hulls[:max_h]


def _quat_to_mat3_yup(quat_wxyz: list[float]) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """Converts Blender local quaternion (w, x, y, z) in Z-up space to Frostbite Y-up 3x3 basis vectors (right, up, forward)."""
    if not quat_wxyz or len(quat_wxyz) != 4:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = [float(c) for c in quat_wxyz]
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-6:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = w / n, x / n, y / n, z / n

    # 3x3 rotation matrix in Blender Z-up
    r00 = 1.0 - 2.0 * (y * y + z * z)
    r01 = 2.0 * (x * y - z * w)
    r02 = 2.0 * (x * z + y * w)
    r10 = 2.0 * (x * y + z * w)
    r11 = 1.0 - 2.0 * (x * x + z * z)
    r12 = 2.0 * (y * z - x * w)
    r20 = 2.0 * (x * z - y * w)
    r21 = 2.0 * (y * z + x * w)
    r22 = 1.0 - 2.0 * (x * x + y * y)

    # Blender SPOT light shines along local -Z_bl = (-r02, -r12, -r22).
    # Frostbite PbrSpotLightEntityData shines along -forward (offset 0x39C).
    # With coordinate change C: (x_fb, y_fb, z_fb) = (x_bl, z_bl, -y_bl),
    # map C * Z_bl -> forward, -C * Y_bl -> right, -C * X_bl -> up (right-handed, det = +1):
    right = (-r01, -r21, r11)
    up = (-r00, -r20, r10)
    forward = (r02, r22, -r12)
    return (right, up, forward)


def build_custom_light_effect_ebx(
    prop_key: str,
    lights: list[dict],
    noble_effect_raw: bytes,
    seed_prefix: str | None = None,
    use_coexist_ids: bool = True,
) -> tuple[bytes, bytes, bytes, str]:
    """
    Synthesizes a standalone per-prop EffectBlueprint EBX from EffectBlueprint struct
    dynamically emitting 1..16 PbrSpotLightEntityData / PbrSphereLightEntityData instances
    and matching LightEffectEntityData instances with exact user light transforms, colors,
    cone angles, radii, intensities, and shadow settings (zero fake fill lights).
    Returns (patched_ebx_bytes, ebp_fg, ebp_ig, ebp_name).
    """
    if "ebp_chr_" in prop_key.lower():
        raise RuntimeError(
            f"Forbidden Effect Blueprint Name: '{prop_key}' contains 'ebp_chr_' "
            f"(character/costume/impact effects crash the game on prop rotate)!"
        )
    ebp_internal = clamp_slug(f"Effects/CustomProps/EBP_{prop_key}", 43)
    ebp_name = ebp_internal.lower()

    # Use a deterministic prefix > 0xc359 (e.g. 0xe842) so sorted rank in b_ebxs is 50 (after _physics at 0xc359),
    # keeping all 8 internal Bundle 39 EBX indices (0, 25, 27, 28, 29, 31, 32, 33) at their exact vanilla indices!
    if use_coexist_ids and seed_prefix:
        ebp_fg = coexist_uuid5_bytes(f"{seed_prefix}/ebp_fg", prefix_2b=b"\xe8\x42")
        ebp_ig = coexist_uuid5_bytes(f"{seed_prefix}/ebp_ig", prefix_2b=b"\x77\xdf")
    else:
        ebp_fg = b"\xe8\x42" + deterministic_guid_bytes(f"ebp_fg:{prop_key}")[2:]
        ebp_ig = b"\x77\xdf" + deterministic_guid_bytes(f"ebp_ig:{prop_key}")[2:]

    form, chunks = demux_riff(noble_effect_raw)
    cmap = dict(chunks)
    ebxd_orig = bytes(cmap[b"EBXD"])
    efix_orig = bytes(cmap[b"EFIX"])
    ebxx_orig = bytes(cmap[b"EBXX"])
    pl_orig = ebxd_orig[12:]

    # Extract canonical instance templates from EffectBlueprint struct payload
    hdr_16 = pl_orig[0x00:0x10]
    inst0_tpl = pl_orig[0x10:0x90]    # 0x80 bytes: EffectBlueprint
    spot_tpl = pl_orig[0x90:0x200]    # 0x170 bytes: PbrSpotLightEntityData
    sphere_tpl = pl_orig[0x200:0x350] # 0x150 bytes: PbrSphereLightEntityData
    le_tpl = pl_orig[0x350:0x450]     # 0x100 bytes: LightEffectEntityData
    eff_tpl = pl_orig[0x550:0x5F0]    # 0xA0 bytes: EffectEntityData
    str_tpl = pl_orig[0x630:]         # 0x2C bytes: String table ('Effects/CustomProps/LightTemplate_EffectEBP\x00')

    lights_list = list(lights[:16]) if lights else [{
        "type": "SPOT",
        "pos": [0.0, 0.0, 1.68],
        "rot": [1.0, 0.0, 0.0, 0.0],
        "color": [1.0, 0.85, 0.70],
        "intensity": 51.83,
        "radius": 30.0,
        "inner_cone": 24.0,
        "outer_cone": 50.0,
        "cast_shadows": False,
    }]
    n_lights = len(lights_list)

    new_pl = bytearray(hdr_16)
    new_pl[0x00:0x10] = ebp_ig  # Exported Root Instance GUID lives at pl[0x00:0x10]
    inst_offsets = []

    # 1. Inst[0]: EffectBlueprint at pl+0x10 (blk[0x00:0x08] is uint64 type_index = 0)
    inst0_blk = bytearray(inst0_tpl)
    inst_offsets.append(len(new_pl))
    new_pl.extend(inst0_blk)

    # 2. Emit N light data instances (PbrSpotLightEntityData type_index=1 or PbrSphereLightEntityData type_index=2)
    light_data_offsets = []
    for i, l_spec in enumerate(lights_list):
        if any("ebp_chr_" in str(v).lower() for v in l_spec.values() if isinstance(v, str)):
            raise RuntimeError(
                f"Forbidden Effect Blueprint in light[{i}]: references 'ebp_chr_*' "
                f"(character/costume/impact effects crash the game on prop rotate)!"
            )
        l_off = len(new_pl)
        light_data_offsets.append(l_off)
        inst_offsets.append(l_off)

        l_type = str(l_spec.get("type", "SPOT")).upper()
        color = l_spec.get("color", [1.0, 0.85, 0.70])
        r_c = float(color[0]) if len(color) > 0 else 1.0
        g_c = float(color[1]) if len(color) > 1 else 0.85
        b_c = float(color[2]) if len(color) > 2 else 0.70

        if l_type == "POINT":
            blk = bytearray(sphere_tpl)
            raw_int = float(l_spec.get("intensity", 50.0))
            sphere_intensity = max(1.0, raw_int * 35.0)
            radius = max(0.5, float(l_spec.get("radius", 12.0)))
            struct.pack_into("<3f", blk, 0x80, r_c, g_c, b_c)
            struct.pack_into("<f", blk, 0xD8, sphere_intensity)
            struct.pack_into("<f", blk, 0xF8, radius)
            new_pl.extend(blk)
        else:
            blk = bytearray(spot_tpl)
            intensity = max(0.5, float(l_spec.get("intensity", 51.83)))
            radius = max(0.5, float(l_spec.get("radius", 30.0)))
            outer_raw = float(l_spec.get("outer_cone", 50.0))
            inner_raw = float(l_spec.get("inner_cone", 24.0))
            outer_deg = math.degrees(outer_raw) if outer_raw <= math.pi + 1e-3 else outer_raw
            inner_deg = math.degrees(inner_raw) if inner_raw <= math.pi + 1e-3 else inner_raw
            outer_deg = max(5.0, min(89.5, outer_deg))
            inner_deg = max(1.0, min(outer_deg, inner_deg))
            cast_sh = 1 if l_spec.get("cast_shadows", False) else 0

            struct.pack_into("<3f", blk, 0x90, r_c, g_c, b_c)
            struct.pack_into("<f", blk, 0xB4, outer_deg)
            struct.pack_into("<f", blk, 0xCC, radius)
            struct.pack_into("<I", blk, 0xD0, cast_sh)
            struct.pack_into("<f", blk, 0xD8, inner_deg)
            struct.pack_into("<f", blk, 0x114, intensity)
            new_pl.extend(blk)

    # 3. Emit N LightEffectEntityData instances (0x100 bytes each, type_index=3)
    le_offsets = []
    for i, l_spec in enumerate(lights_list):
        le_off = len(new_pl)
        le_offsets.append(le_off)
        inst_offsets.append(le_off)

        le_blk = bytearray(le_tpl)

        pos_bl = l_spec.get("pos", [0.0, 0.0, 1.68])
        px = float(pos_bl[0]) if len(pos_bl) > 0 else 0.0
        py_bl = float(pos_bl[1]) if len(pos_bl) > 1 else 0.0
        pz_bl = float(pos_bl[2]) if len(pos_bl) > 2 else 1.68
        # Blender Z-up (x, y, z) -> Frostbite Y-up (x, z, -y)
        trans_fb = (px, pz_bl, -py_bl)
        right, up, forward = _quat_to_mat3_yup(l_spec.get("rot", [1.0, 0.0, 0.0, 0.0]))
        struct.pack_into("<4f", le_blk, 0x20, right[0], right[1], right[2], 0.0)
        struct.pack_into("<4f", le_blk, 0x30, up[0], up[1], up[2], 0.0)
        struct.pack_into("<4f", le_blk, 0x40, forward[0], forward[1], forward[2], 0.0)
        struct.pack_into("<4f", le_blk, 0x50, trans_fb[0], trans_fb[1], trans_fb[2], 0.0)

        # Internal pointer at le_off + 0xB0 -> light_data_offsets[i]
        struct.pack_into("<q", le_blk, 0xB0, light_data_offsets[i] - (le_off + 0xB0))
        new_pl.extend(le_blk)

    # 4. Emit EffectEntityData (0xA0 bytes, type_index=4)
    eff_off = len(new_pl)
    inst_offsets.append(eff_off)
    eff_blk = bytearray(eff_tpl)
    new_pl.extend(eff_blk)

    # 5. Emit arrays section at arrays_off
    arrays_off = len(new_pl)
    arr0_off = arrays_off + 0x10
    new_pl.extend(b"\x00" * 0x24)  # 36 zero bytes before Array[1] count prefix (arr0_off at +0x10)
    new_pl.extend(struct.pack("<I", n_lights))  # 4-byte count prefix right before arr1_off (+0x24)
    arr1_off = len(new_pl)  # arrays_off + 0x28
    for i in range(n_lights):
        elem_off = len(new_pl)
        new_pl.extend(struct.pack("<q", le_offsets[i] - elem_off))
    while len(new_pl) % 16 != 0:
        new_pl.append(0)

    # 6. Emit strings section at strings_off
    strings_off = len(new_pl)
    boxed_off = strings_off
    new_pl.extend(str_tpl)

    # Patch internal pointers in Inst[0] and EffectEntityData
    struct.pack_into("<q", new_pl, 0x28, strings_off - 0x28)
    for p_off in (0x30, 0x38, 0x50, 0x68):
        struct.pack_into("<q", new_pl, p_off, arr0_off - p_off)
    struct.pack_into("<q", new_pl, 0x78, eff_off - 0x78)
    struct.pack_into("<q", new_pl, eff_off + 0x60, arr1_off - (eff_off + 0x60))

    new_ebxd = bytearray(ebxd_orig[:12]) + new_pl
    bsp.patch_ebx_string_inplace(new_ebxd, 12 + strings_off, 43, ebp_internal)

    # 7. Rebuild EFIX chunk
    ptr_offsets = (
        [0x28, 0x30, 0x38, 0x50, 0x68, 0x78]
        + [le_off + 0xB0 for le_off in le_offsets]
        + [eff_off + 0x60]
        + [arr1_off + i * 8 for i in range(n_lights)]
    )

    new_efix = bytearray()
    new_efix.extend(ebp_fg)
    new_efix.extend(efix_orig[16:128])  # n_types=5, type_guids, n_sig=5, sigs, n_exp=1
    new_efix.extend(struct.pack(f"<I{len(inst_offsets)}I", len(inst_offsets), *inst_offsets))
    new_efix.extend(struct.pack(f"<I{len(ptr_offsets)}I", len(ptr_offsets), *ptr_offsets))
    new_efix.extend(struct.pack("<4I", 0, 0, 0, 0))  # n_res=0, n_imp=0, n_imp_ptrs=0, n_typeinfo=0
    new_efix.extend(struct.pack("<4I", arrays_off, boxed_off, strings_off, 0))

    # 8. Rebuild EBXX chunk (contains 1 entry at offset 8 for the Components pointer array at arr1_off)
    new_ebxx = bytearray(ebxx_orig)
    struct.pack_into("<Ii", new_ebxx, 8, arr1_off, n_lights)

    patched_raw = remux_riff(
        form,
        [
            (
                cid,
                new_ebxd if cid == b"EBXD" else (new_efix if cid == b"EFIX" else (new_ebxx if cid == b"EBXX" else pl)),
            )
            for cid, pl in chunks
        ],
    )
    return patched_raw, ebp_fg, ebp_ig, ebp_name


def build_custom_vfx_effect_ebx(
    prop_key: str,
    vfx_emitters: list[dict],
    music_emitter: dict | None,
    prop_lights: list[dict] | None = None,
    seed_prefix: str = "",
    use_coexist_ids: bool = False,
) -> tuple[bytes, bytes, bytes, str]:
    """
    Synthesizes a standalone EffectBlueprint EBX for a prop with ReSkate Lab VFX emitters
    and/or Interactive Music / Boombox emitter using Bundle[16]'s verified EffectBlueprint
    templates (`EBX[24]` `ebp_spinning_goose_firework` or `EBX[21]` `eb_act_firework_sparkler_fuse`).
    """
    b16_cache = bsp._load_b16_vfx_bundle_assets()
    if b16_cache is None or not b16_cache.get("ebx_records"):
        raise RuntimeError("Could not load Bundle[16] VFX template assets from vanilla buildkititems.toc")

    b16_by_name = {rec["name"].lower(): rec["raw"] for rec in b16_cache["ebx_records"]}
    use_sparkler_only = (
        len(vfx_emitters) == 1
        and str(vfx_emitters[0].get("preset", "")).upper() == "SPARKLER_FUSE"
        and not (music_emitter and music_emitter.get("enabled"))
        and not prop_lights
    )
    if use_sparkler_only and "effects/season02/effectblueprints/eb_act_firework_sparkler_fuse" in b16_by_name:
        tpl_raw = b16_by_name["effects/season02/effectblueprints/eb_act_firework_sparkler_fuse"]
        str_pl_off, str_max_len = 20940, 63
        vfx_comp_offs = [0xA0, 0x300, 0x560, 0x7C0, 0xA10]
        light_comp_offs = []
        sphere_light_offs = []
    else:
        tpl_raw = b16_by_name["effects/season02/effectblueprints/ebp_spinning_goose_firework"]
        str_pl_off, str_max_len = 21692, 61
        vfx_comp_offs = [0xA0, 0x300, 0x560, 0x7C0, 0xA10]
        light_comp_offs = [0xB10, 0xEB0]
        sphere_light_offs = [0xC10, 0xD60]

    ebp_name = f"effects/{prop_key}_fx"
    ebp_internal = f"effects/{prop_key}_fx"
    if use_coexist_ids and seed_prefix:
        ebp_fg = coexist_uuid5_bytes(f"{seed_prefix}/vfx_ebp_fg")
        ebp_ig = coexist_uuid5_bytes(f"{seed_prefix}/vfx_ebp_ig")
    else:
        ebp_fg = deterministic_guid_bytes(f"vfx_ebp_fg:{prop_key}")
        ebp_ig = deterministic_guid_bytes(f"vfx_ebp_ig:{prop_key}")

    form, chunks = demux_riff(tpl_raw)
    cmap = dict(chunks)
    ebxd = bytearray(cmap[b"EBXD"])
    efix = bytearray(cmap[b"EFIX"])

    efix[0:16] = ebp_fg
    ebxd[12:28] = ebp_ig
    bsp.patch_ebx_string_inplace(ebxd, 12 + str_pl_off, str_max_len, ebp_internal)

    # Determine emitter offsets in Frostbite Y-up coordinates: Blender (x, y, z) -> Frostbite (x, z, -y)
    emitter_positions_fb: list[tuple[float, float, float, float]] = []
    for em in (vfx_emitters or []):
        if isinstance(em, dict):
            off = em.get("offset") or [0.0, 0.0, 0.5]
            bx = float(off[0]) if len(off) > 0 else 0.0
            by = float(off[1]) if len(off) > 1 else 0.0
            bz = float(off[2]) if len(off) > 2 else 0.5
            sc = max(0.1, min(5.0, float(em.get("scale", 1.0) or 1.0)))
            emitter_positions_fb.append((bx, bz, -by, sc))
    if music_emitter and isinstance(music_emitter, dict) and music_emitter.get("enabled"):
        m_off = music_emitter.get("offset") or [0.0, 0.0, 0.45]
        bx = float(m_off[0]) if len(m_off) > 0 else 0.0
        by = float(m_off[1]) if len(m_off) > 1 else 0.0
        bz = float(m_off[2]) if len(m_off) > 2 else 0.45
        emitter_positions_fb.append((bx, bz, -by, 1.0))
    if not emitter_positions_fb:
        emitter_positions_fb.append((0.0, 0.5, 0.0, 1.0))

    for idx_c, pl_off in enumerate(vfx_comp_offs):
        fx, fy, fz, sc = emitter_positions_fb[idx_c % len(emitter_positions_fb)]
        eb_off = 12 + pl_off
        if eb_off + 0x60 <= len(ebxd):
            # Scale 3x3 basis vectors slightly if scale != 1.0 and set translation at +0x50
            if abs(sc - 1.0) > 1e-3:
                for row_off in (0x20, 0x30, 0x40):
                    rx, ry, rz, rw = struct.unpack_from("<4f", ebxd, eb_off + row_off)
                    struct.pack_into("<4f", ebxd, eb_off + row_off, rx * sc, ry * sc, rz * sc, rw)
            struct.pack_into("<4f", ebxd, eb_off + 0x50, fx, fy, fz, 0.0)

    # Tune the 2 embedded PbrSphereLightEntityData instances in EBX[24]
    if light_comp_offs and sphere_light_offs:
        lights_src = list(prop_lights or [])
        preset_upper = str((vfx_emitters[0].get("preset") if vfx_emitters else "") or "").upper()
        if "FIRE" in preset_upper or "SPARK" in preset_upper:
            default_cols = [(1.0, 0.48, 0.14), (1.0, 0.72, 0.25)]
        elif "SMOKE" in preset_upper:
            default_cols = [(0.55, 0.70, 0.95), (0.45, 0.60, 0.85)]
        elif music_emitter and music_emitter.get("enabled") and not vfx_emitters:
            default_cols = [(0.20, 0.88, 1.0), (0.95, 0.30, 0.85)]
        else:
            default_cols = [(1.0, 0.65, 0.22), (0.30, 0.85, 1.0)]

        for l_i, (le_pl_off, sp_pl_off) in enumerate(zip(light_comp_offs, sphere_light_offs)):
            le_eb_off = 12 + le_pl_off
            sp_eb_off = 12 + sp_pl_off
            if l_i < len(lights_src):
                ls = lights_src[l_i]
                lpos = ls.get("pos", [0.0, 0.0, 1.2])
                lx = float(lpos[0]) if len(lpos) > 0 else 0.0
                ly_bl = float(lpos[1]) if len(lpos) > 1 else 0.0
                lz_bl = float(lpos[2]) if len(lpos) > 2 else 1.2
                lcol = ls.get("color", [1.0, 0.85, 0.70])
                rc, gc, bc = float(lcol[0]), float(lcol[1]), float(lcol[2])
                lint = max(50.0, float(ls.get("intensity", 50.0)) * 35.0)
                lrad = max(2.0, float(ls.get("radius", 12.0)))
                struct.pack_into("<4f", ebxd, le_eb_off + 0x50, lx, lz_bl, -ly_bl, 0.0)
            else:
                fx, fy, fz, _ = emitter_positions_fb[l_i % len(emitter_positions_fb)]
                rc, gc, bc = default_cols[l_i % len(default_cols)]
                lint = 1400.0
                lrad = float((music_emitter or {}).get("radius", 8.0)) if (music_emitter and music_emitter.get("enabled")) else 7.5
                lrad = max(2.5, min(25.0, lrad))
                struct.pack_into("<4f", ebxd, le_eb_off + 0x50, fx, fy + 0.15, fz, 0.0)
            struct.pack_into("<3f", ebxd, sp_eb_off + 0x80, rc, gc, bc)
            struct.pack_into("<f", ebxd, sp_eb_off + 0xD8, lint)
            struct.pack_into("<f", ebxd, sp_eb_off + 0xF8, lrad)

    patched_raw = remux_riff(
        form,
        [(cid, ebxd if cid == b"EBXD" else (efix if cid == b"EFIX" else pl)) for cid, pl in chunks],
    )
    return patched_raw, ebp_fg, ebp_ig, ebp_name


def build_lit_nopolicy_ebx(
    prop_key: str,
    nopol_fg: bytes,
    nopol_ig: bytes,
    standalone_nopol_internal: str,
    ebp_fg: bytes,
    ebp_ig: bytes,
    shape_packed_list: list[int],
    noble_nopol_raw: bytes,
    light_pos_blender: list[float] | None = None,
) -> bytes:
    """
    Synthesizes a prop's lit .nopolicy EBX from Noble's verified 6-instance lit .nopolicy template
    (lit .nopolicy struct, which contains FuseVfxEntityData & LightEffectEntityData):
    1. Root instance (Inst[0] @ 0x10, 2 occurrences in EBXD) -> nopol_ig (matches _ecsprefab EBXD[0x214]).
    2. Child entity node (Inst[3] @ 0x190, 4 occurrences in EBXD) -> f9b22611e3a75f20dab74ce39852e261
       (matches _ecsprefab EBXD[0x10C] so MeshComponent and CollisionComponent bind to the entity).
    3. Import 0 (EFIX[0x304:0x324], referenced at ebxd+0x3DC = 1) -> Bundle 39 EBX 33
       (7f76c7f1233e624817f906770e8b7535 / 69322c38bd52bbe0825660bbcc924c73).
    4. Import 1 (EFIX[0x324:0x344], referenced at ebxd+0x72C = 3) -> Standalone EffectBlueprint (ebp_fg + ebp_ig).
    """
    form, chunks = demux_riff(noble_nopol_raw)
    cmap = dict(chunks)
    ebxd = bytearray(cmap[b"EBXD"])
    efix = bytearray(cmap[b"EFIX"])

    orig_noble_root_ig = bytes.fromhex("6a0b8c82a892dfce1d70e57c77fdbc7a")
    orig_noble_child_ig = bytes.fromhex("85e683e80fb66f8ce23b319cc118f3b5")
    target_b39_child_ig = bytes.fromhex("f9b22611e3a75f20dab74ce39852e261")
    b39_ebx33_imp = bytes.fromhex("7f76c7f1233e624817f906770e8b753569322c38bd52bbe0825660bbcc924c73")

    # 1. Replace all occurrences of root instance GUID (2x) and child entity GUID (4x)
    ebxd = bytearray(
        bytes(ebxd)
        .replace(orig_noble_root_ig, nopol_ig)
        .replace(orig_noble_child_ig, target_b39_child_ig)
    )

    # 2. Patch FileGuid and the 2 imports in EFIX
    efix[0:16] = nopol_fg
    efix[0x304:0x324] = b39_ebx33_imp
    efix[0x324:0x344] = ebp_fg + ebp_ig

    # 3. Patch internal name strings in EBXD (at 2238 max_len=73 and 1872 max_len=78)
    bsp.patch_ebx_string_inplace(ebxd, 2238, 73, standalone_nopol_internal)
    bsp.patch_ebx_string_inplace(ebxd, 1872, 78, standalone_nopol_internal)

    # 4. Remux RIFF and apply shape physics materials
    remuxed = remux_riff(
        form,
        [(cid, ebxd if cid == b"EBXD" else (efix if cid == b"EFIX" else pl)) for cid, pl in chunks],
    )
    return patch_prefab_ebx_materials(remuxed, shape_packed_list)


def _weld_and_sharpen_collision_for_grind_paths(
    tm: trimesh.Trimesh,
    grind_paths: list[dict] | None = None,
) -> tuple[trimesh.Trimesh, np.ndarray]:
    """
    1. Welds duplicate vertices along glTF UV/normal seams (`merge_vertices(merge_tex=True, merge_norm=True)`)
       so `face_adjacency` and Havok's edge solver see connected manifold dihedral angles (fixes _Var3!).
    2. For any marked `LEDGE` paths in `grind_paths`, levels crowned top-table vertices between left/right lips
       and sharpens side-wall vertices immediately beneath the marked lip so remeshed/crowned barriers
       (like `graffiti_ba_738256fd`) form a crisp >= 80° grindable corner along the marked path.
    3. Returns `(welded_tm, protected_vertex_mask)` so `decimate_trimesh_fast` never collapses marked grind lips.
    """
    work = tm.copy()
    try:
        work.merge_vertices(merge_tex=True, merge_norm=True)
    except Exception:
        pass

    v = np.asarray(work.vertices, dtype=np.float64).copy()
    n_verts = len(v)
    protected = np.zeros(n_verts, dtype=bool)
    if n_verts == 0 or len(work.faces) == 0:
        return work, protected

    bmin = v.min(axis=0)
    bmax = v.max(axis=0)
    span = bmax - bmin
    # Horizontal long axis in GLB Y-up is X (0) or Z (2); cross-section axis is the other horizontal axis
    long_ax = 0 if span[0] >= span[2] else 2
    cross_ax = 2 if long_ax == 0 else 0

    # Protect existing sharp top edges (>= 52° in upper 45% of prop height)
    try:
        adj_f = work.face_adjacency
        adj_e = work.face_adjacency_edges
        if len(adj_f) > 0:
            fn = np.asarray(work.face_normals, dtype=np.float64)
            dots = np.clip(np.sum(fn[adj_f[:, 0]] * fn[adj_f[:, 1]], axis=1), -1.0, 1.0)
            angs = np.degrees(np.arccos(dots))
            emid_y = 0.5 * (v[adj_e[:, 0], 1] + v[adj_e[:, 1], 1])
            top_thresh_y = bmin[1] + 0.55 * max(0.05, span[1])
            sharp_top = (angs >= 52.0) & (emid_y >= top_thresh_y)
            if np.any(sharp_top):
                sharp_v = np.unique(adj_e[sharp_top].ravel())
                protected[sharp_v] = True
    except Exception:
        pass

    if not grind_paths:
        return work, protected

    # Collect marked LEDGE and ROUND_RAIL/COPING segment points in GLB Y-up (x, z, -y)
    ledge_pts_list = []
    all_path_pts_list = []
    for gp in grind_paths:
        if not isinstance(gp, dict) or not gp.get("enabled", True):
            continue
        raw_segs = gp.get("segments") or []
        if not raw_segs:
            continue
        is_z_up = bool(gp.get("z_up", True))
        g_type = str(gp.get("type", "LEDGE")).upper()
        pts = []
        for seg in raw_segs:
            if len(seg) >= 2:
                for p in (seg[0], seg[1]):
                    if is_z_up:
                        pts.append([float(p[0]), float(p[2]), -float(p[1])])
                    else:
                        pts.append([float(p[0]), float(p[1]), float(p[2])])
        if pts:
            pts_arr = np.unique(np.round(np.asarray(pts, dtype=np.float64), 5), axis=0)
            all_path_pts_list.append(pts_arr)
            if g_type == "LEDGE" and str(gp.get("surface_id", "")) != "No_Grind":
                ledge_pts_list.append(pts_arr)

    if all_path_pts_list:
        all_pts = np.vstack(all_path_pts_list)
        dists_all = np.linalg.norm(v[:, None, :] - all_pts[None, :, :], axis=2).min(axis=1)
        protected[dists_all <= 0.055] = True

    if ledge_pts_list:
        ledge_pts = np.vstack(ledge_pts_list)
        dists_l = np.linalg.norm(v[:, None, :] - ledge_pts[None, :, :], axis=2).min(axis=1)
        lip_mask = dists_l <= 0.048
        if np.any(lip_mask):
            protected[lip_mask] = True
            # Bin along long_ax to level any crowned top-table between marked lips and sharpen the upper side-wall under the lip
            n_bins = 24
            l_min, l_max = float(bmin[long_ax]), float(bmax[long_ax])
            bin_edges = np.linspace(l_min - 1e-4, l_max + 1e-4, n_bins + 1)
            lip_indices = np.where(lip_mask)[0]
            for bi in range(n_bins):
                bx0, bx1 = bin_edges[bi], bin_edges[bi + 1]
                in_bin_lips = lip_indices[(v[lip_indices, long_ax] >= bx0) & (v[lip_indices, long_ax] <= bx1)]
                if len(in_bin_lips) == 0:
                    expanded = lip_indices[(v[lip_indices, long_ax] >= bx0 - 0.25) & (v[lip_indices, long_ax] <= bx1 + 0.25)]
                    in_bin_lips = expanded
                if len(in_bin_lips) == 0:
                    continue
                y_lip = float(np.percentile(v[in_bin_lips, 1], 80.0))
                v[in_bin_lips, 1] = y_lip
                c_vals = v[in_bin_lips, cross_ax]
                c_min_lip = float(c_vals.min())
                c_max_lip = float(c_vals.max())
                c_mid = 0.5 * (c_min_lip + c_max_lip)

                bin_verts = np.where((v[:, long_ax] >= bx0) & (v[:, long_ax] <= bx1))[0]
                for vi in bin_verts:
                    if lip_mask[vi]:
                        continue
                    vy = float(v[vi, 1])
                    vc = float(v[vi, cross_ax])
                    # 1. Crowned top-table vertices sitting between left & right marked lips
                    if vy >= y_lip - 0.035 and (c_min_lip - 0.015) <= vc <= (c_max_lip + 0.015):
                        v[vi, 1] = y_lip
                        protected[vi] = True
                    # 2. Upper side-wall vertices 1.5cm..14cm below the marked lip -> pull 50% toward lip cross-coord for crisp >= 82° corner
                    elif (y_lip - 0.14) <= vy < (y_lip - 0.012):
                        if vc < c_mid and abs(vc - c_min_lip) <= 0.12:
                            v[vi, cross_ax] = vc + 0.50 * (c_min_lip - vc)
                            protected[vi] = True
                        elif vc >= c_mid and abs(vc - c_max_lip) <= 0.12:
                            v[vi, cross_ax] = vc + 0.50 * (c_max_lip - vc)
                            protected[vi] = True

            work = trimesh.Trimesh(vertices=v, faces=np.asarray(work.faces, dtype=np.int32), process=False)

    return work, protected


def decimate_trimesh_fast(
    tm: trimesh.Trimesh,
    target_faces: int,
    protected_mask: np.ndarray | None = None,
) -> trimesh.Trimesh:
    """
    Fast pure-numpy spatial vertex clustering decimation for trimesh when `fast_simplification`
    is unavailable in Blender 5.3's Python environment.
    Welds split glTF vertices first and pins `protected_mask` vertices (such as sharp top ledges
    and marked `grind_paths`) so grindable edges never collapse during decimation.
    """
    work_tm = tm.copy()
    if protected_mask is None:
        try:
            work_tm.merge_vertices(merge_tex=True, merge_norm=True)
        except Exception:
            pass
    max_verts_limit = 2600
    if len(work_tm.faces) <= target_faces and len(work_tm.vertices) <= max_verts_limit:
        return work_tm
    if protected_mask is None or not np.any(protected_mask):
        try:
            dec = work_tm.simplify_quadric_decimation(target_faces)
            if 4 <= len(dec.faces) <= target_faces and len(dec.vertices) <= max_verts_limit:
                return dec
        except Exception:
            pass
    v = np.asarray(work_tm.vertices, dtype=np.float32)
    f = np.asarray(work_tm.faces, dtype=np.int32)
    prot = (
        np.asarray(protected_mask, dtype=bool)
        if (protected_mask is not None and len(protected_mask) == len(v))
        else np.zeros(len(v), dtype=bool)
    )
    prot_idx = np.where(prot)[0]
    bmin = v.min(axis=0)
    bmax = v.max(axis=0)
    span = np.maximum(bmax - bmin, 1e-4)
    lo, hi = 4, 110
    best_tm = None
    while lo <= hi:
        mid = (lo + hi) // 2
        grid_coords = np.floor(((v - bmin) / span) * mid).astype(np.int64)
        stride = int(mid + 3)
        keys = grid_coords[:, 0] * stride * stride + grid_coords[:, 1] * stride + grid_coords[:, 2]
        if len(prot_idx) > 0 and len(prot_idx) < max_verts_limit - 64:
            base_pin = int(keys.max()) + 10
            keys[prot_idx] = base_pin + np.arange(len(prot_idx), dtype=np.int64)
        unique_keys, inv = np.unique(keys, return_inverse=True)
        new_v = np.zeros((len(unique_keys), 3), dtype=np.float32)
        np.add.at(new_v, inv, v)
        counts = np.bincount(inv).astype(np.float32)[:, None]
        new_v /= np.maximum(counts, 1.0)
        new_f = inv[f]
        valid = (new_f[:, 0] != new_f[:, 1]) & (new_f[:, 1] != new_f[:, 2]) & (new_f[:, 2] != new_f[:, 0])
        new_f = new_f[valid]
        if len(new_f) >= 4:
            sorted_f = np.sort(new_f, axis=1)
            _, uniq_idx = np.unique(sorted_f, axis=0, return_index=True)
            new_f = new_f[np.sort(uniq_idx)]
        if 4 <= len(new_f) <= target_faces and len(new_v) <= max_verts_limit:
            best_tm = trimesh.Trimesh(vertices=new_v, faces=new_f, process=False)
            lo = mid + 1
        elif len(new_f) > target_faces or len(new_v) > max_verts_limit:
            hi = mid - 1
        else:
            lo = mid + 1
    if best_tm is not None and len(best_tm.faces) >= 4:
        return best_tm
    try:
        return work_tm.convex_hull
    except Exception:
        return work_tm


def _extract_submesh_embedded_image(sm: trimesh.Trimesh) -> tuple[Image.Image | None, tuple[float, float, float]]:
    """Extracts an embedded PIL Image and average RGB from a trimesh submesh's PBRMaterial if available."""
    tex_img = None
    avg_rgb = (0.72, 0.72, 0.72)
    try:
        if hasattr(sm, "visual") and hasattr(sm.visual, "material") and sm.visual.material is not None:
            mat = sm.visual.material
            if getattr(mat, "baseColorTexture", None) is not None:
                tex_img = mat.baseColorTexture
            elif getattr(mat, "image", None) is not None:
                tex_img = mat.image
            elif getattr(mat, "baseColorFactor", None) is not None:
                bcf = list(mat.baseColorFactor)
                if len(bcf) >= 3:
                    scale = 1.0 / 255.0 if max(bcf[:3]) > 1.0 else 1.0
                    avg_rgb = (float(bcf[0]) * scale, float(bcf[1]) * scale, float(bcf[2]) * scale)
    except Exception:
        pass
    if tex_img is not None:
        try:
            rgba = tex_img.convert("RGBA")
            arr = np.asarray(rgba, dtype=np.float32) / 255.0
            avg_rgb = (float(arr[:, :, 0].mean()), float(arr[:, :, 1].mean()), float(arr[:, :, 2].mean()))
            return rgba, avg_rgb
        except Exception:
            pass
    return None, avg_rgb


# Exact 627-byte (0x273) native Quick Drop Category 0 _osk Alpha Cutout Shader Block from Bundle 58 (fightfence) / Bundle 421 (hockeynet)
CUTOUT_OSK_273_TEMPLATE_HEX = (
    "130000004534b20b6148a37930fcc5140100e89901dd7ce79d2908a5a4eebab5"
    "f7611340980ef5151685eeed11844f88467e4cd5b9cad44c3f37ab17af30fcc5"
    "140100f7d6017a90665e6865bfdc806270e225314b578662521685eeed11844f"
    "88467e4cd5b9870d7b689ad317af30fcc5140100edb501d4c1efc0a133599113"
    "a2334e73fcc232b54ba52a85eeed11844f88467e4cd5b9276f1885c0f00a0d2d"
    "5cb62b01005a4a0100000000000000d6ce1a4de0491b0ed5e9362a01006aae01"
    "0000000000803f0000803f305fb4c3098f976759ff5e8801002d550100000000"
    "00003f0000003f0000003fd094c63ab89073817e814a8e0100db290100000000"
    "0000009093c2072961e7824434ed36010001010100000001000000411933da65"
    "61e0904434ed360100c47f0100000000000000dd725c79a779549459ff5e8801"
    "00df0c010000000000003f0000003f0000003f8ec964b187a3dab22d5cb62b01"
    "008fa201000000000000000681892ee19096b559ff5e8801004ece0100000000"
    "00003f0000003f0000003f1573e4ae4a37dbb7ca70f9570200ce1e02000000fc"
    "b7b5b7fc1a1a1aaaddbc2c50fa19c198e926780100d85101000000000000003a"
    "46176bbed82dc159ff5e880200849d020000009118a33e9118a33e9118a33e00"
    "00000000000000000000008bfaedbc919d0fc22d5cb62b010032340100000000"
    "000000225715d11fd176d87e814a8e0100a3d6010000000100000009d83e18a0"
    "b4b3f659ff5e880200c9b0020000000000803f0000803f0000803f0000000000"
    "000000000000008a3a2f922c7b3dfd59ff5e880200421602000000b137373f1e"
    "58353fb137373fcdcccc3dcdcccc3dcdcccc3d"
)
DECALS_OSK_242_TEMPLATE_HEX = CUTOUT_OSK_273_TEMPLATE_HEX


def _prepare_alpha_opacity_decal_textures(
    rgba_img: Image.Image,
    msk_img: Image.Image,
    alpha_thresh: int = 16,
    msk_g_is_roughness: bool = False,
    msk_g_is_curved_smoothness: bool = False,
) -> tuple[Image.Image, Image.Image, bool]:
    """
    Proven Method C (`1C` / `2C`):
    Routes the PNG's Alpha channel into the Frostbite `0x273` `_osk` (`Opacity / Smoothness / Mask`)
    texture's Red & Alpha channels (`R = Opacity`, `G = (1 - Roughness)^2.2`, `B = 0`, `A = Opacity`)
    while keeping the mesh as a clean quad (no silhouette mesh tracing), and dilates foreground RGB colors
    into transparent borders of `_c` so bilinear mipmaps never bleed dark fringes around the cutout.
    Note: Both `0x771` and `0x273` read Green (`G`) as Smoothness (`255 = 0% Rough / Shiny`, `0 = 100% Rough / Matte`),
    and `0x273` has a steeper specular response where `G = (1 - Roughness)^2.2` matches `0x771` linear roughness.
    """
    target_sz = rgba_img.size if (rgba_img.width >= 1024 and rgba_img.width == rgba_img.height) else (1024, 1024)
    rgba = rgba_img.convert("RGBA").resize(target_sz, Image.Resampling.LANCZOS) if rgba_img.size != target_sz else rgba_img.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.uint8).copy()
    alpha = arr[:, :, 3]
    fg_mask = alpha >= alpha_thresh
    fg_frac = float(fg_mask.mean())

    # 1. Un-premultiply semi-transparent edge pixels and dilate foreground RGB outward by 6 iterations
    if 0.001 < fg_frac < 0.995:
        rgb_f = arr[:, :, :3].astype(np.float32)
        semi = (alpha > 4) & (alpha < 250)
        if np.any(semi):
            scale = 255.0 / np.maximum(alpha[semi].astype(np.float32), 16.0)
            rgb_f[semi] = np.clip(rgb_f[semi] * scale[:, None], 0.0, 255.0)
        arr[:, :, :3] = np.round(rgb_f).astype(np.uint8)

        valid_mask = alpha >= alpha_thresh
        for _ in range(6):
            inv_valid = ~valid_mask
            if not np.any(inv_valid):
                break
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                sh_valid = np.roll(np.roll(valid_mask, dy, axis=0), dx, axis=1)
                sh_rgb = np.roll(np.roll(arr[:, :, :3], dy, axis=0), dx, axis=1)
                fill = inv_valid & sh_valid
                if np.any(fill):
                    arr[:, :, :3][fill] = sh_rgb[fill]
                    valid_mask[fill] = True
                    inv_valid = ~valid_mask

    cleaned_c = Image.fromarray(arr, mode="RGBA")

    # 2. Pack Method C (`R = Opacity`, `G = (1 - Roughness)^2.2`, `B = 0`, `A = Opacity`) into `_msk` (`_osk`)
    msk_resized = msk_img.convert("RGBA").resize(target_sz, Image.Resampling.LANCZOS) if msk_img.size != target_sz else msk_img.convert("RGBA")
    msk_rgba = np.asarray(msk_resized, dtype=np.uint8).copy()
    if not msk_g_is_curved_smoothness:
        g_norm = msk_rgba[:, :, 1].astype(np.float32) / 255.0
        smooth_lin = (1.0 - g_norm) if msk_g_is_roughness else g_norm
        smooth_curved = np.power(np.clip(smooth_lin, 0.0, 1.0), 2.2)
        msk_rgba[:, :, 1] = np.clip(np.round(smooth_curved * 255.0), 0, 255).astype(np.uint8)
    msk_rgba[:, :, 0] = alpha
    msk_rgba[:, :, 2] = 0
    msk_rgba[:, :, 3] = alpha
    cleaned_osk = Image.fromarray(msk_rgba, mode="RGBA")

    print(
        f"  [Alpha Cutout / Decal Layer (Method C)] Packed 0x273 _osk with R=Opacity, G=(1-Roughness)^2.2, B=0 "
        f"({100.0 * (1.0 - fg_frac):.1f}% transparent area; clean geometry preserved).",
        flush=True,
    )
    return cleaned_c, cleaned_osk, True


def compile_split_opaque_and_cutout_mesh_into_b421(
    donor_ms_b421: bytes,
    donor_mc_ebx: bytes,
    verts: np.ndarray,
    normals: np.ndarray,
    uvs: np.ndarray,
    opaque_faces: np.ndarray,
    cutout_faces: np.ndarray,
    flip_normals: bool = False,
    vc_layers: np.ndarray | None = None,
):
    """
    Compiles a multi-material prop containing BOTH Opaque PBR slots (`Sec[0]`, Shader `0x771`, `material_index=0`)
    and Alpha Cutout / Decal slots (`Sec[1]`, Shader `0x273`, `material_index=1`) into Bundle #421 (`HockeyNet`)'s
    native 4,192-byte multi-section `MeshSet`.
    """
    from ReSkatePropForge import encode_fb_tangentspace_vec

    total_verts = len(verts)
    if total_verts > 65535:
        raise ValueError(f"Mesh vertex count ({total_verts}) exceeds Frostbite uint16 limit (65,535).")

    opaque_f = np.asarray(opaque_faces, dtype=np.int32).reshape((-1, 3))
    cutout_f = np.asarray(cutout_faces, dtype=np.int32).reshape((-1, 3))

    def _extract_submesh_vb_ib(sub_faces: np.ndarray, include_vc_ts: bool = True):
        if len(sub_faces) == 0:
            # Dummy 1-triangle degenerate section if empty
            uniq_v = np.array([0, 0, 0], dtype=np.int32)
            local_f = np.array([[0, 0, 0]], dtype=np.uint16)
        else:
            uniq_v, inv = np.unique(sub_faces.reshape(-1), return_inverse=True)
            local_f = inv.astype(np.uint16).reshape((-1, 3))
        nv = len(uniq_v)
        sv = verts[uniq_v]
        sn = normals[uniq_v] if (normals is not None and len(normals) == total_verts) else np.zeros((nv, 3), dtype=np.float32)
        suv = uvs[uniq_v] if (uvs is not None and len(uvs) == total_verts) else np.zeros((nv, 2), dtype=np.float32)

        pos_h4 = np.ones((nv, 4), dtype="<f2")
        pos_h4[:, 0:3] = sv.astype("<f2")
        pos_b = pos_h4.tobytes()

        uv_h2 = np.zeros((nv, 2), dtype="<f2")
        uv_fl = suv.copy().astype(np.float32)
        uv_fl[:, 1] = 1.0 - uv_fl[:, 1]
        uv_h2[:] = uv_fl.astype("<f2")
        uv_b = uv_h2.tobytes()

        if not include_vc_ts:
            return pos_b + uv_b, local_f.tobytes(), nv, len(local_f)

        ts_b = encode_fb_tangentspace_vec(
            sn,
            flip_normals=flip_normals,
            verts=sv,
            uvs=suv,
            faces=local_f.astype(np.int32),
        )
        if vc_layers is not None and len(vc_layers) == total_verts:
            vc_arr = np.zeros((nv, 4), dtype=np.uint8)
            vc_arr[:, 0] = np.clip(vc_layers[uniq_v], 0, 7).astype(np.uint8)
            vc_b = vc_arr.tobytes()
        else:
            vc_b = b"\x00\x00\x00\x00" * nv

        return pos_b + uv_b + vc_b + ts_b, local_f.tobytes(), nv, len(local_f)

    # Build contiguous per-section Structure-of-Arrays (SoA) vertex buffers and 0-based local index buffers
    # matching vanilla Bundle #421 (HockeyNet) layout:
    # - Sec[0] (0x771 Opaque): strides=[8,4,4,4] (20B/vert) at v_off0 = 0
    # - Sec[1] (0x273 Cutout): strides=[8,4,4,4] (20B/vert) at v_off1 = len(vb0)
    # - Sec[2] (Cutout Shadow): strides=[8,4]    (12B/vert) at v_off2 = len(vb0) + len(vb1)
    vb0, ib0, nv0, n_opaque = _extract_submesh_vb_ib(opaque_f, include_vc_ts=True)
    vb1, ib1, nv1, n_cutout = _extract_submesh_vb_ib(cutout_f, include_vc_ts=True)
    vb2, ib2, nv2, _ = _extract_submesh_vb_ib(cutout_f, include_vc_ts=False)

    v_off0 = 0
    v_off1 = len(vb0)
    v_off2 = len(vb0) + len(vb1)

    start_i0 = 0
    start_i1 = n_opaque * 3
    start_i2 = (n_opaque + n_cutout) * 3

    unified_vb = bytearray(vb0 + vb1 + vb2)
    vb_pad = (16 - (len(unified_vb) % 16)) % 16
    unified_vb.extend(b"\x00" * vb_pad)
    vb_size_field = len(unified_vb)

    unified_ib = bytearray(ib0 + ib1 + ib2)
    ib_size_field = (len(unified_ib) + 3) & ~3
    if len(unified_ib) < ib_size_field:
        unified_ib.extend(b"\x00" * (ib_size_field - len(unified_ib)))

    new_chk = bytearray(unified_vb + unified_ib)
    chk_pad = (16 - (len(new_chk) % 16)) % 16
    new_chk.extend(b"\x00" * chk_pad)

    ms = bytearray(donor_ms_b421)
    min_x, min_y, min_z = [float(x) - 0.25 for x in verts.min(axis=0)]
    max_x, max_y, max_z = [float(x) + 0.25 for x in verts.max(axis=0)]
    min_v = (min_x, min_y, min_z, 0.0)
    max_v = (max_x, max_y, max_z, 0.0)
    struct.pack_into("<4f4f", ms, 0x10, *min_v, *max_v)

    lod_ptrs = [p for p in struct.unpack_from("<6Q", ms, 0x30) if p != 0]
    for li, lp in enumerate(lod_ptrs):
        sc_i = struct.unpack_from("<I", ms, lp + 0x18)[0]
        sptr_i = struct.unpack_from("<Q", ms, lp + 0x1C)[0]
        struct.pack_into("<II", ms, lp + 0x68, ib_size_field, vb_size_field)
        if sc_i >= 3:
            # Sec[0] = Opaque 0x771, Sec[1] = Cutout 0x273, Sec[2] = Cutout Shadow
            # IMPORTANT: Patch BOTH +0x30..+0x3C (prim, start_i, v_off, v_cnt) AND +0x64 (v_off) AND +0x150 (start_i)!
            sp0 = sptr_i + 0 * 0x180
            sp1 = sptr_i + 1 * 0x180
            sp2 = sptr_i + 2 * 0x180
            struct.pack_into("<4I", ms, sp0 + 0x30, n_opaque, start_i0, v_off0, nv0)
            struct.pack_into("<I", ms, sp0 + 0x64, v_off0)
            struct.pack_into("<I", ms, sp0 + 0x150, start_i0)
            struct.pack_into("<4f4f", ms, sp0 + 0x160, *min_v, *max_v)

            struct.pack_into("<4I", ms, sp1 + 0x30, n_cutout, start_i1, v_off1, nv1)
            struct.pack_into("<I", ms, sp1 + 0x64, v_off1)
            struct.pack_into("<I", ms, sp1 + 0x150, start_i1)
            struct.pack_into("<4f4f", ms, sp1 + 0x160, *min_v, *max_v)

            struct.pack_into("<4I", ms, sp2 + 0x30, n_cutout, start_i2, v_off2, nv2)
            struct.pack_into("<I", ms, sp2 + 0x64, v_off2)
            struct.pack_into("<I", ms, sp2 + 0x150, start_i2)
            struct.pack_into("<4f4f", ms, sp2 + 0x160, *min_v, *max_v)

            # Restrict Cat[3] (ZOnly) and Cat[4] (Shadow) on LOD0/LOD1 to Sec[0] (Opaque) only so Sec[2] is never invoked
            for ci in (3, 4):
                cat_off = lp + 0x10 + 0x14 + ci * 12
                c_cnt, c_ptr = struct.unpack_from("<IQ", ms, cat_off)
                if c_cnt > 0 and c_ptr + 16 < len(ms):
                    struct.pack_into("<I", ms, cat_off, 1)
                    ms[c_ptr + 16] = 0
        elif sc_i == 1:
            # LOD2 has only Sec[0] (Opaque 0x771)
            sp0 = sptr_i
            struct.pack_into("<4I", ms, sp0 + 0x30, n_opaque, start_i0, v_off0, nv0)
            struct.pack_into("<I", ms, sp0 + 0x64, v_off0)
            struct.pack_into("<I", ms, sp0 + 0x150, start_i0)
            struct.pack_into("<4f4f", ms, sp0 + 0x160, *min_v, *max_v)

    mc = bytearray(donor_mc_ebx)
    if len(mc) >= 20 + 172:
        struct.pack_into("<4f4f", mc, 20 + 140, *min_v, *max_v)

    return bytes(ms), bytes(mc), bytes(new_chk), min_v, max_v


def _apply_2x2m_detail_shader_overlay(norm_img, msk_img, preset: str, intensity: float, tile_scale_m: float = 1.0):
    """Blends procedural 2x2m micro-detail into _n and _msk maps without touching _c Albedo, honoring tile_scale_m."""
    intensity = max(0.0, min(1.0, float(intensity)))
    if intensity <= 1e-4:
        return norm_img, msk_img

    tile_scale_m = max(0.25, min(8.0, float(tile_scale_m or 1.0)))
    # Relative frequency vs legacy 2.0m tile (default 1.0m -> 2x finer grain; 0.5m -> 4x finer grain)
    freq_mult = max(0.25, min(8.0, 2.0 / tile_scale_m))

    preset_key = str(preset or "CONCRETE_SIDEWALK").upper().strip()
    w, h = 1024, 1024
    if norm_img is None:
        norm_img = Image.new("RGBA", (w, h), (128, 128, 255, 255))
    elif norm_img.size != (w, h):
        norm_img = norm_img.resize((w, h), Image.Resampling.LANCZOS)

    if msk_img is None:
        msk_img = Image.new("RGBA", (w, h), (0, 140, 255, 255))
    elif msk_img.size != (w, h):
        msk_img = msk_img.resize((w, h), Image.Resampling.LANCZOS)

    # Deterministic tileable micro-surface synthesis scaled by freq_mult
    seed_map = {
        "CONCRETE_SIDEWALK": 101,
        "SYNTHETIC_PAINT": 202,
        "METAL_BRASS": 303,
        "SKATEPARK_TRIM": 404,
    }
    rng = np.random.default_rng(seed_map.get(preset_key, 101))
    yy, xx = np.meshgrid(np.linspace(0.0, 2.0 * np.pi * freq_mult, h, endpoint=False, dtype=np.float32),
                         np.linspace(0.0, 2.0 * np.pi * freq_mult, w, endpoint=False, dtype=np.float32),
                         indexing="ij")

    def _scaled_noise(base_div: float, resample_mode=Image.Resampling.BICUBIC) -> np.ndarray:
        div = max(1.0, float(base_div) / freq_mult)
        gh = max(16, min(h, int(round(h / div))))
        gw = max(16, min(w, int(round(w / div))))
        g = rng.standard_normal((gh, gw)).astype(np.float32)
        if gh == h and gw == w:
            return np.clip(g * 0.25, -0.5, 0.5)
        im = Image.fromarray(np.clip((g * 0.25 + 0.5) * 255.0, 0, 255).astype(np.uint8), mode="L").resize((w, h), resample_mode)
        return np.asarray(im, dtype=np.float32) / 255.0 - 0.5

    if preset_key == "METAL_BRASS":
        # Anisotropic brushed metal striations + subtle cross grain
        div_x = max(1.0, 8.0 / freq_mult)
        gw = max(16, min(w, int(round(w / div_x))))
        fine = rng.standard_normal((h, gw)).astype(np.float32)
        fine_im = Image.fromarray(np.clip((fine * 0.25 + 0.5) * 255.0, 0, 255).astype(np.uint8), mode="L").resize((w, h), Image.Resampling.BILINEAR)
        h_field = (np.asarray(fine_im, dtype=np.float32) / 255.0 - 0.5) * 0.8 + 0.2 * np.sin(xx * 24.0 + yy * 4.0)
        smooth_mod = h_field * 0.08 * intensity
        metal_mod = 0.06 * intensity
        norm_scale = 0.35 * intensity
    elif preset_key == "SYNTHETIC_PAINT":
        # Fine isotropic orange-peel synthetic paint micro-grain
        h_field = _scaled_noise(2.0, Image.Resampling.BICUBIC)
        smooth_mod = h_field * 0.06 * intensity
        metal_mod = 0.0
        norm_scale = 0.28 * intensity
    elif preset_key == "SKATEPARK_TRIM":
        # Trowel-smoothed transition trim micro-grain
        h_field = 0.6 * _scaled_noise(4.0, Image.Resampling.BICUBIC) + 0.4 * _scaled_noise(2.0, Image.Resampling.BILINEAR)
        smooth_mod = h_field * 0.07 * intensity
        metal_mod = 0.02 * intensity
        norm_scale = 0.32 * intensity
    else:
        # CONCRETE_SIDEWALK default: multi-octave mineral micro-pitting
        h_field = 0.55 * _scaled_noise(4.0, Image.Resampling.BICUBIC) + 0.45 * _scaled_noise(1.0, Image.Resampling.BILINEAR)
        smooth_mod = (-0.06 + h_field * 0.08) * intensity
        metal_mod = 0.0
        norm_scale = 0.42 * intensity

    # Compute tangent-space normal perturbation from height field
    dh_dx = (np.roll(h_field, -1, axis=1) - np.roll(h_field, 1, axis=1)) * norm_scale
    dh_dy = (np.roll(h_field, -1, axis=0) - np.roll(h_field, 1, axis=0)) * norm_scale

    n_arr = np.asarray(norm_img.convert("RGBA"), dtype=np.float32) / 255.0
    nx = (n_arr[:, :, 0] * 2.0 - 1.0) + dh_dx
    ny = (n_arr[:, :, 1] * 2.0 - 1.0) + dh_dy
    nz = np.maximum(0.1, n_arr[:, :, 2] * 2.0 - 1.0)
    inv_len = 1.0 / np.sqrt(nx * nx + ny * ny + nz * nz + 1e-8)
    n_out = np.empty_like(n_arr)
    n_out[:, :, 0] = np.clip((nx * inv_len) * 0.5 + 0.5, 0.0, 1.0)
    n_out[:, :, 1] = np.clip((ny * inv_len) * 0.5 + 0.5, 0.0, 1.0)
    n_out[:, :, 2] = np.clip((nz * inv_len) * 0.5 + 0.5, 0.0, 1.0)
    n_out[:, :, 3] = 1.0
    norm_out_img = Image.fromarray(np.clip(np.round(n_out * 255.0), 0, 255).astype(np.uint8), mode="RGBA")

    m_arr = np.asarray(msk_img.convert("RGBA"), dtype=np.float32) / 255.0
    m_arr[:, :, 0] = np.clip(m_arr[:, :, 0] + metal_mod, 0.0, 1.0)
    m_arr[:, :, 1] = np.clip(m_arr[:, :, 1] + smooth_mod, 0.0, 1.0)
    msk_out_img = Image.fromarray(np.clip(np.round(m_arr * 255.0), 0, 255).astype(np.uint8), mode="RGBA")

    print(
        f"  [2x2m Detail] Applied '{preset_key}' micro-detail to _n and _msk "
        f"(intensity={intensity:.2f}, scale={tile_scale_m:.2f}m; _c Albedo untouched).",
        flush=True,
    )
    return norm_out_img, msk_out_img


def _partition_collision_shapes_by_face_surfaces(
    base_col_shapes: list[dict],
    face_surface_groups: list[dict],
    default_packed: int = 96,
    round_rail: bool = False,
    max_total_shapes: int = 12,
) -> tuple[list[dict], bool]:
    """
    Partitions collision triangles across `base_col_shapes` into Havok sub-shapes based on
    per-face surface audio overrides (`face_surface_groups` painted in Edit Mode).
    Supports both 1:1 / decimated meshes and simplified hull projections.
    """
    if not base_col_shapes or not face_surface_groups:
        return base_col_shapes, False

    # Collect all shape face centroids in GLB space so we can auto-select Z-up -> Y-up vs direct coordinates
    all_shape_centroids = []
    for s in base_col_shapes:
        tm = s.get("mesh")
        if tm is not None and hasattr(tm, "vertices") and hasattr(tm, "faces") and len(tm.faces) > 0:
            v_arr = np.asarray(tm.vertices, dtype=np.float64)
            f_arr = np.asarray(tm.faces, dtype=np.int32)
            all_shape_centroids.append(v_arr[f_arr].mean(axis=1))
    if not all_shape_centroids:
        return base_col_shapes, False
    ref_centroids = np.vstack(all_shape_centroids)

    parsed_groups = []
    for grp in face_surface_groups:
        if not isinstance(grp, dict):
            continue
        raw_tris = grp.get("triangles") or grp.get("triangles_zup") or grp.get("triangles_yup") or []
        if not raw_tris:
            continue
        grp_code = int(grp.get("code") or grp.get("packed") or default_packed)
        if round_rail:
            grp_code = apply_grind_mode_to_packed(grp_code, "round_rail")
        tri_arr = np.asarray(raw_tris, dtype=np.float64)
        if tri_arr.ndim != 3 or tri_arr.shape[1] != 3 or tri_arr.shape[2] != 3:
            continue

        # Candidate A: Blender Z-up (x, y, z) -> GLB Y-up (x, z, -y)
        tri_yup = np.empty_like(tri_arr)
        tri_yup[:, :, 0] = tri_arr[:, :, 0]
        tri_yup[:, :, 1] = tri_arr[:, :, 2]
        tri_yup[:, :, 2] = -tri_arr[:, :, 1]

        c_raw = tri_arr.mean(axis=1)
        c_yup = tri_yup.mean(axis=1)

        # Pick whichever coordinate convention has closer centroid proximity to ref_centroids
        sample_ref = ref_centroids[::max(1, len(ref_centroids) // 128)]
        d_yup = np.linalg.norm(c_yup[:32, None, :] - sample_ref[None, :, :], axis=2).min(axis=1).mean()
        d_raw = np.linalg.norm(c_raw[:32, None, :] - sample_ref[None, :, :], axis=2).min(axis=1).mean()
        use_tris = tri_yup if d_yup <= d_raw + 1e-4 else tri_arr
        use_cents = use_tris.mean(axis=1)
        e1 = use_tris[:, 1, :] - use_tris[:, 0, :]
        e2 = use_tris[:, 2, :] - use_tris[:, 0, :]
        norms = np.cross(e1, e2)
        n_len = np.linalg.norm(norms, axis=1, keepdims=True)
        norms = np.where(n_len > 1e-9, norms / np.maximum(n_len, 1e-9), 0.0)
        parsed_groups.append({
            "code": grp_code,
            "label": str(grp.get("label") or grp.get("surface_key") or f"Code_{grp_code}"),
            "surface_key": str(grp.get("surface_key") or "Concrete_Solid"),
            "centroids": use_cents,
            "normals": norms,
        })

    if not parsed_groups:
        return base_col_shapes, False

    out_shapes: list[dict] = []
    did_split = False

    for s in base_col_shapes:
        tm = s.get("mesh")
        if tm is None or not hasattr(tm, "faces") or len(tm.faces) == 0:
            out_shapes.append(s)
            continue

        v_arr = np.asarray(tm.vertices, dtype=np.float64)
        f_arr = np.asarray(tm.faces, dtype=np.int32)
        f_cents = v_arr[f_arr].mean(axis=1)
        fe1 = v_arr[f_arr[:, 1]] - v_arr[f_arr[:, 0]]
        fe2 = v_arr[f_arr[:, 2]] - v_arr[f_arr[:, 0]]
        f_norms = np.cross(fe1, fe2)
        fn_len = np.linalg.norm(f_norms, axis=1, keepdims=True)
        f_norms = np.where(fn_len > 1e-9, f_norms / np.maximum(fn_len, 1e-9), 0.0)

        base_code = int(s.get("packed", default_packed))
        assigned_codes = np.full(len(f_arr), base_code, dtype=np.int32)
        best_dist = np.full(len(f_arr), 1e9, dtype=np.float64)

        is_hull = str(s.get("mode", "")).lower() == "convex_hull"
        max_dist_thresh = 0.22 if is_hull else 0.09

        for pg in parsed_groups:
            pg_cents = pg["centroids"]
            pg_norms = pg["normals"]
            pg_code = int(pg["code"])
            # Compute pairwise distances in chunks to stay fast and low-memory
            for start_i in range(0, len(f_cents), 512):
                end_i = min(len(f_cents), start_i + 512)
                fc_chunk = f_cents[start_i:end_i]
                fn_chunk = f_norms[start_i:end_i]
                diff = fc_chunk[:, None, :] - pg_cents[None, :, :]
                dists = np.linalg.norm(diff, axis=2)
                min_idx = np.argmin(dists, axis=1)
                min_d = dists[np.arange(len(fc_chunk)), min_idx]
                dots = np.sum(fn_chunk * pg_norms[min_idx], axis=1)
                # Accept exact centroid match (<= 0.025m) or aligned normal match within threshold
                qualifies = (min_d <= 0.025) | ((min_d <= max_dist_thresh) & (dots >= 0.45))
                better = qualifies & (min_d < best_dist[start_i:end_i])
                if np.any(better):
                    best_dist[start_i:end_i][better] = min_d[better]
                    assigned_codes[start_i:end_i][better] = pg_code

        unique_codes = [int(c) for c in np.unique(assigned_codes)]
        if len(unique_codes) == 1:
            s_copy = dict(s)
            s_copy["packed"] = unique_codes[0]
            if unique_codes[0] != base_code:
                did_split = True
            out_shapes.append(s_copy)
        else:
            did_split = True
            for c_val in unique_codes:
                sub_mask = assigned_codes == c_val
                sub_f = f_arr[sub_mask]
                if len(sub_f) == 0:
                    continue
                sub_tm = trimesh.Trimesh(vertices=v_arr.copy(), faces=sub_f.copy(), process=True)
                if len(sub_tm.faces) == 0:
                    continue
                s_sub = dict(s)
                s_sub["mesh"] = sub_tm
                s_sub["mode"] = "triangle_mesh"
                s_sub["packed"] = int(c_val)
                out_shapes.append(s_sub)

    if len(out_shapes) > max_total_shapes:
        # Sort by face count descending so major surfaces get dedicated shapes
        out_shapes.sort(key=lambda item: len(item["mesh"].faces) if item.get("mesh") is not None else 0, reverse=True)
        out_shapes = out_shapes[:max_total_shapes]

    if did_split:
        summary_str = ", ".join(
            f"#{int(sh['packed'])} ({len(sh['mesh'].faces)} tris)"
            for sh in out_shapes
            if sh.get("mesh") is not None
        )
        print(f"  [Face Surface Audio] Partitioned collision into {len(out_shapes)} Havok sub-shape(s): {summary_str}", flush=True)

    return out_shapes, did_split


def _partition_collision_shapes_by_grind_paths(
    base_col_shapes: list[dict],
    grind_paths: list[dict],
    default_packed: int = 96,
    max_total_shapes: int = 12,
) -> tuple[list[dict], bool]:
    """
    When a prop has marked `grind_paths` AND its body surface has `Do-Not-Align` (PropertySlot 111 / 1)
    or `No-Grind` (0x0800) or a `No_Grind` skatestopper path, partitions the triangles adjacent to
    each marked grind path into a dedicated flush Havok `triangle_mesh` sub-shape with
    `IncludeInSurfaceAnalysis` (`PropertySlot 71`) so the marked ledge is always 100% grindable
    even when the rest of the barrier has `Do-Not-Align` enabled!
    """
    if not base_col_shapes or not grind_paths:
        return base_col_shapes, False

    parsed_paths = []
    for gp in grind_paths:
        if not isinstance(gp, dict) or not gp.get("enabled", True):
            continue
        raw_segs = gp.get("segments") or []
        if not raw_segs:
            continue
        is_z_up = bool(gp.get("z_up", True))
        pts = []
        for seg in raw_segs:
            if len(seg) >= 2:
                for p in (seg[0], seg[1]):
                    if is_z_up:
                        pts.append([float(p[0]), float(p[2]), -float(p[1])])
                    else:
                        pts.append([float(p[0]), float(p[1]), float(p[2])])
        if not pts:
            continue
        pts_arr = np.unique(np.round(np.asarray(pts, dtype=np.float64), 5), axis=0)
        gp_surf = str(gp.get("surface_id") or "Metal_Solid_Grindable")
        gp_code = int(gp.get("surface_code", 352) or 352)
        gp_type = str(gp.get("type", "LEDGE")).upper()
        if gp_surf == "No_Grind" or gp_code == 5920:
            gp_packed = apply_grind_mode_to_packed(gp_code, "no_grind")
            is_rr = False
        else:
            # Strip any Do-Not-Align or No-Grind bits and enable PropertySlot 71 IncludeInSurfaceAnalysis
            clean_base = gp_code & 0x0007F7FF
            if clean_base <= 0:
                clean_base = 352
            gp_packed = apply_grind_mode_to_packed(clean_base, "round_rail")
            is_rr = (gp_type in ("ROUND_RAIL", "COPING"))
        parsed_paths.append({
            "pts": pts_arr,
            "packed": int(gp_packed),
            "surface_id": gp_surf,
            "round_rail": is_rr,
            "band_dist": max(0.065, float(gp.get("radius", 0.032) or 0.032) * 2.2),
        })

    if not parsed_paths:
        return base_col_shapes, False

    out_shapes: list[dict] = []
    did_split = False
    for s in base_col_shapes:
        tm = s.get("mesh")
        if tm is None or not hasattr(tm, "faces") or len(tm.faces) == 0:
            out_shapes.append(s)
            continue
        base_code = int(s.get("packed", default_packed))
        base_prop_slot = (base_code >> 19) & 0x1FFF
        base_mat_slot = (base_code >> 6) & 0x1FFF
        base_is_dna_or_ng = (base_prop_slot in (1, 68, 105, 111)) or (base_mat_slot in (9, 43, 64, 65, 68, 74, 105, 111))
        has_no_grind_path = any((((pp["packed"] >> 19) & 0x1FFF) in (68, 105)) for pp in parsed_paths)
        if not base_is_dna_or_ng and not has_no_grind_path:
            out_shapes.append(s)
            continue

        v_arr = np.asarray(tm.vertices, dtype=np.float64)
        f_arr = np.asarray(tm.faces, dtype=np.int32)
        assigned_codes = np.full(len(f_arr), base_code, dtype=np.int32)
        for pp in parsed_paths:
            v_dists = np.linalg.norm(v_arr[:, None, :] - pp["pts"][None, :, :], axis=2).min(axis=1)
            v_near = v_dists <= pp["band_dist"]
            f_near = v_near[f_arr[:, 0]] | v_near[f_arr[:, 1]] | v_near[f_arr[:, 2]]
            if np.any(f_near):
                assigned_codes[f_near] = int(pp["packed"])

        unique_codes = [int(c) for c in np.unique(assigned_codes)]
        if len(unique_codes) <= 1:
            out_shapes.append(s)
            continue

        did_split = True
        for c_val in unique_codes:
            sub_f = f_arr[assigned_codes == c_val]
            if len(sub_f) == 0:
                continue
            sub_tm = trimesh.Trimesh(vertices=v_arr.copy(), faces=sub_f.copy(), process=True)
            if len(sub_tm.faces) == 0:
                continue
            s_sub = dict(s)
            s_sub["mesh"] = sub_tm
            s_sub["mode"] = "triangle_mesh"
            s_sub["packed"] = int(c_val)
            s_sub["grind_curve"] = (((int(c_val) >> 19) & 0x1FFF) == 71)
            out_shapes.append(s_sub)

    if len(out_shapes) > max_total_shapes:
        out_shapes = out_shapes[:max_total_shapes]
    if did_split:
        print(f"  [Grind Path Partition] Split marked grind lips into {len(out_shapes)} Havok sub-shapes so lips grind cleanly.", flush=True)
    return out_shapes, did_split


def fit_havok_contoured_hexahedron(
    phys_res_bytes: bytes,
    hull_verts: np.ndarray,
    material_packed: int | None = None,
) -> bytes:
    """
    Fits a single 1-hull 0x88 Havok convex hexahedron (based on tpl['res16'], nv=8, ne=24, nf=6, mv=0)
    to `hull_verts` in GLB Y-up space using strictly orthogonal axis-aligned polytope bounds so vertices
    always satisfy the hardcoded quantized face-plane equations at rel[9] = 0x130 and never invert.
    """
    pts = np.asarray(hull_verts, dtype=np.float32)
    if len(pts) == 0:
        b_min = np.array([-0.05, 0.0, -0.05], dtype=np.float32)
        b_max = np.array([0.05, 0.10, 0.05], dtype=np.float32)
    else:
        b_min = pts.min(axis=0)
        b_max = np.maximum(pts.max(axis=0), b_min + 0.025)
    raw_b, _, _ = fit_havok_polytope_to_bounds(phys_res_bytes, b_min, b_max, material_packed)
    return raw_b


def _align_collision_mesh_to_visual_bounds(
    c_verts: np.ndarray,
    c_submeshes: list | None,
    vis_min: np.ndarray | list[float],
    vis_max: np.ndarray | list[float],
    label: str = "collision",
) -> tuple[np.ndarray, list]:
    """
    Detects when an exported custom collision or placement proxy GLB (`_col.glb` / `_pphys.glb`)
    has a world-space origin offset relative to the visual mesh (e.g. when `Ctrl+A -> Location`
    was applied to one object in Blender but not the other, as in `own_customprops_railsimple_col.glb`),
    and translates `c_verts` and `c_submeshes` so its bounding box aligns with `[vis_min, vis_max]`.
    """
    v_arr = np.asarray(c_verts, dtype=np.float32)
    subs = list(c_submeshes) if c_submeshes is not None else []
    if len(v_arr) == 0:
        return v_arr, subs

    vmin = np.asarray(vis_min, dtype=np.float32)
    vmax = np.asarray(vis_max, dtype=np.float32)
    v_center = 0.5 * (vmin + vmax)
    v_span = np.maximum(vmax - vmin, 0.10)

    cmin = v_arr.min(axis=0)
    cmax = v_arr.max(axis=0)
    c_center = 0.5 * (cmin + cmax)

    # Check if bounding boxes are disjoint or horizontal XZ / vertical Y center is displaced beyond visual bounds
    no_overlap_xz = (
        cmax[0] < vmin[0] - 0.25
        or cmin[0] > vmax[0] + 0.25
        or cmax[2] < vmin[2] - 0.25
        or cmin[2] > vmax[2] + 0.25
    )
    center_dist = float(np.linalg.norm(c_center - v_center))
    max_allowed_dist = float(0.65 * np.linalg.norm(v_span) + 0.50)

    if no_overlap_xz or center_dist > max_allowed_dist:
        # Align horizontal X/Z centers and ground Y-minimum (GLB Y-up) to the visual mesh
        dx = float(v_center[0] - c_center[0])
        dy = float(vmin[1] - cmin[1]) if abs(float(cmin[1] - vmin[1])) > 0.25 else 0.0
        dz = float(v_center[2] - c_center[2])
        delta = np.array([dx, dy, dz], dtype=np.float32)
        v_arr = (v_arr + delta).astype(np.float32)
        aligned_subs = []
        for sm in subs:
            sm_c = sm.copy()
            sm_c.apply_translation([float(dx), float(dy), float(dz)])
            aligned_subs.append(sm_c)
        print(
            f"  [Collision Alignment] Auto-centered displaced {label} mesh by "
            f"({dx:+.2f}m, {dy:+.2f}m, {dz:+.2f}m) to match visual mesh bounds.",
            flush=True,
        )
        return v_arr, aligned_subs

    return v_arr, subs


def decompose_mesh_for_placement_proxy_8hulls(
    verts: np.ndarray,
    faces: np.ndarray,
    target_hulls: int = 8,
) -> list[np.ndarray]:
    """
    Decomposes a prop mesh `(verts, faces)` into exactly `target_hulls` (8) tight point clusters
    by first separating disconnected mesh components (e.g. rail bars, support legs, feet, seat planks)
    and then recursively subdividing the largest components along their principal axes.
    """
    v = np.asarray(verts, dtype=np.float32)
    f = np.asarray(faces, dtype=np.int32)
    if len(v) == 0:
        return [np.array([[-0.05, 0.0, -0.05], [0.05, 0.1, 0.05]], dtype=np.float32)] * target_hulls

    clusters: list[np.ndarray] = []
    if HAS_TRIMESH and len(f) >= 4:
        try:
            tm = trimesh.Trimesh(vertices=v, faces=f, process=False)
            parts = tm.split(only_watertight=False)
            for p in parts:
                pv = np.asarray(p.vertices, dtype=np.float32)
                if len(pv) >= 3:
                    # Include triangle centroids so thin long faces subdivide evenly
                    pf = np.asarray(p.faces, dtype=np.int32)
                    if len(pf) > 0:
                        cents = pv[pf].mean(axis=1)
                        pv = np.vstack([pv, cents])
                    clusters.append(pv)
        except Exception:
            clusters = []

    if not clusters:
        if len(f) > 0:
            cents = v[f].mean(axis=1)
            clusters = [np.vstack([v, cents])]
        else:
            clusters = [v]

    # Sort clusters by bounding-box diagonal/volume descending
    def _cluster_score(pts: np.ndarray) -> float:
        if len(pts) < 2:
            return 0.0
        sp = pts.max(axis=0) - pts.min(axis=0)
        return float(np.max(sp)) * float(len(pts) ** 0.25)

    if len(clusters) > target_hulls:
        clusters.sort(key=_cluster_score, reverse=True)
        kept = clusters[:target_hulls]
        extra = clusters[target_hulls:]
        kept_centers = [c.mean(axis=0) for c in kept]
        for ex in extra:
            ex_c = ex.mean(axis=0)
            nearest_idx = int(np.argmin([np.linalg.norm(ex_c - kc) for kc in kept_centers]))
            kept[nearest_idx] = np.vstack([kept[nearest_idx], ex])
            kept_centers[nearest_idx] = kept[nearest_idx].mean(axis=0)
        clusters = kept

    # Recursively split the largest cluster until we have `target_hulls` clusters
    while len(clusters) < target_hulls:
        best_idx = -1
        best_sc = 0.0
        for idx, cl in enumerate(clusters):
            if len(cl) < 4:
                continue
            sp = cl.max(axis=0) - cl.min(axis=0)
            max_sp = float(np.max(sp))
            if max_sp < 0.04:
                continue
            sc = max_sp * (len(cl) ** 0.15)
            if sc > best_sc:
                best_sc = sc
                best_idx = idx
        if best_idx < 0:
            break

        cl = clusters[best_idx]
        sp = cl.max(axis=0) - cl.min(axis=0)
        dom_ax = int(np.argmax(sp))
        vals = cl[:, dom_ax]
        med = float(np.median(vals))
        lo_mask = vals <= med
        hi_mask = vals > med
        if not np.any(lo_mask) or not np.any(hi_mask):
            mid = 0.5 * float(vals.min() + vals.max())
            lo_mask = vals <= mid
            hi_mask = vals > mid
        if not np.any(lo_mask) or not np.any(hi_mask):
            half = len(cl) // 2
            c_lo, c_hi = cl[:half], cl[half:]
        else:
            c_lo, c_hi = cl[lo_mask], cl[hi_mask]
        clusters[best_idx] = c_lo
        clusters.append(c_hi)

    while len(clusters) < target_hulls:
        clusters.append(clusters[-1].copy())

    return clusters[:target_hulls]


def preprocess_prop_manifest_entry(prop_entry: dict, tpl: dict, prop_idx: int = 1, prop_total: int = 1) -> dict:
    """Preprocesses a single prop manifest entry into binary asset chunks."""
    _refresh_pack_asset_cache_for_prop(prop_entry)
    raw_id = str(prop_entry.get("prop_id") or prop_entry.get("prop_key", "own_customprops_prop")).strip()
    stem = _clean_prop_stem(
        raw_id,
        extra_prefixes=[
            prop_entry.get("target_mod_name"),
            prop_entry.get("custom_qd_tab"),
            prop_entry.get("category"),
        ],
    )
    slug = clamp_slug(f"own_customprops_{stem}", 36)

    raw_display_name = prop_entry.get("display_name", stem.replace("_", " ").title())
    display_name = sanitize_display_name(raw_display_name)
    category = prop_entry.get("category", "Custom Props")
    surface_code = int(prop_entry.get("surface_material_code", 96))
    surface_id = prop_entry.get("surface_id", "Concrete_Solid")
    round_rail = bool(prop_entry.get("round_rail", False))
    enable_custom_textures = bool(prop_entry.get("enable_custom_textures", True))
    roughness_mult = float(prop_entry.get("roughness_multiplier", 0.45))
    metallic_mult = float(prop_entry.get("metallic_multiplier", 0.0))
    specular_mult = float(prop_entry.get("specular_multiplier", 0.50))
    albedo_tint = [float(v) for v in prop_entry.get("albedo_tint", [1.0, 1.0, 1.0])]
    lights = prop_entry.get("lights", [])
    col_mode = str(prop_entry.get("collision_mode", "SAME_AS_MESH")).upper()
    placement_proxy_mode = str(prop_entry.get("placement_proxy_mode", "MATCH_COLLISION") or "MATCH_COLLISION").upper()
    max_col_tris = int(prop_entry.get("max_col_tris", 5000))
    max_hulls = int(prop_entry.get("coacd_max_hulls", prop_entry.get("max_hulls", 8)))
    material_surfaces = prop_entry.get("material_surfaces", [])
    material_textures = prop_entry.get("material_textures", [])
    look_mode = str(prop_entry.get("look_mode", "AS_IS")).upper()
    grind_paths = prop_entry.get("grind_paths", [])
    has_rr_grind_paths = bool(grind_paths) and all(
        str(gp.get("type", "")).upper() in ("ROUND_RAIL", "COPING") for gp in grind_paths if isinstance(gp, dict)
    )
    if surface_code in (2976, 37227424) or str(surface_id) == "metal_rail" or has_rr_grind_paths:
        round_rail = True
        if has_rr_grind_paths and surface_code == 352:
            surface_code = 2976
            surface_id = "metal_rail"
            for ms in material_surfaces:
                if isinstance(ms, dict) and int(ms.get("packed", 0)) == 352:
                    ms["packed"] = 2976
                    ms["surface"] = "metal_rail"
    anti_lip_enabled = bool(prop_entry.get("anti_lip_enabled", False))
    anti_lip_sink_m = float(prop_entry.get("anti_lip_sink_m", 0.008))
    use_2x2m_detail_shader = bool(prop_entry.get("use_2x2m_detail_shader", False))
    detail_2x2m_preset = str(prop_entry.get("detail_2x2m_preset", "CONCRETE_SIDEWALK"))
    detail_2x2m_intensity = float(prop_entry.get("detail_2x2m_intensity", 0.35))
    detail_2x2m_scale = float(prop_entry.get("detail_2x2m_scale", 1.0))
    try:
        atlas_res = int(prop_entry.get("atlas_resolution", 1024))
    except Exception:
        atlas_res = 1024
    if atlas_res not in (1024, 2048, 4096, 8192):
        atlas_res = 1024
    exp_cfg = prop_entry.get("experimental") if isinstance(prop_entry.get("experimental"), dict) else {}

    # 1. Load visual geometry & submeshes
    emit_prop_substep(prop_idx, prop_total, display_name, 1, "Parsing GLB & UVs...")
    glb_path = prop_entry.get("glb_path")
    verts, norms, uvs, faces, submeshes = load_mesh_geometry_and_submeshes(glb_path)

    vmin = verts.min(axis=0)
    vmax = verts.max(axis=0)
    bounds_min = [float(x) for x in vmin]
    bounds_max = [float(x) for x in vmax]

    vm16, _, _, vol, com, obb_ext = compute_mesh_volume_moments(verts, faces)

    # 2. Resolve textures, per-submesh vc_layers (0..7), and per-slot PBR classes (Smoothness, Metallic, Tint, Emission)
    emit_prop_substep(prop_idx, prop_total, display_name, 2, "Baking DDS Textures & ORM Mask...")
    tex_path = prop_entry.get("texture_path")
    atlas_img = None

    # Filter visible submeshes if any material slot is marked `invisible: True` (Collision Only)
    vis_indices = [
        idx for idx in range(len(submeshes))
        if not (idx < len(material_textures) and bool(material_textures[idx].get("invisible", False)))
    ]
    if not vis_indices:
        vis_indices = list(range(len(submeshes)))

    vc_layer_chunks = []
    for idx in vis_indices:
        sm = submeshes[idx]
        vc_layer_chunks.append(np.full(len(sm.vertices), min(idx, 7), dtype=np.uint8))

    if len(vis_indices) != len(submeshes):
        v_list, n_list, uv_list, f_list = [], [], [], []
        v_off = 0
        for idx in vis_indices:
            sm = submeshes[idx]
            sv = np.asarray(sm.vertices, dtype=np.float32)
            sf = np.asarray(sm.faces, dtype=np.int32)
            sn = np.asarray(sm.vertex_normals, dtype=np.float32)
            if len(sn) != len(sv):
                sn = np.zeros_like(sv, dtype=np.float32)
                if len(sn) > 0:
                    sn[:, 1] = 1.0
            suv = (
                np.asarray(sm.visual.uv, dtype=np.float32)
                if (hasattr(sm, "visual") and hasattr(sm.visual, "uv") and sm.visual.uv is not None and len(sm.visual.uv) == len(sv))
                else np.zeros((len(sv), 2), dtype=np.float32)
            )
            v_list.append(sv)
            n_list.append(sn)
            uv_list.append(suv)
            f_list.append(sf + v_off)
            v_off += len(sv)
        verts = np.vstack(v_list)
        norms = np.vstack(n_list)
        uvs = np.vstack(uv_list)
        faces = np.vstack(f_list)

    vc_layers = np.concatenate(vc_layer_chunks) if vc_layer_chunks else np.zeros(len(verts), dtype=np.uint8)

    # Build up to 8 per-slot PBR classes (Smoothness = 1 - Roughness, Metallic, Tint, Emission)
    defined_pbr_classes: dict[int, dict] = {}
    n_slots_total = max(len(submeshes), len(material_textures), 1)
    for idx in range(min(8, n_slots_total)):
        slot_rough = roughness_mult
        slot_metal = metallic_mult
        slot_tint = list(albedo_tint)
        slot_em_strength = 0.0
        slot_em_color = [1.0, 1.0, 1.0]
        if idx < len(material_textures) and isinstance(material_textures[idx], dict):
            mt = material_textures[idx]
            if mt.get("roughness") is not None:
                slot_rough = float(mt["roughness"])
            if mt.get("metallic") is not None:
                slot_metal = float(mt["metallic"])
            if mt.get("tint") and len(mt["tint"]) >= 3:
                slot_tint = [float(mt["tint"][0]), float(mt["tint"][1]), float(mt["tint"][2])]
            if mt.get("emission_strength") is not None:
                raw_em_s = max(0.0, float(mt["emission_strength"]))
                mat_nm_l = str(mt.get("material_name") or "").lower()
                is_true_em = bool(
                    mt.get("has_emission_map")
                    or raw_em_s > 1.05
                    or (raw_em_s > 0.01 and any(k in mat_nm_l for k in ("emissive", "glow", "neon", "lamp", "led", "light")))
                )
                slot_em_strength = raw_em_s if is_true_em else 0.0
            if mt.get("emission_color") and len(mt["emission_color"]) >= 3:
                slot_em_color = [float(mt["emission_color"][0]), float(mt["emission_color"][1]), float(mt["emission_color"][2])]
            if slot_em_strength > 0.01:
                slot_rough = min(slot_rough, 0.04)
                slot_tint = list(slot_em_color)
        elif idx < len(submeshes):
            sm_mat = getattr(getattr(submeshes[idx], "visual", None), "material", None)
            if sm_mat is not None:
                if getattr(sm_mat, "roughnessFactor", None) is not None and "roughness_multiplier" not in prop_entry:
                    slot_rough = float(sm_mat.roughnessFactor)
                if getattr(sm_mat, "metallicFactor", None) is not None and "metallic_multiplier" not in prop_entry:
                    slot_metal = float(sm_mat.metallicFactor)
        slot_rough = max(0.0, min(1.0, slot_rough))
        slot_smooth = max(0.0, min(1.0, 1.0 - slot_rough))
        slot_metal = max(0.0, min(1.0, slot_metal))
        defined_pbr_classes[idx] = {
            "roughness": round(slot_rough, 4),
            "smoothness": round(slot_smooth, 4),
            "metallic": round(slot_metal, 4),
            "tint": [round(float(c), 4) for c in slot_tint[:3]],
            "emission_strength": round(slot_em_strength, 4),
            "emission_color": [round(float(c), 4) for c in slot_em_color[:3]],
        }

    default_pbr_cls = defined_pbr_classes.get(0, {
        "roughness": round(roughness_mult, 4),
        "smoothness": round(max(0.0, min(1.0, 1.0 - roughness_mult)), 4),
        "metallic": round(max(0.0, min(1.0, metallic_mult)), 4),
        "tint": [round(float(c), 4) for c in albedo_tint[:3]],
        "emission_strength": 0.0,
        "emission_color": [1.0, 1.0, 1.0],
    })
    slot_pbr_classes = [copy.deepcopy(defined_pbr_classes.get(k, default_pbr_cls)) for k in range(8)]

    norm_atlas_img = None
    msk_atlas_img = None
    should_pack_quadrants = False
    max_uv_span = 1.0
    if len(uvs) > 0:
        max_uv_span = max(
            float(uvs[:, 0].max() - uvs[:, 0].min()),
            float(uvs[:, 1].max() - uvs[:, 1].min()),
        )

    # Auto-upgrade 1024 -> 2048 (2K) for multi-material props or tiled UVs so multi-slot atlases & rails stay razor-sharp
    if atlas_res == 1024 and (len(vis_indices) > 1 or max_uv_span > 1.25):
        target_tex_res = 2048
    else:
        target_tex_res = 4096 if atlas_res >= 4096 else (2048 if atlas_res >= 2048 else 1024)
    compose_res = max(target_tex_res, min(8192, atlas_res))

    prop_decal_shader_mode = str(
        prop_entry.get("decal_shader_mode") or exp_cfg.get("decal_shader_mode") or "CUTOUT_273"
    ).strip().upper()
    decal_overlay_vis_set: set[int] = set()
    cutout_vis_set: set[int] = set()
    metallic_decal_vis_set: set[int] = set()
    for idx in vis_indices:
        sm = submeshes[idx]
        _, mt = _resolve_submesh_material_entry(sm, idx, material_textures)
        if mt is not None:
            slot_decal_mode = str(mt.get("decal_shader_mode") or prop_decal_shader_mode).strip().upper()
            is_metallic_decal_slot = (slot_decal_mode == "METALLIC_771")
            src_nm_low = str(mt.get("source_image_name") or "").lower()
            is_baked_opaque_slot = (
                not bool(mt.get("is_decal_slot", False))
                and str(mt.get("domain") or "").lower() != "decal"
                and any(k in src_nm_low for k in ("_basecolor", "_ambientocclusion", "_roughness", "_metallic", "_normal", "reskate_atlas_", "reskate_baked_"))
            )
            if str(mt.get("domain") or "").lower() == "decal" or bool(mt.get("is_decal_slot", False)):
                decal_overlay_vis_set.add(idx)
                if is_metallic_decal_slot:
                    metallic_decal_vis_set.add(idx)
                else:
                    cutout_vis_set.add(idx)
            elif not is_baked_opaque_slot and bool(mt.get("has_alpha_transparency", False)):
                if is_metallic_decal_slot:
                    metallic_decal_vis_set.add(idx)
                else:
                    cutout_vis_set.add(idx)

    if len(vis_indices) > 1 and look_mode != "BAKE":
        part_specs = []
        norm_part_specs = []
        msk_part_specs = []
        any_slot_has_norm = False
        distinct_image_keys = set()
        distinct_finish_keys = set()
        first_real_im = None
        for idx in vis_indices:
            sm = submeshes[idx]
            sv = np.asarray(sm.vertices, dtype=np.float32)
            suv = (
                np.asarray(sm.visual.uv, dtype=np.float32)
                if (hasattr(sm, "visual") and hasattr(sm.visual, "uv") and sm.visual.uv is not None and len(sm.visual.uv) == len(sv))
                else np.zeros((len(sv), 2), dtype=np.float32)
            )
            part_im = None
            part_norm_im = None
            part_msk_im = None
            part_rgb = (0.72, 0.72, 0.72)
            is_real_image = False
            is_slot_decal = idx in decal_overlay_vis_set
            is_slot_cutout = idx in cutout_vis_set
            img_key = None
            slot_em_s = 0.0
            slot_em_c = (1.0, 1.0, 1.0)
            mt_idx, mt = _resolve_submesh_material_entry(sm, idx, material_textures)
            if mt is not None:
                raw_em_s = float(mt.get("emission_strength", 0.0) or 0.0)
                mat_nm_l = str(mt.get("material_name") or "").lower()
                is_true_em = bool(
                    mt.get("has_emission_map")
                    or raw_em_s > 1.05
                    or (raw_em_s > 0.01 and any(k in mat_nm_l for k in ("emissive", "glow", "neon", "lamp", "led", "light")))
                )
                slot_em_s = raw_em_s if is_true_em else 0.0
                if mt.get("emission_color") and len(mt["emission_color"]) >= 3:
                    slot_em_c = (float(mt["emission_color"][0]), float(mt["emission_color"][1]), float(mt["emission_color"][2]))
                mt_p = mt.get("texture_path")
                explicit_has_img = mt.get("has_image_texture")
                if mt_p and Path(mt_p).exists():
                    try:
                        log_input_read(mt_p)
                        part_im = Image.open(mt_p).convert("RGBA")
                        arr = np.asarray(part_im, dtype=np.float32) / 255.0
                        part_rgb = (float(arr[:, :, 0].mean()), float(arr[:, :, 1].mean()), float(arr[:, :, 2].mean()))
                        if float(arr[:, :, 3].min()) < 0.96 and not is_baked_opaque_slot and (is_slot_decal or is_slot_cutout) and idx not in metallic_decal_vis_set:
                            is_slot_cutout = True
                            cutout_vis_set.add(idx)
                        elif not is_slot_cutout:
                            # Force opaque alpha on non-cutout slots (e.g. Cycles-baked maps with empty UV gutters)
                            arr_u8 = np.asarray(part_im, dtype=np.uint8).copy()
                            arr_u8[:, :, 3] = 255
                            part_im = Image.fromarray(arr_u8, mode="RGBA")
                        if explicit_has_img is not None:
                            is_real_image = bool(explicit_has_img)
                        else:
                            is_real_image = float(arr[:, :, :3].std()) > 0.002 or is_slot_cutout
                        if is_real_image:
                            src_im_name = str(mt.get("source_image_name") or "").strip()
                            img_key = src_im_name if src_im_name else hashlib.sha1(part_im.resize((64, 64)).tobytes()).hexdigest()
                    except Exception:
                        pass
                if not is_real_image and mt.get("base_color"):
                    bc = mt["base_color"]
                    part_rgb = (float(bc[0]), float(bc[1]), float(bc[2]))
                if slot_em_s > 0.01 and not is_real_image:
                    # Emissive glow slot: bake bright emissive color directly into the slot's quadrant!
                    part_rgb = (
                        max(0.25, min(1.0, slot_em_c[0])),
                        max(0.25, min(1.0, slot_em_c[1])),
                        max(0.25, min(1.0, slot_em_c[2])),
                    )
                    em_u8 = (
                        int(round(part_rgb[0] * 255.0)),
                        int(round(part_rgb[1] * 255.0)),
                        int(round(part_rgb[2] * 255.0)),
                        255,
                    )
                    part_im = Image.new("RGBA", (256, 256), em_u8)
                    part_msk_im = Image.new("RGBA", (256, 256), (0, 250, 255, 255))
                    is_real_image = True
                    img_key = f"__emissive_slot_{idx}_{em_u8[0]}_{em_u8[1]}_{em_u8[2]}"
                norm_p = mt.get("normal_texture_path")
                if norm_p and Path(norm_p).exists():
                    try:
                        log_input_read(norm_p)
                        part_norm_im = Image.open(norm_p).convert("RGBA")
                        any_slot_has_norm = True
                    except Exception:
                        pass
                msk_p = mt.get("mask_texture_path")
                if part_msk_im is None and msk_p and Path(msk_p).exists():
                    try:
                        log_input_read(msk_p)
                        part_msk_im = Image.open(msk_p).convert("RGBA")
                    except Exception:
                        pass
            if part_im is None:
                emb_im, emb_rgb = _extract_submesh_embedded_image(sm)
                if emb_im is not None:
                    part_im = emb_im
                    part_rgb = emb_rgb
                    arr_e = np.asarray(emb_im, dtype=np.float32) / 255.0
                    if float(arr_e[:, :, 3].min()) < 0.96:
                        is_slot_cutout = True
                        cutout_vis_set.add(idx)
                    is_real_image = float(arr_e[:, :, :3].std()) > 0.002 or is_slot_cutout
                    if is_real_image:
                        img_key = hashlib.sha1(emb_im.resize((64, 64)).tobytes()).hexdigest()
                elif mt is None:
                    has_explicit_tint = (
                        "albedo_tint" in prop_entry
                        and isinstance(albedo_tint, (list, tuple))
                        and len(albedo_tint) >= 3
                        and any(abs(float(c) - 1.0) > 1e-3 for c in albedo_tint[:3])
                    )
                    part_rgb = (float(albedo_tint[0]), float(albedo_tint[1]), float(albedo_tint[2])) if has_explicit_tint else emb_rgb
            if is_real_image and img_key:
                distinct_image_keys.add(img_key)
                if first_real_im is None:
                    first_real_im = part_im
            cls_i = slot_pbr_classes[min(mt_idx if mt is not None else idx, 7)]
            distinct_finish_keys.add((
                round(part_rgb[0], 2),
                round(part_rgb[1], 2),
                round(part_rgb[2], 2),
                round(float(cls_i["roughness"]), 2),
                round(float(cls_i["metallic"]), 2),
                round(float(cls_i.get("emission_strength", 0.0)), 1),
            ))
            slot_msk_g_is_roughness = bool(mt.get("msk_g_is_roughness", False)) if mt is not None else False
            slot_msk_g_is_curved_smoothness = bool(mt.get("msk_g_is_curved_smoothness", False)) if mt is not None else False
            part_specs.append({
                "tex_img": part_im if is_real_image else None,
                "avg_rgb": part_rgb,
                "uvs": suv,
                "slot_idx": idx,
                "is_decal": is_slot_decal,
                "is_cutout": is_slot_cutout,
                "has_msk_img": part_msk_im is not None,
                "msk_g_is_roughness": slot_msk_g_is_roughness,
                "msk_g_is_curved_smoothness": slot_msk_g_is_curved_smoothness,
            })
            norm_part_specs.append({
                "tex_img": part_norm_im,
                "avg_rgb": (128.0 / 255.0, 128.0 / 255.0, 1.0),
                "uvs": suv,
                "slot_idx": idx,
                "is_decal": is_slot_decal,
                "is_cutout": is_slot_cutout,
            })
            msk_part_specs.append({
                "tex_img": part_msk_im,
                "avg_rgb": (
                    (1.0, pow(max(0.0, min(1.0, float(cls_i["smoothness"]))), 2.2), 0.0)
                    if is_slot_cutout
                    else (float(cls_i["metallic"]), float(cls_i["smoothness"]), 1.0)
                ),
                "uvs": suv,
                "slot_idx": idx,
                "is_decal": is_slot_decal,
                "is_cutout": is_slot_cutout,
            })

        # Quadrant-remap UVs when >= 2 distinct real image textures exist, OR any slot is cutout/decal, OR when untextured/emissive slots
        # have distinct colors/roughness/emission (e.g. Make Selected Faces Glow on a solid-colored prop)!
        should_pack_quadrants = (
            len(distinct_image_keys) >= 2
            or any(p.get("is_cutout", False) for p in part_specs)
            or (len(distinct_image_keys) == 0 and len(distinct_finish_keys) >= 2 and not prop_entry.get("use_shared_uv_atlas", False))
        )
        if should_pack_quadrants:
            atlas_img, remapped_uvs = build_multi_material_atlas_safe(part_specs, compose_res, preserve_alpha=True)
            uvs = np.vstack(remapped_uvs)
            if any_slot_has_norm:
                norm_atlas_img, _ = build_multi_material_atlas_safe(norm_part_specs, compose_res, preserve_alpha=False)
            msk_atlas_img, _ = build_multi_material_atlas_safe(msk_part_specs, compose_res, preserve_alpha=False)
            if any(p.get("is_cutout", False) for p in part_specs):
                # Proven Method C (1C): Route PNG Alpha of each cutout/decal quadrant into Red & Alpha of msk_atlas_img
                # and curve Green as G = (1 - Roughness)^2.2 (so 0% Rough = 255 Shiny, 50% Rough = 55 Satin, 100% Rough = 0 Matte) with Blue = 0!
                n_p = len(part_specs)
                q_cols = 2 if n_p <= 4 else (3 if n_p <= 9 else 4)
                q_rows = int(math.ceil(n_p / q_cols))
                q_tw = compose_res // q_cols
                q_th = compose_res // q_rows
                c_arr_q = np.asarray(atlas_img.convert("RGBA"), dtype=np.uint8)
                m_arr_q = np.asarray(msk_atlas_img.convert("RGBA"), dtype=np.uint8).copy()
                for q_i, p_sp in enumerate(part_specs):
                    if p_sp.get("is_cutout", False):
                        qx0 = (q_i % q_cols) * q_tw
                        qy0 = (q_i // q_cols) * q_th
                        q_alpha = c_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 3]
                        if p_sp.get("has_msk_img", False) and not p_sp.get("msk_g_is_curved_smoothness", False):
                            g_slice = m_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 1].astype(np.float32) / 255.0
                            s_lin = (1.0 - g_slice) if p_sp.get("msk_g_is_roughness", False) else g_slice
                            s_curv = np.power(np.clip(s_lin, 0.0, 1.0), 2.2)
                            m_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 1] = np.clip(np.round(s_curv * 255.0), 0, 255).astype(np.uint8)
                        m_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 0] = q_alpha
                        m_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 2] = 0
                        m_arr_q[qy0 : qy0 + q_th, qx0 : qx0 + q_tw, 3] = q_alpha
                msk_atlas_img = Image.fromarray(m_arr_q, mode="RGBA")
        elif first_real_im is not None:
            atlas_img = first_real_im

    if norm_atlas_img is None:
        top_norm_p = prop_entry.get("normal_texture_path")
        if top_norm_p and Path(top_norm_p).exists():
            log_input_read(top_norm_p)
            norm_atlas_img = Image.open(top_norm_p).convert("RGBA")
        else:
            for mt in material_textures:
                if isinstance(mt, dict) and mt.get("normal_texture_path") and Path(mt["normal_texture_path"]).exists():
                    log_input_read(mt["normal_texture_path"])
                    norm_atlas_img = Image.open(mt["normal_texture_path"]).convert("RGBA")
                    break

    chosen_msk_g_is_roughness = bool(prop_entry.get("msk_g_is_roughness", False))
    chosen_msk_g_is_curved_smoothness = bool(prop_entry.get("msk_g_is_curved_smoothness", False))
    if msk_atlas_img is None:
        top_msk_p = prop_entry.get("mask_texture_path")
        if top_msk_p and Path(top_msk_p).exists():
            log_input_read(top_msk_p)
            msk_atlas_img = Image.open(top_msk_p).convert("RGBA")
        else:
            chosen_msk_p = None
            for mt in material_textures:
                if isinstance(mt, dict) and mt.get("mask_texture_path") and Path(mt["mask_texture_path"]).exists():
                    if mt.get("has_roughness_map") or mt.get("has_metallic_map"):
                        chosen_msk_p = mt["mask_texture_path"]
                        chosen_msk_g_is_roughness = bool(mt.get("msk_g_is_roughness", False))
                        chosen_msk_g_is_curved_smoothness = bool(mt.get("msk_g_is_curved_smoothness", False))
                        break
                    elif chosen_msk_p is None:
                        chosen_msk_p = mt["mask_texture_path"]
                        chosen_msk_g_is_roughness = bool(mt.get("msk_g_is_roughness", False))
                        chosen_msk_g_is_curved_smoothness = bool(mt.get("msk_g_is_curved_smoothness", False))
            if chosen_msk_p:
                log_input_read(chosen_msk_p)
                msk_atlas_img = Image.open(chosen_msk_p).convert("RGBA")
            else:
                cls0 = slot_pbr_classes[0]
                m_u8 = int(max(0, min(255, round(float(cls0["metallic"]) * 255.0))))
                s_u8 = int(max(0, min(255, round(float(cls0["smoothness"]) * 255.0))))
                msk_atlas_img = Image.new("RGBA", (target_tex_res, target_tex_res), (m_u8, s_u8, 255, 255))
                chosen_msk_g_is_roughness = False
                chosen_msk_g_is_curved_smoothness = False

    if atlas_img is None:
        if tex_path and Path(tex_path).exists():
            log_input_read(tex_path)
            atlas_img = Image.open(tex_path).convert("RGBA")
        elif material_textures and material_textures[0].get("texture_path") and Path(material_textures[0]["texture_path"]).exists():
            log_input_read(material_textures[0]["texture_path"])
            atlas_img = Image.open(material_textures[0]["texture_path"]).convert("RGBA")
        elif submeshes:
            emb_im, emb_rgb = _extract_submesh_embedded_image(submeshes[vis_indices[0]])
            if emb_im is not None:
                atlas_img = emb_im
            else:
                has_explicit_tint = (
                    "albedo_tint" in prop_entry
                    and isinstance(albedo_tint, (list, tuple))
                    and len(albedo_tint) >= 3
                    and any(abs(float(c) - 1.0) > 1e-3 for c in albedo_tint[:3])
                )
                chosen_rgb = [float(c) for c in albedo_tint[:3]] if has_explicit_tint else emb_rgb
                rgb_u8 = [int(max(0, min(255, round(c * 255)))) for c in chosen_rgb]
                atlas_img = Image.new("RGBA", (target_tex_res, target_tex_res), (rgb_u8[0], rgb_u8[1], rgb_u8[2], 255))
        else:
            atlas_img = Image.new("RGBA", (target_tex_res, target_tex_res), (180, 180, 180, 255))

    # Determine final power-of-two (width, height): preserve non-square power-of-two aspect ratios (e.g. 256x4096) on single-atlas props
    src_aw, src_ah = int(atlas_img.size[0]), int(atlas_img.size[1])
    max_src_a = max(src_aw, src_ah)
    max_cap_res = max(target_tex_res, 4096 if max_src_a >= 3072 else (2048 if max_src_a >= 1536 else 1024))
    if not should_pack_quadrants and src_aw != src_ah:
        scale_f = min(1.0, float(max_cap_res) / float(max(1, max_src_a)))
        target_tex_w = _snap_to_power_of_two(int(round(src_aw * scale_f)), 64, max_cap_res)
        target_tex_h = _snap_to_power_of_two(int(round(src_ah * scale_f)), 64, max_cap_res)
    else:
        target_tex_w = target_tex_res
        target_tex_h = target_tex_res

    if atlas_img.size != (target_tex_w, target_tex_h):
        atlas_img = atlas_img.resize((target_tex_w, target_tex_h), Image.Resampling.LANCZOS)
    if msk_atlas_img.size != (target_tex_w, target_tex_h):
        msk_atlas_img = msk_atlas_img.resize((target_tex_w, target_tex_h), Image.Resampling.LANCZOS)

    # Detect single-material / all-cutout `0x273` props vs. multi-material split Opaque (`Sec[0]` `0x771`) + Cutout (`Sec[1]` `0x273`) props
    has_mat_alpha_flag = any(
        isinstance(mt, dict)
        and (
            bool(mt.get("is_decal_slot", False))
            or str(mt.get("domain") or "").lower() == "decal"
            or (
                (bool(mt.get("has_alpha_transparency", False)) or str(mt.get("alpha") or "").lower() in ("masked", "clip", "blend"))
                and not any(
                    k in str(mt.get("source_image_name") or "").lower()
                    for k in ("_basecolor", "_ambientocclusion", "_roughness", "_metallic", "_normal", "reskate_atlas_", "reskate_baked_")
                )
            )
        )
        for mt in material_textures
    )
    alpha_min_u8 = int(np.asarray(atlas_img.convert("RGBA"), dtype=np.uint8)[:, :, 3].min())
    all_vis_cutout = bool(vis_indices) and all(idx in cutout_vis_set for idx in vis_indices)
    some_vis_cutout = bool(vis_indices) and any(idx in cutout_vis_set for idx in vis_indices) and not all_vis_cutout

    has_multi_mat_cutout_split = bool(
        len(vis_indices) > 1
        and some_vis_cutout
        and ("b421_ms" in tpl)
    )
    cutout_slot_idx = next((idx for idx in vis_indices if idx in cutout_vis_set), 0)

    use_osk_alpha_shader = bool(
        not has_multi_mat_cutout_split
        and prop_decal_shader_mode != "METALLIC_771"
        and not bool(metallic_decal_vis_set)
        and (
            exp_cfg.get("is_surface_decal", False)
            or all_vis_cutout
            or (
                len(vis_indices) == 1
                and (
                    (len(faces) <= 4 and "decal" in display_name.lower())
                    or has_mat_alpha_flag
                )
            )
        )
    )
    if use_osk_alpha_shader:
        atlas_img, msk_atlas_img, _ = _prepare_alpha_opacity_decal_textures(
            atlas_img,
            msk_atlas_img,
            msk_g_is_roughness=chosen_msk_g_is_roughness,
            msk_g_is_curved_smoothness=chosen_msk_g_is_curved_smoothness,
        )
        vc_layers = np.zeros(len(verts), dtype=np.uint8)
    elif not has_multi_mat_cutout_split and alpha_min_u8 < 255:
        # Solid prop: ensure 100% opaque alpha channel on atlas_img
        c_arr_solid = np.asarray(atlas_img.convert("RGBA"), dtype=np.uint8).copy()
        c_arr_solid[:, :, 3] = 255
        atlas_img = Image.fromarray(c_arr_solid, mode="RGBA")

    if has_multi_mat_cutout_split:
        opaque_f_list = []
        cutout_f_list = []
        f_cursor = 0
        for idx in vis_indices:
            n_f_sub = len(submeshes[idx].faces)
            f_slice = faces[f_cursor : f_cursor + n_f_sub]
            f_cursor += n_f_sub
            if idx in cutout_vis_set:
                cutout_f_list.append(f_slice)
            else:
                opaque_f_list.append(f_slice)
        opaque_faces_arr = np.vstack(opaque_f_list) if opaque_f_list else np.zeros((0, 3), dtype=np.int32)
        cutout_faces_arr = np.vstack(cutout_f_list) if cutout_f_list else np.zeros((0, 3), dtype=np.int32)
        new_ms_b39, new_mc_b39, new_chk_b39, _, _ = compile_split_opaque_and_cutout_mesh_into_b421(
            tpl["b421_ms"],
            tpl["ebx28"],
            verts,
            norms,
            uvs,
            opaque_faces_arr,
            cutout_faces_arr,
            flip_normals=False,
            vc_layers=vc_layers,
        )
        print(
            f"  [Multi-Material Cutout Split] Compiled {len(opaque_faces_arr)} opaque tris into Sec[0] (0x771 PBR) "
            f"+ {len(cutout_faces_arr)} cutout/decal tris into Sec[1] (0x273 _osk Cutout).",
            flush=True,
        )
    else:
        # Compile visual mesh into Bundle 39 template (with preserved 1:1 UVs or multi-material quadrant UVs, and per-submesh vc_layers)
        new_ms_b39, new_mc_b39, new_chk_b39, _, _ = compile_mesh_into_donor(
            tpl["res11"],
            tpl["ebx28"],
            verts,
            norms,
            uvs,
            faces,
            vc_layer=0,
            active_sec=0,
            flip_normals=False,
            vc_layers=vc_layers,
        )

    atlas_arr = np.asarray(atlas_img, dtype=np.float32) / 255.0
    avg_rgb = (float(atlas_arr[:, :, 0].mean()), float(atlas_arr[:, :, 1].mean()), float(atlas_arr[:, :, 2].mean()))

    # 4. Thumbnails (256x256 & 768x768) & Parallel BC7 Mipchain Encoding
    def _load_valid_thumb_or_none(p_str, target_sz: int):
        if not p_str:
            return None
        p_obj = Path(p_str)
        if not p_obj.exists() or not p_obj.is_file():
            return None
        try:
            log_input_read(p_obj)
            im = Image.open(p_obj).convert("RGBA")
            arr = np.asarray(im, dtype=np.float32)
            # Reject solid flat fallback placeholders (e.g. 859B / 4028B solid (40,45,55,255) with std < 0.5)
            if float(arr[:, :, :3].std()) < 0.5 and float(arr[:, :, 3].std()) < 0.5:
                return None
            return im.resize((target_sz, target_sz), Image.Resampling.LANCZOS)
        except Exception:
            return None

    img256 = _load_valid_thumb_or_none(prop_entry.get("thumb_256_path"), 256)
    img768 = _load_valid_thumb_or_none(prop_entry.get("thumb_768_path"), 768)
    if img256 is None or img768 is None:
        thumb_fallback = render_fallback_thumbnail_png(verts, norms, faces, avg_rgb, size=768)
        if img256 is None:
            img256 = thumb_fallback.resize((256, 256), Image.Resampling.LANCZOS)
            if prop_entry.get("thumb_256_path"):
                try:
                    img256.save(prop_entry["thumb_256_path"], "PNG")
                except Exception:
                    pass
        if img768 is None:
            img768 = thumb_fallback
            if prop_entry.get("thumb_768_path"):
                try:
                    img768.save(prop_entry["thumb_768_path"], "PNG")
                except Exception:
                    pass

    tex_w_b39, tex_h_b39, _n_mips_b39, mip_sizes_b39, _total_sz_b39 = _get_bc7_mip_spec(target_tex_w, target_tex_h)
    if norm_atlas_img is not None and norm_atlas_img.size != (tex_w_b39, tex_h_b39):
        norm_atlas_img = norm_atlas_img.resize((tex_w_b39, tex_h_b39), Image.Resampling.LANCZOS)
    if msk_atlas_img.size != (tex_w_b39, tex_h_b39):
        msk_atlas_img = msk_atlas_img.resize((tex_w_b39, tex_h_b39), Image.Resampling.LANCZOS)

    # Optional 2x2m Detail Shader (disabled by default; when True, blends preset into _n and _msk without touching _c Albedo)
    if use_2x2m_detail_shader:
        norm_atlas_img, msk_atlas_img = _apply_2x2m_detail_shader_overlay(
            norm_atlas_img, msk_atlas_img, detail_2x2m_preset, detail_2x2m_intensity, tile_scale_m=detail_2x2m_scale
        )
        if norm_atlas_img is not None and norm_atlas_img.size != (tex_w_b39, tex_h_b39):
            norm_atlas_img = norm_atlas_img.resize((tex_w_b39, tex_h_b39), Image.Resampling.LANCZOS)
        if msk_atlas_img.size != (tex_w_b39, tex_h_b39):
            msk_atlas_img = msk_atlas_img.resize((tex_w_b39, tex_h_b39), Image.Resampling.LANCZOS)
        if use_osk_alpha_shader or has_multi_mat_cutout_split:
            # Re-assert Opacity in Red channel of _msk after 2x2m detail overlay
            m_arr_osk = np.asarray(msk_atlas_img.convert("RGBA"), dtype=np.uint8).copy()
            c_alpha_u8 = np.asarray(atlas_img.convert("RGBA"), dtype=np.uint8)[:, :, 3]
            if use_osk_alpha_shader:
                m_arr_osk[:, :, 0] = c_alpha_u8
                m_arr_osk[:, :, 2] = 0
                m_arr_osk[:, :, 3] = c_alpha_u8
            else:
                cut_mask = c_alpha_u8 < 255
                m_arr_osk[:, :, 0][cut_mask] = c_alpha_u8[cut_mask]
                m_arr_osk[:, :, 2][cut_mask] = 0
                m_arr_osk[:, :, 3][cut_mask] = c_alpha_u8[cut_mask]
            msk_atlas_img = Image.fromarray(m_arr_osk, mode="RGBA")

    # Pack-Level Texture Deduplication: Check pack cache before encoding BC7
    atlas_bytes = atlas_img.tobytes() if atlas_img is not None else b""
    atlas_sha = (hashlib.sha1(atlas_bytes).hexdigest() + f"_{tex_w_b39}x{tex_h_b39}") if atlas_bytes else ""

    norm_bytes = norm_atlas_img.tobytes() if norm_atlas_img is not None else b""
    norm_sha = (hashlib.sha1(norm_bytes).hexdigest() + f"_{tex_w_b39}x{tex_h_b39}") if norm_bytes else ""

    msk_bytes = msk_atlas_img.tobytes() if msk_atlas_img is not None else b""
    msk_sha = (hashlib.sha1(msk_bytes).hexdigest() + f"_{tex_w_b39}x{tex_h_b39}") if msk_bytes else ""

    tex_bc7_b39 = None
    norm_bc7_b39 = None
    msk_bc7_b39 = None
    tex_chk_guid_cached = None
    norm_chk_guid_cached = None
    msk_chk_guid_cached = None

    with _PACK_TEXTURE_LOCK:
        if atlas_sha and atlas_sha in _PACK_TEXTURE_CACHE:
            tex_bc7_b39 = _PACK_TEXTURE_CACHE[atlas_sha]["bc7"]
            tex_chk_guid_cached = _PACK_TEXTURE_CACHE[atlas_sha]["guid"]
            print(f"  [Texture Dedup] Reused BC7 Albedo ({tex_w_b39}x{tex_h_b39}) from pack cache: {atlas_sha[:8]}", flush=True)

        if norm_sha and norm_sha in _PACK_TEXTURE_CACHE:
            norm_bc7_b39 = _PACK_TEXTURE_CACHE[norm_sha]["bc7"]
            norm_chk_guid_cached = _PACK_TEXTURE_CACHE[norm_sha]["guid"]
            print(f"  [Texture Dedup] Reused BC7 Normal ({tex_w_b39}x{tex_h_b39}) from pack cache: {norm_sha[:8]}", flush=True)

        if msk_sha and msk_sha in _PACK_TEXTURE_CACHE:
            msk_bc7_b39 = _PACK_TEXTURE_CACHE[msk_sha]["bc7"]
            msk_chk_guid_cached = _PACK_TEXTURE_CACHE[msk_sha]["guid"]
            print(f"  [Texture Dedup] Reused BC7 Mask ({tex_w_b39}x{tex_h_b39}) from pack cache: {msk_sha[:8]}", flush=True)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as bc7_pool:
        f_tex = bc7_pool.submit(encode_rgba_to_bc7_mipchain, atlas_img, tex_w_b39, tex_h_b39, mip_sizes_b39) if tex_bc7_b39 is None else None
        f_norm = bc7_pool.submit(encode_rgba_to_bc7_mipchain, norm_atlas_img, tex_w_b39, tex_h_b39, mip_sizes_b39) if (norm_atlas_img is not None and norm_bc7_b39 is None) else None
        f_msk = bc7_pool.submit(encode_rgba_to_bc7_mipchain, msk_atlas_img, tex_w_b39, tex_h_b39, mip_sizes_b39) if msk_bc7_b39 is None else None
        f_t256 = bc7_pool.submit(encode_rgba_to_bc7_mipchain, img256, 256, 256, [65536])
        f_t768 = bc7_pool.submit(encode_rgba_to_bc7_mipchain, img768, 768, 768, [589824])

        if f_tex is not None:
            tex_bc7_b39 = f_tex.result()
            tex_chk_guid_cached = make_chunk_guid("tex_chk", atlas_sha or stem, prefix_2b=b"\x9e\x8f")
            if atlas_sha:
                with _PACK_TEXTURE_LOCK:
                    _PACK_TEXTURE_CACHE[atlas_sha] = {"bc7": tex_bc7_b39, "guid": tex_chk_guid_cached}

        if f_norm is not None:
            norm_bc7_b39 = f_norm.result()
            norm_chk_guid_cached = make_chunk_guid("norm_chk", norm_sha or stem, prefix_2b=b"\x60\x95")
            if norm_sha:
                with _PACK_TEXTURE_LOCK:
                    _PACK_TEXTURE_CACHE[norm_sha] = {"bc7": norm_bc7_b39, "guid": norm_chk_guid_cached}

        if f_msk is not None:
            msk_bc7_b39 = f_msk.result()
            msk_chk_guid_cached = make_chunk_guid("msk_chk", msk_sha or stem, prefix_2b=b"\x7e\x1c")
            if msk_sha:
                with _PACK_TEXTURE_LOCK:
                    _PACK_TEXTURE_CACHE[msk_sha] = {"bc7": msk_bc7_b39, "guid": msk_chk_guid_cached}

        thumb_sm_bc7 = f_t256.result()
        thumb_lg_bc7 = f_t768.result()

    # 5. Havok Collision (continuous welded round rails + mitered grind prisms with bit-19 IncludeInSurfaceAnalysis)
    emit_prop_substep(prop_idx, prop_total, display_name, 3, "Building Havok Collision & Grind Splines...")
    raw_proxy_phys, _, _ = fit_havok_polytope_to_bounds(tpl["res16"], np.array(bounds_min), np.array(bounds_max), surface_code)
    pphys_bytes_list: list[bytes] = []
    pphys_glb_path = prop_entry.get("pphys_glb_path")
    col_glb_path = prop_entry.get("col_glb_path")
    pphys_target_tris = int(prop_entry.get("pphys_target_tris", 64) or 64)
    proxy_src_glb = None
    if pphys_glb_path and Path(pphys_glb_path).exists() and placement_proxy_mode in ("CUSTOM_OBJECT", "TIGHT_SIMPLIFIED", "BOUNDING_BOX"):
        proxy_src_glb = pphys_glb_path
    elif placement_proxy_mode in ("MATCH_SKATE_COL", "MATCH_COLLISION", "TIGHT_SIMPLIFIED", "EXACT_1_1", "EXACT_MESH", "SAME_AS_MESH") and col_glb_path and Path(col_glb_path).exists():
        proxy_src_glb = col_glb_path

    p_v = verts
    p_f = faces
    pphys_submeshes = []
    if proxy_src_glb:
        try:
            log_input_read(proxy_src_glb)
            loaded_pv, _, _, loaded_pf, loaded_psubs = load_mesh_geometry_and_submeshes(proxy_src_glb)
            if len(loaded_pv) > 0:
                loaded_pv, loaded_psubs = _align_collision_mesh_to_visual_bounds(
                    loaded_pv, loaded_psubs, bounds_min, bounds_max, label="placement proxy"
                )
                p_v = loaded_pv
                p_f = loaded_pf
                pphys_submeshes = loaded_psubs or []
                raw_proxy_phys, _, _ = fit_havok_polytope_to_bounds(
                    tpl["res16"], p_v.min(axis=0), p_v.max(axis=0), surface_code
                )
        except Exception as pphys_err:
            print(f"  [!] Custom Placement Proxy load fallback to visual mesh: {pphys_err}", flush=True)

    all_vis_decal_overlay = bool(vis_indices) and all(idx in decal_overlay_vis_set for idx in vis_indices)
    no_col_placement_mode = str(
        prop_entry.get("no_collision_placement_mode")
        or exp_cfg.get("no_collision_placement_mode")
        or "FLAT_PLANE"
    ).strip().upper()
    no_skater_col_flag = (
        col_mode in ("NO_COLLISION", "NONE", "DECAL_ONLY")
        or bool(exp_cfg.get("no_skater_collision", False))
        or bool(exp_cfg.get("is_surface_decal", False))
        or all_vis_decal_overlay
    )
    is_pack_icon_prop = bool(prop_entry.get("is_pack_icon_prop", False))
    if len(p_v) >= 4:
        p_min = p_v.min(axis=0).copy()
        p_max = np.maximum(p_v.max(axis=0), p_min + 0.025)
        if is_pack_icon_prop:
            # 30cm tall (Y = 0.00m .. +0.30m) 3D placement proxy box so the flat Pack Icon tile is effortless to place & grab!
            p_min = np.array([-0.30, 0.00, -0.30], dtype=np.float32)
            p_max = np.array([+0.30, +0.30, +0.30], dtype=np.float32)
        elif no_skater_col_flag and no_col_placement_mode != "MATCH_MESH":
            # Flat Ground Plane (Floor Decal / Rug): clamp Quick Drop placement proxy (_pphys / res16)
            # to a flat 6cm ground slab spanning the X*Z footprint so even tall 3D props in FLAT_PLANE mode
            # use a flat ground footprint for Quick Drop placement!
            p_min[1] = -0.005
            p_max[1] = 0.055
        raw_proxy_phys, _, _ = fit_havok_polytope_to_bounds(tpl["res16"], p_min, p_max, surface_code)
    pphys_bytes_list = []
    skate_bounds_min = list(bounds_min)
    if anti_lip_enabled and anti_lip_sink_m > 0.0:
        skate_bounds_min[1] = float(bounds_min[1]) - float(anti_lip_sink_m)
    raw_phys_fitted, _, _ = fit_havok_polytope_to_bounds(tpl["res17"], np.array(skate_bounds_min), np.array(bounds_max), surface_code)

    grind_col_shapes = []
    grind_paths = prop_entry.get("grind_paths") or []
    # Only generate physical grind tube prisms if explicitly requested in ReSkate Lab (Exp);
    # in Skate, grinding is driven directly by the collision mesh's dihedral edge angles and MaterialDecl flags.
    if bool(exp_cfg.get("compile_experimental_grind_tubes", False)):
        for gp in grind_paths:
            raw_segs = gp.get("segments") or []
            if not raw_segs:
                continue
            is_z_up = bool(gp.get("z_up", True))
            yup_segs = []
            for seg in raw_segs:
                if len(seg) >= 2:
                    p0, p1 = seg[0], seg[1]
                    if is_z_up:
                        yup_segs.append(([float(p0[0]), float(p0[2]), -float(p0[1])], [float(p1[0]), float(p1[2]), -float(p1[1])]))
                    else:
                        yup_segs.append(([float(p0[0]), float(p0[1]), float(p0[2])], [float(p1[0]), float(p1[1]), float(p1[2])]))
            if not yup_segs:
                continue
            gp_rad = max(0.012, float(gp.get("radius", 0.032)))
            prism_tm = build_tube_prism_from_segments(yup_segs, radius=gp_rad)
            if prism_tm is not None and len(prism_tm.faces) > 0:
                gp_surf = str(gp.get("surface_id") or "Metal_Solid_Grindable")
                gp_code = int(gp.get("surface_code", 352))
                gp_type = str(gp.get("type", "LEDGE")).upper()
                if gp_surf == "No_Grind":
                    gp_packed = apply_grind_mode_to_packed(gp_code, "no_grind")
                    is_rr = False
                elif gp_type == "ROUND_RAIL":
                    gp_packed = apply_grind_mode_to_packed(gp_code, "round_rail")
                    is_rr = True
                else:
                    gp_packed = apply_grind_mode_to_packed(gp_code, "round_rail") if ((gp_code >> 19) & 0x1FFF) == 0 else gp_code
                    is_rr = False
                grind_col_shapes.append({
                    "mesh": prism_tm,
                    "mode": "triangle_mesh",
                    "surface_id": gp_surf,
                    "packed": gp_packed,
                    "round_rail": is_rr,
                    "grind_curve": False,
                })

    base_col_shapes = []
    force_exact_flag = False

    # Filter out any Decal Overlay slots (`Treat Material as Decal Overlay` / `domain == "decal"`) from auto-generated skater collision
    non_decal_vis_indices = [idx for idx in vis_indices if idx not in decal_overlay_vis_set]
    col_auto_faces = faces
    if decal_overlay_vis_set and non_decal_vis_indices:
        kept_f_slices = []
        f_cur = 0
        for idx in vis_indices:
            n_f_s = len(submeshes[idx].faces)
            if idx not in decal_overlay_vis_set:
                kept_f_slices.append(faces[f_cur : f_cur + n_f_s])
            f_cur += n_f_s
        if kept_f_slices:
            col_auto_faces = np.vstack(kept_f_slices)

    no_skater_col = (
        col_mode in ("NO_COLLISION", "NONE", "DECAL_ONLY")
        or bool(exp_cfg.get("no_skater_collision", False))
        or bool(exp_cfg.get("is_surface_decal", False))
        or all_vis_decal_overlay
    )
    if is_pack_icon_prop:
        # Pack Icon Tile (#1 Quick Drop Tab Icon prop):
        # Build _phys (res17 / ebx31) as a 30cm tall (0.60m x 0.30m x 0.60m, Y = 0.00m .. +0.30m) No_Grind 3D box
        # so players can easily place it down and pick it back up in Quick Drop without paper-thin raycast issues!
        icon_grab_tm = trimesh.creation.box(extents=[0.60, 0.30, 0.60])
        icon_grab_tm.apply_translation([0.0, 0.15, 0.0])
        degen_packed = apply_grind_mode_to_packed(surface_code, "no_grind")
        base_col_shapes = [{
            "mesh": icon_grab_tm,
            "mode": "convex_hull",
            "surface_id": "No_Grind",
            "packed": degen_packed,
            "round_rail": False,
        }]
        grind_col_shapes = []
        force_exact_flag = False
        raw_phys_fitted, _, _ = fit_havok_polytope_to_bounds(
            tpl["res17"],
            np.array([-0.30, 0.00, -0.30], dtype=np.float32),
            np.array([+0.30, +0.30, +0.30], dtype=np.float32),
            degen_packed,
        )
        print(
            "  [Collision] Pack Icon #1 Tile: 30cm tall (0.60x0.30x0.60m) Quick Drop placement & grab proxy enabled.",
            flush=True,
        )
    elif no_skater_col:
        # Quick Drop's "Select / Grab Placed Prop" cursor raycast tests _phys (res17 / ebx31),
        # while active ghost dragging tests _pphys (res16 / ebx32).
        # Build _phys as a wafer-thin +2mm (0.002m) No_Grind ground sheet spanning the decal's X*Z footprint
        # (Y = -0.005m .. +0.002m): +2mm is hit by Quick Drop's selection raycast before the Y=0 ground so the
        # placed decal is 100% grabbable/movable, while remaining 8x thinner than a skateboard wheel axle nut
        # so the skateboard rolls straight over it without any lip bump or snag!
        dec_xmin = float(bounds_min[0])
        dec_xmax = max(float(bounds_max[0]), dec_xmin + 0.10)
        dec_zmin = float(bounds_min[2])
        dec_zmax = max(float(bounds_max[2]), dec_zmin + 0.10)
        dec_ext_x = max(0.10, dec_xmax - dec_xmin)
        dec_ext_z = max(0.10, dec_zmax - dec_zmin)
        dec_cx = 0.5 * (dec_xmin + dec_xmax)
        dec_cz = 0.5 * (dec_zmin + dec_zmax)
        # Y spans -0.005m to +0.002m (thickness = 0.007m, center Y = -0.0015m)
        wafer_tm = trimesh.creation.box(extents=[dec_ext_x, 0.007, dec_ext_z])
        wafer_tm.apply_translation([dec_cx, -0.0015, dec_cz])
        degen_packed = apply_grind_mode_to_packed(surface_code, "no_grind")
        base_col_shapes = [{
            "mesh": wafer_tm,
            "mode": "convex_hull",
            "surface_id": "No_Grind",
            "packed": degen_packed,
            "round_rail": False,
        }]
        grind_col_shapes = []
        force_exact_flag = False
        raw_phys_fitted, _, _ = fit_havok_polytope_to_bounds(
            tpl["res17"],
            np.array([dec_xmin, -0.005, dec_zmin], dtype=np.float32),
            np.array([dec_xmax, +0.002, dec_zmax], dtype=np.float32),
            degen_packed,
        )
        print(
            f"  [Collision] No Skater Collision Wafer Sheet (Y=-5mm..+2mm, {dec_ext_x:.2f}x{dec_ext_z:.2f}m, "
            f"placement={no_col_placement_mode}): Quick Drop selection raycast enabled + zero skate lip.",
            flush=True,
        )
    elif col_mode in ("CUSTOM_OBJECT", "AUTHORED_COL") or (
        bool(prop_entry.get("col_thickened", False))
        and col_glb_path
        and Path(col_glb_path).exists()
        and col_mode not in ("SIMPLE_HULLS", "CONVEX_HULL")
    ):
        if not col_glb_path or not Path(col_glb_path).exists():
            raise RuntimeError(
                f"Collision mode is '{col_mode}', but custom collision GLB is missing or was not exported: {col_glb_path}"
            )
        col_all_v, _, _, _, col_submeshes = load_mesh_geometry_and_submeshes(col_glb_path)
        if not bool(prop_entry.get("col_thickened", False)):
            _, col_submeshes = _align_collision_mesh_to_visual_bounds(
                col_all_v, col_submeshes, bounds_min, bounds_max, label="custom collision"
            )
        n_col_subs = max(1, min(len(col_submeshes), 12))
        safe_total_tris = max(500, min(max_col_tris, 10000))
        slot_max = max(150, safe_total_tris // n_col_subs)
        for idx, c_tm in enumerate(col_submeshes[:n_col_subs]):
            welded_c_tm, prot_c_mask = _weld_and_sharpen_collision_for_grind_paths(c_tm, grind_paths)
            work_tm = decimate_trimesh_fast(welded_c_tm, slot_max, protected_mask=prot_c_mask)
            slot_packed = surface_code
            _, ms_ent = _resolve_submesh_material_entry(c_tm, idx, material_surfaces)
            if ms_ent is not None and ms_ent.get("packed"):
                slot_packed = int(ms_ent["packed"])
            if round_rail:
                slot_packed = apply_grind_mode_to_packed(slot_packed, "round_rail")
            elif grind_paths and any(str(gp.get("surface_id", "")) != "No_Grind" for gp in grind_paths if isinstance(gp, dict)):
                # Ensure marked grindable paths have IncludeInSurfaceAnalysis (PropertySlot 71) when not Do-Not-Align
                if ((slot_packed >> 19) & 0x1FFF) == 0:
                    slot_packed = apply_grind_mode_to_packed(slot_packed, "round_rail")
            base_col_shapes.append({
                "mesh": work_tm,
                "mode": "triangle_mesh",
                "surface_id": surface_id,
                "packed": slot_packed,
                "round_rail": round_rail,
                "grind_curve": round_rail or bool(grind_paths),
            })
        force_exact_flag = True

    elif col_mode in ("SIMPLE_HULLS", "CONVEX_HULL") and not round_rail:
        if col_glb_path and Path(col_glb_path).exists():
            c_verts, _, _, c_faces, _ = load_mesh_geometry_and_submeshes(col_glb_path)
            c_verts, _ = _align_collision_mesh_to_visual_bounds(
                c_verts, None, bounds_min, bounds_max, label="custom hull collision"
            )
            tm_full = trimesh.Trimesh(vertices=c_verts, faces=c_faces, process=True)
        else:
            tm_full = trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)
        hull_budget = max(1, min(12, max_hulls))
        hulls = decompose_mesh_into_hulls(tm_full, max_hulls=hull_budget)
        for h_tm in hulls:
            slot_packed = apply_grind_mode_to_packed(surface_code, "round_rail") if round_rail else surface_code
            base_col_shapes.append({
                "mesh": h_tm,
                "mode": "convex_hull",
                "surface_id": surface_id,
                "packed": slot_packed,
                "round_rail": round_rail,
            })
        force_exact_flag = False

    else:
        # Default: SAME_AS_MESH / EXACT_MESH / EXACT_1_1 (or round_rail continuous welded mesh)
        safe_total_tris = max(500, min(max_col_tris, 10000))
        active_col_sub_indices = [idx for idx in range(len(submeshes)) if idx in non_decal_vis_indices][:12]
        if len(active_col_sub_indices) > 1 and len(material_surfaces) > 1 and not round_rail:
            n_subs = max(1, len(active_col_sub_indices))
            slot_max = max(150, safe_total_tris // n_subs)
            for idx in active_col_sub_indices:
                sub_tm = submeshes[idx]
                welded_sub, prot_sub = _weld_and_sharpen_collision_for_grind_paths(sub_tm, grind_paths)
                work_tm = decimate_trimesh_fast(welded_sub, slot_max, protected_mask=prot_sub)
                slot_packed = surface_code
                _, ms_ent = _resolve_submesh_material_entry(sub_tm, idx, material_surfaces)
                if ms_ent is not None and ms_ent.get("packed"):
                    slot_packed = int(ms_ent["packed"])
                if round_rail:
                    slot_packed = apply_grind_mode_to_packed(slot_packed, "round_rail")
                elif grind_paths and ((slot_packed >> 19) & 0x1FFF) == 0:
                    slot_packed = apply_grind_mode_to_packed(slot_packed, "round_rail")
                base_col_shapes.append({
                    "mesh": work_tm,
                    "mode": "triangle_mesh",
                    "surface_id": surface_id,
                    "packed": slot_packed,
                    "round_rail": round_rail,
                    "grind_curve": round_rail or bool(grind_paths),
                })
        else:
            raw_tm = trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=False)
            welded_tm, prot_mask = _weld_and_sharpen_collision_for_grind_paths(raw_tm, grind_paths)
            tm_exact = decimate_trimesh_fast(welded_tm, safe_total_tris, protected_mask=prot_mask)
            base_packed = surface_code
            if len(material_surfaces) == 1 and material_surfaces[0].get("surface") not in (None, "DEFAULT") and material_surfaces[0].get("packed"):
                base_packed = int(material_surfaces[0]["packed"])
            has_active_grind = round_rail or bool(
                grind_paths and any(str(gp.get("surface_id", "")) != "No_Grind" for gp in grind_paths if isinstance(gp, dict))
            )
            slot_packed = apply_grind_mode_to_packed(base_packed, "round_rail") if (has_active_grind and ((base_packed >> 19) & 0x1FFF) == 0) else base_packed
            base_col_shapes.append({
                "mesh": tm_exact,
                "mode": "triangle_mesh",
                "surface_id": surface_id,
                "packed": slot_packed,
                "round_rail": round_rail,
                "grind_curve": has_active_grind,
            })
        force_exact_flag = True

    if grind_paths and not round_rail and not no_skater_col and base_col_shapes:
        base_col_shapes, did_split_grind = _partition_collision_shapes_by_grind_paths(
            base_col_shapes,
            grind_paths,
            default_packed=surface_code,
            max_total_shapes=max(1, 12 - len(grind_col_shapes)),
        )
        if did_split_grind:
            force_exact_flag = True

    face_surface_groups = prop_entry.get("face_surface_groups") or []
    if face_surface_groups and not no_skater_col and base_col_shapes:
        base_col_shapes, did_split_faces = _partition_collision_shapes_by_face_surfaces(
            base_col_shapes,
            face_surface_groups,
            default_packed=surface_code,
            round_rail=round_rail,
            max_total_shapes=max(1, 12 - len(grind_col_shapes)),
        )
        if did_split_faces:
            force_exact_flag = True

    all_col_shapes = (base_col_shapes + grind_col_shapes)[:12]
    if no_skater_col and no_col_placement_mode == "MATCH_MESH":
        unsunk_proxy_meshes = [trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)]
    else:
        unsunk_proxy_meshes = [
            s["mesh"].copy()
            for s in base_col_shapes
            if s.get("mesh") is not None and hasattr(s["mesh"], "faces") and len(s["mesh"].faces) > 0
        ]

    # Ramp Anti-Lip: sink bottom vertices (v_y <= global_min_y + 0.015) of skate collision shapes by -anti_lip_sink_m
    # while keeping Placement Proxy (raw_proxy_phys / res16) and bounds_min flush at 0.0m!
    if anti_lip_enabled and anti_lip_sink_m > 0.0 and not no_skater_col and all_col_shapes:
        col_min_y = min(
            float(np.asarray(s["mesh"].vertices)[:, 1].min())
            for s in all_col_shapes
            if hasattr(s.get("mesh"), "vertices") and len(s["mesh"].vertices) > 0
        )
        lip_band_top = col_min_y + 0.015
        for s in all_col_shapes:
            tm_s = s.get("mesh")
            if tm_s is not None and hasattr(tm_s, "vertices") and len(tm_s.vertices) > 0:
                tm_copy = tm_s.copy()
                v_arr = np.asarray(tm_copy.vertices, dtype=np.float64).copy()
                lip_mask = v_arr[:, 1] <= lip_band_top
                if np.any(lip_mask):
                    v_arr[lip_mask, 1] -= float(anti_lip_sink_m)
                    tm_copy.vertices = v_arr
                    s["mesh"] = tm_copy
        print(
            f"  [Anti-Lip] Sunk bottom skate collision lip by -{anti_lip_sink_m * 1000.0:.1f}mm "
            f"while keeping Quick Drop Placement Proxy flush at 0.0m.",
            flush=True,
        )

    if exp_cfg.get("vfx_emitters"):
        print(f"  [ReSkate Lab] Attached {len(exp_cfg['vfx_emitters'])} VFX emitter(s) to {display_name}", flush=True)
    if isinstance(exp_cfg.get("music_emitter"), dict) and exp_cfg["music_emitter"].get("enabled"):
        me = exp_cfg["music_emitter"]
        print(
            f"  [ReSkate Lab] Attached Music/Boombox Emitter ({me.get('preset', 'AMBIENT_BOOMBOX_LOOP')}, "
            f"r={float(me.get('radius_m', 15.0)):.1f}m) to {display_name}",
            flush=True,
        )

    shape_packed_list = [int(s["packed"]) for s in all_col_shapes]

    compiled_phys_bytes = None
    compiled_shape_cnt = len(all_col_shapes)
    try:
        compiled_phys_bytes, compiled_shape_cnt = compile_multishape_havok_via_cli(
            all_col_shapes,
            max_faces_per_hull=50000 if force_exact_flag else 48,
            force_exact_triangle_mesh=force_exact_flag,
        )
    except Exception as ex:
        print(f"  [!] Havok collision compile fallback ({col_mode}): {ex}", flush=True)
        compiled_phys_bytes = raw_phys_fitted
        compiled_shape_cnt = struct.unpack_from("<I", compiled_phys_bytes, 12)[0]
        shape_packed_list = [surface_code] * max(1, compiled_shape_cnt)

    # Build Contoured or 1:1 Model Quick Drop Placement Proxy (_pphys / res16 / ebx32) when placement_proxy_mode is not BOUNDING_BOX
    pphys_strip_mv0 = bool(prop_entry.get("pphys_strip_mv0", True))
    pphys_force_convex = bool(prop_entry.get("pphys_force_convex", False))
    pphys_recenter = bool(prop_entry.get("pphys_recenter", True))
    explicit_8hull = prop_entry.get("use_b94_8hull_pphys")
    pphys_mode_8hull = str(prop_entry.get("pphys_8hull_mode") or "contoured_cli").strip().lower()
    pphys_vms_list: list[tuple] = []

    has_prop_lights_or_vfx = bool(lights) or (
        bool(exp_cfg.get("compile_wip_vfx_in_game", False))
        and (bool(exp_cfg.get("vfx_emitters")) or bool((exp_cfg.get("music_emitter") or {}).get("enabled")))
    )
    allow_3d_pphys = (not no_skater_col) or (no_col_placement_mode == "MATCH_MESH")
    auto_8hull_parts: list[trimesh.Trimesh] = []
    if explicit_8hull is None and allow_3d_pphys and not has_prop_lights_or_vfx and placement_proxy_mode != "BOUNDING_BOX":
        try:
            if placement_proxy_mode == "CUSTOM_OBJECT" and proxy_src_glb and pphys_submeshes and len(pphys_submeshes) >= 2:
                for sm in pphys_submeshes:
                    if isinstance(sm, trimesh.Trimesh) and len(sm.vertices) >= 4 and len(sm.faces) >= 2:
                        auto_8hull_parts.append(sm.copy())
            else:
                probe_tm = (
                    trimesh.Trimesh(vertices=p_v, faces=p_f, process=True)
                    if (proxy_src_glb and len(p_v) >= 4 and len(p_f) >= 2)
                    else (
                        unsunk_proxy_meshes[0]
                        if (unsunk_proxy_meshes and len(unsunk_proxy_meshes) == 1)
                        else (
                            trimesh.util.concatenate(unsunk_proxy_meshes)
                            if unsunk_proxy_meshes
                            else trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)
                        )
                    )
                )
                auto_8hull_parts = split_mesh_into_pphys_islands(
                    probe_tm, max_islands=8, max_faces_per_hull=max(12, min(32, int(pphys_target_tris)))
                )
        except Exception:
            auto_8hull_parts = []
        use_b94_8hull_pphys = len(auto_8hull_parts) >= 2
    else:
        use_b94_8hull_pphys = bool(explicit_8hull) and not has_prop_lights_or_vfx

    if allow_3d_pphys and use_b94_8hull_pphys:
        try:
            # Build 8 separate sc=1 placement proxy hulls (_pphys_0..7) for Bundle 94 8-hull multi-part shell
            if auto_8hull_parts:
                part_tms = list(auto_8hull_parts)
            elif placement_proxy_mode == "CUSTOM_OBJECT" and proxy_src_glb and pphys_submeshes and len(pphys_submeshes) >= 2:
                part_tms = []
                for sm in pphys_submeshes:
                    if isinstance(sm, trimesh.Trimesh):
                        if len(sm.vertices) >= 4 and len(sm.faces) >= 2:
                            part_tms.append(sm.copy())
                    elif isinstance(sm, dict):
                        sv, sf = sm.get("verts"), sm.get("faces")
                        if sv is not None and sf is not None and len(sv) >= 4 and len(sf) >= 2:
                            part_tms.append(trimesh.Trimesh(vertices=sv, faces=sf, process=True))
            elif unsunk_proxy_meshes and len(unsunk_proxy_meshes) > 1:
                part_tms = list(unsunk_proxy_meshes)
            else:
                src_tm_8 = (
                    trimesh.Trimesh(vertices=p_v, faces=p_f, process=True)
                    if (proxy_src_glb and len(p_v) >= 4 and len(p_f) >= 2)
                    else (
                        unsunk_proxy_meshes[0]
                        if unsunk_proxy_meshes
                        else trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)
                    )
                )
                part_tms = split_mesh_into_pphys_islands(src_tm_8, max_islands=8, max_faces_per_hull=max(12, min(32, int(pphys_target_tris))))

            if not part_tms:
                part_tms = [trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)]
            if len(part_tms) > 8:
                part_tms = part_tms[:8]
            while len(part_tms) < 8:
                part_tms.append(part_tms[-1].copy())

            built_8_bytes = []
            built_8_vms = []
            if pphys_mode_8hull == "contoured_cli":
                # Unified Multi-Hull Batching + In-Process Analytical Fitting
                batch_results = rpf.compile_multishape_havok_batch_via_cli(
                    part_tms,
                    max_faces_per_hull=max(12, min(32, int(pphys_target_tris))),
                    strip_mv0=pphys_strip_mv0,
                    surface_code=surface_code,
                )
                built_8_bytes = [pb for pb, sc in batch_results]
                for h_tm in part_tms:
                    hv = np.asarray(h_tm.vertices, dtype=np.float32)
                    hf = np.asarray(h_tm.faces, dtype=np.int32)
                    h_vm16, _, _, _, _, _ = compute_mesh_volume_moments(hv, hf)
                    built_8_vms.append(h_vm16)
            else:
                for h_tm in part_tms:
                    hv = np.asarray(h_tm.vertices, dtype=np.float32)
                    hf = np.asarray(h_tm.faces, dtype=np.int32)
                    h_min = hv.min(axis=0)
                    h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
                    h_vm16, _, _, _, _, _ = compute_mesh_volume_moments(hv, hf)
                    h_pb, _, _ = fit_havok_polytope_to_bounds(tpl["res16"], h_min, h_max, surface_code)
                    built_8_bytes.append(h_pb)
                    built_8_vms.append(h_vm16)

            if len(built_8_bytes) == 8:
                pphys_bytes_list = built_8_bytes
                pphys_vms_list = built_8_vms
                raw_proxy_phys = built_8_bytes[0]
                print(
                    f"  [Placement Proxy] Built 8-Hull Multi-Part _pphys_0..7 (mode={pphys_mode_8hull}, mv0={pphys_strip_mv0}) for {display_name}",
                    flush=True,
                )
        except Exception as b94_ex:
            print(f"  [!] 8-Hull Multi-Part _pphys fallback: {b94_ex}", flush=True)

    elif allow_3d_pphys and placement_proxy_mode in ("EXACT_1_1", "EXACT_MESH", "SAME_AS_MESH", "MATCH_COLLISION", "TIGHT_SIMPLIFIED", "MATCH_SKATE_COL", "CUSTOM_OBJECT"):
        try:
            # Single-slot _pphys (sc == 1) uses Havok hknpConvexPolytopeShape (mv == 0), so always
            # compile a valid closed convex polytope unless pphys_allow_concave_1slot is explicitly set
            allow_concave_1slot = bool(prop_entry.get("pphys_allow_concave_1slot", False))
            use_exact_1_1_pphys = (
                not no_skater_col
                and placement_proxy_mode in ("EXACT_1_1", "EXACT_MESH", "SAME_AS_MESH", "MATCH_SKATE_COL", "MATCH_COLLISION")
                and allow_concave_1slot
                and not pphys_force_convex
            )
            if (
                use_exact_1_1_pphys
                and compiled_phys_bytes is not None
                and int(compiled_shape_cnt) == 1
                and not (anti_lip_enabled and anti_lip_sink_m > 0.0)
                and not (proxy_src_glb and placement_proxy_mode == "CUSTOM_OBJECT")
            ):
                # Direct 1:1 reuse of the prop's exact compiled Havok collision mesh (with _pphys a2 footer flags)
                raw_proxy_phys = convert_havok_to_pphys_mv0(
                    compiled_phys_bytes, a2_mode=3, strip_mv0=pphys_strip_mv0, recenter_vertices=pphys_recenter
                )
                print(
                    f"  [Placement Proxy] Using 1:1 model collision physics for _pphys (sc=1, mv0={pphys_strip_mv0}, "
                    f"{len(raw_proxy_phys):,} bytes, mode={placement_proxy_mode}) for {display_name}",
                    flush=True,
                )
            else:
                if placement_proxy_mode == "CUSTOM_OBJECT" and proxy_src_glb and len(p_v) >= 4 and len(p_f) >= 2:
                    src_pphys_tm = trimesh.Trimesh(vertices=p_v, faces=p_f, process=True)
                elif unsunk_proxy_meshes:
                    src_pphys_tm = (
                        unsunk_proxy_meshes[0]
                        if len(unsunk_proxy_meshes) == 1
                        else trimesh.util.concatenate(unsunk_proxy_meshes)
                    )
                else:
                    src_pphys_tm = trimesh.Trimesh(vertices=verts, faces=col_auto_faces, process=True)
                sh_tm = simplify_collision_mesh_for_havok(
                    src_pphys_tm, max_faces=max(16, min(48, int(pphys_target_tris))), force_convex=(not allow_concave_1slot)
                )
                p_vm16, _, _, _, _, _ = compute_mesh_volume_moments(
                    np.asarray(sh_tm.vertices, dtype=np.float32), np.asarray(sh_tm.faces, dtype=np.int32)
                )
                pphys_vms_list = [p_vm16]
                contoured_pphys, pphys_sc = compile_contoured_pphys_via_cli(
                    [sh_tm],
                    max_faces_per_hull=max(16, min(48, int(pphys_target_tris))),
                    exact_1_1=allow_concave_1slot and not pphys_force_convex,
                    strip_mv0=pphys_strip_mv0,
                )
                if contoured_pphys and len(contoured_pphys) >= 200 and int(pphys_sc) == 1:
                    raw_proxy_phys = contoured_pphys
                    print(
                        f"  [Placement Proxy] Compiled contoured convex _pphys (sc={pphys_sc}, mv0={pphys_strip_mv0}, "
                        f"{len(raw_proxy_phys):,} bytes, mode={placement_proxy_mode}) for {display_name}",
                        flush=True,
                    )
        except Exception as pphys_ex:
            print(f"  [!] Contoured/1:1 _pphys compile fallback to 1-box AABB: {pphys_ex}", flush=True)
    elif allow_3d_pphys and placement_proxy_mode == "BOUNDING_BOX" and proxy_src_glb and len(p_v) >= 4 and len(p_f) >= 2:
        p_vm16, _, _, _, _, _ = compute_mesh_volume_moments(p_v, p_f)
        pphys_vms_list = [p_vm16]

    # Also build 1:1 exact trimesh if requested for optional 2A companion
    exact_trimesh_phys_bytes = compiled_phys_bytes
    exact_trimesh_cnt = compiled_shape_cnt

    # Build collision preview summary & warnings for 'Check Collision' + viewport preview
    col_warnings: list[str] = []
    ext_vec = np.asarray(bounds_max, dtype=np.float64) - np.asarray(bounds_min, dtype=np.float64)
    min_thick_m = float(np.min(ext_vec)) if len(ext_vec) == 3 else 0.1
    raw_tri_cnt = int(len(faces))
    col_tri_cnt = sum(
        int(len(s["mesh"].faces))
        for s in all_col_shapes
        if s.get("mesh") is not None and hasattr(s["mesh"], "faces")
    )
    if min_thick_m < 0.025:
        col_warnings.append(
            f"Too thin: minimum thickness is {min_thick_m * 100.0:.1f} cm (< 2.5 cm); Havok collision may pad thin geometry."
        )
    if raw_tri_cnt > int(max_col_tris):
        col_warnings.append(
            f"Too many triangles: mesh has {raw_tri_cnt:,} triangles (> {int(max_col_tris):,} budget); collision was decimated to {col_tri_cnt:,} triangles."
        )
    raw_total_shapes = len(base_col_shapes) + len(grind_col_shapes)
    pphys_hull_cnt = len(auto_8hull_parts) if use_b94_8hull_pphys else 1
    if raw_total_shapes >= 12 or pphys_hull_cnt >= 8:
        col_warnings.append(
            f"Hull count limit hit: reached {compiled_shape_cnt}/12 skate shapes and {pphys_hull_cnt}/8 placement hulls."
        )

    def _extract_zup_wireframe_lines(meshes_list: list, max_edges: int = 2000) -> list[list[float]]:
        lines_out: list[list[float]] = []
        for tm_item in meshes_list:
            tm_obj = tm_item.get("mesh") if isinstance(tm_item, dict) else tm_item
            if tm_obj is None or not hasattr(tm_obj, "vertices") or not hasattr(tm_obj, "faces"):
                continue
            v_arr = np.asarray(tm_obj.vertices, dtype=np.float32)
            f_arr = np.asarray(tm_obj.faces, dtype=np.int32)
            if len(v_arr) == 0 or len(f_arr) == 0:
                continue
            # glTF Y-up (vx, vy, vz) -> Blender Z-up (vx, -vz, vy)
            seen_e: set[tuple[int, int]] = set()
            for tri in f_arr:
                for k_e in range(3):
                    ia, ib = int(tri[k_e]), int(tri[(k_e + 1) % 3])
                    ek = (ia, ib) if ia < ib else (ib, ia)
                    if ek in seen_e:
                        continue
                    seen_e.add(ek)
                    pa = v_arr[ia]
                    pb = v_arr[ib]
                    lines_out.append([
                        round(float(pa[0]), 4), round(float(-pa[2]), 4), round(float(pa[1]), 4),
                        round(float(pb[0]), 4), round(float(-pb[2]), 4), round(float(pb[1]), 4),
                    ])
                    if len(lines_out) >= max_edges:
                        return lines_out
        return lines_out

    collision_preview_data = {
        "prop_key": slug,
        "compiler_version": COMPILER_VERSION,
        "game_dir": str(GAME_DIR),
        "skate_shape_count": int(compiled_shape_cnt),
        "pphys_hull_count": int(pphys_hull_cnt),
        "input_tri_count": int(raw_tri_cnt),
        "col_tri_count": int(col_tri_cnt),
        "min_thickness_m": round(float(min_thick_m), 4),
        "warnings": col_warnings,
        "col_lines_zup": _extract_zup_wireframe_lines(all_col_shapes, max_edges=2000),
        "pphys_lines_zup": _extract_zup_wireframe_lines(auto_8hull_parts if auto_8hull_parts else unsunk_proxy_meshes[:1], max_edges=800),
    }

    return {
        "collision_preview": collision_preview_data,
        "spec": {
            "key_stem": stem,
            "prop_key": slug,
            "prop_id": stem,
            "display_name": display_name,
            "family_title": str(prop_entry.get("family_title") or prop_entry.get("variant_group") or "").strip(),
            "category": category,
            "custom_qd_tab": str(prop_entry.get("custom_qd_tab") or "").strip(),
            "collision_mode": col_mode,
            "surface_id": surface_id,
            "surface_label": str(prop_entry.get("surface_label") or "").strip(),
            "material_surfaces": list(prop_entry.get("material_surfaces") or []),
            "is_pack_icon_prop": bool(prop_entry.get("is_pack_icon_prop", False)),
            "packed": surface_code,
            "shape_packed_list": shape_packed_list,
            "round_rail": round_rail,
            "enable_custom_textures": enable_custom_textures,
            "roughness_multiplier": roughness_mult,
            "metallic_multiplier": metallic_mult,
            "specular_multiplier": specular_mult,
            "albedo_tint": albedo_tint,
            "slot_pbr_classes": slot_pbr_classes,
            "lights": lights,
            "use_osk_alpha_shader": bool(use_osk_alpha_shader),
            "has_multi_mat_cutout_split": bool(has_multi_mat_cutout_split),
            "cutout_slot_idx": int(cutout_slot_idx),
            "use_native_771_detail_slots": bool(prop_entry.get("use_native_771_detail_slots", False)),
            "use_2x2m_detail_shader": use_2x2m_detail_shader,
            "detail_2x2m_preset": detail_2x2m_preset,
            "detail_2x2m_intensity": detail_2x2m_intensity,
            "detail_2x2m_scale": detail_2x2m_scale,
            "anti_lip_enabled": anti_lip_enabled,
            "anti_lip_sink_m": anti_lip_sink_m,
            "atlas_resolution": target_tex_res,
            "experimental": exp_cfg,
            "id_mode": prop_entry.get("id_mode"),
            "sbd_name_mode": prop_entry.get("sbd_name_mode"),
            "register_in_res3245": prop_entry.get("register_in_res3245"),
            "use_b94_8hull_pphys": bool(use_b94_8hull_pphys),
            "thumb_256_path": str(prop_entry.get("thumb_256_path") or prop_entry.get("custom_thumbnail_path") or "").strip(),
            "thumb_768_path": str(prop_entry.get("thumb_768_path") or "").strip(),
            "input_tri_count": int(raw_tri_cnt),
        },
        "prev": {
            "bounds_min": bounds_min,
            "bounds_max": bounds_max,
            "volume_moments_16f": vm16,
            "avg_rgb": avg_rgb,
        },
        "verts": verts,
        "norms": norms,
        "uvs": uvs,
        "faces": faces,
        "mesh_res_b39": new_ms_b39,
        "mesh_mc_b39": new_mc_b39,
        "mesh_chk_b39": new_chk_b39,
        "tex_bc7_b39": tex_bc7_b39,
        "norm_bc7_b39": norm_bc7_b39,
        "msk_bc7_b39": msk_bc7_b39,
        "tex_dims_b39": (int(tex_w_b39), int(tex_h_b39)),
        "thumb_sm_bc7": thumb_sm_bc7,
        "thumb_lg_bc7": thumb_lg_bc7,
        "raw_proxy_phys": raw_proxy_phys,
        "pphys_bytes_list": pphys_bytes_list,
        "pphys_vms_list": pphys_vms_list,
        "raw_phys_fitted": raw_phys_fitted,
        "multishape_phys_bytes": compiled_phys_bytes,
        "multishape_cnt": compiled_shape_cnt,
        "exact_trimesh_phys_bytes": exact_trimesh_phys_bytes,
        "exact_trimesh_cnt": exact_trimesh_cnt,
        "tex_chk_guid_cached": tex_chk_guid_cached,
        "norm_chk_guid_cached": norm_chk_guid_cached,
        "msk_chk_guid_cached": msk_chk_guid_cached,
    }


def _enc_varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def inject_global_shaders_and_globals_toc(mod_dir: Path, prop_specs_for_toc: list[dict], coexist_mode: bool = True):
    """
    Synthesizes Win32/items.toc (custom texture RES/CHK metadata; leaving RES[3245] shaderblockdepot
    untouched by default) and Win32/globals.toc (RES[1130] streamabletexturesetlookuptable
    + RES[2002] shaderstate_db) for textured props.
    - Never reads Mods\\ (builds strictly from vanilla Data\\Win32\\globals.toc).
    - Leaves Win32/items.toc RES[3245] untouched (is_patch=0) in coexist_mode.
    - Writes RES[2002] lookupSize as len(new_r2002) - (programSize + relocationSize) read from resMeta.
    """
    import hashlib

    tex_specs = [
        s for s in prop_specs_for_toc
        if s.get("enable_custom_textures", True)
        and "rh_even" in s
        and s["rh_even"] != 0xF136CD5C26C6D084
        and "sbd_259_block" in s
    ]
    if not tex_specs:
        stale_globals = mod_dir / "Win32" / "globals.toc"
        if stale_globals.exists():
            stale_globals.unlink()
        return

    cas_path = mod_dir / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas"
    toc_path = mod_dir / "Win32" / "items.toc"
    if not cas_path.exists() or not toc_path.exists():
        return

    cas_bytes = bytearray(cas_path.read_bytes())
    toc_bytes = toc_path.read_bytes()
    items_pl = toc_bytes[556:]
    iw = struct.unpack_from(">12I", items_pl, 0)
    _, itype_sz, idesc_off = struct.unpack_from(">IIQ", items_pl, iw[1])
    istreams = list(bsp.parse_bundle_desc(items_pl[idesc_off : idesc_off + (itype_sz & 0x3FFFFFFF)]))
    is0 = istreams[0]
    sb_raw = (
        bytes(cas_bytes[is0[3] : is0[3] + is0[4]])
        if is0[0] == 1
        else rpf.read_cas_raw(0 if is0[1] == 3162564249 else 1, is0[2], is0[3], is0[4])
    )
    sb_raw_dec = _get_sbdata_payload(sb_raw)
    magic, ebxs, ress, chks, cmeta = bsp.parse_sbdata_full(sb_raw_dec)

    # Parse cmeta blobs in items.toc
    cmeta_entries = bsp.parse_cmeta_h64s(cmeta)
    cmeta_blobs = [bytes(cmeta[pos : pos + esz]) for pos, esz, _ in cmeta_entries]

    res_by_name = {r["name"].lower(): idx for idx, r in enumerate(ress)}
    chk_by_guid = {bytes(c["guid_raw"]): idx for idx, c in enumerate(chks)}

    # 1A. Patch custom texture RES & CHK metadata in items.toc (_c, _n, and _msk)
    for s in prop_specs_for_toc:
        tex_patch_items = [
            (f"items/customprops/{s['prop_key']}_c".lower(), s.get("tex_chunk_guid_raw"), s.get("raw_tex_bc7")),
            (f"items/customprops/{s['prop_key']}_n".lower(), s.get("norm_chunk_guid_raw"), s.get("raw_norm_bc7")),
            (f"items/customprops/{s['prop_key']}_msk".lower(), s.get("msk_chunk_guid_raw"), s.get("raw_msk_bc7")),
        ]
        for tname, g_raw, raw_bc7 in tex_patch_items:
            if g_raw and tname in res_by_name and g_raw in chk_by_guid:
                r_idx = res_by_name[tname]
                c_idx = chk_by_guid[g_raw]
                c_st = istreams[1 + len(ebxs) + len(ress) + c_idx]
                if c_st[0] == 1:
                    raw_top = bytes(cas_bytes[c_st[3] : c_st[3] + c_st[4]])
                    raw_bc7_len = len(raw_bc7) if raw_bc7 is not None else 1398128
                    tex_w_p, tex_h_p, n_mips_p, mip_sizes_p, total_mip_sz_p = _get_bc7_mip_spec_from_bytes_len(
                        raw_bc7_len, hint_dims=s.get("tex_dims_b39")
                    )
                    mip1_u_off = mip_sizes_p[0]
                    mip2_u_off = mip_sizes_p[0] + mip_sizes_p[1]
                    cur_b = 0
                    u_acc = 0
                    range_start = 0
                    range_end = 0
                    while cur_b + 8 <= len(raw_top):
                        if u_acc == mip1_u_off and range_start == 0:
                            range_start = cur_b
                        if u_acc == mip2_u_off and range_end == 0:
                            range_end = cur_b
                        u_sz, fl_b, c_sz = struct.unpack_from(">IHH", raw_top, cur_b)
                        act_c_sz = u_sz if fl_b in (0x0070, 0x0000, 0x0071) else c_sz
                        cur_b += 8 + act_c_sz
                        u_acc += u_sz
                    tex_h64 = bsp.frostbite_fnv1_64_lower(tname)

                    r_st = istreams[1 + len(ebxs) + r_idx]
                    if r_st[0] == 1:
                        r9_raw = bytearray(_decompress_cas_payload(bytes(cas_bytes[r_st[3] : r_st[3] + r_st[4]])))
                        if len(r9_raw) >= 0xB4:
                            struct.pack_into("<II", r9_raw, 0x00, range_start, range_end)
                            struct.pack_into("<HH", r9_raw, 0x16, tex_w_p, tex_h_p)
                            r9_raw[0x1E:0x22] = bytes([n_mips_p, 0, 0, n_mips_p])
                            r9_raw[0x24:0x28] = b"\x00\x00\x00\x00"
                            padded_mips_p = (mip_sizes_p + [0] * 15)[:15]
                            struct.pack_into("<15I", r9_raw, 0x38, *padded_mips_p)
                            struct.pack_into("<I", r9_raw, 0x74, total_mip_sz_p)
                            struct.pack_into("<Q", r9_raw, 0x78, tex_h64)
                            r9_raw[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")
                            while len(cas_bytes) % 4 != 0:
                                cas_bytes.append(0)
                            r9_cas_off = len(cas_bytes)
                            r9_comp = struct.pack(">IHH", len(r9_raw), 0x0070, len(r9_raw)) + bytes(r9_raw)
                            cas_bytes += r9_comp
                            ress[r_idx]["orig_size"] = len(r9_raw)
                            ress[r_idx]["sha1"] = hashlib.sha1(bytes(r9_raw)).digest()
                            istreams[1 + len(ebxs) + r_idx] = (1, 3162564249, 1, r9_cas_off, len(r9_comp))

                    chks[c_idx]["log_off"] = 0
                    chks[c_idx]["log_sz"] = total_mip_sz_p
                    if raw_bc7 is not None and len(raw_bc7) == total_mip_sz_p:
                        chks[c_idx]["sha1"] = hashlib.sha1(raw_bc7).digest()
                    if c_idx < len(cmeta_blobs):
                        cm_b = bytearray(cmeta_blobs[c_idx])
                        if len(cm_b) == 36:
                            struct.pack_into("<Q", cm_b, 5, tex_h64)
                            struct.pack_into("<I", cm_b, 30, 0)
                            cmeta_blobs[c_idx] = bytes(cm_b)

    diplo_sbd_raw = None
    diplo_sbd_meta = None
    diplo_r1130_raw = None
    diplo_r2002_raw = None
    diplo_r2002_meta = None

    # 1B. Patch items.toc RES[3245] (`shaderblockdepot_6743618527722487888`) ONLY for diagnostic props that explicitly opt in.
    # By default (coexist_mode=True), sbd3245_specs is empty so items.toc RES[3245] is left untouched (is_patch=0).
    sbd_res_idx = next(
        (i for i, r in enumerate(ress) if r["res_type"] == 0x73312045 or "shaderblockdepot" in r["name"].lower()),
        None,
    )
    sbd3245_specs = [s for s in tex_specs if s.get("register_in_res3245", not coexist_mode)]
    if not sbd3245_specs:
        print(
            f"  [+] Leaving items.toc RES[{sbd_res_idx}] untouched (is_patch=0); material blocks reside only in bundle-local ShaderBlockDepot.",
            flush=True,
        )
    elif sbd_res_idx is not None:
        if diplo_sbd_raw is not None and diplo_sbd_meta is not None:
            v_sbd = diplo_sbd_raw
            v_meta = diplo_sbd_meta
            base_sbd_label = "Diplomatico+Vanilla"
        else:
            sbd_st = istreams[1 + len(ebxs) + sbd_res_idx]
            pkg_idx = 0 if sbd_st[1] == 3162564249 else 1
            if sbd_st[0] == 1:
                v_sbd = _decompress_cas_payload(bytes(cas_bytes[sbd_st[3] : sbd_st[3] + sbd_st[4]]))
            else:
                v_sbd = rpf.decompress_cas(pkg_idx, sbd_st[2], sbd_st[3], sbd_st[4])
            v_meta = ress[sbd_res_idx]["res_meta"]
            base_sbd_label = "Vanilla"

        v_r_cnt = struct.unpack_from("<I", v_meta, 12)[0]
        v_b_start = 0x10 + v_r_cnt * 16
        v_d_off = struct.unpack_from("<Q", v_sbd, 0x18)[0]
        v_reloc_off = struct.unpack_from("<I", v_meta, 0)[0]
        v_d_cnt = (v_reloc_off - v_d_off) // 24

        # Find true end of existing blocks from last directory entry
        last_boff, _last_subh, last_bsz = struct.unpack_from("<QQQ", v_sbd, v_d_off + (v_d_cnt - 1) * 24)
        v_blocks_end = last_boff + last_bsz
        v_blocks_raw = v_sbd[v_b_start:v_blocks_end]

        n_add = len(sbd3245_specs)
        new_r_cnt = v_r_cnt + 2 * n_add
        new_b_start = 0x10 + new_r_cnt * 16
        delta_b_start = new_b_start - v_b_start

        appended_blocks = b"".join(s["sbd_259_block"] for s in sbd3245_specs)
        all_blocks = v_blocks_raw + appended_blocks
        new_d_off = (new_b_start + len(all_blocks) + 15) & ~15
        pad_before_dir = b"\x00" * (new_d_off - (new_b_start + len(all_blocks)))
        delta_d_off = new_d_off - v_d_off

        # Update existing root table entries (shift dir_ptr by +delta_d_off) and append 2 * n_add new roots
        new_roots = bytearray()
        for ri in range(v_r_cnt):
            rh, dptr = struct.unpack_from("<QQ", v_sbd, 0x10 + ri * 16)
            new_roots += struct.pack("<QQ", rh, dptr + delta_d_off)
        for i, s in enumerate(sbd3245_specs):
            dir_ptr = new_d_off + (v_d_cnt + i) * 24
            new_roots += struct.pack("<QQ", s["rh_even"], dir_ptr)
            new_roots += struct.pack("<QQ", s["rh_odd"], dir_ptr)

        # Update existing directory entries (shift boff by +delta_b_start) and append n_add new directory entries
        new_dirs = bytearray()
        for di in range(v_d_cnt):
            boff, subh, bsz = struct.unpack_from("<QQQ", v_sbd, v_d_off + di * 24)
            new_dirs += struct.pack("<QQQ", boff + delta_b_start, subh, bsz)
        cur_boff = new_b_start + len(v_blocks_raw)
        for s in sbd3245_specs:
            bsz = len(s["sbd_259_block"])
            new_dirs += struct.pack("<QQQ", cur_boff, s["unique_sub0"], bsz)
            cur_boff += bsz

        new_d_cnt = v_d_cnt + n_add
        new_reloc_off = (new_d_off + new_d_cnt * 24 + 15) & ~15
        pad_before_reloc = b"\x00" * (new_reloc_off - (new_d_off + len(new_dirs)))

        sbd_header_and_body = (
            v_sbd[:0x10]
            + bytes(new_roots)
            + all_blocks
            + pad_before_dir
            + bytes(new_dirs)
            + pad_before_reloc
        )

        # Build exact pointer relocation table
        seen_dirs = set()
        relocs = []
        for ri in range(new_r_cnt):
            rptr = struct.unpack_from("<Q", sbd_header_and_body, 0x18 + ri * 16)[0]
            if rptr not in seen_dirs:
                seen_dirs.add(rptr)
                relocs.append(rptr)
            relocs.append(0x18 + ri * 16)
        relocs.append(0)
        reloc_bytes = struct.pack(f"<{len(relocs)}I", *relocs)

        new_sbd_raw = sbd_header_and_body + reloc_bytes
        new_sbd_meta = struct.pack("<4I", new_reloc_off, len(reloc_bytes), 0x01190003, new_r_cnt)

        while len(cas_bytes) % 4 != 0:
            cas_bytes.append(0)
        sbd_cas_off = len(cas_bytes)
        sbd_comp = rpf.cas_compress_uncompressed(new_sbd_raw)
        cas_bytes += sbd_comp
        ress[sbd_res_idx]["orig_size"] = len(new_sbd_raw)
        ress[sbd_res_idx]["sha1"] = hashlib.sha1(new_sbd_raw).digest()
        ress[sbd_res_idx]["res_meta"] = new_sbd_meta
        istreams[1 + len(ebxs) + sbd_res_idx] = (1, 3162564249, 1, sbd_cas_off, len(sbd_comp))
        print(
            f"  [+] Patched items.toc RES[{sbd_res_idx}] ({base_sbd_label} base: {v_r_cnt}->{new_r_cnt} roots, {v_d_cnt}->{new_d_cnt} blocks for {[s['prop_key'] for s in sbd3245_specs]})",
            flush=True,
        )

    # Rebuild items.toc sbdata and bundle descriptor
    new_cmeta_blobs = b"".join(b"\x82" + _enc_varint(len(b)) + b for b in cmeta_blobs)
    new_cmeta_body = new_cmeta_blobs + b"\x00"
    new_cmeta = b"\x01chunkMeta\x00" + _enc_varint(len(new_cmeta_body)) + new_cmeta_body
    new_sb = bsp.serialize_sbdata_full(magic, ebxs, ress, chks, new_cmeta)
    while len(cas_bytes) % 4 != 0:
        cas_bytes.append(0)
    new_sb_off = len(cas_bytes)
    cas_bytes += new_sb
    istreams[0] = (1, 3162564249, 1, new_sb_off, len(new_sb))
    new_bdesc = bsp.serialize_bundle_desc(istreams)

    w0, w1, n_b, w3, w4, n_c, c_loc_off, _w7, str_off, _w9, _w10, _w11 = struct.unpack_from(">12I", items_pl, 0)
    str_rel = struct.unpack_from(">I", items_pl, w1)[0]
    s_end = items_pl.index(0, str_off + str_rel)
    b_name_bytes = bytes(items_pl[str_off + str_rel : s_end + 1])
    new_desc_start = (str_off + len(b_name_bytes) + 3) & ~3
    new_b_entry = struct.pack(">IIQ", 0, 0x40000000 | len(new_bdesc), new_desc_start)
    str_pad = b"\x00" * (new_desc_start - (str_off + len(b_name_bytes)))
    desc_pad = b"\x00" * (((len(new_bdesc) + 3) & ~3) - len(new_bdesc))

    new_items_pl = (
        bytes(items_pl[:w1])
        + new_b_entry
        + bytes(items_pl[w1 + 16 : str_off])
        + b_name_bytes
        + str_pad
        + new_bdesc
        + desc_pad
    )
    toc_path.write_bytes(bytes(toc_bytes[:556]) + new_items_pl)

    # 2. Synthesize Win32/globals.toc with RES[1130] and RES[2002]
    base_globals_path = GAME_DIR / "Data" / "Win32" / "globals.toc"
    if base_globals_path.exists():
        log_input_read(base_globals_path, coexist_mode=coexist_mode)
        g_toc_v = base_globals_path.read_bytes()
        g_pl = g_toc_v[556:]
        gw = struct.unpack_from(">12I", g_pl, 0)
        _, g_type_sz, g_desc_off = struct.unpack_from(">IIQ", g_pl, gw[1] + 0 * 16)
        g_streams = list(bsp.parse_bundle_desc(g_pl[g_desc_off : g_desc_off + (g_type_sz & 0x3FFFFFFF)]))
        gs0 = g_streams[0]
        g_sb_raw = rpf.read_cas_raw(0 if gs0[1] == 3162564249 else 1, gs0[2], gs0[3], gs0[4])
        g_magic, g_ebxs, g_ress, g_chks, g_cmeta = bsp.parse_sbdata_full(g_sb_raw)

        # Patch RES[1130] (`streamabletexturesetlookuptable`)
        if not coexist_mode and diplo_r1130_raw is not None:
            v_r1130 = diplo_r1130_raw
        else:
            st_1130 = g_streams[1 + len(g_ebxs) + 1130]
            v_r1130 = rpf.decompress_cas(0 if st_1130[1] == 3162564249 else 1, st_1130[2], st_1130[3], st_1130[4])
        map_1130 = {struct.unpack_from("<Q", v_r1130, i * 16)[0]: struct.unpack_from("<Q", v_r1130, i * 16 + 8)[0] for i in range(len(v_r1130) // 16)}
        for s in tex_specs:
            if s.get("has_multi_mat_cutout_split", False):
                map_1130[s["rh_even"]] = 0xE863DEE0C1440B29
                map_1130[s["rh_odd"]] = 0xE863DEE0C1440B29
            else:
                map_1130[s["rh_even"]] = 0xDADE2D688B5F4787
                map_1130[s["rh_odd"]] = 0xDADE2D688B5F4787
            if "rh771_even" in s and s["rh771_even"] != 0x5959DE714A3CB802:
                if s.get("use_osk_alpha_shader", False):
                    map_1130[s["rh771_even"]] = 0xE863DEE0C1440B29
                    map_1130[s["rh771_odd"]] = 0xE863DEE0C1440B29
                else:
                    map_1130[s["rh771_even"]] = 0xC9172E519B9CEDAF
                    map_1130[s["rh771_odd"]] = 0xC9172E519B9CEDAF
        pairs_1130 = sorted(map_1130.items(), key=lambda x: x[0])
        new_r1130 = b"".join(struct.pack("<QQ", k, v) for k, v in pairs_1130)
        while len(cas_bytes) % 4 != 0:
            cas_bytes.append(0)
        r1130_off = len(cas_bytes)
        r1130_comp = rpf.cas_compress_uncompressed(new_r1130)
        cas_bytes += r1130_comp
        g_ress[1130]["orig_size"] = len(new_r1130)
        g_ress[1130]["sha1"] = hashlib.sha1(new_r1130).digest()
        g_streams[1 + len(g_ebxs) + 1130] = (1, 3162564249, 1, r1130_off, len(r1130_comp))

        # Patch RES[2002] (`shaderstate_db`)
        if not coexist_mode and diplo_r2002_raw is not None and diplo_r2002_meta is not None:
            v_r2002 = diplo_r2002_raw
            g_ress[2002]["res_meta"] = diplo_r2002_meta
        else:
            st_2002 = g_streams[1 + len(g_ebxs) + 2002]
            v_r2002 = rpf.decompress_cas(0 if st_2002[1] == 3162564249 else 1, st_2002[2], st_2002[3], st_2002[4])

        meta_2002 = bytearray(g_ress[2002]["res_meta"])
        prog_sz_2002, reloc_sz_2002 = struct.unpack_from("<II", meta_2002, 0)
        lookup_off_2002 = prog_sz_2002 + reloc_sz_2002
        v_cnt_2002 = struct.unpack_from("<I", v_r2002, lookup_off_2002)[0]
        pos_2002 = lookup_off_2002 + 4
        map_2002 = {}
        for _ in range(v_cnt_2002):
            rh, n_ids = struct.unpack_from("<QI", v_r2002, pos_2002)
            ids = struct.unpack_from(f"<{n_ids}I", v_r2002, pos_2002 + 12)
            map_2002[rh] = ids
            pos_2002 += 12 + n_ids * 4
        b39_ids = (587, 632, 736, 1132, 1152)
        b39_771_ids = (173, 292, 299, 332, 958, 994, 1019, 1355, 1374)
        b58_273_cutout_ids = (32, 73, 333, 1033)
        for s in tex_specs:
            if s.get("has_multi_mat_cutout_split", False):
                map_2002[s["rh_even"]] = b58_273_cutout_ids
                map_2002[s["rh_odd"]] = b58_273_cutout_ids
            else:
                map_2002[s["rh_even"]] = b39_ids
                map_2002[s["rh_odd"]] = b39_ids
            if "rh771_even" in s and s["rh771_even"] != 0x5959DE714A3CB802:
                if s.get("use_osk_alpha_shader", False):
                    map_2002[s["rh771_even"]] = b58_273_cutout_ids
                    map_2002[s["rh771_odd"]] = b58_273_cutout_ids
                else:
                    map_2002[s["rh771_even"]] = b39_771_ids
                    map_2002[s["rh771_odd"]] = b39_771_ids
        ents_2002 = sorted(map_2002.items(), key=lambda x: x[0])
        tail_2002 = bytearray(struct.pack("<I", len(ents_2002)))
        for rh, ids in ents_2002:
            tail_2002 += struct.pack(f"<QI{len(ids)}I", rh, len(ids), *ids)
        new_r2002 = v_r2002[:lookup_off_2002] + bytes(tail_2002)
        struct.pack_into("<I", meta_2002, 8, len(new_r2002) - (prog_sz_2002 + reloc_sz_2002))
        g_ress[2002]["res_meta"] = bytes(meta_2002)
        while len(cas_bytes) % 4 != 0:
            cas_bytes.append(0)
        r2002_off = len(cas_bytes)
        r2002_comp = rpf.cas_compress_uncompressed(new_r2002)
        cas_bytes += r2002_comp
        g_ress[2002]["orig_size"] = len(new_r2002)
        g_ress[2002]["sha1"] = hashlib.sha1(new_r2002).digest()
        g_streams[1 + len(g_ebxs) + 2002] = (1, 3162564249, 1, r2002_off, len(r2002_comp))

        while len(cas_bytes) % 4 != 0:
            cas_bytes.append(0)
        gsb_off = len(cas_bytes)
        gsb_new = bsp.serialize_sbdata_full(g_magic, g_ebxs, g_ress, g_chks, g_cmeta)
        cas_bytes += gsb_new
        g_streams[0] = (1, 3162564249, 1, gsb_off, len(gsb_new))
        gb_desc = bsp.serialize_bundle_desc(g_streams)

        gb_name = "win32/configurations/gameconfigurations/delmargameconfiguration"
        gw0 = 0x30
        gw1 = 0x38
        g_str_off = gw1 + 16
        gb_name_b = gb_name.encode("ascii") + b"\x00"
        g_desc_start = (g_str_off + len(gb_name_b) + 3) & ~3
        g_str_pad = b"\x00" * (g_desc_start - (g_str_off + len(gb_name_b)))
        g_desc_pad = b"\x00" * (((len(gb_desc) + 3) & ~3) - len(gb_desc))
        g_hdr12 = struct.pack(">12I", gw0, gw1, 1, 0, 0, 0, g_str_off, g_str_off, g_str_off, g_str_off, 0, 3)
        g_mph_buf = struct.pack(">i", -1) + b"\x00\x00\x00\x00"
        g_b_entry = struct.pack(">IIQ", 0, 0x40000000 | len(gb_desc), g_desc_start)
        g_pl_out = g_hdr12 + g_mph_buf + g_b_entry + gb_name_b + g_str_pad + gb_desc + g_desc_pad
        g_full_toc = b"\x00\xd1\xce\x01" + (b"\x00" * 552) + g_pl_out
        (mod_dir / "Win32" / "globals.toc").write_bytes(g_full_toc)
        print(
            f"  [+] Generated Win32/globals.toc & registered {len(tex_specs)*2} shader roots in RES[1130] ({len(pairs_1130)} total) & RES[2002] ({len(ents_2002)} total)",
            flush=True,
        )

    cas_path.write_bytes(cas_bytes)


def compile_blender_props_package(
    cached_props: list[dict],
    meta: dict,
    tpl: dict,
    item_tpl: bytes,
    imc_tpl: bytes,
    noble_effect_ebx: bytes | None,
) -> Path:
    """Synthesizes zero-donor infinite-extent standalone Bundle 39 mod package, TOCs, and .fbmod."""
    _apply_game_and_staging_runtime_paths(meta.get("game_dir"), meta.get("output_staging_dir"))
    mod_name = meta["target_mod_name"]
    author_name_str = str(meta.get("author_name") or "").strip()
    category_name = cached_props[0]["spec"]["category"] if cached_props else "Custom Props"
    staging_dir = meta["output_staging_dir"]
    staging_dir.mkdir(parents=True, exist_ok=True)
    out_fbmod = staging_dir / f"{mod_name}.fbmod"
    target_mod_dir = staging_dir / mod_name

    include_standard_set = True
    include_exact_2a_set = bool(meta.get("include_2a_variant", False))
    export_fbmod_only = bool(meta.get("export_fbmod_only", False))
    coexist_mode = bool(meta.get("coexist_mode", True))
    default_ns = f"{author_name_str}/{mod_name}" if author_name_str else mod_name
    coexist_namespace = str(meta.get("coexist_namespace") or default_ns).strip("/")
    pack_version = str(meta.get("pack_version", "0.1.0" if coexist_mode else "1.5.0"))
    addon_version = meta.get("addon_version", None)
    compiler_version_stamp = COMPILER_VERSION
    build_note = str(meta.get("build_note", "")).strip() or (
        f"Standalone coexist build ({mod_name}: vanilla globals base, dynamic RES[2002] lookupSize, untouched RES[3245], bundle-local SBD, SHA-256/UUIDv5 IDs)"
        if coexist_mode
        else f"Standard standalone Bundle 39 build ({mod_name})"
    )
    pack_desc = str(meta.get("description", "")).strip() or (
        f"ReSkate Prop Studio standalone prop pack ({mod_name} v{pack_version}, built with compiler {compiler_version_stamp})"
        if coexist_mode
        else f"ReSkate Blender Prop Studio Authored Mod ({mod_name})"
    )
    deploy_to_mods = bool(meta.get("deploy_to_mods", coexist_mode))
    raw_pack_slug = str(meta.get("pack_slug") or coexist_namespace).lower()
    pack_slug = re.sub(r"[^a-z0-9_]+", "_", raw_pack_slug).strip("_") or "custompack"
    bundle_stem_base = str(meta.get("bundle_stem_base", "items/customprops")).strip("/")
    key_prefix = "" if coexist_mode else "own_customprops_"
    vanilla_root_hi32s = _load_vanilla_root_hi32s(tpl, coexist_mode=coexist_mode)

    has_any_lights = any(bool((cp.get("spec") or {}).get("lights")) for cp in cached_props)
    has_any_vfx_or_music = any(
        bool(((cp.get("spec") or {}).get("experimental") or {}).get("compile_wip_vfx_in_game", False))
        and (
            bool(((cp.get("spec") or {}).get("experimental") or {}).get("vfx_emitters"))
            or bool((((cp.get("spec") or {}).get("experimental") or {}).get("music_emitter") or {}).get("enabled"))
        )
        for cp in cached_props
    )
    noble_nopol_raw = None
    if has_any_lights or has_any_vfx_or_music:
        noble_nopol_raw = rpf.get_light_nopol_struct_bytes()
    if has_any_lights and noble_effect_ebx is None:
        noble_effect_ebx = rpf.get_light_ebp_struct_bytes()

    print("\n" + "=" * 80, flush=True)
    print(f"COMPILING RESKATE BLENDER PROPS SUITE (COMPILER {compiler_version_stamp}): {mod_name} (pack {pack_version})", flush=True)
    print(f"Props: {len(cached_props)} | Category: {category_name} | Coexist Mode: {coexist_mode} | 2A Companion: {include_exact_2a_set}", flush=True)
    print(f"Staging: {staging_dir}", flush=True)
    print("=" * 80, flush=True)

    fbmod_records = []
    custom_imc_refs = []
    prop_specs_for_toc = []
    allowed_custom_ebx = set()
    allowed_custom_res = set()
    forbidden_chunk_guids_raw = set()

    def _extract_fg_ig(eb_raw: bytes):
        _, ch_list = demux_riff(eb_raw)
        cm = dict(ch_list)
        return bytes(cm[b"EFIX"][0:16]), bytes(cm[b"EBXD"][12:28])

    orig_fg24, orig_ig24 = _extract_fg_ig(tpl["ebx24"])
    orig_fg25, orig_ig25 = _extract_fg_ig(tpl["ebx25"])
    orig_fg26, orig_ig26 = _extract_fg_ig(tpl["ebx26"])
    orig_fg39, orig_ig39 = _extract_fg_ig(tpl["ebx39"])
    orig_fg77, orig_ig77 = _extract_fg_ig(tpl["ebx77"])
    orig_fg78, orig_ig78 = _extract_fg_ig(tpl["ebx78"])
    orig_fg79, orig_ig79 = _extract_fg_ig(tpl["ebx79"])
    orig_fg81, orig_ig81 = _extract_fg_ig(tpl["ebx81"])
    orig_fg82, orig_ig82 = _extract_fg_ig(tpl["ebx82"])
    _, orig_ig34 = _extract_fg_ig(tpl["ebx34"])
    _, orig_ig35 = _extract_fg_ig(tpl["ebx35"])

    variant_passes = []
    if include_standard_set:
        variant_passes.append(("standard", key_prefix, ""))
    if include_exact_2a_set:
        pfx_2a = key_prefix if not include_standard_set else f"{key_prefix}2a_"
        variant_passes.append(("exact_2a", pfx_2a, " [2A 1:1 Mesh]"))

    n_total_props = max(1, len(cached_props))
    first_pass_variant = variant_passes[0][0] if variant_passes else "standard"

    for col_variant, cur_pfx, disp_suffix in variant_passes:
        used_prop_keys_in_pass: set[str] = set()
        used_item_keys_in_pass: set[str] = set()
        for p_idx, cp in enumerate(cached_props, start=1):
            spec = cp["spec"]
            prev = cp["prev"]
            stem = spec["key_stem"]
            display_name = sanitize_display_name(f"{spec['display_name']}{disp_suffix}")
            if col_variant == first_pass_variant:
                cached_tag = " (Cached)" if cp.get("_is_cached") else ""
                emit_prop_substep(p_idx, n_total_props, display_name, 4, f"Assembling Bundle 39 & Prefab EBX{cached_tag}...")
            prop_cat = spec.get("category") or category_name
            prop_lights = spec.get("lights") or []
            prop_id_mode = str(spec.get("id_mode") or ("coexist" if coexist_mode else "legacy")).lower()
            use_coexist_ids = (prop_id_mode == "coexist")
            prop_sbd_name_mode = str(spec.get("sbd_name_mode") or ("vanilla" if coexist_mode else "legacy")).lower()
            prop_register_res3245 = spec.get("register_in_res3245")
            if prop_register_res3245 is None:
                prop_register_res3245 = (not coexist_mode)
            if coexist_mode:
                # Never write items.toc RES[3245] in coexist_mode!
                prop_register_res3245 = False

            exp_spec = spec.get("experimental") if isinstance(spec.get("experimental"), dict) else {}

            if use_coexist_ids:
                qd_tab_slug = _resolve_qd_tab_slug(prop_cat, spec.get("custom_qd_tab"))
                clean_stem = _clean_prop_stem(
                    f"{cur_pfx}{stem}",
                    extra_prefixes=[pack_slug, mod_name, qd_tab_slug, meta.get("author_name", "")],
                )
                raw_prop_slug = re.sub(r"[^a-z0-9_]+", "_", clean_stem.lower()).strip("_") or "prop"
                if raw_prop_slug.endswith("_lrg"):
                    raw_prop_slug = f"{raw_prop_slug[:-4]}_large"
                short_pack = _clean_prop_stem(pack_slug, extra_prefixes=[qd_tab_slug, meta.get("author_name", "")])
                base_pk = f"{short_pack}__{raw_prop_slug}" if (short_pack and short_pack != raw_prop_slug) else raw_prop_slug
                prop_key = clamp_slug(base_pk, 36)
                if prop_key in used_prop_keys_in_pass:
                    prop_key = clamp_slug(f"{base_pk}_{p_idx}", 36)
                used_prop_keys_in_pass.add(prop_key)

                item_key_str = clamp_slug(f"own_{qd_tab_slug}_{raw_prop_slug}", 250)
                if item_key_str.endswith("_lrg"):
                    item_key_str = f"{item_key_str[:-4]}_large"
                if item_key_str in used_item_keys_in_pass:
                    item_key_str = clamp_slug(f"{item_key_str}_{p_idx}", 250)
                used_item_keys_in_pass.add(item_key_str)

                thumb_key = item_key_str
                item_path = f"Items/{qd_tab_slug}/{item_key_str}"
                seed_prefix = f"{coexist_namespace}/{raw_prop_slug}"
            else:
                raw_prop_slug = stem
                prop_key = clamp_slug(f"{cur_pfx}{stem}", 36)
                item_key_str = prop_key
                thumb_key = prop_key
                qd_tab_slug = _resolve_qd_tab_slug(prop_cat, spec.get("custom_qd_tab"))
                item_path = f"Items/{qd_tab_slug}/{prop_key}"
                seed_prefix = f"{coexist_namespace}/{prop_key}"

            def _guid(purpose: str, prefix_2b: bytes | None = None) -> bytes:
                if use_coexist_ids:
                    return coexist_uuid5_bytes(f"{seed_prefix}/{purpose}", prefix_2b=prefix_2b)
                raw_g = deterministic_guid_bytes(f"{purpose}:{prop_key}")
                return (prefix_2b + raw_g[2:]) if prefix_2b is not None else raw_g

            def _rid(purpose: str) -> int:
                if use_coexist_ids:
                    return coexist_sha256_u64(f"{seed_prefix}/{purpose}", force_odd_rid=True)
                return bsp.deterministic_u64(f"{purpose}:{prop_key}")

            pphys_bytes_list = cp.get("pphys_bytes_list") or []
            pphys_vms_list = cp.get("pphys_vms_list") or []
            prop_vfx_emitters = exp_spec.get("vfx_emitters") if isinstance(exp_spec.get("vfx_emitters"), list) else []
            prop_music_emitter = exp_spec.get("music_emitter") if isinstance(exp_spec.get("music_emitter"), dict) else {}
            wip_vfx_enabled = bool(exp_spec.get("compile_wip_vfx_in_game", False))
            has_prop_vfx_or_music = wip_vfx_enabled and (bool(prop_vfx_emitters) or bool(prop_music_emitter.get("enabled")))
            has_dynamic_lights = bool(prop_lights and noble_effect_ebx is not None and noble_nopol_raw is not None)
            use_b94_8hull = (
                bool(spec.get("use_b94_8hull_pphys", False))
                and len(pphys_bytes_list) == 8
                and not has_dynamic_lights
                and not has_prop_vfx_or_music
                and "b94_ebx118" in tpl
                and "b94_ebx121" in tpl
            )

            spec_entry = dict(spec)
            spec_entry["raw_prop_key"] = raw_prop_slug
            spec_entry["prop_key"] = prop_key
            spec_entry["item_key_str"] = item_key_str
            spec_entry["thumb_key"] = thumb_key
            spec_entry["bundle_prefix"] = bundle_stem_base
            spec_entry["raw_tex_bc7"] = cp["tex_bc7_b39"]
            spec_entry["raw_norm_bc7"] = cp.get("norm_bc7_b39")
            spec_entry["raw_msk_bc7"] = cp.get("msk_bc7_b39")
            spec_entry["tex_dims_b39"] = cp.get("tex_dims_b39")
            spec_entry["extra_bundle_ebxs"] = []
            spec_entry["extra_bundle_ress"] = []
            spec_entry["use_b94_8hull_pphys"] = bool(use_b94_8hull)
            spec_entry["id_mode"] = prop_id_mode
            spec_entry["sbd_name_mode"] = prop_sbd_name_mode
            spec_entry["register_in_res3245"] = bool(prop_register_res3245)
            prop_specs_for_toc.append(spec_entry)
            allowed_custom_ebx.add(item_path.lower())

            # 1. Visual Mesh Chunk (CHK 85) with deterministic 0xa1db prefix so sorted rank stays 61!
            mesh_chk_guid_raw = _guid("mesh_chk", prefix_2b=b"\xa1\xdb")
            mesh_chunk_guid_str = str(uuid.UUID(bytes_le=mesh_chk_guid_raw))
            spec_entry["mesh_chunk_guid_raw"] = mesh_chk_guid_raw
            forbidden_chunk_guids_raw.add(mesh_chk_guid_raw)

            ms_b = bytearray(cp["mesh_res_b39"])
            lod_ptrs = [p for p in struct.unpack_from("<6Q", ms_b, 0x30) if p != 0]
            for lp in lod_ptrs:
                ms_b[lp + 0x84 : lp + 0x94] = mesh_chk_guid_raw

            # Keep LOD0 sec[0] (0x2D0) and LOD1 sec[0] (0x450) on Bundle 39's Full Multi-Layer PBR 0x771 shader
            # (+0x28 = 0x0E300000, +0x2C = 0x03140000, sampling _c, _ny, and _msk), while LOD2 sec[0] (0x5D0)
            # uses the 0x259 proxy shader (+0x28 = 0x0A300000).
            enable_tex = bool(spec.get("enable_custom_textures", True))
            is_split_b421_ms = bool(spec.get("has_multi_mat_cutout_split", False)) and len(ms_b) == 4192
            if enable_tex:
                if use_coexist_ids:
                    rh_even, rh_odd = coexist_root_hash_pair(seed_prefix, forbidden_hi32=vanilla_root_hi32s)
                    forb_771 = set(vanilla_root_hi32s) | {(rh_even >> 32) & 0xFFFFFFFF}
                    rh771_even, rh771_odd = coexist_root_hash_pair_771(seed_prefix, forbidden_hi32=forb_771)
                else:
                    rh_seed = bsp.deterministic_u64(f"sbd_root_v5:{prop_key}") & 0xFFFFFFFF00000000
                    rh_even = rh_seed | 0x26C6D084
                    rh_odd = (rh_even + 0x256B6804) & 0xFFFFFFFFFFFFFFFF
                    rh771_seed = bsp.deterministic_u64(f"sbd_root_771:{prop_key}") & 0xFFFFFFFF00000000
                    rh771_even = rh771_seed | 0x4A3CB802
                    rh771_odd = (rh771_even + 0x256B6804) & 0xFFFFFFFFFFFFFFFF
                if is_split_b421_ms:
                    # Bundle #421 multi-section MeshSet: Sec[0] (0x2D0, 0x750, 0xBD0) -> rh771_even (0x771 Opaque),
                    # Sec[1]/Sec[2] (0x450, 0x5D0, 0x8D0, 0xA50) -> rh_even (0x273 Cutout)
                    for sec_off in (0x2D0, 0x750, 0xBD0):
                        struct.pack_into("<Q", ms_b, sec_off + 0x148, rh771_even)
                    for sec_off in (0x450, 0x5D0, 0x8D0, 0xA50):
                        struct.pack_into("<Q", ms_b, sec_off + 0x148, rh_even)
                elif spec.get("use_osk_alpha_shader", False):
                    # Single-shader 0x273 Cutout prop on Bundle #39 MeshSet:
                    # Set +0x28 = 0x0A300000, +0x2C = 0x03140001 (material_index=1 -> mm1_ig SurfaceShaderId=6),
                    # bind LOD0/1/2 Sec[0] to rh771_even, and disable Cat[3] (ZOnly) & Cat[4] (Shadow) like FightFence (#58)
                    for sec_off in (0x2D0, 0x450, 0x5D0):
                        struct.pack_into("<Q", ms_b, sec_off + 0x148, rh771_even)
                        struct.pack_into("<I", ms_b, sec_off + 0x28, 0x0A300000)
                        struct.pack_into("<I", ms_b, sec_off + 0x2C, 0x03140001)
                    for lp in lod_ptrs:
                        for ci in (3, 4):
                            struct.pack_into("<I", ms_b, lp + 0x10 + 0x14 + ci * 12, 0)
                else:
                    for sec_off in (0x2D0, 0x450):
                        struct.pack_into("<Q", ms_b, sec_off + 0x148, rh771_even)
                    struct.pack_into("<Q", ms_b, 0x5D0 + 0x148, rh_even)
            else:
                rh_even = 0xF136CD5C26C6D084
                rh_odd = 0xF136CD5C4C323888
                rh771_even = 0x5959DE714A3CB802
                rh771_odd = 0x5959DE716FA82006
            spec_entry["rh_even"] = rh_even
            spec_entry["rh_odd"] = rh_odd
            spec_entry["rh771_even"] = rh771_even
            spec_entry["rh771_odd"] = rh771_odd
            spec_entry["unique_sub0"] = (
                coexist_sha256_u64(f"{seed_prefix}/mat_0/sub_0", force_odd_rid=True)
                if use_coexist_ids
                else bsp.deterministic_u64(f"sbd_subh:{prop_key}")
            )
            spec_entry["unique_sub1"] = (
                coexist_sha256_u64(f"{seed_prefix}/mat_771/sub_1", force_odd_rid=True)
                if use_coexist_ids
                else bsp.deterministic_u64(f"sbd_subh771:{prop_key}")
            )

            standalone_ms_name = f"items/customprops/{prop_key}_mesh"
            standalone_ms_rid = _rid("ms_rid")
            ms_res_hash = fnv1_32_lower(standalone_ms_name)
            struct.pack_into("<I", ms_b, 120, ms_res_hash)
            if callable(cp.get("custom_ms_hook")):
                ms_b = bytearray(cp["custom_ms_hook"](ms_b, spec_entry))

            fbmod_records.append({
                "rtype": 3,
                "name": mesh_chunk_guid_str,
                "raw": cp["mesh_chk_b39"],
                "is_added": True,
                "tag": "reskate-cosmetic-texture:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_hash": ms_res_hash,
            })
            fbmod_records.append({
                "rtype": 2,
                "name": standalone_ms_name.lower(),
                "raw": bytes(ms_b),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x49B156D4,
                "res_rid": standalone_ms_rid,
                "res_meta": tpl.get("b421_ms_meta", tpl["ress"][11]["res_meta"]) if is_split_b421_ms else tpl["ress"][11]["res_meta"],
            })

            # EBX 27: Standalone MeshAsset EBX
            ma_fg = _guid("ma_fg", prefix_2b=b"\xbd\xdf")
            ma_ig = _guid("ma_ig", prefix_2b=b"\x4c\x67")
            mm0_ig = _guid("mm0_ig", prefix_2b=b"\xbb\xaa")
            mm1_ig = _guid("mm1_ig", prefix_2b=b"\xc9\xad")
            spec_entry["ma_fg"] = ma_fg
            spec_entry["ma_ig"] = ma_ig
            spec_entry["mm0_ig"] = mm0_ig
            spec_entry["mm1_ig"] = mm1_ig

            form_ma, chunks_ma = demux_riff(tpl["ebx27"])
            cmap_ma = dict(chunks_ma)
            ma_ebxd = bytearray(cmap_ma[b"EBXD"])
            ma_efix = bytearray(cmap_ma[b"EFIX"])
            ma_efix[0:16] = ma_fg
            ma_ebxd[12:28] = ma_ig
            struct.pack_into("<Q", ma_ebxd, 140, standalone_ms_rid)
            struct.pack_into("<I", ma_ebxd, 228, ms_res_hash)
            ma_ebxd[260:276] = mm0_ig
            ma_ebxd[460:476] = mm1_ig
            bsp.patch_ebx_string_inplace(ma_ebxd, 732, 124, standalone_ms_name)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_ms_name.lower(),
                "raw": remux_riff(form_ma, [(cid, ma_ebxd if cid == b"EBXD" else (ma_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_ma]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 28: Standalone EcsMeshComponentAsset EBX
            standalone_mc_name = f"items/customprops/{prop_key}_mc"
            standalone_mc_fg = _guid("mc_fg", prefix_2b=(b"\xd1\x0a" if use_b94_8hull else b"\x53\x71"))
            form_mc, chunks_mc = demux_riff(cp["mesh_mc_b39"])
            cmap_mc = dict(chunks_mc)
            mc_ebxd = bytearray(cmap_mc[b"EBXD"])
            mc_efix = bytearray(cmap_mc[b"EFIX"])
            mc_efix[0:16] = standalone_mc_fg
            mc_ebxd[12:28] = standalone_mc_fg
            mc_efix[128:160] = ma_fg + ma_ig
            bsp.patch_ebx_string_inplace(mc_ebxd, 172, 88, standalone_mc_name)
            struct.pack_into("<q", mc_ebxd, 52, 120)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_mc_name.lower(),
                "raw": remux_riff(form_mc, [(cid, mc_ebxd if cid == b"EBXD" else (mc_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_mc]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 29: Standalone DingoOutline EBX
            standalone_outl_name = f"items/customprops/{prop_key}_outl"
            standalone_outl_fg = _guid("outl_fg", prefix_2b=(b"\x99\x0f" if use_b94_8hull else b"\x73\x00"))
            form_ol, chunks_ol = demux_riff(tpl["ebx29"])
            cmap_ol = dict(chunks_ol)
            ol_ebxd = bytearray(cmap_ol[b"EBXD"])
            ol_efix = bytearray(cmap_ol[b"EFIX"])
            ol_efix[0:16] = standalone_outl_fg
            ol_ebxd[12:28] = standalone_outl_fg
            ol_efix[104:136] = ma_fg + ma_ig
            bsp.patch_ebx_string_inplace(ol_ebxd, 124, 79, standalone_outl_name)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_outl_name.lower(),
                "raw": remux_riff(form_ol, [(cid, ol_ebxd if cid == b"EBXD" else (ol_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_ol]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 38: Standalone MeshVariationDatabase EBX (Slot 38)
            standalone_mvdb_name = f"items/customprops/{prop_key}_bundle/meshvariationdb_win32"
            standalone_mvdb_internal = f"items/customprops/{prop_key}_bundle/MeshVariationDb_Win32"
            mvdb_fg = _guid("mvdb_fg")
            mvdb_ig = _guid("mvdb_ig")
            form_mv, chunks_mv = demux_riff(tpl["ebx38"])
            cmap_mv = dict(chunks_mv)
            mv_ebxd = bytearray(cmap_mv[b"EBXD"])
            mv_efix = bytearray(cmap_mv[b"EFIX"])
            mv_efix[0:16] = mvdb_fg
            mv_ebxd[12:28] = mvdb_ig
            mv_efix[372:404] = ma_fg + ma_ig
            mv_efix[404:436] = ma_fg + mm0_ig
            mv_efix[436:468] = ma_fg + mm1_ig
            bsp.patch_ebx_string_inplace(mv_ebxd, 1308, 113, standalone_mvdb_internal)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_mvdb_name.lower(),
                "raw": remux_riff(form_mv, [(cid, mv_ebxd if cid == b"EBXD" else (mv_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_mv]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # 2. Texture (CHK 84 + RES 9 + EBX 25 + RES 18 ShaderBlockDepot)
            tex_chk_guid_raw = _guid("tex_chk", prefix_2b=b"\x9e\x8f")
            tex_chunk_guid_str = str(uuid.UUID(bytes_le=tex_chk_guid_raw))
            spec_entry["tex_chunk_guid_raw"] = tex_chk_guid_raw
            # Keep custom texture EBX, RES, and BC7 chunk in BOTH Win32/items.toc (cas_main_sharedbundle)
            # AND the prop's standalone bundle in Win32/buildkititems.toc (matching NOBLE1337-Noble_Lights!)
            standalone_tex_name = f"items/customprops/{prop_key}_c"
            standalone_tex_rid = _rid("tex_rid")
            tex_res_hash = fnv1_32_lower(standalone_tex_name)
            tex_h64 = bsp.frostbite_fnv1_64_lower(standalone_tex_name)
            allowed_custom_ebx.add(standalone_tex_name.lower())
            allowed_custom_res.add(standalone_tex_name.lower())

            fbmod_records.append({
                "rtype": 3,
                "name": tex_chunk_guid_str,
                "raw": cp["tex_bc7_b39"],
                "is_added": True,
                "tag": "reskate-cosmetic-texture:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_hash": tex_res_hash,
            })

            tex_w_c, tex_h_c, n_mips_c, mip_sizes_c, total_sz_c = _get_bc7_mip_spec_from_bytes_len(
                len(cp["tex_bc7_b39"]), hint_dims=cp.get("tex_dims_b39")
            )
            padded_mips_c = (mip_sizes_c + [0] * 15)[:15]
            res9_b = bytearray(tpl["res9"])
            struct.pack_into("<II", res9_b, 0x00, 0x00100080, 0x001400A0)
            struct.pack_into("<HH", res9_b, 0x16, tex_w_c, tex_h_c)
            res9_b[0x1E:0x22] = bytes([n_mips_c, 0, 0, n_mips_c])
            res9_b[0x24:0x28] = b"\x00\x00\x00\x00"
            res9_b[0x28:0x38] = tex_chk_guid_raw
            struct.pack_into("<15I", res9_b, 0x38, *padded_mips_c)
            struct.pack_into("<I", res9_b, 0x74, total_sz_c)
            struct.pack_into("<Q", res9_b, 0x78, tex_h64)
            res9_b[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")

            fbmod_records.append({
                "rtype": 2,
                "name": standalone_tex_name.lower(),
                "raw": bytes(res9_b),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x6BDE20BA,
                "res_rid": standalone_tex_rid,
                "res_meta": tpl["ress"][9]["res_meta"],
            })

            tex_fg = _guid("tex_fg")
            tex_ig = _guid("tex_ig")
            spec_entry["tex_fg"] = tex_fg
            spec_entry["tex_ig"] = tex_ig
            form_tx, chunks_tx = demux_riff(tpl["ebx25"])
            cmap_tx = dict(chunks_tx)
            tx_ebxd = bytearray(cmap_tx[b"EBXD"])
            tx_efix = bytearray(cmap_tx[b"EFIX"])
            tx_efix[0:16] = tex_fg
            tx_ebxd[12:28] = tex_ig
            struct.pack_into("<Q", tx_ebxd, 60, standalone_tex_rid)
            struct.pack_into("<HH", tx_ebxd, 76, tex_w_c, tex_h_c)
            bsp.patch_ebx_string_inplace(tx_ebxd, 124, 69, standalone_tex_name)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_tex_name.lower(),
                "raw": remux_riff(form_tx, [(cid, tx_ebxd if cid == b"EBXD" else (tx_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_tx]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # 2a. Custom Normal Map (_n -> EBX 26, RES 8, CHK 83, cmeta rank 35, Linear BC7 0x42) if present
            norm_fg = None
            norm_ig = None
            if enable_tex and cp.get("norm_bc7_b39") is not None:
                norm_chk_guid_raw = _guid("norm_chk", prefix_2b=b"\x60\x95")
                norm_chunk_guid_str = str(uuid.UUID(bytes_le=norm_chk_guid_raw))
                spec_entry["norm_chunk_guid_raw"] = norm_chk_guid_raw
                standalone_norm_name = f"items/customprops/{prop_key}_n"
                standalone_norm_rid = _rid("norm_rid")
                norm_res_hash = fnv1_32_lower(standalone_norm_name)
                norm_h64 = bsp.frostbite_fnv1_64_lower(standalone_norm_name)
                allowed_custom_ebx.add(standalone_norm_name.lower())
                allowed_custom_res.add(standalone_norm_name.lower())

                fbmod_records.append({
                    "rtype": 3,
                    "name": norm_chunk_guid_str,
                    "raw": cp["norm_bc7_b39"],
                    "is_added": True,
                    "tag": "reskate-cosmetic-texture:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                    "res_hash": norm_res_hash,
                })

                tex_w_n, tex_h_n, n_mips_n, mip_sizes_n, total_sz_n = _get_bc7_mip_spec_from_bytes_len(
                    len(cp["norm_bc7_b39"]), hint_dims=cp.get("tex_dims_b39")
                )
                padded_mips_n = (mip_sizes_n + [0] * 15)[:15]
                res8_b = bytearray(tpl["res9"])
                res8_b[0x0C] = 0x42  # BC7_UNORM Linear for Normal map
                struct.pack_into("<II", res8_b, 0x00, 0x00100080, 0x001400A0)
                struct.pack_into("<HH", res8_b, 0x16, tex_w_n, tex_h_n)
                res8_b[0x1E:0x22] = bytes([n_mips_n, 0, 0, n_mips_n])
                res8_b[0x24:0x28] = b"\x00\x00\x00\x00"
                res8_b[0x28:0x38] = norm_chk_guid_raw
                struct.pack_into("<15I", res8_b, 0x38, *padded_mips_n)
                struct.pack_into("<I", res8_b, 0x74, total_sz_n)
                struct.pack_into("<Q", res8_b, 0x78, norm_h64)
                res8_b[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")

                fbmod_records.append({
                    "rtype": 2,
                    "name": standalone_norm_name.lower(),
                    "raw": bytes(res8_b),
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                    "res_type": 0x6BDE20BA,
                    "res_rid": standalone_norm_rid,
                    "res_meta": tpl["ress"][9]["res_meta"],
                })

                norm_fg = _guid("norm_fg")
                norm_ig = _guid("norm_ig")
                spec_entry["norm_fg"] = norm_fg
                spec_entry["norm_ig"] = norm_ig
                form_nm, chunks_nm = demux_riff(tpl["ebx25"])
                cmap_nm = dict(chunks_nm)
                nm_ebxd = bytearray(cmap_nm[b"EBXD"])
                nm_efix = bytearray(cmap_nm[b"EFIX"])
                nm_efix[0:16] = norm_fg
                nm_ebxd[12:28] = norm_ig
                struct.pack_into("<Q", nm_ebxd, 60, standalone_norm_rid)
                struct.pack_into("<HH", nm_ebxd, 76, tex_w_n, tex_h_n)
                bsp.patch_ebx_string_inplace(nm_ebxd, 124, 69, standalone_norm_name)
                fbmod_records.append({
                    "rtype": 1,
                    "name": standalone_norm_name.lower(),
                    "raw": remux_riff(form_nm, [(cid, nm_ebxd if cid == b"EBXD" else (nm_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_nm]),
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                })

            # 2b. Custom Mask / Roughness / Metallic Map (_msk -> EBX 24, RES 10, CHK 97, cmeta rank 50, Linear BC7 0x42)
            msk_fg = None
            msk_ig = None
            if enable_tex and cp.get("msk_bc7_b39") is not None:
                msk_chk_guid_raw = _guid("msk_chk", prefix_2b=b"\x7e\x1c")
                msk_chunk_guid_str = str(uuid.UUID(bytes_le=msk_chk_guid_raw))
                spec_entry["msk_chunk_guid_raw"] = msk_chk_guid_raw
                standalone_msk_name = f"items/customprops/{prop_key}_msk"
                standalone_msk_rid = _rid("msk_rid")
                msk_res_hash = fnv1_32_lower(standalone_msk_name)
                msk_h64 = bsp.frostbite_fnv1_64_lower(standalone_msk_name)
                allowed_custom_ebx.add(standalone_msk_name.lower())
                allowed_custom_res.add(standalone_msk_name.lower())

                fbmod_records.append({
                    "rtype": 3,
                    "name": msk_chunk_guid_str,
                    "raw": cp["msk_bc7_b39"],
                    "is_added": True,
                    "tag": "reskate-cosmetic-texture:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                    "res_hash": msk_res_hash,
                })

                tex_w_m, tex_h_m, n_mips_m, mip_sizes_m, total_sz_m = _get_bc7_mip_spec_from_bytes_len(
                    len(cp["msk_bc7_b39"]), hint_dims=cp.get("tex_dims_b39")
                )
                padded_mips_m = (mip_sizes_m + [0] * 15)[:15]
                res10_b = bytearray(tpl["res9"])
                res10_b[0x0C] = 0x42  # BC7_UNORM Linear for Mask (R=Metallic, G=Smoothness, B=AO)
                struct.pack_into("<II", res10_b, 0x00, 0x00100080, 0x001400A0)
                struct.pack_into("<HH", res10_b, 0x16, tex_w_m, tex_h_m)
                res10_b[0x1E:0x22] = bytes([n_mips_m, 0, 0, n_mips_m])
                res10_b[0x24:0x28] = b"\x00\x00\x00\x00"
                res10_b[0x28:0x38] = msk_chk_guid_raw
                struct.pack_into("<15I", res10_b, 0x38, *padded_mips_m)
                struct.pack_into("<I", res10_b, 0x74, total_sz_m)
                struct.pack_into("<Q", res10_b, 0x78, msk_h64)
                res10_b[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")

                fbmod_records.append({
                    "rtype": 2,
                    "name": standalone_msk_name.lower(),
                    "raw": bytes(res10_b),
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                    "res_type": 0x6BDE20BA,
                    "res_rid": standalone_msk_rid,
                    "res_meta": tpl["ress"][9]["res_meta"],
                })

                msk_fg = _guid("msk_fg")
                msk_ig = _guid("msk_ig")
                spec_entry["msk_fg"] = msk_fg
                spec_entry["msk_ig"] = msk_ig
                form_mk, chunks_mk = demux_riff(tpl["ebx25"])
                cmap_mk = dict(chunks_mk)
                mk_ebxd = bytearray(cmap_mk[b"EBXD"])
                mk_efix = bytearray(cmap_mk[b"EFIX"])
                mk_efix[0:16] = msk_fg
                mk_ebxd[12:28] = msk_ig
                struct.pack_into("<Q", mk_ebxd, 60, standalone_msk_rid)
                struct.pack_into("<HH", mk_ebxd, 76, tex_w_m, tex_h_m)
                bsp.patch_ebx_string_inplace(mk_ebxd, 124, 69, standalone_msk_name)
                fbmod_records.append({
                    "rtype": 1,
                    "name": standalone_msk_name.lower(),
                    "raw": remux_riff(form_mk, [(cid, mk_ebxd if cid == b"EBXD" else (mk_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_mk]),
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                })

            # RES 18: Standalone ShaderBlockDepot
            if prop_sbd_name_mode == "vanilla":
                sbd_u64 = (
                    coexist_sha256_u64(f"{seed_prefix}/sbd_num", force_odd_rid=False)
                    if use_coexist_ids
                    else bsp.deterministic_u64(f"sbd_num:{prop_key}")
                ) & ~3
                standalone_sbd_name = f"{bundle_stem_base}/{prop_key}_bundle_win32_shaderstate/shaderblockdepot_{sbd_u64}"
            else:
                standalone_sbd_name = f"items/customprops/{prop_key}_sbd"
            spec_entry["sbd_res_name"] = standalone_sbd_name
            standalone_sbd_rid = _rid("sbd_rid")
            sbd_b = bytearray(patch_shader_block_depot_rgb(tpl["res18"], prev["avg_rgb"], has_custom_tex=enable_tex))
            sbd_meta_out = tpl["ress"][18]["res_meta"]

            if enable_tex:
                # A) In Dir[0], the 0x259-byte LOD2 Proxy Shader block (0x0070..0x02C9 in Bundle 39 SBD):
                # 1) Point the UV Atlas Albedo texture slot at 0x085..0x0A5 (phash=0x3F11) to tex_ig + tex_fg!
                sbd_b[0x085:0x0A5] = tex_ig + tex_fg
                # 2) Write per-class Smoothness (0x0B9, phash=0x9D59, scaled to vanilla [0.0..0.35] curve),
                #    Metalness (0x145, phash=0xB162), and Albedo Tint (0x269, phash=0x1848) for vc_layer 0..7:
                rough_mult = float(spec.get("roughness_multiplier", 0.45))
                metal_mult = float(spec.get("metallic_multiplier", 0.0))
                tint_default = [float(x) for x in spec.get("albedo_tint", [1.0, 1.0, 1.0])]
                slot_classes = spec.get("slot_pbr_classes") or []
                for k_idx in range(8):
                    cls_info = slot_classes[k_idx] if k_idx < len(slot_classes) else (slot_classes[0] if slot_classes else {})
                    rough_k = float(cls_info.get("roughness", rough_mult))
                    rough_k = max(0.0, min(1.0, rough_k))
                    em_s_k = float(cls_info.get("emission_strength", 0.0) or 0.0)
                    # Vanilla 0x259 phash=0x9D59 spans [0.0 .. 0.366] (0.0=Matte, 0.10=Satin, 0.323=Polished Chrome)
                    smooth_259 = 0.35 if em_s_k > 0.01 else (0.0 if rough_k >= 0.98 else ((1.0 - rough_k) ** 2) * 0.35)
                    metal_k = float(cls_info.get("metallic", max(0.0, min(1.0, metal_mult))))
                    metal_k = max(0.0, min(1.0, metal_k))
                    tint_k = cls_info.get("emission_color") if em_s_k > 0.01 else (cls_info.get("tint") or tint_default)
                    if not tint_k or len(tint_k) < 3:
                        tint_k = tint_default
                    hdr_mult_259 = max(3.5, min(25.0, em_s_k)) if em_s_k > 0.01 else 1.0
                    struct.pack_into("<3f", sbd_b, 0x0B9 + k_idx * 12, smooth_259, smooth_259, smooth_259)
                    struct.pack_into("<3f", sbd_b, 0x145 + k_idx * 12, metal_k, metal_k, metal_k)
                    struct.pack_into(
                        "<3f",
                        sbd_b,
                        0x269 + k_idx * 12,
                        float(tint_k[0]) * hdr_mult_259,
                        float(tint_k[1]) * hdr_mult_259,
                        float(tint_k[2]) * hdr_mult_259,
                    )

                # B) Bind 0x771 Full Multi-Layer PBR Shader block parameters at 0x02C9..0x0A3A:
                active_norm_pair = (norm_ig + norm_fg) if (norm_ig is not None and norm_fg is not None) else (orig_ig81 + orig_fg81)
                active_msk_pair = (msk_ig + msk_fg) if (msk_ig is not None and msk_fg is not None) else (orig_ig24 + orig_fg24)
                for c_off in (0x3A2, 0x4F9, 0x3D3, 0x404):
                    sbd_b[c_off : c_off + 32] = tex_ig + tex_fg
                for ny_off in (0x4C8, 0x52A, 0x371, 0x5EE):
                    sbd_b[ny_off : ny_off + 32] = active_norm_pair
                for msk_off in (0x5BD, 0x497, 0x30F, 0x340):
                    sbd_b[msk_off : msk_off + 32] = active_msk_pair

                # Scale 0x771 per-layer Tints by 0.5 (vanilla neutral 1.0x albedo brightness in 0x771 is [0.5, 0.5, 0.5]);
                # if a layer is an Emissive Glow slot (emission_strength > 0.01), boost its float3 tint into HDR (> 1.0)
                # so the glowing surface visibly self-illuminates and triggers post-process bloom in-game!
                layer_tint_offs = [
                    (0, (0x7E2, 0x802, 0x822, 0x842)),  # Layer 0 (0x3A68)
                    (1, (0x972, 0x992)),                # Layer 1 (0xDCB0)
                    (2, (0x9EE, 0xA0E)),                # Layer 2 (0x14EE)
                    (3, (0x75A, 0x77A)),                # Layer 3 (0xBE3C)
                ]
                for l_idx, t_offs in layer_tint_offs:
                    cls_l = slot_classes[l_idx] if l_idx < len(slot_classes) else (slot_classes[0] if slot_classes else {})
                    em_s_l = float(cls_l.get("emission_strength", 0.0) or 0.0)
                    t_l = cls_l.get("emission_color") if em_s_l > 0.01 else (cls_l.get("tint") or tint_default)
                    if not t_l or len(t_l) < 3:
                        t_l = tint_default
                    hdr_mult_771 = max(3.5, min(25.0, em_s_l)) if em_s_l > 0.01 else 1.0
                    tr = 0.5 * float(t_l[0]) * hdr_mult_771
                    tg = 0.5 * float(t_l[1]) * hdr_mult_771
                    tb = 0.5 * float(t_l[2]) * hdr_mult_771
                    for t_off in t_offs:
                        struct.pack_into("<3f", sbd_b, t_off, tr, tg, tb)

                # Build the 627-byte (0x273) Native Category 0 Alpha Cutout shader block (from Bundle #58 FightFence / #421 HockeyNet)
                blk_273 = bytearray(bytes.fromhex(CUTOUT_OSK_273_TEMPLATE_HEX))
                blk_273[0x015 : 0x035] = tex_ig + tex_fg
                blk_273[0x046 : 0x066] = active_norm_pair
                blk_273[0x077 : 0x097] = active_msk_pair
                cut_idx = min(int(spec.get("cutout_slot_idx", 0)), len(slot_classes) - 1) if slot_classes else 0
                cls_cut = slot_classes[cut_idx] if slot_classes else {}
                t_cut = cls_cut.get("tint") or tint_default
                if not t_cut or len(t_cut) < 3:
                    t_cut = tint_default
                for t273_off in (0x0DF, 0x147, 0x17F):
                    struct.pack_into(
                        "<3f",
                        blk_273,
                        t273_off,
                        0.5 * float(t_cut[0]),
                        0.5 * float(t_cut[1]),
                        0.5 * float(t_cut[2]),
                    )

                if spec.get("use_osk_alpha_shader", False):
                    sbd_b[0x02C9 : 0x02C9 + len(blk_273)] = blk_273
                    sbd_b[0x02C9 + len(blk_273) : 0x0A3A] = b"\x00" * (0x0A3A - (0x02C9 + len(blk_273)))
                    struct.pack_into("<Q", sbd_b, 0x0A68, len(blk_273))

            if enable_tex:
                if is_split_b421_ms:
                    # Multi-material Opaque (0x771) + Cutout (0x273) SBD:
                    # Dir[0] @ 0x0070..0x02E3 (627B) = blk_273 (rh_even/odd), Dir[1] @ 0x02E3..0x0A54 (1905B) = blk_771 (rh771_even/odd)
                    blk_771 = bytes(sbd_b[0x02C9:0x0A3A])
                    hdr_70 = bytearray(sbd_b[:0x0070])
                    struct.pack_into("<QQ", hdr_70, 0x10, rh_even, 0x0A60)
                    struct.pack_into("<QQ", hdr_70, 0x20, rh_odd, 0x0A60)
                    struct.pack_into("<QQ", hdr_70, 0x30, rh771_even, 0x0A78)
                    struct.pack_into("<QQ", hdr_70, 0x40, rh771_odd, 0x0A78)
                    struct.pack_into("<Q", hdr_70, 0x58, 0x0A60)
                    struct.pack_into("<Q", hdr_70, 0x68, 0x0A60)
                    dir_buf = struct.pack(
                        "<QQQQQQ",
                        0x0070,
                        spec_entry["unique_sub0"],
                        len(blk_273),
                        0x02E3,
                        spec_entry["unique_sub1"],
                        len(blk_771),
                    )
                    reloc_buf = struct.pack("<9I", 0x0A60, 0x18, 0x28, 0x0A78, 0x38, 0x48, 0x58, 0x68, 0)
                    if callable(cp.get("custom_split_sbd_hook")):
                        blk_273, blk_771 = cp["custom_split_sbd_hook"](bytearray(blk_273), bytearray(blk_771), spec_entry)
                    sbd_b = bytearray(bytes(hdr_70) + bytes(blk_273) + bytes(blk_771) + (b"\x00" * 12) + dir_buf + reloc_buf)
                    sbd_meta_out = struct.pack("<4I", 0x0A90, 36, 0x01190003, 6)
                    spec_entry["sbd_259_block"] = bytes(blk_273)
                    spec_entry["sbd_771_block"] = bytes(blk_771)
                else:
                    struct.pack_into("<Q", sbd_b, 0x10, rh_even)
                    struct.pack_into("<Q", sbd_b, 0x20, rh_odd)
                    struct.pack_into("<Q", sbd_b, 0x30, rh771_even)
                    struct.pack_into("<Q", sbd_b, 0x40, rh771_odd)
                    struct.pack_into("<Q", sbd_b, 0x0A48, spec_entry["unique_sub0"])
                    struct.pack_into("<Q", sbd_b, 0x0A60, spec_entry["unique_sub1"])
                    if callable(cp.get("custom_sbd_hook")):
                        sbd_b = bytearray(cp["custom_sbd_hook"](sbd_b, spec_entry))
                    spec_entry["sbd_259_block"] = bytes(sbd_b[0x0070:0x02C9])
                    spec_entry["sbd_771_block"] = bytes(sbd_b[0x02C9:0x0A3A])

            fbmod_records.append({
                "rtype": 2,
                "name": standalone_sbd_name.lower(),
                "raw": bytes(sbd_b),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x73312045,
                "res_rid": standalone_sbd_rid,
                "res_meta": sbd_meta_out,
            })

            # 2c. Thumbnails (256x256 & 768x768) in items.toc
            t_sm_guid_raw = _guid("thumb_sm_chk", prefix_2b=b"\xff\xf0")
            t_sm_guid_str = str(uuid.UUID(bytes_le=t_sm_guid_raw))
            tool_sm_name = f"Thumbnail/Tool/{thumb_key}"
            tool_sm_hash = fnv1_32_lower(tool_sm_name)
            tool_sm_h64 = bsp.frostbite_fnv1_64_lower(tool_sm_name)
            tool_sm_rid = _rid("thumb_sm_rid")
            allowed_custom_res.add(tool_sm_name.lower())
            allowed_custom_ebx.add(tool_sm_name.lower())

            fbmod_records.append({
                "rtype": 3,
                "name": t_sm_guid_str,
                "raw": cp["thumb_sm_bc7"],
                "is_added": True,
                "tag": "reskate-cosmetic-texture:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_hash": tool_sm_hash,
            })
            res_tsm = bytearray(tpl["thumb_tool_sm_res"])
            res_tsm[0x28:0x38] = t_sm_guid_raw
            struct.pack_into("<Q", res_tsm, 0x78, tool_sm_h64)
            fbmod_records.append({
                "rtype": 2,
                "name": tool_sm_name.lower(),
                "raw": bytes(res_tsm),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x6BDE20BA,
                "res_rid": tool_sm_rid,
                "res_meta": tpl["thumb_tool_sm_meta"],
            })
            tool_sm_fg = _guid("tool_sm_fg")
            tool_sm_ig = _guid("tool_sm_ig")
            form_tsm, chunks_tsm = demux_riff(tpl["thumb_tool_sm_ebx"])
            cmap_tsm = dict(chunks_tsm)
            tsm_ed = bytearray(cmap_tsm[b"EBXD"])
            tsm_ef = bytearray(cmap_tsm[b"EFIX"])
            tsm_ef[0:16] = tool_sm_fg
            tsm_ed[12:28] = tool_sm_ig
            struct.pack_into("<Q", tsm_ed, 60, tool_sm_rid)
            bsp.patch_ebx_string_inplace(tsm_ed, 124, 60, tool_sm_name)
            fbmod_records.append({
                "rtype": 1,
                "name": tool_sm_name.lower(),
                "raw": remux_riff(form_tsm, [(cid, tsm_ed if cid == b"EBXD" else (tsm_ef if cid == b"EFIX" else pl)) for cid, pl in chunks_tsm]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })
            cdn_sm_name = f"Thumbnail/Cdn/img_{thumb_key}"
            cdn_sm_fg = _guid("cdn_sm_fg")
            cdn_sm_ig = _guid("cdn_sm_ig")
            allowed_custom_ebx.add(cdn_sm_name.lower())
            form_csm, chunks_csm = demux_riff(tpl["thumb_cdn_sm_ebx"])
            cmap_csm = dict(chunks_csm)
            csm_ed = bytearray(cmap_csm[b"EBXD"])
            csm_ef = bytearray(cmap_csm[b"EFIX"])
            csm_ef[0:16] = cdn_sm_fg
            csm_ed[12:28] = cdn_sm_ig
            csm_ef[72:104] = tool_sm_fg + tool_sm_ig
            bsp.patch_ebx_string_inplace(csm_ed, 124, 63, cdn_sm_name)
            fbmod_records.append({
                "rtype": 1,
                "name": cdn_sm_name.lower(),
                "raw": remux_riff(form_csm, [(cid, csm_ed if cid == b"EBXD" else (csm_ef if cid == b"EFIX" else pl)) for cid, pl in chunks_csm]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # 768x768 Thumbnail
            t_lg_guid_raw = _guid("thumb_lg_chk", prefix_2b=b"\xff\xf1")
            t_lg_guid_str = str(uuid.UUID(bytes_le=t_lg_guid_raw))
            tool_lg_name = f"Thumbnail/Tool/{thumb_key}_LRG"
            tool_lg_hash = fnv1_32_lower(tool_lg_name)
            tool_lg_h64 = bsp.frostbite_fnv1_64_lower(tool_lg_name)
            tool_lg_rid = _rid("thumb_lg_rid")
            allowed_custom_res.add(tool_lg_name.lower())
            allowed_custom_ebx.add(tool_lg_name.lower())

            fbmod_records.append({
                "rtype": 3,
                "name": t_lg_guid_str,
                "raw": cp["thumb_lg_bc7"],
                "is_added": True,
                "tag": "reskate-cosmetic-texture:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_hash": tool_lg_hash,
            })
            res_tlg = bytearray(tpl["thumb_tool_lg_res"])
            res_tlg[0x28:0x38] = t_lg_guid_raw
            struct.pack_into("<Q", res_tlg, 0x78, tool_lg_h64)
            fbmod_records.append({
                "rtype": 2,
                "name": tool_lg_name.lower(),
                "raw": bytes(res_tlg),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x6BDE20BA,
                "res_rid": tool_lg_rid,
                "res_meta": tpl["thumb_tool_lg_meta"],
            })
            tool_lg_fg = _guid("tool_lg_fg")
            tool_lg_ig = _guid("tool_lg_ig")
            form_tlg, chunks_tlg = demux_riff(tpl["thumb_tool_lg_ebx"])
            cmap_tlg = dict(chunks_tlg)
            tlg_ed = bytearray(cmap_tlg[b"EBXD"])
            tlg_ef = bytearray(cmap_tlg[b"EFIX"])
            tlg_ef[0:16] = tool_lg_fg
            tlg_ed[12:28] = tool_lg_ig
            struct.pack_into("<Q", tlg_ed, 60, tool_lg_rid)
            bsp.patch_ebx_string_inplace(tlg_ed, 124, 64, tool_lg_name)
            fbmod_records.append({
                "rtype": 1,
                "name": tool_lg_name.lower(),
                "raw": remux_riff(form_tlg, [(cid, tlg_ed if cid == b"EBXD" else (tlg_ef if cid == b"EFIX" else pl)) for cid, pl in chunks_tlg]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })
            cdn_lg_name = f"Thumbnail/Cdn/img_{thumb_key}_LRG"
            cdn_lg_fg = _guid("cdn_lg_fg")
            cdn_lg_ig = _guid("cdn_lg_ig")
            allowed_custom_ebx.add(cdn_lg_name.lower())
            form_clg, chunks_clg = demux_riff(tpl["thumb_cdn_lg_ebx"])
            cmap_clg = dict(chunks_clg)
            clg_ed = bytearray(cmap_clg[b"EBXD"])
            clg_ef = bytearray(cmap_clg[b"EFIX"])
            clg_ef[0:16] = cdn_lg_fg
            clg_ed[12:28] = cdn_lg_ig
            clg_ef[72:104] = tool_lg_fg + tool_lg_ig
            bsp.patch_ebx_string_inplace(clg_ed, 124, 67, cdn_lg_name)
            fbmod_records.append({
                "rtype": 1,
                "name": cdn_lg_name.lower(),
                "raw": remux_riff(form_clg, [(cid, clg_ed if cid == b"EBXD" else (clg_ef if cid == b"EFIX" else pl)) for cid, pl in chunks_clg]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # 3. Havok Collision (RES 17 & EBX 31 Skateable + RES 16 & EBX 32 Placement Proxy)
            dom_packed = int(spec["packed"])
            if col_variant == "exact_2a" and cp.get("exact_trimesh_phys_bytes") is not None:
                chosen_phys_bytes = cp["exact_trimesh_phys_bytes"]
                chosen_shape_cnt = cp["exact_trimesh_cnt"]
                shape_packed_list = spec.get("shape_packed_list") or [dom_packed] * max(1, chosen_shape_cnt)
            elif cp.get("multishape_phys_bytes") is not None:
                chosen_phys_bytes = cp["multishape_phys_bytes"]
                chosen_shape_cnt = cp["multishape_cnt"]
                shape_packed_list = spec.get("shape_packed_list") or [dom_packed] * max(1, chosen_shape_cnt)
            else:
                chosen_phys_bytes = cp["raw_phys_fitted"]
                chosen_shape_cnt = struct.unpack_from("<I", chosen_phys_bytes, 12)[0]
                shape_packed_list = [dom_packed] * max(1, chosen_shape_cnt)

            standalone_col_name = f"items/customprops/{prop_key}_col"
            standalone_col_rid = _rid("col_rid")
            fbmod_records.append({
                "rtype": 2,
                "name": standalone_col_name.lower(),
                "raw": chosen_phys_bytes,
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x41759364,
                "res_rid": standalone_col_rid,
                "res_meta": tpl["ress"][17]["res_meta"],
            })

            standalone_pphys_name = f"items/customprops/{prop_key}_pphys"
            standalone_pphys_rid = _rid("pphys_rid")
            pphys_0_raw = pphys_bytes_list[0] if use_b94_8hull else cp["raw_proxy_phys"]
            fbmod_records.append({
                "rtype": 2,
                "name": standalone_pphys_name.lower(),
                "raw": pphys_0_raw,
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
                "res_type": 0x41759364,
                "res_rid": standalone_pphys_rid,
                "res_meta": tpl["ress"][16]["res_meta"],
            })

            # EBX 31: Skateable EcsPhysics
            patched_phys_ebx = patch_physics_ebx_volume_moments(
                tpl["ebx31"], prev["volume_moments_16f"], shape_cnt=chosen_shape_cnt, is_skateable_col=True
            )
            standalone_phys_ebx_name = f"items/customprops/{prop_key}_physics"
            standalone_phys_fg = _guid("phys_fg", prefix_2b=(b"\xeb\xf4" if use_b94_8hull else b"\xc3\x59"))
            form_ph, chunks_ph = demux_riff(patched_phys_ebx)
            cmap_ph = dict(chunks_ph)
            phys_ebxd = bytearray(cmap_ph[b"EBXD"])
            phys_efix = bytearray(cmap_ph[b"EFIX"])
            phys_efix[0:16] = standalone_phys_fg
            phys_ebxd[12:28] = standalone_phys_fg
            struct.pack_into("<Q", phys_ebxd, 124, standalone_col_rid)
            bsp.patch_ebx_string_inplace(phys_ebxd, 204, 167, standalone_phys_ebx_name)
            struct.pack_into("<q", phys_ebxd, 52, 152)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_phys_ebx_name.lower(),
                "raw": remux_riff(form_ph, [(cid, phys_ebxd if cid == b"EBXD" else (phys_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_ph]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 32: Placement Proxy EcsPhysics (+ _pphys_1..7 when use_b94_8hull is active)
            B94_PPHYS_PREFIXES = (
                b"\x25\x7a",
                b"\x30\x85",
                b"\x51\x06",
                b"\x91\x8f",
                b"\x9b\xb1",
                b"\x9c\xdc",
                b"\xa2\xa4",
                b"\xb0\xb5",
            )
            pphys_0_vm = pphys_vms_list[0] if len(pphys_vms_list) >= 1 else prev["volume_moments_16f"]
            patched_pphys_ebx = patch_physics_ebx_volume_moments(
                tpl["ebx32"], pphys_0_vm, shape_cnt=1, is_skateable_col=False
            )
            standalone_pphys_fg = _guid("pphys_fg", prefix_2b=(B94_PPHYS_PREFIXES[0] if use_b94_8hull else b"\xa7\xe1"))
            form_pp, chunks_pp = demux_riff(patched_pphys_ebx)
            cmap_pp = dict(chunks_pp)
            pphys_ebxd = bytearray(cmap_pp[b"EBXD"])
            pphys_efix = bytearray(cmap_pp[b"EFIX"])
            pphys_efix[0:16] = standalone_pphys_fg
            pphys_ebxd[12:28] = standalone_pphys_fg
            struct.pack_into("<Q", pphys_ebxd, 124, standalone_pphys_rid)
            bsp.patch_ebx_string_inplace(pphys_ebxd, 188, 167, standalone_pphys_name)
            struct.pack_into("<q", pphys_ebxd, 52, 136)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_pphys_name.lower(),
                "raw": remux_riff(form_pp, [(cid, pphys_ebxd if cid == b"EBXD" else (pphys_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_pp]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            pphys_fgs_8 = [standalone_pphys_fg]
            if use_b94_8hull:
                for hull_i in range(1, 8):
                    sub_pphys_name = f"items/customprops/{prop_key}_pphys_{hull_i}"
                    sub_pphys_rid = _rid(f"pphys_rid_{hull_i}")
                    sub_pphys_fg = _guid(f"pphys_fg_{hull_i}", prefix_2b=B94_PPHYS_PREFIXES[hull_i])
                    pphys_fgs_8.append(sub_pphys_fg)
                    spec_entry["extra_bundle_ress"].append(sub_pphys_name.lower())
                    spec_entry["extra_bundle_ebxs"].append(sub_pphys_name.lower())
                    fbmod_records.append({
                        "rtype": 2,
                        "name": sub_pphys_name.lower(),
                        "raw": pphys_bytes_list[hull_i],
                        "is_added": True,
                        "tag": "reskate-cosmetic-addition:v1",
                        "bundles": [SHARED_BUNDLE_HASH],
                        "res_type": 0x41759364,
                        "res_rid": sub_pphys_rid,
                        "res_meta": tpl["ress"][16]["res_meta"],
                    })
                    sub_vm = pphys_vms_list[hull_i] if hull_i < len(pphys_vms_list) else pphys_0_vm
                    sub_patched_ebx = patch_physics_ebx_volume_moments(
                        tpl["ebx32"], sub_vm, shape_cnt=1, is_skateable_col=False
                    )
                    _, sub_chunks_pp = demux_riff(sub_patched_ebx)
                    sub_cmap_pp = dict(sub_chunks_pp)
                    sub_ebxd = bytearray(sub_cmap_pp[b"EBXD"])
                    sub_efix = bytearray(sub_cmap_pp[b"EFIX"])
                    sub_efix[0:16] = sub_pphys_fg
                    sub_ebxd[12:28] = sub_pphys_fg
                    struct.pack_into("<Q", sub_ebxd, 124, sub_pphys_rid)
                    bsp.patch_ebx_string_inplace(sub_ebxd, 188, 167, sub_pphys_name)
                    struct.pack_into("<q", sub_ebxd, 52, 136)
                    fbmod_records.append({
                        "rtype": 1,
                        "name": sub_pphys_name.lower(),
                        "raw": remux_riff(form_pp, [(cid, sub_ebxd if cid == b"EBXD" else (sub_efix if cid == b"EFIX" else pl)) for cid, pl in sub_chunks_pp]),
                        "is_added": True,
                        "tag": "reskate-cosmetic-addition:v1",
                        "bundles": [SHARED_BUNDLE_HASH],
                    })

            # 4. Standalone .nopolicy (EBX 35), EcsRuntimePrefabAsset (EBX 34), and NetworkRegistryAsset (EBX 37)
            standalone_nopol_name = f"items/customprops/{prop_key}_ecsprefab.nopolicy"
            standalone_nopol_internal = f"items/customprops/{prop_key}_ecsPrefab.nopolicy"
            nopol_fg = _guid("nopol_fg", prefix_2b=(b"\xf2\x77" if use_b94_8hull else b"\x73\x13"))
            nopol_ig = _guid("nopol_ig")

            if (prop_vfx_emitters or prop_music_emitter.get("enabled")) and not wip_vfx_enabled:
                print(
                    f"  [ReSkate Lab] Saved WIP VFX/Music metadata on '{prop_key}' without injecting Bundle[16] (public release safe mode).",
                    flush=True,
                )

            if has_prop_vfx_or_music and noble_nopol_raw is not None:
                # Synthesize per-prop VFX / Music EffectBlueprint EBX from Bundle[16] and inject Bundle[16] VFX/Audio assets!
                ebp_raw, ebp_fg, ebp_ig, ebp_name = build_custom_vfx_effect_ebx(
                    prop_key,
                    prop_vfx_emitters,
                    prop_music_emitter,
                    prop_lights=prop_lights,
                    seed_prefix=seed_prefix,
                    use_coexist_ids=use_coexist_ids,
                )
                spec_entry["has_vfx_bundle_assets"] = True
                spec_entry["extra_bundle_ebxs"].append(ebp_name.lower())
                fbmod_records.append({
                    "rtype": 1,
                    "name": ebp_name.lower(),
                    "raw": ebp_raw,
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                })
                patched_nopol = build_lit_nopolicy_ebx(
                    prop_key,
                    nopol_fg,
                    nopol_ig,
                    standalone_nopol_internal,
                    ebp_fg,
                    ebp_ig,
                    shape_packed_list,
                    noble_nopol_raw,
                    light_pos_blender=prop_lights[0].get("pos") if prop_lights else None,
                )
            elif prop_lights and noble_effect_ebx is not None and noble_nopol_raw is not None:
                # Synthesize per-prop custom EffectBlueprint EBX and wire it into the prop's .nopolicy!
                ebp_raw, ebp_fg, ebp_ig, ebp_name = build_custom_light_effect_ebx(
                    prop_key, prop_lights, noble_effect_ebx, seed_prefix=seed_prefix, use_coexist_ids=use_coexist_ids
                )
                spec_entry["extra_bundle_ebxs"].append(ebp_name.lower())
                fbmod_records.append({
                    "rtype": 1,
                    "name": ebp_name.lower(),
                    "raw": ebp_raw,
                    "is_added": True,
                    "tag": "reskate-cosmetic-addition:v1",
                    "bundles": [SHARED_BUNDLE_HASH],
                })
                patched_nopol = build_lit_nopolicy_ebx(
                    prop_key,
                    nopol_fg,
                    nopol_ig,
                    standalone_nopol_internal,
                    ebp_fg,
                    ebp_ig,
                    shape_packed_list,
                    noble_nopol_raw,
                    light_pos_blender=prop_lights[0].get("pos") if prop_lights else None,
                )
            elif use_b94_8hull:
                orig_b94_ig121 = bytes.fromhex("681543395c0ba3678fa96bc088ccac7f")
                orig_b94_child = bytes.fromhex("0640fff630e89ed0a8abf2f4953e96a5")
                child_guid_expected = bytes.fromhex("f9b22611e3a75f20dab74ce39852e261")
                form_np, chunks_np = demux_riff(tpl["b94_ebx121"])
                cmap_np = dict(chunks_np)
                np_ebxd = bytearray(
                    bytes(cmap_np[b"EBXD"])
                    .replace(orig_b94_ig121, nopol_ig)
                    .replace(orig_b94_child, child_guid_expected)
                )
                np_efix = bytearray(cmap_np[b"EFIX"])
                np_efix[0:16] = nopol_fg
                bsp.patch_ebx_string_inplace(np_ebxd, 2052, 83, standalone_nopol_internal)
                patched_nopol = patch_prefab_ebx_materials(
                    remux_riff(form_np, [(cid, np_ebxd if cid == b"EBXD" else (np_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_np]),
                    shape_packed_list,
                )
            else:
                form_np, chunks_np = demux_riff(tpl["ebx35"])
                cmap_np = dict(chunks_np)
                np_ebxd = bytearray(bytes(cmap_np[b"EBXD"]).replace(orig_ig35, nopol_ig))
                np_efix = bytearray(cmap_np[b"EFIX"])
                np_efix[0:16] = nopol_fg
                bsp.patch_ebx_string_inplace(np_ebxd, 920, 92, standalone_nopol_internal)
                patched_nopol = patch_prefab_ebx_materials(
                    remux_riff(form_np, [(cid, np_ebxd if cid == b"EBXD" else (np_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_np]),
                    shape_packed_list,
                )

            fbmod_records.append({
                "rtype": 1,
                "name": standalone_nopol_name.lower(),
                "raw": patched_nopol,
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 34: Standalone EcsRuntimePrefabAsset
            standalone_prefab_name = f"items/customprops/{prop_key}_ecsprefab"
            standalone_prefab_internal = f"items/customprops/{prop_key}_ecsPrefab"
            standalone_prefab_fg = _guid("prefab_fg")
            standalone_prefab_ig = _guid("prefab_ig")
            spec_entry["prefab_fg"] = standalone_prefab_fg
            spec_entry["prefab_ig"] = standalone_prefab_ig
            child_guid_expected = bytes.fromhex("f9b22611e3a75f20dab74ce39852e261")
            if use_b94_8hull:
                orig_b94_ig118 = bytes.fromhex("c7cecc1735f16856616dad630c3ff66b")
                orig_b94_ig121 = bytes.fromhex("681543395c0ba3678fa96bc088ccac7f")
                orig_b94_child = bytes.fromhex("0640fff630e89ed0a8abf2f4953e96a5")
                form_pf, chunks_pf = demux_riff(tpl["b94_ebx118"])
                cmap_pf = dict(chunks_pf)
                pref_ebxd = bytearray(
                    bytes(cmap_pf[b"EBXD"])
                    .replace(orig_b94_ig118, standalone_prefab_ig)
                    .replace(orig_b94_ig121, nopol_ig)
                    .replace(orig_b94_child, child_guid_expected)
                )
                pref_ebxd[0x214:0x224] = nopol_ig
                pref_ebxd[0x10C:0x11C] = child_guid_expected

                pref_efix = bytearray(cmap_pf[b"EFIX"])
                pref_efix[0:16] = standalone_prefab_fg
                # Patch 13 sorted imports in Bundle 94 EFIX (@1364..1780):
                # imp[0..3]: pphys_0..3, imp[4]: outl, imp[5..8]: pphys_4..7,
                # imp[9]: defaultdingooutlinecategory (untouched), imp[10]: mc, imp[11]: phys, imp[12]: nopol
                for imp_i in range(4):
                    off_i = 1364 + imp_i * 32
                    pref_efix[off_i : off_i + 32] = pphys_fgs_8[imp_i] + pphys_fgs_8[imp_i]
                pref_efix[1364 + 4 * 32 : 1364 + 5 * 32] = standalone_outl_fg + standalone_outl_fg
                for imp_i in range(4):
                    off_i = 1364 + (5 + imp_i) * 32
                    pref_efix[off_i : off_i + 32] = pphys_fgs_8[4 + imp_i] + pphys_fgs_8[4 + imp_i]
                pref_efix[1364 + 10 * 32 : 1364 + 11 * 32] = standalone_mc_fg + standalone_mc_fg
                pref_efix[1364 + 11 * 32 : 1364 + 12 * 32] = standalone_phys_fg + standalone_phys_fg
                pref_efix[1364 + 12 * 32 : 1364 + 13 * 32] = nopol_fg + nopol_ig
                bsp.patch_ebx_string_inplace(pref_ebxd, 11748, 85, standalone_prefab_internal)
                patched_pref = patch_prefab_ebx_materials(
                    remux_riff(form_pf, [(cid, pref_ebxd if cid == b"EBXD" else (pref_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_pf]),
                    shape_packed_list,
                )
            else:
                form_pf, chunks_pf = demux_riff(tpl["ebx34"])
                cmap_pf = dict(chunks_pf)
                # Patch root instance GUID with standalone_prefab_ig AND patch .nopolicy reference with nopol_ig
                pref_ebxd = bytearray(
                    bytes(cmap_pf[b"EBXD"])
                    .replace(orig_ig34, standalone_prefab_ig)
                    .replace(orig_ig35, nopol_ig)
                )
                # Explicitly patch EBXD[0x214:0x224] to guarantee nopol_ig parity
                pref_ebxd[0x214:0x224] = nopol_ig
                # Verify EBXD[0x10C:0x11C] matches child node GUID
                pref_ebxd[0x10C:0x11C] = child_guid_expected

                pref_efix = bytearray(cmap_pf[b"EFIX"])
                pref_efix[0:16] = standalone_prefab_fg
                pref_efix[1156:1188] = standalone_mc_fg + standalone_mc_fg
                pref_efix[1188:1220] = standalone_outl_fg + standalone_outl_fg
                pref_efix[1220:1252] = nopol_fg + nopol_ig
                pref_efix[1252:1284] = standalone_pphys_fg + standalone_pphys_fg
                pref_efix[1316:1348] = standalone_phys_fg + standalone_phys_fg
                bsp.patch_ebx_string_inplace(pref_ebxd, 14548, 94, standalone_prefab_internal)
                patched_pref = patch_prefab_ebx_materials(
                    remux_riff(form_pf, [(cid, pref_ebxd if cid == b"EBXD" else (pref_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_pf]),
                    shape_packed_list,
                )
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_prefab_name.lower(),
                "raw": patched_pref,
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # EBX 37: Standalone NetworkRegistryAsset
            standalone_netreg_name = f"items/customprops/{prop_key}_bundle_networkregistry_win32"
            standalone_netreg_internal = f"items/customprops/{prop_key}_bundle_networkregistry_Win32"
            netreg_fg = _guid("netreg_fg")
            form_nr, chunks_nr = demux_riff(tpl["ebx37"])
            cmap_nr = dict(chunks_nr)
            nr_ebxd = bytearray(cmap_nr[b"EBXD"])
            nr_efix = bytearray(cmap_nr[b"EFIX"])
            nr_efix[0:16] = netreg_fg
            nr_ebxd[12:28] = netreg_fg
            nr_efix[76:108] = standalone_prefab_fg + standalone_prefab_ig
            bsp.patch_ebx_string_inplace(nr_ebxd, 124, 113, standalone_netreg_internal)
            fbmod_records.append({
                "rtype": 1,
                "name": standalone_netreg_name.lower(),
                "raw": remux_riff(form_nr, [(cid, nr_ebxd if cid == b"EBXD" else (nr_efix if cid == b"EFIX" else pl)) for cid, pl in chunks_nr]),
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })

            # 5. DelMarBaseItemAsset pointing directly to this prop's standalone bundle
            # and standalone prefab (`items/customprops/<prop_key>_ecsPrefab`)
            bundle_stem = f"{bundle_stem_base}/{prop_key}"
            tsm_imp = cdn_sm_fg + cdn_sm_ig
            tlg_imp = cdn_lg_fg + cdn_lg_ig
            custom_item_raw, file_guid, inst0_guid = build_custom_item_ebx(
                item_tpl,
                item_path,
                prop_key,
                display_name,
                bundle_stem,
                None,
                None,
                tsm_imp,
                tlg_imp,
            )
            if use_coexist_ids:
                custom_item_raw, file_guid, inst0_guid = _patch_coexist_item_ebx(
                    custom_item_raw,
                    seed_prefix=seed_prefix,
                    item_path=item_path,
                    key_str=item_key_str,
                    display_name=display_name,
                    bundle_str=f"{bundle_stem}_bundle",
                    prefab_str=f"items/customprops/{prop_key}_ecsPrefab",
                )
            fbmod_records.append({
                "rtype": 1,
                "name": item_path.lower(),
                "raw": custom_item_raw,
                "is_added": True,
                "tag": "reskate-cosmetic-addition:v1",
                "bundles": [SHARED_BUNDLE_HASH],
            })
            custom_imc_refs.append((file_guid, inst0_guid))

    # 6. Global ItemMasterCollection
    for p_idx, cp in enumerate(cached_props, start=1):
        cached_tag = " (Cached)" if cp.get("_is_cached") else ""
        emit_prop_substep(p_idx, n_total_props, cp["spec"].get("display_name", "Prop"), 5, f"Packaging .fbmod & TOC{cached_tag}...")
    emit_step(4, 6, f"Registering {len(custom_imc_refs)} props in ItemMasterCollection & packaging .fbmod")
    emit_progress(60, f"Registering {len(custom_imc_refs)} custom prop entries in ItemMasterCollection...")
    patched_imc = build_patched_imc_ebx(imc_tpl, custom_imc_refs)
    fbmod_records.append({
        "rtype": 1,
        "name": "items/itemmastercollection",
        "raw": patched_imc,
        "is_added": False,
        "tag": "reskate-cosmetic-registration:v1",
        "bundles": [SHARED_BUNDLE_HASH],
    })

    # 7. Write Frosty v6 .fbmod
    emit_progress(70, f"Writing Frosty v6 archive {out_fbmod.name} ({len(fbmod_records)} records)...")
    write_fbmod_v6_full(
        out_fbmod,
        title=mod_name,
        author=author_name_str,
        version=pack_version,
        desc=pack_desc,
        records=fbmod_records,
    )
    print(f"  [+] Generated {out_fbmod.name} ({out_fbmod.stat().st_size / 1024.0:.1f} KB)", flush=True)

    # 8. Compile via reskate_cli compile-mod
    emit_step(5, 6, "Running reskate_cli compile-mod & building standalone TOC streams")
    emit_progress(80, f"Executing reskate_cli compile-mod for {mod_name}...")
    cli_staging_dir = staging_dir / f"CliStage_{mod_name}"
    if cli_staging_dir.exists():
        shutil.rmtree(cli_staging_dir, ignore_errors=True)
    cli_staging_dir.mkdir(parents=True, exist_ok=True)

    compile_cmd = [str(CLI_EXE), "compile-mod", str(GAME_DIR), str(cli_staging_dir), str(out_fbmod)]
    res_compile = subprocess.run(compile_cmd, capture_output=True, text=True)
    if res_compile.returncode != 0:
        emit_error(f"reskate_cli compile-mod failed: {res_compile.stderr.strip()}")
        raise RuntimeError(f"reskate_cli compile-mod failed: {res_compile.stderr}")
    print(f"  [+] compile-mod succeeded: {res_compile.stdout.strip()}", flush=True)

    # 9. Deploy to staging mod directory (and update live Mods folder if already installed)
    emit_step(6, 6, "Deploying standalone bundles & updating Quick Drop inventory")
    emit_progress(90, f"Synthesizing standalone Win32/buildkititems.toc and Win32/items.toc...")
    if not export_fbmod_only:
        auto_disable_old_versions = bool(meta.get("auto_disable_old_mod_versions", True))
        game_deployed = GAME_DIR / "Mods" / mod_name
        has_family_sibling_in_mods = _has_family_sibling_in_mods_dir(GAME_DIR / "Mods", mod_name)
        had_live_mod = game_deployed.exists() or deploy_to_mods or has_family_sibling_in_mods

        # Clear any stale backup dir so reskate_cli deploy-mod does not hit Windows error 5
        backup_mod_dir = GAME_DIR / ".ReSkateStudio-Mod-backup" / mod_name
        if backup_mod_dir.exists():
            shutil.rmtree(backup_mod_dir, ignore_errors=True)

        built_ts = datetime.now(timezone.utc).isoformat()
        raw_pack_part = mod_name
        if author_name_str and raw_pack_part.lower().startswith(f"{author_name_str.lower()}-"):
            raw_pack_part = raw_pack_part[len(author_name_str) + 1 :]
        elif author_name_str and raw_pack_part.lower().startswith(f"{author_name_str.lower()}_"):
            raw_pack_part = raw_pack_part[len(author_name_str) + 1 :]
        ts_safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_pack_part).strip("_")[:128] or "Custom_Props"
        ts_safe_author = re.sub(r"[^a-zA-Z0-9_]+", "_", author_name_str or "Author").strip("_") or "Author"
        manifest_data = {
            "name": ts_safe_name,
            "author": ts_safe_author,
            "version_number": pack_version,
            "website_url": "",
            "description": (pack_desc or "Custom skateable props built with ReSkate Prop Studio")[:250],
            "dependencies": [],
        }
        build_data = {
            "schema": 1,
            "tool": f"ReSkate Prop Studio Standalone CLI Compiler {compiler_version_stamp}",
            "version": compiler_version_stamp,
            "compiler_version": compiler_version_stamp,
            "addon_version": addon_version,
            "author": author_name_str,
            "author_name": author_name_str,
            "pack_version": pack_version,
            "option": mod_name,
            "category": category_name,
            "built": built_ts,
            "build_note": build_note,
            "items": [s.get("item_key_str", s["prop_key"]) for s in prop_specs_for_toc],
            "props": [
                {
                    "prop_key": s.get("raw_prop_key", s["prop_key"]),
                    "bundle_key": s["prop_key"],
                    "item_key": s.get("item_key_str", s["prop_key"]),
                    "display_name": s.get("display_name", s["prop_key"]),
                }
                for s in prop_specs_for_toc
            ],
        }
        compiled_prop_meta = {
            "schema": 1,
            "compiler_version": compiler_version_stamp,
            "addon_version": addon_version,
            "author": author_name_str,
            "author_name": author_name_str,
            "mod_name": mod_name,
            "pack_version": pack_version,
            "built": built_ts,
            "props": [
                {
                    "prop_key": s.get("raw_prop_key", s["prop_key"]),
                    "bundle_key": s["prop_key"],
                    "item_key": s.get("item_key_str", s["prop_key"]),
                    "display_name": s.get("display_name", s["prop_key"]),
                    "category": s.get("category", category_name),
                    "collision_mode": s.get("collision_mode", "SAME_AS_MESH"),
                    "surface_id": s.get("surface_id", "Concrete_Solid"),
                    "anti_lip_enabled": bool(s.get("anti_lip_enabled", False)),
                    "anti_lip_sink_m": float(s.get("anti_lip_sink_m", 0.008)),
                    "use_2x2m_detail_shader": bool(s.get("use_2x2m_detail_shader", False)),
                    "detail_2x2m_preset": str(s.get("detail_2x2m_preset", "CONCRETE_SIDEWALK")),
                    "detail_2x2m_intensity": float(s.get("detail_2x2m_intensity", 0.35)),
                    "atlas_resolution": int(s.get("atlas_resolution", 1024)),
                    "lights_count": len(s.get("lights") or []),
                    "experimental": s.get("experimental") or {},
                }
                for s in prop_specs_for_toc
            ],
        }
        compiled_prop_meta_json = json.dumps(compiled_prop_meta, indent=2)

        _backup_pack_build_if_exists(staging_dir, target_mod_dir, mod_name)

        def _write_mod_icon_if_present(dest_dir: Path) -> None:
            try:
                icon_src_str = str(meta.get("mod_icon_path") or "").strip()
                pack_icon_cache = _get_pack_storage_dir(staging_dir, mod_name) / "icon.png"
                src_cand = None
                if icon_src_str and Path(icon_src_str).is_file():
                    src_cand = Path(icon_src_str)
                elif pack_icon_cache.is_file():
                    src_cand = pack_icon_cache
                if src_cand is None and meta.get("source_props_list"):
                    first_p = meta["source_props_list"][0]
                    if isinstance(first_p, dict):
                        for k_tp in ("custom_thumbnail_path", "thumbnail_path", "thumb_256_path", "thumb_768_path"):
                            tp = first_p.get(k_tp)
                            if tp and Path(tp).is_file():
                                src_cand = Path(tp)
                                break
                if src_cand is None and prop_specs_for_toc:
                    first_s = prop_specs_for_toc[0]
                    for k_tp in ("thumb_256_path", "thumb_768_path"):
                        tp = first_s.get(k_tp)
                        if tp and Path(tp).is_file():
                            src_cand = Path(tp)
                            break
                if src_cand is not None and src_cand.is_file():
                    im_ic = Image.open(src_cand).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    im_ic.save(dest_dir / "icon.png", format="PNG")
                    pack_icon_cache.parent.mkdir(parents=True, exist_ok=True)
                    if src_cand.resolve() != pack_icon_cache.resolve():
                        im_ic.save(pack_icon_cache, format="PNG")
            except Exception as ic_err:
                print(f"  [!] Mod icon.png write skipped: {ic_err}", flush=True)

        _SURFACE_DISPLAY_LABELS = {
            "concrete_smooth": "Concrete (Smooth)",
            "concrete_solid": "Concrete (Smooth)",
            "concrete_rough": "Concrete (Rough)",
            "concrete_polished": "Concrete (Polished)",
            "concrete_aggregate": "Concrete (Exposed Aggregate)",
            "concrete_painted": "Painted Concrete",
            "asphalt_smooth": "Asphalt (Street)",
            "asphalt_rough": "Asphalt (Rough)",
            "brick_coarse": "Brick (Coarse)",
            "brick_paver": "Brick / Pavers",
            "marble_tile": "Marble / Tile",
            "marble": "Marble / Tile",
            "granite_ledge": "Granite / Stone",
            "metal_rail": "Metal (Round Rail)",
            "metal_solid": "Metal (Grindable)",
            "metal_solid_grindable": "Metal (Grindable)",
            "metal_grind_pipe_hollow": "Metal (Hollow Pipe)",
            "metal_hollow": "Metal (Hollow Pipe)",
            "metal_sheet": "Sheet Metal / Sign",
            "metal_corrugated": "Corrugated Metal",
            "metal_grate": "Metal Grate",
            "metal_chainlink": "Chainlink Fence",
            "metal_diamond_plate": "Metal Diamond Plate",
            "wood_plywood": "Wood (Plywood)",
            "wood_planks": "Wood (Planks / Deck)",
            "wood_board": "Wood (Planks / Deck)",
            "wood_log": "Wood (Log / Bark)",
            "cardboard": "Cardboard",
            "plastic_solid": "Plastic / Jersey Barrier",
            "plastic": "Plastic / Jersey Barrier",
            "glass_solid": "Glass",
            "rubber_mat": "Rubber",
            "grass": "Grass",
            "dirt_earth": "Dirt / Earth",
            "sand": "Sand",
            "gravel": "Gravel",
            "water_puddle": "Water / Puddle",
            "no_grind": "No-Grind Surface",
        }

        def _format_prop_surface_display(s: dict) -> str:
            explicit_lbl = str(s.get("surface_label") or "").strip()
            if explicit_lbl:
                return explicit_lbl
            mat_surfs = [
                ms for ms in (s.get("material_surfaces") or [])
                if isinstance(ms, dict)
                and not bool(ms.get("is_decal_overlay", False))
                and str(ms.get("surface") or "").strip().lower() not in ("", "default", "decal_overlay")
            ]
            if mat_surfs:
                slot_labels: list[str] = []
                for ms in mat_surfs:
                    raw_s = str(ms.get("surface") or "").strip()
                    mapped = _SURFACE_DISPLAY_LABELS.get(raw_s.lower()) or raw_s.replace("_", " ").strip().title()
                    if mapped and mapped not in slot_labels:
                        slot_labels.append(mapped)
                if slot_labels:
                    return " + ".join(slot_labels)
            raw_id = str(s.get("surface_id") or "Concrete_Solid").strip()
            return _SURFACE_DISPLAY_LABELS.get(raw_id.lower()) or raw_id.replace("_", " ").strip()

        def _write_mod_readme_if_present(dest_dir: Path) -> None:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                readme_src_str = str(meta.get("mod_readme_path") or "").strip()
                footer_md = "\n---\n*Created with [ReSkate Prop Studio](https://github.com/CameronTutt/ReSkate-Prop-Studio)*\n"
                if readme_src_str and Path(readme_src_str).is_file():
                    raw_rm = Path(readme_src_str).read_text(encoding="utf-8", errors="replace").rstrip()
                    if "github.com/camerontutt/reskate-prop-studio" not in raw_rm.lower():
                        raw_rm = f"{raw_rm}\n{footer_md}"
                    else:
                        raw_rm = raw_rm + "\n"
                    (dest_dir / "README.md").write_text(raw_rm, encoding="utf-8")
                    return
                lines = [
                    f"# {mod_name}",
                    "",
                    pack_desc,
                    "",
                    f"- **Author:** {author_name_str or 'ReSkate Creator'}",
                    f"- **Version:** {pack_version}",
                    f"- **Props Included:** {len(prop_specs_for_toc)}",
                    "",
                    "## Included Props",
                    "",
                    "| Prop Name | Variant Family | Quick Drop Tab | Surface | Collision |",
                    "| :--- | :--- | :--- | :--- | :--- |",
                ]
                for s in prop_specs_for_toc:
                    dname = str(s.get("display_name") or s.get("prop_key", "Prop")).strip()
                    fam_t = str(s.get("family_title") or "—").strip() or "—"
                    cat_t = str(s.get("custom_qd_tab") or s.get("category") or "Custom Props").strip()
                    surf_t = _format_prop_surface_display(s)
                    col_t = str(s.get("collision_mode") or "SAME_AS_MESH").strip()
                    lines.append(f"| **{dname}** | {fam_t} | `{cat_t}` | `{surf_t}` | `{col_t}` |")
                lines.extend([
                    "",
                    "## Installation",
                    "",
                    f"1. Copy the `{mod_name}` folder into `<Skate Game Folder>/Mods/` or install via ReSkate Mod Manager.",
                    "2. Launch Skate. and open the Quick Drop Object Browser.",
                    "",
                    "---",
                    "*Created with [ReSkate Prop Studio](https://github.com/CameronTutt/ReSkate-Prop-Studio)*",
                    "",
                ])
                (dest_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
            except Exception as rm_err:
                print(f"  [!] Mod README.md write skipped: {rm_err}", flush=True)

        def _write_reskate_objects_and_live_bridge(dest_dir: Path) -> None:
            try:
                categories_map: dict[str, dict] = {}
                families_map: dict[str, dict] = {}
                objects_list: list[dict] = []
                family_members_map: dict[str, list[str]] = {}
                variants_bridge: dict[str, dict] = {}

                # First collect clean display names and base stems so variant siblings
                # (e.g. "Kickout curb", "Kickout curb nodecals", "Kickout curb [2A 1:1 Mesh]")
                # automatically group onto a single X/Square family tile in the Object Browser!
                raw_titles = []
                for s in prop_specs_for_toc:
                    disp = str(s.get("display_name") or s.get("prop_key", "Custom Prop")).strip()
                    base_disp = re.sub(r"\s*\[.*?\]\s*$", "", disp).strip()
                    raw_titles.append((s, disp, base_disp))

                all_base_lowers = sorted(
                    {bt.lower() for _, _, bt in raw_titles if len(bt) >= 3},
                    key=len,
                )

                def _resolve_family_root_title(base_disp: str, spec_d: dict) -> str:
                    explicit_fam = str(spec_d.get("family_title") or spec_d.get("variant_group") or "").strip()
                    if explicit_fam:
                        return explicit_fam
                    bl = base_disp.lower()
                    for cand in all_base_lowers:
                        if cand != bl and bl.startswith(cand + " "):
                            # Match shortest parent title in the same pack (e.g. "Kickout curb" for "Kickout curb nodecals")
                            for _, _, orig_bt in raw_titles:
                                if orig_bt.lower() == cand:
                                    return orig_bt
                    return base_disp

                for s, disp, base_disp in raw_titles:
                    item_key = str(s.get("item_key_str") or s.get("prop_key", "")).strip()
                    if not item_key.startswith("own_"):
                        item_key = f"own_{item_key}"

                    prop_cat_raw = str(s.get("category") or category_name or "Custom Props").strip()
                    custom_tab_raw = str(s.get("custom_qd_tab") or "").strip()
                    qd_slug = _resolve_qd_tab_slug(prop_cat_raw, custom_tab_raw)
                    cat_suffix = qd_slug[2:] if qd_slug.startswith("bk") and len(qd_slug) > 2 else qd_slug
                    cat_id = f"category_{cat_suffix}"

                    if cat_id not in categories_map:
                        clean_tab = re.sub(r"^bk_?", "", custom_tab_raw or cat_suffix, flags=re.IGNORECASE).strip()
                        if clean_tab.lower() == "customprops":
                            cat_title = mod_name.replace("_", " ").strip()
                        elif clean_tab.lower() in mod_name.lower().replace("_", ""):
                            cat_title = mod_name.replace("_Assets", "").replace("_", " ").strip()
                        else:
                            cat_title = clean_tab.replace("_", " ").strip().title()
                        categories_map[cat_id] = {
                            "id": cat_id[:60],
                            "title": cat_title[:60] or "Custom Props",
                            "priority": 100 + len(categories_map),
                        }

                    fam_root_title = _resolve_family_root_title(base_disp, s)
                    fam_slug = re.sub(r"[^a-z0-9_]+", "_", fam_root_title.lower()).strip("_") or "prop"
                    fam_id = f"qdparent_{cat_suffix}_{fam_slug}"[:60]

                    if fam_id not in families_map:
                        families_map[fam_id] = {
                            "id": fam_id,
                            "title": fam_root_title[:60],
                            "category": cat_id[:60],
                        }
                    family_members_map.setdefault(fam_id, []).append(item_key)

                    surf_id = _format_prop_surface_display(s)
                    col_mode = str(s.get("collision_mode") or "SAME_AS_MESH")
                    desc_str = str(s.get("description") or f"{mod_name} | Surface: {surf_id} | Collision: {col_mode}")[:250]

                    objects_list.append({
                        "key": item_key[:250],
                        "title": disp[:120],
                        "description": desc_str,
                        "family": fam_id,
                    })

                    variants_bridge[item_key] = {
                        "item_key": item_key,
                        "display_name": disp,
                        "family_id": fam_id,
                        "family_title": fam_root_title,
                        "category_id": cat_id,
                        "surface_id": surf_id,
                        "collision_mode": col_mode,
                        "is_dynamic": bool(s.get("is_dynamic_physics", False)),
                        "physics_twin": str(s.get("physics_twin_key") or ""),
                    }

                for item_key, vinfo in variants_bridge.items():
                    fid = vinfo["family_id"]
                    vinfo["family_members"] = family_members_map.get(fid, [item_key])

                reskate_objects_doc = {
                    "schema": 1,
                    "author": author_name_str,
                    "mod_name": mod_name,
                    "categories": list(categories_map.values()),
                    "families": list(families_map.values()),
                    "objects": objects_list,
                }
                dest_dir.mkdir(parents=True, exist_ok=True)
                obj_json_text = json.dumps(reskate_objects_doc, indent=2)
                (dest_dir / "reskate-objects.json").write_text(
                    obj_json_text,
                    encoding="utf-8",
                )
                (dest_dir / "reskate-items.json").write_text(
                    obj_json_text,
                    encoding="utf-8",
                )

                # Write shared content bridge inside the mod folder for ReSkate_QuickDropPro
                bridge_dir = dest_dir / "quickdrop_bridge"
                bridge_dir.mkdir(parents=True, exist_ok=True)
                bridge_doc = {
                    "schema": 1,
                    "author": author_name_str,
                    "mod_name": mod_name,
                    "built": built_ts,
                    "variants": variants_bridge,
                }
                (bridge_dir / "variants.json").write_text(
                    json.dumps(bridge_doc, indent=2),
                    encoding="utf-8",
                )

                # Also update live_bridge.json in Mods/.reskate_plugin_data/ReSkate_QuickDropPro/
                plugin_data_dir = GAME_DIR / "Mods" / ".reskate_plugin_data" / "ReSkate_QuickDropPro"
                plugin_data_dir.mkdir(parents=True, exist_ok=True)
                live_bridge_path = plugin_data_dir / "live_bridge.json"
                existing_bridge = {}
                if live_bridge_path.is_file():
                    try:
                        existing_bridge = json.loads(live_bridge_path.read_text(encoding="utf-8"))
                    except Exception:
                        existing_bridge = {}
                merged_variants = dict(existing_bridge.get("variants") or {})
                merged_variants.update(variants_bridge)
                live_payload = {
                    "schema": 1,
                    "updated_at": built_ts,
                    "last_compiled_mod": mod_name,
                    "variants": merged_variants,
                    "wireframe_previews": existing_bridge.get("wireframe_previews") or [],
                    "placements": existing_bridge.get("placements") or [],
                    "apply_placements": False,
                }
                live_bridge_path.write_text(json.dumps(live_payload, indent=2), encoding="utf-8")
                print(
                    f"  [+] Generated reskate-objects.json ({len(categories_map)} categories, {len(families_map)} X/Square families, {len(objects_list)} objects) & synced Quick Drop Pro bridge",
                    flush=True,
                )
            except Exception as ro_err:
                print(f"  [!] reskate-objects.json / bridge write warning: {ro_err}", flush=True)

        if coexist_mode and (cli_staging_dir / "Patch").exists():
            log_input_read(GAME_DIR / "Data" / "Win32" / "buildkititems.toc", coexist_mode=coexist_mode)
            if target_mod_dir.exists():
                shutil.rmtree(target_mod_dir, ignore_errors=True)
            shutil.copytree(cli_staging_dir / "Patch", target_mod_dir, dirs_exist_ok=True)
            bsp.build_custom_buildkititems_toc(target_mod_dir, prop_specs_for_toc, tpl, bundle_stem_base=bundle_stem_base)
            clean_items_toc(target_mod_dir, allowed_custom_ebx, allowed_custom_res, forbidden_chunk_guids_raw)
            inject_global_shaders_and_globals_toc(target_mod_dir, prop_specs_for_toc, coexist_mode=coexist_mode)
            (target_mod_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
            (target_mod_dir / "reskate-build.json").write_text(json.dumps(build_data, indent=2), encoding="utf-8")
            (target_mod_dir / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
            _write_mod_icon_if_present(target_mod_dir)
            _write_mod_readme_if_present(target_mod_dir)
            _write_reskate_objects_and_live_bridge(target_mod_dir)
            world_music_nodes = []
            for s in prop_specs_for_toc:
                ex_m = (s.get("experimental") or {}).get("music_emitter") if isinstance(s.get("experimental"), dict) else None
                if isinstance(ex_m, dict) and ex_m.get("enabled"):
                    world_music_nodes.append({
                        "prop_key": s.get("raw_prop_key", s["prop_key"]),
                        "display_name": s.get("display_name", s["prop_key"]),
                        "world_level_toc": ex_m.get("world_level_toc", "Win32/levels/worlds/dingo/dingolevel_reskate_in/dingolevel_reskate_in.toc"),
                        "grabster_prefab_path": ex_m.get("grabster_prefab_path", "content/interactivemusic/grabster/grabster_prefab"),
                        "grabster_prefab_fg": ex_m.get("grabster_prefab_fg", "4fef15c13030f011a855b4c61a8d2328"),
                        "grabster_prefab_ig": ex_m.get("grabster_prefab_ig", "fd399f66b95eb348b108855471905521"),
                        "speaker_prefab_path": ex_m.get("speaker_prefab_path", "content/interactivemusic/speakers/speaker_prefab"),
                        "speaker_prefab_fg": ex_m.get("speaker_prefab_fg", "27ceacb5caf2ea119589cffbb351dba0"),
                        "speaker_prefab_ig": ex_m.get("speaker_prefab_ig", "91e3579597d415159c19fb4f271b13f2"),
                        "audio_tag_path": ex_m.get("audio_tag_path", "audio/_systems/audiotag/object/dgo_tag_obj_boombox"),
                        "audio_tag_fg": ex_m.get("audio_tag_fg", "861042442e9db448abbf58a80a93ca72"),
                        "audio_tag_ig": ex_m.get("audio_tag_ig", "2a6a6941f964e2b9fd3782c6e18083a8"),
                        "boombox_icon_vfx_path": ex_m.get("boombox_icon_vfx_path", "effects/gestures/effectblueprints/ebp_gesture_boombox_shapes"),
                        "boombox_icon_vfx_fg": ex_m.get("boombox_icon_vfx_fg", "10ffbf7fbbe50741957d231fb6270e90"),
                        "boombox_icon_vfx_ig": ex_m.get("boombox_icon_vfx_ig", "2dc0b2a3430e5d709d2c0576bf9e2e2f"),
                        "dsp_profile": ex_m.get("dsp_profile", "BOOMBOX"),
                        "subpatch_path": ex_m.get("subpatch_path", ""),
                        "radius": float(ex_m.get("radius", 15.0)),
                        "offset": ex_m.get("offset", [0.0, 0.0, 0.4]),
                        "world_location": ex_m.get("world_location", [0.0, 0.0, 0.0]),
                    })
            if world_music_nodes:
                (target_mod_dir / "san_van_interactive_music_map_nodes.json").write_text(
                    json.dumps({"schema": 1, "nodes": world_music_nodes}, indent=2),
                    encoding="utf-8",
                )
            try:
                (_get_pack_storage_dir(staging_dir, mod_name) / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
            except Exception:
                pass
            if meta.get("source_props_list"):
                _persist_mod_props_registry(target_mod_dir, meta["source_props_list"], mod_name, staging_dir=staging_dir, meta=meta)
            if had_live_mod:
                if game_deployed.exists():
                    shutil.rmtree(game_deployed, ignore_errors=True)
                shutil.copytree(target_mod_dir, game_deployed, dirs_exist_ok=True)
                shutil.copy2(out_fbmod, GAME_DIR / "Mods" / out_fbmod.name)
                _sync_mods_json_after_deploy(GAME_DIR, mod_name, auto_disable_old_versions=auto_disable_old_versions)
                print(f"  [+] Deployed coexist-mode mod folder at: {game_deployed} (and staged copy at {target_mod_dir})", flush=True)
            else:
                print(f"  [+] Staged coexist-mode mod folder safely isolated at: {target_mod_dir}", flush=True)
        else:
            deploy_cmd = [str(CLI_EXE), "deploy-mod", str(GAME_DIR), str(cli_staging_dir), mod_name]
            res_deploy = subprocess.run(deploy_cmd, capture_output=True, text=True)
            if res_deploy.returncode != 0:
                # Fallback: if game_deployed is locked by an explorer window or process, deploy under a temporary unique name and move into target_mod_dir
                temp_mod_name = f"{mod_name}_tmp_{os.getpid()}"
                temp_deployed = GAME_DIR / "Mods" / temp_mod_name
                res_deploy2 = subprocess.run([str(CLI_EXE), "deploy-mod", str(GAME_DIR), str(cli_staging_dir), temp_mod_name], capture_output=True, text=True)
                if res_deploy2.returncode != 0:
                    emit_error(f"reskate_cli deploy-mod failed: {res_deploy2.stderr.strip()}")
                    raise RuntimeError(f"reskate_cli deploy-mod failed: {res_deploy2.stderr}")
                bsp.build_custom_buildkititems_toc(temp_deployed, prop_specs_for_toc, tpl, bundle_stem_base=bundle_stem_base)
                clean_items_toc(temp_deployed, allowed_custom_ebx, allowed_custom_res, forbidden_chunk_guids_raw)
                inject_global_shaders_and_globals_toc(temp_deployed, prop_specs_for_toc, coexist_mode=coexist_mode)
                (temp_deployed / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
                (temp_deployed / "reskate-build.json").write_text(json.dumps(build_data, indent=2), encoding="utf-8")
                (temp_deployed / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
                _write_mod_icon_if_present(temp_deployed)
                _write_mod_readme_if_present(temp_deployed)
                _write_reskate_objects_and_live_bridge(temp_deployed)
                try:
                    (_get_pack_storage_dir(staging_dir, mod_name) / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
                except Exception:
                    pass
                if meta.get("source_props_list"):
                    _persist_mod_props_registry(temp_deployed, meta["source_props_list"], mod_name, staging_dir=staging_dir, meta=meta)
                if target_mod_dir.exists():
                    shutil.rmtree(target_mod_dir, ignore_errors=True)
                shutil.copytree(temp_deployed, target_mod_dir, dirs_exist_ok=True)
                if had_live_mod:
                    try:
                        shutil.copytree(temp_deployed, game_deployed, dirs_exist_ok=True)
                        shutil.copy2(out_fbmod, GAME_DIR / "Mods" / out_fbmod.name)
                        _sync_mods_json_after_deploy(GAME_DIR, mod_name, auto_disable_old_versions=auto_disable_old_versions)
                    except Exception as cp_err:
                        print(f"  [!] Live Mods folder locked ({cp_err}); staged cleanly at {target_mod_dir}", flush=True)
                shutil.rmtree(temp_deployed, ignore_errors=True)
            else:
                # 10. Synthesize standalone Win32/buildkititems.toc with per-prop custom bundles!
                bsp.build_custom_buildkititems_toc(game_deployed, prop_specs_for_toc, tpl, bundle_stem_base=bundle_stem_base)

                # 11. Clean Win32/items.toc and inject global shader & texture registration (leaving items.toc RES[3245] untouched + Win32/globals.toc)
                clean_items_toc(game_deployed, allowed_custom_ebx, allowed_custom_res, forbidden_chunk_guids_raw)
                inject_global_shaders_and_globals_toc(game_deployed, prop_specs_for_toc, coexist_mode=coexist_mode)

                (game_deployed / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
                (game_deployed / "reskate-build.json").write_text(json.dumps(build_data, indent=2), encoding="utf-8")
                (game_deployed / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
                _write_mod_icon_if_present(game_deployed)
                _write_mod_readme_if_present(game_deployed)
                _write_reskate_objects_and_live_bridge(game_deployed)
                try:
                    (_get_pack_storage_dir(staging_dir, mod_name) / "compiled_prop_meta.json").write_text(compiled_prop_meta_json, encoding="utf-8")
                except Exception:
                    pass
                if meta.get("source_props_list"):
                    _persist_mod_props_registry(game_deployed, meta["source_props_list"], mod_name, staging_dir=staging_dir, meta=meta)

                if target_mod_dir.exists():
                    shutil.rmtree(target_mod_dir, ignore_errors=True)
                shutil.copytree(game_deployed, target_mod_dir, dirs_exist_ok=True)

                if not had_live_mod:
                    shutil.rmtree(game_deployed, ignore_errors=True)
                    print(f"  [+] Staged mod folder safely isolated at: {target_mod_dir}", flush=True)
                else:
                    shutil.copy2(out_fbmod, GAME_DIR / "Mods" / out_fbmod.name)
                    _sync_mods_json_after_deploy(GAME_DIR, mod_name, auto_disable_old_versions=auto_disable_old_versions)
                    print(f"  [+] Deployed mod folder at: {game_deployed} (and staged copy at {target_mod_dir})", flush=True)

        # 12. Offline SQLite inventory & entitlements registration
        if DB_PATH.exists():
            try:
                conn = sqlite3.connect(str(DB_PATH))
                c = conn.cursor()
                for s in prop_specs_for_toc:
                    keys_to_unlock = {s["prop_key"]}
                    if s.get("item_key_str"):
                        keys_to_unlock.add(s["item_key_str"])
                    for k_u in keys_to_unlock:
                        c.execute("INSERT OR REPLACE INTO inventory (kind, item_id, owned) VALUES ('object', ?, 1)", (k_u,))
                        c.execute("INSERT OR REPLACE INTO entitlements (entitlement_id, owned) VALUES (?, 1)", (k_u,))
                conn.commit()
                conn.close()
                print(f"  [+] Registered {len(prop_specs_for_toc)} prop keys in offline SQLite inventory & entitlements.", flush=True)
            except Exception as ex:
                print(f"  [!] SQLite unlock note: {ex}", flush=True)

        # 13. Pre-Flight Fail-Loud Validator (Checks 1-5)
        emit_step(6, 6, "Running pre-flight fail-loud validation (Checks 1-5)")
        emit_progress(96, "Validating compiled mod integrity, cross-GUIDs, and conflict gates...")
        validate_compiled_mod_preflight(target_mod_dir, mod_name=mod_name, coexist_mode=coexist_mode)

    _PACK_TEXTURE_CACHE.clear()
    _SEEN_CHUNK_GUIDS.clear()
    emit_progress(100, f"Compilation complete: {out_fbmod.name}")
    emit_done(out_fbmod if export_fbmod_only else target_mod_dir)
    return out_fbmod


def _mod_family_stem(mod_name: str) -> str:
    """Computes the base family stem of a mod folder name by stripping trailing version suffixes."""
    s = str(mod_name or "").strip()
    if not s:
        return ""
    stripped = re.sub(
        r"(?:[-_.]?v\d+[a-z0-9._-]*|[-_.]\d+(?:\.\d+)+[a-z0-9._-]*)$",
        "",
        s,
        flags=re.IGNORECASE,
    ).rstrip("-_.").lower()
    return stripped or s.lower()


def _has_family_sibling_in_mods_dir(mods_dir: Path, mod_folder_name: str) -> bool:
    """Checks if any folder or mods.json entry in `mods_dir` shares the same base family stem."""
    try:
        mods_dir = Path(mods_dir)
        if not mods_dir.exists():
            return False
        target_stem = _mod_family_stem(mod_folder_name)
        if not target_stem:
            return False
        for d in mods_dir.iterdir():
            if d.is_dir() and _mod_family_stem(d.name) == target_stem:
                return True
        mods_json_p = mods_dir / "mods.json"
        if mods_json_p.exists():
            data = json.loads(mods_json_p.read_text(encoding="utf-8"))
            for entry in data.get("mods", []):
                if isinstance(entry, dict) and _mod_family_stem(entry.get("name", "")) == target_stem:
                    return True
    except Exception:
        pass
    return False


def _sync_mods_json_after_deploy(
    game_dir: Path,
    mod_folder_name: str,
    auto_disable_old_versions: bool = True,
) -> dict:
    """
    Ensures `mod_folder_name` is present and enabled in `<game_dir>/Mods/mods.json`,
    and when `auto_disable_old_versions` is True, sets `"enabled": false` on any older
    mod versions that share the same base family stem.
    Never calls `log_input_read` so Check 5 (coexist_mode zero Mods\\ reads) remains satisfied.
    """
    result = {"updated": False, "enabled_mod": mod_folder_name, "disabled_mods": []}
    try:
        mods_dir = Path(game_dir) / "Mods"
        if not mods_dir.exists():
            return result
        mods_json_p = mods_dir / "mods.json"
        if mods_json_p.exists():
            try:
                data = json.loads(mods_json_p.read_text(encoding="utf-8"))
            except Exception:
                data = {"mods": []}
        else:
            data = {"mods": []}

        if not isinstance(data, dict):
            data = {"mods": []}
        mods_list = data.get("mods")
        if not isinstance(mods_list, list):
            mods_list = []
            data["mods"] = mods_list

        target_name = str(mod_folder_name).strip()
        target_lower = target_name.lower()
        target_stem = _mod_family_stem(target_name)
        found_target = False
        changed = False

        for entry in mods_list:
            if not isinstance(entry, dict):
                continue
            e_name = str(entry.get("name") or "").strip()
            if not e_name:
                continue
            if e_name.lower() == target_lower:
                found_target = True
                if not bool(entry.get("enabled", False)):
                    entry["enabled"] = True
                    changed = True
            elif auto_disable_old_versions and target_stem and _mod_family_stem(e_name) == target_stem:
                if bool(entry.get("enabled", False)):
                    entry["enabled"] = False
                    changed = True
                    result["disabled_mods"].append(e_name)
                    print(
                        f"  [Mods.json] Auto-disabled older mod version: '{e_name}' -> enabled: false (replaced by '{target_name}')",
                        flush=True,
                    )

        if not found_target:
            mods_list.append({"enabled": True, "name": target_name})
            changed = True

        if changed or not mods_json_p.exists():
            tmp_p = mods_dir / f"mods.json.tmp_{os.getpid()}"
            tmp_p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(str(tmp_p), str(mods_json_p))
            result["updated"] = True
    except Exception as ex:
        print(f"  [!] Warning: Could not sync mods.json after deploy: {ex}", flush=True)
    return result


# ==============================================================================
# PRE-FLIGHT FAIL-LOUD VALIDATOR (CHECKS 1 - 5)
# ==============================================================================

COMBINABLE_RES_TYPES = {
    0xE2E6955B,  # fb::shader::programLookupType (RES[2002])
    0x2D254A89,  # fb::shader::textureLookupType (RES[1130])
    0x41759364,  # fb::material_grid::physicsResourceType
}


def _decompress_cas_payload(raw: bytes) -> bytes:
    """Decompresses Frostbite CAS chunk(s) using Oodle or raw copy."""
    oodle = rpf.get_oodle()
    out = bytearray()
    cur = 0
    while cur + 8 <= len(raw):
        u_sz, fl_b, c_sz = struct.unpack_from(">IHH", raw, cur)
        cur += 8
        comp_data = raw[cur : cur + c_sz]
        cur += c_sz
        if fl_b in (0x0070, 0x0000) or u_sz == c_sz:
            out.extend(comp_data)
        else:
            out_buf = ctypes.create_string_buffer(u_sz)
            ret = oodle.OodleLZ_Decompress(
                comp_data, c_sz, out_buf, u_sz, 1, 1, 0, None, 0, None, None, None, 0, 3
            )
            if ret <= 0:
                raise RuntimeError(f"Oodle decompression failed: ret={ret}")
            out.extend(out_buf.raw[:ret])
    return bytes(out)


def _get_sbdata_payload(sb_raw: bytes) -> bytes:
    """Returns raw sbdata bytes, decompressing from CAS block(s) if necessary."""
    if len(sb_raw) >= 8 and struct.unpack_from("<I", sb_raw, 4)[0] == 0x9D798ED6:
        return sb_raw
    if len(sb_raw) >= 8:
        try:
            decomp = _decompress_cas_payload(sb_raw)
            if len(decomp) >= 8 and struct.unpack_from("<I", decomp, 4)[0] == 0x9D798ED6:
                return decomp
        except Exception:
            pass
    return sb_raw


def _parse_efix_chunk(efix: bytes, payload_len: int, ebx_name: str = "") -> dict:
    """
    Parses canonical Frostbite EFIX chunk (matches ReSkate_SDK ebx_document.cpp)
    and verifies all chunk headers, section offsets, pointer/import fixups,
    and ensures n_boxed_value_refs == 0.
    """
    if len(efix) < 16:
        raise RuntimeError(f"Check 1 Failed: EFIX chunk truncated in '{ebx_name}' (len={len(efix)})")

    file_guid = bytes(efix[:16])
    if file_guid == b"\x00" * 16:
        raise RuntimeError(f"Check 1 Failed: All-zero FileGuid in EFIX of '{ebx_name}'")

    off = 16
    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before type count in '{ebx_name}'")
    n_types = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_types < 0 or off + n_types * 16 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid EFIX type count ({n_types}) in '{ebx_name}'")
    type_guids = [bytes(efix[off + i * 16 : off + (i + 1) * 16]) for i in range(n_types)]
    off += n_types * 16

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before signature count in '{ebx_name}'")
    n_sig = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_sig < 0 or off + n_sig * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid EFIX signature count ({n_sig}) in '{ebx_name}'")
    sigs = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_sig)]
    off += n_sig * 4

    if off + 8 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before instance counts in '{ebx_name}'")
    n_exp = struct.unpack_from("<i", efix, off)[0]; off += 4
    n_inst = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_exp < 0 or n_inst < 0 or n_exp > n_inst or off + n_inst * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid EFIX instance counts (exp={n_exp}, inst={n_inst}) in '{ebx_name}'")
    inst_offsets = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_inst)]
    off += n_inst * 4
    for io in inst_offsets:
        if not (0 <= io < payload_len):
            raise RuntimeError(f"Check 1 Failed: Instance offset 0x{io:X} out of bounds [0..{payload_len}) in '{ebx_name}'")

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before pointer count in '{ebx_name}'")
    n_ptrs = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_ptrs < 0 or off + n_ptrs * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid pointer fixup count ({n_ptrs}) in '{ebx_name}'")
    ptrs = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_ptrs)]
    off += n_ptrs * 4
    for po in ptrs:
        if not (0 <= po <= payload_len - 8):
            raise RuntimeError(f"Check 1 Failed: Pointer fixup offset 0x{po:X} exceeds EBXD payload length {payload_len} in '{ebx_name}'")

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before resource count in '{ebx_name}'")
    n_res = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_res < 0 or off + n_res * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid resource ref count ({n_res}) in '{ebx_name}'")
    res_refs = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_res)]
    off += n_res * 4
    for ro in res_refs:
        if not (0 <= ro <= payload_len - 8):
            raise RuntimeError(f"Check 1 Failed: Resource ref offset 0x{ro:X} exceeds EBXD payload length {payload_len} in '{ebx_name}'")

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before import count in '{ebx_name}'")
    n_imp = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_imp < 0 or off + n_imp * 32 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid import count ({n_imp}) in '{ebx_name}'")
    imports = [(bytes(efix[off + i * 32 : off + i * 32 + 16]), bytes(efix[off + i * 32 + 16 : off + (i + 1) * 32])) for i in range(n_imp)]
    off += n_imp * 32

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before import fixup count in '{ebx_name}'")
    n_imp_ptrs = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_imp_ptrs < 0 or off + n_imp_ptrs * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid import fixup count ({n_imp_ptrs}) in '{ebx_name}'")
    imp_ptrs = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_imp_ptrs)]
    off += n_imp_ptrs * 4
    for ipo in imp_ptrs:
        if not (0 <= ipo <= payload_len - 8):
            raise RuntimeError(f"Check 1 Failed: Import fixup offset 0x{ipo:X} exceeds EBXD payload length {payload_len} in '{ebx_name}'")

    if off + 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: EFIX truncated before typeinfo count in '{ebx_name}'")
    n_typeinfo = struct.unpack_from("<i", efix, off)[0]; off += 4
    if n_typeinfo < 0 or off + n_typeinfo * 4 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Invalid typeinfo count ({n_typeinfo}) in '{ebx_name}'")
    typeinfo = [struct.unpack_from("<I", efix, off + i * 4)[0] for i in range(n_typeinfo)]
    off += n_typeinfo * 4

    if off + 12 > len(efix):
        raise RuntimeError(f"Check 1 Failed: Truncated EFIX section offsets in '{ebx_name}'")
    arrays_off, boxed_off, strings_off = struct.unpack_from("<III", efix, off); off += 12
    if arrays_off > payload_len or boxed_off > payload_len or strings_off > payload_len:
        raise RuntimeError(
            f"Check 1 Failed: Section offset out of bounds in '{ebx_name}' "
            f"(arrays={arrays_off}, boxed={boxed_off}, strings={strings_off}, payload_len={payload_len})"
        )

    n_boxed = 0
    if off < len(efix):
        n_boxed = struct.unpack_from("<i", efix, off)[0]; off += 4
        if n_boxed > 0:
            raise RuntimeError(
                f"Check 1 Failed: n_boxed_value_refs={n_boxed} in '{ebx_name}' "
                f"(MUST be 0 to prevent 0x143def262 crash)"
            )
        off += n_boxed * 4

    if off != len(efix):
        raise RuntimeError(f"Check 1 Failed: {len(efix) - off} unexpected unparsed trailing bytes in EFIX of '{ebx_name}'")

    return {
        "file_guid": file_guid,
        "n_types": n_types,
        "type_guids": type_guids,
        "sigs": sigs,
        "n_exp": n_exp,
        "n_inst": n_inst,
        "inst_offsets": inst_offsets,
        "ptrs": ptrs,
        "res_refs": res_refs,
        "imports": imports,
        "imp_ptrs": imp_ptrs,
        "typeinfo": typeinfo,
        "arrays_off": arrays_off,
        "boxed_off": boxed_off,
        "strings_off": strings_off,
        "n_boxed": n_boxed,
    }


def _parse_ebxx_chunk(ebxx: bytes, payload_len: int, ebx_name: str = "") -> dict:
    """Parses and validates EBXX array and boxed value chunk."""
    if len(ebxx) < 8:
        raise RuntimeError(f"Check 1 Failed: EBXX chunk truncated in '{ebx_name}' (len={len(ebxx)})")
    n_arr = struct.unpack_from("<i", ebxx, 0)[0]
    n_box = struct.unpack_from("<i", ebxx, 4)[0]
    if n_arr < 0 or n_box < 0:
        raise RuntimeError(f"Check 1 Failed: Negative count in EBXX in '{ebx_name}'")
    expected_len = 8 + (n_arr + n_box) * 16
    if len(ebxx) != expected_len:
        raise RuntimeError(
            f"Check 1 Failed: EBXX chunk length mismatch in '{ebx_name}' "
            f"(expected {expected_len}, got {len(ebxx)})"
        )
    pos = 8
    arrays = []
    for _ in range(n_arr):
        off = struct.unpack_from("<I", ebxx, pos)[0]
        cnt = struct.unpack_from("<i", ebxx, pos + 4)[0]
        h = struct.unpack_from("<I", ebxx, pos + 8)[0]
        enc, cls = struct.unpack_from("<HH", ebxx, pos + 12)
        pos += 16
        if off > payload_len:
            raise RuntimeError(f"Check 1 Failed: EBXX array offset 0x{off:X} exceeds payload length {payload_len} in '{ebx_name}'")
        arrays.append({"offset": off, "count": cnt, "hash": h, "type": enc, "class": cls})
    boxed = []
    for _ in range(n_box):
        off = struct.unpack_from("<I", ebxx, pos)[0]
        cnt = struct.unpack_from("<i", ebxx, pos + 4)[0]
        h = struct.unpack_from("<I", ebxx, pos + 8)[0]
        enc, cls = struct.unpack_from("<HH", ebxx, pos + 12)
        pos += 16
        if off > payload_len:
            raise RuntimeError(f"Check 1 Failed: EBXX boxed value offset 0x{off:X} exceeds payload length {payload_len} in '{ebx_name}'")
        boxed.append({"offset": off, "count": cnt, "hash": h, "type": enc, "class": cls})
    return {"arrays": arrays, "boxed": boxed}


def _collect_mod_assets(mod_dir: Path) -> dict:
    """Extracts all TOC bundles, patched EBXs, RESs, and CHKs from mod_dir."""
    mod_dir = Path(mod_dir)
    cas_path = mod_dir / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas"
    if not cas_path.exists():
        cas_candidates = list(mod_dir.rglob("*.cas"))
        cas_path = cas_candidates[0] if cas_candidates else None
    cas_bytes = cas_path.read_bytes() if cas_path and cas_path.exists() else b""

    tocs = {}
    ebx_documents = {}
    res_resources = {}
    chk_chunks = {}
    standalone_bundles = {}
    toc_ord_chunks = {}

    for toc_file in mod_dir.rglob("*.toc"):
        if toc_file.name.lower() == "layout.toc":
            continue
        rel_toc = str(toc_file.relative_to(mod_dir)).replace("\\", "/").lower()
        toc_bytes = toc_file.read_bytes()
        if len(toc_bytes) < 568:
            continue
        pl = toc_bytes[556:] if toc_bytes[:4] == b"\x00\xd1\xce\x01" else toc_bytes
        iw = struct.unpack_from(">12I", pl, 0)
        w0, w1, n_b, w3, w4, n_c, c_loc_off, _, str_off = iw[:9]
        if w1 + n_b * 16 > len(pl):
            continue

        ord_guids = set()
        if n_c > 0 and w4 > 0:
            c_guid_base = w4
            for c_i in range(n_c):
                g_rev = pl[c_guid_base + c_i * 20 : c_guid_base + c_i * 20 + 16]
                ord_guids.add(bytes(g_rev[::-1]))
        toc_ord_chunks[rel_toc] = ord_guids

        bundles = []
        for b_idx in range(n_b):
            name_off, desc_sz, desc_off = struct.unpack_from(">IIQ", pl, w1 + b_idx * 16)
            s_end = pl.index(0, str_off + name_off)
            b_name = pl[str_off + name_off : s_end].decode("ascii", errors="replace")
            real_sz = desc_sz & 0x3FFFFFFF
            streams = bsp.parse_bundle_desc(pl[desc_off : desc_off + real_sz])
            bundles.append((b_name, streams))

            sb_s = streams[0]
            if sb_s[0] == 1 and cas_bytes:
                sb_raw = _get_sbdata_payload(cas_bytes[sb_s[3] : sb_s[3] + sb_s[4]])
                if len(sb_raw) < 8 or struct.unpack_from("<I", sb_raw, 4)[0] != 0x9D798ED6:
                    continue
                magic, b_ebxs, b_ress, b_chks, b_cmeta = bsp.parse_sbdata_full(sb_raw)
                standalone_bundles[b_name.lower()] = {
                    "name": b_name,
                    "ebxs": b_ebxs,
                    "ress": b_ress,
                    "chks": b_chks,
                    "cmeta": b_cmeta,
                    "streams": streams,
                    "toc": rel_toc,
                }
                for e_idx, e_info in enumerate(b_ebxs):
                    st = streams[1 + e_idx]
                    if st[0] == 1 and cas_bytes:
                        raw_ebx = _decompress_cas_payload(cas_bytes[st[3] : st[3] + st[4]])
                        ebx_documents[e_info["name"].lower()] = {
                            "name": e_info["name"],
                            "raw": raw_ebx,
                            "bundle": b_name,
                            "toc": rel_toc,
                        }
                for r_idx, r_info in enumerate(b_ress):
                    st = streams[1 + len(b_ebxs) + r_idx]
                    if st[0] == 1 and cas_bytes:
                        raw_res = _decompress_cas_payload(cas_bytes[st[3] : st[3] + st[4]])
                        res_resources[r_info["name"].lower()] = {
                            "name": r_info["name"],
                            "res_type": r_info["res_type"],
                            "res_rid": r_info["res_rid"],
                            "res_meta": r_info["res_meta"],
                            "raw": raw_res,
                            "bundle": b_name,
                            "toc": rel_toc,
                        }
                for c_idx, c_info in enumerate(b_chks):
                    st = streams[1 + len(b_ebxs) + len(b_ress) + c_idx]
                    if st[0] == 1:
                        chk_chunks[bytes(c_info["guid_raw"])] = {
                            "guid_raw": c_info["guid_raw"],
                            "log_sz": c_info["log_sz"],
                            "bundle": b_name,
                            "toc": rel_toc,
                            "stream": st,
                        }
        tocs[rel_toc] = bundles

    return {
        "tocs": tocs,
        "ebx_documents": ebx_documents,
        "res_resources": res_resources,
        "chk_chunks": chk_chunks,
        "standalone_bundles": standalone_bundles,
        "toc_ord_chunks": toc_ord_chunks,
    }


def _validate_check_1_riff_ebx_integrity(ebx_documents: dict) -> dict:
    """Check 1: RIFF / EFIX / EBXD / EBXX Integrity across all EBXs in the mod."""
    if not ebx_documents:
        raise RuntimeError("Check 1 Failed (RIFF/EFIX/EBXD/EBXX Integrity): No EBX documents found in mod!")

    parsed_ebxs = {}
    for e_name, e_doc in ebx_documents.items():
        raw = e_doc["raw"]
        if len(raw) < 12 or not raw.startswith(b"RIFF"):
            raise RuntimeError(f"Check 1 Failed (RIFF Integrity): EBX '{e_name}' is not a valid RIFF container (length={len(raw)})")
        form, chunks = demux_riff(raw)
        if form not in (b"EBX\x00", b"EBXD", b"EBXS"):
            raise RuntimeError(f"Check 1 Failed (RIFF Form): EBX '{e_name}' has unrecognized RIFF form {form}")
        cmap = dict(chunks)
        for req_chunk in (b"EBXD", b"EFIX", b"EBXX"):
            if req_chunk not in cmap:
                raise RuntimeError(f"Check 1 Failed (Missing Chunk): EBX '{e_name}' missing required chunk {req_chunk}")

        ebxd = cmap[b"EBXD"]
        efix = cmap[b"EFIX"]
        ebxx = cmap[b"EBXX"]

        if len(ebxd) < 12:
            raise RuntimeError(f"Check 1 Failed (EBXD Truncated): EBX '{e_name}' EBXD is smaller than 12-byte header")
        payload_len = len(ebxd) - 12

        efix_parsed = _parse_efix_chunk(efix, payload_len, ebx_name=e_name)
        ebxx_parsed = _parse_ebxx_chunk(ebxx, payload_len, ebx_name=e_name)

        # Validate pointer fixups relative offsets against ebxd payload
        for po in efix_parsed["ptrs"]:
            rel = struct.unpack_from("<i", ebxd, 12 + po)[0]
            if rel != 0:
                tgt = po + rel
                if tgt not in efix_parsed["inst_offsets"]:
                    if not (0 <= tgt < payload_len):
                        raise RuntimeError(
                            f"Check 1 Failed (Pointer Target OOB): EBX '{e_name}' pointer @ +0x{po:03X} "
                            f"target 0x{tgt:03X} exceeds EBXD payload length {payload_len}"
                        )

        parsed_ebxs[e_name] = {
            "name": e_name,
            "raw": raw,
            "ebxd": ebxd,
            "efix": efix,
            "ebxx": ebxx,
            "efix_parsed": efix_parsed,
            "ebxx_parsed": ebxx_parsed,
            "bundle": e_doc.get("bundle", ""),
            "toc": e_doc.get("toc", ""),
        }

    print(f"  [PASS] Check 1: RIFF/EFIX/EBXD/EBXX integrity verified across {len(parsed_ebxs)} EBXs (zero boxed value refs, valid section offsets).", flush=True)
    return parsed_ebxs


def _read_cstr(buf: bytes, pos: int) -> str:
    if pos < 0 or pos >= len(buf):
        return ""
    end = buf.find(b"\x00", pos)
    if end == -1:
        end = len(buf)
    return buf[pos:end].decode("ascii", errors="replace")


def _validate_check_2_guid_imports(parsed_ebxs: dict, mod_assets: dict, coexist_mode: bool = True) -> None:
    """Check 2: Every EBX Import, DelMarBaseItemAsset Bundle/Prefab/Key String, Internal Cross-GUID, & Forbidden ebp_chr_* Effects Must Resolve/Pass."""
    standalone_bundles = mod_assets["standalone_bundles"]
    res_resources = mod_assets["res_resources"]

    for e_name, doc in parsed_ebxs.items():
        if "ebp_chr_" in e_name.lower() or b"ebp_chr_" in doc.get("ebxd", b"").lower():
            raise RuntimeError(
                f"Check 2 Failed (Forbidden ebp_chr_* Effect): EBX '{e_name}' is or references a character/costume/impact effect "
                f"('ebp_chr_*'), which crashes the game as soon as the prop is rotated!"
            )
        for imp_idx, (fg, ig) in enumerate(doc["efix_parsed"]["imports"]):
            if fg == b"\x00" * 16 or ig == b"\x00" * 16:
                raise RuntimeError(f"Check 2 Failed: EBX '{e_name}' Import {imp_idx} contains null GUID ({fg.hex()} / {ig.hex()})")

        # Validate DelMarBaseItemAsset bundle, prefab, and item key pointers
        if e_name.startswith("items/") and e_name != "items/itemmastercollection" and not e_name.startswith("items/customprops/"):
            ebxd = doc["ebxd"]
            if len(ebxd) >= 348:
                rel_k = struct.unpack_from("<q", ebxd, 84)[0]
                rel_b = struct.unpack_from("<q", ebxd, 324)[0]
                rel_p = struct.unpack_from("<q", ebxd, 340)[0]
                k_str = _read_cstr(ebxd, 84 + rel_k)
                b_str = _read_cstr(ebxd, 324 + rel_b)
                p_str = _read_cstr(ebxd, 340 + rel_p)
                asset_id_u32 = struct.unpack_from("<I", ebxd, 100)[0]
                expected_hash = fnv1_32_lower(k_str)
                if asset_id_u32 != expected_hash:
                    raise RuntimeError(
                        f"Check 2 Failed (DelMarBaseItemAsset AssetId Hash Mismatch): '{e_name}' ebxd+100 is "
                        f"0x{asset_id_u32:08X}, expected fnv1_32_lower('{k_str}') == 0x{expected_hash:08X}!"
                    )
                if coexist_mode:
                    if not re.match(r"^own_bk[a-z0-9_]+$", k_str):
                        raise RuntimeError(
                            f"Check 2 Failed (Invalid Item Key Format): '{e_name}' s2 key '{k_str}' does not match "
                            f"^own_bk[a-z0-9_]+$ (required by valid_network_object() and build_kit_item())!"
                        )
                    if k_str.endswith("_lrg"):
                        raise RuntimeError(
                            f"Check 2 Failed (Forbidden _lrg Suffix): '{e_name}' s2 key '{k_str}' ends with '_lrg'!"
                        )
                    if len(k_str) > 256:
                        raise RuntimeError(
                            f"Check 2 Failed (Item Key Length > 256): '{e_name}' s2 key '{k_str}' is {len(k_str)} chars (> 256)!"
                        )
                    expected_thumb_res = f"thumbnail/tool/{k_str}"
                    if expected_thumb_res not in res_resources or expected_thumb_res not in parsed_ebxs:
                        raise RuntimeError(
                            f"Check 2 Failed (Missing Park Editor Thumbnail): '{e_name}' key '{k_str}' has no matching "
                            f"'{expected_thumb_res}' RES/EBX in Win32/items.toc (required by read_build_kit_thumbnails())!"
                        )
                if "bkprops" in b_str.lower() or not b_str.lower().startswith("items/customprops/"):
                    raise RuntimeError(
                        f"Check 2 Failed (DelMarBaseItemAsset Bundle Target): '{e_name}' ptr@324 points to "
                        f"'{b_str}' instead of standalone 'items/customprops/<prop_key>_bundle'!"
                    )
                full_b_key = f"win32/{b_str.lower()}"
                if full_b_key not in standalone_bundles:
                    raise RuntimeError(
                        f"Check 2 Failed (Unmounted Bundle): '{e_name}' ptr@324 references bundle '{full_b_key}', "
                        f"which does not exist in buildkititems.toc!"
                    )
                if p_str.lower() not in parsed_ebxs:
                    raise RuntimeError(
                        f"Check 2 Failed (Missing Prefab): '{e_name}' ptr@340 references prefab '{p_str}', "
                        f"which does not exist in compiled EBXs!"
                    )

    ecsprefabs = {k: v for k, v in parsed_ebxs.items() if k.endswith("_ecsprefab")}
    nopolicy_ebxs = {k: v for k, v in parsed_ebxs.items() if k.endswith(".nopolicy")}

    if any("customprops" in k for k in parsed_ebxs) and not ecsprefabs:
        raise RuntimeError("Check 2 Failed: Custom prop package contains no _ecsprefab EBX")

    EXPECTED_CHILD_ENTITY = bytes.fromhex("f9b22611e3a75f20dab74ce39852e261")
    EXPECTED_EBX33 = bytes.fromhex("7f76c7f1233e624817f906770e8b7535")
    EXPECTED_EBX33_B94 = bytes.fromhex("0045e0f3292e417b7d7908ea61ceaf24")
    EXPECTED_EBX33_SET = {EXPECTED_EBX33, EXPECTED_EBX33_B94}

    for pref_key, pref_doc in ecsprefabs.items():
        base_stem = pref_key[:-len("_ecsprefab")]
        nopol_key = f"{base_stem}_ecsprefab.nopolicy"
        if nopol_key not in nopolicy_ebxs:
            matching = [v for k, v in nopolicy_ebxs.items() if base_stem in k]
            if matching:
                nopol_doc = matching[0]
            else:
                raise RuntimeError(f"Check 2 Failed: Standalone prefab '{pref_key}' has no corresponding .nopolicy EBX")
        else:
            nopol_doc = nopolicy_ebxs[nopol_key]

        pref_ebxd = pref_doc["ebxd"]
        nopol_ebxd = nopol_doc["ebxd"]

        if len(pref_ebxd) < 12 + 0x224:
            raise RuntimeError(f"Check 2 Failed: _ecsprefab EBXD payload too small ({len(pref_ebxd)} bytes)")

        pref_nopol_ref = bytes(pref_ebxd[0x214:0x224])
        nopol_root_guid = bytes(nopol_ebxd[12:28])
        if pref_nopol_ref != nopol_root_guid:
            raise RuntimeError(
                f"Check 2 Failed: _ecsprefab EBXD[0x214:0x224] ({pref_nopol_ref.hex()}) "
                f"does not resolve to .nopolicy root instance GUID ({nopol_root_guid.hex()})"
            )

        pref_child_ref = bytes(pref_ebxd[0x10C:0x11C])
        if pref_child_ref != EXPECTED_CHILD_ENTITY:
            raise RuntimeError(
                f"Check 2 Failed: _ecsprefab EBXD[0x10C:0x11C] ({pref_child_ref.hex()}) "
                f"does not match expected .nopolicy child entity node ({EXPECTED_CHILD_ENTITY.hex()})"
            )

        # Verify child entity GUID actually exists 4 times in .nopolicy EBXD (including exported child instance)
        child_cnt_in_nopol = nopol_ebxd.count(EXPECTED_CHILD_ENTITY)
        if child_cnt_in_nopol < 4:
            raise RuntimeError(
                f"Check 2 Failed: .nopolicy '{nopol_doc['name']}' contains child entity GUID "
                f"{EXPECTED_CHILD_ENTITY.hex()} only {child_cnt_in_nopol} time(s) (expected 4 occurrences)!"
            )

        nopol_imports = nopol_doc["efix_parsed"]["imports"]
        if not nopol_imports:
            raise RuntimeError(f"Check 2 Failed: .nopolicy EBX '{nopol_doc['name']}' has no imports")
        imp0_fg = nopol_imports[0][0]
        if imp0_fg not in EXPECTED_EBX33_SET:
            raise RuntimeError(
                f"Check 2 Failed: .nopolicy Import 0 FileGuid ({imp0_fg.hex()}) "
                f"does not match EBX 33 ({EXPECTED_EBX33.hex()} or {EXPECTED_EBX33_B94.hex()})"
            )

        nopol_bundle = nopol_doc.get("bundle", "").lower()
        ebp_ebxs = [
            v for k, v in parsed_ebxs.items()
            if ("ebp_" in k or "effectblueprint" in k) and v.get("bundle", "").lower() == nopol_bundle
        ]
        if ebp_ebxs or len(nopol_imports) > 1:
            if nopol_doc["efix_parsed"]["n_inst"] < 6:
                raise RuntimeError(
                    f"Check 2 Failed: Lit prop .nopolicy has only {nopol_doc['efix_parsed']['n_inst']} instances "
                    f"(expected 6 instances from lit .nopolicy struct including FuseVfxEntityData & LightEffectEntityData)!"
                )
            if len(nopol_imports) < 2:
                raise RuntimeError(
                    f"Check 2 Failed: Lit prop .nopolicy has {len(nopol_imports)} import(s), expected at least 2 (EBX 33 + EffectBlueprint)"
                )
            imp1_fg, imp1_ig = nopol_imports[1]
            if ebp_ebxs:
                ebp_doc = ebp_ebxs[0]
                if "ebp_chr_" in ebp_doc["name"].lower() or b"ebp_chr_" in ebp_doc.get("ebxd", b"").lower():
                    raise RuntimeError(
                        f"Check 2 Failed (Forbidden ebp_chr_* Effect): Prop '{pref_key}' .nopolicy references character effect "
                        f"'{ebp_doc['name']}' ('ebp_chr_*'), which crashes the game on rotate!"
                    )
                expected_ebp_fg = ebp_doc["efix_parsed"]["file_guid"]
                expected_ebp_ig = bytes(ebp_doc["ebxd"][12:28])
                if imp1_fg != expected_ebp_fg or imp1_ig != expected_ebp_ig:
                    raise RuntimeError(
                        f"Check 2 Failed: Lit prop .nopolicy Import 1 ({imp1_fg.hex()}/{imp1_ig.hex()}) "
                        f"does not match standalone EffectBlueprint ({expected_ebp_fg.hex()}/{expected_ebp_ig.hex()})"
                    )

            found_autostart = False
            for test_pos in range(12, len(nopol_ebxd) - 16, 4):
                ptr_val = struct.unpack_from("<Q", nopol_ebxd, test_pos)[0]
                if ptr_val == 3:
                    auto_start = struct.unpack_from("<I", nopol_ebxd, test_pos + 8)[0]
                    if auto_start == 1:
                        found_autostart = True
                        break
            if not found_autostart:
                raise RuntimeError("Check 2 Failed: Lit prop .nopolicy missing FuseVfxEntity pointing to Import 1 with AutoStart=True")

    print(f"  [PASS] Check 2: All EBX imports, DelMarBaseItemAsset bundle/prefab pointers, _ecsprefab <-> .nopolicy (4x child node), and lit FuseVfxEntity resolved.", flush=True)


def _validate_check_3_rids_and_chunks(mod_assets: dict, parsed_ebxs: dict, coexist_mode: bool = True) -> None:
    """Check 3: Win32/items.toc World-Asset Isolation, Resource RIDs, Havok Shape Parity, and Chunk Ranks."""
    standalone_bundles = mod_assets["standalone_bundles"]
    toc_ord_chunks = mod_assets["toc_ord_chunks"]

    # 3A. Verify Win32/items.toc (cas_main_sharedbundle) contains ZERO 3D world mesh/physics/prefab assets!
    # (Custom _c TextureAssets and their 0x6BDE20BA RES are permitted in items.toc matching NOBLE1337-Noble_Lights!)
    for b_name, b_data in standalone_bundles.items():
        if "items.toc" in b_data.get("toc", "") and "buildkititems.toc" not in b_data.get("toc", ""):
            streams = b_data["streams"]
            ebxs = b_data["ebxs"]
            ress = b_data["ress"]
            for i, e in enumerate(ebxs):
                if streams[1 + i][0] == 1:
                    en = e["name"].lower()
                    if en.startswith("thumbnail/tool/") or (en.endswith(("_c", "_n", "_msk")) and not en.endswith("_mc")):
                        continue
                    if (
                        en.startswith(("items/customprops/", "effects/customprops/"))
                        or "/ebp_" in en
                        or en.endswith(("_mesh", "_mc", "_col", "_pphys", "_physics", "_ecsprefab", ".nopolicy"))
                    ):
                        raise RuntimeError(
                            f"Check 3 Failed (items.toc World-Asset Leak): 3D world EBX '{en}' leaked into Win32/items.toc "
                            f"(cas_main_sharedbundle)! Causes DingoLevel_Splash boot crash."
                        )
            for i, r in enumerate(ress):
                if streams[1 + len(ebxs) + i][0] == 1:
                    rn = r["name"].lower()
                    if coexist_mode and r["res_type"] == 0x73312045:
                        raise RuntimeError(
                            f"Check 3 Failed (Forbidden items.toc RES[3245] Modification): Win32/items.toc ShaderBlockDepot "
                            f"'{rn}' (RES[{i}], type 0x73312045) is modified (is_patch=1)! Never write items.toc RES[3245]."
                        )
                    if not (
                        rn.startswith("thumbnail/tool/")
                        or (rn.endswith(("_c", "_n", "_msk")) and r["res_type"] == 0x6BDE20BA)
                        or r["res_type"] == 0x73312045
                    ):
                        raise RuntimeError(
                            f"Check 3 Failed (items.toc World-Resource Leak): Non-texture RES '{rn}' leaked into Win32/items.toc "
                            f"(cas_main_sharedbundle)! Causes DingoLevel_Splash boot crash."
                        )
                    if r["res_type"] == 0x6BDE20BA:
                        r_doc = mod_assets["res_resources"].get(rn)
                        r_raw = r_doc.get("raw", b"") if r_doc else b""
                        if len(r_raw) >= 0x78:
                            r_start, r_end = struct.unpack_from("<II", r_raw, 0x00)
                            tw, th = struct.unpack_from("<HH", r_raw, 0x16)
                            m_tuple = tuple(r_raw[0x1E:0x22])
                            tot_sz = struct.unpack_from("<I", r_raw, 0x74)[0]
                            chk_g = bytes(r_raw[0x28:0x38])
                            b_chks_map = {bytes(c["guid_raw"]): c for c in b_data.get("chks", [])}
                            c_ent = b_chks_map.get(chk_g)
                            if rn.startswith("thumbnail/tool/"):
                                if m_tuple != (1, 0, 0, 1) or (r_start, r_end) != (0, 0):
                                    raise RuntimeError(
                                        f"Check 3 Failed (Corrupted Thumbnail Header in items.toc): '{rn}' ({tw}x{th}) has "
                                        f"mips={m_tuple}, range=({r_start},{r_end}) (expected mips=(1,0,0,1), range=(0,0))!"
                                    )
                            if c_ent is not None and int(c_ent.get("log_sz", 0)) != int(tot_sz):
                                raise RuntimeError(
                                    f"Check 3 Failed (Texture Chunk Size Mismatch in items.toc): '{rn}' declares tot_sz={tot_sz} "
                                    f"but chunk {chk_g.hex()} has log_sz={c_ent.get('log_sz')}!"
                                )

    all_ord_chunks = set()
    for ord_set in toc_ord_chunks.values():
        all_ord_chunks.update(ord_set)

    found_custom_bundle = False
    for b_name, b_data in standalone_bundles.items():
        if "customprops" not in b_name:
            continue
        found_custom_bundle = True
        ress_by_rid = {r["res_rid"]: r for r in b_data["ress"]}
        chks = b_data["chks"]
        cmeta = b_data["cmeta"]
        cmeta_entries = bsp.parse_cmeta_h64s(cmeta)

        for e_name, doc in parsed_ebxs.items():
            if doc.get("bundle", "").lower() != b_name:
                continue
            ebxd = doc["ebxd"]
            r_refs = doc["efix_parsed"]["res_refs"]

            if e_name.endswith("_mesh"):
                if not r_refs:
                    raise RuntimeError(f"Check 3 Failed: MeshAsset EBX '{e_name}' has no resource references in EFIX")
                ms_rid = struct.unpack_from("<Q", ebxd, 12 + r_refs[0])[0]
                if ms_rid not in ress_by_rid:
                    raise RuntimeError(f"Check 3 Failed: MeshAsset EBX '{e_name}' RID 0x{ms_rid:016X} not found in bundle RES table")
                if ress_by_rid[ms_rid]["res_type"] != 0x49B156D4:
                    raise RuntimeError(f"Check 3 Failed: MeshAsset EBX '{e_name}' RID 0x{ms_rid:016X} points to type 0x{ress_by_rid[ms_rid]['res_type']:08X}, expected 0x49B156D4 (MeshSet)")
                ms_name = ress_by_rid[ms_rid]["name"].lower()
                ms_res = mod_assets["res_resources"].get(ms_name)
                ms_raw = ms_res.get("raw", b"") if ms_res else b""
                if ms_raw and len(ms_raw) >= 0x60:
                    lod_ptrs = [p for p in struct.unpack_from("<6Q", ms_raw, 0x30) if p != 0]
                    for lp in lod_ptrs:
                        if lp + 0x94 <= len(ms_raw):
                            m_chk = bytes(ms_raw[lp + 0x84 : lp + 0x94])
                            if all_ord_chunks and m_chk not in all_ord_chunks:
                                raise RuntimeError(f"Check 3 Failed: MeshSet chunk {m_chk.hex()} missing from buildkititems.toc ord_chunks")
                            sc_i = struct.unpack_from("<I", ms_raw, lp + 0x18)[0]
                            sptr_i = struct.unpack_from("<Q", ms_raw, lp + 0x1C)[0]
                            ib_sz_i, vb_sz_i = struct.unpack_from("<II", ms_raw, lp + 0x68)
                            for si in range(sc_i):
                                sp_i = sptr_i + si * 0x180
                                if sp_i + 0x180 <= len(ms_raw):
                                    prim_i, start_i, v_off_i, v_cnt_i = struct.unpack_from("<4I", ms_raw, sp_i + 0x30)
                                    v_off_64 = struct.unpack_from("<I", ms_raw, sp_i + 0x64)[0]
                                    start_i_150 = struct.unpack_from("<I", ms_raw, sp_i + 0x150)[0]
                                    if v_off_64 != v_off_i or start_i_150 != start_i:
                                        raise RuntimeError(
                                            f"Check 3 Failed (MeshSubset GPU Binding Offset Mismatch): '{ms_name}' Sec[{si}] has "
                                            f"(+0x34 start_i={start_i}, +0x38 v_off={v_off_i}) vs (+0x150={start_i_150}, +0x64={v_off_64})!"
                                        )
                                    if v_cnt_i > 0 and v_off_i >= vb_sz_i:
                                        raise RuntimeError(
                                            f"Check 3 Failed (MeshSubset VB Out of Bounds): '{ms_name}' Sec[{si}] v_off={v_off_i} >= vb_sz={vb_sz_i}!"
                                        )
                                    if prim_i > 0 and (start_i + prim_i * 3) * 2 > ib_sz_i:
                                        raise RuntimeError(
                                            f"Check 3 Failed (MeshSubset IB Out of Bounds): '{ms_name}' Sec[{si}] end_byte={(start_i + prim_i * 3) * 2} > ib_sz={ib_sz_i}!"
                                        )

            elif e_name.endswith("_physics") or e_name.endswith("_pphys") or "_pphys_" in e_name:
                if not r_refs:
                    raise RuntimeError(f"Check 3 Failed: Physics EBX '{e_name}' has no resource references in EFIX")
                p_rid = struct.unpack_from("<Q", ebxd, 12 + r_refs[0])[0]
                if p_rid not in ress_by_rid:
                    raise RuntimeError(f"Check 3 Failed: Physics EBX '{e_name}' RID 0x{p_rid:016X} not found in bundle RES table")
                if ress_by_rid[p_rid]["res_type"] != 0x41759364:
                    raise RuntimeError(f"Check 3 Failed: Physics EBX '{e_name}' RID 0x{p_rid:016X} points to type 0x{ress_by_rid[p_rid]['res_type']:08X}, expected 0x41759364 (Havok Physics)")
                p_res_name = ress_by_rid[p_rid]["name"].lower()
                p_res_obj = mod_assets["res_resources"].get(p_res_name)
                if p_res_obj and len(p_res_obj.get("raw", b"")) >= 16:
                    p_raw = p_res_obj["raw"]
                    havok_shapes = struct.unpack_from("<I", p_raw, 12)[0]
                    if (e_name.endswith("_pphys") or "_pphys_" in e_name):
                        if havok_shapes != 1:
                            raise RuntimeError(
                                f"Check 3 Failed (Placement Proxy Shape Mismatch): '{e_name}' references Havok RES '{p_res_name}' "
                                f"with {havok_shapes} shapes (expected 1-hull placement proxy)!"
                            )
                        if len(p_raw) == 540:
                            # Validate orthogonal 8-vertex Havok polytope at 0xD4..0x134 so inverted vertices never ship
                            verts_8 = [struct.unpack_from("<3f", p_raw, 0xD4 + vi * 12) for vi in range(8)]
                            for (b_idx, t_idx) in ((0, 2), (1, 4), (3, 6), (5, 7)):
                                if not (verts_8[b_idx][1] < verts_8[t_idx][1]):
                                    raise RuntimeError(
                                        f"Check 3 Failed (Inverted Havok Polytope): '{p_res_name}' has bottom vertex v{b_idx}.y="
                                        f"{verts_8[b_idx][1]:.4f} >= top vertex v{t_idx}.y={verts_8[t_idx][1]:.4f}!"
                                    )
                            for (l_idx, r_idx) in ((0, 1), (2, 4), (3, 5), (6, 7)):
                                if not (verts_8[l_idx][0] < verts_8[r_idx][0]):
                                    raise RuntimeError(
                                        f"Check 3 Failed (Inverted Havok Polytope): '{p_res_name}' has left vertex v{l_idx}.x="
                                        f"{verts_8[l_idx][0]:.4f} >= right vertex v{r_idx}.x={verts_8[r_idx][0]:.4f}!"
                                    )

            elif e_name.endswith(("_c", "_n", "_msk")) and not e_name.endswith("_mc"):
                if r_refs:
                    t_rid = struct.unpack_from("<Q", ebxd, 12 + r_refs[0])[0]
                    if t_rid not in ress_by_rid and t_rid not in [r["res_rid"] for r in mod_assets["res_resources"].values()]:
                        raise RuntimeError(f"Check 3 Failed: TextureAsset EBX '{e_name}' RID 0x{t_rid:016X} not found in bundle or items.toc RES")

        if len(chks) != len(cmeta_entries):
            raise RuntimeError(f"Check 3 Failed: Bundle '{b_name}' chunk count ({len(chks)}) != cmeta entry count ({len(cmeta_entries)})")

        chks_by_guid = {bytes(c["guid_raw"]): c for c in chks}
        for c_idx, c_info in enumerate(chks):
            g_raw = bytes(c_info["guid_raw"])
            c_pos, c_esz, _ = cmeta_entries[c_idx]
            if c_info["log_sz"] >= 1398128 and c_esz == 36:
                first_mip = struct.unpack_from("<I", cmeta, c_pos + 30)[0]
                if first_mip != 0:
                    raise RuntimeError(f"Check 3 Failed: Custom texture chunk {g_raw.hex()} has cmeta firstMip={first_mip} (MUST be 0)")

    if not found_custom_bundle:
        raise RuntimeError("Check 3 Failed: No custom standalone bundle found in buildkititems.toc")

    print(f"  [PASS] Check 3: All resource RIDs (MeshSet, Havok, Texture), chunk GUIDs, and cmeta firstMip == 0 verified.", flush=True)


def _validate_check_4_shader_roots(mod_assets: dict, parsed_ebxs: dict | None = None, coexist_mode: bool = True) -> None:
    """Check 4: Every Shader Root Hash Must Resolve in Standalone _sbd (with intact internal directory & texture binding) and globals.toc."""
    res_resources = mod_assets["res_resources"]

    sbd_resources = {
        k: v
        for k, v in res_resources.items()
        if (k.endswith("_sbd") or "_bundle_win32_shaderstate/shaderblockdepot_" in k)
        and v["res_type"] == 0x73312045
        and "buildkititems.toc" in v.get("toc", "")
    }
    if not sbd_resources:
        print("  [PASS] Check 4: No custom _sbd resources present; Check 4 passed.", flush=True)
        return

    DONOR_NEUTRAL_WHITE_HASH = 0xF136CD5C26C6D084
    exported_tex_pairs = set()
    if parsed_ebxs:
        for k, v in parsed_ebxs.items():
            if k.endswith("_c") and len(v.get("ebxd", b"")) >= 28 and len(v.get("efix", b"")) >= 16:
                exported_tex_pairs.add((bytes(v["ebxd"][12:28]), bytes(v["efix"][0:16])))

    for sbd_key, sbd_data in sbd_resources.items():
        sbd_raw = sbd_data["raw"]
        if len(sbd_raw) < 0x0A50:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' truncated ({len(sbd_raw)} bytes)")

        rh_even = struct.unpack_from("<Q", sbd_raw, 0x10)[0]
        rh_odd = struct.unpack_from("<Q", sbd_raw, 0x20)[0]
        rh771_even = struct.unpack_from("<Q", sbd_raw, 0x30)[0]
        rh771_odd = struct.unpack_from("<Q", sbd_raw, 0x40)[0]
        if rh_even == 0 or rh_odd == 0 or (rh_even >> 32) == 0:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' has uninitialized root hashes (even=0x{rh_even:016X}, odd=0x{rh_odd:016X})")
        if (rh_even >> 32) != (rh_odd >> 32) or ((rh_odd - rh_even) & 0xFFFFFFFFFFFFFFFF) != 0x256B6804:
            raise RuntimeError(
                f"Check 4 Failed: _sbd '{sbd_key}' violates root-hash pair invariant "
                f"(even=0x{rh_even:016X}, odd=0x{rh_odd:016X}, diff=0x{(rh_odd - rh_even) & 0xFFFFFFFFFFFFFFFF:016X})"
            )
        if (rh771_even >> 32) != (rh771_odd >> 32) or ((rh771_odd - rh771_even) & 0xFFFFFFFFFFFFFFFF) != 0x256B6804:
            raise RuntimeError(
                f"Check 4 Failed: _sbd '{sbd_key}' violates 0x771 root-hash pair invariant "
                f"(even=0x{rh771_even:016X}, odd=0x{rh771_odd:016X})"
            )
        if coexist_mode and rh_even != DONOR_NEUTRAL_WHITE_HASH:
            if (rh_even & 0xFFFFFFFF) != 0x26C6D084 or (rh_odd & 0xFFFFFFFF) != 0x4C323888:
                raise RuntimeError(
                    f"Check 4 Failed: _sbd '{sbd_key}' violates coexist low-32 root-hash invariant "
                    f"(lo32_even=0x{rh_even & 0xFFFFFFFF:08X}, lo32_odd=0x{rh_odd & 0xFFFFFFFF:08X})"
                )
            if (rh771_even & 0xFFFFFFFF) != 0x4A3CB802 or (rh771_odd & 0xFFFFFFFF) != 0x6FA82006:
                raise RuntimeError(
                    f"Check 4 Failed: _sbd '{sbd_key}' violates coexist 0x771 low-32 root-hash invariant "
                    f"(lo32_even=0x{rh771_even & 0xFFFFFFFF:08X}, lo32_odd=0x{rh771_odd & 0xFFFFFFFF:08X})"
                )

        # Verify matching MeshSet section root hashes (+0x148: LOD0/LOD1 -> rh771_even, LOD2 -> rh_even)
        if "_bundle_win32_shaderstate/shaderblockdepot_" in sbd_key:
            ms_key = sbd_key.split("_bundle_win32_shaderstate/")[0] + "_mesh"
        else:
            ms_key = sbd_key[:-4] + "_mesh"
        if ms_key in res_resources:
            ms_raw = res_resources[ms_key]["raw"]
            if len(ms_raw) == 4192:
                sec_checks = ((0x2D0, {rh771_even}), (0x450, {rh_even}), (0x5D0, {rh_even}), (0x750, {rh771_even}), (0xBD0, {rh771_even}))
            else:
                sec_checks = ((0x2D0, {rh_even, rh771_even}), (0x450, {rh_even, rh771_even}), (0x5D0, {rh_even, rh771_even}))
            for sec_off, allowed_rhs in sec_checks:
                if sec_off + 0x150 <= len(ms_raw):
                    ms_rh = struct.unpack_from("<Q", ms_raw, sec_off + 0x148)[0]
                    if ms_rh not in allowed_rhs:
                        raise RuntimeError(
                            f"Check 4 Failed: MeshSet '{ms_key}' sec@0x{sec_off:X}+0x148 (0x{ms_rh:016X}) not in expected SBD roots ({[hex(r) for r in allowed_rhs]})"
                        )

        uv_atlas_slot = sbd_raw[0x085:0x0A5]
        tex_ig = bytes(uv_atlas_slot[:16])
        tex_fg = bytes(uv_atlas_slot[16:32])
        if tex_ig == b"\x00" * 16 or tex_fg == b"\x00" * 16:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' UV Atlas slot +0x15 has null texture GUIDs")

    # Filter for SBDs using non-vanilla synthetic root hashes that require Win32/globals.toc registration
    custom_sbds = {
        k: v for k, v in sbd_resources.items()
        if struct.unpack_from("<Q", v["raw"], 0x10)[0] != DONOR_NEUTRAL_WHITE_HASH
    }

    has_globals = any("globals.toc" in k for k in mod_assets["tocs"].keys())

    if not custom_sbds and not has_globals:
        print(
            f"  [PASS] Check 4: All {len(sbd_resources)} standalone _sbd depot(s) verified (intact SBD directory, valid sub0 link, matched MeshSet root hashes; custom texture bound: {any((bytes(v['raw'][0x085:0x095]), bytes(v['raw'][0x095:0x0A5])) in exported_tex_pairs for v in sbd_resources.values())}).",
            flush=True,
        )
        return

    if custom_sbds and not has_globals:
        raise RuntimeError("Check 4 Failed: Custom shader root hashes used without Win32/globals.toc")

    r1130 = None
    r2002 = None
    for r_key, r_data in res_resources.items():
        if r_data["res_type"] == 0x2D254A89:
            r1130 = r_data
        elif r_data["res_type"] == 0xE2E6955B:
            r2002 = r_data

    if r1130 is None or r2002 is None:
        raise RuntimeError("Check 4 Failed: globals.toc missing RES[1130] (0x2D254A89) or RES[2002] (0xE2E6955B)")

    r1130_raw = r1130["raw"]
    if len(r1130_raw) % 16 != 0:
        raise RuntimeError(f"Check 4 Failed: RES[1130] payload size ({len(r1130_raw)}) is not a multiple of 16")
    keys_1130 = set(struct.unpack_from("<Q", r1130_raw, i * 16)[0] for i in range(len(r1130_raw) // 16))

    r2002_raw = r2002["raw"]
    r2002_meta = r2002["res_meta"]
    if len(r2002_meta) != 16:
        raise RuntimeError(f"Check 4 Failed: RES[2002] res_meta is not 16 bytes (got {len(r2002_meta)})")
    prog_sz, reloc_sz, lk_sz = struct.unpack_from("<III", r2002_meta, 0)
    lk_off = prog_sz + reloc_sz
    if lk_off + 4 > len(r2002_raw):
        raise RuntimeError(f"Check 4 Failed: RES[2002] lookup offset 0x{lk_off:X} exceeds payload length {len(r2002_raw)}")
    n_ents_2002 = struct.unpack_from("<I", r2002_raw, lk_off)[0]
    pos = lk_off + 4
    keys_2002 = set()
    for _ in range(n_ents_2002):
        if pos + 12 > len(r2002_raw):
            break
        k = struct.unpack_from("<Q", r2002_raw, pos)[0]
        n_ids = struct.unpack_from("<I", r2002_raw, pos + 8)[0]
        keys_2002.add(k)
        pos += 12 + n_ids * 4

    target_sbds = custom_sbds if custom_sbds else sbd_resources
    for sbd_key, sbd_data in target_sbds.items():
        sbd_raw = sbd_data["raw"]
        rh_even = struct.unpack_from("<Q", sbd_raw, 0x10)[0]
        rh_odd = struct.unpack_from("<Q", sbd_raw, 0x20)[0]
        rh771_even = struct.unpack_from("<Q", sbd_raw, 0x30)[0]
        rh771_odd = struct.unpack_from("<Q", sbd_raw, 0x40)[0]

        uv_atlas_slot = sbd_raw[0x085:0x0A5]
        tex_ig = uv_atlas_slot[:16]
        tex_fg = uv_atlas_slot[16:32]
        if tex_ig == b"\x00" * 16 or tex_fg == b"\x00" * 16:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' UV Atlas slot +0x15 not bound to valid texture GUID pair")

        if rh_even not in keys_1130 or rh_odd not in keys_1130 or rh771_even not in keys_1130 or rh771_odd not in keys_1130:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' root hashes not found in globals.toc RES[1130] (TextureLookup)")
        if rh_even not in keys_2002 or rh_odd not in keys_2002 or rh771_even not in keys_2002 or rh771_odd not in keys_2002:
            raise RuntimeError(f"Check 4 Failed: _sbd '{sbd_key}' root hashes not found in globals.toc RES[2002] (ProgramLookup)")

    print(f"  [PASS] Check 4: Shader root hashes verified in standalone _sbd and globals.toc (RES[1130] & RES[2002]).", flush=True)


def _validate_check_5_conflict_gate(mod_dir: Path, mod_assets: dict, mod_name: str = "", coexist_mode: bool = True) -> None:
    """Check 5: Generalized Mod-Merger Conflict Gate Across ALL Enabled Mods in mods.json."""
    mod_dir = Path(mod_dir)
    if not mod_name:
        mod_name = mod_dir.name

    standalone_bundles = mod_assets["standalone_bundles"]
    for b_name, b_data in standalone_bundles.items():
        if "buildkititems.toc" in b_data.get("toc", ""):
            b_lower = b_name.lower()
            if "bkprops" in b_lower or ("customprops" not in b_lower and "items" in b_lower):
                raise RuntimeError(
                    f"Check 5 Failed (Vanilla Bundle Modification): Mod modifies base-game vanilla bundle "
                    f"'{b_name}' in buildkititems.toc (violates zero-donor standalone bundle rule)"
                )

    if coexist_mode:
        mods_reads = [p for p in LOGGED_INPUT_READS if "\\mods\\" in p.lower()]
        if mods_reads:
            raise RuntimeError(
                f"Check 5 Failed (Forbidden Mods\\ Read): Compiler read from Mods\\ during coexist build: {mods_reads}"
            )
        print("  [PASS] Check 5: Standalone bundle rule & zero Mods\\ reads verified in coexist_mode.", flush=True)
        return

    mods_json_p = GAME_DIR / "Mods" / "mods.json"
    if not mods_json_p.exists():
        print("  [!] mods.json not found; skipping cross-mod conflict check.", flush=True)
        return

    try:
        mods_data = json.loads(mods_json_p.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"  [!] Failed to parse mods.json ({ex}); skipping cross-mod conflict check.", flush=True)
        return

    mod_prefix = (mod_name.split("-")[0] + "-") if "-" in mod_name else ""
    enabled_mod_names = [
        m.get("name")
        for m in mods_data.get("mods", [])
        if m.get("enabled", False)
        and m.get("name")
        and m.get("name") != mod_name
        and not (mod_prefix and str(m.get("name", "")).startswith(mod_prefix))
    ]

    our_patched_assets = {}
    for b_name, b_data in standalone_bundles.items():
        toc_rel = b_data.get("toc", "")
        streams = b_data.get("streams", [])
        ebxs = b_data.get("ebxs", [])
        ress = b_data.get("ress", [])
        chks = b_data.get("chks", [])

        for i, e in enumerate(ebxs):
            if streams[1 + i][0] == 1:
                our_patched_assets[(toc_rel, b_name, e["name"].lower())] = ("ebx", 0)
        for i, r in enumerate(ress):
            if streams[1 + len(ebxs) + i][0] == 1:
                our_patched_assets[(toc_rel, b_name, r["name"].lower())] = ("res", r["res_type"])
        for i, c in enumerate(chks):
            if streams[1 + len(ebxs) + len(ress) + i][0] == 1:
                our_patched_assets[(toc_rel, b_name, bytes(c["guid_raw"]).hex())] = ("chk", 0)

    for other_name in enabled_mod_names:
        other_dir = GAME_DIR / "Mods" / other_name
        if not other_dir.exists():
            continue

        for toc_file in other_dir.rglob("*.toc"):
            if toc_file.name.lower() == "layout.toc":
                continue
            rel_toc = str(toc_file.relative_to(other_dir)).replace("\\", "/").lower()
            toc_bytes = toc_file.read_bytes()
            if len(toc_bytes) < 568:
                continue
            pl = toc_bytes[556:] if toc_bytes[:4] == b"\x00\xd1\xce\x01" else toc_bytes
            iw = struct.unpack_from(">12I", pl, 0)
            w0, w1, n_b, _, _, _, _, _, str_off = iw[:9]
            if w1 + n_b * 16 > len(pl):
                continue

            other_cas = other_dir / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas"
            if not other_cas.exists():
                other_cas_list = list(other_dir.rglob("*.cas"))
                other_cas = other_cas_list[0] if other_cas_list else None
            other_cas_bytes = other_cas.read_bytes() if other_cas and other_cas.exists() else None
            if not other_cas_bytes:
                continue

            for b_idx in range(n_b):
                name_off, desc_sz, desc_off = struct.unpack_from(">IIQ", pl, w1 + b_idx * 16)
                s_end = pl.index(0, str_off + name_off)
                b_name = pl[str_off + name_off : s_end].decode("ascii", errors="replace").lower()
                real_sz = desc_sz & 0x3FFFFFFF
                streams = bsp.parse_bundle_desc(pl[desc_off : desc_off + real_sz])
                if streams[0][0] != 1:
                    continue

                try:
                    sb_raw = _get_sbdata_payload(other_cas_bytes[streams[0][3] : streams[0][3] + streams[0][4]])
                    if len(sb_raw) < 8 or struct.unpack_from("<I", sb_raw, 4)[0] != 0x9D798ED6:
                        continue
                    _, o_ebxs, o_ress, o_chks, _ = bsp.parse_sbdata_full(sb_raw)
                except Exception:
                    continue

                for i, e in enumerate(o_ebxs):
                    if streams[1 + i][0] == 1:
                        aname = e["name"].lower()
                        key = (rel_toc, b_name, aname)
                        if key in our_patched_assets:
                            if aname != "items/itemmastercollection":
                                raise RuntimeError(
                                    f"Check 5 Failed (Mod-Merger EBX Collision): Asset '{aname}' in bundle '{b_name}' "
                                    f"({rel_toc}) is modified by both '{mod_name}' and enabled mod '{other_name}'"
                                )

                for i, r in enumerate(o_ress):
                    if streams[1 + len(o_ebxs) + i][0] == 1:
                        aname = r["name"].lower()
                        key = (rel_toc, b_name, aname)
                        if key in our_patched_assets:
                            rtype = r["res_type"]
                            is_combinable = (
                                rtype in COMBINABLE_RES_TYPES
                                or aname.endswith("_bundlereftable")
                            )
                            if not is_combinable:
                                raise RuntimeError(
                                    f"Check 5 Failed (Mod-Merger Uncombinable Resource Collision): Resource '{aname}' "
                                    f"(type 0x{rtype:08X}) in bundle '{b_name}' ({rel_toc}) is modified by both "
                                    f"'{mod_name}' and enabled mod '{other_name}'"
                                )

                for i, c in enumerate(o_chks):
                    if streams[1 + len(o_ebxs) + len(o_ress) + i][0] == 1:
                        cg = bytes(c["guid_raw"]).hex()
                        key = (rel_toc, b_name, cg)
                        if key in our_patched_assets:
                            raise RuntimeError(
                                f"Check 5 Failed (Mod-Merger Chunk Collision): Chunk {cg} in bundle '{b_name}' "
                                f"({rel_toc}) is modified by both '{mod_name}' and enabled mod '{other_name}'"
                            )

    print(f"  [PASS] Check 5: Mod-Merger Conflict Gate scanned {len(enabled_mod_names)} enabled mod(s) in mods.json with zero uncombinable collisions.", flush=True)


def validate_compiled_mod_preflight(mod_dir: Path, mod_name: str = "", coexist_mode: bool = True) -> None:
    """
    Pre-Flight Fail-Loud Validator (Checks 1-5).
    Fails immediately with a descriptive RuntimeError if any check fails.
    """
    mod_dir = Path(mod_dir)
    if not mod_dir.exists():
        raise RuntimeError(f"Pre-flight Validator Error: Mod directory does not exist: {mod_dir}")
    if not mod_name:
        mod_name = mod_dir.name

    print(f"\n[VALIDATOR] Starting Pre-Flight Integrity Validation for '{mod_name}'...", flush=True)
    assets = _collect_mod_assets(mod_dir)
    parsed_ebxs = _validate_check_1_riff_ebx_integrity(assets["ebx_documents"])
    _validate_check_2_guid_imports(parsed_ebxs, assets, coexist_mode=coexist_mode)
    _validate_check_3_rids_and_chunks(assets, parsed_ebxs, coexist_mode=coexist_mode)
    _validate_check_4_shader_roots(assets, parsed_ebxs, coexist_mode=coexist_mode)
    _validate_check_5_conflict_gate(mod_dir, assets, mod_name=mod_name, coexist_mode=coexist_mode)
    print(f"[VALIDATOR] [SUCCESS] All 5 Pre-Flight Checks Passed Cleanly for '{mod_name}'.\n", flush=True)


def _load_templates_cached(staging_root_dir: Path, fast_mode: bool = True) -> tuple[dict, bytes, bytes, bytes | None]:
    """
    Loads Bundle 39 / 94 / 421 and items.toc donor templates, disk-caching the unpacked tuple
    in `<Staging>/.compiler_cache/base_templates_v1.pkl` when `fast_mode=True` (keyed by compiler version,
    game_dir, file size, and mtime_ns of `buildkititems.toc`, `items.toc`, `globals.toc`, and `items.toc`).
    """
    if not fast_mode:
        return load_templates()
    try:
        import pickle
        cache_root = staging_root_dir.parent / ".compiler_cache" if staging_root_dir.name == "Pack_Queue" else (staging_root_dir / ".compiler_cache")
        cache_root.mkdir(parents=True, exist_ok=True)
        tpl_cache_file = cache_root / "base_templates_v1.pkl"
        sig_parts = [f"compiler:{COMPILER_VERSION}", f"gamedir:{GAME_DIR}", "schema_v29_0_b39_b94_b421"]
        for cand_p in (
            GAME_DIR / "Data" / "Win32" / "buildkititems.toc",
            GAME_DIR / "Data" / "Win32" / "items.toc",
            GAME_DIR / "Data" / "Win32" / "globals.toc",
        ):
            if cand_p.exists():
                st = cand_p.stat()
                sig_parts.append(f"{cand_p.name}:{st.st_size}:{st.st_mtime_ns}")
            else:
                sig_parts.append(f"{cand_p.name}:missing")
        sig_str = "|".join(sig_parts)
        if tpl_cache_file.is_file():
            saved_sig, saved_tuple = pickle.loads(tpl_cache_file.read_bytes())
            if saved_sig == sig_str and isinstance(saved_tuple, tuple) and len(saved_tuple) == 4:
                return saved_tuple
        loaded_tuple = load_templates()
        try:
            tpl_cache_file.write_bytes(pickle.dumps((sig_str, loaded_tuple), protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            pass
        return loaded_tuple
    except Exception:
        return load_templates()


def main():
    global PIPELINE_MODE
    t_compile_start = time.perf_counter()
    print(f"=== ReSkate Prop Studio Standalone Compiler v{COMPILER_VERSION} ===", flush=True)
    parser = argparse.ArgumentParser(description="ReSkate Blender Prop Studio Standalone CLI Compiler")
    parser.add_argument("manifest_pos", nargs="?", default=None, help="Path to prop_manifest.json or pack_manifest.json")
    parser.add_argument("--manifest", dest="manifest_flag", default=None, help="Path to single prop_manifest.json")
    parser.add_argument("--pack-manifest", dest="pack_manifest", default=None, help="Path to multi-prop pack_manifest.json")
    parser.add_argument("--staging-dir", default=str(DEFAULT_STAGING_DIR), help="Output staging directory")
    parser.add_argument("--target-mod", default="BlenderAuthoredProps", help="Target mod name")
    parser.add_argument("--include-2a", action="store_true", default=False, help="Include [2A 1:1 Mesh] companion variant")
    parser.add_argument("--no-2a", dest="include_2a", action="store_false", help="Disable [2A 1:1 Mesh] companion variant")
    parser.add_argument("--fbmod-only", action="store_true", default=False, help="Export .fbmod only without deploying mod folder")
    parser.add_argument("--check-collision-only", action="store_true", default=False, help="Build and cache Havok collision & placement hulls and output collision_check_result.json without packaging .fbmod")
    parser.add_argument("--merge-existing", action="store_true", default=False, help="Add/merge props into existing target mod folder without overwriting prior props")
    parser.add_argument("--coexist-mode", dest="coexist_mode", action="store_true", default=None, help="Enable coexist mode (default in 0.20.0+)")
    parser.add_argument("--legacy-mode", dest="coexist_mode", action="store_false", help="Disable coexist mode (legacy diagnostic use only)")
    parser.add_argument(
        "--pipeline-mode",
        dest="pipeline_mode",
        default=None,
        choices=["FAST_OPTIMIZED", "LEGACY_FULL", "fast_optimized", "legacy_full"],
        help="Compilation pipeline mode: FAST_OPTIMIZED (deduplicated & cached, default) or LEGACY_FULL (uncached rebuild for A/B test)",
    )

    # Direct fallback & Zero-Setup .glb Drop-In Inbox flags
    parser.add_argument("--drop-glb", nargs="?", const="DEFAULT_INBOX", default=None, help="Compile all raw .glb files in QuickDrop_GLB_Inbox (or specified folder) with zero manual setup")
    parser.add_argument("--glb", default=None, help="Direct GLB path")
    parser.add_argument("--texture", default=None, help="Direct texture atlas PNG path")
    parser.add_argument("--prop-key", default=None, help="Direct prop key slug")
    parser.add_argument("--display-name", default=None, help="Direct display name")
    parser.add_argument("--category", default="Custom Props", help="Direct category name")
    parser.add_argument("--surface-code", type=int, default=96, help="Direct packed surface code")
    parser.add_argument("--collision-mode", default="SAME_AS_MESH", help="Collision mode")

    args = parser.parse_args()

    manifest_file = args.manifest_flag or args.pack_manifest or args.manifest_pos

    try:
        emit_step(1, 6, "Parsing prop manifest")
        emit_progress(5, "Loading manifest configuration...")
        if args.drop_glb is not None:
            inbox_dir = (
                Path(__file__).resolve().parent / "QuickDrop_GLB_Inbox"
                if args.drop_glb == "DEFAULT_INBOX"
                else Path(args.drop_glb)
            )
            inbox_dir.mkdir(parents=True, exist_ok=True)
            glb_files = sorted(
                p for p in inbox_dir.glob("*.glb")
                if not p.stem.lower().endswith("_col")
            )
            if not glb_files:
                raise RuntimeError(f"No .glb files found in Drop-In Inbox: {inbox_dir}. Drop any .glb file there and run again!")
            coexist_default = True if args.coexist_mode is None else bool(args.coexist_mode)
            target_mod = args.target_mod if args.target_mod != "BlenderAuthoredProps" else "QuickTest_GLB_Props"
            props_list = []
            # Generate Concrete, Metal, and Wood sound variants grouped onto one X/Square tile per .glb!
            sound_presets = [
                ("Concrete_Solid", 96, ""),
                ("Metal_Grind_Pipe_Hollow", 52, " [Metal]"),
                ("Wood_Plywood", 112, " [Wood]"),
            ]
            for gpath in glb_files:
                base_stem = re.sub(r"[^a-z0-9_]+", "_", gpath.stem.lower()).strip("_") or "glb_prop"
                base_title = gpath.stem.replace("_", " ").replace("-", " ").strip().title() or "GLB Prop"
                col_cand = gpath.with_name(f"{gpath.stem}_col.glb")
                for surf_id, surf_code, suffix_label in sound_presets:
                    var_slug = base_stem if not suffix_label else f"{base_stem}_{surf_id.split('_')[0].lower()}"
                    props_list.append({
                        "prop_key": f"own_bkquickglb_{var_slug}",
                        "display_name": f"{base_title}{suffix_label}",
                        "family_title": base_title,
                        "category": "CUSTOM_TAB",
                        "custom_qd_tab": "bk_quickglb",
                        "glb_path": str(gpath),
                        "col_glb_path": str(col_cand) if col_cand.is_file() else None,
                        "surface_id": surf_id,
                        "surface_material_code": surf_code,
                        "collision_mode": "CUSTOM_COL_GLB" if col_cand.is_file() else "SAME_AS_MESH",
                    })
            author_env = os.environ.get("RESKATE_AUTHOR_NAME", "").strip()
            meta = {
                "pack_name": target_mod,
                "target_mod_name": target_mod,
                "author_name": author_env,
                "output_staging_dir": Path(args.staging_dir),
                "include_2a_variant": False,
                "export_fbmod_only": args.fbmod_only,
                "merge_existing_mod": bool(args.merge_existing),
                "coexist_mode": coexist_default,
                "coexist_namespace": f"{author_env}/{target_mod}" if author_env else target_mod,
                "deploy_to_mods": True,
            }
            print(f"  [Drop-In GLB Inbox] Auto-packaged {len(glb_files)} .glb file(s) ({len(props_list)} sound variants) -> {target_mod}", flush=True)
        elif manifest_file:
            manifest_p = Path(manifest_file)
            props_list, meta = parse_manifest(manifest_p)
            if args.merge_existing:
                meta["merge_existing_mod"] = True
            if args.coexist_mode is not None:
                meta["coexist_mode"] = bool(args.coexist_mode)
        elif args.glb:
            coexist_default = True if args.coexist_mode is None else bool(args.coexist_mode)
            author_env = os.environ.get("RESKATE_AUTHOR_NAME", "").strip()
            props_list = [{
                "prop_key": args.prop_key or "own_customprops_test",
                "display_name": args.display_name or "Test Prop",
                "category": args.category,
                "glb_path": args.glb,
                "texture_path": args.texture,
                "surface_material_code": args.surface_code,
                "collision_mode": args.collision_mode,
            }]
            meta = {
                "pack_name": args.target_mod,
                "target_mod_name": args.target_mod,
                "author_name": author_env,
                "output_staging_dir": Path(args.staging_dir),
                "include_2a_variant": args.include_2a,
                "export_fbmod_only": args.fbmod_only,
                "merge_existing_mod": bool(args.merge_existing),
                "coexist_mode": coexist_default,
                "coexist_namespace": f"{author_env}/{args.target_mod}" if author_env else args.target_mod,
            }
        else:
            default_p = DEFAULT_STAGING_DIR / "prop_manifest.json"
            if default_p.exists():
                props_list, meta = parse_manifest(default_p)
                if args.merge_existing:
                    meta["merge_existing_mod"] = True
                if args.coexist_mode is not None:
                    meta["coexist_mode"] = bool(args.coexist_mode)
            else:
                parser.print_help()
                sys.exit(1)

        if args.pipeline_mode:
            meta["compile_pipeline_mode"] = str(args.pipeline_mode).strip().upper()
        PIPELINE_MODE = str(
            meta.get("compile_pipeline_mode") or os.environ.get("RESKATE_PIPELINE_MODE") or "FAST_OPTIMIZED"
        ).strip().upper()
        if PIPELINE_MODE not in ("FAST_OPTIMIZED", "LEGACY_FULL"):
            PIPELINE_MODE = "FAST_OPTIMIZED"
        os.environ["RESKATE_PIPELINE_MODE"] = PIPELINE_MODE
        is_fast_pipeline = (PIPELINE_MODE == "FAST_OPTIMIZED")

        staging_root_dir = Path(meta.get("output_staging_dir") or _get_staging_dir())
        _apply_game_and_staging_runtime_paths(meta.get("game_dir"), staging_root_dir)

        coexist_flag = bool(meta.get("coexist_mode", True))
        props_list = _merge_existing_mod_props_if_requested(props_list, meta)
        meta["source_props_list"] = list(props_list)
        props_list = _ensure_pack_icon_first_prop(props_list, meta, staging_root_dir)

        emit_step(2, 6, f"Loading Frostbite Bundle 39 templates [{PIPELINE_MODE}]")
        emit_progress(15, "Decompressing Bundle 39 & items.toc templates...")
        log_input_read(GAME_DIR / "Data" / "Win32" / "buildkititems.toc", coexist_mode=coexist_flag)
        log_input_read(GAME_DIR / "Data" / "Win32" / "items.toc", coexist_mode=coexist_flag)
        log_input_read(GAME_DIR / "Data" / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas", coexist_mode=coexist_flag)
        tpl, item_tpl, imc_tpl, noble_effect_ebx = _load_templates_cached(staging_root_dir, fast_mode=is_fast_pipeline)

        emit_progress(16, f"Scanning prop compile cache ({len(props_list)} prop(s))...")
        import pickle
        import concurrent.futures

        cache_dir = staging_root_dir / ".prop_compile_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        pack_mod_name = meta.get("target_mod_name") or meta.get("pack_name") or "BlenderAuthoredProps"
        pack_fallback_cache_dir = _get_pack_storage_dir(staging_root_dir, pack_mod_name) / "cache"
        pack_fallback_cache_dir.mkdir(parents=True, exist_ok=True)

        _VOLATILE_PATH_KEYS = {
            "output_staging_dir",
            "glb_path",
            "col_glb_path",
            "pphys_glb_path",
            "texture_path",
            "normal_texture_path",
            "mask_texture_path",
            "thumb_256_path",
            "thumb_768_path",
            "custom_thumbnail_path",
            "thumbnail_path",
            "built_at",
            "compiler_version",
        }

        def _compute_prop_cache_key(pe: dict) -> str:
            h = hashlib.sha256()
            h.update(f"compiler:{COMPILER_VERSION}|gamedir:{GAME_DIR}|schema:v29_0_method_c_decal_content_sha1|".encode("utf-8"))
            key_fields = {}
            for k, v in pe.items():
                if k in _VOLATILE_PATH_KEYS:
                    continue
                if k == "material_textures" and isinstance(v, list):
                    clean_mts = []
                    for mt in v:
                        if isinstance(mt, dict):
                            clean_mts.append({
                                mk: mv for mk, mv in mt.items()
                                if mk not in ("texture_path", "normal_texture_path", "mask_texture_path")
                            })
                        else:
                            clean_mts.append(mt)
                    key_fields[k] = clean_mts
                else:
                    key_fields[k] = v
            h.update(json.dumps(key_fields, sort_keys=True, default=str).encode("utf-8"))
            file_paths = [
                ("glb", pe.get("glb_path")),
                ("col_glb", pe.get("col_glb_path")),
                ("pphys_glb", pe.get("pphys_glb_path")),
                ("tex", pe.get("texture_path")),
                ("norm", pe.get("normal_texture_path")),
                ("msk", pe.get("mask_texture_path")),
                ("t256", pe.get("thumb_256_path")),
                ("t768", pe.get("thumb_768_path")),
            ]
            for mt_i, mt in enumerate(pe.get("material_textures") or []):
                if isinstance(mt, dict):
                    file_paths.extend([
                        (f"mt{mt_i}_c", mt.get("texture_path")),
                        (f"mt{mt_i}_n", mt.get("normal_texture_path")),
                        (f"mt{mt_i}_m", mt.get("mask_texture_path")),
                    ])
            for tag_k, fp in file_paths:
                if fp and Path(fp).is_file():
                    try:
                        content_sha1 = hashlib.sha1(Path(fp).read_bytes()).hexdigest()
                        h.update(f"|{tag_k}:{content_sha1}".encode("utf-8"))
                    except Exception:
                        pass
            return h.hexdigest()

        n_props = max(1, len(props_list))
        cached_props = [None] * len(props_list)
        miss_indices = []
        miss_keys = {}
        cache_hit_count = 0

        for idx, prop_entry in enumerate(props_list):
            t_prop_chk = time.perf_counter()
            _refresh_pack_asset_cache_for_prop(prop_entry, staging_dir=staging_root_dir, mod_name=pack_mod_name)
            ck = _compute_prop_cache_key(prop_entry)
            cp_file = cache_dir / f"{ck}.pkl"
            if is_fast_pipeline and cp_file.exists():
                try:
                    cached_props[idx] = pickle.loads(cp_file.read_bytes())
                    cached_props[idx]["_is_cached"] = True
                    if isinstance(cached_props[idx].get("spec"), dict):
                        for _mk in ("surface_label", "material_surfaces", "surface_id", "display_name", "family_title", "custom_qd_tab", "is_pack_icon_prop"):
                            if _mk in prop_entry and prop_entry[_mk] is not None:
                                cached_props[idx]["spec"][_mk] = prop_entry[_mk]
                    cache_hit_count += 1
                    dname = sanitize_display_name(prop_entry.get("display_name", "prop"))
                    dt_hit = time.perf_counter() - t_prop_chk
                    print(f"  [PropCache] ⚡ Cache HIT for '{dname}' ({dt_hit:.2f}s)", flush=True)
                    emit_prop_cached(idx + 1, n_props, dname)
                    pct = 20 + int(((idx + 1) / n_props) * 35)
                    emit_progress(pct, f"Cached [{idx + 1}/{n_props}] {dname} (unchanged)")
                    continue
                except Exception:
                    pass
            miss_indices.append(idx)
            miss_keys[idx] = ck

        # Emit Step 3 banner post-cache scan with honest changed vs cached prop counts
        if not miss_indices:
            emit_step(3, 6, f"Reusing {cache_hit_count}/{n_props} cached props (0 to compile)")
        elif cache_hit_count > 0:
            emit_step(3, 6, f"Compiling {len(miss_indices)} changed prop(s) ({cache_hit_count}/{n_props} cached)")
        else:
            emit_step(3, 6, f"Compiling geometry, BC7 textures & Havok collision ({n_props} prop(s))")

        def _safe_preprocess_prop(idx_in: int):
            pe = props_list[idx_in]
            dname_in = sanitize_display_name(pe.get("display_name", "prop"))
            pk_norm = _normalize_prop_key(pe.get("prop_id") or pe.get("prop_key", f"prop_{idx_in}"))
            fb_pkl = pack_fallback_cache_dir / f"{pk_norm}_latest.pkl"
            try:
                cp_res = preprocess_prop_manifest_entry(pe, tpl, prop_idx=idx_in + 1, prop_total=n_props)
                try:
                    raw_pkl = pickle.dumps(cp_res, protocol=pickle.HIGHEST_PROTOCOL)
                    (cache_dir / f"{miss_keys[idx_in]}.pkl").write_bytes(raw_pkl)
                    fb_pkl.write_bytes(raw_pkl)
                except Exception:
                    pass
                cp_res["_is_cached"] = False
                return cp_res
            except Exception as prop_err:
                print(f"  [Pack Safeguard] Prop '{dname_in}' ({pk_norm}) failed during compilation: {prop_err}", flush=True)
                traceback.print_exc()
                if fb_pkl.exists():
                    try:
                        recovered = pickle.loads(fb_pkl.read_bytes())
                        recovered["_is_cached"] = True
                        print(f"  [Pack Safeguard] Recovered last-known-good cached build for '{dname_in}' from {fb_pkl.name}", flush=True)
                        return recovered
                    except Exception:
                        pass
                if len(props_list) <= 1:
                    raise
                print(f"  [Pack Safeguard] Skipping failed prop '{dname_in}' so remaining pack props can finish compiling.", flush=True)
                return None

        compiler_backend = str(meta.get("compiler_backend") or "CPU").upper()
        if compiler_backend == "GPU_EXP":
            print("  [Compiler Backend] GPU / Fast Parallel Pipeline Active (Experimental Mode)", flush=True)
        if len(miss_indices) == 1:
            idx = miss_indices[0]
            prop_entry = props_list[idx]
            dname = sanitize_display_name(prop_entry.get("display_name", "prop"))
            pct = 20 + int((idx / n_props) * 35)
            emit_progress(pct, f"Processing [{idx + 1}/{n_props}] {dname}...")
            cached_props[idx] = _safe_preprocess_prop(idx)
        elif len(miss_indices) > 1:
            max_w = min(max(6, os.cpu_count() or 8) if compiler_backend == "GPU_EXP" else 4, len(miss_indices))
            emit_progress(22, f"Compiling {len(miss_indices)} modified props in parallel ({max_w} workers, {compiler_backend})...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as prop_pool:
                fut_map = {
                    prop_pool.submit(_safe_preprocess_prop, idx): idx
                    for idx in miss_indices
                }
                done_cnt = 0
                for fut in concurrent.futures.as_completed(fut_map):
                    idx = fut_map[fut]
                    cp = fut.result()
                    cached_props[idx] = cp
                    done_cnt += 1
                    dname = sanitize_display_name(props_list[idx].get("display_name", "prop"))
                    pct = 20 + int((done_cnt / len(miss_indices)) * 35)
                    status_verb = "Compiled" if cp is not None else "Failed"
                    emit_progress(pct, f"{status_verb} [{done_cnt}/{len(miss_indices)}] {dname}")

        valid_cached_props = [cp for cp in cached_props if cp is not None]
        if not valid_cached_props:
            raise RuntimeError("All props in manifest failed compilation.")

        if getattr(args, "check_collision_only", False):
            cp0 = valid_cached_props[0]
            col_prev = dict(cp0.get("collision_preview") or {})
            col_prev["cache_key"] = miss_keys.get(0) or _compute_prop_cache_key(props_list[0])
            col_prev["cached_prop_pkl"] = str(cache_dir / f"{col_prev['cache_key']}.pkl")
            out_col_json = staging_root_dir / "collision_check_result.json"
            out_col_json.write_text(json.dumps(col_prev, indent=2), encoding="utf-8")
            emit_progress(100, "Collision check complete (cached for compile)")
            emit_done(out_col_json)
            sys.exit(0)

        out_fbmod = compile_blender_props_package(
            valid_cached_props,
            meta,
            tpl,
            item_tpl,
            imc_tpl,
            noble_effect_ebx,
        )

        print(f"\n[INPUT-AUDIT] Total input files read during compile ({len(LOGGED_INPUT_READS)}):", flush=True)
        for p_in in LOGGED_INPUT_READS:
            print(f"  - {p_in}", flush=True)

        if out_fbmod.exists() and out_fbmod.stat().st_size > 0:
            total_elapsed_s = time.perf_counter() - t_compile_start
            mode_short = "Fast" if is_fast_pipeline else "Legacy"
            print(
                f"COMPILE_SUMMARY mode={PIPELINE_MODE} mode_label={mode_short} elapsed={total_elapsed_s:.2f}s cache_hits={cache_hit_count}/{n_props}",
                flush=True,
            )
            print(
                f"[SUCCESS] ReSkate Prop Mod compiled successfully in {total_elapsed_s:.2f}s "
                f"[Mode: {mode_short}, Cache Hits: {cache_hit_count}/{n_props}]: {out_fbmod}",
                flush=True,
            )
            sys.exit(0)
        else:
            emit_error("Failed to compile mod archive.")
            sys.exit(1)
    except Exception as ex:
        emit_error(str(ex))
        raise


if __name__ == "__main__":
    main()
