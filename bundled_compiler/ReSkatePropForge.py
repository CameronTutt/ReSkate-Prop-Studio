r"""
ReSkatePropForge.py (v3.0 Studio)
---------------------------------
Standalone Custom 3D Prop Importer, Multi-Material PBR Atlas Compiler,
Automated Havok Multi-Shape Collision Compiler (trimesh + CoACD + Native hknp Compiler),
Interactive Per-Material Surface Audio & Click-to-Draw Grind Spline Studio,
Blender 5.3 Beta Bridge, & Quick Drop Catalog Exporter.
"""

import base64
import ctypes
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw
from pygltflib import GLTF2, Material as GLTFMaterial

try:
    import coacd
    HAS_COACD = True
except Exception:
    HAS_COACD = False

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False

# ==============================================================================
# PATHS & CONSTANTS (STRICTLY ISOLATED FROM WORKING MODS)
# ==============================================================================
def _get_staging_dir() -> Path:
    env_stg = os.environ.get("RESKATE_STAGING_DIR", "").strip()
    if env_stg:
        return Path(env_stg)
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
    return base / "ReSkate_Prop_Studio" / "Staging"


PROPFORGE_DIR = Path(__file__).resolve().parent
_BUNDLED_ROOT = PROPFORGE_DIR
STUDIO_DIR = Path(os.environ.get("RESKATE_STUDIO_DIR") or _BUNDLED_ROOT)
_TEMPLATE_DIR = _BUNDLED_ROOT
_DEFAULT_GAME_DIR = STUDIO_DIR.parent if (STUDIO_DIR.parent / "Data" / "Win32").exists() else Path.cwd()
GAME_DIR = Path(os.environ.get("RESKATE_GAME_DIR") or _DEFAULT_GAME_DIR)
CLI_EXE = Path(os.environ.get("RESKATE_CLI_EXE") or (STUDIO_DIR / "reskate_cli.exe"))
if not CLI_EXE.is_file() and (GAME_DIR / "ReSkate Studio" / "reskate_cli.exe").is_file():
    CLI_EXE = GAME_DIR / "ReSkate Studio" / "reskate_cli.exe"
elif not CLI_EXE.is_file() and (GAME_DIR / "reskate_cli.exe").is_file():
    CLI_EXE = GAME_DIR / "reskate_cli.exe"
OODLE_DLL_PATH = GAME_DIR / "oo2core_9_win64.dll"
STAGING_DIR = _get_staging_dir()
SHARED_BUNDLE_HASH = 0x04308B5D
TEMPLATE_BUNDLE_IDX = 39

# ==============================================================================
# OODLE DECOMPRESSION & CAS / TOC HELPERS
# ==============================================================================
_OODLE = None


def get_oodle():
    global _OODLE
    if _OODLE is None:
        oodle_p = OODLE_DLL_PATH if OODLE_DLL_PATH.is_file() else (GAME_DIR / "oo2core_9_win64.dll")
        _OODLE = ctypes.WinDLL(str(oodle_p))
        _OODLE.OodleLZ_Decompress.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
        ]
        _OODLE.OodleLZ_Decompress.restype = ctypes.c_ssize_t
        _OODLE.OodleLZ_Compress.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int64,
            ctypes.c_char_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_int64,
        ]
        _OODLE.OodleLZ_Compress.restype = ctypes.c_int64
    return _OODLE


def fnv1_32_lower(s: str) -> int:
    h = 5381
    for ch in s.lower().encode("utf-8"):
        h = ((h * 33) ^ ch) & 0xFFFFFFFF
    return h


def read_cas_raw(pkg: int, cas_num: int, offset: int, size: int) -> bytes:
    folder = "initialinstallpackage" if pkg == 0 else "defaultinstallpackage"
    cas_path = GAME_DIR / "Data" / "Win32" / "configurations" / "layout" / folder / f"cas_{cas_num:02d}.cas"
    with open(cas_path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def decompress_cas(pkg: int, cas_num: int, offset: int, comp_size: int) -> bytes:
    raw = read_cas_raw(pkg, cas_num, offset, comp_size)
    oodle = get_oodle()
    out = bytearray()
    cur = 0
    while cur < len(raw):
        unc_size, flag_type, c_size = struct.unpack_from(">IHH", raw, cur)
        comp_data = raw[cur + 8 : cur + 8 + c_size]
        if flag_type in (0x0070, 0x0000) or unc_size == c_size:
            out.extend(comp_data)
        else:
            out_buf = ctypes.create_string_buffer(unc_size)
            ret = oodle.OodleLZ_Decompress(
                comp_data, c_size, out_buf, unc_size, 1, 1, 0, None, 0, None, None, None, 0, 3
            )
            if ret <= 0:
                raise RuntimeError(f"OodleLZ_Decompress failed ret={ret}")
            out.extend(out_buf.raw[:ret])
        cur += 8 + c_size
    return bytes(out)


def cas_compress_uncompressed(raw: bytes) -> bytes:
    oodle = get_oodle()
    out = bytearray()
    pos = 0
    n = len(raw)
    out_buf = ctypes.create_string_buffer(0x10000 + 16384)
    while pos < n:
        chunk = raw[pos : min(pos + 0x10000, n)]
        clen = len(chunk)
        comp_len = oodle.OodleLZ_Compress(8, chunk, clen, out_buf, 4, None, None, None, None, 0)
        if 0 < comp_len < clen and comp_len <= 0xFFFF:
            out += struct.pack(">IHH", clen, 0x1170, comp_len) + out_buf.raw[:comp_len]
            pos += clen
        else:
            sub_len = min(clen, 0xFFF0)
            sub_chunk = raw[pos : pos + sub_len]
            out += struct.pack(">IHH", sub_len, 0x0070, sub_len) + sub_chunk
            pos += sub_len
    return bytes(out)


def parse_bundle_streams(toc_bytes: bytes, rec_pos: int):
    hdr = struct.unpack_from(">9I", toc_bytes, rec_pos)
    total_streams = hdr[3]
    pos = rec_pos + 36
    streams = []
    cur_pkg = 0
    cur_cas = 0
    while len(streams) < total_streams:
        sig = toc_bytes[pos + 2 : pos + 6]
        if sig == b"\xbc\x80\xe6\x99":
            cur_pkg = 0
            cur_cas = toc_bytes[pos + 7]
            pos += 8
        elif sig == b"\x78\x37\x15\x2c":
            cur_pkg = 1
            cur_cas = toc_bytes[pos + 7]
            pos += 8
        off, sz = struct.unpack_from(">II", toc_bytes, pos)
        pos += 8
        streams.append((cur_pkg, cur_cas, off, sz))
    return streams


def parse_sbdata(sb_raw: bytes):
    base = 4
    magic, total_cnt, ebx_cnt, res_cnt, chunk_cnt, str_off, meta_off, meta_sz = struct.unpack_from(
        "<8I", sb_raw, base
    )
    pos = base + 32 + total_cnt * 20
    ebx_list = []
    for _ in range(ebx_cnt):
        noff, orig_sz = struct.unpack_from("<II", sb_raw, pos)
        pos += 8
        s_start = base + str_off + noff
        s_end = sb_raw.find(b"\x00", s_start)
        name = sb_raw[s_start:s_end].decode("ascii", errors="replace")
        ebx_list.append({"name": name, "orig_size": orig_sz})

    res_list = []
    for _ in range(res_cnt):
        noff, orig_sz = struct.unpack_from("<II", sb_raw, pos)
        pos += 8
        s_start = base + str_off + noff
        s_end = sb_raw.find(b"\x00", s_start)
        name = sb_raw[s_start:s_end].decode("ascii", errors="replace")
        res_list.append({"name": name, "orig_size": orig_sz})

    for i in range(res_cnt):
        res_list[i]["res_type"] = struct.unpack_from("<I", sb_raw, pos)[0]
        pos += 4
    for i in range(res_cnt):
        res_list[i]["res_meta"] = sb_raw[pos : pos + 16]
        pos += 16
    for i in range(res_cnt):
        res_list[i]["res_rid"] = struct.unpack_from("<Q", sb_raw, pos)[0]
        pos += 8

    chunk_list = []
    for _ in range(chunk_cnt):
        cguid = sb_raw[pos : pos + 16]
        log_off, log_sz = struct.unpack_from("<II", sb_raw, pos + 16)
        pos += 24
        chunk_list.append({"guid_raw": cguid, "log_off": log_off, "log_sz": log_sz})

    return ebx_list, res_list, chunk_list


# ==============================================================================
# SURFACE MATERIAL BITFIELD & GRIND PRISM BUILDERS
# ==============================================================================
SURFACE_MATERIAL_INDEX_MASK = 0x1F
SURFACE_MATERIAL_PHYSICS_SHIFT = 19
SURFACE_MATERIAL_PHYSICS_MASK = 0x7FF
SURFACE_ANALYSIS_PROPERTY_SLOT = 71
NO_GRIND_PROPERTY_SLOT = 104

SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT = {
    1: 37224480,   # Concrete_Smooth + IncludeInSurfaceAnalysis (slot 71)
    13: 37225248,  # Brick_Rough + IncludeInSurfaceAnalysis (slot 71)
    27: 37226144,  # Marble + IncludeInSurfaceAnalysis (slot 71)
    33: 37226528,  # Metal_Thick + IncludeInSurfaceAnalysis (slot 71)
    46: 37227424,  # Metal_Rail + IncludeInSurfaceAnalysis (slot 71)
    57: 37228128,  # Wood_Thick_Smooth + IncludeInSurfaceAnalysis (slot 71)
    69: 37229024,  # Plastic + IncludeInSurfaceAnalysis (slot 71)
}


def apply_grind_mode_to_packed(packed: int, grind_mode: str) -> int:
    val = int(packed)
    slot = (val >> 6) & 0x7F
    if grind_mode == "round_rail":
        if slot in SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT:
            return SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT[slot]
        if slot == 0:
            return 37227424  # Metal_Rail + IncludeInSurfaceAnalysis (slot 71)
        return (SURFACE_ANALYSIS_PROPERTY_SLOT << 19) | (slot << 6) | (val & 0x3F) | 0x20
    elif grind_mode == "no_grind":
        eff_slot = slot if slot > 0 else 51
        return (NO_GRIND_PROPERTY_SLOT << 19) | (eff_slot << 6) | 0x20
    return val


def _chain_3d_segments_into_polylines(segments: list, weld_tol: float = 1e-4) -> list[list[np.ndarray]]:
    """Chains unordered `(p0, p1)` 3D segments into connected polylines so curved rails have no internal seams."""
    raw_segs = []
    for p0, p1 in (segments or []):
        a = np.asarray(p0, dtype=np.float64)
        b = np.asarray(p1, dtype=np.float64)
        if float(np.linalg.norm(b - a)) > weld_tol:
            raw_segs.append((a, b))
    if not raw_segs:
        return []

    used = [False] * len(raw_segs)
    chains = []
    for i in range(len(raw_segs)):
        if used[i]:
            continue
        used[i] = True
        chain = [raw_segs[i][0], raw_segs[i][1]]
        extended = True
        while extended:
            extended = False
            for j in range(len(raw_segs)):
                if used[j]:
                    continue
                a, b = raw_segs[j]
                if float(np.linalg.norm(chain[-1] - a)) <= weld_tol:
                    chain.append(b)
                    used[j] = True
                    extended = True
                elif float(np.linalg.norm(chain[-1] - b)) <= weld_tol:
                    chain.append(a)
                    used[j] = True
                    extended = True
                elif float(np.linalg.norm(chain[0] - b)) <= weld_tol:
                    chain.insert(0, a)
                    used[j] = True
                    extended = True
                elif float(np.linalg.norm(chain[0] - a)) <= weld_tol:
                    chain.insert(0, b)
                    used[j] = True
                    extended = True
        chains.append(chain)
    return chains


def _simplify_polyline_3d(pts: list[np.ndarray], tol: float = 0.005, min_seg_len: float = 0.05) -> list[np.ndarray]:
    """Simplifies a 3D polyline using Douglas-Peucker (`tol=5mm`) and enforces `min_seg_len=5cm` (matching sk8_map_export.py)."""
    if len(pts) <= 2:
        return pts

    def _dp(sub: list[np.ndarray]) -> list[np.ndarray]:
        if len(sub) <= 2:
            return sub
        p0, p1 = sub[0], sub[-1]
        chord = p1 - p0
        c_len = float(np.linalg.norm(chord))
        max_d = -1.0
        max_idx = 0
        for k in range(1, len(sub) - 1):
            if c_len < 1e-8:
                d = float(np.linalg.norm(sub[k] - p0))
            else:
                t = float(np.clip(np.dot(sub[k] - p0, chord) / (c_len * c_len), 0.0, 1.0))
                proj = p0 + t * chord
                d = float(np.linalg.norm(sub[k] - proj))
            if d > max_d:
                max_d = d
                max_idx = k
        if max_d > tol and max_idx > 0:
            left = _dp(sub[: max_idx + 1])
            right = _dp(sub[max_idx:])
            return left[:-1] + right
        return [p0, p1]

    simplified = _dp(pts)
    if len(simplified) <= 2 or min_seg_len <= 0.0:
        return simplified
    filtered = [simplified[0]]
    for pt in simplified[1:-1]:
        if float(np.linalg.norm(pt - filtered[-1])) >= min_seg_len:
            filtered.append(pt)
    filtered.append(simplified[-1])
    return filtered


def build_tube_prism_from_segments(segments: list, radius: float = 0.032, sides: int = 8) -> trimesh.Trimesh:
    """
    Builds a continuous, open-ended, mitered grind prism mesh from `(p0, p1)` 3D segments
    (matching sk8_map_export.py's `grind_prism` with zero internal perpendicular end-cap walls).
    """
    chains = _chain_3d_segments_into_polylines(segments)
    if not chains:
        return None
    prism_meshes = []
    half = max(0.012, float(radius))
    drop = max(0.024, float(radius) * 1.5)
    up_default = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    for raw_chain in chains:
        pts = _simplify_polyline_3d(raw_chain, tol=0.005, min_seg_len=0.05)
        if len(pts) < 2:
            continue
        n_pts = len(pts)
        tangents = []
        for i in range(n_pts):
            if i == 0:
                t = pts[1] - pts[0]
            elif i == n_pts - 1:
                t = pts[-1] - pts[-2]
            else:
                t0 = pts[i] - pts[i - 1]
                t1 = pts[i + 1] - pts[i]
                l0 = float(np.linalg.norm(t0))
                l1 = float(np.linalg.norm(t1))
                t = (t0 / max(l0, 1e-8)) + (t1 / max(l1, 1e-8))
            t_len = float(np.linalg.norm(t))
            tangents.append(t / max(t_len, 1e-8))

        rings = []
        prev_side = None
        for pt, tang in zip(pts, tangents):
            up = up_default
            if abs(float(np.dot(tang, up))) > 0.92:
                up = np.array([1.0, 0.0, 0.0], dtype=np.float64)
            side = np.cross(tang, up)
            s_len = float(np.linalg.norm(side))
            side = side / max(s_len, 1e-8)
            if prev_side is not None and float(np.dot(side, prev_side)) < 0.0:
                side = -side
            prev_side = side
            up_ortho = np.cross(side, tang)
            u_len = float(np.linalg.norm(up_ortho))
            up_ortho = up_ortho / max(u_len, 1e-8)
            # 3-vertex open-ended mitered cross-section (top-left, top-right, bottom-spine)
            # matching sk8_map_export.py's continuous grind_prism with NO internal end-caps!
            p_left = pt - side * half
            p_right = pt + side * half
            p_bottom = pt - up_ortho * drop
            rings.extend([p_left, p_right, p_bottom])

        p_verts = np.asarray(rings, dtype=np.float32)
        p_faces = []
        for s_idx in range(n_pts - 1):
            a = s_idx * 3
            b = (s_idx + 1) * 3
            p_faces.extend([
                [a + 0, a + 1, b + 1],
                [a + 0, b + 1, b + 0],
                [a + 1, a + 2, b + 2],
                [a + 1, b + 2, b + 1],
                [a + 2, a + 0, b + 0],
                [a + 2, b + 0, b + 2],
            ])
        if p_faces:
            prism_meshes.append(trimesh.Trimesh(vertices=p_verts, faces=np.asarray(p_faces, dtype=np.int32), process=False))

    if not prism_meshes:
        return None
    return trimesh.util.concatenate(prism_meshes)


def detect_sharp_grind_segments(verts: np.ndarray, faces: np.ndarray, min_crease_deg: float = 38.0) -> list:
    """Detects sharp top/coping crease edges and returns a list of `(p0, p1)` 3D segments."""
    if len(faces) == 0:
        return []
    m = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    if len(m.face_adjacency) == 0:
        return []
    angles = np.degrees(m.face_adjacency_angles)
    sharp_mask = angles >= min_crease_deg
    if not np.any(sharp_mask):
        return []

    adj_pairs = m.face_adjacency[sharp_mask]
    adj_edges = m.face_adjacency_edges[sharp_mask]
    fnorms = m.face_normals
    y_min = float(m.vertices[:, 1].min())
    y_span = max(0.1, float(m.vertices[:, 1].max() - y_min))

    valid_edges = []
    for (f0, f1), (v0, v1) in zip(adj_pairs, adj_edges):
        p0, p1 = m.vertices[v0], m.vertices[v1]
        if min(p0[1], p1[1]) < y_min + 0.12 * y_span:
            continue
        if max(fnorms[f0, 1], fnorms[f1, 1]) < 0.25:
            continue
        seg = p1 - p0
        length = float(np.linalg.norm(seg))
        if length < 0.18:
            continue
        if abs(seg[1]) / length > 0.72:
            continue
        valid_edges.append((p0.tolist(), p1.tolist()))
    return valid_edges[:32]


# ==============================================================================
# MULTI-MATERIAL PBR TEXTURE ATLAS PACKER & BC7_SRGB ENCODER
# ==============================================================================
def build_multi_material_atlas(parts: list, atlas_size: int = 1024):
    """
    Given a list of included mesh parts (each with `tex_img`, `avg_rgb`, `uvs`),
    packs their textures into a single `atlas_size x atlas_size` RGBA image and
    remaps each part's UVs (wrapping tiled UVs via `np.mod(uv, 1.0)`) into its
    assigned sub-tile so multi-material GLBs render with all materials intact
    inside a single-texture Frostbite donor shell!
    Returns `(atlas_img, remapped_uvs_per_part)`.
    """
    n = len(parts)
    if n == 0:
        img = Image.new("RGBA", (atlas_size, atlas_size), (185, 135, 88, 255))
        return img, []

    if n == 1:
        p0 = parts[0]
        im = p0.get("tex_img")
        if im is None:
            rgb = [int(max(0, min(255, round(c * 255)))) for c in p0.get("avg_rgb", (0.72, 0.52, 0.34))]
            im = Image.new("RGBA", (atlas_size, atlas_size), (rgb[0], rgb[1], rgb[2], 255))
        else:
            im = im.convert("RGBA").resize((atlas_size, atlas_size), Image.Resampling.LANCZOS)
        return im, [p0["uvs"].copy()]

    # Determine grid layout (e.g. 2 parts -> 1x2 vertical split or 2x2 grid)
    cols = 2 if n <= 4 else (3 if n <= 9 else 4)
    rows = int(math.ceil(n / cols))
    tile_w = atlas_size // cols
    tile_h = atlas_size // rows

    atlas = Image.new("RGBA", (atlas_size, atlas_size), (32, 36, 44, 255))
    remapped_uvs = []
    pad = 2  # 2px gutter to prevent mip bleeding

    for idx, p in enumerate(parts):
        col = idx % cols
        row = idx // cols
        x0 = col * tile_w
        y0 = row * tile_h

        im = p.get("tex_img")
        if im is None:
            rgb = [int(max(0, min(255, round(c * 255)))) for c in p.get("avg_rgb", (0.72, 0.52, 0.34))]
            tile_im = Image.new("RGBA", (tile_w, tile_h), (rgb[0], rgb[1], rgb[2], 255))
        else:
            tile_im = im.convert("RGBA").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        atlas.paste(tile_im, (x0, y0))

        # Remap UVs into [u_min..u_max, v_min..v_max]
        # Note: in glTF UV space before V-flip, V=0 is bottom and V=1 is top, while PIL y=0 is top!
        uv = np.asarray(p["uvs"], dtype=np.float32).copy()
        uv_wrapped = np.mod(uv, 1.0)
        u_min = (x0 + pad) / float(atlas_size)
        u_max = (x0 + tile_w - pad) / float(atlas_size)
        # PIL row 0 is at top -> Frostbite V=0 (which corresponds to glTF V=1 before `1.0 - v` flip)
        v_top_fb = (y0 + pad) / float(atlas_size)
        v_bot_fb = (y0 + tile_h - pad) / float(atlas_size)
        gltf_v_min = 1.0 - v_bot_fb
        gltf_v_max = 1.0 - v_top_fb

        uv_new = np.zeros_like(uv_wrapped)
        uv_new[:, 0] = u_min + uv_wrapped[:, 0] * (u_max - u_min)
        uv_new[:, 1] = gltf_v_min + uv_wrapped[:, 1] * (gltf_v_max - gltf_v_min)
        remapped_uvs.append(uv_new)

    return atlas, remapped_uvs


_TORCH_CUDA_AVAILABLE = None


def _check_torch_cuda() -> bool:
    global _TORCH_CUDA_AVAILABLE
    if _TORCH_CUDA_AVAILABLE is not None:
        return _TORCH_CUDA_AVAILABLE
    try:
        import torch  # type: ignore
        _TORCH_CUDA_AVAILABLE = bool(torch.cuda.is_available())
    except Exception:
        _TORCH_CUDA_AVAILABLE = False
    return _TORCH_CUDA_AVAILABLE


def _encode_rgba_to_bc7_mode6_single(rgba_arr: np.ndarray) -> bytes:
    h, w, _ = rgba_arr.shape
    N = (h // 4) * (w // 4)

    # Fast constant-color shortcut: if the entire mip level is a single uniform RGBA color
    # (common for constant roughness/metallic _msk or flat normal _n maps), encode 1 block and tile N times!
    first_px = rgba_arr[0, 0]
    if N > 1 and np.all(rgba_arr == first_px):
        single_blk = _encode_rgba_to_bc7_mode6_single(np.broadcast_to(first_px, (4, 4, 4)).copy())
        return single_blk * N

    # Optional GPU / CUDA acceleration via PyTorch when available for large mip levels
    if N >= 4096 and _check_torch_cuda():
        try:
            import torch  # type: ignore
            dev = torch.device("cuda")
            blocks_t = (
                torch.from_numpy(rgba_arr)
                .to(dev, dtype=torch.int32)
                .reshape(h // 4, 4, w // 4, 4, 4)
                .permute(0, 2, 1, 3, 4)
                .reshape(-1, 16, 4)
            )
            c_min_t = blocks_t.min(dim=1).values
            c_max_t = blocks_t.max(dim=1).values
            span_t = (c_max_t - c_min_t).to(torch.float32)
            span_len2_t = torch.sum(span_t * span_t, dim=1, keepdim=True)
            rel_t = blocks_t.to(torch.float32) - c_min_t[:, None, :].to(torch.float32)
            proj_t = torch.sum(rel_t * span_t[:, None, :], dim=2) / torch.clamp(span_len2_t, min=1e-6)
            idx_t = torch.clamp(torch.round(proj_t * 15.0), 0, 15).to(torch.int64)
            swap_t = idx_t[:, 0] >= 8
            idx_t = torch.where(swap_t[:, None], 15 - idx_t, idx_t)
            e0_t = torch.where(swap_t[:, None], c_max_t, c_min_t).to(torch.int64)
            e1_t = torch.where(swap_t[:, None], c_min_t, c_max_t).to(torch.int64)
            idx = idx_t.cpu().numpy().astype(np.uint64)
            e0 = e0_t.cpu().numpy().astype(np.uint64)
            e1 = e1_t.cpu().numpy().astype(np.uint64)
        except Exception:
            idx = None
    else:
        idx = None

    if idx is None:
        blocks_u8 = rgba_arr.reshape(h // 4, 4, w // 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 4)
        c_min = blocks_u8.min(axis=1).astype(np.int32)
        c_max = blocks_u8.max(axis=1).astype(np.int32)

        span = c_max - c_min
        span_len2 = np.maximum(np.sum(span * span, axis=1, keepdims=True), 1)
        rel = blocks_u8.astype(np.int32) - c_min[:, None, :]
        num = np.sum(rel * span[:, None, :], axis=2) * 15 + (span_len2 >> 1)
        idx = np.clip(num // span_len2, 0, 15).astype(np.uint64)

        swap = (idx[:, 0] >= 8)
        idx = np.where(swap[:, None], 15 - idx, idx)
        e0 = np.where(swap[:, None], c_max, c_min).astype(np.uint64)
        e1 = np.where(swap[:, None], c_min, c_max).astype(np.uint64)

    r0, g0, b0, a0 = (e0[:, 0] >> 1), (e0[:, 1] >> 1), (e0[:, 2] >> 1), (e0[:, 3] >> 1)
    r1, g1, b1, a1 = (e1[:, 0] >> 1), (e1[:, 1] >> 1), (e1[:, 2] >> 1), (e1[:, 3] >> 1)
    p0 = (e0[:, 0] & 1)
    p1 = (e1[:, 0] & 1)

    lo = np.full(N, 1 << 6, dtype=np.uint64)
    lo |= (r0 << 7) | (r1 << 14) | (g0 << 21) | (g1 << 28) | (b0 << 35) | (b1 << 42) | (a0 << 49) | (a1 << 56)
    lo |= (p0 << 63)

    hi = p1.copy()
    hi |= ((idx[:, 0] & 0x7) << 1)
    for k in range(1, 16):
        shift = 4 + (k - 1) * 4
        hi |= ((idx[:, k] & 0xF) << shift)

    out = np.empty((N, 2), dtype="<u8")
    out[:, 0] = lo
    out[:, 1] = hi
    return out.tobytes()


def _is_fast_pipeline_mode() -> bool:
    return os.environ.get("RESKATE_PIPELINE_MODE", "FAST_OPTIMIZED").strip().upper() != "LEGACY_FULL"


def _get_compiler_cache_root() -> Path:
    base = _get_staging_dir()
    if base.name == "Pack_Queue":
        base = base.parent
    return base / ".compiler_cache"


def encode_rgba_to_bc7_mipchain(img: Image.Image, width: int, height: int, mip_sizes: list) -> bytes:
    base_rgba = img.convert("RGBA")
    if base_rgba.size != (width, height):
        base_rgba = base_rgba.resize((width, height), Image.Resampling.BILINEAR)

    fast_mode = _is_fast_pipeline_mode()
    base_arr = np.asarray(base_rgba, dtype=np.uint8)

    # Fast Constant-Color BC7 Mipchain Shortcut: if all pixels share identical RGBA,
    # encode a single 4x4 BC7 Mode 6 block once and tile it across all mip levels (< 0.001s)!
    if fast_mode and base_arr.size >= 4:
        c_min = base_arr.min(axis=(0, 1))
        c_max = base_arr.max(axis=(0, 1))
        if np.array_equal(c_min, c_max):
            block4x4 = np.broadcast_to(c_min.reshape((1, 1, 4)), (4, 4, 4)).copy()
            single_blk16 = _encode_rgba_to_bc7_mode6_single(block4x4)
            if len(single_blk16) == 16:
                out_const = bytearray()
                for expected_sz in mip_sizes:
                    if expected_sz <= 0:
                        break
                    n_blks = expected_sz // 16
                    rem = expected_sz - (n_blks * 16)
                    out_const.extend(single_blk16 * n_blks)
                    if rem > 0:
                        out_const.extend(b"\x00" * rem)
                return bytes(out_const)

    bc7_cache_file = None
    if fast_mode:
        try:
            img_sha1 = hashlib.sha1(base_arr.tobytes()).hexdigest()
            mips_tag = f"{len(mip_sizes)}_{sum(int(x) for x in mip_sizes if x > 0)}"
            bc7_dir = _get_compiler_cache_root() / "bc7_v1"
            bc7_dir.mkdir(parents=True, exist_ok=True)
            bc7_cache_file = bc7_dir / f"{img_sha1}_{width}x{height}_{mips_tag}.bin"
            if bc7_cache_file.is_file():
                cached_b = bc7_cache_file.read_bytes()
                expected_total = sum(int(x) for x in mip_sizes if x > 0)
                if len(cached_b) == expected_total:
                    return cached_b
        except Exception:
            bc7_cache_file = None

    cur_rgba = base_rgba
    out = bytearray()
    for m, expected_sz in enumerate(mip_sizes):
        if expected_sz <= 0:
            break
        mw = max(1, width >> m)
        mh = max(1, height >> m)
        if m == 0:
            resized = cur_rgba
            arr = base_arr
        else:
            resized = cur_rgba.resize((mw, mh), Image.Resampling.BOX)
            cur_rgba = resized
            arr = np.asarray(resized, dtype=np.uint8)
        bw = max(4, (mw + 3) & ~3)
        bh = max(4, (mh + 3) & ~3)
        if bw != mw or bh != mh:
            pad_arr = np.pad(arr, ((0, bh - mh), (0, bw - mw), (0, 0)), mode="edge")
        else:
            pad_arr = arr
        mip_bytes = _encode_rgba_to_bc7_mode6_single(pad_arr)
        if len(mip_bytes) < expected_sz:
            mip_bytes += b"\x00" * (expected_sz - len(mip_bytes))
        elif len(mip_bytes) > expected_sz:
            mip_bytes = mip_bytes[:expected_sz]
        out.extend(mip_bytes)
    res_bytes = bytes(out)
    if bc7_cache_file is not None:
        try:
            bc7_cache_file.write_bytes(res_bytes)
        except Exception:
            pass
    return res_bytes


def generate_procedural_material_image(preset: str, size: int = 512) -> Image.Image:
    rng = np.random.default_rng(42)
    y, x = np.mgrid[0:size, 0:size]
    if preset == "wood_planks":
        base = np.array([172.0, 118.0, 68.0], dtype=np.float32)
        grain = np.sin(x * 0.18 + np.sin(y * 0.04) * 3.0) * 14.0
        plank_seam = ((y % 64) < 2).astype(np.float32) * (-45.0)
        noise = rng.normal(0, 5.0, (size, size))
        rgb = np.clip(base[None, None, :] + (grain + plank_seam + noise)[:, :, None], 20, 245).astype(np.uint8)
    elif preset == "concrete":
        base = np.array([158.0, 160.0, 156.0], dtype=np.float32)
        noise = rng.normal(0, 11.0, (size, size))
        speck = (np.sin(x * 0.07) * np.cos(y * 0.07)) * 8.0
        rgb = np.clip(base[None, None, :] + (noise + speck)[:, :, None], 40, 235).astype(np.uint8)
    elif preset == "metal_steel":
        base = np.array([138.0, 146.0, 155.0], dtype=np.float32)
        brush = rng.normal(0, 7.0, (size, 1))
        rgb = np.clip(base[None, None, :] + brush[:, :, None], 50, 240).astype(np.uint8)
    else:
        base = np.array([196.0, 148.0, 92.0], dtype=np.float32)
        grain = np.sin(y * 0.09 + np.cos(x * 0.03) * 4.0) * 10.0
        rgb = np.clip(base[None, None, :] + grain[:, :, None], 30, 245).astype(np.uint8)
    rgba = np.dstack([rgb, np.full((size, size), 255, dtype=np.uint8)])
    return Image.fromarray(rgba, mode="RGBA")


def pil_to_data_url(im: Image.Image, max_sz: int = 512) -> str:
    rgba = im.convert("RGBA")
    if rgba.width > max_sz or rgba.height > max_sz:
        rgba = rgba.resize((min(max_sz, rgba.width), min(max_sz, rgba.height)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ==============================================================================
# FROSTBITE MESH STREAM & MULTI-SHAPE HAVOK PHYSICS COMPILERS
# ==============================================================================
def encode_fb_tangentspace_vec(
    normals: np.ndarray,
    flip_normals: bool = False,
    verts: np.ndarray | None = None,
    uvs: np.ndarray | None = None,
    faces: np.ndarray | None = None,
    hand_val: int = 1,
) -> bytes:
    """
    Encodes vertex normals and tangents into Frostbite's 32-bit `9+9+10+4` stereographic
    axis-dominant tangent-space frame (`u == 52, fmt == 13`, `DXGI_FORMAT_R8G8B8A8_UNORM`).
    Bitfield layout across `[b0, b1, b2, b3]`:
      - `b3 & 0x07`        : Cube face `0..5` (`+X, -X, +Y, -Y, +Z, -Z`)
      - `(b3 >> 3) & 0x01` : Handedness bit (`1`)
      - `p0` (9-bit `0..511`, center `256`): `b0 | (((b3 >> 4) & 1) << 8)`
      - `p1` (9-bit `0..511`, center `256`): `b1 | (((b3 >> 5) & 1) << 8)`
      - `p2` (10-bit `0..1023` tangent spin angle in Householder basis): `b2 | (((b3 >> 6) & 3) << 8)`
    Eliminates the 45-degree dominant-axis normal/reflection seam on curved transition ramps.
    """
    n = np.asarray(normals, dtype=np.float32).copy()
    if len(n) == 0:
        return b""
    if flip_normals:
        n = -n
    norms = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(norms > 1e-6, n / np.maximum(norms, 1e-6), np.array([[0.0, 1.0, 0.0]], dtype=np.float32))

    dom_axis = np.argmax(np.abs(n), axis=1)
    idx_arange = np.arange(len(n))
    dom_val = n[idx_arange, dom_axis]
    sign_bit = (dom_val < 0.0).astype(np.int32)
    face = dom_axis * 2 + sign_bit  # 0:+X, 1:-X, 2:+Y, 3:-Y, 4:+Z, 5:-Z

    c0 = np.where(dom_axis == 0, n[:, 1], n[:, 0])
    c1 = np.where(dom_axis == 2, n[:, 1], n[:, 2])

    denom = np.maximum(1.0 + np.abs(dom_val), 1e-6)
    stereo_scale = 255.0 / (np.sqrt(2.0) - 1.0)  # ~615.624458
    p0 = np.clip(np.round(256.0 + (c0 / denom) * stereo_scale), 0, 511).astype(np.int32)
    p1 = np.clip(np.round(256.0 + (c1 / denom) * stereo_scale), 0, 511).astype(np.int32)

    # Compute smooth vertex tangent vectors `t` from mesh UV gradients when available
    t = np.zeros_like(n)
    if (
        verts is not None
        and uvs is not None
        and faces is not None
        and len(verts) == len(n)
        and len(uvs) == len(n)
        and len(faces) > 0
    ):
        try:
            f_arr = np.asarray(faces, dtype=np.int32)
            v_arr = np.asarray(verts, dtype=np.float32)
            uv_arr = np.asarray(uvs, dtype=np.float32)
            i0, i1, i2 = f_arr[:, 0], f_arr[:, 1], f_arr[:, 2]
            dp1 = v_arr[i1] - v_arr[i0]
            dp2 = v_arr[i2] - v_arr[i0]
            duv1 = uv_arr[i1] - uv_arr[i0]
            duv2 = uv_arr[i2] - uv_arr[i0]
            det = duv1[:, 0] * duv2[:, 1] - duv1[:, 1] * duv2[:, 0]
            valid = np.abs(det) > 1e-8
            inv_det = np.where(valid, 1.0 / np.where(valid, det, 1.0), 0.0)[:, None]
            t_tri = (duv2[:, 1:2] * dp1 - duv1[:, 1:2] * dp2) * inv_det
            t_tri_norm = np.linalg.norm(t_tri, axis=1, keepdims=True)
            t_tri = np.where(t_tri_norm > 1e-6, t_tri / np.maximum(t_tri_norm, 1e-6), 0.0)
            np.add.at(t, i0, t_tri)
            np.add.at(t, i1, t_tri)
            np.add.at(t, i2, t_tri)
        except Exception:
            pass

    t_len = np.linalg.norm(t, axis=1, keepdims=True)
    fallback_t = np.where(
        np.abs(n[:, 0:1]) < 0.9,
        np.array([[1.0, 0.0, 0.0]], dtype=np.float32),
        np.array([[0.0, 0.0, 1.0]], dtype=np.float32),
    )
    t = np.where(t_len > 1e-5, t, fallback_t)
    # Gram-Schmidt orthogonalize tangent `t` against normal `n`
    t = t - np.sum(t * n, axis=1, keepdims=True) * n
    t_len2 = np.linalg.norm(t, axis=1, keepdims=True)
    t = np.where(t_len2 > 1e-6, t / np.maximum(t_len2, 1e-6), fallback_t)

    # Householder reflection of canonical face basis `(a0, a1)` from `e_face` to `n`
    e_lut = np.array(
        [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]],
        dtype=np.float32,
    )
    a0_lut = np.array(
        [[0, 0, 1], [0, 0, -1], [1, 0, 0], [-1, 0, 0], [-1, 0, 0], [1, 0, 0]],
        dtype=np.float32,
    )
    a1_lut = np.array(
        [[0, -1, 0], [0, -1, 0], [0, 0, -1], [0, 0, -1], [0, -1, 0], [0, -1, 0]],
        dtype=np.float32,
    )
    e_v = e_lut[face]
    a0_v = a0_lut[face]
    a1_v = a1_lut[face]
    v_hh = e_v - n
    v_norm_sq = np.sum(v_hh * v_hh, axis=1, keepdims=True)
    safe_v_sq = np.maximum(v_norm_sq, 1e-8)
    u0 = np.where(v_norm_sq > 1e-8, a0_v - 2.0 * (np.sum(a0_v * v_hh, axis=1, keepdims=True) / safe_v_sq) * v_hh, a0_v)
    u1 = np.where(v_norm_sq > 1e-8, a1_v - 2.0 * (np.sum(a1_v * v_hh, axis=1, keepdims=True) / safe_v_sq) * v_hh, a1_v)

    cos_a = np.sum(t * u0, axis=1)
    sin_a = np.sum(t * u1, axis=1)
    ang = np.mod(np.arctan2(sin_a, cos_a), 2.0 * np.pi)
    p2 = np.round((ang / (2.0 * np.pi)) * 1024.0).astype(np.int32) & 1023

    out = np.zeros((len(n), 4), dtype=np.uint8)
    out[:, 0] = (p0 & 0xFF).astype(np.uint8)
    out[:, 1] = (p1 & 0xFF).astype(np.uint8)
    out[:, 2] = (p2 & 0xFF).astype(np.uint8)
    out[:, 3] = (
        (face & 0x07)
        | ((int(hand_val) & 1) << 3)
        | (((p0 >> 8) & 1) << 4)
        | (((p1 >> 8) & 1) << 5)
        | (((p2 >> 8) & 3) << 6)
    ).astype(np.uint8)
    return out.tobytes()


def apply_collision_shape_edits(
    verts: np.ndarray,
    trim_bottom: float = 0.0,
    col_scale_x: float = 1.0,
    col_scale_y: float = 1.0,
    col_scale_z: float = 1.0,
    col_offset_y: float = 0.0,
) -> np.ndarray:
    """
    Applies interactive collision hull shaping:
    - `trim_bottom` (`0.00..0.40m`): flattens/snaps all vertices within `trim_bottom` meters of
      the bottom `Y_min` down to `Y_min` so ramps with rough bottom lips sit 100% flush on the ground.
    - `col_scale_x/y/z`: scales the collision hull around `(center_x, Y_min, center_z)`.
    - `col_offset_y`: shifts the collision hull vertically relative to the visual mesh.
    """
    v = np.asarray(verts, dtype=np.float32).copy()
    if len(v) == 0:
        return v
    vmin = v.min(axis=0)
    vmax = v.max(axis=0)
    y_floor = float(vmin[1])
    y_height = float(vmax[1] - vmin[1])

    tb = float(trim_bottom or 0.0)
    if tb > 1e-4 and y_height > 0.05:
        clamp_h = min(tb, y_height * 0.45)
        low_mask = v[:, 1] < (y_floor + clamp_h)
        v[low_mask, 1] = y_floor

    sx = float(col_scale_x or 1.0)
    sy = float(col_scale_y or 1.0)
    sz = float(col_scale_z or 1.0)
    if abs(sx - 1.0) > 1e-4 or abs(sy - 1.0) > 1e-4 or abs(sz - 1.0) > 1e-4:
        cx = 0.5 * float(vmin[0] + vmax[0])
        cz = 0.5 * float(vmin[2] + vmax[2])
        v[:, 0] = cx + (v[:, 0] - cx) * sx
        v[:, 1] = y_floor + (v[:, 1] - y_floor) * sy
        v[:, 2] = cz + (v[:, 2] - cz) * sz

    dy = float(col_offset_y or 0.0)
    if abs(dy) > 1e-4:
        v[:, 1] += dy

    return v


def simplify_collision_mesh_for_havok(tm: trimesh.Trimesh, max_faces: int = 48, force_convex: bool = False) -> trimesh.Trimesh:
    """
    Ensures every collision mesh passed to Havok (`hknpConvexPolytopeShape` / compound shape)
    stays strictly within Havok's safe `uint8` polytope face/vertex budget (`<= max_faces` faces, `<= 64` verts),
    preventing high-poly collision hangs (`ZwWaitForSingleObject` worker deadlock).
    """
    max_f = max(12, min(110, int(max_faces or 48)))
    try:
        work = tm.convex_hull if force_convex else tm
    except Exception:
        work = tm
    if len(work.faces) <= max_f and len(work.vertices) <= 64:
        return work

    # Try convex hull first if it already satisfies or is close to budget
    try:
        ch = work.convex_hull
        if force_convex and len(ch.faces) <= max_f and len(ch.vertices) <= 64:
            return ch
    except Exception:
        ch = work

    # Quantize / cluster vertices onto a spatial grid and re-hull / decimate until within safe Havok budget
    v = np.asarray(work.vertices, dtype=np.float32)
    bmin = v.min(axis=0)
    bmax = v.max(axis=0)
    span = np.maximum(bmax - bmin, 1e-3)

    # Farthest-point / extremal-direction sampling on surface to preserve exact corners & ramp lips
    max_verts = max(10, min(56, (max_f // 2) + 4))
    if len(v) > max_verts:
        # Generate 42 quasi-uniform directions on the unit sphere + 8 AABB corners
        golden = (1.0 + math.sqrt(5.0)) / 2.0
        dirs = []
        for i in range(max_verts):
            theta = 2.0 * math.pi * i / golden
            phi = math.acos(1.0 - 2.0 * (i + 0.5) / max_verts)
            dirs.append([math.cos(theta) * math.sin(phi), math.cos(phi), math.sin(theta) * math.sin(phi)])
        dir_mat = np.array(dirs, dtype=np.float32)  # (K, 3)
        norm_v = (v - bmin) / span
        dots = norm_v @ dir_mat.T  # (N, K)
        chosen_idx = np.unique(np.argmax(dots, axis=0))
        sampled_v = v[chosen_idx]
        try:
            pts_cloud = trimesh.points.PointCloud(sampled_v)
            dec_hull = pts_cloud.convex_hull
            if len(dec_hull.faces) >= 4:
                return dec_hull
        except Exception:
            pass

    if len(ch.faces) > max_f or len(ch.vertices) > 64:
        # Pure-numpy AABB box fallback if scipy is unavailable
        bx = trimesh.creation.box(extents=np.maximum(bmax - bmin, 0.015))
        bx.apply_translation(0.5 * (bmin + bmax))
        return bx
    return ch


def compute_mesh_volume_moments(verts: np.ndarray, faces: np.ndarray):
    bmin = verts.min(axis=0).astype(np.float32)
    bmax = verts.max(axis=0).astype(np.float32)
    span = np.maximum(bmax - bmin, 0.05)

    try:
        m = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
        phys_m = m if (m.is_watertight and m.volume > 1e-4) else m.convex_hull
        vol = float(max(abs(phys_m.volume), float(span[0] * span[1] * span[2] * 0.15), 1e-4))
        com = np.asarray(phys_m.center_mass, dtype=np.float32)
        if not np.all(np.isfinite(com)):
            com = 0.5 * (bmin + bmax)
        inertia = np.asarray(phys_m.moment_inertia, dtype=np.float32)
        if not np.all(np.isfinite(inertia)) or np.max(np.abs(inertia)) < 1e-7:
            raise ValueError("Fallback to box covariance")
        # Convert central rotational inertia I_ij = tr(Sigma)*delta_ij - Sigma_ij
        # into central second-moment covariance Sigma_central = 0.5*tr(I)*I_3 - I
        tr_i = float(inertia[0, 0] + inertia[1, 1] + inertia[2, 2])
        cov_c = 0.5 * tr_i * np.eye(3, dtype=np.float64) - inertia.astype(np.float64)
        for d_i in range(3):
            if cov_c[d_i, d_i] <= 0.0:
                cov_c[d_i, d_i] = float(vol) * (float(span[d_i]) ** 2) / 12.0
        # Shift from center of mass to prop origin via parallel-axis theorem: Sigma_ij = Sigma_central_ij + V * c_i * c_j
        cov_origin = cov_c + float(vol) * np.outer(com.astype(np.float64), com.astype(np.float64))
        try:
            obb_ext = np.asarray(phys_m.bounding_primitive.extents, dtype=np.float32)
        except Exception:
            obb_ext = span
    except Exception:
        vol = float(max(span[0] * span[1] * span[2] * 0.5, 1e-3))
        com = 0.5 * (bmin + bmax)
        sxx = vol * (float(span[0] ** 2) / 12.0 + float(com[0] ** 2))
        syy = vol * (float(span[1] ** 2) / 12.0 + float(com[1] ** 2))
        szz = vol * (float(span[2] ** 2) / 12.0 + float(com[2] ** 2))
        sxy = vol * float(com[0] * com[1])
        sxz = vol * float(com[0] * com[2])
        syz = vol * float(com[1] * com[2])
        cov_origin = np.array(
            [[sxx, sxy, sxz], [sxy, syy, syz], [sxz, syz, szz]],
            dtype=np.float64,
        )
        obb_ext = span

    cx_v = float(com[0]) * float(vol)
    cy_v = float(com[1]) * float(vol)
    cz_v = float(com[2]) * float(vol)
    vm16 = (
        float(cov_origin[0, 0]), float(cov_origin[0, 1]), float(cov_origin[0, 2]), cx_v,
        float(cov_origin[1, 0]), float(cov_origin[1, 1]), float(cov_origin[1, 2]), cy_v,
        float(cov_origin[2, 0]), float(cov_origin[2, 1]), float(cov_origin[2, 2]), cz_v,
        cx_v, cy_v, cz_v, float(vol),
    )
    return vm16, bmin, bmax, vol, com, obb_ext


def fit_havok_polytope_to_bounds(phys_res_bytes: bytes, target_min: np.ndarray, target_max: np.ndarray, material_packed: int = None):
    pb = bytearray(phys_res_bytes)
    magic0, magic1, has_tf, sc = struct.unpack_from("<4I", pb, 0)
    if magic0 != 2 or magic1 != 0x69AF7015 or sc == 0:
        return bytes(pb), [], []

    rel_offs = struct.unpack_from("<5I", pb, 0x24)
    abs_offs = [0x24 + j * 4 + rel_offs[j] for j in range(5)]
    sp0 = abs_offs[1]
    arr2_0 = abs_offs[2]

    orig_body_min = np.array([1e9, 1e9, 1e9], dtype=np.float32)
    orig_body_max = np.array([-1e9, -1e9, -1e9], dtype=np.float32)
    for s in range(sc):
        sp = sp0 + s * 0x54
        a2 = arr2_0 + s * 0x48
        nv, ne, nf, mv = struct.unpack_from("<4H", pb, sp + 0x18)
        rel = struct.unpack_from("<10i", pb, sp + 0x24)
        tx, ty, tz = struct.unpack_from("<3f", pb, a2 + 0x10)
        t_vec = np.array([tx, ty, tz], dtype=np.float32)
        if nv > 0 and rel[0] != 0:
            t_abs0 = sp + 0x24 + rel[0]
            vl = np.frombuffer(pb, dtype="<f4", count=nv * 3, offset=t_abs0).reshape(nv, 3)
            vb = vl + t_vec
            orig_body_min = np.minimum(orig_body_min, vb.min(axis=0))
            orig_body_max = np.maximum(orig_body_max, vb.max(axis=0))
        else:
            s_min = np.array(struct.unpack_from("<3f", pb, sp), dtype=np.float32)
            s_max = np.array(struct.unpack_from("<3f", pb, sp + 0x0C), dtype=np.float32)
            orig_body_min = np.minimum(orig_body_min, s_min + t_vec)
            orig_body_max = np.maximum(orig_body_max, s_max + t_vec)

    orig_span = np.maximum(orig_body_max - orig_body_min, 1e-4)
    t_min = np.asarray(target_min, dtype=np.float32)
    t_max = np.asarray(target_max, dtype=np.float32)
    t_span = np.maximum(t_max - t_min, 0.05)

    preview_verts_list = []
    preview_edges = []
    v_base_idx = 0

    for s in range(sc):
        sp = sp0 + s * 0x54
        a2 = arr2_0 + s * 0x48
        if material_packed is not None:
            struct.pack_into("<I", pb, a2 + 0x40, int(material_packed))

        nv, ne, nf, mv = struct.unpack_from("<4H", pb, sp + 0x18)
        rel = struct.unpack_from("<10i", pb, sp + 0x24)
        if nv == 0 or rel[0] == 0:
            continue
        t_abs = [sp + 0x24 + j * 4 + rel[j] for j in range(8)]
        tx, ty, tz = struct.unpack_from("<3f", pb, a2 + 0x10)
        t_vec = np.array([tx, ty, tz], dtype=np.float32)

        verts_local = np.frombuffer(pb, dtype="<f4", count=nv * 3, offset=t_abs[0]).reshape(nv, 3).copy()
        verts_body = verts_local + t_vec
        norm_coords = (verts_body - orig_body_min) / orig_span
        new_body_verts = t_min + norm_coords * t_span

        new_s_min = new_body_verts.min(axis=0)
        new_s_max = new_body_verts.max(axis=0)
        new_center = 0.5 * (new_s_min + new_s_max)
        new_local_verts = (new_body_verts - new_center).astype("<f4")
        local_min = new_local_verts.min(axis=0)
        local_max = new_local_verts.max(axis=0)

        if mv == 0:
            pb[sp : sp + 0x18] = b"\x00" * 24
        else:
            struct.pack_into(
                "<6f", pb, sp,
                float(local_min[0]), float(local_min[1]), float(local_min[2]),
                float(local_max[0]), float(local_max[1]), float(local_max[2]),
            )
        pb[t_abs[0] : t_abs[0] + nv * 12] = new_local_verts.tobytes()
        struct.pack_into("<4f", pb, a2 + 0x10, float(new_center[0]), float(new_center[1]), float(new_center[2]), 1.0)

        preview_verts_list.extend(new_body_verts.tolist())
        if nf > 0 and ne > 0 and rel[3] > 0 and rel[7] > 0:
            f_idx = np.frombuffer(pb, dtype="<u2", count=ne, offset=t_abs[3])
            f_starts = np.frombuffer(pb, dtype="<u2", count=nf + 1, offset=t_abs[7])
            edge_set = set()
            for f in range(nf):
                st, en = int(f_starts[f]), int(f_starts[f + 1])
                ring = f_idx[st:en]
                for k in range(len(ring)):
                    a = v_base_idx + int(ring[k])
                    b = v_base_idx + int(ring[(k + 1) % len(ring)])
                    if a != b:
                        edge_set.add((min(a, b), max(a, b)))
            preview_edges.extend([list(e) for e in sorted(edge_set)])
        v_base_idx += nv

    return bytes(pb), preview_verts_list, preview_edges


def compile_multishape_havok_via_cli(col_shapes: list, max_faces_per_hull: int = 48, force_exact_triangle_mesh: bool = False):
    """
    Compiles 1..12 collision shapes (`col_shapes`: list of dicts with
    `{"mesh": trimesh.Trimesh, "mode": "hull"|"triangle_mesh", "surface_id": str, "packed": int, "round_rail": bool}`)
    into a single native Havok `0x41759364` BuildKit `.ecsphysics` resource (`flag=0`) via
    `reskate_cli.exe compile-map --no-gi --no-lods`.
    Automatically caps each shape's vertex/face count to prevent Havok `uint8` polytope table overflow!
    When `force_exact_triangle_mesh=True` (Option 2A), passes the un-simplified triangle mesh directly with `collision_mode="triangle_mesh"`.
    Returns `(pb_bk_bytes, shape_cnt)`.
    """
    shapes = col_shapes[:12]
    if not shapes:
        raise ValueError("No collision shapes provided")

    fast_mode = _is_fast_pipeline_mode()
    havok_cache_file = None
    active_cli = Path(os.environ.get("RESKATE_CLI_EXE") or CLI_EXE)
    active_game_dir = Path(os.environ.get("RESKATE_GAME_DIR") or GAME_DIR)
    comp_ver = os.environ.get("RESKATE_COMPILER_VERSION", "0.32.1")
    if fast_mode:
        try:
            import pickle
            h_key = hashlib.sha1()
            cli_st = active_cli.stat() if active_cli.exists() else None
            cli_sig = f"{cli_st.st_size}:{cli_st.st_mtime_ns}" if cli_st else "nocli"
            h_key.update(
                f"havok_cli_v2|ver:{comp_ver}|game:{str(active_game_dir)}|cli:{cli_sig}|maxf:{int(max_faces_per_hull)}|exact:{bool(force_exact_triangle_mesh)}|n:{len(shapes)}|".encode("utf-8")
            )
            for s in shapes:
                stm = s["mesh"]
                sv = np.round(np.asarray(stm.vertices, dtype=np.float32), 5)
                sf = np.asarray(stm.faces, dtype=np.int32)
                h_key.update(sv.tobytes())
                h_key.update(sf.tobytes())
                h_key.update(
                    f"|pk:{int(s.get('packed', 3680))}|rr:{bool(s.get('round_rail'))}|gc:{bool(s.get('grind_curve'))}|".encode("utf-8")
                )
            havok_dir = _get_compiler_cache_root() / "havok_hulls_v1"
            havok_dir.mkdir(parents=True, exist_ok=True)
            havok_cache_file = havok_dir / f"cli_{h_key.hexdigest()}.pkl"
            if havok_cache_file.is_file():
                cached_pb, cached_sc = pickle.loads(havok_cache_file.read_bytes())
                if isinstance(cached_pb, bytes) and len(cached_pb) >= 200 and int(cached_sc) >= 1:
                    return cached_pb, int(cached_sc)
        except Exception:
            havok_cache_file = None

    import threading
    tmp_root = _get_staging_dir() / f"havok_cli_tmp_{os.getpid()}_{threading.get_ident()}"
    tmp_in = tmp_root / "in_map"
    tmp_out = tmp_root / "out_map"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    (tmp_in / "meshes").mkdir(parents=True, exist_ok=True)
    tmp_out.mkdir(parents=True, exist_ok=True)

    try:
        safe_meshes = []
        for s in shapes:
            raw_tm = s["mesh"]
            if force_exact_triangle_mesh or s.get("round_rail") or s.get("grind_curve"):
                safe_tm = raw_tm
            elif len(raw_tm.faces) > max_faces_per_hull or len(raw_tm.vertices) > 64:
                safe_tm = simplify_collision_mesh_for_havok(raw_tm, max_faces=max_faces_per_hull, force_convex=True)
            else:
                safe_tm = raw_tm
            safe_meshes.append(safe_tm)

        glb_path = tmp_in / "meshes" / "col_prop.glb"
        sc = trimesh.Scene(safe_meshes)
        sc.export(str(glb_path))

        # Merge all mesh primitives into meshes[0].primitives so reskate_cli builds a single compound physics resource
        gltf = GLTF2().load(str(glb_path))
        all_prims = []
        for m in gltf.meshes:
            for p in m.primitives:
                p.material = len(all_prims)
                all_prims.append(p)
        gltf.meshes = [gltf.meshes[0]]
        gltf.meshes[0].primitives = all_prims
        gltf.materials = [GLTFMaterial(name=f"mat_{i}") for i in range(len(all_prims))]
        gltf.nodes = [gltf.nodes[0]]
        gltf.nodes[0].mesh = 0
        gltf.nodes[0].children = []
        gltf.nodes[0].matrix = None
        gltf.scenes[0].nodes = [0]
        gltf.save(str(glb_path))

        materials_dict = {}
        objects_list = []
        for idx, s in enumerate(shapes[: len(all_prims)]):
            m_key = f"m{idx}"
            materials_dict[m_key] = {"surface": "wood"}
            # Always pass "triangle_mesh" to reskate_cli so each decomposed primitive
            # (including pre-computed convex hulls from decompose_mesh_into_hulls) is
            # compiled as its own independent shape instead of being merged into a
            # single outer convex hull across all primitives (which bridged leg gaps!).
            cli_mode = "triangle_mesh"
            packed_val = int(s.get("packed", 3680))
            # IMPORTANT: Never set grind_curve=True or render=False when compiling multi-shape
            # prop collision (e.g. barrier body + grind lip prisms), because reskate_cli compile-map
            # discards all non-grind_curve primitives in col_prop.glb if any primitive has grind_curve=True!
            use_grind_curve = bool(s.get("grind_curve")) and len(all_prims) == 1 and bool(s.get("round_rail"))
            obj_rec = {
                "name": f"col_prop / section {idx + 1}",
                "placement_mode": "authored_mesh",
                "mesh": "meshes/col_prop.glb",
                "primitive": idx,
                "material": m_key,
                "render": True,
                "transform": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
                "static": True,
                "collision_mode": cli_mode,
                "collision_material": f"material_{packed_val:04d}",
                "collision_material_packed": packed_val,
            }
            if s.get("round_rail"):
                obj_rec["round_rail"] = True
            if use_grind_curve:
                obj_rec["grind_curve"] = True
            objects_list.append(obj_rec)

        doc = {
            "format": 1,
            "units": "meters",
            "up": "y",
            "forward": "-z",
            "materials": materials_dict,
            "surface_profiles": {},
            "objects": objects_list,
            "lights": [],
            "audio_volumes": [],
            "audio_emitters": [],
            "interaction_prefabs": [],
            "trigger_effects": [],
            "vfx_prefabs": [],
            "npc_routes": [],
            "spawn": {"position": [0, 1, 0], "forward": [0, 0, -1], "up": [0, 1, 0]},
        }
        (tmp_in / "map.json").write_text(json.dumps(doc), encoding="utf-8")

        res = subprocess.run(
            [str(active_cli), "compile-map", str(active_game_dir), str(tmp_in), str(tmp_out), "--no-gi", "--no-lods"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"compile-map physics failed: {res.stderr}")

        toc_p = next(tmp_out.rglob("*in_map.toc"))
        tb = toc_p.read_bytes()

        def _read_stream(is_patch, pkg, cas, off, sz):
            if is_patch:
                folder = "initialinstallpackage" if pkg == 0 else "defaultinstallpackage"
                p = tmp_out / "Patch" / "Win32" / "configurations" / "layout" / folder / f"cas_{cas:02d}.cas"
                if p.exists():
                    with open(p, "rb") as f:
                        f.seek(off)
                        return f.read(sz)
            return read_cas_raw(pkg, cas, off, sz)

        def _decomp_stream(is_patch, pkg, cas, off, sz):
            raw = _read_stream(is_patch, pkg, cas, off, sz)
            oodle = get_oodle()
            out = bytearray()
            cur = 0
            while cur < len(raw):
                unc_size, flag_type, c_size = struct.unpack_from(">IHH", raw, cur)
                comp_data = raw[cur + 8 : cur + 8 + c_size]
                if flag_type in (0x0070, 0x0000) or unc_size == c_size:
                    out.extend(comp_data)
                else:
                    out_buf = ctypes.create_string_buffer(unc_size)
                    ret = oodle.OodleLZ_Decompress(
                        comp_data, c_size, out_buf, unc_size, 1, 1, 0, None, 0, None, None, None, 0, 3
                    )
                    out.extend(out_buf.raw[:ret])
                cur += 8 + c_size
            return bytes(out)

        def _parse_map_bundle_streams(toc_b: bytes, rec_pos: int):
            hdr = struct.unpack_from(">9I", toc_b, rec_pos)
            total_s = hdr[3]
            pos = rec_pos + 36
            s_list = []
            cur_p = 0
            cur_pkg = 0
            cur_cas = 0
            while len(s_list) < total_s:
                sig = toc_b[pos + 2 : pos + 6]
                if sig == b"\xbc\x80\xe6\x99":
                    cur_p = 1 if toc_b[pos + 1] else 0
                    cur_pkg = 0
                    cur_cas = toc_b[pos + 7]
                    pos += 8
                elif sig == b"\x78\x37\x15\x2c":
                    cur_p = 1 if toc_b[pos + 1] else 0
                    cur_pkg = 1
                    cur_cas = toc_b[pos + 7]
                    pos += 8
                off, sz = struct.unpack_from(">II", toc_b, pos)
                pos += 8
                s_list.append((cur_p, cur_pkg, cur_cas, off, sz))
            return s_list

        base = 0x22C
        b_off = struct.unpack_from(">I", tb, base + 0x04)[0]
        b_cnt = struct.unpack_from(">I", tb, base + 0x08)[0]
        for i in range(b_cnt):
            u3 = struct.unpack_from(">I", tb, base + b_off + i * 16 + 12)[0]
            streams = _parse_map_bundle_streams(tb, base + u3)
            ebxs, ress, chks = parse_sbdata(_read_stream(*streams[0]))
            for r_i, r in enumerate(ress):
                if r["res_type"] == 0x41759364 and "col_prop" in r["name"]:
                    pb_cli = _decomp_stream(*streams[1 + len(ebxs) + r_i])
                    h = struct.unpack_from("<14I", pb_cli, 0)
                    flag = h[2]
                    shape_cnt = h[3]
                    if flag == 1:
                        pb_bk = bytearray(pb_cli[:0x38] + pb_cli[0x84:])
                        struct.pack_into("<I", pb_bk, 0x08, 0)
                        for off in (0x28, 0x2C, 0x30, 0x34):
                            val = struct.unpack_from("<I", pb_bk, off)[0]
                            struct.pack_into("<I", pb_bk, off, val - 0x4C)
                    else:
                        pb_bk = bytearray(pb_cli)
                    out_pb_bytes = bytes(pb_bk)
                    if havok_cache_file is not None:
                        try:
                            import pickle
                            havok_cache_file.write_bytes(
                                pickle.dumps((out_pb_bytes, int(shape_cnt)), protocol=pickle.HIGHEST_PROTOCOL)
                            )
                        except Exception:
                            pass
                    return out_pb_bytes, int(shape_cnt)

        raise RuntimeError("col_prop.physics not found in compile-map output")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def convert_havok_to_pphys_mv0(pb_in: bytes, a2_mode: int = 3, strip_mv0: bool = False, recenter_vertices: bool = True) -> bytes:
    """
    Patches a valid single-shape (`sc == 1`) or multi-shape `0x41759364` Havok physics resource
    to use Quick Drop Placement Proxy `a2` footer flags (`a2[9] = a2_mode`, `a2[10:13] =
    (0x859F4B83, 0x2AF24804, 0x859F4B83)`, `a2[16] = 0x20`, `a2[17] = 0x800`).

    When `recenter_vertices=True`, also shifts each shape's vertex array at `abs_ptrs[0]` so
    vertices are centered around `(0, 0, 0)` in hull-local space and writes the hull center
    `(cx, cy, cz, 1.0)` into `a2[4..7]` (`a2 + 0x10`), matching vanilla `_pphys` resources.

    When `strip_mv0=True` (and `sc == 1`), also reconstructs the exact vanilla `_pphys` (`mv == 0`)
    binary layout observed across all 3,596 vanilla `_pphys` resources (e.g. `Bundle[344] RES[28]`):
      - `sp[0x00..0x18]` = 24 zero bytes (`0.0f * 6`)
      - `mv` (`sp + 0x1E`) = `0`
      - `rel[4..6]` = `0` (removes skateable `a2[9]=5` edge-cosine/adjacency tables)
      - `rel[7]` points directly to the `(nf + 1) * 2` face-edge index table (truncating the 8B-per-face tail)
      - `sp[0x4C..0x54]` = `(0, 0)`.
    """
    if len(pb_in) < 200:
        return pb_in
    hdr = list(struct.unpack_from("<14I", pb_in, 0))
    flag = hdr[2]
    sc = hdr[3]
    if flag != 0 or sc < 1 or sc > 16:
        return pb_in

    abs_offs = [0x24 + j * 4 + struct.unpack_from("<i", pb_in, 0x24 + j * 4)[0] for j in range(5)]
    sp0, a2_0, d0 = abs_offs[0], abs_offs[2], abs_offs[4]
    if sp0 < 0x38 or a2_0 < sp0 + sc * 0x54 or d0 < a2_0 + sc * 0x48 or d0 > len(pb_in):
        return pb_in

    out = bytearray(pb_in)
    for k in range(sc):
        sp = sp0 + k * 0x54
        a2 = a2_0 + k * 0x48
        if a2 + 0x48 > len(out) or sp + 0x54 > len(out):
            return pb_in
        a2_words = list(struct.unpack_from("<18I", out, a2))
        a2_words[9] = int(a2_mode)
        a2_words[10] = 0x859F4B83
        a2_words[11] = 0x2AF24804
        a2_words[12] = 0x859F4B83
        a2_words[16] = 0x20
        a2_words[17] = 0x800
        struct.pack_into("<18I", out, a2, *a2_words)

        if recenter_vertices:
            nv_k, ne_k, nf_k, mv_k = struct.unpack_from("<4H", out, sp + 0x18)
            rel_k = struct.unpack_from("<10i", out, sp + 0x24)
            v_abs = sp + 0x24 + rel_k[0] if rel_k[0] != 0 else 0
            if nv_k > 0 and v_abs >= d0 and v_abs + nv_k * 12 <= len(out):
                tx_k, ty_k, tz_k = struct.unpack_from("<3f", out, a2 + 0x10)
                t_vec_k = np.array([tx_k, ty_k, tz_k], dtype=np.float32)
                v_loc = np.frombuffer(out, dtype="<f4", count=nv_k * 3, offset=v_abs).reshape(nv_k, 3).copy()
                v_body = v_loc + t_vec_k
                v_min_k = v_body.min(axis=0)
                v_max_k = v_body.max(axis=0)
                new_c = 0.5 * (v_min_k + v_max_k)
                new_v_loc = (v_body - new_c).astype("<f4")
                out[v_abs : v_abs + nv_k * 12] = new_v_loc.tobytes()
                struct.pack_into("<4f", out, a2 + 0x10, float(new_c[0]), float(new_c[1]), float(new_c[2]), 1.0)
                if not strip_mv0:
                    loc_min = new_v_loc.min(axis=0)
                    loc_max = new_v_loc.max(axis=0)
                    struct.pack_into(
                        "<6f", out, sp,
                        float(loc_min[0]), float(loc_min[1]), float(loc_min[2]),
                        float(loc_max[0]), float(loc_max[1]), float(loc_max[2]),
                    )

    if strip_mv0 and sc == 1:
        nv, ne, nf, mv = struct.unpack_from("<4H", out, sp0 + 0x18)
        rel = list(struct.unpack_from("<10i", out, sp0 + 0x24))
        abs_ptrs = [0 if rel[j] == 0 else (sp0 + 0x24 + j * 4 + rel[j]) for j in range(10)]
        # Verify abs_ptrs[3] (edges) and abs_ptrs[7] (face-edge index table) are valid
        end_of_arr3 = abs_ptrs[4] if abs_ptrs[4] > abs_ptrs[3] else (abs_ptrs[3] + ne * 2)
        # Align end_of_arr3 to 2-byte boundary (note: in vanilla mv=0, abs[7] follows immediately at abs[3] + ne*2)
        new_abs7 = abs_ptrs[3] + ne * 2
        tail_idx_len = (nf + 1) * 2
        if (
            abs_ptrs[0] == d0
            and abs_ptrs[3] > abs_ptrs[2] > abs_ptrs[1] > abs_ptrs[0]
            and new_abs7 <= end_of_arr3
            and abs_ptrs[7] >= end_of_arr3
            and abs_ptrs[7] + tail_idx_len <= len(out)
        ):
            tail_idx_bytes = bytes(out[abs_ptrs[7] : abs_ptrs[7] + tail_idx_len])
            new_pb = bytearray(out[:new_abs7]) + tail_idx_bytes
            # Zero sp0[0x00..0x18] and sp0[0x4C..0x54], set mv=0, and update rel[4..7]
            new_pb[sp0 : sp0 + 0x18] = b"\x00" * 24
            struct.pack_into("<H", new_pb, sp0 + 0x1E, 0)
            rel[4] = 0
            rel[5] = 0
            rel[6] = 0
            rel[7] = new_abs7 - (sp0 + 0x24 + 7 * 4)
            rel[8] = 0
            rel[9] = 0
            struct.pack_into("<10i", new_pb, sp0 + 0x24, *rel)
            new_pb[sp0 + 0x4C : sp0 + 0x54] = b"\x00" * 8
            return bytes(new_pb)

    return bytes(out)


def decompose_mesh_into_structural_pphys_hulls(raw_tm: trimesh.Trimesh, max_hulls: int = 8, max_faces_per_hull: int = 24) -> list[trimesh.Trimesh]:
    """
    Decomposes a connected or multi-part prop mesh (such as a grind rail with two posts and an elevated bar,
    a bench with legs and an overhanging deck, a gazebo, or a pole jam) into up to `max_hulls` (`<= 8`)
    tight convex hulls that preserve open gaps underneath elevated bars/decks and under cantilevered overhangs!

    Key Precision Rules (verified in-game via Pack v3):
    1. First checks if the mesh already contains distinct non-overlapping mesh parts (`split(only_watertight=False, repair=False)`):
       - For multi-part models (like benches/rigs where legs, deck, overhang, and kicker are separate mesh elements),
         preserves each part's exact geometry directly instead of blending adjacent parts across coarse bins!
       - For sloped/wedge parts (like kickers or pole jams), also generates stair-stepped sub-hulls into any remaining
         slots up to `max_hulls` (`8`) so Quick Drop's placement solver has crisp horizontal & sloped contact planes.
    2. For single-manifold or face-split meshes with an open under-gap (like `long_rail_skinny` where the posts and
       top bar share vertices or per-face strips):
       - Computes the exact underside of the elevated bar/deck (`y_bar_bottom`) from the mesh vertices/triangles in the
         open gap (`y_split = y_bar_bottom`, without dropping 1.5cm below the bar!)
       - Clusters lower support vertices (`Y < y_bar_bottom - 0.005`) by actual spatial distance (`<= 5cm` gap along `dom_ax`)
         rather than coarse 24-bin boundaries, so a 6cm-wide rail post stays `6.1cm` wide (`[-2.835, -2.773]`) instead of
         inflating to `25cm..30cm`!
    """
    if raw_tm is None or not hasattr(raw_tm, "faces") or len(raw_tm.faces) == 0:
        return []
    per_hull_faces = max(10, min(48, int(max_faces_per_hull)))
    v = np.asarray(raw_tm.vertices, dtype=np.float64)
    f = np.asarray(raw_tm.faces, dtype=np.int32)
    bmin = v.min(axis=0)
    bmax = v.max(axis=0)
    span = np.maximum(bmax - bmin, 1e-3)
    height_y = float(span[1])

    def _make_hull_from_verts(pts: np.ndarray, prefer_tight_box: bool = False) -> trimesh.Trimesh | None:
        if pts is None or len(pts) < 2:
            return None
        p_min = pts.min(axis=0)
        p_max = np.maximum(pts.max(axis=0), p_min + 0.015)
        ext = p_max - p_min
        ctr = 0.5 * (p_min + p_max)
        if prefer_tight_box or len(pts) < 4:
            bx = trimesh.creation.box(extents=ext)
            bx.apply_translation(ctr)
            return bx
        try:
            ch = trimesh.points.PointCloud(pts).convex_hull
            if len(ch.faces) >= 4 and len(ch.vertices) >= 4:
                aabb_v = float(ext[0] * ext[1] * ext[2])
                ch_v = float(abs(ch.volume)) if ch.is_volume else 0.0
                if aabb_v > 0.0 and ch_v >= 0.78 * aabb_v:
                    bx = trimesh.creation.box(extents=ext)
                    bx.apply_translation(ctr)
                    return bx
                return simplify_collision_mesh_for_havok(ch, max_faces=per_hull_faces, force_convex=True)
        except Exception:
            pass
        bx = trimesh.creation.box(extents=ext)
        bx.apply_translation(ctr)
        return bx

    def _split_sloped_wedge_into_hulls(sp_tm: trimesh.Trimesh, slope_ax: int | None = None, max_step_boxes: int = 3) -> list[trimesh.Trimesh]:
        """
        Converts a sloped ramp / kicker / pole-jam wedge into its exact sloped 6-vertex prism hull PLUS
        up to `max_step_boxes` stair-stepped tight boxes along its slope so Quick Drop's placement solver
        gets rock-solid horizontal & sloped contact without any air-gap!
        """
        sv = np.asarray(sp_tm.vertices, dtype=np.float64)
        s_min, s_max = sv.min(axis=0), sv.max(axis=0)
        s_span = np.maximum(s_max - s_min, 0.015)
        s_ax = int(slope_ax) if slope_ax in (0, 2) else (0 if s_span[0] >= s_span[2] else 2)
        top_v = sv[sv[:, 1] >= s_min[1] + 0.80 * s_span[1]]
        high_at_max = (float(top_v[:, s_ax].mean()) >= 0.5 * float(s_min[s_ax] + s_max[s_ax])) if len(top_v) else True
        wedge_hulls: list[trimesh.Trimesh] = []
        # Also build the exact 6-vertex right-triangular prism convex hull if trimesh convex_hull succeeded
        try:
            if high_at_max:
                p_verts = np.array([
                    [s_min[0], s_min[1], s_min[2]],
                    [s_min[0], s_min[1], s_max[2]],
                    [s_max[0], s_min[1], s_min[2]],
                    [s_max[0], s_min[1], s_max[2]],
                    [s_max[0] if s_ax == 0 else s_min[0], s_max[1], s_min[2] if s_ax == 0 else s_max[2]],
                    [s_max[0], s_max[1], s_max[2]],
                ], dtype=np.float64)
            else:
                p_verts = np.array([
                    [s_min[0], s_min[1], s_min[2]],
                    [s_min[0], s_min[1], s_max[2]],
                    [s_max[0], s_min[1], s_min[2]],
                    [s_max[0], s_min[1], s_max[2]],
                    [s_min[0], s_max[1], s_min[2]],
                    [s_min[0] if s_ax == 0 else s_max[0], s_max[1], s_max[2] if s_ax == 0 else s_min[2]],
                ], dtype=np.float64)
            p_faces = np.array([
                [0, 2, 1], [1, 2, 3],  # Bottom quad
                [2, 4, 3], [3, 4, 5],  # Back vertical quad (or front)
                [0, 1, 4], [1, 5, 4],  # Sloped ramp quad
                [0, 4, 2],             # Side triangle 1
                [1, 3, 5],             # Side triangle 2
            ], dtype=np.int32)
            prism_tm = trimesh.Trimesh(vertices=p_verts, faces=p_faces, process=True)
            prism_tm.fix_normals()
            if len(prism_tm.vertices) >= 6 and len(prism_tm.faces) >= 6:
                wedge_hulls.append(prism_tm)
        except Exception:
            pass
        if int(max_step_boxes) <= 0:
            return wedge_hulls
        n_steps = max(2, min(4, int(max_step_boxes)))
        step_edges = np.linspace(s_min[s_ax], s_max[s_ax], n_steps + 1)
        for si in range(n_steps):
            lo_x = step_edges[si] - (0.015 if si > 0 else 0.0)
            hi_x = step_edges[si + 1] + (0.015 if si + 1 < n_steps else 0.0)
            if high_at_max:
                t_lo = max(0.0, (si - 0.15) / float(n_steps))
                t_hi = min(1.0, (si + 1.0) / float(n_steps))
                touch_floor = (si == 0)
            else:
                t_lo = max(0.0, (n_steps - si - 1 - 0.15) / float(n_steps))
                t_hi = min(1.0, (n_steps - si) / float(n_steps))
                touch_floor = (si == n_steps - 1)
            b0 = s_min.copy()
            b1 = s_max.copy()
            b0[s_ax] = lo_x
            b1[s_ax] = hi_x
            b0[1] = s_min[1] + (0.0 if touch_floor else t_lo * s_span[1])
            b1[1] = s_min[1] + max(0.18 * s_span[1], t_hi * s_span[1])
            sub_bx = _make_hull_from_verts(np.vstack([b0, b1]), prefer_tight_box=True)
            if sub_bx is not None:
                wedge_hulls.append(sub_bx)
        return wedge_hulls

    def _merge_parts_down_to_budget(parts_list: list[trimesh.Trimesh], budget: int) -> list[trimesh.Trimesh]:
        """
        When a prop has `> budget` (`> 8`) sub-parts (e.g. a 12-piece S-curve bench or 14-piece wavy rail+post assembly),
        greedily merges the spatially closest adjacent pair of same-tier parts (keeping ground-touching legs/posts separate
        from elevated top-bar/deck segments whenever possible) until `len(work_list) == budget`.
        This guarantees that a 10..20 piece prop preserves its full end-to-end length and curve contour across all 8 hulls
        instead of dropping parts 9..N!
        """
        work_list = [p for p in parts_list if p is not None and hasattr(p, "vertices") and len(p.vertices) >= 4]
        if len(work_list) <= budget:
            return work_list
        all_ymin = min(float(p.bounds[0][1]) for p in work_list)
        all_ymax = max(float(p.bounds[1][1]) for p in work_list)
        h_total = max(1e-3, all_ymax - all_ymin)
        ground_y = all_ymin + 0.18 * h_total
        elev_y = all_ymin + 0.28 * h_total

        def _part_tier(p: trimesh.Trimesh) -> int:
            b0, b1 = p.bounds[0], p.bounds[1]
            ext = b1 - b0
            if float(b0[1]) >= elev_y:
                return 1  # Elevated deck / bar segment
            if float(b0[1]) <= ground_y and float(b1[1]) <= all_ymin + 0.90 * h_total:
                if max(float(ext[0]), float(ext[2])) <= 0.38:
                    return 0  # Narrow vertical ground support leg / post / footplate
                return 2      # Wide ground manual pad / ramp block
            return 2          # Full-height or sloped transition part

        while len(work_list) > budget:
            best_pair = (0, 1)
            best_cost = float("inf")
            n_cur = len(work_list)
            tiers = [_part_tier(p) for p in work_list]
            centers = [0.5 * (p.bounds[0] + p.bounds[1]) for p in work_list]
            for i in range(n_cur):
                b0_i, b1_i = work_list[i].bounds[0], work_list[i].bounds[1]
                vol_i = float(np.prod(np.maximum(b1_i - b0_i, 0.01)))
                for j in range(i + 1, n_cur):
                    b0_j, b1_j = work_list[j].bounds[0], work_list[j].bounds[1]
                    vol_j = float(np.prod(np.maximum(b1_j - b0_j, 0.01)))
                    u_min = np.minimum(b0_i, b0_j)
                    u_max = np.maximum(b1_i, b1_j)
                    u_vol = float(np.prod(np.maximum(u_max - u_min, 0.01)))
                    dead_space = max(0.0, u_vol - (vol_i + vol_j))
                    dist = float(np.linalg.norm(centers[i] - centers[j]))
                    # Check if a post and its footplate overlap horizontally (same X/Z column)
                    horiz_gap_x = max(0.0, float(max(b0_i[0], b0_j[0]) - min(b1_i[0], b1_j[0])))
                    horiz_gap_z = max(0.0, float(max(b0_i[2], b0_j[2]) - min(b1_i[2], b1_j[2])))
                    same_vertical_column = (horiz_gap_x <= 0.04 and horiz_gap_z <= 0.04)
                    tier_penalty = 0.0 if tiers[i] == tiers[j] else 100.0
                    if tiers[i] == 0 and tiers[j] == 0:
                        if same_vertical_column:
                            # Immediately merge a vertical post with its own footplate!
                            cost = -10.0 + dist * 0.1
                        else:
                            # Strongly penalize merging two separated ground legs across an open gap!
                            cost = 80.0 + dead_space * 4.0 + dist * 2.0
                    else:
                        cost = tier_penalty + dead_space * 4.0 + dist * 0.5
                    if cost < best_cost:
                        best_cost = cost
                        best_pair = (i, j)
            i_m, j_m = best_pair
            merged_pts = np.vstack([
                np.asarray(work_list[i_m].vertices, dtype=np.float64),
                np.asarray(work_list[j_m].vertices, dtype=np.float64),
            ])
            merged_hull = _make_hull_from_verts(merged_pts, prefer_tight_box=False)
            if merged_hull is None:
                merged_hull = trimesh.util.concatenate([work_list[i_m], work_list[j_m]])
            work_list[i_m] = merged_hull
            work_list.pop(j_m)
        return work_list

    # Step 1: Check if raw_tm splits cleanly into 2..32 volumetric sub-objects (e.g. separate legs, deck, cantilever, wedge, or >8 piece curved assembly)
    try:
        raw_parts = [
            c for c in raw_tm.split(only_watertight=False, repair=False)
            if hasattr(c, "vertices") and len(c.vertices) >= 4 and len(c.faces) >= 4
        ]
    except Exception:
        raw_parts = []

    if 2 <= len(raw_parts) <= 32:
        vol_parts = []
        for cp in raw_parts:
            c_ext = cp.bounds[1] - cp.bounds[0]
            if np.sum(c_ext >= 0.015) >= 3:
                vol_parts.append(cp)
        if len(vol_parts) >= 2 and len(vol_parts) == len(raw_parts):
            if len(vol_parts) > max_hulls:
                vol_parts = _merge_parts_down_to_budget(vol_parts, max_hulls)
            vol_parts.sort(key=lambda p: float(np.prod(np.maximum(p.bounds[1] - p.bounds[0], 0.01))), reverse=True)
            out_parts: list[trimesh.Trimesh] = []
            sloped_parts: list[tuple[trimesh.Trimesh, int]] = []
            for vp in vol_parts[:max_hulls]:
                vp_v = np.asarray(vp.vertices, dtype=np.float64)
                vp_min, vp_max = vp_v.min(axis=0), vp_v.max(axis=0)
                vp_ext = vp_max - vp_min
                # Detect a sloped wedge/ramp along EITHER horizontal axis (X or Z): top Y only appears on one half of that axis,
                # AND the part actually reaches near the ground (unlike an elevated arched rail segment)!
                reaches_ground = float(vp_min[1]) <= (bmin[1] + 0.15 * height_y)
                top_mask = vp_v[:, 1] >= (vp_min[1] + 0.80 * vp_ext[1])
                top_span_0 = float(vp_v[top_mask, 0].max() - vp_v[top_mask, 0].min()) if np.any(top_mask) else float(vp_ext[0])
                top_span_2 = float(vp_v[top_mask, 2].max() - vp_v[top_mask, 2].min()) if np.any(top_mask) else float(vp_ext[2])
                slope_0 = (vp_ext[0] >= 0.15) and (top_span_0 <= 0.65 * float(vp_ext[0]))
                slope_2 = (vp_ext[2] >= 0.15) and (top_span_2 <= 0.65 * float(vp_ext[2]))
                is_sloped_wedge = reaches_ground and (vp_ext[1] >= 0.15) and (slope_0 or slope_2)
                if is_sloped_wedge:
                    sloped_parts.append((vp, 0 if slope_0 else 2))
                else:
                    # Use contoured convex hull for angled/curved segments, or tight box for axis-aligned boxes
                    h_vp = _make_hull_from_verts(vp_v, prefer_tight_box=False)
                    if h_vp is not None:
                        out_parts.append(h_vp)

            # For any sloped wedge part, distribute remaining hull slots evenly across all sloped wedges!
            n_sloped = max(1, len(sloped_parts))
            for idx_sp, (sp_tm, sp_ax) in enumerate(sloped_parts):
                if len(out_parts) >= max_hulls:
                    break
                rem_wedges = max(1, n_sloped - idx_sp)
                slots_for_this_wedge = max(1, (max_hulls - len(out_parts)) // rem_wedges)
                if slots_for_this_wedge == 1:
                    w_hulls = _split_sloped_wedge_into_hulls(sp_tm, slope_ax=sp_ax, max_step_boxes=0)
                    if w_hulls:
                        out_parts.append(w_hulls[0])
                    else:
                        h_w = _make_hull_from_verts(np.asarray(sp_tm.vertices, dtype=np.float64), prefer_tight_box=False)
                        if h_w is not None:
                            out_parts.append(h_w)
                else:
                    w_hulls = _split_sloped_wedge_into_hulls(sp_tm, slope_ax=sp_ax, max_step_boxes=max(2, slots_for_this_wedge - 1))
                    for wh in w_hulls[:slots_for_this_wedge]:
                        if len(out_parts) < max_hulls:
                            out_parts.append(wh)
            if len(out_parts) >= 2:
                return out_parts[:max_hulls]

    # Step 2: Connected / face-strip mesh with open under-space (elevated bar, bench gap, or cantilevered overhang)
    dom_ax = 0 if span[0] >= span[2] else 2
    sec_ax = 2 if dom_ax == 0 else 0
    if height_y >= 0.14 and span[dom_ax] >= 0.35:
        n_bins = 48
        edges = np.linspace(bmin[dom_ax], bmax[dom_ax], n_bins + 1)
        bin_ymin = np.full(n_bins, np.inf, dtype=np.float64)
        bin_ymax = np.full(n_bins, -np.inf, dtype=np.float64)
        # Sample triangles densely across their area so large spanning faces are represented in every bin they cross
        try:
            pts_surf, _ = trimesh.sample.sample_surface(raw_tm, count=min(8000, max(2000, len(f) * 10)), seed=42)
            sample_pts = np.vstack([v, np.asarray(pts_surf, dtype=np.float64)])
        except Exception:
            sample_pts = v
        for bi in range(n_bins):
            m_b = (sample_pts[:, dom_ax] >= edges[bi] - 1e-4) & (sample_pts[:, dom_ax] <= edges[bi + 1] + 1e-4)
            if np.any(m_b):
                bin_ymin[bi] = float(sample_pts[m_b, 1].min())
                bin_ymax[bi] = float(sample_pts[m_b, 1].max())

        valid_bins = np.isfinite(bin_ymin)
        ground_thresh = bmin[1] + 0.18 * height_y
        elev_thresh = bmin[1] + 0.30 * height_y
        has_ground_bins = np.any(valid_bins & (bin_ymin <= ground_thresh))
        has_elevated_gap_bins = np.any(valid_bins & (bin_ymin >= elev_thresh))

        if has_ground_bins and has_elevated_gap_bins:
            # Find the EXACT underside of the elevated top bar/deck (no -1.5cm downward inflation!)
            elev_ymins = bin_ymin[valid_bins & (bin_ymin >= elev_thresh)]
            y_bar_bottom = float(np.percentile(elev_ymins, 25))
            y_split = max(bmin[1] + 0.25 * height_y, min(bmin[1] + 0.88 * height_y, y_bar_bottom - 0.002))

            structural_hulls: list[trimesh.Trimesh] = []

            # 1. Upper bar / deck points: use actual mesh vertices >= y_split (falling back to sample_pts if sparse)
            upper_v_exact = v[v[:, 1] >= y_split]
            upper_v = upper_v_exact if len(upper_v_exact) >= 6 else sample_pts[sample_pts[:, 1] >= y_split]
            if len(upper_v) >= 4:
                # Check if upper_v curves/winds in the horizontal plane or varies in height (e.g. S-curve bench, curved rail, or rainbow rail)
                u_min_dom, u_max_dom = float(upper_v[:, dom_ax].min()), float(upper_v[:, dom_ax].max())
                n_u_slices = 16
                u_edges = np.linspace(u_min_dom, u_max_dom, n_u_slices + 1)
                slice_widths = np.zeros(n_u_slices, dtype=np.float64)
                slice_sec_centers = np.full(n_u_slices, np.nan, dtype=np.float64)
                slice_y_tops = np.full(n_u_slices, np.nan, dtype=np.float64)
                for ui in range(n_u_slices):
                    su = sample_pts[(sample_pts[:, 1] >= y_split) & (sample_pts[:, dom_ax] >= u_edges[ui] - 1e-4) & (sample_pts[:, dom_ax] <= u_edges[ui + 1] + 1e-4)]
                    if len(su) >= 2:
                        slice_widths[ui] = float(su[:, sec_ax].max() - su[:, sec_ax].min())
                        slice_sec_centers[ui] = 0.5 * float(su[:, sec_ax].min() + su[:, sec_ax].max())
                        slice_y_tops[ui] = float(su[:, 1].max())
                pos_w = slice_widths[slice_widths > 0.005]
                valid_c = slice_sec_centers[np.isfinite(slice_sec_centers)]
                valid_y = slice_y_tops[np.isfinite(slice_y_tops)]
                median_w = float(np.median(pos_w)) if len(pos_w) else float(span[sec_ax])
                sec_excursion = (float(valid_c.max() - valid_c.min()) if len(valid_c) >= 4 else 0.0)
                y_arch_excursion = (float(valid_y.max() - valid_y.min()) if len(valid_y) >= 4 else 0.0)
                is_curved_or_arched_upper = (sec_excursion > max(0.08, 0.45 * median_w)) or (y_arch_excursion > 0.12)

                if is_curved_or_arched_upper:
                    # Segment the curved/S-shaped/arched upper bar or deck into 4..5 tight contoured hulls along dom_ax!
                    n_curve_segs = 4
                    c_edges = np.linspace(u_min_dom, u_max_dom, n_curve_segs + 1)
                    for ci in range(n_curve_segs):
                        c_lo = c_edges[ci] - (0.015 if ci > 0 else 0.0)
                        c_hi = c_edges[ci + 1] + (0.015 if ci + 1 < n_curve_segs else 0.0)
                        seg_u = sample_pts[(sample_pts[:, 1] >= y_split) & (sample_pts[:, dom_ax] >= c_lo) & (sample_pts[:, dom_ax] <= c_hi)]
                        if len(seg_u) >= 4:
                            h_seg = _make_hull_from_verts(seg_u, prefer_tight_box=False)
                            if h_seg is not None:
                                structural_hulls.append(h_seg)
                elif len(pos_w) >= 4 and float(pos_w.max()) / max(float(pos_w.min()), 1e-3) >= 1.75:
                    # Find the exact transition coordinate where width jumps between narrow and wide
                    w_thresh = 0.5 * float(pos_w.max() + pos_w.min())
                    wide_mask = slice_widths >= w_thresh
                    split_idx = int(np.argmax(wide_mask[1:] != wide_mask[:-1])) + 1
                    x_trans = float(u_edges[split_idx])
                    left_u = upper_v[upper_v[:, dom_ax] <= x_trans + 0.005]
                    right_u = upper_v[upper_v[:, dom_ax] >= x_trans - 0.005]
                    h_ul = _make_hull_from_verts(left_u, prefer_tight_box=True)
                    h_ur = _make_hull_from_verts(right_u, prefer_tight_box=True)
                    if h_ul is not None:
                        structural_hulls.append(h_ul)
                    if h_ur is not None:
                        structural_hulls.append(h_ur)
                else:
                    h_up = _make_hull_from_verts(upper_v, prefer_tight_box=True)
                    if h_up is not None:
                        structural_hulls.append(h_up)

            # 2. Lower support legs / posts / ramps: cluster actual mesh vertices (and triangles below y_split)
            # by exact coordinate gaps (> 4cm gap along dom_ax) so posts retain their exact 1:1 width!
            lower_face_mask = np.any(v[f, 1] < (y_split - 0.005), axis=1)
            if np.any(lower_face_mask):
                lower_vert_indices = np.unique(f[lower_face_mask].ravel())
                lower_pts = v[lower_vert_indices].copy()
                # Clamp the top of lower_pts to y_split + 0.005 so vertical post faces don't pull in the entire horizontal bar
                lower_pts[:, 1] = np.minimum(lower_pts[:, 1], y_split + 0.005)
            else:
                lower_pts = sample_pts[sample_pts[:, 1] <= y_split + 0.005]

            if len(lower_pts) >= 4:
                order = np.argsort(lower_pts[:, dom_ax])
                sorted_pts = lower_pts[order]
                diffs = np.diff(sorted_pts[:, dom_ax])
                gap_thresh = max(0.045, 0.03 * float(span[dom_ax]))
                split_indices = np.where(diffs > gap_thresh)[0] + 1
                clusters = np.split(sorted_pts, split_indices)

                for cl_pts in clusters:
                    if len(cl_pts) < 4:
                        continue
                    c_min = cl_pts.min(axis=0)
                    c_max = cl_pts.max(axis=0)
                    # Only keep clusters that actually reach near the ground or span a meaningful support height
                    if c_min[1] > ground_thresh and (c_max[1] - c_min[1]) < 0.15 * height_y:
                        continue
                    # Ensure ground-reaching posts reach cleanly up to y_split so they meet the top bar with zero gap
                    if c_min[1] <= ground_thresh and c_max[1] < y_split:
                        c_max[1] = y_split + 0.002
                    c_span_dom = float(c_max[dom_ax] - c_min[dom_ax])
                    # If this support cluster is narrow (like a vertical rail post or bench leg), make it an exact tight box!
                    if c_span_dom <= 0.35:
                        bx_leg = trimesh.creation.box(extents=np.maximum(c_max - c_min, 0.015))
                        bx_leg.apply_translation(0.5 * (c_min + c_max))
                        structural_hulls.append(bx_leg)
                    else:
                        # Wide support cluster (e.g. sloped ramp/kicker + leg): split if needed
                        h_leg = _make_hull_from_verts(cl_pts, prefer_tight_box=False)
                        if h_leg is not None:
                            structural_hulls.append(h_leg)

            if len(structural_hulls) >= 2:
                if len(structural_hulls) > max_hulls:
                    structural_hulls = _merge_parts_down_to_budget(structural_hulls, max_hulls)
                return structural_hulls[:max_hulls]

    # Step 3: Ground-resting curved/S-shaped/C-shaped props (without an under-gap, e.g. solid curved Hubba or wave ramp)
    # Detect horizontal curvature or large dead-space in the single convex hull and slice along dom_ax into up to max_hulls!
    if span[dom_ax] >= 0.80 and span[sec_ax] >= 0.35:
        n_c_slices = 12
        c_edges = np.linspace(bmin[dom_ax], bmax[dom_ax], n_c_slices + 1)
        sec_means = []
        sec_widths = []
        for si in range(n_c_slices):
            sv = v[(v[:, dom_ax] >= c_edges[si] - 1e-4) & (v[:, dom_ax] <= c_edges[si + 1] + 1e-4)]
            if len(sv) >= 2:
                sec_means.append(0.5 * float(sv[:, sec_ax].min() + sv[:, sec_ax].max()))
                sec_widths.append(float(sv[:, sec_ax].max() - sv[:, sec_ax].min()))
        if len(sec_means) >= 6:
            med_w = float(np.median(sec_widths))
            excur = float(max(sec_means) - min(sec_means))
            if excur > max(0.12, 0.50 * med_w):
                n_segs = min(max_hulls, 6)
                seg_edges = np.linspace(bmin[dom_ax], bmax[dom_ax], n_segs + 1)
                curved_hulls: list[trimesh.Trimesh] = []
                for si in range(n_segs):
                    s_lo = seg_edges[si] - (0.02 if si > 0 else 0.0)
                    s_hi = seg_edges[si + 1] + (0.02 if si + 1 < n_segs else 0.0)
                    sv = v[(v[:, dom_ax] >= s_lo) & (v[:, dom_ax] <= s_hi)]
                    if len(sv) >= 4:
                        h_s = _make_hull_from_verts(sv, prefer_tight_box=False)
                        if h_s is not None:
                            curved_hulls.append(h_s)
                if len(curved_hulls) >= 2:
                    return curved_hulls[:max_hulls]

    return []


def split_mesh_into_pphys_islands(
    raw_tm: trimesh.Trimesh,
    max_islands: int = 8,
    max_faces_per_hull: int = 24,
) -> list:
    """
    Simplifies a prop or `_PPHYS` mesh into 1..`max_islands` convex polytope hulls (`<= max_faces_per_hull` faces).
    Note: Single-slot `RES[16]` / `EBX[32]` (`_pphys`) requires `sc == 1` (`max_islands=1`), whereas 8-slot
    Bundle 94 (`_pphys_0..7`) uses up to 8 separate `sc == 1` resources.
    """
    if raw_tm is None or not hasattr(raw_tm, "faces") or len(raw_tm.faces) == 0:
        return []
    work = raw_tm.copy()
    per_hull_faces = max(10, min(48, int(max_faces_per_hull)))
    if int(max_islands) <= 1:
        try:
            h_single = simplify_collision_mesh_for_havok(work, max_faces=per_hull_faces, force_convex=True)
            if h_single is not None and len(h_single.vertices) >= 4 and len(h_single.faces) >= 4:
                return [h_single]
        except Exception:
            pass

    # First check if structural gap/leg/cantilever decomposition applies
    if int(max_islands) > 1:
        try:
            struct_hulls = decompose_mesh_into_structural_pphys_hulls(work, max_hulls=max_islands, max_faces_per_hull=per_hull_faces)
            if len(struct_hulls) >= 2:
                return struct_hulls[:max_islands]
        except Exception:
            pass

    try:
        parts = [
            p for p in work.split(only_watertight=False)
            if hasattr(p, "vertices") and len(p.vertices) >= 4 and len(p.faces) >= 2
        ]
    except Exception:
        parts = [work]

    if not parts:
        parts = [work]

    def _part_score(p):
        try:
            ext = np.maximum(p.bounds[1] - p.bounds[0], 0.005)
            return float(ext[0] * ext[1] * ext[2]) + 1e-5 * len(p.faces)
        except Exception:
            return float(len(p.faces))

    parts.sort(key=_part_score, reverse=True)
    if len(parts) > max_islands:
        parts = parts[:max_islands]
    elif len(parts) == 1 and max_islands > 1 and len(parts[0].faces) >= 12:
        p0 = parts[0]
        try:
            ch0 = p0.convex_hull
            ext0 = np.maximum(p0.bounds[1] - p0.bounds[0], 0.01)
            aabb_vol = float(ext0[0] * ext0[1] * ext0[2])
            hull_vol = float(abs(ch0.volume)) if ch0.is_volume else aabb_vol
            if len(p0.faces) >= 20 and hull_vol > 0.01:
                decomp = decompose_mesh_into_hulls(p0, max_hulls=min(6, max_islands))
                if len(decomp) > 1:
                    parts = decomp
        except Exception:
            pass

    out_hulls = []
    for p in parts:
        try:
            h_tm = simplify_collision_mesh_for_havok(p, max_faces=per_hull_faces, force_convex=True)
            if h_tm is not None and len(h_tm.vertices) >= 4 and len(h_tm.faces) >= 4:
                out_hulls.append(h_tm)
        except Exception:
            pass

    if not out_hulls:
        try:
            out_hulls = [simplify_collision_mesh_for_havok(work, max_faces=per_hull_faces, force_convex=True)]
        except Exception:
            pass
    return out_hulls


# Canonical 528-byte Havok Box Polytope template (extracted from Bundle 39 res16)
CANONICAL_BOX_HAVOK_HEX = (
    "020000001570af69000000000100000000000000000000000100000000000000"
    "000000001400000010000000600000005c000000a00000000000000000000000"
    "0000000000000000000000000000000008001800060000000000000078000000"
    "d4000000e20000000e0100000000000000000000000000002e01000000000000"
    "000000003001000000000000000000000000803f000000000000000000000000"
    "71ea323d000000000000803f0000000002000000834b9f850448f22a834b9f85"
    "0000000000000000000000002000000000080000000080bf71ea32bd000080bf"
    "0000803f71ea32bd000080bf000080bf71ea323d000080bf000080bf71ea32bd"
    "0000803f0000803f71ea323d000080bf0000803f71ea32bd0000803f000080bf"
    "71ea323d0000803f0000803f71ea323d0000803f00000300060009000c000f00"
    "12001500180000000800100004000900130003000c00110001000b0014000500"
    "0f00120007000a00150002000d00170006000e00160000000300060002000100"
    "0400070005000000010005000300020006000700040000000200040001000300"
    "0500070006000000040008000c0010001400180001800000000080bfff7f0000"
    "000080bf0000018071ea32bd0000ff7f71ea32bdff7fff7f000080bf00000000"
    "000080bf00000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000"
)
CANONICAL_BOX_HAVOK_BYTES = bytes.fromhex(CANONICAL_BOX_HAVOK_HEX)


def fit_havok_box_hull(
    h_min: np.ndarray | list[float],
    h_max: np.ndarray | list[float],
    surface_code: int = 96,
    strip_mv0: bool = False,
) -> tuple[bytes, int]:
    """
    Analytically constructs and fits a 100% valid native Frostbite Havok Box Polytope
    chunk (`0x41759364`, `sc == 1`) in memory (< 0.001s), completely bypassing `reskate_cli.exe`.
    """
    t_min = np.asarray(h_min, dtype=np.float32)
    t_max = np.asarray(h_max, dtype=np.float32)
    fitted_raw, _, _ = fit_havok_polytope_to_bounds(
        CANONICAL_BOX_HAVOK_BYTES,
        t_min,
        t_max,
        material_packed=int(surface_code),
    )
    pphys = convert_havok_to_pphys_mv0(fitted_raw, a2_mode=3, strip_mv0=strip_mv0)
    return pphys, 1


def is_mesh_axis_aligned_box(mesh: trimesh.Trimesh) -> bool:
    """
    Detects whether a trimesh is an axis-aligned box polytope (rejecting rotated boxes
    whose AABB volume exceeds their true mesh volume).
    """
    if mesh is None or not hasattr(mesh, "vertices") or len(mesh.vertices) < 4:
        return False
    hv = np.asarray(mesh.vertices, dtype=np.float32)
    hf = np.asarray(mesh.faces, dtype=np.int32)
    if len(hf) < 4:
        return False
    h_min = hv.min(axis=0)
    h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
    h_ext = h_max - h_min
    aabb_v = float(h_ext[0] * h_ext[1] * h_ext[2])
    if aabb_v <= 0.0:
        return False
    if len(hv) == 8 and len(hf) == 12:
        # Verify all 8 vertices lie on the AABB corners {min_x, max_x} x {min_y, max_y} x {min_z, max_z}
        # so rotated boxes (e.g. rotated 45 deg around Z) are routed to convex hull compilation instead of AABB inflation.
        tol = np.maximum(1e-3, 0.02 * (hv.max(axis=0) - h_min))
        on_min = np.abs(hv - h_min) <= tol
        on_max = np.abs(hv - hv.max(axis=0)) <= tol
        if bool(np.all(on_min | on_max)):
            return True
    try:
        hull_v = float(abs(mesh.volume)) if mesh.is_volume else 0.0
    except Exception:
        hull_v = 0.0
    return hull_v >= 0.95 * aabb_v


def compile_multishape_havok_batch_via_cli(
    hull_shapes_list: list,
    max_faces_per_hull: int = 32,
    strip_mv0: bool = False,
    surface_code: int = 96,
    active_cli_path: Path | None = None,
    active_game_path: Path | None = None,
) -> list[tuple[bytes, int]]:
    """
    Unified Multi-Hull Havok Batching:
    Given a list of up to 12 placement proxy hulls (either `trimesh.Trimesh` or shape dicts):
    1. Analytically fits all AABB box hulls in-process (<0.001s).
    2. Bundles all remaining contoured hulls into a single `map.json` and temporary GLB scene.
    3. Executes `reskate_cli.exe compile-map` ONCE, extracting all individual `_pphys` chunks
       from the output map bundle and caching them.
    Returns `list[tuple[bytes, int]]` matching the exact input order.
    """
    if not hull_shapes_list:
        return []

    hull_shapes_list = list(hull_shapes_list)[:12]
    active_cli = Path(active_cli_path or os.environ.get("RESKATE_CLI_EXE") or CLI_EXE)
    active_game_dir = Path(active_game_path or os.environ.get("RESKATE_GAME_DIR") or GAME_DIR)
    comp_ver = os.environ.get("RESKATE_COMPILER_VERSION", "0.32.1")
    fast_mode = _is_fast_pipeline_mode()
    cache_root = (_get_compiler_cache_root() / "havok_hulls_v1") if fast_mode else None
    if cache_root is not None:
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            cache_root = None

    normalized_hulls = []
    for item in hull_shapes_list:
        if isinstance(item, dict):
            normalized_hulls.append(item)
        elif isinstance(item, trimesh.Trimesh):
            normalized_hulls.append({
                "mesh": item,
                "surface_id": "Concrete_Solid",
                "packed": int(surface_code),
                "round_rail": False,
                "grind_curve": False,
            })
        else:
            raise TypeError(f"Unsupported hull item type: {type(item)}")

    results: list[tuple[bytes, int] | None] = [None] * len(normalized_hulls)
    uncached_contoured: list[tuple[int, dict, str]] = []  # (orig_idx, shape_dict, sha1_key)

    import pickle
    cli_st = active_cli.stat() if active_cli.exists() else None
    cli_sig = f"{cli_st.st_size}:{cli_st.st_mtime_ns}" if cli_st else "nocli"

    # Step A: Evaluate analytical box fitting & disk cache
    for idx, s in enumerate(normalized_hulls):
        tm = s["mesh"]
        hv = np.asarray(tm.vertices, dtype=np.float32)
        hf = np.asarray(tm.faces, dtype=np.int32)
        h_min = hv.min(axis=0)
        h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
        packed_code = int(s.get("packed", surface_code))

        # Check AABB Box Hull
        if is_mesh_axis_aligned_box(tm):
            pphys_bytes, sc = fit_havok_box_hull(h_min, h_max, surface_code=packed_code, strip_mv0=strip_mv0)
            results[idx] = (pphys_bytes, sc)
            continue

        # Check Disk Cache for contoured hull
        h_sha = hashlib.sha1()
        h_sha.update(
            f"hull_batch_v1|cv:{comp_ver}|gd:{str(active_game_dir)}|cli:{cli_sig}|maxf:{int(max_faces_per_hull)}|mv0:{bool(strip_mv0)}|sc:{packed_code}|".encode("utf-8")
        )
        h_sha.update(np.round(hv, 5).tobytes())
        h_sha.update(hf.tobytes())
        sha1_key = h_sha.hexdigest()

        if cache_root is not None:
            cfile = cache_root / f"hull8_{sha1_key}.pkl"
            if cfile.is_file():
                try:
                    cached_val = pickle.loads(cfile.read_bytes())
                    if isinstance(cached_val, tuple) and len(cached_val) == 2:
                        cached_pb, cached_second = cached_val
                        if isinstance(cached_pb, bytes) and len(cached_pb) >= 200:
                            sc_val = int(cached_second) if isinstance(cached_second, (int, np.integer)) else 1
                            if sc_val >= 1:
                                results[idx] = (cached_pb, sc_val)
                                continue
                except Exception:
                    pass

        uncached_contoured.append((idx, s, sha1_key))

    # If all hulls were satisfied by analytical box fitting or disk cache, return immediately!
    if not uncached_contoured:
        return [res for res in results if res is not None]

    # Step B: Unified Batch CLI Invocation for all remaining contoured hulls
    tmp_root = _get_staging_dir() / f"havok_cli_tmp_batch_{os.getpid()}_{threading.get_ident()}"
    tmp_in = tmp_root / "in_map"
    tmp_out = tmp_root / "out_map"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    (tmp_in / "meshes").mkdir(parents=True, exist_ok=True)
    tmp_out.mkdir(parents=True, exist_ok=True)

    try:
        materials_dict = {}
        objects_list = []
        per_hull_cap = max(12, min(48, int(max_faces_per_hull)))

        for slot_i, (orig_idx, s, _) in enumerate(uncached_contoured):
            raw_tm = s["mesh"]
            if len(raw_tm.faces) > per_hull_cap or len(raw_tm.vertices) > 64:
                safe_tm = simplify_collision_mesh_for_havok(raw_tm, max_faces=per_hull_cap, force_convex=True)
            else:
                safe_tm = raw_tm
            if safe_tm is None or len(safe_tm.vertices) < 4:
                safe_tm = raw_tm

            hull_glb = tmp_in / "meshes" / f"hull_{orig_idx}.glb"
            safe_tm.export(str(hull_glb))

            m_key = f"m{slot_i}"
            materials_dict[m_key] = {"surface": "wood"}
            packed_val = int(s.get("packed", surface_code))
            obj_rec = {
                "name": f"hull_{orig_idx}",
                "placement_mode": "authored_mesh",
                "mesh": f"meshes/hull_{orig_idx}.glb",
                "primitive": 0,
                "material": m_key,
                "render": True,
                "transform": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
                "static": True,
                "collision_mode": "triangle_mesh",
                "collision_material": f"material_{packed_val:04d}",
                "collision_material_packed": packed_val,
            }
            objects_list.append(obj_rec)

        doc = {
            "format": 1,
            "units": "meters",
            "up": "y",
            "forward": "-z",
            "materials": materials_dict,
            "surface_profiles": {},
            "objects": objects_list,
            "lights": [],
            "audio_volumes": [],
            "audio_emitters": [],
            "interaction_prefabs": [],
            "trigger_effects": [],
            "vfx_prefabs": [],
            "npc_routes": [],
            "spawn": {"position": [0, 1, 0], "forward": [0, 0, -1], "up": [0, 1, 0]},
        }
        (tmp_in / "map.json").write_text(json.dumps(doc), encoding="utf-8")

        res = subprocess.run(
            [str(active_cli), "compile-map", str(active_game_dir), str(tmp_in), str(tmp_out), "--no-gi", "--no-lods"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Unified multi-hull compile-map failed: {res.stderr}")

        # Parse output map bundle and extract all individual hull physics chunks
        toc_p = next(tmp_out.rglob("*in_map.toc"))
        tb = toc_p.read_bytes()
        base = 0x22C
        b_off = struct.unpack_from(">I", tb, base + 0x04)[0]
        b_cnt = struct.unpack_from(">I", tb, base + 0x08)[0]

        oodle = get_oodle()

        def _read_cas(is_patch, pkg, cas, off, sz):
            if is_patch:
                folder = "initialinstallpackage" if pkg == 0 else "defaultinstallpackage"
                p = tmp_out / "Patch" / "Win32" / "configurations" / "layout" / folder / f"cas_{cas:02d}.cas"
                if p.exists():
                    with open(p, "rb") as f:
                        f.seek(off)
                        return f.read(sz)
            return read_cas_raw(pkg, cas, off, sz)

        def _decomp(is_patch, pkg, cas, off, sz):
            raw = _read_cas(is_patch, pkg, cas, off, sz)
            out = bytearray()
            cur = 0
            while cur < len(raw):
                unc_size, flag_type, c_size = struct.unpack_from(">IHH", raw, cur)
                comp_data = raw[cur + 8 : cur + 8 + c_size]
                if flag_type in (0x0070, 0x0000) or unc_size == c_size:
                    out.extend(comp_data)
                else:
                    out_buf = ctypes.create_string_buffer(unc_size)
                    ret = oodle.OodleLZ_Decompress(
                        comp_data, c_size, out_buf, unc_size, 1, 1, 0, None, 0, None, None, None, 0, 3
                    )
                    out.extend(out_buf.raw[:ret])
                cur += 8 + c_size
            return bytes(out)

        extracted_map: dict[int, tuple[bytes, int]] = {}
        for b_i in range(b_cnt):
            u3 = struct.unpack_from(">I", tb, base + b_off + b_i * 16 + 12)[0]
            rec_pos = base + u3
            hdr = struct.unpack_from(">9I", tb, rec_pos)
            total_s = hdr[3]
            pos = rec_pos + 36
            streams = []
            cur_p = cur_pkg = cur_cas = 0
            while len(streams) < total_s:
                sig = tb[pos + 2 : pos + 6]
                if sig == b"\xbc\x80\xe6\x99":
                    cur_p = 1 if tb[pos + 1] else 0
                    cur_pkg = 0
                    cur_cas = tb[pos + 7]
                    pos += 8
                elif sig == b"\x78\x37\x15\x2c":
                    cur_p = 1 if tb[pos + 1] else 0
                    cur_pkg = 1
                    cur_cas = tb[pos + 7]
                    pos += 8
                off, sz = struct.unpack_from(">II", tb, pos)
                pos += 8
                streams.append((cur_p, cur_pkg, cur_cas, off, sz))

            sb_data = _read_cas(*streams[0])
            ebxs, ress, chks = parse_sbdata(sb_data)
            for r_i, r in enumerate(ress):
                r_name = r.get("name", "")
                if r["res_type"] == 0x41759364 and "collision/hull_" in r_name:
                    m = re.search(r"collision/hull_(\d+)_", r_name)
                    if m:
                        matched_idx = int(m.group(1))
                        pb_cli = _decomp(*streams[1 + len(ebxs) + r_i])
                        h = struct.unpack_from("<14I", pb_cli, 0)
                        flag = h[2]
                        shape_cnt = h[3]
                        if flag == 1:
                            pb_bk = bytearray(pb_cli[:0x38] + pb_cli[0x84:])
                            struct.pack_into("<I", pb_bk, 0x08, 0)
                            for off_fix in (0x28, 0x2C, 0x30, 0x34):
                                val = struct.unpack_from("<I", pb_bk, off_fix)[0]
                                struct.pack_into("<I", pb_bk, off_fix, val - 0x4C)
                        else:
                            pb_bk = bytearray(pb_cli)
                        pb_pphys = convert_havok_to_pphys_mv0(bytes(pb_bk), a2_mode=3, strip_mv0=strip_mv0)
                        extracted_map[matched_idx] = (pb_pphys, int(shape_cnt))

        # Assign extracted chunks and cache them
        for orig_idx, s, sha1_key in uncached_contoured:
            if orig_idx in extracted_map:
                pb_res, sc = extracted_map[orig_idx]
                results[orig_idx] = (pb_res, sc)
                if cache_root is not None and sha1_key:
                    try:
                        (cache_root / f"hull8_{sha1_key}.pkl").write_bytes(
                            pickle.dumps((pb_res, sc), protocol=pickle.HIGHEST_PROTOCOL)
                        )
                    except Exception:
                        pass
            else:
                # Analytical box fallback if a specific hull wasn't produced
                tm = s["mesh"]
                hv = np.asarray(tm.vertices, dtype=np.float32)
                h_min = hv.min(axis=0)
                h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
                packed_code = int(s.get("packed", surface_code))
                results[orig_idx] = fit_havok_box_hull(h_min, h_max, surface_code=packed_code, strip_mv0=strip_mv0)

    except Exception as batch_err:
        print(f"  [Havok Batch] Warning: batch compile-map failed: {batch_err}. Falling back to analytical box fitting.", flush=True)
        for orig_idx, s, _ in uncached_contoured:
            if results[orig_idx] is None:
                tm = s["mesh"]
                hv = np.asarray(tm.vertices, dtype=np.float32)
                h_min = hv.min(axis=0)
                h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
                packed_code = int(s.get("packed", surface_code))
                results[orig_idx] = fit_havok_box_hull(h_min, h_max, surface_code=packed_code, strip_mv0=strip_mv0)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return [r for r in results if r is not None]


def compile_contoured_pphys_via_cli(
    pphys_meshes: list,
    max_faces_per_hull: int = 32,
    exact_1_1: bool = False,
    strip_mv0: bool = False,
    surface_code: int = 96,
) -> tuple[bytes, int]:
    """
    Compiles a single Havok physics shape (`sc == 1`) into a Quick Drop Placement Proxy
    (`.ecsphysics` `0x41759364` resource for `RES[16]` / `EBX[32]`) with fast analytical
    box fitting and batch caching.

    When `exact_1_1=True`, compiles the 1:1 model mesh directly (`force_exact_triangle_mesh=True`)
    as a single Havok shape (`sc == 1`) instead of simplifying to a convex hull.
    """
    valid_parts = [
        m for m in (pphys_meshes or [])
        if m is not None and hasattr(m, "vertices") and len(m.vertices) >= 4
    ]
    if not valid_parts:
        raise ValueError("No valid mesh geometry for contoured _pphys")

    if len(valid_parts) == 1:
        combined_tm = valid_parts[0]
    else:
        try:
            combined_tm = trimesh.util.concatenate(valid_parts)
        except Exception:
            all_v = np.vstack([np.asarray(m.vertices, dtype=np.float64) for m in valid_parts])
            combined_tm = trimesh.Trimesh(vertices=all_v, faces=np.zeros((0, 3), dtype=np.int64), process=False).convex_hull

    if exact_1_1 and hasattr(combined_tm, "faces") and len(combined_tm.faces) >= 2:
        h_tm = combined_tm.copy()
        if len(h_tm.faces) > 5000:
            h_tm = decimate_trimesh_fast(h_tm, 5000)
        col_shapes = [{
            "mesh": h_tm,
            "mode": "triangle_mesh",
            "surface_id": "Concrete_Solid",
            "packed": int(surface_code),
            "round_rail": False,
            "grind_curve": False,
        }]
        pb_raw, sc = compile_multishape_havok_via_cli(
            col_shapes,
            max_faces_per_hull=50000,
            force_exact_triangle_mesh=True,
        )
        if int(sc) == 1 and pb_raw and len(pb_raw) >= 200:
            return convert_havok_to_pphys_mv0(pb_raw, a2_mode=3, strip_mv0=strip_mv0), 1

    # Step 1: In-process analytical box fitting (< 0.001s)
    if is_mesh_axis_aligned_box(combined_tm):
        hv = np.asarray(combined_tm.vertices, dtype=np.float32)
        h_min = hv.min(axis=0)
        h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
        return fit_havok_box_hull(h_min, h_max, surface_code=surface_code, strip_mv0=strip_mv0)

    # Step 2: Batched & cached compile_multishape_havok_batch_via_cli
    batch_res = compile_multishape_havok_batch_via_cli(
        [combined_tm],
        max_faces_per_hull=max_faces_per_hull,
        strip_mv0=strip_mv0,
        surface_code=surface_code,
    )
    if batch_res:
        return batch_res[0]

    # Fallback to analytical box
    hv = np.asarray(combined_tm.vertices, dtype=np.float32)
    h_min = hv.min(axis=0)
    h_max = np.maximum(hv.max(axis=0), h_min + 0.020)
    return fit_havok_box_hull(h_min, h_max, surface_code=surface_code, strip_mv0=strip_mv0)



def patch_physics_ebx_volume_moments(phys_ebx_bytes: bytes, vol_moments_16f: tuple, shape_cnt: int = 1, is_skateable_col: bool = False) -> bytes:
    """
    Patches `VolumeMoments` (64B at `20+60..20+124`) and, if `is_skateable_col=True`
    (an `.ecsphysics` EBX with `ebxx_cnt == 1` `ShapeToFaceMaterialMap`), updates
    `ShapeToFaceMaterialMap` count (`20+188`), indices (`20+192..204`), and `EBXX` array count (`ebxx_pos + 20`).
    """
    col_eb = bytearray(phys_ebx_bytes)
    if len(col_eb) >= 20 + 124:
        struct.pack_into("<16f", col_eb, 20 + 60, *vol_moments_16f)
    if is_skateable_col and len(col_eb) >= 20 + 204:
        sc = max(1, min(12, int(shape_cnt)))
        struct.pack_into("<I", col_eb, 20 + 188, sc)
        col_eb[20 + 192 : 20 + 204] = bytes(range(sc)).ljust(12, b"\x00")
        ebxx_pos = col_eb.find(b"EBXX")
        if ebxx_pos != -1 and len(col_eb) >= ebxx_pos + 24:
            ebxx_cnt = struct.unpack_from("<I", col_eb, ebxx_pos + 8)[0]
            if ebxx_cnt >= 1:
                struct.pack_into("<I", col_eb, ebxx_pos + 20, sc)
    return bytes(col_eb)


def patch_prefab_ebx_materials(eb_bytes: bytes, packed_materials: list) -> bytes:
    """
    Patches all `EcsValuePair` boxed `MaterialDecl.Packed` values (`Material`, `material-0`..`material-11`)
    AND strips any `JumpPadBehaviour` bounce (`(val >> 19) == 86`, e.g. `45092384` / `45091552` in `basictrampoline_00001`)
    inside `*_ecs_ecsprefab.nopolicy` and `*_ecsprefab` EBX files.
    """
    if not packed_materials:
        packed_materials = [3680]
    dom_packed = int(packed_materials[0])

    form, chunks = demux_riff(eb_bytes)
    cmap = {cid: payload for cid, payload in chunks}
    if b"EBXD" not in cmap:
        return eb_bytes
    ebxd = cmap[b"EBXD"]

    # Map key names ("material", "material-0".."material-11") and their hashes to user's packed material list
    key_to_packed = {"material": dom_packed}
    hash_to_packed = {fnv1_32_lower("material"): dom_packed}
    for k in range(12):
        pk = int(packed_materials[k]) if k < len(packed_materials) else dom_packed
        key_to_packed[f"material-{k}"] = pk
        hash_to_packed[fnv1_32_lower(f"material-{k}")] = pk

    # 1a. Patch canonical 32-byte EcsValuePair entries via EBXX (type_code @ +0, v_rel @ +8, s_rel @ +16, k_hash @ +24)
    ebxx = cmap.get(b"EBXX")
    if ebxx is not None and len(ebxx) >= 8:
        na, _ = struct.unpack_from("<2i", ebxx, 0)
        for i in range(max(0, na)):
            if 8 + (i + 1) * 16 > len(ebxx):
                break
            arr_off, cnt, h, enc, _ = struct.unpack_from("<IiIHH", ebxx, 8 + i * 16)
            if h == 0x008594DA and cnt > 0:
                for idx in range(cnt):
                    e_pos = 12 + arr_off + idx * 32
                    if e_pos + 32 > len(ebxd):
                        break
                    tc, v_rel, s_rel = struct.unpack_from("<Qqq", ebxd, e_pos)
                    v_abs = e_pos + 8 + v_rel
                    s_abs = e_pos + 16 + s_rel
                    if 0 < tc < 0x200 and 12 <= v_abs <= len(ebxd) - 4 and 12 <= s_abs < len(ebxd):
                        s_end = ebxd.find(b"\x00", s_abs)
                        if s_end != -1:
                            s_key = ebxd[s_abs:s_end].decode("ascii", errors="ignore").lower()
                            if s_key in key_to_packed:
                                struct.pack_into("<I", ebxd, v_abs, key_to_packed[s_key])

    # 1b. Fallback scan for EcsValuePair boxed MaterialDecl entries
    for off in range(0, len(ebxd) - 32, 4):
        h_val = struct.unpack_from("<I", ebxd, off + 8)[0]
        if h_val in hash_to_packed:
            type_code = struct.unpack_from("<Q", ebxd, off + 16)[0]
            v_rel = struct.unpack_from("<q", ebxd, off + 24)[0]
            if 0 < type_code < 0x100 and v_rel > 0:
                t_off = off + 24 + v_rel
                if 0 <= t_off <= len(ebxd) - 4:
                    struct.pack_into("<I", ebxd, t_off, hash_to_packed[h_val])

    # 2. Strip any JumpPadBehaviour bounce (intrinsic property slot 86 -> val >> 19 == 86) anywhere in EBXD
    for off in range(0, len(ebxd) - 4, 4):
        val = struct.unpack_from("<I", ebxd, off)[0]
        if (val >> 19) == 86 and (val & 0x3F) == 0 and ((val >> 6) & 0x1FFF) < 300:
            struct.pack_into("<I", ebxd, off, dom_packed)

    return remux_riff(form, chunks)


def compile_mesh_into_donor(
    donor_ms: bytes,
    donor_mc_ebx: bytes,
    verts: np.ndarray,
    normals: np.ndarray,
    uvs: np.ndarray,
    faces: np.ndarray,
    vc_layer: int = 0,
    active_sec: int = 0,
    flip_normals: bool = False,
    vc_layers: np.ndarray | None = None,
):
    total_verts = len(verts)
    total_prims = len(faces)
    if total_verts > 65535:
        raise ValueError(f"Mesh vertex count ({total_verts}) exceeds Frostbite uint16 limit (65,535).")

    lp0 = struct.unpack_from("<Q", donor_ms, 0x30)[0]
    sec_cnt = struct.unpack_from("<I", donor_ms, lp0 + 0x18)[0]
    sec_ptr = struct.unpack_from("<Q", donor_ms, lp0 + 0x1C)[0]
    target_sec = min(max(0, int(active_sec)), sec_cnt - 1)

    pos_h4 = np.ones((total_verts, 4), dtype="<f2")
    pos_h4[:, 0:3] = verts.astype("<f2")
    pos_bytes = pos_h4.tobytes()

    uv_h2 = np.zeros((total_verts, 2), dtype="<f2")
    if uvs is not None and len(uvs) == total_verts:
        uv_flipped = uvs.copy().astype(np.float32)
        uv_flipped[:, 1] = 1.0 - uv_flipped[:, 1]
        uv_h2[:] = uv_flipped.astype("<f2")
    uv_bytes = uv_h2.tobytes()

    ts_bytes = encode_fb_tangentspace_vec(
        normals,
        flip_normals=flip_normals,
        verts=verts,
        uvs=uvs,
        faces=faces,
    )
    if vc_layers is not None and len(vc_layers) == total_verts:
        vc_arr = np.zeros((total_verts, 4), dtype=np.uint8)
        vc_arr[:, 0] = np.clip(vc_layers, 0, 7).astype(np.uint8)
        vc_bytes = vc_arr.tobytes()
    else:
        vc_bytes = bytes([int(vc_layer) & 0xFF, 0, 0, 0]) * total_verts
    color_bytes = b"\x00\x00\x00\xff" * total_verts

    def _build_section_vb(sp_off: int, nv_sec: int, is_dummy: bool):
        strides = [s for s in struct.unpack_from("<8H", donor_ms, sp_off + 0x0C0) if s > 0]
        elems = [struct.unpack_from("<4B", donor_ms, sp_off + 0x80 + e * 4) for e in range(len(strides))]
        sec_vb = bytearray()
        for st, el in zip(strides, elems):
            u, fmt = el[0], el[1]
            if is_dummy:
                sec_vb.extend(b"\x00" * (nv_sec * st))
                continue
            if u == 1:
                sec_vb.extend(pos_bytes)
            elif u in (33, 34):
                sec_vb.extend(uv_bytes)
            elif u == 51:
                sec_vb.extend(vc_bytes)
            elif u == 52:
                sec_vb.extend(ts_bytes)
            elif u in (30, 31):
                sec_vb.extend(color_bytes)
            else:
                sec_vb.extend(b"\x00" * (nv_sec * st))
        return bytes(sec_vb)

    ms = bytearray(donor_ms)
    # Pad culling bounds slightly (+0.25m) so frustum/LOD culling never clips or flashes the prop
    min_x, min_y, min_z = [float(x) - 0.25 for x in verts.min(axis=0)]
    max_x, max_y, max_z = [float(x) + 0.25 for x in verts.max(axis=0)]
    min_v = (min_x, min_y, min_z, 0.0)
    max_v = (max_x, max_y, max_z, 0.0)
    struct.pack_into("<4f4f", ms, 0x10, *min_v, *max_v)

    unified_vb = bytearray()
    unified_ib = bytearray()
    cur_start_idx = 0
    main_start_idx = 0
    main_vert_off = 0

    for s in range(sec_cnt):
        sp = sec_ptr + s * 0x180
        vert_off = len(unified_vb)
        if s == target_sec:
            main_start_idx = cur_start_idx
            main_vert_off = vert_off
            sec_vb = _build_section_vb(sp, total_verts, is_dummy=False)
            unified_vb.extend(sec_vb)
            ib_sec = faces.astype("<u2").tobytes()
            unified_ib.extend(ib_sec)
            struct.pack_into("<4I", ms, sp + 0x30, total_prims, cur_start_idx, vert_off, total_verts)
            struct.pack_into("<I", ms, sp + 0x64, vert_off)
            struct.pack_into("<I", ms, sp + 0x150, cur_start_idx)
            cur_start_idx += total_prims * 3
        else:
            sec_vb = _build_section_vb(sp, 3, is_dummy=True)
            unified_vb.extend(sec_vb)
            unified_ib.extend(struct.pack("<3H", 0, 0, 0))
            struct.pack_into("<4I", ms, sp + 0x30, 1, cur_start_idx, vert_off, 3)
            struct.pack_into("<I", ms, sp + 0x64, vert_off)
            struct.pack_into("<I", ms, sp + 0x150, cur_start_idx)
            cur_start_idx += 3
        # IMPORTANT: Section local AABB is at +0x160..+0x180 (NOT +0x15C, which is texel density float!)
        struct.pack_into("<4f4f", ms, sp + 0x160, *min_v, *max_v)

    vb_pad = (16 - (len(unified_vb) % 16)) % 16
    unified_vb.extend(b"\x00" * vb_pad)
    vb_size_field = len(unified_vb)

    ib_size_field = (len(unified_ib) + 3) & ~3
    if len(unified_ib) < ib_size_field:
        unified_ib.extend(b"\x00" * (ib_size_field - len(unified_ib)))

    new_chk = bytearray(unified_vb + unified_ib)
    chk_pad = (16 - (len(new_chk) % 16)) % 16
    new_chk.extend(b"\x00" * chk_pad)

    lod_ptrs = [p for p in struct.unpack_from("<6Q", ms, 0x30) if p != 0]
    struct.pack_into("<II", ms, lp0 + 0x68, ib_size_field, vb_size_field)

    # Preserve each LOD's own section header pointers (+0x18 relocations) while pointing all LODs to our VB/IB!
    for lpi in lod_ptrs[1:]:
        sc_i = struct.unpack_from("<I", ms, lpi + 0x18)[0]
        sptr_i = struct.unpack_from("<Q", ms, lpi + 0x1C)[0]
        struct.pack_into("<II", ms, lpi + 0x68, ib_size_field, vb_size_field)
        if sptr_i != 0:
            for s in range(sc_i):
                sp_i = sptr_i + s * 0x180
                # Both main mesh section (s == 0) and shadow caster section (strides=[8] pos-only at offset 0)
                # read directly from main_vert_off (0) and main_start_idx (0)!
                struct.pack_into("<4I", ms, sp_i + 0x30, total_prims, main_start_idx, main_vert_off, total_verts)
                struct.pack_into("<I", ms, sp_i + 0x64, main_vert_off)
                struct.pack_into("<I", ms, sp_i + 0x150, main_start_idx)
                struct.pack_into("<4f4f", ms, sp_i + 0x160, *min_v, *max_v)

    mc = bytearray(donor_mc_ebx)
    if len(mc) >= 20 + 172:
        struct.pack_into("<4f4f", mc, 20 + 140, *min_v, *max_v)

    return bytes(ms), bytes(mc), bytes(new_chk), min_v, max_v


def patch_shader_block_depot_rgb(
    sbd_bytes: bytes,
    rgb_float: tuple,
    has_custom_tex: bool = True,
    target_c_guid: bytes = None,
    other_c_guids: list = None,
) -> bytes:
    """
    Patches Frostbite `0x73312045` ShaderBlockDepot (`sbd`):
    1. `59ff5e88 <uint16 cnt1> <uint16 phash> <uint32 cnt2>` Vec3 parameter arrays at `idx + 12`:
       When `has_custom_tex=True`, sets all per-layer and LOD Albedo tint multipliers
       (`phash` in `0x1848, 0xBE3C, 0x14EE, 0xDCB0, 0x3A68, 0x1244, 0x9392`) to neutral `(0.75, 0.75, 0.75)`
       so dark/colored donors (`basictrampoline_00001` = `0.03` black, `snowman_00001` = orange, `logbench_00001` = brown)
       never tint custom textures black or orange!
    2. `7e814a8e 080067a4 08000000` (`Int[8]` `67a4` texture-array selector in `slalomflag`, `startline`, `finishline`):
       Zeros out all 8 ints so no layer ever samples `sg_gameplayobjects_array_c` (blue diagonal stripes)!
    3. Redirects any secondary `_c` texture EBX instance GUIDs (`other_c_guids`) to `target_c_guid` so all layers/LODs
       sample the custom BC7_SRGB texture!
    """
    sbd = bytearray(sbd_bytes)
    pat_vec3 = struct.pack("<I", 0x59FF5E88)
    r, g, b = (0.75, 0.75, 0.75) if has_custom_tex else [float(x) for x in rgb_float]
    tint_hashes = {0x1848, 0xBE3C, 0x14EE, 0xDCB0, 0x3A68, 0x1244, 0x9392}

    pos = 0
    while True:
        idx = sbd.find(pat_vec3, pos)
        if idx == -1:
            break
        if idx + 12 <= len(sbd):
            cnt1, phash, cnt2 = struct.unpack_from("<HHI", sbd, idx + 4)
            if 0 < cnt1 <= 16 and cnt1 == cnt2 and ( idx + 12 + cnt1 * 12 <= len(sbd) ):
                if phash in tint_hashes:
                    for k in range(cnt1):
                        struct.pack_into("<3f", sbd, idx + 12 + k * 12, r, g, b)
        pos = idx + 4

    # Disable sg_gameplayobjects_array_c blue diagonal stripe selector (67a4) in slalomflag / startline / finishline
    if has_custom_tex:
        pat_67a4 = bytes.fromhex("7e814a8e080067a408000000")
        idx_67a4 = sbd.find(pat_67a4)
        if idx_67a4 != -1 and idx_67a4 + 12 + 32 <= len(sbd):
            sbd[idx_67a4 + 12 : idx_67a4 + 12 + 32] = b"\x00" * 32

    # Redirect any secondary albedo _c texture references in sbd to target_c_guid
    if has_custom_tex and target_c_guid and len(target_c_guid) == 16 and other_c_guids:
        for og in other_c_guids:
            if og and len(og) == 16 and og != target_c_guid:
                sbd = bytearray(bytes(sbd).replace(og, target_c_guid))

    return bytes(sbd)


def render_fallback_thumbnail_png(verts: np.ndarray, norms: np.ndarray, faces: np.ndarray, avg_rgb: tuple, size: int = 512) -> Image.Image:
    """
    Software isometric 3D triangle rasterizer using PIL ImageDraw so every queued prop
    automatically gets a crisp 3D thumbnail photo even if the user doesn't click 'Take Thumbnail Photo'.
    """
    im = Image.new("RGBA", (size, size), (14, 19, 28, 255))
    draw = ImageDraw.Draw(im)

    # Subtle studio background gradient & grid lines
    for y in range(0, size, 4):
        c_val = int(14 + (y / size) * 18)
        draw.rectangle([0, y, size, y + 4], fill=(c_val, c_val + 5, c_val + 14, 255))

    if len(verts) == 0 or len(faces) == 0:
        return im

    # Decimate face list if huge so software rendering stays < 0.03s
    f_arr = faces if len(faces) <= 1500 else faces[:: max(1, len(faces) // 1500)]
    v = verts.astype(np.float32)
    bmin = v.min(axis=0)
    bmax = v.max(axis=0)
    center = 0.5 * (bmin + bmax)
    v_centered = v - center
    radius = max(float(np.linalg.norm(bmax - bmin)) * 0.52, 0.1)

    # Isometric camera rotation (yaw = 38 deg, pitch = 24 deg)
    yaw, pitch = math.radians(38.0), math.radians(24.0)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], dtype=np.float32)
    R = Rx @ Ry
    vp = v_centered @ R.T

    scale = (size * 0.40) / radius
    sx = (size * 0.5) + vp[:, 0] * scale
    sy_scr = (size * 0.54) - vp[:, 1] * scale
    sz = vp[:, 2]

    tri_v0 = v_centered[f_arr[:, 0]]
    tri_v1 = v_centered[f_arr[:, 1]]
    tri_v2 = v_centered[f_arr[:, 2]]
    fn = np.cross(tri_v1 - tri_v0, tri_v2 - tri_v0)
    fn_len = np.linalg.norm(fn, axis=1, keepdims=True)
    fn = np.where(fn_len > 1e-6, fn / fn_len, np.array([[0.0, 1.0, 0.0]], dtype=np.float32))

    light_dir = np.array([0.45, 0.78, 0.42], dtype=np.float32)
    light_dir /= np.linalg.norm(light_dir)
    intensity = np.clip(0.38 + 0.62 * np.abs(fn @ light_dir), 0.25, 1.05)

    tri_z = (sz[f_arr[:, 0]] + sz[f_arr[:, 1]] + sz[f_arr[:, 2]]) / 3.0
    order = np.argsort(tri_z)

    br, bg, bb = [max(0.22, min(0.95, float(c))) for c in avg_rgb]
    for idx in order:
        i0, i1, i2 = f_arr[idx]
        lit = float(intensity[idx])
        col = (
            int(min(255, br * lit * 255)),
            int(min(255, bg * lit * 255)),
            int(min(255, bb * lit * 255)),
            255,
        )
        pts = [(float(sx[i0]), float(sy_scr[i0])), (float(sx[i1]), float(sy_scr[i1])), (float(sx[i2]), float(sy_scr[i2]))]
        draw.polygon(pts, fill=col)

    # Subtle cyan accent border
    draw.rectangle([2, 2, size - 3, size - 3], outline=(56, 189, 248, 180), width=3)
    return im


def guid_to_le_bytes(guid_str: str) -> bytes:
    clean = guid_str.replace("-", "")
    return (
        bytes.fromhex(clean[0:8])[::-1]
        + bytes.fromhex(clean[8:12])[::-1]
        + bytes.fromhex(clean[12:16])[::-1]
        + bytes.fromhex(clean[16:32])
    )


def deterministic_guid_bytes(seed_str: str) -> bytes:
    u = uuid.uuid5(uuid.NAMESPACE_DNS, seed_str.lower())
    return guid_to_le_bytes(str(u))


def demux_riff(data: bytes):
    riff_pos = data.find(b"RIFF")
    if riff_pos > 0:
        data = data[riff_pos:]
    form = data[8:12]
    chunks = []
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        payload = bytearray(data[pos + 8 : pos + 8 + csz])
        chunks.append((cid, payload))
        pos += 8 + csz + (csz % 2)
    return form, chunks


def remux_riff(form: bytes, chunks: list) -> bytes:
    out = bytearray(b"RIFF\x00\x00\x00\x00" + form)
    for cid, payload in chunks:
        out += cid
        out += struct.pack("<I", len(payload))
        out += payload
        if len(payload) % 2 == 1:
            out += b"\x00"
    struct.pack_into("<I", out, 4, len(out) - 8)
    return bytes(out)


def build_custom_item_ebx(
    template_bytes: bytes,
    item_path: str,
    key_str: str,
    display_name: str,
    bundle_stem: str,
    file_guid_override: bytes = None,
    inst_guid_override: bytes = None,
    thumb_sm_import: bytes = None,
    thumb_lg_import: bytes = None,
):
    form, chunks = demux_riff(template_bytes)
    cmap = {cid: payload for cid, payload in chunks}
    ebxd = cmap[b"EBXD"]
    efix = cmap[b"EFIX"]

    file_guid = file_guid_override or deterministic_guid_bytes(f"file:{item_path}")
    inst0_guid = inst_guid_override or deterministic_guid_bytes(f"inst0:{item_path}")
    inst1_guid = deterministic_guid_bytes(f"inst1:{item_path}")
    inst2_guid = deterministic_guid_bytes(f"inst2:{item_path}")

    ebxd[12:28] = inst0_guid
    ebxd[124:140] = inst1_guid
    ebxd[220:236] = inst2_guid
    struct.pack_into("<I", ebxd, 100, fnv1_32_lower(key_str))

    bundle_str = f"{bundle_stem}_bundle"
    prefab_str = f"{bundle_stem}_ecsPrefab"

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

    new_str_table = s1 + s2 + s3 + s4 + s5
    ebxd[412:] = new_str_table

    struct.pack_into("<Q", ebxd, 52, off1 - 52)
    struct.pack_into("<Q", ebxd, 84, off2 - 84)
    struct.pack_into("<Q", ebxd, 260, off3 - 260)
    struct.pack_into("<Q", ebxd, 324, off4 - 324)
    struct.pack_into("<Q", ebxd, 340, off5 - 340)

    cat_map = {
        "bkfunboxes": 0,
        "bkramps": 1,
        "bkrails": 2,
        "bkpropsconstruction": 5,
        "bkpropsevent": 6,
        "bkpropsfurniture": 7,
        "bkpropsindustrial": 8,
        "bkprops": 10,
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

    efix[0:16] = file_guid

    # Preserve non-thumbnail imports (0x140 = 1abf3e16..., 0x98 = ac50bfe2..., 0x188 = f599078c...)
    imp_140 = bytes(efix[148:180])
    imp_098 = bytes(efix[212:244])
    imp_188 = bytes(efix[244:276])
    imp_040 = (
        bytes(thumb_sm_import)
        if (thumb_sm_import and len(thumb_sm_import) == 32)
        else bytes(efix[180:212])
    )
    imp_050 = (
        bytes(thumb_lg_import)
        if (thumb_lg_import and len(thumb_lg_import) == 32)
        else bytes(efix[276:308])
    )

    # Sort all 5 EFIX imports by (FileGuid, InstanceGuid) and update EBXD import indices (76, 92, 164, 332, 404)
    tagged_imports = [
        (imp_040, 76),   # 0x40: Thumbnail (256x256)
        (imp_050, 92),   # 0x50: ThumbnailLarge (768x768)
        (imp_098, 164),  # 0x98: Base item type ref
        (imp_140, 332),  # 0x140: Bundle asset ref
        (imp_188, 404),  # 0x188: Quick Drop placement tag ref
    ]
    tagged_imports.sort(key=lambda pair: pair[0])
    for sorted_idx, (imp_bytes, ebxd_off) in enumerate(tagged_imports):
        efix[148 + sorted_idx * 32 : 148 + (sorted_idx + 1) * 32] = imp_bytes
        struct.pack_into("<Q", ebxd, ebxd_off, (sorted_idx * 2) + 1)

    rebuilt = remux_riff(form, chunks)
    return rebuilt, file_guid, inst0_guid


def build_patched_imc_ebx(imc_template: bytes, added_guid_pairs: list) -> bytes:
    form, chunks = demux_riff(imc_template)
    cmap = {cid: bytearray(payload) for cid, payload in chunks}
    ebxd = cmap[b"EBXD"]
    efix = cmap[b"EFIX"]
    ebxx = cmap[b"EBXX"]

    vanilla_count = 1221
    existing_pairs = set()
    for i in range(vanilla_count):
        off = 80 + i * 32
        existing_pairs.add((bytes(efix[off : off + 16]), bytes(efix[off + 16 : off + 32])))

    deduped_pairs = []
    for fg, ig in added_guid_pairs:
        pair = (bytes(fg), bytes(ig))
        if pair not in existing_pairs:
            existing_pairs.add(pair)
            deduped_pairs.append(pair)

    n_add = len(deduped_pairs)
    new_count = vanilla_count + n_add

    # 1. Rebuild EBXD
    struct.pack_into("<I", ebxd, 112, new_count)
    vanilla_ptrs_end = 116 + vanilla_count * 8  # 9884 (12B header + 104B offset + 1221 * 8)
    rebuilt_ebxd = bytearray(ebxd[:vanilla_ptrs_end])

    # Append new item pointers: each pointer is 8 bytes with ptr_val in low 32 bits, 0 in high 32 bits
    for i in range(n_add):
        item_idx = vanilla_count + i
        ptr_val = (item_idx * 2) + 1
        rebuilt_ebxd += struct.pack("<Q", ptr_val)

    str1_off = len(rebuilt_ebxd)
    str_data = b"Items/ItemMasterCollection\x00ItemMasterCollection\x00"
    rebuilt_ebxd += str_data
    str2_off = str1_off + len(b"Items/ItemMasterCollection\x00")

    # Update string relative offsets at offset 52 and 60
    struct.pack_into("<I", rebuilt_ebxd, 52, str1_off - 52)
    struct.pack_into("<I", rebuilt_ebxd, 60, str2_off - 60)

    # 2. Rebuild EFIX
    struct.pack_into("<I", efix, 76, new_count)
    vanilla_imports_end = 80 + (vanilla_count * 32)
    new_imports = b"".join(fg + ig for fg, ig in deduped_pairs)

    tpl_import_count = struct.unpack_from("<I", efix, 76)[0]
    noble_imports_end = 80 + (tpl_import_count * 32)
    noble_fixups_start = noble_imports_end + 4
    vanilla_fixups_end = noble_fixups_start + (1221 * 4)

    new_fixups_count = 1221 + n_add
    new_fixups = b"".join(struct.pack("<I", 9872 + (i * 8)) for i in range(n_add))

    trailer = struct.pack("<IIIII", 0, 64, str1_off - 12, str1_off - 12, 0)

    rebuilt_efix = bytearray()
    rebuilt_efix += efix[:vanilla_imports_end]
    rebuilt_efix += new_imports
    rebuilt_efix += struct.pack("<I", new_fixups_count)
    rebuilt_efix += efix[noble_fixups_start:vanilla_fixups_end]
    rebuilt_efix += new_fixups
    rebuilt_efix += trailer

    cmap[b"EBXD"] = rebuilt_ebxd
    cmap[b"EFIX"] = rebuilt_efix
    struct.pack_into("<I", ebxx, 12, new_count)
    cmap[b"EBXX"] = ebxx

    out_chunks = [(cid, cmap[cid]) for cid, _ in chunks]
    return remux_riff(form, out_chunks)



def write_fbmod_v6_full(out_path: Path, title: str, author: str, version: str, desc: str, records: list):
    body = bytearray()
    payloads = []
    fast_mode = _is_fast_pipeline_mode()
    cas_comp_dir = None
    if fast_mode:
        try:
            cas_comp_dir = _get_compiler_cache_root() / "cas_comp_v1"
            cas_comp_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            cas_comp_dir = None

    for i, rec in enumerate(records):
        rtype = rec["rtype"]
        raw = rec["raw"]
        comp = None
        sha1 = None
        if (
            fast_mode
            and isinstance(rec.get("_cached_comp"), bytes)
            and isinstance(rec.get("_cached_sha1"), bytes)
            and len(rec["_cached_sha1"]) == 20
            and rec.get("_cached_raw_len") == len(raw)
        ):
            comp = rec["_cached_comp"]
            sha1 = rec["_cached_sha1"]
        elif fast_mode and cas_comp_dir is not None and len(raw) >= 4096:
            raw_sha1_hex = hashlib.sha1(raw).hexdigest()
            cc_file = cas_comp_dir / f"{raw_sha1_hex}_{len(raw)}.bin"
            if cc_file.is_file():
                try:
                    c_bytes = cc_file.read_bytes()
                    if len(c_bytes) > 20:
                        sha1 = c_bytes[:20]
                        comp = c_bytes[20:]
                except Exception:
                    comp = None
            if comp is None:
                comp = cas_compress_uncompressed(raw)
                sha1 = hashlib.sha1(comp).digest()
                try:
                    cc_file.write_bytes(sha1 + comp)
                except Exception:
                    pass
        if comp is None or sha1 is None:
            comp = cas_compress_uncompressed(raw)
            sha1 = hashlib.sha1(comp).digest()
        rec["_cached_comp"] = comp
        rec["_cached_sha1"] = sha1
        rec["_cached_raw_len"] = len(raw)
        payloads.append(comp)

        body += struct.pack("<Bi", rtype, i)
        body += rec["name"].lower().encode("utf-8") + b"\x00"
        body += sha1
        body += struct.pack("<Q", len(raw))
        body += b"\x08" if rec.get("is_added", False) else b"\x00"
        body += struct.pack("<I", 0)
        body += rec.get("tag", "").encode("utf-8") + b"\x00"

        bundles = rec.get("bundles", [])
        body += struct.pack("<I", len(bundles))
        for b_hash in bundles:
            body += struct.pack("<I", b_hash & 0xFFFFFFFF)

        if rtype == 2:
            body += struct.pack("<IQI", rec["res_type"], rec["res_rid"], len(rec["res_meta"])) + rec["res_meta"]
        elif rtype == 3:
            body += struct.pack("<IIIIIII", 0, 0, 0, len(raw), rec.get("res_hash", 0) & 0xFFFFFFFF, 0, 0)

    header = bytearray(b"FROSTY\x00\x01")
    header += struct.pack("<I", 6)
    data_off_pos = len(header)
    header += struct.pack("<Q", 0)
    header += struct.pack("<I", len(records))
    prof = b"Skate"
    header += struct.pack("<B", len(prof)) + prof
    header += struct.pack("<I", 625200)
    for s in (title, author, "Custom Props & Ramps", version, desc, ""):
        header += s.encode("utf-8") + b"\x00"
    header += struct.pack("<I", 5 + len(records))
    for emb_name in ("Icon", "Screenshot0", "Screenshot1", "Screenshot2", "Screenshot3"):
        header += b"\x00\xff\xff\xff\xff" + emb_name.encode("utf-8") + b"\x00" + b"\x00" * 4

    full_hdr = header + body
    struct.pack_into("<Q", full_hdr, data_off_pos, len(full_hdr))

    table = bytearray()
    cur_off = 0
    for p in payloads:
        table += struct.pack("<QQ", cur_off, len(p))
        cur_off += len(p)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(full_hdr) + bytes(table) + b"".join(payloads))



def deterministic_u64(seed_str: str) -> int:
    h = hashlib.sha256(seed_str.encode("utf-8")).digest()
    return struct.unpack("<Q", h[:8])[0] | 1


def mph_hash(key_bytes: bytes, seed: int = 0x811C9DC5) -> int:
    ecx = seed & 0xFFFFFFFF
    for b in key_bytes:
        sb = b if b < 128 else b - 256
        ecx = (ecx * 0x01000193) & 0xFFFFFFFF
        ecx = (ecx ^ (sb & 0xFFFFFFFF)) & 0xFFFFFFFF
    return ecx % 0x01000193


def build_mph(items: list, key_fn):
    n = len(items)
    if n == 0:
        return [], []
    buckets = [[] for _ in range(n)]
    for item in items:
        k = key_fn(item)
        buckets[mph_hash(k, 0x811C9DC5) % n].append((k, item))
    buckets_with_idx = sorted(enumerate(buckets), key=lambda x: len(x[1]), reverse=True)
    table = [-1] * n
    used = [False] * n
    ordered = [None] * n
    for b_idx, b_items in buckets_with_idx:
        if len(b_items) > 1:
            d = 1
            while True:
                slots = [mph_hash(k, d) % n for k, _ in b_items]
                if len(set(slots)) == len(slots) and all(not used[s] for s in slots):
                    table[b_idx] = d
                    for s, (_, item) in zip(slots, b_items):
                        used[s] = True
                        ordered[s] = item
                    break
                d += 1
        elif len(b_items) == 1:
            free_slot = used.index(False)
            table[b_idx] = ~free_slot
            used[free_slot] = True
            ordered[free_slot] = b_items[0][1]
    return table, ordered


def parse_bundle_desc(desc: bytes) -> list:
    w0, w1, w2, n_streams, w4, w5, w6, w7, w8 = struct.unpack_from(">9I", desc, 0)
    flags = desc[w2 : w2 + n_streams]
    pos = w4
    streams = []
    is_patch = ic = cas_idx = 0
    for i in range(n_streams):
        fl = flags[i]
        if fl == 1:
            hdr = struct.unpack_from(">I", desc, pos)[0]
            pos += 4
            is_patch = 1 if (hdr & 0x00FF0000) else 0
            cas_idx = hdr & 0xFF
        elif fl != 0:
            hdr = struct.unpack_from(">Q", desc, pos)[0]
            pos += 8
            is_patch = 1 if (hdr & 0x00FF000000000000) else 0
            ic = (hdr >> 16) & 0xFFFFFFFF
            cas_idx = hdr & 0xFFFF
        off, sz = struct.unpack_from(">II", desc, pos)
        pos += 8
        streams.append((is_patch, ic, cas_idx, off, sz))
    return streams


def serialize_bundle_desc(streams: list) -> bytes:
    n_streams = len(streams)
    flags = bytearray()
    sdata = bytearray()
    prev = None
    for i, (is_patch, ic, cas_idx, off, sz) in enumerate(streams):
        if prev != (is_patch, ic, cas_idx):
            flags.append(0x84 if i == 0 else 0x80)
            hdr = ((1 if is_patch else 0) << 48) | ((ic & 0xFFFFFFFF) << 16) | (cas_idx & 0xFFFF)
            sdata += struct.pack(">QII", hdr, off, sz)
        else:
            flags.append(0)
            sdata += struct.pack(">II", off, sz)
        prev = (is_patch, ic, cas_idx)
    w4 = 0x24
    w2 = w4 + len(sdata)
    hdr9 = struct.pack(">9I", 0, 0, w2, n_streams, w4, w4, w4, 0, n_streams)
    return hdr9 + bytes(sdata) + bytes(flags)


def parse_sbdata_full(sb_raw: bytes):
    b = sb_raw[4:]
    magic, tot, n_ebx, n_res, n_chk, str_off, meta_off, meta_sz = struct.unpack_from("<8I", b, 0)
    sha1s = [b[32 + i * 20 : 32 + (i + 1) * 20] for i in range(tot)]
    pos = 32 + tot * 20
    ebxs = []
    for i in range(n_ebx):
        noff, orig_sz = struct.unpack_from("<II", b, pos)
        pos += 8
        s_end = b.index(0, str_off + noff)
        name = b[str_off + noff : s_end].decode("ascii")
        ebxs.append({"name": name, "orig_size": orig_sz, "sha1": sha1s[i]})
    ress = []
    for i in range(n_res):
        noff, orig_sz = struct.unpack_from("<II", b, pos)
        pos += 8
        s_end = b.index(0, str_off + noff)
        name = b[str_off + noff : s_end].decode("ascii")
        ress.append({"name": name, "orig_size": orig_sz, "sha1": sha1s[n_ebx + i]})
    for i in range(n_res):
        ress[i]["res_type"] = struct.unpack_from("<I", b, pos)[0]
        pos += 4
    for i in range(n_res):
        ress[i]["res_meta"] = b[pos : pos + 16]
        pos += 16
    for i in range(n_res):
        ress[i]["res_rid"] = struct.unpack_from("<Q", b, pos)[0]
        pos += 8
    chks = []
    for i in range(n_chk):
        g = b[pos : pos + 16]
        lo, hi = struct.unpack_from("<II", b, pos + 16)
        pos += 24
        chks.append({"guid_raw": g, "log_off": lo, "log_sz": hi, "sha1": sha1s[n_ebx + n_res + i]})
    cmeta = b[meta_off : meta_off + meta_sz]
    return magic, ebxs, ress, chks, cmeta


def serialize_sbdata_full(magic: int, ebxs: list, ress: list, chks: list, cmeta: bytes) -> bytes:
    n_ebx, n_res, n_chk = len(ebxs), len(ress), len(chks)
    tot = n_ebx + n_res + n_chk
    sha1_part = (
        b"".join(e["sha1"] for e in ebxs)
        + b"".join(r["sha1"] for r in ress)
        + b"".join(c["sha1"] for c in chks)
    )
    str_buf = bytearray()
    ebx_part = bytearray()
    for e in ebxs:
        noff = len(str_buf)
        str_buf += e["name"].encode("ascii") + b"\x00"
        ebx_part += struct.pack("<II", noff, e["orig_size"])
    res_part = bytearray()
    for r in ress:
        noff = len(str_buf)
        str_buf += r["name"].encode("ascii") + b"\x00"
        res_part += struct.pack("<II", noff, r["orig_size"])
    res_types = b"".join(struct.pack("<I", r["res_type"]) for r in ress)
    res_metas = b"".join(r["res_meta"] for r in ress)
    res_rids = b"".join(struct.pack("<Q", r["res_rid"]) for r in ress)
    chk_part = b"".join(
        c["guid_raw"] + struct.pack("<II", c["log_off"], c["log_sz"]) for c in chks
    )
    str_off = (
        32
        + len(sha1_part)
        + len(ebx_part)
        + len(res_part)
        + len(res_types)
        + len(res_metas)
        + len(res_rids)
        + len(chk_part)
    )
    meta_off = str_off + len(str_buf)
    meta_sz = len(cmeta)
    hdr = struct.pack("<8I", magic, tot, n_ebx, n_res, n_chk, str_off, meta_off, meta_sz)
    body = (
        hdr
        + sha1_part
        + bytes(ebx_part)
        + bytes(res_part)
        + res_types
        + res_metas
        + res_rids
        + chk_part
        + bytes(str_buf)
        + cmeta
    )
    return struct.pack(">I", len(body)) + body


def parse_cmeta_h64s(cmeta: bytes) -> list:
    pos = 11
    while True:
        b = cmeta[pos]
        pos += 1
        if not (b & 0x80):
            break
    res = []
    while pos < len(cmeta) - 1:
        pos += 1
        shift = 0
        esz = 0
        while True:
            b = cmeta[pos]
            pos += 1
            esz |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        ent = cmeta[pos : pos + esz]
        h64 = struct.unpack_from("<Q", ent, 5)[0] if ent.startswith(b"\th64\x00") else None
        res.append((pos, esz, h64))
        pos += esz
    return res


def load_template_assets():
    """
    Loads and decompresses all template assets from Bundle 39
    (`own_bkprops_generic_industrialpatiotile_00001_ecs.2558150692_bundle`)
    in `Data/Win32/buildkititems.toc` and thumbnail templates from `Data/Win32/items.toc`.
    """
    base_bk_path = GAME_DIR / "Data" / "Win32" / "buildkititems.toc"
    if not base_bk_path.exists():
        env_gd = os.environ.get("RESKATE_GAME_DIR", "").strip()
        if env_gd:
            base_bk_path = Path(env_gd) / "Data" / "Win32" / "buildkititems.toc"

    bki_toc = base_bk_path.read_bytes()
    base = 0x22C
    b_off = struct.unpack_from(">I", bki_toc, base + 0x04)[0]
    _, tpl_type_sz, tpl_desc_off = struct.unpack_from(
        ">IIQ", bki_toc, base + b_off + TEMPLATE_BUNDLE_IDX * 16
    )
    tpl_desc = bki_toc[base + tpl_desc_off : base + tpl_desc_off + (tpl_type_sz & 0x3FFFFFFF)]
    tpl_streams = parse_bundle_desc(tpl_desc)

    def _dstream(s):
        _is_p, ic, c_idx, off, sz = s
        pkg = 0 if ic == 3162564249 else 1
        return decompress_cas(pkg, c_idx, off, sz)

    s0 = tpl_streams[0]
    tpl_sb_raw = read_cas_raw(0 if s0[1] == 3162564249 else 1, s0[2], s0[3], s0[4])
    tpl_magic, tpl_ebxs, tpl_ress, tpl_chks, tpl_cmeta = parse_sbdata_full(tpl_sb_raw)

    n_e = len(tpl_ebxs)
    tpl = {
        "streams": tpl_streams,
        "magic": tpl_magic,
        "ebxs": tpl_ebxs,
        "ress": tpl_ress,
        "chks": tpl_chks,
        "cmeta": tpl_cmeta,
        "ebx25": _dstream(tpl_streams[1 + 25]),
        "ebx27": _dstream(tpl_streams[1 + 27]),
        "ebx28": _dstream(tpl_streams[1 + 28]),
        "ebx29": _dstream(tpl_streams[1 + 29]),
        "ebx31": _dstream(tpl_streams[1 + 31]),
        "ebx32": _dstream(tpl_streams[1 + 32]),
        "ebx34": _dstream(tpl_streams[1 + 34]),
        "ebx35": _dstream(tpl_streams[1 + 35]),
        "ebx37": _dstream(tpl_streams[1 + 37]),
        "ebx38": _dstream(tpl_streams[1 + 38]),
        "res8": _dstream(tpl_streams[1 + n_e + 8]),
        "res9": _dstream(tpl_streams[1 + n_e + 9]),
        "res10": _dstream(tpl_streams[1 + n_e + 10]),
        "res11": _dstream(tpl_streams[1 + n_e + 11]),
        "res16": _dstream(tpl_streams[1 + n_e + 16]),
        "res17": _dstream(tpl_streams[1 + n_e + 17]),
        "res18": _dstream(tpl_streams[1 + n_e + 18]),
        "ebx24": _dstream(tpl_streams[1 + 24]),
        "ebx26": _dstream(tpl_streams[1 + 26]),
        "ebx39": _dstream(tpl_streams[1 + 39]),
        "ebx77": _dstream(tpl_streams[1 + 77]),
        "ebx78": _dstream(tpl_streams[1 + 78]),
        "ebx79": _dstream(tpl_streams[1 + 79]),
        "ebx81": _dstream(tpl_streams[1 + 81]),
        "ebx82": _dstream(tpl_streams[1 + 82]),
    }

    # Also load Bundle 94's 8-hull Placement Proxy prefab templates (EBX[114, 118, 121])
    try:
        _, b94_type_sz, b94_desc_off = struct.unpack_from(">IIQ", bki_toc, base + b_off + 94 * 16)
        b94_desc = bki_toc[base + b94_desc_off : base + b94_desc_off + (b94_type_sz & 0x3FFFFFFF)]
        b94_streams = parse_bundle_desc(b94_desc)
        s94_0 = b94_streams[0]
        b94_sb_raw = read_cas_raw(0 if s94_0[1] == 3162564249 else 1, s94_0[2], s94_0[3], s94_0[4])
        _, b94_ebxs, _, _, _ = parse_sbdata_full(b94_sb_raw)
        tpl["b94_ebx114_info"] = b94_ebxs[114]
        tpl["b94_ebx114_stream"] = b94_streams[1 + 114]
        tpl["b94_ebx118"] = _dstream(b94_streams[1 + 118])
        tpl["b94_ebx121"] = _dstream(b94_streams[1 + 121])
    except Exception as b94_err:
        print(f"  [!] Warning: Could not load Bundle 94 8-hull placement proxy templates: {b94_err}")

    # Also load Bundle 421 (HockeyNet) multi-section MeshSet (Sec[0]=0x771 Opaque + Sec[1]=0x273 Cutout)
    try:
        _, b421_type_sz, b421_desc_off = struct.unpack_from(">IIQ", bki_toc, base + b_off + 421 * 16)
        b421_desc = bki_toc[base + b421_desc_off : base + b421_desc_off + (b421_type_sz & 0x3FFFFFFF)]
        b421_streams = parse_bundle_desc(b421_desc)
        s421_0 = b421_streams[0]
        b421_sb_raw = read_cas_raw(0 if s421_0[1] == 3162564249 else 1, s421_0[2], s421_0[3], s421_0[4])
        _, b421_ebxs, b421_ress, _, _ = parse_sbdata_full(b421_sb_raw)
        for ri421, r421 in enumerate(b421_ress):
            if r421["res_type"] == 0x49B156D4:
                tpl["b421_ms"] = _dstream(b421_streams[1 + len(b421_ebxs) + ri421])
                tpl["b421_ms_meta"] = r421["res_meta"]
                break
    except Exception as b421_err:
        print(f"  [!] Warning: Could not load Bundle 421 HockeyNet multi-section MeshSet template: {b421_err}")


    # Also load thumbnail templates from Data/Win32/items.toc (bundle 1908 cas_main_sharedbundle)
    base_items_path = GAME_DIR / "Data" / "Win32" / "items.toc"
    if not base_items_path.exists():
        env_gd = os.environ.get("RESKATE_GAME_DIR", "").strip()
        if env_gd:
            base_items_path = Path(env_gd) / "Data" / "Win32" / "items.toc"
    it_toc = base_items_path.read_bytes()
    ib_off = struct.unpack_from(">I", it_toc, base + 0x04)[0]
    _, it_type_sz, it_desc_off = struct.unpack_from(">IIQ", it_toc, base + ib_off + 1908 * 16)
    it_streams = parse_bundle_desc(
        it_toc[base + it_desc_off : base + it_desc_off + (it_type_sz & 0x3FFFFFFF)]
    )
    is0 = it_streams[0]
    it_sb_raw = read_cas_raw(0 if is0[1] == 3162564249 else 1, is0[2], is0[3], is0[4])
    _, it_ebxs, it_ress, _, _ = parse_sbdata_full(it_sb_raw)

    it_e_map = {e["name"]: i for i, e in enumerate(it_ebxs)}
    it_r_map = {r["name"]: i for i, r in enumerate(it_ress)}

    sm_tool_nm = "thumbnail/tool/own_bkprops_generic_industrialpatiotile_00001"
    lg_tool_nm = "thumbnail/tool/own_bkprops_generic_industrialpatiotile_00001_lrg"
    sm_cdn_nm = "thumbnail/cdn/img_own_bkprops_generic_industrialpatiotile_00001"
    lg_cdn_nm = "thumbnail/cdn/img_own_bkprops_generic_industrialpatiotile_00001_lrg"

    tpl["thumb_tool_sm_ebx"] = _dstream(it_streams[1 + it_e_map[sm_tool_nm]])
    tpl["thumb_tool_lg_ebx"] = _dstream(it_streams[1 + it_e_map[lg_tool_nm]])
    tpl["thumb_cdn_sm_ebx"] = _dstream(it_streams[1 + it_e_map[sm_cdn_nm]])
    tpl["thumb_cdn_lg_ebx"] = _dstream(it_streams[1 + it_e_map[lg_cdn_nm]])
    tpl["thumb_tool_sm_res"] = _dstream(it_streams[1 + len(it_ebxs) + it_r_map[sm_tool_nm]])
    tpl["thumb_tool_lg_res"] = _dstream(it_streams[1 + len(it_ebxs) + it_r_map[lg_tool_nm]])
    tpl["thumb_tool_sm_meta"] = it_ress[it_r_map[sm_tool_nm]]["res_meta"]
    tpl["thumb_tool_lg_meta"] = it_ress[it_r_map[lg_tool_nm]]["res_meta"]

    it_e_low = {e["name"].lower(): i for i, e in enumerate(it_ebxs)}
    if "items/itemmastercollection" in it_e_low:
        tpl["imc_template_ebx"] = _dstream(it_streams[1 + it_e_low["items/itemmastercollection"]])

    return tpl


def patch_ebx_string_inplace(ed: bytearray, s_off: int, s_max_len: int, new_str: str):
    b = new_str.encode("ascii")
    if len(b) > s_max_len:
        # If this string is at the end of EBXD (no non-zero bytes after s_off + s_max_len + 1), we can safely extend EBXD
        tail = ed[s_off + s_max_len + 1 :]
        if not any(tail):
            ed[s_off:] = b + b"\x00"
            return
        raise ValueError(f"EBX string '{new_str}' ({len(b)}B) exceeds max template length {s_max_len}B")
    ed[s_off : s_off + s_max_len + 1] = b + b"\x00" * (s_max_len + 1 - len(b))


def frostbite_fnv1_64_lower(s: str) -> int:
    """Frostbite 64-bit DJB2-XOR string hash (initial=5381, mul=33, xor=lowercase_ascii)."""
    h = 5381
    for b in s.lower().encode("ascii"):
        h = ((h * 33) & 0xFFFFFFFFFFFFFFFF) ^ b
    return h


_B16_VFX_BUNDLE_CACHE: dict | None = None


def _load_b16_vfx_bundle_assets() -> dict | None:
    """Lazy-loads Bundle[16] VFX/audio bundle metadata, CAS stream tuples, and decompressed EBX templates (EBX[21], EBX[24])."""
    global _B16_VFX_BUNDLE_CACHE
    if _B16_VFX_BUNDLE_CACHE is not None:
        return _B16_VFX_BUNDLE_CACHE
    try:
        base_bk_path = GAME_DIR / "Data" / "Win32" / "buildkititems.toc"
        if not base_bk_path.exists():
            env_gd = os.environ.get("RESKATE_GAME_DIR", "").strip()
            if env_gd:
                base_bk_path = Path(env_gd) / "Data" / "Win32" / "buildkititems.toc"
        bki_toc = base_bk_path.read_bytes()
        base_off = 0x22C
        b_off = struct.unpack_from(">I", bki_toc, base_off + 0x04)[0]
        _, b16_tsz, b16_doff = struct.unpack_from(">IIQ", bki_toc, base_off + b_off + 16 * 16)
        b16_streams = list(parse_bundle_desc(bki_toc[base_off + b16_doff : base_off + b16_doff + (b16_tsz & 0x3FFFFFFF)]))
        s16_0 = b16_streams[0]
        b16_sb = read_cas_raw(0 if s16_0[1] == 3162564249 else 1, s16_0[2], s16_0[3], s16_0[4])
        _, b16_ebxs, b16_ress, b16_chks, b16_cmeta = parse_sbdata_full(b16_sb)
        b16_cmeta_ents = parse_cmeta_h64s(b16_cmeta)

        ebx_records = []
        for e_idx in (21, 24):
            if e_idx < len(b16_ebxs):
                st = b16_streams[1 + e_idx]
                e_raw = decompress_cas(0 if st[1] == 3162564249 else 1, st[2], st[3], st[4])
                ebx_records.append({
                    "name": b16_ebxs[e_idx]["name"],
                    "raw": e_raw,
                })

        _B16_VFX_BUNDLE_CACHE = {
            "streams": b16_streams,
            "ebxs": b16_ebxs,
            "ress": b16_ress,
            "chks": b16_chks,
            "cmeta": b16_cmeta,
            "cmeta_ents": b16_cmeta_ents,
            "ebx_records": ebx_records,
        }
        return _B16_VFX_BUNDLE_CACHE
    except Exception as ex:
        print(f"  [!] Warning: Could not load Bundle 16 VFX cache: {ex}")
        return None


def build_custom_buildkititems_toc(mod_dir: Path, prop_specs: list, tpl: dict, bundle_stem_base: str = "items/customprops"):
    """
    Build a compliant Frostbite Win32/buildkititems.toc + append bundle sbdata manifests
    into cas_01.cas inside mod_dir using Template Bundle 39 (`industrialpatiotile_00001`)
    so ReSkate.dll's mod merger merges all standalone custom prop bundles with zero errors.
    """
    tpl_magic = tpl["magic"]
    tpl_ebxs = tpl["ebxs"]
    tpl_ress = tpl["ress"]
    tpl_chks = tpl["chks"]
    tpl_cmeta = tpl["cmeta"]
    tpl_streams = tpl["streams"]
    tpl_cmeta_h64s = parse_cmeta_h64s(tpl_cmeta)

    items_pl = (mod_dir / "Win32" / "items.toc").read_bytes()[556:]
    cas_path = mod_dir / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas"
    cas_bytes = bytearray(cas_path.read_bytes())

    iw = struct.unpack_from(">12I", items_pl, 0)
    _, itype_sz, idesc_off = struct.unpack_from(">IIQ", items_pl, iw[1])
    istreams = parse_bundle_desc(items_pl[idesc_off : idesc_off + (itype_sz & 0x3FFFFFFF)])
    isb_off, isb_sz = istreams[0][3], istreams[0][4]
    imagic, iebxs, iress, ichks, icmeta = parse_sbdata_full(bytes(cas_bytes[isb_off : isb_off + isb_sz]))
    icmeta_h64s = parse_cmeta_h64s(icmeta)

    ebx_by_name = {
        e["name"]: (e, istreams[1 + i])
        for i, e in enumerate(iebxs)
        if istreams[1 + i][0] == 1
    }
    res_by_name = {
        r["name"]: (r, istreams[1 + len(iebxs) + i])
        for i, r in enumerate(iress)
        if istreams[1 + len(iebxs) + i][0] == 1
    }
    chk_by_guid = {
        c["guid_raw"]: (c, icmeta_h64s[i][2], istreams[1 + len(iebxs) + len(iress) + i])
        for i, c in enumerate(ichks)
        if istreams[1 + len(iebxs) + len(iress) + i][0] == 1
    }

    bundles_to_write = []
    chunks_to_write = []
    b16_vfx_cache = _load_b16_vfx_bundle_assets() if any(s.get("has_vfx_bundle_assets") for s in prop_specs) else None

    for spec in prop_specs:
        pk = spec["prop_key"]
        b_ebxs = copy.deepcopy(tpl_ebxs)
        b_ress = copy.deepcopy(tpl_ress)
        b_chks = copy.deepcopy(tpl_chks)
        b_cmeta = bytearray(tpl_cmeta)
        b_streams = list(tpl_streams)

        # Wire per-prop standalone EBX slots in Bundle 39
        ebx_slot_map = [
            (25, f"items/customprops/{pk}_c"),
            (27, f"items/customprops/{pk}_mesh"),
            (28, f"items/customprops/{pk}_mc"),
            (29, f"items/customprops/{pk}_outl"),
            (31, f"items/customprops/{pk}_physics"),
            (32, f"items/customprops/{pk}_pphys"),
            (34, f"items/customprops/{pk}_ecsprefab"),
            (35, f"items/customprops/{pk}_ecsprefab.nopolicy"),
            (37, f"items/customprops/{pk}_bundle_networkregistry_win32"),
            (38, f"items/customprops/{pk}_bundle/meshvariationdb_win32"),
        ]
        if spec.get("norm_chunk_guid_raw"):
            ebx_slot_map.append((26, f"items/customprops/{pk}_n"))
        if spec.get("msk_chunk_guid_raw"):
            ebx_slot_map.append((24, f"items/customprops/{pk}_msk"))
        for idx, e_name in ebx_slot_map:
            if e_name.lower() in ebx_by_name:
                e_info, s_info = ebx_by_name[e_name.lower()]
                b_ebxs[idx] = copy.deepcopy(e_info)
                b_streams[1 + idx] = s_info
            else:
                raise RuntimeError(f"Missing standalone EBX '{e_name.lower()}' in items.toc for prop '{pk}'!")

        if spec.get("use_b94_8hull_pphys") and "b94_ebx114_info" in tpl and "b94_ebx114_stream" in tpl:
            b_ebxs[33] = copy.deepcopy(tpl["b94_ebx114_info"])
            b_streams[1 + 33] = tpl["b94_ebx114_stream"]

        # Optional per-prop dynamic light EffectBlueprint EBX or 8-hull _pphys_1..7 EBXs appended to the bundle's EBX list
        extra_ebxs = spec.get("extra_bundle_ebxs", [])
        for extra_name in extra_ebxs:
            if extra_name.lower() in ebx_by_name:
                e_info, s_info = ebx_by_name[extra_name.lower()]
                b_ebxs.append(copy.deepcopy(e_info))
                b_streams.insert(1 + len(b_ebxs) - 1, s_info)

        # Wire per-prop standalone RES slots in Bundle 39
        res_slot_map = [
            (9, f"items/customprops/{pk}_c"),
            (11, f"items/customprops/{pk}_mesh"),
            (16, f"items/customprops/{pk}_pphys"),
            (17, f"items/customprops/{pk}_col"),
            (18, spec.get("sbd_res_name", f"items/customprops/{pk}_sbd")),
        ]
        if spec.get("norm_chunk_guid_raw"):
            res_slot_map.append((8, f"items/customprops/{pk}_n"))
        if spec.get("msk_chunk_guid_raw"):
            res_slot_map.append((10, f"items/customprops/{pk}_msk"))
        for idx, r_name in res_slot_map:
            if r_name.lower() in res_by_name:
                r_info, s_info = res_by_name[r_name.lower()]
                b_ress[idx] = copy.deepcopy(r_info)
                b_streams[1 + len(b_ebxs) + idx] = s_info

        # Optional per-prop extra RES entries (e.g. 8-hull placement proxy _pphys_1..7)
        extra_ress = spec.get("extra_bundle_ress", [])
        for extra_r_name in extra_ress:
            if extra_r_name.lower() in res_by_name:
                r_info, s_info = res_by_name[extra_r_name.lower()]
                b_ress.append(copy.deepcopy(r_info))
                b_streams.insert(1 + len(b_ebxs) + len(b_ress) - 1, s_info)

        tex_name_low = f"items/customprops/{pk}_c".lower()
        ms_name_low = f"items/customprops/{pk}_mesh".lower()
        tex_h64 = frostbite_fnv1_64_lower(tex_name_low)
        ms_h32 = fnv1_32_lower(ms_name_low)

        chk_wire_specs = [
            (84, 60, spec["tex_chunk_guid_raw"], tex_h64, True, 9, "raw_tex_bc7"),
            (85, 61, spec["mesh_chunk_guid_raw"], ms_h32, False, -1, None),
        ]
        if spec.get("norm_chunk_guid_raw"):
            norm_h64 = frostbite_fnv1_64_lower(f"items/customprops/{pk}_n")
            chk_wire_specs.append((83, 35, spec["norm_chunk_guid_raw"], norm_h64, True, 8, "raw_norm_bc7"))
        if spec.get("msk_chunk_guid_raw"):
            msk_h64 = frostbite_fnv1_64_lower(f"items/customprops/{pk}_msk")
            chk_wire_specs.append((97, 50, spec["msk_chunk_guid_raw"], msk_h64, True, 10, "raw_msk_bc7"))

        # Wire BC7 texture chunks (_c at CHK 84/rank 60, _n at CHK 83/rank 35, _msk at CHK 97/rank 50) and 3D mesh VB/IB chunk (CHK 85/rank 61)
        for chk_idx, cmeta_rank, g_raw, h64_val, is_tex_chk, res_idx, raw_bc7_key in chk_wire_specs:
            c_info, _old_h, s_info = chk_by_guid[g_raw]
            c_copy = copy.deepcopy(c_info)
            if is_tex_chk:
                # Match NOBLE1337-Noble_Lights's exact single-chunk full-mip layout for 1024 (11 mips),
                # 2048 (12 mips), or 4096 (13 mips) BC7 texture chunks!
                chunks_to_write.append((c_copy["guid_raw"], s_info))
                raw_top = bytes(cas_bytes[s_info[3] : s_info[3] + s_info[4]])
                raw_bc7_blob = spec.get(raw_bc7_key) if raw_bc7_key else None
                raw_bc7_len = len(raw_bc7_blob) if raw_bc7_blob else 1398128
                hint_dims = spec.get("tex_dims_b39")
                if (
                    isinstance(hint_dims, (tuple, list))
                    and len(hint_dims) == 2
                    and int(hint_dims[0]) >= 4
                    and int(hint_dims[1]) >= 4
                ):
                    hw = max(4, min(8192, 1 << int(max(4, int(hint_dims[0])) - 1).bit_length()))
                    hh = max(4, min(8192, 1 << int(max(4, int(hint_dims[1])) - 1).bit_length()))
                    cand_mips = []
                    cw, ch = hw, hh
                    while True:
                        cand_mips.append(max(1, cw // 4) * max(1, ch // 4) * 16)
                        if cw == 1 and ch == 1:
                            break
                        cw = max(1, cw // 2)
                        ch = max(1, ch // 2)
                    if sum(cand_mips) == raw_bc7_len:
                        tex_w, tex_h, n_mips = hw, hh, len(cand_mips)
                        mip_sizes = cand_mips
                    elif raw_bc7_len >= 20000000:
                        tex_w, tex_h, n_mips = 4096, 4096, 13
                        mip_sizes = [16777216, 4194304, 1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                    elif raw_bc7_len >= 5000000:
                        tex_w, tex_h, n_mips = 2048, 2048, 12
                        mip_sizes = [4194304, 1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                    else:
                        tex_w, tex_h, n_mips = 1024, 1024, 11
                        mip_sizes = [1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                elif raw_bc7_len >= 20000000:
                    tex_w, tex_h, n_mips = 4096, 4096, 13
                    mip_sizes = [16777216, 4194304, 1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                elif raw_bc7_len >= 5000000:
                    tex_w, tex_h, n_mips = 2048, 2048, 12
                    mip_sizes = [4194304, 1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                else:
                    tex_w, tex_h, n_mips = 1024, 1024, 11
                    mip_sizes = [1048576, 262144, 65536, 16384, 4096, 1024, 256, 64, 16, 16, 16]
                total_mip_sz = sum(mip_sizes)
                mip1_u_off = mip_sizes[0]
                mip2_u_off = mip_sizes[0] + mip_sizes[1]

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
                c_copy["log_off"] = 0
                c_copy["log_sz"] = total_mip_sz
                if raw_bc7_blob and len(raw_bc7_blob) >= 1398128:
                    c_copy["sha1"] = hashlib.sha1(raw_bc7_blob).digest()
                b_chks[chk_idx] = c_copy
                b_streams[1 + len(b_ebxs) + len(b_ress) + chk_idx] = s_info

                # Patch RES stream with exact (range_start, range_end), (tex_w, tex_h), [n_mips, 0, 0, n_mips] (firstMip = 0), mip_sizes, total_mip_sz, and Proxy_TexGrp!
                r_s = b_streams[1 + len(b_ebxs) + res_idx]
                r_raw = bytearray(cas_bytes[r_s[3] + 8 : r_s[3] + r_s[4]])
                struct.pack_into("<II", r_raw, 0x00, range_start, range_end)
                struct.pack_into("<HH", r_raw, 0x16, tex_w, tex_h)
                r_raw[0x1E:0x22] = bytes([n_mips, 0, 0, n_mips])
                r_raw[0x24:0x28] = b"\x00\x00\x00\x00"
                padded_mips = (mip_sizes + [0] * 15)[:15]
                struct.pack_into("<15I", r_raw, 0x38, *padded_mips)
                struct.pack_into("<I", r_raw, 0x74, total_mip_sz)
                struct.pack_into("<Q", r_raw, 0x78, h64_val)
                r_raw[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")
                while len(cas_bytes) % 4 != 0:
                    cas_bytes.append(0)
                r_cas_off = len(cas_bytes)
                r_comp = struct.pack(">IHH", len(r_raw), 0x0070, len(r_raw)) + bytes(r_raw)
                cas_bytes += r_comp
                b_ress[res_idx]["orig_size"] = len(r_raw)
                b_ress[res_idx]["sha1"] = hashlib.sha1(bytes(r_raw)).digest()
                b_streams[1 + len(b_ebxs) + res_idx] = (1, 3162564249, 1, r_cas_off, len(r_comp))
            else:
                b_chks[chk_idx] = c_copy
                b_streams[1 + len(b_ebxs) + len(b_ress) + chk_idx] = s_info
                chunks_to_write.append((c_copy["guid_raw"], s_info))

            c_pos, c_esz, _ = tpl_cmeta_h64s[cmeta_rank]
            struct.pack_into("<Q", b_cmeta, c_pos + 5, h64_val)
            if is_tex_chk and c_esz == 36:
                struct.pack_into("<I", b_cmeta, c_pos + 30, 0)  # firstMip = 0 at offset +30 in 36-byte cmeta entry!

        # Sort b_chks and their corresponding bundle streams by guid_raw so b_chks[i] 100% aligns with cmeta[i]!
        chk_base = 1 + len(b_ebxs) + len(b_ress)
        paired_chks = sorted(
            zip(b_chks, b_streams[chk_base : chk_base + len(b_chks)]),
            key=lambda pair: pair[0]["guid_raw"],
        )
        b_chks = [p[0] for p in paired_chks]
        for ci_idx, (_c_item, c_st) in enumerate(paired_chks):
            b_streams[chk_base + ci_idx] = c_st

        # If this prop uses experimental VFX emitters or Music/Boombox emitters, inject Bundle 16's
        # particle emitter graphs, textures, and audio patches into the prop's bundle!
        if spec.get("has_vfx_bundle_assets") and b16_vfx_cache is not None:
            def _enc_vint(v: int) -> bytes:
                out_v = bytearray()
                while True:
                    b_v = v & 0x7F
                    v >>= 7
                    if v:
                        out_v.append(b_v | 0x80)
                    else:
                        out_v.append(b_v)
                        break
                return bytes(out_v)

            b16_e = b16_vfx_cache["ebxs"]
            b16_r = b16_vfx_cache["ress"]
            b16_c = b16_vfx_cache["chks"]
            b16_st = b16_vfx_cache["streams"]
            b16_cm = b16_vfx_cache["cmeta"]
            b16_cm_ents = b16_vfx_cache["cmeta_ents"]

            existing_e_names = {x["name"].lower() for x in b_ebxs}
            for e16_idx in (6, 7, 8, 9, 10, 11, 12, 16, 17, 18, 20, 21, 22, 23, 24, 25, 83):
                if e16_idx < len(b16_e):
                    e_item = b16_e[e16_idx]
                    if e_item["name"].lower() not in existing_e_names:
                        b_ebxs.append(copy.deepcopy(e_item))
                        b_streams.insert(1 + len(b_ebxs) - 1, b16_st[1 + e16_idx])
                        existing_e_names.add(e_item["name"].lower())

            existing_r_names = {x["name"].lower() for x in b_ress}
            for r16_idx in (2, 3, 5, 6, 9, 12, 19, 20, 22, 23):
                if r16_idx < len(b16_r):
                    r_item = b16_r[r16_idx]
                    if r_item["name"].lower() not in existing_r_names:
                        b_ress.append(copy.deepcopy(r_item))
                        b_streams.insert(1 + len(b_ebxs) + len(b_ress) - 1, b16_st[1 + len(b16_e) + r16_idx])
                        existing_r_names.add(r_item["name"].lower())

            # Pair current sorted b_chks with their current cmeta blobs
            cur_cm_ents = parse_cmeta_h64s(bytes(b_cmeta))
            cur_cm_blobs = [bytes(b_cmeta[pos : pos + esz]) for pos, esz, _ in cur_cm_ents]
            chk_base_now = 1 + len(b_ebxs) + len(b_ress)
            chk_triples = [
                (b_chks[i], b_streams[chk_base_now + i], cur_cm_blobs[i])
                for i in range(len(b_chks))
            ]
            existing_c_guids = {x["guid_raw"] for x in b_chks}
            for c16_idx in (8, 17, 25, 27, 41):
                if c16_idx < len(b16_c) and c16_idx < len(b16_cm_ents):
                    c_item = b16_c[c16_idx]
                    if c_item["guid_raw"] not in existing_c_guids:
                        c16_pos, c16_esz, _ = b16_cm_ents[c16_idx]
                        c16_blob = bytes(b16_cm[c16_pos : c16_pos + c16_esz])
                        c16_stream = b16_st[1 + len(b16_e) + len(b16_r) + c16_idx]
                        chk_triples.append((copy.deepcopy(c_item), c16_stream, c16_blob))
                        existing_c_guids.add(c_item["guid_raw"])

            chk_triples.sort(key=lambda t: t[0]["guid_raw"])
            b_chks = [t[0] for t in chk_triples]
            b_streams = b_streams[:chk_base_now] + [t[1] for t in chk_triples]
            new_cm_blobs = b"".join(b"\x82" + _enc_vint(len(t[2])) + t[2] for t in chk_triples)
            new_cm_body = new_cm_blobs + b"\x00"
            b_cmeta = bytearray(b"\x01chunkMeta\x00" + _enc_vint(len(new_cm_body)) + new_cm_body)

        while len(cas_bytes) % 4 != 0:
            cas_bytes.append(0)
        sb_cas_off = len(cas_bytes)
        new_sb = serialize_sbdata_full(tpl_magic, b_ebxs, b_ress, b_chks, bytes(b_cmeta))
        cas_bytes += new_sb
        b_streams[0] = (1, 3162564249, 1, sb_cas_off, len(new_sb))

        b_prefix = spec.get("bundle_prefix", bundle_stem_base)
        b_name = f"win32/{b_prefix}/{pk}_bundle".lower()
        b_desc = serialize_bundle_desc(b_streams)
        bundles_to_write.append((b_name, b_desc))

    cas_path.write_bytes(cas_bytes)

    b_mph, ord_bundles = build_mph(bundles_to_write, lambda x: x[0].lower().encode("ascii"))
    c_mph, ord_chunks = build_mph(chunks_to_write, lambda x: x[0])

    n_b = len(ord_bundles)
    n_c = len(ord_chunks)
    w0 = 0x30 if n_b > 0 else 0
    w1 = (0x30 + n_b * 4 + 7) & ~7
    w3 = (w1 + n_b * 16) if n_c > 0 else 0
    w4 = (w1 + n_b * 16 + n_c * 4) if n_c > 0 else 0
    c_loc_off = (w1 + n_b * 16 + n_c * 4 + n_c * 20 + 3) & ~3
    str_off = c_loc_off + n_c * 16

    str_buf = bytearray()
    str_rel_offs = []
    for b_name, _ in ord_bundles:
        str_rel_offs.append(len(str_buf))
        str_buf += b_name.encode("ascii") + b"\x00"

    desc_start = (str_off + len(str_buf) + 3) & ~3
    cur_desc_off = desc_start
    desc_buf = bytearray()
    b_entry_buf = bytearray()
    for i, (_b_name, b_desc) in enumerate(ord_bundles):
        b_entry_buf += struct.pack(">IIQ", str_rel_offs[i], 0x40000000 | len(b_desc), cur_desc_off)
        desc_buf += b_desc
        pad = ((len(b_desc) + 3) & ~3) - len(b_desc)
        desc_buf += b"\x00" * pad
        cur_desc_off += len(b_desc) + pad

    hdr12 = struct.pack(
        ">12I", w0, w1, n_b, w3, w4, n_c, c_loc_off, c_loc_off, str_off, c_loc_off, n_c * 4, 3
    )
    b_mph_buf = struct.pack(f">{n_b}i", *b_mph)
    b_mph_pad = b"\x00" * (w1 - (0x30 + len(b_mph_buf)))
    c_mph_buf = struct.pack(f">{n_c}i", *c_mph)
    c_guid_buf = bytearray()
    c_loc_buf = bytearray()
    for i, (g_raw, s_info) in enumerate(ord_chunks):
        c_guid_buf += g_raw[::-1] + struct.pack(">I", 0x80000000 | (i * 4))
        is_p, ic, cas_idx, off, sz = s_info
        c_loc_buf += struct.pack(">BB", 0, 1 if is_p else 0) + struct.pack(
            ">IHII", ic, cas_idx, off, sz
        )

    str_pad = b"\x00" * (desc_start - (str_off + len(str_buf)))
    toc_payload = (
        hdr12
        + b_mph_buf
        + b_mph_pad
        + bytes(b_entry_buf)
        + c_mph_buf
        + bytes(c_guid_buf)
        + bytes(c_loc_buf)
        + bytes(str_buf)
        + str_pad
        + bytes(desc_buf)
    )
    full_toc = b"\x00\xd1\xce\x01" + (b"\x00" * 552) + toc_payload
    out_toc_path = mod_dir / "Win32" / "buildkititems.toc"
    out_toc_path.parent.mkdir(parents=True, exist_ok=True)
    out_toc_path.write_bytes(full_toc)
    print(f"  [+] Generated {out_toc_path.name} ({len(full_toc)} bytes, {n_b} standalone bundles, {n_c} chunks)")



def _encode_varint(val: int) -> bytes:
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


def clean_items_toc(
    target_mod_dir: Path,
    allowed_custom_ebx_names: set,
    allowed_custom_res_names: set,
    forbidden_chunk_guids_raw: set = None,
):
    """
    Remove 3D runtime world assets (_mesh, _mc, _outl, _physics, _pphys, _ecsprefab, _nopolicy, _netreg, _mvdb, _sbd, _col)
    AND their 3D mesh/texture chunks from cas_main_sharedbundle and the top-level chunk table in items.toc,
    leaving ONLY ItemMasterCollection, DelMarBaseItemAsset, and custom Thumbnail entries.
    """
    toc_path = target_mod_dir / "Win32" / "items.toc"
    cas_path = target_mod_dir / "Win32" / "configurations" / "layout" / "initialinstallpackage" / "cas_01.cas"
    if not toc_path.exists() or not cas_path.exists():
        return

    forbidden_chunk_guids_raw = forbidden_chunk_guids_raw or set()
    toc_bytes = bytearray(toc_path.read_bytes())
    items_pl = toc_bytes[556:]
    cas_bytes = bytearray(cas_path.read_bytes())

    # Load vanilla base cas_main_sharedbundle streams from Data/Win32/items.toc (bundle 1908)
    base_items_toc_path = GAME_DIR / "Data" / "Win32" / "items.toc"
    base_ebx_streams = {}
    base_res_streams = {}
    base_chk_streams = {}
    if base_items_toc_path.exists():
        b_pl = base_items_toc_path.read_bytes()[556:]
        biw = struct.unpack_from(">12I", b_pl, 0)
        for b_idx in range(biw[2]):
            _, b_tsz, b_doff = struct.unpack_from(">IIQ", b_pl, biw[1] + b_idx * 16)
            bstreams = parse_bundle_desc(b_pl[b_doff : b_doff + (b_tsz & 0x3FFFFFFF)])
            if len(bstreams) > 20000:
                s0 = bstreams[0]
                b_cas_path = GAME_DIR / "Data" / "Win32" / "configurations" / "layout" / "initialinstallpackage" / f"cas_{s0[2]:02d}.cas"
                if b_cas_path.exists():
                    with b_cas_path.open("rb") as bf:
                        bf.seek(s0[3])
                        b_sb = bf.read(s0[4])
                    _, b_ebxs, b_ress, b_chks, _ = parse_sbdata_full(b_sb)
                    for i, e in enumerate(b_ebxs):
                        base_ebx_streams[e["name"].lower()] = bstreams[1 + i]
                    for i, r in enumerate(b_ress):
                        base_res_streams[r["name"].lower()] = bstreams[1 + len(b_ebxs) + i]
                    for i, c in enumerate(b_chks):
                        base_chk_streams[bytes(c["guid_raw"])] = bstreams[1 + len(b_ebxs) + len(b_ress) + i]
                break

    iw = struct.unpack_from(">12I", items_pl, 0)
    _, itype_sz, idesc_off = struct.unpack_from(">IIQ", items_pl, iw[1])
    istreams = parse_bundle_desc(items_pl[idesc_off : idesc_off + (itype_sz & 0x3FFFFFFF)])
    isb_off, isb_sz = istreams[0][3], istreams[0][4]
    magic, ebxs, ress, chks, cmeta = parse_sbdata_full(cas_bytes[isb_off : isb_off + isb_sz])
    cmeta_entries = parse_cmeta_h64s(cmeta)

    # Precompute chunk stream lookup for custom texture chunks in items.toc so we can patch range_start/range_end & firstMip=0
    chk_stream_by_guid = {
        bytes(c["guid_raw"]): istreams[1 + len(ebxs) + len(ress) + k]
        for k, c in enumerate(chks)
        if istreams[1 + len(ebxs) + len(ress) + k][0] == 1
    }
    custom_tex_h64_by_guid = {}

    keep_ebxs = []
    keep_ebx_streams = []
    for i, e in enumerate(ebxs):
        n = e["name"].lower()
        is_custom = any(
            k in n
            for k in [
                "items/customprops/",
                "items/bkcustomprops/",
                "world/customprops/",
                "effects/customprops/",
                "ebp_own_",
                "own_bkcustom_",
                "own_bkcustomprops_",
                "own_customprops_",
                "own_customprops2a_",
                "own_customprops3_",
                "own_dynamicprops_",
            ]
        )
        if not is_custom or n in allowed_custom_ebx_names:
            keep_ebxs.append(e)
            s_cur = istreams[1 + i]
            if not is_custom and n != "items/itemmastercollection" and n in base_ebx_streams:
                s_cur = base_ebx_streams[n]
            keep_ebx_streams.append(s_cur)

    keep_ress = []
    keep_res_streams = []
    for i, r in enumerate(ress):
        n = r["name"].lower()
        is_custom = any(
            k in n
            for k in [
                "items/customprops/",
                "items/bkcustomprops/",
                "world/customprops/",
                "own_bkcustom_",
                "own_bkcustomprops_",
                "own_customprops_",
                "own_customprops2a_",
                "own_customprops3_",
                "own_dynamicprops_",
            ]
        )
        if not is_custom or n in allowed_custom_res_names:
            r_copy = copy.deepcopy(r)
            s_cur = istreams[1 + len(ebxs) + i]
            if not is_custom and n not in allowed_custom_res_names and n in base_res_streams:
                s_cur = base_res_streams[n]
            elif (
                is_custom
                and not n.startswith("thumbnail/")
                and n.startswith("world/customprops/")
                and n.endswith("_c")
                and r_copy["res_type"] == 0x6BDE20BA
                and s_cur[0] == 1
            ):
                # Patch custom 3D prop Texture RES in items.toc with exact (range_start, range_end), [11, 0, 0, 11], and Proxy_TexGrp
                # matching NOBLE1337-Noble_Lights RES[7304] & RES[7309]!
                r_raw = bytearray(cas_bytes[s_cur[3] + 8 : s_cur[3] + s_cur[4]])
                if len(r_raw) >= 180:
                    chk_g = bytes(r_raw[0x28:0x38])
                    tex_h64 = frostbite_fnv1_64_lower(n)
                    custom_tex_h64_by_guid[chk_g] = tex_h64
                    range_start, range_end = 0x00100080, 0x001400A0
                    if chk_g in chk_stream_by_guid:
                        cs_info = chk_stream_by_guid[chk_g]
                        raw_top = bytes(cas_bytes[cs_info[3] : cs_info[3] + cs_info[4]])
                        cur_b = 0
                        blk_offs = []
                        while cur_b + 8 <= len(raw_top):
                            u_sz, fl_b, c_sz = struct.unpack_from(">IHH", raw_top, cur_b)
                            act_c_sz = u_sz if fl_b in (0x0070, 0x0000, 0x0071) else c_sz
                            blk_offs.append((cur_b, cur_b + 8 + act_c_sz, u_sz))
                            cur_b += 8 + act_c_sz
                        if len(blk_offs) >= 21:
                            range_start = blk_offs[16][0]
                            range_end = blk_offs[20][0]
                    struct.pack_into("<II", r_raw, 0x00, range_start, range_end)
                    r_raw[0x1E:0x22] = bytes([11, 0, 0, 11])
                    r_raw[0x24:0x28] = b"\x00\x00\x00\x00"
                    struct.pack_into("<Q", r_raw, 0x78, tex_h64)
                    r_raw[0x80:0xB4] = b"Proxy_TexGrp".ljust(52, b"\x00")
                    while len(cas_bytes) % 4 != 0:
                        cas_bytes.append(0)
                    r_cas_off = len(cas_bytes)
                    r_comp = struct.pack(">IHH", len(r_raw), 0x0070, len(r_raw)) + bytes(r_raw)
                    cas_bytes += r_comp
                    r_copy["orig_size"] = len(r_raw)
                    r_copy["sha1"] = hashlib.sha1(bytes(r_raw)).digest()
                    s_cur = (1, 3162564249, 1, r_cas_off, len(r_comp))
            keep_ress.append(r_copy)
            keep_res_streams.append(s_cur)

    keep_chks = []
    keep_chk_streams = []
    keep_cmeta_blobs = bytearray()
    allowed_top_chunk_guids = set()
    for k, c in enumerate(chks):
        g_raw = bytes(c["guid_raw"])
        if g_raw in forbidden_chunk_guids_raw:
            continue
        s_cur = istreams[1 + len(ebxs) + len(ress) + k]
        if g_raw in base_chk_streams:
            s_cur = base_chk_streams[g_raw]
        else:
            allowed_top_chunk_guids.add(g_raw)
        c_copy = copy.deepcopy(c)
        c_pos, c_esz, _ = cmeta_entries[k]
        cm_slice = bytearray(cmeta[c_pos : c_pos + c_esz])
        if g_raw in custom_tex_h64_by_guid:
            c_copy["log_off"] = 0
            c_copy["log_sz"] = 1398128
            struct.pack_into("<Q", cm_slice, 5, custom_tex_h64_by_guid[g_raw])
            if len(cm_slice) >= 34:
                struct.pack_into("<I", cm_slice, 30, 0)  # firstMip = 0 in items.toc cmeta!
        keep_chks.append(c_copy)
        keep_chk_streams.append(s_cur)
        keep_cmeta_blobs += b"\x82" + _encode_varint(c_esz) + bytes(cm_slice)

    new_cmeta_body = bytes(keep_cmeta_blobs) + b"\x00"
    new_cmeta = b"\x01chunkMeta\x00" + _encode_varint(len(new_cmeta_body)) + new_cmeta_body

    new_sb = serialize_sbdata_full(magic, keep_ebxs, keep_ress, keep_chks, new_cmeta)

    while len(cas_bytes) % 4 != 0:
        cas_bytes.append(0)
    new_sb_off = len(cas_bytes)
    cas_bytes += new_sb
    cas_path.write_bytes(cas_bytes)

    new_streams = [(1, 3162564249, 1, new_sb_off, len(new_sb))] + keep_ebx_streams + keep_res_streams + keep_chk_streams
    new_bdesc = serialize_bundle_desc(new_streams)

    # Also filter forbidden_chunk_guids_raw and modified donor mesh chunks from top-level items.toc chunk table
    n_b = iw[2]
    n_c = iw[5]
    c_guid_off = iw[4]
    c_loc_off = iw[6]
    str_off = iw[8]

    kept_top_chunks = []
    for i in range(n_c):
        g_be = bytes(items_pl[c_guid_off + i * 20 : c_guid_off + i * 20 + 16])
        g_raw = g_be[::-1]
        if g_raw in forbidden_chunk_guids_raw or (base_chk_streams and g_raw not in allowed_top_chunk_guids):
            continue
        idx_flag = struct.unpack_from(">I", items_pl, c_guid_off + i * 20 + 16)[0]
        loc_idx = (idx_flag & 0x7FFFFFFF) // 4
        loc_16 = bytes(items_pl[c_loc_off + loc_idx * 16 : c_loc_off + (loc_idx + 1) * 16])
        kept_top_chunks.append((g_raw, loc_16))

    c_mph, ord_chunks = build_mph(kept_top_chunks, lambda x: x[0])
    new_n_c = len(ord_chunks)

    w0 = iw[0]
    w1 = iw[1]
    b_mph_and_entries = bytes(items_pl[w0 : w1 + n_b * 16])
    str_rel = struct.unpack_from(">I", items_pl, w1)[0]
    s_end = items_pl.index(0, str_off + str_rel)
    b_name_bytes = bytes(items_pl[str_off + str_rel : s_end + 1])

    w3 = (w1 + n_b * 16) if new_n_c > 0 else 0
    w4 = (w1 + n_b * 16 + new_n_c * 4) if new_n_c > 0 else 0
    new_c_loc_off = (w1 + n_b * 16 + new_n_c * 4 + new_n_c * 20 + 3) & ~3
    new_str_off = new_c_loc_off + new_n_c * 16
    new_desc_start = (new_str_off + len(b_name_bytes) + 3) & ~3

    new_b_entry = struct.pack(">IIQ", 0, 0x40000000 | len(new_bdesc), new_desc_start)
    hdr12 = struct.pack(
        ">12I", w0, w1, n_b, w3, w4, new_n_c, new_c_loc_off, new_c_loc_off, new_str_off, new_c_loc_off, new_n_c * 4, 3
    )
    c_mph_buf = struct.pack(f">{new_n_c}i", *c_mph) if new_n_c > 0 else b""
    c_guid_buf = bytearray()
    c_loc_buf = bytearray()
    for i, (g_raw, loc_16) in enumerate(ord_chunks):
        c_guid_buf += g_raw[::-1] + struct.pack(">I", 0x80000000 | (i * 4))
        c_loc_buf += loc_16

    c_guid_pad = b"\x00" * (new_c_loc_off - (w4 + len(c_guid_buf))) if new_n_c > 0 else b""
    str_pad = b"\x00" * (new_desc_start - (new_str_off + len(b_name_bytes)))
    desc_pad = b"\x00" * (((len(new_bdesc) + 3) & ~3) - len(new_bdesc))

    new_pl = (
        hdr12
        + b_mph_and_entries[: w1 - w0]
        + new_b_entry
        + c_mph_buf
        + bytes(c_guid_buf)
        + c_guid_pad
        + bytes(c_loc_buf)
        + b_name_bytes
        + str_pad
        + new_bdesc
        + desc_pad
    )

    new_toc = bytes(toc_bytes[:556]) + new_pl
    toc_path.write_bytes(new_toc)
    print(f"  [+] Cleaned Win32/items.toc: kept {len(keep_ebxs)} EBXs, {len(keep_ress)} RESs, {len(keep_chks)} bundle CHKs, {new_n_c} top CHKs")



import base64 as _b64
import zlib

_DELMAR_ITEM_STRUCT_B85 = 'c-rMwdr(y875~_!E+EK`g|LVT_+U!$1xrI9aRhc@T`KB^hpfclUf8?rwRd;%?p;_^5=cr45iu=j1JN*(7^9UUF%k`9AZfKe+N4ox43cW2No_MIQAXqV+MaXwTOLNI>EFFG-`;c1cfQB@o!>ckbF+0ieF!008Y7O(mt|za)8!we_+<NfW9PBg@8;M;|3U71B=>g+vHE#+BYp3p^K$wYsXdVDAMA?=CJ%aUU)C1QA2#;>%iUXiyZx-+L(cmV`5{kD8jcAe4Zkqw(ZGAp9P-<IC1O`+s_Y^Ae@XSQ&c$g-Nl9sKt&wRz<V1tV<DpwfkMI89jPnU1|J#FmD9HcglcmcxnjriWXAi$~X>0Q8wih;Z5B0V5A*-0K2#;#oOk?FsA-d!Q{y+t({WdKB`Sw%&j?J=2E<51O1?r3DnMXFgZmhl&b+7@~RA0}}?N1%-InZ60DyQsJB2Qo-wF6+p!AO9S0z(huChBm96c~D%PGsM-zD18Li5&WQQ+exT)wZ2a#Y~-CsogNLwBw1xss-2PPK#H5z2)usfd!ja{rGC3|HemOy}YAiX5s1FmlCbDXI=|&cNqFE9PgR5xH(aKDdnGz62-;7>cZLenY*{%t7wf~KX|=W(O75k&u9`Zez_*U<6z_U@X&8ICFq)`zS2BJ_xX}LsZDPsPJXj^&*d3+V&~NVw&?D{EdwhqJlp-nPm)e%o;r+oOh-n-cok*1dp*5BV)_al63!Z<Q8kXO=!bGthtg&x9#5_=M?DitH{=1hBc}Ibj~L&NKVsgoJ|Na=Ex7mI(E)dE-E6b_z+ladzD7g*Z~naX#rECKmqYgyZan_RwiB~5S6#52-+L!=)xdmlS53!)=7Ua6gmw6jLw{Se=Y9FwrW4M2-*$&z`YAucE!H%zQN*lV@%zVWdbDBHA4L42XXtC$EOp3@+8#clG<5Efr*9^v4s#8ADpq8udKLzbL`T(qwQ^zQS1CUenCG6<Eo(XYG3z()R#3r7Ms{}Z&d}uG+ebbuxvOhvZFlQ~+jAnBtp$%an{FOks}2lwMPF(}9Adk+Z<0&ZY$8Tr&fgo(e==3#lnF3b66dKk+hYl0W&(aG$!G4CTQUh_6Nv%;zs~ygv)+#5)$2{CifDF-3iP%}Ph^OfYagLCv@%{EE_H(RngrM{L9t`obm_eGPnDpb{&#YGw6tIPrvb#DG&Sm{%<yttT5o;Va%?s|ANOB%=47%Obbq=&8Jlls?(;r|YYT-Tl~flV&zRkRoR6H$b!~Yph5DS!TDM3q1?2H0ul+nKU*ep{^D9;(&p{uM3%l#}F?bj7kGmbPBTkCe;~6~OoSH!8=mO=@bSi5F&vb*-0w<C1f!=ezzA%)t)8^yqS#@H%uZtfFj=${t;_MEK8}RpiRK~@nD<0IS=>EN*l~`h=I#H0l*SBejBnOc1^3|PaH$g1tu3YO?QMp>EYfwr)f#i#e-SX+wu1L}TGLFWJcb+ZXIf(4ocfdiYPgiluQ~p$L+yC*&04hJs$9y$D*%xn7nkn5E?{`mHAD2BKt}%HsvCmT$`Es{EDyH2?<>nE#(@N!!ez76QL~+2_6KL!=`=ebkRGxNA9&VsM0oPKmiZqXkm9_mFsC?#kes8&Hj=H9_sMDpj!KD7i#^Lh+661Iru4MK%1XJ19d;G?I0g*c>KS66j14iW5YoKE;-Uc0GdK$n%?)QO?_b&$>Yw0xjAul(<8*`Kd^T^+IpkwW90UhtZU*^Sl1a!>7U*L_k(g}9t=^^}A1TmC=9XbCT<={P<r}JP(j^XzLakcn)?~eb7FfY-t57xQ`bnF3`1LWZ(nU~kYpd((`qcIo7GH;#{d*s<Zup<xK;EkB=lX-g)VuL;58L(sTLGobk=fFJH+Pk1*uHL}-fZvtihkRHBI&!=YbgaEU(O#hhJNC3N&@oSDn8&=g$h@^R19YrOEK2MZE?R#f_?>~=9|J$+Of9^z_brC`OqO5D2pY!8W{a#X!|7y2qH%G&c?l=xTV1viJHzohfw6J+GOfcdGMt?iMm=y%CL=Nge@3Sb4xTHm^6usRoiI(vGRDS|C2SSZGfpdOmV8h(2oBL9R+Y2M?R*u<a|ohA;2Z)cRuL^DvSlE$PBMDv=>5i|97ZT(C67FA-I%aCleI7|UK}&z*%+Q5Qzx!lo#hq<=0004TfquW)=Z?G5ix{?eJEOF1zVa#+@y-EBe@Qd5jlr_Icy<tW|riYGCVt~X<;9M$>P}x$qQGisWHR6#^n@Ak;7rrF{PqIAih=JYtX~Lr_hKdn3l3;&Sjfu^Wxy!(@FU-mRHVM&NEfZ#s{i#+08sV!DJ8s-x6xVSxB~pD<k;=V|Vi2&@C2Z!uL?hD-~GQ?zB2Yn&K*w;WBfMQ4Oz|ZO?<ySp<eulI0~%ktyX_jl->Hc#9Of&z9o=Wc4gpW)<m){{fm9sa6'
_LIGHT_EBP_STRUCT_B85 = 'c-p;L2Ut|evhG2WAj+BpDvJ3USTTnMh9Im6ETE`}aDX!~GRZSTa#zJ1cg4J7POLdA>gu8xaIax8tzq@9`Fhp$iaGPDPWNHX450UW-+gbsZ<y}t>guZM{;R5+h~U7$4IKyxP!5iSuYZa?vG`>CxheMYy!TP-=D(Qr;;S8r#Oh;%f3_3i$mSK>(ccqxq{7d(O6Wf_dTA;By=-fiuaNO6Dq4SqoRaNbUiyDZD=+_0d~+Y^Yn~^4X+L3UXYcz3rC-VC*;>d7QdVsB6JJG{%tOA+Wa4ybb#J7dJ{1Bo&nYE&l&`Lztz1O-k$!}@E3MZy>w3ah>QxbCd-MUhePNETWP?IM|B4?g7ufti`@16lPI~x_ckZGF-W88qe$aMmUqv4}hx+xczvO%BFN4z3f9}3=U&?(>_s1*`eTuSFDwVfc9@?g$&Q&lMvpuNDp9NE9FqZ5BYpsiS#^O4Xz1i3XS{dIA%_Z+;z7sxSbHzTR+x@9ews+2Z36Y=se0pEVXY619y6*ql{__LQdAFS7_|^Y~{p7=c*M8ed?61~vqVs<PPbuuTiPg8VkK*%AAQi>@9?UPEA1(b8pi=RPCSxbRG^0tc4HxtVW2X>xa*8R6*BUgOiI1Zk0m^U^5Ewl85zJ}(Lvxo4t-J@$I%@ZaM{M+cOa%c;8=3OLJWi$ec{^^L-MA}}vCYVa+@s&`{Kx4chle|tw;3N%oO*Yot6#{3{yQ(PYt}5GL3+QU<#Qee{kgKmRh6(Q`PxvEXO|0K?zc~0^vBiPDAQi|)B5$PBcE=Vb@cM|`kw8EtsgT4ZO7E*3f~-NTsR!kaM-ADn1q|s2JJ+fG2AjAXG)uCM{`<z5aMoML4=1fPkZ_m{i7T_xV|jz;XL%`#opDYra<<K2n^}muoWS#D)0g7Z|w)P$J!TYpS3^GUTdGA{jP%o3YuPT-QBxh#FLl>LnoFr`D>GV=!vY&Ba<&qdob;!c1m7T+aBFb!{^uD`SH7W*VnU~`qVjEL-Y9TD3>w(%NOhWb=~pk?1e$CPp{E8owM)TmjyG!+s!$-V8ZhC#_2USM$h<l)snw^_z%0K`g8012E(p&G_A|t)M@Ts$8q<+U)w&uWOdWlxx!+XRxfLLb==te`>nMmZ_lgYS!=_okAtgLb3b@%Ouuh;y1hvKxNl8^dGkh18XebbTl&^70!FV~c5U;|S5KVo5OaM=e5^yGx+;eq<3{u`Ec|h0ohEUKPujgb_Vucgmdbg@>|?gujR{RCS>qe-5dSRwW^D1?;(tbXcm?<!jIn9-IJ>RSIc3*+r&9B`zq*#YeDPsZ@`R6DpDez<+2+8A@vDY}=e;@LJ>|*|W5#`XU_`IGlLUTePIk*>=c8j9hu50^-P2ip$1m2*9{s(Y(~)00bZIukaaQ&BcCiQT`fAVr{A{GI-+bFX&IQLVym|BD-wQkhr<$feCiLB}yfN&ng;kS^JY#KM{k?X}StobRp4er#^4vNf*T!e;Lvxefw_Cpc!uV+Y+wL>9!w=Rg-2dpVjYnsfm*e(p>Lu1_f9#tFcWZv|y0G!kFI@Eb=)8AFN2i=!^x=!FREN;1VNGhUSiEP*6OOO9x%R!+zdf5#t4hA&VROY1M^&U>>-qPyHs1Go@_4zo%glSuYnz9<uR9aEGHPVgm{_;mvqE9Q#l%)qTbJG%dg!Ov%>`?Xc@sCpl=SbgCi99*$+7ReItS*jXj=2?xt|v{&G)qL>-6$&w<+E8H4o0#X|%s|<f%*5Y$q>y*X!oUxQJnWbse7`4QPJJdG5`K_Q~5e<`+$K%yayD)B^pPd51qF3_m%$`wkz|)@A{kbFSN>{+u?paMq>26@J|V&;Fa^gP*n%^F=&h&5?9arrh&`z7O;b3Rg0`_4L)b(C-QTaOe}`9Q%9FcY!{EexZA)>nrG2gZ>IYx9g?$<64ODhWQ|`$hY?QO+@&{1D+j;W8~txu53IA`i-^IBW>8Y5A=WYm>o;lxI6Tx#-AHX5gbqILx1;y<F_3|ejjnXLt#(fYGR1+5y#ua{`GrJ*?6PW#MF9X9Pr7u1+K+)JWA@*SacYrj$bzFw1;fG=}3q8w$QK2#<S-Y-BOC<Xpbwo|IAkBQ)Ztl*?eGmVt3Y$hyKk*>gwGXKd%2kQ{B6(tZzKJq?4D-zNJw|B095i)L-?w+kl?bo?;)6=jpI!Nnde)e2T%lB&c(^^8}gx)5i69IgN%>YEP>>{xfK}q~m?=zsiyEFKw&$UO+>t*#BojfBw8^FWtW;DFJ_7qw9k|6r}6MTU~C*<Y#VPmT`-XyTV`3!oaX+OkYty)4MxW6y|XUXusPMF_u+y#4lQSg#Nz)G1yG6N?eUQiZSjVQk0fDru=)eJ67|M6*l+ieyWYwu0|$lbqhVklTcc^v?T8+VQr_ii+M-Zu2;+LbZwLma%SxdGP$xg%C9dTEIQXwHkZiey2|ECa?E-<k{@P0^70Y&aUqrI(CksNXKfKf)ye*y&VRYG_Q_tokBB-bYLMyu_pT*O{@YhG=Sq1j;`d(7q?{GD#BGgqFSwv|^O0)0EeT!irTWIb^Ue;^RnM%a1L@gxQp3Sw4lONhUt`aTAg0^$snt(}vv%m&{hD}DH&?Q1d)8CJ{9~45Prm5YX=w`MH`DDzoESt~z93U~#c53_2W<JE@XM8K$=QDMtYk}*^yeXxEq8j@WJtDzpRJc8*)lO_&v%k7O52h)QkriP<8#=7wX545+2~l#UME?*K9TRWnO;xEQ&-lmL)um0;@m|)G$5)THVA)cHzfQre^*!5c4+m^&z-ezy5)E9WbHLO^7Y~wfVH*_@6p?qwR3l!KI_QZ=Cx{*Hy;}|jAy<w(`(7}sY#;xe$(Lwo9n-H%G+J6J;LSd%THMQ!1%@o;uTa*)a9{7E7%@6cU1g7n9&Og;%{AG{Eu9k9mZk^+s4L&p9HaX`tf&PCNTcD{o;Q*#rWGt)crxcY$+O%?ER~Xa+tl{_S=qjWpmeGf6(hP<9CaTy23FzRZ<8162f?T)>t^|fOt;a$g+(5SHl=jD;w|UiLBi^Z1tmTCcjaQkg!c6e`9jC=(iMG1#R2jp7YR=wY_?6uF;;!ADClEXu;?kJ8hWzn$5kJa_IIo);?;yUS}O^&lz=mu|Jzz+imHBF5=uKq*L(O`Gc4~&(DnI_Yz8PRxmW8n6<Z@9rB?En|ouA;=v<kd)$f025yW_I%VIy&*;l1?d>&`$-F-GVdy~CKKlHiOB8Eg4g0BC3~Rgh=~Lan{E%2cw!CL@#%3RMbZ7Fbc)ahwjnVIgw05q|=##lClRa5`O1#~!6O8A@<Lhl>nf$#U>y!>*?SWj2ylRX;GLe6Nl<{AVDJmMyc+#KUv^&Ib^xiGKWjNyxjT(3Sx#*Lo<g2&BfoR6R^y)tr-I2~}ZsA8C#(z9!0oR7LuUGeaU!S%273ZyqV{_*^#d=C{!`W?ax)e9Mz4B@##f|(!wZ=(tV|&}jZc^NM=6Ga<6gQUiyZ?tIr}O1s8cA}ZcQ4o?(PJ+a?w06<W9n~{<S$Zv7$wP9rWD+e<p1`oYkg_%+V%S!q`7<cx0^4`eRRxJB+Y$sZTSRg?v!@-4@<fg&-><(B*%65oJ>hh^6{walAOrF<HuAf7vE}<q=??aA?91JCDmVhSAzXu8TO?Zg80pr;a9>e;#a~E;`eJAex>t(_;pv9@vD^K_bVBGb7c7KqA=rk8!_Y8R)$|s8Gi2&Gk%}T@GIdR@k<nD{7Uf<@q3M!@#`bQ?*SQp+bGQVl`xC=y&%JHH5q;-EF*pcWcckP!>_Llzy6ir_mm935|$Can`HQ1N6h$@FpKz=Vl3h}NQPfO8GiQ?Gk%L@_zjfd_lXR@J!JT;uQ21cuMEFZJVE@nkl|N~X^7v~#Ejp53hP`}n-t^~@)H@)>RG!^%oKgnOspld#eHc`nk;F&gw`G&Bxi4`+Q8Z^JiATKVfnFfTK)D@rFD-FeLtJgW1i$*BCPEcbfvd~wQDr&Gs1?oOJ`-*slwV@F7+>avpTp*dtJhvBnRcA94U9B94U9BoNDFrxI4L#n%;RE!(Ek}+lf-#Yx&#lz!J7@bGzIe$!}d!9;==(`l&Au?z_h5DU(AIB%Ja$m%iM~{E+!^nez$O?%g2%<O;Fff)r(y43*CC-ClzROIZF->X66&=ciSxpe7l7D0Npns}*PWE>Nb3Yqcb}@0m=8(F?0iUC8v`+O?VYJl4j&miFkiagky^qvO8kuzklpT_D@jnX*0o(P~d`D9n3tLt);F8w&GYU|Yf7aul-nZL|aDorEvya?ynl#K2T1S~J(g!vXhK9~Q@ZfndbuX1MGj7B9gs-miK?z()W3!+gZL8t91aIq-}3J11BKW7v;SlwysT2z|T<55oVefR0a{i5Rn9z&PG_$FUsJ7S_X9(*)#T9XJN&VVyq1hUU4kb~I=4_B6k(vZMEbJTMO9%y^iGI9};Q>tjrYSg*!{9E=l<fR6sn2Pa_-IoX!p5Bh+7tXXx?$NOgs_{Dr12lDZ*5&`nD4z!0pp6hckAMZJ%U>;(&6zsuR*c;ZzoKXez!!tGn<ltW2gK@-ILzsv6-8w+WGjiO8-Z%F;(fdmYtRvpn;TO-@Y8c15T4hV`t@$7q@pBX8V4N(hO79IF(N4G*D}fK=@BpCWy>lSU$9xkHe#SU|5&VnyX;0w8cZFHtcf?30@ZmecO6VhgQlXFU42z+UwZ94Y@jmk!?8P%b9PB|{x&t5Pr#&Da@A<Dm55)TAYV_W`8Tc^Q6u`Q8|GNnC5D!Oyj(Gn7e3%0@Fdt*wJm}+j{tL$OT>^6p-oY-zx_E~w0Qva7a2tLR&(WY4=Ka~gkN43BtTr;((L0o~3cZi3s?mDl7Wf(S^=Nxq%Xm1@JCz3bFz>&yqxD5CXIfiqWi{0gK*x8RWLCQv!5+lyT9Au*=>hl~@xLGZfiYw#{NlS#2JquO;qP|z9v2PkV9fg$%)>kFM6eHY`Y8umD}{nw%+nj-7jyR__{BT@-+&Kuj}PdD_eXn>gL&Q&#xYNP3-a(CBm(?__t6sY7v5>-+R)l>53G;xRIS;axftYOZps9Cc+U=lKIY3X@DJW`8rac$<VIKr-x@HVVZP3*O6$Zk_Ovc^0KG96f2c}h%On?CJN^sCF^0Q9AMas5fnJzLy<k18o!Z*dTJKj_5AR)fZ0Q|#K^0mLP6WH~EkF<Y;~T;mSRZq8J7-!)#=|()b@?z4Yqw{hFXr`hkcanod=$X=`WpB#Hw*(k@SOyUU3|m1#@@Ad10UXncS9fZK#VQDlQ#f8uvRRDKE9WARnS~`3*_M)a|zI~9;{<e>*-*7TBE*mqWKda+^|0O!Am*ROC7;KSa1GrOW(^*fPAdquERLK*Zqv|C8l?GazIafUliY1p#E}!c~}>I3w-!4g!vR}$u+PZzMJ4Z1mC01fL?fKeFpOJ{cePUzFT74$9Id9U?<k2^I;y=Lh(-YU930sF{gULx>&nTv8V50Zs1?6p_hSNykF18xB_)eD)ccYodEl=R(uG2SO<jx9c!;%PSXEfl$tc&AgFaF5}qJL8uTXdUoQbVliHN!&zU$%PT>XKdVu!nzh=zTa)hvi5f;msTa-zeW)%7GkJR=x8hMjQ@zd)}oLa{VaIQElPXaYuvXS6FiFm=31*19;uGZ^Fq&`hZ<Vh4KB*RqN3sm!(q~Ih*lzQbS8^IP`s!W(=*GPhOCSFi+AU`0T*O_FTVn5QrClZiMR~1QYP1?4aM2DiX41DDyalyKDPNPox1c#qS4MV=Vq)1*!2M2wQ^Ail8l@Q9CGW0?!pY&N#R-9(_D!1Cm&WMrFIgyDeyp}Vm6ODw<l@rRE;&z6qRCrS8PnZzI8B=5|aGWf-#8pU?TFd+M8WSh#V$q8W)$6QD;TkRr)wdu<>jh1c6)hmsBye9=MC@D<(W@e2mx_p8t%!d5G}wY*W29PZ(C`RQUmd5(0*lK*RG-Lctcj5Za1N)DW7gd42Oy(<l7ljw2M^$oB_shRJSRj6oX)7y3tBRWPxK<0B#Vq90xn9@3<L%;I3bCckc*gk19V)1hEF2?YGYyqm!wWZlz~uRT{74dmS&Qg5xhzR>gWYJg9Hae0)ka)Xbv>;=AN%{0B20a;Q?tHliC1BNg$CaT#`Ox0GCN+(QY7@#KQs+oGzIsew@)1#HocKUeMEFB6R~Y4R9QE=qx%S5lFcaEoTT9cu)h>?#~N4UPC~SxH1}?K#`!-X9kLd&X(OSVz(ccW;Cif9i?@xfY!YNTF=TAw${l{uh9!)G}9Sj$BC~AT4@}DknKT$Rt=)ld6;kDna5>Z3`ydV8mLoKI9v4MLejKqogU7NK0`br<;FN&qHGTF6%&TPTBS-e@~npeHBG|_B%BlASZjDu@klt#JozRK;-V=FJE)y>jw&X{g}{DRGBkh(i?x-F27!W=jSbYmX|8M#4m8yWPZr$<qu3s#*Q9BAlb}u{{#iOst4<uCPf&{@=;>mXnE607xE^w4vU$raCCTMS$XBo?$QeZdCz^GWcSCqxvZ>7Q5I!AFjl3gwC?#0Bk<#SOqZA95QIfC5O0<;ns3sA-5wxedT_pIXne>$rl@Jn>2B<5f!o}Z$I#k68^c;utkgmb_WGOO)LO5eUkV1uJ-(RiO!l{;REyGBK+aepY^tl8q@v!3pdU0>dTC(HLR-_aC(lnaLM1kjZz5>t5!dW>Ya>7Y8iC{DHm65DYpp{_Q8hu7TjXpsNBsfmjp*jhmw+LYfe9V|INYsKliN_#hOcTt6Kq1X&O4C~S(T{^9%_hny0}V->i5K%*navR7g-E?%3K!IRLG%)yfpk7e(!CrZju}^eV1PbdT(aCiqzMa#@^qSN%T~e@RMxTbnbkH}Y=DL9WzMy9D=o8`pTcyStRP-%P#cj*{8H4Kq;j=2s4bYFFyOMRqmfdt*AT=igQti{3PQ>41};Mfxif*I_CPvJ(rd+lUinZQKqgox9ji~WQDv?53e@O1Q$;hAc@e%=17(NEU*Iyrgd{i>*z$u?MBrF5>d*5AUk&6enpEJgiZ?;Q1m^1v5IYIgt1N^D3j9b~8PU=XJ#Ec{x>%DSjsRKIuxu}ZXi0z)s0+z3r&zx<C}4?&TnmYQdM(Y_Mr%(6<H3W%1xn;}Uh+9TkNwpejWR2mLy%xQOb3i~2y!(5DM~MD6`4jaE&l^M!qR*'
_LIGHT_NOPOL_STRUCT_B85 = 'c-pNx2Ut|c^N)Ti2pAQ_hGIb_fSOoQQHpm8iWI>L=kefPIPS<5JuDGp6k^AcM1vYTDzPH=s8M4Lf)&fpmRMq9jm8r7{xiG#@Nqdc`M$q?%g*l3&d$!xPT6;{ipa=2Z8<KYS5h35@W5(Zm4BbqnD%+$>?@@Wvu=zX^=j8>&g!2%{@KWJQ)IL~%f9M(4r~A6K<hGAKV$uctewK@#x+j$af*BBS#<JQ1g`hK_22hzZ1mj~M^$HrEtjXQ`Fx{L)9x+&*Ou8{-<qCbPVu>M*Jt)$U6R`LhAjED{afgX|6*C~%l7wxG0?!FYJW2a+w>>hoOs*4L^thv*R}=wy#L(tHvX$S+vfky+R(q-Ze374F=NcErUeTVjtmZ5xHA01*mLp)-o68Ji{8ioU{-Hq^#)O{_EpdK3szgxPg`Kvx9azuy&FCsJo}^i&p&i{%X&$y9m8rlt2<dS7|d#Gx%?NMu$SyBR??5M?Z;q$M_Mmob+h@i=SlHG?YJC-Wo$f$)x$QpSD{y7u~?+*hQ6ll_wkRoFx4WSxBL~IXRXh#B{-Gx#I^0J?kkV2D|Y+ZzGpjZ-7}=Qjm<y&y6S#7#@9Ah+rQ<`y=%Wx{-pc1hNa`SVDX=$1Rlt0Gu!wB#^<hM`v;#bsde?0HYscGQ18GzRZ{Cu8|~ZH@<p&$Sl%5shkvO{BK;gjf7V;*U;4O5`Q??_MZ4QOH}&=L8uVZ3%NhOPx6tofdh?}&?}|FwHmeUV{%uN||4QG3(Jy;xn|?FptQ>i8e*MiSwgt>RwD07<p^rEn82l@%zh&#p?!2)RCmBD?>P74zAHGifL^@u?>Mz-Nj~ea@ss4y~d8}5x2QQV?o!^5O%4$y=c;X(#IafVw7s1G98iG_NgHe~6rPpN{f}kTyqcrl#8X;Y6Oy*SvEHQdMO*tY^tIN`91XcFi#2RTb@B`D#5n7{Q%;qA}(s-4T>uECT;*3hYk&D&oj9fTx)M;UXH+WGxl~NP0S85GuI(;S=%^TEl0&pRu2`Z&g&}q3$z}E{(&4<BUm`<Y+4A3b~R+_XbwIVaiq%rU!aTtC>o{bGu0tKy#Crri*nY^A0&(<n41y!V8P?2STOQWP2rqrlRD2r&N0VJTy;`K&>H*nFqOkQj3pQg}ahq9N>3|%-QK0Ax&dh3-VVc7_+a)gFY<>J)JEFQ#X;En%}(hegukf$h|7-@bZqPAQ#1G$JuMbdIm1H<$7hXy2+tjrnnm0#&6gCgnyg)^5vKHGZW*_7-)MTtEwZLl=nGvbGB@w1(y>rP$%a^O#{eST<vX==@4Mb`TDp4Ef=FWg*_{N?xy3RUi~@%IAjwrJlqp!izk<GoLRKcBm;VXcdwZMy9lGA6Li-lY?_zd91%VnD%yStBdr9}QNwm|p*}-}FJ+4cz2Og_rl|3B?Jg_r!(Iiaa`_!OUx}Z9BWjHMvK&H!F@QYn0S#NYd)yy)mc0j_==QMbqM`*LzJq@$^`eu4$j~H;*hZH)#H6*|4E5#p?3YXCDvmcqWLCp3ypeLRitGLovTa7ygm<>I**o;MN}P=XL1#V(5h)y%!#664b43VY0)h-CjTL_~VAXUpu$IChX97sniQ=O}(Ax_m%67rhko3@8dLd<FLWAPxvLB{&;ce=8zC`v*d`$s~$Y?{Jh<Sj=86Q`(MPu`o@+0{f>_ec`;#v(}eTZ(U7%OvW_;6cFfw-X?5ehgElW&HF;t}<EL)=9=D859lUanx%_zMpjR!7yz2e5`^>2wE{kg`EQ7a%>&$^p1D(q^ez?o{Y52@)S*jUs8^(TlU`N6H1;4KxWuH=Ry7l{ay#M3r?LuCyb`Ll>;8E>P!k6c(AHaYMiw9aw^r&D0W<j|MB?hcT7mOlU#1hIP8jNl*(n*1$hH{%(_82HhP*R|%p%g-q0Syl*-cWp@_(M@c$%10Gr;!&%+a!{9ppm3Mlu#%!Q1YN0cAyc82NYjN8r7(w9EMVcZ71LlN*<J1P;Ntc07VWPi-Dqsk_9CX$}A`wplpLu0p%)g-UaBpyz$J<Zy$A+e?2U{wD*$mmtjSJT1OSFW;As>-J(ar%mYpr{e~sp9(AQ?SjF6z@rCoA%zV;)lggfD2)-^<uA$8JS3tQ6<pIhA#`ofWfX^H#SD}OgFDX!RpcFzWgYpddwWqYg8IO?RBvk;-IvBZelrFB>((t&z-@#%*`ncqvHh%l%Ts5*5wiWqX*I+x8wZ}gkH&PB1P|i4imD#JY9O90{Y`o^6#&S95L}ZWSPcM5dlY_SSvi8h@7nZ3ZcKL|4cc?Nl<seKv8=Sw+ym~&;H?ncF^X9*0_7+PHYaj8gHWAy?Sle<uD_?GBvCOr?|IP}($O?a(BaN2oD0(-Sb2W$@(W70MJBh>nqAcws?J}$OGpjbr8ryx{8yrf3{rR)D#olLSD6AL2+QSxoUlhu@aluyYkF46AtlA+~?NF<BY-Cis`hK8?Hf%56du#iPcEH*Oqg}AJ!)PbIX>j*sfamf<&f{Hn?j9ff&}FSluc9%s=4y4q>yks~&9Rf3rJA>1%=v52w3ybjk9{?9@oK{q_qB;rk1Q=X>K8urdfJusFY66GA80JhDGHjiEq>YBhl2;y_qdkPvGcGC83jX%v(v5ZJ=&%N-J0LKUwp~`^pFg>Z;Ma#d+xV-YV8Nmep&khv}e}#8Ex2%7USz}s5nq_Qo8?Zzu}(8&mZX~w+rXgo+Y`jzi;HzWz}~{P1W&FWC!csSka_yK~yWh4=<;wk9IGqmS5^)j!BsL#rM;jue;r{R{tHF9e%3utH-PS>E;#_oBa?_e@=d%QF9#Hx4W}*LE!$Q`4MNl4Np#`Z`QUcxZwSlwQoXO+j3LKke-A8XptIp!Q*h7JJ;d^9qg=q7TRNLzlHXBNaITxiOm<6Z&;J7H;umAO{KYV-Ca?8OwbSiD_j4l*Jan+8&~gNygk?2r=d-sw&S$F_Hnx%o5%K>ed*z*jF2sdmpz|<?pEP1r8iHf_$vFD8&zCf(d)jU%fo=gDPyOu`C$1skJ}9F(Y=*VPivove!|-Cp`WO=_R*C6O>XQRF(N(3zWK4-{hRjJv-YRxTdaL5`WkD$i9W~LccKqMJ#S&Yu?X+58+ersU^Ak(j4SCxvY-_3J2Gx`N~7x`#NMUAe;Ie8gVQ(#YbU|qQvU~Y97qJ4!udk`ta<JWdrA9*U;Z#^-)TE9<s?g<Q#fyEd+xau<VnUub9`adv!?yCK7e+0SiKXBA7Ab>wIQ^@Oz*>4<y)#9XiD02{MtB=@|I*h<W|OY-LhZjFKORv`f`3N;^IQddT}jt8U@hxz(>fq_D6TF3?%yv1%E5!67p|uZ^!22_^17IJGYnMZ|NG|=3@!{e&e$@cVPV21}cRw9u{_Du|Lw=HGbcXa4A1ujE;(6>!CbSU!+~4DM2WhXGiYk<Ig$`kkA|Pc<P^VMBbr9{*A-DrVo+GLlGF&Ace^T>CNc8GM1+<os4&n96wx5_g@waI|z1JUXa1s9?)Js^xzmR<%hIG2CWX()7XT}PgvvE&`kL!{FR2Zs-7)bKdO)B`_DLHGf8_Hl)u--j;Ckr!i6R3|M14_2JHEperE}elLU6;uCCkYo<gK2<9;jo#$8TgpK9Edl%F;v5x53tKePOP41ry_r74?&LrFYjvHUXW#b>^x-;EocKl5BLfopP?_bH<NiN;whb?#M+>qPo%a|xTeR?8!6)!}Bfo!fl^i5V=Gc?a+2=P)?ZrBM)a_8s`Yu+W}cJs5n><GO>4!JRuQer-UM++s1u2e_bqU4^-e>YJuFWBnEDdpGrE@W6m`W4swo(3l%%IL6!a*FHYMY~9<#yUuCL;A;o&EcPd9Vs);5&`%e{45O}3!8Y+lW?*Kk72+$#t)XKTVrHqk^K7j6qG9;!%iY0@hs%zaHp<w#tCpBwiagl6w`uIb<llC}<Z&M{9)7h;B(E+Q=co&d2|I;ACqQA6$q2q1CltKwNnhXGxzaA$>mF>aahpoI%S8Cz$)^;@gInG5?>rASH%c}Cj3}QCeSChTH^`H_I`i+l-UR-DJ6fY#6z#87X;+Zv!#lqnCnK++7R%~JHMf$NHFieg+2P(rex&+Y_`JN1uXw%^UG4D2p8LV9Uy6^sc{AP)+>9@dVK~84|M%LT?5i%fV`1sYU<Q}w5Ao^5_{n#85gW?j>?ym>6Ws>cE)OFr5BOeOfN5SM55~iR7ww;UGoHN)ll;iuVUMv3yIpKRc<`=r*Q&4hmX|Yro~{Z`=)~l6<#~BeZ+1t@W}3#(J5rA`%`f-zXXm(QlHV=Lvk!N9&#J97=V-u5Wr(_6DZ@ovfhZs8d7(^1dD?f0pHE&u?CNv7iY-?nm@ef#4rv<B;L$@D)u8gEa`RyDu7X{)MLq9wDP@GH=Y3j!yIs`t1#@FAi+X-{+HYTpdXBt_cQZEKJG&uU>)7%Bhlo7w8geNqc7HWs{k@i~-o!DPUofVKkICH3zU2<$x*r5BIVr9?y5|vJy5GidA5*teo_#s*YpMahtbh5YtvaHgD4*e-x;ip=LGjP!G6w$}oO|J?zI0`<$K>PpMc#hDoV!@$tt@<;o5<Ug(pzd#CZqc<+b7Coaqz}#PU7p<)Tmt?JIDP?#(pExZ0O_DOQh+vt{_FEX`HGPM4Hu}?y02d?l2>1V&BD+5I#N7HPKSP`fP;PCl6n&xmCQByuiI$(CYGriWLZJ?7eSj`?p(#aEasZ^ADVUQiyQHtM6hJUE8cDe2%_6prE2Nfg7b6oZIZIwHe{-^Y8vJnOE#acx}0SPM>k}2(0LM>}6Sb0fC49ylwchin4Nq`-Xfjvs|lr3gOLOuQoU*jw5!~V868P!wjd32=CjT_fvsJeG}pAy&<DdG--Sn;hL*tn;+h^JVf~X)YdY`T^C6PcsBP7x7&s*H4v^R?|JIt)%8S2TaO9asrh>taRIB6UU@poAGId0jZc=Y|0U*hTjGZg`xrciC80Ux^uIpaI-=2T0@pn}P4)FOhc4vRvU{Otbl1DYhT1un+*$L>(J*q33B%JHoNE_}aFfQ5u6~kV(VOVs`8~_K)y^O`vEb^eS%&UElMfQ(OMSw2B>k3%aMHZE_9-V$jUY5jhD6sfEgFSz9hP^A=W6C^)^%Vnl9-R$vG)ng?d)i(PV!PamiuDPiEuTR&)Sigjpm97*I;=f!mcbQwIgvR&6iQPYO>tcj=h6mUW>3N%lVL(S}cFIXFOxBhWh8l@@(v{&vHh@$GiTZ&UZ&p*YPgC(Cj<uNaGWd*CKt42l0T==HVQ)J3dgO9j+_sN82Rr$9RO3jAKkfWC<|^$LJtGXy>s;8;>>m2COk|A+OosOI;;xjA5kn#r4qUd)erJ8{Z<nxSrT=-4^lQM+g19+dJeT;-Qa2`o+v<?gqs{zj2n`VP8-T^c@e_ecB1ifpG&K9^TXaFpja99~>0Mmm_3!t!~cLwjFh$ckgTsdIsz9oebi{5_V^Pai#aJGt}rew!z_}UmxU1?|WUChrVtU^ka<G(T?)$<V54=Bxf4u_JegXKR6D2p&wp@?;Q{iq0*qumjfT@yV~I!7Q}O|c2ou>j#O6j?Ww&p!KJ`F0Sz(6j~EkUo|6wX#@8Abdgk~Q6z}IRAY>Bv13#E=bc1>55AfXy<I>hZA7iMSfRDah4tN-A&2XSLd$x?;`Q2_b_WK<8!dNL5#xdVm2=g%pKIlMgKm)jV=&Mp(sJ(qrjoRSzP-Fg(?M!vJ4y=Q5L^mgD-veMA{o^;z)b`hcHs*7v{1}goV);gA7ix<Ot5aRM;Yj^Sk&OEC!%o!ht%LKzn5BagwQ+%N)PLP|p+4s*;G>W11#QghCfQRvYH+1G`wZx!f7dxv`z^pZV|*h3J<MzR!90w$8`{x0cskUWzhXX%v2AUjhp{6D%Xo*MK#e&BenrB#YAWDijOvAQ1V0=H^e`Wd0y>z76~Q_f<G*C~cLDH^`6~uPm_wjL$2h)+9ram8kO$_=eZ_bM_(uOb684F)QbT9z7fV2{n76LAr?Jdpd}oEct(yzY*F_&YxEhTERzN@I2nBZ3Cz#!6KA?d%=C`ka4(6ryjx=Ah$f$2ha-}w`gFW?;_kcgl3ogMtjEVeQs4tuka>YFN49EfFyOEC6h7E>!_+Egv0OM{WjAM@UJ+v`~Y~w<GUA`;LRg!@&#!Cq<G>*Fn`@(!Xz=8VECayF#86u;(N|_z?`;paYEaNVt@yAW5G3FfXK;Hv)+EHH~27F?S=Zf+e=TRO8{9!B==|bbTU!AGnc7k<Cz5(+w{|dzW4f$fR1GU*LoH&wa#X>*E+UH=un5XY@q%r4x7{_?Fg){ZX1KenQ6^wQ(rO|basWJaq3iL2HyvAamg>YW@9+2lqZE9a<>gPXz`Ivi5!x$9uoiQ*E^N)0Vdxw~BvJ3SUnXn&x56Fe}F@CKF{g_Wpz;l6IVIAP(`^#|j;gHkax1;gVjOx?}`~l+;DnqPEYZNkh`UR_}0l#`dFZ~i1qco}&+B6;Zgy}M~bXs0(jN{YsYZ~^^FLcI;RDm=lG<qrV+n&;>4AW_iN<qu(W5|~=x&ZxKWf*iynaOhst-*-j{9^euUeDvlzZj(+Si!G*;usgFGwD^l6|eF5^$tLNHb*;pn}k%Zr(UJzjoDeeLYvB)kr=Q};YdMmFovlGO)72o<ITo6VT4A|rZZ$cKeB2^r8wB9LWL-So~fx^EU(mL(!a@J8UaN_8*#jTBy3PR(o>sSrMDkQmVq%s77t5`qA&{N<0vIA*{MYQ|JXU@_RV}gm7WYIJum4n6(~b`+d$=JIqAB{N2QRZoS3uVyai*W+8DU1XdKfcgG31_`*8L|)Z#h_8@MWZD*3kCe-vmBPO*iMO^&N-mt1L36J28yDmjc4c+>@$K(qpB%9Yww4bvUz9U$M0<B3|O=@3zb34k3{QDH8jPH7s{z&B}gQGzyuPbIK{`zPxLhCl@qrUQ<-e@Gx&Fbmpn-XNrF$+fe2sU>%Uf>AnMhAGQ7a&njwj*7{GfViqgr%dgyB|i*+iR|VYh-O9#8YA3S$|v$vd27jv_%i|t165>0SyCCo`Ang5K(r_Wl^PQtqZIU9Up^ZP3XdbbRfZYEB0|*`!7{Ml3Ls^J_&WmHpaY_HsT_RMp)mC0jiYpWI0&>^T)a}B4yGqgt;8)Ot3+YIv`B6xm;}0If&eXvbYl5T-AGCM%^_5k09{D@aRyjA=oIxygp<+sNZXXSRacfqsjAx7AFPjFNVVNb7ZF#J>>pr7CcW8cuTphdO*WOHc!f#hjToYNqcRnQMs1FP?c5duwW)X*{CkFz6SU0qzY9q;((geF7Yqzf$^dc-S^(Ys_))}KNH+pH^C_4?Uez8l<!o*=XHY4@q75)9joc{iQxpE2LyIoUh`*81(4WM_sIv`%%3$NiGH)HBe&OxIa0f^7X1PhDc^6VPxRX?HHSa)CW*JPHcdZncrH1?c4g~U>8ul}ma9#xrDOIR1u|m3#+Dn(3E&0KXjlN`scBE1x*u+nc6wJ0MCBqd@)01PeHOels&9OLPbmexWzxerQS2DE^wnloH;M(^UjERC#9fLOv9U4Kfg&hxf3oNFsp>Vx2gGWD?21f<)Mt`_gw(t^B!Zr!et@Z|4z*V<77Y(*vr_rF68*Cx;(+%X+f=a_z{$lGLD}iRdb1|@uN)<3$Nvk)n%j7XKutgK@mY}gcGD0ud<ThR}q^H{!2Jlw7zCsIs00N=fLPbHdb9v{k<1#?<BXmlAY9*SS*JRmV|DT(;g~fyoq2Ry67nLo{q&Mhnt{baNPt|EO?>x2twwA52C?U;gTk8L2yS9cRl?LP654w`$|8@g=X>`N@z5lZG(@AcgEqu{7*<uc8=#}qQ9b!J-yU6?Phq%{w?2J@eBQeSB$DIF3Xzw}0%A;Vkx&H$Iy>D>'


def get_delmar_item_struct_bytes() -> bytes:
    return zlib.decompress(_b64.b85decode(_DELMAR_ITEM_STRUCT_B85))


def get_light_ebp_struct_bytes() -> bytes:
    return zlib.decompress(_b64.b85decode(_LIGHT_EBP_STRUCT_B85))


def get_light_nopol_struct_bytes() -> bytes:
    return zlib.decompress(_b64.b85decode(_LIGHT_NOPOL_STRUCT_B85))


def load_templates():
    print("[+] Loading Frostbite template assets from Bundle 39 and items.toc...")
    tpl = load_template_assets()
    item_tpl = get_delmar_item_struct_bytes()
    imc_tpl = tpl.get("imc_template_ebx")
    if not imc_tpl:
        raise RuntimeError("Could not load Items/ItemMasterCollection from Data/Win32/items.toc!")
    light_effect_ebx = get_light_ebp_struct_bytes()
    return tpl, item_tpl, imc_tpl, light_effect_ebx

