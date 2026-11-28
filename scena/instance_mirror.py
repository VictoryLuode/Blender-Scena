"""Mirror whole group instances until the user enables Realize Instances."""

import bpy
from bpy.app.handlers import persistent

from . import hosts, instance_array


GRAPH = "_scena_instance_mirror"
GRAPH_NAME = "Scena Mirror Instances"
PREVIOUS_REALIZE = "_scena_previous_realize"
CREATED_ANIMATION = "_scena_mirror_created_animation"
BOUND = "_scena_bound_mirror_uid"
MANUAL_OFF = "_scena_manual_mirror_uid"
_pending = {}


class Graph:
    def __init__(self, tree):
        self.tree = tree

    def node(self, kind, inputs=(), **settings):
        node = self.tree.nodes.new(kind)
        for key, value in settings.items():
            setattr(node, key, value)
        depth = 0
        for key, value in inputs:
            if isinstance(value, bpy.types.NodeSocket):
                self.tree.links.new(value, node.inputs[key])
                depth = max(depth, int(value.node.location.x) + 200)
            else:
                node.inputs[key].default_value = value
        node.location = (depth, -180 * sum(n != node and n.location.x == depth for n in self.tree.nodes))
        return node

    def switch(self, kind, condition, off, on):
        inputs = [('Switch', condition)]
        inputs += [(key, val) for key, val in [('False', off), ('True', on)] if val is not None]
        return self.node('GeometryNodeSwitch', inputs, input_type=kind).outputs[0]

    def boolean(self, op, a, b):
        return self.node('FunctionNodeBooleanMath', [(0, a), (1, b)], operation=op).outputs[0]

    def matrix_product(self, a, b):
        return self.node('FunctionNodeMatrixMultiply', [(0, a), (1, b)]).outputs[0]

def _socket(tree, name, kind, direction='INPUT'):
    return tree.interface.new_socket(name=name, in_out=direction, socket_type='NodeSocket' + kind)


FIELDS = {'Viewport': 'show_viewport', 'Render': 'show_render'}
GRAPH_VERSION = 2


def _wire_kernel(tree):
    # Keep socket identifiers stable for drivers and links in existing .blend files.
    original = set(tree.nodes)
    previous = next((n for n in original if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
    try:
        g = Graph(tree)
        i = g.node('NodeGroupInput').outputs
        columns = []
        for c in range(4):
            xyz = g.node('ShaderNodeSeparateXYZ', [(0, i[f'Plane Column {c}'])]).outputs
            columns.extend((4*c+r, xyz[r]) for r in range(3))
        world = g.node('FunctionNodeCombineMatrix', columns).outputs[0]
        self_obj = g.node('GeometryNodeSelfObject').outputs[0]
        owner = g.node('GeometryNodeObjectInfo', [('Object', self_obj)]).outputs['Transform']
        relative = g.matrix_product(g.node('FunctionNodeInvertMatrix', [(0, owner)]).outputs[0], world)
        identity = g.node('FunctionNodeCombineMatrix').outputs[0]
        plane = g.switch('MATRIX', i['External Plane'], identity, relative)
        inverse = g.node('FunctionNodeInvertMatrix', [(0, plane)]).outputs[0]
        geometry = i['Geometry']
        for index, axis in enumerate('XYZ'):
            diagonal = g.node('FunctionNodeCombineMatrix', [(5 * index, -1.0)]).outputs[0]
            reflect = g.matrix_product(plane, g.matrix_product(diagonal, inverse))
            copy = g.node('GeometryNodeTransform', [('Mode', 'Matrix'), ('Geometry', geometry),
                                                   ('Transform', reflect)]).outputs[0]
            joined = g.node('GeometryNodeJoinGeometry', [(0, geometry), (0, copy)]).outputs[0]
            geometry = g.switch('GEOMETRY', i[axis + ' Enabled'], geometry, joined)
        viewport = g.node('GeometryNodeIsViewport').outputs[0]
        enabled = g.switch('BOOLEAN', viewport, i['Render'], i['Viewport'])
        # The upstream Scena Group switch supplies mesh only when Realize Instances is on.
        domain = g.node('GeometryNodeAttributeDomainSize', [(0, i['Geometry'])], component='INSTANCES')
        enabled = g.boolean('AND', enabled, domain.outputs['Instance Count'])
        output = g.node('NodeGroupOutput', [('Geometry', g.switch('GEOMETRY', enabled, i['Geometry'], geometry))])
        output.is_active_output = True
    except Exception:
        for node in set(tree.nodes) - original:
            tree.nodes.remove(node)
        if previous:
            previous.is_active_output = True
        raise
    for node in original:
        tree.nodes.remove(node)
    tree[GRAPH] = GRAPH_VERSION


def upgrade_graphs():
    for tree in list(bpy.data.node_groups):
        if tree.library or tree.override_library:
            continue
        if tree.get(GRAPH):
            if tree.get(GRAPH) == 1:
                _wire_kernel(tree)
            if tree.name.startswith(("Scena · Instance Mirror", "Scena · Mirror Instances")):
                tree.name = GRAPH_NAME
        elif tree.get(instance_array.GRAPH) and tree.name.startswith(("Scena · Instance Array", "Scena · Array Instances")):
            tree.name = instance_array.GRAPH_NAME


def _graph():
    for tree in bpy.data.node_groups:
        if not tree.library and tree.get(GRAPH):
            if tree.get(GRAPH) == 1:
                _wire_kernel(tree)
            return tree
    tree = bpy.data.node_groups.new(GRAPH_NAME, 'GeometryNodeTree')
    try:
        tree.is_modifier = True
        _socket(tree, 'Geometry', 'Geometry')
        _socket(tree, 'Geometry', 'Geometry', 'OUTPUT')
        for name in FIELDS:
            _socket(tree, name, 'Bool').hide_in_modifier = True
        for axis in 'XYZ':
            _socket(tree, axis + ' Enabled', 'Bool').hide_in_modifier = True
        for c in range(4):
            _socket(tree, f'Plane Column {c}', 'Vector').hide_in_modifier = True
        _socket(tree, 'External Plane', 'Bool').hide_in_modifier = True
        _wire_kernel(tree)
        return tree
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


EMBEDDED = "_scena_mirror_embedded"


def _controls(kernel):
    return [s for s in kernel.interface.items_tree
            if s.in_out == 'INPUT' and s.socket_type != 'NodeSocketGeometry']


def _add_stage(tree, kernel, marker, *, prefix="", before=None):
    # Interface edits rebuild group node sockets. Resolve the links only after
    # the interface is complete, using identifiers that survive those edits.
    controls = []
    for control in _controls(kernel):
        socket = tree.interface.new_socket(name=prefix + control.name, in_out='INPUT', socket_type=control.socket_type)
        socket.hide_in_modifier = True
        controls.append((socket.identifier, control.identifier))
    group = tree.nodes.new('GeometryNodeGroup')
    group.node_tree = kernel
    inputs = next((n for n in tree.nodes if n.type == 'GROUP_INPUT'), None)
    output = before or next((n for n in tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
    destination = output.inputs.get(hosts.GEOMETRY_NAME) if output else None
    if inputs is None or destination is None or not destination.is_linked:
        raise ValueError("Scena Group needs a connected geometry output to add modifier support")
    upstream = destination.links[0].from_socket
    for interface_id, control_id in controls:
        source, target = inputs.outputs[interface_id], group.inputs[control_id]
        tree.links.new(source, target)
        source.hide = target.hide = True
    tree.links.new(upstream, group.inputs['Geometry'])
    tree.links.new(group.outputs['Geometry'], destination)
    tree[marker] = True


def _ensure_stages(mod, slots=(), *, mirror_kernel=None):
    original = mod.node_group
    missing = [slot for slot in slots if not original.get(slot)]
    needs_mirror = not original.get(EMBEDDED) and (
        mirror_kernel is not None or any(slot.startswith('Array After ') for slot in slots))
    if not (needs_mirror or missing):
        return
    # Placements at different nesting levels can evaluate the same node tree.
    # Build the entire interface off the modifier and publish it in one assignment.
    tree = original.copy()
    try:
        if original.library:
            hosts._name_graph(tree)
        if needs_mirror:
            _add_stage(tree, mirror_kernel or _graph(), EMBEDDED)
        if missing:
            kernel = instance_array._kernel()
            mirror = next((n for n in tree.nodes if n.type == 'GROUP' and n.node_tree and n.node_tree.get(GRAPH)), None)
            for slot in missing:
                before = mirror if slot.startswith('Array Before ') else None
                _add_stage(tree, kernel, slot, prefix=slot + ' ', before=before)
        hosts._layout_stages(tree)
        mod.node_group = tree
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


def _legacy(obj):
    return next((m for m in obj.modifiers if m.type == 'NODES' and m.node_group and m.node_group.get(GRAPH)), None)


def adapter(obj):
    legacy = _legacy(obj)
    if legacy:
        return legacy
    if BOUND in obj or PREVIOUS_REALIZE in obj:
        mod = hosts._source_modifier(obj)
        if mod and mod.node_group.get(EMBEDDED):
            return mod
    return None


def _entry(mod, name):
    return getattr(mod.properties.inputs, mod.node_group.interface.items_tree[name].identifier)


def migrate(obj):
    legacy = _legacy(obj)
    if legacy is None or obj.library or obj.override_library:
        return
    mod = hosts._source_modifier(obj)
    if not mod or list(obj.modifiers).index(legacy) != list(obj.modifiers).index(mod) + 1:
        return
    _ensure_stages(mod, mirror_kernel=legacy.node_group)
    paths = {}
    muted = set()
    for socket in _controls(legacy.node_group):
        old = _entry(legacy, socket.name)
        new = _entry(mod, socket.name)
        if (socket.name == 'Viewport' and not legacy.show_viewport
                or socket.name == 'Render' and not legacy.show_render):
            muted.add(old.path_from_id('value'))
            new.value = False
        else:
            new.value = old.value
        paths[old.path_from_id('value')] = new.path_from_id('value')
    anim = obj.animation_data
    if anim:
        for curve in anim.drivers:
            if curve.data_path in paths:
                if curve.data_path in muted:
                    curve.mute = True
                curve.data_path = paths[curve.data_path]
    obj.modifiers.remove(legacy)
    obj.update_tag()


def _remove_drivers(obj, mod, created_animation):
    if not obj.animation_data:
        return
    kernel = next(n.node_tree for n in mod.node_group.nodes
                  if n.type == 'GROUP' and n.node_tree and n.node_tree.get(GRAPH))
    paths = {_entry(mod, s.name).path_from_id('value') for s in _controls(kernel)}
    for curve in list(obj.animation_data.drivers):
        if curve.data_path in paths:
            obj.animation_data.drivers.remove(curve)
    anim = obj.animation_data
    if created_animation and not (anim.drivers or anim.action or anim.nla_tracks):
        obj.animation_data_clear()


def _disconnect(obj, existing):
    if existing.node_group.get(GRAPH):
        raise ValueError("Place the legacy Instance Mirror directly after Scena Group to migrate it")
    _remove_drivers(obj, existing, obj.get(CREATED_ANIMATION, False))
    _entry(existing, 'Viewport').value = False
    _entry(existing, 'Render').value = False
    obj.pop(PREVIOUS_REALIZE, None)
    obj.pop(CREATED_ANIMATION, None)
    obj.pop(BOUND, None)
    obj.update_tag()


def _enable(obj):
    if hosts.host_type(obj) != 'NODES':
        raise ValueError("Switch the Link Group to Geometry Nodes first")
    mods = list(obj.modifiers)
    mirror = _stack_mirror(obj)
    if mods[0] != hosts._source_modifier(obj) or (not mirror and len(mods) != 1):
        raise ValueError("Instance Mirror needs the Group source followed by one Mirror")
    mod = mods[0]
    slots = [slot for slot, _ in instance_array._slots(obj)]
    _ensure_stages(mod, slots, mirror_kernel=_graph())
    created = mirror is None
    created_animation = obj.animation_data is None
    try:
        if created:
            mirror = obj.modifiers.new('Mirror', 'MIRROR')
        base = mirror.path_from_id() + '.'

        def drive(socket, prop, component=None, optional=False):
            entry = _entry(mod, socket)
            curve = entry.driver_add('value') if component is None else entry.driver_add('value', component)
            driver = curve.driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            target = var.targets[0]
            target.id = obj
            target.data_path = base + prop
            if optional:
                target.use_fallback_value = True
                target.fallback_value = 0

        for name, prop in FIELDS.items():
            drive(name, prop)
        for idx, axis in enumerate('XYZ'):
            drive(axis + ' Enabled', f'use_axis[{idx}]')
        for col in range(4):
            for row in range(3):
                drive(f'Plane Column {col}', f'mirror_object.matrix_world[{col}][{row}]', row, True)
        drive('External Plane', 'mirror_object.matrix_world[3][3]', optional=True)
        obj[CREATED_ANIMATION] = created_animation
        obj[BOUND] = mirror.persistent_uid
        obj.pop(MANUAL_OFF, None)
    except Exception:
        _remove_drivers(obj, mod, created_animation)
        _entry(mod, 'Viewport').value = False
        _entry(mod, 'Render').value = False
        if created and mirror:
            obj.modifiers.remove(mirror)
        raise


def _stack_mirror(obj):
    for mod in instance_array.stack(obj):
        if mod.type == 'MIRROR':
            return mod
    return None


def toggle(obj):
    migrate(obj)
    existing = adapter(obj)
    if existing:
        mirror = _stack_mirror(obj)
        _disconnect(obj, existing)
        if mirror:
            obj[MANUAL_OFF] = mirror.persistent_uid
    else:
        _enable(obj)


def _needs_sync(obj):
    if obj.type != 'MESH' or obj.library or obj.override_library or obj.mode != 'OBJECT':
        return False
    mirror = _stack_mirror(obj)
    if not mirror:
        return BOUND in obj or PREVIOUS_REALIZE in obj or MANUAL_OFF in obj
    uid = mirror.persistent_uid
    return uid != obj.get(MANUAL_OFF) and uid != obj.get(BOUND)


def _sync(obj):
    if not _needs_sync(obj):
        return
    mirror = _stack_mirror(obj)
    existing = adapter(obj)
    if existing:
        _disconnect(obj, existing)
    elif BOUND in obj or PREVIOUS_REALIZE in obj:
        for key in (PREVIOUS_REALIZE, CREATED_ANIMATION, BOUND):
            obj.pop(key, None)
    obj.pop(MANUAL_OFF, None)
    if mirror and hosts.definition(obj):
        _enable(obj)


def _flush():
    # Resolve live IDs after the operator and its modifier sorting have finished.
    batch = _pending.copy()
    _pending.clear()
    for uid, name in batch.items():
        obj = bpy.data.objects.get(name)
        if obj and obj.session_uid == uid:
            try:
                _sync(obj)
                instance_array.sync(obj)
            except (ValueError, RuntimeError) as exc:
                print(f"Scena: {obj.name}: {exc}")
    return 0.0 if _pending else None


@persistent
def _stack_updates(scene, depsgraph):
    for update in depsgraph.updates:
        obj = update.id
        if isinstance(obj, bpy.types.Object) and update.is_updated_geometry:
            obj = obj.original
            if _needs_sync(obj) or instance_array.needs_sync(obj):
                _pending[obj.session_uid] = obj.name
    if _pending and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.0)


@persistent
def refresh(_unused=None):
    _pending.clear()
    for obj in bpy.data.objects:
        if _needs_sync(obj) or instance_array.needs_sync(obj):
            _pending[obj.session_uid] = obj.name
    if _pending and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.0)


def register():
    bpy.app.handlers.depsgraph_update_post.append(_stack_updates)
    bpy.app.handlers.undo_post.append(refresh)
    bpy.app.handlers.redo_post.append(refresh)


def unregister():
    bpy.app.handlers.depsgraph_update_post.remove(_stack_updates)
    bpy.app.handlers.undo_post.remove(refresh)
    bpy.app.handlers.redo_post.remove(refresh)
    if bpy.app.timers.is_registered(_flush):
        bpy.app.timers.unregister(_flush)
    _pending.clear()
