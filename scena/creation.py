"""Create and place editable collection definitions in the current scene or source view."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, StringProperty
from mathutils import Matrix, Quaternion, Vector

from . import editor, hosts
from .exchange_ids import copy_block


DEFAULT_KIND = 'NODES'
KINDS = (
    ('NATIVE', "Collection", "Place the group as a collection. Suited to shared assets and mixed objects", 'OUTLINER_COLLECTION', 0),
    ('NODES', "Geometry Nodes", "Place the group on a mesh so modifiers can affect it", 'GEOMETRY_NODES', 1),
)
PIVOTS = (
    ('ACTIVE', "Active Object", "Use the active object's origin"),
    ('CURSOR', "3D Cursor", "Use the 3D cursor location"),
    ('CENTER', "Bounds Center", "Center of the selected objects' bounds"),
    ('BOTTOM', "Bottom Center", "Bottom center of the selection bounds on the chosen axis"),
)
ORIENTATIONS = (
    ('WORLD', "World", "Use world axes"),
    ('ACTIVE', "Active Object", "Use the active object's axes"),
    ('CURSOR', "3D Cursor", "Use the 3D cursor axes"),
)

CREATE_DEFAULT_FIELDS = ("kind", "pivot", "orientation", "include_children")


def _scene_default(field):
    """Use the add-on preference until this scene explicitly stores a value."""
    def read(settings, stored, is_set):
        if is_set:
            return stored
        from . import preferences
        prefs = preferences.get(bpy.context)
        if prefs is None:
            return stored
        value = getattr(prefs, "default_" + field)
        prop = settings.bl_rna.properties[field]
        if prop.type == 'ENUM' and isinstance(value, str):
            # RNA transforms receive and return the enum's numeric value.
            return prop.enum_items[value].value
        return value

    return read


class GM_PG_create_settings(bpy.types.PropertyGroup):
    group_name: StringProperty(name="Group Name", description="Collection name. Scena adds a Group_ prefix",
                               get_transform=_scene_default("group_name"))
    kind: EnumProperty(name="Group Type", items=KINDS, default=DEFAULT_KIND,
                       get_transform=_scene_default("kind"))
    pivot: EnumProperty(name="Origin", items=PIVOTS, default='ACTIVE',
                        get_transform=_scene_default("pivot"))
    orientation: EnumProperty(name="Local Axes", items=ORIENTATIONS, default='WORLD',
                              get_transform=_scene_default("orientation"))
    include_children: BoolProperty(name="Include Children", default=False,
                                   get_transform=_scene_default("include_children"))


def current_settings(context):
    return context.scene.gm_create_settings


class GM_OT_cycle_create_kind(bpy.types.Operator):
    bl_idname = "groupmanager.cycle_create_kind"
    bl_label = "Group Type"
    bl_options = {'UNDO'}

    @classmethod
    def description(cls, context, properties):
        kind = current_settings(context).kind
        if kind == 'NODES':
            return "Geometry Nodes. Click to create a Collection"
        return "Collection. Click to create with Geometry Nodes"

    def execute(self, context):
        from . import preferences
        settings = current_settings(context)
        kind = 'NATIVE' if settings.kind == 'NODES' else 'NODES'
        prefs = preferences.get(context)
        if prefs is not None:
            prefs.default_kind = kind
            if settings.is_property_set("kind"):
                settings.property_unset("kind")
        else:
            settings.kind = kind
        return {'FINISHED'}


class GM_OT_reset_create_defaults(bpy.types.Operator):
    bl_idname = "groupmanager.reset_create_defaults"
    bl_label = "Reset to Add-on Defaults"
    bl_description = "Make this scene follow the add-on defaults for type, origin, axes, and children again"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.scene is None or not context.scene.is_editable:
            return False
        settings = current_settings(context)
        return any(settings.is_property_set(name) for name in CREATE_DEFAULT_FIELDS)

    def execute(self, context):
        settings = current_settings(context)
        for name in CREATE_DEFAULT_FIELDS:
            settings.property_unset(name)
        return {'FINISHED'}


def select_result(context, obj):
    context.view_layer.update()
    for old in context.selected_objects:
        old.select_set(False)
    if obj and context.view_layer.objects.get(obj.name) == obj:
        obj.select_set(True)
        context.view_layer.objects.active = obj


def _writable(data):
    if data.library or data.override_library or not data.is_editable:
        raise ValueError(f"Make a local editable copy first: {data.name}")


def _edit_path(context):
    return editor.path(context)


def _link_target(context):
    path = _edit_path(context)
    target = hosts.definition(path[-1]) if path else context.collection
    if target is None:
        target = context.scene.collection
    _writable(target)
    return target


def _contains_collection(source, target):
    pending = [source]
    seen = set()
    while pending:
        coll = pending.pop()
        if coll == target:
            return True
        if coll in seen:
            continue
        seen.add(coll)
        pending.extend(coll.children)
        for obj in coll.objects:
            child = hosts.definition(obj) or obj.instance_collection
            if child:
                pending.append(child)
    return False


def ensure_no_cycle(destination, objects):
    for obj in objects:
        source = hosts.definition(obj) or obj.instance_collection
        if source and _contains_collection(source, destination):
            raise ValueError(f"Adding {obj.name} would create a cycle")


def _object_bounds(context, objects):
    """Use evaluated instance geometry for Empty/GN definitions, and origins as fallback."""
    depsgraph = context.evaluated_depsgraph_get()
    points = []
    found = set()
    objects = set(objects)

    def include(obj, matrix, owner):
        bounds = obj.bound_box
        if obj.type in {'EMPTY', 'LIGHT', 'CAMERA', 'SPEAKER'} or not bounds:
            return
        if all(tuple(corner) == (-1.0, -1.0, -1.0) for corner in bounds):
            return
        points.extend(matrix @ Vector(corner) for corner in bounds)
        found.add(owner)

    for instance in depsgraph.object_instances:
        obj = instance.object
        if instance.is_instance:
            owner = instance.parent.original if instance.parent else None
        else:
            owner = obj.original
        if owner in objects:
            include(obj, instance.matrix_world, owner)
    points.extend(obj.matrix_world.translation.copy() for obj in objects - found)
    return points


def _group_frame(context, objects, active, pivot, orientation):
    cursor = context.scene.cursor
    if orientation == 'ACTIVE':
        rotation = active.matrix_world.to_quaternion()
    elif orientation == 'CURSOR':
        rotation = cursor.matrix.to_quaternion()
    else:
        rotation = Quaternion()
    if pivot == 'CURSOR':
        origin = cursor.location.copy()
    elif pivot == 'ACTIVE':
        origin = active.matrix_world.translation.copy()
    else:
        inverse_rotation = rotation.inverted()
        local_points = [inverse_rotation @ point for point in _object_bounds(context, objects)]
        bottom = Vector(min(point[i] for point in local_points) for i in range(3))
        top = Vector(max(point[i] for point in local_points) for i in range(3))
        center = (bottom + top) * .5
        if pivot == 'BOTTOM':
            center.z = bottom.z
        origin = rotation @ center
    return Matrix.LocRotScale(origin, rotation, None)


def _check_members(objects, scene):
    """Reframing a selected object must not detach other users from its placement."""
    selected = set(objects)
    if not selected:
        raise ValueError("Select group members first")
    for obj in selected:
        _writable(obj)
        if len(obj.users_collection) != 1:
            raise ValueError(f"{obj.name} belongs to more than one collection. Choose which collection to move")
        _writable(obj.users_collection[0])
        scenes = set(obj.users_scene)
        if scenes - {scene}:
            raise ValueError(f"{obj.name} also belongs to another scene. Make a local copy first")
        if obj.parent and obj.parent not in selected:
            raise ValueError(f"Also select parent {obj.parent.name} ({obj.name})")
        outside = next((child for child in obj.children if child not in selected), None)
        if outside:
            raise ValueError(f"Also select child {outside.name} ({obj.name})")
    for obj, users in bpy.data.user_map(subset=selected).items():
        for user in users:
            # Text datablocks can store add-on ID registries. Moving an object
            # keeps its ID and all of those registry pointers intact.
            if user in selected or isinstance(user, (bpy.types.Collection, bpy.types.Text)):
                continue
            if user == scene and scene.camera != obj:
                continue
            raise ValueError(f"{user.name} references selected object {obj.name}. Check that outside reference first")
    return [obj for obj in objects if obj.parent not in selected]


def _referenced_objects(objects):
    """Objects outside this set that the selection still points at."""
    members = set(objects)
    found = set()
    for target, users in bpy.data.user_map(key_types={'OBJECT'}, value_types={'OBJECT'}).items():
        if isinstance(target, bpy.types.Object) and target not in members and users & members:
            found.add(target)
    return found


def _clear_object_pointers(datablock, external):
    if datablock is None:
        return
    for prop in datablock.bl_rna.properties:
        if prop.type != 'POINTER' or prop.is_readonly:
            continue
        fixed = getattr(prop, "fixed_type", None)
        if fixed is None or fixed.bl_rna.identifier != 'Object':
            continue
        if getattr(datablock, prop.identifier, None) not in external:
            continue
        try:
            setattr(datablock, prop.identifier, None)
        except (AttributeError, TypeError, ValueError):
            pass


def _constraint_snapshot(constraint):
    props = {}
    for prop in constraint.bl_rna.properties:
        if prop.identifier in {"rna_type", "type"} or prop.is_readonly or prop.type == 'COLLECTION':
            continue
        try:
            props[prop.identifier] = getattr(constraint, prop.identifier)
        except (AttributeError, TypeError):
            pass
    return constraint.type, props


def _restore_constraint(obj, kind, props):
    copy = obj.constraints.new(kind)
    for identifier, value in props.items():
        try:
            setattr(copy, identifier, value)
        except (AttributeError, TypeError, ValueError):
            pass
    return copy


def _drop_external_links(objects):
    """Drop links to outside objects. Returns a function that puts those links back."""
    external = _referenced_objects(objects)
    if not external:
        return lambda: None
    undo = []

    def remember(action):
        undo.append(action)

    for obj in objects:
        if obj.parent in external:
            parent, inverse, world = obj.parent, obj.matrix_parent_inverse.copy(), obj.matrix_world.copy()
            parent_type, parent_bone = obj.parent_type, obj.parent_bone
            obj.parent = None
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = world

            def restore_parent(obj=obj, parent=parent, inverse=inverse, world=world,
                               parent_type=parent_type, parent_bone=parent_bone):
                obj.parent = parent
                obj.parent_type = parent_type
                obj.parent_bone = parent_bone
                obj.matrix_parent_inverse = inverse
                obj.matrix_world = world

            remember(restore_parent)
        for constraint in list(obj.constraints):
            target = getattr(constraint, "target", None)
            slots = list(getattr(constraint, "targets", []) or [])
            if target in external or any(getattr(slot, "target", None) in external for slot in slots):
                kind, props = _constraint_snapshot(constraint)
                obj.constraints.remove(constraint)

                def restore_constraint(obj=obj, kind=kind, props=props):
                    _restore_constraint(obj, kind, props)

                remember(restore_constraint)
                continue
            cleared = []
            for prop in constraint.bl_rna.properties:
                if prop.type != 'POINTER' or prop.is_readonly:
                    continue
                fixed = getattr(prop, "fixed_type", None)
                if fixed is None or fixed.bl_rna.identifier != 'Object':
                    continue
                current = getattr(constraint, prop.identifier, None)
                if current not in external:
                    continue
                setattr(constraint, prop.identifier, None)
                cleared.append((prop.identifier, current))
            if cleared:
                def restore_constraint_pointers(constraint=constraint, cleared=cleared):
                    for identifier, value in cleared:
                        setattr(constraint, identifier, value)

                remember(restore_constraint_pointers)
        for modifier in obj.modifiers:
            cleared = []
            for prop in modifier.bl_rna.properties:
                if prop.type != 'POINTER' or prop.is_readonly:
                    continue
                fixed = getattr(prop, "fixed_type", None)
                if fixed is None or fixed.bl_rna.identifier != 'Object':
                    continue
                current = getattr(modifier, prop.identifier, None)
                if current not in external:
                    continue
                try:
                    setattr(modifier, prop.identifier, None)
                except (AttributeError, TypeError, ValueError):
                    continue
                cleared.append((prop.identifier, current))
            group = getattr(modifier, "node_group", None)
            interface = getattr(group, "interface", None)
            if interface is not None:
                for item in interface.items_tree:
                    if getattr(item, "socket_type", "") != 'NodeSocketObject':
                        continue
                    try:
                        current = modifier[item.identifier]
                    except (KeyError, TypeError):
                        continue
                    if current not in external:
                        continue
                    modifier[item.identifier] = None
                    cleared.append((item.identifier, current, True))
            if cleared:

                def restore_modifier(modifier=modifier, cleared=cleared):
                    for item in cleared:
                        if len(item) == 3:
                            modifier[item[0]] = item[1]
                        else:
                            setattr(modifier, item[0], item[1])

                remember(restore_modifier)
        for system in obj.particle_systems:
            settings = system.settings
            if settings is None or settings.instance_object not in external:
                continue
            instance = settings.instance_object
            settings.instance_object = None

            def restore_particle(settings=settings, instance=instance):
                settings.instance_object = instance

            remember(restore_particle)

    def restore():
        for action in reversed(undo):
            action()

    return restore


def _check_transform(objects, roots, transform):
    if transform == Matrix.Identity(4):
        return
    members = set(objects)
    for target, users in bpy.data.user_map(key_types={'OBJECT'}, value_types={'OBJECT'}).items():
        if target not in members and users & members:
            owner = next(iter(users & members))
            raise ValueError(f"{owner.name} references external object {target.name}. Include that dependency or keep the current axes")
    for root in roots:
        if root.constraints or root.animation_data:
            raise ValueError(f"{root.name} has a constraint or object animation. Resolve that before changing its axes")
        desired = transform @ root.matrix_world
        pos, rotation, scale = desired.decompose()
        recomposed = Matrix.LocRotScale(pos, rotation, scale)
        error = max(abs(recomposed[r][c] - desired[r][c]) for r in range(3) for c in range(3))
        magnitude = max(1.0, *(abs(scale[i]) for i in range(3)))
        if error > 1e-5 * magnitude:
            raise ValueError(f"{root.name} would get shear in its new local transform, which an object transform cannot store")


def _move(objects, destination, roots, transform):
    old_owners = {obj: obj.users_collection[0] for obj in objects}
    worlds = {obj: obj.matrix_world.copy() for obj in roots}
    originals = {obj: obj.matrix_basis.copy() for obj in roots}
    try:
        for obj in objects:
            if old_owners[obj] != destination:
                destination.objects.link(obj)
                old_owners[obj].objects.unlink(obj)
        if transform != Matrix.Identity(4):
            for obj, world in worlds.items():
                obj.matrix_world = transform @ world
    except Exception:
        for obj, owner in old_owners.items():
            if owner.objects.get(obj.name) != obj:
                owner.objects.link(obj)
            if destination != owner and destination.objects.get(obj.name) == obj:
                destination.objects.unlink(obj)
        for obj, original in originals.items():
            obj.matrix_basis = original
        raise


def _outside_group_targets(context, objects):
    """The open group plus other selected groups, when those groups are not members here."""
    route = editor.path(context)
    if not route:
        return None
    outsiders = [obj for obj in objects if not editor.in_scope(context, obj)]
    if not outsiders:
        return None
    if any(not hosts.definition(obj) for obj in outsiders):
        raise ValueError("Select only members of the group being edited. Add new members with Add to Group")
    targets = [route[-1]]
    for obj in outsiders:
        if obj not in targets:
            targets.append(obj)
    return targets


def create_group(context, objects, group_name, kind=DEFAULT_KIND, pivot='ACTIVE', orientation='WORLD'):
    objects = list(objects)
    name = group_name.strip() or "Group"
    context.view_layer.update()
    grouped = _outside_group_targets(context, objects)
    if grouped:
        while editor.path(context) and any(not editor.in_scope(context, obj) for obj in grouped):
            editor.back(context, 1)
        objects = grouped
    active = context.view_layer.objects.active
    if active not in objects:
        active = objects[0]
    route = editor.path(context)
    frame = _group_frame(context, objects, active, pivot, orientation)
    if route:
        local = editor.frame(context).inverted()
        center = local @ frame.translation
        rotation = (local.to_3x3() @ frame.to_3x3()).to_quaternion()
        frame = Matrix.LocRotScale(center, rotation, None)
    with editor.source_space(context):
        host = _create_in_source(context, objects, name, kind, active, frame)
    select_result(context, host)
    if grouped:
        editor.open_path(context, [*editor.path(context), host])
    return host


def _create_in_source(context, objects, name, kind, active, frame):
    roots = _check_members(objects, context.scene)
    target = active.users_collection[0]
    ensure_no_cycle(target, objects)
    # Blender applies collection.instance_offset in native and GN placement alike.
    # Pure translation can thus preserve source coordinates, constraints and actions.
    no_rotation = frame.to_3x3() == Matrix.Identity(3)
    transform = Matrix.Identity(4) if no_rotation else frame.inverted()
    _check_transform(objects, roots, transform)
    source = bpy.data.collections.new(name)
    host = None
    try:
        if no_rotation:
            source.instance_offset = frame.translation
        hosts.mark_definition(source)
        host = hosts.make_host(name, source, kind)
        target.objects.link(host)
        host.matrix_world = frame
        _move(objects, source, roots, transform)
    except Exception:
        if host:
            _discard_host(host)
        bpy.data.collections.remove(source)
        raise
    return host


def _discard_host(host):
    data = host.data
    bpy.data.objects.remove(host, do_unlink=True)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
        bpy.data.meshes.remove(data)


class GM_OT_create(bpy.types.Operator):
    bl_idname = "groupmanager.create"
    bl_label = "Create Group"
    bl_description = "Turn the selected objects into a reusable shared group"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty(name="Definition", options={'SKIP_SAVE'})
    kind: EnumProperty(name="Group Type", items=KINDS, default=DEFAULT_KIND, options={'SKIP_SAVE'})
    pivot: EnumProperty(name="Origin", items=PIVOTS, default='ACTIVE', options={'SKIP_SAVE'})
    orientation: EnumProperty(name="Local Axes", items=ORIENTATIONS, default='WORLD', options={'SKIP_SAVE'})
    include_children: BoolProperty(name="Include children of the selected hierarchy", default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(context.selected_objects) > 0

    def draw(self, context):
        layout = self.layout
        for name in ("group_name", "kind", "pivot", "orientation", "include_children"):
            layout.prop(self, name)
        selection = set(context.selected_objects)
        missing = {child for obj in selection for child in obj.children_recursive} - selection
        if missing:
            action = "Also include" if self.include_children else "Missing"
            layout.label(text=f"{action} {len(missing)} child objects", icon='OUTLINER')

    def execute(self, context):
        settings = current_settings(context)
        for name in ("group_name", "kind", "pivot", "orientation", "include_children"):
            if not self.properties.is_property_set(name, ghost=False):
                setattr(self, name, getattr(settings, name))
        if not self.group_name.strip():
            active = context.active_object or context.selected_objects[0]
            self.group_name = active.name
        try:
            members = list(context.selected_objects)
            if self.include_children:
                members = list(dict.fromkeys(members + [ch for obj in members for ch in obj.children_recursive]))
            create_group(context, members, self.group_name, self.kind, self.pivot, self.orientation)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def _place_at_cursor(context, source, kind, name):
    target = _link_target(context)
    if _contains_collection(source, target):
        raise ValueError("A group cannot be placed inside itself or a descendant")
    position = context.scene.cursor.location.copy()
    if editor.path(context):
        position = editor.frame(context).inverted() @ position
    with editor.source_space(context):
        host = hosts.make_host(name, source, kind)
        try:
            target.objects.link(host)
            host.location = position
        except Exception:
            _discard_host(host)
            raise
    select_result(context, host)


class GM_OT_add_group(bpy.types.Operator):
    bl_idname = "groupmanager.add_group"
    bl_label = "Add Group"
    bl_description = "Group the selected objects, or add an empty group at the 3D cursor"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        if context.selected_objects:
            return bpy.ops.groupmanager.create('EXEC_DEFAULT')
        settings = current_settings(context)
        name = settings.group_name.strip() or "Group"
        source = bpy.data.collections.new(name)
        try:
            hosts.mark_definition(source)
            _place_at_cursor(context, source, settings.kind, name)
        except (ValueError, RuntimeError) as exc:
            bpy.data.collections.remove(source)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_duplicate_shared(bpy.types.Operator):
    bl_idname = "groupmanager.duplicate_shared"
    bl_label = "Duplicate Linked"
    bl_description = "Duplicate this Link Group and keep the whole instance linked to the original"
    bl_options = {'REGISTER', 'UNDO'}

    offset: FloatVectorProperty(name="World Offset", size=3, subtype='TRANSLATION')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and hosts.definition(context.active_object) is not None

    def execute(self, context):
        obj = context.active_object
        try:
            offset = Vector(self.offset)
            route = editor.path(context)
            if route:
                if not editor.in_scope(context, obj):
                    raise ValueError("Select a Link Group in the group being edited")
                offset = editor.frame(context).inverted().to_3x3() @ offset
            with editor.source_space(context):
                duplicate = self._copy(context, obj, offset)
            select_result(context, duplicate)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

    def _copy(self, context, obj, offset):
        if not obj.users_collection:
            raise ValueError("The Link Group has no collection")
        if set(obj.users_scene) - {context.scene}:
            raise ValueError("This Link Group also belongs to other scenes. Choose its scope first")
        for coll in obj.users_collection:
            _writable(coll)
        duplicate = copy_block(obj)
        try:
            from .layout_tools import EXCLUDED, INDEX, SETTINGS, SLOTS
            for key in (EXCLUDED, INDEX, SETTINGS, SLOTS):
                if key in duplicate:
                    del duplicate[key]
            for coll in obj.users_collection:
                coll.objects.link(duplicate)
            if any(offset):
                duplicate.matrix_world = Matrix.Translation(offset) @ obj.matrix_world
        except Exception:
            _discard_host(duplicate)
            raise
        return duplicate


def _selected_group_copies(context):
    """Every selected object is a group. A plain object keeps Blender's own duplicate."""
    selected = [obj for obj in context.selected_objects if obj is not None and not editor.is_helper(obj)]
    if not selected or any(hosts.definition(obj) is None for obj in selected):
        return None
    return selected


class GM_OT_duplicate_move(bpy.types.Operator):
    bl_idname = "groupmanager.duplicate_move"
    bl_label = "Duplicate"
    bl_description = "Copy the selected groups"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(
        name="Type",
        items=[
            ('GROUP', "Group", "New collection for this group. Nested groups stay linked. Member meshes stay linked"),
            ('INSTANCE', "Instance", "Another instance of the same group. Editing this copy changes the original"),
            ('NESTED', "Group and Nested", "New collections for this group and every nested group. Member meshes stay linked"),
        ],
        default='GROUP',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.selected_objects

    def execute(self, context):
        groups = _selected_group_copies(context)
        if not groups:
            self.report({'WARNING'}, "Select a group")
            return {'CANCELLED'}
        try:
            from . import branches
            with editor.source_space(context):
                copies = branches.duplicate_placements(
                    groups, linked=self.kind == 'INSTANCE', nested=self.kind == 'NESTED')
            for obj in list(context.selected_objects):
                obj.select_set(False)
            context.view_layer.update()
            for obj in copies:
                if context.view_layer.objects.get(obj.name) == obj:
                    obj.select_set(True)
            context.view_layer.objects.active = copies[-1]
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

    def invoke(self, context, event):
        if _selected_group_copies(context) is None:
            if self.kind == 'NESTED':
                return {'PASS_THROUGH'}
            operator = bpy.ops.object.duplicate_move_linked if self.kind == 'INSTANCE' else bpy.ops.object.duplicate_move
            return operator('INVOKE_DEFAULT')
        result = self.execute(context)
        if result != {'FINISHED'}:
            return result
        return bpy.ops.transform.translate('INVOKE_DEFAULT')


class GM_OT_add_members(bpy.types.Operator):
    bl_idname = "groupmanager.add_members"
    bl_label = "Add to Group"
    bl_description = "Add selected objects to the shared group. They appear in every linked placement"
    bl_options = {'REGISTER', 'UNDO'}

    def _candidates(self, context):
        route = editor.path(context)
        host = route[-1] if route else context.active_object
        objects = [obj for obj in context.selected_objects
                   if obj != host and obj not in route and obj not in editor.peers(context)]
        if not route:
            return objects
        source = hosts.definition(host)
        members = editor.content(source).all_objects if source else []
        return [obj for obj in objects if not editor.is_helper(obj) and members.get(obj.name) != obj]

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            return False
        route = editor.path(context)
        if route:
            members = editor.content(hosts.definition(route[-1])).all_objects
            return any(not editor.is_helper(obj) and obj not in route and obj not in editor.peers(context)
                       and members.get(obj.name) != obj for obj in context.selected_objects)
        return hosts.definition(context.active_object) is not None and len(context.selected_objects) > 1

    def execute(self, context):
        editor.follow_selection(context)
        route = editor.path(context)
        host = route[-1] if route else context.active_object
        source = hosts.definition(host)
        objects = self._candidates(context)
        released = []
        try:
            if not objects:
                raise ValueError("Select an object outside the active group")
            restore_links = _drop_external_links(objects)
            context.view_layer.update()
            if route:
                captured = {obj: obj.matrix_world.copy() for obj in objects}
                for obj in objects:
                    owner = next((peer for peer in editor.peers(context) if editor._owns(peer, obj)), None)
                    if owner and owner not in released:
                        released.append(owner)
                for peer in released:
                    editor.close_peer(context, peer)
                for obj, world in captured.items():
                    obj.matrix_world = world
            _writable(source)
            try:
                transform = (editor.frame(context).inverted() if route else
                             Matrix.Translation(source.instance_offset) @ host.matrix_world.inverted())
            except ValueError:
                raise ValueError("The group scale is zero, so members cannot be converted into group space") from None
            with editor.source_space(context):
                ensure_no_cycle(source, objects)
                roots = _check_members(objects, context.scene)
                _check_transform(objects, roots, transform)
                _move(objects, source, roots, transform)
            select_result(context, objects[0] if route else host)
        except (ValueError, RuntimeError) as exc:
            if "restore_links" in locals():
                restore_links()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            for peer in released:
                try:
                    if hosts.definition(peer) and not editor.host_is_open(peer):
                        editor.open_peer(context, peer)
                except (ValueError, RuntimeError):
                    pass
        return {'FINISHED'}


class GM_OT_remove_members(bpy.types.Operator):
    bl_idname = "groupmanager.remove_members"
    bl_label = "Remove from Group"
    bl_description = "Remove selected members from every linked placement and keep one copy outside the edited group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects) and bool(_edit_path(context))

    def execute(self, context):
        path = _edit_path(context)
        host = path[-1]
        source = hosts.definition(host)
        objects = list(context.selected_objects)
        try:
            with editor.source_space(context):
                if len(host.users_collection) != 1:
                    raise ValueError("This Link Group's collection membership is not unique")
                target = host.users_collection[0]
                _writable(target)
                if any(source.objects.get(obj.name) != obj for obj in objects):
                    raise ValueError("Select a direct member of the current group")
                roots = _check_members(objects, context.scene)
                ensure_no_cycle(target, objects)
                transform = host.matrix_world @ Matrix.Translation(-source.instance_offset)
                _check_transform(objects, roots, transform)
                _move(objects, target, roots, transform)
            context.view_layer.update()
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (
    GM_PG_create_settings,
    GM_OT_cycle_create_kind,
    GM_OT_reset_create_defaults,
    GM_OT_create,
    GM_OT_add_group,
    GM_OT_duplicate_shared,
    GM_OT_duplicate_move,
    GM_OT_add_members,
    GM_OT_remove_members,
)
