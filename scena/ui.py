from functools import wraps
from types import SimpleNamespace

import blf
import bpy
from bpy.props import BoolProperty, PointerProperty

from . import creation, editor, hosts, modeling, operations, preferences, usage


class Sidebar:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Item"


class ObjectProperties:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'object'


def _full_row(layout):
    """Text button plus a right icon, both rows sharing the same full width."""
    row = layout.row(align=True)
    return row.split(factor=0.86, align=True)


def create_group_row(layout, context):
    settings = creation.current_settings(context)
    kind_icon = 'GEOMETRY_NODES' if settings.kind == 'NODES' else 'OUTLINER_COLLECTION'
    split = _full_row(layout)
    split.operator("groupmanager.create", text="Create Group", icon='NEWFOLDER')
    split.operator("groupmanager.cycle_create_kind", text="", icon=kind_icon)


def clear_controls(layout, context):
    split = _full_row(layout)
    split.operator_context = 'INVOKE_REGION_WIN'
    split.operator("groupmanager.dissolve", icon='GROUP')
    split.prop(context.window_manager, "gm_keep_group_modifiers", text="", icon='MODIFIER', toggle=True)


def _shown_group(context):
    """The group whose details Info should show."""
    obj = context.active_object
    if obj is not None and hosts.definition(obj):
        return obj
    route = editor.path(context)
    return route[-1] if route else obj


class SCENA_MT_group_data(bpy.types.Menu):
    bl_idname = "SCENA_MT_group_data"
    bl_label = "Browse Group Data"
    bl_options = {'SEARCH_ON_KEY_PRESS'}

    def draw(self, context):
        self.layout.operator_context = 'EXEC_DEFAULT'
        self.layout.operator_enum("groupmanager.choose_data", "definition")


def _draw_group_data(field, context, group, source):
    count = sum(usage.counts(source).values())
    parts = field.column(align=True)
    data = parts.row(align=True)
    browse = data.row(align=True)
    browse.enabled = context.active_object == group and bpy.ops.groupmanager.choose_data.poll()
    browse.ui_units_x = 1.7
    browse.menu(SCENA_MT_group_data.bl_idname, text=" ", icon='OUTLINER_COLLECTION')
    data.prop(source, "name", text="")

    data.operator("groupmanager.usages", text=str(count)).definition_uid = str(source.session_uid)
    keep = data.row(align=True)
    keep.ui_units_x = 1.0
    keep.prop(source, "use_fake_user", text="", toggle=True)
    if context.active_object == group and context.mode == 'OBJECT' and editor.in_scope(context, group) and count > 1:
        parts.operator("groupmanager.make_single_user", text="Make Group Unique").scope = 'OBJECT'


def _group_info(layout, context, *, fit_panel=None):
    active = context.active_object
    if active and not active.select_get():
        active = None
    space = context.space_data
    pinned = (space and space.type == 'PROPERTIES' and space.use_pin_id
              and isinstance(space.pin_id, bpy.types.Object))
    if pinned:
        active = space.pin_id
    definition = hosts.definition(active)
    route = editor.path(context) if not pinned and not definition else []
    group = active if definition else (route[-1] if route else None)
    source = definition or hosts.definition(group)

    data_prop = None
    image_empty = False
    if active and not definition:
        image_empty = active.type == 'EMPTY' and active.empty_display_type == 'IMAGE'
        if active.data or image_empty:
            data_prop = "data"
        elif active.instance_type == 'COLLECTION':
            data_prop = "instance_collection"
    data = getattr(active, data_prop) if data_prop else source
    blf.size(0, context.preferences.ui_styles[0].widget.points)
    name_width = blf.dimensions(0, active.name)[0] / 20 if active else 0
    if fit_panel is not None:
        data_width = blf.dimensions(0, data.name)[0] / 20 if data else 0
        source_width = blf.dimensions(0, source.name)[0] / 20 if source else 0
        # Leave space for the breadcrumb icons and the data selector's user count.
        min_width = 12 if context.active_object else 9.5
        fit_panel.ui_units_x = max(min_width, 5.5 + name_width + data_width, (3 + source_width) / 0.76 + 1)
    column = layout.column()
    path = column.row(align=True)
    path.alignment = 'LEFT'
    if active is not None:
        path.label(text="", icon='OBJECT_DATA')
        name_field = path.row(align=True)
        # Give the editable name its own bounds in both panels and pie menus.
        name_field.ui_units_x = 1.15 + name_width
        name_field.prop(active, "name", text="", emboss=False)
        if data is not None:
            arrow = path.row(align=True)
            arrow.ui_units_x = 0.85
            arrow.label(text="", icon='RIGHTARROW')
            icon = bpy.types.ID.bl_rna.properties["id_type"].enum_items[data.id_type].icon
            if active.type in {'FONT', 'SURFACE'}:
                icon = 'FONT_DATA' if active.type == 'FONT' else 'SURFACE_DATA'
            path.label(text=data.name, icon=icon)
    else:
        path.label(text="No active object", icon='OBJECT_DATA')
    if data_prop:
        field = column.row(align=True)
        field.use_property_split = False
        field.use_property_decorate = False
        if image_empty:
            field.template_ID(active, data_prop, open="image.open", unlink="object.unlink_data")
        else:
            field.template_ID(active, data_prop)
    if source:
        field = column.row(align=True)
        field.use_property_split = False
        field.use_property_decorate = False
        _draw_group_data(field, context, group, source)
    elif not data_prop:
        field = column.row()
        field.enabled = False
        field.label(text="—", icon='LINKED')


def _active_object(context):
    return context.active_object


def _visibility_buttons(layout, context):
    obj = _active_object(context)
    if obj is None:
        return
    row = layout.row(align=True)
    row.use_property_split = False
    row.use_property_decorate = False
    row.emboss = 'NORMAL'
    row.ui_units_x = 6
    row.prop(obj, "hide_select", text="", icon='RESTRICT_SELECT_OFF', toggle=True)
    hidden = obj.hide_get()
    row.operator("groupmanager.toggle_viewport_hide", text="",
                 icon='HIDE_ON' if hidden else 'HIDE_OFF', depress=hidden)
    row.prop(obj, "hide_viewport", text="", icon='RESTRICT_VIEW_OFF', toggle=True)
    row.prop(obj, "hide_render", text="", icon='RESTRICT_RENDER_OFF', toggle=True)
    row.prop(obj, "is_holdout", text="", icon='HOLDOUT_OFF', toggle=True)
    row.prop(obj, "visible_camera", text="", icon='INDIRECT_ONLY_OFF', toggle=True, invert_checkbox=True)


def _display_controls(layout, context):
    obj = _active_object(context)
    if obj is None:
        return
    controls = layout.row(align=True)
    controls.use_property_split = False
    controls.use_property_decorate = False
    controls.prop(obj, "display_type", text="")
    more = controls.row(align=True)
    more.ui_units_x = 1
    more.popover(panel="SCENA_PT_visibility_display", text="", icon='DOWNARROW_HLT')


class SCENA_PT_overlay_frame(bpy.types.Panel):
    bl_label = "Frame"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_parent_id = "VIEW3D_PT_overlay"

    def draw(self, context):
        prefs = preferences.get(context)
        controls = self.layout.column()
        controls.active = context.space_data.overlay.show_overlays
        flat = controls.row(align=True)
        flat.prop(prefs, "show_bounds_2d")
        flat.prop(prefs, "bounds_2d_fit")
        controls.prop(prefs, "show_bounds_3d")
        controls.prop(prefs, "bounds_hierarchy_gap")
        controls.prop(prefs, "show_frame_info")


class SCENA_PT_visibility_display(bpy.types.Panel):
    bl_label = "Ray Visibility and Display"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 10

    def draw(self, context):
        obj = _active_object(context)
        if obj is None:
            return
        rays = self.layout.column(align=True)
        rays.label(text="Ray Visibility")
        for prop, label in (
            ("visible_diffuse", "Diffuse"),
            ("visible_glossy", "Glossy"),
            ("visible_transmission", "Transmission"),
            ("visible_volume_scatter", "Volume"),
            ("visible_shadow", "Shadow"),
        ):
            rays.prop(obj, prop, text=label)
        if "sna_override_collection_viewport_display" in obj.bl_rna.properties:
            self.layout.separator()
            self.layout.prop(obj, "sna_override_collection_viewport_display", text="Collection Setting")


def _light_info(layout, context):
    obj = _active_object(context)
    if obj is None or obj.type != 'LIGHT' or obj.data is None:
        return
    data = obj.data
    column = layout.column(align=True)
    column.prop(data, "type", text="")
    column.prop(data, "color", text="")
    column.prop(data, "energy", text="Energy")
    if hasattr(data, "size"):
        column.prop(data, "size", text="Area Size")
    if hasattr(data, "shadow_soft_size"):
        column.prop(data, "shadow_soft_size", text="Point Size")


def info_controls(layout, context, *, fit_panel=None):
    _group_info(layout, context, fit_panel=fit_panel)
    _display_controls(layout, context)
    _light_info(layout, context)


def edit_navigation(layout, context, *, radial=False):
    controls = layout if radial else layout.row(align=True)
    route = editor.path(context)
    active = context.active_object
    enter = controls
    if not active or not active.select_get() or (route and (active in route or not editor.in_scope(context, active))):
        enter = controls.row(align=True)
        enter.enabled = False
    enter.operator_context = 'EXEC_DEFAULT'
    label = "Edit Child Group" if route else "Edit Group"
    enter.operator("groupmanager.edit", text=label, icon='OUTLINER_COLLECTION').unique = False
    controls.operator_context = 'INVOKE_DEFAULT'
    controls.operator("groupmanager.back", text="Back", icon='LOOP_BACK').steps = 1


def pick_controls(layout):
    layout.operator("groupmanager.pick", text="Pick", icon='EYEDROPPER')


def group_controls(layout, context, *, in_pie=False):
    pair = layout.column(align=True)
    create_group_row(pair, context)
    clear_controls(pair, context)
    duplicate = layout.row(align=True)
    duplicate.enabled = bpy.ops.groupmanager.duplicate_move.poll()
    duplicate.operator_context = 'INVOKE_REGION_WIN'
    duplicate.operator("groupmanager.duplicate_move", text="Duplicate", icon='DUPLICATE').kind = 'GROUP'
    choices = duplicate.row(align=True)
    choices.ui_units_x = 1.0
    if in_pie:
        choices.popover(panel="SCENA_PT_duplicate_choices", text="", icon='DOWNARROW_HLT')
    else:
        choices.menu("SCENA_MT_duplicate", text="")
    members = layout.row(align=True)
    members.operator("groupmanager.add_members", text="Add", icon='ADD')
    members.operator("groupmanager.remove_members", text="Remove", icon='REMOVE')


def all_group_controls(layout, context, *, in_pie=False):
    group_controls(layout, context, in_pie=in_pie)
    pick_controls(layout)
    layout.separator(factor=0.5)
    if in_pie:
        layout.popover(panel="SCENA_PT_group_operations", text="Operations", icon='MODIFIER')
    else:
        layout.menu("SCENA_MT_group_operations", text="Operations", icon='MODIFIER')
    layout.popover(panel="GROUPMANAGER_PT_definitions", text="Group Library", icon='OUTLINER_COLLECTION')


class SCENA_MT_duplicate(bpy.types.Menu):
    bl_label = "Duplicate"

    def draw(self, context):
        self.layout.operator_context = 'INVOKE_REGION_WIN'
        self.layout.operator_enum("groupmanager.duplicate_move", "kind")


class SCENA_MT_group_operations(bpy.types.Menu):
    bl_label = "Operations"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        select_instances_button(layout, context)
        tool_controls(layout, context)
        switch_type_buttons(layout, context)


def tool_controls(layout, context):
    layout.operator("groupmanager.split_mirror", text="Split Mirror", icon='MOD_MIRROR')


class SCENA_PT_duplicate_choices(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Duplicate"
    bl_ui_units_x = 6
    draw = SCENA_MT_duplicate.draw


class SCENA_PT_group_operations(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Operations"
    bl_ui_units_x = 12
    draw = SCENA_MT_group_operations.draw


def select_instances_button(layout, context):
    row = layout.row(align=True)
    row.enabled = usage.instance_source(context.active_object) is not None
    row.operator("groupmanager.select_similar", icon='LINKED')


def switch_type_buttons(layout, context):
    kinds = {hosts.host_type(obj) for obj in operations.switch_targets(context)}
    for target, source, label, icon in (
        ('NODES', 'NATIVE', "Switch to Geometry Nodes", 'GEOMETRY_NODES'),
        ('NATIVE', 'NODES', "Switch to Collection", 'OUTLINER_COLLECTION'),
    ):
        row = layout.row()
        row.enabled = source in kinds
        row.operator("groupmanager.switch_type", text=label, icon=icon).kind = target


class GROUPMANAGER_PT_group_data(ObjectProperties, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_group_data"
    bl_label = "Group Data"

    @classmethod
    def poll(cls, context):
        return hosts.definition(context.object) is not None

    def draw(self, context):
        group = context.object
        row = self.layout.row(align=True)
        row.use_property_split = False
        _draw_group_data(row, context, group, hosts.definition(group))


def _add_menu(self, context):
    if context.mode == 'OBJECT':
        self.layout.operator("groupmanager.add_group", text="Group", icon='OUTLINER_COLLECTION')


class _AddMenuLayout:
    """Forward the native Add menu and insert Group immediately after Mesh."""
    def __init__(self, layout):
        self._layout = layout

    def __getattr__(self, name):
        return getattr(self._layout, name)

    @property
    def operator_context(self):
        return self._layout.operator_context

    @operator_context.setter
    def operator_context(self, value):
        self._layout.operator_context = value

    def menu(self, menu, *args, **kwargs):
        result = self._layout.menu(menu, *args, **kwargs)
        if menu == "VIEW3D_MT_mesh_add":
            self._layout.operator("groupmanager.add_group", text="Group", icon='OUTLINER_COLLECTION')
        return result


_add_menu_hook = None


def _install_add_menu():
    global _add_menu_hook
    menu = bpy.types.VIEW3D_MT_add
    callbacks = menu._dyn_ui_initialize()
    for index, original in enumerate(callbacks):
        if original.__module__ == "bl_ui.space_view3d" and original.__qualname__ == "VIEW3D_MT_add.draw":
            @wraps(original)
            def draw(self, context):
                if context.mode == 'OBJECT':
                    self = SimpleNamespace(layout=_AddMenuLayout(self.layout))
                return original(self, context)

            callbacks[index] = draw
            _add_menu_hook = (callbacks, draw, original)
            return
    menu.append(_add_menu)


class GROUPMANAGER_PT_current(Sidebar, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_current"
    bl_label = "Group"

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        row.operator("groupmanager.open_preferences", text="", icon='PREFERENCES', emboss=False)
        row.separator(factor=1.5)

    def draw(self, context):
        if self.bl_space_type == 'PROPERTIES':
            return
        edit_navigation(self.layout, context)
        all_group_controls(self.layout, context)


def _draw_edit_path(layout, route):
    row = layout.row(align=True)
    row.alignment = 'LEFT'
    for index, item in enumerate(route):
        if index:
            row.label(text="", icon='RIGHTARROW')
        if index == len(route) - 1:
            row.label(text=item.name, icon='OUTLINER_COLLECTION')
        else:
            row.operator("groupmanager.back", text=item.name, emboss=False,
                         icon='OUTLINER_COLLECTION').steps = len(route) - index - 1


class GROUPMANAGER_PT_info(Sidebar, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_info"
    bl_label = "Object"
    bl_order = 2

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        _visibility_buttons(row, context)
        row.separator(factor=1.5)

    def draw(self, context):
        _group_info(self.layout, context)
        _display_controls(self.layout, context)
        if self.bl_space_type == 'PROPERTIES' and (route := editor.path(context)):
            _draw_edit_path(self.layout, route)


class GROUPMANAGER_PT_info_light(Sidebar, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_info_light"
    bl_parent_id = "GROUPMANAGER_PT_info"
    bl_label = "Light"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'LIGHT'

    def draw(self, context):
        _light_info(self.layout, context)


class GROUPMANAGER_PT_object_navigate(ObjectProperties, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_object_navigate"
    bl_parent_id = "GROUPMANAGER_PT_object"
    bl_label = "Navigate"
    bl_order = 1

    def draw(self, context):
        edit_navigation(self.layout, context)
        pick_controls(self.layout)


class GROUPMANAGER_PT_object_edit(ObjectProperties, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_object_edit"
    bl_parent_id = "GROUPMANAGER_PT_object"
    bl_label = "Edit"
    bl_order = 2

    def draw(self, context):
        group_controls(self.layout, context)
        switch_type_buttons(self.layout, context)


class GROUPMANAGER_PT_definitions(bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_definitions"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 20
    bl_label = "Group Library"
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        usage.draw_library(self.layout, context)


class GROUPMANAGER_PT_object_tool(ObjectProperties, bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_object_tool"
    bl_parent_id = "GROUPMANAGER_PT_object"
    bl_label = "Tool"
    bl_order = 3
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        tool_controls(self.layout, context)


def _properties_panel(panel, idname, parent=None):
    namespace = {
        "bl_idname": idname,
        "bl_label": panel.bl_label,
    }
    if "draw" in panel.__dict__:
        namespace["draw"] = panel.draw
    if "bl_order" in panel.__dict__:
        namespace["bl_order"] = panel.bl_order
    if parent:
        namespace["bl_parent_id"] = parent
    if "bl_options" in panel.__dict__:
        namespace["bl_options"] = panel.bl_options
    if "draw_header_preset" in panel.__dict__:
        namespace["draw_header_preset"] = panel.draw_header_preset
    if "poll" in panel.__dict__:
        namespace["poll"] = panel.poll
    return type(idname, (ObjectProperties, bpy.types.Panel), namespace)


GROUPMANAGER_PT_object = _properties_panel(GROUPMANAGER_PT_current, "GROUPMANAGER_PT_object")
GROUPMANAGER_PT_object_info = _properties_panel(GROUPMANAGER_PT_info, "GROUPMANAGER_PT_object_info")
GROUPMANAGER_PT_object_info_light = _properties_panel(GROUPMANAGER_PT_info_light, "GROUPMANAGER_PT_object_info_light", "GROUPMANAGER_PT_object_info")
GROUPMANAGER_PT_object_manage = _properties_panel(GROUPMANAGER_PT_definitions, "GROUPMANAGER_PT_object_manage", "GROUPMANAGER_PT_object")
GROUPMANAGER_PT_object_manage.bl_label = "Manage"
GROUPMANAGER_PT_current.bl_order = 1
GROUPMANAGER_PT_object.bl_order = 1


class GROUPMANAGER_PT_pie_types(bpy.types.Panel):
    bl_idname = "GROUPMANAGER_PT_pie_types"
    bl_label = "Type"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        layout.label(text="New Group")
        layout.prop(creation.current_settings(context), "kind", expand=True)
        layout.separator()
        switch_type_buttons(layout, context)


class GROUPMANAGER_MT_quick(bpy.types.Menu):
    bl_idname = "GROUPMANAGER_MT_quick"
    bl_label = "Scena"

    def draw(self, context):
        pie = self.layout.menu_pie()
        # Blender consumes radial slots in W, E, S, N, then the four diagonals.
        edit_navigation(pie, context, radial=True)

        south = pie.column()
        south.operator_context = 'INVOKE_REGION_WIN'
        blf.size(0, context.preferences.ui_styles[0].widget.points)
        translate = bpy.app.translations.pgettext_iface
        paired_labels = (("Group Library",), ("Create Group",), ("Operations",), ("Add", "Remove"))
        south.ui_units_x = max(10, max(
            sum(blf.dimensions(0, translate(label))[0] for label in labels) / 20 + 4.7
            for labels in paired_labels))
        south.scale_y = 1.08
        all_group_controls(south, context, in_pie=True)
        north = pie.column()
        box = north.box()
        header = box.row(align=True)
        header.label(text="Object")
        header.separator_spacer()
        _visibility_buttons(header, context)
        info_controls(box, context, fit_panel=north)

        pie.separator()
        pie.separator()
        pie.separator()
        pie.separator()


class GROUPMANAGER_OT_toggle_viewport_hide(bpy.types.Operator):
    bl_idname = "groupmanager.toggle_viewport_hide"
    bl_label = "Hide in Viewport"
    bl_description = "Temporarily hide or show the active object in this view layer"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        obj.hide_set(not obj.hide_get())
        return {'FINISHED'}


class GROUPMANAGER_OT_quick(bpy.types.Operator):
    bl_idname = "groupmanager.quick_menu"
    bl_label = "Scena Menu"

    def execute(self, context):
        return bpy.ops.wm.call_menu_pie(name="GROUPMANAGER_MT_quick")


def _header(self, context):
    space = context.space_data
    if space.type != 'VIEW_3D' or not space.overlay.show_overlays:
        return
    route = editor.path(context)
    if route:
        prefs = preferences.get(context)
        if prefs and not prefs.show_edit_header:
            return
        coll = hosts.definition(route[-1])
        count = sum(usage.counts(coll).values()) if coll else 0
        trail = self.layout.row(align=True)
        for index, host in enumerate(route[:-1]):
            jump = trail.operator("groupmanager.back", text=host.name, emboss=False)
            jump.steps = len(route) - index - 1
            trail.label(text="", icon='RIGHTARROW')
        trail.label(text=f"{route[-1].name} · {count}")
        self.layout.operator("groupmanager.back", text="", icon='BACK')


CLASSES = (SCENA_PT_overlay_frame, SCENA_MT_group_data, SCENA_MT_duplicate, SCENA_MT_group_operations, GROUPMANAGER_PT_group_data, GROUPMANAGER_PT_current, GROUPMANAGER_PT_info,
           SCENA_PT_visibility_display, SCENA_PT_duplicate_choices, SCENA_PT_group_operations, GROUPMANAGER_PT_info_light,
           GROUPMANAGER_PT_definitions,
           GROUPMANAGER_PT_object, GROUPMANAGER_PT_object_info,
           GROUPMANAGER_PT_object_info_light,
           GROUPMANAGER_PT_object_navigate,
           GROUPMANAGER_PT_object_edit,
           GROUPMANAGER_PT_object_tool, GROUPMANAGER_PT_object_manage,
           GROUPMANAGER_PT_pie_types,
           GROUPMANAGER_MT_quick,
           GROUPMANAGER_OT_toggle_viewport_hide,
           GROUPMANAGER_OT_quick)


def register():
    bpy.types.Scene.gm_create_settings = PointerProperty(type=creation.GM_PG_create_settings)
    bpy.types.WindowManager.gm_keep_group_modifiers = BoolProperty(
        name="Keep Modifiers",
        description="When ungrouping, keep this group's modifiers on the objects inside it",
        default=False,
    )
    bpy.types.VIEW3D_HT_header.append(_header)
    _install_add_menu()


def unregister():
    global _add_menu_hook
    if _add_menu_hook:
        callbacks, draw, original = _add_menu_hook
        if draw in callbacks:
            callbacks[callbacks.index(draw)] = original
        _add_menu_hook = None
    else:
        bpy.types.VIEW3D_MT_add.remove(_add_menu)
    bpy.types.VIEW3D_HT_header.remove(_header)
    del bpy.types.WindowManager.gm_keep_group_modifiers
    del bpy.types.Scene.gm_create_settings
