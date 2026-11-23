"""Edits to a version's materials, origin and placement pointers."""

from contextlib import ExitStack

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from mathutils import Matrix, Quaternion, Vector

from . import branches, editor, hosts, menu_hooks, usage


def _members(source):
    return list(source.all_objects)


def _require_local(item):
    if not item.is_editable or item.override_library:
        raise ValueError(f'"{item.name}" is a read-only asset or a library override')


def _check_exclusive_member(obj, source):
    allowed = {source, *source.children_recursive}
    if any(coll not in allowed for coll in obj.users_collection):
        raise ValueError(f'"{obj.name}" also belongs to another collection. Make this version unique first')


def _top_placements(context):
    objects = list(context.selected_objects)
    if not objects or any(not hosts.definition(obj) for obj in objects):
        raise ValueError("Select only Link Groups")
    if editor.path(context):
        raise ValueError("Return to the scene and select the Link Group to change on its own")
    definition_collections = {coll for root in bpy.data.collections if hosts.is_definition(root)
                              for coll in (root, *root.children_recursive)}
    for obj in objects:
        _require_local(obj)
        if len(obj.users_scene) != 1:
            raise ValueError(f'"{obj.name}" is the same Link Group object in more than one scene')
        if definition_collections.intersection(obj.users_collection):
            raise ValueError(f'"{obj.name}" is also a member of another group')
    return objects


def _material_plan(source, material):
    """Map each affected definition to its matching members and child placements."""
    plans = {}
    visiting = set()

    def visit(current):
        if current in visiting:
            raise ValueError(f'The definition of "{current.name}" contains a cycle')
        if current in plans:
            return plans[current]
        visiting.add(current)
        matches, children = {}, {}
        for obj in _members(current):
            child = hosts.definition(obj)
            if child:
                if visit(child):
                    children[obj] = child
            elif hasattr(obj.data, "materials"):
                slots = [i for i, slot in enumerate(obj.material_slots)
                         if slot.material == material]
                if material is None and not obj.material_slots:
                    slots.append(-1)
                if slots:
                    matches[obj] = slots
        visiting.remove(current)
        plans[current] = (matches, children) if matches or children else None
        return plans[current]

    visit(source)
    return {coll: plan for coll, plan in plans.items() if plan}


def _restore_slot(obj, index, old_link, old_material, old_data_material):
    obj.data.materials[index] = old_data_material
    slot = obj.material_slots[index]
    slot.link = old_link
    if old_link == 'OBJECT':
        slot.material = old_material


def _restore_data(obj, old_data, copy):
    obj.data = old_data
    if copy.users == 0:
        bpy.data.batch_remove(ids=(copy,))


def _write_slots(obj, indices, material, rollback):
    # Realize Instances reads data-level materials. Give a shared datablock a
    # local copy before writing so rendered GN and native placements agree.
    if obj.data.users > 1 or obj.data.library:
        old_data = obj.data
        local_data = old_data.copy()
        rollback.callback(_restore_data, obj, old_data, local_data)
        obj.data = local_data
    if indices == [-1]:
        obj.data.materials.append(material)
        rollback.callback(obj.data.materials.pop, index=0)
        return
    for index in indices:
        slot = obj.material_slots[index]
        rollback.callback(_restore_slot, obj, index, slot.link, slot.material,
                          obj.data.materials[index])
        slot.link = 'DATA'
        slot.material = material


def replace_material(source, old, new, selected=None):
    """Change slots within this version; isolate shared children along matched paths."""
    if new is None or old == new:
        raise ValueError("Choose a different target material")
    plans = _material_plan(source, old)
    if source not in plans:
        raise ValueError("No matching material slots in this definition or its subgroups")
    if selected is not None:
        if not selected or any(hosts.definition(obj) != source for obj in selected):
            raise ValueError("Selected Link Groups must use the same definition")
        for obj in selected:
            _require_local(obj)
    else:
        _require_local(source)
        for obj in (*plans[source][0], *plans[source][1]):
            _require_local(obj)
            _check_exclusive_member(obj, source)

    with branches.BranchChanges() as change, ExitStack() as rollback:
        derived = {}

        def apply(current):
            if current in derived:
                return derived[current]
            members, children = plans[current]
            if current == source and selected is None:
                target = source
                mapping = {obj: obj for obj in (*members, *children)}
            else:
                target, mapping = change.clone(current, f"{current.name} · {new.name}")
            derived[current] = target
            for obj, indices in members.items():
                _write_slots(mapping[obj], indices, new, rollback)
            for host, child in children.items():
                change.point(mapping[host], apply(child))
            return target

        target = apply(source)
        for obj in selected or ():
            change.point(obj, target)
        rollback.pop_all()
    return target


def _resolve_material(name):
    materials = [mat for mat in bpy.data.materials if mat.name == name]
    if len(materials) != 1:
        raise ValueError(f"Choose a material with a definite name: {name or '(empty)'}")
    return materials[0]


class _ManagedGroupOperator:
    @classmethod
    def poll(cls, context):
        active = context.active_object
        return context.mode == 'OBJECT' and bool(hosts.definition(active)) and editor.in_scope(context, active)


class GM_OT_replace_material(_ManagedGroupOperator, bpy.types.Operator):
    bl_idname = "groupmanager.replace_material"
    bl_label = "Replace Group Material"
    bl_description = "Split shared subgroups by material slot, then replace materials on this definition or the selected Link Groups"
    bl_options = {'REGISTER', 'UNDO'}

    scope: EnumProperty(name="Apply To", items=[
        ('SELECTED', "Selected Link Groups share a new version", ""),
        ('DEFINITION', "Whole definition", ""),
    ])
    mode: EnumProperty(name="Match", items=[
        ('REPLACE', "Chosen Material", ""), ('EMPTY', "Empty Slots", ""),
    ])
    old_material: StringProperty(name="From")
    new_material: StringProperty(name="To")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=410)

    def draw(self, context):
        layout = self.layout
        source = hosts.definition(context.active_object)
        layout.prop(self, "scope")
        layout.prop(self, "mode")
        if self.mode == 'REPLACE':
            layout.prop_search(self, "old_material", bpy.data, "materials")
        layout.prop_search(self, "new_material", bpy.data, "materials")
        if not source:
            return
        affected = len(context.selected_objects) if self.scope == 'SELECTED' else sum(
            usage.counts(source).values())
        layout.label(text=f"{source.name} · {affected} affected")
        if self.mode == 'EMPTY' or self.old_material:
            try:
                old = None if self.mode == 'EMPTY' else _resolve_material(self.old_material)
                plan = _material_plan(source, old)
                slots = sum(len(slots) for members, _ in plan.values() for slots in members.values())
                layout.label(text=f"{slots} source slots across {len(plan)} definitions")
            except ValueError as error:
                layout.label(text=str(error), icon='ERROR')

    def execute(self, context):
        try:
            source = hosts.definition(context.active_object)
            selected = _top_placements(context) if self.scope == 'SELECTED' else None
            old = None if self.mode == 'EMPTY' else _resolve_material(self.old_material)
            with editor.source_space(context):
                result = replace_material(source, old, _resolve_material(self.new_material), selected)
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Updated materials on {result.name}")
        return {'FINISHED'}


class GM_OT_match_version(_ManagedGroupOperator, bpy.types.Operator):
    bl_idname = "groupmanager.match_version"
    bl_label = "Use Active Version"
    bl_description = "Keep each Link Group and its transform, and point the other selected Link Groups at the active definition"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return super().poll(context) and len(context.selected_objects) > 1

    def execute(self, context):
        active = context.active_object
        source = hosts.definition(active)
        try:
            targets = [obj for obj in _top_placements(context) if hosts.definition(obj) != source]
            if not targets:
                raise ValueError("The selected Link Groups already use this version")
            with branches.BranchChanges() as change:
                for obj in targets:
                    change.point(obj, source)
                    if hosts.get_realize(obj):
                        hosts.check_realize(obj, True)
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


def _source_bounds(source):
    points = []

    def visit(current, matrix, ancestors):
        if current in ancestors:
            raise ValueError("The group definition contains a cycle")
        base = matrix @ Matrix.Translation(-current.instance_offset)
        for obj in _members(current):
            world = base @ obj.matrix_world
            child = hosts.definition(obj)
            if child:
                visit(child, world, ancestors | {current})
            elif obj.type not in {'EMPTY', 'LIGHT', 'CAMERA', 'SPEAKER'}:
                box = obj.bound_box
                if box and not all(tuple(p) == (-1, -1, -1) for p in box):
                    points.extend(world @ Vector(p) for p in box)

    visit(source, Matrix.Identity(4), set())
    if not points:
        raise ValueError("The group has no geometry with bounds. Use the 3D cursor instead")
    low = Vector([min(p[i] for p in points) for i in range(3)])
    high = Vector([max(p[i] for p in points) for i in range(3)])
    return low, high


def _check_transform(obj, matrix):
    _require_local(obj)
    if obj.constraints or obj.animation_data or obj.rigid_body:
        raise ValueError(f'The transform of "{obj.name}" is driven by animation, a constraint, or rigid body')
    if (any(obj.delta_location) or any(obj.delta_rotation_euler)
            or tuple(obj.delta_rotation_quaternion) != (1., 0., 0., 0.)
            or tuple(obj.delta_scale) != (1., 1., 1.)):
        raise ValueError(f'"{obj.name}" has a delta transform')
    decomposed = Matrix.LocRotScale(*matrix.decompose())
    if max(abs(matrix[r][c] - decomposed[r][c]) for r in range(4) for c in range(4)) > 1e-5:
        raise ValueError(f'"{obj.name}" would get shear that a Blender object cannot store')


def _hold_local_modifiers(placement, old_world):
    """Keep a mirror plane on the origin this placement had before the origin moved."""
    mirrors = [modifier for modifier in placement.modifiers
               if modifier.type == 'MIRROR' and modifier.mirror_object is None]
    if not mirrors:
        return None
    anchor = bpy.data.objects.new(f"{placement.name}_GM_Mirror", None)
    anchor.empty_display_size = 0.25
    anchor.hide_viewport = True
    anchor.hide_render = True
    collections = list(placement.users_collection) or [bpy.context.scene.collection]
    for collection in collections:
        collection.objects.link(anchor)
    anchor.parent = placement
    anchor.matrix_world = old_world
    for modifier in mirrors:
        modifier.mirror_object = anchor
    return anchor


def reframe(active, cursor, pivot='CURSOR', orientation='KEEP', parent_frame=None, world_point=None):
    """Change source coordinates and compensate every managed placement."""
    source = hosts.definition(active)
    _require_local(source)
    if any(source == coll.children.get(source.name) for coll in bpy.data.collections):
        raise ValueError("The definition is also shown directly in another collection. Close that edit view first")
    if any(source == scene.collection.children.get(source.name) for scene in bpy.data.scenes):
        raise ValueError("The definition is shown directly in the scene. Close that edit view first")
    world = active.matrix_world.copy()
    if parent_frame is not None:
        world = parent_frame @ world
    if abs(world.to_3x3().determinant()) < 1e-10:
        raise ValueError("The active Link Group's transform cannot be inverted")
    if world_point is not None:
        point = Vector(world_point)
    elif pivot == 'CURSOR':
        point = cursor.location
    elif pivot == 'KEEP':
        point = world.translation
    else:
        low, high = _source_bounds(source)
        local = (low + high) * .5
        if pivot == 'BOTTOM':
            local.z = low.z
        point = world @ local
    if orientation == 'KEEP':
        delta = Matrix.Translation(world.inverted() @ point)
    else:
        rotation = cursor.matrix.to_quaternion() if orientation == 'CURSOR' else Matrix.Identity(3).to_quaternion()
        delta = world.inverted() @ Matrix.LocRotScale(point, rotation, world.to_scale())
    _compensate(active, delta)


def _compensate(active, delta):
    """Bake a placement-space delta into the definition and keep every instance visually still."""
    source = hosts.definition(active)
    _require_local(source)
    if any(source == coll.children.get(source.name) for coll in bpy.data.collections):
        raise ValueError("The definition is also shown directly in another collection. Close that edit view first")
    if any(source == scene.collection.children.get(source.name) for scene in bpy.data.scenes):
        raise ValueError("The definition is shown directly in the scene. Close that edit view first")
    try:
        delta.inverted()
    except ValueError as error:
        raise ValueError("This group's transform cannot be applied") from error
    offset = Matrix.Translation(source.instance_offset)
    source_delta = offset @ delta.inverted() @ offset.inverted()
    edits = {}
    members = set(_members(source))
    for obj in members:
        _check_exclusive_member(obj, source)
        if obj.parent and obj.parent not in members:
            raise ValueError(f'"{obj.name}" has a parent outside the definition')
        if any(child not in members for child in obj.children) or obj.constraints:
            raise ValueError(f'"{obj.name}" has a child outside the group or a constrained transform')
        for modifier in obj.modifiers:
            if getattr(modifier, "texture_coords", None) == 'GLOBAL':
                raise ValueError(f'"{obj.name} / {modifier.name}" evaluates in world space')
        if not obj.parent:
            edits[obj] = source_delta @ obj.matrix_basis
    users = bpy.data.user_map(subset={source}).get(source, set())
    for user in users:
        if not isinstance(user, bpy.types.Object) or hosts.definition(user) != source:
            raise ValueError(f'"{user.name}" references the definition directly, so its coordinates cannot be compensated')
    placements = []
    for obj in users:
        if hosts.definition(obj) == source:
            placements.append(obj)
            edits[obj] = obj.matrix_basis @ delta
    mirror_planes = [(obj, obj.matrix_world.copy()) for obj in placements]
    held_children = [(child, child.matrix_world.copy())
                     for obj in placements for child in obj.children if child not in edits]
    # An object used as a constraint target, camera focus or material mapping
    # reference would drive unrelated data when its coordinate system changes.
    incoming = bpy.data.user_map(subset=set(edits))
    children = {child for child, _world in held_children}
    for obj, refs in incoming.items():
        for ref in refs:
            if isinstance(ref, (bpy.types.Collection, bpy.types.Scene)) or ref in members or ref in children:
                continue
            raise ValueError(f'"{ref.name}" references the object coordinates of "{obj.name}"')
    external_targets = set(bpy.data.objects) - members
    for target, users in bpy.data.user_map(subset=external_targets).items():
        if members.intersection(users):
            raise ValueError(f'An object in the group depends on the coordinates of external target "{target.name}"')
    for obj, matrix in edits.items():
        _check_transform(obj, matrix)
    anchors = []
    try:
        with ExitStack() as rollback:
            for obj, matrix in edits.items():
                rollback.callback(setattr, obj, "matrix_basis", obj.matrix_basis.copy())
                obj.matrix_basis = matrix
            rollback.pop_all()
        if mirror_planes or held_children:
            bpy.context.view_layer.update()
        for child, world in held_children:
            child.matrix_world = world
        for placement, old_world in mirror_planes:
            anchor = _hold_local_modifiers(placement, old_world)
            if anchor is not None:
                anchors.append(anchor)
    except Exception:
        for anchor in anchors:
            if anchor.name in bpy.data.objects:
                bpy.data.objects.remove(anchor, do_unlink=True)
        raise


def _cleared_basis(basis, location, rotation, scale):
    loc, rot, sca = basis.decompose()
    if location:
        loc = Vector()
    if rotation:
        rot = Quaternion()
    if scale:
        sca = Vector((1.0, 1.0, 1.0))
    return Matrix.LocRotScale(loc, rot, sca)


def _needs_apply(basis, location, rotation, scale):
    cleared = _cleared_basis(basis, location, rotation, scale)
    return max(abs(basis[r][c] - cleared[r][c]) for r in range(4) for c in range(4)) > 1e-5


def _clear_delta(basis, location, rotation, scale):
    try:
        return basis.inverted() @ _cleared_basis(basis, location, rotation, scale)
    except ValueError as error:
        raise ValueError("This group's transform cannot be applied") from error


def apply_group_transforms(context, groups, location, rotation, scale):
    """Clear the chosen channels on group instances and keep every instance visually still."""
    sources = {hosts.definition(obj) for obj in groups if hosts.definition(obj)}
    released = []
    for host in list(editor.peers(context)):
        if hosts.definition(host) in sources:
            editor.close_peer(context, host)
            released.append(host)
    try:
        with editor.source_space(context):
            pending = list(dict.fromkeys(groups))
            while pending:
                host = pending.pop(0)
                source = hosts.definition(host)
                if source is None or not _needs_apply(host.matrix_basis, location, rotation, scale):
                    continue
                _compensate(host, _clear_delta(host.matrix_basis, location, rotation, scale))
                still = []
                for other in pending:
                    if hosts.definition(other) != source or not _needs_apply(other.matrix_basis, location, rotation, scale):
                        still.append(other)
                        continue
                    still.append(branches.independent_path([other]))
                pending = still
    finally:
        for host in released:
            try:
                editor.open_peer(context, host)
            except (ValueError, RuntimeError):
                pass


class GM_OT_transform_apply(bpy.types.Operator):
    bl_idname = "groupmanager.transform_apply"
    bl_label = "Apply"
    bl_description = "Apply location, rotation, or scale. Group contents stay where they are, including other instances"
    bl_options = {'REGISTER', 'UNDO'}

    location: BoolProperty(name="Location", default=False)
    rotation: BoolProperty(name="Rotation", default=False)
    scale: BoolProperty(name="Scale", default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected = list(context.selected_objects)
        groups = [obj for obj in selected if hosts.definition(obj)]
        plain = [obj for obj in selected if obj not in groups]
        try:
            if groups:
                apply_group_transforms(context, groups, self.location, self.rotation, self.scale)
            if plain:
                for obj in context.selected_objects:
                    obj.select_set(False)
                for obj in plain:
                    if obj.name in context.view_layer.objects:
                        obj.select_set(True)
                context.view_layer.objects.active = plain[0]
                result = bpy.ops.object.transform_apply(
                    location=self.location, rotation=self.rotation, scale=self.scale)
                if result != {'FINISHED'}:
                    return result
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        finally:
            for obj in selected:
                try:
                    if obj.name in context.view_layer.objects:
                        obj.select_set(True)
                except ReferenceError:
                    pass
        return {'FINISHED'}


_apply_menu_draw = None


class _ApplyLayout(menu_hooks.Layout):
    def operator(self, operator, *args, **kwargs):
        if operator == "object.transform_apply":
            operator = "groupmanager.transform_apply"
        return self._layout.operator(operator, *args, **kwargs)


def _group_apply_active(context):
    return context.mode == 'OBJECT' and any(hosts.definition(obj) for obj in context.selected_objects)


def register():
    global _apply_menu_draw
    _apply_menu_draw = menu_hooks.install(bpy.types.VIEW3D_MT_object_apply, _ApplyLayout, _group_apply_active)


def unregister():
    global _apply_menu_draw
    menu_hooks.restore(_apply_menu_draw)
    _apply_menu_draw = None


class GM_OT_origin_to_bottom(_ManagedGroupOperator, bpy.types.Operator):
    bl_idname = "groupmanager.origin_to_bottom"
    bl_label = "Origin to Bottom"
    bl_description = "Move the group origin to the bottom center of its lowest geometry. Link Groups stay where they are"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            active = context.active_object
            parent_frame = editor.frame(context)
            with editor.source_space(context):
                reframe(active, context.scene.cursor, 'BOTTOM', 'KEEP', parent_frame)
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_reframe(_ManagedGroupOperator, bpy.types.Operator):
    bl_idname = "groupmanager.reframe"
    bl_label = "Set Origin and Axes"
    bl_description = "Move the definition origin and compensate every Link Group that uses it"
    bl_options = {'REGISTER', 'UNDO'}

    pivot: EnumProperty(name="Origin", items=[
        ('CURSOR', "3D Cursor", ""), ('CENTER', "Bounds Center", ""),
        ('BOTTOM', "Bottom Center", ""), ('KEEP', "Keep Location", ""),
    ])
    orientation: EnumProperty(name="Axes", items=[
        ('KEEP', "Keep Axes", ""), ('WORLD', "World", ""), ('CURSOR', "3D Cursor", ""),
    ])

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            active = context.active_object
            parent_frame = editor.frame(context)
            with editor.source_space(context):
                reframe(active, context.scene.cursor, self.pivot, self.orientation, parent_frame)
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


_PLAIN_ORIGIN_TYPES = {'MESH', 'CURVE', 'SURFACE', 'FONT'}


def _group_targets(context):
    active = context.active_object
    ordered = [active] if active else []
    ordered.extend(obj for obj in context.selected_objects if obj != active)
    targets = []
    seen = set()
    for obj in ordered:
        source = hosts.definition(obj)
        if source and source not in seen and editor.in_scope(context, obj):
            seen.add(source)
            targets.append(obj)
    if not targets:
        route = editor.path(context)
        placement = route[-1] if route else None
        if placement and hosts.definition(placement):
            targets.append(placement)
    return targets


def _plain_targets(context):
    return [obj for obj in context.selected_objects
            if not hosts.definition(obj) and obj.type in _PLAIN_ORIGIN_TYPES]


def _reframe_groups(context, pivot, world_point=None, skip_active_definition=False):
    active = context.active_object
    active_source = hosts.definition(active) if active else None
    targets = _group_targets(context)
    if skip_active_definition and active_source:
        targets = [obj for obj in targets if hosts.definition(obj) != active_source]
    parent_frame = editor.frame(context)
    for obj in targets:
        with editor.source_space(context):
            reframe(obj, context.scene.cursor, pivot, 'KEEP', parent_frame, world_point)


def _use_selection(context, objects):
    previous = list(context.selected_objects)
    active = context.active_object
    for obj in previous:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]

    def restore():
        for obj in list(context.selected_objects):
            obj.select_set(False)
        for obj in previous:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if active and active.name in context.view_layer.objects:
            context.view_layer.objects.active = active

    return restore


def _origin_set(context, objects, origin_type):
    if not objects:
        return
    restore = _use_selection(context, objects)
    try:
        bpy.ops.object.origin_set(type=origin_type)
    finally:
        restore()


def _plain_bottom(context, objects):
    cursor = context.scene.cursor
    saved = cursor.matrix.copy()
    try:
        for obj in objects:
            local = [Vector(corner) for corner in obj.bound_box]
            point = Vector((
                (min(corner.x for corner in local) + max(corner.x for corner in local)) * 0.5,
                (min(corner.y for corner in local) + max(corner.y for corner in local)) * 0.5,
                min(corner.z for corner in local),
            ))
            cursor.location = obj.matrix_world @ point
            _origin_set(context, [obj], 'ORIGIN_CURSOR')
    finally:
        cursor.matrix = saved


def _plain_to_active(context, objects):
    active = context.active_object
    others = [obj for obj in objects if obj != active]
    if active is None or not others:
        return
    cursor = context.scene.cursor
    saved = cursor.matrix.copy()
    try:
        cursor.location = active.matrix_world.translation
        _origin_set(context, others, 'ORIGIN_CURSOR')
    finally:
        cursor.matrix = saved


def _edit_selection_target(context):
    """Label for the element that can receive the origin while a mesh is being edited."""
    mode = tuple(context.scene.tool_settings.mesh_select_mode)
    if mode == (True, False, False):
        return "Vert", 'VERTEXSEL'
    if mode == (False, True, False):
        return "Edge", 'EDGESEL'
    if mode == (False, False, True):
        return "Face", 'FACESEL'
    return None, None


def _selection_world(obj):
    import bmesh
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    select_mode = tuple(bpy.context.scene.tool_settings.mesh_select_mode)
    if select_mode[0]:
        chosen = [vert.co for vert in bm.verts if vert.select]
    elif select_mode[1]:
        chosen = [(edge.verts[0].co + edge.verts[1].co) * 0.5 for edge in bm.edges if edge.select]
    elif select_mode[2]:
        chosen = [face.calc_center_median_weighted() for face in bm.faces if face.select]
    else:
        chosen = []
    if not chosen:
        return None
    center = sum(chosen, Vector()) / len(chosen)
    return obj.matrix_world @ center


def _move_edit_origin(obj, world_point):
    """Move a mesh origin while editing and keep every vertex where it is."""
    import bmesh
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        raise ValueError("Edit a mesh to move its origin")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    if not bm.verts:
        raise ValueError("This mesh has no vertices")
    children = [(child, child.matrix_world.copy()) for child in obj.children]
    old = obj.matrix_world.copy()
    new = old.copy()
    new.translation = world_point
    delta = new.inverted() @ old
    obj.matrix_world = new
    bmesh.ops.transform(bm, verts=bm.verts, matrix=delta)
    bmesh.update_edit_mesh(obj.data)
    for child, world in children:
        child.matrix_world = world


def _flush_edit_mode(context):
    """Separate mesh edits from the origin change so Blender can undo them independently."""
    override = _view3d_override(context)
    if override is None:
        raise RuntimeError("Open a 3D Viewport to move the origin")
    with override:
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.ed.undo_push(message="Flush Edit Mode Changes")
        bpy.ops.object.mode_set(mode='EDIT')


class GM_OT_set_origin(bpy.types.Operator):
    bl_idname = "groupmanager.set_origin"
    bl_label = "Set Origin"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(items=(
        ('GEOMETRY', "to Geometry", "Move the origin to the geometry center"),
        ('CURSOR', "to Cursor", "Move the origin to the 3D cursor"),
        ('ACTIVE', "to Active", "Move other origins to the active object"),
        ('BOTTOM', "to Bottom", "Move the origin to the bottom center"),
    ))

    @classmethod
    def poll(cls, context):
        active = context.active_object
        if active is None:
            return False
        if context.mode == 'EDIT_MESH':
            return active.type == 'MESH'
        return context.mode == 'OBJECT'

    @classmethod
    def description(cls, context, properties):
        if context.mode == 'EDIT_MESH':
            kind, _icon = _edit_selection_target(context)
            if properties.mode == 'ACTIVE' and kind:
                return f"Move the origin to the selected {kind.lower()} and keep the mesh in place"
            if properties.mode == 'CURSOR':
                return "Move the origin to the 3D cursor and keep the mesh in place"
        if hosts.definition(context.active_object) or editor.path(context):
            return "Move the group origin and keep Link Groups, modifiers, and children in place"
        return "Move the object origin"

    def execute(self, context):
        try:
            if context.mode == 'EDIT_MESH':
                self._execute_edit(context)
                return {'FINISHED'}
            groups = _group_targets(context)
            plains = _plain_targets(context)
            if self.mode == 'ACTIVE':
                active = context.active_object
                active_source = hosts.definition(active)
                other_groups = [obj for obj in groups if hosts.definition(obj) != active_source]
                other_plains = [obj for obj in plains if obj != active]
                if not other_groups and not other_plains:
                    raise ValueError("Select another object to take the active origin")
                _reframe_groups(context, 'KEEP', active.matrix_world.translation.copy(), True)
                _plain_to_active(context, plains)
            elif self.mode == 'GEOMETRY':
                if groups:
                    _reframe_groups(context, 'CENTER')
                _origin_set(context, plains, 'ORIGIN_GEOMETRY')
            elif self.mode == 'CURSOR':
                if groups:
                    _reframe_groups(context, 'CURSOR')
                _origin_set(context, plains, 'ORIGIN_CURSOR')
            elif self.mode == 'BOTTOM':
                if groups:
                    _reframe_groups(context, 'BOTTOM')
                _plain_bottom(context, plains)
            if not groups and not plains and self.mode != 'ACTIVE':
                raise ValueError("Select a group or an object with geometry")
        except (ValueError, RuntimeError) as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}

    def _execute_edit(self, context):
        obj = context.active_object
        if self.mode == 'CURSOR':
            point = context.scene.cursor.location.copy()
        elif self.mode == 'ACTIVE':
            point = _selection_world(obj)
            if point is None:
                kind, _icon = _edit_selection_target(context)
                target = kind.lower() if kind else "element"
                raise ValueError(f"Select a {target} to place the origin")
        else:
            raise ValueError("Use to Cursor or the selected element while editing")
        _flush_edit_mode(context)
        _move_edit_origin(context.active_object, point)


def _view3d_override(context):
    if context.area and context.area.type == 'VIEW_3D':
        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if region:
            return context.temp_override(area=context.area, region=region)
    window = context.window
    screen = window.screen if window else None
    if screen:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next((item for item in area.regions if item.type == 'WINDOW'), None)
            if region:
                return context.temp_override(window=window, area=area, region=region)
    return None


class GM_OT_snap_cursor(bpy.types.Operator):
    bl_idname = "groupmanager.snap_cursor"
    bl_label = "Snap"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(items=(
        ('SELECTED', "to Selected", "Move the 3D cursor to the selection"),
        ('ORIGIN', "to Origin", "Move the 3D cursor to the world origin"),
        ('CURSOR', "to Cursor", "Move the selection to the 3D cursor"),
        ('OFFSET', "to Cursor, Offset", "Move the selection to the 3D cursor and keep its offset"),
    ))

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        override = _view3d_override(context)
        if override is None:
            self.report({'WARNING'}, "Open a 3D Viewport to use the cursor")
            return {'CANCELLED'}
        with override:
            if self.mode == 'SELECTED':
                result = bpy.ops.view3d.snap_cursor_to_selected()
            elif self.mode == 'ORIGIN':
                result = bpy.ops.view3d.snap_cursor_to_center()
            else:
                result = bpy.ops.view3d.snap_selected_to_cursor(use_offset=self.mode == 'OFFSET')
        if result != {'FINISHED'}:
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_origin_commands(layout, context):
    if context.mode == 'EDIT_MESH':
        layout.label(text="Object Origin")
        kind, icon = _edit_selection_target(context)
        row = layout.row(align=True)
        row.scale_y = 1.2
        if kind:
            to_element = row.operator(GM_OT_set_origin.bl_idname, text=f"to {kind}", icon=icon)
            to_element.mode = 'ACTIVE'
        to_cursor = row.operator(GM_OT_set_origin.bl_idname, text="to Cursor", icon='LAYER_ACTIVE')
        to_cursor.mode = 'CURSOR'
        return
    group_origin = bool(hosts.definition(context.active_object) or editor.path(context))
    layout.label(text="Group Origin" if group_origin else "Object Origin")
    geometry = layout.operator(GM_OT_set_origin.bl_idname, text="to Geometry", icon='MESH_DATA')
    geometry.mode = 'GEOMETRY'
    to_cursor = layout.operator(GM_OT_set_origin.bl_idname, text="to Cursor", icon='LAYER_ACTIVE')
    to_cursor.mode = 'CURSOR'
    to_active = layout.operator(GM_OT_set_origin.bl_idname, text="to Active", icon='TRANSFORM_ORIGINS')
    to_active.mode = 'ACTIVE'


class GROUPMANAGER_MT_cursor(bpy.types.Menu):
    bl_idname = "GROUPMANAGER_MT_cursor"
    bl_label = "Cursor and Origin"

    def draw(self, context):
        pie = self.layout.menu_pie()
        if context.mode == 'EDIT_MESH':
            kind, _icon = _edit_selection_target(context)
            selected_text = f"to {kind}" if kind else "to Selected"
        else:
            selected_text = "to Selected"
        selected = pie.operator(GM_OT_snap_cursor.bl_idname, text=selected_text, icon='PIVOT_CURSOR')
        selected.mode = 'SELECTED'
        cursor = pie.operator(GM_OT_snap_cursor.bl_idname, text="to Cursor", icon='RESTRICT_SELECT_OFF')
        cursor.mode = 'CURSOR'

        bottom = pie.column(align=True)
        bottom.ui_units_x = 9
        bottom.scale_y = 1.2
        draw_origin_commands(bottom, context)
        pie.separator()

        origin = pie.operator(GM_OT_snap_cursor.bl_idname, text="to Origin", icon='PIVOT_CURSOR')
        origin.mode = 'ORIGIN'
        offset = pie.operator(GM_OT_snap_cursor.bl_idname, text="to Cursor, Offset", icon='RESTRICT_SELECT_OFF')
        offset.mode = 'OFFSET'


CLASSES = (GM_OT_replace_material, GM_OT_origin_to_bottom, GM_OT_set_origin, GM_OT_snap_cursor,
           GM_OT_reframe, GM_OT_transform_apply, GROUPMANAGER_MT_cursor)
