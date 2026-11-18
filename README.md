
# 🛹 ReSkate QuickDrop Studio (`v1.0.0`)

<img width="2350" height="1615" alt="quickdropstudio" src="https://github.com/user-attachments/assets/66eb13e8-ae1b-4d81-a43b-99ab6e08bc8d" />



**Turn any 3D mesh in Blender into standalone, skateable Quick Drop Objects (`QDOs`) and custom Quick Drop obstacles for *ReSkate.* in just a few steps.**

**ReSkate QuickDrop Studio** is an open-source Blender add-on (**Blender 4.2 – 5.3+**) that compiles 3D models—from **street ledges, handrails, flatbars, hubbas, stairs, and manny pads** to **kickers, quarterpipes, DIY skate spots, and custom skateparks**—directly into standalone **ReSkate** QDO packs. Every object exports with **1:1 Havok skate & grind physics (up to 100,000 triangles)**, **authentic surface audio** (concrete, wood, metal, brick, grass), **1K–16K PBR & cutout texture atlases**, **dynamic in-game Spot/Point lights**, **Quick Drop variant stacking**, and **1-click Thunderstore `.zip` packaging**.

> **Zero Donor Hijacking:** Every Quick Drop Object (`QDO`) you compile is 100% standalone and additive. Your custom Quick Drop obstacles load into their own custom Quick Drop tabs and never overwrite base-game props.

---


## 🛠️ Step-by-Step Installation & First-Time Setup


You do **not** need any command-line tools or external scripts—everything runs right inside Blender. Click each step below to expand it:

<details open>
<summary><b>1. What You Need Installed</b></summary>
<br>

- **Blender 4.2 – 5.3+** (**Blender 5.2 / 5.3 LTS** recommended)
- **ReSkate** installed in your *Skate.* game folder
- **`reskate_quickdrop_studio_addon.zip`** (from the Releases page)
-  **ReSkate Studio** Which can be found in the ReSkate Discord server.
-  Reskate Studio folder should be in your Skate parent folder (where the skate.exe / reskatelauncher.exe is located)

</details>

<details>
<summary><b>2. Install the Add-on in Blender</b></summary>
<br>

1. Open Blender and go to **`Edit` → `Preferences...`** in the top menu bar.
2. Click the **`Add-ons`** tab on the left.
3. Click the **drop-down arrow (`⌄`)** in the top-right corner of the Add-ons window and choose **`Install from Disk...`** *(or click `Install...` in Blender 4.2)*.
4. Select **`reskate_quickdrop_studio_addon.zip`** and click **`Install from Disk`**.
5. Make sure the checkbox next to **`3D View: ReSkate QuickDrop Studio`** is **checked (`☑️`)**.

(You're also able to drag and drop the `.zip` file from the releases directly into the viewport of Blender to install your Quick Drop Object (`QDO`) toolchain!)

</details>

<details>
<summary><b>3. Configure Your Preferences (One-Time Setup)</b></summary>
<br>

While you are still inside **`Edit` → `Preferences` → `Add-ons`**, click the arrow next to **`ReSkate QuickDrop Studio`** to expand its settings:

| Setting | What to Enter |
| :--- | :--- |
| **Skate. Game Folder (`Skate.exe`)** | Click the folder icon and select your *Skate.* installation folder (the folder that contains `Skate.exe`). |
| **Staging & Cache Folder** | Leave on the default (`.../ReSkate Studio/Native/Research/Temp_DirectExport`) or pick any local working folder with ~2 GB of free space. |
| **Creator / Author Name** | Type your creator handle (e.g., `YourName`). This is automatically used to name your Quick Drop Object (QDO) Packs (`YourName-PackName`). |
| **Origin Ground Offset (m)** | Default **`0.05 m`** (`5 cm`). Raises the placement origin slightly above the bottom of the mesh when clicking `Origin to Ground Contact` so custom Quick Drop obstacles don't jitter against the ground in Quick Drop. |
| **Variant Corner Notch Color** | Default **Green**. Sets the color of the bottom-left corner badge drawn on thumbnails when QDOs are grouped into a Variant Family. |
| **Auto-Render Missing Thumbnails on Compile** | **Off by default** for instant `0 ms` compile times (uses a shared static `"No Thumbnail"` icon until you click `Render Thumbnail` on Page 5). Enable this if you want Blender to launch EEVEE automatically during compile whenever a QDO has no thumbnail. |
| **Enable Viewport Overlays** | Master switch for 3D viewport overlays. Zero GPU draw handlers are attached when all overlays in the panel are turned off. |

</details>

<details>
<summary><b>4. Open the ReSkate QuickDrop Sidebar in the 3D Viewport</b></summary>
<br>

1. Close the Preferences window and click anywhere in Blender's **3D Viewport**.
2. Press **`N`** on your keyboard to open the right-hand Sidebar.
3. Click the **`ReSkate QuickDrop`** tab on the vertical tab strip.

<img width="1964" height="1087" alt="image" src="https://github.com/user-attachments/assets/79b35a3c-370b-46fd-ac00-4e739f253903" />

</details>

---

## 🚀 Beginner Guide: The 6-Page Quick Drop Object (`QDO`) Workflow

At the top of the **ReSkate QuickDrop** panel, you will always see:
- **Prop / QDO Carousel**: Shows thumbnails of your selected Quick Drop Objects (`QDOs`)—such as street ledges, handrails, flatbars, kickers, or quarterpipes (or `"No Prop Selected"` when nothing is selected). Objects without a rendered thumbnail yet display a fast shared `"No Thumbnail"` icon.
  - 🟩 **Green Bottom-Left Corner Notch**: Means the QDO is part of a **Variant Family** (customizable in Preferences).
  - **`Frame Selected` (Left Button)**: Centers your 3D viewport camera on the currently selected QDO(s).
  - **`Select All in Pack (N)` (Right Button)**: Selects every QDO in your `.blend` scene that belongs to the active Pack in memory.


- **`📏 Scale References` Twirl-Down**: Click **`Spawn Skater`**, **`Spawn Skateboard`**, or **`Spawn Ground Plane`** anytime to check your obstacle's real-world proportions (`1 Blender meter = 1 Game meter`) for DIY skate spots and custom skateparks. Scale references spawn right at your **3D Cursor** with their feet/wheels flush on the cursor's Z plane, and you can also use **`Set Prop Flush to Ground (Z = 0)`** on them directly!

<img width="1615" height="603" alt="Group 12" src="https://github.com/user-attachments/assets/a3409b09-5760-4f7f-98a4-aa1440ca7957" />


---
---
---

Click each workflow page below to open or close its step-by-step instructions:

<details open>
<summary><b>Page 1 — Scale & Origin</b></summary>
<br>

Your Quick Drop Object's **Origin Point** (the little orange dot in Blender) determines where the Quick Drop cursor grabs your street ledge, flatbar, manny pad, or quarterpipe and how it sits on the ground.

1. Select your 3D mesh QDO in the viewport and check the **Dimensions** box (`Width × Depth × Height` in meters).
2. Click **`Origin to Ground Contact (Recommended)`**.
   - This centers the origin horizontally on the bottom footprint of your QDO and raises the origin point **`+0.05 m`** (`5 cm`) above the lowest vertices so the obstacle places smoothly on the ground without jittering or clipping.
3. Click **`Set Prop Flush to Ground (Z = 0)`** to rest the bottom of the mesh (or any selected Skater/Skateboard reference) cleanly on the `Z = 0` ground plane.
4. *(Optional)* Need a custom pivot point? Click **`Move Origin Point`**, drag the Move gizmo arrows in the viewport, and click **`Done Moving Origin`**.

<img width="2006" height="749" alt="Group 7" src="https://github.com/user-attachments/assets/bfebaa01-39c8-4e20-a06e-bf8556c1e516" />

</details>

<details>
<summary><b>Page 2 — Collision</b></summary>
<br>

This page controls how the skateboard, wheels, trucks, and player body collide with and grind your custom Quick Drop obstacle.

1. Leave **Collision Mode** set to **`Same as visible mesh (Recommended)`**.
   - You will see **`✅ 1:1 Visible Mesh Collision Active`**. ReSkate QuickDrop Studio supports **1:1 Havok skateable collision up to `100,000` triangles** (`< 25,000` tris and `< 1,000 tris/m²` recommended for optimal skate prediction performance; visual meshes must also stay at or below `65,535` vertices). Every street ledge, handrail, flatbar, hubba, stair set, manny pad, kicker, coping, and quarterpipe transition on your visible mesh compiles 1:1 into Havok physics, and sharp top edges are automatically detected as grindable.
2. **When to use the other modes:**
   - **`Simple Hulls (For high-poly props)`**: Use this if your QDO exceeds **`100,000` triangles** (or has dense geometry `> 25,000` tris / `> 1,000 tris/m²` where lighter prediction physics are preferred). Adjust **`Max Hulls`** / **`Concavity`** and click **`Check Collision`** to preview the simplified collision hulls.
   - **`Custom Collision object`**: Lets you pick a separate lower-poly mesh (`_COL`) for Havok physics (also supporting 1:1 Havok collision up to `100,000` triangles, `< 25,000` tris recommended) while keeping a high-poly visual mesh.
   - **`No Skater Collision (Visual / Decal Prop)`**: Skaters pass right through the QDO, while Quick Drop can still grab and place it.

> **💡 Ramp Entry Tip:** Ultra-thin, razor-flat feathered ramp entries at the bottom of a quarterpipe or kicker can cause the skater's wheels to catch or buck. Give the bottom lip a slightly steeper transition angle from flat ground.

<img width="1776" height="548" alt="Group 11" src="https://github.com/user-attachments/assets/524d2db2-78ef-40e8-8c0c-9f86b4d864fe" />


</details>

<details>
<summary><b>Page 3 — Surfaces & Textures (Shaders, Sound & Look)</b></summary>
<br>

Each **Material Slot** on your QDO mesh can have its own skating physics feel, PBR texture sheet resolution, and rolling/landing/grinding sound effect.

1. **Select Your Texture Atlas Resolution (`1K – 16K`):**
   - Under **Atlas Resolution**, choose from **1K–16K (`1024` to `16384`) texture atlases** (`2048 × 2048` 2K High-Res is the default; options include `1024`, `2048`, `4096`, `8192`, and `16384`).
   - **Automatic Multi-Shader Atlasing**: If your QDO uses more than 1 material slot (for example, a brick hubba with steel coping, or a wood-and-metal kicker), Page 3 displays a live **Multi-Shader Atlas** preview showing per-shader pixel resolution and VRAM usage. **2-shader QDOs automatically use a `2:1` rectangular atlas** (e.g., `2048×1024` at 2K or `4096×2048` at 4K — 50% smaller VRAM), while 3+ material slots pack cleanly onto a shared square sheet where you can step up to `4K`, `8K`, or `16K` if needed.
2. **Assign Material Surfaces & Sound:**
   - Under **Material Surfaces**, click **`Select Material`** next to any material slot to select all of its faces in Edit Mode (and click **`Exit Select`** to return to Object Mode), then choose its surface type from the dropdown:
     - **`Concrete (Smooth)`** / **`Concrete (Rough)`** / **`Brick / Pavers`**
     - **`Wood (Plywood / Ramp)`** / **`Wood (Planks / Boardwalk)`**
     - **`Metal (Solid / Rail)`** / **`Metal (Hollow / Pipe)`**
     - **`Grass / Turf`** / **`Dirt / Mud`**
     - **`Un-Grindable (Plastic / Rubber)`** — Solid collision you can ride and ollie on, but **trucks will never lock into a grind** on these faces (great for traffic cones, rubber bases, or ramp supports).
     - **`Decal Overlay (Cutout / Sticker — No Collision)`** — Renders as an alpha cutout (`0x273`) or metallic PBR decal (`0x771`) and **excludes those faces from skater collision** so wheels never snag on floating stickers.
3. **Checking Your Principled BSDF Textures:**
   - Expand **`Shader Texture Previews`** to see which PBR image maps (`Base Color`, `Normal Map`, `Roughness`, `Metallic`) are plugged into your Blender **Principled BSDF** material.
   - Don't have a material yet? Expand **`🎨 Starter Material Templates`** to apply a ready-made Concrete, Wood, Metal, Asphalt, Marble, or Painted material in 1 click.

<img width="3671" height="1201" alt="Group 15" src="https://github.com/user-attachments/assets/92146d5f-ef2a-4c2f-b2c1-4a06cccf91d8" />

</details>

<details>
<summary><b>Page 4 — Lights (In-Game) <i>(Optional)</i></b></summary>
<br>

Want a streetlamp, spotlight, or construction light that casts real-time light across your **Quick Drop Objects (`QDOs`)**, DIY skate spots, or custom skateparks in *Skate.*?

1. Click **`+ Add Spot Light`** or **`+ Add Point Light`**.
2. Move and rotate the spawned light in the 3D viewport so it aims where you want.
3. Select the light in the **Lights on this prop** list to tweak its **Color**, **Intensity (W)**, **Cone Angle**, **Radius (m)**, and **Cast Shadows**.
 
 
 QUICK NOTE: **Quick Drop Objects (`QDOs`) do not currently support emission/glowing surfaces.** In order to have a glowing surface the best you can do is fake it using a point light and a spotlight combined. Place a point light on the surface you want to glow. Set its radius to a small amount and then add a larger point light or spotlight on top of it
 
</details>

<details>
<summary><b>Page 5 — Identity & Thumbnail</b></summary>
<br>

This page sets how your QDO (such as a street ledge, handrail, flatbar, hubba, stair set, manny pad, kicker, or quarterpipe) is named, organized, and previewed inside the in-game Quick Drop menu.

1. **Prop Name**: Type the display name shown in Quick Drop (e.g., `Brick Ledge Wide`).
2. **Quick Drop Tab Name**:
   - Type the name of the custom Quick Drop tab where this QDO should appear (e.g., `CustomProps`, `StreetLedges`, `Funboxes`, `DIYSpots`).
   - **You do NOT need to type `bk`** at the beginning—ReSkate automatically formats and prefixes the internal tab ID for you behind the scenes!
   - **1-Click Tab Reuse (`🗂️ Quick Drop Tabs in Scene` Twirl-Down)**: Expand the **`Quick Drop Tabs in Scene (Click to Assign)`** twirl-down menu right below the text box to see every tab name used in your current `.blend` scene. Just click a tab button to assign the active QDO (or all selected QDOs) to that tab without retyping it!
3. **Variant Family (Optional — Stack Multiple QDOs into 1 Quick Drop Tile)**:
   - Have several versions of a custom Quick Drop obstacle (e.g., Clean, Waxed, Graffiti, or Small/Medium/Large ledges and flatbars)?
   - Select all of them in the viewport and click **`Group N Selected Props as Variants (1 Quick Drop Tile)`** (or type the same **Variant Family** name on each prop).
   - Grouped variant QDOs automatically receive a **bottom-left corner notch** (Green by default, customizable in Add-on Preferences) on both their Blender Carousel icon and their in-game Quick Drop thumbnail so you can easily tell which obstacles have variants.
4. **Thumbnail Camera & Render (Optional)**:
   - By default, compiling without a rendered thumbnail uses a fast `"No Thumbnail"` placeholder icon so compile time is `0 ms`.
   - When you're ready to render an icon: click **`Frame & View Camera`**, use **`↺ -45°`** / **`↻ +45°`** (or scroll wheel to zoom in/out), and click **`Render Thumbnail`**. If you middle-mouse rotate out of the camera view, ReSkate automatically exits camera mode and restores normal viewport lighting.

</details>

<details>
<summary><b>Page 6 — Package & Compile</b></summary>
<br>

1. **On-Demand Pre-Flight Check**:
   - Click **`Run Pre-Flight Check`** on Page 6 anytime (it also runs automatically right before compiling) without slowing down normal viewport navigation.
   - Click **`Fix All`** to apply safe geometry fixes (Scale/Rotation, Origin, N-gon triangulation) in 1 click. UV warnings are strictly advisory so `Fix All` never alters or repacks your UV maps or textures.
2. **Prop Pack Name & Collapsible Scene Prop Packs**:
   - Type a **`Prop Pack Name`** (e.g., `StreetPack` or `DIYSkatepark`). ReSkate automatically combines this with your Creator Name from Preferences (`YourName-StreetPack`).
   - Expand the **`Scene Prop Packs (Click to Switch / Assign)`** twirl-down to view all QDO packs assigned to objects in the current `.blend` scene in memory:
     - Click **`[PackName (N props)]`** to switch to that pack (and assign any selected QDOs to it).
     - Click **`[Select]`** to select all QDOs in the viewport that belong to that pack.
     - Click **`[X]`** to unassign/remove that pack tag from the scene without deleting your 3D meshes.
3. **Compile & Deploy**:
   - Select 1 QDO and click **`Compile and Deploy This Prop`**, OR select multiple custom Quick Drop obstacles and click **`Compile and Deploy Prop Pack (N Props)`**.
   - ReSkate QuickDrop Studio compiles the Havok physics, textures, thumbnails, and Frostbite bundles in the background and deploys the pack straight to your `Skate/Mods/` folder!
4. **Share on Thunderstore (Optional)**:
   - Expand **`📦 Thunderstore Icon, README & Description`** if you want to customize the pack description or icon, then click **`Export Release .zip (Thunderstore)`** to build a ready-to-upload Thunderstore package.

QUICK NOTE: the first time you compile a set of Quick Drop Objects (`QDOs`), it will take a bit of time. Subsequent exports will be faster as they use a cached version of the unchanged elements for each obstacle. 

</details>

---

<img width="1927" height="834" alt="Group 14" src="https://github.com/user-attachments/assets/aa77140a-72cf-48d6-8ee7-5e7b550914af" />

Additional Info - Courtesy of the ReSkate community
<img width="667" height="128" alt="Screenshot 2026-11-08 at 3 17 56 PM" src="https://github.com/user-attachments/assets/653c0f52-4dd6-4ad3-bac7-05cddf5549fc" />



## 🔧 Quick Troubleshooting FAQ

<details>
<summary><b>Click to expand Common Questions & Fixes</b></summary>
<br>

| Symptom | Cause & Solution |
| :--- | :--- |
| **QDO textures export as flat gray / missing details in-game, even though they look right in EEVEE** | Your material uses procedural Blender nodes or **UV maps scaled outside `[0, 1]`** (which Quick Drop Objects do not support). Pack your UVs manually inside `[0, 1]` and plug baked PBR image textures (**Base Color**, **Normal**, **Roughness**, **Metallic**) into a **Principled BSDF** node. |
| **Textures look blurry on a multi-material QDO (e.g., brick hubba, graffiti ledge, or custom skatepark piece)** | Frostbite packs multi-material QDOs onto a shared texture atlas (**selectable 1K–16K (`1024` to `16384`), `2048` default**, with **automatic `2:1` rectangular atlases for 2-shader QDOs**). Step **Atlas Resolution** up to `4096` (4K), `8192` (8K), or `16384` (16K) on **Page 3**, or combine material slots for sharper per-shader pixels. |
| **Skater prediction lags or stutters around a high-poly obstacle or DIY skate spot** | While 1:1 Havok skateable collision works up to **`100,000` triangles**, meshes above **`25,000` tris** or **`1,000 tris/m²`** dense micro-triangles can slow trajectory prediction. Keep collision under `25,000` tris (`<= 65,535` visual verts), or switch **Page 2** to **`Simple Hulls`** or a low-poly **`Custom Collision object` (`_COL`)**. |
| **Skateboard wheels clip or get stuck on a decal / sticker on a ledge, stair, or ramp** | The decal is a separate floating mesh slightly offset from the surface and still has solid collision. Either **author the decal into the main QDO texture**, or set the decal's Material Slot on **Page 3** to **`Decal Overlay (Cutout / Sticker — No Collision)`**. |
| **QDO jitters or freaks out when placed on flat ground in Quick Drop** | The origin point is dead-flush at `0.00 m` or misaligned. Go to **Page 1** and click **`Origin to Ground Contact (Recommended)`** (which raises the origin by `+0.05 m`), then click **`Set Prop Flush to Ground (Z = 0)`** and recompile. |
| **Skateboard bucks at the bottom entry of a custom kicker or quarterpipe** | The bottom lip of the ramp is feathered too thin/flat. Give the bottom entry a slightly steeper transition angle from flat ground. |
| **Where are the 3D Grind Edge / Collision viewport overlays?** | Overlays are off by default for zero GPU overhead on large scenes. Expand **`🖥️ Viewport Overlays`** at the top of the **ReSkate QuickDrop** panel and toggle on any overlay you want to inspect. |

</details>

---

## ⚠️ IMPORTANT: Textures, UV Maps, Multi-Shader Atlases & Collision Budgets

Before exporting your first Quick Drop Object (`QDO`) or custom skatepark obstacle, keep these four rules in mind so your textures and skate physics behave properly in-game:

1. **Author Game-Ready PBR Textures Inside `[0, 1]` UV Maps (Why EEVEE QDOs Can Export Flat Gray):**
   - Blender's EEVEE viewport can display procedural shader nodes, complex node groups, and **scaled-up tiling UV maps** (UV islands stretched far outside the `[0, 1]` UV square).
   - **Quick Drop Objects (`QDOs`) in *Skate.* cannot tile UVs outside `[0, 1]` or read Blender procedural nodes.** If a street ledge, manny pad, or rail looks great in EEVEE but shows up **flat gray** or with missing details in-game, its UVs are scaled outside `[0, 1]` or its shader isn't using standard baked image textures.
   - **The Fix:** Pack your UVs inside the `[0, 1]` UV square and plug standard game-ready image textures (**Base Color**, **Normal**, **Roughness**, **Metallic**) directly into a **Principled BSDF** node before compiling.
2. **Choose the Right 1K–16K Texture Atlas Resolution (Multi-Shader QDOs):**
   - When a QDO has **more than 1 material slot**, Frostbite packs all of its shaders onto a single shared texture atlas at compile time—**selectable from 1K (`1024`) up to 16K (`16384`) on Page 3**, with **`2048 × 2048` (`2K`) as the default**.
   - **2-shader QDOs automatically use a `2:1` rectangular atlas** (e.g., `2048×1024` at 2K or `4096×2048` at 4K — cutting VRAM in half without losing horizontal detail). For 3+ shader obstacles (stairs, hubbas, DIY spots), step up to `4096` (4K), `8192` (8K), or `16384` (16K) if needed (keeping VRAM budget warnings in mind), or combine materials where possible.
3. **Don't Leave Floating Decal Meshes With Solid Collision:**
   - Whenever possible, **author stickers, graffiti, and decals directly into the QDO's main Base Color texture** so the skating surface is one clean mesh.
   - If you do use separate floating geometry for a decal or sticker, **do not leave it as a solid surface**: if a decal mesh is offset even a few millimeters off a ledge or ramp, **your skateboard wheels will clip into the edge of the decal and get stuck**.
   - **The Fix:** On **Page 3 — Surfaces & Textures**, set any floating decal material slot to **`Decal Overlay (Cutout / Sticker — No Collision)`**. ReSkate will automatically strip physical skater collision from those faces while rendering the alpha cutout (`0x273`) or metallic PBR decal (`0x771`) in-game.
4. **Stay Within Optimal Havok Collision Budgets (`100,000`-Tri Ceiling, `< 25,000` Recommended):**
   - **ReSkate QuickDrop Studio** supports **1:1 Havok skateable collision up to `100,000` triangles** (`<= 65,535` visual mesh vertices), so even intricate street ledges, handrails, flatbars, hubbas, stairs, kickers, and quarterpipes compile cleanly.
   - For optimal skater trajectory prediction performance, keep collision under **`25,000` triangles** and **`< 1,000 tris/m²`**; for heavier scans or full custom skateparks, use **`Simple Hulls`** or a clean **`Custom Collision object` (`_COL`)** on **Page 2**.

---

## 🙌 Acknowledgements

- **NOBLE** — Huge shout-out to **NOBLE** for his foundational insights, research, and discoveries into making custom Quick Drop Objects work in *Skate.* (including dynamic lighting blueprints, prop shaders, and Quick Drop item registration).
- **Crem** — Creator and maintainer of **ReSkate QuickDrop Studio**.
- **The ReSkate Team & Community** — For building the core ReSkate modding framework and testing custom Quick Drop obstacles and DIY skateparks.

---

## 📄 License

Open-source under the **MIT / GPL-3.0-or-later** license. Created and maintained by **Crem** for the **ReSkate** community.
