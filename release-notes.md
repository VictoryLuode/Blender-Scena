# Scena 0.3.3

- Pick array and mirror copies in the viewport. Alt+E highlights visible modifier surfaces, and Ctrl-double-click enters their group. This works with mesh output and linked instances inside nested groups.
- Use **Modifier Preview** in the **Scena Group** modifier to show generated copies while editing. Turning it on during an edit builds the preview on demand. **Frame with Modifiers** includes the evaluated result in the group outline.
- Viewport frames reuse evaluated vertices and screen projections when unrelated objects move.
- Exported placements keep their own IDs when copied, hidden, exported individually, and reopened. Linked placements still share their model definition.
- The release builder checks the archive against the source and verifies installation and preferences across Blender restarts before producing the ZIP.

Requires Blender 5.3.
