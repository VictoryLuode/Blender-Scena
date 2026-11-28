"""User-wide defaults and display preferences for Scena."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import creation, edit_layer, hotkeys


BOUNDS_COLOR = (0.18, 0.72, 0.57)
BOUNDS_WIDTH = 1.25
BOUNDS_2D_FIT = 'GROUP'
BOUNDS_HIERARCHY_GAP = 2.5
BOUNDS_CORNER = 12.0
PREVIEW_DISPLAYS = (
    ('WIRE', "Wireframe", "Display the modifier result as edges", 0),
    ('SOLID', "Solid", "Display the faces of the modifier result", 1),
)
_BOX_SHAPES = (
    ('FRAME', "Outline", "Draw the whole box"),
    ('CORNERS', "Corners", "Draw only the corners"),
)
_VIEWPORT_FITS = (
    ('BOX', "3D Box", "Fit the flat box to the projected 3D box"),
    ('GROUP', "Group", "Fit the flat box to the group's widest points"),
)
_OBJECT_FRAME_TYPES = (
    ("frame_mesh", "MESH", "Mesh", "Mesh objects"),
    ("frame_curve", "CURVE", "Curve", "Curve objects"),
    ("frame_surface", "SURFACE", "Surface", "Surface objects"),
    ("frame_meta", "META", "Metaball", "Metaball objects"),
    ("frame_font", "FONT", "Text", "Text objects"),
    ("frame_curves", "CURVES", "Hair", "Hair curves"),
    ("frame_pointcloud", "POINTCLOUD", "Point Cloud", "Point cloud objects"),
    ("frame_volume", "VOLUME", "Volume", "Volume objects"),
    ("frame_grease_pencil", "GREASEPENCIL", "Grease Pencil", "Grease Pencil objects"),
    ("frame_armature", "ARMATURE", "Armature", "Armature objects"),
    ("frame_lattice", "LATTICE", "Lattice", "Lattice objects"),
    ("frame_empty", "EMPTY", "Empty", "Empty objects"),
    ("frame_light", "LIGHT", "Light", "Light objects"),
    ("frame_light_probe", "LIGHT_PROBE", "Light Probe", "Light probe objects"),
    ("frame_camera", "CAMERA", "Camera", "Camera objects"),
    ("frame_speaker", "SPEAKER", "Speaker", "Speaker objects"),
)


def get(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def preview_settings(context):
    prefs = get(context)
    return prefs.modifier_preview if prefs is not None else getattr(context.window_manager, "gm_modifier_preview", None)


def _redraw_viewports(self, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _update_kind(self, context):
    scene = context.scene
    if scene and hasattr(scene, "gm_create_settings"):
        scene.gm_create_settings.property_unset("kind")
    _redraw_viewports(self, context)


def _update_cursor_origin(self, context):
    hotkeys.set_cursor_enabled(self.use_cursor_origin)


def _read_category(self, value, is_set):
    # Old profiles can still store index 3 for the former Keymap tab.
    return 1 if value == 3 else value


class SCENA_PG_modifier_preview(bpy.types.PropertyGroup):
    # Existing profiles supply the default when a placement graph first gains the preview socket.
    enabled: BoolProperty(name="Modifier Preview", description="Show modifier copies while editing a group",
                          default=True, options={'HIDDEN'}, update=edit_layer.update_modifier_previews)
    display: EnumProperty(name="Display", description="How to draw the modifier preview",
                          items=PREVIEW_DISPLAYS, default='WIRE', options=set(), update=edit_layer.update_modifier_previews)


class GROUPMANAGER_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    modifier_preview: PointerProperty(type=SCENA_PG_modifier_preview)
    settings: EnumProperty(name="Category",
                           description="Which group of settings to show",
                           items=(('GROUP', "Group", "Create a group and move through it", 1),
                                  ('DISPLAY', "Display", "Viewport frames and modifier previews", 2),
                                  ('TOOL', "Tool", "Optional extra tools", 0)),
                           default='GROUP', get_transform=_read_category)
    use_cursor_origin: BoolProperty(name="Cursor and Origin",
                                    description="Open the Cursor and Origin pie with Shift+S. Turn this off to keep Blender's snap pie",
                                    default=False, update=_update_cursor_origin)

    default_group_name: StringProperty(name="Group Name",
                                       description="Default collection name. Leave empty to use the active object's name. Scena adds a Group_ prefix",
                                       default="")
    default_kind: EnumProperty(name="Group Type", items=[(key, label, tip, number)
                                     for key, label, tip, icon, number in creation.KINDS],
                               default=creation.DEFAULT_KIND, update=_update_kind)
    keep_modifiers: BoolProperty(name="Keep Modifiers",
                                 description="When ungrouping, keep this group's modifiers on the objects inside it",
                                 default=False, update=_redraw_viewports)
    default_pivot: EnumProperty(name="Origin", items=creation.PIVOTS, default='ACTIVE',
                                update=_redraw_viewports)
    default_orientation: EnumProperty(name="Local Axes", items=creation.ORIENTATIONS,
                                      default='WORLD', update=_redraw_viewports)
    default_include_children: BoolProperty(name="Include Children",
                                           description="Include all descendants of the selected objects when creating a group",
                                           default=False, update=_redraw_viewports)

    show_edit_bounds: BoolProperty(name="Group", description="Draw the group frame",
                                   default=True, update=_redraw_viewports)
    bounds_color: FloatVectorProperty(name="Bounds Color", size=3, subtype='COLOR_GAMMA',
                                      min=0, max=1, default=BOUNDS_COLOR, update=_redraw_viewports)
    bounds_width: FloatProperty(name="Line Width", default=BOUNDS_WIDTH, min=0.5, max=6.0,
                                precision=2, step=25, subtype='PIXEL', update=_redraw_viewports)
    bounds_hierarchy_gap: FloatProperty(name="Hierarchy Gap", default=BOUNDS_HIERARCHY_GAP,
                                        min=0.0, soft_max=32.0, precision=1, subtype='PIXEL',
                                        description="Minimum spacing between parent and child frames in UI-scaled pixels",
                                        update=_redraw_viewports)
    show_bounds_3d: BoolProperty(name="3D Frame", description="Draw the frame in the scene",
                                 default=False, update=_redraw_viewports)
    bounds_3d_style: EnumProperty(name="3D Style", items=_BOX_SHAPES, default='CORNERS',
                                  update=_redraw_viewports)
    bounds_3d_corner: FloatProperty(name="3D Corner", default=BOUNDS_CORNER, min=2.0, max=80.0,
                                    precision=1, step=10, subtype='PIXEL',
                                    description="Length of each 3D corner, in pixels",
                                    update=_redraw_viewports)
    show_bounds_2d: BoolProperty(name="2D Frame", description="Draw the flat frame in the viewport",
                                 default=True, update=_redraw_viewports)
    show_frame_info: BoolProperty(name="Object Info", description="Show the object name and type icon beside its frame",
                                  default=True, update=_redraw_viewports)
    bounds_2d_fit: EnumProperty(name="2D Fit", items=_VIEWPORT_FITS, default=BOUNDS_2D_FIT,
                                description="Where the flat frame takes its width and height",
                                update=_redraw_viewports)
    bounds_2d_style: EnumProperty(name="2D Style", items=_BOX_SHAPES, default='CORNERS',
                                  update=_redraw_viewports)
    bounds_2d_corner: FloatProperty(name="2D Corner", default=BOUNDS_CORNER, min=2.0, max=80.0,
                                    precision=1, step=10, subtype='PIXEL',
                                    description="Length of each 2D corner, in pixels",
                                    update=_redraw_viewports)
    click_names: BoolProperty(name="Click Names",
                              description="Click a viewport name to select it; double-click to enter its level",
                              default=True)
    click_objects: BoolProperty(name="Click Objects",
                                description="Use the level navigation gesture on objects and empty space",
                                default=True)
    frame_mesh: BoolProperty(name="Mesh", description="Mesh objects", default=False, update=_redraw_viewports)
    frame_curve: BoolProperty(name="Curve", description="Curve objects", default=False, update=_redraw_viewports)
    frame_surface: BoolProperty(name="Surface", description="Surface objects", default=False, update=_redraw_viewports)
    frame_meta: BoolProperty(name="Metaball", description="Metaball objects", default=False, update=_redraw_viewports)
    frame_font: BoolProperty(name="Text", description="Text objects", default=False, update=_redraw_viewports)
    frame_curves: BoolProperty(name="Hair", description="Hair curves", default=False, update=_redraw_viewports)
    frame_pointcloud: BoolProperty(name="Point Cloud", description="Point cloud objects", default=False, update=_redraw_viewports)
    frame_volume: BoolProperty(name="Volume", description="Volume objects", default=False, update=_redraw_viewports)
    frame_grease_pencil: BoolProperty(name="Grease Pencil", description="Grease Pencil objects", default=False, update=_redraw_viewports)
    frame_armature: BoolProperty(name="Armature", description="Armature objects", default=False, update=_redraw_viewports)
    frame_lattice: BoolProperty(name="Lattice", description="Lattice objects", default=False, update=_redraw_viewports)
    frame_empty: BoolProperty(name="Empty", description="Empty objects", default=False, update=_redraw_viewports)
    frame_light: BoolProperty(name="Light", description="Light objects", default=False, update=_redraw_viewports)
    frame_light_probe: BoolProperty(name="Light Probe", description="Light probe objects", default=False, update=_redraw_viewports)
    frame_camera: BoolProperty(name="Camera", description="Camera objects", default=False, update=_redraw_viewports)
    frame_speaker: BoolProperty(name="Speaker", description="Speaker objects", default=False, update=_redraw_viewports)

    def draw(self, context):
        layout = self.layout
        layout.use_property_decorate = False
        column = layout.column(align=True)
        column.row(align=True).prop(self, "settings", expand=True)
        box = column.box()
        if self.settings == 'GROUP':
            self.draw_group(box, context)
        elif self.settings == 'DISPLAY':
            self.draw_display(box, context)
        else:
            self.draw_tool(box, context)

    def draw_tool(self, layout, context):
        hotkeys.draw_tool_shortcuts(layout, context, self)

    def draw_group(self, layout, context):
        create = layout.box()
        create.label(text="Create", icon='COLLECTION_NEW')
        fields = create.column()
        fields.use_property_split = True
        fields.prop(self, "default_group_name", placeholder="Active object name")
        fields.prop(self, "default_kind")
        fields.prop(self, "default_pivot")
        fields.prop(self, "default_orientation")
        fields.prop(self, "default_include_children")

        ungroup = layout.box()
        ungroup.label(text="Ungroup")
        ungroup.prop(self, "keep_modifiers")

        navigation = layout.box()
        navigation.label(text="Navigate", icon='VIEW3D')
        fields = navigation.column()
        fields.use_property_split = True
        fields.prop(self, "click_names")
        fields.prop(self, "click_objects")
        hotkeys.draw_group_shortcuts(layout, context)

    def draw_display(self, layout, context):
        preview = layout.box().column()
        preview.use_property_split = True
        preview.prop(self.modifier_preview, "display", text="Modifier Preview Display")

        frame = layout.box()
        frame.label(text="Frame", icon='OUTLINER_COLLECTION')
        fields = frame.column()
        fields.use_property_split = True
        fields.prop(self, "bounds_width")
        fields.prop(self, "bounds_hierarchy_gap")
        fields.prop(self, "show_frame_info")
        solid = fields.column(align=True)
        solid.prop(self, "show_bounds_3d")
        solid_opts = solid.column(align=True)
        solid_opts.active = self.show_bounds_3d
        solid_opts.prop(self, "bounds_3d_style")
        solid_corner = solid_opts.column()
        solid_corner.active = self.bounds_3d_style == 'CORNERS'
        solid_corner.prop(self, "bounds_3d_corner")
        flat = fields.column(align=True)
        flat.prop(self, "show_bounds_2d")
        flat_opts = flat.column(align=True)
        flat_opts.active = self.show_bounds_2d
        flat_opts.prop(self, "bounds_2d_fit")
        flat_opts.prop(self, "bounds_2d_style")
        flat_corner = flat_opts.column()
        flat_corner.active = self.bounds_2d_style == 'CORNERS'
        flat_corner.prop(self, "bounds_2d_corner")

        objects = layout.box()
        objects.label(text="Objects", icon='OBJECT_DATAMODE')
        objects.label(text="Checked types use the frame. Group covers group instances.")
        entries = ["show_edit_bounds", *(attr for attr, _identifier, _name, _description in _OBJECT_FRAME_TYPES)]
        row = objects.row(align=True)
        columns = (row.column(align=True), row.column(align=True))
        for column in columns:
            column.use_property_split = False
        for index, attr in enumerate(entries):
            columns[index % 2].prop(self, attr)

CLASSES = (SCENA_PG_modifier_preview, GROUPMANAGER_Preferences)
