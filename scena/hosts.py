"""Persistent native and geometry-nodes placements for Blender 5.3."""

import bpy

DEFINITION_KEY = "_gm_definition"
GRAPH_KEY = "_gm_placement_graph"
SOURCE_SOCKET = "_gm_source_socket"
REALIZE_SOCKET = "_gm_realize_socket"
FRAME_SOCKET = "_scena_frame_modifiers_socket"
PREVIEW_SOCKET = "_scena_modifier_preview_socket"
DEFINITION_PREFIX = "Group_"
GRAPH_NAME = "Scena Group"
MODIFIER_NAME = "Scena Group"
SOURCE_NAME = "Source"
OUTPUT_NAME = "Convert to Mesh"
GEOMETRY_NAME = "Geometry"
_NODE_NAMES = {
    "NodeGroupInput": "Group Inputs",
    "GeometryNodeCollectionInfo": "Group Collection",
    "GeometryNodeRealizeInstances": "Realize Group Geometry",
    "GeometryNodeSwitch": "Choose Instances or Mesh",
    "GeometryNodeMenuSwitch": "Choose Instances or Mesh",
    "NodeGroupOutput": "Group Output",
}


def with_group_prefix(name):
    cleaned = (name or "").strip() or "Group"
    if cleaned.startswith(DEFINITION_PREFIX):
        return cleaned
    return f"{DEFINITION_PREFIX}{cleaned}"


def mark_definition(collection):
    if collection.library:
        raise ValueError("The linked collection is read-only. Make a local definition first")
    prefixed = with_group_prefix(collection.name)
    if collection.name != prefixed:
        collection.name = prefixed
    collection[DEFINITION_KEY] = 1
    collection.use_fake_user = True
    from .usage import invalidate
    invalidate()
    return collection


def is_definition(collection):
    return isinstance(collection, bpy.types.Collection) and collection.get(DEFINITION_KEY) == 1


def is_edit_scene(scene):
    return bool(scene and scene.get("_gm_edit", False))


def _name_modifier(obj, modifier):
    if obj.library or obj.override_library or modifier.name == MODIFIER_NAME:
        return
    modifier.name = MODIFIER_NAME


def _source_modifier(obj):
    if obj and obj.type == 'MESH':
        for modifier in obj.modifiers:
            if modifier.type == 'NODES' and modifier.node_group and modifier.node_group.get(GRAPH_KEY) == 1:
                _name_modifier(obj, modifier)
                return modifier
    return None


def refresh_definition_names():
    for collection in bpy.data.collections:
        if collection.library or collection.override_library or not is_definition(collection):
            continue
        prefixed = with_group_prefix(collection.name)
        if collection.name != prefixed:
            collection.name = prefixed


def refresh_modifier_names():
    for tree in bpy.data.node_groups:
        if tree.library is None and tree.get(GRAPH_KEY) == 1:
            _name_graph(tree)
    for obj in bpy.data.objects:
        _source_modifier(obj)


def _socket(modifier, key):
    identifier = modifier.node_group[key]
    return getattr(modifier.properties.inputs, identifier)


def definition(obj):
    if obj is None:
        return None
    if obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION':
        collection = obj.instance_collection
    else:
        modifier = _source_modifier(obj)
        if not modifier:
            return None
        try:
            collection = _socket(modifier, SOURCE_SOCKET).value
        except (AttributeError, KeyError):
            return None
    return collection if is_definition(collection) else None


def host_type(obj):
    if definition(obj) is None:
        return None
    return 'NATIVE' if obj.type == 'EMPTY' else 'NODES'


def _output_switch(tree):
    node = tree.nodes.new('GeometryNodeSwitch')
    node.input_type = 'GEOMETRY'
    return node


def _upgrade_output(tree):
    identifier = tree[REALIZE_SOCKET]
    item = tree.interface.items_tree[identifier]
    if item.name != OUTPUT_NAME:
        item.name = OUTPUT_NAME
    if item.socket_type != 'NodeSocketMenu':
        return
    inputs = next(n for n in tree.nodes if n.type == 'GROUP_INPUT')
    old = next((link.to_node for link in inputs.outputs[identifier].links
                if link.to_node.type == 'MENU_SWITCH'), None)
    if not old or not all(old.inputs[key].is_linked for key in ('Instance', 'Mesh')):
        return
    mesh_outputs = [mod for obj in bpy.data.user_map(subset={tree}).get(tree, ())
                    if isinstance(obj, bpy.types.Object) and not obj.library
                    for mod in obj.modifiers if mod.type == 'NODES' and mod.node_group == tree
                    and _mesh_output(mod)]
    switch = _output_switch(tree)
    switch.parent = old.parent
    switch.location, switch.width, switch.label = old.location.copy(), old.width, old.label
    name = old.name
    for src, dst in [('Instance', 'False'), ('Mesh', 'True')]:
        tree.links.new(old.inputs[src].links[0].from_socket, switch.inputs[dst])
    for link in list(old.outputs[0].links):
        tree.links.new(switch.outputs[0], link.to_socket)
    tree.nodes.remove(old)
    switch.name = name
    item.socket_type = 'NodeSocketBool'
    tree.links.new(inputs.outputs[identifier], switch.inputs['Switch'])
    tree.interface_update(bpy.context)
    for mod in mesh_outputs:
        _socket(mod, REALIZE_SOCKET).value = True


def _display_socket(tree, key, name, description, *, after, default, output_name):
    identifier = tree.get(key)
    if identifier and tree.interface.items_tree.get(identifier) is not None:
        return
    socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketBool')
    socket.description = description
    socket.structure_type = 'SINGLE'
    socket.default_value = default
    previous = tree.interface.items_tree[tree[after]]
    tree.interface.move(socket, previous.position + 1)
    # Pass the UI setting through a hidden value output. Blender dims unused
    # inputs in the modifier panel. The geometry output stays independent.
    inputs = next((n for n in tree.nodes if n.type == 'GROUP_INPUT'), None)
    output = next((n for n in tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
    if inputs and output:
        setting = next((s for s in tree.interface.items_tree if s.item_type == 'SOCKET'
                        and s.in_out == 'OUTPUT' and s.name == output_name), None)
        if setting is None:
            setting = tree.interface.new_socket(name=output_name, in_out='OUTPUT', socket_type='NodeSocketBool')
            setting.structure_type = 'SINGLE'
            setting.hide_in_modifier = True
        tree.links.new(inputs.outputs[socket.identifier], output.inputs[setting.identifier])
        inputs.outputs[socket.identifier].hide = True
        output.inputs[setting.identifier].hide = True
    tree[key] = socket.identifier
    if default and tree.users:
        # Blender adds an initially false Boolean to existing modifiers before
        # applying the interface default used for future placements.
        for obj in bpy.data.user_map(subset={tree}).get(tree, ()):
            if isinstance(obj, bpy.types.Object) and not obj.library and not obj.override_library:
                for modifier in obj.modifiers:
                    if modifier.type == 'NODES' and modifier.node_group == tree:
                        _socket(modifier, key).value = True


def _ensure_display_sockets(tree):
    if tree.library or tree.override_library:
        return
    from . import preferences
    settings = preferences.preview_settings(bpy.context)
    _display_socket(tree, FRAME_SOCKET, "Frame with Modifiers",
                    "Fit this placement's viewport frame to the result of its modifier stack",
                    after=REALIZE_SOCKET, default=False, output_name="Frame Setting")
    _display_socket(tree, PREVIEW_SOCKET, "Modifier Preview",
                    "Show this placement's modifier copies while editing the group",
                    after=FRAME_SOCKET, default=getattr(settings, "enabled", True), output_name="Preview Setting")


def display_input(obj, key):
    modifier = _source_modifier(obj)
    identifier = modifier.node_group.get(key) if modifier else None
    return getattr(modifier.properties.inputs, identifier, None) if identifier else None


def frame_includes_modifiers(obj):
    entry = display_input(obj, FRAME_SOCKET)
    return bool(entry and entry.value)


def has_viewport_modifiers(obj):
    generator = _source_modifier(obj)
    return generator is not None and any(mod != generator and mod.show_viewport for mod in obj.modifiers)


def _name_graph(tree):
    _upgrade_output(tree)
    _ensure_display_sockets(tree)
    version = tree.get("_scena_node_layout", 0)
    if version >= 2:
        return
    if version == 1:
        for node in tree.nodes:
            parts = node.label.split()
            if (node.type == 'GROUP' and len(parts) == 5 and parts[0] == "Array"
                    and parts[1].isdigit() and parts[2] == "·"
                    and parts[3] in {"Before", "After"} and parts[4] == "Mirror"):
                node.name = node.label = _array_label(parts[3], parts[1])
        tree["_scena_node_layout"] = 2
        return
    if tree.name != GRAPH_NAME:
        tree.name = GRAPH_NAME
    for item in tree.interface.items_tree:
        if item.item_type != 'SOCKET':
            continue
        if item.identifier == tree.get(SOURCE_SOCKET):
            item.name = SOURCE_NAME
            item.description = "Collection shared by linked Groups"
        elif item.identifier == tree.get(REALIZE_SOCKET):
            item.name = OUTPUT_NAME
            item.description = "Choose shared instances or mesh geometry for this Group"
        elif item.in_out == 'OUTPUT' and item.socket_type == 'NodeSocketGeometry':
            item.name = GEOMETRY_NAME
    for node in tree.nodes:
        label = _NODE_NAMES.get(node.bl_idname)
        if label:
            node.name = node.label = label
            node.width = 195
    _layout_stages(tree)
    tree["_scena_node_layout"] = 2


def _array_label(side, index):
    name = "Array" if index == "1" else f"Array {index}"
    return f"{name} {side} Mirror"


def _layout_stages(tree):
    from . import instance_array, instance_mirror
    output = next((node for node in tree.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output), None)
    if not output:
        return
    stages = []
    node = output
    while node.inputs.get(GEOMETRY_NAME) and node.inputs[GEOMETRY_NAME].is_linked:
        node = node.inputs[GEOMETRY_NAME].links[0].from_node
        if node.type in {'SWITCH', 'MENU_SWITCH'}:
            x, y = node.location
            for stage in [*reversed(stages), output]:
                x += 260
                stage.location = (x, y)
            break
        if node.type != 'GROUP' or not node.node_tree or node in stages:
            break
        if node.node_tree.get(instance_mirror.GRAPH):
            label = "Mirror Instances"
        elif node.node_tree.get(instance_array.GRAPH):
            # The linked control retains the slot name even in older projects.
            control = node.inputs.get("Count")
            if not control or not control.is_linked:
                break
            parts = control.links[0].from_socket.name.split()
            if len(parts) != 4 or parts[1] not in {'Before', 'After'}:
                break
            label = _array_label(parts[1], parts[2])
        else:
            break
        node.name = node.label = label
        node.width = 200
        stages.append(node)


def _placement_graph():
    from .instance_mirror import EMBEDDED
    for tree in bpy.data.node_groups:
        # A saved file may contain only the layout of an arrayed placement.
        if (tree.library is None and tree.get(GRAPH_KEY) == 1
                and not tree.get(EMBEDDED) and not tree.get('Array Before 1')):
            _name_graph(tree)
            return tree

    tree = bpy.data.node_groups.new(GRAPH_NAME, 'GeometryNodeTree')
    try:
        tree.is_modifier = True
        tree.interface.new_socket(name=GEOMETRY_NAME, in_out='OUTPUT', socket_type='NodeSocketGeometry')
        source = tree.interface.new_socket(name=SOURCE_NAME, in_out='INPUT', socket_type='NodeSocketCollection')
        realize = tree.interface.new_socket(name=OUTPUT_NAME, in_out='INPUT', socket_type='NodeSocketBool')
        tree[SOURCE_SOCKET] = source.identifier
        tree[REALIZE_SOCKET] = realize.identifier
        inputs = tree.nodes.new('NodeGroupInput')
        collection = tree.nodes.new('GeometryNodeCollectionInfo')
        realization = tree.nodes.new('GeometryNodeRealizeInstances')
        switch = _output_switch(tree)
        output = tree.nodes.new('NodeGroupOutput')
        inputs.location = (-530, 80)
        collection.location = (-300, 100)
        realization.location = (-50, -110)
        switch.location = (180, 100)
        output.location = (420, 100)
        tree.links.new(inputs.outputs[source.name], collection.inputs['Collection'])
        tree.links.new(inputs.outputs[realize.identifier], switch.inputs['Switch'])
        tree.links.new(collection.outputs['Instances'], realization.inputs['Geometry'])
        tree.links.new(collection.outputs['Instances'], switch.inputs['False'])
        tree.links.new(realization.outputs['Geometry'], switch.inputs['True'])
        tree.links.new(switch.outputs[0], output.inputs[GEOMETRY_NAME])
        tree[GRAPH_KEY] = 1
        _name_graph(tree)
        return tree
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


def _check_kind(kind):
    if kind not in {'NATIVE', 'NODES'}:
        raise ValueError(f"Unknown group type {kind}")


def _remove_host(obj):
    mesh = obj.data if obj.type == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh and not mesh.users and not mesh.library:
        bpy.data.meshes.remove(mesh)


def make_host(name, collection, kind):
    _check_kind(kind)
    if not is_definition(collection):
        raise ValueError("A registered group definition is required")
    mesh = bpy.data.meshes.new(name) if kind == 'NODES' else None
    obj = bpy.data.objects.new(name, mesh)
    try:
        if mesh:
            mod = obj.modifiers.new(MODIFIER_NAME, 'NODES')
            mod.node_group = _placement_graph()
            _socket(mod, SOURCE_SOCKET).value = collection
        else:
            obj.empty_display_type = 'PLAIN_AXES'
            obj.empty_display_size = 0.35
            obj.instance_type = 'COLLECTION'
            obj.instance_collection = collection
        return obj
    except Exception:
        _remove_host(obj)
        raise


def assign_definition(obj, collection):
    if not is_definition(collection):
        raise ValueError("A registered group definition is required")
    if obj.library or obj.override_library:
        raise ValueError(f"Link Group {obj.name} is linked or overridden")
    if obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION':
        obj.instance_collection = collection
    else:
        modifier = _source_modifier(obj)
        if not modifier:
            raise ValueError("Select a Link Group")
        _socket(modifier, SOURCE_SOCKET).value = collection
    obj.update_tag()
    from .usage import invalidate
    invalidate()


def _mesh_output(modifier):
    value = _socket(modifier, REALIZE_SOCKET).value
    return value is True or value == "Mesh"


def get_realize(obj):
    modifier = _source_modifier(obj)
    return bool(modifier and _mesh_output(modifier))


def _non_mesh_member(collection):
    pending = [collection]
    seen = set()
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.add(item)
        for obj in item.objects:
            nested = definition(obj)
            if nested:
                pending.append(nested)
            elif obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION' and obj.instance_collection:
                pending.append(obj.instance_collection)
            elif obj.type != 'MESH' and obj.type != 'EMPTY':
                return obj
        pending.extend(item.children)
    return None


def check_realize(obj, enabled):
    if host_type(obj) != 'NODES':
        raise ValueError("Switch to a Geometry Nodes group first")
    if obj.library or obj.override_library:
        raise ValueError("The linked group is read-only")
    if enabled:
        unsupported = _non_mesh_member(definition(obj))
        if unsupported:
            raise ValueError(f"{unsupported.name} ({unsupported.type}) must keep its object type. Realized mesh output only supports mesh members")


def set_realize(obj, enabled):
    check_realize(obj, enabled)
    output = _socket(_source_modifier(obj), REALIZE_SOCKET)
    if isinstance(output.value, bool):  # Older linked node groups can be read-only.
        output.value = bool(enabled)
    else:
        output.value = "Mesh" if enabled else "Instance"
    obj.update_tag()


def _fcurve_paths(action, slot):
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = strip.channelbag(slot) if slot else None
            if channelbag:
                for curve in channelbag.fcurves:
                    yield curve.data_path


def check_switch(obj, kind):
    _check_kind(kind)
    current = host_type(obj)
    if current is None:
        raise ValueError("Select a Link Group")
    if current == kind:
        return
    if obj.library or obj.override_library or obj.mode != 'OBJECT':
        raise ValueError("Switch a local Link Group from Object Mode")
    source = _source_modifier(obj)
    extra = [m.name for m in obj.modifiers if m != source]
    if extra:
        raise ValueError("This Link Group has modifiers. Resolve them first: " + "、".join(extra))
    if source and (not source.show_render or not source.show_viewport):
        raise ValueError("The source modifier is hidden. Make it visible first")
    if obj.type == 'MESH' and (len(obj.data.vertices) or obj.data.materials or obj.data.shape_keys):
        raise ValueError("The host mesh has custom geometry, materials, or shape keys")
    if obj.asset_data or obj.rigid_body or obj.rigid_body_constraint:
        raise ValueError("Asset marks or rigid-body settings need the current object type")
    animation = obj.animation_data
    if animation:
        if animation.drivers or animation.nla_tracks:
            raise ValueError("Drivers or NLA tracks need the current object type")
        if animation.action:
            for path in _fcurve_paths(animation.action, animation.action_slot):
                if path.startswith(("modifiers[", "data.", "instance_", "empty_", "vertex_groups[")):
                    raise ValueError(f"An action references a type-specific property {path}")

    for user in bpy.data.user_map(subset={obj}).get(obj, ()):
        if user.library or user.override_library:
            raise ValueError(f"Linked user {user.name} cannot be remapped safely")


def _copy_settings(source, target):
    # Copy individual transform channels, including delta transforms and locked axes.
    # Assigning only matrix_world would discard their decomposition and animation base.
    for name in (
        'parent', 'parent_type', 'parent_vertices', 'parent_bone', 'parent_bone_head_tail_factor',
        'use_parent_final_indices', 'matrix_parent_inverse', 'rotation_mode', 'location',
        'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale', 'delta_location',
        'delta_rotation_euler', 'delta_rotation_quaternion', 'delta_scale',
        'lock_location', 'lock_rotation', 'lock_rotation_w', 'lock_rotations_4d', 'lock_scale',
        'use_fake_user', 'pass_index', 'color', 'hide_viewport', 'hide_select', 'hide_render',
        'visible_camera', 'visible_diffuse', 'visible_glossy', 'visible_transmission',
        'visible_volume_scatter', 'visible_shadow', 'is_holdout', 'is_shadow_catcher',
        'show_name', 'show_axis', 'show_bounds', 'display_bounds_type', 'show_in_front',
        'display_type', 'show_wire', 'show_all_edges', 'show_transparent', 'lightgroup',
    ):
        setattr(target, name, getattr(source, name))
    for constraint in source.constraints:
        clone = target.constraints.copy(constraint)
        if constraint == source.constraints.active:
            target.constraints.active = clone
    for key, value in source.items():
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "to_list"):
            value = value.to_list()
        target[key] = value
        try:
            target.id_properties_ui(key).update_from(source.id_properties_ui(key))
        except TypeError:
            pass
    if source.animation_data and source.animation_data.action:
        current = source.animation_data
        animation = target.animation_data_create()
        animation.action = current.action
        animation.action_slot = current.action_slot
        animation.action_blend_type = current.action_blend_type
        animation.action_extrapolation = current.action_extrapolation
        animation.action_influence = current.action_influence


def _set_pointer(holder, key, value):
    prop = holder.bl_rna.properties.get(key) if getattr(holder, "bl_rna", None) else None
    if prop is not None and not prop.is_readonly:
        setattr(holder, key, value)
    else:
        holder[key] = value


def _dependent_bindings(obj):
    """Object pointers on other local objects that should follow a stand-in."""
    bindings = []
    for user in bpy.data.user_map(subset={obj}).get(obj, ()):
        if not isinstance(user, bpy.types.Object) or user == obj or user.library or user.override_library:
            continue
        for holder in (*user.modifiers, *user.constraints):
            for prop in holder.bl_rna.properties:
                if prop.type != 'POINTER' or prop.is_readonly:
                    continue
                if getattr(holder, prop.identifier, None) == obj:
                    bindings.append((holder, prop.identifier))
            try:
                keys = list(holder.keys())
            except (AttributeError, TypeError):
                keys = []
            for key in keys:
                if holder[key] == obj:
                    bindings.append((holder, key))
    return bindings


def stand_in_for_dependents(obj):
    """Point outside modifier and constraint targets at an Empty with this object's transform."""
    bindings = _dependent_bindings(obj)
    if not bindings:
        return None
    empty = bpy.data.objects.new(f"{obj.name}_GM_Target", None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = 0.25
    for collection in obj.users_collection or (bpy.context.scene.collection,):
        collection.objects.link(empty)
    world = obj.matrix_world.copy()
    empty.parent = obj.parent
    empty.parent_type = obj.parent_type
    empty.parent_bone = obj.parent_bone
    empty.matrix_world = world
    for holder, key in bindings:
        _set_pointer(holder, key, empty)
    return empty, bindings


def _type_dependent_bindings(obj):
    bindings = []
    for user in bpy.data.user_map(subset={obj}).get(obj, ()):
        if not isinstance(user, bpy.types.Object) or user.library or user.override_library:
            continue
        for modifier in user.modifiers:
            if modifier.type == 'NODES':
                continue
            for prop in modifier.bl_rna.properties:
                if prop.type != 'POINTER' or prop.is_readonly:
                    continue
                if getattr(modifier, prop.identifier, None) == obj:
                    bindings.append((modifier, prop.identifier))
    return bindings


def _stand_in_for_type_dependents(obj):
    """Give type-sensitive modifier targets an Empty that keeps this placement's transform."""
    bindings = _type_dependent_bindings(obj)
    if not bindings:
        return None
    empty = bpy.data.objects.new(f"{obj.name}_GM_Target", None)
    applied = []
    try:
        empty.empty_display_type = 'PLAIN_AXES'
        empty.empty_display_size = 0.25
        for collection in obj.users_collection or (bpy.context.scene.collection,):
            collection.objects.link(empty)
        empty.matrix_world = obj.matrix_world.copy()
        for modifier, identifier in bindings:
            _set_pointer(modifier, identifier, empty)
            applied.append((modifier, identifier))
    except Exception:
        _discard_stand_in((empty, applied), obj)
        raise
    return empty, bindings


def _discard_stand_in(stand_in, original):
    if not stand_in:
        return
    empty, bindings = stand_in
    for holder, key in bindings:
        _set_pointer(holder, key, original)
    bpy.data.objects.remove(empty, do_unlink=True)


def _parent_keep_world(child, parent):
    world = child.matrix_world.copy()
    child.parent = parent
    try:
        child.matrix_parent_inverse = parent.matrix_world.inverted() @ world
    except ValueError:
        child.parent = None
        child.matrix_world = world


def switch_host(obj, kind):
    """Replace the carrier and remap its users as a single undoable caller operation."""
    with HostSwitches() as switches:
        return switches.convert(obj, kind)


class _HostSwitch:
    def __init__(self, original):
        self.original = original
        self.name = original.name
        self.target = self.stand_in = None
        self.remapped = False
        self.layers = [
            (layer, original.select_get(view_layer=layer), original.hide_get(view_layer=layer),
             layer.objects.active == original)
            for scene in original.users_scene for layer in scene.view_layers
            if layer.objects.get(self.name) == original
        ]
        self.local_views = [
            (area.spaces.active, original.local_view_get(area.spaces.active))
            for wm in bpy.data.window_managers for window in wm.windows
            if window.view_layer.objects.get(self.name) == original
            for area in window.screen.areas
            if area.type == 'VIEW_3D' and area.spaces.active.local_view
        ]

    def _restore_view(self, obj):
        for layer, selected, hidden, active in self.layers:
            layer.update()
            if layer.objects.get(self.name) == obj:
                obj.hide_set(hidden, view_layer=layer)
                obj.select_set(selected, view_layer=layer)
                if active:
                    layer.objects.active = obj
        for space, included in self.local_views:
            obj.local_view_set(space, included)

    def convert(self, kind):
        self.stand_in = _stand_in_for_type_dependents(self.original)
        self.target = make_host(self.name, definition(self.original), kind)
        _copy_settings(self.original, self.target)
        self.original.user_remap(self.target)
        self.remapped = True
        self.original.name = ".Scena replaced"
        self.target.name = self.name
        self._restore_view(self.target)
        if self.stand_in:
            _parent_keep_world(self.stand_in[0], self.target)
        return self.target

    def rollback(self):
        if self.remapped:
            self.target.user_remap(self.original)
        _discard_stand_in(self.stand_in, self.original)
        if self.target is not None:
            _remove_host(self.target)
        if self.remapped:
            self.original.name = self.name
            self._restore_view(self.original)


class HostSwitches:
    """Retain original carriers and their references until every switch succeeds."""

    def __init__(self):
        self.changes = []
        self.placement_graphs = None

    def __enter__(self):
        return self

    def convert(self, obj, kind):
        check_switch(obj, kind)
        if host_type(obj) == kind:
            return obj
        if kind == 'NODES' and self.placement_graphs is None:
            self.placement_graphs = {tree for tree in bpy.data.node_groups if tree.get(GRAPH_KEY) == 1}
        change = _HostSwitch(obj)
        self.changes.append(change)
        return change.convert(kind)

    def __exit__(self, error, value, trace):
        if error is None:
            for change in self.changes:
                _remove_host(change.original)
        else:
            for change in reversed(self.changes):
                change.rollback()
            if self.placement_graphs is not None:
                for tree in list(bpy.data.node_groups):
                    if not tree.users and tree.get(GRAPH_KEY) == 1 and tree not in self.placement_graphs:
                        bpy.data.node_groups.remove(tree)
