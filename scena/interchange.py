"""Export shared static assets and placements to a Scena interchange folder."""
import json
import os
from pathlib import Path
from contextlib import ExitStack, contextmanager
import subprocess
import tempfile
from time import monotonic

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty

from . import editor, hosts
from .exchange_ids import stable_ids

GEOMETRY = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
_active_export = None


@contextmanager
def export_lock(path):
    with path.open("x"):
        pass
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _export_rows(context):
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
    ids = stable_ids(blocks, (bpy.data.objects, bpy.data.collections))
    rows = []
    for obj, definition in objects.items():
        # Per-placement modifiers can diverge from the source collection.
        if hosts.host_type(obj) == 'NODES' and len(obj.modifiers) > 1:
            definition = obj
        rows.append(dict(object_name=obj.name_full, id=ids[obj], name=obj.name,
                         definition=ids[definition], definition_name=definition.name))
    return scene_id, rows


def _publish_bundle(folder, work, previous):
    target = folder / "scene.scena.json"
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


class _ExportJob:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.resources = ExitStack()
        self.process = None

    def start(self, context):
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=True)
        self.resources.enter_context(export_lock(folder / ".scena-export.lock"))
        scene_id, rows = _export_rows(context)
        target = folder / "scene.scena.json"
        self.previous = json.loads(target.read_text("utf-8")) if target.exists() else {}
        if target.exists() and (self.previous.get("format"), self.previous.get("id")) != (
                "scena.interchange", scene_id):
            raise ValueError("Choose an empty folder or this scene's previous export")
        self.work = Path(self.resources.enter_context(
            tempfile.TemporaryDirectory(dir=folder, prefix=".scena-")))
        (self.work / "rows.json").write_text(json.dumps(rows), "utf-8")
        snapshot = self.work / "snapshot.blend"
        scene = context.scene
        bpy.data.libraries.write(str(snapshot), {scene}, path_remap='ABSOLUTE')
        command = [bpy.app.binary_path, "-b", "--factory-startup", "--disable-autoexec",
                   str(snapshot), "--python-exit-code", "1", "--python",
                   str(Path(__file__).with_name("interchange_worker.py")), "--",
                   scene.name, context.view_layer.name, scene_id, str(self.work)]
        with (self.work / "worker.log").open("wb") as log:
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.resources.callback(self._stop)

    def progress(self):
        try:
            value, stage = json.loads((self.work / "progress.json").read_text("utf-8"))
        except (OSError, ValueError):
            return 5, "Starting Blender export"
        return value, stage

    def publish(self):
        code = self.process.poll()
        if code is None:
            raise RuntimeError("The export worker is still running")
        if code:
            with (self.work / "worker.log").open("rb") as log:
                log.seek(max(0, log.seek(0, os.SEEK_END) - 65536))
                output = log.read().decode("utf-8", errors="replace")
            errors = [line for line in output.splitlines() if "Error" in line]
            raise RuntimeError(" / ".join(errors[-2:]) or output[-400:] or f"Blender exited with code {code}")
        return _publish_bundle(self.folder, self.work, self.previous)

    def _stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def close(self):
        self.resources.close()


def export_bundle(context, folder):
    job = _ExportJob(folder)
    try:
        job.start(context)
        job.process.wait()
        return job.publish()
    finally:
        job.close()


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
        global _active_export
        try:
            if not self.directory:
                raise ValueError("Select an export folder")
            directory = bpy.path.abspath(self.directory)
            if bpy.app.background or not context.window:
                return self._complete(export_bundle(context, directory))
            if _active_export is not None:
                raise ValueError("A Scena export is already running")
            self._job = _ExportJob(directory)
            self._scene = context.scene
            self._layer = context.view_layer
            self._wm = context.window_manager
            self._workspace = context.workspace
            self._timer = None
            self._next_update = 0.0
            self._last_status = None
            _active_export = self
            self._wm.progress_begin(0, 100)
            self._status(0, "Preparing scene snapshot")
            self._timer = self._wm.event_timer_add(0.12, window=context.window)
            self._wm.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.cancel(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def modal(self, context, event):
        if _active_export is not self:
            return {'CANCELLED'}
        if event.type == 'ESC' and event.value == 'PRESS':
            self.cancel(context)
            self.report({'INFO'}, "Scena export cancelled")
            return {'CANCELLED'}
        now = monotonic()
        if event.type != 'TIMER' or now < self._next_update:
            return {'RUNNING_MODAL', 'PASS_THROUGH'}
        self._next_update = now + 0.10
        try:
            if self._job.process is None:
                if (context.scene != self._scene or context.view_layer != self._layer
                        or not self.poll(context)):
                    raise ValueError("The export scene or view layer changed before its snapshot")
                self._job.start(context)
            if self._job.process.poll() is None:
                self._status(*self._job.progress())
                return {'RUNNING_MODAL'}
            self._status(98, "Writing export files")
            doc = self._job.publish()
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.cancel(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self._status(100, "Export complete")
        self.cancel(context)
        return self._complete(doc)

    def _status(self, value, stage):
        if self._last_status == (value, stage):
            return
        self._last_status = value, stage
        self._wm.progress_update(value)
        self._workspace.status_text_set(f"Scena Export  {value}%   {stage}   ·   Esc to cancel")

    def cancel(self, context):
        global _active_export
        if _active_export is not self:
            return
        _active_export = None
        try:
            self._job.close()
        finally:
            if self._timer is not None:
                self._wm.event_timer_remove(self._timer)
                self._timer = None
            self._wm.progress_end()
            self._workspace.status_text_set(None)

    def _complete(self, doc):
        self.report({'INFO'}, f"Exported {len(doc['instances'])} placements. Save the .blend to keep their IDs")
        return {'FINISHED'}


def menu(self, context):
    self.layout.operator(SCENA_OT_export_interchange.bl_idname, text="Scena Shared Scene (folder)")


def register():
    bpy.types.TOPBAR_MT_file_export.append(menu)
    bpy.app.handlers.load_pre.append(_cancel_active)


def unregister():
    _cancel_active()
    bpy.app.handlers.load_pre.remove(_cancel_active)
    bpy.types.TOPBAR_MT_file_export.remove(menu)


@persistent
def _cancel_active(_unused=None):
    if _active_export is not None:
        _active_export.cancel(bpy.context)


CLASSES = (SCENA_OT_export_interchange,)
