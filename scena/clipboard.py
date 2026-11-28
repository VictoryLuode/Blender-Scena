"""Copy selected Group hosts through Blender's object clipboard."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import bpy
from mathutils import Matrix

from . import branches, creation, editor, hosts
from .exchange_ids import new_id


_PREFIX = ".Scena Clipboard "
_SESSION = "scena.clipboard.session"


def _buffer():
    return Path(bpy.app.tempdir).parent / "copybuffer.blend"


def _copy(context, selected):
    world = {obj: obj.matrix_world.copy() for obj in selected}
    active = context.active_object
    # Peer previews are temporary objects, rebuilt when the edit path reopens.
    roots = [(obj.get("_gm_mirror_of") or obj, world[obj], obj == active) for obj in selected]
    path = _buffer()
    with editor.source_space(context):
        bundle = bpy.data.collections.new(_PREFIX + uuid4().hex)
        try:
            bundle["session"] = bpy.app.driver_namespace.setdefault(_SESSION, uuid4().hex)
            entries = {}
            groups = set()
            for index, (obj, matrix, is_active) in enumerate(roots):
                entries[str(index)] = {"object": obj, "world": [v for row in matrix for v in row],
                                       "active": is_active}
                if hosts.definition(obj):
                    groups.add(obj)
            # Direct host dependencies include Group Data, GN trees and modifier targets.
            # Storing ID pointers keeps the snapshot portable if a local ID disappears.
            refs = {str(data.session_uid): data for data, users in bpy.data.user_map().items()
                    if not groups.isdisjoint(users) and data not in selected}
            bundle["roots"] = entries
            bundle["references"] = refs
            with NamedTemporaryFile(dir=path.parent, suffix=".blend", delete=False) as tmp:
                temporary = Path(tmp.name)
            try:
                bpy.data.libraries.write(str(temporary), {bundle}, path_remap='ABSOLUTE')
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            bpy.data.collections.remove(bundle)


def _discard_unused(before, roots):
    users = bpy.data.user_map()
    added = users.keys() - before
    dependencies = {}
    keep = set(roots)
    for data in added:
        for user in users[data]:
            if user in added:
                dependencies.setdefault(user, []).append(data)
            else:
                keep.add(data)
    pending = list(keep)
    while pending:
        for data in dependencies.get(pending.pop(), ()):
            if data not in keep:
                keep.add(data)
                pending.append(data)
    if unused := added - keep:
        bpy.data.batch_remove(ids=unused)


def _paste(context, path, name):
    target = creation._link_target(context)
    inverse = editor.frame(context).inverted()
    with editor.source_space(context):
        before = set(bpy.data.user_map())
        try:
            with bpy.data.libraries.load(str(path)) as (_, out):
                out.collections = [name]
            bundle = out.collections[0]
            if bundle is None:
                raise ValueError("The object clipboard changed. Paste again")
            if bundle["session"] == bpy.app.driver_namespace.get(_SESSION):
                local = {data.session_uid: data for data in before}
                for uid, imported in list(bundle["references"].items()):
                    original = local.get(int(uid))
                    if original and imported not in before and imported.id_type == original.id_type:
                        imported.user_remap(original)
            entries = list(bundle["roots"].values())
            objects = [entry["object"] for entry in entries]
            with branches.BranchChanges() as changes:
                for obj in objects:
                    if obj in before:
                        raise ValueError("The clipboard object could not be copied locally")
                    new_id(obj)
                    if hosts.definition(obj):
                        changes.duplicate_group_data(obj)
            creation.ensure_no_cycle(target, objects)
            active = objects[-1]
            for entry, obj in zip(entries, objects):
                if obj.parent not in objects:
                    obj.parent = None
                    values = entry["world"]
                    obj.matrix_world = inverse @ Matrix([values[i:i+4] for i in range(0, 16, 4)])
                target.objects.link(obj)
                if entry["active"]:
                    active = obj
            bpy.data.collections.remove(bundle)
            _discard_unused(before, objects)
        except Exception:
            added = bpy.data.user_map().keys() - before
            if added:
                bpy.data.batch_remove(ids=added)
            raise
    creation.select_result(context, active)
    for obj in objects:
        if context.view_layer.objects.get(obj.name) == obj:
            obj.select_set(True)


def _clipboard_area(context):
    return (context.mode == 'OBJECT' and context.area and context.region
            and context.region.type == 'WINDOW' and context.area.type in {'VIEW_3D', 'OUTLINER'})


def _selected(context):
    if context.area.type == 'OUTLINER':
        ids = context.selected_ids
        if any(not isinstance(item, bpy.types.Object) for item in ids):
            return []
        return ids
    return context.selected_objects


class SCENA_OT_copy_groups(bpy.types.Operator):
    bl_idname = "scena.copy_groups"
    bl_label = "Copy Group Objects"

    @classmethod
    def poll(cls, context):
        return _clipboard_area(context) and any(hosts.definition(obj) for obj in _selected(context))

    def execute(self, context):
        try:
            _copy(context, list(_selected(context)))
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class SCENA_OT_paste_groups(bpy.types.Operator):
    bl_idname = "scena.paste_groups"
    bl_label = "Paste Group Objects"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return _clipboard_area(context)

    def execute(self, context):
        path = _buffer()
        if not path.is_file():
            return {'PASS_THROUGH'}
        try:
            with bpy.data.libraries.load(str(path)) as (src, _):
                name = next((s for s in src.collections if s.startswith(_PREFIX) and
                             len(s) == len(_PREFIX) + 32), None)
            if name is None:
                return {'PASS_THROUGH'}
            _paste(context, path, name)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (SCENA_OT_copy_groups, SCENA_OT_paste_groups)
