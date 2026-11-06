"""
ReSkate Prop Studio - compiler_bridge.py
Internal Addon Bridge Module for non-destructive origin-relative exporting of
geometry, custom collision objects, pass-through / baked textures, thumbnails,
explicitly linked dynamic lights, vertex grind paths, and manifests, and
executing compile_blender_prop.py standalone compiler.
"""

from __future__ import annotations

import concurrent.futures
import filecmp
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

import bmesh
import bpy
import numpy as np
import struct
from mathutils import Euler, Matrix, Vector

try:
    from PIL import Image as _PIL_Image
except ImportError:
    _PIL_Image = None


class _NumpyCompatImage:
    """Zero-dependency NumPy + zlib + Blender image shim when Pillow (PIL) is not installed in Blender's embedded Python."""

    class Resampling:
        NEAREST = 0
        BILINEAR = 2
        BICUBIC = 3
        LANCZOS = 1

    def __init__(self, arr: np.ndarray, mode: str = "RGBA"):
        a = np.asarray(arr, dtype=np.uint8)
        if a.ndim == 2:
            if mode == "L":
                self._arr = a.copy()
                self.mode = "L"
            else:
                self._arr = np.dstack([a, a, a, np.full_like(a, 255)])
                self.mode = "RGBA"
        elif a.ndim == 3 and a.shape[2] == 3:
            if mode == "RGB":
                self._arr = a.copy()
                self.mode = "RGB"
            else:
                self._arr = np.dstack([a, np.full((a.shape[0], a.shape[1]), 255, dtype=np.uint8)])
                self.mode = "RGBA"
        elif a.ndim == 3 and a.shape[2] >= 4:
            self._arr = a[:, :, :4].copy()
            self.mode = "RGBA"
        else:
            self._arr = np.zeros((1, 1, 4), dtype=np.uint8)
            self.mode = "RGBA"

    @property
    def size(self) -> tuple[int, int]:
        return (int(self._arr.shape[1]), int(self._arr.shape[0]))

    def copy(self) -> "_NumpyCompatImage":
        return _NumpyCompatImage(self._arr.copy(), mode=self.mode)

    def __array__(self, dtype=None):
        return self._arr.astype(dtype) if dtype is not None else self._arr

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def convert(self, mode: str = "RGBA") -> "_NumpyCompatImage":
        m = (mode or "RGBA").upper()
        if m == self.mode:
            return self.copy()
        if m == "RGBA":
            if self._arr.ndim == 2:
                return _NumpyCompatImage(np.dstack([self._arr, self._arr, self._arr, np.full_like(self._arr, 255)]), "RGBA")
            if self._arr.shape[2] == 3:
                return _NumpyCompatImage(np.dstack([self._arr, np.full(self._arr.shape[:2], 255, dtype=np.uint8)]), "RGBA")
            return _NumpyCompatImage(self._arr[:, :, :4], "RGBA")
        if m == "RGB":
            if self._arr.ndim == 2:
                return _NumpyCompatImage(np.dstack([self._arr, self._arr, self._arr]), "RGB")
            return _NumpyCompatImage(self._arr[:, :, :3], "RGB")
        if m == "L":
            if self._arr.ndim == 2:
                return self.copy()
            lum = np.clip(np.round(0.299 * self._arr[:, :, 0] + 0.587 * self._arr[:, :, 1] + 0.114 * self._arr[:, :, 2]), 0, 255).astype(np.uint8)
            return _NumpyCompatImage(lum, "L")
        return self.copy()

    def resize(self, size: tuple[int, int], resample=None) -> "_NumpyCompatImage":
        new_w, new_h = max(1, int(size[0])), max(1, int(size[1]))
        old_h, old_w = self._arr.shape[:2]
        if (old_w, old_h) == (new_w, new_h):
            return self.copy()
        ys = np.clip((np.arange(new_h, dtype=np.float32) + 0.5) * (old_h / float(new_h)) - 0.5, 0.0, max(0, old_h - 1))
        xs = np.clip((np.arange(new_w, dtype=np.float32) + 0.5) * (old_w / float(new_w)) - 0.5, 0.0, max(0, old_w - 1))
        y0 = np.floor(ys).astype(np.int32)
        x0 = np.floor(xs).astype(np.int32)
        y1 = np.minimum(y0 + 1, max(0, old_h - 1))
        x1 = np.minimum(x0 + 1, max(0, old_w - 1))
        wy = (ys - y0).astype(np.float32)[:, None]
        wx = (xs - x0).astype(np.float32)[None, :]
        src = self._arr.astype(np.float32)
        if src.ndim == 3:
            wy = wy[:, :, None]
            wx = wx[:, :, None]
        top = src[np.ix_(y0, x0)] * (1.0 - wx) + src[np.ix_(y0, x1)] * wx
        bot = src[np.ix_(y1, x0)] * (1.0 - wx) + src[np.ix_(y1, x1)] * wx
        out = np.clip(np.round(top * (1.0 - wy) + bot * wy), 0, 255).astype(np.uint8)
        return _NumpyCompatImage(out, mode=self.mode)

    def save(self, fp, fmt: str = "PNG"):
        rgba = self.convert("RGBA")._arr
        h, w = rgba.shape[:2]
        raw_rows = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

        def _chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
        png_bytes = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw_rows, 6)) + _chunk(b"IEND", b"")
        if hasattr(fp, "write"):
            fp.write(png_bytes)
        else:
            p = Path(fp)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(png_bytes)

    @classmethod
    def fromarray(cls, arr: np.ndarray, mode: str = "RGBA") -> "_NumpyCompatImage":
        return cls(arr, mode=mode)

    @classmethod
    def new(cls, mode: str, size: tuple[int, int], color=0) -> "_NumpyCompatImage":
        w, h = max(1, int(size[0])), max(1, int(size[1]))
        m = (mode or "RGBA").upper()
        if m == "L":
            c = int(color[0]) if hasattr(color, "__len__") else int(color)
            return cls(np.full((h, w), c, dtype=np.uint8), mode="L")
        if hasattr(color, "__len__"):
            c_tup = list(color) + [255] * 4
        else:
            c_tup = [int(color), int(color), int(color), 255]
        ch = 3 if m == "RGB" else 4
        arr = np.empty((h, w, ch), dtype=np.uint8)
        for i in range(ch):
            arr[:, :, i] = int(max(0, min(255, c_tup[i])))
        return cls(arr, mode=m)

    @classmethod
    def open(cls, fp) -> "_NumpyCompatImage":
        tmp_path = None
        try:
            if hasattr(fp, "read"):
                data = fp.read()
                tmp_path = Path(DEFAULT_STAGING_DIR) / f"_tmp_compat_open_{os.getpid()}.png"
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_bytes(data)
                load_path = str(tmp_path)
            else:
                load_path = str(fp)
            bl_im = bpy.data.images.load(load_path, check_existing=False)
            try:
                w, h = int(bl_im.size[0]), int(bl_im.size[1])
                ch = int(getattr(bl_im, "channels", 4) or 4)
                buf = np.empty(w * h * ch, dtype=np.float32)
                bl_im.pixels.foreach_get(buf)
                arr = buf.reshape((h, w, ch))[::-1, :, :].copy()
                u8 = np.clip(np.round(arr * 255.0), 0, 255).astype(np.uint8)
                return cls(u8, mode="RGBA")
            finally:
                bpy.data.images.remove(bl_im)
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


Image = _PIL_Image if _PIL_Image is not None else _NumpyCompatImage

from . import properties
from .properties import (
    BLENDER_ORG_CATALOG,
    GRIND_SURFACE_CODE_MAP,
    LOOK_PRESETS,
    PRIMARY_SURFACE_CODE_MAP,
    apply_prevent_wall_snap_code,
    auto_detect_default_paths,
    get_addon_preferences,
    get_or_init_object_prop_state,
    get_surface_behavior_badges,
    get_surface_label,
    get_surface_packed_code,
    is_bouncy_surface_code,
    is_collision_helper_object,
    is_do_not_align_surface_code,
    is_no_grind_surface_code,
    load_persisted_user_paths,
    resolve_active_prop_mesh,
    resolve_collision_to_owner_prop,
    save_object_prop_state,
    strip_do_not_align_code,
    sync_active_object_props,
)

ADDON_VERSION = "0.33.0"
ADDON_VERSION_STR = ADDON_VERSION
COMPILER_VERSION = "0.33.0"
RESKATE_DISCORD_INVITE_URL = "https://discord.gg/reskate"
GITHUB_REPO_URL = "https://github.com/CameronTutt/ReSkate-Prop-Studio"
GITHUB_RELEASES_API_URL = "https://api.github.com/repos/CameronTutt/ReSkate-Prop-Studio/releases/latest"

# Once-per-session non-blocking background GitHub update check state
_SESSION_UPDATE_CHECK_STARTED: bool = False
_SESSION_UPDATE_AVAILABLE_TAG: str = ""
_SESSION_UPDATE_HTML_URL: str = ""
_SESSION_UPDATE_DISMISSED: bool = False


def parse_version_tuple(v_str: str) -> tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", str(v_str or ""))]
    return tuple(parts) if parts else (0, 0, 0)


def _tag_redraw_all_view3d_for_update_banner():
    try:
        wm = getattr(bpy.context, "window_manager", None)
        if wm:
            for win in wm.windows:
                if win.screen:
                    for area in win.screen.areas:
                        if area.type == 'VIEW_3D':
                            area.tag_redraw()
    except Exception:
        pass
    return None


def _background_session_update_worker() -> None:
    global _SESSION_UPDATE_AVAILABLE_TAG, _SESSION_UPDATE_HTML_URL
    try:
        import urllib.request
        req = urllib.request.Request(
            GITHUB_RELEASES_API_URL,
            headers={"User-Agent": f"ReSkatePropStudio/{ADDON_VERSION_STR}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag_name = str(payload.get("tag_name") or "").strip().lstrip("vV")
        html_url = str(payload.get("html_url") or f"{GITHUB_REPO_URL}/releases/latest")
        if tag_name and parse_version_tuple(tag_name) > parse_version_tuple(ADDON_VERSION_STR):
            _SESSION_UPDATE_AVAILABLE_TAG = tag_name
            _SESSION_UPDATE_HTML_URL = html_url
            try:
                if hasattr(bpy.app, "timers"):
                    bpy.app.timers.register(_tag_redraw_all_view3d_for_update_banner, first_interval=0.05)
            except Exception:
                pass
    except Exception:
        pass


def ensure_session_update_check() -> None:
    """Fires a single non-blocking daemon thread once per Blender session to check GitHub Releases."""
    global _SESSION_UPDATE_CHECK_STARTED
    if _SESSION_UPDATE_CHECK_STARTED or bpy.app.background:
        return
    _SESSION_UPDATE_CHECK_STARTED = True
    try:
        t = threading.Thread(target=_background_session_update_worker, name="ReSkateSessionUpdateCheck", daemon=True)
        t.start()
    except Exception:
        pass

# Module-level Face Surface Attribute & JSON Property Constants
FACE_SURFACE_ATTR_NAME = "reskate_face_surface"
FACE_SURFACES_JSON_PROP = "reskate_face_surfaces_json"
FACE_SURFACE_LEGEND_JSON_PROP = "reskate_face_surface_legend_json"


def _get_staging_dir() -> Path:
    env_dir = os.environ.get("RESKATE_STAGING_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "ReSkate_Prop_Studio" / "Staging"
    return Path.home() / "AppData" / "Local" / "ReSkate_Prop_Studio" / "Staging"


class ProgressStatus(tuple):
    """Immutable (label, percent) tuple for generator progress yielding."""
    def __new__(cls, label: str, percent: float):
        return super().__new__(cls, (str(label), float(percent)))

    @property
    def label(self) -> str:
        return self[0]

    @property
    def percent(self) -> float:
        return self[1]

    def __repr__(self) -> str:
        return f"ProgressStatus(label='{self[0]}', percent={self[1]:.1f})"


_AUTO_PATHS = auto_detect_default_paths()
DEFAULT_STAGING_DIR = Path(_AUTO_PATHS.get("staging_dir") or _get_staging_dir())
STUDIO_DIR = Path(_AUTO_PATHS.get("studio_dir") or Path(__file__).resolve().parent / "bundled_compiler")
GAME_MODS_DIR = (Path(_AUTO_PATHS["game_dir"]) / "Mods") if _AUTO_PATHS.get("game_dir") else (DEFAULT_STAGING_DIR / "Mods")


def get_game_dir(context=None) -> Path:
    """Returns the user's configured Skate game directory (where Skate.exe and Data/Win32/buildkititems.toc live)."""
    prefs = get_addon_preferences(context)
    if prefs is not None:
        gd = str(getattr(prefs, "game_dir", "") or "").strip()
        if gd:
            return Path(gd)
    saved = load_persisted_user_paths()
    if saved.get("game_dir"):
        return Path(saved["game_dir"])
    env_gd = os.environ.get("RESKATE_GAME_DIR", "").strip()
    if env_gd:
        return Path(env_gd)
    auto_gd = _AUTO_PATHS.get("game_dir", "")
    if auto_gd:
        return Path(auto_gd)
    return Path()


def is_game_dir_valid(game_dir: str | Path | None = None, context=None) -> bool:
    """Returns True only if `<Skate>\\Data\\Win32\\buildkititems.toc` exists."""
    global _GAME_DIR_VALID_CACHE
    gd = Path(game_dir) if (game_dir and str(game_dir).strip()) else get_game_dir(context)
    if not gd or str(gd) in ("", "."):
        return False
    gd_str = str(gd)
    now = time.monotonic()
    if _GAME_DIR_VALID_CACHE[1] == gd_str and (now - _GAME_DIR_VALID_CACHE[0]) < 3.0:
        return _GAME_DIR_VALID_CACHE[2]
    val = (gd / "Data" / "Win32" / "buildkititems.toc").is_file()
    _GAME_DIR_VALID_CACHE = (now, gd_str, val)
    return val


def get_reskate_cli_status(game_dir: str | Path | None = None, context=None) -> dict:
    """
    Checks ReSkate Studio installation inside the user's Skate folder:
      - Recommended: `<Skate>\\ReSkate Studio\\reskate_cli.exe`
      - Also accepted: `<Skate>\\reskate_cli.exe` (with soft notice pointing to recommended location)
      - Missing: returns error message + Discord link
    """
    global _CLI_STATUS_CACHE
    gd = Path(game_dir) if (game_dir and str(game_dir).strip()) else get_game_dir(context)
    gd_str = str(gd) if gd else ""
    now = time.monotonic()
    if _CLI_STATUS_CACHE[1] == gd_str and _CLI_STATUS_CACHE[2] and (now - _CLI_STATUS_CACHE[0]) < 3.0:
        return dict(_CLI_STATUS_CACHE[2])
    if gd and gd_str not in ("", "."):
        rec_cli = gd / "ReSkate Studio" / "reskate_cli.exe"
        if rec_cli.is_file():
            res = {
                "ok": True,
                "status_kind": "RECOMMENDED",
                "cli_path": str(rec_cli),
                "studio_dir": str(rec_cli.parent),
                "message": f"Found: {rec_cli}",
                "notice": "",
            }
            _CLI_STATUS_CACHE = (now, gd_str, res)
            return dict(res)
        alt_cli = gd / "reskate_cli.exe"
        if alt_cli.is_file():
            res = {
                "ok": True,
                "status_kind": "ALT_ROOT",
                "cli_path": str(alt_cli),
                "studio_dir": str(alt_cli.parent),
                "message": f"Found at Skate root: {alt_cli}",
                "notice": "Recommended location: <Skate>\\ReSkate Studio\\reskate_cli.exe",
            }
            _CLI_STATUS_CACHE = (now, gd_str, res)
            return dict(res)
    res_miss = {
        "ok": False,
        "status_kind": "MISSING",
        "cli_path": "",
        "studio_dir": "",
        "message": (
            f"ReSkate Studio is required. Download it from the ReSkate Discord ({RESKATE_DISCORD_INVITE_URL}) "
            "and unzip it into your Skate folder so this file exists: <Skate>\\ReSkate Studio\\reskate_cli.exe."
        ),
        "notice": "",
    }
    _CLI_STATUS_CACHE = (now, gd_str, res_miss)
    return dict(res_miss)


def get_studio_dir(context=None) -> Path:
    """Returns the ReSkate Studio directory inside `<Skate>\\ReSkate Studio` (or bundled_compiler fallback for scripts)."""
    cli_st = get_reskate_cli_status(context=context)
    if cli_st.get("ok") and cli_st.get("studio_dir"):
        return Path(cli_st["studio_dir"])
    prefs = get_addon_preferences(context)
    if prefs is not None:
        sd = str(getattr(prefs, "studio_dir", "") or "").strip()
        if sd:
            return Path(sd)
    saved = load_persisted_user_paths()
    if saved.get("studio_dir"):
        return Path(saved["studio_dir"])
    return STUDIO_DIR


def get_game_mods_dir(context=None) -> Path:
    """Returns `<Game Folder>/Mods`."""
    gd = get_game_dir(context)
    if gd and str(gd) not in ("", "."):
        return gd / "Mods"
    return get_staging_dir(context) / "Mods"


def get_staging_dir(context=None) -> Path:
    """Returns the configured Staging / Build Cache folder (defaults to %LOCALAPPDATA%\\ReSkate_Prop_Studio\\Staging)."""
    ctx = context if context is not None else bpy.context
    scn = ctx if isinstance(ctx, bpy.types.Scene) else (getattr(ctx, "scene", None) if ctx else getattr(bpy.context, "scene", None))
    prop = getattr(scn, "reskate_prop", None) if scn else None
    if prop is not None:
        st_prop = str(getattr(prop, "staging_dir", "") or "").strip()
        if st_prop:
            return Path(st_prop)
    prefs = get_addon_preferences(context)
    if prefs is not None:
        st = str(getattr(prefs, "staging_dir", "") or "").strip()
        if st:
            return Path(st)
    saved = load_persisted_user_paths()
    if saved.get("staging_dir"):
        return Path(saved["staging_dir"])
    return _get_staging_dir()


def get_author_name(context=None, *_args, **_kwargs) -> str:
    """Returns the required Author Name from Add-on Preferences (empty by default)."""
    prefs = get_addon_preferences(context)
    if prefs is not None:
        auth = str(getattr(prefs, "author_name", "") or "").strip()
        if auth:
            return auth
        return os.environ.get("RESKATE_AUTHOR_NAME", "").strip()
    saved = load_persisted_user_paths()
    if saved.get("author_name"):
        return str(saved["author_name"]).strip()
    env_auth = os.environ.get("RESKATE_AUTHOR_NAME", "").strip()
    if env_auth:
        return env_auth
    return ""


def get_resolved_target_mod_name(context=None, target_mod_override: str | None = None, *_args, **_kwargs) -> str:
    """
    Formats the compiled mod folder name as `<Author>-<PackName>` using `author_name` from Preferences
    and the user's Prop Pack Name on the Ship page.
    """
    ctx = context if context is not None else bpy.context
    scn = ctx if isinstance(ctx, bpy.types.Scene) else (getattr(ctx, "scene", None) if ctx else getattr(bpy.context, "scene", None))
    prop = getattr(scn, "reskate_prop", None) if scn else None
    if target_mod_override is not None and not isinstance(target_mod_override, str):
        if hasattr(target_mod_override, "target_mod_name"):
            target_mod_override = str(getattr(target_mod_override, "target_mod_name", "") or "").strip() or None
        else:
            target_mod_override = None
    raw_pack = str(target_mod_override or (getattr(prop, "target_mod_name", "") if prop else "") or "CustomProps").strip()
    if raw_pack.lower().endswith(".fbmod"):
        raw_pack = raw_pack[:-6].strip()
    if raw_pack.lower().endswith("-blenderauthoredprops_test"):
        raw_pack = "CustomProps"
    if not raw_pack:
        raw_pack = "CustomProps"

    author = get_author_name(ctx)
    if author:
        clean_author = re.sub(r'[<>:"/\\|?*]+', "", author).strip()
        if clean_author and not raw_pack.lower().startswith(f"{clean_author.lower()}-"):
            # If raw_pack had a previous author prefix `<OldAuthor>-<Pack>`, keep `<Pack>` if `<Author>-<Pack>` is cleaner
            if "-" in raw_pack:
                parts = raw_pack.split("-", 1)
                pack_tail = parts[1].strip() or raw_pack
                cand_with_author = f"{clean_author}-{pack_tail}"
                if (get_staging_dir(ctx) / "Packs" / raw_pack / "pack_registry.json").is_file():
                    return raw_pack
                return cand_with_author
            return f"{clean_author}-{raw_pack}"
    return raw_pack


def get_last_compiler_log_text(context=None) -> str:
    """Returns the most recent compiler log text from Add-on Preferences or Scene settings."""
    ctx = context if context is not None else bpy.context
    prefs = get_addon_preferences(ctx)
    if prefs is not None:
        pref_log = str(getattr(prefs, "last_compile_log", "") or "").strip()
        if pref_log:
            return pref_log
    scn = ctx if isinstance(ctx, bpy.types.Scene) else (getattr(ctx, "scene", None) if ctx else getattr(bpy.context, "scene", None))
    prop = getattr(scn, "reskate_prop", None) if scn else None
    if prop is not None:
        for attr in ("last_compile_log", "compile_log_text"):
            val = str(getattr(prop, attr, "") or "").strip()
            if val:
                return val
    return ""


def get_compile_pipeline_mode(context=None) -> str:
    """Returns 'FAST_OPTIMIZED' (default) or 'LEGACY_FULL' from Add-on Preferences (or scene override)."""
    ctx = context if context is not None else bpy.context
    prefs = get_addon_preferences(ctx)
    if prefs is not None:
        pm = str(getattr(prefs, "default_compile_pipeline_mode", "FAST_OPTIMIZED") or "FAST_OPTIMIZED").strip().upper()
        if pm in ("FAST_OPTIMIZED", "LEGACY_FULL"):
            return pm
    scn = getattr(ctx, "scene", None) if ctx else None
    prop = getattr(scn, "reskate_prop", None) if scn else None
    if prop is not None:
        m = str(getattr(prop, "compile_pipeline_mode", "") or "").strip().upper()
        if m in ("FAST_OPTIMIZED", "LEGACY_FULL"):
            return m
    return "FAST_OPTIMIZED"


def build_compiler_env(context=None) -> dict[str, str]:
    """Builds a subprocess environment dict with RESKATE_GAME_DIR, RESKATE_STUDIO_DIR, RESKATE_STAGING_DIR, RESKATE_CLI_EXE, RESKATE_AUTHOR_NAME, and PYTHONPATH."""
    import site
    env = os.environ.copy()
    gd = get_game_dir(context)
    cli_st = get_reskate_cli_status(gd, context=context)
    env["RESKATE_GAME_DIR"] = str(gd) if gd else ""
    env["RESKATE_STUDIO_DIR"] = str(get_studio_dir(context))
    env["RESKATE_STAGING_DIR"] = str(get_staging_dir(context))
    env["RESKATE_PIPELINE_MODE"] = get_compile_pipeline_mode(context)
    env["RESKATE_AUTHOR_NAME"] = get_author_name(context)
    env["RESKATE_COMPILER_VERSION"] = COMPILER_VERSION
    if cli_st.get("cli_path"):
        env["RESKATE_CLI_EXE"] = str(cli_st["cli_path"])

    # Ensure site.getusersitepackages() is on PYTHONPATH so --user pip installs are always importable
    try:
        user_site = site.getusersitepackages()
        if user_site:
            cur_pp = env.get("PYTHONPATH", "")
            pp_parts = [p for p in cur_pp.split(os.pathsep) if p]
            if user_site not in pp_parts:
                pp_parts.append(user_site)
            env["PYTHONPATH"] = os.pathsep.join(pp_parts)
    except Exception:
        pass
    return env


def clamp_slug(slug: str, max_len: int = 36) -> str:
    """Clamps generated slug length <= max_len (default 36) using deterministic CRC32 truncation."""
    slug = re.sub(r"__+", "_", str(slug or "").strip())
    if len(slug) <= max_len:
        return slug
    trunc_len = max_len - 9  # 27 chars for 36 max_len
    crc = zlib.crc32(slug.encode("utf-8", errors="replace")) & 0xFFFFFFFF
    prefix = slug[:trunc_len].rstrip("_")
    return f"{prefix}_{crc:08x}"


def get_target_mesh_object(context) -> bpy.types.Object | None:
    """Returns the active object if it is a valid mesh with polygons. Resolves _COL / simplified collision proxies to owner prop mesh."""
    obj = resolve_active_prop_mesh(context)
    if not obj or obj.type != 'MESH':
        return None
    if len(obj.data.polygons) == 0:
        return None
    return obj


def _deduplicate_prop_slug_stem(raw_str: str) -> str:
    """
    Strips redundant Quick Drop / pack prefixes (e.g. `own_bkcustomprops_`, `own_customprops_`,
    `bkcustomprops_`) and collapses consecutive duplicate tokens so prop keys stay clean.
    """
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in str(raw_str or "").lower()).strip("_")
    changed = True
    while changed:
        changed = False
        for pfx in (
            "own_bkcustomprops_",
            "own_customprops_",
            "own_bkcustom_",
            "bkcustomprops_",
            "customprops_",
            "bk_custom_prop_",
            "bk_custom_props_",
        ):
            if s.startswith(pfx) and len(s) > len(pfx):
                s = s[len(pfx):].strip("_")
                changed = True
    # Collapse consecutive identical tokens
    tokens = [t for t in s.split("_") if t]
    dedup_tokens: list[str] = []
    for t in tokens:
        if len(dedup_tokens) >= 1 and dedup_tokens[-1] == t:
            continue
        dedup_tokens.append(t)
    res = "_".join(dedup_tokens).strip("_")
    return res or "prop"


def clean_prop_display_name(raw_name: str, author_name: str = "", target_mod: str = "") -> str:
    """
    Cleans redundant internal prefixes from a prop's in-game Display Name while preserving user free text.
    """
    s = str(raw_name or "").strip()
    if not s:
        return "Custom Prop"
    s = re.sub(r"^own_(?:bk)?[a-z0-9]+_", "", s, flags=re.IGNORECASE).strip()
    changed = True
    while changed and s:
        changed = False
        for pfx in (
            "BK Custom Props - ",
            "BK Custom Prop - ",
            "BK Custom Props ",
            "BK Custom Prop ",
            "bkcustomprops ",
            "bkcustomprops_",
            "customprops_",
        ):
            if s.lower().startswith(pfx.lower()) and len(s) > len(pfx):
                s = s[len(pfx):].strip(" -_:")
                changed = True
                break
    return s or str(raw_name or "Custom Prop").strip()


def get_prop_display_name(obj_or_ctx=None, context=None) -> str:
    """Returns the resolved in-game Display Name for a prop mesh object."""
    if isinstance(obj_or_ctx, bpy.types.Object):
        obj = obj_or_ctx
        ctx = context if context is not None else bpy.context
    elif isinstance(context, bpy.types.Object):
        obj = context
        ctx = obj_or_ctx if obj_or_ctx is not None else bpy.context
    else:
        obj = None
        ctx = obj_or_ctx if obj_or_ctx is not None else bpy.context

    if obj is not None:
        resolved = resolve_collision_to_owner_prop(obj)
        if resolved is not None and getattr(resolved, "type", None) == 'MESH':
            obj = resolved
        custom_disp = str(obj.get("reskate_display_name", "") or "").strip()
        if custom_disp:
            return clean_prop_display_name(custom_disp)
        try:
            st = get_or_init_object_prop_state(obj)
            st_disp = str(st.get("prop_display_name", "") or "").strip()
            if st_disp:
                return clean_prop_display_name(st_disp)
        except Exception:
            pass
        return clean_prop_display_name(obj.name)

    scn = ctx if isinstance(ctx, bpy.types.Scene) else (getattr(ctx, "scene", None) if ctx else getattr(bpy.context, "scene", None))
    prop = getattr(scn, "reskate_prop", None) if scn else None
    if prop and getattr(prop, "prop_display_name", ""):
        return clean_prop_display_name(prop.prop_display_name)
    return "Custom Prop"


_KNOWN_PACK_TAG_RE = re.compile(r"^[a-z0-9]{4,6}_")


def compute_pack_namespace_tag(context, target_mod_override: str | None = None) -> str:
    """
    Computes a deterministic 5-character alphanumeric namespace tag (`<2-char-slug><3-hex-sha1>`)
    from `author_name` + `target_mod_name` so props in different prop packs or by different authors
    never collide on `prop_key` / GUIDs even when they share generic names like 'Rail' or 'Ledge'.
    """
    target_mod = get_resolved_target_mod_name(context, target_mod_override=target_mod_override)
    author = get_author_name(context)

    clean_mod = re.sub(r"[^a-z0-9]+", "", target_mod.lower())
    clean_auth = re.sub(r"[^a-z0-9]+", "", author.lower())
    if clean_auth and clean_mod.startswith(clean_auth) and len(clean_mod) > len(clean_auth) + 1:
        clean_mod = clean_mod[len(clean_auth):]
    prefix2 = (clean_mod[:2] or clean_auth[:2] or "pk").ljust(2, "x")
    digest3 = hashlib.sha1(f"{clean_auth}:{target_mod.lower()}".encode("utf-8")).hexdigest()[:3]
    return f"{prefix2}{digest3}"


def _strip_any_pack_tag_prefix(stem: str, obj=None) -> str:
    """Strips a previously applied auto-pack tag prefix if the user switched `target_mod_name` to a new pack."""
    s = str(stem or "").strip("_")
    if obj is not None:
        prev_tag = str(obj.get("reskate_pack_ns_tag", "") or "").strip().lower()
        if prev_tag and s.startswith(f"{prev_tag}_") and len(s) > len(prev_tag) + 1:
            return s[len(prev_tag) + 1:]
    if len(s) > 6 and s[5] == "_" and all(c in "0123456789abcdef" for c in s[2:5]):
        return s[6:]
    return s


def _find_legacy_unprefixed_slug_in_pack(target_mod: str, clean_stem: str) -> str | None:
    """
    If `own_customprops_<clean_stem>` (or any matching stem) already exists in `Packs/<target_mod>/pack_registry.json`,
    preserve it so existing packs built prior to v0.29.0 update in-place without duplicating props.
    Uses `inspect_existing_mod_props` TTL cache so UI redraws never hit disk.
    """
    if not target_mod or not clean_stem:
        return None
    try:
        info = inspect_existing_mod_props(target_mod, use_cache=True)
        if info.get("exists"):
            cand = clamp_slug(f"own_customprops_{clean_stem}", 36)
            for pk in info.get("prop_keys", []):
                pk_s = str(pk)
                if pk_s == cand or pk_s == clean_stem or pk_s.endswith(f"_{clean_stem}"):
                    return pk_s if pk_s.startswith("own_") else clamp_slug(f"own_customprops_{pk_s}", 36)
    except Exception:
        pass
    return None


def _is_duplicate_object_sharing_stable_key(obj: bpy.types.Object, stable_key: str) -> bool:
    """
    Returns True if `obj` is a Blender duplicate (e.g. `.001` suffix) of another object in `bpy.data.objects`
    that already holds `stable_key` as its primary owner.
    """
    if not obj or not stable_key:
        return False
    sharing = [
        o for o in bpy.data.objects
        if getattr(o, "type", None) == 'MESH'
        and str(o.get("reskate_stable_prop_key", "") or "").strip() == stable_key
    ]
    if len(sharing) <= 1:
        return False
    owner_name = str(obj.get("reskate_stable_key_owner_name", "") or "").strip()
    if owner_name and owner_name != obj.name and any(o.name == owner_name for o in sharing):
        return True
    # If none matched owner_name, keep the one without a .001 suffix (or lowest name sort) as primary
    sharing_sorted = sorted(sharing, key=lambda o: (bool(re.search(r"\.\d+$", o.name)), o.name))
    return sharing_sorted[0] != obj


def lock_stable_prop_key_on_object(
    obj_or_context: bpy.types.Object | object | None,
    slug_or_context: str | object | None = None,
) -> str:
    """
    Stores the stable internal prop key on `obj` on first compile so later renames only change display_name.
    Safely accepts `(obj, slug)`, `(obj, context)`, `(context, obj)`, or `(obj)`.
    """
    if isinstance(obj_or_context, bpy.types.Object):
        obj = obj_or_context
        if isinstance(slug_or_context, str):
            slug = slug_or_context.strip()
            ctx = bpy.context
        else:
            slug = ""
            ctx = slug_or_context if slug_or_context is not None else bpy.context
    elif isinstance(slug_or_context, bpy.types.Object):
        obj = slug_or_context
        if isinstance(obj_or_context, str):
            slug = obj_or_context.strip()
            ctx = bpy.context
        else:
            slug = ""
            ctx = obj_or_context if obj_or_context is not None else bpy.context
    else:
        return ""

    if obj is not None:
        resolved = resolve_collision_to_owner_prop(obj)
        if resolved is not None and getattr(resolved, "type", None) == 'MESH':
            obj = resolved

    if obj is None or getattr(obj, "type", None) != 'MESH':
        return ""

    if not slug:
        slug = get_prop_slug(ctx, obj)
    if not slug or not isinstance(slug, str):
        return ""

    norm_slug = clamp_slug(slug if slug.startswith("own_") else f"own_customprops_{slug}", 36)
    short_id = norm_slug[len("own_customprops_"):] if norm_slug.startswith("own_customprops_") else norm_slug
    try:
        obj["reskate_stable_prop_key"] = norm_slug
        obj["reskate_stable_key_owner_name"] = obj.name
        obj["reskate_key_slug"] = norm_slug
        obj["reskate_prop_id"] = short_id
        st = get_or_init_object_prop_state(obj)
        st["stable_prop_key"] = norm_slug
        st["prop_key_slug"] = norm_slug
        st["prop_id"] = short_id
        save_object_prop_state(obj, st)
        _PROP_SLUG_CACHE.clear()
    except Exception:
        pass
    return norm_slug


def reset_stable_prop_key_on_object(context_or_obj, obj: bpy.types.Object | object | None = None) -> str:
    """Explicitly resets the stable internal key on `obj` (Advanced add-on action only) and recomputes it from current name."""
    if isinstance(context_or_obj, bpy.types.Object):
        real_obj = context_or_obj
        context = obj if (obj is not None and not isinstance(obj, bpy.types.Object)) else bpy.context
        obj = real_obj
    else:
        context = context_or_obj if context_or_obj is not None else bpy.context
    if obj is not None and isinstance(obj, bpy.types.Object):
        resolved = resolve_collision_to_owner_prop(obj)
        if resolved is not None and getattr(resolved, "type", None) == 'MESH':
            obj = resolved
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return ""
    for k in ("reskate_stable_prop_key", "reskate_stable_key_owner_name", "reskate_key_slug", "reskate_prop_id"):
        if k in obj:
            try:
                del obj[k]
            except Exception:
                obj[k] = ""
    st = get_or_init_object_prop_state(obj)
    st.pop("stable_prop_key", None)
    clean_disp = clean_prop_display_name(obj.name)
    clean_id = _deduplicate_prop_slug_stem(clean_disp)
    st["prop_display_name"] = clean_disp
    st["prop_key_slug"] = ""
    st["prop_id"] = clean_id
    obj["reskate_display_name"] = clean_disp
    obj["reskate_prop_id"] = clean_id
    save_object_prop_state(obj, st)
    _PROP_SLUG_CACHE.clear()
    new_slug = get_prop_slug(context, obj)
    return new_slug


def get_canonical_prop_id(context=None, obj=None) -> str:
    """Returns the short canonical prop_id (without 'own_customprops_' prefix) for the given object."""
    slug = get_prop_slug(context, obj)
    if slug.startswith("own_customprops_"):
        return slug[len("own_customprops_"):]
    if slug.startswith("own_"):
        return slug[len("own_"):]
    return slug


_PROP_SLUG_CACHE: dict[tuple, tuple[float, str]] = {}


def get_prop_slug(context=None, obj=None, target_mod_override: str | None = None, staging_dir: Path | None = None) -> str:
    """
    Computes or retrieves the stable pack-namespaced internal asset key (`own_customprops_<packtag>_<stem>`)
    clamped to <= 36 characters.
    - Once a prop has been compiled (`reskate_stable_prop_key` is set on `obj`), later renames of the Prop Name
      or Blender object name NEVER alter the internal key unless explicitly reset via `reset_stable_prop_key_on_object`.
    - Handles Blender `.001` duplicate objects cleanly so duplicated objects get a fresh key.
    """
    if isinstance(context, bpy.types.Object) and not isinstance(obj, bpy.types.Object):
        context, obj = (obj if obj is not None else bpy.context), context

    scn = context if isinstance(context, bpy.types.Scene) else (getattr(context, "scene", None) if context else getattr(bpy.context, "scene", None))
    prop = getattr(scn, "reskate_prop", None) if scn else None
    is_locked = bool(getattr(prop, "prop_id_locked", False)) if prop else False

    now_slug = time.monotonic()
    slug_cache_key = None
    if obj is not None and getattr(obj, "type", None) == 'MESH' and not is_collision_helper_object(obj):
        slug_cache_key = (
            obj.as_pointer() if hasattr(obj, "as_pointer") else id(obj),
            obj.name,
            str(obj.get("reskate_stable_prop_key", "") or ""),
            str(obj.get("reskate_key_slug", "") or ""),
            str(target_mod_override or ""),
            str(getattr(prop, "target_mod_name", "") if prop else ""),
            is_locked,
            str(getattr(prop, "prop_id", "") if (is_locked and prop) else ""),
        )
        hit_slug = _PROP_SLUG_CACHE.get(slug_cache_key)
        if hit_slug is not None and (now_slug - hit_slug[0]) < 2.0:
            return hit_slug[1]

    if obj is not None:
        resolved = resolve_collision_to_owner_prop(obj)
        if resolved is not None and getattr(resolved, "type", None) == 'MESH':
            obj = resolved

    target_mod = get_resolved_target_mod_name(context, target_mod_override=target_mod_override)
    pack_tag = compute_pack_namespace_tag(context, target_mod_override=target_mod)

    # 1. Check for existing stable internal key on the object
    if obj is not None and getattr(obj, "type", None) == 'MESH':
        stable_key = str(obj.get("reskate_stable_prop_key", "") or "").strip()
        if stable_key:
            if _is_duplicate_object_sharing_stable_key(obj, stable_key):
                # User duplicated this object in Blender (Shift+D -> .001); clear inherited stable key on duplicate
                try:
                    del obj["reskate_stable_prop_key"]
                    del obj["reskate_stable_key_owner_name"]
                    del obj["reskate_key_slug"]
                    del obj["reskate_prop_id"]
                except Exception:
                    pass
                st_dup = get_or_init_object_prop_state(obj)
                st_dup.pop("stable_prop_key", None)
                st_dup["prop_key_slug"] = ""
                st_dup["prop_id"] = ""
                save_object_prop_state(obj, st_dup)
            else:
                # Update owner_name tracking in case the user renamed the object in Blender
                try:
                    if obj.get("reskate_stable_key_owner_name") != obj.name:
                        obj["reskate_stable_key_owner_name"] = obj.name
                except Exception:
                    pass
                try:
                    info_ex = inspect_existing_mod_props(target_mod, staging_dir=staging_dir, use_cache=True)
                    if info_ex.get("exists"):
                        ex_keys = info_ex.get("prop_keys", [])
                        if stable_key not in ex_keys:
                            norm_k = re.sub(r"__+", "_", stable_key)
                            if norm_k in ex_keys:
                                stable_key = norm_k
                                lock_stable_prop_key_on_object(obj, stable_key)
                            else:
                                for pd in info_ex.get("props_detail", []):
                                    if pd.get("display_name") == obj.name or pd.get("prop_id") == obj.name:
                                        stable_key = pd.get("prop_key")
                                        lock_stable_prop_key_on_object(obj, stable_key)
                                        break
                except Exception:
                    pass
                res_s = clamp_slug(stable_key if stable_key.startswith("own_") else f"own_customprops_{stable_key}", 36)
                if slug_cache_key is not None:
                    _PROP_SLUG_CACHE[slug_cache_key] = (now_slug, res_s)
                return res_s

        # Backwards-compatibility with v0.28.x objects that already have reskate_key_slug / reskate_prop_id in pack_registry.json
        state = get_or_init_object_prop_state(obj)
        prev_slug = str(state.get("stable_prop_key") or obj.get("reskate_key_slug", "") or state.get("prop_key_slug", "") or "").strip()
        if prev_slug:
            norm_prev = clamp_slug(prev_slug if prev_slug.startswith("own_") else f"own_customprops_{prev_slug}", 36)
            if not _is_duplicate_object_sharing_stable_key(obj, norm_prev):
                try:
                    info_prev = inspect_existing_mod_props(target_mod, staging_dir=staging_dir, use_cache=True)
                    if info_prev.get("exists") and norm_prev in info_prev.get("prop_keys", []):
                        lock_stable_prop_key_on_object(obj, norm_prev)
                        if slug_cache_key is not None:
                            _PROP_SLUG_CACHE[slug_cache_key] = (now_slug, norm_prev)
                        return norm_prev
                except Exception:
                    pass

    raw_stem = ""
    if is_locked and prop:
        raw_id = (getattr(prop, "prop_id", "") or getattr(prop, "prop_key_slug", "")).strip()
        if raw_id:
            raw_stem = _deduplicate_prop_slug_stem(raw_id)
            res_l = clamp_slug(f"own_customprops_{raw_stem}", 36)
            if slug_cache_key is not None:
                _PROP_SLUG_CACHE[slug_cache_key] = (now_slug, res_l)
            return res_l

    if obj is not None and getattr(obj, "type", None) == 'MESH':
        name = obj.name
        if name.lower().endswith('_col'):
            name = name[:-4]
        raw_stem = _deduplicate_prop_slug_stem(name) or "prop"
    elif prop:
        raw_id = (getattr(prop, "prop_display_name", "") or getattr(prop, "prop_id", "") or "prop").strip()
        raw_stem = _deduplicate_prop_slug_stem(raw_id) or "prop"
    else:
        raw_stem = "prop"

    base_stem = _strip_any_pack_tag_prefix(raw_stem, obj=obj) or "prop"

    # Backwards-compatibility: if this exact un-prefixed stem is already in the current pack's registry, keep & lock it
    legacy_slug = _find_legacy_unprefixed_slug_in_pack(target_mod, base_stem)
    if legacy_slug is not None:
        if obj is not None and getattr(obj, "type", None) == 'MESH':
            lock_stable_prop_key_on_object(obj, legacy_slug)
        if slug_cache_key is not None:
            _PROP_SLUG_CACHE[slug_cache_key] = (now_slug, legacy_slug)
        return legacy_slug

    if not base_stem.startswith(f"{pack_tag}_"):
        namespaced_stem = f"{pack_tag}_{base_stem}"
    else:
        namespaced_stem = base_stem

    if obj is not None and getattr(obj, "type", None) == 'MESH':
        try:
            if obj.get("reskate_pack_ns_tag") != pack_tag:
                obj["reskate_pack_ns_tag"] = pack_tag
        except Exception:
            pass

    final_slug = clamp_slug(f"own_customprops_{namespaced_stem}", 36)
    if slug_cache_key is not None:
        if len(_PROP_SLUG_CACHE) > 512:
            _PROP_SLUG_CACHE.clear()
        _PROP_SLUG_CACHE[slug_cache_key] = (now_slug, final_slug)
    return final_slug


_LOOK_STATUS_CACHE: dict[tuple, dict] = {}
_SKATE_RUNNING_CACHE: tuple[float, bool] = (0.0, False)
_GAME_DIR_VALID_CACHE: tuple[float, str, bool] = (0.0, "", False)
_CLI_STATUS_CACHE: tuple[float, str, dict] = (0.0, "", {})
_EXISTING_MOD_CACHE: dict[str, tuple[float, dict]] = {}
_ROUGHNESS_SUMMARY_CACHE: dict[tuple, dict] = {}
_ALIGNMENT_ISSUES_CACHE: dict[str, tuple[tuple, dict]] = {}
_FACE_SURFACE_SUMMARY_CACHE: dict[str, tuple[tuple, list[dict]]] = {}
_HAVOK_CEILING_CACHE: dict[tuple, dict] = {}
_THIN_MESH_CACHE: dict[tuple, dict] = {}
_UV_BOUNDS_CACHE: dict[tuple, dict] = {}
_PREFLIGHT_CHECKS_CACHE: dict[tuple, list[dict]] = {}
_PACK_COMPILE_DIFF_CACHE: dict[tuple, tuple[float, dict]] = {}
_ALL_SCENE_PROP_MESHES_CACHE: tuple[float, int, int, list[str]] = (0.0, -1, -1, [])
_PACK_PROP_MESHES_CACHE: dict[tuple, tuple[float, list[str]]] = {}
_SCENE_PROP_PACKS_SUMMARY_CACHE: tuple[float, tuple | None, list[dict]] = (0.0, None, [])
_ALL_PACK_REGISTRIES_CACHE: tuple[float, dict[str, dict]] = (0.0, {})
_LOGGED_ORM_AUTODETECT: set[tuple[str, str]] = set()
# Persistent PIL image cache keyed by (img.name, width, height, filepath_raw) — NEVER cleared by invalidate_ui_caches()
_IMAGE_PIL_CACHE: dict[tuple, object] = {}
_DETACHED_BUFFER_CACHE: dict[tuple, dict] = {}
_SYNCED_MAT_NODE_SIGS: dict[str, tuple[tuple, dict]] = {}
_STATIC_NO_THUMB_CACHE: dict[tuple[bool, int], Path] = {}


def is_experimental_addon_enabled(context=None) -> bool:
    """Returns True only when the companion `sk8_prop_experimental_addon` is installed and enabled."""
    try:
        ctx = context if context is not None else bpy.context
        prefs = getattr(ctx, "preferences", None) if ctx else None
        addons = getattr(prefs, "addons", None) if prefs else None
        if addons is not None:
            for k in addons.keys():
                if "sk8_prop_experimental_addon" in str(k):
                    return True
    except Exception:
        pass
    return False


def invalidate_alignment_issues_cache(obj_name: str | None = None):
    """Clears the cached alignment issues for `obj_name` (or all objects when None)."""
    global _ALIGNMENT_ISSUES_CACHE
    if obj_name is None:
        _ALIGNMENT_ISSUES_CACHE.clear()
    else:
        _ALIGNMENT_ISSUES_CACHE.pop(str(obj_name), None)


def invalidate_face_surface_summary_cache(obj_name: str | None = None):
    """Clears the cached face surface summary for `obj_name` (or all objects when None)."""
    global _FACE_SURFACE_SUMMARY_CACHE
    if obj_name is None:
        _FACE_SURFACE_SUMMARY_CACHE.clear()
    else:
        _FACE_SURFACE_SUMMARY_CACHE.pop(str(obj_name), None)


def invalidate_roughness_summary_cache(mat_name: str | None = None):
    """Clears the cached roughness summary for `mat_name` (or all materials when `mat_name` is None)."""
    global _ROUGHNESS_SUMMARY_CACHE
    if mat_name is None:
        _ROUGHNESS_SUMMARY_CACHE.clear()
        return
    target = str(mat_name)
    for k in list(_ROUGHNESS_SUMMARY_CACHE.keys()):
        if isinstance(k, tuple) and len(k) > 0 and k[0] == target:
            _ROUGHNESS_SUMMARY_CACHE.pop(k, None)


def invalidate_havok_ceiling_cache(obj_name: str | None = None):
    """Clears the cached Havok ceiling status for `obj_name` (or all objects when None)."""
    global _HAVOK_CEILING_CACHE
    if obj_name is None:
        _HAVOK_CEILING_CACHE.clear()
    else:
        target = str(obj_name)
        for k in list(_HAVOK_CEILING_CACHE.keys()):
            if isinstance(k, tuple) and len(k) > 0 and k[0] == target:
                _HAVOK_CEILING_CACHE.pop(k, None)


def invalidate_ui_caches(clear_gpu: bool = False):
    """Clears cached mesh look, process status, alignment issues, and face surface summary entries."""
    global _LOOK_STATUS_CACHE, _EXISTING_MOD_CACHE, _ALIGNMENT_ISSUES_CACHE, _FACE_SURFACE_SUMMARY_CACHE
    global _THIN_MESH_CACHE, _UV_BOUNDS_CACHE, _PREFLIGHT_CHECKS_CACHE, _PACK_COMPILE_DIFF_CACHE, _GAME_DIR_VALID_CACHE, _CLI_STATUS_CACHE
    global _PROP_SLUG_CACHE, _ALL_SCENE_PROP_MESHES_CACHE, _PACK_PROP_MESHES_CACHE, _SCENE_PROP_PACKS_SUMMARY_CACHE, _ALL_PACK_REGISTRIES_CACHE
    global _SYNCED_MAT_NODE_SIGS
    _LOOK_STATUS_CACHE.clear()
    _EXISTING_MOD_CACHE.clear()
    _OBJ_PACK_MEMBERSHIP_CACHE.clear()
    _PROP_SLUG_CACHE.clear()
    _ALL_SCENE_PROP_MESHES_CACHE = (0.0, -1, -1, [])
    _PACK_PROP_MESHES_CACHE.clear()
    _SCENE_PROP_PACKS_SUMMARY_CACHE = (0.0, None, [])
    _ALL_PACK_REGISTRIES_CACHE = (0.0, {})
    _THIN_MESH_CACHE.clear()
    _UV_BOUNDS_CACHE.clear()
    _PREFLIGHT_CHECKS_CACHE.clear()
    _PACK_COMPILE_DIFF_CACHE.clear()
    _SYNCED_MAT_NODE_SIGS.clear()
    _GAME_DIR_VALID_CACHE = (0.0, "", False)
    _CLI_STATUS_CACHE = (0.0, "", {})
    invalidate_alignment_issues_cache()
    invalidate_face_surface_summary_cache()
    invalidate_roughness_summary_cache()
    invalidate_havok_ceiling_cache()
    try:
        if hasattr(properties, "invalidate_prop_state_caches"):
            properties.invalidate_prop_state_caches()
    except Exception:
        pass
    if clear_gpu:
        try:
            from . import gpu_draw
            gpu_draw.invalidate_gpu_caches(clear_ui=False)
        except Exception:
            pass


def inspect_mesh_look_status(obj: bpy.types.Object) -> dict:
    """
    Non-destructively inspects a mesh's materials and UVs to determine whether
    Path A ('Use my texture as-is') or Path B ('Bake for me') applies.
    Uses fast numpy C-array `foreach_get` and fingerprint caching (< 0.05ms).
    Returns dict with:
      - can_use_as_is: bool
      - mat_count: int
      - uvs_in_01: bool
      - has_baked_atlas: bool
      - primary_image: bpy.types.Image | None
      - reason: str
    """
    if not obj or obj.type != 'MESH':
        return {
            "can_use_as_is": False,
            "mat_count": 0,
            "uvs_in_01": True,
            "has_baked_atlas": False,
            "primary_image": None,
            "reason": "Select a 3D mesh object",
        }

    mesh = obj.data
    valid_mats = [m for m in mesh.materials if m is not None]
    mat_count = len(valid_mats)
    uv_names = tuple(uvl.name for uvl in mesh.uv_layers)
    has_baked_atlas = ("ReSkate_Atlas_UV" in uv_names)

    # Find primary image texture in sk8_material overrides or material shader nodes
    primary_image = None
    for mat in valid_mats:
        ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
        if ms is not None and getattr(ms, "base_color_image", None) is not None:
            primary_image = ms.base_color_image
            break
        if mat.use_nodes and mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image is not None:
                    if not node.name.startswith("ReSkate_Bake"):
                        primary_image = node.image
                        break
        if primary_image:
            break

    cache_key = (
        obj.name,
        len(mesh.vertices),
        len(mesh.polygons),
        len(mesh.loops),
        uv_names,
        tuple(m.name for m in valid_mats),
        primary_image.name if primary_image else None,
        obj.mode,
    )
    cached = _LOOK_STATUS_CACHE.get(cache_key)
    if cached is not None and obj.mode != 'EDIT':
        return cached

    # Check if original UV layer is within [0..1] (with small tolerance 0.05) using C-speed foreach_get
    uvs_in_01 = True
    orig_uv = None
    for uvl in mesh.uv_layers:
        if uvl.name != "ReSkate_Atlas_UV":
            orig_uv = uvl
            break
    if orig_uv is None and len(mesh.uv_layers) > 0:
        orig_uv = mesh.uv_layers[0]

    n_uv = len(orig_uv.data) if orig_uv is not None else 0
    if n_uv > 0:
        try:
            uv_buf = np.empty(n_uv * 2, dtype=np.float32)
            orig_uv.data.foreach_get("uv", uv_buf)
            if float(uv_buf.min()) < -0.05 or float(uv_buf.max()) > 1.05:
                uvs_in_01 = False
        except Exception:
            for loop_uv in orig_uv.data:
                u, v = loop_uv.uv.x, loop_uv.uv.y
                if u < -0.05 or u > 1.05 or v < -0.05 or v > 1.05:
                    uvs_in_01 = False
                    break

    # Authored UVs are always preserved 1:1 by default (single-material passthrough or multi-material quadrant packing)
    can_use_as_is = True
    if mat_count <= 1 and uvs_in_01:
        reason = "Ready as-is (1:1 UVs & Skate Map PBR)"
    elif mat_count > 1:
        reason = f"Ready as-is ({mat_count} materials auto-packed at compile time; UVs preserved)"
    else:
        reason = "Ready as-is (authored UVs preserved 1:1)"

    result = {
        "can_use_as_is": can_use_as_is,
        "mat_count": mat_count,
        "uvs_in_01": uvs_in_01,
        "has_baked_atlas": has_baked_atlas,
        "primary_image": primary_image,
        "reason": reason,
    }
    if len(_LOOK_STATUS_CACHE) > 32:
        _LOOK_STATUS_CACHE.clear()
    _LOOK_STATUS_CACHE[cache_key] = result
    return result


def _set_global_sync_state(syncing_prop: bool, syncing_mat: bool) -> None:
    """Set _IS_SYNCING_PROP and _IN_MAT_TEX_SYNC across all loaded copies of properties.py."""
    for mod_name, mod in list(sys.modules.items()):
        if mod and ("sk8_prop" in mod_name or mod_name.endswith(".properties") or mod_name == "properties"):
            if hasattr(mod, "_IS_SYNCING_PROP"):
                try:
                    mod._IS_SYNCING_PROP = syncing_prop
                except Exception:
                    pass
            if hasattr(mod, "_IN_MAT_TEX_SYNC"):
                try:
                    mod._IN_MAT_TEX_SYNC = syncing_mat
                except Exception:
                    pass
    try:
        properties._IS_SYNCING_PROP = syncing_prop
        properties._IN_MAT_TEX_SYNC = syncing_mat
    except Exception:
        pass


def export_visual_glb_generator(
    context,
    obj: bpy.types.Object,
    out_path: Path,
    pct_val: float = 0.0,
    label_prefix: str = "",
    end_pct: float | None = None,
):
    """
    Generator version of export_visual_glb yielding between mesh transform/UV preparation
    and glTF scene export, keeping all execution intervals strictly sub-350ms.
    """
    if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')

    prev_active = context.view_layer.objects.active
    prev_selected = [o for o in context.selected_objects]

    prop = getattr(context.scene, "reskate_prop", None)
    look_mode = getattr(prop, "look_mode", 'AS_IS') if prop else 'AS_IS'
    scale_mult = float(getattr(prop, "scale_mult", 1.0)) if prop else 1.0

    prev_sync_prop = getattr(properties, "_IS_SYNCING_PROP", False)
    prev_sync_mat = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    _set_global_sync_state(True, True)

    temp_obj = None
    temp_mesh = None

    # Compute monotonic intermediate percentage points bounded by caller window
    if end_pct is not None and end_pct > pct_val:
        span = end_pct - pct_val
        eval_pct = pct_val
        write_pct = round(pct_val + span * 0.35, 2)
        done_pct = round(pct_val + span * 0.70, 2)
    else:
        eval_pct = pct_val
        write_pct = round(pct_val + 0.2, 2)
        done_pct = round(pct_val + 0.4, 2)

    # Evaluate modifiers (Array, Mirror, Bevel, Solidify, Boolean) via depsgraph so baking rotation/scale
    # never happens before modifier evaluation or gets skewed by delta transforms!
    try:
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        temp_mesh = bpy.data.meshes.new_from_object(eval_obj, preserve_all_data_layers=True, depsgraph=depsgraph)
    except Exception:
        temp_mesh = obj.data.copy()
    temp_obj = obj.copy()
    temp_obj.data = temp_mesh
    try:
        temp_obj.modifiers.clear()
        temp_obj.delta_location = (0.0, 0.0, 0.0)
        temp_obj.delta_rotation_euler = (0.0, 0.0, 0.0)
        temp_obj.delta_scale = (1.0, 1.0, 1.0)
    except Exception:
        pass
    context.collection.objects.link(temp_obj)

    # Intermediate yield 1: isolate depsgraph and modifier baking from subsequent transform / UV work
    if eval_pct > 0.0:
        yield ProgressStatus(f"{label_prefix}Evaluating mesh & modifiers for '{obj.name}'...", round(eval_pct, 2))

    try:
        # Bake the object's rotation and scale directly into temp_mesh vertices in origin-relative space,
        # and set temp_obj.matrix_world to Identity so Blender's glTF exporter never leaves unapplied node transforms!
        loc, rot, sca = obj.matrix_world.decompose()
        temp_obj.parent = None
        bake_mat = (
            rot.to_matrix().to_4x4()
            @ Matrix.Diagonal((sca.x * scale_mult, sca.y * scale_mult, sca.z * scale_mult, 1.0))
        )
        temp_mesh.transform(bake_mat)
        if bake_mat.determinant() < 0.0:
            temp_mesh.flip_normals()
        temp_mesh.update()
        temp_obj.matrix_world = Matrix.Identity(4)

        # Only use ReSkate_Atlas_UV if the user explicitly chose 'BAKE' mode;
        # in default 'AS_IS' mode, always preserve the user's authored / active-render UV map as TEXCOORD_0!
        use_baked_uv = (
            look_mode == 'BAKE'
            and "ReSkate_Atlas_UV" in temp_mesh.uv_layers
        )
        if use_baked_uv:
            atlas_uv = temp_mesh.uv_layers["ReSkate_Atlas_UV"]
            atlas_uv.active_render = True
            atlas_uv.active = True
            for uvl in list(temp_mesh.uv_layers):
                if uvl.name != "ReSkate_Atlas_UV":
                    temp_mesh.uv_layers.remove(uvl)
        else:
            if "ReSkate_Atlas_UV" in temp_mesh.uv_layers and len(temp_mesh.uv_layers) > 1:
                temp_mesh.uv_layers.remove(temp_mesh.uv_layers["ReSkate_Atlas_UV"])
            if len(temp_mesh.uv_layers) > 0:
                # Identify the exact UV layer used by the active Principled BSDF's upstream texture nodes,
                # or marked active_render (camera icon) on the mesh
                target_uv_name = None
                for slot in obj.material_slots:
                    m = slot.material
                    if m and m.use_nodes and m.node_tree:
                        p_active = _principled_node(m)
                        if p_active is not None:
                            visited_nodes = set()
                            stack_nodes = [p_active]
                            while stack_nodes:
                                cur_n = stack_nodes.pop(0)
                                if cur_n is None or id(cur_n) in visited_nodes:
                                    continue
                                visited_nodes.add(id(cur_n))
                                if cur_n.type == "UVMAP" and getattr(cur_n, "uv_map", "") and cur_n.uv_map in temp_mesh.uv_layers:
                                    target_uv_name = cur_n.uv_map
                                    break
                                for in_s in getattr(cur_n, "inputs", ()):
                                    if in_s.is_linked:
                                        for lk in in_s.links:
                                            if lk.from_node is not None:
                                                stack_nodes.append(lk.from_node)
                        if target_uv_name:
                            break
                if not target_uv_name:
                    render_uv = next((u for u in temp_mesh.uv_layers if getattr(u, "active_render", False)), None)
                    active_uv = temp_mesh.uv_layers.active or temp_mesh.uv_layers[0]
                    # If the user has multiple UV layers and selected or set active_render on a layer (e.g. Sanctus Bake UV), honor it!
                    if render_uv is not None:
                        target_uv_name = render_uv.name
                    elif active_uv is not None:
                        target_uv_name = active_uv.name
                    else:
                        target_uv_name = temp_mesh.uv_layers[0].name

                if target_uv_name and target_uv_name in temp_mesh.uv_layers:
                    # Remove all other UV layers on the temporary export mesh so glTF exports target_uv_name as TEXCOORD_0!
                    for uvl in list(temp_mesh.uv_layers):
                        if uvl.name != target_uv_name:
                            temp_mesh.uv_layers.remove(uvl)
                    if len(temp_mesh.uv_layers) > 0:
                        temp_mesh.uv_layers[0].active_render = True
                        temp_mesh.uv_layers[0].active = True

        # Direct deselection loop eliminates cold bpy.ops.object.select_all operator startup overhead (up to 498ms)
        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        temp_obj.select_set(True)
        context.view_layer.objects.active = temp_obj

        # Intermediate yield 2: isolate UV/transform preparation from glTF C++ operator execution
        yield ProgressStatus(f"{label_prefix}Writing glTF scene for '{obj.name}'...", round(write_pct, 2))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(
            filepath=str(out_path),
            use_selection=True,
            export_apply=False,
            export_yup=True,
            export_texcoords=True,
            export_normals=True,
            export_materials='EXPORT',
            export_image_format='NONE',
            export_animations=False,
            export_skins=False,
            export_morph=False,
            export_lights=False,
            export_cameras=False,
        )
        # Intermediate yield 3: isolate glTF export from finally: mesh deallocation and caller loops
        yield ProgressStatus(f"{label_prefix}Visual GLB written for '{obj.name}', cleaning temporary mesh...", round(done_pct, 2))
        return out_path
    finally:
        if temp_obj is not None:
            try:
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            except Exception:
                pass
        if temp_mesh is not None:
            try:
                bpy.data.meshes.remove(temp_mesh, do_unlink=True)
            except Exception:
                pass
        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        for o in prev_selected:
            try:
                if o and o.name in bpy.data.objects:
                    o.select_set(True)
            except Exception:
                pass
        if prev_active and prev_active.name in bpy.data.objects:
            context.view_layer.objects.active = prev_active
        _set_global_sync_state(prev_sync_prop, prev_sync_mat)


def export_visual_glb(context, obj: bpy.types.Object, out_path: Path) -> Path:
    """
    Exports active mesh to GLB relative to the object's OWN origin (0,0,0)
    without moving or altering the user's object or UVs in the Blender scene!
    """
    gen = export_visual_glb_generator(context, obj, out_path)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def resolve_custom_collision_object(context, visual_obj: bpy.types.Object) -> bpy.types.Object | None:
    """
    Resolves the custom collision mesh object for `visual_obj`:
    1. Per-object saved collision pointer `visual_obj.reskate_custom_collision_object` (or state name)
    2. Explicit eyedropper pointer `scene.reskate_prop.custom_collision_object` (when `visual_obj` is active)
    3. Fallback `<PropName>_COL` or child tagged `is_col_proxy`
    """
    if visual_obj:
        saved_col = getattr(visual_obj, "reskate_custom_collision_object", None)
        if saved_col and saved_col.name in bpy.data.objects and saved_col.type == 'MESH' and len(saved_col.data.polygons) > 0:
            return saved_col
        state = get_or_init_object_prop_state(visual_obj)
        col_name_saved = state.get("custom_collision_object_name", "")
        if col_name_saved and col_name_saved in bpy.data.objects:
            c_obj = bpy.data.objects[col_name_saved]
            if c_obj.type == 'MESH' and len(c_obj.data.polygons) > 0:
                return c_obj

    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    active_mesh = resolve_active_prop_mesh(context)
    if prop and (visual_obj is None or visual_obj == active_mesh):
        picked = getattr(prop, "custom_collision_object", None)
        if picked and picked.name in bpy.data.objects and picked.type == 'MESH' and len(picked.data.polygons) > 0:
            return picked

    if visual_obj:
        col_name = f"{visual_obj.name}_COL"
        col_obj = bpy.data.objects.get(col_name)
        if col_obj and col_obj.type == 'MESH' and len(col_obj.data.polygons) > 0:
            return col_obj
        for o in bpy.data.objects:
            if getattr(getattr(o, "reskate_col_prop", None), "is_col_proxy", False):
                if o.get("reskate_col_owner") == visual_obj.name or o.name.lower().startswith(visual_obj.name.lower() + "_col"):
                    if o.type == 'MESH' and len(o.data.polygons) > 0:
                        return o
        # 4. Sibling/co-located collision mesh (e.g. Collision.001 co-located with Cube.002, RailCollision with RailSimple)
        try:
            v_bb = [visual_obj.matrix_world @ Vector(pt) for pt in visual_obj.bound_box] if visual_obj.bound_box else [visual_obj.matrix_world.to_translation()]
            v_center = sum(v_bb, Vector((0.0, 0.0, 0.0))) / float(len(v_bb))
            col_cands = []
            for o in bpy.data.objects:
                if o == visual_obj or getattr(o, "type", None) != 'MESH' or len(o.data.polygons) == 0:
                    continue
                if not is_collision_helper_object(o):
                    continue
                stem_u = re.sub(r"\.\d+$", "", o.name).upper()
                if stem_u.endswith("_PPHYS") or bool(o.get("reskate_is_pphys_proxy", False)):
                    continue
                if stem_u.endswith("_COL") and not o.name.lower().startswith(visual_obj.name.lower()):
                    continue
                o_bb = [o.matrix_world @ Vector(pt) for pt in o.bound_box] if o.bound_box else [o.matrix_world.to_translation()]
                o_center = sum(o_bb, Vector((0.0, 0.0, 0.0))) / float(len(o_bb))
                dist = (o_center - v_center).length
                if dist <= 0.45:
                    col_cands.append((dist, o))
            if col_cands:
                col_cands.sort(key=lambda item: item[0])
                return col_cands[0][1]
        except Exception:
            pass
    return None


def export_col_glb_generator(
    context,
    visual_obj: bpy.types.Object,
    out_path: Path,
    explicit_col_obj: bpy.types.Object | None = None,
    pct_val: float = 0.0,
    label_prefix: str = "",
    end_pct: float | None = None,
):
    """
    Generator that exports the custom collision object to GLB in the visual prop's origin-relative
    frame without moving or modifying the user's objects in the scene.
    Yields ProgressStatus at mesh evaluation and before/after glTF operator execution.
    """
    if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')

    col_obj = explicit_col_obj or resolve_custom_collision_object(context, visual_obj)
    if not col_obj or col_obj.type != 'MESH' or len(col_obj.data.polygons) == 0:
        return None

    prev_active = context.view_layer.objects.active
    prev_selected = [o for o in context.selected_objects]

    prop = getattr(context.scene, "reskate_prop", None)
    scale_mult = float(getattr(prop, "scale_mult", 1.0)) if prop else 1.0

    eval_pct = round(pct_val, 2)
    write_pct = round((pct_val + (end_pct or pct_val)) / 2.0, 2) if end_pct else eval_pct
    done_pct = round(end_pct, 2) if end_pct else eval_pct

    prev_sync_prop = getattr(properties, "_IS_SYNCING_PROP", False)
    prev_sync_mat = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    _set_global_sync_state(True, True)

    temp_obj = None
    temp_mesh = None

    try:
        if eval_pct > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating collision mesh for '{visual_obj.name}'...", eval_pct)

        try:
            depsgraph = context.evaluated_depsgraph_get()
            eval_col = col_obj.evaluated_get(depsgraph)
            temp_mesh = bpy.data.meshes.new_from_object(eval_col, preserve_all_data_layers=True, depsgraph=depsgraph)
        except Exception:
            temp_mesh = col_obj.data.copy()
        temp_obj = col_obj.copy()
        temp_obj.data = temp_mesh
        try:
            temp_obj.modifiers.clear()
            temp_obj.delta_location = (0.0, 0.0, 0.0)
            temp_obj.delta_rotation_euler = (0.0, 0.0, 0.0)
            temp_obj.delta_scale = (1.0, 1.0, 1.0)
        except Exception:
            pass
        context.collection.objects.link(temp_obj)

        # Express col_obj relative to visual_obj's origin (translation removed, rotation/scale preserved).
        v_loc, v_rot, v_sca = visual_obj.matrix_world.decompose()
        v_origin_world = Matrix.Translation(v_loc)
        c_loc, _c_rot, _c_sca = col_obj.matrix_world.decompose()
        c_origin_world = Matrix.Translation(c_loc)
        try:
            rel_from_visual_origin = v_origin_world.inverted() @ col_obj.matrix_world
        except Exception:
            rel_from_visual_origin = col_obj.matrix_world.copy()
        try:
            rel_from_col_origin = c_origin_world.inverted() @ col_obj.matrix_world
            rel_vis_self = v_origin_world.inverted() @ visual_obj.matrix_world
            if col_obj.parent != visual_obj and (c_loc - v_loc).length > 0.25:
                v_corners = [rel_vis_self @ Vector(b) for b in visual_obj.bound_box]
                c_corners_vis = [rel_from_visual_origin @ Vector(b) for b in col_obj.bound_box]
                c_corners_self = [rel_from_col_origin @ Vector(b) for b in col_obj.bound_box]
                v_center = sum(v_corners, Vector((0.0, 0.0, 0.0))) / 8.0
                c_center_vis = sum(c_corners_vis, Vector((0.0, 0.0, 0.0))) / 8.0
                c_center_self = sum(c_corners_self, Vector((0.0, 0.0, 0.0))) / 8.0
                if (c_center_vis - v_center).length > (c_center_self - v_center).length + 0.25:
                    rel_from_visual_origin = rel_from_col_origin
        except Exception:
            pass

        temp_obj.parent = None
        bake_col_mat = (
            Matrix.Diagonal((scale_mult, scale_mult, scale_mult, 1.0))
            @ rel_from_visual_origin
        )
        temp_mesh.transform(bake_col_mat)
        if bake_col_mat.determinant() < 0.0:
            temp_mesh.flip_normals()
        temp_mesh.update()

        try:
            if len(temp_mesh.vertices) > 0 and visual_obj.bound_box:
                vis_bake_mat = (
                    v_rot.to_matrix().to_4x4()
                    @ Matrix.Diagonal((v_sca.x * scale_mult, v_sca.y * scale_mult, v_sca.z * scale_mult, 1.0))
                )
                v_pts = [vis_bake_mat @ Vector(b) for b in visual_obj.bound_box]
                vmin = Vector((min(p.x for p in v_pts), min(p.y for p in v_pts), min(p.z for p in v_pts)))
                vmax = Vector((max(p.x for p in v_pts), max(p.y for p in v_pts), max(p.z for p in v_pts)))
                v_center = 0.5 * (vmin + vmax)
                v_span = vmax - vmin

                c_xs = [v.co.x for v in temp_mesh.vertices]
                c_ys = [v.co.y for v in temp_mesh.vertices]
                c_zs = [v.co.z for v in temp_mesh.vertices]
                cmin = Vector((min(c_xs), min(c_ys), min(c_zs)))
                cmax = Vector((max(c_xs), max(c_ys), max(c_zs)))
                c_center = 0.5 * (cmin + cmax)

                disjoint_xy = (
                    cmax.x < vmin.x - 0.25
                    or cmin.x > vmax.x + 0.25
                    or cmax.y < vmin.y - 0.25
                    or cmin.y > vmax.y + 0.25
                )
                if disjoint_xy or (c_center - v_center).length > 0.65 * v_span.length + 0.50:
                    dz = (vmin.z - cmin.z) if abs(cmin.z - vmin.z) > 0.25 else 0.0
                    align_vec = Vector((v_center.x - c_center.x, v_center.y - c_center.y, dz))
                    temp_mesh.transform(Matrix.Translation(align_vec))
                    temp_mesh.update()
        except Exception:
            pass

        temp_obj.matrix_world = Matrix.Identity(4)
        temp_obj.hide_viewport = False
        temp_obj.hide_set(False)

        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        temp_obj.select_set(True)
        context.view_layer.objects.active = temp_obj

        if write_pct > 0.0:
            yield ProgressStatus(f"{label_prefix}Writing collision glTF scene for '{visual_obj.name}'...", write_pct)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(
            filepath=str(out_path),
            use_selection=True,
            export_apply=False,
            export_yup=True,
            export_texcoords=True,
            export_normals=True,
            export_materials='NONE',
            export_image_format='NONE',
            export_animations=False,
            export_skins=False,
            export_morph=False,
            export_lights=False,
            export_cameras=False,
        )

        if done_pct > 0.0:
            yield ProgressStatus(f"{label_prefix}Collision GLB written for '{visual_obj.name}', cleaning temporary mesh...", done_pct)

        return out_path
    finally:
        if temp_obj is not None:
            try:
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            except Exception:
                pass
        if temp_mesh is not None:
            try:
                bpy.data.meshes.remove(temp_mesh, do_unlink=True)
            except Exception:
                pass
        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        for o in prev_selected:
            try:
                if o and o.name in bpy.data.objects:
                    o.select_set(True)
            except Exception:
                pass
        if prev_active and prev_active.name in bpy.data.objects:
            context.view_layer.objects.active = prev_active
        _set_global_sync_state(prev_sync_prop, prev_sync_mat)


def export_col_glb(
    context,
    visual_obj: bpy.types.Object,
    out_path: Path,
    explicit_col_obj: bpy.types.Object | None = None,
) -> Path | None:
    """
    Exports the custom collision object to GLB in the visual prop's origin-relative
    frame without moving or modifying the user's objects in the scene.
    Synchronous backward-compatible wrapper that runs export_col_glb_generator to completion.
    """
    gen = export_col_glb_generator(context, visual_obj, out_path, explicit_col_obj=explicit_col_obj)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value



def resolve_custom_placement_proxy_object(context, visual_obj: bpy.types.Object | None) -> bpy.types.Object | None:
    """
    Resolves the custom Quick Drop Placement Proxy (`_pphys`) mesh object for `visual_obj`:
    1. Per-object saved placement proxy pointer `visual_obj.reskate_custom_placement_proxy_object` (or state name)
    2. Explicit eyedropper pointer `scene.reskate_prop.custom_placement_proxy_object` (when `visual_obj` is active)
    3. Fallback `<PropName>_PPHYS` or child tagged `reskate_is_pphys_proxy`
    """
    if visual_obj:
        for attr_nm in ("reskate_custom_placement_proxy_object", "custom_placement_proxy_obj"):
            saved_p = getattr(visual_obj, attr_nm, None)
            if saved_p and saved_p.name in bpy.data.objects and saved_p.type == 'MESH' and len(saved_p.data.polygons) > 0:
                return saved_p
        state = get_or_init_object_prop_state(visual_obj)
        p_name_saved = state.get("custom_placement_proxy_object_name", "")
        if p_name_saved and p_name_saved in bpy.data.objects:
            p_obj = bpy.data.objects[p_name_saved]
            if p_obj.type == 'MESH' and len(p_obj.data.polygons) > 0:
                return p_obj

    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    active_mesh = resolve_active_prop_mesh(context) if context else None
    if prop and (visual_obj is None or visual_obj == active_mesh):
        for attr_nm in ("custom_placement_proxy_object", "custom_placement_proxy_obj"):
            picked = getattr(prop, attr_nm, None)
            if picked and picked.name in bpy.data.objects and picked.type == 'MESH' and len(picked.data.polygons) > 0:
                return picked

    if visual_obj:
        pphys_name = f"{visual_obj.name}_PPHYS"
        pphys_obj = bpy.data.objects.get(pphys_name)
        if pphys_obj and pphys_obj.type == 'MESH' and len(pphys_obj.data.polygons) > 0:
            return pphys_obj
        for o in bpy.data.objects:
            if o.get("reskate_is_pphys_proxy") or getattr(getattr(o, "reskate_col_prop", None), "is_pphys_proxy", False):
                if o.parent == visual_obj or o.name.startswith(visual_obj.name):
                    if o.type == 'MESH' and len(o.data.polygons) > 0:
                        return o
    return None


def export_pphys_glb(
    context,
    visual_obj: bpy.types.Object,
    out_path: Path,
    explicit_pphys_obj: bpy.types.Object | None = None,
) -> Path | None:
    """
    Exports the custom Quick Drop placement proxy (`_pphys`) mesh object to GLB in `visual_obj`'s
    origin-relative frame so `compile_blender_prop.py` can compile a 1-hull `hknpConvexPolytopeShape`.
    """
    pphys_obj = explicit_pphys_obj or resolve_custom_placement_proxy_object(context, visual_obj)
    if not pphys_obj or pphys_obj.type != 'MESH' or len(pphys_obj.data.polygons) == 0:
        return None
    return export_col_glb(context, visual_obj, out_path, explicit_col_obj=pphys_obj)


def extract_prop_lights(prop_obj: bpy.types.Object, scale_mult: float = 1.0) -> list[dict]:
    """
    Extracts lights linked to `prop_obj` and reads cone, radius, color, intensity,
    and shadow settings straight from each Blender light object (`light_obj.data`).
    Priority:
      1. Explicit `prop_obj.reskate_linked_lights` collection (eyedropper list)
      2. Direct child `LIGHT` objects parented to `prop_obj` (if not already in the list)
    Never attaches unparented scene lights by name or tag to every prop!
    Because `export_visual_glb` bakes `prop_obj`'s world rotation and scale (`sca * scale_mult`)
    into the exported mesh vertices, each light's position is its exact world offset from
    `prop_obj`'s origin scaled by `scale_mult`, and its rotation is its exact world quaternion!
    """
    if not prop_obj:
        return []

    candidates = []
    seen = set()

    # 1. Explicit linked lights list on this prop
    if hasattr(prop_obj, "reskate_linked_lights"):
        for item in prop_obj.reskate_linked_lights:
            if not getattr(item, "enabled", True):
                continue
            l_obj = getattr(item, "light_obj", None)
            if l_obj and l_obj.name in bpy.data.objects and l_obj.type == 'LIGHT':
                if l_obj.name not in seen:
                    seen.add(l_obj.name)
                    candidates.append(l_obj)

    # 2. Direct children parented to this specific prop
    for child in prop_obj.children:
        if child.type == 'LIGHT' and child.name not in seen:
            seen.add(child.name)
            candidates.append(child)

    p_loc = prop_obj.matrix_world.to_translation()
    sm = float(scale_mult) if scale_mult else 1.0

    lights = []
    for l_obj in candidates:
        l_data = l_obj.data
        if not l_data or l_data.type not in ('SPOT', 'POINT'):
            continue

        # Compute origin-relative world offset & world rotation matching the baked visual mesh frame
        l_loc = l_obj.matrix_world.to_translation()
        pos = [
            float((l_loc.x - p_loc.x) * sm),
            float((l_loc.y - p_loc.y) * sm),
            float((l_loc.z - p_loc.z) * sm),
        ]
        quat = [float(v) for v in l_obj.matrix_world.to_quaternion()]

        is_spot = (l_data.type == 'SPOT')
        # Read parameters directly from the native Blender Light data!
        if is_spot:
            # Blender spot_size is full cone angle in radians; Frostbite outer_cone is half-angle (degrees/radians)
            half_cone_rad = float(getattr(l_data, "spot_size", math.radians(100.0))) * 0.5
            spot_blend = float(getattr(l_data, "spot_blend", 0.40))
            outer_deg = math.degrees(half_cone_rad)
            inner_deg = outer_deg * max(0.05, 1.0 - spot_blend)
        else:
            outer_deg = 50.0
            inner_deg = 30.0

        use_cust_dist = bool(getattr(l_data, "use_custom_distance", False))
        cutoff = float(getattr(l_data, "cutoff_distance", 30.0))
        radius = cutoff if use_cust_dist and cutoff > 0.1 else 30.0
        intensity = float(getattr(l_data, "energy", 51.83))
        color = [float(c) for c in getattr(l_data, "color", (1.0, 0.70, 0.50))]
        # Noble QuickDrop guide: custom prop lights default to cast_shadows=False unless explicitly enabled
        if "reskate_shadow_initialized" not in l_obj:
            try:
                l_data.use_shadow = False
                l_obj["reskate_shadow_initialized"] = 1
            except Exception:
                pass
        shadows = bool(getattr(l_data, "use_shadow", False))

        lights.append({
            "name": l_obj.name,
            "type": 'SPOT' if is_spot else 'POINT',
            "pos": pos,
            "rot": quat,
            "color": color,
            "intensity": intensity,
            "inner_cone": inner_deg,
            "outer_cone": outer_deg,
            "radius": radius,
            "cast_shadows": shadows,
        })

    return lights


def extract_prop_grind_paths(prop_obj: bpy.types.Object, scale_mult: float = 1.0) -> list[dict]:
    """
    Extracts both named vertex-path grind entries (`prop_obj.reskate_grind_paths`)
    and any auto-detected / marked `reskate_grind` edges in origin-relative Z-up coordinates.
    """
    if not prop_obj or prop_obj.type != 'MESH':
        return []

    mesh = prop_obj.data
    _, p_rot, p_sca = prop_obj.matrix_world.decompose()
    local_xform = (
        p_rot.to_matrix().to_4x4()
        @ Matrix.Diagonal((p_sca.x * scale_mult, p_sca.y * scale_mult, p_sca.z * scale_mult, 1.0))
    )

    paths_out = []

    # 1. Named vertex-path grind entries
    if hasattr(prop_obj, "reskate_grind_paths"):
        for gp in prop_obj.reskate_grind_paths:
            if not getattr(gp, "enabled", True):
                continue
            raw_csv = getattr(gp, "vertex_indices_csv", "").strip()
            if not raw_csv:
                continue
            v_indices = []
            for tok in raw_csv.split(","):
                tok = tok.strip()
                if tok.isdigit():
                    idx = int(tok)
                    if 0 <= idx < len(mesh.vertices):
                        v_indices.append(idx)
            if len(v_indices) < 2:
                continue
            segs = []
            for i in range(len(v_indices) - 1):
                v0 = local_xform @ mesh.vertices[v_indices[i]].co
                v1 = local_xform @ mesh.vertices[v_indices[i + 1]].co
                if (v1 - v0).length > 1e-4:
                    segs.append(([float(v0.x), float(v0.y), float(v0.z)], [float(v1.x), float(v1.y), float(v1.z)]))
            if segs:
                surf_key = getattr(gp, "surface", "Metal_Solid_Grindable")
                paths_out.append({
                    "name": gp.name,
                    "type": getattr(gp, "grind_type", "LEDGE"),
                    "radius": float(getattr(gp, "radius", 0.032)),
                    "surface_id": surf_key,
                    "surface_code": int(GRIND_SURFACE_CODE_MAP.get(surf_key, 352)),
                    "segments": segs,
                    "z_up": True,
                })

    # 2. Auto-detected / marked edges in `reskate_grind` attribute
    # Edge tag values: 1 = Hard Edge / Ledge, 2 = Round Rail, 3 = Pool / Ramp Coping
    attr = mesh.attributes.get("reskate_grind")
    if attr:
        scene_prop = getattr(bpy.context.scene, "reskate_prop", None) if bpy.context and bpy.context.scene else None
        default_g_type = getattr(scene_prop, "grind_type", "LEDGE") if scene_prop else "LEDGE"
        if scene_prop and getattr(scene_prop, "round_rail", False):
            default_g_type = "ROUND_RAIL"
        g_rad = float(getattr(scene_prop, "grind_tube_radius", 0.032)) if scene_prop else 0.032
        g_surf = getattr(scene_prop, "grind_surface_override", "Metal_Solid_Grindable") if scene_prop else "Metal_Solid_Grindable"

        grouped_segs = {"LEDGE": [], "ROUND_RAIL": [], "COPING": []}
        for e in mesh.edges:
            if e.index < len(attr.data):
                val = int(attr.data[e.index].value)
                if val > 0:
                    v0 = local_xform @ mesh.vertices[e.vertices[0]].co
                    v1 = local_xform @ mesh.vertices[e.vertices[1]].co
                    if (v1 - v0).length > 1e-4:
                        seg = ([float(v0.x), float(v0.y), float(v0.z)], [float(v1.x), float(v1.y), float(v1.z)])
                        if val == 2:
                            grouped_segs["ROUND_RAIL"].append(seg)
                        elif val == 3:
                            grouped_segs["COPING"].append(seg)
                        else:
                            grouped_segs[default_g_type if default_g_type in grouped_segs else "LEDGE"].append(seg)

        for g_key, seg_list in grouped_segs.items():
            if seg_list and len(paths_out) < 6:
                paths_out.append({
                    "name": f"Marked {g_key.title().replace('_', ' ')} Edges",
                    "type": g_key,
                    "radius": g_rad,
                    "surface_id": g_surf,
                    "surface_code": int(GRIND_SURFACE_CODE_MAP.get(g_surf, 352)),
                    "segments": seg_list[:64],
                    "z_up": True,
                })

    return paths_out


def detect_world_space_grind_edges(
    obj: bpy.types.Object,
    bm: bmesh.types.BMesh | None = None,
    threshold_rad: float = math.radians(38.0),
    min_edge_len: float = 0.0,
    ignore_bottom_pct: float = 0.08,
    open_above: bool = False,
    clearance_h: float = 0.35,
    include_concave: bool = False,
    only_selected: bool = False,
) -> dict[int, int]:
    """
    Shared grind-edge detector used by BOTH the Grindable Edge Visualizer (`gpu_draw.py`)
    and `Mark Edges` (`operators.py`) so preview and marked edges are 100% identical (§6.3).
    Evaluates mesh edges in WORLD SPACE (`obj.matrix_world`) and returns `{edge_index: grind_tag}`:
      - `1` = Hard Edge / Ledge (sharp or beveled top crease)
      - `2` = Round Rail (cylindrical / smooth pipe top ridge)
    Non-destructively welds split vertices (`dist=0.001`) on an in-memory BMesh copy so split normals
    or UV-seam hard edges still detect their true dihedral crease angles, then maps back to `bm.edges`
    via KDTree nearest-welded-vertex correspondence.
    """
    from mathutils.kdtree import KDTree
    from mathutils.bvhtree import BVHTree

    if not obj or getattr(obj, "type", None) != 'MESH' or not obj.data:
        return {}

    owns_bm = False
    if bm is None:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        owns_bm = True

    try:
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        mw = obj.matrix_world
        rot_scale = mw.to_3x3()
        try:
            normal_mat = rot_scale.inverted().transposed()
        except Exception:
            normal_mat = rot_scale

        bm_w = bm.copy()
        try:
            bmesh.ops.remove_doubles(bm_w, verts=bm_w.verts[:], dist=0.001)
        except Exception:
            pass
        bm_w.verts.ensure_lookup_table()
        bm_w.edges.ensure_lookup_table()
        bm_w.faces.ensure_lookup_table()

        v_world = [mw @ v.co for v in bm_w.verts]
        if not v_world:
            bm_w.free()
            return {}

        kd = KDTree(len(v_world))
        for w_idx, wp in enumerate(v_world):
            kd.insert(wp, w_idx)
        kd.balance()

        orig_v_to_w: list[int] = []
        for orig_v in bm.verts:
            _, w_idx, _ = kd.find(mw @ orig_v.co)
            orig_v_to_w.append(int(w_idx))

        orig_edge_map: dict[tuple[int, int], list[int]] = {}
        selected_edge_keys: set[tuple[int, int]] = set()
        for orig_e in bm.edges:
            w0 = orig_v_to_w[orig_e.verts[0].index]
            w1 = orig_v_to_w[orig_e.verts[1].index]
            if w0 == w1:
                continue
            pair_key = (w0, w1) if w0 <= w1 else (w1, w0)
            orig_edge_map.setdefault(pair_key, []).append(orig_e.index)
            if orig_e.select:
                selected_edge_keys.add(pair_key)

        min_z = min(p.z for p in v_world)
        max_z = max(p.z for p in v_world)
        height_span = max(0.05, max_z - min_z)
        ground_cutoff_z = min_z + max(0.012, height_span * max(0.0, min(0.5, float(ignore_bottom_pct))))

        f_norm_w: list[Vector] = []
        f_center_w: list[Vector] = []
        for f in bm_w.faces:
            nw = normal_mat @ f.normal
            if nw.length_squared > 1e-10:
                nw.normalize()
            else:
                nw = Vector((0.0, 0.0, 1.0))
            f_norm_w.append(nw)
            f_center_w.append(mw @ f.calc_center_median())

        bvh_world = None
        if open_above and clearance_h > 0.04 and len(bm_w.faces) > 0:
            try:
                bm_bvh = bm_w.copy()
                for v in bm_bvh.verts:
                    v.co = mw @ v.co
                bvh_world = BVHTree.FromBMesh(bm_bvh)
                bm_bvh.free()
            except Exception:
                bvh_world = None

        marked_w: dict[int, int] = {}
        suppressed_bevel_edges: set[int] = set()
        pipe_candidates: list[int] = []

        def _trace_bevel_strip_from_top_edge(top_edge, f_top, f_first_bevel) -> list[int] | None:
            n_top = f_norm_w[f_top.index]
            p0_top = v_world[top_edge.verts[0].index]
            p1_top = v_world[top_edge.verts[1].index]
            seg_top = p1_top - p0_top
            len_top = seg_top.length
            if len_top < 1e-4:
                return None
            dir_top = seg_top / len_top
            mid_top = 0.5 * (p0_top + p1_top)

            cur_edge = top_edge
            cur_face = f_first_bevel
            total_width = 0.0
            lower_edges: list[int] = []
            visited_faces = {f_top.index, cur_face.index}

            for _depth in range(3):
                cur_v0, cur_v1 = cur_edge.verts[0].index, cur_edge.verts[1].index
                cur_mid = 0.5 * (v_world[cur_v0] + v_world[cur_v1])
                best_opp = None
                best_width = 999.0

                for cand_e in cur_face.edges:
                    if cand_e.index == cur_edge.index:
                        continue
                    cv0, cv1 = cand_e.verts[0].index, cand_e.verts[1].index
                    if cv0 in (cur_v0, cur_v1) or cv1 in (cur_v0, cur_v1):
                        continue
                    cp0 = v_world[cv0]
                    cp1 = v_world[cv1]
                    c_seg = cp1 - cp0
                    c_len = c_seg.length
                    if c_len < 1e-4:
                        continue
                    c_dir = c_seg / c_len
                    if abs(c_dir.dot(dir_top)) < 0.68:
                        continue
                    c_mid = 0.5 * (cp0 + cp1)
                    delta_m = c_mid - cur_mid
                    perp_vec = delta_m - dir_top * delta_m.dot(dir_top)
                    w_step = perp_vec.length
                    if 1e-4 < w_step < best_width:
                        best_width = w_step
                        best_opp = cand_e

                if best_opp is None:
                    return None

                total_width += best_width
                if total_width > 0.095:
                    return None

                opp_mid = 0.5 * (v_world[best_opp.verts[0].index] + v_world[best_opp.verts[1].index])
                if opp_mid.z > mid_top.z + 0.008:
                    return None

                lower_edges.append(best_opp.index)
                if len(best_opp.link_faces) != 2:
                    return None
                next_face = best_opp.link_faces[0] if best_opp.link_faces[1].index == cur_face.index else best_opp.link_faces[1]
                if next_face.index in visited_faces:
                    return None
                visited_faces.add(next_face.index)

                n_next = f_norm_w[next_face.index]
                cum_angle = n_top.angle(n_next, 0.0)
                if n_next.z < 0.50 and cum_angle >= min(threshold_rad, math.radians(32.0)):
                    return lower_edges

                if n_next.z >= f_norm_w[cur_face.index].z + 0.05:
                    return None
                cur_edge = best_opp
                cur_face = next_face

            return None

        for edge in bm_w.edges:
            w0, w1 = edge.verts[0].index, edge.verts[1].index
            pair_key = (w0, w1) if w0 <= w1 else (w1, w0)
            if only_selected and pair_key not in selected_edge_keys:
                continue

            p0 = v_world[w0]
            p1 = v_world[w1]
            seg = p1 - p0
            seg_len = seg.length
            if seg_len < 1e-4:
                continue
            edge_dir = seg / seg_len

            if abs(edge_dir.z) > 0.75:
                continue
            mid_pt = 0.5 * (p0 + p1)
            if mid_pt.z <= ground_cutoff_z and height_span > 0.06:
                continue

            num_lf = len(edge.link_faces)
            if num_lf == 0:
                marked_w[edge.index] = 2
                continue
            if num_lf == 1:
                f1 = edge.link_faces[0]
                n1 = f_norm_w[f1.index]
                c1 = f_center_w[f1.index]
                if n1.z > 0.65 and mid_pt.z >= c1.z - 0.015 and mid_pt.z > min_z + 0.30 * height_span:
                    marked_w[edge.index] = 1
                continue
            if num_lf > 2:
                sorted_lf = sorted(edge.link_faces, key=lambda f: f_norm_w[f.index].z, reverse=True)
                f1, f2 = sorted_lf[0], sorted_lf[1]
            else:
                f1, f2 = edge.link_faces[0], edge.link_faces[1]

            n1 = f_norm_w[f1.index]
            n2 = f_norm_w[f2.index]
            c1 = f_center_w[f1.index]
            c2 = f_center_w[f2.index]

            if bvh_world is not None:
                avg_n = n1 + n2
                if avg_n.length_squared > 1e-8:
                    avg_n.normalize()
                else:
                    avg_n = Vector((0.0, 0.0, 1.0))
                ray_orig = mid_pt + avg_n * 0.02 + Vector((0.0, 0.0, 0.06))
                hit_loc, _, hit_f_idx, hit_dist = bvh_world.ray_cast(
                    ray_orig, Vector((0.0, 0.0, 1.0)), max(0.08, float(clearance_h))
                )
                if hit_loc is not None and hit_dist is not None and hit_dist > 0.04:
                    adj_face_ids = {f.index for v in edge.verts for f in v.link_faces}
                    if hit_f_idx not in adj_face_ids:
                        continue

            d1 = (c1 - mid_pt) - edge_dir * (c1 - mid_pt).dot(edge_dir)
            d2 = (c2 - mid_pt) - edge_dir * (c2 - mid_pt).dot(edge_dir)
            if d1.length_squared > 1e-12:
                d1.normalize()
            if d2.length_squared > 1e-12:
                d2.normalize()
            cross_convex = (d2.dot(n1) + d1.dot(n2)) <= 0.08
            max_nz = max(n1.z, n2.z)
            min_nz = min(n1.z, n2.z)
            c_lower_z = c2.z if n1.z >= n2.z else c1.z
            top_lip_drop = (max_nz > 0.55 and min_nz < 0.65 and c_lower_z < mid_pt.z - 0.004)
            is_convex = cross_convex or top_lip_drop
            if not is_convex and not include_concave:
                continue

            angle = n1.angle(n2, 0.0)

            if n1.z > 0.45 and n2.z > 0.45 and math.radians(4.0) <= angle <= math.radians(55.0) and is_convex:
                pipe_candidates.append(edge.index)
                continue

            if max_nz > 0.65 and min_nz >= 0.02 and angle >= math.radians(8.0):
                f_top = f1 if n1.z >= n2.z else f2
                f_bev = f2 if n1.z >= n2.z else f1
                low_edges = _trace_bevel_strip_from_top_edge(edge, f_top, f_bev)
                if low_edges is not None:
                    marked_w[edge.index] = 1
                    suppressed_bevel_edges.update(low_edges)
                    continue

            if angle >= threshold_rad and max_nz > 0.15:
                marked_w[edge.index] = 1
                continue

            if angle >= math.radians(18.0) and max_nz > 0.70 and min_nz < 0.68:
                marked_w[edge.index] = 1
                continue

        if suppressed_bevel_edges:
            for sb_idx in suppressed_bevel_edges:
                marked_w.pop(sb_idx, None)

        if pipe_candidates:
            cand_set = set(pipe_candidates)
            for e_idx in pipe_candidates:
                edge = bm_w.edges[e_idx]
                p0 = v_world[edge.verts[0].index]
                p1 = v_world[edge.verts[1].index]
                mid_z = 0.5 * (p0.z + p1.z)
                f1, f2 = edge.link_faces[0], edge.link_faces[1]
                n1 = f_norm_w[f1.index]
                n2 = f_norm_w[f2.index]

                my_score = mid_z + 0.05 * (n1.z + n2.z)
                is_local_crest = True
                for adj_f in (f1, f2):
                    for other_e in adj_f.edges:
                        if other_e.index == e_idx or other_e.index not in cand_set:
                            continue
                        op0 = v_world[other_e.verts[0].index]
                        op1 = v_world[other_e.verts[1].index]
                        omid_z = 0.5 * (op0.z + op1.z)
                        of1, of2 = other_e.link_faces[0], other_e.link_faces[1]
                        other_score = omid_z + 0.05 * (f_norm_w[of1.index].z + f_norm_w[of2.index].z)
                        if other_score > my_score + 1e-4 or (abs(other_score - my_score) <= 1e-4 and other_e.index < e_idx):
                            is_local_crest = False
                            break
                    if not is_local_crest:
                        break

                if is_local_crest:
                    marked_w[e_idx] = 2

            if any(v == 2 for v in marked_w.values()):
                for k in [k for k, v in marked_w.items() if v == 1]:
                    e_k = bm_w.edges[k]
                    if (v_world[e_k.verts[1].index] - v_world[e_k.verts[0].index]).length < 0.08:
                        del marked_w[k]

        if min_edge_len > 1e-3 and marked_w:
            vert_to_marked_edges: dict[int, list[int]] = {}
            for e_idx in marked_w:
                ed = bm_w.edges[e_idx]
                vert_to_marked_edges.setdefault(ed.verts[0].index, []).append(e_idx)
                vert_to_marked_edges.setdefault(ed.verts[1].index, []).append(e_idx)

            visited_e: set[int] = set()
            for e_idx in list(marked_w.keys()):
                if e_idx in visited_e:
                    continue
                comp_edges = []
                stack = [e_idx]
                visited_e.add(e_idx)
                total_chain_len = 0.0
                while stack:
                    cur_e = stack.pop()
                    comp_edges.append(cur_e)
                    ed = bm_w.edges[cur_e]
                    total_chain_len += (v_world[ed.verts[1].index] - v_world[ed.verts[0].index]).length
                    for v_i in (ed.verts[0].index, ed.verts[1].index):
                        for nbr_e in vert_to_marked_edges.get(v_i, []):
                            if nbr_e not in visited_e:
                                visited_e.add(nbr_e)
                                stack.append(nbr_e)
                if total_chain_len < float(min_edge_len):
                    for ce in comp_edges:
                        marked_w.pop(ce, None)

        marked_orig: dict[int, int] = {}
        for e_idx, tag_val in marked_w.items():
            ed = bm_w.edges[e_idx]
            w0, w1 = ed.verts[0].index, ed.verts[1].index
            pair_key = (w0, w1) if w0 <= w1 else (w1, w0)
            for orig_idx in orig_edge_map.get(pair_key, []):
                marked_orig[orig_idx] = tag_val

        bm_w.free()
        return marked_orig
    finally:
        if owns_bm and bm is not None:
            try:
                bm.free()
            except Exception:
                pass


_SK8_SURFACE_SHORTHAND_MAP = {
    "concrete": ("concrete_smooth", 96),
    "wood": ("wood_plywood", 736),
    "metal": ("metal_solid", 352),
    "asphalt": ("asphalt_smooth", 64),
    "brick": ("brick_coarse", 544),
    "tile": ("tile_marble", 160),
    "grass": ("grass", 992),
    "dirt": ("dirt", 928),
    "sand": ("sand", 1216),
    "glass": ("glass_solid", 1056),
}


def _guess_material_surface_preset(mat_name: str) -> str:
    low = (mat_name or "").lower()
    keyword_map = [
        (("chainlink", "chain_link", "fence_wire", "wire_mesh"), "chainlink_fence"),
        (("diamond_plate", "diamondplate", "tread_plate"), "metal_diamond_plate"),
        (("grate", "grating", "catwalk", "drain_cover"), "metal_grate"),
        (("skirt", "flashing"), "metal_skirt"),
        (("round_rail", "handrail", "rail_tube"), "metal_rail"),
        (("sheet_metal", "metal_thin", "sign", "tin"), "metal_thin"),
        (("metal", "steel", "iron", "aluminum", "chrome", "coping"), "metal_grindable"),
        (("plastic_smooth", "acrylic", "fiberglass", "plexiglass"), "plastic_smooth"),
        (("plastic_heavy", "hdpe"), "plastic_heavy"),
        (("plastic", "poly", "cone", "barrier", "pvc"), "plastic_solid"),
        (("rubber", "tire", "bumper", "neoprene"), "rubber"),
        (("glass", "window", "windshield"), "glass"),
        (("cardboard", "carton"), "cardboard"),
        (("masonite", "skatelite", "wood_thin_smooth"), "wood_thin_smooth"),
        (("pallet", "wood_thin_rough", "weathered_wood"), "wood_thin_rough"),
        (("plywood", "wood_ramp", "ramp_wood"), "wood_ramp"),
        (("bark", "tree_trunk"), "bark"),
        (("wood", "timber", "plank", "lumber", "oak", "pine"), "wood_thick"),
        (("marble", "granite", "tile", "terrazzo"), "marble_tile"),
        (("brick", "paver", "masonry", "cinder"), "brick"),
        (("shingle", "roofing"), "roofing_asphalt"),
        (("asphalt", "tarmac", "road", "blacktop"), "asphalt"),
        (("bedrock", "boulder", "rock", "cliff"), "bedrock"),
        (("concrete_rough", "sidewalk"), "concrete_rough"),
        (("concrete", "cement", "plaza", "ledge"), "concrete_smooth"),
        (("canvas",), "canvas"),
        (("leather", "vinyl_seat"), "leather"),
        (("cloth", "tarp", "fabric", "banner", "tent"), "cloth"),
        (("grass", "turf", "lawn", "foliage"), "grass"),
        (("gravel", "ballast"), "gravel"),
        (("sand",), "sand_gravel"),
        (("dirt", "earth", "soil", "mud"), "earth_dirt"),
        (("water", "puddle", "pool_water"), "water"),
        (("jump_pad", "jumppad", "bounce"), "jump_pad"),
        (("stair", "steps"), "stairs_concrete"),
        (("wipeout", "bail"), "wipeout_hazard"),
        (("no_grind", "nogrind", "skatestopper"), "no_grind_plastic"),
    ]
    for keys, preset in keyword_map:
        if any(k in low for k in keys):
            return preset
    return "DEFAULT"


def infer_surface_from_material_slot(mat: bpy.types.Material | None) -> tuple[str, int | None]:
    """
    Checks a Blender Material for sk8_map_export collision properties (`sk8_collision_material_packed`,
    `sk8_material.collision_material`, `sk8_material.surface`, or material name keywords).
    Returns (surface_identifier, packed_code_or_None).
    """
    if mat is None:
        return ('DEFAULT', None)
    try:
        raw_packed = mat.get("sk8_collision_material_packed")
        if raw_packed is not None:
            packed = int(raw_packed)
            cand_id = f"material_{packed:04d}"
            return (cand_id, packed)
    except Exception:
        pass
    sk8_mat = getattr(mat, "sk8_material", None)
    if sk8_mat is not None:
        if hasattr(sk8_mat, "collision_material"):
            try:
                is_explicit = hasattr(sk8_mat, "is_property_set") and sk8_mat.is_property_set("collision_material")
                if is_explicit:
                    col_mat_id = str(sk8_mat.collision_material)
                    if col_mat_id:
                        return (col_mat_id, get_surface_packed_code(col_mat_id))
            except Exception:
                pass
        if hasattr(sk8_mat, "surface"):
            try:
                surf_short = str(getattr(sk8_mat, "surface", "default")).lower()
                if surf_short in _SK8_SURFACE_SHORTHAND_MAP:
                    return _SK8_SURFACE_SHORTHAND_MAP[surf_short]
            except Exception:
                pass
    guessed = _guess_material_surface_preset(mat.name)
    if guessed != "DEFAULT":
        return (guessed, get_surface_packed_code(guessed))
    return ('DEFAULT', None)


def ensure_unique_object_material(
    obj: bpy.types.Object | None,
    slot_idx: int | None = None,
) -> bpy.types.Material | None:
    """
    Ensures the material in `obj.material_slots[slot_idx]` (or active slot) is uniquely owned
    by `obj` (`mat.users <= 1` and not shared with any other object in `bpy.data.objects`).
    If shared across multiple objects, forks `mat.copy()` and assigns it to `obj` so editing
    templates, shaders, or surface audio on `obj` never mutates other objects in the scene!
    """
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return None
    if len(obj.material_slots) == 0:
        mat = bpy.data.materials.new(name=f"ReSkate_{obj.name}_Mat")
        mat.use_nodes = True
        obj.data.materials.append(mat)
        return mat

    if slot_idx is None:
        slot_idx = max(0, min(int(getattr(obj, "active_material_index", 0) or 0), len(obj.material_slots) - 1))
    elif not (0 <= slot_idx < len(obj.material_slots)):
        return None

    mat = obj.material_slots[slot_idx].material
    if mat is None:
        mat = bpy.data.materials.new(name=f"ReSkate_{obj.name}_Mat")
        mat.use_nodes = True
        obj.material_slots[slot_idx].material = mat
        return mat

    # Check if any other object (excluding `<obj.name>_COL` collision helper) shares this material
    shared_with_other = False
    if getattr(mat, "users", 1) > 1:
        col_helper_name = f"{obj.name}_COL"
        for other_obj in bpy.data.objects:
            if other_obj == obj or other_obj.name == col_helper_name or getattr(other_obj, "type", None) != 'MESH':
                continue
            if any(s.material == mat for s in other_obj.material_slots):
                shared_with_other = True
                break
        # Also check if used multiple times or if mat.users > 1 without an obvious single owner
        if not shared_with_other and getattr(mat, "users", 1) > 2:
            shared_with_other = True

    if shared_with_other:
        old_name = mat.name
        new_mat = mat.copy()
        base_stem = re.sub(r"\.\d+$", "", old_name)
        clean_obj = re.sub(r"[^a-zA-Z0-9_]", "_", obj.name)[:24]
        new_mat.name = f"{base_stem}_{clean_obj}"
        obj.material_slots[slot_idx].material = new_mat
        if hasattr(obj, "reskate_material_surfaces") and 0 <= slot_idx < len(obj.reskate_material_surfaces):
            try:
                obj.reskate_material_surfaces[slot_idx].material_name = new_mat.name
            except Exception:
                pass
        return new_mat

    return mat


def ensure_all_object_materials_unique(obj: bpy.types.Object | None) -> list[bpy.types.Material]:
    """Forks any shared materials across all material slots on `obj` so each slot is unique to `obj`."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return []
    out = []
    for idx in range(len(obj.material_slots)):
        m = ensure_unique_object_material(obj, idx)
        if m is not None:
            out.append(m)
    return out


def get_synced_material_surface_info(
    prop_obj: bpy.types.Object | None,
    slot_idx_or_mat=0,
    mat_or_idx=None,
    default_code: int = 96,
    **kwargs,
) -> dict:
    """
    Resolves the synced Collision Surface & Contact Audio for a material slot on `prop_obj`.
    Keeps 2. Collision per-material surface and 4. Look Surface / Contact Audio in sync.
    Always reads per-prop defaults from `prop_obj`'s own saved state so batch compilation
    or shared materials never leak `Do-Not-Align` across unrelated objects.
    """
    if isinstance(slot_idx_or_mat, int):
        slot_idx = slot_idx_or_mat
        mat = mat_or_idx if isinstance(mat_or_idx, bpy.types.Material) else None
    else:
        mat = slot_idx_or_mat if isinstance(slot_idx_or_mat, bpy.types.Material) else None
        slot_idx = int(mat_or_idx) if isinstance(mat_or_idx, int) else 0

    obj_state = get_or_init_object_prop_state(prop_obj) if (prop_obj and getattr(prop_obj, "type", None) == 'MESH') else {}
    if (default_code is None or default_code == 96) and obj_state:
        st_code = int(obj_state.get("surface_material_code", 0) or 0)
        if st_code > 0:
            default_code = st_code
        elif obj_state.get("primary_surface"):
            default_code = int(get_surface_packed_code(obj_state.get("primary_surface")))

    surf_choice = "DEFAULT"
    slot_prevent_wall_snap = False
    if prop_obj and hasattr(prop_obj, "reskate_material_surfaces") and 0 <= slot_idx < len(prop_obj.reskate_material_surfaces):
        slot_item = prop_obj.reskate_material_surfaces[slot_idx]
        surf_choice = getattr(slot_item, "surface", "DEFAULT")
        slot_prevent_wall_snap = bool(getattr(slot_item, "prevent_wall_snap", False))

    ext = (getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)) if mat else None
    if ext is not None and hasattr(ext, "surface_role_preset"):
        role_val = str(getattr(ext, "surface_role_preset", "AUTO_FROM_NAME") or "AUTO_FROM_NAME")
        if role_val == "ROUND_METAL_RAIL":
            surf_choice = "metal_rail"
        elif role_val not in ("AUTO_FROM_NAME", "DECAL_OVERLAY", "DEFAULT", ""):
            surf_choice = role_val
        elif role_val == "AUTO_FROM_NAME" and mat is not None and bool(mat.get("reskate_user_set_surface_role", "") == "AUTO_FROM_NAME"):
            surf_choice = "DEFAULT"

    if obj_state:
        default_prevent_wall_snap = bool(obj_state.get("prevent_wall_snap_default", False))
    else:
        scene_prop = getattr(bpy.context.scene, "reskate_prop", None) if (bpy.context and getattr(bpy.context, "scene", None)) else None
        default_prevent_wall_snap = bool(getattr(scene_prop, "prevent_wall_snap_default", False)) if scene_prop else False
    prevent_wall_snap = slot_prevent_wall_snap or default_prevent_wall_snap

    if surf_choice == "DEFAULT" and mat is not None:
        inferred_id, inferred_packed = infer_surface_from_material_slot(mat)
        # If the object is a single-material prop with an explicit non-default primary_surface,
        # or if a shared material leaked a Do-Not-Align code while prevent_wall_snap is False on this prop,
        # honor the prop's own surface & alignment state!
        has_single_slot = (prop_obj is not None and len(getattr(prop_obj, "material_slots", [])) <= 1)
        prop_has_explicit_surface = bool(obj_state and (obj_state.get("primary_surface", "concrete_smooth") != "concrete_smooth" or int(obj_state.get("surface_material_code", 96) or 96) != 96))
        if has_single_slot and prop_has_explicit_surface:
            surf_choice = str(obj_state.get("primary_surface") or "DEFAULT")
            packed = int(default_code)
        elif inferred_packed is not None:
            packed = int(inferred_packed)
            if not prevent_wall_snap and is_do_not_align_surface_code(packed):
                packed = int(strip_do_not_align_code(packed, default_code))
                surf_choice = f"material_{packed:04d}"
            else:
                surf_choice = inferred_id
        else:
            packed = int(default_code)
    else:
        packed = int(default_code) if surf_choice == "DEFAULT" else int(get_surface_packed_code(surf_choice))

    if not prevent_wall_snap and is_do_not_align_surface_code(packed) and surf_choice == "DEFAULT":
        packed = int(strip_do_not_align_code(packed, 96))
    elif prevent_wall_snap:
        packed = int(apply_prevent_wall_snap_code(packed, surf_choice))

    ext = (getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)) if mat else None
    override_audio = bool(getattr(ext, "override_contact_audio", False)) if ext is not None else False
    audio_code = packed
    if override_audio and mat is not None:
        sk8 = getattr(mat, "sk8_material", None)
        if sk8 is not None and hasattr(sk8, "collision_material"):
            audio_code = int(get_surface_packed_code(getattr(sk8, "collision_material", f"material_{packed:04d}")))

    surf_label = get_surface_label(surf_choice if (surf_choice != "DEFAULT" and not prevent_wall_snap) else packed)
    audio_lbl = get_surface_label(audio_code)
    badges = get_surface_behavior_badges(surf_choice if surf_choice != "DEFAULT" else packed, packed)
    return {
        "surface": surf_choice,
        "collision_preset": surf_choice,
        "packed": int(packed),
        "label": surf_label,
        "prevent_wall_snap": bool(prevent_wall_snap),
        "badges": badges,
        "override_audio": override_audio,
        "audio_packed": int(audio_code),
        "audio_label": audio_lbl,
        "contact_audio": audio_lbl,
    }


def _compute_prop_alignment_sig(prop_obj: bpy.types.Object | None, context=None) -> tuple:
    """Computes a lightweight immutable signature capturing geometry, materials, JSON state, and collision helper state."""
    if prop_obj is None or getattr(prop_obj, "type", None) != 'MESH' or prop_obj.data is None:
        return ()
    me = prop_obj.data
    raw_st = str(prop_obj.get("reskate_prop_state", "") or prop_obj.get("reskate_prop_state_json", ""))
    st_sig = (len(raw_st), hash(raw_st)) if raw_st else (0, 0)

    raw_fs = str(prop_obj.get("reskate_face_surfaces_json", ""))
    fs_sig = (len(raw_fs), hash(raw_fs)) if raw_fs else (0, 0)

    attr = me.attributes.get("reskate_face_surface") if hasattr(me, "attributes") else None
    attr_len = len(attr.data) if (attr is not None and hasattr(attr, "data")) else 0

    slots_sig = tuple(
        (
            s.material.name if s.material else "",
            int(s.material.get("sk8_collision_material_packed", 0) or 0) if s.material else 0,
        )
        for s in prop_obj.material_slots
    )

    if prop_obj.mode == 'EDIT' and getattr(me, "is_editmode", False):
        try:
            import bmesh
            bm = bmesh.from_edit_mesh(me)
            vert_count = len(bm.verts)
            poly_count = len(bm.faces)
        except Exception:
            vert_count = len(me.vertices)
            poly_count = len(me.polygons)
    else:
        vert_count = len(me.vertices)
        poly_count = len(me.polygons)

    col_obj = resolve_custom_collision_object(context, prop_obj) if context else None
    if col_obj is not None and getattr(col_obj, "type", None) == 'MESH' and col_obj.data is not None:
        c_me = col_obj.data
        c_fs = str(col_obj.get("reskate_face_surfaces_json", ""))
        c_fs_sig = (len(c_fs), hash(c_fs)) if c_fs else (0, 0)
        c_attr = c_me.attributes.get("reskate_face_surface") if hasattr(c_me, "attributes") else None
        c_attr_len = len(c_attr.data) if (c_attr is not None and hasattr(c_attr, "data")) else 0
        if col_obj.mode == 'EDIT' and getattr(c_me, "is_editmode", False):
            try:
                import bmesh
                c_bm = bmesh.from_edit_mesh(c_me)
                c_verts = len(c_bm.verts)
                c_polys = len(c_bm.faces)
            except Exception:
                c_verts = len(c_me.vertices)
                c_polys = len(c_me.polygons)
        else:
            c_verts = len(c_me.vertices)
            c_polys = len(c_me.polygons)
        col_sig = (
            col_obj.name,
            col_obj.mode,
            c_verts,
            c_polys,
            c_fs_sig,
            c_attr_len,
        )
    else:
        col_sig = ()

    return (
        prop_obj.name,
        prop_obj.mode,
        vert_count,
        poly_count,
        st_sig,
        fs_sig,
        attr_len,
        slots_sig,
        col_sig,
    )


def inspect_prop_alignment_issues(context, prop_obj: bpy.types.Object | None) -> dict:
    """
    Inspects `prop_obj` (and its collision helper if present) for any active `Do Not Align`
    (`prevent_wall_snap`, `PropertySlot 111`, legacy `527456`) or `No-Grind` (`0x0800`, `5920`,
    `PropertySlot 68/105`) settings across:
      1. Prop-level default surface (`prevent_wall_snap_default`, `surface_material_code`),
      2. Material slots (`reskate_material_surfaces`, `mat['sk8_collision_material_packed']`),
      3. Per-face painted surface overrides (`reskate_face_surface` on `prop_obj` and `<Prop>_COL`),
      4. Marked Grind Paths (`surface == 'No_Grind'`).
    Used by both the 3D GPU Alignment Overlay and the N-Panel troubleshooting banner.
    """
    res = {
        "has_do_not_align": False,
        "has_no_grind": False,
        "prop_level_do_not_align": False,
        "prop_level_no_grind": False,
        "prop_surface_code": 96,
        "prop_surface_label": "Concrete (Smooth / Plaza)",
        "slot_issues": [],
        "face_do_not_align_count": 0,
        "face_no_grind_count": 0,
        "summary_lines": [],
    }
    if prop_obj is None or getattr(prop_obj, "type", None) != 'MESH':
        return res


    sig = _compute_prop_alignment_sig(prop_obj, context=context)
    cached = _ALIGNMENT_ISSUES_CACHE.get(prop_obj.name)
    if cached is not None and cached[0] == sig:
        return dict(cached[1])

    obj_state = get_or_init_object_prop_state(prop_obj)
    default_code = int(obj_state.get("surface_material_code", 96) or 96)
    prevent_def = bool(obj_state.get("prevent_wall_snap_default", False))
    if prevent_def:
        default_code = int(apply_prevent_wall_snap_code(default_code, str(obj_state.get("primary_surface", ""))))
    res["prop_surface_code"] = default_code
    res["prop_surface_label"] = get_surface_label(default_code)

    if prevent_def or is_do_not_align_surface_code(default_code):
        res["has_do_not_align"] = True
        res["prop_level_do_not_align"] = True
        res["summary_lines"].append(f"Prop Default Surface has 'Do Not Align' active ({res['prop_surface_label']}, code {default_code})")
    if is_no_grind_surface_code(default_code):
        res["has_no_grind"] = True
        res["prop_level_no_grind"] = True
        res["summary_lines"].append(f"Prop Default Surface is No-Grind ({res['prop_surface_label']}, code {default_code})")

    for idx, slot in enumerate(prop_obj.material_slots):
        info = get_synced_material_surface_info(prop_obj, idx, slot.material, default_code=default_code)
        p_code = int(info.get("packed", default_code))
        is_dna = bool(info.get("prevent_wall_snap", False)) or is_do_not_align_surface_code(p_code)
        # Also check if the raw material has a lingering sk8_collision_material_packed with Do-Not-Align
        raw_mat_packed = int(slot.material.get("sk8_collision_material_packed", 0) or 0) if slot.material else 0
        has_stale_mat_dna = is_do_not_align_surface_code(raw_mat_packed)
        is_ng = is_no_grind_surface_code(p_code) or is_no_grind_surface_code(raw_mat_packed)
        if is_dna or has_stale_mat_dna or is_ng:
            mat_nm = slot.material.name if slot.material else f"Slot {idx + 1}"
            issue = {
                "slot_index": idx,
                "material_name": mat_nm,
                "packed": p_code,
                "raw_mat_packed": raw_mat_packed,
                "label": info.get("label", ""),
                "do_not_align": bool(is_dna or has_stale_mat_dna),
                "no_grind": bool(is_ng),
            }
            res["slot_issues"].append(issue)
            if is_dna or has_stale_mat_dna:
                res["has_do_not_align"] = True
                if not res["prop_level_do_not_align"]:
                    res["summary_lines"].append(f"Material Slot '{mat_nm}' has 'Do Not Align' (code {raw_mat_packed or p_code})")
            if is_ng:
                res["has_no_grind"] = True
                if not res["prop_level_no_grind"]:
                    res["summary_lines"].append(f"Material Slot '{mat_nm}' has 'No-Grind' (code {raw_mat_packed or p_code})")

    # Check per-face overrides on visual mesh and collision mesh
    col_obj = resolve_custom_collision_object(context, prop_obj) if context else None
    for target_m in [o for o in (prop_obj, col_obj) if o is not None and getattr(o, "type", None) == 'MESH']:
        f_arr = get_mesh_face_surface_array(target_m)
        if f_arr:
            dna_c = sum(1 for c in f_arr if c > 0 and is_do_not_align_surface_code(c))
            ng_c = sum(1 for c in f_arr if c > 0 and is_no_grind_surface_code(c))
            res["face_do_not_align_count"] += dna_c
            res["face_no_grind_count"] += ng_c
            if dna_c > 0:
                res["has_do_not_align"] = True
                res["summary_lines"].append(f"{dna_c} face(s) on '{target_m.name}' painted with 'Do Not Align'")
            if ng_c > 0:
                res["has_no_grind"] = True
                res["summary_lines"].append(f"{ng_c} face(s) on '{target_m.name}' painted with 'No-Grind'")

    if len(_ALIGNMENT_ISSUES_CACHE) > 32:
        _ALIGNMENT_ISSUES_CACHE.clear()
    _ALIGNMENT_ISSUES_CACHE[prop_obj.name] = (sig, res)
    return dict(res)


def extract_material_surfaces(prop_obj: bpy.types.Object, default_code: int) -> list[dict]:
    """Extracts per-material-slot surface overrides if configured on `prop_obj` or via sk8_map_export material tags."""
    out = []
    if not prop_obj or prop_obj.type != 'MESH':
        return out
    for idx, slot in enumerate(prop_obj.material_slots):
        mat_name = slot.material.name if slot.material else f"Slot {idx + 1}"
        info = get_synced_material_surface_info(prop_obj, idx, slot.material, default_code=default_code)
        out.append({
            "slot_index": idx,
            "material_name": mat_name,
            "surface": info["surface"],
            "packed": int(info["packed"]),
            "prevent_wall_snap": bool(info.get("prevent_wall_snap", False)),
            "contact_audio_packed": int(info["audio_packed"]),
        })
    return out


# ---------------------------------------------------------------------------
# Per-Face Surface Audio Override Painter & Collision Partitioning Helpers
# ---------------------------------------------------------------------------

def get_face_surface_overlay_color(surface_id_or_code, packed_code: int | None = None) -> tuple[float, float, float, float]:
    """Returns a distinct vibrant RGBA color for a painted face surface in the 3D Viewport GPU overlay."""
    label_low = (str(surface_id_or_code or "") + " " + get_surface_label(surface_id_or_code if surface_id_or_code is not None else (packed_code or 96))).lower()
    code_val = int(packed_code if packed_code is not None else get_surface_packed_code(surface_id_or_code))
    if is_no_grind_surface_code(code_val) or any(k in label_low for k in ("no-grind", "no_grind", "skatestopper", "wipeout", "hazard")):
        return (0.98, 0.20, 0.24, 0.46)
    if any(k in label_low for k in ("metal", "rail", "steel", "chrome", "grate", "dumpster", "sign", "chainlink", "fence", "gutter", "tin", "iron", "brass")):
        return (0.14, 0.82, 1.00, 0.44)
    if any(k in label_low for k in ("wood", "plywood", "masonite", "bark", "mulch", "bench", "plank", "boardwalk", "timber")):
        return (0.96, 0.58, 0.14, 0.44)
    if any(k in label_low for k in ("grass", "turf", "foliage", "lawn", "bush", "hedge")):
        return (0.20, 0.88, 0.36, 0.44)
    if any(k in label_low for k in ("dirt", "mud", "sand", "gravel", "earth", "soil", "clay")):
        return (0.78, 0.52, 0.24, 0.44)
    if any(k in label_low for k in ("brick", "cobble", "paver", "tile", "terracotta", "masonry", "shingle")):
        return (0.94, 0.34, 0.20, 0.44)
    if any(k in label_low for k in ("marble", "granite", "glass", "ice", "polished")):
        return (0.64, 0.38, 0.98, 0.44)
    if any(k in label_low for k in ("plastic", "tarp", "canvas", "cloth", "rubber", "jump", "vinyl", "leather")):
        return (0.95, 0.26, 0.80, 0.44)
    if any(k in label_low for k in ("water", "puddle")):
        return (0.18, 0.52, 0.98, 0.44)
    # Concrete / Asphalt / Stone — use golden-lime with slight deterministic hue shift per code
    shift = ((code_val * 37) % 100) / 500.0
    return (min(1.0, 0.72 + shift), min(1.0, 0.92 - shift * 0.5), 0.18, 0.42)


def get_mesh_face_surface_array(obj: bpy.types.Object | None) -> list[int]:
    """
    Returns a per-polygon list of `packed` surface codes (`0` = default prop surface, `> 0` = custom override)
    for `obj`, supporting both Edit Mode (`bmesh`) and Object Mode (`mesh.attributes['reskate_face_surface']`
    or `obj['reskate_face_surfaces_json']`).
    """
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return []
    me = obj.data

    # 1. If in Edit Mode, read live from bmesh face int layer first
    if getattr(obj, "mode", "OBJECT") == 'EDIT' and getattr(me, "is_editmode", False):
        try:
            import bmesh
            bm = bmesh.from_edit_mesh(me)
            if len(bm.faces) == 0:
                return []
            attr_name = getattr(sys.modules.get(__name__), "FACE_SURFACE_ATTR_NAME", "reskate_face_surface")
            layer = bm.faces.layers.int.get("reskate_face_surface") or bm.faces.layers.int.get(attr_name)
            if layer is not None:
                return [int(f[layer]) for f in bm.faces]
        except Exception:
            pass

    n_polys = len(me.polygons)
    if n_polys == 0:
        return []

    # 2. Object Mode: read from mesh attribute `reskate_face_surface`
    attr = me.attributes.get("reskate_face_surface")
    if attr is not None and getattr(attr, "domain", "") == 'FACE' and len(attr.data) == n_polys:
        try:
            vals = [0] * n_polys
            attr.data.foreach_get("value", vals)
            if any(v > 0 for v in vals):
                return [int(v) for v in vals]
        except Exception:
            try:
                return [int(d.value) for d in attr.data]
            except Exception:
                pass

    # 3. Fallback: reconstruct from `obj["reskate_face_surfaces_json"]` if present
    raw_json = obj.get("reskate_face_surfaces_json", "")
    if raw_json:
        try:
            data = json.loads(str(raw_json))
            if isinstance(data, dict):
                vals = [0] * n_polys
                for code_str, entry in data.items():
                    code_val = int(code_str)
                    if code_val <= 0 or not isinstance(entry, dict):
                        continue
                    for f_idx in entry.get("face_indices", []):
                        fi = int(f_idx)
                        if 0 <= fi < n_polys:
                            vals[fi] = code_val
                return vals
        except Exception:
            pass

    return [0] * n_polys


def get_object_face_surface_legend(obj: bpy.types.Object | None) -> dict[int, dict]:
    """Returns `{packed_code: {'surface': str, 'label': str, 'prevent_wall_snap': bool}}` stored on `obj`."""
    out: dict[int, dict] = {}
    if obj is None:
        return out
    for key_name in ("reskate_face_surface_legend_json", "reskate_face_surfaces_json"):
        raw = obj.get(key_name, "")
        if not raw:
            continue
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    code_i = int(k)
                    if code_i > 0 and isinstance(v, dict):
                        out[code_i] = {
                            "surface": str(v.get("surface") or f"material_{code_i:04d}"),
                            "label": str(v.get("label") or get_surface_label(v.get("surface") or code_i)),
                            "prevent_wall_snap": bool(v.get("prevent_wall_snap", False)),
                        }
        except Exception:
            pass
    return out


def _compute_face_surface_summary_sig(obj: bpy.types.Object | None, context=None) -> tuple:
    """Computes a signature for get_object_face_surface_summary covering target object, owner, and collision mesh."""
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return ()
    owner = resolve_collision_to_owner_prop(obj) or obj
    col_obj = resolve_custom_collision_object(context, owner) if (owner and owner.type == 'MESH') else None

    target_objs = []
    seen = set()
    for o in (obj, owner, col_obj):
        if o is not None and getattr(o, "type", None) == 'MESH' and o.name not in seen:
            seen.add(o.name)
            target_objs.append(o)

    sigs = []
    for t_obj in target_objs:
        me = t_obj.data
        if me is None:
            continue
        raw_fs = str(t_obj.get("reskate_face_surfaces_json", ""))
        fs_sig = (len(raw_fs), hash(raw_fs)) if raw_fs else (0, 0)
        raw_leg = str(t_obj.get("reskate_face_surface_legend_json", ""))
        leg_sig = (len(raw_leg), hash(raw_leg)) if raw_leg else (0, 0)
        attr = me.attributes.get("reskate_face_surface") if hasattr(me, "attributes") else None
        attr_len = len(attr.data) if (attr is not None and hasattr(attr, "data")) else 0
        loc = t_obj.matrix_world.to_translation()
        loc_sig = (round(loc.x, 3), round(loc.y, 3), round(loc.z, 3))
        if t_obj.mode == 'EDIT' and getattr(me, "is_editmode", False):
            try:
                import bmesh
                bm = bmesh.from_edit_mesh(me)
                vert_count = len(bm.verts)
                poly_count = len(bm.faces)
            except Exception:
                vert_count = len(me.vertices)
                poly_count = len(me.polygons)
        else:
            vert_count = len(me.vertices)
            poly_count = len(me.polygons)
        sigs.append((
            t_obj.name,
            t_obj.mode,
            vert_count,
            poly_count,
            loc_sig,
            fs_sig,
            leg_sig,
            attr_len,
        ))
    return tuple(sigs)


def get_object_face_surface_summary(obj: bpy.types.Object | None, context=None) -> list[dict]:
    """
    Returns a summary list of all custom per-face surface audio overrides painted on `obj`
    (and its `<Prop>_COL` collision object if present), used by `🟧 2. Collision` and `gpu_draw.py`:
      `[{'packed': int, 'surface': str, 'label': str, 'prevent_wall_snap': bool, 'face_count': int,
        'color': (r,g,b,a), 'target_obj_name': str, 'is_col_mesh': bool}]`
    """
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return []

    sig = _compute_face_surface_summary_sig(obj, context=context)
    cached = _FACE_SURFACE_SUMMARY_CACHE.get(obj.name)
    if cached is not None and cached[0] == sig:
        return list(cached[1])

    owner = resolve_collision_to_owner_prop(obj) or obj
    col_obj = resolve_custom_collision_object(context, owner) if owner.type == 'MESH' else None

    targets: list[tuple[bpy.types.Object, bool]] = []
    if is_collision_helper_object(obj) and obj.type == 'MESH':
        targets.append((obj, True))
        if owner is not None and owner != obj and owner.type == 'MESH':
            targets.append((owner, False))
    else:
        if owner is not None and owner.type == 'MESH':
            targets.append((owner, False))
        if col_obj is not None and col_obj != owner and col_obj.type == 'MESH':
            targets.append((col_obj, True))

    summary_map: dict[tuple[str, int], dict] = {}
    for t_obj, is_col in targets:
        codes = get_mesh_face_surface_array(t_obj)
        if not codes or not any(c > 0 for c in codes):
            continue
        legend = get_object_face_surface_legend(t_obj)
        counts: dict[int, int] = {}
        cent_sums: dict[int, Vector] = {}
        mw_t = t_obj.matrix_world
        polys_t = t_obj.data.polygons if t_obj.data else []
        for fi, c in enumerate(codes):
            if c > 0:
                counts[c] = counts.get(c, 0) + 1
                if fi < len(polys_t):
                    cw = mw_t @ polys_t[fi].center
                    cent_sums[c] = cent_sums.get(c, Vector((0.0, 0.0, 0.0))) + cw
        for code_val, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            meta = legend.get(code_val, {}) or legend.get(str(code_val), {})
            surf_id = str(meta.get("surface") or f"material_{code_val:04d}")
            pws = bool(meta.get("prevent_wall_snap", False))
            lbl = str(meta.get("label") or get_surface_label(surf_id if not pws else code_val))
            col_rgba = get_face_surface_overlay_color(surf_id, code_val)
            c_world_vec = (cent_sums[code_val] / float(cnt)) if code_val in cent_sums else mw_t.to_translation()
            key = (t_obj.name, code_val)
            summary_map[key] = {
                "code": int(code_val),
                "packed": int(code_val),
                "surface": surf_id,
                "surface_key": surf_id,
                "label": lbl,
                "prevent_wall_snap": pws,
                "face_count": int(cnt),
                "color": col_rgba,
                "centroid_world": (float(c_world_vec.x), float(c_world_vec.y), float(c_world_vec.z)),
                "source_object": t_obj.name,
                "target_obj_name": t_obj.name,
                "is_col_mesh": bool(is_col),
            }
    result = list(summary_map.values())
    if len(_FACE_SURFACE_SUMMARY_CACHE) > 32:
        _FACE_SURFACE_SUMMARY_CACHE.clear()
    _FACE_SURFACE_SUMMARY_CACHE[obj.name] = (sig, result)
    return list(result)


def get_havok_ceiling_status(context, obj: bpy.types.Object | None = None) -> dict:
    """
    Checks whether a prop in SAME_AS_MESH ('Same as visible mesh') mode exceeds Havok's
    single-shape 1:1 limits (welded_verts > 2600 or tris > max_col_tris, default 5000).

    Returns a dict with:
      - exceeds_ceiling: bool
      - welded_verts: int
      - raw_verts: int
      - tris: int
      - max_verts: int (2600)
      - max_tris: int (5000 or per-prop max_col_tris)
      - is_same_as_mesh: bool
      - reason: str ('none', 'exceeds_verts', 'exceeds_tris', 'exceeds_both')
    """
    default_res = {
        "exceeds_ceiling": False,
        "welded_verts": 0,
        "raw_verts": 0,
        "tris": 0,
        "max_verts": 2600,
        "max_tris": 5000,
        "is_same_as_mesh": False,
        "reason": "none",
    }

    if obj is None:
        target_obj = get_target_mesh_object(context)
    else:
        target_obj = obj

    if target_obj is None or getattr(target_obj, "type", None) != 'MESH' or not getattr(target_obj, "data", None):
        return default_res

    me = target_obj.data

    # Fast count extraction (< 0.001ms)
    is_editmode = target_obj.mode == 'EDIT' and getattr(me, "is_editmode", False)
    if is_editmode:
        try:
            bm = bmesh.from_edit_mesh(me)
            raw_verts = len(bm.verts)
            poly_count = len(bm.faces)
        except Exception:
            raw_verts = len(me.vertices)
            poly_count = len(me.polygons)
    else:
        raw_verts = len(me.vertices)
        poly_count = len(me.polygons)

    if raw_verts == 0 or poly_count == 0:
        return default_res

    # Fast collision mode & max triangle lookup (< 0.001ms)
    prop = getattr(getattr(context, "scene", None), "reskate_prop", None) if context else None
    active_obj = getattr(getattr(getattr(context, "view_layer", None), "objects", None), "active", None) if context else None

    if active_obj == target_obj and prop is not None:
        col_mode = str(getattr(prop, "collision_mode", "SAME_AS_MESH") or "SAME_AS_MESH").upper()
        max_col_tris = int(getattr(prop, "max_col_tris", 5000) or 5000)
    else:
        col_mode = target_obj.get("reskate_collision_mode")
        if col_mode is None:
            raw_st = target_obj.get("reskate_prop_state", "")
            if raw_st and '"collision_mode":' in raw_st:
                try:
                    parsed = json.loads(str(raw_st))
                    col_mode = str(parsed.get("collision_mode", "SAME_AS_MESH")).upper()
                except Exception:
                    col_mode = "SAME_AS_MESH"
            else:
                col_mode = "SAME_AS_MESH"
        else:
            col_mode = str(col_mode).upper()
        max_col_tris = int(target_obj.get("reskate_max_col_tris", 5000) or 5000)

    max_verts = 2600
    max_tris = max_col_tris

    # FAST TOP-LEVEL CACHE LOOKUP (< 0.002ms)
    cache_key = (target_obj.name, raw_verts, poly_count, col_mode, max_verts, max_tris)
    if cache_key in _HAVOK_CEILING_CACHE:
        return _HAVOK_CEILING_CACHE[cache_key]

    # --- SLOW PATH (Cache Miss Only) ---
    actual_target = target_obj
    if is_collision_helper_object(actual_target):
        owner = resolve_collision_to_owner_prop(actual_target)
        if owner is not None and getattr(owner, "type", None) == 'MESH' and getattr(owner, "data", None):
            actual_target = owner

    # Check for resolved custom collision mesh (_COL or explicit pointer)
    col_obj = resolve_custom_collision_object(context, actual_target)

    # Check collision mesh source in state
    obj_state = get_or_init_object_prop_state(actual_target)
    col_mesh_source = str(obj_state.get("collision_mesh_source", "") or target_obj.get("reskate_collision_mesh_source", "") or "")

    # Determine if collision is truly 1:1 same as visible mesh
    is_same_as_mesh = True
    if col_obj is not None:
        is_same_as_mesh = False
    elif col_mesh_source in ('CUSTOM', 'PRIMITIVE_BOX', 'BOX', 'HULL'):
        is_same_as_mesh = False
    elif col_mode not in ('SAME_AS_MESH', 'EXACT_MESH', 'EXACT_1_1'):
        is_same_as_mesh = False

    # Triangle count and welded vertices (skip np.unique when raw_verts <= max_verts)
    if is_editmode:
        try:
            bm = bmesh.from_edit_mesh(me)
            tris = sum(max(1, len(f.verts) - 2) for f in bm.faces)
            if raw_verts <= max_verts:
                welded_verts = raw_verts
            else:
                v_arr = np.asarray([v.co for v in bm.verts], dtype=np.float32)
                rounded = np.round(v_arr, 4)
                welded_verts = int(len(np.unique(rounded, axis=0)))
        except Exception:
            tris = max(0, len(me.loops) - 2 * poly_count)
            welded_verts = raw_verts
    else:
        tris = max(0, len(me.loops) - 2 * poly_count)
        if raw_verts <= max_verts:
            welded_verts = raw_verts
        else:
            v_buf = np.empty(raw_verts * 3, dtype=np.float32)
            me.vertices.foreach_get("co", v_buf)
            rounded = np.round(v_buf.reshape(-1, 3), 4)
            welded_verts = int(len(np.unique(rounded, axis=0)))

    if not is_same_as_mesh:
        exceeds_ceiling = False
        reason = "none"
    else:
        v_exceeded = welded_verts > max_verts
        t_exceeded = tris > max_tris
        exceeds_ceiling = bool(v_exceeded or t_exceeded)
        if v_exceeded and t_exceeded:
            reason = "exceeds_both"
        elif v_exceeded:
            reason = "exceeds_verts"
        elif t_exceeded:
            reason = "exceeds_tris"
        else:
            reason = "none"

    result = {
        "exceeds_ceiling": exceeds_ceiling,
        "welded_verts": welded_verts,
        "raw_verts": raw_verts,
        "tris": tris,
        "max_verts": max_verts,
        "max_tris": max_tris,
        "is_same_as_mesh": is_same_as_mesh,
        "reason": reason,
    }

    if len(_HAVOK_CEILING_CACHE) > 64:
        _HAVOK_CEILING_CACHE.clear()
    _HAVOK_CEILING_CACHE[cache_key] = result
    return result


def extract_face_surface_overrides(
    prop_obj: bpy.types.Object | None,
    scale_mult=1.0,
    col_obj: bpy.types.Object | None = None,
) -> list[dict]:
    """
    Extracts painted per-face surface audio overrides from `col_obj` (if painted) and/or `prop_obj`
    into 3D triangle groups (`triangles`, `triangles_zup`, `triangles_yup`, `centroids_yup`, `normals_yup`, `code`, `packed`) so
    `compile_blender_prop.py` can partition Havok collision triangles into dedicated sub-shapes
    with exact `ShapeToFaceMaterialMap` surface codes across Exact Mesh, Simple Hulls, and Custom Collision modes!
    """
    if prop_obj is None or getattr(prop_obj, "type", None) != 'MESH':
        return []
    if not isinstance(scale_mult, (int, float)):
        scale_mult = 1.0
    scale_mult = float(scale_mult or 1.0)
    owner = resolve_collision_to_owner_prop(prop_obj) or prop_obj
    if col_obj is None:
        col_obj = resolve_custom_collision_object(bpy.context, owner)

    # If the collision mesh itself has painted face surfaces, extract from `col_obj`;
    # otherwise extract from `owner` (which auto-projects onto simplified hulls during compile!).
    sources: list[tuple[bpy.types.Object, str]] = []
    if col_obj is not None and col_obj.type == 'MESH' and any(c > 0 for c in get_mesh_face_surface_array(col_obj)):
        sources.append((col_obj, "COL"))
    if owner is not None and owner.type == 'MESH' and any(c > 0 for c in get_mesh_face_surface_array(owner)):
        sources.append((owner, "MAIN"))

    if not sources:
        return []

    origin_world = owner.matrix_world.translation.copy()
    groups_out: list[dict] = []

    for src_obj, src_tag in sources:
        # Ensure edit-mode bmesh changes are flushed to mesh polygons
        if getattr(src_obj, "mode", "OBJECT") == 'EDIT':
            try:
                src_obj.update_from_editmode()
            except Exception:
                pass
        codes = get_mesh_face_surface_array(src_obj)
        if not codes or not any(c > 0 for c in codes):
            continue
        legend = get_object_face_surface_legend(src_obj)
        me = src_obj.data
        me.calc_loop_triangles()
        mw = src_obj.matrix_world

        # Group loop_triangles by polygon's assigned `packed` surface code
        by_code: dict[int, dict] = {}
        for lt in me.loop_triangles:
            p_idx = lt.polygon_index
            if p_idx < 0 or p_idx >= len(codes):
                continue
            c_val = int(codes[p_idx])
            if c_val <= 0:
                continue
            g = by_code.get(c_val)
            if g is None:
                meta = legend.get(c_val, {})
                surf_id = str(meta.get("surface") or f"material_{c_val:04d}")
                pws = bool(meta.get("prevent_wall_snap", False))
                lbl = str(meta.get("label") or get_surface_label(surf_id if not pws else c_val))
                g = {
                    "code": c_val,
                    "packed": c_val,
                    "surface": surf_id,
                    "surface_key": surf_id,
                    "label": lbl,
                    "prevent_wall_snap": pws,
                    "source_mesh": src_tag,
                    "face_indices_set": set(),
                    "triangles": [],
                    "triangles_zup": [],
                    "triangles_yup": [],
                    "centroids_yup": [],
                    "normals_yup": [],
                }
                by_code[c_val] = g

            g["face_indices_set"].add(int(p_idx))
            v0_w = (mw @ me.vertices[lt.vertices[0]].co - origin_world) * scale_mult
            v1_w = (mw @ me.vertices[lt.vertices[1]].co - origin_world) * scale_mult
            v2_w = (mw @ me.vertices[lt.vertices[2]].co - origin_world) * scale_mult

            # Blender Z-up: (x, y, z) -> GLB Y-up: (x, z, -y)
            t_zup = [
                [round(float(v0_w.x), 5), round(float(v0_w.y), 5), round(float(v0_w.z), 5)],
                [round(float(v1_w.x), 5), round(float(v1_w.y), 5), round(float(v1_w.z), 5)],
                [round(float(v2_w.x), 5), round(float(v2_w.y), 5), round(float(v2_w.z), 5)],
            ]
            p0_y = Vector((float(v0_w.x), float(v0_w.z), float(-v0_w.y)))
            p1_y = Vector((float(v1_w.x), float(v1_w.z), float(-v1_w.y)))
            p2_y = Vector((float(v2_w.x), float(v2_w.z), float(-v2_w.y)))
            cent_y = (p0_y + p1_y + p2_y) / 3.0
            n_y = (p1_y - p0_y).cross(p2_y - p0_y)
            if n_y.length_squared > 1e-12:
                n_y.normalize()
            else:
                n_y = Vector((0.0, 1.0, 0.0))

            g["triangles"].append(t_zup)
            g["triangles_zup"].append(t_zup)
            g["triangles_yup"].append([
                [round(float(p0_y.x), 5), round(float(p0_y.y), 5), round(float(p0_y.z), 5)],
                [round(float(p1_y.x), 5), round(float(p1_y.y), 5), round(float(p1_y.z), 5)],
                [round(float(p2_y.x), 5), round(float(p2_y.y), 5), round(float(p2_y.z), 5)],
            ])
            g["centroids_yup"].append([round(float(cent_y.x), 5), round(float(cent_y.y), 5), round(float(cent_y.z), 5)])
            g["normals_yup"].append([round(float(n_y.x), 4), round(float(n_y.y), 4), round(float(n_y.z), 4)])

        for c_val, g in sorted(by_code.items(), key=lambda kv: kv[0]):
            f_list = sorted(g.pop("face_indices_set"))
            g["face_indices"] = f_list
            g["face_count"] = len(f_list)
            groups_out.append(g)

    return groups_out


# ---------------------------------------------------------------------------
# Native Skate Map Material (`mat.sk8_material`) & Principled BSDF PBR Readers
# ---------------------------------------------------------------------------

def _principled_node(mat: bpy.types.Material | None):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    nodes = mat.node_tree.nodes
    # Trace upstream from the active Material Output ('OUTPUT_MATERIAL') first so that when
    # tools like Sanctus Bake instantiate a new Principled BSDF alongside an older disconnected
    # node tree, we always read the live rendered Principled BSDF!
    out_nodes = [n for n in nodes if n.type == "OUTPUT_MATERIAL"]
    out_nodes.sort(key=lambda n: (not bool(getattr(n, "is_active_output", False))))
    for out_n in out_nodes:
        surf = out_n.inputs.get("Surface")
        if surf is None or not surf.is_linked:
            continue
        queue = [surf]
        seen = set()
        while queue:
            cur_s = queue.pop(0)
            if cur_s is None or not cur_s.is_linked or id(cur_s) in seen:
                continue
            seen.add(id(cur_s))
            for lk in cur_s.links:
                fn = lk.from_node
                if fn is None:
                    continue
                if fn.type == "BSDF_PRINCIPLED":
                    return fn
                if fn.type == "GROUP" and getattr(fn, "node_tree", None) is not None:
                    g_p = next((gn for gn in fn.node_tree.nodes if gn.type == "BSDF_PRINCIPLED"), None)
                    if g_p is not None:
                        return g_p
                for in_s in getattr(fn, "inputs", ()):
                    if in_s.is_linked:
                        queue.append(in_s)
    return next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)


def _is_non_albedo_pbr_image_name(name: str) -> bool:
    """Returns True if an image name is clearly a non-albedo utility map (AO, Roughness, Metallic, Normal, Height)."""
    low = str(name or "").strip().lower()
    if not low:
        return False
    # Strip trailing Blender numeric duplicate suffix like .001, .002
    base_low = re.sub(r"\.\d+$", "", low)
    if any(k in base_low for k in ("basecolor", "base_color", "albedo", "diffuse", "color", "_col", "_bc")):
        if not any(k in base_low for k in ("ambientocclusion", "roughness", "metallic", "metalness", "normal")):
            return False
    return any(
        k in base_low
        for k in (
            "ambientocclusion",
            "_ao",
            "roughness",
            "_rough",
            "metallic",
            "metalness",
            "_metal",
            "normal",
            "_norm",
            "_nor",
            "height",
            "bump",
            "displacement",
            "curvature",
            "_msk",
            "_orm",
        )
    )


def _ordered_node_inputs_for_image_search(node):
    """Returns node inputs ordered so primary color sockets (Color1 / A / Color / Base Color) are searched before AO/mask sockets (Color2 / B / Fac)."""
    inputs = list(getattr(node, "inputs", ()))
    if not inputs:
        return inputs
    if getattr(node, "type", "") in ("MIX_RGB", "MIX"):
        pri = []
        sec = []
        fac = []
        for s in inputs:
            nm = str(getattr(s, "name", "") or "")
            if nm in ("Color1", "A"):
                pri.append(s)
            elif nm in ("Fac", "Factor"):
                fac.append(s)
            else:
                sec.append(s)
        return pri + sec + fac
    return inputs


def _linked_image(socket, maximum_depth: int = 8, exclude_non_albedo: bool = False) -> bpy.types.Image | None:
    if socket is None:
        return None
    pending = [(socket, 0)]
    seen = set()
    fallback_img = None
    while pending:
        current, depth = pending.pop(0)
        if current is None or not current.is_linked or id(current) in seen:
            continue
        seen.add(id(current))
        for link in current.links:
            node = link.from_node
            if node is None:
                continue
            if node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None:
                if not node.name.startswith(("_ReSkate_Temp_Bake", "ReSkate_Wear", "ReSkate_Decal_")):
                    img = node.image
                    if exclude_non_albedo and _is_non_albedo_pbr_image_name(getattr(img, "name", "")):
                        if fallback_img is None:
                            fallback_img = img
                    else:
                        return img
            if node.type == "GROUP" and getattr(node, "node_tree", None) is not None and depth < maximum_depth:
                out_name = getattr(link.from_socket, "name", "")
                for gn in node.node_tree.nodes:
                    if gn.type == "GROUP_OUTPUT":
                        g_sock = gn.inputs.get(out_name) if out_name else (gn.inputs[0] if len(gn.inputs) > 0 else None)
                        if g_sock is not None and g_sock.is_linked:
                            pending.append((g_sock, depth + 1))
            if depth < maximum_depth:
                pending.extend((child, depth + 1) for child in _ordered_node_inputs_for_image_search(node))
    return fallback_img


def _base_color_image(mat: bpy.types.Material | None) -> bpy.types.Image | None:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    out = _principled_node(mat)
    if out is not None:
        img = _linked_image(out.inputs.get("Base Color"), exclude_non_albedo=True)
        if img is not None and not _is_non_albedo_pbr_image_name(getattr(img, "name", "")):
            return img
    # Fallback: scan TEX_IMAGE nodes for an albedo/basecolor image (excluding AO/Roughness/Metallic/Normal)
    for n in mat.node_tree.nodes:
        if (
            n.type == "TEX_IMAGE"
            and getattr(n, "image", None) is not None
            and not n.name.startswith(("_ReSkate_Temp_Bake", "ReSkate_Wear", "ReSkate_Decal_", "ReSkate_User_"))
            and not _is_non_albedo_pbr_image_name(getattr(n.image, "name", ""))
        ):
            return n.image
    if out is not None:
        img_any = _linked_image(out.inputs.get("Base Color"), exclude_non_albedo=False)
        if img_any is not None and not _is_non_albedo_pbr_image_name(getattr(img_any, "name", "")):
            return img_any
    return None


def _normal_image(mat: bpy.types.Material | None) -> bpy.types.Image | None:
    p = _principled_node(mat)
    return _linked_image(p.inputs.get("Normal")) if p else None


def _opacity_image(mat: bpy.types.Material | None) -> bpy.types.Image | None:
    p = _principled_node(mat)
    return _linked_image(p.inputs.get("Alpha")) if p else None


def _first_linked_value_input(node):
    """Finds the primary linked value/color input on a shader node (e.g., inputs[1] 'Color' on INVERT)."""
    if not node or not getattr(node, "inputs", None):
        return None
    for pref_name in ("Color", "Value", "Image", "Vector", "A", "B", "Color1", "Color2"):
        sock = node.inputs.get(pref_name)
        if sock is not None and sock.is_linked:
            return sock
    for sock in node.inputs:
        if sock.is_linked:
            return sock
    return node.inputs[0] if node.inputs else None


def _scalar_texture_source(socket) -> tuple[bpy.types.Image | None, str]:
    """Traces a Principled BSDF Roughness/Metallic socket through Separate Color / Invert / Math to its (Image, Channel)."""
    channel = "Color"
    seen = set()
    cur = socket
    while cur is not None and cur.is_linked and id(cur) not in seen:
        seen.add(id(cur))
        link = cur.links[0]
        node, output = link.from_node, link.from_socket
        node_nm = str(getattr(node, "name", ""))
        if node_nm.startswith("ReSkate_Wear_"):
            # Step into the base input (inputs[0] or inputs['A']) so ReSkate_Wear_MaskTex is never mistaken for a user texture
            cur = node.inputs[0] if len(node.inputs) > 0 else None
            continue
        if node_nm.startswith("ReSkate_Decal_"):
            # Step into the upstream base input (A / Normal) of the viewport decal preview wrapper
            if node.type == "MIX":
                dt = str(getattr(node, "data_type", "FLOAT")).upper()
                cur = (node.inputs[6] if len(node.inputs) > 6 else node.inputs.get("A")) if dt == "RGBA" else (node.inputs[2] if len(node.inputs) > 2 else node.inputs.get("A"))
            elif node.type == "BUMP":
                cur = node.inputs.get("Normal")
            else:
                cur = node.inputs.get("A") or (node.inputs[0] if len(node.inputs) > 0 else None)
            continue
        if node.type in {"REROUTE", "FRAME"}:
            cur = _first_linked_value_input(node)
        elif node.type == "GROUP" and getattr(node, "node_tree", None) is not None:
            g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT" and getattr(n, "is_active_output", True)), None)
            if g_out is None:
                g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT"), None)
            if g_out is not None:
                in_sock = g_out.inputs.get(output.name) if output is not None else None
                if in_sock is None or not in_sock.is_linked:
                    in_sock = next((s for s in g_out.inputs if s.is_linked), None)
                cur = in_sock
            else:
                cur = _first_linked_value_input(node)
        elif node.type in {"SEPRGB", "SEPXYZ", "SEPARATE_COLOR"}:
            if output.name in {"R", "X", "Red"}:
                channel = "Red"
            elif output.name in {"G", "Y", "Green"}:
                channel = "Green"
            elif output.name in {"B", "Z", "Blue"}:
                channel = "Blue"
            cur = _first_linked_value_input(node)
        elif node.type == "RGBTOBW":
            channel = "Color"
            cur = _first_linked_value_input(node)
        elif node.type in {"VALTORGB", "CURVE_RGB", "CURVE_FLOAT", "MAP_RANGE", "CLAMP", "MATH", "GAMMA", "INVERT", "MIX", "MIX_RGB"}:
            # Continue walking upstream via the linked Color/Value socket (e.g. inputs[1] on INVERT)
            cur = _first_linked_value_input(node)
            if cur is None or not cur.is_linked:
                img = _linked_image(link.from_socket)
                return img, channel
        elif node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None:
            if node.name.startswith(("_ReSkate_Temp_Bake", "ReSkate_Wear", "ReSkate_Decal_")) or node.image.name.startswith(("ReSkate_WearMask_", "_ReSkate_Temp_Bake")):
                return None, channel
            out_ch = "Alpha" if output.name == "Alpha" else (channel if channel != "Color" else "Color")
            return node.image, out_ch
        else:
            img = _linked_image(cur)
            return img, channel
    return None, channel


def _socket_scalar_default(sock, fallback: float = 0.0) -> float:
    if sock is None or not hasattr(sock, "default_value"):
        return fallback
    dv = sock.default_value
    if hasattr(dv, "__len__"):
        if len(dv) >= 3:
            return float(0.299 * dv[0] + 0.587 * dv[1] + 0.114 * dv[2])
        if len(dv) >= 1:
            return float(dv[0])
    try:
        return float(dv)
    except Exception:
        return fallback


def _get_upstream_mapping_scale(node) -> tuple[float, float, float, float]:
    """Traces `node.inputs['Vector']` upstream for a `ShaderNodeMapping` and returns `(sx, sy, ox, oy)`."""
    if not node or not getattr(node, "inputs", None):
        return 1.0, 1.0, 0.0, 0.0
    vec_sock = node.inputs.get("Vector")
    seen = set()
    cur = vec_sock
    while cur is not None and cur.is_linked and id(cur) not in seen:
        seen.add(id(cur))
        n = cur.links[0].from_node
        if n.type == "MAPPING":
            sc_sock = n.inputs.get("Scale")
            loc_sock = n.inputs.get("Location")
            sx, sy = 1.0, 1.0
            ox, oy = 0.0, 0.0
            if sc_sock is not None and hasattr(sc_sock, "default_value"):
                dv = sc_sock.default_value
                sx = float(dv[0]) if len(dv) > 0 else 1.0
                sy = float(dv[1]) if len(dv) > 1 else sx
            if loc_sock is not None and hasattr(loc_sock, "default_value"):
                lv = loc_sock.default_value
                ox = float(lv[0]) if len(lv) > 0 else 0.0
                oy = float(lv[1]) if len(lv) > 1 else 0.0
            return sx, sy, ox, oy
        cur = n.inputs.get("Vector") if hasattr(n, "inputs") else None
    return 1.0, 1.0, 0.0, 0.0


def _tile_2d_array_by_mapping(arr: np.ndarray, sx: float, sy: float, ox: float = 0.0, oy: float = 0.0) -> np.ndarray:
    """Applies UV Mapping scale `(sx, sy)` and offset `(ox, oy)` to a 2D `(H, W)` or 3D `(H, W, C)` array via wrap-around indexing."""
    if abs(sx - 1.0) < 1e-4 and abs(sy - 1.0) < 1e-4 and abs(ox) < 1e-4 and abs(oy) < 1e-4:
        return arr
    h, w = arr.shape[:2]
    u = np.mod(np.linspace(0.0, 1.0, w, endpoint=False, dtype=np.float32) * sx + ox, 1.0)
    v = np.mod(np.linspace(0.0, 1.0, h, endpoint=False, dtype=np.float32) * sy + oy, 1.0)
    ix = np.clip((u * w).astype(np.int32), 0, w - 1)
    iy = np.clip((v * h).astype(np.int32), 0, h - 1)
    return arr[np.ix_(iy, ix)]


_SYNTH_PROC_CACHE: dict[tuple, np.ndarray] = {}
_SOCKET_SCALAR_CACHE: dict[tuple, tuple[np.ndarray | None, bpy.types.Image | None, str]] = {}
_SOCKET_COLOR_CACHE: dict[tuple, tuple[np.ndarray | None, bpy.types.Image | None, bool]] = {}
_SOCKET_NORMAL_CACHE: dict[tuple, tuple[np.ndarray | None, bpy.types.Image | None, bool]] = {}
_CHANNEL_MAP_CACHE: dict[tuple, tuple[np.ndarray, str]] = {}


def clear_ephemeral_shader_caches() -> None:
    """Clears ephemeral shader socket and procedural synthesis caches."""
    _SYNTH_PROC_CACHE.clear()
    _SOCKET_SCALAR_CACHE.clear()
    _SOCKET_COLOR_CACHE.clear()
    _SOCKET_NORMAL_CACHE.clear()
    _CHANNEL_MAP_CACHE.clear()


def _synthesize_procedural_node_2d(
    node,
    output_name: str = "Fac",
    size: tuple[int, int] = (1024, 1024),
) -> np.ndarray | None:
    """
    Synthesizes a deterministic 2D float32 array for procedural Blender shader nodes
    (`TEX_NOISE`, `TEX_VORONOI`, `TEX_WAVE`, `TEX_GRADIENT`, `TEX_CHECKER`) honoring
    their `Scale`, `Detail`, `Roughness`, and upstream `Mapping` UV scale without touching UVs.
    Memoized deterministically in _SYNTH_PROC_CACHE.
    """
    if node is None or not is_experimental_addon_enabled():
        return None

    w, h = int(size[0]), int(size[1])
    cache_key = (node.as_pointer(), str(output_name), w, h)
    if cache_key in _SYNTH_PROC_CACHE:
        return _SYNTH_PROC_CACHE[cache_key].copy()

    res = _synthesize_procedural_node_2d_uncached(node, output_name=output_name, size=size)
    if res is not None:
        if len(_SYNTH_PROC_CACHE) > 256:
            _SYNTH_PROC_CACHE.clear()
        _SYNTH_PROC_CACHE[cache_key] = res.copy()
    return res


def _synthesize_procedural_node_2d_uncached(
    node,
    output_name: str = "Fac",
    size: tuple[int, int] = (1024, 1024),
) -> np.ndarray | None:
    if node is None:
        return None

    w, h = int(size[0]), int(size[1])
    sx, sy, ox, oy = _get_upstream_mapping_scale(node)
    sx = max(0.05, abs(float(sx or 1.0)))
    sy = max(0.05, abs(float(sy or 1.0)))

    ntype = str(getattr(node, "type", "")).upper()
    seed_val = int(zlib.crc32(str(getattr(node, "name", "noise")).encode("utf-8")) & 0x7FFFFFFF)

    if ntype == "TEX_NOISE":
        base_scale = _socket_scalar_default(node.inputs.get("Scale"), 5.0)
        detail = max(0.0, min(12.0, _socket_scalar_default(node.inputs.get("Detail"), 2.0)))
        rough = max(0.0, min(1.0, _socket_scalar_default(node.inputs.get("Roughness"), 0.5)))
        eff_sx = max(2.0, min(float(w // 2), abs(base_scale) * sx))
        eff_sy = max(2.0, min(float(h // 2), abs(base_scale) * sy))
        octaves = max(1, min(6, int(round(detail)) + 1))

        def _build_fbn_channel(ch_seed: int) -> np.ndarray:
            rng = np.random.default_rng(ch_seed)
            acc = np.zeros((h, w), dtype=np.float32)
            amp = 1.0
            amp_sum = 0.0
            freq_x, freq_y = eff_sx, eff_sy
            for _ in range(octaves):
                gw = max(4, min(w, int(round(freq_x))))
                gh = max(4, min(h, int(round(freq_y))))
                grid = rng.uniform(0.0, 1.0, size=(gh, gw)).astype(np.float32)
                im_g = Image.fromarray(np.clip(grid * 255.0, 0, 255).astype(np.uint8), mode="L")
                up = np.asarray(im_g.resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32) / 255.0
                acc += up * amp
                amp_sum += amp
                amp *= max(0.15, rough)
                freq_x = min(float(w), freq_x * 2.0)
                freq_y = min(float(h), freq_y * 2.0)
            norm_f = acc / max(amp_sum, 1e-6)
            # Match Blender Noise Fac distribution (centered at 0.5 with ~0.18 std)
            norm_f = np.clip(0.5 + (norm_f - float(norm_f.mean())) * 1.45, 0.0, 1.0)
            return norm_f.astype(np.float32)

        if output_name == "Color":
            c0 = _build_fbn_channel(seed_val)
            c1 = _build_fbn_channel(seed_val + 101)
            c2 = _build_fbn_channel(seed_val + 202)
            return np.dstack([c0, c1, c2]).astype(np.float32)
        return _build_fbn_channel(seed_val)

    if ntype == "TEX_VORONOI":
        base_scale = max(1.0, abs(_socket_scalar_default(node.inputs.get("Scale"), 5.0)))
        ncells = max(4, min(64, int(round(base_scale * 0.5 * (sx + sy)))))
        rng = np.random.default_rng(seed_val)
        pts_x = rng.uniform(0.0, 1.0, size=(ncells,)).astype(np.float32)
        pts_y = rng.uniform(0.0, 1.0, size=(ncells,)).astype(np.float32)
        gh, gw = min(256, h), min(256, w)
        yy, xx = np.meshgrid(
            np.linspace(0.0, 1.0, gh, endpoint=False, dtype=np.float32),
            np.linspace(0.0, 1.0, gw, endpoint=False, dtype=np.float32),
            indexing="ij",
        )
        dx = np.abs(xx[:, :, None] - pts_x[None, None, :])
        dy = np.abs(yy[:, :, None] - pts_y[None, None, :])
        dx = np.minimum(dx, 1.0 - dx)
        dy = np.minimum(dy, 1.0 - dy)
        dist = np.sqrt(np.min(dx * dx + dy * dy, axis=2)) * float(np.sqrt(ncells))
        dist = np.clip(dist, 0.0, 1.0).astype(np.float32)
        if (gw, gh) != (w, h):
            im_v = Image.fromarray(np.clip(dist * 255.0, 0, 255).astype(np.uint8), mode="L")
            dist = np.asarray(im_v.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        if output_name == "Color":
            return np.dstack([dist, dist, dist]).astype(np.float32)
        return dist

    if ntype == "TEX_WAVE":
        base_scale = _socket_scalar_default(node.inputs.get("Scale"), 5.0)
        yy, xx = np.meshgrid(
            np.linspace(0.0, 1.0, h, endpoint=False, dtype=np.float32) * sy + oy,
            np.linspace(0.0, 1.0, w, endpoint=False, dtype=np.float32) * sx + ox,
            indexing="ij",
        )
        wave = 0.5 + 0.5 * np.sin((xx + yy * 0.25) * base_scale * 2.0 * np.pi)
        wave = wave.astype(np.float32)
        if output_name == "Color":
            return np.dstack([wave, wave, wave]).astype(np.float32)
        return wave

    if ntype == "TEX_CHECKER":
        base_scale = max(1.0, _socket_scalar_default(node.inputs.get("Scale"), 5.0))
        yy, xx = np.meshgrid(
            np.linspace(0.0, 1.0, h, endpoint=False, dtype=np.float32) * sy * base_scale,
            np.linspace(0.0, 1.0, w, endpoint=False, dtype=np.float32) * sx * base_scale,
            indexing="ij",
        )
        chk = ((np.floor(xx).astype(np.int32) + np.floor(yy).astype(np.int32)) & 1).astype(np.float32)
        if output_name == "Color":
            return np.dstack([chk, chk, chk]).astype(np.float32)
        return chk

    if ntype == "TEX_GRADIENT":
        yy, xx = np.meshgrid(
            np.linspace(0.0, 1.0, h, endpoint=False, dtype=np.float32),
            np.linspace(0.0, 1.0, w, endpoint=False, dtype=np.float32),
            indexing="ij",
        )
        grad = np.clip(xx, 0.0, 1.0).astype(np.float32)
        if output_name == "Color":
            return np.dstack([grad, grad, grad]).astype(np.float32)
        return grad

    return None


def _cache_and_return_scalar(cache_key: tuple, res: tuple) -> tuple:
    if res[0] is not None:
        if len(_SOCKET_SCALAR_CACHE) > 256:
            _SOCKET_SCALAR_CACHE.clear()
        _SOCKET_SCALAR_CACHE[cache_key] = (res[0].copy(), res[1], res[2])
    return res


def _evaluate_scalar_socket_2d(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    forced_channel: str | None = None,
    _depth: int = 0,
    target_size: tuple[int, int] | None = None,
) -> tuple[np.ndarray | None, bpy.types.Image | None, str]:
    """
    Evaluates the full Blender shader node chain connected to `socket` on a 2D float32 grid.
    Synchronous backward-compatible wrapper around _evaluate_scalar_socket_2d_generator.
    """
    gen = _evaluate_scalar_socket_2d_generator(
        socket,
        staging_dir=staging_dir,
        size=size,
        forced_channel=forced_channel,
        _depth=_depth,
        target_size=target_size,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _evaluate_scalar_socket_2d_generator(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    forced_channel: str | None = None,
    _depth: int = 0,
    target_size: tuple[int, int] | None = None,
    pct_val: float = 0.0,
    label_prefix: str = "",
):
    """
    Evaluates the full Blender shader node chain connected to `socket`
    into a 2D float32 array in [0.0, 1.0] while yielding ProgressStatus
    to keep main-thread execution strictly under 350ms.
    Memoizes in _SOCKET_SCALAR_CACHE.
    Returns (evaluated_2d_array_or_None, source_image_or_None, channel_name).
    """
    if target_size is not None:
        size = target_size
    if socket is None or not socket.is_linked or not socket.links or _depth > 14:
        return None, None, forced_channel or "Color"

    cache_key = (socket.as_pointer(), int(size[0]), int(size[1]), str(forced_channel))
    if cache_key in _SOCKET_SCALAR_CACHE:
        arr, im, ch = _SOCKET_SCALAR_CACHE[cache_key]
        return (arr.copy() if arr is not None else None), im, ch

    link = socket.links[0]
    node, output = link.from_node, link.from_socket

    # Bypass muted nodes or ReSkate_Wear_* / ReSkate_Decal_* viewport wrapper nodes transparently
    node_nm = str(getattr(node, "name", ""))
    if getattr(node, "mute", False) or node_nm.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
        if node_nm.startswith("ReSkate_Decal_"):
            if node.type == "MIX":
                dt = str(getattr(node, "data_type", "FLOAT")).upper()
                in0 = (node.inputs[6] if len(node.inputs) > 6 else node.inputs.get("A")) if dt == "RGBA" else (node.inputs[2] if len(node.inputs) > 2 else node.inputs.get("A"))
            else:
                in0 = node.inputs.get("A") or (node.inputs[0] if len(node.inputs) > 0 else None)
        elif node_nm.startswith("ReSkate_Wear_") and len(node.inputs) > 0:
            in0 = node.inputs[0]
        else:
            in0 = _first_linked_value_input(node)
        res = yield from _evaluate_scalar_socket_2d_generator(
            in0,
            staging_dir=staging_dir,
            size=size,
            forced_channel=forced_channel,
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=label_prefix,
        )
        return _cache_and_return_scalar(cache_key, res)

    def _eval_in(in_sock, fallback: float = 0.0, ch_override: str | None = None):
        if in_sock is not None and in_sock.is_linked:
            arr, im, ch = yield from _evaluate_scalar_socket_2d_generator(
                in_sock,
                staging_dir=staging_dir,
                size=size,
                forced_channel=ch_override,
                _depth=_depth + 1,
                pct_val=pct_val,
                label_prefix=label_prefix,
            )
            if arr is not None:
                return arr, im, ch
        return _socket_scalar_default(in_sock, fallback), None, ch_override or "Color"

    if node.type in {"REROUTE", "FRAME"}:
        in0 = _first_linked_value_input(node)
        res = yield from _evaluate_scalar_socket_2d_generator(
            in0,
            staging_dir=staging_dir,
            size=size,
            forced_channel=forced_channel,
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=label_prefix,
        )
        return _cache_and_return_scalar(cache_key, res)

    if node.type == "GROUP" and getattr(node, "node_tree", None) is not None:
        g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT" and getattr(n, "is_active_output", True)), None)
        if g_out is None:
            g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT"), None)
        if g_out is not None:
            in_sock = g_out.inputs.get(output.name) if output is not None else None
            if in_sock is None or not in_sock.is_linked:
                in_sock = next((s for s in g_out.inputs if s.is_linked), None)
            if in_sock is not None:
                res = yield from _evaluate_scalar_socket_2d_generator(
                    in_sock,
                    staging_dir=staging_dir,
                    size=size,
                    forced_channel=forced_channel,
                    _depth=_depth + 1,
                    pct_val=pct_val,
                    label_prefix=label_prefix,
                )
                return _cache_and_return_scalar(cache_key, res)

    if node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None:
        pil_im = yield from _blender_image_to_pil_generator(
            node.image,
            staging_dir=staging_dir,
            is_color=False,
            label_prefix=f"{label_prefix}Image: ",
            pct_val=pct_val,
        )
        if pil_im is None:
            return None, None, forced_channel or "Color"
        out_ch = forced_channel if forced_channel else ("Alpha" if output.name == "Alpha" else "Color")
        arr, used_ch = yield from _extract_channel_map_generator(
            pil_im,
            out_ch,
            size,
            image_name=getattr(node.image, "name", ""),
            return_channel_used=True,
            label_prefix=label_prefix,
            pct_val=pct_val,
        )
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Channel '{used_ch}' extracted for '{node.image.name}'", round(pct_val, 2))
        return _cache_and_return_scalar(cache_key, (arr, node.image, used_ch))

    if node.type in {"TEX_NOISE", "TEX_VORONOI", "TEX_WAVE", "TEX_CHECKER", "TEX_GRADIENT"}:
        if not is_experimental_addon_enabled():
            return None, None, forced_channel or "Color"
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Synthesizing procedural {node.type} ({node.name})...", round(pct_val, 2))
        proc_arr = _synthesize_procedural_node_2d(node, output_name="Fac", size=size)
        if proc_arr is not None:
            return _cache_and_return_scalar(cache_key, (proc_arr, None, f"Procedural ({node.type})"))

    if node.type in {"SEPRGB", "SEPXYZ", "SEPARATE_COLOR"}:
        sep_ch = "Color"
        if output.name in {"R", "X", "Red"}:
            sep_ch = "Red"
        elif output.name in {"G", "Y", "Green"}:
            sep_ch = "Green"
        elif output.name in {"B", "Z", "Blue"}:
            sep_ch = "Blue"
        in0 = _first_linked_value_input(node)
        res = yield from _evaluate_scalar_socket_2d_generator(
            in0,
            staging_dir=staging_dir,
            size=size,
            forced_channel=sep_ch,
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=label_prefix,
        )
        return _cache_and_return_scalar(cache_key, res)

    if node.type == "RGBTOBW":
        in0 = _first_linked_value_input(node)
        res = yield from _evaluate_scalar_socket_2d_generator(
            in0,
            staging_dir=staging_dir,
            size=size,
            forced_channel="Color",
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=label_prefix,
        )
        return _cache_and_return_scalar(cache_key, res)

    if node.type == "INVERT":
        fac_sock = node.inputs[0] if len(node.inputs) > 0 else None
        col_sock = node.inputs[1] if len(node.inputs) > 1 else _first_linked_value_input(node)
        fac, im_f, ch_f = yield from _eval_in(fac_sock, 1.0, forced_channel)
        col, im_c, ch_c = yield from _eval_in(col_sock, 0.5, forced_channel)
        if not isinstance(fac, np.ndarray) and not isinstance(col, np.ndarray):
            return None, None, "Color"
        res = (1.0 - fac) * col + fac * (1.0 - col)
        return _cache_and_return_scalar(cache_key, (np.asarray(res, dtype=np.float32), (im_c or im_f), (ch_c if im_c else ch_f)))

    if node.type == "MATH":
        op = str(getattr(node, "operation", "ADD")).upper()
        s0 = node.inputs[0] if len(node.inputs) > 0 else None
        s1 = node.inputs[1] if len(node.inputs) > 1 else None
        s2 = node.inputs[2] if len(node.inputs) > 2 else None
        a, im_a, ch_a = yield from _eval_in(s0, 0.5, forced_channel)
        b, im_b, ch_b = yield from _eval_in(s1, 0.5, forced_channel)
        c, im_c, ch_c = yield from _eval_in(s2, 0.5, forced_channel)
        if not isinstance(a, np.ndarray) and not isinstance(b, np.ndarray) and not isinstance(c, np.ndarray):
            return None, None, "Color"
        if op == "ADD":
            res = a + b
        elif op == "SUBTRACT":
            res = a - b
        elif op == "MULTIPLY":
            res = a * b
        elif op == "DIVIDE":
            res = np.where(np.abs(b) > 1e-6, a / b, 0.0)
        elif op == "MULTIPLY_ADD":
            res = a * b + c
        elif op == "POWER":
            res = np.power(np.maximum(a, 0.0), b)
        elif op == "SQRT":
            res = np.sqrt(np.maximum(a, 0.0))
        elif op == "INVERSE_SQRT":
            res = 1.0 / np.sqrt(np.maximum(a, 1e-6))
        elif op == "ABSOLUTE":
            res = np.abs(a)
        elif op == "MINIMUM":
            res = np.minimum(a, b)
        elif op == "MAXIMUM":
            res = np.maximum(a, b)
        elif op == "LESS_THAN":
            res = (a < b).astype(np.float32)
        elif op == "GREATER_THAN":
            res = (a > b).astype(np.float32)
        elif op == "ROUND":
            res = np.round(a)
        elif op == "FLOOR":
            res = np.floor(a)
        elif op == "CEIL":
            res = np.ceil(a)
        elif op == "FRACT":
            res = a - np.floor(a)
        else:
            res = a
        res = np.asarray(res, dtype=np.float32)
        if getattr(node, "use_clamp", False):
            res = np.clip(res, 0.0, 1.0)
        src_im = im_a or im_b or im_c
        src_ch = ch_a if im_a else (ch_b if im_b else ch_c)
        return _cache_and_return_scalar(cache_key, (res, src_im, src_ch))

    if node.type == "MAP_RANGE":
        # If this is our ReSkate_Roughness_Mix / ReSkate_Metallic_Mix wrapper node, evaluate its upstream Value input
        v_sock = node.inputs.get("Value") or (node.inputs[0] if len(node.inputs) > 0 else None)
        if node.name in ("ReSkate_Roughness_Mix", "ReSkate_Metallic_Mix"):
            res = yield from _evaluate_scalar_socket_2d_generator(
                v_sock,
                staging_dir=staging_dir,
                size=size,
                forced_channel=forced_channel,
                _depth=_depth + 1,
                pct_val=pct_val,
                label_prefix=label_prefix,
            )
            return _cache_and_return_scalar(cache_key, res)
        v, im_v, ch_v = yield from _eval_in(v_sock, 0.5, forced_channel)
        if not isinstance(v, np.ndarray):
            return None, None, "Color"
        f_min, _, _ = yield from _eval_in(node.inputs.get("From Min"), 0.0)
        f_max, _, _ = yield from _eval_in(node.inputs.get("From Max"), 1.0)
        t_min, _, _ = yield from _eval_in(node.inputs.get("To Min"), 0.0)
        t_max, _, _ = yield from _eval_in(node.inputs.get("To Max"), 1.0)
        denom = f_max - f_min
        t = np.where(np.abs(denom) > 1e-6, (v - f_min) / denom, 0.0)
        res = np.asarray(t_min + t * (t_max - t_min), dtype=np.float32)
        if getattr(node, "clamp", True):
            lo = np.minimum(t_min, t_max)
            hi = np.maximum(t_min, t_max)
            res = np.clip(res, lo, hi)
        return _cache_and_return_scalar(cache_key, (res, im_v, ch_v))

    if node.type == "GAMMA":
        col_sock = node.inputs[0] if len(node.inputs) > 0 else None
        gam_sock = node.inputs[1] if len(node.inputs) > 1 else None
        col, im_c, ch_c = yield from _eval_in(col_sock, 0.5, forced_channel)
        gam, _, _ = yield from _eval_in(gam_sock, 1.0)
        if not isinstance(col, np.ndarray):
            return None, None, "Color"
        res = np.power(np.maximum(col, 0.0), gam).astype(np.float32)
        return _cache_and_return_scalar(cache_key, (res, im_c, ch_c))

    if node.type == "CLAMP":
        val, im_v, ch_v = yield from _eval_in(node.inputs[0] if len(node.inputs) > 0 else None, 0.5, forced_channel)
        lo, _, _ = yield from _eval_in(node.inputs[1] if len(node.inputs) > 1 else None, 0.0)
        hi, _, _ = yield from _eval_in(node.inputs[2] if len(node.inputs) > 2 else None, 1.0)
        if not isinstance(val, np.ndarray):
            return None, None, "Color"
        return _cache_and_return_scalar(cache_key, (np.clip(val, lo, hi).astype(np.float32), im_v, ch_v))

    if node.type == "VALTORGB" and getattr(node, "color_ramp", None) is not None:
        fac_sock = node.inputs[0] if len(node.inputs) > 0 else None
        fac, im_f, ch_f = yield from _eval_in(fac_sock, 0.5, forced_channel)
        if not isinstance(fac, np.ndarray):
            return None, None, "Color"
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating ColorRamp ({node.name})...", round(pct_val, 2))
        ramp = node.color_ramp
        xs = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        ys = np.empty(256, dtype=np.float32)
        for i, x in enumerate(xs):
            rgba = ramp.evaluate(float(x))
            if output.name == "Alpha":
                ys[i] = float(rgba[3])
            else:
                ys[i] = float(0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2])
        res = np.interp(np.clip(fac, 0.0, 1.0), xs, ys).astype(np.float32)
        return _cache_and_return_scalar(cache_key, (res, im_f, ch_f))

    if node.type in {"CURVE_RGB", "CURVE_FLOAT"} and getattr(node, "mapping", None) is not None:
        in_sock = node.inputs[1] if len(node.inputs) > 1 else (node.inputs[0] if len(node.inputs) > 0 else None)
        fac_sock = node.inputs[0] if len(node.inputs) > 1 else None
        val, im_v, ch_v = yield from _eval_in(in_sock, 0.5, forced_channel)
        fac, _, _ = (yield from _eval_in(fac_sock, 1.0)) if fac_sock is not None else (1.0, None, "Color")
        if not isinstance(val, np.ndarray):
            return None, None, "Color"
        mapping = node.mapping
        try:
            mapping.initialize()
            curve = mapping.curves[0]
            xs = np.linspace(0.0, 1.0, 256, dtype=np.float32)
            ys = np.array([float(mapping.evaluate(curve, float(x))) for x in xs], dtype=np.float32)
            mapped = np.interp(np.clip(val, 0.0, 1.0), xs, ys).astype(np.float32)
            res = (1.0 - fac) * val + fac * mapped
            return _cache_and_return_scalar(cache_key, (np.asarray(res, dtype=np.float32), im_v, ch_v))
        except Exception:
            return _cache_and_return_scalar(cache_key, (val, im_v, ch_v))

    if node.type in {"MIX", "MIX_RGB"}:
        if node.type == "MIX_RGB":
            f_s, a_s, b_s = node.inputs[0], node.inputs[1], node.inputs[2]
            blend = str(getattr(node, "blend_type", "MIX")).upper()
            clamp_res = bool(getattr(node, "use_clamp", False))
        else:
            dt = str(getattr(node, "data_type", "FLOAT")).upper()
            if dt == "RGBA":
                f_s = node.inputs[0]
                a_s = node.inputs[6] if len(node.inputs) > 6 else node.inputs.get("A")
                b_s = node.inputs[7] if len(node.inputs) > 7 else node.inputs.get("B")
            else:
                f_s = node.inputs[0]
                a_s = node.inputs[2] if len(node.inputs) > 2 else node.inputs.get("A")
                b_s = node.inputs[3] if len(node.inputs) > 3 else node.inputs.get("B")
            blend = str(getattr(node, "blend_type", "MIX")).upper()
            clamp_res = bool(getattr(node, "clamp_result", False))
        fac, im_f, ch_f = yield from _eval_in(f_s, 0.5, forced_channel)
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}scalar mix branch A ({node.name})...", round(pct_val, 2))
        a, im_a, ch_a = yield from _eval_in(a_s, 0.5, forced_channel)
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}scalar mix branch B ({node.name})...", round(pct_val, 2))
        b, im_b, ch_b = yield from _eval_in(b_s, 0.5, forced_channel)
        if not isinstance(fac, np.ndarray) and not isinstance(a, np.ndarray) and not isinstance(b, np.ndarray):
            return None, None, "Color"
        fac = np.clip(fac, 0.0, 1.0)
        if blend == "MULTIPLY":
            mixed = a * b
        elif blend == "ADD":
            mixed = a + b
        elif blend == "SUBTRACT":
            mixed = a - b
        elif blend == "SCREEN":
            mixed = 1.0 - (1.0 - a) * (1.0 - b)
        elif blend == "DARKEN":
            mixed = np.minimum(a, b)
        elif blend == "LIGHTEN":
            mixed = np.maximum(a, b)
        else:
            mixed = b
        res = np.asarray((1.0 - fac) * a + fac * mixed, dtype=np.float32)
        if clamp_res:
            res = np.clip(res, 0.0, 1.0)
        src_im = im_a or im_b or im_f
        src_ch = ch_a if im_a else (ch_b if im_b else ch_f)
        return _cache_and_return_scalar(cache_key, (res, src_im, src_ch))

    # Fallback: find any upstream linked socket
    in_fb = _first_linked_value_input(node)
    if in_fb is not None and in_fb.is_linked:
        res = yield from _evaluate_scalar_socket_2d_generator(
            in_fb,
            staging_dir=staging_dir,
            size=size,
            forced_channel=forced_channel,
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=label_prefix,
        )
        return _cache_and_return_scalar(cache_key, res)

    return None, None, forced_channel or "Color"


def _socket_rgb_default(sock, fallback: tuple[float, float, float] = (0.78, 0.78, 0.78)) -> np.ndarray:
    if sock is None or not hasattr(sock, "default_value"):
        return np.array(fallback, dtype=np.float32)
    dv = sock.default_value
    if hasattr(dv, "__len__") and len(dv) >= 3:
        return np.array([float(dv[0]), float(dv[1]), float(dv[2])], dtype=np.float32)
    try:
        v = float(dv)
        return np.array([v, v, v], dtype=np.float32)
    except Exception:
        return np.array(fallback, dtype=np.float32)


def _cache_and_return_color(cache_key: tuple, res: tuple) -> tuple:
    if res[0] is not None:
        if len(_SOCKET_COLOR_CACHE) > 128:
            _SOCKET_COLOR_CACHE.clear()
        _SOCKET_COLOR_CACHE[cache_key] = (res[0].copy(), res[1], res[2])
    return res


def _evaluate_color_socket_2d(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    _depth: int = 0,
) -> tuple[np.ndarray | None, bpy.types.Image | None, bool]:
    """
    Evaluates any Blender shader node chain connected to `Base Color` (or Color inputs)
    into a `(H, W, 3)` float32 RGB array in `[0.0, 1.0]` without altering UVs.
    Synchronous wrapper around _evaluate_color_socket_2d_generator.
    """
    gen = _evaluate_color_socket_2d_generator(
        socket,
        staging_dir=staging_dir,
        size=size,
        _depth=_depth,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _evaluate_color_socket_2d_generator(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    _depth: int = 0,
    pct_val: float = 0.0,
    label_prefix: str = "Base color mix",
):
    """
    Evaluates any Blender shader node chain connected to `Base Color` (or Color inputs)
    into a `(H, W, 3)` float32 RGB array in `[0.0, 1.0]` without altering UVs.
    Supports `TEX_IMAGE` (with Mapping scale/offset), `VALTORGB` (ColorRamp with full RGB stops),
    `MIX`/`MIX_RGB`, `TEX_NOISE`, `TEX_VORONOI`, `TEX_WAVE`, `INVERT`, `GAMMA`, `BRIGHTCONTRAST`,
    `HUE_SAT`, `CURVE_RGB`, and `RGB`.
    Yields ProgressStatus at MIX branches and memoizes in _SOCKET_COLOR_CACHE.
    Returns `(rgb_2d_or_None, primary_image_or_None, is_procedural_bool)`.
    """
    if socket is None or not socket.is_linked or not socket.links or _depth > 14:
        return None, None, False

    cache_key = (socket.as_pointer(), int(size[0]), int(size[1]))
    if cache_key in _SOCKET_COLOR_CACHE:
        arr, im, pr = _SOCKET_COLOR_CACHE[cache_key]
        return (arr.copy() if arr is not None else None), im, pr

    link = socket.links[0]
    node, output = link.from_node, link.from_socket
    w, h = int(size[0]), int(size[1])

    # Unwrap muted nodes or ReSkate_Wear_* / ReSkate_Decal_* viewport wrapper nodes transparently
    node_nm = str(getattr(node, "name", ""))
    if getattr(node, "mute", False) or node_nm.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
        if node_nm.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
            in_base = node.inputs[6] if len(node.inputs) > 6 else (node.inputs.get("A") or (node.inputs[1] if len(node.inputs) > 1 else None))
            res = yield from _evaluate_color_socket_2d_generator(in_base, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
            return _cache_and_return_color(cache_key, res)
        in0 = _first_linked_value_input(node)
        res = yield from _evaluate_color_socket_2d_generator(in0, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        return _cache_and_return_color(cache_key, res)

    if node.type in {"REROUTE", "FRAME"}:
        in0 = _first_linked_value_input(node)
        res = yield from _evaluate_color_socket_2d_generator(in0, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        return _cache_and_return_color(cache_key, res)

    if node.type == "GROUP" and getattr(node, "node_tree", None) is not None:
        g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT" and getattr(n, "is_active_output", True)), None)
        if g_out is None:
            g_out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT"), None)
        if g_out is not None:
            in_sock = g_out.inputs.get(output.name) if output is not None else None
            if in_sock is None or not in_sock.is_linked:
                in_sock = next((s for s in g_out.inputs if s.is_linked), None)
            if in_sock is not None:
                res = yield from _evaluate_color_socket_2d_generator(in_sock, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
                return _cache_and_return_color(cache_key, res)

    if node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None:
        pil_im = yield from _blender_image_to_pil_generator(
            node.image,
            staging_dir=staging_dir,
            is_color=True,
            label_prefix=f"{label_prefix}Image: ",
            pct_val=pct_val,
        )
        if pil_im is None:
            return None, None, False
        try:
            im_res = pil_im.resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
            rgb = (np.asarray(im_res, dtype=np.float32)[:, :, :3] / 255.0).copy()
            return _cache_and_return_color(cache_key, (rgb, node.image, False))
        except Exception:
            return None, None, False

    if node.type == "RGB":
        col = _socket_rgb_default(output, (0.78, 0.78, 0.78))
        arr = np.broadcast_to(col[None, None, :], (h, w, 3)).copy()
        return _cache_and_return_color(cache_key, (arr, None, True))

    if node.type in {"TEX_NOISE", "TEX_VORONOI", "TEX_WAVE", "TEX_CHECKER", "TEX_GRADIENT"}:
        if not is_experimental_addon_enabled():
            return None, None, False
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Synthesizing procedural {node.type} ({node.name})...", round(pct_val, 2))
        proc = _synthesize_procedural_node_2d(node, output_name="Color" if output.name == "Color" else "Fac", size=size)
        if proc is not None:
            if proc.ndim == 2:
                proc = np.dstack([proc, proc, proc])
            return _cache_and_return_color(cache_key, (proc.astype(np.float32), None, True))

    if node.type == "VALTORGB" and getattr(node, "color_ramp", None) is not None:
        fac_sock = node.inputs[0] if len(node.inputs) > 0 else None
        fac_2d, src_im, _ = yield from _evaluate_scalar_socket_2d_generator(
            fac_sock, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
        )
        if fac_2d is None:
            f_val = _socket_scalar_default(fac_sock, 0.5)
            rgba = node.color_ramp.evaluate(float(np.clip(f_val, 0.0, 1.0)))
            col = np.array([float(rgba[0]), float(rgba[1]), float(rgba[2])], dtype=np.float32)
            return _cache_and_return_color(cache_key, (np.broadcast_to(col[None, None, :], (h, w, 3)).copy(), None, True))
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating color ramp ({node.name})...", round(pct_val, 2))
        ramp = node.color_ramp
        xs = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        lut = np.empty((256, 3), dtype=np.float32)
        for i, x in enumerate(xs):
            rgba = ramp.evaluate(float(x))
            lut[i, 0] = float(rgba[0])
            lut[i, 1] = float(rgba[1])
            lut[i, 2] = float(rgba[2])
        fac_c = np.clip(fac_2d, 0.0, 1.0)
        r_ch = np.interp(fac_c, xs, lut[:, 0])
        g_ch = np.interp(fac_c, xs, lut[:, 1])
        b_ch = np.interp(fac_c, xs, lut[:, 2])
        rgb = np.dstack([r_ch, g_ch, b_ch]).astype(np.float32)
        return _cache_and_return_color(cache_key, (rgb, src_im, (src_im is None)))

    if node.type in {"MIX", "MIX_RGB"}:
        if node.type == "MIX_RGB":
            f_s, a_s, b_s = node.inputs[0], node.inputs[1], node.inputs[2]
            blend = str(getattr(node, "blend_type", "MIX")).upper()
            clamp_res = bool(getattr(node, "use_clamp", False))
        else:
            dt = str(getattr(node, "data_type", "RGBA")).upper()
            if dt == "RGBA":
                f_s = node.inputs[0]
                a_s = node.inputs[6] if len(node.inputs) > 6 else node.inputs.get("A")
                b_s = node.inputs[7] if len(node.inputs) > 7 else node.inputs.get("B")
            else:
                # Scalar mix plugged into color socket
                s_arr, s_im, _ = yield from _evaluate_scalar_socket_2d_generator(
                    socket, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
                )
                if s_arr is not None:
                    return _cache_and_return_color(cache_key, (np.dstack([s_arr, s_arr, s_arr]).astype(np.float32), s_im, (s_im is None)))
                return None, None, False
            blend = str(getattr(node, "blend_type", "MIX")).upper()
            clamp_res = bool(getattr(node, "clamp_result", False))

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating mix factor ({node.name})...", round(pct_val, 2))
        fac_2d, im_f, _ = yield from _evaluate_scalar_socket_2d_generator(
            f_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
        )
        fac = fac_2d[:, :, None] if fac_2d is not None else float(_socket_scalar_default(f_s, 0.5))
        fac = np.clip(fac, 0.0, 1.0)

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating mix branch A ({node.name})...", round(pct_val, 2))
        a_2d, im_a, proc_a = yield from _evaluate_color_socket_2d_generator(
            a_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
        )
        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Mix branch A complete ({node.name})...", round(pct_val, 2))

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating mix branch B ({node.name})...", round(pct_val, 2))
        b_2d, im_b, proc_b = yield from _evaluate_color_socket_2d_generator(
            b_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
        )

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Blending mix branches ({node.name})...", round(pct_val, 2))

        if a_2d is None and b_2d is None and fac_2d is None:
            return None, None, False
        a = a_2d if a_2d is not None else _socket_rgb_default(a_s, (0.5, 0.5, 0.5))[None, None, :]
        b = b_2d if b_2d is not None else _socket_rgb_default(b_s, (0.5, 0.5, 0.5))[None, None, :]

        if blend == "MULTIPLY":
            mixed = a * b
        elif blend == "ADD":
            mixed = a + b
        elif blend == "SUBTRACT":
            mixed = a - b
        elif blend == "SCREEN":
            mixed = 1.0 - (1.0 - a) * (1.0 - b)
        elif blend == "OVERLAY":
            mixed = np.where(a < 0.5, 2.0 * a * b, 1.0 - 2.0 * (1.0 - a) * (1.0 - b))
        elif blend == "DARKEN":
            mixed = np.minimum(a, b)
        elif blend == "LIGHTEN":
            mixed = np.maximum(a, b)
        else:
            mixed = b
        res = np.asarray((1.0 - fac) * a + fac * mixed, dtype=np.float32)
        if res.shape[:2] != (h, w):
            res = np.broadcast_to(res, (h, w, 3)).copy()
        if clamp_res:
            res = np.clip(res, 0.0, 1.0)
        return _cache_and_return_color(cache_key, (np.clip(res, 0.0, 1.0), (im_a or im_b or im_f), bool(proc_a or proc_b or (im_a is None and im_b is None))))

    if node.type == "INVERT":
        fac_s = node.inputs[0] if len(node.inputs) > 0 else None
        col_s = node.inputs[1] if len(node.inputs) > 1 else _first_linked_value_input(node)
        c_2d, im_c, proc_c = yield from _evaluate_color_socket_2d_generator(col_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        if c_2d is None:
            return None, None, False
        f_val = _socket_scalar_default(fac_s, 1.0)
        res = (1.0 - f_val) * c_2d + f_val * (1.0 - c_2d)
        return _cache_and_return_color(cache_key, (np.clip(res, 0.0, 1.0).astype(np.float32), im_c, proc_c))

    if node.type == "GAMMA":
        col_s = node.inputs[0] if len(node.inputs) > 0 else None
        gam_s = node.inputs[1] if len(node.inputs) > 1 else None
        c_2d, im_c, proc_c = yield from _evaluate_color_socket_2d_generator(col_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        if c_2d is None:
            return None, None, False
        gam = max(0.05, _socket_scalar_default(gam_s, 1.0))
        return _cache_and_return_color(cache_key, (np.power(np.clip(c_2d, 0.0, 1.0), gam).astype(np.float32), im_c, proc_c))

    if node.type == "BRIGHTCONTRAST":
        col_s = node.inputs.get("Color") or (node.inputs[0] if len(node.inputs) > 0 else None)
        c_2d, im_c, proc_c = yield from _evaluate_color_socket_2d_generator(col_s, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        if c_2d is None:
            return None, None, False
        bright = _socket_scalar_default(node.inputs.get("Bright"), 0.0)
        contr = _socket_scalar_default(node.inputs.get("Contrast"), 0.0)
        res = (c_2d - 0.5) * (1.0 + contr) + 0.5 + bright
        return _cache_and_return_color(cache_key, (np.clip(res, 0.0, 1.0).astype(np.float32), im_c, proc_c))

    # Fallback: if scalar node is plugged into Base Color, broadcast to RGB
    s_2d, s_im, _ = _evaluate_scalar_socket_2d(socket, staging_dir=staging_dir, size=size, _depth=_depth + 1)
    if s_2d is not None:
        s_c = np.clip(s_2d, 0.0, 1.0).astype(np.float32)
        return _cache_and_return_color(cache_key, (np.dstack([s_c, s_c, s_c]), s_im, (s_im is None)))
    return None, None, False


def _evaluate_normal_socket_2d(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    _depth: int = 0,
) -> tuple[np.ndarray | None, bpy.types.Image | None, bool]:
    """
    Synchronous wrapper for _evaluate_normal_socket_2d_generator.
    """
    gen = _evaluate_normal_socket_2d_generator(
        socket, staging_dir=staging_dir, size=size, _depth=_depth
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _evaluate_normal_socket_2d_generator(
    socket,
    staging_dir: Path | None = None,
    size: tuple[int, int] = (1024, 1024),
    _depth: int = 0,
    pct_val: float = 0.0,
    label_prefix: str = "",
):
    """
    Evaluates `Principled BSDF.inputs['Normal']` (supporting `ShaderNodeNormalMap` AND
    procedural `ShaderNodeBump` chains!) into a `(H, W, 3)` tangent-space normal map
    in `[0.0, 1.0]` (`(0.5, 0.5, 1.0)` = flat neutral normal).
    Yields ProgressStatus at procedural bump stages.
    Returns `(norm_01_2d_or_None, source_image_or_None, is_procedural_bool)`.
    """
    if socket is None or not socket.is_linked or not socket.links or _depth > 12:
        return None, None, False

    cache_key = (socket.as_pointer(), int(size[0]), int(size[1]))
    if cache_key in _SOCKET_NORMAL_CACHE:
        arr, im, pr = _SOCKET_NORMAL_CACHE[cache_key]
        return (arr.copy() if arr is not None else None), im, pr

    link = socket.links[0]
    node = link.from_node
    w, h = int(size[0]), int(size[1])

    if getattr(node, "mute", False) or str(getattr(node, "name", "")).startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
        n_in = node.inputs.get("Normal") or _first_linked_value_input(node)
        return (yield from _evaluate_normal_socket_2d_generator(n_in, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix))

    if node.type in {"REROUTE", "FRAME"}:
        in0 = _first_linked_value_input(node)
        return (yield from _evaluate_normal_socket_2d_generator(in0, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix))

    if node.type == "NORMAL_MAP":
        col_sock = node.inputs.get("Color")
        strength = max(0.0, min(4.0, _socket_scalar_default(node.inputs.get("Strength"), 1.0)))
        direct_n_img = _linked_image(col_sock) if col_sock is not None else None
        if direct_n_img is not None:
            pil_nm = yield from _blender_image_to_pil_generator(
                direct_n_img,
                staging_dir=staging_dir,
                is_color=False,
                label_prefix=f"{label_prefix}Normal: ",
                pct_val=pct_val,
            )
            if pil_nm is not None:
                nm_res = pil_nm.resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
                col_2d = (np.asarray(nm_res, dtype=np.float32)[:, :, :3] / 255.0).copy()
                src_im, is_proc = direct_n_img, False
            else:
                col_2d, src_im, is_proc = None, None, False
        else:
            col_2d, src_im, is_proc = yield from _evaluate_color_socket_2d_generator(col_sock, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix)
        if col_2d is None:
            return None, None, False
        n_xyz = col_2d * 2.0 - 1.0
        n_xyz[:, :, 0] *= strength
        n_xyz[:, :, 1] *= strength
        norm_len = np.linalg.norm(n_xyz, axis=2, keepdims=True)
        n_xyz = np.where(norm_len > 1e-6, n_xyz / np.maximum(norm_len, 1e-6), np.array([[[0.0, 0.0, 1.0]]], dtype=np.float32))
        res = (np.clip(n_xyz * 0.5 + 0.5, 0.0, 1.0).astype(np.float32), src_im, is_proc)
        if res[0] is not None:
            if len(_SOCKET_NORMAL_CACHE) > 128:
                _SOCKET_NORMAL_CACHE.clear()
            _SOCKET_NORMAL_CACHE[cache_key] = (res[0].copy(), res[1], res[2])
        return res

    if node.type == "BUMP":
        h_sock = node.inputs.get("Height")
        n_sock = node.inputs.get("Normal")
        strength = max(0.0, min(2.0, _socket_scalar_default(node.inputs.get("Strength"), 0.25)))
        invert = bool(getattr(node, "invert", False))

        base_n, base_im, base_proc = (None, None, False)
        if n_sock is not None and n_sock.is_linked:
            base_n, base_im, base_proc = yield from _evaluate_normal_socket_2d_generator(
                n_sock, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
            )

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Evaluating bump height map...", round(pct_val, 2))

        h_2d, h_im, _ = yield from _evaluate_scalar_socket_2d_generator(
            h_sock,
            staging_dir=staging_dir,
            size=size,
            _depth=_depth + 1,
            pct_val=pct_val,
            label_prefix=f"{label_prefix}bump: ",
        )
        if h_2d is None or strength <= 1e-4:
            return base_n, base_im, base_proc

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Computing bump height gradient...", round(pct_val, 2))

        sign = -1.0 if invert else 1.0
        h_f = h_2d.astype(np.float32) if h_2d.dtype != np.float32 else h_2d
        dh_dy, dh_dx = np.gradient(h_f)

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Constructing bump normal vectors...", round(pct_val, 2))

        bump_gain = strength * sign * 8.0
        bx = -dh_dx * bump_gain
        by = dh_dy * bump_gain
        if base_n is not None:
            bn = base_n * 2.0 - 1.0
            nx = bn[:, :, 0] + bx
            ny = bn[:, :, 1] + by
            nz = np.maximum(bn[:, :, 2], 0.15)
        else:
            nx = bx
            ny = by
            nz = np.ones_like(bx)
        n_vec = np.dstack([nx, ny, nz]).astype(np.float32)

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Normalizing bump normal vectors...", round(pct_val, 2))

        # Vectorized in-place Euclidean normalization (4x faster than np.linalg.norm)
        sq = n_vec[:, :, 0] ** 2 + n_vec[:, :, 1] ** 2 + n_vec[:, :, 2] ** 2
        inv_len = 1.0 / np.maximum(np.sqrt(sq, out=sq), 1e-6)
        n_vec[:, :, 0] *= inv_len
        n_vec[:, :, 1] *= inv_len
        n_vec[:, :, 2] *= inv_len

        if pct_val > 0.0:
            yield ProgressStatus(f"{label_prefix}Finalizing bump normal surface...", round(pct_val, 2))

        res_arr = np.clip(n_vec * 0.5 + 0.5, 0.0, 1.0).astype(np.float32)
        res = (res_arr, (base_im or h_im), True)
        if res[0] is not None:
            if len(_SOCKET_NORMAL_CACHE) > 128:
                _SOCKET_NORMAL_CACHE.clear()
            _SOCKET_NORMAL_CACHE[cache_key] = (res[0].copy(), res[1], res[2])
        return res

    # Direct image texture connected to Normal socket
    col_2d, src_im, is_proc = yield from _evaluate_color_socket_2d_generator(
        socket, staging_dir=staging_dir, size=size, _depth=_depth + 1, pct_val=pct_val, label_prefix=label_prefix
    )
    if col_2d is not None:
        res = (col_2d, src_im, is_proc)
        if res[0] is not None:
            if len(_SOCKET_NORMAL_CACHE) > 128:
                _SOCKET_NORMAL_CACHE.clear()
            _SOCKET_NORMAL_CACHE[cache_key] = (res[0].copy(), res[1], res[2])
        return res
    return None, None, False


# ==============================================================================
# PROCEDURAL EDGE WEAR, CREVICE DARKENING & TOP-DOWN GRIND SCRATCH ENGINE
# ==============================================================================
_WEAR_MASK_CACHE: dict[tuple, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}


def compute_uv_geometry_wear_masks(
    obj: bpy.types.Object | None,
    size: tuple[int, int] = (512, 512),
    wear_scale: float = 0.50,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Computes 3 geometry-driven procedural wear masks in UV0 space (`(H, W)` float32 in `0.0..1.0`)
    WITHOUT modifying the mesh or its UVs:
      1. `edge_mask`   : Sharp convex corners (dihedral >= 18° + bevel lips) with chipped noise falloff
      2. `crevice_mask`: Concave inner crevices (dihedral >= 15° + ground base contact) with cavity grime
      3. `top_mask`    : Upward-facing surfaces (`N_z > 0.35`) with directional skateboard-truck scratch striations
    """
    w, h = int(size[0]), int(size[1])
    zero = np.zeros((h, w), dtype=np.float32)
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return zero, zero, zero

    me = obj.data
    if len(me.polygons) == 0 or len(me.uv_layers) == 0:
        return zero, zero, zero

    scale_f = max(0.10, min(2.00, float(wear_scale or 0.50)))
    cache_key = (obj.name, len(me.vertices), len(me.polygons), w, h, round(scale_f, 3))
    if cache_key in _WEAR_MASK_CACHE:
        e_c, c_c, t_c = _WEAR_MASK_CACHE[cache_key]
        return e_c.copy(), c_c.copy(), t_c.copy()

    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return zero, zero, zero

    uv_layer = me.uv_layers.active or me.uv_layers[0]
    uv_data = uv_layer.data
    mw = obj.matrix_world
    n_mat = mw.to_3x3().inverted().transposed() if mw.determinant() != 0 else mw.to_3x3()

    # World-space vertex coordinates & Z bounds
    v_world = [mw @ v.co for v in me.vertices]
    z_vals = [p.z for p in v_world]
    z_min = min(z_vals) if z_vals else 0.0
    z_max = max(z_vals) if z_vals else 1.0
    z_span = max(0.05, z_max - z_min)

    # Polygon centers, world normals, and per-edge adjacent polygons + loop UV pairs
    poly_centers = []
    poly_normals = []
    edge_to_polys: dict[int, list[int]] = {}
    edge_uv_segs: dict[int, list[tuple[float, float, float, float]]] = {}

    # 1. Rasterize upward-facing polygon normals into `top_im` (Top-Down Surface Mask)
    top_im = Image.new("L", (w, h), 0)
    top_draw = ImageDraw.Draw(top_im)

    for p in me.polygons:
        pc = mw @ p.center
        pn = n_mat @ p.normal
        if pn.length_squared > 1e-10:
            pn.normalize()
        poly_centers.append(pc)
        poly_normals.append(pn)

        up_fac = max(0.0, min(1.0, (float(pn.z) - 0.32) / 0.58)) ** 1.35
        l_start = p.loop_start
        l_tot = p.loop_total
        loop_uvs = []
        for k in range(l_tot):
            uv = uv_data[l_start + k].uv
            u0 = float(uv[0]) % 1.0 if (uv[0] < -0.001 or uv[0] > 1.001) else max(0.0, min(1.0, float(uv[0])))
            v0 = float(uv[1]) % 1.0 if (uv[1] < -0.001 or uv[1] > 1.001) else max(0.0, min(1.0, float(uv[1])))
            px = int(round(u0 * (w - 1)))
            py = int(round((1.0 - v0) * (h - 1)))
            loop_uvs.append((px, py, u0, v0))

        if up_fac > 0.02 and len(loop_uvs) >= 3:
            up_u8 = int(max(1, min(255, round(up_fac * 255.0))))
            pts_2d = [(pt[0], pt[1]) for pt in loop_uvs]
            top_draw.polygon(pts_2d, fill=up_u8, outline=up_u8)

        for k in range(l_tot):
            li_a = l_start + k
            li_b = l_start + ((k + 1) % l_tot)
            e_idx = me.loops[li_a].edge_index
            edge_to_polys.setdefault(e_idx, []).append(p.index)
            _, _, ua, va = loop_uvs[k]
            _, _, ub, vb = loop_uvs[(k + 1) % l_tot]
            if abs(ua - ub) < 0.75 and abs(va - vb) < 0.75:
                edge_uv_segs.setdefault(e_idx, []).append((ua, va, ub, vb))

    # 2. Classify every 3D edge into Convex Sharp Edges vs Concave Crevices and draw into UV0
    edge_core_im = Image.new("L", (w, h), 0)
    edge_halo_im = Image.new("L", (w, h), 0)
    crev_im = Image.new("L", (w, h), 0)
    d_core = ImageDraw.Draw(edge_core_im)
    d_halo = ImageDraw.Draw(edge_halo_im)
    d_crev = ImageDraw.Draw(crev_im)

    w_core = max(2, int(round(4.0 * scale_f * (w / 512.0))))
    w_halo = max(4, int(round(14.0 * scale_f * (w / 512.0))))
    w_crev = max(4, int(round(16.0 * scale_f * (w / 512.0))))

    for e in me.edges:
        e_idx = e.index
        uv_list = edge_uv_segs.get(e_idx)
        if not uv_list:
            continue
        adj = edge_to_polys.get(e_idx, [])
        v0_w = v_world[e.vertices[0]]
        v1_w = v_world[e.vertices[1]]
        mid_z = 0.5 * (v0_w.z + v1_w.z)

        convex_w = 0.0
        concave_w = 0.0
        if len(adj) == 2:
            f0, f1 = adj[0], adj[1]
            n0, n1 = poly_normals[f0], poly_normals[f1]
            c0, c1 = poly_centers[f0], poly_centers[f1]
            dot_n = max(-1.0, min(1.0, float(n0.dot(n1))))
            ang_deg = math.degrees(math.acos(dot_n))
            if ang_deg >= 14.0:
                # Signed convexity: (c1 - c0) . (n0 - n1) > 0 => convex outer ridge; < 0 => concave inner crevice
                conv_dot = float((c1 - c0).dot(n0 - n1))
                if conv_dot > 1e-5:
                    convex_w = max(0.15, min(1.0, (ang_deg - 14.0) / 62.0))
                elif conv_dot < -1e-5:
                    concave_w = max(0.18, min(1.0, (ang_deg - 12.0) / 58.0))
        elif len(adj) == 1:
            # Open boundary edge acts as a sharp outer lip
            convex_w = 0.75

        # Subtle ground-base contact grime at bottom 3% of prop
        if mid_z <= z_min + max(0.015, 0.03 * z_span):
            concave_w = max(concave_w, 0.42)

        if convex_w > 0.01:
            u8_c = int(round(convex_w * 255.0))
            u8_h = int(round(convex_w * 210.0))
            for ua, va, ub, vb in uv_list:
                p0 = (int(round(ua * (w - 1))), int(round((1.0 - va) * (h - 1))))
                p1 = (int(round(ub * (w - 1))), int(round((1.0 - vb) * (h - 1))))
                d_halo.line([p0, p1], fill=u8_h, width=w_halo)
                d_core.line([p0, p1], fill=u8_c, width=w_core)

        if concave_w > 0.01:
            u8_cr = int(round(concave_w * 255.0))
            for ua, va, ub, vb in uv_list:
                p0 = (int(round(ua * (w - 1))), int(round((1.0 - va) * (h - 1))))
                p1 = (int(round(ub * (w - 1))), int(round((1.0 - vb) * (h - 1))))
                d_crev.line([p0, p1], fill=u8_cr, width=w_crev)

    blur_scale = max(0.5, scale_f * (w / 512.0))
    edge_core_f = np.asarray(edge_core_im.filter(ImageFilter.GaussianBlur(radius=1.6 * blur_scale)), dtype=np.float32) / 255.0
    edge_halo_f = np.asarray(edge_halo_im.filter(ImageFilter.GaussianBlur(radius=5.5 * blur_scale)), dtype=np.float32) / 255.0
    crev_f = np.asarray(crev_im.filter(ImageFilter.GaussianBlur(radius=6.5 * blur_scale)), dtype=np.float32) / 255.0
    top_f = np.asarray(top_im.filter(ImageFilter.GaussianBlur(radius=2.2 * (w / 512.0))), dtype=np.float32) / 255.0

    # Normalize blurred line masks so peak intensity reaches 1.0 along sharp edges and crevices
    # (GaussianBlur on thin UV edge lines otherwise dilutes peak values down to ~0.22).
    def _normalize_blurred_line_mask(arr: np.ndarray, boost: float = 1.18) -> np.ndarray:
        nz = arr[arr > 2e-3]
        if nz.size == 0:
            return arr
        p96 = float(np.percentile(nz, 96.0))
        denom = max(0.06, min(1.0, p96))
        return np.clip((arr / denom) * boost, 0.0, 1.0).astype(np.float32)

    edge_core_f = _normalize_blurred_line_mask(edge_core_f, boost=1.25)
    edge_halo_f = _normalize_blurred_line_mask(edge_halo_f, boost=1.12)
    crev_f = _normalize_blurred_line_mask(crev_f, boost=1.22)

    # 3. Synthesize procedural multi-octave chip noise (for sharp edges), grime noise (for crevices),
    #    and anisotropic skateboard-truck scratch striations (for top-down surfaces)
    seed_base = int(zlib.crc32(obj.name.encode("utf-8")) & 0x7FFFFFFF)
    rng = np.random.default_rng(seed_base)

    def _fast_noise(grid_w: int, grid_h: int) -> np.ndarray:
        gw = max(4, min(w, int(grid_w)))
        gh = max(4, min(h, int(grid_h)))
        g = rng.uniform(0.0, 1.0, size=(gh, gw)).astype(np.float32)
        im_n = Image.fromarray(np.clip(g * 255.0, 0, 255).astype(np.uint8), mode="L")
        return np.asarray(im_n.resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32) / 255.0

    freq = max(0.4, min(2.5, 1.0 / max(0.2, scale_f)))
    chip_n = 0.55 * _fast_noise(int(48 * freq), int(48 * freq)) + 0.45 * _fast_noise(int(140 * freq), int(140 * freq))
    grime_n = 0.60 * _fast_noise(int(36 * freq), int(36 * freq)) + 0.40 * _fast_noise(int(110 * freq), int(110 * freq))
    # Anisotropic directional skate-truck scratches (stretched along U and V + fine scuff breakup)
    scratch_u = _fast_noise(int(22 * freq), int(180 * freq))
    scratch_v = _fast_noise(int(180 * freq), int(22 * freq))
    scuff_fine = _fast_noise(int(160 * freq), int(160 * freq))
    scratch_pat = np.clip((np.maximum(scratch_u, scratch_v) - 0.34) * 2.25, 0.0, 1.0)
    scratch_pat = np.clip(0.68 * scratch_pat + 0.32 * scuff_fine, 0.0, 1.0)

    # Combine normalized edge falloff with chipped noise erosion so corners look naturally chipped/scuffed
    raw_edge = np.clip(np.maximum(edge_core_f, edge_halo_f * 0.82), 0.0, 1.0)
    edge_mask = np.clip(raw_edge * (0.55 + 0.78 * chip_n) - 0.06 * (1.0 - raw_edge), 0.0, 1.0).astype(np.float32)
    crevice_mask = np.clip(crev_f * (0.65 + 0.62 * grime_n), 0.0, 1.0).astype(np.float32)
    # Top-down wear is concentrated on upward-facing surfaces + boosted near top grind edges
    top_mask = np.clip(top_f * (0.40 + 0.78 * scratch_pat) + 0.35 * top_f * edge_mask, 0.0, 1.0).astype(np.float32)

    if len(_WEAR_MASK_CACHE) > 24:
        _WEAR_MASK_CACHE.clear()
    _WEAR_MASK_CACHE[cache_key] = (edge_mask.copy(), crevice_mask.copy(), top_mask.copy())
    return edge_mask, crevice_mask, top_mask


def _overdrive_wear_mask(mask: np.ndarray, strength: float) -> np.ndarray:
    """
    Applies a 0.0..3.0 overdrive curve to a normalized wear mask:
      - 0.0..1.0 scales linearly up to full crisp edge/crevice/scratch intensity.
      - 1.0..3.0 expands the spatial falloff halo (gamma < 1) and boosts contrast so pushing
        sliders past 1.0 produces heavy, unmistakable chipped edges, deep cavity grime, and
        dense top-down grind scratches.
    """
    s = max(0.0, min(3.0, float(strength)))
    if s <= 1e-4:
        return np.zeros_like(mask, dtype=np.float32)
    m = np.clip(mask, 0.0, 1.0)
    if s > 1.0:
        gamma = 1.0 / (1.0 + 0.58 * (s - 1.0))
        m = np.power(m, gamma)
    return np.clip(m * s, 0.0, 1.0).astype(np.float32)


def _resolve_effective_wear_basis(prop_state: dict, mat: bpy.types.Material | None, mat_metallic: float) -> str:
    basis = str(prop_state.get("wear_basis", "AUTO") or "AUTO").upper()
    if basis != "AUTO":
        return basis
    mname = (mat.name.lower() if mat else "") + " " + str(prop_state.get("primary_surface", "")).lower()
    if mat_metallic >= 0.55 or any(k in mname for k in ("metal", "rail", "steel", "chrome", "coping", "iron")):
        return "METAL_RAIL"
    if any(k in mname for k in ("paint", "coat", "enamel", "powder")):
        return "PAINTED"
    if any(k in mname for k in ("wood", "plywood", "timber", "ramp", "masonite")):
        return "WOOD"
    if any(k in mname for k in ("asphalt", "brick", "road", "tar")):
        return "ASPHALT"
    return "CONCRETE"


def apply_procedural_wear_to_pbr_maps(
    obj: bpy.types.Object | None,
    mat: bpy.types.Material | None,
    prop_state: dict,
    rgba_arr: np.ndarray,
    rough_map_2d: np.ndarray | None,
    mat_roughness: float,
    metal_map_2d: np.ndarray | None,
    mat_metallic: float,
    norm_01_2d: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray | None, float, np.ndarray | None, float, np.ndarray | None]:
    """
    Applies Procedural Sharp Edge Wear & Bump, Crevice Darkening, and Top-Down Grind Scratches
    onto `rgba_arr`, `rough_map_2d`, `metal_map_2d`, and `norm_01_2d` (works on both Starter
    Templates/solid colors and Image Textures without modifying UVs, supporting 0.0..3.0 overdrive).
    """
    if not is_experimental_addon_enabled() or not bool(prop_state.get("use_procedural_wear", False)):
        return rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d

    w_edge = max(0.0, min(3.0, float(prop_state.get("wear_edge_strength", 0.45))))
    w_crev = max(0.0, min(3.0, float(prop_state.get("wear_crevice_strength", 0.45))))
    w_top = max(0.0, min(3.0, float(prop_state.get("wear_top_strength", 0.35))))
    w_scale = max(0.10, min(2.00, float(prop_state.get("wear_scale", 0.50))))
    if w_edge <= 1e-3 and w_crev <= 1e-3 and w_top <= 1e-3:
        return rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d

    h, w = rgba_arr.shape[:2]
    edge_m, crev_m, top_m = compute_uv_geometry_wear_masks(obj, size=(w, h), wear_scale=w_scale)
    if float(edge_m.max()) <= 1e-4 and float(crev_m.max()) <= 1e-4 and float(top_m.max()) <= 1e-4:
        return rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d

    basis = _resolve_effective_wear_basis(prop_state, mat, mat_metallic)

    # Basis-specific wear appearance parameters (calibrated so 1.0 is strong and 3.0 is heavy overdrive)
    basis_cfg = {
        "CONCRETE": {
            "edge_lighten": 0.48,
            "edge_tint": np.array([0.88, 0.87, 0.84], dtype=np.float32),
            "edge_rough_delta": 0.28,
            "edge_metal_target": None,
            "edge_bump": 0.85,
            "crev_darken": 0.72,
            "top_lighten": 0.36,
            "top_rough_min": 0.82,
            "top_bump": 0.55,
        },
        "METAL_RAIL": {
            "edge_lighten": 0.54,
            "edge_tint": np.array([0.92, 0.94, 0.97], dtype=np.float32),
            "edge_rough_delta": 0.32,
            "edge_metal_target": 0.98,
            "edge_bump": 0.65,
            "crev_darken": 0.68,
            "top_lighten": 0.50,
            "top_rough_min": 0.68,  # Scratched top of shiny metal rail becomes visibly rougher & lighter!
            "top_bump": 0.70,
        },
        "PAINTED": {
            "edge_lighten": 0.66,
            "edge_tint": np.array([0.86, 0.88, 0.91], dtype=np.float32),
            "edge_rough_delta": 0.36,
            "edge_metal_target": 0.82,  # Exposed bare metal/primer on chipped painted edges
            "edge_bump": 0.80,
            "crev_darken": 0.70,
            "top_lighten": 0.52,
            "top_rough_min": 0.70,
            "top_bump": 0.65,
        },
        "WOOD": {
            "edge_lighten": 0.50,
            "edge_tint": np.array([0.84, 0.70, 0.52], dtype=np.float32),
            "edge_rough_delta": 0.30,
            "edge_metal_target": None,
            "edge_bump": 0.75,
            "crev_darken": 0.72,
            "top_lighten": 0.40,
            "top_rough_min": 0.78,
            "top_bump": 0.55,
        },
        "ASPHALT": {
            "edge_lighten": 0.52,
            "edge_tint": np.array([0.62, 0.61, 0.58], dtype=np.float32),
            "edge_rough_delta": 0.28,
            "edge_metal_target": None,
            "edge_bump": 0.95,
            "crev_darken": 0.78,
            "top_lighten": 0.38,
            "top_rough_min": 0.90,
            "top_bump": 0.65,
        },
    }.get(basis, None) or {
        "edge_lighten": 0.50,
        "edge_tint": np.array([0.88, 0.88, 0.88], dtype=np.float32),
        "edge_rough_delta": 0.28,
        "edge_metal_target": None,
        "edge_bump": 0.75,
        "crev_darken": 0.70,
        "top_lighten": 0.42,
        "top_rough_min": 0.72,
        "top_bump": 0.55,
    }

    rgb = rgba_arr[:, :, :3].copy()
    r_2d = rough_map_2d.copy() if rough_map_2d is not None else np.full((h, w), mat_roughness, dtype=np.float32)
    m_2d = metal_map_2d.copy() if metal_map_2d is not None else np.full((h, w), mat_metallic, dtype=np.float32)

    edge_fac = _overdrive_wear_mask(edge_m, w_edge)
    crev_fac = _overdrive_wear_mask(crev_m, w_crev)
    top_fac = _overdrive_wear_mask(top_m, w_top)

    # 1. Crevice Darkening (Cavity Grime)
    if w_crev > 1e-3:
        darken_strength = min(0.96, float(basis_cfg["crev_darken"]) * (1.0 + 0.28 * max(0.0, w_crev - 1.0)))
        rgb *= (1.0 - crev_fac[:, :, None] * darken_strength)
        r_2d = np.clip(r_2d + crev_fac * 0.45, 0.0, 1.0)

    # 2. Sharp Edge Wear (Lightened Chipped Corners + Roughness Variation)
    if w_edge > 1e-3:
        lighten_boost = min(0.95, float(basis_cfg["edge_lighten"]) * (1.0 + 0.36 * max(0.0, w_edge - 1.0)))
        lighten_amt = np.clip(edge_fac[:, :, None] * lighten_boost, 0.0, 0.95)
        target_edge_rgb = np.clip(
            np.maximum(rgb + 0.30 + 0.12 * max(0.0, w_edge - 1.0), basis_cfg["edge_tint"][None, None, :]),
            0.0,
            1.0,
        )
        rgb = rgb * (1.0 - lighten_amt) + target_edge_rgb * lighten_amt
        r_2d = np.clip(r_2d + edge_fac * float(basis_cfg["edge_rough_delta"]) * (1.0 + 0.30 * max(0.0, w_edge - 1.0)), 0.0, 1.0)
        if basis_cfg["edge_metal_target"] is not None:
            m_blend = np.clip(edge_fac * (0.65 + 0.15 * max(0.0, w_edge - 1.0)), 0.0, 0.95)
            m_2d = np.clip(m_2d * (1.0 - m_blend) + float(basis_cfg["edge_metal_target"]) * m_blend, 0.0, 1.0)

    # 3. Top-Down Surface / Rail Grind Scratch Wear (Lighter + Rougher Scratched Top)
    if w_top > 1e-3:
        top_light_boost = min(0.94, float(basis_cfg["top_lighten"]) * (1.0 + 0.38 * max(0.0, w_top - 1.0)))
        top_light = np.clip(top_fac[:, :, None] * top_light_boost, 0.0, 0.94)
        scratch_rgb = np.clip(
            np.maximum(rgb + 0.32 + 0.14 * max(0.0, w_top - 1.0), basis_cfg["edge_tint"][None, None, :]),
            0.0,
            1.0,
        )
        rgb = rgb * (1.0 - top_light) + scratch_rgb * top_light
        # Roughen top surface along grind scratches (crucial for shiny metal rails!)
        target_top_r = np.maximum(r_2d + 0.40 * top_fac, float(basis_cfg["top_rough_min"]) * top_fac)
        r_blend = np.clip(top_fac * (0.80 + 0.10 * max(0.0, w_top - 1.0)), 0.0, 0.96)
        r_2d = np.clip(r_2d * (1.0 - r_blend) + target_top_r * r_blend, 0.0, 1.0)

    rgba_arr = rgba_arr.copy()
    rgba_arr[:, :, :3] = np.clip(rgb, 0.0, 1.0)
    mat_roughness = float(r_2d.mean())
    mat_metallic = float(m_2d.mean())

    # 4. Edge Chip & Top Scratch Normal-Map Micro-Relief (`norm_01_2d`)
    if w_edge > 1e-3 or w_top > 1e-3 or w_crev > 1e-3:
        height_field = (
            -edge_fac * float(basis_cfg["edge_bump"]) * (1.0 + 0.35 * max(0.0, w_edge - 1.0))
            - top_fac * float(basis_cfg["top_bump"]) * (1.0 + 0.35 * max(0.0, w_top - 1.0))
            - crev_fac * 0.38 * (1.0 + 0.25 * max(0.0, w_crev - 1.0))
        ).astype(np.float32)
        dh_dy, dh_dx = np.gradient(height_field)
        bx = -dh_dx * 8.0
        by = dh_dy * 8.0
        if norm_01_2d is not None:
            bn = norm_01_2d * 2.0 - 1.0
            nx = bn[:, :, 0] + bx
            ny = bn[:, :, 1] + by
            nz = np.maximum(bn[:, :, 2], 0.15)
        else:
            nx = bx
            ny = by
            nz = np.ones_like(bx)
        n_vec = np.dstack([nx, ny, nz]).astype(np.float32)
        n_len = np.linalg.norm(n_vec, axis=2, keepdims=True)
        n_vec = np.where(n_len > 1e-6, n_vec / np.maximum(n_len, 1e-6), np.array([[[0.0, 0.0, 1.0]]], dtype=np.float32))
        norm_01_2d = np.clip(n_vec * 0.5 + 0.5, 0.0, 1.0).astype(np.float32)

    return rgba_arr, r_2d, mat_roughness, m_2d, mat_metallic, norm_01_2d


# ---------------------------------------------------------------------------
# Projected PBR Decal Viewport Synchronization & UV Space Baking
# ---------------------------------------------------------------------------

def remove_projected_decal_nodes_from_material(mat: bpy.types.Material | None):
    """
    Non-destructively removes all `ReSkate_Decal_*` viewport projection nodes from `mat`,
    restoring pristine upstream links to Principled BSDF or Procedural Wear.
    """
    if not mat or not mat.use_nodes or not mat.node_tree:
        return
    nt = mat.node_tree
    p_node = _principled_node(mat)
    if p_node is None:
        return

    # 1. Restore Base Color link
    bc_sock = p_node.inputs.get("Base Color")
    if bc_sock and bc_sock.is_linked and bc_sock.links[0].from_node.name.startswith("ReSkate_Decal_"):
        cur = bc_sock.links[0].from_node
        orig_out = None
        while cur and cur.name.startswith("ReSkate_Decal_"):
            in_a = cur.inputs[6] if (len(cur.inputs) > 6 and cur.inputs[6].is_linked) else cur.inputs.get("A")
            if in_a and in_a.is_linked:
                if in_a.links[0].from_node.name.startswith("ReSkate_Decal_"):
                    cur = in_a.links[0].from_node
                else:
                    orig_out = in_a.links[0].from_socket
                    break
            else:
                break
        for l in list(bc_sock.links):
            nt.links.remove(l)
        if orig_out is not None:
            nt.links.new(orig_out, bc_sock)

    # 2. Restore Roughness link
    r_sock = p_node.inputs.get("Roughness")
    if r_sock and r_sock.is_linked and r_sock.links[0].from_node.name.startswith("ReSkate_Decal_"):
        cur = r_sock.links[0].from_node
        orig_out = None
        while cur and cur.name.startswith("ReSkate_Decal_"):
            in_a = cur.inputs.get("A") or (cur.inputs[6] if len(cur.inputs) > 6 else None)
            if in_a and in_a.is_linked:
                if in_a.links[0].from_node.name.startswith("ReSkate_Decal_"):
                    cur = in_a.links[0].from_node
                else:
                    orig_out = in_a.links[0].from_socket
                    break
            else:
                break
        for l in list(r_sock.links):
            nt.links.remove(l)
        if orig_out is not None:
            nt.links.new(orig_out, r_sock)

    # 3. Restore Metallic link
    m_sock = p_node.inputs.get("Metallic")
    if m_sock and m_sock.is_linked and m_sock.links[0].from_node.name.startswith("ReSkate_Decal_"):
        cur = m_sock.links[0].from_node
        orig_out = None
        while cur and cur.name.startswith("ReSkate_Decal_"):
            in_a = cur.inputs.get("A") or (cur.inputs[6] if len(cur.inputs) > 6 else None)
            if in_a and in_a.is_linked:
                if in_a.links[0].from_node.name.startswith("ReSkate_Decal_"):
                    cur = in_a.links[0].from_node
                else:
                    orig_out = in_a.links[0].from_socket
                    break
            else:
                break
        for l in list(m_sock.links):
            nt.links.remove(l)
        if orig_out is not None:
            nt.links.new(orig_out, m_sock)

    # 4. Restore Normal link
    n_sock = p_node.inputs.get("Normal")
    if n_sock and n_sock.is_linked and n_sock.links[0].from_node.name.startswith("ReSkate_Decal_"):
        cur = n_sock.links[0].from_node
        orig_out = None
        while cur and cur.name.startswith("ReSkate_Decal_"):
            in_n = cur.inputs.get("Normal")
            if in_n and in_n.is_linked:
                if in_n.links[0].from_node.name.startswith("ReSkate_Decal_"):
                    cur = in_n.links[0].from_node
                else:
                    orig_out = in_n.links[0].from_socket
                    break
            else:
                break
        for l in list(n_sock.links):
            nt.links.remove(l)
        if orig_out is not None:
            nt.links.new(orig_out, n_sock)

    # Remove all decal nodes
    for n in list(nt.nodes):
        if n.name.startswith("ReSkate_Decal_"):
            nt.nodes.remove(n)


def remove_procedural_wear_nodes_from_material(mat: bpy.types.Material | None):
    """Cleanly unhooks and removes `ReSkate_Wear_*` viewport preview nodes from `mat`, restoring original links."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    p_node = _principled_node(mat)
    if p_node is not None:
        # Restore Base Color link if routed through ReSkate_Wear_EdgeMix / ReSkate_Wear_CrevMix
        bc_sock = p_node.inputs.get("Base Color")
        if bc_sock and bc_sock.is_linked and bc_sock.links[0].from_node.name.startswith("ReSkate_Wear_"):
            cur_n = bc_sock.links[0].from_node
            orig_out = None
            while cur_n and cur_n.name.startswith("ReSkate_Wear_"):
                in_a = cur_n.inputs[6] if len(cur_n.inputs) > 6 else (cur_n.inputs.get("A") or (cur_n.inputs[1] if len(cur_n.inputs) > 1 else None))
                if in_a and in_a.is_linked:
                    if in_a.links[0].from_node.name.startswith("ReSkate_Wear_"):
                        cur_n = in_a.links[0].from_node
                    else:
                        orig_out = in_a.links[0].from_socket
                        break
                else:
                    break
            for l in list(bc_sock.links):
                links.remove(l)
            if orig_out is not None:
                links.new(orig_out, bc_sock)

        # Restore Roughness link if routed through ReSkate_Wear_RoughAdd
        r_sock = p_node.inputs.get("Roughness")
        if r_sock and r_sock.is_linked and r_sock.links[0].from_node.name.startswith("ReSkate_Wear_"):
            wn = r_sock.links[0].from_node
            in0 = wn.inputs[0] if len(wn.inputs) > 0 else None
            orig_r = in0.links[0].from_socket if (in0 and in0.is_linked and not in0.links[0].from_node.name.startswith("ReSkate_Wear_")) else None
            for l in list(r_sock.links):
                links.remove(l)
            if orig_r is not None:
                links.new(orig_r, r_sock)

        # Restore Normal link if routed through ReSkate_Wear_Bump
        n_sock = p_node.inputs.get("Normal")
        if n_sock and n_sock.is_linked and n_sock.links[0].from_node.name.startswith("ReSkate_Wear_"):
            wn = n_sock.links[0].from_node
            in_n = wn.inputs.get("Normal")
            orig_n = in_n.links[0].from_socket if (in_n and in_n.is_linked and not in_n.links[0].from_node.name.startswith("ReSkate_Wear_")) else None
            for l in list(n_sock.links):
                links.remove(l)
            if orig_n is not None:
                links.new(orig_n, n_sock)

    for n in list(nodes):
        if n.name.startswith("ReSkate_Wear_"):
            nodes.remove(n)


def sync_procedural_wear_to_viewport(obj: bpy.types.Object | None, prop=None):
    """
    Updates the live Blender 3D Viewport shader nodes (`ReSkate_Wear_*`) on `obj`
    so the user sees Sharp Edge Wear, Crevice Darkening, and Top-Down Grind Scratches
    in real time on top of both Starter Templates and Image Textures without altering UVs.
    """
    if not is_experimental_addon_enabled():
        return False
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return False
    st = get_or_init_object_prop_state(obj)
    if prop is not None:
        use_wear = bool(getattr(prop, "use_procedural_wear", st.get("use_procedural_wear", False)))
        w_edge = float(getattr(prop, "wear_edge_strength", st.get("wear_edge_strength", 0.45)))
        w_crev = float(getattr(prop, "wear_crevice_strength", st.get("wear_crevice_strength", 0.45)))
        w_top = float(getattr(prop, "wear_top_strength", st.get("wear_top_strength", 0.35)))
        w_scale = float(getattr(prop, "wear_scale", st.get("wear_scale", 0.50)))
    else:
        use_wear = bool(st.get("use_procedural_wear", False))
        w_edge = float(st.get("wear_edge_strength", 0.45))
        w_crev = float(st.get("wear_crevice_strength", 0.45))
        w_top = float(st.get("wear_top_strength", 0.35))
        w_scale = float(st.get("wear_scale", 0.50))

    mats = [s.material for s in obj.material_slots if s.material is not None]
    if not use_wear or (w_edge <= 1e-3 and w_crev <= 1e-3 and w_top <= 1e-3):
        for m in mats:
            remove_procedural_wear_nodes_from_material(m)
        return

    # Ensure each material slot on `obj` is uniquely owned by `obj` before injecting object-specific wear nodes
    mats = ensure_all_object_materials_unique(obj)

    # Build/update the 128x128 UV0 wear mask image (R = Edge+Top Lighten, G = Crevice Darken, B = Roughness+Bump Delta)
    w_res, h_res = 128, 128
    edge_m, crev_m, top_m = compute_uv_geometry_wear_masks(obj, size=(w_res, h_res), wear_scale=w_scale)
    edge_fac = _overdrive_wear_mask(edge_m, w_edge)
    crev_fac = _overdrive_wear_mask(crev_m, w_crev)
    top_fac = _overdrive_wear_mask(top_m, w_top)

    r_ch = np.clip(edge_fac * 0.68 + top_fac * 0.58, 0.0, 1.0)
    g_ch = np.clip(1.0 - crev_fac * 0.88, 0.02, 1.0)
    b_ch = np.clip(edge_fac * 0.55 + top_fac * 0.72 + crev_fac * 0.38, 0.0, 1.0)

    # Flip vertically for Blender OpenGL bottom-up image.pixels
    rgba_gl = np.ones((h_res, w_res, 4), dtype=np.float32)
    rgba_gl[:, :, 0] = r_ch[::-1, :]
    rgba_gl[:, :, 1] = g_ch[::-1, :]
    rgba_gl[:, :, 2] = b_ch[::-1, :]

    img_name = f"ReSkate_WearMask_{obj.name}"
    bl_img = bpy.data.images.get(img_name)
    if bl_img is None or int(bl_img.size[0]) != w_res or int(bl_img.size[1]) != h_res:
        if bl_img is not None:
            bpy.data.images.remove(bl_img)
        bl_img = bpy.data.images.new(name=img_name, width=w_res, height=h_res, alpha=False)
        try:
            bl_img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
    bl_img.pixels.foreach_set(rgba_gl.ravel())
    bl_img.update()

    bump_str = min(1.0, max(w_edge, w_top) * 0.55)
    for mat in mats:
        if not mat.use_nodes or not mat.node_tree:
            continue
        nt = mat.node_tree
        nodes = nt.nodes
        links = nt.links
        p_node = _principled_node(mat)
        if p_node is None:
            continue

        # Fast path: if ReSkate_Wear_* nodes are already wired into p_node, update in-place
        # without deleting/recreating shader nodes (avoids EEVEE Next GLSL recompilation freeze!)
        existing_tex = nodes.get("ReSkate_Wear_MaskTex")
        existing_edge_mix = nodes.get("ReSkate_Wear_EdgeMix")
        existing_bump = nodes.get("ReSkate_Wear_Bump")
        bc_sock = p_node.inputs.get("Base Color")
        already_wired = (
            existing_tex is not None
            and existing_edge_mix is not None
            and bc_sock is not None
            and bc_sock.is_linked
            and bc_sock.links[0].from_node == existing_edge_mix
        )
        if already_wired:
            if existing_tex.image != bl_img:
                existing_tex.image = bl_img
            if existing_bump is not None and "Strength" in existing_bump.inputs:
                existing_bump.inputs["Strength"].default_value = bump_str
            continue

        remove_procedural_wear_nodes_from_material(mat)

        tex_n = nodes.new("ShaderNodeTexImage")
        tex_n.name = "ReSkate_Wear_MaskTex"
        tex_n.label = "ReSkate Geometry Wear Mask"
        tex_n.image = bl_img
        tex_n.location = (p_node.location.x - 520, p_node.location.y - 380)

        sep_n = nodes.new("ShaderNodeSeparateColor")
        sep_n.name = "ReSkate_Wear_Sep"
        sep_n.location = (p_node.location.x - 280, p_node.location.y - 380)
        links.new(tex_n.outputs["Color"], sep_n.inputs["Color"])

        # 1. Base Color: Multiply by Crevice (G) then Screen/Add Edge+Top Scratches (R)
        bc_sock = p_node.inputs.get("Base Color")
        if bc_sock is not None:
            orig_bc_out = bc_sock.links[0].from_socket if bc_sock.is_linked else None
            orig_bc_col = tuple(bc_sock.default_value)

            crev_mix = nodes.new("ShaderNodeMix")
            crev_mix.name = "ReSkate_Wear_CrevMix"
            crev_mix.data_type = 'RGBA'
            crev_mix.blend_type = 'MULTIPLY'
            crev_mix.inputs[0].default_value = 1.0
            crev_mix.inputs[6].default_value = orig_bc_col
            if orig_bc_out is not None:
                links.new(orig_bc_out, crev_mix.inputs[6])
            links.new(sep_n.outputs["Green"], crev_mix.inputs[7])

            edge_mix = nodes.new("ShaderNodeMix")
            edge_mix.name = "ReSkate_Wear_EdgeMix"
            edge_mix.data_type = 'RGBA'
            edge_mix.blend_type = 'SCREEN'
            edge_mix.inputs[7].default_value = (0.90, 0.91, 0.92, 1.0)
            links.new(sep_n.outputs["Red"], edge_mix.inputs[0])
            links.new(crev_mix.outputs[2], edge_mix.inputs[6])
            links.new(edge_mix.outputs[2], bc_sock)

        # 2. Roughness: Add wear roughness delta (B) to base Roughness
        r_sock = p_node.inputs.get("Roughness")
        if r_sock is not None:
            orig_r_out = r_sock.links[0].from_socket if r_sock.is_linked else None
            orig_r_val = float(r_sock.default_value)
            r_add = nodes.new("ShaderNodeMath")
            r_add.name = "ReSkate_Wear_RoughAdd"
            r_add.operation = 'ADD'
            r_add.use_clamp = True
            r_add.inputs[0].default_value = orig_r_val
            if orig_r_out is not None:
                links.new(orig_r_out, r_add.inputs[0])
            links.new(sep_n.outputs["Blue"], r_add.inputs[1])
            links.new(r_add.outputs["Value"], r_sock)

        # 3. Normal: Chipped Edge & Top Scratch Bump
        n_sock = p_node.inputs.get("Normal")
        if n_sock is not None:
            orig_n_out = n_sock.links[0].from_socket if n_sock.is_linked else None
            w_bump = nodes.new("ShaderNodeBump")
            w_bump.name = "ReSkate_Wear_Bump"
            w_bump.invert = True
            w_bump.inputs["Strength"].default_value = min(1.0, max(w_edge, w_top) * 0.55)
            w_bump.inputs["Distance"].default_value = 0.020
            links.new(sep_n.outputs["Blue"], w_bump.inputs["Height"])
            if orig_n_out is not None:
                links.new(orig_n_out, w_bump.inputs["Normal"])
            links.new(w_bump.outputs["Normal"], n_sock)


def _is_internal_wear_or_bake_image(img: bpy.types.Image | None) -> bool:
    if img is None:
        return False
    nm = str(getattr(img, "name", "") or "")
    return nm.startswith(("ReSkate_WearMask_", "_ReSkate_Temp_Bake"))


def _parse_bake_prefix_and_suffix(img_name: str) -> tuple[str, str, str] | None:
    """
    Parses a standard baked texture name like `Cylinder.002_Roughness.002` or `Cube_Var3.003_BaseColor`
    into `(prefix, map_type_lower, suffix)` e.g. `('Cylinder.002', 'roughness', '.002')`.
    """
    if not img_name:
        return None
    m = re.match(
        r"^(.+?)_(BaseColor|base_color|Albedo|albedo|Roughness|roughness|Metallic|metallic|Normal|normal|AmbientOcclusion|AO)(\.\d+)?$",
        str(img_name),
    )
    if not m:
        return None
    return m.group(1), m.group(2).lower(), (m.group(3) or "")


def _sample_blender_image_uv_stats(img: bpy.types.Image | None, uv_coords: np.ndarray | None = None) -> dict:
    """
    Fast strided pixel check on a Blender Image (either across the whole image or at specific polygon UVs).
    Returns `{'valid': bool, 'non_black_ratio': float, 'std_rgb': float, 'mean_rgb': (r, g, b), 'is_flat_default_white': bool}`.
    """
    res = {"valid": False, "non_black_ratio": 0.0, "std_rgb": 0.0, "mean_rgb": (0.0, 0.0, 0.0), "is_flat_default_white": False}
    if img is None:
        return res
    try:
        w, h = int(img.size[0]), int(img.size[1])
        ch = int(getattr(img, "channels", 4) or 4)
        if w <= 0 or h <= 0 or ch < 3 or len(img.pixels) != w * h * ch:
            return res
        buf = np.empty(w * h * ch, dtype=np.float32)
        img.pixels.foreach_get(buf)
        arr = buf.reshape((h, w, ch))
        if uv_coords is not None and len(uv_coords) > 0:
            us = np.clip(np.mod(uv_coords[:, 0], 1.0) * (w - 1), 0, w - 1).astype(np.int32)
            vs = np.clip(np.mod(uv_coords[:, 1], 1.0) * (h - 1), 0, h - 1).astype(np.int32)
            samples = arr[vs, us, :3]
        else:
            step_y = max(1, h // 32)
            step_x = max(1, w // 32)
            samples = arr[::step_y, ::step_x, :3].reshape((-1, 3))
        if len(samples) == 0:
            return res
        max_rgb = samples.max(axis=1)
        non_black = float(np.mean(max_rgb > 0.012))
        std_rgb = float(samples.std())
        mean_r, mean_g, mean_b = float(samples[:, 0].mean()), float(samples[:, 1].mean()), float(samples[:, 2].mean())
        # Sanctus Bake unlinked Principled BSDF Base Color default is 0.8 linear (or ~0.906 sRGB) with zero variance
        is_flat_white = (
            non_black > 0.90
            and std_rgb < 0.0025
            and abs(mean_r - mean_g) < 0.003
            and abs(mean_g - mean_b) < 0.003
            and (0.76 <= mean_r <= 0.93)
        )
        res.update({
            "valid": True,
            "non_black_ratio": non_black,
            "std_rgb": std_rgb,
            "mean_rgb": (mean_r, mean_g, mean_b),
            "is_flat_default_white": is_flat_white,
        })
    except Exception:
        pass
    return res


def _get_slot_polygon_uvs(obj: bpy.types.Object | None, slot_index: int) -> np.ndarray | None:
    """Returns a sample of (u, v) coordinates (up to 512 points) for polygons assigned to `slot_index`."""
    if obj is None or getattr(obj, "type", None) != "MESH" or getattr(obj, "data", None) is None or slot_index < 0:
        return None
    me = obj.data
    if not me.uv_layers or len(me.polygons) == 0:
        return None
    uv_layer = next((u for u in me.uv_layers if getattr(u, "active_render", False)), None) or me.uv_layers.active or me.uv_layers[0]
    if uv_layer is None or len(uv_layer.data) == 0:
        return None
    uv_list = []
    polys = [p for p in me.polygons if p.material_index == slot_index]
    if not polys:
        return None
    step = max(1, len(polys) // 128)
    for p in polys[::step]:
        for li in p.loop_indices:
            uv = uv_layer.data[li].uv
            uv_list.append((float(uv[0]), float(uv[1])))
            if len(uv_list) >= 512:
                break
        if len(uv_list) >= 512:
            break
    return np.array(uv_list, dtype=np.float32) if uv_list else None


def heal_corrupted_reskate_user_override_nodes_generator(
    mat: bpy.types.Material | None,
    obj: bpy.types.Object | None = None,
    slot_index: int = -1,
    label_prefix: str = "",
    pct_base: float = 0.0,
    pct_span: float = 0.0,
):
    """
    Generator variant of heal_corrupted_reskate_user_override_nodes yielding ProgressStatus
    between override node repairs, Base Color candidate validation, and multi-slot UV checks.
    """
    if mat is None or not getattr(mat, "use_nodes", False) or not getattr(mat, "node_tree", None):
        return False

    if pct_span > 0.0:
        yield ProgressStatus(f"{label_prefix}Validating shader override hierarchy...", round(pct_base + 0.10 * pct_span, 2))

    healed = False
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    has_template = any(n.name.startswith("ReSkate_Template_") for n in nodes)

    p_node = _principled_node(mat)
    if p_node is None:
        return False

    # 1. If this is NOT a ReSkate Starter Template material, check if `ReSkate_User_*` nodes
    # hijacked the Principled BSDF sockets away from the user's real shader nodes (e.g. Sanctus Bake MixRGB/TexImage).
    if not has_template:
        bc_sock = p_node.inputs.get("Base Color")
        if bc_sock is not None and bc_sock.is_linked and bc_sock.links:
            top_n = bc_sock.links[0].from_node
            # Walk past Wear/Decal wrapper nodes if currently active in viewport
            target_sock = bc_sock
            while top_n and top_n.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
                in_s = top_n.inputs[6] if len(top_n.inputs) > 6 else top_n.inputs.get("A")
                if in_s is not None and in_s.is_linked and in_s.links:
                    target_sock = in_s
                    top_n = in_s.links[0].from_node
                else:
                    break
            if top_n is not None and top_n.name in ("ReSkate_User_BaseColorTex", "ReSkate_PBR_BaseColorTex"):
                # Look for an unlinked user MixRGB (e.g. Sanctus Bake BaseColor * AO) or user TEX_IMAGE node
                best_mix = None
                for cand in nodes:
                    if cand.type in ("MIX_RGB", "MIX") and not cand.name.startswith("ReSkate_"):
                        c_img = _linked_image(cand.inputs.get("Color1") or cand.inputs.get("A"), exclude_non_albedo=True)
                        if c_img is not None and not _is_non_albedo_pbr_image_name(getattr(c_img, "name", "")):
                            best_mix = cand
                            break
                if best_mix is not None:
                    out_s = best_mix.outputs.get("Color") or best_mix.outputs.get("Result") or (best_mix.outputs[0] if len(best_mix.outputs) > 0 else None)
                    if out_s is not None:
                        for lk in list(target_sock.links):
                            links.remove(lk)
                        links.new(out_s, target_sock)
                        nodes.remove(top_n)
                        healed = True
                else:
                    user_bc_tex = next(
                        (
                            n for n in nodes
                            if n.type == "TEX_IMAGE"
                            and n != top_n
                            and not n.name.startswith(("ReSkate_", "_ReSkate_"))
                            and getattr(n, "image", None) is not None
                            and not _is_non_albedo_pbr_image_name(getattr(n.image, "name", ""))
                        ),
                        None,
                    )
                    if user_bc_tex is not None and "Color" in user_bc_tex.outputs:
                        for lk in list(target_sock.links):
                            links.remove(lk)
                        links.new(user_bc_tex.outputs["Color"], target_sock)
                        nodes.remove(top_n)
                        healed = True

        # Check Normal, Roughness, Metallic hijacked by ReSkate_User_* when real user nodes exist
        for sock_nm, usr_names in (
            ("Roughness", ("ReSkate_User_RoughnessTex", "ReSkate_PBR_RoughnessTex")),
            ("Metallic", ("ReSkate_User_MetallicTex", "ReSkate_PBR_MetallicTex")),
        ):
            s_in = p_node.inputs.get(sock_nm)
            if s_in is not None and s_in.is_linked and s_in.links:
                fn = s_in.links[0].from_node
                if fn is not None and fn.name in usr_names:
                    fn_img = getattr(fn, "image", None)
                    orig_tex = next(
                        (
                            n for n in nodes
                            if n.type == "TEX_IMAGE"
                            and n != fn
                            and not n.name.startswith(("ReSkate_", "_ReSkate_"))
                            and getattr(n, "image", None) is not None
                            and (getattr(n, "image", None) == fn_img or sock_nm.lower() in str(getattr(n.image, "name", "")).lower())
                        ),
                        None,
                    )
                    if orig_tex is not None and "Color" in orig_tex.outputs:
                        for lk in list(s_in.links):
                            links.remove(lk)
                        links.new(orig_tex.outputs["Color"], s_in)
                        nodes.remove(fn)
                        healed = True

        n_in = p_node.inputs.get("Normal")
        if n_in is not None and n_in.is_linked and n_in.links:
            fn = n_in.links[0].from_node
            if fn is not None and fn.name in ("ReSkate_User_NormalMap", "ReSkate_PBR_NormalMap"):
                orig_nmap = next(
                    (
                        n for n in nodes
                        if n.type == "NORMAL_MAP"
                        and n != fn
                        and not n.name.startswith(("ReSkate_", "_ReSkate_"))
                        and n.inputs.get("Color") is not None
                        and n.inputs["Color"].is_linked
                    ),
                    None,
                )
                if orig_nmap is not None and "Normal" in orig_nmap.outputs:
                    for lk in list(n_in.links):
                        links.remove(lk)
                    links.new(orig_nmap.outputs["Normal"], n_in)
                    for rm_nm in ("ReSkate_User_NormalMap", "ReSkate_User_NormalTex"):
                        rn = nodes.get(rm_nm)
                        if rn is not None:
                            nodes.remove(rn)
                    healed = True

        # Reset accidental `use_custom_texture_overrides` unless the material is using ReSkate_Baked_* maps
        for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
            if holder is not None and bool(getattr(holder, "use_custom_texture_overrides", False)):
                b_im = getattr(holder, "base_color_image", None)
                msk_im = getattr(holder, "mask_image", None)
                op_im = getattr(holder, "opacity_image", None)
                if msk_im is None and op_im is None and not (b_im and str(getattr(b_im, "name", "")).startswith("ReSkate_Baked_")):
                    try:
                        holder.use_custom_texture_overrides = False
                    except Exception:
                        pass

    # 2. Check Base Color TEX_IMAGE node for:
    #    (a) `_AmbientOcclusion` / non-albedo image accidentally assigned to Base Color, OR
    #    (b) Bake-set suffix mismatch where Base Color is on an unbaked/flat white suffix (e.g. `.001`)
    #        while Roughness/Normal is on a newer baked suffix (e.g. `.002`).
    r_img, _ = _scalar_texture_source(p_node.inputs.get("Roughness"))
    n_img = _normal_image(mat)
    ref_bake_info = _parse_bake_prefix_and_suffix(getattr(r_img or n_img, "name", "")) if (r_img or n_img) else None

    # Find the active Base Color TEX_IMAGE node feeding Principled BSDF.Base Color
    bc_tex_node = None
    bc_sock = p_node.inputs.get("Base Color")
    if bc_sock is not None and bc_sock.is_linked:
        pending_s = [bc_sock]
        seen_s = set()
        while pending_s:
            cs = pending_s.pop(0)
            if cs is None or not cs.is_linked or id(cs) in seen_s:
                continue
            seen_s.add(id(cs))
            for lk in cs.links:
                nd = lk.from_node
                if nd is None or nd.name.startswith(("_ReSkate_Temp_Bake", "ReSkate_Wear", "ReSkate_Decal_")):
                    if nd is not None and nd.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
                        in_s = nd.inputs[6] if len(nd.inputs) > 6 else nd.inputs.get("A")
                        if in_s is not None:
                            pending_s.append(in_s)
                    continue
                if nd.type == "TEX_IMAGE":
                    if bc_tex_node is None or not _is_non_albedo_pbr_image_name(getattr(getattr(nd, "image", None), "name", "")):
                        bc_tex_node = nd
                        if not _is_non_albedo_pbr_image_name(getattr(getattr(nd, "image", None), "name", "")):
                            break
                for ch_in in _ordered_node_inputs_for_image_search(nd):
                    if ch_in.is_linked:
                        pending_s.append(ch_in)
            if bc_tex_node is not None and not _is_non_albedo_pbr_image_name(getattr(getattr(bc_tex_node, "image", None), "name", "")):
                break

    slot_uvs = _get_slot_polygon_uvs(obj, slot_index) if (obj is not None and slot_index >= 0) else None

    if bc_tex_node is not None and getattr(bc_tex_node, "image", None) is not None:
        cur_bc_img = bc_tex_node.image
        cur_bc_name = str(getattr(cur_bc_img, "name", "") or "")
        cur_is_non_albedo = _is_non_albedo_pbr_image_name(cur_bc_name)
        cur_bake_info = _parse_bake_prefix_and_suffix(cur_bc_name)
        cur_stats = _sample_blender_image_uv_stats(cur_bc_img, slot_uvs)

        # Check if we need to heal Base Color from a sibling `<prefix>_BaseColor<suffix>` image in `bpy.data.images`
        needs_sibling_bc = cur_is_non_albedo or cur_stats.get("is_flat_default_white", False)
        if not needs_sibling_bc and ref_bake_info is not None and cur_bake_info is not None:
            if ref_bake_info[0] == cur_bake_info[0] and ref_bake_info[2] != cur_bake_info[2]:
                needs_sibling_bc = True

        if needs_sibling_bc:
            prefix = ref_bake_info[0] if ref_bake_info else (cur_bake_info[0] if cur_bake_info else "")
            pref_suf = ref_bake_info[2] if ref_bake_info else ""
            if prefix:
                candidates = []
                exact_name = f"{prefix}_BaseColor{pref_suf}"
                if exact_name in bpy.data.images:
                    candidates.append(bpy.data.images[exact_name])
                for im in bpy.data.images:
                    p_info = _parse_bake_prefix_and_suffix(getattr(im, "name", ""))
                    if p_info and p_info[0] == prefix and p_info[1] in ("basecolor", "base_color", "albedo") and im not in candidates:
                        candidates.append(im)
                for cand_im in candidates:
                    if pct_span > 0.0:
                        yield ProgressStatus(f"{label_prefix}Validating base color candidate '{cand_im.name}'...", round(pct_base + 0.50 * pct_span, 2))
                    c_stats = _sample_blender_image_uv_stats(cand_im, slot_uvs)
                    if c_stats.get("valid") and c_stats.get("non_black_ratio", 0.0) > 0.50 and not c_stats.get("is_flat_default_white", False):
                        if bc_tex_node.image != cand_im:
                            bc_tex_node.image = cand_im
                            cur_bc_img = cand_im
                            healed = True
                        break

    # 3. Multi-slot UV coverage healer: if this slot's BaseColor image has >70% unbaked black background `[0,0,0]`
    #    at this slot's polygon UVs while another slot on the SAME object has painted texture data at this slot's UVs
    #    (e.g. `long_rail_skinny` where Slot 0 had old `Cylinder.001_*` and Slot 1 had unified `Cylinder.004_*`),
    #    update this slot's texture nodes to the unified bake set!
    if obj is not None and slot_index >= 0 and len(obj.material_slots) > 1 and slot_uvs is not None and len(slot_uvs) > 0:
        cur_bc_img = _base_color_image(mat)
        if cur_bc_img is not None:
            st = _sample_blender_image_uv_stats(cur_bc_img, slot_uvs)
            if st.get("valid") and (st.get("non_black_ratio", 1.0) < 0.30 or st.get("is_flat_default_white", False)):
                for other_idx, other_slot in enumerate(obj.material_slots):
                    if other_idx == slot_index or other_slot.material is None:
                        continue
                    other_mat = other_slot.material
                    other_bc = _base_color_image(other_mat)
                    if other_bc is None or other_bc == cur_bc_img:
                        continue
                    o_st = _sample_blender_image_uv_stats(other_bc, slot_uvs)
                    if o_st.get("valid") and o_st.get("non_black_ratio", 0.0) > 0.75 and not o_st.get("is_flat_default_white", False):
                        o_info = _parse_bake_prefix_and_suffix(getattr(other_bc, "name", ""))
                        if o_info:
                            o_prefix, _, o_suf = o_info
                            for n in nodes:
                                if n.type == "TEX_IMAGE" and getattr(n, "image", None) is not None:
                                    n_info = _parse_bake_prefix_and_suffix(getattr(n.image, "name", ""))
                                    if n_info:
                                        _, map_kind, _ = n_info
                                        canon_kind = {
                                            "basecolor": "BaseColor",
                                            "base_color": "BaseColor",
                                            "albedo": "BaseColor",
                                            "roughness": "Roughness",
                                            "metallic": "Metallic",
                                            "normal": "Normal",
                                            "ambientocclusion": "AmbientOcclusion",
                                            "ao": "AmbientOcclusion",
                                        }.get(map_kind, "")
                                        if canon_kind:
                                            target_im = bpy.data.images.get(f"{o_prefix}_{canon_kind}{o_suf}")
                                            if target_im is not None and n.image != target_im:
                                                n.image = target_im
                                                healed = True
                        break

    # 4. Restore float_buffer (`is_float == True`) BaseColor images whose colorspace was set to 'sRGB'
    #    back to 'Linear Rec.709' so Blender's viewport & exporter do not double-darken them!
    final_bc_img = _base_color_image(mat)
    if final_bc_img is not None:
        try:
            cs_now = str(getattr(getattr(final_bc_img, "colorspace_settings", None), "name", "") or "").strip().lower()
            if (
                bool(getattr(final_bc_img, "is_float", False))
                and cs_now == "srgb"
                and (getattr(final_bc_img, "packed_file", None) is not None or _source_file_for_image(final_bc_img) is not None)
            ):
                final_bc_img.colorspace_settings.name = "Linear Rec.709"
                _IMAGE_PIL_CACHE.clear()
                _DETACHED_BUFFER_CACHE.clear()
                healed = True
        except Exception:
            pass

    return healed


def heal_corrupted_reskate_user_override_nodes(
    mat: bpy.types.Material | None,
    obj: bpy.types.Object | None = None,
    slot_index: int = -1,
) -> bool:
    """
    Synchronous wrapper iterating heal_corrupted_reskate_user_override_nodes_generator to completion.
    """
    gen = heal_corrupted_reskate_user_override_nodes_generator(mat, obj=obj, slot_index=slot_index)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def sync_user_texture_slots_to_shader_nodes(
    obj: bpy.types.Object | None,
    mat: bpy.types.Material | None,
    source_settings=None,
):
    """
    Synchronizes user-assigned image textures (`base_color_image`, `normal_image`,
    `roughness_image`, `metallic_image`) on `mat.sk8_material` / `mat.reskate_mat`
    directly into the Blender shader node tree (`Principled BSDF`), replacing any
    Starter Material Template procedural nodes (`ReSkate_Template_*`) for those specific
    slots while keeping Procedural Edge Wear (`ReSkate_Wear_*`) layered on top!
    """
    if mat is None:
        return
    ms = source_settings or getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    if ms is None:
        return

    # Keep sk8_material and reskate_mat pointers in sync without triggering recursive update callbacks
    prev_sync_flag = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    properties._IN_MAT_TEX_SYNC = True
    try:
        for other in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
            if other is not None and other != ms:
                for attr in ("base_color_image", "normal_image", "roughness_image", "metallic_image", "mask_image", "opacity_image", "emission_image"):
                    if hasattr(ms, attr) and hasattr(other, attr):
                        val = getattr(ms, attr, None)
                        if not _is_internal_wear_or_bake_image(val) and getattr(other, attr, None) != val:
                            try:
                                setattr(other, attr, val)
                            except Exception:
                                pass
    finally:
        properties._IN_MAT_TEX_SYNC = prev_sync_flag

    mat.use_nodes = True
    if not mat.node_tree:
        return

    # Temporarily unwrap ReSkate_Wear_* and ReSkate_Decal_* nodes so we wire directly to Principled BSDF
    remove_procedural_wear_nodes_from_material(mat)
    remove_projected_decal_nodes_from_material(mat)

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    p_node = _principled_node(mat)
    if p_node is None:
        p_node = nodes.new("ShaderNodeBsdfPrincipled")
        p_node.location = (0, 0)

    bc_img = getattr(ms, "base_color_image", None)
    if _is_internal_wear_or_bake_image(bc_img):
        bc_img = None
    norm_img = getattr(ms, "normal_image", None)
    if _is_internal_wear_or_bake_image(norm_img):
        norm_img = None
    rough_img = getattr(ms, "roughness_image", None)
    if _is_internal_wear_or_bake_image(rough_img):
        rough_img = None
    metal_img = getattr(ms, "metallic_image", None)
    if _is_internal_wear_or_bake_image(metal_img):
        metal_img = None

    # 1. Base Color slot
    bc_sock = p_node.inputs.get("Base Color")
    if bc_sock is not None:
        cur_bc_node = bc_sock.links[0].from_node if (bc_sock.is_linked and bc_sock.links) else None
        if bc_img is not None:
            # If current link is empty, a template node, or our user tex node, wire ReSkate_User_BaseColorTex.
            # If the user already has a custom TEX_IMAGE node connected directly, update its image in-place!
            is_template_or_user = (
                cur_bc_node is None
                or cur_bc_node.name.startswith(("ReSkate_Template_", "ReSkate_User_", "ReSkate_PBR_"))
            )
            if is_template_or_user:
                u_bc = nodes.get("ReSkate_User_BaseColorTex")
                if u_bc is None or u_bc.type != "TEX_IMAGE":
                    u_bc = nodes.new("ShaderNodeTexImage")
                    u_bc.name = "ReSkate_User_BaseColorTex"
                    u_bc.label = "User Base Color Texture"
                    u_bc.location = (p_node.location.x - 560, p_node.location.y + 260)
                u_bc.image = bc_img
                for lk in list(bc_sock.links):
                    links.remove(lk)
                links.new(u_bc.outputs["Color"], bc_sock)
                # If this material is marked as a Decal Overlay, also link Alpha to Principled BSDF.Alpha!
                if bool(getattr(ms, "is_decal_slot", False)) or str(getattr(ms, "domain", "surface")) == "decal":
                    a_sock = p_node.inputs.get("Alpha")
                    if a_sock is not None and not a_sock.is_linked and "Alpha" in u_bc.outputs:
                        links.new(u_bc.outputs["Alpha"], a_sock)
                    try:
                        if hasattr(mat, "surface_render_method"):
                            mat.surface_render_method = 'BLENDED'
                        elif hasattr(mat, "blend_method"):
                            mat.blend_method = 'HASHED'
                    except Exception:
                        pass
            elif cur_bc_node.type == "TEX_IMAGE" and getattr(cur_bc_node, "image", None) != bc_img:
                cur_bc_node.image = bc_img
        elif cur_bc_node is not None and cur_bc_node.name in ("ReSkate_User_BaseColorTex", "ReSkate_PBR_BaseColorTex"):
            nodes.remove(cur_bc_node)
            tpl_ramp = nodes.get("ReSkate_Template_ColorRamp")
            if tpl_ramp is not None and "Color" in tpl_ramp.outputs:
                links.new(tpl_ramp.outputs["Color"], bc_sock)

    # Also ensure existing Base Color texture node has Alpha linked when user checks 'Treat Material as Decal Overlay'
    if bool(getattr(ms, "is_decal_slot", False)) or str(getattr(ms, "domain", "surface")) == "decal":
        a_sock = p_node.inputs.get("Alpha")
        if a_sock is not None and not a_sock.is_linked and bc_sock is not None and bc_sock.is_linked:
            bc_from = bc_sock.links[0].from_node
            if bc_from is not None and bc_from.type == "TEX_IMAGE" and "Alpha" in bc_from.outputs:
                links.new(bc_from.outputs["Alpha"], a_sock)
        try:
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = 'BLENDED'
            elif hasattr(mat, "blend_method"):
                mat.blend_method = 'HASHED'
        except Exception:
            pass

    # 2. Normal slot
    n_sock = p_node.inputs.get("Normal")
    if n_sock is not None:
        cur_n_node = n_sock.links[0].from_node if (n_sock.is_linked and n_sock.links) else None
        if norm_img is not None:
            is_template_or_user = (
                cur_n_node is None
                or cur_n_node.name.startswith(("ReSkate_Template_", "ReSkate_User_", "ReSkate_PBR_"))
            )
            if is_template_or_user:
                u_ntex = nodes.get("ReSkate_User_NormalTex")
                if u_ntex is None or u_ntex.type != "TEX_IMAGE":
                    u_ntex = nodes.new("ShaderNodeTexImage")
                    u_ntex.name = "ReSkate_User_NormalTex"
                    u_ntex.label = "User Normal Texture"
                    u_ntex.location = (p_node.location.x - 780, p_node.location.y - 160)
                u_ntex.image = norm_img
                try:
                    if (
                        getattr(norm_img, "source", "") != 'GENERATED'
                        and (getattr(norm_img, "packed_file", None) is not None or _source_file_for_image(norm_img) is not None)
                        and norm_img.colorspace_settings.name != 'Non-Color'
                    ):
                        norm_img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                u_nmap = nodes.get("ReSkate_User_NormalMap")
                if u_nmap is None or u_nmap.type != "NORMAL_MAP":
                    u_nmap = nodes.new("ShaderNodeNormalMap")
                    u_nmap.name = "ReSkate_User_NormalMap"
                    u_nmap.label = "User Normal Map"
                    u_nmap.location = (p_node.location.x - 460, p_node.location.y - 160)
                for lk in list(u_nmap.inputs["Color"].links):
                    links.remove(lk)
                links.new(u_ntex.outputs["Color"], u_nmap.inputs["Color"])
                for lk in list(n_sock.links):
                    links.remove(lk)
                links.new(u_nmap.outputs["Normal"], n_sock)
        elif cur_n_node is not None and cur_n_node.name in ("ReSkate_User_NormalMap", "ReSkate_PBR_NormalMap"):
            for rm_nm in ("ReSkate_User_NormalMap", "ReSkate_User_NormalTex"):
                rn = nodes.get(rm_nm)
                if rn is not None:
                    nodes.remove(rn)
            tpl_bump = nodes.get("ReSkate_Template_Bump")
            if tpl_bump is not None and "Normal" in tpl_bump.outputs:
                links.new(tpl_bump.outputs["Normal"], n_sock)

    # 3. Roughness slot
    r_sock = p_node.inputs.get("Roughness")
    if r_sock is not None:
        cur_r_node = r_sock.links[0].from_node if (r_sock.is_linked and r_sock.links) else None
        if rough_img is not None:
            is_template_or_user = (
                cur_r_node is None
                or cur_r_node.name.startswith(("ReSkate_Template_", "ReSkate_User_", "ReSkate_PBR_"))
            )
            if is_template_or_user:
                u_rtex = nodes.get("ReSkate_User_RoughnessTex")
                if u_rtex is None or u_rtex.type != "TEX_IMAGE":
                    u_rtex = nodes.new("ShaderNodeTexImage")
                    u_rtex.name = "ReSkate_User_RoughnessTex"
                    u_rtex.label = "User Roughness Texture"
                    u_rtex.location = (p_node.location.x - 560, p_node.location.y + 40)
                u_rtex.image = rough_img
                try:
                    if (
                        getattr(rough_img, "source", "") != 'GENERATED'
                        and (getattr(rough_img, "packed_file", None) is not None or _source_file_for_image(rough_img) is not None)
                        and rough_img.colorspace_settings.name != 'Non-Color'
                    ):
                        rough_img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                for lk in list(r_sock.links):
                    links.remove(lk)
                links.new(u_rtex.outputs["Color"], r_sock)
            elif cur_r_node.type == "TEX_IMAGE" and getattr(cur_r_node, "image", None) != rough_img:
                cur_r_node.image = rough_img
        elif cur_r_node is not None and cur_r_node.name in ("ReSkate_User_RoughnessTex", "ReSkate_PBR_RoughnessTex"):
            nodes.remove(cur_r_node)

    # 4. Metallic slot
    m_sock = p_node.inputs.get("Metallic")
    if m_sock is not None:
        cur_m_node = m_sock.links[0].from_node if (m_sock.is_linked and m_sock.links) else None
        if metal_img is not None:
            is_template_or_user = (
                cur_m_node is None
                or cur_m_node.name.startswith(("ReSkate_Template_", "ReSkate_User_", "ReSkate_PBR_"))
            )
            if is_template_or_user:
                u_mtex = nodes.get("ReSkate_User_MetallicTex")
                if u_mtex is None or u_mtex.type != "TEX_IMAGE":
                    u_mtex = nodes.new("ShaderNodeTexImage")
                    u_mtex.name = "ReSkate_User_MetallicTex"
                    u_mtex.label = "User Metallic Texture"
                    u_mtex.location = (p_node.location.x - 560, p_node.location.y - 60)
                u_mtex.image = metal_img
                try:
                    if (
                        getattr(metal_img, "source", "") != 'GENERATED'
                        and (getattr(metal_img, "packed_file", None) is not None or _source_file_for_image(metal_img) is not None)
                        and metal_img.colorspace_settings.name != 'Non-Color'
                    ):
                        metal_img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                for lk in list(m_sock.links):
                    links.remove(lk)
                links.new(u_mtex.outputs["Color"], m_sock)
            elif cur_m_node.type == "TEX_IMAGE" and getattr(cur_m_node, "image", None) != metal_img:
                cur_m_node.image = metal_img
        elif cur_m_node is not None and cur_m_node.name in ("ReSkate_User_MetallicTex", "ReSkate_PBR_MetallicTex"):
            nodes.remove(cur_m_node)

    # Update _LAST_SK8_MAT_IMG_SIGS so depsgraph handler knows this state is synced
    sk8_h = getattr(mat, "sk8_material", None)
    if sk8_h is not None and hasattr(properties, "_LAST_SK8_MAT_IMG_SIGS"):
        properties._LAST_SK8_MAT_IMG_SIGS[mat.name] = tuple(
            getattr(getattr(sk8_h, k, None), "name", "")
            for k in ("base_color_image", "normal_image", "roughness_image", "metallic_image", "mask_image", "opacity_image", "emission_image")
        )

    # Re-layer Procedural Edge Wear on top of the updated shader graph!
    target_obj = obj
    if target_obj is None and bpy.context is not None:
        target_obj = resolve_active_prop_mesh(bpy.context)
    if target_obj is not None:
        scn = getattr(bpy.context, "scene", None) if bpy.context else None
        prop = getattr(scn, "reskate_prop", None) if scn else None
        sync_procedural_wear_to_viewport(target_obj, prop=prop)

    invalidate_roughness_summary_cache(mat.name)


def _emission_image_and_strength(mat: bpy.types.Material | None) -> tuple[bpy.types.Image | None, tuple[float, float, float], float]:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None, (0.0, 0.0, 0.0), 0.0
    p = _principled_node(mat)
    if p is not None:
        em_sock = p.inputs.get("Emission Color") or p.inputs.get("Emission")
        str_sock = p.inputs.get("Emission Strength")
        strength = float(str_sock.default_value) if str_sock is not None else 0.0
        em_col = (0.0, 0.0, 0.0)
        if em_sock is not None and hasattr(em_sock, "default_value"):
            dv = em_sock.default_value
            em_col = (float(dv[0]), float(dv[1]), float(dv[2]))
        em_img = _linked_image(em_sock) if em_sock is not None else None
        if em_img is not None or (strength > 0.001 and max(em_col) > 0.001):
            return em_img, em_col, (strength if strength > 0.0 else 1.0)
    # Also check standalone ShaderNodeEmission
    em_node = next((n for n in mat.node_tree.nodes if n.type == "EMISSION"), None)
    if em_node is not None:
        c_sock = em_node.inputs.get("Color")
        s_sock = em_node.inputs.get("Strength")
        strength = float(s_sock.default_value) if s_sock is not None else 1.0
        em_col = (1.0, 1.0, 1.0)
        if c_sock is not None and hasattr(c_sock, "default_value"):
            dv = c_sock.default_value
            em_col = (float(dv[0]), float(dv[1]), float(dv[2]))
        em_img = _linked_image(c_sock) if c_sock is not None else None
        return em_img, em_col, strength
    return None, (0.0, 0.0, 0.0), 0.0


def inspect_material_shader_nodes(mat: bpy.types.Material | None) -> dict:
    """
    Inspects a Blender Material's Principled BSDF (including Separate Color / RGB nodes,
    procedural Noise/ColorRamp/Bump nodes, and Emission inputs) and returns all live shader bindings.
    """
    p = _principled_node(mat)
    base_img = _base_color_image(mat)
    norm_img = _normal_image(mat)
    op_img = _opacity_image(mat)
    rough_img, rough_chan = _scalar_texture_source(p.inputs.get("Roughness")) if p else (None, "Color")
    metal_img, metal_chan = _scalar_texture_source(p.inputs.get("Metallic")) if p else (None, "Color")
    em_img, em_col, em_str = _emission_image_and_strength(mat)

    has_proc_base = bool(
        p
        and "Base Color" in p.inputs
        and p.inputs["Base Color"].is_linked
        and base_img is None
        and not p.inputs["Base Color"].links[0].from_node.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_"))
    )
    has_proc_norm = bool(
        p
        and "Normal" in p.inputs
        and p.inputs["Normal"].is_linked
        and norm_img is None
        and not p.inputs["Normal"].links[0].from_node.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_"))
    )

    rough_val = float(p.inputs["Roughness"].default_value) if (p and "Roughness" in p.inputs) else 0.45
    if p and "Roughness" in p.inputs and p.inputs["Roughness"].is_linked and p.inputs["Roughness"].links:
        rn = p.inputs["Roughness"].links[0].from_node
        while rn and rn.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
            in_s = (rn.inputs[2] if len(rn.inputs) > 2 else rn.inputs.get("A")) if rn.name.startswith("ReSkate_Decal_") else (rn.inputs[0] if len(rn.inputs) > 0 else None)
            if in_s is not None and in_s.is_linked and in_s.links:
                rn = in_s.links[0].from_node
            else:
                if in_s is not None and hasattr(in_s, "default_value"):
                    rough_val = float(in_s.default_value)
                rn = None
        if rn and rn.name == "ReSkate_Roughness_Mix" and "reskate_base_val" in rn:
            rough_val = float(rn["reskate_base_val"])
    metal_val = float(p.inputs["Metallic"].default_value) if (p and "Metallic" in p.inputs) else 0.0
    if p and "Metallic" in p.inputs and p.inputs["Metallic"].is_linked and p.inputs["Metallic"].links:
        mn = p.inputs["Metallic"].links[0].from_node
        while mn and mn.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
            in_s = (mn.inputs[2] if len(mn.inputs) > 2 else mn.inputs.get("A")) if mn.name.startswith("ReSkate_Decal_") else (mn.inputs[0] if len(mn.inputs) > 0 else None)
            if in_s is not None and in_s.is_linked and in_s.links:
                mn = in_s.links[0].from_node
            else:
                if in_s is not None and hasattr(in_s, "default_value"):
                    metal_val = float(in_s.default_value)
                mn = None
        if mn and mn.name == "ReSkate_Metallic_Mix" and "reskate_base_val" in mn:
            metal_val = float(mn["reskate_base_val"])
    base_col = (0.78, 0.78, 0.78)
    if p and "Base Color" in p.inputs:
        bc_sock = p.inputs["Base Color"]
        while bc_sock and bc_sock.is_linked and bc_sock.links and bc_sock.links[0].from_node.name.startswith(("ReSkate_Wear_", "ReSkate_Decal_")):
            bn = bc_sock.links[0].from_node
            bc_sock = bn.inputs[6] if len(bn.inputs) > 6 else bn.inputs.get("A")
        if bc_sock is not None and hasattr(bc_sock, "default_value"):
            dv = bc_sock.default_value
            base_col = (float(dv[0]), float(dv[1]), float(dv[2]))

    return {
        "has_principled": p is not None,
        "base_color_image": base_img,
        "has_procedural_base": has_proc_base,
        "base_color": base_col,
        "normal_image": norm_img,
        "has_procedural_normal": has_proc_norm,
        "roughness_image": rough_img,
        "roughness_channel": rough_chan,
        "roughness_value": rough_val,
        "metallic_image": metal_img,
        "metallic_channel": metal_chan,
        "metallic_value": metal_val,
        "opacity_image": op_img,
        "emission_image": em_img,
        "emission_color": em_col,
        "emission_strength": em_str,
    }


def sync_material_from_shader_nodes(
    mat: bpy.types.Material | None,
    force: bool = False,
    obj: bpy.types.Object | None = None,
    slot_index: int = -1,
    heal: bool = False,
    **kwargs,
) -> dict:
    """
    Populates `mat.sk8_material` (and `mat.reskate_mat`) slots directly from the
    Blender Principled BSDF + Separate Color RGB node tree unless manual overrides are enabled.
    When `heal=True` (compile/explicit sync), first repairs any corrupted `ReSkate_User_*` override nodes.
    """
    if mat is not None and heal:
        heal_corrupted_reskate_user_override_nodes(mat, obj=obj, slot_index=slot_index)

    if mat is not None and obj is not None and slot_index >= 0:
        surf_info = get_synced_material_surface_info(obj, slot_index, mat)
        if not surf_info.get("override_audio", False):
            target_id = f"material_{int(surf_info['packed']):04d}"
            for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
                if holder is not None and hasattr(holder, "collision_material"):
                    try:
                        if getattr(holder, "collision_material", None) != target_id:
                            holder.collision_material = target_id
                    except Exception:
                        pass

    def _mat_node_sig(m):
        nt_m = m.node_tree if getattr(m, "use_nodes", False) else None
        if nt_m is None:
            return (m.name, 0, 0, ())
        tex_imgs = tuple(
            (n.name, getattr(getattr(n, "image", None), "name", ""))
            for n in nt_m.nodes
            if getattr(n, "type", "") == "TEX_IMAGE"
        )
        return (m.name, len(nt_m.nodes), len(nt_m.links), tex_imgs)

    if mat is not None and not force and not heal:
        sig = _mat_node_sig(mat)
        cached_mat = _SYNCED_MAT_NODE_SIGS.get(mat.name)
        if cached_mat is not None and cached_mat[0] == sig:
            return dict(cached_mat[1])

    info = inspect_material_shader_nodes(mat)
    if mat is None:
        return info

    ext = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    if not force and ext is not None and bool(getattr(ext, "use_custom_texture_overrides", False)):
        sig = _mat_node_sig(mat)
        _SYNCED_MAT_NODE_SIGS[mat.name] = (sig, dict(info))
        return info

    prev_sync = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    properties._IN_MAT_TEX_SYNC = True
    try:
        for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
            if holder is None:
                continue
            try:
                if hasattr(holder, "base_color_image") and getattr(holder, "base_color_image", None) != info["base_color_image"]:
                    holder.base_color_image = info["base_color_image"]
                if hasattr(holder, "normal_image") and getattr(holder, "normal_image", None) != info["normal_image"]:
                    holder.normal_image = info["normal_image"]
                if hasattr(holder, "roughness_image") and getattr(holder, "roughness_image", None) != info["roughness_image"]:
                    holder.roughness_image = info["roughness_image"]
                if hasattr(holder, "roughness_channel") and info["roughness_channel"] and getattr(holder, "roughness_channel", None) != info["roughness_channel"]:
                    holder.roughness_channel = info["roughness_channel"]
                if hasattr(holder, "metallic_image") and getattr(holder, "metallic_image", None) != info["metallic_image"]:
                    holder.metallic_image = info["metallic_image"]
                if hasattr(holder, "metallic_channel") and info["metallic_channel"] and getattr(holder, "metallic_channel", None) != info["metallic_channel"]:
                    holder.metallic_channel = info["metallic_channel"]
                if hasattr(holder, "opacity_image") and info["opacity_image"] is not None and getattr(holder, "opacity_image", None) != info["opacity_image"]:
                    holder.opacity_image = info["opacity_image"]
                if hasattr(holder, "emission_image") and getattr(holder, "emission_image", None) != info["emission_image"]:
                    holder.emission_image = info["emission_image"]
                if hasattr(holder, "emission_color"):
                    cur_ec = getattr(holder, "emission_color", None)
                    new_ec = info["emission_color"]
                    if cur_ec is None or tuple(round(float(x), 4) for x in cur_ec[:3]) != tuple(round(float(x), 4) for x in new_ec[:3]):
                        holder.emission_color = new_ec
                if hasattr(holder, "emission_strength"):
                    if abs(float(getattr(holder, "emission_strength", 0.0) or 0.0) - float(info["emission_strength"])) > 1e-5:
                        holder.emission_strength = float(info["emission_strength"])
            except Exception:
                pass
        sk8_h = getattr(mat, "sk8_material", None)
        if sk8_h is not None and hasattr(properties, "_LAST_SK8_MAT_IMG_SIGS"):
            properties._LAST_SK8_MAT_IMG_SIGS[mat.name] = tuple(
                getattr(getattr(sk8_h, k, None), "name", "")
                for k in ("base_color_image", "normal_image", "roughness_image", "metallic_image", "mask_image", "opacity_image", "emission_image")
            )
    finally:
        properties._IN_MAT_TEX_SYNC = prev_sync

    sig = _mat_node_sig(mat)
    if len(_SYNCED_MAT_NODE_SIGS) > 256:
        _SYNCED_MAT_NODE_SIGS.clear()
    _SYNCED_MAT_NODE_SIGS[mat.name] = (sig, dict(info))

    return info


def _source_file_for_image(img: bpy.types.Image | None) -> str | None:
    if img is None:
        return None
    for attr in ("filepath_from_user",):
        try:
            p = getattr(img, attr)()
            if p and os.path.isfile(bpy.path.abspath(p)):
                return bpy.path.abspath(p)
        except Exception:
            pass
    for raw in (getattr(img, "filepath_raw", ""), getattr(img, "filepath", "")):
        if raw:
            try:
                p = bpy.path.abspath(raw)
                if os.path.isfile(p):
                    return p
            except Exception:
                pass
    return None


def _linear_to_srgb_np(rgb: np.ndarray) -> np.ndarray:
    """Converts linear-light RGB float32 values [0..1] to standard IEC 61966-2-1 sRGB [0..1]."""
    lin = np.clip(rgb, 0.0, 1.0)
    return np.where(
        lin <= 0.0031308,
        lin * 12.92,
        1.055 * np.power(lin, 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def _blender_image_to_pil_generator(
    img: bpy.types.Image | None,
    staging_dir: Path | None = None,
    is_color: bool = True,
    label_prefix: str = "",
    pct_val: float = 0.0,
):
    """
    Generator variant of _blender_image_to_pil yielding ProgressStatus
    between pixel reading (C-API foreach_get) and NumPy/PIL conversion.
    Reliably converts any Blender `bpy.types.Image` (including packed GLB textures,
    external files, or generated/baked buffers) into a top-down RGBA PIL.Image.
    Automatically converts Linear Rec.709 / ACEScg / float_buffer color textures
    (such as Sanctus Bake or Cycles `BaseColor` maps) into standard sRGB, and heals
    float_buffer images whose colorspace was accidentally set to 'sRGB' back to
    'Linear Rec.709' so Frostbite's `BC7_UNORM_SRGB` sampler never double-darkens them.
    """
    if img is None:
        return None

    cs_name = str(getattr(getattr(img, "colorspace_settings", None), "name", "") or "").strip().lower()
    img_nm_low = str(getattr(img, "name", "") or "").lower()
    is_non_color = (
        (not is_color)
        or any(k in cs_name for k in ("non-color", "raw", "data"))
        or any(img_nm_low.endswith(suf) for suf in ("_roughness", "_metallic", "_normal", "_n", "_msk", "_orm"))
    )
    is_float_buf = bool(getattr(img, "is_float", False))

    # If a float_buffer color image (like Sanctus Bake `BaseColor` created with `float_buffer=True`)
    # had its colorspace mutated to 'sRGB' while backed by a packed PNG or disk file, restore 'Linear Rec.709'
    # so Blender's OpenColorIO does not apply a second sRGB->Linear curve on top of the linear PNG bytes!
    if (
        (not is_non_color)
        and is_float_buf
        and cs_name == "srgb"
        and (getattr(img, "packed_file", None) is not None or _source_file_for_image(img) is not None)
    ):
        try:
            img.colorspace_settings.name = "Linear Rec.709"
            cs_name = "linear rec.709"
        except Exception:
            pass

    is_linear_color = (not is_non_color) and any(
        k in cs_name for k in ("linear", "rec.709", "rec709", "aces", "scene_linear", "filmic", "agx")
    )
    is_dirty_buf = bool(getattr(img, "is_dirty", False)) or str(getattr(img, "source", "")).upper() == "GENERATED"

    # Non-dirty packed float images (e.g. Cylinder.009_Metallic.002) are eligible for _IMAGE_PIL_CACHE
    is_dynamic = _is_internal_wear_or_bake_image(img) or is_dirty_buf
    cache_key = None
    if not is_dynamic:
        try:
            cache_key = (
                str(getattr(img, "name", "")),
                int(img.size[0]),
                int(img.size[1]),
                str(getattr(img, "filepath_raw", "") or ""),
                cs_name,
                is_float_buf,
                bool(is_color),
            )
            if cache_key in _IMAGE_PIL_CACHE:
                return _IMAGE_PIL_CACHE[cache_key].copy()
        except Exception:
            cache_key = None

    def _apply_linear_to_srgb_pil(pil_rgba):
        if pil_rgba is None or (not is_linear_color and not is_float_buf) or is_non_color:
            return pil_rgba
        arr_f = np.asarray(pil_rgba.convert("RGBA"), dtype=np.float32) / 255.0
        arr_f[:, :, :3] = _linear_to_srgb_np(arr_f[:, :, :3])
        u8_c = np.clip(np.round(arr_f * 255.0), 0, 255).astype(np.uint8)
        return Image.fromarray(u8_c, mode="RGBA")

    def _store_and_return(pil_res):
        if pil_res is not None and cache_key is not None:
            if len(_IMAGE_PIL_CACHE) > 96:
                _IMAGE_PIL_CACHE.clear()
            _IMAGE_PIL_CACHE[cache_key] = pil_res.copy()
        return pil_res

    def _read_from_blender_pixels_gen():
        try:
            w, h = int(img.size[0]), int(img.size[1])
            ch = int(getattr(img, "channels", 4) or 4)
            if w > 0 and h > 0 and len(img.pixels) == w * h * ch:
                if pct_val > 0.0:
                    yield ProgressStatus(f"{label_prefix}Reading pixel buffer for '{img.name}'...", round(pct_val, 2))
                buf = np.empty(w * h * ch, dtype=np.float32)
                img.pixels.foreach_get(buf)

                if pct_val > 0.0:
                    yield ProgressStatus(f"{label_prefix}Reshaping image channels for '{img.name}'...", round(pct_val, 2))
                arr = buf.reshape((h, w, ch))[::-1, :, :].copy()
                del buf

                # When reading `img.pixels` in Blender for a Linear or float_buffer color image,
                # Blender stores scene-linear RGB floats in `img.pixels`. Convert to sRGB for PNG export!
                if (not is_non_color) and (is_float_buf or is_linear_color) and ch >= 3:
                    if pct_val > 0.0:
                        yield ProgressStatus(f"{label_prefix}Converting float colorspace to sRGB for '{img.name}'...", round(pct_val, 2))
                    if h >= 2048:
                        chunk_size = max(512, h // 4)
                        for r_start in range(0, h, chunk_size):
                            r_end = min(h, r_start + chunk_size)
                            arr[r_start:r_end, :, :3] = _linear_to_srgb_np(arr[r_start:r_end, :, :3])
                            if pct_val > 0.0 and r_end < h:
                                yield ProgressStatus(f"{label_prefix}Converting float colorspace to sRGB ({r_end}/{h}) for '{img.name}'...", round(pct_val, 2))
                    else:
                        arr[:, :, :3] = _linear_to_srgb_np(arr[:, :, :3])

                if pct_val > 0.0:
                    yield ProgressStatus(f"{label_prefix}Quantizing image channels for '{img.name}'...", round(pct_val, 2))
                u8 = np.clip(np.round(arr * 255.0), 0, 255).astype(np.uint8)
                del arr

                if pct_val > 0.0:
                    yield ProgressStatus(f"{label_prefix}Constructing image surface for '{img.name}'...", round(pct_val, 2))
                if ch == 4:
                    return _store_and_return(Image.fromarray(u8, mode="RGBA"))
                elif ch == 3:
                    rgba = np.dstack([u8, np.full((h, w), 255, dtype=np.uint8)])
                    return _store_and_return(Image.fromarray(rgba, mode="RGBA"))
                elif ch == 1:
                    g = u8[:, :, 0]
                    rgba = np.dstack([g, g, g, np.full((h, w), 255, dtype=np.uint8)])
                    return _store_and_return(Image.fromarray(rgba, mode="RGBA"))
        except Exception:
            pass
        return None

    # If the image is an unsaved/dirty or float or Linear Rec.709 bake buffer in memory (e.g. Sanctus Bake / Cycles Bake),
    # read directly from `img.pixels` FIRST so stale disk files are never used and Linear -> sRGB is applied cleanly!
    if is_dirty_buf or is_float_buf or is_linear_color:
        pil_mem = yield from _read_from_blender_pixels_gen()
        if pil_mem is not None:
            return pil_mem

    # 1. Packed image data in memory (standard for imported GLBs!)
    try:
        packed = getattr(img, "packed_file", None)
        if packed is not None and getattr(packed, "data", None):
            raw_bytes = bytes(packed.data)
            if len(raw_bytes) > 16:
                if pct_val > 0.0:
                    yield ProgressStatus(f"{label_prefix}Unpacking image data for '{img.name}'...", round(pct_val, 2))
                pil_packed = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
                return _store_and_return(_apply_linear_to_srgb_pil(pil_packed))
    except Exception:
        pass

    # 2. External file on disk
    src_file = _source_file_for_image(img)
    if src_file:
        try:
            if pct_val > 0.0:
                yield ProgressStatus(f"{label_prefix}Loading image from disk for '{img.name}'...", round(pct_val, 2))
            pil_disk = Image.open(src_file).convert("RGBA")
            return _store_and_return(_apply_linear_to_srgb_pil(pil_disk))
        except Exception:
            pass

    # 3. Direct Blender pixel buffer fallback
    pil_px = yield from _read_from_blender_pixels_gen()
    if pil_px is not None:
        return pil_px

    # 4. Fallback `save(filepath=...)` to staging temp file
    if staging_dir is not None:
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            tmp_dst = staging_dir / f"_tmp_extract_{os.getpid()}.png"
            tmp = img.copy()
            try:
                tmp.file_format = "PNG"
                try:
                    tmp.save(filepath=str(tmp_dst))
                except TypeError:
                    tmp.filepath_raw = str(tmp_dst)
                    tmp.save()
            finally:
                bpy.data.images.remove(tmp)
            if tmp_dst.exists() and tmp_dst.stat().st_size > 64:
                pil_im = Image.open(tmp_dst).convert("RGBA")
                try:
                    tmp_dst.unlink()
                except Exception:
                    pass
                return _store_and_return(_apply_linear_to_srgb_pil(pil_im))
        except Exception:
            pass

    return None


def _blender_image_to_pil(
    img: bpy.types.Image | None,
    staging_dir: Path | None = None,
    is_color: bool = True,
):
    """
    Reliably converts any Blender `bpy.types.Image` into a top-down RGBA PIL.Image.
    Synchronous backwards-compatible wrapper delegating to _blender_image_to_pil_generator.
    """
    gen = _blender_image_to_pil_generator(img, staging_dir=staging_dir, is_color=is_color)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


_eval_socket_map = _evaluate_scalar_socket_2d


def _image_stem(img: bpy.types.Image | None) -> str:
    if img is None:
        return ""
    name = getattr(img, "name", "") or ""
    return os.path.splitext(os.path.basename(name))[0].lower().strip()


def _extract_channel_map_generator(
    pil_img,
    channel: str = "Color",
    size: tuple[int, int] = (1024, 1024),
    image_name: str = "",
    return_channel_used: bool = False,
    label_prefix: str = "",
    pct_val: float = 0.0,
):
    """
    Generator variant of _extract_channel_map yielding ProgressStatus between
    Pillow image resizing and NumPy float32 channel slicing to keep main thread <250ms.
    Memoizes in _CHANNEL_MAP_CACHE for zero-cost repeat evaluations across props.
    """
    cache_key = (
        str(image_name or id(pil_img)),
        str(channel or "Color").capitalize(),
        int(size[0]),
        int(size[1]),
    )
    if cache_key in _CHANNEL_MAP_CACHE:
        cached_res, cached_ch = _CHANNEL_MAP_CACHE[cache_key]
        if return_channel_used:
            return cached_res.copy(), cached_ch
        return cached_res.copy()

    out_ch = str(channel or "Color").capitalize()
    if pct_val > 0.0:
        yield ProgressStatus(f"{label_prefix}Extracting '{out_ch}' for '{image_name or 'texture'}'...", round(pct_val, 2))

    if pil_img.mode == "RGBA" and pil_img.size == size:
        resized = pil_img
    elif pil_img.mode == "RGBA":
        resized = pil_img.resize(size)
    elif pil_img.size == size:
        resized = pil_img.convert("RGBA")
    else:
        resized = pil_img.convert("RGBA").resize(size)

    if pct_val > 0.0:
        yield ProgressStatus(f"{label_prefix}Quantizing '{image_name or 'texture'}' channel buffer...", round(pct_val, 2))

    arr = np.asarray(resized, dtype=np.float32) / 255.0
    used_ch = out_ch
    if out_ch == "Red":
        res = arr[:, :, 0]
    elif out_ch == "Green":
        res = arr[:, :, 1]
    elif out_ch == "Blue":
        res = arr[:, :, 2]
    elif out_ch == "Alpha":
        res = arr[:, :, 3]
    else:
        # Check for ORM single-varying-channel map (2 constant channels with std < 1e-3, 1 varying channel with std >= 1e-3)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            sub_arr = arr[::4, ::4, :] if (arr.shape[0] >= 512 and arr.shape[1] >= 512) else arr
            stds = [float(np.std(sub_arr[:, :, i])) for i in range(3)]
            ch_names = ("Red", "Green", "Blue")
            varying = [(idx, ch_names[idx], stds[idx]) for idx in range(3) if stds[idx] >= 1e-3]
            constants = [idx for idx in range(3) if stds[idx] < 1e-3]
            if len(varying) == 1 and len(constants) == 2:
                v_idx, v_name, v_std = varying[0]
                used_ch = v_name
                res = arr[:, :, v_idx]
                log_key = f"{image_name or 'unnamed'}:{v_name}"
                if log_key not in _LOGGED_ORM_AUTODETECT:
                    _LOGGED_ORM_AUTODETECT.add(log_key)
                    print(
                        f"[PBR Channel Auto-Detect] Image '{image_name or 'Texture'}' has 1 varying channel "
                        f"({v_name}, std={v_std:.4f}) and 2 constant channels; extracting {v_name} directly.",
                        flush=True,
                    )
            else:
                res = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        else:
            res = arr[:, :, 0] if arr.ndim == 3 else arr

    if len(_CHANNEL_MAP_CACHE) > 128:
        _CHANNEL_MAP_CACHE.clear()
    _CHANNEL_MAP_CACHE[cache_key] = (res.copy(), used_ch)

    if return_channel_used:
        return res, used_ch
    return res


def _extract_channel_map(
    pil_img,
    channel: str = "Color",
    size: tuple[int, int] = (1024, 1024),
    image_name: str = "",
    return_channel_used: bool = False,
):
    """
    Synchronous wrapper iterating _extract_channel_map_generator to completion.
    """
    gen = _extract_channel_map_generator(
        pil_img,
        channel=channel,
        size=size,
        image_name=image_name,
        return_channel_used=return_channel_used,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value



def _has_active_invert_in_socket_chain(socket, _depth: int = 0) -> bool:
    """Returns True if an unmuted INVERT node is active in the upstream chain of `socket`."""
    if socket is None or not socket.is_linked or not socket.links or _depth > 14:
        return False
    node = socket.links[0].from_node
    if not getattr(node, "mute", False) and node.type == "INVERT":
        fac_sock = node.inputs[0] if len(node.inputs) > 0 else None
        fac_val = _socket_scalar_default(fac_sock, 1.0)
        if (fac_sock is not None and fac_sock.is_linked) or fac_val >= 0.5:
            return True
    for in_sock in node.inputs:
        if in_sock.is_linked and _has_active_invert_in_socket_chain(in_sock, _depth + 1):
            return True
    return False


def _compute_material_roughness_signature(mat: bpy.types.Material | None, obj: bpy.types.Object | None = None) -> str:
    if mat is None:
        return "None"
    parts = [mat.name]
    if obj is not None:
        try:
            st = get_or_init_object_prop_state(obj)
            parts.append(f"dr={float(st.get('dielectric_roughness', 0.45)):.3f}|rm={float(st.get('roughness_map_mix', 1.0)):.3f}|lp={st.get('look_preset', 'CUSTOM')}")
        except Exception:
            pass
    ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    if ms is not None:
        ovr = bool(getattr(ms, "use_custom_texture_overrides", False))
        r_im = getattr(ms, "roughness_image", None)
        m_im = getattr(ms, "mask_image", None)
        parts.append(f"ovr={ovr}|ri={r_im.name if r_im else ''}|rc={getattr(ms, 'roughness_channel', '')}|mi={m_im.name if m_im else ''}")
    if mat.use_nodes and mat.node_tree:
        nt = mat.node_tree
        for n in nt.nodes:
            im_nm = getattr(getattr(n, "image", None), "name", "")
            in_vals = []
            for s in n.inputs:
                if not s.is_linked and hasattr(s, "default_value"):
                    dv = s.default_value
                    if not hasattr(dv, "__len__"):
                        try:
                            in_vals.append(round(float(dv), 3))
                        except Exception:
                            pass
            b_val = round(float(n["reskate_base_val"]), 3) if "reskate_base_val" in n else ""
            m_val = round(float(n["reskate_mix_val"]), 3) if "reskate_mix_val" in n else ""
            parts.append(f"N:{n.name}:{n.type}:{int(bool(getattr(n, 'mute', False)))}:{getattr(n, 'operation', '')}:{im_nm}:{in_vals}:{b_val}:{m_val}")
        for lk in nt.links:
            if getattr(lk, "is_valid", True):
                parts.append(f"L:{lk.from_node.name}.{lk.from_socket.name}->{lk.to_node.name}.{lk.to_socket.name}")
    return "|".join(parts)


def get_material_roughness_summary(obj_or_mat=None, mat: bpy.types.Material | None = None) -> dict:
    """
    Evaluates a fast 64x64 preview of the active material's Roughness output (cached by
    material name + node link signature so UI drawing stays instant) and returns:
      - `matte_pct`: percentage of texels with roughness >= 0.5 (Matte / Rough)
      - `glossy_pct`: percentage of texels with roughness < 0.5 (Glossy / Smooth)
      - `mean_rough`: float 0.0..1.0
      - `is_inverted_in_nodes`: bool (whether an active INVERT node is in the Roughness chain)
      - `has_map`: bool
      - `channel_used`: str (e.g. 'Green', 'Red', 'Color')
      - `image_name`: str
    """
    obj = None
    if isinstance(obj_or_mat, bpy.types.Material):
        mat = obj_or_mat
    elif isinstance(obj_or_mat, bpy.types.Object):
        obj = obj_or_mat
        if mat is None:
            mat = obj.active_material
            if mat is None and len(obj.material_slots) > 0:
                mat = obj.material_slots[0].material

    if mat is None:
        return {
            "matte_pct": 0.0,
            "glossy_pct": 100.0,
            "mean_rough": 0.45,
            "is_inverted_in_nodes": False,
            "has_map": False,
            "channel_used": "Color",
            "image_name": "",
        }

    sig = _compute_material_roughness_signature(mat, obj)
    cache_key = (mat.name, sig)
    if cache_key in _ROUGHNESS_SUMMARY_CACHE:
        return _ROUGHNESS_SUMMARY_CACHE[cache_key]

    p_node = _principled_node(mat)
    ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    use_overrides = bool(getattr(ms, "use_custom_texture_overrides", False)) if ms is not None else False
    obj_state = get_or_init_object_prop_state(obj) if (obj is not None and obj.type == 'MESH') else {}

    base_roughness = float(obj_state.get("dielectric_roughness", 0.45))
    r_mix = float(obj_state.get("roughness_map_mix", 1.0))
    if ms is not None and hasattr(ms, "roughness_map_mix") and "roughness_map_mix" in ms:
        r_mix = float(getattr(ms, "roughness_map_mix", r_mix))
    r_mix = max(0.0, min(1.0, r_mix))

    is_inverted = False
    r_sock = p_node.inputs.get("Roughness") if p_node is not None else None
    if r_sock is not None:
        is_inverted = _has_active_invert_in_socket_chain(r_sock)
        if not r_sock.is_linked:
            base_roughness = float(r_sock.default_value)
        elif r_sock.links and r_sock.links[0].from_node.name == "ReSkate_Roughness_Mix":
            mr_n = r_sock.links[0].from_node
            if "reskate_base_val" in mr_n:
                base_roughness = float(mr_n["reskate_base_val"])
            if "reskate_mix_val" in mr_n:
                r_mix = max(0.0, min(1.0, float(mr_n["reskate_mix_val"])))

    has_map = False
    channel_used = "Color"
    image_name = ""
    rough_2d = None

    eval_r_2d, eval_r_img, eval_r_chan = (None, None, "Color")
    if r_sock is not None and r_sock.is_linked:
        eval_r_2d, eval_r_img, eval_r_chan = _evaluate_scalar_socket_2d(
            r_sock,
            staging_dir=None,
            size=(64, 64),
        )

    ovr_r_img = getattr(ms, "roughness_image", None) if (ms is not None and use_overrides) else None
    ovr_r_chan = getattr(ms, "roughness_channel", "Color") if (ms is not None and use_overrides) else "Color"
    has_distinct_manual_override = bool(
        use_overrides and ovr_r_img is not None and _image_stem(ovr_r_img) != _image_stem(eval_r_img)
    )

    if eval_r_2d is not None and not has_distinct_manual_override:
        raw_r = np.clip(eval_r_2d, 0.0, 1.0)
        rough_2d = np.clip((1.0 - r_mix) * base_roughness + r_mix * raw_r, 0.0, 1.0)
        has_map = True
        channel_used = str(eval_r_chan or "Color")
        image_name = getattr(eval_r_img, "name", "") if eval_r_img else ""
    elif has_distinct_manual_override:
        pil_rough = _blender_image_to_pil(ovr_r_img, staging_dir=None)
        if pil_rough is not None:
            raw_r, used_ch = _extract_channel_map(
                pil_rough,
                ovr_r_chan,
                (64, 64),
                image_name=getattr(ovr_r_img, "name", ""),
                return_channel_used=True,
            )
            rough_2d = np.clip((1.0 - r_mix) * base_roughness + r_mix * raw_r, 0.0, 1.0)
            has_map = True
            channel_used = str(used_ch or ovr_r_chan or "Color")
            image_name = getattr(ovr_r_img, "name", "")

    if rough_2d is not None:
        mean_rough = float(np.mean(rough_2d))
        matte_pct = float(np.mean(rough_2d >= 0.5) * 100.0)
        glossy_pct = float(100.0 - matte_pct)
    else:
        mean_rough = max(0.0, min(1.0, float(base_roughness)))
        matte_pct = 100.0 if mean_rough >= 0.5 else 0.0
        glossy_pct = 100.0 - matte_pct

    res = {
        "matte_pct": round(matte_pct, 1),
        "glossy_pct": round(glossy_pct, 1),
        "mean_rough": round(mean_rough, 4),
        "is_inverted_in_nodes": bool(is_inverted),
        "has_map": bool(has_map),
        "channel_used": channel_used,
        "image_name": image_name,
    }
    _ROUGHNESS_SUMMARY_CACHE[cache_key] = res
    return res



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


def _refresh_pack_cached_texture_files(
    staging_dir: Path,
    files_to_sync: list[Path],
    stale_to_remove: list[str] | None = None,
    target_mod: str = "",
) -> None:
    """
    Copies/refreshes extracted per-slot textures into `Packs/<target_mod>/assets/` if it exists,
    strictly scoping operations to target_mod and skipping redundant writes when files match.
    """
    try:
        base_staging = staging_dir.parent if staging_dir.name == "Pack_Queue" else staging_dir
        packs_root = base_staging / "Packs"
        if not packs_root.exists() or not packs_root.is_dir():
            return
        if not target_mod:
            scene_prop = getattr(bpy.context.scene, "reskate_prop", None) if (bpy.context and getattr(bpy.context, "scene", None)) else None
            target_mod = (getattr(scene_prop, "target_mod_name", "") if scene_prop else "").strip()
        if not target_mod:
            return
        target_ad = packs_root / target_mod / "assets"
        if not target_ad.is_dir():
            if (packs_root / target_mod).is_dir():
                target_ad.mkdir(parents=True, exist_ok=True)
            else:
                return

        for src_f in files_to_sync:
            if src_f is not None and src_f.exists() and src_f.is_file():
                dst_f = target_ad / src_f.name
                if dst_f.resolve() != src_f.resolve() and _file_needs_copy(src_f, dst_f):
                    try:
                        shutil.copy2(src_f, dst_f)
                    except Exception:
                        pass
        for rm_name in (stale_to_remove or []):
            rm_p = target_ad / rm_name
            if rm_p.exists():
                try:
                    rm_p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


# ==============================================================================
# Blender.org Texture Archive Integration & Disk Caching (Milestone 4 - R4)
# ==============================================================================

BLENDER_ORG_ARCHIVE_BASE = "https://download.blender.org/demo-files/archives/textures/"
BLENDER_ORG_WEB_BASE = "https://download.blender.org/archive/textures/"


def get_texture_cache_dir(slug: str, staging_dir: Path | str | None = None) -> Path:
    """
    Resolves the local disk cache directory `<staging_dir>/BlenderOrg_TextureCache/<slug>/`.
    Falls back to `<addon_dir>/BlenderOrg_TextureCache/<slug>/` if staging is unspecified.
    """
    leaf_slug = slug.split("/")[-1].strip() if slug else "default"
    if staging_dir is not None:
        base = Path(staging_dir)
    else:
        try:
            base = get_staging_dir()
        except Exception:
            base = DEFAULT_STAGING_DIR

    cache_path = base / "BlenderOrg_TextureCache" / leaf_slug
    try:
        cache_path.mkdir(parents=True, exist_ok=True)
        return cache_path
    except Exception:
        addon_base = Path(__file__).resolve().parent / "BlenderOrg_TextureCache" / leaf_slug
        addon_base.mkdir(parents=True, exist_ok=True)
        return addon_base


def download_texture_file(url: str, dest_path: Path, timeout: float = 8.0) -> bool:
    """
    Safely downloads a texture file from `url` to `dest_path`.
    Uses atomic temp-file replacement to prevent corrupt partial downloads.
    Returns True on success, False on network failure or HTTP error.
    """
    dest_path = Path(dest_path)
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.parent / f"{dest_path.name}.tmp_{os.getpid()}_{int(time.time() * 1000)}"
    headers = {"User-Agent": "ReSkate-Blender-Addon/1.0"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            with open(tmp_path, "wb") as f_out:
                shutil.copyfileobj(resp, f_out)

        if tmp_path.exists() and tmp_path.stat().st_size > 0:
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except Exception:
                    pass
            tmp_path.replace(dest_path)
            return True
        else:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, Exception):
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


def resolve_blender_org_asset_info(category: str, slug: str | None = None) -> tuple[str, str, dict | None]:
    """
    Resolves catalog entry for `(category, slug)`.
    Returns `(category_key, resolved_slug, variant_dict_or_None)`.
    """
    cat_upper = category.upper().strip() if category else "CONCRETE"
    cat_data = BLENDER_ORG_CATALOG.get(cat_upper)

    # Allow passing slug as first argument (e.g. resolve_blender_org_asset_info("concrete_wall_12"))
    if not cat_data:
        for c_key, c_val in BLENDER_ORG_CATALOG.items():
            variants = c_val.get("variants", {})
            if category in variants:
                return c_key, category, variants[category]
            if slug and slug in variants:
                return c_key, slug, variants[slug]
        return cat_upper, (slug or "default"), None

    if not slug or slug == "default":
        slug = cat_data.get("default", cat_data.get("default_variant", ""))

    variant = cat_data.get("variants", {}).get(slug)
    if not variant and slug:
        # Check other categories if slug was passed with mismatched category
        for c_key, c_val in BLENDER_ORG_CATALOG.items():
            if slug in c_val.get("variants", {}):
                return c_key, slug, c_val["variants"][slug]

    return cat_upper, slug, variant


def is_blender_org_online(timeout: float = 2.0) -> bool:
    """Fast check whether download.blender.org is reachable."""
    try:
        req = urllib.request.Request(BLENDER_ORG_ARCHIVE_BASE, headers={"User-Agent": "ReSkate-Blender-Addon/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 404, 301, 302)
    except Exception:
        return False


def fetch_and_cache_blender_org_maps(
    category: str,
    slug: str = "default",
    staging_dir: Path | str | None = None,
    offline: bool = False,
    timeout: float = 8.0,
) -> dict:
    """
    Fetches and caches PBR texture maps for `category` and `slug` from Blender.org archive
    into `<staging_dir>/BlenderOrg_TextureCache/<slug>/`.
    
    Returns a dictionary:
    {
        "status": "CACHED" | "DOWNLOADED" | "PARTIAL_DOWNLOAD" | "OFFLINE_FALLBACK" | "ERROR_FALLBACK",
        "slug": slug,
        "leaf_name": leaf_slug,
        "cache_dir": Path,
        "maps": {
            "color": Path | None,
            "normal": Path | None,
            "roughness": Path | None,
            "specular": Path | None,
            "displacement": Path | None,
            "bump": Path | None,
            "glossiness": Path | None,
            "metallic": Path | None,
        },
        "is_normal_bump": bool,
        "is_glossiness": bool,
        "surface": str,
        "used_fallback": bool,
        "is_fallback": bool,
        "error_msg": str | None,
    }
    """
    cat_key, resolved_slug, variant_info = resolve_blender_org_asset_info(category, slug)
    leaf_slug = resolved_slug.split("/")[-1] if resolved_slug else "default"
    cache_dir = get_texture_cache_dir(leaf_slug, staging_dir=staging_dir)

    res = {
        "status": "UNKNOWN",
        "slug": resolved_slug,
        "leaf_name": leaf_slug,
        "cache_dir": cache_dir,
        "maps": {
            "color": None,
            "normal": None,
            "roughness": None,
            "specular": None,
            "displacement": None,
            "bump": None,
            "glossiness": None,
            "metallic": None,
        },
        "is_normal_bump": False,
        "is_glossiness": False,
        "surface": "concrete_smooth",
        "used_fallback": False,
        "is_fallback": False,
        "error_msg": None,
    }

    if variant_info is None:
        res.update({
            "status": "ERROR_FALLBACK",
            "used_fallback": True,
            "is_fallback": True,
            "error_msg": f"Asset slug '{resolved_slug}' not found in Blender.org catalog",
        })
        return res


    res["is_normal_bump"] = bool(variant_info.get("is_normal_bump", False))
    res["is_glossiness"] = bool(variant_info.get("is_glossiness", False))
    res["surface"] = str(variant_info.get("surface", "concrete_smooth"))

    cat_path = variant_info.get("path", leaf_slug)
    map_dict = variant_info.get("maps", {})

    all_exist_locally = True
    for map_key, filename in map_dict.items():
        local_p = cache_dir / filename
        if not (local_p.exists() and local_p.stat().st_size > 0):
            all_exist_locally = False
            break

    # If all maps exist locally, serve immediately from disk
    if all_exist_locally and len(map_dict) > 0:
        for map_key, filename in map_dict.items():
            res["maps"][map_key] = cache_dir / filename
        # Ensure bump / glossiness aliases are set
        if res["is_normal_bump"] and res["maps"].get("normal"):
            res["maps"]["bump"] = res["maps"]["normal"]
        if res["is_glossiness"] and res["maps"].get("roughness"):
            res["maps"]["glossiness"] = res["maps"]["roughness"]
        res["status"] = "CACHED"
        res["used_fallback"] = False
        res["is_fallback"] = False
        return res


    # If offline requested and not fully cached:
    if offline:
        c_file = map_dict.get("color")
        if c_file and (cache_dir / c_file).exists() and (cache_dir / c_file).stat().st_size > 0:
            for map_key, filename in map_dict.items():
                p = cache_dir / filename
                if p.exists() and p.stat().st_size > 0:
                    res["maps"][map_key] = p
            if res["is_normal_bump"] and res["maps"].get("normal"):
                res["maps"]["bump"] = res["maps"]["normal"]
            if res["is_glossiness"] and res["maps"].get("roughness"):
                res["maps"]["glossiness"] = res["maps"]["roughness"]
            res["status"] = "CACHED"
            res["used_fallback"] = False
            res["is_fallback"] = False
            return res

        res.update({
            "status": "OFFLINE_FALLBACK",
            "used_fallback": True,
            "is_fallback": True,
            "error_msg": f"Blender.org texture '{resolved_slug}' not cached locally and offline mode active",
        })
        return res


    # Online fetch with error handling
    download_success_count = 0
    download_fail_count = 0

    for map_key, filename in map_dict.items():
        local_p = cache_dir / filename
        if local_p.exists() and local_p.stat().st_size > 0:
            res["maps"][map_key] = local_p
            download_success_count += 1
            continue

        remote_url = f"{BLENDER_ORG_ARCHIVE_BASE}{cat_path}/{filename}"
        ok = download_texture_file(remote_url, local_p, timeout=timeout)
        if ok and local_p.exists() and local_p.stat().st_size > 0:
            res["maps"][map_key] = local_p
            download_success_count += 1
        else:
            download_fail_count += 1

    # Check if primary color map was successfully retrieved or cached
    color_p = res["maps"].get("color")
    if color_p is not None and color_p.exists() and color_p.stat().st_size > 0:
        if res["is_normal_bump"] and res["maps"].get("normal"):
            res["maps"]["bump"] = res["maps"]["normal"]
        if res["is_glossiness"] and res["maps"].get("roughness"):
            res["maps"]["glossiness"] = res["maps"]["roughness"]
        res["status"] = "DOWNLOADED" if download_fail_count == 0 else "PARTIAL_DOWNLOAD"
        res["used_fallback"] = False
        res["is_fallback"] = False
    else:
        # Check if color map already existed from a prior partial run
        c_file = map_dict.get("color")
        if c_file and (cache_dir / c_file).exists() and (cache_dir / c_file).stat().st_size > 0:
            res["maps"]["color"] = cache_dir / c_file
            res["status"] = "CACHED"
            res["used_fallback"] = False
            res["is_fallback"] = False
        else:
            res["status"] = "ERROR_FALLBACK"
            res["used_fallback"] = True
            res["is_fallback"] = True
            res["error_msg"] = f"Failed to download primary color map from {BLENDER_ORG_ARCHIVE_BASE}{cat_path}/"

    return res



def ensure_material_decal_slot_initialized(mat: bpy.types.Material | None, is_multi_material: bool = True) -> bool:
    """
    Ensures `mat.reskate_mat.is_decal_slot` / `mat.sk8_material.is_decal_slot` is initialized once
    for legacy materials whose name explicitly indicates a decal/sticker/wax/graffiti overlay on a
    multi-material mesh, and thereafter strictly honors the user's `Treat Material as Decal Overlay` checkbox!
    """
    if mat is None:
        return False
    rm = getattr(mat, "reskate_mat", None)
    ms = getattr(mat, "sk8_material", None)
    holder = rm if (rm is not None and hasattr(rm, "is_decal_slot")) else (ms or rm)
    if holder is None:
        return False
    if not bool(mat.get("reskate_decal_slot_init", False)):
        m_low = mat.name.lower()
        dom = str(getattr(ms, "domain", None) or getattr(rm, "domain", "surface") or "surface").lower()
        auto_is_decal = (dom == "decal") or (
            is_multi_material and any(k in m_low for k in ("sticker", "decal", "graffiti", "stencil", "wax"))
        )
        if auto_is_decal:
            for h in (rm, ms):
                if h is not None and hasattr(h, "is_decal_slot"):
                    try:
                        h.is_decal_slot = True
                    except Exception:
                        pass
        try:
            mat["reskate_decal_slot_init"] = True
        except Exception:
            pass
    return (
        bool(getattr(rm, "is_decal_slot", False))
        or bool(getattr(ms, "is_decal_slot", False))
        or str(getattr(ms, "domain", None) or getattr(rm, "domain", "surface") or "surface").lower() == "decal"
    )


def _composite_joined_decal_slots_over_opaque(
    obj: bpy.types.Object | None,
    prop_slug: str,
    staging_dir: Path,
    material_textures: list[dict],
    slot_rgba_list: list[np.ndarray],
    slot_rough_list: list[np.ndarray],
    slot_metal_list: list[np.ndarray],
    slot_norm_list: list[np.ndarray],
) -> None:
    """
    When a multi-material prop has a material slot with `Treat Material as Decal Overlay` enabled
    (`is_decal_slot == True`, e.g. `Wax_Type 2`, `Stickers.001`), Frostbite's single-shader
    `MeshSet` cannot mix `_osk` alpha transparency with opaque `0x771` without forcing the entire prop
    into translucent sorting.
    This helper raycasts behind each decal slot's polygons onto the underlying opaque mesh triangles,
    samples the backing surface's Base Color, Roughness, Metallic, and Normal at the hit UV scale/offset,
    composites the decal's transparent pixels over that backing surface, and re-saves the decal slot's
    `_c.png`, `_msk.png`, and `_n.png` as 100% opaque (`has_alpha_transparency = False`, `is_composited_decal = True`).
    """
    if not is_experimental_addon_enabled():
        return
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return
    if len(material_textures) <= 1 or len(slot_rgba_list) != len(material_textures):
        return

    opaque_indices = [
        i
        for i, mt in enumerate(material_textures)
        if not bool(mt.get("is_decal_slot", False)) and not bool(mt.get("invisible", False))
    ]
    decal_indices = [
        i
        for i, mt in enumerate(material_textures)
        if bool(mt.get("is_decal_slot", False)) and not bool(mt.get("invisible", False))
    ]
    if not opaque_indices or not decal_indices:
        return

    try:
        from mathutils.bvhtree import BVHTree
        from mathutils.geometry import barycentric_transform
    except Exception:
        return

    me = obj.data
    me.calc_loop_triangles()
    uv_layer = me.uv_layers.active or (me.uv_layers[0] if me.uv_layers else None)

    # Build a BVHTree over all triangles belonging to opaque material slots
    verts_co = [v.co.copy() for v in me.vertices]
    opaque_set = set(opaque_indices)
    bvh_tris: list[tuple[int, int, int]] = []
    bvh_meta: list[tuple[int, Vector, Vector, Vector, Vector, Vector, Vector]] = []
    for lt in me.loop_triangles:
        p = me.polygons[lt.polygon_index]
        m_idx = int(p.material_index)
        if m_idx in opaque_set:
            v0_i, v1_i, v2_i = int(lt.vertices[0]), int(lt.vertices[1]), int(lt.vertices[2])
            bvh_tris.append((v0_i, v1_i, v2_i))
            if uv_layer is not None:
                l0, l1, l2 = int(lt.loops[0]), int(lt.loops[1]), int(lt.loops[2])
                uv0 = Vector((float(uv_layer.data[l0].uv[0]), float(uv_layer.data[l0].uv[1]), 0.0))
                uv1 = Vector((float(uv_layer.data[l1].uv[0]), float(uv_layer.data[l1].uv[1]), 0.0))
                uv2 = Vector((float(uv_layer.data[l2].uv[0]), float(uv_layer.data[l2].uv[1]), 0.0))
            else:
                uv0 = Vector((0.0, 0.0, 0.0))
                uv1 = Vector((1.0, 0.0, 0.0))
                uv2 = Vector((0.0, 1.0, 0.0))
            bvh_meta.append((m_idx, verts_co[v0_i], verts_co[v1_i], verts_co[v2_i], uv0, uv1, uv2))

    bvh = BVHTree.FromPolygons(verts_co, bvh_tris) if bvh_tris else None

    for d_idx in decal_indices:
        dec_rgba = slot_rgba_list[d_idx]
        dec_rough = slot_rough_list[d_idx]
        dec_metal = slot_metal_list[d_idx]
        dec_norm = slot_norm_list[d_idx]
        dh, dw = dec_rgba.shape[:2]

        # Raycast from decal polygons onto the opaque BVH to find the underlying material slot and UV mapping
        hit_slots: dict[int, int] = {}
        uv_pairs_u: list[tuple[float, float]] = []
        uv_pairs_v: list[tuple[float, float]] = []

        if bvh is not None and uv_layer is not None:
            for lt in me.loop_triangles:
                p = me.polygons[lt.polygon_index]
                if int(p.material_index) != d_idx:
                    continue
                n_poly = p.normal.copy()
                if n_poly.length_squared < 1e-9:
                    continue
                n_poly.normalize()
                # Raycast at each vertex and centroid of the decal triangle
                l0, l1, l2 = int(lt.loops[0]), int(lt.loops[1]), int(lt.loops[2])
                pts_and_uvs = [
                    (verts_co[int(lt.vertices[0])], uv_layer.data[l0].uv),
                    (verts_co[int(lt.vertices[1])], uv_layer.data[l1].uv),
                    (verts_co[int(lt.vertices[2])], uv_layer.data[l2].uv),
                    (
                        (verts_co[int(lt.vertices[0])] + verts_co[int(lt.vertices[1])] + verts_co[int(lt.vertices[2])]) / 3.0,
                        (uv_layer.data[l0].uv + uv_layer.data[l1].uv + uv_layer.data[l2].uv) / 3.0,
                    ),
                ]
                for pt_co, d_uv in pts_and_uvs:
                    # Cast inward (-n_poly) starting 4mm above the decal quad, up to 20cm
                    hit_loc, _hit_norm, hit_idx, _hit_dist = bvh.ray_cast(pt_co + n_poly * 0.004, -n_poly, 0.20)
                    if hit_loc is None or hit_idx is None:
                        # Fallback: cast outward (+n_poly) in case decal quad normal was flipped
                        hit_loc, _hit_norm, hit_idx, _hit_dist = bvh.ray_cast(pt_co - n_poly * 0.004, n_poly, 0.20)
                    if hit_loc is None or hit_idx is None:
                        hit_loc, _hit_norm, hit_idx, _hit_dist = bvh.find_nearest(pt_co, 0.20)
                    if hit_loc is not None and hit_idx is not None and 0 <= int(hit_idx) < len(bvh_meta):
                        m_hit, tv0, tv1, tv2, tuv0, tuv1, tuv2 = bvh_meta[int(hit_idx)]
                        hit_slots[m_hit] = hit_slots.get(m_hit, 0) + 1
                        try:
                            h_uv = barycentric_transform(hit_loc, tv0, tv1, tv2, tuv0, tuv1, tuv2)
                            uv_pairs_u.append((float(d_uv[0]), float(h_uv.x)))
                            uv_pairs_v.append((float(d_uv[1]), float(h_uv.y)))
                        except Exception:
                            pass

        if hit_slots:
            backing_slot = max(hit_slots.items(), key=lambda kv: kv[1])[0]
        else:
            backing_slot = opaque_indices[0]

        back_rgba = slot_rgba_list[backing_slot]
        back_rough = slot_rough_list[backing_slot]
        back_metal = slot_metal_list[backing_slot]
        back_norm = slot_norm_list[backing_slot]
        bh, bw = back_rgba.shape[:2]

        # Estimate UV scale & offset of the underlying opaque surface relative to the decal quad's [0..1] UV
        su, ou = 1.0, 0.0
        sv, ov = 1.0, 0.0
        if len(uv_pairs_u) >= 2:
            du_arr = np.array([p[0] for p in uv_pairs_u], dtype=np.float32)
            hu_arr = np.array([p[1] for p in uv_pairs_u], dtype=np.float32)
            d_span_u = float(du_arr.max() - du_arr.min())
            h_span_u = float(hu_arr.max() - hu_arr.min())
            if d_span_u > 0.15 and 0.05 <= (h_span_u / d_span_u) <= 4.0:
                su = float(h_span_u / d_span_u)
                ou = float(np.median(hu_arr - su * du_arr))
        if len(uv_pairs_v) >= 2:
            dv_arr = np.array([p[0] for p in uv_pairs_v], dtype=np.float32)
            hv_arr = np.array([p[1] for p in uv_pairs_v], dtype=np.float32)
            d_span_v = float(dv_arr.max() - dv_arr.min())
            h_span_v = float(hv_arr.max() - hv_arr.min())
            if d_span_v > 0.15 and 0.05 <= (h_span_v / d_span_v) <= 4.0:
                sv = float(h_span_v / d_span_v)
                ov = float(np.median(hv_arr - sv * dv_arr))

        # Sample backing maps onto the decal's (dh, dw) grid with wrapped UV coordinates
        u_grid = (np.linspace(0.0, 1.0, dw, endpoint=False, dtype=np.float32) * su + ou) % 1.0
        v_grid = (np.linspace(1.0, 0.0, dh, endpoint=False, dtype=np.float32) * sv + ov) % 1.0
        ix = np.clip((u_grid * bw).astype(np.int32), 0, bw - 1)
        iy = np.clip(((1.0 - v_grid) * bh).astype(np.int32), 0, bh - 1)

        sampled_back_rgb = back_rgba[np.ix_(iy, ix)][:, :, :3]
        sampled_back_rough = back_rough[np.ix_(iy, ix)]
        sampled_back_metal = back_metal[np.ix_(iy, ix)]
        sampled_back_norm = back_norm[np.ix_(iy, ix)]

        alpha = np.clip(dec_rgba[:, :, 3:4], 0.0, 1.0)
        alpha_2d = alpha[:, :, 0]

        comp_rgb = np.clip((1.0 - alpha) * sampled_back_rgb + alpha * dec_rgba[:, :, :3], 0.0, 1.0)
        comp_rough = np.clip((1.0 - alpha_2d) * sampled_back_rough + alpha_2d * dec_rough, 0.0, 1.0)
        comp_metal = np.clip((1.0 - alpha_2d) * sampled_back_metal + alpha_2d * dec_metal, 0.0, 1.0)
        comp_norm = (1.0 - alpha) * sampled_back_norm + alpha * dec_norm
        # Re-normalize tangent-space normal
        n_xyz = comp_norm * 2.0 - 1.0
        n_len = np.maximum(np.linalg.norm(n_xyz, axis=2, keepdims=True), 1e-6)
        comp_norm = np.clip((n_xyz / n_len) * 0.5 + 0.5, 0.0, 1.0)

        comp_rgba = np.ones((dh, dw, 4), dtype=np.float32)
        comp_rgba[:, :, :3] = comp_rgb
        slot_rgba_list[d_idx] = comp_rgba
        slot_rough_list[d_idx] = comp_rough
        slot_metal_list[d_idx] = comp_metal
        slot_norm_list[d_idx] = comp_norm

        # Save updated 100% opaque PNGs for this decal slot
        mt = material_textures[d_idx]
        slot_png_path = Path(mt["texture_path"])
        slot_msk_path = Path(mt["mask_texture_path"])
        slot_norm_path = Path(mt["normal_texture_path"]) if mt.get("normal_texture_path") else (staging_dir / f"ReSkate_Mat_{prop_slug}_{d_idx}_n.png")

        out_u8 = np.clip(np.round(comp_rgba * 255.0), 0, 255).astype(np.uint8)
        out_u8[:, :, 3] = 255
        Image.fromarray(out_u8, mode="RGBA").save(slot_png_path, "PNG", compress_level=1)

        msk_u8 = np.full((dh, dw, 4), 255, dtype=np.uint8)
        msk_u8[:, :, 0] = np.clip(np.round(comp_metal * 255.0), 0, 255).astype(np.uint8)
        msk_u8[:, :, 1] = np.clip(np.round((1.0 - comp_rough) * 255.0), 0, 255).astype(np.uint8)
        Image.fromarray(msk_u8, mode="RGBA").save(slot_msk_path, "PNG", compress_level=1)

        norm_u8 = np.full((dh, dw, 4), 255, dtype=np.uint8)
        norm_u8[:, :, :3] = np.clip(np.round(comp_norm * 255.0), 0, 255).astype(np.uint8)
        Image.fromarray(norm_u8, mode="RGBA").save(slot_norm_path, "PNG", compress_level=1)

        _refresh_pack_cached_texture_files(staging_dir, [slot_png_path, slot_msk_path, slot_norm_path], [])

        new_r = float(comp_rough.mean())
        new_m = float(comp_metal.mean())
        mt["has_alpha_transparency"] = False
        mt["is_composited_decal"] = True
        mt["alpha"] = "opaque"
        mt["normal_texture_path"] = str(slot_norm_path)
        mt["has_normal_map"] = True
        mt["roughness"] = round(new_r, 4)
        mt["smoothness"] = round(1.0 - new_r, 4)
        mt["metallic"] = round(new_m, 4)
        print(
            f"[Decal Auto-Composite] Slot {d_idx} ('{mt.get('material_name')}') composited over opaque Slot {backing_slot} "
            f"('{material_textures[backing_slot].get('material_name')}', UV scale=({su:.2f}, {sv:.2f})) -> 100% solid Shader 0x771."
        )

_ACTIVE_TEMP_LOCK = threading.Lock()
_ACTIVE_TEMP_FILES = set()


def _safe_unlink(path: Path, max_retries: int = 12, backoff_base: float = 0.02) -> bool:
    """
    Safely unlinks a file with retry backoff to handle transient Windows
    file-sharing locks (antivirus, search indexer, etc.). Maximum retry span: ~2.5s.
    """
    path = Path(path)
    if not path.exists():
        return True
    for attempt in range(max_retries):
        try:
            path.unlink(missing_ok=True)
            if not path.exists():
                return True
            time.sleep(backoff_base * (1.5 ** min(attempt, 8)))
            if not path.exists():
                return True
        except (PermissionError, OSError):
            time.sleep(backoff_base * (1.5 ** min(attempt, 8)))
        except FileNotFoundError:
            return True
        except Exception:
            break
    return not path.exists()


def _atomic_png_save(
    image_obj,
    target_path: Path,
    compress_level: int | None = 1,
    cancel_token: threading.Event | None = None,
) -> bool:
    """
    Saves a PIL or _NumpyCompatImage to a temporary file (.tmp.{uuid}) and atomically
    renames it to target_path, preventing corrupt or partial files upon cancellation.
    Returns True on success, False if aborted due to cancellation.
    """
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        return False

    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    renamed = False

    with _ACTIVE_TEMP_LOCK:
        _ACTIVE_TEMP_FILES.add(tmp_path)

    try:
        if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            return False

        if compress_level is not None:
            image_obj.save(tmp_path, "PNG", compress_level=compress_level)
        else:
            image_obj.save(tmp_path, "PNG")

        if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            _safe_unlink(tmp_path)
            return False

        # Windows-safe atomic replace with 8 retries and exponential backoff for momentary OS file locks
        for attempt in range(8):
            if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
                _safe_unlink(tmp_path)
                return False
            try:
                tmp_path.replace(target_path)
                renamed = True
                break
            except (PermissionError, OSError):
                if attempt == 7:
                    raise
                time.sleep(0.02 * (1.5 ** attempt))
        return renamed
    except Exception:
        _safe_unlink(tmp_path)
        raise
    finally:
        # Unconditionally unlink tmp_path if it was not atomically renamed to target_path
        if not renamed and tmp_path.exists():
            _safe_unlink(tmp_path)
        with _ACTIVE_TEMP_LOCK:
            _ACTIVE_TEMP_FILES.discard(tmp_path)


def _read_detached_image_data(img: bpy.types.Image | None, is_color: bool = True) -> dict | None:
    """
    Main-thread ONLY: Reads socket image properties, packed file bytes, disk file path,
    or copies pixel buffer via img.pixels.foreach_get(buf) into detached memory.
    Returns a dictionary with ZERO bpy references, or None.
    """
    if img is None:
        return None

    cs_name = str(getattr(getattr(img, "colorspace_settings", None), "name", "") or "").strip().lower()
    img_nm_low = str(getattr(img, "name", "") or "").lower()
    is_non_color = (
        (not is_color)
        or any(k in cs_name for k in ("non-color", "raw", "data"))
        or any(img_nm_low.endswith(suf) for suf in ("_roughness", "_metallic", "_normal", "_n", "_msk", "_orm"))
    )
    is_float_buf = bool(getattr(img, "is_float", False))

    if (
        (not is_non_color)
        and is_float_buf
        and cs_name == "srgb"
        and (getattr(img, "packed_file", None) is not None or _source_file_for_image(img) is not None)
    ):
        try:
            img.colorspace_settings.name = "Linear Rec.709"
            cs_name = "linear rec.709"
        except Exception:
            pass

    is_linear_color = (not is_non_color) and any(
        k in cs_name for k in ("linear", "rec.709", "rec709", "aces", "scene_linear", "filmic", "agx")
    )
    is_dirty_buf = bool(getattr(img, "is_dirty", False)) or str(getattr(img, "source", "")).upper() == "GENERATED"

    # Check cache first for static images
    is_dynamic = _is_internal_wear_or_bake_image(img) or is_float_buf or is_linear_color or is_dirty_buf
    if not is_dynamic:
        try:
            cache_key = (
                str(getattr(img, "name", "")),
                int(img.size[0]),
                int(img.size[1]),
                str(getattr(img, "filepath_raw", "") or ""),
                cs_name,
                is_float_buf,
                bool(is_color),
            )
            if cache_key in _IMAGE_PIL_CACHE:
                return {
                    "type": "pil",
                    "pil": _IMAGE_PIL_CACHE[cache_key].copy(),
                    "name": getattr(img, "name", ""),
                    "is_linear": False,
                    "is_non_color": is_non_color,
                    "cache_key": cache_key,
                }
        except Exception:
            pass

    w, h = int(img.size[0]), int(img.size[1])
    ch = int(getattr(img, "channels", 4) or 4)

    # If dirty, float, or linear color image in Blender's memory, read directly via foreach_get
    if (is_dirty_buf or is_float_buf or is_linear_color) and w > 0 and h > 0 and len(img.pixels) == w * h * ch:
        cache_buf_key = (
            str(getattr(img, "name", "")),
            w,
            h,
            ch,
            str(getattr(img, "filepath_raw", "") or ""),
            is_linear_color,
            is_non_color,
        )
        if not is_dirty_buf and cache_buf_key in _DETACHED_BUFFER_CACHE:
            cached = _DETACHED_BUFFER_CACHE[cache_buf_key]
            return {
                "type": "buffer",
                "buf": cached["buf"].copy(),
                "w": w,
                "h": h,
                "ch": ch,
                "is_linear": (is_float_buf or is_linear_color),
                "is_non_color": is_non_color,
                "name": getattr(img, "name", ""),
            }

        buf = np.empty(w * h * ch, dtype=np.float32)
        img.pixels.foreach_get(buf)
        res_dict = {
            "type": "buffer",
            "buf": buf,
            "w": w,
            "h": h,
            "ch": ch,
            "is_linear": (is_float_buf or is_linear_color),
            "is_non_color": is_non_color,
            "name": getattr(img, "name", ""),
        }
        if not is_dirty_buf:
            if len(_DETACHED_BUFFER_CACHE) > 32:
                _DETACHED_BUFFER_CACHE.clear()
            _DETACHED_BUFFER_CACHE[cache_buf_key] = {"buf": buf.copy()}
        return res_dict

    # 1. Packed image data in memory
    try:
        packed = getattr(img, "packed_file", None)
        if packed is not None and getattr(packed, "data", None):
            raw_bytes = bytes(packed.data)
            if len(raw_bytes) > 16:
                return {
                    "type": "packed",
                    "bytes": raw_bytes,
                    "is_linear": (is_float_buf or is_linear_color),
                    "is_non_color": is_non_color,
                    "name": getattr(img, "name", ""),
                }
    except Exception:
        pass

    # 2. External file on disk
    src_file = _source_file_for_image(img)
    if src_file and Path(src_file).exists():
        return {
            "type": "disk",
            "path": str(src_file),
            "is_linear": (is_float_buf or is_linear_color),
            "is_non_color": is_non_color,
            "name": getattr(img, "name", ""),
        }

    # 3. Direct Blender pixel buffer fallback
    if w > 0 and h > 0 and len(img.pixels) == w * h * ch:
        buf = np.empty(w * h * ch, dtype=np.float32)
        img.pixels.foreach_get(buf)
        return {
            "type": "buffer",
            "buf": buf,
            "w": w,
            "h": h,
            "ch": ch,
            "is_linear": (is_float_buf or is_linear_color),
            "is_non_color": is_non_color,
            "name": getattr(img, "name", ""),
        }

    return None


def _decode_detached_image(detached_dict: dict | None):
    """
    Worker-thread ONLY: Decodes detached image dictionary into a PIL.Image or _NumpyCompatImage.
    Applies linear-to-sRGB conversion when indicated. Zero bpy calls.
    """
    if not detached_dict:
        return None
    t = detached_dict["type"]
    if t == "pil":
        return detached_dict["pil"]

    is_linear = detached_dict.get("is_linear", False)
    is_non_color = detached_dict.get("is_non_color", False)

    if t == "packed":
        im = Image.open(io.BytesIO(detached_dict["bytes"])).convert("RGBA")
        if is_linear and not is_non_color:
            arr_f = np.asarray(im, dtype=np.float32) / 255.0
            arr_f[:, :, :3] = _linear_to_srgb_np(arr_f[:, :, :3])
            u8_c = np.clip(np.round(arr_f * 255.0), 0, 255).astype(np.uint8)
            im = Image.fromarray(u8_c, mode="RGBA")
        return im

    if t == "disk":
        im = Image.open(detached_dict["path"]).convert("RGBA")
        if is_linear and not is_non_color:
            arr_f = np.asarray(im, dtype=np.float32) / 255.0
            arr_f[:, :, :3] = _linear_to_srgb_np(arr_f[:, :, :3])
            u8_c = np.clip(np.round(arr_f * 255.0), 0, 255).astype(np.uint8)
            im = Image.fromarray(u8_c, mode="RGBA")
        return im

    if t == "buffer":
        w, h, ch = detached_dict["w"], detached_dict["h"], detached_dict["ch"]
        buf = detached_dict["buf"]
        arr = buf.reshape((h, w, ch))[::-1, :, :].copy()
        if is_linear and (not is_non_color) and ch >= 3:
            arr[:, :, :3] = _linear_to_srgb_np(arr[:, :, :3])
        u8 = np.clip(np.round(arr * 255.0), 0, 255).astype(np.uint8)
        if ch == 4:
            return Image.fromarray(u8, mode="RGBA")
        elif ch == 3:
            rgba = np.dstack([u8, np.full((h, w), 255, dtype=np.uint8)])
            return Image.fromarray(rgba, mode="RGBA")
        elif ch == 1:
            g = u8[:, :, 0]
            rgba = np.dstack([g, g, g, np.full((h, w), 255, dtype=np.uint8)])
            return Image.fromarray(rgba, mode="RGBA")
        else:
            return Image.fromarray(u8[:, :, :4], mode="RGBA")

    return None


def _apply_precomputed_wear_offthread(
    wear_state: dict,
    wear_masks: tuple[np.ndarray, np.ndarray, np.ndarray],
    basis: str,
    rgba_arr: np.ndarray,
    rough_map_2d: np.ndarray | None,
    mat_roughness: float,
    metal_map_2d: np.ndarray | None,
    mat_metallic: float,
    norm_01_2d: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray | None, float, np.ndarray | None, float, np.ndarray | None]:
    """
    Worker-thread ONLY: Applies procedural wear using precomputed UV geometry wear masks.
    Zero bpy calls.
    """
    w_edge = max(0.0, min(3.0, float(wear_state.get("wear_edge_strength", 0.45))))
    w_crev = max(0.0, min(3.0, float(wear_state.get("wear_crevice_strength", 0.45))))
    w_top = max(0.0, min(3.0, float(wear_state.get("wear_top_strength", 0.35))))
    if w_edge <= 1e-3 and w_crev <= 1e-3 and w_top <= 1e-3:
        return rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d

    edge_m, crev_m, top_m = wear_masks
    if float(edge_m.max()) <= 1e-4 and float(crev_m.max()) <= 1e-4 and float(top_m.max()) <= 1e-4:
        return rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d

    basis_cfg = {
        "CONCRETE": {
            "edge_lighten": 0.48,
            "edge_tint": np.array([0.88, 0.87, 0.84], dtype=np.float32),
            "edge_rough_delta": 0.28,
            "edge_metal_target": None,
            "edge_bump": 0.85,
            "crev_darken": 0.72,
            "top_lighten": 0.36,
            "top_rough_min": 0.82,
            "top_bump": 0.55,
        },
        "METAL_RAIL": {
            "edge_lighten": 0.54,
            "edge_tint": np.array([0.92, 0.94, 0.97], dtype=np.float32),
            "edge_rough_delta": 0.32,
            "edge_metal_target": 0.98,
            "edge_bump": 0.65,
            "crev_darken": 0.68,
            "top_lighten": 0.50,
            "top_rough_min": 0.68,
            "top_bump": 0.70,
        },
        "PAINTED": {
            "edge_lighten": 0.66,
            "edge_tint": np.array([0.86, 0.88, 0.91], dtype=np.float32),
            "edge_rough_delta": 0.36,
            "edge_metal_target": 0.82,
            "edge_bump": 0.80,
            "crev_darken": 0.70,
            "top_lighten": 0.52,
            "top_rough_min": 0.70,
            "top_bump": 0.65,
        },
        "WOOD": {
            "edge_lighten": 0.50,
            "edge_tint": np.array([0.84, 0.70, 0.52], dtype=np.float32),
            "edge_rough_delta": 0.30,
            "edge_metal_target": None,
            "edge_bump": 0.75,
            "crev_darken": 0.72,
            "top_lighten": 0.40,
            "top_rough_min": 0.78,
            "top_bump": 0.55,
        },
        "ASPHALT": {
            "edge_lighten": 0.52,
            "edge_tint": np.array([0.62, 0.61, 0.58], dtype=np.float32),
            "edge_rough_delta": 0.28,
            "edge_metal_target": None,
            "edge_bump": 0.95,
            "crev_darken": 0.78,
            "top_lighten": 0.38,
            "top_rough_min": 0.90,
            "top_bump": 0.65,
        },
    }.get(basis, None) or {
        "edge_lighten": 0.50,
        "edge_tint": np.array([0.88, 0.88, 0.88], dtype=np.float32),
        "edge_rough_delta": 0.28,
        "edge_metal_target": None,
        "edge_bump": 0.75,
        "crev_darken": 0.70,
        "top_lighten": 0.42,
        "top_rough_min": 0.72,
        "top_bump": 0.55,
    }

    h, w = rgba_arr.shape[:2]
    rgb = rgba_arr[:, :, :3].copy()
    r_2d = rough_map_2d.copy() if rough_map_2d is not None else np.full((h, w), mat_roughness, dtype=np.float32)
    m_2d = metal_map_2d.copy() if metal_map_2d is not None else np.full((h, w), mat_metallic, dtype=np.float32)

    edge_fac = _overdrive_wear_mask(edge_m, w_edge)
    crev_fac = _overdrive_wear_mask(crev_m, w_crev)
    top_fac = _overdrive_wear_mask(top_m, w_top)

    # 1. Crevice Darkening
    if w_crev > 1e-3:
        darken_strength = min(0.96, float(basis_cfg["crev_darken"]) * (1.0 + 0.28 * max(0.0, w_crev - 1.0)))
        rgb *= (1.0 - crev_fac[:, :, None] * darken_strength)
        r_2d = np.clip(r_2d + crev_fac * 0.45, 0.0, 1.0)

    # 2. Sharp Edge Wear
    if w_edge > 1e-3:
        lighten_boost = min(0.95, float(basis_cfg["edge_lighten"]) * (1.0 + 0.36 * max(0.0, w_edge - 1.0)))
        lighten_amt = np.clip(edge_fac[:, :, None] * lighten_boost, 0.0, 0.95)
        target_edge_rgb = np.clip(
            np.maximum(rgb + 0.30 + 0.12 * max(0.0, w_edge - 1.0), basis_cfg["edge_tint"][None, None, :]),
            0.0,
            1.0,
        )
        rgb = rgb * (1.0 - lighten_amt) + target_edge_rgb * lighten_amt
        r_2d = np.clip(r_2d + edge_fac * float(basis_cfg["edge_rough_delta"]) * (1.0 + 0.30 * max(0.0, w_edge - 1.0)), 0.0, 1.0)
        if basis_cfg["edge_metal_target"] is not None:
            m_blend = np.clip(edge_fac * (0.65 + 0.15 * max(0.0, w_edge - 1.0)), 0.0, 0.95)
            m_2d = np.clip(m_2d * (1.0 - m_blend) + float(basis_cfg["edge_metal_target"]) * m_blend, 0.0, 1.0)

    # 3. Top-Down Surface / Rail Grind Scratch Wear
    if w_top > 1e-3:
        top_light_boost = min(0.94, float(basis_cfg["top_lighten"]) * (1.0 + 0.38 * max(0.0, w_top - 1.0)))
        top_light = np.clip(top_fac[:, :, None] * top_light_boost, 0.0, 0.94)
        scratch_rgb = np.clip(
            np.maximum(rgb + 0.32 + 0.14 * max(0.0, w_top - 1.0), basis_cfg["edge_tint"][None, None, :]),
            0.0,
            1.0,
        )
        rgb = rgb * (1.0 - top_light) + scratch_rgb * top_light
        target_top_r = np.maximum(r_2d + 0.40 * top_fac, float(basis_cfg["top_rough_min"]) * top_fac)
        r_blend = np.clip(top_fac * (0.80 + 0.10 * max(0.0, w_top - 1.0)), 0.0, 0.96)
        r_2d = np.clip(r_2d * (1.0 - r_blend) + target_top_r * r_blend, 0.0, 1.0)

    rgba_arr = rgba_arr.copy()
    rgba_arr[:, :, :3] = np.clip(rgb, 0.0, 1.0)
    mat_roughness = float(r_2d.mean())
    mat_metallic = float(m_2d.mean())

    # 4. Edge Chip & Top Scratch Normal-Map Micro-Relief
    if w_edge > 1e-3 or w_top > 1e-3 or w_crev > 1e-3:
        height_field = (
            -edge_fac * float(basis_cfg["edge_bump"]) * (1.0 + 0.35 * max(0.0, w_edge - 1.0))
            - top_fac * float(basis_cfg["top_bump"]) * (1.0 + 0.35 * max(0.0, w_top - 1.0))
            - crev_fac * 0.38 * (1.0 + 0.25 * max(0.0, w_crev - 1.0))
        ).astype(np.float32)
        dh_dy, dh_dx = np.gradient(height_field)
        bx = -dh_dx * 8.0
        by = dh_dy * 8.0
        if norm_01_2d is not None:
            bn = norm_01_2d * 2.0 - 1.0
            nx = bn[:, :, 0] + bx
            ny = bn[:, :, 1] + by
            nz = np.maximum(bn[:, :, 2], 0.15)
        else:
            nx = bx
            ny = by
            nz = np.ones_like(bx)
        n_vec = np.dstack([nx, ny, nz]).astype(np.float32)
        n_len = np.linalg.norm(n_vec, axis=2, keepdims=True)
        n_vec = np.where(n_len > 1e-6, n_vec / np.maximum(n_len, 1e-6), np.array([[[0.0, 0.0, 1.0]]], dtype=np.float32))
        norm_01_2d = np.clip(n_vec * 0.5 + 0.5, 0.0, 1.0).astype(np.float32)

    return rgba_arr, r_2d, mat_roughness, m_2d, mat_metallic, norm_01_2d


def _extract_slot_inputs_mainthread(
    obj: bpy.types.Object,
    slot_idx: int,
    mat: bpy.types.Material | None,
    mats_count: int,
    prop_slug: str,
    staging_dir: Path,
    target_atlas_res: int,
    obj_state: dict,
    scene_prop,
    preset_vals: dict | None,
) -> dict:
    """
    Main-thread ONLY: Synchronous wrapper that iterates _extract_slot_inputs_mainthread_generator to completion.
    """
    gen = _extract_slot_inputs_mainthread_generator(
        obj=obj,
        slot_idx=slot_idx,
        mat=mat,
        mats_count=mats_count,
        prop_slug=prop_slug,
        staging_dir=staging_dir,
        target_atlas_res=target_atlas_res,
        obj_state=obj_state,
        scene_prop=scene_prop,
        preset_vals=preset_vals,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _extract_slot_inputs_mainthread_generator(
    obj: bpy.types.Object,
    slot_idx: int,
    mat: bpy.types.Material | None,
    mats_count: int,
    prop_slug: str,
    staging_dir: Path,
    target_atlas_res: int,
    obj_state: dict,
    scene_prop,
    preset_vals: dict | None,
    slot_base_pct: float = 10.0,
    slot_end_pct: float = 20.0,
):
    """
    Main-thread generator: Inspects shader nodes, reads socket values, calls
    img.pixels.foreach_get(buf) or reads packed bytes, precalculates wear masks,
    yielding ProgressStatus at intermediate sub-operations, and returns a completely
    detached dictionary with ZERO bpy references.
    """
    span = slot_end_pct - slot_base_pct
    last_pct = slot_base_pct

    def _status(label: str, pct: float) -> ProgressStatus:
        nonlocal last_pct
        clamped_pct = min(slot_end_pct, max(last_pct, round(float(pct), 2)))
        last_pct = clamped_pct
        return ProgressStatus(label, clamped_pct)

    mat_name = mat.name if mat else f"Slot_{slot_idx}"
    rm = getattr(mat, "reskate_mat", None) if mat else None
    sk8_ext = getattr(mat, "sk8_material", None) if mat else None
    ms = rm or sk8_ext
    holder_with_user_imgs = rm or sk8_ext

    yield _status(
        f"[{obj.name}] Validating shader graph for slot {slot_idx+1}/{mats_count} ('{mat_name}')...",
        slot_base_pct + 0.04 * span,
    )
    prev_sync_prop = getattr(properties, "_IS_SYNCING_PROP", False)
    prev_sync_mat = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    _set_global_sync_state(True, True)
    try:
        if mat is not None:
            yield from heal_corrupted_reskate_user_override_nodes_generator(
                mat,
                obj=obj,
                slot_index=slot_idx,
                label_prefix=f"[{obj.name}] ",
                pct_base=slot_base_pct + 0.04 * span,
                pct_span=0.06 * span,
            )
    finally:
        _set_global_sync_state(prev_sync_prop, prev_sync_mat)

    yield _status(
        f"[{obj.name}] Syncing shader nodes for slot {slot_idx+1}/{mats_count} ('{mat_name}')...",
        slot_base_pct + 0.10 * span,
    )
    prev_sync_prop = getattr(properties, "_IS_SYNCING_PROP", False)
    prev_sync_mat = getattr(properties, "_IN_MAT_TEX_SYNC", False)
    _set_global_sync_state(True, True)
    try:
        if mat is not None and holder_with_user_imgs is not None:
            if bool(getattr(holder_with_user_imgs, "use_custom_texture_overrides", False)):
                has_slot_user_img = any(
                    getattr(holder_with_user_imgs, k, None) is not None
                    and not _is_internal_wear_or_bake_image(getattr(holder_with_user_imgs, k, None))
                    for k in ("base_color_image", "normal_image", "roughness_image", "metallic_image")
                )
                if has_slot_user_img:
                    sync_user_texture_slots_to_shader_nodes(obj, mat, holder_with_user_imgs)
            else:
                sync_material_from_shader_nodes(mat, obj=obj, slot_index=slot_idx, force=True, heal=False)
    finally:
        _set_global_sync_state(prev_sync_prop, prev_sync_mat)

    yield _status(
        f"[{obj.name}] Initializing slot geometry & decal parameters for slot {slot_idx+1}/{mats_count}...",
        slot_base_pct + 0.18 * span,
    )

    p_node = _principled_node(mat)
    use_overrides = bool(getattr(ms, "use_custom_texture_overrides", False)) if ms is not None else False

    invisible = bool(getattr(sk8_ext, "invisible", False) or getattr(ms, "invisible", False)) if ms is not None else False
    domain = str(getattr(sk8_ext, "domain", None) or getattr(ms, "domain", "surface") or "surface") if ms is not None else "surface"
    srgb = bool(getattr(ms, "srgb", True)) if ms is not None else True
    alpha_mode = str(getattr(ms, "alpha", "auto")) if ms is not None else "auto"
    alpha_source = str(getattr(ms, "alpha_source", "auto")) if ms is not None else "auto"
    is_decal_slot = ensure_material_decal_slot_initialized(mat, is_multi_material=(mats_count > 1))
    decal_alpha_mode = str(getattr(ms, "decal_alpha_mode", "AUTO") or "AUTO").upper() if ms is not None else "AUTO"
    prop_decal_mode = str(obj_state.get("decal_shader_mode", "CUTOUT_273") or "CUTOUT_273").upper()
    decal_shader_mode = (
        str(getattr(ms, "decal_shader_mode", prop_decal_mode) or prop_decal_mode).upper()
        if ms is not None
        else prop_decal_mode
    )

    # 1. Base color & image selection
    base_col = [0.78, 0.78, 0.78]
    if p_node is not None and "Base Color" in p_node.inputs:
        dv = p_node.inputs["Base Color"].default_value
        base_col = [float(dv[0]), float(dv[1]), float(dv[2])]

    ms_bc_img = getattr(ms, "base_color_image", None) if ms is not None else None
    if _is_internal_wear_or_bake_image(ms_bc_img) or (ms_bc_img is not None and _is_non_albedo_pbr_image_name(getattr(ms_bc_img, "name", ""))):
        ms_bc_img = None
    shader_bc_img = _base_color_image(mat)
    if use_overrides and ms_bc_img is not None:
        base_img_bl = ms_bc_img
    else:
        base_img_bl = shader_bc_img or (ms_bc_img if use_overrides else None)

    # Candidate images for sizing
    ref_img_for_size = base_img_bl
    if ref_img_for_size is None:
        for cand_bl in (
            getattr(ms, "normal_image", None) if (ms is not None and use_overrides) else None,
            getattr(ms, "roughness_image", None) if (ms is not None and use_overrides) else None,
            getattr(ms, "metallic_image", None) if (ms is not None and use_overrides) else None,
            getattr(ms, "mask_image", None) if ms is not None else None,
            _normal_image(mat),
        ):
            if cand_bl is not None and not _is_internal_wear_or_bake_image(cand_bl):
                ref_img_for_size = cand_bl
                break

    slot_dim = 1024
    slot_w = 1024
    slot_h = 1024
    if ref_img_for_size is not None:
        src_w, src_h = int(ref_img_for_size.size[0]), int(ref_img_for_size.size[1])
        if src_w > 0 and src_h > 0:
            max_src_dim = max(src_w, src_h)
            max_allowed = 4096 if (target_atlas_res >= 4096 or max_src_dim >= 3072) else (2048 if (target_atlas_res >= 2048 or max_src_dim >= 1536) else 1024)

            def _snap_pot_dim(val: int, cap: int) -> int:
                v = max(64, min(cap, int(val)))
                best = 64
                for p in (64, 128, 256, 512, 1024, 2048, 4096):
                    if p <= cap and abs(v - p) <= abs(v - best):
                        best = p
                return best

            if mats_count == 1 and src_w != src_h:
                scale_down = min(1.0, float(max_allowed) / float(max(1, max_src_dim)))
                slot_w = _snap_pot_dim(int(round(src_w * scale_down)), max_allowed)
                slot_h = _snap_pot_dim(int(round(src_h * scale_down)), max_allowed)
                slot_dim = max(slot_w, slot_h)
            else:
                if max_src_dim >= 3072 and max_allowed >= 4096 and mats_count == 1:
                    slot_dim = 4096
                elif max_src_dim >= 1536 and max_allowed >= 2048:
                    slot_dim = 2048
                slot_w, slot_h = slot_dim, slot_dim

    slot_size = (slot_w, slot_h)

    # Base image detached buffer grab
    yield _status(
        f"[{obj.name}] Reading base color for slot {slot_idx+1}/{mats_count} ({mat_name})...",
        slot_base_pct + 0.25 * span,
    )
    detached_base = _read_detached_image_data(base_img_bl, is_color=True)
    eval_col_2d, eval_col_img, is_proc_col = (None, None, False)
    if detached_base is None and p_node is not None and p_node.inputs.get("Base Color") is not None:
        eval_col_2d, eval_col_img, is_proc_col = yield from _evaluate_color_socket_2d_generator(
            p_node.inputs.get("Base Color"),
            staging_dir=staging_dir,
            size=slot_size,
            pct_val=round(slot_base_pct + 0.35 * span, 2),
            label_prefix=f"[{obj.name}] Base color mix",
        )

    # 2. Roughness & Metallic
    yield _status(
        f"[{obj.name}] Evaluating roughness & metallic for slot {slot_idx+1}/{mats_count}...",
        slot_base_pct + 0.45 * span,
    )
    base_roughness = float(obj_state.get("dielectric_roughness", 0.45))
    base_metallic = float(obj_state.get("dielectric_metallic", 0.0))
    r_mix = float(obj_state.get("roughness_map_mix", 1.0))
    m_mix = float(obj_state.get("metallic_map_mix", 1.0))
    if ms is not None:
        if hasattr(ms, "roughness_map_mix") and "roughness_map_mix" in ms:
            r_mix = float(getattr(ms, "roughness_map_mix", r_mix))
        if hasattr(ms, "metallic_map_mix") and "metallic_map_mix" in ms:
            m_mix = float(getattr(ms, "metallic_map_mix", m_mix))
    r_mix = max(0.0, min(1.0, r_mix))
    m_mix = max(0.0, min(1.0, m_mix))

    if p_node is not None:
        r_sock = p_node.inputs.get("Roughness")
        m_sock = p_node.inputs.get("Metallic")
        if r_sock is not None:
            if not r_sock.is_linked:
                base_roughness = float(r_sock.default_value)
            elif r_sock.links and r_sock.links[0].from_node.name == "ReSkate_Roughness_Mix":
                mr_n = r_sock.links[0].from_node
                if "From Min" in mr_n.inputs:
                    mr_n.inputs["From Min"].default_value = 0.0
                if "From Max" in mr_n.inputs:
                    mr_n.inputs["From Max"].default_value = 1.0
                if "reskate_base_val" in mr_n:
                    base_roughness = float(mr_n["reskate_base_val"])
                if "reskate_mix_val" in mr_n:
                    r_mix = max(0.0, min(1.0, float(mr_n["reskate_mix_val"])))
        if m_sock is not None:
            if not m_sock.is_linked:
                base_metallic = float(m_sock.default_value)
            elif m_sock.links and m_sock.links[0].from_node.name == "ReSkate_Metallic_Mix":
                mr_n = m_sock.links[0].from_node
                if "From Min" in mr_n.inputs:
                    mr_n.inputs["From Min"].default_value = 0.0
                if "From Max" in mr_n.inputs:
                    mr_n.inputs["From Max"].default_value = 1.0
                if "reskate_base_val" in mr_n:
                    base_metallic = float(mr_n["reskate_base_val"])
                if "reskate_mix_val" in mr_n:
                    m_mix = max(0.0, min(1.0, float(mr_n["reskate_mix_val"])))

    mask_img_bl = getattr(ms, "mask_image", None) if ms is not None else None
    detached_mask = _read_detached_image_data(mask_img_bl, is_color=False)

    eval_r_2d, eval_r_img_name, eval_r_chan = (None, "", "Color")
    eval_m_2d, eval_m_img_name, eval_m_chan = (None, "", "Color")
    detached_rough = None
    detached_metal = None
    ovr_r_chan = "Color"
    ovr_m_chan = "Color"
    has_distinct_r_override = False
    has_distinct_m_override = False

    if detached_mask is None:
        eval_r_img = None
        if p_node is not None and p_node.inputs.get("Roughness") is not None:
            eval_r_2d, eval_r_img, eval_r_chan = yield from _evaluate_scalar_socket_2d_generator(
                p_node.inputs.get("Roughness"),
                staging_dir=staging_dir,
                size=slot_size,
                pct_val=round(slot_base_pct + 0.50 * span, 2),
                label_prefix=f"[{obj.name}] Roughness: ",
            )
            eval_r_img_name = getattr(eval_r_img, "name", "") if eval_r_img else ""
        ovr_r_img = getattr(ms, "roughness_image", None) if (ms is not None and use_overrides) else None
        ovr_r_chan = getattr(ms, "roughness_channel", "Color") if (ms is not None and use_overrides) else "Color"
        has_distinct_r_override = bool(
            use_overrides and ovr_r_img is not None and _image_stem(ovr_r_img) != _image_stem(eval_r_img)
        )
        if has_distinct_r_override or (use_overrides and ovr_r_img is not None):
            yield _status(
                f"[{obj.name}] Reading roughness override buffer for slot {slot_idx+1}/{mats_count}...",
                slot_base_pct + 0.55 * span,
            )
            detached_rough = _read_detached_image_data(ovr_r_img, is_color=False)

        yield _status(
            f"[{obj.name}] Reading metallic socket for slot {slot_idx+1}/{mats_count}...",
            slot_base_pct + 0.60 * span,
        )
        eval_m_img = None
        if p_node is not None and p_node.inputs.get("Metallic") is not None:
            eval_m_2d, eval_m_img, eval_m_chan = yield from _evaluate_scalar_socket_2d_generator(
                p_node.inputs.get("Metallic"),
                staging_dir=staging_dir,
                size=slot_size,
                pct_val=round(slot_base_pct + 0.65 * span, 2),
                label_prefix=f"[{obj.name}] Metallic: ",
            )
            eval_m_img_name = getattr(eval_m_img, "name", "") if eval_m_img else ""
        ovr_m_img = getattr(ms, "metallic_image", None) if (ms is not None and use_overrides) else None
        ovr_m_chan = getattr(ms, "metallic_channel", "Color") if (ms is not None and use_overrides) else "Color"
        has_distinct_m_override = bool(
            use_overrides and ovr_m_img is not None and _image_stem(ovr_m_img) != _image_stem(eval_m_img)
        )
        if has_distinct_m_override or (use_overrides and ovr_m_img is not None):
            yield _status(
                f"[{obj.name}] Reading metallic override buffer for slot {slot_idx+1}/{mats_count}...",
                slot_base_pct + 0.70 * span,
            )
            detached_metal = _read_detached_image_data(ovr_m_img, is_color=False)

    # 3. Normal Map
    yield _status(
        f"[{obj.name}] Reading normal map for slot {slot_idx+1}/{mats_count}...",
        slot_base_pct + 0.75 * span,
    )
    ms_norm_img = getattr(ms, "normal_image", None) if ms is not None else None
    if _is_internal_wear_or_bake_image(ms_norm_img):
        ms_norm_img = None
    if use_overrides and ms_norm_img is not None:
        norm_img_bl = ms_norm_img
    else:
        norm_img_bl = _normal_image(mat) or (ms_norm_img if use_overrides else None)
    if norm_img_bl is not None:
        yield _status(
            f"[{obj.name}] Reading normal map buffer for slot {slot_idx+1}/{mats_count} ('{norm_img_bl.name}')...",
            slot_base_pct + 0.78 * span,
        )
    detached_norm = _read_detached_image_data(norm_img_bl, is_color=False)

    yield _status(
        f"[{obj.name}] Evaluating normal socket for slot {slot_idx+1}/{mats_count}...",
        slot_base_pct + 0.82 * span,
    )

    normal_strength = 1.0
    if p_node is not None and p_node.inputs.get("Normal") is not None and p_node.inputs["Normal"].is_linked:
        n_link_node = p_node.inputs["Normal"].links[0].from_node
        if n_link_node.type == "NORMAL_MAP":
            normal_strength = max(0.0, min(4.0, _socket_scalar_default(n_link_node.inputs.get("Strength"), 1.0)))

    eval_n_2d, eval_n_img, is_proc_n = (None, None, False)
    if detached_norm is None and p_node is not None and p_node.inputs.get("Normal") is not None:
        eval_n_2d, eval_n_img, is_proc_n = yield from _evaluate_normal_socket_2d_generator(
            p_node.inputs.get("Normal"),
            staging_dir=staging_dir,
            size=slot_size,
            pct_val=round(slot_base_pct + 0.85 * span, 2),
            label_prefix=f"[{obj.name}] ",
        )

    flip_normal_green = bool(getattr(ms, "flip_normal_green", False)) if ms is not None else False

    # 3b. Procedural Wear geometry masks evaluation (Main Thread)
    wear_state = dict(obj_state)
    active_obj = resolve_active_prop_mesh(bpy.context) if bpy.context else None
    if scene_prop is not None and active_obj is not None and active_obj.name == obj.name:
        for wk in (
            "use_procedural_wear",
            "wear_basis",
            "wear_edge_strength",
            "wear_crevice_strength",
            "wear_top_strength",
            "wear_scale",
        ):
            if hasattr(scene_prop, wk):
                wear_state[wk] = getattr(scene_prop, wk)

    wear_masks = None
    wear_basis = "CONCRETE"
    if is_experimental_addon_enabled() and bool(wear_state.get("use_procedural_wear", False)):
        yield _status(
            f"[{obj.name}] Evaluating geometry wear masks...",
            slot_base_pct + 0.85 * span,
        )
        w_scale = max(0.10, min(2.00, float(wear_state.get("wear_scale", 0.50))))
        wear_masks = compute_uv_geometry_wear_masks(obj, size=slot_size, wear_scale=w_scale)
        wear_basis = _resolve_effective_wear_basis(wear_state, mat, base_metallic)

    # 4. Emission
    yield _status(
        f"[{obj.name}] Reading emission & opacity for slot {slot_idx+1}/{mats_count}...",
        slot_base_pct + 0.88 * span,
    )
    em_img_bl, em_col, em_strength = _emission_image_and_strength(mat)
    if ms is not None:
        ms_em_img = getattr(ms, "emission_image", None)
        ms_em_str = float(getattr(ms, "emission_strength", 0.0))
        ms_em_col = getattr(ms, "emission_color", (0.0, 0.0, 0.0))
        if use_overrides or (em_strength <= 0.001 and ms_em_img is not None):
            if ms_em_img is not None:
                em_img_bl = ms_em_img
            if ms_em_str > 0.001 or use_overrides:
                em_strength = ms_em_str if ms_em_str > 0.001 else (1.0 if ms_em_img is not None else 0.0)
            if ms_em_col is not None and len(ms_em_col) >= 3:
                em_col = [float(ms_em_col[0]), float(ms_em_col[1]), float(ms_em_col[2])]
    detached_em = _read_detached_image_data(em_img_bl, is_color=True)

    # 5. Opacity / Alpha
    if use_overrides and ms is not None and getattr(ms, "opacity_image", None) is not None:
        op_img_bl = getattr(ms, "opacity_image", None)
    else:
        op_img_bl = _opacity_image(mat) or (getattr(ms, "opacity_image", None) if (ms is not None and use_overrides) else None)
    detached_op = None
    if op_img_bl is not None and op_img_bl != base_img_bl:
        detached_op = _read_detached_image_data(op_img_bl, is_color=False)

    has_linked_bsdf_alpha = bool(
        p_node is not None and p_node.inputs.get("Alpha") is not None and p_node.inputs["Alpha"].is_linked
    )
    has_sub_one_bsdf_alpha = bool(
        p_node is not None
        and p_node.inputs.get("Alpha") is not None
        and not p_node.inputs["Alpha"].is_linked
        and float(p_node.inputs["Alpha"].default_value) < 0.995
    )
    bsdf_alpha_default = float(p_node.inputs["Alpha"].default_value) if (p_node is not None and p_node.inputs.get("Alpha") is not None) else 1.0

    eval_a_2d = None
    if has_linked_bsdf_alpha:
        eval_a_2d, _, _ = yield from _evaluate_scalar_socket_2d_generator(
            p_node.inputs["Alpha"],
            staging_dir=staging_dir,
            size=slot_size,
            pct_val=round(slot_base_pct + 0.90 * span, 2),
            label_prefix=f"[{obj.name}] Alpha: ",
        )

    is_alpha_or_decal_slot = bool(
        is_decal_slot
        or has_linked_bsdf_alpha
        or has_sub_one_bsdf_alpha
        or op_img_bl is not None
    )

    synced_surf = get_synced_material_surface_info(obj, slot_idx, mat)

    return {
        "slot_idx": slot_idx,
        "mat_name": mat_name,
        "prop_slug": prop_slug,
        "staging_dir": str(staging_dir),
        "slot_size": (slot_w, slot_h),
        "base_col": base_col,
        "detached_base": detached_base,
        "eval_col_2d": eval_col_2d,
        "is_proc_col": is_proc_col,
        "detached_mask": detached_mask,
        "detached_norm": detached_norm,
        "normal_strength": normal_strength,
        "eval_n_2d": eval_n_2d,
        "is_proc_n": is_proc_n,
        "flip_normal_green": flip_normal_green,
        "base_roughness": base_roughness,
        "base_metallic": base_metallic,
        "r_mix": r_mix,
        "m_mix": m_mix,
        "eval_r_2d": eval_r_2d,
        "eval_r_img_name": eval_r_img_name,
        "eval_r_chan": eval_r_chan,
        "has_distinct_r_override": has_distinct_r_override,
        "detached_rough": detached_rough,
        "ovr_r_chan": ovr_r_chan,
        "eval_m_2d": eval_m_2d,
        "eval_m_img_name": eval_m_img_name,
        "eval_m_chan": eval_m_chan,
        "has_distinct_m_override": has_distinct_m_override,
        "detached_metal": detached_metal,
        "ovr_m_chan": ovr_m_chan,
        "preset_vals": preset_vals,
        "domain": domain,
        "wear_masks": wear_masks,
        "wear_basis": wear_basis,
        "wear_state": wear_state,
        "detached_em": detached_em,
        "em_col": em_col,
        "em_strength": em_strength,
        "detached_op": detached_op,
        "is_alpha_or_decal_slot": is_alpha_or_decal_slot,
        "has_linked_bsdf_alpha": has_linked_bsdf_alpha,
        "has_sub_one_bsdf_alpha": has_sub_one_bsdf_alpha,
        "bsdf_alpha_default": bsdf_alpha_default,
        "eval_a_2d": eval_a_2d,
        "is_decal_slot": is_decal_slot,
        "decal_alpha_mode": decal_alpha_mode,
        "decal_shader_mode": decal_shader_mode,
        "synced_surf": synced_surf,
        "invisible": invisible,
        "srgb": srgb,
        "alpha_mode": alpha_mode,
        "alpha_source": alpha_source,
        "compress_level": 1,
    }


def _process_slot_textures_offthread(payload: dict) -> dict:
    """
    Worker-thread ONLY: Performs pure NumPy and PIL operations: Lanczos resizing,
    sRGB math, normal map normalization & green flip, channel packing, and atomic
    PNG writes. Zero bpy calls.
    """
    cancel_token = payload.get("cancel_token")
    if cancel_token and cancel_token.is_set():
        return {}

    staging_dir = Path(payload["staging_dir"])
    prop_slug = payload["prop_slug"]
    slot_idx = payload["slot_idx"]
    slot_w, slot_h = payload["slot_size"]
    slot_size = (slot_w, slot_h)
    mat_name = payload["mat_name"]
    compress_level = payload.get("compress_level", 1)

    # 1. Decode detached images
    pil_base = _decode_detached_image(payload["detached_base"])
    pil_norm = _decode_detached_image(payload["detached_norm"])
    pil_mask = _decode_detached_image(payload["detached_mask"])
    pil_rough = _decode_detached_image(payload.get("detached_rough"))
    pil_metal = _decode_detached_image(payload.get("detached_metal"))
    pil_em = _decode_detached_image(payload["detached_em"])
    pil_op = _decode_detached_image(payload["detached_op"])

    # 2. Base Color & Albedo
    rgba_arr = np.ones((slot_h, slot_w, 4), dtype=np.float32)
    has_image_texture = False
    source_image_name = ""
    base_col = list(payload["base_col"])

    if pil_base is not None:
        pil_base_res = pil_base.resize(slot_size, Image.Resampling.LANCZOS)
        rgba_arr = np.asarray(pil_base_res, dtype=np.float32) / 255.0
        has_image_texture = True
        source_image_name = str(payload["detached_base"].get("name", ""))
    elif payload.get("eval_col_2d") is not None and payload.get("is_proc_col"):
        eval_col_2d = payload["eval_col_2d"]
        rgba_arr[:, :, :3] = np.clip(eval_col_2d, 0.0, 1.0)
        has_image_texture = True
        source_image_name = f"Procedural ({mat_name})"
        base_col = [
            float(rgba_arr[:, :, 0].mean()),
            float(rgba_arr[:, :, 1].mean()),
            float(rgba_arr[:, :, 2].mean()),
        ]
    else:
        has_image_texture = False
        source_image_name = ""
        rgba_arr[:, :, 0] = base_col[0]
        rgba_arr[:, :, 1] = base_col[1]
        rgba_arr[:, :, 2] = base_col[2]

    # 3. Roughness & Metallic
    base_roughness = payload["base_roughness"]
    base_metallic = payload["base_metallic"]
    r_mix = payload["r_mix"]
    m_mix = payload["m_mix"]
    mat_roughness = base_roughness
    mat_metallic = base_metallic

    rough_map_2d = None
    metal_map_2d = None
    has_roughness_map = False
    roughness_map_name = ""
    has_metallic_map = False
    metallic_map_name = ""

    if pil_mask is not None:
        msk_resized = pil_mask.resize(slot_size, Image.Resampling.LANCZOS)
        msk_arr = np.asarray(msk_resized, dtype=np.float32) / 255.0
        raw_metal_2d = msk_arr[:, :, 0]
        raw_rough_2d = 1.0 - msk_arr[:, :, 1]
        metal_map_2d = np.clip((1.0 - m_mix) * base_metallic + m_mix * raw_metal_2d, 0.0, 1.0)
        rough_map_2d = np.clip((1.0 - r_mix) * base_roughness + r_mix * raw_rough_2d, 0.0, 1.0)
        mat_metallic = float(metal_map_2d.mean())
        mat_roughness = float(rough_map_2d.mean())
        has_roughness_map = True
        has_metallic_map = True
        mask_nm = payload["detached_mask"].get("name", "")
        roughness_map_name = f"{mask_nm} (G)" if mask_nm else "Mask (G)"
        metallic_map_name = f"{mask_nm} (R)" if mask_nm else "Mask (R)"
    else:
        eval_r_2d = payload.get("eval_r_2d")
        has_distinct_r_override = payload.get("has_distinct_r_override", False)
        if eval_r_2d is not None and not has_distinct_r_override:
            raw_rough_2d = np.clip(eval_r_2d, 0.0, 1.0)
            rough_map_2d = np.clip((1.0 - r_mix) * base_roughness + r_mix * raw_rough_2d, 0.0, 1.0)
            mat_roughness = float(rough_map_2d.mean())
            has_roughness_map = True
            r_img_nm = payload.get("eval_r_img_name", "")
            r_chan = payload.get("eval_r_chan", "Color")
            roughness_map_name = f"{r_img_nm} ({r_chan})" if r_img_nm else "Roughness Node Chain"
        elif has_distinct_r_override or pil_rough is not None:
            if pil_rough is not None:
                r_img_nm = payload["detached_rough"].get("name", "") if payload.get("detached_rough") else ""
                raw_rough_2d, used_r_ch = _extract_channel_map(
                    pil_rough,
                    payload.get("ovr_r_chan", "Color"),
                    slot_size,
                    image_name=r_img_nm,
                    return_channel_used=True,
                )
                rough_map_2d = np.clip((1.0 - r_mix) * base_roughness + r_mix * raw_rough_2d, 0.0, 1.0)
                mat_roughness = float(rough_map_2d.mean())
                has_roughness_map = True
                roughness_map_name = f"{r_img_nm} ({used_r_ch})" if r_img_nm else "Roughness Map"

        eval_m_2d = payload.get("eval_m_2d")
        has_distinct_m_override = payload.get("has_distinct_m_override", False)
        if eval_m_2d is not None and not has_distinct_m_override:
            raw_metal_2d = np.clip(eval_m_2d, 0.0, 1.0)
            metal_map_2d = np.clip((1.0 - m_mix) * base_metallic + m_mix * raw_metal_2d, 0.0, 1.0)
            mat_metallic = float(metal_map_2d.mean())
            has_metallic_map = True
            m_img_nm = payload.get("eval_m_img_name", "")
            m_chan = payload.get("eval_m_chan", "Color")
            metallic_map_name = f"{m_img_nm} ({m_chan})" if m_img_nm else "Metallic Node Chain"
        elif has_distinct_m_override or pil_metal is not None:
            if pil_metal is not None:
                m_img_nm = payload["detached_metal"].get("name", "") if payload.get("detached_metal") else ""
                raw_metal_2d, used_m_ch = _extract_channel_map(
                    pil_metal,
                    payload.get("ovr_m_chan", "Color"),
                    slot_size,
                    image_name=m_img_nm,
                    return_channel_used=True,
                )
                metal_map_2d = np.clip((1.0 - m_mix) * base_metallic + m_mix * raw_metal_2d, 0.0, 1.0)
                mat_metallic = float(metal_map_2d.mean())
                has_metallic_map = True
                metallic_map_name = f"{m_img_nm} ({used_m_ch})" if m_img_nm else "Metallic Map"

    if payload.get("preset_vals") is not None:
        p_vals = payload["preset_vals"]
        if rough_map_2d is None:
            mat_roughness = float(p_vals["roughness"])
        if metal_map_2d is None:
            mat_metallic = float(p_vals["metallic"])

    domain = payload.get("domain", "surface")
    if domain == "glass_opaque":
        mat_roughness = min(mat_roughness, 0.10)
        mat_metallic = max(mat_metallic, 0.85)

    # 4. Normal Map
    if cancel_token and cancel_token.is_set():
        return {}

    norm_01_2d = None
    has_normal_map = pil_norm is not None
    normal_map_name = str(payload["detached_norm"].get("name", "")) if (payload.get("detached_norm") and has_normal_map) else ""

    if pil_norm is not None:
        norm_resized = pil_norm.resize(slot_size, Image.Resampling.LANCZOS).convert("RGBA")
        norm_01_2d = (np.asarray(norm_resized, dtype=np.float32)[:, :, :3] / 255.0).copy()
        n_str = payload.get("normal_strength", 1.0)
        if abs(n_str - 1.0) > 1e-3:
            n_xyz = norm_01_2d * 2.0 - 1.0
            n_xyz[:, :, 0] *= n_str
            n_xyz[:, :, 1] *= n_str
            norm_len = np.linalg.norm(n_xyz, axis=2, keepdims=True)
            n_xyz = np.where(norm_len > 1e-6, n_xyz / np.maximum(norm_len, 1e-6), np.array([[[0.0, 0.0, 1.0]]], dtype=np.float32))
            norm_01_2d = np.clip(n_xyz * 0.5 + 0.5, 0.0, 1.0).astype(np.float32)
        has_normal_map = True
    elif payload.get("eval_n_2d") is not None and payload.get("is_proc_n"):
        norm_01_2d = np.clip(payload["eval_n_2d"], 0.0, 1.0).astype(np.float32)
        has_normal_map = True
        if not normal_map_name:
            normal_map_name = f"Procedural Bump ({mat_name})"

    if norm_01_2d is not None and payload.get("flip_normal_green", False):
        norm_01_2d = norm_01_2d.copy()
        norm_01_2d[:, :, 1] = np.clip(1.0 - norm_01_2d[:, :, 1], 0.0, 1.0)

    # 5. Procedural Wear
    wear_masks = payload.get("wear_masks")
    if wear_masks is not None:
        rgba_arr, rough_map_2d, mat_roughness, metal_map_2d, mat_metallic, norm_01_2d = (
            _apply_precomputed_wear_offthread(
                payload["wear_state"],
                wear_masks,
                payload.get("wear_basis", "CONCRETE"),
                rgba_arr,
                rough_map_2d,
                mat_roughness,
                metal_map_2d,
                mat_metallic,
                norm_01_2d,
            )
        )
        has_image_texture = True
        has_roughness_map = True
        if not source_image_name:
            source_image_name = f"Procedural Wear ({mat_name})"
        if not roughness_map_name:
            roughness_map_name = f"Procedural Wear Roughness ({mat_name})"
        if norm_01_2d is not None:
            has_normal_map = True
            if not normal_map_name:
                normal_map_name = f"Procedural Wear Normal ({mat_name})"

    mat_roughness = max(0.0, min(1.0, mat_roughness))
    mat_metallic = max(0.0, min(1.0, mat_metallic))
    mat_smoothness = max(0.0, min(1.0, 1.0 - mat_roughness))
    mat_specular = max(0.50, min(1.0, 0.45 + 0.45 * mat_metallic))

    slot_norm_path = None
    if norm_01_2d is not None:
        if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            return None
        n_u8 = np.full((slot_h, slot_w, 4), 255, dtype=np.uint8)
        n_u8[:, :, :3] = np.clip(np.round(norm_01_2d * 255.0), 0, 255).astype(np.uint8)
        slot_norm_path = staging_dir / f"ReSkate_Mat_{prop_slug}_{slot_idx}_n.png"
        if not _atomic_png_save(Image.fromarray(n_u8, mode="RGBA"), slot_norm_path, compress_level=compress_level, cancel_token=cancel_token):
            return None

    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        return None

    msk_rgba = np.full((slot_h, slot_w, 4), 255, dtype=np.uint8)
    if metal_map_2d is not None:
        msk_rgba[:, :, 0] = np.clip(np.round(metal_map_2d * 255.0), 0, 255).astype(np.uint8)
    else:
        msk_rgba[:, :, 0] = int(max(0, min(255, round(mat_metallic * 255.0))))
    if rough_map_2d is not None:
        smooth_2d = np.clip(1.0 - rough_map_2d, 0.0, 1.0)
        msk_rgba[:, :, 1] = np.clip(np.round(smooth_2d * 255.0), 0, 255).astype(np.uint8)
    else:
        msk_rgba[:, :, 1] = int(max(0, min(255, round(mat_smoothness * 255.0))))
    slot_msk_path = staging_dir / f"ReSkate_Mat_{prop_slug}_{slot_idx}_msk.png"
    mask_saved = _atomic_png_save(Image.fromarray(msk_rgba, mode="RGBA"), slot_msk_path, compress_level=compress_level, cancel_token=cancel_token)
    if not mask_saved and cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        return None

    # 6. Emission
    em_strength = payload.get("em_strength", 0.0)
    em_col = payload.get("em_col", [0.0, 0.0, 0.0])
    if em_strength > 0.001:
        weight = min(float(em_strength), 2.5) * 0.55
        if pil_em is not None:
            em_arr = np.asarray(pil_em.resize(slot_size, Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
            rgba_arr[:, :, :3] = np.clip(rgba_arr[:, :, :3] + em_arr[:, :, :3] * weight, 0.0, 1.0)
        elif max(em_col) > 0.01:
            if not has_image_texture:
                if em_strength >= 1.2 or "emissive" in mat_name.lower() or "glow" in mat_name.lower():
                    base_col = [1.0, 1.0, 1.0]
                else:
                    base_col = [
                        max(0.25, min(1.0, float(em_col[0]))),
                        max(0.25, min(1.0, float(em_col[1]))),
                        max(0.25, min(1.0, float(em_col[2]))),
                    ]
                rgba_arr[:, :, 0] = base_col[0]
                rgba_arr[:, :, 1] = base_col[1]
                rgba_arr[:, :, 2] = base_col[2]
                mat_roughness = min(mat_roughness, 0.04)
                mat_smoothness = max(mat_smoothness, 0.96)
                msk_rgba[:, :, 1] = 250
                mask_saved = _atomic_png_save(Image.fromarray(msk_rgba, mode="RGBA"), slot_msk_path, compress_level=compress_level, cancel_token=cancel_token)
                if not mask_saved and cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
                    return None
            else:
                em_vec = np.array(em_col, dtype=np.float32)[None, None, :] * weight
                rgba_arr[:, :, :3] = np.clip(rgba_arr[:, :, :3] + em_vec, 0.0, 1.0)

    # 7. Opacity / Alpha
    if pil_op is not None:
        op_rgba = np.asarray(pil_op.resize(slot_size, Image.Resampling.LANCZOS).convert("RGBA"), dtype=np.float32) / 255.0
        if float(np.min(op_rgba[:, :, 3])) < 0.96 and float(np.std(op_rgba[:, :, 3])) > 1e-3:
            rgba_arr[:, :, 3] = op_rgba[:, :, 3]
        else:
            rgba_arr[:, :, 3] = _extract_channel_map(pil_op, "Red", slot_size)

    is_alpha_or_decal_slot = payload.get("is_alpha_or_decal_slot", False)
    is_decal_slot = payload.get("is_decal_slot", False)
    decal_alpha_mode = payload.get("decal_alpha_mode", "AUTO")
    decal_shader_mode = payload.get("decal_shader_mode", "CUTOUT_273")
    alpha_mode = payload.get("alpha_mode", "auto")

    if is_alpha_or_decal_slot:
        eval_a_2d = payload.get("eval_a_2d")
        if eval_a_2d is None and payload.get("has_sub_one_bsdf_alpha"):
            eval_a_2d = np.full((slot_h, slot_w), payload["bsdf_alpha_default"], dtype=np.float32)

        img_alpha_2d = rgba_arr[:, :, 3].copy()
        if decal_alpha_mode == 'IMAGE_ALPHA':
            decal_a = img_alpha_2d
        elif decal_alpha_mode == 'BSDF_ALPHA':
            decal_a = eval_a_2d if eval_a_2d is not None else img_alpha_2d
        elif decal_alpha_mode == 'LUMINANCE_BLACK':
            lum = np.max(rgba_arr[:, :, :3], axis=2)
            decal_a = np.clip((lum - 0.03) / 0.22, 0.0, 1.0)
        elif decal_alpha_mode == 'LUMINANCE_WHITE':
            lum = np.min(rgba_arr[:, :, :3], axis=2)
            decal_a = np.clip((0.97 - lum) / 0.22, 0.0, 1.0)
        else:  # 'AUTO'
            if eval_a_2d is not None and float(np.min(img_alpha_2d)) < 0.98:
                decal_a = np.minimum(np.clip(eval_a_2d, 0.0, 1.0), img_alpha_2d)
            elif eval_a_2d is not None and float(np.min(eval_a_2d)) < 0.98:
                decal_a = np.clip(eval_a_2d, 0.0, 1.0)
            else:
                decal_a = img_alpha_2d
            if is_decal_slot and float(np.min(decal_a)) >= 0.98:
                lum = np.max(rgba_arr[:, :, :3], axis=2)
                if float(np.mean(lum < 0.08)) > 0.10:
                    decal_a = np.clip((lum - 0.03) / 0.22, 0.0, 1.0)

        # 1) Un-premultiply RGB
        rgb_decal = rgba_arr[:, :, :3].copy()
        max_rgb = np.max(rgb_decal, axis=2)
        semi_mask = (decal_a > 0.02) & (decal_a < 0.95) & (max_rgb <= decal_a + 0.08)
        if np.any(semi_mask):
            safe_a = np.maximum(decal_a[:, :, None], 0.15)
            rgb_decal = np.where(semi_mask[:, :, None], np.clip(rgb_decal / safe_a, 0.0, 1.0), rgb_decal)
            max_rgb = np.max(rgb_decal, axis=2)

        # 2) Black background suppression
        if is_decal_slot and decal_alpha_mode in ('AUTO', 'LUMINANCE_BLACK'):
            vis_px = max_rgb[decal_a > 0.05]
            if vis_px.size > 0 and float(np.percentile(vis_px, 95)) > 0.45 and float(np.mean(max_rgb < 0.05)) > 0.20:
                black_bg_suppression = np.clip((max_rgb - 0.02) / 0.18, 0.0, 1.0)
                decal_a = decal_a * black_bg_suppression

        # 3) Dilate foreground RGB
        valid_fg = decal_a >= 0.06
        fg_ratio = float(np.mean(valid_fg))
        if 0.001 < fg_ratio < 0.995:
            v_mask = valid_fg.copy()
            for _ in range(6):
                inv_m = ~v_mask
                if not np.any(inv_m):
                    break
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                    sh_v = np.roll(np.roll(v_mask, dy, axis=0), dx, axis=1)
                    sh_rgb = np.roll(np.roll(rgb_decal, dy, axis=0), dx, axis=1)
                    fill = inv_m & sh_v
                    if np.any(fill):
                        rgb_decal[fill] = sh_rgb[fill]
                        v_mask[fill] = True
                        inv_m = ~v_mask

        rgba_arr[:, :, :3] = rgb_decal
        rgba_arr[:, :, 3] = np.clip(decal_a, 0.0, 1.0)
        has_alpha_transparency = bool(is_decal_slot or float(np.min(rgba_arr[:, :, 3])) < 0.98)
        if has_alpha_transparency and str(alpha_mode).lower() not in ("masked", "clip", "blend"):
            alpha_mode = "masked"
    else:
        rgba_arr[:, :, 3] = 1.0
        has_alpha_transparency = False
        alpha_mode = "opaque"

    msk_g_is_roughness = False
    msk_g_is_curved_smoothness = False
    if has_alpha_transparency and decal_shader_mode != "METALLIC_771":
        msk_rgba[:, :, 0] = np.clip(np.round(rgba_arr[:, :, 3] * 255.0), 0, 255).astype(np.uint8)
        if rough_map_2d is not None:
            smooth_curved = np.power(np.clip(1.0 - np.clip(rough_map_2d, 0.0, 1.0), 0.0, 1.0), 2.2)
            msk_rgba[:, :, 1] = np.clip(np.round(smooth_curved * 255.0), 0, 255).astype(np.uint8)
        else:
            s_val = pow(max(0.0, min(1.0, 1.0 - mat_roughness)), 2.2)
            msk_rgba[:, :, 1] = int(max(0, min(255, round(s_val * 255.0))))
        msk_rgba[:, :, 2] = 0
        msk_rgba[:, :, 3] = msk_rgba[:, :, 0]
        msk_g_is_curved_smoothness = True
        mask_saved = _atomic_png_save(Image.fromarray(msk_rgba, mode="RGBA"), slot_msk_path, compress_level=compress_level, cancel_token=cancel_token)
        if not mask_saved and cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            return None

    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        return None

    out_u8 = np.clip(np.round(rgba_arr * 255.0), 0, 255).astype(np.uint8)
    slot_png_path = staging_dir / f"ReSkate_Mat_{prop_slug}_{slot_idx}.png"
    if not _atomic_png_save(Image.fromarray(out_u8, mode="RGBA"), slot_png_path, compress_level=compress_level, cancel_token=cancel_token):
        return None

    synced_surf = payload["synced_surf"]
    return {
        "slot_index": slot_idx,
        "material_name": mat_name,
        "texture_path": str(slot_png_path),
        "normal_texture_path": str(slot_norm_path) if slot_norm_path else None,
        "mask_texture_path": str(slot_msk_path) if mask_saved else None,
        "has_image_texture": bool(has_image_texture),
        "has_alpha_transparency": bool(has_alpha_transparency),
        "is_decal_slot": bool(is_decal_slot),
        "decal_alpha_mode": decal_alpha_mode,
        "decal_shader_mode": decal_shader_mode,
        "msk_g_is_roughness": bool(msk_g_is_roughness),
        "msk_g_is_curved_smoothness": bool(msk_g_is_curved_smoothness),
        "is_composited_decal": False,
        "source_image_name": source_image_name,
        "has_roughness_map": bool(has_roughness_map),
        "roughness_map_name": roughness_map_name,
        "has_metallic_map": bool(has_metallic_map),
        "metallic_map_name": metallic_map_name,
        "has_normal_map": bool(has_normal_map),
        "normal_map_name": normal_map_name,
        "raw_roughness": float(mat_roughness),
        "raw_specular": float(mat_specular),
        "roughness": round(mat_roughness, 4),
        "smoothness": round(mat_smoothness, 4),
        "metallic": round(mat_metallic, 4),
        "specular": round(mat_specular, 4),
        "emission_strength": round(float(em_strength), 4),
        "emission_color": [round(float(c), 4) for c in em_col],
        "domain": domain,
        "srgb": payload.get("srgb", True),
        "alpha": alpha_mode,
        "alpha_source": payload.get("alpha_source", "auto"),
        "invisible": payload.get("invisible", False),
        "base_color": [round(c, 4) for c in base_col],
        "collision_surface": synced_surf["collision_preset"],
        "contact_audio": synced_surf["contact_audio"],
    }


def extract_material_textures_and_pbr_generator(
    obj: bpy.types.Object | None,
    prop_slug: str,
    staging_dir: Path,
    start_pct: float = 10.0,
    end_pct: float = 40.0,
):
    """
    Generator that performs progressive main-thread shader extraction, immediately
    submitting each slot payload to a ThreadPoolExecutor (max 4 workers) so background
    threads process slot 0 while slot 1 is extracted on the main thread.
    Yields ProgressStatus at sub-350ms cadence throughout extraction and polling.
    Cleanly cancels on GeneratorExit without partial files.
    """
    clear_ephemeral_shader_caches()
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    if not obj or obj.type != 'MESH':
        return [], 0.45, 0.50

    slots = list(obj.material_slots)
    mats = [s.material for s in slots] if slots else [None]
    n_slots = len(mats)

    obj_state = get_or_init_object_prop_state(obj)
    scene_prop = getattr(bpy.context.scene, "reskate_prop", None) if (bpy.context and getattr(bpy.context, "scene", None)) else None
    active_obj = resolve_active_prop_mesh(bpy.context) if bpy.context else None
    if scene_prop is not None and active_obj is not None and active_obj.name == obj.name:
        active_look_preset = getattr(scene_prop, "look_preset", obj_state.get("look_preset", "CUSTOM"))
    else:
        active_look_preset = str(obj_state.get("look_preset", "CUSTOM"))
    preset_vals = LOOK_PRESETS.get(active_look_preset) if active_look_preset != "CUSTOM" else None

    raw_atlas_res = getattr(scene_prop, "atlas_resolution", obj_state.get("atlas_resolution", "2048")) if scene_prop else obj_state.get("atlas_resolution", "2048")
    try:
        target_atlas_res = int(str(raw_atlas_res or "2048"))
    except ValueError:
        target_atlas_res = 2048

    last_pct = start_pct

    def _status(label: str, pct: float) -> ProgressStatus:
        nonlocal last_pct
        clamped_pct = min(end_pct, max(last_pct, round(float(pct), 2)))
        last_pct = clamped_pct
        return ProgressStatus(label, clamped_pct)

    yield _status(f"Preparing material extraction for '{obj.name}'...", start_pct)

    cancel_token = threading.Event()
    norm_stem = re.sub(r"[^a-z0-9]", "_", str(prop_slug or "").lower()).strip("_")
    # Sweep any preexisting stale tmp files for this prop slug at generator entry without recursive rglob traversal
    yield _status(f"Cleaning temporary files for '{obj.name}'...", start_pct)
    sweep_dirs = [staging_dir]
    if staging_dir.name == "Pack_Queue" and staging_dir.parent.exists():
        sweep_dirs.append(staging_dir.parent)
    stale_tmps = []
    for s_dir in sweep_dirs:
        if s_dir.exists():
            for old_tmp in s_dir.glob("*.tmp*"):
                if norm_stem in old_tmp.name.lower() or "ReSkate_Mat_" in old_tmp.name:
                    stale_tmps.append(old_tmp)
    for old_tmp in stale_tmps:
        _safe_unlink(old_tmp)
        yield _status(f"Cleaned stale temporary file for '{obj.name}' [{old_tmp.name}]...", start_pct)

    yield _status(f"Starting worker threads for '{obj.name}'...", start_pct)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, os.cpu_count() or 4),
        thread_name_prefix="ReSkate_TexWorker",
    )
    yield _status(f"Initializing texture extraction pool for '{obj.name}'...", start_pct)

    futures = []
    normal_completion = False
    slot_results = []

    try:
        extract_span = (end_pct - start_pct) * 0.55
        for idx, mat in enumerate(mats):
            mat_disp = mat.name if mat else f"Slot_{idx}"
            s_base_pct = round(start_pct + (idx / max(1, n_slots)) * extract_span, 2)
            s_end_pct = round(start_pct + ((idx + 1) / max(1, n_slots)) * extract_span, 2)

            yield _status(
                f"Extracting shader inputs for '{obj.name}' [{idx+1}/{n_slots}: {mat_disp}]...",
                s_base_pct,
            )

            payload = yield from _extract_slot_inputs_mainthread_generator(
                obj=obj,
                slot_idx=idx,
                mat=mat,
                mats_count=n_slots,
                prop_slug=prop_slug,
                staging_dir=staging_dir,
                target_atlas_res=target_atlas_res,
                obj_state=obj_state,
                scene_prop=scene_prop,
                preset_vals=preset_vals,
                slot_base_pct=s_base_pct,
                slot_end_pct=s_end_pct,
            )

            payload["cancel_token"] = cancel_token
            fut = executor.submit(_process_slot_textures_offthread, payload)
            futures.append(fut)

            done_cnt = sum(1 for f in futures if f.done())
            yield _status(
                f"Submitted slot {idx+1}/{n_slots} for '{obj.name}' ({done_cnt}/{len(futures)} off-thread done)...",
                s_end_pct,
            )

        n_fut = len(futures)
        poll_base_pct = start_pct + extract_span
        poll_span = end_pct - poll_base_pct

        while not all(f.done() for f in futures):
            done_cnt = sum(1 for f in futures if f.done())
            poll_pct = round(poll_base_pct + (done_cnt / max(1, n_fut)) * poll_span, 2)
            yield _status(
                f"Processing PBR textures for '{obj.name}' ({done_cnt}/{n_fut} slots)...",
                poll_pct,
            )
            time.sleep(0.01)

        poll_pct = round(poll_base_pct + poll_span, 2)
        yield _status(f"Verifying extracted PBR textures for '{obj.name}' on disk...", poll_pct)

        slot_results = [f.result() for f in futures]
        normal_completion = True
    finally:
        if not normal_completion:
            # 1. Signal cancellation immediately to all active worker threads
            cancel_token.set()
            # 2. Cancel all queued futures that have not yet started
            for fut in futures:
                fut.cancel()
            # 3. Wait up to 3.0s for active workers to observe cancel_token and abort
            concurrent.futures.wait(futures, timeout=3.0)
            # 4. Join and shutdown executor cleanly with wait=True so workers release OS handles
            executor.shutdown(wait=True, cancel_futures=True)
            # 5. Sweep and unlink with retry loop to ensure OS file handles are closed by Windows
            sweep_targets = {staging_dir}
            if staging_dir.name == "Pack_Queue":
                sweep_targets.add(staging_dir.parent)
            for attempt in range(5):
                time.sleep(0.02 * (attempt + 1))
                with _ACTIVE_TEMP_LOCK:
                    active_snapshot = list(_ACTIVE_TEMP_FILES)
                for act_path in active_snapshot:
                    if _safe_unlink(act_path, max_retries=8):
                        with _ACTIVE_TEMP_LOCK:
                            _ACTIVE_TEMP_FILES.discard(act_path)
                leftovers = []
                for s_dir in sweep_targets:
                    if s_dir.exists():
                        for leftover in s_dir.glob("*.tmp*"):
                            leftovers.append(leftover)
                            _safe_unlink(leftover, max_retries=8)
                if not leftovers and not active_snapshot:
                    break

    yield _status(f"Joining texture worker threads for '{obj.name}'...", poll_pct)
    executor.shutdown(wait=True, cancel_futures=False)

    # Sliced NTFS read verification barrier on all written output texture paths
    for slot_idx_res, mt in enumerate(slot_results or []):
        for k in ("texture_path", "mask_texture_path", "normal_texture_path"):
            p_str = mt.get(k)
            if not p_str:
                continue
            p = Path(p_str)
            yield _status(f"Verifying disk commit for '{obj.name}' [{p.name}]...", poll_pct)
            for _retry in range(8):
                try:
                    if p.exists() and p.is_file() and p.stat().st_size > 0:
                        with open(p, "rb") as f:
                            if f.read(1):
                                break
                except (PermissionError, OSError):
                    pass
                yield _status(f"Waiting for disk commit for '{obj.name}' [{p.name}] (attempt {_retry+1}/8)...", poll_pct)
                time.sleep(0.015 * (_retry + 1))

    # 3. Main-thread post-processing: asset cache refresh & average roughness/specular
    material_textures = slot_results
    rough_vals = []
    spec_vals = []

    for slot_idx_res, mt in enumerate(material_textures):
        slot_png_path = Path(mt["texture_path"])
        slot_msk_path = Path(mt["mask_texture_path"]) if mt.get("mask_texture_path") else None
        slot_norm_path = Path(mt["normal_texture_path"]) if mt.get("normal_texture_path") else None
        idx = mt["slot_index"]

        yield _status(
            f"Synchronizing pack assets cache for '{obj.name}' (slot {slot_idx_res+1}/{len(material_textures)})...",
            poll_pct,
        )
        sync_files = [slot_png_path] + ([slot_msk_path] if slot_msk_path else []) + ([slot_norm_path] if slot_norm_path else [])
        stale_names = [] if slot_norm_path else [f"ReSkate_Mat_{prop_slug}_{idx}_n.png"]
        _refresh_pack_cached_texture_files(staging_dir, sync_files, stale_names)

        raw_r = mt.pop("raw_roughness", mt.get("roughness", 0.45))
        raw_s = mt.pop("raw_specular", mt.get("specular", 0.50))
        if not mt.get("invisible", False):
            rough_vals.append(float(raw_r))
            spec_vals.append(float(raw_s))

    avg_rough = float(sum(rough_vals) / len(rough_vals)) if rough_vals else 0.45
    avg_spec = float(sum(spec_vals) / len(spec_vals)) if spec_vals else 0.50

    yield _status(f"Texture extraction complete for '{obj.name}'", end_pct)
    return material_textures, avg_rough, avg_spec


def extract_material_textures_and_pbr(
    obj: bpy.types.Object | None,
    prop_slug: str,
    staging_dir: Path,
) -> tuple[list[dict], float, float]:
    """
    Synchronous wrapper for extract_material_textures_and_pbr_generator.
    Reads `mat.sk8_material` and Principled BSDF nodes for each material slot on `obj`,
    compositing normal-map micro-relief, roughness detail, emission, and opacity
    into per-slot PNGs via ThreadPoolExecutor.
    Returns `(material_textures_list, avg_roughness, avg_specular)`.
    """
    gen = extract_material_textures_and_pbr_generator(obj, prop_slug, staging_dir)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def ensure_texture_atlas(
    context,
    obj: bpy.types.Object,
    prop_slug_or_dir,
    staging_dir_or_slug=None,
    pre_extracted_materials: list[dict] | None = None,
) -> Path:
    """
    Non-destructively resolves the prop's texture:
    - Path A ('AS_IS', default): extracts per-material textures and Skate Map / Principled BSDF
      PBR channels directly without touching or repacking UVs!
    - Path B ('BAKE', optional): uses or bakes `ReSkate_Atlas_<slug>.png` only when explicitly requested.
    """
    if isinstance(prop_slug_or_dir, Path):
        staging_dir = prop_slug_or_dir
        prop_slug = str(staging_dir_or_slug or get_prop_slug(context, obj))
    else:
        prop_slug = str(prop_slug_or_dir)
        staging_dir = Path(staging_dir_or_slug) if staging_dir_or_slug else get_staging_dir(context)

    prop = getattr(context.scene, "reskate_prop", None)
    look_mode = getattr(prop, "look_mode", 'AS_IS') if prop else 'AS_IS'
    use_ext_override = bool(getattr(prop, "use_external_texture_override", False)) if prop else False

    # 1. Explicit custom texture path override (only in BAKE mode or when use_external_texture_override is True)
    if prop and (look_mode == 'BAKE' or use_ext_override) and getattr(prop, "custom_texture_path", None):
        custom_p = Path(prop.custom_texture_path)
        if custom_p.exists() and custom_p.is_file():
            return custom_p

    staging_dir.mkdir(parents=True, exist_ok=True)

    # 2. Path A (Default): Pass-through user's textures & Skate Map PBR without baking or squishing UVs!
    if look_mode != 'BAKE':
        passthrough_path = staging_dir / f"ReSkate_Passthrough_{prop_slug}.png"
        if pre_extracted_materials is not None:
            mat_tex_list = pre_extracted_materials
        else:
            mat_tex_list, _, _ = extract_material_textures_and_pbr(obj, prop_slug, staging_dir)
        vis_tex = [m for m in mat_tex_list if not m.get("invisible", False)] or mat_tex_list
        if vis_tex:
            # Prefer the first slot that has a real image texture so 1-texture multi-slot props stay 1:1 un-squished
            primary_slot = next((m for m in vis_tex if m.get("has_image_texture", False)), vis_tex[0])
            src_p = Path(primary_slot["texture_path"])
            if src_p.exists():
                shutil.copy2(src_p, passthrough_path)
                _refresh_pack_cached_texture_files(staging_dir, [passthrough_path])
                return passthrough_path

    # 3. Path B ('BAKE' explicitly selected): check already baked file or Blender Image datablock
    atlas_path = staging_dir / f"ReSkate_Atlas_{prop_slug}.png"
    if atlas_path.exists() and atlas_path.stat().st_size > 500:
        return atlas_path

    img_name = f"ReSkate_Atlas_{prop_slug}"
    img = bpy.data.images.get(img_name) or bpy.data.images.get("ReSkate_Atlas")
    if img:
        pil_im = _blender_image_to_pil(img, staging_dir=staging_dir)
        if pil_im is not None:
            pil_im.save(atlas_path, "PNG", compress_level=1)
            if atlas_path.exists():
                return atlas_path

    if look_mode == 'BAKE' and hasattr(bpy.ops.reskate, "bake_uv_atlas"):
        try:
            bpy.ops.reskate.bake_uv_atlas()
            if atlas_path.exists() and atlas_path.stat().st_size > 500:
                return atlas_path
        except Exception:
            pass

    # 4. Fallback neutral surface PNG
    im = Image.new("RGBA", (1024, 1024), (200, 200, 200, 255))
    im.save(atlas_path, "PNG", compress_level=1)

    return atlas_path


_VALID_THUMB_MEMO: dict[tuple[str, int, int, int], bool] = {}


def _is_valid_rendered_thumbnail(p: Path | str | None, min_bytes: int = 1200) -> bool:
    """
    Returns True only if `p` exists on disk, is not a static 'No Thumbnail' placeholder,
    is larger than `min_bytes` (rejecting 859-byte / 4,028-byte solid-slate placeholders),
    and has non-zero pixel variance.
    """
    if not p:
        return False
    p_obj = Path(p)
    if "reskate_static_no_thumb_" in p_obj.name.lower():
        return False
    try:
        st = p_obj.stat()
    except OSError:
        return False
    sz = st.st_size
    if sz < min_bytes or sz in (859, 4028):
        return False
    memo_key = (str(p_obj), int(st.st_mtime_ns), int(sz), int(min_bytes))
    cached = _VALID_THUMB_MEMO.get(memo_key)
    if cached is not None:
        return cached
    try:
        if Image is not None:
            with Image.open(p_obj) as im:
                arr = np.asarray(im.convert("RGBA"), dtype=np.float32)
                valid = not (float(arr[:, :, :3].std()) < 0.5 and float(arr[:, :, 3].std()) < 0.5)
        else:
            img = bpy.data.images.load(str(p_obj), check_existing=False)
            try:
                w_px, h_px = int(img.size[0]), int(img.size[1])
                px_arr = np.empty(w_px * h_px * 4, dtype=np.float32)
                img.pixels.foreach_get(px_arr)
                arr = px_arr.reshape((h_px, w_px, 4)) * 255.0
                valid = not (float(arr[:, :, :3].std()) < 0.5 and float(arr[:, :, 3].std()) < 0.5)
            finally:
                bpy.data.images.remove(img)
        if len(_VALID_THUMB_MEMO) > 512:
            _VALID_THUMB_MEMO.clear()
        _VALID_THUMB_MEMO[memo_key] = valid
        return valid
    except Exception:
        return False


def apply_variant_corner_notch_to_png_in_place(
    png_path: Path | None,
    notch_rgb: tuple[int, int, int] | None = None,
) -> bool:
    """Draws a solid colored bottom-left corner triangle notch on `png_path` to identify Variant Family props."""
    if not png_path or not Path(png_path).exists():
        return False
    p = Path(png_path)
    if notch_rgb is None:
        try:
            from . import properties as _props
            notch_rgb = _props.get_variant_notch_color_rgb()
        except Exception:
            notch_rgb = (46, 204, 97)
    r_u8 = int(max(0, min(255, notch_rgb[0])))
    g_u8 = int(max(0, min(255, notch_rgb[1])))
    b_u8 = int(max(0, min(255, notch_rgb[2])))

    if Image is not None:
        try:
            with Image.open(p) as im:
                rgba = im.convert("RGBA")
                arr = np.array(rgba, dtype=np.uint8)
            h_px, w_px = arr.shape[:2]
            notch_sz = max(16, int(round(min(w_px, h_px) * 0.175)))
            if notch_sz > 0 and h_px >= notch_sz and w_px >= notch_sz:
                y0 = h_px - notch_sz
                for dy in range(notch_sz):
                    y = y0 + dy
                    arr[y, 0 : dy + 1, 0] = r_u8
                    arr[y, 0 : dy + 1, 1] = g_u8
                    arr[y, 0 : dy + 1, 2] = b_u8
                    arr[y, 0 : dy + 1, 3] = 255
                tmp_p = p.with_name(f"{p.name}.tmp.{uuid.uuid4().hex[:8]}")
                try:
                    out_im = Image.fromarray(arr, mode="RGBA")
                    out_im.save(tmp_p, "PNG")
                    for _attempt in range(8):
                        try:
                            tmp_p.replace(p)
                            break
                        except (PermissionError, OSError):
                            if _attempt == 7:
                                raise
                            time.sleep(0.02 * (1.5 ** _attempt))
                finally:
                    if tmp_p.exists():
                        _safe_unlink(tmp_p)
                return True
        except Exception:
            pass
    try:
        img = bpy.data.images.load(str(p), check_existing=False)
        try:
            w_px, h_px = int(img.size[0]), int(img.size[1])
            notch_sz = max(16, int(round(min(w_px, h_px) * 0.175)))
            if notch_sz > 0 and w_px >= notch_sz and h_px >= notch_sz:
                px_arr = np.empty(w_px * h_px * 4, dtype=np.float32)
                img.pixels.foreach_get(px_arr)
                px_grid = px_arr.reshape((h_px, w_px, 4))
                # In Blender's image.pixels, row 0 is the bottom of the image
                rf, gf, bf = r_u8 / 255.0, g_u8 / 255.0, b_u8 / 255.0
                for ry in range(notch_sz):
                    max_x = notch_sz - ry
                    px_grid[ry, 0:max_x, 0] = rf
                    px_grid[ry, 0:max_x, 1] = gf
                    px_grid[ry, 0:max_x, 2] = bf
                    px_grid[ry, 0:max_x, 3] = 1.0
                img.pixels.foreach_set(px_grid.ravel())
                img.filepath_raw = str(p)
                img.file_format = 'PNG'
                img.save()
                return True
        finally:
            bpy.data.images.remove(img)
    except Exception:
        pass
    return False


_apply_variant_cutout_to_png_in_place = apply_variant_corner_notch_to_png_in_place


def ensure_static_no_thumbnail_icon(context=None, has_variant: bool = False, size: int = 256) -> Path:
    """
    Returns the path to the shared static 'No Thumbnail' placeholder PNG (`size` x `size`),
    with an optional bottom-left Variant Family corner notch when `has_variant=True`.
    Generated once and cached on disk so carousel UI redraws and compile-time fallbacks take ~0 ms.
    """
    sz = max(64, int(size or 256))
    key = (bool(has_variant), sz)
    cached_p = _STATIC_NO_THUMB_CACHE.get(key)
    if cached_p is not None and cached_p.is_file():
        return cached_p

    out_dir = get_staging_dir(context) / "Preview_Icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_var" if has_variant else ""
    out_path = out_dir / f"reskate_static_no_thumb_{sz}{suffix}.png"
    if not out_path.is_file() or out_path.stat().st_size < 200:
        arr = np.zeros((sz, sz, 4), dtype=np.uint8)
        arr[:, :] = (34, 39, 48, 255)
        # Outer frame border
        b = max(2, sz // 64)
        arr[:b, :] = (76, 86, 104, 255)
        arr[-b:, :] = (76, 86, 104, 255)
        arr[:, :b] = (76, 86, 104, 255)
        arr[:, -b:] = (76, 86, 104, 255)
        # Center camera/frame placeholder glyph + diagonal slash
        c0 = int(sz * 0.28)
        c1 = int(sz * 0.72)
        t = max(2, sz // 48)
        arr[c0:c0 + t, c0:c1] = (135, 148, 168, 255)
        arr[c1 - t:c1, c0:c1] = (135, 148, 168, 255)
        arr[c0:c1, c0:c0 + t] = (135, 148, 168, 255)
        arr[c0:c1, c1 - t:c1] = (135, 148, 168, 255)
        # Diagonal line inside frame indicating 'No Thumbnail'
        span = max(1, c1 - c0)
        idx = np.arange(span)
        for dt in range(-t // 2, t // 2 + 1):
            ys = np.clip(c0 + idx + dt, c0, c1 - 1)
            xs = np.clip(c0 + idx, c0, c1 - 1)
            arr[ys, xs] = (165, 178, 198, 255)
        Image.fromarray(arr, mode="RGBA").save(out_path, "PNG")
        if has_variant:
            apply_variant_corner_notch_to_png_in_place(out_path)

    _STATIC_NO_THUMB_CACHE[key] = out_path
    return out_path


def _has_variant_notch_pixels(png_path: Path | str | None, context=None) -> bool:
    """Returns True if `png_path` exists on disk and has the bottom-left Variant Family corner notch stamped on it."""
    if not png_path:
        return False
    p = Path(png_path)
    if not p.is_file() or "reskate_static_no_thumb_" in p.name.lower():
        return False
    try:
        from . import properties as _props
        notch_rgb = _props.get_variant_notch_color_rgb(context)
    except Exception:
        notch_rgb = (46, 204, 97)
    r_u8 = int(max(0, min(255, notch_rgb[0])))
    g_u8 = int(max(0, min(255, notch_rgb[1])))
    b_u8 = int(max(0, min(255, notch_rgb[2])))
    if Image is not None:
        try:
            with Image.open(p) as im:
                rgba = im.convert("RGBA")
                w_px, h_px = rgba.size
                if w_px < 16 or h_px < 16:
                    return False
                sample_coords = [
                    (0, h_px - 1),
                    (1, h_px - 2),
                    (2, h_px - 4),
                ]
                for sx, sy in sample_coords:
                    pr, pg, pb, pa = rgba.getpixel((sx, sy))
                    if pa < 240 or abs(int(pr) - r_u8) > 16 or abs(int(pg) - g_u8) > 16 or abs(int(pb) - b_u8) > 16:
                        return False
            return True
        except Exception:
            pass
    try:
        img = bpy.data.images.load(str(p), check_existing=False)
        try:
            w_px, h_px = int(img.size[0]), int(img.size[1])
            if w_px < 16 or h_px < 16:
                return False
            px_arr = np.empty(w_px * h_px * 4, dtype=np.float32)
            img.pixels.foreach_get(px_arr)
            px_grid = px_arr.reshape((h_px, w_px, 4))
            rf, gf, bf = r_u8 / 255.0, g_u8 / 255.0, b_u8 / 255.0
            for ry, rx in ((0, 0), (1, 1), (3, 2)):
                pr, pg, pb, pa = px_grid[ry, rx]
                if pa < 0.90 or abs(float(pr) - rf) > 0.08 or abs(float(pg) - gf) > 0.08 or abs(float(pb) - bf) > 0.08:
                    return False
            return True
        finally:
            bpy.data.images.remove(img)
    except Exception:
        return False


def is_prop_thumbnail_out_of_date(context, obj: bpy.types.Object | None) -> tuple[bool, str]:
    """
    Checks whether `obj`'s rendered thumbnail is missing or out of date (e.g. Variant Family corner notch
    added/removed since last render, or prop geometry/materials/surface modified since last render).
    Returns `(is_outdated: bool, reason: str)`.
    """
    if not obj or getattr(obj, "type", None) != 'MESH':
        return False, ""

    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    obj_st = get_or_init_object_prop_state(obj)
    active_mesh = resolve_active_prop_mesh(context) if context else None
    is_active_obj = bool(active_mesh is not None and obj.name == active_mesh.name)
    fam_str = str(
        (getattr(prop, "prop_family_title", "") if (prop and is_active_obj) else "")
        or obj.get("reskate_family_title", "")
        or obj_st.get("prop_family_title", "")
        or ""
    ).strip()
    want_notch = bool(fam_str)

    # Locate current thumbnail on disk
    slug = get_prop_slug(context, obj)
    staging_dir = get_staging_dir(context)
    thumb_candidates = []
    for k in ("reskate_custom_thumbnail_path", "reskate_rendered_thumb_256"):
        v = str(obj.get(k, "") or "").strip()
        if v:
            thumb_candidates.append(Path(v))
    thumb_candidates.append(staging_dir / f"{slug}_thumb_256.png")
    if DEFAULT_STAGING_DIR != staging_dir:
        thumb_candidates.append(DEFAULT_STAGING_DIR / f"{slug}_thumb_256.png")

    valid_thumb_p: Path | None = None
    for cand in thumb_candidates:
        if _is_valid_rendered_thumbnail(cand, min_bytes=500):
            valid_thumb_p = cand
            break

    if valid_thumb_p is None:
        return True, "No thumbnail rendered yet"

    # 1. Check Variant Family corner notch state (metadata + actual bottom-left pixels on disk)
    if "reskate_thumb_rendered_notch" in obj:
        if bool(obj.get("reskate_thumb_rendered_notch")) != want_notch:
            if want_notch:
                return True, f"Variant Family corner notch missing ('{fam_str}')"
            return True, "Variant Family corner notch still present after removal"
    if _has_variant_notch_pixels(valid_thumb_p, context) != want_notch:
        if want_notch:
            return True, f"Variant Family corner notch missing ('{fam_str}')"
        return True, "Variant Family corner notch still present after removal"

    # 2. Check if prop was modified since thumbnail was last rendered
    if bool(obj.get("reskate_thumb_dirty", False)):
        return True, "Prop modified since thumbnail was rendered"
    rendered_fp = str(obj.get("reskate_thumb_rendered_fp", "") or "").strip()
    if rendered_fp:
        cur_fp = compute_prop_scene_fingerprint(context, obj)
        if cur_fp and cur_fp != rendered_fp:
            return True, "Prop modified since thumbnail was rendered"

    return False, "Thumbnail up to date"


def has_any_outdated_thumbnails_fast(context, obj: bpy.types.Object | None = None) -> tuple[bool, int, str]:
    """
    Zero-I/O in-memory check suitable for UI panel drawing to detect whether `obj` or any selected
    pack prop has a missing or out-of-date thumbnail (e.g. Variant Family changed after rendering).
    Returns `(has_outdated, count, first_reason)`.
    """
    targets = get_selected_prop_meshes(context) if context else []
    if obj is not None and getattr(obj, "type", None) == 'MESH' and obj not in targets:
        targets = [obj] + list(targets)
    if not targets:
        return False, 0, ""

    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    active_mesh = resolve_active_prop_mesh(context) if context else None
    count = 0
    first_reason = ""
    for o in targets:
        if not o or getattr(o, "type", None) != 'MESH':
            continue
        is_act = bool(active_mesh is not None and o.name == active_mesh.name)
        o_st = get_or_init_object_prop_state(o)
        fam = str(
            (getattr(prop, "prop_family_title", "") if (prop and is_act) else "")
            or o.get("reskate_family_title", "")
            or o_st.get("prop_family_title", "")
            or ""
        ).strip()
        want_notch = bool(fam)
        has_rendered = bool(o.get("reskate_thumb_rendered", False) or o.get("reskate_rendered_thumb_256", "") or o.get("reskate_custom_thumbnail_path", ""))
        if not has_rendered:
            count += 1
            if not first_reason:
                first_reason = f"'{o.name}' has no rendered thumbnail"
            continue
        if "reskate_thumb_rendered_notch" in o and bool(o.get("reskate_thumb_rendered_notch")) != want_notch:
            count += 1
            if not first_reason:
                first_reason = f"'{o.name}' Variant Family corner notch out of date"
            continue
        if bool(o.get("reskate_thumb_dirty", False)):
            count += 1
            if not first_reason:
                first_reason = f"'{o.name}' modified since thumbnail was rendered"
            continue
    return (count > 0), count, first_reason


def ensure_thumbnails(
    context,
    obj: bpy.types.Object,
    prop_slug: str | None = None,
    staging_dir: Path | None = None,
    force_rerender: bool | None = None,
    force: bool | None = None,
) -> tuple[Path, Path]:
    """
    Ensures 256x256 and 768x768 thumbnails exist on disk for `obj` (`prop_slug`).
    Reuses existing rendered/custom thumbnails when `skip_existing_thumbnails` is enabled.
    When `skip_existing_thumbnails` is unchecked (`force_rerender=True`), re-renders via EEVEE
    when available and ALWAYS falls back to existing valid thumbnails on disk (never overwriting
    a valid thumbnail with the static 'No Thumbnail' placeholder).
    """
    if not prop_slug:
        prop_slug = get_prop_slug(context, obj)
    if staging_dir is None:
        staging_dir = get_staging_dir(context)
    else:
        staging_dir = Path(staging_dir)
    if force_rerender is None and force is not None:
        force_rerender = bool(force)
    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    skip_existing = bool(getattr(prop, "skip_existing_thumbnails", True)) if prop is not None else True
    obj_st = get_or_init_object_prop_state(obj) if (obj is not None and getattr(obj, "type", None) == 'MESH') else {}
    if force_rerender is not None:
        skip_existing = not bool(force_rerender)

    active_mesh = resolve_active_prop_mesh(context) if context else None
    is_active_obj = bool(obj is not None and active_mesh is not None and obj.name == active_mesh.name)
    fam_str = str(
        (obj.get("reskate_family_title", "") if obj else "")
        or obj_st.get("prop_family_title", "")
        or (getattr(prop, "prop_family_title", "") if (prop and is_active_obj) else "")
        or ""
    ).strip()
    notch_active = bool(fam_str)

    t256 = staging_dir / f"{prop_slug}_thumb_256.png"
    t768 = staging_dir / f"{prop_slug}_thumb_768.png"
    t256_clean = staging_dir / f"{prop_slug}_thumb_256.clean.png"
    t768_clean = staging_dir / f"{prop_slug}_thumb_768.clean.png"

    def _finalize_cached_pair(p256: Path, p768: Path) -> tuple[Path, Path]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        # Check if clean un-notched backups exist in staging_dir or parent/default staging dir
        for cand_d in (staging_dir, staging_dir.parent if staging_dir.name == "Pack_Queue" else None, DEFAULT_STAGING_DIR):
            if cand_d is None:
                continue
            c256_cl = cand_d / f"{prop_slug}_thumb_256.clean.png"
            c768_cl = cand_d / f"{prop_slug}_thumb_768.clean.png"
            if _is_valid_rendered_thumbnail(c256_cl, min_bytes=1200) and _is_valid_rendered_thumbnail(c768_cl, min_bytes=4500):
                if c256_cl.resolve() != t256_clean.resolve():
                    try:
                        shutil.copy2(c256_cl, t256_clean)
                        shutil.copy2(c768_cl, t768_clean)
                    except Exception:
                        pass
                break

        if p256.resolve() != t256.resolve():
            shutil.copy2(p256, t256)
            p256 = t256
        if p768.resolve() != t768.resolve():
            shutil.copy2(p768, t768)
            p768 = t768

        # If p256/p768 is currently un-notched and we don't have a .clean.png backup yet, save one now!
        if not _is_valid_rendered_thumbnail(t768_clean, min_bytes=4500) and not _has_variant_notch_pixels(p768, context):
            try:
                shutil.copy2(p768, t768_clean)
                shutil.copy2(p256, t256_clean)
            except Exception:
                pass

        # Restore from clean un-notched backup before stamping/clearing so removing a Variant Family also removes the notch
        if _is_valid_rendered_thumbnail(t256_clean, min_bytes=1200) and _is_valid_rendered_thumbnail(t768_clean, min_bytes=4500):
            try:
                shutil.copy2(t768_clean, p768)
                shutil.copy2(t256_clean, p256)
            except Exception:
                pass

        if notch_active:
            apply_variant_corner_notch_to_png_in_place(p768)
            apply_variant_corner_notch_to_png_in_place(p256)

        if obj is not None:
            try:
                obj["reskate_thumb_rendered_notch"] = bool(notch_active)
                obj["reskate_thumb_rendered_family"] = fam_str
            except Exception:
                pass
        return p256, p768

    def _find_existing_valid_thumbnails() -> tuple[Path, Path] | None:
        # 1. Per-object custom override first, then scene override if it isn't bound to a different object
        if obj:
            for cust_key in ("reskate_custom_thumbnail_path", "reskate_custom_thumb_png"):
                if obj.get(cust_key):
                    obj_custom = Path(str(obj.get(cust_key)))
                    if _is_valid_rendered_thumbnail(obj_custom, min_bytes=500):
                        return _finalize_cached_pair(obj_custom, obj_custom)

        if prop and getattr(prop, "custom_thumb_png", None):
            owner = str(prop.get("custom_thumb_owner", "")) if hasattr(prop, "get") else ""
            if not owner or (obj and owner == obj.name):
                custom_thumb = Path(prop.custom_thumb_png)
                if _is_valid_rendered_thumbnail(custom_thumb, min_bytes=500):
                    return _finalize_cached_pair(custom_thumb, custom_thumb)

        # 2. Per-object rendered thumbnail paths stored on the object
        if obj:
            r256_str = str(obj.get("reskate_rendered_thumb_256", ""))
            r768_str = str(obj.get("reskate_rendered_thumb_768", ""))
            if r256_str and r768_str:
                r256_p = Path(r256_str)
                r768_p = Path(r768_str)
                if _is_valid_rendered_thumbnail(r256_p, min_bytes=1200) and _is_valid_rendered_thumbnail(r768_p, min_bytes=4500):
                    return _finalize_cached_pair(r256_p, r768_p)

        # 3. Fast PIL Lanczos downsample if 768 exists but 256 is missing
        if _is_valid_rendered_thumbnail(t768, min_bytes=4500) and not _is_valid_rendered_thumbnail(t256, min_bytes=1200):
            try:
                with Image.open(t768) as im768:
                    im256 = im768.resize((256, 256), Image.Resampling.LANCZOS)
                    im256.save(t256, "PNG")
            except Exception:
                pass

        if _is_valid_rendered_thumbnail(t256, min_bytes=1200) and _is_valid_rendered_thumbnail(t768, min_bytes=4500):
            return _finalize_cached_pair(t256, t768)

        # 4. Check if valid rendered thumbnails already exist in parent staging_dir or DEFAULT_STAGING_DIR
        for cand_dir in (staging_dir.parent if staging_dir.name == "Pack_Queue" else None, DEFAULT_STAGING_DIR):
            if cand_dir is None:
                continue
            c256 = cand_dir / f"{prop_slug}_thumb_256.png"
            c768 = cand_dir / f"{prop_slug}_thumb_768.png"
            if _is_valid_rendered_thumbnail(c256, min_bytes=1200) and _is_valid_rendered_thumbnail(c768, min_bytes=4500):
                return _finalize_cached_pair(c256, c768)
        return None

    if skip_existing:
        existing_pair = _find_existing_valid_thumbnails()
        if existing_pair is not None:
            return existing_pair
    else:
        # Even when re-rendering is forced, if the user explicitly picked an external custom image file (not a staged _thumb_256.png), keep their custom image!
        if obj and obj.get("reskate_custom_thumbnail_path"):
            ext_p = Path(str(obj.get("reskate_custom_thumbnail_path")))
            if not ext_p.name.endswith(("_thumb_256.png", "_thumb_768.png")) and _is_valid_rendered_thumbnail(ext_p, min_bytes=500):
                return _finalize_cached_pair(ext_p, ext_p)

    # Render via EEVEE when:
    # - `not skip_existing` (user explicitly unchecked 'Skip Re-Rendering Existing Thumbnails' or requested force_rerender), OR
    # - `auto_render_thumbnails_on_compile` is enabled in Add-on Preferences for missing thumbnails.
    auto_render_enabled = bool(properties.is_auto_render_thumbnails_on_compile_enabled(context))
    should_render = (not skip_existing) or auto_render_enabled
    if should_render and not bpy.app.background and hasattr(bpy.ops.reskate, "render_thumbnails"):
        prev_active = getattr(context.view_layer.objects, "active", None) if context and hasattr(context, "view_layer") else None
        try:
            if obj and context and hasattr(context, "view_layer"):
                context.view_layer.objects.active = obj
            bpy.ops.reskate.render_thumbnails('EXEC_DEFAULT')
            r256_after = Path(str(obj.get("reskate_rendered_thumb_256", ""))) if (obj and obj.get("reskate_rendered_thumb_256")) else (get_staging_dir(context) / f"{prop_slug}_thumb_256.png")
            r768_after = Path(str(obj.get("reskate_rendered_thumb_768", ""))) if (obj and obj.get("reskate_rendered_thumb_768")) else (get_staging_dir(context) / f"{prop_slug}_thumb_768.png")
            if _is_valid_rendered_thumbnail(r256_after, min_bytes=1200) and _is_valid_rendered_thumbnail(r768_after, min_bytes=4500):
                return _finalize_cached_pair(r256_after, r768_after)
        except Exception:
            pass
        finally:
            if prev_active and context and hasattr(context, "view_layer"):
                try:
                    context.view_layer.objects.active = prev_active
                except Exception:
                    pass

    # Always preserve any existing valid thumbnail on disk (updating its Variant Corner Notch) before falling back to 'No Thumbnail'
    fallback_existing = _find_existing_valid_thumbnails()
    if fallback_existing is not None:
        return fallback_existing

    # Shared static 'No Thumbnail' placeholder PNGs (0 ms compile time, only when no rendered thumbnail exists anywhere)
    static_256 = ensure_static_no_thumbnail_icon(context, has_variant=notch_active, size=256)
    static_768 = ensure_static_no_thumbnail_icon(context, has_variant=notch_active, size=768)
    return static_256, static_768


def compute_prop_scene_fingerprint_fast(context, obj: bpy.types.Object | None) -> str:
    """
    Computes a fast SHA1 fingerprint over `obj`'s mesh geometry, UVs, world transform,
    materials (`sk8_material` + Principled BSDF nodes), collision settings, grind paths,
    and linked lights using metadata hashing without accessing `im_obj.pixels` or `.packed_file.data`.
    """
    if not obj or obj.type != 'MESH' or not obj.data:
        return ""

    prop = getattr(context.scene, "reskate_prop", None) if context and context.scene else None
    scale_mult = float(getattr(prop, "scale_mult", 1.0)) if prop else 1.0

    mesh = obj.data
    h = hashlib.sha1()
    h.update(f"name:{obj.name}|v:{len(mesh.vertices)}|p:{len(mesh.polygons)}|l:{len(mesh.loops)}|sm:{scale_mult:.4f}".encode("utf-8"))

    # Transform matrix
    mat_vals = [round(float(obj.matrix_world[r][c]), 4) for r in range(4) for c in range(4)]
    h.update(f"|mw:{mat_vals}".encode("utf-8"))

    # Sampled vertex positions
    nv = len(mesh.vertices)
    if nv > 0:
        try:
            v_buf = np.empty(nv * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", v_buf)
            step = max(1, (nv * 3) // 96)
            h.update(np.round(v_buf[::step], 4).tobytes())
        except Exception:
            pass

    # Sampled UVs across all UV layers (plus active_render flag)
    for uvl in mesh.uv_layers:
        h.update(f"|uvl:{uvl.name}:{getattr(uvl, 'active_render', False)}:{getattr(uvl, 'active', False)}".encode("utf-8"))
        nuv = len(uvl.data)
        if nuv > 0:
            try:
                uv_buf = np.empty(nuv * 2, dtype=np.float32)
                uvl.data.foreach_get("uv", uv_buf)
                step = max(1, (nuv * 2) // 96)
                h.update(np.round(uv_buf[::step], 4).tobytes())
            except Exception:
                pass

    _token_cache: dict[int, str] = {}
    def _image_fp_token(im_obj: bpy.types.Image | None) -> str:
        if im_obj is None:
            return "None"
        ptr = im_obj.as_pointer() if hasattr(im_obj, "as_pointer") else id(im_obj)
        if ptr in _token_cache:
            return _token_cache[ptr]
        try:
            cs = str(getattr(getattr(im_obj, "colorspace_settings", None), "name", "") or "")
            is_dirty = getattr(im_obj, "is_dirty", False)
            # Fast metadata hashing: avoid .packed_file.data buffer copy, avoid im_obj.pixels read, and avoid im_obj.size / is_float C header decode!
            pk_sz = int(im_obj.packed_file.size) if getattr(im_obj, "packed_file", None) else 0
            filepath = str(getattr(im_obj, "filepath_raw", "") or "")
            st_sz = 0
            st_mt_ns = 0
            if filepath:
                src = _source_file_for_image(im_obj)
                if src and os.path.exists(src):
                    try:
                        st = os.stat(src)
                        st_sz = int(st.st_size)
                        st_mt_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                    except Exception:
                        pass
            tok = f"{im_obj.name}:{cs}:d={is_dirty}:pk={pk_sz}:sz={st_sz}:mt={st_mt_ns}:fp={filepath}"
            _token_cache[ptr] = tok
            return tok
        except Exception:
            tok = str(getattr(im_obj, "name", ""))
            _token_cache[ptr] = tok
            return tok

    # Materials & Skate Map settings
    for idx, slot in enumerate(obj.material_slots):
        mat = slot.material
        if not mat:
            h.update(f"|slot{idx}:None".encode("utf-8"))
            continue
        # Read-only hashing: avoid mutating shader node trees with heal_corrupted_reskate_user_override_nodes here
        ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
        ms_str = ""
        if ms is not None:
            for k in (
                "invisible", "domain", "surface", "collision_material", "srgb", "alpha", "alpha_source",
                "roughness_channel", "metallic_channel", "use_custom_texture_overrides",
                "emission_strength", "override_contact_audio", "is_decal_slot", "decal_alpha_mode", "decal_shader_mode",
            ):
                ms_str += f"{k}={getattr(ms, k, '')};"
            for img_k in ("base_color_image", "normal_image", "mask_image", "roughness_image", "metallic_image", "opacity_image", "emission_image"):
                im_obj = getattr(ms, img_k, None)
                ms_str += f"{img_k}={_image_fp_token(im_obj)};"
        p_node = _principled_node(mat)
        p_str = ""
        if p_node is not None:
            p_str += f"p_node={p_node.name};"
            for sk in ("Base Color", "Roughness", "Metallic", "Normal", "Alpha", "Emission Strength"):
                sock = p_node.inputs.get(sk)
                if sock is not None:
                    dv = getattr(sock, "default_value", None)
                    dv_repr = [round(float(x), 3) for x in dv] if (dv is not None and hasattr(dv, "__len__")) else (round(float(dv), 3) if dv is not None else "")
                    ln_img = _base_color_image(mat) if sk == "Base Color" else _linked_image(sock)
                    p_str += f"{sk}:{dv_repr}:{_image_fp_token(ln_img)};"
        if mat.use_nodes and mat.node_tree:
            for nd in mat.node_tree.nodes:
                if nd.type == "TEX_IMAGE" and getattr(nd, "image", None) is not None:
                    p_str += f"texnode({nd.name})={_image_fp_token(nd.image)};"
            for lk in mat.node_tree.links:
                if lk.from_node and lk.to_node:
                    p_str += f"lk({lk.from_node.name}.{lk.from_socket.name}->{lk.to_node.name}.{lk.to_socket.name});"
        h.update(f"|mat{idx}:{mat.name}:{ms_str}:{p_str}".encode("utf-8"))

    # Per-material surface overrides
    for idx, ov in enumerate(getattr(obj, "reskate_material_surfaces", [])):
        h.update(f"|msurf{idx}:{getattr(ov, 'surface', 'DEFAULT')}".encode("utf-8"))

    # Grind paths
    for idx, gp in enumerate(getattr(obj, "reskate_grind_paths", [])):
        h.update(
            f"|gp{idx}:{getattr(gp, 'enabled', True)}:{getattr(gp, 'grind_type', '')}:{getattr(gp, 'radius', 0.0):.3f}:{getattr(gp, 'surface', '')}:{getattr(gp, 'vertex_indices_csv', '')}".encode("utf-8")
        )

    # Linked / child lights
    lights = extract_prop_lights(obj, scale_mult=scale_mult)
    h.update(f"|lights:{json.dumps(lights, sort_keys=True)}".encode("utf-8"))

    # Experimental ReSkate Lab state & per-prop v0.22 settings
    exp_data = extract_prop_experimental_data(obj, scale_mult=scale_mult)
    if exp_data:
        h.update(f"|exp:{json.dumps(exp_data, sort_keys=True)}".encode("utf-8"))
    obj_st = get_or_init_object_prop_state(obj)
    for k_st in (
        "prop_category",
        "custom_qd_tab",
        "collision_mode",
        "no_collision_placement_mode",
        "placement_proxy_mode",
        "anti_lip_enabled",
        "anti_lip_sink_m",
        "use_2x2m_detail_shader",
        "detail_2x2m_preset",
        "detail_2x2m_intensity",
        "detail_2x2m_scale",
        "use_procedural_wear",
        "wear_basis",
        "wear_edge_strength",
        "wear_crevice_strength",
        "wear_top_strength",
        "wear_scale",
        "atlas_resolution",
        "thumb_variant_cutout_enabled",
    ):
        h.update(f"|{k_st}:{obj_st.get(k_st, '')}".encode("utf-8"))

    # Custom collision object & painted per-face surface overrides if present
    col_obj = resolve_custom_collision_object(context, obj) if context else None
    if col_obj and col_obj.type == 'MESH' and col_obj.data:
        cm = col_obj.data
        c_mw = [round(float(col_obj.matrix_world[r][c]), 4) for r in range(4) for c in range(4)]
        c_fs = get_mesh_face_surface_array(col_obj)
        c_fs_nz = [(i, v) for i, v in enumerate(c_fs) if v > 0]
        h.update(f"|col:{col_obj.name}:{len(cm.vertices)}:{len(cm.polygons)}:{c_mw}:{c_fs_nz}".encode("utf-8"))
    m_fs = get_mesh_face_surface_array(obj)
    m_fs_nz = [(i, v) for i, v in enumerate(m_fs) if v > 0]
    if m_fs_nz:
        h.update(f"|mfs:{m_fs_nz}".encode("utf-8"))

    return h.hexdigest()


def compute_prop_scene_fingerprint(context, obj: bpy.types.Object | None) -> str:
    """Computes a fast SHA1 fingerprint over obj's scene state using metadata hashing."""
    return compute_prop_scene_fingerprint_fast(context, obj)


def is_queue_item_modified(context, item) -> bool:
    """Returns True if the scene object corresponding to `item` has been modified since it was queued/synced."""
    if not context or not context.scene or not item:
        return False
    obj = context.scene.objects.get(item.name) or bpy.data.objects.get(item.name)
    if not obj or obj.type != 'MESH':
        return False
    stored = getattr(item, "snapshot_hash", "")
    if not stored:
        return True
    current = compute_prop_scene_fingerprint(context, obj)
    return current != stored


def sync_queue_item_from_scene(context, item, batch_dir: Path | None = None, skip_textures: bool = False) -> bool:
    """Re-exports the scene object's GLB, collision, textures, lights, and metadata into `item` and updates `snapshot_hash`."""
    if not context or not context.scene or not item:
        return False
    scene = context.scene
    prop = getattr(scene, "reskate_prop", None)
    obj = scene.objects.get(item.name) or bpy.data.objects.get(item.name)
    if not obj or obj.type != 'MESH':
        return False

    if batch_dir is None:
        batch_dir = get_staging_dir(context) / "Pack_Queue"
    batch_dir.mkdir(parents=True, exist_ok=True)

    obj_state = get_or_init_object_prop_state(obj)
    slug = get_prop_slug(context, obj)
    item.prop_key = slug
    item.prop_key_slug = slug
    item.display_name = clean_prop_display_name(str(obj_state.get("prop_display_name") or item.display_name or obj.name))
    item.category = str(obj_state.get("prop_category") or item.category or "Custom Props")
    item.collision_mode = str(obj_state.get("collision_mode") or item.collision_mode or "SAME_AS_MESH")
    item.primary_surface = str(obj_state.get("primary_surface") or item.primary_surface or "concrete_smooth")
    item.surface_code = int(obj_state.get("surface_material_code") or item.surface_code or 96)
    item.surface_name = get_surface_label(item.primary_surface)

    scale_mult = float(obj_state.get("scale_mult", getattr(prop, "scale_mult", 1.0) if prop else 1.0))

    # 1. Re-export visual GLB
    glb_p = batch_dir / f"{slug}.glb"
    export_visual_glb(context, obj, glb_p)
    item.glb_path = str(glb_p)

    # 2. Re-export custom collision GLB if applicable (including Simple Hulls geometric mesh `<ObjName>_COL`)
    col_obj = resolve_custom_collision_object(context, obj)
    item.has_col_proxy = col_obj is not None
    if item.collision_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL', 'SIMPLE_HULLS', 'CONVEX_HULL') or col_obj is not None:
        col_out = batch_dir / f"{slug}_col.glb"
        c_res = export_col_glb(context, obj, col_out, explicit_col_obj=col_obj)
        if c_res and c_res.exists():
            item.col_glb_path = str(c_res)

    # 3. Re-export textures & Skate Map PBR (skipped when build_pack_manifest extracts them directly to avoid double extraction!)
    if not skip_textures:
        tex_p = ensure_texture_atlas(context, obj, slug, batch_dir)
        item.texture_path = str(tex_p)
    elif not item.texture_path:
        item.texture_path = str(batch_dir / f"ReSkate_Passthrough_{slug}.png")

    # 4. Update counts, lights, and snapshot fingerprint
    lights = extract_prop_lights(obj, scale_mult=scale_mult)
    item.has_lights = len(lights) > 0
    item.poly_count = len(obj.data.polygons)
    item.tri_count = sum(max(0, len(p.vertices) - 2) for p in obj.data.polygons)
    item.snapshot_hash = compute_prop_scene_fingerprint(context, obj)
    return True


def get_pack_storage_dir(target_mod_name: str, staging_dir: Path | None = None) -> Path:
    """Returns the canonical staging pack registry directory (`<Staging>/Packs/<target_mod_name>`)."""
    base = Path(staging_dir) if staging_dir else get_staging_dir()
    resolved_mod = (target_mod_name or "").strip() or get_resolved_target_mod_name()
    return base / "Packs" / (resolved_mod or "CustomProps").strip()


def inspect_existing_mod_props(target_mod_name: str, staging_dir: Path | None = None, use_cache: bool = True) -> dict:
    """
    Checks `<Staging>/Packs/<target_mod_name>/pack_registry.json` (and legacy staging directories)
    and returns info about existing compiled props, timestamps, compiler versions, and rolling backups
    without ever reading from `Mods/`.
    """
    global _EXISTING_MOD_CACHE
    mod_name = (target_mod_name or "").strip()
    if not mod_name:
        return {
            "exists": False,
            "mod_dir": "",
            "prop_keys": [],
            "prop_names": [],
            "props_detail": [],
            "count": 0,
            "has_backups": False,
            "backups_count": 0,
        }

    base_staging = Path(staging_dir) if staging_dir else get_staging_dir()
    cache_key = f"{mod_name}|{base_staging}"
    now = time.monotonic()
    if use_cache and cache_key in _EXISTING_MOD_CACHE:
        ts, cached_res = _EXISTING_MOD_CACHE[cache_key]
        if (now - ts) < 2.0:
            return cached_res

    resolved_mod = get_resolved_target_mod_name(None, mod_name)
    pack_store = get_pack_storage_dir(mod_name, base_staging)
    resolved_pack_store = get_pack_storage_dir(resolved_mod, base_staging)
    backups_dir = (pack_store / "backups") if pack_store.exists() else (resolved_pack_store / "backups")
    backup_folders = sorted([d for d in backups_dir.iterdir() if d.is_dir()]) if backups_dir.exists() else []

    candidates = [
        pack_store,
        resolved_pack_store,
        base_staging / "Packs" / mod_name,
        base_staging / "Packs" / resolved_mod,
        base_staging / mod_name,
        base_staging / resolved_mod,
        base_staging / "mods" / mod_name,
        DEFAULT_STAGING_DIR / "Packs" / mod_name,
        DEFAULT_STAGING_DIR / "Packs" / resolved_mod,
        DEFAULT_STAGING_DIR / mod_name,
        DEFAULT_STAGING_DIR / "mods" / mod_name,
        Path(r"B:\Games\ReSkate\Staging_Test_Pack") / "Packs" / mod_name,
        Path(r"B:\Games\ReSkate\Staging_Test_Pack") / "Packs" / resolved_mod,
        Path(r"B:\Games\ReSkate\Staging_Test_Pack") / mod_name,
        Path(r"B:\Games\ReSkate\Staging_Test_Pack") / resolved_mod,
    ]

    for mod_dir in candidates:
        if not mod_dir.exists() or not mod_dir.is_dir():
            continue

        prop_keys: list[str] = []
        prop_names: list[str] = []
        props_detail: list[dict] = []

        # 1. Check canonical pack_registry.json or legacy .reskate_source/props_registry.json
        for reg_file in (mod_dir / "pack_registry.json", mod_dir / ".reskate_source" / "props_registry.json"):
            if reg_file.exists():
                try:
                    reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
                    reg_built_at = reg_data.get("updated_at_iso", "")
                    reg_ver = reg_data.get("compiler_version", "0.20.0")
                    for p in reg_data.get("props", []):
                        pk = p.get("prop_key") or p.get("prop_id") or p.get("name")
                        pid = p.get("prop_id") or (pk[len("own_customprops_"):] if str(pk).startswith("own_customprops_") else pk)
                        pn = p.get("display_name") or pk
                        if pk and pk not in prop_keys:
                            prop_keys.append(str(pk))
                            prop_names.append(str(pn))
                            props_detail.append({
                                "prop_key": str(pk),
                                "prop_id": str(pid),
                                "slug": str(p.get("slug") or pid or pk),
                                "source_blend_object": str(p.get("source_blend_object") or ""),
                                "display_name": str(pn),
                                "category": str(p.get("category", "Custom Props")),
                                "built_at": str(p.get("built_at_iso") or reg_built_at or ""),
                                "compiler_version": str(p.get("compiler_version") or reg_ver),
                            })
                    break
                except Exception:
                    pass

        # 2. Check reskate-build.json in staging
        build_file = mod_dir / "reskate-build.json"
        if not prop_keys and build_file.exists():
            try:
                b_data = json.loads(build_file.read_text(encoding="utf-8"))
                items_raw = b_data.get("props") or b_data.get("items")
                if isinstance(items_raw, list):
                    for p in items_raw:
                        if isinstance(p, dict):
                            pk = p.get("prop_key") or p.get("name")
                            pn = p.get("display_name") or pk
                        else:
                            pk = str(p)
                            pn = str(p)
                        if pk and pk not in prop_keys:
                            prop_keys.append(pk)
                            prop_names.append(pn)
                            props_detail.append({
                                "prop_key": str(pk),
                                "prop_id": str(pk[len("own_customprops_"):] if str(pk).startswith("own_customprops_") else pk),
                                "display_name": str(pn),
                                "category": "Custom Props",
                                "built_at": "",
                                "compiler_version": str(b_data.get("compiler_version", "0.20.0")),
                            })
            except Exception:
                pass

        if prop_keys or (mod_dir / "pack_registry.json").exists() or build_file.exists():
            res = {
                "exists": True,
                "mod_dir": str(mod_dir),
                "has_source_registry": True,
                "prop_keys": prop_keys,
                "prop_names": prop_names,
                "props_detail": props_detail,
                "count": len(prop_keys),
                "has_backups": len(backup_folders) > 0,
                "backups_count": len(backup_folders),
                "latest_backup": backup_folders[-1].name if backup_folders else "",
            }
            _EXISTING_MOD_CACHE[cache_key] = (now, res)
            return res


    res_empty = {
        "exists": False,
        "mod_dir": "",
        "prop_keys": [],
        "prop_names": [],
        "props_detail": [],
        "count": 0,
        "has_backups": len(backup_folders) > 0,
        "backups_count": len(backup_folders),
        "latest_backup": backup_folders[-1].name if backup_folders else "",
    }
    _EXISTING_MOD_CACHE[cache_key] = (now, res_empty)
    return res_empty


def _get_obj_pack_sort_order(o: bpy.types.Object) -> int:
    """Returns the user-defined pack sort order for `o` (or 999999 if unset)."""
    if o is None:
        return 999999
    try:
        if "reskate_pack_sort_order" in o:
            return int(o["reskate_pack_sort_order"])
        st = get_or_init_object_prop_state(o)
        if "pack_sort_order" in st:
            return int(st["pack_sort_order"])
    except Exception:
        pass
    return 999999


def _sort_prop_meshes_by_pack_order(objs: list[bpy.types.Object]) -> list[bpy.types.Object]:
    """Sorts a list of prop mesh objects by `reskate_pack_sort_order` while preserving original relative order for ties."""
    if len(objs) < 2:
        return objs
    return [o for _, o in sorted(enumerate(objs), key=lambda item: (_get_obj_pack_sort_order(item[1]), item[0]))]


def resolve_prop_surface_summary(
    obj: bpy.types.Object | None,
    mat_surfaces: list[dict],
    default_surf_id: str,
    default_surf_code: int,
) -> tuple[str, int, str]:
    """
    Resolves `(surf_id, surf_code, surface_label)` for a prop from its per-material-slot
    `mat_surfaces` (populated from `mat.reskate_mat.surface_role_preset` / `extract_material_surfaces`)
    so the prop chart and `reskate-objects.json` always show the real material surface(s)
    instead of falling back to `"Concrete_Solid"` / `"concrete_smooth"`.
    """
    surf_id = str(default_surf_id or "Concrete_Solid").strip()
    surf_code = int(default_surf_code or 96)
    non_decal = [
        ms for ms in (mat_surfaces or [])
        if isinstance(ms, dict)
        and not bool(ms.get("is_decal_overlay", False))
        and str(ms.get("surface") or "").strip().lower() != "decal_overlay"
    ]
    active_slots = non_decal if non_decal else [ms for ms in (mat_surfaces or []) if isinstance(ms, dict)]

    labels: list[str] = []
    if active_slots:
        ms0 = active_slots[0]
        s0_name = str(ms0.get("surface") or "").strip()
        s0_code = int(ms0.get("packed") or surf_code or 96)
        if s0_name and s0_name.upper() != "DEFAULT":
            # Promote the primary non-decal material slot surface if prop-level surface was left at default concrete
            if surf_id.lower() in ("concrete_smooth", "concrete_solid", "default", "") or len(active_slots) == 1:
                surf_id = s0_name
                surf_code = s0_code
        for ms in active_slots:
            ms_surf = str(ms.get("surface") or "").strip()
            ms_code = int(ms.get("packed") or surf_code or 96)
            lbl = get_surface_label(ms_surf if (ms_surf and ms_surf.upper() != "DEFAULT") else ms_code)
            if lbl and lbl not in labels:
                labels.append(lbl)

    if not labels:
        fallback_lbl = get_surface_label(surf_id if surf_id else surf_code)
        if fallback_lbl:
            labels.append(fallback_lbl)

    surface_label = " + ".join(labels) if labels else "Concrete (Smooth)"
    return surf_id, surf_code, surface_label


def get_selected_prop_meshes(context) -> list[bpy.types.Object]:
    """
    Returns all selected prop MESH objects in the 3D Viewport / Outliner,
    automatically resolving any selected collision helper meshes (`_COL`, `_HULL`, custom collision)
    to their owner main prop mesh and excluding scale reference models.
    """
    if context is None:
        return []
    raw_sel = list(getattr(context, "selected_objects", None) or [])
    raw_active = getattr(context, "active_object", None)
    if raw_active is None:
        vl = getattr(context, "view_layer", None)
        if vl is not None and getattr(vl, "objects", None) is not None:
            raw_active = vl.objects.active
    if (
        raw_active is not None
        and raw_active not in raw_sel
        and (
            getattr(raw_active, "mode", "OBJECT") != 'OBJECT'
            or bool(getattr(raw_active, "select_get", lambda: False)())
        )
    ):
        raw_sel.insert(0, raw_active)

    out: list[bpy.types.Object] = []
    seen_names: set[str] = set()
    for o in raw_sel:
        if o is None:
            continue
        nm_up = o.name.upper()
        if (
            nm_up.startswith("RESKATE_REF_")
            or nm_up.startswith("RESKATE_SKATER_")
            or nm_up.startswith("RESKATE_BOARD_")
            or nm_up.startswith("RESKATE_GROUND_PLANE")
            or nm_up.startswith("_RESKATE_")
            or bool(o.get("reskate_is_ref_helper", False))
            or bool(o.get("reskate_is_scale_reference", False))
        ):
            continue
        resolved = resolve_collision_to_owner_prop(o)
        if resolved is None or getattr(resolved, "type", None) != 'MESH' or not resolved.data or len(resolved.data.polygons) == 0:
            continue
        if is_collision_helper_object(resolved):
            continue
        if resolved.name not in seen_names:
            seen_names.add(resolved.name)
            out.append(resolved)
    return _sort_prop_meshes_by_pack_order(out)


get_selected_prop_mesh_objects = get_selected_prop_meshes


def get_all_scene_prop_meshes(context) -> list[bpy.types.Object]:
    """
    Returns all valid authorable prop MESH objects in `context.scene.objects`,
    excluding scale references (`RESKATE_REF_*`, `RESKATE_SKATER_*`, `RESKATE_BOARD_*`,
    `RESKATE_GROUND_PLANE*`, `_RESKATE_*`) and collision/placement helpers (`_COL`, `_PPHYS`, `_HULL`).
    """
    global _ALL_SCENE_PROP_MESHES_CACHE
    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    if scene is None:
        return []

    now = time.monotonic()
    scn_ptr = scene.as_pointer() if hasattr(scene, "as_pointer") else id(scene)
    scn_cnt = len(scene.objects)
    c_ts, c_ptr, c_cnt, c_names = _ALL_SCENE_PROP_MESHES_CACHE
    if c_ptr == scn_ptr and c_cnt == scn_cnt and (now - c_ts) < 1.5 and c_names:
        resolved_cached = []
        for nm in c_names:
            o_ref = scene.objects.get(nm)
            if o_ref is not None:
                resolved_cached.append(o_ref)
        if len(resolved_cached) == len(c_names):
            return resolved_cached

    out: list[bpy.types.Object] = []
    seen_names: set[str] = set()
    for o in scene.objects:
        if o is None or getattr(o, "type", None) != 'MESH' or not o.data or len(o.data.polygons) == 0:
            continue
        nm_up = o.name.upper()
        if (
            nm_up.startswith("RESKATE_REF_")
            or nm_up.startswith("RESKATE_SKATER_")
            or nm_up.startswith("RESKATE_BOARD_")
            or nm_up.startswith("RESKATE_GROUND_PLANE")
            or nm_up.startswith("_RESKATE_")
            or bool(o.get("reskate_is_ref_helper", False))
            or bool(o.get("reskate_is_scale_reference", False))
        ):
            continue
        if is_collision_helper_object(o):
            continue
        if o.name not in seen_names:
            seen_names.add(o.name)
            out.append(o)
    out = _sort_prop_meshes_by_pack_order(out)
    _ALL_SCENE_PROP_MESHES_CACHE = (now, scn_ptr, scn_cnt, [o.name for o in out])
    return out


def get_pack_prop_meshes_in_scene(context=None, target_mod_name: str = "") -> list[bpy.types.Object]:
    """
    Returns all valid prop MESH objects in `context.scene.objects` that belong to `target_mod_name`
    using in-memory object attributes only (`reskate_target_mod_name`, `target_mod_name` in state,
    or `reskate_last_compiled_mod`) without hitting disk registries.
    """
    if isinstance(context, str):
        context, target_mod_name = (target_mod_name if not isinstance(target_mod_name, str) else bpy.context), context
    if context is None:
        context = bpy.context
    all_props = get_all_scene_prop_meshes(context)
    if not all_props:
        return []

    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    prop_grp = getattr(scene, "reskate_prop", None) if scene else None
    raw_mod = (target_mod_name or (getattr(prop_grp, "target_mod_name", "") if prop_grp else "") or "").strip()
    resolved_mod = get_resolved_target_mod_name(context, raw_mod or prop_grp)

    now = time.monotonic()
    scn_ptr = scene.as_pointer() if (scene is not None and hasattr(scene, "as_pointer")) else id(scene)
    scn_cnt = len(all_props)
    cache_key = (scn_ptr, scn_cnt, raw_mod, resolved_mod)
    hit = _PACK_PROP_MESHES_CACHE.get(cache_key)
    if hit is not None and (now - hit[0]) < 1.5 and scene is not None:
        cached_objs = []
        for nm in hit[1]:
            o_ref = scene.objects.get(nm)
            if o_ref is not None:
                cached_objs.append(o_ref)
        if len(cached_objs) == len(hit[1]):
            return cached_objs

    want_mods = {m.lower() for m in (raw_mod, resolved_mod) if m}
    matched: list[bpy.types.Object] = []
    for o in all_props:
        st_dict = get_or_init_object_prop_state(o)
        o_mod = str(
            o.get("reskate_target_mod_name", "")
            or st_dict.get("target_mod_name", "")
            or o.get("reskate_last_compiled_mod", "")
            or ""
        ).strip()
        if not o_mod:
            continue
        unassigned_raw = str(o.get("reskate_unassigned_packs_csv", "") or "")
        unassigned_set = {u.strip().lower() for u in unassigned_raw.split(",") if u.strip()}
        if want_mods and bool(want_mods & unassigned_set):
            continue
        o_mod_resolved = get_resolved_target_mod_name(context, o_mod) if o_mod else ""
        if want_mods and (
            o_mod.lower() in want_mods
            or (o_mod_resolved and o_mod_resolved.lower() in want_mods)
        ):
            matched.append(o)

    matched = _sort_prop_meshes_by_pack_order(matched)
    if len(_PACK_PROP_MESHES_CACHE) > 32:
        _PACK_PROP_MESHES_CACHE.clear()
    _PACK_PROP_MESHES_CACHE[cache_key] = (now, [o.name for o in matched])
    return matched


def get_scene_prop_packs_summary(context) -> list[dict]:
    """
    Returns a summary of all Prop Packs assigned to prop meshes in the current `.blend` scene
    from in-memory object attributes only (`reskate_target_mod_name`, `target_mod_name` in object state,
    or `reskate_last_compiled_mod`).
    Each entry is a dict:
      `{"pack_name": str, "resolved_mod": str, "count": int, "object_names": list[str], "is_active": bool}`.
    """
    global _SCENE_PROP_PACKS_SUMMARY_CACHE
    all_props = get_all_scene_prop_meshes(context)
    if not all_props:
        return []

    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    prop_grp = getattr(scene, "reskate_prop", None) if scene else None
    active_raw_mod = (getattr(prop_grp, "target_mod_name", "") if prop_grp else "").strip()
    active_resolved = get_resolved_target_mod_name(context, prop_grp) if prop_grp else ""

    now = time.monotonic()
    scn_ptr = scene.as_pointer() if (scene is not None and hasattr(scene, "as_pointer")) else id(scene)
    summary_key = (scn_ptr, len(all_props))
    s_ts, s_key, s_val = _SCENE_PROP_PACKS_SUMMARY_CACHE
    if s_key == summary_key and (now - s_ts) < 2.0 and s_val is not None:
        out_cached: list[dict] = []
        for item in s_val:
            c_item = dict(item)
            res_low = c_item["resolved_mod"].lower()
            c_item["is_active"] = bool(
                (active_resolved and res_low == active_resolved.lower())
                or (active_raw_mod and res_low == active_raw_mod.lower())
            )
            out_cached.append(c_item)
        out_cached.sort(key=lambda d: (0 if d["is_active"] else 1, d["pack_name"].lower()))
        return out_cached

    # Map: resolved_mod_lower -> {"pack_name": str, "resolved_mod": str, "objs": dict[str, Object]}
    by_mod: dict[str, dict] = {}
    author_nm = get_author_name(context, prop_grp)

    def _add_obj_to_pack(raw_name: str, obj_item: bpy.types.Object):
        r_clean = (raw_name or "").strip()
        if not r_clean:
            return
        res_mod = get_resolved_target_mod_name(context, r_clean) or r_clean
        key = res_mod.lower()
        entry = by_mod.get(key)
        if entry is None:
            # Prefer clean short pack name when possible (strip leading '<Author>_')
            short_nm = r_clean
            if author_nm and short_nm.lower().startswith(f"{author_nm.lower()}_"):
                short_nm = short_nm[len(author_nm) + 1 :] or r_clean
            entry = {
                "pack_name": short_nm,
                "resolved_mod": res_mod,
                "objs": {},
            }
            by_mod[key] = entry
        entry["objs"][obj_item.name] = obj_item

    for o in all_props:
        st_dict = get_or_init_object_prop_state(o)
        explicit_mod = str(
            o.get("reskate_target_mod_name", "")
            or st_dict.get("target_mod_name", "")
            or o.get("reskate_last_compiled_mod", "")
            or ""
        ).strip()
        if explicit_mod:
            unassigned_raw = str(o.get("reskate_unassigned_packs_csv", "") or "")
            unassigned_set = {u.strip().lower() for u in unassigned_raw.split(",") if u.strip()}
            res_exp = get_resolved_target_mod_name(context, explicit_mod) or explicit_mod
            if explicit_mod.lower() not in unassigned_set and res_exp.lower() not in unassigned_set:
                _add_obj_to_pack(explicit_mod, o)

    out: list[dict] = []
    for key, info in by_mod.items():
        objs_map = info["objs"]
        if not objs_map:
            continue
        is_act = bool(
            (active_resolved and key == active_resolved.lower())
            or (active_raw_mod and key == active_raw_mod.lower())
        )
        out.append({
            "pack_name": info["pack_name"],
            "resolved_mod": info["resolved_mod"],
            "count": len(objs_map),
            "object_names": list(objs_map.keys()),
            "is_active": is_act,
        })

    out.sort(key=lambda d: (0 if d["is_active"] else 1, d["pack_name"].lower()))
    _SCENE_PROP_PACKS_SUMMARY_CACHE = (now, summary_key, out)
    return list(out)



def get_compile_block_reason(context, obj: bpy.types.Object | None = None) -> str | None:
    """
    Checks whether compiling is currently blocked and returns a concise human-readable reason string
    (or None if ready to compile). Used to disable the Compile button on Page 5 with an explicit reason (§3 Page 5).
    """
    author = get_author_name(context)
    if not author:
        return "Set Author Name in Edit → Preferences → Add-ons → ReSkate Prop Studio."
    game_dir = get_game_dir(context)
    if not is_game_dir_valid(game_dir):
        return "Set a valid Skate Game Folder in Edit → Preferences → Add-ons → ReSkate Prop Studio."
    cli_st = get_reskate_cli_status(game_dir, context=context)
    if not cli_st.get("ok"):
        return str(cli_st.get("message") or "ReSkate Studio is missing.")

    target_obj = obj
    if target_obj is None and context is not None:
        sel_list = get_selected_prop_meshes(context)
        target_obj = sel_list[0] if sel_list else get_target_mesh_object(context)

    if target_obj is None or getattr(target_obj, "type", None) != 'MESH' or not target_obj.data:
        return "Select a 3D mesh object to compile."
    if len(target_obj.data.polygons) == 0:
        return f"'{target_obj.name}' has 0 polygons."
    if len(target_obj.data.uv_layers) == 0:
        return f"'{target_obj.name}' is missing a UV map."
    return None


def compute_pack_compile_diff(
    context,
    target_mod_name: str,
    single_obj: bpy.types.Object | None = None,
    selected_objs: list[bpy.types.Object] | None = None,
) -> dict:
    """
    Computes a clear pre-compile summary comparing the current prop(s) to be compiled
    against `<Staging>/Packs/<target_mod_name>/pack_registry.json`:
      - `adding`: list of display names being added for the first time
      - `updating`: list of display names already in the pack whose geometry/PBR/display_name changed
      - `unchanged`: list of display names already in the pack that are unchanged since last compile
      - `keeping`: list of other display names in the pack registry that will be kept untouched
    """
    resolved_mod = get_resolved_target_mod_name(context, target_mod_override=target_mod_name)
    scene = getattr(context, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None
    merge_flag = True if prop is None else bool(getattr(prop, "merge_existing_mod", True))
    sel_sig = tuple(o.name for o in selected_objs) if selected_objs else ((single_obj.name,) if single_obj else ())
    diff_cache_key = (resolved_mod, sel_sig, merge_flag)
    now_diff = time.monotonic()
    hit_diff = _PACK_COMPILE_DIFF_CACHE.get(diff_cache_key)
    if hit_diff is not None and (now_diff - hit_diff[0]) < 1.0:
        return dict(hit_diff[1])

    info = inspect_existing_mod_props(resolved_mod)
    existing_map = {}
    for pd in info.get("props_detail", []):
        pk = pd.get("prop_key", "")
        pid = pd.get("prop_id", "")
        if pk:
            existing_map[pk] = pd.get("display_name", pk)
        if pid:
            existing_map[f"own_customprops_{pid}"] = pd.get("display_name", pid)

    incoming: list[tuple[str, str, bpy.types.Object | None]] = []

    if selected_objs:
        for s_obj in selected_objs:
            st = get_or_init_object_prop_state(s_obj)
            slug = get_prop_slug(context, s_obj, target_mod_override=resolved_mod)
            dname = str(st.get("prop_display_name") or s_obj.name)
            incoming.append((slug, dname, s_obj))
    elif single_obj is not None:
        slug = get_prop_slug(context, single_obj, target_mod_override=resolved_mod)
        dname = (prop.prop_display_name if (prop and prop.prop_display_name) else single_obj.name)
        incoming.append((slug, dname, single_obj))
    elif scene is not None:
        sel_list = get_selected_prop_meshes(context)
        if sel_list:
            for s_obj in sel_list:
                st = get_or_init_object_prop_state(s_obj)
                slug = get_prop_slug(context, s_obj, target_mod_override=resolved_mod)
                dname = str(st.get("prop_display_name") or s_obj.name)
                incoming.append((slug, dname, s_obj))
        else:
            queue = getattr(scene, "reskate_pack_queue", [])
            enabled_items = [it for it in queue if getattr(it, "enabled", getattr(it, "is_enabled", True))]
            if enabled_items:
                for it in enabled_items:
                    q_obj = scene.objects.get(it.name) or bpy.data.objects.get(it.name)
                    raw_k = "".join(c if c.isalnum() or c == '_' else '_' for c in (it.prop_key or it.prop_key_slug or it.name).lower()).strip('_')
                    slug = clamp_slug(raw_k if raw_k.startswith("own_") else f"own_customprops_{raw_k}", 36)
                    incoming.append((slug, it.display_name or it.name, q_obj))
            else:
                active_obj = get_target_mesh_object(context)
                if active_obj is not None:
                    slug = get_prop_slug(context, active_obj, target_mod_override=resolved_mod)
                    dname = (prop.prop_display_name if (prop and prop.prop_display_name) else active_obj.name)
                    incoming.append((slug, dname, active_obj))

    adding = []
    updating = []
    unchanged = []
    incoming_keys = set()
    for slug, dname, inc_obj in incoming:
        incoming_keys.add(slug)
        if slug in existing_map:
            prev_disp = str(existing_map.get(slug, ""))
            is_same_fp = False
            if inc_obj is not None and getattr(inc_obj, "type", None) == 'MESH':
                last_fp = str(inc_obj.get("reskate_last_compiled_fp", "") or "")
                last_mod = str(inc_obj.get("reskate_last_compiled_mod", "") or "")
                if last_fp and last_mod == resolved_mod and prev_disp == dname:
                    try:
                        cur_fp = compute_prop_scene_fingerprint(context, inc_obj)
                        if cur_fp == last_fp:
                            is_same_fp = True
                    except Exception:
                        is_same_fp = False
            if is_same_fp:
                unchanged.append(dname)
            else:
                updating.append(dname)
        else:
            adding.append(dname)

    keeping = []
    if prop is None or bool(getattr(prop, "merge_existing_mod", True)):
        for pd in info.get("props_detail", []):
            pk = pd.get("prop_key", "")
            if pk and pk not in incoming_keys:
                keeping.append(pd.get("display_name", pk))

    res_diff = {
        "adding": adding,
        "updating": updating,
        "unchanged": unchanged,
        "keeping": keeping,
    }
    if len(_PACK_COMPILE_DIFF_CACHE) > 32:
        _PACK_COMPILE_DIFF_CACHE.clear()
    _PACK_COMPILE_DIFF_CACHE[diff_cache_key] = (now_diff, res_diff)
    return dict(res_diff)


_OBJ_PACK_MEMBERSHIP_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}


def get_object_pack_memberships(context=None, obj: bpy.types.Object | None = None) -> list[str]:
    """
    Returns a list of Prop Pack names (`<Author>-<PackName>` or `<PackName>`) that `obj`
    belongs to from in-memory object attributes only (`reskate_target_mod_name`, `target_mod_name`,
    or `reskate_last_compiled_mod`) without scanning disk pack registries.
    """
    if getattr(context, "type", None) == 'MESH' and getattr(obj, "type", None) != 'MESH':
        obj, context = context, obj
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return []
    st = get_or_init_object_prop_state(obj)
    last_mod = str(obj.get("reskate_last_compiled_mod", "") or "").strip()
    own_mod = str(
        obj.get("reskate_target_mod_name", "")
        or st.get("target_mod_name", "")
        or last_mod
        or ""
    ).strip()
    packs: list[str] = []
    for cand in (own_mod, last_mod):
        if cand and cand not in packs:
            packs.append(cand)
    return packs



def check_mesh_thin_or_single_sided_faces(
    obj: bpy.types.Object | None,
    context=None,
    min_thickness_m: float = 0.02,
) -> dict:
    """
    Modular collision thickness & concave step analyzer (Item 3 & Item 10).
    Detects:
      1. Overall bounding-box thinness (`min(dx, dy, dz) < min_thickness_m`, default 2.0 cm)
      2. Open single-sided mesh sheets (boundary edge ratio > 15% without solid back-face volume)
      3. Thin connected mesh islands `< 2.0 cm` inside a larger bounding box (e.g. spiral staircase steps
         where the overall staircase is 2m x 2m x 3m, but individual treads are paper-thin or single-sided)
      4. Multi-step concave staircase geometry where `SIMPLE_HULLS` could bridge across steps.
    If `obj` already has a thickened `<Prop>_COL` proxy (`obj.get("reskate_col_thickened")`), reports thickness as resolved.
    """
    res = {
        "is_too_thin": False,
        "is_thin": False,
        "overall_thin": False,
        "is_single_sided": False,
        "has_open_faces": False,
        "has_thin_islands": False,
        "thin_island_count": 0,
        "boundary_edge_count": 0,
        "min_dim_m": 0.1,
        "has_concave_step_risk": False,
        "is_staircase_like": False,
        "col_thickened": False,
        "summary": "",
        "reason": "",
    }
    if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
        return res

    col_obj = resolve_custom_collision_object(context, obj) if context else bpy.data.objects.get(f"{obj.name}_COL")
    if bool(obj.get("reskate_col_thickened", False)) and col_obj is not None and getattr(col_obj, "type", None) == 'MESH':
        res["col_thickened"] = True
        res["min_dim_m"] = float(obj.get("reskate_col_thickened_m", 0.025) or 0.025)
        return res

    me = obj.data
    n_verts = len(me.vertices)
    n_polys = len(me.polygons)
    if n_verts == 0 or n_polys == 0:
        return res

    sx, sy, sz = (abs(float(obj.scale[0])) or 1.0, abs(float(obj.scale[1])) or 1.0, abs(float(obj.scale[2])) or 1.0)
    thin_cache_key = (
        obj.name,
        n_verts,
        n_polys,
        len(me.edges),
        round(sx, 4),
        round(sy, 4),
        round(sz, 4),
        round(float(min_thickness_m), 4),
        bool(obj.get("reskate_col_thickened", False)),
    )
    if obj.mode != 'EDIT' and thin_cache_key in _THIN_MESH_CACHE:
        return dict(_THIN_MESH_CACHE[thin_cache_key])

    v_buf = np.empty(n_verts * 3, dtype=np.float32)
    me.vertices.foreach_get("co", v_buf)
    coords = v_buf.reshape((-1, 3)) * np.array([sx, sy, sz], dtype=np.float32)
    bmin = coords.min(axis=0)
    bmax = coords.max(axis=0)
    dims = bmax - bmin
    min_dim = float(min(dims[0], dims[1], dims[2]))
    max_dim = float(max(dims[0], dims[1], dims[2]))
    res["min_dim_m"] = min_dim

    if min_dim < min_thickness_m:
        res["overall_thin"] = True

    # Inspect BMesh topology for single-sided open sheets & thin sub-islands (e.g. spiral staircase treads)
    if n_polys <= 25000:
        bm = bmesh.new()
        try:
            bm.from_mesh(me)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            total_edges = max(1, len(bm.edges))
            boundary_edges = sum(1 for e in bm.edges if len(e.link_faces) <= 1)
            res["boundary_edge_count"] = int(boundary_edges)

            # Check connected components (islands) for thinness or open sheets
            visited_v: set[int] = set()
            thin_islands = 0
            open_sheet_islands = 0
            step_z_levels: list[float] = []

            for v in bm.verts:
                if v.index in visited_v or not v.link_faces:
                    continue
                stack = [v]
                visited_v.add(v.index)
                comp_verts = []
                comp_edges = set()
                comp_faces = set()
                while stack:
                    cur = stack.pop()
                    comp_verts.append(cur)
                    for e in cur.link_edges:
                        comp_edges.add(e)
                        other = e.other_vert(cur)
                        if other.index not in visited_v:
                            visited_v.add(other.index)
                            stack.append(other)
                    for f in cur.link_faces:
                        comp_faces.add(f)

                if not comp_faces:
                    continue
                pts = np.array(
                    [[cv.co.x * sx, cv.co.y * sy, cv.co.z * sz] for cv in comp_verts],
                    dtype=np.float32,
                )
                c_min = pts.min(axis=0)
                c_max = pts.max(axis=0)
                c_dims = c_max - c_min
                c_min_dim = float(min(c_dims[0], c_dims[1], c_dims[2]))
                c_max_dim = float(max(c_dims[0], c_dims[1], c_dims[2]))

                # Also check PCA / normal thickness for angled treads or panels
                if c_min_dim >= min_thickness_m and len(comp_faces) <= 64 and len(pts) >= 3:
                    f_list = list(comp_faces)
                    n0 = f_list[0].normal
                    if n0.length > 1e-5:
                        n_vec = np.array([n0.x, n0.y, n0.z], dtype=np.float32)
                        n_vec /= max(1e-6, float(np.linalg.norm(n_vec)))
                        proj = pts @ n_vec
                        normal_span = float(proj.max() - proj.min())
                        c_min_dim = min(c_min_dim, normal_span)

                comp_boundary = sum(1 for ce in comp_edges if len(ce.link_faces) <= 1)
                if c_max_dim > 0.08 and c_min_dim < min_thickness_m:
                    thin_islands += 1
                    res["min_dim_m"] = min(res["min_dim_m"], c_min_dim)
                elif comp_boundary > 0 and (comp_boundary / max(1, len(comp_edges))) > 0.25 and c_min_dim < (min_thickness_m * 1.5):
                    open_sheet_islands += 1
                    res["min_dim_m"] = min(res["min_dim_m"], c_min_dim)

                # Track horizontal step heights for spiral/multi-step staircase detection
                up_faces = [f for f in comp_faces if abs(float(f.normal.z)) > 0.75]
                if up_faces:
                    avg_z = sum(float((f.calc_center_median().z) * sz) for f in up_faces) / len(up_faces)
                    step_z_levels.append(round(avg_z, 2))

            # Even on a single connected mesh (e.g. spiral staircase welded to central pole),
            # check if upward-facing step faces are single-sided (no downward-facing back-face within 2.5 cm below them)
            upward_treads = [f for f in bm.faces if float(f.normal.z) > 0.70 and f.calc_area() * (sx * sy) > 0.004]
            downward_undersides = [f for f in bm.faces if float(f.normal.z) < -0.70 and f.calc_area() * (sx * sy) > 0.002]
            if len(upward_treads) >= 4:
                distinct_step_z = sorted({round(float(f.calc_center_median().z * sz), 2) for f in upward_treads})
                if len(distinct_step_z) >= 4:
                    res["has_concave_step_risk"] = True
                # Check if upward treads lack a matching bottom face within 3 cm below them
                unbacked_treads = 0
                down_centers = [(f.calc_center_median(), f.normal) for f in downward_undersides]
                for uf in upward_treads[:40]:
                    uc = uf.calc_center_median()
                    has_bottom = False
                    for dc, _dn in down_centers:
                        dz = (uc.z - dc.z) * sz
                        dxy = math.hypot((uc.x - dc.x) * sx, (uc.y - dc.y) * sy)
                        if 0.001 <= dz <= 0.12 and dxy < 0.25:
                            has_bottom = True
                            break
                    if not has_bottom:
                        unbacked_treads += 1
                if unbacked_treads >= max(2, len(upward_treads[:40]) // 3):
                    res["is_single_sided"] = True
                    res["min_dim_m"] = min(res["min_dim_m"], 0.002)

            if boundary_edges > 0 and (boundary_edges / total_edges) > 0.22 and len(downward_undersides) == 0:
                res["is_single_sided"] = True
                res["min_dim_m"] = min(res["min_dim_m"], 0.002)

            if thin_islands > 0 or open_sheet_islands > 0:
                res["has_thin_islands"] = True
                res["thin_island_count"] = int(thin_islands + open_sheet_islands)
        finally:
            bm.free()

    res["is_too_thin"] = bool(res["overall_thin"] or res["is_single_sided"] or res["has_thin_islands"])
    res["is_thin"] = res["is_too_thin"]
    res["has_open_faces"] = bool(res["is_single_sided"])
    res["is_staircase_like"] = bool(res["has_concave_step_risk"])
    if res["is_too_thin"]:
        if res["has_thin_islands"] and not res["overall_thin"]:
            res["summary"] = (
                f"Too thin collision geometry ({res['thin_island_count']} thin/open step or panel island(s) "
                f"< {min_thickness_m * 100:.0f} cm — skater wheels may clip through)"
            )
        elif res["is_single_sided"]:
            res["summary"] = (
                f"Too thin / single-sided mesh faces detected ({res['boundary_edge_count']} open boundary edges "
                f"< {min_thickness_m * 100:.0f} cm thickness — skater wheels may clip through)"
            )
        else:
            res["summary"] = (
                f"Too thin geometry ({res['min_dim_m'] * 100:.1f} cm < {min_thickness_m * 100:.0f} cm minimum "
                f"— skater wheels may clip through)"
            )
        res["reason"] = res["summary"]
    if obj.mode != 'EDIT':
        if len(_THIN_MESH_CACHE) > 64:
            _THIN_MESH_CACHE.clear()
        _THIN_MESH_CACHE[thin_cache_key] = dict(res)
    return res


def check_mesh_uv_bounds(obj: bpy.types.Object | None, tolerance: float = 0.02) -> dict:
    """
    Checks whether `obj` has a UV map and whether all UV coordinates lie within `[0.0 - tolerance, 1.0 + tolerance]`.
    Returns:
      `{"status": "MISSING"|"OUT_OF_BOUNDS"|"OK", "uv_name": str, "out_of_bounds_count": int,
        "total_uvs": int, "min_u": float, "max_u": float, "min_v": float, "max_v": float, "label": str}`
    """
    if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
        return {
            "status": "MISSING",
            "uv_name": "",
            "out_of_bounds_count": 0,
            "total_uvs": 0,
            "min_u": 0.0,
            "max_u": 0.0,
            "min_v": 0.0,
            "max_v": 0.0,
            "label": "Missing UV Map (required for Frostbite shader)",
        }

    me = obj.data
    if len(me.uv_layers) == 0:
        return {
            "status": "MISSING",
            "uv_name": "",
            "out_of_bounds_count": 0,
            "total_uvs": 0,
            "min_u": 0.0,
            "max_u": 0.0,
            "min_v": 0.0,
            "max_v": 0.0,
            "label": "Missing UV Map (required for Frostbite shader)",
        }

    uv_layer = me.uv_layers.active or me.uv_layers[0]
    n_loops = len(uv_layer.data)
    if n_loops == 0:
        return {
            "status": "OK",
            "uv_name": uv_layer.name,
            "out_of_bounds_count": 0,
            "total_uvs": 0,
            "min_u": 0.0,
            "max_u": 1.0,
            "min_v": 0.0,
            "max_v": 1.0,
            "label": f"UV Map '{uv_layer.name}' within 0–1 bounds",
        }

    uv_cache_key = (obj.name, uv_layer.name, n_loops, len(me.vertices), len(me.polygons), round(float(tolerance), 4))
    if obj.mode != 'EDIT' and uv_cache_key in _UV_BOUNDS_CACHE:
        return dict(_UV_BOUNDS_CACHE[uv_cache_key])

    uv_buf = np.empty(n_loops * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv_buf)
    uvs = uv_buf.reshape((-1, 2))
    min_u, min_v = float(uvs[:, 0].min()), float(uvs[:, 1].min())
    max_u, max_v = float(uvs[:, 0].max()), float(uvs[:, 1].max())
    lo = -float(tolerance)
    hi = 1.0 + float(tolerance)
    oob_mask = (uvs[:, 0] < lo) | (uvs[:, 0] > hi) | (uvs[:, 1] < lo) | (uvs[:, 1] > hi)
    oob_count = int(np.count_nonzero(oob_mask))

    if oob_count > 0:
        res_uv = {
            "status": "OUT_OF_BOUNDS",
            "uv_name": uv_layer.name,
            "out_of_bounds_count": oob_count,
            "total_uvs": int(n_loops),
            "min_u": min_u,
            "max_u": max_u,
            "min_v": min_v,
            "max_v": max_v,
            "label": (
                f"UV Map '{uv_layer.name}' outside 0–1 bounds "
                f"({oob_count:,} UVs in U[{min_u:.2f}..{max_u:.2f}], V[{min_v:.2f}..{max_v:.2f}])"
            ),
        }
    else:
        res_uv = {
            "status": "OK",
            "uv_name": uv_layer.name,
            "out_of_bounds_count": 0,
            "total_uvs": int(n_loops),
            "min_u": min_u,
            "max_u": max_u,
            "min_v": min_v,
            "max_v": max_v,
            "label": f"UV Map '{uv_layer.name}' within 0–1 bounds",
        }
    if obj.mode != 'EDIT':
        if len(_UV_BOUNDS_CACHE) > 64:
            _UV_BOUNDS_CACHE.clear()
        _UV_BOUNDS_CACHE[uv_cache_key] = dict(res_uv)
    return res_uv


PACK_README_ATTRIBUTION_FOOTER = (
    "\n---\n"
    f"*Created with [ReSkate Prop Studio]({GITHUB_REPO_URL})*\n"
)


def ensure_pack_readme_footer(readme_text: str) -> str:
    """Appends the 'Created with ReSkate Prop Studio' GitHub attribution footer if not already present."""
    base = str(readme_text or "").rstrip()
    if "github.com/camerontutt/reskate-prop-studio" in base.lower():
        return base + "\n"
    if not base:
        return PACK_README_ATTRIBUTION_FOOTER.lstrip("\n")
    return f"{base}\n{PACK_README_ATTRIBUTION_FOOTER}"


def generate_pack_readme_markdown(manifest_data: dict) -> str:
    """
    Auto-generates a Thunderstore & mod-folder `README.md` with pack metadata and the full object list
    (including Variant Family groupings when present).
    """
    pack_name = str(manifest_data.get("pack_name") or manifest_data.get("target_mod_name") or "CustomProps").strip()
    author = str(manifest_data.get("author_name") or "ReSkateAuthor").strip()
    version = str(manifest_data.get("pack_version") or "0.1.0").strip()
    desc = str(
        manifest_data.get("pack_description")
        or manifest_data.get("description")
        or "Custom skateable props built with ReSkate Prop Studio."
    ).strip()

    props_list = manifest_data.get("props")
    if not isinstance(props_list, list) or not props_list:
        props_list = [manifest_data] if manifest_data.get("display_name") or manifest_data.get("prop_id") else []

    lines = [
        f"# {pack_name}",
        "",
        desc,
        "",
        f"- **Author:** {author}",
        f"- **Version:** {version}",
        f"- **Total Props:** {len(props_list)}",
        "",
        "## Included Objects",
        "",
    ]

    if props_list:
        for idx, p in enumerate(props_list, 1):
            d_name = str(p.get("display_name") or p.get("name") or p.get("prop_id") or f"Prop {idx}").strip()
            cat = str(p.get("category") or "Custom Props").strip()
            fam = str(p.get("family_title") or p.get("variant_group") or "").strip()
            fam_suffix = f" *(Variant Family: {fam})*" if fam else ""
            lines.append(f"{idx}. **{d_name}** — `{cat}`{fam_suffix}")
    else:
        lines.append("- Standalone Quick Drop Prop Pack")

    folder_name = pack_name if pack_name.lower().startswith(f"{author.lower()}-") else f"{author}-{pack_name}"
    lines.extend([
        "",
        "## Installation",
        "",
        f"1. Copy the `{folder_name}` folder into your `Skate/Mods/` directory.",
        "2. Enable the pack in ReSkate Manager / `mods.json` and launch Skate.",
        "",
    ])
    return ensure_pack_readme_footer("\n".join(lines))


def run_preflight_checks(context, obj: bpy.types.Object | None) -> list[dict]:
    """
    Runs the Step 7 Pre-flight checklist on `obj` and returns a list of check items:
      [{"level": "OK"|"WARN"|"ERROR", "label": str, "fix_action": str | None}]
    """
    checks = []
    if not obj or obj.type != 'MESH' or not obj.data:
        return [{"level": "ERROR", "label": "Select a 3D mesh object with polygons", "fix_action": None, "badge": "❌"}]

    mesh = obj.data
    sx, sy, sz = obj.scale
    rx, ry, rz = obj.rotation_euler
    prop_scn = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    obj_st_pf = get_or_init_object_prop_state(obj)
    col_mode_pf = str(obj_st_pf.get("collision_mode") or (getattr(prop_scn, "collision_mode", "SAME_AS_MESH") if prop_scn else "SAME_AS_MESH"))
    is_no_col_pf = col_mode_pf in ("NO_COLLISION", "NONE", "DECAL_ONLY")
    bbox = obj.bound_box
    bbox_sig = (round(float(bbox[0][0]), 3), round(float(bbox[0][1]), 3), round(float(bbox[0][2]), 3), round(float(bbox[6][0]), 3), round(float(bbox[6][1]), 3), round(float(bbox[6][2]), 3)) if bbox else ()
    uv_active_name = (mesh.uv_layers.active or mesh.uv_layers[0]).name if len(mesh.uv_layers) > 0 else ""
    pf_cache_key = (
        obj.name,
        len(mesh.vertices),
        len(mesh.polygons),
        len(mesh.loops),
        len(mesh.uv_layers),
        uv_active_name,
        round(float(sx), 4),
        round(float(sy), 4),
        round(float(sz), 4),
        round(float(rx), 4),
        round(float(ry), 4),
        round(float(rz), 4),
        bbox_sig,
        bool(obj.get("reskate_custom_origin", False)),
        bool(obj.get("reskate_col_thickened", False)),
        col_mode_pf,
        str(obj.get("reskate_family_title", "") or obj_st_pf.get("prop_family_title", "") or ""),
        str(obj.get("reskate_thumb_rendered_fp", "") or ""),
        obj.get("reskate_thumb_rendered_notch", None),
        bool(obj.get("reskate_thumb_dirty", False)),
    )
    if obj.mode != 'EDIT' and pf_cache_key in _PREFLIGHT_CHECKS_CACHE:
        return [dict(c) for c in _PREFLIGHT_CHECKS_CACHE[pf_cache_key]]

    # 1. Scale & Rotation check
    unapplied_scale = abs(sx - 1.0) > 1e-3 or abs(sy - 1.0) > 1e-3 or abs(sz - 1.0) > 1e-3
    unapplied_rot = abs(rx) > 1e-3 or abs(ry) > 1e-3 or abs(rz) > 1e-3
    if unapplied_scale or unapplied_rot:
        checks.append({
            "level": "WARN",
            "label": f"Unapplied Transform (Scale {sx:.2f},{sy:.2f},{sz:.2f} | Rot {math.degrees(rx):.0f}°,{math.degrees(ry):.0f}°,{math.degrees(rz):.0f}°)",
            "fix_action": "APPLY_SCALE_ROT",
        })
    else:
        checks.append({"level": "OK", "label": "Scale (1,1,1) & Rotation applied", "fix_action": None})

    # 2. Origin at bottom-center / ground-contact check (including Preferences Origin Height Offset +0.05m)
    if len(mesh.vertices) > 0:
        if bool(obj.get("reskate_custom_origin", False)):
            checks.append({"level": "OK", "label": "Origin set (Quick Drop pivot ready)", "fix_action": None})
        else:
            try:
                from . import properties as _props
                z_off_pref = float(_props.get_origin_ground_z_offset(context))
            except Exception:
                z_off_pref = 0.05
            if bbox:
                xs = [float(pt[0]) for pt in bbox]
                ys = [float(pt[1]) for pt in bbox]
                zs = [float(pt[2]) for pt in bbox]
                cx = 0.5 * (min(xs) + max(xs))
                cy = 0.5 * (min(ys) + max(ys))
                min_z = min(zs)
            else:
                v_buf = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
                mesh.vertices.foreach_get("co", v_buf)
                coords = v_buf.reshape((-1, 3))
                bmin = coords.min(axis=0)
                bmax = coords.max(axis=0)
                cx = float(0.5 * (bmin[0] + bmax[0]))
                cy = float(0.5 * (bmin[1] + bmax[1]))
                min_z = float(bmin[2])
            min_z_ok = (-abs(z_off_pref) - 0.08) <= min_z <= 0.08
            if abs(cx) > 0.08 or abs(cy) > 0.08 or not min_z_ok:
                checks.append({
                    "level": "WARN",
                    "label": f"Origin offset from ground contact (dXY={math.hypot(cx, cy):.2f}m, minZ={min_z:.2f}m)",
                    "fix_action": "ORIGIN_BOTTOM_CENTER",
                    "fix_label": "Ground Contact",
                    "secondary_fix_action": "LOCK_CUSTOM_ORIGIN",
                    "secondary_fix_label": "Keep Custom",
                })
            else:
                checks.append({"level": "OK", "label": "Origin at ground contact (Quick Drop pivot ready)", "fix_action": None})

    # 3. N-gons (>4 verts per face) using C-array foreach_get on loop_total
    n_polys = len(mesh.polygons)
    n_loops = len(mesh.loops)
    ngon_count = 0
    if n_polys > 0:
        try:
            lt_buf = np.empty(n_polys, dtype=np.int32)
            mesh.polygons.foreach_get("loop_total", lt_buf)
            ngon_count = int(np.count_nonzero(lt_buf > 4))
        except Exception:
            ngon_count = sum(1 for p in mesh.polygons if len(p.vertices) > 4)
    if ngon_count > 0:
        checks.append({
            "level": "WARN",
            "label": f"{ngon_count} N-gon face(s) (>4 vertices) detected",
            "fix_action": "TRIANGULATE_NGONS",
        })
    else:
        checks.append({"level": "OK", "label": "No N-gons (clean Quads/Triangles)", "fix_action": None})

    # 5. UV Map missing or outside 0–1 bounds check (Item 10 - strictly advisory, never repack UVs)
    uv_res = check_mesh_uv_bounds(obj)
    if uv_res["status"] == "MISSING":
        checks.append({
            "level": "WARN",
            "label": f"{uv_res['label']} (Manual UV unwrap recommended)",
            "fix_action": None,
        })
    elif uv_res["status"] == "OUT_OF_BOUNDS":
        checks.append({
            "level": "WARN",
            "label": f"{uv_res['label']} (Manual UV layout check recommended)",
            "fix_action": None,
        })
    else:
        checks.append({
            "level": "OK",
            "label": uv_res["label"],
            "fix_action": None,
        })

    # 6. Triangle count > 20,000 (O(1) exact formula: loops - 2 * polygons)
    tri_count = max(0, n_loops - 2 * n_polys) if n_polys > 0 else 0
    if tri_count > 20000:
        checks.append({
            "level": "WARN",
            "label": f"High triangle count ({tri_count:,} tris > 20,000 budget)",
            "fix_action": None,
        })
    else:
        checks.append({"level": "OK", "label": f"Triangle budget OK ({tri_count:,} tris)", "fix_action": None})

    # Havok 1:1 single-shape ceiling check
    if not is_no_col_pf:
        h_stat = get_havok_ceiling_status(context, obj)
        if h_stat.get("exceeds_ceiling"):
            checks.append({
                "level": "WARN",
                "label": f"Havok 1:1 Ceiling Exceeded ({h_stat['welded_verts']:,}v / {h_stat['tris']:,}t > 2,600 / 5,000) — grind edges may not match",
                "fix_action": "GENERATE_SIMPLIFIED_COL",
                "fix_label": "Simplify",
            })
        elif h_stat.get("is_same_as_mesh"):
            checks.append({
                "level": "OK",
                "label": f"Havok 1:1 Ceiling OK ({h_stat['welded_verts']:,}v / {h_stat['tris']:,}t <= 2,600 / 5,000)",
                "fix_action": None,
            })

    # 7. Thumbnail up-to-date & Variant Corner Notch check
    thumb_outdated, thumb_reason = is_prop_thumbnail_out_of_date(context, obj)
    other_outdated_cnt = 0
    try:
        for o_other in get_selected_prop_meshes(context):
            if o_other is not None and o_other != obj and is_prop_thumbnail_out_of_date(context, o_other)[0]:
                other_outdated_cnt += 1
    except Exception:
        other_outdated_cnt = 0

    if thumb_outdated:
        extra_sfx = f" (+{other_outdated_cnt} other prop(s) in pack)" if other_outdated_cnt > 0 else ""
        checks.append({
            "level": "WARN",
            "label": f"Thumbnail out of date: {thumb_reason}{extra_sfx}",
            "fix_action": "RERENDER_OUTDATED_THUMBS",
            "fix_label": "Re-Render Out-of-Date Thumbnails",
        })
    elif other_outdated_cnt > 0:
        checks.append({
            "level": "WARN",
            "label": f"{other_outdated_cnt} other prop thumbnail(s) out of date in pack",
            "fix_action": "RERENDER_OUTDATED_THUMBS",
            "fix_label": "Re-Render Out-of-Date Thumbnails",
        })
    else:
        checks.append({
            "level": "OK",
            "label": "Thumbnail up to date",
            "fix_action": None,
        })

    # 8. Forbidden ebp_chr_* effect check
    slug = get_prop_slug(context, obj)
    if "ebp_chr_" in slug.lower():
        checks.append({
            "level": "ERROR",
            "label": "Prop ID contains forbidden 'ebp_chr_' character effect prefix!",
            "fix_action": None,
        })

    for c in checks:
        lvl = c.get("level", "OK")
        c["badge"] = "✅" if lvl == "OK" else ("⚠️" if lvl == "WARN" else "❌")

    if obj.mode != 'EDIT':
        if len(_PREFLIGHT_CHECKS_CACHE) > 64:
            _PREFLIGHT_CHECKS_CACHE.clear()
        _PREFLIGHT_CHECKS_CACHE[pf_cache_key] = [dict(c) for c in checks]
    return checks


def extract_prop_experimental_data(obj: bpy.types.Object | None, scale_mult: float = 1.0) -> dict:
    """
    Safely parses `obj.get("reskate_exp_json", "{}")` from the companion `ReSkate Lab` add-on
    and syncs live local offsets/rotations/scales from any child `_VFX_` or `_MusicBoombox_Gizmo`
    helper Empties in the Blender scene.
    """
    if obj is None or not is_experimental_addon_enabled():
        return {}
    raw_str = obj.get("reskate_exp_json", "{}")
    if not raw_str:
        return {}
    try:
        exp_data = json.loads(str(raw_str))
        if not isinstance(exp_data, dict):
            return {}
        exp_data.pop("projected_decals", None)
    except Exception:
        return {}

    try:
        inv_parent = obj.matrix_world.inverted_safe()
    except Exception:
        inv_parent = Matrix.Identity(4)

    # Sync live transforms for any VFX helper Empties
    vfx_list = exp_data.get("vfx_emitters")
    if isinstance(vfx_list, list):
        synced_vfx = []
        for entry in vfx_list:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            helper_name = str(item.get("object_name", "") or "")
            helper_obj = bpy.data.objects.get(helper_name) if helper_name else None
            if helper_obj is not None:
                rel_m = inv_parent @ helper_obj.matrix_world
                loc = rel_m.to_translation()
                rot = rel_m.to_euler('XYZ')
                scl = rel_m.to_scale()
                item["offset"] = [
                    round(float(loc.x * scale_mult), 4),
                    round(float(loc.y * scale_mult), 4),
                    round(float(loc.z * scale_mult), 4),
                ]
                item["rotation_euler"] = [
                    round(math.degrees(float(rot.x)), 2),
                    round(math.degrees(float(rot.y)), 2),
                    round(math.degrees(float(rot.z)), 2),
                ]
                item["scale"] = round(float(max(abs(scl.x), abs(scl.y), abs(scl.z), 0.01)), 4)
            synced_vfx.append(item)
        exp_data["vfx_emitters"] = synced_vfx

    # Sync live offset for Music / Boombox emitter gizmo if present
    music = exp_data.get("music_emitter")
    if isinstance(music, dict) and music.get("enabled"):
        m_item = dict(music)
        m_obj_name = str(m_item.get("object_name", "") or f"{obj.name}_MusicBoombox_Gizmo")
        m_obj = bpy.data.objects.get(m_obj_name)
        if m_obj is not None:
            rel_m = inv_parent @ m_obj.matrix_world
            loc = rel_m.to_translation()
            m_item["offset"] = [
                round(float(loc.x * scale_mult), 4),
                round(float(loc.y * scale_mult), 4),
                round(float(loc.z * scale_mult), 4),
            ]
        exp_data["music_emitter"] = m_item

    # Sync scene-level WIP VFX compile toggle if ReSkate Lab is loaded
    try:
        scn = getattr(bpy.context, "scene", None)
        exp_scn = getattr(scn, "reskate_exp", None) if scn else None
        if exp_scn is not None and hasattr(exp_scn, "compile_wip_vfx_in_game"):
            exp_data["compile_wip_vfx_in_game"] = bool(exp_scn.compile_wip_vfx_in_game)
    except Exception:
        pass

    return exp_data


def get_world_bounds_zup(obj: bpy.types.Object) -> tuple[Vector, Vector]:
    """Returns `(bmin_world, bmax_world)` in Blender Z-up world coordinates for `obj`."""
    if not obj or not getattr(obj, "bound_box", None):
        return Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))
    mw = obj.matrix_world
    corners = [mw @ Vector(c) for c in obj.bound_box]
    bmin = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    bmax = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    return bmin, bmax


def build_single_prop_manifest_generator(
    context,
    obj: bpy.types.Object,
    staging_dir: Path | None = None,
    target_mod_override: str | None = None,
    skip_thumbnails: bool = False,
):
    """Generator yielding ProgressStatus(label, percent) during single prop export preparation."""
    staging = staging_dir or get_staging_dir(context)
    try:
        return (yield from _build_single_prop_manifest_generator_impl(
            context,
            obj,
            staging_dir=staging,
            target_mod_override=target_mod_override,
            skip_thumbnails=skip_thumbnails,
        ))
    finally:
        for attempt in range(5):
            leftovers_found = False
            if staging.exists():
                for lingering in staging.glob("*.tmp*"):
                    leftovers_found = True
                    _safe_unlink(lingering, max_retries=8)
                bq = staging / "Pack_Queue"
                if bq.exists():
                    for lingering in bq.glob("*.tmp*"):
                        leftovers_found = True
                        _safe_unlink(lingering, max_retries=8)
            if not leftovers_found:
                break
            time.sleep(0.02 * (attempt + 1))


def _build_single_prop_manifest_generator_impl(
    context,
    obj: bpy.types.Object,
    staging_dir: Path | None = None,
    target_mod_override: str | None = None,
    skip_thumbnails: bool = False,
):
    """Internal implementation for single prop export preparation."""
    last_pct = 0.0

    def _status(label: str, pct: float) -> ProgressStatus:
        nonlocal last_pct
        clamped_pct = max(last_pct, min(100.0, max(0.0, round(float(pct), 2))))
        last_pct = clamped_pct
        return ProgressStatus(label, clamped_pct)

    yield _status(f"Starting export preparation for '{obj.name}'...", 0.0)
    scene = context.scene
    prop = getattr(scene, "reskate_prop", None)
    staging = staging_dir or get_staging_dir(context)
    staging.mkdir(parents=True, exist_ok=True)
    obj_state = get_or_init_object_prop_state(obj) if (obj and obj.type == 'MESH') else {}

    target_mod = get_resolved_target_mod_name(context, target_mod_override=target_mod_override)
    author_name = get_author_name(context) or (str(getattr(prop, "author_name", "") or "").strip() if prop else "") or "ReSkateAuthor"
    slug = get_prop_slug(context, obj, target_mod_override=target_mod, staging_dir=staging)
    lock_stable_prop_key_on_object(obj, slug)
    prop_id = slug[len("own_customprops_"):] if slug.startswith("own_customprops_") else get_canonical_prop_id(context, obj)
    display_name = get_prop_display_name(obj, context)
    default_qd_tab = properties.get_default_qd_tab_name(context) if hasattr(properties, "get_default_qd_tab_name") else (author_name if author_name != "ReSkateAuthor" else "CustomProps")
    custom_qd_tab = str(getattr(prop, "custom_qd_tab", obj_state.get("custom_qd_tab", default_qd_tab)) or default_qd_tab) if prop else str(obj_state.get("custom_qd_tab", default_qd_tab) or default_qd_tab)
    if custom_qd_tab.lower() in ("customprops", "bkcustomprops") and not bool(obj.get("reskate_user_set_qd_tab", False)):
        custom_qd_tab = default_qd_tab
    category = "CUSTOM_TAB" if custom_qd_tab else str(obj_state.get("prop_category") or (prop.prop_category if prop else "Custom Props"))
    try:
        from . import properties as _props
        if custom_qd_tab:
            _props.save_custom_qd_tab(custom_qd_tab)
        raw_mod_nm = (getattr(prop, "target_mod_name", "") if prop else "").strip() or target_mod
        if raw_mod_nm:
            obj["reskate_target_mod_name"] = raw_mod_nm
            obj_state["target_mod_name"] = raw_mod_nm
            save_object_prop_state(obj, obj_state)
    except Exception:
        pass
    raw_mod_icon = str(getattr(prop, "mod_icon_path", "") or "").strip() if prop else ""
    mod_icon_path = str(Path(bpy.path.abspath(raw_mod_icon))) if raw_mod_icon else ""
    raw_mod_readme = str(getattr(prop, "mod_readme_path", "") or "").strip() if prop else ""
    mod_readme_path = str(Path(bpy.path.abspath(raw_mod_readme))) if raw_mod_readme else ""
    family_title = str(
        obj.get("reskate_family_title", "")
        or obj_state.get("prop_family_title", "")
        or (getattr(prop, "prop_family_title", "") if prop else "")
        or ""
    ).strip()
    col_mode = str(obj_state.get("collision_mode") or (prop.collision_mode if prop else "SAME_AS_MESH"))
    no_collision_placement_mode = str(
        getattr(prop, "no_collision_placement_mode", obj_state.get("no_collision_placement_mode", "FLAT_PLANE"))
        if prop
        else obj_state.get("no_collision_placement_mode", "FLAT_PLANE")
    ) or "FLAT_PLANE"
    placement_proxy_mode = str(getattr(prop, "placement_proxy_mode", obj_state.get("placement_proxy_mode", "MATCH_COLLISION")) or "MATCH_COLLISION") if prop else str(obj_state.get("placement_proxy_mode", "MATCH_COLLISION") or "MATCH_COLLISION")
    if getattr(scene, "sk8_ui_mode", "SIMPLE") == "SIMPLE":
        placement_proxy_mode = "MATCH_COLLISION"
    pphys_target_tris = int(getattr(prop, "pphys_target_tris", obj_state.get("pphys_target_tris", 64)) or 64) if prop else int(obj_state.get("pphys_target_tris", 64) or 64)
    ui_mode_cur = str(getattr(scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE").upper()
    compiler_backend = (
        str(getattr(prop, "compiler_backend", "CPU") or "CPU")
        if (prop and ui_mode_cur == "EXPERIMENTAL")
        else "CPU"
    )
    compile_pipeline_mode = get_compile_pipeline_mode(context)
    decal_shader_mode = str(
        getattr(prop, "decal_shader_mode", obj_state.get("decal_shader_mode", "CUTOUT_273")) or "CUTOUT_273"
    ).upper() if prop else str(obj_state.get("decal_shader_mode", "CUTOUT_273") or "CUTOUT_273").upper()
    max_col_tris = (prop.max_col_tris if prop else 5000)
    coacd_max_hulls = (prop.coacd_max_hulls if prop else 8)
    round_rail = (getattr(prop, "round_rail", False) or getattr(prop, "grind_type", "") == 'ROUND_RAIL') if prop else False
    look_mode = (getattr(prop, "look_mode", "AS_IS") if prop else "AS_IS")
    albedo_tint = [float(v) for v in getattr(prop, "albedo_tint", (1.0, 1.0, 1.0))] if prop else [1.0, 1.0, 1.0]
    enable_custom_textures = (bool(getattr(prop, "enable_custom_textures", True)) if prop else True)
    pack_version = (getattr(prop, "pack_version", "0.1.0") if prop else "0.1.0").strip() or "0.1.0"
    pack_description = (getattr(prop, "pack_description", "Standalone Quick Drop Prop Pack") if prop else "Standalone Quick Drop Prop Pack").strip()
    include_2a = (getattr(prop, "include_2a_variant", False) if prop else False)
    export_fbmod_only = (getattr(prop, "export_fbmod_only", False) if prop else False)
    merge_existing_mod = (bool(getattr(prop, "merge_existing_mod", True)) if prop else True)
    auto_disable_old_mod_versions = (bool(getattr(prop, "auto_disable_old_mod_versions", True)) if prop else True)
    scale_mult = (float(getattr(prop, "scale_mult", 1.0)) if prop else 1.0)
    anti_lip_enabled = bool(getattr(prop, "anti_lip_enabled", obj_state.get("anti_lip_enabled", False))) if prop else bool(obj_state.get("anti_lip_enabled", False))
    anti_lip_sink_m = float(getattr(prop, "anti_lip_sink_m", obj_state.get("anti_lip_sink_m", 0.015))) if prop else float(obj_state.get("anti_lip_sink_m", 0.015))
    use_2x2m_detail_shader = bool(
        getattr(prop, "use_2x2m_detail_shader", False) or getattr(prop, "use_native_771_detail_slots", False)
    ) if prop else bool(obj_state.get("use_2x2m_detail_shader", False) or obj_state.get("use_native_771_detail_slots", False))
    detail_2x2m_preset = str(getattr(prop, "detail_2x2m_preset", obj_state.get("detail_2x2m_preset", "CONCRETE_SIDEWALK")) or "CONCRETE_SIDEWALK") if prop else str(obj_state.get("detail_2x2m_preset", "CONCRETE_SIDEWALK"))
    detail_2x2m_intensity = float(getattr(prop, "detail_2x2m_intensity", obj_state.get("detail_2x2m_intensity", 0.35))) if prop else float(obj_state.get("detail_2x2m_intensity", 0.35))
    detail_2x2m_scale = float(getattr(prop, "detail_2x2m_scale", obj_state.get("detail_2x2m_scale", 1.0))) if prop else float(obj_state.get("detail_2x2m_scale", 1.0))
    atlas_res_raw = getattr(prop, "atlas_resolution", obj_state.get("atlas_resolution", "2048")) if prop else obj_state.get("atlas_resolution", "2048")
    try:
        atlas_resolution = int(str(atlas_res_raw or "2048"))
    except ValueError:
        atlas_resolution = 2048

    # Surface resolution (supports both 45 curated presets and the full 279-item Frostbite MaterialGrid catalog)
    use_full = bool(getattr(prop, "use_full_surface_catalog", False)) if prop else False
    if use_full and prop:
        surface_key = getattr(prop, "full_surface_catalog", "material_0096")
        surface_code = get_surface_packed_code(surface_key)
        surface_id = surface_key
    else:
        surface_key = getattr(prop, "primary_surface", "concrete_smooth") if prop else "concrete_smooth"
        surface_code = getattr(prop, "surface_material_code", None)
        if surface_code is None or surface_code == 0:
            surface_code = get_surface_packed_code(surface_key)
        surface_id = getattr(prop, "primary_surface", "Concrete_Solid") if prop else "Concrete_Solid"

    if prop and bool(getattr(prop, "prevent_wall_snap_default", False)):
        surface_code = apply_prevent_wall_snap_code(surface_code, surface_key)

    yield _status(f"Extracting materials & PBR textures for '{obj.name}'...", 10.0)
    # Extract per-material textures & Skate Map PBR parameters first
    material_textures, derived_rough, derived_spec = yield from extract_material_textures_and_pbr_generator(
        obj, slug, staging, start_pct=10.0, end_pct=40.0
    )
    last_pct = max(last_pct, 40.0)
    vis_metals = [float(m.get("metallic", 0.0)) for m in material_textures if not m.get("invisible", False)]
    derived_metal = float(sum(vis_metals) / len(vis_metals)) if vis_metals else 0.0

    roughness = derived_rough if material_textures else (float(getattr(prop, "dielectric_roughness", 0.45)) if prop else 0.45)
    metallic = derived_metal if material_textures else (float(getattr(prop, "dielectric_metallic", 0.0)) if prop else 0.0)
    specular = derived_spec if material_textures else (float(getattr(prop, "dielectric_specular", 0.50)) if prop else 0.50)

    yield _status(f"Exporting visual GLB for '{obj.name}'...", 40.0)
    # Export visual GLB (origin-relative, non-destructive, preserving authored UVs)
    glb_path = staging / f"{slug}.glb"
    yield from export_visual_glb_generator(context, obj, glb_path, pct_val=41.5, end_pct=47.5)
    last_pct = max(last_pct, 47.5)
    yield _status(f"Visual GLB exported for '{obj.name}'", 47.5)

    yield _status(f"Exporting collision GLB for '{obj.name}'...", 55.0)
    # Export custom collision GLB if in CUSTOM_OBJECT / AUTHORED_COL mode, if a generated geometric hull exists in SIMPLE_HULLS,
    # or if <Prop>_COL was thickened by Thicken Thin Collision Faces (Item 3)
    col_thickened = bool(obj.get("reskate_col_thickened", False))
    col_glb_path = None
    if col_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL'):
        c_path = staging / f"{slug}_col.glb"
        col_res = yield from export_col_glb_generator(
            context, obj, c_path, pct_val=56.0, end_pct=59.0
        )
        if col_res and col_res.exists():
            col_glb_path = str(col_res)
        else:
            raise ValueError("Custom collision mode is selected, but no valid Collision Object is picked.")
    elif col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL') or col_thickened:
        col_proxy = resolve_custom_collision_object(context, obj)
        if col_proxy is not None:
            c_path = staging / f"{slug}_col.glb"
            col_res = yield from export_col_glb_generator(
                context, obj, c_path, explicit_col_obj=col_proxy, pct_val=56.0, end_pct=59.0
            )
            if col_res and col_res.exists():
                col_glb_path = str(col_res)
    if col_glb_path:
        last_pct = max(last_pct, 60.0)
        yield _status(f"Collision GLB exported for '{obj.name}'", 60.0)

    yield _status(f"Exporting placement proxy GLB for '{obj.name}'...", 65.0)
    # Export custom/generated placement proxy GLB (_pphys) only when explicitly in CUSTOM_OBJECT or TIGHT_SIMPLIFIED mode;
    # in MATCH_COLLISION mode, compile_blender_prop.py runs automatic 8-hull structural & curved decomposition (_pphys_0..7)
    pphys_glb_path = None
    if placement_proxy_mode in ('CUSTOM_OBJECT', 'TIGHT_SIMPLIFIED'):
        p_proxy = resolve_custom_placement_proxy_object(context, obj)
        if p_proxy is not None:
            p_path = staging / f"{slug}_pphys.glb"
            p_res = export_pphys_glb(context, obj, p_path, explicit_pphys_obj=p_proxy)
            if p_res and p_res.exists():
                pphys_glb_path = str(p_res)

    yield _status(f"Preparing texture atlas for '{obj.name}'...", 75.0)
    tex_path = ensure_texture_atlas(context, obj, slug, staging, pre_extracted_materials=material_textures)
    yield _status(f"Generating thumbnails for '{obj.name}'...", 82.0)
    if skip_thumbnails:
        t256_path = staging / f"{slug}_thumb_256.png"
        t768_path = staging / f"{slug}_thumb_768.png"
    else:
        t256_path, t768_path = ensure_thumbnails(context, obj, slug, staging)
    lights = extract_prop_lights(obj, scale_mult=scale_mult)
    grind_paths = extract_prop_grind_paths(obj, scale_mult=scale_mult)
    material_surfaces = extract_material_surfaces(obj, int(surface_code))
    face_surface_groups = extract_face_surface_overrides(obj, scale_mult=scale_mult)
    experimental_data = extract_prop_experimental_data(obj, scale_mult=scale_mult)
    if col_mode == "NO_COLLISION":
        experimental_data["no_skater_collision"] = True
        experimental_data["no_collision_placement_mode"] = no_collision_placement_mode

    # If single material has an explicit Skate Map surface override and prop uses default surface, inherit it
    if len(material_surfaces) == 1 and material_surfaces[0].get("surface") not in (None, "DEFAULT"):
        surface_code = int(material_surfaces[0]["packed"])
        surface_id = str(material_surfaces[0]["surface"])

    surface_id, surface_code, surface_label = resolve_prop_surface_summary(
        obj, material_surfaces, surface_id, int(surface_code)
    )

    # Auto-promote Round Rail mode when surface is Metal (Round Rail) (2976 / 37227424) or all grind_paths are ROUND_RAIL / COPING
    has_round_rail_paths = bool(grind_paths) and all(
        str(gp.get("type", "")).upper() in ("ROUND_RAIL", "COPING") for gp in grind_paths
    )
    if int(surface_code) in (2976, 37227424) or str(surface_key) == "metal_rail" or has_round_rail_paths:
        round_rail = True
        if has_round_rail_paths and int(surface_code) == 352:
            # Starter template (e.g. Painted Coat) set 352 over a Round Rail prop — restore 2976 Metal_Rail
            surface_code = 2976
            surface_id = "metal_rail"
            for ms in material_surfaces:
                if int(ms.get("packed", 0)) == 352:
                    ms["packed"] = 2976
                    ms["surface"] = "metal_rail"
            surface_id, surface_code, surface_label = resolve_prop_surface_summary(
                obj, material_surfaces, surface_id, int(surface_code)
            )

    try:
        obj["reskate_last_compiled_fp"] = compute_prop_scene_fingerprint(context, obj)
        obj["reskate_last_compiled_mod"] = target_mod
    except Exception:
        pass

    has_custom_pack_icon = bool(mod_icon_path and Path(mod_icon_path).is_file())
    use_pack_icon_as_first_prop = bool(
        has_custom_pack_icon and (getattr(prop, "use_pack_icon_as_first_prop", True) if prop else True)
    )
    if not mod_icon_path and t256_path and t256_path.exists():
        mod_icon_path = str(t256_path)

    manifest_data = {
        "addon_version": ADDON_VERSION,
        "game_dir": str(get_game_dir(context)),
        "prop_id": prop_id,
        "prop_key": slug,
        "display_name": display_name,
        "family_title": family_title,
        "category": category,
        "custom_qd_tab": custom_qd_tab,
        "mod_icon_path": mod_icon_path,
        "has_custom_pack_icon": has_custom_pack_icon,
        "use_pack_icon_as_first_prop": use_pack_icon_as_first_prop,
        "mod_readme_path": mod_readme_path,
        "author_name": author_name,
        "pack_version": pack_version,
        "pack_description": pack_description,
        "coexist_namespace": f"{author_name}/{target_mod}",
        "use_coexist_ids": True,
        "glb_path": str(glb_path),
        "col_glb_path": col_glb_path,
        "col_thickened": col_thickened,
        "pphys_glb_path": pphys_glb_path,
        "texture_path": str(tex_path),
        "material_textures": material_textures,
        "look_mode": look_mode,
        "atlas_resolution": int(atlas_resolution),
        "thumb_256_path": str(t256_path) if t256_path and t256_path.exists() else None,
        "thumb_768_path": str(t768_path) if t768_path and t768_path.exists() else None,
        "collision_mode": col_mode,
        "no_collision_placement_mode": no_collision_placement_mode,
        "placement_proxy_mode": placement_proxy_mode,
        "pphys_target_tris": int(pphys_target_tris),
        "max_col_tris": int(max_col_tris),
        "coacd_max_hulls": int(coacd_max_hulls),
        "anti_lip_enabled": bool(anti_lip_enabled),
        "anti_lip_sink_m": float(anti_lip_sink_m),
        "surface_material_code": int(surface_code),
        "surface_id": surface_id,
        "surface_label": surface_label,
        "material_surfaces": material_surfaces,
        "face_surface_groups": face_surface_groups,
        "grind_paths": grind_paths,
        "round_rail": bool(round_rail),
        "enable_custom_textures": bool(enable_custom_textures),
        "roughness_multiplier": float(roughness),
        "metallic_multiplier": float(metallic),
        "specular_multiplier": float(specular),
        "roughness_map_mix": float(getattr(prop, "roughness_map_mix", 1.0)),
        "metallic_map_mix": float(getattr(prop, "metallic_map_mix", 1.0)),
        "use_native_771_detail_slots": bool(use_2x2m_detail_shader),
        "use_2x2m_detail_shader": bool(use_2x2m_detail_shader),
        "detail_2x2m_preset": detail_2x2m_preset,
        "detail_2x2m_intensity": float(detail_2x2m_intensity),
        "detail_2x2m_scale": float(detail_2x2m_scale),
        "albedo_tint": albedo_tint,
        "lights": lights,
        "experimental": experimental_data,
        "output_staging_dir": str(staging),
        "target_mod_name": target_mod,
        "include_2a_variant": bool(include_2a),
        "export_fbmod_only": bool(export_fbmod_only),
        "merge_existing_mod": bool(merge_existing_mod),
        "auto_disable_old_mod_versions": bool(auto_disable_old_mod_versions),
        "compiler_backend": compiler_backend,
        "compile_pipeline_mode": compile_pipeline_mode,
        "decal_shader_mode": decal_shader_mode,
    }

    yield _status(f"Writing manifest for '{obj.name}'...", 95.0)
    manifest_file = staging / "prop_manifest.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    yield _status(f"Export preparation complete for '{obj.name}'", 100.0)
    return manifest_file, False, display_name


def build_single_prop_manifest(
    context,
    obj: bpy.types.Object,
    staging_dir: Path | None = None,
    target_mod_override: str | None = None,
    skip_thumbnails: bool = False,
) -> tuple[dict, Path]:
    """Extracts prop data from scene and writes prop_manifest.json conforming to v0.22.0 interface contract."""
    gen = build_single_prop_manifest_generator(
        context,
        obj,
        staging_dir=staging_dir,
        target_mod_override=target_mod_override,
        skip_thumbnails=skip_thumbnails,
    )
    manifest_file = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        manifest_file = e.value[0] if isinstance(e.value, (tuple, list)) else e.value
    m_data = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
    return m_data, Path(manifest_file)


def get_marked_scene_props(context) -> list[bpy.types.Object]:
    """Returns all mesh objects in the scene that have `reskate_include_in_pack == True`."""
    marked = []
    for o in context.scene.objects:
        if o.type == 'MESH' and len(o.data.polygons) > 0:
            if getattr(o, "reskate_include_in_pack", False):
                marked.append(o)
    return marked


def build_pack_manifest_generator(
    context,
    staging_dir: Path | None = None,
    selected_objs: list[bpy.types.Object] | None = None,
    target_mod_override: str | None = None,
    merge_existing_override: bool | None = None,
):
    """
    Two-Stage Modal Generator yielding ProgressStatus(label, percent) during multi-prop pack
    export preparation, returning (pack_manifest_path, True, disp_label) upon completion.
    """
    staging = staging_dir or get_staging_dir(context)
    batch_dir = staging / "Pack_Queue"
    try:
        return (yield from _build_pack_manifest_generator_impl(
            context,
            staging_dir=staging,
            selected_objs=selected_objs,
            target_mod_override=target_mod_override,
            merge_existing_override=merge_existing_override,
        ))
    finally:
        for attempt in range(5):
            leftovers_found = False
            if staging.exists():
                for lingering in staging.glob("*.tmp*"):
                    leftovers_found = True
                    _safe_unlink(lingering, max_retries=8)
            if batch_dir.exists():
                for lingering in batch_dir.glob("*.tmp*"):
                    leftovers_found = True
                    _safe_unlink(lingering, max_retries=8)
            if not leftovers_found:
                break
            time.sleep(0.02 * (attempt + 1))


def _build_pack_manifest_generator_impl(
    context,
    staging_dir: Path | None = None,
    selected_objs: list[bpy.types.Object] | None = None,
    target_mod_override: str | None = None,
    merge_existing_override: bool | None = None,
):
    """
    Internal implementation for multi-prop pack export preparation.
    """
    last_pct = 0.0

    def _status(label: str, pct: float) -> ProgressStatus:
        nonlocal last_pct
        clamped_pct = max(last_pct, min(100.0, max(0.0, round(float(pct), 2))))
        last_pct = clamped_pct
        return ProgressStatus(label, clamped_pct)

    yield _status("Initializing pack export preparation...", 0.0)
    scene = context.scene
    prop = getattr(scene, "reskate_prop", None)
    staging = staging_dir or get_staging_dir(context)
    batch_dir = staging / "Pack_Queue"
    batch_dir.mkdir(parents=True, exist_ok=True)

    target_mod = get_resolved_target_mod_name(context, target_mod_override=target_mod_override)
    author_name = get_author_name(context) or (str(getattr(prop, "author_name", "") or "").strip() if prop else "") or "ReSkateAuthor"
    pack_version = (getattr(prop, "pack_version", "0.1.0") if prop else "0.1.0").strip() or "0.1.0"
    pack_description = (getattr(prop, "pack_description", "Standalone Quick Drop Prop Pack") if prop else "Standalone Quick Drop Prop Pack").strip()
    raw_mod_icon = str(getattr(prop, "mod_icon_path", "") or "").strip() if prop else ""
    mod_icon_path = str(Path(bpy.path.abspath(raw_mod_icon))) if raw_mod_icon else ""
    raw_mod_readme = str(getattr(prop, "mod_readme_path", "") or "").strip() if prop else ""
    mod_readme_path = str(Path(bpy.path.abspath(raw_mod_readme))) if raw_mod_readme else ""
    include_2a = (getattr(prop, "include_2a_variant", False) if prop else False)
    export_fbmod_only = (getattr(prop, "export_fbmod_only", False) if prop else False)
    merge_existing_mod = bool(merge_existing_override) if merge_existing_override is not None else (bool(getattr(prop, "merge_existing_mod", True)) if prop else True)
    auto_disable_old_mod_versions = (bool(getattr(prop, "auto_disable_old_mod_versions", True)) if prop else True)
    scale_mult = (float(getattr(prop, "scale_mult", 1.0)) if prop else 1.0)
    look_mode = (getattr(prop, "look_mode", "AS_IS") if prop else "AS_IS")
    default_atlas_res_str = str(getattr(prop, "atlas_resolution", "2048") if prop else "2048")

    queue = getattr(scene, "reskate_pack_queue", [])
    if selected_objs is not None:
        selected_objs = _sort_prop_meshes_by_pack_order(list(selected_objs))
        # Build queue items dynamically for the selected mesh objects
        queue.clear()
        total_sel = len(selected_objs)
        yield _status(f"Configuring pack queue for {total_sel} prop(s)...", 0.2)
        for s_idx, m_obj in enumerate(selected_objs):
            if m_obj is None or m_obj.type != 'MESH':
                continue
            setup_pct = round(0.2 + (s_idx / max(1, total_sel)) * 1.6, 2)
            yield _status(f"Registering pack prop [{s_idx+1}/{total_sel}] '{m_obj.name}'...", setup_pct)
            st = get_or_init_object_prop_state(m_obj)
            slug = get_prop_slug(context, m_obj, target_mod_override=target_mod, staging_dir=staging)
            item = queue.add()
            item.name = m_obj.name
            item.display_name = str(st.get("prop_display_name") or m_obj.name)
            item.prop_key = slug
            item.prop_key_slug = slug
            item.category = str(st.get("prop_category") or (prop.prop_category if prop else "Custom Props"))
            item.collision_mode = str(st.get("collision_mode") or (prop.collision_mode if prop else "SAME_AS_MESH"))
            surf_c = int(st.get("surface_material_code", prop.surface_material_code if prop else 96) or 96)
            item.surface_code = surf_c
            item.surface_name = get_surface_label(str(st.get("primary_surface") or (prop.primary_surface if prop else "concrete_smooth")))
            item.enabled = True
            item.is_enabled = True

    enabled_items = [item for item in queue if getattr(item, "enabled", getattr(item, "is_enabled", True))]

    marked_objs = get_marked_scene_props(context)
    if not enabled_items and marked_objs:
        total_m = len(marked_objs)
        yield _status(f"Scanning {total_m} marked scene props...", 0.5)
        for m_idx, m_obj in enumerate(marked_objs):
            setup_pct = round(0.5 + (m_idx / max(1, total_m)) * 1.3, 2)
            yield _status(f"Registering marked prop [{m_idx+1}/{total_m}] '{m_obj.name}'...", setup_pct)
            slug = get_prop_slug(context, m_obj, target_mod_override=target_mod, staging_dir=staging)
            item = queue.add()
            item.name = m_obj.name
            item.display_name = m_obj.name
            item.prop_key = slug
            item.prop_key_slug = slug
            item.category = prop.prop_category if prop else "Custom Props"
            item.collision_mode = prop.collision_mode if prop else "SAME_AS_MESH"
            item.surface_code = prop.surface_material_code if prop else 96
            item.surface_name = get_surface_label(prop.primary_surface if prop else "concrete_smooth")
            item.enabled = True
            item.is_enabled = True
        enabled_items = [item for item in queue if getattr(item, "enabled", getattr(item, "is_enabled", True))]

    if not enabled_items:
        sel_fallback = get_selected_prop_meshes(context)
        if sel_fallback:
            return (yield from build_pack_manifest_generator(context, staging_dir=staging_dir, selected_objs=sel_fallback, target_mod_override=target_mod_override, merge_existing_override=merge_existing_override))
        raise ValueError("No selected or marked props to compile.")

    props_list = []
    seen_pack_slugs: set[str] = set()
    seen_pack_displays: set[str] = set()
    total_q = len(enabled_items)

    yield _status(f"Found {total_q} prop(s) to prepare...", 2.0)

    for q_idx, item in enumerate(enabled_items):
        prop_base_pct = round(5.0 + (q_idx / total_q) * 88.0, 2)
        delta_pct = round(88.0 / total_q, 2)
        prop_mid_pct = round(prop_base_pct + 0.5 * delta_pct, 2)
        prop_end_pct = round(5.0 + ((q_idx + 1) / total_q) * 88.0, 2)
        yield _status(f"[{q_idx+1}/{total_q}] Processing '{item.name}'...", prop_base_pct)
        obj = scene.objects.get(item.name) or bpy.data.objects.get(item.name)
        if obj is not None and obj.type == 'MESH':
            slug = get_prop_slug(context, obj, target_mod_override=target_mod, staging_dir=staging)
            if "__" in slug:
                cand_norm = re.sub(r"__+", "_", slug)
                try:
                    info_chk = inspect_existing_mod_props(target_mod, staging_dir=staging, use_cache=True)
                    if info_chk.get("exists") and cand_norm in info_chk.get("prop_keys", []):
                        slug = cand_norm
                        lock_stable_prop_key_on_object(obj, slug)
                except Exception:
                    pass
        else:
            raw_stem = _strip_any_pack_tag_prefix(_deduplicate_prop_slug_stem(item.prop_key or item.prop_key_slug or item.name))
            legacy_slug = _find_legacy_unprefixed_slug_in_pack(context, raw_stem, target_mod_override=target_mod)
            if legacy_slug:
                slug = legacy_slug
            else:
                pack_tag = compute_pack_namespace_tag(context, target_mod_override=target_mod)
                slug = clamp_slug(f"own_customprops_{pack_tag}_{raw_stem}", 36)
        prop_id = slug[len("own_customprops_"):] if slug.startswith("own_customprops_") else slug
        item.display_name = clean_prop_display_name(item.display_name or item.name)
        item.prop_key = slug
        item.prop_key_slug = slug
        if slug in seen_pack_slugs or (item.display_name or "").strip().lower() in seen_pack_displays:
            base_pid = re.sub(r"_var\d+$", "", prop_id, flags=re.IGNORECASE)
            base_disp = re.sub(r"\s*\(?Var\s*\d+\)?$", "", (item.display_name or item.name).strip(), flags=re.IGNORECASE).strip()
            v_idx = 2
            while True:
                cand_pid = f"{base_pid}_var{v_idx}"
                cand_slug = clamp_slug(f"own_customprops_{cand_pid}", 36)
                cand_disp = f"{base_disp} Var {v_idx}"
                if cand_slug not in seen_pack_slugs and cand_disp.lower() not in seen_pack_displays:
                    break
                v_idx += 1
            slug = cand_slug
            prop_id = slug[len("own_customprops_"):] if slug.startswith("own_customprops_") else slug
            item.prop_key = slug
            item.prop_key_slug = slug
            item.display_name = cand_disp
            if obj is not None:
                obj["reskate_display_name"] = cand_disp
                obj["reskate_prop_id"] = prop_id
                obj["reskate_key_slug"] = slug
                st_dup = get_or_init_object_prop_state(obj)
                st_dup["prop_display_name"] = cand_disp
                st_dup["prop_id"] = prop_id
                st_dup["prop_key_slug"] = slug
                save_object_prop_state(obj, st_dup)
        seen_pack_slugs.add(slug)
        seen_pack_displays.add((item.display_name or item.name).strip().lower())
        if obj is not None and obj.type == 'MESH':
            lock_stable_prop_key_on_object(obj, slug)
            try:
                obj["reskate_last_compiled_fp"] = compute_prop_scene_fingerprint(context, obj)
                obj["reskate_last_compiled_mod"] = target_mod
            except Exception:
                pass
        obj_state = get_or_init_object_prop_state(obj) if (obj and obj.type == 'MESH') else {}
        item_scale_mult = float(obj_state.get("scale_mult", scale_mult))
        item_look_mode = str(obj_state.get("look_mode", look_mode))
        item_albedo_tint = list(obj_state.get("albedo_tint", [float(v) for v in getattr(prop, "albedo_tint", (1.0, 1.0, 1.0))] if prop else [1.0, 1.0, 1.0]))
        item_enable_tex = bool(obj_state.get("enable_custom_textures", getattr(prop, "enable_custom_textures", True) if prop else True))
        item_round_rail = bool(obj_state.get("round_rail", False) or obj_state.get("grind_type", "") == "ROUND_RAIL")
        item_anti_lip_enabled = bool(obj_state.get("anti_lip_enabled", getattr(prop, "anti_lip_enabled", False) if prop else False))
        item_anti_lip_sink_m = float(obj_state.get("anti_lip_sink_m", getattr(prop, "anti_lip_sink_m", 0.015) if prop else 0.015))
        item_use_2x2m = bool(
            obj_state.get(
                "use_2x2m_detail_shader",
                obj_state.get(
                    "use_native_771_detail_slots",
                    (getattr(prop, "use_2x2m_detail_shader", False) or getattr(prop, "use_native_771_detail_slots", False)) if prop else False,
                ),
            )
        )
        item_detail_preset = str(obj_state.get("detail_2x2m_preset", getattr(prop, "detail_2x2m_preset", "CONCRETE_SIDEWALK") if prop else "CONCRETE_SIDEWALK") or "CONCRETE_SIDEWALK")
        item_detail_intensity = float(obj_state.get("detail_2x2m_intensity", getattr(prop, "detail_2x2m_intensity", 0.35) if prop else 0.35))
        item_detail_scale = float(obj_state.get("detail_2x2m_scale", getattr(prop, "detail_2x2m_scale", 1.0) if prop else 1.0))
        item_atlas_res_raw = obj_state.get("atlas_resolution", default_atlas_res_str)
        try:
            item_atlas_res = int(str(item_atlas_res_raw or "2048"))
        except ValueError:
            item_atlas_res = 2048

        # If the object is live in the scene, ALWAYS re-export fresh geometry, textures, PBR, collision & lights
        # so the user never ships stale queued files!
        material_textures = []
        derived_rough = float(obj_state.get("dielectric_roughness", getattr(prop, "dielectric_roughness", 0.45) if prop else 0.45))
        derived_metal = float(obj_state.get("dielectric_metallic", getattr(prop, "dielectric_metallic", 0.0) if prop else 0.0))
        derived_spec = float(obj_state.get("dielectric_specular", getattr(prop, "dielectric_specular", 0.50) if prop else 0.50))

        if obj and obj.type == 'MESH':
            current_fp = compute_prop_scene_fingerprint(context, obj)
            pbr_cache_file = batch_dir / f"{slug}_pbr_cache.json"
            used_pbr_cache = False
            pipeline_mode = str(getattr(prop, "compile_pipeline_mode", "FAST_OPTIMIZED") or "FAST_OPTIMIZED").upper() if prop else "FAST_OPTIMIZED"
            active_scene_obj = resolve_active_prop_mesh(context) if context else None
            # Always re-extract the currently active object (or all objects in LEGACY_FULL mode) so live texture/shader edits are never skipped!
            allow_pbr_cache = (pipeline_mode != "LEGACY_FULL") and (active_scene_obj is None or obj.name != active_scene_obj.name)
            if allow_pbr_cache and pbr_cache_file.exists():
                try:
                    c_data = json.loads(pbr_cache_file.read_text(encoding="utf-8"))
                    c_glb = Path(c_data.get("glb_path", ""))
                    c_tex = Path(c_data.get("texture_path", ""))
                    c_mats = c_data.get("material_textures", [])
                    all_mat_files_ok = bool(c_mats) and all(
                        Path(m.get("texture_path", "")).exists() and Path(m.get("mask_texture_path", "")).exists()
                        for m in c_mats
                    )
                    if (
                        c_data.get("fingerprint") == current_fp
                        and c_data.get("addon_version") == ADDON_VERSION
                        and c_glb.exists()
                        and c_tex.exists()
                        and all_mat_files_ok
                    ):
                        slug = str(c_data.get("slug") or slug)
                        prop_id = slug[len("own_customprops_"):] if slug.startswith("own_customprops_") else slug
                        item.prop_key = slug
                        item.prop_key_slug = slug
                        item.glb_path = str(c_glb)
                        item.texture_path = str(c_tex)
                        if c_data.get("col_glb_path") and Path(c_data["col_glb_path"]).exists():
                            item.col_glb_path = str(c_data["col_glb_path"])
                        glb_p = c_glb
                        tex_p = c_tex
                        material_textures = c_mats
                        derived_rough = float(c_data.get("derived_rough", derived_rough))
                        derived_metal = float(c_data.get("derived_metal", derived_metal))
                        derived_spec = float(c_data.get("derived_spec", derived_spec))
                        item.snapshot_hash = current_fp
                        used_pbr_cache = True
                except Exception:
                    used_pbr_cache = False

            if used_pbr_cache:
                yield _status(f"[{q_idx+1}/{total_q}] Reusing cached assets for '{item.name}'...", round(prop_base_pct + 0.08 * delta_pct, 2))
            else:
                yield _status(f"[{q_idx+1}/{total_q}] Exporting visual GLB for '{item.name}'...", round(prop_base_pct + 0.15 * delta_pct, 2))
                glb_p = batch_dir / f"{slug}.glb"
                yield from export_visual_glb_generator(
                    context, obj, glb_p,
                    pct_val=round(prop_base_pct + 0.165 * delta_pct, 2),
                    end_pct=round(prop_base_pct + 0.22 * delta_pct, 2),
                    label_prefix=f"[{q_idx+1}/{total_q}] ",
                )
                last_pct = max(last_pct, round(prop_base_pct + 0.22 * delta_pct, 2))
                item.glb_path = str(glb_p)
                yield _status(f"[{q_idx+1}/{total_q}] Visual GLB exported for '{item.name}'", round(prop_base_pct + 0.22 * delta_pct, 2))

                yield _status(f"[{q_idx+1}/{total_q}] Exporting collision GLB for '{item.name}'...", round(prop_base_pct + 0.30 * delta_pct, 2))
                col_obj = resolve_custom_collision_object(context, obj)
                item.has_col_proxy = col_obj is not None
                if item.collision_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL', 'SIMPLE_HULLS', 'CONVEX_HULL') or col_obj is not None:
                    col_out = batch_dir / f"{slug}_col.glb"
                    c_res = yield from export_col_glb_generator(
                        context,
                        obj,
                        col_out,
                        explicit_col_obj=col_obj,
                        pct_val=round(prop_base_pct + 0.31 * delta_pct, 2),
                        label_prefix=f"[{q_idx+1}/{total_q}] ",
                        end_pct=round(prop_base_pct + 0.36 * delta_pct, 2),
                    )
                    if c_res and c_res.exists():
                        item.col_glb_path = str(c_res)
                        last_pct = max(last_pct, round(prop_base_pct + 0.37 * delta_pct, 2))
                        yield _status(f"[{q_idx+1}/{total_q}] Collision GLB exported for '{item.name}'", round(prop_base_pct + 0.37 * delta_pct, 2))

                item.poly_count = len(obj.data.polygons)
                item.tri_count = sum(max(0, len(p.vertices) - 2) for p in obj.data.polygons)
                item.snapshot_hash = current_fp

                yield _status(f"[{q_idx+1}/{total_q}] Extracting materials & PBR textures for '{item.name}'...", round(prop_base_pct + 0.45 * delta_pct, 2))
                material_textures, mat_rough, mat_spec = yield from extract_material_textures_and_pbr_generator(
                    obj, slug, batch_dir, start_pct=round(prop_base_pct + 0.45 * delta_pct, 2), end_pct=round(prop_base_pct + 0.65 * delta_pct, 2)
                )
                last_pct = max(last_pct, round(prop_base_pct + 0.65 * delta_pct, 2))
                yield _status(f"[{q_idx+1}/{total_q}] Materials & PBR extracted for '{item.name}'", round(prop_base_pct + 0.65 * delta_pct, 2))

                yield _status(f"[{q_idx+1}/{total_q}] Building texture atlas for '{item.name}'...", round(prop_base_pct + 0.70 * delta_pct, 2))
                tex_p = ensure_texture_atlas(context, obj, slug, batch_dir, pre_extracted_materials=material_textures)
                last_pct = max(last_pct, round(prop_base_pct + 0.72 * delta_pct, 2))
                yield _status(f"[{q_idx+1}/{total_q}] Texture atlas built for '{item.name}'", round(prop_base_pct + 0.72 * delta_pct, 2))
                item.texture_path = str(tex_p)
                derived_rough = mat_rough
                derived_spec = mat_spec
                vis_metals = [float(m.get("metallic", 0.0)) for m in material_textures if not m.get("invisible", False)]
                if vis_metals:
                    derived_metal = float(sum(vis_metals) / len(vis_metals))
                try:
                    pbr_cache_file.write_text(
                        json.dumps(
                            {
                                "addon_version": ADDON_VERSION,
                                "fingerprint": current_fp,
                                "slug": slug,
                                "glb_path": str(glb_p),
                                "col_glb_path": item.col_glb_path,
                                "texture_path": str(tex_p),
                                "material_textures": material_textures,
                                "derived_rough": derived_rough,
                                "derived_metal": derived_metal,
                                "derived_spec": derived_spec,
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        else:
            glb_p = Path(item.glb_path) if (item.glb_path and Path(item.glb_path).exists()) else (batch_dir / f"{slug}.glb")
            tex_p = Path(item.texture_path) if (item.texture_path and Path(item.texture_path).exists()) else (batch_dir / f"ReSkate_Atlas_{slug}.png")

        item_col_thickened = bool(obj is not None and obj.get("reskate_col_thickened", False))
        col_glb_p = None
        if item.collision_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL', 'SIMPLE_HULLS', 'CONVEX_HULL') or item_col_thickened:
            if item.col_glb_path and Path(item.col_glb_path).exists():
                col_glb_p = str(Path(item.col_glb_path))

        item_pphys_mode = str(obj_state.get("placement_proxy_mode", getattr(prop, "placement_proxy_mode", "MATCH_COLLISION") if prop else "MATCH_COLLISION") or "MATCH_COLLISION")
        item_pphys_tris = int(obj_state.get("pphys_target_tris", getattr(prop, "pphys_target_tris", 64) if prop else 64) or 64)
        pphys_glb_p = None
        if item_pphys_mode in ('CUSTOM_OBJECT', 'TIGHT_SIMPLIFIED') and obj and obj.type == 'MESH':
            yield _status(f"[{q_idx+1}/{total_q}] Exporting placement proxy for '{item.name}'...", round(prop_base_pct + 0.78 * delta_pct, 2))
            p_proxy = resolve_custom_placement_proxy_object(context, obj)
            if p_proxy is not None:
                p_out = batch_dir / f"{slug}_pphys.glb"
                p_res = export_pphys_glb(context, obj, p_out, explicit_pphys_obj=p_proxy)
                if p_res and p_res.exists():
                    pphys_glb_p = str(p_res)

        # Thumbnails (verify existing paths are real rendered thumbnails and respect skip_existing_thumbnails!)
        yield _status(f"[{q_idx+1}/{total_q}] Generating thumbnails for '{item.name}'...", round(prop_base_pct + 0.85 * delta_pct, 2))
        item_skip_thumbs = bool(getattr(prop, "skip_existing_thumbnails", True)) if prop is not None else True
        if obj:
            t256_p, t768_p = ensure_thumbnails(context, obj, slug, batch_dir, force_rerender=(not item_skip_thumbs))
            item.thumb_256_path = str(t256_p)
            item.thumb_768_path = str(t768_p)
        elif (
            item.thumb_256_path
            and item.thumb_768_path
            and _is_valid_rendered_thumbnail(item.thumb_256_path, min_bytes=1200)
            and _is_valid_rendered_thumbnail(item.thumb_768_path, min_bytes=4500)
        ):
            t256_p = Path(item.thumb_256_path)
            t768_p = Path(item.thumb_768_path)
            item_fam_check = str(obj_state.get("prop_family_title", "") or "").strip()
            if item_fam_check:
                apply_variant_corner_notch_to_png_in_place(t768_p)
                apply_variant_corner_notch_to_png_in_place(t256_p)
        else:
            t256_p = batch_dir / f"{slug}_thumb_256.png"
            t768_p = batch_dir / f"{slug}_thumb_768.png"

        # Dynamic lights, grind paths & ReSkate Lab experimental state
        yield _status(f"[{q_idx+1}/{total_q}] Extracting surfaces, lights & grind paths for '{item.name}'...", round(prop_base_pct + 0.92 * delta_pct, 2))
        lights = extract_prop_lights(obj, scale_mult=item_scale_mult) if (obj and obj.type == 'MESH') else []
        grind_paths = extract_prop_grind_paths(obj, scale_mult=item_scale_mult) if (obj and obj.type == 'MESH') else []
        surf_code = int(obj_state.get("surface_material_code") or getattr(item, "surface_code", 96) or 96)
        surf_id = str(obj_state.get("primary_surface") or getattr(item, "surface_name", "Concrete_Solid") or "Concrete_Solid")
        if bool(obj_state.get("prevent_wall_snap_default", False)):
            surf_code = int(apply_prevent_wall_snap_code(surf_code, surf_id))
        has_rr_paths = bool(grind_paths) and all(
            str(gp.get("type", "")).upper() in ("ROUND_RAIL", "COPING") for gp in grind_paths
        )
        if int(surf_code) in (2976, 37227424) or surf_id == "metal_rail" or has_rr_paths:
            item_round_rail = True
            if has_rr_paths and int(surf_code) == 352:
                surf_code = 2976
                surf_id = "metal_rail"
        mat_surfaces = extract_material_surfaces(obj, int(surf_code)) if (obj and obj.type == 'MESH') else []
        if item_round_rail and has_rr_paths:
            for ms in mat_surfaces:
                if int(ms.get("packed", 0)) == 352:
                    ms["packed"] = 2976
                    ms["surface"] = "metal_rail"
        surf_id, surf_code, surf_label = resolve_prop_surface_summary(
            obj, mat_surfaces, surf_id, int(surf_code)
        )
        face_surf_groups = extract_face_surface_overrides(obj, scale_mult=item_scale_mult) if (obj and obj.type == 'MESH') else []
        exp_data = extract_prop_experimental_data(obj, scale_mult=item_scale_mult) if (obj and obj.type == 'MESH') else {}
        item_col_mode = str(item.collision_mode or obj_state.get("collision_mode") or "SAME_AS_MESH")
        item_no_col_placement = str(
            obj_state.get("no_collision_placement_mode", getattr(prop, "no_collision_placement_mode", "FLAT_PLANE") if prop else "FLAT_PLANE")
            or "FLAT_PLANE"
        )
        if item_col_mode == "NO_COLLISION":
            exp_data["no_skater_collision"] = True
            exp_data["no_collision_placement_mode"] = item_no_col_placement

        default_item_qd_tab = properties.get_default_qd_tab_name(context) if hasattr(properties, "get_default_qd_tab_name") else (author_name if author_name != "ReSkateAuthor" else "CustomProps")
        item_custom_qd_tab = str(obj_state.get("custom_qd_tab") or (getattr(prop, "custom_qd_tab", default_item_qd_tab) if prop else default_item_qd_tab) or default_item_qd_tab)
        if item_custom_qd_tab.lower() in ("customprops", "bkcustomprops") and not bool(obj is not None and obj.get("reskate_user_set_qd_tab", False)):
            item_custom_qd_tab = default_item_qd_tab
        item_category = "CUSTOM_TAB" if item_custom_qd_tab else str(obj_state.get("prop_category") or item.category or "Custom Props")
        try:
            from . import properties as _props
            if item_custom_qd_tab:
                _props.save_custom_qd_tab(item_custom_qd_tab)
            if obj is not None and obj.type == 'MESH':
                raw_mod_nm = (getattr(prop, "target_mod_name", "") if prop else "").strip() or target_mod
                if raw_mod_nm:
                    obj["reskate_target_mod_name"] = raw_mod_nm
                    obj_state["target_mod_name"] = raw_mod_nm
                    save_object_prop_state(obj, obj_state)
        except Exception:
            pass
        item_family_title = str(
            (obj.get("reskate_family_title", "") if obj else "")
            or obj_state.get("prop_family_title", "")
            or ""
        ).strip()

        props_list.append({
            "name": item.name,
            "prop_id": prop_id,
            "prop_key": slug,
            "display_name": item.display_name or item.name,
            "family_title": item_family_title,
            "category": item_category,
            "custom_qd_tab": item_custom_qd_tab,
            "glb_path": str(glb_p),
            "col_glb_path": col_glb_p,
            "col_thickened": item_col_thickened,
            "pphys_glb_path": pphys_glb_p,
            "texture_path": str(tex_p),
            "material_textures": material_textures,
            "look_mode": item_look_mode,
            "atlas_resolution": int(item_atlas_res),
            "thumb_256_path": str(t256_p) if t256_p.exists() else None,
            "thumb_768_path": str(t768_p) if t768_p.exists() else None,
            "collision_mode": item_col_mode,
            "no_collision_placement_mode": item_no_col_placement,
            "placement_proxy_mode": item_pphys_mode,
            "pphys_target_tris": int(item_pphys_tris),
            "max_col_tris": int(obj_state.get("max_col_tris", getattr(prop, "max_col_tris", 5000) if prop else 5000) or 5000),
            "anti_lip_enabled": bool(item_anti_lip_enabled),
            "anti_lip_sink_m": float(item_anti_lip_sink_m),
            "surface_material_code": int(surf_code),
            "surface_id": surf_id,
            "surface_label": surf_label,
            "material_surfaces": mat_surfaces,
            "face_surface_groups": face_surf_groups,
            "grind_paths": grind_paths,
            "round_rail": item_round_rail,
            "enable_custom_textures": item_enable_tex,
            "roughness_multiplier": float(derived_rough),
            "metallic_multiplier": float(derived_metal),
            "specular_multiplier": float(derived_spec),
            "roughness_map_mix": float(obj_state.get("roughness_map_mix", getattr(prop, "roughness_map_mix", 1.0))),
            "metallic_map_mix": float(obj_state.get("metallic_map_mix", getattr(prop, "metallic_map_mix", 1.0))),
            "use_native_771_detail_slots": bool(item_use_2x2m),
            "use_2x2m_detail_shader": bool(item_use_2x2m),
            "detail_2x2m_preset": item_detail_preset,
            "detail_2x2m_intensity": float(item_detail_intensity),
            "detail_2x2m_scale": float(item_detail_scale),
            "albedo_tint": item_albedo_tint,
            "lights": lights,
            "experimental": exp_data,
            "decal_shader_mode": str(obj_state.get("decal_shader_mode", getattr(prop, "decal_shader_mode", "CUTOUT_273") if prop else "CUTOUT_273") or "CUTOUT_273").upper(),
            "tris": 0,
        })
        yield _status(f"[{q_idx+1}/{total_q}] Ready: '{item.name}'", prop_end_pct)

    has_custom_pack_icon = bool(mod_icon_path and Path(mod_icon_path).is_file())
    use_pack_icon_as_first_prop = bool(
        has_custom_pack_icon and (getattr(prop, "use_pack_icon_as_first_prop", True) if prop else True)
    )
    if not mod_icon_path and props_list:
        first_thumb = props_list[0].get("thumb_256_path") or props_list[0].get("thumb_768_path")
        if first_thumb and Path(str(first_thumb)).exists():
            mod_icon_path = str(first_thumb)

    ui_mode_pack = str(getattr(scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE").upper()
    compiler_backend = (
        str(getattr(prop, "compiler_backend", "CPU") or "CPU")
        if (prop and ui_mode_pack == "EXPERIMENTAL")
        else "CPU"
    )
    compile_pipeline_mode = get_compile_pipeline_mode(context)
    pack_manifest = {
        "addon_version": ADDON_VERSION,
        "game_dir": str(get_game_dir(context)),
        "pack_name": str(target_mod_override or target_mod),
        "author_name": author_name,
        "pack_version": pack_version,
        "pack_description": pack_description,
        "mod_icon_path": mod_icon_path,
        "has_custom_pack_icon": has_custom_pack_icon,
        "use_pack_icon_as_first_prop": use_pack_icon_as_first_prop,
        "mod_readme_path": mod_readme_path,
        "coexist_namespace": f"{author_name}/{target_mod}",
        "use_coexist_ids": True,
        "props_count": len(props_list),
        "output_staging_dir": str(staging),
        "target_mod_name": target_mod,
        "include_2a_variant": bool(include_2a),
        "export_fbmod_only": bool(export_fbmod_only),
        "merge_existing_mod": bool(merge_existing_mod),
        "auto_disable_old_mod_versions": bool(auto_disable_old_mod_versions),
        "compiler_backend": compiler_backend,
        "compile_pipeline_mode": compile_pipeline_mode,
        "atlas_resolution": int(default_atlas_res_str) if default_atlas_res_str.isdigit() else 2048,
        "props": props_list,
    }

    # Robust global directory barrier: ensure all textures from all props exist and have non-zero size
    all_pack_textures = []
    for p_item in props_list:
        for mt in (p_item.get("material_textures") or []):
            for k in ("texture_path", "mask_texture_path", "normal_texture_path"):
                tp = mt.get(k)
                if tp:
                    all_pack_textures.append(Path(tp))
        if p_item.get("texture_path"):
            all_pack_textures.append(Path(p_item["texture_path"]))

    # Deduplicate paths while preserving discovery order
    seen_tex_paths = set()
    dedup_pack_textures = []
    for tp in all_pack_textures:
        res_key = str(tp.resolve()) if hasattr(tp, "resolve") else str(tp)
        if res_key not in seen_tex_paths:
            seen_tex_paths.add(res_key)
            dedup_pack_textures.append(tp)
    all_pack_textures = dedup_pack_textures
    total_pack_tex = len(all_pack_textures)

    yield _status(f"Verifying all pack textures on disk ({total_pack_tex} texture(s) across {len(props_list)} prop(s))...", 94.0)

    for idx, tex_p in enumerate(all_pack_textures):
        poll_pct = round(94.0 + ((idx + 1) / max(1, total_pack_tex)) * 2.0, 2)
        yield _status(f"Verifying pack texture ({idx+1}/{total_pack_tex}): {tex_p.name}...", poll_pct)

        committed = False
        try:
            if tex_p.exists() and tex_p.is_file() and tex_p.stat().st_size > 0:
                with open(tex_p, "rb") as f:
                    if f.read(1):
                        committed = True
        except (PermissionError, OSError):
            committed = False

        if not committed:
            for _retry in range(30):
                yield _status(
                    f"Waiting for pack texture ({idx+1}/{total_pack_tex}): {tex_p.name} (attempt {_retry+1}/30)...",
                    poll_pct,
                )
                time.sleep(0.025)
                try:
                    if tex_p.exists() and tex_p.is_file() and tex_p.stat().st_size > 0:
                        with open(tex_p, "rb") as f:
                            if f.read(1):
                                committed = True
                                break
                except (PermissionError, OSError):
                    pass

    yield _status(f"Writing pack manifest ({len(props_list)} prop(s))...", 96.0)
    manifest_file = batch_dir / "pack_manifest.json"
    manifest_file.write_text(json.dumps(pack_manifest, indent=2), encoding="utf-8")
    (staging / "pack_manifest.json").write_text(json.dumps(pack_manifest, indent=2), encoding="utf-8")

    yield _status(f"Export preparation complete ({len(props_list)} prop(s))", 100.0)
    return manifest_file, True, f"{target_mod} ({len(props_list)} prop(s))"


def build_pack_manifest(
    context,
    staging_dir: Path | None = None,
    selected_objs: list[bpy.types.Object] | None = None,
    target_mod_override: str | None = None,
    merge_existing_override: bool | None = None,
) -> tuple[dict, Path]:
    """
    Builds pack_manifest.json by running build_pack_manifest_generator to completion.
    """
    gen = build_pack_manifest_generator(
        context,
        staging_dir=staging_dir,
        selected_objs=selected_objs,
        target_mod_override=target_mod_override,
        merge_existing_override=merge_existing_override,
    )
    manifest_file = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        manifest_file = e.value[0] if isinstance(e.value, (tuple, list)) else e.value
    pack_data = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
    return pack_data, Path(manifest_file)


_PROGRESS_SUBSTEP_RE = re.compile(
    r"^PROGRESS\s+(?:(?P<pct>\d+(?:\.\d+)?)\s+)?\[(?P<i>\d+)/(?P<N>\d+)\]\s+(?P<prop>[^—\-]+?)\s*[—\-]\s*(?:(?P<substep>\d+)/5\s+)?(?P<msg>.*)$"
)


def poll_compile_job(line_or_proc=None, prop=None, log_lines: list[str] | None = None) -> dict:
    """
    Parses granular compiler progress lines such as:
      `PROGRESS [i/N] <prop_name> — <substep>/5 <message>`
      `PROGRESS [i/N] <prop_name> — ⚡ Reusing cached prop bundle (unchanged)`
    and computes smooth overall progress `((i - 1 + substep / 5.0) / N)`.
    Updates `prop.compile_progress` (0..100%) and `prop.compile_status_msg` if `prop` is passed.
    """
    res = {
        "matched": False,
        "prop_index": 1,
        "prop_total": 1,
        "substep": 0,
        "progress_fraction": 0.0,
        "progress_pct": 0.0,
        "status_msg": "",
        "step_label": "",
        "is_done": False,
        "has_error": False,
    }
    if not isinstance(line_or_proc, str):
        return res


    clean = line_or_proc.strip()
    if not clean:
        return res

    if log_lines is not None:
        log_lines.append(clean)

    m = _PROGRESS_SUBSTEP_RE.match(clean)
    if m:
        idx = max(1, int(m.group("i")))
        total = max(1, int(m.group("N")))
        prop_name = m.group("prop").strip()
        msg_tail = m.group("msg").strip()
        sub_str = m.group("substep")
        if sub_str is not None:
            substep = max(1, min(5, int(sub_str)))
            status_text = f"[{idx}/{total}] {prop_name} — {substep}/5 {msg_tail}"
        elif "Reusing cached prop bundle" in msg_tail or "⚡" in msg_tail:
            substep = 5
            status_text = f"[{idx}/{total}] {prop_name} — {msg_tail}"
        else:
            substep = 1
            status_text = f"[{idx}/{total}] {prop_name} — {msg_tail}"

        frac = max(0.0, min(1.0, ((idx - 1) + (substep / 5.0)) / float(total)))
        pct = round(frac * 100.0, 1)
        res.update({
            "matched": True,
            "prop_index": idx,
            "prop_total": total,
            "substep": substep,
            "progress_fraction": frac,
            "progress_pct": pct,
            "status_msg": status_text,
            "step_label": f"Prop {idx}/{total} ({substep}/5)",
        })
        if prop is not None:
            try:
                prop.compile_progress = pct
                prop.compile_status_msg = status_text
                prop.compile_step_label = f"Prop {idx}/{total} — Step {substep}/5"
            except Exception:
                pass
        return res


    if clean.startswith("STEP "):
        parts = clean.split(" ", 2)
        if len(parts) >= 3:
            lbl = f"Step {parts[1]} — {parts[2]}"
            res["matched"] = True
            res["step_label"] = lbl
            if prop is not None:
                try:
                    prop.compile_step_label = lbl
                except Exception:
                    pass
    elif clean.startswith("PROGRESS "):
        parts = clean.split(" ", 2)
        if len(parts) >= 2:
            try:
                pct = float(parts[1])
                res["matched"] = True
                res["progress_pct"] = pct
                res["progress_fraction"] = pct / 100.0
                if prop is not None:
                    prop.compile_progress = pct
            except ValueError:
                pass
        if len(parts) >= 3:
            res["status_msg"] = parts[2]
            if prop is not None:
                try:
                    prop.compile_status_msg = parts[2]
                except Exception:
                    pass
    elif clean.startswith("ERROR "):
        err_txt = clean[6:].strip()
        res.update({"matched": True, "has_error": True, "status_msg": err_txt})
        if prop is not None:
            try:
                prop.compile_has_error = True
                prop.compile_status_msg = err_txt
            except Exception:
                pass
    elif clean.startswith("COMPILE_SUMMARY "):
        # Format: COMPILE_SUMMARY mode=FAST_OPTIMIZED mode_label=Fast elapsed=2.14s cache_hits=3/4
        kv = {}
        for tok in clean[len("COMPILE_SUMMARY "):].split():
            if "=" in tok:
                k_s, v_s = tok.split("=", 1)
                kv[k_s.strip()] = v_s.strip()
        el_str = kv.get("elapsed", "")
        m_lbl = kv.get("mode_label") or ("Fast" if kv.get("mode", "FAST_OPTIMIZED") == "FAST_OPTIMIZED" else "Legacy")
        ch_str = kv.get("cache_hits", "0/1")
        summary_msg = f"⏱️ Compiled in {el_str} [Mode: {m_lbl}, Cache Hits: {ch_str}]"
        res.update({
            "matched": True,
            "status_msg": summary_msg,
            "compile_summary": summary_msg,
        })
        if prop is not None:
            try:
                prop.compile_status_msg = summary_msg
                prop["last_compile_summary"] = summary_msg
            except Exception:
                pass
    elif clean.startswith("DONE "):
        prev_summary = str(prop.get("last_compile_summary", "") if prop is not None else "")
        done_txt = f"{prev_summary} → {clean[5:].strip()}" if prev_summary else f"Done → {clean[5:].strip()}"
        res.update({
            "matched": True,
            "is_done": True,
            "progress_fraction": 1.0,
            "progress_pct": 100.0,
            "status_msg": done_txt,
        })
        if prop is not None:
            try:
                prop.compile_progress = 100.0
                prop.compile_status_msg = done_txt
            except Exception:
                pass
    return res



def find_compiler_executable() -> Path:
    """Locates compile_blender_prop.py on disk (supports both external Studio folder and bundled_compiler inside the add-on zip)."""
    studio = get_studio_dir()
    bundled_dir = Path(__file__).resolve().parent / "bundled_compiler"
    candidates = [
        studio / "compile_blender_prop.py",
        bundled_dir / "compile_blender_prop.py",
        STUDIO_DIR / "compile_blender_prop.py",
        Path(__file__).resolve().parent.parent.parent / "compile_blender_prop.py",
        Path.cwd() / "compile_blender_prop.py",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return bundled_dir / "compile_blender_prop.py"


_DEPS_VERIFIED_PY: set[str] = set()


def ensure_python_compiler_dependencies(python_exe: str | None = None) -> tuple[bool, str]:
    r"""
    Ensures Pillow, trimesh, and pygltflib are installed in the target Python interpreter using pip (§1.4 / §9.1).
    Rules:
      - NEVER install or upgrade `numpy` (Blender bundles `numpy` and its `.pyd` files are locked in-process).
      - Install only missing packages (`Pillow`, `trimesh`, `pygltflib`) without `--upgrade`.
      - Install `scipy` separately pinned to `numpy` version (`scipy<1.14` for numpy 1.x, `scipy>=1.13` for numpy 2.x),
        and treat `scipy` install failure as non-fatal because pure-numpy fallbacks exist in `ReSkatePropForge.py` and `compile_blender_prop.py`.
      - Retry with `--user` if Blender is installed in `C:\Program Files\` (permission error).
    """
    py = python_exe or find_python_interpreter()
    env = build_compiler_env()
    if py in _DEPS_VERIFIED_PY:
        return True, "Already verified"

    # Fast <0.001ms in-process check when using Blender's bundled Python (avoids spawning any subprocess!)
    try:
        if Path(py).resolve() == Path(sys.executable).resolve():
            import numpy  # noqa: F401
            import PIL  # noqa: F401
            import trimesh  # noqa: F401
            import pygltflib  # noqa: F401
            _DEPS_VERIFIED_PY.add(py)
            return True, "All Python dependencies present (in-process)"
    except Exception:
        pass

    try:
        chk = subprocess.run(
            [py, "-c", "import numpy, PIL, trimesh, pygltflib; import scipy.spatial; print('OK')"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if chk.returncode == 0:
            _DEPS_VERIFIED_PY.add(py)
            return True, "All Python dependencies present"
    except Exception:
        pass

    # Check which required packages are missing
    missing_core: list[str] = []
    np_major = 1
    try:
        probe = subprocess.run(
            [
                py,
                "-c",
                (
                    "import json\n"
                    "out = {'missing': [], 'np_major': 1, 'scipy_ok': False}\n"
                    "try:\n"
                    "    import numpy as np\n"
                    "    out['np_major'] = int(str(np.__version__).split('.')[0])\n"
                    "except Exception:\n"
                    "    pass\n"
                    "for mod, pkg in [('PIL', 'Pillow'), ('trimesh', 'trimesh'), ('pygltflib', 'pygltflib')]:\n"
                    "    try:\n"
                    "        __import__(mod)\n"
                    "    except Exception:\n"
                    "        out['missing'].append(pkg)\n"
                    "try:\n"
                    "    import scipy.spatial\n"
                    "    out['scipy_ok'] = True\n"
                    "except Exception:\n"
                    "    out['scipy_ok'] = False\n"
                    "print(json.dumps(out))\n"
                ),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            info = json.loads(probe.stdout.strip().splitlines()[-1])
            missing_core = list(info.get("missing", []))
            np_major = int(info.get("np_major", 1))
            scipy_ok = bool(info.get("scipy_ok", False))
        else:
            missing_core = ["Pillow", "trimesh", "pygltflib"]
            scipy_ok = False
    except Exception:
        missing_core = ["Pillow", "trimesh", "pygltflib"]
        scipy_ok = False

    def _pip_install(pkgs: list[str]) -> tuple[bool, str]:
        if not pkgs:
            return True, "Nothing to install"
        try:
            subprocess.run([py, "-m", "ensurepip"], capture_output=True, text=True, env=env, timeout=45)
        except Exception:
            pass
        for use_user in (False, True):
            cmd = [py, "-m", "pip", "install", "--no-warn-script-location"]
            if use_user:
                cmd.append("--user")
            cmd.extend(pkgs)
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
                if res.returncode == 0:
                    return True, (res.stdout or "Installed").strip()
                err_txt = (res.stderr or res.stdout or "").strip()
                if not use_user and ("permission" in err_txt.lower() or "access is denied" in err_txt.lower() or "winerror 5" in err_txt.lower()):
                    continue
            except Exception as ex:
                if not use_user:
                    continue
                return False, str(ex)
        return False, f"Failed to install {', '.join(pkgs)}"

    if missing_core:
        ok_core, msg_core = _pip_install(missing_core)
        if not ok_core:
            return False, msg_core

    if not scipy_ok:
        scipy_spec = "scipy<1.14" if np_major < 2 else "scipy>=1.13"
        # Non-fatal: pure-numpy fallbacks handle missing/broken scipy
        _pip_install([scipy_spec])

    # Final verification of core imports
    try:
        v_chk = subprocess.run(
            [py, "-c", "import numpy, PIL, trimesh, pygltflib"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if v_chk.returncode == 0:
            _DEPS_VERIFIED_PY.add(py)
            return True, "Verified numpy, Pillow, trimesh, pygltflib"
        return False, (v_chk.stderr or v_chk.stdout or "Required Python packages failed to import").strip()
    except Exception as exc:
        return False, str(exc)


def find_python_interpreter() -> str:
    """Finds the best Python interpreter (always prefers Blender's bundled Python `sys.executable`)."""
    blender_py = Path(sys.executable)
    if blender_py.exists() and "python" in blender_py.name.lower():
        return str(blender_py)
    studio = get_studio_dir()
    for base_st in (studio, STUDIO_DIR):
        studio_bl_py = base_st / "Blender 5.3 Beta" / "5.3" / "python" / "bin" / "python.exe"
        if studio_bl_py.exists():
            return str(studio_bl_py)
    for py_name in ("python", "python3"):
        which_py = shutil.which(py_name)
        if which_py:
            return which_py
    return sys.executable


def is_skate_exe_running(use_cache: bool = True, ttl_sec: float = 5.0) -> bool:
    """
    Checks whether Skate.exe is currently running on Windows.
    Uses a fast in-process Win32/lock check and a TTL cache so UI redraws never spawn subprocesses.
    """
    global _SKATE_RUNNING_CACHE
    if sys.platform != "win32":
        return False
    now = time.monotonic()
    cached_ts, cached_val = _SKATE_RUNNING_CACHE
    if use_cache and (now - cached_ts) < ttl_sec:
        return cached_val

    running = False
    try:
        import ctypes
        # Fast <0.02ms Win32 FindWindowW check for Frostbite / Skate window
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("Frostbite", None) or user32.FindWindowW(None, "skate.")
        if hwnd:
            running = True
        elif not use_cache:
            # Only fall back to tasklist when explicitly requested outside UI draw
            res = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Skate.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            running = "skate.exe" in res.stdout.lower()
    except Exception:
        running = False

    _SKATE_RUNNING_CACHE = (now, running)
    return running


def build_compiler_command(manifest_path: Path, pack_manifest: bool = False) -> list[str]:
    """Builds the CLI command list to invoke compile_blender_prop.py."""
    compiler_script = find_compiler_executable()
    python_exe = find_python_interpreter()
    ensure_python_compiler_dependencies(python_exe)
    pipeline_mode = get_compile_pipeline_mode()
    return [
        python_exe,
        str(compiler_script),
        "--pack-manifest" if pack_manifest else "--manifest",
        str(manifest_path),
        "--pipeline-mode",
        pipeline_mode,
    ]


def build_check_collision_command(manifest_path: Path) -> list[str]:
    """Builds the CLI command list to invoke compile_blender_prop.py in --check-collision-only mode (§3 Page 2)."""
    compiler_script = find_compiler_executable()
    python_exe = find_python_interpreter()
    ensure_python_compiler_dependencies(python_exe)
    return [
        python_exe,
        str(compiler_script),
        "--manifest",
        str(manifest_path),
        "--check-collision-only",
    ]


def run_compiler_subprocess(manifest_path: Path, pack_manifest: bool = False) -> tuple[int, str, str]:
    """Synchronously invokes compile_blender_prop.py (used in headless/background mode)."""
    compiler_script = find_compiler_executable()
    if not compiler_script.exists():
        return 1, "", f"Compiler script not found at {compiler_script}"

    cmd = build_compiler_command(manifest_path, pack_manifest=pack_manifest)
    studio = get_studio_dir()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(studio if studio.exists() else compiler_script.parent),
            env=build_compiler_env(),
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as ex:
        return 1, "", str(ex)


def register():
    pass


def unregister():
    pass
