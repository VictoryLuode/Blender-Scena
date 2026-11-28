"""Run with blender -b --factory-startup --python-exit-code 1 --python THIS -- OUT."""
import json
from pathlib import Path
import shutil
import struct
import sys
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scena
from scena import hosts, interchange

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

directory = out / "current"
def bundle_files():
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}

previous = bundle_files()
job = interchange._ExportJob(directory)
try:
    job.start(bpy.context)
    assert (directory / ".scena-export.lock").is_file()
finally:
    job.close()
assert job.process.poll() is not None and not job.work.exists()
assert bundle_files() == previous

placements_changed = placed[0]
placements_changed.location.x += 1
replace_file = interchange.os.replace

def fail_manifest(source, destination):
    if source.name == destination.name == "scene.scena.json":
        raise OSError("Simulated manifest write failure")
    replace_file(source, destination)

with patch.object(interchange.os, "replace", side_effect=fail_manifest):
    try:
        interchange.export_bundle(bpy.context, directory)
    except OSError as exc:
        assert str(exc) == "Simulated manifest write failure"
    else:
        raise AssertionError("The export did not report the write failure")
assert bundle_files() == previous
assert not any(directory.glob(".scena-*"))
placements_changed.location.x -= 1

bpy.ops.wm.save_as_mainfile(filepath=str(out / "identity.blend"))
bpy.ops.wm.open_mainfile(filepath=str(out / "identity.blend"))
last, _ = export("reopened")
assert last == third

# A linked Scena copy and a legacy/native copy can be the only visible
# placement on alternating exports. Their IDs must survive hide and reload.
original = bpy.data.objects["Board 0"]
original_id = last[original.name]["id"]
bpy.ops.object.select_all(action='DESELECT')
original.select_set(True)
bpy.context.view_layer.objects.active = original
assert bpy.ops.groupmanager.duplicate_move(kind='INSTANCE') == {'FINISHED'}
copy = bpy.context.active_object
copy_id = copy["_scena_exchange_id"]
assert copy_id != original_id
legacy = original.copy()
bpy.context.scene.collection.objects.link(legacy)
assert legacy["_scena_exchange_id"] == original_id
original.hide_set(True)
copy.hide_set(True)
def visible_ids():
    assert bpy.ops.scena.export_interchange(directory=str(directory)) == {'FINISHED'}
    doc = json.loads((directory / "scene.scena.json").read_text("utf-8"))
    return {row["name"]: row["id"] for row in doc["instances"]}
only = visible_ids()
assert original.name not in only and copy.name not in only
legacy_id = only[legacy.name]
assert legacy_id not in {original_id, copy_id}
copy.hide_set(False)
assert visible_ids()[copy.name] == copy_id
original.hide_set(False)
expected = visible_ids()
assert [expected[o.name] for o in (original, copy, legacy)] == [original_id, copy_id, legacy_id]
bpy.ops.wm.save_as_mainfile(filepath=str(out / "copy-identity.blend"))
bpy.ops.wm.open_mainfile(filepath=str(out / "copy-identity.blend"))
assert visible_ids() == expected
print("SCENA_INTERCHANGE_PASS")
