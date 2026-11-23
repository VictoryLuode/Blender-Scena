"""File, viewport shading and transform tools."""

import bpy


class SCENA_MT_save_pie(bpy.types.Menu):
    bl_label = "Save Pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        pie.operator_context = 'INVOKE_DEFAULT'
        pie.operator("wm.recover_auto_save", text="Recover Auto Save")
        pie.operator("wm.save_mainfile", text="Save")
        data = pie.column()
        data.label(text="Data Manager")
        purge = data.operator("outliner.orphans_purge", text="Clean Unused Local Data")
        purge.do_linked_ids = False
        data.operator("wm.previews_ensure", text="Refresh Previews")
        files = data.column(align=True)
        files.operator("file.report_missing_files")
        files.operator("file.find_missing_files")
        data.label(text="Pack Blend File")
        pack = data.column(align=True)
        pack.operator("file.autopack_toggle", text="Auto Pack", depress=bpy.data.use_autopack)
        row = pack.row(align=True)
        row.operator("file.pack_all", text="Pack")
        row.operator("file.unpack_all", text="Unpack")
        other = pie.column()
        other.label(text="Default")
        other.operator("wm.save_homefile", text="Save Startup File")
        other.label(text="Other")
        other.operator("wm.revert_mainfile", text="Revert File")
        other.operator("script.reload", text="Reload Scripts")


class SCENA_MT_shader_pie(bpy.types.Menu):
    bl_label = "Shader Pie"

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def draw(self, context):
        pie = self.layout.menu_pie()
        space = context.space_data
        shading, overlay = space.shading, space.overlay
        viewport = pie.column()
        viewport.label(text="Viewport")
        solid = viewport.box().column(align=True)
        solid.prop(shading, "color_type", text="Solid Type", emboss=False)
        solid.prop(shading, "show_object_outline", text="Outline")
        solid.prop(shading, "show_specular_highlight")
        solid.prop(shading, "show_backface_culling")
        overlays = viewport.box().column(align=True)
        overlays.prop(overlay, "show_overlays", text="Overlays", toggle=True)
        overlays.prop(space, "show_gizmo", text="Gizmo")
        overlays.prop(overlay, "show_object_origins_all", text="All Origins")
        overlays.prop(overlay, "show_floor", text="Floor")
        overlays.prop(overlay, "show_relationship_lines")
        overlays.prop(overlay, "show_extras")
        tools = viewport.box().column(align=True)
        tools.prop(overlay, "show_face_orientation")
        tools.prop(overlay, "show_wireframes")

        render = pie.column()
        for mode, label in (('MATERIAL', "Material Preview"), ('WIREFRAME', "Wireframe")):
            render.prop_enum(shading, "type", mode, text=label)
        scene = context.scene
        if scene.render.engine == 'CYCLES' and hasattr(bpy.types, "CYCLES_VIEW3D_PT_shading_render_pass"):
            render.popover(panel="CYCLES_VIEW3D_PT_shading_render_pass", text="Render Pass")
        else:
            render.popover(panel="VIEW3D_PT_shading_render_pass", text="Render Pass")
        lighting = render.box().column(align=True)
        lighting.prop(shading, "use_scene_lights_render", text="Scene Lights")
        lighting.prop(shading, "use_scene_world_render", text="Scene World")
        effects = render.box().column(align=True)
        effects.prop(shading, "use_compositor", text="Compositor")
        effects.prop(scene.render, "film_transparent", text="Transparent")
        effects.prop(scene.render, "use_motion_blur")
        if scene.render.engine == 'CYCLES' and hasattr(scene, "cycles"):
            row = effects.row(align=True)
            row.prop(scene.cycles, "use_preview_denoising", text="Denoise")
            row.prop(scene.cycles, "preview_denoising_start_sample", text="")
        border = render.column(heading="Render Region", align=True)
        border.prop(scene.render, "use_border", text="Camera")
        border.prop(space, "use_render_border", text="Screen")
        pie.prop_enum(shading, "type", 'RENDERED', text="Rendered")
        pie.prop_enum(shading, "type", 'SOLID', text="Solid")


class SCENA_MT_transform_pie(bpy.types.Menu):
    bl_label = "Transform Pie"

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def draw(self, context):
        pie = self.layout.menu_pie()
        settings = context.scene.tool_settings
        snap = pie.box().column(align=True)
        snap.prop(settings, "use_snap", text="Snap")
        snap.prop(settings, "use_proportional_edit_objects" if context.mode == 'OBJECT'
                  else "use_proportional_edit", text="Proportional Editing")
        snap.prop(settings, "proportional_edit_falloff", text="Falloff")
        options = pie.column()
        affect = options.box().column(heading="Affect Only", align=True)
        affect.prop(settings, "use_transform_data_origin", text="Origins")
        affect.prop(settings, "use_transform_pivot_point_align", text="Locations")
        affect.prop(settings, "use_transform_skip_children", text="Parents")
        mesh = options.box().column(heading="Mesh Options", align=True)
        mesh.prop(settings, "use_transform_correct_face_attributes", text="Correct Face Attributes")
        mesh.prop(settings, "use_transform_correct_keep_connected", text="Keep Connected")
        mesh.prop(settings, "use_mesh_automerge", text="Auto Merge")
        orientation = pie.box().column(align=True)
        slot = context.scene.transform_orientation_slots[0]
        for value in ('GLOBAL', 'LOCAL', 'NORMAL', 'GIMBAL', 'VIEW', 'CURSOR', 'PARENT'):
            orientation.prop_enum(slot, "type", value)
        custom = orientation.row(align=True)
        custom.prop(slot, "type", text="")
        custom.operator("transform.create_orientation", text="Custom", icon='ADD')
        pivot = pie.box().column(align=True)
        for value in ('ACTIVE_ELEMENT', 'MEDIAN_POINT', 'INDIVIDUAL_ORIGINS', 'CURSOR', 'BOUNDING_BOX_CENTER'):
            pivot.prop_enum(settings, "transform_pivot_point", value)


CLASSES = (SCENA_MT_save_pie, SCENA_MT_shader_pie, SCENA_MT_transform_pie)
