"""Bake one reusable static model per definition, in a disposable Blender process."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

import bpy
from pxr import Gf, Usd, UsdGeom
from mathutils import Matrix


def write_usd_layout(work, definitions, instances):
    paths = {row["id"]: row["usd"] for row in definitions}
    stage = Usd.Stage.CreateNew(str(work / "scene.usda"))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/Room")
    stage.SetDefaultPrim(root.GetPrim())
    for row in instances:
        node = UsdGeom.Xform.Define(stage, "/Room/i_" + row["id"])
        prim = node.GetPrim()
        prim.SetDisplayName(row["name"])
        prim.GetReferences().AddReference("./" + paths[row["definition"]])
        prim.SetInstanceable(True)
        # Gf uses row vectors while the Blender manifest uses column vectors.
        node.AddTransformOp().Set(Gf.Matrix4d(*row["matrix"]).GetTranspose())
    stage.GetRootLayer().Save()


def export_assets(scene_name, layer_name, scene_id, work):
    scene = bpy.data.scenes[scene_name]
    context = bpy.context
    context.window.scene = scene
    context.window.view_layer = scene.view_layers[layer_name]
    context.view_layer.update()
    graph = context.evaluated_depsgraph_get()
    unit = scene.unit_settings.scale_length if scene.unit_settings.system != 'NONE' else 1.0
    rows = json.loads((work / "rows.json").read_text("utf-8"))
    representatives = {}
    best = {}
    roots = {}
    instances = []
    by_name = {obj.name_full: obj for obj in scene.objects}
    for row in rows:
        root = by_name.get(row["object_name"])
        if root is None:
            raise ValueError("Missing snapshot object: " + row["object_name"])
        world = root.evaluated_get(graph).matrix_world.copy()
        did = row["definition"]
        determinant = abs(world.to_3x3().determinant())
        representatives.setdefault(did, row)
        if determinant and (did not in best or determinant > best[did][0]):
            best[did] = (determinant, root, world.inverted())
        world.translation *= unit
        instances.append({k: row[k] for k in ("id", "name", "definition")} |
                         {"matrix": [v for line in world for v in line]})
    for did, row in representatives.items():
        if did not in best:
            raise ValueError("All placements have singular transforms: " + row["definition_name"])
        _, root, inverse = best[did]
        roots[root] = did, inverse
    geometry = defaultdict(list)
    for item in graph.object_instances:
        obj = item.object
        root = item.parent.original if item.is_instance and item.parent else obj.original
        if root not in roots or not item.show_self or obj.hide_render:
            continue
        if obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        mesh = bpy.data.meshes.new_from_object(obj, depsgraph=graph, preserve_all_data_layers=True)
        if not mesh or not len(mesh.vertices):
            continue
        for index, slot in enumerate(obj.material_slots):
            if index < len(mesh.materials):
                mesh.materials[index] = slot.material.original if slot.material else None
        did, inverse = roots[root]
        local = inverse @ item.matrix_world
        local.translation *= unit
        mesh.transform(Matrix.Scale(unit, 4))
        geometry[did].append((obj.original.name, obj.original.get("_scena_exchange_id"), mesh, local))
    out = work / "assets"
    out.mkdir()
    definitions = []
    for did, row in representatives.items():
        stage = bpy.data.scenes.new("Scena model")
        context.window.scene = stage
        objects = []
        for name, member_id, mesh, local in geometry[did]:
            obj = bpy.data.objects.new(name, mesh)
            stage.collection.objects.link(obj)
            if member_id:
                obj["scena_member_id"] = member_id
            # Bake shear when a local object transform cannot represent it.
            loc, rot, scale = local.decompose()
            trs = Matrix.LocRotScale(loc, rot, scale)
            if any(abs(a-b) > 1e-5 for r,s in zip(trs,local) for a,b in zip(r,s)):
                mesh.transform(local)
                if local.to_3x3().determinant() < 0:
                    mesh.flip_normals()
            else:
                obj.matrix_world = local
            objects.append(obj)
        if not objects:
            obj = bpy.data.objects.new(row["definition_name"], None)
            stage.collection.objects.link(obj)
            objects.append(obj)
        context.view_layer.update()
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        files = {}
        for ext in ("glb", "fbx", "usdz"):
            rel = f"assets/{did}.{ext}"
            path = work / rel
            if ext == "glb":
                status = bpy.ops.export_scene.gltf(filepath=str(path), export_format='GLB',
                            use_selection=True, export_animations=False, export_cameras=False, export_extras=True)
            elif ext == "usdz":
                status = bpy.ops.wm.usd_export(filepath=str(path), selected_objects_only=True,
                            root_prim_path="/Model", export_animation=False, export_textures_mode='NEW',
                            generate_materialx_network=False, convert_world_material=False)
            else:
                status = bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True,
                            bake_anim=False, path_mode='COPY', embed_textures=True,
                            axis_forward='-Z', axis_up='Y', add_leaf_bones=False, use_custom_props=True)
            if 'FINISHED' not in status or not path.is_file():
                raise RuntimeError(f"Failed to export {row['definition_name']} as {ext}")
            files["usd" if ext == "usdz" else ext] = rel
        digest = hashlib.sha256((work / files["glb"]).read_bytes()).hexdigest()
        definitions.append(dict(id=did, name=row["definition_name"], revision=digest, **files))
        context.window.scene = scene
        bpy.data.scenes.remove(stage)
        for obj in objects:
            bpy.data.objects.remove(obj)
    write_usd_layout(work, definitions, instances)
    doc = dict(format="scena.interchange", version=1, id=scene_id, name=scene.name, usd_layout="scene.usda",
               definitions=definitions, instances=instances)
    (work / "scene.scena.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                                                      allow_nan=False), "utf-8")


if __name__ == "__main__":
    scene, layer, identity, directory = sys.argv[sys.argv.index("--") + 1:]
    export_assets(scene, layer, identity, Path(directory))
