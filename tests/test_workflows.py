"""Run with Blender 5.3: blender -b --factory-startup --python-exit-code 1 --python tests/test_workflows.py"""

import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy
from mathutils import Matrix, Vector


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tests" / "fixtures" / "GroupManager.blend"
sys.path.insert(0, str(ROOT))

import scena
from scena import branches, editor, edit_layer, hosts, picking, preferences, usage


def select(objects):
    objects = list(objects)
    for item in bpy.context.selected_objects:
        item.select_set(False)
    for item in objects:
        item.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def flat_part(name, location=(0, 0, 0)):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj["qa_role"] = name
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    bpy.context.view_layer.update()
    return obj


def create_group(objects, name, kind="NATIVE", **options):
    select(objects)
    status = bpy.ops.groupmanager.create(group_name=name, kind=kind, **options)
    assert status == {"FINISHED"}, (name, status)
    group = bpy.context.active_object
    assert hosts.definition(group).name.startswith("Group_"), hosts.definition(group).name
    return group


def linked_copy(source, x):
    duplicate = source.copy()
    bpy.context.scene.collection.objects.link(duplicate)
    duplicate.location.x = x
    return duplicate


def placements(source, count):
    rows = [source]
    rows.extend(linked_copy(source, n * 5) for n in range(1, count))
    bpy.context.view_layer.update()
    return rows


def count_uses(collection):
    return usage.counts(collection).get(bpy.context.scene, 0)


def eval_faces(obj):
    bpy.context.view_layer.update()
    return len(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.polygons)


def realized_parts():
    bpy.context.view_layer.update()
    counts = Counter()
    for instance in bpy.context.evaluated_depsgraph_get().object_instances:
        if instance.is_instance:
            role = instance.object.original.get("qa_role")
            if role:
                counts[(role, round(instance.matrix_world.translation.z, 3))] += 1
    return counts


def visible_part_locations(role):
    bpy.context.view_layer.update()
    positions = []
    for instance in bpy.context.evaluated_depsgraph_get().object_instances:
        if instance.show_self and instance.object.original.get("qa_role") == role:
            positions.append(tuple(round(v, 4) for v in instance.matrix_world.translation))
    return sorted(positions)


def nested_workstations(count=21, second_monitor=False):
    monitor = create_group([flat_part("screen")], "Monitor")
    keyboard = create_group([flat_part("keys")], "Keyboard")
    members = [monitor, keyboard]
    if second_monitor:
        members.append(linked_copy(monitor, 2))
    desk = create_group(members, "Workstation")
    return placements(desk, count), monitor, keyboard


def peak_memory_mib():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                ("peak_paged", ctypes.c_size_t), ("paged", ctypes.c_size_t),
                ("peak_nonpaged", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
        memory_info.restype = wintypes.BOOL
        ok = memory_info(current_process(), ctypes.byref(counters), counters.cb)
        return round(counters.peak_working_set / 2**20, 1) if ok else None
    return None


class GroupWorkflows(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.open_mainfile(filepath=str(TEMPLATE))

    def assertMatrixClose(self, actual, expected, precision=5):
        for actual_row, expected_row in zip(actual, expected):
            for a, b in zip(actual_row, expected_row):
                self.assertAlmostEqual(a, b, places=precision)

    def assertVectorClose(self, actual, expected, precision=4):
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, places=precision)

    def test_2d_fit_uses_evaluated_mesh_for_plain_group_member(self):
        bpy.ops.mesh.primitive_monkey_add(location=(0.3, -0.1, 0.2))
        member = bpy.context.active_object
        member.modifiers.new('Subdivide', 'SUBSURF').levels = 3
        member.modifiers.new('Displace', 'DISPLACE').strength = 0.4
        host = create_group([member], 'Suzanne group', pivot='CURSOR')
        host.rotation_euler = (0.2, 0.1, 0.5)
        editor.open_path(bpy.context, [host])
        evaluated = member.evaluated_get(bpy.context.evaluated_depsgraph_get())
        self.assertGreater(len(evaluated.data.vertices), 8192)
        region = SimpleNamespace(width=1000, height=800)
        projection = Matrix.Rotation(0.6, 4, 'X') @ Matrix.Rotation(-0.4, 4, 'Z')
        projection[3] = (0.04, -0.08, 0.12, 4)
        view = SimpleNamespace(perspective_matrix=projection)
        # The picked occurrence of a linked member can have its own world matrix.
        for placement in (None, Matrix.Translation((1.2, -0.5, 0)) @ evaluated.matrix_world):
            world = evaluated.matrix_world if placement is None else placement
            matrix = projection @ world
            clips = [matrix @ vertex.co.to_4d() for vertex in evaluated.data.vertices]
            xs = [(1 + p.x / p.w) * region.width / 2 for p in clips]
            ys = [(1 + p.y / p.w) * region.height / 2 for p in clips]
            measured = editor._extent_screen_limits(editor._frame_extent_points(member, placement), region, view)
            self.assertVectorClose(measured, (min(xs), min(ys), max(xs), max(ys)), precision=3)
        editor.back(bpy.context, all_levels=True)

    def test_modifier_frame_follows_each_placement_and_survives_reopen(self):
        member = flat_part('Panel', (3, 0, 0))
        group = create_group([member], 'Panel array', 'NODES', pivot='CURSOR')
        hosts.set_realize(group, True)
        array = group.modifiers.new('Repeat', 'ARRAY')
        array.use_relative_offset = False
        array.use_constant_offset = True
        array.constant_offset_displace = (0, 4, 0)
        array.count = 4
        group.modifiers.new('Reflect', 'MIRROR')
        peer = linked_copy(group, 20)
        bpy.context.view_layer.update()
        self.assertIsNone(editor._modifier_frame(group))
        option = hosts._socket(hosts._source_modifier(group), hosts.FRAME_SOCKET)
        option.value = True
        bpy.context.view_layer.update()
        self.assertIsNone(editor._modifier_frame(peer))
        corners, _ = editor._modifier_frame(group)
        self.assertVectorClose(corners[0], (-4, -1, 0))
        self.assertVectorClose(corners[-1], (4, 13, 0))
        array.count = 2
        bpy.context.view_layer.update()
        self.assertVectorClose(editor._modifier_frame(group)[0][-1], (4, 5, 0))
        hosts.display_input(group, hosts.PREVIEW_SOCKET).value = False
        editor.open_path(bpy.context, [group])
        self.assertVectorClose(editor._modifier_frame(group)[0][-1], (4, 5, 0))
        editor.back(bpy.context, all_levels=True)
        group_name, peer_name = group.name, peer.name
        with tempfile.TemporaryDirectory() as folder:
            filepath = str(Path(folder) / "Frame.blend")
            bpy.ops.wm.save_as_mainfile(filepath=filepath)
            bpy.ops.wm.open_mainfile(filepath=filepath)
            group, peer = bpy.data.objects[group_name], bpy.data.objects[peer_name]
            self.assertIsNone(editor._modifier_frame(peer))
            self.assertVectorClose(editor._modifier_frame(group)[0][-1], (4, 5, 0))

    def test_modifier_frame_measures_unrealized_nested_instances(self):
        from scena import instance_array
        member = flat_part('Panel', (2, 1, 0))
        inner = create_group([member], 'Nested', 'NATIVE', pivot='CURSOR')
        inner.rotation_euler.z = 0.45
        group = create_group([inner], 'Nested array', 'NODES', pivot='CURSOR')
        array = group.modifiers.new('Repeat', 'ARRAY')
        array.use_relative_offset = False
        array.use_constant_offset = True
        array.constant_offset_displace = (0, 5, 0)
        array.count = 3
        instance_array.sync(group)
        hosts._socket(hosts._source_modifier(group), hosts.FRAME_SOCKET).value = True
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        self.assertEqual(len(group.evaluated_get(graph).data.vertices), 0)
        points = [instance.matrix_world @ v.co for instance in graph.object_instances
                  if instance.show_self and instance.object.original == member for v in instance.object.data.vertices]
        self.assertEqual(len(points), 12)
        corners, blocks = editor._modifier_frame(group)
        for corner, bound in [(corners[0], min), (corners[-1], max)]:
            self.assertVectorClose(corner, [bound(axis) for axis in zip(*points)])
        region = SimpleNamespace(width=900, height=700)
        projection = Matrix.Rotation(0.42, 4, 'Z')
        projection[3] = (0.02, 0.03, 0.04, 3.5)
        view = SimpleNamespace(perspective_matrix=projection)
        clips = [projection @ p.to_4d() for p in points]
        xs = [(1 + p.x / p.w) * region.width / 2 for p in clips]
        ys = [(1 + p.y / p.w) * region.height / 2 for p in clips]
        self.assertVectorClose(editor._extent_screen_limits(blocks, region, view), (min(xs), min(ys), max(xs), max(ys)), precision=3)

    def test_imported_older_group_gains_display_settings_once(self):
        group = create_group([flat_part('Panel')], 'Legacy', 'NODES')
        mod = hosts._source_modifier(group)
        tree = mod.node_group
        for key, output in ((hosts.PREVIEW_SOCKET, "Preview Setting"), (hosts.FRAME_SOCKET, "Frame Setting")):
            tree.interface.remove(tree.interface.items_tree[tree[key]])
            tree.interface.remove(tree.interface.items_tree[output])
            del tree[key]
        source_name = hosts.definition(group).name
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "Legacy.blend")
            bpy.data.libraries.write(path, {group})
            for previous in (True, False):
                bpy.ops.wm.open_mainfile(filepath=str(TEMPLATE))
                preferences.preview_settings(bpy.context).enabled = previous
                with bpy.data.libraries.load(path) as (src, dst):
                    dst.objects = ['Legacy']
                imported = dst.objects[0]
                tree = hosts._source_modifier(imported).node_group
                identifiers = [tree[key] for key in (hosts.REALIZE_SOCKET, hosts.FRAME_SOCKET, hosts.PREVIEW_SOCKET)]
                positions = [tree.interface.items_tree[key].position for key in identifiers]
                self.assertEqual(positions, list(range(positions[0], positions[0] + 3)))
                self.assertFalse(hosts.frame_includes_modifiers(imported))
                preview = hosts.display_input(imported, hosts.PREVIEW_SOCKET)
                self.assertEqual(preview.value, previous)
                preview.value = not previous
                hosts._name_graph(tree)
                self.assertEqual(tree[hosts.PREVIEW_SOCKET], identifiers[-1])
                self.assertEqual(preview.value, not previous)
                self.assertEqual(hosts.definition(imported).name, source_name)

    def test_preview_switch_is_independent_for_simultaneously_edited_linked_placements(self):
        member = flat_part('Editable panel')
        first = create_group([member], 'Array preview', 'NODES')
        hosts.set_realize(first, True)
        array = first.modifiers.new('Three panels', 'ARRAY')
        array.count = 3
        array.relative_offset_displace = (2, 0, 0)
        second = linked_copy(first, 20)
        bpy.context.view_layer.update()
        a = hosts.display_input(first, hosts.PREVIEW_SOCKET)
        b = hosts.display_input(second, hosts.PREVIEW_SOCKET)
        a.value, b.value = True, False
        hosts.display_input(first, hosts.FRAME_SOCKET).value = True
        editor.open_path(bpy.context, [first])
        editor.open_peer(bpy.context, second)
        shell = hosts.definition(first)[edit_layer._SHELL]
        def previews():
            bpy.context.view_layer.update()
            return {obj[edit_layer._MODIFIER_PREVIEW]: obj for obj in shell.objects if obj.get(edit_layer._MODIFIER_PREVIEW)}

        self.assertEqual(set(previews()), {first})
        self.assertEqual(eval_faces(previews()[first]), 2)
        a.value, b.value = False, True
        current = previews()
        self.assertEqual((eval_faces(current[first]), eval_faces(current[second])), (0, 2))
        self.assertTrue(member.visible_get())
        self.assertTrue(hosts.frame_includes_modifiers(first))
        self.assertFalse(hosts.frame_includes_modifiers(second))
        editor.back(bpy.context, all_levels=True)
        names = first.name, second.name
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "Preview.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)
            self.assertEqual([hosts.display_input(bpy.data.objects[n], hosts.PREVIEW_SOCKET).value for n in names],
                             [False, True])

    def test_modifier_preview_keeps_editable_member_and_offset_beveled_copies(self):
        bpy.ops.mesh.primitive_cube_add()
        member = bpy.context.active_object
        host = create_group([member], 'Beveled array', 'NODES', pivot='CURSOR')
        hosts.set_realize(host, True)
        array = host.modifiers.new('Array', 'ARRAY')
        array.use_relative_offset = False
        array.use_constant_offset = True
        array.constant_offset_displace = (0.75, 0, 0)
        bevel = host.modifiers.new('Bevel', 'BEVEL')
        bevel.width, bevel.segments = 0.12, 3
        array.count = 1
        faces_per_copy = eval_faces(host)
        array.count = 4
        wm = bpy.context.window_manager
        wm.gm_modifier_preview_mode = 'SOLID'
        editor.open_path(bpy.context, [host])
        shell = hosts.definition(host)[edit_layer._SHELL]
        preview = next(obj for obj in shell.objects if obj.get(edit_layer._MODIFIER_PREVIEW) == host)
        settings = preferences.preview_settings(bpy.context)
        option = hosts.display_input(host, hosts.PREVIEW_SOCKET)
        option.value = False
        self.assertEqual(eval_faces(preview), 0)
        self.assertEqual(settings.display, 'SOLID')
        settings.display = 'WIRE'
        self.assertEqual(eval_faces(preview), 0)
        option.value = True
        self.assertEqual(preview.display_type, 'WIRE')
        self.assertEqual(eval_faces(preview), 3 * faces_per_copy)
        for mode, copies in [('SOLID', 3), ('WIRE', 3), ('OFF', 0), ('SOLID', 3)]:
            wm.gm_modifier_preview_mode = mode
            self.assertEqual(eval_faces(preview), copies * faces_per_copy)
            self.assertTrue(member.visible_get() and member.select_get())
            self.assertEqual(bpy.context.active_object, member)
        array.constant_offset_displace = (0, 0, 0)
        self.assertEqual(eval_faces(preview), 0)
        editor.back(bpy.context, all_levels=True)
        self.assertEqual(eval_faces(host), 4 * faces_per_copy)
        self.assertFalse(any(obj.get(edit_layer._MODIFIER_PREVIEW) for obj in bpy.data.objects))

    def test_modifier_preview_first_enabled_during_edit(self):
        member = flat_part("Editable panel")
        host = create_group([member], "Late array", 'NODES')
        hosts.set_realize(host, True)
        array = host.modifiers.new("Repeat", 'ARRAY')
        array.count = 4
        array.relative_offset_displace = (2, 0, 0)
        array.show_viewport = False
        option = hosts.display_input(host, hosts.PREVIEW_SOCKET)
        option.value = False

        def previews():
            bpy.context.view_layer.update()
            return [obj for obj in bpy.data.objects if obj.get(edit_layer._MODIFIER_PREVIEW) == host]

        for _ in range(2):
            editor.open_path(bpy.context, [host])
            self.assertEqual(previews(), [])
            array.show_viewport = True
            option.value = True
            preview, = previews()
            self.assertEqual(eval_faces(preview), 3)
            self.assertTrue(member.visible_get())
            editor.back(bpy.context, all_levels=True)
            self.assertEqual(previews(), [])
            option.value = False

    def test_frame_cache_reuses_vertices_and_tracks_source_edits(self):
        member = flat_part("Frame member")
        host = create_group([member], "Framed", 'NODES', pivot='CURSOR')
        hosts.set_realize(host, True)
        hosts.display_input(host, hosts.FRAME_SOCKET).value = True
        other = flat_part("Unrelated", (20, 0, 0))
        def frame():
            bpy.context.view_layer.update()
            corners, parts = editor._modifier_frame(host)
            return corners[-1].x, parts[0][0][0]

        edge, points = frame()
        other.location.x += 2
        self.assertIs(frame()[1], points)
        host.location.x += 3
        moved, after_move = frame()
        self.assertAlmostEqual(moved, edge + 3)
        self.assertIs(after_move, points)
        member.data.vertices[1].co.x += 1
        member.data.update()
        edited, after_edit = frame()
        self.assertAlmostEqual(edited, moved + 1)
        self.assertIsNot(after_edit, points)

    def test_pick_generated_copies_inside_nested_group(self):
        from scena import instance_array
        member = flat_part("Pick source")
        repeated = create_group([member], "Four panels", 'NODES', pivot='CURSOR')
        array = repeated.modifiers.new('Repeat', 'ARRAY')
        array.count = 4
        array.use_relative_offset = False
        array.use_constant_offset = True
        array.constant_offset_displace = (4, 0, 0)
        outer = create_group([repeated], 'Outer', 'NATIVE', pivot='CURSOR')
        outer.location.x = -5
        # Verify both broad phases, including the one used in large scenes.
        visible = [outer, *(linked_copy(outer, 100 + n * 20) for n in range(64))]
        for realize in (False, True):
            hosts.set_realize(repeated, realize)
            instance_array.sync(repeated)
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            index = picking._PickIndex(visible, graph)
            index.prepare_hover()
            for i in range(4):
                groups, members = index.cast(Vector((-5 + 4*i, 0, 10)), Vector((0, 0, -1)))
                self.assertEqual({h.path for h in groups}, {(outer,), (outer, repeated)})
                self.assertEqual(members[0].path, (outer, repeated))
                self.assertEqual(members[0].obj, repeated if i else member)
            self.assertEqual(index.cast(Vector((-3, 0, 10)), Vector((0, 0, -1))), ([], []))

        front = flat_part("Front", (3, 0, 2))
        rear = flat_part("Rear", (3, 0, 0))
        mirror = create_group([front, rear], "Mirrored panels", 'NODES', pivot='CURSOR')
        hosts.set_realize(mirror, True)
        mirror.modifiers.new("Mirror panels", 'MIRROR')
        bpy.context.view_layer.update()
        index = picking._PickIndex([mirror], bpy.context.evaluated_depsgraph_get())
        _, originals = index.cast(Vector((3, 0, 10)), Vector((0, 0, -1)))
        self.assertEqual([h.obj for h in originals], [front, rear])
        _, reflected = index.cast(Vector((-3, 0, 10)), Vector((0, 0, -1)))
        self.assertEqual([h.obj for h in reflected], [mirror])

    def test_modifier_preview_keeps_welded_mirror_across_editable_seam(self):
        member = flat_part('Half', (1, 0, 0))
        host = create_group([member], 'Mirrored', 'NODES', pivot='CURSOR')
        hosts.set_realize(host, True)
        mirror = host.modifiers.new('Mirror', 'MIRROR')
        mirror.use_clip = True
        mirror.use_mirror_merge = True
        self.assertEqual(eval_faces(host), 2)
        bpy.context.window_manager.gm_modifier_preview_mode = 'SOLID'
        editor.open_path(bpy.context, [host])
        shell = hosts.definition(host)[edit_layer._SHELL]
        preview = next(obj for obj in shell.objects if obj.get(edit_layer._MODIFIER_PREVIEW) == host)
        self.assertEqual(eval_faces(preview), 1)
        result = preview.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        self.assertLess(result.polygons[0].center.x, 0)
        self.assertTrue(member.visible_get() and member.select_get())
        editor.back(bpy.context, all_levels=True)
        self.assertEqual(eval_faces(host), 2)

    def test_reopened_scene_caches_vertex_edits_and_detects_retarget(self):
        part = flat_part('shared mesh')
        host = create_group([part], 'shared')
        peer = linked_copy(host, 5)
        other = create_group([flat_part('other mesh')], 'other')
        source, target = hosts.definition(host), hosts.definition(other)
        self.assertEqual(count_uses(source), 2)
        index = usage._index
        part.data.vertices[0].co.z += 1
        part.data.update()
        bpy.context.view_layer.update()
        self.assertEqual(count_uses(source), 2)
        self.assertIs(usage._index, index)
        peer.instance_collection = target
        bpy.context.view_layer.update()
        self.assertEqual(count_uses(source), 1)
        self.assertEqual(count_uses(target), 2)

    def test_origin_to_bottom_uses_lowest_geometry_and_keeps_it_in_place(self):
        mesh = bpy.data.meshes.new("block")
        mesh.from_pydata(
            [(-1, -1, 2), (1, -1, 2), (1, 1, 2), (-1, 1, 2),
             (-1, -1, 4), (1, -1, 4), (1, 1, 4), (-1, 1, 4)],
            [], [])
        mesh.update()
        part = bpy.data.objects.new("block", mesh)
        part["qa_role"] = "block"
        bpy.context.scene.collection.objects.link(part)
        group = create_group([part], "Raised")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.origin_to_bottom(), {"FINISHED"})
        self.assertAlmostEqual(group.matrix_world.translation.z, 2, places=4)
        from scena import modeling
        low, high = modeling._source_bounds(hosts.definition(group))
        self.assertAlmostEqual(low.z, 0, places=4)
        self.assertAlmostEqual((low.x + high.x) * 0.5, 0, places=4)
        self.assertAlmostEqual((low.y + high.y) * 0.5, 0, places=4)
        self.assertEqual(visible_part_locations("block"), [(0, 0, 0)])

    def test_cursor_list_origin_uses_group_center(self):
        mesh = bpy.data.meshes.new("block")
        mesh.from_pydata(
            [(-1, -1, 2), (1, -1, 2), (1, 1, 2), (-1, 1, 2),
             (-1, -1, 4), (1, -1, 4), (1, 1, 4), (-1, 1, 4)],
            [], [])
        mesh.update()
        part = bpy.data.objects.new("block", mesh)
        part["qa_role"] = "block"
        bpy.context.scene.collection.objects.link(part)
        group = create_group([part], "Raised")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.set_origin(mode="GEOMETRY"), {"FINISHED"})
        self.assertAlmostEqual(group.matrix_world.translation.z, 3, places=4)
        self.assertEqual(visible_part_locations("block"), [(0, 0, 0)])

    def test_origin_to_bottom_keeps_mirror_remesh_and_child(self):
        mesh = bpy.data.meshes.new("block")
        mesh.from_pydata(
            [(1, -1, 2), (3, -1, 2), (3, 1, 2), (1, 1, 2),
             (1, -1, 4), (3, -1, 4), (3, 1, 4), (1, 1, 4)],
            [],
            [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
        )
        mesh.update()
        part = bpy.data.objects.new("block", mesh)
        part["qa_role"] = "block"
        bpy.context.scene.collection.objects.link(part)
        group = create_group([part], "Raised", "NODES")
        hosts.set_realize(group, True)
        mirror = group.modifiers.new("Mirror", 'MIRROR')
        mirror.use_axis[0] = True
        remesh = group.modifiers.new("Remesh", 'REMESH')
        remesh.mode = 'VOXEL'
        remesh.voxel_size = 0.25
        child = bpy.data.objects.new("marker", None)
        bpy.context.scene.collection.objects.link(child)
        child.parent = group
        child.matrix_world.translation = Vector((5, 6, 7))
        bpy.context.view_layer.update()

        def bounds(obj):
            bpy.context.view_layer.update()
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            mesh = evaluated.to_mesh()
            points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
            evaluated.to_mesh_clear()
            low = Vector(min(point[axis] for point in points) for axis in range(3))
            high = Vector(max(point[axis] for point in points) for axis in range(3))
            return low, high

        before = bounds(group)
        child_before = child.matrix_world.translation.copy()
        select([group])
        self.assertEqual(bpy.ops.groupmanager.origin_to_bottom(), {"FINISHED"})
        self.assertAlmostEqual(group.matrix_world.translation.z, 2, places=3)
        self.assertIsNotNone(mirror.mirror_object)
        after = bounds(group)
        for axis in range(3):
            self.assertAlmostEqual(before[0][axis], after[0][axis], places=1)
            self.assertAlmostEqual(before[1][axis], after[1][axis], places=1)
        self.assertAlmostEqual((child.matrix_world.translation - child_before).length, 0, places=4)

    def test_both_kinds_create_with_world_pose(self):
        for kind in ("NATIVE", "NODES"):
            with self.subTest(kind=kind):
                part = flat_part("tilted_" + kind, (3, -4, 1))
                child = flat_part("child_" + kind, (2, 0, 1))
                child.parent = part
                part.rotation_euler = (0.2, -0.3, 0.65)
                part.scale = (1.5, 0.8, 2.0)
                bpy.context.view_layer.update()
                matrices = {o: o.matrix_world.copy() for o in (part, child)}
                group = create_group([part, child], "Tilted " + kind, kind, pivot="ACTIVE", orientation="ACTIVE")
                self.assertEqual(hosts.host_type(group), kind)
                self.assertEqual(set(hosts.definition(group).objects), set(matrices))
                self.assertEqual(child.parent, part)
                seen = set()
                bpy.context.view_layer.update()
                for item in bpy.context.evaluated_depsgraph_get().object_instances:
                    original = item.object.original
                    if item.is_instance and original in matrices:
                        self.assertMatrixClose(item.matrix_world, matrices[original])
                        seen.add(original)
                self.assertEqual(seen, set(matrices))

    def test_switch_and_per_placement_geometry(self):
        first = create_group([flat_part("plate")], "Table", "NATIVE")
        definition = hosts.definition(first)
        untouched = linked_copy(first, 7)
        bpy.context.view_layer.update()
        expected = first.matrix_world.copy()
        select([first])
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES"), {"FINISHED"})
        first = bpy.context.active_object
        self.assertIsNotNone(first)
        self.assertMatrixClose(first.matrix_world, expected)
        self.assertEqual(hosts.host_type(untouched), "NATIVE")
        self.assertEqual(hosts.definition(first), definition)
        self.assertEqual(count_uses(definition), 2)
        self.assertEqual(bpy.ops.groupmanager.realize(enabled=True), {"FINISHED"})
        solidify = first.modifiers.new("Placement thickness", "SOLIDIFY")
        solidify.thickness = 0.031
        self.assertEqual(eval_faces(first), 6)
        other_plate = flat_part("extra", (0, 0, 2))
        bpy.context.scene.collection.objects.unlink(other_plate)
        definition.objects.link(other_plate)
        self.assertEqual(eval_faces(first), 12)
        self.assertEqual(len(untouched.modifiers), 0)
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NATIVE"), {"CANCELLED"})
        self.assertAlmostEqual(solidify.thickness, 0.031, places=5)
        self.assertEqual(hosts.host_type(first), "NODES")
        first.modifiers.remove(solidify)
        self.assertEqual(bpy.ops.groupmanager.realize(enabled=False), {"FINISHED"})
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NATIVE"), {"FINISHED"})
        restored = bpy.context.active_object
        self.assertEqual(hosts.definition(restored), definition)
        self.assertEqual(hosts.host_type(restored), "NATIVE")
        self.assertMatrixClose(restored.matrix_world, expected)

    def test_three_share_one_new_version_and_survive_reopen(self):
        part = flat_part("tabletop")
        group = create_group([part], "Standard")
        rows = placements(group, 21)
        original = hosts.definition(group)
        matrices = [row.matrix_world.copy() for row in rows]
        select(rows[6:9])
        self.assertEqual(bpy.ops.groupmanager.fork(group_name="Standing"), {"FINISHED"})
        variant = hosts.definition(rows[6])
        self.assertEqual((count_uses(original), count_uses(variant)), (18, 3))
        self.assertEqual({hosts.definition(o) for o in rows[6:9]}, {variant})
        updated = next(o for o in variant.objects if o.get("qa_role") == "tabletop")
        self.assertEqual(updated.data, part.data)
        self.assertEqual(list(updated.data.materials), list(part.data.materials))
        updated.location.z = 1.75
        self.assertEqual(realized_parts(), Counter({("tabletop", 0): 18, ("tabletop", 1.75): 3}))
        for i, row in enumerate(rows):
            self.assertMatrixClose(row.matrix_world, matrices[i])
        with tempfile.TemporaryDirectory() as scratch:
            scene_path = Path(scratch) / "RoundTrip.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(scene_path))
            bpy.ops.wm.open_mainfile(filepath=str(scene_path))
            self.assertEqual(realized_parts(), Counter({("tabletop", 0): 18, ("tabletop", 1.75): 3}))
            actual = Counter(hosts.definition(o).name for o in bpy.context.scene.objects if hosts.definition(o))
            self.assertEqual(sorted(actual.values()), [3, 18])

    def test_switch_preserves_hierarchy_external_constraint_and_animation(self):
        group = create_group([flat_part("panel")], "Carrier")
        source = hosts.definition(group)
        parent = flat_part("frame", (2, 3, 0))
        group.parent = parent
        group.scale = (-1.25, 0.7, 1.5)
        group.rotation_euler.z = 0.35
        group.keyframe_insert(data_path="location", frame=1)
        child = flat_part("child", (0, 2, 3))
        child.parent = group
        watcher = flat_part("watcher")
        watcher.constraints.new("COPY_LOCATION").target = group
        bpy.context.view_layer.update()
        old_world = group.matrix_world.copy()
        child_world = child.matrix_world.copy()
        action = group.animation_data.action
        for kind in ("NODES", "NATIVE"):
            select([group])
            self.assertEqual(bpy.ops.groupmanager.switch_type(kind=kind), {"FINISHED"})
            group = bpy.context.active_object
            bpy.context.view_layer.update()
            self.assertEqual(hosts.definition(group), source)
            self.assertEqual(group.parent, parent)
            self.assertEqual(child.parent, group)
            self.assertEqual(watcher.constraints[0].target, group)
            self.assertEqual(group.animation_data.action, action)
            self.assertMatrixClose(group.matrix_world, old_world)
            self.assertMatrixClose(child.matrix_world, child_world)

    def test_failed_batch_switch_restores_carriers_and_references(self):
        first = create_group([flat_part("left")], "Left")
        second = create_group([flat_part("right")], "Right")
        first.use_fake_user = True
        second.parent = first
        watcher = flat_part("watcher")
        constraint = watcher.constraints.new("COPY_LOCATION")
        constraint.target = first
        mirror = watcher.modifiers.new("Mirror reference", 'MIRROR')
        mirror.mirror_object = first
        first.keyframe_insert(data_path="location", frame=1)
        action = first.animation_data.action
        select([first, second])
        layer = bpy.context.view_layer
        layer.update()
        world = second.matrix_world.copy()
        sources = hosts.definition(first), hosts.definition(second)
        original_ids = tuple(set(items) for items in (bpy.data.objects, bpy.data.meshes, bpy.data.node_groups))
        copied = hosts._copy_settings
        attempts = []

        def fail_second(source, target):
            attempts.append(source)
            if len(attempts) == 2:
                raise RuntimeError("Simulated second carrier failure")
            copied(source, target)

        with patch.object(hosts, "_copy_settings", side_effect=fail_second):
            self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES"), {"CANCELLED"})

        self.assertEqual(attempts, [first, second])
        self.assertEqual(tuple(hosts.definition(obj) for obj in attempts), sources)
        self.assertTrue(all(hosts.host_type(obj) == 'NATIVE' for obj in attempts))
        self.assertTrue(first.use_fake_user)
        self.assertEqual(first.animation_data.action, action)
        self.assertEqual(second.parent, first)
        self.assertEqual(constraint.target, first)
        self.assertEqual(mirror.mirror_object, first)
        self.assertEqual(set(layer.objects.selected), {first, second})
        self.assertEqual(layer.objects.active, first)
        self.assertMatrixClose(second.matrix_world, world)
        self.assertEqual(tuple(set(items) for items in (bpy.data.objects, bpy.data.meshes, bpy.data.node_groups)),
                         original_ids)
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES"), {"FINISHED"})

    def test_failed_nested_batch_switch_restores_edit_path_and_shared_definitions(self):
        desks, monitor, keyboard = nested_workstations(count=2)
        shared = hosts.definition(desks[0])
        select([desks[0]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([monitor, keyboard])
        originals = {obj for obj in bpy.data.objects if not editor.is_helper(obj)}
        definitions = {c for c in bpy.data.collections if hosts.is_definition(c)}
        show = editor._show
        attempts = []

        def fail_after_reopening(context, route):
            show(context, route)
            attempts.append(route)
            if len(attempts) == 1:
                raise RuntimeError("Simulated failure while restoring edit mode")

        with patch.object(editor, "_show", side_effect=fail_after_reopening):
            self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES"), {"CANCELLED"})

        self.assertEqual(len(attempts), 2)
        self.assertEqual(editor.path(bpy.context), [desks[0]])
        self.assertEqual(editor.edit_layer.open_host(shared), desks[0])
        self.assertEqual(hosts.definition(desks[0]), shared)
        self.assertEqual(hosts.definition(desks[1]), shared)
        self.assertEqual(hosts.host_type(monitor), 'NATIVE')
        self.assertEqual(hosts.host_type(keyboard), 'NATIVE')
        self.assertEqual(set(bpy.context.selected_objects), {monitor, keyboard})
        self.assertEqual(bpy.context.active_object, monitor)
        self.assertEqual({obj for obj in bpy.data.objects if not editor.is_helper(obj)}, originals)
        self.assertEqual({c for c in bpy.data.collections if hosts.is_definition(c)}, definitions)

    def test_single_user_scopes_preserve_material_slots_and_internal_links(self):
        for kind in ("NATIVE", "NODES"):
            for scope in ("OBJECT", "OBJECT_DATA", "OBJECT_DATA_MATERIALS"):
                with self.subTest(kind=kind, scope=scope):
                    panel = flat_part("panel")
                    paint = bpy.data.materials.new("Paint")
                    trim = bpy.data.materials.new("Trim")
                    panel.data.materials.append(paint)
                    panel.data.materials.append(trim)
                    panel.data.materials.append(None)
                    panel.material_slots[1].link = 'OBJECT'
                    panel.material_slots[1].material = paint
                    frame = linked_copy(panel, 2)
                    frame["qa_role"] = "frame"
                    frame.material_slots[1].link = 'DATA'
                    original = create_group([panel, frame], "Assembly", kind)
                    source = hosts.definition(original)
                    target = linked_copy(original, 8)
                    select([target, original])
                    self.assertEqual(bpy.ops.groupmanager.make_single_user(scope=scope), {"FINISHED"})
                    self.assertEqual(bpy.context.active_object, target)
                    self.assertEqual(editor.path(bpy.context), [])
                    self.assertEqual(hosts.definition(original), source)
                    result = hosts.definition(target)
                    self.assertNotEqual(result, source)
                    copied = {obj["qa_role"]: obj for obj in result.objects}
                    copy_panel, copy_frame = copied["panel"], copied["frame"]
                    self.assertNotEqual(copy_panel, panel)
                    self.assertNotEqual(copy_frame, frame)
                    self.assertEqual(copy_panel.data, copy_frame.data)
                    self.assertEqual(copy_panel.data == panel.data, scope == "OBJECT")
                    self.assertEqual([slot.link for slot in copy_panel.material_slots], ["DATA", "OBJECT", "DATA"])
                    self.assertEqual(copy_frame.material_slots[1].link, "DATA")
                    new_paint, new_trim, empty = copy_frame.data.materials
                    self.assertIsNone(empty)
                    self.assertEqual(copy_panel.material_slots[1].material, new_paint)
                    self.assertEqual(new_paint == paint, scope != "OBJECT_DATA_MATERIALS")
                    self.assertEqual(new_trim == trim, scope != "OBJECT_DATA_MATERIALS")
                    self.assertEqual(list(panel.data.materials), [paint, trim, None])
                    self.assertEqual(panel.material_slots[1].material, paint)

    def test_single_user_nested_target_stays_in_parent_edit(self):
        first = create_group([flat_part("screen")], "Monitor", "NODES")
        screen_source = hosts.definition(first)
        second = linked_copy(first, 4)
        tabletop = flat_part("tabletop")
        parent = create_group([first, second, tabletop], "Desk")
        parent_source = hosts.definition(parent)
        other = linked_copy(parent, 12)
        select([other])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([second])
        self.assertEqual(bpy.ops.groupmanager.make_single_user(scope="OBJECT_DATA_MATERIALS"), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [other])
        target = bpy.context.active_object
        self.assertTrue(target.select_get())
        self.assertEqual(hosts.host_type(target), "NODES")
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        variant = hosts.definition(other)
        self.assertNotEqual(variant, parent_source)
        self.assertEqual(hosts.definition(parent), parent_source)
        self.assertIn(target, list(variant.objects))
        self.assertNotEqual(hosts.definition(target), screen_source)
        self.assertEqual(sum(hosts.definition(obj) == screen_source for obj in variant.objects), 1)
        self.assertEqual(hosts.definition(first), screen_source)
        self.assertEqual(hosts.definition(second), screen_source)
        copied_tabletop = next(obj for obj in variant.objects if obj.get("qa_role") == "tabletop")
        self.assertEqual(copied_tabletop.data, tabletop.data)

    def test_nested_branch_affects_only_one_occurrence(self):
        desks, monitor, keyboard = nested_workstations()
        keys_definition = hosts.definition(keyboard)
        old_monitor = hosts.definition(monitor)
        self.assertEqual(count_uses(old_monitor), 21)
        select([desks[6]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([monitor])
        self.assertEqual(bpy.ops.groupmanager.edit(unique=True), {"FINISHED"})
        selected = editor.path(bpy.context)[-1]
        new_monitor = hosts.definition(selected)
        self.assertNotEqual(hosts.definition(desks[6]), hosts.definition(desks[0]))
        self.assertNotEqual(new_monitor, old_monitor)
        screen = next(o for o in bpy.context.visible_objects if o.get("qa_role") == "screen")
        screen.location.z = 2
        next(o for o in keys_definition.objects if o.get("qa_role") == "keys").location.z = 1
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertEqual(realized_parts(), Counter({("screen", 0): 20, ("screen", 2): 1, ("keys", 1): 21}))

    def test_two_identical_siblings_on_one_path(self):
        desks, monitor, keyboard = nested_workstations(second_monitor=True)
        source = hosts.definition(monitor)
        right = next(o for o in hosts.definition(desks[6]).objects if o != monitor and hosts.definition(o) == source)
        right = branches.independent_path([desks[6], right])
        self.assertEqual(count_uses(hosts.definition(right)), 1)
        self.assertEqual(count_uses(source), 41)
        self.assertEqual(count_uses(hosts.definition(keyboard)), 21)

    def test_nested_type_switch_localizes_parent_only(self):
        desks, monitor, keyboard = nested_workstations()
        screen_def = hosts.definition(monitor)
        desks_def = hosts.definition(desks[0])
        select([desks[6]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([monitor])
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES", local=True), {"FINISHED"})
        current = bpy.context.active_object
        self.assertEqual(hosts.host_type(current), "NODES")
        self.assertEqual(hosts.definition(current), screen_def)
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertNotEqual(hosts.definition(desks[6]), desks_def)
        self.assertEqual(count_uses(desks_def), 20)
        self.assertEqual(count_uses(screen_def), 21)
        self.assertEqual(count_uses(hosts.definition(keyboard)), 21)
        self.assertEqual(hosts.host_type(monitor), "NATIVE")
        self.assertEqual(realized_parts(), Counter({("screen", 0): 21, ("keys", 0): 21}))

    def test_group_mode_stays_open_after_mesh_edit(self):
        group = create_group([flat_part("leg")], "Table")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="EDIT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="OBJECT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "OBJECT")
        self.assertEqual(editor.path(bpy.context)[-1], group)
        self.assertTrue(edit_layer.is_open(hosts.definition(group)))

    def test_object_mode_leaves_group_editing(self):
        group = create_group([flat_part("leg")], "Table")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertTrue(editor.path(bpy.context))
        self.assertEqual(bpy.ops.groupmanager.object_mode(), {"FINISHED"})
        self.assertFalse(editor.path(bpy.context))
        self.assertEqual(bpy.ops.groupmanager.object_mode(), {"FINISHED"})

    def test_switch_type_uses_the_open_group(self):
        group = create_group([flat_part("leg")], "Table")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        member = next(obj for obj in editor.content(hosts.definition(editor.path(bpy.context)[-1])).objects
                      if obj.type == "MESH")
        select([member])
        self.assertIsNone(hosts.definition(bpy.context.active_object))
        self.assertTrue(bpy.ops.groupmanager.switch_type.poll())
        self.assertEqual(bpy.ops.groupmanager.switch_type(kind="NODES"), {"FINISHED"})
        host = editor.path(bpy.context)[-1]
        self.assertEqual(hosts.host_type(host), "NODES")
        self.assertTrue(hosts.definition(host).name.startswith("Group_"))

    def test_failed_add_restores_external_parent(self):
        group = create_group([flat_part("leg")], "Table")
        group.location = (3, 0, 0)
        bpy.context.view_layer.update()
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        anchor = bpy.data.objects.new("Empty", None)
        bpy.context.scene.collection.objects.link(anchor)
        panel = flat_part("panel", (1, 2, 0))
        child = flat_part("child", (1.2, 2, 0))
        panel.parent = anchor
        child.parent = panel
        bpy.context.view_layer.update()
        select([panel])
        with self.assertRaises(RuntimeError):
            bpy.ops.groupmanager.add_members()
        self.assertEqual(panel.parent, anchor)
        self.assertNotIn(panel, set(editor.content(hosts.definition(group)).objects))

    def test_open_group_shows_every_nested_group(self):
        bolt = create_group([flat_part("bolt")], "Bolt")
        inner = create_group([bolt, flat_part("leg", (1, 0, 0))], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        def open_and_child_frames():
            frames = editor._open_boxes(bpy.context) + editor._member_group_boxes(bpy.context, [])
            return {host for host, _corners in frames}

        boxed = open_and_child_frames()
        self.assertIn(outer, boxed)
        self.assertIn(inner, boxed)
        self.assertNotIn(bolt, boxed)
        editor._enter_named_group(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        entered = open_and_child_frames()
        self.assertIn(inner, entered)
        self.assertIn(bolt, entered)
        editor._enter_named_group(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        editor._enter_named_group(bpy.context, bolt)
        self.assertEqual(editor.path(bpy.context), [outer, inner, bolt])
        editor._enter_named_group(bpy.context, outer)
        self.assertEqual(editor.path(bpy.context), [outer])
        editor._enter_named_group(bpy.context, outer)
        self.assertEqual(editor.path(bpy.context), [outer])
        editor._enter_named_group(bpy.context, inner)
        parents = {host for host, _corners in editor._ancestor_boxes(bpy.context)}
        self.assertEqual(parents, {outer})

    def test_selected_group_name_enters_group_mode(self):
        group = create_group([flat_part("leg")], "Table")
        select([group])
        self.assertFalse(editor.path(bpy.context))
        boxed = {host for host, _corners in editor._selected_boxes(bpy.context, [])}
        self.assertIn(group, boxed)
        style = editor._bounds_style(bpy.context, preview=True)
        self.assertTrue(style.show_3d or style.show_2d)
        editor._handle_name_press(bpy.context, group)
        self.assertFalse(editor.path(bpy.context))
        self.assertEqual(bpy.context.active_object, group)
        editor._handle_name_press(bpy.context, group)
        self.assertEqual(editor.path(bpy.context), [group])

    def test_sibling_group_shows_beside_the_open_level(self):
        spare = create_group([flat_part("arm")], "Arm")
        inner = create_group([flat_part("leg")], "Leg")
        outer = create_group([inner, spare, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor._sibling_boxes(bpy.context), [])
        editor._enter_named_group(bpy.context, inner)
        siblings = {host for host, _corners in editor._sibling_boxes(bpy.context)}
        self.assertEqual(siblings, {spare})
        editor._enter_named_group(bpy.context, spare)
        self.assertEqual(editor.path(bpy.context), [outer, spare])

    def test_up_and_down_level(self):
        bolt = create_group([flat_part("bolt")], "Bolt")
        inner = create_group([bolt, flat_part("leg", (1, 0, 0))], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer])
        self.assertFalse(bpy.ops.groupmanager.up_level.poll())
        select([inner])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        select([bolt])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, inner, bolt])
        self.assertEqual(bpy.ops.groupmanager.up_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        self.assertEqual(bpy.ops.groupmanager.up_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer])
        self.assertTrue(editor.path(bpy.context))

    def test_up_and_down_follow_the_selected_group(self):
        spare = create_group([flat_part("arm")], "Arm")
        bolt = create_group([flat_part("bolt")], "Bolt")
        inner = create_group([bolt, flat_part("leg", (1, 0, 0))], "Leg")
        outer = create_group([inner, spare, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        select([inner])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        select([bolt])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        select([inner])
        self.assertEqual(bpy.ops.groupmanager.up_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer])
        select([spare])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, spare])
        shown = [outer, inner, spare]
        active, selected = editor._highlight_hosts(bpy.context, shown)
        self.assertEqual(active, spare)
        self.assertNotIn(inner, selected)

    def test_up_level_returns_to_the_root_group(self):
        bolt = create_group([flat_part("bolt")], "Bolt")
        inner = create_group([bolt, flat_part("leg", (1, 0, 0))], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        select([inner])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        select([bolt])
        self.assertEqual(bpy.ops.groupmanager.down_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer, inner, bolt])
        bpy.context.view_layer.objects.active = outer
        self.assertEqual(bpy.ops.groupmanager.up_level(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer])

    def test_double_click_member_stays_in_group_mode(self):
        part = flat_part("panel")
        group = create_group([part], "Table")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertFalse(editor._object_double_click(bpy.context, group))
        self.assertFalse(editor._object_double_click(bpy.context, group))
        self.assertEqual(editor.path(bpy.context), [group])
        self.assertFalse(editor._object_double_click(bpy.context, part))
        self.assertFalse(editor._object_double_click(bpy.context, part))
        self.assertEqual(editor.path(bpy.context), [group])

    def test_double_click_name_enters_named_level(self):
        inner = create_group([flat_part("leg")], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        editor._pending_name_click = None
        editor._handle_name_press(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer])
        editor._handle_name_press(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        editor._handle_name_press(bpy.context, outer)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        editor._handle_name_press(bpy.context, outer)
        self.assertEqual(editor.path(bpy.context), [outer])

    def test_empty_double_click_closes_one_level_including_root(self):
        inner = create_group([flat_part("leg")], "Leg")
        outer = create_group([inner], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        editor._enter_named_group(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        editor._pending_empty_clicks = None
        self.assertFalse(editor._empty_click(bpy.context))
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        self.assertTrue(editor._empty_click(bpy.context))
        self.assertEqual(editor.path(bpy.context), [outer])
        self.assertFalse(editor._empty_click(bpy.context))
        self.assertEqual(editor.path(bpy.context), [outer])
        self.assertTrue(editor._empty_click(bpy.context))
        self.assertFalse(editor.path(bpy.context))

    def test_double_click_child_group_enters_that_level(self):
        panel = flat_part("panel")
        inner = create_group([panel], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()

        def above(obj):
            for instance in depsgraph.object_instances:
                if instance.is_instance and instance.object.original == obj:
                    return instance.matrix_world.translation + Vector((0, 0, 5))
            self.fail(f"{obj.name} has no instance")

        host = editor._level_group_hit(bpy.context, above(panel), Vector((0, 0, -1)), panel)
        self.assertEqual(host, inner)
        editor._pending_level_clicks = None
        self.assertFalse(editor._level_group_double_click(bpy.context, inner))
        self.assertEqual(editor.path(bpy.context), [outer])
        self.assertTrue(editor._level_group_double_click(bpy.context, inner))
        self.assertEqual(editor.path(bpy.context), [outer, inner])

    def test_double_click_another_branch_enters_that_group(self):
        spare = create_group([flat_part("arm")], "Arm")
        inner = create_group([flat_part("leg")], "Leg")
        outer = create_group([inner, spare], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        editor._enter_named_group(bpy.context, inner)
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        self.assertEqual(editor._jump_path(bpy.context, spare), [outer, spare])
        arm = next(obj for obj in editor.content(hosts.definition(spare)).all_objects if obj.get("qa_role") == "arm")
        self.assertEqual(editor._specific_other_group(bpy.context, arm), spare)
        editor._pending_level_clicks = None
        self.assertFalse(editor._level_group_double_click(bpy.context, spare))
        self.assertEqual(editor.path(bpy.context), [outer, inner])
        self.assertTrue(editor._level_group_double_click(bpy.context, spare))
        self.assertEqual(editor.path(bpy.context), [outer, spare])

    def test_double_click_group_enters_group_mode(self):
        panel = flat_part("panel")
        inner = create_group([panel], "Leg")
        outer = create_group([inner], "Table")
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()

        def above(obj):
            for instance in depsgraph.object_instances:
                if instance.is_instance and instance.object.original == obj:
                    return instance.matrix_world.translation + Vector((0, 0, 5))
            self.fail(f"{obj.name} has no instance")

        host = editor._top_group_hit(bpy.context, above(panel), Vector((0, 0, -1)), [outer], panel)
        self.assertEqual(host, outer)
        self.assertFalse(editor.path(bpy.context))
        editor._pending_group_clicks = None
        self.assertFalse(editor._group_double_click(bpy.context, outer))
        self.assertFalse(editor.path(bpy.context))
        self.assertTrue(editor._group_double_click(bpy.context, outer))
        self.assertEqual(editor.path(bpy.context), [outer])

    def test_create_group_wraps_the_open_group_and_another_group(self):
        part = flat_part("leg")
        table = create_group([part], "Table")
        lamp = create_group([flat_part("bulb", (3, 0, 0))], "Lamp")
        select([table])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([part, lamp])
        self.assertEqual(bpy.ops.groupmanager.create(group_name="Room"), {"FINISHED"})
        route = editor.path(bpy.context)
        self.assertEqual(len(route), 1)
        room = route[-1]
        members = set(editor.content(hosts.definition(room)).objects)
        self.assertEqual(members, {table, lamp})
        self.assertIn(part, set(editor.content(hosts.definition(table)).objects))

    def test_create_group_wraps_a_nested_level_and_its_sibling(self):
        spare = create_group([flat_part("arm")], "Arm")
        inner = create_group([flat_part("leg")], "Leg")
        outer = create_group([inner, spare, flat_part("top", (2, 0, 0))], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        editor._enter_named_group(bpy.context, inner)
        select([spare])
        self.assertEqual(bpy.ops.groupmanager.create(group_name="Pair"), {"FINISHED"})
        route = editor.path(bpy.context)
        self.assertEqual(route[0], outer)
        self.assertEqual(len(route), 2)
        pair = route[-1]
        self.assertEqual(set(editor.content(hosts.definition(pair)).objects), {inner, spare})

    def test_add_can_include_or_drop_an_external_parent(self):
        group = create_group([flat_part("leg")], "Table")
        group.location = (3, 1, 0)
        bpy.context.view_layer.update()
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        panel = flat_part("panel", (1, 2, 0))
        empty = bpy.data.objects.new("Empty", None)
        bpy.context.scene.collection.objects.link(empty)
        empty.location = (1, 0, 0)
        panel.parent = empty
        bpy.context.view_layer.update()
        world = panel.matrix_world.translation.copy()
        select([panel])
        self.assertEqual(bpy.ops.groupmanager.add_members(), {"FINISHED"})
        content = editor.content(hosts.definition(group))
        self.assertIn(panel, set(content.objects))
        self.assertNotIn(empty, set(content.objects))
        self.assertIsNone(panel.parent)
        self.assertVectorClose(panel.matrix_world.translation, world)

    def test_shift_d_copies_the_group_and_alt_d_instances_it(self):
        part = flat_part("leg")
        group = create_group([part], "Table")
        source = hosts.definition(group)
        mesh = part.data
        select([group])
        self.assertEqual(bpy.ops.groupmanager.duplicate_move(kind='GROUP'), {"FINISHED"})
        copied = bpy.context.active_object
        copied_source = hosts.definition(copied)
        self.assertNotEqual(copied_source, source)
        copied_part = next(obj for obj in copied_source.objects if obj.get("qa_role") == "leg")
        self.assertEqual(copied_part.data, mesh)
        self.assertNotIn(copied_part, set(source.all_objects))
        before = part.location.x
        copied_part.location.x += 4
        self.assertEqual(part.location.x, before)
        select([group])
        self.assertEqual(bpy.ops.groupmanager.duplicate_move(kind='INSTANCE'), {"FINISHED"})
        linked = bpy.context.active_object
        self.assertEqual(hosts.definition(linked), source)
        self.assertNotEqual(linked, group)

    def test_alt_shift_d_copies_nested_groups(self):
        bolt_part = flat_part("bolt")
        bolt = create_group([bolt_part], "Bolt")
        inner = create_group([bolt, flat_part("leg", (1, 0, 0))], "Leg")
        outer = create_group([inner, flat_part("top", (2, 0, 0))], "Table")
        bolt_source = hosts.definition(bolt)
        inner_source = hosts.definition(inner)
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.duplicate_move(kind='NESTED'), {"FINISHED"})
        copied = bpy.context.active_object
        copied_inner = next(obj for obj in hosts.definition(copied).objects if hosts.definition(obj))
        copied_inner_source = hosts.definition(copied_inner)
        self.assertNotEqual(copied_inner_source, inner_source)
        copied_bolt = next(obj for obj in copied_inner_source.objects if hosts.definition(obj))
        self.assertNotEqual(hosts.definition(copied_bolt), bolt_source)
        copied_bolt_part = next(obj for obj in hosts.definition(copied_bolt).objects if obj.get("qa_role") == "bolt")
        self.assertEqual(copied_bolt_part.data, bolt_part.data)
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.duplicate_move(kind='GROUP'), {"FINISHED"})
        shared = bpy.context.active_object
        shared_inner = next(obj for obj in hosts.definition(shared).objects if hosts.definition(obj))
        self.assertEqual(hosts.definition(shared_inner), inner_source)

    def test_shared_member_edits_and_unused_definition_survive_library_removal(self):
        group = create_group([flat_part("surface")], "Table")
        source = hosts.definition(group)
        select([group])
        self.assertEqual(bpy.ops.groupmanager.duplicate_shared(offset=(5, 0, 0)), {"FINISHED"})
        duplicate = bpy.context.active_object
        extra = flat_part("edge", (0, 0, 2))
        select([group, extra])
        self.assertEqual(bpy.ops.groupmanager.add_members(), {"FINISHED"})
        self.assertEqual(realized_parts()[("edge", 2)], 2)
        select([group])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([extra])
        self.assertEqual(bpy.ops.groupmanager.remove_members(), {"FINISHED"})
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertNotIn(extra, set(source.objects))
        self.assertIn(extra, set(bpy.context.scene.objects))
        bpy.data.objects.remove(duplicate, do_unlink=True)
        bpy.data.objects.remove(group, do_unlink=True)
        bpy.context.view_layer.update()
        self.assertEqual(count_uses(source), 0)
        self.assertTrue(source.use_fake_user)
        self.assertIn(source.name, bpy.data.collections)
        for panel in ('GROUPMANAGER_PT_definitions', 'GROUPMANAGER_PT_object_manage'):
            self.assertFalse(hasattr(bpy.types, panel))
        self.assertFalse(hasattr(bpy.types.WindowManager, 'gm_library_query'))
        for operator in ('place_existing', 'cleanup_unused'):
            self.assertNotIn(operator, dir(bpy.ops.groupmanager))

    def test_pick_object_selects_member_through_nested_groups(self):
        panel = flat_part("panel")
        other = flat_part("other", location=(4, 0, 0))
        core = create_group([panel], "Core")
        outer = create_group([core, other], "Outer")
        outer.location = (3, 0, 0)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()

        def above(obj):
            for instance in depsgraph.object_instances:
                if instance.is_instance and instance.object.original == obj:
                    return instance.matrix_world.translation + Vector((0, 0, 5))
            self.fail(f"{obj.name} has no instance")

        index = picking._PickIndex([outer], depsgraph)
        _, nested = index.cast(above(panel), Vector((0, 0, -1)))
        _, direct = index.cast(above(other), Vector((0, 0, -1)))
        self.assertTrue(nested)
        self.assertIs(nested[0].obj, panel)
        self.assertEqual(nested[0].path, (outer, core))
        self.assertTrue(direct)
        self.assertIs(direct[0].obj, other)
        self.assertEqual(direct[0].path, (outer,))
        picking._select_picked(bpy.context, nested[0].path, nested[0].obj)
        self.assertIs(bpy.context.view_layer.objects.active, panel)
        self.assertEqual(tuple(editor.path(bpy.context)), (outer, core))
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})

    def test_pick_group_reaches_every_nested_level(self):
        panel = flat_part("panel")
        bolt = create_group([panel], "Bolt")
        inner = create_group([bolt], "Leg")
        outer = create_group([inner], "Table")
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()

        def above(obj):
            for instance in depsgraph.object_instances:
                if instance.is_instance and instance.object.original == obj:
                    return instance.matrix_world.translation + Vector((0, 0, 5))
            self.fail(f"{obj.name} has no instance")

        hits, _ = picking._PickIndex([outer], depsgraph).cast(above(panel), Vector((0, 0, -1)))
        depths = {len(hit.path) for hit in hits}
        self.assertEqual(depths, {1, 2, 3})
        deepest = max(hits, key=lambda hit: (len(hit.path), hit.distance))
        chain = picking._pick_chain(hits, deepest)
        self.assertEqual([hit.path[-1] for hit in chain], [outer, inner, bolt])

    def test_force_ungroup_expands_every_nested_group(self):
        core = create_group([flat_part("core")], "Core")
        middle = create_group([core], "Middle")
        outer = create_group([middle, flat_part("shell")], "Outer")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.force_ungroup(), {"FINISHED"})
        selected = list(bpy.context.selected_objects)
        self.assertEqual({obj.get("qa_role") for obj in selected}, {"core", "shell"})
        self.assertFalse(any(hosts.definition(obj) for obj in selected))

    def test_internal_targets_dissolve_one_layer(self):
        target = flat_part("target")
        mesh = flat_part("controlled")
        mesh.parent = target
        constraint = mesh.constraints.new("COPY_LOCATION")
        constraint.target = target
        boolean = mesh.modifiers.new("Cut", "BOOLEAN")
        boolean.object = target
        nested = create_group([flat_part("nested")], "Nested")
        root = create_group([target, mesh, nested], "Constrained")
        other = linked_copy(root, 7)
        original = hosts.definition(root)
        variant = branches.fork_selected([other], name="Local")
        parts = {o.get("qa_role"): o for o in variant.objects}
        self.assertEqual(parts["controlled"].constraints[0].target, parts["target"])
        self.assertEqual(parts["controlled"].parent, parts["target"])
        self.assertEqual(parts["controlled"].modifiers[0].object, parts["target"])
        self.assertEqual(mesh.constraints[0].target, target)
        self.assertEqual(mesh.modifiers[0].object, target)
        select([other])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        new_items = bpy.context.selected_objects
        ordinary = {o.get("qa_role"): o for o in new_items}
        self.assertEqual(ordinary["controlled"].parent, ordinary["target"])
        self.assertEqual(ordinary["controlled"].constraints[0].target, ordinary["target"])
        self.assertEqual(ordinary["controlled"].modifiers[0].object, ordinary["target"])
        self.assertEqual(sum(hosts.definition(o) is not None for o in new_items), 1)
        self.assertEqual(hosts.definition(root), original)
        self.assertEqual(count_uses(original), 1)

    def test_clear_group_scope_preserves_shared_geometry_positions(self):
        selected = create_group([flat_part("board")], "Board")
        source = hosts.definition(selected)
        peer = linked_copy(selected, 8)
        another = linked_copy(selected, 13)
        select([selected])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertEqual(count_uses(source), 2)
        self.assertEqual(hosts.definition(peer), source)
        self.assertEqual(hosts.definition(another), source)
        self.assertEqual({o.location.x for o in bpy.context.selected_objects}, {0})
        select([peer, another])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        bpy.context.view_layer.update()
        self.assertEqual(count_uses(source), 0)
        boards = [o for o in bpy.context.scene.objects if o.get("qa_role") == "board"]
        self.assertEqual(sorted(o.matrix_world.translation.x for o in boards), [0, 8, 13])

    def test_ungroup_replaces_external_modifier_target_with_empty(self):
        selected = create_group([flat_part("board")], "Board")
        mirror = flat_part("mirror", (3, 0, 0))
        modifier = mirror.modifiers.new("Mirror", 'MIRROR')
        modifier.mirror_object = selected
        world = selected.matrix_world.copy()
        removed_name = selected.name
        select([selected])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertIsNone(bpy.data.objects.get(removed_name))
        target = modifier.mirror_object
        self.assertIsNotNone(target)
        self.assertEqual(target.type, 'EMPTY')
        self.assertTrue(target.name.startswith(removed_name + "_GM_Target"))
        self.assertMatrixClose(target.matrix_world, world)
        self.assertTrue(any(obj.get("qa_role") == "board" for obj in bpy.context.scene.objects))

    def test_split_mirror_creates_linked_instance_and_keeps_other_modifiers(self):
        selected = create_group([flat_part("board")], "Board", "NODES")
        selected.location = (4, 0, 0)
        bpy.context.view_layer.update()
        mirror_empty = bpy.data.objects.new("Mirror", None)
        bpy.context.scene.collection.objects.link(mirror_empty)
        modifier = selected.modifiers.new("Mirror", 'MIRROR')
        modifier.use_axis[0] = True
        modifier.use_axis[1] = False
        modifier.use_axis[2] = False
        modifier.mirror_object = mirror_empty
        selected.modifiers.new("Remesh", 'REMESH')
        source = hosts.definition(selected)
        select([selected])
        self.assertEqual(bpy.ops.groupmanager.split_mirror(), {"FINISHED"})
        instances = [obj for obj in bpy.context.scene.objects if hosts.definition(obj) == source]
        self.assertEqual(len(instances), 2)
        self.assertEqual({round(obj.matrix_world.translation.x, 4) for obj in instances}, {-4.0, 4.0})
        for obj in instances:
            self.assertFalse(any(item.type == 'MIRROR' for item in obj.modifiers))
            self.assertTrue(any(item.type == 'REMESH' for item in obj.modifiers))
        self.assertEqual(count_uses(source), 2)

    def test_ungroup_delete_modifiers_discards_mirror(self):
        selected = create_group([flat_part("board")], "Board", "NODES")
        selected.location = (4, 0, 0)
        bpy.context.view_layer.update()
        modifier = selected.modifiers.new("Mirror", 'MIRROR')
        modifier.use_axis[0] = True
        modifier.use_axis[1] = False
        modifier.use_axis[2] = False
        source = hosts.definition(selected)
        select([selected])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertFalse(any(hosts.definition(obj) == source for obj in bpy.context.scene.objects))
        self.assertTrue(any(obj.get("qa_role") == "board" for obj in bpy.context.scene.objects))

    def test_delete_group_removes_placement_and_keeps_source(self):
        selected = create_group([flat_part("board")], "Board")
        source = hosts.definition(selected)
        peer = linked_copy(selected, 4)
        member = next(iter(source.objects))
        removed_name = selected.name
        select([selected])
        self.assertEqual(bpy.ops.groupmanager.delete_group(), {"FINISHED"})
        self.assertIsNone(bpy.data.objects.get(removed_name))
        self.assertEqual(hosts.definition(peer), source)
        self.assertEqual(count_uses(source), 1)
        self.assertIn(member, set(source.objects))
        self.assertNotIn(member, set(bpy.context.scene.objects))

    def test_ungroup_selection_nested_and_other_scene(self):
        desks, monitor, keyboard = nested_workstations(3)
        source = hosts.definition(monitor)
        keyboard_source = hosts.definition(keyboard)
        other_scene = bpy.data.scenes.new("Other scene")
        remote = monitor.copy()
        other_scene.collection.objects.link(remote)
        remote.location.x = 17
        select([desks[0]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([monitor])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertNotEqual(hosts.definition(desks[0]), hosts.definition(desks[1]))
        self.assertEqual(hosts.definition(desks[1]), hosts.definition(desks[2]))
        parts = [o for o in hosts.definition(desks[0]).objects if o.get("qa_role") == "screen"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(hosts.definition(remote), source)
        self.assertEqual(count_uses(keyboard_source), 3)
        bpy.context.window.scene = other_scene
        bpy.context.view_layer.update()
        select([remote])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertEqual(len(other_scene.objects), 1)
        ordinary = next(iter(other_scene.objects))
        self.assertEqual(ordinary.get("qa_role"), "screen")
        self.assertAlmostEqual(ordinary.matrix_world.translation.x, 17)

    def test_ungroup_discards_extra_modifier_for_every_selected_placement(self):
        selected = create_group([flat_part("board")], "Board")
        source = hosts.definition(selected)
        modified = hosts.make_host("Modified", source, "NODES")
        bpy.context.scene.collection.objects.link(modified)
        modified.modifiers.new("Bevel to discard", "BEVEL")
        select([selected, modified])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        self.assertIsNone(bpy.data.objects.get("Modified"))
        self.assertEqual(count_uses(source), 0)
        boards = [obj for obj in bpy.context.scene.objects if obj.get("qa_role") == "board"]
        self.assertTrue(boards)
        self.assertFalse(any(modifier.type == 'BEVEL' for obj in boards for modifier in obj.modifiers))

        kept = hosts.make_host("Kept", source, "NODES")
        bpy.context.scene.collection.objects.link(kept)
        kept.modifiers.new("Bevel to keep", "BEVEL")
        bpy.context.window_manager.gm_keep_group_modifiers = True
        select([kept])
        self.assertEqual(bpy.ops.groupmanager.dissolve(), {"FINISHED"})
        kept_boards = [obj for obj in bpy.context.selected_objects if obj.get("qa_role") == "board"]
        self.assertTrue(kept_boards)
        self.assertTrue(all(any(modifier.type == 'BEVEL' for modifier in obj.modifiers) for obj in kept_boards))

    def test_edit_navigation_and_clean_save(self):
        desks, monitor, _ = nested_workstations()
        stage = bpy.context.scene.name
        select([desks[6]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context)[0], desks[6])
        select([monitor])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [desks[6], monitor])
        self.assertEqual(bpy.ops.groupmanager.back(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [desks[6]])
        self.assertEqual(bpy.context.active_object, monitor)
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "EditState.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(output))
            bpy.ops.wm.open_mainfile(filepath=str(output))
            self.assertEqual(bpy.context.scene.name, stage)
            self.assertFalse(any(s.get("_gm_edit") for s in bpy.data.scenes))
            self.assertEqual(realized_parts(), Counter({("screen", 0): 21, ("keys", 0): 21}))

    def test_edit_visible_placement_in_current_scene_and_save(self):
        for kind in ("NATIVE", "NODES"):
            with self.subTest(kind=kind):
                bpy.ops.wm.open_mainfile(filepath=str(TEMPLATE))
                part = flat_part("panel", (1, 2, 0.5))
                other = flat_part("reference", (15, 1, 2))
                host = create_group([part], "Assembly", kind)
                source = hosts.definition(host)
                peer = linked_copy(host, -8)
                host.location = (4, -3, 2)
                host.rotation_euler.z = 0.52
                host.scale = (1.4, -0.8, 1.2)
                stage = bpy.context.scene
                remote = bpy.data.scenes.new("Remote")
                remote.collection.objects.link(peer.copy())
                bpy.context.view_layer.update()
                offset = Matrix.Translation(-source.instance_offset)
                member_space = host.matrix_world @ offset
                peer_space = peer.matrix_world @ offset
                expected_world = member_space @ part.matrix_world
                initial = visible_part_locations("panel")
                self.assertEqual(len(initial), 2)
                select([host])
                self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
                self.assertEqual(bpy.context.window.scene, stage)
                self.assertEqual(set(bpy.data.scenes), {stage, remote})
                self.assertTrue(other.visible_get())
                self.assertEqual(usage.counts(source), {stage: 2, remote: 1})
                self.assertEqual(visible_part_locations("panel"), initial)
                member = next(o for o in bpy.context.visible_objects if o.get("qa_role") == "panel")
                self.assertMatrixClose(member.matrix_world, expected_world)
                moved = member.matrix_world.copy()
                moved.translation += Vector((0.5, 0.3, -1.0))
                member.matrix_world = moved
                bpy.context.view_layer.update()
                expected_peer = peer_space @ member_space.inverted() @ moved
                expected = sorted(tuple(round(v, 4) for v in mat.translation)
                                  for mat in (moved, expected_peer))
                self.assertEqual(visible_part_locations("panel"), expected)
                with tempfile.TemporaryDirectory() as scratch:
                    output = Path(scratch) / "InPlace.blend"
                    self.assertEqual(bpy.ops.wm.save_as_mainfile(filepath=str(output)), {"FINISHED"})
                    bpy.ops.wm.open_mainfile(filepath=str(output))
                    self.assertEqual(visible_part_locations("panel"), expected)
                    self.assertEqual(editor.path(bpy.context), [])
                    self.assertEqual({s.name for s in bpy.data.scenes}, {"GroupManager", "Remote"})
                    source = next(c for c in bpy.data.collections if hosts.is_definition(c))
                    self.assertEqual({s.name: n for s, n in usage.counts(source).items()},
                                     {"GroupManager": 2, "Remote": 1})

    def test_linked_placements_can_be_edited_together(self):
        part = flat_part("panel", (1, 0, 0))
        host = create_group([part], "Assembly")
        second = linked_copy(host, 5)
        third = linked_copy(host, -5)
        select([host, second, third])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        member = next(obj for obj in bpy.context.visible_objects
                      if obj.get("qa_role") == "panel" and not obj.get("_gm_mirror"))
        mirrors = [obj for obj in bpy.data.objects if obj.get("_gm_mirror_of") == member]
        self.assertEqual(len(mirrors), 2)
        self.assertEqual(mirrors[0].data, member.data)
        from scena import edit_layer
        edit_layer.sync_linked_mirrors()
        before = member.matrix_world.translation.x
        other = mirrors[1].matrix_world.translation.x
        moved = mirrors[0].matrix_world.copy()
        moved.translation.x += 2
        mirrors[0].matrix_world = moved
        edit_layer.sync_linked_mirrors()
        self.assertAlmostEqual(member.matrix_world.translation.x, before + 2, places=3)
        self.assertAlmostEqual(mirrors[1].matrix_world.translation.x, other + 2, places=3)
        places = visible_part_locations("panel")
        self.assertGreaterEqual(len(places), 3)
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertFalse(any(obj.get("_gm_mirror") for obj in bpy.data.objects))
        self.assertFalse(second.hide_get())
        self.assertFalse(third.hide_get())

    def test_linked_placements_show_child_groups_at_every_copy(self):
        bolt = create_group([flat_part("bolt")], "Bolt")
        table = create_group([bolt, flat_part("top", (2, 0, 0))], "Table")
        second = linked_copy(table, 6)
        third = linked_copy(table, 12)
        select([table, second, third])
        source = hosts.definition(bolt)
        self.assertEqual(count_uses(source), 3)
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(set(editor.peers(bpy.context)) | {editor.path(bpy.context)[-1]}, {table, second, third})
        self.assertEqual(count_uses(source), 3)
        self.assertEqual({path[0] for path in usage.paths(bpy.context.scene, source)}, {table, second, third})
        boxes = [corners for host, corners in editor._member_group_boxes(bpy.context, [])
                 if hosts.definition(host) == source]
        self.assertGreaterEqual(len(boxes), 3)
        xs = sorted(corners[0].x for corners in boxes)
        self.assertGreater(xs[-1] - xs[0], 3)

    def test_edit_preserves_hidden_and_excluded_placements(self):
        member = flat_part("seat")
        host = create_group([member], "Chair")
        visible, hidden, excluded = [linked_copy(host, x) for x in (5, 10, 15)]
        layer = bpy.context.view_layer
        collection = bpy.data.collections.new("Excluded chairs")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(excluded)
        bpy.context.scene.collection.objects.unlink(excluded)
        layer.layer_collection.children[collection.name].exclude = True
        hidden.hide_set(True)
        select([host])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(visible_part_locations("seat"), [(0, 0, 0), (5, 0, 0)])
        for peer in (hidden, excluded):
            with self.assertRaisesRegex(ValueError, "visible"):
                editor.join_host(bpy.context, peer)
        editor.join_host(bpy.context, visible)
        self.assertEqual(visible_part_locations("seat"), [(0, 0, 0), (5, 0, 0)])
        editor.close_peer(bpy.context, visible)
        self.assertTrue(visible.visible_get())
        member.location.z += 2
        self.assertEqual(visible_part_locations("seat"), [(0, 0, 2), (5, 0, 2)])
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertTrue(hidden.hide_get())
        self.assertTrue(layer.layer_collection.children[collection.name].exclude)
        self.assertEqual(visible_part_locations("seat"), [(0, 0, 2), (5, 0, 2)])
        hidden.hide_set(False)
        layer.layer_collection.children[collection.name].exclude = False
        self.assertEqual(visible_part_locations("seat"), [(x, 0, 2) for x in (0, 5, 10, 15)])

    def test_thousand_placements_follow_transform_and_mesh_edits(self):
        member = flat_part("seat")
        host = create_group([member], "Chair")
        rows = placements(host, 1000)
        object_count = len(bpy.data.objects)
        select([rows[-1]])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(len(bpy.data.objects) - object_count, 2)
        member.location.z += 3
        member.data.vertices[0].co.z += 1
        member.data.update()
        expected = [(i * 5, 3, 1) for i in range(1000)]

        def evaluated_seats():
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            return sorted((item.matrix_world.translation.x, item.matrix_world.translation.z,
                           item.object.data.vertices[0].co.z) for item in graph.object_instances
                          if item.show_self and item.object.original == member)

        self.assertEqual(evaluated_seats(), expected)
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        self.assertEqual(evaluated_seats(), expected)
        self.assertEqual(len(bpy.data.objects), object_count)

    def test_counts_hidden_multilink_and_second_scene(self):
        part = create_group([flat_part("part")], "Shared")
        group = hosts.definition(part)
        primary = bpy.context.scene
        also = bpy.data.collections.new("Another collection")
        primary.collection.children.link(also)
        also.objects.link(part)
        part.hide_set(True)
        second = bpy.data.scenes.new("Second scene")
        other = part.copy()
        second.collection.objects.link(other)
        bpy.context.view_layer.update()
        counts = usage.counts(group)
        self.assertEqual((counts.get(primary), counts.get(second)), (1, 1))

    def test_select_same_data_across_group_types(self):
        native = create_group([flat_part("seat")], "Chair")
        source = hosts.definition(native)
        nodes = hosts.make_host("Chair GN", source, "NODES")
        bpy.context.scene.collection.objects.link(nodes)
        independent = linked_copy(native, 5)
        branches.fork_selected([independent])
        for active in (native, nodes):
            select([active, independent])
            self.assertEqual(bpy.ops.groupmanager.select_similar(), {"FINISHED"})
            self.assertEqual(set(bpy.context.selected_objects), {native, nodes})
            self.assertEqual(bpy.context.active_object, active)

        select([independent])
        self.assertEqual(bpy.ops.groupmanager.select_similar(definition_uid=str(source.session_uid)),
                         {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {native, nodes})

    def test_select_shared_mesh_and_collection_in_current_view(self):
        scene = bpy.context.scene
        mesh = flat_part("Leg")
        peer = linked_copy(mesh, 1)
        unique = linked_copy(mesh, 2)
        unique.data = unique.data.copy()
        hidden = linked_copy(mesh, 3)
        hidden.hide_set(True)
        locked = linked_copy(mesh, 4)
        locked.hide_select = True
        excluded = bpy.data.collections.new("Excluded")
        scene.collection.children.link(excluded)
        excluded.objects.link(mesh.copy())
        bpy.context.view_layer.layer_collection.children[excluded.name].exclude = True
        remote = bpy.data.scenes.new("Other scene")
        remote.collection.objects.link(mesh.copy())
        bpy.context.view_layer.update()
        select([peer, unique])
        self.assertEqual(bpy.ops.groupmanager.select_similar(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {mesh, peer})
        self.assertEqual(bpy.context.active_object, peer)

        collection = bpy.data.collections.new("Kit")
        placement = bpy.data.objects.new("Kit instance", None)
        scene.collection.objects.link(placement)
        placement.instance_type = 'COLLECTION'
        placement.instance_collection = collection
        second = linked_copy(placement, 3)
        unrelated = linked_copy(placement, 6)
        unrelated.instance_collection = bpy.data.collections.new("Kit independent")
        select([second, unrelated])
        self.assertEqual(bpy.ops.groupmanager.select_similar(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {placement, second})
        self.assertEqual(bpy.context.active_object, second)

    def test_select_shared_mesh_inside_edited_group(self):
        part = flat_part("Shelf")
        peer = linked_copy(part, 1)
        outside = linked_copy(part, 5)
        create_group([part, peer], "Cabinet")
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([part])
        self.assertTrue(outside.visible_get())
        self.assertEqual(bpy.ops.groupmanager.select_similar(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {part, peer})
        self.assertEqual(bpy.context.active_object, part)
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})

    def test_select_linked_group_reaches_outside_the_open_level(self):
        bolt = create_group([flat_part("bolt")], "Bolt")
        source = hosts.definition(bolt)
        outside = linked_copy(bolt, 8)
        other = linked_copy(bolt, 0)
        table = create_group([other, flat_part("top", (2, 0, 0))], "Table")
        select([table])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        member = next(obj for obj in editor.content(hosts.definition(table)).objects
                      if hosts.definition(obj) == source)
        select([member])
        self.assertEqual(bpy.ops.groupmanager.select_similar(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {member, outside, bolt})

    def test_select_grouped_and_linked_group_menus(self):
        mesh = flat_part("Leg")
        plain = flat_part("Loose")
        native = create_group([flat_part("seat")], "Chair")
        source = hosts.definition(native)
        nodes = hosts.make_host("Chair GN", source, "NODES")
        bpy.context.scene.collection.objects.link(nodes)
        other = create_group([flat_part("door")], "Door")
        bpy.context.view_layer.update()

        select([native])
        self.assertEqual(bpy.ops.groupmanager.select_grouped_kind(), {"FINISHED"})
        selected = set(bpy.context.selected_objects)
        self.assertIn(native, selected)
        self.assertIn(nodes, selected)
        self.assertIn(other, selected)
        self.assertNotIn(mesh, selected)
        self.assertNotIn(plain, selected)

        select([nodes, mesh])
        self.assertEqual(bpy.ops.groupmanager.select_linked_group(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {native, nodes})
        self.assertEqual(bpy.context.active_object, nodes)

        inner = create_group([flat_part("knob")], "Knob")
        board = flat_part("Board")
        host = create_group([board, inner], "Cabinet")
        select([host])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([board])
        self.assertEqual(bpy.ops.groupmanager.select_grouped_kind(), {"FINISHED"})
        self.assertEqual(set(bpy.context.selected_objects), {inner})
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})

    def test_native_add_while_editing_at_placement(self):
        surface = flat_part("surface")
        host = create_group([surface], "Table")
        host.location.x = 10
        peer = linked_copy(host, -3)
        outside = flat_part("surrounding", (0, -7, 0))
        select([host])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        bpy.context.scene.cursor.location = (10, 4, 0)
        self.assertEqual(bpy.ops.mesh.primitive_cube_add(size=0.5), {"FINISHED"})
        cube = bpy.context.active_object
        cube["qa_role"] = "shelf"
        self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
        table = hosts.definition(host)
        self.assertEqual(hosts.definition(peer), table)
        self.assertIn(cube, set(table.all_objects))
        self.assertNotIn(outside, set(table.all_objects))
        self.assertEqual(visible_part_locations("shelf"), [(-3.0, 4.0, 0.0), (10.0, 4.0, 0.0)])

    def test_occurrence_operations_reject_one_host_in_two_scenes(self):
        for kind, other_kind in (("NATIVE", "NODES"), ("NODES", "NATIVE")):
            with self.subTest(kind=kind):
                group = create_group([flat_part("shared_" + kind)], kind + " table", kind)
                src = hosts.definition(group)
                other = bpy.data.scenes.new("Other " + kind)
                other.collection.objects.link(group)
                bpy.context.view_layer.update()
                select([group])
                self.assertEqual(bpy.ops.groupmanager.fork(group_name="Local"), {"CANCELLED"})
                self.assertEqual(bpy.ops.groupmanager.switch_type(kind=other_kind, local=True), {"CANCELLED"})
                if kind == "NODES":
                    self.assertEqual(bpy.ops.groupmanager.realize(enabled=True, local=True), {"CANCELLED"})
                    self.assertFalse(hosts.get_realize(group))
                self.assertEqual(bpy.ops.groupmanager.dissolve(local=True), {"CANCELLED"})
                self.assertEqual(other.objects.get(group.name), group)
                self.assertEqual(hosts.definition(group), src)
                self.assertEqual(hosts.host_type(group), kind)

    def test_last_selected_group_stays_active_until_edit_ends(self):
        table = create_group([flat_part("leg")], "Table")
        lamp = create_group([flat_part("bulb", (3, 0, 0))], "Lamp")
        cup = create_group([flat_part("bowl", (6, 0, 0))], "Cup")
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        table.select_set(True)
        lamp.select_set(True)
        cup.select_set(True)
        bpy.context.view_layer.objects.active = cup
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context)[-1], cup)
        self.assertEqual(set(editor.peers(bpy.context)), {table, lamp})
        self.assertTrue(edit_layer.is_open(hosts.definition(table)))
        self.assertTrue(edit_layer.is_open(hosts.definition(lamp)))
        self.assertTrue(edit_layer.is_open(hosts.definition(cup)))

        spoon = flat_part("spoon", (1, 2, 0))
        spoon_world = spoon.matrix_world.translation.copy()
        select([spoon])
        self.assertEqual(bpy.ops.groupmanager.add_members(), {"FINISHED"})
        self.assertIn(spoon, set(editor.content(hosts.definition(cup)).objects))
        self.assertNotIn(spoon, set(editor.content(hosts.definition(table)).objects))
        self.assertAlmostEqual((spoon.matrix_world.translation - spoon_world).length, 0, places=4)

        bulb = next(obj for obj in editor.content(hosts.definition(lamp)).all_objects if obj.get("qa_role") == "bulb")
        select([bulb])
        editor.follow_selection(bpy.context)
        self.assertEqual(editor.path(bpy.context)[-1], lamp)
        bowl = next(obj for obj in editor.content(hosts.definition(cup)).all_objects if obj.get("qa_role") == "bowl")
        select([bowl])
        editor.follow_selection(bpy.context)
        self.assertEqual(editor.path(bpy.context)[-1], cup)

        leg = next(obj for obj in editor.content(hosts.definition(table)).all_objects if obj.get("qa_role") == "leg")
        select([leg])
        editor.follow_selection(bpy.context)
        self.assertEqual(editor.path(bpy.context)[-1], table)

        self.assertEqual(bpy.ops.groupmanager.back(), {"FINISHED"})
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(editor.peers(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(table)))
        self.assertFalse(edit_layer.is_open(hosts.definition(lamp)))
        self.assertFalse(edit_layer.is_open(hosts.definition(cup)))

    def test_editing_another_group_keeps_the_current_one_open(self):
        first = create_group([flat_part("leg")], "Table")
        second = create_group([flat_part("bulb", (4, 0, 0))], "Lamp")
        third = create_group([flat_part("bowl", (8, 0, 0))], "Cup")
        select([first])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([second])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context)[-1], first)
        self.assertEqual(editor.peers(bpy.context), [second])
        self.assertTrue(edit_layer.is_open(hosts.definition(first)))
        self.assertTrue(edit_layer.is_open(hosts.definition(second)))
        editor.activate_host(bpy.context, second)
        self.assertEqual(editor.path(bpy.context)[-1], second)
        self.assertEqual(editor.peers(bpy.context), [first])
        select([third])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context)[-1], second)
        self.assertEqual(set(editor.peers(bpy.context)), {first, third})
        self.assertTrue(edit_layer.is_open(hosts.definition(third)))
        self.assertEqual(bpy.ops.groupmanager.back(), {"FINISHED"})
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(editor.peers(bpy.context))

    def test_bounds_controls_activate_and_close_one_group(self):
        first = create_group([flat_part("leg")], "Table")
        second = create_group([flat_part("bulb", (4, 0, 0))], "Lamp")
        select([first])
        first.select_set(True)
        second.select_set(True)
        bpy.context.view_layer.objects.active = second
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        editor.activate_host(bpy.context, first)
        self.assertEqual(editor.path(bpy.context)[-1], first)
        self.assertEqual(editor.peers(bpy.context), [second])
        editor.close_edited(bpy.context, second)
        self.assertEqual(editor.path(bpy.context)[-1], first)
        self.assertFalse(editor.peers(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(second)))
        self.assertTrue(edit_layer.is_open(hosts.definition(first)))
        editor.close_edited(bpy.context, first)
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(first)))

    def test_selected_group_stays_boxed_until_activate(self):
        first = create_group([flat_part("leg")], "Table")
        second = create_group([flat_part("bulb", (4, 0, 0))], "Lamp")
        select([first])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([second])
        self.assertEqual(editor.preview_host(bpy.context), second)
        self.assertEqual(editor.path(bpy.context)[-1], first)
        self.assertFalse(edit_layer.is_open(hosts.definition(second)))
        other = flat_part("loose", (8, 0, 0))
        select([other])
        self.assertIsNone(editor.preview_host(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(second)))
        select([second])
        editor.close_edited(bpy.context, second)
        self.assertIsNone(editor.preview_host(bpy.context))
        select([second])
        editor.join_host(bpy.context, second)
        self.assertEqual(editor.path(bpy.context)[-1], first)
        self.assertEqual(editor.peers(bpy.context), [second])
        self.assertTrue(edit_layer.is_open(hosts.definition(second)))
        self.assertIsNone(editor.preview_host(bpy.context))
        editor.activate_host(bpy.context, second)
        self.assertEqual(editor.path(bpy.context)[-1], second)
        self.assertEqual(editor.peers(bpy.context), [first])
        self.assertTrue(edit_layer.is_open(hosts.definition(second)))
        self.assertIsNone(editor.preview_host(bpy.context))

    def test_edit_mode_origin_follows_the_cursor_or_selection(self):
        import bmesh
        part = flat_part("panel", (1, 2, 0))
        select([part])
        self.assertEqual(bpy.ops.object.mode_set(mode="EDIT"), {"FINISHED"})
        bm = bmesh.from_edit_mesh(part.data)
        bm.verts.ensure_lookup_table()
        before = [part.matrix_world @ vert.co.copy() for vert in bm.verts]
        bpy.context.scene.cursor.location = (4, 0, 1)
        self.assertEqual(bpy.ops.groupmanager.set_origin(mode="CURSOR"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        self.assertVectorClose(part.matrix_world.translation, (4, 0, 1))
        bm = bmesh.from_edit_mesh(part.data)
        bm.verts.ensure_lookup_table()
        for start, vert in zip(before, bm.verts):
            self.assertVectorClose(part.matrix_world @ vert.co, start)
        for vert in bm.verts:
            vert.select = False
        bm.verts[0].select = True
        bpy.context.tool_settings.mesh_select_mode = (True, False, False)
        bmesh.update_edit_mesh(part.data)
        target = part.matrix_world @ bm.verts[0].co
        before = [part.matrix_world @ vert.co.copy() for vert in bm.verts]
        self.assertEqual(bpy.ops.groupmanager.set_origin(mode="ACTIVE"), {"FINISHED"})
        self.assertVectorClose(part.matrix_world.translation, target)
        bm = bmesh.from_edit_mesh(part.data)
        bm.verts.ensure_lookup_table()
        for start, vert in zip(before, bm.verts):
            self.assertVectorClose(part.matrix_world @ vert.co, start)

    def test_edit_mode_opens_every_member_mesh(self):
        import bmesh
        loose = flat_part("loose", (8, 0, 0))
        select([loose])
        self.assertFalse(bpy.ops.groupmanager.set_mode.poll())
        self.assertEqual(bpy.context.mode, "OBJECT")
        leg = flat_part("leg")
        top = flat_part("top", (3, 0, 0))
        top_mesh = top.data
        index = max(range(len(top_mesh.vertices)), key=lambda i: top_mesh.vertices[i].co.x)
        original = top_mesh.vertices[index].co.x
        group = create_group([leg, top], "Table")
        select([group])
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="EDIT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        meshes = [obj for obj in editor.content(hosts.definition(group)).all_objects
                  if obj.type == "MESH" and not edit_layer.is_helper(obj)]
        self.assertEqual({obj.mode for obj in meshes}, {"EDIT"})
        self.assertEqual(set(bpy.context.selected_objects), set(meshes))
        edited = bmesh.from_edit_mesh(top_mesh)
        edited.verts.ensure_lookup_table()
        edited.verts[index].co.x = original + 0.5
        bmesh.update_edit_mesh(top_mesh)
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="OBJECT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "OBJECT")
        self.assertAlmostEqual(top_mesh.vertices[index].co.x, original + 0.5, places=4)
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(group)))

    def test_edit_mode_opens_a_selected_nested_group(self):
        bolt = flat_part("bolt")
        nested = create_group([bolt], "Bolt", "NODES")
        outer = create_group([nested, flat_part("leg")], "Table")
        select([outer])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([nested])
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="EDIT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        self.assertEqual(bolt.mode, "EDIT")
        self.assertEqual(editor.path(bpy.context), [outer, nested])
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="OBJECT"), {"FINISHED"})
        self.assertEqual(editor.path(bpy.context), [outer])

    def test_edit_mode_includes_every_selected_group(self):
        first = create_group([flat_part("leg")], "Table")
        second = create_group([flat_part("bulb", (4, 0, 0))], "Lamp")
        select([first, second])
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="EDIT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        meshes = []
        for host in (first, second):
            meshes.extend(obj for obj in editor.content(hosts.definition(host)).all_objects
                          if obj.type == "MESH" and not edit_layer.is_helper(obj))
        self.assertEqual({obj.mode for obj in meshes}, {"EDIT"})
        self.assertTrue(set(meshes) <= set(bpy.context.selected_objects))
        self.assertTrue(edit_layer.is_open(hosts.definition(first)))
        self.assertTrue(edit_layer.is_open(hosts.definition(second)))
        self.assertEqual(bpy.ops.groupmanager.set_mode(mode="OBJECT"), {"FINISHED"})
        self.assertEqual(bpy.context.mode, "OBJECT")
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(editor.peers(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(first)))
        self.assertFalse(edit_layer.is_open(hosts.definition(second)))

    def test_every_selected_group_shows_an_overlay_box(self):
        first = create_group([flat_part("leg")], "Table")
        second = create_group([flat_part("bulb", (4, 0, 0))], "Lamp")
        loose = flat_part("loose", (8, 0, 0))
        select([first, second, loose])
        def selected_and_open_frames():
            frames = editor._selected_boxes(bpy.context, []) + editor._open_boxes(bpy.context)
            return {host for host, _corners in frames}

        boxed = selected_and_open_frames()
        self.assertEqual(boxed, {first, second})
        self.assertFalse(editor.path(bpy.context))
        select([first])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([first, second, loose])
        boxed = selected_and_open_frames()
        self.assertEqual(boxed, {first, second})

    def test_shared_source_can_stay_open_together(self):
        board = flat_part("board", (1, 0, 0))
        host = create_group([board], "Crate")
        peer = linked_copy(host, 6)
        select([host])
        self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
        select([peer])
        self.assertEqual(editor.preview_host(bpy.context), peer)
        editor.join_host(bpy.context, peer)
        self.assertEqual(editor.path(bpy.context)[-1], host)
        self.assertEqual(editor.peers(bpy.context), [peer])
        self.assertIsNone(editor.preview_host(bpy.context))
        member = next(obj for obj in editor.content(hosts.definition(host)).all_objects if obj.get("qa_role") == "board")
        member.location.x += 0.5
        bpy.context.view_layer.update()
        self.assertEqual(len(visible_part_locations("board")), 2)
        editor.close_edited(bpy.context, peer)
        self.assertEqual(editor.path(bpy.context)[-1], host)
        self.assertFalse(editor.peers(bpy.context))
        self.assertTrue(edit_layer.is_open(hosts.definition(host)))
        editor.close_edited(bpy.context, host)
        self.assertFalse(editor.path(bpy.context))
        self.assertFalse(edit_layer.is_open(hosts.definition(host)))

    def test_apply_scale_and_location_keep_every_instance_still(self):
        board = flat_part("board", (1, 2, 0))
        host = create_group([board], "Crate")
        host.location = (4, -1, 2)
        host.rotation_euler.z = 0.4
        host.scale = (2, 3, 4)
        bpy.context.view_layer.update()
        peer = linked_copy(host, 12)
        peer.scale = (1, 1, 1)
        bpy.context.view_layer.update()
        before = visible_part_locations("board")
        select([host])
        self.assertEqual(bpy.ops.groupmanager.transform_apply(location=False, rotation=False, scale=True), {"FINISHED"})
        bpy.context.view_layer.update()
        self.assertVectorClose(host.scale, Vector((1, 1, 1)))
        self.assertEqual(visible_part_locations("board"), before)
        self.assertVectorClose(peer.scale, Vector((0.5, 1 / 3, 0.25)))
        select([host])
        self.assertEqual(bpy.ops.groupmanager.transform_apply(location=True, rotation=False, scale=False), {"FINISHED"})
        bpy.context.view_layer.update()
        self.assertVectorClose(host.location, Vector())
        self.assertEqual(visible_part_locations("board"), before)
        loose = flat_part("loose")
        loose.scale = (2, 2, 2)
        select([loose])
        self.assertEqual(bpy.ops.groupmanager.transform_apply(location=False, rotation=False, scale=True), {"FINISHED"})
        self.assertVectorClose(loose.scale, Vector((1, 1, 1)))

    def test_repeated_assets_scale(self):
        for size in (100, 1000):
            with self.subTest(size=size):
                bpy.ops.wm.open_mainfile(filepath=str(TEMPLATE))
                part = create_group([flat_part("bench")], "Bench")
                source = hosts.definition(part)
                started = time.perf_counter()
                rows = placements(part, size)
                created = time.perf_counter()
                self.assertEqual(count_uses(source), size)
                counted = time.perf_counter()
                before = len(bpy.data.meshes)
                select([rows[-1]])
                self.assertEqual(bpy.ops.groupmanager.edit(), {"FINISHED"})
                self.assertEqual(len(bpy.data.meshes), before)
                self.assertEqual(bpy.ops.groupmanager.back(all=True), {"FINISHED"})
                browsed = time.perf_counter()
                branches.fork_selected([rows[-1]], name="Single copy")
                forked = time.perf_counter()
                self.assertEqual(count_uses(source), size - 1)
                print("GROUPMANAGER_BENCH", {
                    "placements": size, "create_ms": round((created - started) * 1000, 2),
                    "count_ms": round((counted - created) * 1000, 2),
                    "select_enter_exit_ms": round((browsed - counted) * 1000, 2),
                    "unique_ms": round((forked - browsed) * 1000, 2),
                    "peak_process_mib": peak_memory_mib(),
                }, flush=True)


if __name__ == "__main__":
    if not bpy.app.background:
        raise RuntimeError("Use an independent Blender --background process for this suite.")
    scena.register()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GroupWorkflows)
    report = unittest.TextTestRunner(verbosity=2).run(suite)
    if not report.wasSuccessful():
        raise AssertionError("Scena workflow acceptance failed")
