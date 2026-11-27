"""Run Blender with its UI: blender --factory-startup tests/fixtures/GroupManager.blend --python tests/test_viewport.py.

The script uses the real window event loop, validates native undo/redo and opens
a temporary copy. The starting file is never saved. A failed assertion exits 1.
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scena
from scena import editor, hosts

_state = {}
_tmp = tempfile.TemporaryDirectory(prefix="groupmanager-viewport-")
_file = str(Path(_tmp.name) / "viewport.blend")
_drawn = set()


def _mark_draw(panel, context):
    _drawn.add(bool(editor.path(context)) if context.window else False)


def _areas(window):
    return sorted((a for a in window.screen.areas if a.type == 'VIEW_3D'),
                  key=lambda a: a.width * a.height, reverse=True)


def _view(area):
    r = area.spaces.active.region_3d
    return tuple(round(n, 4) for n in (*r.view_location, *r.view_rotation, r.view_distance))


def _select(obj):
    layer = bpy.context.window.view_layer
    # Newly linked copies can inherit selection before their layer bases are rebuilt.
    layer.update()
    for item in list(layer.objects.selected):
        item.select_set(False, view_layer=layer)
    obj.select_set(True, view_layer=layer)
    layer.objects.active = obj


def _operator(name, **kwargs):
    op = getattr(bpy.ops.groupmanager, name)
    result = op('EXEC_DEFAULT', True, **kwargs)
    assert result == {'FINISHED'}, (name, result)


def _worlds(role):
    """World matrices of the actually evaluated parts, including all instance paths."""
    bpy.context.view_layer.update()
    results = [entry.matrix_world.copy() for entry in bpy.context.evaluated_depsgraph_get().object_instances
               if entry.object.type == 'MESH' and entry.object.original.get("qa_role") == role]
    return sorted(results, key=lambda m: tuple(round(v, 4) for v in m.translation))


def _matrix_close(left, right, tolerance=0.0002):
    assert max(abs(left[i][j] - right[i][j]) for i in range(4) for j in range(4)) < tolerance, (left, right)


def _editable(role):
    members = [o for o in bpy.context.visible_objects if o.get("qa_role") == role]
    assert len(members) == 1, [(o.name, o.type) for o in members]
    return members[0]


def _checkpoint(message):
    print("VIEWPORT", message, flush=True)


def _nested_edit():
    """Edit one placement at depth two, then check a shared geometry edit and native undo."""
    w = bpy.context.window
    stage = w.scene
    stage_name = stage.name
    bpy.ops.mesh.primitive_cube_add(size=1)
    part = bpy.context.active_object
    part.name = "Nested display panel"
    part["qa_role"] = "nested panel"
    _operator("create", group_name="Monitors", kind='NODES', pivot='ACTIVE')
    inner = bpy.context.active_object
    inner.location = (0, 3, 1)
    inner.rotation_euler.z = 0.47
    bpy.context.view_layer.update()
    _operator("create", group_name="Workstations", kind='NATIVE', pivot='ACTIVE')
    root = bpy.context.active_object
    root.name = "Workstation A"
    root.location = (-8, 0, 0)
    sibling = root.copy()
    sibling.name = "Workstation B"
    stage.collection.objects.link(sibling)
    sibling.location = (8, 1, 0)
    sibling.scale = (2.2, -1.4, 0.8)
    sibling.rotation_euler = (.2, .3, -.22)
    _select(sibling)
    initial = _worlds("nested panel")
    assert len(initial) == 2
    reference = bpy.data.objects["Surrounding reference"]
    reference_pos = reference.matrix_world.copy()
    view_before = _view(_areas(w)[0])
    scene_keys = set(bpy.data.scenes.keys())
    _operator("edit", unique=False)
    assert w.scene == stage and set(bpy.data.scenes.keys()) == scene_keys
    candidates = [o for o in bpy.context.visible_objects if hosts.definition(o) == hosts.definition(inner)]
    assert len(candidates) == 1, [o.name for o in candidates]
    _select(candidates[0])
    _operator("edit", unique=True)
    assert len(editor.path(bpy.context)) == 2 and w.scene == stage
    assert _view(_areas(w)[0]) == view_before and reference.visible_get()
    editable = _editable("nested panel")
    _matrix_close(editable.matrix_world, initial[1])
    shown = _worlds("nested panel")
    assert len(shown) == 2, f"Expected both workstations exactly once: {shown}"
    for actual, expected in zip(shown, initial):
        _matrix_close(actual, expected)
    updated = editable.matrix_world.copy()
    updated.translation += Vector((0, 0, 1.25))
    editable.matrix_world = updated
    w.view_layer.update()
    yield
    _operator("back", all=True)
    result = _worlds("nested panel")
    assert len(result) == 2
    _matrix_close(result[0], initial[0])
    _matrix_close(result[1], updated)
    _matrix_close(bpy.data.objects["Surrounding reference"].matrix_world, reference_pos)
    _checkpoint("two-level mixed hosts, selected occurrence world matrix, unique world move and visible context")

    # Use the same definition twice, edit it, and undo/redo entering shared edit mode.
    root = bpy.data.objects["Workstation A"]
    peer = root.copy()
    peer.name = "Workstation C"
    bpy.context.scene.collection.objects.link(peer)
    peer.location.x = -16
    _select(root)
    bpy.ops.ed.undo_push(message="Three workstations")
    _operator("edit", unique=False)
    assert len(editor.path(bpy.context)) == 1
    assert bpy.ops.ed.undo() == {'FINISHED'}
    assert not editor.path(bpy.context) and bpy.context.scene.name == stage_name
    assert bpy.ops.ed.redo() == {'FINISHED'}
    assert len(editor.path(bpy.context)) == 1
    opened = editor.content(hosts.definition(editor.path(bpy.context)[-1]))
    candidate = next(o for o in opened.all_objects if hosts.host_type(o) == 'NODES')
    _select(candidate)
    _operator("edit", unique=False)
    panel = _editable("nested panel")
    mesh = panel.data
    mesh.vertices[0].co.z += 0.5
    mesh.update()
    w = bpy.context.window
    w.view_layer.update()
    vertex_heights = [entry.object.data.vertices[0].co.z
                      for entry in bpy.context.evaluated_depsgraph_get().object_instances
                      if entry.object.type == 'MESH' and entry.object.original.get("qa_role") == "nested panel"]
    assert len(vertex_heights) == 3, f"Expected three panels during shared edit: {vertex_heights}"
    assert sum(abs(height - mesh.vertices[0].co.z) < 1e-5 for height in vertex_heights) == 2
    _operator("back", all=True)
    assert not editor.path(bpy.context)
    assert len(_worlds("nested panel")) == 3
    _checkpoint("Undo/Redo rebinds an in-place editing session without switching scene")


def _workflow():
    assert not bpy.app.background, "Requires a full Blender window"
    assert not bpy.context.scene.objects, "Start with a new empty project"
    bpy.context.preferences.use_preferences_save = False
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.view.smooth_view = 0
    scena.register()
    bpy.types.GROUPMANAGER_PT_current.append(_mark_draw)
    w = bpy.context.window
    spare = next(a for a in w.screen.areas if a.type == 'OUTLINER')
    spare.type = 'VIEW_3D'
    areas = _areas(w)
    assert len(areas) >= 2
    for i, area in enumerate(areas[:2]):
        area.spaces.active.region_3d.view_location = (i + 4, i - 3, 2)
        area.spaces.active.region_3d.view_distance = i + 13
    areas[0].spaces.active.show_region_ui = True
    areas[0].tag_redraw()
    yield

    window = bpy.context.window
    viewport = _areas(window)[0]
    sidebar = next(r for r in viewport.regions if r.type == 'UI')
    sidebar.active_panel_category = "Item"
    viewport.tag_redraw()
    yield
    bpy.ops.mesh.primitive_cube_add()
    part = bpy.context.window.view_layer.objects.active
    part.name = "Original part"
    part["qa_role"] = "seat"
    settings = bpy.context.scene.gm_create_settings
    settings.kind = 'NODES'
    settings.group_name = 'Chair'
    settings.pivot = 'CURSOR'
    bpy.context.scene.cursor.location = (1, 2, 3)
    result = bpy.ops.groupmanager.create('INVOKE_DEFAULT', True)
    assert result == {'FINISHED'}, f"Create should complete on the first click: {result}"
    placement = bpy.context.window.view_layer.objects.active
    assert hosts.host_type(placement) == 'NODES'
    assert hosts.definition(placement).name == 'Group_Chair'
    assert tuple(placement.matrix_world.translation) == (1, 2, 3)
    _checkpoint("one-click create used the current group type, name and pivot")
    _operator("switch_type", kind="NATIVE")
    placement = bpy.context.window.view_layer.objects.active
    placement.name = "Chair 01"
    peer = placement.copy()
    peer.name = "Chair 02"
    window.scene.collection.objects.link(peer)
    peer.location.x = 4
    window.view_layer.update()
    _select(placement)
    _operator("switch_type", kind="NODES", local=True)
    assert hosts.host_type(bpy.data.objects["Chair 01"]) == 'NODES'
    assert hosts.host_type(peer) == 'NATIVE'
    assert hosts.definition(bpy.data.objects["Chair 01"]) == hosts.definition(peer)
    _operator("realize", enabled=True)
    bpy.ops.ed.undo()
    assert not hosts.get_realize(bpy.data.objects["Chair 01"])
    bpy.ops.ed.redo()
    assert hosts.get_realize(bpy.data.objects["Chair 01"])
    _checkpoint("switch, modifier output, one-step undo and redo")
    yield

    window = bpy.context.window
    bpy.ops.mesh.primitive_cube_add(location=(7, 4, 2))
    reference = bpy.context.active_object
    reference.name = "Surrounding reference"
    reference_world = reference.matrix_world.copy()
    _select(bpy.data.objects["Chair 02"])
    bpy.ops.view3d.localview(frame_selected=False)
    areas = _areas(window)
    assert areas[0].spaces.active.local_view and not areas[1].spaces.active.local_view
    reference.local_view_set(areas[0].spaces.active, True)
    _operator("switch_type", kind="NODES")
    assert bpy.data.objects["Chair 02"].local_view_get(areas[0].spaces.active)
    _operator("switch_type", kind="NATIVE")
    assert bpy.data.objects["Chair 02"].local_view_get(areas[0].spaces.active)
    prior_views = [_view(a) for a in areas[:2]]
    original_scene = window.scene
    original_scenes = set(bpy.data.scenes.keys())
    source_world = bpy.data.objects["Original part"].matrix_world.copy()
    peer = bpy.data.objects["Chair 02"]
    peer.rotation_euler = (.16, -.3, .41)
    peer.scale = (-1.5, .8, 1.3)
    window.view_layer.update()
    definition = hosts.definition(peer)
    expected_world = peer.matrix_world.copy()
    expected_world.translation = expected_world @ -definition.instance_offset
    expected_world = expected_world @ source_world
    _operator("edit", unique=True)
    assert len(editor.path(bpy.context)) == 1
    assert window.scene == original_scene and set(bpy.data.scenes.keys()) == original_scenes
    assert hosts.definition(bpy.data.objects["Chair 01"]) != hosts.definition(bpy.data.objects["Chair 02"])
    displayed = _editable("seat")
    _matrix_close(displayed.matrix_world, expected_world)
    assert reference.visible_get(viewport=areas[0].spaces.active)
    _select(reference)
    assert bpy.context.active_object == reference
    _select(displayed)
    moved = displayed.matrix_world.copy()
    moved.translation += Vector((0, 0, 3))
    displayed.matrix_world = moved
    window.view_layer.update()
    assert [_view(a) for a in areas[:2]] == prior_views
    areas[0].tag_redraw()
    yield

    _operator("back", all=True)
    window = bpy.context.window
    assert not editor.path(bpy.context) and not any(hosts.is_edit_scene(s) for s in bpy.data.scenes)
    assert window.view_layer.objects.active.name == "Chair 02"
    areas = _areas(window)
    assert [_view(a) for a in areas[:2]] == prior_views
    assert areas[0].spaces.active.local_view and not areas[1].spaces.active.local_view
    assert bpy.data.objects["Chair 02"].local_view_get(areas[0].spaces.active)
    assert not bpy.data.objects["Chair 01"].local_view_get(areas[0].spaces.active)
    _matrix_close(reference.matrix_world, reference_world)
    _matrix_close(bpy.data.objects["Original part"].matrix_world, source_world)
    bpy.ops.view3d.localview(frame_selected=False)
    evaluated = _worlds("seat")
    assert any((m.translation - moved.translation).length < 0.0002 for m in evaluated), evaluated
    _checkpoint("in-place mirrored nonuniform edit, world-space move, visible reference and retained views")
    _operator("edit")
    assert editor.path(bpy.context)
    bpy.ops.wm.save_as_mainfile(filepath=_file)
    assert not any(hosts.is_edit_scene(s) for s in bpy.data.scenes)
    yield

    bpy.ops.wm.open_mainfile(filepath=_file)
    yield
    assert bpy.data.filepath == _file
    assert not any(hosts.is_edit_scene(s) for s in bpy.data.scenes)
    assert not editor.path(bpy.context)
    assert set(bpy.context.window.scene.objects.keys()) == {"Chair 01", "Chair 02", "Surrounding reference"}
    assert any((m.translation - moved.translation).length < 0.0002 for m in _worlds("seat"))
    _checkpoint("save during editing and reopen in the original scene")
    assert _drawn == {True, False}, f"Scena panel draw coverage: {_drawn}"
    _checkpoint("real Scena panel callbacks in both modes")
    root = bpy.data.objects["Chair 02"]
    definition_name = hosts.definition(root).name
    peer = root.copy()
    peer.name = "Chair 03"
    bpy.context.scene.collection.objects.link(peer)
    _select(root)
    bpy.ops.ed.undo_push(message="Two linked chairs")
    assert bpy.ops.groupmanager.dissolve('INVOKE_DEFAULT', True) == {'FINISHED'}
    assert hosts.definition(bpy.data.objects["Chair 03"]).name == definition_name
    assert bpy.data.objects.get("Chair 02") is None
    bpy.ops.ed.undo()
    _select(bpy.data.objects["Chair 02"])
    bpy.data.objects["Chair 03"].select_set(True)
    assert bpy.ops.groupmanager.dissolve('INVOKE_DEFAULT', True) == {'FINISHED'}
    assert all(bpy.data.objects.get(name) is None for name in ("Chair 02", "Chair 03"))
    assert hosts.definition(bpy.data.objects["Chair 01"]).name != definition_name
    bpy.ops.ed.undo()
    assert all(hosts.definition(bpy.data.objects[name]).name == definition_name
               for name in ("Chair 02", "Chair 03"))
    _checkpoint("ungroup follows the selection and undoes in one step")
    yield from _nested_edit()


def _tick():
    try:
        win = bpy.context.window_manager.windows[0]
        area = _areas(win)[0]
        region = next(r for r in area.regions if r.type == 'WINDOW')
        with bpy.context.temp_override(window=win, area=area, region=region):
            next(_state["steps"])
        return 0.5
    except StopIteration:
        _tmp.cleanup()
        _checkpoint("PASS")
        # wm.quit_blender also writes Blender's shared quit.blend recovery file.
        os._exit(0)
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        _tmp.cleanup()
        os._exit(1)
    return None


if __name__ == "__main__":
    _state["steps"] = _workflow()
    bpy.app.timers.register(_tick, first_interval=0.8, persistent=True)
