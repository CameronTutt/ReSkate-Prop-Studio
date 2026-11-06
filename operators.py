"""
ReSkate Prop Studio - operators.py
Core functional operator backend for Blender 5.3 Beta implementing the 7-Step Checklist:
  1. Setup: set_origin, place_on_ground_world_center, toggle_skater_ref, toggle_skateboard_ref
  2. Collision: toggle_col_preview, generate_authored_col, create_simplified_col_copy, sync_material_surfaces, trim_bottom_lip
  3. Grind: autodetect_grind_edges, add_grind_path_from_selection, remove_grind_path, clear_grind_paths, mark_grind_edge, clear_grind_edge, clear_all_grind_edges, generate_grind_tube_proxy
  4. Look (Textures): bake_uv_atlas (strictly non-destructive), preview_baked_atlas
  5. Light (in-game): add_dynamic_prop_light, link_light_slot, unlink_light_slot
  6. Thumbnail: frame_isometric_camera, render_thumbnails
  7. Ship: compile_and_deploy, queue_compile_all (non-blocking modal + background support), cancel_compile, copy_compile_log, open_mod_folder, inspect_manifest
"""

from __future__ import annotations

import heapq
import json
import math
import os
import queue as py_queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
import uuid
from pathlib import Path

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

try:
    from PIL import Image
except ImportError:
    Image = None

from . import compiler_bridge
from . import gpu_draw
from . import properties
from . import scale_reference_meshes as scale_refs
from .properties import (
    GRIND_GEOMETRY_TYPES,
    LOOK_PRESETS,
    ORIGIN_MODES,
    SURFACE_FEEL_PRESETS,
    auto_detect_default_paths,
    get_addon_preferences,
    get_or_init_object_prop_state,
    get_surface_label,
    is_collision_helper_object,
    is_do_not_align_surface_code,
    is_no_grind_surface_code,
    load_prop_settings_from_object,
    resolve_collision_to_owner_prop,
    save_object_prop_state,
    save_persisted_user_paths,
    save_prop_settings_to_object,
    strip_do_not_align_code,
    sync_active_object_props,
)


DEFAULT_STAGING_DIR = compiler_bridge.DEFAULT_STAGING_DIR
STUDIO_DIR = compiler_bridge.STUDIO_DIR
TEMP_BAKE_NODE_NAME = "ReSkate_Bake_Target_Node"
PREVIEW_MATERIAL_NAME = "ReSkate_Atlas_Preview"

# Module-level state for non-blocking background compilation
_ACTIVE_COMPILE_PROC: subprocess.Popen | None = None
_ACTIVE_COMPILE_QUEUE: py_queue.Queue | None = None
_ACTIVE_COMPILE_CANCELLED: bool = False


def get_target_mesh_object(context) -> bpy.types.Object | None:
    """Returns the active or target mesh object.
    Always resolves via `properties.resolve_active_prop_mesh(context)` and `compiler_bridge.get_selected_prop_meshes(context)`
    first so all operators target the exact same prop mesh displayed in the ReSkate UI panels.
    """
    try:
        resolved = properties.resolve_active_prop_mesh(context)
        if resolved is not None and getattr(resolved, "type", None) == 'MESH' and resolved.data and len(resolved.data.polygons) > 0:
            return resolved
    except Exception:
        pass

    try:
        sel_props = compiler_bridge.get_selected_prop_meshes(context)
        if sel_props:
            return sel_props[0]
    except Exception:
        pass

    obj = getattr(context, "active_object", None)
    if obj:
        resolved_o = resolve_collision_to_owner_prop(obj)
        if resolved_o is not None and getattr(resolved_o, "type", None) == 'MESH' and resolved_o.data and len(resolved_o.data.polygons) > 0:
            return resolved_o

    for sel in getattr(context, 'selected_objects', []):
        resolved_s = resolve_collision_to_owner_prop(sel)
        if resolved_s is not None and getattr(resolved_s, "type", None) == 'MESH' and resolved_s.data and len(resolved_s.data.polygons) > 0:
            return resolved_s

    scene = getattr(context, "scene", None)
    if scene and hasattr(scene, "objects"):
        mesh_objs = [
            o for o in scene.objects
            if o.type == 'MESH'
            and o.data
            and len(o.data.polygons) > 0
            and not o.name.startswith(("ReSkate_", "_ReSkate_"))
            and not bool(o.get("reskate_is_scale_reference", False))
            and not bool(o.get("reskate_is_ref_helper", False))
        ]
        non_col = [o for o in mesh_objs if not is_collision_helper_object(o)]
        if non_col:
            return non_col[0]
        if mesh_objs:
            return mesh_objs[0]

    return None


def get_prop_slug(context, obj) -> str:
    """Computes a clean file/asset slug for the active prop."""
    return compiler_bridge.get_prop_slug(context, obj)


# ==============================================================================
# Step 1: Setup Operators
# ==============================================================================

class RESKATE_OT_set_origin(bpy.types.Operator):
    """Moves the active object's origin to Bottom Center or Geometry Center without moving the object in the scene"""
    bl_idname = "reskate.set_origin"
    bl_label = "Set Origin"
    bl_description = "Moves only the object's origin (Bottom Center so the prop sits flush on the ground, or Geometry Center); the object itself stays exactly where it is in the scene"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Origin Position",
        items=ORIGIN_MODES,
        default='BOTTOM_CENTER',
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        if len(obj.data.vertices) == 0:
            self.report({'WARNING'}, "ReSkate: Active mesh has no vertices.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        chosen_mode = self.mode if self.properties.is_property_set("mode") else (getattr(prop, "origin_mode", 'BOTTOM_CENTER') if prop else 'BOTTOM_CENTER')

        # Compute bounding box in world-rotated/scaled space so objects with unapplied rotations
        # (e.g. 90° X Sketchfab imports) always find their true world-space bottom (min Z) and XY center!
        rot_scale = obj.matrix_world.to_3x3()
        world_pts = [rot_scale @ v.co for v in obj.data.vertices]
        xs = [p.x for p in world_pts]
        ys = [p.y for p in world_pts]
        zs = [p.z for p in world_pts]

        cx_w = 0.5 * (min(xs) + max(xs))
        cy_w = 0.5 * (min(ys) + max(ys))
        z_off = properties.get_origin_ground_z_offset(context) if hasattr(properties, "get_origin_ground_z_offset") else 0.05
        if chosen_mode == 'BOTTOM_CENTER':
            cz_w = min(zs) + z_off
            world_offset = Vector((cx_w, cy_w, cz_w))
        elif chosen_mode == 'GROUND_CONTACT':
            min_z_w = min(zs)
            z_span = max(0.01, max(zs) - min_z_w)
            tol_z = max(0.015, min(0.04, z_span * 0.03))
            contact_area = 0.0
            contact_xy = Vector((0.0, 0.0))
            for poly in obj.data.polygons:
                p_zs = [world_pts[vi].z for vi in poly.vertices]
                if p_zs and max(p_zs) <= min_z_w + tol_z:
                    a = max(1e-6, float(poly.area))
                    c_w = rot_scale @ poly.center
                    contact_xy += Vector((c_w.x, c_w.y)) * a
                    contact_area += a
            if contact_area > 1e-6:
                gx = contact_xy.x / contact_area
                gy = contact_xy.y / contact_area
            else:
                low_pts = [p for p in world_pts if p.z <= min_z_w + tol_z]
                if low_pts:
                    gx = sum(p.x for p in low_pts) / len(low_pts)
                    gy = sum(p.y for p in low_pts) / len(low_pts)
                else:
                    gx, gy = cx_w, cy_w
            world_offset = Vector((gx, gy, min_z_w + z_off))
        elif chosen_mode == 'CENTER_OF_MASS':
            total_area = 0.0
            accum = Vector((0.0, 0.0, 0.0))
            for poly in obj.data.polygons:
                a = float(poly.area)
                if a > 1e-8:
                    accum += (rot_scale @ poly.center) * a
                    total_area += a
            if total_area > 1e-8:
                world_offset = accum / total_area
            else:
                world_offset = sum(world_pts, Vector((0.0, 0.0, 0.0))) / max(1, len(world_pts))
        else:
            cz_w = 0.5 * (min(zs) + max(zs))
            world_offset = Vector((cx_w, cy_w, cz_w))
        try:
            local_offset = rot_scale.inverted() @ world_offset
        except Exception:
            local_offset = world_offset.copy()

        if local_offset.length > 1e-6:
            # Shift mesh vertices by -local_offset and compensate object world location so mesh stays in place!
            world_before = obj.matrix_world @ local_offset
            for vert in obj.data.vertices:
                vert.co -= local_offset
            obj.data.update()

            # Preserve child world transforms while shifting parent origin
            children_world = [(ch, ch.matrix_world.copy()) for ch in obj.children]
            obj.location = world_before
            context.view_layer.update()
            for ch, ch_mat in children_world:
                ch.matrix_world = ch_mat

        obj["reskate_custom_origin"] = True
        obj["reskate_custom_origin_set"] = True
        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        if chosen_mode == 'BOTTOM_CENTER':
            label = f"Origin to Bottom Center (+{z_off:.2f} m)" if z_off > 0.0 else "Origin to Bottom Center"
        elif chosen_mode == 'GROUND_CONTACT':
            label = f"Origin to Ground Contact (+{z_off:.2f} m)" if z_off > 0.0 else "Origin to Ground Contact"
        elif chosen_mode == 'CENTER_OF_MASS':
            label = "Center of Gravity / Volume"
        else:
            label = "Origin to Geometry Center"
        self.report({'INFO'}, f"ReSkate: {label} applied.")
        return {'FINISHED'}


class RESKATE_OT_origin_to_bottom_center(bpy.types.Operator):
    """Moves the object's origin to the horizontal center of its bounding box slightly above its lowest Z point (+0.05 m default) without moving the mesh"""
    bl_idname = "reskate.origin_to_bottom_center"
    bl_label = "Origin to Bottom Center"
    bl_description = "Moves the object's origin to the horizontal center of its bounding box slightly above its lowest Z point (+0.05 m default in Preferences) without moving the mesh."
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.set_origin(mode='BOTTOM_CENTER')


class RESKATE_OT_origin_to_geometry_center(bpy.types.Operator):
    """Moves the object's origin to the 3D center of its bounding box without moving the mesh"""
    bl_idname = "reskate.origin_to_geometry_center"
    bl_label = "Origin to Geometry Center"
    bl_description = "Moves the object's origin to the 3D center of its bounding box without moving the mesh."
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.set_origin(mode='GEOMETRY_CENTER')


class RESKATE_OT_origin_to_ground_contact(bpy.types.Operator):
    """Moves the object's origin to the area-weighted center of its lowest ground-touching faces (+0.05 m default height offset) so props place cleanly on the ground"""
    bl_idname = "reskate.origin_to_ground_contact"
    bl_label = "Origin to Ground Contact (Recommended)"
    bl_description = "Moves the object's origin to the area-weighted center of its lowest ground-touching faces (+0.05 m default height offset in Preferences) so props place cleanly on the ground."
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.set_origin(mode='GROUND_CONTACT')


class RESKATE_OT_drop_prop_flush_to_ground(bpy.types.Operator):
    """Translates the active (and selected) prop or scale reference mesh vertically along world Z so its lowest point rests flush at Z = 0.0 without changing its origin"""
    bl_idname = "reskate.drop_prop_flush_to_ground"
    bl_label = "Set Prop Flush to Ground (Z = 0)"
    bl_description = (
        "Shifts the selected prop or scale reference (Skater, Skateboard, Pipe) vertically along world Z "
        "so its lowest ground-contact vertex sits flush at Z = 0.0 without altering its origin point relative to the mesh"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        targets = list(compiler_bridge.get_selected_prop_meshes(context))
        seen_names = {o.name for o in targets if o}
        for sel_o in getattr(context, "selected_objects", []) or []:
            if (
                sel_o
                and getattr(sel_o, "type", None) == 'MESH'
                and getattr(sel_o, "data", None)
                and len(sel_o.data.vertices) > 0
                and not is_collision_helper_object(sel_o)
                and not sel_o.name.startswith("ReSkate_Ground_Plane")
                and sel_o.name not in seen_names
            ):
                targets.append(sel_o)
                seen_names.add(sel_o.name)
        if not targets:
            act = context.active_object or get_target_mesh_object(context)
            if act and getattr(act, "type", None) == 'MESH' and act.data and not act.name.startswith("ReSkate_Ground_Plane"):
                targets = [act]
        if not targets:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop or scale reference first.")
            return {'CANCELLED'}

        target_set = set(targets)
        moved_count = 0
        last_delta_z = 0.0
        for obj in targets:
            if not obj or obj.type != 'MESH' or not obj.data or len(obj.data.vertices) == 0:
                continue
            if obj.parent in target_set:
                continue
            mw = obj.matrix_world
            min_z = min((mw @ v.co).z for v in obj.data.vertices)
            delta_z = -float(min_z)
            if abs(delta_z) > 1e-7:
                mw_new = mw.copy()
                mw_new.translation.z += delta_z
                obj.matrix_world = mw_new
            last_delta_z = delta_z
            moved_count += 1

        context.view_layer.update()
        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        self.report(
            {'INFO'},
            f"ReSkate: Dropped {moved_count} mesh(es) flush to ground (Z = 0.0, ΔZ = {last_delta_z:+.3f} m).",
        )
        return {'FINISHED'}


class RESKATE_OT_set_flush_to_ground(bpy.types.Operator):
    """Alias operator for Set Prop Flush to Ground (Z = 0.0) supporting props and scale references"""
    bl_idname = "reskate.set_flush_to_ground"
    bl_label = "Set Prop Flush to Ground (Z = 0)"
    bl_description = (
        "Shifts the selected prop or scale reference (Skater, Skateboard, Pipe) vertically along world Z "
        "so its lowest ground-contact vertex sits flush at Z = 0.0 without altering its origin point relative to the mesh"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.drop_prop_flush_to_ground()


class RESKATE_OT_snap_origin_to_selected(bpy.types.Operator):
    """Moves the active object's origin to the center of selected vertices/edges/faces in Edit Mode (or 3D Cursor in Object Mode) without moving the mesh"""
    bl_idname = "reskate.snap_origin_to_selected"
    bl_label = "Snap Origin to Selected / Cursor"
    bl_description = "In Edit Mode: moves the object's origin to the center of selected vertices/edges/faces (great for cantilevered or bent props). In Object Mode: moves origin to the 3D Cursor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.data:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh object first.")
            return {'CANCELLED'}

        was_edit = (obj.mode == 'EDIT')
        target_local = None

        if was_edit:
            try:
                bm = bmesh.from_edit_mesh(obj.data)
                sel_verts = [v.co.copy() for v in bm.verts if v.select]
                if sel_verts:
                    target_local = sum(sel_verts, Vector((0.0, 0.0, 0.0))) / len(sel_verts)
            except Exception:
                pass
            if bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')

        if target_local is None:
            sel_v_obj = [v.co.copy() for v in obj.data.vertices if v.select]
            if sel_v_obj:
                target_local = sum(sel_v_obj, Vector((0.0, 0.0, 0.0))) / len(sel_v_obj)
            else:
                try:
                    target_local = obj.matrix_world.inverted() @ context.scene.cursor.location.copy()
                except Exception:
                    self.report({'WARNING'}, "ReSkate: Select faces/vertices in Edit Mode first, or place the 3D Cursor.")
                    return {'CANCELLED'}

        if target_local is not None and target_local.length > 1e-6:
            world_before = obj.matrix_world @ target_local
            for vert in obj.data.vertices:
                vert.co -= target_local
            obj.data.update()
            children_world = [(ch, ch.matrix_world.copy()) for ch in obj.children]
            obj.location = world_before
            context.view_layer.update()
            for ch, ch_mat in children_world:
                ch.matrix_world = ch_mat

        obj["reskate_custom_origin"] = True
        if was_edit and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, "ReSkate: Origin snapped to selected geometry / 3D Cursor (Custom Origin locked).")
        return {'FINISHED'}


class RESKATE_OT_toggle_interactive_origin_edit(bpy.types.Operator):
    """Toggles Blender's interactive Origin Transform mode with the Move gizmo so you can drag the origin point directly"""
    bl_idname = "reskate.toggle_interactive_origin_edit"
    bl_label = "Move Origin Point (Interactive Gizmo)"
    bl_description = "Toggles Blender's 'Affect Only Origins' mode and activates the Move gizmo so you can slide the origin point directly in the viewport (click again when done)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        ts = getattr(context.scene, "tool_settings", None)
        if ts is None or not hasattr(ts, "use_transform_data_origin"):
            self.report({'WARNING'}, "ReSkate: Origin transform setting not available in this Blender context.")
            return {'CANCELLED'}

        target_obj = get_target_mesh_object(context) or context.active_object
        if target_obj and getattr(target_obj, "type", None) == 'MESH':
            target_obj["reskate_custom_origin"] = True

        new_state = not bool(ts.use_transform_data_origin)
        ts.use_transform_data_origin = new_state

        if new_state:
            try:
                bpy.ops.wm.tool_set_by_id(name="builtin.move")
            except Exception:
                pass
            compiler_bridge.invalidate_ui_caches()
            self.report({'INFO'}, "ReSkate: Interactive Origin Move ENABLED (Custom Origin locked) — drag the Move arrows to reposition the origin, then click 'Done Moving Origin'.")
        else:
            compiler_bridge.invalidate_ui_caches()
            gpu_draw.invalidate_gpu_caches()
            self.report({'INFO'}, "ReSkate: Interactive Origin Move FINISHED — Custom Origin locked & normal object movement restored.")
        return {'FINISHED'}


class RESKATE_OT_clear_all_overlays(bpy.types.Operator):
    """Turns off all ReSkate GPU overlays and hides any helper wireframes so viewport editing is completely unobstructed"""
    bl_idname = "reskate.clear_all_overlays"
    bl_label = "Turn Off All Overlays"
    bl_description = "Turns off all ReSkate GPU overlays (Grind Edges, Skate Collision, Placement Proxy, Face Surfaces, Lights) for clean mesh editing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if prop is not None:
            for attr in (
                "draw_grind_overlay",
                "show_grind_floating_badge",
                "draw_col_overlay",
                "show_collision_overlay",
                "show_placement_proxy_overlay",
                "draw_face_surface_overlay",
                "show_alignment_overlay",
                "draw_light_overlay",
                "show_round_rail_diameter_guide",
            ):
                if hasattr(prop, attr):
                    try:
                        setattr(prop, attr, False)
                    except Exception:
                        pass
        try:
            context.scene.reskate_show_col_overlay = False
        except Exception:
            pass
        gpu_draw.invalidate_gpu_caches()
        gpu_draw.sync_draw_handlers(context)
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        self.report({'INFO'}, "ReSkate: All viewport overlays turned off.")
        return {'FINISHED'}


class RESKATE_OT_run_preflight_check(bpy.types.Operator):
    """Runs the on-demand Pre-Flight Check on the active (and selected) prop mesh and caches the results for Page 6"""
    bl_idname = "reskate.run_preflight_check"
    bl_label = "Run Pre-Flight Check"
    bl_description = "Runs the Pre-Flight Checklist on your selected prop(s) on-demand without slowing down normal viewport navigation"
    bl_options = {'REGISTER'}

    def execute(self, context):
        targets = compiler_bridge.get_selected_prop_meshes(context)
        act_obj = get_target_mesh_object(context) or context.active_object
        if act_obj and getattr(act_obj, "type", None) == 'MESH' and act_obj not in targets:
            targets = [act_obj] + list(targets)
        if not targets:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}

        total_warns = 0
        total_errs = 0
        for o in targets:
            checks = compiler_bridge.run_preflight_checks(context, o)
            try:
                o["_reskate_last_preflight_json"] = json.dumps(checks)
            except Exception:
                pass
            if o == (act_obj or targets[0]):
                total_warns = sum(1 for c in checks if c.get("level") == "WARN")
                total_errs = sum(1 for c in checks if c.get("level") == "ERROR")

        compiler_bridge.invalidate_ui_caches()
        primary_name = (act_obj or targets[0]).name
        if total_errs > 0 or total_warns > 0:
            self.report(
                {'WARNING'},
                f"ReSkate Pre-Flight ('{primary_name}'): {total_errs} error(s), {total_warns} warning(s).",
            )
        else:
            self.report({'INFO'}, f"ReSkate Pre-Flight ('{primary_name}'): All checks passed!")
        return {'FINISHED'}


class RESKATE_OT_preflight_fix_all(bpy.types.Operator):
    """Automatically fixes all non-destructive Pre-Flight warnings on the active prop in one click (never alters UVs or textures)"""
    bl_idname = "reskate.preflight_fix_all"
    bl_label = "Fix All Pre-Flight Issues"
    bl_description = "Applies safe automatic Pre-Flight fixes (Scale/Rotation, Origin to Bottom, Triangulate N-gons) in one click without modifying or repacking UV maps"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}

        checks = compiler_bridge.run_preflight_checks(context, obj)
        fixed = 0
        for chk in checks:
            act = str(chk.get("fix_action") or "").upper()
            if not act or chk.get("level") == "OK":
                continue
            # Never auto-unwrap or repack UVs in Fix All so existing textures are never ruined
            if act in ("SMART_UV_PROJECT", "AUTO_UNWRAP_UV", "AUTO_UNWRAP_UVS", "PACK_UV_BOUNDS"):
                continue
            try:
                if act == "GENERATE_SIMPLIFIED_COL":
                    bpy.ops.reskate.create_simplified_col_copy()
                else:
                    bpy.ops.reskate.preflight_fix(fix_action=act)
                fixed += 1
            except Exception:
                pass

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        try:
            updated_checks = compiler_bridge.run_preflight_checks(context, obj)
            obj["_reskate_last_preflight_json"] = json.dumps(updated_checks)
        except Exception:
            pass
        self.report({'INFO'}, f"ReSkate: Applied {fixed} safe Pre-Flight fix(es) on '{obj.name}' (UV maps preserved).")
        return {'FINISHED'}


class RESKATE_OT_preview_material_textures_popup(bpy.types.Operator):
    """Opens a large Pop-Out Material & Texture Preview window for the active shader"""
    bl_idname = "reskate.preview_material_textures_popup"
    bl_label = "Shader & Texture Preview"
    bl_description = "Opens a larger pop-out window showing the active Blender material preview ball and all 4 PBR texture maps (Base Color, Normal, Roughness, Metallic)"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=480)

    def draw(self, context):
        layout = self.layout
        obj = get_target_mesh_object(context) or context.active_object
        mat = obj.active_material if (obj and obj.type == 'MESH') else None
        if mat is None and obj and obj.type == 'MESH' and len(obj.material_slots) > 0:
            mat = obj.material_slots[0].material

        if mat is None:
            layout.label(text="No active material on the selected prop.", icon='INFO')
            return

        box = layout.box()
        box.label(text=f"Shader Preview: {mat.name}", icon='MATERIAL')
        try:
            box.template_preview(mat, show_buttons=False)
        except Exception:
            pass

        sk8 = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
        s_info = compiler_bridge.inspect_material_shader_nodes(mat)
        slots = [
            ("Base Color", (getattr(sk8, "base_color_image", None) if sk8 else None) or s_info.get("base_color_image")),
            ("Normal Map", (getattr(sk8, "normal_image", None) if sk8 else None) or s_info.get("normal_image")),
            ("Roughness", (getattr(sk8, "roughness_image", None) if sk8 else None) or s_info.get("roughness_image")),
            ("Metallic", (getattr(sk8, "metallic_image", None) if sk8 else None) or s_info.get("metallic_image")),
        ]

        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=False, align=True)
        for lbl, img in slots:
            cell = grid.box()
            if img is not None:
                try:
                    img.preview_ensure()
                except Exception:
                    pass
                cell.label(text=f"✅ {lbl}", icon='IMAGE_DATA')
                if getattr(img, "preview", None) and getattr(img.preview, "icon_id", 0):
                    r_ic = cell.row()
                    r_ic.alignment = 'CENTER'
                    r_ic.template_icon(icon_value=img.preview.icon_id, scale=6.0)
                w_px = int(img.size[0]) if len(img.size) > 0 else 0
                h_px = int(img.size[1]) if len(img.size) > 1 else 0
                cell.label(text=f"{img.name} ({w_px}×{h_px})")
            else:
                cell.alert = True
                cell.label(text=f"⚠️ {lbl}: Missing / Flat Value", icon='ERROR')

    def execute(self, context):
        return {'FINISHED'}


class RESKATE_OT_open_reference_project(bpy.types.Operator):
    """Opens or reveals the ReSkate Reference Project (.blend) demonstrating what works vs. what doesn't"""
    bl_idname = "reskate.open_reference_project"
    bl_label = "Open ReSkate Reference Project (.blend)"
    bl_description = "Opens the ReSkate Reference .blend Project showing real-world scale, lip height limits, grind rails, decals, and collision examples"
    bl_options = {'REGISTER'}

    def execute(self, context):
        addon_dir = Path(__file__).resolve().parent
        studio_dir = compiler_bridge.get_studio_dir(context)
        candidates = [
            studio_dir / "Reference_Projects" / "ReSkate_Reference_Project.blend",
            studio_dir / "ReSkate_Reference_Project.blend",
            addon_dir / "ReSkate_Reference_Project.blend",
        ]
        found = next((p for p in candidates if p.is_file()), None)
        if found is not None:
            try:
                bpy.ops.wm.open_mainfile(filepath=str(found))
                return {'FINISHED'}
            except Exception as exc:
                self.report({'WARNING'}, f"ReSkate: Could not open reference project ({exc}).")
                return {'CANCELLED'}
        ref_folder = studio_dir / "Reference_Projects"
        try:
            ref_folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.report(
            {'INFO'},
            f"ReSkate: Save your Reference Project to '{ref_folder / 'ReSkate_Reference_Project.blend'}' to open it with 1 click!",
        )
        return {'FINISHED'}


class RESKATE_OT_place_on_ground_world_center(bpy.types.Operator):
    """Moves the active object to world (0, 0, 0) resting flush on the ground"""
    bl_idname = "reskate.place_on_ground_world_center"
    bl_label = "Place on Ground at World Center"
    bl_description = "Moves the object to world center (0, 0, 0) with its bottom sitting flush on Z = 0"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        if len(obj.data.vertices) == 0:
            self.report({'WARNING'}, "ReSkate: Active mesh has no vertices.")
            return {'CANCELLED'}

        # First set origin to Bottom Center, then place object at world (0,0,0)
        bpy.ops.reskate.set_origin(mode='BOTTOM_CENTER')
        obj.location = Vector((0.0, 0.0, 0.0))
        context.view_layer.update()
        gpu_draw.invalidate_gpu_caches()

        self.report({'INFO'}, "ReSkate: Object placed on ground (Z=0) at world center (0,0,0).")
        return {'FINISHED'}


class RESKATE_OT_snap_ground_center(bpy.types.Operator):
    """Legacy operator alias: snaps origin to Bottom Center and moves object to world (0, 0, 0)"""
    bl_idname = "reskate.snap_ground_center"
    bl_label = "Place on Ground at World Center"
    bl_description = "Sets origin to bottom center and moves the object to world (0, 0, 0)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.place_on_ground_world_center()


class RESKATE_OT_center_origin(bpy.types.Operator):
    """Centers object origin in XY without altering its current Z height"""
    bl_idname = "reskate.center_origin"
    bl_label = "Center Origin (XY Only)"
    bl_description = "Centers object origin horizontally in XY while preserving its vertical Z height and scene position"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = context.active_object
        if not obj or obj.type != 'MESH' or len(obj.data.vertices) == 0:
            self.report({'WARNING'}, "ReSkate: Please select a valid mesh object first.")
            return {'CANCELLED'}

        bbox_local = [Vector(c) for c in obj.bound_box]
        center_x = (min(v.x for v in bbox_local) + max(v.x for v in bbox_local)) / 2.0
        center_y = (min(v.y for v in bbox_local) + max(v.y for v in bbox_local)) / 2.0
        local_offset = Vector((center_x, center_y, 0.0))

        for vert in obj.data.vertices:
            vert.co.x -= local_offset.x
            vert.co.y -= local_offset.y
        obj.data.update()

        world_shift = obj.matrix_world.to_3x3() @ local_offset
        obj.location += world_shift
        context.view_layer.update()
        gpu_draw.invalidate_gpu_caches()

        self.report({'INFO'}, "ReSkate: Origin centered in XY.")
        return {'FINISHED'}


def _resolve_scale_ref_spawn_location(context, step_x: float = 0.55) -> tuple[Vector, Vector]:
    """
    Resolves `(spawn_loc, base_origin)` for a scale reference mesh:
    defaults to the 3D Cursor's location (`context.scene.cursor.location`) instead of the
    selected object's origin. If another scale reference is already sitting at that exact spot,
    steps along +X so multiple spawns sit neatly side-by-side and can be freely moved/duplicated.
    """
    if getattr(context, "scene", None) and getattr(context.scene, "cursor", None):
        base_loc = Vector(context.scene.cursor.location).copy()
    else:
        base_loc = Vector((0.0, 0.0, 0.0))

    existing_refs = [
        o for o in context.scene.objects
        if bool(o.get("reskate_is_scale_reference", False))
        or o.name.startswith(("ReSkate_Skater_Scale_Ref", "ReSkate_Skateboard_Scale_Ref", "ReSkate_RoundRail_Scale_Ref"))
    ]
    cand = base_loc.copy()
    offset_idx = 0
    while any((o.location - cand).length < 0.08 for o in existing_refs) and offset_idx < 32:
        offset_idx += 1
        cand = Vector((base_loc.x + step_x * offset_idx, base_loc.y, base_loc.z))
    return cand, base_loc


def _align_mesh_lowest_z_to_zero(mesh: bpy.types.Mesh) -> float:
    """Shifts local mesh vertices vertically so the lowest vertex sits at local Z = 0.0 (flush with object origin Z)."""
    if mesh is None or len(mesh.vertices) == 0:
        return 0.0
    min_z = min(float(v.co.z) for v in mesh.vertices)
    if abs(min_z) > 1e-7:
        for v in mesh.vertices:
            v.co.z -= min_z
        mesh.update()
    return min_z


class RESKATE_OT_toggle_skater_ref(bpy.types.Operator):
    """Spawns a selectable, duplicatable 1.812 m (5'11.3") in-game Skate. player character 3D reference model at the 3D Cursor"""
    bl_idname = "reskate.toggle_skater_ref"
    bl_label = "Spawn Skater"
    bl_description = (
        "Spawns the exact 1.8122 m (5'11.3\") in-game Skate. player character 3D reference mesh at the 3D Cursor "
        "with feet flush on the cursor's Z plane (scale locked, selectable, movable, and duplicatable with Shift+D)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        base_name = "ReSkate_Skater_Scale_Ref"
        spawn_loc, base_origin = _resolve_scale_ref_spawn_location(context, step_x=0.65)

        verts, faces = scale_refs.get_skater_mesh_pydata()
        mesh = bpy.data.meshes.new(f"{base_name}_Mesh")
        mesh.from_pydata(verts, [], faces)
        for poly in mesh.polygons:
            poly.use_smooth = True
        mesh.update()
        _align_mesh_lowest_z_to_zero(mesh)

        ref_obj = bpy.data.objects.new(base_name, mesh)
        context.collection.objects.link(ref_obj)
        ref_obj.location = spawn_loc
        ref_obj.lock_scale = (True, True, True)
        ref_obj.display_type = 'SOLID'
        ref_obj.show_wire = True
        ref_obj.color = (0.22, 0.78, 0.98, 0.65)
        ref_obj.hide_render = True
        ref_obj.hide_select = False
        ref_obj["reskate_is_scale_reference"] = True
        ref_obj["reskate_spawn_origin"] = [float(base_origin.x), float(base_origin.y), float(base_origin.z)]
        ref_obj["reskate_standing_height_m"] = scale_refs.PLAYER_STANDING_HEIGHT_M
        ref_obj["reskate_bare_foot_sole_m"] = scale_refs.PLAYER_BARE_FOOT_SOLE_M
        ref_obj["reskate_eye_height_m"] = scale_refs.PLAYER_EYE_HEIGHT_M
        ref_obj["reskate_shoulder_width_m"] = scale_refs.PLAYER_SHOULDER_WIDTH_M
        ref_obj["reskate_hip_width_m"] = scale_refs.PLAYER_HIP_WIDTH_M
        ref_obj["reskate_apose_span_m"] = scale_refs.PLAYER_APOSE_SPAN_M
        ref_obj["reskate_on_board_height_m"] = scale_refs.PLAYER_ON_BOARD_HEIGHT_M
        ref_obj["reskate_source_mesh"] = (
            "characters/maincharacters/generic/cas/head/headblends/rsp/cas_rsp_head_base_mesh + "
            "cas_rsp_body_base_mesh + adidas_sneaker_superstarpro_mesh"
        )

        for o in context.selected_objects:
            o.select_set(False)
        ref_obj.select_set(True)
        context.view_layer.objects.active = ref_obj
        context.view_layer.update()

        self.report(
            {'INFO'},
            f"ReSkate: Spawned 1.812 m Skater reference '{ref_obj.name}' at 3D Cursor ({spawn_loc.x:.2f}, {spawn_loc.y:.2f}, {spawn_loc.z:.2f}) — feet flush on Z!",
        )
        return {'FINISHED'}


class RESKATE_OT_toggle_skateboard_ref(bpy.types.Operator):
    """Spawns a selectable, duplicatable in-game Skate. 3D skateboard reference model (0.8245 m × 0.2170 m × 0.1427 m) at the 3D Cursor"""
    bl_idname = "reskate.toggle_skateboard_ref"
    bl_label = "Spawn Skateboard"
    bl_description = (
        "Spawns the exact in-game Skate. 3D skateboard reference mesh (0.8245 m × 0.2170 m × 0.1427 m) at the 3D Cursor "
        "with wheels flush on the cursor's Z plane (scale locked, selectable, movable, and duplicatable with Shift+D)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        base_name = "ReSkate_Skateboard_Scale_Ref"
        spawn_loc, base_origin = _resolve_scale_ref_spawn_location(context, step_x=0.35)

        verts, faces = scale_refs.get_skateboard_mesh_pydata()
        mesh = bpy.data.meshes.new(f"{base_name}_Mesh")
        mesh.from_pydata(verts, [], faces)
        for poly in mesh.polygons:
            poly.use_smooth = True
        mesh.update()
        _align_mesh_lowest_z_to_zero(mesh)

        ref_obj = bpy.data.objects.new(base_name, mesh)
        context.collection.objects.link(ref_obj)
        ref_obj.location = spawn_loc
        ref_obj.lock_scale = (True, True, True)
        ref_obj.display_type = 'SOLID'
        ref_obj.show_wire = True
        ref_obj.color = (0.98, 0.68, 0.22, 0.75)
        ref_obj.hide_render = True
        ref_obj.hide_select = False
        ref_obj["reskate_is_scale_reference"] = True
        ref_obj["reskate_spawn_origin"] = [float(base_origin.x), float(base_origin.y), float(base_origin.z)]
        ref_obj["reskate_deck_length_m"] = scale_refs.SKATEBOARD_DECK_LENGTH_M
        ref_obj["reskate_deck_width_m"] = scale_refs.SKATEBOARD_DECK_WIDTH_M
        ref_obj["reskate_truck_axle_width_m"] = scale_refs.SKATEBOARD_TRUCK_AXLE_WIDTH_M
        ref_obj["reskate_wheel_diameter_m"] = scale_refs.SKATEBOARD_WHEEL_DIAMETER_M
        ref_obj["reskate_wheelbase_outer_m"] = scale_refs.SKATEBOARD_WHEELBASE_OUTER_M
        ref_obj["reskate_deck_grip_height_m"] = scale_refs.SKATEBOARD_DECK_GRIP_CENTER_M
        ref_obj["reskate_tail_height_m"] = scale_refs.SKATEBOARD_TAIL_HEIGHT_M
        ref_obj["reskate_nose_height_m"] = scale_refs.SKATEBOARD_NOSE_HEIGHT_M
        ref_obj["reskate_source_mesh"] = (
            "characters/skateboard/unlicensed/deck/generic/popsicle/2022/deck_gen_popsicle_mesh + "
            "truck_gen_default_mesh + wheel_gen_classic_mesh"
        )

        for o in context.selected_objects:
            o.select_set(False)
        ref_obj.select_set(True)
        context.view_layer.objects.active = ref_obj
        context.view_layer.update()

        self.report(
            {'INFO'},
            f"ReSkate: Spawned 0.825 m Skateboard reference '{ref_obj.name}' at 3D Cursor ({spawn_loc.x:.2f}, {spawn_loc.y:.2f}, {spawn_loc.z:.2f}) — wheels flush on Z!",
        )
        return {'FINISHED'}


class RESKATE_OT_toggle_ground_plane_ref(bpy.types.Operator):
    """Spawns or removes a 50m x 50m scalable flat gray ground plane at world Z = 0.0 for scale & shadow reference"""
    bl_idname = "reskate.toggle_ground_plane_ref"
    bl_label = "Spawn Ground Plane"
    bl_description = (
        "Spawns or removes a 50 m × 50 m flat gray ground plane at world Z = 0.0 "
        "(freely scalable with S; ignored by prop compilation)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    size_m: FloatProperty(
        name="Plane Size (m)",
        default=50.0,
        min=1.0,
        max=1000.0,
        unit='LENGTH',
        description="Initial width and depth of the reference ground plane in meters",
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        base_name = "ReSkate_Ground_Plane_Ref"
        existing = context.scene.objects.get(base_name) or bpy.data.objects.get(base_name)
        if existing is not None:
            m_data = existing.data if existing.type == 'MESH' else None
            try:
                bpy.data.objects.remove(existing, do_unlink=True)
            except Exception:
                pass
            if m_data is not None and m_data.users == 0:
                try:
                    bpy.data.meshes.remove(m_data)
                except Exception:
                    pass
            target = get_target_mesh_object(context)
            if target is not None and target.name in context.scene.objects:
                target.select_set(True)
                context.view_layer.objects.active = target
            self.report({'INFO'}, "ReSkate: Removed reference Ground Plane.")
            return {'FINISHED'}

        half_sz = max(0.5, float(self.size_m) * 0.5)
        verts = [
            (-half_sz, -half_sz, 0.0),
            (half_sz, -half_sz, 0.0),
            (half_sz, half_sz, 0.0),
            (-half_sz, half_sz, 0.0),
        ]
        faces = [(0, 1, 2, 3)]
        mesh = bpy.data.meshes.new(f"{base_name}_Mesh")
        mesh.from_pydata(verts, [], faces)
        mesh.update()

        mat_name = "ReSkate_Ground_Ref_Mat"
        g_mat = bpy.data.materials.get(mat_name)
        if g_mat is None:
            g_mat = bpy.data.materials.new(mat_name)
            g_mat.use_nodes = True
        if g_mat.use_nodes and g_mat.node_tree:
            p_bsdf = next((n for n in g_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if p_bsdf is not None:
                if "Base Color" in p_bsdf.inputs:
                    p_bsdf.inputs["Base Color"].default_value = (0.35, 0.35, 0.37, 1.0)
                if "Roughness" in p_bsdf.inputs:
                    p_bsdf.inputs["Roughness"].default_value = 0.88
                if "Metallic" in p_bsdf.inputs:
                    p_bsdf.inputs["Metallic"].default_value = 0.0
                if "Specular IOR Level" in p_bsdf.inputs:
                    p_bsdf.inputs["Specular IOR Level"].default_value = 0.20
        g_mat.diffuse_color = (0.35, 0.35, 0.37, 1.0)
        g_mat.roughness = 0.88
        g_mat.metallic = 0.0
        mesh.materials.append(g_mat)

        ref_obj = bpy.data.objects.new(base_name, mesh)
        context.collection.objects.link(ref_obj)
        ref_obj.location = Vector((0.0, 0.0, 0.0))
        ref_obj.lock_scale = (False, False, False)
        ref_obj.display_type = 'SOLID'
        ref_obj.color = (0.35, 0.35, 0.37, 1.0)
        ref_obj.hide_render = True
        ref_obj.hide_select = False
        ref_obj["reskate_is_scale_reference"] = True
        ref_obj["reskate_is_ref_helper"] = True

        for o in context.selected_objects:
            o.select_set(False)
        ref_obj.select_set(True)
        context.view_layer.objects.active = ref_obj
        context.view_layer.update()

        self.report(
            {'INFO'},
            f"ReSkate: Spawned {self.size_m:.0f}m × {self.size_m:.0f}m Ground Plane at Z = 0.0 — press S to scale freely!",
        )
        return {'FINISHED'}


class RESKATE_OT_clear_scale_references(bpy.types.Operator):
    """Removes all spawned in-game Scale Reference meshes (Skater, Skateboard, Ground Plane, Round Rail) from the scene"""
    bl_idname = "reskate.clear_scale_references"
    bl_label = "Remove All"
    bl_description = "Deletes all spawned Skater, Skateboard, and Ground Plane scale reference helper objects from the scene"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        to_remove = [
            o for o in list(context.scene.objects)
            if bool(o.get("reskate_is_scale_reference", False))
            or o.name.startswith((
                "ReSkate_Skater_Scale_Ref",
                "ReSkate_Skateboard_Scale_Ref",
                "ReSkate_RoundRail_Scale_Ref",
                "ReSkate_Ground_Plane_Ref",
            ))
        ]
        removed = 0
        for o in to_remove:
            m_data = o.data if o.type == 'MESH' else None
            try:
                bpy.data.objects.remove(o, do_unlink=True)
                removed += 1
            except Exception:
                pass
            if m_data is not None and m_data.users == 0:
                try:
                    bpy.data.meshes.remove(m_data)
                except Exception:
                    pass

        target = get_target_mesh_object(context)
        if target is not None and target.name in context.scene.objects:
            target.select_set(True)
            context.view_layer.objects.active = target

        self.report({'INFO'}, f"ReSkate: Removed {removed} scale reference object(s) from the scene.")
        return {'FINISHED'}


class RESKATE_OT_toggle_round_rail_ref(bpy.types.Operator):
    """Spawns or toggles visibility of the recommended Skate. Round Rail Cylinder Diameter Reference (12-Sided Cylinder, Ø 6.4 cm / 2.5 in, r = 3.2 cm, height = 0.45 m) at the 3D Cursor"""
    bl_idname = "reskate.toggle_round_rail_ref"
    bl_label = "Round Rail Pipe (Ø 6.4 cm / 12-Sided)"
    bl_description = (
        "Toggles a 3D reference 12-sided Round Rail pipe (recommended Ø 6.4 cm / 2.5 in diameter, r = 3.2 cm, "
        "standard 0.45 m street rail height × 2.0 m length) at the 3D Cursor with posts flush on the cursor's Z plane"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        ref_name = "ReSkate_RoundRail_Scale_Ref"
        ref_obj = context.scene.objects.get(ref_name)
        if ref_obj and ref_obj.type == 'MESH' and len(ref_obj.data.vertices) >= 16 and int(ref_obj.get("reskate_rail_sides", 0)) == 12:
            ref_obj.hide_viewport = not ref_obj.hide_viewport
            state = "hidden" if ref_obj.hide_viewport else "visible"
            self.report({'INFO'}, f"ReSkate: 12-Sided Round Rail reference (Ø 6.4 cm / 2.5 in, r = 3.2 cm — vanilla rails use 8–12 sides) is now {state}.")
            return {'FINISHED'}

        if ref_obj:
            try:
                bpy.data.objects.remove(ref_obj, do_unlink=True)
            except Exception:
                pass

        prop = getattr(context.scene, "reskate_prop", None)
        rad = float(getattr(prop, "grind_tube_radius", 0.032) or 0.032) if prop else 0.032
        diam_cm = rad * 200.0
        rail_len = 2.0
        rail_top_z = 0.45
        cyl_center_z = rail_top_z - rad
        spawn_loc, base_origin = _resolve_scale_ref_spawn_location(context, step_x=0.65)

        bm = bmesh.new()
        # Horizontal 12-sided round rail pipe along Y axis at Z = cyl_center_z + two 8-sided vertical support posts down to Z = 0
        rot_x90 = Matrix.Rotation(math.radians(90.0), 4, 'X')
        bmesh.ops.create_cone(
            bm,
            cap_ends=True,
            cap_tris=True,
            segments=12,
            radius1=rad,
            radius2=rad,
            depth=rail_len,
            matrix=Matrix.Translation((0.0, 0.0, cyl_center_z)) @ rot_x90,
        )
        post_h = max(0.05, cyl_center_z)
        post_r = max(0.014, rad * 0.72)
        for y_off in (-rail_len * 0.38, rail_len * 0.38):
            bmesh.ops.create_cone(
                bm,
                cap_ends=True,
                cap_tris=True,
                segments=8,
                radius1=post_r,
                radius2=post_r,
                depth=post_h,
                matrix=Matrix.Translation((0.0, y_off, post_h * 0.5)),
            )
        bm.normal_update()

        mesh = bpy.data.meshes.new(f"{ref_name}_Mesh")
        bm.to_mesh(mesh)
        bm.free()
        for poly in mesh.polygons:
            poly.use_smooth = True
        mesh.update()
        _align_mesh_lowest_z_to_zero(mesh)

        ref_obj = bpy.data.objects.new(ref_name, mesh)
        context.collection.objects.link(ref_obj)
        ref_obj.location = spawn_loc
        ref_obj.lock_scale = (True, True, True)
        ref_obj.display_type = 'SOLID'
        ref_obj.show_wire = True
        ref_obj.color = (0.05, 0.88, 1.00, 0.72)
        ref_obj.hide_render = True
        ref_obj.hide_select = False
        ref_obj["reskate_is_scale_reference"] = True
        ref_obj["reskate_spawn_origin"] = [float(base_origin.x), float(base_origin.y), float(base_origin.z)]
        ref_obj["reskate_rail_sides"] = 12
        ref_obj["reskate_recommended_diameter_m"] = round(rad * 2.0, 4)
        ref_obj["reskate_recommended_radius_m"] = round(rad, 4)

        for o in context.selected_objects:
            o.select_set(False)
        ref_obj.select_set(True)
        context.view_layer.objects.active = ref_obj
        context.view_layer.update()

        self.report(
            {'INFO'},
            f"ReSkate: Spawned 12-sided Round Rail reference at 3D Cursor ({spawn_loc.x:.2f}, {spawn_loc.y:.2f}, {spawn_loc.z:.2f}) (Ø {diam_cm:.1f} cm / {diam_cm / 2.54:.2f}\", r = {rad * 100.0:.1f} cm).",
        )
        return {'FINISHED'}


class RESKATE_OT_toggle_scale_reference(bpy.types.Operator):
    """Spawns a scale reference mesh (Skater, Skateboard, Round Rail Pipe, or Ground Plane) at the 3D Cursor with its lowest point flush on the cursor's Z plane"""
    bl_idname = "reskate.toggle_scale_reference"
    bl_label = "Spawn Scale Reference"
    bl_description = "Spawns the requested scale reference at the 3D Cursor with its lowest point (feet/wheels/posts) flush on the cursor's Z plane"
    bl_options = {'REGISTER', 'UNDO'}

    ref_type: StringProperty(
        name="Reference Type",
        default="SKATER",
        description="Scale reference to spawn: SKATER, SKATEBOARD, PIPE / ROUND_RAIL, or GROUND_PLANE",
    )

    def execute(self, context):
        rt = (self.ref_type or "SKATER").strip().upper()
        if rt in ("SKATEBOARD", "BOARD"):
            return bpy.ops.reskate.toggle_skateboard_ref()
        if rt in ("PIPE", "ROUND_RAIL", "RAIL"):
            return bpy.ops.reskate.toggle_round_rail_ref()
        if rt in ("GROUND", "GROUND_PLANE"):
            return bpy.ops.reskate.toggle_ground_plane_ref()
        return bpy.ops.reskate.toggle_skater_ref()


# ==============================================================================
# Step 2: Collision Operators
# ==============================================================================

class RESKATE_OT_toggle_col_preview(bpy.types.Operator):
    """Toggles collision overlay visualization in the 3D viewport"""
    bl_idname = "reskate.toggle_col_preview"
    bl_label = "Preview Collision"
    bl_description = "Toggles wireframe visualization of the active collision geometry in the 3D viewport"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            prop.draw_col_overlay = not prop.draw_col_overlay
            state = "ON" if prop.draw_col_overlay else "OFF"

            obj = get_target_mesh_object(context) or context.active_object
            if obj:
                col_obj = compiler_bridge.resolve_custom_collision_object(context, obj)
                if col_obj:
                    col_obj.hide_viewport = not prop.draw_col_overlay
                    col_obj.display_type = 'WIRE'
                    col_obj.show_wire = True

            self.report({'INFO'}, f"ReSkate: Collision preview turned {state}.")
        return {'FINISHED'}


class RESKATE_OT_generate_authored_col(bpy.types.Operator):
    """Creates a starter copy of the active mesh and assigns it to the Custom Collision Object picker"""
    bl_idname = "reskate.generate_authored_col"
    bl_label = "Create Starter Copy"
    bl_description = "Duplicates the visible mesh into a wireframe collision object and assigns it to the Collision Object picker"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        col_name = f"{obj.name}_COL"
        col_obj = context.scene.objects.get(col_name)

        if not col_obj:
            col_mesh = obj.data.copy()
            col_mesh.name = f"{obj.name}_COL_Mesh"
            col_obj = bpy.data.objects.new(col_name, col_mesh)
            context.collection.objects.link(col_obj)
            col_obj.matrix_world = obj.matrix_world.copy()
            col_obj.data.materials.clear()
            col_obj.display_type = 'WIRE'
            col_obj.show_wire = True
            col_obj.show_all_edges = True
            col_obj.hide_render = True
            col_obj.hide_viewport = False
            col_obj.hide_select = False
            try:
                col_obj.hide_set(False)
            except Exception:
                pass

            if hasattr(col_obj, "reskate_col_prop"):
                col_prop = col_obj.reskate_col_prop
                col_prop.is_col_proxy = True
                if prop:
                    col_prop.custom_surface_code = prop.surface_material_code
                    col_prop.round_rail = prop.round_rail
                    col_prop.trim_bottom = prop.trim_bottom

            col_obj["reskate_col_owner"] = obj.name
            col_obj.parent = obj
            try:
                parent_inv = obj.matrix_world.inverted()
            except Exception:
                parent_inv = Matrix.Identity(4)
            col_obj.matrix_parent_inverse = parent_inv
        else:
            col_obj.hide_viewport = False
            col_obj.hide_select = False
            try:
                col_obj.hide_set(False)
            except Exception:
                pass

        if hasattr(obj, "reskate_custom_collision_object"):
            obj.reskate_custom_collision_object = col_obj
        if prop:
            prop.collision_mode = 'CUSTOM_OBJECT'
            prop.custom_collision_object = col_obj

        context.view_layer.objects.active = obj
        obj.select_set(True)
        col_obj.select_set(False)

        self.report({'INFO'}, f"ReSkate: Created starter collision copy '{col_obj.name}' and assigned to picker.")
        return {'FINISHED'}


class RESKATE_OT_create_simplified_col_copy(bpy.types.Operator):
    """Duplicates the visible mesh, decimates it to the target triangle budget, and assigns it to Custom Collision Object"""
    bl_idname = "reskate.create_simplified_col_copy"
    bl_label = "Create Simplified Copy"
    bl_description = "Creates a simplified duplicate of the visible mesh (~1,500 tris) and sets Collision Mode to Custom Collision Object"
    bl_options = {'REGISTER', 'UNDO'}

    target_tris: IntProperty(
        name="Target Triangles", default=1500, min=100, max=5000,
        description="Target triangle count for the simplified collision mesh",
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        bpy.ops.reskate.generate_authored_col()
        prop = getattr(context.scene, "reskate_prop", None)
        col_obj = prop.custom_collision_object if prop else context.scene.objects.get(f"{obj.name}_COL")
        if not col_obj or col_obj.type != 'MESH':
            return {'CANCELLED'}

        cur_tris = sum(len(p.vertices) - 2 for p in col_obj.data.polygons)
        if cur_tris > self.target_tris and cur_tris > 0:
            ratio = max(0.05, min(0.95, float(self.target_tris) / float(cur_tris)))
            bm = bmesh.new()
            bm.from_mesh(col_obj.data)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.to_mesh(col_obj.data)
            bm.free()

            prev_active = context.view_layer.objects.active
            context.view_layer.objects.active = col_obj
            dec_mod = col_obj.modifiers.new(name="ReSkate_Col_Simplify", type='DECIMATE')
            dec_mod.ratio = ratio
            try:
                bpy.ops.object.modifier_apply(modifier=dec_mod.name)
            except Exception:
                col_obj.modifiers.remove(dec_mod)
            context.view_layer.objects.active = prev_active

        new_tris = sum(len(p.vertices) - 2 for p in col_obj.data.polygons)
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Simplified collision mesh '{col_obj.name}' ready ({new_tris:,} tris).")
        return {'FINISHED'}


class RESKATE_OT_generate_collision_preview(bpy.types.Operator):
    """Generates a simplified collision copy (_COL) within Havok 1:1 limits (~1,500 tris) and enables collision preview"""
    bl_idname = "reskate.generate_collision_preview"
    bl_label = "Generate Simplified Collision (_COL)"
    bl_description = "Creates a simplified duplicate of the visible mesh (~1,500 tris), assigns it to Custom Collision Object, selects it, and turns on collision preview"
    bl_options = {'REGISTER', 'UNDO'}

    target_tris: IntProperty(
        name="Target Triangles", default=1500, min=100, max=5000,
        description="Target triangle count for the simplified collision mesh",
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        # 1. Create or retrieve starter authored collision copy
        bpy.ops.reskate.generate_authored_col()
        prop = getattr(context.scene, "reskate_prop", None)
        col_obj = prop.custom_collision_object if prop else context.scene.objects.get(f"{obj.name}_COL")
        if not col_obj or col_obj.type != 'MESH':
            self.report({'ERROR'}, "ReSkate: Failed to create collision object.")
            return {'CANCELLED'}

        # 2. Decimate if triangle count exceeds target budget
        cur_tris = sum(len(p.vertices) - 2 for p in col_obj.data.polygons)
        if cur_tris > self.target_tris and cur_tris > 0:
            ratio = max(0.05, min(0.95, float(self.target_tris) / float(cur_tris)))
            bm = bmesh.new()
            bm.from_mesh(col_obj.data)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.to_mesh(col_obj.data)
            bm.free()

            prev_active = context.view_layer.objects.active
            context.view_layer.objects.active = col_obj
            dec_mod = col_obj.modifiers.new(name="ReSkate_Col_Simplify", type='DECIMATE')
            dec_mod.ratio = ratio
            try:
                bpy.ops.object.modifier_apply(modifier=dec_mod.name)
            except Exception:
                col_obj.modifiers.remove(dec_mod)
            context.view_layer.objects.active = prev_active

        # 3. Turn on collision preview overlay and select the new _COL object
        if prop:
            prop.collision_mode = 'CUSTOM_OBJECT'
            prop.custom_collision_object = col_obj
            prop.draw_col_overlay = True

        obj_state = compiler_bridge.get_or_init_object_prop_state(obj)
        obj_state["collision_mode"] = "CUSTOM_OBJECT"
        obj_state["collision_mesh_source"] = "CUSTOM"
        obj_state["custom_collision_object_name"] = col_obj.name
        obj["reskate_prop_state"] = json.dumps(obj_state)
        obj["reskate_collision_mode"] = "CUSTOM_OBJECT"

        col_obj.hide_viewport = False
        col_obj.hide_select = False
        try:
            col_obj.hide_set(False)
        except Exception:
            pass
        col_obj.select_set(True)

        new_tris = sum(len(p.vertices) - 2 for p in col_obj.data.polygons)
        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Generated simplified collision mesh '{col_obj.name}' ({new_tris:,} tris) within Havok ceiling.")
        return {'FINISHED'}


class RESKATE_OT_generate_geometric_hull_col(bpy.types.Operator):
    """Generates a clean, flat-faced geometric collision mesh (<ObjName>_COL) in Simple Hulls mode that preserves sharp boxes, ledges, and ramps without rounding"""
    bl_idname = "reskate.generate_geometric_hull_col"
    bl_label = "Generate Geometric Hull Mesh"
    bl_description = "Builds a clean, flat-faced geometric collision mesh (<ObjName>_COL) that preserves sharp boxes, ledges, and ramps (never rounds boxes into blobs) and shows it in the viewport"
    bl_options = {'REGISTER', 'UNDO'}

    planar_angle_deg: FloatProperty(
        name="Planar Merge Angle (°)", default=8.0, min=1.0, max=35.0,
        description="Coplanar faces within this angle are merged into flat geometric planes while sharp creases and material boundaries are preserved",
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        col_name = f"{obj.name}_COL"
        old_col = context.scene.objects.get(col_name)
        if old_col:
            try:
                old_mesh = old_col.data
                bpy.data.objects.remove(old_col, do_unlink=True)
                if old_mesh and old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
            except Exception:
                pass

        col_mesh = obj.data.copy()
        col_mesh.name = f"{obj.name}_COL_Mesh"
        col_obj = bpy.data.objects.new(col_name, col_mesh)
        context.collection.objects.link(col_obj)
        col_obj.matrix_world = obj.matrix_world.copy()

        # Dissolve coplanar subdivisions while strictly preserving sharp box/ledge edges & material slots
        bm = bmesh.new()
        bm.from_mesh(col_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.0005)
        try:
            bmesh.ops.dissolve_limit(
                bm,
                angle_limit=math.radians(float(self.planar_angle_deg)),
                use_dissolve_boundaries=False,
                verts=bm.verts[:],
                edges=bm.edges[:],
                delimit={'MATERIAL', 'SHARP'},
            )
        except Exception:
            pass
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(col_obj.data)
        bm.free()
        col_obj.data.update()

        # If still above target triangle budget, apply planar/edge collapse while preserving sharp borders
        max_tris = int(getattr(prop, "max_col_tris", 1500)) if prop else 1500
        cur_tris = len(col_obj.data.polygons)
        if cur_tris > max_tris and cur_tris > 0:
            ratio = max(0.08, min(0.95, float(max_tris) / float(cur_tris)))
            prev_active = context.view_layer.objects.active
            context.view_layer.objects.active = col_obj
            dec_mod = col_obj.modifiers.new(name="ReSkate_GeoHull", type='DECIMATE')
            dec_mod.ratio = ratio
            try:
                bpy.ops.object.modifier_apply(modifier=dec_mod.name)
            except Exception:
                col_obj.modifiers.remove(dec_mod)
            context.view_layer.objects.active = prev_active

        col_obj.display_type = 'WIRE'
        col_obj.show_wire = True
        col_obj.show_all_edges = True
        col_obj.hide_render = True
        col_obj.hide_viewport = False
        col_obj.hide_select = False
        try:
            col_obj.hide_set(False)
        except Exception:
            pass

        if hasattr(col_obj, "reskate_col_prop"):
            col_prop = col_obj.reskate_col_prop
            col_prop.is_col_proxy = True
            if prop:
                col_prop.custom_surface_code = prop.surface_material_code
                col_prop.round_rail = prop.round_rail
                col_prop.trim_bottom = prop.trim_bottom

        col_obj["reskate_col_owner"] = obj.name
        col_obj.parent = obj
        try:
            parent_inv = obj.matrix_world.inverted()
        except Exception:
            parent_inv = Matrix.Identity(4)
        col_obj.matrix_parent_inverse = parent_inv

        if hasattr(obj, "reskate_custom_collision_object"):
            obj.reskate_custom_collision_object = col_obj
        if prop:
            if prop.collision_mode not in ('SIMPLE_HULLS', 'CONVEX_HULL', 'CUSTOM_OBJECT', 'AUTHORED_COL'):
                prop.collision_mode = 'SIMPLE_HULLS'
            prop.custom_collision_object = col_obj
            prop.draw_col_overlay = True

        context.view_layer.objects.active = obj
        obj.select_set(True)
        col_obj.select_set(False)
        gpu_draw.invalidate_gpu_caches()

        final_tris = sum(max(0, len(p.vertices) - 2) for p in col_obj.data.polygons)
        self.report(
            {'INFO'},
            f"ReSkate: Generated geometric hull mesh '{col_obj.name}' ({final_tris:,} flat tris, sharp edges preserved).",
        )
        return {'FINISHED'}


class RESKATE_OT_toggle_edit_collision_mesh(bpy.types.Operator):
    """Toggles between editing the prop's custom collision mesh (<Prop>_COL) in Edit Mode and returning to the visual prop"""
    bl_idname = "reskate.toggle_edit_collision_mesh"
    bl_label = "Edit Collision Mesh"
    bl_description = (
        "Isolates the visual prop to wireframe (locking its selection so you never click the prop underneath), "
        "selects the collision mesh (<Prop>_COL or Custom Collision Object), and enters Edit Mode "
        "(click again to finish editing and restore the visual prop)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _restore_visual_prop_isolation(vis_obj: bpy.types.Object | None):
        if not vis_obj or getattr(vis_obj, "type", None) != 'MESH':
            return
        try:
            vis_obj.hide_viewport = False
            vis_obj.hide_set(False)
        except Exception:
            pass
        if bool(vis_obj.get("reskate_col_edit_isolated", False)):
            saved_disp = str(vis_obj.get("reskate_saved_display_type", "TEXTURED") or "TEXTURED")
            saved_sel = bool(vis_obj.get("reskate_saved_hide_select", False))
            try:
                vis_obj.display_type = saved_disp
            except Exception:
                vis_obj.display_type = 'TEXTURED'
            try:
                vis_obj.hide_select = saved_sel
            except Exception:
                vis_obj.hide_select = False
            vis_obj["reskate_col_edit_isolated"] = False

    @staticmethod
    def _isolate_visual_prop_for_proxy_edit(vis_obj: bpy.types.Object, hide_completely: bool = False):
        if not vis_obj or getattr(vis_obj, "type", None) != 'MESH':
            return
        if not bool(vis_obj.get("reskate_col_edit_isolated", False)):
            vis_obj["reskate_saved_display_type"] = str(getattr(vis_obj, "display_type", "TEXTURED") or "TEXTURED")
            vis_obj["reskate_saved_hide_select"] = bool(getattr(vis_obj, "hide_select", False))
        vis_obj["reskate_col_edit_isolated"] = True
        try:
            vis_obj.hide_select = True
        except Exception:
            pass
        if hide_completely:
            try:
                vis_obj.hide_set(True)
            except Exception:
                vis_obj.display_type = 'WIRE'
        else:
            try:
                vis_obj.hide_set(False)
            except Exception:
                pass
            vis_obj.display_type = 'WIRE'

    def execute(self, context):
        act = context.active_object
        resolved_owner = resolve_collision_to_owner_prop(act, context.scene) if act else None
        owner_from_act = resolved_owner if (resolved_owner is not None and resolved_owner != act) else None
        is_col_active = bool(
            act and act.type == 'MESH' and (
                is_collision_helper_object(act)
                or owner_from_act is not None
                or bool(act.get("reskate_col_owner", ""))
            )
        )

        # If currently editing the _COL / _PPHYS / custom collision mesh -> exit Edit Mode and restore visual prop
        if is_col_active and (context.mode == 'EDIT_MESH' or (owner_from_act and bool(owner_from_act.get("reskate_col_edit_isolated", False)))):
            if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')
            vis_obj = (
                owner_from_act
                or (act.parent if (act.parent and act.parent.type == 'MESH') else None)
                or get_target_mesh_object(context)
            )
            act.display_type = 'WIRE'
            act.show_wire = True
            act.show_all_edges = True
            if vis_obj and vis_obj.name in context.scene.objects:
                self._restore_visual_prop_isolation(vis_obj)
                bpy.ops.object.select_all(action='DESELECT')
                vis_obj.select_set(True)
                context.view_layer.objects.active = vis_obj
            gpu_draw.invalidate_gpu_caches()
            self.report({'INFO'}, f"ReSkate: Finished editing '{act.name}' and restored visual prop '{vis_obj.name if vis_obj else ''}'.")
            return {'FINISHED'}

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        vis_obj = get_target_mesh_object(context) or context.active_object
        if not vis_obj or vis_obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D prop mesh first.")
            return {'CANCELLED'}

        col_obj = compiler_bridge.resolve_custom_collision_object(context, vis_obj)
        if not col_obj or col_obj == vis_obj:
            bpy.ops.reskate.generate_authored_col()
            col_obj = compiler_bridge.resolve_custom_collision_object(context, vis_obj)
        if not col_obj or col_obj.type != 'MESH' or col_obj == vis_obj:
            self.report({'WARNING'}, "ReSkate: Could not find or create a collision mesh for this prop.")
            return {'CANCELLED'}

        # Tag owner link on col_obj so resolve_collision_to_owner_prop always links back to vis_obj
        col_obj["reskate_col_owner"] = vis_obj.name
        if hasattr(col_obj, "reskate_col_prop"):
            try:
                col_obj.reskate_col_prop.is_col_proxy = True
            except Exception:
                pass

        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            prop.draw_col_overlay = True

        col_obj.hide_viewport = False
        col_obj.hide_select = False
        try:
            col_obj.hide_set(False)
        except Exception:
            pass
        col_obj.display_type = 'SOLID'
        col_obj.show_wire = True
        col_obj.show_all_edges = True

        bpy.ops.object.select_all(action='DESELECT')
        col_obj.select_set(True)
        context.view_layer.objects.active = col_obj
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        hide_vis = bool(getattr(prop, "hide_visual_during_col_edit", False)) if prop else False
        self._isolate_visual_prop_for_proxy_edit(vis_obj, hide_completely=hide_vis)

        gpu_draw.invalidate_gpu_caches()
        self.report(
            {'INFO'},
            f"ReSkate: Editing collision mesh '{col_obj.name}' — visual prop '{vis_obj.name}' is selection-locked in wireframe.",
        )
        return {'FINISHED'}


class RESKATE_OT_generate_placement_proxy(bpy.types.Operator):
    """Generates or edits a tight, hyper-simplified Placement Proxy mesh (<Prop>_PPHYS) for Quick Drop placement bounds"""
    bl_idname = "reskate.generate_placement_proxy"
    bl_label = "Generate Tight Placement Mesh (_PPHYS)"
    bl_description = (
        "Preserves disconnected parts (e.g. angled pole jam + baseplate or 4 gazebo legs + roof) inside a single "
        "<Prop>_PPHYS mesh object and hyper-simplifies/convex-hulls each part to tightly contour the prop for Quick Drop placement"
    )
    bl_options = {'REGISTER', 'UNDO'}

    enter_edit_mode: BoolProperty(
        name="Enter Edit Mode",
        default=False,
        description="When enabled, immediately enters Edit Mode on <Prop>_PPHYS and selection-locks the visual prop",
    )
    force_regenerate: BoolProperty(
        name="Force Regenerate",
        default=True,
        description="Rebuild the <Prop>_PPHYS mesh geometry from the visual prop using the current Target Placement Tris budget",
    )

    @staticmethod
    def _build_tight_pphys_bmesh(vis_obj: bpy.types.Object, target_tris: int = 64) -> bmesh.types.BMesh:
        """
        Builds a tight, multi-island hyper-simplified BMesh from `vis_obj` (excluding Decal Overlay / Collision-Only slots):
        1. Separates disconnected vertex/face islands (e.g. 4 gazebo legs + roof, or pole + baseplate).
        2. Computes the 3D convex hull of each island independently so empty space between legs/bars is never bridged.
        3. Dissolves nearly-coplanar faces so flat surfaces use minimal triangles.
        """
        decal_slots: set[int] = set()
        for idx, slot in enumerate(vis_obj.material_slots):
            mat = slot.material
            if mat:
                sk8 = getattr(mat, "sk8_material", None) or getattr(mat, "reskate_mat", None)
                if sk8 and (bool(getattr(sk8, "is_decal_overlay", False)) or str(getattr(sk8, "material_domain", "")).upper() == 'DECAL'):
                    decal_slots.add(idx)

        src_bm = bmesh.new()
        src_bm.from_mesh(vis_obj.data)
        src_bm.verts.ensure_lookup_table()
        src_bm.faces.ensure_lookup_table()

        if decal_slots and len(src_bm.faces) > 0:
            non_decal = [f for f in src_bm.faces if f.material_index not in decal_slots]
            if non_decal and len(non_decal) < len(src_bm.faces):
                del_f = [f for f in src_bm.faces if f.material_index in decal_slots]
                bmesh.ops.delete(src_bm, geom=del_f, context='FACES')
                lone_v = [v for v in src_bm.verts if not v.link_faces]
                if lone_v:
                    bmesh.ops.delete(src_bm, geom=lone_v, context='VERTS')

        src_bm.verts.ensure_lookup_table()
        src_bm.edges.ensure_lookup_table()
        src_bm.faces.ensure_lookup_table()

        # Find connected vertex islands via BFS across edges
        visited: set[int] = set()
        islands: list[list[Vector]] = []
        for v in src_bm.verts:
            if v.index in visited:
                continue
            comp_coords: list[Vector] = []
            queue = [v]
            visited.add(v.index)
            while queue:
                cur = queue.pop()
                comp_coords.append(cur.co.copy())
                for e in cur.link_edges:
                    other = e.other_vert(cur)
                    if other.index not in visited:
                        visited.add(other.index)
                        queue.append(other)
            if len(comp_coords) >= 4:
                islands.append(comp_coords)
        src_bm.free()

        def _island_bounds(pts: list[Vector]):
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            zs = [p.z for p in pts]
            return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

        def _island_vol(pts: list[Vector]) -> float:
            b0, b1 = _island_bounds(pts)
            return max(0.01, b1[0] - b0[0]) * max(0.01, b1[1] - b0[1]) * max(0.01, b1[2] - b0[2])

        # Filter to true 3D volumetric islands (if all islands have 3D thickness, unlike per-face cylinder strips)
        vol_islands = []
        for isl in islands:
            b0, b1 = _island_bounds(isl)
            thick_cnt = int((b1[0] - b0[0]) >= 0.015) + int((b1[1] - b0[1]) >= 0.015) + int((b1[2] - b0[2]) >= 0.015)
            if thick_cnt >= 3:
                vol_islands.append(isl)

        if len(vol_islands) > 8 and len(vol_islands) == len(islands):
            # Tier-aware greedy merging along Blender Z-up so >8 piece curved benches/rails preserve all 8 hulls & open leg gaps!
            all_zmin = min(_island_bounds(p)[0][2] for p in vol_islands)
            all_zmax = max(_island_bounds(p)[1][2] for p in vol_islands)
            h_tot = max(1e-3, all_zmax - all_zmin)
            g_z = all_zmin + 0.18 * h_tot
            e_z = all_zmin + 0.28 * h_tot

            def _isl_tier(pts: list[Vector]) -> int:
                b0, b1 = _island_bounds(pts)
                if b0[2] >= e_z:
                    return 1
                if b0[2] <= g_z and b1[2] <= all_zmin + 0.90 * h_tot:
                    if max(b1[0] - b0[0], b1[1] - b0[1]) <= 0.38:
                        return 0
                    return 2
                return 2

            while len(vol_islands) > 8:
                best_pair = (0, 1)
                best_cost = 1e18
                n_cur = len(vol_islands)
                tiers = [_isl_tier(p) for p in vol_islands]
                bounds_list = [_island_bounds(p) for p in vol_islands]
                for i in range(n_cur):
                    b0_i, b1_i = bounds_list[i]
                    vi = _island_vol(vol_islands[i])
                    ci = Vector((0.5 * (b0_i[0] + b1_i[0]), 0.5 * (b0_i[1] + b1_i[1]), 0.5 * (b0_i[2] + b1_i[2])))
                    for j in range(i + 1, n_cur):
                        b0_j, b1_j = bounds_list[j]
                        vj = _island_vol(vol_islands[j])
                        cj = Vector((0.5 * (b0_j[0] + b1_j[0]), 0.5 * (b0_j[1] + b1_j[1]), 0.5 * (b0_j[2] + b1_j[2])))
                        ux = max(0.01, max(b1_i[0], b1_j[0]) - min(b0_i[0], b0_j[0]))
                        uy = max(0.01, max(b1_i[1], b1_j[1]) - min(b0_i[1], b0_j[1]))
                        uz = max(0.01, max(b1_i[2], b1_j[2]) - min(b0_i[2], b0_j[2]))
                        dead_v = max(0.0, ux * uy * uz - (vi + vj))
                        dist = (ci - cj).length
                        gx = max(0.0, max(b0_i[0], b0_j[0]) - min(b1_i[0], b1_j[0]))
                        gy = max(0.0, max(b0_i[1], b0_j[1]) - min(b1_i[1], b1_j[1]))
                        same_col = (gx <= 0.04 and gy <= 0.04)
                        t_pen = 0.0 if tiers[i] == tiers[j] else 100.0
                        if tiers[i] == 0 and tiers[j] == 0:
                            cost = (-10.0 + dist * 0.1) if same_col else (80.0 + dead_v * 4.0 + dist * 2.0)
                        else:
                            cost = t_pen + dead_v * 4.0 + dist * 0.5
                        if cost < best_cost:
                            best_cost = cost
                            best_pair = (i, j)
                im, jm = best_pair
                vol_islands[im] = vol_islands[im] + vol_islands[jm]
                vol_islands.pop(jm)
            islands = vol_islands
        else:
            islands.sort(key=_island_vol, reverse=True)
            islands = islands[:8]

        # If the mesh is a single connected manifold or face-strip cylinder mesh (like long_rail_skinny where 27 face strips
        # represent 1 bar + 2 posts), run structural Z-up decomposition across all vertices!
        all_pts = [p for isl in islands for p in isl]
        is_strip_split = len(vol_islands) < len(islands)
        if len(all_pts) >= 8 and (len(islands) <= 2 or is_strip_split):
            xs = [p.x for p in all_pts]
            ys = [p.y for p in all_pts]
            zs = [p.z for p in all_pts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_z, max_z = min(zs), max(zs)
            span_x = max_x - min_x
            span_y = max_y - min_y
            span_z = max_z - min_z
            use_x = (span_x >= span_y)
            dom_min = min_x if use_x else min_y
            dom_span = max(span_x, span_y)
            if span_z >= 0.14 and dom_span >= 0.35:
                n_bins = 48
                bin_zmin = [1e9] * n_bins
                for p in all_pts:
                    d_val = p.x if use_x else p.y
                    bi = max(0, min(n_bins - 1, int(((d_val - dom_min) / max(dom_span, 1e-4)) * n_bins)))
                    if p.z < bin_zmin[bi]:
                        bin_zmin[bi] = p.z
                valid_z = [z for z in bin_zmin if z < 1e8]
                ground_t = min_z + 0.18 * span_z
                elev_t = min_z + 0.30 * span_z
                has_g = any(z <= ground_t for z in valid_z)
                elev_zs = sorted(z for z in valid_z if z >= elev_t)
                if has_g and elev_zs:
                    z_bar_bot = elev_zs[max(0, len(elev_zs) // 4)]
                    z_split = max(min_z + 0.25 * span_z, min(min_z + 0.88 * span_z, z_bar_bot - 0.002))
                    upper_pts = [p for p in all_pts if p.z >= z_split]
                    new_islands: list[list[Vector]] = []
                    if len(upper_pts) >= 4:
                        # Check if upper_pts curves in X/Y or arches in Z
                        u_ds = [p.x if use_x else p.y for p in upper_pts]
                        u_ss = [p.y if use_x else p.x for p in upper_pts]
                        u_dmin, u_dmax = min(u_ds), max(u_ds)
                        u_s_span = max(u_ss) - min(u_ss)
                        if u_s_span > 0.25 and (u_dmax - u_dmin) >= 0.80:
                            for ci in range(4):
                                d0 = u_dmin + (ci / 4.0) * (u_dmax - u_dmin) - (0.015 if ci > 0 else 0.0)
                                d1 = u_dmin + ((ci + 1) / 4.0) * (u_dmax - u_dmin) + (0.015 if ci < 3 else 0.0)
                                seg_pts = [p for p in upper_pts if d0 <= (p.x if use_x else p.y) <= d1]
                                if len(seg_pts) >= 4:
                                    new_islands.append(seg_pts)
                        else:
                            new_islands.append(upper_pts)
                    # Cluster lower support vertices by exact coordinate distance along the dominant axis
                    lower_pts = sorted(
                        [p for p in all_pts if p.z < z_split + 0.005],
                        key=lambda pt: (pt.x if use_x else pt.y),
                    )
                    if len(lower_pts) >= 4:
                        gap_thresh = max(0.045, 0.03 * dom_span)
                        clusters: list[list[Vector]] = [[lower_pts[0]]]
                        for p in lower_pts[1:]:
                            prev_d = clusters[-1][-1].x if use_x else clusters[-1][-1].y
                            cur_d = p.x if use_x else p.y
                            if (cur_d - prev_d) > gap_thresh:
                                clusters.append([p])
                            else:
                                clusters[-1].append(p)
                        for cl in clusters:
                            if len(cl) < 4:
                                continue
                            lx0, lx1 = min(p.x for p in cl), max(p.x for p in cl)
                            ly0, ly1 = min(p.y for p in cl), max(p.y for p in cl)
                            lz0, lz1 = min(p.z for p in cl), max(p.z for p in cl)
                            if lz0 > ground_t and (lz1 - lz0) < 0.15 * span_z:
                                continue
                            top_z = max(lz1, z_split + 0.002) if lz0 <= ground_t else lz1
                            box_corners = [
                                Vector((lx0, ly0, lz0)), Vector((lx1, ly0, lz0)),
                                Vector((lx0, ly1, lz0)), Vector((lx1, ly1, lz0)),
                                Vector((lx0, ly0, top_z)), Vector((lx1, ly0, top_z)),
                                Vector((lx0, ly1, top_z)), Vector((lx1, ly1, top_z)),
                            ]
                            new_islands.append(box_corners)
                    if len(new_islands) >= 2:
                        islands = new_islands[:8]

        out_bm = bmesh.new()
        planar_deg = 14.0 if target_tris <= 48 else (9.0 if target_tris <= 96 else 5.0)
        for pts in islands:
            part_bm = bmesh.new()
            for co in pts:
                part_bm.verts.new(co)
            part_bm.verts.ensure_lookup_table()
            try:
                bmesh.ops.remove_doubles(part_bm, verts=part_bm.verts[:], dist=0.002)
                if len(part_bm.verts) >= 4:
                    res_ch = bmesh.ops.convex_hull(part_bm, input=part_bm.verts[:], use_existing_faces=False)
                    hull_geom = set(res_ch.get("geom", []))
                    del_faces = [f for f in part_bm.faces if f not in hull_geom]
                    if del_faces:
                        bmesh.ops.delete(part_bm, geom=del_faces, context='FACES')
                    del_edges = [e for e in part_bm.edges if e not in hull_geom and not e.link_faces]
                    if del_edges:
                        bmesh.ops.delete(part_bm, geom=del_edges, context='EDGES')
                    del_verts = [v for v in part_bm.verts if v not in hull_geom and not v.link_faces]
                    if del_verts:
                        bmesh.ops.delete(part_bm, geom=del_verts, context='VERTS')
                    if len(part_bm.faces) > 6:
                        bmesh.ops.dissolve_limit(
                            part_bm,
                            angle_limit=math.radians(planar_deg),
                            use_dissolve_boundaries=False,
                            verts=part_bm.verts[:],
                            edges=part_bm.edges[:],
                        )
                    bmesh.ops.triangulate(part_bm, faces=part_bm.faces[:])
            except Exception:
                pass

            if len(part_bm.faces) >= 4:
                v_map = {}
                for pv in part_bm.verts:
                    v_map[pv.index] = out_bm.verts.new(pv.co)
                out_bm.verts.ensure_lookup_table()
                for pf in part_bm.faces:
                    try:
                        out_bm.faces.new([v_map[v.index] for v in pf.verts])
                    except ValueError:
                        pass
            part_bm.free()

        if len(out_bm.verts) < 4 or len(out_bm.faces) < 4:
            out_bm.clear()
            bb = [Vector(c) for c in vis_obj.bound_box]
            min_v = Vector((min(p.x for p in bb), min(p.y for p in bb), min(p.z for p in bb)))
            max_v = Vector((max(p.x for p in bb), max(p.y for p in bb), max(p.z for p in bb)))
            bmesh.ops.create_cube(out_bm, size=1.0)
            for v in out_bm.verts:
                v.co.x = min_v.x if v.co.x < 0 else max_v.x
                v.co.y = min_v.y if v.co.y < 0 else max_v.y
                v.co.z = min_v.z if v.co.z < 0 else max_v.z
            bmesh.ops.triangulate(out_bm, faces=out_bm.faces[:])

        out_bm.normal_update()
        return out_bm, len(islands)

    def execute(self, context):
        act = context.active_object
        if act and act.type == 'MESH' and (act.name.upper().endswith("_PPHYS") or bool(act.get("reskate_is_pphys_proxy", False))):
            if context.mode == 'EDIT_MESH':
                return bpy.ops.reskate.toggle_edit_collision_mesh()

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        vis_obj = get_target_mesh_object(context) or context.active_object
        if not vis_obj or vis_obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D prop mesh first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        target_tris = int(getattr(prop, "pphys_target_tris", 64) or 64) if prop else 64

        pphys_obj = compiler_bridge.resolve_custom_placement_proxy_object(context, vis_obj)
        if pphys_obj is None or pphys_obj == vis_obj:
            pphys_name = f"{vis_obj.name}_PPHYS"
            pphys_obj = context.scene.objects.get(pphys_name)

        n_islands = 1
        should_build = (
            bool(self.force_regenerate)
            or pphys_obj is None
            or pphys_obj.type != 'MESH'
            or len(pphys_obj.data.vertices) < 4
        )
        if should_build:
            pphys_name = f"{vis_obj.name}_PPHYS"
            bm, n_islands = self._build_tight_pphys_bmesh(vis_obj, target_tris=target_tris)
            pphys_me = bpy.data.meshes.new(f"{pphys_name}_Mesh")
            bm.to_mesh(pphys_me)
            bm.free()

            if pphys_obj is not None and pphys_obj.name in bpy.data.objects:
                old_me = pphys_obj.data
                pphys_obj.data = pphys_me
                if old_me and old_me.users == 0:
                    bpy.data.meshes.remove(old_me)
            else:
                pphys_obj = bpy.data.objects.new(pphys_name, pphys_me)
                (vis_obj.users_collection[0] if vis_obj.users_collection else context.collection).objects.link(pphys_obj)

            pphys_obj.parent = vis_obj
            pphys_obj.matrix_parent_inverse = Matrix.Identity(4)
            pphys_obj.location = (0.0, 0.0, 0.0)
            pphys_obj.rotation_euler = (0.0, 0.0, 0.0)
            pphys_obj.scale = (1.0, 1.0, 1.0)

            # Apply decimate modifier if total tris still exceed target_tris
            cur_tris = len(pphys_obj.data.polygons)
            if cur_tris > target_tris and cur_tris > 12:
                ratio = max(0.10, min(0.95, float(target_tris) / float(cur_tris)))
                prev_active = context.view_layer.objects.active
                context.view_layer.objects.active = pphys_obj
                dec_mod = pphys_obj.modifiers.new(name="ReSkate_PPhys_Decimate", type='DECIMATE')
                dec_mod.ratio = ratio
                try:
                    bpy.ops.object.modifier_apply(modifier=dec_mod.name)
                except Exception:
                    pphys_obj.modifiers.remove(dec_mod)
                context.view_layer.objects.active = prev_active

        pphys_obj["reskate_is_pphys_proxy"] = True
        pphys_obj["reskate_col_owner"] = vis_obj.name
        vis_obj.reskate_custom_placement_proxy_object = pphys_obj

        st = dict(get_or_init_object_prop_state(vis_obj))
        if self.enter_edit_mode or st.get("placement_proxy_mode") == "BOUNDING_BOX":
            st["placement_proxy_mode"] = 'CUSTOM_OBJECT' if self.enter_edit_mode else 'MATCH_COLLISION'
        st["custom_placement_proxy_object_name"] = pphys_obj.name
        ser = json.dumps(st)
        vis_obj["reskate_prop_state"] = ser
        vis_obj["reskate_prop_state_json"] = ser

        if prop is not None:
            if self.enter_edit_mode:
                prop.placement_proxy_mode = 'CUSTOM_OBJECT'
            elif getattr(prop, "placement_proxy_mode", "MATCH_COLLISION") == 'BOUNDING_BOX':
                prop.placement_proxy_mode = 'MATCH_COLLISION'
            prop.custom_placement_proxy_object = pphys_obj
            prop.show_placement_proxy_overlay = True

        pphys_obj.hide_render = True
        pphys_obj.hide_viewport = False
        pphys_obj.hide_select = False
        try:
            pphys_obj.hide_set(False)
        except Exception:
            pass
        pphys_obj.display_type = 'WIRE'
        pphys_obj.show_wire = True
        pphys_obj.show_all_edges = True

        if self.enter_edit_mode:
            pphys_obj.display_type = 'SOLID'
            bpy.ops.object.select_all(action='DESELECT')
            pphys_obj.select_set(True)
            context.view_layer.objects.active = pphys_obj
            if bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='EDIT')

            hide_vis = bool(getattr(prop, "hide_visual_during_col_edit", False)) if prop else False
            RESKATE_OT_toggle_edit_collision_mesh._isolate_visual_prop_for_proxy_edit(vis_obj, hide_completely=hide_vis)
            msg = f"ReSkate: Editing Placement Mesh '{pphys_obj.name}' — click Done Editing when finished."
        else:
            bpy.ops.object.select_all(action='DESELECT')
            vis_obj.select_set(True)
            context.view_layer.objects.active = vis_obj
            final_tris = sum(max(0, len(p.vertices) - 2) for p in pphys_obj.data.polygons)
            msg = (
                f"ReSkate: Generated tight Placement Mesh '{pphys_obj.name}' "
                f"({len(pphys_obj.data.vertices)} verts, {final_tris} tris across {n_islands} part(s))."
            )

        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _order_selected_chain_from_orig_coords(
    bm: bmesh.types.BMesh,
    sel_indices: list[int],
    orig_map: dict[int, Vector],
) -> list[int]:
    """Orders selected vertices along connected edges (or spatial progression) using their original baseline coordinates."""
    if len(sel_indices) <= 2:
        return list(sel_indices)
    sel_set = set(sel_indices)
    adj: dict[int, list[int]] = {i: [] for i in sel_indices}
    for i in sel_indices:
        v = bm.verts[i]
        for e in v.link_edges:
            if e.select or e.other_vert(v).index in sel_set:
                oth = e.other_vert(v).index
                if oth in sel_set and oth not in adj[i]:
                    adj[i].append(oth)

    endpoints = [i for i, nbrs in adj.items() if len(nbrs) == 1]
    if endpoints:
        start = endpoints[0]
    else:
        # Pick the vertex furthest from the centroid as start
        centroid = sum((orig_map[i] for i in sel_indices), Vector((0.0, 0.0, 0.0))) / float(len(sel_indices))
        start = max(sel_indices, key=lambda idx: (orig_map[idx] - centroid).length_squared)

    ordered = [start]
    visited = {start}
    cur = start
    while len(ordered) < len(sel_indices):
        unvis_nbrs = [n for n in adj.get(cur, []) if n not in visited]
        if unvis_nbrs:
            cur_co = orig_map[cur]
            nxt = min(unvis_nbrs, key=lambda n: (orig_map[n] - cur_co).length_squared)
        else:
            rem = [i for i in sel_indices if i not in visited]
            cur_co = orig_map[cur]
            nxt = min(rem, key=lambda n: (orig_map[n] - cur_co).length_squared)
        ordered.append(nxt)
        visited.add(nxt)
        cur = nxt
    return ordered


class RESKATE_OT_straighten_collision_edges(bpy.types.Operator):
    """Non-destructively straightens, curves, or flattens selected collision vertices/edges relative to a stored baseline snapshot"""
    bl_idname = "reskate.straighten_collision_edges"
    bl_label = "Apply / Update Edge Snap"
    bl_description = "Snaps selected collision edges/vertices onto a Straight Line, Smooth Arc, or Flat Plane using a non-destructive baseline snapshot so you can adjust Strength/Arc freely or Reset at any time"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            obj = get_target_mesh_object(context)
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a mesh object first.")
            return {'CANCELLED'}

        # If called from Object Mode, check if an active Grind Path has vertex indices we can select, and enter Edit Mode
        if obj.mode != 'EDIT':
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            if bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='EDIT')
            me_chk = obj.data
            bm_chk = bmesh.from_edit_mesh(me_chk)
            bm_chk.verts.ensure_lookup_table()
            bm_chk.edges.ensure_lookup_table()
            sel_v_chk = [v for v in bm_chk.verts if v.select]
            if len(sel_v_chk) < 2 and hasattr(obj, "reskate_grind_paths") and len(obj.reskate_grind_paths) > 0:
                gp_idx = max(0, min(getattr(obj, "reskate_grind_path_index", 0), len(obj.reskate_grind_paths) - 1))
                gp = obj.reskate_grind_paths[gp_idx]
                v_ids = [int(x) for x in (gp.vertex_indices_csv or "").split(",") if x.strip().isdigit()]
                if len(v_ids) >= 2:
                    v_set = set(v_ids)
                    for v in bm_chk.verts:
                        v.select = (v.index in v_set)
                    for e in bm_chk.edges:
                        e.select = (e.verts[0].index in v_set and e.verts[1].index in v_set)
                    bmesh.update_edit_mesh(me_chk)
            sel_v_chk = [v for v in bm_chk.verts if v.select]
            if len(sel_v_chk) < 2:
                self.report({'INFO'}, f"ReSkate: Entered Edit Mode on '{obj.name}' — select the edge chain you want to straighten and click Apply.")
                return {'FINISHED'}

        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        sel_verts = [v for v in bm.verts if v.select]
        if len(sel_verts) < 2:
            self.report({'WARNING'}, "ReSkate: Select at least 2 vertices (or 1+ edges) in Edit Mode.")
            return {'CANCELLED'}

        sel_indices = sorted(v.index for v in sel_verts)
        sig_str = ",".join(str(i) for i in sel_indices)

        # Load or create non-destructive baseline snapshot
        baseline = None
        raw_base = obj.get("reskate_col_edge_baseline_json", "")
        if raw_base:
            try:
                parsed = json.loads(str(raw_base))
                if isinstance(parsed, dict) and parsed.get("sig") == sig_str and isinstance(parsed.get("coords"), dict):
                    baseline = parsed
            except Exception:
                baseline = None

        if baseline is None:
            coords_dict = {
                str(v.index): [round(float(v.co.x), 6), round(float(v.co.y), 6), round(float(v.co.z), 6)]
                for v in sel_verts
            }
            baseline = {"sig": sig_str, "coords": coords_dict}
            obj["reskate_col_edge_baseline_json"] = json.dumps(baseline)

        coords_map = baseline["coords"]
        orig_map: dict[int, Vector] = {}
        for idx in sel_indices:
            c_list = coords_map.get(str(idx))
            if c_list and len(c_list) == 3:
                orig_map[idx] = Vector((float(c_list[0]), float(c_list[1]), float(c_list[2])))
            else:
                orig_map[idx] = bm.verts[idx].co.copy()

        prop = getattr(context.scene, "reskate_prop", None)
        mode = str(getattr(prop, "col_straighten_mode", 'LINE') or 'LINE') if prop else 'LINE'
        strength = float(getattr(prop, "col_straighten_strength", 100.0)) if prop else 100.0
        arc_pct = float(getattr(prop, "col_straighten_arc", 50.0)) if prop else 50.0
        alpha = max(0.0, min(1.0, strength / 100.0))

        target_map: dict[int, Vector] = {}
        n_sel = len(sel_indices)
        centroid = sum(orig_map.values(), Vector((0.0, 0.0, 0.0))) / float(n_sel)

        if mode in ('LINE', 'ARC'):
            ordered = _order_selected_chain_from_orig_coords(bm, sel_indices, orig_map)
            pt_a = orig_map[ordered[0]]
            pt_b = orig_map[ordered[-1]]
            # If closed loop where endpoints coincide, pick the two furthest vertices as A and B
            if (pt_b - pt_a).length < 1e-5 and n_sel >= 2:
                best_pair = (ordered[0], ordered[-1])
                best_d2 = -1.0
                for i_a in range(n_sel):
                    for i_b in range(i_a + 1, n_sel):
                        d2 = (orig_map[ordered[i_b]] - orig_map[ordered[i_a]]).length_squared
                        if d2 > best_d2:
                            best_d2 = d2
                            best_pair = (ordered[i_a], ordered[i_b])
                pt_a = orig_map[best_pair[0]]
                pt_b = orig_map[best_pair[1]]

            # Cumulative chord length parameterization along ordered chain
            cum_dists = [0.0]
            for k in range(1, len(ordered)):
                seg_d = (orig_map[ordered[k]] - orig_map[ordered[k - 1]]).length
                cum_dists.append(cum_dists[-1] + seg_d)
            total_len = cum_dists[-1]

            t_by_idx: dict[int, float] = {}
            ab_vec = pt_b - pt_a
            ab_len2 = ab_vec.length_squared
            for k, v_idx in enumerate(ordered):
                if total_len > 1e-6:
                    t_val = cum_dists[k] / total_len
                elif ab_len2 > 1e-8:
                    t_val = max(0.0, min(1.0, (orig_map[v_idx] - pt_a).dot(ab_vec) / ab_len2))
                else:
                    t_val = 0.0
                t_by_idx[v_idx] = t_val

            if mode == 'LINE':
                for v_idx in sel_indices:
                    t = t_by_idx.get(v_idx, 0.0)
                    target_map[v_idx] = pt_a.lerp(pt_b, t)
            else:
                # Smooth Quadratic Bezier Arc: p_arc(t) = (1-t)^2 A + 2(1-t)t C + t^2 B
                p_line_mid = 0.5 * (pt_a + pt_b)
                # Find apex/mid vertex of the original chain (furthest perpendicular offset or middle vertex)
                mid_chain_idx = ordered[len(ordered) // 2]
                m_orig = orig_map[mid_chain_idx]
                max_dev = (m_orig - pt_a.lerp(pt_b, t_by_idx.get(mid_chain_idx, 0.5))).length
                for v_idx in ordered[1:-1]:
                    t_v = t_by_idx.get(v_idx, 0.5)
                    dev = (orig_map[v_idx] - pt_a.lerp(pt_b, t_v)).length
                    if dev > max_dev:
                        max_dev = dev
                        m_orig = orig_map[v_idx]
                if max_dev < 1e-5:
                    m_orig = centroid

                arc_factor = arc_pct / 50.0
                # For quadratic Bezier to pass through p_line_mid + arc_factor*(m_orig - p_line_mid) at t=0.5:
                ctrl_c = p_line_mid + 2.0 * arc_factor * (m_orig - p_line_mid)
                for v_idx in sel_indices:
                    t = t_by_idx.get(v_idx, 0.0)
                    omt = 1.0 - t
                    target_map[v_idx] = (omt * omt) * pt_a + (2.0 * omt * t) * ctrl_c + (t * t) * pt_b

        elif mode == 'PLANE_Z':
            mean_z = centroid.z
            for v_idx in sel_indices:
                oc = orig_map[v_idx]
                target_map[v_idx] = Vector((oc.x, oc.y, mean_z))

        elif mode == 'PLANE_X':
            mean_x = centroid.x
            for v_idx in sel_indices:
                oc = orig_map[v_idx]
                target_map[v_idx] = Vector((mean_x, oc.y, oc.z))

        elif mode == 'PLANE_Y':
            mean_y = centroid.y
            for v_idx in sel_indices:
                oc = orig_map[v_idx]
                target_map[v_idx] = Vector((oc.x, mean_y, oc.z))

        elif mode == 'PLANE_FIT':
            try:
                import numpy as np
                pts_np = np.array([[orig_map[i].x - centroid.x, orig_map[i].y - centroid.y, orig_map[i].z - centroid.z] for i in sel_indices], dtype=np.float64)
                _, _, vh = np.linalg.svd(pts_np, full_matrices=False)
                n_vec = Vector((float(vh[-1, 0]), float(vh[-1, 1]), float(vh[-1, 2])))
                if n_vec.length_squared > 1e-10:
                    n_vec.normalize()
                else:
                    n_vec = Vector((0.0, 0.0, 1.0))
            except Exception:
                n_vec = Vector((0.0, 0.0, 1.0))
            for v_idx in sel_indices:
                oc = orig_map[v_idx]
                dist_to_plane = (oc - centroid).dot(n_vec)
                target_map[v_idx] = oc - dist_to_plane * n_vec

        # Interpolate non-destructively from baseline orig_co
        mw = obj.matrix_world
        world_old_to_new: list[tuple[Vector, Vector]] = []
        for v_idx in sel_indices:
            orig_co = orig_map[v_idx]
            p_target = target_map.get(v_idx, orig_co)
            new_local = orig_co.lerp(p_target, alpha)
            old_world = mw @ bm.verts[v_idx].co
            orig_world = mw @ orig_co
            new_world = mw @ new_local
            bm.verts[v_idx].co = new_local
            world_old_to_new.append((orig_world, new_world))
            if (old_world - orig_world).length_squared > 1e-8:
                world_old_to_new.append((old_world, new_world))

        bm.normal_update()
        bmesh.update_edit_mesh(me, loop_triangles=True, destructive=False)

        # Smart Sync: if straightening on the visual prop while a custom <Prop>_COL exists,
        # automatically sync matching vertices on <Prop>_COL so custom collision stays aligned!
        is_col_obj = obj.name.endswith("_COL") or getattr(getattr(obj, "reskate_col_prop", None), "is_col_proxy", False)
        if not is_col_obj and world_old_to_new:
            col_obj = compiler_bridge.resolve_custom_collision_object(context, obj)
            if col_obj is not None and col_obj != obj and getattr(col_obj, "type", None) == 'MESH' and col_obj.data:
                try:
                    col_mw = col_obj.matrix_world
                    col_inv = col_mw.inverted_safe()
                    col_me = col_obj.data
                    tol2 = 0.045 * 0.045
                    updated_col = 0
                    for cv in col_me.vertices:
                        cw = col_mw @ cv.co
                        best_new_w = None
                        best_d2 = tol2
                        for old_w, new_w in world_old_to_new:
                            d2 = (cw - old_w).length_squared
                            if d2 <= best_d2:
                                best_d2 = d2
                                best_new_w = new_w
                        if best_new_w is not None:
                            cv.co = col_inv @ best_new_w
                            updated_col += 1
                    if updated_col > 0:
                        col_me.update()
                except Exception:
                    pass

        gpu_draw.invalidate_gpu_caches()
        return {'FINISHED'}


class RESKATE_OT_reset_collision_edges(bpy.types.Operator):
    """Restores selected collision vertices (or all vertices in the baseline snapshot) back to their original coordinates"""
    bl_idname = "reskate.reset_collision_edges"
    bl_label = "Reset Edge to Original"
    bl_description = "Restores selected collision vertices (or all vertices in the stored baseline snapshot) back to their original unmodified coordinates"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
            self.report({'WARNING'}, "ReSkate: Enter Edit Mode on a collision mesh first.")
            return {'CANCELLED'}

        raw_base = obj.get("reskate_col_edge_baseline_json", "")
        if not raw_base:
            self.report({'INFO'}, "ReSkate: No baseline snapshot stored on this mesh yet.")
            return {'CANCELLED'}

        try:
            baseline = json.loads(str(raw_base))
            coords_map = baseline.get("coords", {}) if isinstance(baseline, dict) else {}
        except Exception:
            coords_map = {}

        if not coords_map:
            return {'CANCELLED'}

        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()

        sel_verts = [v for v in bm.verts if v.select and str(v.index) in coords_map]
        targets = sel_verts if sel_verts else [bm.verts[int(k)] for k in coords_map if k.isdigit() and int(k) < len(bm.verts)]

        restored = 0
        for v in targets:
            c_list = coords_map.get(str(v.index))
            if c_list and len(c_list) == 3:
                v.co = Vector((float(c_list[0]), float(c_list[1]), float(c_list[2])))
                restored += 1

        bm.normal_update()
        bmesh.update_edit_mesh(me, loop_triangles=True, destructive=False)
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Restored {restored} vertex(es) to original baseline positions.")
        return {'FINISHED'}


class RESKATE_OT_lock_collision_edge_baseline(bpy.types.Operator):
    """Locks the current positions of selected collision vertices as the new baseline snapshot"""
    bl_idname = "reskate.lock_collision_edge_baseline"
    bl_label = "Lock Current as New Baseline"
    bl_description = "Saves the current vertex positions of your selection as the new baseline snapshot so further snaps interpolate from this shape"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
            self.report({'WARNING'}, "ReSkate: Enter Edit Mode on a collision mesh and select vertices first.")
            return {'CANCELLED'}

        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()

        sel_verts = [v for v in bm.verts if v.select]
        if not sel_verts:
            self.report({'WARNING'}, "ReSkate: Select vertices/edges in Edit Mode to lock as baseline.")
            return {'CANCELLED'}

        sel_indices = sorted(v.index for v in sel_verts)
        sig_str = ",".join(str(i) for i in sel_indices)
        coords_dict = {
            str(v.index): [round(float(v.co.x), 6), round(float(v.co.y), 6), round(float(v.co.z), 6)]
            for v in sel_verts
        }
        obj["reskate_col_edge_baseline_json"] = json.dumps({"sig": sig_str, "coords": coords_dict})
        self.report({'INFO'}, f"ReSkate: Locked {len(sel_verts)} vertex(es) as the new Edge Straightener baseline.")
        return {'FINISHED'}


class RESKATE_OT_sync_material_surfaces(bpy.types.Operator):
    """Synchronizes the per-material surface override slots with the active mesh's material slots"""
    bl_idname = "reskate.sync_material_surfaces"
    bl_label = "Refresh Material Slots"
    bl_description = "Populates per-material surface override slots from the mesh's material list and syncs with Material > Skate Map"
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _guess_surface_from_name(mat_name: str) -> str:
        return compiler_bridge._guess_material_surface_preset(mat_name)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a mesh object first.")
            return {'CANCELLED'}

        coll = obj.reskate_material_surfaces
        existing = {item.material_name: item.surface for item in coll}
        coll.clear()
        for idx, slot in enumerate(obj.material_slots):
            m_name = slot.material.name if slot.material else f"Slot {idx + 1}"
            item = coll.add()
            item.material_name = m_name
            if m_name in existing and existing[m_name] != 'DEFAULT':
                item.surface = existing[m_name]
            else:
                inferred_id, _ = compiler_bridge.infer_surface_from_material_slot(slot.material)
                if inferred_id != 'DEFAULT':
                    try:
                        item.surface = inferred_id
                    except Exception:
                        item.surface = self._guess_surface_from_name(m_name)
                else:
                    item.surface = self._guess_surface_from_name(m_name)
            if slot.material is not None:
                compiler_bridge.sync_material_from_shader_nodes(slot.material, obj=obj, slot_index=idx)
        self.report({'INFO'}, f"ReSkate: Synced {len(coll)} material slots across Collision and Look > Skate Map.")
        return {'FINISHED'}


class RESKATE_OT_toggle_prop_id_lock(bpy.types.Operator):
    """Toggles the lock on Prop ID (changing Prop ID after publishing breaks parks where the prop was already placed)"""
    bl_idname = "reskate.toggle_prop_id_lock"
    bl_label = "Toggle Prop ID Lock"
    bl_description = "Unlocks or locks the internal Prop ID (Warning: changing Prop ID after placing this prop in a park will break saved park placements)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        prop.prop_id_locked = not bool(getattr(prop, "prop_id_locked", True))
        if not prop.prop_id_locked:
            self.report({'WARNING'}, "ReSkate: Prop ID unlocked. Changing Prop ID will break saved parks that already placed this prop!")
        else:
            self.report({'INFO'}, f"ReSkate: Prop ID locked as '{prop.prop_id}'.")
        return {'FINISHED'}


class RESKATE_OT_preflight_fix(bpy.types.Operator):
    """Applies a one-click fix for a Step 6 Pre-flight checklist warning/error (never alters or repacks UV maps)"""
    bl_idname = "reskate.preflight_fix"
    bl_label = "Fix Pre-Flight Issue"
    bl_description = "Automatically fixes this pre-flight check item on the active prop (UV fixes are strictly manual to protect existing textures)"
    bl_options = {'REGISTER', 'UNDO'}

    fix_action: StringProperty(name="Fix Action", default="")

    def _refresh_cached_preflight(self, context, obj):
        try:
            obj["_reskate_last_preflight_json"] = json.dumps(compiler_bridge.run_preflight_checks(context, obj))
        except Exception:
            pass

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a mesh object first.")
            return {'CANCELLED'}

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        act = (self.fix_action or "").upper()
        if act in ("SMART_UV_PROJECT", "AUTO_UNWRAP_UV", "AUTO_UNWRAP_UVS", "PACK_UV_BOUNDS"):
            self.report(
                {'INFO'},
                "ReSkate: UV checks are advisory only — unwrap or adjust UVs manually in UV Editing so your textures are never overwritten.",
            )
            return {'CANCELLED'}

        if act == "APPLY_SCALE_ROT":
            prev_active = context.view_layer.objects.active
            prev_sel = [o for o in context.selected_objects]
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            try:
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            finally:
                bpy.ops.object.select_all(action='DESELECT')
                for o in prev_sel:
                    if o and o.name in bpy.data.objects:
                        o.select_set(True)
                if prev_active and prev_active.name in bpy.data.objects:
                    context.view_layer.objects.active = prev_active
            gpu_draw.invalidate_gpu_caches()
            self._refresh_cached_preflight(context, obj)
            self.report({'INFO'}, "ReSkate: Applied Scale (1,1,1) and Rotation (0,0,0).")
            return {'FINISHED'}

        elif act == "ORIGIN_BOTTOM_CENTER":
            obj["reskate_custom_origin"] = False
            res = bpy.ops.reskate.set_origin(mode='BOTTOM_CENTER')
            self._refresh_cached_preflight(context, obj)
            return res

        elif act == "LOCK_CUSTOM_ORIGIN":
            obj["reskate_custom_origin"] = True
            compiler_bridge.invalidate_ui_caches()
            gpu_draw.invalidate_gpu_caches()
            self._refresh_cached_preflight(context, obj)
            self.report({'INFO'}, f"ReSkate: Custom Origin locked on '{obj.name}' (Pre-Flight check marked OK).")
            return {'FINISHED'}

        elif act == "TRIANGULATE_NGONS":
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            ngons = [f for f in bm.faces if len(f.verts) > 4]
            cnt = len(ngons)
            if ngons:
                bmesh.ops.triangulate(bm, faces=ngons)
                bm.to_mesh(obj.data)
                obj.data.update()
            bm.free()
            self._refresh_cached_preflight(context, obj)
            self.report({'INFO'}, f"ReSkate: Triangulated {cnt} N-gon face(s).")
            return {'FINISHED'}

        elif act == "LINK_CHILD_LIGHTS":
            linked_names = {
                getattr(it.light_obj, "name", "")
                for it in getattr(obj, "reskate_linked_lights", [])
                if getattr(it, "light_obj", None) is not None
            }
            added = 0
            for ch in obj.children:
                if ch.type == 'LIGHT' and ch.name not in linked_names:
                    slot = obj.reskate_linked_lights.add()
                    slot.light_obj = ch
                    slot.enabled = True
                    added += 1
            self._refresh_cached_preflight(context, obj)
            self.report({'INFO'}, f"ReSkate: Linked {added} child light(s) to '{obj.name}'.")
            return {'FINISHED'}

        elif act == "THICKEN_COLLISION":
            res = bpy.ops.reskate.thicken_collision_proxy()
            self._refresh_cached_preflight(context, obj)
            return res

        elif act == "GENERATE_SIMPLIFIED_COL":
            res = bpy.ops.reskate.generate_collision_preview()
            self._refresh_cached_preflight(context, obj)
            return res

        return {'CANCELLED'}


class RESKATE_OT_apply_surface_feel_preset(bpy.types.Operator):
    """Applies a plain-language Surface Feel Preset to the prop or a specific material slot"""
    bl_idname = "reskate.apply_surface_feel_preset"
    bl_label = "Apply Surface Feel Preset"
    bl_description = "Sets the physics surface and wall-snap behavior from a curated skateboard feel preset"
    bl_options = {'REGISTER', 'UNDO'}

    preset_id: StringProperty(name="Preset", default="CONCRETE_LEDGE")
    slot_index: IntProperty(name="Slot Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        prop = getattr(context.scene, "reskate_prop", None)
        pinfo = SURFACE_FEEL_PRESETS.get(self.preset_id)
        if not pinfo:
            return {'CANCELLED'}

        surf_key = pinfo["surface"]
        no_snap = bool(pinfo.get("prevent_wall_snap", False))

        if self.slot_index >= 0 and obj and hasattr(obj, "reskate_material_surfaces"):
            if self.slot_index < len(obj.reskate_material_surfaces):
                slot_item = obj.reskate_material_surfaces[self.slot_index]
                slot_item.feel_preset = self.preset_id
                slot_item.surface = surf_key
                slot_item.prevent_wall_snap = no_snap
                self.report({'INFO'}, f"ReSkate: Set '{slot_item.material_name}' to {pinfo['label']}.")
                return {'FINISHED'}

        if prop:
            prop.surface_feel_preset = self.preset_id
            prop.use_full_surface_catalog = False
            prop.primary_surface = surf_key
            prop.prevent_wall_snap_default = no_snap
            self.report({'INFO'}, f"ReSkate: Applied Surface Feel Preset '{pinfo['label']}'.")
        return {'FINISHED'}


class RESKATE_OT_trim_bottom_lip(bpy.types.Operator):
    """Trims bottom-clipping lips using BMesh bisect plane and seals face to ensure flush ground contact"""
    bl_idname = "reskate.trim_bottom_lip"
    bl_label = "Trim Bottom Lip (Flush Ground)"
    bl_description = "Cuts away bottom geometry below cut height and seals with a planar face so ramps make seamless ground contact"
    bl_options = {'REGISTER', 'UNDO'}

    trim_height: FloatProperty(
        name="Trim Height", default=0.01, min=0.001, max=0.40, precision=3, unit='LENGTH',
        description="Vertical height above lowest vertex to slice flush"
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = context.active_object
        if not obj or obj.type != 'MESH' or len(obj.data.vertices) == 0:
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        height = prop.trim_bottom if (prop and prop.trim_bottom > 0.0) else self.trim_height

        bm = bmesh.new()
        bm.from_mesh(obj.data)

        min_z = min(v.co.z for v in bm.verts)
        max_z = max(v.co.z for v in bm.verts)
        mesh_height = max_z - min_z

        if height >= mesh_height - 1e-4:
            bm.free()
            self.report({'WARNING'}, f"ReSkate: Trim height ({height:.3f}m) exceeds mesh height ({mesh_height:.3f}m).")
            return {'CANCELLED'}

        cut_z = min_z + height
        ret = bmesh.ops.bisect_plane(
            bm,
            geom=bm.faces[:] + bm.edges[:] + bm.verts[:],
            plane_co=Vector((0.0, 0.0, cut_z)),
            plane_no=Vector((0.0, 0.0, -1.0)),
            clear_outer=True,
            clear_inner=False,
            use_snap_center=False,
        )

        cut_edges = [e for e in ret.get('geom_cut', []) if isinstance(e, bmesh.types.BMEdge)]
        if cut_edges:
            try:
                bmesh.ops.edgeloop_fill(bm, edges=cut_edges)
            except Exception:
                pass

        for v in bm.verts:
            v.co.z -= cut_z

        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        gpu_draw.invalidate_gpu_caches()

        self.report({'INFO'}, f"ReSkate: Trimmed {height:.3f}m bottom lip.")
        return {'FINISHED'}


# ==============================================================================
# Step 3: Grind Operators (World-Space Auto-Detect + Vertex-Path + Grind Type)
# ==============================================================================

def _shortest_vertex_path_bmesh(bm: bmesh.types.BMesh, start_idx: int, end_idx: int) -> list[int]:
    """Finds the shortest edge path between two vertex indices using Dijkstra's algorithm."""
    bm.verts.ensure_lookup_table()
    if start_idx == end_idx:
        return [start_idx]
    n = len(bm.verts)
    if not (0 <= start_idx < n and 0 <= end_idx < n):
        return []

    dist = {start_idx: 0.0}
    prev = {}
    pq = [(0.0, start_idx)]
    visited = set()

    while pq:
        d, u = heapq.heappop(pq)
        if u == end_idx:
            break
        if u in visited:
            continue
        visited.add(u)
        vu = bm.verts[u]
        for e in vu.link_edges:
            vv = e.other_vert(vu)
            v_idx = vv.index
            if v_idx in visited:
                continue
            nd = d + (vv.co - vu.co).length
            if nd < dist.get(v_idx, float("inf")):
                dist[v_idx] = nd
                prev[v_idx] = u
                heapq.heappush(pq, (nd, v_idx))

    if end_idx not in prev:
        return [start_idx, end_idx]

    path = []
    cur = end_idx
    while cur is not None:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    return path


def _order_selected_vertex_chain(bm: bmesh.types.BMesh, selected_indices: list[int]) -> list[int]:
    """Orders selected vertices along connected edges or by spatial chain progression."""
    bm.verts.ensure_lookup_table()
    if len(selected_indices) <= 1:
        return list(selected_indices)
    if len(selected_indices) == 2:
        return _shortest_vertex_path_bmesh(bm, selected_indices[0], selected_indices[1])

    sel_set = set(selected_indices)
    adj = {i: [] for i in selected_indices}
    for i in selected_indices:
        v = bm.verts[i]
        for e in v.link_edges:
            other = e.other_vert(v).index
            if other in sel_set:
                adj[i].append(other)

    # Find an endpoint (degree 1 in subgraph) or extremal vertex
    endpoints = [i for i, nbrs in adj.items() if len(nbrs) == 1]
    start = endpoints[0] if endpoints else selected_indices[0]

    ordered = [start]
    visited = {start}
    cur = start
    while len(ordered) < len(selected_indices):
        unvisited_nbrs = [n for n in adj.get(cur, []) if n not in visited]
        if unvisited_nbrs:
            nxt = unvisited_nbrs[0]
        else:
            # Nearest unvisited selected vertex, bridged via shortest edge path
            rem = [i for i in selected_indices if i not in visited]
            cur_co = bm.verts[cur].co
            nxt = min(rem, key=lambda idx: (bm.verts[idx].co - cur_co).length)
            bridge = _shortest_vertex_path_bmesh(bm, cur, nxt)
            for b_idx in bridge[1:-1]:
                if b_idx not in visited:
                    ordered.append(b_idx)
                    visited.add(b_idx)
        ordered.append(nxt)
        visited.add(nxt)
        cur = nxt

    return ordered


class RESKATE_OT_add_grind_path_from_selection(bpy.types.Operator):
    """Selection-mode-aware grind path builder: works in Edge Select, Vertex Select, and Face Select modes"""
    bl_idname = "reskate.add_grind_path_from_selection"
    bl_label = "Add Grind from Selection"
    bl_description = "Adds a Grind Path from your current Edit Mode selection (Edge Select = selected edges; Vertex Select = shortest path or chain; Face Select = top/outer boundary edges)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a mesh object first.")
            return {'CANCELLED'}

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh) if obj.mode == 'EDIT' else bmesh.new()
        if obj.mode != 'EDIT':
            bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        prop = getattr(context.scene, "reskate_prop", None)
        chosen_type = getattr(prop, "grind_type", 'LEDGE') if prop else 'LEDGE'
        tag_val = 2 if chosen_type == 'ROUND_RAIL' else (3 if chosen_type == 'COPING' else 1)

        sel_mode = tuple(context.tool_settings.mesh_select_mode) if (context and getattr(context, "tool_settings", None)) else (True, False, False)
        is_face_mode = bool(sel_mode[2]) and not bool(sel_mode[0])
        is_edge_mode = bool(sel_mode[1]) and not bool(sel_mode[0])

        mw = obj.matrix_world
        ordered_indices: list[int] = []
        marked_edges: list[int] = []

        if is_face_mode:
            sel_faces = [f for f in bm.faces if f.select]
            if not sel_faces:
                if obj.mode != 'EDIT':
                    bm.free()
                self.report({'WARNING'}, "ReSkate: Select at least 1 face in Face Select mode.")
                return {'CANCELLED'}
            sel_face_set = set(sel_faces)
            boundary_edges = []
            for f in sel_faces:
                for e in f.edges:
                    linked_sel = [lf for lf in e.link_faces if lf in sel_face_set]
                    if len(linked_sel) == 1:
                        p0 = mw @ e.verts[0].co
                        p1 = mw @ e.verts[1].co
                        seg = p1 - p0
                        if seg.length > 1e-4 and abs(seg.z / seg.length) < 0.72:
                            boundary_edges.append(e)
            if not boundary_edges:
                boundary_edges = [e for f in sel_faces for e in f.edges]
            # Pick highest boundary edges (top lip of selected face region)
            if boundary_edges:
                max_ez = max(0.5 * ((mw @ e.verts[0].co).z + (mw @ e.verts[1].co).z) for e in boundary_edges)
                min_ez = min(0.5 * ((mw @ e.verts[0].co).z + (mw @ e.verts[1].co).z) for e in boundary_edges)
                span_ez = max_ez - min_ez
                top_edges = [
                    e for e in boundary_edges
                    if 0.5 * ((mw @ e.verts[0].co).z + (mw @ e.verts[1].co).z) >= max_ez - max(0.05, span_ez * 0.35)
                ] or boundary_edges
                marked_edges = [e.index for e in top_edges]
                v_set = sorted({v.index for e in top_edges for v in e.verts})
                ordered_indices = _order_selected_vertex_chain(bm, v_set)
        elif is_edge_mode:
            sel_edges = [e for e in bm.edges if e.select]
            if not sel_edges:
                if obj.mode != 'EDIT':
                    bm.free()
                self.report({'WARNING'}, "ReSkate: Select at least 1 edge in Edge Select mode.")
                return {'CANCELLED'}
            marked_edges = [e.index for e in sel_edges]
            v_set = sorted({v.index for e in sel_edges for v in e.verts})
            ordered_indices = _order_selected_vertex_chain(bm, v_set)
        else:
            hist_verts = [elem.index for elem in bm.select_history if isinstance(elem, bmesh.types.BMVert)]
            sel_verts = [v.index for v in bm.verts if v.select]
            if len(sel_verts) < 2:
                if obj.mode != 'EDIT':
                    bm.free()
                self.report({'WARNING'}, "ReSkate: Select at least 2 vertices (or switch to Edge/Face Select mode).")
                return {'CANCELLED'}
            if len(sel_verts) == 2 and len(hist_verts) == 2:
                ordered_indices = _shortest_vertex_path_bmesh(bm, hist_verts[0], hist_verts[1])
            elif len(sel_verts) == 2:
                ordered_indices = _shortest_vertex_path_bmesh(bm, sel_verts[0], sel_verts[1])
            else:
                ordered_indices = _order_selected_vertex_chain(bm, sel_verts)

        # Also tag the corresponding edges on `reskate_grind` so viewport overlay and edge attributes stay synced
        if marked_edges:
            if obj.mode == 'EDIT':
                layer = bm.edges.layers.int.get("reskate_grind") or bm.edges.layers.int.new("reskate_grind")
                for e_idx in marked_edges:
                    if 0 <= e_idx < len(bm.edges):
                        bm.edges[e_idx][layer] = tag_val
                bmesh.update_edit_mesh(mesh)
            else:
                attr = mesh.attributes.get("reskate_grind") or mesh.attributes.new(name="reskate_grind", type='INT', domain='EDGE')
                for e_idx in marked_edges:
                    if 0 <= e_idx < len(attr.data):
                        attr.data[e_idx].value = tag_val

        if obj.mode != 'EDIT':
            bm.free()

        if len(ordered_indices) < 2:
            self.report({'WARNING'}, "ReSkate: Could not connect selection into a grind path.")
            return {'CANCELLED'}

        paths = obj.reskate_grind_paths
        new_item = paths.add()
        new_item.name = f"Grind Path {len(paths)}"
        new_item.grind_type = chosen_type
        new_item.radius = float(getattr(prop, "grind_tube_radius", 0.032)) if prop else 0.032
        new_item.surface = getattr(prop, "grind_surface_override", 'Metal_Solid_Grindable') if prop else 'Metal_Solid_Grindable'
        new_item.vertex_indices_csv = ",".join(str(i) for i in ordered_indices)
        new_item.is_auto_set = False
        new_item.enabled = True
        obj.reskate_grind_path_index = len(paths) - 1

        if prop:
            prop.draw_grind_overlay = True
        gpu_draw.invalidate_gpu_caches()

        self.report({'INFO'}, f"ReSkate: Added '{new_item.name}' ({len(ordered_indices) - 1} segments, type={new_item.grind_type}).")
        return {'FINISHED'}


class RESKATE_OT_remove_grind_from_selection(bpy.types.Operator):
    """Removes grind tags and path segments touching the currently selected edges, vertices, or faces"""
    bl_idname = "reskate.remove_grind_from_selection"
    bl_label = "Remove Grind from Selection"
    bl_description = "Clears grind tags and removes grind path segments on the selected edges, vertices, or faces"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}

        mesh = obj.data
        sel_vert_ids: set[int] = set()
        cleared_edges = 0

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            sel_vert_ids = {v.index for v in bm.verts if v.select}
            layer = bm.edges.layers.int.get("reskate_grind")
            if layer:
                for e in bm.edges:
                    if e.select or e.verts[0].select or e.verts[1].select:
                        if e[layer] != 0:
                            cleared_edges += 1
                        e[layer] = 0
                bmesh.update_edit_mesh(mesh)
        else:
            sel_vert_ids = {v.index for v in mesh.vertices if v.select}
            attr = mesh.attributes.get("reskate_grind")
            if attr:
                for e in mesh.edges:
                    if e.select or e.vertices[0] in sel_vert_ids or e.vertices[1] in sel_vert_ids:
                        if attr.data[e.index].value != 0:
                            cleared_edges += 1
                        attr.data[e.index].value = 0

        removed_paths = 0
        if hasattr(obj, "reskate_grind_paths") and sel_vert_ids:
            for idx in range(len(obj.reskate_grind_paths) - 1, -1, -1):
                gp = obj.reskate_grind_paths[idx]
                v_ids = [int(t.strip()) for t in gp.vertex_indices_csv.split(",") if t.strip().isdigit()]
                kept = [v for v in v_ids if v not in sel_vert_ids]
                if len(kept) < 2 and len(v_ids) >= 2:
                    obj.reskate_grind_paths.remove(idx)
                    removed_paths += 1
                elif len(kept) != len(v_ids):
                    gp.vertex_indices_csv = ",".join(str(v) for v in kept)

        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Removed grind from selection ({cleared_edges} edges, {removed_paths} paths).")
        return {'FINISHED'}


class RESKATE_OT_block_grind_on_selection(bpy.types.Operator):
    """Blocks grinds on selected faces/edges by clearing grind tags and setting the slot surface to No-Grind (Skatestopper)"""
    bl_idname = "reskate.block_grind_on_selection"
    bl_label = "Block Grind on Selection"
    bl_description = "Clears grind tags on the selection and sets the selected face's material slot to No-Grind (Skatestopper)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}

        bpy.ops.reskate.remove_grind_from_selection()

        mesh = obj.data
        sel_slots: set[int] = set()
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            for f in bm.faces:
                if f.select:
                    sel_slots.add(int(f.material_index))
        else:
            for p in mesh.polygons:
                if p.select:
                    sel_slots.add(int(p.material_index))

        if sel_slots:
            if len(obj.reskate_material_surfaces) < len(obj.material_slots):
                bpy.ops.reskate.sync_material_surfaces()
            for s_idx in sel_slots:
                if 0 <= s_idx < len(obj.reskate_material_surfaces):
                    obj.reskate_material_surfaces[s_idx].surface = "no_grind_plastic"
                    obj.reskate_material_surfaces[s_idx].feel_preset = "NO_GRIND_STOPPER"

        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, "ReSkate: Blocked grind on selection (set to No-Grind Skatestopper).")
        return {'FINISHED'}


class RESKATE_OT_remove_grind_path(bpy.types.Operator):
    """Removes a grind path entry from the active prop"""
    bl_idname = "reskate.remove_grind_path"
    bl_label = "Remove Grind Path"
    bl_description = "Deletes this grind path entry from the prop"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or not hasattr(obj, "reskate_grind_paths"):
            return {'CANCELLED'}
        paths = obj.reskate_grind_paths
        idx = self.index if self.index >= 0 else obj.reskate_grind_path_index
        if 0 <= idx < len(paths):
            paths.remove(idx)
            obj.reskate_grind_path_index = max(0, min(idx, len(paths) - 1))
        gpu_draw.invalidate_gpu_caches()
        return {'FINISHED'}


class RESKATE_OT_select_grind_path_in_edit_mode(bpy.types.Operator):
    """Switches to Edit Mode and selects the edges and vertices belonging to a Grind Path so you can tweak or straighten them immediately"""
    bl_idname = "reskate.select_grind_path_in_edit_mode"
    bl_label = "Select Grind Path in Edit Mode"
    bl_description = "Enters Edit Mode and highlights the edges/vertices of this Grind Path (on <Prop>_COL if currently editing collision, or on the prop mesh)"
    bl_options = {'REGISTER', 'UNDO'}

    path_index: IntProperty(name="Path Index", default=-1)

    def execute(self, context):
        active_raw = getattr(context, "active_object", None)
        root_obj = get_target_mesh_object(context) or active_raw
        if not root_obj or root_obj.type != 'MESH' or not hasattr(root_obj, "reskate_grind_paths"):
            self.report({'WARNING'}, "ReSkate: Select a prop mesh with Grind Paths first.")
            return {'CANCELLED'}

        paths = root_obj.reskate_grind_paths
        idx = self.path_index if self.path_index >= 0 else getattr(root_obj, "reskate_grind_path_index", 0)
        if not (0 <= idx < len(paths)):
            self.report({'WARNING'}, "ReSkate: No valid Grind Path at that index.")
            return {'CANCELLED'}

        root_obj.reskate_grind_path_index = idx
        gp = paths[idx]
        v_ids = [int(x) for x in (getattr(gp, "vertex_indices_csv", "") or "").split(",") if x.strip().isdigit()]

        # Collect world-space points along this grind path for Smart Sync selection on <Prop>_COL
        root_mw = root_obj.matrix_world
        root_verts = root_obj.data.vertices
        world_pts: list[Vector] = [root_mw @ root_verts[vi].co for vi in v_ids if 0 <= vi < len(root_verts)]

        # Smart Sync: if user is currently editing <Prop>_COL, select matching edges on <Prop>_COL; otherwise on root_obj
        is_editing_col = bool(
            active_raw
            and active_raw.type == 'MESH'
            and (active_raw.name.endswith("_COL") or getattr(getattr(active_raw, "reskate_col_prop", None), "is_col_proxy", False))
        )
        target_obj = active_raw if is_editing_col else root_obj

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        if getattr(context, "tool_settings", None) is not None:
            context.tool_settings.mesh_select_mode = (False, True, False)

        me = target_obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        for f in bm.faces:
            f.select = False
        for e in bm.edges:
            e.select = False
        for v in bm.verts:
            v.select = False

        sel_v_set: set[int] = set()
        if target_obj == root_obj and v_ids:
            for vi in v_ids:
                if 0 <= vi < len(bm.verts):
                    bm.verts[vi].select = True
                    sel_v_set.add(vi)

        if not sel_v_set and world_pts:
            t_mw = target_obj.matrix_world
            tol2 = 0.04 * 0.04
            for v in bm.verts:
                vw = t_mw @ v.co
                if any((vw - wp).length_squared <= tol2 for wp in world_pts):
                    v.select = True
                    sel_v_set.add(v.index)

        # Fallback: if this is an Auto-Detected set without vertex_indices_csv, select edges tagged with reskate_grind != 0
        sel_edge_cnt = 0
        if sel_v_set:
            # Select edges connecting consecutive v_ids or both endpoints in sel_v_set
            consec_pairs = set()
            if target_obj == root_obj and len(v_ids) >= 2:
                for k in range(len(v_ids) - 1):
                    a, b = v_ids[k], v_ids[k + 1]
                    consec_pairs.add((min(a, b), max(a, b)))
            for e in bm.edges:
                i0, i1 = e.verts[0].index, e.verts[1].index
                pair = (min(i0, i1), max(i0, i1))
                if (consec_pairs and pair in consec_pairs) or (not consec_pairs and i0 in sel_v_set and i1 in sel_v_set):
                    e.select = True
                    sel_edge_cnt += 1
            if sel_edge_cnt == 0:
                for e in bm.edges:
                    if e.verts[0].index in sel_v_set and e.verts[1].index in sel_v_set:
                        e.select = True
                        sel_edge_cnt += 1
        else:
            layer = bm.edges.layers.int.get("reskate_grind")
            if layer is not None:
                for e in bm.edges:
                    if e[layer] != 0:
                        e.select = True
                        e.verts[0].select = True
                        e.verts[1].select = True
                        sel_v_set.add(e.verts[0].index)
                        sel_v_set.add(e.verts[1].index)
                        sel_edge_cnt += 1

        bm.select_flush_mode()
        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        self.report(
            {'INFO'},
            f"ReSkate: Selected '{gp.name}' ({sel_edge_cnt} edge(s), {len(sel_v_set)} vert(s)) in Edit Mode on '{target_obj.name}'.",
        )
        return {'FINISHED'}


class RESKATE_OT_clear_grind_paths(bpy.types.Operator):
    """Clears all vertex grind paths and auto-detected grind edges on the active prop"""
    bl_idname = "reskate.clear_grind_paths"
    bl_label = "Clear All Grind Paths"
    bl_description = "Removes all vertex grind paths and clears auto-detected grind edges"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if obj and hasattr(obj, "reskate_grind_paths"):
            obj.reskate_grind_paths.clear()
            obj.reskate_grind_path_index = 0
        bpy.ops.reskate.clear_all_grind_edges()
        gpu_draw.invalidate_gpu_caches()
        return {'FINISHED'}


def _quant_pos_key(pt: Vector) -> tuple[int, int, int]:
    return (int(round(pt.x * 2000.0)), int(round(pt.y * 2000.0)), int(round(pt.z * 2000.0)))


def _detect_world_space_grind_edges(
    obj: bpy.types.Object,
    bm: bmesh.types.BMesh,
    threshold_rad: float,
    min_edge_len: float = 0.0,
    ignore_bottom_pct: float = 0.08,
    open_above: bool = False,
    clearance_h: float = 0.35,
    include_concave: bool = False,
    only_selected: bool = False,
) -> dict[int, int]:
    """
    Evaluates mesh edges in WORLD SPACE (`obj.matrix_world`) and returns `{edge_index: grind_tag}` where:
      - `1` = Hard Edge / Ledge (sharp or beveled top crease)
      - `2` = Round Rail (cylindrical / smooth pipe top ridge)
    Non-destructively welds split vertices (`dist=0.001`) on an in-memory BMesh copy so split normals
    or disconnected hard-edge faces still detect their true dihedral crease angles, then maps back to
    `bm.edges` via exact KDTree nearest-welded-vertex correspondence (immune to quantization grid drift).
    """
    from mathutils.kdtree import KDTree

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    mw = obj.matrix_world
    rot_scale = mw.to_3x3()
    try:
        normal_mat = rot_scale.inverted().transposed()
    except Exception:
        normal_mat = rot_scale

    # Create a welded in-memory copy so split-vertex hard edges have 2 linked faces
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

    # Build a KDTree of welded world-space vertices so every original vertex in `bm.verts`
    # maps deterministically to its welded vertex index in `bm_w.verts` (zero rounding boundary drift)
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

    # Optional BVHTree in world space for Open-Above clearance checks
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
        """
        Checks if `f_first_bevel` starts a 1..3 face narrow bevel/chamfer strip (total width <= 0.085m)
        bridging from upward top face `f_top` (n.z > 0.65) to a side/vertical face (n.z < 0.50)
        whose cumulative normal angle with `f_top` is >= threshold_rad.
        Returns the list of lower/intermediate longitudinal bevel edge indices to suppress if matched, else None.
        """
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

        # Skip vertical or steep edges (> ~48 deg slope) and bottom ground base perimeter edges
        if abs(edge_dir.z) > 0.75:
            continue
        mid_pt = 0.5 * (p0 + p1)
        if mid_pt.z <= ground_cutoff_z and height_span > 0.06:
            continue

        num_lf = len(edge.link_faces)
        if num_lf == 0:
            # Wireframe edge chain
            marked_w[edge.index] = 2
            continue
        if num_lf == 1:
            # Open-sheet boundary lip: if face is upward-facing and edge is on upper half of prop, allow as ledge lip
            f1 = edge.link_faces[0]
            n1 = f_norm_w[f1.index]
            c1 = f_center_w[f1.index]
            if n1.z > 0.65 and mid_pt.z >= c1.z - 0.015 and mid_pt.z > min_z + 0.30 * height_span:
                marked_w[edge.index] = 1
            continue
        if num_lf > 2:
            # Non-manifold T-junction: pick the two most upward/outward faces
            sorted_lf = sorted(edge.link_faces, key=lambda f: f_norm_w[f.index].z, reverse=True)
            f1, f2 = sorted_lf[0], sorted_lf[1]
        else:
            f1, f2 = edge.link_faces[0], edge.link_faces[1]

        n1 = f_norm_w[f1.index]
        n2 = f_norm_w[f2.index]
        c1 = f_center_w[f1.index]
        c2 = f_center_w[f2.index]

        # Open-above clearance check: offset slightly outward along average face normal and ignore self-hits on adjacent faces
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
                # Ignore hits on faces directly sharing a vertex with this edge
                adj_face_ids = {f.index for v in edge.verts for f in v.link_faces}
                if hit_f_idx not in adj_face_ids:
                    continue

        # Cross-sectional convexity check (projected perpendicular to edge_dir so sloped/triangulated rails never skew the dot product)
        d1 = (c1 - mid_pt) - edge_dir * (c1 - mid_pt).dot(edge_dir)
        d2 = (c2 - mid_pt) - edge_dir * (c2 - mid_pt).dot(edge_dir)
        if d1.length_squared > 1e-12:
            d1.normalize()
        if d2.length_squared > 1e-12:
            d2.normalize()
        cross_convex = (d2.dot(n1) + d1.dot(n2)) <= 0.08
        # Also treat any top lip bridging an upward face (max_nz > 0.55) to a lower side face (c_side.z < mid_pt.z - 0.005) as convex
        max_nz = max(n1.z, n2.z)
        min_nz = min(n1.z, n2.z)
        c_lower_z = c2.z if n1.z >= n2.z else c1.z
        top_lip_drop = (max_nz > 0.55 and min_nz < 0.65 and c_lower_z < mid_pt.z - 0.004)
        is_convex = cross_convex or top_lip_drop
        if not is_convex and not include_concave:
            continue

        angle = n1.angle(n2, 0.0)

        # Case 0: Smooth or faceted cylindrical / round rail top ridge candidate (both faces face upward n.z > 0.45)
        if n1.z > 0.45 and n2.z > 0.45 and math.radians(4.0) <= angle <= math.radians(55.0) and is_convex:
            pipe_candidates.append(edge.index)
            continue

        # Bevel-Aware Ledge Check (1 to 3 narrow bevel faces <= 0.095m bridging top face n.z > 0.65 and side face n.z < 0.50)
        if max_nz > 0.65 and min_nz >= 0.02 and angle >= math.radians(8.0):
            f_top = f1 if n1.z >= n2.z else f2
            f_bev = f2 if n1.z >= n2.z else f1
            low_edges = _trace_bevel_strip_from_top_edge(edge, f_top, f_bev)
            if low_edges is not None:
                marked_w[edge.index] = 1
                suppressed_bevel_edges.update(low_edges)
                continue

        # Case 1: Sharp ledge crease (angle >= threshold, outer top corner)
        if angle >= threshold_rad and max_nz > 0.15:
            marked_w[edge.index] = 1
            continue

        # Case 2: Single-step chamfered or photogrammetry ledge (18°+ between an upward top face n.z > 0.70 and a steeper side face)
        if angle >= math.radians(18.0) and max_nz > 0.70 and min_nz < 0.68:
            marked_w[edge.index] = 1
            continue

    # Remove any suppressed lower/intermediate bevel edges so beveled ledges get ONLY a single clean top lip path
    if suppressed_bevel_edges:
        for sb_idx in suppressed_bevel_edges:
            marked_w.pop(sb_idx, None)

    # Filter cylindrical pipe candidates to keep only the highest ridge line along each cross-section
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

    # Filter connected chains by total chain length >= min_edge_len so subdivided rails/ledges pass cleanly
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

    # Map welded edge indices back to original mesh edge indices via exact KDTree welded-vertex pairs
    marked_orig: dict[int, int] = {}
    for e_idx, tag_val in marked_w.items():
        ed = bm_w.edges[e_idx]
        w0, w1 = ed.verts[0].index, ed.verts[1].index
        pair_key = (w0, w1) if w0 <= w1 else (w1, w0)
        for orig_idx in orig_edge_map.get(pair_key, []):
            marked_orig[orig_idx] = tag_val

    bm_w.free()
    return marked_orig


class RESKATE_OT_autodetect_grind_edges(bpy.types.Operator):
    """Auto-detects grindable ledge creases and cylindrical round rail top ridges in world space (welds split vertices on a temp copy)"""
    bl_idname = "reskate.autodetect_grind_edges"
    bl_label = "Mark Edges"
    bl_description = "Evaluates face normals in world space (non-destructively welding split vertices on a temp copy), marks grindable edges, and creates selectable Grind Path entries for each detected chain"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        ui_mode = str(getattr(context.scene, "sk8_ui_mode", "SIMPLE") or "SIMPLE")
        threshold = float(getattr(prop, "crease_angle", math.radians(42.0))) if prop else math.radians(42.0)
        min_len = float(getattr(prop, "auto_min_edge_length", 0.15)) if prop else 0.15
        ignore_bot = float(getattr(prop, "auto_ignore_bottom_pct", 8.0)) / 100.0 if prop else 0.08
        if ui_mode == "SIMPLE":
            open_above = False
            clearance_h = 0.35
            inc_concave = False
            only_sel = False
        else:
            open_above = bool(getattr(prop, "auto_open_above", False)) if prop else False
            clearance_h = float(getattr(prop, "auto_clearance_height", 0.35)) if prop else 0.35
            inc_concave = bool(getattr(prop, "auto_include_concave", False)) if prop else False
            only_sel = bool(getattr(prop, "auto_only_selected", False)) if prop else False

        mesh = obj.data
        ledge_count = 0
        rail_count = 0
        detected_chains: list[tuple[int, list[int], float]] = []

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
        else:
            bm = bmesh.new()
            bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        marked = compiler_bridge.detect_world_space_grind_edges(
            obj,
            bm=bm,
            threshold_rad=threshold,
            min_edge_len=min_len,
            ignore_bottom_pct=ignore_bot,
            open_above=open_above,
            clearance_h=clearance_h,
            include_concave=inc_concave,
            only_selected=only_sel,
        )

        if obj.mode == 'EDIT':
            layer = bm.edges.layers.int.get("reskate_grind") or bm.edges.layers.int.new("reskate_grind")
            for edge in bm.edges:
                if only_sel and not edge.select:
                    continue
                val = marked.get(edge.index, 0)
                edge[layer] = val
                edge.select = bool(val > 0)
                if val == 1:
                    ledge_count += 1
                elif val == 2:
                    rail_count += 1
            bmesh.update_edit_mesh(mesh)
        else:
            attr = mesh.attributes.get("reskate_grind") or mesh.attributes.new(name="reskate_grind", type='INT', domain='EDGE')
            for edge in bm.edges:
                if only_sel and not edge.select:
                    continue
                val = marked.get(edge.index, 0)
                attr.data[edge.index].value = val
                if edge.index < len(mesh.edges):
                    mesh.edges[edge.index].select = bool(val > 0)
                if val == 1:
                    ledge_count += 1
                elif val == 2:
                    rail_count += 1

        # Group marked original edges into connected chains (welding coincident split vertices by world position)
        # so each detected lip/rail becomes a single continuous selectable Grind Path item even on split-edge FBX meshes!
        if marked:
            mw = obj.matrix_world
            pos_to_marked_e: dict[tuple[int, int, int], list[int]] = {}
            for e_idx, tag_val in marked.items():
                if 0 <= e_idx < len(bm.edges):
                    ed = bm.edges[e_idx]
                    k0 = _quant_pos_key(mw @ ed.verts[0].co)
                    k1 = _quant_pos_key(mw @ ed.verts[1].co)
                    pos_to_marked_e.setdefault(k0, []).append(e_idx)
                    pos_to_marked_e.setdefault(k1, []).append(e_idx)

            visited_orig_e: set[int] = set()
            for e_idx, tag_val in marked.items():
                if e_idx in visited_orig_e or not (0 <= e_idx < len(bm.edges)):
                    continue
                comp_edges = []
                comp_verts: set[int] = set()
                chain_len = 0.0
                stack = [e_idx]
                visited_orig_e.add(e_idx)
                while stack:
                    cur_e = stack.pop()
                    comp_edges.append(cur_e)
                    ed = bm.edges[cur_e]
                    v0_i, v1_i = ed.verts[0].index, ed.verts[1].index
                    comp_verts.add(v0_i)
                    comp_verts.add(v1_i)
                    p0_w = mw @ ed.verts[0].co
                    p1_w = mw @ ed.verts[1].co
                    chain_len += (p1_w - p0_w).length
                    for kp in (_quant_pos_key(p0_w), _quant_pos_key(p1_w)):
                        for nbr_e in pos_to_marked_e.get(kp, []):
                            if nbr_e not in visited_orig_e and marked.get(nbr_e) == tag_val:
                                visited_orig_e.add(nbr_e)
                                stack.append(nbr_e)
                ordered_v = _order_selected_vertex_chain(bm, sorted(comp_verts))
                if len(ordered_v) >= 2:
                    detected_chains.append((tag_val, ordered_v, chain_len))

        if obj.mode != 'EDIT':
            bm.free()

        total = ledge_count + rail_count
        if prop and hasattr(prop, "draw_grind_overlay"):
            prop.draw_grind_overlay = True

        # Populate `obj.reskate_grind_paths` with real selectable Grind Path entries for each detected chain
        if hasattr(obj, "reskate_grind_paths"):
            for idx in range(len(obj.reskate_grind_paths) - 1, -1, -1):
                if getattr(obj.reskate_grind_paths[idx], "is_auto_set", False):
                    obj.reskate_grind_paths.remove(idx)

            detected_chains.sort(key=lambda item: item[2], reverse=True)
            default_surf = getattr(prop, "grind_surface_override", 'Metal_Solid_Grindable') if prop else 'Metal_Solid_Grindable'
            default_rad = float(getattr(prop, "grind_tube_radius", 0.032)) if prop else 0.032
            for chain_idx, (tag_val, ordered_v, chain_len) in enumerate(detected_chains[:16]):
                auto_item = obj.reskate_grind_paths.add()
                g_type = 'ROUND_RAIL' if tag_val == 2 else 'LEDGE'
                type_title = "Rail" if g_type == 'ROUND_RAIL' else "Ledge"
                auto_item.name = f"Auto {type_title} {chain_idx + 1} ({chain_len:.2f}m)"
                auto_item.is_auto_set = True
                auto_item.grind_type = g_type
                auto_item.radius = default_rad
                auto_item.surface = default_surf
                auto_item.auto_angle_deg = round(math.degrees(threshold), 1)
                auto_item.auto_crease_deg = round(math.degrees(threshold), 1)
                auto_item.auto_min_length = min_len
                auto_item.auto_min_len = min_len
                auto_item.auto_ignore_bottom_pct = ignore_bot * 100.0
                auto_item.auto_open_above = open_above
                auto_item.auto_clearance_height = clearance_h
                auto_item.auto_include_concave = inc_concave
                auto_item.auto_only_selected = only_sel
                auto_item.vertex_indices_csv = ",".join(str(vi) for vi in ordered_v)
                auto_item.enabled = True
            if len(obj.reskate_grind_paths) > 0:
                obj.reskate_grind_path_index = len(obj.reskate_grind_paths) - 1

        gpu_draw.invalidate_gpu_caches()
        self.report(
            {'INFO'},
            f"ReSkate: Auto-detected {total} grind edges across {len(detected_chains[:16])} path(s) ({ledge_count} ledge, {rail_count} round rail).",
        )
        return {'FINISHED'}


class RESKATE_OT_auto_detect_grind_edges(bpy.types.Operator):
    """Alias operator (`reskate.auto_detect_grind_edges`) for `reskate.autodetect_grind_edges`"""
    bl_idname = "reskate.auto_detect_grind_edges"
    bl_label = "Mark Edges"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.autodetect_grind_edges()


class RESKATE_OT_recalculate_auto_grind_set(bpy.types.Operator):
    """Restores the saved Auto-Detect filter settings from an Auto-Detected Grind Set and recalculates"""
    bl_idname = "reskate.recalculate_auto_grind_set"
    bl_label = "Recalculate Auto-Detected Grinds"
    bl_description = "Restores the saved angle, minimum length, bottom cutoff, and clearance settings from this Auto-Detected Grind Set and re-runs detection"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        prop = getattr(context.scene, "reskate_prop", None)
        if not obj or not prop or not hasattr(obj, "reskate_grind_paths"):
            return {'CANCELLED'}
        idx = self.index if self.index >= 0 else obj.reskate_grind_path_index
        if 0 <= idx < len(obj.reskate_grind_paths):
            gp = obj.reskate_grind_paths[idx]
            if getattr(gp, "is_auto_set", False):
                prop.crease_angle = math.radians(float(getattr(gp, "auto_angle_deg", 38.0)))
                prop.auto_min_edge_length = float(getattr(gp, "auto_min_length", 0.25))
                prop.auto_ignore_bottom_pct = float(getattr(gp, "auto_ignore_bottom_pct", 10.0))
                prop.auto_open_above = bool(getattr(gp, "auto_open_above", True))
                prop.auto_clearance_height = float(getattr(gp, "auto_clearance_height", 0.35))
                prop.auto_include_concave = bool(getattr(gp, "auto_include_concave", False))
        return bpy.ops.reskate.autodetect_grind_edges()


class RESKATE_OT_mark_grind_edge(bpy.types.Operator):
    """Marks selected edges as grindable with the chosen grind type (Ledge, Round Rail, or Coping)"""
    bl_idname = "reskate.mark_grind_edge"
    bl_label = "Mark Selected Edges"
    bl_description = "Tags selected edges as grindable using the active Grind Type (Ledge = Orange, Round Rail = Cyan, Coping = Magenta)"
    bl_options = {'REGISTER', 'UNDO'}

    grind_type: EnumProperty(
        name="Grind Type",
        items=GRIND_GEOMETRY_TYPES,
        default='LEDGE',
    )

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        chosen_type = self.grind_type if self.properties.is_property_set("grind_type") else (getattr(prop, "grind_type", 'LEDGE') if prop else 'LEDGE')
        tag_val = 2 if chosen_type == 'ROUND_RAIL' else (3 if chosen_type == 'COPING' else 1)

        mesh = obj.data
        count = 0

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            layer = bm.edges.layers.int.get("reskate_grind") or bm.edges.layers.int.new("reskate_grind")
            for edge in bm.edges:
                if edge.select:
                    edge[layer] = tag_val
                    count += 1
            bmesh.update_edit_mesh(mesh)
        else:
            attr = mesh.attributes.get("reskate_grind")
            if not attr:
                attr = mesh.attributes.new(name="reskate_grind", type='INT', domain='EDGE')
            for edge in mesh.edges:
                if edge.select:
                    attr.data[edge.index].value = tag_val
                    count += 1

        if prop and hasattr(prop, "draw_grind_overlay"):
            prop.draw_grind_overlay = True
        gpu_draw.invalidate_gpu_caches()

        self.report({'INFO'}, f"ReSkate: Marked {count} edges as {chosen_type}.")
        return {'FINISHED'}


class RESKATE_OT_set_selected_grind_type(bpy.types.Operator):
    """Sets the Grind Type (Hard Edge / Ledge, Round Rail, or Pool Coping) on selected edges and/or the active Grind Path"""
    bl_idname = "reskate.set_selected_grind_type"
    bl_label = "Set Grind Type on Selected"
    bl_description = "Updates the grind type (Hard Edge / Ledge, Round Rail, or Coping) on selected edges or the highlighted Grind Path"
    bl_options = {'REGISTER', 'UNDO'}

    grind_type: EnumProperty(
        name="Grind Type",
        items=GRIND_GEOMETRY_TYPES,
        default='LEDGE',
    )

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        chosen_type = self.grind_type
        if prop:
            prop.grind_type = chosen_type

        tag_val = 2 if chosen_type == 'ROUND_RAIL' else (3 if chosen_type == 'COPING' else 1)
        mesh = obj.data
        edge_count = 0

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            layer = bm.edges.layers.int.get("reskate_grind") or bm.edges.layers.int.new("reskate_grind")
            for edge in bm.edges:
                if edge.select:
                    edge[layer] = tag_val
                    edge_count += 1
            bmesh.update_edit_mesh(mesh)
        else:
            attr = mesh.attributes.get("reskate_grind")
            if attr:
                for edge in mesh.edges:
                    if edge.select or attr.data[edge.index].value > 0:
                        attr.data[edge.index].value = tag_val
                        edge_count += 1

        # Also update the highlighted grind path entry if one exists
        if hasattr(obj, "reskate_grind_paths") and len(obj.reskate_grind_paths) > 0:
            idx = obj.reskate_grind_path_index
            if 0 <= idx < len(obj.reskate_grind_paths):
                obj.reskate_grind_paths[idx].grind_type = chosen_type

        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Set grind type to {chosen_type} ({edge_count} edges updated).")
        return {'FINISHED'}


class RESKATE_OT_clear_grind_edge(bpy.types.Operator):
    """Clears grindable tag from selected edges"""
    bl_idname = "reskate.clear_grind_edge"
    bl_label = "Clear Selected Edges"
    bl_description = "Clears grindable tag from currently selected edges"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        mesh = obj.data
        count = 0

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            layer = bm.edges.layers.int.get("reskate_grind")
            if layer:
                for edge in bm.edges:
                    if edge.select:
                        if edge[layer] != 0:
                            count += 1
                        edge[layer] = 0
                bmesh.update_edit_mesh(mesh)
        else:
            attr = mesh.attributes.get("reskate_grind")
            if attr:
                for edge in mesh.edges:
                    if edge.select:
                        if attr.data[edge.index].value != 0:
                            count += 1
                        attr.data[edge.index].value = 0

        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Cleared grind flag from {count} edges.")
        return {'FINISHED'}


class RESKATE_OT_clear_all_grind_edges(bpy.types.Operator):
    """Clears all grindable edge tags on the active prop mesh"""
    bl_idname = "reskate.clear_all_grind_edges"
    bl_label = "Clear All Grind Edges"
    bl_description = "Resets auto-detected grind edge tags across the active mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}

        mesh = obj.data
        count = 0

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            layer = bm.edges.layers.int.get("reskate_grind")
            if layer:
                for edge in bm.edges:
                    if edge[layer] != 0:
                        count += 1
                    edge[layer] = 0
                bmesh.update_edit_mesh(mesh)
        else:
            attr = mesh.attributes.get("reskate_grind")
            if attr:
                for item in attr.data:
                    if item.value != 0:
                        count += 1
                    item.value = 0

        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Cleared {count} grind edge tags.")
        return {'FINISHED'}


class RESKATE_OT_generate_grind_tube_proxy(bpy.types.Operator):
    """Generates an 8-sided ledge coping or 12-sided round rail proxy mesh along grind paths/edges"""
    bl_idname = "reskate.generate_grind_tube_proxy"
    bl_label = "Generate Visible Grind Tube Proxy"
    bl_description = "Builds a visible tube mesh along marked grind edges and vertex paths"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select an active mesh object first.")
            return {'CANCELLED'}

        mesh = obj.data
        attr = mesh.attributes.get("reskate_grind")
        tagged_indices = set()
        if attr:
            for i, item in enumerate(attr.data):
                if item.value == 1:
                    tagged_indices.add(i)

        bm_src = bmesh.new()
        bm_src.from_mesh(mesh)
        bm_src.verts.ensure_lookup_table()
        bm_src.edges.ensure_lookup_table()

        segments = []
        for idx in tagged_indices:
            if idx < len(bm_src.edges):
                e = bm_src.edges[idx]
                p1 = obj.matrix_world @ e.verts[0].co
                p2 = obj.matrix_world @ e.verts[1].co
                segments.append((p1, p2))

        if hasattr(obj, "reskate_grind_paths"):
            for gp in obj.reskate_grind_paths:
                if not getattr(gp, "enabled", True):
                    continue
                v_ids = [int(t) for t in gp.vertex_indices_csv.split(",") if t.strip().isdigit()]
                for k in range(len(v_ids) - 1):
                    if 0 <= v_ids[k] < len(bm_src.verts) and 0 <= v_ids[k + 1] < len(bm_src.verts):
                        p1 = obj.matrix_world @ bm_src.verts[v_ids[k]].co
                        p2 = obj.matrix_world @ bm_src.verts[v_ids[k + 1]].co
                        segments.append((p1, p2))

        bm_src.free()

        if not segments:
            self.report({'WARNING'}, "ReSkate: No grind paths or edges marked. Run Auto-Detect or Add Grind Path first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        radius = prop.grind_tube_radius if prop else 0.032
        is_round = prop.round_rail if prop else False
        sides = 12 if is_round else 8

        bm_out = bmesh.new()
        for p1, p2 in segments:
            delta = p2 - p1
            length = delta.length
            if length < 1e-5:
                continue
            tangent = delta / length
            ref = Vector((0.0, 0.0, 1.0)) if abs(tangent.z) < 0.9 else Vector((1.0, 0.0, 0.0))
            u = tangent.cross(ref).normalized()
            v = tangent.cross(u).normalized()

            ring1 = []
            ring2 = []
            for i in range(sides):
                ang = 2.0 * math.pi * i / sides
                off = (u * math.cos(ang) + v * math.sin(ang)) * radius
                ring1.append(bm_out.verts.new(p1 + off))
                ring2.append(bm_out.verts.new(p2 + off))

            for i in range(sides):
                next_i = (i + 1) % sides
                bm_out.faces.new([ring1[i], ring2[i], ring2[next_i], ring1[next_i]])

        bmesh.ops.remove_doubles(bm_out, verts=bm_out.verts[:], dist=radius * 0.4)

        proxy_name = f"{obj.name}_Grind_Proxy"
        old_proxy = context.scene.objects.get(proxy_name)
        if old_proxy:
            bpy.data.objects.remove(old_proxy, do_unlink=True)

        proxy_mesh = bpy.data.meshes.new(f"{proxy_name}_Mesh")
        bm_out.to_mesh(proxy_mesh)
        bm_out.free()

        proxy_obj = bpy.data.objects.new(proxy_name, proxy_mesh)
        context.collection.objects.link(proxy_obj)
        proxy_obj.display_type = 'WIRE'
        proxy_obj.show_wire = True
        proxy_obj.hide_render = True

        if hasattr(proxy_obj, "reskate_col_prop"):
            col_prop = proxy_obj.reskate_col_prop
            col_prop.is_col_proxy = True
            col_prop.round_rail = is_round
            col_prop.surface_material = 'material_0352'

        proxy_obj.parent = obj
        try:
            parent_inv = obj.matrix_world.inverted()
        except Exception:
            parent_inv = Matrix.Identity(4)
        proxy_obj.matrix_parent_inverse = parent_inv

        self.report({'INFO'}, f"ReSkate: Generated grind proxy '{proxy_name}' ({len(segments)} segments).")
        return {'FINISHED'}


# ==============================================================================
# Step 4: Look (Textures) Operators — Strictly Non-Destructive
# ==============================================================================

class RESKATE_OT_bake_uv_atlas(bpy.types.Operator):
    """Bakes multi-materials or tiled UVs into a NEW image on a NEW UV map (ReSkate_Atlas_UV), leaving original UVs and materials untouched"""
    bl_idname = "reskate.bake_uv_atlas"
    bl_label = "Bake Texture for Me"
    bl_description = "Bakes your materials and tiled UVs to a new ReSkate_Atlas_UV map and a new image file without altering your original UVs or materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not compiler_bridge.is_experimental_addon_enabled(context):
            self.report({'WARNING'}, "ReSkate: Built-in texture baker is archived in v0.31.0 (requires sk8_prop_experimental_addon). Author game-ready Principled BSDF materials directly.")
            return {'CANCELLED'}
        start_time = time.time()
        obj = get_target_mesh_object(context)
        if not obj:
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        if context.view_layer.objects.active != obj:
            context.view_layer.objects.active = obj
        obj.select_set(True)

        prop = getattr(context.scene, "reskate_prop", None)
        resolution_str = str(getattr(prop, "atlas_resolution", '1024') or '1024')
        resolution = 4096 if resolution_str == '4096' else (2048 if resolution_str == '2048' else 1024)
        island_margin = getattr(prop, "island_margin", 0.015)
        slug = get_prop_slug(context, obj)

        # 1. Snapshot original active UV layer, active render UV layer, and materials so they are restored 100% intact
        orig_active_uv_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
        orig_render_uv_name = None
        for uvl in obj.data.uv_layers:
            if uvl.active_render:
                orig_render_uv_name = uvl.name
                break

        # 2. Create or reuse the dedicated secondary UV map `ReSkate_Atlas_UV`
        if "ReSkate_Atlas_UV" not in obj.data.uv_layers:
            atlas_uv = obj.data.uv_layers.new(name="ReSkate_Atlas_UV")
        else:
            atlas_uv = obj.data.uv_layers["ReSkate_Atlas_UV"]

        obj.data.uv_layers.active = atlas_uv

        # 3. Unwrap ONLY the new `ReSkate_Atlas_UV` layer into [0.0, 1.0]
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(
            angle_limit=math.radians(66.0),
            island_margin=island_margin,
            area_weight=0.0,
            correct_aspect=True,
            scale_to_bounds=True,
        )
        bpy.ops.object.mode_set(mode='OBJECT')

        # 4. Set `ReSkate_Atlas_UV` as target for bake while keeping original UV active for sampling
        atlas_uv = obj.data.uv_layers["ReSkate_Atlas_UV"]
        atlas_uv.active_render = True
        if orig_active_uv_name and orig_active_uv_name in obj.data.uv_layers:
            obj.data.uv_layers[orig_active_uv_name].active = True

        # 5. Create or retrieve the baked image datablock
        img_name = f"ReSkate_Atlas_{slug}"
        atlas_img = bpy.data.images.get(img_name)
        if not atlas_img or atlas_img.size[0] != resolution or atlas_img.size[1] != resolution:
            if atlas_img:
                bpy.data.images.remove(atlas_img)
            atlas_img = bpy.data.images.new(name=img_name, width=resolution, height=resolution, alpha=True)
            atlas_img.colorspace_settings.name = 'sRGB'

        # 6. Snapshot if mesh had zero materials so we clean up any temporary material after baking
        had_no_materials = (len(obj.data.materials) == 0)
        temp_fallback_mat = None
        if had_no_materials:
            temp_fallback_mat = bpy.data.materials.new(name="ReSkate_Temp_Bake_Mat")
            temp_fallback_mat.use_nodes = True
            obj.data.materials.append(temp_fallback_mat)

        # 7. Inject temporary image nodes for Cycles baking, then remove them immediately after
        temp_nodes = []
        prev_active_nodes = []
        for mat in obj.data.materials:
            if not mat:
                continue
            if not mat.node_tree:
                mat.use_nodes = True
            prev_active_nodes.append((mat, mat.node_tree.nodes.active))
            tex_node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            tex_node.name = TEMP_BAKE_NODE_NAME
            tex_node.image = atlas_img
            # Explicitly bind target UV map to ReSkate_Atlas_UV so original UV is sampled and ReSkate_Atlas_UV is written
            uv_node = mat.node_tree.nodes.new("ShaderNodeUVMap")
            uv_node.name = f"{TEMP_BAKE_NODE_NAME}_UV"
            uv_node.uv_map = "ReSkate_Atlas_UV"
            mat.node_tree.links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])
            mat.node_tree.nodes.active = tex_node
            tex_node.select = True
            temp_nodes.append((mat, tex_node, uv_node))

        # 8. Snapshot and configure Cycles bake settings
        scene = context.scene
        prev_engine = scene.render.engine
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 1
        scene.cycles.bake_type = 'DIFFUSE'
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
        scene.render.bake.use_pass_color = True
        scene.render.bake.margin = 8
        scene.render.bake.margin_type = 'EXTEND'
        scene.render.bake.target = 'IMAGE_TEXTURES'

        prev_sel = [o for o in context.selected_objects]
        for o in context.scene.objects:
            o.select_set(False)
        context.view_layer.objects.active = obj
        obj.select_set(True)

        try:
            bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
        except Exception as e:
            self.report({'ERROR'}, f"ReSkate: Bake failed: {e}")
            return {'CANCELLED'}
        finally:
            # Clean up all temporary bake nodes and restore original active nodes, materials, and UV selection!
            for mat, tex_node, uv_node in temp_nodes:
                try:
                    mat.node_tree.nodes.remove(tex_node)
                except Exception:
                    pass
                try:
                    mat.node_tree.nodes.remove(uv_node)
                except Exception:
                    pass
            for mat, act_n in prev_active_nodes:
                try:
                    mat.node_tree.nodes.active = act_n
                except Exception:
                    pass
            if had_no_materials and temp_fallback_mat:
                obj.data.materials.clear()
                bpy.data.materials.remove(temp_fallback_mat)
            if orig_active_uv_name and orig_active_uv_name in obj.data.uv_layers:
                obj.data.uv_layers[orig_active_uv_name].active = True
            if orig_render_uv_name and orig_render_uv_name in obj.data.uv_layers:
                obj.data.uv_layers[orig_render_uv_name].active_render = True
            scene.render.engine = prev_engine
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)

        # Save baked atlas PNG to staging
        staging_dir = DEFAULT_STAGING_DIR
        staging_dir.mkdir(parents=True, exist_ok=True)
        out_png = staging_dir / f"{img_name}.png"
        atlas_img.filepath_raw = str(out_png)
        atlas_img.file_format = 'PNG'
        atlas_img.save()

        if prop:
            prop.look_mode = 'BAKE'
            prop.custom_texture_path = str(out_png)

        elapsed = time.time() - start_time
        self.report({'INFO'}, f"ReSkate: Baked {resolution}x{resolution} texture to {out_png.name} in {elapsed:.2f}s (original UVs & materials untouched).")
        return {'FINISHED'}


class RESKATE_OT_preview_baked_atlas(bpy.types.Operator):
    """Toggles between original materials and the baked texture preview in the viewport"""
    bl_idname = "reskate.preview_baked_atlas"
    bl_label = "Toggle Before / After Bake Preview"
    bl_description = "Toggles between your original materials and a preview of the baked texture (click again to restore originals)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not compiler_bridge.is_experimental_addon_enabled(context):
            self.report({'WARNING'}, "ReSkate: Built-in texture baker is archived in v0.31.0 (requires sk8_prop_experimental_addon).")
            return {'CANCELLED'}
        obj = get_target_mesh_object(context)
        if not obj:
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object to preview.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        slug = get_prop_slug(context, obj)
        img_name = f"ReSkate_Atlas_{slug}"

        # If currently in preview mode -> Restore original materials and UV layers
        if len(obj.data.materials) == 1 and obj.data.materials[0] and obj.data.materials[0].name == PREVIEW_MATERIAL_NAME:
            orig_mat_names = obj.get("_reskate_orig_materials", [])
            orig_indices = obj.get("_reskate_orig_poly_mat_indices", [])
            orig_uv_name = obj.get("_reskate_orig_active_uv", "")
            obj.data.materials.clear()
            for m_name in orig_mat_names:
                mat = bpy.data.materials.get(m_name)
                if mat:
                    obj.data.materials.append(mat)
            if orig_indices and len(orig_indices) == len(obj.data.polygons):
                for p, idx in zip(obj.data.polygons, orig_indices):
                    p.material_index = idx
            if orig_uv_name and orig_uv_name in obj.data.uv_layers:
                obj.data.uv_layers[orig_uv_name].active = True
                obj.data.uv_layers[orig_uv_name].active_render = True
            for k in ("_reskate_orig_materials", "_reskate_orig_poly_mat_indices", "_reskate_orig_active_uv"):
                if k in obj:
                    del obj[k]
            self.report({'INFO'}, "ReSkate: Restored original materials and UVs.")
            return {'FINISHED'}

        atlas_img = bpy.data.images.get(img_name)
        if not atlas_img and prop and prop.custom_texture_path and os.path.exists(prop.custom_texture_path):
            try:
                atlas_img = bpy.data.images.load(prop.custom_texture_path)
            except Exception:
                pass
        if not atlas_img:
            staged_path = DEFAULT_STAGING_DIR / f"{img_name}.png"
            if staged_path.exists():
                try:
                    atlas_img = bpy.data.images.load(str(staged_path))
                except Exception:
                    pass

        if not atlas_img or "ReSkate_Atlas_UV" not in obj.data.uv_layers:
            self.report({'WARNING'}, "ReSkate: No baked texture found yet. Click 'Bake Texture for Me' first.")
            return {'CANCELLED'}

        obj["_reskate_orig_materials"] = [m.name for m in obj.data.materials if m]
        obj["_reskate_orig_poly_mat_indices"] = [p.material_index for p in obj.data.polygons]
        obj["_reskate_orig_active_uv"] = obj.data.uv_layers.active.name if obj.data.uv_layers.active else ""

        prev_mat = bpy.data.materials.get(PREVIEW_MATERIAL_NAME) or bpy.data.materials.new(PREVIEW_MATERIAL_NAME)
        prev_mat.use_nodes = True
        nodes = prev_mat.node_tree.nodes
        links = prev_mat.node_tree.links
        nodes.clear()

        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = "ReSkate_Atlas_UV"
        uv_node.location = (-600, 0)

        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.image = atlas_img
        tex_node.location = (-350, 0)

        bsdf_node = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf_node.location = (0, 0)
        roughness = getattr(prop, "dielectric_roughness", 0.45) if prop else 0.45
        specular = getattr(prop, "dielectric_specular", 0.50) if prop else 0.50
        bsdf_node.inputs["Roughness"].default_value = roughness
        if "Specular IOR Level" in bsdf_node.inputs:
            bsdf_node.inputs["Specular IOR Level"].default_value = specular
        elif "Specular" in bsdf_node.inputs:
            bsdf_node.inputs["Specular"].default_value = specular

        out_node = nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)

        links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])
        links.new(tex_node.outputs["Color"], bsdf_node.inputs["Base Color"])
        links.new(bsdf_node.outputs["BSDF"], out_node.inputs["Surface"])

        obj.data.materials.clear()
        obj.data.materials.append(prev_mat)
        obj.data.uv_layers["ReSkate_Atlas_UV"].active = True
        obj.data.uv_layers["ReSkate_Atlas_UV"].active_render = True

        self.report({'INFO'}, "ReSkate: Showing baked texture preview. Click again to restore original materials.")
        return {'FINISHED'}


class RESKATE_OT_sync_shader_to_sk8_material(bpy.types.Operator):
    """Auto-syncs Blender Shader nodes (Principled BSDF + Separate Color/RGB + Emission) into Material > Skate Map"""
    bl_idname = "reskate.sync_shader_to_sk8_material"
    bl_label = "Sync from Blender Shader Nodes"
    bl_description = "Pulls Base Color, Normal, Roughness/Metallic (including Separate Color R/G/B channels), Emission, and Opacity from Principled BSDF into Material > Skate Map"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a mesh object first.")
            return {'CANCELLED'}

        synced = 0
        synced_details = []
        for idx, slot in enumerate(obj.material_slots):
            mat = slot.material
            if mat is not None:
                for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
                    if holder is not None and hasattr(holder, "use_custom_texture_overrides"):
                        try:
                            holder.use_custom_texture_overrides = False
                        except Exception:
                            pass
                if hasattr(compiler_bridge, "heal_corrupted_reskate_user_override_nodes"):
                    compiler_bridge.heal_corrupted_reskate_user_override_nodes(mat, obj=obj, slot_index=idx)
                info = compiler_bridge.sync_material_from_shader_nodes(mat, obj=obj, slot_index=idx, force=True)
                bc_nm = getattr(info.get("base_color_image"), "name", "Flat") if info.get("base_color_image") else "Flat"
                synced_details.append(f"{mat.name}: {bc_nm}")
                synced += 1

        # Clear any stale *_pbr_cache.json files in Pack_Queue so next compile re-extracts fresh textures
        try:
            stg_dir = compiler_bridge.get_staging_dir(context)
            pq_dir = stg_dir / "Pack_Queue"
            if pq_dir.exists():
                for cf in pq_dir.glob("*_pbr_cache.json"):
                    try:
                        cf.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        compiler_bridge.invalidate_ui_caches()
        try:
            for win in context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type in ('VIEW_3D', 'PROPERTIES', 'NODE_EDITOR'):
                        area.tag_redraw()
        except Exception:
            pass

        detail_str = f" ({', '.join(synced_details[:3])})" if synced_details else ""
        self.report({'INFO'}, f"ReSkate: Synced {synced} material(s) from live shader nodes{detail_str}.")
        return {'FINISHED'}


class RESKATE_OT_apply_look_preset(bpy.types.Operator):
    """Applies a 1-click Look Preset (Matte, Satin, Glossy, Metal) to the active prop and its unmapped Principled BSDF materials"""
    bl_idname = "reskate.apply_look_preset"
    bl_label = "Apply Look Preset"
    bl_description = "Applies Matte, Satin, Glossy, or Metal Roughness & Metallic values to the prop and its Principled BSDF material slots"
    bl_options = {'REGISTER', 'UNDO'}

    preset: StringProperty(name="Preset", default="MATTE")

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        p_id = (self.preset or "MATTE").upper()
        p_vals = LOOK_PRESETS.get(p_id)
        if not prop or not p_vals:
            return {'CANCELLED'}

        r_val = float(p_vals["roughness"])
        m_val = float(p_vals["metallic"])
        s_val = float(p_vals["specular"])

        prop.dielectric_roughness = r_val
        prop.dielectric_metallic = m_val
        prop.dielectric_specular = s_val
        prop.look_preset = p_id

        obj = get_target_mesh_object(context) or context.active_object
        target_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not target_objs and obj and obj.type == 'MESH':
            target_objs = [obj]

        updated_mats = 0
        for t_obj in target_objs:
            if not t_obj or t_obj.type != 'MESH':
                continue
            compiler_bridge.ensure_all_object_materials_unique(t_obj)
            for slot in t_obj.material_slots:
                mat = slot.material
                if not mat or not mat.use_nodes or not mat.node_tree:
                    continue
                p_node = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                if p_node:
                    r_sock = p_node.inputs.get("Roughness")
                    m_sock = p_node.inputs.get("Metallic")
                    if r_sock is not None:
                        r_sock.default_value = r_val
                    if m_sock is not None:
                        m_sock.default_value = m_val
                    updated_mats += 1
            if t_obj != obj:
                st_t = dict(get_or_init_object_prop_state(t_obj))
                st_t["dielectric_roughness"] = r_val
                st_t["dielectric_metallic"] = m_val
                st_t["dielectric_specular"] = s_val
                st_t["look_preset"] = p_id
                ser = json.dumps(st_t)
                t_obj["reskate_prop_state"] = ser
                t_obj["reskate_prop_state_json"] = ser

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Applied '{p_id.title()}' look preset (Roughness={r_val:.2f}, Metallic={m_val:.2f}, {updated_mats} mat(s) updated).")
        return {'FINISHED'}


class RESKATE_OT_toggle_roughness_invert(bpy.types.Operator):
    """1-click shader-synced toggle inserting or removing a ReSkate_Roughness_Invert node right before Principled BSDF Roughness"""
    bl_idname = "reskate.toggle_roughness_invert"
    bl_label = "Invert Roughness Map (Gloss ↔ Roughness)"
    bl_description = "Inserts or removes a 'ReSkate_Roughness_Invert' node right before Principled BSDF Roughness so both the Blender viewport and exported in-game _msk.png flip Gloss ↔ Roughness in 100% sync"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        mat = None
        if obj and obj.type == 'MESH':
            mat = obj.active_material
            if mat is None and len(obj.material_slots) > 0:
                mat = obj.material_slots[0].material
        if mat is None and getattr(context, "material", None) is not None:
            mat = context.material
        if not mat or not mat.use_nodes or not mat.node_tree:
            self.report({'WARNING'}, "ReSkate: Select a mesh with a node-based material first.")
            return {'CANCELLED'}

        nt = mat.node_tree
        p_node = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not p_node or "Roughness" not in p_node.inputs:
            self.report({'WARNING'}, f"ReSkate: Material '{mat.name}' has no Principled BSDF Roughness input.")
            return {'CANCELLED'}

        r_sock = p_node.inputs["Roughness"]
        # If ReSkate_Roughness_Mix is sitting directly in front of Roughness, inspect its Value input
        target_sock = r_sock
        if r_sock.is_linked and r_sock.links:
            first_node = r_sock.links[0].from_node
            if first_node.name == "ReSkate_Roughness_Mix" and first_node.type == "MAP_RANGE" and "Value" in first_node.inputs:
                target_sock = first_node.inputs["Value"]

        inv_node = nt.nodes.get("ReSkate_Roughness_Invert")
        # Check if ReSkate_Roughness_Invert is currently active in the Roughness chain
        is_currently_inverted = False
        if target_sock.is_linked and target_sock.links:
            upstream_node = target_sock.links[0].from_node
            if upstream_node.type == "INVERT" and (upstream_node.name == "ReSkate_Roughness_Invert" or not upstream_node.mute):
                inv_node = upstream_node
                is_currently_inverted = True

        if is_currently_inverted and inv_node is not None:
            # Remove/bypass the Invert node and reconnect its upstream Color input directly to target_sock
            col_in = inv_node.inputs.get("Color")
            if col_in and col_in.is_linked and col_in.links:
                orig_from_sock = col_in.links[0].from_socket
                nt.links.new(orig_from_sock, target_sock)
            try:
                nt.nodes.remove(inv_node)
            except Exception:
                inv_node.mute = True
            state_lbl = "Standard Roughness (Invert OFF)"
        else:
            if not target_sock.is_linked or not target_sock.links:
                # If no map is linked yet, flip scalar roughness 1.0 - r as fallback
                r_sock.default_value = max(0.0, min(1.0, 1.0 - float(r_sock.default_value)))
                compiler_bridge.invalidate_ui_caches()
                self.report({'INFO'}, f"ReSkate: Inverted scalar Roughness to {r_sock.default_value:.2f}.")
                return {'FINISHED'}

            orig_from_sock = target_sock.links[0].from_socket
            if inv_node is None or inv_node.type != "INVERT":
                inv_node = nt.nodes.new("ShaderNodeInvert")
                inv_node.name = "ReSkate_Roughness_Invert"
                inv_node.label = "ReSkate Invert Roughness"
            inv_node.mute = False
            if "Fac" in inv_node.inputs:
                inv_node.inputs["Fac"].default_value = 1.0
            inv_node.location = (p_node.location.x - 240, p_node.location.y - 160)
            nt.links.new(orig_from_sock, inv_node.inputs["Color"])
            nt.links.new(inv_node.outputs["Color"], target_sock)
            state_lbl = "Inverted Gloss → Roughness (Invert ON)"

        compiler_bridge.invalidate_ui_caches()
        try:
            from . import ui_panels
            ui_panels.invalidate_panel_caches()
        except Exception:
            pass
        if context.screen:
            for area in context.screen.areas:
                if area.type in ('VIEW_3D', 'PROPERTIES', 'NODE_EDITOR'):
                    area.tag_redraw()

        self.report({'INFO'}, f"ReSkate: '{mat.name}' Roughness Map set to {state_lbl}.")
        return {'FINISHED'}


class RESKATE_OT_create_lights_from_emission(bpy.types.Operator):
    """Clusters contiguous emissive faces on the mesh and spawns up to 4 linked dynamic lights"""
    bl_idname = "reskate.create_lights_from_emission"
    bl_label = "Create Light at Emissive Faces (Max 4)"
    bl_description = "Clusters contiguous emissive faces on this prop and spawns 1–4 linked dynamic lights at their centers (never spawns 100 lights)"
    bl_options = {'REGISTER', 'UNDO'}

    max_lights: IntProperty(
        name="Max Lights", default=4, min=1, max=4,
        description="Maximum number of lights to spawn across emissive face clusters (strictly capped at 4)",
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or len(obj.data.polygons) == 0:
            self.report({'WARNING'}, "ReSkate: Select a mesh object with faces first.")
            return {'CANCELLED'}

        # Find which material slots have emission enabled or named emissive/light/bulb/neon
        emissive_slots: dict[int, tuple[tuple[float, float, float], float]] = {}
        for idx, slot in enumerate(obj.material_slots):
            mat = slot.material
            if not mat:
                continue
            info = compiler_bridge.inspect_material_shader_nodes(mat)
            ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
            em_str = max(
                float(info.get("emission_strength", 0.0)),
                float(getattr(ms, "emission_strength", 0.0)) if ms else 0.0,
            )
            em_col = info.get("emission_color") or (list(getattr(ms, "emission_color", (1.0, 0.9, 0.75))) if ms else [1.0, 0.9, 0.75])
            has_em_img = (info.get("emission_image") is not None) or (getattr(ms, "emission_image", None) is not None if ms else False)
            name_low = mat.name.lower()
            is_named_emissive = any(k in name_low for k in ("emiss", "emit", "bulb", "lamp", "neon", "glow", "led", "light_glass", "lens"))
            if em_str > 0.01 or has_em_img or is_named_emissive:
                col_rgb = (
                    max(0.1, float(em_col[0])),
                    max(0.1, float(em_col[1])),
                    max(0.1, float(em_col[2])),
                ) if max(em_col) > 0.05 else (1.0, 0.92, 0.78)
                emissive_slots[idx] = (col_rgb, max(1.0, em_str))

        mesh = obj.data
        mw = obj.matrix_world
        rot_scale = mw.to_3x3()
        try:
            normal_mat = rot_scale.inverted().transposed()
        except Exception:
            normal_mat = rot_scale

        # Collect candidate faces (either faces using emissive_slots, or selected faces if no slot is explicitly tagged)
        cand_polys = [p for p in mesh.polygons if p.material_index in emissive_slots]
        if not cand_polys:
            cand_polys = [p for p in mesh.polygons if p.select]
        if not cand_polys and len(obj.material_slots) <= 1:
            # Fallback: top upward/outward brightest faces or whole object single cluster
            cand_polys = list(mesh.polygons)

        if not cand_polys:
            self.report({'WARNING'}, "ReSkate: No emissive material slots or selected faces found on this prop.")
            return {'CANCELLED'}

        # Cluster contiguous candidate faces via shared vertices
        poly_by_idx = {p.index: p for p in cand_polys}
        vert_to_polys: dict[int, list[int]] = {}
        for p in cand_polys:
            for vi in p.vertices:
                vert_to_polys.setdefault(vi, []).append(p.index)

        visited: set[int] = set()
        clusters: list[list[int]] = []
        for p in cand_polys:
            if p.index in visited:
                continue
            comp = []
            stack = [p.index]
            visited.add(p.index)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for vi in poly_by_idx[cur].vertices:
                    for nbr in vert_to_polys.get(vi, []):
                        if nbr not in visited:
                            visited.add(nbr)
                            stack.append(nbr)
            clusters.append(comp)

        # Compute world center, area-weighted normal, total area, and color for each cluster
        cluster_infos = []
        for comp in clusters:
            total_area = 0.0
            center_sum = Vector((0.0, 0.0, 0.0))
            norm_sum = Vector((0.0, 0.0, 0.0))
            slot_idx = poly_by_idx[comp[0]].material_index
            for pid in comp:
                poly = poly_by_idx[pid]
                w_area = max(1e-5, float(poly.area))
                w_center = mw @ poly.center
                w_norm = (normal_mat @ poly.normal)
                if w_norm.length_squared > 1e-10:
                    w_norm.normalize()
                else:
                    w_norm = Vector((0.0, 0.0, -1.0))
                center_sum += w_center * w_area
                norm_sum += w_norm * w_area
                total_area += w_area

            c_world = center_sum / total_area
            n_world = norm_sum.normalized() if norm_sum.length_squared > 1e-8 else Vector((0.0, 0.0, -1.0))
            col_rgb, em_s = emissive_slots.get(slot_idx, ((1.0, 0.92, 0.78), 1.0))
            cluster_infos.append({
                "center": c_world,
                "normal": n_world,
                "area": total_area,
                "color": col_rgb,
                "strength": em_s,
            })

        # Sort by area descending and strictly cap at max_lights (1..4)
        cluster_infos.sort(key=lambda x: x["area"], reverse=True)
        cap = max(1, min(4, int(self.max_lights)))
        chosen = cluster_infos[:cap]

        scene = context.scene
        spawned_names = []
        for i, ci in enumerate(chosen):
            base_name = f"{obj.name}_EmissiveLight_{i + 1:02d}"
            light_name = base_name
            suffix = 1
            while light_name in scene.objects:
                light_name = f"{base_name}_{suffix}"
                suffix += 1

            light_data = bpy.data.lights.new(name=light_name, type='SPOT')
            light_data.energy = min(200.0, max(25.0, 45.0 * float(ci["strength"])))
            light_data.color = ci["color"]
            light_data.use_custom_distance = True
            light_data.cutoff_distance = 25.0
            light_data.use_shadow = False
            light_data.spot_size = math.radians(105.0)
            light_data.spot_blend = 0.45

            light_obj = bpy.data.objects.new(name=light_name, object_data=light_data)
            light_obj["reskate_shadow_initialized"] = 1
            scene.collection.objects.link(light_obj)

            # Position slightly in front of the emissive surface and point along face normal (-Z tracks normal)
            n_dir = ci["normal"]
            light_obj.location = ci["center"] + n_dir * 0.04
            up_axis = 'Y' if abs(n_dir.y) < 0.9 else 'X'
            light_obj.rotation_euler = n_dir.to_track_quat('-Z', up_axis).to_euler()

            light_obj.parent = obj
            try:
                light_obj.matrix_parent_inverse = obj.matrix_world.inverted()
            except Exception:
                light_obj.matrix_parent_inverse = Matrix.Identity(4)

            if hasattr(obj, "reskate_linked_lights"):
                slot = obj.reskate_linked_lights.add()
                slot.light_obj = light_obj
                slot.enabled = True
                obj.reskate_linked_light_index = len(obj.reskate_linked_lights) - 1
            spawned_names.append(light_name)

        self.report(
            {'INFO'},
            f"ReSkate: Spawned {len(spawned_names)} light(s) from {len(cluster_infos)} emissive cluster(s) (capped at {cap}).",
        )
        return {'FINISHED'}


# ==============================================================================
# Step 5: Light (in-game) Operators — Explicit Linking + Native Blender Lights
# ==============================================================================

class RESKATE_OT_add_dynamic_prop_light(bpy.types.Operator):
    """Creates a native Blender Spot or Point light, lets you place & aim it interactively on the prop surface (or spawns directly in headless mode), and links it to the active prop"""
    bl_idname = "reskate.add_dynamic_prop_light"
    bl_label = "Add Light"
    bl_description = (
        "Click to interactively place & aim a Spot or Point light on your prop surface "
        "(Move Mouse: Aim along smoothed surface normal | F: Flip Outward/Inward | Scroll: Offset Distance | Left-Click: Place | Right-Click/ESC: Cancel)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    light_type: EnumProperty(
        name="Light Type",
        items=[('SPOT', "Spot Light", "Directional cone light"), ('POINT', "Point Light", "Omnidirectional fill light")],
        default='SPOT',
    )
    interactive_aim: BoolProperty(
        name="Interactive Surface Aim",
        default=True,
        description="Immediately links the light to the selected prop and lets you hover over the prop in the 3D Viewport to align it smoothly to the surface normal",
    )

    def _redraw_all_view3d_and_ui(self, context):
        try:
            from . import ui_panels
            ui_panels.invalidate_panel_caches()
        except Exception:
            pass
        try:
            compiler_bridge.invalidate_ui_caches()
            gpu_draw.invalidate_gpu_caches()
        except Exception:
            pass
        try:
            wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
            if wm:
                for win in wm.windows:
                    if win.screen:
                        for area in win.screen.areas:
                            if area.type in ('VIEW_3D', 'PROPERTIES', 'OUTLINER'):
                                area.tag_redraw()
                                for reg in area.regions:
                                    reg.tag_redraw()
        except Exception:
            pass

    def _create_and_link_light(self, context):
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        target_mesh = get_target_mesh_object(context)
        if target_mesh is None or getattr(target_mesh, "type", None) != 'MESH':
            act = getattr(context, "active_object", None)
            if act is not None and getattr(act, "type", None) == 'MESH':
                target_mesh = act

        l_type = self.light_type if self.properties.is_property_set("light_type") else (getattr(prop, "light_type", 'SPOT') if prop else 'SPOT')
        base_name = f"{target_mesh.name}_Light" if (target_mesh and target_mesh.type == 'MESH') else "ReSkate_Dynamic_Light"

        idx = 1
        light_name = base_name
        while light_name in scene.objects:
            light_name = f"{base_name}_{idx:02d}"
            idx += 1

        light_data = bpy.data.lights.new(name=light_name, type=l_type)
        light_data.energy = 51.83
        light_data.color = (1.0, 0.70, 0.50)
        light_data.use_custom_distance = True
        light_data.cutoff_distance = 30.0
        light_data.use_shadow = False
        if l_type == 'SPOT':
            light_data.spot_size = math.radians(100.0)  # 50 deg half-angle
            light_data.spot_blend = 0.40

        light_obj = bpy.data.objects.new(name=light_name, object_data=light_data)
        light_obj["reskate_shadow_initialized"] = 1
        try:
            properties._KNOWN_LIGHT_PTRS.add(light_obj.as_pointer())
        except Exception:
            pass
        scene.collection.objects.link(light_obj)

        if target_mesh and target_mesh.type == 'MESH':
            light_obj["reskate_owner_prop"] = target_mesh.name
            bbox = [target_mesh.matrix_world @ Vector(c) for c in target_mesh.bound_box]
            top_z = max(v.z for v in bbox)
            cx = sum(v.x for v in bbox) / 8.0
            cy = sum(v.y for v in bbox) / 8.0
            light_obj.location = Vector((cx, cy, top_z + 0.50))
            light_obj.rotation_euler = (0.0, 0.0, 0.0)
            light_obj.parent = target_mesh
            try:
                light_obj.matrix_parent_inverse = target_mesh.matrix_world.inverted()
            except Exception:
                light_obj.matrix_parent_inverse = Matrix.Identity(4)

            if hasattr(target_mesh, "reskate_linked_lights"):
                existing = [item.light_obj for item in target_mesh.reskate_linked_lights if item.light_obj is not None]
                if light_obj not in existing:
                    slot = target_mesh.reskate_linked_lights.add()
                    slot.light_obj = light_obj
                    slot.enabled = True
                    target_mesh.reskate_linked_light_index = len(target_mesh.reskate_linked_lights) - 1
            try:
                if context.view_layer and context.view_layer.objects.active != target_mesh:
                    target_mesh.select_set(True)
                    context.view_layer.objects.active = target_mesh
            except Exception:
                pass
            if prop is not None:
                try:
                    prop["_active_synced_obj_name"] = target_mesh.name
                except Exception:
                    pass
        else:
            light_obj.location = Vector((0.0, 0.0, 2.0))

        self._redraw_all_view3d_and_ui(context)
        return light_obj, target_mesh, l_type

    def _raycast_smoothed_surface(self, context, event, region, rv3d):
        """
        Raycasts from cursor `(x, y)` inside the 3D Viewport `WINDOW` region (plus a 4-sample 4px cross kernel)
        into the active prop mesh to compute a stabilized, smoothed surface normal.
        """
        from bpy_extras import view3d_utils

        # Always compute coordinates relative to `region` (the 3D Viewport WINDOW region),
        # because `event.mouse_region_x` is relative to the N-panel UI region when clicked from the sidebar!
        rx = int(event.mouse_x) - int(region.x)
        ry = int(event.mouse_y) - int(region.y)
        if rx < 0 or ry < 0 or rx > int(region.width) or ry > int(region.height):
            return False, None, None
        coord = (rx, ry)

        target_mesh = getattr(self, "_target_mesh", None)
        depsgraph = context.evaluated_depsgraph_get()

        def _single_cast(c2d):
            ro = view3d_utils.region_2d_to_origin_3d(region, rv3d, c2d)
            rd = view3d_utils.region_2d_to_vector_3d(region, rv3d, c2d).normalized()
            if target_mesh is not None and target_mesh.type == 'MESH' and target_mesh.name in context.scene.objects:
                try:
                    mw = target_mesh.matrix_world
                    mw_inv = mw.inverted()
                    ro_l = mw_inv @ ro
                    rd_l = (mw_inv.to_3x3() @ rd).normalized()
                    ok, loc_l, norm_l, f_idx = target_mesh.ray_cast(ro_l, rd_l)
                    if ok:
                        loc_w = mw @ loc_l
                        norm_w = (mw.to_3x3().inverted().transposed() @ norm_l).normalized()
                        return True, loc_w, norm_w, f_idx, target_mesh
                except Exception:
                    pass
            try:
                ok, loc_w, norm_w, f_idx, hit_obj, _mat = context.scene.ray_cast(depsgraph, ro, rd)
                if (
                    ok
                    and hit_obj is not None
                    and hit_obj != getattr(self, "_preview_light", None)
                    and not is_collision_helper_object(hit_obj)
                    and not bool(hit_obj.get("reskate_is_scale_reference", False))
                    and not hit_obj.name.startswith("_ReSkate_")
                ):
                    return True, loc_w, norm_w.normalized(), f_idx, hit_obj
            except Exception:
                pass
            return False, None, None, -1, None

        ok, hit_loc, hit_norm, face_idx, hit_obj = _single_cast(coord)
        if not ok or hit_loc is None or hit_norm is None:
            return False, None, None
        if hit_obj is not None:
            resolved_hit = resolve_collision_to_owner_prop(hit_obj) or (hit_obj if getattr(hit_obj, "type", None) == 'MESH' else None)
            if resolved_hit is not None:
                self._last_hit_mesh = resolved_hit

        accum_norm = hit_norm * 2.0
        # Blend with adjacent polygon normals on the hit mesh if available
        if hit_obj is not None and getattr(hit_obj, "type", None) == 'MESH' and hit_obj.data and 0 <= face_idx < len(hit_obj.data.polygons):
            try:
                polys = hit_obj.data.polygons
                hit_poly = polys[face_idx]
                hit_verts = set(hit_poly.vertices)
                n_mat = hit_obj.matrix_world.to_3x3().inverted().transposed()
                neighbor_cnt = 0
                v2p_cache = getattr(self, "_vert_to_polys", None) if (hit_obj == target_mesh) else None
                if v2p_cache is not None:
                    seen_p = {face_idx}
                    for v_idx in hit_verts:
                        for p_idx in v2p_cache.get(v_idx, ()):
                            if p_idx in seen_p:
                                continue
                            seen_p.add(p_idx)
                            p = polys[p_idx]
                            pn_w = (n_mat @ p.normal).normalized()
                            if pn_w.dot(hit_norm) > 0.35:
                                accum_norm += pn_w * 0.65
                                neighbor_cnt += 1
                                if neighbor_cnt >= 6:
                                    break
                        if neighbor_cnt >= 6:
                            break
                elif len(polys) <= 1500:
                    for p in polys:
                        if p.index != face_idx and hit_verts.intersection(p.vertices):
                            pn_w = (n_mat @ p.normal).normalized()
                            if pn_w.dot(hit_norm) > 0.35:
                                accum_norm += pn_w * 0.65
                                neighbor_cnt += 1
                                if neighbor_cnt >= 6:
                                    break
            except Exception:
                pass

        # Screen-space 4-ray kernel (4px radius) for smooth interpolation across bevel edges
        for dx, dy in ((4, 0), (-4, 0), (0, 4), (0, -4)):
            ok_n, loc_n, norm_n, _, _ = _single_cast((coord[0] + dx, coord[1] + dy))
            if ok_n and norm_n is not None and norm_n.dot(hit_norm) > 0.25 and (loc_n - hit_loc).length < 0.35:
                accum_norm += norm_n * 0.75

        smoothed = accum_norm.normalized() if accum_norm.length > 1e-6 else hit_norm
        prev_n = getattr(self, "_smoothed_normal", None)
        if prev_n is not None and prev_n.length > 1e-6:
            # Exponential smoothing across mouse move events
            blended = (prev_n * 0.55 + smoothed * 0.45)
            if blended.length > 1e-6:
                smoothed = blended.normalized()
        self._smoothed_normal = smoothed
        return True, hit_loc, smoothed

    def _update_light_from_ray(self, context, event):
        region = getattr(self, "_region", None) or context.region
        rv3d = getattr(self, "_rv3d", None) or context.region_data
        light_obj = getattr(self, "_preview_light", None)
        if not region or not rv3d or not light_obj:
            return

        ok, hit_loc, smoothed_norm = self._raycast_smoothed_surface(context, event, region, rv3d)
        if not ok or hit_loc is None or smoothed_norm is None:
            return

        offset_d = max(0.0, float(getattr(self, "_offset_distance", 0.05)))
        shine_inward = bool(getattr(self, "_shine_inward", False))
        world_pos = hit_loc + smoothed_norm * offset_d
        aim_vec = (-smoothed_norm) if shine_inward else smoothed_norm
        rot_quat = aim_vec.to_track_quat('-Z', 'Y')

        mw = Matrix.Translation(world_pos) @ rot_quat.to_matrix().to_4x4()
        target_mesh = getattr(self, "_target_mesh", None)
        if target_mesh is not None and light_obj.parent == target_mesh:
            try:
                light_obj.matrix_parent_inverse = target_mesh.matrix_world.inverted()
            except Exception:
                light_obj.matrix_parent_inverse = Matrix.Identity(4)
        light_obj.matrix_world = mw
        try:
            context.view_layer.update()
        except Exception:
            pass

    def _set_modal_status_text(self, context):
        mode_lbl = "Inward (Floodlight)" if getattr(self, "_shine_inward", False) else "Outward (Fixture/Bulb)"
        off_m = float(getattr(self, "_offset_distance", 0.05))
        msg = (
            f"Left-Click: Place Light | F: Flip Outward/Inward [{mode_lbl}] | "
            f"Scroll: Offset Distance ({off_m:.2f}m) | Right-Click/ESC: Cancel"
        )
        try:
            if context.workspace:
                context.workspace.status_text_set(msg)
        except Exception:
            pass
        try:
            if context.area:
                context.area.header_text_set(msg)
        except Exception:
            pass

    def _clear_modal_status_text(self, context):
        try:
            if context.workspace:
                context.workspace.status_text_set(None)
        except Exception:
            pass
        try:
            if context.area:
                context.area.header_text_set(None)
        except Exception:
            pass

    def invoke(self, context, event):
        if (
            not bool(getattr(self, "interactive_aim", False))
            or bpy.app.background
            or not getattr(context, "window", None)
            or not getattr(context, "screen", None)
        ):
            return self.execute(context)

        # Find a 3D Viewport region & region_3d for raycasting
        v3d_area = context.area if (context.area and context.area.type == 'VIEW_3D') else None
        v3d_region = context.region if (v3d_area and context.region and context.region.type == 'WINDOW') else None
        v3d_rv3d = context.region_data if v3d_region else None
        if v3d_area is None or v3d_region is None or v3d_rv3d is None:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    v3d_area = area
                    for reg in area.regions:
                        if reg.type == 'WINDOW':
                            v3d_region = reg
                            break
                    for sp in area.spaces:
                        if sp.type == 'VIEW_3D' and sp.region_3d:
                            v3d_rv3d = sp.region_3d
                            break
                    break

        if v3d_area is None or v3d_region is None or v3d_rv3d is None:
            return self.execute(context)

        light_obj, target_mesh, l_type = self._create_and_link_light(context)
        self._preview_light = light_obj
        self._target_mesh = target_mesh
        self._light_type_created = l_type
        self._initial_matrix_world = light_obj.matrix_world.copy() if light_obj else Matrix.Identity(4)
        self._area = v3d_area
        self._region = v3d_region
        self._rv3d = v3d_rv3d
        self._shine_inward = False
        self._offset_distance = 0.05
        self._smoothed_normal = None
        self._vert_to_polys = None
        if target_mesh is not None and getattr(target_mesh, "type", None) == 'MESH' and target_mesh.data:
            try:
                polys = target_mesh.data.polygons
                if len(polys) <= 25000:
                    v2p = {}
                    for p in polys:
                        p_idx = p.index
                        for v_idx in p.vertices:
                            v2p.setdefault(v_idx, []).append(p_idx)
                    self._vert_to_polys = v2p
            except Exception:
                self._vert_to_polys = None

        self._set_modal_status_text(context)
        self._redraw_all_view3d_and_ui(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        light_obj = getattr(self, "_preview_light", None)
        if light_obj is None or light_obj.name not in bpy.data.objects:
            self._clear_modal_status_text(context)
            self._redraw_all_view3d_and_ui(context)
            return {'FINISHED'}

        region = getattr(self, "_region", None)
        in_v3d_window = bool(
            region
            and 0 <= (int(event.mouse_x) - int(region.x)) <= int(region.width)
            and 0 <= (int(event.mouse_y) - int(region.y)) <= int(region.height)
        )

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if in_v3d_window:
                self._update_light_from_ray(context, event)
                if getattr(self, "_area", None):
                    self._area.tag_redraw()
                    if not getattr(self, "_ui_tagged_once", False):
                        self._ui_tagged_once = True
                        for reg in self._area.regions:
                            reg.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'F' and event.value == 'PRESS':
            self._shine_inward = not bool(getattr(self, "_shine_inward", False))
            if self._shine_inward and abs(float(self._offset_distance) - 0.05) < 1e-4:
                self._offset_distance = 0.50
            elif (not self._shine_inward) and abs(float(self._offset_distance) - 0.50) < 1e-4:
                self._offset_distance = 0.05
            if in_v3d_window:
                self._update_light_from_ray(context, event)
            self._set_modal_status_text(context)
            if getattr(self, "_area", None):
                self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELUPMOUSE':
            step = 0.02 if float(self._offset_distance) < 0.25 else 0.05
            self._offset_distance = min(10.0, float(self._offset_distance) + step)
            if in_v3d_window:
                self._update_light_from_ray(context, event)
            self._set_modal_status_text(context)
            if getattr(self, "_area", None):
                self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELDOWNMOUSE':
            step = 0.02 if float(self._offset_distance) <= 0.25 else 0.05
            self._offset_distance = max(0.0, float(self._offset_distance) - step)
            if in_v3d_window:
                self._update_light_from_ray(context, event)
            self._set_modal_status_text(context)
            if getattr(self, "_area", None):
                self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if in_v3d_window:
                self._update_light_from_ray(context, event)
            owner_m = getattr(self, "_target_mesh", None) or getattr(self, "_last_hit_mesh", None)
            if owner_m is not None and getattr(owner_m, "type", None) == 'MESH' and owner_m.name in bpy.data.objects:
                mw_final = light_obj.matrix_world.copy()
                light_obj["reskate_owner_prop"] = owner_m.name
                if light_obj.parent != owner_m:
                    light_obj.parent = owner_m
                    try:
                        light_obj.matrix_parent_inverse = owner_m.matrix_world.inverted()
                    except Exception:
                        light_obj.matrix_parent_inverse = Matrix.Identity(4)
                    light_obj.matrix_world = mw_final
                if hasattr(owner_m, "reskate_linked_lights"):
                    existing = [it.light_obj for it in owner_m.reskate_linked_lights if it.light_obj is not None]
                    if light_obj not in existing:
                        slot = owner_m.reskate_linked_lights.add()
                        slot.light_obj = light_obj
                        slot.enabled = True
                        owner_m.reskate_linked_light_index = len(owner_m.reskate_linked_lights) - 1
                try:
                    owner_m.select_set(True)
                    if context.view_layer:
                        context.view_layer.objects.active = owner_m
                except Exception:
                    pass
            self._clear_modal_status_text(context)
            self._redraw_all_view3d_and_ui(context)
            self.report({'INFO'}, f"ReSkate: Placed and linked {self._light_type_created} light '{light_obj.name}' on prop.")
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._clear_modal_status_text(context)
            # Keep the light linked to the prop at its default position above the prop!
            init_mw = getattr(self, "_initial_matrix_world", None)
            if init_mw is not None and light_obj and light_obj.name in bpy.data.objects:
                try:
                    light_obj.matrix_world = init_mw.copy()
                except Exception:
                    pass
            self._redraw_all_view3d_and_ui(context)
            self.report({'INFO'}, f"ReSkate: Added and linked {self._light_type_created} light '{light_obj.name}' above prop.")
            return {'FINISHED'}

        return {'PASS_THROUGH'}

    def execute(self, context):
        light_obj, _target_mesh, l_type = self._create_and_link_light(context)
        self._redraw_all_view3d_and_ui(context)
        self.report({'INFO'}, f"ReSkate: Added {l_type} light '{light_obj.name}' and linked to prop.")
        return {'FINISHED'}


class RESKATE_OT_link_light_slot(bpy.types.Operator):
    """Adds an empty light slot to 'Lights on this prop' so the user can pick any scene light with the eyedropper"""
    bl_idname = "reskate.link_light_slot"
    bl_label = "Link Existing Light"
    bl_description = "Adds a light picker row so you can link any Spot or Point light in the scene to this prop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a prop mesh first.")
            return {'CANCELLED'}

        slot = obj.reskate_linked_lights.add()
        slot.enabled = True
        # Auto-pick an unlinked scene light if one exists (preferring active object if it is a LIGHT)
        linked_set = {item.light_obj for item in obj.reskate_linked_lights if item.light_obj}
        act_obj = getattr(context, "active_object", None)
        if act_obj is not None and getattr(act_obj, "type", None) == 'LIGHT' and act_obj not in linked_set:
            slot.light_obj = act_obj
            try:
                act_obj["reskate_unlinked_by_user"] = 0
                act_obj["reskate_owner_prop"] = obj.name
            except Exception:
                pass
        else:
            for candidate in context.scene.objects:
                if candidate.type == 'LIGHT' and candidate not in linked_set and not candidate.name.startswith(("ReSkate_Temp_", "_ReSkate_")):
                    slot.light_obj = candidate
                    try:
                        candidate["reskate_unlinked_by_user"] = 0
                        candidate["reskate_owner_prop"] = obj.name
                    except Exception:
                        pass
                    break
        obj.reskate_linked_light_index = len(obj.reskate_linked_lights) - 1
        try:
            from . import ui_panels
            ui_panels.invalidate_panel_caches()
        except Exception:
            pass
        return {'FINISHED'}


class RESKATE_OT_unlink_light_slot(bpy.types.Operator):
    """Removes a light slot from 'Lights on this prop' without deleting the light object from the scene"""
    bl_idname = "reskate.unlink_light_slot"
    bl_label = "Unlink Light"
    bl_description = "Unlinks this light from the prop (keeps the light in the scene)"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or not hasattr(obj, "reskate_linked_lights"):
            return {'CANCELLED'}
        coll = obj.reskate_linked_lights
        idx = self.index if self.index >= 0 else obj.reskate_linked_light_index
        if 0 <= idx < len(coll):
            l_obj = getattr(coll[idx], "light_obj", None)
            if l_obj is not None:
                try:
                    l_obj["reskate_unlinked_by_user"] = 1
                except Exception:
                    pass
            coll.remove(idx)
            obj.reskate_linked_light_index = max(0, min(idx, len(coll) - 1))
        try:
            from . import ui_panels
            ui_panels.invalidate_panel_caches()
        except Exception:
            pass
        return {'FINISHED'}


class RESKATE_OT_select_linked_light(bpy.types.Operator):
    """Selects the linked light object in the 3D Viewport so you can move or rotate it directly"""
    bl_idname = "reskate.select_linked_light"
    bl_label = "Select Light in Viewport"
    bl_description = "Selects this light in the 3D Viewport so you can move (G) or rotate (R) it while keeping it linked to the prop"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or not hasattr(obj, "reskate_linked_lights"):
            return {'CANCELLED'}
        coll = obj.reskate_linked_lights
        idx = self.index if self.index >= 0 else obj.reskate_linked_light_index
        if not (0 <= idx < len(coll)):
            return {'CANCELLED'}
        l_obj = getattr(coll[idx], "light_obj", None)
        if not l_obj:
            self.report({'WARNING'}, "ReSkate: No light object assigned to this slot.")
            return {'CANCELLED'}
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        l_obj.select_set(True)
        context.view_layer.objects.active = l_obj
        self.report({'INFO'}, f"ReSkate: Selected light '{l_obj.name}' in Viewport.")
        return {'FINISHED'}


class RESKATE_OT_delete_linked_light(bpy.types.Operator):
    """Deletes the linked light object from the scene and removes its slot from the prop"""
    bl_idname = "reskate.delete_linked_light"
    bl_label = "Delete Light"
    bl_description = "Deletes this light object from the scene and removes it from 'Lights on this prop'"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or not hasattr(obj, "reskate_linked_lights"):
            return {'CANCELLED'}
        coll = obj.reskate_linked_lights
        idx = self.index if self.index >= 0 else obj.reskate_linked_light_index
        if not (0 <= idx < len(coll)):
            return {'CANCELLED'}
        l_obj = getattr(coll[idx], "light_obj", None)
        l_name = l_obj.name if l_obj else "Light"
        coll.remove(idx)
        obj.reskate_linked_light_index = max(0, min(idx, len(coll) - 1))
        if l_obj is not None:
            try:
                l_data = l_obj.data
                bpy.data.objects.remove(l_obj, do_unlink=True)
                if l_data and l_data.users == 0:
                    bpy.data.lights.remove(l_data, do_unlink=True)
            except Exception:
                pass
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Deleted light '{l_name}'.")
        return {'FINISHED'}


# ==============================================================================
# Step 6: Thumbnail Operators (Per-Object Camera Framing, Numpad 0 View, & Scene Isolation)
# ==============================================================================

def _setup_isometric_camera(context, report_fn=None):
    """Calculates orthographic isometric camera location, rotation, and frustum scale for the active prop."""
    scene = context.scene
    cam_name = "ReSkate_Thumbnail_Camera"
    cam_obj = scene.objects.get(cam_name) or scene.objects.get("ReSkate_Thumb_Camera")

    if not cam_obj:
        cam_data = bpy.data.cameras.new(name=cam_name)
        cam_obj = bpy.data.objects.new(name=cam_name, object_data=cam_data)
        scene.collection.objects.link(cam_obj)
    else:
        cam_data = cam_obj.data

    cam_data.type = 'ORTHO'

    obj = get_target_mesh_object(context) or context.active_object
    if obj and obj.type == 'MESH' and len(obj.data.vertices) > 0:
        bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        center = sum(bbox_corners, Vector()) / 8.0
        world_dims = (
            max(p.x for p in bbox_corners) - min(p.x for p in bbox_corners),
            max(p.y for p in bbox_corners) - min(p.y for p in bbox_corners),
            max(p.z for p in bbox_corners) - min(p.z for p in bbox_corners),
        )
        max_dim = max(world_dims) if max(world_dims) > 0.001 else 2.0
    else:
        bbox_corners = [Vector((-1, -1, 0)), Vector((1, 1, 1))]
        center = Vector((0.0, 0.0, 0.5))
        max_dim = 2.0

    elev_rad = math.asin(1.0 / math.sqrt(3.0))
    azim_rad = math.radians(45.0)
    dist = max(10.0, max_dim * 3.0)

    offset = Vector((
        dist * math.cos(elev_rad) * math.sin(azim_rad),
        -dist * math.cos(elev_rad) * math.cos(azim_rad),
        dist * math.sin(elev_rad)
    ))

    cam_obj.location = center + offset
    direction = -offset.normalized()
    cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

    context.view_layer.update()
    inv_cam = cam_obj.matrix_world.inverted()
    cam_pts = [inv_cam @ pt for pt in bbox_corners]

    dx = max(p.x for p in cam_pts) - min(p.x for p in cam_pts)
    dy = max(p.y for p in cam_pts) - min(p.y for p in cam_pts)
    cam_data.ortho_scale = max(0.25, max(dx, dy) * 1.20)

    cam_data.clip_start = 0.1
    cam_data.clip_end = dist * 3.0
    scene.camera = cam_obj

    # Record which object this camera was framed for and the auto-framed transform
    cam_obj["reskate_target_obj"] = obj.name if obj else ""
    cam_obj["reskate_framed_target"] = obj.name if obj else ""
    cam_obj["reskate_framed_loc"] = [round(float(v), 5) for v in cam_obj.location]
    cam_obj["reskate_framed_rot"] = [round(float(v), 5) for v in cam_obj.rotation_euler]
    cam_obj["reskate_pivot_center"] = [float(center.x), float(center.y), float(center.z)]
    cam_obj["reskate_base_dist"] = float(dist)
    cam_obj["reskate_base_ortho_scale"] = float(cam_data.ortho_scale)
    _sync_view3d_camera_lock_pivot(context, cam_obj, center)

    if report_fn:
        target_lbl = f" for '{obj.name}'" if obj else ""
        report_fn({'INFO'}, f"ReSkate: Thumbnail camera framed{target_lbl} (scale={cam_data.ortho_scale:.2f}m).")
    return cam_obj


def _sync_view3d_camera_lock_pivot(context=None, cam_obj=None, center: Vector | None = None):
    """
    Synchronizes all 3D Viewport `region_3d` orbit pivots (`view_location`, `view_distance`, `view_rotation`)
    with `cam_obj` and the active prop's bounding-box center so `lock_camera = True` MMB-orbits smoothly
    around the prop without jumping.
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if cam_obj is None and scene is not None:
        cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
    if cam_obj is None:
        return
    if center is None:
        piv = cam_obj.get("reskate_pivot_center")
        if piv and len(piv) == 3:
            center = Vector((float(piv[0]), float(piv[1]), float(piv[2])))
        else:
            obj = get_target_mesh_object(ctx) or getattr(ctx, "active_object", None)
            if obj and obj.type == 'MESH' and len(obj.data.vertices) > 0:
                bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
                center = sum(bbox_corners, Vector()) / 8.0
            else:
                center = Vector((0.0, 0.0, 0.5))
    dist = max(0.5, (cam_obj.location - center).length)
    cam_quat = cam_obj.rotation_euler.to_quaternion()
    scr = getattr(ctx, "screen", None)
    if not scr:
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            scr = wm.windows[0].screen
    if scr:
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and space.region_3d:
                        try:
                            space.region_3d.view_location = center
                            space.region_3d.view_distance = dist
                            space.region_3d.view_rotation = cam_quat
                        except Exception:
                            pass


def _was_camera_manually_moved(cam_obj: bpy.types.Object) -> bool:
    """Returns True if the user moved or rotated `cam_obj` after clicking Frame Camera."""
    framed_loc = cam_obj.get("reskate_framed_loc")
    framed_rot = cam_obj.get("reskate_framed_rot")
    if not framed_loc or not framed_rot or len(framed_loc) < 3 or len(framed_rot) < 3:
        return True
    cur_loc = [round(float(v), 5) for v in cam_obj.location]
    cur_rot = [round(float(v), 5) for v in cam_obj.rotation_euler]
    loc_diff = sum(abs(a - b) for a, b in zip(cur_loc, framed_loc))
    rot_diff = sum(abs(a - b) for a, b in zip(cur_rot, framed_rot))
    return (loc_diff > 1e-3) or (rot_diff > 1e-3)


def _cleanup_live_thumbnail_preview_rig(scene):
    """Removes live thumbnail preview helper objects/lights/floor and restores previous world/resolution if saved."""
    if not scene:
        return
    for obj_name in (
        "_ReSkate_Live_Thumb_Sun",
        "_ReSkate_Live_Thumb_Fill",
        "_ReSkate_Live_Thumb_Floor",
        "_ReSkate_Live_Thumb_VariantCutout",
    ):
        o = scene.objects.get(obj_name) or bpy.data.objects.get(obj_name)
        if o is not None:
            d = o.data
            o_type = o.type
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
            if d is not None and getattr(d, "users", 0) == 0:
                try:
                    if o_type == 'LIGHT':
                        bpy.data.lights.remove(d, do_unlink=True)
                    elif o_type == 'MESH':
                        bpy.data.meshes.remove(d, do_unlink=True)
                except Exception:
                    pass

    prev_world_name = str(scene.get("reskate_saved_world_name", "") or "")
    if prev_world_name and prev_world_name in bpy.data.worlds:
        scene.world = bpy.data.worlds[prev_world_name]
    live_w = bpy.data.worlds.get("_ReSkate_Live_Thumb_World")
    if live_w is not None:
        if scene.world == live_w:
            scene.world = None
        try:
            bpy.data.worlds.remove(live_w, do_unlink=True)
        except Exception:
            pass

    if "reskate_saved_res_x" in scene and "reskate_saved_res_y" in scene:
        try:
            scene.render.resolution_x = int(scene["reskate_saved_res_x"])
            scene.render.resolution_y = int(scene["reskate_saved_res_y"])
            del scene["reskate_saved_res_x"]
            del scene["reskate_saved_res_y"]
        except Exception:
            pass

    if "reskate_saved_exposure" in scene and hasattr(scene, "view_settings"):
        try:
            scene.view_settings.exposure = float(scene["reskate_saved_exposure"])
            del scene["reskate_saved_exposure"]
        except Exception:
            pass
    scene["reskate_thumb_live_active"] = False


def has_any_view3d_area(context=None) -> bool:
    """Returns True if at least one 3D Viewport area exists in the current window/screen."""
    ctx = context if context is not None else bpy.context
    scr = getattr(ctx, "screen", None)
    if not scr:
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            scr = wm.windows[0].screen
    if scr:
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                return True
    return False


def cleanup_if_exited_thumbnail_camera_view(context=None) -> bool:
    """
    Detects when the user manually orbited/rotated the 3D Viewport out of Camera View
    (`view_perspective != 'CAMERA'`) while `reskate_thumb_live_active` or `_ReSkate_Live_Thumb_*`
    studio lights/floor were active, and immediately cleans them up so clicking other props
    never spawns or moves studio lights or ground planes outside Camera View (Item 2).
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if not scene:
        return False
    has_live = (
        bool(scene.get("reskate_thumb_live_active", False))
        or ("_ReSkate_Live_Thumb_Sun" in scene.objects)
        or ("_ReSkate_Live_Thumb_Fill" in scene.objects)
        or ("_ReSkate_Live_Thumb_Floor" in scene.objects)
        or ("_ReSkate_Live_Thumb_VariantCutout" in scene.objects)
    )
    if not has_live:
        return False
    if has_any_view3d_area(ctx) and not is_in_thumbnail_camera_view(ctx):
        _exit_thumbnail_camera_preview(ctx)
        return True
    return False


def sync_live_thumbnail_preview_rig(context=None, force_activate: bool = False):
    """
    Synchronizes the live 3D Viewport thumbnail lighting, floor color, key/fill ratio, sun rotation,
    sun elevation, exposure, and backdrop preset while in Thumbnail Camera preview (§3 Page 5, §4.6).
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if not scene:
        return
    prop = getattr(scene, "reskate_prop", None)
    if not prop:
        return

    in_cam = is_in_thumbnail_camera_view(ctx)
    on_page5 = (int(getattr(prop, "simple_wizard_page", 1) or 1) == 5)
    if not force_activate and cleanup_if_exited_thumbnail_camera_view(ctx):
        return
    if force_activate and not in_cam and not on_page5:
        cleanup_if_exited_thumbnail_camera_view(ctx)
        return

    if force_activate and (in_cam or on_page5):
        scene["reskate_thumb_live_active"] = True

    # Only build/update live scene objects when camera preview is active (or explicitly forced on Page 5)
    has_live_objs = (
        bool(force_activate and (in_cam or on_page5))
        or bool(scene.get("reskate_thumb_live_active", False))
        or ("_ReSkate_Live_Thumb_Sun" in scene.objects)
        or ("_ReSkate_Live_Thumb_Floor" in scene.objects)
        or ("_ReSkate_Live_Thumb_VariantCutout" in scene.objects)
        or bool(in_cam and on_page5)
    )
    if not has_live_objs:
        return
    if not force_activate and has_any_view3d_area(ctx) and not in_cam:
        _exit_thumbnail_camera_preview(ctx)
        return

    obj = get_target_mesh_object(ctx) or getattr(ctx, "active_object", None)
    if obj and obj.type == 'MESH' and len(obj.data.vertices) > 0:
        bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        center = sum(bbox_corners, Vector()) / 8.0
        min_z = min(p.z for p in bbox_corners)
        dims_w = (
            max(p.x for p in bbox_corners) - min(p.x for p in bbox_corners),
            max(p.y for p in bbox_corners) - min(p.y for p in bbox_corners),
            max(p.z for p in bbox_corners) - min(p.z for p in bbox_corners),
        )
        max_dim = max(0.5, max(dims_w))
    else:
        center = Vector((0.0, 0.0, 0.5))
        min_z = 0.0
        max_dim = 2.0

    preset = str(getattr(prop, "thumb_bg_preset", "SUNNY_DAY") or "SUNNY_DAY").upper()
    floor_col = tuple(getattr(prop, "thumb_floor_color", (0.22, 0.24, 0.27)))
    bg_col = tuple(getattr(prop, "thumb_bg_color", (0.14, 0.16, 0.20)))
    key_fill = max(0.25, float(getattr(prop, "thumb_key_fill_ratio", 2.2) or 2.2))
    sun_rot = float(getattr(prop, "thumb_sun_rotation", 42.0) or 42.0)
    sun_elev = float(getattr(prop, "thumb_sun_elevation", 38.0) or 38.0)
    thumb_exp = float(getattr(prop, "thumb_exposure", 0.0) or 0.0)

    if hasattr(scene, "view_settings"):
        if "reskate_saved_exposure" not in scene:
            scene["reskate_saved_exposure"] = float(scene.view_settings.exposure)
        scene.view_settings.exposure = float(scene.get("reskate_saved_exposure", 0.0)) + thumb_exp

    # 1. Key Sun Light
    elev_r = math.radians(sun_elev)
    rot_r = math.radians(sun_rot)
    sun_dir = Vector((
        math.cos(elev_r) * math.sin(rot_r),
        -math.cos(elev_r) * math.cos(rot_r),
        math.sin(elev_r),
    )).normalized()

    sun_obj = scene.objects.get("_ReSkate_Live_Thumb_Sun")
    if sun_obj is None or sun_obj.type != 'LIGHT':
        sun_data = bpy.data.lights.new(name="_ReSkate_Live_Thumb_Sun", type='SUN')
        sun_obj = bpy.data.objects.new(name="_ReSkate_Live_Thumb_Sun", object_data=sun_data)
        scene.collection.objects.link(sun_obj)
        sun_obj.hide_select = True
    else:
        sun_data = sun_obj.data
    sun_data.energy = 3.2 if preset == "SUNNY_DAY" else 2.4
    sun_data.color = (1.0, 0.97, 0.92) if preset == "SUNNY_DAY" else (1.0, 0.99, 0.97)
    sun_data.use_shadow = True
    sun_obj.location = center + sun_dir * max(6.0, max_dim * 2.5)
    sun_obj.rotation_euler = (-sun_dir).to_track_quat('-Z', 'Y').to_euler()

    # 2. Fill Area Light (controlled by Key / Fill Ratio)
    fill_obj = scene.objects.get("_ReSkate_Live_Thumb_Fill")
    dist_scale = max(1.8, max_dim * 2.2)
    area_power_scale = (dist_scale / 2.2) ** 2
    if fill_obj is None or fill_obj.type != 'LIGHT':
        fill_data = bpy.data.lights.new(name="_ReSkate_Live_Thumb_Fill", type='AREA')
        fill_obj = bpy.data.objects.new(name="_ReSkate_Live_Thumb_Fill", object_data=fill_data)
        scene.collection.objects.link(fill_obj)
        fill_obj.hide_select = True
    else:
        fill_data = fill_obj.data
    fill_data.energy = (140.0 * area_power_scale) / key_fill
    fill_data.color = (0.86, 0.93, 1.0)
    fill_data.size = dist_scale * 1.15
    fill_data.use_shadow = False
    fill_obj.location = center + Vector((-sun_dir.x * dist_scale, -sun_dir.y * dist_scale, dist_scale * 0.75))
    aim_dir = (center - fill_obj.location).normalized()
    if aim_dir.length > 1e-5:
        fill_obj.rotation_euler = aim_dir.to_track_quat('-Z', 'Y').to_euler()

    # 3. Floor plane (for SUNNY_DAY and STUDIO presets)
    use_floor = preset in ("SUNNY_DAY", "STUDIO")
    floor_obj = scene.objects.get("_ReSkate_Live_Thumb_Floor")
    if use_floor:
        floor_rad = max(25.0, max_dim * 12.0)
        f_verts = [
            (center.x - floor_rad, center.y - floor_rad, min_z - 0.001),
            (center.x + floor_rad, center.y - floor_rad, min_z - 0.001),
            (center.x + floor_rad, center.y + floor_rad, min_z - 0.001),
            (center.x - floor_rad, center.y + floor_rad, min_z - 0.001),
        ]
        if floor_obj is None or floor_obj.type != 'MESH':
            f_mesh = bpy.data.meshes.new("_ReSkate_Live_Thumb_Floor_Mesh")
            f_mesh.from_pydata(f_verts, [], [(0, 1, 2, 3)])
            f_mesh.update()
            floor_obj = bpy.data.objects.new("_ReSkate_Live_Thumb_Floor", f_mesh)
            scene.collection.objects.link(floor_obj)
            floor_obj.hide_select = True
        else:
            f_mesh = floor_obj.data
            if len(f_mesh.vertices) == 4:
                for idx, co in enumerate(f_verts):
                    f_mesh.vertices[idx].co = co
                f_mesh.update()

        f_mat = bpy.data.materials.get("_ReSkate_Live_Thumb_Floor_Mat")
        if f_mat is None:
            f_mat = bpy.data.materials.new("_ReSkate_Live_Thumb_Floor_Mat")
            f_mat.use_nodes = True
        p_bsdf = next((n for n in f_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if p_bsdf:
            p_bsdf.inputs["Base Color"].default_value = (float(floor_col[0]), float(floor_col[1]), float(floor_col[2]), 1.0)
            p_bsdf.inputs["Roughness"].default_value = 0.85
        if len(floor_obj.data.materials) == 0:
            floor_obj.data.materials.append(f_mat)
        else:
            floor_obj.data.materials[0] = f_mat
        floor_obj.hide_viewport = False
        floor_obj.hide_render = False
    elif floor_obj is not None:
        floor_obj.hide_viewport = True
        floor_obj.hide_render = True

    # 3b. Variant Icon Cutout (Deprecated in v0.31.0 — replaced by bottom-left colored corner notch)
    cutout_enabled = False
    cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera")
    if cam_obj is None and in_cam:
        try:
            cam_obj = _setup_isometric_camera(ctx)
        except Exception:
            cam_obj = None
    cutout_obj = scene.objects.get("_ReSkate_Live_Thumb_VariantCutout")
    if cutout_obj is not None:
        cutout_obj.hide_viewport = True
        cutout_obj.hide_render = True

    # 4. World backdrop & Transparent Film
    scene.render.film_transparent = (preset == "TRANSPARENT")
    try:
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm:
            for win in wm.windows:
                if win.screen:
                    for area in win.screen.areas:
                        if area.type == 'VIEW_3D':
                            for sp in area.spaces:
                                if sp.type == 'VIEW_3D' and hasattr(sp, "shading"):
                                    if sp.shading.type in ('MATERIAL', 'RENDERED'):
                                        sp.shading.use_scene_lights = True
                                        sp.shading.use_scene_world = True
                                    if sp.shading.type in ('MATERIAL', 'SOLID') and preset == "TRANSPARENT":
                                        sp.shading.type = 'RENDERED'
    except Exception:
        pass
    if "reskate_saved_world_name" not in scene and scene.world and scene.world.name != "_ReSkate_Live_Thumb_World":
        scene["reskate_saved_world_name"] = scene.world.name
    live_w = bpy.data.worlds.get("_ReSkate_Live_Thumb_World")
    if live_w is None:
        live_w = bpy.data.worlds.new("_ReSkate_Live_Thumb_World")
        live_w.use_nodes = True
    bg_node = next((n for n in live_w.node_tree.nodes if n.type == "BACKGROUND"), None)
    if bg_node:
        if preset == "SUNNY_DAY":
            bg_node.inputs["Color"].default_value = (0.52, 0.68, 0.90, 1.0)
            bg_node.inputs["Strength"].default_value = 0.55
        elif preset == "STUDIO":
            bg_node.inputs["Color"].default_value = (float(floor_col[0]) * 0.75, float(floor_col[1]) * 0.75, float(floor_col[2]) * 0.75, 1.0)
            bg_node.inputs["Strength"].default_value = 0.65
        else:
            bg_node.inputs["Color"].default_value = (float(bg_col[0]), float(bg_col[1]), float(bg_col[2]), 1.0)
            bg_node.inputs["Strength"].default_value = 1.0
    scene.world = live_w


_THUMB_WATCHDOG_REGISTERED = False


def _thumb_camera_watchdog_timer():
    """
    Lightweight main-thread timer (runs ONLY while Thumbnail Camera preview is active) that:
      1. Syncs orthographic camera scale when the user zooms with the mouse scroll wheel while `lock_camera=True`.
      2. Immediately exits Thumbnail Camera preview and restores normal scene lighting/world/shading if the user
         ever leaves Camera View (e.g. via Numpad 0 or unchecking Lock Camera to View and orbiting out).
    """
    global _THUMB_WATCHDOG_REGISTERED
    try:
        ctx = bpy.context
        scene = getattr(ctx, "scene", None)
        if scene is None:
            wm = getattr(ctx, "window_manager", None)
            if wm and wm.windows:
                scene = wm.windows[0].scene
        if scene is None:
            _THUMB_WATCHDOG_REGISTERED = False
            return None

        has_live = (
            bool(scene.get("reskate_thumb_live_active", False))
            or ("_ReSkate_Live_Thumb_Sun" in scene.objects)
            or ("_ReSkate_Live_Thumb_Fill" in scene.objects)
            or ("_ReSkate_Live_Thumb_Floor" in scene.objects)
            or ("_ReSkate_Live_Thumb_VariantCutout" in scene.objects)
        )
        if not has_live:
            _THUMB_WATCHDOG_REGISTERED = False
            return None

        if has_any_view3d_area(ctx) and not is_in_thumbnail_camera_view(ctx):
            _exit_thumbnail_camera_preview(ctx)
            _THUMB_WATCHDOG_REGISTERED = False
            return None

        # Sync orthographic scale if user zoomed with scroll wheel / Ctrl+MMB while in Camera View.
        # In Blender, orthographic cameras do not zoom by changing `view_distance`; instead Blender's
        # `view3d.zoom` modifies `space.region_3d.view_camera_zoom` (the 2D viewport frame border).
        # We convert any delta in `view_camera_zoom` directly into `cam_obj.data.ortho_scale` and snap
        # `view_camera_zoom` back to `locked_zoom` so the camera border stays fixed while the ortho frustum zooms!
        cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
        if cam_obj is not None and cam_obj.type == 'CAMERA' and getattr(cam_obj.data, "type", "") == 'ORTHO':
            wm = getattr(ctx, "window_manager", None)
            scr = getattr(ctx, "screen", None) or (wm.windows[0].screen if (wm and wm.windows) else None)
            if scr:
                locked_zoom = float(scene.get("reskate_locked_cam_zoom", 0.0) or 0.0)
                fac_locked = max(1e-4, ((math.sqrt(2.0) + locked_zoom / 50.0) ** 2) / 4.0)
                for area in scr.areas:
                    if area.type == 'VIEW_3D':
                        for space in area.spaces:
                            if space.type == 'VIEW_3D' and space.region_3d and space.region_3d.view_perspective == 'CAMERA':
                                cur_zoom = float(space.region_3d.view_camera_zoom)
                                if abs(cur_zoom - locked_zoom) > 1e-4:
                                    fac_cur = max(1e-4, ((math.sqrt(2.0) + cur_zoom / 50.0) ** 2) / 4.0)
                                    zoom_ratio = max(0.05, min(20.0, fac_cur / fac_locked))
                                    new_ortho = max(0.05, float(cam_obj.data.ortho_scale) / zoom_ratio)
                                    cam_obj.data.ortho_scale = new_ortho
                                    cam_obj["reskate_base_ortho_scale"] = float(new_ortho)
                                    try:
                                        space.region_3d.view_camera_zoom = locked_zoom
                                    except Exception:
                                        pass
                                    cutout_obj = scene.objects.get("_ReSkate_Live_Thumb_VariantCutout")
                                    if cutout_obj is not None and cutout_obj.type == 'MESH' and not cutout_obj.hide_viewport:
                                        sync_live_thumbnail_preview_rig(ctx)
                                    area.tag_redraw()
                                break
                        break
        return 0.025
    except Exception:
        _THUMB_WATCHDOG_REGISTERED = False
        return None


def ensure_thumbnail_camera_watchdog(context=None):
    """Schedules `_thumb_camera_watchdog_timer` if live thumbnail preview objects are active in the scene."""
    global _THUMB_WATCHDOG_REGISTERED
    if bpy.app.background:
        return
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if not scene:
        return
    has_live = (
        bool(scene.get("reskate_thumb_live_active", False))
        or ("_ReSkate_Live_Thumb_Sun" in scene.objects)
        or ("_ReSkate_Live_Thumb_Fill" in scene.objects)
        or ("_ReSkate_Live_Thumb_Floor" in scene.objects)
        or ("_ReSkate_Live_Thumb_VariantCutout" in scene.objects)
    )
    if not has_live:
        return
    if not _THUMB_WATCHDOG_REGISTERED and hasattr(bpy.app, "timers"):
        try:
            _THUMB_WATCHDOG_REGISTERED = True
            bpy.app.timers.register(_thumb_camera_watchdog_timer, first_interval=0.025)
        except Exception:
            _THUMB_WATCHDOG_REGISTERED = False


def _enter_thumbnail_camera_preview(context):
    """Switches render resolution to 1:1 (512x512), locks camera to view for MMB orbit around the prop, and activates live preview rig."""
    scene = getattr(context, "scene", None)
    if not scene:
        return
    if "reskate_saved_res_x" not in scene:
        scene["reskate_saved_res_x"] = int(scene.render.resolution_x)
        scene["reskate_saved_res_y"] = int(scene.render.resolution_y)
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene["reskate_thumb_live_active"] = True

    prefs = get_addon_preferences(context)
    want_shading = str(getattr(prefs, "thumb_live_preview_mode", "RENDERED") or "RENDERED") if prefs else "RENDERED"
    if want_shading not in ("RENDERED", "MATERIAL"):
        want_shading = "RENDERED"

    cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
    _sync_view3d_camera_lock_pivot(context, cam_obj)

    if getattr(context, "screen", None):
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        if hasattr(space, "lock_camera"):
                            if "reskate_saved_lock_camera" not in scene:
                                scene["reskate_saved_lock_camera"] = bool(space.lock_camera)
                            try:
                                space.lock_camera = True
                            except Exception:
                                pass
                        if space.region_3d:
                            space.region_3d.view_perspective = 'CAMERA'
                            scene["reskate_locked_cam_zoom"] = float(space.region_3d.view_camera_zoom)
                        if hasattr(space, "shading"):
                            if "reskate_saved_shading_mode" not in scene:
                                scene["reskate_saved_shading_mode"] = str(space.shading.type)
                            try:
                                space.shading.type = want_shading
                            except Exception:
                                pass
                        area.tag_redraw()
                        break
                break
    sync_live_thumbnail_preview_rig(context, force_activate=True)
    ensure_thumbnail_camera_watchdog(context)


def _exit_thumbnail_camera_preview(context):
    """Restores previous render resolution, camera lock, viewport shading, and cleans up the live preview rig."""
    scene = getattr(context, "scene", None)
    if not scene:
        wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            scene = wm.windows[0].scene
    if not scene:
        return
    saved_shading = str(scene.get("reskate_saved_shading_mode", "") or "")
    has_saved_lock = ("reskate_saved_lock_camera" in scene)
    saved_lock = bool(scene.get("reskate_saved_lock_camera", False))
    scr = getattr(context, "screen", None)
    if not scr:
        wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            scr = wm.windows[0].screen
    if scr:
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        if has_saved_lock and hasattr(space, "lock_camera"):
                            try:
                                space.lock_camera = saved_lock
                            except Exception:
                                pass
                        if space.region_3d and space.region_3d.view_perspective == 'CAMERA':
                            space.region_3d.view_perspective = 'PERSP'
                        if saved_shading and hasattr(space, "shading"):
                            try:
                                space.shading.type = saved_shading
                            except Exception:
                                pass
                        area.tag_redraw()
                        for reg in area.regions:
                            reg.tag_redraw()
                        break
                break
    for k in ("reskate_saved_shading_mode", "reskate_saved_lock_camera", "reskate_locked_cam_zoom"):
        if k in scene:
            try:
                del scene[k]
            except Exception:
                pass
    _cleanup_live_thumbnail_preview_rig(scene)


def is_in_thumbnail_camera_view(context=None) -> bool:
    """Returns True if any 3D Viewport in the current screen is viewing through the Thumbnail Camera."""
    ctx = context if context is not None else bpy.context
    scr = getattr(ctx, "screen", None)
    if not scr:
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            scr = wm.windows[0].screen
    if scr:
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and space.region_3d:
                        if space.region_3d.view_perspective == 'CAMERA':
                            return True
    return False


def sync_thumbnail_camera_on_prop_switch(context=None, target_obj=None):
    """
    When switching props (via carousel ◀/▶, Sanctus popup grid, or viewport selection) while in
    Thumbnail Camera view, automatically jumps the Thumbnail Camera to frame the newly active prop
    and syncs its lighting rig. If the user manually orbited out of Camera View, cleans up any
    leftover studio lights/floor instead of spawning them!
    """
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    if not scene:
        return
    if cleanup_if_exited_thumbnail_camera_view(ctx):
        return
    in_cam = is_in_thumbnail_camera_view(ctx)
    if has_any_view3d_area(ctx) and not in_cam:
        return
    live_active = bool(scene.get("reskate_thumb_live_active", False))
    if not in_cam and not live_active:
        return
    _setup_isometric_camera(ctx)
    sync_live_thumbnail_preview_rig(ctx)


class RESKATE_OT_frame_isometric_camera(bpy.types.Operator):
    """Frames an orthographic isometric 1:1 camera targeting the active prop and activates live thumbnail preview"""
    bl_idname = "reskate.frame_isometric_camera"
    bl_label = "Frame Camera"
    bl_description = "Frames an isometric 1:1 camera around the currently selected prop and previews thumbnail lighting in the viewport"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _setup_isometric_camera(context, self.report)
        _enter_thumbnail_camera_preview(context)
        return {'FINISHED'}


class RESKATE_OT_orbit_thumb_camera(bpy.types.Operator):
    """Orbits the thumbnail camera around the active prop's Z center by ±45°"""
    bl_idname = "reskate.orbit_thumb_camera"
    bl_label = "Orbit Camera"
    bl_description = "Rotates the Thumbnail Camera around the active prop's center by 45° (useful when the default isometric angle looks at the back of the prop)"
    bl_options = {'REGISTER', 'UNDO'}

    angle_deg: FloatProperty(name="Orbit Angle (°)", default=45.0)

    def execute(self, context):
        scene = context.scene
        obj = get_target_mesh_object(context) or context.active_object
        cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
        if not cam_obj or cam_obj.type != 'CAMERA':
            cam_obj = _setup_isometric_camera(context)

        if obj and obj.type == 'MESH' and len(obj.data.vertices) > 0:
            bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            center = sum(bbox_corners, Vector()) / 8.0
        else:
            center = Vector((0.0, 0.0, 0.5))

        rel = cam_obj.location - center
        rot_z = Matrix.Rotation(math.radians(float(self.angle_deg)), 3, 'Z')
        new_rel = rot_z @ rel
        cam_obj.location = center + new_rel
        direction = (-new_rel).normalized()
        if direction.length > 1e-5:
            cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
        context.view_layer.update()

        cam_obj["reskate_framed_loc"] = [round(float(v), 5) for v in cam_obj.location]
        cam_obj["reskate_framed_rot"] = [round(float(v), 5) for v in cam_obj.rotation_euler]
        cam_obj["reskate_pivot_center"] = [float(center.x), float(center.y), float(center.z)]
        _sync_view3d_camera_lock_pivot(context, cam_obj, center)
        self.report({'INFO'}, f"ReSkate: Orbited Thumbnail Camera by {self.angle_deg:+.0f}°.")
        return {'FINISHED'}


class RESKATE_OT_view_thumbnail_camera(bpy.types.Operator):
    """Toggles the 3D Viewport into and out of the 1:1 Thumbnail Camera live preview (same as Numpad 0)"""
    bl_idname = "reskate.view_thumbnail_camera"
    bl_label = "View Camera"
    bl_description = "Toggles the 1:1 Thumbnail Camera view and live lighting preview in the 3D Viewport (restores your previous view and resolution when exited)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        obj = get_target_mesh_object(context) or context.active_object
        cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
        if not cam_obj or (obj and cam_obj.get("reskate_target_obj", "") != obj.name and not _was_camera_manually_moved(cam_obj)):
            cam_obj = _setup_isometric_camera(context)
        else:
            scene.camera = cam_obj

        in_cam_now = False
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D' and space.region_3d:
                            in_cam_now = (space.region_3d.view_perspective == 'CAMERA')
                            break
                    break

        if in_cam_now:
            _exit_thumbnail_camera_preview(context)
            self.report({'INFO'}, "ReSkate: Exited Thumbnail Camera view (restored resolution & viewport shading).")
        else:
            _enter_thumbnail_camera_preview(context)
            self.report({'INFO'}, "ReSkate: Viewing through 1:1 Thumbnail Camera (live lighting preview active).")
        return {'FINISHED'}


class RESKATE_OT_choose_thumbnail_image(bpy.types.Operator):
    """Opens a file browser to choose a custom image file for this prop's Quick Drop thumbnail"""
    bl_idname = "reskate.choose_thumbnail_image"
    bl_label = "Choose Image File"
    bl_description = "Select a custom image file (.png, .jpg, .webp) from disk to use as this prop's Quick Drop thumbnail"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(name="Image File", subtype='FILE_PATH', default="")
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.webp", options={'HIDDEN'})

    def invoke(self, context, event):
        if getattr(context, "window_manager", None) and not bpy.app.background:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def execute(self, context):
        raw_path = (self.filepath or "").strip()
        if not raw_path:
            return {'CANCELLED'}
        abs_p = Path(bpy.path.abspath(raw_path))
        if not abs_p.exists() or not abs_p.is_file():
            self.report({'WARNING'}, f"ReSkate: Image file not found: {abs_p}")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        obj = get_target_mesh_object(context) or context.active_object
        if prop is not None:
            prop.custom_thumbnail_path = str(abs_p)
            prop.custom_thumb_png = str(abs_p)
            if obj is not None:
                prop["custom_thumb_owner"] = obj.name
        if obj is not None:
            obj["reskate_custom_thumbnail_path"] = str(abs_p)
            obj["reskate_rendered_thumb_256"] = str(abs_p)
            obj["reskate_thumb_rendered"] = True

        try:
            img = bpy.data.images.load(str(abs_p), check_existing=True)
            img.reload()
            if not bpy.app.background:
                img.preview_ensure()
        except Exception:
            pass

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Selected custom thumbnail '{abs_p.name}'.")
        return {'FINISHED'}


THUMB_BG_PRESETS = {
    'SUNNY_DAY': ((0.52, 0.68, 0.90), 'STUDIO_WARM', True),
    'STUDIO': ((0.18, 0.20, 0.24), 'STUDIO_DARK', True),
    'TRANSPARENT': ((0.14, 0.16, 0.20), 'TRANSPARENT', False),
    'FLAT_COLOR': ((0.14, 0.16, 0.20), 'FLAT_COLOR', False),
    'DARK_SLATE': ((0.14, 0.16, 0.20), 'STUDIO_DARK', True),
    'WARM_STUDIO': ((0.24, 0.20, 0.17), 'STUDIO_WARM', True),
    'RAIL_BLUE': ((0.11, 0.20, 0.32), 'STUDIO_DARK', True),
    'RAMP_ORANGE': ((0.30, 0.17, 0.10), 'STUDIO_WARM', True),
    'LEDGE_GREEN': ((0.12, 0.24, 0.17), 'STUDIO_DARK', True),
    'VERT_PURPLE': ((0.20, 0.13, 0.28), 'STUDIO_DARK', True),
}


class RESKATE_OT_set_thumb_bg_preset(bpy.types.Operator):
    """Applies a one-click color-coded backdrop preset for Quick Drop thumbnail rendering"""
    bl_idname = "reskate.set_thumb_bg_preset"
    bl_label = "Set Thumbnail Backdrop Preset"
    bl_description = "Sets the thumbnail backdrop color, transparency mode, and shadow-catching studio floor from a curated preset"
    bl_options = {'REGISTER', 'UNDO'}

    preset: StringProperty(name="Preset", default="SUNNY_DAY")

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        key = (self.preset or "SUNNY_DAY").upper()
        if hasattr(prop, "thumb_bg_preset") and key in ("SUNNY_DAY", "STUDIO", "TRANSPARENT", "FLAT_COLOR"):
            prop.thumb_bg_preset = key
        rgb, bg_style, use_floor = THUMB_BG_PRESETS.get(key, THUMB_BG_PRESETS['SUNNY_DAY'])
        prop.thumb_bg_color = rgb
        prop.thumb_bg_style = bg_style
        prop.thumb_use_cyclorama_floor = use_floor
        sync_live_thumbnail_preview_rig(context)
        self.report({'INFO'}, f"ReSkate: Thumbnail backdrop set to '{key.replace('_', ' ').title()}'.")
        return {'FINISHED'}


_COPIED_THUMB_LIGHTING_PRESET: dict[str, object] = {}


def has_copied_thumb_lighting_preset(context=None) -> bool:
    """Returns True if a thumbnail lighting & background preset has been copied to the session buffer."""
    if _COPIED_THUMB_LIGHTING_PRESET:
        return True
    ctx = context if context is not None else bpy.context
    scene = getattr(ctx, "scene", None)
    return bool(scene and scene.get("reskate_copied_lighting_json", ""))


def _apply_lighting_dict_to_object_and_prop(obj: bpy.types.Object | None, prop, data: dict):
    """Applies a copied thumbnail lighting & background dict to `prop` (if active) and persists it on `obj`."""
    if prop is not None:
        if "thumb_bg_preset" in data:
            prop.thumb_bg_preset = str(data["thumb_bg_preset"])
        if "thumb_floor_color" in data:
            prop.thumb_floor_color = tuple(float(v) for v in data["thumb_floor_color"][:3])
        if "thumb_bg_color" in data:
            prop.thumb_bg_color = tuple(float(v) for v in data["thumb_bg_color"][:3])
        if "thumb_key_fill_ratio" in data:
            prop.thumb_key_fill_ratio = float(data["thumb_key_fill_ratio"])
        if "thumb_sun_rotation" in data:
            prop.thumb_sun_rotation = float(data["thumb_sun_rotation"])
        if "thumb_sun_elevation" in data:
            prop.thumb_sun_elevation = float(data["thumb_sun_elevation"])
        if "thumb_exposure" in data:
            prop.thumb_exposure = float(data["thumb_exposure"])
        if "thumb_bg_style" in data:
            prop.thumb_bg_style = str(data["thumb_bg_style"])
        if "thumb_use_cyclorama_floor" in data:
            prop.thumb_use_cyclorama_floor = bool(data["thumb_use_cyclorama_floor"])

    if obj is not None and obj.type == 'MESH':
        st = get_or_init_object_prop_state(obj)
        for k, v in data.items():
            st[k] = v
        save_object_prop_state(obj, st)


class RESKATE_OT_copy_thumb_lighting_preset(bpy.types.Operator):
    """Copies the active prop's Thumbnail Lighting & Background settings so you can paste them onto other props"""
    bl_idname = "reskate.copy_thumb_lighting_preset"
    bl_label = "Copy Lighting"
    bl_description = "Copies the current backdrop preset, floor/background color, key/fill ratio, rotation, elevation, and exposure so you can paste them onto other props"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        data = {
            "thumb_bg_preset": str(getattr(prop, "thumb_bg_preset", "SUNNY_DAY") or "SUNNY_DAY"),
            "thumb_floor_color": [float(v) for v in getattr(prop, "thumb_floor_color", (0.22, 0.24, 0.27))],
            "thumb_bg_color": [float(v) for v in getattr(prop, "thumb_bg_color", (0.14, 0.16, 0.20))],
            "thumb_key_fill_ratio": float(getattr(prop, "thumb_key_fill_ratio", 2.2)),
            "thumb_sun_rotation": float(getattr(prop, "thumb_sun_rotation", 42.0)),
            "thumb_sun_elevation": float(getattr(prop, "thumb_sun_elevation", 38.0)),
            "thumb_exposure": float(getattr(prop, "thumb_exposure", 0.0)),
            "thumb_bg_style": str(getattr(prop, "thumb_bg_style", "STUDIO_DARK") or "STUDIO_DARK"),
            "thumb_use_cyclorama_floor": bool(getattr(prop, "thumb_use_cyclorama_floor", True)),
        }
        _COPIED_THUMB_LIGHTING_PRESET.clear()
        _COPIED_THUMB_LIGHTING_PRESET.update(data)
        try:
            context.scene["reskate_copied_lighting_json"] = json.dumps(data)
        except Exception:
            pass
        self.report({'INFO'}, f"ReSkate: Copied '{data['thumb_bg_preset']}' thumbnail lighting preset.")
        return {'FINISHED'}


class RESKATE_OT_paste_thumb_lighting_preset(bpy.types.Operator):
    """Pastes the copied Thumbnail Lighting & Background preset onto the active prop (and any other selected props)"""
    bl_idname = "reskate.paste_thumb_lighting_preset"
    bl_label = "Paste Lighting"
    bl_description = "Pastes the copied thumbnail lighting & background settings onto the active prop (and all selected props)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        data = dict(_COPIED_THUMB_LIGHTING_PRESET)
        if not data and context.scene and context.scene.get("reskate_copied_lighting_json", ""):
            try:
                data = json.loads(str(context.scene.get("reskate_copied_lighting_json", "{}")))
            except Exception:
                data = {}
        if not data:
            self.report({'WARNING'}, "ReSkate: Copy a lighting preset first.")
            return {'CANCELLED'}

        active_obj = get_target_mesh_object(context) or context.active_object
        _apply_lighting_dict_to_object_and_prop(active_obj, prop, data)

        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        for s_obj in sel_meshes:
            if s_obj != active_obj:
                _apply_lighting_dict_to_object_and_prop(s_obj, None, data)

        sync_live_thumbnail_preview_rig(context)
        n_applied = max(1, len(sel_meshes))
        self.report({'INFO'}, f"ReSkate: Pasted thumbnail lighting preset onto {n_applied} prop(s).")
        return {'FINISHED'}


class RESKATE_OT_apply_thumb_lighting_to_selected(bpy.types.Operator):
    """Copies the active prop's Thumbnail Lighting & Background settings to all selected props in one click"""
    bl_idname = "reskate.apply_thumb_lighting_to_selected"
    bl_label = "Apply Lighting to Selected"
    bl_description = "Applies the active prop's thumbnail lighting & background settings to all currently selected props"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        bpy.ops.reskate.copy_thumb_lighting_preset()
        return bpy.ops.reskate.paste_thumb_lighting_preset()


class RESKATE_OT_group_selected_as_variants(bpy.types.Operator):
    """Groups all selected props into a single ReSkate 2.1.1 Object Family so they share one Quick Drop tile with variant cycling"""
    bl_idname = "reskate.group_selected_as_variants"
    bl_label = "Group Selected Props as Variants"
    bl_description = (
        "Assigns a shared ReSkate 2.1.1 Object Family Title to all selected props so they collapse into a single "
        "Quick Drop tile in-game with variant cycling"
    )
    bl_options = {'REGISTER', 'UNDO'}

    family_title: StringProperty(
        name="Variant Family Title",
        default="",
        description="Shared family title shown on the Quick Drop tile (leave blank to derive from the active prop's display name)",
    )

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        active_obj = get_target_mesh_object(context) or context.active_object
        if not sel_meshes and active_obj and getattr(active_obj, "type", None) == 'MESH':
            sel_meshes = [active_obj]
        if not sel_meshes:
            self.report({'WARNING'}, "ReSkate: Select 2 or more prop meshes to group as variants.")
            return {'CANCELLED'}

        fam = (self.family_title or "").strip()
        if not fam and prop:
            fam = str(getattr(prop, "prop_family_title", "") or "").strip()
        if not fam:
            base_obj = active_obj if (active_obj in sel_meshes) else sel_meshes[0]
            raw_disp = compiler_bridge.get_prop_display_name(base_obj, context)
            fam = re.sub(r"\s*(?:\(?Var(?:iant)?\s*\d+\)?|[_-]?v\d+|\s+\d+)$", "", raw_disp, flags=re.IGNORECASE).strip() or raw_disp

        base_obj = active_obj if (active_obj in sel_meshes) else sel_meshes[0]
        base_st = get_or_init_object_prop_state(base_obj)
        shared_cat = "CUSTOM_TAB"
        shared_tab = str(getattr(prop, "custom_qd_tab", "") if prop else "") or str(base_st.get("custom_qd_tab") or "CustomProps")

        for m_obj in sel_meshes:
            m_obj["reskate_family_title"] = fam
            m_obj["reskate_user_set_variant_cutout"] = False
            m_obj["reskate_thumb_variant_cutout_enabled"] = False
            st = get_or_init_object_prop_state(m_obj)
            st["prop_family_title"] = fam
            st["prop_category"] = shared_cat
            st["custom_qd_tab"] = shared_tab
            st["thumb_variant_cutout_enabled"] = False
            save_object_prop_state(m_obj, st)
            try:
                from . import ui_panels as _uip
                if hasattr(_uip, "invalidate_carousel_icon_for_object"):
                    _uip.invalidate_carousel_icon_for_object(m_obj)
            except Exception:
                pass

        if prop is not None:
            act_target = get_target_mesh_object(context) or base_obj
            load_prop_settings_from_object(prop, act_target)

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Grouped {len(sel_meshes)} prop(s) into Variant Family '{fam}'.")
        return {'FINISHED'}


class RESKATE_OT_clear_variant_family(bpy.types.Operator):
    """Clears the ReSkate 2.1.1 Object Family assignment from the active (or selected) props"""
    bl_idname = "reskate.clear_variant_family"
    bl_label = "Ungroup Variant Family"
    bl_description = "Removes the Variant Family title from the active/selected props so each prop has its own standalone Quick Drop tile"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        active_obj = get_target_mesh_object(context) or context.active_object
        targets = sel_meshes if sel_meshes else ([active_obj] if (active_obj and getattr(active_obj, "type", None) == 'MESH') else [])
        for m_obj in targets:
            m_obj["reskate_family_title"] = ""
            m_obj["reskate_user_set_variant_cutout"] = False
            m_obj["reskate_thumb_variant_cutout_enabled"] = False
            st = get_or_init_object_prop_state(m_obj)
            st["prop_family_title"] = ""
            st["thumb_variant_cutout_enabled"] = False
            save_object_prop_state(m_obj, st)
            try:
                from . import ui_panels as _uip
                if hasattr(_uip, "invalidate_carousel_icon_for_object"):
                    _uip.invalidate_carousel_icon_for_object(m_obj)
            except Exception:
                pass
        if prop is not None:
            act_target = get_target_mesh_object(context) or active_obj
            if act_target and getattr(act_target, "type", None) == 'MESH':
                load_prop_settings_from_object(prop, act_target)
        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Cleared Variant Family on {len(targets)} prop(s).")
        return {'FINISHED'}


class RESKATE_OT_render_thumbnails(bpy.types.Operator):
    """Renders 256×256 and 768×768 Quick Drop thumbnails for the active prop (backs up any previous render to .bak.png)"""
    bl_idname = "reskate.render_thumbnails"
    bl_label = "Render Thumbnail"
    bl_description = "Renders the 256×256 Quick Drop wheel icon and 768×768 inspection card image (backs up existing thumbnails to .bak.png before overwriting)"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        if bpy.app.background or not getattr(context, "window_manager", None):
            return self.execute(context)
        obj = get_target_mesh_object(context) or context.active_object
        slug = get_prop_slug(context, obj) if obj else "own_customprops_prop"
        staging_dir = compiler_bridge.get_staging_dir(context)
        path_256 = staging_dir / f"{slug}_thumb_256.png"
        if path_256.exists() and path_256.stat().st_size > 200:
            self._existing_thumb_name = path_256.name
            return context.window_manager.invoke_props_dialog(self, width=380)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text=f"Thumbnail '{getattr(self, '_existing_thumb_name', '')}' already exists.", icon='INFO')
        box.label(text="Existing files will be backed up to .bak.png before rendering.")

    def execute(self, context):
        scene = context.scene
        prop = getattr(scene, "reskate_prop", None)
        obj = get_target_mesh_object(context) or context.active_object

        # Preserve manual camera adjustments when re-rendering the same prop; ALWAYS re-frame when switching to a different prop!
        cam_obj = scene.objects.get("ReSkate_Thumbnail_Camera") or scene.objects.get("ReSkate_Thumb_Camera") or scene.camera
        if not cam_obj or cam_obj.type != 'CAMERA':
            cam_obj = _setup_isometric_camera(context)
        else:
            target_owner = str(cam_obj.get("reskate_target_obj", ""))
            if obj and target_owner != obj.name:
                cam_obj = _setup_isometric_camera(context)
            elif not _was_camera_manually_moved(cam_obj) and not target_owner:
                cam_obj = _setup_isometric_camera(context)
            else:
                scene.camera = cam_obj

        preset_now = str(getattr(prop, "thumb_bg_preset", "SUNNY_DAY") or "SUNNY_DAY").upper() if prop else "SUNNY_DAY"
        bg_style = getattr(prop, "thumb_bg_style", 'TRANSPARENT') if prop else 'TRANSPARENT'
        use_floor = bool(getattr(prop, "thumb_use_cyclorama_floor", False)) if prop else False
        if preset_now in ("SUNNY_DAY", "STUDIO"):
            use_floor = True
            bg_style = 'STUDIO_WARM' if preset_now == "SUNNY_DAY" else 'STUDIO_DARK'
        elif preset_now == "TRANSPARENT":
            use_floor = False
            bg_style = 'TRANSPARENT'
        elif preset_now == "FLAT_COLOR":
            use_floor = False
            bg_style = 'FLAT_COLOR'
        bg_col = tuple(getattr(prop, "thumb_bg_color", (0.14, 0.16, 0.20))) if prop else (0.14, 0.16, 0.20)
        floor_col = tuple(getattr(prop, "thumb_floor_color", bg_col)) if prop else bg_col
        key_fill = max(0.25, float(getattr(prop, "thumb_key_fill_ratio", 2.2) or 2.2)) if prop else 2.2
        rim_col = tuple(getattr(prop, "thumb_rim_tint", (0.90, 0.95, 1.00))) if prop else (0.90, 0.95, 1.00)
        light_mode = str(getattr(prop, "thumb_lighting_mode", 'STUDIO_3PT') or 'STUDIO_3PT') if prop else 'STUDIO_3PT'
        if preset_now == "SUNNY_DAY":
            light_mode = 'SKY_SUN'
        elif preset_now == "STUDIO":
            light_mode = 'STUDIO_3PT'
        sun_elev = float(getattr(prop, "thumb_sun_elevation", 38.0)) if prop else 38.0
        sun_rot = float(getattr(prop, "thumb_sun_rotation", 42.0)) if prop else 42.0
        thumb_exp = float(getattr(prop, "thumb_exposure", 0.0)) if prop else 0.0

        obj_st_thumb = get_or_init_object_prop_state(obj) if (obj is not None and obj.type == 'MESH') else {}
        fam_title = str(
            (getattr(prop, "prop_family_title", "") if prop is not None else "")
            or obj_st_thumb.get("prop_family_title", "")
            or (obj.get("reskate_family_title", "") if obj is not None else "")
            or ""
        ).strip()
        has_variant_notch = bool(fam_title)

        prev_film_transparent = bool(scene.render.film_transparent)
        prev_color_mode = str(scene.render.image_settings.color_mode)
        prev_res_x = int(scene.render.resolution_x)
        prev_res_y = int(scene.render.resolution_y)
        prev_filepath = str(scene.render.filepath)
        prev_exposure = float(scene.view_settings.exposure) if hasattr(scene, "view_settings") else 0.0
        prev_world = scene.world

        if bg_style == 'TRANSPARENT' and not use_floor:
            scene.render.film_transparent = True
            scene.render.image_settings.color_mode = 'RGBA'
        else:
            scene.render.film_transparent = False
            scene.render.image_settings.color_mode = 'RGB'

        scene.render.image_settings.file_format = 'PNG'

        slug = get_prop_slug(context, obj) if obj else "own_customprops_prop"
        staging_dir = compiler_bridge.get_staging_dir(context)
        staging_dir.mkdir(parents=True, exist_ok=True)

        path_768 = staging_dir / f"{slug}_thumb_768.png"
        path_256 = staging_dir / f"{slug}_thumb_256.png"

        # Back up existing thumbnails to .bak.png before overwriting
        for tp in (path_256, path_768):
            if tp.exists() and tp.stat().st_size > 200:
                try:
                    shutil.copy2(tp, tp.with_suffix(".bak.png"))
                except Exception:
                    pass

        # Compute prop world bounds for auto-scaled studio lighting & cyclorama floor at Z = min_z
        if obj and obj.type == 'MESH' and len(obj.data.vertices) > 0:
            bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            center = sum(bbox_corners, Vector()) / 8.0
            min_z = min(p.z for p in bbox_corners)
            dims_w = (
                max(p.x for p in bbox_corners) - min(p.x for p in bbox_corners),
                max(p.y for p in bbox_corners) - min(p.y for p in bbox_corners),
                max(p.z for p in bbox_corners) - min(p.z for p in bbox_corners),
            )
            max_dim = max(0.5, max(dims_w))
        else:
            center = Vector((0.0, 0.0, 0.5))
            min_z = 0.0
            max_dim = 2.0

        # Isolate the active prop + its hierarchy + linked/scene lights unless 'thumb_render_as_is' is True
        render_as_is = bool(getattr(prop, "thumb_render_as_is", False)) if prop else False
        saved_hide_render: list[tuple[bpy.types.Object, bool]] = []
        temp_objs: list[bpy.types.Object] = []
        temp_lights: list[bpy.types.Light] = []
        temp_meshes: list[bpy.types.Mesh] = []
        temp_mats: list[bpy.types.Material] = []
        temp_worlds: list[bpy.types.World] = []

        if obj and not render_as_is:
            keep_visible: set[bpy.types.Object] = {obj, cam_obj}
            stack = list(obj.children)
            while stack:
                ch = stack.pop()
                is_col = ch.name.endswith("_COL") or getattr(getattr(ch, "reskate_col_prop", None), "is_col_proxy", False)
                if not is_col:
                    keep_visible.add(ch)
                stack.extend(ch.children)

            linked_lights = [
                it.light_obj for it in getattr(obj, "reskate_linked_lights", [])
                if getattr(it, "enabled", True) and getattr(it, "light_obj", None) is not None
            ]
            if linked_lights:
                keep_visible.update(linked_lights)
            elif light_mode == 'SCENE_AS_IS':
                for so in scene.objects:
                    if so.type == 'LIGHT':
                        keep_visible.add(so)

            for so in scene.objects:
                saved_hide_render.append((so, bool(so.hide_render)))
                if so.name.startswith("_ReSkate_Live_Thumb_") or so not in keep_visible:
                    so.hide_render = True

        try:
            if hasattr(scene, "view_settings"):
                scene.view_settings.exposure = prev_exposure + thumb_exp

            # Spawn temporary lighting rig or sky world if requested
            if light_mode == 'STUDIO_3PT':
                dist_scale = max(1.8, max_dim * 2.2)
                area_power_scale = (dist_scale / 2.2) ** 2
                fill_power = (140.0 * area_power_scale) / max(0.25, key_fill)
                rig_specs = [
                    ("ReSkate_Temp_KeyLight", (dist_scale * 0.85, -dist_scale * 0.95, dist_scale * 1.15), 140.0 * area_power_scale, (1.0, 0.98, 0.95), dist_scale * 0.85, True),
                    ("ReSkate_Temp_FillLight", (-dist_scale * 1.05, -dist_scale * 0.65, dist_scale * 0.65), fill_power, (0.85, 0.92, 1.0), dist_scale * 1.10, False),
                    ("ReSkate_Temp_RimLight", (0.0, dist_scale * 1.10, dist_scale * 0.90), 95.0 * area_power_scale, rim_col, dist_scale * 0.75, False),
                ]
                for l_name, rel_pos, l_energy, l_col, l_size, l_shad in rig_specs:
                    ld = bpy.data.lights.new(name=l_name, type='AREA')
                    ld.energy = float(l_energy)
                    ld.color = (float(l_col[0]), float(l_col[1]), float(l_col[2]))
                    ld.size = float(l_size)
                    ld.use_shadow = bool(l_shad)
                    lo = bpy.data.objects.new(name=l_name, object_data=ld)
                    scene.collection.objects.link(lo)
                    lo.location = center + Vector(rel_pos)
                    aim_dir = (center - lo.location).normalized()
                    if aim_dir.length > 1e-5:
                        lo.rotation_euler = aim_dir.to_track_quat('-Z', 'Y').to_euler()
                    temp_lights.append(ld)
                    temp_objs.append(lo)

            elif light_mode == 'SKY_SUN':
                elev_r = math.radians(sun_elev)
                rot_r = math.radians(sun_rot)
                sun_dir = Vector((
                    math.cos(elev_r) * math.sin(rot_r),
                    -math.cos(elev_r) * math.cos(rot_r),
                    math.sin(elev_r),
                )).normalized()
                ld = bpy.data.lights.new(name="ReSkate_Temp_Sun", type='SUN')
                ld.energy = 3.2
                ld.color = (1.0, 0.97, 0.91)
                ld.angle = math.radians(1.5)
                ld.use_shadow = True
                lo = bpy.data.objects.new(name="ReSkate_Temp_Sun", object_data=ld)
                scene.collection.objects.link(lo)
                lo.location = center + sun_dir * max(8.0, max_dim * 3.0)
                lo.rotation_euler = (-sun_dir).to_track_quat('-Z', 'Y').to_euler()
                temp_lights.append(ld)
                temp_objs.append(lo)

                w_sky = bpy.data.worlds.new(name="ReSkate_Temp_SkyWorld")
                w_sky.use_nodes = True
                temp_worlds.append(w_sky)
                w_nt = w_sky.node_tree
                bg_node = next((n for n in w_nt.nodes if n.type == "BACKGROUND"), None)
                if bg_node:
                    try:
                        sky_node = w_nt.nodes.new("ShaderNodeTexSky")
                        try:
                            sky_node.sky_type = 'MULTIPLE_SCATTERING'
                        except Exception:
                            try:
                                sky_node.sky_type = 'NISHITA'
                            except Exception:
                                pass
                        if hasattr(sky_node, "sun_elevation"):
                            sky_node.sun_elevation = elev_r
                        if hasattr(sky_node, "sun_rotation"):
                            sky_node.sun_rotation = rot_r
                        w_nt.links.new(sky_node.outputs["Color"], bg_node.inputs["Color"])
                        bg_node.inputs["Strength"].default_value = 0.35
                    except Exception:
                        bg_node.inputs["Color"].default_value = (float(bg_col[0]), float(bg_col[1]), float(bg_col[2]), 1.0)
                scene.world = w_sky

            # Spawn temporary colored world + shadow-receiving studio floor if cyclorama floor or non-transparent backdrop is active
            if use_floor or bg_style != 'TRANSPARENT':
                if not temp_worlds:
                    w_bg = bpy.data.worlds.new(name="ReSkate_Temp_BackdropWorld")
                    w_bg.use_nodes = True
                    temp_worlds.append(w_bg)
                    bg_node = next((n for n in w_bg.node_tree.nodes if n.type == "BACKGROUND"), None)
                    if bg_node:
                        bg_node.inputs["Color"].default_value = (float(bg_col[0]), float(bg_col[1]), float(bg_col[2]), 1.0)
                        bg_node.inputs["Strength"].default_value = 1.0
                    scene.world = w_bg

                if use_floor or bg_style in ('STUDIO_DARK', 'STUDIO_WARM'):
                    floor_rad = max(25.0, max_dim * 12.0)
                    f_verts = [
                        (center.x - floor_rad, center.y - floor_rad, min_z - 0.001),
                        (center.x + floor_rad, center.y - floor_rad, min_z - 0.001),
                        (center.x + floor_rad, center.y + floor_rad, min_z - 0.001),
                        (center.x - floor_rad, center.y + floor_rad, min_z - 0.001),
                    ]
                    f_mesh = bpy.data.meshes.new("ReSkate_Temp_Cyclorama_Mesh")
                    f_mesh.from_pydata(f_verts, [], [(0, 1, 2, 3)])
                    f_mesh.update()
                    temp_meshes.append(f_mesh)

                    f_mat = bpy.data.materials.new("ReSkate_Temp_Cyclorama_Mat")
                    f_mat.use_nodes = True
                    temp_mats.append(f_mat)
                    p_bsdf = next((n for n in f_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                    if p_bsdf:
                        p_bsdf.inputs["Base Color"].default_value = (float(floor_col[0]), float(floor_col[1]), float(floor_col[2]), 1.0)
                        p_bsdf.inputs["Roughness"].default_value = 0.88
                        if "Specular IOR Level" in p_bsdf.inputs:
                            p_bsdf.inputs["Specular IOR Level"].default_value = 0.15
                    f_mesh.materials.append(f_mat)

                    f_obj = bpy.data.objects.new("ReSkate_Temp_Cyclorama_Floor", f_mesh)
                    scene.collection.objects.link(f_obj)
                    temp_objs.append(f_obj)

            context.view_layer.update()

            scene.render.resolution_x = 768
            scene.render.resolution_y = 768
            scene.render.filepath = str(path_768)
            bpy.ops.render.render(write_still=True)

            # Single-pass 768² render with PIL Lanczos downsampling to 256² (and bottom-left variant corner notch if in a Variant Family)
            if Image is not None and path_768.exists():
                try:
                    with Image.open(path_768) as im768:
                        rgba768 = im768.convert("RGBA")
                        arr768 = np.array(rgba768, dtype=np.uint8)
                    im768_obj = Image.fromarray(arr768, mode="RGBA")
                    im256 = im768_obj.resize((256, 256), Image.Resampling.LANCZOS)
                    im256.save(path_256, "PNG")
                except Exception:
                    scene.render.resolution_x = 256
                    scene.render.resolution_y = 256
                    scene.render.filepath = str(path_256)
                    bpy.ops.render.render(write_still=True)
            else:
                scene.render.resolution_x = 256
                scene.render.resolution_y = 256
                scene.render.filepath = str(path_256)
                bpy.ops.render.render(write_still=True)

            if has_variant_notch:
                compiler_bridge.apply_variant_corner_notch_to_png_in_place(path_768)
                compiler_bridge.apply_variant_corner_notch_to_png_in_place(path_256)
        finally:
            scene.render.film_transparent = prev_film_transparent
            scene.render.image_settings.color_mode = prev_color_mode
            scene.render.resolution_x = prev_res_x
            scene.render.resolution_y = prev_res_y
            scene.render.filepath = prev_filepath
            if hasattr(scene, "view_settings"):
                scene.view_settings.exposure = prev_exposure
            scene.world = prev_world

            for to in temp_objs:
                try:
                    bpy.data.objects.remove(to, do_unlink=True)
                except Exception:
                    pass
            for tl in temp_lights:
                try:
                    bpy.data.lights.remove(tl, do_unlink=True)
                except Exception:
                    pass
            for tm in temp_meshes:
                try:
                    bpy.data.meshes.remove(tm, do_unlink=True)
                except Exception:
                    pass
            for tmat in temp_mats:
                try:
                    bpy.data.materials.remove(tmat, do_unlink=True)
                except Exception:
                    pass
            for tw in temp_worlds:
                try:
                    bpy.data.worlds.remove(tw, do_unlink=True)
                except Exception:
                    pass

            for so, orig_hide in saved_hide_render:
                try:
                    so.hide_render = orig_hide
                except Exception:
                    pass

        if obj:
            obj["reskate_rendered_thumb_256"] = str(path_256)
            obj["reskate_rendered_thumb_768"] = str(path_768)
            obj["reskate_thumb_rendered"] = True
        if prop:
            prop.custom_thumb_png = str(path_256)
            if obj:
                prop["custom_thumb_owner"] = obj.name

        # Reload/refresh rendered thumbnail in bpy.data.images so inline UI preview updates immediately
        try:
            for img in list(bpy.data.images):
                try:
                    if Path(bpy.path.abspath(img.filepath)).resolve() in (path_256.resolve(), path_768.resolve()):
                        img.reload()
                        if not bpy.app.background:
                            img.preview_ensure()
                except Exception:
                    pass
            preview_img = bpy.data.images.load(str(path_256), check_existing=True)
            preview_img.reload()
            if not bpy.app.background:
                preview_img.preview_ensure()
        except Exception:
            pass

        try:
            from . import ui_panels
            ui_panels.invalidate_panel_caches()
        except Exception:
            pass

        self.report({'INFO'}, f"ReSkate: Rendered thumbnails for '{obj.name if obj else slug}' ({path_256.name} & {path_768.name}).")
        return {'FINISHED'}


# ==============================================================================
# Step 7: Ship Operators — Non-Blocking Progress, Cancel, Live Log & Pack Queue
# ==============================================================================

def _extract_concise_error_summary(log_lines: list[str], ret_code: int) -> str:
    """Extracts a 1-line error summary from compiler output and appends the Preferences log pointer (§7.4)."""
    summary = f"Compilation failed (exit code {ret_code})."
    for ln in reversed(log_lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("ERROR ") or "Error:" in s or "Exception:" in s or "RuntimeError:" in s or "ValueError:" in s or "FileNotFoundError:" in s:
            clean = s[6:].strip() if s.startswith("ERROR ") else s
            summary = clean[:160]
            break
    return f"{summary} Full log: Edit → Preferences → Add-ons → ReSkate Prop Studio."


def _parse_compiler_output_line(prop, line: str, log_lines: list[str]):
    """Parses structured STEP / STAGE / PROGRESS / ERROR / DONE lines emitted by compile_blender_prop.py."""
    clean = line.strip()
    if not clean:
        return
    log_lines.append(clean)
    if clean.startswith(("STEP ", "STAGE ")):
        parts = clean.split(" ", 2)
        if len(parts) >= 3:
            prop.compile_step_label = f"Step {parts[1]} — {parts[2]}"
            prop.compile_status_msg = f"Step {parts[1]} — {parts[2]}"
    elif clean.startswith("PROGRESS "):
        parts = clean.split(" ", 2)
        if len(parts) >= 2:
            try:
                prop.compile_progress = float(parts[1])
            except ValueError:
                pass
        if len(parts) >= 3:
            prop.compile_status_msg = parts[2]
    elif clean.startswith("ERROR "):
        prop.compile_has_error = True
        prop.compile_status_msg = clean[6:].strip()
    elif clean.startswith("DONE "):
        prop.compile_progress = 100.0
        prop.compile_status_msg = f"Done → {clean[5:].strip()}"


def _maybe_auto_bump_pack_version(prop) -> str | None:
    """If `prop.auto_bump_pack_version` is enabled, increments the patch version (e.g. 0.1.0 -> 0.1.1) and returns the new version."""
    if not prop or not bool(getattr(prop, "auto_bump_pack_version", True)):
        return None
    cur = (getattr(prop, "pack_version", "0.1.0") or "0.1.0").strip()
    toks = [int(t) if t.isdigit() else 0 for t in (cur.split(".") + ["0", "0", "0"])[:3]]
    toks[2] += 1
    new_v = f"{toks[0]}.{toks[1]}.{toks[2]}"
    prop.pack_version = new_v
    return new_v


def _start_async_or_sync_compile(op_self, context, manifest_path: Path, is_pack: bool, disp_label: str):
    """
    Runs compile_blender_prop.py synchronously when in background/headless mode (so unit tests work deterministically),
    or asynchronously via non-blocking modal timer with progress bar in interactive GUI mode (§5.3).
    """
    global _ACTIVE_COMPILE_PROC, _ACTIVE_COMPILE_QUEUE, _ACTIVE_COMPILE_CANCELLED

    prop = getattr(context.scene, "reskate_prop", None)
    if prop:
        prop.is_compiling = True
        prop.compile_progress = 2.0
        prop.compile_step_label = "Step 1/6 — Starting compiler..."
        prop.compile_status_msg = f"Compiling {disp_label}..."
        prop.compile_has_error = False
        prop.compile_log_text = ""
        if hasattr(prop, "last_compile_error_summary"):
            prop.last_compile_error_summary = ""

    # In headless mode (`--background`) or if no window manager modal is available, run synchronously while parsing STEP/PROGRESS
    if bpy.app.background or not getattr(context, "window", None):
        ret, stdout, stderr = compiler_bridge.run_compiler_subprocess(manifest_path, pack_manifest=is_pack)
        log_lines = []
        if prop:
            for ln in (stdout or "").splitlines():
                _parse_compiler_output_line(prop, ln, log_lines)
            for ln in (stderr or "").splitlines():
                if ln.strip():
                    log_lines.append(ln.strip())
            full_log = "\n".join(log_lines)
            prop.compile_log_text = full_log
            if hasattr(prop, "last_compile_log"):
                prop.last_compile_log = full_log
            prefs = get_addon_preferences(context)
            if prefs is not None and hasattr(prefs, "last_compile_log"):
                prefs.last_compile_log = full_log
            prop.is_compiling = False

        if ret != 0:
            one_line = _extract_concise_error_summary(log_lines, ret)
            if prop:
                prop.compile_has_error = True
                prop.compile_status_msg = one_line
                if hasattr(prop, "last_compile_error_summary"):
                    prop.last_compile_error_summary = one_line
            op_self.report({'ERROR'}, f"ReSkate: {one_line}")
            return {'CANCELLED'}

        if prop:
            prop.compile_progress = 100.0
            if hasattr(prop, "last_compile_error_summary"):
                prop.last_compile_error_summary = ""
            _maybe_auto_bump_pack_version(prop)
        op_self.report({'INFO'}, f"ReSkate: '{disp_label}' compiled and deployed!")
        return {'FINISHED'}

    # Interactive GUI mode: launch subprocess.Popen + reader thread + modal timer + wm.progress_begin
    cmd = compiler_bridge.build_compiler_command(manifest_path, pack_manifest=is_pack)
    q = py_queue.Queue()
    _ACTIVE_COMPILE_QUEUE = q
    _ACTIVE_COMPILE_CANCELLED = False

    try:
        studio_cwd = compiler_bridge.get_studio_dir(context)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(studio_cwd if studio_cwd.exists() else STUDIO_DIR),
            env=compiler_bridge.build_compiler_env(context),
        )
    except Exception as ex:
        one_line = f"Failed to launch compiler: {ex}. Full log: Edit → Preferences → Add-ons → ReSkate Prop Studio."
        if prop:
            prop.is_compiling = False
            prop.compile_has_error = True
            prop.compile_status_msg = one_line
            if hasattr(prop, "last_compile_error_summary"):
                prop.last_compile_error_summary = one_line
        op_self.report({'ERROR'}, f"ReSkate: {one_line}")
        return {'CANCELLED'}

    _ACTIVE_COMPILE_PROC = proc

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        context.window_manager.progress_begin(0, 100)
        context.window_manager.progress_update(2)
    except Exception:
        pass
    op_self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
    op_self._log_lines = []
    op_self._disp_label = disp_label
    if not getattr(op_self, "_is_modal_active", False):
        context.window_manager.modal_handler_add(op_self)
        op_self._is_modal_active = True
    return {'RUNNING_MODAL'}


def _terminate_proc_tree(proc):
    """Terminates a subprocess and its entire child process tree (e.g., reskate_cli.exe, Havok CLI, texconv)."""
    if proc is None:
        return
    try:
        pid = getattr(proc, "pid", None)
        if sys.platform == "win32" and pid:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=3,
            )
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _handle_compile_modal(op_self, context, event):
    global _ACTIVE_COMPILE_PROC, _ACTIVE_COMPILE_QUEUE, _ACTIVE_COMPILE_CANCELLED
    prop = getattr(context.scene, "reskate_prop", None)

    if event.type == 'ESC' or _ACTIVE_COMPILE_CANCELLED:
        if _ACTIVE_COMPILE_PROC and _ACTIVE_COMPILE_PROC.poll() is None:
            _terminate_proc_tree(_ACTIVE_COMPILE_PROC)
        if getattr(op_self, "_timer", None):
            context.window_manager.event_timer_remove(op_self._timer)
        try:
            context.window_manager.progress_end()
        except Exception:
            pass
        if prop:
            prop.is_compiling = False
            prop.compile_status_msg = "Compilation cancelled by user."
        _ACTIVE_COMPILE_PROC = None
        op_self.report({'WARNING'}, "ReSkate: Compilation cancelled.")
        return {'CANCELLED'}

    if event.type == 'TIMER':
        q = _ACTIVE_COMPILE_QUEUE
        updated = False
        while q and not q.empty():
            try:
                line = q.get_nowait()
                if prop:
                    _parse_compiler_output_line(prop, line, op_self._log_lines)
                updated = True
            except py_queue.Empty:
                break

        if updated and prop:
            full_log = "\n".join(op_self._log_lines)
            prop.compile_log_text = full_log
            if hasattr(prop, "last_compile_log"):
                prop.last_compile_log = full_log
            prefs = get_addon_preferences(context)
            if prefs is not None and hasattr(prefs, "last_compile_log"):
                prefs.last_compile_log = full_log
            try:
                context.window_manager.progress_update(max(0, min(100, int(prop.compile_progress))))
            except Exception:
                pass
            for area in context.screen.areas if context.screen else []:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        if _ACTIVE_COMPILE_PROC is not None and _ACTIVE_COMPILE_PROC.poll() is not None:
            ret = _ACTIVE_COMPILE_PROC.returncode
            while q and not q.empty():
                try:
                    line = q.get_nowait()
                    if prop:
                        _parse_compiler_output_line(prop, line, op_self._log_lines)
                except py_queue.Empty:
                    break
            if prop:
                full_log = "\n".join(op_self._log_lines)
                prop.compile_log_text = full_log
                if hasattr(prop, "last_compile_log"):
                    prop.last_compile_log = full_log
                prefs = get_addon_preferences(context)
                if prefs is not None and hasattr(prefs, "last_compile_log"):
                    prefs.last_compile_log = full_log
                prop.is_compiling = False
            if getattr(op_self, "_timer", None):
                context.window_manager.event_timer_remove(op_self._timer)
            try:
                context.window_manager.progress_end()
            except Exception:
                pass
            _ACTIVE_COMPILE_PROC = None

            if ret == 0:
                if prop:
                    prop.compile_progress = 100.0
                    if hasattr(prop, "last_compile_error_summary"):
                        prop.last_compile_error_summary = ""
                    _maybe_auto_bump_pack_version(prop)
                op_self.report({'INFO'}, f"ReSkate: '{op_self._disp_label}' compiled and deployed!")
                return {'FINISHED'}
            else:
                one_line = _extract_concise_error_summary(op_self._log_lines, ret)
                if prop:
                    prop.compile_has_error = True
                    prop.compile_status_msg = one_line
                    if hasattr(prop, "last_compile_error_summary"):
                        prop.last_compile_error_summary = one_line
                op_self.report({'ERROR'}, f"ReSkate: {one_line}")
                return {'CANCELLED'}

    return {'PASS_THROUGH'}


def _handle_compile_or_prep_modal(op_self, context, event):
    stage = getattr(op_self, "_stage", "COMPILE")
    if stage == 'PREP':
        prop = getattr(context.scene, "reskate_prop", None)
        if event.type == 'ESC' or _ACTIVE_COMPILE_CANCELLED:
            gen = getattr(op_self, "_prep_generator", None)
            if gen:
                try:
                    gen.close()
                except Exception:
                    pass
            if getattr(op_self, "_timer", None):
                try:
                    context.window_manager.event_timer_remove(op_self._timer)
                except Exception:
                    pass
            try:
                context.window_manager.progress_end()
            except Exception:
                pass
            if prop:
                prop.is_compiling = False
                prop.compile_status_msg = "Export preparation cancelled by user."
            op_self.report({'WARNING'}, "ReSkate: Export preparation cancelled.")
            return {'CANCELLED'}

        if event.type == 'TIMER':
            try:
                status = next(op_self._prep_generator)
                lbl, pct = status[0], status[1]
                try:
                    context.window_manager.progress_update(max(0, min(100, int(pct))))
                except Exception:
                    pass
                if prop:
                    prop.compile_step_label = str(lbl)
                    prop.compile_progress = float(pct)
                for area in context.screen.areas if context.screen else []:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                return {'PASS_THROUGH'}
            except StopIteration as result:
                # Stage 1 complete!
                compiler_bridge.invalidate_ui_caches()
                if getattr(op_self, "_timer", None):
                    try:
                        context.window_manager.event_timer_remove(op_self._timer)
                    except Exception:
                        pass
                op_self._stage = 'COMPILE'
                res = result.value
                manifest_path = res[0] if isinstance(res, (tuple, list)) else res
                is_pack = bool(res[1]) if (isinstance(res, (tuple, list)) and len(res) > 1) else False
                disp_label = str(res[2]) if (isinstance(res, (tuple, list)) and len(res) > 2) else "Prop"
                return _start_async_or_sync_compile(op_self, context, manifest_path, is_pack=is_pack, disp_label=disp_label)
            except Exception as ex:
                gen = getattr(op_self, "_prep_generator", None)
                if gen:
                    try:
                        gen.close()
                    except Exception:
                        pass
                if getattr(op_self, "_timer", None):
                    try:
                        context.window_manager.event_timer_remove(op_self._timer)
                    except Exception:
                        pass
                try:
                    context.window_manager.progress_end()
                except Exception:
                    pass
                if prop:
                    prop.is_compiling = False
                    prop.compile_has_error = True
                    prop.compile_status_msg = f"Export preparation failed: {ex}"
                op_self.report({'ERROR'}, f"ReSkate: Pre-export check failed: {ex}")
                return {'CANCELLED'}

        return {'PASS_THROUGH'}

    # Stage 2: Background compiler subprocess monitor
    return _handle_compile_modal(op_self, context, event)


class RESKATE_OT_compile_and_deploy(bpy.types.Operator):
    """Exports the active prop (or all selected props) and compiles into a standalone ReSkate Quick Drop mod"""
    bl_idname = "reskate.compile_and_deploy"
    bl_label = "Compile and Deploy This Prop"
    bl_description = "Exports the selected mesh(es), collision, textures, lights, and thumbnails, then compiles and deploys the prop pack"
    bl_options = {'REGISTER'}

    merge_existing: BoolProperty(
        name="Merge into Existing Pack (Keep Existing Props)",
        default=True,
        description="When enabled, adds/updates this prop inside the staging pack registry and recompiles all props together instead of replacing the pack",
    )

    def invoke(self, context, event):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if len(sel_objs) >= 2:
            return bpy.ops.reskate.compile_selected_props('INVOKE_DEFAULT')
        if bpy.app.background or not getattr(context, "window_manager", None):
            return self.execute(context)
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        obj = get_target_mesh_object(context)
        info = compiler_bridge.inspect_existing_mod_props(target_mod)
        if info.get("exists") and info.get("count", 0) > 0:
            self._existing_mod_info = info
            self._compile_diff = compiler_bridge.compute_pack_compile_diff(context, target_mod, single_obj=obj)
            self.merge_existing = True
            return context.window_manager.invoke_props_dialog(self, width=440)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        info = getattr(self, "_existing_mod_info", {})
        diff = getattr(self, "_compile_diff", {})
        cnt = info.get("count", 0)
        box = layout.box()
        box.label(text=f"Pack already has {cnt} prop(s) in staging registry:", icon='INFO')
        if diff.get("adding"):
            box.label(text=f"• Adding: {', '.join(diff['adding'])}", icon='ADD')
        if diff.get("updating"):
            box.label(text=f"• Updating: {', '.join(diff['updating'])}", icon='FILE_REFRESH')
        if diff.get("unchanged"):
            box.label(text=f"• Unchanged: {', '.join(diff['unchanged'][:5])}", icon='CHECKMARK')
        if diff.get("keeping"):
            box.label(text=f"• Keeping: {', '.join(diff['keeping'][:5])}", icon='CHECKMARK')
        box.prop(self, "merge_existing")

    def execute(self, context):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if len(sel_objs) >= 2:
            return bpy.ops.reskate.compile_selected_props(merge_existing=self.merge_existing)
        obj = get_target_mesh_object(context)
        if not obj:
            self.report({'WARNING'}, "ReSkate: Please select a 3D mesh object first.")
            return {'CANCELLED'}

        try:
            obj["_reskate_last_preflight_json"] = json.dumps(compiler_bridge.run_preflight_checks(context, obj))
        except Exception:
            pass

        compiler_bridge.lock_stable_prop_key_on_object(obj, context)
        prop = getattr(context.scene, "reskate_prop", None)
        if prop and (self.properties.is_property_set("merge_existing") or hasattr(self, "_existing_mod_info")):
            prop.merge_existing_mod = bool(self.merge_existing)

        if bpy.app.background or not getattr(context, "window_manager", None):
            try:
                manifest_data, manifest_path = compiler_bridge.build_single_prop_manifest(context, obj)
            except Exception as ex:
                self.report({'ERROR'}, f"ReSkate: Pre-export check failed: {ex}")
                return {'CANCELLED'}

            compiler_bridge.invalidate_ui_caches()
            disp = manifest_data.get('display_name', obj.name)
            return _start_async_or_sync_compile(self, context, manifest_path, is_pack=False, disp_label=disp)

        # Stage 1: Non-blocking Two-Stage Modal Generator State Machine
        global _ACTIVE_COMPILE_CANCELLED
        _ACTIVE_COMPILE_CANCELLED = False
        self._stage = 'PREP'
        self._prep_generator = compiler_bridge.build_single_prop_manifest_generator(context, obj)
        if prop:
            prop.is_compiling = True
            prop.compile_has_error = False
            prop.compile_step_label = f"Preparing export for '{obj.name}'..."
            prop.compile_progress = 0.0
        try:
            context.window_manager.progress_begin(0, 100)
            context.window_manager.progress_update(0)
        except Exception:
            pass
        self._timer = context.window_manager.event_timer_add(0.02, window=context.window)
        if not getattr(self, "_is_modal_active", False):
            context.window_manager.modal_handler_add(self)
            self._is_modal_active = True
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        return _handle_compile_or_prep_modal(self, context, event)


class RESKATE_OT_compile_selected_props(bpy.types.Operator):
    """Compiles and deploys all currently selected mesh props in the 3D Viewport / Outliner into the active pack"""
    bl_idname = "reskate.compile_selected_props"
    bl_label = "Compile and Deploy Prop Pack"
    bl_description = "Compiles and deploys all selected mesh props into the active Prop Pack (updates existing props in the pack and adds any new ones)"
    bl_options = {'REGISTER'}

    merge_existing: BoolProperty(
        name="Merge into Existing Pack (Keep Existing Props)",
        default=True,
        description="When enabled, updates any selected props already in the pack, adds any new selected props, and keeps all other props in the pack",
    )

    def invoke(self, context, event):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not sel_objs:
            self.report({'WARNING'}, "ReSkate: Select at least one 3D mesh prop in the Viewport or Outliner.")
            return {'CANCELLED'}
        if bpy.app.background or not getattr(context, "window_manager", None):
            return self.execute(context)
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        info = compiler_bridge.inspect_existing_mod_props(target_mod)
        if info.get("exists") and info.get("count", 0) > 0:
            self._existing_mod_info = info
            self._compile_diff = compiler_bridge.compute_pack_compile_diff(context, target_mod, selected_objs=sel_objs)
            self.merge_existing = True
            return context.window_manager.invoke_props_dialog(self, width=440)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        info = getattr(self, "_existing_mod_info", {})
        diff = getattr(self, "_compile_diff", {})
        cnt = info.get("count", 0)
        box = layout.box()
        box.label(text=f"Pack already has {cnt} prop(s) in staging registry:", icon='INFO')
        if diff.get("adding"):
            box.label(text=f"• Adding: {', '.join(diff['adding'][:5])}", icon='ADD')
        if diff.get("updating"):
            box.label(text=f"• Updating: {', '.join(diff['updating'][:5])}", icon='FILE_REFRESH')
        if diff.get("unchanged"):
            box.label(text=f"• Unchanged: {', '.join(diff['unchanged'][:5])}", icon='CHECKMARK')
        if diff.get("keeping"):
            box.label(text=f"• Keeping: {', '.join(diff['keeping'][:5])}", icon='CHECKMARK')
        box.prop(self, "merge_existing")

    def execute(self, context):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not sel_objs:
            self.report({'WARNING'}, "ReSkate: Select at least one 3D mesh prop in the Viewport or Outliner.")
            return {'CANCELLED'}

        for o in sel_objs:
            try:
                o["_reskate_last_preflight_json"] = json.dumps(compiler_bridge.run_preflight_checks(context, o))
            except Exception:
                pass
            compiler_bridge.lock_stable_prop_key_on_object(o, context)

        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            if self.properties.is_property_set("merge_existing") or hasattr(self, "_existing_mod_info"):
                prop.merge_existing_mod = bool(self.merge_existing)
            else:
                prop.merge_existing_mod = True

        if bpy.app.background or not getattr(context, "window_manager", None):
            try:
                pack_manifest, pack_manifest_path = compiler_bridge.build_pack_manifest(context, selected_objs=sel_objs)
            except Exception as ex:
                self.report({'ERROR'}, f"ReSkate: {ex}")
                return {'CANCELLED'}

            compiler_bridge.invalidate_ui_caches()
            cnt = pack_manifest.get("props_count", len(sel_objs))
            target = pack_manifest.get("target_mod_name", "Prop Pack")
            return _start_async_or_sync_compile(self, context, pack_manifest_path, is_pack=True, disp_label=f"{target} ({cnt} selected prop(s))")

        # Stage 1: Non-blocking Two-Stage Modal Generator State Machine
        global _ACTIVE_COMPILE_CANCELLED
        _ACTIVE_COMPILE_CANCELLED = False
        self._stage = 'PREP'
        self._prep_generator = compiler_bridge.build_pack_manifest_generator(context, selected_objs=sel_objs)
        if prop:
            prop.is_compiling = True
            prop.compile_has_error = False
            prop.compile_step_label = f"Preparing export for {len(sel_objs)} prop(s)..."
            prop.compile_progress = 0.0
        try:
            context.window_manager.progress_begin(0, 100)
            context.window_manager.progress_update(0)
        except Exception:
            pass
        self._timer = context.window_manager.event_timer_add(0.02, window=context.window)
        if not getattr(self, "_is_modal_active", False):
            context.window_manager.modal_handler_add(self)
            self._is_modal_active = True
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        return _handle_compile_or_prep_modal(self, context, event)


class RESKATE_OT_queue_compile_all(bpy.types.Operator):
    """Compiles all marked props in the scene or pack queue into a single unified mod pack"""
    bl_idname = "reskate.queue_compile_all"
    bl_label = "Compile All Marked Props"
    bl_description = "Compiles all props marked with 'Include in Pack' (or in the pack queue) into a single mod pack"
    bl_options = {'REGISTER'}

    merge_existing: BoolProperty(
        name="Merge into Existing Pack (Keep Existing Props)",
        default=True,
        description="When enabled, merges these marked props with any props already inside the staging pack registry and recompiles",
    )

    def invoke(self, context, event):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        if bpy.app.background or not getattr(context, "window_manager", None):
            return self.execute(context)
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        info = compiler_bridge.inspect_existing_mod_props(target_mod)
        if info.get("exists") and info.get("count", 0) > 0:
            self._existing_mod_info = info
            self._compile_diff = compiler_bridge.compute_pack_compile_diff(context, target_mod, single_obj=None)
            self.merge_existing = True
            return context.window_manager.invoke_props_dialog(self, width=440)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        info = getattr(self, "_existing_mod_info", {})
        diff = getattr(self, "_compile_diff", {})
        cnt = info.get("count", 0)
        box = layout.box()
        box.label(text=f"Pack already has {cnt} prop(s) in staging registry:", icon='INFO')
        if diff.get("adding"):
            box.label(text=f"• Adding: {', '.join(diff['adding'][:5])}", icon='ADD')
        if diff.get("updating"):
            box.label(text=f"• Updating: {', '.join(diff['updating'][:5])}", icon='FILE_REFRESH')
        if diff.get("unchanged"):
            box.label(text=f"• Unchanged: {', '.join(diff['unchanged'][:5])}", icon='CHECKMARK')
        if diff.get("keeping"):
            box.label(text=f"• Keeping: {', '.join(diff['keeping'][:5])}", icon='CHECKMARK')
        box.prop(self, "merge_existing")

    def execute(self, context):
        block_reason = compiler_bridge.get_compile_block_reason(context)
        if block_reason:
            self.report({'ERROR'}, f"ReSkate: {block_reason}")
            return {'CANCELLED'}
        prop = getattr(context.scene, "reskate_prop", None)
        if prop and (self.properties.is_property_set("merge_existing") or hasattr(self, "_existing_mod_info")):
            prop.merge_existing_mod = bool(self.merge_existing)

        if bpy.app.background or not getattr(context, "window_manager", None):
            try:
                pack_manifest, pack_manifest_path = compiler_bridge.build_pack_manifest(context)
            except Exception as ex:
                self.report({'ERROR'}, f"ReSkate: {ex}")
                return {'CANCELLED'}

            compiler_bridge.invalidate_ui_caches()
            cnt = pack_manifest.get("props_count", 1)
            target = pack_manifest.get("target_mod_name", "Prop Pack")
            return _start_async_or_sync_compile(self, context, pack_manifest_path, is_pack=True, disp_label=f"{target} ({cnt} props)")

        # Stage 1: Non-blocking Two-Stage Modal Generator State Machine
        global _ACTIVE_COMPILE_CANCELLED
        _ACTIVE_COMPILE_CANCELLED = False
        self._stage = 'PREP'
        self._prep_generator = compiler_bridge.build_pack_manifest_generator(context)
        if prop:
            prop.is_compiling = True
            prop.compile_has_error = False
            prop.compile_step_label = "Preparing export (0%)..."
            prop.compile_progress = 0.0
        try:
            context.window_manager.progress_begin(0, 100)
            context.window_manager.progress_update(0)
        except Exception:
            pass
        self._timer = context.window_manager.event_timer_add(0.02, window=context.window)
        if not getattr(self, "_is_modal_active", False):
            context.window_manager.modal_handler_add(self)
            self._is_modal_active = True
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        return _handle_compile_or_prep_modal(self, context, event)


class RESKATE_OT_select_pack_or_scene_props(bpy.types.Operator):
    """Selects all prop mesh objects in the current Prop Pack (or a specific Prop Pack in the .blend scene)"""
    bl_idname = "reskate.select_pack_or_scene_props"
    bl_label = "Select All in Pack"
    bl_description = (
        "Selects all 3D mesh props in the current .blend scene that belong to the Prop Pack "
        "(automatically ignoring scale references, ground planes, and _COL/_PPHYS proxies)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Selection Scope",
        items=[
            ('PACK', "All Props in Active Pack", "Select all scene prop meshes that belong to the current Prop Pack"),
            ('ALL_SCENE', "All Props in Scene", "Select all valid prop meshes in the current .blend scene"),
        ],
        default='PACK',
    )
    target_pack: StringProperty(
        name="Target Pack",
        default="",
        options={'SKIP_SAVE'},
    )
    frame_view: BoolProperty(
        name="Frame in 3D Viewport",
        default=False,
    )

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        prop = getattr(context.scene, "reskate_prop", None)
        explicit_pk = str(self.target_pack or "").strip()
        resolved_mod = compiler_bridge.get_resolved_target_mod_name(
            context, explicit_pk if explicit_pk else prop
        )
        mode_up = str(self.mode or "PACK").upper()

        if mode_up == "PACK":
            targets = compiler_bridge.get_pack_prop_meshes_in_scene(context, resolved_mod)
            used_fallback = False
            if not targets and not explicit_pk:
                targets = compiler_bridge.get_all_scene_prop_meshes(context)
                used_fallback = True
        else:
            targets = compiler_bridge.get_all_scene_prop_meshes(context)
            used_fallback = False

        if not targets:
            self.report({'WARNING'}, "ReSkate: No valid prop mesh objects found for this pack in the current scene.")
            return {'CANCELLED'}

        prev_active = getattr(context.view_layer.objects, "active", None)
        bpy.ops.object.select_all(action='DESELECT')
        selected_objs = []
        for o in targets:
            try:
                o.hide_viewport = False
                o.hide_set(False)
            except Exception:
                pass
            try:
                o.select_set(True)
                selected_objs.append(o)
            except Exception:
                pass

        if not selected_objs:
            self.report({'WARNING'}, "ReSkate: Could not select prop objects (check if their collection is hidden).")
            return {'CANCELLED'}

        if prev_active in selected_objs:
            context.view_layer.objects.active = prev_active
        else:
            context.view_layer.objects.active = selected_objs[0]

        sync_active_object_props(context.scene)

        if self.frame_view:
            try:
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D':
                            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                            if region:
                                with context.temp_override(window=window, area=area, region=region):
                                    bpy.ops.view3d.view_selected()
                                break
            except Exception:
                pass

        if mode_up == "PACK" and not used_fallback:
            self.report({'INFO'}, f"ReSkate: Selected {len(selected_objs)} prop(s) belonging to pack '{resolved_mod}'.")
        else:
            self.report({'INFO'}, f"ReSkate: Selected {len(selected_objs)} prop(s) in the current scene.")
        return {'FINISHED'}


class RESKATE_OT_frame_selected_props(bpy.types.Operator):
    """Frames the 3D Viewport camera around the currently selected (or active) prop mesh(es)"""
    bl_idname = "reskate.frame_selected_props"
    bl_label = "Frame Selected"
    bl_description = "Centers and zooms the 3D Viewport on the currently selected (or active) prop mesh(es)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        act = get_target_mesh_object(context) or getattr(context, "active_object", None)
        if not sel_meshes and act is not None and getattr(act, "type", None) == 'MESH':
            try:
                act.select_set(True)
                sel_meshes = [act]
            except Exception:
                pass
        if not sel_meshes:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop in the viewport first.")
            return {'CANCELLED'}

        framed = False
        try:
            wm = getattr(context, "window_manager", None)
            if wm and getattr(wm, "windows", None):
                for window in wm.windows:
                    if not window.screen:
                        continue
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D':
                            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                            if region:
                                with context.temp_override(window=window, area=area, region=region):
                                    bpy.ops.view3d.view_selected()
                                    framed = True
                                break
                    if framed:
                        break
        except Exception:
            pass
        if not framed:
            try:
                if bpy.ops.view3d.view_selected.poll():
                    bpy.ops.view3d.view_selected()
                    framed = True
            except Exception:
                pass
        self.report({'INFO'}, f"ReSkate: Framed {len(sel_meshes)} selected prop(s) in viewport.")
        return {'FINISHED'}


class RESKATE_OT_switch_or_assign_prop_pack(bpy.types.Operator):
    """Switches the active Prop Pack Name to this pack and assigns the currently selected prop(s) to it"""
    bl_idname = "reskate.switch_or_assign_prop_pack"
    bl_label = "Use / Assign Prop Pack"
    bl_description = "Sets this Prop Pack as the active Prop Pack Name and assigns the currently selected prop(s) to it"
    bl_options = {'REGISTER', 'UNDO'}

    pack_name: StringProperty(name="Pack Name", default="", options={'SKIP_SAVE'})

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        pk = str(self.pack_name or "").strip()
        if not prop or not pk:
            return {'CANCELLED'}

        author = compiler_bridge.get_author_name(context, prop)
        clean_pk = pk
        if author and clean_pk.lower().startswith(f"{author.lower()}-"):
            clean_pk = clean_pk[len(author) + 1:].strip() or clean_pk
        resolved_pk = compiler_bridge.get_resolved_target_mod_name(context, clean_pk)

        prop.target_mod_name = clean_pk
        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        if not sel_meshes:
            act = get_target_mesh_object(context)
            if act is not None and getattr(act, "type", None) == 'MESH':
                sel_meshes = [act]

        for m_obj in sel_meshes:
            m_obj["reskate_target_mod_name"] = clean_pk
            raw_un = str(m_obj.get("reskate_unassigned_packs_csv", "") or "")
            if raw_un:
                kept = [
                    x.strip() for x in raw_un.split(",")
                    if x.strip() and x.strip().lower() not in (clean_pk.lower(), resolved_pk.lower(), pk.lower())
                ]
                m_obj["reskate_unassigned_packs_csv"] = ",".join(kept)
            st = get_or_init_object_prop_state(m_obj)
            st["target_mod_name"] = clean_pk
            save_object_prop_state(m_obj, st)

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Active Prop Pack set to '{clean_pk}' ({len(sel_meshes)} prop(s) assigned).")
        return {'FINISHED'}


class RESKATE_OT_unassign_prop_pack_from_scene(bpy.types.Operator):
    """Unassigns/removes a Prop Pack tag from all props in the current .blend scene without deleting any 3D meshes"""
    bl_idname = "reskate.unassign_prop_pack_from_scene"
    bl_label = "Remove Prop Pack Tag from Scene"
    bl_description = "Clears this Prop Pack assignment from props in the current .blend scene so you can clean up lingering packs without deleting any 3D meshes"
    bl_options = {'REGISTER', 'UNDO'}

    pack_name: StringProperty(name="Pack Name", default="", options={'SKIP_SAVE'})

    def execute(self, context):
        pk = str(self.pack_name or "").strip()
        if not pk:
            return {'CANCELLED'}
        prop = getattr(context.scene, "reskate_prop", None)
        author = compiler_bridge.get_author_name(context, prop)
        clean_pk = pk
        if author and clean_pk.lower().startswith(f"{author.lower()}-"):
            clean_pk = clean_pk[len(author) + 1:].strip() or clean_pk
        resolved_pk = compiler_bridge.get_resolved_target_mod_name(context, clean_pk)
        match_keys = {pk.lower(), clean_pk.lower(), resolved_pk.lower()}

        cleared_count = 0
        for m_obj in compiler_bridge.get_all_scene_prop_meshes(context):
            st = get_or_init_object_prop_state(m_obj)
            obj_tmod = str(m_obj.get("reskate_target_mod_name", "") or st.get("target_mod_name", "") or "").strip()
            obj_lmod = str(m_obj.get("reskate_last_compiled_mod", "") or "").strip()
            obj_res_t = compiler_bridge.get_resolved_target_mod_name(context, obj_tmod) if obj_tmod else ""
            obj_res_l = compiler_bridge.get_resolved_target_mod_name(context, obj_lmod) if obj_lmod else ""
            obj_packs = compiler_bridge.get_object_pack_memberships(context, m_obj)

            hit = (
                obj_tmod.lower() in match_keys
                or obj_lmod.lower() in match_keys
                or obj_res_t.lower() in match_keys
                or obj_res_l.lower() in match_keys
                or any(p.lower() in match_keys for p in obj_packs)
            )
            if hit:
                cleared_count += 1
                if obj_tmod.lower() in match_keys or obj_res_t.lower() in match_keys:
                    m_obj["reskate_target_mod_name"] = ""
                    st["target_mod_name"] = ""
                if obj_lmod.lower() in match_keys or obj_res_l.lower() in match_keys:
                    m_obj["reskate_last_compiled_mod"] = ""
                raw_un = str(m_obj.get("reskate_unassigned_packs_csv", "") or "")
                un_list = [x.strip() for x in raw_un.split(",") if x.strip()]
                for mk in (clean_pk, resolved_pk):
                    if mk and mk.lower() not in {u.lower() for u in un_list}:
                        un_list.append(mk)
                m_obj["reskate_unassigned_packs_csv"] = ",".join(un_list)
                save_object_prop_state(m_obj, st)

        if prop is not None:
            cur_t = str(getattr(prop, "target_mod_name", "") or "").strip()
            if cur_t.lower() in match_keys:
                remaining = compiler_bridge.get_scene_prop_packs_summary(context)
                other_pks = [r["pack_name"] for r in remaining if r["pack_name"].lower() not in match_keys and r["resolved_mod"].lower() not in match_keys]
                if other_pks:
                    prop.target_mod_name = other_pks[0]

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Unassigned pack '{clean_pk}' from {cleared_count} scene prop(s) (meshes kept intact).")
        return {'FINISHED'}


class RESKATE_OT_pack_prop_action(bpy.types.Operator):
    """Per-prop action inside the Pack Contents Manager (Select in Scene, or Remove prop from pack registry)"""
    bl_idname = "reskate.pack_prop_action"
    bl_label = "Manage Prop in Pack"
    bl_description = "Selects this prop in the 3D scene or removes it from the staging pack registry"
    bl_options = {'REGISTER', 'UNDO'}

    action: StringProperty(name="Action", default="REMOVE")
    prop_key: StringProperty(name="Prop Key", default="")
    display_name: StringProperty(name="Display Name", default="")

    def invoke(self, context, event):
        if self.action == "REMOVE" and not bpy.app.background and getattr(context, "window_manager", None):
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text=f"Remove '{self.display_name or self.prop_key}' from this pack?", icon='ERROR')
        box.label(text="Warning: Removing a prop breaks saved parks where it was already placed!")

    def execute(self, context):
        act_up = (self.action or "").upper()
        if act_up == "SELECT":
            if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')
            target_obj = None
            want_key = (self.prop_key or "").strip().lower()
            want_disp = (self.display_name or "").strip().lower()
            for o in context.scene.objects:
                if o.type != 'MESH' or o.name.endswith("_COL") or getattr(getattr(o, "reskate_col_prop", None), "is_col_proxy", False):
                    continue
                o_slug = compiler_bridge.get_prop_slug(context, o).lower()
                o_id = compiler_bridge.get_canonical_prop_id(context, o).lower()
                st = get_or_init_object_prop_state(o)
                o_disp = str(st.get("prop_display_name") or o.name).strip().lower()
                if (
                    (want_key and (o_slug == want_key or o_id == want_key or f"own_customprops_{o_id}" == want_key))
                    or (want_disp and (o_disp == want_disp or o.name.lower() == want_disp))
                ):
                    target_obj = o
                    break
            if not target_obj:
                self.report({'WARNING'}, f"ReSkate: Prop '{self.display_name or self.prop_key}' is in the pack registry, but no matching mesh object is in the current .blend scene.")
                return {'CANCELLED'}
            target_obj.hide_viewport = False
            try:
                target_obj.hide_set(False)
            except Exception:
                pass
            bpy.ops.object.select_all(action='DESELECT')
            target_obj.select_set(True)
            context.view_layer.objects.active = target_obj
            sync_active_object_props(context.scene)
            try:
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D':
                            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                            if region:
                                with context.temp_override(window=window, area=area, region=region):
                                    bpy.ops.view3d.view_selected()
                                break
            except Exception:
                pass
            self.report({'INFO'}, f"ReSkate: Selected & framed '{target_obj.name}' in scene.")
            return {'FINISHED'}

        prop = getattr(context.scene, "reskate_prop", None)
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        pack_store = compiler_bridge.get_pack_storage_dir(target_mod)
        reg_file = pack_store / "pack_registry.json"
        if not reg_file.exists():
            self.report({'WARNING'}, f"ReSkate: No staging pack registry found for '{target_mod}'.")
            return {'CANCELLED'}

        try:
            reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        except Exception as ex:
            self.report({'ERROR'}, f"ReSkate: Could not read pack registry: {ex}")
            return {'CANCELLED'}

        props_list = reg_data.get("props", [])
        if act_up == "REMOVE":
            kept = [p for p in props_list if p.get("prop_key") != self.prop_key and p.get("prop_id") != self.prop_key]
            if not kept:
                reg_data["props"] = []
                reg_data["props_count"] = 0
                reg_file.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")
                compiler_bridge.invalidate_ui_caches()
                self.report({'INFO'}, f"ReSkate: Removed '{self.display_name or self.prop_key}' (pack is now empty).")
                return {'FINISHED'}

            reg_data["props"] = kept
            reg_data["props_count"] = len(kept)
            reg_file.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")

            # Write a recompile pack manifest from the remaining registry entries
            rebuild_manifest = {
                "pack_name": target_mod,
                "target_mod_name": target_mod,
                "author_name": compiler_bridge.get_author_name(context),
                "pack_version": getattr(prop, "pack_version", "0.1.0") if prop else "0.1.0",
                "output_staging_dir": str(compiler_bridge.get_staging_dir(context)),
                "merge_existing_mod": False,
                "props": kept,
            }
            rebuild_path = pack_store / "rebuild_pack_manifest.json"
            rebuild_path.write_text(json.dumps(rebuild_manifest, indent=2), encoding="utf-8")
            compiler_bridge.invalidate_ui_caches()
            return _start_async_or_sync_compile(
                self, context, rebuild_path, is_pack=True, disp_label=f"{target_mod} ({len(kept)} props)"
            )

        return {'FINISHED'}

    def modal(self, context, event):
        return _handle_compile_modal(self, context, event)


class RESKATE_OT_restore_pack_backup(bpy.types.Operator):
    """Restores the most recent rolling backup of the pack from <Staging>/Packs/<pack>/backups/"""
    bl_idname = "reskate.restore_pack_backup"
    bl_label = "Restore Previous Build"
    bl_description = "Restores the previous pack build (registry, Mods/<pack>, and .fbmod) from the rolling backups folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        pack_store = compiler_bridge.get_pack_storage_dir(target_mod)
        backups_dir = pack_store / "backups"
        if not backups_dir.exists():
            self.report({'WARNING'}, f"ReSkate: No backups found for '{target_mod}'.")
            return {'CANCELLED'}

        folders = sorted([d for d in backups_dir.iterdir() if d.is_dir()])
        if not folders:
            self.report({'WARNING'}, f"ReSkate: No backups found for '{target_mod}'.")
            return {'CANCELLED'}

        latest = folders[-1]
        bak_reg = latest / "pack_registry.json"
        if bak_reg.exists():
            shutil.copy2(bak_reg, pack_store / "pack_registry.json")

        staging_dir = compiler_bridge.get_staging_dir(context)
        bak_fbmod = latest / f"{target_mod}.fbmod"
        if bak_fbmod.exists():
            shutil.copy2(bak_fbmod, staging_dir / f"{target_mod}.fbmod")

        bak_mod_dir = latest / target_mod
        if bak_mod_dir.exists() and bak_mod_dir.is_dir():
            dest_mod = compiler_bridge.get_game_mods_dir(context) / target_mod
            if dest_mod.exists():
                shutil.rmtree(dest_mod, ignore_errors=True)
            shutil.copytree(bak_mod_dir, dest_mod)

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Restored '{target_mod}' from backup '{latest.name}'.")
        return {'FINISHED'}


class RESKATE_OT_bump_pack_version(bpy.types.Operator):
    """Increments the Semantic Version (MAJOR.MINOR.PATCH) of the prop pack"""
    bl_idname = "reskate.bump_pack_version"
    bl_label = "Bump Version"
    bl_description = "Increments the semantic version (e.g. 0.1.0 -> 0.1.1)"
    bl_options = {'REGISTER', 'UNDO'}

    part: StringProperty(name="Part", default="PATCH")

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        cur = (getattr(prop, "pack_version", "0.1.0") or "0.1.0").strip()
        toks = [int(t) if t.isdigit() else 0 for t in (cur.split(".") + ["0", "0", "0"])[:3]]
        p_up = (self.part or "PATCH").upper()
        if p_up == "MAJOR":
            toks = [toks[0] + 1, 0, 0]
        elif p_up == "MINOR":
            toks = [toks[0], toks[1] + 1, 0]
        else:
            toks = [toks[0], toks[1], toks[2] + 1]
        prop.pack_version = f"{toks[0]}.{toks[1]}.{toks[2]}"
        self.report({'INFO'}, f"ReSkate: Pack version bumped to {prop.pack_version}.")
        return {'FINISHED'}


class RESKATE_OT_export_thunderstore_zip(bpy.types.Operator):
    """Packages the compiled mod folder contents (Win32/, layout.toc, initfs_win32, reskate-objects.json) along with root manifest.json, README.md, CHANGELOG.md, and 256×256 icon.png into a Thunderstore-ready .zip"""
    bl_idname = "reskate.export_thunderstore_zip"
    bl_label = "Export Release .zip (Thunderstore)"
    bl_description = "Packages the compiled mod folder contents and Thunderstore metadata (manifest.json, README.md, CHANGELOG.md, 256×256 icon.png) at the root of a Thunderstore-ready .zip"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        target_mod = compiler_bridge.get_resolved_target_mod_name(context)
        author_raw = compiler_bridge.get_author_name(context) or "Author"
        author = re.sub(r"[^a-zA-Z0-9_]+", "_", author_raw).strip("_") or "Author"
        version = (getattr(prop, "pack_version", "0.1.0") if prop else "0.1.0").strip() or "0.1.0"
        desc = (getattr(prop, "pack_description", "Standalone Quick Drop Prop Pack") if prop else "Standalone Quick Drop Prop Pack").strip()

        staging_dir = compiler_bridge.get_staging_dir(context)
        game_dir = compiler_bridge.get_game_dir(context)
        mods_dir = (game_dir / "Mods") if game_dir else None

        # Locate compiled mod directory in Mods/<target_mod> or Staging/<target_mod>
        compiled_mod_dir = None
        if mods_dir and (mods_dir / target_mod).is_dir() and ((mods_dir / target_mod) / "Win32").is_dir():
            compiled_mod_dir = mods_dir / target_mod
        elif (staging_dir / target_mod).is_dir() and ((staging_dir / target_mod) / "Win32").is_dir():
            compiled_mod_dir = staging_dir / target_mod
        elif mods_dir and (mods_dir / target_mod).is_dir():
            compiled_mod_dir = mods_dir / target_mod
        elif (staging_dir / target_mod).is_dir():
            compiled_mod_dir = staging_dir / target_mod

        if compiled_mod_dir is None or not compiled_mod_dir.exists():
            self.report({'WARNING'}, f"ReSkate: Compiled mod folder '{target_mod}' not found. Compile the pack first.")
            return {'CANCELLED'}

        info = compiler_bridge.inspect_existing_mod_props(target_mod, use_cache=False)
        prop_names = info.get("prop_names", [])

        raw_pack_part = target_mod
        if raw_pack_part.lower().startswith(f"{author_raw.lower()}-") or raw_pack_part.lower().startswith(f"{author_raw.lower()}_"):
            raw_pack_part = raw_pack_part[len(author_raw) + 1:]
        elif raw_pack_part.lower().startswith(f"{author.lower()}-") or raw_pack_part.lower().startswith(f"{author.lower()}_"):
            raw_pack_part = raw_pack_part[len(author) + 1:]

        # Thunderstore package names can ONLY contain a-z, A-Z, 0-9, and _
        safe_pack = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_pack_part).strip("_")[:128] or "Custom_Props"

        zip_name = f"{author}-{safe_pack}-{version}.zip"
        out_dir = mods_dir if (mods_dir and mods_dir.is_dir()) else staging_dir
        zip_path = out_dir / zip_name

        ts_manifest = {
            "name": safe_pack,
            "author": author,
            "version_number": version,
            "website_url": "",
            "description": (desc or "Custom skateable props built with ReSkate Prop Studio")[:250],
            "dependencies": [],
        }

        custom_readme_p = (getattr(prop, "mod_readme_path", "") or "").strip() if prop else ""
        readme_text = ""
        if custom_readme_p:
            try:
                p_rm = Path(bpy.path.abspath(custom_readme_p))
                if p_rm.exists() and p_rm.is_file():
                    readme_text = p_rm.read_text(encoding="utf-8", errors="replace")
            except Exception:
                readme_text = ""

        if not readme_text and (compiled_mod_dir / "README.md").exists():
            try:
                readme_text = (compiled_mod_dir / "README.md").read_text(encoding="utf-8", errors="replace")
            except Exception:
                readme_text = ""

        if not readme_text:
            props_for_readme = [{"display_name": pn, "category": "Custom Props"} for pn in prop_names]
            if not props_for_readme:
                sel_m = compiler_bridge.get_selected_prop_meshes(context)
                for m_o in sel_m:
                    props_for_readme.append({
                        "display_name": compiler_bridge.get_prop_display_name(m_o, context),
                        "category": str(m_o.get("reskate_category") or (prop.prop_category if prop else "Custom Props")),
                        "family_title": str(m_o.get("reskate_family_title") or ""),
                    })
            readme_text = compiler_bridge.generate_pack_readme_markdown({
                "pack_name": safe_pack,
                "author_name": author,
                "pack_version": version,
                "pack_description": desc,
                "props": props_for_readme,
            })

        readme_text = compiler_bridge.ensure_pack_readme_footer(readme_text)

        changelog_lines = [
            "# Changelog",
            "",
            f"## [{version}] - {time.strftime('%Y-%m-%d')}",
            f"- Built with ReSkate Prop Studio v{compiler_bridge.ADDON_VERSION}.",
        ]
        changelog_text = "\n".join(changelog_lines) + "\n"

        # Find a 256x256 icon PNG (prefer explicit mod_icon_path, then compiled_mod_dir/icon.png, then staging thumbnail)
        icon_bytes = None
        custom_mod_icon = (getattr(prop, "mod_icon_path", "") or "").strip() if prop else ""
        if custom_mod_icon:
            try:
                p_icon = Path(bpy.path.abspath(custom_mod_icon))
                if p_icon.exists() and p_icon.is_file():
                    import io as _io
                    from PIL import Image
                    im = Image.open(p_icon).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                    buf = _io.BytesIO()
                    im.save(buf, "PNG")
                    icon_bytes = buf.getvalue()
            except Exception:
                icon_bytes = None
        if icon_bytes is None and (compiled_mod_dir / "icon.png").exists():
            try:
                import io as _io
                from PIL import Image
                im = Image.open(compiled_mod_dir / "icon.png").convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                buf = _io.BytesIO()
                im.save(buf, "PNG")
                icon_bytes = buf.getvalue()
            except Exception:
                icon_bytes = (compiled_mod_dir / "icon.png").read_bytes()
        obj = get_target_mesh_object(context)
        if icon_bytes is None and obj:
            slug = get_prop_slug(context, obj)
            cand_icon = staging_dir / f"{slug}_thumb_256.png"
            if cand_icon.exists():
                icon_bytes = cand_icon.read_bytes()
        if icon_bytes is None:
            thumbs = sorted(staging_dir.glob("*_thumb_256.png"))
            if thumbs:
                icon_bytes = thumbs[-1].read_bytes()
        if icon_bytes is None:
            try:
                import io as _io
                from PIL import Image
                im = Image.new("RGBA", (256, 256), (36, 42, 54, 255))
                buf = _io.BytesIO()
                im.save(buf, "PNG")
                icon_bytes = buf.getvalue()
            except Exception:
                icon_bytes = b""

        manifest_json_str = json.dumps(ts_manifest, indent=2) + "\n"
        readme_final_str = readme_text if readme_text.endswith("\n") else (readme_text + "\n")

        # Also keep the compiled mod folder's root metadata files Thunderstore-valid in case the user zips the folder manually
        try:
            (compiled_mod_dir / "manifest.json").write_text(manifest_json_str, encoding="utf-8")
            (compiled_mod_dir / "README.md").write_text(readme_final_str, encoding="utf-8")
            (compiled_mod_dir / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
            if icon_bytes:
                (compiled_mod_dir / "icon.png").write_bytes(icon_bytes)
        except Exception:
            pass

        skip_root_names = {"manifest.json", "readme.md", "changelog.md", "icon.png"}
        skip_dirs = {".reskate_source", "__pycache__", ".git"}

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 1. Write required Thunderstore root files
            zf.writestr("manifest.json", manifest_json_str)
            zf.writestr("README.md", readme_final_str)
            zf.writestr("CHANGELOG.md", changelog_text)
            if icon_bytes:
                zf.writestr("icon.png", icon_bytes)

            # 2. Write all compiled mod files (Win32/, layout.toc, initfs_win32, reskate-objects.json, etc.) at root of .zip
            for item_p in sorted(compiled_mod_dir.rglob("*")):
                if not item_p.is_file():
                    continue
                rel_p = item_p.relative_to(compiled_mod_dir)
                if any(part in skip_dirs for part in rel_p.parts):
                    continue
                if len(rel_p.parts) == 1 and rel_p.name.lower() in skip_root_names:
                    continue
                if item_p.suffix.lower() in (".zip", ".fbmod", ".bak"):
                    continue
                arc_str = str(rel_p).replace("\\", "/")
                zf.write(item_p, arcname=arc_str)

        # Also copy to staging_dir if out_dir != staging_dir
        try:
            if out_dir != staging_dir:
                shutil.copy2(zip_path, staging_dir / zip_name)
        except Exception:
            pass

        # Reveal the exported .zip in Windows Explorer so the user can immediately drag it into Thunderstore
        try:
            subprocess.Popen(["explorer", f"/select,{str(zip_path)}"])
        except Exception:
            pass

        _maybe_auto_bump_pack_version(prop)
        self.report({'INFO'}, f"ReSkate: Exported Thunderstore package '{zip_path.name}' to {zip_path.parent}")
        return {'FINISHED'}


class RESKATE_OT_cancel_compile(bpy.types.Operator):
    """Cancels the running background prop compilation"""
    bl_idname = "reskate.cancel_compile"
    bl_label = "Cancel Compilation"
    bl_description = "Stops the currently running compiler process"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _ACTIVE_COMPILE_CANCELLED, _ACTIVE_COMPILE_PROC
        _ACTIVE_COMPILE_CANCELLED = True
        if _ACTIVE_COMPILE_PROC and _ACTIVE_COMPILE_PROC.poll() is None:
            _terminate_proc_tree(_ACTIVE_COMPILE_PROC)
        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            prop.is_compiling = False
            prop.compile_status_msg = "Compilation cancelled."
        return {'FINISHED'}


class RESKATE_OT_copy_compile_log(bpy.types.Operator):
    """Copies the full compiler output log to the system clipboard"""
    bl_idname = "reskate.copy_compile_log"
    bl_label = "Copy Full Log"
    bl_description = "Copies the complete compiler log output to the clipboard"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        log_text = getattr(prop, "compile_log_text", "") if prop else ""
        if not log_text:
            self.report({'INFO'}, "ReSkate: Compiler log is empty.")
            return {'FINISHED'}
        context.window_manager.clipboard = log_text
        self.report({'INFO'}, "ReSkate: Full compiler log copied to clipboard.")
        return {'FINISHED'}


class RESKATE_OT_queue_add_active(bpy.types.Operator):
    """Marks the active prop for inclusion in the multi-prop pack and adds/syncs it in the pack list"""
    bl_idname = "reskate.queue_add_active"
    bl_label = "Mark Active Prop for Pack"
    bl_description = "Adds or updates the active prop in the pack list so it compiles when clicking 'Compile All Marked Props'"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        scene = context.scene
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Please select a mesh object first.")
            return {'CANCELLED'}

        obj.reskate_include_in_pack = True
        prop = getattr(scene, "reskate_prop", None)

        # Check if this object is already in the pack queue; if so, update & sync in place
        existing_idx = next((i for i, it in enumerate(scene.reskate_pack_queue) if it.name == obj.name), -1)
        if existing_idx >= 0:
            item = scene.reskate_pack_queue[existing_idx]
            scene.reskate_pack_queue_index = existing_idx
            compiler_bridge.sync_queue_item_from_scene(context, item)
            self.report({'INFO'}, f"ReSkate: Synced '{item.display_name}' in pack.")
            return {'FINISHED'}

        base_slug = "".join(c if c.isalnum() or c == '_' else '_' for c in (prop.prop_key_slug if (prop and prop.prop_key_slug) else obj.name).lower()).strip('_')
        if not base_slug.startswith("own_"):
            base_slug = f"own_customprops_{base_slug}"

        existing_slugs = {getattr(item, "prop_key_slug", "") for item in scene.reskate_pack_queue}
        existing_slugs.update({getattr(item, "prop_key", "") for item in scene.reskate_pack_queue})
        final_slug = base_slug
        if final_slug in existing_slugs:
            idx = 2
            while f"{base_slug}_{idx:02d}" in existing_slugs:
                idx += 1
            final_slug = f"{base_slug}_{idx:02d}"

        item = scene.reskate_pack_queue.add()
        item.name = obj.name
        item.display_name = prop.prop_display_name if (prop and prop.prop_display_name) else obj.name
        item.prop_key = final_slug
        item.prop_key_slug = final_slug
        item.category = prop.prop_category if prop else "Custom Props"
        item.collision_mode = prop.collision_mode if prop else "SAME_AS_MESH"
        item.primary_surface = prop.primary_surface if prop else "concrete_smooth"
        item.surface_name = get_surface_label(prop.primary_surface if prop else "concrete_smooth")
        item.surface_code = prop.surface_material_code if prop else 96

        poly_count = len(obj.data.polygons)
        tri_count = sum(len(p.vertices) - 2 for p in obj.data.polygons) if poly_count > 0 else 0
        item.poly_count = poly_count
        item.tri_count = tri_count
        item.enabled = True
        item.is_enabled = True
        item.has_col_proxy = (compiler_bridge.resolve_custom_collision_object(context, obj) is not None)
        item.has_lights = (len(compiler_bridge.extract_prop_lights(obj)) > 0)
        item.snapshot_hash = compiler_bridge.compute_prop_scene_fingerprint(context, obj)

        scene.reskate_pack_queue_index = len(scene.reskate_pack_queue) - 1
        self.report({'INFO'}, f"ReSkate: Added '{item.display_name}' to pack.")
        return {'FINISHED'}


class RESKATE_OT_queue_sync_item(bpy.types.Operator):
    """Re-exports and syncs a modified pack queue item with the current 3D scene object"""
    bl_idname = "reskate.queue_sync_item"
    bl_label = "Sync Prop Changes"
    bl_description = "Updates this pack entry with the latest mesh, UVs, materials, collision, and lights from the scene"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        queue = context.scene.reskate_pack_queue
        target_idx = self.index if self.index >= 0 else context.scene.reskate_pack_queue_index
        if not (0 <= target_idx < len(queue)):
            self.report({'WARNING'}, "ReSkate: No valid pack item selected.")
            return {'CANCELLED'}
        item = queue[target_idx]
        ok = compiler_bridge.sync_queue_item_from_scene(context, item)
        if not ok:
            self.report({'WARNING'}, f"ReSkate: Scene object '{item.name}' not found.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"ReSkate: Synced '{item.display_name or item.name}' with scene changes.")
        return {'FINISHED'}


class RESKATE_OT_queue_sync_all(bpy.types.Operator):
    """Syncs all modified props in the pack queue with their current 3D scene state"""
    bl_idname = "reskate.queue_sync_all"
    bl_label = "Sync All Modified Props"
    bl_description = "Re-exports and updates all out-of-sync props in the pack list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        queue = context.scene.reskate_pack_queue
        synced = 0
        for item in queue:
            if compiler_bridge.is_queue_item_modified(context, item):
                if compiler_bridge.sync_queue_item_from_scene(context, item):
                    synced += 1
        self.report({'INFO'}, f"ReSkate: Synced {synced} modified prop(s) in pack.")
        return {'FINISHED'}


class RESKATE_OT_queue_remove_item(bpy.types.Operator):
    """Removes a specific item from the multi-prop pack list"""
    bl_idname = "reskate.queue_remove_item"
    bl_label = "Remove Prop from Pack"
    bl_description = "Removes this prop entry from the pack list"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        queue = context.scene.reskate_pack_queue
        target_idx = self.index if self.index >= 0 else context.scene.reskate_pack_queue_index
        if 0 <= target_idx < len(queue):
            obj_name = queue[target_idx].name
            obj = context.scene.objects.get(obj_name)
            if obj and hasattr(obj, "reskate_include_in_pack"):
                obj.reskate_include_in_pack = False
            queue.remove(target_idx)
            if context.scene.reskate_pack_queue_index >= len(queue):
                context.scene.reskate_pack_queue_index = max(0, len(queue) - 1)
        return {'FINISHED'}


class RESKATE_OT_queue_remove_active(bpy.types.Operator):
    """Removes the highlighted item from the multi-prop pack list"""
    bl_idname = "reskate.queue_remove_active"
    bl_label = "Remove Selected Prop"
    bl_description = "Removes the highlighted prop from the pack list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.reskate.queue_remove_item(index=context.scene.reskate_pack_queue_index)


class RESKATE_OT_queue_move_up(bpy.types.Operator):
    """Moves the selected queue item up in order"""
    bl_idname = "reskate.queue_move_up"
    bl_label = "Move Prop Up"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        queue = context.scene.reskate_pack_queue
        idx = context.scene.reskate_pack_queue_index
        if 0 < idx < len(queue):
            queue.move(idx, idx - 1)
            context.scene.reskate_pack_queue_index = idx - 1
        return {'FINISHED'}


class RESKATE_OT_queue_move_down(bpy.types.Operator):
    """Moves the selected queue item down in order"""
    bl_idname = "reskate.queue_move_down"
    bl_label = "Move Prop Down"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        queue = context.scene.reskate_pack_queue
        idx = context.scene.reskate_pack_queue_index
        if 0 <= idx < len(queue) - 1:
            queue.move(idx, idx + 1)
            context.scene.reskate_pack_queue_index = idx + 1
        return {'FINISHED'}


class RESKATE_OT_queue_clear(bpy.types.Operator):
    """Clears all props from the pack list"""
    bl_idname = "reskate.queue_clear"
    bl_label = "Clear Pack List"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for o in context.scene.objects:
            if hasattr(o, "reskate_include_in_pack"):
                o.reskate_include_in_pack = False
        context.scene.reskate_pack_queue.clear()
        context.scene.reskate_pack_queue_index = 0
        return {'FINISHED'}


class RESKATE_OT_open_mod_folder(bpy.types.Operator):
    """Opens deployed mod directory, Game Mods folder, or isolated staging folder in Windows Explorer"""
    bl_idname = "reskate.open_mod_folder"
    bl_label = "Open Mod / Output Folder"
    bl_description = "Opens the deployed mod directory (<GameDir>/Mods/<ModName>), <GameDir>/Mods, or staging folder in File Explorer"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        target_name = compiler_bridge.get_resolved_target_mod_name(context)
        export_only = getattr(prop, "export_fbmod_only", False) if prop else False

        mods_root = compiler_bridge.get_game_mods_dir(context)
        game_mods = mods_root / target_name
        staging_dir = compiler_bridge.get_staging_dir(context)

        if not export_only and game_mods.exists():
            target_folder = game_mods
        elif not export_only and mods_root.exists():
            target_folder = mods_root
        else:
            target_folder = staging_dir
        target_folder.mkdir(parents=True, exist_ok=True)

        try:
            if sys.platform == 'win32':
                os.startfile(str(target_folder))
            else:
                subprocess.Popen(['xdg-open', str(target_folder)])
            self.report({'INFO'}, f"ReSkate: Opened folder: {target_folder.name}")
        except Exception as ex:
            self.report({'WARNING'}, f"ReSkate: Could not open folder: {ex}")
        return {'FINISHED'}


class RESKATE_OT_open_mod_output_folder(bpy.types.Operator):
    """Opens the deployed mod folder (<GameDir>/Mods/<ModName>) or staging output folder in Windows Explorer"""
    bl_idname = "reskate.open_mod_output_folder"
    bl_label = "Open Mod / Output Folder"
    bl_description = "Opens the deployed mod folder (<GameDir>/Mods/<ModName>) or staging output folder in Windows Explorer"
    bl_options = {'REGISTER'}

    def execute(self, context):
        return bpy.ops.reskate.open_mod_folder()


class RESKATE_OT_inspect_manifest(bpy.types.Operator):
    """Loads and displays generated prop_manifest.json or pack_manifest.json in Blender Text Editor"""
    bl_idname = "reskate.inspect_manifest"
    bl_label = "Inspect Manifest"
    bl_description = "Loads the generated JSON manifest into Blender's Text Editor"
    bl_options = {'REGISTER'}

    def execute(self, context):
        staging_dir = compiler_bridge.get_staging_dir(context)
        candidates = [
            staging_dir / "prop_manifest.json",
            staging_dir / "pack_manifest.json",
            staging_dir / "Pack_Queue" / "pack_manifest.json",
        ]
        existing = [c for c in candidates if c.exists() and c.is_file()]
        if not existing:
            self.report({'WARNING'}, "ReSkate: No manifest found. Compile a prop first.")
            return {'CANCELLED'}

        manifest_path = max(existing, key=lambda p: p.stat().st_mtime)
        try:
            content = manifest_path.read_text(encoding="utf-8")
            text_block = bpy.data.texts.get(manifest_path.name) or bpy.data.texts.new(manifest_path.name)
            text_block.clear()
            text_block.write(content)
            self.report({'INFO'}, f"ReSkate: Loaded '{manifest_path.name}' into Text Editor.")
            return {'FINISHED'}
        except Exception as ex:
            self.report({'ERROR'}, f"ReSkate: Failed to read manifest: {ex}")
            return {'CANCELLED'}


class RESKATE_OT_reset_default_paths(bpy.types.Operator):
    """Resets Staging Folder (or auto-detects default paths) in Add-on Preferences"""
    bl_idname = "reskate.reset_default_paths"
    bl_label = "Reset"
    bl_description = "Resets the Staging Folder to the default cache directory"
    bl_options = {'REGISTER'}

    staging_only: BoolProperty(
        name="Staging Folder Only",
        default=True,
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        defaults = auto_detect_default_paths()
        prefs = get_addon_preferences(context)
        if prefs is not None:
            if self.staging_only:
                prefs.staging_dir = defaults.get("staging_dir", "")
            else:
                prefs.game_dir = defaults.get("game_dir", "")
                prefs.staging_dir = defaults.get("staging_dir", "")
                prefs.studio_dir = defaults.get("studio_dir", "")
        save_persisted_user_paths(
            getattr(prefs, "game_dir", "") if prefs else defaults.get("game_dir", ""),
            getattr(prefs, "studio_dir", "") if prefs else defaults.get("studio_dir", ""),
            getattr(prefs, "staging_dir", "") if prefs else defaults.get("staging_dir", ""),
            getattr(prefs, "author_name", "") if prefs else defaults.get("author_name", ""),
        )
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, "ReSkate: Staging folder reset to default.")
        return {'FINISHED'}


class RESKATE_OT_copy_settings_to_selected(bpy.types.Operator):
    """Copies Surface assignments, Collision settings, or Finish settings from the active prop to all other selected props"""
    bl_idname = "reskate.copy_settings_to_selected"
    bl_label = "Copy Surfaces to Selected"
    bl_description = "Copies Surface assignments from the active prop to all other selected prop meshes"
    bl_options = {'REGISTER', 'UNDO'}

    copy_mode: EnumProperty(
        name="Copy Mode",
        items=[
            ('SURFACES', "Copy Surfaces to Selected", "Copy surface assignments from the active prop to all other selected props"),
            ('COLLISION', "Copy Collision", "Copy Collision Mode & Physics Surface settings to selected props"),
            ('FINISH', "Copy Finish", "Copy Roughness, Metallic & Look settings to selected props"),
            ('BOTH', "Copy Both", "Copy both Collision and Finish settings to selected props"),
        ],
        default='SURFACES',
    )

    def execute(self, context):
        src_obj = get_target_mesh_object(context) or context.active_object
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        targets = [o for o in sel_objs if o != src_obj]
        if not src_obj or not targets:
            self.report({'WARNING'}, "ReSkate: Select at least 2 prop meshes (Shift-click) so settings can be copied from the active prop.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            save_prop_settings_to_object(src_obj, prop)
        src_state = get_or_init_object_prop_state(src_obj)

        col_keys = (
            "collision_mode",
            "primary_surface",
            "surface_material_code",
            "use_full_surface_catalog",
            "full_surface_catalog",
            "prevent_wall_snap_default",
            "max_col_tris",
            "coacd_max_hulls",
            "trim_bottom",
            "round_rail",
            "grind_type",
            "grind_surface_override",
        )
        fin_keys = (
            "look_preset",
            "dielectric_roughness",
            "dielectric_metallic",
            "dielectric_specular",
            "roughness_map_mix",
            "metallic_map_mix",
            "albedo_tint",
            "look_mode",
            "enable_custom_textures",
        )

        mode = (self.copy_mode or "SURFACES").upper()
        do_col = mode in ("SURFACES", "COLLISION", "BOTH")
        do_fin = mode in ("FINISH", "BOTH")

        src_surfs = list(getattr(src_obj, "reskate_material_surfaces", []))
        src_by_mat_name = {
            str(getattr(it, "material_name", "")).strip(): it
            for it in src_surfs
            if str(getattr(it, "material_name", "")).strip()
        }

        for dst_obj in targets:
            dst_state = dict(get_or_init_object_prop_state(dst_obj))
            if do_col:
                keys_to_copy = ("primary_surface", "surface_material_code", "use_full_surface_catalog", "full_surface_catalog") if mode == "SURFACES" else col_keys
                for k in keys_to_copy:
                    if k in src_state:
                        dst_state[k] = src_state[k]
                # Sync per-material surface slots on dst_obj
                if hasattr(dst_obj, "reskate_material_surfaces"):
                    dst_coll = dst_obj.reskate_material_surfaces
                    dst_coll.clear()
                    slot_count = max(1, len(dst_obj.material_slots))
                    for idx in range(slot_count):
                        slot = dst_obj.material_slots[idx] if idx < len(dst_obj.material_slots) else None
                        mat_nm = slot.material.name if (slot and slot.material) else f"Slot {idx + 1}"
                        it = dst_coll.add()
                        it.material_name = mat_nm
                        matched = src_by_mat_name.get(mat_nm)
                        if matched is not None:
                            it.surface = matched.surface
                            it.prevent_wall_snap = matched.prevent_wall_snap
                        elif idx < len(src_surfs):
                            it.surface = src_surfs[idx].surface
                            it.prevent_wall_snap = src_surfs[idx].prevent_wall_snap
                        else:
                            it.surface = str(src_state.get("primary_surface", "concrete_smooth"))
                            it.prevent_wall_snap = bool(src_state.get("prevent_wall_snap_default", False))

            if do_fin:
                for k in fin_keys:
                    if k in src_state:
                        dst_state[k] = src_state[k]
                compiler_bridge.ensure_all_object_materials_unique(dst_obj)
                r_val = float(src_state.get("dielectric_roughness", 0.45))
                m_val = float(src_state.get("dielectric_metallic", 0.0))
                r_mix = float(src_state.get("roughness_map_mix", 1.0))
                m_mix = float(src_state.get("metallic_map_mix", 1.0))
                for slot in dst_obj.material_slots:
                    mat = slot.material
                    if not mat or not mat.use_nodes or not mat.node_tree:
                        continue
                    p_node = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                    if not p_node:
                        continue
                    r_s = p_node.inputs.get("Roughness")
                    if r_s is not None:
                        if not r_s.is_linked:
                            r_s.default_value = r_val
                        elif r_s.links and r_s.links[0].from_node.name == "ReSkate_Roughness_Mix":
                            mr = r_s.links[0].from_node
                            mr["reskate_base_val"] = r_val
                            mr["reskate_mix_val"] = r_mix
                            mr.inputs["To Min"].default_value = (1.0 - r_mix) * r_val
                            mr.inputs["To Max"].default_value = (1.0 - r_mix) * r_val + r_mix
                    m_s = p_node.inputs.get("Metallic")
                    if m_s is not None:
                        if not m_s.is_linked:
                            m_s.default_value = m_val
                        elif m_s.links and m_s.links[0].from_node.name == "ReSkate_Metallic_Mix":
                            mm = m_s.links[0].from_node
                            mm["reskate_base_val"] = m_val
                            mm["reskate_mix_val"] = m_mix
                            mm.inputs["To Min"].default_value = (1.0 - m_mix) * m_val
                            mm.inputs["To Max"].default_value = (1.0 - m_mix) * m_val + m_mix

            ser_dst = json.dumps(dst_state)
            dst_obj["reskate_prop_state"] = ser_dst
            dst_obj["reskate_prop_state_json"] = ser_dst

        compiler_bridge.invalidate_ui_caches()
        label = "Collision & Finish" if mode == "BOTH" else ("Collision" if mode == "COLLISION" else "Finish")
        self.report({'INFO'}, f"ReSkate: Copied {label} settings from '{src_obj.name}' to {len(targets)} selected prop(s).")
        return {'FINISHED'}


class RESKATE_OT_make_faces_glow_emitter(bpy.types.Operator):
    """Assigns selected faces to a Noble-style glowing emissive material slot and optionally spawns a linked dynamic light at each glowing face cluster"""
    bl_idname = "reskate.make_faces_glow_emitter"
    bl_label = "Make Selected Faces Glow (Mesh Emitter + Light)"
    bl_description = "Assigns selected faces (in Edit Mode or Object Mode) to a glowing emissive material slot on this prop and optionally spawns a linked dynamic light at each glowing face cluster"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        was_edit = (context.mode == 'EDIT_MESH')
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or len(obj.data.polygons) == 0:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh object first.")
            return {'CANCELLED'}

        mesh = obj.data
        selected_polys = [p for p in mesh.polygons if p.select]
        if not selected_polys:
            if len(mesh.polygons) <= 64:
                selected_polys = list(mesh.polygons)
            else:
                # Enter Edit Mode so the user can highlight the glowing faces immediately
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                if bpy.ops.object.mode_set.poll():
                    bpy.ops.object.mode_set(mode='EDIT')
                self.report({'WARNING'}, "ReSkate: Highlight the faces that should glow in Edit Mode, then click 'Make Selected Faces Glow' again.")
                return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        glow_col = tuple(getattr(prop, "glow_color", (1.0, 0.88, 0.65))) if prop else (1.0, 0.88, 0.65)
        glow_str = float(getattr(prop, "glow_intensity", 2.5)) if prop else 2.5
        spawn_light = bool(getattr(prop, "glow_spawn_light", True)) if prop else True

        # Ensure the mesh has at least a base material slot if some faces are non-glowing
        if len(mesh.materials) == 0 and len(selected_polys) < len(mesh.polygons):
            base_mat_name = f"{obj.name}_Base_Mat"
            base_mat = bpy.data.materials.get(base_mat_name) or bpy.data.materials.new(base_mat_name)
            base_mat.use_nodes = True
            mesh.materials.append(base_mat)

        glow_mat_name = f"{obj.name}_Emissive_Glow"
        glow_mat = bpy.data.materials.get(glow_mat_name) or bpy.data.materials.new(glow_mat_name)
        glow_mat.use_nodes = True
        nodes = glow_mat.node_tree.nodes
        p_node = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not p_node:
            p_node = nodes.new("ShaderNodeBsdfPrincipled")
            out_n = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None) or nodes.new("ShaderNodeOutputMaterial")
            glow_mat.node_tree.links.new(p_node.outputs["BSDF"], out_n.inputs["Surface"])

        rgba = (float(glow_col[0]), float(glow_col[1]), float(glow_col[2]), 1.0)
        if "Base Color" in p_node.inputs:
            p_node.inputs["Base Color"].default_value = rgba
        if "Roughness" in p_node.inputs:
            p_node.inputs["Roughness"].default_value = 0.08
        if "Metallic" in p_node.inputs:
            p_node.inputs["Metallic"].default_value = 0.0
        em_sock = p_node.inputs.get("Emission Color") or p_node.inputs.get("Emission")
        if em_sock is not None:
            em_sock.default_value = rgba
        if "Emission Strength" in p_node.inputs:
            p_node.inputs["Emission Strength"].default_value = glow_str

        for holder in (getattr(glow_mat, "sk8_material", None), getattr(glow_mat, "reskate_mat", None)):
            if holder is not None:
                try:
                    if hasattr(holder, "emission_color"):
                        holder.emission_color = (float(glow_col[0]), float(glow_col[1]), float(glow_col[2]))
                    if hasattr(holder, "emission_strength"):
                        holder.emission_strength = glow_str
                except Exception:
                    pass

        glow_slot_idx = -1
        for i, m in enumerate(mesh.materials):
            if m == glow_mat:
                glow_slot_idx = i
                break
        if glow_slot_idx < 0:
            mesh.materials.append(glow_mat)
            glow_slot_idx = len(mesh.materials) - 1

        for p in selected_polys:
            p.material_index = glow_slot_idx
        mesh.update()

        spawned_count = 0
        if spawn_light:
            # Cluster the selected glowing faces and spawn up to 4 linked dynamic Spot lights
            mw = obj.matrix_world
            rot_scale = mw.to_3x3()
            try:
                normal_mat = rot_scale.inverted().transposed()
            except Exception:
                normal_mat = rot_scale

            poly_by_idx = {p.index: p for p in selected_polys}
            vert_to_polys: dict[int, list[int]] = {}
            for p in selected_polys:
                for vi in p.vertices:
                    vert_to_polys.setdefault(vi, []).append(p.index)

            visited: set[int] = set()
            clusters: list[list[int]] = []
            for p in selected_polys:
                if p.index in visited:
                    continue
                comp = []
                stack = [p.index]
                visited.add(p.index)
                while stack:
                    cur = stack.pop()
                    comp.append(cur)
                    for vi in poly_by_idx[cur].vertices:
                        for nbr in vert_to_polys.get(vi, []):
                            if nbr not in visited:
                                visited.add(nbr)
                                stack.append(nbr)
                clusters.append(comp)

            cluster_infos = []
            for comp in clusters:
                total_area = 0.0
                center_sum = Vector((0.0, 0.0, 0.0))
                norm_sum = Vector((0.0, 0.0, 0.0))
                for pid in comp:
                    poly = poly_by_idx[pid]
                    w_area = max(1e-5, float(poly.area))
                    w_center = mw @ poly.center
                    w_norm = normal_mat @ poly.normal
                    if w_norm.length_squared > 1e-10:
                        w_norm.normalize()
                    else:
                        w_norm = Vector((0.0, 0.0, -1.0))
                    center_sum += w_center * w_area
                    norm_sum += w_norm * w_area
                    total_area += w_area
                c_world = center_sum / total_area
                n_world = norm_sum.normalized() if norm_sum.length_squared > 1e-8 else Vector((0.0, 0.0, -1.0))
                cluster_infos.append({"center": c_world, "normal": n_world, "area": total_area})

            cluster_infos.sort(key=lambda x: x["area"], reverse=True)
            scene = context.scene
            for i, ci in enumerate(cluster_infos[:4]):
                base_name = f"{obj.name}_GlowLight_{i + 1:02d}"
                light_name = base_name
                sfx = 1
                while light_name in scene.objects:
                    light_name = f"{base_name}_{sfx}"
                    sfx += 1

                light_data = bpy.data.lights.new(name=light_name, type='SPOT')
                light_data.energy = min(250.0, max(25.0, 35.0 * glow_str))
                light_data.color = (float(glow_col[0]), float(glow_col[1]), float(glow_col[2]))
                light_data.use_custom_distance = True
                light_data.cutoff_distance = 25.0
                light_data.use_shadow = False
                light_data.spot_size = math.radians(105.0)
                light_data.spot_blend = 0.45

                light_obj = bpy.data.objects.new(name=light_name, object_data=light_data)
                light_obj["reskate_shadow_initialized"] = 1
                scene.collection.objects.link(light_obj)

                n_dir = ci["normal"]
                light_obj.location = ci["center"] + n_dir * 0.04
                up_axis = 'Y' if abs(n_dir.y) < 0.9 else 'X'
                light_obj.rotation_euler = n_dir.to_track_quat('-Z', up_axis).to_euler()

                light_obj.parent = obj
                try:
                    light_obj.matrix_parent_inverse = obj.matrix_world.inverted()
                except Exception:
                    light_obj.matrix_parent_inverse = Matrix.Identity(4)

                if hasattr(obj, "reskate_linked_lights"):
                    slot = obj.reskate_linked_lights.add()
                    slot.light_obj = light_obj
                    slot.enabled = True
                    obj.reskate_linked_light_index = len(obj.reskate_linked_lights) - 1
                spawned_count += 1

        if was_edit and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        light_msg = f" + {spawned_count} linked dynamic light(s)" if spawned_count else ""
        self.report({'INFO'}, f"ReSkate: Assigned {len(selected_polys)} face(s) to '{glow_mat.name}'{light_msg}.")
        return {'FINISHED'}


class RESKATE_OT_duplicate_prop_variant(bpy.types.Operator):
    """Duplicates the active prop as a new variant with an auto-incremented Prop Name and unique Prop ID"""
    bl_idname = "reskate.duplicate_prop_variant"
    bl_label = "Duplicate Prop as New Variant"
    bl_description = "Duplicates the active mesh (and its collision mesh if present) as an independent prop variant with a new unique Prop ID"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        src_obj = get_target_mesh_object(context) or context.active_object
        if not src_obj or src_obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D prop mesh to duplicate.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        if prop:
            save_prop_settings_to_object(src_obj, prop)
        src_state = dict(get_or_init_object_prop_state(src_obj))

        # Determine incremented base name and unique prop_id
        import re
        base_disp = str(src_state.get("prop_display_name") or src_obj.name).strip()
        m_num = re.search(r"^(.*?)[\s_]+(\d+)$", base_disp)
        stem_disp = m_num.group(1).strip() if m_num else base_disp
        idx_num = (int(m_num.group(2)) + 1) if m_num else 2

        existing_ids = set()
        for o in bpy.data.objects:
            if o.type == 'MESH':
                existing_ids.add(compiler_bridge.get_canonical_prop_id(context, o).lower())

        while True:
            new_disp = f"{stem_disp} {idx_num:02d}"
            cand_id = "".join(c if c.isalnum() or c == '_' else '_' for c in new_disp.lower()).strip('_')
            if cand_id not in existing_ids and f"{stem_disp.replace(' ', '_')}_{idx_num:02d}" not in bpy.data.objects:
                break
            idx_num += 1

        new_obj_name = f"{stem_disp.replace(' ', '_')}_{idx_num:02d}"
        new_mesh = src_obj.data.copy()
        new_mesh.name = f"{new_obj_name}_Mesh"
        new_obj = src_obj.copy()
        new_obj.name = new_obj_name
        new_obj.data = new_mesh
        context.collection.objects.link(new_obj)
        compiler_bridge.ensure_all_object_materials_unique(new_obj)

        # Offset slightly in X so it sits right next to the original prop
        bbox_w = [src_obj.matrix_world @ Vector(pt) for pt in src_obj.bound_box]
        span_x = (max(p.x for p in bbox_w) - min(p.x for p in bbox_w)) if bbox_w else 1.5
        new_obj.location = src_obj.location + Vector((max(0.8, span_x + 0.35), 0.0, 0.0))

        # If source has a custom _COL mesh, duplicate and parent it to new_obj
        src_col = compiler_bridge.resolve_custom_collision_object(context, src_obj)
        new_col = None
        if src_col and src_col.type == 'MESH':
            new_col_mesh = src_col.data.copy()
            new_col_mesh.name = f"{new_obj.name}_COL_Mesh"
            new_col = src_col.copy()
            new_col.name = f"{new_obj.name}_COL"
            new_col.data = new_col_mesh
            context.collection.objects.link(new_col)
            new_col.parent = new_obj
            try:
                new_col.matrix_parent_inverse = new_obj.matrix_world.inverted()
            except Exception:
                new_col.matrix_parent_inverse = Matrix.Identity(4)
            new_col.matrix_world = Matrix.Translation(new_obj.location - src_obj.location) @ src_col.matrix_world
            new_col.hide_select = False
            new_col.hide_viewport = False
            if hasattr(new_obj, "reskate_custom_collision_object"):
                new_obj.reskate_custom_collision_object = new_col

        new_state = dict(src_state)
        new_state["prop_display_name"] = new_disp
        new_state["prop_id"] = cand_id
        new_state["prop_key_slug"] = f"own_customprops_{cand_id}"
        new_state["custom_collision_object_name"] = new_col.name if new_col else ""
        ser_new = json.dumps(new_state)
        new_obj["reskate_prop_state"] = ser_new
        new_obj["reskate_prop_state_json"] = ser_new
        new_obj["reskate_prop_id"] = cand_id
        new_obj["reskate_key_slug"] = f"own_customprops_{cand_id}"

        bpy.ops.object.select_all(action='DESELECT')
        new_obj.select_set(True)
        context.view_layer.objects.active = new_obj
        if prop:
            load_prop_settings_from_object(new_obj, prop)

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report({'INFO'}, f"ReSkate: Created variant '{new_disp}' (ID: {cand_id}).")
        return {'FINISHED'}


class RESKATE_OT_auto_unwrap_uvs(bpy.types.Operator):
    """Generates a clean Smart UV Project layout on the active mesh in 1 click"""
    bl_idname = "reskate.auto_unwrap_uvs"
    bl_label = "Auto-Unwrap UV Islands (Smart UV)"
    bl_description = "Creates or unwraps the mesh's UV map using Smart UV Project (66° angle limit, 0.02 island margin) so textures and roughness maps apply cleanly without stretching"
    bl_options = {'REGISTER', 'UNDO'}

    angle_limit_deg: FloatProperty(name="Angle Limit (°)", default=66.0, min=15.0, max=89.0)
    island_margin: FloatProperty(name="Island Margin", default=0.02, min=0.0, max=0.2)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or len(obj.data.polygons) == 0:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh object first.")
            return {'CANCELLED'}

        was_edit = (context.mode == 'EDIT_MESH')
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        if len(mesh.uv_layers) == 0:
            mesh.uv_layers.new(name="UVMap")

        prev_active = context.view_layer.objects.active
        prev_sel = [o for o in context.selected_objects]
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(
                angle_limit=math.radians(float(self.angle_limit_deg)),
                island_margin=float(self.island_margin),
            )
            if not was_edit:
                bpy.ops.object.mode_set(mode='OBJECT')
        finally:
            if not was_edit:
                bpy.ops.object.select_all(action='DESELECT')
                for o in prev_sel:
                    if o and o.name in bpy.data.objects:
                        o.select_set(True)
                if prev_active and prev_active.name in bpy.data.objects:
                    context.view_layer.objects.active = prev_active

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Auto-unwrapped UV islands on '{obj.name}'.")
        return {'FINISHED'}


class RESKATE_OT_remove_exp_feature(bpy.types.Operator):
    """Removes a single active ReSkate Lab experimental feature from the active prop"""
    bl_idname = "reskate.remove_exp_feature"
    bl_label = "Remove Experimental Feature"
    bl_description = "Disables and removes this ReSkate Lab experimental feature from the active prop"
    bl_options = {'REGISTER', 'UNDO'}

    feature_key: StringProperty(name="Feature Key", default="")

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj:
            return {'CANCELLED'}
        raw = obj.get("reskate_exp_json", "")
        if not raw:
            return {'CANCELLED'}
        try:
            exp = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            exp = {}
        if not isinstance(exp, dict):
            exp = {}

        key = (self.feature_key or "").strip()
        if key == "no_skater_collision":
            exp.pop("no_skater_collision", None)
        elif key == "is_surface_decal":
            exp.pop("is_surface_decal", None)
            exp.pop("decal_z_lift_m", None)
        elif key == "music_emitter":
            if isinstance(exp.get("music_emitter"), dict):
                exp["music_emitter"]["enabled"] = False
            else:
                exp.pop("music_emitter", None)
            for child in list(obj.children):
                if child.type == 'EMPTY' and ("_MusicBoombox_Gizmo" in child.name or child.get("reskate_exp_helper_type") == "MUSIC"):
                    try:
                        bpy.data.objects.remove(child, do_unlink=True)
                    except Exception:
                        pass
        elif key == "qd_variant":
            if isinstance(exp.get("qd_variant"), dict):
                exp["qd_variant"]["enabled"] = False
            else:
                exp.pop("qd_variant", None)
        elif key.startswith("vfx:"):
            target_vfx = key.split(":", 1)[1]
            vfx_list = exp.get("vfx_emitters", [])
            if isinstance(vfx_list, list):
                kept = []
                for idx, v in enumerate(vfx_list):
                    vid = str(v.get("id") or v.get("object_name") or v.get("helper_object") or idx) if isinstance(v, dict) else str(idx)
                    if vid == target_vfx or (isinstance(v, dict) and v.get("preset") == target_vfx):
                        helper_name = (v.get("object_name") or v.get("helper_object")) if isinstance(v, dict) else None
                        if helper_name and helper_name in bpy.data.objects:
                            try:
                                bpy.data.objects.remove(bpy.data.objects[helper_name], do_unlink=True)
                            except Exception:
                                pass
                    else:
                        kept.append(v)
                exp["vfx_emitters"] = kept

        obj["reskate_exp_json"] = json.dumps(exp)
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Removed experimental feature '{key}' from '{obj.name}'.")
        return {'FINISHED'}


class RESKATE_OT_clear_all_exp_features(bpy.types.Operator):
    """Clears all active ReSkate Lab experimental features from the active prop"""
    bl_idname = "reskate.clear_all_exp_features"
    bl_label = "Clear All Experimental"
    bl_description = "Removes all active ReSkate Lab experimental features (Decal, No-Collide, VFX, Music, Variant) from this prop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj:
            return {'CANCELLED'}
        for child in list(obj.children):
            if child.type == 'EMPTY' and (child.get("reskate_is_exp_helper") or "_VFX_" in child.name or "_MusicBoombox_" in child.name or child.get("reskate_vfx_preset")):
                try:
                    bpy.data.objects.remove(child, do_unlink=True)
                except Exception:
                    pass
        obj["reskate_exp_json"] = "{}"
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Cleared all experimental features from '{obj.name}'.")
        return {'FINISHED'}



# ---------------------------------------------------------------------------
# Blender.org PBR Texture Wiring Helper (Milestone 4 - R4)
# ---------------------------------------------------------------------------

def wire_blender_org_pbr_material(
    target_or_mat,
    *args,
    **kwargs,
) -> bool:
    """
    Wires downloaded/cached Blender.org PBR texture maps (Base Color, Normal/Bump, Roughness/Glossiness, Metallic)
    into `mat`'s Principled BSDF using a clean UV Tiling Mapping node graph:
      UV (TexCoord) -> Mapping ("ReSkate_Template_Mapping", Scale=(uv_scale, uv_scale, 1.0))
        -> ReSkate_PBR_BaseColorTex (sRGB, REPEAT) -> Principled BSDF [Base Color]
        -> ReSkate_PBR_NormalTex (Non-Color, REPEAT) -> ReSkate_PBR_NormalMap (or Bump) -> Principled BSDF [Normal]
        -> ReSkate_PBR_RoughnessTex (Non-Color, REPEAT) -> (ReSkate_PBR_RoughnessInvert if glossiness) -> Principled BSDF [Roughness]
    Supports:
      wire_blender_org_pbr_material(mat, asset_slug, maps_dict, uv_scale=1.0)
      wire_blender_org_pbr_material(obj, mat, maps, cfg, uv_scale=1.0)
    Returns True on success, False if no valid Base Color texture could be wired.
    """
    if isinstance(target_or_mat, bpy.types.Object):
        obj = target_or_mat
        mat = args[0] if len(args) > 0 else kwargs.get("mat", getattr(obj, "active_material", None))
        maps_data = args[1] if len(args) > 1 else kwargs.get("maps", kwargs.get("maps_dict", {}))
        cfg = args[2] if len(args) > 2 else kwargs.get("cfg", {})
        uv_scale = args[3] if len(args) > 3 else kwargs.get("uv_scale", 1.0)
        asset_slug = kwargs.get("asset_slug", "")
    else:
        mat = target_or_mat
        obj = kwargs.get("obj", None)
        asset_slug = args[0] if len(args) > 0 else kwargs.get("asset_slug", "")
        maps_data = args[1] if len(args) > 1 else kwargs.get("maps_dict", kwargs.get("maps", {}))
        uv_scale = args[2] if len(args) > 2 else kwargs.get("uv_scale", 1.0)
        cfg = kwargs.get("cfg", {})

    if not mat or not getattr(mat, "use_nodes", False) or not mat.node_tree:
        if mat:
            mat.use_nodes = True
        else:
            return False

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    p_node = compiler_bridge._principled_node(mat) if hasattr(compiler_bridge, "_principled_node") else next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not p_node:
        p_node = nodes.new("ShaderNodeBsdfPrincipled")
        p_node.location = (0, 0)
        out_n = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None) or nodes.new("ShaderNodeOutputMaterial")
        out_n.location = (300, 0)
        links.new(p_node.outputs["BSDF"], out_n.inputs["Surface"])

    # Extract maps and topology flags
    if isinstance(maps_data, dict):
        if "maps" in maps_data and isinstance(maps_data["maps"], dict):
            maps = maps_data["maps"]
            is_normal_bump = bool(maps_data.get("is_normal_bump", False))
            is_glossiness = bool(maps_data.get("is_glossiness", False))
        else:
            maps = maps_data
            is_normal_bump = kwargs.get("is_normal_bump", None)
            is_glossiness = kwargs.get("is_glossiness", None)
    else:
        maps = {}
        is_normal_bump = False
        is_glossiness = False

    if is_normal_bump is None or is_glossiness is None:
        if asset_slug and hasattr(compiler_bridge, "resolve_blender_org_asset_info"):
            _, _, v_info = compiler_bridge.resolve_blender_org_asset_info("", asset_slug)
            if v_info:
                if is_normal_bump is None:
                    is_normal_bump = v_info.get("is_normal_bump", False)
                if is_glossiness is None:
                    is_glossiness = v_info.get("is_glossiness", False)
        if is_normal_bump is None:
            is_normal_bump = bool(maps.get("bump") and not maps.get("normal"))
        if is_glossiness is None:
            is_glossiness = bool(maps.get("glossiness") and not maps.get("roughness"))

    # 1. Unhook existing procedural wear nodes if present
    if hasattr(compiler_bridge, "remove_procedural_wear_nodes_from_material"):
        try:
            compiler_bridge.remove_procedural_wear_nodes_from_material(mat)
        except Exception:
            pass

    # 2. Remove obsolete procedural noise / color ramp nodes
    for old_name in ("ReSkate_Template_Noise", "ReSkate_Template_ColorRamp", "ReSkate_Template_Bump"):
        old_n = nodes.get(old_name)
        if old_n is not None:
            try:
                nodes.remove(old_n)
            except Exception:
                pass

    # 3. Setup TexCoord -> Mapping (UV Tiling Scale)
    tc_node = nodes.get("ReSkate_Template_TexCoord") or nodes.new("ShaderNodeTexCoord")
    tc_node.name = "ReSkate_Template_TexCoord"
    tc_node.location = (-1100, 200)

    map_node = nodes.get("ReSkate_Template_Mapping") or nodes.new("ShaderNodeMapping")
    map_node.name = "ReSkate_Template_Mapping"
    map_node.label = "ReSkate UV Tiling Scale"
    map_node.location = (-900, 200)
    if "Scale" in map_node.inputs:
        map_node.inputs["Scale"].default_value = (float(uv_scale), float(uv_scale), 1.0)
    links.new(tc_node.outputs["UV"], map_node.inputs["Vector"])

    # 4. Base Color Image Texture
    color_path = maps.get("color")
    bc_img = None
    if color_path and os.path.isfile(str(color_path)):
        try:
            bc_img = bpy.data.images.load(str(color_path), check_existing=True)
            if hasattr(bc_img, "colorspace_settings"):
                bc_img.colorspace_settings.name = 'sRGB'
        except Exception as e:
            print(f"[WARN] ReSkate: Failed to load Base Color image '{color_path}': {e}")

    if bc_img is None:
        return False

    bc_node = nodes.get("ReSkate_PBR_BaseColorTex") or nodes.new("ShaderNodeTexImage")
    bc_node.name = "ReSkate_PBR_BaseColorTex"
    bc_node.label = "Blender.org Base Color"
    bc_node.image = bc_img
    bc_node.extension = 'REPEAT'
    bc_node.location = (-650, 260)
    links.new(map_node.outputs["Vector"], bc_node.inputs["Vector"])

    bc_sock = p_node.inputs.get("Base Color")
    if bc_sock is not None:
        for l in list(bc_sock.links):
            links.remove(l)
        links.new(bc_node.outputs["Color"], bc_sock)

    # 5. Normal / Bump Map
    norm_path = maps.get("normal")
    bump_path = maps.get("bump") or maps.get("displacement")
    n_sock = p_node.inputs.get("Normal")

    if is_normal_bump and (bump_path or norm_path):
        b_target = bump_path or norm_path
        b_img = None
        if b_target and os.path.isfile(str(b_target)):
            try:
                b_img = bpy.data.images.load(str(b_target), check_existing=True)
                if hasattr(b_img, "colorspace_settings"):
                    b_img.colorspace_settings.name = 'Non-Color'
            except Exception:
                b_img = None

        if b_img is not None:
            b_tex = nodes.get("ReSkate_PBR_BumpTex") or nodes.new("ShaderNodeTexImage")
            b_tex.name = "ReSkate_PBR_BumpTex"
            b_tex.label = "Blender.org Height/Bump Map"
            b_tex.image = b_img
            b_tex.extension = 'REPEAT'
            b_tex.location = (-650, -220)
            links.new(map_node.outputs["Vector"], b_tex.inputs["Vector"])

            bm = nodes.get("ReSkate_PBR_Bump") or nodes.new("ShaderNodeBump")
            bm.name = "ReSkate_PBR_Bump"
            bm.location = (-380, -220)
            if "Strength" in bm.inputs:
                bm.inputs["Strength"].default_value = float(cfg.get("bump_strength", 0.30) if cfg else 0.30)
            if "Distance" in bm.inputs:
                bm.inputs["Distance"].default_value = 0.015
            links.new(b_tex.outputs["Color"], bm.inputs["Height"])

            if n_sock is not None:
                for l in list(n_sock.links):
                    links.remove(l)
                links.new(bm.outputs["Normal"], n_sock)
    elif norm_path and os.path.isfile(str(norm_path)):
        n_img = None
        try:
            n_img = bpy.data.images.load(str(norm_path), check_existing=True)
            if hasattr(n_img, "colorspace_settings"):
                n_img.colorspace_settings.name = 'Non-Color'
        except Exception:
            n_img = None

        if n_img is not None:
            n_tex = nodes.get("ReSkate_PBR_NormalTex") or nodes.new("ShaderNodeTexImage")
            n_tex.name = "ReSkate_PBR_NormalTex"
            n_tex.label = "Blender.org Normal Map"
            n_tex.image = n_img
            n_tex.extension = 'REPEAT'
            n_tex.location = (-650, -220)
            links.new(map_node.outputs["Vector"], n_tex.inputs["Vector"])

            n_map = nodes.get("ReSkate_PBR_NormalMap") or nodes.new("ShaderNodeNormalMap")
            n_map.name = "ReSkate_PBR_NormalMap"
            n_map.label = "ReSkate Normal Map"
            n_map.location = (-380, -220)
            links.new(n_tex.outputs["Color"], n_map.inputs["Color"])

            if n_sock is not None:
                for l in list(n_sock.links):
                    links.remove(l)
                links.new(n_map.outputs["Normal"], n_sock)
    else:
        if n_sock is not None:
            for l in list(n_sock.links):
                links.remove(l)

    # 6. Roughness / Glossiness Map
    rough_path = maps.get("roughness")
    gloss_path = maps.get("glossiness")
    r_sock = p_node.inputs.get("Roughness")

    if is_glossiness and (gloss_path or rough_path):
        g_target = gloss_path or rough_path
        g_img = None
        if g_target and os.path.isfile(str(g_target)):
            try:
                g_img = bpy.data.images.load(str(g_target), check_existing=True)
                if hasattr(g_img, "colorspace_settings"):
                    g_img.colorspace_settings.name = 'Non-Color'
            except Exception:
                g_img = None

        if g_img is not None:
            r_tex = nodes.get("ReSkate_PBR_RoughnessTex") or nodes.new("ShaderNodeTexImage")
            r_tex.name = "ReSkate_PBR_RoughnessTex"
            r_tex.label = "Blender.org Glossiness (Inverted)"
            r_tex.image = g_img
            r_tex.extension = 'REPEAT'
            r_tex.location = (-650, 20)
            links.new(map_node.outputs["Vector"], r_tex.inputs["Vector"])

            inv_n = nodes.get("ReSkate_PBR_RoughnessInvert") or nodes.new("ShaderNodeInvert")
            inv_n.name = "ReSkate_PBR_RoughnessInvert"
            inv_n.location = (-380, 20)
            links.new(r_tex.outputs["Color"], inv_n.inputs["Color"])

            if r_sock is not None:
                for l in list(r_sock.links):
                    links.remove(l)
                links.new(inv_n.outputs["Color"], r_sock)
    elif rough_path and os.path.isfile(str(rough_path)):
        r_img = None
        try:
            r_img = bpy.data.images.load(str(rough_path), check_existing=True)
            if hasattr(r_img, "colorspace_settings"):
                r_img.colorspace_settings.name = 'Non-Color'
        except Exception:
            r_img = None

        if r_img is not None:
            r_tex = nodes.get("ReSkate_PBR_RoughnessTex") or nodes.new("ShaderNodeTexImage")
            r_tex.name = "ReSkate_PBR_RoughnessTex"
            r_tex.label = "Blender.org Roughness"
            r_tex.image = r_img
            r_tex.extension = 'REPEAT'
            r_tex.location = (-650, 20)
            links.new(map_node.outputs["Vector"], r_tex.inputs["Vector"])

            if r_sock is not None:
                for l in list(r_sock.links):
                    links.remove(l)
                links.new(r_tex.outputs["Color"], r_sock)
    else:
        if r_sock is not None:
            for l in list(r_sock.links):
                links.remove(l)
            r_sock.default_value = float(cfg.get("roughness", 0.84) if cfg else 0.84)

    # 7. Metallic Map
    metal_path = maps.get("metallic")
    m_sock = p_node.inputs.get("Metallic")
    if metal_path and os.path.isfile(str(metal_path)):
        m_img = None
        try:
            m_img = bpy.data.images.load(str(metal_path), check_existing=True)
            if hasattr(m_img, "colorspace_settings"):
                m_img.colorspace_settings.name = 'Non-Color'
        except Exception:
            m_img = None

        if m_img is not None:
            m_tex = nodes.get("ReSkate_PBR_MetallicTex") or nodes.new("ShaderNodeTexImage")
            m_tex.name = "ReSkate_PBR_MetallicTex"
            m_tex.label = "Blender.org Metallic"
            m_tex.image = m_img
            m_tex.extension = 'REPEAT'
            m_tex.location = (-650, -420)
            links.new(map_node.outputs["Vector"], m_tex.inputs["Vector"])

            if m_sock is not None:
                for l in list(m_sock.links):
                    links.remove(l)
                links.new(m_tex.outputs["Color"], m_sock)
    else:
        if m_sock is not None:
            for l in list(m_sock.links):
                links.remove(l)
            m_sock.default_value = float(cfg.get("metallic", 0.0) if cfg else 0.0)

    if hasattr(compiler_bridge, "invalidate_roughness_summary_cache"):
        compiler_bridge.invalidate_roughness_summary_cache(mat.name)

    return True


class RESKATE_OT_apply_starter_material_template(bpy.types.Operator):
    """Applies a 1-click Starter Material Template (Concrete, Wood, Metal, Asphalt, Marble, or Painted) with a ready-to-edit Principled BSDF + UV Tiling & Bump setup"""
    bl_idname = "reskate.apply_starter_material_template"
    bl_label = "Apply Starter Material Template"
    bl_description = "Sets up a complete Principled BSDF shader (Base Color, Roughness, Metallic, Bump, and UV Tiling Mapping node) and syncs the slot's physics surface"
    bl_options = {'REGISTER', 'UNDO'}

    template_id: StringProperty(name="Template ID", default="")
    template: StringProperty(name="Template", default="CONCRETE")

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh object first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        tid = (self.template_id or self.template or "CONCRETE").upper()
        alias_map = {
            "PAINTED_COAT": "PAINTED",
            "METAL_RAIL": "METAL",
            "WOOD_RAMP": "WOOD",
            "MARBLE_TILE": "MARBLE",
        }
        tid = alias_map.get(tid, tid)

        templates = {
            "CONCRETE": {
                "label": "Concrete",
                "base_color": (0.58, 0.59, 0.60, 1.0),
                "color2": (0.48, 0.49, 0.50, 1.0),
                "roughness": 0.84,
                "metallic": 0.0,
                "noise_scale": 32.0,
                "bump_strength": 0.22,
                "surface": "concrete_smooth",
                "detail_preset": "CONCRETE_SIDEWALK",
                "detail_intensity": 0.35,
            },
            "WOOD": {
                "label": "Wood / Plywood",
                "base_color": (0.64, 0.46, 0.28, 1.0),
                "color2": (0.48, 0.32, 0.18, 1.0),
                "roughness": 0.62,
                "metallic": 0.0,
                "noise_scale": 18.0,
                "bump_strength": 0.16,
                "surface": "wood_ramp",
                "detail_preset": "SYNTHETIC_PAINT",
                "detail_intensity": 0.20,
            },
            "METAL": {
                "label": "Metal / Rail",
                "base_color": (0.72, 0.74, 0.77, 1.0),
                "color2": (0.58, 0.60, 0.64, 1.0),
                "roughness": 0.24,
                "metallic": 0.95,
                "noise_scale": 45.0,
                "bump_strength": 0.08,
                "surface": "metal_grindable",
                "detail_preset": "METAL_BRASS",
                "detail_intensity": 0.25,
            },
            "ASPHALT": {
                "label": "Asphalt",
                "base_color": (0.20, 0.21, 0.23, 1.0),
                "color2": (0.13, 0.14, 0.15, 1.0),
                "roughness": 0.90,
                "metallic": 0.0,
                "noise_scale": 55.0,
                "bump_strength": 0.32,
                "surface": "asphalt",
                "detail_preset": "CONCRETE_SIDEWALK",
                "detail_intensity": 0.45,
            },
            "MARBLE": {
                "label": "Marble / Plaza Tile",
                "base_color": (0.86, 0.85, 0.83, 1.0),
                "color2": (0.72, 0.71, 0.69, 1.0),
                "roughness": 0.18,
                "metallic": 0.0,
                "noise_scale": 8.5,
                "bump_strength": 0.05,
                "surface": "marble_tile",
                "detail_preset": "SYNTHETIC_PAINT",
                "detail_intensity": 0.15,
            },
            "PAINTED": {
                "label": "Painted Coat",
                "base_color": (0.18, 0.46, 0.78, 1.0),
                "color2": (0.15, 0.40, 0.70, 1.0),
                "roughness": 0.36,
                "metallic": 0.05,
                "noise_scale": 40.0,
                "bump_strength": 0.10,
                "surface": "metal_grindable",
                "detail_preset": "SYNTHETIC_PAINT",
                "detail_intensity": 0.25,
            },
        }
        cfg = templates.get(tid, templates["CONCRETE"])
        wear_basis_map = {
            "CONCRETE": "CONCRETE",
            "WOOD": "WOOD",
            "METAL": "METAL_RAIL",
            "ASPHALT": "ASPHALT",
            "MARBLE": "CONCRETE",
            "PAINTED": "PAINTED",
        }

        target_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not target_objs or obj not in target_objs:
            target_objs = [obj] + [o for o in target_objs if o != obj]

        last_mat_name = ""
        for t_obj in target_objs:
            if not t_obj or t_obj.type != 'MESH' or not t_obj.data:
                continue

            if len(t_obj.data.materials) == 0:
                mat = bpy.data.materials.new(name=f"{t_obj.name}_{cfg['label']}")
                t_obj.data.materials.append(mat)
                slot_idx = 0
            else:
                slot_idx = max(0, min(t_obj.active_material_index, len(t_obj.material_slots) - 1))
                # Always ensure `t_obj` has its own unique material datablock so editing/applying a template
                # never mutates shared materials across other objects in the scene!
                mat = compiler_bridge.ensure_unique_object_material(t_obj, slot_idx)
                if mat is None:
                    mat = bpy.data.materials.new(name=f"{t_obj.name}_{cfg['label']}")
                    t_obj.material_slots[slot_idx].material = mat

            # GUARANTEE UNIQUE MATERIAL ISOLATION (mat.users == 1)
            if getattr(mat, "users", 1) > 1 or getattr(mat, "use_fake_user", False):
                mat = mat.copy()
                mat.use_fake_user = False
                t_obj.material_slots[slot_idx].material = mat

            last_mat_name = mat.name
            mat.use_nodes = True
            nt = mat.node_tree
            nodes = nt.nodes
            links = nt.links

            p_node = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
            if not p_node:
                p_node = nodes.new("ShaderNodeBsdfPrincipled")
                out_n = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None) or nodes.new("ShaderNodeOutputMaterial")
                links.new(p_node.outputs["BSDF"], out_n.inputs["Surface"])

            uv_scale = float(getattr(prop, "uv_tiling_mult", 1.0)) if prop else 1.0

            # Determine whether to apply Blender.org PBR textures or procedural fallback
            use_b_org = bool(getattr(prop, "use_blender_org_textures", True))
            wired_pbr_success = False

            if use_b_org and hasattr(compiler_bridge, "fetch_and_cache_blender_org_maps"):
                selected_slug = ""
                if prop and hasattr(prop, "get_selected_blender_org_slug") and getattr(prop, "blender_org_category", "") == tid:
                    selected_slug = prop.get_selected_blender_org_slug()
                elif prop and hasattr(prop, f"blender_org_{tid.lower()}_variant"):
                    selected_slug = getattr(prop, f"blender_org_{tid.lower()}_variant", "")
                if not selected_slug and hasattr(properties, "BLENDER_ORG_CATALOG"):
                    cat_entry = properties.BLENDER_ORG_CATALOG.get(tid, {})
                    selected_slug = cat_entry.get("default", cat_entry.get("default_variant", ""))

                staging_dir_val = getattr(prop, "staging_dir", "") or r"B:\Games\ReSkate\Staging_BlenderPropTest"
                staging_dir = Path(staging_dir_val)
                offline_flag = False
                if prop and hasattr(prop, "blender_org_offline_fallback") and not prop.blender_org_offline_fallback:
                    offline_flag = False

                try:
                    maps = compiler_bridge.fetch_and_cache_blender_org_maps(tid, selected_slug, staging_dir, offline=offline_flag)
                    if maps and not maps.get("used_fallback") and not maps.get("is_fallback") and maps.get("maps", {}).get("color"):
                        wired_pbr_success = wire_blender_org_pbr_material(
                            t_obj, mat, maps, cfg, uv_scale=uv_scale
                        )
                except Exception as e:
                    print(f"[WARN] ReSkate: Blender.org texture fetch failed: {e}")
                    wired_pbr_success = False

            # Fallback to procedural template nodes if Blender.org textures not used or download unavailable
            if not wired_pbr_success:
                # Create/update non-destructive template nodes (UV -> Mapping -> Noise -> ColorRamp / Bump -> Principled BSDF)
                tc_node = nodes.get("ReSkate_Template_TexCoord") or nodes.new("ShaderNodeTexCoord")
                tc_node.name = "ReSkate_Template_TexCoord"
                tc_node.location = (-860, 120)

                map_node = nodes.get("ReSkate_Template_Mapping") or nodes.new("ShaderNodeMapping")
                map_node.name = "ReSkate_Template_Mapping"
                map_node.label = "ReSkate UV Tiling Scale"
                map_node.location = (-660, 120)
                if "Scale" in map_node.inputs:
                    map_node.inputs["Scale"].default_value = (uv_scale, uv_scale, 1.0)

                noise_node = nodes.get("ReSkate_Template_Noise") or nodes.new("ShaderNodeTexNoise")
                noise_node.name = "ReSkate_Template_Noise"
                noise_node.location = (-440, 120)
                if "Scale" in noise_node.inputs:
                    noise_node.inputs["Scale"].default_value = float(cfg["noise_scale"])
                if "Detail" in noise_node.inputs:
                    noise_node.inputs["Detail"].default_value = 6.0
                if "Roughness" in noise_node.inputs:
                    noise_node.inputs["Roughness"].default_value = 0.65

                ramp_node = nodes.get("ReSkate_Template_ColorRamp") or nodes.new("ShaderNodeValToRGB")
                ramp_node.name = "ReSkate_Template_ColorRamp"
                ramp_node.location = (-230, 220)
                if ramp_node.color_ramp and len(ramp_node.color_ramp.elements) >= 2:
                    ramp_node.color_ramp.elements[0].position = 0.25
                    ramp_node.color_ramp.elements[0].color = cfg["color2"]
                    ramp_node.color_ramp.elements[1].position = 0.75
                    ramp_node.color_ramp.elements[1].color = cfg["base_color"]

                bump_node = nodes.get("ReSkate_Template_Bump") or nodes.new("ShaderNodeBump")
                bump_node.name = "ReSkate_Template_Bump"
                bump_node.location = (-230, -60)
                if "Strength" in bump_node.inputs:
                    bump_node.inputs["Strength"].default_value = float(cfg["bump_strength"])
                if "Distance" in bump_node.inputs:
                    bump_node.inputs["Distance"].default_value = 0.015

                links.new(tc_node.outputs["UV"], map_node.inputs["Vector"])
                links.new(map_node.outputs["Vector"], noise_node.inputs["Vector"])
                links.new(noise_node.outputs["Fac"], ramp_node.inputs["Fac"])
                links.new(noise_node.outputs["Fac"], bump_node.inputs["Height"])

                # Unhook any existing ReSkate_Wear_* wrapper links before wiring the starter template
                if hasattr(compiler_bridge, "remove_procedural_wear_nodes_from_material"):
                    try:
                        compiler_bridge.remove_procedural_wear_nodes_from_material(mat)
                    except Exception:
                        pass
                if hasattr(compiler_bridge, "remove_projected_decal_nodes_from_material"):
                    try:
                        compiler_bridge.remove_projected_decal_nodes_from_material(mat)
                    except Exception:
                        pass

                # Explicitly remove obsolete ReSkate_PBR_* nodes from any prior PBR material
                for pbr_name in (
                    "ReSkate_PBR_BaseColorTex",
                    "ReSkate_PBR_NormalTex",
                    "ReSkate_PBR_NormalMap",
                    "ReSkate_PBR_RoughnessTex",
                    "ReSkate_PBR_RoughnessInvert",
                    "ReSkate_PBR_MetallicTex",
                ):
                    pbr_n = nodes.get(pbr_name)
                    if pbr_n is not None:
                        try:
                            nodes.remove(pbr_n)
                        except Exception:
                            pass

                # Only connect template ColorRamp / Bump if the user hasn't already wired an Image Texture node
                bc_sock = p_node.inputs.get("Base Color")
                if bc_sock is not None:
                    has_user_img = bc_sock.is_linked and any(
                        l.from_node.type == "TEX_IMAGE" and not l.from_node.name.startswith("ReSkate_PBR_")
                        for l in bc_sock.links
                    )
                    if not has_user_img:
                        links.new(ramp_node.outputs["Color"], bc_sock)
                    bc_sock.default_value = cfg["base_color"]

                r_sock = p_node.inputs.get("Roughness")
                if r_sock is not None:
                    has_user_r_img = r_sock.is_linked and any(
                        l.from_node.type == "TEX_IMAGE" and not l.from_node.name.startswith("ReSkate_PBR_")
                        for l in r_sock.links
                    )
                    if not has_user_r_img:
                        for l in list(r_sock.links):
                            links.remove(l)
                    r_sock.default_value = float(cfg["roughness"])

                m_sock = p_node.inputs.get("Metallic")
                if m_sock is not None:
                    has_user_m_img = m_sock.is_linked and any(
                        l.from_node.type == "TEX_IMAGE" and not l.from_node.name.startswith("ReSkate_PBR_")
                        for l in m_sock.links
                    )
                    if not has_user_m_img:
                        for l in list(m_sock.links):
                            links.remove(l)
                    m_sock.default_value = float(cfg["metallic"])

                n_sock = p_node.inputs.get("Normal")
                if n_sock is not None:
                    has_user_n = n_sock.is_linked and any(
                        (l.from_node.type == "NORMAL_MAP" and not l.from_node.name.startswith("ReSkate_PBR_"))
                        for l in n_sock.links
                    )
                    if not has_user_n:
                        links.new(bump_node.outputs["Normal"], n_sock)

            # Preserve Round Rail physics (2976) if this object is already a Round Rail and template is METAL/PAINTED!
            t_state = dict(get_or_init_object_prop_state(t_obj))
            has_rr_paths = any(
                getattr(gp, "enabled", True) and getattr(gp, "grind_type", "") in ("ROUND_RAIL", "COPING")
                for gp in getattr(t_obj, "reskate_grind_paths", [])
            )
            was_round_rail = (
                t_state.get("primary_surface") == "metal_rail"
                or int(t_state.get("surface_material_code", 0) or 0) in (2976, 37227424)
                or bool(t_state.get("round_rail", False))
                or has_rr_paths
            )
            target_surf = str(cfg["surface"])
            if was_round_rail and target_surf == "metal_grindable":
                target_surf = "metal_rail"

            if t_obj == obj and prop:
                prev_sync = getattr(properties, "_IS_SYNCING_PROP", False)
                properties._IS_SYNCING_PROP = True
                try:
                    prop.dielectric_roughness = float(cfg["roughness"])
                    prop.dielectric_metallic = float(cfg["metallic"])
                    prop.primary_surface = target_surf
                    if target_surf == "metal_rail":
                        prop.round_rail = True
                    prop.detail_2x2m_preset = str(cfg["detail_preset"])
                    prop.detail_2x2m_intensity = float(cfg["detail_intensity"])
                    if hasattr(prop, "wear_basis"):
                        prop.wear_basis = wear_basis_map.get(tid, "AUTO")
                finally:
                    properties._IS_SYNCING_PROP = prev_sync
            else:
                t_state["dielectric_roughness"] = float(cfg["roughness"])
                t_state["dielectric_metallic"] = float(cfg["metallic"])
                t_state["primary_surface"] = target_surf
                t_state["surface_material_code"] = compiler_bridge.get_surface_packed_code(target_surf)
                if target_surf == "metal_rail":
                    t_state["round_rail"] = True
                t_state["detail_2x2m_preset"] = str(cfg["detail_preset"])
                t_state["detail_2x2m_intensity"] = float(cfg["detail_intensity"])
                t_state["wear_basis"] = wear_basis_map.get(tid, "AUTO")
                ser_t = json.dumps(t_state)
                t_obj["reskate_prop_state"] = ser_t
                t_obj["reskate_prop_state_json"] = ser_t

            if (getattr(prop, "use_procedural_wear", False) or t_state.get("use_procedural_wear", False)) and hasattr(compiler_bridge, "sync_procedural_wear_to_viewport"):
                try:
                    compiler_bridge.sync_procedural_wear_to_viewport(t_obj, prop if t_obj == obj else None)
                except Exception:
                    pass

            # Sync slot physics surface
            if hasattr(t_obj, "reskate_material_surfaces") and 0 <= slot_idx < len(t_obj.reskate_material_surfaces):
                t_obj.reskate_material_surfaces[slot_idx].surface = target_surf
            ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
            if ms is not None and hasattr(ms, "surface"):
                try:
                    ms.surface = target_surf
                except Exception:
                    pass
            compiler_bridge.invalidate_roughness_summary_cache(mat.name)

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        if len(target_objs) > 1:
            self.report({'INFO'}, f"ReSkate: Applied '{cfg['label']}' starter template with unique per-object materials to {len(target_objs)} props.")
        else:
            self.report({'INFO'}, f"ReSkate: Applied '{cfg['label']}' starter material template to '{last_mat_name}'.")
        return {'FINISHED'}


class RESKATE_OT_fetch_blender_org_texture(bpy.types.Operator):
    """Downloads and caches the selected Blender.org texture archive asset and wires it into the active material"""
    bl_idname = "reskate.fetch_blender_org_texture"
    bl_label = "Download & Apply Blender.org Texture"
    bl_description = "Fetches Base Color, Normal, and Roughness maps from Blender.org and wires them into the active material's Principled BSDF"
    bl_options = {'REGISTER', 'UNDO'}

    category: StringProperty(name="Category", default="CONCRETE")
    slug: StringProperty(name="Slug", default="")

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        slot_idx = max(0, min(obj.active_material_index, len(obj.material_slots) - 1)) if len(obj.material_slots) > 0 else 0
        mat = compiler_bridge.ensure_unique_object_material(obj, slot_idx)
        if mat is None:
            mat = bpy.data.materials.new(name=f"{obj.name}_{self.category}")
            if len(obj.material_slots) == 0:
                obj.data.materials.append(mat)
            else:
                obj.material_slots[slot_idx].material = mat

        # Guarantee unique isolation
        if getattr(mat, "users", 1) > 1 or getattr(mat, "use_fake_user", False):
            mat = mat.copy()
            mat.use_fake_user = False
            obj.material_slots[slot_idx].material = mat

        cat = (self.category or (prop.blender_org_category if prop else "CONCRETE")).upper()
        slug = self.slug
        if not slug and prop and hasattr(prop, "get_selected_blender_org_slug") and getattr(prop, "blender_org_category", "") == cat:
            slug = prop.get_selected_blender_org_slug()
        elif not slug and prop and hasattr(prop, f"blender_org_{cat.lower()}_variant"):
            slug = getattr(prop, f"blender_org_{cat.lower()}_variant", "")
        if not slug and hasattr(properties, "BLENDER_ORG_CATALOG"):
            cat_entry = properties.BLENDER_ORG_CATALOG.get(cat, {})
            slug = cat_entry.get("default", cat_entry.get("default_variant", ""))

        staging_dir = Path(getattr(prop, "staging_dir", r"B:\Games\ReSkate\Staging_BlenderPropTest"))
        uv_scale = float(getattr(prop, "uv_tiling_mult", 1.0)) if prop else 1.0
        cfg = {"bump_strength": 0.30, "roughness": 0.84, "metallic": 0.0}

        if hasattr(compiler_bridge, "fetch_and_cache_blender_org_maps"):
            maps = compiler_bridge.fetch_and_cache_blender_org_maps(cat, slug, staging_dir)
            if maps and not maps.get("used_fallback") and not maps.get("is_fallback") and maps.get("maps", {}).get("color"):
                ok = wire_blender_org_pbr_material(obj, mat, maps, cfg, uv_scale=uv_scale)
                if ok:
                    if (getattr(prop, "use_procedural_wear", False)) and hasattr(compiler_bridge, "sync_procedural_wear_to_viewport"):
                        try:
                            compiler_bridge.sync_procedural_wear_to_viewport(obj, prop)
                        except Exception:
                            pass
                    self.report({'INFO'}, f"ReSkate: Downloaded and applied '{slug}' to '{mat.name}'.")
                    return {'FINISHED'}

        self.report({'WARNING'}, f"ReSkate: Could not download '{slug}', falling back to procedural template.")
        bpy.ops.reskate.apply_starter_material_template(template_id=cat)
        return {'FINISHED'}


class RESKATE_OT_apply_uv_tiling_scale(bpy.types.Operator):
    """Applies the live UV Tiling Scale multiplier to the active material's Mapping node (creating one if needed) and 2x2m detail scale"""
    bl_idname = "reskate.apply_uv_tiling_scale"
    bl_label = "Apply UV Tiling to Shader"
    bl_description = "Updates or inserts a UV Mapping node on the active material scaled by UV Tiling Scale so textures tile cleanly in the viewport and in-game"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}
        prop = getattr(context.scene, "reskate_prop", None)
        scale_val = float(getattr(prop, "uv_tiling_mult", 1.0)) if prop else 1.0
        if prop:
            prop.detail_2x2m_scale = scale_val

        slot_idx = max(0, min(obj.active_material_index, len(obj.material_slots) - 1)) if len(obj.material_slots) > 0 else -1
        mat = obj.material_slots[slot_idx].material if slot_idx >= 0 else None
        if not mat or not mat.use_nodes or not mat.node_tree:
            return {'CANCELLED'}

        nt = mat.node_tree
        nodes = nt.nodes
        links = nt.links

        map_nodes = [n for n in nodes if n.type == "MAPPING"]
        if not map_nodes:
            tc_node = next((n for n in nodes if n.type == "TEX_COORD"), None) or nodes.new("ShaderNodeTexCoord")
            map_node = nodes.new("ShaderNodeMapping")
            map_node.name = "ReSkate_Template_Mapping"
            map_node.label = "ReSkate UV Tiling Scale"
            links.new(tc_node.outputs["UV"], map_node.inputs["Vector"])
            for n in nodes:
                if n.type in ("TEX_IMAGE", "TEX_NOISE", "TEX_VORONOI", "TEX_WAVE") and "Vector" in n.inputs and not n.inputs["Vector"].is_linked:
                    links.new(map_node.outputs["Vector"], n.inputs["Vector"])
            map_nodes = [map_node]

        for mn in map_nodes:
            if "Scale" in mn.inputs:
                mn.inputs["Scale"].default_value = (scale_val, scale_val, 1.0)

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Set UV Tiling Scale to {scale_val:.2f}x on '{mat.name}'.")
        return {'FINISHED'}


class RESKATE_OT_apply_world_scale_uvs(bpy.types.Operator):
    """Projects uniform World-Scale Tiling UVs (in real meters) onto rails, ledges, and blocks without manual UV unwrapping"""
    bl_idname = "reskate.apply_world_scale_uvs"
    bl_label = "Auto World-Scale Tiling UVs (Rail / Ledge Mode)"
    bl_description = (
        "Projects uniform world-meter UVs along the prop's primary length axis so textures tile evenly "
        "(1 UV repeat per Tile Size meters) on long rails, ledges, and blocks with zero stretching"
    )
    bl_options = {'REGISTER', 'UNDO'}

    tile_size_override: FloatProperty(name="Tile Size (m)", default=0.0, min=0.0, max=20.0)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or not obj.data or len(obj.data.polygons) == 0:
            self.report({'WARNING'}, "ReSkate: Select a 3D prop mesh first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        tile_m = (
            float(self.tile_size_override)
            if self.tile_size_override > 0.01
            else float(getattr(prop, "world_scale_tile_size_m", 0.50) if prop else 0.50)
        )
        tile_m = max(0.05, tile_m)
        scope = str(getattr(prop, "world_scale_uv_scope", "ALL_SLOTS") or "ALL_SLOTS") if prop else "ALL_SLOTS"

        was_edit = (obj.mode == 'EDIT')
        if was_edit and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        me = obj.data
        if len(me.uv_layers) == 0:
            me.uv_layers.new(name="UVMap")
        uv_layer = me.uv_layers.active or me.uv_layers[0]
        uv_data = uv_layer.data

        # Determine which material slots should receive world-scale tiling UVs
        # (Skip transparent/decal sticker slots automatically when scope == 'ALL_SLOTS')
        decal_slots: set[int] = set()
        for s_idx, slot in enumerate(obj.material_slots):
            m = slot.material
            if not m:
                continue
            info = compiler_bridge.inspect_material_shader_nodes(m)
            m_low = m.name.lower()
            has_op = bool(info.get("opacity_image") is not None or float(info.get("alpha_scalar", 1.0)) < 0.98)
            is_dec_name = any(k in m_low for k in ("decal", "wax", "sticker", "graffiti", "tag", "stencil", "logo", "splat"))
            if has_op or is_dec_name:
                decal_slots.add(s_idx)

        active_slot_idx = max(0, min(int(obj.active_material_index), max(0, len(obj.material_slots) - 1)))

        mw = obj.matrix_world
        rot_scale = mw.to_3x3()
        try:
            normal_mat = rot_scale.inverted().transposed()
        except Exception:
            normal_mat = rot_scale

        world_verts = [mw @ v.co for v in me.vertices]
        if not world_verts:
            return {'CANCELLED'}

        min_x = min(p.x for p in world_verts)
        max_x = max(p.x for p in world_verts)
        min_y = min(p.y for p in world_verts)
        max_y = max(p.y for p in world_verts)
        min_z = min(p.z for p in world_verts)
        origin_w = Vector((min_x, min_y, min_z))

        # Align primary axis T1 along the horizontal direction of greatest extent (or PCA in XY)
        dx = max_x - min_x
        dy = max_y - min_y
        if len(world_verts) >= 4 and max(dx, dy) > 1e-4:
            cx = sum(p.x for p in world_verts) / float(len(world_verts))
            cy = sum(p.y for p in world_verts) / float(len(world_verts))
            sxx = sum((p.x - cx) ** 2 for p in world_verts)
            syy = sum((p.y - cy) ** 2 for p in world_verts)
            sxy = sum((p.x - cx) * (p.y - cy) for p in world_verts)
            theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
            t1 = Vector((math.cos(theta), math.sin(theta), 0.0)).normalized()
        else:
            t1 = Vector((1.0, 0.0, 0.0)) if dx >= dy else Vector((0.0, 1.0, 0.0))

        t3 = Vector((0.0, 0.0, 1.0))
        t2 = t3.cross(t1).normalized()

        inv_tile = 1.0 / tile_m
        updated_polys = 0

        for poly in me.polygons:
            if scope == 'ACTIVE_SLOT' and len(obj.material_slots) > 1:
                if poly.material_index != active_slot_idx:
                    continue
            elif scope == 'ALL_SLOTS' and len(obj.material_slots) > 1:
                if poly.material_index in decal_slots:
                    continue

            n_w = (normal_mat @ poly.normal)
            if n_w.length_squared > 1e-10:
                n_w.normalize()
            else:
                n_w = Vector((0.0, 0.0, 1.0))

            n1 = abs(n_w.dot(t1))
            n2 = abs(n_w.dot(t2))
            n3 = abs(n_w.dot(t3))

            # For any face that runs along the rail/ledge length (top, bottom, sides, bevels),
            # map U along T1 (length) and V along the dominant cross-section axis (T2 or T3).
            # For end-caps facing T1, map U along T2 and V along T3.
            if n1 >= 0.85 and n1 > n2 and n1 > n3:
                u_axis = t2
                v_axis = t3
            else:
                u_axis = t1
                v_axis = t2 if n3 >= n2 else t3

            for loop_idx in poly.loop_indices:
                v_idx = me.loops[loop_idx].vertex_index
                pw = world_verts[v_idx] - origin_w
                u_val = pw.dot(u_axis) * inv_tile
                v_val = pw.dot(v_axis) * inv_tile
                uv_data[loop_idx].uv = (u_val, v_val)

            updated_polys += 1

        me.update()
        if was_edit and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        self.report(
            {'INFO'},
            f"ReSkate: Projected World-Scale Tiling UVs ({tile_m:.2f}m per tile) across {updated_polys} face(s) on '{obj.name}'.",
        )
        return {'FINISHED'}


class RESKATE_OT_preview_thumbnail_popup(bpy.types.Operator):
    """Opens a pop-out Image Editor window displaying the active prop's full-resolution rendered thumbnail"""
    bl_idname = "reskate.preview_thumbnail_popup"
    bl_label = "Pop-Out Thumbnail Preview"
    bl_description = "Opens the active prop's rendered Quick Drop thumbnail in a pop-out Image Editor preview window"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        prop = getattr(context.scene, "reskate_prop", None)
        staging = compiler_bridge.get_staging_dir(context)

        thumb_path = None
        custom_p = (getattr(prop, "custom_thumbnail_path", "") or "").strip() if prop else ""
        if custom_p:
            cp = Path(bpy.path.abspath(custom_p))
            if cp.exists() and cp.is_file():
                thumb_path = cp
        if thumb_path is None and obj:
            slug = get_prop_slug(context, obj)
            for cand in (
                staging / f"{slug}_thumb_768.png",
                staging / f"{slug}_thumb_256.png",
                staging / "Pack_Queue" / f"{slug}_thumb_768.png",
                staging / "Pack_Queue" / f"{slug}_thumb_256.png",
            ):
                if cand.exists() and cand.is_file():
                    thumb_path = cand
                    break

        if thumb_path is None:
            # Render thumbnails now if none exist yet
            try:
                bpy.ops.reskate.render_thumbnails()
                if obj:
                    slug = get_prop_slug(context, obj)
                    for cand in (staging / f"{slug}_thumb_768.png", staging / f"{slug}_thumb_256.png"):
                        if cand.exists():
                            thumb_path = cand
                            break
            except Exception:
                pass

        if thumb_path is None or not thumb_path.exists():
            self.report({'WARNING'}, "ReSkate: No thumbnail found yet. Click 'Render Thumbnail' first.")
            return {'CANCELLED'}

        try:
            img = bpy.data.images.load(str(thumb_path), check_existing=True)
            img.reload()
            img.preview_ensure()
        except Exception as ex:
            self.report({'ERROR'}, f"ReSkate: Could not load thumbnail image: {ex}")
            return {'CANCELLED'}

        if not bpy.app.background and getattr(context, "window_manager", None):
            try:
                bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
                new_win = context.window_manager.windows[-1]
                area = new_win.screen.areas[0]
                area.type = 'IMAGE_EDITOR'
                area.ui_type = 'IMAGE_EDITOR'
                for space in area.spaces:
                    if space.type == 'IMAGE_EDITOR':
                        space.image = img
                        break
            except Exception:
                # Fallback: assign to any existing IMAGE_EDITOR area
                for win in context.window_manager.windows:
                    for area in win.screen.areas:
                        if area.type == 'IMAGE_EDITOR':
                            for space in area.spaces:
                                if space.type == 'IMAGE_EDITOR':
                                    space.image = img
                                    break

        self.report({'INFO'}, f"ReSkate: Opened thumbnail preview '{thumb_path.name}'.")
        return {'FINISHED'}


class RESKATE_OT_use_prop_thumb_as_mod_icon(bpy.types.Operator):
    """Sets the Mod Folder Icon (icon.png) to the active prop's rendered thumbnail PNG"""
    bl_idname = "reskate.use_prop_thumb_as_mod_icon"
    bl_label = "Use Active Prop Thumbnail as Mod Icon"
    bl_description = "Sets the Mod Folder Icon (icon.png) to the active prop's rendered Quick Drop thumbnail"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop or not obj:
            return {'CANCELLED'}

        staging = compiler_bridge.get_staging_dir(context)
        slug = get_prop_slug(context, obj)
        for cand in (
            staging / f"{slug}_thumb_256.png",
            staging / f"{slug}_thumb_768.png",
            staging / "Pack_Queue" / f"{slug}_thumb_256.png",
        ):
            if cand.exists():
                prop.mod_icon_path = str(cand)
                self.report({'INFO'}, f"ReSkate: Set Mod Folder Icon (icon.png) to '{cand.name}'.")
                return {'FINISHED'}

        self.report({'WARNING'}, "ReSkate: Render the active prop's thumbnail first, or pick an image file.")
        return {'CANCELLED'}


class RESKATE_OT_apply_category_to_selected(bpy.types.Operator):
    """Applies the active Quick Drop Tab (Category) to all selected prop meshes in the scene"""
    bl_idname = "reskate.apply_category_to_selected"
    bl_label = "Apply Quick Drop Tab to Selected Props"
    bl_description = "Copies the current Quick Drop Tab (Category) to all selected 3D prop meshes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not sel_objs:
            self.report({'WARNING'}, "ReSkate: Select at least one prop mesh.")
            return {'CANCELLED'}

        custom_tab = str(getattr(prop, "custom_qd_tab", "CustomProps") or "CustomProps").strip() or "CustomProps"
        if hasattr(properties, "format_qd_tab_display_name"):
            custom_tab = properties.format_qd_tab_display_name(custom_tab)
        cat = "CUSTOM_TAB"
        if hasattr(properties, "save_custom_qd_tab"):
            properties.save_custom_qd_tab(custom_tab)
        for o in sel_objs:
            st = dict(get_or_init_object_prop_state(o))
            st["prop_category"] = cat
            st["custom_qd_tab"] = custom_tab
            ser_o = json.dumps(st)
            o["reskate_prop_state"] = ser_o
            o["reskate_prop_state_json"] = ser_o

        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Applied Quick Drop Tab '{custom_tab}' to {len(sel_objs)} selected prop(s).")
        return {'FINISHED'}


class RESKATE_OT_select_saved_qd_tab(bpy.types.Operator):
    """Selects a Quick Drop Tab Name used in this Prop Pack (or saved shortcuts) in 1 click"""
    bl_idname = "reskate.select_saved_qd_tab"
    bl_label = "Select Quick Drop Tab"
    bl_description = "Applies this Quick Drop Tab Name to the active prop (and all selected props if 2+ are selected)"
    bl_options = {'REGISTER', 'UNDO'}

    tab_id: StringProperty(name="Tab ID", default="CustomProps")

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        raw_val = str(self.tab_id or "").strip()
        if not raw_val:
            return {'CANCELLED'}
        disp_val = (
            properties.format_qd_tab_display_name(raw_val)
            if hasattr(properties, "format_qd_tab_display_name")
            else raw_val
        )
        prop.prop_category = 'CUSTOM_TAB'
        prop.custom_qd_tab = disp_val
        if hasattr(properties, "save_custom_qd_tab"):
            properties.save_custom_qd_tab(disp_val)

        sel_objs = compiler_bridge.get_selected_prop_meshes(context)
        if len(sel_objs) >= 2:
            for o in sel_objs:
                st = dict(get_or_init_object_prop_state(o))
                st["prop_category"] = 'CUSTOM_TAB'
                st["custom_qd_tab"] = disp_val
                ser_o = json.dumps(st)
                o["reskate_prop_state"] = ser_o
                o["reskate_prop_state_json"] = ser_o
            self.report({'INFO'}, f"ReSkate: Selected Quick Drop tab '{disp_val}' on {len(sel_objs)} selected props.")
        else:
            self.report({'INFO'}, f"ReSkate: Selected Quick Drop tab '{disp_val}'.")
        compiler_bridge.invalidate_ui_caches()
        return {'FINISHED'}


class RESKATE_OT_remove_saved_qd_tab(bpy.types.Operator):
    """Removes a custom Quick Drop tab from the persistent saved tabs list"""
    bl_idname = "reskate.remove_saved_qd_tab"
    bl_label = "Remove Saved Quick Drop Tab"
    bl_description = "Removes this Quick Drop tab from the saved shortcuts list"
    bl_options = {'REGISTER', 'UNDO'}

    tab_id: StringProperty(name="Tab ID", default="")

    def execute(self, context):
        t_val = str(self.tab_id or "").strip()
        if t_val and hasattr(properties, "remove_custom_qd_tab"):
            properties.remove_custom_qd_tab(t_val)
            compiler_bridge.invalidate_ui_caches()
            self.report({'INFO'}, f"ReSkate: Removed saved Quick Drop tab '{t_val}'.")
        return {'FINISHED'}


def _sync_face_surface_json_from_array(obj: bpy.types.Object, codes: list[int], legend: dict[int, dict]):
    """Persists painted face surface codes and legend metadata into `obj['reskate_face_surfaces_json']` and `obj['reskate_face_surface_legend_json']`."""
    by_code: dict[str, dict] = {}
    clean_legend: dict[str, dict] = {}
    for fi, c_val in enumerate(codes):
        if c_val <= 0:
            continue
        k = str(int(c_val))
        meta = legend.get(int(c_val), {})
        surf_id = str(meta.get("surface") or f"material_{int(c_val):04d}")
        pws = bool(meta.get("prevent_wall_snap", False))
        lbl = str(meta.get("label") or compiler_bridge.get_surface_label(surf_id if not pws else int(c_val)))
        clean_legend[k] = {
            "surface": surf_id,
            "label": lbl,
            "prevent_wall_snap": pws,
        }
        entry = by_code.get(k)
        if entry is None:
            entry = {
                "surface": surf_id,
                "label": lbl,
                "prevent_wall_snap": pws,
                "face_indices": [],
            }
            by_code[k] = entry
        entry["face_indices"].append(int(fi))
    obj["reskate_face_surfaces_json"] = json.dumps(by_code) if by_code else ""
    obj["reskate_face_surface_legend_json"] = json.dumps(clean_legend) if clean_legend else ""


class RESKATE_OT_assign_face_surface(bpy.types.Operator):
    """Assigns a custom physics & rolling/grind/landing audio surface to currently selected faces"""
    bl_idname = "reskate.assign_face_surface"
    bl_label = "Apply Surface to Selected Faces"
    bl_description = (
        "Assigns the selected Surface / Audio preset to highlighted faces on this mesh (or <Prop>_COL) "
        "so those faces compile into a dedicated Havok collision sub-shape with that exact sound and feel"
    )
    bl_options = {'REGISTER', 'UNDO'}

    surface_override: StringProperty(name="Surface Override", default="")

    def execute(self, context):
        act = context.active_object
        obj = act if (act and act.type == 'MESH') else get_target_mesh_object(context)
        if not obj or obj.type != 'MESH' or not obj.data:
            self.report({'WARNING'}, "ReSkate: Select a mesh and highlight faces in Edit Mode first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        if self.surface_override:
            surf_key = str(self.surface_override)
        elif prop is not None:
            if bool(getattr(prop, "use_full_surface_catalog", False)):
                surf_key = str(getattr(prop, "full_surface_catalog", "material_0096") or "material_0096")
            else:
                surf_key = str(getattr(prop, "face_paint_surface", "metal_rail") or "metal_rail")
        else:
            surf_key = "metal_rail"

        pws = bool(getattr(prop, "face_paint_prevent_wall_snap", False)) if prop else False
        packed_code = int(compiler_bridge.get_surface_packed_code(surf_key))
        if pws:
            packed_code = int(compiler_bridge.apply_prevent_wall_snap_code(packed_code, surf_key))
        surf_label = compiler_bridge.get_surface_label(surf_key if not pws else packed_code)

        me = obj.data
        assigned_count = 0
        codes: list[int] = []

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            layer = bm.faces.layers.int.get("reskate_face_surface") or bm.faces.layers.int.new("reskate_face_surface")
            for f in bm.faces:
                if f.select:
                    f[layer] = packed_code
                    assigned_count += 1
                codes.append(int(f[layer]))
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        else:
            attr = me.attributes.get("reskate_face_surface")
            if attr is None or getattr(attr, "domain", "") != 'FACE':
                if attr is not None:
                    me.attributes.remove(attr)
                attr = me.attributes.new(name="reskate_face_surface", type='INT', domain='FACE')
            for p in me.polygons:
                if p.select:
                    attr.data[p.index].value = packed_code
                    assigned_count += 1
                codes.append(int(attr.data[p.index].value))

        if assigned_count == 0:
            self.report({'WARNING'}, "ReSkate: Select one or more faces in Edit Mode first, then click 'Apply Surface to Selected Faces'.")
            return {'CANCELLED'}

        legend = compiler_bridge.get_object_face_surface_legend(obj)
        legend[packed_code] = {
            "surface": surf_key,
            "label": surf_label,
            "prevent_wall_snap": pws,
        }
        _sync_face_surface_json_from_array(obj, codes, legend)

        if prop is not None and hasattr(prop, "draw_face_surface_overlay"):
            prop.draw_face_surface_overlay = True

        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Applied '{surf_label}' (Code {packed_code}) to {assigned_count} face(s) on '{obj.name}'.")
        return {'FINISHED'}


class RESKATE_OT_clear_face_surface(bpy.types.Operator):
    """Clears custom per-face surface audio overrides from selected faces (or a specific surface group)"""
    bl_idname = "reskate.clear_face_surface"
    bl_label = "Clear Selected Faces"
    bl_description = "Restores selected faces (or this surface group) back to the prop's Default Physics & Surface Audio"
    bl_options = {'REGISTER', 'UNDO'}

    target_packed_code: IntProperty(name="Target Surface Code", default=0)
    target_obj_name: StringProperty(name="Target Object", default="")

    def execute(self, context):
        obj = None
        if self.target_obj_name:
            obj = context.scene.objects.get(self.target_obj_name) or bpy.data.objects.get(self.target_obj_name)
        if obj is None:
            act = context.active_object
            obj = act if (act and act.type == 'MESH') else get_target_mesh_object(context)
        if not obj or obj.type != 'MESH' or not obj.data:
            return {'CANCELLED'}

        me = obj.data
        cleared = 0
        codes: list[int] = []
        target_code = int(self.target_packed_code)

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            layer = bm.faces.layers.int.get("reskate_face_surface")
            if layer is not None:
                for f in bm.faces:
                    val = int(f[layer])
                    if (target_code > 0 and val == target_code) or (target_code <= 0 and f.select and val > 0):
                        f[layer] = 0
                        val = 0
                        cleared += 1
                    codes.append(val)
                bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        else:
            codes = compiler_bridge.get_mesh_face_surface_array(obj)
            attr = me.attributes.get("reskate_face_surface")
            if attr is None and any(c > 0 for c in codes):
                attr = me.attributes.new(name="reskate_face_surface", type='INT', domain='FACE')
                for i, cv in enumerate(codes):
                    if i < len(attr.data):
                        attr.data[i].value = int(cv)
            if attr is not None:
                new_codes = []
                for p in me.polygons:
                    val = int(attr.data[p.index].value)
                    if (target_code > 0 and val == target_code) or (target_code <= 0 and p.select and val > 0):
                        attr.data[p.index].value = 0
                        val = 0
                        cleared += 1
                    new_codes.append(val)
                codes = new_codes

        legend = compiler_bridge.get_object_face_surface_legend(obj)
        _sync_face_surface_json_from_array(obj, codes, legend)
        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Cleared custom surface override from {cleared} face(s) on '{obj.name}'.")
        return {'FINISHED'}


class RESKATE_OT_clear_all_face_surfaces(bpy.types.Operator):
    """Clears all custom per-face surface audio overrides on the active prop (or a single surface code if target_code > 0)"""
    bl_idname = "reskate.clear_all_face_surfaces"
    bl_label = "Clear All Custom Face Surfaces"
    bl_description = "Removes all custom per-face surface audio overrides on this prop and its collision mesh"
    bl_options = {'REGISTER', 'UNDO'}

    target_code: IntProperty(name="Target Surface Code", default=0)

    def execute(self, context):
        t_code = int(self.target_code)
        if t_code > 0:
            return bpy.ops.reskate.clear_face_surface(target_packed_code=t_code)

        act = context.active_object
        owner = get_target_mesh_object(context) or (act if (act and act.type == 'MESH') else None)
        if not owner or owner.type != 'MESH':
            return {'CANCELLED'}
        col_obj = compiler_bridge.resolve_custom_collision_object(context, owner)

        targets = [owner]
        if col_obj is not None and col_obj != owner and col_obj.type == 'MESH':
            targets.append(col_obj)
        if act is not None and act not in targets and act.type == 'MESH':
            targets.append(act)

        total_cleared = 0
        for t_obj in targets:
            me = t_obj.data
            if not me:
                continue
            if t_obj.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(me)
                layer = bm.faces.layers.int.get("reskate_face_surface")
                if layer is not None:
                    for f in bm.faces:
                        if int(f[layer]) != 0:
                            total_cleared += 1
                        f[layer] = 0
                    bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
            else:
                attr = me.attributes.get("reskate_face_surface")
                if attr is not None:
                    for item in attr.data:
                        if int(item.value) != 0:
                            total_cleared += 1
                        item.value = 0
            t_obj["reskate_face_surfaces_json"] = ""
            t_obj["reskate_face_surface_legend_json"] = ""

        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Cleared all custom face surfaces ({total_cleared} face(s) reset).")
        return {'FINISHED'}


class RESKATE_OT_select_face_surface_group(bpy.types.Operator):
    """Enters Edit Mode and selects all faces assigned to a specific custom surface audio group"""
    bl_idname = "reskate.select_face_surface_group"
    bl_label = "Select Faces with Surface"
    bl_description = "Enters Edit Mode and highlights all faces painted with this custom surface audio override"
    bl_options = {'REGISTER', 'UNDO'}

    target_code: IntProperty(name="Target Code", default=0)
    packed_code: IntProperty(name="Packed Code", default=0)
    target_obj_name: StringProperty(name="Target Object", default="")

    def execute(self, context):
        obj = None
        if self.target_obj_name:
            obj = context.scene.objects.get(self.target_obj_name) or bpy.data.objects.get(self.target_obj_name)
        if obj is None:
            act = context.active_object
            obj = act if (act and act.type == 'MESH') else get_target_mesh_object(context)
        if not obj or obj.type != 'MESH' or not obj.data:
            return {'CANCELLED'}

        if context.active_object != obj:
            if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj

        if obj.mode != 'EDIT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='EDIT')

        try:
            context.tool_settings.mesh_select_mode = (False, False, True)
        except Exception:
            pass

        me = obj.data
        sel_count = 0
        target_code = int(self.target_code or self.packed_code)
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            layer = bm.faces.layers.int.get("reskate_face_surface")
            for f in bm.faces:
                match = bool(layer is not None and int(f[layer]) == target_code)
                f.select = match
                if match:
                    sel_count += 1
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        else:
            codes = compiler_bridge.get_mesh_face_surface_array(obj)
            for p in me.polygons:
                match = bool(p.index < len(codes) and int(codes[p.index]) == target_code)
                p.select = match
                if match:
                    sel_count += 1

        self.report({'INFO'}, f"ReSkate: Selected {sel_count} face(s) with Surface Code {target_code} on '{obj.name}'.")
        return {'FINISHED'}


class RESKATE_OT_clear_do_not_align(bpy.types.Operator):
    """Clears all 'Do Not Align' (Prevent Wall-Snap) and 'No-Grind' restrictions from the active/selected prop(s) in 1 click"""
    bl_idname = "reskate.clear_do_not_align"
    bl_label = "Clear 'Do Not Align' & Restore Grind Alignment"
    bl_description = (
        "Strips all 'Do Not Align' (Prevent Wall-Snap / PropertySlot 111 / 527456) and accidental 'No-Grind' flags "
        "from the prop's default surface, material slots, and painted faces so the skater can align and grind freely"
    )
    bl_options = {'REGISTER', 'UNDO'}

    also_clear_no_grind: BoolProperty(name="Also Clear No-Grind", default=True)

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        target_objs = compiler_bridge.get_selected_prop_meshes(context)
        if not target_objs and obj and obj.type == 'MESH':
            target_objs = [obj]
        if not target_objs:
            self.report({'WARNING'}, "ReSkate: Select a prop mesh first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        if prop is not None:
            prop.prevent_wall_snap_default = False
            cur_code = int(getattr(prop, "surface_material_code", 96) or 96)
            if is_do_not_align_surface_code(cur_code):
                clean_c = int(strip_do_not_align_code(cur_code, 96))
                prop.surface_material_code = clean_c
                if getattr(prop, "primary_surface", "") == "edge_excluded":
                    prop.primary_surface = "concrete_smooth"

        cleared_objs = 0
        for t_obj in target_objs:
            if not t_obj or t_obj.type != 'MESH':
                continue
            st = dict(get_or_init_object_prop_state(t_obj))
            st["prevent_wall_snap_default"] = False
            c_code = int(st.get("surface_material_code", 96) or 96)
            if is_do_not_align_surface_code(c_code):
                c_code = int(strip_do_not_align_code(c_code, 96))
                st["surface_material_code"] = c_code
            if st.get("primary_surface") in ("edge_excluded", "no_grind_plastic", "no_grind_cloth") and self.also_clear_no_grind:
                st["primary_surface"] = "concrete_smooth"
                st["surface_material_code"] = 96
            ser = json.dumps(st)
            t_obj["reskate_prop_state"] = ser
            t_obj["reskate_prop_state_json"] = ser

            # Clear per-slot prevent_wall_snap & stale sk8_collision_material_packed
            if hasattr(t_obj, "reskate_material_surfaces"):
                for ms_item in t_obj.reskate_material_surfaces:
                    ms_item.prevent_wall_snap = False
                    s_val = getattr(ms_item, "surface", "DEFAULT")
                    if s_val != "DEFAULT":
                        s_code = compiler_bridge.get_surface_packed_code(s_val)
                        if is_do_not_align_surface_code(s_code):
                            clean_sc = strip_do_not_align_code(s_code, c_code)
                            ms_item.surface = "DEFAULT" if clean_sc == c_code else f"material_{clean_sc:04d}"

            for slot in t_obj.material_slots:
                mat = slot.material
                if not mat:
                    continue
                try:
                    mat["reskate_prevent_wall_snap"] = False
                    raw_mp = int(mat.get("sk8_collision_material_packed", 0) or 0)
                    if is_do_not_align_surface_code(raw_mp):
                        mat["sk8_collision_material_packed"] = int(strip_do_not_align_code(raw_mp, c_code))
                except Exception:
                    pass
                for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
                    if holder is not None and hasattr(holder, "collision_material"):
                        try:
                            cm_code = compiler_bridge.get_surface_packed_code(getattr(holder, "collision_material", ""))
                            if is_do_not_align_surface_code(cm_code):
                                clean_cm = strip_do_not_align_code(cm_code, c_code)
                                holder.collision_material = f"material_{clean_cm:04d}"
                        except Exception:
                            pass

            # Clear Do-Not-Align codes on per-face painted surfaces (both visual and _COL)
            col_o = compiler_bridge.resolve_custom_collision_object(context, t_obj)
            for m_target in [o for o in (t_obj, col_o) if o is not None and getattr(o, "type", None) == 'MESH']:
                f_codes = compiler_bridge.get_mesh_face_surface_array(m_target)
                if f_codes and any(c > 0 and is_do_not_align_surface_code(c) for c in f_codes):
                    new_codes = [
                        (0 if is_do_not_align_surface_code(c) else int(c))
                        for c in f_codes
                    ]
                    me = m_target.data
                    if m_target.mode == 'EDIT':
                        bm = bmesh.from_edit_mesh(me)
                        layer = bm.faces.layers.int.get("reskate_face_surface")
                        if layer is not None:
                            for f in bm.faces:
                                if f.index < len(new_codes):
                                    f[layer] = int(new_codes[f.index])
                            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
                    else:
                        attr = me.attributes.get("reskate_face_surface")
                        if attr is not None:
                            for fi, item in enumerate(attr.data):
                                if fi < len(new_codes):
                                    item.value = int(new_codes[fi])
                    raw_leg = m_target.get("reskate_face_surface_legend_json", "")
                    leg_d = {}
                    if raw_leg:
                        try:
                            leg_d = {int(k): v for k, v in json.loads(str(raw_leg)).items()}
                        except Exception:
                            pass
                    _sync_face_surface_json_from_array(m_target, new_codes, leg_d)

            cleared_objs += 1

        if obj and prop:
            load_prop_settings_from_object(prop, obj)
        gpu_draw.invalidate_gpu_caches()
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Cleared 'Do Not Align' and restored skate/grind alignment on {cleared_objs} prop(s).")
        return {'FINISHED'}


class RESKATE_OT_clear_material_texture_slot(bpy.types.Operator):
    """Clears a texture image slot on the active material and updates the live shader node graph (keeping Procedural Wear intact)"""
    bl_idname = "reskate.clear_material_texture_slot"
    bl_label = "Clear Texture Slot"
    bl_description = "Clears this image texture slot on the active material while keeping Procedural Edge Wear & Starter Template settings intact"
    bl_options = {'REGISTER', 'UNDO'}

    slot_name: StringProperty(name="Slot Name", default="base_color_image")

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        mat = obj.active_material if (obj and obj.type == 'MESH') else getattr(context, "material", None)
        if mat is None and obj and obj.type == 'MESH' and len(obj.material_slots) > 0:
            mat = obj.material_slots[0].material
        if not mat:
            return {'CANCELLED'}

        attr = str(self.slot_name or "base_color_image")
        for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
            if holder is not None and hasattr(holder, attr):
                try:
                    setattr(holder, attr, None)
                except Exception:
                    pass

        ms = getattr(mat, "reskate_mat", None) or getattr(mat, "sk8_material", None)
        if hasattr(compiler_bridge, "sync_user_texture_slots_to_shader_nodes"):
            compiler_bridge.sync_user_texture_slots_to_shader_nodes(obj, mat, ms)
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Cleared '{attr}' on material '{mat.name}'.")
        return {'FINISHED'}


class RESKATE_OT_sync_quickdrop_live_bridge(bpy.types.Operator):
    bl_idname = "reskate.sync_quickdrop_live_bridge"
    bl_label = "Push Live Preview to Quick Drop Pro (R3)"
    bl_description = (
        "Instantly writes a live 3D wireframe preview and prop metadata to Quick Drop Pro's "
        "live_bridge.json (<50ms, no game restart required)"
    )

    def execute(self, context):
        import json
        from datetime import datetime, timezone

        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        if not sel_meshes:
            obj = get_target_mesh_object(context)
            if obj is not None:
                sel_meshes = [obj]
        if not sel_meshes:
            self.report({'WARNING'}, "ReSkate: Select at least one mesh object to push to Quick Drop Pro.")
            return {'CANCELLED'}

        game_dir = compiler_bridge.get_game_dir(context)
        bridge_dir = game_dir / "Mods" / ".reskate_plugin_data" / "ReSkate_QuickDropPro"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = bridge_dir / "live_bridge.json"

        existing = {}
        if bridge_path.is_file():
            try:
                existing = json.loads(bridge_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        wireframes = []
        for m_obj in sel_meshes[:8]:
            mesh_data = m_obj.data
            if mesh_data is None:
                continue
            mw = m_obj.matrix_world
            origin_w = mw.translation
            verts = mesh_data.vertices
            edges = mesh_data.edges
            total_e = len(edges)
            if total_e == 0:
                continue
            stride = max(1, total_e // 220)
            wf_edges = []
            for idx in range(0, total_e, stride):
                ed = edges[idx]
                v0 = (mw @ verts[ed.vertices[0]].co) - origin_w
                v1 = (mw @ verts[ed.vertices[1]].co) - origin_w
                # Convert Blender Z-up (x, y, z) -> Frostbite Y-up (x, z, -y)
                p0 = [round(float(v0.x), 4), round(float(v0.z), 4), round(float(-v0.y), 4)]
                p1 = [round(float(v1.x), 4), round(float(v1.z), 4), round(float(-v1.y), 4)]
                wf_edges.append([p0, p1])

            wireframes.append({
                "name": m_obj.name,
                "position": [round(float(origin_w.x), 4), round(float(origin_w.z), 4), round(float(-origin_w.y), 4)],
                "heading": 0.0,
                "scale": 1.0,
                "colour": "#5ecdfa",
                "edges": wf_edges,
            })

        payload = {
            "schema": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "variants": existing.get("variants") or {},
            "wireframe_previews": wireframes,
            "placements": existing.get("placements") or [],
            "apply_placements": False,
        }
        bridge_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.report({'INFO'}, f"ReSkate: Synced {len(wireframes)} prop wireframe(s) to Quick Drop Pro live_bridge.json!")
        return {'FINISHED'}


class RESKATE_OT_clear_quickdrop_live_wireframes(bpy.types.Operator):
    bl_idname = "reskate.clear_quickdrop_live_wireframes"
    bl_label = "Clear Live Wireframes"
    bl_description = "Clears any 3D Blender wireframe previews currently shown in Quick Drop Pro"

    def execute(self, context):
        import json
        from datetime import datetime, timezone

        game_dir = compiler_bridge.get_game_dir(context)
        bridge_path = game_dir / "Mods" / ".reskate_plugin_data" / "ReSkate_QuickDropPro" / "live_bridge.json"
        if bridge_path.is_file():
            try:
                data = json.loads(bridge_path.read_text(encoding="utf-8"))
                data["wireframe_previews"] = []
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                bridge_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass
        self.report({'INFO'}, "ReSkate: Cleared Quick Drop Pro live 3D wireframe previews.")
        return {'FINISHED'}


class RESKATE_OT_open_drop_glb_inbox(bpy.types.Operator):
    bl_idname = "reskate.open_drop_glb_inbox"
    bl_label = "Open Zero-Setup .glb Drop-In Inbox"
    bl_description = "Opens the QuickDrop_GLB_Inbox folder where any raw .glb file can be dropped for instant auto-compilation"

    def execute(self, context):
        import os
        inbox_dir = compiler_bridge.get_studio_dir(context) / "QuickDrop_GLB_Inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(inbox_dir))
        except Exception as err:
            self.report({'WARNING'}, f"Could not open folder: {err}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Opened QuickDrop_GLB_Inbox: {inbox_dir}")
        return {'FINISHED'}



# ==============================================================================
# Registration Routine
# ==============================================================================

class RESKATE_OT_install_python_deps(bpy.types.Operator):
    """Installs or verifies Pillow, trimesh, pygltflib, and scipy inside Blender's bundled Python via pip"""
    bl_idname = "reskate.install_python_deps"
    bl_label = "Verify / Install Python Libraries (pip)"
    bl_description = "Checks if Pillow, trimesh, and pygltflib are installed in Blender's Python and installs them automatically via pip"

    def execute(self, context):
        ok, msg = compiler_bridge.ensure_python_compiler_dependencies()
        if ok:
            self.report({'INFO'}, f"ReSkate: {msg}")
            return {'FINISHED'}
        self.report({'ERROR'}, f"ReSkate: Python dependency install failed: {msg}")
        return {'CANCELLED'}


class RESKATE_OT_bake_procedural_materials(bpy.types.Operator):
    """1-click Cycles baker that evaluates any procedural/node-graph material (Sanctus Library, Multiply/AO nodes, NodeGroups) and populates Prop Studio's Base Color, Roughness, Metallic, and Normal texture slots"""
    bl_idname = "reskate.bake_procedural_materials"
    bl_label = "Bake Procedural / Node Materials (Cycles)"
    bl_description = (
        "Switches temporarily to Cycles, bakes Base Color (including Multiply/AO/NodeGroups), Roughness, "
        "Metallic, and Normal into 100% opaque PNG textures on the mesh's existing UV map, and assigns them "
        "directly into Prop Studio's Material Texture Slots"
    )
    bl_options = {'REGISTER', 'UNDO'}

    active_slot_only: BoolProperty(
        name="Active Material Slot Only",
        default=False,
        description="If enabled, only bakes the currently selected material slot; otherwise bakes all procedural/node-graph slots on the active prop",
    )

    def execute(self, context):
        if not compiler_bridge.is_experimental_addon_enabled(context):
            self.report({'WARNING'}, "ReSkate: Built-in texture baker is archived in v0.31.0 (requires sk8_prop_experimental_addon). Author game-ready Principled BSDF materials directly.")
            return {'CANCELLED'}
        start_t = time.time()
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or not obj.data or len(obj.data.polygons) == 0:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop with a material first.")
            return {'CANCELLED'}

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        me = obj.data
        if len(me.uv_layers) == 0:
            me.uv_layers.new(name="UVMap")
            prev_act = context.view_layer.objects.active
            context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.015)
            bpy.ops.object.mode_set(mode='OBJECT')
            if prev_act and prev_act.name in bpy.data.objects:
                context.view_layer.objects.active = prev_act

        prop = getattr(context.scene, "reskate_prop", None)
        res_str = str(getattr(prop, "atlas_resolution", "2048") or "2048")
        try:
            resolution = max(512, min(4096, int(res_str)))
        except ValueError:
            resolution = 2048

        slug = get_prop_slug(context, obj)
        staging_dir = compiler_bridge.get_staging_dir(context)
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Ensure unique materials on `obj` before baking so shared materials aren't mutated
        compiler_bridge.ensure_all_object_materials_unique(obj)

        slot_indices = (
            [max(0, min(int(obj.active_material_index), len(obj.material_slots) - 1))]
            if (self.active_slot_only and len(obj.material_slots) > 0)
            else list(range(len(obj.material_slots)))
        )
        if not slot_indices:
            self.report({'WARNING'}, "ReSkate: No material slots found on this prop.")
            return {'CANCELLED'}

        scene = context.scene
        prev_engine = scene.render.engine
        prev_samples = getattr(scene.cycles, "samples", 16) if hasattr(scene, "cycles") else 16
        prev_margin = scene.render.bake.margin
        prev_sel = [o for o in context.selected_objects]
        prev_active = context.view_layer.objects.active

        for o in context.scene.objects:
            o.select_set(False)
        context.view_layer.objects.active = obj
        obj.select_set(True)

        scene.render.engine = 'CYCLES'
        if hasattr(scene, "cycles"):
            scene.cycles.samples = 1
        scene.render.bake.margin = 16
        scene.render.bake.margin_type = 'EXTEND'
        scene.render.bake.target = 'IMAGE_TEXTURES'

        baked_count = 0
        try:
            for s_idx in slot_indices:
                if s_idx < 0 or s_idx >= len(obj.material_slots):
                    continue
                mat = obj.material_slots[s_idx].material
                if not mat:
                    continue
                if not mat.use_nodes or not mat.node_tree:
                    mat.use_nodes = True

                nt = mat.node_tree
                nodes = nt.nodes
                links = nt.links

                # Remove any temporary ReSkate_Wear_* viewport preview nodes before baking the underlying material
                if hasattr(compiler_bridge, "remove_procedural_wear_nodes_from_material"):
                    try:
                        compiler_bridge.remove_procedural_wear_nodes_from_material(mat)
                    except Exception:
                        pass

                out_node = next((n for n in nodes if n.type == "OUTPUT_MATERIAL" and getattr(n, "is_active_output", True)), None)
                if out_node is None:
                    out_node = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None)
                if out_node is None or not out_node.inputs.get("Surface"):
                    continue

                orig_surf_sock = out_node.inputs["Surface"].links[0].from_socket if out_node.inputs["Surface"].is_linked else None
                p_node = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)

                # Helper to find BSDF sockets even when inside a top-level Sanctus/procedural NodeGroup
                group_node = (
                    orig_surf_sock.node
                    if (orig_surf_sock is not None and orig_surf_sock.node.type == "GROUP" and getattr(orig_surf_sock.node, "node_tree", None))
                    else None
                )

                safe_mat = re.sub(r"[^a-zA-Z0-9_]+", "_", mat.name).strip("_") or f"slot_{s_idx}"
                prefix = f"ReSkate_Baked_{slug}_{s_idx}_{safe_mat}"

                temp_emit = nodes.new("ShaderNodeEmission")
                temp_emit.name = "_ReSkate_Temp_Bake_Emit"
                temp_emit.inputs["Strength"].default_value = 1.0
                temp_tex = nodes.new("ShaderNodeTexImage")
                temp_tex.name = "_ReSkate_Temp_Bake_Tex"

                prev_act_node = nodes.active

                def _bake_pass_to_png(pass_suffix: str, bake_type: str, is_non_color: bool, source_sock=None, fallback_val=None) -> bpy.types.Image | None:
                    img_nm = f"{prefix}_{pass_suffix}"
                    old_im = bpy.data.images.get(img_nm)
                    if old_im is not None:
                        try:
                            bpy.data.images.remove(old_im)
                        except Exception:
                            pass
                    # Create with alpha=False so empty UV gutters are 100% opaque (alpha=1.0) and never trigger 0x273 cutout!
                    b_img = bpy.data.images.new(name=img_nm, width=resolution, height=resolution, alpha=False)
                    try:
                        b_img.colorspace_settings.name = 'Non-Color' if is_non_color else 'sRGB'
                    except Exception:
                        pass

                    temp_tex.image = b_img
                    for n in nodes:
                        n.select = False
                    temp_tex.select = True
                    nodes.active = temp_tex

                    if bake_type == 'EMIT':
                        for lk in list(temp_emit.inputs["Color"].links):
                            links.remove(lk)
                        if source_sock is not None:
                            links.new(source_sock, temp_emit.inputs["Color"])
                        elif fallback_val is not None:
                            if hasattr(fallback_val, "__len__") and len(fallback_val) >= 3:
                                temp_emit.inputs["Color"].default_value = (float(fallback_val[0]), float(fallback_val[1]), float(fallback_val[2]), 1.0)
                            else:
                                fv = float(fallback_val)
                                temp_emit.inputs["Color"].default_value = (fv, fv, fv, 1.0)
                        links.new(temp_emit.outputs["Emission"], out_node.inputs["Surface"])
                        bpy.ops.object.bake(type='EMIT')
                    elif bake_type == 'NORMAL':
                        if orig_surf_sock is not None:
                            links.new(orig_surf_sock, out_node.inputs["Surface"])
                        scene.render.bake.normal_space = 'TANGENT'
                        bpy.ops.object.bake(type='NORMAL')
                    elif bake_type == 'ROUGHNESS':
                        if orig_surf_sock is not None:
                            links.new(orig_surf_sock, out_node.inputs["Surface"])
                        bpy.ops.object.bake(type='ROUGHNESS')
                    elif bake_type == 'DIFFUSE':
                        if orig_surf_sock is not None:
                            links.new(orig_surf_sock, out_node.inputs["Surface"])
                        scene.render.bake.use_pass_direct = False
                        scene.render.bake.use_pass_indirect = False
                        scene.render.bake.use_pass_color = True
                        bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})

                    # Guarantee 100% opaque alpha (1.0) across all pixels including UV margins
                    try:
                        import numpy as np
                        px = np.empty(resolution * resolution * 4, dtype=np.float32)
                        b_img.pixels.foreach_get(px)
                        px[3::4] = 1.0
                        b_img.pixels.foreach_set(px)
                        b_img.update()
                    except Exception:
                        pass

                    out_path = staging_dir / f"{img_nm}.png"
                    b_img.filepath_raw = str(out_path)
                    b_img.file_format = 'PNG'
                    b_img.save()
                    return b_img

                temp_group_sockets: list[tuple] = []

                def _resolve_group_or_inner_bsdf_socket(
                    out_names: tuple[str, ...],
                    bsdf_input_name: str,
                    socket_type_rna: str,
                    temp_suffix: str,
                    default_fallback,
                ):
                    """Finds an exposed Group Output socket (like Sanctus 'Color Bake' / 'Metallic Bake') or promotes the inner Principled BSDF input through the Group Output interface."""
                    if group_node is None or getattr(group_node, "node_tree", None) is None:
                        return None, default_fallback
                    # 1. Check existing outputs on group_node (e.g. Sanctus 'Color Bake', 'Roughness Bake', 'Metallic Bake')
                    for gout in group_node.outputs:
                        if gout.name.strip().lower() in out_names:
                            return gout, default_fallback
                    # 2. Check inner Principled BSDF inside group_node.node_tree and promote its input to a temporary Group Output
                    gtree = group_node.node_tree
                    inner_p = next((n for n in gtree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                    group_out_node = next((n for n in gtree.nodes if n.type == "GROUP_OUTPUT"), None)
                    if inner_p is not None and bsdf_input_name in inner_p.inputs:
                        in_sock = inner_p.inputs[bsdf_input_name]
                        fb = default_fallback
                        if hasattr(in_sock, "default_value"):
                            dv = in_sock.default_value
                            fb = tuple(dv)[:3] if hasattr(dv, "__len__") else float(dv)
                        if in_sock.is_linked and group_out_node is not None and hasattr(gtree, "interface"):
                            tmp_nm = f"_reskate_tmp_{temp_suffix}"
                            try:
                                existing_item = gtree.interface.items_tree.get(tmp_nm)
                                if existing_item:
                                    gtree.interface.remove(existing_item)
                                gtree.interface.new_socket(name=tmp_nm, socket_type=socket_type_rna, in_out='OUTPUT')
                                temp_group_sockets.append((gtree, tmp_nm))
                                gtree.update_tag()
                                tmp_in = group_out_node.inputs.get(tmp_nm)
                                if tmp_in is not None:
                                    gtree.links.new(in_sock.links[0].from_socket, tmp_in)
                                    nt.update_tag()
                                    promoted_out = group_node.outputs.get(tmp_nm)
                                    if promoted_out is not None:
                                        return promoted_out, fb
                            except Exception:
                                pass
                        return None, fb
                    # 3. Fallback: check top-level group_node inputs
                    for gin in group_node.inputs:
                        if gin.name.strip().lower() in out_names:
                            src_s = gin.links[0].from_socket if gin.is_linked else None
                            fb = default_fallback
                            if hasattr(gin, "default_value"):
                                dv = gin.default_value
                                fb = tuple(dv)[:3] if hasattr(dv, "__len__") else float(dv)
                            return src_s, fb
                    return None, default_fallback

                try:
                    # 1. Base Color (via EMIT so Metallic=1.0 never blackens DIFFUSE pass!)
                    bc_src = None
                    bc_def = (0.78, 0.78, 0.78)
                    if p_node is not None and "Base Color" in p_node.inputs:
                        bc_in = p_node.inputs["Base Color"]
                        bc_src = bc_in.links[0].from_socket if bc_in.is_linked else None
                        bc_def = tuple(bc_in.default_value)[:3]
                    elif group_node is not None:
                        bc_src, bc_def = _resolve_group_or_inner_bsdf_socket(
                            ("color bake", "base color", "basecolor", "color", "albedo", "diffuse"),
                            "Base Color",
                            "NodeSocketColor",
                            "bc",
                            bc_def,
                        )

                    if bc_src is not None or p_node is not None or group_node is not None:
                        baked_bc = _bake_pass_to_png("BaseColor", 'EMIT', False, source_sock=bc_src, fallback_val=bc_def)
                    else:
                        baked_bc = _bake_pass_to_png("BaseColor", 'DIFFUSE', False)

                    # 2. Roughness
                    r_src = None
                    r_def = 0.45
                    if p_node is not None and "Roughness" in p_node.inputs:
                        r_in = p_node.inputs["Roughness"]
                        r_src = r_in.links[0].from_socket if r_in.is_linked else None
                        r_def = float(r_in.default_value)
                    elif group_node is not None:
                        r_src, r_def = _resolve_group_or_inner_bsdf_socket(
                            ("roughness bake", "roughness", "rough"),
                            "Roughness",
                            "NodeSocketFloat",
                            "rough",
                            r_def,
                        )
                    if r_src is not None or p_node is not None or group_node is not None:
                        baked_r = _bake_pass_to_png("Roughness", 'EMIT', True, source_sock=r_src, fallback_val=r_def)
                    else:
                        baked_r = _bake_pass_to_png("Roughness", 'ROUGHNESS', True)

                    # 3. Metallic
                    m_src = None
                    m_def = 0.0
                    if p_node is not None and "Metallic" in p_node.inputs:
                        m_in = p_node.inputs["Metallic"]
                        m_src = m_in.links[0].from_socket if m_in.is_linked else None
                        m_def = float(m_in.default_value)
                    elif group_node is not None:
                        m_src, m_def = _resolve_group_or_inner_bsdf_socket(
                            ("metallic bake", "metallic", "metalness", "metal"),
                            "Metallic",
                            "NodeSocketFloat",
                            "metal",
                            m_def,
                        )
                    baked_m = _bake_pass_to_png("Metallic", 'EMIT', True, source_sock=m_src, fallback_val=m_def)

                    # 4. Normal
                    baked_n = _bake_pass_to_png("Normal", 'NORMAL', True)

                finally:
                    for gtree_tmp, sock_nm in temp_group_sockets:
                        try:
                            item_rm = gtree_tmp.interface.items_tree.get(sock_nm)
                            if item_rm is not None:
                                gtree_tmp.interface.remove(item_rm)
                        except Exception:
                            pass
                    if orig_surf_sock is not None:
                        links.new(orig_surf_sock, out_node.inputs["Surface"])
                    for tn in (temp_emit, temp_tex):
                        try:
                            nodes.remove(tn)
                        except Exception:
                            pass
                    if prev_act_node and prev_act_node.name in nodes:
                        nodes.active = prev_act_node

                # Assign baked maps directly to Prop Studio's Texture Slots (`sk8_material` & `reskate_mat`) with override enabled!
                for holder in (getattr(mat, "sk8_material", None), getattr(mat, "reskate_mat", None)):
                    if holder is None:
                        continue
                    try:
                        if hasattr(holder, "use_custom_texture_overrides"):
                            holder.use_custom_texture_overrides = True
                        if hasattr(holder, "show_texture_slots"):
                            holder.show_texture_slots = True
                        if baked_bc is not None:
                            holder.base_color_image = baked_bc
                        if baked_n is not None:
                            holder.normal_image = baked_n
                        if baked_r is not None:
                            holder.roughness_image = baked_r
                            if hasattr(holder, "roughness_channel"):
                                holder.roughness_channel = 'Red'
                        if baked_m is not None:
                            holder.metallic_image = baked_m
                            if hasattr(holder, "metallic_channel"):
                                holder.metallic_channel = 'Red'
                    except Exception:
                        pass

                baked_count += 1
        except Exception as ex:
            self.report({'ERROR'}, f"ReSkate: Cycles material bake failed: {ex}")
            return {'CANCELLED'}
        finally:
            scene.render.engine = prev_engine
            if hasattr(scene, "cycles"):
                scene.cycles.samples = prev_samples
            scene.render.bake.margin = prev_margin
            for o in context.scene.objects:
                o.select_set(False)
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            if prev_active and prev_active.name in context.scene.objects:
                context.view_layer.objects.active = prev_active

        compiler_bridge.invalidate_ui_caches()
        elapsed = time.time() - start_t
        self.report(
            {'INFO'},
            f"ReSkate: Baked {baked_count} material slot(s) ({resolution}x{resolution} Base Color, Roughness, Metallic, Normal) into Prop Studio texture slots in {elapsed:.1f}s.",
        )
        return {'FINISHED'}


class RESKATE_OT_toggle_pbr_neutral_viewport(bpy.types.Operator):
    """Toggles Khronos PBR Neutral color management and Material Preview shading in the 3D Viewport"""
    bl_idname = "reskate.toggle_pbr_neutral_viewport"
    bl_label = "Toggle PBR Neutral Viewport Preview"
    bl_description = "Switches Blender's View Transform between Khronos PBR Neutral (true-to-game Skate PBR colors) and Standard/AgX, and activates Material Preview shading"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        vs = getattr(scene, "view_settings", None)
        chosen_vt = "Standard"
        if vs is not None:
            cur = str(getattr(vs, "view_transform", "Standard") or "Standard")
            if "PBR Neutral" in cur:
                for cand in ("AgX", "Filmic", "Standard"):
                    try:
                        vs.view_transform = cand
                        chosen_vt = cand
                        break
                    except Exception:
                        continue
            else:
                for cand in ("Khronos PBR Neutral", "PBR Neutral", "Standard"):
                    try:
                        vs.view_transform = cand
                        chosen_vt = cand
                        break
                    except Exception:
                        continue

        # Also switch active 3D viewports to Material Preview if currently in Solid mode
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D' and hasattr(space, "shading"):
                            if space.shading.type == 'SOLID':
                                space.shading.type = 'MATERIAL'

        self.report({'INFO'}, f"ReSkate: Viewport Color Management set to '{chosen_vt}'.")
        return {'FINISHED'}


class RESKATE_OT_send_to_game(bpy.types.Operator):
    """Copies the compiled .fbmod pack into the game's Frosty Mods folder (or compiles & deploys if not built yet)"""
    bl_idname = "reskate.send_to_game"
    bl_label = "Send to Game (Frosty Mods)"
    bl_description = "Deploys the compiled .fbmod prop pack directly to your Skate Frosty Mods folder (automatically compiles first if not yet built)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}

        target_mod = compiler_bridge.get_resolved_target_mod_name(context, prop)
        if not target_mod.lower().endswith(".fbmod"):
            fbmod_filename = f"{target_mod}.fbmod"
        else:
            fbmod_filename = target_mod

        staging_dir = compiler_bridge.get_staging_dir(context)
        mods_dir = Path(prop.mods_dir) if getattr(prop, "mods_dir", "") else compiler_bridge.get_game_mods_dir(context)

        # Look for an already-compiled .fbmod in Mods dir or Staging Packs dir
        dest_fbmod = mods_dir / fbmod_filename
        staging_candidates = [
            staging_dir / "Packs" / target_mod.replace(".fbmod", "") / fbmod_filename,
            staging_dir / fbmod_filename,
        ]
        src_fbmod = next((p for p in staging_candidates if p.is_file()), None)

        if src_fbmod is not None:
            try:
                mods_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_fbmod, dest_fbmod)
                self.report({'INFO'}, f"ReSkate: Deployed '{fbmod_filename}' to '{mods_dir}'!")
                return {'FINISHED'}
            except Exception as exc:
                self.report({'WARNING'}, f"ReSkate: Failed copying '{fbmod_filename}': {exc}")

        if dest_fbmod.is_file():
            self.report({'INFO'}, f"ReSkate: '{fbmod_filename}' is already deployed in '{mods_dir}' ({dest_fbmod.stat().st_size // 1024:,} KB).")
            return {'FINISHED'}

        # Not compiled yet -> trigger Compile & Deploy directly!
        return bpy.ops.reskate.compile_and_deploy('INVOKE_DEFAULT')


class RESKATE_OT_open_addon_preferences(bpy.types.Operator):
    """Opens Blender Preferences directly to the ReSkate Prop Studio Add-on settings"""
    bl_idname = "reskate.open_addon_preferences"
    bl_label = "Open ReSkate Add-on Preferences"
    bl_description = "Opens Blender Preferences directly to ReSkate Prop Studio where you can configure Author Name, Skate. Game Folder, Staging Folder, and compiler settings"
    bl_options = {'REGISTER'}

    def execute(self, context):
        mod_name = __package__ or "sk8_prop_authoring_addon"
        try:
            bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
        except Exception:
            pass
        try:
            if hasattr(context, "preferences") and context.preferences:
                context.preferences.active_section = 'ADDONS'
        except Exception:
            pass
        try:
            bpy.ops.preferences.addon_show(module=mod_name)
        except Exception:
            pass
        self.report({'INFO'}, "ReSkate: Opened Add-on Preferences (Edit -> Preferences -> Add-ons -> ReSkate Prop Studio).")
        return {'FINISHED'}


class RESKATE_OT_clear_compiler_cache(bpy.types.Operator):
    """Clears all cached Havok physics hulls, BC7 mipchains, Oodle CAS blocks, base-game donor templates, and per-prop .pkl caches in the Staging folder"""
    bl_idname = "reskate.clear_compiler_cache"
    bl_label = "Clear Compiler Build Cache"
    bl_description = "Deletes cached Havok physics hulls, BC7 textures, Oodle CAS blocks, and per-prop .pkl build caches in the Staging directory so the next compile rebuilds from scratch"
    bl_options = {'REGISTER'}

    def execute(self, context):
        staging_dir = compiler_bridge.get_staging_dir(context)
        removed_files = 0
        removed_bytes = 0

        cache_dirs = [
            staging_dir / ".compiler_cache",
            staging_dir / ".prop_compile_cache",
        ]
        packs_root = staging_dir / "Packs"
        if packs_root.is_dir():
            for pack_sub in packs_root.iterdir():
                if pack_sub.is_dir():
                    c_dir = pack_sub / "cache"
                    if c_dir.is_dir():
                        cache_dirs.append(c_dir)

        for c_dir in cache_dirs:
            if c_dir.is_dir():
                for root_p, _dirs, files in os.walk(c_dir):
                    for fn in files:
                        fp = Path(root_p) / fn
                        try:
                            removed_bytes += fp.stat().st_size
                            removed_files += 1
                        except Exception:
                            pass
                try:
                    shutil.rmtree(c_dir, ignore_errors=True)
                except Exception:
                    pass

        pack_queue = staging_dir / "Pack_Queue"
        if pack_queue.is_dir():
            for fp in pack_queue.glob("*_pbr_cache.json"):
                try:
                    removed_bytes += fp.stat().st_size
                    fp.unlink()
                    removed_files += 1
                except Exception:
                    pass

        compiler_bridge.invalidate_ui_caches()
        mb = removed_bytes / (1024.0 * 1024.0)
        self.report(
            {'INFO'},
            f"ReSkate: Cleared {removed_files} cached compiler file(s) ({mb:.2f} MB) from '{staging_dir}'.",
        )
        return {'FINISHED'}


def _build_real_collision_and_pphys_for_check_collision(
    context,
    obj: bpy.types.Object,
    prop,
    solidify_thickness_m: float = 0.0,
) -> tuple[bpy.types.Object | None, bpy.types.Object | None]:
    """
    Modular helper for `Check Collision` and `Thicken Collision Proxy` (Item 3).
    1. Builds/updates the real Quick Drop Placement Proxy mesh `<Prop>_PPHYS` using
       `RESKATE_OT_generate_placement_proxy._build_tight_pphys_bmesh`.
    2. Builds/updates the real `<Prop>_COL` collision proxy mesh (excluding Decal/Invisible slots),
       and when `solidify_thickness_m > 0.0` (or when thin/single-sided staircase treads are thickened),
       solidifies thin/single-sided faces by `solidify_thickness_m` (default 2.5 cm downward/inward)
       so skater wheels never clip through thin or single-sided steps.
    """
    if not obj or getattr(obj, "type", None) != 'MESH' or not getattr(obj, "data", None):
        return None, None

    prev_active = context.view_layer.objects.active
    col_collection = obj.users_collection[0] if obj.users_collection else context.collection

    # 1. Build / update <Prop>_PPHYS placement proxy
    pphys_obj = None
    col_mode = str(getattr(prop, "collision_mode", "SAME_AS_MESH") or "SAME_AS_MESH").upper() if prop else "SAME_AS_MESH"
    no_col_place = str(getattr(prop, "no_collision_placement_mode", "FLAT_PLANE") or "FLAT_PLANE").upper() if prop else "FLAT_PLANE"
    try:
        target_pphys_tris = int(getattr(prop, "pphys_target_tris", 64) or 64) if prop else 64
        pphys_name = f"{obj.name}_PPHYS"
        pphys_obj = compiler_bridge.resolve_custom_placement_proxy_object(context, obj)
        if pphys_obj is None or pphys_obj == obj:
            pphys_obj = context.scene.objects.get(pphys_name) or bpy.data.objects.get(pphys_name)

        if col_mode == "NO_COLLISION" and no_col_place == "FLAT_PLANE":
            # Build flat 6cm ground placement box spanning the prop's X/Y footprint
            xs = [float(v.co.x) for v in obj.data.vertices] or [-0.5, 0.5]
            ys = [float(v.co.y) for v in obj.data.vertices] or [-0.5, 0.5]
            zs = [float(v.co.z) for v in obj.data.vertices] or [0.0]
            min_x, max_x = min(xs), max(max(xs), min(xs) + 0.10)
            min_y, max_y = min(ys), max(max(ys), min(ys) + 0.10)
            min_z = min(zs)
            bm_p = bmesh.new()
            bmesh.ops.create_cube(bm_p, size=1.0)
            for v in bm_p.verts:
                v.co.x = min_x if v.co.x < 0 else max_x
                v.co.y = min_y if v.co.y < 0 else max_y
                v.co.z = min_z if v.co.z < 0 else (min_z + 0.060)
            bmesh.ops.triangulate(bm_p, faces=bm_p.faces[:])
            bm_p.normal_update()
        else:
            bm_p, _n_isl = RESKATE_OT_generate_placement_proxy._build_tight_pphys_bmesh(
                obj, target_tris=target_pphys_tris
            )
        pphys_me = bpy.data.meshes.new(f"{pphys_name}_Mesh")
        bm_p.to_mesh(pphys_me)
        bm_p.free()

        if pphys_obj is not None and pphys_obj.name in bpy.data.objects and pphys_obj != obj:
            old_p_me = pphys_obj.data
            pphys_obj.data = pphys_me
            if old_p_me and old_p_me.users == 0:
                bpy.data.meshes.remove(old_p_me)
            if pphys_obj.name not in context.scene.objects:
                col_collection.objects.link(pphys_obj)
        else:
            pphys_obj = bpy.data.objects.new(pphys_name, pphys_me)
            col_collection.objects.link(pphys_obj)

        pphys_obj.parent = obj
        pphys_obj.matrix_parent_inverse = Matrix.Identity(4)
        pphys_obj.location = (0.0, 0.0, 0.0)
        pphys_obj.rotation_euler = (0.0, 0.0, 0.0)
        pphys_obj.scale = (1.0, 1.0, 1.0)
        pphys_obj.display_type = 'WIRE'
        pphys_obj.hide_render = True
        pphys_obj.hide_select = True
        pphys_obj["reskate_is_pphys_proxy"] = True
        pphys_obj["reskate_col_owner"] = obj.name
        obj.reskate_custom_placement_proxy_object = pphys_obj
    except Exception:
        pphys_obj = None

    # 2. Build / update <Prop>_COL collision proxy
    col_obj = None
    try:
        col_name = f"{obj.name}_COL"
        existing_col = compiler_bridge.resolve_custom_collision_object(context, obj)
        if existing_col is not None and existing_col != obj and col_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL', 'CUSTOM_COL') and solidify_thickness_m <= 0.0:
            col_obj = existing_col
        elif col_mode == "NO_COLLISION":
            # Build thin 7mm ground wafer so skater collision is disabled while Quick Drop selection raycast still works
            xs = [float(v.co.x) for v in obj.data.vertices] or [-0.5, 0.5]
            ys = [float(v.co.y) for v in obj.data.vertices] or [-0.5, 0.5]
            zs = [float(v.co.z) for v in obj.data.vertices] or [0.0]
            min_x, max_x = min(xs), max(max(xs), min(xs) + 0.10)
            min_y, max_y = min(ys), max(max(ys), min(ys) + 0.10)
            min_z = min(zs)
            bm_c = bmesh.new()
            bmesh.ops.create_cube(bm_c, size=1.0)
            for v in bm_c.verts:
                v.co.x = min_x if v.co.x < 0 else max_x
                v.co.y = min_y if v.co.y < 0 else max_y
                v.co.z = (min_z - 0.005) if v.co.z < 0 else (min_z + 0.002)
            bmesh.ops.triangulate(bm_c, faces=bm_c.faces[:])
            bm_c.normal_update()
            col_me = bpy.data.meshes.new(f"{col_name}_Mesh")
            bm_c.to_mesh(col_me)
            bm_c.free()
            col_me.update()

            col_obj = context.scene.objects.get(col_name) or bpy.data.objects.get(col_name)
            if col_obj is not None and col_obj != obj:
                old_c_me = col_obj.data
                col_obj.data = col_me
                if old_c_me and old_c_me.users == 0:
                    bpy.data.meshes.remove(old_c_me)
                if col_obj.name not in context.scene.objects:
                    col_collection.objects.link(col_obj)
            else:
                col_obj = bpy.data.objects.new(col_name, col_me)
                col_collection.objects.link(col_obj)

            col_obj.parent = obj
            col_obj.matrix_parent_inverse = Matrix.Identity(4)
            col_obj.location = (0.0, 0.0, 0.0)
            col_obj.rotation_euler = (0.0, 0.0, 0.0)
            col_obj.scale = (1.0, 1.0, 1.0)
            col_obj.display_type = 'WIRE'
            col_obj.show_wire = True
            col_obj.show_all_edges = True
            col_obj.hide_render = True
            col_obj.hide_select = True
            col_obj["reskate_col_owner"] = obj.name
            if hasattr(col_obj, "reskate_col_prop"):
                col_obj.reskate_col_prop.is_col_proxy = True
            if hasattr(obj, "reskate_custom_collision_object"):
                obj.reskate_custom_collision_object = col_obj
        else:
            decal_slots: set[int] = set()
            for s_idx, slot in enumerate(obj.material_slots):
                m = slot.material
                if m is not None:
                    sk8_m = getattr(m, "reskate_mat", None) or getattr(m, "sk8_material", None)
                    if sk8_m is not None and (
                        bool(getattr(sk8_m, "is_decal_slot", False))
                        or bool(getattr(sk8_m, "is_decal_overlay", False))
                        or str(getattr(sk8_m, "surface_role_preset", "")) == "DECAL_OVERLAY"
                        or bool(getattr(sk8_m, "invisible", False))
                    ):
                        decal_slots.add(s_idx)

            bm_c = bmesh.new()
            bm_c.from_mesh(obj.data)
            bm_c.verts.ensure_lookup_table()
            bm_c.edges.ensure_lookup_table()
            bm_c.faces.ensure_lookup_table()

            if decal_slots and len(bm_c.faces) > 0:
                non_dec = [f for f in bm_c.faces if int(f.material_index) not in decal_slots]
                if non_dec and len(non_dec) < len(bm_c.faces):
                    del_f = [f for f in bm_c.faces if int(f.material_index) in decal_slots]
                    bmesh.ops.delete(bm_c, geom=del_f, context='FACES')
                    lone_v = [v for v in bm_c.verts if not v.link_faces]
                    if lone_v:
                        bmesh.ops.delete(bm_c, geom=lone_v, context='VERTS')

            bmesh.ops.remove_doubles(bm_c, verts=bm_c.verts[:], dist=0.0005)

            if solidify_thickness_m > 0.0 and len(bm_c.faces) > 0:
                # Solidify open / thin faces downward/inward (-thickness) so top skateable step surfaces stay at exact height
                bmesh.ops.solidify(bm_c, geom=bm_c.faces[:], thickness=-float(solidify_thickness_m))
                bmesh.ops.recalc_face_normals(bm_c, faces=bm_c.faces[:])
                obj["reskate_col_thickened"] = True
                obj["reskate_col_thickened_m"] = float(solidify_thickness_m)

            if col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL', 'AUTO_CONVEX') and len(bm_c.faces) > 12:
                try:
                    bmesh.ops.dissolve_limit(
                        bm_c,
                        angle_limit=math.radians(6.0),
                        use_dissolve_boundaries=False,
                        verts=bm_c.verts[:],
                        edges=bm_c.edges[:],
                        delimit={'MATERIAL', 'SHARP'},
                    )
                except Exception:
                    pass

            bmesh.ops.triangulate(bm_c, faces=bm_c.faces[:])
            bm_c.normal_update()

            col_me = bpy.data.meshes.new(f"{col_name}_Mesh")
            for mat_slot in obj.material_slots:
                col_me.materials.append(mat_slot.material)
            bm_c.to_mesh(col_me)
            bm_c.free()
            col_me.update()

            col_obj = context.scene.objects.get(col_name) or bpy.data.objects.get(col_name)
            if col_obj is not None and col_obj != obj:
                old_c_me = col_obj.data
                col_obj.data = col_me
                if old_c_me and old_c_me.users == 0:
                    bpy.data.meshes.remove(old_c_me)
                if col_obj.name not in context.scene.objects:
                    col_collection.objects.link(col_obj)
            else:
                col_obj = bpy.data.objects.new(col_name, col_me)
                col_collection.objects.link(col_obj)

            col_obj.parent = obj
            col_obj.matrix_parent_inverse = Matrix.Identity(4)
            col_obj.location = (0.0, 0.0, 0.0)
            col_obj.rotation_euler = (0.0, 0.0, 0.0)
            col_obj.scale = (1.0, 1.0, 1.0)
            col_obj.display_type = 'WIRE'
            col_obj.show_wire = True
            col_obj.show_all_edges = True
            col_obj.hide_render = True
            col_obj.hide_select = True
            col_obj["reskate_col_owner"] = obj.name
            if hasattr(col_obj, "reskate_col_prop"):
                col_obj.reskate_col_prop.is_col_proxy = True
            if hasattr(obj, "reskate_custom_collision_object"):
                obj.reskate_custom_collision_object = col_obj
            if prop and solidify_thickness_m > 0.0:
                prop.custom_collision_object = col_obj
    except Exception:
        col_obj = None

    if prev_active and prev_active.name in bpy.data.objects:
        context.view_layer.objects.active = prev_active
    return col_obj, pphys_obj


class RESKATE_OT_thicken_collision_proxy(bpy.types.Operator):
    """Solidifies thin or single-sided collision faces on <Prop>_COL (+2.5 cm) so skater wheels never clip through thin panels or spiral staircase steps"""
    bl_idname = "reskate.thicken_collision_proxy"
    bl_label = "Thicken Thin Collision Faces (+2.5 cm)"
    bl_description = (
        "Builds or updates <Prop>_COL with 2.5 cm of solid underside thickness on thin/single-sided faces "
        "(such as spiral staircase steps or sheet metal panels) without altering your visual mesh"
    )
    bl_options = {'REGISTER', 'UNDO'}

    thickness_m: FloatProperty(
        name="Solidify Thickness (m)",
        default=0.025,
        min=0.01,
        max=0.20,
        unit='LENGTH',
    )

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or not obj.data:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}
        prop = getattr(context.scene, "reskate_prop", None)
        col_obj, _pphys_obj = _build_real_collision_and_pphys_for_check_collision(
            context, obj, prop, solidify_thickness_m=float(self.thickness_m)
        )
        bpy.ops.reskate.check_collision()
        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        if col_obj is not None:
            self.report(
                {'INFO'},
                f"ReSkate: Solidified '{col_obj.name}' by {self.thickness_m * 100.0:.1f} cm — thin collision warning resolved.",
            )
        return {'FINISHED'}


class RESKATE_OT_check_collision(bpy.types.Operator):
    """Generates/updates real <Prop>_COL & <Prop>_PPHYS proxies, checks for thin/single-sided or concave staircase geometry, and turns on both Collision & Placement Proxy overlays"""
    bl_idname = "reskate.check_collision"
    bl_label = "Check Collision"
    bl_description = (
        "Generates/updates the real Collision (<Prop>_COL) and Placement Proxy (<Prop>_PPHYS) meshes, "
        "checks for thin/single-sided or concave staircase geometry, and turns on both Collision & Placement Proxy overlays"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or not obj.data:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}

        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}

        col_mode = getattr(prop, "collision_mode", "SAME_AS_MESH")
        thin_info = compiler_bridge.check_mesh_thin_or_single_sided_faces(obj, context=context)

        # Build/update real <Prop>_COL and <Prop>_PPHYS scene proxies (Item 3)
        saved_thick_m = float(obj.get("reskate_col_thickened_m", 0.0) or 0.0) if bool(obj.get("reskate_col_thickened", False)) else 0.0
        gen_col_obj, gen_pphys_obj = _build_real_collision_and_pphys_for_check_collision(
            context, obj, prop, solidify_thickness_m=saved_thick_m
        )

        # Compute local mesh metrics & warning flags
        bmin_w, bmax_w = compiler_bridge.get_world_bounds_zup(obj)
        dims = (
            max(0.0, float(bmax_w.x - bmin_w.x)),
            max(0.0, float(bmax_w.y - bmin_w.y)),
            max(0.0, float(bmax_w.z - bmin_w.z)),
        )
        min_dim_m = float(thin_info.get("min_dim_m", min(dims) if any(d > 0.0 for d in dims) else 0.0))

        col_obj = gen_col_obj or obj
        if col_mode in ('CUSTOM_OBJECT', 'AUTHORED_COL', 'CUSTOM_COL'):
            cand_col = compiler_bridge.resolve_custom_collision_object(context, obj) or getattr(prop, "custom_collision_object", None)
            if cand_col is not None:
                col_obj = cand_col
        elif col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL', 'AUTO_CONVEX'):
            cand_col = compiler_bridge.resolve_custom_collision_object(context, obj)
            if cand_col is not None:
                col_obj = cand_col

        # Evaluate mesh triangles excluding Decal / Invisible material slots
        decal_slots: set[int] = set()
        if obj.material_slots:
            for s_idx, slot in enumerate(obj.material_slots):
                m = slot.material
                if m is not None:
                    sk8_m = getattr(m, "reskate_mat", None) or getattr(m, "sk8_material", None)
                    if sk8_m is not None and (
                        bool(getattr(sk8_m, "is_decal_slot", False))
                        or str(getattr(sk8_m, "surface_role_preset", "")) == "DECAL_OVERLAY"
                        or bool(getattr(sk8_m, "invisible", False))
                    ):
                        decal_slots.add(s_idx)

        eval_tris = 0
        decal_tris = 0
        for p in obj.data.polygons:
            t_cnt = max(1, len(p.vertices) - 2)
            if int(p.material_index) in decal_slots:
                decal_tris += t_cnt
            else:
                eval_tris += t_cnt
        if col_obj and col_obj != obj and col_obj.data:
            eval_tris = sum(max(1, len(p.vertices) - 2) for p in col_obj.data.polygons)

        pphys_tris = (
            sum(max(1, len(p.vertices) - 2) for p in gen_pphys_obj.data.polygons)
            if (gen_pphys_obj and gen_pphys_obj.data)
            else 0
        )

        max_hulls = int(getattr(prop, "coacd_max_hulls", 8) or 8)
        if col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL', 'AUTO_CONVEX'):
            shape_count = min(max_hulls, max(1, (eval_tris + 31) // 32))
        else:
            shape_count = 1
        col_tris = eval_tris

        # Clear any stale raw JSON lines so `draw_col_overlay` uses the fast cached Green/Red per-slot batches!
        if "reskate_col_check_preview_json" in obj:
            try:
                del obj["reskate_col_check_preview_json"]
            except Exception:
                pass

        warnings_list = []
        if col_mode != 'NO_COLLISION':
            if thin_info.get("has_concave_step_risk") and col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL', 'AUTO_CONVEX'):
                warnings_list.append(
                    "Multi-step / spiral staircase geometry detected in Simple Hulls — use Same as Mesh so convex hulls do not bridge across concave steps."
                )
            if col_mode == 'SAME_AS_MESH' and col_tris > 8000:
                warnings_list.append(
                    f"Too many triangles ({col_tris:,} > 8,000 in Same as Mesh) — switch to Simple Hulls or decimate."
                )
            if col_mode in ('SIMPLE_HULLS', 'CONVEX_HULL', 'AUTO_CONVEX') and shape_count >= max_hulls and eval_tris > 500:
                warnings_list.append(
                    f"Using {shape_count}/{max_hulls} convex hulls — use Custom Object collision if concave pockets need tighter fit."
                )

        summary_data = {
            "mode": col_mode,
            "no_collision_placement_mode": str(getattr(prop, "no_collision_placement_mode", "FLAT_PLANE") or "FLAT_PLANE"),
            "shape_count": shape_count,
            "triangle_count": col_tris,
            "pphys_triangle_count": pphys_tris,
            "col_object_name": col_obj.name if col_obj else "",
            "col_proxy_name": col_obj.name if col_obj else "",
            "pphys_object_name": gen_pphys_obj.name if gen_pphys_obj else "",
            "pphys_proxy_name": gen_pphys_obj.name if gen_pphys_obj else "",
            "decal_excluded_tris": decal_tris,
            "excluded_decal_triangles": decal_tris,
            "min_dim_m": round(min_dim_m, 4),
            "is_too_thin": bool(thin_info.get("is_too_thin", False)),
            "has_thin_faces": bool(thin_info.get("is_too_thin", False)),
            "has_concave_step_risk": bool(thin_info.get("has_concave_step_risk", False)),
            "col_thickened": bool(thin_info.get("col_thickened", False)),
            "warnings": warnings_list,
            "ok": True,
        }
        try:
            obj["reskate_col_check_status"] = "WARN" if warnings_list else "PASS"
            obj["reskate_col_check_summary"] = (
                f"{shape_count} shape(s), {col_tris:,} col tris"
                + (f", {pphys_tris:,} proxy tris" if pphys_tris > 0 else "")
                + (f" ({decal_tris:,} decal tris excluded)" if decal_tris > 0 else "")
                + (f" ({len(warnings_list)} warning(s))" if warnings_list else "")
            )
            obj["reskate_col_check_summary_json"] = json.dumps(summary_data)
        except Exception:
            pass

        # Enable BOTH Collision Preview overlay AND Placement Proxy overlay immediately (Item 3)
        try:
            prop.draw_col_overlay = True
            prop.show_collision_overlay = True
            prop.show_placement_proxy_overlay = True
        except Exception:
            pass
        try:
            context.scene.reskate_show_col_overlay = True
        except Exception:
            pass
        gpu_draw.sync_draw_handlers(context)
        gpu_draw.invalidate_gpu_caches()

        if warnings_list:
            self.report({'WARNING'}, f"Collision checked ({shape_count} shape(s), {col_tris:,} tris, {pphys_tris:,} proxy tris) — {len(warnings_list)} warning(s).")
        else:
            self.report({'INFO'}, f"Collision checked: {shape_count} shape(s), {col_tris:,} col tris, {pphys_tris:,} proxy tris — Ready!")
        return {'FINISHED'}


_LAST_HIGHLIGHTED_EDIT_SLOT: tuple[str, int] | None = None


class RESKATE_OT_highlight_surface_material(bpy.types.Operator):
    """Switches to Edit Mode (Face Select) and selects all faces assigned to this material slot (click again on the same slot to return to Object Mode)"""
    bl_idname = "reskate.highlight_surface_material"
    bl_label = "Select Material"
    bl_description = (
        "Selects all faces assigned to this material slot in Edit Mode (Face Select) so you can see or edit them "
        "with zero GPU overlay overhead — click again on the same slot to return to Object Mode"
    )
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(
        name="Material Slot Index",
        default=0,
        min=0,
        options={'SKIP_SAVE'},
    )
    enter_edit_mode_select: BoolProperty(
        name="Select Faces in Edit Mode",
        default=True,
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        global _LAST_HIGHLIGHTED_EDIT_SLOT
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH' or not obj.data:
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}

        idx = int(self.slot_index)
        n_slots = len(obj.material_slots)
        clamped_idx = max(0, min(idx, n_slots - 1)) if n_slots > 0 else 0
        if n_slots > 0 and getattr(obj, "active_material_index", 0) != clamped_idx:
            obj.active_material_index = clamped_idx

        compiler_bridge.invalidate_ui_caches()

        # If clicking the left material slot name button while in Object Mode, just switch the active shader slot preview
        if not self.enter_edit_mode_select and getattr(obj, "mode", "OBJECT") != 'EDIT':
            return {'FINISHED'}

        # If clicking 'Highlight Surfaces' again on the same slot while already in Edit Mode, toggle back to Object Mode
        if (
            self.enter_edit_mode_select
            and getattr(obj, "mode", "OBJECT") == 'EDIT'
            and _LAST_HIGHLIGHTED_EDIT_SLOT == (obj.name, clamped_idx)
        ):
            if bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')
            _LAST_HIGHLIGHTED_EDIT_SLOT = None
            self.report({'INFO'}, "ReSkate: Returned to Object Mode.")
            return {'FINISHED'}

        # Ensure target object is selected and active before entering Edit Mode
        try:
            if getattr(context, "mode", "OBJECT") != 'OBJECT' and getattr(context, "active_object", None) != obj:
                if bpy.ops.object.mode_set.poll():
                    bpy.ops.object.mode_set(mode='OBJECT')
            if not obj.select_get():
                obj.select_set(True)
            context.view_layer.objects.active = obj
            if getattr(obj, "mode", "OBJECT") != 'EDIT' and bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='EDIT')
        except Exception as exc:
            self.report({'WARNING'}, f"ReSkate: Could not enter Edit Mode on '{obj.name}' ({exc}).")
            return {'CANCELLED'}

        try:
            ts = getattr(context, "tool_settings", None) or getattr(context.scene, "tool_settings", None)
            if ts is not None:
                ts.mesh_select_mode = (False, False, True)
        except Exception:
            pass

        sel_faces = 0
        try:
            bm = bmesh.from_edit_mesh(obj.data)
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            for f in bm.faces:
                match = (int(f.material_index) == clamped_idx) if n_slots > 0 else True
                f.select = match
                if match:
                    sel_faces += 1
                    for e in f.edges:
                        e.select = True
                    for v in f.verts:
                        v.select = True
            bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except Exception:
            try:
                bpy.ops.mesh.select_all(action='DESELECT')
                if n_slots > 0:
                    bpy.ops.object.material_slot_select()
                else:
                    bpy.ops.mesh.select_all(action='SELECT')
            except Exception:
                pass

        _LAST_HIGHLIGHTED_EDIT_SLOT = (obj.name, clamped_idx)
        mat_name = (
            obj.material_slots[clamped_idx].material.name
            if (n_slots > 0 and obj.material_slots[clamped_idx].material is not None)
            else f"Slot {clamped_idx + 1}"
        )
        if context.screen:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        self.report(
            {'INFO'},
            f"ReSkate: Selected {sel_faces:,} face(s) for '{mat_name}' in Edit Mode — click 'Exit Select' to return to Object Mode.",
        )
        return {'FINISHED'}


class RESKATE_OT_carousel_step(bpy.types.Operator):
    """Switches which selected prop is currently active in the multi-prop carousel without clearing the selection"""
    bl_idname = "reskate.carousel_step"
    bl_label = "Switch Selected Prop"
    bl_description = "Cycles which selected prop is currently being edited while keeping all selected props in your selection"
    bl_options = {'REGISTER'}

    direction: IntProperty(
        name="Direction",
        default=1,
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        sel_meshes = compiler_bridge.get_selected_prop_meshes(context)
        if len(sel_meshes) < 2:
            # If only 1 (or 0) prop is selected, cycle through all valid prop meshes in the scene so ◀/▶ always works!
            scene_meshes = [
                o for o in context.scene.objects
                if getattr(o, "type", None) == 'MESH'
                and o.data
                and len(o.data.polygons) > 0
                and not is_collision_helper_object(o)
                and not bool(o.get("reskate_is_scale_reference", False))
                and not bool(o.get("reskate_is_ref_helper", False))
                and not o.name.upper().startswith(("RESKATE_REF_", "RESKATE_SKATER_", "RESKATE_BOARD_", "RESKATE_GROUND_PLANE", "_RESKATE_"))
            ]
            if len(scene_meshes) < 2:
                return {'FINISHED'}
            sel_meshes = scene_meshes

        active = context.view_layer.objects.active
        try:
            cur_idx = sel_meshes.index(active)
        except ValueError:
            cur_idx = 0

        next_idx = (cur_idx + int(self.direction)) % len(sel_meshes)
        target_obj = sel_meshes[next_idx]

        prop = getattr(context.scene, "reskate_prop", None)
        if prop and active and active.type == 'MESH':
            save_prop_settings_to_object(active, prop)

        try:
            target_obj.select_set(True)
        except Exception:
            pass
        context.view_layer.objects.active = target_obj
        if prop:
            load_prop_settings_from_object(target_obj, prop)

        sync_thumbnail_camera_on_prop_switch(context, target_obj)
        compiler_bridge.invalidate_ui_caches()
        gpu_draw.invalidate_gpu_caches()
        return {'FINISHED'}


class RESKATE_OT_simple_page_nav(bpy.types.Operator):
    """Navigates between the 6 pages of the Simple Mode Wizard"""
    bl_idname = "reskate.simple_page_nav"
    bl_label = "Navigate Wizard Step"
    bl_description = "Switches to the selected page in the Simple Mode workflow"
    bl_options = {'REGISTER'}

    target_page: IntProperty(
        name="Target Page",
        default=0,
        min=0,
        max=6,
        options={'SKIP_SAVE'},
    )
    delta: IntProperty(
        name="Step Delta",
        default=0,
        min=-6,
        max=6,
        options={'SKIP_SAVE'},
    )
    direction: IntProperty(
        name="Direction",
        default=0,
        min=-6,
        max=6,
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        prop = getattr(context.scene, "reskate_prop", None)
        if not prop:
            return {'CANCELLED'}
        cleanup_if_exited_thumbnail_camera_view(context)
        cur = int(getattr(prop, "simple_wizard_page", 1) or 1)
        if self.target_page in (1, 2, 3, 4, 5, 6):
            new_page = int(self.target_page)
        else:
            step = int(self.delta) if int(self.delta) != 0 else int(self.direction)
            new_page = max(1, min(6, cur + step))
        prop.simple_wizard_page = int(new_page)
        if int(new_page) != 5:
            _exit_thumbnail_camera_preview(context)
        return {'FINISHED'}


class RESKATE_OT_copy_compiler_log(bpy.types.Operator):
    """Copies the full compiler log to the system clipboard"""
    bl_idname = "reskate.copy_compiler_log"
    bl_label = "Copy Log to Clipboard"
    bl_description = "Copies the full output from the most recent compile run to your clipboard"
    bl_options = {'REGISTER'}

    def execute(self, context):
        log_text = compiler_bridge.get_last_compiler_log_text(context)
        context.window_manager.clipboard = log_text
        self.report({'INFO'}, "ReSkate: Copied compiler log to clipboard.")
        return {'FINISHED'}


class RESKATE_OT_check_for_updates(bpy.types.Operator):
    """Checks GitHub Releases (https://github.com/CameronTutt/ReSkate-Prop-Studio) for a newer version of ReSkate Prop Studio"""
    bl_idname = "reskate.check_for_updates"
    bl_label = "Check for Updates"
    bl_description = "Queries GitHub Releases (https://github.com/CameronTutt/ReSkate-Prop-Studio/releases) to check if a newer version of ReSkate Prop Studio is available"
    bl_options = {'REGISTER'}

    def execute(self, context):
        import urllib.request
        prefs = get_addon_preferences(context)
        current_ver = compiler_bridge.ADDON_VERSION_STR
        repo_url = getattr(compiler_bridge, "GITHUB_REPO_URL", "https://github.com/CameronTutt/ReSkate-Prop-Studio")
        api_url = getattr(
            compiler_bridge,
            "GITHUB_RELEASES_API_URL",
            "https://api.github.com/repos/CameronTutt/ReSkate-Prop-Studio/releases/latest",
        )
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": f"ReSkatePropStudio/{current_ver}", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            tag_name = str(payload.get("tag_name") or "").strip().lstrip("vV")
            html_url = str(payload.get("html_url") or f"{repo_url}/releases/latest")
            if not tag_name:
                status_msg = f"✅ Up to date (v{current_ver})"
                latest_url = ""
            else:
                def _ver_tuple(v_str: str) -> tuple[int, ...]:
                    parts = []
                    for p in re.findall(r"\d+", v_str):
                        parts.append(int(p))
                    return tuple(parts) if parts else (0, 0, 0)

                if _ver_tuple(tag_name) > _ver_tuple(current_ver):
                    status_msg = f"🆕 Update available: v{tag_name} (installed: v{current_ver})"
                    latest_url = html_url
                else:
                    status_msg = f"✅ Up to date (v{current_ver} is the latest release on GitHub)"
                    latest_url = ""
        except Exception as exc:
            status_msg = f"⚠️ Couldn't reach GitHub ({exc})"
            latest_url = f"{repo_url}/releases"

        if prefs is not None:
            prefs.last_update_status = status_msg
            if hasattr(prefs, "update_check_status"):
                prefs.update_check_status = status_msg
            if hasattr(prefs, "update_latest_url"):
                prefs.update_latest_url = latest_url
        if getattr(context, "screen", None):
            for area in context.screen.areas:
                area.tag_redraw()
        self.report({'INFO'}, f"ReSkate Update Check: {status_msg}")
        return {'FINISHED'}


class RESKATE_OT_reset_stable_prop_key(bpy.types.Operator):
    """Clears the locked internal prop key on the active object so the next compile treats it as a brand-new prop instead of updating the existing entry"""
    bl_idname = "reskate.reset_stable_prop_key"
    bl_label = "Reset internal key"
    bl_description = "Clears the locked internal prop key on this object so the next compile adds it as a brand-new prop instead of updating the existing pack entry"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = get_target_mesh_object(context) or context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "ReSkate: Select a 3D mesh prop first.")
            return {'CANCELLED'}
        compiler_bridge.reset_stable_prop_key_on_object(context, obj)
        prop = getattr(context.scene, "reskate_prop", None)
        if prop is not None:
            prop.prop_id_locked = False
        compiler_bridge.invalidate_ui_caches()
        self.report({'INFO'}, f"ReSkate: Reset internal key on '{obj.name}'. Next compile will treat it as a new prop.")
        return {'FINISHED'}


CLASSES = (
    # Step 1: Scale & Origin
    RESKATE_OT_toggle_pbr_neutral_viewport,
    RESKATE_OT_send_to_game,
    RESKATE_OT_open_addon_preferences,
    RESKATE_OT_clear_compiler_cache,
    RESKATE_OT_set_origin,
    RESKATE_OT_origin_to_bottom_center,
    RESKATE_OT_origin_to_geometry_center,
    RESKATE_OT_origin_to_ground_contact,
    RESKATE_OT_drop_prop_flush_to_ground,
    RESKATE_OT_set_flush_to_ground,
    RESKATE_OT_snap_origin_to_selected,
    RESKATE_OT_toggle_interactive_origin_edit,
    RESKATE_OT_clear_all_overlays,
    RESKATE_OT_run_preflight_check,
    RESKATE_OT_preflight_fix_all,
    RESKATE_OT_preview_material_textures_popup,
    RESKATE_OT_open_reference_project,
    RESKATE_OT_place_on_ground_world_center,
    RESKATE_OT_snap_ground_center,
    RESKATE_OT_center_origin,
    RESKATE_OT_toggle_skater_ref,
    RESKATE_OT_toggle_skateboard_ref,
    RESKATE_OT_toggle_ground_plane_ref,
    RESKATE_OT_clear_scale_references,
    RESKATE_OT_toggle_round_rail_ref,
    RESKATE_OT_toggle_scale_reference,
    RESKATE_OT_toggle_prop_id_lock,
    RESKATE_OT_reset_stable_prop_key,
    RESKATE_OT_preflight_fix,
    RESKATE_OT_duplicate_prop_variant,
    RESKATE_OT_apply_category_to_selected,
    RESKATE_OT_select_saved_qd_tab,
    RESKATE_OT_remove_saved_qd_tab,
    RESKATE_OT_remove_exp_feature,
    RESKATE_OT_clear_all_exp_features,

    # Step 2: Collision & Surfaces
    RESKATE_OT_check_collision,
    RESKATE_OT_thicken_collision_proxy,
    RESKATE_OT_highlight_surface_material,
    RESKATE_OT_carousel_step,
    RESKATE_OT_simple_page_nav,
    RESKATE_OT_toggle_col_preview,
    RESKATE_OT_generate_authored_col,
    RESKATE_OT_create_simplified_col_copy,
    RESKATE_OT_generate_collision_preview,
    RESKATE_OT_generate_geometric_hull_col,
    RESKATE_OT_toggle_edit_collision_mesh,
    RESKATE_OT_generate_placement_proxy,
    RESKATE_OT_straighten_collision_edges,
    RESKATE_OT_reset_collision_edges,
    RESKATE_OT_lock_collision_edge_baseline,
    RESKATE_OT_sync_material_surfaces,
    RESKATE_OT_assign_face_surface,
    RESKATE_OT_clear_face_surface,
    RESKATE_OT_clear_all_face_surfaces,
    RESKATE_OT_select_face_surface_group,
    RESKATE_OT_clear_do_not_align,
    RESKATE_OT_apply_surface_feel_preset,
    RESKATE_OT_trim_bottom_lip,
    RESKATE_OT_copy_settings_to_selected,

    # Step 3: Grind
    RESKATE_OT_add_grind_path_from_selection,
    RESKATE_OT_remove_grind_from_selection,
    RESKATE_OT_block_grind_on_selection,
    RESKATE_OT_remove_grind_path,
    RESKATE_OT_select_grind_path_in_edit_mode,
    RESKATE_OT_clear_grind_paths,
    RESKATE_OT_autodetect_grind_edges,
    RESKATE_OT_auto_detect_grind_edges,
    RESKATE_OT_recalculate_auto_grind_set,
    RESKATE_OT_mark_grind_edge,
    RESKATE_OT_set_selected_grind_type,
    RESKATE_OT_clear_grind_edge,
    RESKATE_OT_clear_all_grind_edges,
    RESKATE_OT_generate_grind_tube_proxy,

    # Step 4: Look
    RESKATE_OT_bake_uv_atlas,
    RESKATE_OT_preview_baked_atlas,
    RESKATE_OT_bake_procedural_materials,
    RESKATE_OT_sync_shader_to_sk8_material,
    RESKATE_OT_clear_material_texture_slot,
    RESKATE_OT_apply_look_preset,
    RESKATE_OT_apply_starter_material_template,
    RESKATE_OT_fetch_blender_org_texture,
    RESKATE_OT_apply_uv_tiling_scale,
    RESKATE_OT_apply_world_scale_uvs,
    RESKATE_OT_toggle_roughness_invert,
    RESKATE_OT_create_lights_from_emission,
    RESKATE_OT_auto_unwrap_uvs,

    # Step 5: Light
    RESKATE_OT_add_dynamic_prop_light,
    RESKATE_OT_link_light_slot,
    RESKATE_OT_unlink_light_slot,
    RESKATE_OT_select_linked_light,
    RESKATE_OT_delete_linked_light,
    RESKATE_OT_make_faces_glow_emitter,

    # Step 6: Thumbnail & Variant Families
    RESKATE_OT_frame_isometric_camera,
    RESKATE_OT_orbit_thumb_camera,
    RESKATE_OT_view_thumbnail_camera,
    RESKATE_OT_set_thumb_bg_preset,
    RESKATE_OT_copy_thumb_lighting_preset,
    RESKATE_OT_paste_thumb_lighting_preset,
    RESKATE_OT_apply_thumb_lighting_to_selected,
    RESKATE_OT_group_selected_as_variants,
    RESKATE_OT_clear_variant_family,
    RESKATE_OT_render_thumbnails,
    RESKATE_OT_choose_thumbnail_image,
    RESKATE_OT_preview_thumbnail_popup,
    RESKATE_OT_use_prop_thumb_as_mod_icon,

    # Step 7 & 8: Ship, Pack Management & Thunderstore Release
    RESKATE_OT_compile_and_deploy,
    RESKATE_OT_compile_selected_props,
    RESKATE_OT_queue_compile_all,
    RESKATE_OT_select_pack_or_scene_props,
    RESKATE_OT_frame_selected_props,
    RESKATE_OT_switch_or_assign_prop_pack,
    RESKATE_OT_unassign_prop_pack_from_scene,
    RESKATE_OT_pack_prop_action,
    RESKATE_OT_restore_pack_backup,
    RESKATE_OT_bump_pack_version,
    RESKATE_OT_export_thunderstore_zip,
    RESKATE_OT_cancel_compile,
    RESKATE_OT_copy_compile_log,
    RESKATE_OT_copy_compiler_log,
    RESKATE_OT_check_for_updates,
    RESKATE_OT_queue_add_active,
    RESKATE_OT_queue_sync_item,
    RESKATE_OT_queue_sync_all,
    RESKATE_OT_queue_remove_item,
    RESKATE_OT_queue_remove_active,
    RESKATE_OT_queue_move_up,
    RESKATE_OT_queue_move_down,
    RESKATE_OT_queue_clear,
    RESKATE_OT_open_mod_folder,
    RESKATE_OT_open_mod_output_folder,
    RESKATE_OT_inspect_manifest,
    RESKATE_OT_reset_default_paths,
    RESKATE_OT_install_python_deps,
    RESKATE_OT_sync_quickdrop_live_bridge,
    RESKATE_OT_clear_quickdrop_live_wireframes,
    RESKATE_OT_open_drop_glb_inbox,
)

OPERATOR_CLASSES = CLASSES


def register():
    for cls in CLASSES:
        if not getattr(cls, "is_registered", False):
            bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
