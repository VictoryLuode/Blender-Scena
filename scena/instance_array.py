"""Evaluate Hard Ops' native Array controls on instances inside Scena Group."""

import bpy

from . import hosts


GRAPH = "_scena_array_graph"
GRAPH_NAME = "Scena Array Instances"
BINDINGS = "_scena_array_bindings"
FIELDS = {
    'Viewport': ('Bool', 'show_viewport'), 'Render': ('Bool', 'show_render'),
    'Count': ('Int', 'count'), 'Fit': ('Int', 'fit_type'), 'Length': ('Float', 'fit_length'),
    'Relative': ('Bool', 'use_relative_offset'), 'Factor': ('Vector', 'relative_offset_displace'),
    'Constant': ('Bool', 'use_constant_offset'), 'Distance': ('Vector', 'constant_offset_displace'),
    'Object Offset': ('Bool', 'use_object_offset'),
}


def stack(obj):
    mods = obj.modifiers
    if not mods or mods[0].type != 'NODES' or not mods[0].node_group or not mods[0].node_group.get(hosts.GRAPH_KEY):
        return
    seen_mirror = False
    for mod in list(mods)[1:]:
        if mod.type == 'MIRROR' and not seen_mirror:
            seen_mirror = True
        elif mod.type != 'ARRAY':
            break
        yield mod


def _slots(obj):
    side = 'Before'
    index = 0
    for mod in stack(obj):
        if mod.type == 'MIRROR':
            side, index = 'After', 0
        else:
            index += 1
            yield f'Array {side} {index}', mod


def _signature(slots):
    # Pointer changes need a new Object Info input. Numeric controls use drivers.
    return '|'.join(f'{slot}:{m.persistent_uid}:{m.offset_object.session_uid if m.offset_object else 0}:'
                    f'{m.curve.session_uid if m.curve else 0}' for slot, m in slots)


def needs_sync(obj):
    return (obj.type == 'MESH' and obj.mode == 'OBJECT' and not obj.library and not obj.override_library
            and _signature(_slots(obj)) != obj.get(BINDINGS, ''))


def _build(tree):
    from .instance_mirror import Graph, _socket
    _socket(tree, 'Geometry', 'Geometry')
    _socket(tree, 'Geometry', 'Geometry', 'OUTPUT')
    for name, (kind, _) in FIELDS.items():
        _socket(tree, name, kind)
    for name, kind in [('Offset Object', 'Object'), ('Has Offset', 'Bool'), ('Curve', 'Object'), ('Has Curve', 'Bool')]:
        _socket(tree, name, kind)
    g = Graph(tree)
    i = g.node('NodeGroupInput').outputs
    src = i['Geometry']

    def vector(op, a, b=None):
        args = [(0, a)] if b is None else [(0, a), (1, b)]
        return g.node('ShaderNodeVectorMath', args, operation=op).outputs['Value' if op == 'LENGTH' else 'Vector']

    def math(op, a, b):
        return g.node('ShaderNodeMath', [(0, a), (1, b)], operation=op).outputs[0]

    # Hulls preserve the exact extrema even for rotated or reflected members.
    # Only the hulls go through realization to measure the array step.
    hulls = g.node('GeometryNodeConvexHull', [(0, src)]).outputs[0]
    measured = g.node('GeometryNodeRealizeInstances', [(0, hulls)]).outputs[0]
    bounds = g.node('GeometryNodeBoundBox', [(0, measured)]).outputs
    size = vector('SUBTRACT', bounds['Max'], bounds['Min'])
    relative = g.switch('VECTOR', i['Relative'], (0, 0, 0), vector('MULTIPLY', size, i['Factor']))
    constant = g.switch('VECTOR', i['Constant'], (0, 0, 0), i['Distance'])
    offset = vector('ADD', relative, constant)
    transform = g.node('FunctionNodeCombineTransform', [('Translation', offset)]).outputs[0]
    ob = g.node('GeometryNodeObjectInfo', [('Object', i['Offset Object'])], transform_space='RELATIVE')
    step = g.matrix_product(transform, ob.outputs['Transform'])
    use_object = g.boolean('AND', i['Object Offset'], i['Has Offset'])
    advance = g.node('FunctionNodeSeparateTransform', [(0, step)]).outputs['Translation']
    stride = vector('LENGTH', g.switch('VECTOR', use_object, offset, advance))

    curve = g.node('GeometryNodeObjectInfo', [('Object', i['Curve'])])
    curve_length = g.node('GeometryNodeCurveLength', [(0, curve.outputs['Geometry'])]).outputs[0]
    unit = (3 ** -0.5,) * 3
    scaled = g.node('FunctionNodeTransformDirection', [(0, unit), (1, curve.outputs['Transform'])]).outputs[0]
    curve_length = math('MULTIPLY', curve_length, vector('LENGTH', scaled))
    use_curve = g.boolean('AND', math('GREATER_THAN', i['Fit'], 1), i['Has Curve'])
    length = g.switch('FLOAT', use_curve, i['Length'], curve_length)
    ratio = math('DIVIDE', math('ADD', length, 1e-6), stride)
    floor = g.node('ShaderNodeMath', [(0, ratio)], operation='FLOOR').outputs[0]
    fitted = g.switch('INT', math('GREATER_THAN', stride, 1e-6), 1, math('ADD', floor, 1))
    count = g.switch('INT', i['Fit'], i['Count'], fitted)

    points = g.node('GeometryNodeMeshLine', [('Count', count), ('Offset', offset)]).outputs[0]
    linear = g.node('GeometryNodeInstanceOnPoints', [('Points', points), ('Instance', src)]).outputs[0]
    # Accumulate full matrices for object offsets, including rotation and nonuniform scale.
    end = g.node('GeometryNodeRepeatOutput')
    end.repeat_items.new('MATRIX', 'Transform')
    start = g.node('GeometryNodeRepeatInput')
    start.pair_with_output(end)
    tree.links.new(math('SUBTRACT', count, 1), start.inputs['Iterations'])
    tree.links.new(src, start.inputs['Geometry'])
    tree.links.new(step, start.inputs['Transform'])
    copy = g.node('GeometryNodeTransform', [('Mode', 'Matrix'), ('Geometry', src),
                                            ('Transform', start.outputs['Transform'])]).outputs[0]
    joined = g.node('GeometryNodeJoinGeometry', [(0, start.outputs['Geometry']), (0, copy)]).outputs[0]
    tree.links.new(joined, end.inputs['Geometry'])
    tree.links.new(g.matrix_product(start.outputs['Transform'], step), end.inputs['Transform'])
    result = g.switch('GEOMETRY', use_object, linear, end.outputs['Geometry'])
    viewport = g.node('GeometryNodeIsViewport').outputs[0]
    visible = g.switch('BOOLEAN', viewport, i['Render'], i['Viewport'])
    domain = g.node('GeometryNodeAttributeDomainSize', [(0, src)], component='INSTANCES')
    enabled = g.boolean('AND', visible, domain.outputs['Instance Count'])
    g.node('NodeGroupOutput', [('Geometry', g.switch('GEOMETRY', enabled, src, result))])


def _kernel():
    for tree in bpy.data.node_groups:
        if not tree.library and tree.get(GRAPH):
            return tree
    tree = bpy.data.node_groups.new(GRAPH_NAME, 'GeometryNodeTree')
    try:
        _build(tree)
        tree[GRAPH] = True
        return tree
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


def _unbind(source, slots):
    from .instance_mirror import _entry
    anim = source.id_data.animation_data
    for slot in slots:
        if not source.node_group.get(slot):
            continue
        paths = {_entry(source, f'{slot} {name}').path_from_id('value') for name in FIELDS}
        if anim:
            for curve in list(anim.drivers):
                if curve.data_path in paths:
                    anim.drivers.remove(curve)
        for name in ('Viewport', 'Render'):
            _entry(source, f'{slot} {name}').value = False
        for name in ('Offset Object', 'Curve'):
            _entry(source, f'{slot} {name}').value = None


def sync(obj):
    if not needs_sync(obj):
        return
    from . import instance_mirror as mirror
    source = hosts._source_modifier(obj)
    old = obj.get(BINDINGS, '')
    slots = list(_slots(obj))
    signature = _signature(slots)
    if not source:
        obj.pop(BINDINGS, None)
        return
    _unbind(source, [item.split(':')[0] for item in old.split('|') if item])
    obj.pop(BINDINGS, None)
    if slots:
        mirror._embed(source, mirror._graph())
        kernel = _kernel()
        mirror_node = next(n for n in source.node_group.nodes
                           if n.type == 'GROUP' and n.node_tree and n.node_tree.get(mirror.GRAPH))
        try:
            for slot, mod in slots:
                prefix = slot + ' '
                before = mirror_node if slot.startswith('Array Before') else None
                mirror._embed(source, kernel, marker=slot, prefix=prefix, before=before)
                for name, (kind, prop) in FIELDS.items():
                    entry = mirror._entry(source, prefix + name)
                    for index in range(3) if kind == 'Vector' else (None,):
                        curve = entry.driver_add('value') if index is None else entry.driver_add('value', index)
                        curve.driver.type = 'AVERAGE'
                        target = curve.driver.variables.new().targets[0]
                        target.id = obj
                        target.data_path = mod.path_from_id(prop) + (f'[{index}]' if index is not None else '')
                for name, val in [('Offset Object', mod.offset_object), ('Has Offset', bool(mod.offset_object)),
                                  ('Curve', mod.curve), ('Has Curve', bool(mod.curve))]:
                    mirror._entry(source, prefix + name).value = val
        except Exception:
            _unbind(source, [slot for slot, _ in slots])
            raise
        obj[BINDINGS] = signature
    obj.update_tag()
