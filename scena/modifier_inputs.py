"""Expose evaluated mesh layer names to native modifiers on a Scena placement."""

import bpy
from bpy.app.handlers import persistent

from . import hosts


_pending = {}


def _plan(obj, depsgraph):
    if (obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.override_library
            or not obj.data or obj.data.vertices or not obj.modifiers):
        return
    source = hosts._source_modifier(obj)
    if not source:
        return
    if obj.modifiers[0] != source:
        return obj.modifiers.find(source.name), (), ()
    if not hosts._mesh_output(source):
        return
    evaluated = obj.evaluated_get(depsgraph)
    if evaluated.data and evaluated.is_evaluated:
        groups = tuple(g.name for g in evaluated.vertex_groups if g.name not in obj.vertex_groups)
        uvs = tuple(layer.name for layer in evaluated.data.uv_layers if layer.name not in obj.data.uv_layers)
        if groups or uvs:
            return 0, groups, uvs


def _inspect(obj, depsgraph, *, fix_order=False):
    plan = _plan(obj, depsgraph)
    if plan:
        if fix_order and plan[0] > 0:
            # Blender inserts Hook before the generator. Correct the order during
            # this update so the first target move already sees realized geometry.
            obj.modifiers.move(plan[0], 0)
        _pending[obj.session_uid] = obj.name


def _prepare(obj, depsgraph):
    plan = _plan(obj, depsgraph)
    if not plan:
        return
    index, groups, uvs = plan
    if index > 0:
        # This generator has no geometry input. Anything above it would be discarded.
        obj.modifiers.move(index, 0)
        return
    # Only the empty carrier needs names for Blender's pickers. Keep linked peers
    # independent and let Geometry Nodes continue to provide the actual weights/UVs.
    if obj.data.users > 1 or obj.data.library:
        obj.data = obj.data.copy()
    active_group = obj.vertex_groups.active_index
    for name in groups:
        obj.vertex_groups.new(name=name)
    if groups and active_group >= 0:
        obj.vertex_groups.active_index = active_group
    for name in uvs:
        if not obj.data.uv_layers.new(name=name, do_init=False):
            break


def _flush():
    batch = _pending.copy()
    _pending.clear()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for uid, name in batch.items():
        obj = bpy.data.objects.get(name)
        if obj and obj.session_uid == uid:
            _prepare(obj, depsgraph)
    return 0.0 if _pending else None


@persistent
def _updated(_scene, depsgraph):
    if depsgraph.mode != 'VIEWPORT':
        return
    # Changing the stack can invalidate the depsgraph's update iterator.
    objects = [update.id.original for update in depsgraph.updates
               if update.is_updated_geometry and isinstance(update.id, bpy.types.Object)]
    for obj in objects:
        _inspect(obj, depsgraph, fix_order=True)
    if _pending and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush)


def refresh():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in bpy.context.view_layer.objects:
        _inspect(obj, depsgraph)
    if _pending and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush)


def register():
    bpy.app.handlers.depsgraph_update_post.append(_updated)


def unregister():
    bpy.app.handlers.depsgraph_update_post.remove(_updated)
    if bpy.app.timers.is_registered(_flush):
        bpy.app.timers.unregister(_flush)
    _pending.clear()
