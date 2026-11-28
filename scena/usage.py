"""Counts and paths describe authored placements, including nested and hidden ones."""

from collections import Counter, defaultdict
from graphlib import CycleError, TopologicalSorter
from itertools import islice
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import IntProperty, StringProperty

from . import editor, hosts, menu_hooks


def _authored_scene_objects(scene, opened):
    if not opened:
        return scene.objects
    # A source member may also have a legitimate second link in the scene.
    # Skip the temporary collection link, not every object it contains.
    objects = set()
    seen = set()
    pending = [scene.collection]
    while pending:
        collection = pending.pop()
        if collection in seen or editor.is_helper(collection):
            continue
        seen.add(collection)
        content = opened.get(collection, collection)
        objects.update(content.objects)
        pending.extend(content.children)
    return objects


class UsageIndex:
    def __init__(self):
        self.definitions = [c for c in bpy.data.collections if hosts.is_definition(c)]
        contents = {c: editor.content(c) for c in self.definitions}
        opened = {c: work for c, work in contents.items() if work != c}
        self.roots = {
            scene: self._placements(_authored_scene_objects(scene, opened))
            for scene in bpy.data.scenes if not scene.get("_gm_edit")
        }
        self.children = {c: self._placements(work.all_objects) for c, work in contents.items()}
        # Small ordinary leaf groups commonly receive a collection update for each
        # vertex edit. Remember their membership so we can verify such updates locally.
        self.mesh_leaves = {
            c.session_uid: frozenset(obj.session_uid for obj in c.objects)
            for c, work in contents.items() if c == work and not self.children[c]
            and not c.children and 0 < len(c.objects) <= 64
            and all(obj.type == 'MESH' and not editor.is_helper(obj) for obj in c.objects)
        }
        self.placement_ids = {obj.session_uid for placements in (*self.roots.values(), *self.children.values())
                              for obj, _ in placements}
        self.parents = defaultdict(set)
        edges = {}
        for parent, children in self.children.items():
            edges[parent] = Counter(target for _, target in children)
            for target in edges[parent]:
                self.parents[target].add(parent)
        self.totals = defaultdict(dict)
        try:
            ordered = list(TopologicalSorter(self.parents).static_order())
        except CycleError:
            ordered = None
        for scene, placements in self.roots.items():
            if ordered is None:
                totals = Counter(target for _, target in self._walk(placements))
            else:
                totals = Counter(target for _, target in placements)
                for parent in ordered:
                    count = totals[parent]
                    if count:
                        for target, multiplicity in edges.get(parent, {}).items():
                            totals[target] += count * multiplicity
            for target, count in totals.items():
                self.totals[target][scene] = count

    @staticmethod
    def _placements(objects):
        # Blender exposes unique objects in both Scene.objects and Collection.all_objects.
        return [(obj, target) for obj in objects if not editor.is_helper(obj)
                and (target := hosts.definition(obj))]

    def _walk(self, placements, ancestors=None):
        stack = [(iter(placements), (), frozenset())]
        while stack:
            items, chain, seen = stack[-1]
            item = next(items, None)
            if item is None:
                stack.pop()
                continue
            host, target = item
            if target in seen or (ancestors is not None and target not in ancestors):
                continue
            current = (*chain, host)
            yield current, target
            children = self.children.get(target)
            if children:
                stack.append((iter(children), current, seen | {target}))

    def paths(self, scene, target):
        ancestors = {target}
        work = [target]
        while work:
            new = self.parents.get(work.pop(), set()) - ancestors
            ancestors.update(new)
            work.extend(new)
        for chain, found in self._walk(self.roots.get(scene, ()), ancestors):
            if found == target:
                yield chain


_index = None
_shape = None


def invalidate(*_):
    global _index, _shape
    _index = _shape = None


def _data():
    global _index, _shape
    shape = len(bpy.data.objects), len(bpy.data.collections), len(bpy.data.scenes)
    if _index is None or shape != _shape:
        _index = UsageIndex()
        _shape = shape
    return _index


def counts(definition):
    """All scenes that contain the definition, with their unfolded placement counts."""
    return dict(_data().totals.get(definition, {}))


def placement_label(definition):
    total = sum(counts(definition).values())
    return f"{total} instance" if total == 1 else f"{total} instances"


def paths(scene, definition, limit=100):
    result = _data().paths(scene, definition)
    return result if limit is None else islice(result, max(limit, 0))


@persistent
def _reset(*_):
    invalidate()


def _unchanged_mesh_leaves(depsgraph):
    touched = set()
    members = set()
    edited_meshes = set()
    member_meshes = set()
    for update in depsgraph.updates:
        item = update.id.original
        if isinstance(item, bpy.types.Collection):
            previous = _index.mesh_leaves.get(item.session_uid)
            if (previous is None or not hosts.is_definition(item) or item.children
                    or len(item.objects) != len(previous) or editor.content(item) != item):
                return False
            for obj in item.objects:
                if obj.session_uid not in previous or hosts.definition(obj) or editor.is_helper(obj):
                    return False
                member_meshes.add(obj.data)
            members.update(previous)
        elif isinstance(item, bpy.types.Object):
            # Editing an occurrence outside the group also updates the source member.
            # An old or new placement can affect counts even when its mesh is shared.
            if (item.type == 'MESH' and item.mode == 'EDIT' and update.is_updated_geometry
                    and item.session_uid not in _index.placement_ids and not hosts.definition(item)):
                edited_meshes.add(item.data)
            else:
                touched.add(item.session_uid)
        elif not isinstance(item, bpy.types.Mesh):
            return False
    return bool(members) and touched <= members and edited_meshes <= member_meshes


def _structure_changed(depsgraph):
    """Placement counts ignore moves. Rebuild only when objects or collections change."""
    updated = getattr(depsgraph, "id_type_updated", None)
    if updated is None:
        return True
    if updated('SCENE'):
        return True
    if updated('COLLECTION'):
        return not _unchanged_mesh_leaves(depsgraph)
    if not updated('OBJECT'):
        return False
    updates = list(getattr(depsgraph, "updates", ()) or ())
    if not updates:
        return True
    saw_object = False
    for update in updates:
        if not isinstance(getattr(update, "id", None), bpy.types.Object):
            continue
        saw_object = True
        if getattr(update, "is_updated_geometry", False) or not getattr(update, "is_updated_transform", False):
            return True
    return not saw_object


@persistent
def _on_update(scene, depsgraph):
    if _index is None:
        return
    if _structure_changed(depsgraph):
        invalidate()


def _by_uid(ids, uid):
    return next((item for item in ids if str(item.session_uid) == uid), None)


def _target(context, uid):
    if uid:
        col = _by_uid(bpy.data.collections, uid)
        return col if col and hosts.is_definition(col) else None
    col = hosts.definition(context.active_object)
    if col:
        return col
    path = editor.path(context)
    return hosts.definition(path[-1]) if path else None


def _visible(obj, context):
    return (
        obj.name in context.view_layer.objects and not obj.hide_select
        and obj.visible_get(view_layer=context.view_layer,
                            viewport=context.space_data if context.area and context.area.type == 'VIEW_3D' else None)
    )


def _select_only(context, items):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in items:
        obj.select_set(True)
    context.view_layer.objects.active = items[0]


def _selection_pool(context):
    route = editor.path(context)
    if route:
        definition = hosts.definition(route[-1])
        if definition is not None:
            return editor.content(definition).all_objects
    return context.selectable_objects


def _candidate(obj, context):
    return obj is not None and not editor.is_helper(obj) and _visible(obj, context)


def _matching_visible(context, target):
    """Visible objects in this view that share the link, including ones outside the open group."""
    items = []
    for obj in context.view_layer.objects:
        if not _candidate(obj, context):
            continue
        if instance_source(obj) != target:
            continue
        items.append(obj)
    return items


def instance_source(obj):
    """Return the shared Blender ID represented by a selectable placement."""
    if obj is None or editor.is_helper(obj):
        return None
    if obj.instance_type == 'COLLECTION':
        return obj.instance_collection
    return hosts.definition(obj) or obj.data


class GM_OT_SelectSimilar(bpy.types.Operator):
    bl_idname = "groupmanager.select_similar"
    bl_label = "Select Linked"
    bl_description = "Select visible objects that share this definition, instance collection, or object data"
    bl_options = {'REGISTER', 'UNDO'}

    definition_uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        active = context.active_object
        target = _target(context, self.definition_uid) if self.definition_uid else instance_source(active)
        if target is None:
            self.report({'WARNING'}, "Select a group, collection instance, or object with data")
            return {'CANCELLED'}
        route = editor.path(context)
        group = isinstance(target, bpy.types.Collection) and hosts.is_definition(target)
        if route and not group:
            members = set(editor.content(hosts.definition(route[-1])).all_objects)
            items = [obj for obj in context.view_layer.objects
                     if obj in members and instance_source(obj) == target and _candidate(obj, context)]
        else:
            items = _matching_visible(context, target)
        if not items:
            self.report({'INFO'}, "No selectable matching instances")
            return {'CANCELLED'}
        if active in items:
            items.remove(active)
            items.insert(0, active)
        _select_only(context, items)
        self.report({'INFO'}, f"Selected {len(items)} matching instances")
        return {'FINISHED'}


class GM_OT_select_grouped_kind(bpy.types.Operator):
    bl_idname = "groupmanager.select_grouped_kind"
    bl_label = "Group"
    bl_description = "Select visible objects that are groups"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        items = [obj for obj in _selection_pool(context)
                 if _candidate(obj, context) and hosts.definition(obj) is not None]
        if not items:
            self.report({'INFO'}, "No selectable groups")
            return {'CANCELLED'}
        active = context.active_object
        if active in items:
            items.remove(active)
            items.insert(0, active)
        _select_only(context, items)
        self.report({'INFO'}, f"Selected {len(items)}")
        return {'FINISHED'}


class GM_OT_select_linked_group(bpy.types.Operator):
    bl_idname = "groupmanager.select_linked_group"
    bl_label = "Group"
    bl_description = "Select visible group objects that share this group's definition"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        active = context.active_object
        target = hosts.definition(active)
        if target is None:
            self.report({'WARNING'}, "Select a group")
            return {'CANCELLED'}
        items = [obj for obj in _matching_visible(context, target)
                 if hosts.definition(obj) == target]
        if not items:
            self.report({'INFO'}, "No other selectable copies of this group")
            return {'CANCELLED'}
        if active in items:
            items.remove(active)
            items.insert(0, active)
        _select_only(context, items)
        self.report({'INFO'}, f"Selected {len(items)} linked groups")
        return {'FINISHED'}


class GROUPMANAGER_MT_select_grouped(bpy.types.Menu):
    bl_idname = "GROUPMANAGER_MT_select_grouped"
    bl_label = "Select Grouped"

    def draw(self, context):
        layout = self.layout
        layout.operator_enum("object.select_grouped", "type")
        layout.separator()
        layout.operator(GM_OT_select_grouped_kind.bl_idname, text="Group")


class GROUPMANAGER_MT_select_linked(bpy.types.Menu):
    bl_idname = "GROUPMANAGER_MT_select_linked"
    bl_label = "Select Linked"

    def draw(self, context):
        layout = self.layout
        layout.operator_enum("object.select_linked", "type")
        layout.separator()
        layout.operator(GM_OT_select_linked_group.bl_idname, text="Group")


class _SelectLayout(menu_hooks.Layout):
    def operator_menu_enum(self, operator, prop, *args, **kwargs):
        if prop == "type" and operator == "object.select_grouped":
            return self._layout.menu(GROUPMANAGER_MT_select_grouped.bl_idname, *args, **kwargs)
        if prop == "type" and operator == "object.select_linked":
            return self._layout.menu(GROUPMANAGER_MT_select_linked.bl_idname, *args, **kwargs)
        return self._layout.operator_menu_enum(operator, prop, *args, **kwargs)


_select_menu_hook = None
_linked_binding = None
_menus_live = False


def _hook_selection_menus():
    global _select_menu_hook, _linked_binding
    if _select_menu_hook is None:
        _select_menu_hook = menu_hooks.install(bpy.types.VIEW3D_MT_select_object, _SelectLayout)
    config = bpy.context.window_manager.keyconfigs.addon if bpy.context.window_manager else None
    if config is not None and _linked_binding is None:
        keymap = config.keymaps.new(name="Object Mode", space_type='EMPTY', region_type='WINDOW')
        item = keymap.keymap_items.new("wm.call_menu", 'L', 'PRESS', shift=True, head=True)
        item.properties.name = GROUPMANAGER_MT_select_linked.bl_idname
        _linked_binding = (keymap, item)


def _unhook_selection_menus():
    global _select_menu_hook, _linked_binding
    menu_hooks.restore(_select_menu_hook)
    _select_menu_hook = None
    if _linked_binding is not None:
        keymap, item = _linked_binding
        keymap.keymap_items.remove(item)
        _linked_binding = None


class GM_OT_Locate(bpy.types.Operator):
    bl_idname = "groupmanager.locate"
    bl_label = "Locate Link Group"

    scene_uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    path_uids: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = _by_uid(bpy.data.scenes, self.scene_uid)
        try:
            ids = json.loads(self.path_uids)
            if not isinstance(ids, list) or not ids:
                raise ValueError()
            if not scene:
                raise ValueError()
            members = (obj for obj, _ in _data().roots.get(scene, ()))
            route = []
            for uid in ids:
                obj = _by_uid(members, str(uid))
                col = hosts.definition(obj) if obj else None
                if not col:
                    raise ValueError()
                route.append(obj)
                members = editor.content(col).all_objects
        except (ValueError, AttributeError, TypeError):
            self.report({'WARNING'}, "The Link Group path changed. Open usages again")
            return {'CANCELLED'}
        if not context.window or context.mode != 'OBJECT':
            self.report({'WARNING'}, "Locate it from Object Mode in a Blender window")
            return {'CANCELLED'}
        previous_path = editor.path(context)
        previous_stage = context.window.scene
        previous_selection = list(context.selected_objects)
        previous_active = context.active_object
        target = route[-1]
        if target.hide_select or target.hide_viewport:
            self.report({'WARNING'}, f"{target.name} is hidden or selection is locked")
            return {'CANCELLED'}
        try:
            if previous_path and (previous_stage != scene or len(route) == 1):
                if 'FINISHED' not in bpy.ops.groupmanager.back(all=True):
                    return {'CANCELLED'}
            if previous_stage != scene:
                context.window.scene = scene
            if len(route) > 1:
                editor.open_path(context, route[:-1])
            if not _visible(target, context):
                raise ValueError(f"{target.name} cannot be selected in the current view layer")
            _select_only(context, [target])
        except (ValueError, RuntimeError) as error:
            if editor.path(context):
                bpy.ops.groupmanager.back(all=True)
            context.window.scene = previous_stage
            if previous_path:
                editor.open_path(context, previous_path)
            if previous_selection:
                _select_only(context, previous_selection)
            if previous_active and previous_active.name in context.view_layer.objects:
                context.view_layer.objects.active = previous_active
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_Usages(bpy.types.Operator):
    bl_idname = "groupmanager.usages"
    bl_label = "Users"

    definition_uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    offset: IntProperty(default=0, min=0, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        target = _target(context, self.definition_uid)
        if not target:
            return {'CANCELLED'}
        self.definition_uid = str(target.session_uid)
        return context.window_manager.invoke_popup(self, width=480)

    def execute(self, context):
        return {'FINISHED'}

    def draw(self, context):
        target = _target(context, self.definition_uid)
        if not target:
            self.layout.label(text="Definition was removed")
            return
        scene_counts = counts(target)
        total = sum(scene_counts.values())
        self.layout.label(text=f"{target.name} · {total}")
        if not total:
            return
        page_size = 25
        base = 0
        for scene, count in scene_counts.items():
            start = max(0, self.offset - base)
            end = min(count, self.offset + page_size - base)
            if start < end:
                self.layout.label(text=f"{scene.name} · {count}", icon='SCENE_DATA')
                for route in islice(paths(scene, target, limit=end), start, end):
                    button = self.layout.operator(
                        "groupmanager.locate", text=" › ".join(o.name for o in route),
                        icon='VIEWZOOM', emboss=False,
                    )
                    button.scene_uid = str(scene.session_uid)
                    button.path_uids = json.dumps([obj.session_uid for obj in route])
            base += count
        row = self.layout.row(align=True)
        for offset, label in ((self.offset - page_size, "Previous"), (self.offset + page_size, "Next")):
            if 0 <= offset < total:
                button = row.operator("groupmanager.usages", text=label)
                button.definition_uid = self.definition_uid
                button.offset = offset


CLASSES = (GM_OT_SelectSimilar, GM_OT_select_grouped_kind, GM_OT_select_linked_group,
           GROUPMANAGER_MT_select_grouped, GROUPMANAGER_MT_select_linked,
           GM_OT_Locate, GM_OT_Usages)


def _hook_if_live():
    if _menus_live:
        _hook_selection_menus()
    return None


def register():
    global _menus_live
    _menus_live = True
    _hook_selection_menus()
    if not bpy.app.timers.is_registered(_hook_if_live):
        bpy.app.timers.register(_hook_if_live, first_interval=1.0)
    for key in ("load_pre", "undo_pre", "redo_pre", "load_post", "undo_post", "redo_post"):
        handlers = getattr(bpy.app.handlers, key)
        if _reset not in handlers:
            handlers.append(_reset)
    if _on_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_update)
    invalidate()


def unregister():
    global _menus_live
    _menus_live = False
    _unhook_selection_menus()
    for key in ("load_pre", "undo_pre", "redo_pre", "load_post", "undo_post", "redo_post"):
        handlers = getattr(bpy.app.handlers, key)
        if _reset in handlers:
            handlers.remove(_reset)
    if _on_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_update)
    invalidate()
