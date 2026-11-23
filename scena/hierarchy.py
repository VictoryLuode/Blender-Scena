"""Group existing Blender hierarchies and unpack a placement to a chosen depth."""

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from mathutils import Matrix

from . import branches, creation, editor, hosts, usage


HOST_KINDS = (
    ('NATIVE', "Collection", "Place as a collection", 'OUTLINER_COLLECTION', 0),
    ('NODES', "Geometry Nodes", "Place with a mesh so group modifiers can be added", 'GEOMETRY_NODES', 1),
)
OUTPUT_TYPES = (
    ('FLAT', "Put members in the current collection", "Keep existing parent relationships between members"),
    ('EMPTY', "Keep an Empty hierarchy", "Each expanded Link Group gets a parent Empty"),
    ('COLLECTION', "Keep a collection hierarchy", "Each expanded Link Group gets a child collection"),
)


def _context_collection(context):
    selected = [item for item in getattr(context, 'selected_ids', ())
                if isinstance(item, bpy.types.Collection)]
    return selected[0] if len(selected) == 1 else context.collection


def _check_layer_settings(layer, subtree):
    if layer.collection in subtree and (
        layer.exclude or layer.hide_viewport or layer.holdout or layer.indirect_only
    ):
        raise ValueError(f'View-layer state on collection "{layer.name}" cannot move into the instance')
    for child in layer.children:
        _check_layer_settings(child, subtree)


def group_from_collection(context, source, kind=creation.DEFAULT_KIND, editing=None):
    """Replace a single scene link with a placement of the entire original tree."""
    scene = context.scene
    if source is None or source == scene.collection:
        raise ValueError("Select a child collection in the Outliner")
    if any(hosts.definition(placement) == source for placement in editor.path(context)):
        raise ValueError("Go up one level before converting the source collection being edited")
    if not source.is_editable or source.override_library:
        raise ValueError("Make a local collection from the read-only library collection first")
    parents = [parent for parent in (scene.collection, *bpy.data.collections)
               if source in parent.children[:]]
    scenes = [s for s in bpy.data.scenes if source in s.collection.children_recursive]
    if editing:
        if source not in editing.children_recursive or scenes:
            raise ValueError("Select a child collection that belongs only to the current group")
    if len(parents) != 1 or (not editing and scenes != [scene]):
        raise ValueError("Select a collection with a single parent link in the current scene")
    parent = parents[0]
    if not parent.is_editable or parent.override_library:
        raise ValueError("The parent collection is read-only, so its link cannot be replaced")
    subtree = {source, *source.children_recursive}
    members = set(source.all_objects)
    for layer in scene.view_layers:
        _check_layer_settings(layer.layer_collection, subtree)
    for member in members:
        if member.parent and member.parent not in members:
            raise ValueError(f'"{member.name}" has a parent outside the collection')
        if any(owner not in subtree for owner in member.users_collection):
            raise ValueError(f'"{member.name}" also belongs to a collection outside the source')
    users = bpy.data.user_map(subset=members)
    for member, references in users.items():
        for user in references:
            if user in members or user in subtree or isinstance(user, bpy.types.Text):
                continue
            if user == scene and scene.camera != member:
                continue
            raise ValueError(f'Outside data "{user.name}" references member "{member.name}"')
    original_fake = source.use_fake_user
    definition_tag = source.get(hosts.DEFINITION_KEY)
    graphs = set(bpy.data.node_groups)
    host = None
    try:
        hosts.mark_definition(source)
        host = hosts.make_host(source.name, source, kind)
        host.location = source.instance_offset
        parent.objects.link(host)
        parent.children.unlink(source)
    except Exception:
        if source not in parent.children[:]:
            parent.children.link(source)
        if host:
            hosts._remove_host(host)
        for tree in set(bpy.data.node_groups) - graphs:
            if tree.get(hosts.GRAPH_KEY) and not tree.users:
                bpy.data.node_groups.remove(tree)
        if definition_tag is None:
            if hosts.DEFINITION_KEY in source:
                del source[hosts.DEFINITION_KEY]
        else:
            source[hosts.DEFINITION_KEY] = definition_tag
        source.use_fake_user = original_fake
        raise
    return host


def group_from_empty(context, root, kind=creation.DEFAULT_KIND):
    if root is None or root.type != 'EMPTY' or hosts.definition(root) or not root.children:
        raise ValueError("Select a parent Empty that has children")
    return creation.create_group(context, [root, *root.children_recursive], root.name,
                                 kind=kind, pivot='ACTIVE', orientation='ACTIVE')


def unpack_counts(host, depth=1):
    """Return expanded placements and final members without allocating Blender IDs."""
    cache = {}

    def count(source, remaining, path):
        if source in path:
            raise ValueError(f'Definition "{source.name}" contains a cycle')
        key = (source, remaining)
        if key in cache:
            return cache[key]
        layers, objects = 1, 0
        for obj in source.all_objects:
            child = hosts.definition(obj)
            if child and remaining != 1:
                sublayers, subobjects = count(child, max(0, remaining - 1), path | {source})
                layers += sublayers
                objects += subobjects
            else:
                objects += 1
        cache[key] = (layers, objects)
        return cache[key]

    source = hosts.definition(host)
    if not source or depth < 0:
        raise ValueError("Select a Link Group and a valid depth")
    return count(source, depth, set())


def _anchor_for(placement, owner):
    anchor = bpy.data.objects.new(placement.name + "_GM_Hierarchy", None)
    owner.objects.link(anchor)
    anchor.parent = placement.parent
    anchor.parent_type = placement.parent_type
    anchor.parent_bone = placement.parent_bone
    anchor.matrix_parent_inverse = placement.matrix_parent_inverse
    anchor.matrix_basis = placement.matrix_basis
    return anchor


def _attach_root(obj, anchor):
    # Preserve the evaluated matrix even if a scaled parent introduces shear.
    relative = anchor.matrix_world.inverted() @ obj.matrix_world
    basis = obj.matrix_basis.copy()
    obj.parent = anchor
    obj.parent_type = 'OBJECT'
    if abs(basis.determinant()) > 1e-10:
        obj.matrix_parent_inverse = relative @ basis.inverted()
        obj.matrix_basis = basis
    else:
        obj.matrix_parent_inverse = relative
        obj.matrix_basis = Matrix.Identity(4)


def dissolve_depth(context, host, depth=1, output='FLAT'):
    """Finish the entire expansion in staging before replacing the original host."""
    if output not in {kind for kind, _, _ in OUTPUT_TYPES}:
        raise ValueError("Unknown ungroup hierarchy mode")
    unpack_counts(host, depth)
    branches._check_root(host)
    owners = branches._check_unpack(host, hosts.definition(host))
    if not owners or any(not owner.is_editable or owner.override_library for owner in owners):
        raise ValueError("The Link Group's collection is unlinked or read-only")
    before = set(bpy.data.user_map())
    owned = set()
    try:
        stage = bpy.data.collections.new("GM_Ungroup")
        owned.add(stage)
        context.scene.collection.children.link(stage)
        staged_host = host.copy()
        owned.add(staged_host)
        stage.objects.link(staged_host)
        pending = [(staged_host, depth)]
        while pending:
            placement, remaining = pending.pop()
            context.view_layer.update()
            anchor = None
            if output == 'EMPTY':
                anchor = _anchor_for(placement, stage)
                owned.add(anchor)
                if abs(placement.matrix_world.determinant()) < 1e-10:
                    raise ValueError("This Link Group has a zero axis scale, so a reversible parent cannot be created")
            elif output == 'COLLECTION':
                destination = bpy.data.collections.new(placement.name)
                owned.add(destination)
                owners_before = tuple(placement.users_collection)
                for owner in owners_before:
                    owner.children.link(destination)
                destination.objects.link(placement)
                for owner in owners_before:
                    owner.objects.unlink(placement)
            members = branches.dissolve(placement)
            owned.update(members)
            owned.update(obj.data for obj in members if obj.data and obj.data not in before)
            owned.update(stage.children_recursive)
            context.view_layer.update()
            member_set = set(members)
            for obj in members:
                if anchor and obj.parent not in member_set:
                    _attach_root(obj, anchor)
                if remaining != 1 and hosts.definition(obj):
                    pending.append((obj, max(0, remaining - 1)))
        result = list(stage.all_objects)
        structural = list(stage.children_recursive)
        for owner in owners:
            for obj in (stage.objects if output == 'COLLECTION' else result):
                owner.objects.link(obj)
            if output == 'COLLECTION':
                for child in stage.children:
                    owner.children.link(child)
        bpy.data.collections.remove(stage)
        if output != 'COLLECTION':
            # Retain any collection still referenced by a copied member or modifier.
            while structural:
                unused = [collection for collection in structural if not collection.users]
                if not unused:
                    break
                structural = [collection for collection in structural if collection not in unused]
                bpy.data.batch_remove(ids=unused)
        carrier = host.data if hosts.host_type(host) == 'NODES' else None
        bpy.data.objects.remove(host, do_unlink=True)
        if carrier and not carrier.users:
            bpy.data.meshes.remove(carrier)
    except Exception:
        new_ids = owned.intersection(set(bpy.data.user_map()) - before)
        if new_ids:
            bpy.data.batch_remove(ids=new_ids)
        raise
    return result


class _HierarchyAction:
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            self.apply(context)
        except (RuntimeError, ValueError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        usage.invalidate()
        return {'FINISHED'}


def _collection_menu(menu, context):
    if GROUPMANAGER_OT_from_collection.poll(context):
        menu.layout.operator("groupmanager.from_collection", icon='OUTLINER_COLLECTION')


def _object_menu(menu, context):
    if GROUPMANAGER_OT_from_empty.poll(context):
        menu.layout.operator("groupmanager.from_empty_hierarchy", icon='EMPTY_AXIS')


class GROUPMANAGER_OT_from_collection(_HierarchyAction, bpy.types.Operator):
    bl_idname = "groupmanager.from_collection"
    bl_label = "Group from Collection"
    bl_description = "Turn this collection and its children into a Link Group in place"
    kind: EnumProperty(name="Type", items=HOST_KINDS, default=creation.DEFAULT_KIND)

    @classmethod
    def register(cls):
        bpy.types.OUTLINER_MT_collection.append(_collection_menu)

    @classmethod
    def unregister(cls):
        bpy.types.OUTLINER_MT_collection.remove(_collection_menu)

    @classmethod
    def poll(cls, context):
        source = _context_collection(context)
        if context.mode != 'OBJECT' or source is None or source == context.scene.collection:
            return False
        route = editor.path(context)
        return not route or source in editor.content(hosts.definition(route[-1])).children_recursive

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def apply(self, context):
        source = _context_collection(context)
        route = editor.path(context)
        editing = hosts.definition(route[-1]) if route else None
        with editor.source_space(context):
            placement = group_from_collection(context, source, self.kind, editing)
        creation.select_result(context, placement)


class GROUPMANAGER_OT_from_empty(_HierarchyAction, bpy.types.Operator):
    bl_idname = "groupmanager.from_empty_hierarchy"
    bl_label = "Group from Empty Hierarchy"
    bl_description = "Group the selected parent and all descendants, keeping object parenting"
    kind: EnumProperty(name="Type", items=HOST_KINDS, default=creation.DEFAULT_KIND)

    @classmethod
    def register(cls):
        bpy.types.OUTLINER_MT_object.append(_object_menu)

    @classmethod
    def unregister(cls):
        bpy.types.OUTLINER_MT_object.remove(_object_menu)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'OBJECT' and obj is not None and obj.type == 'EMPTY'
                and bool(obj.children) and not hosts.definition(obj) and editor.in_scope(context, obj))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def apply(self, context):
        placement = group_from_empty(context, context.active_object, self.kind)
        creation.select_result(context, placement)


class GROUPMANAGER_OT_dissolve_depth(_HierarchyAction, bpy.types.Operator):
    bl_idname = "groupmanager.dissolve_depth"
    bl_label = "Ungroup by Depth"
    bl_description = "Expand this Link Group level by level and choose the output hierarchy"
    depth: IntProperty(name="Depth (0 = all)", default=1, min=0, soft_max=8)
    output: EnumProperty(name="Output", items=OUTPUT_TYPES, default='FLAT')

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return context.mode == 'OBJECT' and bool(hosts.definition(active)) and editor.in_scope(context, active)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, 'depth')
        self.layout.prop(self, 'output')
        try:
            layers, objects = unpack_counts(context.active_object, self.depth)
            self.layout.label(text=f"Expand {layers} Link Groups into {objects} members")
        except ValueError as exc:
            self.layout.label(text=str(exc), icon='ERROR')

    def apply(self, context):
        from .operations import edit_placement
        objects = edit_placement(context, context.active_object, True,
                                 lambda place: dissolve_depth(context, place, self.depth, self.output))
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in objects:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
                context.view_layer.objects.active = obj


class SCENA_OT_collect_selected(bpy.types.Operator):
    bl_idname = "scena.collect_selected"
    bl_label = "New Collection from Selection"
    bl_description = "Move the selected Outliner objects and collections into a new collection"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name: StringProperty(name="Name", default="Collection", options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'OUTLINER'
                and context.space_data.display_mode in {'VIEW_LAYER', 'SCENES'}
                and any(isinstance(item, (bpy.types.Object, bpy.types.Collection))
                        and item != context.scene.collection for item in context.selected_ids))

    def execute(self, context):
        root = context.scene.collection
        selected = set(context.selected_ids)
        groups = {item for item in selected if isinstance(item, bpy.types.Collection) and item != root}
        objects = {item for item in selected if isinstance(item, bpy.types.Object)}

        paths = {root: (root,)}
        stack = [root]
        owners = {item: [] for item in groups}
        while stack:
            parent = stack.pop()
            for child in parent.children:
                if child in groups:
                    owners[child].append(parent)
                if child not in paths:
                    paths[child] = (*paths[parent], child)
                    stack.append(child)
        if groups - paths.keys():
            self.report({'WARNING'}, "Select collections in the current scene")
            return {'CANCELLED'}

        covered = {child for group in groups for child in group.children_recursive}
        moves = []
        for item in (groups - covered) | objects:
            if item in groups:
                candidates = owners[item]
                attr = "children"
            else:
                if any(owner in groups or owner in covered for owner in item.users_collection):
                    continue
                candidates = [owner for owner in item.users_collection if owner in paths]
                attr = "objects"
            if not candidates:
                self.report({'WARNING'}, "Select items in the current scene")
                return {'CANCELLED'}
            # Keep other collection links of the same datablock intact.
            owner = context.collection if context.collection in candidates else candidates[0]
            moves.append((item, owner, attr))
        if not moves:
            return {'CANCELLED'}
        common = set.intersection(*(set(paths[owner]) for _, owner, _ in moves))
        parent = max(common, key=lambda col: len(paths[col]))
        if any(not col.is_editable or col.override_library for col in [parent, *(m[1] for m in moves)]):
            self.report({'WARNING'}, "The source and destination collections must be editable")
            return {'CANCELLED'}

        new = bpy.data.collections.new(self.collection_name.strip() or "Collection")
        removed = []
        try:
            parent.children.link(new)
            for item, owner, attr in moves:
                getattr(new, attr).link(item)
            for item, owner, attr in moves:
                getattr(owner, attr).unlink(item)
                removed.append((item, owner, attr))
        except RuntimeError as exc:
            for item, owner, attr in removed:
                getattr(owner, attr).link(item)
            bpy.data.collections.remove(new)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Created {new.name}")
        return {'FINISHED'}


CLASSES = (GROUPMANAGER_OT_dissolve_depth, SCENA_OT_collect_selected)
