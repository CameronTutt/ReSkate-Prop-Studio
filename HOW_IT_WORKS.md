# How It's Made: ReSkate Zero-Donor Standalone Prop Architecture

This document explains **how custom standalone Quick Drop props work in *Skate.* (Frostbite)** and how **ReSkate Prop Studio** (`sk8_prop_authoring_addon`) + `compile_blender_prop.py` build them from scratch without replacing any base-game items.

Everything here is open source so you can inspect, modify, or build your own tools on top of these findings.

---

## 1. High-Level Pipeline Overview

```
[ Blender 3D Mesh + Principled BSDF Materials + Optional _COL & Lights ]
                                  │
                                  ▼  (compiler_bridge.py)
  Staging Folder:
  • <prop>.glb (Visual Mesh, UVs, Material Slots)
  • <prop>_col.glb (Physical Collision Mesh + Per-Face Surface Codes)
  • <prop>_albedo.png / _normal.png / _roughness.png / _metallic.png
  • <prop>_thumb_256.png & <prop>_thumb_768.png
  • <prop>_manifest.json (Surface codes, lights, decal flags, metadata)
                                  │
                                  ▼  (compile_blender_prop.py + reskate_cli.exe)
  Deployed Mod (Skate/Mods/<PackName>/):
  • Standalone Bundle in Win32/buildkititems.toc (flag 0x08 additive)
  • EcsRuntimePrefabAsset + MeshSet (0x49B156D4) + ShaderBlockDepot
  • Havok Physics Data (0x41759364) + 101-Entry ShapeToFaceMaterialMap
  • ItemMasterCollection + DelMarBaseItemAsset in Win32/items.toc
```

---

## 2. True Additive Bundles Over Donor Hijacking (`buildkititems.toc`)

Early prop mods worked by overwriting an existing base-game prop's `MeshSet` and `HavokPhysicsData` ("donor hijacking"), which meant you could only replace existing items and two mods replacing the same donor would conflict.

**ReSkate Prop Studio uses a Zero-Donor Additive Architecture:**
1. Every custom prop is packaged into its own brand-new bundle path:
   - **Bundle Path:** `win32/items/customprops/<prop_name>_bundle`
   - **TOC Target:** `Win32/buildkititems.toc`
   - **Bundle Tag:** `reskate-cosmetic-addition:v1`
   - **TOC Flags:** `0x08` (`is_added: True`)
2. When `reskate_cli compile-mod` builds the `.fbmod`, it **appends** the new bundle record to `buildkititems.toc` and `Win32/globals.toc` without mutating any vanilla bundle streams.
3. All internal EBX Partition GUIDs, Instance GUIDs, Resource IDs, and Chunk GUIDs are generated deterministically via `SHA-256` from `"<author>/<pack>/<prop_id>"`, so two authors' prop packs never collide.

---

## 3. Standalone ECS Prefab (`EcsRuntimePrefabAsset`)

Each prop bundle contains a dedicated `EcsRuntimePrefabAsset` EBX partition that wires together:
1. **Root Transform Entity**: Defines the prop's origin (which is why setting your mesh's origin to **Bottom Center** makes the prop sit flush on the ground when placed in Quick Drop).
2. **`MeshComponent`** -> Points to the prop's standalone **`MeshSet` (`0x49B156D4`)** and **`ShaderBlockDepot` (`_sbd`)**.
3. **`CollisionEntity` (Skater Physics)** -> Points to the prop's standalone **Havok Physics Asset (`0x41759364`)** containing `hknpCompressedMeshShapeData`.
4. **Placement Proxy (`_pphys`)** -> A single-box `hknpConvexPolytopeShape` fitted to the prop's bounding box used by the Quick Drop placement gizmo to position and settle the prop.
5. **Optional `EffectBlueprint` Entity** -> If the prop has linked Blender `SPOT` or `POINT` lights, instantiates a child entity referencing a custom `EffectBlueprint` with `AutoStart = True`.

---

## 4. Shaders & Multi-Material Splitting (`0x771` Opaque PBR vs. `0x273` Alpha Cutout)

Frostbite binds textures and material constants through `ShaderBlockDepot` (`RES` type `0x33768C09`) paired with root hash lookups in `Win32/globals.toc`.

### A. `0x771` (`1,905`-Byte) Category 0 Opaque PBR Shader
Used for standard solid skatepark materials (concrete, wood, metal, painted surfaces):
- **Albedo (`_c`)**: BC7 sRGB mipchain (`1024×1024` up to `4096×4096`).
- **Normal (`_ny`)**: BC7 Linear two-channel tangent-space normal map (`R = X`, `G = Y`).
- **Packed Mask (`_msk`)**: BC7 Linear packed map:
  - `R` = Ambient Occlusion (`AO`)
  - `G` = **Smoothness** (`1.0 - Roughness`)
  - `B` = **Metallic**
- **Optional `2×2m` World-Tiled Micro-Detail**: Binds vanilla 2×2m concrete/wood/metal grain normal maps into the secondary detail slots of the `0x771` block.

### B. `0x273` (`627`-Byte) Category 0 Alpha Cutout & Decal Shader
Extracted from vanilla `Bundle #58` (`FightFence`) and `Bundle #421` (`HockeyNet`) for chainlink fences, nets, grates, and sticker/graffiti decals:
- **Byte `+0x015..+0x035`**: `_c` Albedo (`16-byte Instance GUID + 16-byte Partition GUID`). Transparent pixels have their RGB dilated outward from opaque neighbors so bilinear mipmaps never bleed dark halos.
- **Byte `+0x046..+0x066`**: `_ny` Normal (`16-byte Instance GUID + 16-byte Partition GUID`).
- **Byte `+0x077..+0x097`**: `_osk` Opacity/Smoothness/Mask (`R = Opacity`, `G = Smoothness`, `B = 0`, `A = Opacity`).
- **Byte `+0x0DF`, `+0x147`, `+0x17F`**: `float3` Albedo Tint (`0.5 * r, 0.5 * g, 0.5 * b`).
- **Byte `+0x117`**: `uint32 = 1` (Double-sided rendering enabled).
- **`globals.toc` Registration**: Registered with `RES[1130] = 0xE863DEE0C1440B29` (`TextureLookup`) and `RES[2002] = (32, 73, 333, 1033)` (`ProgramLookup`).

### C. Multi-Section Opaque + Decal/Cutout Split (`Bundle #421` `HockeyNet` Template)
When a prop has both solid materials and a material slot marked **`🩹 Treat Material as Decal Overlay / Cutout (0x273)`**:
1. `compile_blender_prop.py` splits the mesh geometry into two draw sections inside a `4,192`-byte `Bundle #421` `MeshSet`:
   - `Sec[0]`: All opaque triangles -> bound to `Dir[1]` (`0x771` Opaque PBR).
   - `Sec[1]`: All decal/cutout triangles -> bound to `Dir[0]` (`0x273` Alpha Cutout).
2. Both shader blocks are packed into a dual-entry `2,740`-byte (`0xAB4`) `ShaderBlockDepot`.
3. Triangles belonging to a Decal Overlay slot (`is_decal_slot = True`) are automatically excluded from skater collision so floating sticker quads never create physical bumps.

---

## 5. Havok Collision, Grind Detection & Surface Audio (`0x41759364`)

### A. Why Grind Edges Come Directly from Collision Geometry
In *Skate.*, there are **no separate grind spline resources** on static Quick Drop props. Instead, the engine analyzes your Havok collision mesh (`hknpCompressedMeshShapeData`) at load/placement time:
- Any convex edge whose adjacent triangle normals form a sharp dihedral crease (roughly **`35°` to `90°`**) and whose surface material has **`PropertySlot 71` (`IncludeInSurfaceAnalysis = True`)** becomes a grindable ledge, rail, or coping edge.
- **Havok 1:1 Ceiling (`2,600` Welded Verts / `5,000` Tris)**: A single `hknpCompressedMeshShapeData` tree holds up to ~`2,600` welded vertices / `5,000` triangles. Keeping your collision mesh (or custom `_COL` mesh) under this ceiling guarantees 1:1 physical grind edges without compile-time decimation.

### B. Surface Physics & Per-Face Audio (`ShapeToFaceMaterialMap`)
The prop's physics EBX contains a **101-entry `PhysicsMaterial` table** paired with a per-triangle `ShapeToFaceMaterialMap` array:
- Every triangle in the collision mesh indexes into the material table to determine:
  1. **Rolling & Landing Audio** (Smooth Concrete, Rough Street Concrete, Plywood Ramp, Hollow Metal, Grass, etc.).
  2. **Grind Friction & Slide Audio** (Metal Zing, Concrete Crunch, Wood Slide).
  3. **Surface Alignment (`PropertySlot 71`)**:
     - Standard skateable codes (e.g. `Concrete_Smooth = 96`) enable `PropertySlot 71`, allowing trucks to lock onto ledges and coping.
     - **`Do Not Align` / `Prevent Wall-Snap`** variants disable `PropertySlot 71` on vertical walls so the skater doesn't involuntarily rotate sideways when jumping near a wall.

### C. Flush Ramp Bottom (Anti-Lip `-15mm`)
On transition ramps and quarterpipes, even a `2mm` vertical bottom edge can snag skateboard wheels when rolling onto the ramp from flat ground.
- Enabling **Flush Ramp Bottom (Anti-Lip)** keeps the Quick Drop placement proxy (`_pphys`) flush at `Z = 0.000m` while sinking the bottom ring of vertices in the skater collision mesh (`hknpCompressedMeshShapeData`) down to `Z = -0.015m` (`-15mm`), creating a seamless roll-on transition below the floor plane.

---

## 6. Quick Drop Wheel Registration (`Win32/items.toc`)

To make a standalone prop show up in the in-game Quick Drop menu without replacing a donor item:
1. A new `DelMarBaseItemAsset` EBX is generated for the prop, containing:
   - The Quick Drop category folder tag (`bkcustomprops`, `bkrails`, `bkledges`, `bkramps`, etc.).
   - References to the prop's `256×256` wheel icon and `768×768` inspect card icon (`TextureAsset` + BC7 chunk).
   - Reference to the prop's `EcsRuntimePrefabAsset`.
2. In `Win32/items.toc`, the `DelMarBaseItemAsset` is tagged with `SHARED_BUNDLE_HASH = 0x04308B5D` (`cas_main_sharedbundle`).
3. The prop's `(PartitionGUID, InstanceGUID)` pair is appended to the import table of `Items/ItemMasterCollection`, and an 8-byte external pointer (`((vanilla_count + i) * 2) + 1`) is appended to the collection array.

---

## 7. In-Game Dynamic Lights (Noble `EffectBlueprint` Pattern)

When you link up to 4 Blender `SPOT` or `POINT` lights in **`💡 4. Light (in-game)`**:
1. `compiler_bridge.py` extracts each light's local position, forward/up transform matrix, RGB color, Wattage intensity, attenuation radius, inner/outer spot cone angles, and shadow flag relative to the prop's origin.
2. `compile_blender_prop.py` clones the vanilla Noble Worklight `EffectBlueprint` template and patches its `PbrSpotLightEntityData` and `PbrSphereLightEntityData` nodes with your exact Blender light transforms and radiometric values.
3. The `EcsRuntimePrefabAsset` spawns the `EffectBlueprint` automatically whenever the prop is placed in the world.

---

## 8. Contributing & Modifying

All source files inside `sk8_prop_authoring_addon/` and `compile_blender_prop.py` are plain Python with zero obfuscation or compiled `.pyc`-only locks:
- Edit any `.py` file in `sk8_prop_authoring_addon/` and press `F3` -> `Reload Scripts` in Blender to test UI/operator changes immediately.
- Run `python compile_blender_prop.py --help` in the `ReSkate Studio` folder to run the standalone compiler directly from the command line.
