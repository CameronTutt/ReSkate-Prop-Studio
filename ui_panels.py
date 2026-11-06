"""
ReSkate Prop Studio — UI Panels (ui_panels.py)
Implements the 3-Mode UI Architecture (Simple | Advanced | Experimental) on top of the
complete 7-Step Checklist UX:
  1. Setup → 2. Collision & Grind → 3. Shaders, Sound & Look → 4. Light (in-game) → 5. Thumbnail → 6. Ship
Each step header displays ✅ / ⚠️ / ❌ with a clear one-line status reason, live compile progress,
scale reference toggles (Skater, Skateboard, 12-Sided Pipe), and all pro tools in Advanced Mode.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import bpy
import numpy as np
from bpy.types import Panel, UIList
from mathutils import Vector

from . import compiler_bridge
from . import gpu_draw
from . import properties as _props


DEFAULT_STAGING_DIR = compiler_bridge._get_staging_dir()

_METRICS_CACHE: dict[tuple, dict] = {}
_STEP_STATUS_CACHE: tuple[float, tuple | None, dict] = (0.0, None, {})
_GRIND_ATTR_CACHE: dict[tuple, int] = {}
_PATH_EXISTS_CACHE: dict[str, tuple[float, bool]] = {}
_THUMB_PREVIEW_CACHE: dict[str, tuple[float, str]] = {}
_SANCTUS_PREVIEW_COLL = None
_SANCTUS_ICON_MTIMES: dict[str, float] = {}
_SANCTUS_CAROUSEL_ITEMS_CACHE: tuple[float, tuple | None, list[tuple]] = (0.0, None, [])


def _get_sanctus_preview_collection():
    """Returns or creates the `bpy.utils.previews` collection for the Sanctus-style thumbnail carousel."""
    global _SANCTUS_PREVIEW_COLL
    if _SANCTUS_PREVIEW_COLL is not None:
        return _SANCTUS_PREVIEW_COLL
    try:
        from bpy.utils import previews as _bpy_previews
        _SANCTUS_PREVIEW_COLL = _bpy_previews.new()
    except Exception:
        _SANCTUS_PREVIEW_COLL = None
    return _SANCTUS_PREVIEW_COLL


def invalidate_carousel_icon_for_object(obj: bpy.types.Object | None = None) -> None:
    """Evicts cached Sanctus carousel preview icons for `obj` (or all objects when `obj` is None)."""
    global _SANCTUS_PREVIEW_COLL, _SANCTUS_ICON_MTIMES, _SANCTUS_CAROUSEL_ITEMS_CACHE
    _SANCTUS_CAROUSEL_ITEMS_CACHE = (0.0, None, [])
    if _SANCTUS_PREVIEW_COLL is None:
        _SANCTUS_ICON_MTIMES.clear()
        return
    if obj is None:
        try:
            _SANCTUS_PREVIEW_COLL.clear()
        except Exception:
            pass
        _SANCTUS_ICON_MTIMES.clear()
        return
    prefix = f"{obj.name}::"
    stale_keys = [k for k in list(_SANCTUS_PREVIEW_COLL.keys()) if str(k).startswith(prefix)]
    for k in stale_keys:
        try:
            del _SANCTUS_PREVIEW_COLL[k]
        except Exception:
            pass
        _SANCTUS_ICON_MTIMES.pop(k, None)


def _ensure_standby_prop_icon_png(context, obj: bpy.types.Object | None = None) -> str:
    """
    Returns the shared static 'No Thumbnail' icon PNG path (or its variant-notched counterpart)
    without running CPU wireframe silhouette generation.
    """
    try:
        ctx = context if hasattr(context, "scene") else bpy.context
        if obj is None and isinstance(context, bpy.types.Object):
            obj = context
        has_var = False
        if obj is not None:
            st = _props.get_or_init_object_prop_state(obj)
            fam_title = str(st.get("prop_family_title") or obj.get("reskate_family_title") or "").strip()
            has_var = bool(fam_title)
        return str(compiler_bridge.ensure_static_no_thumbnail_icon(ctx, has_variant=has_var, size=256))
    except Exception:
        return ""


def _resolve_prop_thumbnail_path(context, prop, obj: bpy.types.Object | None) -> tuple[str, bool]:
    """Returns `(path_str, is_custom_or_rendered)` for `obj`."""
    if obj is None:
        return "", False
    # 1. Explicit per-object custom or rendered path
    for k in ("reskate_custom_thumbnail_path", "reskate_rendered_thumb_256"):
        cand = str(obj.get(k, "") or "").strip()
        if cand and _cached_path_exists(cand, ttl_sec=0.5):
            return cand, True
    # 2. Scene prop custom_thumb_png if owned by this object
    if prop is not None:
        owner = str(prop.get("custom_thumb_owner", "")) if hasattr(prop, "get") else ""
        c_png = str(getattr(prop, "custom_thumb_png", "") or "").strip()
        if c_png and (not owner or owner == obj.name) and _cached_path_exists(c_png, ttl_sec=0.5):
            return c_png, True
    # 3. Staging `<slug>_thumb_256.png`
    slug = compiler_bridge.get_prop_slug(context, obj)
    staging_dir = compiler_bridge.get_staging_dir(context)
    for st_cand in (staging_dir / f"{slug}_thumb_256.png", DEFAULT_STAGING_DIR / f"{slug}_thumb_256.png"):
        if _cached_path_exists(str(st_cand), ttl_sec=0.5):
            return str(st_cand), True
    return "", False


def get_sanctus_carousel_enum_items(prop_self, context) -> list[tuple]:
    """Builds the EnumProperty items list with custom preview `icon_id`s for `template_icon_view`."""
    global _SANCTUS_CAROUSEL_ITEMS_CACHE
    ctx = context if context is not None else bpy.context
    if ctx is None:
        return [(
            _props._intern_carousel_str("__NONE__"),
            _props._intern_carousel_str("No Prop Selected"),
            _props._intern_carousel_str("Select a 3D mesh prop"),
            'MESH_DATA',
            0,
        )]

    sel_meshes = compiler_bridge.get_selected_prop_meshes(ctx)
    if not sel_meshes:
        return [(
            _props._intern_carousel_str("__NONE__"),
            _props._intern_carousel_str("No Prop Selected"),
            _props._intern_carousel_str("Select a 3D mesh prop in the viewport"),
            'MESH_DATA',
            0,
        )]

    now = time.monotonic()
    act_obj = getattr(ctx, "active_object", None)
    act_name = act_obj.name if act_obj else ""
    notch_rgb = _props.get_variant_notch_color_rgb(ctx)
    c_sig = (
        tuple((o.name, int(o.get("reskate_pack_sort_order", 999999))) for o in sel_meshes),
        act_name,
        notch_rgb,
    )
    c_ts, prev_sig, prev_items = _SANCTUS_CAROUSEL_ITEMS_CACHE
    if prev_sig == c_sig and (now - c_ts) < 1.0 and prev_items:
        return prev_items

    pcoll = _get_sanctus_preview_collection()
    items: list[tuple] = []
    for idx, m_obj in enumerate(sel_meshes):
        st = _props.get_or_init_object_prop_state(m_obj)
        fam_title = str(st.get("prop_family_title") or m_obj.get("reskate_family_title") or "").strip()
        thumb_path, _is_real = _resolve_prop_thumbnail_path(ctx, prop_self, m_obj)
        is_static_placeholder = False
        if not thumb_path:
            is_static_placeholder = True
            try:
                thumb_path = str(
                    compiler_bridge.ensure_static_no_thumbnail_icon(
                        ctx, has_variant=bool(fam_title), size=256
                    )
                )
            except Exception:
                thumb_path = ""
        elif fam_title and _cached_path_exists(thumb_path, ttl_sec=2.0):
            try:
                staging_dir = compiler_bridge.get_staging_dir(ctx)
                cache_dir = staging_dir / ".carousel_icons"
                cache_dir.mkdir(parents=True, exist_ok=True)
                safe_nm = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in m_obj.name)[:40]
                n_r, n_g, n_b = notch_rgb
                notched_p = cache_dir / f"real_var_{safe_nm}_{n_r}_{n_g}_{n_b}.png"
                src_mtime = Path(thumb_path).stat().st_mtime
                if (not notched_p.is_file()) or (notched_p.stat().st_mtime < src_mtime):
                    import shutil
                    shutil.copy2(thumb_path, notched_p)
                    compiler_bridge.apply_variant_corner_notch_to_png_in_place(
                        str(notched_p), rgb_u8=(n_r, n_g, n_b)
                    )
                thumb_path = str(notched_p)
            except Exception:
                pass

        icon_val = 'MESH_DATA'
        if pcoll is not None and thumb_path and _cached_path_exists(thumb_path, ttl_sec=2.0):
            try:
                key = (
                    f"__STATIC_NO_THUMB__::{bool(fam_title)}::{thumb_path}"
                    if is_static_placeholder
                    else f"{m_obj.name}::{thumb_path}"
                )
                if key not in pcoll:
                    mtime = Path(thumb_path).stat().st_mtime
                    thumb_entry = pcoll.load(key, str(thumb_path), 'IMAGE')
                    _SANCTUS_ICON_MTIMES[key] = mtime
                else:
                    thumb_entry = pcoll[key]
                if getattr(thumb_entry, "icon_id", 0):
                    icon_val = int(thumb_entry.icon_id)
            except Exception:
                icon_val = 'MESH_DATA'

        disp_nm = str(st.get("prop_display_name") or m_obj.name)
        packs = compiler_bridge.get_object_pack_memberships(ctx, m_obj)
        pack_desc = f"Pack: {', '.join(packs)}" if packs else "New Prop (not in a compiled pack yet)"
        id_s = _props._intern_carousel_str(m_obj.name)
        nm_s = _props._intern_carousel_str(disp_nm)
        desc_s = _props._intern_carousel_str(f"{disp_nm} ({m_obj.name}) — {pack_desc}")
        ic_s = _props._intern_carousel_str(icon_val) if isinstance(icon_val, str) else int(icon_val)
        items.append((id_s, nm_s, desc_s, ic_s, idx))
    _SANCTUS_CAROUSEL_ITEMS_CACHE = (now, c_sig, items)
    return items


def _is_advanced_lab_addon_enabled(context=None) -> bool:
    """Returns True when the second add-on ('ReSkate Prop Studio — Advanced & Lab') is enabled."""
    if getattr(bpy.types, "RESKATE_PT_lab_panel", None) is not None:
        return True
    ctx = context or bpy.context
    prefs = getattr(ctx, "preferences", None) if ctx else None
    if prefs is not None and hasattr(prefs, "addons"):
        if "sk8_prop_experimental_addon" in prefs.addons:
            return True
    return False


def _get_ui_mode(context) -> str:
    if not _is_advanced_lab_addon_enabled(context):
        return "SIMPLE"
    scene = getattr(context, "scene", None) if context else None
    if scene is not None and hasattr(scene, "sk8_ui_mode"):
        val = str(getattr(scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE")
        if val in ("SIMPLE", "ADVANCED", "EXPERIMENTAL"):
            return val
    prop = getattr(scene, "reskate_prop", None) if scene else None
    if prop is not None and hasattr(prop, "ui_mode"):
        val = str(getattr(prop, "ui_mode", "SIMPLE") or "SIMPLE")
        if val in ("SIMPLE", "ADVANCED", "EXPERIMENTAL"):
            return val
    return "SIMPLE"


def _cached_path_exists(path_str: str, ttl_sec: float = 2.0) -> bool:
    """Caches filesystem `.exists()` checks during UI draw loops to avoid disk stat() spam."""
    if not path_str:
        return False
    now = time.monotonic()
    hit = _PATH_EXISTS_CACHE.get(path_str)
    if hit is not None and (now - hit[0]) < ttl_sec:
        return hit[1]
    val = Path(path_str).exists()
    if len(_PATH_EXISTS_CACHE) > 64:
        _PATH_EXISTS_CACHE.clear()
    _PATH_EXISTS_CACHE[path_str] = (now, val)
    return val


def _get_cached_preview_image(path_str: str):
    """Loads or refreshes a Blender image datablock for UI template_icon preview, cached by file mtime."""
    if not path_str or not _cached_path_exists(path_str):
        return None
    try:
        p = Path(path_str)
        mtime = p.stat().st_mtime
        cached = _THUMB_PREVIEW_CACHE.get(path_str)
        img_name = f".reskate_preview_{p.name}"
        img = bpy.data.images.get(img_name)
        if cached and cached[0] == mtime and img is not None:
            try:
                img.preview_ensure()
            except Exception:
                pass
            return img
        if img is None:
            img = bpy.data.images.load(str(p), check_existing=True)
            img.name = img_name
        else:
            img.filepath = str(p)
            img.reload()
        try:
            img.preview_ensure()
        except Exception:
            pass
        _THUMB_PREVIEW_CACHE[path_str] = (mtime, img.name)
        return img
    except Exception:
        return None


def on_active_prop_switched():
    """Lightweight cache reset when the active object changes without wiping preview collections or scene pack caches."""
    global _STEP_STATUS_CACHE, _SANCTUS_CAROUSEL_ITEMS_CACHE
    _STEP_STATUS_CACHE = (0.0, None, {})
    _SANCTUS_CAROUSEL_ITEMS_CACHE = (0.0, None, [])


def invalidate_panel_caches():
    """Clears UI panel caches when operators mutate geometry, UVs, or files."""
    global _STEP_STATUS_CACHE, _SANCTUS_CAROUSEL_ITEMS_CACHE
    _METRICS_CACHE.clear()
    _GRIND_ATTR_CACHE.clear()
    _PATH_EXISTS_CACHE.clear()
    _THUMB_PREVIEW_CACHE.clear()
    _SANCTUS_ICON_MTIMES.clear()
    _SANCTUS_CAROUSEL_ITEMS_CACHE = (0.0, None, [])
    if _SANCTUS_PREVIEW_COLL is not None:
        try:
            _SANCTUS_PREVIEW_COLL.clear()
        except Exception:
            pass
    _STEP_STATUS_CACHE = (0.0, None, {})
    compiler_bridge.invalidate_ui_caches()


def _sync_shader_role_display_value(obj: bpy.types.Object | None, slot_idx: int, mat: bpy.types.Material | None):
    """Ensures `mat.reskate_mat.surface_role_preset` reflects any existing decal state or saved per-material surface and auto-populates detected shader images into texture slots."""
    if mat is None:
        return None
    ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
    if ms is None or not hasattr(ms, "surface_role_preset"):
        return ms

    is_multi_mat = bool(obj and obj.type == 'MESH' and len(obj.material_slots) > 1)
    if hasattr(compiler_bridge, "ensure_material_decal_slot_initialized"):
        try:
            compiler_bridge.ensure_material_decal_slot_initialized(mat, is_multi_material=is_multi_mat)
        except Exception:
            pass

    valid_keys = {it[0] for it in _props.SHADER_SURFACE_ROLE_ITEMS}
    cur_role = str(getattr(ms, "surface_role_preset", "concrete_smooth") or "concrete_smooth")
    if cur_role not in valid_keys:
        cur_role = "concrete_smooth"
    is_decal = bool(getattr(ms, "is_decal_slot", False))

    if is_decal and cur_role != "DECAL_OVERLAY":
        prev_sync = getattr(_props, "_IS_SYNCING_PROP", False)
        _props._IS_SYNCING_PROP = True
        try:
            ms.surface_role_preset = "DECAL_OVERLAY"
        except Exception:
            pass
        finally:
            _props._IS_SYNCING_PROP = prev_sync
        return ms

    if not is_decal and not mat.get("reskate_user_set_surface_role"):
        saved_surf = "DEFAULT"
        if obj is not None and hasattr(obj, "reskate_material_surfaces") and 0 <= slot_idx < len(obj.reskate_material_surfaces):
            saved_surf = str(getattr(obj.reskate_material_surfaces[slot_idx], "surface", "DEFAULT") or "DEFAULT")
        if saved_surf not in ("DEFAULT", "", "AUTO_FROM_NAME") and saved_surf in valid_keys and cur_role != saved_surf:
            prev_sync = getattr(_props, "_IS_SYNCING_PROP", False)
            _props._IS_SYNCING_PROP = True
            try:
                ms.surface_role_preset = saved_surf
            except Exception:
                pass
            finally:
                _props._IS_SYNCING_PROP = prev_sync
        elif hasattr(compiler_bridge, "get_synced_material_surface_info"):
            try:
                s_info = compiler_bridge.get_synced_material_surface_info(obj, slot_idx, mat)
                det_key = str(s_info.get("preset_key", "") or "")
                if det_key in valid_keys and cur_role == "concrete_smooth" and det_key != "concrete_smooth":
                    prev_sync = getattr(_props, "_IS_SYNCING_PROP", False)
                    _props._IS_SYNCING_PROP = True
                    try:
                        ms.surface_role_preset = det_key
                    except Exception:
                        pass
                    finally:
                        _props._IS_SYNCING_PROP = prev_sync
            except Exception:
                pass
    return ms


# ============================================================================
# Base Panel Definition
# ============================================================================

class RESKATE_PT_base:
    """Base mixin defining N-panel tab location and layout category."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ReSkate Prop'


# ============================================================================
# UI & Step Checklist Status Helpers
# ============================================================================

def draw_advanced_foldout(layout, prop_group, bool_prop_name: str, title: str = "Advanced"):
    """Draws a collapsible advanced settings foldout container."""
    is_open = getattr(prop_group, bool_prop_name, False)
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    box = layout.box()
    row = box.row(align=True)
    row.prop(prop_group, bool_prop_name, text=title, icon=icon, emboss=False)
    if is_open:
        col = box.column()
        col.separator(factor=0.5)
        return col
    return None


def _fast_mesh_tri_count(mesh) -> int:
    """Computes exact triangle count without Python polygon iteration using loop/poly counts."""
    poly_count = len(mesh.polygons)
    if poly_count == 0:
        return 0
    loop_count = len(mesh.loops)
    if loop_count >= 3 * poly_count:
        return loop_count - 2 * poly_count
    return sum(len(p.vertices) - 2 for p in mesh.polygons)


def get_active_mesh_metrics(context):
    """Safely calculates dimensions and polygon budget metrics for the active mesh in O(1) time."""
    obj = compiler_bridge.get_target_mesh_object(context)
    if not obj or obj.type != 'MESH':
        return None

    dims = obj.dimensions
    w, h, d = float(dims.x), float(dims.z), float(dims.y)  # Blender X=width, Z=height, Y=depth

    mesh = obj.data
    poly_count = len(mesh.polygons)
    vert_count = len(mesh.vertices)
    loop_count = len(mesh.loops)

    custom_orig = bool(obj.get("reskate_custom_origin", False))
    bbox = obj.bound_box
    if vert_count > 0 and bbox:
        rot_scale = obj.matrix_world.to_3x3()
        world_corners = [rot_scale @ Vector(pt) for pt in bbox]
        xs = [c.x for c in world_corners]
        ys = [c.y for c in world_corners]
        zs = [c.z for c in world_corners]
        w = float(max(xs) - min(xs))
        d = float(max(ys) - min(ys))
        h = float(max(zs) - min(zs))
        min_z = float(min(zs))
        z_off = _props.get_origin_ground_z_offset(context) if hasattr(_props, "get_origin_ground_z_offset") else 0.05
        origin_flush = custom_orig or (abs(min_z) < 0.02) or (abs(min_z + z_off) < 0.025)
    else:
        min_z = 0.0
        origin_flush = True

    cache_key = (obj.name, poly_count, vert_count, loop_count, round(min_z, 4), round(w, 4), round(h, 4), round(d, 4), custom_orig)
    cached = _METRICS_CACHE.get(cache_key)
    if cached is not None:
        cached["obj"] = obj
        return cached

    tri_count = _fast_mesh_tri_count(mesh)

    res = {
        "obj": obj,
        "width": w,
        "height": h,
        "depth": d,
        "polys": poly_count,
        "verts": vert_count,
        "tris": tri_count,
        "origin_flush": origin_flush,
    }
    if len(_METRICS_CACHE) > 32:
        _METRICS_CACHE.clear()
    _METRICS_CACHE[cache_key] = res
    return res


def _count_grind_attr_edges(mesh) -> int:
    """Counts edges marked with reskate_grind > 0 using fast numpy foreach_get and signature cache."""
    attr = mesh.attributes.get("reskate_grind")
    if not attr or len(attr.data) == 0:
        return 0
    n_data = len(attr.data)
    f0 = attr.data[0].value
    fm = attr.data[n_data // 2].value
    fl = attr.data[n_data - 1].value
    key = (id(mesh), n_data, f0, fm, fl)
    cached = _GRIND_ATTR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        buf = np.empty(n_data, dtype=np.int32)
        attr.data.foreach_get("value", buf)
        cnt = int(np.count_nonzero(buf > 0))
    except Exception:
        cnt = sum(1 for item in attr.data if item.value > 0)
    if len(_GRIND_ATTR_CACHE) > 32:
        _GRIND_ATTR_CACHE.clear()
    _GRIND_ATTR_CACHE[key] = cnt
    return cnt


def evaluate_all_step_statuses(context) -> dict[str, tuple[str, str]]:
    """
    Evaluates all checklist steps and returns a dict mapping step key -> (badge, reason)
    where badge is one of '✅', '⚠️', '❌'.
    """
    global _STEP_STATUS_CACHE
    scene = getattr(context, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None
    metrics = get_active_mesh_metrics(context)

    if prop is None or metrics is None:
        return {
            "setup": ("❌", "No 3D mesh selected"),
            "collision": ("❌", "Select a mesh first"),
            "grind": ("⚠️", "Select a mesh first"),
            "look": ("❌", "Select a mesh first"),
            "light": ("✅", "Optional (no lights linked)"),
            "thumbnail": ("✅", "Auto-generated if not rendered"),
            "ship": ("❌", "Select a 3D mesh object to ship"),
        }

    obj = metrics["obj"]
    tris = metrics["tris"]
    col_obj_ptr = getattr(prop, "custom_collision_object", None)
    n_paths = len(getattr(obj, "reskate_grind_paths", []))
    n_auto = _count_grind_attr_edges(obj.data)
    linked_items = list(getattr(obj, "reskate_linked_lights", []))

    now = time.monotonic()
    state_sig = (
        obj.name,
        obj.mode,
        tris,
        metrics["verts"],
        metrics["origin_flush"],
        round(metrics["width"], 3),
        round(metrics["height"], 3),
        round(metrics["depth"], 3),
        prop.prop_display_name,
        prop.collision_mode,
        getattr(prop, "no_collision_placement_mode", "FLAT_PLANE"),
        prop.max_col_tris,
        prop.coacd_max_hulls,
        col_obj_ptr.name if col_obj_ptr else None,
        len(col_obj_ptr.data.polygons) if (col_obj_ptr and col_obj_ptr.type == 'MESH' and col_obj_ptr.data) else 0,
        n_paths,
        n_auto,
        prop.look_mode,
        prop.custom_texture_path,
        prop.custom_thumb_png,
        getattr(prop, "skip_existing_thumbnails", True),
        obj.get("reskate_rendered_thumb_256", ""),
        len(obj.data.uv_layers),
        len(obj.data.materials),
        len(linked_items),
        tuple((getattr(it, "enabled", True), getattr(it, "light_obj", None).name if getattr(it, "light_obj", None) else None) for it in linked_items),
        len(obj.children),
        len(scene.objects) if scene else 0,
    )
    cached_ts, cached_sig, cached_dict = _STEP_STATUS_CACHE
    if cached_sig == state_sig and (now - cached_ts) < 0.20 and obj.mode != 'EDIT':
        return cached_dict

    # 1. Setup status
    disp_name = (prop.prop_display_name or "").strip()
    if not disp_name:
        s1 = ("❌", "Prop name is empty")
    elif tris == 0:
        s1 = ("❌", "Mesh has 0 triangles")
    elif not metrics["origin_flush"]:
        s1 = ("⚠️", f"Origin not at bottom ({tris:,} tris)")
    elif tris > 15000:
        s1 = ("⚠️", f"High poly ({tris:,} tris) — consider simplifying")
    else:
        s1 = ("✅", f"Ready ({tris:,} tris, {metrics['width']:.1f}×{metrics['height']:.1f}×{metrics['depth']:.1f}m)")

    # 2. Collision status
    col_mode = getattr(prop, "collision_mode", "SAME_AS_MESH")
    max_col = getattr(prop, "max_col_tris", 5000)
    if col_mode in ("NO_COLLISION", "NONE", "DECAL_ONLY"):
        nc_pm = getattr(prop, "no_collision_placement_mode", "FLAT_PLANE")
        pm_lbl = "Match 3D Mesh placement" if nc_pm == "MATCH_MESH" else "Flat Ground Plane placement"
        s2 = ("✅", f"No skater collision ({pm_lbl})")
    elif col_mode in ("CUSTOM_OBJECT", "AUTHORED_COL"):
        col_obj = compiler_bridge.resolve_custom_collision_object(context, obj)
        if not col_obj:
            s2 = ("❌", "Custom collision selected, but no object picked")
        else:
            c_tris = _fast_mesh_tri_count(col_obj.data)
            if c_tris > max_col:
                s2 = ("⚠️", f"Custom collision is {c_tris:,} tris, consider simplifying")
            else:
                s2 = ("✅", f"Custom object '{col_obj.name}' ({c_tris:,} tris)")
    elif col_mode in ("SIMPLE_HULLS", "CONVEX_HULL"):
        col_obj = compiler_bridge.resolve_custom_collision_object(context, obj)
        if col_obj:
            c_tris = _fast_mesh_tri_count(col_obj.data)
            s2 = ("✅", f"Geometric hull mesh '{col_obj.name}' ({c_tris:,} tris)")
        else:
            hulls = getattr(prop, "coacd_max_hulls", 8)
            s2 = ("✅", f"Simple hulls (up to {hulls} shapes)")
    else:
        h_stat = compiler_bridge.get_havok_ceiling_status(context, obj) if hasattr(compiler_bridge, "get_havok_ceiling_status") else {}
        if h_stat.get("exceeds_ceiling"):
            s2 = ("⚠️", f"Havok ceiling exceeded ({h_stat['welded_verts']:,}v / {h_stat['tris']:,}t > 2,600 / 5,000)")
        elif tris > max_col:
            s2 = ("⚠️", f"Collision is {tris:,} tris, consider simplifying")
        else:
            s2 = ("✅", f"1:1 Mesh + 8-Hull Placement ({tris:,} tris)")

    # 3. Grind status
    h_stat = compiler_bridge.get_havok_ceiling_status(context, obj) if hasattr(compiler_bridge, "get_havok_ceiling_status") else {}
    if col_mode in ("NO_COLLISION", "NONE", "DECAL_ONLY"):
        s3 = ("✅", "N/A (No skater collision)")
    elif h_stat.get("exceeds_ceiling") and (n_paths > 0 or n_auto > 0):
        s3 = ("⚠️", f"Grind edges not 1:1 (Havok ceiling exceeded: {h_stat['welded_verts']:,}v)")
    elif n_paths > 0 and n_auto > 0:
        s3 = ("✅", f"{n_paths} grind path(s) + {n_auto} marked edges")
    elif n_paths > 0:
        s3 = ("✅", f"{n_paths} grind path(s) configured")
    elif n_auto > 0:
        s3 = ("✅", f"{n_auto} grind edges marked")
    else:
        s3 = ("✅", "Optional (no grind edges marked)")

    # 4. Look (Textures) status
    look_info = compiler_bridge.inspect_mesh_look_status(obj)
    look_mode = getattr(prop, "look_mode", "AS_IS")
    use_ext_tex = bool(getattr(prop, "use_external_texture_override", False))
    if use_ext_tex and prop.custom_texture_path and _cached_path_exists(prop.custom_texture_path):
        s4 = ("✅", f"Ready (Override: {Path(prop.custom_texture_path).name})")
    elif look_info["can_use_as_is"] and look_mode == "AS_IS":
        s4 = ("✅", "Ready as-is (Auto-synced from Blender Shader)")
    elif look_info["has_baked_atlas"] or (look_mode == "BAKE" and prop.custom_texture_path and _cached_path_exists(prop.custom_texture_path)):
        s4 = ("✅", "Baked texture ready")
    else:
        s4 = ("⚠️", look_info["reason"])

    # 5. Light (in-game) status
    unsupported_lights = []
    valid_lights = []
    for item in linked_items:
        if not getattr(item, "enabled", True):
            continue
        l_o = getattr(item, "light_obj", None)
        if l_o and l_o.type == 'LIGHT' and l_o.data:
            if l_o.data.type not in ('SPOT', 'POINT'):
                unsupported_lights.append(l_o.name)
            else:
                valid_lights.append(l_o)
    for ch in obj.children:
        if ch.type == 'LIGHT' and ch not in valid_lights:
            if ch.data and ch.data.type not in ('SPOT', 'POINT'):
                unsupported_lights.append(ch.name)
            else:
                valid_lights.append(ch)

    if unsupported_lights:
        s5 = ("❌", f"Unsupported light type ({unsupported_lights[0]}: use Spot or Point)")
    elif valid_lights:
        s5 = ("✅", f"💡 {len(valid_lights)} light(s) linked to {obj.name}")
    else:
        s5 = ("✅", "Optional (no lights linked)")

    # 6. Thumbnail status (per-object aware)
    slug = compiler_bridge.get_prop_slug(context, obj)
    staging_dir = compiler_bridge.get_staging_dir(context)
    t256_str = str(obj.get("reskate_rendered_thumb_256", "")) or str(staging_dir / f"{slug}_thumb_256.png")
    owner = str(prop.get("custom_thumb_owner", "")) if hasattr(prop, "get") else ""
    skip_existing = bool(getattr(prop, "skip_existing_thumbnails", True))
    if prop.custom_thumb_png and _cached_path_exists(prop.custom_thumb_png) and (not owner or owner == obj.name):
        s6 = ("✅", f"Ready ({Path(prop.custom_thumb_png).name})")
    elif _cached_path_exists(t256_str):
        if skip_existing:
            s6 = ("✅", f"Rendered for '{obj.name}' (256 & 768 icons ready — reusing on compile)")
        else:
            s6 = ("✅", f"Rendered for '{obj.name}' (will re-render fresh on compile)")
    else:
        s6 = ("✅", f"Will auto-render '{obj.name}' on ship")

    # 7. Ship status (disabled if any step has ❌)
    blockers = [
        name for name, (badge, _reason) in (
            ("Setup", s1),
            ("Collision", s2),
            ("Grind", s3),
            ("Look", s4),
            ("Light", s5),
            ("Thumbnail", s6),
        )
        if badge == "❌"
    ]
    if blockers:
        s7 = ("❌", f"Fix {', '.join(blockers)} before shipping")
    else:
        s7 = ("✅", f"Ready to ship '{disp_name}'")

    result = {
        "setup": s1,
        "collision": s2,
        "grind": s3,
        "look": s4,
        "light": s5,
        "thumbnail": s6,
        "ship": s7,
    }
    _STEP_STATUS_CACHE = (now, state_sig, result)
    return result


def _draw_step_status_banner(layout, badge: str, reason: str):
    """Draws the one-line step status reason banner at the top of each step card."""
    row = layout.row(align=True)
    icon = 'CHECKMARK' if badge == "✅" else ('ERROR' if badge == "⚠️" else 'CANCEL')
    row.label(text=f"{badge} {reason}", icon=icon)


def _draw_viewport_overlay_toggles(layout, prop):
    """Draws the unified Viewport Overlay toggle bar at the top of the panel."""
    if not _props.is_viewport_overlays_master_enabled():
        return
    ov_box = layout.box()

    head_row = ov_box.row(align=True)
    head_row.label(text="Viewport Overlays (Default: Off for Clean Editing):", icon='RESTRICT_VIEW_OFF')
    head_row.operator("reskate.clear_all_overlays", text="Hide All", icon='HIDE_ON')
    ov_row1 = ov_box.row(align=True)
    ov_row1.prop(prop, "draw_grind_overlay", text="Grind Edges", toggle=True)
    ov_row1.prop(prop, "show_collision_overlay", text="Skate Collision", toggle=True)
    if hasattr(prop, "show_placement_proxy_overlay"):
        ov_row1.prop(prop, "show_placement_proxy_overlay", text="Placement Proxy", toggle=True)
    ov_row2 = ov_box.row(align=True)
    if hasattr(prop, "draw_face_surface_overlay"):
        ov_row2.prop(prop, "draw_face_surface_overlay", text="Face Surfaces", toggle=True)
    if hasattr(prop, "show_alignment_overlay"):
        ov_row2.prop(prop, "show_alignment_overlay", text="⚠️ Do Not Align", toggle=True)
    if hasattr(prop, "draw_light_overlay"):
        ov_row2.prop(prop, "draw_light_overlay", text="Lights", toggle=True)


def _draw_preflight_checklist(layout, context, obj, prop):
    """Draws the on-demand Pre-Flight Check box on Page 6 (only evaluates when clicking 'Run Pre-Flight Check' or before compile)."""
    if not obj or obj.type != 'MESH':
        return
    pf_checks = None
    raw_pf = str(obj.get("_reskate_last_preflight_json", "") or "").strip()
    if raw_pf:
        try:
            parsed = json.loads(raw_pf)
            if isinstance(parsed, list):
                pf_checks = parsed
        except Exception:
            pf_checks = None

    pf_box = layout.box()
    fast_outdated, fast_outdated_cnt, fast_outdated_reason = compiler_bridge.has_any_outdated_thumbnails_fast(context, obj)
    if pf_checks is None:
        pf_head = pf_box.row(align=True)
        pf_head.label(text="Pre-Flight Check (On-Demand & Auto Before Compile)", icon='CHECKMARK')
        run_btn = pf_head.row(align=True)
        run_btn.scale_y = 1.15
        run_btn.operator("reskate.run_preflight_check", text="Run Pre-Flight Check", icon='PLAY')
        if fast_outdated:
            thumb_warn_row = pf_box.row(align=True)
            thumb_warn_row.alert = True
            thumb_warn_row.label(
                text=f"⚠️ {fast_outdated_reason}" if fast_outdated_cnt == 1 else f"⚠️ {fast_outdated_cnt} prop thumbnail(s) out of date ({fast_outdated_reason})"
            )
            thumb_btn = thumb_warn_row.row(align=True)
            thumb_btn.alert = False
            thumb_btn.operator("reskate.rerender_outdated_thumbnails", text="Re-Render Out-of-Date Thumbnails", icon='RENDER_STILL')
        return

    non_pass = [c for c in pf_checks if c.get("badge") != "✅"]
    fixable = [c for c in non_pass if c.get("fix_action")]
    has_pf_thumb_warn = any(c.get("fix_action") == "RERENDER_OUTDATED_THUMBS" and c.get("badge") != "✅" for c in pf_checks)
    if non_pass:
        pf_box.alert = True
    pf_head = pf_box.row(align=True)
    show_det = bool(getattr(prop, "show_preflight_details", False))
    det_icon = 'TRIA_DOWN' if show_det else 'TRIA_RIGHT'
    if non_pass:
        head_txt = f"⚠️ Pre-Flight: {len(non_pass)} Item(s) Need Attention"
    else:
        head_txt = f"✅ All {len(pf_checks)} Pre-Flight Checks Passed"
    if hasattr(prop, "show_preflight_details"):
        pf_head.prop(prop, "show_preflight_details", text=head_txt, icon=det_icon, emboss=False)
    else:
        pf_head.label(text=head_txt, icon='ERROR' if non_pass else 'CHECKMARK')

    act_sub = pf_head.row(align=True)
    act_sub.alert = False
    act_sub.operator("reskate.run_preflight_check", text="Run Pre-Flight Check", icon='FILE_REFRESH')
    if fixable:
        act_sub.operator("reskate.preflight_fix_all", text="Fix All", icon='TOOL_SETTINGS')

    if (has_pf_thumb_warn or fast_outdated) and not show_det:
        thumb_quick_row = pf_box.row(align=True)
        thumb_quick_row.alert = False
        thumb_quick_row.operator(
            "reskate.rerender_outdated_thumbnails",
            text="Re-Render Out-of-Date Thumbnails",
            icon='RENDER_STILL',
        )

    if show_det:
        det_col = pf_box.column(align=True)
        det_col.alert = False
        for chk in pf_checks:
            c_row = det_col.row(align=True)
            if chk.get("badge") != "✅":
                c_row.alert = True
            c_row.label(text=f"{chk.get('badge', '•')} {chk.get('label', '')}")
            if chk.get("fix_action"):
                btn_sub = c_row.row(align=True)
                btn_sub.alert = False
                if chk["fix_action"] == "GENERATE_SIMPLIFIED_COL":
                    btn_sub.operator("reskate.create_simplified_col_copy", text="Simplify", icon='MOD_DECIM')
                elif chk["fix_action"] == "RERENDER_OUTDATED_THUMBS":
                    btn_sub.operator(
                        "reskate.rerender_outdated_thumbnails",
                        text=chk.get("fix_label") or "Re-Render Out-of-Date Thumbnails",
                        icon='RENDER_STILL',
                    )
                else:
                    fx = btn_sub.operator("reskate.preflight_fix", text=chk.get("fix_label") or "Fix", icon='TOOL_SETTINGS')
                    fx.fix_action = chk["fix_action"]
                if chk.get("secondary_fix_action"):
                    fx2 = btn_sub.operator(
                        "reskate.preflight_fix",
                        text=chk.get("secondary_fix_label") or "Keep Custom",
                        icon='LOCKED',
                    )
                    fx2.fix_action = chk["secondary_fix_action"]


def _draw_alignment_troubleshooting_alert(layout, context, obj, prop):
    """Draws a prominent alert card with 1-click repair whenever 'Do Not Align' or 'No-Grind' is active on the prop."""
    if obj is None:
        return
    inspect_fn = getattr(compiler_bridge, "inspect_prop_alignment_issues", None)
    if not callable(inspect_fn):
        return
    try:
        issues = inspect_fn(context, obj)
    except Exception:
        return
    if not isinstance(issues, dict):
        return
    has_dna = bool(issues.get("has_do_not_align", False))
    has_ng = bool(issues.get("has_no_grind", False))
    if not has_dna and not has_ng:
        return

    alert_box = layout.box()
    alert_box.alert = True
    head_row = alert_box.row(align=True)
    head_row.label(
        text="⚠️ Alignment / Grind Lock-On Blocked on This Prop!",
        icon='ERROR',
    )
    if hasattr(prop, "show_alignment_overlay"):
        head_row.prop(
            prop,
            "show_alignment_overlay",
            text="GPU Highlight",
            toggle=True,
            icon='HIDE_OFF',
        )

    col = alert_box.column(align=True)
    col.scale_y = 0.85
    for line in (issues.get("summary_lines") or [])[:4]:
        col.label(text=f"• {line}")
    if has_dna:
        col.label(
            text="ℹ️ 'Do Not Align' disables PropertySlot 71 (IncludeInSurfaceAnalysis), preventing grind/ledge lock-on."
        )

    fix_row = alert_box.row(align=True)
    fix_row.scale_y = 1.25
    fix_row.alert = False
    fix_row.operator(
        "reskate.clear_do_not_align",
        text="Clear 'Do Not Align' & Restore Grind Alignment (1-Click)",
        icon='CHECKMARK',
    )


def _draw_havok_ceiling_warning(layout, context, obj, source_panel: str = "") -> bool:
    """Renders a prominent alert warning box whenever the active prop exceeds Havok's 1:1 single-shape limit."""
    if obj is None or obj.type != 'MESH':
        return False

    status_fn = getattr(compiler_bridge, "get_havok_ceiling_status", None)
    if not callable(status_fn):
        return False

    try:
        status = status_fn(context, obj)
    except Exception:
        return False

    if not isinstance(status, dict) or not status.get("exceeds_ceiling", False):
        return False

    welded_v = int(status.get("welded_verts", 0))
    tris = int(status.get("tris", 0))
    max_v = int(status.get("max_verts", 2600))
    max_t = int(status.get("max_tris", 5000))

    box = layout.box()
    box.alert = True

    h_row = box.row(align=True)
    h_row.label(
        text=f"⚠️ Havok 1:1 Ceiling Exceeded: {welded_v:,} verts / {tris:,} tris (Max {max_v:,} / {max_t:,})",
        icon='ERROR',
    )

    col = box.column(align=True)
    col.scale_y = 0.90
    col.label(
        text="Mesh will be auto-decimated at compile time. In-game grind edges may not match visible geometry 1:1!",
        icon='INFO',
    )

    if source_panel == "collision":
        col.label(text="• Authoring an explicit simplified mesh ensures predictable physical collision & crisp grind edges.")
    elif source_panel == "ship":
        col.label(text="• High vertex count will trigger Frostbite's automatic decimation during packaging.")

    btn_col = box.column(align=True)
    btn_col.scale_y = 1.2
    btn_col.alert = False
    btn_col.operator(
        "reskate.generate_collision_preview",
        text="Generate Simplified Collision (_COL) (Recommended)",
        icon="MOD_DECIM",
    )
    return True


def _draw_edge_straightener_box(layout, prop, active_raw, col_proxy=None):
    """Draws the Non-Destructive Edge Straightener box in 2. Collision & Grind (auto-expanded in Edit Mode)."""
    is_edit = bool(active_raw and active_raw.type == 'MESH' and active_raw.mode == 'EDIT')
    show_str = is_edit or bool(getattr(prop, "show_edge_straightener", False))
    str_box = layout.box()
    s_head = str_box.row(align=True)
    if is_edit:
        s_head.label(text="📐 Align / Straighten Selected Edges (Edit Mode):", icon='MOD_SIMPLIFY')
    elif hasattr(prop, "show_edge_straightener"):
        s_icon = 'TRIA_DOWN' if show_str else 'TRIA_RIGHT'
        s_head.prop(
            prop,
            "show_edge_straightener",
            text="📐 Align / Straighten Collision & Grind Edges",
            icon=s_icon,
            emboss=False,
        )
    else:
        s_head.label(text="📐 Align / Straighten Collision & Grind Edges:", icon='MOD_SIMPLIFY')

    if not show_str:
        return

    if not is_edit:
        hint_row = str_box.row(align=True)
        if col_proxy:
            hint_row.operator(
                "reskate.toggle_edit_collision_mesh",
                text=f"Enter Edit Mode on {col_proxy.name} to Select Edges",
                icon='EDITMODE_HLT',
            )
        else:
            hint_row.label(text="Tip: Select edges in Edit Mode and click Apply to straighten:", icon='INFO')

    str_box.prop(prop, "col_straighten_mode", text="Align Mode")
    str_box.prop(prop, "col_straighten_strength", text="Snap Strength", slider=True)
    if getattr(prop, "col_straighten_mode", "LINE") == "ARC":
        str_box.prop(prop, "col_straighten_arc", text="Arc Smoothness / Bulge", slider=True)
    s_btn_row = str_box.row(align=True)
    s_btn_row.scale_y = 1.15
    s_btn_row.operator("reskate.straighten_collision_edges", text="Apply / Update Edge Snap", icon='SNAP_EDGE')
    s_sub_row = str_box.row(align=True)
    s_sub_row.operator("reskate.lock_collision_edge_baseline", text="Lock Baseline", icon='LOCKED')
    s_sub_row.operator("reskate.reset_collision_edges", text="Reset to Original", icon='LOOP_BACK')


def _draw_batch_copy_row(layout, context):
    """When 2+ prop meshes are selected, draws a 1-click Batch Copy bar for Collision / Finish / Both."""
    sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
    if len(sel_meshes) <= 1:
        return
    others = len(sel_meshes) - 1
    b_box = layout.box()
    b_box.label(text=f"Batch Copy Active Prop Settings to {others} Selected Prop(s):", icon='COPYDOWN')
    b_row = b_box.row(align=True)
    op_c = b_row.operator("reskate.copy_settings_to_selected", text="Copy Collision", icon='PHYSICS')
    op_c.copy_mode = 'COLLISION'
    op_f = b_row.operator("reskate.copy_settings_to_selected", text="Copy Finish", icon='SHADING_RENDERED')
    op_f.copy_mode = 'FINISH'
    op_b = b_row.operator("reskate.copy_settings_to_selected", text="Copy Both", icon='DUPLICATE')
    op_b.copy_mode = 'BOTH'


# ============================================================================
# Simple Mode 6-Page Wizard Helpers (§3, §4, §5, §6, §7)
# ============================================================================

def _draw_sanctus_prop_carousel_header(layout, context, prop, obj):
    """Draws the persistent Sanctus-style prop carousel & clickable thumbnail selector on ALL 6 Simple Mode pages."""
    car_box = layout.box()
    sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
    active = (obj if (obj is not None and obj in sel_meshes) else (sel_meshes[0] if sel_meshes else None))

    # Top row: [ ◀ ] [ Large Clickable Thumbnail (template_icon_view) ] [ ▶ ]
    main_row = car_box.row(align=True)

    left_col = main_row.column(align=True)
    left_col.scale_x = 1.15
    left_col.scale_y = 5.4
    left_col.enabled = len(sel_meshes) >= 2
    prev_op = left_col.operator("reskate.carousel_step", text="", icon='TRIA_LEFT')
    prev_op.direction = -1

    center_col = main_row.column(align=True)
    if hasattr(prop, "sanctus_carousel_prop"):
        center_col.template_icon_view(prop, "sanctus_carousel_prop", show_labels=True, scale=5.4, scale_popup=4.5)
    else:
        center_col.label(text=active.name if active else "No Prop Selected", icon='MESH_DATA')

    right_col = main_row.column(align=True)
    right_col.scale_x = 1.15
    right_col.scale_y = 5.4
    right_col.enabled = len(sel_meshes) >= 2
    next_op = right_col.operator("reskate.carousel_step", text="", icon='TRIA_RIGHT')
    next_op.direction = 1

    # Bottom status row: Prop Name (+ Variant Family badge if grouped)
    if active is not None:
        disp_name = str(active.get("reskate_display_name", "") or "").strip() or active.name
        fam_title = str(active.get("reskate_family_title", "") or getattr(prop, "prop_family_title", "") or "").strip()
        try:
            idx = sel_meshes.index(active) + 1
        except ValueError:
            idx = 1

        title_row = car_box.row(align=True)
        title_row.alignment = 'CENTER'
        fam_suffix = f"  |  🔗 Family: {fam_title}" if fam_title else ""
        if len(sel_meshes) >= 2:
            title_row.label(text=f"Editing Prop {idx} of {len(sel_meshes)}: {disp_name} ({active.name}){fam_suffix}", icon='OBJECT_DATA')
        else:
            title_row.label(text=f"{disp_name} ({active.name}){fam_suffix}", icon='OBJECT_DATA')
    else:
        empty_row = car_box.row(align=True)
        empty_row.alignment = 'CENTER'
        empty_row.label(text="No Prop Selected — Select a 3D mesh prop in the viewport to begin.", icon='INFO')

    # Quick-action row: Frame Selected (left) & Select All in Pack (right)
    all_scene_props = compiler_bridge.get_all_scene_prop_meshes(context)
    pack_scene_props = compiler_bridge.get_pack_prop_meshes_in_scene(context)
    n_scene_total = len(all_scene_props)
    n_pack_total = len(pack_scene_props)
    real_sel = sel_meshes
    real_sel_names = {o.name for o in real_sel}

    if n_scene_total > 0 or len(real_sel) > 0:
        sel_bar = car_box.row(align=True)
        sel_bar.scale_y = 1.15
        left_sub = sel_bar.row(align=True)
        left_sub.enabled = len(real_sel) > 0
        left_sub.operator(
            "reskate.frame_selected_props",
            text="Frame Selected",
            icon='ZOOM_SELECTED',
        )
        right_sub = sel_bar.row(align=True)
        right_sub.enabled = n_pack_total > 0
        all_pack_selected = (
            n_pack_total > 0
            and len(real_sel_names) == n_pack_total
            and all(o.name in real_sel_names for o in pack_scene_props)
        )
        op_pk = right_sub.operator(
            "reskate.select_pack_or_scene_props",
            text=f"Select All in Pack ({n_pack_total})",
            icon='PACKAGE',
            depress=all_pack_selected,
        )
        op_pk.mode = 'PACK'
        op_pk.target_pack = ""
        op_pk.frame_view = False


def _draw_scale_references_twirldown(layout, prop):
    """Draws the top-level collapsible Scale References twirl-down available across all pages."""
    ref_box = layout.box()
    ref_head = ref_box.row(align=True)
    show_ref = bool(getattr(prop, "show_scale_references_box", False))
    if hasattr(prop, "show_scale_references_box"):
        ref_head.prop(
            prop,
            "show_scale_references_box",
            text="📏 Scale References",
            icon='TRIA_DOWN' if show_ref else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        ref_head.label(text="📏 Scale References", icon='USER')

    if show_ref:
        ref_row = ref_box.row(align=True)
        ref_row.scale_y = 1.25
        ref_row.operator("reskate.toggle_skater_ref", text="Spawn Skater", icon='USER')
        ref_row.operator("reskate.toggle_skateboard_ref", text="Spawn Skateboard", icon='MESH_CAPSULE')
        ref_row.operator("reskate.toggle_ground_plane_ref", text="Spawn Ground Plane", icon='MESH_PLANE')
        ref_row.operator("reskate.clear_scale_references", text="Remove All", icon='TRASH')


def _draw_simple_page1_scale_origin(layout, context, prop, obj, metrics):
    """Page 1 — Scale & Origin (§3 Page 1, §4.1, §4.2, §4.3)."""
    p_box = layout.box()
    p_box.label(text="Page 1 — Scale & Origin", icon='OBJECT_ORIGIN')

    # 1. Dimensions Readout
    dim_box = p_box.box()
    if metrics is not None:
        dim_box.label(
            text=f"Dimensions: {metrics['width']:.2f} m × {metrics['depth']:.2f} m × {metrics['height']:.2f} m",
            icon='CON_SIZELIMIT',
        )
        dim_box.label(
            text=f"Mesh: {metrics['tris']:,} Triangles ({metrics['verts']:,} Vertices)  |  1 Blender m = 1 Game m",
            icon='MESH_DATA',
        )
    else:
        dim_box.label(text="Select a 3D mesh prop in the viewport to view dimensions.", icon='INFO')

    # 2. Origin
    orig_box = p_box.box()
    orig_box.label(text="Origin", icon='OBJECT_ORIGIN')
    orig_col = orig_box.column(align=True)
    orig_col.scale_y = 1.25
    orig_col.enabled = obj is not None
    orig_col.operator("reskate.origin_to_ground_contact", text="Origin to Ground Contact (Recommended)", icon='SNAP_ON')
    orig_col.operator("reskate.origin_to_bottom_center", text="Origin to Bottom Center", icon='PIVOT_BOUNDBOX')
    orig_col.operator("reskate.origin_to_geometry_center", text="Origin to Geometry Center", icon='PIVOT_MEDIAN')

    has_any_active = (obj is not None) or (getattr(context, "active_object", None) is not None) or bool(getattr(context, "selected_objects", None))
    flush_row = orig_box.row(align=True)
    flush_row.scale_y = 1.25
    flush_row.enabled = has_any_active
    flush_row.operator("reskate.drop_prop_flush_to_ground", text="Set Prop Flush to Ground (Z = 0)", icon='IMPORT')

    ts = getattr(context.scene, "tool_settings", None)
    is_moving_origin = bool(ts and getattr(ts, "use_transform_data_origin", False))
    mv_row = orig_box.row(align=True)
    mv_row.scale_y = 1.25
    mv_row.enabled = obj is not None
    mv_row.operator(
        "reskate.toggle_interactive_origin_edit",
        text="Done Moving Origin" if is_moving_origin else "Move Origin Point",
        icon='CHECKMARK' if is_moving_origin else 'ORIENTATION_GLOBAL',
        depress=is_moving_origin,
    )
    if is_moving_origin:
        orig_box.label(text="Drag the Move gizmo arrows to position the origin, then click 'Done Moving Origin'.", icon='INFO')


def _draw_simple_page2_collision(layout, context, prop, obj, metrics):
    """Page 2 — Collision (§3 Page 2)."""
    p_box = layout.box()
    p_box.label(text="Page 2 — Collision", icon='PHYSICS')

    col_box = p_box.box()
    col_box.prop(prop, "collision_mode_ui", text="Collision Mode")
    col_mode = getattr(prop, "collision_mode", "SAME_AS_MESH")

    if col_mode in ("SIMPLE_HULLS", "CONVEX_HULL", "AUTO_CONVEX"):
        col_box.prop(prop, "coacd_max_hulls", text="Max Hulls", slider=True)
        col_box.prop(prop, "coacd_concavity", text="Concavity", slider=True)
    elif col_mode in ("CUSTOM_OBJECT", "AUTHORED_COL", "CUSTOM_COL"):
        col_box.prop(prop, "custom_collision_object", text="Collision Object", icon='OBJECT_DATA')
    elif col_mode == "NO_COLLISION":
        if hasattr(prop, "no_collision_placement_mode"):
            col_box.prop(prop, "no_collision_placement_mode", text="Placement Shape")
            nc_mode = str(getattr(prop, "no_collision_placement_mode", "FLAT_PLANE") or "FLAT_PLANE")
            if nc_mode == "MATCH_MESH":
                col_box.label(
                    text="ℹ️ Skater passes through prop, while Quick Drop cursor grabs the 3D mesh shape.",
                    icon='INFO',
                )
            else:
                col_box.label(
                    text="ℹ️ Skater passes through prop; Quick Drop uses a flat ground-plane placement wafer.",
                    icon='INFO',
                )

    if col_mode not in ("SAME_AS_MESH", "EXACT_MESH", "EXACT_1_1", "NO_COLLISION"):
        chk_row = col_box.row(align=True)
        chk_row.scale_y = 1.35
        chk_row.enabled = obj is not None
        chk_row.operator("reskate.check_collision", text="Check Collision", icon='PHYSICS')

    # Inline summary & warning conditions (§3 Page 2)
    sum_box = p_box.box()
    raw_sum = str(obj.get("reskate_col_check_summary_json", "") or "") if obj else ""
    parsed_sum = None
    if raw_sum:
        try:
            import json
            parsed_sum = json.loads(raw_sum)
        except Exception:
            parsed_sum = None

    if col_mode in ("SAME_AS_MESH", "EXACT_MESH", "EXACT_1_1") and metrics is not None:
        tris = metrics["tris"]
        sum_box.label(
            text=f"✅ 1:1 Visible Mesh Collision Active ({tris:,} triangles)",
            icon='CHECKMARK',
        )
        if tris > 8000:
            w_row = sum_box.row(align=True)
            w_row.alert = True
            w_row.label(text=f"Too many triangles ({tris:,} > 8,000) — switch to Simple Hulls or simplify.", icon='ERROR')
    elif isinstance(parsed_sum, dict):
        sh_cnt = int(parsed_sum.get("shape_count", 1))
        tr_cnt = int(parsed_sum.get("triangle_count", 0))
        ex_cnt = int(parsed_sum.get("excluded_decal_triangles", 0) or 0)
        warns = [w for w in list(parsed_sum.get("warnings") or []) if "thin" not in w.lower()]
        ex_suffix = f" ({ex_cnt:,} decal/non-solid tris excluded in Red)" if ex_cnt > 0 else ""
        sum_box.label(
            text=f"Collision Checked: {sh_cnt} shape(s), {tr_cnt:,} solid triangles{ex_suffix}",
            icon='CHECKMARK' if not warns else 'ERROR',
        )
        col_p_name = str(parsed_sum.get("col_proxy_name", "") or "")
        pphys_p_name = str(parsed_sum.get("pphys_proxy_name", "") or "")
        if col_p_name or pphys_p_name:
            sum_box.label(
                text=f"Generated Proxies: {col_p_name or 'N/A'} (Skate)  |  {pphys_p_name or 'N/A'} (Placement)",
                icon='MESH_ICOSPHERE',
            )
        for w_msg in warns:
            w_row = sum_box.row(align=True)
            w_row.alert = True
            w_row.label(text=w_msg, icon='ERROR')
    elif metrics is not None:
        tris = metrics["tris"]
        if col_mode == "NO_COLLISION":
            sum_box.label(text="✅ No Skater Collision Active (Prop is placement-only)", icon='CHECKMARK')
        else:
            sum_box.label(text=f"Current Mesh: {tris:,} triangles (Click 'Check Collision' to preview shapes)", icon='INFO')
    else:
        sum_box.label(text="Select a 3D mesh prop to configure collision.", icon='INFO')

    tip_row = sum_box.row(align=True)
    tip_row.label(
        text="Ramp Tip: Ultra-flat feathered ramp entries can buck the skater — prop the entry to a slightly steeper transition angle from flat ground.",
        icon='LIGHT',
    )


def _draw_multi_shader_atlas_warning(layout, obj, prop=None):
    """Draws a layman's warning box when a prop has > 1 material slots explaining Frostbite texture atlas resolution sharing."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return
    n_slots = len(getattr(obj, "material_slots", ()) or ())
    if n_slots <= 1:
        return
    atlas_res = 2048
    if prop is not None:
        try:
            atlas_res = int(getattr(prop, "atlas_resolution", 2048) or 2048)
        except Exception:
            atlas_res = 2048
    grid_dim = max(2, int(math.ceil(math.sqrt(n_slots))))
    raw_px = float(atlas_res) / float(grid_dim)
    eff_px = int(round(raw_px / 10.0) * 10) if grid_dim == 3 else int(round(raw_px))

    w_box = layout.box()
    w_box.alert = True
    w_head = w_box.row(align=True)
    w_head.label(
        text=f"⚠️ Multi-Shader Atlas Notice: {n_slots} shaders on a {atlas_res} atlas = ~{eff_px}px per shader",
        icon='ERROR',
    )
    w_col = w_box.column(align=True)
    w_col.alert = False
    w_col.scale_y = 0.88
    w_col.label(
        text="• Skate. packs all material slots on a prop into one shared texture sheet (atlas).",
    )
    w_col.label(
        text="• More material slots = smaller texture slice per shader (lower in-game sharpness). Combine into 1 shader for full crispness.",
    )


def _draw_simple_page3_surfaces(layout, context, prop, obj):
    """Page 3 — Surfaces & Textures (§3 Page 3, §6.1)."""
    p_box = layout.box()
    p_box.label(text="Page 3 — Surfaces & Textures", icon='MATERIAL')

    if obj is None or obj.type != 'MESH':
        p_box.label(text="Select a 3D mesh prop to assign surfaces.", icon='INFO')
        return

    slots = list(obj.material_slots)
    _draw_multi_shader_atlas_warning(p_box, obj, prop)

    surf_box = p_box.box()
    surf_box.label(text=f"Material Surfaces ({max(1, len(slots))} Slot{'s' if len(slots) != 1 else ''})", icon='SOUND')

    active_idx = max(0, min(int(getattr(obj, "active_material_index", 0) or 0), max(0, len(slots) - 1)))
    is_obj_in_edit = (getattr(obj, "mode", "OBJECT") == 'EDIT')
    decal_mats = []
    if len(slots) == 0 or all(s.material is None for s in slots):
        r = surf_box.row(align=True)
        r.prop(prop, "primary_surface", text="Default Surface")
        hl_op = r.operator(
            "reskate.highlight_surface_material",
            text="Exit Select" if is_obj_in_edit else "Select Material",
            icon='EDITMODE_HLT' if is_obj_in_edit else 'RESTRICT_SELECT_OFF',
            depress=is_obj_in_edit,
        )
        hl_op.slot_index = 0
        hl_op.enter_edit_mode_select = True
    else:
        for idx, slot in enumerate(slots):
            mat = slot.material
            is_sel_slot = (idx == active_idx) and (len(slots) > 1)
            is_edit_hl_slot = is_obj_in_edit and (idx == active_idx)
            row = surf_box.row(align=True)
            if mat is not None:
                sk8 = _sync_shader_role_display_value(obj, idx, mat)
                sel_op = row.operator(
                    "reskate.highlight_surface_material",
                    text=mat.name,
                    icon='RADIOBUT_ON' if is_sel_slot else 'MATERIAL',
                    depress=is_sel_slot,
                )
                sel_op.slot_index = idx
                sel_op.enter_edit_mode_select = False
                if sk8 is not None and hasattr(sk8, "surface_role_preset"):
                    row.prop(sk8, "surface_role_preset", text="")
                    if getattr(sk8, "is_decal_slot", False) or getattr(sk8, "surface_role_preset", "") == "DECAL_OVERLAY":
                        decal_mats.append((mat, sk8))
                else:
                    row.prop(prop, "primary_surface", text="")
            else:
                sel_op = row.operator(
                    "reskate.highlight_surface_material",
                    text=f"Slot {idx + 1} (Empty)",
                    icon='RADIOBUT_ON' if is_sel_slot else 'MATERIAL',
                    depress=is_sel_slot,
                )
                sel_op.slot_index = idx
                sel_op.enter_edit_mode_select = False
                row.prop(prop, "primary_surface", text="")
            hl_op = row.operator(
                "reskate.highlight_surface_material",
                text="Exit Select" if is_edit_hl_slot else "Select Material",
                icon='EDITMODE_HLT' if is_edit_hl_slot else 'RESTRICT_SELECT_OFF',
                depress=is_edit_hl_slot,
            )
            hl_op.slot_index = idx
            hl_op.enter_edit_mode_select = True

    # Auto-appearing Decal Mode box when any slot is set to Decal / Sticker Overlay
    for dec_mat, dec_sk8 in decal_mats:
        dec_box = p_box.box()
        dec_box.label(text=f"Decal / Cutout Settings ({dec_mat.name}):", icon='MOD_MASK')
        if hasattr(dec_sk8, "decal_shader_mode"):
            dec_box.prop(dec_sk8, "decal_shader_mode", text="Decal Mode")
        if hasattr(dec_sk8, "decal_alpha_mode"):
            dec_box.prop(dec_sk8, "decal_alpha_mode", text="Alpha Source")
        dec_mode = getattr(dec_sk8, "decal_shader_mode", "CUTOUT_273") or "CUTOUT_273"
        if dec_mode == "METALLIC_771":
            dec_box.label(text="✅ 0x771 Metallic PBR Decal (Excluded from skater collision)", icon='CHECKMARK')
        else:
            dec_box.label(text="✅ 0x273 Alpha Cutout Decal (Excluded from skater collision)", icon='CHECKMARK')

    sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
    if len(sel_meshes) >= 2:
        cp_row = p_box.row(align=True)
        cp_row.scale_y = 1.25
        cp_op = cp_row.operator("reskate.copy_settings_to_selected", text="Copy Surfaces to Selected", icon='COPYDOWN')
        cp_op.copy_mode = 'SURFACES'

    # Collapsible Read-Only Shader Texture Previews twirl-down (synced with active Material Surfaces slot!)
    active_mat = (
        slots[active_idx].material
        if (slots and 0 <= active_idx < len(slots) and slots[active_idx].material is not None)
        else (obj.active_material or (slots[0].material if slots and slots[0].material else None))
    )
    tex_box = p_box.box()
    show_tex_prev = bool(getattr(prop, "show_shader_texture_previews", True))
    th_row = tex_box.row(align=True)
    mat_title = f"Shader Texture Previews ({active_mat.name})" if active_mat else "Shader Texture Previews"
    if hasattr(prop, "show_shader_texture_previews"):
        th_row.prop(
            prop,
            "show_shader_texture_previews",
            text=mat_title,
            icon='TRIA_DOWN' if show_tex_prev else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        th_row.label(text=mat_title, icon='TEXTURE')
    if active_mat is not None:
        th_row.operator("reskate.preview_material_textures_popup", text="Large Preview", icon='ZOOM_IN')

    if show_tex_prev:
        if active_mat is None:
            tex_box.label(text="No material assigned yet — expand Starter Material Templates below.", icon='INFO')
        else:
            shader_info = compiler_bridge.inspect_material_shader_nodes(active_mat)
            sk8_m = getattr(active_mat, "reskate_mat", None) or getattr(active_mat, "sk8_material", None)
            use_ovr = bool(getattr(sk8_m, "use_custom_texture_overrides", False)) if sk8_m else False
            b_img = (getattr(sk8_m, "base_color_image", None) if use_ovr else None) or shader_info.get("base_color_image") or (getattr(sk8_m, "base_color_image", None) if sk8_m else None)
            n_img = (getattr(sk8_m, "normal_image", None) if use_ovr else None) or shader_info.get("normal_image") or (getattr(sk8_m, "normal_image", None) if sk8_m else None)
            r_img = (getattr(sk8_m, "roughness_image", None) if use_ovr else None) or shader_info.get("roughness_image") or (getattr(sk8_m, "roughness_image", None) if sk8_m else None)
            m_img = (getattr(sk8_m, "metallic_image", None) if use_ovr else None) or shader_info.get("metallic_image") or (getattr(sk8_m, "metallic_image", None) if sk8_m else None)

            present_cnt = sum(1 for im in (b_img, n_img, r_img, m_img) if im is not None)
            if present_cnt == 4:
                tex_box.label(text="✅ All 4 PBR Texture Maps Detected from Principled BSDF (Auto-Passthrough)", icon='CHECKMARK')
            elif present_cnt > 0:
                tex_box.label(text=f"🖼️ {present_cnt}/4 PBR Maps Detected from Principled BSDF (Auto-Passthrough)", icon='IMAGE_DATA')
            else:
                tex_box.label(text="ℹ️ Solid Principled BSDF Values (Connect Image Textures for Custom Maps)", icon='NODE_MATERIAL')

            for slot_label, active_img in (
                ("Base Color", b_img),
                ("Normal Map", n_img),
                ("Roughness", r_img),
                ("Metallic", m_img),
            ):
                s_row = tex_box.row(align=True)
                if active_img is not None:
                    try:
                        active_img.preview_ensure()
                        if getattr(active_img, "preview", None) and getattr(active_img.preview, "icon_id", 0):
                            s_row.template_icon(icon_value=active_img.preview.icon_id, scale=2.2)
                    except Exception:
                        pass
                    w_px = int(active_img.size[0]) if len(active_img.size) > 0 else 0
                    h_px = int(active_img.size[1]) if len(active_img.size) > 1 else 0
                    res_txt = f" ({w_px}×{h_px})" if (w_px > 0 and h_px > 0) else ""
                    s_row.label(text=f"✅ {slot_label}: {active_img.name}{res_txt}", icon='IMAGE_DATA')
                else:
                    s_row.label(text=f"• {slot_label}: Flat Shader Value (No Image Texture)", icon='DOT')

    # Collapsible Starter Material Templates twirl-down
    pbr_box = p_box.box()
    pbr_head = pbr_box.row(align=True)
    show_tpl = bool(getattr(prop, "show_starter_templates", False))
    tpl_icon = 'TRIA_DOWN' if show_tpl else 'TRIA_RIGHT'
    if hasattr(prop, "show_starter_templates"):
        pbr_head.prop(
            prop,
            "show_starter_templates",
            text="🎨 Starter Material Templates",
            icon=tpl_icon,
            emboss=False,
        )
    else:
        pbr_head.label(text="🎨 Starter Material Templates", icon='MATERIAL')

    if show_tpl:
        curr_preset = str(getattr(prop, "look_preset", "CUSTOM") or "CUSTOM").upper()
        tpl_row1 = pbr_box.row(align=True)
        tpl_row1.scale_y = 1.2
        for t_key, t_lbl, t_icon in (
            ("CONCRETE", "Concrete", 'MOD_BUILD'),
            ("WOOD", "Wood", 'TEXTURE'),
            ("METAL", "Metal", 'SHADING_RENDERED'),
        ):
            op_t = tpl_row1.operator(
                "reskate.apply_starter_material_template",
                text=t_lbl,
                icon=t_icon,
                depress=(curr_preset == t_key),
            )
            op_t.template = t_key
            op_t.template_id = t_key
        tpl_row2 = pbr_box.row(align=True)
        tpl_row2.scale_y = 1.2
        for t_key, t_lbl, t_icon in (
            ("ASPHALT", "Asphalt", 'FORCE_TURBULENCE'),
            ("MARBLE", "Marble", 'NODE_MATERIAL'),
            ("PAINTED", "Painted", 'BRUSH_DATA'),
        ):
            op_t = tpl_row2.operator(
                "reskate.apply_starter_material_template",
                text=t_lbl,
                icon=t_icon,
                depress=(curr_preset == t_key),
            )
            op_t.template = t_key
            op_t.template_id = t_key

    # Advisory Missing UV Map notice (never destructively repacks or overwrites UVs)
    any_missing_uv = any(
        (m.type == 'MESH' and m.data and len(m.data.uv_layers) == 0)
        for m in (sel_meshes if sel_meshes else [obj])
    )
    if any_missing_uv:
        uv_box = p_box.box()
        uv_box.alert = True
        uv_box.label(text="⚠️ Missing UV Map detected — unwrap UVs manually in Edit Mode before using image textures.", icon='ERROR')



def _draw_simple_page4_lights(layout, context, prop, obj):
    """Page 4 — Lights (In-Game) (§3 Page 4)."""
    p_box = layout.box()
    p_box.label(text="Page 4 — Lights (In-Game)", icon='LIGHT')
    p_box.label(text="Optional — add Spot or Point lights that illuminate the prop in-game.", icon='INFO')

    add_row = p_box.row(align=True)
    add_row.scale_y = 1.35
    add_row.enabled = obj is not None
    op_spot = add_row.operator("reskate.add_dynamic_prop_light", text="+ Add Spot Light", icon='LIGHT_SPOT')
    op_spot.light_type = 'SPOT'
    op_pt = add_row.operator("reskate.add_dynamic_prop_light", text="+ Add Point Light", icon='LIGHT_POINT')
    op_pt.light_type = 'POINT'

    # Linked lights list & per-light controls (Color, Intensity, Cone Angle, Radius, Shadows)
    l_box = p_box.box()
    head = l_box.row(align=True)
    head.label(text="Lights on this prop:", icon='LIGHT')
    head.operator("reskate.link_light_slot", text="Link Existing Light", icon='ADD')

    if obj and hasattr(obj, "reskate_linked_lights") and len(obj.reskate_linked_lights) > 0:
        l_box.template_list(
            "RESKATE_UL_linked_lights", "reskate_simple_lights",
            obj, "reskate_linked_lights",
            obj, "reskate_linked_light_index",
            rows=2, maxrows=4,
        )
        idx = obj.reskate_linked_light_index
        if 0 <= idx < len(obj.reskate_linked_lights):
            sel_slot = obj.reskate_linked_lights[idx]
            l_obj = getattr(sel_slot, "light_obj", None)
            if l_obj and l_obj.type == 'LIGHT' and l_obj.data:
                l_data = l_obj.data
                d_box = l_box.box()
                d_col = d_box.column(align=True)
                d_col.label(text=f"Selected Light ({l_obj.name}):", icon='PREFERENCES')
                act_row = d_col.row(align=True)
                sel_l_op = act_row.operator("reskate.select_linked_light", text="Select in Viewport", icon='RESTRICT_SELECT_OFF')
                sel_l_op.index = idx
                del_l_op = act_row.operator("reskate.delete_linked_light", text="Delete Light", icon='TRASH')
                del_l_op.index = idx
                d_col.separator(factor=0.3)
                d_col.prop(l_data, "color", text="Color")
                d_col.prop(l_data, "energy", text="Intensity (W)")
                if l_data.type == 'SPOT':
                    d_col.prop(l_data, "spot_size", text="Cone Angle")
                    d_col.prop(l_data, "spot_blend", text="Cone Softness", slider=True)
                d_col.prop(l_data, "use_custom_distance", text="Use Custom Radius")
                if getattr(l_data, "use_custom_distance", False):
                    d_col.prop(l_data, "cutoff_distance", text="Radius (m)")
                d_col.prop(l_data, "use_shadow", text="Cast Shadows")
    else:
        l_box.label(text="No lights linked yet — click '+ Add Spot Light' or '+ Add Point Light' above.", icon='INFO')


def _draw_clickable_qd_tabs_list(layout, context, prop):
    """Draws a collapsible twirl-down menu for Quick Drop Tab Names used in the current .blend scene (defaulted to collapsed)."""
    if hasattr(_props, "get_scene_qd_tabs_with_counts"):
        tabs_info = _props.get_scene_qd_tabs_with_counts(context)
    elif hasattr(_props, "get_pack_and_scene_qd_tabs"):
        tabs_info = _props.get_pack_and_scene_qd_tabs(context)
    else:
        return
    if not tabs_info:
        return

    t_box = layout.box()
    t_head = t_box.row(align=True)
    show_qd = bool(getattr(prop, "show_qd_tabs_foldout", False)) if prop is not None else False
    if prop is not None and hasattr(prop, "show_qd_tabs_foldout"):
        t_head.prop(
            prop,
            "show_qd_tabs_foldout",
            text=f"🗂️ Quick Drop Tabs in Scene ({len(tabs_info)} — Click to Assign)",
            icon='TRIA_DOWN' if show_qd else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        t_head.label(text="Quick Drop Tabs in Scene (Click to Assign):", icon='BOOKMARKS')

    if not show_qd:
        return

    saved_set = {
        _props._normalize_qd_tab_key(t)
        for t in (_props.load_custom_qd_tabs() if hasattr(_props, "load_custom_qd_tabs") else [])
    }
    cur_val = _props.format_qd_tab_display_name(str(getattr(prop, "custom_qd_tab", "") or ""))
    cur_key = _props._normalize_qd_tab_key(cur_val)
    t_col = t_box.column(align=True)
    for t_item in tabs_info:
        if isinstance(t_item, dict):
            t_name = str(t_item.get("tab_name", "") or "").strip()
            cnt = int(t_item.get("count", 0) or 0)
            is_act = bool(t_item.get("is_active", False))
            is_saved = bool(t_item.get("is_saved", False))
        else:
            t_name = str(t_item or "").strip()
            cnt = 0
            t_key = _props._normalize_qd_tab_key(t_name)
            is_act = (t_key == cur_key)
            is_saved = (t_key in saved_set)
        if not t_name:
            continue
        lbl = f"{t_name} ({cnt})" if cnt > 0 else t_name
        r = t_col.row(align=True)
        op_s = r.operator(
            "reskate.select_saved_qd_tab",
            text=lbl,
            icon='CHECKMARK' if is_act else 'ASSET_MANAGER',
            depress=is_act,
        )
        op_s.tab_id = t_name
        if is_saved:
            op_r = r.operator("reskate.remove_saved_qd_tab", text="", icon='X', emboss=False)
            op_r.tab_id = t_name


def _draw_clickable_scene_prop_packs_list(layout, context, prop=None):
    """Draws a collapsible twirl-down menu for Scene Prop Packs (in-memory only, defaulted to collapsed)."""
    if not hasattr(compiler_bridge, "get_scene_prop_packs_summary"):
        return
    if prop is None:
        scn = getattr(context, "scene", None) if context else None
        prop = getattr(scn, "reskate_prop", None) if scn else None

    packs_info = compiler_bridge.get_scene_prop_packs_summary(context)
    if not packs_info:
        return

    pk_box = layout.box()
    pk_head = pk_box.row(align=True)
    show_pk = bool(getattr(prop, "show_scene_prop_packs_foldout", False)) if prop is not None else False
    if prop is not None and hasattr(prop, "show_scene_prop_packs_foldout"):
        pk_head.prop(
            prop,
            "show_scene_prop_packs_foldout",
            text=f"📁 Scene Prop Packs ({len(packs_info)} Pack{'s' if len(packs_info) != 1 else ''} in Memory)",
            icon='TRIA_DOWN' if show_pk else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        pk_head.label(text="📁 Scene Prop Packs (Click to Switch / Assign):", icon='PACKAGE')

    if not show_pk:
        return

    pk_col = pk_box.column(align=True)
    for p_item in packs_info:
        p_name = str(p_item.get("pack_name", "") or "").strip()
        r_mod = str(p_item.get("resolved_mod", "") or p_name).strip()
        cnt = int(p_item.get("count", 0) or 0)
        is_act = bool(p_item.get("is_active", False))
        if not p_name:
            continue
        r = pk_col.row(align=True)
        op_sw = r.operator(
            "reskate.switch_or_assign_prop_pack",
            text=f"{p_name} ({cnt} prop{'s' if cnt != 1 else ''})",
            icon='CHECKMARK' if is_act else 'PACKAGE',
            depress=is_act,
        )
        op_sw.pack_name = p_name
        op_sel = r.operator(
            "reskate.select_pack_or_scene_props",
            text="Select",
            icon='RESTRICT_SELECT_OFF',
        )
        op_sel.mode = 'PACK'
        op_sel.target_pack = r_mod
        op_sel.frame_view = False
        op_un = r.operator(
            "reskate.unassign_prop_pack_from_scene",
            text="",
            icon='X',
            emboss=False,
        )
        op_un.pack_name = r_mod


def _draw_simple_page5_identity_thumbnail(layout, context, prop, obj):
    """Page 5 — Identity & Thumbnail (§3 Page 5, §4.6)."""
    p_box = layout.box()
    p_box.label(text="Page 5 — Identity & Thumbnail", icon='CAMERA_DATA')

    sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
    n_sel = len(sel_meshes)

    # 1. Prop Name, Variant Family & Quick Drop Tab (Always shows active carousel prop's name)
    id_box = p_box.box()
    id_box.label(text="Prop Name & Quick Drop Tab", icon='FONT_DATA')
    id_box.prop(prop, "prop_display_name", text="Prop Name", icon='FONT_DATA')

    if hasattr(prop, "prop_family_title"):
        fam_val = (
            str(getattr(prop, "prop_family_title", "") or "").strip()
            or (str(obj.get("reskate_family_title", "") or "").strip() if obj is not None else "")
        )
        fam_row = id_box.row(align=True)
        fam_row.prop(prop, "prop_family_title", text="Variant Family", icon='LINKED')
        if fam_val:
            fam_row.operator("reskate.clear_variant_family", text="", icon='X')
            badge_row = id_box.row(align=True)
            badge_row.label(text=f"🔗 Grouped in Quick Drop Tile: '{fam_val}'", icon='CHECKMARK')
    if n_sel >= 2:
        grp_row = id_box.row(align=True)
        grp_row.scale_y = 1.2
        grp_row.operator(
            "reskate.group_selected_as_variants",
            text=f"Group {n_sel} Selected Props as Variants (1 Quick Drop Tile)",
            icon='LINKED',
        )

    if hasattr(prop, "custom_qd_tab"):
        id_box.prop(prop, "custom_qd_tab", text="Quick Drop Tab", icon='ASSET_MANAGER')
        def_author_tab = _props.get_default_qd_tab_name(context) if hasattr(_props, "get_default_qd_tab_name") else "CustomProps"
        if def_author_tab != "CustomProps":
            hint_qd = id_box.row(align=True)
            hint_qd.active = False
            hint_qd.label(text=f"Defaults to Author ('{def_author_tab}') so props sort by author in Quick Drop", icon='INFO')
    _draw_clickable_qd_tabs_list(id_box, context, prop)
    if n_sel >= 2:
        id_box.operator(
            "reskate.apply_category_to_selected",
            text=f"Apply Quick Drop Tab to {n_sel} Selected Props",
            icon='COPYDOWN',
        )

    # 2. Consolidated Camera & Render Actions + Orbit Controls
    act_box = p_box.box()
    act_box.label(text="Thumbnail Camera & Render (Optional — Uses 'No Thumbnail' icon until rendered)", icon='CAMERA_DATA')
    in_cam = False
    _ops_mod = None
    try:
        from . import operators as _ops
        _ops_mod = _ops
        in_cam = _ops.is_in_thumbnail_camera_view(context)
    except Exception:
        in_cam = False

    cam_row = act_box.row(align=True)
    cam_row.scale_y = 1.25
    orb_l = cam_row.operator("reskate.orbit_thumb_camera", text="↺ -45°", icon='LOOP_BACK')
    orb_l.angle_deg = -45.0
    if in_cam:
        cam_row.operator("reskate.view_thumbnail_camera", text="Exit Camera View", icon='LOOP_BACK', depress=True)
    else:
        cam_row.operator("reskate.frame_isometric_camera", text="Frame & View Camera", icon='VIEW_CAMERA')
    orb_r = cam_row.operator("reskate.orbit_thumb_camera", text="↻ +45°", icon='LOOP_FORWARDS')
    orb_r.angle_deg = 45.0

    ren_row = act_box.row(align=True)
    ren_row.scale_y = 1.35
    ren_row.operator("reskate.render_thumbnails", text="Render Thumbnail", icon='RENDER_STILL')
    ren_row.operator("reskate.choose_thumbnail_image", text="Choose", icon='FILE_IMAGE')

    # 3. Collapsible Lighting & Background Twirl-Down
    lt_box = p_box.box()
    lt_head = lt_box.row(align=True)
    show_lt = bool(getattr(prop, "show_thumb_lighting_box", False))
    if hasattr(prop, "show_thumb_lighting_box"):
        lt_head.prop(
            prop,
            "show_thumb_lighting_box",
            text="☀️ Lighting & Background",
            icon='TRIA_DOWN' if show_lt else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        lt_head.label(text="☀️ Lighting & Background", icon='LIGHT_SUN')

    if show_lt:
        pr_row = lt_box.row(align=True)
        pr_row.operator("reskate.copy_thumb_lighting_preset", text="Copy Lighting", icon='COPYDOWN')
        paste_sub = pr_row.row(align=True)
        has_clip = True
        if _ops_mod is not None and hasattr(_ops_mod, "has_copied_thumb_lighting_preset"):
            try:
                has_clip = bool(_ops_mod.has_copied_thumb_lighting_preset())
            except Exception:
                has_clip = True
        paste_sub.enabled = has_clip
        paste_sub.operator("reskate.paste_thumb_lighting_preset", text="Paste Lighting", icon='PASTEDOWN')
        if n_sel >= 2:
            lt_box.operator(
                "reskate.apply_thumb_lighting_to_selected",
                text=f"Apply Lighting to {n_sel} Selected Props",
                icon='DUPLICATE',
            )

        lt_box.prop(prop, "thumb_bg_preset", text="Lighting Preset")
        preset = str(getattr(prop, "thumb_bg_preset", "SUNNY_DAY") or "SUNNY_DAY")
        if preset in ("SUNNY_DAY", "STUDIO_LIGHTING", "STUDIO"):
            if hasattr(prop, "thumb_floor_color"):
                lt_box.prop(prop, "thumb_floor_color", text="Floor Color")
        elif preset in ("FLAT_BG", "FLAT_COLOR"):
            lt_box.prop(prop, "thumb_bg_color", text="Background Color")
        elif preset == "TRANSPARENT":
            lt_box.label(text="✅ Transparent background active (Studio floor hidden)", icon='CHECKMARK')

        if hasattr(prop, "thumb_key_fill_ratio"):
            lt_box.prop(prop, "thumb_key_fill_ratio", text="Key / Fill Ratio", slider=True)
        rot_label = "Sun Rotation" if preset == "SUNNY_DAY" else "Key Light Rotation"
        elev_label = "Sun Elevation" if preset == "SUNNY_DAY" else "Key Light Elevation"
        lt_box.prop(prop, "thumb_sun_rotation", text=rot_label, slider=True)
        lt_box.prop(prop, "thumb_sun_elevation", text=elev_label, slider=True)
        lt_box.prop(prop, "thumb_exposure", text="Exposure", slider=True)


def _draw_simple_page6_package_compile(layout, context, prop, obj):
    """Page 6 — Package & Compile (§3 Page 6, §5.1, §5.2, §5.3, §5.4, §7)."""
    p_box = layout.box()
    p_box.label(text="Page 6 — Package & Compile", icon='PACKAGE')

    sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
    n_sel = len(sel_meshes)

    # 1. On-Demand Pre-Flight Checklist with 1-click Fix All
    if obj and obj.type == 'MESH':
        _draw_preflight_checklist(p_box, context, obj, prop)

    # 2. Prop Pack Name & Thunderstore Metadata
    pack_box = p_box.box()
    pack_box.label(text="Prop Pack", icon='FILE_FOLDER')
    pack_box.prop(prop, "target_mod_name", text="Prop Pack Name", icon='PACKAGE')
    _draw_clickable_scene_prop_packs_list(pack_box, context, prop)
    resolved_mod = compiler_bridge.get_resolved_target_mod_name(context, prop)
    author_str = compiler_bridge.get_author_name(context, prop)
    if not author_str:
        w_row = pack_box.row(align=True)
        w_row.alert = True
        w_row.label(text="Author Name not set in Preferences", icon='ERROR')

    # Collapsible Thunderstore Icon, README.md & Description Twirl-Down
    ts_head = pack_box.row(align=True)
    show_ts = bool(getattr(prop, "show_thunderstore_metadata", False))
    if hasattr(prop, "show_thunderstore_metadata"):
        ts_head.prop(
            prop,
            "show_thunderstore_metadata",
            text="📦 Thunderstore Icon, README & Description",
            icon='TRIA_DOWN' if show_ts else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        ts_head.label(text="📦 Thunderstore Icon, README & Description", icon='IMAGE_DATA')

    if show_ts:
        ts_col = pack_box.column(align=True)
        ts_safe_pkg = compiler_bridge.get_thunderstore_safe_pack_name(resolved_mod, author_str)
        ts_ver_norm = compiler_bridge.normalize_semver(getattr(prop, "pack_version", "0.1.0"))
        ts_col.label(
            text=f"🔒 Thunderstore ID: {ts_safe_pkg}  |  Version: {ts_ver_norm}",
            icon='LOCKED',
        )
        if hasattr(prop, "mod_icon_path"):
            mi_row = ts_col.row(align=True)
            mi_row.prop(prop, "mod_icon_path", text="Pack Icon (icon.png)")
            mi_row.operator("reskate.use_prop_thumb_as_mod_icon", text="", icon='IMAGE_DATA')
            has_icon_str = bool((getattr(prop, "mod_icon_path", "") or "").strip())
            if hasattr(prop, "use_pack_icon_as_first_prop"):
                ts_col.prop(
                    prop,
                    "use_pack_icon_as_first_prop",
                    text="Use Pack Icon as #1 Quick Drop Tile (Sets Tab Icon)",
                )
            if not has_icon_str:
                ts_col.label(text="ℹ️ Blank = Automatically uses #1 prop's thumbnail as icon.png & Tab Icon", icon='INFO')
            elif bool(getattr(prop, "use_pack_icon_as_first_prop", True)):
                ts_col.label(
                    text="✅ Pack Icon auto-generates as Prop #1 (60cm square tile + 30cm tall grab proxy)",
                    icon='CHECKMARK',
                )
        if hasattr(prop, "mod_readme_path"):
            ts_col.prop(prop, "mod_readme_path", text="Custom README.md")
            if not (getattr(prop, "mod_readme_path", "") or "").strip():
                ts_col.label(text="ℹ️ Blank = Auto-generates README.md with pack info & prop list", icon='INFO')
        ts_col.prop(prop, "pack_description", text="Description")

    # 3. Prominent 4-Column Pack Compile Summary Card (Adding / Updating / Unchanged / Keeping)
    mod_info = compiler_bridge.inspect_existing_mod_props(resolved_mod)
    if n_sel >= 2:
        diff = compiler_bridge.compute_pack_compile_diff(context, resolved_mod, selected_objs=sel_meshes)
    elif obj:
        diff = compiler_bridge.compute_pack_compile_diff(context, resolved_mod, single_obj=obj)
    else:
        diff = {"adding": [], "updating": [], "unchanged": [], "keeping": []}

    n_add = len(diff.get("adding", []))
    n_upd = len(diff.get("updating", []))
    n_unc = len(diff.get("unchanged", []))
    n_keep = len(diff.get("keeping", []))

    sum_card = pack_box.box()
    sum_head = sum_card.row(align=True)
    sum_head.label(text="📊 Pack Compile Summary", icon='SEQ_LUMA_WAVEFORM')
    sum_grid = sum_card.grid_flow(row_major=True, columns=4, even_columns=True, even_rows=True, align=True)

    c_add = sum_grid.box()
    r_add = c_add.row(align=True)
    r_add.alignment = 'CENTER'
    if n_add > 0:
        r_add.scale_y = 1.15
        r_add.prop(prop, "merge_existing_mod", text=f"➕ Adding: {n_add}", icon='ADD', toggle=True, emboss=True)
    else:
        r_add.active = False
        r_add.label(text=f"➕ Adding: {n_add}", icon='DOT')

    c_upd = sum_grid.box()
    r_upd = c_upd.row(align=True)
    r_upd.alignment = 'CENTER'
    if n_upd > 0:
        r_upd.scale_y = 1.15
        r_upd.alert = True
        r_upd.label(text=f"🔄 Updating: {n_upd}", icon='FILE_REFRESH')
    else:
        r_upd.active = False
        r_upd.label(text=f"🔄 Updating: {n_upd}", icon='DOT')

    c_unc = sum_grid.box()
    r_unc = c_unc.row(align=True)
    r_unc.alignment = 'CENTER'
    if n_unc == 0:
        r_unc.active = False
    r_unc.label(text=f"✅ Unchanged: {n_unc}", icon='CHECKMARK' if n_unc > 0 else 'DOT')

    c_keep = sum_grid.box()
    r_keep = c_keep.row(align=True)
    r_keep.alignment = 'CENTER'
    if n_keep == 0:
        r_keep.active = False
    r_keep.label(text=f"📦 Keeping: {n_keep}", icon='PACKAGE' if n_keep > 0 else 'DOT')

    pack_scene_objs = compiler_bridge.get_pack_prop_meshes_in_scene(context, resolved_mod)
    ordered_scene_objs: list = []
    seen_scn_names: set[str] = set()
    for o_cand in (sel_meshes if len(sel_meshes) >= 2 else pack_scene_objs):
        if o_cand is not None and o_cand.name not in seen_scn_names:
            seen_scn_names.add(o_cand.name)
            ordered_scene_objs.append(o_cand)
    for o_cand in pack_scene_objs:
        if o_cand is not None and o_cand.name not in seen_scn_names:
            seen_scn_names.add(o_cand.name)
            ordered_scene_objs.append(o_cand)
    if not ordered_scene_objs and sel_meshes:
        for o_cand in sel_meshes:
            if o_cand is not None and o_cand.name not in seen_scn_names:
                seen_scn_names.add(o_cand.name)
                ordered_scene_objs.append(o_cand)

    pack_order_rows: list[dict] = []
    seen_row_keys: set[str] = set()
    reg_keys_set = {
        str(pd.get("prop_key") or pd.get("slug") or "").strip().lower()
        for pd in mod_info.get("props_detail", [])
        if isinstance(pd, dict) and not bool(pd.get("is_pack_icon_prop", False))
    }
    for s_obj in ordered_scene_objs:
        st_o = _props.get_or_init_object_prop_state(s_obj)
        s_disp = str(st_o.get("prop_display_name") or s_obj.get("reskate_display_name") or s_obj.name).strip()
        s_key = compiler_bridge.get_prop_slug(context, s_obj)
        s_key_low = s_key.lower()
        seen_row_keys.add(s_key_low)
        pack_order_rows.append({
            "obj_name": s_obj.name,
            "display_name": s_disp,
            "prop_key": s_key,
            "in_scene": True,
            "in_registry": s_key_low in reg_keys_set,
            "is_active": bool(obj and s_obj.name == obj.name),
        })
    for p_det in mod_info.get("props_detail", []):
        if not isinstance(p_det, dict) or bool(p_det.get("is_pack_icon_prop", False)):
            continue
        p_name = p_det.get("display_name") or p_det.get("name") or p_det.get("prop_key", "")
        p_key = p_det.get("prop_key") or p_det.get("slug", "")
        p_key_low = str(p_key).strip().lower()
        if p_key_low and p_key_low not in seen_row_keys:
            seen_row_keys.add(p_key_low)
            pack_order_rows.append({
                "obj_name": "",
                "display_name": p_name,
                "prop_key": p_key,
                "in_scene": False,
                "in_registry": True,
                "is_active": False,
            })

    if pack_order_rows or (mod_info.get("exists") and mod_info.get("count", 0) > 0):
        pm_head = pack_box.row(align=True)
        show_pc = bool(getattr(prop, "show_pack_contents", True))
        pc_icon = 'TRIA_DOWN' if show_pc else 'TRIA_RIGHT'
        pm_head.prop(
            prop,
            "show_pack_contents",
            text=f"Pack Contents & Prop Order ({len(pack_order_rows)} prop(s) in '{resolved_mod}')",
            icon=pc_icon,
            emboss=False,
        )
        if pack_scene_objs:
            sel_all_pk = pm_head.operator(
                "reskate.select_pack_or_scene_props",
                text=f"Select All in Pack ({len(pack_scene_objs)})",
                icon='RESTRICT_SELECT_OFF',
            )
            sel_all_pk.mode = 'PACK'
            sel_all_pk.frame_view = False
        bak_cnt = int(mod_info.get("backups_count", mod_info.get("backup_count", 0)) or 0)
        if bak_cnt > 0:
            pm_head.operator("reskate.restore_pack_backup", text=f"Restore Backup ({bak_cnt})", icon='LOOP_BACK')
        if show_pc:
            raw_icon_str = (getattr(prop, "mod_icon_path", "") or "").strip()
            has_custom_icon_prop = bool(raw_icon_str and getattr(prop, "use_pack_icon_as_first_prop", True))
            if has_custom_icon_prop:
                ic_row = pack_box.row(align=True)
                ic_row.label(
                    text="📌 #1 [Tab Icon]: Custom Pack Icon Tile (60×60cm surface + 30cm tall Quick Drop proxy)",
                    icon='IMAGE_DATA',
                )
            elif len(pack_order_rows) >= 2:
                hint_row = pack_box.row(align=True)
                hint_row.label(
                    text="💡 Prop #1 sets the Quick Drop Tab Icon. Reordering also updates the Carousel (◀ / ▶):",
                    icon='INFO',
                )

            n_rows = len(pack_order_rows)
            for row_idx, r_item in enumerate(pack_order_rows):
                p_name = r_item["display_name"]
                p_key = r_item["prop_key"]
                o_nm = r_item["obj_name"]
                disp_num = (row_idx + 2) if has_custom_icon_prop else (row_idx + 1)
                badge_prefix = f"#{disp_num}"
                if row_idx == 0 and not has_custom_icon_prop:
                    badge_prefix = "#1 [Tab Icon]"

                p_row = pack_box.row(align=True)
                p_row.label(
                    text=f"{badge_prefix}: {p_name}",
                    icon='RADIOBUT_ON' if r_item["is_active"] else 'OBJECT_DATA',
                )

                if n_rows >= 2:
                    top_sub = p_row.row(align=True)
                    top_sub.enabled = row_idx > 0
                    top_op = top_sub.operator("reskate.reorder_pack_prop", text="", icon='TRIA_UP_BAR')
                    top_op.obj_name = o_nm
                    top_op.prop_key = p_key
                    top_op.direction = 'TOP'

                    up_sub = p_row.row(align=True)
                    up_sub.enabled = row_idx > 0
                    up_op = up_sub.operator("reskate.reorder_pack_prop", text="", icon='TRIA_UP')
                    up_op.obj_name = o_nm
                    up_op.prop_key = p_key
                    up_op.direction = 'UP'

                    dn_sub = p_row.row(align=True)
                    dn_sub.enabled = row_idx < (n_rows - 1)
                    dn_op = dn_sub.operator("reskate.reorder_pack_prop", text="", icon='TRIA_DOWN')
                    dn_op.obj_name = o_nm
                    dn_op.prop_key = p_key
                    dn_op.direction = 'DOWN'

                sel_op = p_row.operator("reskate.pack_prop_action", text="Select & Frame", icon='RESTRICT_SELECT_OFF')
                sel_op.action = 'SELECT'
                sel_op.prop_key = p_key
                sel_op.display_name = p_name
                if r_item["in_registry"]:
                    rem_op = p_row.operator("reskate.pack_prop_action", text="", icon='TRASH', emboss=False)
                    rem_op.action = 'REMOVE'
                    rem_op.prop_key = p_key
                    rem_op.display_name = p_name

    # 4. Block Reason Check & Dynamic Compile Button
    block_reason = compiler_bridge.get_compile_block_reason(context)
    if block_reason:
        blk_box = p_box.box()
        blk_box.alert = True
        blk_box.label(text=block_reason, icon='ERROR')
        pref_btn = blk_box.row(align=True)
        pref_btn.scale_y = 1.25
        pref_btn.alert = False
        pref_btn.operator("reskate.open_addon_preferences", text="Open ReSkate Add-on Preferences", icon='PREFERENCES')

    is_compiling = bool(getattr(prop, "is_compiling", False))
    if is_compiling:
        prog_box = p_box.box()
        prog_box.label(text=prop.compile_step_label or "Compiling...", icon='SORTTIME')
        prog_box.prop(prop, "compile_progress", text="Progress", slider=True)
        if prop.compile_status_msg:
            prog_box.label(text=prop.compile_status_msg)
        prog_box.operator("reskate.cancel_compile", text="Cancel", icon='CANCEL')
    else:
        comp_row = p_box.row(align=True)
        comp_row.scale_y = 1.55
        comp_row.enabled = (not bool(block_reason)) and (obj is not None or n_sel > 0)
        if n_sel >= 2:
            comp_row.operator(
                "reskate.compile_selected_props",
                text=f"Compile and Deploy Prop Pack ({n_sel} Props)",
                icon='PLAY',
            )
        else:
            comp_row.operator(
                "reskate.compile_and_deploy",
                text="Compile and Deploy This Prop",
                icon='PLAY',
            )

        if mod_info.get("exists") and mod_info.get("count", 0) > 0:
            rel_row = p_box.row(align=True)
            rel_row.scale_y = 1.15
            rel_row.operator("reskate.export_thunderstore_zip", text="Export Release .zip (Thunderstore)", icon='PACKAGE')

        # 5. Collapsible Compile & Versioning Preferences Twirl-Down
        cp_box = p_box.box()
        cp_head = cp_box.row(align=True)
        show_cp = bool(getattr(prop, "show_compile_preferences_box", False))
        if hasattr(prop, "show_compile_preferences_box"):
            cp_head.prop(
                prop,
                "show_compile_preferences_box",
                text="⚙️ Compile & Versioning Preferences",
                icon='TRIA_DOWN' if show_cp else 'TRIA_RIGHT',
                emboss=False,
            )
        else:
            cp_head.label(text="⚙️ Compile & Versioning Preferences", icon='PREFERENCES')

        if show_cp:
            cp_col = cp_box.column(align=True)
            if hasattr(prop, "skip_existing_thumbnails"):
                cp_col.prop(
                    prop,
                    "skip_existing_thumbnails",
                    text="Skip Re-Rendering Existing Thumbnails (Faster Compile)",
                )
            v_row = cp_col.row(align=True)
            v_row.prop(prop, "pack_version", text="Version")
            op_patch = v_row.operator("reskate.bump_pack_version", text="+Patch")
            op_patch.part = 'PATCH'
            op_minor = v_row.operator("reskate.bump_pack_version", text="+Minor")
            op_minor.part = 'MINOR'
            if hasattr(prop, "auto_bump_pack_version"):
                cp_col.prop(
                    prop,
                    "auto_bump_pack_version",
                    text="Auto-Increment Patch Version on Thunderstore Export",
                )

        # 6. Error Box (only on error) & Open Mod / Output Folder Button
        if prop.compile_status_msg and prop.compile_has_error:
            st_box = p_box.box()
            st_box.alert = True
            st_box.label(text=prop.compile_status_msg, icon='ERROR')
            st_box.label(text="Full log: Edit → Preferences → Add-ons → ReSkate Prop Studio.", icon='INFO')
            log_btn = st_box.row(align=True)
            log_btn.alert = False
            log_btn.operator("reskate.open_addon_preferences", text="View Compiler Log in Preferences", icon='PREFERENCES')
            log_btn.operator("reskate.copy_compiler_log", text="Copy Log to Clipboard", icon='COPYDOWN')

        open_row = p_box.row(align=True)
        open_row.scale_y = 1.25
        open_row.operator("reskate.open_mod_output_folder", text="Open Mod / Output Folder", icon='FILE_FOLDER')


def _draw_simple_mode_wizard(layout, context, prop, metrics):
    """Draws the complete 6-Page Simple Mode Wizard inside RESKATE_PT_main_panel (§3)."""
    obj = metrics["obj"] if metrics else None

    # 1. Collapsible Viewport Overlays Twirl-Down (hidden completely when disabled at source in Preferences)
    overlays_master_on = _props.is_viewport_overlays_master_enabled(context)
    if overlays_master_on:
        ov_box = layout.box()
        ov_head = ov_box.row(align=True)
        show_ov = bool(getattr(prop, "show_viewport_overlays_box", True))
        if hasattr(prop, "show_viewport_overlays_box"):
            ov_head.prop(
                prop,
                "show_viewport_overlays_box",
                text="🖥️ Viewport Overlays",
                icon='TRIA_DOWN' if show_ov else 'TRIA_RIGHT',
                emboss=False,
            )
        else:
            ov_head.label(text="🖥️ Viewport Overlays", icon='RESTRICT_VIEW_OFF')
        if show_ov:
            ov_head.operator("reskate.clear_all_overlays", text="Hide All", icon='HIDE_ON')

        if show_ov:
            g_row = ov_box.row(align=True)
            g_row.prop(prop, "draw_grind_overlay", text="Grindable Edge Visualizer", toggle=True, icon='SNAP_EDGE')
            g_row.prop(prop, "show_collision_overlay", text="Collision Preview", toggle=True, icon='PHYSICS')
            if hasattr(prop, "show_placement_proxy_overlay"):
                g_row.prop(prop, "show_placement_proxy_overlay", text="Placement Proxy", toggle=True, icon='MESH_ICOSPHERE')

            g_row2 = ov_box.row(align=True)
            if hasattr(prop, "draw_face_surface_overlay"):
                g_row2.prop(prop, "draw_face_surface_overlay", text="Face Surfaces", toggle=True, icon='FACESEL')
            if hasattr(prop, "show_alignment_overlay"):
                g_row2.prop(prop, "show_alignment_overlay", text="⚠️ Do Not Align", toggle=True)
            if hasattr(prop, "draw_light_overlay"):
                g_row2.prop(prop, "draw_light_overlay", text="Lights", toggle=True, icon='LIGHT')

            if getattr(prop, "draw_grind_overlay", False) and obj is not None:
                try:
                    from . import gpu_draw as _gd
                    g_counts = _gd.get_grind_edge_counts(obj)
                    ov_box.label(
                        text=f"Auto-Detected Grind Edges: 🟩 {g_counts.get('ledge', 0)} Ledges  |  🟦 {g_counts.get('rail', 0)} Rails  |  🟪 {g_counts.get('coping', 0)} Coping",
                        icon='INFO',
                    )
                except Exception:
                    pass
            if getattr(prop, "show_collision_overlay", False):
                ov_box.label(
                    text="🟩 Green = Solid Skate Collision  |  🟥 Red = Decal / Excluded from Collision",
                    icon='INFO',
                )
            if getattr(prop, "show_placement_proxy_overlay", False):
                ov_box.label(
                    text="🟧 Orange = Placement / Selection Bounds (_PPHYS)",
                    icon='INFO',
                )

    # 1b. Top-level Collapsible Scale References Twirl-Down (Available on ALL pages!)
    _draw_scale_references_twirldown(layout, prop)

    # 2. Clickable 6-Step Indicator Bar (Responsive labels based on panel width!)
    cur_page = int(getattr(prop, "simple_wizard_page", "1") or "1")
    region = getattr(context, "region", None)
    reg_width = int(getattr(region, "width", 420) or 420)
    ui_scale = 1.0
    try:
        prefs_sys = getattr(getattr(context, "preferences", None), "system", None)
        if prefs_sys and getattr(prefs_sys, "ui_scale", 0):
            ui_scale = float(prefs_sys.ui_scale)
    except Exception:
        ui_scale = 1.0
    eff_width = reg_width / max(0.5, ui_scale)

    step_box = layout.box()
    step_row = step_box.row(align=True)
    step_row.scale_y = 1.25
    step_titles = (
        (1, "1. Scale & Origin", "1. Scale"),
        (2, "2. Collision", "2. Collision"),
        (3, "3. Surfaces", "3. Surfaces"),
        (4, "4. Lights", "4. Lights"),
        (5, "5. Thumbnail", "5. Thumb"),
        (6, "6. Compile", "6. Compile"),
    )
    for pg_num, pg_full_lbl, pg_med_lbl in step_titles:
        if eff_width >= 480:
            btn_text = pg_full_lbl
        elif eff_width >= 330:
            btn_text = pg_med_lbl
        else:
            btn_text = pg_full_lbl if cur_page == pg_num else str(pg_num)
        op_s = step_row.operator(
            "reskate.simple_page_nav",
            text=btn_text,
            depress=(cur_page == pg_num),
        )
        op_s.target_page = pg_num
        op_s.delta = 0

    # 3. Persistent Sanctus-Style Carousel & Thumbnail Selector (Identical position on ALL 6 pages!)
    _draw_sanctus_prop_carousel_header(layout, context, prop, obj)

    # 4. Current Page Content
    if cur_page == 1:
        _draw_simple_page1_scale_origin(layout, context, prop, obj, metrics)
    elif cur_page == 2:
        _draw_simple_page2_collision(layout, context, prop, obj, metrics)
    elif cur_page == 3:
        _draw_simple_page3_surfaces(layout, context, prop, obj)
    elif cur_page == 4:
        _draw_simple_page4_lights(layout, context, prop, obj)
    elif cur_page == 5:
        _draw_simple_page5_identity_thumbnail(layout, context, prop, obj)
    else:
        _draw_simple_page6_package_compile(layout, context, prop, obj)

    # 5. Bottom Navigation Bar (◀ Back / Next ▶)
    nav_row = layout.row(align=True)
    nav_row.scale_y = 1.3
    back_sub = nav_row.row(align=True)
    back_sub.enabled = cur_page > 1
    op_prev = back_sub.operator("reskate.simple_page_nav", text="◀ Back")
    op_prev.target_page = 0
    op_prev.delta = -1

    next_sub = nav_row.row(align=True)
    next_sub.enabled = cur_page < 6
    op_next = next_sub.operator("reskate.simple_page_nav", text="Next ▶")
    op_next.target_page = 0
    op_next.delta = 1


# ============================================================================
# Top-Level Main Header & 3-Mode Switcher (`RESKATE_PT_main_panel`)
# ============================================================================

def _draw_session_update_banner(layout):
    """Draws a zero-overhead update notification banner at the very top of the N-panel when a newer GitHub release exists."""
    try:
        if hasattr(compiler_bridge, "ensure_session_update_check"):
            compiler_bridge.ensure_session_update_check()
        latest_tag = str(getattr(compiler_bridge, "_SESSION_UPDATE_AVAILABLE_TAG", "") or "")
        dismissed = bool(getattr(compiler_bridge, "_SESSION_UPDATE_DISMISSED", False))
        if not latest_tag or dismissed:
            return
        cur_ver = str(getattr(compiler_bridge, "ADDON_VERSION_STR", "0.32.3") or "0.32.3")
        rel_url = str(
            getattr(compiler_bridge, "_SESSION_UPDATE_HTML_URL", "")
            or "https://github.com/CameronTutt/ReSkate-Prop-Studio/releases/latest"
        )
        upd_box = layout.box()
        top_row = upd_box.row(align=True)
        lbl_sub = top_row.row(align=True)
        lbl_sub.alert = True
        lbl_sub.label(text=f"🆕 Update Available: v{latest_tag} (You have v{cur_ver})", icon='INFO')
        top_row.operator("reskate.dismiss_update_banner", text="", icon='PANEL_CLOSE', emboss=False)

        btn_row = upd_box.row(align=True)
        btn_row.scale_y = 1.25
        btn_row.operator("reskate.auto_install_update", text=f"⚡ 1-Click Update to v{latest_tag}", icon='IMPORT')
        op_url = btn_row.operator("wm.url_open", text="Release Notes", icon='URL')
        op_url.url = rel_url
    except Exception:
        pass


class RESKATE_PT_main_panel(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_main_panel"
    bl_label = "ReSkate Prop Studio"
    bl_order = 0

    def draw_header(self, context):
        self.layout.label(text="", icon='MOD_DYNAMICPAINT')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)

        # Top-of-N-panel automatic session update notification banner
        _draw_session_update_banner(layout)

        # Synchronize 2D/3D GPU draw handlers with source-level Preferences switches and clean up live thumbnail rig if exited
        try:
            if not bpy.app.background and hasattr(gpu_draw, "sync_draw_handlers"):
                gpu_draw.sync_draw_handlers(context)
        except Exception:
            pass
        try:
            from . import operators as _ops
            if hasattr(_ops, "ensure_thumbnail_camera_watchdog"):
                _ops.ensure_thumbnail_camera_watchdog(context)
        except Exception:
            pass

        # Show 3-Mode Switcher (Simple | Advanced | Experimental) ONLY when the Advanced & Lab add-on is enabled (§8)
        if _is_advanced_lab_addon_enabled(context):
            mode_row = layout.row(align=True)
            mode_row.scale_y = 1.35
            if hasattr(scene, "sk8_ui_mode"):
                mode_row.prop(scene, "sk8_ui_mode", expand=True)
            elif prop is not None and hasattr(prop, "ui_mode"):
                mode_row.prop(prop, "ui_mode", expand=True)

        if prop is None:
            layout.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        # In Simple Mode, render the self-contained 5-Page Simple Mode Wizard
        if ui_mode == "SIMPLE":
            _draw_simple_mode_wizard(layout, context, prop, metrics)
            return

        # If compiling right now in Advanced/Experimental mode, show a live compile progress banner at the top
        if bool(getattr(prop, "is_compiling", False)):
            prog_box = layout.box()
            prog_box.label(text=prop.compile_step_label or "⏳ Compiling Prop Pack...", icon='SORTTIME')
            prog_box.prop(prop, "compile_progress", text="Progress", slider=True)
            if prop.compile_status_msg:
                prog_box.label(text=prop.compile_status_msg)
            prog_box.operator("reskate.cancel_compile", text="Cancel", icon='CANCEL')
            return

        # Compact Active Prop Summary for Advanced & Experimental modes
        status_box = layout.box()
        if metrics is not None:
            obj = metrics["obj"]
            dname = (prop.prop_display_name if (prop and prop.prop_display_name) else obj.name)
            top_row = status_box.row(align=True)
            top_row.label(text=f"Active Prop: {dname} ({obj.name})", icon='OBJECT_DATA')
            top_row.operator("wm.reskate_architecture_guide", text="", icon='FILE_SCRIPT')
            sub_row = status_box.row(align=True)
            sub_row.scale_y = 0.88
            sub_row.label(
                text=f"Size: {metrics['width']:.2f}×{metrics['height']:.2f}×{metrics['depth']:.2f}m  |  {metrics['tris']:,} Tris ({metrics['verts']:,} Verts)"
            )
        else:
            warn_row = status_box.row(align=True)
            warn_row.alert = True
            warn_row.label(text="Select a 3D mesh object in the viewport to begin.", icon='INFO')

        _draw_viewport_overlay_toggles(layout, prop)
        _draw_scale_references_twirldown(layout, prop)


# ============================================================================
# Step 1: Setup (`Advanced` Mode)
# ============================================================================

class RESKATE_PT_card1_setup(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card1_setup"
    bl_label = "🟦 1. Setup"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card1", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        prefs = _props.get_addon_preferences(context)
        if not compiler_bridge.is_game_dir_valid(compiler_bridge.get_game_dir(context)):
            path_alert = card.box()
            path_alert.alert = True
            path_alert.label(text="⚠️ Skate. Game Folder not found — set your paths before compiling:", icon='ERROR')
            p_btn = path_alert.row(align=True)
            p_btn.scale_y = 1.25
            p_btn.alert = False
            p_btn.operator(
                "reskate.open_addon_preferences",
                text="⚙️ Set Skate. Game & Staging Paths in Preferences",
                icon='PREFERENCES',
            )
            if prefs is not None:
                p_col = path_alert.column(align=True)
                p_col.alert = False
                p_col.prop(prefs, "game_dir", text="Skate. Game Folder")
                p_col.prop(prefs, "staging_dir", text="Staging Folder")

        orig_box = card.box()
        orig_box.label(text="Origin Point (Pivot & Balance):", icon='OBJECT_ORIGIN')
        orig_col = orig_box.column(align=True)
        orig_col.scale_y = 1.25
        orig_col.enabled = obj is not None
        orig_col.operator("reskate.origin_to_ground_contact", text="Origin to Ground Contact (Recommended)", icon='SNAP_ON')
        orig_col.operator("reskate.origin_to_bottom_center", text="Origin to Bottom Center", icon='PIVOT_BOUNDBOX')
        orig_col.operator("reskate.origin_to_geometry_center", text="Origin to Geometry Center", icon='PIVOT_MEDIAN')

        has_any_active = (obj is not None) or (getattr(context, "active_object", None) is not None) or bool(getattr(context, "selected_objects", None))
        flush_row = orig_box.row(align=True)
        flush_row.scale_y = 1.25
        flush_row.enabled = has_any_active
        flush_row.operator("reskate.drop_prop_flush_to_ground", text="Set Prop Flush to Ground (Z = 0)", icon='IMPORT')

        man_row = orig_box.row(align=True)
        man_row.scale_y = 1.2
        man_row.enabled = obj is not None
        man_row.operator(
            "reskate.snap_origin_to_selected",
            text="Snap Origin to Selected / Cursor",
            icon='SNAP_ON',
        )
        ts = getattr(scene, "tool_settings", None)
        is_moving_origin = bool(ts and getattr(ts, "use_transform_data_origin", False))
        man_row.operator(
            "reskate.toggle_interactive_origin_edit",
            text="Done Moving Origin" if is_moving_origin else "Move Origin Point",
            icon='CHECKMARK' if is_moving_origin else 'ORIENTATION_GLOBAL',
            depress=is_moving_origin,
        )
        if is_moving_origin:
            orig_box.label(text="💡 Drag the Move gizmo arrows to slide the origin, then click 'Done Moving Origin'.", icon='INFO')

        card.separator(factor=0.8)
        util_box = card.box()
        util_box.label(text="Prop Key & Export Scale (Advanced):", icon='TOOL_SETTINGS')
        util_box.prop(prop, "prop_id_locked", text="Custom Prop ID Override (Lock ID)", icon='LOCKED')
        if getattr(prop, "prop_id_locked", False):
            util_box.prop(prop, "prop_id", text="Custom Prop ID")
        util_box.prop(prop, "prop_key_slug", text="Internal Slug", icon='RNA')
        util_box.operator("reskate.reset_stable_prop_key", text="Reset internal key", icon='FILE_REFRESH')
        util_box.prop(prop, "scale_mult", text="Export Scale", slider=True)


# ============================================================================
# Step 2: Collision & Grind (`Simple` & `Advanced` Modes)
# ============================================================================

class RESKATE_UL_grind_paths(UIList):
    """UIList displaying named vertex-path grind entries."""
    bl_idname = "RESKATE_UL_grind_paths"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="", icon_only=True)
            icon_id = 'AUTO' if getattr(item, "is_auto_set", False) else 'CURVE_PATH'
            row.prop(item, "name", text="", emboss=False, icon=icon_id)
            if item.grind_type == 'ROUND_RAIL':
                type_lbl = "Rail (Cyan)"
            elif item.grind_type == 'COPING':
                type_lbl = "Coping (Magenta)"
            else:
                type_lbl = "Ledge (Orange)"
            row.label(text=type_lbl)
            sel_op = row.operator("reskate.select_grind_path_in_edit_mode", text="", icon='EDITMODE_HLT', emboss=False)
            sel_op.path_index = index
            if getattr(item, "is_auto_set", False):
                rec_op = row.operator("reskate.recalculate_auto_grind_set", text="", icon='FILE_REFRESH', emboss=False)
                rec_op.index = index
            del_op = row.operator("reskate.remove_grind_path", text="", icon='X', emboss=False)
            del_op.index = index
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='CURVE_PATH')


class RESKATE_PT_card2_collision(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card2_collision"
    bl_label = "🟧 2. Grind & Skate Check"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card2", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        active_raw = getattr(context, "active_object", None)
        col_proxy = compiler_bridge.resolve_custom_collision_object(context, obj) if obj else None

        # 1. Grindable Edge Visualizer & Round Rail Mode (Shown in Both Simple & Advanced)
        counts = gpu_draw.get_grind_edge_counts(obj) if obj else {"ledge": 0, "rail": 0, "coping": 0, "no_grind": 0}
        grind_box = card.box()
        g_head = grind_box.row(align=True)
        g_head.label(text="🟨 Grindable Edge Visualizer:", icon='SNAP_EDGE')
        g_head.prop(prop, "draw_grind_overlay", text="GPU Preview", toggle=True, icon='HIDE_OFF')

        r1 = grind_box.row(align=True)
        r1.label(text=f"🟩 Ledges: {counts.get('ledge', 0)}")
        r1.label(text=f"🟦 Rails: {counts.get('rail', 0)}")
        r1.label(text=f"🟪 Coping: {counts.get('coping', 0)}")
        r1.label(text=f"🟥 Blocked: {counts.get('no_grind', 0)}")

        if ui_mode == 'ADVANCED':
            grind_box.prop(
                prop,
                "round_rail",
                text="Round Rail Lock-On Mode (Use for Cylindrical Rails & Pipes)",
                icon='IPO_CIRC',
            )
            rr_box = grind_box.box()
            if hasattr(prop, "show_round_rail_diameter_guide"):
                rr_row = rr_box.row(align=True)
                rr_row.scale_y = 1.2
                rr_row.prop(
                    prop,
                    "show_round_rail_diameter_guide",
                    text="Diameter Guide (GPU)",
                    toggle=True,
                    icon='MESH_CYLINDER',
                )
            if getattr(prop, "round_rail", False) or counts.get("rail", 0) > 0:
                rad_m = float(getattr(prop, "grind_tube_radius", 0.032) or 0.032)
                diam_cm = rad_m * 200.0
                diam_in = diam_cm / 2.54
                rad_row = rr_box.row(align=True)
                rad_row.prop(prop, "grind_tube_radius", text="Guide Radius", slider=True)
                rr_box.label(
                    text=f"Recommended Round Rail: Ø {diam_cm:.1f} cm ({diam_in:.2f}\") (Ideal: Ø 5.0–7.6 cm)",
                    icon='INFO',
                )
            rr_box.label(
                text="💡 Round rails must be 8-to-12-sided cylinders to lock in smoothly as rails.",
                icon='MESH_CYLINDER',
            )

        _draw_alignment_troubleshooting_alert(card, context, obj, prop)
        _draw_havok_ceiling_warning(card, context, obj, source_panel="collision")

        # 3. Advanced Mode Controls: Collision Shape Mode, Quick Drop Placement Proxy, Edge Straightener, Per-Face Audio Painter
        if ui_mode == 'ADVANCED':
            card.separator(factor=0.8)

            is_editing_col = bool(
                active_raw and active_raw.type == 'MESH' and active_raw.mode == 'EDIT'
                and (
                    active_raw == col_proxy
                    or active_raw.name.endswith("_COL")
                    or active_raw.name.lower().endswith("_collision")
                )
            )

            col_box = card.box()
            col_box.label(text="Skater Collision Shape (Advanced):", icon='PHYSICS')
            col_box.prop(prop, "collision_mode_ui", text="Shape Mode")
            col_mode = getattr(prop, "collision_mode", "SAME_AS_MESH")

            if is_editing_col:
                edit_row = col_box.row(align=True)
                edit_row.scale_y = 1.25
                edit_row.operator(
                    "reskate.toggle_edit_collision_mesh",
                    text=f"Done Editing ({active_raw.name})",
                    icon='CHECKMARK',
                )
                col_box.label(text="ℹ️ Visual prop is selection-locked in wireframe — click Done when finished.", icon='INFO')

            if col_mode in ("SAME_AS_MESH", "EXACT_MESH", "EXACT_1_1"):
                col_box.prop(prop, "max_col_tris", text="Triangle Limit", slider=True)
                if not is_editing_col:
                    edit_lbl = f"Edit Collision Mesh ({col_proxy.name})" if col_proxy else "Create & Edit Custom Collision (_COL)"
                    col_box.operator("reskate.toggle_edit_collision_mesh", text=edit_lbl, icon='EDITMODE_HLT')

            elif col_mode in ("SIMPLE_HULLS", "CONVEX_HULL"):
                col_box.label(text="Geometric Hull Generator (Preserves sharp boxes & ramps):", icon='MESH_CUBE')
                gen_row = col_box.row(align=True)
                gen_row.scale_y = 1.2
                gen_row.operator(
                    "reskate.generate_geometric_hull_col",
                    text="Generate Geometric Hull Mesh",
                    icon='MOD_DECIM',
                )
                if col_proxy:
                    c_tris = _fast_mesh_tri_count(col_proxy.data)
                    col_box.label(text=f"✅ Active Hull Mesh: {col_proxy.name} ({c_tris:,} flat tris)", icon='CHECKMARK')
                    col_box.prop(prop, "custom_collision_object", text="Hull Mesh", icon='OBJECT_DATA')
                    if not is_editing_col:
                        ed_row = col_box.row(align=True)
                        ed_row.scale_y = 1.15
                        ed_row.operator(
                            "reskate.toggle_edit_collision_mesh",
                            text=f"Edit Collision Mesh ({col_proxy.name})",
                            icon='EDITMODE_HLT',
                        )
                col_box.prop(prop, "coacd_concavity", text="Concavity", slider=True)
                col_box.prop(prop, "coacd_max_hulls", text="Max Hulls", slider=True)

            elif col_mode in ("CUSTOM_OBJECT", "AUTHORED_COL"):
                col_box.prop(prop, "custom_collision_object", text="Collision Object", icon='OBJECT_DATA')
                helper_row = col_box.row(align=True)
                helper_row.operator("reskate.generate_authored_col", text="Create Starter Copy", icon='DUPLICATE')
                helper_row.operator("reskate.create_simplified_col_copy", text="Create Simplified Copy", icon='MOD_DECIM')
                if not is_editing_col:
                    ed_row = col_box.row(align=True)
                    ed_row.scale_y = 1.2
                    edit_lbl = f"Edit Collision Mesh ({col_proxy.name})" if col_proxy else "Create & Edit Collision Mesh (_COL)"
                    ed_row.operator("reskate.toggle_edit_collision_mesh", text=edit_lbl, icon='EDITMODE_HLT')

            elif col_mode == "NO_COLLISION":
                if hasattr(prop, "no_collision_placement_mode"):
                    col_box.prop(prop, "no_collision_placement_mode", text="Placement Shape")
                    nc_mode = str(getattr(prop, "no_collision_placement_mode", "FLAT_PLANE") or "FLAT_PLANE")
                    if nc_mode == "MATCH_MESH":
                        col_box.label(
                            text="ℹ️ Skater passes through prop, while Quick Drop cursor grabs the 3D mesh shape.",
                            icon='INFO',
                        )
                    else:
                        col_box.label(
                            text="ℹ️ Skater passes through prop; Quick Drop uses a flat ground-plane placement wafer.",
                            icon='INFO',
                        )

            if hasattr(prop, "hide_visual_during_col_edit"):
                col_box.prop(
                    prop,
                    "hide_visual_during_col_edit",
                    text="Completely Hide Visual Prop While Editing Collision",
                    icon='HIDE_ON',
                )

            # Quick Drop Placement Proxy (_PPHYS)
            pphys_box = card.box()
            pphys_head = pphys_box.row(align=True)
            pphys_head.label(text="📦 Quick Drop Placement Proxy (_PPHYS):", icon='MOD_WIREFRAME')
            if hasattr(prop, "show_placement_proxy_overlay"):
                pphys_head.prop(prop, "show_placement_proxy_overlay", text="GPU Cage", toggle=True, icon='HIDE_OFF')

            pphys_box.prop(prop, "placement_proxy_mode", text="Placement Mode")
            p_mode = str(getattr(prop, "placement_proxy_mode", "MATCH_COLLISION") or "MATCH_COLLISION")
            pphys_proxy = compiler_bridge.resolve_custom_placement_proxy_object(context, obj) if (obj and hasattr(compiler_bridge, "resolve_custom_placement_proxy_object")) else None
            is_editing_pphys = bool(
                active_raw and active_raw.type == 'MESH' and active_raw.mode == 'EDIT'
                and (
                    active_raw == pphys_proxy
                    or active_raw.name.upper().endswith("_PPHYS")
                    or bool(active_raw.get("reskate_is_pphys_proxy", False))
                )
            )

            if is_editing_pphys:
                done_p_row = pphys_box.row(align=True)
                done_p_row.scale_y = 1.25
                done_p_row.operator(
                    "reskate.toggle_edit_collision_mesh",
                    text=f"Done Editing Placement Mesh ({active_raw.name})",
                    icon='CHECKMARK',
                )
            elif p_mode in ("MATCH_COLLISION", "TIGHT_SIMPLIFIED", "CUSTOM_OBJECT"):
                if p_mode == "MATCH_COLLISION":
                    pphys_box.label(
                        text="✅ Auto-builds up to 8 placement hulls (_pphys_0..7) for legs, gaps, curves & ramps.",
                        icon='CHECKMARK',
                    )
                elif hasattr(prop, "pphys_target_tris"):
                    pphys_box.prop(prop, "pphys_target_tris", text="Target Placement Tris", slider=True)
                p_btn_row = pphys_box.row(align=True)
                p_btn_row.scale_y = 1.15
                gen_op = p_btn_row.operator(
                    "reskate.generate_placement_proxy",
                    text="Regenerate _PPHYS Preview" if pphys_proxy else "Preview / Generate _PPHYS (Optional)",
                    icon='MOD_DECIM',
                )
                gen_op.enter_edit_mode = False
                gen_op.force_regenerate = True

                ed_op = p_btn_row.operator(
                    "reskate.generate_placement_proxy",
                    text=f"Edit ({pphys_proxy.name})" if pphys_proxy else "Generate & Edit",
                    icon='EDITMODE_HLT',
                )
                ed_op.enter_edit_mode = True
                ed_op.force_regenerate = (pphys_proxy is None)

                if pphys_proxy and pphys_proxy.type == 'MESH':
                    p_verts = len(pphys_proxy.data.vertices)
                    p_tris = _fast_mesh_tri_count(pphys_proxy.data)
                    pphys_box.label(
                        text=f"✅ Active Placement Mesh: {pphys_proxy.name} ({p_verts:,} verts, {p_tris:,} tris)",
                        icon='CHECKMARK',
                    )
                if p_mode == "CUSTOM_OBJECT":
                    pphys_box.prop(prop, "custom_placement_proxy_object", text="Proxy Object", icon='OBJECT_DATA')

            # Edge Straightener
            _draw_edge_straightener_box(card, prop, active_raw, col_proxy=col_proxy)

            # Whole-Prop Default Surface & Prevent Wall-Snap
            surf_box = card.box()
            surf_box.label(text="Fallback Whole-Prop Default Surface & Wall-Snap:", icon='PHYSICS')
            surf_box.prop(prop, "primary_surface", text="Default Surface")
            surf_box.prop(
                prop,
                "prevent_wall_snap_default",
                text="Prevent Wall-Snap (Vertical Faces / Do Not Align)",
                icon='MOD_NORMALEDIT',
            )

            # Per-Face Surface Audio Override Painter
            if obj and hasattr(prop, "face_paint_surface"):
                face_groups = compiler_bridge.get_object_face_surface_summary(obj, context)
                is_edit_any = bool(active_raw and active_raw.type == 'MESH' and active_raw.mode == 'EDIT')
                show_fp = is_edit_any or bool(face_groups) or bool(getattr(prop, "show_face_surface_painter", False))
                fp_box = card.box()
                fp_head = fp_box.row(align=True)
                if hasattr(prop, "show_face_surface_painter") and not (is_edit_any or face_groups):
                    fp_icon = 'TRIA_DOWN' if show_fp else 'TRIA_RIGHT'
                    fp_head.prop(
                        prop,
                        "show_face_surface_painter",
                        text="🎨 Per-Face Surface Audio Override (Edit Mode)",
                        icon=fp_icon,
                        emboss=False,
                    )
                else:
                    fp_head.label(text="🎨 Per-Face Surface Audio Override (Edit Mode):", icon='FACESEL')
                if hasattr(prop, "draw_face_surface_overlay"):
                    fp_head.prop(prop, "draw_face_surface_overlay", text="GPU Overlay", toggle=True, icon='HIDE_OFF')

                if show_fp:
                    fp_box.prop(prop, "face_paint_surface", text="Face Sound")
                    fp_box.prop(
                        prop,
                        "face_paint_prevent_wall_snap",
                        text="Prevent Wall-Snap on Painted Faces",
                        icon='MOD_NORMALEDIT',
                    )
                    btn_row = fp_box.row(align=True)
                    btn_row.scale_y = 1.2
                    btn_row.operator("reskate.assign_face_surface", text="Apply Sound to Selected Faces", icon='BRUSH_DATA')
                    btn_row.operator("reskate.clear_face_surface", text="Clear Selected", icon='LOOP_BACK')

                    if face_groups:
                        grp_col = fp_box.column(align=True)
                        grp_col.label(text=f"Active Custom Face Surfaces ({len(face_groups)} group(s)):", icon='SOUND')
                        for grp in face_groups:
                            g_code = int(grp.get("code", 0))
                            g_lbl = str(grp.get("label", f"Code {g_code}"))
                            g_cnt = int(grp.get("face_count", 0))
                            g_src = str(grp.get("source_object", obj.name))
                            src_tag = " [_COL]" if g_src != obj.name else ""
                            g_row = grp_col.row(align=True)
                            g_row.label(text=f"🔊 {g_lbl} — {g_cnt} face(s) (#{g_code}){src_tag}")
                            sel_op = g_row.operator("reskate.select_face_surface_group", text="Select", icon='RESTRICT_SELECT_OFF')
                            sel_op.target_code = g_code
                            clr_op = g_row.operator("reskate.clear_all_face_surfaces", text="", icon='X', emboss=False)
                            clr_op.target_code = g_code
                        clr_all_row = fp_box.row(align=True)
                        clr_all_op = clr_all_row.operator("reskate.clear_all_face_surfaces", text="Clear All Custom Face Surfaces", icon='TRASH')
                        clr_all_op.target_code = 0


# ============================================================================
# Step 3: Shaders, Sound & Look (`Simple` & `Advanced` Modes)
# ============================================================================

class RESKATE_UL_material_slots(UIList):
    """UIList displaying each Material Slot with its inline Surface & Sound dropdown right on the row."""
    bl_idname = "RESKATE_UL_material_slots"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        slot = item
        mat = getattr(slot, "material", None)
        obj = data if isinstance(data, bpy.types.Object) else None
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if mat is not None:
                sk8 = _sync_shader_role_display_value(obj, index, mat)
                split = row.split(factor=0.44, align=True)
                split.prop(mat, "name", text="", emboss=False, icon='MATERIAL')
                if sk8 is not None and hasattr(sk8, "surface_role_preset"):
                    split.prop(sk8, "surface_role_preset", text="")
                else:
                    split.label(text="Concrete (Smooth)")
            else:
                row.label(text=f"Slot {index + 1} (Empty)", icon='MATERIAL')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MATERIAL')

def _get_roughness_summary_safe(obj, mat) -> dict:
    """Safely fetches in-game roughness matte/glossy % breakdown from compiler_bridge with local fallback."""
    fn = getattr(compiler_bridge, "get_material_roughness_summary", None)
    if callable(fn):
        try:
            res = fn(obj, mat)
            if isinstance(res, dict) and "mean_rough" in res:
                return res
        except Exception:
            pass
    shader_info = compiler_bridge.inspect_material_shader_nodes(mat) if mat else {}
    r_img = shader_info.get("roughness_image")
    mean_r = float(shader_info.get("roughness_value", 0.45) or 0.45)
    is_inv = False
    if mat and mat.use_nodes and mat.node_tree:
        inv_n = mat.node_tree.nodes.get("ReSkate_Roughness_Invert")
        is_inv = inv_n is not None and not getattr(inv_n, "mute", False)
    matte_pct = max(0.0, min(100.0, mean_r * 100.0))
    glossy_pct = 100.0 - matte_pct
    return {
        "mean_rough": mean_r,
        "matte_pct": matte_pct,
        "glossy_pct": glossy_pct,
        "has_map": r_img is not None,
        "is_inverted_in_nodes": is_inv,
    }


def _draw_texture_slot_thumb_row(layout, slot_label: str, img, channel_badge: str = ""):
    """Draws a compact inline square thumbnail preview + resolution + channel badge for a resolved Blender Image."""
    if img is None:
        return
    row = layout.row(align=True)
    try:
        img.preview_ensure()
        if getattr(img, "preview", None) and getattr(img.preview, "icon_id", 0):
            row.template_icon(icon_value=img.preview.icon_id, scale=2.2)
    except Exception:
        pass
    col = row.column(align=True)
    ch_str = f" [{channel_badge}]" if channel_badge else ""
    col.label(text=f"{slot_label}: {img.name}{ch_str}", icon='IMAGE_DATA')
    try:
        w_px, h_px = int(img.size[0]), int(img.size[1])
        if w_px > 0 and h_px > 0:
            col.label(text=f"Resolution: {w_px}×{h_px} px")
    except Exception:
        pass


def _draw_sk8_material_settings(layout, mat, obj=None, slot_index: int = 0, ui_mode: str = "SIMPLE"):
    """Draws the unified per-shader Decal options + collapsible texture slots, large previews & Cycles baker."""
    sk8 = _sync_shader_role_display_value(obj, slot_index, mat)
    if sk8 is None:
        layout.label(text="Material properties unavailable", icon='INFO')
        return

    shader_info = compiler_bridge.inspect_material_shader_nodes(mat)

    # 1. If Decal Overlay role is selected on this shader, show Decal Shader Mode & Alpha Source options
    if getattr(sk8, "is_decal_slot", False) or getattr(sk8, "surface_role_preset", "") == "DECAL_OVERLAY":
        dec_box = layout.box()
        dec_box.label(text=f"Decal / Cutout Settings ({mat.name}):", icon='MOD_MASK')
        if hasattr(sk8, "decal_shader_mode"):
            dec_box.prop(sk8, "decal_shader_mode", text="Decal Mode")
        if hasattr(sk8, "decal_alpha_mode"):
            dec_box.prop(sk8, "decal_alpha_mode", text="Alpha Source")
        dec_mode = getattr(sk8, "decal_shader_mode", "CUTOUT_273") or "CUTOUT_273"
        if dec_mode == "METALLIC_771":
            dec_box.label(
                text="✅ Metallic Decal Mode (0x771 Full PBR): Supports Metallic + Roughness + Normal & excludes decal faces from skater collision.",
                icon='CHECKMARK',
            )
        else:
            dec_box.label(
                text="✅ Standard Decal Mode (0x273 Alpha Cutout — Method C): Crisp alpha cutout + Roughness + Normal & zero skater collision.",
                icon='CHECKMARK',
            )

    # 2. Collapsible Texture Map Inspector (Unified 4-Slot List + Large Pop-Out Preview)
    tex_box = layout.box()
    show_tex = bool(getattr(sk8, "show_texture_slots", False)) or (ui_mode == "ADVANCED")
    head_row = tex_box.row(align=True)
    head_row.scale_y = 1.15
    if hasattr(sk8, "show_texture_slots"):
        head_row.prop(
            sk8,
            "show_texture_slots",
            text="View / Adjust Shader Textures (Color, Normal, Roughness, Metal)",
            icon='TRIA_DOWN' if show_tex else 'TRIA_RIGHT',
            emboss=False,
        )
    else:
        head_row.label(text="Material Textures (Auto-Synced from Blender Shader)", icon='TEXTURE')
    head_row.operator("reskate.preview_material_textures_popup", text="Large Preview", icon='ZOOM_IN')
    head_row.operator("reskate.sync_shader_to_sk8_material", text="", icon='FILE_REFRESH')

    use_ovr = bool(getattr(sk8, "use_custom_texture_overrides", False))
    b_img = (getattr(sk8, "base_color_image", None) if use_ovr else None) or shader_info.get("base_color_image") or getattr(sk8, "base_color_image", None)
    n_img = (getattr(sk8, "normal_image", None) if use_ovr else None) or shader_info.get("normal_image") or getattr(sk8, "normal_image", None)
    r_img = (getattr(sk8, "roughness_image", None) if use_ovr else None) or shader_info.get("roughness_image") or getattr(sk8, "roughness_image", None)
    r_ch = shader_info.get("roughness_channel") or getattr(sk8, "roughness_channel", "") or "G"
    m_img = (getattr(sk8, "metallic_image", None) if use_ovr else None) or shader_info.get("metallic_image") or getattr(sk8, "metallic_image", None)
    m_ch = shader_info.get("metallic_channel") or getattr(sk8, "metallic_channel", "") or "B"
    msk_img = getattr(sk8, "mask_image", None)
    op_img = getattr(sk8, "opacity_image", None)

    present_cnt = sum(1 for im in (b_img, n_img, r_img, m_img) if im is not None)
    missing_names = [
        lbl for lbl, im in (("Base Color", b_img), ("Normal", n_img), ("Roughness", r_img), ("Metallic", m_img))
        if im is None
    ]
    if present_cnt == 4:
        tex_box.label(text="✅ All 4 PBR Texture Maps Active (Color, Normal, Roughness, Metallic)", icon='CHECKMARK')
    elif present_cnt > 0:
        tex_box.label(
            text=f"🖼️ {present_cnt}/4 Maps Active  |  Flat/Missing: {', '.join(missing_names)}",
            icon='IMAGE_DATA',
        )
    else:
        b_lbl = "Procedural Shader (Use Game-Ready Image Maps)" if shader_info.get("has_procedural_base") else "Solid Shader Color"
        tex_box.label(text=f"ℹ️ No Image Maps Assigned ({b_lbl})", icon='NODE_MATERIAL')

    if show_tex:
        # Unified 4-Slot Texture List (Single combined list with preview icon + picker + missing badge!)
        slots_spec = [
            ("Base Color", "base_color_image", b_img, None),
            ("Normal Map", "normal_image", n_img, None),
            ("Roughness", "roughness_image", r_img, "roughness_channel"),
            ("Metallic", "metallic_image", m_img, "metallic_channel"),
        ]
        for slot_label, prop_attr, active_img, chan_attr in slots_spec:
            s_box = tex_box.box()
            s_row = s_box.row(align=True)
            if active_img is not None:
                try:
                    active_img.preview_ensure()
                    if getattr(active_img, "preview", None) and getattr(active_img.preview, "icon_id", 0):
                        s_row.template_icon(icon_value=active_img.preview.icon_id, scale=2.8)
                except Exception:
                    pass
            col_r = s_row.column(align=True)
            top_r = col_r.row(align=True)
            if active_img is not None:
                w_px = int(active_img.size[0]) if len(active_img.size) > 0 else 0
                h_px = int(active_img.size[1]) if len(active_img.size) > 1 else 0
                res_txt = f" ({w_px}×{h_px})" if (w_px > 0 and h_px > 0) else ""
                top_r.label(text=f"✅ {slot_label}: {active_img.name}{res_txt}", icon='IMAGE_DATA')
            else:
                top_r.label(text=f"⚠️ {slot_label} (Missing — Uses Flat Shader Value):", icon='ERROR')

            pick_r = col_r.row(align=True)
            pick_r.prop(sk8, prop_attr, text="")
            if chan_attr and hasattr(sk8, chan_attr):
                pick_r.prop(sk8, chan_attr, text="")
            if getattr(sk8, prop_attr, None) is not None:
                clr_op = pick_r.operator("reskate.clear_material_texture_slot", text="", icon='X', emboss=False)
                clr_op.slot_name = prop_attr

        if hasattr(sk8, "flip_normal_green") and n_img is not None:
            tex_box.prop(sk8, "flip_normal_green", text="Flip Normal Green Channel (DirectX <-> OpenGL)")

        if r_img is not None:
            r_sum = _get_roughness_summary_safe(obj, mat)
            tex_box.operator(
                "reskate.toggle_roughness_invert",
                text="⇄ Invert Roughness Map (Gloss ↔ Roughness)",
                icon='ARROW_LEFTRIGHT',
                depress=bool(r_sum.get("is_inverted_in_nodes", False)),
            )

        if hasattr(sk8, "use_custom_texture_overrides"):
            tex_box.prop(
                sk8,
                "use_custom_texture_overrides",
                text="Show Packed Mask (_msk) & Cutout Opacity Slots",
                icon='MOD_MASK',
            )
        if bool(getattr(sk8, "use_custom_texture_overrides", False)):
            msk_row = tex_box.row(align=True)
            msk_row.prop(sk8, "mask_image", text="Mask (_msk)")
            if getattr(sk8, "mask_image", None) is not None:
                clr_msk = msk_row.operator("reskate.clear_material_texture_slot", text="", icon='X', emboss=False)
                clr_msk.slot_name = "mask_image"
            op_row = tex_box.row(align=True)
            op_row.prop(sk8, "opacity_image", text="Opacity")
            if getattr(sk8, "opacity_image", None) is not None:
                clr_op = op_row.operator("reskate.clear_material_texture_slot", text="", icon='X', emboss=False)
                clr_op.slot_name = "opacity_image"


def _get_texture_archive_ui_status(slug_or_context=None, prop=None) -> tuple[str, str, str]:
    """Returns (status_key, status_label, icon) for texture archive UI display."""
    slug = ""
    if slug_or_context is not None:
        if isinstance(slug_or_context, str):
            slug = slug_or_context
        elif hasattr(slug_or_context, "scene"):
            c_prop = getattr(slug_or_context.scene, "reskate_prop", None)
            if prop is None:
                prop = c_prop
            if prop and hasattr(prop, "get_selected_blender_org_slug"):
                slug = prop.get_selected_blender_org_slug()

    if not slug and prop:
        if hasattr(prop, "get_selected_blender_org_slug"):
            slug = prop.get_selected_blender_org_slug()
        elif hasattr(prop, "blender_org_category"):
            cat = getattr(prop, "blender_org_category", "CONCRETE")
            slug = getattr(prop, f"blender_org_{cat.lower()}_variant", "")

    if not slug:
        return ("ONLINE", "🌐 Online (Ready to Download from Blender.org)", 'INTERNET')

    try:
        leaf_slug = slug.split("/")[-1].strip()
        staging = ""
        prefs = _props.get_addon_preferences() if hasattr(_props, "get_addon_preferences") else None
        if prefs and getattr(prefs, "staging_dir", ""):
            staging = prefs.staging_dir
        if not staging:
            staging = os.environ.get("RESKATE_STAGING_DIR", "")
        if not staging:
            staging = str(compiler_bridge._get_staging_dir())

        cache_dir = Path(staging) / "BlenderOrg_TextureCache" / leaf_slug
        if cache_dir.exists():
            for f in cache_dir.iterdir():
                fn_lower = f.name.lower()
                if ("-color" in fn_lower or "_color" in fn_lower) and f.is_file() and f.stat().st_size > 0:
                    return ("CACHED", "🟢 Cached on Disk (Instant Apply)", 'CHECKMARK')
    except Exception:
        pass

    if prop and not getattr(prop, "use_blender_org_textures", True):
        return ("OFFLINE", "🟡 Procedural Mode (Offline)", 'ERROR')

    return ("ONLINE", "🌐 Online (Ready to Download from Blender.org)", 'INTERNET')


class RESKATE_PT_card4_textures(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card4_textures"
    bl_label = "🟪 3. Shaders, Sound & Look"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card4", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        r_maps = []
        m_maps = []
        active_mat = None
        if obj and obj.type == 'MESH' and len(obj.material_slots) > 0:
            active_mat = obj.active_material or obj.material_slots[0].material
            for s in obj.material_slots:
                if s.material:
                    s_inf = compiler_bridge.inspect_material_shader_nodes(s.material)
                    sk8_s = getattr(s.material, "reskate_mat", None) or getattr(s.material, "sk8_material", None)
                    r_img = (getattr(sk8_s, "roughness_image", None) if sk8_s else None) or s_inf.get("roughness_image")
                    m_img = (getattr(sk8_s, "metallic_image", None) if sk8_s else None) or s_inf.get("metallic_image")
                    if r_img is not None:
                        r_maps.append(f"{s.material.name}: {r_img.name}")
                    if m_img is not None:
                        m_maps.append(f"{s.material.name}: {m_img.name}")

        # 1. Material Slot List (with Inline Surface & Sound Dropdown!) + Collapsible Texture Slots
        if obj and obj.type == 'MESH':
            _draw_multi_shader_atlas_warning(card, obj, prop)
            mat_box = card.box()
            head = mat_box.row(align=True)
            is_multi_m = len(obj.material_slots) > 1
            head.label(
                text=f"Prop Shaders & Surface Sound ({len(obj.material_slots)} Slot{'s' if is_multi_m else ''})",
                icon='MATERIAL',
            )
            head.operator("reskate.preview_material_textures_popup", text="Preview", icon='ZOOM_IN')
            head.operator("reskate.sync_shader_to_sk8_material", text="Sync", icon='FILE_REFRESH')

            list_row = mat_box.row()
            list_row.template_list(
                "RESKATE_UL_material_slots", "reskate_top_matslots",
                obj, "material_slots",
                obj, "active_material_index",
                rows=max(2, min(5, len(obj.material_slots) + 1)),
            )
            slot_btns = list_row.column(align=True)
            slot_btns.operator("object.material_slot_add", icon='ADD', text="")
            slot_btns.operator("object.material_slot_remove", icon='REMOVE', text="")
            if obj.mode == 'EDIT':
                slot_btns.separator()
                slot_btns.operator("object.material_slot_assign", icon='CHECKMARK', text="")

            mat_box.template_ID(obj, "active_material", new="material.new")

            act_idx = int(getattr(obj, "active_material_index", 0) or 0)
            if active_mat is not None:
                _draw_sk8_material_settings(mat_box, active_mat, obj=obj, slot_index=act_idx, ui_mode=ui_mode)
            else:
                mat_box.label(text="No material assigned — click '+ New' above or expand Starter Templates below.", icon='INFO')

        # 2. Collapsible Starter Material Templates & Blender.org PBR Archive (Available in Both Simple & Advanced!)
        card.separator(factor=0.6)
        pbr_box = card.box()
        pbr_head = pbr_box.row(align=True)
        show_tpl = bool(getattr(prop, "show_starter_templates", False)) or (active_mat is None)
        tpl_icon = 'TRIA_DOWN' if show_tpl else 'TRIA_RIGHT'
        if hasattr(prop, "show_starter_templates"):
            pbr_head.prop(
                prop,
                "show_starter_templates",
                text="🎨 Starter Material Templates & Blender.org PBR Archive",
                icon=tpl_icon,
                emboss=False,
            )
        else:
            pbr_head.label(text="🎨 Starter Material Templates & Blender.org PBR Archive:", icon='MATERIAL')

        if show_tpl:
            curr_preset = str(getattr(prop, "look_preset", "CUSTOM") or "CUSTOM").upper()
            tpl_row1 = pbr_box.row(align=True)
            tpl_row1.scale_y = 1.2
            for t_key, t_lbl, t_icon in (
                ("CONCRETE", "Concrete", 'MOD_BUILD'),
                ("WOOD", "Wood", 'TEXTURE'),
                ("METAL", "Metal", 'SHADING_RENDERED'),
            ):
                op_t = tpl_row1.operator(
                    "reskate.apply_starter_material_template",
                    text=t_lbl,
                    icon=t_icon,
                    depress=(curr_preset == t_key),
                )
                op_t.template = t_key
                op_t.template_id = t_key
            tpl_row2 = pbr_box.row(align=True)
            tpl_row2.scale_y = 1.2
            for t_key, t_lbl, t_icon in (
                ("ASPHALT", "Asphalt", 'FORCE_TURBULENCE'),
                ("MARBLE", "Marble", 'NODE_MATERIAL'),
                ("PAINTED", "Painted", 'BRUSH_DATA'),
            ):
                op_t = tpl_row2.operator(
                    "reskate.apply_starter_material_template",
                    text=t_lbl,
                    icon=t_icon,
                    depress=(curr_preset == t_key),
                )
                op_t.template = t_key
                op_t.template_id = t_key

            pbr_box.separator(factor=0.3)
            tog_row = pbr_box.row(align=True)
            tog_row.prop(prop, "use_blender_org_textures", text="Download Realistic PBR Maps (Blender.org Archive)", icon='WORLD_DATA')

            if getattr(prop, "use_blender_org_textures", True):
                arc_box = pbr_box.box()
                c_row = arc_box.row(align=True)
                c_row.prop(prop, "blender_org_category", text="Category")
                cat = getattr(prop, "blender_org_category", "CONCRETE")
                v_prop_name = f"blender_org_{cat.lower()}_variant"
                if hasattr(prop, v_prop_name):
                    arc_box.prop(prop, v_prop_name, text="Variant")
                _status_key, status_label, status_icon = _get_texture_archive_ui_status(context, prop)
                arc_box.label(text=status_label, icon=status_icon)
                apply_row = arc_box.row(align=True)
                apply_row.scale_y = 1.2
                op_apply = apply_row.operator(
                    "reskate.apply_starter_material_template",
                    text=f"Download & Apply {cat.title()} Texture",
                    icon='IMPORT',
                )
                op_apply.template = cat
                op_apply.template_id = cat
                arc_box.prop(prop, "blender_org_offline_fallback", text="Auto-fallback to procedural if offline")

        # 3. Advanced Mode Controls: UV Tiling & World-Scale UVs, 2x2m Detail Grain, PBR Neutral, Export Res
        if ui_mode == 'ADVANCED':
            card.separator(factor=0.8)

            # UV Tiling & World-Scale Projection
            uv_box = card.box()
            uv_box.label(text="📏 UV Tiling & World-Scale Projection:", icon='UV_DATA')
            if hasattr(prop, "uv_tiling_mult"):
                uv_row = uv_box.row(align=True)
                uv_row.prop(prop, "uv_tiling_mult", text="UV Tiling Scale", slider=True)
                uv_row.operator("reskate.apply_uv_tiling_scale", text="Apply to UVs", icon='UV')
            if hasattr(prop, "world_scale_tile_size_m"):
                ws_row = uv_box.row(align=True)
                ws_row.prop(prop, "world_scale_tile_size_m", text="Tile Size (m)", slider=True)
                if obj and len(obj.material_slots) > 1 and hasattr(prop, "world_scale_uv_scope"):
                    ws_row.prop(prop, "world_scale_uv_scope", text="")
                ws_btn = uv_box.row(align=True)
                ws_btn.scale_y = 1.2
                ws_btn.operator(
                    "reskate.apply_world_scale_uvs",
                    text="Auto World-Scale Tiling UVs (1-Click)",
                    icon='MOD_UVPROJECT',
                )

            # 2x2m Micro-Detail Shader, PBR Neutral Viewport & Export Resolution
            adv_box = card.box()
            adv_box.label(text="2×2m Detail Grain, Viewport & Resolution:", icon='TEXTURE')
            adv_box.prop(
                prop,
                "use_2x2m_detail_shader",
                text="Enable 2×2m Micro-Detail Surface (Concrete/Wood/Metal Grain)",
                icon='MOD_OCEAN',
            )
            if getattr(prop, "use_2x2m_detail_shader", False):
                det_col = adv_box.column(align=True)
                det_col.prop(prop, "detail_2x2m_preset", text="Micro-Detail Preset")
                det_col.prop(prop, "detail_2x2m_intensity", text="Detail Intensity", slider=True)
                if hasattr(prop, "detail_2x2m_scale"):
                    det_col.prop(prop, "detail_2x2m_scale", text="Tiling Scale (m)", slider=True)

            adv_box.operator(
                "reskate.toggle_pbr_neutral_viewport",
                text="Toggle PBR Neutral Viewport Preview",
                icon='COLOR',
            )
            adv_box.prop(prop, "atlas_resolution", text="Texture Export Resolution (Up to 8K)")
            adv_box.prop(prop, "enable_custom_textures", text="Pack & Enable Custom Textures in Mod", icon='TEXTURE')
            adv_box.prop(prop, "albedo_tint", text="Color Tint")
            adv_box.prop(prop, "dielectric_specular", text="Fallback Specular", slider=True)
            adv_box.prop(prop, "use_external_texture_override", text="Use External Texture File Override")
            if getattr(prop, "use_external_texture_override", False):
                adv_box.prop(prop, "custom_texture_path", text="Texture File")


class RESKATE_PT_sk8_material_fallback(Panel):
    """Fallback Properties > Material > Skate Map panel when sk8_map_export is not loaded."""
    bl_idname = "RESKATE_PT_sk8_material_fallback"
    bl_label = "Skate Map"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        if getattr(context, "material", None) is None:
            return False
        return not hasattr(bpy.types, "SK8_PT_material")

    def draw(self, context):
        mat = context.material
        if not mat:
            return
        obj = getattr(context, "object", None)
        act_idx = int(getattr(obj, "active_material_index", 0) or 0) if obj else 0
        box = self.layout.box()
        box.label(text="Native Render Material", icon='SHADING_RENDERED')
        _draw_sk8_material_settings(box, mat, obj=obj, slot_index=act_idx, ui_mode="ADVANCED")


# ============================================================================
# Step 4: Light (in-game) (`Simple` & `Advanced` Modes)
# ============================================================================

class RESKATE_UL_linked_lights(UIList):
    """UIList displaying Blender Spot/Point lights explicitly linked to the active prop."""
    bl_idname = "RESKATE_UL_linked_lights"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="", icon_only=True)
            row.prop(item, "light_obj", text="", icon='LIGHT')
            l_obj = getattr(item, "light_obj", None)
            if l_obj and l_obj.type == 'LIGHT' and l_obj.data:
                row.label(text=f"{l_obj.data.type.title()} ({l_obj.data.energy:.0f}W)")
            del_op = row.operator("reskate.unlink_light_slot", text="", icon='X', emboss=False)
            del_op.index = index
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='LIGHT')


class RESKATE_PT_card5_lights(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card5_lights"
    bl_label = "💡 4. Light (in-game)"
    bl_order = 4

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card5", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        # Add Spot / Point Light buttons
        add_row = card.row(align=True)
        add_row.scale_y = 1.4
        op_spot = add_row.operator("reskate.add_dynamic_prop_light", text="+ Add Spot Light", icon='LIGHT_SPOT')
        op_spot.light_type = 'SPOT'
        op_pt = add_row.operator("reskate.add_dynamic_prop_light", text="+ Add Point Light", icon='LIGHT_POINT')
        op_pt.light_type = 'POINT'

        card.separator(factor=0.6)

        # Explicit "Lights on this prop" list
        l_box = card.box()
        head = l_box.row(align=True)
        head.label(text="Lights on this prop:", icon='LIGHT')
        head.operator("reskate.link_light_slot", text="Link Existing Light", icon='ADD')

        if obj and hasattr(obj, "reskate_linked_lights"):
            l_box.template_list(
                "RESKATE_UL_linked_lights", "",
                obj, "reskate_linked_lights",
                obj, "reskate_linked_light_index",
                rows=2, maxrows=5,
            )
            idx = obj.reskate_linked_light_index
            if 0 <= idx < len(obj.reskate_linked_lights):
                sel_slot = obj.reskate_linked_lights[idx]
                l_obj = getattr(sel_slot, "light_obj", None)
                if l_obj and l_obj.type == 'LIGHT' and l_obj.data:
                    l_data = l_obj.data
                    d_box = l_box.box()
                    d_col = d_box.column(align=True)
                    d_col.label(text=f"Selected Light ({l_obj.name}):", icon='PREFERENCES')
                    act_row = d_col.row(align=True)
                    sel_l_op = act_row.operator("reskate.select_linked_light", text="Select in Viewport", icon='RESTRICT_SELECT_OFF')
                    sel_l_op.index = idx
                    del_l_op = act_row.operator("reskate.delete_linked_light", text="Delete Light", icon='TRASH')
                    del_l_op.index = idx
                    d_col.separator(factor=0.4)
                    d_col.prop(l_data, "color", text="Color")
                    d_col.prop(l_data, "energy", text="Intensity (W)")

                    if ui_mode == 'ADVANCED':
                        d_col.separator(factor=0.4)
                        d_col.prop(l_data, "type", text="Light Type")
                        d_col.prop(l_data, "use_custom_distance", text="Use Custom Radius")
                        if getattr(l_data, "use_custom_distance", False):
                            d_col.prop(l_data, "cutoff_distance", text="Radius (m)")
                        if l_data.type == 'SPOT':
                            d_col.prop(l_data, "spot_size", text="Cone Angle")
                            d_col.prop(l_data, "spot_blend", text="Cone Softness", slider=True)
                        if hasattr(l_data, "specular_factor"):
                            d_col.prop(l_data, "specular_factor", text="Specular", slider=True)
                        d_col.prop(l_data, "use_shadow", text="Cast Shadows")
                        d_col.separator(factor=0.3)
                        d_col.prop(l_obj, "location", text="Position")
                        if l_data.type == 'SPOT':
                            d_col.prop(l_obj, "rotation_euler", text="Rotation")
        else:
            l_box.label(text="Select a prop mesh to link lights", icon='INFO')

        if hasattr(prop, "draw_light_overlay"):
            card.prop(prop, "draw_light_overlay", text="Show Light Cones / Spheres Overlay in Viewport", icon='RESTRICT_VIEW_OFF')


# ============================================================================
# Step 5: Thumbnail (`Simple` & `Advanced` Modes)
# ============================================================================

class RESKATE_PT_card5_lights_thumb(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card5_lights_thumb"
    bl_label = "📸 5. Thumbnail"
    bl_order = 5

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card5", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        card.label(
            text="💡 Optional — Frame/render an icon below, or pick an image. If skipped, ReSkate uses the static 'No Thumbnail' icon on compile.",
            icon='INFO',
        )
        card.separator(factor=0.4)

        # Live Thumbnail Preview Box + Pop-Out Preview Dialog
        prev_box = card.box()
        prev_head = prev_box.row(align=True)
        prev_head.label(text="Quick Drop Icon Preview:", icon='IMAGE_DATA')
        prev_head.operator("reskate.preview_thumbnail_popup", text="Pop-Out Preview", icon='ZOOM_IN')
        thumb_path = ""
        owner = str(prop.get("custom_thumb_owner", "")) if hasattr(prop, "get") else ""
        if prop.custom_thumb_png and _cached_path_exists(prop.custom_thumb_png) and (not owner or (obj and owner == obj.name)):
            thumb_path = prop.custom_thumb_png
        elif obj:
            slug = compiler_bridge.get_prop_slug(context, obj)
            staging_dir = compiler_bridge.get_staging_dir(context)
            t256_str = str(obj.get("reskate_rendered_thumb_256", "")) or str(staging_dir / f"{slug}_thumb_256.png")
            if _cached_path_exists(t256_str):
                thumb_path = t256_str

        t_img = _get_cached_preview_image(thumb_path) if thumb_path else None
        if t_img is not None and getattr(t_img, "preview", None) and getattr(t_img.preview, "icon_id", 0):
            icon_row = prev_box.row()
            icon_row.alignment = 'CENTER'
            icon_row.template_icon(icon_value=t_img.preview.icon_id, scale=5.5)
            prev_box.label(text=f"File: {Path(thumb_path).name} (256×256 & 768×768)", icon='CHECKMARK')
        else:
            prev_box.label(text="No rendered icon on disk yet — click Render Thumbnail below.", icon='INFO')

        card.separator(factor=0.6)

        # Camera & Render Primary Buttons
        col = card.column(align=True)
        cam_row = col.row(align=True)
        cam_row.scale_y = 1.25
        orb_l = cam_row.operator("reskate.orbit_thumb_camera", text="↺ -45°", icon='LOOP_BACK')
        orb_l.angle_deg = -45.0
        cam_row.operator("reskate.frame_isometric_camera", text="Frame Camera", icon='VIEW_CAMERA')
        orb_r = cam_row.operator("reskate.orbit_thumb_camera", text="↻ +45°", icon='LOOP_FORWARDS')
        orb_r.angle_deg = 45.0
        col.operator("reskate.view_thumbnail_camera", text="View Camera (Numpad 0)", icon='CAMERA_DATA')

        card.separator(factor=0.4)
        render_btn = card.row()
        render_btn.scale_y = 1.5
        render_btn.operator("reskate.render_thumbnails", text="Render Thumbnail (256 & 768)", icon='RENDER_STILL')
        if hasattr(prop, "skip_existing_thumbnails"):
            card.prop(
                prop,
                "skip_existing_thumbnails",
                text="Reuse Existing Thumbnail on Compile (Skip Re-Render)",
            )

        card.separator(factor=0.6)

        # Studio Backdrop Color & Presets (Single Dropdown)
        bg_box = card.box()
        bg_box.label(text="Studio Backdrop & Floor:", icon='WORLD')
        if hasattr(prop, "thumb_bg_preset"):
            bg_box.prop(prop, "thumb_bg_preset", text="Preset")
        bg_box.prop(prop, "thumb_bg_color", text="Backdrop Color")
        bg_box.prop(prop, "thumb_use_cyclorama_floor", text="Shadow-Catching Studio Floor (Grounding Shadows)")

        card.separator(factor=0.4)
        card.prop(prop, "custom_thumb_png", text="Or Pick Prop Icon Image")

        if ui_mode == 'ADVANCED':
            card.separator(factor=0.6)
            lt_box = card.box()
            lt_box.label(text="Thumbnail Lighting & Scene Options:", icon='LIGHT_SUN')
            pr_row = lt_box.row(align=True)
            pr_row.operator("reskate.copy_thumb_lighting_preset", text="Copy Lighting", icon='COPYDOWN')
            pr_row.operator("reskate.paste_thumb_lighting_preset", text="Paste Lighting", icon='PASTEDOWN')
            lt_box.prop(prop, "thumb_lighting_mode", text="Rig")
            if getattr(prop, "thumb_lighting_mode", "STUDIO_3PT") == "SKY_SUN":
                s_row = lt_box.row(align=True)
                s_row.prop(prop, "thumb_sun_elevation", text="Sun Elevation", slider=True)
                s_row.prop(prop, "thumb_sun_rotation", text="Sun Rotation", slider=True)
            lt_box.prop(prop, "thumb_exposure", text="Exposure", slider=True)
            lt_box.prop(prop, "thumb_rim_tint", text="Rim Light Tint")
            lt_box.prop(prop, "thumb_render_as_is", text="Render Scene As-Is (Include Other Scene Objects)")


# ============================================================================
# Step 6: Ship (`Simple` & `Advanced` Modes)
# ============================================================================

class RESKATE_UL_pack_queue(UIList):
    """UIList displaying marked props for multi-prop pack compilation with live change detection."""
    bl_idname = "RESKATE_UL_pack_queue"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0, flt_flag=0):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            enabled_attr = "enabled" if hasattr(item, "enabled") else "is_enabled"
            row.prop(item, enabled_attr, text="", icon_only=True)
            name_text = item.display_name if item.display_name else item.name
            is_mod = compiler_bridge.is_queue_item_modified(context, item)
            if is_mod:
                row.label(text=f"{name_text} (Modified)", icon='ERROR')
                sync_op = row.operator("reskate.queue_sync_item", text="Sync", icon='FILE_REFRESH')
                sync_op.index = index
            else:
                row.label(text=name_text, icon='MESH_DATA')
                tri_val = getattr(item, "tri_count", getattr(item, "poly_count", 0))
                row.label(text=f"{tri_val:,} tris")
            del_op = row.operator("reskate.queue_remove_item", text="", icon='X', emboss=False)
            del_op.index = index
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MESH_DATA')


class RESKATE_PT_card6_export(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_card6_export"
    bl_label = "🟩 6. Ship"
    bl_order = 6

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'ADVANCED'

    def draw_header(self, context):
        self.layout.operator("wm.reskate_tutorial_card6", text="", icon='HELP')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        ui_mode = _get_ui_mode(context)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        card = layout.box()
        if prop is None:
            card.label(text="Prop Studio settings not initialized", icon='ERROR')
            return

        _draw_havok_ceiling_warning(card, context, obj, source_panel="ship")

        # Pre-flight Checklist with 1-Click Fix All & Red Alert Highlight
        _draw_preflight_checklist(card, context, obj, prop)

        _draw_batch_copy_row(card, context)

        # Prop Name, Prop ID & Quick Drop Menu Tab (Right where you package & ship the prop)
        id_box = card.box()
        id_box.label(text="Prop Name & Quick Drop Tab:", icon='FONT_DATA')
        id_col = id_box.column(align=True)
        id_col.scale_y = 1.15
        id_col.prop(prop, "prop_display_name", text="Prop Name", icon='FONT_DATA')
        if hasattr(prop, "prop_family_title"):
            fam_row = id_col.row(align=True)
            fam_row.prop(prop, "prop_family_title", text="Variant Family", icon='LINKED')
            if (getattr(prop, "prop_family_title", "") or "").strip() or (obj and str(obj.get("reskate_family_title", "") or "").strip()):
                fam_row.operator("reskate.clear_variant_family", text="", icon='X')

        canonical_id = compiler_bridge.get_canonical_prop_id(context, obj)
        if ui_mode == 'ADVANCED':
            id_row = id_box.row(align=True)
            is_locked = bool(getattr(prop, "prop_id_locked", False))
            if is_locked:
                id_row.prop(prop, "prop_id", text="Custom Prop ID", icon='LOCKED')
                id_row.operator("reskate.toggle_prop_id_lock", text="", icon='LOCKED')
            else:
                id_row.label(text=f"Prop ID (Auto): {canonical_id}", icon='CHECKMARK')
            id_box.operator("reskate.reset_stable_prop_key", text="Reset internal key", icon='FILE_REFRESH')

        if hasattr(prop, "custom_qd_tab"):
            id_col.prop(prop, "custom_qd_tab", text="Quick Drop Tab", icon='ASSET_MANAGER')
        _draw_clickable_qd_tabs_list(id_box, context, prop)

        sel_meshes_qd = compiler_bridge.get_selected_prop_meshes(context)
        if len(sel_meshes_qd) >= 2:
            id_box.operator(
                "reskate.group_selected_as_variants",
                text=f"Group {len(sel_meshes_qd)} Selected Props as Variants (1 Quick Drop Tile)",
                icon='LINKED',
            )
            id_box.operator(
                "reskate.apply_category_to_selected",
                text=f"Apply Quick Drop Tab to {len(sel_meshes_qd)} Selected Props",
                icon='COPYDOWN',
            )

        dup_row = id_box.row(align=True)
        dup_row.scale_y = 1.15
        dup_row.operator("reskate.duplicate_prop_variant", text="Duplicate Prop as New Variant", icon='DUPLICATE')

        # Target Mod Folder & Pack Info
        pack_meta_box = card.box()
        pack_meta_box.label(text="Prop Pack:", icon='PACKAGE')
        pack_meta_box.prop(prop, "target_mod_name", text="Prop Pack Name", icon='FILE_FOLDER')
        _draw_clickable_scene_prop_packs_list(pack_meta_box, context, prop)
        resolved_mod = compiler_bridge.get_resolved_target_mod_name(context, prop)

        if ui_mode == 'ADVANCED':
            v_row = pack_meta_box.row(align=True)
            v_row.prop(prop, "pack_version", text="Version")
            op_patch = v_row.operator("reskate.bump_pack_version", text="+Patch")
            op_patch.part = 'PATCH'
            op_minor = v_row.operator("reskate.bump_pack_version", text="+Minor")
            op_minor.part = 'MINOR'
            if hasattr(prop, "auto_bump_pack_version"):
                pack_meta_box.prop(
                    prop,
                    "auto_bump_pack_version",
                    text="Auto-Increment Patch Version on Thunderstore Export",
                )
            pack_meta_box.prop(prop, "pack_description", text="Description")
            if hasattr(prop, "mod_icon_path"):
                mi_row = pack_meta_box.row(align=True)
                mi_row.prop(prop, "mod_icon_path", text="Mod Icon (icon.png)")
                mi_row.operator("reskate.use_prop_thumb_as_mod_icon", text="", icon='IMAGE_DATA')
            if hasattr(prop, "mod_readme_path"):
                pack_meta_box.prop(prop, "mod_readme_path", text="Custom README.md")
            if hasattr(prop, "auto_disable_old_mod_versions"):
                pack_meta_box.prop(
                    prop,
                    "auto_disable_old_mod_versions",
                    text="Auto-Disable Older Mod Versions in mods.json",
                )

        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        n_sel = len(sel_meshes)
        mod_info = compiler_bridge.inspect_existing_mod_props(resolved_mod)
        if n_sel > 1:
            diff = compiler_bridge.compute_pack_compile_diff(context, resolved_mod, selected_objs=sel_meshes)
        elif obj:
            diff = compiler_bridge.compute_pack_compile_diff(context, resolved_mod, single_obj=obj)
        else:
            diff = {"adding": [], "updating": [], "unchanged": [], "keeping": []}

        n_add = len(diff["adding"])
        n_upd = len(diff["updating"])
        n_keep = len(diff["keeping"])
        is_gpu_exp = (ui_mode == "EXPERIMENTAL" and getattr(prop, "compiler_backend", "CPU") == "GPU_EXP")
        is_fast_pipe = (getattr(prop, "compile_pipeline_mode", "FAST_OPTIMIZED") != "LEGACY_FULL")
        sec_per_prop = 1.6 if is_fast_pipe else (2.2 if is_gpu_exp else 4.0)
        est_sec = max(1, int(round((n_add + n_upd) * sec_per_prop + n_keep * (0.2 if is_fast_pipe else 0.8))))
        sum_box = card.box()
        sum_box.label(
            text=f"⏳ Queued: {n_upd} updating, {n_add} adding, {n_keep} cached (~Est. {est_sec} sec)",
            icon='SORTTIME',
        )
        if hasattr(prop, "compile_pipeline_mode"):
            pipe_row = sum_box.row(align=True)
            pipe_row.prop(prop, "compile_pipeline_mode", text="Compile Speed")
            pipe_row.operator("reskate.clear_compiler_cache", text="🧹 Clear Build Cache", icon='TRASH')
        if hasattr(prop, "skip_existing_thumbnails"):
            sum_box.prop(
                prop,
                "skip_existing_thumbnails",
                text="Skip Re-Rendering Existing Thumbnails (Faster Compile)",
            )

        if not prop.export_fbmod_only and compiler_bridge.is_skate_exe_running():
            warn_box = card.box()
            warn_box.label(text="⚠️ Skate.exe is running — mod will stage safely and apply on next game restart.", icon='INFO')

        card.separator(factor=0.6)

        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            blk_box = card.box()
            blk_box.alert = True
            blk_box.label(text=block_reason, icon='ERROR')
            blk_row = blk_box.row(align=True)
            blk_row.alert = False
            blk_row.operator("reskate.open_addon_preferences", text="Open ReSkate Add-on Preferences", icon='PREFERENCES')

        has_blockers = bool(block_reason) or (obj is None and n_sel == 0)
        is_compiling = bool(getattr(prop, "is_compiling", False))

        # Non-blocking progress bar & Cancel button while compiling
        if is_compiling:
            prog_box = card.box()
            prog_box.label(text=prop.compile_step_label or "Compiling...", icon='SORTTIME')
            prog_box.prop(prop, "compile_progress", text="Progress", slider=True)
            if prop.compile_status_msg:
                prog_box.label(text=prop.compile_status_msg)
            prog_box.operator("reskate.cancel_compile", text="Cancel", icon='CANCEL')
        else:
            compile_row = card.row()
            compile_row.scale_y = 1.55
            compile_row.enabled = (not has_blockers)
            if n_sel >= 2:
                compile_row.operator(
                    "reskate.compile_selected_props",
                    text=f"Compile and Deploy Prop Pack ({n_sel} Props)",
                    icon='PLAY',
                )
            else:
                compile_row.operator(
                    "reskate.compile_and_deploy",
                    text="Compile and Deploy This Prop",
                    icon='PLAY',
                )

            if prop.compile_status_msg and prop.compile_has_error:
                err_box = card.box()
                err_box.alert = True
                err_box.label(text=prop.compile_status_msg, icon='ERROR')
                err_box.label(text="Full log: Edit → Preferences → Add-ons → ReSkate Prop Studio.", icon='INFO')

            open_row = card.row(align=True)
            open_row.scale_y = 1.25
            open_row.operator("reskate.open_mod_output_folder", text="Open Mod / Output Folder", icon='FILE_FOLDER')

        card.separator(factor=0.6)

        # Collapsible & Searchable Pack Contents Manager (Available in Simple & Advanced!)
        if mod_info.get("exists") and mod_info.get("count", 0) > 0:
            pm_box = card.box()
            pm_head = pm_box.row(align=True)
            show_pc = bool(getattr(prop, "show_pack_contents", False))
            pc_icon = 'TRIA_DOWN' if show_pc else 'TRIA_RIGHT'
            pm_head.prop(
                prop,
                "show_pack_contents",
                text=f"Pack Contents ({mod_info['count']} prop(s) in '{prop.target_mod_name}')",
                icon=pc_icon,
                emboss=False,
            )
            bak_cnt = int(mod_info.get("backups_count", mod_info.get("backup_count", 0)) or 0)
            if bak_cnt > 0:
                pm_head.operator(
                    "reskate.restore_pack_backup",
                    text=f"Restore Backup ({bak_cnt})",
                    icon='LOOP_BACK',
                )
            if show_pc:
                pm_box.prop(prop, "pack_contents_filter", text="", icon='VIEWZOOM')
                q_filt = (getattr(prop, "pack_contents_filter", "") or "").strip().lower()
                for p_det in mod_info.get("props_detail", []):
                    p_name = p_det.get("display_name") or p_det.get("name") or p_det.get("prop_key", "")
                    p_key = p_det.get("prop_key") or p_det.get("slug", "")
                    if q_filt and q_filt not in p_name.lower() and q_filt not in p_key.lower():
                        continue
                    p_row = pm_box.row(align=True)
                    p_row.label(text=f"{p_name}", icon='OBJECT_DATA')
                    sel_op = p_row.operator("reskate.pack_prop_action", text="Select & Frame", icon='RESTRICT_SELECT_OFF')
                    sel_op.action = 'SELECT'
                    sel_op.prop_key = p_key
                    sel_op.display_name = p_name
                    rem_op = p_row.operator("reskate.pack_prop_action", text="", icon='TRASH', emboss=False)
                    rem_op.action = 'REMOVE'
                    rem_op.prop_key = p_key
                    rem_op.display_name = p_name

        # Collapsible 10-line live compiler log + Copy full log button (Available whenever log exists!)
        if prop.compile_log_text:
            log_box = card.box()
            l_row = log_box.row(align=True)
            l_icon = 'TRIA_DOWN' if prop.show_compile_log else 'TRIA_RIGHT'
            l_row.prop(prop, "show_compile_log", text="Compiler Log (Last 10 Lines)", icon=l_icon, emboss=False)
            l_row.operator("reskate.copy_compile_log", text="Copy Full Log", icon='COPYDOWN')
            if prop.show_compile_log:
                lines = prop.compile_log_text.splitlines()[-10:]
                l_col = log_box.column(align=True)
                for ln in lines:
                    l_col.label(text=ln[:85])

        # Persistent Folder Paths Configuration & Advanced Export Options
        prefs = _props.get_addon_preferences(context)
        game_dir_path = compiler_bridge.get_game_dir(context)
        game_valid = _cached_path_exists(str(game_dir_path / "Skate.exe")) or _cached_path_exists(str(game_dir_path / "Win32"))
        if not game_valid:
            card.separator(factor=0.6)
            paths_box = card.box()
            paths_box.alert = True
            paths_box.label(text="⚠️ Skate. Game Folder not found — configure paths:", icon='ERROR')
            p_btn_row = paths_box.row(align=True)
            p_btn_row.alert = False
            p_btn_row.operator(
                "reskate.open_addon_preferences",
                text="⚙️ Set Paths in Add-on Preferences",
                icon='PREFERENCES',
            )
            p_btn_row.operator("reskate.reset_default_paths", text="Auto-Detect", icon='FILE_REFRESH')
            if prefs is not None:
                p_col = paths_box.column(align=True)
                p_col.alert = False
                p_col.prop(prefs, "game_dir", text="Game Folder")
                p_col.prop(prefs, "staging_dir", text="Staging Folder")
        elif ui_mode == 'ADVANCED':
            card.separator(factor=0.6)
            paths_box = card.box()
            p_head = paths_box.row(align=True)
            show_paths = bool(getattr(prop, "show_paths_config", False))
            p_icon = 'TRIA_DOWN' if show_paths else 'TRIA_RIGHT'
            p_head.prop(
                prop,
                "show_paths_config",
                text="Paths & Folders (Game & Staging)",
                icon=p_icon,
                emboss=False,
            )
            p_head.operator(
                "reskate.open_addon_preferences",
                text="⚙️ Preferences",
                icon='PREFERENCES',
            )
            if show_paths:
                if prefs is not None:
                    paths_box.prop(prefs, "game_dir", text="Game Folder")
                    paths_box.prop(prefs, "staging_dir", text="Staging Folder")
                    paths_box.prop(prefs, "studio_dir", text="Studio Folder")
                else:
                    paths_box.label(text=f"Game: {game_dir_path}", icon='FILE_FOLDER')
                    paths_box.label(text=f"Staging: {compiler_bridge.get_staging_dir(context)}", icon='FILE_FOLDER')
                p_btn_row = paths_box.row(align=True)
                p_btn_row.operator("reskate.reset_default_paths", text="Auto-Detect Default Paths", icon='FILE_REFRESH')
                p_btn_row.operator("reskate.clear_compiler_cache", text="Clear Build Cache", icon='TRASH')
        else:
            card.separator(factor=0.4)
            pref_row = card.row(align=True)
            pref_row.operator(
                "reskate.open_addon_preferences",
                text="⚙️ Paths & Add-on Preferences",
                icon='PREFERENCES',
            )

        if ui_mode == 'ADVANCED':
            card.separator(factor=0.6)
            rel_row = card.row(align=True)
            rel_row.scale_y = 1.25
            rel_row.operator("reskate.export_thunderstore_zip", text="Export Release .zip (Thunderstore)", icon='PACKAGE')

            adv_box = card.box()
            adv_box.prop(prop, "export_fbmod_only", text="Export .fbmod Archive Only (Do Not Deploy)")
            tool_row = adv_box.row(align=True)
            tool_row.operator("reskate.open_mod_folder", text="Open Mod Folder", icon='FILE_FOLDER')
            tool_row.operator("reskate.inspect_manifest", text="Inspect Manifest", icon='TEXT')


# ============================================================================
# Experimental Mode Cards (`sk8_ui_mode == 'EXPERIMENTAL'`)
# ============================================================================

class RESKATE_PT_exp1_material_catalog(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_exp1_material_catalog"
    bl_label = "🧪 Exp 1. Raw 279 Frostbite MaterialGrid Catalog"
    bl_order = 10

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'EXPERIMENTAL'

    def draw(self, context):
        layout = self.layout
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return
        box = layout.box()
        box.label(text="Unfiltered 279 Frostbite MaterialGrid Index Override:", icon='PHYSICS')
        box.prop(prop, "use_full_surface_catalog", text="Browse All 279 Frostbite MaterialGrid Declarations", icon='PRESET')
        filt_row = box.row(align=True)
        filt_row.prop(prop, "surface_category_filter", text="Group")
        if hasattr(prop, "surface_behavior_filter"):
            filt_row.prop(prop, "surface_behavior_filter", text="Behavior")
        box.prop(prop, "surface_search_filter", text="Search", icon='VIEWZOOM')
        box.prop(prop, "full_surface_catalog", text="Frostbite Surface")
        box.prop(prop, "surface_material_code", text="Raw Packed Code")


class RESKATE_PT_exp2_manual_grind(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_exp2_manual_grind"
    bl_label = "🧪 Exp 2. Manual Grind Angle & Edge Marking Overrides"
    bl_order = 11

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'EXPERIMENTAL'

    def draw(self, context):
        layout = self.layout
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        ang_box = layout.box()
        ang_box.label(text="Grind Qualifier Angle & Bevel Thresholds:", icon='MOD_BEVEL')
        ang_box.prop(prop, "crease_angle", text="Min Corner Angle (°)", slider=True)
        t_sub = ang_box.row(align=True)
        t_sub.prop(prop, "auto_min_edge_length", text="Min Edge Len (m)")
        t_sub.prop(prop, "auto_ignore_bottom_pct", text="Skip Ground Base %", slider=True)
        ang_box.prop(prop, "auto_open_above", text="Must Be Open Above")
        if getattr(prop, "auto_open_above", True):
            ang_box.prop(prop, "auto_clearance_height", text="Clearance Height (m)")

        m_box = layout.box()
        m_box.label(text="Manual Grind Edge Tagging (Edit Mode):", icon='EDITMODE_HLT')
        b_row1 = m_box.row(align=True)
        b_row1.scale_y = 1.25
        b_row1.enabled = obj is not None
        b_row1.operator("reskate.autodetect_grind_edges", text="Auto-Mark Sharp Edges", icon='AUTO')
        b_row1.operator("reskate.add_grind_path_from_selection", text="Mark Selected Edges", icon='ADD')

        b_row2 = m_box.row(align=True)
        b_row2.scale_y = 1.2
        b_row2.enabled = obj is not None
        b_row2.operator("reskate.remove_grind_from_selection", text="Unmark Selected", icon='REMOVE')
        b_row2.operator("reskate.block_grind_on_selection", text="Block Grind (No-Grind)", icon='CANCEL')
        b_row2.operator("reskate.clear_all_grind_edges", text="Clear All", icon='TRASH')

        if obj is not None and len(obj.reskate_grind_paths) > 0:
            m_box.template_list(
                "RESKATE_UL_grind_paths", "",
                obj, "reskate_grind_paths",
                obj, "reskate_grind_path_index",
                rows=2, maxrows=5,
            )


class RESKATE_PT_exp3_vfx_audio(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_exp3_vfx_audio"
    bl_label = "🧪 Exp 3. Experimental VFX & Audio Emitters"
    bl_order = 12

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'EXPERIMENTAL'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        exp = getattr(scene, "reskate_exp", None)
        metrics = get_active_mesh_metrics(context)
        obj = metrics["obj"] if metrics else None

        vfx_box = layout.box()
        vfx_box.label(text="Fuse VFX & Particle Emitters (R&D):", icon='PARTICLES')
        if exp is not None:
            vfx_box.prop(exp, "vfx_preset", text="VFX Blueprint")
            vfx_box.prop(exp, "vfx_scale", text="Effect Scale")
            vfx_box.prop(exp, "vfx_auto_start", text="Auto-Start Continuous")
            v_row = vfx_box.row(align=True)
            v_row.scale_y = 1.3
            v_row.enabled = obj is not None
            v_row.operator("reskate_exp.add_vfx_emitter", text="+ Attach VFX Emitter to Prop", icon='ADD')

            aud_box = layout.box()
            aud_box.label(text="Interactive Music / Boombox Emitter (R&D):", icon='SOUND')
            aud_box.prop(exp, "music_dsp_profile", text="DSP Speaker Profile")
            aud_box.prop(exp, "music_radius", text="Audible Radius (m)")
            a_row = aud_box.row(align=True)
            a_row.scale_y = 1.3
            a_row.enabled = obj is not None
            a_row.operator("reskate_exp.toggle_music_emitter", text="Toggle Boombox Audio Gizmo", icon='SPEAKER')
        else:
            vfx_box.label(text="Experimental VFX/Audio module ready.", icon='INFO')


class RESKATE_PT_exp4_compiler_hw(RESKATE_PT_base, Panel):
    bl_idname = "RESKATE_PT_exp4_compiler_hw"
    bl_label = "🧪 Exp 4. Hardware & Experimental Compiler Toggles"
    bl_order = 13

    @classmethod
    def poll(cls, context):
        return _get_ui_mode(context) == 'EXPERIMENTAL'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        exp = getattr(scene, "reskate_exp", None)
        if not prop:
            return
        hw_box = layout.box()
        hw_box.label(text="Hardware Acceleration & R&D Compiler Flags:", icon='PREFERENCES')
        if hasattr(prop, "compiler_backend"):
            hw_box.prop(prop, "compiler_backend", text="Compiler Backend")
        hw_box.prop(prop, "max_col_tris", text="Havok 1:1 Max Tris", slider=True)
        if exp is not None:
            if hasattr(exp, "compile_wip_vfx_in_game"):
                hw_box.prop(exp, "compile_wip_vfx_in_game", text="Compile WIP VFX Entities In-Game")
            if hasattr(exp, "compile_experimental_grind_tubes"):
                hw_box.prop(exp, "compile_experimental_grind_tubes", text="Compile Physical Grind Tube Prisms")

        dep_box = layout.box()
        dep_box.label(text="Deprecated / Legacy Controls:", icon='MOD_DISPLACE')
        dep_box.prop(
            prop,
            "anti_lip_enabled",
            text="Smooth Ramp Bottom (Legacy Anti-Lip -15mm)",
            icon='MOD_DISPLACE',
        )
        if getattr(prop, "anti_lip_enabled", False):
            dep_box.prop(prop, "anti_lip_sink_m", text="Lip Sink Depth (m)", slider=True)
        dep_box.prop(prop, "dielectric_roughness", text="Fallback Flat Roughness", slider=True)
        dep_box.prop(prop, "dielectric_metallic", text="Fallback Flat Metallic", slider=True)

        d_box = layout.box()
        d_row = d_box.row(align=True)
        d_row.scale_y = 1.25
        d_row.operator("reskate.inspect_manifest", text="Inspect prop_manifest.json", icon='TEXT')
        d_row.operator("reskate.open_mod_folder", text="Open Staging / Mods Folder", icon='FILE_FOLDER')


# ============================================================================
# Registration & Unregistration
# ============================================================================

CLASSES = (
    RESKATE_UL_grind_paths,
    RESKATE_UL_material_slots,
    RESKATE_UL_linked_lights,
    RESKATE_UL_pack_queue,
    RESKATE_PT_main_panel,
    RESKATE_PT_card1_setup,
    RESKATE_PT_card2_collision,
    RESKATE_PT_card4_textures,
    RESKATE_PT_sk8_material_fallback,
    RESKATE_PT_card5_lights,
    RESKATE_PT_card5_lights_thumb,
    RESKATE_PT_card6_export,
    RESKATE_PT_exp1_material_catalog,
    RESKATE_PT_exp2_manual_grind,
    RESKATE_PT_exp3_vfx_audio,
    RESKATE_PT_exp4_compiler_hw,
)


def register():
    for cls in CLASSES:
        if not getattr(cls, "is_registered", False):
            bpy.utils.register_class(cls)


def unregister():
    global _SANCTUS_PREVIEW_COLL
    if _SANCTUS_PREVIEW_COLL is not None:
        try:
            from bpy.utils import previews as _bpy_previews
            _bpy_previews.remove(_SANCTUS_PREVIEW_COLL)
        except Exception:
            pass
        _SANCTUS_PREVIEW_COLL = None
    for cls in reversed(CLASSES):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
