# Scena 0.3.8

- Array and Mirror now build their Geometry Nodes changes on a copy of the placement's node tree. Adding an array to a nested group preserves the node tree and settings used by its linked copies.
- With **Convert to Mesh** enabled, Blender applies a new Array directly to the evaluated mesh. Hard Ops users can adjust count and spacing the first time they open **Array V2** on a Geometry Nodes group.
- Scena's group commands integrate with Blender's built-in menus. Other add-ons retain control of their menus and shortcuts.
- The 3D Viewport sidebar brings the group definition controls together under **Item → Object**. Use the collection icon to switch definitions and the user count to make a placement unique.

Verified in Blender 5.3 with 88 workflow tests, two nested-edit tests, and interactive checks for Array adjustment, confirmation, cancellation, undo, and redo.

Requires Blender 5.3.
