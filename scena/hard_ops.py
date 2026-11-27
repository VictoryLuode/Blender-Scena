"""Run Hard Ops modifiers on an instance-output placement from its Q menu."""

import bpy
from bpy.props import EnumProperty

from . import hosts


_actions = (
    ('SOLIDIFY', "Solidify", 'add_mod_solidify'),
    ('ARRAY', "Array", 'add_mod_array'),
    ('DIFFERENCE', "Difference", 'bool_difference'),
)
_menu = None


class SCENA_OT_hard_ops_modifier(bpy.types.Operator):
    bl_idname = "scena.hard_ops_modifier"
    bl_label = "Hard Ops Modifier"
    bl_options = {'UNDO'}

    action: EnumProperty(items=[(key, label, "") for key, label, _ in _actions])

    @classmethod
    def poll(cls, context):
        return bpy.ops.groupmanager.prepare_hard_ops.poll()

    def execute(self, context):
        selected = [obj for obj in context.selected_objects if obj != context.active_object]
        if self.action == 'DIFFERENCE' and not any(obj.type == 'MESH' for obj in selected):
            self.report({'WARNING'}, "Select a mesh cutter and make the group active")
            return {'CANCELLED'}
        method = next(method for key, _, method in _actions if key == self.action)
        operator = getattr(bpy.ops.hops, method)
        if method not in dir(bpy.ops.hops):
            self.report({'WARNING'}, "Enable Hard Ops first")
            return {'CANCELLED'}
        if bpy.ops.groupmanager.prepare_hard_ops() != {'FINISHED'}:
            return {'CANCELLED'}
        for obj in selected:
            obj.select_set(True)
        options = {'operator': 'DIFFERENCE'} if self.action == 'DIFFERENCE' else {}
        return operator(**options)


def _draw(self, context):
    if not SCENA_OT_hard_ops_modifier.poll(context) or hosts.get_realize(context.active_object):
        return
    for key, label, _ in _actions:
        self.layout.operator("scena.hard_ops_modifier", text=label).action = key
    self.layout.separator()


def _bind():
    global _menu
    menu = getattr(bpy.types, "HOPS_MT_MainMenu", None)
    if menu is not _menu:
        if menu:
            menu.prepend(_draw)
        _menu = menu
    return 2.0


def register():
    bpy.utils.register_class(SCENA_OT_hard_ops_modifier)
    _bind()
    bpy.app.timers.register(_bind, first_interval=2.0, persistent=True)


def unregister():
    global _menu
    if bpy.app.timers.is_registered(_bind):
        bpy.app.timers.unregister(_bind)
    if _menu and _menu is getattr(bpy.types, "HOPS_MT_MainMenu", None):
        _menu.remove(_draw)
    _menu = None
    bpy.utils.unregister_class(SCENA_OT_hard_ops_modifier)
