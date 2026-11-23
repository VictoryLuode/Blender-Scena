"""Run through test_install.py --preferences-ui, using a temporary installed extension."""

import importlib
import os
from pathlib import Path
import sys
import traceback

import bpy


MODULE = "bl_ext.user_default.scena"


def binding(context):
    keymap = context.window_manager.keyconfigs.user.keymaps["Object Mode"]
    [item] = [item for item in keymap.keymap_items if item.idname == "wm.call_menu_pie"
              and item.properties.name == "GROUPMANAGER_MT_quick"]
    return item


def run():
    stage, scene_file = sys.argv[sys.argv.index("--") + 1:]
    # This test changes and saves preferences. Require the profile created by the runner.
    profile = Path(os.environ["BLENDER_USER_RESOURCES"])
    assert Path(scene_file).parent.samefile(profile)
    assert Path(bpy.utils.user_resource("CONFIG")).samefile(profile / "config")
    prefs_mod = importlib.import_module(f"{MODULE}.preferences")
    creation = importlib.import_module(f"{MODULE}.creation")
    hosts = importlib.import_module(f"{MODULE}.hosts")
    context = bpy.context
    prefs = prefs_mod.get(context)
    settings = creation.current_settings(context)
    if stage == "restore":
        assert prefs.default_kind == "NODES" and prefs.default_include_children
        assert prefs.default_pivot == "CURSOR" and prefs.default_orientation == "CURSOR"
        assert not prefs.show_edit_bounds and not prefs.show_edit_header and prefs.bounds_width == 2
        assert abs(prefs.bounds_color[1] - 0.3) < 0.001
        assert (binding(context).type, binding(context).ctrl) == ("F8", 1)
        assert settings.kind == "NATIVE" and settings.pivot == "CURSOR"
        assert bpy.ops.groupmanager.reset_create_defaults() == {"FINISHED"}
        assert settings.kind == "NODES" and settings.group_name == "Keep this name"
        print("PASS: restored global defaults, viewport choices, shortcut and scene overrides", flush=True)
        return

    completed_draws = []
    original = type(prefs).draw

    def draw(instance, draw_context):
        original(instance, draw_context)
        completed_draws.append(draw_context.area.type)

    type(prefs).draw = draw
    win = context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    with context.temp_override(window=win, area=area, region=region):
        assert bpy.ops.groupmanager.open_preferences() == {"FINISHED"}
    for _ in range(16):
        if completed_draws:
            break
        yield
    assert "PREFERENCES" in completed_draws, "The add-on preferences did not draw"
    assert context.preferences.active_section == "ADDONS"
    out = os.getenv("GROUPMANAGER_PREFERENCES_SCREENSHOT")
    if out:
        prefs_win = next(w for w in context.window_manager.windows
                         if any(a.type == "PREFERENCES" for a in w.screen.areas))
        prefs_area = next(a for a in prefs_win.screen.areas if a.type == "PREFERENCES")
        prefs_region = next(r for r in prefs_area.regions if r.type == "WINDOW")
        with context.temp_override(window=prefs_win, area=prefs_area, region=prefs_region):
            bpy.ops.view2d.scroll_down(page=True)
        yield
        with context.temp_override(window=prefs_win):
            bpy.ops.screen.screenshot(filepath=out)

    prefs.default_kind, prefs.default_pivot = "NODES", "CURSOR"
    prefs.default_orientation, prefs.default_include_children = "CURSOR", True
    prefs.show_edit_bounds, prefs.show_edit_header = False, False
    prefs.bounds_color, prefs.bounds_width = (0.2, 0.3, 0.4), 2
    assert (settings.kind, settings.pivot, settings.orientation, settings.include_children) == (
        "NODES", "CURSOR", "CURSOR", True)
    with context.temp_override(window=win, area=area, region=region):
        context.scene.cursor.location = (4, 7, 2)
        bpy.ops.mesh.primitive_cube_add()
        assert bpy.ops.groupmanager.create("INVOKE_DEFAULT") == {"FINISHED"}
        host = context.active_object
        assert hosts.host_type(host) == "NODES" and tuple(host.location) == (4, 7, 2)
        settings.kind, settings.group_name = "NATIVE", "Keep this name"
        bpy.ops.mesh.primitive_cube_add()
        assert bpy.ops.groupmanager.create() == {"FINISHED"}
        assert hosts.host_type(context.active_object) == "NATIVE"
        bpy.ops.wm.save_as_mainfile(filepath=scene_file)

    item = binding(context)
    assert item.type == "E" and item.active
    item.type, item.ctrl = "F8", True
    context.window_manager.keyconfigs.update()
    bpy.ops.wm.save_userpref()
    print("PASS: real preferences draw, one-click defaults and scene overrides", flush=True)


def tick():
    try:
        next(steps)
    except StopIteration:
        os._exit(0)
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return 0.4


if __name__ == "__main__":
    steps = run()
    bpy.app.timers.register(tick, first_interval=1.5)
