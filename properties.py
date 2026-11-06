"""
ReSkate Prop Studio - properties.py
Property groups, Frostbite surface enums, and Scene/Object registrations
for the 7-Step Checklist UX Redesign:
  Setup → Collision → Grind → Look → Light (in-game) → Thumbnail → Ship
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


# ---------------------------------------------------------------------------
# Persistent User Paths (Game Folder, ReSkate Studio Folder, Staging Folder)
# ---------------------------------------------------------------------------

GITHUB_REPO_URL = "https://github.com/CameronTutt/ReSkate-Prop-Studio"
GITHUB_RELEASES_API_URL = "https://api.github.com/repos/CameronTutt/ReSkate-Prop-Studio/releases/latest"
RESKATE_DISCORD_INVITE_URL = "https://discord.gg/reskate"


def _user_paths_config_file() -> Path:
    local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return local_app / "ReSkate_Prop_Studio" / "user_paths.json"


def _default_staging_dir_str() -> str:
    local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return str(local_app / "ReSkate_Prop_Studio" / "Staging")


_AUTO_DETECTED_PATHS_CACHE: dict[str, object] | None = None
_PERSISTED_USER_PATHS_CACHE: tuple[float, dict[str, object]] = (0.0, {})


def auto_detect_default_paths() -> dict[str, str]:
    """
    Auto-detects (game_dir, studio_dir, staging_dir, author_name) without hardcoding personal paths
    on other users' machines. Cached in memory so UI/slug lookups never repeat filesystem walks.
    """
    global _AUTO_DETECTED_PATHS_CACHE
    if _AUTO_DETECTED_PATHS_CACHE is not None:
        res = dict(_AUTO_DETECTED_PATHS_CACHE)
        res["variant_notch_color"] = list(_AUTO_DETECTED_PATHS_CACHE.get("variant_notch_color", [0.18, 0.80, 0.38]))
        return res

    studio_dir = os.environ.get("RESKATE_STUDIO_DIR", "").strip()
    if not studio_dir:
        cur = Path(__file__).resolve().parent
        for parent in [cur] + list(cur.parents):
            if (parent / "compile_blender_prop.py").exists() or (parent / "reskate_cli.exe").exists():
                studio_dir = str(parent)
                break

    game_dir = os.environ.get("RESKATE_GAME_DIR", "").strip()
    if not game_dir and studio_dir:
        cand_parent = Path(studio_dir).parent
        if (cand_parent / "Data" / "Win32" / "buildkititems.toc").is_file() or (cand_parent / "Data" / "Win32").is_dir():
            game_dir = str(cand_parent)
    if not game_dir:
        for cand in (
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\Skate"),
            Path(r"C:\Program Files\Steam\steamapps\common\Skate"),
            Path(r"C:\Program Files\EA Games\Skate"),
        ):
            if (cand / "Data" / "Win32" / "buildkititems.toc").is_file() or (cand / "Data" / "Win32").is_dir():
                game_dir = str(cand)
                break

    staging_dir = os.environ.get("RESKATE_STAGING_DIR", "").strip() or _default_staging_dir_str()
    author_name = os.environ.get("RESKATE_AUTHOR_NAME", "").strip()

    _AUTO_DETECTED_PATHS_CACHE = {
        "game_dir": game_dir,
        "studio_dir": studio_dir,
        "staging_dir": staging_dir,
        "author_name": author_name,
        "enable_viewport_overlays_master": False,
        "enable_pack_badges_master": False,
        "auto_render_thumbnails_on_compile": False,
        "suppress_thumb_overwrite_warning": False,
        "origin_ground_z_offset": 0.05,
        "variant_notch_color": [0.18, 0.80, 0.38],
    }
    res = dict(_AUTO_DETECTED_PATHS_CACHE)
    res["variant_notch_color"] = list(_AUTO_DETECTED_PATHS_CACHE["variant_notch_color"])
    return res


def load_persisted_user_paths() -> dict:
    global _PERSISTED_USER_PATHS_CACHE
    import time as _time
    now = _time.monotonic()
    if _PERSISTED_USER_PATHS_CACHE[1] and (now - _PERSISTED_USER_PATHS_CACHE[0]) < 2.5:
        cached = dict(_PERSISTED_USER_PATHS_CACHE[1])
        cached["variant_notch_color"] = list(_PERSISTED_USER_PATHS_CACHE[1].get("variant_notch_color", [0.18, 0.80, 0.38]))
        return cached

    out = dict(auto_detect_default_paths())
    cfg = _user_paths_config_file()
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in ("game_dir", "studio_dir", "staging_dir", "author_name"):
                    v = str(data.get(k) or "").strip()
                    if v:
                        out[k] = v
                for bk in (
                    "enable_viewport_overlays_master",
                    "enable_pack_badges_master",
                    "auto_render_thumbnails_on_compile",
                    "suppress_thumb_overwrite_warning",
                ):
                    if bk in data and isinstance(data[bk], bool):
                        out[bk] = bool(data[bk])
                if "origin_ground_z_offset" in data and isinstance(data["origin_ground_z_offset"], (int, float)):
                    out["origin_ground_z_offset"] = float(data["origin_ground_z_offset"])
                if (
                    "variant_notch_color" in data
                    and isinstance(data["variant_notch_color"], (list, tuple))
                    and len(data["variant_notch_color"]) >= 3
                ):
                    out["variant_notch_color"] = [
                        max(0.0, min(1.0, float(data["variant_notch_color"][0]))),
                        max(0.0, min(1.0, float(data["variant_notch_color"][1]))),
                        max(0.0, min(1.0, float(data["variant_notch_color"][2]))),
                    ]
        except Exception:
            pass
    _PERSISTED_USER_PATHS_CACHE = (now, dict(out))
    return out


def save_persisted_user_paths(
    game_dir: str,
    studio_dir: str,
    staging_dir: str,
    author_name: str | None = None,
    enable_viewport_overlays_master: bool | None = None,
    enable_pack_badges_master: bool | None = None,
    origin_ground_z_offset: float | None = None,
    variant_notch_color: tuple | list | None = None,
    auto_render_thumbnails_on_compile: bool | None = None,
    suppress_thumb_overwrite_warning: bool | None = None,
):
    global _PERSISTED_USER_PATHS_CACHE
    if getattr(bpy.app, "background", False) and os.environ.get("RESKATE_ALLOW_BG_SAVE_PATHS", "").strip() not in ("1", "true", "TRUE"):
        return
    cfg = _user_paths_config_file()
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if cfg.exists():
            try:
                existing = json.loads(cfg.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        auth_val = str(author_name if author_name is not None else existing.get("author_name", "")).strip()
        ov_master = (
            bool(enable_viewport_overlays_master)
            if enable_viewport_overlays_master is not None
            else bool(existing.get("enable_viewport_overlays_master", False))
        )
        badge_master = (
            bool(enable_pack_badges_master)
            if enable_pack_badges_master is not None
            else bool(existing.get("enable_pack_badges_master", False))
        )
        auto_thumb = (
            bool(auto_render_thumbnails_on_compile)
            if auto_render_thumbnails_on_compile is not None
            else bool(existing.get("auto_render_thumbnails_on_compile", False))
        )
        suppress_thumb_warn = (
            bool(suppress_thumb_overwrite_warning)
            if suppress_thumb_overwrite_warning is not None
            else bool(existing.get("suppress_thumb_overwrite_warning", False))
        )
        orig_z = (
            float(origin_ground_z_offset)
            if origin_ground_z_offset is not None
            else float(existing.get("origin_ground_z_offset", 0.05))
        )
        if variant_notch_color is not None and len(variant_notch_color) >= 3:
            v_col = [
                round(max(0.0, min(1.0, float(variant_notch_color[0]))), 4),
                round(max(0.0, min(1.0, float(variant_notch_color[1]))), 4),
                round(max(0.0, min(1.0, float(variant_notch_color[2]))), 4),
            ]
        else:
            ex_col = existing.get("variant_notch_color", [0.18, 0.80, 0.38])
            if isinstance(ex_col, (list, tuple)) and len(ex_col) >= 3:
                v_col = [float(ex_col[0]), float(ex_col[1]), float(ex_col[2])]
            else:
                v_col = [0.18, 0.80, 0.38]
        cfg.write_text(
            json.dumps(
                {
                    "game_dir": str(game_dir or "").strip(),
                    "studio_dir": str(studio_dir or "").strip(),
                    "staging_dir": str(staging_dir or "").strip(),
                    "author_name": auth_val,
                    "enable_viewport_overlays_master": ov_master,
                    "enable_pack_badges_master": badge_master,
                    "auto_render_thumbnails_on_compile": auto_thumb,
                    "suppress_thumb_overwrite_warning": suppress_thumb_warn,
                    "origin_ground_z_offset": round(orig_z, 4),
                    "variant_notch_color": v_col,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _PERSISTED_USER_PATHS_CACHE = (0.0, {})
        _OVERLAY_PREFS_MEMO["last_check_ts"] = 0.0
    except Exception:
        pass


def get_addon_preferences(context=None):
    ctx = context if context is not None else bpy.context
    if ctx is None or not hasattr(ctx, "preferences") or ctx.preferences is None:
        return None
    pkg = __package__ or "reskate_prop_studio_addon"
    addon = (
        ctx.preferences.addons.get(pkg)
        or ctx.preferences.addons.get("reskate_prop_studio_addon")
        or ctx.preferences.addons.get("sk8_prop_authoring_addon")
    )
    if addon is None:
        for k, a in ctx.preferences.addons.items():
            if k.endswith(("reskate_prop_studio_addon", "sk8_prop_authoring_addon")):
                addon = a
                break
    return getattr(addon, "preferences", None) if addon else None


_INITIAL_PATHS = load_persisted_user_paths()
_OVERLAY_PREFS_MEMO: dict[str, object] = {
    "mtime_ns": -1,
    "last_check_ts": 0.0,
    "enable_viewport_overlays_master": bool(_INITIAL_PATHS.get("enable_viewport_overlays_master", False)),
    "enable_pack_badges_master": bool(_INITIAL_PATHS.get("enable_pack_badges_master", False)),
    "auto_render_thumbnails_on_compile": bool(_INITIAL_PATHS.get("auto_render_thumbnails_on_compile", False)),
    "suppress_thumb_overwrite_warning": bool(_INITIAL_PATHS.get("suppress_thumb_overwrite_warning", False)),
    "origin_ground_z_offset": float(_INITIAL_PATHS.get("origin_ground_z_offset", 0.05)),
    "variant_notch_color": list(_INITIAL_PATHS.get("variant_notch_color", [0.18, 0.80, 0.38])),
}
_SYNCING_OVERLAY_PREFS = False


def _refresh_overlay_prefs_from_disk() -> None:
    """Keeps in-memory master overlay switches synced with `user_paths.json` (throttled to at most once per 1.5s)."""
    try:
        import time as _time
        now = _time.monotonic()
        if (now - float(_OVERLAY_PREFS_MEMO.get("last_check_ts", 0.0) or 0.0)) < 1.5 and _OVERLAY_PREFS_MEMO.get("mtime_ns", -1) != -1:
            return
        _OVERLAY_PREFS_MEMO["last_check_ts"] = now
        cfg = _user_paths_config_file()
        if cfg.is_file():
            st_ns = cfg.stat().st_mtime_ns
            if st_ns != _OVERLAY_PREFS_MEMO.get("mtime_ns"):
                loaded = load_persisted_user_paths()
                _OVERLAY_PREFS_MEMO["mtime_ns"] = st_ns
                _OVERLAY_PREFS_MEMO["enable_viewport_overlays_master"] = bool(loaded.get("enable_viewport_overlays_master", False))
                _OVERLAY_PREFS_MEMO["enable_pack_badges_master"] = bool(loaded.get("enable_pack_badges_master", False))
                _OVERLAY_PREFS_MEMO["auto_render_thumbnails_on_compile"] = bool(loaded.get("auto_render_thumbnails_on_compile", False))
                _OVERLAY_PREFS_MEMO["suppress_thumb_overwrite_warning"] = bool(loaded.get("suppress_thumb_overwrite_warning", False))
                _OVERLAY_PREFS_MEMO["origin_ground_z_offset"] = float(loaded.get("origin_ground_z_offset", 0.05))
                _OVERLAY_PREFS_MEMO["variant_notch_color"] = list(loaded.get("variant_notch_color", [0.18, 0.80, 0.38]))
    except Exception:
        pass


def is_viewport_overlays_master_enabled(context=None) -> bool:
    """Single source of truth for whether 'Enable All Viewport Overlays (Source Level)' is enabled."""
    prefs = get_addon_preferences(context)
    if prefs is not None and hasattr(prefs, "enable_viewport_overlays_master"):
        try:
            return bool(getattr(prefs, "enable_viewport_overlays_master", True))
        except Exception:
            pass
    return True


def is_pack_badges_master_enabled(context=None) -> bool:
    """Deprecated in v0.31.0: 2D Pack Membership badges have been removed for zero-overhead viewport performance."""
    return False


def is_auto_render_thumbnails_on_compile_enabled(context=None) -> bool:
    """Returns True only if the user explicitly enabled 'Auto-Render Missing Thumbnails on Compile' in Add-on Preferences (default False)."""
    prefs = get_addon_preferences(context)
    if prefs is not None and hasattr(prefs, "auto_render_thumbnails_on_compile"):
        try:
            return bool(getattr(prefs, "auto_render_thumbnails_on_compile", False))
        except Exception:
            pass
    _refresh_overlay_prefs_from_disk()
    return bool(_OVERLAY_PREFS_MEMO.get("auto_render_thumbnails_on_compile", False))


def is_suppress_thumb_overwrite_warning_enabled(context=None) -> bool:
    """Returns True if the user enabled 'Bypass Thumbnail Overwrite Warning (Do Not Ask Again)' in Add-on Preferences."""
    prefs = get_addon_preferences(context)
    if prefs is not None and hasattr(prefs, "suppress_thumb_overwrite_warning"):
        try:
            return bool(getattr(prefs, "suppress_thumb_overwrite_warning", False))
        except Exception:
            pass
    _refresh_overlay_prefs_from_disk()
    return bool(_OVERLAY_PREFS_MEMO.get("suppress_thumb_overwrite_warning", False))


def set_suppress_thumb_overwrite_warning(context, val: bool) -> None:
    """Sets and persists the 'Bypass Thumbnail Overwrite Warning (Do Not Ask Again)' preference."""
    b_val = bool(val)
    _OVERLAY_PREFS_MEMO["suppress_thumb_overwrite_warning"] = b_val
    prefs = get_addon_preferences(context)
    if prefs is not None and hasattr(prefs, "suppress_thumb_overwrite_warning"):
        try:
            prefs.suppress_thumb_overwrite_warning = b_val
            return
        except Exception:
            pass
    loaded = load_persisted_user_paths()
    save_persisted_user_paths(
        loaded.get("game_dir", ""),
        loaded.get("studio_dir", ""),
        loaded.get("staging_dir", ""),
        loaded.get("author_name", ""),
        loaded.get("enable_viewport_overlays_master", False),
        False,
        loaded.get("origin_ground_z_offset", 0.05),
        loaded.get("variant_notch_color", [0.18, 0.80, 0.38]),
        loaded.get("auto_render_thumbnails_on_compile", False),
        b_val,
    )


def get_origin_ground_z_offset(context=None) -> float:
    """Returns the user's configured Origin Ground Z-Offset in meters (default +0.05 m)."""
    prefs = get_addon_preferences(context)
    if prefs is not None and hasattr(prefs, "origin_ground_z_offset"):
        try:
            return float(getattr(prefs, "origin_ground_z_offset", 0.05))
        except Exception:
            pass
    _refresh_overlay_prefs_from_disk()
    return float(_OVERLAY_PREFS_MEMO.get("origin_ground_z_offset", 0.05))


def get_variant_notch_color_rgb(context=None) -> tuple[int, int, int]:
    """Returns the user's configured Variant Thumbnail Notch Color as 8-bit `(R, G, B)` (default green `46, 204, 97`)."""
    prefs = get_addon_preferences(context)
    col_flt = None
    if prefs is not None and hasattr(prefs, "variant_notch_color"):
        try:
            c = getattr(prefs, "variant_notch_color", None)
            if c is not None and len(c) >= 3:
                col_flt = (float(c[0]), float(c[1]), float(c[2]))
        except Exception:
            col_flt = None
    if col_flt is None:
        _refresh_overlay_prefs_from_disk()
        raw_c = _OVERLAY_PREFS_MEMO.get("variant_notch_color", [0.18, 0.80, 0.38])
        if isinstance(raw_c, (list, tuple)) and len(raw_c) >= 3:
            col_flt = (float(raw_c[0]), float(raw_c[1]), float(raw_c[2]))
        else:
            col_flt = (0.18, 0.80, 0.38)
    return (
        int(max(0, min(255, round(col_flt[0] * 255.0)))),
        int(max(0, min(255, round(col_flt[1] * 255.0)))),
        int(max(0, min(255, round(col_flt[2] * 255.0)))),
    )


get_variant_notch_rgb_uint8 = get_variant_notch_color_rgb


def _on_addon_pref_path_changed(self, context):
    orig_z = float(getattr(self, "origin_ground_z_offset", 0.05))
    v_col_attr = getattr(self, "variant_notch_color", (0.18, 0.80, 0.38))
    v_col = [float(v_col_attr[0]), float(v_col_attr[1]), float(v_col_attr[2])] if len(v_col_attr) >= 3 else [0.18, 0.80, 0.38]
    auto_thumb = bool(getattr(self, "auto_render_thumbnails_on_compile", False))
    suppress_thumb_warn = bool(getattr(self, "suppress_thumb_overwrite_warning", False))
    save_persisted_user_paths(
        getattr(self, "game_dir", ""),
        getattr(self, "studio_dir", ""),
        getattr(self, "staging_dir", ""),
        getattr(self, "author_name", ""),
        getattr(self, "enable_viewport_overlays_master", True),
        False,
        orig_z,
        v_col,
        auto_thumb,
        suppress_thumb_warn,
    )
    _OVERLAY_PREFS_MEMO["enable_viewport_overlays_master"] = bool(getattr(self, "enable_viewport_overlays_master", True))
    _OVERLAY_PREFS_MEMO["enable_pack_badges_master"] = False
    _OVERLAY_PREFS_MEMO["auto_render_thumbnails_on_compile"] = auto_thumb
    _OVERLAY_PREFS_MEMO["suppress_thumb_overwrite_warning"] = suppress_thumb_warn
    _OVERLAY_PREFS_MEMO["origin_ground_z_offset"] = orig_z
    _OVERLAY_PREFS_MEMO["variant_notch_color"] = v_col
    try:
        cfg = _user_paths_config_file()
        if cfg.is_file():
            _OVERLAY_PREFS_MEMO["mtime_ns"] = cfg.stat().st_mtime_ns
    except Exception:
        pass
    try:
        if context and getattr(context, "preferences", None):
            context.preferences.is_dirty = True
    except Exception:
        pass
    try:
        invalidate_prop_state_caches()
        def_tab = get_default_qd_tab_name(context)
        for sc in getattr(bpy.data, "scenes", ()):
            pr = getattr(sc, "reskate_prop", None)
            if pr is not None and hasattr(pr, "custom_qd_tab"):
                cur_t = str(getattr(pr, "custom_qd_tab", "") or "").strip()
                if not cur_t or cur_t.lower() in ("customprops", "bkcustomprops"):
                    pr.custom_qd_tab = def_tab
    except Exception:
        pass


def _on_overlay_master_pref_changed(self, context):
    """Unregisters or re-registers SpaceView3D GPU draw handlers at the source level when toggled in Preferences."""
    global _SYNCING_OVERLAY_PREFS
    if _SYNCING_OVERLAY_PREFS:
        return
    _on_addon_pref_path_changed(self, context)
    ov_val = bool(getattr(self, "enable_viewport_overlays_master", True))
    ctx = context if context is not None else bpy.context
    # Sync across all loaded copies of sk8_prop_authoring_addon in preferences.addons
    _SYNCING_OVERLAY_PREFS = True
    try:
        if ctx and getattr(ctx, "preferences", None):
            for k, addon_entry in ctx.preferences.addons.items():
                if k.endswith(("reskate_prop_studio_addon", "sk8_prop_authoring_addon")) and getattr(addon_entry, "preferences", None) is not None:
                    p_inst = addon_entry.preferences
                    if p_inst is not self:
                        try:
                            p_inst.enable_viewport_overlays_master = ov_val
                        except Exception:
                            pass
        if ov_val:
            for sc in getattr(bpy.data, "scenes", ()):
                pr = getattr(sc, "reskate_prop", None)
                if pr is not None and hasattr(pr, "show_viewport_overlays_box"):
                    pr.show_viewport_overlays_box = True
    finally:
        _SYNCING_OVERLAY_PREFS = False

    try:
        from . import gpu_draw
        gpu_draw.sync_draw_handlers(context)
    except Exception:
        pass
    try:
        wm = getattr(ctx, "window_manager", None)
        if wm:
            for win in wm.windows:
                scr = getattr(win, "screen", None)
                if not scr:
                    continue
                for area in scr.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                        for reg in area.regions:
                            reg.tag_redraw()
    except Exception:
        pass


COMPILE_PIPELINE_MODE_ITEMS = [
    (
        'FAST_OPTIMIZED',
        "Fast (Deduplicated & Cached — Recommended)",
        "Skips duplicate 8-hull compile-map calls, caches Havok physics by mesh SHA1, caches unchanged props in packs by content SHA1, and caches base-game templates",
    ),
    (
        'LEGACY_FULL',
        "Legacy (Full Uncached Rebuild — A/B Test)",
        "Runs the unoptimized full rebuild pipeline (no hull deduplication, no Havok/template/prop caching) to compare compile speeds",
    ),
]

THUMB_LIVE_PREVIEW_MODE_ITEMS = [
    (
        'RENDERED',
        "Rendered (EEVEE)",
        "Preview thumbnail lighting and backdrop live in EEVEE Rendered viewport shading",
    ),
    (
        'MATERIAL',
        "Material Preview",
        "Preview thumbnail framing in Material Preview viewport shading",
    ),
]

DECAL_SHADER_MODE_ITEMS = [
    (
        'CUTOUT_273',
        "Standard Decal (Crisp Alpha Cutout — Stickers, Wax, Paint, Stencils)",
        "Method C (0x273 _osk): Crisp alpha cutout with working Roughness & Normal maps (no metallic reflectance)",
    ),
    (
        'METALLIC_771',
        "Metallic Decal / Street Plate (Full PBR — Manholes, Drains, Metal Signs)",
        "Compiles with Full PBR 0x771 shader so Metallic reflectance works on solo decals like storm drains or manhole covers (excluded from skater collision)",
    ),
]


class ReSkateAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or "sk8_prop_authoring_addon"

    author_name: StringProperty(
        name="Author Name",
        default=_INITIAL_PATHS.get("author_name", ""),
        update=_on_addon_pref_path_changed,
        description="Required author name used as the <Author>-<PackName> mod prefix and in all compiled manifest/item author fields",
    )
    game_dir: StringProperty(
        name="Skate Game Folder",
        subtype='DIR_PATH',
        default=_INITIAL_PATHS["game_dir"],
        update=_on_addon_pref_path_changed,
        description="Root Skate installation folder containing Data\\Win32\\buildkititems.toc and Mods\\",
    )
    studio_dir: StringProperty(
        name="ReSkate Studio Folder",
        subtype='DIR_PATH',
        default=_INITIAL_PATHS["studio_dir"],
        update=_on_addon_pref_path_changed,
        description="Optional folder override containing reskate_cli.exe",
    )
    staging_dir: StringProperty(
        name="Staging Folder",
        subtype='DIR_PATH',
        default=_INITIAL_PATHS["staging_dir"],
        update=_on_addon_pref_path_changed,
        description="Working folder (%LOCALAPPDATA%\\ReSkate_Prop_Studio\\Staging by default) used to stage exported GLBs, textures, and .fbmod files",
    )
    thumb_live_preview_mode: EnumProperty(
        name="Live Thumbnail Preview Mode",
        items=THUMB_LIVE_PREVIEW_MODE_ITEMS,
        default='RENDERED',
        description="Viewport shading mode activated automatically when clicking Frame Camera or View Camera",
    )
    auto_render_thumbnails_on_compile: BoolProperty(
        name="Auto-Render Missing Thumbnails on Compile",
        default=False,
        update=_on_addon_pref_path_changed,
        description=(
            "When enabled, automatically launches EEVEE to render thumbnails for props that do not have a rendered or custom thumbnail at compile time. "
            "When disabled (default), uses the instant 'No Thumbnail' placeholder PNG (0 ms compile overhead)"
        ),
    )
    suppress_thumb_overwrite_warning: BoolProperty(
        name="Bypass Thumbnail Overwrite Warning (Do Not Ask Again)",
        default=bool(_INITIAL_PATHS.get("suppress_thumb_overwrite_warning", False)),
        update=_on_addon_pref_path_changed,
        description=(
            "When enabled, clicking 'Render Thumbnail' on a prop that already has a thumbnail immediately backs up "
            "the previous thumbnail to .bak.png and renders without showing the confirmation popup dialog"
        ),
    )
    highlight_surfaces_enabled: BoolProperty(
        name="Highlight Surfaces",
        default=True,
        description="Pulse-highlight faces using the selected shader slot for 2.5 seconds in the 3D Viewport",
    )
    origin_ground_z_offset: FloatProperty(
        name="Origin Height Offset (m)",
        default=float(_INITIAL_PATHS.get("origin_ground_z_offset", 0.05)),
        min=-0.50,
        max=0.50,
        step=1,
        precision=3,
        update=_on_addon_pref_path_changed,
        description=(
            "Raises the origin point slightly above the lowest vertex (+0.05 m default) when clicking "
            "'Origin to Ground Contact (Recommended)' or 'Origin to Bottom Center' so props don't sit on a paper-thin zero plane"
        ),
    )
    variant_notch_color: FloatVectorProperty(
        name="Variant Corner Notch Color",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=tuple(_INITIAL_PATHS.get("variant_notch_color", [0.18, 0.80, 0.38])),
        update=_on_addon_pref_path_changed,
        description="Color of the bottom-left corner notch applied to thumbnails of props that belong to a Variant Family",
    )
    enable_viewport_overlays_master: BoolProperty(
        name="Enable 3D Viewport Overlays (Source Level)",
        default=True,
        update=_on_overlay_master_pref_changed,
        description=(
            "Master source-level switch for ReSkate 3D viewport GPU overlays (Grind Edges, Collision, Placement Bounds). "
            "Note: ReSkate automatically detaches the 3D GPU draw handler whenever all individual overlay toggles are OFF"
        ),
    )
    enable_pack_badges_master: BoolProperty(
        name="Enable Pack Membership Badges (Deprecated)",
        default=False,
        description="Deprecated in v0.31.0: 2D Pack Membership badges have been removed for zero-overhead viewport performance",
    )
    show_developer_prefs: BoolProperty(
        name="Developer",
        default=False,
        description="Show Developer compiler settings",
    )
    default_compile_pipeline_mode: EnumProperty(
        name="Default Compile Speed Mode",
        items=COMPILE_PIPELINE_MODE_ITEMS,
        default='FAST_OPTIMIZED',
        description="Default compilation pipeline mode (Fast Deduplicated & Cached vs. Legacy Full Uncached Rebuild)",
    )
    last_compile_log: StringProperty(
        name="Compiler Log",
        default="",
        description="Full output from the most recent compile or collision check",
    )
    last_compile_error_summary: StringProperty(
        name="Last Compile Error",
        default="",
        description="One-line summary of the most recent compiler failure",
    )
    last_update_status: StringProperty(
        name="Update Status",
        default="",
        description="Result of the most recent Check for Updates query",
    )
    update_check_status: StringProperty(
        name="Update Check Status",
        default="",
        description="Result of the most recent Check for Updates query",
    )
    update_latest_url: StringProperty(
        name="Latest Release URL",
        default="",
        description="URL to the latest GitHub Release when a newer version is available",
    )

    def draw(self, context):
        from . import compiler_bridge
        layout = self.layout

        # 1. Required Author & Paths
        box = layout.box()
        box.label(text="ReSkate Prop Studio — Setup & Paths", icon='PREFERENCES')
        col = box.column(align=True)

        auth_val = str(getattr(self, "author_name", "") or "").strip()
        auth_row = col.row(align=True)
        auth_row.alert = not bool(auth_val)
        auth_row.prop(self, "author_name", text="Author Name (Required)")
        if not auth_val:
            warn_r = col.row(align=True)
            warn_r.alert = True
            warn_r.label(text="❌ Enter your Author Name above before compiling (used for <Author>-<PackName>).", icon='ERROR')

        col.separator(factor=0.5)
        col.prop(self, "game_dir", text="Skate Game Folder")
        g_raw = str(getattr(self, "game_dir", "") or "").strip()
        game_ok = compiler_bridge.is_game_dir_valid(g_raw)
        g_row = col.row(align=True)
        if game_ok:
            g_row.label(text="✅ Skate Game Folder verified (Data\\Win32\\buildkititems.toc found)", icon='CHECKMARK')
        else:
            g_row.alert = True
            g_row.label(text="❌ Invalid Skate Game Folder — select the folder containing Data\\Win32\\buildkititems.toc", icon='ERROR')

        # Read-only ReSkate Studio status
        col.separator(factor=0.4)
        cli_st = compiler_bridge.get_reskate_cli_status(g_raw, context=context)
        cli_ok = bool(cli_st.get("ok"))
        cli_msg = str(cli_st.get("message") or "")
        st_row = col.row(align=True)
        if cli_ok:
            st_row.label(text=f"ReSkate Studio: ✅ {cli_msg}", icon='CHECKMARK')
            if cli_st.get("notice"):
                n_row = col.row(align=True)
                n_row.scale_y = 0.85
                n_row.label(text=str(cli_st["notice"]), icon='INFO')
        else:
            st_row.alert = True
            st_row.label(text=f"ReSkate Studio: ❌ {cli_msg}", icon='ERROR')
            disc_row = col.row(align=True)
            op_disc = disc_row.operator("wm.url_open", text="Open ReSkate Discord", icon='URL')
            op_disc.url = RESKATE_DISCORD_INVITE_URL

        col.separator(factor=0.5)
        stg_row = col.row(align=True)
        stg_row.prop(self, "staging_dir", text="Staging Folder")
        stg_row.operator("reskate.reset_default_paths", text="Reset", icon='LOOP_BACK')

        # 2. Viewport & Thumbnail Preferences
        vp_box = layout.box()
        vp_box.label(text="Viewport & Thumbnail Preview", icon='RESTRICT_VIEW_OFF')
        vp_col = vp_box.column(align=True)
        vp_col.prop(self, "enable_viewport_overlays_master", text="Enable 3D Viewport Overlays (Source Level)")
        vp_col.prop(self, "auto_render_thumbnails_on_compile", text="Auto-Render Missing Thumbnails on Compile")
        vp_col.prop(self, "suppress_thumb_overwrite_warning", text="Bypass Thumbnail Overwrite Warning (Do Not Ask Again)")
        vp_col.separator(factor=0.4)
        vp_col.prop(self, "thumb_live_preview_mode", text="Live Thumbnail Preview")
        vp_col.prop(self, "highlight_surfaces_enabled", text="Highlight Surfaces")
        vp_col.prop(self, "origin_ground_z_offset", text="Origin Height Offset (m)")
        vp_col.prop(self, "variant_notch_color", text="Variant Corner Notch Color")

        # 3. Compiler Log Box
        log_box = layout.box()
        log_hdr = log_box.row(align=True)
        log_hdr.label(text="Compiler Log", icon='CONSOLE')
        log_hdr.operator("reskate.copy_compiler_log", text="Copy Log to Clipboard", icon='COPYDOWN')
        log_txt = str(getattr(self, "last_compile_log", "") or "").strip()
        if log_txt:
            lines = log_txt.splitlines()[-12:]
            l_col = log_box.column(align=True)
            for ln in lines:
                l_col.label(text=ln[:120])
        else:
            log_box.label(text="No compiler log recorded yet in this session.", icon='INFO')

        # 4. Collapsible Developer Section
        dev_box = layout.box()
        dev_row = dev_box.row(align=True)
        dev_row.prop(
            self,
            "show_developer_prefs",
            icon='TRIA_DOWN' if self.show_developer_prefs else 'TRIA_RIGHT',
            emboss=False,
            text="Developer",
        )
        if self.show_developer_prefs:
            dev_col = dev_box.column(align=True)
            dev_col.prop(self, "default_compile_pipeline_mode", text="Compile Pipeline")
            dev_btns = dev_col.row(align=True)
            dev_btns.operator("reskate.clear_compiler_cache", text="Clear Compiler Build Cache", icon='TRASH')
            dev_btns.operator("reskate.install_python_deps", text="Verify / Install Python Libs", icon='PACKAGE')

        # 5. Updates & Credits Section
        upd_box = layout.box()
        upd_box.label(text=f"Updates & Credits (v{compiler_bridge.ADDON_VERSION})", icon='WORLD')
        upd_row = upd_box.row(align=True)
        upd_row.operator("reskate.check_for_updates", text="Check for Updates", icon='FILE_REFRESH')
        op_gh = upd_row.operator("wm.url_open", text="GitHub Repository (ReSkate Prop Studio)", icon='URL')
        op_gh.url = GITHUB_REPO_URL
        status_txt = str(getattr(self, "last_update_status", "") or getattr(self, "update_check_status", "") or "").strip()
        if status_txt:
            upd_box.label(text=status_txt, icon='INFO')
        latest_url = str(getattr(self, "update_latest_url", "") or "").strip()
        if latest_url:
            dl_row = upd_box.row(align=True)
            dl_row.scale_y = 1.2
            op_dl = dl_row.operator("wm.url_open", text="Open Latest Release on GitHub", icon='IMPORT')
            op_dl.url = latest_url
        cred_col = upd_box.column(align=True)
        cred_col.scale_y = 0.85
        cred_col.label(text="Maintained by Crem • Special shout-out to NOBLE for his insights into making props for the game.", icon='HEART')

# ---------------------------------------------------------------------------
# Enums: 5-Page Simple Mode & Advanced Tabs
# ---------------------------------------------------------------------------

SIMPLE_WIZARD_PAGES = [
    ('1', "1. Scale & Origin", "Scale reference models, unit conversion, and origin placement"),
    ('2', "2. Collision", "Collision mode and fast Havok collision check"),
    ('3', "3. Surfaces & Textures", "Per-material riding surface sound, physics feel, and shader texture previews"),
    ('4', "4. Lights (In-Game)", "Optional dynamic Spot/Point lights and glowing emissive faces"),
    ('5', "5. Identity & Thumbnail", "Prop name, Quick Drop tab, and thumbnail camera studio"),
    ('6', "6. Package & Compile", "Pre-flight check, prop pack name, compile diff summary, and one-click deploy"),
]

PROP_CATEGORIES = [
    ('CUSTOM_TAB', "Custom Quick Drop Tab (bk...)", "Enter your own custom Quick Drop folder name (e.g. bkmanny, bkpark)"),
    ('Custom Props', "bkcustomprops — Custom Props", "Appears in Quick Drop > bkcustomprops"),
    ('BK_RAILS', "bkrails — Rails & Flatbars", "Appears in Quick Drop > bkrails"),
    ('BK_LEDGES', "bkledges — Ledges & Curbs", "Appears in Quick Drop > bkledges"),
    ('BK_RAMPS', "bkramps — Ramps & Banks", "Appears in Quick Drop > bkramps"),
    ('BK_PADS', "bkpads — Manual Pads", "Appears in Quick Drop > bkpads"),
    ('BK_FUNBOXES', "bkfunboxes — Funboxes", "Appears in Quick Drop > bkfunboxes"),
    ('BK_QUARTERPIPES', "bkquarterpipes — Quarterpipes & Vert", "Appears in Quick Drop > bkquarterpipes"),
    ('BK_LIGHTS', "bklights — Lights & Lamps", "Appears in Quick Drop > bklights"),
    ('BK_AUDIO', "bkaudio — Audio & Speakers", "Appears in Quick Drop > bkaudio"),
    ('BK_CONSTRUCTION', "bkpropsconstruction — Construction", "Appears in Quick Drop > bkpropsconstruction"),
    ('BK_URBAN', "bkpropsurban — Street & Urban", "Appears in Quick Drop > bkpropsurban"),
    ('bkcustom', "bkprops — BuildKit Props", "Appears in Quick Drop > bkprops"),
    ('Dynamic Props', "bkinteractive — Interactive / Dynamic", "Appears in Quick Drop > bkinteractive"),
]

ORIGIN_MODES = [
    ('BOTTOM_CENTER', "Bottom Center (Recommended — Flush on Ground)", "Moves the object's origin to the bottom center of its mesh while keeping the object in place in the scene"),
    ('CENTER_OF_MASS', "Center of Gravity / Volume (Optional)", "Moves the object's origin to its center of gravity/mass while keeping the object in place in the scene"),
    ('GEOMETRY_CENTER', "Geometry Center (Bounding Box)", "Moves the object's origin to the center of its bounding box while keeping the object in place in the scene"),
    ('GROUND_CONTACT', "Ground Contact (Lowest Touching Vertices)", "Moves the object's origin to the XY centroid of its lowest ground-touching vertices at min Z"),
]

# Legacy kept only for programmatic compatibility if old scripts read it; hidden from UI
ORIENTATION_PRESETS = [
    ('Y_UP', "Z-Up (Blender Default)", "Standard Blender Z-up coordinate system (converted automatically on export)"),
    ('Z_UP', "Z-Up (Frostbite Engine)", "Frostbite engine alignment"),
    ('ROT_90X', "Rotate 90° X", "Compensate for imported axis offset"),
]

COLLISION_MODES = [
    ('SAME_AS_MESH', "Same as visible mesh (Recommended)", "Skater rides directly on the visible mesh (recommended for meshes under 2,500 triangles)"),
    ('SIMPLE_HULLS', "Simple Hulls", "Generates a clean, geometric/boxy hull collision mesh that preserves flat skateable surfaces"),
    ('CUSTOM_OBJECT', "Custom Collision object", "Pick any mesh object in the scene to act as the physical collision"),
    ('NO_COLLISION', "No Collision", "Skater passes right through the prop (with a selectable Quick Drop placement proxy for grabbing and moving)"),
    # Legacy aliases kept for backwards-compatible script calls:
    ('EXACT_MESH', "Same as visible mesh (Recommended)", "Alias for Same as visible mesh"),
    ('EXACT_1_1', "Same as visible mesh (Recommended)", "Alias for Same as visible mesh"),
    ('AUTHORED_COL', "Custom Collision object", "Alias for Custom collision object"),
    ('CONVEX_HULL', "Simple Hulls", "Alias for Simple hulls"),
]

COLLISION_MODE_UI_ITEMS = [
    ('SAME_AS_MESH', "Same as visible mesh (Recommended)", "Skater rides directly on the visible mesh (recommended for meshes under 2,500 triangles)"),
    ('SIMPLE_HULLS', "Simple Hulls", "Generates a clean, geometric/boxy hull collision mesh right in the scene that preserves flat skateable boxes and ramps"),
    ('CUSTOM_OBJECT', "Custom Collision object", "Pick any mesh object in the scene to act as the physical collision"),
    ('NO_COLLISION', "No Collision", "Skater passes right through the prop (with a selectable Quick Drop placement proxy for grabbing and moving)"),
]

NO_COLLISION_PLACEMENT_ITEMS = [
    (
        'FLAT_PLANE',
        "Flat Ground Plane (Floor Decal / Rug)",
        "Uses a flat 7mm grabbable floor wafer so skaters roll right over it and Quick Drop grabs it flush on the ground",
    ),
    (
        'MATCH_MESH',
        "Match 3D Mesh Shape (Visual-Only 3D Prop / Foliage)",
        "Disables skater collision while compiling the Quick Drop Placement Proxy (_pphys) from the 3D mesh silhouette so you can grab and place the prop anywhere on its 3D shape",
    ),
]

PRIMARY_SURFACE_PRESETS = [
    # Concrete, Stone, Brick & Asphalt
    ('concrete_smooth', "Concrete (Smooth / Plaza)", "Smooth plaza slabs, ledges, and polished concrete"),
    ('concrete_rough', "Concrete (Rough / Street)", "Rough textured street sidewalk concrete"),
    ('marble_tile', "Marble / Tile (Waxed)", "Waxed plaza marble and smooth tile"),
    ('brick', "Brick (Pavers / Masonry)", "Brick pavers and masonry"),
    ('asphalt', "Asphalt (Street)", "Street road asphalt"),
    ('roofing_asphalt', "Roofing Asphalt (Shingles)", "Gritty asphalt roof shingles and tar paper"),
    ('bedrock', "Bedrock / Natural Stone", "Natural rock boulders, cliffs, and rough stone"),
    # Wood & Bark
    ('wood_ramp', "Wood (Ramp / Plywood)", "Skatepark plywood ramps and banks"),
    ('wood_thick', "Wood (Solid Timber)", "Solid timber benches and picnic tables"),
    ('wood_thin_smooth', "Wood (Thin Smooth / Masonite)", "Thin smooth plywood, masonite skatelite, and indoor ramps"),
    ('wood_thin_rough', "Wood (Thin Rough / Weathered)", "Weathered thin wood, pallets, and old boards"),
    ('bark', "Bark / Tree Trunk", "Natural rough tree bark and logs (no auto-grind edges)"),
    # Metals & Chainlink
    ('metal_grindable', "Metal (Solid / Grindable)", "Solid metal rails, flat bars, and coping"),
    ('metal_rail', "Metal (Round Rail)", "Dedicated cylindrical metal rails"),
    ('metal_thin', "Metal (Thin Sheet)", "Sheet metal, signs, dumpsters, and vending machines"),
    ('metal_grate', "Metal (Grate / Catwalk)", "Rattling metal grating, catwalks, and drain covers"),
    ('metal_diamond_plate', "Metal (Diamond Plate)", "Textured steel diamond tread plate"),
    ('metal_skirt', "Metal (Skirt / Flashing)", "Metal ramp transition threshold and flashing skirt"),
    ('chainlink_fence', "Chainlink Fence", "Flexible rattling chainlink wire mesh fencing"),
    # Plastic, Rubber, Glass & Synthetics
    ('plastic_solid', "Plastic (Solid / Barrier / Cone)", "Hard molded plastic barriers, cones, bins, and jersey walls"),
    ('plastic_smooth', "Plastic (Smooth / Fiberglass)", "Smooth slick plastic, acrylic, and fiberglass shells"),
    ('plastic_heavy', "Plastic (Heavy / Utility)", "Heavy-duty industrial plastic crates and dumpsters"),
    ('rubber', "Rubber (Tires / Bumpers)", "High-grip rubber tires, mats, and dock bumpers"),
    ('glass', "Glass (Windows / Panels)", "Smooth glass windows, bus stops, and skylights"),
    # Cardboard, Cloth & Soft Goods
    ('cardboard', "Cardboard (Boxes)", "Corrugated cardboard boxes and packaging"),
    ('cloth', "Cloth / Tarp / Banner", "Fabric awnings, tents, banners, and tarps"),
    ('canvas', "Canvas (Heavy Fabric)", "Heavy canvas fabric and awnings"),
    ('leather', "Leather / Cushioned Vinyl", "Padded leather/vinyl couches, seats, and gym mats"),
    # Terrain, Grass & Water
    ('grass', "Grass / Turf (Slowdown)", "Grass and turf surfaces"),
    ('earth_dirt', "Earth / Packed Dirt", "Packed soil, dirt trails, and planters"),
    ('sand_gravel', "Dirt / Sand / Gravel", "Unpaved dirt, sand, and gravel"),
    ('gravel', "Gravel (Loose Stones)", "Loose crunching gravel and ballast"),
    ('water', "Water (Puddle / Pool Hazard)", "Water surface with splash and hazard physics"),
    # Special Gameplay Behaviours
    ('jump_pad', "Jump Pad (Bounce / Launch)", "Bounce / jump-pad physics behaviour"),
    ('jump_pad_rubber', "Jump Pad (Rubber Bounce)", "Rubber surface with jump-pad bounce behaviour"),
    ('jump_pad_plastic', "Jump Pad (Plastic Bounce)", "Plastic surface with jump-pad bounce behaviour"),
    ('stairs_concrete', "Stairs (Concrete Stair Behaviour)", "Concrete surface with native Frostbite stair behaviour"),
    ('stairs_metal', "Stairs (Metal Stair Behaviour)", "Metal surface with native Frostbite stair behaviour"),
    ('stairs_marble', "Stairs (Marble Stair Behaviour)", "Marble surface with native Frostbite stair behaviour"),
    ('wipeout_hazard', "Wipeout (Force Bail on Contact)", "Forces the skater to bail/wipeout on contact"),
    ('no_grind_plastic', "No Grinding (Plastic Skate-Stopper)", "Plastic surface that explicitly blocks truck grinds"),
    ('no_grind_cloth', "No Grinding (Cloth / Soft)", "Soft cloth surface that explicitly blocks truck grinds"),
    ('edge_excluded', "Edge-Excluded (No Auto-Grind)", "Excludes geometry from automatic edge/grind alignment"),
    ('camera_occluder', "Camera Occluder", "Marks geometry as a camera occluder"),
    ('default_generic', "Default (Generic Material)", "Frostbite Slot 0 default collision material"),
]

MATERIAL_SLOT_SURFACE_ITEMS = [
    ('DEFAULT', "Use Default Surface", "Inherit the prop's default surface material"),
] + list(PRIMARY_SURFACE_PRESETS)

PRIMARY_SURFACE_CODE_MAP = {
    'concrete_smooth': 96,
    'concrete_rough': 3168,
    'marble_tile': 480,
    'brick': 2016,
    'asphalt': 160,
    'roofing_asphalt': 2272,
    'bedrock': 6944,
    'wood_ramp': 3680,
    'wood_thick': 4000,
    'wood_thin_smooth': 4320,
    'wood_thin_rough': 416,
    'bark': 4256,
    'metal_grindable': 352,
    'metal_rail': 2976,
    'metal_thin': 1696,
    'metal_grate': 1632,
    'metal_diamond_plate': 7072,
    'metal_skirt': 4640,
    'chainlink_fence': 5024,
    'plastic_solid': 3296,
    'plastic_smooth': 3360,
    'plastic_heavy': 4064,
    'rubber': 4128,
    'glass': 2144,
    'cardboard': 5280,
    'cloth': 3552,
    'canvas': 2784,
    'leather': 2592,
    'grass': 608,
    'earth_dirt': 3488,
    'sand_gravel': 4896,
    'gravel': 4960,
    'water': 15730592,
    'jump_pad': 6496,
    'jump_pad_rubber': 45092896,
    'jump_pad_plastic': 45092832,
    'stairs_concrete': 47186720,
    'stairs_metal': 47187616,
    'stairs_marble': 47191520,
    'wipeout_hazard': 54003616,
    'no_grind_plastic': 54529248,
    'no_grind_cloth': 54529568,
    'edge_excluded': 4512,
    'camera_occluder': 57147424,
    'default_generic': 32,
}

GRIND_GEOMETRY_TYPES = [
    ('LEDGE', "Hard Edge / Ledge", "Sharp edge or box corner for ledge grinds (Orange guide + 3D box preview)"),
    ('ROUND_RAIL', "Round Rail", "Cylindrical tube rail with custom radius (Cyan guide + 3D cylinder preview)"),
    ('COPING', "Pool / Ramp Coping", "Transition/pool coping lip (Magenta guide + 3D cylinder preview)"),
]

GRIND_SURFACE_OVERRIDES = [
    ('Metal_Solid_Grindable', "Metal (Solid / Flatbar)", "Fast, smooth metal grind sound and feel"),
    ('Metal_Rail_Grindable', "Metal (Round Rail)", "Dedicated cylindrical metal rail grind sound and feel"),
    ('Metal_Hollow_Grindable', "Metal (Thin / Hollow)", "Hollow sheet-metal / dumpster edge grind sound"),
    ('Concrete_Grindable', "Concrete (Ledge)", "Crunchy concrete ledge grind sound and feel"),
    ('Marble_Grindable', "Marble / Tile (Waxed)", "Fast waxed plaza marble ledge grind sound and feel"),
    ('Brick_Grindable', "Brick (Coping)", "Chunky brick coping grind sound and feel"),
    ('Wood_Grindable', "Wood (Coping / Timber)", "Wooden coping friction and sound"),
    ('Plastic_Grindable', "Plastic (Barrier / Curb)", "Slick molded plastic barrier grind sound and feel"),
    ('Chainlink_Grindable', "Chainlink Fence Top", "Rattling chainlink fence top rail grind sound"),
    ('Rubber_Grindable', "Rubber (High Friction)", "High-friction rubber edge"),
    ('Glass_Grindable', "Glass Edge", "Smooth glass edge grind sound"),
    ('No_Grind', "Not Grindable", "Prevents trucks from locking onto this edge"),
]

GRIND_SURFACE_CODE_MAP = {
    'Metal_Solid_Grindable': 352,
    'Metal_Rail_Grindable': 2976,
    'Metal_Hollow_Grindable': 1696,
    'Concrete_Grindable': 2592,
    'Marble_Grindable': 480,
    'Brick_Grindable': 2016,
    'Wood_Grindable': 416,
    'Plastic_Grindable': 3296,
    'Chainlink_Grindable': 5024,
    'Rubber_Grindable': 4128,
    'Glass_Grindable': 2144,
    'No_Grind': 5920,
}

LOOK_MODES = [
    ('AS_IS', "Use my texture as-is", "Passes your model's single texture and 0..1 UVs straight through without altering anything"),
    ('BAKE', "Bake for me", "Bakes multiple materials or tiled UVs into a new UV map and new texture image, leaving your originals untouched"),
]

LOOK_PRESET_ITEMS = [
    ('MATTE', "Matte", "Roughness 0.85, Metallic 0.00 (concrete, wood, unpolished surfaces)"),
    ('SATIN', "Satin", "Roughness 0.45, Metallic 0.00 (painted wood, plastic, coated metal)"),
    ('GLOSSY', "Glossy", "Roughness 0.15, Metallic 0.00 (waxed marble, glass, wet/polished surfaces)"),
    ('METAL', "Metal", "Roughness 0.28, Metallic 1.00 (bare steel, aluminum, chrome)"),
    ('CUSTOM', "Custom", "Custom Roughness and Metallic sliders or texture maps"),
]

LOOK_PRESETS = {
    "MATTE": {"roughness": 0.85, "metallic": 0.00, "specular": 0.45},
    "SATIN": {"roughness": 0.45, "metallic": 0.00, "specular": 0.50},
    "GLOSSY": {"roughness": 0.15, "metallic": 0.00, "specular": 0.65},
    "METAL": {"roughness": 0.28, "metallic": 1.00, "specular": 0.90},
}

SURFACE_FEEL_PRESETS = [
    ('NORMAL', "Normal", "Standard skateable rolling friction and pop"),
    ('GRIPPY', "Grippy", "High-traction surface feel (rough concrete / rubber / asphalt)"),
    ('SLICK', "Slick", "Fast, low-friction surface feel (waxed marble / smooth tile / metal)"),
    ('BOUNCY_OFF', "Bouncy-Off (Safe)", "Explicitly strips any jump-pad / bounce behavior and prevents wall-snap"),
]

SURFACE_CATEGORY_FILTERS = [
    ('ALL', "All Categories", "Show all surface presets and Frostbite surfaces"),
    ('CONCRETE', "Concrete / Stone / Brick", "Plaza slabs, rough concrete, marble, brick, asphalt"),
    ('WOOD', "Wood / Ramps / Bark", "Plywood ramps, solid timber, masonite, bark"),
    ('METAL', "Metal / Rails / Grates", "Solid metal, round rails, sheet metal, grates, diamond plate"),
    ('FENCING', "Fencing / Do-Not-Align", "Chainlink fence and do-not-align vertical wall surfaces"),
    ('PLASTIC', "Plastic / Rubber / Glass", "Barriers, smooth plastic, rubber, glass, cardboard, cloth"),
    ('NATURAL', "Grass / Dirt / Sand / Water", "Grass, packed earth, sand, gravel, water"),
    ('SPECIAL', "Special Gameplay (Stairs / No-Grind / Bouncy)", "Stairs, no-grinding stoppers, edge-excluded, jump pads"),
]

SURFACE_BEHAVIOR_FILTERS = [
    ('ALL', "All Behaviors (279 Surfaces)", "Show all Frostbite MaterialGrid declarations"),
    ('STANDARD', "Standard Skateable (Grindable)", "Standard skateable surfaces that allow edge generation and grinding"),
    ('DO_NOT_ALIGN', "Do-Not-Align (No Wall-Snap)", "Surfaces with Do-Not-Align behavior that prevent sideways wall-ride snapping"),
    ('NO_GRIND', "No-Grind / Edge-Excluded", "Surfaces that block truck grinding or automatic edge generation"),
    ('STAIRS', "Stair Behavior", "Surfaces with native Frostbite stair behavior"),
    ('BOUNCY', "Jump Pad / Bouncy / Hazard", "Jump-pad bounce, wipeout, and water hazard surfaces"),
]

STARTER_MATERIAL_TEMPLATES = {
    "CONCRETE": {
        "label": "Concrete",
        "base_color": (0.62, 0.61, 0.59, 1.0),
        "roughness": 0.84,
        "metallic": 0.00,
        "specular": 0.45,
        "surface": "concrete_smooth",
        "detail_preset": "CONCRETE_SIDEWALK",
        "noise_scale": 35.0,
        "bump_strength": 0.30,
    },
    "WOOD": {
        "label": "Wood",
        "base_color": (0.68, 0.50, 0.32, 1.0),
        "roughness": 0.58,
        "metallic": 0.00,
        "specular": 0.48,
        "surface": "wood_ramp",
        "detail_preset": "SKATEPARK_TRIM",
        "noise_scale": 24.0,
        "bump_strength": 0.22,
    },
    "METAL": {
        "label": "Metal",
        "base_color": (0.76, 0.78, 0.80, 1.0),
        "roughness": 0.26,
        "metallic": 1.00,
        "specular": 0.90,
        "surface": "metal_grindable",
        "detail_preset": "METAL_BRASS",
        "noise_scale": 50.0,
        "bump_strength": 0.15,
    },
    "ASPHALT": {
        "label": "Asphalt",
        "base_color": (0.22, 0.22, 0.24, 1.0),
        "roughness": 0.92,
        "metallic": 0.00,
        "specular": 0.38,
        "surface": "asphalt",
        "detail_preset": "CONCRETE_SIDEWALK",
        "noise_scale": 65.0,
        "bump_strength": 0.45,
    },
    "MARBLE": {
        "label": "Marble",
        "base_color": (0.88, 0.87, 0.85, 1.0),
        "roughness": 0.16,
        "metallic": 0.00,
        "specular": 0.68,
        "surface": "marble_tile",
        "detail_preset": "SYNTHETIC_PAINT",
        "noise_scale": 12.0,
        "bump_strength": 0.08,
    },
    "PAINTED": {
        "label": "Painted",
        "base_color": (0.22, 0.50, 0.82, 1.0),
        "roughness": 0.42,
        "metallic": 0.00,
        "specular": 0.52,
        "surface": "metal_grindable",
        "detail_preset": "SYNTHETIC_PAINT",
        "noise_scale": 80.0,
        "bump_strength": 0.12,
    },
}

# ---------------------------------------------------------------------------
# Blender.org Texture Archive Catalog & Enums (Milestone 4 - R4)
# ---------------------------------------------------------------------------

BLENDER_ORG_CATEGORIES = [
    ('CONCRETE', "Concrete", "Cast concrete walls and paved ground slabs", 'MOD_BUILD', 0),
    ('METAL', "Metal", "Galvanized plates and aged industrial steel", 'SHADING_RENDERED', 1),
    ('WOOD', "Wood", "Skate ramp plywood and timber grain", 'TEXTURE', 2),
    ('MARBLE', "Marble", "Polished plaza marble and stone tiles", 'NODE_MATERIAL', 3),
    ('BRICK', "Brick", "Fired red brick masonry and mortar", 'MOD_SOLIDIFY', 4),
    ('GRUNGE', "Grunge", "Scraped street surfaces and damp ground", 'FORCE_TURBULENCE', 5),
    ('DECAL', "Decal", "Industrial graphic stencils and warning decals", 'IMAGE_PLANE', 6),
]

BLENDER_ORG_CONCRETE_VARIANTS = [
    ('concrete_wall_12', "Concrete Wall 12 (Smooth Plaza)", "Smooth cast concrete wall/slab with fine aggregate (-color, -normal, -roughness, -specular)"),
    ('concrete_ground_02', "Concrete Ground 02 (Rough Slab)", "Rough poured concrete ground with pitted surface bump (-color, -bump, -specular)"),
]

BLENDER_ORG_METAL_VARIANTS = [
    ('metal_plate_05', "Metal Plate 05 (Clean Galvanized)", "Clean galvanized steel plate with normal-map seams (-color, -normal)"),
    ('metal_rusty_20', "Metal Rusty 20 (Aged Industrial)", "Weathered rusty metal plate with pitted roughness (-color, -normal, -roughness)"),
]

BLENDER_ORG_WOOD_VARIANTS = [
    ('wood_simple_01', "Wood Simple 01 (Clean Plywood)", "Natural skate ramp plywood with warm wood grain (-color)"),
]

BLENDER_ORG_MARBLE_VARIANTS = [
    ('marble_solid_08', "Marble Solid 08 (Plaza Marble)", "Polished architectural marble with veining (-color, -normal, -roughness, -specular)"),
]

BLENDER_ORG_BRICK_VARIANTS = [
    ('brick_wall_01', "Brick Wall 01 (Classic Red Brick)", "Traditional fired red brick masonry with recessed mortar lines (-color, -normal, -glossiness)"),
]

BLENDER_ORG_GRUNGE_VARIANTS = [
    ('grunge_scratches_01', "Grunge Scratches 01 (Scraped Street)", "Heavily scraped and scratched urban ground surface (-color)"),
    ('grunge_wet_01', "Grunge Wet 01 (Street Slick)", "Dark damp street ground with wet sheen (-color)"),
]

BLENDER_ORG_DECAL_VARIANTS = [
    ('decal_warning_robot', "Decal Warning Robot (Industrial Stencil)", "Industrial warning graphic stencil (-color)"),
]

BLENDER_ORG_CATALOG = {
    "CONCRETE": {
        "label": "Concrete",
        "icon": 'MOD_BUILD',
        "default": "concrete_wall_12",
        "default_variant": "concrete_wall_12",
        "variants": {
            "concrete_wall_12": {
                "name": "Concrete Wall 12 (Smooth Plaza)",
                "path": "concrete/concrete_wall_12",
                "slug": "concrete_wall_12",
                "color_ext": "png",
                "maps": {
                    "color": "concrete_wall_12-color.png",
                    "normal": "concrete_wall_12-normal.png",
                    "roughness": "concrete_wall_12-roughness.png",
                    "specular": "concrete_wall_12-specular.png",
                    "displacement": "concrete_wall_12-displacement.png",
                },
                "maps_list": ["color.png", "normal.png", "roughness.png", "specular.png", "displacement.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "concrete_smooth",
                "detail_preset": "CONCRETE_SIDEWALK",
                "description": "Smooth cast concrete wall/plaza slab with fine aggregate",
            },
            "concrete_ground_02": {
                "name": "Concrete Ground 02 (Rough Slab)",
                "path": "concrete/concrete_ground_02",
                "slug": "concrete_ground_02",
                "color_ext": "png",
                "maps": {
                    "color": "concrete_ground_02-color.png",
                    "normal": "concrete_ground_02-bump.png",
                    "bump": "concrete_ground_02-bump.png",
                    "specular": "concrete_ground_02-specular.png",
                },
                "maps_list": ["color.png", "bump.png", "specular.png"],
                "is_normal_bump": True,
                "is_glossiness": False,
                "surface": "concrete_smooth",
                "detail_preset": "CONCRETE_SIDEWALK",
                "description": "Rough poured concrete ground with pitted surface bump",
            },
        },
    },
    "METAL": {
        "label": "Metal",
        "icon": 'SHADING_RENDERED',
        "default": "metal_plate_05",
        "default_variant": "metal_plate_05",
        "variants": {
            "metal_plate_05": {
                "name": "Metal Plate 05 (Clean Galvanized)",
                "path": "metal/metal_plate_05",
                "slug": "metal_plate_05",
                "color_ext": "png",
                "maps": {
                    "color": "metal_plate_05-color.png",
                    "normal": "metal_plate_05-normal.png",
                    "displacement": "metal_plate_05-displacement.png",
                },
                "maps_list": ["color.png", "normal.png", "displacement.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "metal_grindable",
                "detail_preset": "METAL_BRASS",
                "description": "Clean industrial metal plate with normal-map seams",
            },
            "metal_rusty_20": {
                "name": "Metal Rusty 20 (Aged Industrial)",
                "path": "metal/metal_rusty_20",
                "slug": "metal_rusty_20",
                "color_ext": "png",
                "maps": {
                    "color": "metal_rusty_20-color.png",
                    "normal": "metal_rusty_20-normal.png",
                    "roughness": "metal_rusty_20-roughness.png",
                    "displacement": "metal_rusty_20-displacement.png",
                },
                "maps_list": ["color.png", "normal.png", "roughness.png", "displacement.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "metal_grindable",
                "detail_preset": "METAL_BRASS",
                "description": "Weathered rusty metal plate with pitted roughness",
            },
        },
    },
    "WOOD": {
        "label": "Wood",
        "icon": 'TEXTURE',
        "default": "wood_simple_01",
        "default_variant": "wood_simple_01",
        "variants": {
            "wood_simple_01": {
                "name": "Wood Simple 01 (Clean Plywood)",
                "path": "wood/wood_patterned/wood_simple_01",
                "slug": "wood_simple_01",
                "color_ext": "jpg",
                "maps": {
                    "color": "wood_simple_01-color.jpg",
                },
                "maps_list": ["color.jpg"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "wood_ramp",
                "detail_preset": "SYNTHETIC_PAINT",
                "description": "Natural skate ramp plywood with warm wood grain",
            },
        },
    },
    "MARBLE": {
        "label": "Marble",
        "icon": 'NODE_MATERIAL',
        "default": "marble_solid_08",
        "default_variant": "marble_solid_08",
        "variants": {
            "marble_solid_08": {
                "name": "Marble Solid 08 (Plaza Marble)",
                "path": "marble/marble_solid_08",
                "slug": "marble_solid_08",
                "color_ext": "png",
                "maps": {
                    "color": "marble_solid_08-color.png",
                    "normal": "marble_solid_08-normal.png",
                    "roughness": "marble_solid_08-roughness.png",
                    "specular": "marble_solid_08-specular.png",
                },
                "maps_list": ["color.png", "normal.png", "roughness.png", "specular.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "marble_tile",
                "detail_preset": "SYNTHETIC_PAINT",
                "description": "Polished architectural marble with veining and smooth gloss",
            },
        },
    },
    "BRICK": {
        "label": "Brick",
        "icon": 'MOD_SOLIDIFY',
        "default": "brick_wall_01",
        "default_variant": "brick_wall_01",
        "variants": {
            "brick_wall_01": {
                "name": "Brick Wall 01 (Classic Red Brick)",
                "path": "bricks/brick_wall_01",
                "slug": "brick_wall_01",
                "color_ext": "png",
                "maps": {
                    "color": "brick_wall_01-color.png",
                    "normal": "brick_wall_01-normal.png",
                    "roughness": "brick_wall_01-glossiness.png",
                    "glossiness": "brick_wall_01-glossiness.png",
                    "displacement": "brick_wall_01-displacement.png",
                },
                "maps_list": ["color.png", "normal.png", "glossiness.png", "displacement.png"],
                "is_normal_bump": False,
                "is_glossiness": True,
                "surface": "concrete_rough",
                "detail_preset": "CONCRETE_SIDEWALK",
                "description": "Traditional fired red brick masonry with recessed mortar lines",
            },
        },
    },
    "GRUNGE": {
        "label": "Grunge",
        "icon": 'FORCE_TURBULENCE',
        "default": "grunge_scratches_01",
        "default_variant": "grunge_scratches_01",
        "variants": {
            "grunge_scratches_01": {
                "name": "Grunge Scratches 01 (Scraped Street)",
                "path": "grunge/grunge_scratches_01",
                "slug": "grunge_scratches_01",
                "color_ext": "png",
                "maps": {
                    "color": "grunge_scratches_01-color.png",
                },
                "maps_list": ["color.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "asphalt",
                "detail_preset": "CONCRETE_SIDEWALK",
                "description": "Heavily scraped and scratched urban ground surface",
            },
            "grunge_wet_01": {
                "name": "Grunge Wet 01 (Street Slick)",
                "path": "grunge/grunge_wet_01",
                "slug": "grunge_wet_01",
                "color_ext": "png",
                "maps": {
                    "color": "grunge_wet_01-color.png",
                },
                "maps_list": ["color.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "concrete_smooth",
                "detail_preset": "CONCRETE_SIDEWALK",
                "description": "Dark damp street ground with wet sheen",
            },
        },
    },
    "DECAL": {
        "label": "Decal",
        "icon": 'IMAGE_PLANE',
        "default": "decal_warning_robot",
        "default_variant": "decal_warning_robot",
        "variants": {
            "decal_warning_robot": {
                "name": "Decal Warning Robot (Industrial Stencil)",
                "path": "decals/decal_warning_robot",
                "slug": "decal_warning_robot",
                "color_ext": "png",
                "maps": {
                    "color": "decal_warning_robot-color.png",
                },
                "maps_list": ["color.png"],
                "is_normal_bump": False,
                "is_glossiness": False,
                "surface": "metal_grindable",
                "detail_preset": "SYNTHETIC_PAINT",
                "description": "Industrial warning graphic stencil with transparent perimeter",
            },
        },
    },
}

BLENDER_ORG_TEXTURE_CATALOG = BLENDER_ORG_CATALOG

ATLAS_RESOLUTIONS = [
    ('1024', "1024 × 1024 (1K Standard)", "Fast 1K native texture compilation"),
    ('2048', "2048 × 2048 (2K High-Res Supersampled)", "2K bake supersampled for crisp normal and roughness edges"),
    ('4096', "4096 × 4096 (4K Ultra Supersampled)", "4K bake supersampled with Lanczos anti-aliasing"),
    ('8192', "8192 × 8192 (8K Extreme Supersampled)", "8K ultra-crisp supersampled bake for hero props and packs"),
]

DETAIL_2X2M_PRESET_ITEMS = [
    ('CONCRETE_SIDEWALK', "Concrete Sidewalk (2x2m)", "Native concrete_sidewalk_02_2x2m micro-pores"),
    ('SYNTHETIC_PAINT', "Synthetic Paint (2x2m)", "Native synthetic_paint_01_2x2m coated surface grain"),
    ('METAL_BRASS', "Brushed Metal (2x2m)", "Native metal_brass_01_2x2m metallic micro-grain"),
    ('SKATEPARK_TRIM', "Skatepark Trim", "Native t_skatepark_trim_01 surface detail"),
]

COL_STRAIGHTEN_MODE_ITEMS = [
    ('LINE', "Straight Line (Endpoint to Endpoint)", "Snap selected edge chain/loop onto a clean straight line between its two endpoints"),
    ('ARC', "Smooth Arc (Curved Rail / Ledge)", "Fit a smooth circular/quadratic arc between endpoints with adjustable curvature"),
    ('PLANE_Z', "Flatten Horizontal (Z)", "Snap selected vertices to a flat horizontal Z height"),
    ('PLANE_X', "Flatten X Axis", "Align selected vertices along X"),
    ('PLANE_Y', "Flatten Y Axis", "Align selected vertices along Y"),
    ('PLANE_FIT', "Flatten to Best-Fit Plane", "Project selected vertices onto their mathematical best-fit plane"),
]

THUMB_LIGHTING_MODES = [
    ('OUTDOOR_SUN', "Outdoor Sky & Sun Disk", "Uses a temporary Multiple-Scattering Nishita Sky & Sun Disk"),
    ('STUDIO_3PT', "Auto-Scaled 3-Point Studio Rig", "Automatically spawns a temporary Key, Fill & Rim studio light rig scaled to the prop's dimensions"),
    ('SKY_SUN', "Outdoor Sky & Sun Disk", "Uses a temporary Multiple-Scattering Nishita Sky & Sun Disk"),
    ('SCENE_AS_IS', "Use Scene Lighting As-Is", "Renders using only existing lights in the scene"),
]

THUMB_BG_STYLES = [
    ('SUNNY_DAY', "Sunny Day", "Outdoor sunny sky with ground shadow"),
    ('STUDIO_LIGHTING', "Studio Lighting", "Soft 3-point studio lighting with floor"),
    ('TRANSPARENT', "Transparent", "Clean cutout icon with alpha transparency"),
    ('FLAT_COLOR', "Flat Background Color", "Solid flat background color"),
    ('STUDIO_DARK', "Dark", "Neutral dark studio backdrop"),
    ('STUDIO_WARM', "Warm", "Warm studio backdrop"),
]

LIGHT_TYPES = [
    ('SPOT', "Spot Light", "Directional cone light (reads cone angle, radius, color, intensity, and shadows directly from the Blender Spot light)"),
    ('POINT', "Point Light", "Omnidirectional fill light (reads radius, color, and intensity directly from the Blender Point light)"),
]

# ---------------------------------------------------------------------------
# Native Surface Lookup Helpers (Internal)
# ---------------------------------------------------------------------------

NATIVE_COLLISION_MATERIAL_ROWS = (
    (0, "Default", ""),
    (1, "Concrete", ""),
    (2, "Asphalt", ""),
    (3, "Earth", ""),
    (4, "Glass", ""),
    (5, "Metal", ""),
    (6, "Wood Thin Rough", ""),
    (7, "Marble", ""),
    (8, "Plastic", ""),
    (9, "Grass", "no edge generation; do not align"),
    (0, "Default", ""),
    (0, "Default", ""),
    (10, "Concrete", ""),
    (11, "Asphalt", ""),
    (12, "Native material", ""),
    (13, "Native material", ""),
    (14, "Native material", ""),
    (0, "Default", ""),
    (15, "Footwear Hi-Tops", ""),
    (16, "Footwear Slip-Ons", ""),
    (17, "Footwear Skate Shoes", ""),
    (18, "Footwear Boots", ""),
    (0, "Default", ""),
    (19, "Footwear Loafers", ""),
    (0, "Default", ""),
    (20, "Metal Grate", ""),
    (21, "Metal Thin", ""),
    (0, "Default", ""),
    (0, "Default", ""),
    (0, "Default", ""),
    (22, "Water", ""),
    (23, "Brick", ""),
    (24, "Metal", ""),
    (25, "Glass", ""),
    (26, "Glass", ""),
    (27, "Roofing Asphalt", ""),
    (28, "Clothing", ""),
    (29, "Clothing Cotton Light", ""),
    (30, "Clothing Cotton Heavy", ""),
    (31, "Clothing Denim", ""),
    (32, "Clothing Leather", ""),
    (33, "Clothing Nylon", ""),
    (34, "Clothing Bare Skin", ""),
    (35, "Clothing Canvas", ""),
    (36, "Clothing Corduroy", ""),
    (0, "Default", ""),
    (37, "Metal Rail", ""),
    (38, "Metal", "no edge generation"),
    (39, "Asphalt", ""),
    (40, "Concrete Rough", ""),
    (41, "Marble", ""),
    (42, "Plastic", ""),
    (43, "Plastic", ""),
    (44, "Grass", ""),
    (45, "Earth", ""),
    (46, "Cloth", ""),
    (47, "Cloth", ""),
    (48, "Wood Thick Rough", ""),
    (49, "Wood Thin Rough", ""),
    (50, "Wood Thin Rough", ""),
    (51, "Metal", ""),
    (52, "Metal", ""),
    (53, "Wood Thick Smooth", ""),
    (54, "Plastic", ""),
    (55, "Rubber", ""),
    (56, "Grass", "no edge generation; do not align"),
    (57, "Bark", "no edge generation; do not align"),
    (58, "Wood Thin Smooth", ""),
    (59, "Footwear Flip-Flops", ""),
    (0, "Default", ""),
    (60, "Edge-excluded surface", "no edge generation"),
    (61, "Surface-analysis surface", "include in surface analysis"),
    (62, "Metal Skirt", ""),
    (63, "Native material", ""),
    (64, "Native material", ""),
    (65, "Clothing Plastic Thin", ""),
    (66, "Sand", "do not align"),
    (67, "Gravel", ""),
    (68, "Chainlink Fence", ""),
    (69, "Native material", ""),
    (70, "Native material", ""),
    (0, "Default", ""),
    (71, "Cardboard", ""),
    (72, "Clothing Cardboard", ""),
    (73, "Earth", ""),
    (74, "Cloth", ""),
    (75, "Jump Pad", "jump-pad and wipeout behaviour"),
    (76, "Marble", ""),
    (77, "Footwear Slippers", ""),
    (78, "Native material", ""),
    (79, "Stairs", "stair behaviour"),
    (80, "Clothing Rubber", ""),
    (0, "Default", ""),
    (81, "Native material", ""),
    (82, "Native material", ""),
    (83, "Native material", ""),
    (84, "Native material", ""),
    (85, "Native material", ""),
    (86, "Native material", ""),
    (87, "Native material", ""),
    (88, "Native material", ""),
    (89, "Jump Pad", "no edge generation; jump-pad and wipeout behaviour"),
    (90, "Jump Pad", "no edge generation; jump-pad and wipeout behaviour"),
    (91, "Wipeout", "no edge generation; wipeout behaviour"),
    (92, "No Grinding", "exclude from grinding"),
    (0, "Default", ""),
    (93, "Clothing Metal Thin", ""),
    (94, "Clothing Bone", ""),
    (95, "Bedrock", ""),
    (96, "Camera Occluder", "can occlude the camera"),
    (97, "Metal Diamond Plate", ""),
    (98, "Do-not-align surface", "do not align"),
    (0, "Default", ""),
    (99, "Native material", ""),
    (100, "Native material", ""),
    (101, "Clothing Nylon Inflated", ""),
)

NATIVE_COLLISION_NETWORK_PACKED = (
    0, 64, 128, 320, 384, 448, 576, 768, 832, 896, 960, 1024,
    1152, 1216, 1280, 1344, 1472, 1600, 1664, 1984, 2048, 2112, 2176, 2240,
    2304, 2368, 2432, 2496, 2560, 2624, 2688, 2752, 2816, 2944, 3072, 3136,
    3200, 3264, 3328, 3392, 3456, 3520, 3584, 3648, 3712, 3776, 3840, 3904,
    3968, 4032, 4096, 4160, 4224, 4288, 4352, 4480, 4608, 4800, 4864, 4928,
    4992, 5120, 5248, 5312, 5376, 5440, 5568, 5632, 5824, 6464, 6784, 6848,
    6912, 7040, 7360, 524352, 525056, 527424, 529856, 1048704, 1573056,
    2099328, 2621760, 2623488, 2625280, 2625344, 3149376, 3670464, 4194816,
    4197568, 4197632, 6291520, 6292224, 6293504, 6294592, 6297024, 6816576,
    13108800, 13631488, 13633152, 13635392, 15730560, 16253376, 16253696,
    16254912, 16256064, 16258496, 16778880, 16779264, 17303616, 17827968,
    18352320, 22547136, 24120192, 24643136, 24643584, 24643904, 24645056,
    25168896, 25690176, 25693248, 26217600, 26741952, 27266304, 27267776,
    27790656, 28315008, 28839872, 29362880, 29363712, 29886464, 29888064,
    29888128, 30412416, 30412480, 30936768, 31459328, 31460224, 31461120,
    31985472, 32509824, 33033088, 33034176, 33558528, 34607232, 35130560,
    35131136, 35131584, 36700480, 36700928, 36701824, 36702144, 36702208,
    36702272, 36702336, 36703104, 36703296, 36703424, 36703488, 36703552,
    36703616, 36703680, 36703744, 36703808, 36703872, 36703936, 36704064,
    36704128, 36704192, 36704256, 36704384, 36704448, 36705024, 36705088,
    36705408, 36705472, 36705728, 36706944, 36707072, 37224448, 37225216,
    37226112, 37226496, 37227392, 37228096, 37228992, 37753344, 38273792,
    38275008, 38276160, 38276224, 38278592, 38798080, 38800960, 38801280,
    38802880, 39326400, 40375104, 40899456, 41418752, 41419520, 41420736,
    41421888, 41421952, 41422016, 41424320, 41943104, 41943552, 41943808,
    41945024, 41946176, 41946304, 41948608, 42996864, 44569920, 45091520,
    45092352, 45092800, 45092864, 45094592, 45615040, 45618624, 46662400,
    46663296, 46663616, 46664768, 46664832, 46665536, 46667200, 47186688,
    47187584, 47189056, 47191488, 48759552, 48760832, 48761984, 48762624,
    48764352, 48765696, 49283136, 49283840, 49286208, 49286272, 49288640,
    49808128, 49810496, 49812928, 50332416, 50334784, 50337216, 50856704,
    50859072, 50861504, 51380992, 51383360, 51385792, 51905280, 51908160,
    51908480, 52432448, 52432512, 52432768, 52957184, 53481408, 54003584,
    54528704, 54529216, 54529280, 54529536, 54529984, 54530112, 54531392,
    57147392, 58197632, 58198144, 58199616, 58200192, 59248192, 59772480,
)

NATIVE_COLLISION_MATERIAL_FLAG = 0x20

def _build_native_surface_items():
    items = []
    for network_id, network_packed in enumerate(NATIVE_COLLISION_NETWORK_PACKED):
        material_slot = (network_packed >> 6) & 0x1FFF
        property_slot = (network_packed >> 19) & 0x1FFF
        _row, material_label, material_behaviour = NATIVE_COLLISION_MATERIAL_ROWS[material_slot]
        if property_slot:
            _p_row, property_label, _p_beh = NATIVE_COLLISION_MATERIAL_ROWS[property_slot]
            label = f"{material_label} + {property_label} behavior"
        else:
            label = material_label
        packed = network_packed | NATIVE_COLLISION_MATERIAL_FLAG
        badges = []
        beh_combined = f"{material_behaviour} {label}".lower()
        if "no grinding" in beh_combined or "exclude from grinding" in beh_combined or "no edge" in beh_combined:
            badges.append("[no grind]")
        if "do not align" in beh_combined or "do-not-align" in beh_combined:
            badges.append("[do not align]")
        if "stair" in beh_combined:
            badges.append("[stair]")
        if "jump pad" in beh_combined or "jump-pad" in beh_combined or "wipeout" in beh_combined:
            badges.append("[bouncy]")
        badge_str = (" " + " ".join(badges)) if badges else ""
        desc = f"Surface {network_id:03d}: {label}"
        if material_behaviour:
            desc += f" ({material_behaviour})"
        items.append((f"material_{packed:04d}", f"{network_id:03d} - {label}{badge_str}", desc, 0, packed))
    return items

FROSTBITE_101_SURFACES = _build_native_surface_items()
NATIVE_COLLISION_MATERIAL_PACKED = {item[0]: item[4] for item in FROSTBITE_101_SURFACES}
NATIVE_COLLISION_MATERIAL_IDENTIFIER_MAP = {item[0]: item[1] for item in FROSTBITE_101_SURFACES}
MATERIAL_SLOT_SURFACE_ITEMS.extend(
    [(item[0], f"Frostbite {item[1]}", item[2]) for item in FROSTBITE_101_SURFACES]
)


def is_bouncy_surface_code(packed_code: int) -> bool:
    """Returns True if the given MaterialDecl.Packed code carries jump-pad or wipeout behavior."""
    code = int(packed_code or 0)
    prop_slot = (code >> 19) & 0x1FFF
    mat_slot = (code >> 6) & 0x1FFF
    return prop_slot in (86, 102, 103, 104) or mat_slot in (86, 102, 103, 104)


def is_do_not_align_surface_code(packed_code: int) -> bool:
    """Returns True if the given MaterialDecl.Packed code carries Do-Not-Align behavior (including legacy 0x00080000 bit)."""
    code = int(packed_code or 0)
    prop_slot = (code >> 19) & 0x1FFF
    mat_slot = (code >> 6) & 0x1FFF
    return prop_slot in (1, 111) or mat_slot in (9, 64, 65, 74, 111)


def strip_do_not_align_code(packed_code: int, fallback_code: int = 96) -> int:
    """Strips Do-Not-Align behavior from `packed_code` and returns a clean skateable/grindable surface code."""
    code = int(packed_code or 0)
    if not is_do_not_align_surface_code(code):
        return code if code > 0 else int(fallback_code or 96)
    if code == 58197664:
        return 352  # Metal (Solid / Grindable)
    if code == 58199648:
        return 3680  # Wood (Ramp / Plywood)
    if code == 58198176:
        return 480  # Marble / Tile
    if code == 58200224:
        fb = int(fallback_code or 96)
        return fb if (fb > 0 and not is_do_not_align_surface_code(fb)) else 96
    prop_slot = (code >> 19) & 0x1FFF
    if prop_slot in (1, 111):
        base_code = code & 0x0007FFFF
        mat_slot = (base_code >> 6) & 0x1FFF
        if base_code > 0 and mat_slot not in (9, 64, 65, 74, 111):
            return base_code
    fb = int(fallback_code or 96)
    return fb if (fb > 0 and not is_do_not_align_surface_code(fb)) else 96


def is_no_grind_surface_code(packed_code: int) -> bool:
    """Returns True if the given MaterialDecl.Packed code excludes grinding or edge generation."""
    code = int(packed_code or 0)
    prop_slot = (code >> 19) & 0x1FFF
    mat_slot = (code >> 6) & 0x1FFF
    return prop_slot in (68, 105) or mat_slot in (9, 43, 64, 65, 68, 105)


def apply_prevent_wall_snap_code(packed_code: int, surface_hint: str = "") -> int:
    """
    Maps a surface code to its matching Frostbite Do-Not-Align surface code (property_slot = 111)
    so vertical props/walls/fences never pull the skater sideways when jumping at them:
      - Metal / Rail / Chainlink / Grate -> 58197664 (273 - Metal Thin + Do-not-align)
      - Wood / Timber / Plywood          -> 58199648 (275 - Wood Thick Rough + Do-not-align)
      - Glass / Marble / Smooth Plastic  -> 58198176 (274 - Glass + Do-not-align)
      - Bark / Natural / Concrete / Rest -> 58200224 (276 - Bark + Do-not-align)
    """
    code = int(packed_code or 96)
    if is_do_not_align_surface_code(code):
        return code
    lbl = f"{surface_hint} {get_surface_label(code)}".lower()
    if any(k in lbl for k in ("metal", "rail", "grate", "chainlink", "fence", "diamond", "skirt")):
        return 58197664
    if any(k in lbl for k in ("wood", "timber", "plywood", "masonite", "ramp")):
        return 58199648
    if any(k in lbl for k in ("glass", "marble", "tile", "plastic", "smooth")):
        return 58198176
    return 58200224


def get_surface_behavior_badges(code_or_key: int | str | None, fallback_code: int | None = None) -> list[str]:
    """Returns plain-language behavior badges for a packed surface code or preset key."""
    badges = []
    if fallback_code is not None:
        code = int(fallback_code or 0)
    elif isinstance(code_or_key, int):
        code = int(code_or_key)
    else:
        code = int(get_surface_packed_code(code_or_key) or 0)
    if is_no_grind_surface_code(code):
        badges.append("[no grind]")
    if is_do_not_align_surface_code(code):
        badges.append("[do not align]")
    prop_slot = (code >> 19) & 0x1FFF
    mat_slot = (code >> 6) & 0x1FFF
    if prop_slot == 90 or mat_slot == 90:
        badges.append("[stair]")
    if is_bouncy_surface_code(code):
        badges.append("[bouncy]")
    return badges


_FILTERED_SURFACE_ITEMS_CACHE: list[tuple] = []


def get_filtered_frostbite_surfaces(
    cat_filter: str = "ALL",
    beh_filter: str = "ALL",
    search_q: str = "",
) -> list[tuple]:
    """Filters the 279 Frostbite MaterialGrid declarations by material family, gameplay behavior, and search query."""
    cat_f = str(cat_filter or "ALL").upper()
    beh_f = str(beh_filter or "ALL").upper()
    q = str(search_q or "").strip().lower()

    if cat_f == "ALL" and beh_f == "ALL" and not q:
        return list(FROSTBITE_101_SURFACES)

    out = []
    for item in FROSTBITE_101_SURFACES:
        ident, lbl, desc, _icon, packed = item
        text_low = f"{lbl} {desc}".lower()

        # 1. Category filter
        if cat_f == "CONCRETE_STONE" and not any(k in text_low for k in ("concrete", "asphalt", "marble", "brick", "bedrock", "default")):
            continue
        elif cat_f == "METAL" and not any(k in text_low for k in ("metal", "rail", "grate", "chainlink", "fence", "diamond", "skirt")):
            continue
        elif cat_f == "WOOD" and not any(k in text_low for k in ("wood", "bark", "cardboard")):
            continue
        elif cat_f == "PLASTIC_GLASS" and not any(k in text_low for k in ("plastic", "glass", "rubber")):
            continue
        elif cat_f == "NATURAL" and not any(k in text_low for k in ("grass", "earth", "sand", "gravel", "water", "bark")):
            continue
        elif cat_f == "SPECIAL" and not any(k in text_low for k in ("no grind", "do not align", "stair", "jump", "wipeout", "occluder", "exclude")):
            continue

        # 2. Behavior filter
        is_no_align = is_do_not_align_surface_code(packed) or "[do not align]" in text_low
        is_no_grind = is_no_grind_surface_code(packed) or "[no grind]" in text_low
        is_stair = "[stair]" in text_low or "stair" in text_low
        is_bouncy = is_bouncy_surface_code(packed) or "[bouncy]" in text_low
        if beh_f == "STANDARD":
            if is_no_align or is_no_grind or is_stair or is_bouncy:
                continue
        elif beh_f in ("DO_NOT_ALIGN", "NO_ALIGN"):
            if not is_no_align:
                continue
        elif beh_f == "NO_GRIND":
            if not is_no_grind:
                continue
        elif beh_f == "STAIRS":
            if not is_stair:
                continue
        elif beh_f in ("BOUNCY", "BOUNCY_HAZARD"):
            if not is_bouncy:
                continue

        # 3. Keyword search
        if q and q not in text_low and q not in ident.lower():
            continue

        out.append(item)

    return out if out else list(FROSTBITE_101_SURFACES[:1])


_FILTERED_SURFACE_ITEMS_HISTORY: list[list[tuple]] = []


def _get_filtered_surface_catalog_items(self, context):
    """Dynamic EnumProperty items callback for `full_surface_catalog`."""
    global _FILTERED_SURFACE_ITEMS_CACHE
    try:
        cat_f = getattr(self, "surface_category_filter", "ALL")
        beh_f = getattr(self, "surface_behavior_filter", "ALL")
        q = getattr(self, "surface_search_filter", "")
        raw_items = list(get_filtered_frostbite_surfaces(cat_f, beh_f, q))
        # Ensure currently stored enum integer/identifier (and default 96) is always present so RNA never logs a warning
        cur_num = self.get("full_surface_catalog", 96) if hasattr(self, "get") else 96
        present_nums = {it[4] for it in raw_items}
        for req_num in (cur_num, 96):
            if isinstance(req_num, int) and req_num not in present_nums:
                for base_it in FROSTBITE_101_SURFACES:
                    if base_it[4] == req_num:
                        raw_items.append(base_it)
                        present_nums.add(req_num)
                        break
        items = [
            (
                _intern_carousel_str(it[0]),
                _intern_carousel_str(it[1]),
                _intern_carousel_str(it[2]),
                int(it[3]),
                int(it[4]),
            )
            for it in raw_items
        ]
        if items != _FILTERED_SURFACE_ITEMS_CACHE:
            _FILTERED_SURFACE_ITEMS_CACHE = items
            _FILTERED_SURFACE_ITEMS_HISTORY.append(items)
            if len(_FILTERED_SURFACE_ITEMS_HISTORY) > 16:
                del _FILTERED_SURFACE_ITEMS_HISTORY[:-16]
    except Exception:
        _FILTERED_SURFACE_ITEMS_CACHE = list(FROSTBITE_101_SURFACES)
    return _FILTERED_SURFACE_ITEMS_CACHE

def get_surface_packed_code(preset_or_identifier: str | int | None) -> int:
    """Returns the uint32 MaterialDecl.Packed code for any preset key or material identifier."""
    if isinstance(preset_or_identifier, int):
        return preset_or_identifier
    if not isinstance(preset_or_identifier, str):
        return 96  # Default Concrete Smooth
    if preset_or_identifier in PRIMARY_SURFACE_CODE_MAP:
        return PRIMARY_SURFACE_CODE_MAP[preset_or_identifier]
    if preset_or_identifier in GRIND_SURFACE_CODE_MAP:
        return GRIND_SURFACE_CODE_MAP[preset_or_identifier]
    if preset_or_identifier in NATIVE_COLLISION_MATERIAL_PACKED:
        return NATIVE_COLLISION_MATERIAL_PACKED[preset_or_identifier]
    if preset_or_identifier.startswith("material_"):
        try:
            return int(preset_or_identifier.split("_")[1])
        except (IndexError, ValueError):
            pass
    return 96  # Default Concrete Smooth

def get_surface_label(preset_or_identifier: str | int | None) -> str:
    """Returns the human-friendly label for any preset or catalog identifier."""
    if not isinstance(preset_or_identifier, str):
        if isinstance(preset_or_identifier, int):
            for k, v in PRIMARY_SURFACE_CODE_MAP.items():
                if v == preset_or_identifier:
                    return get_surface_label(k)
            for k, v in NATIVE_COLLISION_MATERIAL_PACKED.items():
                if v == preset_or_identifier:
                    return NATIVE_COLLISION_MATERIAL_IDENTIFIER_MAP.get(k, "Concrete (Smooth / Plaza)")
        return "Concrete (Smooth / Plaza)"
    for item in PRIMARY_SURFACE_PRESETS:
        if item[0] == preset_or_identifier:
            return item[1]
    if preset_or_identifier in NATIVE_COLLISION_MATERIAL_IDENTIFIER_MAP:
        return NATIVE_COLLISION_MATERIAL_IDENTIFIER_MAP[preset_or_identifier]
    return "Concrete (Smooth / Plaza)"

# ---------------------------------------------------------------------------
# Per-Object Prop State Persistence & Active-Object Selection Sync
# ---------------------------------------------------------------------------

_IS_SYNCING_PROP = False
_IN_AUTO_GRIND_UPDATE = False
_MSGBUS_OWNER = object()

_PER_PROP_DEFAULTS = {
    "prop_display_name": "My Custom Prop",
    "prop_id": "my_custom_prop",
    "prop_id_locked": False,
    "prop_category": "CUSTOM_TAB",
    "custom_qd_tab": "CustomProps",
    "prop_family_title": "",
    "origin_mode": "BOTTOM_CENTER",
    "prop_key_slug": "",
    "scale_mult": 1.0,
    "collision_mode": "SAME_AS_MESH",
    "no_collision_placement_mode": "FLAT_PLANE",
    "placement_proxy_mode": "MATCH_COLLISION",
    "pphys_target_tris": 64,
    "primary_surface": "concrete_smooth",
    "surface_feel_preset": "NORMAL",
    "prevent_wall_snap_default": False,
    "surface_behavior_filter": "ALL",
    "use_full_surface_catalog": False,
    "full_surface_catalog": "material_0096",
    "surface_material_code": 96,
    "max_col_tris": 5000,
    "trim_bottom": 0.0,
    "anti_lip_enabled": False,
    "anti_lip_sink_m": 0.015,
    "coacd_concavity": 0.05,
    "coacd_max_hulls": 8,
    "crease_angle": math.radians(42.0),
    "auto_min_edge_length": 0.15,
    "auto_ignore_bottom_pct": 8.0,
    "auto_open_above": True,
    "auto_clearance_height": 0.35,
    "auto_include_concave": False,
    "auto_only_selected": False,
    "grind_type": "LEDGE",
    "round_rail": False,
    "grind_tube_radius": 0.032,
    "grind_surface_override": "Metal_Solid_Grindable",
    "enable_custom_textures": True,
    "use_blender_org_textures": True,
    "blender_org_category": "CONCRETE",
    "blender_org_concrete_variant": "concrete_wall_12",
    "blender_org_metal_variant": "metal_plate_05",
    "blender_org_wood_variant": "wood_simple_01",
    "blender_org_marble_variant": "marble_solid_08",
    "blender_org_brick_variant": "brick_wall_01",
    "blender_org_grunge_variant": "grunge_scratches_01",
    "blender_org_decal_variant": "decal_warning_robot",
    "blender_org_offline_fallback": True,
    "look_mode": "AS_IS",
    "look_preset": "CUSTOM",
    "atlas_resolution": "2048",
    "uv_tiling_mult": 1.0,
    "world_scale_tile_size_m": 0.50,
    "world_scale_uv_scope": "ALL_SLOTS",
    "hide_visual_during_col_edit": False,
    "island_margin": 0.015,
    "dielectric_roughness": 0.45,
    "dielectric_metallic": 0.0,
    "dielectric_specular": 0.50,
    "roughness_map_mix": 1.0,
    "metallic_map_mix": 1.0,
    "use_native_771_detail_slots": False,
    "use_2x2m_detail_shader": False,
    "detail_2x2m_preset": "CONCRETE_SIDEWALK",
    "detail_2x2m_intensity": 0.35,
    "detail_2x2m_scale": 1.0,
    "use_procedural_wear": False,
    "wear_basis": "AUTO",
    "wear_edge_strength": 0.65,
    "wear_crevice_strength": 0.65,
    "wear_top_strength": 0.50,
    "wear_scale": 0.50,
    "face_paint_surface": "metal_rail",
    "face_paint_prevent_wall_snap": False,
    "albedo_tint": [1.0, 1.0, 1.0],
    "use_external_texture_override": False,
    "custom_texture_path": "",
    "thumb_bg_preset": "SUNNY_DAY",
    "thumb_bg_style": "SUNNY_DAY",
    "thumb_bg_color": [0.14, 0.16, 0.20],
    "thumb_floor_color": [0.42, 0.40, 0.37],
    "thumb_key_fill_ratio": 0.65,
    "thumb_use_cyclorama_floor": True,
    "thumb_lighting_mode": "OUTDOOR_SUN",
    "thumb_sun_elevation": 38.0,
    "thumb_sun_rotation": 42.0,
    "thumb_exposure": 0.0,
    "thumb_render_as_is": False,
    "thumb_variant_cutout_enabled": False,
    "thumb_rim_tint": [0.90, 0.95, 1.00],
    "custom_thumb_png": "",
    "decal_shader_mode": "CUTOUT_273",
}
_PER_PROP_FIELD_NAMES = frozenset(
    list(_PER_PROP_DEFAULTS.keys())
    + ["custom_collision_object", "custom_placement_proxy_object", "custom_placement_proxy_obj", "collision_mode_ui"]
)


_CUSTOM_COL_HELPER_NAMES_CACHE: tuple[int, float, set[str]] = (-1, 0.0, set())
_OBJ_PROP_STATE_CACHE: dict[int, tuple[tuple, dict]] = {}


def invalidate_prop_state_caches(obj: bpy.types.Object | None = None) -> None:
    """Invalidates cached custom collision helper set and per-object prop state dict."""
    global _CUSTOM_COL_HELPER_NAMES_CACHE, _PACK_AND_SCENE_QD_TABS_CACHE
    _CUSTOM_COL_HELPER_NAMES_CACHE = (-1, 0.0, set())
    _PACK_AND_SCENE_QD_TABS_CACHE = (0.0, "", -1, [])
    if obj is None:
        _OBJ_PROP_STATE_CACHE.clear()
    else:
        try:
            _OBJ_PROP_STATE_CACHE.pop(obj.as_pointer(), None)
        except Exception:
            _OBJ_PROP_STATE_CACHE.clear()


def _get_custom_col_helper_names_set() -> set[str]:
    """Returns a cached set of custom collision / placement proxy object names across `bpy.data.objects`."""
    global _CUSTOM_COL_HELPER_NAMES_CACHE
    import time as _time
    try:
        n_objs = len(bpy.data.objects)
    except Exception:
        return set()
    now = _time.monotonic()
    if _CUSTOM_COL_HELPER_NAMES_CACHE[0] == n_objs and (now - _CUSTOM_COL_HELPER_NAMES_CACHE[1]) < 2.0:
        return _CUSTOM_COL_HELPER_NAMES_CACHE[2]
    names: set[str] = set()
    try:
        for cand in bpy.data.objects:
            if getattr(cand, "type", None) != 'MESH':
                continue
            c_col = getattr(cand, "reskate_custom_collision_object", None)
            if c_col is not None and getattr(c_col, "name", ""):
                names.add(c_col.name)
            c_pphys = getattr(cand, "reskate_custom_placement_proxy_object", None)
            if c_pphys is not None and getattr(c_pphys, "name", ""):
                names.add(c_pphys.name)
            raw_st = cand.get("reskate_prop_state", "")
            if raw_st and ("custom_collision_object_name" in str(raw_st) or "custom_placement_proxy_object_name" in str(raw_st)):
                try:
                    st_d = json.loads(str(raw_st))
                    if isinstance(st_d, dict):
                        cn = st_d.get("custom_collision_object_name")
                        if cn:
                            names.add(str(cn))
                        pn = st_d.get("custom_placement_proxy_object_name")
                        if pn:
                            names.add(str(pn))
                except Exception:
                    pass
    except Exception:
        pass
    _CUSTOM_COL_HELPER_NAMES_CACHE = (n_objs, now, names)
    return names


def is_collision_helper_object(obj: bpy.types.Object | None) -> bool:
    """Returns True if `obj` is a simplified/custom collision or placement proxy helper mesh (`_COL`, `_PPHYS`, `_HULL`, `Collision`, `RailCollision`, `is_col_proxy`, etc.)."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return False
    nm_stem = re.sub(r"\.\d+$", "", obj.name).upper()
    if (
        nm_stem.endswith("_COL")
        or nm_stem.endswith("_PPHYS")
        or nm_stem.endswith("_HULL")
        or nm_stem.endswith("COLLISION")
        or nm_stem.endswith("_COLLISION_PREVIEW")
        or bool(getattr(getattr(obj, "reskate_col_prop", None), "is_col_proxy", False))
        or bool(obj.get("reskate_is_col_helper", False))
        or bool(obj.get("reskate_is_pphys_proxy", False))
        or bool(obj.get("reskate_col_owner", ""))
    ):
        return True
    return obj.name in _get_custom_col_helper_names_set()


def resolve_collision_to_owner_prop(obj: bpy.types.Object | None, scene=None) -> bpy.types.Object | None:
    """
    If `obj` is a collision or placement proxy mesh (`<Prop>_COL`, `<Prop>_PPHYS`, simplified hull, or custom collision object),
    resolves and returns its owner main prop MESH object. Otherwise returns `obj` if it's a valid prop mesh.
    """
    if obj is None:
        return None
    if (
        "Scale_Ref" in obj.name
        or obj.name in ("ReSkate_Skater_Scale_Ref", "ReSkate_Skateboard_Scale_Ref", "ReSkate_RoundRail_Scale_Ref", "ReSkate_Ground_Plane_Ref")
        or obj.name.startswith("ReSkate_Skater")
        or obj.name.startswith("ReSkate_Skateboard")
        or obj.name.startswith("ReSkate_Board")
        or obj.name.startswith("ReSkate_RoundRail")
        or obj.name.startswith("ReSkate_Ground_Plane")
        or obj.name.startswith("_ReSkate_")
        or bool(obj.get("reskate_is_scale_reference", False))
        or bool(obj.get("reskate_is_ref_helper", False))
    ):
        return None
    if getattr(obj, "type", None) == 'MESH':
        if is_collision_helper_object(obj):
            # 1. Explicit owner name property
            owner_name = str(obj.get("reskate_col_owner", "") or "").strip()
            if owner_name and owner_name in bpy.data.objects:
                cand_o = bpy.data.objects[owner_name]
                if getattr(cand_o, "type", None) == 'MESH' and not is_collision_helper_object(cand_o):
                    return cand_o
            # 2. Direct parent mesh
            if obj.parent and getattr(obj.parent, "type", None) == 'MESH' and not is_collision_helper_object(obj.parent):
                return obj.parent
            # 3. Reverse pointer search across scene/blend objects (`reskate_custom_collision_object` / `reskate_custom_placement_proxy_object` or saved state)
            try:
                for cand in bpy.data.objects:
                    if cand != obj and getattr(cand, "type", None) == 'MESH' and not is_collision_helper_object(cand):
                        if getattr(cand, "reskate_custom_collision_object", None) == obj:
                            return cand
                        if getattr(cand, "reskate_custom_placement_proxy_object", None) == obj:
                            return cand
                        raw_st = cand.get("reskate_prop_state", "")
                        if raw_st and obj.name in str(raw_st):
                            try:
                                st_d = json.loads(str(raw_st))
                                if isinstance(st_d, dict) and obj.name in (
                                    st_d.get("custom_collision_object_name"),
                                    st_d.get("custom_placement_proxy_object_name"),
                                ):
                                    return cand
                            except Exception:
                                pass
            except Exception:
                pass
            # 4. Name suffix stripping (`_COL`, `_PPHYS`, `_HULL`, `_Collision`, `_COL.001`)
            base_name = re.sub(r"(_col|_pphys|_hull|_collision|_collision_preview)(\.\d+)?$", "", obj.name, flags=re.IGNORECASE)
            if base_name and base_name != obj.name and base_name in bpy.data.objects:
                cand_b = bpy.data.objects[base_name]
                if getattr(cand_b, "type", None) == 'MESH':
                    return cand_b
            # 5. Sibling visual prop mesh in the same asset sub-collection (e.g. 'Collision.001' -> 'Cube.002' in 'Kickout_curb')
            try:
                root_coll = scene.collection if scene is not None else (bpy.context.scene.collection if getattr(bpy.context, "scene", None) else None)
                for coll in getattr(obj, "users_collection", ()):
                    if coll == root_coll:
                        continue
                    vis_cands = [
                        o for o in coll.objects
                        if o != obj
                        and getattr(o, "type", None) == 'MESH'
                        and not is_collision_helper_object(o)
                    ]
                    if len(vis_cands) == 1:
                        return vis_cands[0]
                    elif len(vis_cands) > 1:
                        c_loc = obj.matrix_world.to_translation()
                        vis_cands.sort(key=lambda v: (v.matrix_world.to_translation() - c_loc).length)
                        return vis_cands[0]
            except Exception:
                pass
        return obj
    if obj.parent and getattr(obj.parent, "type", None) == 'MESH':
        return resolve_collision_to_owner_prop(obj.parent)
    if getattr(obj, "type", None) == 'LIGHT':
        owner_nm = str(obj.get("reskate_owner_prop", "") or "").strip()
        if owner_nm and owner_nm in bpy.data.objects:
            cand_m = resolve_collision_to_owner_prop(bpy.data.objects[owner_nm])
            if cand_m is not None:
                return cand_m
        try:
            for cand in bpy.data.objects:
                if getattr(cand, "type", None) == 'MESH' and not is_collision_helper_object(cand):
                    for it in getattr(cand, "reskate_linked_lights", ()):
                        if getattr(it, "light_obj", None) == obj:
                            return cand
        except Exception:
            pass
    return None


def resolve_active_prop_mesh(context=None) -> bpy.types.Object | None:
    """Resolves the active object (or _COL / simplified collision / child light / thumbnail camera) to its authored prop MESH object."""
    ctx = context if context is not None else bpy.context
    if ctx is None:
        return None
    obj = None
    vl = getattr(ctx, "view_layer", None)
    if vl is not None and getattr(vl, "objects", None) is not None:
        obj = vl.objects.active
    if obj is None:
        obj = getattr(ctx, "active_object", None)
    sel_objs = list(getattr(ctx, "selected_objects", ()) or ())
    scene = getattr(ctx, "scene", None) or getattr(bpy.context, "scene", None)
    last_synced = ""
    last_was_selected = False
    if scene is not None:
        try:
            prop = getattr(scene, "reskate_prop", None)
            if prop is not None:
                last_synced = str(super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_obj_name", "") or "")
                last_was_selected = bool(super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_was_selected", False))
        except Exception:
            pass
    if obj is not None:
        try:
            obj_is_selected = bool(obj.select_get()) or (obj in sel_objs) or (getattr(obj, "mode", "OBJECT") != 'OBJECT')
        except Exception:
            obj_is_selected = True
        # If the user box-selected a prop in the viewport while the previously-selected synced object remained `active_object` (now unselected),
        # prefer the newly selected prop mesh in `selected_objects`!
        if not obj_is_selected and sel_objs and last_synced and obj.name == last_synced and last_was_selected:
            for sel_o in sel_objs:
                sel_resolved = resolve_collision_to_owner_prop(sel_o)
                if sel_resolved is not None:
                    return sel_resolved
        resolved = resolve_collision_to_owner_prop(obj)
        if resolved is not None:
            return resolved
        # If the Thumbnail Camera is active (e.g. while framing/adjusting on Page 5), resolve its framed target mesh
        if getattr(obj, "type", None) == 'CAMERA':
            for t_key in ("reskate_target_obj", "reskate_framed_target"):
                t_name = str(obj.get(t_key, "") or "").strip()
                if t_name and t_name in bpy.data.objects:
                    t_resolved = resolve_collision_to_owner_prop(bpy.data.objects[t_name])
                    if t_resolved is not None:
                        return t_resolved

    # Fallback to selected objects if a helper/light/camera is active alongside a selected prop mesh
    for sel_o in sel_objs:
        sel_resolved = resolve_collision_to_owner_prop(sel_o)
        if sel_resolved is not None:
            return sel_resolved

    # Final fallback: last synced object on scene.reskate_prop if still present in the scene
    scene = getattr(ctx, "scene", None)
    if scene is not None:
        try:
            prop = getattr(scene, "reskate_prop", None)
            if prop is not None:
                last_synced = super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_obj_name", "")
                if last_synced and last_synced in scene.objects:
                    last_resolved = resolve_collision_to_owner_prop(scene.objects[last_synced])
                    if last_resolved is not None:
                        return last_resolved
        except Exception:
            pass
    return None


def _clean_slug_id(raw_str: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', str(raw_str or "").lower().strip())
    for pfx in ("own_bkcustomprops_", "own_customprops_", "own_bkcustom_"):
        if clean.startswith(pfx):
            clean = clean[len(pfx):]
            break
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean or "prop"


def get_or_init_object_prop_state(obj: bpy.types.Object) -> dict:
    """Returns the per-object prop authoring settings dictionary for `obj`, initializing defaults from `obj` if needed."""
    default_qd_tab = get_default_qd_tab_name()
    if obj is None or getattr(obj, "type", None) != 'MESH':
        state = dict(_PER_PROP_DEFAULTS)
        state["custom_qd_tab"] = default_qd_tab
        state["albedo_tint"] = list(_PER_PROP_DEFAULTS["albedo_tint"])
        state["thumb_bg_color"] = list(_PER_PROP_DEFAULTS["thumb_bg_color"])
        state["thumb_floor_color"] = list(_PER_PROP_DEFAULTS["thumb_floor_color"])
        state["thumb_rim_tint"] = list(_PER_PROP_DEFAULTS["thumb_rim_tint"])
        state["custom_collision_object_name"] = ""
        state["custom_placement_proxy_object_name"] = ""
        return state

    raw_json = str(obj.get("reskate_prop_state", "") or obj.get("reskate_prop_state_json", "") or "")
    obj_ptr = obj.as_pointer() if hasattr(obj, "as_pointer") else id(obj)
    cache_sig = (
        obj.name,
        raw_json,
        str(obj.get("reskate_display_name", "")),
        str(obj.get("reskate_prop_id", "")),
        str(obj.get("reskate_key_slug", "")),
        str(obj.get("reskate_family_title", "")),
        bool(obj.get("reskate_custom_id_override", False)),
        default_qd_tab,
    )
    hit = _OBJ_PROP_STATE_CACHE.get(obj_ptr)
    if hit is not None and hit[0] == cache_sig:
        res_copy = dict(hit[1])
        res_copy["albedo_tint"] = list(hit[1]["albedo_tint"])
        res_copy["thumb_bg_color"] = list(hit[1]["thumb_bg_color"])
        res_copy["thumb_floor_color"] = list(hit[1]["thumb_floor_color"])
        res_copy["thumb_rim_tint"] = list(hit[1]["thumb_rim_tint"])
        return res_copy

    state = dict(_PER_PROP_DEFAULTS)
    state["custom_qd_tab"] = default_qd_tab
    state["albedo_tint"] = list(_PER_PROP_DEFAULTS["albedo_tint"])
    state["thumb_bg_color"] = list(_PER_PROP_DEFAULTS["thumb_bg_color"])
    state["thumb_floor_color"] = list(_PER_PROP_DEFAULTS["thumb_floor_color"])
    state["thumb_rim_tint"] = list(_PER_PROP_DEFAULTS["thumb_rim_tint"])
    state["custom_collision_object_name"] = ""
    state["custom_placement_proxy_object_name"] = ""

    loaded_from_json = False
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                state.update(parsed)
                loaded_from_json = True
        except Exception:
            pass

    if not loaded_from_json:
        # Derive clean per-object initial defaults from the mesh object's own name & materials
        clean_id = _clean_slug_id(obj.name)
        disp_name = str(obj.get("reskate_display_name") or obj.name)
        pid = _clean_slug_id(obj.get("reskate_prop_id") or clean_id)
        slug = str(obj.get("reskate_key_slug") or f"own_customprops_{pid}")
        if not slug.startswith("own_"):
            slug = f"own_customprops_{pid}"
        state["prop_display_name"] = disp_name
        state["prop_id"] = pid
        state["prop_id_locked"] = bool(obj.get("reskate_custom_id_override", False))
        state["prop_key_slug"] = slug
        state["prop_family_title"] = str(obj.get("reskate_family_title") or "").strip()
        state["custom_qd_tab"] = format_qd_tab_display_name(str(obj.get("reskate_custom_qd_tab") or default_qd_tab))
        state["thumb_variant_cutout_enabled"] = False

        # Check if a matching <ObjName>_COL or <ObjName>_PPHYS proxy exists
        col_cand_name = f"{obj.name}_COL"
        col_cand = getattr(obj, "reskate_custom_collision_object", None) or bpy.data.objects.get(col_cand_name)
        if col_cand is not None and getattr(col_cand, "type", None) == 'MESH':
            state["custom_collision_object_name"] = col_cand.name
        pphys_cand_name = f"{obj.name}_PPHYS"
        pphys_cand = getattr(obj, "reskate_custom_placement_proxy_object", None) or bpy.data.objects.get(pphys_cand_name)
        if pphys_cand is not None and getattr(pphys_cand, "type", None) == 'MESH':
            state["custom_placement_proxy_object_name"] = pphys_cand.name
    else:
        # Unless user explicitly turned on Custom Prop ID Override, keep prop_id_locked=False so Prop Name always updates Prop ID
        if not bool(obj.get("reskate_custom_id_override", False)):
            state["prop_id_locked"] = False
        if int(state.get("max_col_tris", 5000) or 5000) == 2500:
            state["max_col_tris"] = 5000
        if state.get("placement_proxy_mode") == "BOUNDING_BOX" and not bool(obj.get("reskate_pphys_mode_explicit", False)):
            state["placement_proxy_mode"] = "MATCH_COLLISION"
        raw_tab = str(state.get("custom_qd_tab") or "").strip()
        if not raw_tab or (raw_tab.lower() in ("bkcustomprops", "customprops") and not bool(obj.get("reskate_user_set_qd_tab", False))):
            cat_prev = str(state.get("prop_category") or "").strip()
            if cat_prev and cat_prev.upper() not in ("CUSTOM_TAB", "CUSTOM PROPS", "BK_CUSTOM"):
                raw_tab = cat_prev
            else:
                raw_tab = default_qd_tab
        state["custom_qd_tab"] = format_qd_tab_display_name(raw_tab or default_qd_tab)
        state["prop_category"] = "CUSTOM_TAB"
        if state["custom_qd_tab"] and state["custom_qd_tab"] != "CustomProps":
            try:
                save_custom_qd_tab(state["custom_qd_tab"])
            except Exception:
                pass
        # Ensure legacy ID properties stay in sync if someone wrote them directly
        if obj.get("reskate_display_name") and not state.get("prop_display_name"):
            state["prop_display_name"] = str(obj["reskate_display_name"])
        if obj.get("reskate_prop_id") and not state.get("prop_id"):
            state["prop_id"] = _clean_slug_id(obj["reskate_prop_id"])
        if not state.get("prop_key_slug"):
            state["prop_key_slug"] = f"own_customprops_{_clean_slug_id(state.get('prop_id') or obj.name)}"
        if obj.get("reskate_family_title") is not None and not state.get("prop_family_title"):
            state["prop_family_title"] = str(obj.get("reskate_family_title") or "").strip()
        state["thumb_variant_cutout_enabled"] = False

        # Self-heal if an earlier unsynced carousel switch accidentally overwrote this object's display name / ID with another object's name or default
        saved_disp = str(state.get("prop_display_name") or "").strip()
        clean_obj_slug = _clean_slug_id(obj.name)
        clean_disp_slug = _clean_slug_id(saved_disp)
        contaminated_by_other = False
        if not saved_disp or (saved_disp == "My Custom Prop" and obj.name != "My Custom Prop"):
            contaminated_by_other = True
        elif clean_disp_slug != clean_obj_slug and not bool(obj.get("reskate_user_renamed_display", False)):
            for other_o in bpy.data.objects:
                if other_o != obj and getattr(other_o, "type", None) == 'MESH':
                    if other_o.name == saved_disp or _clean_slug_id(other_o.name) == clean_disp_slug:
                        contaminated_by_other = True
                        break
        if contaminated_by_other:
            state["prop_display_name"] = obj.name
            state["prop_id"] = clean_obj_slug
            state["prop_key_slug"] = f"own_customprops_{clean_obj_slug}"
            try:
                obj["reskate_display_name"] = obj.name
                obj["reskate_prop_id"] = clean_obj_slug
                obj["reskate_key_slug"] = state["prop_key_slug"]
                ser_healed = json.dumps(state)
                obj["reskate_prop_state"] = ser_healed
                obj["reskate_prop_state_json"] = ser_healed
            except Exception:
                pass

    # Whenever look_preset is CUSTOM, keep Roughness / Metallic synced from the object's Principled BSDF material
    if state.get("look_preset", "CUSTOM") == "CUSTOM":
        try:
            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat.use_nodes and mat.node_tree:
                    p_node = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                    if p_node:
                        if "Roughness" in p_node.inputs:
                            r_val = round(float(p_node.inputs["Roughness"].default_value), 3)
                            state["dielectric_roughness"] = max(0.0, min(1.0, r_val))
                        if "Metallic" in p_node.inputs:
                            m_val = round(float(p_node.inputs["Metallic"].default_value), 3)
                            state["dielectric_metallic"] = max(0.0, min(1.0, m_val))
                        break
        except Exception:
            pass

    if len(_OBJ_PROP_STATE_CACHE) > 512:
        _OBJ_PROP_STATE_CACHE.clear()
    _OBJ_PROP_STATE_CACHE[obj_ptr] = (cache_sig, dict(state))
    return state


def save_object_prop_state(obj: bpy.types.Object | None, state: dict) -> None:
    """Persists a raw per-object state dict onto `obj`."""
    if obj is None or not isinstance(state, dict):
        return
    try:
        serialized = json.dumps(state)
        obj["reskate_prop_state"] = serialized
        obj["reskate_prop_state_json"] = serialized
        if "prop_display_name" in state:
            obj["reskate_display_name"] = state["prop_display_name"]
        if "prop_id" in state:
            obj["reskate_prop_id"] = state["prop_id"]
        if "prop_key_slug" in state:
            obj["reskate_key_slug"] = state["prop_key_slug"]
        if "prop_family_title" in state:
            obj["reskate_family_title"] = str(state.get("prop_family_title") or "").strip()
        invalidate_prop_state_caches(obj)
    except Exception:
        pass


def save_prop_settings_to_object(prop, obj: bpy.types.Object | None):
    """Persists all per-prop fields from `prop` (`scene.reskate_prop`) onto `obj`."""
    if isinstance(prop, bpy.types.Object) and not isinstance(obj, bpy.types.Object):
        prop, obj = obj, prop
    if prop is None or obj is None or getattr(obj, "type", None) != 'MESH':
        return
    if obj.name.endswith("_COL") or getattr(getattr(obj, "reskate_col_prop", None), "is_col_proxy", False):
        return
    # Never overwrite `obj` if `prop` is still synced to a DIFFERENT existing object in the scene!
    try:
        synced_name = str(super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_obj_name", "") or "")
        if synced_name and synced_name != obj.name and not _IN_NAME_SYNC and synced_name in bpy.data.objects:
            return
    except Exception:
        pass
    state = {}
    for k, default_v in _PER_PROP_DEFAULTS.items():
        try:
            val = super(ReSkatePropSettings, prop).__getattribute__(k)
            if isinstance(default_v, list):
                val = [round(float(x), 4) for x in val]
            elif isinstance(default_v, bool):
                val = bool(val)
            elif isinstance(default_v, int):
                val = int(val)
            elif isinstance(default_v, float):
                val = round(float(val), 6)
            else:
                val = str(val)
            state[k] = val
        except Exception:
            state[k] = default_v

    col_obj = None
    try:
        col_obj = super(ReSkatePropSettings, prop).__getattribute__("custom_collision_object")
    except Exception:
        pass
    state["custom_collision_object_name"] = col_obj.name if (col_obj is not None and getattr(col_obj, "name", "")) else ""

    pphys_obj = None
    try:
        pphys_obj = super(ReSkatePropSettings, prop).__getattribute__("custom_placement_proxy_object")
    except Exception:
        pass
    state["custom_placement_proxy_object_name"] = pphys_obj.name if (pphys_obj is not None and getattr(pphys_obj, "name", "")) else ""
    if bool(obj.get("reskate_user_set_variant_cutout", False)):
        state["user_set_variant_cutout"] = True

    try:
        serialized = json.dumps(state)
        obj["reskate_prop_state"] = serialized
        obj["reskate_prop_state_json"] = serialized
        obj["reskate_display_name"] = state["prop_display_name"]
        obj["reskate_prop_id"] = state["prop_id"]
        obj["reskate_prop_id_locked"] = bool(state["prop_id_locked"])
        obj["reskate_key_slug"] = state["prop_key_slug"]
        obj["reskate_family_title"] = str(state.get("prop_family_title", "") or "").strip()
        obj["reskate_thumb_variant_cutout_enabled"] = bool(state.get("thumb_variant_cutout_enabled", False))
        if hasattr(obj, "reskate_custom_collision_object") and getattr(obj, "reskate_custom_collision_object", None) != col_obj:
            obj.reskate_custom_collision_object = col_obj
        if hasattr(obj, "reskate_custom_placement_proxy_object") and getattr(obj, "reskate_custom_placement_proxy_object", None) != pphys_obj:
            obj.reskate_custom_placement_proxy_object = pphys_obj
        prop["_active_synced_obj_name"] = obj.name
        try:
            prop["_active_synced_was_selected"] = bool(obj.select_get())
        except Exception:
            prop["_active_synced_was_selected"] = True
        prop["_slug_owner_obj"] = obj.name
        invalidate_prop_state_caches(obj)
    except Exception:
        pass


_DEFERRED_SYNC_SCHEDULED: bool = False


def _run_deferred_active_prop_sync():
    global _DEFERRED_SYNC_SCHEDULED
    _DEFERRED_SYNC_SCHEDULED = False
    try:
        sync_active_object_props(bpy.context)
    except Exception:
        pass
    return None


def _schedule_deferred_active_prop_sync():
    global _DEFERRED_SYNC_SCHEDULED
    if _DEFERRED_SYNC_SCHEDULED or bpy.app.background:
        return
    try:
        if hasattr(bpy.app, "timers"):
            _DEFERRED_SYNC_SCHEDULED = True
            bpy.app.timers.register(_run_deferred_active_prop_sync, first_interval=0.001)
    except Exception:
        _DEFERRED_SYNC_SCHEDULED = False


def load_prop_settings_from_object(prop, obj: bpy.types.Object | None) -> bool:
    """Loads per-object prop settings from `obj` into `prop` (`scene.reskate_prop`). Returns True if loaded."""
    global _IS_SYNCING_PROP
    if isinstance(prop, bpy.types.Object) and not isinstance(obj, bpy.types.Object):
        prop, obj = obj, prop
    if prop is None or obj is None or getattr(obj, "type", None) != 'MESH':
        return False

    state = get_or_init_object_prop_state(obj)
    if state.get("thumb_bg_preset") not in ("SUNNY_DAY", "STUDIO", "TRANSPARENT", "FLAT_COLOR"):
        state["thumb_bg_preset"] = "STUDIO" if state.get("thumb_bg_preset") != "TRANSPARENT" else "TRANSPARENT"
    prev_sync = _IS_SYNCING_PROP
    _IS_SYNCING_PROP = True
    try:
        # Probe whether RNA property writes are allowed in the current Blender context (blocked inside UI Panel.draw()).
        # Never mark _active_synced_obj_name as synced if RNA setattr is disallowed!
        try:
            setattr(prop, "prop_id_locked", bool(state.get("prop_id_locked", False)))
        except Exception:
            _schedule_deferred_active_prop_sync()
            return False

        for k, default_v in _PER_PROP_DEFAULTS.items():
            val = state.get(k, default_v)
            try:
                setattr(prop, k, val)
            except Exception:
                pass

        col_obj = getattr(obj, "reskate_custom_collision_object", None)
        if col_obj is None:
            col_name = state.get("custom_collision_object_name", "")
            if col_name and col_name in bpy.data.objects:
                col_obj = bpy.data.objects[col_name]
        try:
            if super(ReSkatePropSettings, prop).__getattribute__("custom_collision_object") != col_obj:
                prop.custom_collision_object = col_obj
        except Exception:
            pass

        pphys_obj = getattr(obj, "reskate_custom_placement_proxy_object", None)
        if pphys_obj is None:
            pphys_name = state.get("custom_placement_proxy_object_name", "")
            if pphys_name and pphys_name in bpy.data.objects:
                pphys_obj = bpy.data.objects[pphys_name]
        try:
            if super(ReSkatePropSettings, prop).__getattribute__("custom_placement_proxy_object") != pphys_obj:
                prop.custom_placement_proxy_object = pphys_obj
        except Exception:
            pass

        prop["_active_synced_obj_name"] = obj.name
        try:
            prop["_active_synced_was_selected"] = bool(obj.select_get())
        except Exception:
            prop["_active_synced_was_selected"] = True
        prop["_slug_owner_obj"] = obj.name
        if state.get("custom_thumb_png"):
            prop["custom_thumb_owner"] = obj.name
    except Exception:
        return False
    finally:
        _IS_SYNCING_PROP = prev_sync

    return True


def sync_active_object_props(context=None) -> bool:
    """
    Checks if the active mesh object changed and synchronizes `scene.reskate_prop`
    to reflect the newly selected object's saved Prop Studio settings.
    """
    global _IS_SYNCING_PROP
    if _IS_SYNCING_PROP:
        return False

    ctx = context if context is not None else bpy.context
    if ctx is None:
        return False
    if isinstance(ctx, bpy.types.Scene):
        scene = ctx
        ctx = bpy.context
    else:
        scene = getattr(ctx, "scene", None) or getattr(bpy.context, "scene", None)
    if scene is None:
        return False
    prop = getattr(scene, "reskate_prop", None)
    if prop is None:
        return False

    # If the user manually rotated out of Thumbnail Camera view in the viewport, clean up any lingering studio lights/floor immediately
    try:
        from . import operators
        if hasattr(operators, "cleanup_if_exited_thumbnail_camera_view"):
            operators.cleanup_if_exited_thumbnail_camera_view(ctx)
    except Exception:
        pass

    active_mesh = resolve_active_prop_mesh(ctx)
    if active_mesh is None:
        return False

    # Ensure any direct child Spot/Point LIGHT objects parented to `active_mesh` are reflected in `reskate_linked_lights`
    try:
        if hasattr(active_mesh, "reskate_linked_lights"):
            existing_l = {getattr(it, "light_obj", None) for it in active_mesh.reskate_linked_lights if getattr(it, "light_obj", None) is not None}
            for ch in getattr(active_mesh, "children", ()):
                if (
                    getattr(ch, "type", None) == 'LIGHT'
                    and getattr(ch, "data", None) is not None
                    and getattr(ch.data, "type", "") in ('SPOT', 'POINT')
                    and not ch.name.startswith(("ReSkate_Temp_", "_ReSkate_"))
                    and not bool(ch.get("reskate_unlinked_by_user", False))
                    and ch not in existing_l
                ):
                    slot = active_mesh.reskate_linked_lights.add()
                    slot.light_obj = ch
                    slot.enabled = True
                    active_mesh.reskate_linked_light_index = len(active_mesh.reskate_linked_lights) - 1
                    existing_l.add(ch)
    except Exception:
        pass

    # If the user manually selected the visual prop directly in Object Mode after editing collision, restore its display/select state
    if (
        getattr(ctx, "mode", "OBJECT") == 'OBJECT'
        and getattr(ctx, "active_object", None) == active_mesh
        and bool(active_mesh.get("reskate_col_edit_isolated", False))
    ):
        try:
            active_mesh.hide_viewport = False
            active_mesh.hide_set(False)
            active_mesh.display_type = str(active_mesh.get("reskate_saved_display_type", "TEXTURED") or "TEXTURED")
            active_mesh.hide_select = bool(active_mesh.get("reskate_saved_hide_select", False))
            active_mesh["reskate_col_edit_isolated"] = False
        except Exception:
            pass

    try:
        synced_name = super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_obj_name", "")
    except Exception:
        synced_name = ""

    if synced_name == active_mesh.name:
        return False

    loaded = load_prop_settings_from_object(prop, active_mesh)
    if loaded:
        try:
            from . import compiler_bridge
            for idx, slot in enumerate(getattr(active_mesh, "material_slots", ())):
                if slot.material is not None:
                    compiler_bridge.sync_material_from_shader_nodes(
                        slot.material, obj=active_mesh, slot_index=idx, heal=False
                    )
        except Exception:
            pass
        try:
            from . import operators
            if hasattr(operators, "sync_thumbnail_camera_on_prop_switch"):
                operators.sync_thumbnail_camera_on_prop_switch(ctx, active_mesh)
        except Exception:
            pass
        try:
            from . import ui_panels
            if hasattr(ui_panels, "on_active_prop_switched"):
                ui_panels.on_active_prop_switched()
            else:
                ui_panels.invalidate_panel_caches()
        except Exception:
            pass
        try:
            wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
            if wm:
                for win in wm.windows:
                    if win.screen:
                        for area in win.screen.areas:
                            if area.type in ('VIEW_3D', 'PROPERTIES', 'OUTLINER'):
                                area.tag_redraw()
        except Exception:
            pass
    return loaded


def _handle_per_prop_field_write(prop, context, field_name: str, extra_fn=None):
    """
    Handles any write to a per-prop property on `ReSkatePropSettings`:
    1. If the active mesh object changed since the last sync, loads the new object's state first
       and then applies only the newly written field so previous object settings never bleed over!
    2. Runs `extra_fn(prop, context)` if provided.
    3. Saves all per-prop settings onto the active mesh object.
    """
    global _IS_SYNCING_PROP
    if _IS_SYNCING_PROP:
        return

    ctx = context if context is not None else bpy.context
    obj = resolve_active_prop_mesh(ctx)

    prev_sync = _IS_SYNCING_PROP
    _IS_SYNCING_PROP = True
    try:
        if obj is not None:
            synced_name = super(ReSkatePropSettings, prop).__getattribute__("get")("_active_synced_obj_name", "")
            if synced_name != obj.name:
                # Capture the value just written to `field_name` before loading `obj`'s existing state
                written_val = super(ReSkatePropSettings, prop).__getattribute__(field_name)
                if hasattr(written_val, "__len__") and not isinstance(written_val, str):
                    written_val = list(written_val)
                load_prop_settings_from_object(prop, obj)
                setattr(prop, field_name, written_val)

        if extra_fn is not None:
            extra_fn(prop, ctx)

        if obj is not None:
            save_prop_settings_to_object(prop, obj)
    finally:
        _IS_SYNCING_PROP = prev_sync


# ---------------------------------------------------------------------------
# Filter & Update Callbacks
# ---------------------------------------------------------------------------

def _poll_mesh_object(self, obj):
    return obj is not None and getattr(obj, "type", None) == 'MESH'

def _poll_light_object(self, obj):
    return obj is not None and getattr(obj, "type", None) == 'LIGHT'

_IN_NAME_SYNC: bool = False


def _extra_update_prop_name(self, context):
    global _IN_NAME_SYNC, _SANCTUS_CAROUSEL_CACHE
    raw_disp = str(super(ReSkatePropSettings, self).__getattribute__("prop_display_name") or "").strip()
    clean = _clean_slug_id(raw_disp)
    obj = resolve_active_prop_mesh(context)
    cur_id = str(super(ReSkatePropSettings, self).__getattribute__("prop_id") or "")
    stable_key = str(obj.get("reskate_stable_prop_key", "") or "").strip() if obj is not None else ""

    # Two-way sync: keep the Blender object's name in the Outliner in sync with Prop Name (with re-entrancy guard)
    if obj is not None and not _IN_NAME_SYNC:
        new_disp = raw_disp
        if new_disp and obj.name != new_disp:
            _IN_NAME_SYNC = True
            try:
                old_name = obj.name
                # Clear any 0-user orphaned object squatting on `new_disp` so Blender doesn't append .001
                existing_other = bpy.data.objects.get(new_disp)
                if existing_other is not None and existing_other != obj and getattr(existing_other, "users", 1) == 0:
                    try:
                        bpy.data.objects.remove(existing_other, do_unlink=True)
                    except Exception:
                        pass
                obj.name = new_disp
                if getattr(obj, "data", None) is not None and getattr(obj.data, "users", 0) == 1:
                    try:
                        obj.data.name = obj.name
                    except Exception:
                        pass
                try:
                    _LAST_OBJ_NAMES[obj.as_pointer()] = obj.name
                except Exception:
                    pass
                self["_active_synced_obj_name"] = obj.name
                self["_slug_owner_obj"] = obj.name
                # Keep linked _COL / _PPHYS owner and Thumbnail Camera target references in sync with the new object name
                for o_cand in bpy.data.objects:
                    if o_cand != obj:
                        if str(o_cand.get("reskate_col_owner", "") or "") == old_name:
                            o_cand["reskate_col_owner"] = obj.name
                        if getattr(o_cand, "type", None) == 'CAMERA':
                            for t_k in ("reskate_target_obj", "reskate_framed_target"):
                                if str(o_cand.get(t_k, "") or "") == old_name:
                                    o_cand[t_k] = obj.name
            except Exception:
                pass
            finally:
                _IN_NAME_SYNC = False

    if obj is not None:
        try:
            obj["reskate_display_name"] = raw_disp
            obj["reskate_user_renamed_display"] = True
        except Exception:
            pass

    _SANCTUS_CAROUSEL_CACHE = (0.0, (), [])
    try:
        from . import compiler_bridge
        compiler_bridge.invalidate_ui_caches()
    except Exception:
        pass
    try:
        wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm:
            for win in wm.windows:
                if win.screen:
                    for area in win.screen.areas:
                        if area.type in ('OUTLINER', 'VIEW_3D', 'PROPERTIES'):
                            area.tag_redraw()
    except Exception:
        pass

    # If the prop has already been compiled and locked a stable internal key (§5.4), preserve its internal key so packs update in-place
    if stable_key:
        pid_locked = stable_key[len("own_customprops_"):] if stable_key.startswith("own_customprops_") else stable_key
        self.prop_id = pid_locked
        self.prop_key_slug = stable_key
        return

    # Before first compile, auto-update Prop ID and Internal Slug from Prop Name unless Custom Prop ID Override is enabled
    if not getattr(self, "prop_id_locked", False) or not cur_id or cur_id == "my_custom_prop":
        self.prop_id = clean
        self.prop_key_slug = f"own_customprops_{clean}"
        if obj is not None:
            try:
                obj["reskate_prop_id"] = clean
                obj["reskate_key_slug"] = f"own_customprops_{clean}"
            except Exception:
                pass
    elif not self.prop_key_slug:
        pid = _clean_slug_id(cur_id or clean)
        self.prop_key_slug = f"own_customprops_{pid}"


def _extra_update_prop_id(self, context):
    clean = _clean_slug_id(self.prop_id)
    if self.prop_id != clean:
        self.prop_id = clean
    new_slug = f"own_customprops_{clean}"
    if self.prop_key_slug != new_slug:
        self.prop_key_slug = new_slug
    obj = resolve_active_prop_mesh(context)
    if obj is not None:
        try:
            obj["reskate_id_finalized"] = True
        except Exception:
            pass


def _extra_update_prop_slug(self, context):
    clean = _clean_slug_id(self.prop_key_slug)
    if clean and getattr(self, "prop_id", "") != clean:
        self.prop_id = clean
    new_slug = f"own_customprops_{clean}"
    if self.prop_key_slug != new_slug:
        self.prop_key_slug = new_slug


def _update_prop_name(self, context):
    _handle_per_prop_field_write(self, context, "prop_display_name", _extra_update_prop_name)


def _update_prop_id(self, context):
    _handle_per_prop_field_write(self, context, "prop_id", _extra_update_prop_id)


def _update_prop_slug(self, context):
    _handle_per_prop_field_write(self, context, "prop_key_slug", _extra_update_prop_slug)


def _extra_update_prop_family_title(self, context):
    val = str(getattr(self, "prop_family_title", "") or "").strip()
    obj = resolve_active_prop_mesh(context)
    if obj is not None:
        try:
            obj["reskate_family_title"] = val
            if "_reskate_last_preflight_json" in obj:
                obj["_reskate_last_preflight_json"] = ""
        except Exception:
            pass
    try:
        from . import compiler_bridge
        compiler_bridge.invalidate_ui_caches()
    except Exception:
        pass
    try:
        from . import ui_panels
        if hasattr(ui_panels, "invalidate_carousel_icon_for_object") and obj is not None:
            ui_panels.invalidate_carousel_icon_for_object(obj)
    except Exception:
        pass


def _update_prop_family_title(self, context):
    _handle_per_prop_field_write(self, context, "prop_family_title", _extra_update_prop_family_title)
    try:
        from . import operators as _ops
        _ops.sync_live_thumbnail_preview_rig(context)
    except Exception:
        pass
    _tag_redraw_view3d(self, context)


def _extra_update_thumb_variant_cutout(self, context):
    try:
        val = bool(super(ReSkatePropSettings, self).__getattribute__("thumb_variant_cutout_enabled"))
    except Exception:
        val = bool(getattr(self, "thumb_variant_cutout_enabled", False))
    obj = resolve_active_prop_mesh(context)
    if obj is not None:
        try:
            obj["reskate_user_set_variant_cutout"] = True
            obj["reskate_thumb_variant_cutout_enabled"] = val
        except Exception:
            pass


def _update_thumb_variant_cutout(self, context):
    if _IS_SYNCING_PROP:
        return
    _handle_per_prop_field_write(self, context, "thumb_variant_cutout_enabled", _extra_update_thumb_variant_cutout)
    _sync_live_thumb_rig_if_active(self, context)


_SAVED_CUSTOM_QD_TABS_CACHE: list[str] | None = None


def _get_custom_qd_tabs_json_path() -> Path:
    try:
        d = Path.home() / ".reskate_studio"
        d.mkdir(parents=True, exist_ok=True)
        return d / "custom_qd_tabs.json"
    except Exception:
        from . import compiler_bridge
        return compiler_bridge.get_staging_dir() / "custom_qd_tabs.json"


def get_default_qd_tab_name(context=None) -> str:
    """
    Returns the default Quick Drop Tab Name: the user's configured Author Name
    (so props are automatically sorted by author in Quick Drop when left at default),
    or 'CustomProps' if no Author Name has been set yet.
    """
    ctx = context if context is not None else bpy.context
    author = ""
    try:
        if ctx and getattr(ctx, "preferences", None):
            for k, addon_entry in ctx.preferences.addons.items():
                if k.endswith(("reskate_prop_studio_addon", "sk8_prop_authoring_addon")) and getattr(addon_entry, "preferences", None) is not None:
                    cand = str(getattr(addon_entry.preferences, "author_name", "") or "").strip()
                    if cand:
                        author = cand
                        break
    except Exception:
        pass
    if not author:
        try:
            scn = getattr(ctx, "scene", None) if ctx else None
            prop = getattr(scn, "reskate_prop", None) if scn else None
            if prop is not None:
                author = str(getattr(prop, "author_name", "") or getattr(prop, "ts_author_name", "") or "").strip()
        except Exception:
            pass
    if not author:
        try:
            author = str(load_persisted_user_paths().get("author_name", "") or "").strip()
        except Exception:
            author = ""
    if author and author.lower() not in ("yourname", "reskateauthor", "customprops", "bkcustomprops"):
        return format_qd_tab_display_name(author)
    return "CustomProps"


def format_qd_tab_display_name(raw_tab: str) -> str:
    """Formats a raw Quick Drop tab string/slug into a clean display name without a leading 'bk' prefix."""
    t = str(raw_tab or "").strip()
    if not t:
        return "CustomProps"
    t_low = t.lower()
    if t_low in ("bkcustomprops", "customprops", "custom_tab", "custom props", "bk_custom"):
        return "CustomProps"
    if t_low.startswith("bk") and len(t) > 2:
        stripped = t[2:].strip("_- ")
        if stripped:
            return stripped
    return t


def _normalize_qd_tab_key(raw_tab: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(raw_tab or "").strip().lower())
    if s.startswith("bk") and len(s) > 2:
        s = s[2:]
    return s or "customprops"


def load_custom_qd_tabs() -> list[str]:
    """Loads persisted Quick Drop tab names (clean display strings without leading 'bk') from custom_qd_tabs.json."""
    global _SAVED_CUSTOM_QD_TABS_CACHE
    if _SAVED_CUSTOM_QD_TABS_CACHE is not None:
        return list(_SAVED_CUSTOM_QD_TABS_CACHE)
    tabs: list[str] = []
    seen_keys: set[str] = set()
    p = _get_custom_qd_tabs_json_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    disp = format_qd_tab_display_name(str(item or ""))
                    k = _normalize_qd_tab_key(disp)
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        tabs.append(disp)
        except Exception:
            pass
    _SAVED_CUSTOM_QD_TABS_CACHE = tabs
    return list(tabs)


_PACK_AND_SCENE_QD_TABS_CACHE: tuple[float, str, int, list[str]] = (0.0, "", -1, [])


def save_custom_qd_tab(raw_tab: str) -> str:
    """Normalizes and persists a Quick Drop tab name across sessions."""
    global _SAVED_CUSTOM_QD_TABS_CACHE, _PACK_AND_SCENE_QD_TABS_CACHE
    disp = format_qd_tab_display_name(raw_tab)
    k = _normalize_qd_tab_key(disp)
    if not k:
        return "CustomProps"
    tabs = load_custom_qd_tabs()
    existing_keys = {_normalize_qd_tab_key(t): idx for idx, t in enumerate(tabs)}
    if k not in existing_keys:
        tabs.insert(0, disp)
        tabs = tabs[:24]
        _SAVED_CUSTOM_QD_TABS_CACHE = tabs
        _PACK_AND_SCENE_QD_TABS_CACHE = (0.0, "", -1, [])
        try:
            p = _get_custom_qd_tabs_json_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(tabs, indent=2), encoding="utf-8")
        except Exception:
            pass
    return disp


def remove_custom_qd_tab(raw_tab: str) -> list[str]:
    """Removes a saved Quick Drop tab name from persistent storage."""
    global _SAVED_CUSTOM_QD_TABS_CACHE, _PACK_AND_SCENE_QD_TABS_CACHE
    k = _normalize_qd_tab_key(raw_tab)
    tabs = [t for t in load_custom_qd_tabs() if _normalize_qd_tab_key(t) != k]
    _SAVED_CUSTOM_QD_TABS_CACHE = tabs
    _PACK_AND_SCENE_QD_TABS_CACHE = (0.0, "", -1, [])
    try:
        p = _get_custom_qd_tabs_json_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tabs, indent=2), encoding="utf-8")
    except Exception:
        pass
    return list(tabs)


def get_scene_qd_tabs_with_counts(context=None) -> list[dict]:
    """
    Returns an ordered list of Quick Drop Tab dicts (`{"tab_name": str, "count": int, "is_active": bool}`)
    strictly from the prop meshes in the current .blend scene (no global history or disk clutter).
    """
    ctx = context if context is not None else bpy.context
    scn = getattr(ctx, "scene", None) if ctx else None
    prop = getattr(scn, "reskate_prop", None) if scn else None
    def_tab = get_default_qd_tab_name(ctx)
    def_key = _normalize_qd_tab_key(def_tab)
    raw_cur = str(getattr(prop, "custom_qd_tab", "") or "").strip()
    if not raw_cur or raw_cur.lower() in ("customprops", "bkcustomprops"):
        raw_cur = def_tab
    cur_val = format_qd_tab_display_name(raw_cur)
    cur_key = _normalize_qd_tab_key(cur_val)

    counts_by_key: dict[str, int] = {}
    disp_by_key: dict[str, str] = {}
    order_keys: list[str] = []

    def _record(raw_val: str, inc: int = 1):
        if not raw_val:
            return
        disp = format_qd_tab_display_name(raw_val)
        k = _normalize_qd_tab_key(disp)
        if not k:
            return
        if k not in disp_by_key:
            disp_by_key[k] = disp
            counts_by_key[k] = 0
            order_keys.append(k)
        counts_by_key[k] += int(inc)

    try:
        from . import compiler_bridge
        active_mesh = resolve_active_prop_mesh(ctx)
        for o in compiler_bridge.get_all_scene_prop_meshes(ctx):
            if active_mesh is not None and o.name == active_mesh.name and prop is not None:
                c_tab = str(getattr(prop, "custom_qd_tab", "") or "").strip()
                if not c_tab or (c_tab.lower() in ("customprops", "bkcustomprops") and not bool(o.get("reskate_user_set_qd_tab", False))):
                    c_tab = def_tab
            else:
                st_d = get_or_init_object_prop_state(o)
                c_tab = str(
                    (st_d.get("custom_qd_tab") if isinstance(st_d, dict) else "")
                    or o.get("reskate_custom_qd_tab", "")
                    or ""
                ).strip()
                if not c_tab and isinstance(st_d, dict):
                    cat = str(st_d.get("prop_category") or o.get("reskate_category", "") or "").strip()
                    if cat and cat.upper() not in ("CUSTOM_TAB", "CUSTOM PROPS"):
                        c_tab = cat
                if not c_tab or (c_tab.lower() in ("customprops", "bkcustomprops") and not bool(o.get("reskate_user_set_qd_tab", False))):
                    c_tab = def_tab
            _record(c_tab or def_tab, 1)
    except Exception:
        pass

    if cur_val and cur_key not in disp_by_key:
        _record(cur_val, 0)
    if not order_keys:
        _record(def_tab, 0)

    return [
        {
            "tab_name": disp_by_key[k],
            "count": counts_by_key.get(k, 0),
            "is_active": (k == cur_key),
            "is_saved": (k != def_key and k != "customprops"),
        }
        for k in order_keys
    ]


def get_pack_and_scene_qd_tabs(context=None) -> list[str]:
    """
    Returns an ordered list of Quick Drop Tab Names used in the current .blend scene only.
    Cached with a 1.0s TTL so UI redraws stay 0 ms.
    """
    global _PACK_AND_SCENE_QD_TABS_CACHE
    import time as _time
    ctx = context if context is not None else bpy.context
    now = _time.monotonic()
    scn = getattr(ctx, "scene", None) if ctx else None
    n_scn_objs = len(scn.objects) if scn else 0
    prop = getattr(scn, "reskate_prop", None) if scn else None
    def_tab = get_default_qd_tab_name(ctx)
    cur_tab_sig = f"{def_tab}|{getattr(prop, 'custom_qd_tab', '') if prop else ''}"

    if (
        _PACK_AND_SCENE_QD_TABS_CACHE[3]
        and _PACK_AND_SCENE_QD_TABS_CACHE[1] == cur_tab_sig
        and _PACK_AND_SCENE_QD_TABS_CACHE[2] == n_scn_objs
        and (now - _PACK_AND_SCENE_QD_TABS_CACHE[0]) < 1.0
    ):
        return list(_PACK_AND_SCENE_QD_TABS_CACHE[3])

    out = [item["tab_name"] for item in get_scene_qd_tabs_with_counts(ctx)]
    if not out:
        out = [def_tab]
    _PACK_AND_SCENE_QD_TABS_CACHE = (now, cur_tab_sig, n_scn_objs, list(out))
    return out


def _extra_update_custom_qd_tab(self, context):
    global _PACK_AND_SCENE_QD_TABS_CACHE
    _PACK_AND_SCENE_QD_TABS_CACHE = (0.0, "", -1, [])
    raw = str(getattr(self, "custom_qd_tab", "") or "").strip()
    def_tab = get_default_qd_tab_name(context)
    obj = resolve_active_prop_mesh(context)
    if not raw:
        clean_disp = def_tab
        user_explicit = False
        try:
            if getattr(self, "custom_qd_tab", "") != clean_disp:
                self.custom_qd_tab = clean_disp
        except Exception:
            pass
    else:
        clean_disp = format_qd_tab_display_name(raw)
        user_explicit = (_normalize_qd_tab_key(clean_disp) != _normalize_qd_tab_key(def_tab))
        save_custom_qd_tab(clean_disp)
    if getattr(self, "prop_category", "") != "CUSTOM_TAB":
        try:
            self.prop_category = "CUSTOM_TAB"
        except Exception:
            pass
    if obj is not None:
        try:
            obj["reskate_user_set_qd_tab"] = bool(user_explicit)
            obj["reskate_custom_qd_tab"] = clean_disp
            st = get_or_init_object_prop_state(obj)
            st["prop_category"] = "CUSTOM_TAB"
            st["custom_qd_tab"] = clean_disp
            save_object_prop_state(obj, st)
        except Exception:
            pass


def _update_custom_qd_tab(self, context):
    _handle_per_prop_field_write(self, context, "custom_qd_tab", _extra_update_custom_qd_tab)


def _update_target_mod_name(self, context):
    if _IS_SYNCING_PROP:
        return
    val = str(getattr(self, "target_mod_name", "") or "").strip()
    if not val:
        return
    try:
        from . import compiler_bridge
        sel = compiler_bridge.get_selected_prop_meshes(context)
        if not sel:
            act = resolve_active_prop_mesh(context)
            if act is not None:
                sel = [act]
        for o in sel:
            try:
                o["reskate_target_mod_name"] = val
                st = get_or_init_object_prop_state(o)
                st["target_mod_name"] = val
                save_object_prop_state(o, st)
            except Exception:
                pass
        compiler_bridge.invalidate_ui_caches()
    except Exception:
        pass


def _extra_update_placement_proxy_mode(self, context):
    obj = resolve_active_prop_mesh(context)
    if obj is not None:
        try:
            obj["reskate_pphys_mode_explicit"] = True
        except Exception:
            pass


def _update_placement_proxy_mode(self, context):
    _handle_per_prop_field_write(self, context, "placement_proxy_mode", _extra_update_placement_proxy_mode)
    _tag_redraw_view3d(self, context)


def _sync_principled_map_mix_node(
    mat: bpy.types.Material,
    sock_name: str,
    base_val: float,
    mix_val: float,
):
    """
    Syncs a Principled BSDF Roughness/Metallic socket with `base_val` and `mix_val`:
      Final = (1.0 - mix_val) * base_val + mix_val * Map
    When `mix_val >= 0.999` (100% map, the default), removes any `ReSkate_*_Mix` helper node
    and restores the direct link to `Principled BSDF` so the user's shader graph is untouched.
    When `mix_val < 0.999`, inserts/updates a non-destructive `ShaderNodeMapRange`.
    """
    if not mat or not mat.use_nodes or not mat.node_tree:
        return
    nt = mat.node_tree
    p_node = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not p_node or sock_name not in p_node.inputs:
        return
    sock = p_node.inputs[sock_name]
    sock.default_value = float(base_val)
    helper_name = f"ReSkate_{sock_name}_Mix"
    to_min = (1.0 - mix_val) * base_val
    to_max = (1.0 - mix_val) * base_val + mix_val

    if sock.is_linked and sock.links:
        src_link = sock.links[0]
        src_node = src_link.from_node
        src_sock = src_link.from_socket
        if mix_val >= 0.999:
            # At 100% map mix (default), bypass/remove our MapRange helper so the user's shader is untouched!
            if src_node.name == helper_name and src_node.type == "MAP_RANGE":
                val_in = src_node.inputs.get("Value")
                if val_in and val_in.is_linked and val_in.links:
                    orig_sock = val_in.links[0].from_socket
                    nt.links.new(orig_sock, sock)
                try:
                    nt.nodes.remove(src_node)
                except Exception:
                    pass
            return

        if src_node.name == helper_name and src_node.type == "MAP_RANGE":
            mr_node = src_node
        else:
            mr_node = nt.nodes.get(helper_name)
            if mr_node is None or mr_node.type != "MAP_RANGE":
                mr_node = nt.nodes.new("ShaderNodeMapRange")
                mr_node.name = helper_name
                mr_node.label = f"ReSkate {sock_name} Mix"
            mr_node.location = (p_node.location.x - 190, p_node.location.y - (140 if sock_name == "Roughness" else 60))
            mr_node.clamp = True
            nt.links.new(src_sock, mr_node.inputs["Value"])
            nt.links.new(mr_node.outputs["Result"], sock)
        mr_node.clamp = True
        mr_node["reskate_base_val"] = float(base_val)
        mr_node["reskate_mix_val"] = float(mix_val)
        if "From Min" in mr_node.inputs:
            mr_node.inputs["From Min"].default_value = 0.0
        if "From Max" in mr_node.inputs:
            mr_node.inputs["From Max"].default_value = 1.0
        if "To Min" in mr_node.inputs:
            mr_node.inputs["To Min"].default_value = float(to_min)
        if "To Max" in mr_node.inputs:
            mr_node.inputs["To Max"].default_value = float(to_max)


def _extra_update_dielectric_roughness(self, context):
    if getattr(self, "look_preset", "CUSTOM") != "CUSTOM":
        self.look_preset = "CUSTOM"
    obj = resolve_active_prop_mesh(context)
    if obj is not None and getattr(obj, "type", None) == 'MESH':
        r_val = max(0.0, min(1.0, float(getattr(self, "dielectric_roughness", 0.45))))
        r_mix = max(0.0, min(1.0, float(getattr(self, "roughness_map_mix", 1.0))))
        for slot in obj.material_slots:
            mat = slot.material
            if mat:
                sk8_mat = getattr(mat, "sk8_material", None)
                if sk8_mat is not None and hasattr(sk8_mat, "roughness_map_mix"):
                    try:
                        sk8_mat["roughness_map_mix"] = r_mix
                    except Exception:
                        pass
                _sync_principled_map_mix_node(mat, "Roughness", r_val, r_mix)


def _extra_update_dielectric_metallic(self, context):
    if getattr(self, "look_preset", "CUSTOM") != "CUSTOM":
        self.look_preset = "CUSTOM"
    obj = resolve_active_prop_mesh(context)
    if obj is not None and getattr(obj, "type", None) == 'MESH':
        m_val = max(0.0, min(1.0, float(getattr(self, "dielectric_metallic", 0.0))))
        m_mix = max(0.0, min(1.0, float(getattr(self, "metallic_map_mix", 1.0))))
        for slot in obj.material_slots:
            mat = slot.material
            if mat:
                sk8_mat = getattr(mat, "sk8_material", None)
                if sk8_mat is not None and hasattr(sk8_mat, "metallic_map_mix"):
                    try:
                        sk8_mat["metallic_map_mix"] = m_mix
                    except Exception:
                        pass
                _sync_principled_map_mix_node(mat, "Metallic", m_val, m_mix)


def _update_dielectric_roughness(self, context):
    _handle_per_prop_field_write(self, context, "dielectric_roughness", _extra_update_dielectric_roughness)


def _update_dielectric_metallic(self, context):
    _handle_per_prop_field_write(self, context, "dielectric_metallic", _extra_update_dielectric_metallic)


def _update_roughness_map_mix(self, context):
    _handle_per_prop_field_write(self, context, "roughness_map_mix", _extra_update_dielectric_roughness)


def _update_metallic_map_mix(self, context):
    _handle_per_prop_field_write(self, context, "metallic_map_mix", _extra_update_dielectric_metallic)



def _get_col_mode_ui(self) -> int:
    mode = getattr(self, "collision_mode", "SAME_AS_MESH")
    if mode in ("SIMPLE_HULLS", "CONVEX_HULL"):
        return 1
    if mode in ("CUSTOM_OBJECT", "AUTHORED_COL"):
        return 2
    if mode in ("NO_COLLISION", "NONE", "DECAL_ONLY"):
        return 3
    return 0


def _set_col_mode_ui(self, value: int):
    modes = ("SAME_AS_MESH", "SIMPLE_HULLS", "CUSTOM_OBJECT", "NO_COLLISION")
    idx = max(0, min(len(modes) - 1, int(value)))
    self.collision_mode = modes[idx]


def _update_material_slot_surface(self, context):
    """Syncs a per-material collision surface change in 2. Collision to mat.sk8_material in 4. Look."""
    mat_name = getattr(self, "material_name", "")
    surf_val = getattr(self, "surface", "DEFAULT")
    prevent_snap = bool(getattr(self, "prevent_wall_snap", False))
    if not mat_name:
        return
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        return
    prop = getattr(context.scene, "reskate_prop", None) if (context and getattr(context, "scene", None)) else None
    default_code = int(getattr(prop, "surface_material_code", 96) or 96) if prop else 96
    packed_code = default_code if surf_val == "DEFAULT" else get_surface_packed_code(surf_val)
    if prevent_snap:
        packed_code = apply_prevent_wall_snap_code(packed_code, surf_val)
    try:
        mat["sk8_collision_material_packed"] = int(packed_code)
        mat["reskate_prevent_wall_snap"] = bool(prevent_snap)
    except Exception:
        pass
    ext = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    override_audio = bool(getattr(ext, "override_contact_audio", False)) if ext is not None else False
    if not override_audio:
        target_id = f"material_{int(packed_code):04d}"
        for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
            if holder is not None and hasattr(holder, "collision_material"):
                try:
                    holder.collision_material = target_id
                except Exception:
                    pass


def _update_material_slot_feel(self, context):
    """Maps a Feel preset (Normal, Grippy, Slick, Bouncy-Off) onto the material slot's surface."""
    feel = getattr(self, "feel_preset", "NORMAL")
    cur_surf = getattr(self, "surface", "DEFAULT")
    if feel == "GRIPPY":
        if cur_surf in ("DEFAULT", "concrete_smooth", "marble_tile"):
            self.surface = "concrete_rough"
    elif feel == "SLICK":
        if cur_surf in ("DEFAULT", "concrete_rough", "asphalt"):
            self.surface = "marble_tile"
    elif feel == "BOUNCY_OFF":
        if "jump_pad" in str(cur_surf) or "wipeout" in str(cur_surf):
            self.surface = "concrete_smooth"
        self.prevent_wall_snap = True
    _update_material_slot_surface(self, context)


def _extra_update_surface_feel_preset(self, context):
    feel = getattr(self, "surface_feel_preset", "NORMAL")
    if feel == "GRIPPY":
        if self.primary_surface in ("concrete_smooth", "marble_tile"):
            self.primary_surface = "concrete_rough"
            self.surface_material_code = PRIMARY_SURFACE_CODE_MAP.get("concrete_rough", 3168)
    elif feel == "SLICK":
        if self.primary_surface in ("concrete_rough", "asphalt", "concrete_smooth"):
            self.primary_surface = "marble_tile"
            self.surface_material_code = PRIMARY_SURFACE_CODE_MAP.get("marble_tile", 480)
    elif feel == "BOUNCY_OFF":
        if is_bouncy_surface_code(self.surface_material_code):
            self.primary_surface = "concrete_smooth"
            self.surface_material_code = 96
        self.prevent_wall_snap_default = True


def _extra_update_primary_surface(self, context):
    code = PRIMARY_SURFACE_CODE_MAP.get(self.primary_surface, 96)
    self.surface_material_code = code
    if self.primary_surface == "metal_rail":
        self.round_rail = True
        self.grind_type = "ROUND_RAIL"
        self.grind_surface_override = "Metal_Rail_Grindable"


def _extra_update_full_surface(self, context):
    code = get_surface_packed_code(self.full_surface_catalog)
    self.surface_material_code = code


def _extra_update_use_full_catalog(self, context):
    if getattr(self, "use_full_surface_catalog", False):
        self.surface_material_code = get_surface_packed_code(self.full_surface_catalog)
    else:
        self.surface_material_code = PRIMARY_SURFACE_CODE_MAP.get(self.primary_surface, 96)


def _update_surface_feel_preset(self, context):
    _handle_per_prop_field_write(self, context, "surface_feel_preset", _extra_update_surface_feel_preset)


def _update_primary_surface(self, context):
    _handle_per_prop_field_write(self, context, "primary_surface", _extra_update_primary_surface)


def _update_full_surface(self, context):
    _handle_per_prop_field_write(self, context, "full_surface_catalog", _extra_update_full_surface)


def _update_use_full_catalog(self, context):
    _handle_per_prop_field_write(self, context, "use_full_surface_catalog", _extra_update_use_full_catalog)


def _tag_redraw_view3d(self=None, context=None):
    """Invalidates GPU caches, synchronizes source-level draw handlers, and tags all 3D Viewports for redraw."""
    if _IS_SYNCING_PROP:
        return
    try:
        from . import gpu_draw
        gpu_draw.invalidate_gpu_caches()
        if hasattr(gpu_draw, "sync_draw_handlers"):
            gpu_draw.sync_draw_handlers(context)
    except Exception:
        pass
    try:
        ctx = context if context is not None else bpy.context
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm:
            for win in wm.windows:
                if win.screen:
                    for area in win.screen.areas:
                        if area.type in ('VIEW_3D', 'PROPERTIES'):
                            area.tag_redraw()
    except Exception:
        pass


def _make_prop_updater(field_name: str):
    def _cb(self, context):
        _handle_per_prop_field_write(self, context, field_name)
    return _cb


def _make_prop_updater_with_redraw(field_name: str):
    def _cb(self, context):
        _handle_per_prop_field_write(self, context, field_name)
        _tag_redraw_view3d(self, context)
    return _cb


def _on_auto_grind_filter_update(self, context):
    """Live callback when auto-grind filter sliders change: re-runs auto-detect if active object has an Auto-Detected path or is previewing in Edit Mode."""
    global _IN_AUTO_GRIND_UPDATE
    if _IS_SYNCING_PROP or _IN_AUTO_GRIND_UPDATE:
        return
    ctx = context if context is not None else bpy.context
    obj = resolve_active_prop_mesh(ctx)
    if obj is None or getattr(obj, "type", None) != 'MESH':
        _tag_redraw_view3d(self, ctx)
        return

    has_auto_path = any(getattr(gp, "is_auto_set", False) for gp in getattr(obj, "reskate_grind_paths", []))
    is_edit_preview = (getattr(obj, "mode", "") == 'EDIT')
    has_marked_attr = False
    if not has_auto_path and not is_edit_preview:
        try:
            attr = obj.data.attributes.get("reskate_grind")
            if attr and len(attr.data) > 0:
                has_marked_attr = any(item.value > 0 for item in attr.data)
        except Exception:
            pass

    if has_auto_path or is_edit_preview or has_marked_attr:
        _IN_AUTO_GRIND_UPDATE = True
        try:
            if hasattr(bpy.ops, "reskate") and hasattr(bpy.ops.reskate, "autodetect_grind_edges"):
                bpy.ops.reskate.autodetect_grind_edges()
        except Exception:
            pass
        finally:
            _IN_AUTO_GRIND_UPDATE = False

    _tag_redraw_view3d(self, ctx)


def _make_auto_grind_updater(field_name: str):
    def _cb(self, context):
        _handle_per_prop_field_write(self, context, field_name)
        _on_auto_grind_filter_update(self, context)
    return _cb


def _on_col_straighten_param_update(self, context):
    """Live-updates collision edge straighten in Edit Mode if a baseline snapshot is active on the mesh."""
    if _IS_SYNCING_PROP:
        return
    ctx = context if context is not None else bpy.context
    act = getattr(ctx, "active_object", None)
    if act and getattr(act, "type", None) == 'MESH' and getattr(act, "mode", "") == 'EDIT':
        if act.get("reskate_col_edge_baseline_json"):
            try:
                if hasattr(bpy.ops, "reskate") and hasattr(bpy.ops.reskate, "straighten_collision_edges"):
                    bpy.ops.reskate.straighten_collision_edges()
            except Exception:
                pass
    _tag_redraw_view3d(self, ctx)


def _extra_update_2x2m_detail(self, context):
    val = bool(getattr(self, "use_2x2m_detail_shader", False))
    try:
        self.use_native_771_detail_slots = val
    except Exception:
        pass


def _update_use_2x2m_detail_shader(self, context):
    _handle_per_prop_field_write(self, context, "use_2x2m_detail_shader", _extra_update_2x2m_detail)


def _extra_update_native_771_slots(self, context):
    val = bool(getattr(self, "use_native_771_detail_slots", False))
    try:
        self.use_2x2m_detail_shader = val
    except Exception:
        pass


def _update_use_native_771_detail_slots(self, context):
    _handle_per_prop_field_write(self, context, "use_native_771_detail_slots", _extra_update_native_771_slots)


def _extra_update_uv_tiling_mult(self, context):
    scale_val = float(getattr(self, "uv_tiling_mult", 1.0) or 1.0)
    try:
        ctx = context if context is not None else bpy.context
        obj = resolve_active_prop_mesh(ctx) or getattr(ctx, "active_object", None)
        if obj and getattr(obj, "type", None) == 'MESH' and len(obj.material_slots) > 0:
            slot_idx = max(0, min(obj.active_material_index, len(obj.material_slots) - 1))
            mat = obj.material_slots[slot_idx].material
            if mat and mat.use_nodes and mat.node_tree:
                for n in mat.node_tree.nodes:
                    if n.type == "MAPPING" and "Scale" in n.inputs:
                        n.inputs["Scale"].default_value = (scale_val, scale_val, 1.0)
    except Exception:
        pass


def _update_uv_tiling_mult(self, context):
    _handle_per_prop_field_write(self, context, "uv_tiling_mult", _extra_update_uv_tiling_mult)


WEAR_BASIS_ITEMS = [
    (
        'AUTO',
        "Auto (From Material / Surface)",
        "Automatically matches edge wear, crevice grime, and top-down scratches to your material type (Concrete, Metal Rail, Wood, etc.)",
    ),
    (
        'CONCRETE',
        "Concrete / Stone (Chipped Edges & Crevices)",
        "Lightened chipped concrete corners with gritty edge normal bump and darkened cavity crevices",
    ),
    (
        'METAL_RAIL',
        "Metal Rail / Coping (Top-Down Grind Scratches)",
        "Rougher, lighter scratched-up top surface from skateboard truck grinds plus bright bare-metal edge wear",
    ),
    (
        'PAINTED',
        "Painted Surface (Scuffed Bare-Metal Edges)",
        "Wears away paint along sharp edges and top surfaces to reveal lighter, rougher scuffed primer/metal underneath",
    ),
    (
        'WOOD',
        "Wood / Plywood (Worn Corners & Recess Grime)",
        "Lighter worn plywood edges with grain roughness variation and darkened recesses",
    ),
    (
        'ASPHALT',
        "Asphalt / Brick (Weathered Corners & Cavity Dirt)",
        "Coarse weathered corner aggregate and deep crevice dirt",
    ),
]

WEAR_BASIS_PRESET_SLIDERS = {
    "CONCRETE": {"edge": 0.70, "crevice": 0.70, "top": 0.30, "scale": 0.50},
    "METAL_RAIL": {"edge": 0.65, "crevice": 0.45, "top": 0.90, "scale": 0.45},
    "PAINTED": {"edge": 0.85, "crevice": 0.55, "top": 0.75, "scale": 0.45},
    "WOOD": {"edge": 0.65, "crevice": 0.65, "top": 0.40, "scale": 0.50},
    "ASPHALT": {"edge": 0.75, "crevice": 0.80, "top": 0.35, "scale": 0.55},
}


_IN_MAT_TEX_SYNC = False


def _on_sk8_material_texture_slot_changed(self, context):
    """
    Live callback when a user assigns/clears an image texture in 4. Look (`base_color_image`,
    `normal_image`, `roughness_image`, `metallic_image`, `mask_image`, `opacity_image`):
    replaces any starter template procedural node for that slot with the user's image texture
    and re-layers Procedural Wear on top in both the Blender Viewport and compiler!
    """
    global _IN_MAT_TEX_SYNC
    if _IS_SYNCING_PROP or _IN_MAT_TEX_SYNC:
        return
    _IN_MAT_TEX_SYNC = True
    try:
        from . import compiler_bridge
        ctx = context if context is not None else bpy.context
        obj = resolve_active_prop_mesh(ctx) or getattr(ctx, "active_object", None)
        mat = None
        for cand_m in bpy.data.materials:
            if getattr(cand_m, "sk8_material", None) == self or getattr(cand_m, "reskate_mat", None) == self:
                mat = cand_m
                break
        if mat is None and obj is not None and getattr(obj, "type", None) == 'MESH':
            mat = obj.active_material
        if mat is not None and hasattr(compiler_bridge, "sync_user_texture_slots_to_shader_nodes"):
            compiler_bridge.sync_user_texture_slots_to_shader_nodes(obj, mat, source_settings=self)
            compiler_bridge.invalidate_ui_caches()
    except Exception:
        pass
    finally:
        _IN_MAT_TEX_SYNC = False
    _tag_redraw_view3d(self, context)


def _sync_viewport_wear_Callback(prop_self, context):
    try:
        from . import compiler_bridge
        ctx = context if context is not None else bpy.context
        if not compiler_bridge.is_experimental_addon_enabled(ctx):
            return
        obj = resolve_active_prop_mesh(ctx) or getattr(ctx, "active_object", None)
        if obj and getattr(obj, "type", None) == 'MESH':
            compiler_bridge.sync_procedural_wear_to_viewport(obj, prop_self)
            compiler_bridge.invalidate_ui_caches()
    except Exception:
        pass
    _tag_redraw_view3d(prop_self, context)


def _make_wear_updater(field_name: str):
    def _cb(self, context):
        if _IS_SYNCING_PROP:
            return
        _handle_per_prop_field_write(self, context, field_name)
        _sync_viewport_wear_Callback(self, context)
    return _cb


def _extra_update_wear_basis(self, context):
    basis = str(getattr(self, "wear_basis", "AUTO") or "AUTO").upper()
    preset = WEAR_BASIS_PRESET_SLIDERS.get(basis)
    if preset:
        try:
            self.wear_edge_strength = float(preset["edge"])
            self.wear_crevice_strength = float(preset["crevice"])
            self.wear_top_strength = float(preset["top"])
            self.wear_scale = float(preset["scale"])
        except Exception:
            pass


def _update_wear_basis(self, context):
    if _IS_SYNCING_PROP:
        return
    _handle_per_prop_field_write(self, context, "wear_basis", _extra_update_wear_basis)
    _sync_viewport_wear_Callback(self, context)


def _update_inner_cone(self, context):
    if self.inner_cone_angle > self.outer_cone_angle:
        self.outer_cone_angle = self.inner_cone_angle

def _update_outer_cone(self, context):
    if self.outer_cone_angle < self.inner_cone_angle:
        self.inner_cone_angle = self.outer_cone_angle


THUMB_BG_PRESET_ITEMS = [
    ('SUNNY_DAY', "Sunny Day", "Outdoor sunny sky with directional sun light and ground shadow plane"),
    ('STUDIO', "Studio Lighting", "Three-point studio lighting with studio floor"),
    ('TRANSPARENT', "Transparent", "Transparent alpha PNG background with no floor"),
    ('FLAT_COLOR', "Flat Background Color", "Solid RGB background color with clean even lighting"),
]

_THUMB_BG_PRESET_MAP = {
    'SUNNY_DAY': ((0.52, 0.68, 0.88), 'SUNNY_DAY', True, 'OUTDOOR_SUN'),
    'STUDIO': ((0.16, 0.18, 0.22), 'STUDIO_DARK', True, 'STUDIO_3PT'),
    'TRANSPARENT': ((0.14, 0.16, 0.20), 'TRANSPARENT', False, 'STUDIO_3PT'),
    'FLAT_COLOR': ((0.18, 0.20, 0.24), 'STUDIO_DARK', False, 'STUDIO_3PT'),
}


def _sync_live_thumb_rig_if_active(prop_self, context):
    """Updates the live thumbnail preview rig (sun angle/rotation, floor color, exposure, key/fill) only if camera preview is active."""
    try:
        from . import operators
        if hasattr(operators, "sync_live_thumbnail_preview_rig"):
            operators.sync_live_thumbnail_preview_rig(context or bpy.context, force_activate=False)
    except Exception:
        pass
    _tag_redraw_view3d(prop_self, context)


_SANCTUS_ENUM_ITEMS_REF: list[tuple] = []
_SANCTUS_ENUM_ITEMS_HISTORY: list[list[tuple]] = []
_SANCTUS_INTERNED_STRINGS: set[str] = set()


def _intern_carousel_str(val: str) -> str:
    """Interns and permanently retains strings used in dynamic EnumProperty items so Blender C RNA pointers never dangle."""
    import sys
    s = sys.intern(str(val or ""))
    _SANCTUS_INTERNED_STRINGS.add(s)
    return s


def _get_sanctus_carousel_items(self, context):
    """Dynamic EnumProperty items callback for the Sanctus-style thumbnail carousel (`template_icon_view`)."""
    global _SANCTUS_ENUM_ITEMS_REF
    new_items: list[tuple] = []
    try:
        from . import ui_panels
        if hasattr(ui_panels, "get_sanctus_carousel_enum_items"):
            raw_items = list(ui_panels.get_sanctus_carousel_enum_items(self, context))
            for it in raw_items:
                if len(it) >= 5:
                    new_items.append((
                        _intern_carousel_str(it[0]),
                        _intern_carousel_str(it[1]),
                        _intern_carousel_str(it[2]),
                        it[3],
                        int(it[4]),
                    ))
                elif len(it) == 4:
                    new_items.append((
                        _intern_carousel_str(it[0]),
                        _intern_carousel_str(it[1]),
                        _intern_carousel_str(it[2]),
                        it[3],
                    ))
                elif len(it) >= 3:
                    new_items.append((
                        _intern_carousel_str(it[0]),
                        _intern_carousel_str(it[1]),
                        _intern_carousel_str(it[2]),
                    ))
    except Exception:
        pass
    if not new_items:
        new_items = [(
            _intern_carousel_str("__NONE__"),
            _intern_carousel_str("No Prop Selected"),
            _intern_carousel_str("Select a 3D mesh prop in the viewport"),
            'MESH_DATA',
            0,
        )]
    if new_items != _SANCTUS_ENUM_ITEMS_REF:
        _SANCTUS_ENUM_ITEMS_REF = new_items
        _SANCTUS_ENUM_ITEMS_HISTORY.append(new_items)
        if len(_SANCTUS_ENUM_ITEMS_HISTORY) > 32:
            del _SANCTUS_ENUM_ITEMS_HISTORY[:-32]
    return _SANCTUS_ENUM_ITEMS_REF


def _get_sanctus_carousel_index(self) -> int:
    ctx = bpy.context
    items = _SANCTUS_ENUM_ITEMS_REF if _SANCTUS_ENUM_ITEMS_REF else _get_sanctus_carousel_items(self, ctx)
    obj = resolve_active_prop_mesh(ctx) if ctx else None
    if obj is not None:
        for i, it in enumerate(items):
            if it[0] == obj.name:
                return int(it[4]) if len(it) >= 5 else i
        # If active object wasn't in cached list, refresh once
        items = _get_sanctus_carousel_items(self, ctx)
        for i, it in enumerate(items):
            if it[0] == obj.name:
                return int(it[4]) if len(it) >= 5 else i
    return 0


def _set_sanctus_carousel_index(self, value: int):
    ctx = bpy.context
    if ctx is None:
        return
    items = _SANCTUS_ENUM_ITEMS_REF if _SANCTUS_ENUM_ITEMS_REF else _get_sanctus_carousel_items(self, ctx)
    chosen_name = ""
    for i, it in enumerate(items):
        idx_val = int(it[4]) if len(it) >= 5 else i
        if idx_val == int(value):
            chosen_name = str(it[0])
            break
    if not chosen_name or chosen_name == "__NONE__":
        return
    target_obj = bpy.data.objects.get(chosen_name)
    if target_obj is None or getattr(target_obj, "type", None) != 'MESH':
        return
    active = resolve_active_prop_mesh(ctx)
    synced_name = str(self.get("_active_synced_obj_name", "") or "")
    if active and active.type == 'MESH' and active != target_obj and synced_name == active.name:
        save_prop_settings_to_object(self, active)
    try:
        target_obj.select_set(True)
        ctx.view_layer.objects.active = target_obj
    except Exception:
        pass
    load_prop_settings_from_object(self, target_obj)
    try:
        from . import operators, ui_panels
        if hasattr(operators, "sync_thumbnail_camera_on_prop_switch"):
            operators.sync_thumbnail_camera_on_prop_switch(ctx, target_obj)
        if hasattr(ui_panels, "on_active_prop_switched"):
            ui_panels.on_active_prop_switched()
    except Exception:
        pass
    _tag_redraw_view3d(self, ctx)


def _extra_update_thumb_bg_preset(self, context):
    key = str(super(ReSkatePropSettings, self).__getattribute__("thumb_bg_preset") or "SUNNY_DAY").upper()
    rgb, bg_style, use_floor, light_mode = _THUMB_BG_PRESET_MAP.get(key, _THUMB_BG_PRESET_MAP['SUNNY_DAY'])
    try:
        if key != 'FLAT_COLOR':
            self.thumb_bg_color = rgb
        self.thumb_bg_style = bg_style
        self.thumb_use_cyclorama_floor = use_floor
        self.thumb_lighting_mode = light_mode
    except Exception:
        pass


def _update_thumb_bg_preset(self, context):
    if _IS_SYNCING_PROP:
        return
    _handle_per_prop_field_write(self, context, "thumb_bg_preset", _extra_update_thumb_bg_preset)
    _sync_live_thumb_rig_if_active(self, context)


def _make_thumb_live_updater(field_name: str):
    def _cb(self, context):
        if _IS_SYNCING_PROP:
            return
        _handle_per_prop_field_write(self, context, field_name)
        _sync_live_thumb_rig_if_active(self, context)
    return _cb

# ---------------------------------------------------------------------------
# Property Groups
# ---------------------------------------------------------------------------

def _update_grind_path_item(self, context):
    """Updates edge attribute tags and invalidates GPU overlay caches when a Grind Path item's type/radius/enabled changes."""
    try:
        ctx = context if context is not None else bpy.context
        obj = resolve_active_prop_mesh(ctx) or getattr(ctx, "active_object", None)
        if obj and getattr(obj, "type", None) == 'MESH':
            v_ids = [int(x) for x in (getattr(self, "vertex_indices_csv", "") or "").split(",") if x.strip().isdigit()]
            if len(v_ids) >= 2:
                g_type = getattr(self, "grind_type", "LEDGE")
                tag_val = 0 if not getattr(self, "enabled", True) else (2 if g_type == 'ROUND_RAIL' else (3 if g_type == 'COPING' else 1))
                consec = {(min(v_ids[k], v_ids[k + 1]), max(v_ids[k], v_ids[k + 1])) for k in range(len(v_ids) - 1)}
                v_set = set(v_ids)
                me = obj.data
                if obj.mode == 'EDIT':
                    import bmesh
                    bm = bmesh.from_edit_mesh(me)
                    layer = bm.edges.layers.int.get("reskate_grind") or bm.edges.layers.int.new("reskate_grind")
                    for e in bm.edges:
                        pair = (min(e.verts[0].index, e.verts[1].index), max(e.verts[0].index, e.verts[1].index))
                        if pair in consec or (e.verts[0].index in v_set and e.verts[1].index in v_set):
                            e[layer] = tag_val
                    bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
                else:
                    attr = me.attributes.get("reskate_grind") or me.attributes.new(name="reskate_grind", type='INT', domain='EDGE')
                    for e in me.edges:
                        pair = (min(e.vertices[0], e.vertices[1]), max(e.vertices[0], e.vertices[1]))
                        if pair in consec or (e.vertices[0] in v_set and e.vertices[1] in v_set):
                            if 0 <= e.index < len(attr.data):
                                attr.data[e.index].value = tag_val
    except Exception:
        pass
    _tag_redraw_view3d(self, context)


class ReSkateGrindPathItem(bpy.types.PropertyGroup):
    """Named vertex-path grind entry stored on a prop mesh (manual or auto-detected set)."""
    name: StringProperty(
        name="Grind Path Name", default="Grind Path",
        description="Name of this grind path")
    grind_type: EnumProperty(
        name="Type", items=GRIND_GEOMETRY_TYPES, default='LEDGE',
        update=_update_grind_path_item,
        description="Hard Edge/Ledge (Box), Round Rail (Cylinder), or Pool/Ramp Coping (Cylinder)")
    radius: FloatProperty(
        name="Rail Radius", default=0.032, min=0.010, max=0.100, unit='LENGTH', precision=3,
        update=_update_grind_path_item,
        description="Radius of the round rail preview guide in meters (default 3.2 cm)")
    surface: EnumProperty(
        name="Surface", items=GRIND_SURFACE_OVERRIDES, default='Metal_Solid_Grindable',
        update=_update_grind_path_item,
        description="Surface sound and friction for this grind path")
    vertex_indices_csv: StringProperty(
        name="Vertex Indices", default="",
        description="Ordered comma-separated vertex indices forming this grind path")
    enabled: BoolProperty(
        name="Enabled", default=True,
        update=_update_grind_path_item,
        description="Include this grind path when compiling the prop")
    is_auto_set: BoolProperty(
        name="Is Auto-Detected Set", default=False,
        description="True when this entry was created by Auto-Detect Grind Edges")
    auto_crease_deg: FloatProperty(name="Crease Angle (°)", default=42.0, min=10.0, max=89.0)
    auto_angle_deg: FloatProperty(name="Crease Angle (°)", default=38.0, min=10.0, max=89.0)
    auto_min_len: FloatProperty(name="Min Edge Length (m)", default=0.15, min=0.01, max=5.0)
    auto_min_length: FloatProperty(name="Min Edge Length (m)", default=0.25, min=0.01, max=5.0)
    auto_ignore_bottom_pct: FloatProperty(name="Ignore Bottom (%)", default=8.0, min=0.0, max=50.0)
    auto_open_above: BoolProperty(name="Open Above", default=True)
    auto_clearance_height: FloatProperty(name="Open Above Clearance (m)", default=0.35, min=0.05, max=2.0)
    auto_include_concave: BoolProperty(name="Include Concave Corners", default=False)
    auto_only_selected: BoolProperty(name="Only Selected Area", default=False)


class ReSkateLinkedLightItem(bpy.types.PropertyGroup):
    """Explicitly linked Blender Spot/Point light on a prop."""
    light_obj: PointerProperty(
        name="Light Object",
        type=bpy.types.Object,
        poll=_poll_light_object,
        description="Pick a Spot or Point light in the scene to attach to this prop")
    enabled: BoolProperty(
        name="Enabled", default=True,
        description="Export this light with the prop")


class ReSkateMaterialSurfaceItem(bpy.types.PropertyGroup):
    """Optional per-material-slot surface override."""
    material_name: StringProperty(name="Material Slot", default="")
    surface: EnumProperty(
        name="Surface Override",
        items=MATERIAL_SLOT_SURFACE_ITEMS,
        default='DEFAULT',
        update=_update_material_slot_surface,
        description="Override the physical riding surface for faces using this material slot (auto-syncs with 4. Look Contact Audio)")
    prevent_wall_snap: BoolProperty(
        name="Prevent wall-ride snapping",
        default=False,
        update=_update_material_slot_surface,
        description="Use this if your skater turns sideways toward this prop when jumping at it (applies Frostbite Do-Not-Align behavior)")
    feel_preset: EnumProperty(
        name="Feel",
        items=SURFACE_FEEL_PRESETS,
        default='NORMAL',
        update=_update_material_slot_feel,
        description="Quickly set the riding feel for this material slot")


class ReSkatePackQueueItem(bpy.types.PropertyGroup):
    """Single queued prop for multi-prop batch mod pack compilation."""
    name: StringProperty(name="Name", default="Prop")
    prop_key: StringProperty(name="Prop Key", default="")
    display_name: StringProperty(name="Display Name", default="")
    category: StringProperty(name="Category", default="Custom Props")
    collision_mode: StringProperty(name="Collision Mode", default="SAME_AS_MESH")
    surface_name: StringProperty(name="Surface Name", default="Concrete (Smooth)")
    surface_code: IntProperty(name="Surface Code", default=96)
    tri_count: IntProperty(name="Triangles", default=0)
    poly_count: IntProperty(name="Polygons", default=0)
    has_col_proxy: BoolProperty(name="Has Custom Collision", default=False)
    has_lights: BoolProperty(name="Has Lights", default=False)
    is_enabled: BoolProperty(name="Include in Pack", default=True, description="Include this prop when compiling all marked props")
    enabled: BoolProperty(name="Include in Pack", default=True, description="Include this prop when compiling all marked props")
    prop_key_slug: StringProperty(name="Prop Key Slug", default="")
    primary_surface: StringProperty(name="Primary Surface", default="concrete_smooth")
    staging_dir: StringProperty(name="Staging Dir", default="")
    glb_path: StringProperty(name="GLB Path", default="")
    col_glb_path: StringProperty(name="Collision GLB Path", default="")
    texture_path: StringProperty(name="Texture Path", default="")
    thumb_256_path: StringProperty(name="Thumbnail 256 Path", default="")
    thumb_768_path: StringProperty(name="Thumbnail 768 Path", default="")
    snapshot_hash: StringProperty(
        name="Scene Fingerprint",
        default="",
        description="Fingerprint of the scene object's mesh, materials, UVs, collision, and lights when added/synced to the pack",
    )


UI_MODE_ITEMS = [
    (
        'SIMPLE',
        "Simple",
        "Streamlined beginner mode: automatic 1:1 per-shader collision and 8-hull Quick Drop placement",
    ),
    (
        'ADVANCED',
        "Advanced",
        "Pro controls: custom collision meshes, placement proxy modes, UV projection, 2x2m grain, procedural wear, and light tuning",
    ),
    (
        'EXPERIMENTAL',
        "Experimental",
        "Experimental & R&D tools: Raw 279 Frostbite MaterialGrid catalog, manual grind angle/edge overrides, VFX/Audio emitters, and GPU compiler",
    ),
]

SHADER_SURFACE_ROLE_ITEMS = list(PRIMARY_SURFACE_PRESETS) + [
    (
        'DECAL_OVERLAY',
        "Decal / Sticker Overlay (No Skater Collision, Cutout 0x273)",
        "Compiles with Frostbite 0x273 Alpha Cutout shader and excludes this shader's faces from skater collision",
    ),
    (
        'ROUND_METAL_RAIL',
        "Round Metal Rail / Coping (Grindable Pipe)",
        "Metal Rail audio (2976) + round rail surface analysis (Slot 71) for cylindrical pipes and coping",
    ),
]


def _on_ui_mode_changed(self, context):
    """Enforces safe defaults whenever switching into Simple Mode and refreshes the N-panel."""
    if context is None:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    mode = getattr(scene, "sk8_ui_mode", "SIMPLE")
    prop = getattr(scene, "reskate_prop", None)
    if prop is not None:
        try:
            if getattr(prop, "ui_mode", mode) != mode:
                prop["ui_mode"] = mode
        except Exception:
            pass
        if mode == 'SIMPLE':
            try:
                if getattr(prop, "collision_mode", "SAME_AS_MESH") != 'SAME_AS_MESH':
                    prop.collision_mode = 'SAME_AS_MESH'
                if getattr(prop, "placement_proxy_mode", "MATCH_COLLISION") != 'MATCH_COLLISION':
                    prop.placement_proxy_mode = 'MATCH_COLLISION'
                if getattr(prop, "thumb_lighting_mode", "STUDIO_3PT") != 'STUDIO_3PT':
                    prop.thumb_lighting_mode = 'STUDIO_3PT'
                # Turn off viewport geometry overlays when entering Simple mode so green outlines never interfere with mesh editing
                for ov_attr in (
                    "draw_grind_overlay",
                    "draw_col_overlay",
                    "show_collision_overlay",
                    "show_placement_proxy_overlay",
                    "draw_face_surface_overlay",
                    "show_alignment_overlay",
                ):
                    if getattr(prop, ov_attr, False):
                        setattr(prop, ov_attr, False)
            except Exception:
                pass
    _tag_redraw_view3d(self, context)


def _on_prop_ui_mode_changed(self, context):
    if context and getattr(context, "scene", None) and getattr(context.scene, "sk8_ui_mode", None) != self.ui_mode:
        context.scene.sk8_ui_mode = self.ui_mode
    else:
        _on_ui_mode_changed(self, context)


def _on_surface_role_preset_changed(self, context):
    """Syncs the unified Card 3 'Surface & Sound' dropdown with Decal Overlay, Round Rail, and Per-Material Surface tables."""
    global _IS_SYNCING_PROP
    if _IS_SYNCING_PROP:
        return
    role = getattr(self, "surface_role_preset", "concrete_smooth") or "concrete_smooth"
    owner_mat = getattr(self, "id_data", None)
    if not isinstance(owner_mat, bpy.types.Material):
        owner_mat = None

    _IS_SYNCING_PROP = True
    try:
        if role == 'DECAL_OVERLAY':
            self.is_decal_slot = True
            if owner_mat is not None:
                owner_mat["reskate_decal_slot_initialized"] = True
                owner_mat["reskate_user_set_surface_role"] = "DECAL_OVERLAY"
        else:
            if getattr(self, "is_decal_slot", False):
                self.is_decal_slot = False
            if owner_mat is not None:
                owner_mat["reskate_decal_slot_initialized"] = True
                owner_mat["reskate_user_set_surface_role"] = str(role)

            target_surf = "DEFAULT"
            packed_code = 96
            if role == 'ROUND_METAL_RAIL':
                target_surf = 'metal_rail'
                packed_code = 2976
            elif role == 'AUTO_FROM_NAME':
                target_surf = 'DEFAULT'
                packed_code = 0
            else:
                target_surf = str(role)
                packed_code = int(PRIMARY_SURFACE_CODE_MAP.get(target_surf, 96))

            if owner_mat is not None:
                if packed_code > 0:
                    owner_mat["sk8_collision_material_packed"] = int(packed_code)
                    owner_mat["sk8_collision_material"] = f"material_{packed_code:04d}"
                else:
                    for k in ("sk8_collision_material_packed", "sk8_collision_material"):
                        if k in owner_mat:
                            del owner_mat[k]
                # Sync both reskate_mat and sk8_material pointers on owner_mat
                for ptr_name in ("reskate_mat", "sk8_material"):
                    other = getattr(owner_mat, ptr_name, None)
                    if other is not None and other is not self:
                        try:
                            other.surface_role_preset = role
                            other.is_decal_slot = False
                        except Exception:
                            pass

            # Sync active mesh object's material slot collection and single-material primary_surface
            obj = resolve_active_prop_mesh(context)
            if obj is not None and getattr(obj, "type", None) == 'MESH':
                if hasattr(obj, "reskate_material_surfaces"):
                    while len(obj.reskate_material_surfaces) < len(obj.material_slots):
                        it = obj.reskate_material_surfaces.add()
                        idx_new = len(obj.reskate_material_surfaces) - 1
                        ms_new = obj.material_slots[idx_new].material
                        it.material_name = ms_new.name if ms_new else f"Slot {idx_new + 1}"
                        it.surface = 'DEFAULT'
                    for s_idx, slot in enumerate(obj.material_slots):
                        if slot.material == owner_mat and s_idx < len(obj.reskate_material_surfaces):
                            obj.reskate_material_surfaces[s_idx].material_name = owner_mat.name if owner_mat else f"Slot {s_idx + 1}"
                            obj.reskate_material_surfaces[s_idx].surface = target_surf

                prop = getattr(getattr(context, "scene", None), "reskate_prop", None) if context else None
                if role == 'ROUND_METAL_RAIL' and prop is not None:
                    prop.round_rail = True
                    if len(obj.material_slots) <= 1:
                        prop.primary_surface = 'metal_rail'
                        prop.surface_material_code = 2976
                elif target_surf != 'DEFAULT' and prop is not None and len(obj.material_slots) <= 1:
                    if target_surf in PRIMARY_SURFACE_CODE_MAP:
                        prop.primary_surface = target_surf
                        prop.surface_material_code = int(PRIMARY_SURFACE_CODE_MAP[target_surf])
    finally:
        _IS_SYNCING_PROP = False

    _on_sk8_material_texture_slot_changed(self, context)


class ReSkateSk8MaterialSettings(bpy.types.PropertyGroup):
    """Fallback Material > Skate Map (`mat.sk8_material`) settings when sk8_map_export is not loaded."""
    invisible: BoolProperty(
        name="Invisible (Collision Only)", default=False,
        description="Draw nothing in game: faces with this material keep only their collision",
    )
    collision_material: EnumProperty(
        name="Base Surface / Audio Donor",
        items=FROSTBITE_101_SURFACES,
        default='material_0096',
        description="Shipped surface used as the starting material and donor for sound events and physics",
    )
    surface: EnumProperty(
        name="Surface",
        items=[
            ("default", "Concrete (default)", "Standard hard skateable surface"),
            ("concrete", "Concrete", "Skateable concrete"),
            ("wood", "Wood", "Skatepark plywood / ramps"),
            ("metal", "Metal", "Rails, coping, sheet metal"),
            ("asphalt", "Asphalt", "Street / road"),
            ("brick", "Brick", "Brick / pavers"),
            ("tile", "Tile", "Smooth tile / marble"),
            ("grass", "Grass", "Slow, non-grindable"),
            ("dirt", "Dirt", "Slow, rough"),
            ("sand", "Sand", "Very slow"),
            ("glass", "Glass", "Smooth, hard"),
        ],
        default="default",
    )
    srgb: BoolProperty(
        name="sRGB", default=True,
        description="Colour texture; turn off for masks and data maps",
    )
    alpha: EnumProperty(
        name="Alpha", default="auto",
        items=[
            ("auto", "Auto", "Detect from the image"),
            ("opaque", "Opaque", "Ignore alpha (BC1)"),
            ("mask", "Cutout", "1-bit alpha (BC1A)"),
            ("blend", "Blend", "Full 8-bit alpha (BC3)"),
        ],
    )
    domain: EnumProperty(
        name="Shader Override", default="surface",
        items=[
            ("surface", "Auto", "Keep the current shader selection based on transparency", 0, 0),
            ("decal", "Decal", "Use Studio's current blended decal shader", 0, 1),
            ("foliage", "Foliage", "Use the Kentia palm Cards material and alpha-tested shadows", 0, 2),
            ("ocean", "Ocean", "Use native BAM ocean water, including its wave and reflection textures", 0, 4),
            ("glass_opaque", "Glass (Reflective)", "Use the native reflective window-glass material; opaque", 0, 6),
        ],
    )
    alpha_source: EnumProperty(
        name="Alpha Source", default="auto",
        items=[
            ("auto", "Auto", "Use an explicit opacity image when set, otherwise Base Color alpha"),
            ("base_color_texture", "Base Color Alpha", "Reuse the Base Color texture alpha channel"),
            ("opacity_texture", "Opacity Texture", "Use the explicit opacity image red channel"),
            ("constant", "Constant", "Do not sample texture alpha"),
        ],
    )
    alpha_cutoff: FloatProperty(
        name="Alpha Cutoff", default=0.5, min=0.0, max=1.0,
        description="Cutout threshold used by masked decals and foliage",
    )
    cast_shadows: BoolProperty(name="Cast Shadows", default=True)
    double_sided: BoolProperty(name="Double Sided", default=False)
    transparent_shadow: BoolProperty(
        name="Transparent Shadows", default=False,
        description="Allow the authored alpha mask to shape native shadows",
    )
    base_color_image: PointerProperty(
        name="Base Color Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Override the native donor's base color; replaces Starter Template procedural noise while keeping Procedural Wear on top",
    )
    roughness_image: PointerProperty(
        name="Roughness Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Converted to smoothness in MSK green; packed MSK takes priority",
    )
    metallic_image: PointerProperty(
        name="Metallic Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Packed into MSK red; packed MSK takes priority",
    )
    roughness_channel: EnumProperty(
        name="Channel", default="Color",
        update=_on_sk8_material_texture_slot_changed,
        items=[(name, name, "Source channel") for name in ("Color", "Red", "Green", "Blue", "Alpha")],
    )
    roughness_map_mix: FloatProperty(
        name="Roughness Map Mix", default=1.0, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        description="Blends between the base Roughness slider (0.0) and the Roughness Texture map (1.0)",
    )
    metallic_channel: EnumProperty(
        name="Channel", default="Color",
        update=_on_sk8_material_texture_slot_changed,
        items=[(name, name, "Source channel") for name in ("Color", "Red", "Green", "Blue", "Alpha")],
    )
    metallic_map_mix: FloatProperty(
        name="Metallic Map Mix", default=1.0, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        description="Blends between the base Metallic slider (0.0) and the Metallic Texture map (1.0)",
    )
    normal_image: PointerProperty(
        name="Normal Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Optional tangent-space normal image (replaces Starter Template procedural bump while keeping Procedural Edge Wear bump on top)",
    )
    mask_image: PointerProperty(
        name="Mask Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Native packed MSK; used unchanged and overrides Roughness/Metallic inputs",
    )
    opacity_image: PointerProperty(
        name="Opacity Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Optional red-channel opacity image; leave empty to reuse Base Color alpha",
    )
    use_custom_texture_overrides: BoolProperty(
        name="Use Custom Texture Overrides",
        default=False,
        description="Enable to pick manual texture overrides instead of auto-syncing from the Blender Principled BSDF shader (and Separate Color RGB nodes)",
    )
    emission_image: PointerProperty(
        name="Emission Texture", type=bpy.types.Image,
        update=_on_sk8_material_texture_slot_changed,
        description="Emissive glow texture (auto-synced from Principled BSDF Emission input)",
    )
    emission_color: FloatVectorProperty(
        name="Emission Color", size=3, default=(0.0, 0.0, 0.0), min=0.0, max=1.0, subtype='COLOR',
        description="Emissive glow color (auto-synced from Principled BSDF Emission Color)",
    )
    emission_strength: FloatProperty(
        name="Emission Strength", default=0.0, min=0.0, max=100.0, precision=2,
        description="Emissive glow strength (0 = off; auto-synced from Principled BSDF Emission Strength)",
    )
    override_contact_audio: BoolProperty(
        name="Override Contact Audio Separately",
        default=False,
        description="Enable to choose a different contact audio surface than this material's physical collision surface from 2. Collision",
    )
    is_decal_slot: BoolProperty(
        name="Treat Material as Decal Overlay",
        default=False,
        update=_on_sk8_material_texture_slot_changed,
        description="Enable on a sticker, wax, or graffiti material slot on a multi-material mesh to composite its transparent alpha cleanly over the underlying base material without making the prop invisible",
    )
    decal_shader_mode: EnumProperty(
        name="Decal Shader Mode",
        items=DECAL_SHADER_MODE_ITEMS,
        default='CUTOUT_273',
        update=_on_sk8_material_texture_slot_changed,
        description="Choose Standard Alpha Cutout (0x273 Method C: crisp cutout + roughness + normal) or Metallic Decal / Street Plate (0x771 Full PBR: supports metallic storm drains, manhole covers, and metal signs)",
    )
    decal_alpha_mode: EnumProperty(
        name="Decal Alpha Source",
        items=[
            (
                'AUTO',
                "Auto (Image Alpha / Key Black BG)",
                "Uses the Base Color PNG alpha channel if present; automatically un-premultiplies dark fringes or keys out black backgrounds if opaque",
            ),
            (
                'IMAGE_ALPHA',
                "Base Color PNG Alpha Channel",
                "Uses the RGBA alpha channel directly from the Base Color texture image even if the Principled BSDF Alpha socket is not linked",
            ),
            (
                'BSDF_ALPHA',
                "Shader Alpha Socket / Opacity Map",
                "Evaluates the Principled BSDF Alpha socket or Opacity texture slot",
            ),
            (
                'LUMINANCE_BLACK',
                "Key Out Black Background",
                "Treats dark/black background pixels as transparent",
            ),
            (
                'LUMINANCE_WHITE',
                "Key Out White Background",
                "Treats bright/white background pixels as transparent",
            ),
        ],
        default='AUTO',
        update=_on_sk8_material_texture_slot_changed,
        description="Controls how transparency is extracted when 'Treat Material as Decal Overlay' is enabled",
    )
    surface_role_preset: EnumProperty(
        name="Surface & Sound",
        items=SHADER_SURFACE_ROLE_ITEMS,
        default='concrete_smooth',
        update=_on_surface_role_preset_changed,
        description="Choose the riding surface sound, physics feel, or special shader role (Decal Overlay / Round Metal Pipe) for this shader slot",
    )
    show_texture_slots: BoolProperty(
        name="View / Adjust Shader Textures & Bake",
        default=False,
        description="Expand texture map slots (Base Color, Normal, Roughness, Metallic), Procedural Material Bake, and Normal Green Channel Flip for this shader",
    )
    flip_normal_green: BoolProperty(
        name="Flip Green Channel (DirectX <-> OpenGL)",
        default=False,
        update=_on_sk8_material_texture_slot_changed,
        description="Inverts the Y (Green) channel of this shader's Normal map on export (converts DirectX normal maps to OpenGL or vice versa)",
    )


class ReSkateLightSettings(bpy.types.PropertyGroup):
    """Legacy per-light property group kept for backwards compatibility; Light step reads directly from light_obj.data."""
    light_type: EnumProperty(name="Light Type", items=LIGHT_TYPES, default='SPOT')
    inner_cone_angle: FloatProperty(
        name="Inner Cone Angle", default=math.radians(30.0),
        min=math.radians(5.0), max=math.radians(90.0),
        subtype='ANGLE', unit='ROTATION', update=_update_inner_cone)
    outer_cone_angle: FloatProperty(
        name="Outer Cone Angle", default=math.radians(50.0),
        min=math.radians(10.0), max=math.radians(90.0),
        subtype='ANGLE', unit='ROTATION', update=_update_outer_cone)
    attenuation_radius: FloatProperty(
        name="Attenuation Radius", default=30.0,
        min=1.0, max=100.0, unit='LENGTH', precision=1)
    light_intensity: FloatProperty(
        name="Intensity", default=51.83, min=0.0, max=1000.0, precision=2)
    light_color: FloatVectorProperty(
        name="Light Color", size=3, default=(1.0, 0.6977, 0.4995), min=0.0, max=1.0, subtype='COLOR')
    cast_shadows: BoolProperty(name="Cast Shadows", default=True)


class ReSkateColPropSettings(bpy.types.PropertyGroup):
    """Per-object collision proxy settings."""
    is_col_proxy: BoolProperty(
        name="Is Custom Collision Object", default=False,
        description="Identifies this mesh as a dedicated custom collision object")
    surface_material: EnumProperty(
        name="Surface Material", items=FROSTBITE_101_SURFACES, default='material_0096',
        description="Assigned collision surface for this collision mesh")
    round_rail: BoolProperty(
        name="Round Rail", default=False,
        description="Grind this object as a smooth cylindrical rail")
    custom_surface_code: IntProperty(
        name="Custom Surface Code", default=0, min=0)
    trim_bottom: FloatProperty(
        name="Bottom-Lip Trim", default=0.00, min=0.00, max=0.40, unit='LENGTH', precision=3)


class ReSkatePropSettings(bpy.types.PropertyGroup):
    """Scene-level prop authoring settings for the 7-step checklist (automatically synced per active mesh object)."""

    def __getattribute__(self, name):
        if name == "custom_placement_proxy_obj":
            name = "custom_placement_proxy_object"
        if not _IS_SYNCING_PROP and name in _PER_PROP_FIELD_NAMES:
            try:
                active_mesh = resolve_active_prop_mesh()
                if active_mesh is not None:
                    synced = super().__getattribute__("get")("_active_synced_obj_name", "")
                    if synced != active_mesh.name:
                        if not sync_active_object_props():
                            # Fallback if inside a read-only UI draw before depsgraph_update_post ran:
                            state = get_or_init_object_prop_state(active_mesh)
                            if name == "custom_collision_object":
                                c_obj = getattr(active_mesh, "reskate_custom_collision_object", None)
                                if c_obj is None:
                                    c_name = state.get("custom_collision_object_name", "")
                                    if c_name and c_name in bpy.data.objects:
                                        c_obj = bpy.data.objects[c_name]
                                return c_obj
                            if name == "custom_placement_proxy_object":
                                p_obj = getattr(active_mesh, "reskate_custom_placement_proxy_object", None)
                                if p_obj is None:
                                    p_name = state.get("custom_placement_proxy_object_name", "")
                                    if p_name and p_name in bpy.data.objects:
                                        p_obj = bpy.data.objects[p_name]
                                return p_obj
                            if name in state:
                                return state[name]
            except Exception:
                pass
        val = super().__getattribute__(name)
        if name == "uv_tiling_mult" and isinstance(val, float):
            return round(val, 6)
        return val

    # -----------------------------------------------------------------------
    # UI Mode (Simple | Advanced | Experimental)
    # -----------------------------------------------------------------------
    ui_mode: EnumProperty(
        name="UI Mode",
        items=UI_MODE_ITEMS,
        default='SIMPLE',
        update=_on_prop_ui_mode_changed,
        description="Switch between Simple (Beginner), Advanced (Pro), and Experimental UI modes",
    )
    simple_wizard_page: IntProperty(
        name="Simple Page",
        default=1,
        min=1,
        max=6,
        description="Current page (1..6) in the Simple Mode Wizard",
    )
    sanctus_carousel_prop: EnumProperty(
        name="Selected Prop",
        items=_get_sanctus_carousel_items,
        get=_get_sanctus_carousel_index,
        set=_set_sanctus_carousel_index,
        description="Click the thumbnail to open the asset selector popup and switch between selected props",
    )
    show_viewport_overlays_box: BoolProperty(
        name="Viewport Overlays",
        default=True,
        description="Expand or collapse the Viewport Overlays twirl-down menu",
    )
    show_scale_references_box: BoolProperty(
        name="Scale References",
        default=False,
        description="Expand or collapse the Scale References twirl-down menu (Skater & Skateboard references)",
    )
    show_pack_membership_overlay: BoolProperty(
        name="Pack Membership Badges",
        default=False,
        update=_tag_redraw_view3d,
        description="Show floating Pack Membership badges in the 3D Viewport over props that belong to a compiled prop pack",
    )
    show_shader_texture_previews: BoolProperty(
        name="Shader Texture Previews",
        default=True,
        description="Expand or collapse the incoming Shader Texture Previews (Base Color, Normal, Roughness, Metallic)",
    )
    show_thumb_lighting_box: BoolProperty(
        name="Lighting & Background",
        default=False,
        description="Expand or collapse the Thumbnail Lighting & Background controls",
    )

    # -----------------------------------------------------------------------
    # Step 1: Setup
    # -----------------------------------------------------------------------
    prop_display_name: StringProperty(
        name="Display Name", default="My Custom Prop",
        update=_update_prop_name,
        description="In-game display name shown in the Quick Drop menu (can be changed freely without breaking saved parks)")
    prop_id: StringProperty(
        name="Prop ID", default="my_custom_prop",
        update=_update_prop_id,
        description="Permanent internal Prop ID used to derive all GUIDs and park save keys. Locked after first set so renaming Display Name never breaks placed props")
    prop_id_locked: BoolProperty(
        name="Custom Prop ID Override", default=False,
        update=_make_prop_updater("prop_id_locked"),
        description="Enable in Advanced only if you want to manually set a custom internal Prop ID instead of auto-syncing from Prop Name")
    prop_category: EnumProperty(
        name="Quick Drop Tab", items=PROP_CATEGORIES, default='CUSTOM_TAB',
        update=_make_prop_updater("prop_category"),
        description="Quick Drop menu category/tab where this prop appears in-game (e.g. bkcustomprops, bkrails, bkledges, bklights, bkaudio)")
    custom_qd_tab: StringProperty(
        name="Quick Drop Tab",
        default=format_qd_tab_display_name(_INITIAL_PATHS.get("author_name") or "CustomProps"),
        update=_update_custom_qd_tab,
        description="Quick Drop category tab name — defaults to your Author Name so your props are sorted by author in Quick Drop, or type any custom tab name ('bk' is added automatically during compilation)",
    )
    prop_family_title: StringProperty(
        name="Object Family / Variant Group",
        default="",
        update=_update_prop_family_title,
        description="Optional ReSkate 2.1.1 Object Family name. Props with the same Family name in the same Quick Drop tab are grouped onto a single Quick Drop tile as selectable variants (e.g. sizes or colors)",
    )
    origin_mode: EnumProperty(
        name="Origin Position", items=ORIGIN_MODES, default='BOTTOM_CENTER',
        update=_make_prop_updater("origin_mode"),
        description="Where Set Origin places the object's pivot point without moving the mesh in the scene")
    show_adv_setup: BoolProperty(
        name="Advanced", default=False,
        description="Show internal asset slug and scale settings")
    prop_key_slug: StringProperty(
        name="Internal Slug", default="",
        update=_update_prop_slug,
        description="Internal asset identifier (derived from Prop ID)")
    scale_mult: FloatProperty(
        name="Export Scale", default=1.0, min=0.001, max=1000.0, step=0.1, precision=3,
        update=_make_prop_updater("scale_mult"),
        description="Uniform scale multiplier applied on export")

    # Kept as hidden properties for legacy script compatibility
    orientation_preset: EnumProperty(name="Orientation", items=ORIENTATION_PRESETS, default='Y_UP')
    generate_lods: BoolProperty(name="Auto-Generate LODs", default=True)
    lod1_ratio: FloatProperty(name="LOD1 Ratio", default=0.50, min=0.05, max=1.0)
    lod2_ratio: FloatProperty(name="LOD2 Ratio", default=0.20, min=0.01, max=1.0)

    # -----------------------------------------------------------------------
    # Step 2: Collision
    # -----------------------------------------------------------------------
    collision_mode: EnumProperty(
        name="Collision Mode", items=COLLISION_MODES, default='SAME_AS_MESH',
        update=_make_prop_updater("collision_mode"),
        description="Choose what geometry the skater physically rides on")
    collision_mode_ui: EnumProperty(
        name="Collision Mode",
        items=COLLISION_MODE_UI_ITEMS,
        get=_get_col_mode_ui,
        set=_set_col_mode_ui,
        description="Choose what geometry the skater physically rides on (Same as visible mesh, Simple Hulls, Custom Collision object, or No Collision)",
    )
    no_collision_placement_mode: EnumProperty(
        name="Placement Collision",
        items=NO_COLLISION_PLACEMENT_ITEMS,
        default='FLAT_PLANE',
        update=_make_prop_updater("no_collision_placement_mode"),
        description="Choose how Quick Drop grabs and places this visual-only prop when No Collision is selected",
    )
    placement_proxy_mode: EnumProperty(
        name="Quick Drop Placement Bounds",
        items=[
            (
                'MATCH_COLLISION',
                "Multi-Hull Structural / Contoured (Recommended)",
                "Automatically decomposes legs, posts, overhangs, and ramps into up to 8 independent placement hulls (_pphys_0..7) so props sit flat on the ground and hook cleanly over ledges",
            ),
            (
                'TIGHT_SIMPLIFIED',
                "Tight Simplified Hull (Contoured)",
                "Simplifies the prop into contoured convex hull(s) (_pphys) for Quick Drop placement",
            ),
            (
                'CUSTOM_OBJECT',
                "Custom Placement Mesh (_PPHYS)",
                "Generate or pick a dedicated <Prop>_PPHYS mesh object to customize the exact Quick Drop placement & grab bounds",
            ),
            (
                'BOUNDING_BOX',
                "Simple 1-Box Bounds (Legacy Box)",
                "Fits a single axis-aligned bounding box to the prop's overall bounds",
            ),
        ],
        default='MATCH_COLLISION',
        update=_update_placement_proxy_mode,
        description="Controls the Quick Drop placement collision (_pphys) used when positioning, hooking, and settling this prop in Quick Drop",
    )
    pphys_target_tris: IntProperty(
        name="Target Placement Tris",
        default=64,
        min=12,
        max=256,
        step=8,
        update=_make_prop_updater_with_redraw("pphys_target_tris"),
        description="Target triangle budget for the hyper-simplified Quick Drop Placement Mesh (_PPHYS)",
    )
    custom_placement_proxy_object: PointerProperty(
        name="Placement Proxy Object (_PPHYS)",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_make_prop_updater_with_redraw("custom_placement_proxy_object"),
        description="Pick any mesh object in the scene (compiled into a 1-hull Havok Convex Polytope) for Quick Drop placement and grab bounds",
    )
    custom_collision_object: PointerProperty(
        name="Collision Object",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_make_prop_updater("custom_collision_object"),
        description="Pick any mesh object in the scene to use as the physical collision")
    hide_visual_during_col_edit: BoolProperty(
        name="Completely Hide Visual Prop While Editing Collision",
        default=False,
        update=_make_prop_updater("hide_visual_during_col_edit"),
        description="When enabled, clicking 'Edit Collision Shape' temporarily hides the visual prop in the viewport (instead of ghosting it to a non-selectable wireframe)",
    )
    primary_surface: EnumProperty(
        name="Default Surface", items=PRIMARY_SURFACE_PRESETS, default='concrete_smooth',
        update=_update_primary_surface,
        description="Default surface sound and riding feel for this prop")
    surface_feel_preset: EnumProperty(
        name="Feel Preset", items=SURFACE_FEEL_PRESETS, default='NORMAL',
        update=_update_surface_feel_preset,
        description="Quickly set the riding feel (Grippy, Normal, Slick, or Bouncy-Off)")
    prevent_wall_snap_default: BoolProperty(
        name="Prevent wall-ride snapping",
        default=False,
        update=_make_prop_updater("prevent_wall_snap_default"),
        description="Use this if your skater turns sideways toward this prop when jumping at it (applies Frostbite Do-Not-Align behavior)")
    surface_category_filter: EnumProperty(
        name="Surface Category", items=SURFACE_CATEGORY_FILTERS, default='ALL',
        description="Filter the surface picker by material family")
    surface_behavior_filter: EnumProperty(
        name="Behavior Filter", items=SURFACE_BEHAVIOR_FILTERS, default='ALL',
        update=_make_prop_updater("surface_behavior_filter"),
        description="Filter the 279 Frostbite MaterialGrid declarations by gameplay behavior (Standard Skateable, Do-Not-Align, No-Grind, Stairs, or Bouncy)")
    surface_search_filter: StringProperty(
        name="Search Surfaces", default="",
        description="Filter surfaces by keyword (e.g. metal, wood, no grind, align, stair)")
    use_full_surface_catalog: BoolProperty(
        name="Browse All 279 Frostbite MaterialGrid Declarations", default=False,
        update=_update_use_full_catalog,
        description="Switch from the curated surface presets to the full 279-entry Frostbite MaterialGrid declaration catalog")
    show_per_material_surfaces: BoolProperty(
        name="Per-Material Surface Overrides", default=True,
        description="Assign different riding surfaces (e.g. wood ramp + metal coping) to individual material slots")
    show_adv_collision: BoolProperty(
        name="Advanced", default=False,
        description="Show full 279 Frostbite MaterialGrid catalog and advanced collision options")
    show_edge_straightener: BoolProperty(
        name="Align / Straighten Collision & Grind Edges",
        default=False,
        description="Expand non-destructive Line/Arc edge snapping tools (auto-shown while in Edit Mode)",
    )
    show_face_surface_painter: BoolProperty(
        name="Per-Face Surface Audio Override (Edit Mode)",
        default=False,
        description="Expand per-face contact sound painter (auto-shown when custom face surfaces exist or in Edit Mode)",
    )
    show_grind_thresholds: BoolProperty(
        name="Angle & Bevel Qualifier Thresholds",
        default=False,
        description="Expand viewport dihedral angle and minimum edge length preview sliders",
    )
    show_starter_templates: BoolProperty(
        name="Starter Material Templates & Blender.org PBR Archive",
        default=False,
        description="Expand built-in 1-click Starter Material Templates and Blender.org PBR Texture Archive downloader",
    )
    show_procedural_wear_foldout: BoolProperty(
        name="Procedural Edge Wear, Crevices & Top Scratches",
        default=False,
        description="Expand procedural edge chipping, cavity grime, and top-down grind scratch controls",
    )
    max_col_tris: IntProperty(
        name="Triangle Limit", default=5000, min=500, max=15000, step=250,
        update=_make_prop_updater("max_col_tris"),
        description="Maximum triangles for 1:1 mesh collision before automatic simplification (default 5,000 tris)")
    trim_bottom: FloatProperty(
        name="Flush Bottom Lip", default=0.00, min=0.00, max=0.40, unit='LENGTH', precision=3,
        update=_make_prop_updater("trim_bottom"),
        description="Trims the bottom edge so ramps sit flush with the ground")
    anti_lip_enabled: BoolProperty(
        name="Flush Ramp Bottom (Anti-Lip)",
        default=False,
        update=_make_prop_updater_with_redraw("anti_lip_enabled"),
        description="Keeps Quick Drop placement flush at Z=0 while sinking Skate Collision lip by -15mm so wheels never catch",
    )
    anti_lip_sink_m: FloatProperty(
        name="Sink Skate Lip Below Ground",
        default=0.015,
        min=0.002,
        max=0.060,
        unit='LENGTH',
        precision=3,
        update=_make_prop_updater_with_redraw("anti_lip_sink_m"),
        description="How far below Z=0 to sink the bottom Skate Collision lip (default 15 mm) while Quick Drop placement stays flush at Z=0",
    )
    col_straighten_mode: EnumProperty(
        name="Mode",
        items=COL_STRAIGHTEN_MODE_ITEMS,
        default='LINE',
        update=_on_col_straighten_param_update,
        description="Mathematical curve or plane used to straighten/flatten selected collision vertices from their original baseline",
    )
    col_straighten_strength: FloatProperty(
        name="Snap Strength",
        default=100.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
        precision=0,
        update=_on_col_straighten_param_update,
        description="Interpolates non-destructively between the original baseline shape (0%) and the snapped line/arc/plane (100%)",
    )
    col_straighten_arc: FloatProperty(
        name="Arc Bulge / Smoothness",
        default=50.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
        precision=0,
        update=_on_col_straighten_param_update,
        description="Curvature bulge for Smooth Arc mode (0% = straight line, 50% = natural quadratic arc through apex, 100% = exaggerated arc)",
    )
    coacd_concavity: FloatProperty(
        name="Concavity", default=0.05, min=0.01, max=0.50, precision=2,
        update=_make_prop_updater("coacd_concavity"),
        description="How tightly simple hulls hug concave shapes (lower = tighter fit)")
    coacd_max_hulls: IntProperty(
        name="Max Hulls", default=8, min=1, max=12,
        update=_make_prop_updater("coacd_max_hulls"),
        description="Maximum number of simple convex hulls (1 to 12)")
    draw_col_overlay: BoolProperty(
        name="Preview Skate Collision", default=False,
        update=_tag_redraw_view3d,
        description="Show color-coded Skate Collision wireframe & sunken anti-lip preview in the 3D viewport")
    show_collision_overlay: BoolProperty(
        name="Preview Skate Collision",
        default=False,
        get=lambda s: bool(getattr(s, "draw_col_overlay", False)),
        set=lambda s, v: setattr(s, "draw_col_overlay", bool(v)),
        update=_tag_redraw_view3d,
        description="Show color-coded Skate Collision wireframe & sunken anti-lip preview in the 3D viewport",
    )
    show_placement_proxy_overlay: BoolProperty(
        name="Preview Placement Proxy (Quick Drop Bounds)",
        default=False,
        update=_tag_redraw_view3d,
        description="Show the Quick Drop Placement Proxy bounding box (ebx32 / res13) and flush Z=min_z ground footprint rectangle in the 3D viewport",
    )
    face_paint_surface: EnumProperty(
        name="Face Surface / Audio",
        items=PRIMARY_SURFACE_PRESETS,
        default='metal_rail',
        update=_make_prop_updater("face_paint_surface"),
        description="Physics & contact audio surface to apply to selected faces in Edit Mode",
    )
    face_paint_prevent_wall_snap: BoolProperty(
        name="Prevent Wall-Snap on Selected Faces",
        default=False,
        update=_make_prop_updater("face_paint_prevent_wall_snap"),
        description="Applies Frostbite Do-Not-Align behavior to the selected faces so the skater will not wall-ride snap onto them",
    )
    draw_face_surface_overlay: BoolProperty(
        name="Show Custom Face Surfaces (GPU)",
        default=False,
        update=_tag_redraw_view3d,
        description="Highlights faces that have custom Physics & Surface Audio assigned with a translucent color-coded GPU overlay and 2D surface labels",
    )
    show_alignment_overlay: BoolProperty(
        name="Show 'Do Not Align' & No-Grind Overlay (GPU)",
        default=False,
        update=_tag_redraw_view3d,
        description="Highlights any surface, face, or object that has 'Do Not Align' (Prevent Wall-Snap) or 'No-Grind' behavior applied in bright amber/red so alignment troubleshooting is instant",
    )

    # Full 279-entry Frostbite MaterialGrid catalog & resolved MaterialDecl.Packed code
    full_surface_catalog: EnumProperty(
        name="Frostbite Surface Catalog", items=_get_filtered_surface_catalog_items,
        update=_update_full_surface,
        description="Complete 279-entry Frostbite MaterialGrid declaration catalog (filtered by Group, Behavior, and Search)")
    surface_material_code: IntProperty(
        name="Surface Code", default=96, min=0,
        update=_make_prop_updater("surface_material_code"))

    # -----------------------------------------------------------------------
    # Step 3: Grind
    # -----------------------------------------------------------------------
    crease_angle: FloatProperty(
        name="Corner Sharpness", default=math.radians(42.0),
        min=math.radians(15.0), max=math.radians(85.0),
        subtype='ANGLE', unit='ROTATION',
        update=_make_auto_grind_updater("crease_angle"),
        description="Angle threshold for auto-detecting sharp or beveled grindable edges (default 42°)")
    auto_min_edge_length: FloatProperty(
        name="Shortest Grindable Edge", default=0.15, min=0.02, max=5.0, unit='LENGTH', precision=2,
        update=_make_auto_grind_updater("auto_min_edge_length"),
        description="Ignore tiny bevels or details shorter than this length")
    auto_ignore_bottom_pct: FloatProperty(
        name="Ignore Bottom of Prop (%)", default=8.0, min=0.0, max=50.0, precision=1,
        update=_make_auto_grind_updater("auto_ignore_bottom_pct"),
        description="Skip the bottom percentage of the prop's height so ground base edges are never marked")
    auto_open_above: BoolProperty(
        name="Must Be Open Above", default=True,
        update=_make_auto_grind_updater("auto_open_above"),
        description="Require clear space above the edge so inner wall corners aren't marked, while keeping lower bars of multi-bar rails")
    auto_clearance_height: FloatProperty(
        name="Clearance Height Above Edge", default=0.35, min=0.05, max=2.0, unit='LENGTH', precision=2,
        update=_make_auto_grind_updater("auto_clearance_height"),
        description="How much clear space above the edge is required (default 0.35 m)")
    auto_include_concave: BoolProperty(
        name="Include Inside Corners", default=False,
        update=_make_auto_grind_updater("auto_include_concave"),
        description="Allow concave/inside corners to be marked (off by default)")
    auto_only_selected: BoolProperty(
        name="Only Check Selected Area", default=False,
        update=_make_auto_grind_updater("auto_only_selected"),
        description="When in Edit Mode with geometry selected, only detect grind edges inside the selection")
    draw_grind_overlay: BoolProperty(
        name="Show Grindable Edge Visualizer", default=False,
        update=_tag_redraw_view3d,
        description="Highlight edges that meet Skate's dihedral angle & bevel criteria for grinding in the 3D viewport")
    draw_grind_shape_overlay: BoolProperty(
        name="Show 3D Shape Overlay (Deprecated)", default=False,
        update=_tag_redraw_view3d,
        description="Deprecated: 3D faux box/cylinder overlays are disabled so only exact edge lines are highlighted")
    show_grind_floating_badge: BoolProperty(
        name="Show Compact Floating Grind Indicator", default=False,
        update=_tag_redraw_view3d,
        description="Show a small floating 2D badge in the 3D viewport summarizing grindable Ledges, Coping, and Rails when the prop is selected")
    show_adv_grind: BoolProperty(
        name="Advanced", default=False,
        description="Show auto-detect filters and grind profile settings")
    grind_type: EnumProperty(
        name="Default Grind Type", items=GRIND_GEOMETRY_TYPES, default='LEDGE',
        update=_make_prop_updater("grind_type"),
        description="Default grind shape for new paths and marked edges")
    round_rail: BoolProperty(
        name="Round Rail", default=False,
        update=_make_prop_updater_with_redraw("round_rail"),
        description="Treat auto-detected grind edges as a cylindrical round rail")
    grind_tube_radius: FloatProperty(
        name="Rail Radius (Ø = 2×r)", default=0.032, min=0.015, max=0.060, unit='LENGTH', precision=3,
        update=_make_prop_updater_with_redraw("grind_tube_radius"),
        description="Recommended Round Rail cylinder radius (default 3.2 cm radius = Ø 6.4 cm / 2.5 in diameter)")
    show_round_rail_diameter_guide: BoolProperty(
        name="Show Recommended Ø 6.4 cm Round Rail Cylinder Guide",
        default=False,
        update=_tag_redraw_view3d,
        description="Draws a 3D cylindrical diameter gauge (default Ø 6.4 cm / 2.5 in, r = 3.2 cm) around detected Round Rail edges in the viewport so you can verify your rail pipe thickness",
    )
    grind_surface_override: EnumProperty(
        name="Grind Surface", items=GRIND_SURFACE_OVERRIDES, default='Metal_Solid_Grindable',
        update=_make_prop_updater("grind_surface_override"),
        description="Default surface sound and feel for grind edges")

    # -----------------------------------------------------------------------
    # Step 4: Look (Textures)
    # -----------------------------------------------------------------------
    enable_custom_textures: BoolProperty(
        name="Pack & Enable Custom Textures",
        default=True,
        update=_make_prop_updater("enable_custom_textures"),
        description=(
            "Pack the 1024x1024 BC7 texture mipchain into the mod and bind it in ShaderBlockDepot. "
            "Keep enabled so your textures are always packed in the mod and ready for ReSkate"
        ),
    )
    use_blender_org_textures: BoolProperty(
        name="Use Blender.org Textures",
        default=True,
        update=_make_prop_updater_with_redraw("use_blender_org_textures"),
        description=(
            "When enabled, Starter Material Templates download and bind realistic PBR textures "
            "(Base Color, Normal, Roughness) from the Blender.org Texture Archive instead of purely synthetic procedural noise"
        ),
    )
    blender_org_category: EnumProperty(
        name="Archive Category",
        items=BLENDER_ORG_CATEGORIES,
        default='CONCRETE',
        update=_make_prop_updater_with_redraw("blender_org_category"),
        description="Select category from the Blender.org Texture Archive",
    )
    blender_org_concrete_variant: EnumProperty(
        name="Concrete Variant",
        items=BLENDER_ORG_CONCRETE_VARIANTS,
        default='concrete_wall_12',
        update=_make_prop_updater_with_redraw("blender_org_concrete_variant"),
        description="Select Concrete texture variant",
    )
    blender_org_metal_variant: EnumProperty(
        name="Metal Variant",
        items=BLENDER_ORG_METAL_VARIANTS,
        default='metal_plate_05',
        update=_make_prop_updater_with_redraw("blender_org_metal_variant"),
        description="Select Metal texture variant",
    )
    blender_org_wood_variant: EnumProperty(
        name="Wood Variant",
        items=BLENDER_ORG_WOOD_VARIANTS,
        default='wood_simple_01',
        update=_make_prop_updater_with_redraw("blender_org_wood_variant"),
        description="Select Wood texture variant",
    )
    blender_org_marble_variant: EnumProperty(
        name="Marble Variant",
        items=BLENDER_ORG_MARBLE_VARIANTS,
        default='marble_solid_08',
        update=_make_prop_updater_with_redraw("blender_org_marble_variant"),
        description="Select Marble texture variant",
    )
    blender_org_brick_variant: EnumProperty(
        name="Brick Variant",
        items=BLENDER_ORG_BRICK_VARIANTS,
        default='brick_wall_01',
        update=_make_prop_updater_with_redraw("blender_org_brick_variant"),
        description="Select Brick texture variant",
    )
    blender_org_grunge_variant: EnumProperty(
        name="Grunge Variant",
        items=BLENDER_ORG_GRUNGE_VARIANTS,
        default='grunge_scratches_01',
        update=_make_prop_updater_with_redraw("blender_org_grunge_variant"),
        description="Select Grunge texture variant",
    )
    blender_org_decal_variant: EnumProperty(
        name="Decal Variant",
        items=BLENDER_ORG_DECAL_VARIANTS,
        default='decal_warning_robot',
        update=_make_prop_updater_with_redraw("blender_org_decal_variant"),
        description="Select Decal texture variant",
    )
    blender_org_offline_fallback: BoolProperty(
        name="Offline Fallback to Procedural",
        default=True,
        update=_make_prop_updater("blender_org_offline_fallback"),
        description="Automatically fall back to procedural templates if network connection or Blender.org is unavailable",
    )
    staging_dir: StringProperty(
        name="Staging Dir",
        default="",
        description="Custom staging directory override (e.g. for offline testing or custom build output)",
    )

    def get_selected_blender_org_slug(self) -> str:
        """Returns the currently selected Blender.org texture slug based on active category and variant."""
        cat = getattr(self, "blender_org_category", "CONCRETE")
        prop_name = f"blender_org_{cat.lower()}_variant"
        val = getattr(self, prop_name, "")
        if val:
            return val
        cat_info = BLENDER_ORG_CATALOG.get(cat, {})
        return cat_info.get("default", cat_info.get("default_variant", ""))

    look_mode: EnumProperty(
        name="Texture Mode", items=LOOK_MODES, default='AS_IS',
        update=_make_prop_updater("look_mode"),
        description="Use your single 0..1 texture directly as-is, or bake multiple materials/tiled UVs into a new image")
    look_preset: EnumProperty(
        name="Look Preset", items=LOOK_PRESET_ITEMS, default='CUSTOM',
        update=_make_prop_updater("look_preset"),
        description="Quickly set roughness and metallic for common material types")
    show_adv_texture: BoolProperty(
        name="Advanced", default=False,
        description="Show surface roughness, metallic, specular, tint, and bake settings")
    atlas_resolution: EnumProperty(
        name="Bake / Export Resolution", items=ATLAS_RESOLUTIONS, default='2048',
        update=_make_prop_updater("atlas_resolution"),
        description="Resolution when baking or supersampling textures (1024 Standard, 2048 High-Res, 4096 Ultra Supersampled)")
    uv_tiling_mult: FloatProperty(
        name="UV Tiling Scale", default=1.0, min=0.1, max=16.0, step=0.1, precision=2,
        update=_update_uv_tiling_mult,
        description="Live UV Mapping scale multiplier for starter material templates and tiled textures")
    world_scale_tile_size_m: FloatProperty(
        name="Tile Size (m)",
        default=0.50,
        min=0.05,
        max=10.0,
        step=5,
        precision=2,
        unit='LENGTH',
        update=_make_prop_updater("world_scale_tile_size_m"),
        description="Physical world size in meters for 1 full texture repeat (1.0 UV tile) when applying Auto World-Scale Tiling UVs (default 0.50m)",
    )
    world_scale_uv_scope: EnumProperty(
        name="UV Scope",
        items=[
            ('ALL_SLOTS', "All Opaque Material Slots", "Project world-scale tiling UVs onto all opaque faces (automatically preserves joined decal quads)"),
            ('ACTIVE_SLOT', "Active Material Slot Only", "Project world-scale tiling UVs only onto faces using the active material slot"),
        ],
        default='ALL_SLOTS',
        update=_make_prop_updater("world_scale_uv_scope"),
        description="Choose whether Auto World-Scale Tiling UVs applies to all opaque slots or only the active material slot",
    )
    island_margin: FloatProperty(
        name="UV Island Margin", default=0.015, min=0.001, max=0.100, precision=3,
        update=_make_prop_updater("island_margin"),
        description="Padding between UV islands when baking")
    dielectric_roughness: FloatProperty(
        name="Roughness", default=0.45, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        update=_update_dielectric_roughness,
        description="Surface roughness in-game (0 = glossy, 1 = matte; converted to Smoothness = 1 - Roughness)")
    roughness_map_mix: FloatProperty(
        name="Roughness Map Mix", default=1.0, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        update=_update_roughness_map_mix,
        description="How much the Roughness texture map applies over the base Roughness slider (0.0 = base slider only, 1.0 = full roughness map)")
    dielectric_metallic: FloatProperty(
        name="Metallic", default=0.0, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        update=_update_dielectric_metallic,
        description="Surface metallicness in-game (0 = non-metal dielectric, 1 = bare metal)")
    metallic_map_mix: FloatProperty(
        name="Metallic Map Mix", default=1.0, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        update=_update_metallic_map_mix,
        description="How much the Metallic texture map applies over the base Metallic slider (0.0 = base slider only, 1.0 = full metallic map)")
    dielectric_specular: FloatProperty(
        name="Specular", default=0.50, min=0.0, max=1.0, precision=2, subtype='FACTOR',
        update=_make_prop_updater("dielectric_specular"),
        description="Surface specular reflection strength (used as dielectric F0 fallback)")
    use_native_771_detail_slots: BoolProperty(
        name="Use Native 2x2m Detail Slots (Shader 0x771)", default=False,
        update=_update_use_native_771_detail_slots,
        description="When enabled, binds base-game 2x2m tiling detail maps into the 0x771 shader block without baking into the prop's 0..1 UV texture")
    use_2x2m_detail_shader: BoolProperty(
        name="Enable 2x2m Surface Micro-Detail (Shader 0x771)",
        default=False,
        update=_update_use_2x2m_detail_shader,
        description="Bind native Skate 2x2m world-tiled detail normal/roughness maps in Shader 0x771 (like Skate 3 Create-A-Park props) without baking noise into your UV0 Albedo",
    )
    detail_2x2m_preset: EnumProperty(
        name="2x2m Detail Surface",
        items=DETAIL_2X2M_PRESET_ITEMS,
        default='CONCRETE_SIDEWALK',
        update=_make_prop_updater("detail_2x2m_preset"),
        description="Choose which native 2x2m world-tiled detail surface to bind in Shader 0x771",
    )
    detail_2x2m_intensity: FloatProperty(
        name="Detail Intensity",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        precision=2,
        update=_make_prop_updater("detail_2x2m_intensity"),
        description="Strength of the 2x2m micro-detail normal/roughness overlay in Shader 0x771",
    )
    detail_2x2m_scale: FloatProperty(
        name="Tiling Scale (m)",
        default=1.0,
        min=0.25,
        max=8.0,
        step=10,
        precision=2,
        unit='LENGTH',
        update=_make_prop_updater("detail_2x2m_scale"),
        description="World-space tiling scale in meters for the 2x2m micro-detail grain (default 1.0m = 2x finer than vanilla 2.0m; lower = tighter grain)",
    )
    use_procedural_wear: BoolProperty(
        name="Procedural Edge & Surface Wear",
        default=False,
        update=_make_wear_updater("use_procedural_wear"),
        description="Adds procedural sharp-edge chipping & normal bump, crevice darkening, and top-down surface/rail grind scratches on top of any Starter Material Template or Image Texture without changing UVs",
    )
    wear_basis: EnumProperty(
        name="Wear Basis",
        items=WEAR_BASIS_ITEMS,
        default='AUTO',
        update=_update_wear_basis,
        description="Material basis that tunes edge highlights, crevice grime, and top-down scratch roughness (works with both Starter Templates and Image Textures)",
    )
    wear_edge_strength: FloatProperty(
        name="Sharp Edge Wear & Bump",
        default=0.65,
        min=0.0,
        max=3.0,
        soft_min=0.0,
        soft_max=3.0,
        subtype='FACTOR',
        precision=2,
        update=_make_wear_updater("wear_edge_strength"),
        description="Lightens sharp convex corners and adds chipped normal-map bump and roughness variation along edges (push up to 3.0 for heavy overdrive wear)",
    )
    wear_crevice_strength: FloatProperty(
        name="Crevice Darkening (Cavity)",
        default=0.65,
        min=0.0,
        max=3.0,
        soft_min=0.0,
        soft_max=3.0,
        subtype='FACTOR',
        precision=2,
        update=_make_wear_updater("wear_crevice_strength"),
        description="Darkens concave crevices and inner corners while increasing cavity roughness (push up to 3.0 for deep dark cavities)",
    )
    wear_top_strength: FloatProperty(
        name="Top-Down Grind / Scratch Wear",
        default=0.50,
        min=0.0,
        max=3.0,
        soft_min=0.0,
        soft_max=3.0,
        subtype='FACTOR',
        precision=2,
        update=_make_wear_updater("wear_top_strength"),
        description="Adds scratched, lighter, rougher top-down wear on upward-facing surfaces (push up to 3.0 for heavy rail/ledge grind gouges)",
    )
    wear_scale: FloatProperty(
        name="Wear Width & Grain Scale",
        default=0.50,
        min=0.10,
        max=2.00,
        subtype='FACTOR',
        precision=2,
        update=_make_wear_updater("wear_scale"),
        description="Controls the spread width of edge/crevice wear and the frequency of scratch/chip grain",
    )
    albedo_tint: FloatVectorProperty(
        name="Color Tint", size=3, default=(1.0, 1.0, 1.0), min=0.0, max=2.0, subtype='COLOR',
        update=_make_prop_updater("albedo_tint"),
        description="RGB color multiplier applied to the Base Color texture in-game (1,1,1 = neutral untouched)")
    use_external_texture_override: BoolProperty(
        name="Override Material Texture with External PNG",
        default=False,
        update=_make_prop_updater("use_external_texture_override"),
        description="When enabled, uses Custom Texture Image instead of the Blender material's image node")
    custom_texture_path: StringProperty(
        name="Custom Texture Image", subtype='FILE_PATH', default="",
        update=_make_prop_updater("custom_texture_path"),
        description="Optional external PNG image to use as the prop's texture when Override is enabled")

    # -----------------------------------------------------------------------
    # Step 5: Light (in-game) & Emissive Mesh Glow
    # -----------------------------------------------------------------------
    light_type: EnumProperty(
        name="New Light Type", items=LIGHT_TYPES, default='SPOT',
        description="Type of Blender light created by Add Light")
    draw_light_overlay: BoolProperty(
        name="Show Light Cones in Viewport", default=False,
        update=_tag_redraw_view3d,
        description="Show dynamic light cone and radius guides in the 3D viewport")
    show_preflight_details: BoolProperty(
        name="Show Pre-Flight Details", default=False,
        description="Expand or collapse individual Pre-Flight check items")
    glow_color: FloatVectorProperty(
        name="Glow Color", size=3, default=(1.0, 0.85, 0.55), min=0.0, max=1.0, subtype='COLOR',
        description="Emissive glow color for selected glowing faces and their linked dynamic light")
    glow_intensity: FloatProperty(
        name="Glow Brightness", default=5.0, min=0.1, max=50.0, precision=1,
        description="Emissive brightness for the glowing mesh emitter and its linked dynamic light")
    glow_spawn_light: BoolProperty(
        name="Also Spawn Linked Dynamic Light", default=True,
        description="In addition to making selected faces glow with an emissive material, also spawn a linked dynamic light at the face cluster center")
    # Legacy fallback properties kept for backwards compatibility
    inner_cone_angle: FloatProperty(
        name="Inner Cone Angle", default=math.radians(30.0),
        min=math.radians(5.0), max=math.radians(90.0),
        subtype='ANGLE', unit='ROTATION', update=_update_inner_cone)
    outer_cone_angle: FloatProperty(
        name="Outer Cone Angle", default=math.radians(50.0),
        min=math.radians(10.0), max=math.radians(90.0),
        subtype='ANGLE', unit='ROTATION', update=_update_outer_cone)
    attenuation_radius: FloatProperty(
        name="Attenuation Radius", default=30.0, min=1.0, max=100.0, unit='LENGTH', precision=1)
    light_intensity: FloatProperty(
        name="Intensity", default=51.83, min=0.0, max=1000.0, precision=2)
    light_color: FloatVectorProperty(
        name="Light Color", size=3, default=(1.0, 0.6977, 0.4995), min=0.0, max=1.0, subtype='COLOR')
    cast_shadows: BoolProperty(name="Cast Shadows", default=False)

    # -----------------------------------------------------------------------
    # Step 6: Thumbnail
    # -----------------------------------------------------------------------
    thumb_bg_preset: EnumProperty(
        name="Backdrop Preset",
        items=THUMB_BG_PRESET_ITEMS,
        default='SUNNY_DAY',
        update=_update_thumb_bg_preset,
        description="Select a thumbnail backdrop preset (Sunny Day, Studio Lighting, Transparent, or Flat Background Color)",
    )
    thumb_bg_style: EnumProperty(
        name="Backdrop", items=THUMB_BG_STYLES, default='SUNNY_DAY',
        update=_make_thumb_live_updater("thumb_bg_style"),
        description="Backdrop style for rendered Quick Drop thumbnails")
    thumb_bg_color: FloatVectorProperty(
        name="Background Color",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.14, 0.16, 0.20),
        update=_make_thumb_live_updater("thumb_bg_color"),
        description="Custom RGB background color for Flat Background Color thumbnail renders",
    )
    thumb_floor_color: FloatVectorProperty(
        name="Ground / Floor Color",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.42, 0.40, 0.37),
        update=_make_thumb_live_updater("thumb_floor_color"),
        description="Ground or studio floor color for Sunny Day and Studio Lighting thumbnail renders",
    )
    thumb_key_fill_ratio: FloatProperty(
        name="Key / Fill Balance",
        default=0.65,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        precision=2,
        update=_make_thumb_live_updater("thumb_key_fill_ratio"),
        description="Balance between dramatic key light contrast (1.0) and soft even fill light (0.0) in Studio Lighting",
    )
    thumb_use_cyclorama_floor: BoolProperty(
        name="Shadow-Catching Studio Floor",
        default=True,
        update=_make_thumb_live_updater("thumb_use_cyclorama_floor"),
        description="Places a temporary studio floor flush at Z=0 during thumbnail render",
    )
    thumb_lighting_mode: EnumProperty(
        name="Render Lighting",
        items=THUMB_LIGHTING_MODES,
        default='OUTDOOR_SUN',
        update=_make_thumb_live_updater("thumb_lighting_mode"),
        description="Lighting rig used during thumbnail rendering (temporary lights are cleaned up automatically after render)",
    )
    thumb_sun_elevation: FloatProperty(
        name="Sun Angle",
        default=38.0,
        min=5.0,
        max=88.0,
        update=_make_thumb_live_updater("thumb_sun_elevation"),
        description="Sun elevation angle in degrees when using Sunny Day lighting",
    )
    thumb_sun_rotation: FloatProperty(
        name="Sun Rotation",
        default=42.0,
        min=0.0,
        max=360.0,
        update=_make_thumb_live_updater("thumb_sun_rotation"),
        description="Horizontal sun azimuth rotation in degrees when using Sunny Day lighting",
    )
    thumb_exposure: FloatProperty(
        name="Brightness",
        default=0.0,
        min=-3.0,
        max=3.0,
        precision=2,
        update=_make_thumb_live_updater("thumb_exposure"),
        description="Brightness / exposure adjustment applied during thumbnail preview and rendering",
    )
    thumb_render_as_is: BoolProperty(
        name="Render Scene As-Is",
        default=False,
        update=_make_prop_updater("thumb_render_as_is"),
        description="Render all visible objects in the scene instead of isolating only the selected prop and its linked lights",
    )
    thumb_variant_cutout_enabled: BoolProperty(
        name="Variant Icon Cutout (Bottom-Left Transparency)",
        default=False,
        update=_update_thumb_variant_cutout,
        description=(
            "Punches a transparent bottom-left corner cutout into the thumbnail so Quick Drop's "
            "variant stack icon remains visible over opaque backgrounds (Sunny Day, Studio, etc.)"
        ),
    )
    show_adv_lights_thumb: BoolProperty(
        name="Advanced", default=False,
        description="Show rim light tint, sun angles, exposure, and custom thumbnail image path")
    thumb_rim_tint: FloatVectorProperty(
        name="Rim Light Tint", size=3, default=(0.90, 0.95, 1.00), min=0.0, max=1.0, subtype='COLOR',
        update=_make_thumb_live_updater("thumb_rim_tint"),
        description="Tint color for thumbnail rim lighting")
    custom_thumb_png: StringProperty(
        name="Custom Thumbnail Image", subtype='FILE_PATH', default="",
        update=_make_prop_updater("custom_thumb_png"),
        description="Pick an existing PNG image to use instead of rendering a thumbnail")
    custom_thumbnail_path: StringProperty(
        name="Custom Thumbnail Image",
        subtype='FILE_PATH',
        default="",
        get=lambda s: str(getattr(s, "custom_thumb_png", "")),
        set=lambda s, v: setattr(s, "custom_thumb_png", str(v or "")),
        description="Pick an existing PNG image to use instead of rendering a thumbnail",
    )
    skip_existing_thumbnails: BoolProperty(
        name="Skip Re-Rendering Existing Thumbnails",
        default=True,
        update=_tag_redraw_view3d,
        description=(
            "When enabled, props that already have a valid rendered or custom thumbnail PNG on disk "
            "reuse it during compilation without launching a new 3D thumbnail render pass "
            "(turn off or click 'Render Thumbnail' to force a fresh render)"
        ),
    )

    # -----------------------------------------------------------------------
    # Step 7: Ship & Pack
    # -----------------------------------------------------------------------
    target_mod_name: StringProperty(
        name="Prop Pack Name", default="CustomProps",
        update=_update_target_mod_name,
        description="Prop pack name (automatically prefixed with <Author>- when deploying to Skate/Mods/)")
    author_name: StringProperty(
        name="Author Name", default="",
        description="Author namespace (loaded from Edit -> Preferences -> Add-ons -> ReSkate Prop Studio)")
    pack_version: StringProperty(
        name="Pack Version", default="0.1.0",
        description="Semantic version of this prop pack (e.g. 0.1.0)")
    pack_description: StringProperty(
        name="Pack Description", default="Custom skateable props built with ReSkate Prop Studio",
        description="Short description for manifest.json and Thunderstore")
    mod_icon_path: StringProperty(
        name="Mod Folder Icon (icon.png)", subtype='FILE_PATH', default="",
        description="Optional PNG image copied as icon.png into the deployed mod folder (Mods/<PackName>/icon.png) and Thunderstore package (defaults to the first prop's thumbnail when left blank)")
    use_pack_icon_as_first_prop: BoolProperty(
        name="Use Pack Icon as #1 Quick Drop Tile (Sets Tab Icon)",
        default=True,
        update=_tag_redraw_view3d,
        description=(
            "When a custom Pack Icon (icon.png) is set, automatically generates a small 60x60cm square tile prop "
            "with your Pack Icon as its texture and places it at #1 in the pack so Skate. uses your Pack Icon "
            "as the Quick Drop Tab Icon (includes a grab-friendly 30cm tall Quick Drop placement/grab proxy)"
        ),
    )
    mod_readme_path: StringProperty(
        name="Custom README.md File", subtype='FILE_PATH', default="",
        description="Optional custom README.md file copied into the deployed mod folder and Thunderstore package (auto-generates a README with the object list when left blank)")
    show_thunderstore_metadata: BoolProperty(
        name="Show Thunderstore Icon & README Options", default=False,
        description="Expand Thunderstore mod icon (icon.png), README.md, version, and description settings")
    show_compile_preferences_box: BoolProperty(
        name="Compile & Versioning Preferences",
        default=False,
        description="Expand compile caching and automatic pack versioning options",
    )
    auto_bump_pack_version: BoolProperty(
        name="Auto-Increment Patch Version on Compile / Export",
        default=True,
        description="Automatically increments the patch version (e.g. 1.0.0 -> 1.0.1) each time you compile or export a Thunderstore .zip",
    )
    merge_existing_mod: BoolProperty(
        name="Add & Merge into Existing Mod",
        default=True,
        description="When the target Mod Folder already contains compiled props, add/update this prop alongside the existing props without overwriting them",
    )
    auto_disable_old_mod_versions: BoolProperty(
        name="Auto-Disable Older Mod Versions in mods.json",
        default=True,
        description="Automatically disables older versions of this mod pack in Mods/mods.json when deploying so duplicate prop versions never conflict",
    )
    compiler_backend: EnumProperty(
        name="Compiler Backend",
        items=[
            (
                'CPU',
                "CPU Compilation (Standard)",
                "Standard deterministic CPU texture & physics compilation",
            ),
            (
                'GPU_EXP',
                "GPU Compilation (Experimental - Fast)",
                "Experimental GPU-accelerated texture & BC7 mipmap compilation with parallel workers (may crash if GPU VRAM is low)",
            ),
        ],
        default='CPU',
        description="Choose between standard CPU compilation or experimental GPU-accelerated compilation",
    )
    compile_pipeline_mode: EnumProperty(
        name="Compile Speed Mode",
        items=COMPILE_PIPELINE_MODE_ITEMS,
        default='FAST_OPTIMIZED',
        description="Choose Fast (Deduplicated & Cached — Recommended) or Legacy (Full Uncached Rebuild — A/B Test)",
    )
    decal_shader_mode: EnumProperty(
        name="Decal Shader Mode",
        items=DECAL_SHADER_MODE_ITEMS,
        default='CUTOUT_273',
        update=_make_prop_updater("decal_shader_mode"),
        description="Default Decal Shader Mode for standalone flat decals or decal material slots on this prop",
    )
    show_pack_contents: BoolProperty(
        name="Show Pack Contents",
        default=True,
        description="Expand or collapse the Pack Contents list in 7. Ship",
    )
    show_scene_prop_packs_foldout: BoolProperty(
        name="Scene Prop Packs",
        default=False,
        description="Expand or collapse the Scene Prop Packs list on Page 6",
    )
    show_qd_tabs_foldout: BoolProperty(
        name="Quick Drop Tabs in Pack / Scene",
        default=False,
        description="Expand or collapse the Quick Drop Tabs in Pack / Scene (Click to Assign) list on Page 5",
    )
    pack_contents_filter: StringProperty(
        name="Filter Props",
        default="",
        description="Search filter for props inside Pack Contents",
    )
    export_fbmod_only: BoolProperty(
        name="Export .fbmod Archive Only (Don't Deploy)", default=False,
        description="Only build the .fbmod file in the staging folder without copying to Skate/Mods/")
    show_paths_config: BoolProperty(
        name="Paths & Folders (Game & Staging)", default=False,
        description="Configure persistent Skate. Game Folder, Staging Folder, and ReSkate Studio Folder")
    show_adv_export: BoolProperty(
        name="Advanced", default=False,
        description="Show mod folder name, .fbmod-only toggle, and manifest inspector")
    show_pack_share: BoolProperty(
        name="Pack & Share", default=False,
        description="Show Thunderstore zip export, version bump, and batch thumbnail tools")
    include_2a_variant: BoolProperty(
        name="Include Companion Variant", default=False,
        description="Optional: also generate a second duplicate test item in Quick Drop (off by default)")

    # Non-blocking modal compiler status & live log
    is_compiling: BoolProperty(name="Is Compiling", default=False)
    compile_progress: FloatProperty(
        name="Compile Progress", default=0.0, min=0.0, max=100.0, subtype='PERCENTAGE')
    compile_step_label: StringProperty(name="Compile Step", default="")
    compile_status_msg: StringProperty(name="Compile Status", default="")
    compile_has_error: BoolProperty(name="Compile Error", default=False)
    show_compile_log: BoolProperty(
        name="Show Compiler Log", default=False,
        description="Show the last 10 lines of compiler output")
    compile_log_text: StringProperty(name="Compile Log", default="")


# ---------------------------------------------------------------------------
# Active-Object Selection Watcher Handlers (depsgraph + msgbus + load_post)
# ---------------------------------------------------------------------------

def _msgbus_active_object_callback():
    if _IS_SYNCING_PROP or _IN_MAT_TEX_SYNC:
        return
    for _m in sys.modules.values():
        if getattr(_m, "_IS_SYNCING_PROP", False) or getattr(_m, "_IN_MAT_TEX_SYNC", False):
            return
    try:
        sync_active_object_props(bpy.context)
    except Exception:
        pass
    try:
        from . import gpu_draw
        gpu_draw.sync_draw_handlers(bpy.context)
    except Exception:
        pass


def _subscribe_active_object_msgbus():
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.LayerObjects, "active"),
            owner=_MSGBUS_OWNER,
            args=(),
            notify=_msgbus_active_object_callback,
            options={'PERSISTENT'},
        )
    except Exception:
        pass


_LAST_SK8_MAT_IMG_SIGS: dict[str, tuple] = {}
_LAST_OBJ_NAMES: dict[int, str] = {}
_LAST_DEPSGRAPH_OBJ_COUNT: int = -1
_KNOWN_LIGHT_PTRS: set[int] = set()
_KNOWN_LIGHTS_INITIALIZED: bool = False
_IS_SYNCING_PROP: bool = False
_IN_MAT_TEX_SYNC: bool = False
_IN_NAME_SYNC: bool = False


@persistent
def _on_depsgraph_update_post(scene, depsgraph=None):
    global _IN_MAT_TEX_SYNC, _IN_NAME_SYNC, _IS_SYNCING_PROP, _KNOWN_LIGHTS_INITIALIZED, _LAST_DEPSGRAPH_OBJ_COUNT
    if _IS_SYNCING_PROP or _IN_MAT_TEX_SYNC:
        return
    for _m in sys.modules.values():
        if getattr(_m, "_IS_SYNCING_PROP", False) or getattr(_m, "_IN_MAT_TEX_SYNC", False):
            return

    cur_obj_count = len(bpy.data.objects)
    obj_count_changed = (cur_obj_count != _LAST_DEPSGRAPH_OBJ_COUNT) or (not _KNOWN_LIGHTS_INITIALIZED)
    _LAST_DEPSGRAPH_OBJ_COUNT = cur_obj_count

    if obj_count_changed:
        try:
            # 0. Auto-link newly spawned Spot/Point lights (e.g. Shift+A -> Light -> Spot/Point) to the active/synced prop mesh
            if not _KNOWN_LIGHTS_INITIALIZED:
                _KNOWN_LIGHT_PTRS.clear()
                for o in bpy.data.objects:
                    if getattr(o, "type", None) == 'LIGHT':
                        _KNOWN_LIGHT_PTRS.add(o.as_pointer())
                _KNOWN_LIGHTS_INITIALIZED = True
            else:
                prop_scn0 = getattr(scene, "reskate_prop", None) if scene else None
                last_prop_nm = ""
                if prop_scn0 is not None:
                    try:
                        last_prop_nm = str(super(ReSkatePropSettings, prop_scn0).__getattribute__("get")("_active_synced_obj_name", "") or "").strip()
                    except Exception:
                        last_prop_nm = ""
                owner_mesh = None
                if last_prop_nm and scene and last_prop_nm in scene.objects:
                    owner_mesh = resolve_collision_to_owner_prop(scene.objects[last_prop_nm])
                if owner_mesh is None:
                    owner_mesh = resolve_active_prop_mesh(bpy.context)
                for o in bpy.data.objects:
                    if getattr(o, "type", None) != 'LIGHT':
                        continue
                    l_ptr = o.as_pointer()
                    if l_ptr not in _KNOWN_LIGHT_PTRS:
                        _KNOWN_LIGHT_PTRS.add(l_ptr)
                        if (
                            owner_mesh is not None
                            and getattr(owner_mesh, "type", None) == 'MESH'
                            and getattr(o, "data", None) is not None
                            and getattr(o.data, "type", "") in ('SPOT', 'POINT')
                            and not o.name.startswith(("ReSkate_Temp_", "_ReSkate_"))
                            and not bool(o.get("reskate_unlinked_by_user", False))
                        ):
                            o["reskate_owner_prop"] = owner_mesh.name
                            if o.parent is None:
                                try:
                                    mw_save = o.matrix_world.copy()
                                    o.parent = owner_mesh
                                    o.matrix_parent_inverse = owner_mesh.matrix_world.inverted()
                                    o.matrix_world = mw_save
                                except Exception:
                                    pass
                            if hasattr(owner_mesh, "reskate_linked_lights"):
                                existing_l = [it.light_obj for it in owner_mesh.reskate_linked_lights]
                                if o not in existing_l:
                                    slot = owner_mesh.reskate_linked_lights.add()
                                    slot.light_obj = o
                                    slot.enabled = True
                                    owner_mesh.reskate_linked_light_index = len(owner_mesh.reskate_linked_lights) - 1
                            try:
                                from . import ui_panels
                                ui_panels.invalidate_panel_caches()
                            except Exception:
                                pass
        except Exception:
            pass
        try:
            # 1. Detect duplicated mesh objects sharing the same reskate_stable_prop_key and clear the duplicate's key (§5.4)
            seen_stable_keys: dict[str, bpy.types.Object] = {}
            for o in bpy.data.objects:
                if getattr(o, "type", None) != 'MESH':
                    continue
                sk = str(o.get("reskate_stable_prop_key", "") or "").strip()
                if sk:
                    if sk in seen_stable_keys:
                        prev_o = seen_stable_keys[sk]
                        # Prefer keeping the stable key on the object without a .001/.002 suffix
                        dup_o = o if re.search(r"\.\d+$", o.name) or not re.search(r"\.\d+$", prev_o.name) else prev_o
                        if dup_o is prev_o:
                            seen_stable_keys[sk] = o
                        try:
                            del dup_o["reskate_stable_prop_key"]
                            from . import compiler_bridge
                            clean_disp = compiler_bridge.clean_prop_display_name(dup_o.name)
                            clean_id = _clean_slug_id(clean_disp)
                            dup_o["reskate_display_name"] = clean_disp
                            dup_o["reskate_prop_id"] = clean_id
                            dup_o["reskate_key_slug"] = f"own_customprops_{clean_id}"
                            st_d = get_or_init_object_prop_state(dup_o)
                            st_d["prop_display_name"] = clean_disp
                            st_d["prop_id"] = clean_id
                            st_d["prop_key_slug"] = f"own_customprops_{clean_id}"
                            save_object_prop_state(dup_o, st_d)
                        except Exception:
                            pass
                    else:
                        seen_stable_keys[sk] = o
        except Exception:
            pass

    try:
        # 2. Detect Outliner / Viewport object renames and two-way sync prop_display_name without touching reskate_stable_prop_key (§5.4)
        if not _IN_NAME_SYNC and not _IS_SYNCING_PROP:
            if obj_count_changed or not _LAST_OBJ_NAMES:
                check_objs = list(bpy.data.objects)
            else:
                act_o = getattr(bpy.context, "active_object", None)
                check_objs = [act_o] if act_o is not None else []
            for o in check_objs:
                if (
                    o is None
                    or getattr(o, "type", None) != 'MESH'
                    or is_collision_helper_object(o)
                    or bool(o.get("reskate_is_scale_reference", False))
                    or bool(o.get("reskate_is_ref_helper", False))
                    or o.name.upper().startswith(("RESKATE_REF_", "RESKATE_SKATER_", "RESKATE_BOARD_", "RESKATE_GROUND_PLANE", "_RESKATE_"))
                ):
                    continue
                ptr = o.as_pointer()
                prev_nm = _LAST_OBJ_NAMES.get(ptr)
                if prev_nm is None:
                    _LAST_OBJ_NAMES[ptr] = o.name
                elif prev_nm != o.name:
                    _LAST_OBJ_NAMES[ptr] = o.name
                    _IN_NAME_SYNC = True
                    try:
                        from . import compiler_bridge
                        new_disp = compiler_bridge.clean_prop_display_name(o.name)
                        st_o = get_or_init_object_prop_state(o)
                        st_o["prop_display_name"] = new_disp
                        if not o.get("reskate_stable_prop_key") and not st_o.get("prop_id_locked"):
                            cid = _clean_slug_id(new_disp)
                            st_o["prop_id"] = cid
                            st_o["prop_key_slug"] = f"own_customprops_{cid}"
                        save_object_prop_state(o, st_o)
                        act_mesh = resolve_active_prop_mesh(bpy.context)
                        prop_scn = getattr(scene, "reskate_prop", None) if scene else None
                        if prop_scn is not None and act_mesh == o:
                            prop_scn.prop_display_name = new_disp
                            prop_scn["_active_synced_obj_name"] = o.name
                    except Exception:
                        pass
                    finally:
                        _IN_NAME_SYNC = False
    except Exception:
        pass

    try:
        sync_active_object_props(bpy.context)
    except Exception:
        pass
    try:
        from . import gpu_draw
        gpu_draw.sync_draw_handlers(bpy.context)
    except Exception:
        pass
    if _IS_SYNCING_PROP or _IN_MAT_TEX_SYNC:
        return
    try:
        obj = resolve_active_prop_mesh(bpy.context)
        if obj and getattr(obj, "type", None) == 'MESH':
            for slot in obj.material_slots:
                mat = slot.material
                if not mat:
                    continue
                sk8 = getattr(mat, "sk8_material", None)
                if sk8 is None:
                    continue
                sig = tuple(
                    getattr(getattr(sk8, k, None), "name", "")
                    for k in ("base_color_image", "normal_image", "roughness_image", "metallic_image", "mask_image", "opacity_image", "emission_image")
                )
                prev_sig = _LAST_SK8_MAT_IMG_SIGS.get(mat.name)
                if prev_sig != sig:
                    _LAST_SK8_MAT_IMG_SIGS[mat.name] = sig
                    if prev_sig is not None and any(sig) and bool(getattr(sk8, "use_custom_texture_overrides", False)):
                        _IN_MAT_TEX_SYNC = True
                        try:
                            from . import compiler_bridge
                            if hasattr(compiler_bridge, "sync_user_texture_slots_to_shader_nodes"):
                                compiler_bridge.sync_user_texture_slots_to_shader_nodes(obj, mat, source_settings=sk8)
                                compiler_bridge.invalidate_ui_caches()
                        finally:
                            _IN_MAT_TEX_SYNC = False
    except Exception:
        pass


@persistent
def _on_load_post(_dummy=None, _dummy2=None):
    global _KNOWN_LIGHTS_INITIALIZED
    _subscribe_active_object_msgbus()
    try:
        _KNOWN_LIGHT_PTRS.clear()
        for o in bpy.data.objects:
            if getattr(o, "type", None) == 'LIGHT':
                _KNOWN_LIGHT_PTRS.add(o.as_pointer())
        _KNOWN_LIGHTS_INITIALIZED = True
    except Exception:
        _KNOWN_LIGHTS_INITIALIZED = False
    try:
        for sc in bpy.data.scenes:
            pr = getattr(sc, "reskate_prop", None)
            if pr is not None:
                if getattr(pr, "is_compiling", False):
                    pr.is_compiling = False
                    pr.compile_progress = 0.0
                    pr.compile_step_label = ""
                # Always default viewport overlays to OFF when opening a .blend file so stale saved flags never auto-trigger heavy GPU overlays
                for ov_attr in (
                    "draw_grind_overlay",
                    "draw_col_overlay",
                    "show_collision_overlay",
                    "show_placement_proxy_overlay",
                    "draw_face_surface_overlay",
                    "show_alignment_overlay",
                    "draw_light_overlay",
                    "show_pack_membership_overlay",
                ):
                    if hasattr(pr, ov_attr) and bool(getattr(pr, ov_attr, False)):
                        try:
                            setattr(pr, ov_attr, False)
                        except Exception:
                            pass
        # Clean up any leftover temporary studio render objects/lights/worlds/meshes from an interrupted render
        for o in list(bpy.data.objects):
            if o.name.startswith("ReSkate_Temp_"):
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
            elif o.hide_render and not o.name.startswith("_ReSkate_") and not o.name.endswith("_COL") and not getattr(getattr(o, "reskate_col_prop", None), "is_col_proxy", False):
                # Restore hide_render if a cancelled thumbnail render left non-active props hidden in render
                pass
        for l_d in list(bpy.data.lights):
            if l_d.name.startswith("ReSkate_Temp_"):
                try:
                    bpy.data.lights.remove(l_d, do_unlink=True)
                except Exception:
                    pass
        for w_d in list(bpy.data.worlds):
            if w_d.name.startswith("ReSkate_Temp_"):
                try:
                    bpy.data.worlds.remove(w_d, do_unlink=True)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        for o in bpy.data.objects:
            if getattr(o, "type", None) == 'MESH':
                raw_st = o.get("reskate_prop_state", "") or o.get("reskate_prop_state_json", "")
                if raw_st and "custom_qd_tab" in str(raw_st):
                    try:
                        st = json.loads(str(raw_st))
                        if isinstance(st, dict) and st.get("custom_qd_tab") and st.get("custom_qd_tab") != "bkcustomprops":
                            save_custom_qd_tab(str(st.get("custom_qd_tab")))
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        sync_active_object_props(bpy.context)
    except Exception:
        pass
    try:
        from . import gpu_draw
        gpu_draw.sync_draw_handlers(bpy.context)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registration Routine
# ---------------------------------------------------------------------------

CLASSES = (
    ReSkateAddonPreferences,
    ReSkateGrindPathItem,
    ReSkateLinkedLightItem,
    ReSkateMaterialSurfaceItem,
    ReSkatePackQueueItem,
    ReSkateSk8MaterialSettings,
    ReSkateColPropSettings,
    ReSkateLightSettings,
    ReSkatePropSettings,
)

_OWNED_SK8_MATERIAL = False

def register():
    global _OWNED_SK8_MATERIAL
    for cls in CLASSES:
        if not getattr(cls, "is_registered", False):
            bpy.utils.register_class(cls)
    if not hasattr(bpy.types.Material, "sk8_material"):
        bpy.types.Material.sk8_material = PointerProperty(type=ReSkateSk8MaterialSettings)
        _OWNED_SK8_MATERIAL = True
    if not hasattr(bpy.types.Material, "reskate_mat"):
        bpy.types.Material.reskate_mat = PointerProperty(type=ReSkateSk8MaterialSettings)
    if not hasattr(bpy.types.Scene, "reskate_prop"):
        bpy.types.Scene.reskate_prop = PointerProperty(type=ReSkatePropSettings)
    if not hasattr(bpy.types.Scene, "sk8_ui_mode"):
        bpy.types.Scene.sk8_ui_mode = EnumProperty(
            name="UI Mode",
            items=UI_MODE_ITEMS,
            default='SIMPLE',
            update=_on_ui_mode_changed,
            description="Switch between Simple (Beginner), Advanced (Pro), and Experimental UI modes",
        )
    if not hasattr(bpy.types.Scene, "reskate_pack_queue"):
        bpy.types.Scene.reskate_pack_queue = CollectionProperty(type=ReSkatePackQueueItem)
    if not hasattr(bpy.types.Scene, "reskate_pack_queue_index"):
        bpy.types.Scene.reskate_pack_queue_index = IntProperty(name="Pack Queue Index", default=0, min=0)
    if not hasattr(bpy.types.Object, "reskate_col_prop"):
        bpy.types.Object.reskate_col_prop = PointerProperty(type=ReSkateColPropSettings)
    if not hasattr(bpy.types.Object, "reskate_custom_collision_object"):
        bpy.types.Object.reskate_custom_collision_object = PointerProperty(
            name="Saved Collision Object",
            type=bpy.types.Object,
            poll=_poll_mesh_object,
        )
    if not hasattr(bpy.types.Object, "reskate_custom_placement_proxy_object"):
        bpy.types.Object.reskate_custom_placement_proxy_object = PointerProperty(
            name="Saved Placement Proxy Object",
            type=bpy.types.Object,
            poll=_poll_mesh_object,
        )
    if not hasattr(bpy.types.Object, "reskate_light"):
        bpy.types.Object.reskate_light = PointerProperty(type=ReSkateLightSettings)
    if not hasattr(bpy.types.Object, "reskate_include_in_pack"):
        bpy.types.Object.reskate_include_in_pack = BoolProperty(
            name="Include in Pack",
            default=False,
            description="Mark this prop to be included when clicking 'Compile All Marked Props'",
        )
    if not hasattr(bpy.types.Object, "reskate_grind_paths"):
        bpy.types.Object.reskate_grind_paths = CollectionProperty(type=ReSkateGrindPathItem)
    if not hasattr(bpy.types.Object, "reskate_grind_path_index"):
        bpy.types.Object.reskate_grind_path_index = IntProperty(name="Active Grind Path", default=0, min=0)
    if not hasattr(bpy.types.Object, "reskate_linked_lights"):
        bpy.types.Object.reskate_linked_lights = CollectionProperty(type=ReSkateLinkedLightItem)
    if not hasattr(bpy.types.Object, "reskate_linked_light_index"):
        bpy.types.Object.reskate_linked_light_index = IntProperty(name="Active Linked Light", default=0, min=0)
    if not hasattr(bpy.types.Object, "reskate_material_surfaces"):
        bpy.types.Object.reskate_material_surfaces = CollectionProperty(type=ReSkateMaterialSurfaceItem)

    if _on_depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update_post)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    _subscribe_active_object_msgbus()

def unregister():
    global _OWNED_SK8_MATERIAL
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    except Exception:
        pass
    if _on_depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update_post)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _OWNED_SK8_MATERIAL and hasattr(bpy.types.Material, "sk8_material"):
        delattr(bpy.types.Material, "sk8_material")
        _OWNED_SK8_MATERIAL = False
    if hasattr(bpy.types.Material, "reskate_mat"):
        delattr(bpy.types.Material, "reskate_mat")
    for attr in (
        "reskate_material_surfaces",
        "reskate_linked_light_index",
        "reskate_linked_lights",
        "reskate_grind_path_index",
        "reskate_grind_paths",
        "reskate_include_in_pack",
        "reskate_light",
        "reskate_custom_placement_proxy_object",
        "reskate_custom_collision_object",
        "reskate_col_prop",
    ):
        if hasattr(bpy.types.Object, attr):
            delattr(bpy.types.Object, attr)
    for attr in (
        "sk8_ui_mode",
        "reskate_pack_queue_index",
        "reskate_pack_queue",
        "reskate_prop",
    ):
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)
    for cls in reversed(CLASSES):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
