"""
ReSkate Prop Studio — Open-Source Skate. Prop Authoring Plugin & Compiler Bridge
Package: sk8_prop_authoring_addon
License: MIT / GPL-3.0-or-later (Open Source)
"""

from __future__ import annotations

ADDON_VERSION = "0.32.2"

bl_info = {
    "name": "ReSkate Prop Studio",
    "author": "Crem",
    "version": (0, 32, 2),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > ReSkate Prop",
    "description": "Open-Source Zero-Donor Skate. Prop Authoring, Havok Collision, Grind Visualizer, PBR/Cutout Shaders & Dynamic Lights for ReSkate",
    "doc_url": "https://github.com/CameronTutt/ReSkate-Prop-Studio",
    "category": "3D View",
}

import importlib

from . import properties
from . import scale_reference_meshes
from . import compiler_bridge
from . import operators
from . import gpu_draw
from . import tutorial_dialogs
from . import ui_panels

for _submod in (
    properties,
    scale_reference_meshes,
    compiler_bridge,
    operators,
    gpu_draw,
    tutorial_dialogs,
    ui_panels,
):
    importlib.reload(_submod)


def register():
    """Register all PropertyGroups, Compiler Bridge, Operators, Viewport Handlers, Tutorial Popovers, and UI Panels."""
    import bpy
    active_pkg = bpy.app.driver_namespace.get("_RESKATE_ADDON_ACTIVE_PACKAGE")
    if active_pkg and active_pkg != __name__:
        return
    bpy.app.driver_namespace["_RESKATE_ADDON_ACTIVE_PACKAGE"] = __name__
    properties.register()
    compiler_bridge.register()
    operators.register()
    gpu_draw.register()
    tutorial_dialogs.register()
    ui_panels.register()


def unregister():
    """Unregister all UI Panels, Tutorial Popovers, Viewport Handlers, Operators, Compiler Bridge, and PropertyGroups."""
    import bpy
    active_pkg = bpy.app.driver_namespace.get("_RESKATE_ADDON_ACTIVE_PACKAGE")
    if active_pkg and active_pkg != __name__:
        return
    bpy.app.driver_namespace.pop("_RESKATE_ADDON_ACTIVE_PACKAGE", None)
    ui_panels.unregister()
    tutorial_dialogs.unregister()
    gpu_draw.unregister()
    operators.unregister()
    compiler_bridge.unregister()
    properties.unregister()


if __name__ == "__main__":
    register()
