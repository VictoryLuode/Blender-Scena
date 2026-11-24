# Scena

Build a room with linked groups in Blender. Add a brace to one desk frame and every linked desk gets the same brace. Each workstation keeps its own monitor and chair.

[Watch the 1080p proof-room walkthrough](docs/media/proof-room.mp4) · [User guide](docs/user-guide.md) · [Published releases](https://github.com/VictoryLuode/Blender-Scena/releases)

## Install

In Blender 5.3, open **Edit → Preferences → Get Extensions**. Choose **Install from Disk…** from the dropdown and select the Scena extension ZIP.

## Quick start

1. Select the desktop and legs. Press **E** and choose **Create Group**.
2. Select the desk, choose **Operations → Duplicate Link** or press **Alt+D**, then move the copy into place.
3. Select a brace and the original desk group. Use **Add** to fit the same brace to each linked desk.

For a separately editable copy, use **Operations → Duplicate Group**. See the [sharing rules](docs/user-guide.md#duplicate-and-make-variants) before editing member meshes.

## License and credits

Scena uses the [GNU General Public License v3.0 or later](scena/LICENSE). Its bundled icon font uses artwork derived from [Blender's interface icons](https://github.com/blender/blender/tree/02f0899d78e3/release/datafiles/icons_svg).
