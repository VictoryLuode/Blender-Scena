"""Run in a fresh Blender UI with --enable-event-simulate --python THIS -- <directory containing hardops>."""

import os
from pathlib import Path
import sys
import traceback

import addon_utils
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scena
from scena import hosts


def arrays():
    return [mod for mod in bpy.context.active_object.modifiers if mod.type == 'ARRAY']


def hard_ops_menus():
    menus = {}
    for name in dir(bpy.types):
        if name.startswith("HOPS_MT_"):
            draw = getattr(bpy.types, name).draw
            menus[name] = tuple(getattr(draw, "_draw_funcs", (draw,)))
    return menus


def workflow(window, region):
    bpy.context.preferences.use_preferences_save = False
    sys.path.insert(0, sys.argv[sys.argv.index('--') + 1])
    assert addon_utils.enable('hardops', default_set=True)
    native_menus = hard_ops_menus()
    assert {'HOPS_MT_MainMenu', 'HOPS_MT_SelectGrouped'} <= native_menus.keys()
    scena.register()
    assert hard_ops_menus() == native_menus
    x, y = region.x + region.width // 2, region.y + region.height // 2

    def press(key):
        for value in ('PRESS', 'RELEASE'):
            window.event_simulate(type=key, value=value, x=x, y=y)

    def move(dx):
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + dx, y=y)

    def adjusting():
        return any(op.bl_idname == 'HOPS_OT_st3_array' for op in window.modal_operators)

    for kind in ('NODES', 'NATIVE'):
        bpy.ops.mesh.primitive_cube_add()
        member = bpy.context.active_object
        assert bpy.ops.groupmanager.create(group_name=kind, kind=kind) == {'FINISHED'}
        source = hosts.definition(bpy.context.active_object).name
        if kind == 'NATIVE':
            # Hard Ops shows its Empty menu for a collection instance.
            assert not bpy.ops.hops.st3_array.poll()
            assert bpy.ops.wm.call_menu('INVOKE_DEFAULT', name="HOPS_MT_MainMenu") == {'INTERFACE'}
            yield
            press('ESC')
            yield
            assert hosts.host_type(bpy.context.active_object) == 'NATIVE'
            assert bpy.ops.groupmanager.switch_type(kind='NODES') == {'FINISHED'}
            hosts.set_realize(bpy.context.active_object, True)
        assert bpy.ops.ed.undo_push(message="Before Array") == {'FINISHED'}
        move(0)
        yield
        # Array V2 is the third native entry for these mesh placements in Hard Ops 26.1.7.
        assert bpy.ops.wm.call_menu('INVOKE_DEFAULT', name="HOPS_MT_MainMenu") == {'INTERFACE'}
        yield
        for key in ('HOME', 'DOWN_ARROW', 'DOWN_ARROW', 'RET'):
            press(key)
            yield
        assert adjusting() and len(arrays()) == 1
        count = arrays()[0].count
        press('WHEELUPMOUSE')
        yield
        assert arrays()[0].count == count + 1
        # In native 3D adjustment mode the spacing follows absolute pointer positions.
        press('V')
        yield
        for dx in (2, 85, 125):
            move(dx)
            yield
        spacing = tuple(arrays()[0].constant_offset_displace)
        move(145)
        yield
        assert tuple(arrays()[0].constant_offset_displace) != spacing
        assert adjusting() and hosts.definition(bpy.context.active_object).name == source

        if kind == 'NODES':
            assert not hosts.get_realize(bpy.context.active_object)
            graph = bpy.context.evaluated_depsgraph_get()
            positions = {tuple(item.matrix_world.translation) for item in graph.object_instances
                         if item.is_instance and item.show_self and item.object.original == member}
            assert len(positions) == count + 1, positions
            press('LEFTMOUSE')
            yield
            assert not adjusting() and len(arrays()) == 1 and arrays()[0].count == count + 1
            assert bpy.ops.ed.undo() == {'FINISHED'}
            yield
            assert not arrays() and not hosts.get_realize(bpy.context.active_object)
            assert bpy.ops.ed.redo() == {'FINISHED'}
            yield
            assert len(arrays()) == 1 and arrays()[0].count == count + 1
        else:
            press('ESC')
            yield
            assert not adjusting() and not arrays()
    assert hard_ops_menus() == native_menus
    scena.unregister()
    assert hard_ops_menus() == native_menus
    scena.register()
    assert hard_ops_menus() == native_menus
    print("PASS native Hard Ops menus, first-click Array V2, live geometry, confirm, cancel and undo/redo", flush=True)


def run():
    window = bpy.context.window_manager.windows[0]
    area = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
    region = next(region for region in area.regions if region.type == 'WINDOW')
    steps = workflow(window, region)

    def tick():
        try:
            with bpy.context.temp_override(window=window, area=area, region=region):
                next(steps)
        except StopIteration:
            os._exit(0)
        except BaseException:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)
        return 0.35

    bpy.app.timers.register(tick, first_interval=1.0)


if __name__ == "__main__":
    run()
