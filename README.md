
# 🛹 ReSkate Prop Studio (`v0.33.3`)
(Soon to be ReSkate QuickDrop Studio)

<img width="2350" height="1597" alt="Group 172" src="https://github.com/user-attachments/assets/b3d0d0d6-31c3-4db3-9dcf-ac1847d5fe97" />



**Turn any 3D mesh in Blender into a standalone, skateable custom Quick Drop asset for *ReSkate.* in just a few steps.**

ReSkate Prop Studio is an open-source Blender add-on (**Blender 4.2 – 5.3+**) that compiles 3D models directly into standalone **ReSkate** prop packs—complete with **1:1 Havok skate & grind physics**, **authentic surface audio** (concrete, wood, metal, brick, grass), **PBR textures**, **dynamic in-game Spot/Point lights**, **Quick Drop variant stacking**, and **1-click Thunderstore `.zip` packaging**.

> **Zero Donor Hijacking:** Every prop you compile is 100% standalone and additive. Your props are added into their own custom Quick Drop tabs and never overwrite base-game props.

---


## 🛠️ Step-by-Step Installation & First-Time Setup


You do **not** need any command-line tools or external scripts—everything runs right inside Blender. Click each step below to expand it:

<details open>
<summary><b>1. What You Need Installed</b></summary>
<br>

- **Blender 4.2 – 5.3+** (**Blender 5.2 / 5.3 LTS** recommended)
- **ReSkate** installed in your *Skate.* game folder
- **`reskate_prop_studio_addon.zip`** (from the Releases page)
-  **ReSkate Studio** Which can be found in the ReSkate Discord server.
-  Reskate Studio folder should be in your Skate parent folder (where the skate.exe / reskatelauncher.exe is located)

</details>

<details>
<summary><b>2. Install the Add-on in Blender</b></summary>
<br>

1. Open Blender and go to **`Edit` → `Preferences...`** in the top menu bar.
2. Click the **`Add-ons`** tab on the left.
3. Click the **drop-down arrow (`⌄`)** in the top-right corner of the Add-ons window and choose **`Install from Disk...`** *(or click `Install...` in Blender 4.2)*.
4. Select **`reskate_prop_studio_addon.zip`** and click **`Install from Disk`**.
5. Make sure the checkbox next to **`3D View: ReSkate Prop Studio`** is **checked (`☑️`)**.

(You're also be able to drag and drop the .zip file from the releases directly into the viewport of Blender to have it install. If you would prefer that.)

</details>

<details>
<summary><b>3. Configure Your Preferences (One-Time Setup)</b></summary>
<br>

While you are still inside **`Edit` → `Preferences` → `Add-ons`**, click the arrow next to **`ReSkate Prop Studio`** to expand its settings:

| Setting | What to Enter |
| :--- | :--- |
| **Skate. Game Folder (`Skate.exe`)** | Click the folder icon and select your *Skate.* installation folder (the folder that contains `Skate.exe`). |
| **Staging & Cache Folder** | Leave on the default (`.../ReSkate Studio/Native/Research/Temp_DirectExport`) or pick any local working folder with ~2 GB of free space. |
| **Creator / Author Name** | Type your creator handle (e.g., `YourName`). This is automatically used to name your Prop Packs (`YourName-PackName`). |
| **Origin Ground Offset (m)** | Default **`0.05 m`** (`5 cm`). Raises the placement origin slightly above the bottom of the mesh when clicking `Origin to Ground Contact` so props don't jitter against the ground in Quick Drop. |
| **Variant Corner Notch Color** | Default **Green**. Sets the color of the bottom-left corner badge drawn on thumbnails when props are grouped into a Variant Family. |
| **Auto-Render Missing Thumbnails on Compile** | **Off by default** for instant `0 ms` compile times (uses a shared static `"No Thumbnail"` icon until you click `Render Thumbnail` on Page 5). Enable this if you want Blender to launch EEVEE automatically during compile whenever a prop has no thumbnail. |
| **Enable Viewport Overlays** | Master switch for 3D viewport overlays. Zero GPU draw handlers are attached when all overlays in the panel are turned off. |

</details>

<details>
<summary><b>4. Open the ReSkate Prop Sidebar in the 3D Viewport</b></summary>
<br>

1. Close the Preferences window and click anywhere in Blender's **3D Viewport**.
2. Press **`N`** on your keyboard to open the right-hand Sidebar.
3. Click the **`ReSkate Prop`** tab on the vertical tab strip.

<img width="1964" height="1087" alt="image" src="https://github.com/user-attachments/assets/79b35a3c-370b-46fd-ac00-4e739f253903" />

</details>

---

## 🚀 Beginner Guide: The 6-Page Prop Workflow

At the top of the **ReSkate Prop** panel, you will always see:
- **Prop Carousel**: Shows thumbnails of your selected prop(s) (or `"No Prop Selected"` when nothing is selected). Props without a rendered thumbnail yet display a fast shared `"No Thumbnail"` icon.
  - 🟩 **Green Bottom-Left Corner Notch**: Means the prop is part of a **Variant Family** (customizable in Preferences).
  - **`Frame Selected` (Left Button)**: Centers your 3D viewport camera on the currently selected prop(s).
  - **`Select All in Pack (N)` (Right Button)**: Selects every prop in your `.blend` scene that belongs to the active Prop Pack in memory.


- **`📏 Scale References` Twirl-Down**: Click **`Spawn Skater`**, **`Spawn Skateboard`**, **`Round Rail Pipe`**, or **`Spawn Ground Plane`** anytime to check your prop's real-world size (`1 Blender meter = 1 Game meter`). Scale references spawn right at your **3D Cursor** with their feet/wheels flush on the cursor's Z plane, and you can also use **`Set Prop Flush to Ground (Z = 0)`** on them directly!

<img width="1615" height="603" alt="Group 12" src="https://github.com/user-attachments/assets/a3409b09-5760-4f7f-98a4-aa1440ca7957" />


---
---
---

Click each workflow page below to open or close its step-by-step instructions:

<details open>
<summary><b>Page 1 — Scale & Origin</b></summary>
<br>

Your prop's **Origin Point** (the little orange dot in Blender) determines where the Quick Drop cursor grabs the prop and how it sits on the ground.

1. Select your 3D mesh prop in the viewport and check the **Dimensions** box (`Width × Depth × Height` in meters).
2. Click **`Origin to Ground Contact (Recommended)`**.
   - This centers the origin horizontally on the bottom footprint of your prop and raises the origin point **`+0.05 m`** (`5 cm`) above the lowest vertices so the prop places smoothly on the ground without jittering or clipping.
3. Click **`Set Prop Flush to Ground (Z = 0)`** to rest the bottom of the mesh (or any selected Skater/Skateboard reference) cleanly on the `Z = 0` ground plane.
4. *(Optional)* Need a custom pivot point? Click **`Move Origin Point`**, drag the Move gizmo arrows in the viewport, and click **`Done Moving Origin`**.

<img width="2006" height="749" alt="Group 7" src="https://github.com/user-attachments/assets/bfebaa01-39c8-4e20-a06e-bf8556c1e516" />

</details>

<details>
<summary><b>Page 2 — Collision</b></summary>
<br>

This page controls how the skateboard, wheels, trucks, and player body collide with and grind your prop.

1. Leave **Collision Mode** set to **`Same as visible mesh (Recommended)`**.
   - You will see **`✅ 1:1 Visible Mesh Collision Active`**. Every ledge, rail, coping, and transition on your visible mesh is automatically compiled 1:1 into Havok physics, and sharp top edges are automatically detected as grindable.
2. **When to use the other modes:**
   - **`Simple Hulls (For high-poly props)`**: Use this if your prop has more than **8,000 triangles**. Adjust **`Max Hulls`** / **`Concavity`** and click **`Check Collision`** to preview the simplified collision hulls.
   - **`Custom Collision object`**: Lets you pick a separate low-poly mesh (`_COL`) for physics while keeping a high-poly visual mesh.
   - **`No Skater Collision (Visual / Decal Prop)`**: Skaters pass right through the prop, while Quick Drop can still grab and place it.

> **💡 Ramp Entry Tip:** Ultra-thin, razor-flat feathered ramp entries at the bottom of a quarterpipe or kicker can cause the skater's wheels to catch or buck. Give the bottom lip a slightly steeper transition angle from flat ground.

<img width="1776" height="548" alt="Group 11" src="https://github.com/user-attachments/assets/524d2db2-78ef-40e8-8c0c-9f86b4d864fe" />


</details>

<details>
<summary><b>Page 3 — Surfaces (aka Shaders, Sound & Look)</b></summary>
<br>

Each **Material Slot** on your mesh can have its own skating physics feel and rolling/landing/grinding sound effect.

1. Under **Material Surfaces**, click **`Select Material`** next to any material slot to select all of its faces in Edit Mode (and click **`Exit Select`** to return to Object Mode), then choose its surface type from the dropdown:
   - **`Concrete (Smooth)`** / **`Concrete (Rough)`** / **`Brick / Pavers`**
   - **`Wood (Plywood / Ramp)`** / **`Wood (Planks / Boardwalk)`**
   - **`Metal (Solid / Rail)`** / **`Metal (Hollow / Pipe)`**
   - **`Grass / Turf`** / **`Dirt / Mud`**
   - **`Un-Grindable (Plastic / Rubber)`** — Solid collision you can ride and ollie on, but **trucks will never lock into a grind** on these faces (great for traffic cones, rubber bases, or ramp supports).
   - **`Decal Overlay (Cutout / Sticker — No Collision)`** — Renders with alpha cutout and **excludes those faces from skater collision** so wheels never snag on floating stickers.
2. **Checking Your Principled BSDF Textures:**
   - Expand **`Shader Texture Previews`** to see which PBR image maps (`Base Color`, `Normal Map`, `Roughness`, `Metallic`) are plugged into your Blender **Principled BSDF** material.
   - If your prop has more than 1 material slot, Page 3 displays an automatic **Multi-Shader Atlas Resolution Warning** showing the estimated per-shader pixel resolution on Frostbite's `2048×2048` atlas.
   - Don't have a material yet? Expand **`🎨 Starter Material Templates`** to apply a ready-made Concrete, Wood, Metal, Asphalt, Marble, or Painted material in 1 click.

<img width="3671" height="1201" alt="Group 15" src="https://github.com/user-attachments/assets/92146d5f-ef2a-4c2f-b2c1-4a06cccf91d8" />

</details>

<details>
<summary><b>Page 4 — Lights (In-Game) <i>(Optional)</i></b></summary>
<br>

Want a streetlamp, spotlight, or construction light that casts real-time light in *Skate.*?

1. Click **`+ Add Spot Light`** or **`+ Add Point Light`**.
2. Move and rotate the spawned light in the 3D viewport so it aims where you want.
3. Select the light in the **Lights on this prop** list to tweak its **Color**, **Intensity (W)**, **Cone Angle**, **Radius (m)**, and **Cast Shadows**.
 
 
 QUICK NOTE: **Quick Drop objects do not currently support emission/glowing surfaces.** In order to have a glowing surface the best you can do is fake it using a point light and a spotlight combined. Place a point light on the surface you want to glow. Set its radius to a small amount and then add a larger point light or spotlight on top of it
 
</details>

<details>
<summary><b>Page 5 — Identity & Thumbnail</b></summary>
<br>

This page sets how your prop is named, organized, and previewed inside the in-game Quick Drop menu.

1. **Prop Name**: Type the display name shown in Quick Drop (e.g., `Brick Ledge Wide`).
2. **Quick Drop Tab Name**:
   - Type the name of the custom Quick Drop tab where this prop should appear (e.g., `CustomProps`, `StreetLedges`, `Funboxes`).
   - **You do NOT need to type `bk`** at the beginning—ReSkate automatically formats and prefixes the internal tab ID for you behind the scenes!
   - **1-Click Tab Reuse (`🗂️ Quick Drop Tabs in Pack / Scene` Twirl-Down)**: Expand the **`Quick Drop Tabs in Pack / Scene (Click to Assign)`** twirl-down menu right below the text box to see every tab name used in your current Prop Pack or `.blend` scene. Just click a tab button to assign the active prop (or all selected props) to that tab without retyping it!
3. **Variant Family (Optional — Stack Multiple Props into 1 Quick Drop Tile)**:
   - Have several versions of a prop (e.g., Clean, Waxed, Graffiti, or Small/Medium/Large)?
   - Select all of them in the viewport and click **`Group N Selected Props as Variants (1 Quick Drop Tile)`** (or type the same **Variant Family** name on each prop).
   - Grouped variant props automatically receive a **bottom-left corner notch** (Green by default, customizable in Add-on Preferences) on both their Blender Carousel icon and their in-game Quick Drop thumbnail so you can easily tell which props have variants.
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
   - Type a **`Prop Pack Name`** (e.g., `StreetPack`). ReSkate automatically combines this with your Creator Name from Preferences (`YourName-StreetPack`).
   - Expand the **`Scene Prop Packs (Click to Switch / Assign)`** twirl-down to view all packs assigned to objects in the current `.blend` scene in memory:
     - Click **`[PackName (N props)]`** to switch to that pack (and assign any selected props to it).
     - Click **`[Select]`** to select all props in the viewport that belong to that pack.
     - Click **`[X]`** to unassign/remove that pack tag from the scene without deleting your 3D meshes.
3. **Compile & Deploy**:
   - Select 1 prop and click **`Compile and Deploy This Prop`**, OR select multiple props and click **`Compile and Deploy Prop Pack (N Props)`**.
   - ReSkate compiles the Havok physics, textures, thumbnails, and Frostbite bundles in the background and deploys the pack straight to your `Skate/Mods/` folder!
4. **Share on Thunderstore (Optional)**:
   - Expand **`📦 Thunderstore Icon, README & Description`** if you want to customize the pack description or icon, then click **`Export Release .zip (Thunderstore)`** to build a ready-to-upload Thunderstore package.

QUICK NOTE: the first time you compile a set of assets, it will take a bit of time. Subsequent exports will be faster as they use a cached version of the unchanged elements for each prop. 

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
| **Prop textures export as flat gray / missing details in-game, even though they look right in EEVEE** | Your material uses procedural Blender nodes or **UV maps scaled outside `[0, 1]`** (which Quick Drop props do not support). Pack your UVs manually inside `[0, 1]` and plug baked PBR image textures (**Base Color**, **Normal**, **Roughness**, **Metallic**) into a **Principled BSDF** node. |
| **Textures look blurry on a prop with many material slots** | Frostbite packs multi-material props into a shared `2048×2048` texture atlas (e.g., 8 shaders = `~680px` per shader). Reduce the number of material slots on the mesh for higher per-shader resolution. |
| **Skateboard wheels clip or get stuck on a decal / sticker on a ledge or ramp** | The decal is a separate floating mesh slightly offset from the surface and still has solid collision. Either **author the decal into the main prop texture**, or set the decal's Material Slot on **Page 3** to **`Decal Overlay (Cutout / Sticker — No Collision)`**. |
| **Prop jitters or freaks out when placed on flat ground in Quick Drop** | The origin point is dead-flush at `0.00 m` or misaligned. Go to **Page 1** and click **`Origin to Ground Contact (Recommended)`** (which raises the origin by `+0.05 m`), then click **`Set Prop Flush to Ground (Z = 0)`** and recompile. |
| **Skateboard bucks at the bottom entry of a custom ramp** | The bottom lip of the ramp is feathered too thin/flat. Give the bottom entry a slightly steeper transition angle from flat ground. |
| **Where are the 3D Grind Edge / Collision viewport overlays?** | Overlays are off by default for zero GPU overhead on large scenes. Expand **`🖥️ Viewport Overlays`** at the top of the ReSkate Prop panel and toggle on any overlay you want to inspect. |

</details>

---

## ⚠️ IMPORTANT: Textures, UV Maps & Multi-Shader Props

Before exporting your first prop, keep these three rules in mind so your textures and collision behave properly in-game:

1. **Author Game-Ready PBR Textures Inside `[0, 1]` UV Maps (Why EEVEE Props Can Export Flat Gray):**
   - Blender's EEVEE viewport can display procedural shader nodes, complex node groups, and **scaled-up tiling UV maps** (UV islands stretched far outside the `[0, 1]` UV square).
   - **Quick Drop props in *Skate.* cannot tile UVs outside `[0, 1]` or read Blender procedural nodes.** If a prop looks great in EEVEE but shows up **flat gray** or with missing details in-game, its UVs are scaled outside `[0, 1]` or its shader isn't using standard baked image textures.
   - **The Fix:** Pack your UVs inside the `[0, 1]` UV square and plug standard game-ready image textures (**Base Color**, **Normal**, **Roughness**, **Metallic**) directly into a **Principled BSDF** node before compiling.
2. **Keep Material Slot Counts Low (Frostbite Texture Atlasing):**
   - When a prop has **more than 1 material slot**, Frostbite packs all of its shaders onto a single shared `2048×2048` texture atlas at compile time.
   - More material slots = less resolution per shader (for example, **8 shaders on a 2048 atlas = ~680px per shader**). Combine materials where possible so your textures stay crisp!
3. **Don't Leave Floating Decal Meshes With Solid Collision:**
   - Whenever possible, **author stickers, graffiti, and decals directly into the prop's main Base Color texture** so the skating surface is one clean mesh.
   - If you do use separate floating geometry for a decal or sticker, **do not leave it as a solid surface**: if a decal mesh is offset even a few millimeters off a ledge or ramp, **your skateboard wheels will clip into the edge of the decal and get stuck**.
   - **The Fix:** On **Page 3 — Shaders, Sound & Look**, set any floating decal material slot to **`Decal Overlay (Cutout / Sticker — No Collision)`**. ReSkate will automatically strip physical skater collision from those faces while rendering the alpha cutout in-game.

---

## 🙌 Acknowledgements

- **NOBLE** — Huge shout-out to **NOBLE** for his foundational insights, research, and discoveries into making custom props work in *Skate.* (including dynamic lighting blueprints, prop shaders, and Quick Drop item registration).
- **Crem** — Creator and maintainer of **ReSkate Prop Studio**.
- **The ReSkate Team & Community** — For building the core ReSkate modding framework and testing custom props.

---

## 📄 License

Open-source under the **MIT / GPL-3.0-or-later** license. Created and maintained by **Crem** for the **ReSkate** community.
