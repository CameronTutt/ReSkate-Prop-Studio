"""
ReSkate Prop Studio — gpu_draw.py
3D Viewport GPU shader overlay for vertex grind paths, marked grind edges
(color-coordinated by Ledge / Round Rail / Coping + optional 3D Box & Cylinder shape overlays),
and collision geometry (including Simple Hulls and custom collision objects).
Compatible with Blender 5.3 Beta ('UNIFORM_COLOR') with strict headless safety.
"""

from __future__ import annotations

import json
import math
import time
import bmesh
import blf
import bpy
import gpu
import gpu_extras.batch
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector

from . import compiler_bridge

FACE_SURFACE_ATTR_NAME = getattr(compiler_bridge, "FACE_SURFACE_ATTR_NAME", "reskate_face_surface")

_draw_handler_handle = None
_draw_handler_2d_handle = None
_uniform_shader = None
_grind_batch_cache: dict[str, tuple] = {}
_grind_counts_cache: dict[str, tuple[tuple, dict[str, int]]] = {}
_col_batch_cache: dict[str, tuple] = {}
_face_surface_batch_cache: dict[str, tuple] = {}
_alignment_batch_cache: dict[str, tuple] = {}
_surface_highlight_batch_cache: dict[str, tuple] = {}
_prop_hud_stats_cache: dict[str, tuple] = {}
_selected_props_hud_cache: tuple[float, tuple, list[dict]] | None = None
_scene_badge_objs_cache: tuple[float, tuple, list[str]] | None = None
_hover_badge_lines_cache: dict[tuple, tuple[float, dict]] = {}

# Active 2.5-second surface highlight state (§6.1)
_SURFACE_HIGHLIGHT_STATE: dict[str, object] = {
    "active": False,
    "obj_name": "",
    "slot_index": -1,
    "start_time": 0.0,
    "duration": 2.5,
    "static_fallback": False,
    "timer_registered": False,
}


def is_scale_reference_object(obj: bpy.types.Object | None) -> bool:
    """Returns True if `obj` is an authoring scale reference mannequin/board/ground plane or parented to one."""
    if not obj:
        return False
    cur = obj
    visited = set()
    while cur is not None and getattr(cur, "name", None) not in visited:
        visited.add(cur.name)
        name = getattr(cur, "name", "")
        if (
            "Scale_Ref" in name
            or name in (
                "ReSkate_Skater_Scale_Ref",
                "ReSkate_Skateboard_Scale_Ref",
                "ReSkate_RoundRail_Scale_Ref",
                "ReSkate_Ground_Plane_Ref",
            )
            or name.startswith("ReSkate_Skater")
            or name.startswith("ReSkate_Skateboard")
            or name.startswith("ReSkate_Board")
            or name.startswith("ReSkate_RoundRail")
            or name.startswith("ReSkate_Ground_Plane")
            or name.startswith("_ReSkate_")
        ):
            return True
        if getattr(cur, "get", None) and (cur.get("reskate_is_scale_reference") or cur.get("reskate_is_ref_helper")):
            return True
        cur = getattr(cur, "parent", None)
    return False


COLOR_LEDGE_ORANGE   = (0.25, 1.00, 0.30, 1.0)   # Electric Lime-Green for Hard Edge / Ledge (contrasts with Blender Edit Mode orange)
COLOR_RAIL_CYAN      = (0.00, 0.88, 1.00, 1.0)   # Neon cyan for Round Rail
COLOR_COPING_MAGENTA = (0.95, 0.20, 0.85, 1.0)   # Neon magenta for Pool / Ramp Coping

COLOR_LEDGE_SHAPE    = (1.00, 0.58, 0.12, 0.65)  # 3D Box overlay for Ledge
COLOR_RAIL_SHAPE     = (0.05, 0.90, 1.00, 0.65)  # 3D Cylinder overlay for Round Rail
COLOR_COPING_SHAPE   = (0.98, 0.30, 0.88, 0.65)  # 3D Cylinder overlay for Coping

COLOR_COL_LIME       = (0.10, 0.90, 0.30, 0.65)  # Lime green for normal skateable collision
COLOR_COL_AMBER      = (1.00, 0.68, 0.10, 0.75)  # Amber for Prevent Wall-Snap ([do not align])
COLOR_COL_RED        = (0.95, 0.22, 0.20, 0.75)  # Red for No-Grind ([no grind])
COLOR_COL_CYAN       = (0.15, 0.85, 1.00, 0.75)  # Cyan for Bouncy ([bouncy])
COLOR_COL_ANTI_LIP   = (0.10, 1.00, 0.72, 0.95)  # Bright spring-green/cyan for sunken Anti-Lip ramp bottom edges

COLOR_PROXY_BOX       = (0.20, 0.90, 1.00, 0.85)  # Translucent cyan-white for Quick Drop Placement Proxy bounds
COLOR_PROXY_FOOTPRINT = (0.20, 1.00, 0.75, 0.95)  # Highlighted ground footprint rectangle at Z = min_z


def invalidate_gpu_caches(clear_ui: bool = True):
    """Clears cached GPU batches when grind paths, origins, collision meshes, or face surfaces change."""
    global _selected_props_hud_cache, _scene_badge_objs_cache
    _grind_batch_cache.clear()
    _grind_counts_cache.clear()
    _col_batch_cache.clear()
    _selected_props_hud_cache = None
    _scene_badge_objs_cache = None
    _hover_badge_lines_cache.clear()
    if "_face_surface_batch_cache" in globals():
        globals()["_face_surface_batch_cache"].clear()
    if "_alignment_batch_cache" in globals():
        globals()["_alignment_batch_cache"].clear()
    if "_surface_highlight_batch_cache" in globals():
        globals()["_surface_highlight_batch_cache"].clear()
    if "_prop_hud_stats_cache" in globals():
        globals()["_prop_hud_stats_cache"].clear()
    if clear_ui:
        try:
            compiler_bridge.invalidate_ui_caches(clear_gpu=False)
        except Exception:
            pass


def are_viewport_overlays_enabled_in_prefs(context=None) -> bool:
    """Returns True if the source-level 'Enable All Viewport Overlays' master switch is enabled in Add-on Preferences."""
    try:
        from . import properties
        if hasattr(properties, "is_viewport_overlays_master_enabled"):
            return bool(properties.is_viewport_overlays_master_enabled(context))
        prefs = properties.get_addon_preferences(context)
        if prefs is not None and hasattr(prefs, "enable_viewport_overlays_master"):
            return bool(prefs.enable_viewport_overlays_master)
    except Exception:
        pass
    return True


def are_pack_badges_enabled_in_prefs(context=None) -> bool:
    """Returns True if both 'Enable All Viewport Overlays' and 'Enable Pack Membership Badges & 2D HUD' are enabled in Add-on Preferences."""
    if not are_viewport_overlays_enabled_in_prefs(context):
        return False
    try:
        from . import properties
        if hasattr(properties, "is_pack_badges_master_enabled"):
            return bool(properties.is_pack_badges_master_enabled(context))
        prefs = properties.get_addon_preferences(context)
        if prefs is not None and hasattr(prefs, "enable_pack_badges_master"):
            return bool(prefs.enable_pack_badges_master)
    except Exception:
        pass
    return True


def _get_shader():
    """Lazily fetches and caches the Blender 5.3 builtin UNIFORM_COLOR shader."""
    global _uniform_shader
    if bpy.app.background:
        return None
    if _uniform_shader is None:
        try:
            _uniform_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        except Exception:
            _uniform_shader = None
    return _uniform_shader


def _local_bbox_sig(obj) -> tuple:
    """Returns a compact signature of `obj.data.vertices[0].co` and `obj.bound_box` in local space to detect `Set Origin` vertex shifts."""
    try:
        v0 = obj.data.vertices[0].co if (obj and obj.type == 'MESH' and len(obj.data.vertices) > 0) else (0.0, 0.0, 0.0)
        bb = obj.bound_box
        return (
            round(float(v0[0]), 4), round(float(v0[1]), 4), round(float(v0[2]), 4),
            round(float(bb[0][0]), 4), round(float(bb[0][1]), 4), round(float(bb[0][2]), 4),
            round(float(bb[6][0]), 4), round(float(bb[6][1]), 4), round(float(bb[6][2]), 4),
        )
    except Exception:
        return ()


def _face_attr_signature(mesh_obj) -> int:
    """Returns a fast signature (< 0.03ms) of per-face painted surface overrides to detect attribute edits."""
    if not mesh_obj or getattr(mesh_obj, "type", None) != 'MESH' or not mesh_obj.data:
        return 0

    # 1. Fast path: check persisted face surfaces JSON first (< 0.002ms)
    raw_json = mesh_obj.get("reskate_face_surfaces_json", "")
    if raw_json:
        try:
            return hash(str(raw_json))
        except Exception:
            pass

    me = mesh_obj.data
    # 2. Edit Mode BMesh attribute check
    if getattr(mesh_obj, "mode", "OBJECT") == 'EDIT' and getattr(me, "is_editmode", False):
        try:
            bm = bmesh.from_edit_mesh(me)
            layer = bm.faces.layers.int.get("reskate_face_surface") or bm.faces.layers.int.get(FACE_SURFACE_ATTR_NAME)
            if layer is not None:
                n_faces = len(bm.faces)
                if n_faces <= 2048:
                    return sum(int(f[layer]) for f in bm.faces if f[layer] > 0)
                else:
                    bm.faces.ensure_lookup_table()
                    sample_indices = (0, n_faces // 4, n_faces // 2, (3 * n_faces) // 4, n_faces - 1)
                    return sum(int(bm.faces[i][layer]) for i in sample_indices) * 31 + n_faces
        except Exception:
            pass

    # 3. Object Mode attribute check
    attr = (me.attributes.get("reskate_face_surface") or me.attributes.get(FACE_SURFACE_ATTR_NAME)) if hasattr(me, "attributes") else None
    if attr is not None and getattr(attr, "domain", "") == 'FACE' and len(attr.data) > 0:
        n_attr = len(attr.data)
        if n_attr <= 2048:
            try:
                buf = np.empty(n_attr, dtype=np.int32)
                attr.data.foreach_get("value", buf)
                return int(buf.sum()) * 31 + int(np.count_nonzero(buf))
            except Exception:
                return n_attr
        else:
            try:
                s0 = int(attr.data[0].value)
                sm = int(attr.data[n_attr // 2].value)
                s1 = int(attr.data[n_attr - 1].value)
                return (s0 * 31 + sm * 17 + s1) * 31 + n_attr
            except Exception:
                return n_attr

    return 0


def _build_orthonormal_basis(p0: Vector, p1: Vector) -> tuple[Vector, Vector, Vector] | None:
    """Builds `(tangent, u, v)` orthonormal vectors for a 3D line segment `p0 -> p1`."""
    delta = p1 - p0
    length = delta.length
    if length < 1e-5:
        return None
    t = delta / length
    up = Vector((0.0, 0.0, 1.0)) if abs(t.z) < 0.92 else Vector((1.0, 0.0, 0.0))
    u = t.cross(up)
    if u.length < 1e-5:
        u = Vector((0.0, 1.0, 0.0))
    else:
        u.normalize()
    v = t.cross(u)
    v.normalize()
    return t, u, v


def _append_box_cage_lines(out_lines: list, p0: tuple, p1: tuple, radius: float = 0.025):
    """Appends a 3D rectangular Box wireframe cage around segment `p0 -> p1` (for Hard Edge / Ledge)."""
    v0 = Vector(p0)
    v1 = Vector(p1)
    basis = _build_orthonormal_basis(v0, v1)
    if basis is None:
        return
    _, u, v = basis
    r = max(0.008, float(radius))
    offsets = [
        (u + v) * r,
        (-u + v) * r,
        (-u - v) * r,
        (u - v) * r,
    ]
    c0 = [tuple(v0 + off) for off in offsets]
    c1 = [tuple(v1 + off) for off in offsets]
    for i in range(4):
        nxt = (i + 1) % 4
        # Start cap rectangle
        out_lines.append(c0[i])
        out_lines.append(c0[nxt])
        # End cap rectangle
        out_lines.append(c1[i])
        out_lines.append(c1[nxt])
        # Longitudinal box edge
        out_lines.append(c0[i])
        out_lines.append(c1[i])


def _append_cylinder_cage_lines(out_lines: list, p0: tuple, p1: tuple, radius: float = 0.03, sides: int = 8):
    """Appends a 3D cylindrical wireframe cage around segment `p0 -> p1` (for Round Rail / Coping)."""
    v0 = Vector(p0)
    v1 = Vector(p1)
    basis = _build_orthonormal_basis(v0, v1)
    if basis is None:
        return
    _, u, v = basis
    r = max(0.008, float(radius))
    n_sides = max(4, int(sides))
    c0 = []
    c1 = []
    for k in range(n_sides):
        ang = (2.0 * math.pi * k) / n_sides
        off = (u * math.cos(ang) + v * math.sin(ang)) * r
        c0.append(tuple(v0 + off))
        c1.append(tuple(v1 + off))
    for k in range(n_sides):
        nxt = (k + 1) % n_sides
        # Start ring
        out_lines.append(c0[k])
        out_lines.append(c0[nxt])
        # End ring
        out_lines.append(c1[k])
        out_lines.append(c1[nxt])
        # 4 longitudinal rails
        if k % 2 == 0:
            out_lines.append(c0[k])
            out_lines.append(c1[k])


def _collect_grind_edge_coords_local(obj, build_shapes: bool = False) -> tuple[list, list, list, list, list, list]:
    """
    Returns `(ledge_coords, rail_coords, coping_coords, nogrind_coords, counts_tuple, empty_list)`
    in LOCAL object space by combining:
      1. Live Geometry Angle & Bevel Analysis (shows which edges on the mesh qualify as grindable in Skate!)
      2. Optional manual `reskate_grind` tagged edges / `obj.reskate_grind_paths` (from ReSkate Lab)
    NOTE: 3D faux box/cylinder cages are never generated so only exact edge lines are highlighted.
    """
    if not obj or obj.type != 'MESH' or is_scale_reference_object(obj):
        return [], [], [], [], (0, 0, 0, 0), []

    mesh = obj.data
    ledge_coords: list = []
    rail_coords: list = []
    coping_coords: list = []
    nogrind_coords: list = []

    scene = getattr(bpy.context, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None
    is_round_rail = bool(getattr(prop, "round_rail", False)) if prop else False
    crease_rad = float(getattr(prop, "crease_angle", math.radians(42.0))) if prop else math.radians(42.0)
    crease_deg = max(15.0, min(88.0, math.degrees(crease_rad)))
    min_edge_len = float(getattr(prop, "auto_min_edge_length", 0.15)) if prop else 0.15
    ignore_bot_pct = float(getattr(prop, "auto_ignore_bottom_pct", 8.0)) if prop else 8.0

    def _add_seg(p0: tuple, p1: tuple, gtype: str):
        if gtype == 'NO_GRIND':
            nogrind_coords.append(p0)
            nogrind_coords.append(p1)
        elif gtype == 'ROUND_RAIL':
            rail_coords.append(p0)
            rail_coords.append(p1)
        elif gtype == 'COPING':
            coping_coords.append(p0)
            coping_coords.append(p1)
        else:
            ledge_coords.append(p0)
            ledge_coords.append(p1)

    def _val_to_gtype(val: int) -> str:
        if val == 2:
            return 'ROUND_RAIL'
        if val == 3:
            return 'COPING'
        return 'ROUND_RAIL' if is_round_rail else 'LEDGE'

    # 1. Explicit manual tags or paths (if present from ReSkate Lab)
    has_manual = False
    if obj.mode == 'EDIT':
        try:
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            layer = bm.edges.layers.int.get("reskate_grind")
            if layer:
                for e in bm.edges:
                    val = int(e[layer])
                    if val > 0:
                        has_manual = True
                        _add_seg(tuple(e.verts[0].co), tuple(e.verts[1].co), _val_to_gtype(val))
        except Exception:
            pass
    else:
        attr = mesh.attributes.get("reskate_grind")
        if attr and len(attr.data) > 0 and len(mesh.edges) > 0:
            for e in mesh.edges:
                if e.index < len(attr.data):
                    val = int(attr.data[e.index].value)
                    if val > 0:
                        has_manual = True
                        p0 = tuple(mesh.vertices[e.vertices[0]].co)
                        p1 = tuple(mesh.vertices[e.vertices[1]].co)
                        _add_seg(p0, p1, _val_to_gtype(val))

    if hasattr(obj, "reskate_grind_paths"):
        n_verts = len(mesh.vertices)
        for gp in obj.reskate_grind_paths:
            if not getattr(gp, "enabled", True):
                continue
            raw_csv = getattr(gp, "vertex_indices_csv", "")
            v_ids = [int(t) for t in raw_csv.split(",") if t.strip().isdigit()]
            gtype = str(getattr(gp, "grind_type", 'LEDGE'))
            if getattr(gp, "surface_override", "") == 'No_Grind':
                gtype = 'NO_GRIND'
            for i in range(len(v_ids) - 1):
                i0, i1 = v_ids[i], v_ids[i + 1]
                if 0 <= i0 < n_verts and 0 <= i1 < n_verts:
                    has_manual = True
                    _add_seg(tuple(mesh.vertices[i0].co), tuple(mesh.vertices[i1].co), gtype)

    # 2. Automatic Live Geometry Dihedral Angle & Bevel Lip Analysis (when <= 15,000 edges)
    # Uses the exact same welded world-space detector (`compiler_bridge.detect_world_space_grind_edges`)
    # as `Mark Edges` (`reskate.auto_detect_grind_edges`) so the 3D visualizer and `Mark Edges` match 100% (§6.3).
    if not has_manual and 0 < len(mesh.edges) <= 15000 and len(mesh.polygons) > 0:
        try:
            n_slots = max(1, len(obj.material_slots))
            slot_buckets = [_slot_behavior_bucket(obj, i) for i in range(n_slots)]
            slot_rail_flags = [_slot_is_rail_surface(obj, i) for i in range(n_slots)]

            bm_edit = bmesh.from_edit_mesh(mesh) if obj.mode == 'EDIT' else None
            if bm_edit is not None:
                bm_edit.verts.ensure_lookup_table()
                bm_edit.edges.ensure_lookup_table()
                bm_edit.faces.ensure_lookup_table()

            marked_orig = compiler_bridge.detect_world_space_grind_edges(
                obj,
                bm=bm_edit,
                threshold_rad=crease_rad,
                min_edge_len=min_edge_len,
                ignore_bottom_pct=ignore_bot_pct / 100.0,
            )

            if bm_edit is not None:
                n_bm_edges = len(bm_edit.edges)
                for e_idx, tag_val in sorted(marked_orig.items()):
                    if not (0 <= e_idx < n_bm_edges):
                        continue
                    e = bm_edit.edges[e_idx]
                    v0_l = tuple(e.verts[0].co)
                    v1_l = tuple(e.verts[1].co)
                    has_ng = False
                    has_rail_mat = False
                    for lf in e.link_faces:
                        mi = min(max(0, lf.material_index), n_slots - 1)
                        if slot_buckets[mi] == "NO_GRIND":
                            has_ng = True
                        if slot_rail_flags[mi]:
                            has_rail_mat = True
                    if has_ng:
                        _add_seg(v0_l, v1_l, 'NO_GRIND')
                    elif is_round_rail or has_rail_mat or tag_val == 2:
                        _add_seg(v0_l, v1_l, 'ROUND_RAIL')
                    else:
                        _add_seg(v0_l, v1_l, _val_to_gtype(tag_val))
            else:
                verts = mesh.vertices
                edges = mesh.edges
                polys = mesh.polygons
                n_me_edges = len(edges)
                edge_mat_indices: dict[int, list[int]] = {}
                if any(b == "NO_GRIND" for b in slot_buckets) or any(slot_rail_flags):
                    vert_pair_to_eidx: dict[tuple[int, int], int] = {}
                    for ed in edges:
                        a, b = ed.vertices[0], ed.vertices[1]
                        vert_pair_to_eidx[(a, b) if a <= b else (b, a)] = ed.index
                    for poly in polys:
                        pv = poly.vertices
                        n_pv = len(pv)
                        mi = min(max(0, poly.material_index), n_slots - 1)
                        for k in range(n_pv):
                            a, b = pv[k], pv[(k + 1) % n_pv]
                            e_i = vert_pair_to_eidx.get((a, b) if a <= b else (b, a))
                            if e_i is not None:
                                edge_mat_indices.setdefault(e_i, []).append(mi)

                for e_idx, tag_val in sorted(marked_orig.items()):
                    if not (0 <= e_idx < n_me_edges):
                        continue
                    ed = edges[e_idx]
                    v0_l = tuple(verts[ed.vertices[0]].co)
                    v1_l = tuple(verts[ed.vertices[1]].co)
                    m_list = edge_mat_indices.get(e_idx, ())
                    has_ng = any(slot_buckets[mi] == "NO_GRIND" for mi in m_list)
                    has_rail_mat = any(slot_rail_flags[mi] for mi in m_list)
                    if has_ng:
                        _add_seg(v0_l, v1_l, 'NO_GRIND')
                    elif is_round_rail or has_rail_mat or tag_val == 2:
                        _add_seg(v0_l, v1_l, 'ROUND_RAIL')
                    else:
                        _add_seg(v0_l, v1_l, _val_to_gtype(tag_val))
        except Exception:
            pass

    mw_chain = obj.matrix_world
    if has_manual:
        ledge_coords, n_l, _ = _chain_and_count_grind_segments(ledge_coords, mw_chain, is_rail=False, min_chain_len=min_edge_len)
        rail_coords, n_r, _ = _chain_and_count_grind_segments(rail_coords, mw_chain, is_rail=True, min_chain_len=min_edge_len)
        coping_coords, n_c, _ = _chain_and_count_grind_segments(coping_coords, mw_chain, is_rail=False, min_chain_len=min_edge_len)
        nogrind_coords, n_ng, _ = _chain_and_count_grind_segments(nogrind_coords, mw_chain, is_rail=False, min_chain_len=min_edge_len)
    else:
        # Preserve 100% of segments returned by `detect_world_space_grind_edges` so visualizer and `Mark Edges` match 1-to-1
        _, n_l, _ = _chain_and_count_grind_segments(ledge_coords, mw_chain, is_rail=False, min_chain_len=0.0)
        _, n_r, _ = _chain_and_count_grind_segments(rail_coords, mw_chain, is_rail=False, min_chain_len=0.0)
        _, n_c, _ = _chain_and_count_grind_segments(coping_coords, mw_chain, is_rail=False, min_chain_len=0.0)
        _, n_ng, _ = _chain_and_count_grind_segments(nogrind_coords, mw_chain, is_rail=False, min_chain_len=0.0)

    counts = {
        "ledge": n_l,
        "ledges": n_l,
        "rail": n_r,
        "rails": n_r,
        "coping": n_c,
        "no_grind": n_ng,
        "total": n_l + n_r + n_c,
        "ledge_segments": len(ledge_coords) // 2,
        "rail_segments": len(rail_coords) // 2,
        "coping_segments": len(coping_coords) // 2,
    }
    return ledge_coords, rail_coords, coping_coords, nogrind_coords, counts, []


def _chain_and_count_grind_segments(
    raw_coords: list,
    mw,
    is_rail: bool = False,
    max_turn_deg: float = 70.0,
    min_chain_len: float = 0.0,
) -> tuple[list, int, list[dict]]:
    """
    Groups raw edge segment pairs `[p0, p1, p2, p3, ...]` into continuous grind chains:
      - Connected segments sharing a vertex whose turn angle is `< max_turn_deg` (default 70°)
        are merged into a single continuous grind chain (so a 30-segment curved rail = 1 Rail,
        while a 4-sided rectangular ledge box with 90° corners = 4 Ledges).
      - Filters out tiny isolated chains whose total continuous length is `< min_chain_len`
        (so short individual segments on a subdivided curved rail are preserved!).
      - When `is_rail=True`, parallel longitudinal crown strips on the same tubular cylinder
        (within 9.5cm along the tube) are collapsed to the single highest-Z top-crown centerline
        chain so a cylinder rail displays 1 clean line and counts as 1 Rail.
    Returns `(filtered_coords, chain_count, chain_infos)`.
    """
    n_segs = len(raw_coords) // 2
    if n_segs == 0:
        return [], 0, []

    cos_thresh = math.cos(math.radians(float(max_turn_deg)))
    segs = []
    vert_to_segs: dict[tuple[int, int, int], list[tuple[int, int]]] = {}

    for s_idx in range(n_segs):
        p0_l = raw_coords[2 * s_idx]
        p1_l = raw_coords[2 * s_idx + 1]
        v0_w = mw @ Vector(p0_l)
        v1_w = mw @ Vector(p1_l)
        delta = v1_w - v0_w
        length = delta.length
        if length < 1e-5:
            continue
        t_dir = delta / length
        k0 = (int(round(v0_w.x * 500.0)), int(round(v0_w.y * 500.0)), int(round(v0_w.z * 500.0)))
        k1 = (int(round(v1_w.x * 500.0)), int(round(v1_w.y * 500.0)), int(round(v1_w.z * 500.0)))
        real_idx = len(segs)
        segs.append({
            "p0_l": p0_l,
            "p1_l": p1_l,
            "v0_w": v0_w,
            "v1_w": v1_w,
            "k0": k0,
            "k1": k1,
            "dir": t_dir,
            "len": length,
        })
        vert_to_segs.setdefault(k0, []).append((real_idx, 0))
        vert_to_segs.setdefault(k1, []).append((real_idx, 1))

    if not segs:
        return [], 0, []

    visited = [False] * len(segs)
    chains: list[list[int]] = []

    def _outward_tangent(seg_d: dict, end_idx: int) -> Vector:
        return seg_d["dir"] if end_idx == 0 else (-seg_d["dir"])

    for start_i in range(len(segs)):
        if visited[start_i]:
            continue
        visited[start_i] = True
        chain = [start_i]

        for start_end in (1, 0):
            cur_s = start_i
            cur_end = start_end
            while True:
                cur_seg = segs[cur_s]
                v_key = cur_seg["k1"] if cur_end == 1 else cur_seg["k0"]
                incoming_dir = cur_seg["dir"] if cur_end == 1 else (-cur_seg["dir"])
                best_nbr = -1
                best_nbr_other_end = 0
                best_dot = cos_thresh
                for nbr_s, nbr_end in vert_to_segs.get(v_key, []):
                    if visited[nbr_s]:
                        continue
                    outgoing_dir = _outward_tangent(segs[nbr_s], nbr_end)
                    d_val = float(incoming_dir.dot(outgoing_dir))
                    if d_val >= best_dot:
                        best_dot = d_val
                        best_nbr = nbr_s
                        best_nbr_other_end = 1 - nbr_end
                if best_nbr < 0:
                    break
                visited[best_nbr] = True
                if start_end == 1:
                    chain.append(best_nbr)
                else:
                    chain.insert(0, best_nbr)
                cur_s = best_nbr
                cur_end = best_nbr_other_end

        chains.append(chain)

    chain_infos: list[dict] = []
    for ch in chains:
        pts_w = []
        total_len = 0.0
        dir_acc = Vector((0.0, 0.0, 0.0))
        ref_dir = segs[ch[0]]["dir"]
        for s_i in ch:
            sd = segs[s_i]
            pts_w.append(0.5 * (sd["v0_w"] + sd["v1_w"]))
            total_len += sd["len"]
            d = sd["dir"]
            if d.dot(ref_dir) < 0.0:
                d = -d
            dir_acc += d * sd["len"]
        if min_chain_len > 0.0 and total_len + 1e-4 < min_chain_len:
            continue
        mean_dir = dir_acc.normalized() if dir_acc.length_squared > 1e-10 else ref_dir
        mean_pt = sum(pts_w, Vector((0.0, 0.0, 0.0))) / float(len(pts_w))
        max_z = max(max(segs[s_i]["v0_w"].z, segs[s_i]["v1_w"].z) for s_i in ch)
        chain_infos.append({
            "seg_indices": ch,
            "pts_w": pts_w,
            "mean_pt": mean_pt,
            "mean_dir": mean_dir,
            "total_len": total_len,
            "max_z": max_z,
        })

    if is_rail and len(chain_infos) > 1:
        chain_infos.sort(key=lambda c: (round(c["max_z"], 3), round(c["total_len"], 3)), reverse=True)
        kept_chains: list[dict] = []
        for cand in chain_infos:
            is_parallel_duplicate = False
            step_c = max(1, len(cand["pts_w"]) // 8)
            sample_c = cand["pts_w"][::step_c]
            for kept in kept_chains:
                step_k = max(1, len(kept["pts_w"]) // 12)
                sample_k = kept["pts_w"][::step_k]
                # Check mean nearest-point distance from cand to kept (collapses parallel crown strips on straight or curved rails)
                nearest_dists = [
                    min((p_c - p_k).length for p_k in sample_k)
                    for p_c in sample_c
                ]
                if nearest_dists and (sum(nearest_dists) / len(nearest_dists)) <= 0.095:
                    is_parallel_duplicate = True
                    break
            if not is_parallel_duplicate:
                kept_chains.append(cand)
        chain_infos = kept_chains

    out_coords: list = []
    for ci in chain_infos:
        for s_i in ci["seg_indices"]:
            out_coords.append(segs[s_i]["p0_l"])
            out_coords.append(segs[s_i]["p1_l"])

    return out_coords, len(chain_infos), chain_infos


def _collect_col_mesh_coords_local(obj) -> list:
    """Extracts wireframe edge coords in local space using fast numpy foreach_get."""
    if not obj or obj.type != 'MESH':
        return []

    mesh = obj.data
    n_edges = len(mesh.edges)
    n_verts = len(mesh.vertices)
    if n_edges == 0 or n_verts == 0:
        return []

    try:
        edge_verts = np.empty(n_edges * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edge_verts)
        vert_cos = np.empty(n_verts * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", vert_cos)
        vert_cos = vert_cos.reshape((n_verts, 3))
        return vert_cos[edge_verts].tolist()
    except Exception:
        coords = []
        for e in mesh.edges:
            coords.append(tuple(mesh.vertices[e.vertices[0]].co))
            coords.append(tuple(mesh.vertices[e.vertices[1]].co))
        return coords


def _get_grind_batches(obj, shader):
    """Returns cached `(ledge_b, rail_b, coping_b, nogrind_b, counts_tuple, None)` in local coordinates."""
    if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None) or is_scale_reference_object(obj):
        return None, None, None, None, (0, 0, 0, 0, 0, 0), None
    mesh = obj.data
    scene = getattr(bpy.context, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None
    is_round_rail = bool(getattr(prop, "round_rail", False)) if prop else False
    crease_deg = round(math.degrees(float(getattr(prop, "crease_angle", math.radians(42.0)))), 1) if prop else 42.0
    min_len = round(float(getattr(prop, "auto_min_edge_length", 0.15)), 3) if prop else 0.15
    ignore_bot = round(float(getattr(prop, "auto_ignore_bottom_pct", 8.0)), 1) if prop else 8.0
    n_slots = max(1, len(obj.material_slots))
    slot_buckets = tuple(_slot_behavior_bucket(obj, i) for i in range(n_slots))

    attr = mesh.attributes.get("reskate_grind")
    attr_sig = 0
    if attr and len(attr.data) > 0 and obj.mode != 'EDIT':
        try:
            buf = np.empty(len(attr.data), dtype=np.int32)
            attr.data.foreach_get("value", buf)
            attr_sig = int(buf.sum()) * 31 + int(np.count_nonzero(buf))
        except Exception:
            attr_sig = len(attr.data)

    paths_sig = tuple(
        (
            getattr(gp, "enabled", True),
            getattr(gp, "grind_type", 'LEDGE'),
            getattr(gp, "surface_override", ""),
            getattr(gp, "vertex_indices_csv", ""),
        )
        for gp in getattr(obj, "reskate_grind_paths", [])
    )
    sig = (
        obj.name,
        len(mesh.vertices),
        len(mesh.edges),
        attr_sig,
        is_round_rail,
        round(float(getattr(prop, "grind_tube_radius", 0.032) or 0.032), 4) if prop else 0.032,
        bool(getattr(prop, "show_round_rail_diameter_guide", True)) if prop else True,
        crease_deg,
        min_len,
        ignore_bot,
        slot_buckets,
        paths_sig,
        _local_bbox_sig(obj),
        obj.mode,
    )
    if obj.mode != 'EDIT':
        cached = _grind_batch_cache.get(obj.name)
        if cached is not None and cached[0] == sig:
            return cached[1:]

    l_c, r_c, c_c, ng_c, counts, _ = _collect_grind_edge_coords_local(obj, build_shapes=False)
    ledge_b = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': l_c}) if l_c else None
    rail_b = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': r_c}) if r_c else None
    coping_b = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': c_c}) if c_c else None
    nogrind_b = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': ng_c}) if ng_c else None

    rail_cyl_b = None
    if r_c and (not prop or getattr(prop, "show_round_rail_diameter_guide", True)):
        rail_rad = float(getattr(prop, "grind_tube_radius", 0.032) or 0.032) if prop else 0.032
        cyl_lines: list = []
        # r_c is along the top crown of the rail; offset each cylinder ring center downward by -Z * rail_rad
        # so the cylinder guide wraps the actual pipe body underneath the top crown!
        mw_i = obj.matrix_world
        try:
            inv_rot = mw_i.to_3x3().inverted()
            down_local = (inv_rot @ Vector((0.0, 0.0, -rail_rad)))
        except Exception:
            down_local = Vector((0.0, 0.0, -rail_rad))
        n_r_segs = len(r_c) // 2
        step_r = max(1, n_r_segs // 24)
        for s_i in range(0, n_r_segs, step_r):
            p0_c = Vector(r_c[2 * s_i]) + down_local
            p1_c = Vector(r_c[2 * s_i + 1]) + down_local
            _append_cylinder_cage_lines(cyl_lines, tuple(p0_c), tuple(p1_c), radius=rail_rad, sides=12)
        if cyl_lines:
            rail_cyl_b = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': cyl_lines})

    if obj.mode != 'EDIT':
        if len(_grind_batch_cache) > 16:
            _grind_batch_cache.clear()
        _grind_batch_cache[obj.name] = (sig, ledge_b, rail_b, coping_b, nogrind_b, counts, rail_cyl_b)
    return ledge_b, rail_b, coping_b, nogrind_b, counts, rail_cyl_b


def get_grind_edge_counts(obj_or_ctx, maybe_obj=None) -> dict[str, int]:
    """Returns live detected grindable edge counts for `obj` (used by 🟨 3. Grind UI and 2D badge)."""
    obj = maybe_obj if maybe_obj is not None else obj_or_ctx
    empty = {"ledge": 0, "ledges": 0, "rail": 0, "rails": 0, "coping": 0, "no_grind": 0, "total": 0}
    if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None) or is_scale_reference_object(obj):
        return dict(empty)
    cached = _grind_batch_cache.get(obj.name)
    if cached is not None and len(cached) >= 6 and isinstance(cached[5], dict):
        return dict(cached[5])

    me = obj.data
    scene = getattr(bpy.context, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None
    c_sig = (
        obj.name,
        len(me.vertices),
        len(me.edges),
        len(obj.material_slots),
        bool(getattr(prop, "round_rail", False)) if prop else False,
        round(float(getattr(prop, "crease_angle", 0.733)), 3) if prop else 0.733,
        round(float(getattr(prop, "auto_min_edge_length", 0.15)), 3) if prop else 0.15,
        len(getattr(obj, "reskate_grind_paths", [])),
        getattr(obj, "mode", "OBJECT"),
    )
    if getattr(obj, "mode", "OBJECT") != 'EDIT':
        c_cached = _grind_counts_cache.get(obj.name)
        if c_cached is not None and c_cached[0] == c_sig:
            return dict(c_cached[1])

    try:
        _, _, _, _, counts, _ = _collect_grind_edge_coords_local(obj, build_shapes=False)
        if isinstance(counts, dict):
            if getattr(obj, "mode", "OBJECT") != 'EDIT':
                if len(_grind_counts_cache) > 64:
                    _grind_counts_cache.clear()
                _grind_counts_cache[obj.name] = (c_sig, dict(counts))
            return dict(counts)
    except Exception:
        pass
    return dict(empty)


def _slot_is_rail_surface(active_obj, slot_idx: int) -> bool:
    """Returns True if a material slot is assigned a Metal Rail surface (`packed == 2336` or 'rail' in label)."""
    try:
        mat = None
        if active_obj and 0 <= slot_idx < len(active_obj.material_slots):
            mat = active_obj.material_slots[slot_idx].material
        info = compiler_bridge.get_synced_material_surface_info(active_obj, slot_idx, mat)
        if int(info.get("packed_code", 0) or 0) == 2336:
            return True
        if "rail" in str(info.get("label", "")).lower():
            return True
    except Exception:
        pass
    return False


def _slot_behavior_bucket(active_obj, slot_idx: int) -> str:
    """Returns one of 'NORMAL', 'NO_ALIGN', 'NO_GRIND', 'BOUNCY' for a material slot."""
    try:
        mat = None
        if active_obj and 0 <= slot_idx < len(active_obj.material_slots):
            mat = active_obj.material_slots[slot_idx].material
        if mat is not None:
            for holder in (getattr(mat, "reskate_mat", None), getattr(mat, "sk8_material", None)):
                if holder is not None:
                    if (
                        bool(getattr(holder, "is_decal_slot", False))
                        or str(getattr(holder, "surface_role_preset", "") or "") == "DECAL_OVERLAY"
                        or bool(getattr(holder, "invisible", False))
                        or str(getattr(holder, "domain", "") or "").lower() == "decal"
                    ):
                        return "NO_GRIND"
        info = compiler_bridge.get_synced_material_surface_info(active_obj, slot_idx, mat)
        badges = info.get("badges", [])
        if "[bouncy]" in badges:
            return "BOUNCY"
        if "[no grind]" in badges:
            return "NO_GRIND"
        if "[do not align]" in badges:
            return "NO_ALIGN"
    except Exception:
        pass
    return "NORMAL"


def _get_col_batches(
    col_obj,
    active_obj,
    shader,
    anti_lip_enabled: bool = False,
    anti_lip_sink_m: float = 0.015,
) -> dict[str, object]:
    """Returns cached collision wireframe batches keyed by behavior bucket ('NORMAL', 'NO_ALIGN', 'NO_GRIND', 'BOUNCY', 'ANTI_LIP')."""
    mesh = col_obj.data
    n_slots = max(1, len(active_obj.material_slots)) if active_obj else 1
    slot_buckets = tuple(_slot_behavior_bucket(active_obj, i) for i in range(n_slots))
    sink_val = round(float(anti_lip_sink_m or 0.015), 4) if anti_lip_enabled else 0.0
    sig = (
        col_obj.name,
        len(mesh.vertices),
        len(mesh.edges),
        _local_bbox_sig(col_obj),
        col_obj.mode,
        slot_buckets,
        bool(anti_lip_enabled),
        sink_val,
    )
    if col_obj.mode != 'EDIT':
        cached = _col_batch_cache.get(col_obj.name)
        if cached is not None and cached[0] == sig:
            return cached[1]

    buckets: dict[str, list] = {"NORMAL": [], "NO_ALIGN": [], "NO_GRIND": [], "BOUNCY": [], "ANTI_LIP": []}
    verts = mesh.vertices
    n_verts = len(verts)

    # Precompute vertex positions (with optional Anti-Lip sink on bottom perimeter vertices z <= min_z + 0.015)
    sunken_vert_mask = None
    adjusted_local_cos = None
    if anti_lip_enabled and sink_val > 0.0 and n_verts > 0:
        try:
            mw = col_obj.matrix_world
            rot_scale = mw.to_3x3()
            try:
                inv_rs = rot_scale.inverted()
            except Exception:
                inv_rs = rot_scale
            local_sink_vec = inv_rs @ Vector((0.0, 0.0, -sink_val))
            world_zs = [(mw @ v.co).z for v in verts]
            min_wz = min(world_zs)
            lip_thresh_z = min_wz + 0.015
            sunken_vert_mask = [wz <= lip_thresh_z for wz in world_zs]
            adjusted_local_cos = [
                (
                    float(v.co.x + local_sink_vec.x),
                    float(v.co.y + local_sink_vec.y),
                    float(v.co.z + local_sink_vec.z),
                )
                if sunken_vert_mask[i]
                else (float(v.co.x), float(v.co.y), float(v.co.z))
                for i, v in enumerate(verts)
            ]
        except Exception:
            sunken_vert_mask = None
            adjusted_local_cos = None

    if adjusted_local_cos is not None and sunken_vert_mask is not None:
        if len(set(slot_buckets)) == 1 or len(mesh.polygons) == 0:
            single_b = slot_buckets[0] if slot_buckets else "NORMAL"
            for e in mesh.edges:
                i0, i1 = e.vertices[0], e.vertices[1]
                b_target = "ANTI_LIP" if (sunken_vert_mask[i0] or sunken_vert_mask[i1]) else single_b
                buckets[b_target].append(adjusted_local_cos[i0])
                buckets[b_target].append(adjusted_local_cos[i1])
        else:
            for poly in mesh.polygons:
                b_key = slot_buckets[min(max(0, poly.material_index), len(slot_buckets) - 1)]
                pv = poly.vertices
                n_pv = len(pv)
                for k in range(n_pv):
                    i0 = pv[k]
                    i1 = pv[(k + 1) % n_pv]
                    b_target = "ANTI_LIP" if (sunken_vert_mask[i0] or sunken_vert_mask[i1]) else b_key
                    buckets[b_target].append(adjusted_local_cos[i0])
                    buckets[b_target].append(adjusted_local_cos[i1])
    elif len(set(slot_buckets)) == 1 or len(mesh.polygons) == 0:
        single_b = slot_buckets[0] if slot_buckets else "NORMAL"
        buckets[single_b] = _collect_col_mesh_coords_local(col_obj)
    else:
        for poly in mesh.polygons:
            b_key = slot_buckets[min(max(0, poly.material_index), len(slot_buckets) - 1)]
            target_list = buckets[b_key]
            pv = poly.vertices
            n_pv = len(pv)
            for k in range(n_pv):
                v0 = verts[pv[k]].co
                v1 = verts[pv[(k + 1) % n_pv]].co
                target_list.append((float(v0.x), float(v0.y), float(v0.z)))
                target_list.append((float(v1.x), float(v1.y), float(v1.z)))

    batches: dict[str, object] = {}
    for k, coords in buckets.items():
        batches[k] = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': coords}) if coords else None

    if col_obj.mode != 'EDIT':
        if len(_col_batch_cache) > 16:
            _col_batch_cache.clear()
        _col_batch_cache[col_obj.name] = (sig, batches)
    return batches


def _resolve_overlay_target_mesh(context) -> bpy.types.Object | None:
    """Resolves the active object (or _COL / child helper empty / child light) to its visual prop MESH object."""
    ctx = context if context is not None else bpy.context
    active_obj = getattr(ctx, "active_object", None)
    if is_scale_reference_object(active_obj):
        return None

    try:
        resolved = compiler_bridge.get_target_mesh_object(ctx)
        if resolved and resolved.type == 'MESH':
            if is_scale_reference_object(resolved):
                return None
            return resolved
    except Exception:
        pass

    if not active_obj:
        return None
    if active_obj.type in ('LIGHT', 'EMPTY') and active_obj.parent and active_obj.parent.type == 'MESH':
        active_obj = active_obj.parent
        if is_scale_reference_object(active_obj):
            return None
    elif active_obj.type == 'MESH' and (
        active_obj.name.endswith("_COL")
        or getattr(getattr(active_obj, "reskate_col_prop", None), "is_col_proxy", False)
    ):
        if active_obj.parent and active_obj.parent.type == 'MESH':
            active_obj = active_obj.parent
        else:
            base_name = active_obj.name[:-4] if active_obj.name.endswith("_COL") else ""
            if base_name and base_name in bpy.data.objects and bpy.data.objects[base_name].type == 'MESH':
                active_obj = bpy.data.objects[base_name]
        if is_scale_reference_object(active_obj):
            return None
    elif active_obj.type == 'EMPTY':
        for tag in ("_VFX_", "_MusicBoombox_", "_DecalProj_"):
            if tag in active_obj.name:
                base = active_obj.name.split(tag)[0]
                if base in bpy.data.objects and bpy.data.objects[base].type == 'MESH':
                    active_obj = bpy.data.objects[base]
                    break
        if is_scale_reference_object(active_obj):
            return None

    if active_obj.type != 'MESH' or is_scale_reference_object(active_obj):
        return None
    return active_obj


def _is_known_helper_of_prop(obj: bpy.types.Object | None, prop_obj: bpy.types.Object, ctx=None) -> bool:
    """Returns True if `obj` is an identifiable child helper (_COL, _VFX_, _MusicBoombox_, _DecalProj_, Light) of `prop_obj`."""
    if obj is None or prop_obj is None:
        return False

    # 1. Direct or indirect parent hierarchy
    cur = obj.parent
    visited = set()
    while cur is not None and cur.name not in visited:
        visited.add(cur.name)
        if cur == prop_obj:
            name = getattr(obj, "name", "")
            if (
                name.endswith("_COL")
                or "_COL." in name
                or "_VFX_" in name
                or "_MusicBoombox_" in name
                or name.endswith("_MusicBoombox_Gizmo")
                or "_DecalProj_" in name
                or obj.get("reskate_is_exp_helper")
                or obj.get("reskate_is_vfx_helper")
                or obj.get("reskate_is_music_helper")
                or obj.get("reskate_is_decal_proj")
                or getattr(getattr(obj, "reskate_col_prop", None), "is_col_proxy", False)
                or getattr(obj, "type", None) in ('LIGHT', 'EMPTY')
            ):
                return True
        cur = cur.parent

    # 2. Suffix / naming convention match
    p_name = getattr(prop_obj, "name", "")
    o_name = getattr(obj, "name", "")
    if p_name and o_name.startswith(p_name):
        rem = o_name[len(p_name):]
        if rem.startswith(("_COL", "_VFX_", "_MusicBoombox_", "_DecalProj_")):
            return True

    # 3. Explicit stored collision owner tag
    if str(obj.get("reskate_col_owner", "") or "").strip() == p_name:
        return True

    # 4. Resolved custom collision object reference
    try:
        col_obj = compiler_bridge.resolve_custom_collision_object(ctx, prop_obj)
        if col_obj is not None and obj == col_obj:
            return True
    except Exception:
        pass

    return False


def is_prop_or_helper_selected(prop_obj: bpy.types.Object | None, context=None) -> bool:
    """
    Returns True if `prop_obj` (or one of its child helpers: _COL, _VFX_, _MusicBoombox_,
    _DecalProj_, or child lights) is currently selected in the viewport or in Edit Mode.
    If nothing is selected and the prop is not in Edit Mode, returns False.
    """
    if prop_obj is None or getattr(prop_obj, "type", None) != 'MESH':
        return False
    if is_scale_reference_object(prop_obj):
        return False

    # A. Prop itself is in Edit Mode
    if getattr(prop_obj, "mode", None) == 'EDIT':
        return True

    # B. Prop itself is selected in Viewport / Outliner
    try:
        if prop_obj.select_get():
            return True
    except Exception:
        pass

    ctx = context if context is not None else bpy.context
    if ctx is None:
        return False

    # C. Active object is a selected/editing child helper
    raw_active = getattr(ctx, "active_object", None)
    if raw_active is not None and raw_active != prop_obj:
        if not is_scale_reference_object(raw_active):
            try:
                is_active_sel = raw_active.select_get()
            except Exception:
                is_active_sel = False
            is_active_edit = (getattr(raw_active, "mode", None) == 'EDIT')
            if (is_active_sel or is_active_edit) and _is_known_helper_of_prop(raw_active, prop_obj, ctx):
                return True

    # D. Any object in selected_objects is prop or child helper
    selected = getattr(ctx, "selected_objects", ())
    if not selected:
        return False

    for sel_obj in selected:
        if is_scale_reference_object(sel_obj):
            continue
        if sel_obj == prop_obj:
            return True
        if _is_known_helper_of_prop(sel_obj, prop_obj, ctx):
            return True

    return False


def _collect_face_surface_draw_targets(ctx, active_obj) -> list[bpy.types.Object]:
    """Returns mesh objects (active prop and/or custom _COL) that have custom per-face surface overrides."""
    targets = []
    seen = set()
    raw_active = getattr(ctx, "active_object", None)
    for cand in (raw_active, active_obj, compiler_bridge.resolve_custom_collision_object(ctx, active_obj)):
        if cand and cand.type == 'MESH' and not is_scale_reference_object(cand) and cand.name not in seen:
            seen.add(cand.name)
            targets.append(cand)
    return targets


def _get_face_surface_batches(mesh_obj: bpy.types.Object, shader) -> list[tuple]:
    """
    Returns cached list of `(batch_tris, batch_boundary_lines, rgba_fill, rgba_line)` for `mesh_obj`.
    Batches are cached in `_face_surface_batch_cache` keyed by
    `(obj.name, mode, len(verts), len(polys), state_sig, face_attr_sig)`.
    Boundary lines only include topological perimeter edges (`count == 1` among faces sharing the same surface code).
    """
    if not mesh_obj or mesh_obj.type != 'MESH' or not mesh_obj.data or is_scale_reference_object(mesh_obj):
        return []

    me = mesh_obj.data
    mode = getattr(mesh_obj, "mode", "OBJECT")
    if mode == 'EDIT':
        try:
            bm = bmesh.from_edit_mesh(me)
            n_verts = len(bm.verts)
            n_polys = len(bm.faces)
        except Exception:
            n_verts = len(me.vertices)
            n_polys = len(me.polygons)
    else:
        n_verts = len(me.vertices)
        n_polys = len(me.polygons)

    if n_polys == 0:
        return []

    state_sig = _local_bbox_sig(mesh_obj)
    face_attr_sig = _face_attr_signature(mesh_obj)
    cache_key = (mesh_obj.name, mode, n_verts, n_polys, state_sig, face_attr_sig)

    cached = _face_surface_batch_cache.get(mesh_obj.name)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    # Quick exit if object has no face surface attributes/overrides
    if face_attr_sig == 0:
        if len(_face_surface_batch_cache) > 16:
            _face_surface_batch_cache.clear()
        _face_surface_batch_cache[mesh_obj.name] = (cache_key, [])
        return []

    codes = compiler_bridge.get_mesh_face_surface_array(mesh_obj)
    if not codes or not any(c > 0 for c in codes):
        if len(_face_surface_batch_cache) > 16:
            _face_surface_batch_cache.clear()
        _face_surface_batch_cache[mesh_obj.name] = (cache_key, [])
        return []

    legend = compiler_bridge.get_object_face_surface_legend(mesh_obj)
    tris_by_code: dict[int, list[tuple[float, float, float]]] = {}
    lines_by_code: dict[int, list[tuple[float, float, float]]] = {}

    edge_counts_by_code: dict[int, dict[tuple[int, int], int]] = {}
    edge_coords_by_code: dict[int, dict[tuple[int, int], tuple]] = {}

    if mode == 'EDIT' and getattr(me, "is_editmode", False):
        bm = bmesh.from_edit_mesh(me)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        attr_name = getattr(compiler_bridge, "FACE_SURFACE_ATTR_NAME", "reskate_face_surface")
        layer = bm.faces.layers.int.get("reskate_face_surface") or bm.faces.layers.int.get(attr_name)
        for f_idx, f in enumerate(bm.faces):
            c = int(f[layer]) if layer is not None else (int(codes[f_idx]) if f_idx < len(codes) else 0)
            if c <= 0 or len(f.verts) < 3:
                continue
            n_off = f.normal * 0.0025
            v0 = f.verts[0].co + n_off
            t_list = tris_by_code.setdefault(c, [])
            for k in range(1, len(f.verts) - 1):
                v1 = f.verts[k].co + n_off
                v2 = f.verts[k + 1].co + n_off
                t_list.append((float(v0.x), float(v0.y), float(v0.z)))
                t_list.append((float(v1.x), float(v1.y), float(v1.z)))
                t_list.append((float(v2.x), float(v2.y), float(v2.z)))

            c_counts = edge_counts_by_code.setdefault(c, {})
            c_coords = edge_coords_by_code.setdefault(c, {})
            for e in f.edges:
                i0, i1 = e.verts[0].index, e.verts[1].index
                ek = (min(i0, i1), max(i0, i1))
                c_counts[ek] = c_counts.get(ek, 0) + 1
                if ek not in c_coords:
                    p0 = e.verts[0].co + n_off
                    p1 = e.verts[1].co + n_off
                    c_coords[ek] = (
                        (float(p0.x), float(p0.y), float(p0.z)),
                        (float(p1.x), float(p1.y), float(p1.z)),
                    )
    else:
        verts = me.vertices
        polys = me.polygons
        for p_idx, poly in enumerate(polys):
            if p_idx >= len(codes):
                continue
            c = int(codes[p_idx])
            if c <= 0:
                continue
            p_verts = poly.vertices
            n_pv = len(p_verts)
            if n_pv < 3:
                continue
            n = poly.normal
            nx, ny, nz = float(n.x) * 0.0025, float(n.y) * 0.0025, float(n.z) * 0.0025
            t_list = tris_by_code.setdefault(c, [])
            co_first = verts[p_verts[0]].co
            v0_t = (float(co_first.x) + nx, float(co_first.y) + ny, float(co_first.z) + nz)
            for k in range(1, n_pv - 1):
                co1 = verts[p_verts[k]].co
                co2 = verts[p_verts[k + 1]].co
                t_list.append(v0_t)
                t_list.append((float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz))
                t_list.append((float(co2.x) + nx, float(co2.y) + ny, float(co2.z) + nz))

            c_counts = edge_counts_by_code.setdefault(c, {})
            c_coords = edge_coords_by_code.setdefault(c, {})
            for k in range(n_pv):
                i0 = p_verts[k]
                i1 = p_verts[(k + 1) % n_pv]
                ek = (min(i0, i1), max(i0, i1))
                c_counts[ek] = c_counts.get(ek, 0) + 1
                if ek not in c_coords:
                    co0 = verts[i0].co
                    co1 = verts[i1].co
                    c_coords[ek] = (
                        (float(co0.x) + nx, float(co0.y) + ny, float(co0.z) + nz),
                        (float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz),
                    )

    # Emit only outer boundary edges (count == 1 among faces with this surface code)
    for c, c_counts in edge_counts_by_code.items():
        c_coords = edge_coords_by_code.get(c, {})
        l_list = lines_by_code.setdefault(c, [])
        for ek, count in c_counts.items():
            if count == 1:
                p0, p1 = c_coords[ek]
                l_list.append(p0)
                l_list.append(p1)

    draw_items = []
    if shader is not None:
        for code_val, tri_pts in tris_by_code.items():
            meta = legend.get(str(code_val), {})
            surf_key = str(meta.get("surface_key", "") or "")
            rgba = compiler_bridge.get_face_surface_overlay_color(surf_key, code_val)
            batch_t = gpu_extras.batch.batch_for_shader(shader, 'TRIS', {'pos': tri_pts}) if tri_pts else None
            line_pts = lines_by_code.get(code_val)
            batch_l = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': line_pts}) if line_pts else None
            rgba_fill = (rgba[0], rgba[1], rgba[2], rgba[3])
            rgba_line = (min(1.0, rgba[0] + 0.15), min(1.0, rgba[1] + 0.15), min(1.0, rgba[2] + 0.15), 0.92)
            draw_items.append((batch_t, batch_l, rgba_fill, rgba_line))

    if len(_face_surface_batch_cache) > 16:
        _face_surface_batch_cache.clear()
    _face_surface_batch_cache[mesh_obj.name] = (cache_key, draw_items)
    return draw_items


def draw_face_surface_overlay(context=None, active_obj=None, prop=None, shader=None):
    """
    Draws translucent color-coded face fills + crisp boundary-only outlines for custom Per-Face Surface overrides.
    Works seamlessly in both Edit Mode (via BMesh) and Object Mode with zero-stutter GPU batch caching.
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if prop is None and scene is not None:
        prop = getattr(scene, "reskate_prop", None)
    if not prop or not getattr(prop, "draw_face_surface_overlay", True):
        return
    if active_obj is None:
        active_obj = _resolve_overlay_target_mesh(ctx)
    if not active_obj or active_obj.type != 'MESH':
        return
    if not is_prop_or_helper_selected(active_obj, ctx):
        return
    if shader is None:
        shader = _get_shader()
    if shader is None:
        return

    targets = _collect_face_surface_draw_targets(ctx, active_obj)
    for mesh_obj in targets:
        try:
            draw_items = _get_face_surface_batches(mesh_obj, shader)
            if not draw_items:
                continue

            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(mesh_obj.matrix_world)
                gpu.state.blend_set('ALPHA')
                gpu.state.depth_test_set('LESS_EQUAL')
                shader.bind()

                for batch_t, batch_l, rgba_fill, rgba_line in draw_items:
                    if batch_t:
                        shader.uniform_float('color', rgba_fill)
                        batch_t.draw(shader)
                    if batch_l:
                        gpu.state.line_width_set(2.2)
                        shader.uniform_float('color', rgba_line)
                        batch_l.draw(shader)

                gpu.state.line_width_set(1.0)
                gpu.state.depth_test_set('NONE')
                gpu.state.blend_set('NONE')
        except Exception:
            pass


def _get_alignment_batches(
    mesh_obj: bpy.types.Object,
    shader,
    prop_dna: bool = False,
    prop_ng: bool = False,
    slot_dna_set: set[int] | None = None,
    slot_ng_set: set[int] | None = None,
) -> tuple:
    """
    Returns cached `(b_dt, b_dl, b_nt, b_nl)` batches for `mesh_obj`.
    Batches are cached in `_alignment_batch_cache` keyed by
    `(obj.name, mode, len(verts), len(polys), state_sig, face_attr_sig)`.
    Boundary lines only include topological perimeter edges (`count == 1` for DNA / NG).
    """
    if not mesh_obj or mesh_obj.type != 'MESH' or not mesh_obj.data or is_scale_reference_object(mesh_obj):
        return (None, None, None, None)

    me = mesh_obj.data
    mode = getattr(mesh_obj, "mode", "OBJECT")
    if mode == 'EDIT':
        try:
            bm = bmesh.from_edit_mesh(me)
            n_verts = len(bm.verts)
            n_polys = len(bm.faces)
        except Exception:
            n_verts = len(me.vertices)
            n_polys = len(me.polygons)
    else:
        n_verts = len(me.vertices)
        n_polys = len(me.polygons)

    if n_polys == 0:
        return (None, None, None, None)

    slot_dna = tuple(sorted(slot_dna_set)) if slot_dna_set else ()
    slot_ng = tuple(sorted(slot_ng_set)) if slot_ng_set else ()
    state_sig = (prop_dna, prop_ng, slot_dna, slot_ng, _local_bbox_sig(mesh_obj))
    face_attr_sig = _face_attr_signature(mesh_obj)
    cache_key = (mesh_obj.name, mode, n_verts, n_polys, state_sig, face_attr_sig)

    cached = _alignment_batch_cache.get(mesh_obj.name)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    f_codes = compiler_bridge.get_mesh_face_surface_array(mesh_obj)
    dna_tris, dna_lines = [], []
    ng_tris, ng_lines = [], []

    dna_edge_counts: dict[tuple[int, int], int] = {}
    dna_edge_coords: dict[tuple[int, int], tuple] = {}
    ng_edge_counts: dict[tuple[int, int], int] = {}
    ng_edge_coords: dict[tuple[int, int], tuple] = {}

    s_dna_set = slot_dna_set or set()
    s_ng_set = slot_ng_set or set()

    if mode == 'EDIT' and getattr(me, "is_editmode", False):
        try:
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            bm.verts.ensure_lookup_table()
            attr_name = getattr(compiler_bridge, "FACE_SURFACE_ATTR_NAME", "reskate_face_surface")
            layer = bm.faces.layers.int.get("reskate_face_surface") or bm.faces.layers.int.get(attr_name)
            for f in bm.faces:
                if len(f.verts) < 3:
                    continue
                fi = f.index
                fc = int(f[layer]) if layer is not None else (int(f_codes[fi]) if fi < len(f_codes) else 0)
                is_f_dna = (fc > 0 and compiler_bridge.is_do_not_align_surface_code(fc)) or (
                    fc == 0 and (prop_dna or f.material_index in s_dna_set)
                )
                is_f_ng = (fc > 0 and compiler_bridge.is_no_grind_surface_code(fc)) or (
                    fc == 0 and (prop_ng or f.material_index in s_ng_set)
                )
                if not is_f_dna and not is_f_ng:
                    continue
                n = f.normal
                nx, ny, nz = float(n.x) * 0.003, float(n.y) * 0.003, float(n.z) * 0.003
                t_list = ng_tris if is_f_ng else dna_tris
                co0 = f.verts[0].co
                v0_t = (float(co0.x) + nx, float(co0.y) + ny, float(co0.z) + nz)
                for k in range(1, len(f.verts) - 1):
                    co1 = f.verts[k].co
                    co2 = f.verts[k + 1].co
                    t_list.append(v0_t)
                    t_list.append((float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz))
                    t_list.append((float(co2.x) + nx, float(co2.y) + ny, float(co2.z) + nz))

                if is_f_ng:
                    for e in f.edges:
                        i0, i1 = e.verts[0].index, e.verts[1].index
                        ek = (min(i0, i1), max(i0, i1))
                        ng_edge_counts[ek] = ng_edge_counts.get(ek, 0) + 1
                        if ek not in ng_edge_coords:
                            v0, v1 = e.verts[0].co, e.verts[1].co
                            ng_edge_coords[ek] = (
                                (float(v0.x) + nx, float(v0.y) + ny, float(v0.z) + nz),
                                (float(v1.x) + nx, float(v1.y) + ny, float(v1.z) + nz),
                            )
                elif is_f_dna:
                    for e in f.edges:
                        i0, i1 = e.verts[0].index, e.verts[1].index
                        ek = (min(i0, i1), max(i0, i1))
                        dna_edge_counts[ek] = dna_edge_counts.get(ek, 0) + 1
                        if ek not in dna_edge_coords:
                            v0, v1 = e.verts[0].co, e.verts[1].co
                            dna_edge_coords[ek] = (
                                (float(v0.x) + nx, float(v0.y) + ny, float(v0.z) + nz),
                                (float(v1.x) + nx, float(v1.y) + ny, float(v1.z) + nz),
                            )
        except Exception:
            pass
    else:
        verts = me.vertices
        polys = me.polygons
        for pi, poly in enumerate(polys):
            fc = int(f_codes[pi]) if pi < len(f_codes) else 0
            is_f_dna = (fc > 0 and compiler_bridge.is_do_not_align_surface_code(fc)) or (
                fc == 0 and (prop_dna or poly.material_index in s_dna_set)
            )
            is_f_ng = (fc > 0 and compiler_bridge.is_no_grind_surface_code(fc)) or (
                fc == 0 and (prop_ng or poly.material_index in s_ng_set)
            )
            if not is_f_dna and not is_f_ng:
                continue
            p_verts = poly.vertices
            n_pv = len(p_verts)
            if n_pv < 3:
                continue
            n = poly.normal
            nx, ny, nz = float(n.x) * 0.003, float(n.y) * 0.003, float(n.z) * 0.003
            t_list = ng_tris if is_f_ng else dna_tris
            co_first = verts[p_verts[0]].co
            v0_t = (float(co_first.x) + nx, float(co_first.y) + ny, float(co_first.z) + nz)
            for k in range(1, n_pv - 1):
                co1 = verts[p_verts[k]].co
                co2 = verts[p_verts[k + 1]].co
                t_list.append(v0_t)
                t_list.append((float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz))
                t_list.append((float(co2.x) + nx, float(co2.y) + ny, float(co2.z) + nz))

            if is_f_ng:
                for k in range(n_pv):
                    i0 = p_verts[k]
                    i1 = p_verts[(k + 1) % n_pv]
                    ek = (min(i0, i1), max(i0, i1))
                    ng_edge_counts[ek] = ng_edge_counts.get(ek, 0) + 1
                    if ek not in ng_edge_coords:
                        co0 = verts[i0].co
                        co1 = verts[i1].co
                        ng_edge_coords[ek] = (
                            (float(co0.x) + nx, float(co0.y) + ny, float(co0.z) + nz),
                            (float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz),
                        )
            elif is_f_dna:
                for k in range(n_pv):
                    i0 = p_verts[k]
                    i1 = p_verts[(k + 1) % n_pv]
                    ek = (min(i0, i1), max(i0, i1))
                    dna_edge_counts[ek] = dna_edge_counts.get(ek, 0) + 1
                    if ek not in dna_edge_coords:
                        co0 = verts[i0].co
                        co1 = verts[i1].co
                        dna_edge_coords[ek] = (
                            (float(co0.x) + nx, float(co0.y) + ny, float(co0.z) + nz),
                            (float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz),
                        )

    # Emit only outer boundary edges (count == 1)
    for ek, count in dna_edge_counts.items():
        if count == 1:
            p0, p1 = dna_edge_coords[ek]
            dna_lines.append(p0)
            dna_lines.append(p1)

    for ek, count in ng_edge_counts.items():
        if count == 1:
            p0, p1 = ng_edge_coords[ek]
            ng_lines.append(p0)
            ng_lines.append(p1)

    b_dt = gpu_extras.batch.batch_for_shader(shader, 'TRIS', {'pos': dna_tris}) if (shader and dna_tris) else None
    b_dl = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': dna_lines}) if (shader and dna_lines) else None
    b_nt = gpu_extras.batch.batch_for_shader(shader, 'TRIS', {'pos': ng_tris}) if (shader and ng_tris) else None
    b_nl = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': ng_lines}) if (shader and ng_lines) else None

    batches = (b_dt, b_dl, b_nt, b_nl)
    if len(_alignment_batch_cache) > 16:
        _alignment_batch_cache.clear()
    _alignment_batch_cache[mesh_obj.name] = (cache_key, batches)
    return batches


def draw_alignment_troubleshooting_overlay(context=None, active_obj=None, prop=None, shader=None):
    """
    Draws a high-visibility 3D GPU overlay whenever any surface, material slot, or painted face
    on the active prop has 'Do Not Align' (Prevent Wall-Snap / PropertySlot 111 / 527456) or 'No-Grind'
    applied:
      - Hazard Amber (1.00, 0.56, 0.08) for 'Do Not Align'
      - Crimson Red (0.98, 0.20, 0.24) for 'No-Grind'
    Uses boundary-only edge extraction and zero-stutter batch caching.
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if prop is None and scene is not None:
        prop = getattr(scene, "reskate_prop", None)
    if not prop or not getattr(prop, "show_alignment_overlay", True):
        return
    if active_obj is None:
        active_obj = _resolve_overlay_target_mesh(ctx)
    if not active_obj or active_obj.type != 'MESH':
        return
    if not is_prop_or_helper_selected(active_obj, ctx):
        return
    if shader is None:
        shader = _get_shader()
    if shader is None:
        return

    try:
        issues = compiler_bridge.inspect_prop_alignment_issues(ctx, active_obj)
        if not issues.get("has_do_not_align") and not issues.get("has_no_grind"):
            return

        slot_dna_set = {int(it["slot_index"]) for it in issues.get("slot_issues", []) if it.get("do_not_align")}
        slot_ng_set = {int(it["slot_index"]) for it in issues.get("slot_issues", []) if it.get("no_grind")}
        prop_dna = bool(issues.get("prop_level_do_not_align", False))
        prop_ng = bool(issues.get("prop_level_no_grind", False))

        raw_act = getattr(ctx, "active_object", None)
        col_obj = compiler_bridge.resolve_custom_collision_object(ctx, active_obj)
        target_meshes = [active_obj]
        if col_obj and col_obj != active_obj and (raw_act == col_obj or not col_obj.hide_get()):
            target_meshes.append(col_obj)

        for mesh_obj in target_meshes:
            b_dt, b_dl, b_nt, b_nl = _get_alignment_batches(
                mesh_obj,
                shader,
                prop_dna=prop_dna,
                prop_ng=prop_ng,
                slot_dna_set=slot_dna_set,
                slot_ng_set=slot_ng_set,
            )
            if not any((b_dt, b_dl, b_nt, b_nl)):
                continue

            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(mesh_obj.matrix_world)
                gpu.state.blend_set('ALPHA')
                gpu.state.depth_test_set('LESS_EQUAL')
                shader.bind()

                if b_dt:
                    shader.uniform_float('color', (1.00, 0.56, 0.08, 0.28))
                    b_dt.draw(shader)
                if b_dl:
                    gpu.state.line_width_set(2.0)
                    shader.uniform_float('color', (1.00, 0.68, 0.12, 0.88))
                    b_dl.draw(shader)

                if b_nt:
                    shader.uniform_float('color', (0.98, 0.18, 0.22, 0.30))
                    b_nt.draw(shader)
                if b_nl:
                    gpu.state.line_width_set(2.0)
                    shader.uniform_float('color', (1.00, 0.28, 0.32, 0.90))
                    b_nl.draw(shader)

                gpu.state.line_width_set(1.0)
                gpu.state.depth_test_set('NONE')
                gpu.state.blend_set('NONE')
    except Exception:
        pass


def _tag_view3d_redraw():
    """Tags all 3D Viewports for redraw when an animated overlay is active."""
    if bpy.app.background:
        return
    try:
        wm = getattr(bpy.context, "window_manager", None)
        if not wm:
            return
        for win in wm.windows:
            scr = getattr(win, "screen", None)
            if not scr:
                continue
            for area in scr.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _surface_highlight_timer_tick() -> float | None:
    """Self-stopping timer (~30 FPS) for the 2.5-second pulsing surface highlight (§6.1)."""
    if bpy.app.background or not _SURFACE_HIGHLIGHT_STATE.get("active"):
        _SURFACE_HIGHLIGHT_STATE["active"] = False
        _SURFACE_HIGHLIGHT_STATE["timer_registered"] = False
        sync_draw_handlers()
        _tag_view3d_redraw()
        return None
    elapsed = time.monotonic() - float(_SURFACE_HIGHLIGHT_STATE.get("start_time", 0.0) or 0.0)
    duration = float(_SURFACE_HIGHLIGHT_STATE.get("duration", 2.5) or 2.5)
    if elapsed >= duration:
        _SURFACE_HIGHLIGHT_STATE["active"] = False
        _SURFACE_HIGHLIGHT_STATE["timer_registered"] = False
        sync_draw_handlers()
        _tag_view3d_redraw()
        return None
    if not _SURFACE_HIGHLIGHT_STATE.get("static_fallback"):
        _tag_view3d_redraw()
    return 0.033


def trigger_surface_highlight(obj: bpy.types.Object | None, slot_index: int, duration: float = 2.5) -> bool:
    """
    Triggers a gentle 2.5-second pulsing translucent face highlight on `obj` for `slot_index` (§6.1).
    Respects `prefs.highlight_surfaces_enabled` and falls back to static tint if polygon count > 15,000.
    """
    if obj is None or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
        return False
    prefs = compiler_bridge.get_addon_preferences()
    if prefs is not None and not bool(getattr(prefs, "highlight_surfaces_enabled", True)):
        return False

    n_polys = len(obj.data.polygons)
    _SURFACE_HIGHLIGHT_STATE["active"] = True
    _SURFACE_HIGHLIGHT_STATE["obj_name"] = str(obj.name)
    _SURFACE_HIGHLIGHT_STATE["slot_index"] = int(slot_index)
    _SURFACE_HIGHLIGHT_STATE["start_time"] = time.monotonic()
    _SURFACE_HIGHLIGHT_STATE["duration"] = float(duration)
    _SURFACE_HIGHLIGHT_STATE["static_fallback"] = bool(n_polys > 15000)

    sync_draw_handlers()
    _tag_view3d_redraw()
    if not bpy.app.background and not _SURFACE_HIGHLIGHT_STATE.get("timer_registered"):
        try:
            _SURFACE_HIGHLIGHT_STATE["timer_registered"] = True
            bpy.app.timers.register(_surface_highlight_timer_tick, first_interval=0.033)
        except Exception:
            _SURFACE_HIGHLIGHT_STATE["timer_registered"] = False
    return True


def _get_surface_highlight_batches(mesh_obj: bpy.types.Object, slot_index: int, shader) -> tuple:
    """Returns cached `(batch_tris, batch_lines)` for faces using `slot_index` on `mesh_obj` (§6.1)."""
    if not mesh_obj or mesh_obj.type != 'MESH' or not mesh_obj.data or shader is None:
        return None, None
    me = mesh_obj.data
    n_polys = len(me.polygons)
    n_verts = len(me.vertices)
    if n_polys == 0 or n_verts == 0:
        return None, None

    mode = getattr(mesh_obj, "mode", "OBJECT")
    cache_key = (mesh_obj.name, int(slot_index), mode, n_verts, n_polys, _local_bbox_sig(mesh_obj))
    cached = _surface_highlight_batch_cache.get(f"{mesh_obj.name}:{slot_index}")
    if cached is not None and cached[0] == cache_key:
        return cached[1], cached[2]

    tri_pts: list[tuple[float, float, float]] = []
    line_pts: list[tuple[float, float, float]] = []
    edge_counts: dict[tuple[int, int], int] = {}
    edge_coords: dict[tuple[int, int], tuple] = {}
    target_slot = max(0, int(slot_index))

    if mode == 'EDIT' and getattr(me, "is_editmode", False):
        try:
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            for f in bm.faces:
                if int(f.material_index) != target_slot or len(f.verts) < 3:
                    continue
                n_off = f.normal * 0.003
                v0 = f.verts[0].co + n_off
                for k in range(1, len(f.verts) - 1):
                    v1 = f.verts[k].co + n_off
                    v2 = f.verts[k + 1].co + n_off
                    tri_pts.append((float(v0.x), float(v0.y), float(v0.z)))
                    tri_pts.append((float(v1.x), float(v1.y), float(v1.z)))
                    tri_pts.append((float(v2.x), float(v2.y), float(v2.z)))
                for e in f.edges:
                    i0, i1 = e.verts[0].index, e.verts[1].index
                    ek = (min(i0, i1), max(i0, i1))
                    edge_counts[ek] = edge_counts.get(ek, 0) + 1
                    if ek not in edge_coords:
                        p0 = e.verts[0].co + n_off
                        p1 = e.verts[1].co + n_off
                        edge_coords[ek] = (
                            (float(p0.x), float(p0.y), float(p0.z)),
                            (float(p1.x), float(p1.y), float(p1.z)),
                        )
        except Exception:
            pass
    else:
        verts = me.vertices
        polys = me.polygons
        for poly in polys:
            if int(poly.material_index) != target_slot:
                continue
            pv = poly.vertices
            n_pv = len(pv)
            if n_pv < 3:
                continue
            n = poly.normal
            nx, ny, nz = float(n.x) * 0.003, float(n.y) * 0.003, float(n.z) * 0.003
            co_first = verts[pv[0]].co
            v0_t = (float(co_first.x) + nx, float(co_first.y) + ny, float(co_first.z) + nz)
            for k in range(1, n_pv - 1):
                co1 = verts[pv[k]].co
                co2 = verts[pv[k + 1]].co
                tri_pts.append(v0_t)
                tri_pts.append((float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz))
                tri_pts.append((float(co2.x) + nx, float(co2.y) + ny, float(co2.z) + nz))

            for k in range(n_pv):
                i0 = pv[k]
                i1 = pv[(k + 1) % n_pv]
                ek = (min(i0, i1), max(i0, i1))
                edge_counts[ek] = edge_counts.get(ek, 0) + 1
                if ek not in edge_coords:
                    co0 = verts[i0].co
                    co1 = verts[i1].co
                    edge_coords[ek] = (
                        (float(co0.x) + nx, float(co0.y) + ny, float(co0.z) + nz),
                        (float(co1.x) + nx, float(co1.y) + ny, float(co1.z) + nz),
                    )

    for ek, cnt in edge_counts.items():
        if cnt == 1:
            p0, p1 = edge_coords[ek]
            line_pts.append(p0)
            line_pts.append(p1)

    batch_t = gpu_extras.batch.batch_for_shader(shader, 'TRIS', {'pos': tri_pts}) if tri_pts else None
    batch_l = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': line_pts}) if line_pts else None
    if len(_surface_highlight_batch_cache) > 16:
        _surface_highlight_batch_cache.clear()
    _surface_highlight_batch_cache[f"{mesh_obj.name}:{slot_index}"] = (cache_key, batch_t, batch_l)
    return batch_t, batch_l


def draw_surface_highlight_overlay(context=None, active_obj=None, shader=None):
    """Draws the 2.5-second pulsing (or static-fallback) material slot highlight overlay (§6.1)."""
    if not _SURFACE_HIGHLIGHT_STATE.get("active"):
        return
    prefs = compiler_bridge.get_addon_preferences()
    if prefs is not None and not bool(getattr(prefs, "highlight_surfaces_enabled", True)):
        _SURFACE_HIGHLIGHT_STATE["active"] = False
        return

    elapsed = time.monotonic() - float(_SURFACE_HIGHLIGHT_STATE.get("start_time", 0.0) or 0.0)
    duration = float(_SURFACE_HIGHLIGHT_STATE.get("duration", 2.5) or 2.5)
    if elapsed >= duration:
        _SURFACE_HIGHLIGHT_STATE["active"] = False
        return

    ctx = context if context is not None else bpy.context
    if active_obj is None:
        active_obj = _resolve_overlay_target_mesh(ctx)
    target_name = str(_SURFACE_HIGHLIGHT_STATE.get("obj_name", "") or "")
    mesh_obj = bpy.data.objects.get(target_name) if target_name else active_obj
    if not mesh_obj or mesh_obj.type != 'MESH':
        return
    if shader is None:
        shader = _get_shader()
    if shader is None:
        return

    t0 = time.perf_counter()
    slot_index = int(_SURFACE_HIGHLIGHT_STATE.get("slot_index", 0) or 0)
    batch_t, batch_l = _get_surface_highlight_batches(mesh_obj, slot_index, shader)
    if not batch_t and not batch_l:
        return

    # Fade envelope over 2.5s + gentle pulse (unless static fallback is active)
    fade = min(1.0, max(0.0, (duration - elapsed) / 0.45))
    if _SURFACE_HIGHLIGHT_STATE.get("static_fallback"):
        pulse = 0.75
    else:
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(elapsed * math.pi * 4.0))
    alpha_fill = 0.36 * pulse * fade
    alpha_line = 0.92 * fade

    try:
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(mesh_obj.matrix_world)
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('LESS_EQUAL')
            shader.bind()
            if batch_t:
                shader.uniform_float('color', (0.20, 0.88, 1.00, alpha_fill))
                batch_t.draw(shader)
            if batch_l:
                gpu.state.line_width_set(2.4)
                shader.uniform_float('color', (0.35, 0.95, 1.00, alpha_line))
                batch_l.draw(shader)
            gpu.state.line_width_set(1.0)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
    except Exception:
        pass

    dt_ms = (time.perf_counter() - t0) * 1000.0
    if dt_ms > 8.0:
        _SURFACE_HIGHLIGHT_STATE["static_fallback"] = True


def draw_col_overlay(context=None, active_obj=None, prop=None, shader=None):
    """Draws the Skate Collision wireframe overlay (with live sunken Anti-Lip bottom edge highlighting when enabled)."""
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if prop is None and scene is not None:
        prop = getattr(scene, "reskate_prop", None)
    if not prop or not (
        getattr(prop, "draw_col_overlay", False)
        or getattr(prop, "show_collision_overlay", False)
        or getattr(scene, "reskate_show_col_overlay", False)
    ):
        return
    # Suppress green collision wireframe in Edit Mode when in Simple Mode so mesh edges are easy to select
    raw_act = getattr(ctx, "active_object", None)
    if raw_act and getattr(raw_act, "mode", "") == 'EDIT' and str(getattr(scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE") == "SIMPLE":
        return
    if active_obj is None:
        active_obj = _resolve_overlay_target_mesh(ctx)
    if not active_obj or active_obj.type != 'MESH':
        return
    if not is_prop_or_helper_selected(active_obj, ctx):
        return
    if shader is None:
        shader = _get_shader()
    if shader is None:
        return

    # If `Check Collision` stored compiled Havok hull wireframe lines on `active_obj["reskate_col_check_preview_json"]`,
    # draw those exact Havok hull lines when in Simple Hulls mode or when explicitly previewed (§3 Page 2).
    raw_preview_json = str(active_obj.get("reskate_col_check_preview_json", "") or "")
    if raw_preview_json:
        try:
            prev_data = json.loads(raw_preview_json)
            col_lines_zup = prev_data.get("col_lines_zup") or []
            prev_mode = str(prev_data.get("collision_mode", "") or "")
            cur_mode = str(getattr(prop, "collision_mode", "SAME_AS_MESH") or "SAME_AS_MESH")
            if col_lines_zup and (not prev_mode or prev_mode == cur_mode):
                origin_w = active_obj.matrix_world.translation
                ox, oy, oz = float(origin_w.x), float(origin_w.y), float(origin_w.z)
                pts_w = []
                for pt in col_lines_zup:
                    if isinstance(pt, (list, tuple)):
                        if len(pt) == 6:
                            pts_w.append((float(pt[0]) + ox, float(pt[1]) + oy, float(pt[2]) + oz))
                            pts_w.append((float(pt[3]) + ox, float(pt[4]) + oy, float(pt[5]) + oz))
                        elif len(pt) >= 3:
                            pts_w.append((float(pt[0]) + ox, float(pt[1]) + oy, float(pt[2]) + oz))
                if len(pts_w) >= 2:
                    batch_chk = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': pts_w})
                    gpu.state.blend_set('ALPHA')
                    gpu.state.depth_test_set('LESS_EQUAL')
                    gpu.state.line_width_set(2.0)
                    shader.bind()
                    shader.uniform_float('color', COLOR_COL_LIME)
                    batch_chk.draw(shader)
                    gpu.state.line_width_set(1.0)
                    gpu.state.depth_test_set('NONE')
                    gpu.state.blend_set('NONE')
                    return
        except Exception:
            pass

    col_obj = compiler_bridge.resolve_custom_collision_object(ctx, active_obj)
    if not col_obj and getattr(prop, "collision_mode", 'SAME_AS_MESH') in (
        'SAME_AS_MESH', 'EXACT_MESH', 'EXACT_1_1', 'SIMPLE_HULLS', 'CONVEX_HULL'
    ):
        col_obj = active_obj

    if col_obj and col_obj.type == 'MESH':
        try:
            anti_lip_on = bool(getattr(prop, "anti_lip_enabled", False))
            anti_lip_sink = float(getattr(prop, "anti_lip_sink_m", 0.015) or 0.015)
            batches = _get_col_batches(
                col_obj,
                active_obj,
                shader,
                anti_lip_enabled=anti_lip_on,
                anti_lip_sink_m=anti_lip_sink,
            )
            if batches and any(batches.values()):
                with gpu.matrix.push_pop():
                    gpu.matrix.multiply_matrix(col_obj.matrix_world)
                    gpu.state.blend_set('ALPHA')
                    gpu.state.depth_test_set('LESS_EQUAL')
                    gpu.state.line_width_set(1.8)
                    shader.bind()
                    color_map = {
                        "NORMAL": COLOR_COL_LIME,
                        "NO_ALIGN": COLOR_COL_AMBER,
                        "NO_GRIND": COLOR_COL_RED,
                        "BOUNCY": COLOR_COL_CYAN,
                    }
                    for b_key, col_rgba in color_map.items():
                        b_obj = batches.get(b_key)
                        if b_obj:
                            shader.uniform_float('color', col_rgba)
                            b_obj.draw(shader)
                    anti_lip_batch = batches.get("ANTI_LIP")
                    if anti_lip_batch:
                        gpu.state.line_width_set(2.8)
                        shader.uniform_float('color', COLOR_COL_ANTI_LIP)
                        anti_lip_batch.draw(shader)
                    gpu.state.line_width_set(1.0)
                    gpu.state.depth_test_set('NONE')
                    gpu.state.blend_set('NONE')
        except Exception:
            pass


def draw_placement_proxy_overlay(context=None, active_obj=None, prop=None, shader=None):
    """
    Draws the Quick Drop Placement Proxy (`ebx32` / `res16`) in world space:
      - When `placement_proxy_mode == 'CUSTOM_OBJECT'` (with a valid `_PPHYS` mesh) or `'MATCH_SKATE_COL'`:
        draws the actual wireframe edges of the proxy mesh in cyan + ground footprint at `Z = min_z`.
      - When `placement_proxy_mode == 'AUTO_BOX'` (or Ghost/Decal mode): draws the full 3D AABB box + ground footprint.
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if prop is None and scene is not None:
        prop = getattr(scene, "reskate_prop", None)
    if not prop or not getattr(prop, "show_placement_proxy_overlay", False):
        return
    if active_obj is None:
        active_obj = _resolve_overlay_target_mesh(ctx)
    if not active_obj or active_obj.type != 'MESH':
        return
    if not is_prop_or_helper_selected(active_obj, ctx):
        return
    if shader is None:
        shader = _get_shader()
    if shader is None:
        return

    try:
        p_mode = str(getattr(prop, "placement_proxy_mode", "MATCH_COLLISION") or "MATCH_COLLISION")
        pphys_obj = None
        if p_mode != "BOUNDING_BOX" and hasattr(compiler_bridge, "resolve_custom_placement_proxy_object"):
            cand_pphys = compiler_bridge.resolve_custom_placement_proxy_object(ctx, active_obj)
            if cand_pphys and cand_pphys.type == 'MESH' and len(cand_pphys.data.vertices) > 0:
                pphys_obj = cand_pphys

        col_obj = pphys_obj or active_obj
        if pphys_obj is None:
            col_mode = getattr(prop, "collision_mode", "SAME_AS_MESH")
            if col_mode not in ("SAME_AS_MESH", "EXACT_MESH", "EXACT_1_1"):
                cand_col = compiler_bridge.resolve_custom_collision_object(ctx, active_obj)
                if cand_col and cand_col.type == 'MESH' and len(cand_col.data.vertices) > 0:
                    col_obj = cand_col

        mw = col_obj.matrix_world
        if len(col_obj.data.vertices) > 0:
            world_pts = [mw @ v.co for v in col_obj.data.vertices]
        elif col_obj.bound_box:
            world_pts = [mw @ Vector(pt) for pt in col_obj.bound_box]
        else:
            return

        xs = [p.x for p in world_pts]
        ys = [p.y for p in world_pts]
        zs = [p.z for p in world_pts]
        min_x, max_x = float(min(xs)), float(max(xs))
        min_y, max_y = float(min(ys)), float(max(ys))
        min_z, max_z = float(min(zs)), float(max(zs))

        b0 = (min_x, min_y, min_z)
        b1 = (max_x, min_y, min_z)
        b2 = (max_x, max_y, min_z)
        b3 = (min_x, max_y, min_z)

        t0 = (min_x, min_y, max_z)
        t1 = (max_x, min_y, max_z)
        t2 = (max_x, max_y, max_z)
        t3 = (min_x, max_y, max_z)

        box_lines = []
        used_contoured_edges = False
        if p_mode != "BOUNDING_BOX" and len(col_obj.data.edges) > 0:
            verts_me = col_obj.data.vertices
            for ed in col_obj.data.edges:
                p0 = mw @ verts_me[ed.vertices[0]].co
                p1 = mw @ verts_me[ed.vertices[1]].co
                box_lines.append((float(p0.x), float(p0.y), float(p0.z)))
                box_lines.append((float(p1.x), float(p1.y), float(p1.z)))
            used_contoured_edges = bool(box_lines)
        if not box_lines:
            box_lines = [
                # Top rectangle
                t0, t1, t1, t2, t2, t3, t3, t0,
                # Vertical pillars
                b0, t0, b1, t1, b2, t2, b3, t3,
            ]

        batch_box = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': box_lines})
        batch_foot = None
        if not used_contoured_edges:
            footprint_lines = [
                # Highlighted ground footprint rectangle + subtle corner crosshairs at Z = min_z (only in BOUNDING_BOX mode)
                b0, b1, b1, b2, b2, b3, b3, b0,
                b0, b2, b1, b3,
            ]
            batch_foot = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': footprint_lines})

        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        shader.bind()

        gpu.state.line_width_set(2.0)
        shader.uniform_float('color', COLOR_PROXY_BOX)
        batch_box.draw(shader)

        if batch_foot is not None:
            gpu.state.line_width_set(3.0)
            shader.uniform_float('color', COLOR_PROXY_FOOTPRINT)
            batch_foot.draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _draw_callback_3d(context=None):
    """SpaceView3D POST_VIEW callback rendering grindable edge lines, Skate Collision, Placement Proxy bounds, and light cones."""
    if bpy.app.background:
        return

    ctx = context if context is not None else bpy.context
    if not are_viewport_overlays_enabled_in_prefs(ctx):
        return
    scene = getattr(ctx, "scene", None)
    if not scene:
        return

    prop = getattr(scene, "reskate_prop", None)
    if not prop:
        return

    active_obj = _resolve_overlay_target_mesh(ctx)
    if not active_obj:
        return

    # STRICT SELECTED-ONLY GATING:
    # If the prop or its child helper is not selected or in Edit Mode, draw zero 3D overlays
    if not is_prop_or_helper_selected(active_obj, ctx):
        return

    shader = _get_shader()
    if shader is None:
        return

    # 1. Pure GPU Grindable Edge Visualizer (Orange = Ledge, Cyan = Round Rail, Magenta = Coping, Red = No-Grind)
    # In Simple Mode + Edit Mode, suppress the grind overlay so green/colored lines never interfere with edge selection
    ui_mode_now = str(getattr(scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE")
    raw_act_obj = getattr(ctx, "active_object", None)
    in_edit_mode = bool(raw_act_obj and getattr(raw_act_obj, "mode", "") == 'EDIT')
    if getattr(prop, "draw_grind_overlay", False) and not (in_edit_mode and ui_mode_now == "SIMPLE"):
        try:
            ledge_b, rail_b, coping_b, nogrind_b, _counts, rail_cyl_b = _get_grind_batches(active_obj, shader)
            if any((ledge_b, rail_b, coping_b, nogrind_b, rail_cyl_b)):
                with gpu.matrix.push_pop():
                    gpu.matrix.multiply_matrix(active_obj.matrix_world)
                    gpu.state.blend_set('ALPHA')
                    gpu.state.depth_test_set('LESS_EQUAL')
                    shader.bind()

                    if rail_cyl_b and getattr(prop, "show_round_rail_diameter_guide", True):
                        gpu.state.line_width_set(1.5)
                        shader.uniform_float('color', COLOR_RAIL_SHAPE)
                        rail_cyl_b.draw(shader)

                    gpu.state.line_width_set(3.4)
                    if ledge_b:
                        shader.uniform_float('color', COLOR_LEDGE_ORANGE)
                        ledge_b.draw(shader)
                    if rail_b:
                        shader.uniform_float('color', COLOR_RAIL_CYAN)
                        rail_b.draw(shader)
                    if coping_b:
                        shader.uniform_float('color', COLOR_COPING_MAGENTA)
                        coping_b.draw(shader)
                    if nogrind_b:
                        gpu.state.line_width_set(2.2)
                        shader.uniform_float('color', COLOR_COL_RED)
                        nogrind_b.draw(shader)

                    gpu.state.line_width_set(1.0)
                    gpu.state.depth_test_set('NONE')
                    gpu.state.blend_set('NONE')
        except Exception:
            pass

    # 2. Skate Collision Geometry Overlay (with live sunken Anti-Lip preview)
    draw_col_overlay(ctx, active_obj=active_obj, prop=prop, shader=shader)

    # 2a. Per-Face Surface Audio Override Overlay
    draw_face_surface_overlay(ctx, active_obj=active_obj, prop=prop, shader=shader)

    # 2a-sec. 2.5-Second Pulsing Material Slot Surface Highlight Overlay (§6.1)
    draw_surface_highlight_overlay(ctx, active_obj=active_obj, shader=shader)

    # 2a-bis. 'Do Not Align' & 'No-Grind' Troubleshooting GPU Overlay
    draw_alignment_troubleshooting_overlay(ctx, active_obj=active_obj, prop=prop, shader=shader)

    # 2b. Quick Drop Placement Proxy Bounding Box & Ground Footprint Overlay
    draw_placement_proxy_overlay(ctx, active_obj=active_obj, prop=prop, shader=shader)

    # 3. Linked Dynamic Light Cones Overlay
    if getattr(prop, "draw_light_overlay", False):
        try:
            lights = []
            seen = set()
            if hasattr(active_obj, "reskate_linked_lights"):
                for it in active_obj.reskate_linked_lights:
                    if getattr(it, "enabled", True) and getattr(it, "light_obj", None) and it.light_obj.type == 'LIGHT':
                        lights.append(it.light_obj)
                        seen.add(it.light_obj.name)
            for ch in active_obj.children:
                if ch.type == 'LIGHT' and ch.name not in seen:
                    lights.append(ch)
                    seen.add(ch.name)

            if lights:
                gpu.state.blend_set('ALPHA')
                gpu.state.depth_test_set('LESS_EQUAL')
                gpu.state.line_width_set(1.8)
                shader.bind()
                for l_obj in lights:
                    l_data = l_obj.data
                    if not l_data:
                        continue
                    col = getattr(l_data, "color", (1.0, 0.85, 0.55))
                    rgba = (float(col[0]), float(col[1]), float(col[2]), 0.78)
                    origin = l_obj.matrix_world.to_translation()
                    pts = []
                    if l_data.type == 'POINT':
                        # 3D Omnidirectional Point Light Sphere (3 orthogonal rings + 6 radial starburst rays)
                        sph_r = 0.36
                        segs = 16
                        for k in range(segs):
                            a0 = (2.0 * math.pi * k) / segs
                            a1 = (2.0 * math.pi * (k + 1)) / segs
                            c0, s0 = math.cos(a0) * sph_r, math.sin(a0) * sph_r
                            c1, s1 = math.cos(a1) * sph_r, math.sin(a1) * sph_r
                            # XY ring
                            pts.append((origin.x + c0, origin.y + s0, origin.z))
                            pts.append((origin.x + c1, origin.y + s1, origin.z))
                            # XZ ring
                            pts.append((origin.x + c0, origin.y, origin.z + s0))
                            pts.append((origin.x + c1, origin.y, origin.z + s1))
                            # YZ ring
                            pts.append((origin.x, origin.y + c0, origin.z + s0))
                            pts.append((origin.x, origin.y + c1, origin.z + s1))
                        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                            pts.append((origin.x + dx * 0.12, origin.y + dy * 0.12, origin.z + dz * 0.12))
                            pts.append((origin.x + dx * 0.54, origin.y + dy * 0.54, origin.z + dz * 0.54))
                    else:
                        # Directional Spot Light Cone along -Z
                        fwd = (l_obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
                        right = (l_obj.matrix_world.to_3x3() @ Vector((1.0, 0.0, 0.0))).normalized()
                        up = (l_obj.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))).normalized()
                        cone_len = 1.15
                        half_ang = 0.5 * float(getattr(l_data, "spot_size", math.radians(90.0)))
                        rad = cone_len * math.tan(min(math.radians(70.0), max(math.radians(10.0), half_ang)))
                        center_end = origin + fwd * cone_len
                        ring = []
                        for k in range(12):
                            ang = (2.0 * math.pi * k) / 12.0
                            ring.append(center_end + (right * math.cos(ang) + up * math.sin(ang)) * rad)
                        for k in range(12):
                            pts.append(tuple(ring[k]))
                            pts.append(tuple(ring[(k + 1) % 12]))
                            if k % 3 == 0:
                                pts.append(tuple(origin))
                                pts.append(tuple(ring[k]))
                    batch_l = gpu_extras.batch.batch_for_shader(shader, 'LINES', {'pos': pts})
                    shader.uniform_float('color', rgba)
                    batch_l.draw(shader)
                gpu.state.line_width_set(1.0)
                gpu.state.depth_test_set('NONE')
                gpu.state.blend_set('NONE')
        except Exception:
            pass


_QUICK_DROP_TAB_LABELS = {
    "Rails": "Rails",
    "Ledges": "Ledges",
    "Ramps": "Ramps",
    "Funboxes": "Funboxes",
    "QuarterPipes": "Quarter Pipes",
    "ManualPads": "Manual Pads",
    "Misc": "Misc",
}


def get_selected_props_viewport_info(context=None) -> list[dict]:
    """
    Returns a list of info dicts for each selected prop in the viewport (§6.2):
      - `name`: Display name of the prop
      - `quick_drop_tab`: Quick Drop tab label (e.g. 'Rails', 'Ramps', 'Ledges', ...)
      - `triangle_count`: Triangle count (int)
      - `surface_count`: Surface/material count (int)
      - `thumbnail_status`: 'has' or 'missing'
    """
    global _selected_props_hud_cache
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None

    props_list: list[bpy.types.Object] = []
    try:
        props_list = compiler_bridge.get_selected_prop_meshes(ctx)
    except Exception:
        pass
    if not props_list:
        act = _resolve_overlay_target_mesh(ctx)
        if act is not None and is_prop_or_helper_selected(act, ctx):
            props_list = [act]

    if not props_list:
        return []

    now = time.monotonic()
    hud_sig = (
        tuple(o.name for o in props_list if o),
        str(getattr(prop, "prop_display_name", "") or "") if prop else "",
        str(getattr(prop, "sub_category", "") or "") if prop else "",
        str(getattr(prop, "custom_thumbnail_path", "") or "") if prop else "",
    )
    if _selected_props_hud_cache is not None:
        c_ts, c_sig, c_res = _selected_props_hud_cache
        if c_sig == hud_sig and (now - c_ts) < 1.0:
            return list(c_res)

    out: list[dict] = []
    for p_obj in props_list:
        if not p_obj or getattr(p_obj, "type", None) != 'MESH' or not getattr(p_obj, "data", None):
            continue
        me = p_obj.data
        n_verts = len(me.vertices)
        n_polys = len(me.polygons)
        n_slots = len(p_obj.material_slots)
        sig = (n_verts, n_polys, n_slots)
        cached = _prop_hud_stats_cache.get(p_obj.name)
        if cached is not None and cached[0] == sig:
            tri_count, surf_count = cached[1], cached[2]
        else:
            tri_count = sum(max(0, len(poly.vertices) - 2) for poly in me.polygons) if n_polys <= 25000 else (n_polys * 2)
            surf_count = max(1, len([s for s in p_obj.material_slots if getattr(s, "material", None) is not None]) or n_slots)
            if len(_prop_hud_stats_cache) > 64:
                _prop_hud_stats_cache.clear()
            _prop_hud_stats_cache[p_obj.name] = (sig, int(tri_count), int(surf_count))

        disp_name = compiler_bridge.get_prop_display_name(p_obj, ctx)
        raw_tab = str(
            p_obj.get("reskate_sub_category")
            or (getattr(prop, "sub_category", "Rails") if prop else "Rails")
            or "Rails"
        )
        tab_label = _QUICK_DROP_TAB_LABELS.get(raw_tab, raw_tab)

        has_thumb = False
        try:
            custom_t = str(
                p_obj.get("reskate_custom_thumbnail_path")
                or (getattr(prop, "custom_thumbnail_path", "") if prop and p_obj == getattr(ctx, "active_object", None) else "")
                or ""
            ).strip()
            if custom_t and compiler_bridge.Path(bpy.path.abspath(custom_t)).exists():
                has_thumb = True
            elif bool(p_obj.get("reskate_thumb_rendered", False)):
                has_thumb = True
            else:
                slug = compiler_bridge.get_prop_slug(ctx, p_obj)
                st_dir = compiler_bridge._get_staging_dir()
                for cand_p in (
                    st_dir / f"{slug}_Thumbnail.png",
                    st_dir / "pack_staging" / f"{slug}_Thumbnail.png",
                ):
                    if cand_p.exists():
                        has_thumb = True
                        break
        except Exception:
            has_thumb = False

        out.append({
            "obj_name": p_obj.name,
            "name": disp_name,
            "quick_drop_tab": tab_label,
            "triangle_count": int(tri_count),
            "surface_count": int(surf_count),
            "thumbnail_status": "has" if has_thumb else "missing",
        })
    _selected_props_hud_cache = (now, hud_sig, list(out))
    return out


def _batch_for_2d_shader(shader, prim_type: str, pts_2d: list[tuple[float, float]]):
    """
    Safely creates a 2D GPU batch for Blender 4.x/5.x `UNIFORM_COLOR` shaders,
    automatically handling shaders expecting 3D `(x, y, 0.0)` or 2D `(x, y)` `pos` attributes.
    """
    pts_3d = [(float(p[0]), float(p[1]), 0.0) for p in pts_2d]
    try:
        return gpu_extras.batch.batch_for_shader(shader, prim_type, {'pos': pts_3d})
    except Exception:
        return gpu_extras.batch.batch_for_shader(
            shader, prim_type, {'pos': [(float(p[0]), float(p[1])) for p in pts_2d]}
        )


def get_scene_prop_meshes_for_badges(context=None, limit: int = 16) -> list[bpy.types.Object]:
    """
    Returns up to `limit` valid prop mesh objects in the scene for 2D hovering viewport badges,
    ordered with selected/active props first, excluding `_COL`, `_PPHYS`, scale references, and studio helpers.
    """
    global _scene_badge_objs_cache
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if not scene:
        return []

    sel_props = compiler_bridge.get_selected_prop_meshes(ctx)
    active_obj = _resolve_overlay_target_mesh(ctx)
    now = time.monotonic()
    b_sig = (
        len(getattr(scene, "objects", [])),
        getattr(active_obj, "name", "") if active_obj else "",
        tuple(o.name for o in sel_props if o),
        int(limit),
    )
    if _scene_badge_objs_cache is not None:
        c_ts, c_sig, c_names = _scene_badge_objs_cache
        if c_sig == b_sig and (now - c_ts) < 0.5:
            resolved = []
            for nm in c_names:
                ob = scene.objects.get(nm)
                if ob is not None and getattr(ob, "type", None) == 'MESH' and not getattr(ob, "hide_get", lambda: False)():
                    resolved.append(ob)
            return resolved

    seen: set[str] = set()
    ordered: list[bpy.types.Object] = []

    for obj in sel_props:
        if obj and getattr(obj, "name", "") and obj.name not in seen:
            seen.add(obj.name)
            ordered.append(obj)

    if active_obj and getattr(active_obj, "name", "") and active_obj.name not in seen:
        if not is_scale_reference_object(active_obj) and not compiler_bridge.is_collision_helper_object(active_obj):
            seen.add(active_obj.name)
            ordered.append(active_obj)

    for obj in getattr(scene, "objects", []):
        if len(ordered) >= limit:
            break
        if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
            continue
        if obj.name in seen:
            continue
        if getattr(obj, "hide_get", lambda: False)() or getattr(obj, "hide_viewport", False):
            continue
        if (
            obj.name.startswith("_ReSkate_")
            or obj.name.startswith("ReSkate_")
            or is_scale_reference_object(obj)
            or compiler_bridge.is_collision_helper_object(obj)
        ):
            continue
        seen.add(obj.name)
        ordered.append(obj)

    res = ordered[:limit]
    _scene_badge_objs_cache = (now, b_sig, [o.name for o in res])
    return res


def build_prop_hover_badge_lines(
    p_obj: bpy.types.Object,
    context=None,
    is_selected_or_active: bool = False,
) -> dict:
    """
    Builds the 1- or 2-line hovering viewport badge data for a prop mesh `p_obj`.
    - Line 1: `<Prop Display Name>  |  📦 <PackName or New Prop>` (+ `  |  🔗 <FamilyTitle>` if grouped)
    - Line 2 (for selected/active props): `🔊 <Surface>  |  Grind: <Summary>`
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    prop = getattr(scene, "reskate_prop", None) if scene else None

    now = time.monotonic()
    me = getattr(p_obj, "data", None)
    hb_key = (
        p_obj.name,
        bool(is_selected_or_active),
        len(me.vertices) if me else 0,
        len(p_obj.material_slots),
        str(p_obj.get("reskate_prop_display_name", "") or ""),
        str(p_obj.get("reskate_family_title", "") or ""),
        str(getattr(prop, "prop_display_name", "") or "") if (prop and is_selected_or_active) else "",
        str(getattr(prop, "prop_family_title", "") or "") if (prop and is_selected_or_active) else "",
        str(getattr(prop, "primary_surface", "") or "") if (prop and is_selected_or_active) else "",
    )
    hb_cached = _hover_badge_lines_cache.get(p_obj.name)
    if hb_cached is not None:
        c_ts, c_val = hb_cached
        if c_val.get("_hb_key") == hb_key and (now - c_ts) < 1.0:
            return c_val

    disp_name = compiler_bridge.get_prop_display_name(p_obj, ctx)
    packs = compiler_bridge.get_object_pack_memberships(ctx, p_obj)
    if packs:
        pack_label = f"📦 {packs[0]}" + (f" (+{len(packs) - 1})" if len(packs) > 1 else "")
    else:
        pack_label = "📦 New Prop"

    fam_title = str(
        p_obj.get("reskate_family_title", "")
        or (getattr(prop, "prop_family_title", "") if (prop and p_obj == getattr(ctx, "active_object", None)) else "")
        or ""
    ).strip()

    line1 = f"{disp_name}  |  {pack_label}"
    if fam_title:
        line1 += f"  |  🔗 {fam_title}"

    lines = [line1]
    surf_summary = ""
    grind_summary = ""

    if is_selected_or_active:
        try:
            surf_groups = compiler_bridge.get_object_face_surface_summary(p_obj, ctx)
            if surf_groups:
                primary_surf = str(surf_groups[0].get("label", "Concrete"))
                extra_s = len(surf_groups) - 1
                surf_summary = f"🔊 {primary_surf}" + (f" (+{extra_s})" if extra_s > 0 else "")
            else:
                surf_summary = "🔊 Concrete"
        except Exception:
            surf_summary = "🔊 Concrete"

        try:
            counts = get_grind_edge_counts(p_obj)
            n_ledge = int(counts.get("ledge", 0))
            n_rail = int(counts.get("rail", 0))
            n_coping = int(counts.get("coping", 0))
            g_parts = []
            if n_ledge > 0:
                g_parts.append(f"{n_ledge} Ledge")
            if n_rail > 0:
                g_parts.append(f"{n_rail} Rail")
            if n_coping > 0:
                g_parts.append(f"{n_coping} Coping")
            grind_summary = "Grind: " + (" / ".join(g_parts) if g_parts else "None")
        except Exception:
            grind_summary = "Grind: None"

        lines.append(f"{surf_summary}  |  {grind_summary}")

    res_dict = {
        "_hb_key": hb_key,
        "obj_name": p_obj.name,
        "display_name": disp_name,
        "pack_label": pack_label,
        "in_compiled_pack": bool(packs),
        "family_title": fam_title,
        "surface_summary": surf_summary,
        "grind_summary": grind_summary,
        "lines": lines,
    }
    if len(_hover_badge_lines_cache) > 64:
        _hover_badge_lines_cache.clear()
    _hover_badge_lines_cache[p_obj.name] = (now, res_dict)
    return res_dict


def draw_selected_props_info_box_2d(context=None, region=None, shader=None):
    """
    Draws the compact 2D HUD info box in the bottom-left of the 3D Viewport showing:
    name, Quick Drop tab, triangle count, surface/material count, and thumbnail status (has / missing)
    for each selected prop (§6.2).
    """
    ctx = context if context is not None else bpy.context
    if region is None:
        region = getattr(ctx, "region", None)
    if shader is None:
        shader = _get_shader()
    if not region or not shader:
        return

    items = get_selected_props_viewport_info(ctx)
    if not items:
        return

    max_rows = 5
    shown = items[:max_rows]
    extra_count = max(0, len(items) - max_rows)

    lines: list[tuple[str, tuple[float, float, float, float]]] = []
    header_txt = f"Selected Prop{'s' if len(items) > 1 else ''} ({len(items)})"
    lines.append((header_txt, (0.68, 0.86, 1.00, 0.98)))

    for it in shown:
        thumb_str = "Thumbnail: has" if it["thumbnail_status"] == "has" else "Thumbnail: missing"
        surf_word = "surface" if it["surface_count"] == 1 else "surfaces"
        row_txt = (
            f"• {it['name']}  |  {it['quick_drop_tab']}  |  "
            f"{it['triangle_count']:,} tris  |  {it['surface_count']} {surf_word}  |  {thumb_str}"
        )
        col = (0.94, 0.96, 0.98, 0.96) if it["thumbnail_status"] == "has" else (0.98, 0.92, 0.78, 0.96)
        lines.append((row_txt, col))

    if extra_count > 0:
        lines.append((f"  +{extra_count} more selected...", (0.75, 0.78, 0.84, 0.90)))

    font_id = 0
    blf.size(font_id, 11)
    line_dims = [blf.dimensions(font_id, txt) for txt, _ in lines]
    max_tw = max((d[0] for d in line_dims), default=160.0)
    line_h = max((d[1] for d in line_dims), default=12.0) + 5.0

    pad_x = 10.0
    pad_y = 7.0
    bw = max(220.0, max_tw + pad_x * 2.0 + 6.0)
    bh = len(lines) * line_h + pad_y * 2.0

    x0 = 16.0
    y0 = 16.0
    x1 = min(float(region.width) - 16.0, x0 + bw)
    y1 = min(float(region.height) - 16.0, y0 + bh)

    bg_v = [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]
    bar_v = [(x0, y0), (x0 + 3.5, y0), (x0 + 3.5, y1), (x0, y0), (x0 + 3.5, y1), (x0, y1)]
    bdr_v = [(x0, y0), (x1, y0), (x1, y0), (x1, y1), (x1, y1), (x0, y1), (x0, y1), (x0, y0)]

    try:
        b_bg = _batch_for_2d_shader(shader, 'TRIS', bg_v)
        b_bar = _batch_for_2d_shader(shader, 'TRIS', bar_v)
        b_bdr = _batch_for_2d_shader(shader, 'LINES', bdr_v)
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', (0.07, 0.08, 0.10, 0.84))
        b_bg.draw(shader)
        shader.uniform_float('color', (0.22, 0.72, 0.98, 0.95))
        b_bar.draw(shader)
        gpu.state.line_width_set(1.0)
        shader.uniform_float('color', (0.28, 0.34, 0.42, 0.65))
        b_bdr.draw(shader)
        gpu.state.blend_set('NONE')

        cur_y = y1 - pad_y - line_h + 4.0
        for txt, rgba in lines:
            blf.position(font_id, x0 + pad_x + 4.0, cur_y, 0.0)
            blf.color(font_id, rgba[0], rgba[1], rgba[2], rgba[3])
            blf.draw(font_id, txt)
            cur_y -= line_h
    except Exception:
        pass


def _draw_callback_2d(context=None):
    """SpaceView3D POST_PIXEL callback drawing hovering 2D Prop Info cards, Surface labels, and Grind indicators."""
    if bpy.app.background:
        return

    ctx = context if context is not None else bpy.context
    if not are_viewport_overlays_enabled_in_prefs(ctx):
        return
    scene = getattr(ctx, "scene", None)
    region = getattr(ctx, "region", None)
    rv3d = getattr(ctx, "region_data", None)
    if not scene or not region or not rv3d:
        return

    prop = getattr(scene, "reskate_prop", None)
    if not prop:
        return

    shader = _get_shader()
    if shader is None:
        return

    active_obj = _resolve_overlay_target_mesh(ctx)
    sel_objs = compiler_bridge.get_selected_prop_meshes(ctx)
    sel_names = {o.name for o in sel_objs if o}
    if active_obj and is_prop_or_helper_selected(active_obj, ctx):
        sel_names.add(active_obj.name)

    badges_allowed = are_pack_badges_enabled_in_prefs(ctx) and bool(getattr(prop, "show_pack_membership_overlay", True))

    # 1. Selected-Objects HUD Info Box in bottom-left of Viewport (§6.2)
    if badges_allowed:
        draw_selected_props_info_box_2d(ctx, region=region, shader=shader)

    # 2. Hovering 2D Prop Cards over scene props (§3 Viewport Overlays)
    if badges_allowed:
        try:
            cand_objs = get_scene_prop_meshes_for_badges(ctx, limit=16)
            for p_obj in cand_objs:
                is_sel = p_obj.name in sel_names
                badge = build_prop_hover_badge_lines(p_obj, ctx, is_selected_or_active=is_sel)
                b_lines = badge.get("lines", [])
                if not b_lines:
                    continue

                mw_p = p_obj.matrix_world
                if p_obj.bound_box:
                    w_pts_p = [mw_p @ Vector(pt) for pt in p_obj.bound_box]
                    cx_p = sum(p.x for p in w_pts_p) / len(w_pts_p)
                    cy_p = sum(p.y for p in w_pts_p) / len(w_pts_p)
                    top_z_p = max(p.z for p in w_pts_p) + 0.22
                    anchor_p = Vector((cx_p, cy_p, top_z_p))
                else:
                    anchor_p = mw_p.to_translation() + Vector((0.0, 0.0, 0.45))

                pt_p = location_3d_to_region_2d(region, rv3d, anchor_p)
                if pt_p is None:
                    continue

                font_id = 0
                blf.size(font_id, 10)
                dims_p = [blf.dimensions(font_id, ln) for ln in b_lines]
                max_tw_p = max((d[0] for d in dims_p), default=90.0)
                line_h_p = max((d[1] for d in dims_p), default=11.0) + 4.0

                px_p, py_p = 8.0, 5.0
                bw_p = max(96.0, max_tw_p + px_p * 2.0 + 5.0)
                bh_p = len(b_lines) * line_h_p + py_p * 2.0 - 2.0

                px0 = max(6.0, min(float(region.width) - bw_p - 6.0, float(pt_p.x) - bw_p * 0.5))
                py0 = max(6.0, min(float(region.height) - bh_p - 6.0, float(pt_p.y) + 6.0))
                px1, py1 = px0 + bw_p, py0 + bh_p

                bg_v = [(px0, py0), (px1, py0), (px1, py1), (px0, py0), (px1, py1), (px0, py1)]
                bar_v = [(px0, py0), (px0 + 3.5, py0), (px0 + 3.5, py1), (px0, py0), (px0 + 3.5, py1), (px0, py1)]
                bdr_v = [(px0, py0), (px1, py0), (px1, py0), (px1, y1 if False else py1), (px1, py1), (px0, py1), (px0, py1), (px0, py0)]

                b_bg = _batch_for_2d_shader(shader, 'TRIS', bg_v)
                b_bar = _batch_for_2d_shader(shader, 'TRIS', bar_v)
                b_bdr = _batch_for_2d_shader(shader, 'LINES', bdr_v)

                if badge.get("in_compiled_pack"):
                    accent_col = (0.28, 0.85, 0.52, 0.96)
                elif is_sel:
                    accent_col = (0.22, 0.74, 0.98, 0.96)
                else:
                    accent_col = (0.58, 0.66, 0.78, 0.85)

                gpu.state.blend_set('ALPHA')
                shader.bind()
                shader.uniform_float('color', (0.08, 0.10, 0.14, 0.88 if is_sel else 0.76))
                b_bg.draw(shader)
                shader.uniform_float('color', accent_col)
                b_bar.draw(shader)
                gpu.state.line_width_set(1.0)
                shader.uniform_float('color', (accent_col[0], accent_col[1], accent_col[2], 0.55 if is_sel else 0.35))
                b_bdr.draw(shader)
                gpu.state.blend_set('NONE')

                cur_py = py1 - py_p - line_h_p + 3.0
                for idx_l, ln_txt in enumerate(b_lines):
                    blf.position(font_id, px0 + px_p + 3.5, cur_py, 0.0)
                    if idx_l == 0:
                        blf.color(font_id, 0.95, 0.98, 1.0, 0.98)
                    else:
                        blf.color(font_id, 0.76, 0.90, 0.98, 0.95)
                    blf.draw(font_id, ln_txt)
                    cur_py -= line_h_p
        except Exception:
            pass

    if not active_obj or not is_prop_or_helper_selected(active_obj, ctx):
        return

    # 3. 2D Floating Face Surface Audio Labels
    if getattr(prop, "draw_face_surface_overlay", False):
        try:
            surf_groups = compiler_bridge.get_object_face_surface_summary(active_obj, ctx)
            for grp in surf_groups[:8]:
                c_world = grp.get("centroid_world")
                if not c_world or len(c_world) < 3:
                    continue
                pt_s = location_3d_to_region_2d(region, rv3d, Vector((float(c_world[0]), float(c_world[1]), float(c_world[2]))))
                if pt_s is None:
                    continue
                s_label = f"🔊 {grp.get('label', 'Custom')} ({int(grp.get('face_count', 0))}f)"
                s_rgba = grp.get("color", (0.25, 0.85, 0.95, 0.95))
                font_id = 0
                blf.size(font_id, 10)
                tw_s, th_s = blf.dimensions(font_id, s_label)
                px_s, py_s = 6.0, 4.0
                bw_s = max(72.0, tw_s + px_s * 2.0 + 4.0)
                bh_s = max(18.0, th_s + py_s * 2.0)
                sx0 = max(6.0, min(float(region.width) - bw_s - 6.0, float(pt_s.x) - bw_s * 0.5))
                sy0 = max(6.0, min(float(region.height) - bh_s - 6.0, float(pt_s.y) - bh_s * 0.5))
                sx1, sy1 = sx0 + bw_s, sy0 + bh_s
                bg_v = [(sx0, sy0), (sx1, sy0), (sx1, sy1), (sx0, sy0), (sx1, sy1), (sx0, sy1)]
                bar_v = [(sx0, sy0), (sx0 + 3.0, sy0), (sx0 + 3.0, sy1), (sx0, sy0), (sx0 + 3.0, sy1), (sx0, sy1)]
                b_bg = _batch_for_2d_shader(shader, 'TRIS', bg_v)
                b_bar = _batch_for_2d_shader(shader, 'TRIS', bar_v)
                gpu.state.blend_set('ALPHA')
                shader.bind()
                shader.uniform_float('color', (0.08, 0.09, 0.11, 0.84))
                b_bg.draw(shader)
                shader.uniform_float('color', (float(s_rgba[0]), float(s_rgba[1]), float(s_rgba[2]), 0.95))
                b_bar.draw(shader)
                gpu.state.blend_set('NONE')
                blf.position(font_id, sx0 + px_s + 3.0, sy0 + py_s, 0.0)
                blf.color(font_id, 0.96, 0.97, 0.99, 0.98)
                blf.draw(font_id, s_label)
        except Exception:
            pass

    # 4. 2D Floating 'Do Not Align' / 'No-Grind' Troubleshooting Warning Banner
    if getattr(prop, "show_alignment_overlay", False):
        try:
            issues = compiler_bridge.inspect_prop_alignment_issues(ctx, active_obj)
            if issues.get("has_do_not_align") or issues.get("has_no_grind"):
                mw_w = active_obj.matrix_world
                if active_obj.bound_box:
                    w_pts_w = [mw_w @ Vector(pt) for pt in active_obj.bound_box]
                    cx_w = sum(p.x for p in w_pts_w) / len(w_pts_w)
                    cy_w = sum(p.y for p in w_pts_w) / len(w_pts_w)
                    top_z_w = max(p.z for p in w_pts_w) + 0.14
                    anchor_w = Vector((cx_w, cy_w, top_z_w))
                else:
                    anchor_w = mw_w.to_translation() + Vector((0.0, 0.0, 0.35))
                pt_w = location_3d_to_region_2d(region, rv3d, anchor_w)
                if pt_w is not None:
                    is_ng_only = bool(issues.get("has_no_grind") and not issues.get("has_do_not_align"))
                    first_line = issues.get("summary_lines", ["Do Not Align Active"])[0]
                    warn_text = f"⚠️ {'NO-GRIND' if is_ng_only else 'DO NOT ALIGN'}: {first_line}"
                    font_id = 0
                    blf.size(font_id, 11)
                    tw_w, th_w = blf.dimensions(font_id, warn_text)
                    px_w, py_w = 8.0, 5.0
                    bw_w = max(140.0, tw_w + px_w * 2.0 + 6.0)
                    bh_w = max(21.0, th_w + py_w * 2.0)
                    wx0 = max(8.0, min(float(region.width) - bw_w - 8.0, float(pt_w.x) - bw_w * 0.5))
                    wy0 = max(8.0, min(float(region.height) - bh_w - 8.0, float(pt_w.y) + 34.0))
                    wx1, wy1 = wx0 + bw_w, wy0 + bh_w
                    w_col = (0.98, 0.22, 0.24, 0.96) if is_ng_only else (1.00, 0.62, 0.10, 0.96)
                    bg_v = [(wx0, wy0), (wx1, wy0), (wx1, wy1), (wx0, wy0), (wx1, wy1), (wx0, wy1)]
                    bar_v = [(wx0, wy0), (wx0 + 4.0, wy0), (wx0 + 4.0, wy1), (wx0, wy0), (wx0 + 4.0, wy1), (wx0, wy1)]
                    bdr_v = [(wx0, wy0), (wx1, wy0), (wx1, wy0), (wx1, wy1), (wx1, wy1), (wx0, wy1), (wx0, wy1), (wx0, wy0)]
                    b_bg = _batch_for_2d_shader(shader, 'TRIS', bg_v)
                    b_bar = _batch_for_2d_shader(shader, 'TRIS', bar_v)
                    b_bdr = _batch_for_2d_shader(shader, 'LINES', bdr_v)
                    gpu.state.blend_set('ALPHA')
                    shader.bind()
                    shader.uniform_float('color', (0.14, 0.09, 0.05, 0.92))
                    b_bg.draw(shader)
                    shader.uniform_float('color', w_col)
                    b_bar.draw(shader)
                    gpu.state.line_width_set(1.2)
                    shader.uniform_float('color', (w_col[0], w_col[1], w_col[2], 0.70))
                    b_bdr.draw(shader)
                    gpu.state.blend_set('NONE')
                    blf.position(font_id, wx0 + px_w + 4.0, wy0 + py_w, 0.0)
                    blf.color(font_id, 1.0, 0.92, 0.75, 1.0)
                    blf.draw(font_id, warn_text)
        except Exception:
            pass

    # 5. Standalone Floating Grind Badge (when Pack Membership Card is turned off)
    if getattr(prop, "show_pack_membership_overlay", True):
        return
    if not getattr(prop, "draw_grind_overlay", True) or not getattr(prop, "show_grind_floating_badge", True):
        return

    try:
        counts = get_grind_edge_counts(active_obj)
        n_ledge = int(counts.get("ledge", 0))
        n_rail = int(counts.get("rail", 0))
        n_coping = int(counts.get("coping", 0))
        n_nogrind = int(counts.get("no_grind", 0))
        if (n_ledge + n_rail + n_coping + n_nogrind) == 0:
            return

        # Compute world top-center of active_obj
        mw = active_obj.matrix_world
        if active_obj.bound_box:
            w_pts = [mw @ Vector(pt) for pt in active_obj.bound_box]
            cx = sum(p.x for p in w_pts) / len(w_pts)
            cy = sum(p.y for p in w_pts) / len(w_pts)
            top_z = max(p.z for p in w_pts) + 0.14
            anchor_3d = Vector((cx, cy, top_z))
        else:
            anchor_3d = mw.to_translation() + Vector((0.0, 0.0, 0.35))

        pt2d = location_3d_to_region_2d(region, rv3d, anchor_3d)
        if pt2d is None:
            return

        parts = []
        accent_rgba = COLOR_LEDGE_ORANGE
        if n_rail > 0 and n_ledge == 0:
            accent_rgba = COLOR_RAIL_CYAN
        elif n_coping > 0 and n_ledge == 0:
            accent_rgba = COLOR_COPING_MAGENTA
        elif n_nogrind > 0 and (n_ledge + n_rail + n_coping) == 0:
            accent_rgba = COLOR_COL_RED

        if n_ledge > 0:
            parts.append(f"{n_ledge} Ledge")
        if n_rail > 0:
            parts.append(f"{n_rail} Rail")
        if n_coping > 0:
            parts.append(f"{n_coping} Coping")
        if n_nogrind > 0 and not parts:
            parts.append(f"{n_nogrind} No-Grind")

        label_text = "Grind: " + " | ".join(parts)
        font_id = 0
        blf.size(font_id, 11)
        tw, th = blf.dimensions(font_id, label_text)

        pad_x = 8.0
        pad_y = 5.0
        bw = max(92.0, tw + pad_x * 2.0 + 6.0)
        bh = max(21.0, th + pad_y * 2.0)

        x0 = float(pt2d.x) - bw * 0.5
        y0 = float(pt2d.y) + 8.0
        x0 = max(8.0, min(float(region.width) - bw - 8.0, x0))
        y0 = max(8.0, min(float(region.height) - bh - 8.0, y0))
        x1 = x0 + bw
        y1 = y0 + bh

        bg_verts = [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]
        bar_verts = [(x0, y0), (x0 + 3.5, y0), (x0 + 3.5, y1), (x0, y0), (x0 + 3.5, y1), (x0, y1)]
        border_verts = [(x0, y0), (x1, y0), (x1, y0), (x1, y1), (x1, y1), (x0, y1), (x0, y1), (x0, y0)]

        batch_bg = _batch_for_2d_shader(shader, 'TRIS', bg_verts)
        batch_bar = _batch_for_2d_shader(shader, 'TRIS', bar_verts)
        batch_border = _batch_for_2d_shader(shader, 'LINES', border_verts)

        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', (0.09, 0.10, 0.12, 0.86))
        batch_bg.draw(shader)
        shader.uniform_float('color', (accent_rgba[0], accent_rgba[1], accent_rgba[2], 0.95))
        batch_bar.draw(shader)
        gpu.state.line_width_set(1.0)
        shader.uniform_float('color', (accent_rgba[0], accent_rgba[1], accent_rgba[2], 0.55))
        batch_border.draw(shader)
        gpu.state.blend_set('NONE')

        blf.position(font_id, x0 + pad_x + 4.0, y0 + pad_y, 0.0)
        blf.color(font_id, 0.96, 0.97, 0.99, 0.98)
        blf.draw(font_id, label_text)
    except Exception:
        pass


def draw_callback_view():
    """SpaceView3D POST_VIEW callback wrapper."""
    _draw_callback_3d(bpy.context)


def draw_callback_pixel():
    """SpaceView3D POST_PIXEL callback wrapper."""
    _draw_callback_2d(bpy.context)


def is_any_3d_overlay_active(context=None) -> bool:
    """
    Returns True only when at least one primary 3D viewport overlay is actively toggled ON.
    When all overlays are OFF, returns False so the SpaceView3D POST_VIEW draw handler can be
    detached completely for zero viewport draw overhead.
    """
    if _SURFACE_HIGHLIGHT_STATE.get("active"):
        return True
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None) if ctx else None
    if scene is None:
        return False
    if bool(getattr(scene, "reskate_show_col_overlay", False)):
        return True
    prop = getattr(scene, "reskate_prop", None)
    if prop is None:
        return False
    return bool(
        getattr(prop, "draw_grind_overlay", False)
        or getattr(prop, "draw_col_overlay", False)
        or getattr(prop, "show_collision_overlay", False)
        or getattr(prop, "show_placement_proxy_overlay", False)
        or getattr(prop, "draw_face_surface_overlay", False)
        or getattr(prop, "show_alignment_overlay", False)
        or getattr(prop, "draw_light_overlay", False)
    )


def sync_draw_handlers(context=None, allow_background: bool = False) -> bool:
    """
    Synchronizes SpaceView3D POST_VIEW draw handler registration with active primary 3D overlay toggles
    and the Add-on Preferences master switch (`enable_viewport_overlays_master`).
    Only attaches `SpaceView3D.draw_handler_add` when at least one primary 3D overlay is toggled ON,
    and immediately detaches (`SpaceView3D.draw_handler_remove`) when all overlays are turned OFF
    so there is zero viewport overhead when disabled.
    (The 2D POST_PIXEL Pack Membership badges handler is deprecated and permanently detached.)
    """
    global _draw_handler_handle, _draw_handler_2d_handle
    if bpy.app.background and not allow_background:
        return False

    # Ensure deprecated 2D POST_PIXEL handler is never attached
    if _draw_handler_2d_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_2d_handle, 'WINDOW')
        except Exception:
            pass
        _draw_handler_2d_handle = None

    ctx = context if context is not None else bpy.context
    want_3d = bool(is_any_3d_overlay_active(ctx) and are_viewport_overlays_enabled_in_prefs(ctx))
    if not want_3d:
        if _draw_handler_handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_handle, 'WINDOW')
            except Exception:
                pass
            _draw_handler_handle = None
        return False

    if _draw_handler_handle is None:
        try:
            _draw_handler_handle = bpy.types.SpaceView3D.draw_handler_add(
                draw_callback_view, (), 'WINDOW', 'POST_VIEW'
            )
        except Exception:
            _draw_handler_handle = None
    return _draw_handler_handle is not None


def register():
    """Synchronizes the 3D viewport draw handler with strict headless and zero-overhead-when-disabled protection."""
    if bpy.app.background:
        return
    sync_draw_handlers(bpy.context)


register_draw_handlers = register


def unregister():
    """Removes the 3D and 2D viewport draw handlers."""
    global _draw_handler_handle, _draw_handler_2d_handle
    if bpy.app.background:
        return

    if _draw_handler_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_handle, 'WINDOW')
        except Exception:
            pass
        _draw_handler_handle = None

    if _draw_handler_2d_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_2d_handle, 'WINDOW')
        except Exception:
            pass
        _draw_handler_2d_handle = None
