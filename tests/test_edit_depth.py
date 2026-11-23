"""Run with Blender 5.3 in background mode and factory settings."""

from pathlib import Path
import sys
import unittest

import bpy


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scena
from scena import editor, hosts


def native_chain(depth):
    leaf = bpy.data.collections.new("Leaf")
    hosts.mark_definition(leaf)
    mesh = bpy.data.meshes.new("Triangle")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    member = bpy.data.objects.new("Bottom member", mesh)
    leaf.objects.link(member)
    source = leaf
    children = []
    for level in range(1, depth):
        parent = bpy.data.collections.new(f"Level {level}")
        hosts.mark_definition(parent)
        child = hosts.make_host(f"Inner {level}", source, 'NATIVE')
        parent.objects.link(child)
        children.append(child)
        source = parent
    root = hosts.make_host("Placement", source, 'NATIVE')
    bpy.context.scene.collection.objects.link(root)
    bpy.context.view_layer.update()
    return [root, *reversed(children)], member


def occurrence_count(obj):
    bpy.context.view_layer.update()
    return sum(item.object.original == obj for item in
               bpy.context.evaluated_depsgraph_get().object_instances)


class DepthGuardTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scena.register()

    def tearDown(self):
        if editor.path(bpy.context):
            editor.back(bpy.context, all_levels=True)
        scena.unregister()

    def test_block_seventh_level_compensation_but_allow_unrelated_group(self):
        route, member = native_chain(7)
        shallow, _ = native_chain(1)
        self.assertEqual(occurrence_count(member), 1)
        with self.assertRaisesRegex(ValueError, '7'):
            editor.open_path(bpy.context, route[:1])
        self.assertFalse(editor.path(bpy.context))
        self.assertEqual(occurrence_count(member), 1)
        editor.open_path(bpy.context, shallow)
        self.assertEqual(editor.path(bpy.context), shallow)

    def test_rejected_nested_open_restores_current_edit_path(self):
        route, member = native_chain(6)
        editor.open_path(bpy.context, route[:1])
        self.assertEqual(occurrence_count(member), 1)
        with self.assertRaisesRegex(ValueError, '7'):
            editor.open_path(bpy.context, route[:2])
        self.assertEqual(editor.path(bpy.context), route[:1])
        self.assertEqual(occurrence_count(member), 1)


if __name__ == "__main__":
    if not bpy.app.background:
        raise RuntimeError("Depth tests require a dedicated background Blender process")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DepthGuardTests)
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        raise RuntimeError("Instance depth regression")
