"""Expose real collection members in a placement's coordinates, inside its own scene."""

import bpy
from mathutils import Matrix


_WORK = "_gm_work"
_SHELL = "_gm_shell"
_ANCHOR = "_gm_anchor"
_INVERSE = "_gm_inverse"
_HOST = "_gm_open_host"
_FRAME = "_gm_edit_frame"
_HIDDEN = "_gm_was_hidden"
_HELPER = "_gm_session_helper"
_INITIAL = "_gm_initial_links"
_ROOTS = "_gm_initial_roots"
_KEYS = (_WORK, _SHELL, _ANCHOR, _INVERSE, _HOST, _FRAME, _HIDDEN, _INITIAL, _ROOTS)
_MIRROR = "_gm_mirror"
_MIRROR_OF = "_gm_mirror_of"
_PEER_HOST = "_gm_mirror_host"


def is_open(source):
    return source is not None and _WORK in source


def open_host(source):
    """Placement that holds the real members while this definition is open."""
    return source.get(_HOST) if source is not None else None


def content(source):
    if source is None:
        return None
    return source.get(_WORK) or source


def content_frame(source):
    values = source.get(_FRAME) if source is not None else None
    return Matrix([values[i:i+4] for i in range(0, 16, 4)]) if values else Matrix.Identity(4)


def is_helper(obj):
    return bool(obj and obj.get(_HELPER))


def displayed_members(source, host):
    """Direct edit members at this particular open instance, in world space."""
    if host == open_host(source):
        objects = content(source).all_objects
    else:
        objects = (obj for obj in source[_SHELL].objects if obj.get(_PEER_HOST) == host)
    return (obj for obj in objects if not is_helper(obj))


def _move_contents(source, destination):
    for child in list(source.children):
        if child.name not in destination.children:
            destination.children.link(child)
        source.children.unlink(child)
    for obj in list(source.objects):
        if obj.name not in destination.objects:
            destination.objects.link(obj)
        source.objects.unlink(obj)


def _preflight(source, matrix):
    if source.library or source.override_library:
        raise ValueError("Make a local version of the linked definition first")
    if is_open(source):
        raise ValueError("This group is already open on another edit path")
    try:
        inverse = matrix.inverted()
    except ValueError as exc:
        raise ValueError("The Link Group has zero scale and cannot be edited here") from exc
    members = set(source.all_objects)
    allowed = {source, *source.children_recursive}
    for collection in allowed:
        if not collection.is_editable or collection.override_library:
            raise ValueError(f'Child collection "{collection.name}" is read-only and cannot be edited in place')
    if len(allowed) > 1:
        users = bpy.data.user_map(subset=allowed - {source})
        if any(user not in allowed and not isinstance(user, bpy.types.Text)
               for owners in users.values() for user in owners):
            raise ValueError("A child collection of this definition is also used at another level. Make the group structure unique first")
    if members:
        member_ids = members | {obj.data for obj in members if obj.data}
        for member, users in bpy.data.user_map(subset=members).items():
            for user in users:
                if user in member_ids or isinstance(user, (bpy.types.Scene, bpy.types.Collection, bpy.types.Text)):
                    continue
                raise ValueError(f'Outside data "{user.name}" references member "{member.name}", so its coordinates cannot move')
        external = set(bpy.data.objects) - members
        if external:
            for target, users in bpy.data.user_map(subset=external).items():
                inputs = member_ids.intersection(users)
                if inputs:
                    owner = next(iter(inputs))
                    raise ValueError(f'"{owner.name}" depends on the coordinates of external object "{target.name}"')
    for obj in members:
        if obj.library or obj.override_library:
            raise ValueError(f'Member "{obj.name}" is read-only. Make a local version of the definition')
        if obj.parent and obj.parent not in members:
            raise ValueError(f'Member "{obj.name}" depends on a parent outside the group and cannot stay in place')
        if any(col not in allowed for col in obj.users_collection):
            raise ValueError(f'Member "{obj.name}" also belongs to an outside collection and cannot move to this Link Group')
        if any(child not in members for child in obj.children):
            raise ValueError(f'"{obj.name}" still has children outside the group, so only the members cannot be moved')
        if not obj.parent and any(not c.mute and c.influence for c in obj.constraints):
            raise ValueError(f'Root member "{obj.name}" has a spatial constraint, so in-place coordinates cannot be guaranteed')
    return inverse


def enter(scene, layer, host, source, matrix):
    if layer.objects.get(host.name) != host:
        raise ValueError(f'"{host.name}" is not in the current view layer and cannot be edited in place')
    inverse = _preflight(source, matrix)
    previous_layer = layer.active_layer_collection
    hidden = host.hide_get(view_layer=layer)
    roots = {obj: obj.matrix_basis.copy() for obj in source.all_objects if not obj.parent}
    allocated = []
    work = None
    try:
        shell = bpy.data.collections.new("Group Edit")
        allocated.append(shell)
        work = bpy.data.collections.new(source.name + " Group Content")
        allocated.append(work)
        # Other placements read this collection through a full matrix. A parent inverse
        # stores that matrix without turning the group's objects into children.
        anchor = bpy.data.objects.new("Group Edit Frame", None)
        allocated.append(anchor)
        compensation = bpy.data.objects.new("Group Restore Frame", None)
        allocated.append(compensation)
        for item in (shell, work, anchor, compensation):
            item[_HELPER] = True
        shell.hide_render = True
        anchor.hide_viewport = True
        anchor.hide_select = True
        anchor.hide_render = True
        anchor.empty_display_size = 0.001
        shell.objects.link(anchor)
        compensation.hide_select = True
        compensation.instance_type = 'COLLECTION'
        compensation.instance_collection = work
        compensation.parent = anchor
        compensation.matrix_parent_inverse = inverse

        source[_SHELL] = shell
        source[_ANCHOR] = anchor
        source[_INVERSE] = compensation
        source[_HOST] = host
        source[_FRAME] = [value for row in matrix for value in row]
        source[_WORK] = work
        source[_HIDDEN] = hidden
        source[_INITIAL] = {str(i): entry for i, entry in enumerate([*source.objects, *source.children])}
        source[_ROOTS] = {str(i): obj for i, obj in enumerate(roots)}

        _move_contents(source, work)
        shell.children.link(work)
        for obj, basis in roots.items():
            world = matrix @ basis
            if _needs_affine_parent(world):
                # Rotated children of non-uniformly scaled placements can have shear.
                # Keep the full placement matrix in the parent inverse during the edit.
                obj.parent = anchor
                obj.matrix_parent_inverse = matrix
            else:
                obj.matrix_world = world
        source.objects.link(compensation)
        # Publish the edit hierarchy once its transforms and compensation are ready.
        scene.collection.children.link(shell)
        for scene_layer in scene.view_layers:
            item = scene_layer.layer_collection.children.get(shell.name)
            if item:
                item.exclude = scene_layer != layer
        layer.update()
        host.hide_set(True, view_layer=layer)
        layer.active_layer_collection = layer.layer_collection.children[shell.name].children[work.name]
        layer.update()
    except Exception:
        for obj, basis in roots.items():
            if obj.parent is not None:
                obj.parent = None
                obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = basis
        if work:
            _move_contents(work, source)
        for key in _KEYS:
            if key in source:
                del source[key]
        if allocated:
            bpy.data.batch_remove(ids=allocated)
        layer.update()
        host.hide_set(hidden, view_layer=layer)
        layer.active_layer_collection = previous_layer
        raise


def open_linked_peer(layer, source, host):
    """Expose editable members only at a placement explicitly added to the edit."""
    from . import hosts
    if layer.objects.get(host.name) != host or not host.visible_get(view_layer=layer):
        raise ValueError(f'"{host.name}" must be visible in the current view layer')
    shell = source[_SHELL]
    inverse = content_frame(source).inverted()
    placed = host.evaluated_get(layer.depsgraph).matrix_world @ Matrix.Translation(-source.instance_offset)
    if abs(placed.to_3x3().determinant()) < 1e-10:
        raise ValueError("This Link Group has zero scale and cannot be edited here")
    allocated = []
    try:
        anchor = bpy.data.objects.new("Group Mirror Frame", None)
        allocated.append(anchor)
        anchor[_HELPER] = True
        anchor[_MIRROR] = True
        anchor[_PEER_HOST] = host
        anchor.hide_viewport = True
        anchor.hide_select = True
        anchor.hide_render = True
        anchor.empty_display_size = 0.001
        shell.objects.link(anchor)
        anchor.matrix_world = placed
        for obj in list(content(source).all_objects):
            if is_helper(obj) or obj.get(_MIRROR):
                continue
            copy = obj.copy()
            allocated.append(copy)
            copy[_MIRROR] = True
            copy[_MIRROR_OF] = obj
            copy[_PEER_HOST] = host
            copy.hide_render = True
            if hosts.definition(obj):
                copy.hide_select = False
            shell.objects.link(copy)
            copy.parent = anchor
            copy.matrix_parent_inverse = Matrix.Identity(4)
            copy.matrix_world = placed @ inverse @ obj.matrix_world
        host.hide_set(True, view_layer=layer)
        layer.update()
    except Exception:
        if allocated:
            bpy.data.batch_remove(ids=allocated)
        host.hide_set(False, view_layer=layer)
        raise


_syncing_mirrors = False
_mirror_world = {}


def _close(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(4) for j in range(4)) < 1e-5


def sync_linked_mirrors():
    """Moving a member at any linked placement moves that same member at the others."""
    global _syncing_mirrors
    if _syncing_mirrors:
        return
    _syncing_mirrors = True
    try:
        for source in bpy.data.collections:
            if not is_open(source):
                continue
            shell = source.get(_SHELL)
            if shell is None:
                continue
            try:
                matrix = content_frame(source)
                inverse = matrix.inverted()
            except (ReferenceError, ValueError):
                continue
            mirrors = {}
            anchors = []
            for obj in shell.objects:
                original = obj.get(_MIRROR_OF)
                if original is not None:
                    mirrors.setdefault(original, []).append(obj)
                elif obj.get(_MIRROR) and is_helper(obj):
                    anchors.append(obj)
            anchor_of = {}
            for copy, originals in ((copy, original) for original, copies in mirrors.items() for copy in copies):
                anchor_of[copy] = copy.parent
            for original, copies in mirrors.items():
                try:
                    real_world = original.matrix_world.copy()
                except ReferenceError:
                    continue
                changed = None
                for copy in copies:
                    anchor = anchor_of.get(copy)
                    if anchor is None:
                        continue
                    try:
                        current = copy.matrix_world.copy()
                    except ReferenceError:
                        continue
                    previous = _mirror_world.get(copy.as_pointer())
                    if previous is not None and not _close(current, previous):
                        if _mirror_world.get(original.as_pointer()) and _close(real_world, _mirror_world[original.as_pointer()]):
                            changed = matrix @ anchor.matrix_world.inverted() @ current
                            break
                previous_real = _mirror_world.get(original.as_pointer())
                real_moved = previous_real is None or not _close(real_world, previous_real)
                if changed is not None and not real_moved:
                    original.matrix_world = changed
                    real_world = original.matrix_world.copy()
                elif changed is not None and real_moved:
                    changed = None
                for copy in copies:
                    anchor = anchor_of.get(copy)
                    if anchor is None:
                        continue
                    target = anchor.matrix_world @ inverse @ real_world
                    if not _close(copy.matrix_world, target):
                        copy.matrix_world = target
                    _mirror_world[copy.as_pointer()] = copy.matrix_world.copy()
                _mirror_world[original.as_pointer()] = real_world
            for anchor in anchors:
                _mirror_world.setdefault(anchor.as_pointer(), anchor.matrix_world.copy())
    finally:
        _syncing_mirrors = False


def close_linked_peers(source, layer, host=None):
    """Remove this edit shell's requested mirrors and reveal their placements."""
    shell = source.get(_SHELL)
    if shell is None:
        return
    mirrors = []
    placements = set()
    removed_originals = set()
    remaining_originals = set()
    for obj in shell.objects:
        if not obj.get(_MIRROR):
            continue
        placement = obj.get(_PEER_HOST)
        original = obj.get(_MIRROR_OF)
        if host is not None and placement != host:
            if original is not None:
                remaining_originals.add(original)
            continue
        if placement is not None:
            placements.add(placement)
        _mirror_world.pop(obj.as_pointer(), None)
        if original is not None:
            removed_originals.add(original)
        mirrors.append(obj)
    for original in removed_originals - remaining_originals:
        _mirror_world.pop(original.as_pointer(), None)
    if mirrors:
        bpy.data.batch_remove(ids=mirrors)
    for placement in placements:
        if layer.objects.get(placement.name) == placement:
            placement.hide_set(False, view_layer=layer)


def _needs_affine_parent(matrix):
    decomposed = Matrix.LocRotScale(*matrix.decompose())
    return any(abs(matrix[row][col] - decomposed[row][col]) > 1e-5
               for row in range(3) for col in range(3))


def leave(scene, layer, source):
    if not is_open(source):
        return
    work, shell, anchor, compensation, host = [source.get(k) for k in (_WORK, _SHELL, _ANCHOR, _INVERSE, _HOST)]
    if work is None:
        # A manually removed work collection can leave its data-blocks alive but
        # unlinked. The saved direct links let Closing restore the surviving IDs.
        work = bpy.data.collections.new("GM_RestoreContent")
        for item in source.get(_INITIAL, {}).values():
            if isinstance(item, bpy.types.Collection):
                work.children.link(item)
            elif isinstance(item, bpy.types.Object) and not item.users_collection:
                work.objects.link(item)
    matrix = content_frame(source)
    inverse = matrix.inverted()
    hidden = bool(source.get(_HIDDEN))
    close_linked_peers(source, layer)
    layer.update()
    roots = list(work.all_objects) if work else []
    keep = set(roots)
    outside = [(obj, obj.matrix_world.copy()) for obj in anchor.children
               if obj not in keep and obj != compensation] if anchor else []
    affine_parent = None

    def hold_affine(obj, local, world):
        nonlocal affine_parent
        if obj.animation_data or _needs_affine_parent(world):
            if affine_parent is None:
                affine_parent = bpy.data.objects.new("GM_LocalFrame", None)
                work.objects.link(affine_parent)
                affine_parent.empty_display_size = 0.1
            obj.parent = affine_parent
            obj.matrix_parent_inverse = local
            return True
        return False

    for obj in roots:
        if anchor:
            if obj.parent and obj.parent != anchor:
                continue
            if obj.parent == anchor:
                # The placement matrix lived in the parent inverse, so local channels
                # are already in source coordinates.
                local = inverse @ obj.matrix_parent_inverse
                if local.is_identity or max(abs(local[i][j] - (i == j)) for i in range(4) for j in range(4)) < 1e-5:
                    obj.parent = None
                    obj.matrix_parent_inverse = Matrix.Identity(4)
                    continue
            else:
                local = inverse
            world = local @ obj.matrix_basis
            if not hold_affine(obj, local, world):
                obj.parent = None
                obj.matrix_parent_inverse = Matrix.Identity(4)
                obj.matrix_basis = world
            continue
        if obj.parent and obj.parent in keep:
            continue
        if obj.parent:
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = world
            continue
        # The placement was baked into the object, so put it back into source space.
        restored = inverse @ obj.matrix_basis
        if not hold_affine(obj, inverse, restored):
            obj.matrix_basis = restored
    for obj, world in outside:
        obj.parent = None
        obj.matrix_world = world
    if compensation:
        bpy.data.objects.remove(compensation, do_unlink=True)
    if work:
        _move_contents(work, source)
    for k in _KEYS:
        if k in source:
            del source[k]
    if shell:
        bpy.data.collections.remove(shell)
    if work:
        bpy.data.collections.remove(work)
    if anchor:
        bpy.data.objects.remove(anchor, do_unlink=True)
    layer.update()
    if host and layer.objects.get(host.name) == host:
        host.hide_set(hidden, view_layer=layer)
    layer.update()
