# Scena user guide

[Back to Scena](../README.md) · [Install](../README.md#install) · [Quick start](../README.md#quick-start)

## Create and edit groups

- In Object Mode, use **Shift+A → Group** to group the selected objects. With an empty selection, it makes an empty group at the 3D Cursor. Scena stores the group definition in a separate collection.
- The **Create Group** button has a type switch for collection instances and Geometry Nodes groups. Leave the name blank to use the active object's name. To convert existing groups, select them and use **Operations → Switch to Geometry Nodes** or **Switch to Collection**. Already matching groups and regular objects are skipped.
- Select a group and open **Object Properties → Group Data** to rename the definition, switch to a different definition, or inspect where it is used. **Make Group Unique** gives this placement its own group definition while keeping member meshes shared.
- Press **E → Edit Group** to edit at the selected placement. You can also select multiple placements before entering. Hidden instances stay hidden, and instances excluded from the View Layer do not prevent entering. Choose **Edit Child Group** to go deeper. **Back** goes up one level, and Shift-clicking **Back** returns directly to the scene.

## Duplicate and make variants

The **Duplicate** button performs the same action as **Shift+D**. Its dropdown offers three sharing levels.

| Mode | Default shortcut | What the copy shares |
| --- | --- | --- |
| **Instance** | **Alt+D** | The whole group definition, including member transforms and meshes |
| **Group** | **Shift+D** | Nested group definitions and member meshes. The top-level definition is independent. |
| **Group and Nested** | **Shift+Alt+D** | Member meshes. Both top-level and nested group definitions are independent. |

For an existing placement, choose **F3 → Make Single User**. **Group** makes the definition independent. **Group and Data** also makes member data independent, so you can edit a tabletop mesh in only one placement. **Group, Data, and Materials** also makes material edits independent.

## Navigate and pick in the viewport

- The **Group** sidebar and **E** pie menu provide group creation, duplication, member editing, navigation, and picking. **Operations → Select Linked** selects eligible linked instances in the current scope.
- In Object Mode, press **Alt+E** to start **Pick**. Hover over a model to see object and group labels. Left-click to select an object or middle-click to select a group. Scroll to change the group level. Hold **Shift** while scrolling to cycle overlapping candidates. Double-click to navigate one level at a time. In Pick, **Shift** plus double-click jumps to the deepest level or exits all levels. Rebind Pick in **Scena Preferences → Group → Shortcuts**.
- In the main viewport, Ctrl-click a group or child group to enter one level. Ctrl-click a direct member or an area outside the current group to go back a level. Ctrl-double-click a nested target to reach the deepest member, or use it outside the group to return directly to the scene.
- Inside a group, click a viewport group label to select it, or double-click to enter it. Click the current group's outline label to select all selectable direct members. Click a group name in the viewport header path to jump to that level. Double-click empty space outside the current outline to go up a level.
- A solid blue outline marks the active group. When editing a child, a dashed outline marks the root. Direct members and child groups have labels, and selected members use Blender's theme highlight.

## Manage groups and their origins

- **Ungroup** expands one level. Hold **Shift** to expand every nested level. The wrench beside the button toggles **Keep Modifiers**. Use **Add** and **Remove** to change membership. **Operations** also contains **Split Mirror** and the group type conversions.
- Use **Object → Set Origin** or the viewport context menu to place a group origin at its geometry center, the 3D Cursor, its surface center of mass, or its volume center of mass. Scena compensates for repositioning across linked placements. **Geometry to Origin** moves the shared geometry and is visible in every placement.
- Open **Group Library** in the sidebar or pie menu to search definitions, inspect usage across scenes, and locate nested placements. Click **+** to place at the 3D Cursor. **Remove Unused Definitions** cleans up definitions with no placements or other data-block references.

## Use modifiers

For Geometry Nodes groups, **Scena Group → Output** determines what Blender's modifiers receive.

| Output | Modifier behavior |
| --- | --- |
| **Instance** | Native **Mirror** and **Array** can place linked copies of the whole group. Mirror respects its axes and reference object. Array respects count and offset. Multiple consecutive Arrays can be used with one Mirror in stack order. |
| **Mesh** | Native modifiers operate on the evaluated mesh for this placement. Mirror's Bisect and Merge settings work on the mesh. Bevel, Boolean, Solidify, Subdivision, and Displace can be added through **Add Modifier**. |

The mesh output exposes vertex group and UV names to native modifiers such as **Mask**, **Displace**, and **UV Warp**. To use **Hook**, assign weights on the source mesh, then choose the target object and vertex group in the placement's Hook modifier. Physics caches and Multires sculpting have not been verified.

## Preferences

Use the gear beside the **Group** panel title or open **Edit → Preferences → Add-ons → Scena**.

- **Tool** enables optional pie menus for Save, Shader, Transform, and Cursor and Origin. **Outliner** configures the shortcut for making a collection from selected items.
- **Group** controls creation defaults, viewport header navigation, and the Scena and Pick shortcuts.
- **Display** adjusts outline thickness, the 2D and 3D frame styles, and which object types show outlines.

## Performance and testing

This historical benchmark used Blender 5.3.0 in background mode on one machine. It measured selecting a placement, entering group edit, and exiting, with 1,000 linked instances of a group containing one quad. Each version ran once as a warm-up and five times for the median.

| Metric | 0.2.3 | 0.2.4 |
| --- | ---: | ---: |
| Select, enter edit, and exit | 1,015.14 ms | 65.47 ms |
| Temporary objects when editing one placement | 2,000 | 2 |

In the same setup, group entry and exit took about **one-fifteenth of the previous time**. A separate synchronization check moved a group member and edited one mesh vertex. All 1,000 instances matched during editing and after exit.

Viewport tests cover editing rotated and non-uniformly scaled nested groups, undo and redo, and reopening a scene saved during group editing. The 0.3.1 release ZIP was installed in a clean Blender 5.3.0 profile and tested after restart by creating both collection-instance and Geometry Nodes groups.

[Workflow tests](../tests/test_workflows.py) · [Nested edit tests](../tests/test_edit_depth.py) · [Viewport tests](../tests/test_viewport.py) · [Installation test](../tests/test_install.py)
