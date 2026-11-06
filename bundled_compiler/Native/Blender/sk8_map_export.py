bl_info = {
    "name": "Skate. Map Export",
    "author": "CustomMapTools",
    "version": (2, 22, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Skate Map",
    "description": "Bulk-export meshes and textures plus a map.json handshake",
    "category": "Import-Export",
}

import hashlib
import json
import math
import time
import os
import shutil
import struct
import re
import textwrap

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Menu, Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix, Vector


SURFACES = [
    ("default", "Default", "Generic hard surface"),
    ("concrete", "Concrete", "Concrete, kerbs, plaza slabs"),
    ("asphalt", "Asphalt", "Road surface"),
    ("wood", "Wood", "Plywood, decks, ramps"),
    ("metal", "Metal", "Sheet metal, panels"),
    ("metal_rail", "Metal Rail", "Grindable metal rail"),
    ("metal_ledge", "Metal Ledge", "Grindable metal edge"),
    ("concrete_ledge", "Concrete Ledge", "Grindable concrete edge"),
    ("brick", "Brick", "Brickwork"),
    ("tile", "Tile", "Tiled floor"),
    ("glass", "Glass", "Windows"),
    ("plastic", "Plastic", "Bins, cones, crates"),
    ("grass", "Grass", "Grass and hedges"),
    ("dirt", "Dirt", "Soil, gravel"),
    ("sand", "Sand", "Sand"),
    ("water", "Water", "Water surface"),
    ("carpet", "Carpet", "Interior carpet"),
]


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

NATIVE_COLLISION_MATERIAL_FLAG = 0x20


NATIVE_COLLISION_INTRINSIC_PROPERTIES = {
    9: ("ExcludeFromEdgeGeneration", "DoNotAlign"),
    47: ("ExcludeFromEdgeGeneration",),
    65: ("ExcludeFromEdgeGeneration", "DoNotAlign"),
    66: ("ExcludeFromEdgeGeneration", "DoNotAlign"),
    70: ("ExcludeFromEdgeGeneration",),
    71: ("IncludeInSurfaceAnalysis",),
    76: ("DoNotAlign",),
    86: ("JumpPadBehaviour", "WipeoutBehaviour"),
    90: ("Stair",),
    101: ("ExcludeFromEdgeGeneration", "JumpPadBehaviour", "WipeoutBehaviour"),
    102: ("ExcludeFromEdgeGeneration", "JumpPadBehaviour", "WipeoutBehaviour"),
    103: ("ExcludeFromEdgeGeneration", "WipeoutBehaviour"),
    104: ("ExcludeFromGrinding",),
    109: ("CanOccludeCamera",),
    111: ("DoNotAlign",),
}

NATIVE_COLLISION_PROPERTY_LABELS = {
    "ExcludeFromEdgeGeneration": "No generated grind/mantle edges",
    "IncludeInSurfaceAnalysis": "Included in surface analysis",
    "ExcludeFromGrinding": "Grinding disabled",
    "JumpPadBehaviour": "Jump-pad behaviour",
    "BoostPadBehaviour": "Boost-pad behaviour",
    "WipeoutBehaviour": "Wipeout behaviour",
    "SlideBehaviour": "Slide behaviour",
    "DoNotAlign": "Skater alignment disabled",
    "Stair": "Stair behaviour",
    "CanOccludeCamera": "Can occlude the gameplay camera",
}


BEHAVIOR_FIELDS = (
    ("exclude_from_edge_generation", "ExcludeFromEdgeGeneration",
     "No Generated Edges"),
    ("include_in_surface_analysis", "IncludeInSurfaceAnalysis",
     "Include in Surface Analysis"),
    ("exclude_from_grinding", "ExcludeFromGrinding", "Disable Grinding"),
    ("jump_pad", "JumpPadBehaviour", "Jump Pad"),
    ("boost_pad", "BoostPadBehaviour", "Boost Pad"),
    ("wipeout", "WipeoutBehaviour", "Force Wipeout"),
    ("slide", "SlideBehaviour", "Slide"),
    ("do_not_align", "DoNotAlign", "Do Not Align Skater"),
    ("stairs", "Stair", "Stairs"),
    ("camera_occluder", "CanOccludeCamera", "Camera Occluder"),
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


def _native_collision_material_items():
    items = []
    for network_id, network_packed in enumerate(NATIVE_COLLISION_NETWORK_PACKED):
        material_slot = (network_packed >> 6) & 0x1fff
        property_slot = (network_packed >> 19) & 0x1fff
        material_row, material_label, material_behaviour = \
            NATIVE_COLLISION_MATERIAL_ROWS[material_slot]
        details = [f"material slot {material_slot} / property row {material_row}"]
        behaviours = [material_behaviour] if material_behaviour else []
        if property_slot:
            property_row, property_label, property_behaviour = \
                NATIVE_COLLISION_MATERIAL_ROWS[property_slot]
            label = f"{material_label} + {property_label} behavior"
            details.append(
                f"property slot {property_slot} / property row {property_row}")
            if property_behaviour:
                behaviours.append(property_behaviour)
        else:
            label = material_label
        packed = network_packed | NATIVE_COLLISION_MATERIAL_FLAG
        description = (f"Steam MaterialGrid network id {network_id}: " +
                       ", ".join(details) +
                       f"; MaterialDecl.Packed={packed}")
        if behaviours:
            description += "; " + "; ".join(behaviours)
        items.append((f"material_{packed:04d}",
                      f"{network_id:03d} - {label}",
                      description, 0, packed))
    return items


NATIVE_COLLISION_MATERIALS = _native_collision_material_items()
NATIVE_COLLISION_MATERIAL_PACKED = {
    item[0]: item[4] for item in NATIVE_COLLISION_MATERIALS
}
# Declarations carrying the IncludeInSurfaceAnalysis property (slot 71), by material slot:
# what "Round Rail" switches an object's base surface to (see the shipped round rail kit).
SURFACE_ANALYSIS_PROPERTY_SLOT = 71
SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT = {
    (packed >> 6) & 0x1fff: packed
    for packed in NATIVE_COLLISION_MATERIAL_PACKED.values()
    if ((packed >> 19) & 0x1fff) == SURFACE_ANALYSIS_PROPERTY_SLOT
}
METAL_RAIL_MATERIAL_SLOT = 46


def smooth_grind_packed(packed):
    """The smooth-grind declaration for a packed collision material (metal rail fallback)."""
    if ((packed >> 19) & 0x1fff) == SURFACE_ANALYSIS_PROPERTY_SLOT:
        return packed
    slot = (packed >> 6) & 0x1fff
    if slot == 0:   # an untouched default surface: a rail is metal
        slot = METAL_RAIL_MATERIAL_SLOT
    return SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT.get(
        slot, SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT[METAL_RAIL_MATERIAL_SLOT])
NATIVE_COLLISION_MATERIAL_IDENTIFIER = {
    item[4]: item[0] for item in NATIVE_COLLISION_MATERIALS
}
NATIVE_COLLISION_TEMPLATE_BY_IDENTIFIER = {
    item[0]: item for item in NATIVE_COLLISION_MATERIALS
}
DEFAULT_COLLISION_MATERIAL = "material_0032"
WATER_COLLISION_MATERIAL_PACKED = 0x00F007A0


def _native_collision_template_info(identifier):

    item = NATIVE_COLLISION_TEMPLATE_BY_IDENTIFIER.get(identifier)
    if item is None:
        identifier = DEFAULT_COLLISION_MATERIAL
        item = NATIVE_COLLISION_TEMPLATE_BY_IDENTIFIER[identifier]
    packed = item[4]
    network_packed = packed & ~0x3f
    network_id = NATIVE_COLLISION_NETWORK_PACKED.index(network_packed)
    material_slot = (network_packed >> 6) & 0x1fff
    property_slot = (network_packed >> 19) & 0x1fff
    slots = []
    properties = []
    for channel, slot in (("Material", material_slot),
                          ("Property overlay", property_slot)):
        if channel == "Property overlay" and not slot:
            continue
        property_row, label, _behaviour = NATIVE_COLLISION_MATERIAL_ROWS[slot]
        slots.append({
            "channel": channel,
            "slot": slot,
            "property_row": property_row,
            "label": label,
        })
        for prop in NATIVE_COLLISION_INTRINSIC_PROPERTIES.get(slot, ()):
            if prop not in properties:
                properties.append(prop)
    return {
        "identifier": identifier,
        "network_id": network_id,
        "packed": packed,
        "label": item[1],
        "slots": tuple(slots),
        "properties": tuple(properties),
    }


def _effective_behavior_values(settings, donor_identifier=None):


    if settings.custom_behavior:
        return "override", {
            field: bool(getattr(settings, field))
            for field, _native_property, _label in BEHAVIOR_FIELDS
        }
    identifier = donor_identifier or settings.collision_material
    inherited = set(_native_collision_template_info(identifier)["properties"])
    return "donor", {
        field: native_property in inherited
        for field, native_property, _label in BEHAVIOR_FIELDS
    }


def _effective_behavior_getter(field):

    def get_effective_behavior(settings):
        return _effective_behavior_values(settings)[1][field]
    return get_effective_behavior


def _effective_object_behavior_getter(field):

    def get_effective_object_behavior(settings):
        owner = getattr(settings, "id_data", None)
        material = getattr(owner, "active_material", None)
        donor = _collision_template_for(owner, material)
        source = settings
        if not _object_surface_is_explicit(owner):
            source = getattr(material, "sk8_material", settings)
        if hasattr(source, "custom_behavior"):
            return _effective_behavior_values(source, donor)[1][field]
        native_property = next(
            native for candidate, native, _label in BEHAVIOR_FIELDS
            if candidate == field)
        return native_property in set(
            _native_collision_template_info(donor)["properties"])
    return get_effective_object_behavior


def _update_custom_behavior(settings, context):

    marker = "_sk8_behavior_override_initialized"
    if settings.custom_behavior and not bool(settings.get(marker, False)):
        owner = settings.id_data
        donor = (_collision_template_for(owner, owner.active_material)
                 if isinstance(owner, bpy.types.Object) else settings.collision_material)
        inherited = set(_native_collision_template_info(
            donor)["properties"])


        settings[marker] = True
        for field, native_property, _label in BEHAVIOR_FIELDS:
            setattr(settings, field, native_property in inherited)
    elif not settings.custom_behavior:


        if any(settings.is_property_set(field)
               for field, _native_property, _label in BEHAVIOR_FIELDS):
            settings[marker] = True
    _sync_surface_profile(settings, context)


COLLISION_MODES = [
    ("triangle_mesh", "Exact Triangle Mesh",
     "Use the evaluated render faces exactly; best for floors, walls, bowls, "
     "and other static level geometry"),
    ("convex_parts", "Gameplay Convex (Smart)",
     "Use one hull when the mesh is clean, closed, and convex; otherwise "
     "decompose it into gameplay-safe convex pieces"),
    ("hull", "Forced Single Envelope",
     "Wrap the entire object in one convex hull; concavities and openings will "
     "be filled"),
    ("none", "None", "Export this object with no collision"),
    ("water", "Water", "Use exact faces with native water collision for skater and ragdoll floating"),
]

PLACEMENT_MODES = [
    ("authored_mesh", "Authored Mesh",
     "Convert this Blender mesh into a new native MeshSet and ObjectBlueprint"),
    ("retail_blueprint", "Retail Blueprint Instance",
     "Use this object only as a transform for an ObjectBlueprint already carried "
     "by the BAM host bundle"),
]


def _sync_placement_settings(settings, _context):

    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_placement_mode"] = str(settings.placement_mode)
        owner["sk8_retail_blueprint"] = str(settings.retail_blueprint).strip()
    except Exception:
        pass


def _sync_collision_mode(settings, _context):


    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_collision_mode"] = str(settings.collision_mode)
    except Exception:


        pass


def _sync_round_rail(settings, _context):
    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_round_rail"] = bool(settings.round_rail)
    except Exception:
        pass


def _sync_collision_material(settings, _context):

    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_collision_material_packed"] = int(
            NATIVE_COLLISION_MATERIAL_PACKED[settings.collision_material])
        if isinstance(owner, bpy.types.Object):
            owner["sk8_object_surface_authored"] = True
    except Exception:
        pass
    if hasattr(settings, "custom_audio"):
        _sync_surface_profile(settings, _context)


def _surface_profile_record(settings):

    if not any((settings.custom_audio, settings.custom_friction,
                settings.custom_behavior)):
        return None
    profile = {
        "format": 1,
        "donor_material_packed": int(
            NATIVE_COLLISION_MATERIAL_PACKED[settings.collision_material]),
    }
    if settings.custom_audio:
        profile["audio"] = {
            "softness": float(settings.audio_softness),
            "smoothness": float(settings.audio_smoothness),
            "min_impact_force": float(settings.audio_min_impact_force),
            "impact_cooldown_release": float(
                settings.audio_impact_cooldown_release),
            "ignore_player_collisions": bool(
                settings.audio_ignore_player_collisions),
        }
    if settings.custom_friction:
        profile["friction"] = {
            part: {
                "dynamic": float(getattr(settings, f"{part}_dynamic_friction")),
                "static": float(getattr(settings, f"{part}_static_friction")),
                "restitution": float(getattr(settings, f"{part}_restitution")),
            }
            for part in ("deck", "truck", "wheel")
        }
    if settings.custom_behavior:
        profile["behavior"] = {
            "exclude_from_edge_generation": bool(
                settings.exclude_from_edge_generation),
            "include_in_surface_analysis": bool(
                settings.include_in_surface_analysis),
            "exclude_from_grinding": bool(settings.exclude_from_grinding),
            "jump_pad": bool(settings.jump_pad),
            "boost_pad": bool(settings.boost_pad),
            "wipeout": bool(settings.wipeout),
            "slide": bool(settings.slide),
            "do_not_align": bool(settings.do_not_align),
            "stairs": bool(settings.stairs),
            "camera_occluder": bool(settings.camera_occluder),
        }
    return profile


def _surface_profile_id(profile):
    payload = json.dumps(profile, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    return "surface_" + hashlib.sha256(payload).hexdigest()[:20]


def _stored_surface_profile(owner):
    if owner is None:
        return None
    try:
        raw = owner.get("sk8_surface_profile_json")
        profile = json.loads(str(raw)) if raw else None
        return profile if isinstance(profile, dict) else None
    except Exception:
        return None


def _object_surface_is_explicit(obj):
    if obj is None:
        return False
    try:
        if bool(obj.get("sk8_object_surface_authored", False)):
            return True
    except Exception:
        pass
    return (_explicit_collision_template(obj, "sk8_object") is not None or
            _stored_surface_profile(obj) is not None)


def _owner_surface_profile(owner, group):
    if owner is None:
        return None
    try:
        return _surface_profile_record(getattr(owner, group))
    except Exception:
        return _stored_surface_profile(owner)


def _material_audio_is_explicit(material):
    if material is None:
        return False
    if _explicit_collision_template(material, "sk8_material") is not None:
        return True
    settings = getattr(material, "sk8_material", None)
    if settings is not None and settings.is_property_set("custom_audio"):
        return True
    raw = material.get("sk8_material", {})
    return "custom_audio" in raw or "audio" in (_stored_surface_profile(material) or {})


def _combine_surface_profiles(obj, material, object_profile, material_profile):
    profile = {"format": 1, "donor_material_packed": NATIVE_COLLISION_MATERIAL_PACKED[
        _collision_template_for(obj, material)]}
    audio_source = (material_profile if _material_audio_is_explicit(material)
                    else object_profile or material_profile) or {}
    if "audio" in audio_source:
        profile["audio"] = dict(audio_source["audio"])
    gameplay_source = (object_profile if _object_surface_is_explicit(obj)
                       else material_profile) or {}
    for key in ("friction", "behavior"):
        if key in gameplay_source:
            profile[key] = gameplay_source[key]
    return profile if len(profile) > 2 else None


def _surface_profile_for(obj, material):
    return _combine_surface_profiles(obj, material,
        _owner_surface_profile(obj, "sk8_object"),
        _owner_surface_profile(material, "sk8_material"))


def _sync_surface_profile(settings, _context):

    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        if isinstance(owner, bpy.types.Object):
            owner["sk8_object_surface_authored"] = True
        profile = _surface_profile_record(settings)
        if profile is None:
            if "sk8_surface_profile_json" in owner:
                del owner["sk8_surface_profile_json"]
        else:
            owner["sk8_surface_profile_json"] = json.dumps(
                profile, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True)
    except Exception:

        pass


def _sync_pause_map_visibility(settings, _context):

    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_hide_from_pause_map"] = bool(settings.hide_from_pause_map)
    except Exception:
        pass


def _sync_material_surface(settings, _context):

    owner = getattr(settings, "id_data", None)
    if owner is None:
        return
    try:
        owner["sk8_surface"] = str(settings.surface)
    except Exception:
        pass

SPAWN_MARKER = "spawn"


class Sk8MapSettings(PropertyGroup):
    folder: StringProperty(
        name="Export Folder", subtype="DIR_PATH",
        description="Where map.json, meshes/ and textures/ are written")
    selected_only: BoolProperty(
        name="Selected Only", default=False,
        description="Export only the selected objects")
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    scale: FloatProperty(
        name="Scale", default=1.0, min=0.0001,
        description="Multiplies every exported translation; Blender metres map "
                    "1:1 to game metres at 1.0")
    shadow_distance_low: FloatProperty(
        name="Low Shadow Distance", default=50.0, min=1.0, max=10000.0,
        description="Native low-quality sun-shadow view distance in metres")
    shadow_distance_medium: FloatProperty(
        name="Medium Shadow Distance", default=250.0, min=1.0, max=10000.0,
        description="Native medium-quality sun-shadow view distance in metres")
    shadow_distance_high: FloatProperty(
        name="High Shadow Distance", default=500.0, min=1.0, max=10000.0,
        description="Native high-quality sun-shadow view distance in metres")
    batch_collision_mode: EnumProperty(
        name="Selected Collision Type",
        items=COLLISION_MODES,
        default="triangle_mesh",
        description="Collision type assigned by the selected-mesh batch control")


class Sk8ObjectSettings(PropertyGroup):
    placement_mode: EnumProperty(
        name="Placement",
        items=PLACEMENT_MODES,
        default="authored_mesh",
        update=_sync_placement_settings,
        description="Author new geometry or instance a shipped native blueprint")
    retail_blueprint: StringProperty(
        name="Retail ObjectBlueprint",
        default="",
        update=_sync_placement_settings,
        description=("Exact EBX asset path of an ObjectBlueprint in BAM's base "
                     "bundle; .ebx and a leading win32/ are optional"))
    hide_from_pause_map: BoolProperty(
        name="Hide From Map Overview", default=False,
        update=_sync_pause_map_visibility,
        description=("Leave this object out of the pause-menu map picture and its framing. "
                     "It still exports and plays as normal. On a collection instance it "
                     "hides everything the instance draws"))
    collision_mode: EnumProperty(
        name="Collision",
        items=COLLISION_MODES,
        default="triangle_mesh",
        update=_sync_collision_mode,
        description="How the custom-map compiler builds collision geometry for this object")
    collision_material: EnumProperty(
        name="Base Surface / Audio Donor",
        items=NATIVE_COLLISION_MATERIALS,
        default=DEFAULT_COLLISION_MATERIAL,
        update=_sync_collision_material,
        description=("Shipped surface used as the starting material and donor "
                     "for this object's sound, VFX, and unspecified properties"))
    round_rail: BoolProperty(
        name="Round Rail (smooth grind)", default=False,
        update=_sync_round_rail,
        description=("Grind this object as a smooth surface, the way the shipped round rails "
                     "do. Without it the game only finds grind edges on creases of about 30 "
                     "degrees or more, so tubes with 16 or more sides cannot be grinded. "
                     "Adds the IncludeInSurfaceAnalysis property to the base surface "
                     "(metal rail when the surface has no such variant)"))
    custom_audio: BoolProperty(
        name="Override Audio Metadata", default=False,
        update=_sync_surface_profile,
        description="Override this object's numeric collision-audio metadata")
    audio_softness: FloatProperty(
        name="Softness", default=0.2, min=0.0, max=1.0,
        update=_sync_surface_profile)
    audio_smoothness: FloatProperty(
        name="Smoothness", default=0.25, min=0.0, max=1.0,
        update=_sync_surface_profile)
    audio_min_impact_force: FloatProperty(
        name="Minimum Impact Force", default=0.1, min=0.0, max=10000.0,
        update=_sync_surface_profile)
    audio_impact_cooldown_release: FloatProperty(
        name="Impact Cooldown", default=0.25, min=0.0, max=60.0,
        update=_sync_surface_profile)
    audio_ignore_player_collisions: BoolProperty(
        name="Ignore Player Collisions", default=False,
        update=_sync_surface_profile)
    custom_friction: BoolProperty(
        name="Override Contact Physics", default=False,
        update=_sync_surface_profile,
        description="Override this object's deck, truck, and wheel coefficients")
    deck_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.5, min=0.0, max=10.0,
        update=_sync_surface_profile)
    deck_static_friction: FloatProperty(
        name="Static", default=0.5, min=0.0, max=10.0,
        update=_sync_surface_profile)
    deck_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    truck_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.7, min=0.0, max=10.0,
        update=_sync_surface_profile)
    truck_static_friction: FloatProperty(
        name="Static", default=0.8, min=0.0, max=10.0,
        update=_sync_surface_profile)
    truck_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    wheel_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.7, min=0.0, max=10.0,
        update=_sync_surface_profile)
    wheel_static_friction: FloatProperty(
        name="Static", default=0.8, min=0.0, max=10.0,
        update=_sync_surface_profile)
    wheel_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    custom_behavior: BoolProperty(
        name="Override Gameplay Behavior", default=False,
        update=_update_custom_behavior,
        description="Replace all donor behavior flags for this object")
    exclude_from_edge_generation: BoolProperty(
        name="No Generated Edges", default=False,
        update=_sync_surface_profile)
    include_in_surface_analysis: BoolProperty(
        name="Include in Surface Analysis", default=False,
        update=_sync_surface_profile)
    exclude_from_grinding: BoolProperty(
        name="Disable Grinding", default=False,
        update=_sync_surface_profile)
    jump_pad: BoolProperty(name="Jump Pad", default=False,
                           update=_sync_surface_profile)
    boost_pad: BoolProperty(name="Boost Pad", default=False,
                            update=_sync_surface_profile)
    wipeout: BoolProperty(name="Force Wipeout", default=False,
                          update=_sync_surface_profile)
    slide: BoolProperty(name="Slide", default=False,
                        update=_sync_surface_profile)
    do_not_align: BoolProperty(name="Do Not Align Skater", default=False,
                               update=_sync_surface_profile)
    stairs: BoolProperty(name="Stairs", default=False,
                         update=_sync_surface_profile)
    camera_occluder: BoolProperty(name="Camera Occluder", default=False,
                                  update=_sync_surface_profile)
    effective_exclude_from_edge_generation: BoolProperty(
        name="No Generated Edges", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("exclude_from_edge_generation"))
    effective_include_in_surface_analysis: BoolProperty(
        name="Include in Surface Analysis", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("include_in_surface_analysis"))
    effective_exclude_from_grinding: BoolProperty(
        name="Disable Grinding", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("exclude_from_grinding"))
    effective_jump_pad: BoolProperty(
        name="Jump Pad", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("jump_pad"))
    effective_boost_pad: BoolProperty(
        name="Boost Pad", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("boost_pad"))
    effective_wipeout: BoolProperty(
        name="Force Wipeout", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("wipeout"))
    effective_slide: BoolProperty(
        name="Slide", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("slide"))
    effective_do_not_align: BoolProperty(
        name="Do Not Align Skater", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("do_not_align"))
    effective_stairs: BoolProperty(
        name="Stairs", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("stairs"))
    effective_camera_occluder: BoolProperty(
        name="Camera Occluder", options={"SKIP_SAVE"},
        get=_effective_object_behavior_getter("camera_occluder"))


LIGHT_TIMES = {"morning": 1, "noon": 2, "afternoon": 4, "evening": 8,
               "night": 16, "weatherday": 32, "weathernight": 64}


def _sync_light_settings(self, _context):
    if self.is_property_set("attenuation_radius"):
        self.id_data["sk8_light_range"] = float(self.attenuation_radius)
    self.id_data["sk8_light_tod"] = sum(LIGHT_TIMES[key] for key in self.time_of_day)
    self.id_data["sk8_light_area_mode"] = self.area_mode


def _light_uses_custom_nodes(light):
    # Blender 5 lights always have use_nodes on with a stock Emission -> Light Output tree;
    # only a tree with other nodes or wired inputs changes the light.
    tree = getattr(light, "node_tree", None)
    if tree is None:
        return False
    stock = {"ShaderNodeEmission", "ShaderNodeOutputLight"}
    return any(node.bl_idname not in stock
               or any(socket.is_linked for socket in node.inputs
                      if node.bl_idname == "ShaderNodeEmission")
               for node in tree.nodes)


class Sk8LightSettings(PropertyGroup):
    attenuation_radius: FloatProperty(
        name="Range (m)", default=40.0, min=0.001, update=_sync_light_settings,
        description="Finite native light range before map scale; independent of source size")
    time_of_day: EnumProperty(
        name="Active Time of Day", options={"ENUM_FLAG"}, update=_sync_light_settings,
        items=[("morning", "Morning", "", 1),
               ("noon", "Noon", "", 2),
               ("afternoon", "Afternoon", "", 4),
               ("evening", "Evening", "", 8),
               ("night", "Night", "", 16),
               ("weatherday", "Weather Day", "", 32),
               ("weathernight", "Weather Night", "", 64)],
        default={"morning", "noon", "afternoon", "evening", "night",
                 "weatherday", "weathernight"})
    area_mode: EnumProperty(
        name="Native Type", update=_sync_light_settings,
        items=[("AREA", "Area", "Soft panel that lights its whole hemisphere"),
               ("SPOTLIGHT", "Spotlight",
                "Focused panel: the light leaves the rectangle in a cone set by Spread")],
        default="AREA",
        description="How the game renders this area lamp")


def light_records(depsgraph, scale, selected=None):
    output = []

    def game_vector(vector):
        return [float(vector.x), float(vector.z), -float(vector.y)]

    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "LIGHT":
            continue
        original = getattr(obj, "original", None) or obj
        if original.hide_render or (selected is not None and original not in selected):
            continue
        light = obj.data
        matrix = Matrix(item.matrix_world)
        rotation = matrix.to_3x3()
        forward = (rotation @ Vector((0, 0, -1))).normalized()
        right = rotation @ Vector((1, 0, 0))
        right = (right - forward * right.dot(forward)).normalized()
        up = forward.cross(right).normalized()
        settings = original.get("sk8_light", {})
        distance = original.get("sk8_light_range", settings.get("attenuation_radius"))
        times = original.get("sk8_light_tod", settings.get("time_of_day", 127))
        area_mode = str(original.get("sk8_light_area_mode", "AREA"))
        live = getattr(original, "sk8_light", None)
        if live is not None:
            if live.is_property_set("attenuation_radius"):
                distance = float(live.attenuation_radius)
            if live.is_property_set("time_of_day"):
                times = sum(LIGHT_TIMES[key] for key in live.time_of_day)
            if live.is_property_set("area_mode"):
                area_mode = str(live.area_mode)
        if distance is None:
            distance = (getattr(light, "cutoff_distance", 40.0)
                        if getattr(light, "use_custom_distance", False) else 40.0)
        source = (str(getattr(getattr(original, "library", None), "filepath", ""))
                  + "/" + original.name)
        record = {
            "name": original.name, "source_path": source,
            "type": light.type.lower(),
            "position": game_vector(matrix.translation * scale),
            "direction": game_vector(forward),
            "basis": {"right": game_vector(right), "up": game_vector(up),
                      "forward": game_vector(forward)},
            "color_linear": [float(value) for value in light.color],
            "energy": float(light.energy),
            "normalize_power": bool(getattr(light, "normalize", True)),
            "uses_light_nodes": _light_uses_custom_nodes(light),
            "exposure": float(getattr(light, "exposure", 0.0)),
            "cast_shadow": bool(getattr(light, "use_shadow", True)),
            "use_custom_distance": True,
            "cutoff_distance": float(distance) * scale,
            "diffuse_factor": float(getattr(light, "diffuse_factor", 1.0)),
            "specular_factor": float(getattr(light, "specular_factor", 1.0)),
            "time_of_day": int(times),
            "native_entity_authored": False,
        }
        if light.type in {"POINT", "SPOT"}:
            record["radius"] = float(light.shadow_soft_size) * scale
        if light.type == "SPOT":
            record["spot_size_radians"] = float(light.spot_size)
            record["spot_blend"] = float(light.spot_blend)
        if light.type == "AREA":
            shape = str(light.shape).lower()
            width = float(light.size) * rotation.col[0].length * scale
            height = (float(light.size) if shape in {"square", "disk"}
                      else float(light.size_y)) * rotation.col[1].length * scale
            if not math.isclose(width, height, rel_tol=1e-6):
                shape = "ellipse" if shape in {"disk", "ellipse"} else "rectangle"
            record.update(shape=shape, size=width, size_y=height,
                          spread_radians=float(getattr(light, "spread", math.pi)),
                          area_mode=area_mode.lower())
        if item.is_instance:
            record["source_instance"] = [int(value) for value in item.persistent_id
                                         if int(value) != 2147483647]
        output.append(record)
    output.sort(key=lambda row: (row["source_path"], row.get("source_instance", []),
                                 row["position"], row["basis"]["forward"]))
    return output


AUDIO_TAG_PATHS = """
audio/_systems/audiotag/dgo_tag_disablegrabster
audio/_systems/audiotag/object/dgo_tag_gameplay_intromission
audio/_systems/audiotag/object/dgo_tag_obj_acliftvolume
audio/_systems/audiotag/object/dgo_tag_obj_airconditioner
audio/_systems/audiotag/object/dgo_tag_obj_aircraft
audio/_systems/audiotag/object/dgo_tag_obj_airyalley
audio/_systems/audiotag/object/dgo_tag_obj_amphitheter
audio/_systems/audiotag/object/dgo_tag_obj_amphitheterstage
audio/_systems/audiotag/object/dgo_tag_obj_backalley
audio/_systems/audiotag/object/dgo_tag_obj_barrierpedestrian
audio/_systems/audiotag/object/dgo_tag_obj_barrierpolicestyle
audio/_systems/audiotag/object/dgo_tag_obj_bathroom
audio/_systems/audiotag/object/dgo_tag_obj_bee
audio/_systems/audiotag/object/dgo_tag_obj_behindthewall
audio/_systems/audiotag/object/dgo_tag_obj_bench
audio/_systems/audiotag/object/dgo_tag_obj_benchbacklessconcrete
audio/_systems/audiotag/object/dgo_tag_obj_benchbacklessmetal
audio/_systems/audiotag/object/dgo_tag_obj_benchmodern
audio/_systems/audiotag/object/dgo_tag_obj_benchparklong
audio/_systems/audiotag/object/dgo_tag_obj_benchpicnic
audio/_systems/audiotag/object/dgo_tag_obj_bicycle
audio/_systems/audiotag/object/dgo_tag_obj_bicycledamaged
audio/_systems/audiotag/object/dgo_tag_obj_blimp
audio/_systems/audiotag/object/dgo_tag_obj_blockparty
audio/_systems/audiotag/object/dgo_tag_obj_bluetoothspeaker
audio/_systems/audiotag/object/dgo_tag_obj_boombox
audio/_systems/audiotag/object/dgo_tag_obj_boombox_med
audio/_systems/audiotag/object/dgo_tag_obj_boombox_seasonal
audio/_systems/audiotag/object/dgo_tag_obj_boombox_small_seawallentertainment
audio/_systems/audiotag/object/dgo_tag_obj_bridge_north
audio/_systems/audiotag/object/dgo_tag_obj_buildinghallway
audio/_systems/audiotag/object/dgo_tag_obj_buildinglobbyentertainment
audio/_systems/audiotag/object/dgo_tag_obj_buildinglobbyfinancial
audio/_systems/audiotag/object/dgo_tag_obj_buildingrack
audio/_systems/audiotag/object/dgo_tag_obj_buildingsidewalls
audio/_systems/audiotag/object/dgo_tag_obj_buildingvent
audio/_systems/audiotag/object/dgo_tag_obj_bush
audio/_systems/audiotag/object/dgo_tag_obj_busstop
audio/_systems/audiotag/object/dgo_tag_obj_cafe
audio/_systems/audiotag/object/dgo_tag_obj_can
audio/_systems/audiotag/object/dgo_tag_obj_canalbridge
audio/_systems/audiotag/object/dgo_tag_obj_cardboard_box
audio/_systems/audiotag/object/dgo_tag_obj_carstatic
audio/_systems/audiotag/object/dgo_tag_obj_casperpool
audio/_systems/audiotag/object/dgo_tag_obj_chairrecliner
audio/_systems/audiotag/object/dgo_tag_obj_church
audio/_systems/audiotag/object/dgo_tag_obj_churchbell
audio/_systems/audiotag/object/dgo_tag_obj_citybusparked
audio/_systems/audiotag/object/dgo_tag_obj_coin
audio/_systems/audiotag/object/dgo_tag_obj_collabzoneblock32
audio/_systems/audiotag/object/dgo_tag_obj_collabzoneentertainment
audio/_systems/audiotag/object/dgo_tag_obj_collabzonefinancial
audio/_systems/audiotag/object/dgo_tag_obj_collabzonehistoric
audio/_systems/audiotag/object/dgo_tag_obj_college
audio/_systems/audiotag/object/dgo_tag_obj_construction
audio/_systems/audiotag/object/dgo_tag_obj_constructionbarrier
audio/_systems/audiotag/object/dgo_tag_obj_constructionbarrierbroken
audio/_systems/audiotag/object/dgo_tag_obj_constructionbarrierwooden
audio/_systems/audiotag/object/dgo_tag_obj_couchleather
audio/_systems/audiotag/object/dgo_tag_obj_cranemusic
audio/_systems/audiotag/object/dgo_tag_obj_cranestructure
audio/_systems/audiotag/object/dgo_tag_obj_cricket_01
audio/_systems/audiotag/object/dgo_tag_obj_cricket_02
audio/_systems/audiotag/object/dgo_tag_obj_cricket_night
audio/_systems/audiotag/object/dgo_tag_obj_distant_babylizard
audio/_systems/audiotag/object/dgo_tag_obj_distant_churchbell
audio/_systems/audiotag/object/dgo_tag_obj_distant_diplodocus
audio/_systems/audiotag/object/dgo_tag_obj_distant_horse
audio/_systems/audiotag/object/dgo_tag_obj_distant_triceratops
audio/_systems/audiotag/object/dgo_tag_obj_dumpster
audio/_systems/audiotag/object/dgo_tag_obj_dumpster_01
audio/_systems/audiotag/object/dgo_tag_obj_dumpster_02
audio/_systems/audiotag/object/dgo_tag_obj_electricalbox
audio/_systems/audiotag/object/dgo_tag_obj_elevator
audio/_systems/audiotag/object/dgo_tag_obj_elonecourts
audio/_systems/audiotag/object/dgo_tag_obj_fakesource
audio/_systems/audiotag/object/dgo_tag_obj_fakesourcefromsend
audio/_systems/audiotag/object/dgo_tag_obj_fakesourcepostchallenge
audio/_systems/audiotag/object/dgo_tag_obj_fastfood_restaurant
audio/_systems/audiotag/object/dgo_tag_obj_federalist_rooftopramp
audio/_systems/audiotag/object/dgo_tag_obj_firestation
audio/_systems/audiotag/object/dgo_tag_obj_flies
audio/_systems/audiotag/object/dgo_tag_obj_flies_group
audio/_systems/audiotag/object/dgo_tag_obj_flower
audio/_systems/audiotag/object/dgo_tag_obj_fogmachine
audio/_systems/audiotag/object/dgo_tag_obj_foodcart
audio/_systems/audiotag/object/dgo_tag_obj_foodtruck
audio/_systems/audiotag/object/dgo_tag_obj_foodtruck_speaker
audio/_systems/audiotag/object/dgo_tag_obj_frogchorus
audio/_systems/audiotag/object/dgo_tag_obj_ftue_set01
audio/_systems/audiotag/object/dgo_tag_obj_ftue_set02
audio/_systems/audiotag/object/dgo_tag_obj_ftue_set03
audio/_systems/audiotag/object/dgo_tag_obj_ftue_set04
audio/_systems/audiotag/object/dgo_tag_obj_ftue_set05
audio/_systems/audiotag/object/dgo_tag_obj_funnel
audio/_systems/audiotag/object/dgo_tag_obj_gameplay_mission
audio/_systems/audiotag/object/dgo_tag_obj_garage
audio/_systems/audiotag/object/dgo_tag_obj_goldenspeaker
audio/_systems/audiotag/object/dgo_tag_obj_hvac
audio/_systems/audiotag/object/dgo_tag_obj_icecreamtruck
audio/_systems/audiotag/object/dgo_tag_obj_icecreamtruck_vo
audio/_systems/audiotag/object/dgo_tag_obj_industrialfan
audio/_systems/audiotag/object/dgo_tag_obj_infocenter
audio/_systems/audiotag/object/dgo_tag_obj_intercomspeaker
audio/_systems/audiotag/object/dgo_tag_obj_intersection
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_night
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set1
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set1b
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set2
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set3
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set3b
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set4
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_set5
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_spillway_lower
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_spillway_ocean
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_spillway_upper
audio/_systems/audiotag/object/dgo_tag_obj_isleofgrom_tunnel
audio/_systems/audiotag/object/dgo_tag_obj_katydid
audio/_systems/audiotag/object/dgo_tag_obj_kicker
audio/_systems/audiotag/object/dgo_tag_obj_large
audio/_systems/audiotag/object/dgo_tag_obj_leaves
audio/_systems/audiotag/object/dgo_tag_obj_mcorpbuilding_speaker
audio/_systems/audiotag/object/dgo_tag_obj_medium
audio/_systems/audiotag/object/dgo_tag_obj_metal
audio/_systems/audiotag/object/dgo_tag_obj_metal_chair
audio/_systems/audiotag/object/dgo_tag_obj_metal_hollow
audio/_systems/audiotag/object/dgo_tag_obj_metal_railing
audio/_systems/audiotag/object/dgo_tag_obj_metal_railing_structure
audio/_systems/audiotag/object/dgo_tag_obj_miniature-extravert
audio/_systems/audiotag/object/dgo_tag_obj_miniature-impervatowers
audio/_systems/audiotag/object/dgo_tag_obj_miniature-parkade
audio/_systems/audiotag/object/dgo_tag_obj_mobileconstructionoffice
audio/_systems/audiotag/object/dgo_tag_obj_moths
audio/_systems/audiotag/object/dgo_tag_obj_movietheatre_speaker
audio/_systems/audiotag/object/dgo_tag_obj_musicstore
audio/_systems/audiotag/object/dgo_tag_obj_newsbuilding
audio/_systems/audiotag/object/dgo_tag_obj_outdoorcinema
audio/_systems/audiotag/object/dgo_tag_obj_overpass
audio/_systems/audiotag/object/dgo_tag_obj_palmtree
audio/_systems/audiotag/object/dgo_tag_obj_paperstand_01
audio/_systems/audiotag/object/dgo_tag_obj_paperstand_02
audio/_systems/audiotag/object/dgo_tag_obj_parkade_interior_speaker
audio/_systems/audiotag/object/dgo_tag_obj_parkade_speaker
audio/_systems/audiotag/object/dgo_tag_obj_parkingbarriershort
audio/_systems/audiotag/object/dgo_tag_obj_parkingmeter
audio/_systems/audiotag/object/dgo_tag_obj_passageway
audio/_systems/audiotag/object/dgo_tag_obj_patio
audio/_systems/audiotag/object/dgo_tag_obj_pedestrian
audio/_systems/audiotag/object/dgo_tag_obj_pipe
audio/_systems/audiotag/object/dgo_tag_obj_pivotrooftop
audio/_systems/audiotag/object/dgo_tag_obj_plastic
audio/_systems/audiotag/object/dgo_tag_obj_plasticcup
audio/_systems/audiotag/object/dgo_tag_obj_player
audio/_systems/audiotag/object/dgo_tag_obj_popups
audio/_systems/audiotag/object/dgo_tag_obj_popups_high
audio/_systems/audiotag/object/dgo_tag_obj_popups_low
audio/_systems/audiotag/object/dgo_tag_obj_popups_med
audio/_systems/audiotag/object/dgo_tag_obj_port-a-potty
audio/_systems/audiotag/object/dgo_tag_obj_presentalley
audio/_systems/audiotag/object/dgo_tag_obj_propboombox
audio/_systems/audiotag/object/dgo_tag_obj_ramp
audio/_systems/audiotag/object/dgo_tag_obj_restaurant_french
audio/_systems/audiotag/object/dgo_tag_obj_restaurant_greek
audio/_systems/audiotag/object/dgo_tag_obj_restaurant_italian
audio/_systems/audiotag/object/dgo_tag_obj_rooftopplot12
audio/_systems/audiotag/object/dgo_tag_obj_rooftopplot3
audio/_systems/audiotag/object/dgo_tag_obj_rooftoprampsstadium
audio/_systems/audiotag/object/dgo_tag_obj_rooftoprampstritower
audio/_systems/audiotag/object/dgo_tag_obj_sanvanhotel
audio/_systems/audiotag/object/dgo_tag_obj_seagull_flock
audio/_systems/audiotag/object/dgo_tag_obj_season1_canalmusic
audio/_systems/audiotag/object/dgo_tag_obj_seawallentertainment01
audio/_systems/audiotag/object/dgo_tag_obj_seawallentertainment02
audio/_systems/audiotag/object/dgo_tag_obj_seawallentertainment03
audio/_systems/audiotag/object/dgo_tag_obj_seawallhistoric01
audio/_systems/audiotag/object/dgo_tag_obj_seawallhistoric02
audio/_systems/audiotag/object/dgo_tag_obj_seawallstadium01
audio/_systems/audiotag/object/dgo_tag_obj_seawallstadium02
audio/_systems/audiotag/object/dgo_tag_obj_skatepark
audio/_systems/audiotag/object/dgo_tag_obj_skatepark_speakerarray
audio/_systems/audiotag/object/dgo_tag_obj_skatepark_stadium
audio/_systems/audiotag/object/dgo_tag_obj_skateshop
audio/_systems/audiotag/object/dgo_tag_obj_small
audio/_systems/audiotag/object/dgo_tag_obj_snowmachine
audio/_systems/audiotag/object/dgo_tag_obj_speakerairy
audio/_systems/audiotag/object/dgo_tag_obj_speakergroup
audio/_systems/audiotag/object/dgo_tag_obj_stadium
audio/_systems/audiotag/object/dgo_tag_obj_stadium_exterior
audio/_systems/audiotag/object/dgo_tag_obj_stadium_interior
audio/_systems/audiotag/object/dgo_tag_obj_stadium_upperlevel
audio/_systems/audiotag/object/dgo_tag_obj_stairs
audio/_systems/audiotag/object/dgo_tag_obj_steelcable
audio/_systems/audiotag/object/dgo_tag_obj_streetlight
audio/_systems/audiotag/object/dgo_tag_obj_svnews_interior_speaker
audio/_systems/audiotag/object/dgo_tag_obj_takeoutcontainer
audio/_systems/audiotag/object/dgo_tag_obj_theatre
audio/_systems/audiotag/object/dgo_tag_obj_toad
audio/_systems/audiotag/object/dgo_tag_obj_trafficcone
audio/_systems/audiotag/object/dgo_tag_obj_trainstation
audio/_systems/audiotag/object/dgo_tag_obj_transformer
audio/_systems/audiotag/object/dgo_tag_obj_trash
audio/_systems/audiotag/object/dgo_tag_obj_trashcan
audio/_systems/audiotag/object/dgo_tag_obj_tree
audio/_systems/audiotag/object/dgo_tag_obj_tritowerplayground
audio/_systems/audiotag/object/dgo_tag_obj_truckinterior
audio/_systems/audiotag/object/dgo_tag_obj_truckstatic
audio/_systems/audiotag/object/dgo_tag_obj_tunnel
audio/_systems/audiotag/object/dgo_tag_obj_tunnelcreature
audio/_systems/audiotag/object/dgo_tag_obj_underthebridgeparkade
audio/_systems/audiotag/object/dgo_tag_obj_vehicle
audio/_systems/audiotag/object/dgo_tag_obj_vendingmachine
audio/_systems/audiotag/object/dgo_tag_obj_wallspeakers
audio/_systems/audiotag/object/dgo_tag_obj_warehouse
audio/_systems/audiotag/object/dgo_tag_obj_waterdrips
audio/_systems/audiotag/object/dgo_tag_obj_waterfall_bottom
audio/_systems/audiotag/object/dgo_tag_obj_waterfall_top
audio/_systems/audiotag/object/dgo_tag_obj_watertaxi
audio/_systems/audiotag/object/dgo_tag_obj_watertaxi_engine
audio/_systems/audiotag/object/dgo_tag_obj_watertaxi_engine_waterripples
audio/_systems/audiotag/object/dgo_tag_obj_windchime
audio/_systems/audiotag/object/dgo_tag_obj_window
audio/_systems/audiotag/object/dgo_tag_obj_window_music
audio/_systems/audiotag/object/dgo_tag_obj_wings_pigeon
audio/_systems/audiotag/object/dgo_tag_obj_woodpalette
audio/_systems/audiotag/region/dgo_spot_reg_isleofgrom_spillway_lower
audio/_systems/audiotag/region/dgo_spot_reg_isleofgrom_spillway_ocean
audio/_systems/audiotag/region/dgo_tag_reg_activities
audio/_systems/audiotag/region/dgo_tag_reg_building_church
audio/_systems/audiotag/region/dgo_tag_reg_building_generic
audio/_systems/audiotag/region/dgo_tag_reg_building_mall
audio/_systems/audiotag/region/dgo_tag_reg_building_parkade_historic
audio/_systems/audiotag/region/dgo_tag_reg_building_stadium
audio/_systems/audiotag/region/dgo_tag_reg_building_stadium_interior
audio/_systems/audiotag/region/dgo_tag_reg_district_downtown
audio/_systems/audiotag/region/dgo_tag_reg_elevatorhallway_svnnews_historic
audio/_systems/audiotag/region/dgo_tag_reg_ftue_set01
audio/_systems/audiotag/region/dgo_tag_reg_ftue_set02
audio/_systems/audiotag/region/dgo_tag_reg_ftue_set03
audio/_systems/audiotag/region/dgo_tag_reg_ftue_set04
audio/_systems/audiotag/region/dgo_tag_reg_ftue_set05
audio/_systems/audiotag/region/dgo_tag_reg_ftue-island
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set01
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set01b
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set02
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set03
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set03b
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set4
audio/_systems/audiotag/region/dgo_tag_reg_isleofgrom_set5
audio/_systems/audiotag/region/dgo_tag_reg_megaramp_federalistblock
audio/_systems/audiotag/region/dgo_tag_reg_mpr
audio/_systems/audiotag/region/dgo_tag_reg_mpr_beach
audio/_systems/audiotag/region/dgo_tag_reg_mpr_chalet
audio/_systems/audiotag/region/dgo_tag_reg_mpr_concourse
audio/_systems/audiotag/region/dgo_tag_reg_mpr_forestpark
audio/_systems/audiotag/region/dgo_tag_reg_mpr_lowbowls
audio/_systems/audiotag/region/dgo_tag_reg_mpr_lowramps
audio/_systems/audiotag/region/dgo_tag_reg_mpr_mountain
audio/_systems/audiotag/region/dgo_tag_reg_mpr_pond
audio/_systems/audiotag/region/dgo_tag_reg_mpr_ravine
audio/_systems/audiotag/region/dgo_tag_reg_mpr_upperramps
audio/_systems/audiotag/region/dgo_tag_reg_mpr_walkway
audio/_systems/audiotag/region/dgo_tag_reg_nbhd_entertainmenthub
audio/_systems/audiotag/region/dgo_tag_reg_nbhd_financial
audio/_systems/audiotag/region/dgo_tag_reg_nbhd_historic
audio/_systems/audiotag/region/dgo_tag_reg_nbhd_stadium
audio/_systems/audiotag/region/dgo_tag_reg_plot_alley
audio/_systems/audiotag/region/dgo_tag_reg_plot_halloweenpark
audio/_systems/audiotag/region/dgo_tag_reg_plot_park
audio/_systems/audiotag/region/dgo_tag_reg_plot_pier
audio/_systems/audiotag/region/dgo_tag_reg_plot_plaza
audio/_systems/audiotag/region/dgo_tag_reg_plot_residential
audio/_systems/audiotag/region/dgo_tag_reg_plot_seawall
audio/_systems/audiotag/region/dgo_tag_reg_plot_shops
audio/_systems/audiotag/region/dgo_tag_reg_plot_skatepaddys
audio/_systems/audiotag/region/dgo_tag_reg_plot_skatepark
audio/_systems/audiotag/region/dgo_tag_reg_plot_winterpark
audio/_systems/audiotag/region/dgo_tag_reg_popups
audio/_systems/audiotag/region/dgo_tag_reg_spot_amphitheater
audio/_systems/audiotag/region/dgo_tag_reg_spot_blockparty
audio/_systems/audiotag/region/dgo_tag_reg_spot_casperhotel
audio/_systems/audiotag/region/dgo_tag_reg_spot_cementery
audio/_systems/audiotag/region/dgo_tag_reg_spot_collabzone_plot32
audio/_systems/audiotag/region/dgo_tag_reg_spot_firestation
audio/_systems/audiotag/region/dgo_tag_reg_spot_funnel
audio/_systems/audiotag/region/dgo_tag_reg_spot_halloweenpark_underground
audio/_systems/audiotag/region/dgo_tag_reg_spot_pivotrooftop
audio/_systems/audiotag/region/dgo_tag_reg_spot_playground
audio/_systems/audiotag/region/dgo_tag_reg_spot_rooftop_plot03
audio/_systems/audiotag/region/dgo_tag_reg_spot_rooftop_plot12
audio/_systems/audiotag/region/dgo_tag_reg_spot_season1_canal
audio/_systems/audiotag/region/dgo_tag_reg_spot_warehouse_empty
audio/_systems/audiotag/region/dgo_tag_reg_vehicleunderpass
audio/_systems/audiotag/region/dgo_tag_reg_water_canal
audio/_systems/audiotag/region/dgo_tag_reg_water_ocean
audio/_systems/audiotag/region/dgo_tag_reg_water_river
audio/_systems/audiotag/region/dgo_tag_reg_water_river_underground
audio/_systems/audiotag/region/dgo_tag_reg_water_waterfall
audio/_systems/audiotag/region/dgo_tag_spot_isleofgrom_spillway_upper
audio/_systems/audiotag/region/dgo_tag_spot_isleofgrom_tunnel
audio/_systems/audiotag/region/dgo_tag_spot_parkinggarage_flowtopia
audio/_systems/audiotag/region/dgo_tag_spot_parkinggarage_sanvannews
""".splitlines()


def _audio_tag_items(group):
    prefix = "audio/_systems/audiotag/" + group + "/"
    items = []
    for path in AUDIO_TAG_PATHS:
        if not path.startswith(prefix):
            continue
        leaf = path[len(prefix):]
        label = leaf.removeprefix("dgo_tag_").replace("_", " ").title()
        number = int(hashlib.sha256(path.encode("utf-8")).hexdigest()[:8], 16) & 0xffffff
        items.append((path, label, path, number))
    if len({row[3] for row in items}) != len(items):
        raise ValueError("Audio tag identifiers must be unique")
    return items


AUDIO_REGION_TAGS = _audio_tag_items("region")
AUDIO_OBJECT_TAGS = _audio_tag_items("object")


def _audio_tag_aliases(items):
    aliases = {}
    for path, _label, _description, number in items:
        legacy = int(hashlib.sha256(path.encode("utf-8")).hexdigest()[:8], 16) & 0x7fffffff
        rounded = int(struct.unpack("<f", struct.pack("<f", legacy))[0])
        for value in (path, number, legacy, rounded):
            aliases.setdefault(value, set()).add(path)
    return {value: next(iter(paths)) for value, paths in aliases.items() if len(paths) == 1}


AUDIO_REGION_ALIASES = _audio_tag_aliases(AUDIO_REGION_TAGS)
AUDIO_OBJECT_ALIASES = _audio_tag_aliases(AUDIO_OBJECT_TAGS)
AUDIO_REGION_NUMBERS = {row[0]: row[3] for row in AUDIO_REGION_TAGS}
AUDIO_OBJECT_NUMBERS = {row[0]: row[3] for row in AUDIO_OBJECT_TAGS}


def _audio_region_path(obj):
    stored = getattr(obj, "sk8_audio", None)
    if stored is None:
        stored = obj.get("sk8_audio", {})
    return (AUDIO_REGION_ALIASES.get(stored.get("region_tag"), "")
            or AUDIO_REGION_ALIASES.get(obj.get("sk8_audio_region_tag"), ""))


def _audio_emitter_paths(obj):
    live = getattr(obj, "sk8_audio_emitter", None)
    stored = live if live is not None else obj.get("sk8_audio_emitter", {})
    rows = live.tags if live is not None else stored.get("tags", [])
    try:
        saved = json.loads(obj.get("sk8_audio_emitter_tags", "[]"))
        if not isinstance(saved, list):
            saved = []
    except (ValueError, TypeError):
        saved = []
    paths = []
    count = len(rows) if live is not None or "tags" in stored else len(saved)
    for index in range(count):
        raw = rows[index].get("tag") if index < len(rows) else None
        path = AUDIO_OBJECT_ALIASES.get(raw, "")
        if raw is None and index < len(rows):
            path = "audio/_systems/audiotag/object/dgo_tag_obj_tree"
        if not path and index < len(saved) and isinstance(saved[index], str):
            path = AUDIO_OBJECT_ALIASES.get(saved[index], "")
        paths.append(path)
    return paths


@bpy.app.handlers.persistent
def _migrate_audio_tags(_scene=None):
    for obj in bpy.data.objects:
        if not obj.is_editable:
            continue
        volume = getattr(obj, "sk8_audio", None)
        if volume is None:
            volume = obj.get("sk8_audio")
        if volume is not None:
            path = _audio_region_path(obj)
            if path:
                volume["region_tag"] = AUDIO_REGION_NUMBERS[path]
                obj["sk8_audio_region_tag"] = path
        emitter = getattr(obj, "sk8_audio_emitter", None)
        if emitter is None:
            emitter = obj.get("sk8_audio_emitter")
        if emitter is not None:
            paths = _audio_emitter_paths(obj)
            for row, path in zip(obj.sk8_audio_emitter.tags, paths):
                if path:
                    row["tag"] = AUDIO_OBJECT_NUMBERS[path]
            if paths:
                obj["sk8_audio_emitter_tags"] = json.dumps(paths)


AUDIO_PRESETS = {
    "tunnel": ("audio/amb/bed/indoor/tunnel/dgo_amb_bed_indoor_tunnel_small_behaviorasset",
               "audio/amb/_system/densitygroups/dgo_dg_groundbeds"),
    "drips": ("audio/amb/lbw/water/drips/dgo_amb_lbw_water_drip_light_behaviorasset",
              "audio/amb/_system/densitygroups/dgo_dg_water"),
}


def _sync_audio_settings(self, _context):
    if self.enabled:
        self.id_data.empty_display_type = "CUBE"
    for key in ("enabled", "preset", "region_tag", "behavior", "density_group", "activation_distance",
                "indooriness", "density", "priority", "additive", "allow_in_child_regions"):
        value = getattr(self, key)
        if key == "region_tag" and not value:
            value = _audio_region_path(self.id_data)
        self.id_data["sk8_audio_" + key] = value


class Sk8AudioSettings(PropertyGroup):
    enabled: BoolProperty(name="Audio Volume", default=False, update=_sync_audio_settings)
    preset: EnumProperty(name="Ambience", items=[
        ("tunnel", "Tunnel Room Tone", "Native looping tunnel ambience", 0),
        ("drips", "Water Drips", "Native intermittent water drips", 1),
        ("custom", "Custom BehaviorAsset", "Paste a region-based audio BehaviorAsset path", 2),
        ("region", "Native Region", "Use a game AudioRegionTag and its native ambience assignments", 3)],
        default="tunnel", update=_sync_audio_settings)
    region_tag: EnumProperty(name="Region Tag", items=AUDIO_REGION_TAGS,
        default="audio/_systems/audiotag/region/dgo_tag_reg_mpr_beach", update=_sync_audio_settings)
    behavior: StringProperty(name="BehaviorAsset", update=_sync_audio_settings)
    density_group: StringProperty(name="Density Group",
        default="audio/amb/_system/densitygroups/dgo_dg_groundbeds", update=_sync_audio_settings)
    activation_distance: FloatProperty(name="Activation Distance (m)", default=15, min=0,
        description="Distance outside the region at which its controller activates", update=_sync_audio_settings)
    indooriness: FloatProperty(name="Indooriness", default=1, min=0, max=1, update=_sync_audio_settings)
    density: FloatProperty(name="Density", default=1, min=0,
        description="Native behavior scheduling density, not volume gain", update=_sync_audio_settings)
    priority: IntProperty(name="Priority", default=6, min=-1, update=_sync_audio_settings)
    additive: BoolProperty(name="Additive", default=False,
        description="Blend with other regions instead of competing by priority", update=_sync_audio_settings)
    allow_in_child_regions: BoolProperty(name="Continue in Child Regions", default=False, update=_sync_audio_settings)


def audio_volume_records(depsgraph, scale, selected=None):
    output = []
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "EMPTY":
            continue
        original = getattr(obj, "original", None) or obj
        stored = original.get("sk8_audio", {})

        def setting(key, default):
            live = getattr(original, "sk8_audio", None)
            if live is not None and live.is_property_set(key):
                return getattr(live, key)
            return original.get("sk8_audio_" + key, stored.get(key, default))

        if not setting("enabled", False) or original.hide_render:
            continue
        if selected is not None and original not in selected:
            continue
        matrix = Matrix(item.matrix_world)
        axes = matrix.to_3x3()
        if (axes.col[0].length == 0 or axes.col[1].length == 0 or axes.col[2].length == 0
                or abs(axes.col[0].normalized().z) > 1e-5
                or abs(axes.col[1].normalized().z) > 1e-5
                or abs(abs(axes.col[2].normalized().z) - 1) > 1e-5):
            raise ValueError(f"Audio volume '{original.name}': use a nonzero box with Z rotation only")
        size = float(obj.empty_display_size)
        half_height = abs(axes.col[2].z) * size
        base = (matrix.translation.z - half_height) * scale
        points = []
        for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            point = matrix @ Vector((x * size, y * size, 0))
            points.append([float(point.x * scale), float(base), float(-point.y * scale)])
        preset = setting("preset", "tunnel")
        if isinstance(preset, int):
            preset = {0: "tunnel", 1: "drips", 2: "custom", 3: "region"}.get(preset, "tunnel")
        region_tag = ""
        if preset == "region":
            region_tag = (_audio_region_path(original) or AUDIO_REGION_ALIASES.get(
                setting("region_tag", "audio/_systems/audiotag/region/dgo_tag_reg_mpr_beach"), ""))
            if not region_tag:
                raise ValueError(f"Audio volume '{original.name}': reselect its region tag")
        behavior, group = AUDIO_PRESETS.get(preset, (setting("behavior", ""),
            setting("density_group", "audio/amb/_system/densitygroups/dgo_dg_groundbeds")))
        if not behavior.strip() and not region_tag:
            raise ValueError(f"Audio volume '{original.name}': select an ambience BehaviorAsset")
        output.append({"name": original.name, "footprint": points,
            "height": 2 * half_height * scale, "behavior": behavior.strip(), "density_group": group.strip(),
            "region_tag": region_tag,
            "activation_distance": float(setting("activation_distance", 15)) * scale,
            "indooriness": float(setting("indooriness", 1)), "density": float(setting("density", 1)),
            "priority": int(setting("priority", 6)), "additive": bool(setting("additive", False)),
            "allow_in_child_regions": bool(setting("allow_in_child_regions", False))})
    output.sort(key=lambda row: (row["name"], row["footprint"]))
    return output


class SK8_OT_add_audio_volume(Operator):
    bl_idname = "sk8.add_audio_volume"
    bl_label = "Add Audio Volume"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        obj = bpy.data.objects.new("Audio Volume", None)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.empty_display_type = "CUBE"
        obj.empty_display_size = 1
        obj.scale = (10, 10, 5)
        obj.show_in_front = True
        obj.sk8_audio.enabled = True
        for other in ctx.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        return {"FINISHED"}


class SK8_PT_sidebar_audio_volume(Panel):
    bl_order = 20
    bl_label = "Ambient Audio Volume"
    bl_idname = "SK8_PT_sidebar_audio_volume"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.type == "EMPTY" and ctx.object.name.casefold() != "spawn"

    def draw(self, ctx):
        s = ctx.object.sk8_audio
        col = self.layout.column()
        col.prop(s, "enabled")
        if not s.enabled:
            return
        col.label(text="Move / scale the box; rotate around Z.")
        col.prop(s, "preset")
        if s.preset == "region":
            col.prop(s, "region_tag")
            col.label(text="Uses native ambience and density settings.")
            col.label(text="Unassigned tags mark regions only.")
            return
        if s.preset == "custom":
            col.prop(s, "behavior")
            col.prop(s, "density_group")
        for key in ("activation_distance", "indooriness", "density", "priority", "additive", "allow_in_child_regions"):
            col.prop(s, key)


def _sync_audio_emitter(self, _context):
    obj = self.id_data
    settings = obj.sk8_audio_emitter
    obj["sk8_audio_emitter_enabled"] = settings.enabled
    obj["sk8_audio_emitter_tags"] = json.dumps(_audio_emitter_paths(obj))


class Sk8AudioEmitterTag(PropertyGroup):
    tag: EnumProperty(name="Object Tag", items=AUDIO_OBJECT_TAGS,
        default="audio/_systems/audiotag/object/dgo_tag_obj_tree", update=_sync_audio_emitter)


class Sk8AudioEmitterSettings(PropertyGroup):
    enabled: BoolProperty(name="Audio Emitter Tags", default=False, update=_sync_audio_emitter)
    tags: CollectionProperty(type=Sk8AudioEmitterTag)


def audio_records(depsgraph, scale, selected=None):
    registered = []
    properties = []
    try:
        if not hasattr(bpy.types.Object, "sk8_audio"):
            bpy.utils.register_class(Sk8AudioSettings)
            registered.append(Sk8AudioSettings)
            bpy.types.Object.sk8_audio = PointerProperty(type=Sk8AudioSettings)
            properties.append("sk8_audio")
        if not hasattr(bpy.types.Object, "sk8_audio_emitter"):
            for cls in (Sk8AudioEmitterTag, Sk8AudioEmitterSettings):
                bpy.utils.register_class(cls)
                registered.append(cls)
            bpy.types.Object.sk8_audio_emitter = PointerProperty(type=Sk8AudioEmitterSettings)
            properties.append("sk8_audio_emitter")
        return (audio_volume_records(depsgraph, scale, selected),
                audio_emitter_records(depsgraph, scale, selected))
    finally:
        for name in reversed(properties):
            delattr(bpy.types.Object, name)
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)


def interaction_prefab_records(depsgraph, scale, selected=None):
    output = []
    conversion = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "EMPTY":
            continue
        original = getattr(obj, "original", None) or obj
        name = re.sub(r"\.\d{3,}$", "", original.name).casefold()
        if original.get("sk8_native_behavior", ""):
            continue
        if not name.endswith("_prefab") or original.hide_render:
            continue
        if selected is not None and original not in selected:
            continue
        kind = name[:-len("_prefab")]
        if kind not in {"speaker", "grabster", "grabbster"}:
            raise ValueError(f"Unknown interaction '{original.name}': use speaker_prefab or grabster_prefab")
        live = getattr(original, "sk8_audio_emitter", None)
        enabled = live.enabled if live is not None else original.get("sk8_audio_emitter_enabled", False)
        if enabled:
            raise ValueError(f"'{original.name}' already creates a speaker source; disable its Object Audio Emitter")
        matrix = Matrix(item.matrix_world)
        basis = conversion @ matrix.to_quaternion().to_matrix() @ conversion.transposed()
        position = conversion @ matrix.translation * scale
        output.append({"name": original.name, "type": kind, "position": list(position),
                       "basis": [list(basis.col[i]) for i in range(3)]})
    output.sort(key=lambda row: (row["name"], row["position"]))
    return output


NATIVE_TRIGGER_PRESETS = (
    ("Snow pile (medium)", "effects/season02/prefabs/spf_s2_event_snowpile_md"),
    ("Snow pile (large)", "effects/season02/prefabs/spf_s2_event_snowpile_lrg"),
    ("Leaf pile", "effects/environments/prefabs/cmn_prp_event_02_leafpile_01_pfx"),
    ("Clovers", "effects/season03/prefabs/spf_spd_env_event_02_clovers_01"),
    ("Coin pile", "effects/season03/prefabs/spf_s3_spd_2026_coinpile_01"),
    ("Tunnel falling dust", "effects/environments/prefabs/triggered/spf_dyn_tunnel_falling_dust"),
    ("Camera flashes", "effects/environments/prefabs/spf_fx_triggered_camera_flashes_xgames"),
)


NATIVE_VFX_PRESETS = (
    ("Manhole smoke", "effects/environments/effectblueprints/ebp_env_smoke_manhole_01"),
    ("Vent smoke", "effects/environments/effectblueprints/ebp_env_ventsmoke_aircond_01"),
    ("Steam", "effects/environments/effectblueprints/ebp_env_steam_hotdogstand_01"),
    ("Fire hydrant spray", "effects/gameplay/effectblueprints/eb_firehydrant_loop_a"),
    ("Falling snow (20 m)", "effects/season02/effectblueprints/ebp_s2_snow_falling_20m_outside_hemisphere"),
    ("Snow machine (prefab)", "effects/season02/prefabs/spf_s2_fx_snowmachine"),
)


_VFX_CATALOG_ROWS = {}

VFX_CATEGORIES = (
    ("ALL", "All categories", ""),
    ("ENVIRONMENT", "Environment", "Smoke, water, wildlife, weather and lighting"),
    ("SEASONAL", "Seasonal", "Seasonal and event effects; search a season number to narrow further"),
    ("CHARACTERS", "Characters", "Costume and character effects"),
    ("GAMEPLAY", "Gameplay", "Activity and gameplay effects"),
    ("UI", "UI / Markers", "Beacons, arrows and other markers"),
    ("QUESTS", "Quest splines", "Quest-specific directional spline effects"),
    ("PROPS", "Props", "Effect-bearing world props"),
    ("OTHER", "Other", "Other effect sources"),
)


def _vfx_display_info(path):
    category, category_label = "OTHER", "Other"
    if path.startswith("effects/"):
        folder = path.split("/")[1]
        season = re.fullmatch(r"season_?(\d+)", folder)
        if season:
            category, category_label = "SEASONAL", f"Season {int(season[1])}"
        else:
            category, category_label = {
                "environments": ("ENVIRONMENT", "Environment"),
                "characters": ("CHARACTERS", "Characters"),
                "gameplay": ("GAMEPLAY", "Gameplay"),
                "ui": ("UI", "UI / Markers"),
            }.get(folder, (category, category_label))
    elif path.startswith("activities/"):
        category, category_label = "QUESTS", "Quest splines"
    elif path.startswith("world/"):
        category, category_label = "PROPS", "Props"
    leaf = path.rsplit("/", 1)[-1]
    generated = leaf.endswith("_nongroupable_autogen")
    leaf = leaf.removesuffix("_nongroupable_autogen")
    leaf = re.sub(r"^(?:(?:ebp|eb|spf|spacial|sbp|pfb|pf|fx|env|chr|cmn|prp|dyn|act|ui|pc|s\d+)_)+", "", leaf)
    leaf = re.sub(r"_pfx$", "", leaf)
    leaf = leaf.replace("activityspline_base_", "quest_spline_").replace("activityspline_", "quest_spline_")
    for compact, readable in {
        "chimneysmoke": "chimney_smoke", "ventsmoke": "vent_smoke", "aircond": "air_conditioner",
        "buildingvent": "building_vent", "firehydrant": "fire_hydrant", "snowpile": "snow_pile",
        "snowmachine": "snow_machine", "coinpile": "coin_pile", "leafpile": "leaf_pile",
        "hotdogstand": "hot_dog_stand", "foodtrailer": "food_trailer", "cameraflashes": "camera_flashes",
        "lightrays": "light_rays", "lightray": "light_ray", "impactdust": "impact_dust",
        "watertaxi": "water_taxi", "lowvelocity": "low_velocity", "quarterring": "quarter_ring",
        "highrate": "high_rate", "waterdrop": "water_drop", "trashbags": "trash_bags",
    }.items():
        leaf = leaf.replace(compact, readable)
    words = re.split(r"[_\s]+", leaf)
    expand = {"lrg": "Large", "md": "Medium", "med": "Medium", "sm": "Small", "ac": "AC", "ui": "UI"}
    title = " ".join(expand.get(word, word.upper() if re.fullmatch(r"(?:ch|m)\d+", word) else word.capitalize())
                     for word in words if word)
    return title or path.rsplit("/", 1)[-1], category, category_label, generated


def _vfx_matches(entry, search, category, kind, show_generated, triggers_only=False):
    if triggers_only and not entry.get("trigger"):
        return False
    if not show_generated and entry["generated"]:
        return False
    if category != "ALL" and entry["category"] != category:
        return False
    if kind == "DIRECT" and entry["type"] != "EffectBlueprint":
        return False
    if kind == "PREFAB" and entry["type"] != "SpatialPrefabBlueprint":
        return False
    if kind == "TRIGGER" and not entry.get("trigger"):
        return False
    searchable = " ".join((entry["title"], entry["blueprint"], entry["category_label"], entry["kind_label"])).casefold()
    return all(word in searchable for word in search.casefold().split())


def _vfx_catalog_path():
    return os.path.join(bpy.utils.user_resource("CONFIG"), "sk8_vfx_catalog.json")


def _read_vfx_catalog(path):
    if os.path.getsize(path) > 16 * 1024 * 1024:
        raise ValueError("VFX catalog is too large")
    with open(path, "r", encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("Unsupported VFX catalog format")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("VFX catalog contains no placeable effects")
    rows = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid VFX catalog entry")
        path = entry.get("blueprint")
        kind = entry.get("type")
        if (not isinstance(path, str) or not path or len(path) > 1024 or
                kind not in {"EffectBlueprint", "SpatialPrefabBlueprint"} or
                not isinstance(entry.get("trigger", False), bool) or
                not isinstance(entry.get("note", ""), str)):
            raise ValueError("Invalid VFX catalog blueprint")
        if path in rows:
            raise ValueError("Duplicate VFX catalog blueprint: " + path)
        rows[path] = entry
    return rows


def _load_vfx_catalog():
    global _VFX_CATALOG_ROWS
    rows = _read_vfx_catalog(_vfx_catalog_path())
    for path, entry in rows.items():
        entry["title"], entry["category"], entry["category_label"], entry["generated"] = _vfx_display_info(path)
        entry["kind_label"] = "Trigger prefab" if entry.get("trigger") else (
            "Prefab" if entry["type"] == "SpatialPrefabBlueprint" else "Effect")
    _VFX_CATALOG_ROWS = dict(sorted(rows.items(), key=lambda row:
        (row[1]["title"].casefold(), row[1]["category_label"], row[1]["generated"], row[0])))


class SK8_PG_vfx_choice(PropertyGroup):
    blueprint: StringProperty()
    category: StringProperty()
    kind: StringProperty()
    generated: BoolProperty()


class SK8_UL_vfx_choices(UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, active_propname, index):
        split = layout.split(factor=0.68)
        split.label(text=item.name, icon="PARTICLES" if item.kind == "Effect" else "OUTLINER_OB_EMPTY")
        meta = split.split(factor=0.52)
        meta.label(text=item.category)
        meta.label(text="Generated" if item.generated else item.kind)


def _select_vfx_choice(self, ctx):
    if 0 <= self.active_index < len(self.choices):
        self.blueprint = self.choices[self.active_index].blueprint
    else:
        self.blueprint = ""


def _filter_vfx_choices(self, ctx):
    selected = self.blueprint
    self.choices.clear()
    active = 0
    for path, entry in _VFX_CATALOG_ROWS.items():
        if not _vfx_matches(entry, self.search, self.category, self.kind, self.show_generated, self.triggers_only):
            continue
        item = self.choices.add()
        item.name, item.blueprint = entry["title"], path
        item.category, item.kind, item.generated = entry["category_label"], entry["kind_label"], entry["generated"]
        if path == selected:
            active = len(self.choices) - 1
    self.active_index = active
    _select_vfx_choice(self, ctx)


class SK8_OT_find_native_vfx(Operator):
    bl_idname = "sk8.find_native_vfx"
    bl_label = "Browse Native VFX"
    bl_description = "Search the installed game's complete VFX catalog and add an Empty at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "search"
    triggers_only: BoolProperty(default=False, options={"HIDDEN"})
    blueprint: StringProperty(name="Asset path", options={"HIDDEN"})
    search: StringProperty(name="Search", description="Search readable names or original asset paths",
                           options={"TEXTEDIT_UPDATE", "SKIP_SAVE"}, update=_filter_vfx_choices)
    category: EnumProperty(name="Category", items=VFX_CATEGORIES, default="ALL", update=_filter_vfx_choices)
    kind: EnumProperty(name="Type", items=(("ALL", "All types", ""), ("DIRECT", "Effects", ""),
                       ("PREFAB", "Prefabs", ""), ("TRIGGER", "Trigger prefabs", "")),
                       default="ALL", update=_filter_vfx_choices)
    show_generated: BoolProperty(name="Generated variants", default=False, update=_filter_vfx_choices,
                                  description="Also show automatically generated variants of the same prefab")
    choices: CollectionProperty(type=SK8_PG_vfx_choice, options={"HIDDEN", "SKIP_SAVE"})
    active_index: IntProperty(default=0, options={"HIDDEN", "SKIP_SAVE"}, update=_select_vfx_choice)

    def invoke(self, ctx, _event):
        try:
            _load_vfx_catalog()
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, f"Install/update the add-on from Studio with the game connected, or import a VFX catalog: {error}")
            return {"CANCELLED"}
        if self.triggers_only and not any(entry.get("trigger") for entry in _VFX_CATALOG_ROWS.values()):
            self.report({"WARNING"}, "This catalog has no native trigger prefabs")
            return {"CANCELLED"}
        _filter_vfx_choices(self, ctx)
        options = {"width": 820}
        if "confirm_text" in bpy.types.WindowManager.bl_rna.functions["invoke_props_dialog"].parameters:
            options["confirm_text"] = "Add at Cursor"
        return ctx.window_manager.invoke_props_dialog(self, **options)

    def draw(self, ctx):
        layout = self.layout
        layout.use_property_split = False
        layout.prop(self, "search", text="", icon="VIEWZOOM")
        filters = layout.row(align=True)
        filters.prop(self, "category", text="")
        if not self.triggers_only:
            filters.prop(self, "kind", text="")
        filters.prop(self, "show_generated")
        header = layout.split(factor=0.68)
        header.label(text=f"Name  ({len(self.choices)} results)")
        columns = header.split(factor=0.52)
        columns.label(text="Category")
        columns.label(text="Type")
        layout.template_list("SK8_UL_vfx_choices", "", self, "choices", self, "active_index", rows=12)
        entry = _VFX_CATALOG_ROWS.get(self.blueprint)
        details = layout.box()
        if entry:
            details.label(text=entry["title"], icon="INFO")
            details.label(text=f"{entry['category_label']}  /  {entry['kind_label']}" +
                          ("  /  Generated variant" if entry["generated"] else ""))
            for line in textwrap.wrap(self.blueprint, width=105):
                details.label(text=line)
            notes = details.column(align=True)
            notes.enabled = False
            for line in textwrap.wrap(entry.get("note", ""), width=105):
                notes.label(text=line)
        else:
            details.label(text="No matching effects. Change the search or filters.", icon="INFO")

    def execute(self, ctx):
        if not _VFX_CATALOG_ROWS:
            try:
                _load_vfx_catalog()
            except (OSError, ValueError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
        entry = _VFX_CATALOG_ROWS.get(self.blueprint)
        if entry is None:
            self.report({"ERROR"}, "Select an effect from the catalog")
            return {"CANCELLED"}
        label = entry["title"]
        result = bpy.ops.sk8.add_native_behavior(blueprint=self.blueprint, label=label,
                                                vfx=not self.triggers_only)
        if "FINISHED" in result:
            self.report({"INFO"}, entry.get("note", "Native VFX marker added"))
        return result


class SK8_OT_import_vfx_catalog(Operator):
    bl_idname = "sk8.import_vfx_catalog"
    bl_label = "Import VFX Catalog"
    bl_description = "Import a catalog dumped by Studio's vfx-catalog command"
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, ctx, _event):
        ctx.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, ctx):
        temporary = None
        try:
            rows = _read_vfx_catalog(self.filepath)
            destination = _vfx_catalog_path()
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            import tempfile
            with tempfile.NamedTemporaryFile(mode="wb", dir=os.path.dirname(destination),
                                             prefix="sk8_vfx_", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                with open(self.filepath, "rb") as source:
                    shutil.copyfileobj(source, stream)
            os.replace(temporary, destination)
            temporary = None
            _load_vfx_catalog()
            self.report({"INFO"}, f"Loaded {len(rows)} native VFX blueprints")
            return {"FINISHED"}
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)


def native_behavior_records(depsgraph, scale, selected=None):
    """Native prefab markers, shared by the add-on and Studio's headless import."""
    triggers, vfx = [], []
    conversion = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Native behavior export requires a positive scene scale")
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None:
            continue
        original = getattr(obj, "original", None) or obj
        kind = original.get("sk8_native_behavior", "")
        if not kind or original.hide_render or (selected is not None and original not in selected):
            continue
        if original.type != "EMPTY":
            raise ValueError(f"Native behavior '{original.name}' must be an Empty")
        if kind not in {"trigger_effect", "vfx"}:
            raise ValueError(f"Unknown native behavior '{kind}' on '{original.name}'")
        matrix = Matrix(item.matrix_world)
        if not all(math.isfinite(v) for row in matrix for v in row):
            raise ValueError(f"Native behavior '{original.name}' has a non-finite transform")
        basis = conversion @ matrix.to_3x3() @ conversion.transposed()
        position = conversion @ matrix.translation * scale
        if kind in {"trigger_effect", "vfx"}:
            key = "sk8_native_vfx" if kind == "vfx" else "sk8_native_trigger"
            blueprint = str(original.get(key, "")).strip().replace("\\", "/").lower()
            if not blueprint:
                raise ValueError(f"Native effect '{original.name}' needs a shipped blueprint path")
            if basis.determinant() <= 1e-8:
                raise ValueError(f"Native effect '{original.name}' needs a non-mirrored, nonzero scale")
            transform = [float(v * scale) for i in range(3) for v in basis.col[i]] + list(position)
            output = vfx if kind == "vfx" else triggers
            output.append({"name": original.name, "blueprint": blueprint, "transform": transform})
    triggers.sort(key=lambda row: (row["name"], row["transform"]))
    vfx.sort(key=lambda row: (row["name"], row["transform"]))
    return triggers, vfx


class SK8_OT_add_native_behavior(Operator):
    bl_idname = "sk8.add_native_behavior"
    bl_label = "Add Native Behavior"
    bl_description = "Place an existing retail VFX or trigger-effect prefab"
    bl_options = {"REGISTER", "UNDO"}
    blueprint: StringProperty()
    vfx: BoolProperty(default=False)
    label: StringProperty(default="Native trigger effect")

    def execute(self, ctx):
        obj = bpy.data.objects.new(self.label, None)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.empty_display_type = "SPHERE" if self.vfx else "CUBE"
        obj.empty_display_size = 0.5
        obj.show_in_front = True
        obj["sk8_native_behavior"] = "vfx" if self.vfx else "trigger_effect"
        obj["sk8_native_vfx" if self.vfx else "sk8_native_trigger"] = self.blueprint
        for selected in ctx.selected_objects:
            selected.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        return {"FINISHED"}


class SK8_MT_native_trigger_effects(Menu):
    bl_label = "Native Trigger Effects"
    bl_idname = "SK8_MT_native_trigger_effects"

    def draw(self, ctx):
        op = self.layout.operator("sk8.find_native_vfx", text="Search All Native Trigger Effects...", icon="VIEWZOOM")
        op.triggers_only = True
        self.layout.separator()
        for label, path in NATIVE_TRIGGER_PRESETS:
            op = self.layout.operator("sk8.add_native_behavior", text=label)
            op.label, op.blueprint = label, path
        self.layout.separator()
        self.layout.operator("sk8.add_native_behavior", text="Other Existing Trigger Prefab...")


class SK8_MT_native_vfx(Menu):
    bl_label = "Native VFX / Particles"
    bl_idname = "SK8_MT_native_vfx"

    def draw(self, ctx):
        self.layout.operator("sk8.find_native_vfx", text="Search All Native VFX...", icon="VIEWZOOM")
        self.layout.operator("sk8.import_vfx_catalog", text="Import VFX Catalog...", icon="IMPORT")
        self.layout.separator()
        for label, path in NATIVE_VFX_PRESETS:
            op = self.layout.operator("sk8.add_native_behavior", text=label)
            op.label, op.blueprint, op.vfx = label, path, True
        self.layout.separator()
        op = self.layout.operator("sk8.add_native_behavior", text="Other Existing VFX Prefab...")
        op.vfx, op.label = True, "Native VFX"


class SK8_PT_native_behavior(Panel):
    bl_label = "Native Behavior"
    bl_idname = "SK8_PT_native_behavior"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.get("sk8_native_behavior", "") in {"trigger_effect", "vfx"}

    def draw(self, ctx):
        layout = self.layout
        obj = ctx.object
        if obj.get("sk8_native_behavior") == "vfx":
            layout.prop(obj, '["sk8_native_vfx"]', text="Blueprint")
            layout.label(text="EffectBlueprint or SpatialPrefabBlueprint.")
            layout.label(text="Direct effects start when the level loads.")
            layout.label(text="Native looping/lifetime is retained.")
            layout.label(text="Marker only; particles appear in-game.")
        else:
            layout.prop(obj, '["sk8_native_trigger"]', text="Prefab")
            layout.label(text="Existing trigger, shape and effects are retained.")
            layout.label(text="Marker is the prefab origin, not its bounds.")
            layout.label(text="Move/rotate/scale the whole native prefab.")


class SK8_OT_add_speaker_interaction(Operator):
    bl_idname = "sk8.add_speaker_interaction"
    bl_label = "Add Speaker Interaction"
    bl_description = "Add a native music/Grabster anchor without a visible prop or collision"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        obj = bpy.data.objects.new("speaker_prefab", None)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.empty_display_type = "ARROWS"
        obj.empty_display_size = 0.5
        obj.show_in_front = True
        for selected in ctx.selected_objects:
            selected.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        return {"FINISHED"}


def audio_emitter_records(depsgraph, scale, selected=None):
    output = []
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type not in {"EMPTY", "MESH"}:
            continue
        original = getattr(obj, "original", None) or obj
        live = getattr(original, "sk8_audio_emitter", None)
        enabled = live.enabled if live is not None else original.get("sk8_audio_emitter_enabled", False)
        if not enabled or original.hide_render or (selected is not None and original not in selected):
            continue
        tags = _audio_emitter_paths(original)
        if any(not tag for tag in tags):
            raise ValueError(f"Audio emitter '{original.name}': reselect its blank object tag")
        tags = sorted(set(tags))
        if not tags:
            raise ValueError(f"Audio emitter '{original.name}': add an object tag")
        matrix = Matrix(item.matrix_world)
        rotation = matrix.to_quaternion().to_matrix()
        conversion = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
        basis = conversion @ rotation @ conversion.transposed()
        position = conversion @ matrix.translation * scale
        output.append({"name": original.name, "position": list(position),
            "basis": [list(basis.col[i]) for i in range(3)], "tags": tags})
    output.sort(key=lambda row: (row["name"], row["position"]))
    return output


class SK8_OT_audio_emitter_tag(Operator):
    bl_idname = "sk8.audio_emitter_tag"
    bl_label = "Add Object Audio Tag"
    bl_options = {"REGISTER", "UNDO"}
    remove: IntProperty(default=-1)

    def execute(self, ctx):
        s = ctx.object.sk8_audio_emitter
        if self.remove < 0:
            s.tags.add()
        elif self.remove < len(s.tags):
            s.tags.remove(self.remove)
        _sync_audio_emitter(s, ctx)
        return {"FINISHED"}


class SK8_OT_add_audio_emitter(Operator):
    bl_idname = "sk8.add_audio_emitter"
    bl_label = "Add Audio Emitter"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        obj = bpy.data.objects.new("Audio Emitter", None)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.empty_display_type = "SPHERE"
        obj.empty_display_size = 0.5
        obj.show_in_front = True
        obj.sk8_audio_emitter.tags.add()
        obj.sk8_audio_emitter.enabled = True
        for other in ctx.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        return {"FINISHED"}


class SK8_PT_sidebar_audio_emitter(Panel):
    bl_order = 50
    bl_label = "Object Audio Emitter"
    bl_idname = "SK8_PT_sidebar_audio_emitter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.type in {"EMPTY", "MESH"} and ctx.object.name.casefold() != "spawn"

    def draw(self, ctx):
        s = ctx.object.sk8_audio_emitter
        col = self.layout.column()
        col.prop(s, "enabled")
        if not s.enabled:
            return
        col.label(text="Static audio marker at the object's origin.")
        col.label(text="Nearby ambience chooses sounds from tags.")
        for i, tag in enumerate(s.tags):
            row = col.row(align=True)
            row.prop(tag, "tag", text="")
            row.operator("sk8.audio_emitter_tag", text="", icon="X").remove = i
        col.operator("sk8.audio_emitter_tag", text="Add Tag", icon="ADD")


NPC_ROUTE_KINDS = (
    ("pedestrian", "Pedestrian", "Native pedestrian walking route", 0),
    ("vehicle", "Vehicle Lane", "One-way lane for native traffic", 1),
    ("bus", "Bus Lane", "One-way lane for native buses", 2),
)

NPC_ROUTE_SHAPES = (
    ("path", "Path", "Open path with straight segments; extrude points in Edit Mode", 0),
    ("bezier", "Bezier", "Open curve shaped by editable Bezier handles", 1),
    ("circle", "Circle", "Closed Bezier loop", 2),
)


def _sync_npc_route(self, _context):
    obj = self.id_data
    for key in ("enabled", "kind", "width", "spacing", "weight", "speed", "bidirectional", "stairs"):
        obj["sk8_npc_" + key] = getattr(self, key)


class Sk8NpcRouteSettings(PropertyGroup):
    enabled: BoolProperty(name="NPC Route", default=False, update=_sync_npc_route)
    kind: EnumProperty(name="Type", items=NPC_ROUTE_KINDS, default="pedestrian", update=_sync_npc_route)
    width: FloatProperty(name="Path Width", default=2.0, min=0.01, subtype="DISTANCE", update=_sync_npc_route)
    spacing: FloatProperty(name="Sample Spacing", default=5.0, min=0.01, subtype="DISTANCE", update=_sync_npc_route)
    weight: FloatProperty(name="Route Weight", default=1.0, min=0.001, update=_sync_npc_route)
    speed: IntProperty(name="Speed Class", default=5, min=1, max=9, update=_sync_npc_route)
    bidirectional: BoolProperty(name="Both Directions", default=True, update=_sync_npc_route)
    stairs: BoolProperty(name="Stairs", default=False, update=_sync_npc_route)


def npc_route_records(depsgraph, scale, selected=None):
    registered = not hasattr(bpy.types.Object, "sk8_npc_route")
    if registered:
        bpy.utils.register_class(Sk8NpcRouteSettings)
        bpy.types.Object.sk8_npc_route = PointerProperty(type=Sk8NpcRouteSettings)
    output = []
    try:
        for item in depsgraph.object_instances:
            evaluated = item.object
            original = getattr(evaluated, "original", evaluated)
            if original is None or (selected is not None and original not in selected):
                continue
            settings = original.sk8_npc_route
            if not settings.enabled and not original.get("sk8_npc_enabled", False):
                continue
            if original.hide_render:
                continue
            if evaluated.type != "CURVE":
                raise ValueError(f"NPC route '{original.name}': use a Bezier or Poly curve; remove modifiers that convert it to a mesh")
            matrix = item.matrix_world

            def world(point):
                p = matrix @ Vector(point[:3])
                return [float(p.x * scale), float(p.z * scale), float(-p.y * scale)]

            def option(key):
                return getattr(settings, key) if settings.enabled else original.get("sk8_npc_" + key, getattr(settings, key))

            for index, spline in enumerate(evaluated.data.splines):
                if spline.type not in {"BEZIER", "POLY"}:
                    raise ValueError(f"NPC route '{original.name}': convert NURBS to a Bezier or Poly spline")
                bezier = spline.type == "BEZIER"
                points = []
                for p in (spline.bezier_points if bezier else spline.points):
                    row = {"position": world(p.co)}
                    if bezier:
                        row["left"] = world(p.handle_left)
                        row["right"] = world(p.handle_right)
                    points.append(row)
                if len(points) < (3 if spline.use_cyclic_u else 2):
                    raise ValueError(f"NPC route '{original.name}': add more curve points")
                kind = option("kind")
                output.append({
                    "name": f"{original.name}/{index}", "kind": kind,
                    "spline": "bezier" if bezier else "poly", "points": points,
                    "closed": bool(spline.use_cyclic_u),
                    "width": float(option("width") * scale),
                    "spacing": float(option("spacing") * scale),
                    "weight": float(option("weight")), "speed": int(option("speed")),
                    "bidirectional": bool(kind == "pedestrian" and option("bidirectional")),
                    "stairs": bool(kind == "pedestrian" and option("stairs")),
                })
        return output
    finally:
        if registered:
            del bpy.types.Object.sk8_npc_route
            bpy.utils.unregister_class(Sk8NpcRouteSettings)


class SK8_OT_add_npc_route(Operator):
    bl_idname = "sk8.add_npc_route"
    bl_label = "Add NPC Route"
    bl_options = {"REGISTER", "UNDO"}
    kind: EnumProperty(name="Type", items=NPC_ROUTE_KINDS, default="pedestrian")
    shape: EnumProperty(name="Shape", items=NPC_ROUTE_SHAPES, default="circle")

    def execute(self, ctx):
        if ctx.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        name = {"pedestrian": "Pedestrian Route", "vehicle": "Vehicle Lane", "bus": "Bus Lane"}[self.kind]
        data = bpy.data.curves.new(name, "CURVE")
        data.dimensions = "3D"
        data.resolution_u = 24
        positions = {
            "path": ((-12, 0, 0), (0, 0, 0), (12, 0, 0)),
            "bezier": ((-12, 0, 0), (0, 6, 0), (12, 0, 0)),
            "circle": ((12, 0, 0), (0, 12, 0), (-12, 0, 0), (0, -12, 0)),
        }[self.shape]
        spline = data.splines.new("POLY" if self.shape == "path" else "BEZIER")
        if self.shape == "path":
            spline.points.add(len(positions) - 1)
            for point, position in zip(spline.points, positions):
                point.co = (*position, 1)
                point.select = True
        else:
            spline.bezier_points.add(len(positions) - 1)
            for point, position in zip(spline.bezier_points, positions):
                point.co = position
                point.handle_left_type = "AUTO"
                point.handle_right_type = "AUTO"
                point.select_control_point = True
                point.select_left_handle = True
                point.select_right_handle = True
        spline.use_cyclic_u = self.shape == "circle"
        obj = bpy.data.objects.new(name, data)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.show_in_front = True
        obj.sk8_npc_route.kind = self.kind
        obj.sk8_npc_route.width = 2 if self.kind == "pedestrian" else 3.5
        obj.sk8_npc_route.enabled = True
        for other in ctx.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        return {"FINISHED"}


class _NpcRouteShapeMenu:
    def draw(self, _ctx):
        for shape, label, _description, _value in NPC_ROUTE_SHAPES:
            icon = {"path": "CURVE_PATH", "bezier": "CURVE_BEZCURVE", "circle": "CURVE_BEZCIRCLE"}[shape]
            op = self.layout.operator("sk8.add_npc_route", text=label, icon=icon)
            op.kind = self.kind
            op.shape = shape


class SK8_MT_npc_pedestrian_route(_NpcRouteShapeMenu, Menu):
    bl_label = "Pedestrian"
    kind = "pedestrian"


class SK8_MT_npc_vehicle_route(_NpcRouteShapeMenu, Menu):
    bl_label = "Vehicle Lane"
    kind = "vehicle"


class SK8_MT_npc_bus_route(_NpcRouteShapeMenu, Menu):
    bl_label = "Bus Lane"
    kind = "bus"


class SK8_MT_add_npc_route(Menu):
    bl_label = "Add NPC Route"

    def draw(self, _ctx):
        self.layout.menu("SK8_MT_npc_pedestrian_route")
        self.layout.menu("SK8_MT_npc_vehicle_route")
        self.layout.menu("SK8_MT_npc_bus_route")


class SK8_PT_sidebar_npc_route(Panel):
    bl_order = 10
    bl_label = "NPC Routes"
    bl_idname = "SK8_PT_sidebar_npc_route"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.type == "CURVE"

    def draw(self, ctx):
        s = ctx.object.sk8_npc_route
        col = self.layout.column()
        col.prop(s, "enabled")
        if not s.enabled:
            return
        col.prop(s, "kind")
        splines = ctx.object.data.splines
        spline = splines.active or (splines[0] if splines else None)
        if spline is not None:
            col.prop(spline, "use_cyclic_u", text="Closed")
        col.label(text="Tab: edit points / handles. E: extend the path.")
        col.prop(s, "width")
        col.prop(s, "spacing")
        col.prop(s, "weight")
        if s.kind == "pedestrian":
            col.prop(s, "bidirectional")
            col.prop(s, "stairs")
        else:
            col.prop(s, "speed")
            col.label(text="Traffic follows the spline point order.")
            col.label(text="Use Switch Direction to reverse a lane.")
        col.label(text="Place points on the walking / road surface.")
        col.label(text="Matching points connect routes of the same type.")
        col.label(text="Close curves or join ends for continuous traffic.")
        col.label(text="Uses the game's native population settings.")


# Grind curves. The game makes grind edges only from collision creases of about 30 degrees or
# more (Skate.exe edge analysis), and a physics part with more than ~1000 half-edges per metre
# of its bounds gets none at all, so smooth or dense ripped rails and copings cannot be ground.
# A grind curve is drawn along the top of a rail or coping: the export builds an invisible,
# 8-sided collision prism hanging under it, one corner exactly on the curve, as a collision
# piece of its own. Nothing is drawn in game.
GRIND_CURVE_SIDES = 8                   # 45 degree creases
GRIND_CURVE_TOLERANCE = 0.003           # metres a sampled Bezier may cut inside a bend
GRIND_CURVE_MIN_SPACING = 0.02          # closer points merge
GRIND_CURVE_SPLIT_COSINE = math.cos(math.radians(75.0))   # sharper corners start a new prism
GRIND_CURVE_BEZIER_SAMPLES = 48         # per Bezier segment, before simplifying


def _grind_curve_surface_items():
    items = []
    for slot, packed in sorted(SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT.items()):
        if slot in (0, SURFACE_ANALYSIS_PROPERTY_SLOT):
            continue
        label = NATIVE_COLLISION_MATERIAL_ROWS[slot][1]
        if slot == METAL_RAIL_MATERIAL_SLOT:
            label = "Metal Rail"
        items.append((NATIVE_COLLISION_MATERIAL_IDENTIFIER[packed], label,
                      f"Grinds, sounds and sparks as {label.lower()} (material slot {slot}, "
                      f"MaterialDecl.Packed={packed})", len(items)))
    return items


GRIND_CURVE_SURFACES = _grind_curve_surface_items()
GRIND_CURVE_DEFAULT_SURFACE = NATIVE_COLLISION_MATERIAL_IDENTIFIER[
    SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT[METAL_RAIL_MATERIAL_SLOT]]


class Sk8GrindCurveSettings(PropertyGroup):
    enabled: BoolProperty(
        name="Grind Curve", default=False,
        description=("Build an invisible, sharp-edged collision rail under this curve so the "
                     "game can grind it. Draw the curve along the top of a rail or coping"))
    radius: FloatProperty(
        name="Rail Radius", default=0.03, min=0.005, max=0.25, subtype="DISTANCE",
        description=("Half the thickness of the invisible rail hanging under the curve. Match "
                     "the visible rail or coping: a 2 3/8 inch coping is about 3 cm"))
    surface: EnumProperty(
        name="Surface", items=GRIND_CURVE_SURFACES, default=GRIND_CURVE_DEFAULT_SURFACE,
        description="How the grind sounds and sparks")


def is_grind_curve(obj) -> bool:
    """A curve object switched to Grind Curve."""
    if obj is None or obj.type != "CURVE":
        return False
    settings = getattr(obj, "sk8_grind_curve", None)
    if settings is not None:
        return bool(settings.enabled)
    try:  # not registered: Blender before 5.0 still shows the settings as custom properties
        raw = obj.get("sk8_grind_curve")
        return raw is not None and bool(raw.get("enabled", False))
    except (AttributeError, TypeError):
        return False


def _grind_curve_polylines(curve, matrix):
    """World-space point runs of a curve's splines as (points, closed); NURBS are counted
    and skipped."""
    from mathutils.geometry import interpolate_bezier
    lines, skipped = [], 0
    for spline in curve.splines:
        closed = bool(spline.use_cyclic_u)
        if spline.type == "BEZIER":
            knots = list(spline.bezier_points)
            if len(knots) < 2:
                continue
            pairs = list(zip(knots, knots[1:])) + ([(knots[-1], knots[0])] if closed else [])
            points = []
            for a, b in pairs:
                points.extend(interpolate_bezier(a.co, a.handle_right, b.handle_left, b.co,
                                                 GRIND_CURVE_BEZIER_SAMPLES + 1)[:-1])
            if not closed:
                points.append(knots[-1].co.copy())
        elif spline.type == "POLY":
            points = [Vector(point.co[:3]) for point in spline.points]
        else:
            skipped += 1
            continue
        points = [matrix @ Vector(point) for point in points]
        if len(points) >= 2:
            lines.append((points, closed and len(points) >= 3))
    return lines, skipped


def _simplify_polyline(points, tolerance):
    """Douglas-Peucker: drop points within `tolerance` of the line through their neighbours."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        start, span = points[first], points[last] - points[first]
        length_sq = span.length_squared
        worst, worst_index = -1.0, None
        for index in range(first + 1, last):
            offset = points[index] - start
            if length_sq > 1e-12:
                offset -= span * max(0.0, min(1.0, offset.dot(span) / length_sq))
            if offset.length > worst:
                worst, worst_index = offset.length, index
        if worst_index is not None and worst > tolerance:
            keep[worst_index] = True
            stack += [(first, worst_index), (worst_index, last)]
    return [point for point, kept in zip(points, keep) if kept]


def grind_curve_runs(points, closed):
    """Simplified point runs for prisms: close points merge and corners sharper than 75
    degrees split a run (the game never grinds round those anyway)."""
    points = _simplify_polyline(list(points) + ([points[0]] if closed else []), GRIND_CURVE_TOLERANCE)
    if len(points) < 2:
        return []
    # The ends stay exactly where they are drawn; points between merge when too close.
    merged = [points[0]]
    for point in points[1:-1]:
        if (point - merged[-1]).length >= GRIND_CURVE_MIN_SPACING:
            merged.append(point)
    if len(merged) > 1 and (points[-1] - merged[-1]).length < GRIND_CURVE_MIN_SPACING:
        merged[-1] = points[-1]
    else:
        merged.append(points[-1])
    if closed:
        merged.pop()  # the loop's repeated first point
        closed = len(merged) >= 3
    if len(merged) < 2 or (merged[-1] - merged[0]).length < 1e-6 and not closed:
        return []

    count = len(merged)

    def sharp(index):
        before = (merged[index] - merged[index - 1]).normalized()
        after = (merged[(index + 1) % count] - merged[index]).normalized()
        return before.dot(after) < GRIND_CURVE_SPLIT_COSINE

    if closed:
        corners = [index for index in range(count) if sharp(index)]
        if not corners:
            return [(merged, True)]
        merged = merged[corners[0]:] + merged[:corners[0] + 1]
        closed = False
        count = len(merged)
    runs, start = [], 0
    for index in range(1, count - 1):
        if sharp(index):
            runs.append((merged[start:index + 1], False))
            start = index
    runs.append((merged[start:], False))
    return [run for run in runs if len(run[0]) >= 2]


def grind_prism(points, closed, radius, sides=GRIND_CURVE_SIDES):
    """Positions and triangle indices of an open-ended prism hanging under a point run:
    `sides` faces, one corner on top lying on the run. Faces wind outward (Blender Z up)."""
    count = len(points)
    segments = count if closed else count - 1
    directions = [(points[(index + 1) % count] - points[index]).normalized()
                  for index in range(segments)]
    world_up = Vector((0.0, 0.0, 1.0))
    positions, previous_up = [], None
    for index in range(count):
        if closed:
            before, after = directions[index - 1], directions[index % segments]
        else:
            before = directions[max(0, index - 1)]
            after = directions[min(index, segments - 1)]
        tangent = before + after
        tangent = tangent.normalized() if tangent.length > 1e-6 else after
        up = world_up - tangent * world_up.dot(tangent)
        if up.length < 1e-3:  # a vertical run keeps the last frame's up
            fallback = previous_up or Vector((0.0, 1.0, 0.0))
            up = fallback - tangent * fallback.dot(tangent)
        up.normalize()
        previous_up = up
        side = tangent.cross(up)
        # A mitred joint: offsets across the bend stretch so both segments keep the radius.
        bend = after - before
        bend -= tangent * bend.dot(tangent)
        stretch = 0.0
        if bend.length > 1e-6:
            bend.normalize()
            stretch = 1.0 / max(0.5, min(1.0, before.dot(tangent))) - 1.0
        ring = []
        for corner in range(sides):
            angle = 2.0 * math.pi * corner / sides
            offset = (up * math.cos(angle) + side * math.sin(angle)) * radius
            if stretch:
                offset += bend * (offset.dot(bend) * stretch)
            ring.append(offset)
        centre = points[index] - ring[0]
        positions.extend(centre + offset for offset in ring)
    indices = []
    for index in range(segments):
        here, there = index * sides, ((index + 1) % count) * sides
        for corner in range(sides):
            following = (corner + 1) % sides
            a, b, c, d = here + corner, here + following, there + following, there + corner
            indices += (a, b, c, a, c, d)
    return positions, indices


def grind_curve_records(depsgraph, scale, selected=None):
    """The collision prism of every grind curve in scaled Blender world space (Z up), and
    notes on curves that were skipped or look wrong."""
    registered = not hasattr(bpy.types.Object, "sk8_grind_curve")
    if registered:
        bpy.utils.register_class(Sk8GrindCurveSettings)
        bpy.types.Object.sk8_grind_curve = PointerProperty(type=Sk8GrindCurveSettings)
    records, notes = [], []
    try:
        for item in depsgraph.object_instances:
            evaluated = item.object
            original = getattr(evaluated, "original", evaluated)
            if original is None or (selected is not None and original not in selected):
                continue
            if not is_grind_curve(original) or original.hide_render:
                continue
            if evaluated.type != "CURVE":
                notes.append(f"Grind curve '{original.name}' was skipped: its modifiers make it a mesh")
                continue
            settings = original.sk8_grind_curve
            lines, skipped = _grind_curve_polylines(evaluated.data, item.matrix_world)
            if skipped:
                notes.append(f"Grind curve '{original.name}': {skipped} NURBS splines were skipped; "
                             "convert them to Bezier or Poly")
            positions, indices, pieces, length = [], [], 0, 0.0
            for points, closed in lines:
                for run, run_closed in grind_curve_runs(points, closed):
                    run_positions, run_indices = grind_prism(run, run_closed, settings.radius)
                    base = len(positions)
                    positions.extend(run_positions)
                    indices.extend(base + index for index in run_indices)
                    pieces += 1
                    length += sum((run[(i + 1) % len(run)] - run[i]).length
                                  for i in range(len(run) if run_closed else len(run) - 1))
            if not indices:
                notes.append(f"Grind curve '{original.name}' has no usable points")
                continue
            low = Vector(tuple(min(p[axis] for p in positions) for axis in range(3)))
            high = Vector(tuple(max(p[axis] for p in positions) for axis in range(3)))
            # The game drops edge analysis for parts over ~1000 half-edges per metre of bounds.
            if len(indices) / max((high - low).length, 1e-6) > 900.0:
                notes.append(f"Grind curve '{original.name}' is too detailed for its size to grind; "
                             "use fewer points")
            packed = smooth_grind_packed(NATIVE_COLLISION_MATERIAL_PACKED.get(
                settings.surface, NATIVE_COLLISION_MATERIAL_PACKED[GRIND_CURVE_DEFAULT_SURFACE]))
            name = original.name if not item.is_instance else (
                f"{original.name}__instance_{len(records):04d}")
            records.append({
                "name": name,
                "source_object": original.name,
                "positions": [tuple(float(c) * scale for c in point) for point in positions],
                "indices": indices,
                "packed": packed,
                "radius": float(settings.radius) * scale,
                "pieces": pieces,
                "length": length * scale,
            })
        return records, notes
    finally:
        if registered:
            del bpy.types.Object.sk8_grind_curve
            bpy.utils.unregister_class(Sk8GrindCurveSettings)


def _edge_chains(edges):
    """Vertex runs through a set of edges as (vertices, closed): open runs end at loose ends
    and junctions, loops close."""
    neighbours = {}
    for a, b in edges:
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
    used = set()
    chains = []
    ends = [vertex for vertex, linked in neighbours.items() if len(linked) != 2]
    for start in ends + list(neighbours):
        for first in neighbours[start]:
            if frozenset((start, first)) in used:
                continue
            used.add(frozenset((start, first)))
            chain, previous, current = [start], start, first
            while True:
                chain.append(current)
                linked = neighbours[current]
                if current == start or len(linked) != 2:
                    break
                following = linked[1] if linked[0] == previous else linked[0]
                if frozenset((current, following)) in used:
                    break
                used.add(frozenset((current, following)))
                previous, current = current, following
            closed = len(chain) > 3 and chain[-1] == chain[0]
            chains.append((chain[:-1] if closed else chain, closed))
    return chains


def _new_grind_curve(ctx, name, runs, collections=None):
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    for points, closed in runs:
        spline = data.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for point, position in zip(spline.points, points):
            point.co = (*position, 1.0)
        spline.use_cyclic_u = closed
    obj = bpy.data.objects.new(name, data)
    for collection in (collections or [ctx.collection]):
        collection.objects.link(obj)
    obj.show_in_front = True
    obj.sk8_grind_curve.enabled = True
    for other in ctx.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    ctx.view_layer.objects.active = obj
    return obj


def _bevel_grind_edges(points, faces, edges, max_width):
    # Use coplanar triangle patches so diagonals and material/UV seams do not
    # split a bevel. Broad support planes distinguish it from a round rail.
    if max_width <= 0:
        return [], set()
    links = [[] for _ in faces]
    for linked in edges.values():
        if len(linked) == 2:
            a, b = linked
            links[a].append(b)
            links[b].append(a)
    patches, face_patch = [], [-1] * len(faces)
    for seed, (normal, center, _ids) in enumerate(faces):
        if face_patch[seed] != -1:
            continue
        patch = {"normal": normal, "center": center, "vertices": set(), "edges": [], "area": 0.0}
        patch_id = len(patches)
        patches.append(patch)
        stack = [seed]
        face_patch[seed] = patch_id
        while stack:
            current = stack.pop()
            ids = faces[current][2]
            patch["vertices"].update(ids)
            first, second, third = (points[index] for index in ids)
            patch["area"] += (second - first).cross(third - first).length * 0.5
            for neighbour in links[current]:
                other_normal, other_center, _ids = faces[neighbour]
                if (face_patch[neighbour] == -1 and normal.dot(other_normal) >= 0.99999 and
                        abs(normal.dot(other_center - center)) <= 0.0001):
                    face_patch[neighbour] = patch_id
                    stack.append(neighbour)
    boundaries = {}
    for edge, linked in edges.items():
        if len(linked) != 2:
            continue
        a, b = (face_patch[index] for index in linked)
        if a != b:
            boundaries[edge] = (a, b)
            patches[a]["edges"].append(edge)
            patches[b]["edges"].append(edge)

    def has_support_width(patch, origin, direction, required):
        return any(((points[index] - origin) - direction *
                    (points[index] - origin).dot(direction)).length >= required
                   for index in patch["vertices"])

    def project_corner(position, top, side):
        normal_a, normal_b = top["normal"], side["normal"]
        cosine = normal_a.dot(normal_b)
        denominator = 1.0 - cosine * cosine
        if denominator < 0.01:
            return None
        offset_a = normal_a.dot(top["center"] - position)
        offset_b = normal_b.dot(side["center"] - position)
        return (position + normal_a * ((offset_a - cosine * offset_b) / denominator) +
                normal_b * ((offset_b - cosine * offset_a) / denominator))

    replacements, consumed, corner_supports = [], set(), {}
    # Broad supports precede bevel facets. Try both orientations: a tilted
    # ledge's bevel facet can face more upward than its actual top surface.
    candidates = [(edge, (top, bevel)) for edge, pair in boundaries.items()
                  for top, bevel in (pair, pair[::-1]) if patches[top]["normal"].z >= 0.85]
    candidates.sort(key=lambda item: (-patches[item[1][0]]["area"], item[0], item[1]))
    for seed_edge, (a, b) in candidates:
        if seed_edge in consumed:
            continue
        top, bevel = patches[a], patches[b]
        if (top["normal"].dot(bevel["normal"]) <= 0.05 or
                top["normal"].dot(bevel["normal"]) >= 0.99999 or
                top["normal"].dot(bevel["center"] - top["center"]) > 0.0001):
            continue
        origin, end = (points[index] for index in seed_edge)
        span = end - origin
        if span.length < 0.002:
            continue
        direction = span.normalized()
        if abs(direction.z) > 0.8:
            continue
        # Follow all bevel segments while retaining their common length. This
        # reconstructs the top/side corner without extending beyond end caps.
        stack = [(b, seed_edge, 0.0, span.length, (seed_edge,), frozenset((a, b)), 0.0)]
        found = None
        while stack and found is None:
            current_id, entered, low, high, path, visited, previous_width = stack.pop()
            if len(path) > 64:
                continue
            current = patches[current_id]
            for edge in current["edges"]:
                if edge == entered or edge in consumed:
                    continue
                pair = boundaries[edge]
                neighbour_id = pair[1] if pair[0] == current_id else pair[0]
                if neighbour_id in visited:
                    continue
                neighbour = patches[neighbour_id]
                normal = neighbour["normal"]
                top_cosine = top["normal"].dot(normal)
                if (top_cosine > top["normal"].dot(current["normal"]) + 0.0001 or
                        current["normal"].dot(neighbour["center"] - current["center"]) > 0.0001):
                    continue
                first, second = (points[index] for index in edge)
                across = second - first
                if across.length < 0.002 or abs(direction.dot(across.normalized())) < 0.98:
                    continue
                start_projection, end_projection = (first - origin).dot(direction), (second - origin).dot(direction)
                next_low = max(low, min(start_projection, end_projection))
                next_high = min(high, max(start_projection, end_projection))
                if next_high - next_low < 0.002:
                    continue
                def edge_point(along):
                    return first + across * ((along - start_projection) / (end_projection - start_projection))
                middle = (next_low + next_high) * 0.5
                crossing = edge_point(middle)
                width = (crossing - (origin + direction * middle)).length
                endpoint_width = max((edge_point(along) - (origin + direction * along)).length
                                     for along in (next_low, next_high))
                if width < previous_width - 0.0001 or endpoint_width > max_width + 1e-6:
                    continue
                next_path = path + (edge,)
                if abs(normal.z) <= 0.5 and top_cosine <= 0.75:
                    required_support = max(0.01, width * 1.5)
                    if (width >= 0.002 and top["normal"].dot(normal) <= 0.75 and
                            has_support_width(top, origin + direction * middle, direction, required_support) and
                            has_support_width(neighbour, crossing, direction, required_support)):
                        first_corner = project_corner(origin + direction * next_low, top, neighbour)
                        last_corner = project_corner(origin + direction * next_high, top, neighbour)
                        if first_corner is not None and last_corner is not None:
                            found = (first_corner, last_corner, next_path, next_low, next_high, neighbour)
                            break
                # A nearly vertical facet may still be part of a rounded bevel;
                # keep walking until there is an actual broad side support.
                if top_cosine >= -0.02:
                    stack.append((neighbour_id, edge, next_low, next_high, next_path,
                                  visited | {neighbour_id}, width))
        if found:
            first, last, path, low, high, side = found
            references = (seed_edge[0] if low <= 0.0001 else None,
                          seed_edge[1] if span.length - high <= 0.0001 else None)
            replacements.append((first, last, references))
            for vertex, position in zip(references, (first, last)):
                if vertex is not None:
                    support = corner_supports.setdefault(vertex, {"points": [], "planes": []})
                    support["points"].append(position)
                    for patch in (top, side):
                        normal, center = patch["normal"], patch["center"]
                        if not any(normal.dot(n) >= 0.99999 and abs(normal.dot(c - center)) <= 0.0001
                                   for n, c in support["planes"]):
                            support["planes"].append((normal, center))
            consumed.update(path)
    # Neighbouring bevel segments share a source vertex. Reconstruct that one
    # corner from all its support planes so curved strips do not leave gaps.
    corners = {}
    for vertex, support in corner_supports.items():
        samples, planes = support["points"], support["planes"]
        corner = sum(samples, Vector()) / len(samples)
        system = Matrix(tuple(tuple(sum(n[row] * n[col] for n, _c in planes)
                                    for col in range(3)) for row in range(3)))
        if abs(system.determinant()) > 1e-8:
            right = Vector(tuple(sum(n[axis] * n.dot(center - corner) for n, center in planes)
                                 for axis in range(3)))
            fitted = corner + system.inverted() @ right
            if all(math.isfinite(value) for value in fitted) and all(
                    (fitted - sample).length <= max_width for sample in samples):
                corner = fitted
        corners[vertex] = corner
    return [(corners.get(refs[0], first), corners.get(refs[1], last))
            for first, last, refs in replacements], consumed


def _auto_grind_runs(mesh, matrix, max_bevel_width=0.10):
    mesh.calc_loop_triangles()
    points, welded, vertices, positions, faces, edges = [], {}, {}, {}, [], {}
    reverse = matrix.determinant() < 0.0

    def weld_position(position):
        # Match C++ llround, including negative half-millimetre values.
        key = tuple(math.floor(value * 1000 + 0.5) if value >= 0 else
                    math.ceil(value * 1000 - 0.5) for value in position)
        if key not in welded:
            welded[key] = len(points)
            points.append(position)
        return welded[key]

    def weld(index):
        if index not in vertices:
            position = matrix @ mesh.vertices[index].co
            if not all(math.isfinite(value) for value in position):
                vertices[index] = None
                return None
            positions[index] = position
            vertices[index] = weld_position(position)
        return vertices[index]

    for triangle in mesh.loop_triangles:
        indices = list(triangle.vertices)
        if reverse:
            indices.reverse()
        ids = [weld(index) for index in indices]
        if None in ids or len(set(ids)) < 3:
            continue
        a, b, c = (positions[index] for index in indices)
        normal = (b - a).cross(c - a)
        if normal.length_squared < 1e-16:
            continue
        face_id = len(faces)
        faces.append((normal.normalized(), (a + b + c) / 3.0, ids))
        for first, second in zip(ids, ids[1:] + ids[:1]):
            key = (min(first, second), max(first, second))
            edges.setdefault(key, []).append(face_id)

    bevel_edges, consumed = _bevel_grind_edges(points, faces, edges, max_bevel_width)
    selected = []
    for (first, second), linked in sorted(edges.items()):
        if len(linked) != 2 or (first, second) in consumed:
            continue
        normal_a, center_a, _ids = faces[linked[0]]
        normal_b, center_b, _ids = faces[linked[1]]
        direction = (points[second] - points[first]).normalized()
        if abs(direction.z) > 0.8:
            continue
        angle = math.acos(max(-1.0, min(1.0, normal_a.dot(normal_b))))
        if (angle < 0.035 or max(normal_a.z, normal_b.z) < 0.15 or
                normal_a.dot(center_b - center_a) > 1e-4):
            continue
        radius = (center_a - center_b).length / max(0.001, 2 * math.sin(angle * 0.5))
        if angle < 0.610865 and radius > 0.30:
            continue
        selected.append((first, second))

    for first, second in bevel_edges:
        a, b = weld_position(first), weld_position(second)
        if a != b:
            selected.append((min(a, b), max(a, b)))
    selected = sorted(set(selected))

    adjacent = [[] for _ in points]
    for index, (first, second) in enumerate(selected):
        adjacent[first].append(index)
        adjacent[second].append(index)
    used, runs = set(), []

    def walk(start, edge):
        chain, vertex, total = [start], start, 0.0
        while edge not in used:
            used.add(edge)
            first, second = selected[edge]
            following = second if first == vertex else first
            span = points[following] - points[vertex]
            direction = span.normalized()
            total += span.length
            chain.append(following)
            if following == start or len(adjacent[following]) != 2:
                break
            candidate = next(index for index in adjacent[following] if index != edge)
            first, second = selected[candidate]
            other = second if first == following else first
            if direction.dot((points[other] - points[following]).normalized()) < 0.5:
                break
            vertex, edge = following, candidate
        if total >= 0.5:
            closed = chain[-1] == chain[0]
            runs.append(([points[index] for index in (chain[:-1] if closed else chain)], closed))

    for vertex, linked in enumerate(adjacent):
        if len(linked) != 2:
            for edge in linked:
                if edge not in used:
                    walk(vertex, edge)
    for edge, (first, _second) in enumerate(selected):
        if edge not in used:
            walk(first, edge)
    return runs


class SK8_OT_auto_grind_curves(Operator):
    bl_idname = "sk8.auto_grind_curves"
    bl_label = "Auto Grind Curves From Objects"
    bl_description = ("Detect top edges, narrow bevels and round rails on selected meshes, "
                      "including modifiers, and create editable grind curves")
    bl_options = {"REGISTER", "UNDO"}

    max_bevel_width: FloatProperty(
        name="Max Bevel Width", default=0.10, min=0.0, max=1.0, subtype="DISTANCE",
        description=("Maximum distance across a bevel to replace with one curve at the "
                     "intersection of the top and side surfaces; zero disables bevel detection"))

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == "OBJECT" and any(obj.type == "MESH" for obj in ctx.selected_objects)

    def execute(self, ctx):
        sources = [obj for obj in ctx.selected_objects if obj.type == "MESH"]
        depsgraph = ctx.evaluated_depsgraph_get()
        generated, source_count, skipped = [], 0, 0
        ctx.window_manager.progress_begin(0, len(sources))
        try:
            for index, source in enumerate(sources):
                ctx.window_manager.progress_update(index)
                evaluated = source.evaluated_get(depsgraph)
                mesh = evaluated.to_mesh()
                try:
                    runs = _auto_grind_runs(mesh, evaluated.matrix_world, self.max_bevel_width) if mesh else []
                finally:
                    evaluated.to_mesh_clear()
                if not runs:
                    skipped += 1
                    continue
                collections = [collection for collection in source.users_collection
                               if collection.is_editable]
                # Like the scene viewer, keep runs separate so one object's many
                # ridges do not become a single overly dense collision mesh.
                for run in runs:
                    generated.append(_new_grind_curve(ctx, f"{source.name} Auto Grind", [run], collections))
                source_count += 1
        finally:
            ctx.window_manager.progress_end()
        if not generated:
            self.report({"WARNING"}, "No grindable runs found on the selected meshes")
            return {"CANCELLED"}
        for obj in generated:
            obj.select_set(True)
        self.report({"INFO"}, f"Made {len(generated)} grind curves from {source_count} meshes; "
                              f"{skipped} meshes had no runs. Review the curves before saving")
        return {"FINISHED"}


class SK8_OT_add_grind_curve(Operator):
    bl_idname = "sk8.add_grind_curve"
    bl_label = "Add Grind Curve"
    bl_description = ("Add a 2 m grind curve at the 3D cursor. Move its points onto the top of "
                      "a rail or coping (Tab to edit, E to extend)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        if ctx.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        cursor = ctx.scene.cursor.location
        _new_grind_curve(ctx, "Grind Curve", [([cursor.copy(), cursor + Vector((2.0, 0.0, 0.0))], False)])
        return {"FINISHED"}


class SK8_OT_grind_curve_from_edges(Operator):
    bl_idname = "sk8.grind_curve_from_edges"
    bl_label = "Grind Curve From Selected Edges"
    bl_description = ("In Edit Mode, select the top edge loop of a rail or coping, then make a "
                      "grind curve along it")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == "EDIT_MESH" and ctx.edit_object is not None

    def execute(self, ctx):
        import bmesh
        source = ctx.edit_object
        mesh = bmesh.from_edit_mesh(source.data)
        edges = [edge for edge in mesh.edges if edge.select]
        if not edges:
            self.report({"ERROR"}, "Select the edges along the top of the rail or coping first")
            return {"CANCELLED"}
        matrix = source.matrix_world
        positions = {vertex.index: matrix @ vertex.co for edge in edges for vertex in edge.verts}
        chains = _edge_chains([(edge.verts[0].index, edge.verts[1].index) for edge in edges])
        runs = [([positions[vertex] for vertex in chain], closed) for chain, closed in chains
                if len(chain) >= 2]
        bpy.ops.object.mode_set(mode="OBJECT")
        obj = _new_grind_curve(ctx, f"{source.name} Grind", runs, list(source.users_collection))
        self.report({"INFO"}, f"Made grind curve '{obj.name}' with {len(runs)} runs; "
                              "set its Rail Radius in Skate Map > Grind Curve")
        return {"FINISHED"}


class SK8_PT_sidebar_grind_curve(Panel):
    bl_order = 11
    bl_label = "Grind Curve"
    bl_idname = "SK8_PT_sidebar_grind_curve"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.type == "CURVE"

    def draw(self, ctx):
        settings = ctx.object.sk8_grind_curve
        col = self.layout.column()
        col.prop(settings, "enabled")
        if not settings.enabled:
            col.label(text="Makes a rail or coping grindable.", icon="INFO")
            return
        col.prop(settings, "radius")
        col.prop(settings, "surface")
        col.label(text="Put the curve on the top of the rail.", icon="INFO")
        col.label(text="An invisible rail hangs under it in game.")
        col.label(text="Sharp corners start a new grind.")


class Sk8MaterialSettings(PropertyGroup):
    invisible: BoolProperty(
        name="Invisible (Collision Only)", default=False,
        description=("Draw nothing in game: faces with this material keep only their collision. "
                     "For collider meshes, invisible walls and blockers"))
    collision_material: EnumProperty(
        name="Base Surface / Audio Donor",
        items=NATIVE_COLLISION_MATERIALS,
        default=DEFAULT_COLLISION_MATERIAL,
        update=_sync_collision_material,
        description=("Shipped surface used as the starting material and donor "
                     "for sound events, VFX, and unspecified properties"))
    custom_audio: BoolProperty(
        name="Override Audio Metadata", default=False,
        update=_sync_surface_profile,
        description="Override numeric collision-audio response metadata")
    audio_softness: FloatProperty(
        name="Softness", default=0.2, min=0.0, max=1.0,
        update=_sync_surface_profile)
    audio_smoothness: FloatProperty(
        name="Smoothness", default=0.25, min=0.0, max=1.0,
        update=_sync_surface_profile)
    audio_min_impact_force: FloatProperty(
        name="Minimum Impact Force", default=0.1, min=0.0, max=10000.0,
        update=_sync_surface_profile)
    audio_impact_cooldown_release: FloatProperty(
        name="Impact Cooldown", default=0.25, min=0.0, max=60.0,
        update=_sync_surface_profile)
    audio_ignore_player_collisions: BoolProperty(
        name="Ignore Player Collisions", default=False,
        update=_sync_surface_profile)
    custom_friction: BoolProperty(
        name="Override Contact Physics", default=False,
        update=_sync_surface_profile,
        description="Override deck, truck, and wheel contact coefficients")
    deck_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.5, min=0.0, max=10.0,
        update=_sync_surface_profile)
    deck_static_friction: FloatProperty(
        name="Static", default=0.5, min=0.0, max=10.0,
        update=_sync_surface_profile)
    deck_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    truck_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.7, min=0.0, max=10.0,
        update=_sync_surface_profile)
    truck_static_friction: FloatProperty(
        name="Static", default=0.8, min=0.0, max=10.0,
        update=_sync_surface_profile)
    truck_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    wheel_dynamic_friction: FloatProperty(
        name="Dynamic", default=0.7, min=0.0, max=10.0,
        update=_sync_surface_profile)
    wheel_static_friction: FloatProperty(
        name="Static", default=0.8, min=0.0, max=10.0,
        update=_sync_surface_profile)
    wheel_restitution: FloatProperty(
        name="Bounce", default=0.0, min=0.0, max=1.0,
        update=_sync_surface_profile)
    custom_behavior: BoolProperty(
        name="Override Gameplay Behavior", default=False,
        update=_update_custom_behavior,
        description="Replace all donor behavior flags with the values below")
    exclude_from_edge_generation: BoolProperty(
        name="No Generated Edges", default=False,
        update=_sync_surface_profile)
    include_in_surface_analysis: BoolProperty(
        name="Include in Surface Analysis", default=False,
        update=_sync_surface_profile)
    exclude_from_grinding: BoolProperty(
        name="Disable Grinding", default=False,
        update=_sync_surface_profile)
    jump_pad: BoolProperty(name="Jump Pad", default=False,
                           update=_sync_surface_profile)
    boost_pad: BoolProperty(name="Boost Pad", default=False,
                            update=_sync_surface_profile)
    wipeout: BoolProperty(name="Force Wipeout", default=False,
                          update=_sync_surface_profile)
    slide: BoolProperty(name="Slide", default=False,
                        update=_sync_surface_profile)
    do_not_align: BoolProperty(name="Do Not Align Skater", default=False,
                               update=_sync_surface_profile)
    stairs: BoolProperty(name="Stairs", default=False,
                         update=_sync_surface_profile)
    camera_occluder: BoolProperty(name="Camera Occluder", default=False,
                                  update=_sync_surface_profile)


    effective_exclude_from_edge_generation: BoolProperty(
        name="No Generated Edges", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("exclude_from_edge_generation"))
    effective_include_in_surface_analysis: BoolProperty(
        name="Include in Surface Analysis", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("include_in_surface_analysis"))
    effective_exclude_from_grinding: BoolProperty(
        name="Disable Grinding", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("exclude_from_grinding"))
    effective_jump_pad: BoolProperty(
        name="Jump Pad", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("jump_pad"))
    effective_boost_pad: BoolProperty(
        name="Boost Pad", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("boost_pad"))
    effective_wipeout: BoolProperty(
        name="Force Wipeout", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("wipeout"))
    effective_slide: BoolProperty(
        name="Slide", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("slide"))
    effective_do_not_align: BoolProperty(
        name="Do Not Align Skater", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("do_not_align"))
    effective_stairs: BoolProperty(
        name="Stairs", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("stairs"))
    effective_camera_occluder: BoolProperty(
        name="Camera Occluder", options={"SKIP_SAVE"},
        get=_effective_behavior_getter("camera_occluder"))
    surface: EnumProperty(name="Surface", items=SURFACES, default="default",
                          update=_sync_material_surface)
    srgb: BoolProperty(name="sRGB", default=True,
                       description="Colour texture; turn off for masks and data maps")
    alpha: EnumProperty(
        name="Alpha", default="auto",
        items=[("auto", "Auto", "Detect from the image"),
               ("opaque", "Opaque", "Ignore alpha (BC1)"),
               ("mask", "Cutout", "1-bit alpha (BC1A)"),
               ("blend", "Blend", "Full 8-bit alpha (BC3)")])
    domain: EnumProperty(
        name="Shader Override", default="surface",
        items=[("surface", "Auto", "Keep the current shader selection based on transparency", 0, 0),
               ("decal", "Decal", "Use Studio's current blended decal shader", 0, 1),
               ("foliage", "Foliage", "Use the Kentia palm Cards material and alpha-tested shadows", 0, 2),
               ("ocean", "Ocean", "Use native BAM ocean water, including its wave and reflection textures", 0, 4),
               ("glass_opaque", "Glass (Reflective)", "Use the native reflective window-glass material; opaque", 0, 6)])
    alpha_source: EnumProperty(
        name="Alpha Source", default="auto",
        items=[("auto", "Auto", "Use an explicit opacity image when set, otherwise Base Color alpha"),
               ("base_color_texture", "Base Color Alpha", "Reuse the Base Color texture alpha channel"),
               ("opacity_texture", "Opacity Texture", "Use the explicit opacity image red channel"),
               ("constant", "Constant", "Do not sample texture alpha")])
    alpha_cutoff: FloatProperty(
        name="Alpha Cutoff", default=0.5, min=0.0, max=1.0,
        description="Cutout threshold used by masked decals and foliage")
    cast_shadows: BoolProperty(name="Cast Shadows", default=True)
    double_sided: BoolProperty(name="Double Sided", default=False)
    transparent_shadow: BoolProperty(
        name="Transparent Shadows", default=False,
        description="Allow the authored alpha mask to shape native shadows")
    base_color_image: PointerProperty(
        name="Base Color Texture", type=bpy.types.Image,
        description="Override the native donor's base color; empty uses the Blender shader")
    roughness_image: PointerProperty(
        name="Roughness Texture", type=bpy.types.Image,
        description="Converted to smoothness in MSK green; packed MSK takes priority")
    metallic_image: PointerProperty(
        name="Metallic Texture", type=bpy.types.Image,
        description="Packed into MSK red; packed MSK takes priority")
    roughness_channel: EnumProperty(name="Channel", default="Color", items=[
        (name, name, "Source channel") for name in ("Color", "Red", "Green", "Blue", "Alpha")])
    metallic_channel: EnumProperty(name="Channel", default="Color", items=[
        (name, name, "Source channel") for name in ("Color", "Red", "Green", "Blue", "Alpha")])
    normal_image: PointerProperty(
        name="Normal Texture", type=bpy.types.Image,
        description="Optional tangent-space normal image")
    mask_image: PointerProperty(
        name="Mask Texture", type=bpy.types.Image,
        description="Native packed MSK; used unchanged and overrides Roughness/Metallic inputs")
    opacity_image: PointerProperty(
        name="Opacity Texture", type=bpy.types.Image,
        description="Optional red-channel opacity image; leave empty to reuse Base Color alpha")


def _is_spawn(obj):
    return obj.type == "EMPTY" and obj.name.casefold() == SPAWN_MARKER


def _is_legacy_spawn_mesh(obj):
    return obj.type == "MESH" and obj.name.casefold() in {
        "sk8_playerspawn", "sk8_player_spawn"
    }


def _mesh_objects(ctx, settings):


    src = ctx.selected_objects if settings.selected_only else ctx.scene.objects
    return [o for o in src if o.type == "MESH" and not _is_legacy_spawn_mesh(o)]


def _spawn_objects(ctx):
    return [obj for obj in ctx.scene.objects if _is_spawn(obj)]


def material_invisible(mat) -> bool:
    """The material's Invisible (Collision Only) switch: its faces export as collision with
    nothing drawn."""
    return mat is not None and bool(getattr(mat.sk8_material, "invisible", False))


def _object_material(obj):
    mats = [s.material for s in obj.material_slots if s.material]
    return mats


def _normalise_collision_template(value):

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return NATIVE_COLLISION_MATERIAL_IDENTIFIER.get(value)
    text = str(value).strip()
    if text in NATIVE_COLLISION_MATERIAL_PACKED:
        return text
    if text.lower().startswith("material_"):
        try:
            packed = int(text[len("material_"):], 10)
        except ValueError:
            return None
        return NATIVE_COLLISION_MATERIAL_IDENTIFIER.get(packed)
    return None


def _explicit_collision_template(owner, group_name):

    if owner is None:
        return None
    try:
        raw = owner.get(group_name)
        value = _normalise_collision_template(raw.get("collision_material")) \
            if raw is not None else None
    except Exception:
        value = None
    if value is not None:
        return value
    try:
        settings = getattr(owner, group_name, None)
        explicitly_set = (settings is not None and
                          (not hasattr(settings, "is_property_set") or
                           settings.is_property_set("collision_material")))
        value = (_normalise_collision_template(settings.collision_material)
                 if explicitly_set else None)
    except Exception:
        value = None
    if value is not None:
        return value
    try:
        return _normalise_collision_template(
            owner.get("sk8_collision_material_packed"))
    except Exception:
        return None


def _collision_template_for(obj, material):

    settings = getattr(obj, "sk8_object", None)
    mode = (settings.collision_mode if settings is not None else
            obj.get("sk8_collision_mode") if obj is not None else None)
    if mode == "water":
        return NATIVE_COLLISION_MATERIAL_IDENTIFIER[WATER_COLLISION_MATERIAL_PACKED]
    return (_explicit_collision_template(material, "sk8_material") or
            _explicit_collision_template(obj, "sk8_object") or
            DEFAULT_COLLISION_MATERIAL)


def scalar_texture_source(socket):
    channel = None
    seen = set()
    while socket is not None and socket.is_linked:
        link = socket.links[0]
        node, output = link.from_node, link.from_socket
        pointer = int(node.as_pointer())
        if pointer in seen:
            raise ValueError("cyclic scalar texture graph")
        seen.add(pointer)
        if node.type == "REROUTE":
            socket = node.inputs[0]
        elif node.type in {"SEPRGB", "SEPARATE_COLOR"}:
            if node.type == "SEPARATE_COLOR" and node.mode != "RGB":
                raise ValueError("use Separate Color in RGB mode for material textures")
            channel = output.name
            socket = node.inputs[0]
        elif node.type == "TEX_IMAGE" and node.image is not None:
            vector = node.inputs.get("Vector")
            vector_nodes = set()
            while vector is not None and vector.is_linked:
                source = vector.links[0]
                pointer = int(source.from_node.as_pointer())
                if pointer in vector_nodes:
                    raise ValueError("cyclic scalar texture mapping")
                vector_nodes.add(pointer)
                if source.from_node.type == "REROUTE":
                    vector = source.from_node.inputs[0]
                    continue
                if source.from_node.type == "MAPPING":
                    mapping = source.from_node
                    identity = all(not mapping.inputs[key].is_linked and
                                   all(abs(float(v) - expected) < 1e-7 for v in mapping.inputs[key].default_value)
                                   for key, expected in (("Location", 0), ("Rotation", 0), ("Scale", 1)))
                    if identity:
                        vector = mapping.inputs.get("Vector")
                        continue
                if (source.from_node.type == "UVMAP" or
                    source.from_node.type == "TEX_COORD" and source.from_socket.name == "UV"):
                    break
                raise ValueError("bake the scalar texture's vector mapping to the mesh UVs first")
            if node.projection != "FLAT":
                raise ValueError("scalar textures require Flat UV projection")
            return node.image, "Alpha" if output.name == "Alpha" else channel or output.name
        elif node.type == "RGBTOBW":
            channel = "Color"
            socket = node.inputs[0]
        else:
            raise ValueError(f"bake the {node.type} scalar input to an image first")
    return None, None


def _base_color_image(mat):

    if not mat or not mat.use_nodes:
        return None
    out = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if out is None:
        return next((n.image for n in mat.node_tree.nodes
                     if n.type == "TEX_IMAGE" and n.image), None)
    sock = out.inputs.get("Base Color")
    seen = set()
    stack = [sock]
    while stack:
        s = stack.pop()
        if not s or not s.is_linked or id(s) in seen:
            continue
        seen.add(id(s))
        node = s.links[0].from_node
        if node.type == "TEX_IMAGE" and node.image:
            return node.image
        stack.extend(node.inputs)
    return None


def _linked_image(socket, maximum_depth=8):

    if socket is None:
        return None
    pending = [(socket, 0)]
    seen = set()
    while pending:
        current, depth = pending.pop()
        if current is None or not current.is_linked or id(current) in seen:
            continue
        seen.add(id(current))
        for link in current.links:
            node = link.from_node
            if node.type == "TEX_IMAGE" and node.image:
                return node.image
            if depth < maximum_depth:
                pending.extend((child, depth + 1) for child in node.inputs)
    return None


def _principled_node(mat):
    if not mat or not mat.use_nodes:
        return None
    return next((node for node in mat.node_tree.nodes
                 if node.type == "BSDF_PRINCIPLED"), None)


def _normal_image(mat):
    principled = _principled_node(mat)
    return _linked_image(principled.inputs.get("Normal")) if principled else None


def _opacity_image(mat):
    principled = _principled_node(mat)
    return _linked_image(principled.inputs.get("Alpha")) if principled else None


PASSTHROUGH = (".dds", ".png")


def _source_file(img):


    for attr in ("filepath_from_user", ):
        try:
            p = getattr(img, attr)()
        except Exception:
            continue
        if p and os.path.isfile(bpy.path.abspath(p)):
            return bpy.path.abspath(p)
    for raw in (img.filepath_raw, img.filepath):
        if not raw:
            continue
        p = bpy.path.abspath(raw)
        if os.path.isfile(p):
            return p
    return None


def _save_image(img, out_dir, stem):


    src = _source_file(img)
    if src:
        ext = os.path.splitext(src)[1].lower()
        if ext in PASSTHROUGH:
            name = stem + ext
            dst = os.path.join(out_dir, name)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
            return name

    packed = getattr(img, "packed_file", None)
    if packed and packed.data:
        head = bytes(packed.data[:8])
        ext = (".dds" if head[:4] == b"DDS " else
               ".png" if head[:8] == b"\x89PNG\r\n\x1a\n" else None)
        if ext:
            name = stem + ext
            with open(os.path.join(out_dir, name), "wb") as fh:
                fh.write(packed.data)
            return name

    name = stem + ".png"
    dst = os.path.join(out_dir, name)
    tmp = img.copy()
    try:
        tmp.file_format = "PNG"
        try:
            tmp.alpha_mode = "STRAIGHT"
        except Exception:
            pass
        try:
            tmp.save(filepath=dst)
        except TypeError:
            tmp.filepath_raw = dst
            tmp.save()
    finally:
        bpy.data.images.remove(tmp)
    if not os.path.isfile(dst):
        raise RuntimeError("Blender wrote no file")
    return name


def _conv(v):

    return (v[0], v[2], -v[1])


def _matrix_rows(mat, scale):


    bx = _conv((mat[0][0], mat[1][0], mat[2][0]))
    by = _conv((mat[0][1], mat[1][1], mat[2][1]))
    bz = _conv((mat[0][2], mat[1][2], mat[2][2]))
    t = _conv((mat[0][3], mat[1][3], mat[2][3]))
    right, up, fwd = bx, bz, (-by[0], -by[1], -by[2])
    return [right[0], right[1], right[2],
            up[0], up[1], up[2],
            fwd[0], fwd[1], fwd[2],
            t[0] * scale, t[1] * scale, t[2] * scale]


def _spawn_record(obj, scale):


    import math
    m = obj.matrix_world
    gt = _conv((m[0][3], m[1][3], m[2][3]))
    gt = (gt[0] * scale, gt[1] * scale, gt[2] * scale)
    gf = _conv((m[0][1], m[1][1], m[2][1]))
    yaw = math.degrees(math.atan2(gf[0], gf[2]))
    return {"name": obj.name, "position": [round(v, 4) for v in gt],
            "yaw": round(yaw, 3)}


class SK8_OT_add_spawn(Operator):
    bl_idname = "sk8.add_spawn"
    bl_label = "Add Player Spawn"
    bl_description = "Add the required spawn Empty at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        conflicts = [obj for obj in ctx.scene.objects
                     if obj.name.casefold() == SPAWN_MARKER]
        if conflicts:
            obj = conflicts[0]
            if len(conflicts) != 1 or obj.type != "EMPTY":
                self.report({"ERROR"}, "The scene must contain exactly one Empty named spawn")
                return {"CANCELLED"}
            for selected in ctx.selected_objects:
                selected.select_set(False)
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
            self.report({"INFO"}, "The scene already contains the required spawn Empty")
            return {"FINISHED"}

        obj = bpy.data.objects.new(SPAWN_MARKER, None)
        ctx.collection.objects.link(obj)
        obj.location = ctx.scene.cursor.location
        obj.empty_display_type = "ARROWS"
        obj.empty_display_size = 1.0
        obj.show_in_front = True
        for o in ctx.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj
        self.report({"INFO"}, "Added the required spawn Empty")
        return {"FINISHED"}


def _export_one(obj, path, apply_modifiers):

    prev_active = bpy.context.view_layer.objects.active
    prev_selected = list(bpy.context.selected_objects)
    prev_matrix = obj.matrix_world.copy()
    prev_hide = obj.hide_get()
    try:
        obj.hide_set(False)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        obj.matrix_world.identity()
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB",
            use_selection=True,
            export_apply=apply_modifiers,
            export_yup=True,
            export_normals=True,
            export_texcoords=True,
            export_materials="EXPORT",
            export_cameras=False,
            export_lights=False,
            export_animations=False,
            export_skins=False,
        )
    finally:
        obj.matrix_world = prev_matrix
        obj.hide_set(prev_hide)
        bpy.ops.object.select_all(action="DESELECT")
        for o in prev_selected:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        bpy.context.view_layer.objects.active = prev_active


def _glb_primitive_materials(path):

    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 12:
        raise RuntimeError("GLB header is truncated")
    magic, version, declared = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67 or version != 2 or declared != len(data):
        raise RuntimeError("GLB header is invalid")
    offset = 12
    root = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise RuntimeError("GLB chunk header is truncated")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        if length % 4 or offset + length > len(data):
            raise RuntimeError("GLB chunk length is invalid")
        if kind == 0x4E4F534A:
            root = json.loads(data[offset:offset + length].rstrip(b"\x00 \t\r\n"))
        offset += length
    if root is None:
        raise RuntimeError("GLB has no JSON document")
    names = [str(material.get("name", ""))
             for material in root.get("materials", [])]
    result = []
    for mesh in root.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if int(primitive.get("mode", 4)) != 4:
                continue
            material = primitive.get("material")
            result.append(names[int(material)] if material is not None and
                          0 <= int(material) < len(names) else None)
    if not result:
        raise RuntimeError("GLB contains no triangle primitives")
    return result


class SK8_OT_export_map(Operator):
    bl_idname = "sk8.export_map"
    bl_label = "Export Map"
    bl_description = "Write GLBs, textures and map.json into the export folder"
    bl_options = {"REGISTER"}

    def execute(self, ctx):
        s = ctx.scene.sk8_map
        selected = set(ctx.selected_objects) if s.selected_only else None
        root = bpy.path.abspath(s.folder).rstrip("/\\")
        if not root:
            self.report({"ERROR"}, "Choose an export folder first")
            return {"CANCELLED"}

        spawns = _spawn_objects(ctx)
        if len(spawns) != 1:
            self.report({"ERROR"}, "The scene must contain exactly one Empty named spawn")
            return {"CANCELLED"}

        meshes_dir = os.path.join(root, "meshes")
        tex_dir = os.path.join(root, "textures")
        for d in (root, meshes_dir, tex_dir):
            os.makedirs(d, exist_ok=True)

        if ctx.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        render = _mesh_objects(ctx, s)

        warnings = []
        try:
            npc_routes = npc_route_records(ctx.evaluated_depsgraph_get(), s.scale, selected)
            interaction_prefabs = interaction_prefab_records(ctx.evaluated_depsgraph_get(), s.scale, selected)
            trigger_effects, vfx_prefabs = native_behavior_records(ctx.evaluated_depsgraph_get(), s.scale, selected)
        except (ValueError, RuntimeError) as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        materials = {}
        surface_profiles = {}
        out_objects = []
        saved_images = {}

        def ensure_material(mat):
            mat_key = mat.name if mat else "default"
            if mat_key in materials:
                return mat_key
            entry = {
                "surface": "default",
                "srgb": True,
            }
            if mat:
                ms = mat.sk8_material
                domain = "glass_opaque" if ms.get("domain") in (5, "glass") else ms.domain
                entry["surface"] = ms.surface
                entry["srgb"] = ms.srgb
                entry["domain"] = domain
                entry["shader_override"] = domain
                if domain == "ocean":
                    entry["alpha"] = "opaque"
                    materials[mat_key] = entry
                    return mat_key
                entry["alpha_source"] = (
                    "opacity_texture" if ms.alpha_source == "auto" and
                    (ms.opacity_image or _opacity_image(mat)) is not None
                    else "base_color_texture" if ms.alpha_source == "auto"
                    else ms.alpha_source)
                entry["alpha_cutoff"] = ms.alpha_cutoff
                entry["cast_shadows"] = ms.cast_shadows
                entry["double_sided"] = (ms.double_sided or
                                         domain == "foliage")
                entry["transparent_shadow"] = ms.transparent_shadow
                if ms.alpha != "auto":
                    entry["alpha"] = ms.alpha
                if domain == "glass_opaque":
                    entry["alpha"] = "opaque"
                # Blender 4.2+ exposes surface_render_method; older builds use blend_method.
                render_method = str(getattr(mat, "surface_render_method", "") or
                                    getattr(mat, "blend_method", "")).upper()
                if render_method:
                    entry["blend_method"] = ("blend" if render_method in {"BLENDED", "BLEND"}
                                             else "clip")
                principled = _principled_node(mat)
                if principled is not None:
                    # Unlinked Principled values become the native mask texture when the
                    # material supplies no mask image of its own.
                    for socket, field in (("Roughness", "roughness"), ("Metallic", "metallic")):
                        value = principled.inputs.get(socket)
                        if value is not None and not value.is_linked:
                            entry[field] = float(value.default_value)
                        elif value is not None and not ms.mask_image:
                            try:
                                scalar_image, channel = scalar_texture_source(value)
                                if scalar_image is None:
                                    raise ValueError("no image is connected")
                                if scalar_image.name not in saved_images:
                                    stem = re.sub(r"[^\w.-]", "_", os.path.splitext(scalar_image.name)[0])
                                    saved_images[scalar_image.name] = _save_image(scalar_image, tex_dir, stem)
                                entry[field + "_texture"] = "textures/" + saved_images[scalar_image.name]
                                entry[field + "_channel"] = channel
                                entry[field + "_srgb"] = scalar_image.colorspace_settings.name.casefold() == "srgb"
                            except (ValueError, RuntimeError) as ex:
                                warnings.append(f"{mat.name} {field}: {ex}; using the socket value")
                for field in ("roughness", "metallic"):
                    scalar_image = getattr(ms, field + "_image")
                    if scalar_image is None or ms.mask_image:
                        continue
                    if scalar_image.name not in saved_images:
                        stem = re.sub(r"[^\w.-]", "_", os.path.splitext(scalar_image.name)[0])
                        saved_images[scalar_image.name] = _save_image(scalar_image, tex_dir, stem)
                    entry[field + "_texture"] = "textures/" + saved_images[scalar_image.name]
                    entry[field + "_channel"] = getattr(ms, field + "_channel")
                    entry[field + "_srgb"] = scalar_image.colorspace_settings.name.casefold() == "srgb"
                image = ms.base_color_image or _base_color_image(mat)
                if image:
                    if image.name not in saved_images:
                        stem = os.path.splitext(image.name)[0] or image.name
                        stem = "".join(c if c.isalnum() or c in "._-" else "_"
                                       for c in stem)
                        try:
                            saved_images[image.name] = _save_image(
                                image, tex_dir, stem)
                        except Exception as ex:
                            warnings.append(f"{mat.name}: could not save texture "
                                            f"'{image.name}': {ex}")
                    if image.name in saved_images:
                        entry["texture"] = "textures/" + saved_images[image.name]
                else:
                    warnings.append(f"{mat.name}: no Base Color image texture")
                extra_images = (
                    ("normal_texture", ms.normal_image or _normal_image(mat)),
                    ("mask_texture", ms.mask_image),
                    ("opacity_texture", ms.opacity_image or _opacity_image(mat)),
                )
                for field, extra_image in extra_images:
                    if (extra_image is None or
                            (field == "opacity_texture" and
                             extra_image == image and
                             entry["alpha_source"] == "base_color_texture")):
                        continue
                    if extra_image.name not in saved_images:
                        stem = (os.path.splitext(extra_image.name)[0] or
                                extra_image.name)
                        stem = "".join(c if c.isalnum() or c in "._-" else "_"
                                       for c in stem)
                        try:
                            saved_images[extra_image.name] = _save_image(
                                extra_image, tex_dir, stem)
                        except Exception as ex:
                            warnings.append(f"{mat.name}: could not save {field} "
                                            f"'{extra_image.name}': {ex}")
                    if extra_image.name in saved_images:
                        entry[field] = "textures/" + saved_images[extra_image.name]
                if (entry["alpha_source"] == "opacity_texture" and
                        "opacity_texture" not in entry):
                    warnings.append(
                        f"{mat.name}: opacity-texture alpha is selected but no "
                        "opacity image is available")
            materials[mat_key] = entry
            return mat_key

        for obj in render:
            if obj.sk8_object.placement_mode == "retail_blueprint":
                retail_blueprint = str(obj.sk8_object.retail_blueprint).strip()
                if not retail_blueprint:
                    warnings.append(
                        f"{obj.name}: Retail Blueprint Instance has no EBX asset path")
                    continue
                out_objects.append({
                    "name": obj.name,
                    "placement_mode": "retail_blueprint",
                    "retail_blueprint": retail_blueprint,
                    "retail_collision_policy": "native",
                    "transform": _matrix_rows(obj.matrix_world, s.scale),
                    "static": True,


                    "collision_mode": "none",
                })
                continue
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in obj.name)
            rel = f"meshes/{safe}.glb"
            try:
                _export_one(obj, os.path.join(root, rel), s.apply_modifiers)
                primitive_materials = _glb_primitive_materials(
                    os.path.join(root, rel))
            except Exception as ex:
                warnings.append(f"{obj.name}: GLB export failed: {ex}")
                continue

            mats = _object_material(obj)
            mats_by_name = {material.name: material for material in mats}
            for primitive_index, material_name in enumerate(primitive_materials):
                mat = mats_by_name.get(material_name)
                if mat is None and len(mats) == 1:
                    mat = mats[0]
                if material_name and mat is None:
                    warnings.append(
                        f"{obj.name}: GLB primitive {primitive_index} references "
                        f"unknown material '{material_name}'; using default")
                mat_key = ensure_material(mat)
                collision_template = _collision_template_for(obj, mat)
                surface_profile = (None if obj.sk8_object.collision_mode == "water"
                                   else _surface_profile_for(obj, mat))
                surface_profile_id = (_surface_profile_id(surface_profile)
                                      if surface_profile else None)
                if surface_profile_id:
                    stored_profile = dict(surface_profile)
                    stored_profile["name"] = obj.name
                    surface_profiles[surface_profile_id] = stored_profile
                object_record = {
                    "name": (obj.name if len(primitive_materials) == 1 else
                             f"{obj.name} / section {primitive_index + 1}"),
                    "placement_mode": "authored_mesh",
                    "mesh": rel,
                    "primitive": primitive_index,
                    "material": mat_key,
                    "transform": _matrix_rows(obj.matrix_world, s.scale),
                    "static": True,
                    "collision_mode": obj.sk8_object.collision_mode,
                    "collision_material": collision_template,
                    "collision_material_packed":
                        NATIVE_COLLISION_MATERIAL_PACKED[collision_template],
                }
                if getattr(obj.sk8_object, "round_rail", False):
                    packed = smooth_grind_packed(object_record["collision_material_packed"])
                    object_record["collision_material_packed"] = packed
                    object_record["collision_material"] = NATIVE_COLLISION_MATERIAL_IDENTIFIER.get(
                        packed, collision_template)
                    object_record["round_rail"] = True
                if surface_profile_id:
                    object_record["surface_profile"] = surface_profile_id
                if material_invisible(mat):
                    if obj.sk8_object.collision_mode == "none":
                        continue
                    object_record["render"] = False   # collision only
                if (len(primitive_materials) > 1 and
                        obj.sk8_object.collision_mode == "hull"):
                    if primitive_index == 0:
                        object_record["collision_mesh"] = rel
                    else:
                        object_record["collision_mode"] = "none"
                out_objects.append(object_record)

        # Grind curves: invisible collision prisms, each a collision piece of its own.
        grind_records, grind_notes = grind_curve_records(ctx.evaluated_depsgraph_get(), s.scale, selected)
        warnings.extend(grind_notes)
        for record in grind_records:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in record["name"])
            rel = f"meshes/grind_{safe}.glb"
            indices = record["indices"]
            mesh = bpy.data.meshes.new("GrindCurveCollision")
            mesh.from_pydata(record["positions"], [],
                             [indices[i:i + 3] for i in range(0, len(indices), 3)])
            temp = bpy.data.objects.new("GrindCurveCollision", mesh)
            ctx.scene.collection.objects.link(temp)
            try:
                _export_one(temp, os.path.join(root, rel), False)
            except Exception as ex:
                warnings.append(f"{record['name']}: grind curve export failed: {ex}")
                continue
            finally:
                bpy.data.objects.remove(temp, do_unlink=True)
                bpy.data.meshes.remove(mesh)
            out_objects.append({
                "name": record["name"] + " (grind curve)",
                "placement_mode": "authored_mesh",
                "render": False,
                "collision_mesh": rel,
                "transform": _matrix_rows(Matrix.Identity(4), s.scale),
                "static": True,
                "collision_mode": "triangle_mesh",
                "collision_material": NATIVE_COLLISION_MATERIAL_IDENTIFIER.get(record["packed"], ""),
                "collision_material_packed": record["packed"],
                "grind_curve": True,
            })

        doc = {
            "format": 1,
            "units": "meters",
            "up": "y",
            "forward": "-z",
            "generator": f"blender {bpy.app.version_string} / sk8_map_export "
                         f"{'.'.join(str(v) for v in bl_info['version'])}",
            "materials": materials,
            "surface_profiles": surface_profiles,
            "streaming_distances": {
                "near": s.shadow_distance_low,
                "medium": s.shadow_distance_medium,
                "far": s.shadow_distance_high,
            },
            "objects": out_objects,
            "lights": light_records(ctx.evaluated_depsgraph_get(), s.scale, selected),
            "audio_volumes": audio_volume_records(ctx.evaluated_depsgraph_get(), s.scale, selected),
            "audio_emitters": audio_emitter_records(ctx.evaluated_depsgraph_get(), s.scale, selected),
            "interaction_prefabs": interaction_prefabs,
            "trigger_effects": trigger_effects,
            "vfx_prefabs": vfx_prefabs,
            "npc_routes": npc_routes,
            "warnings": warnings,
        }
        doc["spawn"] = _spawn_record(spawns[0], s.scale)

        with open(os.path.join(root, "map.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        msg = (f"{len(out_objects)} objects, {len(materials)} materials, "
               f"{len(saved_images)} textures, {len(doc['lights'])} lights")
        if warnings:
            msg += f", {len(warnings)} warnings"
            for w in warnings[:5]:
                self.report({"WARNING"}, w)
        self.report({"INFO"}, "Exported " + msg)
        return {"FINISHED"}


class SK8_OT_apply_collision_selected(Operator):
    bl_idname = "sk8.apply_collision_selected"
    bl_label = "Apply Collision Settings to Selected Meshes"
    bl_description = ("Copy collision shape and/or the full object-owned "
                      "donor-and-overrides surface from the active mesh")
    bl_options = {"REGISTER", "UNDO"}

    apply_mode: BoolProperty(
        name="Collision Shape", default=True, options={"SKIP_SAVE"})
    apply_surface: BoolProperty(
        name="Gameplay Surface", default=True, options={"SKIP_SAVE"})
    collision_mode: EnumProperty(
        name="Collision Type",
        items=[("__active__", "Copy Active", "Copy the active mesh's type")] +
              COLLISION_MODES,
        default="__active__",
        options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, ctx):
        active = ctx.object
        return (active is not None and active.type == "MESH" and
                not _is_spawn(active) and
                getattr(active, "sk8_object", None) is not None)

    def execute(self, ctx):
        if not self.apply_mode and not self.apply_surface:
            self.report({"WARNING"}, "Choose a collision field to apply")
            return {"CANCELLED"}
        mode = (ctx.object.sk8_object.collision_mode
                if self.collision_mode == "__active__"
                else self.collision_mode)
        source = ctx.object.sk8_object
        targets = [obj for obj in ctx.selected_objects
                   if obj.type == "MESH" and not _is_spawn(obj)]
        if not targets:
            self.report({"WARNING"}, "No non-spawn meshes are selected")
            return {"CANCELLED"}
        for obj in targets:
            if self.apply_mode:
                obj.sk8_object.collision_mode = mode


                obj["sk8_collision_mode"] = mode
            if self.apply_surface:
                target = obj.sk8_object
                for key in (
                        "custom_friction",
                        "deck_dynamic_friction", "deck_static_friction",
                        "deck_restitution", "truck_dynamic_friction",
                        "truck_static_friction", "truck_restitution",
                        "wheel_dynamic_friction", "wheel_static_friction",
                        "wheel_restitution", "custom_behavior",
                        "exclude_from_edge_generation",
                        "include_in_surface_analysis", "exclude_from_grinding",
                        "jump_pad", "boost_pad", "wipeout", "slide",
                        "do_not_align", "stairs", "camera_occluder"):
                    setattr(target, key, getattr(source, key))
                _sync_surface_profile(target, ctx)
        label = next((item[1] for item in COLLISION_MODES if item[0] == mode), mode)
        fields = ("collision shape and gameplay surface"
                  if self.apply_mode and self.apply_surface else
                  "collision shape" if self.apply_mode else "gameplay surface")
        detail = f" ({label})" if self.apply_mode else ""
        self.report({"INFO"}, f"Applied {fields}{detail} to {len(targets)} meshes")
        return {"FINISHED"}


def _context_material(ctx):
    material = getattr(ctx, "material", None)
    if material is not None:
        return material
    return getattr(getattr(ctx, "object", None), "active_material", None)


class SK8_OT_choose_collision_surface(Operator):

    bl_idname = "sk8.choose_collision_surface"
    bl_label = "Find Surface Audio Donor"
    bl_description = ("Search all 279 shipped gameplay surface templates and "
                      "assign one to the active Blender material")
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "surface"

    surface: EnumProperty(
        name="Gameplay Surface Template",
        items=NATIVE_COLLISION_MATERIALS,
        default=DEFAULT_COLLISION_MATERIAL)

    @classmethod
    def poll(cls, ctx):
        return _context_material(ctx) is not None

    def invoke(self, ctx, _event):
        material = _context_material(ctx)
        if material is None:
            return {"CANCELLED"}
        self.surface = material.sk8_material.collision_material
        ctx.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, ctx):
        material = _context_material(ctx)
        if material is None:
            self.report({"WARNING"}, "Select a material first")
            return {"CANCELLED"}
        material.sk8_material.collision_material = self.surface
        info = _native_collision_template_info(self.surface)
        self.report({"INFO"},
                    f"Selected native template {info['network_id']:03d} "
                    f"(Packed={info['packed']})")
        return {"FINISHED"}


class SK8_OT_open_folder(Operator):
    bl_idname = "sk8.open_folder"
    bl_label = "Open Export Folder"

    def execute(self, ctx):
        p = bpy.path.abspath(ctx.scene.sk8_map.folder)
        if p and os.path.isdir(p):
            import subprocess, sys
            if sys.platform == "win32":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
            return {"FINISHED"}
        self.report({"ERROR"}, "Export folder does not exist yet")
        return {"CANCELLED"}


def _draw_surface_audio_settings(layout, settings):
    audio = layout.box()
    audio.prop(settings, "custom_audio")
    audio.label(text="Sound event references remain inherited from the donor.",
                icon="INFO")
    if settings.custom_audio:
        audio.prop(settings, "audio_softness")
        audio.prop(settings, "audio_smoothness")
        audio.prop(settings, "audio_min_impact_force")
        audio.prop(settings, "audio_impact_cooldown_release")
        audio.prop(settings, "audio_ignore_player_collisions")


def _draw_surface_profile_settings(layout, settings):
    friction = layout.box()
    friction.prop(settings, "custom_friction")
    if settings.custom_friction:
        for part in ("deck", "truck", "wheel"):
            friction.label(text=part.title())
            row = friction.row(align=True)
            row.prop(settings, f"{part}_dynamic_friction")
            row.prop(settings, f"{part}_static_friction")
            row.prop(settings, f"{part}_restitution")

    behavior = layout.box()
    behavior.prop(settings, "custom_behavior")
    if settings.custom_behavior:
        behavior.label(text="Build-Applied Mesh Behavior (Custom Override)",
                       icon="OPTIONS")
        behavior.label(text="These replace all behavior flags from the donor.",
                       icon="INFO")
        for field, _native_property, _label in BEHAVIOR_FIELDS:
            behavior.prop(settings, field)


def _shows_on_pause_map(obj) -> bool:
    # What the pause map's render can draw: geometry, and collection instances of it.
    if obj is None:
        return False
    if obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
        return not _is_legacy_spawn_mesh(obj)
    return (obj.type == "EMPTY" and obj.instance_type == "COLLECTION" and
            obj.instance_collection is not None)


def _draw_pause_map_setting(layout, obj):
    overview = layout.box()
    overview.label(text="Map Overview", icon="IMAGE_DATA")
    overview.prop(obj.sk8_object, "hide_from_pause_map")
    if obj.sk8_object.hide_from_pause_map:
        overview.label(text="Left out of the pause map picture; still exported.",
                       icon="HIDE_ON")
    overview.label(text="Alt+click sets every selected object.", icon="INFO")


def _draw_collision_authoring(layout, ctx):
    active = ctx.object
    if _shows_on_pause_map(active):
        _draw_pause_map_setting(layout, active)
    if active is None or active.type != "MESH":
        layout.label(text="Select a mesh to author its collision", icon="INFO")
        return
    if _is_spawn(active):
        layout.label(text="Player spawn markers do not export collision", icon="INFO")
        return

    settings = active.sk8_object
    layout.label(text=f"Active mesh: {active.name}")

    placement = layout.box()
    placement.label(text="Native Placement", icon="OUTLINER_OB_GROUP_INSTANCE")
    placement.prop(settings, "placement_mode", text="Mode")
    if settings.placement_mode == "retail_blueprint":
        placement.prop(settings, "retail_blueprint", text="ObjectBlueprint")
        placement.label(text="Uses the shipped mesh, materials, physics, and audio.",
                        icon="INFO")
        placement.label(text="Supported source: BAM base-bundle EBX assets only.")
        placement.label(text="The Blender mesh is a placement proxy and is not exported.")
        return

    surface = layout.box()
    surface.label(text="Object Gameplay Overrides", icon="OBJECT_DATA")
    surface.enabled = settings.collision_mode != "water"
    material = active.active_material
    surface.label(text="Surface audio is set in Material > Skate Map.", icon="MATERIAL")
    if not _material_audio_is_explicit(material) and _object_surface_is_explicit(active):
        surface.label(text="Legacy object audio is used until the material is set.", icon="INFO")
    _draw_surface_profile_settings(surface, settings)
    surface.label(text="Saved on this object; shared materials cannot leak it.",
                  icon="CHECKMARK")
    if len(_object_material(active)) > 1:
        surface.label(text="Every material exports as a native mesh section.",
                      icon="CHECKMARK")

    geometry = layout.box()
    geometry.label(text="Collision Geometry (Object)", icon="MOD_PHYSICS")
    geometry.prop(settings, "collision_mode", text="Shape")
    if settings.collision_mode not in ("none", "water"):
        geometry.prop(settings, "round_rail")
        if settings.round_rail:
            geometry.label(text="Grinds as a smooth surface at any side count.", icon="INFO")
        geometry.label(text="Still not grindable? Draw a Grind Curve on it (Map Tools).")
    if settings.collision_mode == "convex_parts":
        geometry.label(text="Closed convex meshes stay one hull; others decompose.",
                       icon="INFO")
    elif settings.collision_mode == "hull":
        geometry.label(text="One envelope fills holes and concave spaces.", icon="INFO")
    elif settings.collision_mode == "triangle_mesh":
        geometry.label(text="Uses the evaluated render faces exactly.", icon="INFO")
    elif settings.collision_mode == "water":
        geometry.label(text="Native water interaction for the skater/ragdoll.", icon="INFO")
        geometry.label(text="Uses the Water preset and stock water material.")
        geometry.label(text="Solid-surface overrides above are not applied.")
    else:
        geometry.label(text="Render only; no collision is emitted.", icon="INFO")

    selected = [obj for obj in ctx.selected_objects
                if obj.type == "MESH" and not _is_spawn(obj)]
    batch = layout.box()
    batch.label(text=f"Edit {len(selected)} Selected Meshes", icon="RESTRICT_SELECT_OFF")
    batch.prop(ctx.scene.sk8_map, "batch_collision_mode",
               text="Collision Type")
    row = batch.row(align=True)
    op = row.operator("sk8.apply_collision_selected", text="Set Type")
    op.apply_mode = True
    op.apply_surface = False
    op.collision_mode = ctx.scene.sk8_map.batch_collision_mode
    op = row.operator("sk8.apply_collision_selected", text="Set Overrides")
    op.apply_mode = False
    op.apply_surface = True
    op = row.operator("sk8.apply_collision_selected", text="Set Both")
    op.apply_mode = True
    op.apply_surface = True
    op.collision_mode = ctx.scene.sk8_map.batch_collision_mode
    batch.label(text="Copies object gameplay overrides; material audio is unchanged.",
                icon="INFO")


# ---------------------------------------------------------------------------
# Merge meshes by material. Ripped maps arrive as thousands of small objects,
# often with duplicated materials ("Concrete", "Concrete.001") that use the
# same images. Every object costs export time and a mesh in the package, so
# objects that share materials and export settings are joined into one, after
# folding identical materials together.

def _property_group_signature(group):
    if group is None:
        return ()
    out = []
    for prop in group.bl_rna.properties:
        if prop.identifier in ("rna_type", "name"):
            continue
        value = getattr(group, prop.identifier, None)
        if hasattr(value, "bl_rna"):
            value = _property_group_signature(value)
        elif hasattr(value, "__iter__") and not isinstance(value, str):
            try:
                value = tuple(_property_group_signature(v) if hasattr(v, "bl_rna") else v
                              for v in value)
            except TypeError:
                value = repr(value)
        out.append((prop.identifier, value))
    return tuple(out)


def _socket_default(socket):
    value = getattr(socket, "default_value", None)
    if value is None:
        return None
    try:
        return tuple(round(v, 4) for v in value)
    except TypeError:
        return round(value, 4) if isinstance(value, float) else value


def _material_fingerprint(mat):
    # Node types, their images and every unlinked input value, plus the link
    # topology: two materials with the same fingerprint render the same.
    nodes = []
    links = []
    if mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type in ("FRAME", "REROUTE"):
                continue
            image = getattr(node, "image", None)
            inputs = tuple(sorted((sock.name, _socket_default(sock))
                                  for sock in node.inputs if not sock.is_linked))
            extra = ()
            if node.type == "TEX_IMAGE":
                extra = (node.extension, node.interpolation, node.projection)
            nodes.append((node.name, node.type, image.name if image else None, inputs, extra))
        for link in mat.node_tree.links:
            links.append((link.from_node.name, link.from_socket.identifier,
                          link.to_node.name, link.to_socket.identifier))
    else:
        nodes.append(("", "SOLID", None, (("Color", tuple(round(c, 4) for c in mat.diffuse_color)),), ()))
    return (tuple(sorted(nodes)), tuple(sorted(links)),
            getattr(mat, "blend_method", None), getattr(mat, "surface_render_method", None),
            mat.use_backface_culling, round(getattr(mat, "alpha_threshold", 0.0), 4),
            _property_group_signature(getattr(mat, "sk8_material", None)))


def _mergeable_mesh(obj, ctx):
    if obj.type != "MESH" or obj.data is None or obj.library or obj.data.library:
        return "not a local mesh"
    if _is_legacy_spawn_mesh(obj) or obj.get("sk8_guide"):
        return "marker"
    if obj.sk8_object.placement_mode == "retail_blueprint":
        return "retail blueprint"
    if obj.data.shape_keys or obj.animation_data or obj.constraints:
        return "animated or constrained"
    if not obj.visible_get(view_layer=ctx.view_layer):
        return "hidden"
    if not obj.data.polygons:
        return "no faces"
    return None


def _join_group_fast(target, others):
    """Builds one mesh from `target` and `others` (world placement baked into the target's
    space) straight through the data API. `bpy.ops.object.join` re-syncs the whole view layer
    for every object it removes, which is quadratic in scene size (13k objects: ten minutes);
    this keeps exactly what the exporter reads - positions, faces, every UV layer, material
    indices and corner normals (written back as custom normals) - and lets the caller delete
    the leftover objects in one batch."""
    import numpy as np

    inverse = target.matrix_world.inverted()
    parts = [(target, Matrix.Identity(4))] + [(obj, inverse @ obj.matrix_world) for obj in others]
    materials = []
    material_index = {}
    active_uv = None
    uv_names = []
    for obj, _ in parts:
        for slot in obj.material_slots:
            mat = slot.material
            key = mat.name if mat else None
            if key not in material_index:
                material_index[key] = len(materials)
                materials.append(mat)
        mesh = obj.data
        if mesh.uv_layers:
            active = mesh.uv_layers.active or mesh.uv_layers[0]
            if active_uv is None:
                active_uv = active.name
            for layer in mesh.uv_layers:
                name = active_uv if layer == active else layer.name
                if name not in uv_names:
                    uv_names.append(name)

    positions = []
    loop_vertices = []
    loop_starts = []
    loop_totals = []
    poly_materials = []
    normals = []
    uvs = {name: [] for name in uv_names}
    vertex_base = 0
    loop_base = 0
    for obj, matrix in parts:
        mesh = obj.data
        vertex_count = len(mesh.vertices)
        loop_count = len(mesh.loops)
        poly_count = len(mesh.polygons)
        if vertex_count == 0 or poly_count == 0:
            continue
        co = np.empty(vertex_count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3).astype(np.float64)
        rotation = np.array(matrix.to_3x3(), dtype=np.float64)
        translation = np.array(matrix.translation, dtype=np.float64)
        positions.append(co @ rotation.T + translation)
        lv = np.empty(loop_count, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", lv)
        loop_vertices.append(lv + vertex_base)
        ls = np.empty(poly_count, dtype=np.int32)
        lt = np.empty(poly_count, dtype=np.int32)
        mi = np.empty(poly_count, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", ls)
        mesh.polygons.foreach_get("loop_total", lt)
        mesh.polygons.foreach_get("material_index", mi)
        loop_starts.append(ls + loop_base)
        loop_totals.append(lt)
        slots = obj.material_slots
        remap = np.array([material_index[slots[i].material.name if i < len(slots) and slots[i].material else None]
                          if i < len(slots) else material_index.get(None, 0)
                          for i in range(max(1, len(slots)))], dtype=np.int32)
        mi = np.clip(mi, 0, len(remap) - 1)
        poly_materials.append(remap[mi])
        normal = np.empty(loop_count * 3, dtype=np.float32)
        mesh.corner_normals.foreach_get("vector", normal)
        normal = normal.reshape(-1, 3).astype(np.float64)
        normal_matrix = np.linalg.inv(rotation).T
        normal = normal @ normal_matrix.T
        lengths = np.linalg.norm(normal, axis=1)
        lengths[lengths == 0.0] = 1.0
        normals.append(normal / lengths[:, None])
        present = {}
        if mesh.uv_layers:
            active = mesh.uv_layers.active or mesh.uv_layers[0]
            for layer in mesh.uv_layers:
                name = active_uv if layer == active else layer.name
                uv = np.empty(loop_count * 2, dtype=np.float32)
                layer.data.foreach_get("uv", uv)
                present[name] = uv.reshape(-1, 2)
        for name in uv_names:
            uvs[name].append(present.get(name, np.zeros((loop_count, 2), dtype=np.float32)))
        vertex_base += vertex_count
        loop_base += loop_count

    merged = bpy.data.meshes.new(target.data.name)
    if positions:
        all_positions = np.concatenate(positions)
        all_loops = np.concatenate(loop_vertices)
        all_starts = np.concatenate(loop_starts)
        all_totals = np.concatenate(loop_totals)
        merged.vertices.add(len(all_positions))
        merged.loops.add(len(all_loops))
        merged.polygons.add(len(all_starts))
        merged.vertices.foreach_set("co", all_positions.astype(np.float32).ravel())
        merged.loops.foreach_set("vertex_index", all_loops)
        merged.polygons.foreach_set("loop_start", all_starts)
        merged.polygons.foreach_set("loop_total", all_totals)
        merged.polygons.foreach_set("material_index", np.concatenate(poly_materials))
        merged.update(calc_edges=True)
        for name in uv_names:
            layer = merged.uv_layers.new(name=name, do_init=False)
            layer.data.foreach_set("uv", np.concatenate(uvs[name]).astype(np.float32).ravel())
        if active_uv is not None:
            merged.uv_layers.active = merged.uv_layers[active_uv]
            merged.uv_layers[active_uv].active_render = True
        merged.normals_split_custom_set(np.concatenate(normals).astype(np.float32))
    for mat in materials:
        merged.materials.append(mat)
    previous = target.data
    target.data = merged
    for slot in target.material_slots:
        slot.link = "DATA"
    return previous


class SK8_OT_merge_by_material(Operator):
    bl_idname = "sk8.merge_by_material"
    bl_label = "Merge Meshes by Material"
    bl_description = ("Join meshes that share the same materials and Skate settings into one "
                      "object per material set (and per grid cell), first folding materials "
                      "that use the same textures together. Cuts object and mesh counts")
    bl_options = {"REGISTER", "UNDO"}

    selected_only: BoolProperty(
        name="Selected Only", default=False,
        description="Only merge the selected meshes; otherwise every exportable mesh in the scene")
    merge_duplicate_materials: BoolProperty(
        name="Fold Duplicate Materials", default=True,
        description="Materials whose textures, values and Skate settings are identical are "
                    "replaced by one of them first, so their meshes can merge")
    cell_size: FloatProperty(
        name="Grid Cell (m)", default=64.0, min=0.0, soft_max=500.0,
        description="Meshes only merge with others whose centre lies in the same square cell "
                    "of this size, keeping merged objects local; 0 merges across the whole map")
    respect_collections: BoolProperty(
        name="Keep Collections Apart", default=True,
        description="Only merge meshes that live in the same collection")
    apply_modifiers: BoolProperty(
        name="Apply Modifiers", default=True,
        description="Apply modifiers before joining; otherwise meshes with modifiers are skipped")

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == "OBJECT" and ctx.scene is not None

    def invoke(self, ctx, event):
        self.selected_only = any(o.type == "MESH" for o in ctx.selected_objects)
        return self.execute(ctx)

    def execute(self, ctx):
        started = time.perf_counter()
        source = ctx.selected_objects if self.selected_only else ctx.scene.objects
        candidates = []
        skipped = {}
        for obj in source:
            reason = _mergeable_mesh(obj, ctx)
            if reason is None and obj.modifiers and not self.apply_modifiers:
                reason = "has modifiers"
            if reason:
                if obj.type == "MESH":
                    skipped[reason] = skipped.get(reason, 0) + 1
                continue
            candidates.append(obj)
        if not candidates:
            self.report({"WARNING"}, "No mergeable meshes" + (" are selected" if self.selected_only else ""))
            return {"CANCELLED"}

        # Modifiers are applied from ONE evaluation of the scene: converting per group made
        # Blender re-evaluate every object each time, which is what made large rips crawl.
        modified = [obj for obj in candidates if obj.modifiers]
        if modified:
            depsgraph = ctx.evaluated_depsgraph_get()
            applied = 0
            for obj in modified:
                evaluated = obj.evaluated_get(depsgraph)
                mesh = bpy.data.meshes.new_from_object(
                    evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
                previous = obj.data
                mesh.name = previous.name
                obj.data = mesh
                obj.modifiers.clear()
                if previous.users == 0:
                    bpy.data.meshes.remove(previous)
                applied += 1
            candidates = [obj for obj in candidates if obj.data.polygons]
            print(f"[sk8 merge] applied modifiers on {applied} meshes in {time.perf_counter() - started:.1f}s")

        folded = 0
        if self.merge_duplicate_materials:
            canonical = {}
            by_print = {}
            for obj in candidates:
                for slot in obj.material_slots:
                    mat = slot.material
                    if mat is None or mat.name in canonical:
                        continue
                    key = _material_fingerprint(mat)
                    keeper = by_print.get(key)
                    if keeper is None or mat.name < keeper.name:
                        by_print[key] = mat
                    canonical[mat.name] = key
            for obj in candidates:
                for slot in obj.material_slots:
                    mat = slot.material
                    if mat is None:
                        continue
                    keeper = by_print[canonical[mat.name]]
                    if keeper != mat:
                        slot.material = keeper
                        folded += 1

        def cell_of(obj):
            if self.cell_size <= 0:
                return ()
            centre = sum((obj.matrix_world @ Vector(corner) for corner in obj.bound_box), Vector()) / 8.0
            return (math.floor(centre.x / self.cell_size), math.floor(centre.y / self.cell_size))

        groups = {}
        for obj in candidates:
            materials = tuple(sorted(m.name for m in _object_material(obj)))
            key = (
                tuple(c.name for c in obj.users_collection) if self.respect_collections else (),
                obj.parent.name if obj.parent else "",
                materials,
                _property_group_signature(obj.sk8_object),
                bool(obj.get("sk8_no_collision")),
                len(obj.data.uv_layers),
                cell_of(obj),
            )
            groups.setdefault(key, []).append(obj)
        work = [group for group in groups.values() if len(group) > 1]
        for group in work:
            group.sort(key=lambda o: (o.name.casefold(), o.name))

        merged_away = 0
        removed_objects = []
        stale_meshes = []
        wm = ctx.window_manager
        wm.progress_begin(0, max(1, len(work)))
        try:
            for index, group in enumerate(work):
                target = group[0]
                stale_meshes.append(_join_group_fast(target, group[1:]))
                removed_objects.extend(group[1:])
                merged_away += len(group) - 1
                wm.progress_update(index + 1)
                if (index + 1) % 100 == 0 or index + 1 == len(work):
                    print(f"[sk8 merge] {index + 1}/{len(work)} groups joined, "
                          f"{merged_away} meshes to remove, {time.perf_counter() - started:.1f}s")
        finally:
            wm.progress_end()
        if removed_objects:
            stale_meshes.extend(obj.data for obj in removed_objects)
            bpy.data.batch_remove(removed_objects)
            orphans = {mesh.name for mesh in stale_meshes if mesh is not None}
            bpy.data.batch_remove([mesh for mesh in bpy.data.meshes if mesh.name in orphans and mesh.users == 0])
            print(f"[sk8 merge] removed {len(removed_objects)} objects, {time.perf_counter() - started:.1f}s")

        if not merged_away and not folded:
            self.report({"INFO"}, f"Nothing to merge: {len(candidates)} meshes already use distinct material sets")
            return {"FINISHED"}
        detail = f"Merged {merged_away + len(work)} meshes into {len(work)} in {time.perf_counter() - started:.0f}s"
        if folded:
            detail += f"; {folded} material slots folded onto duplicates"
        if skipped:
            detail += "; skipped " + ", ".join(f"{count} {reason}" for reason, count in sorted(skipped.items()))
        self.report({"INFO"}, detail)
        return {"FINISHED"}


class SK8_PT_sidebar_map(Panel):
    bl_order = 0
    bl_label = "Map Tools"
    bl_idname = "SK8_PT_sidebar_map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    def draw(self, ctx):
        col = self.layout.column(align=True)
        col.label(text="Add Object")
        col.operator("sk8.add_spawn", icon="OUTLINER_OB_ARMATURE")
        col.operator("sk8.add_bus_stop", icon="AUTO").shelter = True
        col.operator("sk8.add_bus_stop", text="Add Fast Travel Point", icon="EMPTY_SINGLE_ARROW").shelter = False
        col.operator("sk8.refresh_bus_stops", icon="FILE_REFRESH")
        col.operator("sk8.add_audio_volume", icon="SPEAKER")
        col.operator("sk8.add_audio_emitter", icon="SPEAKER")
        col.operator("sk8.add_speaker_interaction", icon="EMPTY_AXIS")
        col.menu("SK8_MT_native_trigger_effects", icon="PARTICLES")
        col.menu("SK8_MT_native_vfx", icon="PARTICLES")
        col.menu("SK8_MT_add_npc_route", icon="CURVE_BEZCURVE")
        col.operator("sk8.add_grind_curve", icon="CURVE_PATH")
        col.operator("sk8.auto_grind_curves", icon="CURVE_PATH")
        col.operator("sk8.grind_curve_from_edges", icon="EDGESEL")
        col.separator()
        optimize = col.box()
        optimize.label(text="Optimize", icon="MOD_BOOLEAN")
        optimize.operator("sk8.merge_by_material", icon="AUTOMERGE_ON")


class SK8_PT_sidebar_collision_authoring(Panel):
    bl_order = 30
    bl_label = "Collision Authoring"
    bl_idname = "SK8_PT_sidebar_collision_authoring"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    def draw(self, ctx):
        _draw_collision_authoring(self.layout, ctx)


# A bus stop is an Empty whose name starts with "TravelPoint" ("TravelPoint.001", ...). The
# game's shelter is spawned there, facing the Empty's +Y, and the stop is a fast-travel point
# on the pause map under its "bus_stop_name".
TRAVEL_POINT_MARKER = "TravelPoint"
BUS_STOP_NAME = "bus_stop_name"
BUS_STOP_SHELTER = "bus_stop_shelter"
# The preview, in the Empty's own axes, from the game's data. The stop's forward is the Empty's
# +Y and its right is the Empty's -X. The shelter mesh (cmn_prp_busstop_02) spans 1.7 m across
# and 4.0 m along that forward axis, 2.6 m tall, standing on the origin. Studio moves the
# classes' arrival point: a shelter lands players 2.5 m out on its right, a bare point on itself.
BUS_STOP_SHELTER_MIN = (-0.83, -2.01, 0.0)
BUS_STOP_SHELTER_MAX = (0.87, 2.02, 2.61)
BUS_STOP_SHELTER_ARRIVAL = (-2.5, 0.0, 0.0)
# A bare point has nothing standing on it; a 1.2 m cube on the ground makes it easy to see and grab.
BUS_STOP_BARE_MIN = (-0.6, -0.6, 0.0)
BUS_STOP_BARE_MAX = (0.6, 0.6, 1.2)
BUS_STOP_BARE_ARRIVAL = (0.0, 0.0, 0.0)


def is_travel_point(obj) -> bool:
    return (obj is not None and obj.type == "EMPTY" and
            obj.name.casefold().startswith(TRAVEL_POINT_MARKER.casefold()))


def rebuild_bus_stop_preview(stop):
    # The shelter's box (or a cube for a bare point) standing on the ground at the
    # Empty, and a cross on the ground where players arrive. Never exported.
    for child in [child for child in stop.children if child.get("sk8_guide", False)]:
        mesh = child.data
        bpy.data.objects.remove(child)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    shelter = bool(stop.get(BUS_STOP_SHELTER, True))
    if shelter:
        (x0, y0, z0), (x1, y1, z1) = BUS_STOP_SHELTER_MIN, BUS_STOP_SHELTER_MAX
        ax, ay, az = BUS_STOP_SHELTER_ARRIVAL
    else:
        (x0, y0, z0), (x1, y1, z1) = BUS_STOP_BARE_MIN, BUS_STOP_BARE_MAX
        ax, ay, az = BUS_STOP_BARE_ARRIVAL
    vertices = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
                (ax - 0.5, ay, az), (ax + 0.5, ay, az), (ax, ay - 0.5, az), (ax, ay + 0.5, az)]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7), (8, 9), (10, 11)]
    if shelter:
        # The shelter opens towards the Empty's -X: an arrow from that side, square to the
        # box, out to where players arrive. Nothing here is drawn at an angle, so the only
        # direction the preview shows is the shelter's front.
        tip = ax + 0.5
        start = len(vertices)
        vertices += [(x0, 0.0, az), (tip, 0.0, az), (tip + 0.5, 0.35, az), (tip + 0.5, -0.35, az)]
        edges += [(start, start + 1), (start + 1, start + 2), (start + 1, start + 3)]
    else:
        # A bare point has no front; its arrow is the way players face when they arrive, the
        # Empty's +Y, as for the player spawn.
        start = len(vertices)
        vertices += [(0.0, y1, az), (0.0, y1 + 1.5, az), (0.35, y1 + 1.0, az), (-0.35, y1 + 1.0, az)]
        edges += [(start, start + 1), (start + 1, start + 2), (start + 1, start + 3)]
    mesh = bpy.data.meshes.new("BusStopGuide")
    mesh.from_pydata(vertices, edges, [])
    guide = bpy.data.objects.new("BusStopGuide", mesh)
    for collection in (stop.users_collection or [bpy.context.scene.collection]):
        collection.objects.link(guide)
    guide.parent = stop
    guide.display_type = "WIRE"
    guide.hide_render = True
    guide.hide_select = True
    guide["sk8_guide"] = True
    return guide


class SK8_OT_add_bus_stop(Operator):
    bl_idname = "sk8.add_bus_stop"
    bl_label = "Add Bus Stop"
    bl_description = ("Add a fast-travel point at the 3D cursor, with the game's bus shelter or "
                      "bare. Put it on the ground; name it in the Object properties")
    bl_options = {"REGISTER", "UNDO"}

    shelter: BoolProperty(
        name="Shelter",
        description="Spawn the game's bus shelter here; off makes a bare fast-travel point",
        default=True)

    def execute(self, ctx):
        count = sum(1 for obj in ctx.scene.objects if is_travel_point(obj))
        stop = bpy.data.objects.new(TRAVEL_POINT_MARKER, None)
        ctx.collection.objects.link(stop)
        stop.location = ctx.scene.cursor.location
        stop.empty_display_type = "PLAIN_AXES"
        stop.empty_display_size = 0.5
        stop.show_in_front = True
        stop[BUS_STOP_NAME] = f"Bus Stop {count + 1}" if self.shelter else f"Travel Point {count + 1}"
        stop[BUS_STOP_SHELTER] = bool(self.shelter)
        rebuild_bus_stop_preview(stop)

        for o in ctx.selected_objects:
            o.select_set(False)
        stop.select_set(True)
        ctx.view_layer.objects.active = stop
        self.report({"INFO"}, "Added a bus stop; set its name under Object properties > Skate Bus Stop")
        return {"FINISHED"}


class SK8_OT_refresh_bus_stops(Operator):
    bl_idname = "sk8.refresh_bus_stops"
    bl_label = "Refresh Bus Stop Previews"
    bl_description = ("Rebuild the preview of every bus stop and fast-travel point: after an add-on "
                      "update, or after changing a stop's Bus shelter setting")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, ctx):
        stops = [obj for obj in ctx.scene.objects if is_travel_point(obj)]
        for stop in stops:
            if BUS_STOP_SHELTER not in stop:
                stop[BUS_STOP_SHELTER] = True
            rebuild_bus_stop_preview(stop)
        self.report({"INFO"}, f"Rebuilt {len(stops)} bus stop previews")
        return {"FINISHED"}


class SK8_PT_bus_stop(Panel):
    bl_label = "Skate Bus Stop"
    bl_idname = "SK8_PT_bus_stop"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, ctx):
        return is_travel_point(ctx.object)

    def draw(self, ctx):
        col = self.layout.column()
        if BUS_STOP_NAME in ctx.object:
            col.prop(ctx.object, f'["{BUS_STOP_NAME}"]', text="Stop name")
        else:
            col.label(text="No stop name: the object's name is used", icon="INFO")
        if BUS_STOP_SHELTER in ctx.object:
            col.prop(ctx.object, f'["{BUS_STOP_SHELTER}"]', text="Bus shelter")
        else:
            col.label(text="Bus shelter: on (Refresh adds the setting)", icon="INFO")
        col.label(text="Put the Empty on the ground: the preview stands on it")
        if bool(ctx.object.get(BUS_STOP_SHELTER, True)):
            col.label(text="The arrow is the shelter's open front")
        else:
            col.label(text="The arrow is the way players face on arrival")
        col.label(text="Players arrive at the cross")
        col.operator("sk8.refresh_bus_stops", icon="FILE_REFRESH")
        col.label(text="Names are limited to about 35 characters")


class SK8_PT_object(Panel):
    bl_label = "Skate Map Collision Authoring"
    bl_idname = "SK8_PT_object"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and (ctx.object.type == "MESH" or
                                           _shows_on_pause_map(ctx.object))

    def draw(self, ctx):
        _draw_collision_authoring(self.layout, ctx)


class SK8_PT_sidebar_light(Panel):
    bl_order = 40
    bl_label = "Light Authoring"
    bl_idname = "SK8_PT_sidebar_light"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skate Map"

    @classmethod
    def poll(cls, ctx):
        return ctx.object is not None and ctx.object.type == "LIGHT"

    def draw(self, ctx):
        col = self.layout.column(align=True)
        light = ctx.object.data
        settings = ctx.object.sk8_light
        if light.type not in {"POINT", "SPOT", "AREA"}:
            col.label(text="Sun/sky lighting uses the game's TOD.", icon="INFO")
            return
        col.prop(light, "color")
        col.prop(light, "energy")
        if hasattr(light, "exposure"):
            col.prop(light, "exposure")
        if light.type in {"POINT", "SPOT"}:
            col.prop(light, "shadow_soft_size")
        if light.type == "SPOT":
            col.prop(light, "spot_size")
            col.prop(light, "spot_blend")
        if light.type == "AREA":
            col.prop(settings, "area_mode")
            col.prop(light, "shape")
            col.prop(light, "size")
            if light.shape in {"RECTANGLE", "ELLIPSE"}:
                col.prop(light, "size_y")
            if settings.area_mode == "SPOTLIGHT" and hasattr(light, "spread"):
                col.prop(light, "spread", text="Cone Angle")
            if light.shape in {"DISK", "ELLIPSE"}:
                col.label(text="Exports as rectangular bounds.", icon="INFO")
        if hasattr(light, "use_shadow"):
            col.prop(light, "use_shadow")
        col.prop(settings, "attenuation_radius")
        if light.use_custom_distance and not settings.is_property_set("attenuation_radius"):
            col.label(text=f"Using Blender Custom Distance: {light.cutoff_distance:g} m")
        col.separator()
        col.label(text="Active Time of Day")
        col.prop(settings, "time_of_day", expand=True)
        col.label(text="All: always on. None: not exported.")
        col.label(text="Direct lighting only; no baked GI.", icon="INFO")


class SK8_PT_material(Panel):
    bl_label = "Skate Map"
    bl_idname = "SK8_PT_material"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    @classmethod
    def poll(cls, ctx):
        return getattr(ctx, "material", None) is not None

    def draw(self, ctx):
        m = ctx.material.sk8_material
        col = self.layout.column(align=True)
        render = col.box()
        render.label(text="Native Render Material", icon="SHADING_RENDERED")
        render.prop(m, "invisible")
        if m.invisible:
            render.label(text="Not drawn in game; its faces keep their collision.", icon="HIDE_ON")
        elif m.domain == "ocean":
            render.prop(m, "domain")
            render.label(text="Uses native water textures and animation.")
            render.label(text="Blender surface textures are not used.")
            col.label(text="Set collision on the object; this is a visual shader.", icon="INFO")
        else:
            render.prop(m, "domain")
            render.prop(m, "srgb")
            if m.domain == "surface":
                render.prop(m, "alpha")
            elif m.domain == "glass_opaque":
                render.label(text="Native reflective window glass (opaque).")
            else:
                render.label(text="Alpha: Cutout" if m.domain == "foliage" else "Alpha: Blend")
            if m.domain != "glass_opaque":
                render.prop(m, "alpha_source")
            if m.domain == "foliage" or (m.domain == "surface" and m.alpha in {"auto", "mask"}):
                render.prop(m, "alpha_cutoff")
            render.prop(m, "cast_shadows")
            render.prop(m, "double_sided")
            if m.domain != "glass_opaque":
                render.prop(m, "transparent_shadow")
            render.label(text="Native textures (empty = Blender shader)")
            render.prop(m, "base_color_image")
            render.prop(m, "normal_image")
            render.prop(m, "mask_image")
            scalar = render.column()
            scalar.enabled = m.mask_image is None
            for role in ("roughness", "metallic"):
                scalar.prop(m, role + "_image")
                if getattr(m, role + "_image"):
                    scalar.prop(m, role + "_channel")
            render.prop(m, "opacity_image")
            col.label(text="Gameplay collision is authored on each object.", icon="INFO")
            img = _base_color_image(ctx.material)
            col.separator()
            col.label(text=f"Texture: {img.name if img else 'none'}",
                      icon="TEXTURE" if img else "ERROR")
        surface = col.box()
        surface.label(text="Surface / Contact Audio", icon="SPEAKER")
        surface.prop(m, "collision_material", text="Surface")
        surface.operator("sk8.choose_collision_surface", text="Search Donors", icon="VIEWZOOM")
        _draw_surface_audio_settings(surface, m)
        surface.label(text="Used by faces assigned to this material.")


CLASSES = (SK8_OT_add_bus_stop, SK8_OT_refresh_bus_stops, SK8_PT_bus_stop, Sk8MapSettings, Sk8ObjectSettings, Sk8MaterialSettings, Sk8LightSettings,
           Sk8NpcRouteSettings, SK8_OT_add_npc_route,
           Sk8GrindCurveSettings, SK8_OT_add_grind_curve, SK8_OT_auto_grind_curves,
           SK8_OT_grind_curve_from_edges,
           SK8_MT_npc_pedestrian_route, SK8_MT_npc_vehicle_route, SK8_MT_npc_bus_route,
           SK8_MT_add_npc_route,
           Sk8AudioSettings, SK8_OT_add_audio_volume,
           Sk8AudioEmitterTag, Sk8AudioEmitterSettings, SK8_OT_audio_emitter_tag,
           SK8_OT_add_audio_emitter, SK8_OT_add_speaker_interaction,
           SK8_OT_add_native_behavior, SK8_PG_vfx_choice, SK8_UL_vfx_choices,
           SK8_OT_find_native_vfx, SK8_OT_import_vfx_catalog,
           SK8_MT_native_trigger_effects, SK8_MT_native_vfx, SK8_PT_native_behavior,
           SK8_OT_add_spawn, SK8_OT_export_map, SK8_OT_apply_collision_selected,
           SK8_OT_choose_collision_surface,
           SK8_OT_open_folder, SK8_OT_merge_by_material,
           SK8_PT_object, SK8_PT_material,
           SK8_PT_sidebar_map, SK8_PT_sidebar_npc_route, SK8_PT_sidebar_grind_curve,
           SK8_PT_sidebar_audio_volume, SK8_PT_sidebar_collision_authoring,
           SK8_PT_sidebar_light, SK8_PT_sidebar_audio_emitter)


@bpy.app.handlers.persistent
def _migrate_shader_overrides(_scene=None):
    for material in bpy.data.materials:
        if not material.is_editable:
            continue
        for settings in (material.get("sk8_material"), material.sk8_material):
            if settings is None:
                continue
            if settings.get("domain") in (3, "water"):
                settings["domain"] = 4
                material.sk8_material.domain = "ocean"
            elif settings.get("domain") in (5, "glass"):
                settings["domain"] = 6
                material.sk8_material.domain = "glass_opaque"


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.sk8_map = PointerProperty(type=Sk8MapSettings)
    bpy.types.Object.sk8_object = PointerProperty(type=Sk8ObjectSettings)
    bpy.types.Material.sk8_material = PointerProperty(type=Sk8MaterialSettings)
    bpy.types.Object.sk8_light = PointerProperty(type=Sk8LightSettings)
    bpy.types.Object.sk8_audio = PointerProperty(type=Sk8AudioSettings)
    bpy.types.Object.sk8_audio_emitter = PointerProperty(type=Sk8AudioEmitterSettings)
    bpy.types.Object.sk8_npc_route = PointerProperty(type=Sk8NpcRouteSettings)
    bpy.types.Object.sk8_grind_curve = PointerProperty(type=Sk8GrindCurveSettings)
    bpy.app.handlers.load_post.append(_migrate_audio_tags)
    bpy.app.timers.register(_migrate_audio_tags, first_interval=0.0)
    bpy.app.handlers.load_post.append(_migrate_shader_overrides)
    bpy.app.timers.register(_migrate_shader_overrides, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(_migrate_audio_tags):
        bpy.app.timers.unregister(_migrate_audio_tags)
    if _migrate_audio_tags in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_audio_tags)
    if bpy.app.timers.is_registered(_migrate_shader_overrides):
        bpy.app.timers.unregister(_migrate_shader_overrides)
    if _migrate_shader_overrides in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_shader_overrides)
    del bpy.types.Object.sk8_light
    del bpy.types.Object.sk8_audio
    del bpy.types.Object.sk8_audio_emitter
    del bpy.types.Object.sk8_npc_route
    del bpy.types.Object.sk8_grind_curve
    del bpy.types.Material.sk8_material
    del bpy.types.Object.sk8_object
    del bpy.types.Scene.sk8_map
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
