"""Run with blender -b --factory-startup --python-exit-code 1 --python THIS -- OUT."""
import json
from pathlib import Path
import shutil
import struct
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scena
from scena import hosts

out = Path(sys.argv[sys.argv.index("--") + 1])
out.mkdir(parents=True, exist_ok=True)
scena.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
definition = hosts.mark_definition(bpy.data.collections.new("Board"))

def part(name, collection):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)])
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj

part("Panel", definition)
placed = []
for i, kind in enumerate(("NATIVE","NODES","NATIVE")):
    obj = hosts.make_host(f"Board {i}", definition, kind)
    scene.collection.objects.link(obj)
    obj.location = (2*i, i, 1)
    placed.append(obj)
for i in range(3):
    part(f"Proof {i}", scene.collection).location = (2*i, i, 1.1)

def export(phase):
    directory = out / "current"
    assert bpy.ops.scena.export_interchange(directory=str(directory)) == {'FINISHED'}
    shutil.copytree(directory, out / phase)
    doc = json.loads((directory / "scene.scena.json").read_text("utf-8"))
    assert len(doc["instances"]) == 6
    return {r["name"]:r for r in doc["instances"]}, {r["id"]:r for r in doc["definitions"]}

def meshes(row, phase):
    raw = (out / phase / row["glb"]).read_bytes()
    n = struct.unpack_from("<I", raw, 12)[0]
    return len(json.loads(raw[20:20+n])["meshes"])

first, defs = export("first")
shared = first["Board 0"]["definition"]
assert len(defs) == 4 and meshes(defs[shared], "first") == 1
assert all(first[f"Board {i}"]["definition"] == shared for i in range(3))
assert first["Board 2"]["matrix"][3:12:4] == [4,2,1]
part("Ledge", definition)
again, defs = export("second")
assert again == first and meshes(defs[shared], "second") == 2
private = hosts.mark_definition(definition.copy())
part("Upper ledge", private)
hosts.assign_definition(placed[2], private)
placed[2].name = "Private board"
third, defs = export("third")
assert third["Private board"]["id"] == first["Board 2"]["id"]
assert third["Private board"]["definition"] != shared
assert meshes(defs[third["Private board"]["definition"]], "third") == 3
assert meshes(defs[shared], "third") == 2
bpy.ops.wm.save_as_mainfile(filepath=str(out / "identity.blend"))
bpy.ops.wm.open_mainfile(filepath=str(out / "identity.blend"))
last, _ = export("reopened")
assert last == third
print("SCENA_INTERCHANGE_PASS")
