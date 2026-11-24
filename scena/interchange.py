"""Export shared static assets and placements to a Scena interchange folder."""
import json
import os
from pathlib import Path
from contextlib import contextmanager
import re
import subprocess
import tempfile
import uuid

import bpy
from bpy.props import StringProperty

from . import editor, hosts

KEY = "_scena_exchange_id"
GEOMETRY = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}


def stable_ids(blocks):
    # A Blender duplicate inherits custom properties. Prefer the last recorded
    # owner, then the older datablock when both have since been renamed.
    result = {}
    seen = set()
    for block in sorted(set(blocks), key=lambda b: (b.get(KEY + "_owner") != b.name, b.session_uid)):
        value = block.get(KEY)
        if block.library:
            value = uuid.uuid5(uuid.NAMESPACE_URL, bpy.path.abspath(block.library.filepath)
                               + "/" + block.bl_rna.identifier + "/" + block.name).hex
        elif not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value) or value in seen:
            value = uuid.uuid4().hex
            block[KEY] = value
        if not block.library and block.get(KEY + "_owner") != block.name:
            block[KEY + "_owner"] = block.name
        seen.add(value)
        result[block] = value
    return result


@contextmanager
def export_lock(path):
    with path.open("x"):
        pass
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def export_bundle(context, folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    scene = context.scene
    scene_id = stable_ids(bpy.data.scenes)[scene]
    objects = {}
    for obj in context.view_layer.objects:
        if obj.hide_render or not obj.visible_get(view_layer=context.view_layer):
            continue
        definition = hosts.definition(obj)
        if definition or obj.type in GEOMETRY or obj.instance_type == 'COLLECTION':
            objects[obj] = definition or obj.instance_collection or obj
    if not objects:
        raise ValueError("The visible scene has no static geometry or groups")
    blocks = set(objects) | set(objects.values())
    pending = [b for b in blocks if isinstance(b, bpy.types.Collection)]
    visited = set()
    while pending:
        collection = pending.pop()
        if collection in visited:
            continue
        visited.add(collection)
        for member in collection.all_objects:
            blocks.add(member)
            nested = hosts.definition(member) or member.instance_collection
            if nested:
                pending.append(nested)
    ids = stable_ids(blocks)
    rows = []
    for obj, definition in objects.items():
        # Per-placement modifiers can diverge from the source collection.
        if hosts.host_type(obj) == 'NODES' and len(obj.modifiers) > 1:
            definition = obj
        rows.append(dict(object_name=obj.name_full, id=ids[obj], name=obj.name,
                         definition=ids[definition], definition_name=definition.name))
    target = folder / "scene.scena.json"
    lock = folder / ".scena-export.lock"
    with export_lock(lock):
        previous = json.loads(target.read_text("utf-8")) if target.exists() else {}
        if target.exists() and (previous.get("format"), previous.get("id")) != ("scena.interchange", scene_id):
            raise ValueError("Choose an empty folder or this scene's previous export")
        with tempfile.TemporaryDirectory(dir=folder, prefix=".scena-") as td:
            work = Path(td)
            (work / "rows.json").write_text(json.dumps(rows), "utf-8")
            snapshot = work / "snapshot.blend"
            bpy.data.libraries.write(str(snapshot), {scene}, path_remap='ABSOLUTE')
            command = [bpy.app.binary_path, "-b", "--factory-startup", "--disable-autoexec",
                       str(snapshot), "--python-exit-code", "1", "--python",
                       str(Path(__file__).with_name("interchange_worker.py")), "--",
                       scene.name, context.view_layer.name, scene_id, str(work)]
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode:
                errors = [s for s in proc.stdout.splitlines() if "Error" in s]
                raise RuntimeError(" / ".join(errors[-2:]) or proc.stdout[-400:])
            manifest = json.loads((work / target.name).read_text("utf-8"))
            assets = folder / "assets"
            if assets.is_symlink() or assets.resolve().parent != folder:
                raise ValueError("Choose a local assets directory")
            assets.mkdir(exist_ok=True)
            owned = {r[k] for r in previous.get("definitions", []) for k in ("fbx", "glb", "usd") if k in r}
            owned.update(previous.get("retained_assets", []))
            prior = {r["id"]: r for r in previous.get("definitions", [])}
            changes = []
            for row in manifest["definitions"]:
                for key in ("fbx", "glb", "usd"):
                    rel = row[key]
                    dest = folder / rel
                    if dest.exists() and (rel not in owned or dest.is_symlink()):
                        raise ValueError("Unmanaged asset already exists: " + str(dest))
                    if key != "usd" and dest.exists() and prior.get(row["id"], {}).get("revision") == row["revision"]:
                        continue
                    changes.append((work / rel, dest))
            active = {r[k] for r in manifest["definitions"] for k in ("fbx", "glb", "usd")}
            manifest["retained_assets"] = sorted(owned - active)
            (work / target.name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
            layout = folder / "scene.usda"
            if layout.exists() and (not previous.get("usd_layout") or layout.is_symlink()):
                raise ValueError("Unmanaged file already exists: " + str(layout))
            changes.append((work / layout.name, layout))
            changes.append((work / target.name, target))
            backups = []
            try:
                for src, dest in changes:
                    backup = work / ("old-" + dest.name) if dest.exists() else None
                    if backup:
                        os.replace(dest, backup)
                    backups.append((dest, backup))
                    os.replace(src, dest)
            except OSError:
                for dest, backup in reversed(backups):
                    if backup:
                        os.replace(backup, dest)
                    else:
                        dest.unlink(missing_ok=True)
                raise
    return manifest


class SCENA_OT_export_interchange(bpy.types.Operator):
    bl_idname = "scena.export_interchange"
    bl_label = "Scena Shared Scene"
    bl_description = "Export static shared definitions and placements. Save the .blend to keep their IDs"
    directory: StringProperty(subtype='DIR_PATH')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and not editor.path(context)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            if not self.directory:
                raise ValueError("Select an export folder")
            doc = export_bundle(context, bpy.path.abspath(self.directory))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Exported {len(doc['instances'])} placements. Save the .blend to keep their IDs")
        return {'FINISHED'}


def menu(self, context):
    self.layout.operator(SCENA_OT_export_interchange.bl_idname, text="Scena Shared Scene (folder)")


def register():
    bpy.types.TOPBAR_MT_file_export.append(menu)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu)


CLASSES = (SCENA_OT_export_interchange,)
