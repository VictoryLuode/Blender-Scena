"""Copy and retarget local group definitions without changing sibling placements."""

from collections import Counter

import bpy
from mathutils import Matrix

from . import hosts


class BranchChanges:
    """Collect allocations and reversible pointer edits until the operation succeeds."""

    def __init__(self):
        self.created = []
        self.retargeted = []

    def own(self, item):
        self.created.append(item)
        return item

    def clone(self, source, name=None):
        return _copy_definition(source, name, self)

    def duplicate_group_data(self, obj, nested=False):
        source = hosts.definition(obj)
        copied, _members = _copy_definition(source, None, self, 'OBJECT')
        self.point(obj, copied)
        if nested:
            _localize_nested_groups(source, copied, self)

    def parent_path(self, path, extras=None):
        return _parent_path(path, self, extras=extras)

    def independent_path(self, path, name=None, scope='OBJECT_DATA'):
        if scope not in {'OBJECT', 'OBJECT_DATA', 'OBJECT_DATA_MATERIALS'}:
            raise ValueError("Unknown make-unique scope")
        remapped = _parent_path(path, self, ancestor_scope='OBJECT')
        target = remapped[-1]
        copied, _ = _copy_definition(hosts.definition(target), name, self, scope)
        self.point(target, copied)
        return remapped

    def point(self, obj, collection):
        self.retargeted.append((obj, hosts.definition(obj)))
        hosts.assign_definition(obj, collection)

    def __enter__(self):
        return self

    def __exit__(self, error, value, trace):
        if error is not None:
            for obj, previous in reversed(self.retargeted):
                hosts.assign_definition(obj, previous)
            if self.created:
                bpy.data.batch_remove(ids=self.created)
        return False


def _editable(obj):
    if obj.library or obj.override_library:
        raise ValueError(f'"{obj.name}" comes from a linked library or override. Make a local version first')


def _check_sources(collection):
    from . import editor
    if editor.content(collection) != collection:
        raise ValueError(f'"{collection.name}" is already being edited. Finish that edit first')
    objects = set(collection.all_objects)
    collections = {collection, *collection.children_recursive}
    for obj in objects:
        if obj.type == 'ARMATURE' or obj.pose:
            raise ValueError(f'"{obj.name}" contains an armature. Bone references cannot be guaranteed after making it unique')
        for owner in (obj, obj.data, getattr(obj.data, "shape_keys", None)):
            anim = getattr(owner, "animation_data", None)
            if anim and anim.drivers:
                raise ValueError(f'"{obj.name}" has drivers. Resolve the driver targets first')
        for mod in obj.modifiers:
            tree = getattr(mod, "node_group", None)
            if tree and _tree_uses_members(tree, objects, collections, set()):
                raise ValueError(f'The node tree on "{obj.name} / {mod.name}" references data inside the group. Use a modifier input instead')
    return objects


def _tree_uses_members(tree, objects, collections, visited):
    if tree in visited:
        return False
    visited.add(tree)
    for node in tree.nodes:
        for socket in node.inputs:
            if socket.type in {'OBJECT', 'COLLECTION'}:
                value = getattr(socket, 'default_value', None)
                if value in objects or value in collections:
                    return True
        child = getattr(node, 'node_tree', None)
        if child and _tree_uses_members(child, objects, collections, visited):
            return True
    return False


def _remap_fields(holder, remap):
    """Remap writable RNA ID pointers, including modifier or constraint subtarget lists."""
    for prop in holder.bl_rna.properties:
        key = prop.identifier
        if prop.type == 'POINTER' and not prop.is_readonly:
            current = getattr(holder, key)
            if isinstance(current, bpy.types.ID) and current in remap:
                setattr(holder, key, remap[current])
        elif prop.type == 'COLLECTION' and key in {'targets', 'projectors'}:
            for entry in getattr(holder, key):
                _remap_fields(entry, remap)


def _remap_custom(holder, remap):
    try:
        keys = list(holder.keys())
    except (AttributeError, TypeError):
        return
    for key in keys:
        value = holder[key]
        if isinstance(value, bpy.types.ID):
            if value in remap:
                holder[key] = remap[value]
        elif hasattr(value, 'keys'):
            _remap_custom(value, remap)


def _copy_materials(objects, data_blocks, changes):
    material_map = {}

    def material_copy(material):
        if material not in material_map:
            material_map[material] = changes.own(material.copy())
        return material_map[material]

    for data in data_blocks:
        slots = getattr(data, "materials", ())
        for index, material in enumerate(slots):
            if material:
                slots[index] = material_copy(material)
    for obj in objects:
        for slot in obj.material_slots:
            if slot.link == 'OBJECT' and slot.material:
                slot.material = material_copy(slot.material)
    return material_map


def _copy_definition(source, name, changes, scope='OBJECT_DATA'):
    objects = _check_sources(source)
    collection_map = {}
    for original in [source, *source.children_recursive]:
        if original in collection_map:
            continue
        copy = changes.own(original.copy())
        collection_map[original] = copy
        for obj in list(copy.objects):
            copy.objects.unlink(obj)
        for child in list(copy.children):
            copy.children.unlink(child)
    root = collection_map[source]
    if name:
        root.name = name
    hosts.mark_definition(root)

    object_map = {}
    data_map = {}
    for original in sorted(objects, key=lambda obj: obj.name):
        copy = changes.own(original.copy())
        object_map[original] = copy
        data = original.data
        if scope != 'OBJECT' and data and original.type != 'EMPTY':
            if data not in data_map:
                data_map[data] = changes.own(data.copy())
            copy.data = data_map[data]
    for original, copied in collection_map.items():
        for child in original.children:
            copied.children.link(collection_map[child])
        for obj in original.objects:
            copied.objects.link(object_map[obj])

    remap = {**collection_map, **object_map, **data_map}
    if scope == 'OBJECT_DATA_MATERIALS':
        remap.update(_copy_materials(object_map.values(), data_map.values(), changes))
    for original, copy in object_map.items():
        if original.parent in object_map:
            copy.parent = object_map[original.parent]
            copy.matrix_parent_inverse = original.matrix_parent_inverse
        for owner in (*copy.constraints, *copy.modifiers):
            _remap_fields(owner, remap)
            _remap_custom(owner, remap)
            # Blender 5.3 exposes geometry node inputs as typed socket properties.
            inputs = getattr(getattr(owner, "properties", None), "inputs", None)
            if inputs is not None:
                for socket in inputs.bl_rna.properties:
                    if socket.type == 'POINTER' and socket.identifier != 'rna_type':
                        _remap_fields(getattr(inputs, socket.identifier), remap)
        _remap_custom(copy, remap)
    for data in data_map.values():
        _remap_fields(data, remap)
        if isinstance(data, bpy.types.Camera):
            _remap_fields(data.dof, remap)
        _remap_custom(data, remap)
    return root, object_map


def clone_definition(collection, name=None):
    """Create an independently editable collection, retaining shared nested definitions."""
    with BranchChanges() as changes:
        return _copy_definition(collection, name, changes)


def _check_root(obj):
    _editable(obj)
    if not hosts.definition(obj):
        raise ValueError(f'"{obj.name}" is not a Link Group')
    scenes = [scene for scene in obj.users_scene if not scene.get('_gm_edit')]
    if len(scenes) > 1:
        raise ValueError(f'Link Group "{obj.name}" is linked into more than one scene. Choose which scene owns it first')


def _shared(collection):
    from . import editor
    refs = 0
    for obj in bpy.data.objects:
        if not editor.is_helper(obj) and hosts.definition(obj) == collection:
            refs += 1
            if refs > 1:
                return True
    return any(not scene.get('_gm_edit') and collection in scene.collection.children_recursive
               for scene in bpy.data.scenes)


def _parent_path(path, changes, ancestor_scope='OBJECT_DATA', extras=None):
    path = list(path)
    if not path:
        raise ValueError("Select one Link Group")
    _check_root(path[0])
    definitions = set()
    for index, host in enumerate(path):
        source = hosts.definition(host)
        if not source or source in definitions:
            raise ValueError("The edit path is invalid or cyclic. Return to the scene and select again")
        definitions.add(source)
        if index + 1 < len(path) and path[index + 1] not in set(source.all_objects):
            raise ValueError("The selected subgroup does not belong to the current parent")
    for index in range(len(path) - 1):
        host = path[index]
        source = hosts.definition(host)
        if _shared(source) or source.library:
            _editable(host)
            copied, members = _copy_definition(source, None, changes, ancestor_scope)
            changes.point(host, copied)
            path[index + 1] = members[path[index + 1]]
            if extras:
                contained = [obj for obj in extras if obj in members]
                if contained and len(contained) != len(extras):
                    raise ValueError("Select Link Groups inside the group being edited")
                if len(contained) == len(extras):
                    extras[:] = [members[obj] for obj in extras]
    _editable(path[-1])
    return path


def localize_parent_path(path):
    """Copy just the shared ancestors, preserving the target placement's definition."""
    with BranchChanges() as changes:
        return _parent_path(path, changes)


def independent_path(path, name=None, scope='OBJECT_DATA'):
    with BranchChanges() as changes:
        return changes.independent_path(path, name, scope)[-1]


def _localize_nested_groups(original, copied, changes):
    """Point nested groups in a copied collection at new collections, not the originals."""
    cloned = {original: copied}

    def walk(collection):
        for obj in list(collection.all_objects):
            nested = hosts.definition(obj)
            if not nested or nested == collection:
                continue
            target = cloned.get(nested)
            if target is None:
                target, _members = _copy_definition(nested, None, changes, 'OBJECT')
                cloned[nested] = target
                walk(target)
            if hosts.definition(obj) != target:
                changes.point(obj, target)

    walk(copied)


def duplicate_placements(objects, linked, nested=False):
    """Shift+D gives each group its own collection. Alt+D instances the same group.

    Alt+Shift+D also gives every nested group its own collection.
    """
    from .layout_tools import EXCLUDED, INDEX, SETTINGS, SLOTS

    selected = list(dict.fromkeys(objects))
    if not selected:
        raise ValueError("Select a group")
    copies = []
    with BranchChanges() as changes:
        for obj in selected:
            _check_root(obj)
            if not obj.users_collection:
                raise ValueError("The Link Group has no collection")
            duplicate = obj.copy()
            try:
                for key in (EXCLUDED, INDEX, SETTINGS, SLOTS):
                    if key in duplicate:
                        del duplicate[key]
                for collection in obj.users_collection:
                    collection.objects.link(duplicate)
                duplicate.matrix_world = obj.matrix_world.copy()
            except Exception:
                hosts._remove_host(duplicate)
                raise
            changes.own(duplicate)
            if not linked or nested:
                changes.duplicate_group_data(duplicate, nested)
            copies.append(duplicate)
    return copies


def fork_selected(objects, name=None):
    selected = list(dict.fromkeys(objects))
    if not selected:
        raise ValueError("Select Link Groups of the same group")
    source = hosts.definition(selected[0])
    for obj in selected:
        _check_root(obj)
        if hosts.definition(obj) != source:
            raise ValueError("The selected Link Groups use different definitions. Select matching instances first")
    with BranchChanges() as changes:
        copied, _ = _copy_definition(source, name, changes, 'OBJECT')
        for obj in selected:
            changes.point(obj, copied)
        return copied


def _extra_modifiers(host):
    source_modifier = hosts._source_modifier(host)
    return [modifier for modifier in host.modifiers if modifier != source_modifier]


def _mirror_transform(matrix, modifier):
    pivot = modifier.mirror_object.matrix_world.copy() if modifier.mirror_object else matrix.copy()
    scale = [1.0, 1.0, 1.0]
    used = False
    for index in range(3):
        if modifier.use_axis[index]:
            scale[index] = -1.0
            used = True
    if not used:
        return matrix
    reflection = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    return pivot @ reflection @ pivot.inverted() @ matrix


def _linked_mirror_group(host, mirrors):
    """Place another linked instance where the mirror modifier would show the opposite side."""
    from .layout_tools import EXCLUDED, INDEX, SETTINGS, SLOTS

    matrix = host.matrix_world.copy()
    for modifier in mirrors:
        matrix = _mirror_transform(matrix, modifier)
    duplicate = host.copy()
    try:
        for key in (EXCLUDED, INDEX, SETTINGS, SLOTS):
            if key in duplicate:
                del duplicate[key]
        for collection in host.users_collection:
            collection.objects.link(duplicate)
        duplicate.matrix_world = matrix
        for modifier in list(duplicate.modifiers):
            if modifier.type == 'MIRROR':
                duplicate.modifiers.remove(modifier)
    except Exception:
        hosts._remove_host(duplicate)
        raise
    return duplicate


def split_mirror(host):
    """Replace a mirror modifier with a second linked instance. Other modifiers stay on both."""
    _check_root(host)
    from . import instance_mirror
    if instance_mirror.adapter(host):
        raise ValueError("Turn off Instance Mirror before splitting the Mirror into separate objects")
    mirrors = [modifier for modifier in host.modifiers if modifier.type == 'MIRROR']
    if not mirrors:
        raise ValueError("This Link Group has no mirror modifier")
    if not host.users_collection:
        raise ValueError("The Link Group has no collection")
    duplicate = _linked_mirror_group(host, mirrors)
    for modifier in list(host.modifiers):
        if modifier.type == 'MIRROR':
            host.modifiers.remove(modifier)
    return duplicate


def _modifier_snapshot(modifier):
    values = {}
    for prop in modifier.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {"rna_type", "name", "type"}:
            continue
        if prop.type not in {'POINTER', 'FLOAT', 'INT', 'BOOLEAN', 'STRING', 'ENUM'}:
            continue
        try:
            value = getattr(modifier, prop.identifier)
        except AttributeError:
            continue
        if prop.type == 'POINTER':
            values[prop.identifier] = value
        elif getattr(prop, "is_array", False):
            values[prop.identifier] = value[:]
        else:
            values[prop.identifier] = value
    return modifier.name, modifier.type, values


def _restore_modifier(obj, snapshot):
    name, modifier_type, values = snapshot
    modifier = obj.modifiers.new(name, modifier_type)
    for key, value in values.items():
        try:
            setattr(modifier, key, value)
        except (AttributeError, TypeError):
            pass
    return modifier


def _apply_snapshots(objects, snapshots, apply=True):
    if not snapshots:
        return
    for obj in objects:
        if getattr(obj, "type", None) not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'LATTICE'}:
            continue
        created = []
        for snapshot in snapshots:
            try:
                created.append(_restore_modifier(obj, snapshot))
            except RuntimeError:
                continue
        if not apply or obj.name not in bpy.context.view_layer.objects:
            continue
        for modifier in created:
            try:
                with bpy.context.temp_override(object=obj, active_object=obj,
                                                selected_objects=[obj], selected_editable_objects=[obj]):
                    bpy.ops.object.modifier_apply(modifier=modifier.name)
            except RuntimeError:
                pass


def _settle_extra_modifiers(placements, modifier_mode):
    """Drop extra modifiers before unpacking. Apply keeps their effect; Delete discards it."""
    snapshots = {}
    restore = {}
    mirrors = []
    for host in placements:
        extras = _extra_modifiers(host)
        restore[host] = [_modifier_snapshot(modifier) for modifier in extras]
        if modifier_mode == 'APPLY':
            snapshots[host] = [_modifier_snapshot(modifier) for modifier in extras if modifier.type != 'MIRROR']
        elif modifier_mode == 'KEEP':
            snapshots[host] = [_modifier_snapshot(modifier) for modifier in extras]
        else:
            snapshots[host] = []
        for modifier in list(extras):
            host.modifiers.remove(modifier)
    return snapshots, mirrors, restore


def _check_unpack(host, source, users=None):
    if host.children or host.constraints or host.animation_data:
        raise ValueError("The Link Group has children, constraints, or animation. Ungrouping would change those relationships")
    owners = set(host.users_collection)
    if not owners or any(not col.is_editable or col.override_library for col in owners):
        raise ValueError(f'The collection of Link Group "{host.name}" is unlinked or read-only')
    if users is None:
        users = bpy.data.user_map(subset=[host, source])
    foreign = users.get(host, set()) - owners
    foreign = [user for user in foreign if not isinstance(user, bpy.types.Scene)]
    if foreign:
        raise ValueError(f'The Link Group is still used by "{foreign[0].name}". Resolve that reference before ungrouping')
    members = set(source.all_objects)
    member_ids = members | {obj.data for obj in members if obj.data}
    if member_ids.intersection(users.get(source, set())):
        raise ValueError("A member references the whole source collection. Resolve that dependency before ungrouping")
    for obj in members:
        if obj.parent and obj.parent not in members:
            raise ValueError(f'"{obj.name}" has a parent outside the group, so the hierarchy cannot be kept after ungrouping')
    return owners


def _unpack_placements(placements, selected, modifier_mode='DELETE'):
    source = hosts.definition(selected)
    if not source:
        raise ValueError("Select a Link Group")
    stand_ins = []
    mirror_copies = []
    restore = {}
    try:
        for host in placements:
            _editable(host)
        snapshots, mirror_copies, restore = _settle_extra_modifiers(placements, modifier_mode)
        for host in placements:
            stand_in = hosts.stand_in_for_dependents(host)
            if stand_in:
                stand_ins.append((host, stand_in))
        users = bpy.data.user_map(subset=[source, *placements])
        owners = {}
        for host in placements:
            owners[host] = _check_unpack(host, source, users)
        from . import editor
        if any(member in owners for route in editor.active_paths() for member in route):
            raise ValueError("A window is editing a Link Group that would be removed. Exit that edit path first")
        # All host deletions are committed together, after every copy, transform and
        # destination link has succeeded. On failure the linked copies are removed.
        discard = set(placements)
        carriers = Counter(host.data for host in placements if host.type == 'MESH')
        for mesh, removed_users in carriers.items():
            if not mesh.library and not mesh.vertices and mesh.users == removed_users:
                discard.add(mesh)
        with BranchChanges() as changes:
            expanded = []
            unpacked = []
            hidden_members = {}
            unpacked_by_host = {}
            for host in placements:
                copied, members = _copy_definition(source, None, changes)
                _position_unpacked(host, source, members)
                for scene in host.users_scene:
                    for layer in scene.view_layers:
                        if host.name in layer.objects and host.hide_get(view_layer=layer):
                            hidden_members.setdefault(layer, []).extend(members.values())
                expanded.append((host, copied))
                unpacked_by_host[host] = list(members.values())
                discard.add(copied)
                unpacked.extend(members.values())
            for host, copied in expanded:
                for destination in owners[host]:
                    for obj in copied.objects:
                        destination.objects.link(obj)
                    for child in copied.children:
                        destination.children.link(child)
            for layer, objects in hidden_members.items():
                layer.update()
                for obj in objects:
                    if obj.name in layer.objects:
                        obj.hide_set(True, view_layer=layer)
            if modifier_mode in {'APPLY', 'KEEP'}:
                for host, objects in unpacked_by_host.items():
                    _apply_snapshots(objects, snapshots.get(host, ()), apply=modifier_mode == 'APPLY')
            bpy.data.batch_remove(ids=discard)
            return unpacked
    except Exception:
        for host, stand_in in reversed(stand_ins):
            hosts._discard_stand_in(stand_in, host)
        for obj in mirror_copies:
            if obj.name in bpy.data.objects:
                hosts._remove_host(obj)
        for host, snapshots_to_restore in restore.items():
            if host.name not in bpy.data.objects:
                continue
            for snapshot in snapshots_to_restore:
                try:
                    _restore_modifier(host, snapshot)
                except RuntimeError:
                    pass
        raise


def _position_unpacked(host, source, members):
    placement = host.matrix_basis @ Matrix.Translation(-source.instance_offset)
    for original, obj in members.items():
        if host.hide_viewport:
            obj.hide_viewport = True
        if host.hide_render:
            obj.hide_render = True
        if original.parent:
            continue
        if original.constraints or original.animation_data:
            raise ValueError(f'"{original.name}" has a root constraint or animation that needs the Link Group hierarchy')
        if host.parent:
            obj.parent = host.parent
            obj.parent_type = host.parent_type
            obj.parent_bone = host.parent_bone
            obj.matrix_parent_inverse = host.matrix_parent_inverse @ placement
        else:
            world = placement @ original.matrix_basis
            decomposed = Matrix.LocRotScale(*world.decompose())
            if any(abs(world[i][j] - decomposed[i][j]) > 1e-5 for i in range(4) for j in range(4)):
                raise ValueError("This Link Group's non-uniform scale and member rotation create shear. Keep the Link Group hierarchy")
            obj.matrix_world = world


def _ensure_acyclic(source, stack=None):
    stack = set() if stack is None else stack
    if source in stack:
        raise ValueError(f'Definition "{source.name}" contains a cycle')
    for obj in source.all_objects:
        child = hosts.definition(obj)
        if child:
            _ensure_acyclic(child, stack | {source})


def _dissolve_tree(host, modifier_mode, stack):
    source = hosts.definition(host)
    if not source:
        return [host]
    if source in stack:
        raise ValueError(f'Definition "{source.name}" contains a cycle')
    expanded = []
    for obj in dissolve(host, modifier_mode):
        if hosts.definition(obj):
            expanded.extend(_dissolve_tree(obj, modifier_mode, stack | {source}))
        else:
            expanded.append(obj)
    return expanded


def dissolve(host, modifier_mode='DELETE'):
    """Unpack one level, duplicating direct members while retaining subgroups."""
    _check_root(host)
    return _unpack_placements([host], host, modifier_mode)


def dissolve_fully(host, modifier_mode='DELETE'):
    """Unpack this placement and every nested group inside it."""
    source = hosts.definition(host)
    if source:
        _ensure_acyclic(source)
    return _dissolve_tree(host, modifier_mode, set())


def dissolve_many(placements, selected, modifier_mode='DELETE'):
    """Unpack one level of each selected placement that shares one definition."""
    hosts_to_unpack = list(dict.fromkeys(placements))
    if not hosts_to_unpack:
        raise ValueError("Select a Link Group")
    for host in hosts_to_unpack:
        _check_root(host)
    source = hosts.definition(selected)
    if any(hosts.definition(host) != source for host in hosts_to_unpack):
        raise ValueError("The selected Link Groups use different definitions. Select matching instances first")
    return _unpack_placements(hosts_to_unpack, selected, modifier_mode)


def dissolve_fully_many(placements, selected, modifier_mode='DELETE'):
    """Unpack the selected placements and every nested group inside them."""
    source = hosts.definition(selected)
    if source:
        _ensure_acyclic(source)
    expanded = []
    for obj in dissolve_many(placements, selected, modifier_mode):
        if hosts.definition(obj):
            expanded.extend(_dissolve_tree(obj, modifier_mode, {source}))
        else:
            expanded.append(obj)
    return expanded


def delete_placements(placements):
    """Remove placements without releasing their members. The source definition stays."""
    from . import editor

    hosts_to_delete = list(dict.fromkeys(placements))
    if not hosts_to_delete:
        raise ValueError("Select a Link Group")
    for host in hosts_to_delete:
        _check_root(host)
    if any(host in route for route in editor.active_paths() for host in hosts_to_delete):
        raise ValueError("A window is editing a Link Group that would be removed. Exit that edit path first")
    for host in hosts_to_delete:
        hosts._remove_host(host)
