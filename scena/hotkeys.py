"""Default shortcuts for Scena's 3D View object-mode tools."""

import bpy
from . import hosts


_bindings = []
_keymap_name = "Object Mode"
_menu_name = "GROUPMANAGER_MT_quick"
_cursor_menu_name = "GROUPMANAGER_MT_cursor"
_cursor_keymap_name = "3D View Generic"
_tool_pies = (
    ("SCENA_MT_save_pie", "Save Pie", "Window", 'S', True),
    ("SCENA_MT_shader_pie", "Shader Pie", "3D View", 'Z', False),
    ("SCENA_MT_transform_pie", "Transform Pie", "3D View", 'BUTTON4MOUSE', False),
)


def _menu_items(keymap, menu_name):
    if keymap is None:
        return []
    found = []
    for item in keymap.keymap_items:
        if item.idname == "wm.call_menu_pie" and item.properties.name == menu_name:
            found.append(item)
        elif menu_name == _menu_name and item.idname == GM_OT_open_pie.bl_idname:
            found.append(item)
    return found


def _pie_items(keymap):
    return _menu_items(keymap, _menu_name)


def _user_keymap(context, name=_keymap_name):
    config = context.window_manager.keyconfigs.user
    return config.keymaps.get(name) if config else None


def _cursor_enabled():
    from . import preferences
    prefs = preferences.get(bpy.context)
    return bool(prefs and prefs.use_cursor_origin)


def _cursor_entries():
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return []
    entries = []
    for keyconfig in (window_manager.keyconfigs.addon, window_manager.keyconfigs.user):
        if keyconfig is None:
            continue
        keymap = keyconfig.keymaps.get(_cursor_keymap_name)
        entries.extend((keymap, item) for item in _menu_items(keymap, _cursor_menu_name))
    return entries


def set_cursor_enabled(enabled):
    """Turn the Cursor and Origin pie on or off without removing its shortcut."""
    enabled = bool(enabled)
    for keymap, item in _cursor_entries():
        if item.active != enabled:
            item.active = enabled


def _draw_key(layout, keymap, item, label, *, enabled_by=None):
    row = layout.row(align=True)
    row.use_property_split = False
    row.context_pointer_set("keymap", keymap)
    owner, prop = enabled_by or (item, "active")
    row.prop(owner, prop, text=label, toggle=False)
    row.prop(item, "type", text="", full_event=item.map_type in {'KEYBOARD', 'MOUSE', 'NDOF'})
    if item.is_user_modified and not item.is_user_defined:
        row.operator("preferences.keyitem_restore", text="", icon='LOOP_BACK').item_id = item.id


def draw_group_shortcuts(layout, context):
    pie_map = _user_keymap(context)
    pie = layout.box()
    pie.label(text="Shortcuts", icon='KEYINGSET')
    if not pie_map or not _bindings:
        pie.label(text="Object Mode keymap is not loaded")
    else:
        items = _pie_items(pie_map)
        if items:
            for item in items:
                _draw_key(pie, pie_map, item, "Scena Pie")
        else:
            pie.operator("groupmanager.add_pie_shortcut", icon='ADD')
        for item in pie_map.keymap_items:
            if item.idname == "groupmanager.pick":
                _draw_key(pie, pie_map, item, "Pick")


def draw_tool_shortcuts(layout, context, prefs):
    pies = layout.box()
    pies.label(text="Pie Menus", icon='MENU_PANEL')
    for menu, label, map_name, _key, _ctrl in _tool_pies:
        keymap = _user_keymap(context, map_name)
        for item in _menu_items(keymap, menu):
            _draw_key(pies, keymap, item, label)

    cursor_map = _user_keymap(context, _cursor_keymap_name)
    items = _menu_items(cursor_map, _cursor_menu_name)
    for item in items:
        _draw_key(pies, cursor_map, item, "Cursor and Origin", enabled_by=(prefs, "use_cursor_origin"))
    if not items:
        pies.operator("groupmanager.add_cursor_shortcut", icon='ADD')

    keymap = _user_keymap(context, "Outliner")
    if keymap:
        outliner = layout.box()
        outliner.label(text="Outliner", icon='OUTLINER')
        for item in keymap.keymap_items:
            if item.idname == "scena.collect_selected":
                _draw_key(outliner, keymap, item, "New Collection from Selection")


class GM_OT_open_pie(bpy.types.Operator):
    bl_idname = "groupmanager.open_pie"
    bl_label = "Scena Pie"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return bpy.ops.wm.call_menu_pie('INVOKE_DEFAULT', name=_menu_name)


class GM_OT_add_pie_shortcut(bpy.types.Operator):
    bl_idname = "groupmanager.add_pie_shortcut"
    bl_label = "Add Pie Shortcut (E)"
    bl_description = "Restore Scena's object-mode shortcut in the current keymap"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        keymap = _user_keymap(context)
        if not keymap or not _bindings:
            return {'CANCELLED'}
        if not _pie_items(keymap):
            # User-defined items survive disabling an extension. An extension-owned
            # operator lets Blender skip this shortcut when Scena is disabled.
            keymap.keymap_items.new(GM_OT_open_pie.bl_idname, 'E', 'PRESS', head=True)
            context.preferences.is_dirty = True
        return {'FINISHED'}


class GM_OT_add_cursor_shortcut(bpy.types.Operator):
    bl_idname = "groupmanager.add_cursor_shortcut"
    bl_label = "Add Cursor and Origin Shortcut"
    bl_description = "Restore the Shift+S Cursor and Origin pie in the current keymap"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from . import preferences
        keymap = _user_keymap(context, _cursor_keymap_name)
        if keymap is None:
            return {'CANCELLED'}
        if not _menu_items(keymap, _cursor_menu_name):
            item = keymap.keymap_items.new("wm.call_menu_pie", 'S', 'PRESS', shift=True)
            item.properties.name = _cursor_menu_name
        prefs = preferences.get(context)
        if prefs:
            prefs.use_cursor_origin = True
        set_cursor_enabled(True)
        context.preferences.is_dirty = True
        return {'FINISHED'}


class GM_OT_open_preferences(bpy.types.Operator):
    bl_idname = "groupmanager.open_preferences"
    bl_label = "Open Scena Preferences"
    bl_description = "Open Scena in Blender Preferences"

    @classmethod
    def poll(cls, context):
        return not bpy.app.background and __package__ in context.preferences.addons

    def execute(self, context):
        context.preferences.addons[__package__].preferences.settings = 'GROUP'
        return bpy.ops.preferences.addon_show(module=__package__)


class GM_OT_group_local_view(bpy.types.Operator):
    bl_idname = "groupmanager.local_view"
    bl_label = "Local View for Groups"
    bl_description = "Isolate the selection and frame the visible group instances"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT'
                and any(hosts.definition(obj) for obj in context.selected_objects))

    def execute(self, context):
        space = context.space_data
        if space.local_view:
            return bpy.ops.view3d.localview()
        # Frame Selected includes evaluated instances. Local View alone measures their carriers.
        result = bpy.ops.view3d.localview(frame_selected=False)
        if 'FINISHED' in result:
            for region in context.area.regions:
                if region.type != 'WINDOW':
                    continue
                with context.temp_override(region=region):
                    view = region.data
                    if view.view_perspective == 'CAMERA':
                        camera = space.camera or context.scene.camera
                        view.view_perspective = 'ORTHO' if camera and camera.data.type == 'ORTHO' else 'PERSP'
                    if bpy.ops.view3d.view_selected.poll():
                        bpy.ops.view3d.view_selected('INVOKE_DEFAULT')
        return result


def _local_view_menu(self, context):
    if GM_OT_group_local_view.poll(context):
        self.layout.operator(GM_OT_group_local_view.bl_idname)


def register():
    bpy.types.VIEW3D_MT_view_local.prepend(_local_view_menu)
    config = bpy.context.window_manager.keyconfigs.addon
    if config is None or _bindings:
        return
    keymap = config.keymaps.new(name=_keymap_name, space_type='EMPTY', region_type='WINDOW')
    item = keymap.keymap_items.new("wm.call_menu_pie", 'E', 'PRESS', head=True)
    item.properties.name = _menu_name
    _bindings.append((keymap, item))
    pick = keymap.keymap_items.new("groupmanager.pick", 'E', 'PRESS', alt=True)
    _bindings.append((keymap, pick))
    outliner = config.keymaps.new(name="Outliner", space_type='OUTLINER', region_type='WINDOW')
    collect = outliner.keymap_items.new("scena.collect_selected", 'G', 'PRESS', ctrl=True)
    collect.active = False
    _bindings.append((outliner, collect))
    for menu, _label, map_name, key, ctrl in _tool_pies:
        tool_map = config.keymaps.new(name=map_name, space_type='EMPTY' if map_name == "Window" else 'VIEW_3D')
        item = tool_map.keymap_items.new("wm.call_menu_pie", key, 'PRESS', ctrl=ctrl)
        item.properties.name = menu
        item.active = False
        _bindings.append((tool_map, item))
    for clipboard_map in (keymap, outliner):
        for key, operator in (('C', "scena.copy_groups"), ('V', "scena.paste_groups")):
            # Blender prepends add-on entries when assembling the effective user map.
            item = clipboard_map.keymap_items.new(operator, key, 'PRESS', ctrl=True)
            _bindings.append((clipboard_map, item))
    for key in ('SLASH', 'NUMPAD_SLASH'):
        item = keymap.keymap_items.new(GM_OT_group_local_view.bl_idname, key, 'PRESS', head=True)
        _bindings.append((keymap, item))
    step = keymap.keymap_items.new("groupmanager.deep_edit", 'LEFTMOUSE', 'CLICK', ctrl=True, head=True)
    step.properties.deep = False
    _bindings.append((keymap, step))
    deep = keymap.keymap_items.new("groupmanager.deep_edit", 'LEFTMOUSE', 'DOUBLE_CLICK', ctrl=True, head=True)
    deep.properties.deep = False
    _bindings.append((keymap, deep))
    # An empty first click can clear the selection. Listen on the viewport itself.
    viewport = config.keymaps.new(name="3D View", space_type='VIEW_3D', region_type='WINDOW')
    back = viewport.keymap_items.new("groupmanager.back", 'LEFTMOUSE', 'DOUBLE_CLICK', head=True)
    back.properties.steps = 1
    back.properties.all = False
    _bindings.append((viewport, back))
    front = keymap.keymap_items.new("groupmanager.select_front_member", 'LEFTMOUSE', 'CLICK', head=True)
    _bindings.append((keymap, front))
    shift_copy = keymap.keymap_items.new("groupmanager.duplicate_move", 'D', 'PRESS', shift=True, head=True)
    shift_copy.properties.kind = 'GROUP'
    _bindings.append((keymap, shift_copy))
    linked_copy = keymap.keymap_items.new("groupmanager.duplicate_move", 'D', 'PRESS', alt=True, head=True)
    linked_copy.properties.kind = 'INSTANCE'
    _bindings.append((keymap, linked_copy))
    nested_copy = keymap.keymap_items.new("groupmanager.duplicate_move", 'D', 'PRESS', shift=True, alt=True, head=True)
    nested_copy.properties.kind = 'NESTED'
    _bindings.append((keymap, nested_copy))

    # This map is handled before the built-in 3D View snap pie.
    cursor_keymap = config.keymaps.new(name=_cursor_keymap_name, space_type='VIEW_3D', region_type='WINDOW')
    cursor_item = cursor_keymap.keymap_items.new("wm.call_menu_pie", 'S', 'PRESS', shift=True)
    cursor_item.properties.name = _cursor_menu_name
    cursor_item.active = _cursor_enabled()
    _bindings.append((cursor_keymap, cursor_item))
    set_cursor_enabled(cursor_item.active)


def unregister():
    bpy.types.VIEW3D_MT_view_local.remove(_local_view_menu)
    for keymap, item in _bindings:
        keymap.keymap_items.remove(item)
    _bindings.clear()


CLASSES = (GM_OT_open_pie, GM_OT_add_pie_shortcut, GM_OT_add_cursor_shortcut,
           GM_OT_open_preferences, GM_OT_group_local_view)
