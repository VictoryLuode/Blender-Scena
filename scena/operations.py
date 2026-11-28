from contextlib import ExitStack

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import branches, creation, editor, hosts, instance_mirror, usage


class PlacementAction:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and hosts.definition(context.active_object) is not None

    def execute(self, context):
        try:
            self.apply(context)
        except (ValueError, RuntimeError) as exc:
            usage.invalidate()
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        usage.invalidate()
        return {'FINISHED'}


def edit_placement(context, obj, local, action):
    route = editor.path(context)
    if route:
        if not editor.in_scope(context, obj):
            raise ValueError("Select a Link Group inside the group being edited")
        if local:
            return editor.scoped_local(context, obj, action)
        with editor.source_space(context):
            return action(obj)
    if local:
        branches.localize_parent_path([obj])
    return action(obj)


def switch_targets(context):
    selected = [obj for obj in context.selected_objects if hosts.host_type(obj)]
    return selected or editor.path(context)[-1:]


class GROUPMANAGER_OT_switch_type(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.switch_type"
    bl_label = "Switch Group Type"
    bl_description = "Convert the selected groups to the specified type using their existing collections"

    kind: EnumProperty(name="Type", items=[
        ('NATIVE', "Collection", "Collection instance", 'OUTLINER_COLLECTION', 0),
        ('NODES', "Geometry Nodes", "Mesh with a collection reference node", 'GEOMETRY_NODES', 1)])
    local: BoolProperty(name="This Path Only", default=True)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(switch_targets(context))

    def apply(self, context):
        targets = [obj for obj in switch_targets(context) if hosts.host_type(obj) != self.kind]
        if not targets:
            raise ValueError("The selected groups already use this type")
        route = editor.path(context)
        for obj in targets:
            try:
                if not route and self.local:
                    branches.localize_parent_path([obj])
                hosts.check_switch(obj, self.kind)
            except ValueError as exc:
                raise ValueError(f"{obj.name}: {exc}") from exc

        switch_open_group = bool(route and targets == [route[-1]])
        if route and not switch_open_group and not all(editor.in_scope(context, obj) for obj in targets):
            raise ValueError("Select groups inside the current edit level, or return to the scene first")

        selected = list(context.selected_objects)
        active = context.active_object
        active_index = selected.index(active) if active in selected else None
        wanted = set(targets)
        target_indices = [i for i, obj in enumerate(selected) if obj in wanted]
        transaction = ExitStack()
        with editor.source_space(context, transaction=transaction):
            changes = transaction.enter_context(branches.BranchChanges())
            switches = transaction.enter_context(hosts.HostSwitches())
            if switch_open_group:
                switches.convert(route[-1], self.kind)
                return
            if route and self.local:
                local_indices = [i for i, obj in enumerate(selected) if editor.in_scope(context, obj)]
                members = [selected[i] for i in local_indices]
                localized = changes.parent_path([*route, targets[0]], extras=members)
                for i, member in zip(local_indices, members):
                    selected[i] = member
            for i in target_indices:
                name = selected[i].name
                try:
                    selected[i] = switches.convert(selected[i], self.kind)
                except (ValueError, ReferenceError, RuntimeError) as exc:
                    raise ValueError(f"{name}: {exc}") from exc
            if route and self.local:
                editor.sync_path(context, localized[:-1])

        if route:
            layer = context.view_layer
            restored = [obj for obj in selected if layer.objects.get(obj.name) == obj]
            for obj in list(context.selected_objects):
                if obj not in restored:
                    obj.select_set(False)
            for obj in restored:
                obj.select_set(True)
            if active_index is not None and selected[active_index] in restored:
                layer.objects.active = selected[active_index]


# Keep the strings backing Blender's dynamic enum alive while the search popup is open.
_group_items = []


def _group_data_items(self, context):
    items = [(str(coll.session_uid), coll.name_full, "", 'OUTLINER_COLLECTION', coll.session_uid)
             for coll in bpy.data.collections if hosts.is_definition(coll)]
    if items != _group_items:
        _group_items[:] = items
    return _group_items


class GROUPMANAGER_OT_choose_data(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.choose_data"
    bl_label = "Browse Group Data"
    bl_description = "Use an existing group definition for the selected object"
    bl_property = "definition"

    definition: EnumProperty(name="Data", items=_group_data_items)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (super().poll(context) and obj.is_editable and not obj.override_library
                and editor.in_scope(context, obj) and not editor.host_is_open(obj))

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'FINISHED'}

    def apply(self, context):
        obj = context.active_object
        uid = self.definition
        target = next((c for c in bpy.data.collections if str(c.session_uid) == uid), None)
        if target is None or not hosts.is_definition(target):
            raise ValueError("Choose an existing group definition")
        if target == hosts.definition(obj):
            return

        def rebind(host):
            if any(creation._contains_collection(target, owner) for owner in host.users_collection):
                raise ValueError("A group cannot contain itself")
            with branches.BranchChanges() as changes:
                changes.point(host, target)
                if hosts.get_realize(host):
                    hosts.check_realize(host, True)
            return host

        result = edit_placement(context, obj, True, rebind)
        if context.view_layer.objects.get(result.name) == result:
            result.select_set(True)
            context.view_layer.objects.active = result


class GROUPMANAGER_OT_realize(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.realize"
    bl_label = "Realize Instances"
    bl_description = "Toggle realized geometry on this Link Group while keeping the source collection"

    enabled: BoolProperty(name="Realize Geometry", default=True)
    local: BoolProperty(name="This Path Only", default=True)

    @classmethod
    def poll(cls, context):
        return super().poll(context) and hosts.host_type(context.active_object) == 'NODES'

    def apply(self, context):
        obj = context.active_object
        if hosts.get_realize(obj) == self.enabled:
            return
        hosts.check_realize(obj, self.enabled)
        edit_placement(context, obj, self.local, lambda place: hosts.set_realize(place, self.enabled))


class GROUPMANAGER_OT_prepare_hard_ops(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.prepare_hard_ops"
    bl_label = "Prepare for Hard Ops"
    bl_description = "Give the active placement mesh output for Hard Ops modifiers while keeping its shared source"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (super().poll(context) and obj.is_editable and not obj.override_library
                and editor.in_scope(context, obj) and not editor.host_is_open(obj))

    def apply(self, context):
        obj = context.active_object
        if hosts.get_realize(obj):
            return
        hosts.check_switch(obj, 'NODES')
        unsupported = hosts._non_mesh_member(hosts.definition(obj))
        if unsupported:
            raise ValueError(f"{unsupported.name} ({unsupported.type}) needs instance output")

        def prepare(place):
            if hosts.host_type(place) != 'NODES':
                place = hosts.switch_host(place, 'NODES')
            hosts.set_realize(place, True)
            return place

        result = edit_placement(context, obj, True, prepare)
        creation.select_result(context, result)


class GROUPMANAGER_OT_make_single_user(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.make_single_user"
    bl_label = "Make Single User"
    bl_description = "Make this group single-user at the chosen depth, then select that instance"

    scope: EnumProperty(name="Scope", items=[
        ('OBJECT', "Group", "Group transforms and modifiers. Meshes and materials stay linked"),
        ('OBJECT_DATA', "Group and Data", "Also make meshes, curves, and other data single-user. Materials stay linked"),
        ('OBJECT_DATA_MATERIALS', "Group, Data, and Materials", "Also make meshes and the materials on members single-user"),
    ], default='OBJECT_DATA')

    def invoke(self, context, event):
        if self.properties.is_property_set("scope", ghost=False):
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=390)

    def draw(self, context):
        source = hosts.definition(context.active_object)
        layout = self.layout
        if source:
            layout.label(text=usage.placement_label(source), icon='LINKED')
        layout.column().prop(self, "scope", expand=True)
        mesh = "Meshes and other data stay shared" if self.scope == 'OBJECT' else "Meshes and other data become independent"
        materials = "Materials become independent" if self.scope == 'OBJECT_DATA_MATERIALS' else "Materials stay shared"
        layout.label(text=mesh)
        layout.label(text=materials)
        layout.label(text="Nested group definitions stay shared")

    @classmethod
    def description(cls, context, properties):
        source = hosts.definition(context.active_object)
        if source and properties.scope == 'OBJECT':
            count = sum(usage.counts(source).values())
            return (f"{count} placements share this group. Give this placement its own group. "
                    "Meshes stay linked. For independent vertices use Make Single User > Group and Data")
        return cls.bl_description

    def apply(self, context):
        obj = context.active_object
        if not editor.in_scope(context, obj):
            raise ValueError("Select a Link Group inside the group being edited")
        route = editor.path(context) + [obj]
        editor.open_path(context, route, unique=True, scope=self.scope, stay_on_host=True)


class GROUPMANAGER_OT_fork(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.fork"
    bl_label = "Duplicate Object"
    bl_description = "Create a new group for the selected Link Groups. Meshes and materials stay linked to the original"

    group_name: StringProperty(name="Name")

    def invoke(self, context, event):
        self.group_name = hosts.definition(context.active_object).name + "_GM_Version"
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "group_name")
        coll = hosts.definition(context.active_object)
        selected = list(context.selected_objects)
        if any(hosts.definition(obj) != coll for obj in selected):
            self.layout.label(text="Select objects that share the same data", icon='ERROR')
            return
        n = len(selected)
        total = sum(usage.counts(coll).values())
        self.layout.label(text=f"{n} use the new group · {max(0, total - n)} keep the original")
        self.layout.label(text="Meshes and materials stay linked. Nested groups stay linked")

    def apply(self, context):
        if editor.path(context):
            raise ValueError("Duplicate Object applies to objects in the scene. Inside a group, use Duplicate.")
        branches.fork_selected(list(context.selected_objects), self.group_name.strip() or None)


def _selected_placements(context):
    active = context.active_object
    if not editor.in_scope(context, active):
        raise ValueError("Select a Link Group inside the group being edited")
    source = hosts.definition(active)
    ordered = []
    seen = set()
    for obj in (active, *context.selected_objects):
        if obj is None or obj in seen or hosts.definition(obj) != source or not editor.in_scope(context, obj):
            continue
        seen.add(obj)
        ordered.append(obj)
    return ordered


def _dissolve_local_many(context, route, targets, modifier_mode, full=False):
    active = targets[0]
    editor._validate(context.scene, list(route) + [active])
    extras = [obj for obj in targets if obj != active]
    unpack = branches.dissolve_fully_many if full else branches.dissolve_many
    with editor.source_space(context):
        with branches.BranchChanges() as changes:
            localized = changes.parent_path(list(route) + [active], extras=extras)
            members = unpack([localized[-1], *extras], localized[-1], modifier_mode)
            editor.sync_path(context, localized[:-1])
            return members


class GROUPMANAGER_OT_instance_mirror(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.instance_mirror"
    bl_label = "Instance Mirror"
    bl_description = "Mirror shared group instances. Choose Mesh in Scena Group for mesh mirroring"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (super().poll(context) and hosts.host_type(obj) == 'NODES'
                and obj.is_editable and not obj.override_library and editor.in_scope(context, obj))

    def apply(self, context):
        edit_placement(context, context.active_object, False, instance_mirror.toggle)


class GROUPMANAGER_OT_split_mirror(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.split_mirror"
    bl_label = "Split Mirror"
    bl_description = "Split a mirrored Link Group into two linked instances and keep the other modifiers"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (super().poll(context) and not obj.library and not obj.override_library
                and any(mod.type == 'MIRROR' and any(mod.use_axis) for mod in obj.modifiers)
                and not instance_mirror.adapter(obj) and bool(obj.users_collection)
                and sum(not scene.get('_gm_edit') for scene in obj.users_scene) <= 1)

    def apply(self, context):
        original = context.active_object
        duplicate = branches.split_mirror(original)
        original.select_set(True)
        if context.view_layer.objects.get(duplicate.name):
            duplicate.select_set(True)
        context.view_layer.objects.active = original


def _ungroup_selected(context, local, full):
    targets = _selected_placements(context)
    active = targets[0]
    route = editor.path(context)
    mode = 'KEEP' if getattr(context.window_manager, "gm_keep_group_modifiers", False) else 'DELETE'
    many = branches.dissolve_fully_many if full else branches.dissolve_many
    one = branches.dissolve_fully if full else branches.dissolve
    if not route:
        if local:
            for obj in targets:
                branches.localize_parent_path([obj])
        members = many(targets, active, mode)
    elif len(targets) == 1:
        members = edit_placement(context, active, local, lambda place: one(place, mode))
    elif local:
        members = _dissolve_local_many(context, route, targets, mode, full)
    else:
        with editor.source_space(context):
            members = many(targets, active, mode)
    for obj in list(context.selected_objects):
        obj.select_set(False)
    for obj in members:
        if context.view_layer.objects.get(obj.name):
            obj.select_set(True)
            context.view_layer.objects.active = obj


class GROUPMANAGER_OT_dissolve(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.dissolve"
    bl_label = "Ungroup"
    bl_description = "Ungroup the selected placements one level. Shift-click to ungroup all nested levels"

    local: BoolProperty(name="This Path Only", default=True)
    full: BoolProperty(name="Ungroup All Levels", description="Ungroup every nested level", default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        self.full = bool(event.shift)
        return self.execute(context)

    def apply(self, context):
        _ungroup_selected(context, self.local, self.full)


class GROUPMANAGER_OT_force_ungroup(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.force_ungroup"
    bl_label = "Force"
    bl_description = "Ungroup the selected Link Groups and every group inside them"

    local: BoolProperty(name="This Path Only", default=True)

    def apply(self, context):
        _ungroup_selected(context, self.local, True)


def _delete_local_many(context, route, targets):
    active = targets[0]
    editor._validate(context.scene, list(route) + [active])
    extras = [obj for obj in targets if obj != active]
    with editor.source_space(context):
        with branches.BranchChanges() as changes:
            localized = changes.parent_path(list(route) + [active], extras=extras)
            branches.delete_placements([localized[-1], *extras])
            editor.sync_path(context, localized[:-1])


class GROUPMANAGER_OT_delete_group(PlacementAction, bpy.types.Operator):
    bl_idname = "groupmanager.delete_group"
    bl_label = "Delete Group"
    bl_description = "Delete the selected Link Groups. The source group remains for other users"

    local: BoolProperty(name="This Path Only", default=True)

    def apply(self, context):
        targets = _selected_placements(context)
        active = targets[0]
        route = editor.path(context)
        if not route:
            if self.local:
                for obj in targets:
                    branches.localize_parent_path([obj])
            branches.delete_placements(targets)
        elif len(targets) == 1:
            edit_placement(context, active, self.local, lambda place: branches.delete_placements([place]))
        elif self.local:
            _delete_local_many(context, route, targets)
        else:
            with editor.source_space(context):
                branches.delete_placements(targets)


CLASSES = (
    GROUPMANAGER_OT_switch_type, GROUPMANAGER_OT_choose_data, GROUPMANAGER_OT_realize,
    GROUPMANAGER_OT_prepare_hard_ops, GROUPMANAGER_OT_make_single_user, GROUPMANAGER_OT_fork, GROUPMANAGER_OT_dissolve,
    GROUPMANAGER_OT_force_ungroup, GROUPMANAGER_OT_delete_group,
    GROUPMANAGER_OT_instance_mirror, GROUPMANAGER_OT_split_mirror,
)
