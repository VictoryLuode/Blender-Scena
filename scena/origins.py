"""Use the Object menus' Set Origin commands on Scena's shared collection data."""

import bpy
from bpy.props import EnumProperty

from . import edit_layer, hosts, menu_hooks, modeling


def _native_items(prop):
    return [(item.identifier, item.name, item.description)
            for item in bpy.ops.object.origin_set.get_rna_type().properties[prop].enum_items]


def _geometry_center(context, source, operation, center):
    # Measure the shared geometry in collection space, independently of the
    # placement's modifier stack. Blender calculates the median, bounds or mass.
    probe = hosts.make_host("Scena Origin Measurement", source, 'NODES')
    try:
        context.scene.collection.objects.link(probe)
        hosts._socket(hosts._source_modifier(probe), hosts.REALIZE_SOCKET).value = True
        probe.update_tag()
        graph = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(probe.evaluated_get(graph), depsgraph=graph)
        empty = probe.data
        probe.modifiers.clear()
        probe.data = mesh
        bpy.data.meshes.remove(empty)
        if not mesh.vertices:
            raise ValueError("The group has no mesh vertices to measure")
        if operation in {'ORIGIN_CENTER_OF_MASS', 'ORIGIN_CENTER_OF_VOLUME'} and not mesh.polygons:
            raise ValueError("Center of mass needs mesh faces")
        with context.temp_override(object=probe, active_object=probe, selected_objects=[probe],
                                   selected_editable_objects=[probe]):
            if bpy.ops.object.origin_set(type=operation, center=center) != {'FINISHED'}:
                raise ValueError("Blender could not measure the group origin")
        return probe.location.copy()
    finally:
        hosts._remove_host(probe)


class SCENA_OT_group_origin_set(bpy.types.Operator):
    bl_idname = "scena.group_origin_set"
    bl_label = "Set Origin"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(items=_native_items("type"), name="Type")
    center: EnumProperty(items=_native_items("center"), name="Center", default='MEDIAN')

    def execute(self, context):
        selected = list(context.selected_editable_objects)
        active = context.active_object
        if active in selected:
            selected.remove(active)
            selected.insert(0, active)
        seen = set()
        plain = []
        changed = False
        errors = []
        for obj in selected:
            source = hosts.definition(obj)
            if not source:
                plain.append(obj)
                continue
            if source in seen:
                continue
            seen.add(source)
            try:
                if edit_layer.is_open(source):
                    raise ValueError("Close the group before changing its shared origin")
                modeling._require_local(source)
                if self.type == 'ORIGIN_CURSOR':
                    point = context.scene.cursor.location.copy()
                else:
                    measure = 'ORIGIN_GEOMETRY' if self.type == 'GEOMETRY_ORIGIN' else self.type
                    offset = _geometry_center(context, source, measure, self.center)
                    if self.type == 'GEOMETRY_ORIGIN':
                        source.instance_offset += offset
                        changed = True
                        continue
                    point = obj.matrix_world @ offset
                modeling.reframe(obj, context.scene.cursor, world_point=point)
                changed = True
            except (RuntimeError, ValueError) as exc:
                errors.append(f"{obj.name}: {exc}")
        if plain:
            with context.temp_override(object=plain[0], active_object=plain[0],
                                       selected_objects=plain, selected_editable_objects=plain):
                changed |= bpy.ops.object.origin_set(type=self.type, center=self.center) == {'FINISHED'}
        if errors:
            self.report({'WARNING'}, " / ".join(errors))
        return {'FINISHED'} if changed else {'CANCELLED'}


class _OriginLayout(menu_hooks.Layout):
    """Forward the standard menu layout, routing just its Set Origin submenu."""

    def operator_menu_enum(self, operator, *args, **kwargs):
        if operator == "object.origin_set":
            operator = SCENA_OT_group_origin_set.bl_idname
        return self._layout.operator_menu_enum(operator, *args, **kwargs)


def _group_selected(context):
    return context.mode == 'OBJECT' and any(hosts.definition(obj) for obj in context.selected_objects)


_menu_hooks = []


def register():
    for menu in (bpy.types.VIEW3D_MT_object, bpy.types.VIEW3D_MT_object_context_menu):
        hook = menu_hooks.install(menu, _OriginLayout, _group_selected)
        if hook:
            _menu_hooks.append(hook)


def unregister():
    for hook in _menu_hooks:
        menu_hooks.restore(hook)
    _menu_hooks.clear()


CLASSES = (SCENA_OT_group_origin_set,)
