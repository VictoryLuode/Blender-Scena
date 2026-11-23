"""Export a static evaluated scene inside a disposable Blender process."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import sys

import bpy
from mathutils import Matrix


@dataclass
class Occurrence:
    source: object
    owner: object
    path: tuple
    data: object
    world: Matrix


def evaluated_occurrences(depsgraph):
    meshes = {}
    result = []
    for item in depsgraph.object_instances:
        obj = item.object
        if not item.show_self or obj.hide_render:
            continue
        owner = item.parent.original if item.is_instance else None
        if owner and owner.hide_render:
            continue
        path = tuple(i for i in item.persistent_id if i != 2147483647) if owner else ()
        data = obj.data
        if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            materials = tuple(
                slot.material.original if slot.material else None for slot in obj.material_slots
            )
            key = (obj.data.as_pointer(), materials)
            if key not in meshes:
                mesh = bpy.data.meshes.new_from_object(
                    obj, preserve_all_data_layers=True, depsgraph=depsgraph
                )
                if mesh is None:
                    raise ValueError(f"Could not get the evaluated mesh for {obj.name}")
                for index, material in enumerate(materials):
                    if index < len(mesh.materials):
                        mesh.materials[index] = material
                meshes[key] = mesh if mesh.vertices else None
            data = meshes[key]
        elif obj.type in {'CAMERA', 'LIGHT'}:
            data = obj.data.copy()
            data.animation_data_clear()
        elif obj.type in {'EMPTY', 'ARMATURE', 'LATTICE'}:
            data = None
        else:
            raise ValueError(f"A static snapshot does not support {obj.name} of type {obj.type}")
        result.append(Occurrence(obj.original, owner, path, data, item.matrix_world.copy()))
    return result


def find_parent(item, primary, direct, siblings):
    if not item.owner:
        return primary.get(item.source.parent)
    if item.source.parent:
        same_branch = siblings.get((item.owner, item.path[1:], item.source.parent))
        if same_branch is not None:
            return same_branch
    # Native instances report their nesting from the leaf back to the root.
    for offset in range(1, len(item.path)):
        ancestor = direct.get((item.owner, item.path[offset:]))
        if ancestor is not None:
            return ancestor
    return primary.get(item.owner)


def stage_scene(source, preserve, format_name, view_layer=None):
    context = bpy.context
    context.window.scene = source
    if view_layer is not None:
        context.window.view_layer = source.view_layers[view_layer]
    context.view_layer.update()
    items = evaluated_occurrences(context.evaluated_depsgraph_get())
    if not items:
        raise ValueError("The current view layer has nothing to export")
    scene = bpy.data.scenes.new("Scena Export")
    scene.world = source.world
    scene.unit_settings.system = source.unit_settings.system
    scene.unit_settings.scale_length = source.unit_settings.scale_length
    scene.frame_set(source.frame_current, subframe=source.frame_subframe)
    repeated = Counter(item.data for item in items if isinstance(item.data, bpy.types.Mesh))
    prototypes = {}
    placements = []
    primary = {}
    direct = {}
    siblings = {}
    for item in items:
        data = item.data
        if preserve and format_name != 'FBX' and data in repeated and repeated[data] > 1:
            if data not in prototypes:
                prototype = bpy.data.collections.new(data.name)
                prototype.objects.link(bpy.data.objects.new(data.name, data))
                prototypes[data] = prototype
            obj = bpy.data.objects.new(item.source.name, None)
            obj.instance_type = 'COLLECTION'
            obj.instance_collection = prototypes[data]
        else:
            if data is not None and not preserve and repeated.get(data, 0) > 1:
                data = data.copy()
            obj = bpy.data.objects.new(item.source.name, data)
        scene.collection.objects.link(obj)
        placements.append(obj)
        if item.owner:
            direct[item.owner, item.path] = obj
            siblings[item.owner, item.path[1:], item.source] = obj
        else:
            primary[item.source] = obj
        if source.camera == item.source and not item.owner:
            scene.camera = obj
    worlds = {obj: item.world for obj, item in zip(placements, items)}
    for item, obj in zip(items, placements):
        parent = find_parent(item, primary, direct, siblings)
        if parent:
            obj.parent = parent
            parent_world = worlds[parent]
            if abs(parent_world.to_3x3().determinant()) > 1e-12:
                # Keep shear from composed parent transforms in the inverse matrix.
                obj.matrix_parent_inverse = parent_world.inverted() @ item.world
                obj.matrix_basis = Matrix.Identity(4)
                continue
            obj.parent = None
        obj.matrix_world = item.world
    context.window.scene = scene
    context.view_layer.update()
    return scene


def export_scene(scene, destination, format_name, mode, view_layer=None):
    if mode not in {'EXPAND', 'PRESERVE'} or format_name not in {'FBX', 'USD', 'ABC'}:
        raise ValueError(f"Unknown export settings {format_name} / {mode}")
    preserve = mode == 'PRESERVE'
    snapshot = stage_scene(scene, preserve, format_name, view_layer)
    options = dict(filepath=str(destination), check_existing=False)
    if format_name == 'FBX':
        result = bpy.ops.export_scene.fbx(**options, bake_anim=False, path_mode='ABSOLUTE')
    elif format_name == 'USD':
        result = bpy.ops.wm.usd_export(
            **options, use_instancing=preserve, export_animation=False,
            export_textures_mode='KEEP', relative_paths=False, overwrite_textures=False,
            convert_world_material=False,
        )
    else:
        result = bpy.ops.wm.alembic_export(
            **options, start=snapshot.frame_current, end=snapshot.frame_current,
            init_scene_frame_range=False, use_instancing=preserve, flatten=False,
            as_background_job=False, face_sets=True,
        )
    if 'FINISHED' not in result or not destination.is_file() or not destination.stat().st_size:
        raise RuntimeError(f"{format_name} export did not finish")


if __name__ == '__main__':
    name, path, format_name, mode, *layer = sys.argv[sys.argv.index('--') + 1:]
    export_scene(bpy.data.scenes[name], Path(path), format_name, mode, *layer)

