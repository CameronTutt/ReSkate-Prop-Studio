# Deprecated & Deferred Feature Archive: Procedural Wear, Projected Decals, Built-in Baker & Pack Conflict Checker

> **Context (v0.31.0 Performance & Streamlining Update)**:
> To maximize viewport responsiveness, object-switching speed, and compile-time predictability, **Procedural Wear**, **Projected Decals**, **2D Procedural Node Synthesis**, and the **Built-in Cycles Texture Baker** were disconnected from the core `sk8_prop_authoring_addon` workflow on Page 3 (`3. Shaders, Sound & Look`).
>
> Prop Studio now focuses 100% on cleanly exporting **manually authored, game-ready Principled BSDF materials** (with direct image textures and scalar PBR sliders) into Frostbite's 2-texture PBR format (`_b.png` BC7 sRGB Base Color + `_msk.png` BC7 Linear packed Normal X/Y, Metallic, Roughness/Smoothness).
>
> This document archives the architecture, removed UI/pipeline snippets, and reimplementation instructions for developers or AI agents who wish to restore these features or maintain them inside the standalone `sk8_prop_experimental_addon` (`ReSkate Lab`). It also documents the deferred **Mod Pack Conflict / Interference Checker** specification.

---

## 1. Why These Features Were Removed from Core Prop Studio

1. **Main-Thread & Compile-Time Overhead**:
   - **Procedural Wear** required building a BMesh, computing dihedral crease angles across all edges, rasterizing UV triangle scanlines on a $512 \times 512$ grid, and running `scipy.ndimage.gaussian_filter` + multi-octave fBM noise (`compute_uv_geometry_wear_masks`), plus injecting a 12-node `ShaderNodeTexPointDensity` / `ShaderNodeAmbientOcclusion` preview graph into every material slot (`sync_procedural_wear_to_viewport`).
   - **Projected Decals** required either live shader wrapper nodes (`ReSkate_Decal_*`) or compile-time alpha-over compositing + nearest-neighbor RGB dilation (`_composite_joined_decal_slots_over_opaque`) to prevent black edge halos at DXT/BC7 mipmap levels.
   - **Built-in Cycles Texture Baker** (`RESKATE_OT_bake_procedural_materials`) switched Blender's render engine to `CYCLES` on the main thread, promoted inner `NodeGroup` sockets, and ran 4 sequential `bpy.ops.object.bake` passes (`EMIT`, `ROUGHNESS`, `NORMAL`) per material slot, freezing the Blender UI during baking.
   - **2D Procedural Node Graph Synthesis** (`_evaluate_color_socket_2d_generator`, `_evaluate_scalar_socket_2d_generator`, `_evaluate_normal_socket_2d_generator`) recursively evaluated Blender shader nodes (`TEX_NOISE`, `TEX_VORONOI`, `VALTORGB`, `BUMP`, `MIX`) in Python/NumPy on the main thread during export.

2. **Authoring Clarity**:
   - Relying on standard, manually authored Principled BSDF materials with pre-baked image textures (from Substance Painter, ArmorPaint, Quixel, AmbientCG, or manual Cycles bakes) guarantees 1:1 visual parity between the Blender viewport and the Frostbite engine with zero surprise shader mutations or compile stalls.

---

## 2. How Manually Authored Principled BSDF Materials Are Handled (Preserved 100%)

The core Principled BSDF extraction pipeline in `compiler_bridge.py` (`inspect_material_shader_nodes`, `sync_material_from_shader_nodes`, `_extract_slot_inputs_mainthread_generator`, and `_process_slot_textures_offthread`) remains 100% intact:
- **Base Color (`_b.png`)**: Extracts the `ShaderNodeTexImage` wired into `Principled BSDF -> Base Color` (or uses the flat `Base Color` RGBA slider if unlinked), applies optional `albedo_tint`, and forces alpha to `255` (`1.0`) for opaque shaders (`0x9c809495` / `0x8c7fb470`) or preserves Cutout Alpha for transparent shaders (`0x273`).
- **Packed PBR Mask (`_msk.png`)**:
  - **Red (`R`) & Green (`G`)**: DirectX Tangent-Space Normal X and Y (`G` is flipped `255 - G` when `normal_green_convention == 'OPENGL'` so Blender OpenGL normals convert automatically to Frostbite DirectX normals).
  - **Blue (`B`)**: Metallic channel (`0..255`), sampled from the `Metallic` image texture (including `Separate Color` / `Separate RGB` channel routing for ORM maps) or `Principled BSDF -> Metallic` scalar slider.
  - **Alpha (`A`)**: Roughness/Smoothness channel (`0..255`), sampled from the `Roughness` image texture (supporting `ReSkate_Roughness_Invert` for Glossiness maps and `Separate Color` G/B channels) or `Principled BSDF -> Roughness` scalar slider.

---

## 3. Archived Feature #1: Procedural Edge Wear & Grind Scuffs

### How It Worked
1. **Viewport Preview (`sync_procedural_wear_to_viewport` in `compiler_bridge.py`)**:
   - Injected a non-destructive node chain labeled `ReSkate_Wear_*` immediately upstream of `Principled BSDF` inputs (`Base Color`, `Roughness`, `Metallic`, `Normal`).
   - Combined `ShaderNodeNewGeometry` (`Pointiness`), `ShaderNodeAmbientOcclusion`, and `ShaderNodeTexNoise` through `ShaderNodeMapRange` and `ShaderNodeMix` (`MIX_RGB`) to preview edge chipping, dirt/grime in cavities, and bright metallic grind scuffs in real time.
2. **Compile-Time UV Mask Synthesis (`compute_uv_geometry_wear_masks` in `compiler_bridge.py`)**:
   - Extracted a welded BMesh in world space (`obj.matrix_world`), computed convex dihedral angles `(1.0 - n1.dot(n2))` for edge wear, upward/downward cavity weights for grime, and proximity to tagged grind edges (`reskate_grind` layer or `reskate_grind_paths`) for grindwax scuffs.
   - Rasterized vertex weights onto a $512 \times 512$ UV grid using barycentric triangle scanline interpolation, blurred with `scipy.ndimage.gaussian_filter`, multiplied by multi-octave fBM noise (`_fbm_noise_2d`), and upsampled to the target atlas resolution (`1024`–`4096`).
3. **Off-Thread PBR Map Application (`apply_procedural_wear_to_pbr_maps` in `compiler_bridge.py`)**:
   - Blended material-specific wear colors (e.g., chipped concrete, raw plywood, bare steel on `METAL_RAIL` or `PAINTED` basis) into `bc_arr`, adjusted roughness/metallic arrays (`r_arr`, `m_arr`), and perturbed the tangent normal map (`n_arr`) via Sobel derivatives of the wear height field.

### Archived UI Snippet (from `ui_panels.py` Page 3)
```python
wear_box = box.box()
w_hdr = wear_box.row(align=True)
w_hdr.prop(prop, "use_procedural_wear", text="Procedural Edge Wear & Grind Scuffs", icon='MOD_NOISE')
if getattr(prop, "use_procedural_wear", False):
    w_col = wear_box.column(align=True)
    w_col.prop(prop, "wear_basis", text="Material Basis")
    w_col.prop(prop, "wear_edge_amount", text="Edge Chipping", slider=True)
    w_col.prop(prop, "wear_grind_scuff", text="Grind Wax / Scuffs", slider=True)
    w_col.prop(prop, "wear_dirt_cavity", text="Cavity Dirt / Grime", slider=True)
```

### How to Reimplement / Re-enable
- Enable the companion `sk8_prop_experimental_addon` (`ReSkate Lab`), which sets `_is_experimental_addon_enabled() == True` in `compiler_bridge.py`.
- Or call `compiler_bridge.compute_uv_geometry_wear_masks(obj, width, height, ...)` on the main thread and pass the resulting `precomputed_wear` dict into `compiler_bridge._apply_precomputed_wear_offthread(...)`.

---

## 4. Archived Feature #2: Projected Decals & Transparent Sticker Compositing

### How It Worked
1. **Shader-Level Projected Decals (`remove_projected_decal_nodes_from_material`)**:
   - Inserted `ReSkate_Decal_*` UV mapping and alpha-mix nodes (`ReSkate_Decal_ColorMix`, `ReSkate_Decal_RoughMix`, `ReSkate_Decal_MetalMix`, `ReSkate_Decal_NormMix`) in front of `Principled BSDF`.
2. **Joined Decal Slot Compositing (`_composite_joined_decal_slots_over_opaque` in `compiler_bridge.py`)**:
   - Detected material slots marked as decal overlays (`is_decal_slot`, `is_decal_overlay`, or `surface_role_preset == 'DECAL_OVERLAY'`) or floating coplanar sticker geometry (`z_offset <= 15mm`).
   - Either exported them as a dedicated Cutout Alpha MeshSet sub-mesh (`0x273` `ShaderPreset_CutoutAlpha`) with dilated RGB borders (`_dilate_cutout_rgb_borders`) so mipmaps never bled dark fringes, or alpha-composited their UV islands directly onto the underlying opaque slot's Base Color, Roughness, Metallic, and Normal arrays.

### How to Reimplement / Re-enable
- For standard props without `sk8_prop_experimental_addon`, authors should either:
  1. Bake/flatten stickers directly into their prop's Base Color / Roughness / Normal texture maps in their 2D/3D painting tool (recommended for 1-draw-call performance), or
  2. Use a standard material slot with an Alpha channel wired into `Principled BSDF -> Alpha` (which Prop Studio natively exports using the `0x273` Cutout Alpha shader).
- Enabling `sk8_prop_experimental_addon` re-activates automatic `_composite_joined_decal_slots_over_opaque` during export.

---

## 5. Archived Feature #3: Built-in Cycles Procedural Material Baker

### How It Worked (`RESKATE_OT_bake_procedural_materials` in `operators.py`)
- Provided a 1-click `"Bake Procedural / Node Materials (Cycles)"` button on Page 3 (`reskate.bake_procedural_materials`).
- Temporarily switched `scene.render.engine = 'CYCLES'` (`samples = 1`, `margin = 16`, `margin_type = 'EXTEND'`).
- Inspected each material slot's `Principled BSDF` (or top-level `ShaderNodeGroup`, such as Sanctus Library materials, promoting internal `Base Color`, `Roughness`, and `Metallic` sockets to temporary Group Outputs).
- Routed each channel through a temporary `ShaderNodeEmission` (`_ReSkate_Temp_Bake_Emit`) to bake `BaseColor`, `Roughness`, and `Metallic` via `bpy.ops.object.bake(type='EMIT')` (avoiding metallic diffuse darkening) and `Normal` via `bpy.ops.object.bake(type='NORMAL', normal_space='TANGENT')`.
- Forced 100% opaque alpha (`px[3::4] = 1.0`) across all pixels so UV gutters never triggered accidental `0x273` alpha cutout, saved the PNGs to Staging, and populated `mat.reskate_mat` texture override slots.

### Archived UI Snippet (from `ui_panels.py` Page 3)
```python
bake_row = box.row(align=True)
bake_row.scale_y = 1.15
op_b = bake_row.operator(
    "reskate.bake_procedural_materials",
    text="Bake Procedural / Node Materials (Cycles)",
    icon='RENDER_STILL',
)
op_b.active_slot_only = False
```

### How to Reimplement / Re-enable
- `RESKATE_OT_bake_procedural_materials` remains registered in `operators.py` for backward compatibility and experimental use (accessible via `F3` search when `sk8_prop_experimental_addon` is enabled, or callable from scripts via `bpy.ops.reskate.bake_procedural_materials()`).

---

## 6. Deferred Future Feature: Mod Pack Conflict / Interference Checker

> **Status**: Deferred in v0.31.0 to keep UI redraws and compilation 100% focused on speed and zero disk I/O overhead.

### Problem Statement
When a user has multiple `.fbmod` prop packs deployed in `<GameDir>/Mods/` (or multiple pack registries in `<StagingDir>/Packs/*/pack_registry.json`), two packs can conflict if:
1. **Duplicate `prop_id` / `prop_key_slug` (`own_customprops_<slug>`)**: Both packs register the same deterministic FNV-1a GUID in `Items/ItemMasterCollection` or the same bundle path `win32/items/customprops/<slug>_bundle` in `Win32/buildkititems.toc`.
2. **Duplicate EBX Partition GUIDs or ResRid collisions**: Two packs generated from cloned objects without resetting their stable prop key (`reskate_stable_prop_key`) overwrite each other's mesh/physics resources in Frosty Mod Manager.

### Recommended Future Implementation Design (Zero UI-Draw Overhead)
When implementing the **Mod Pack Conflict / Interference Checker** in a future release:
1. **Never Run During UI `draw()`**:
   - Do **not** scan `<GameDir>/Mods/*.fbmod` or `<StagingDir>/Packs/*/pack_registry.json` inside `ui_panels.py` or `sync_active_object_props`.
2. **On-Demand Pre-Flight Check Integration (`RESKATE_OT_run_preflight_check`)**:
   - Run the scan **only** when the user explicitly clicks **"Run Pre-Flight Check"** on Page 6 (or as a lightweight pre-compile step).
   - Scan `<StagingDir>/Packs/*/pack_registry.json` (excluding the current `target_mod_name`) and build a lookup table `dict[prop_key_slug, list[pack_name]]`.
   - Optionally parse the binary FBMOD header/resource table of `.fbmod` files in `<GameDir>/Mods/` to detect `win32/items/customprops/<slug>_bundle` collisions with third-party downloaded mods.
3. **Resolution UX**:
   - If a collision is found with another pack, report an advisory `WARN` item in the Page 6 Pre-Flight Checklist:
     `"Prop ID 'own_customprops_ledge_01' is also used in pack 'Crem_PlazaPack' — click 'Reset Internal Key' if this should be a separate prop, or switch Target Pack to 'Crem_PlazaPack' to update it."`
