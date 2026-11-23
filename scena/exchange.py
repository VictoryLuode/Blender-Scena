"""GroupPro import, library placement and export from a disposable scene copy."""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import bpy
from bpy.props import EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix

from . import branches, editor, hosts


def _node_input(modifier, identifier, default=None):
    try:
        return modifier.properties.inputs[identifier]["value"]
    except (KeyError, TypeError, AttributeError):
        return default


def _group_pro(obj):
    if not obj or hosts.definition(obj):
        return None
    for mod in obj.modifiers:
        if mod.type != 'NODES' or not mod.node_group:
            continue
        name = mod.node_group.name
        base, dot, suffix = name.rpartition('.')
        if dot and suffix.isdigit():
            name = base
        if name == "GPro_Instance" and obj.type == 'MESH':
            source = _node_input(mod, "Socket_2")
            if isinstance(source, bpy.types.Collection):
                return source, mod
        if name == "GPro_RealizeAndProxy" and obj.type == 'EMPTY' and obj.instance_collection:
            return obj.instance_collection, mod
    if obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION' and obj.instance_collection:
        source = obj.instance_collection
        if source.get("created_with_gp") or getattr(source, "created_with_gp", False):
            return source, None
    return None


def _plain_source(obj):
    info = _group_pro(obj)
    if not info:
        raise ValueError(f'"{obj.name}" is not a recognized GroupPro instance')
    source, setup = info
    extra = [m.name for m in obj.modifiers if m != setup]
    if extra:
        raise ValueError(f"\"{obj.name}\" has extra modifiers: {', '.join(extra)}. Resolve them first")
    if setup and (not setup.show_viewport or not setup.show_render):
        raise ValueError(f'The GroupPro node on "{obj.name}" is disabled in the viewport or render')
    if setup and (_node_input(setup, "Socket_6", False) or _node_input(setup, "Socket_3", False)):
        raise ValueError(f'"{obj.name}" uses a GroupPro display override or realization. Resolve that first')
    if obj.animation_data or obj.constraints:
        raise ValueError(f'"{obj.name}" has Link Group animation or constraints. Resolve ownership first')
    return source


@contextmanager
def _created_data():
    # Importing a collection can create meshes, materials, images and nested libraries.
    # Roll back only IDs created by this transaction, including zero-user definitions.
    before = {item.as_pointer() for item in bpy.data.user_map()}
    try:
        yield
    except Exception:
        added = [item for item in bpy.data.user_map() if item.as_pointer() not in before]
        if added:
            bpy.data.batch_remove(added)
        raise


def _select(context, placements):
    if not placements:
        return
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in placements:
        obj.select_set(True)
    context.view_layer.objects.active = placements[0]


def _owner(context):
    route = editor.path(context)
    owner = editor.content(hosts.definition(route[-1])) if route else context.collection or context.scene.collection
    if owner.library or not owner.is_editable:
        raise ValueError("Place the asset in a local writable collection")
    return owner


def convert_group_pro(context, selected):
    """Build local copies at the cursor, preserving sharing across the selection."""
    chosen = list(selected)
    if not chosen:
        raise ValueError("Select a GroupPro instance")
    for obj in chosen:
        _plain_source(obj)
    owner = _owner(context)
    anchor = context.active_object if context.active_object in chosen else chosen[0]
    shift = Matrix.Translation(context.scene.cursor.location - anchor.matrix_world.translation)
    converted = {}
    visiting = set()

    def clone_tree(source):
        if source in visiting:
            raise ValueError(f'"{source.name}" contains a cyclic instance reference')
        if source in converted:
            return converted[source]
        visiting.add(source)
        managed, copies = branches.clone_definition(source, source.name + " · GM")
        for member, duplicate in copies.items():
            if not _group_pro(member):
                continue
            child = clone_tree(_plain_source(member))
            replacement = hosts.make_host(member.name, child, 'NODES' if member.type == 'MESH' else 'NATIVE')
            for slot in tuple(duplicate.users_collection):
                slot.objects.link(replacement)
                slot.objects.unlink(duplicate)
            replacement.parent = duplicate.parent
            replacement.parent_type = duplicate.parent_type
            replacement.parent_bone = duplicate.parent_bone
            replacement.matrix_parent_inverse = duplicate.matrix_parent_inverse.copy()
            replacement.matrix_basis = duplicate.matrix_basis.copy()
            _copy_visibility(duplicate, replacement)
            duplicate.user_remap(replacement)
            old_data = duplicate.data
            bpy.data.objects.remove(duplicate)
            if isinstance(old_data, bpy.types.Mesh) and not old_data.users:
                bpy.data.meshes.remove(old_data)
        converted[source] = managed
        visiting.remove(source)
        return managed

    placements = []
    with _created_data():
        for obj in chosen:
            target = clone_tree(_plain_source(obj))
            kind = 'NODES' if obj.type == 'MESH' else 'NATIVE'
            new = hosts.make_host(obj.name + " · GM", target, kind)
            owner.objects.link(new)
            new.matrix_world = shift @ obj.matrix_world
            _copy_visibility(obj, new)
            placements.append(new)
        context.view_layer.update()
        _select(context, placements)
    return placements


def _copy_visibility(source, target):
    for key in ("hide_viewport", "hide_render", "hide_select", "display_type", "color",
                "visible_camera", "visible_diffuse", "visible_glossy", "visible_shadow",
                "visible_transmission", "visible_volume_scatter"):
        setattr(target, key, getattr(source, key))


def _asset_file(path):
    if not path:
        raise ValueError("Select a Blender file")
    resolved = Path(bpy.path.abspath(path)).resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".blend":
        raise ValueError("Select an existing .blend asset library")
    if bpy.data.filepath and resolved == Path(bpy.data.filepath).resolve():
        raise ValueError("This file is already open. Use Add to Group")
    return resolved


_WRAPPER = "_gm_library_wrapper"


def _library_of(source):
    if not source:
        return None
    if source.library:
        return source.library
    if source.get(_WRAPPER) and len(source.children) == 1:
        return source.children[0].library
    return None


def load_placement(context, path, collection_name, link):
    asset = _asset_file(path)
    owner = _owner(context)
    with _created_data():
        with bpy.data.libraries.load(str(asset), link=link, relative=False) as (available, requested):
            names = available.collections
            if not collection_name and len(names) == 1:
                collection_name = names[0]
            if collection_name not in names:
                raise ValueError("Choose a collection in the library: " + "、".join(sorted(names)[:8]))
            requested.collections = [collection_name]
        source = requested.collections[0]
        if not source:
            raise ValueError("Blender could not read the selected collection")
        if not hosts.is_definition(source):
            if source.library:
                wrapper = bpy.data.collections.new(source.name + "_GM_Link")
                wrapper.children.link(source)
                wrapper.instance_offset = source.instance_offset
                wrapper[_WRAPPER] = True
                source = wrapper
            hosts.mark_definition(source)
        placed = hosts.make_host(source.name, source, 'NATIVE')
        owner.objects.link(placed)
        placed.location = context.scene.cursor.location
        context.view_layer.update()
        _select(context, [placed])
    return placed


class GM_OT_import_gp(bpy.types.Operator):
    bl_idname = 'groupmanager.import_gp'
    bl_label = "GroupPro to Local Copy"
    bl_description = "Copy each selected GroupPro source into Scena and place it at the cursor. The original stays"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_group_pro(context.active_object))

    def execute(self, context):
        try:
            placed = convert_group_pro(context, context.selected_objects)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Placed {len(placed)} at the cursor")
        return {'FINISHED'}


def _search_library(self, context, text):
    try:
        asset = _asset_file(self.filepath)
        with bpy.data.libraries.load(str(asset)) as (available, _):
            return [n for n in sorted(available.collections) if text.casefold() in n.casefold()]
    except (OSError, RuntimeError, ValueError):
        return []


class GM_OT_library_file(bpy.types.Operator, ImportHelper):
    bl_idname = 'groupmanager.library_file'
    bl_label = "Place Collection from File"
    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})

    def execute(self, context):
        result = bpy.ops.groupmanager.place_from_library('INVOKE_DEFAULT', filepath=self.filepath)
        return {'CANCELLED'} if 'CANCELLED' in result else {'FINISHED'}


class GM_OT_place_library(bpy.types.Operator):
    bl_idname = 'groupmanager.place_from_library'
    bl_label = "Place Library Collection"
    bl_options = {'REGISTER', 'UNDO'}
    filepath: StringProperty(name="Library", subtype='FILE_PATH')
    collection_name: StringProperty(name="Collection", search=_search_library)
    link: EnumProperty(name="Load", items=[
        ('APPEND', "Append", "Edit as a local asset"),
        ('LINK', "Link", "Read from the source file and allow reload")])

    def invoke(self, context, event):
        try:
            names = _search_library(self, context, "")
            if not names:
                raise ValueError("The file has no collection that can be placed")
            if len(names) == 1:
                self.collection_name = names[0]
            return context.window_manager.invoke_props_dialog(self, width=440)
        except (ValueError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def draw(self, context):
        self.layout.prop(self, "filepath")
        self.layout.prop(self, "collection_name")
        self.layout.prop(self, "link")

    def execute(self, context):
        try:
            load_placement(context, self.filepath, self.collection_name, self.link == 'LINK')
        except (ValueError, RuntimeError, TypeError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_local_source(bpy.types.Operator):
    bl_idname = 'groupmanager.local_library_copy'
    bl_label = "Make Local Asset"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and not editor.path(context)
                and bool(_library_of(hosts.definition(context.active_object))))

    def execute(self, context):
        obj = context.active_object
        if obj.library or obj.override_library or editor.path(context):
            self.report({'ERROR'}, "Select an editable linked group from the local scene")
            return {'CANCELLED'}
        try:
            if len(obj.users_scene) > 1:
                raise ValueError("The Link Group belongs to more than one scene. Choose which one to change")
            with _created_data():
                source = hosts.definition(obj)
                copy, _ = branches.clone_definition(source, source.name + "_GM_Local")
                if _WRAPPER in copy:
                    del copy[_WRAPPER]
                hosts.assign_definition(obj, copy)
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_reload_library(bpy.types.Operator):
    bl_idname = 'groupmanager.reload_library'
    bl_label = "Reload Library"
    bl_description = "Reload every linked datablock from this library in the current file"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(_library_of(hosts.definition(obj))) and editor.in_scope(context, obj)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        library = _library_of(hosts.definition(context.active_object))
        path = Path(bpy.path.abspath(library.filepath, library=library.parent))
        if not path.is_file():
            self.report({'ERROR'}, "Asset library file not found: " + str(path))
            return {'CANCELLED'}
        try:
            library.reload()
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_open_library(bpy.types.Operator):
    bl_idname = 'groupmanager.open_library_source'
    bl_label = "Open Source Library"

    @classmethod
    def poll(cls, context):
        return bool(_library_of(hosts.definition(context.active_object)))

    def execute(self, context):
        library = _library_of(hosts.definition(context.active_object))
        path = Path(bpy.path.abspath(library.filepath, library=library.parent))
        if not path.is_file():
            self.report({'ERROR'}, "Asset library file not found: " + str(path))
            return {'CANCELLED'}
        try:
            subprocess.Popen([bpy.app.binary_path, "--disable-autoexec", str(path)])
        except OSError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_publish_definition(bpy.types.Operator, ExportHelper):
    bl_idname = 'groupmanager.publish_definition'
    bl_label = "Save Definition as Library"
    bl_description = "Write a new Blender file with the selected definition, its dependencies, and an editable scene"
    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        source = hosts.definition(context.active_object)
        return bool(source and not _library_of(source)) and editor.in_scope(context, context.active_object)

    def execute(self, context):
        source = hosts.definition(context.active_object)
        target = Path(bpy.path.abspath(self.filepath)).resolve()
        if target.suffix.lower() != '.blend':
            self.report({'ERROR'}, "Choose a .blend file for the new asset library")
            return {'CANCELLED'}
        if (bpy.data.filepath and target == Path(bpy.data.filepath).resolve()) or target.exists():
            self.report({'ERROR'}, "Choose a new file name. That file already exists")
            return {'CANCELLED'}
        scene = None
        try:
            with editor.source_space(context), tempfile.TemporaryDirectory(prefix=".groupmanager_", dir=target.parent) as tmp:
                staged = Path(tmp) / target.name
                scene = bpy.data.scenes.new(source.name + "_GM_Library")
                scene.collection.children.link(source)
                bpy.data.libraries.write(str(staged), {scene}, path_remap='ABSOLUTE', compress=True)
                # Publish as one step and refuse to replace a competing write.
                if os.name == 'nt':
                    os.rename(staged, target)
                else:
                    os.link(staged, target)
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            if scene:
                bpy.data.scenes.remove(scene)
        self.report({'INFO'}, "Saved asset library " + target.name)
        return {'FINISHED'}


class GM_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'groupmanager.export_scene'
    bl_label = "Export Current Frame"
    bl_description = "Export this frame's evaluated geometry, lights, and cameras through a temporary copy"
    filename_ext = ".fbx"
    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    filter_glob: StringProperty(default="*.fbx;*.usd;*.usda;*.usdc;*.abc", options={'HIDDEN'})
    file_format: EnumProperty(name="Format", items=[
        ('FBX', "FBX", "Object hierarchy and meshes"), ('USD', "USD", "Keep shared data the exporter supports"),
        ('ABC', "Alembic", "Export this frame's geometry and material face-set names")])
    mode: EnumProperty(name="Instances", items=[
        ('PRESERVE', "Keep Native Instances", "Use the hierarchy and instancing supported by the exporter"),
        ('EXPAND', "Expand to Objects", "Convert this frame's evaluated result into scene objects")])

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and not editor.path(context)

    def check(self, context):
        suffixes = {'FBX': ('.fbx',), 'USD': ('.usd', '.usda', '.usdc'), 'ABC': ('.abc',)}
        suffix = suffixes[self.file_format]
        path = Path(self.filepath)
        if path.name and path.suffix.lower() not in suffix:
            self.filepath = str(path.with_suffix(suffix[0]))
            return True
        return False

    def draw(self, context):
        self.layout.prop(self, "file_format")
        self.layout.prop(self, "mode")
        self.layout.label(text=f"Export the evaluated result at frame {context.scene.frame_current}")

    def execute(self, context):
        path = Path(bpy.path.abspath(self.filepath)).resolve()
        formats = {'.fbx': 'FBX', '.usd': 'USD', '.usda': 'USD', '.usdc': 'USD', '.abc': 'ABC'}
        if path.suffix.lower() not in formats:
            self.report({'ERROR'}, "Use a .fbx, .usd, .usda, .usdc, or .abc extension")
            return {'CANCELLED'}
        worker = Path(__file__).with_name("export_helper.py")
        try:
            with tempfile.TemporaryDirectory(prefix="groupmanager_scene_") as scene_tmp:
                with tempfile.TemporaryDirectory(prefix=".groupmanager_", dir=path.parent) as export_tmp:
                    snapshot = Path(scene_tmp) / "snapshot.blend"
                    staged = Path(export_tmp) / path.name
                    bpy.data.libraries.write(str(snapshot), {context.scene}, path_remap='ABSOLUTE')
                    command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
                               str(snapshot), '--python-exit-code', '1', '--python', str(worker), '--',
                               context.scene.name, str(staged), formats[path.suffix.lower()], self.mode,
                               context.view_layer.name]
                    done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, encoding='utf-8', errors='replace',
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if done.returncode or not staged.is_file() or not staged.stat().st_size:
                        errors = [v for v in done.stdout.splitlines() if 'Error' in v or 'ERROR' in v]
                        raise RuntimeError(errors[-1] if errors else "Background export failed. Check the scene and the export format")
                    extras = [p.name for p in staged.parent.rglob('*') if p.is_file() and p != staged]
                    if extras:
                        raise RuntimeError("Export needs external files. Save these images first: " + "、".join(extras[:3]))
                    os.replace(staged, path)
        except (ValueError, RuntimeError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Exported " + path.name)
        return {'FINISHED'}


CLASSES = ()


def draw(layout, context):
    library = _library_of(hosts.definition(context.active_object))
    if library:
        layout.label(text=library.filepath, icon='LIBRARY_DATA_DIRECT')
    layout.operator("groupmanager.import_gp", icon='DUPLICATE')
    layout.operator("groupmanager.local_library_copy")
    layout.operator("groupmanager.open_library_source")
    layout.operator("groupmanager.reload_library")
    layout.operator("groupmanager.library_file", icon='APPEND_BLEND')
    layout.operator("groupmanager.publish_definition", icon='FILE_BLEND')
    layout.operator("groupmanager.export_scene", icon='EXPORT')
