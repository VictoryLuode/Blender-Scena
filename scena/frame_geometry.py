"""Geometry used by viewport frames, in placement-local coordinates."""

import bpy
import numpy as np
from mathutils import Matrix


def vertex_blocks(points):
    """Cache conservative block bounds so screen fitting can skip interior vertices."""
    block_size = 256
    corners = None
    if len(points) >= 8192:
        starts = np.arange(0, len(points), block_size)
        low = np.minimum.reduceat(points, starts, axis=0)
        high = np.maximum.reduceat(points, starts, axis=0)
        axes = np.asarray(tuple((x, y, z) for x in (False, True)
                                for y in (False, True) for z in (False, True)))
        corners = np.where(axes, high[:, None, :], low[:, None, :])
    return points, corners, block_size


def coordinates(values, field):
    points = np.empty((len(values), 3), dtype=np.float32)
    values.foreach_get(field, points.reshape(-1))
    return points


def _reference_parts(ref, graph, parents, read):
    from .picking import _visible_members

    if isinstance(ref, bpy.types.GeometrySet):
        return geometry_parts(ref, graph, parents, read)
    if ref is None or ref in parents:
        return []
    parents = parents | {ref}
    if isinstance(ref, bpy.types.Collection):
        offset = Matrix.Translation(-ref.instance_offset)
        parts = []
        for obj in _visible_members(ref):
            if not obj.hide_viewport:
                world = offset @ obj.evaluated_get(graph).matrix_world
                parts.extend((block, world @ local) for block, local in _reference_parts(obj, graph, parents, read))
        return parts
    obj = ref.evaluated_get(graph)
    if obj.instance_type == 'COLLECTION' and obj.instance_collection:
        return _reference_parts(obj.instance_collection, graph, parents, read)
    if obj.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD'}:
        return geometry_parts(obj.evaluated_geometry(), graph, parents, read)
    return read(None, obj)


def _frame_parts(geometry, obj=None):
    parts = []
    identity = Matrix.Identity(4)
    if geometry is None:
        from .picking import _corners, _local_bounds
        corners = _corners(_local_bounds(obj.bound_box))
        return [(vertex_blocks(np.asarray(corners, dtype=np.float32)), identity)]
    mesh = geometry.mesh
    if mesh is not None and len(mesh.vertices):
        parts.append((vertex_blocks(coordinates(mesh.vertices, "co")), identity))
    for component in (geometry.curves, geometry.pointcloud):
        positions = component.attributes.get("position") if component is not None else None
        if positions is not None and len(positions.data):
            parts.append((vertex_blocks(coordinates(positions.data, "vector")), identity))
    return parts


def geometry_parts(geometry, graph, parents, read):
    """Read each referenced geometry once and retain transforms for repeated instances."""
    parts = read(geometry)
    instances = geometry.instances_pointcloud()
    if instances is None or not len(instances.points):
        return parts
    refs = geometry.instance_references()
    indices = instances.attributes[".reference_index"].data
    transforms = instances.attributes["instance_transform"].data
    resolved = {}
    for index, transform in zip(indices, transforms):
        key = index.value
        if key not in resolved:
            resolved[key] = _reference_parts(refs[key], graph, parents, read)
        matrix = transform.value
        parts.extend((block, matrix @ local) for block, local in resolved[key])
    return parts


def evaluated_frame(obj, graph):
    from .picking import _corners, _local_bounds

    parts = geometry_parts(obj.evaluated_geometry(), graph, {obj.original}, _frame_parts)
    if not parts:
        corners = _corners(_local_bounds(obj.bound_box))
        return corners, [(vertex_blocks(np.asarray(corners, dtype=np.float32)), Matrix.Identity(4))]
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    for (points, _, _), local in parts:
        if not local.is_identity:
            matrix = np.asarray(local, dtype=np.float32)
            points = points @ matrix[:3, :3].T + matrix[:3, 3]
        np.minimum(low, points.min(axis=0), out=low)
        np.maximum(high, points.max(axis=0), out=high)
    return _corners((low, high)), parts
