"""Run a Blender UI with --factory-startup --enable-event-simulate --python tests/test_pie_key.py."""
import os
from pathlib import Path
import sys
import traceback

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scena

draws = []


def menu_draw(menu, context):
    draws.append(context.mode)


def bindings():
    keymap = bpy.context.window_manager.keyconfigs.addon.keymaps.get("Object Mode")
    return [item for item in keymap.keymap_items if item.idname == "wm.call_menu_pie"
            and item.properties.name == "GROUPMANAGER_MT_quick"] if keymap else []


def run():
    scena.register()
    group_menu = bpy.types.GROUPMANAGER_MT_quick
    group_menu.append(menu_draw)
    assert len(bindings()) == 1
    context = bpy.context
    win = context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    x, y = region.x + region.width // 2, region.y + region.height // 2
    with context.temp_override(window=win, area=area, region=region):
        bpy.ops.mesh.primitive_cube_add()

    def key(name):
        for value in ('PRESS', 'RELEASE'):
            win.event_simulate(type=name, value=value, x=x, y=y)

    key('ESC')
    win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
    yield
    toolbar = area.spaces.active.show_region_toolbar
    key('E')
    yield
    assert draws and set(draws) == {'OBJECT'}, f"Object mode E failed: {draws}, toolbar {toolbar} -> {area.spaces.active.show_region_toolbar}"
    assert area.spaces.active.show_region_toolbar == toolbar
    key('ESC')
    yield
    count = len(draws)
    key('E')
    yield
    assert len(draws) > count, "E did not reopen the menu after Esc"
    key('ESC')
    yield
    with context.temp_override(window=win, area=area, region=region):
        if context.active_object is None or context.active_object.type != 'MESH':
            bpy.ops.mesh.primitive_cube_add()
        bpy.ops.object.mode_set(mode='EDIT')
    yield
    count = len(draws)
    key('E')
    yield
    assert len(draws) == count, "E opened the group menu in Edit Mode"
    key('ESC')
    yield
    toolbar = area.spaces.active.show_region_toolbar
    key('T')
    yield
    assert len(draws) == count, "T opened the group menu in Edit Mode"
    assert area.spaces.active.show_region_toolbar != toolbar, "Edit Mode toolbar toggle failed"
    with context.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='OBJECT')
    group_menu.remove(menu_draw)
    scena.unregister()
    assert not bindings(), "Shortcut persisted after unregister"
    scena.register()
    assert len(bindings()) == 1, "Re-enable duplicated shortcut"
    scena.unregister()
    print("PASS: E opened/reopened the pie; Esc cancelled; Edit Mode kept E and the toolbar; clean keymap lifecycle", flush=True)


def tick():
    try:
        next(steps)
    except StopIteration:
        os._exit(0)
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return 0.45


if __name__ == "__main__":
    steps = run()
    bpy.app.timers.register(tick, first_interval=1.5)
