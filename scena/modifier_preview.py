"""Remove the preview mesh at the editable prototype in placement-local space."""

from .instance_mirror import Graph


def _near(g, a, b):
    distance = g.node('ShaderNodeVectorMath', [(0, a), (1, b)], operation='DISTANCE').outputs['Value']
    return g.node('ShaderNodeMath', [(0, distance), (1, 1e-5)], operation='LESS_THAN').outputs[0]


def _sample(g, geometry, domain, data_type, value, index):
    return g.node('GeometryNodeSampleIndex', [('Geometry', geometry), ('Value', value), ('Index', index)],
                  data_type=data_type, domain=domain).outputs['Value']


def _total(g, field, island, domain):
    return g.node('GeometryNodeAccumulateField', [('Value', field), ('Group ID', island)],
                  data_type='INT', domain=domain).outputs['Total']


def build(tree, host, source):
    g = Graph(tree)
    tree.is_modifier = True
    tree.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    placement = g.node('GeometryNodeObjectInfo', [('Object', host)], transform_space='ORIGINAL')
    result = g.node('GeometryNodeRealizeInstances', [(0, placement.outputs['Geometry'])]).outputs[0]
    collection = g.node('GeometryNodeCollectionInfo', [('Collection', source)], transform_space='ORIGINAL')
    prototype = g.node('GeometryNodeRealizeInstances', [(0, collection.outputs['Instances'])]).outputs[0]
    parts = g.node('GeometryNodeSeparateComponents', [(0, result)]).outputs
    mesh = parts['Mesh']
    position = g.node('GeometryNodeInputPosition').outputs[0]
    normal = g.node('GeometryNodeInputNormal').outputs[0]
    island = g.node('GeometryNodeInputMeshIsland').outputs['Island Index']

    # A bevel changes topology and moves the corners inside the editable surface.
    # Keep whole offset islands, including ones which partially intersect the source.
    surface = g.node('GeometryNodeProximity', [('Geometry', prototype)], target_element='FACES').outputs
    face_normal = g.node('GeometryNodeFieldOnDomain', [('Value', normal)], data_type='FLOAT_VECTOR',
                         domain='FACE').outputs[0]
    adjacent_faces = g.node('GeometryNodeInputMeshEdgeNeighbors').outputs['Face Count']
    boundary = g.node('FunctionNodeCompare', [('A', adjacent_faces), ('B', 2)],
                      data_type='INT', operation='NOT_EQUAL').outputs[0]
    # An open surface has no inside. It can match only at the surface itself.
    volume_normal = g.switch('VECTOR', _total(g, boundary, island, 'EDGE'), face_normal, (0, 0, 0))
    surface_normal = g.node('GeometryNodeSampleNearestSurface', [('Mesh', prototype), ('Value', volume_normal)],
                            data_type='FLOAT_VECTOR').outputs['Value']
    delta = g.node('ShaderNodeVectorMath', [(0, position), (1, surface['Position'])],
                   operation='SUBTRACT').outputs[0]
    side = g.node('ShaderNodeVectorMath', [(0, delta), (1, surface_normal)], operation='DOT_PRODUCT').outputs['Value']
    inside = g.node('ShaderNodeMath', [(0, side), (1, -1e-5)], operation='LESS_THAN').outputs[0]
    on_surface = g.node('ShaderNodeMath', [(0, surface['Distance']), (1, 1e-5)], operation='LESS_THAN').outputs[0]
    covered = g.boolean('AND', surface['Is Valid'], g.boolean('OR', inside, on_surface))
    outside = g.node('FunctionNodeBooleanMath', [(0, covered)], operation='NOT').outputs[0]
    inside_island = g.node('FunctionNodeBooleanMath', [(0, _total(g, outside, island, 'POINT'))],
                           operation='NOT').outputs[0]

    # Assign a prototype face to the result island that actually occupies its position.
    # A separate copy inside another member does not claim that member's faces.
    nearest = g.node('GeometryNodeSampleNearest', [('Geometry', prototype)], domain='FACE').outputs['Index']
    source_center = _sample(g, prototype, 'FACE', 'FLOAT_VECTOR', position, nearest)
    hit = g.node('GeometryNodeSampleNearestSurface', [('Mesh', mesh), ('Value', island), ('Sample Position', source_center)],
                 data_type='INT').outputs
    owns_surface = g.node('FunctionNodeCompare', [('A', hit['Value']), ('B', island)],
                          data_type='INT', operation='EQUAL').outputs[0]
    # Several zero-offset copies may tie for the same nearest surface.
    centered = _near(g, position, source_center)
    anchor = g.boolean('AND', on_surface, g.boolean('OR', owns_surface, centered))
    same_island = g.boolean('AND', inside_island, _total(g, anchor, island, 'FACE'))

    # A welded mirror or array can share vertices with the prototype. Remove
    # coincident faces individually in that case and retain the connected copies.
    source_normal = _sample(g, prototype, 'FACE', 'FLOAT_VECTOR', normal, nearest)
    area = g.node('GeometryNodeInputMeshFaceArea').outputs['Area']
    source_area = _sample(g, prototype, 'FACE', 'FLOAT', area, nearest)
    equal_area = g.node('FunctionNodeCompare', [('A', area), ('B', source_area), ('Epsilon', 1e-8)],
                        data_type='FLOAT', operation='EQUAL').outputs[0]
    same_face = g.boolean('AND', centered, g.boolean('AND', equal_area, _near(g, normal, source_normal)))
    remove = g.boolean('AND', surface['Is Valid'], g.boolean('OR', same_island, same_face))
    faces = g.node('GeometryNodeDeleteGeometry', [('Geometry', mesh), ('Selection', remove)], domain='FACE').outputs[0]

    # Loose edges and points have no face samples. Match them in their own domains.
    edge = g.node('GeometryNodeSampleNearest', [('Geometry', prototype)], domain='EDGE').outputs['Index']
    ends = g.node('GeometryNodeInputMeshEdgeVertices').outputs
    a, b = ends['Position 1'], ends['Position 2']
    ref_a = _sample(g, prototype, 'EDGE', 'FLOAT_VECTOR', a, edge)
    ref_b = _sample(g, prototype, 'EDGE', 'FLOAT_VECTOR', b, edge)
    same_edge = g.boolean('OR', g.boolean('AND', _near(g, a, ref_a), _near(g, b, ref_b)),
                          g.boolean('AND', _near(g, a, ref_b), _near(g, b, ref_a)))
    loose_edge = g.node('FunctionNodeBooleanMath', [(0, adjacent_faces)], operation='NOT').outputs[0]
    edges = g.node('GeometryNodeDeleteGeometry', [('Geometry', faces), ('Selection', g.boolean('AND', loose_edge, same_edge))],
                   domain='EDGE').outputs[0]

    near_point = g.node('GeometryNodeProximity', [('Geometry', prototype)], target_element='POINTS').outputs
    neighbors = g.node('GeometryNodeInputMeshVertexNeighbors').outputs['Vertex Count']
    loose_point = g.node('FunctionNodeBooleanMath', [(0, neighbors)], operation='NOT').outputs[0]
    same_point = g.boolean('AND', near_point['Is Valid'], _near(g, position, near_point['Position']))
    filtered = g.node('GeometryNodeDeleteGeometry', [('Geometry', edges), ('Selection', g.boolean('AND', loose_point, same_point))],
                      domain='POINT').outputs[0]

    geometry = g.node('GeometryNodeJoinGeometry', [(0, filtered)] + [
        (0, parts[name]) for name in ('Curve', 'Point Cloud', 'Volume', 'Grease Pencil')]).outputs[0]
    transform = g.node('GeometryNodeObjectInfo', [('Object', host)], transform_space='RELATIVE').outputs['Transform']
    world = g.node('GeometryNodeTransform', [('Mode', 'Matrix'), ('Geometry', geometry),
                                             ('Transform', transform)]).outputs[0]
    g.node('NodeGroupOutput', [('Geometry', world)])
