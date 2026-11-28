"""Scena operator for explicitly running a Hard Ops tool on a prepared group."""

import bpy
from bpy.props import EnumProperty

_actions = (
    ('SOLIDIFY', "Solidify", 'add_mod_solidify'),
    ('ARRAY', "Array", 'add_mod_array'),
    ('DIFFERENCE', "Difference", 'bool_difference'),
)


class SCENA_OT_hard_ops_modifier(bpy.types.Operator):
    bl_idname = "scena.hard_ops_modifier"
    bl_label = "Hard Ops Modifier"
    bl_options = {'UNDO'}

    action: EnumProperty(items=[(key, label, "") for key, label, _ in _actions])

    @classmethod
    def poll(cls, context):
        return bpy.ops.groupmanager.prepare_hard_ops.poll()

    def _prepare(self, context, method):
        if method not in dir(bpy.ops.hops):
            self.report({'WARNING'}, f"Hard Ops operator hops.{method} is unavailable")
            return False
        selected = [obj for obj in context.selected_objects if obj != context.active_object]
        if self.action == 'DIFFERENCE' and not any(obj.type == 'MESH' for obj in selected):
            self.report({'WARNING'}, "Select a mesh cutter and make the group active")
            return False
        if bpy.ops.groupmanager.prepare_hard_ops() != {'FINISHED'}:
            return False
        for obj in selected:
            obj.select_set(True)
        return True

    def invoke(self, context, event):
        if self.action != 'ARRAY':
            return self.execute(context)
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Start Array in a 3D View")
            return {'CANCELLED'}
        if not self._prepare(context, 'st3_array'):
            return {'CANCELLED'}
        # Array V2 reads the prepared placement's dimensions on invoke.
        context.view_layer.update()
        status = bpy.ops.hops.st3_array('INVOKE_REGION_WIN')
        # Hard Ops owns the modal handler and the undo step on confirmation.
        return {'INTERFACE'} if 'RUNNING_MODAL' in status else status

    def execute(self, context):
        method = next(method for key, _, method in _actions if key == self.action)
        if not self._prepare(context, method):
            return {'CANCELLED'}
        options = {'operator': 'DIFFERENCE'} if self.action == 'DIFFERENCE' else {}
        return getattr(bpy.ops.hops, method)(**options)


def register():
    bpy.utils.register_class(SCENA_OT_hard_ops_modifier)


def unregister():
    bpy.utils.unregister_class(SCENA_OT_hard_ops_modifier)
