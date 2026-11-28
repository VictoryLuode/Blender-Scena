"""Preview and select groups and their members in the 3D viewport."""

from dataclasses import dataclass
from itertools import product
from time import monotonic

import bpy
import blf
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from . import editor, hosts, pick_outline, preferences

# Blender's mouse_lmb, mouse_mmb, mouse_mmb_scroll and mouse_rmb SVGs
# from release/datafiles/icons_svg at 02f0899d78e3, in object_icons.ttf.
_MOUSE_GLYPHS = {"LMB": "\ue011", "MMB": "\ue012", "Wheel": "\ue013", "RMB": "\ue014"}


def _pick_geometry(obj, depsgraph):
    """Cache geometry in member space so linked instances reuse it."""
    evaluated = obj.evaluated_get(depsgraph)
    if obj.type not in pick_outline.MESH_TYPES:
        return None, (), ()
    mesh = evaluated.data if obj.type == 'MESH' else evaluated.to_mesh()
    if mesh is None:
        return None, (), ()
    try:
        if mesh.polygons and obj.type == 'MESH':
            return BVHTree.FromObject(obj, depsgraph), (), ()
        coords = [vertex.co.copy() for vertex in mesh.vertices]
        if mesh.polygons:
            mesh.calc_loop_triangles()
            triangles = [tuple(tri.vertices) for tri in mesh.loop_triangles]
            return BVHTree.FromPolygons(coords, triangles, all_triangles=True), (), ()
        return None, coords, [tuple(edge.vertices) for edge in mesh.edges]
    finally:
        if obj.type != 'MESH':
            evaluated.to_mesh_clear()


def _screen_hit(origin, direction, matrix, vertices, edges, screen):
    """Pick a projected wire, point, or the same diamond used for non-mesh objects."""
    if screen is None:
        return None
    region, rv3d, cursor = screen
    world = [matrix @ v for v in vertices] or [matrix.translation]
    points = [view3d_utils.location_3d_to_region_2d(region, rv3d, v) for v in world]
    if not vertices:
        pt = points[0]
        if pt and abs(pt.x - cursor.x) + abs(pt.y - cursor.y) <= 7:
            return (world[0] - origin).dot(direction)
        return None
    nearest = None
    for i, j in edges or ((i, i) for i in range(len(points))):
        a, b = points[i], points[j]
        if a is None or b is None:
            continue
        delta = b - a
        t = max(0.0, min(1.0, (cursor - a).dot(delta) / delta.length_squared)) if delta.length_squared else 0.0
        if (a + t * delta - cursor).length_squared <= 4:
            depth = (world[i].lerp(world[j], t) - origin).dot(direction)
            if depth >= 0 and (nearest is None or depth < nearest):
                nearest = depth
    return nearest


def _corners(bounds):
    low, high = bounds
    return [Vector(xyz) for xyz in product(*zip(low, high))]


def _box_distance(origin, direction, transform, bounds):
    """Intersect in box space, keeping the ray parameter in world units."""
    try:
        inverse = transform.inverted()
    except ValueError:
        return None
    start = inverse @ origin
    delta = inverse.to_3x3() @ direction
    near, far = 0.0, float('inf')
    for axis, (low, high) in enumerate(zip(*bounds)):
        if abs(delta[axis]) < 1e-12:
            if not low - 1e-6 <= start[axis] <= high + 1e-6:
                return None
            continue
        first = (low - start[axis]) / delta[axis]
        last = (high - start[axis]) / delta[axis]
        near = max(near, min(first, last))
        far = min(far, max(first, last))
        if far + 1e-6 < near:
            return None
    return near


@dataclass
class _Shape:
    bounds: tuple
    children: list
    leaves: list
    frame_depth: int


@dataclass
class _Hit:
    path: tuple
    matrix: Matrix
    bounds: tuple
    distance: float


@dataclass
class _MemberHit:
    path: tuple
    obj: object
    matrix: Matrix
    bounds: tuple
    distance: float


def _local_bounds(corners):
    if not corners or all(tuple(corner) == (-1.0, -1.0, -1.0) for corner in corners):
        corners = [Vector((0, 0, 0))]
    return tuple(Vector(f(axis) for axis in zip(*corners)) for f in (min, max))


def _surface_parts(geometry, _obj=None):
    mesh = geometry.mesh if geometry is not None else None
    if mesh is None or not mesh.polygons:
        return []
    mesh.calc_loop_triangles()
    tree = BVHTree.FromPolygons([v.co for v in mesh.vertices],
                                [tri.vertices[:] for tri in mesh.loop_triangles], all_triangles=True)
    return [(tree, Matrix.Identity(4))]


def _surface_distance(tree, matrix, origin, direction):
    try:
        inverse = matrix.inverted()
    except ValueError:
        return None
    point, _, _, _ = tree.ray_cast(inverse @ origin, inverse.to_3x3() @ direction)
    return (matrix @ point - origin).dot(direction) if point is not None else None


class _PickIndex:
    @classmethod
    def from_view(cls, context):
        index = cls(context.visible_objects, context.evaluated_depsgraph_get(), include_loose=True)
        route = tuple(editor.path(context))
        if not route:
            return index
        routes = [route[:i] for i in range(1, len(route) + 1)]
        routes.extend(tuple(editor._jump_path(context, host)) for host in editor.peers(context))
        by_host = {path[-1]: path for path in routes}
        for path in routes:
            host = path[-1]
            source = hosts.definition(host)
            if editor.edit_layer.open_host(source) == host:
                for obj in _visible_members(editor.content(source)):
                    index._parents[obj] = path
            shape = index._shape(source, {source})
            matrix = host.evaluated_get(index._depsgraph).matrix_world.copy()
            index._open_frames[path] = (matrix, shape.bounds)
        for obj in context.visible_objects:
            if obj.get("_gm_mirror_of") and (host := obj.get("_gm_mirror_host")) in by_host:
                index._parents[obj] = by_host[host]
        return index

    def __init__(self, objects, depsgraph, include_loose=False, *, modifier_surfaces=True):
        self._depsgraph = depsgraph
        self._shapes = {}
        self._geometry = {}
        self._modifier_surfaces = modifier_surfaces
        self._surface_boxes = {}
        self._surfaces = {}
        self._parents = {}
        self._open_frames = {}
        self._roots = []
        self._loose = []
        self._root_spheres = None
        self._loose_spheres = None
        for obj in objects:
            if editor.is_helper(obj) or obj.hide_select:
                continue
            source = hosts.definition(obj)
            if source and not source.hide_viewport:
                matrix = obj.evaluated_get(depsgraph).matrix_world.copy()
                self._roots.append((obj, source, matrix))
            elif include_loose and not obj.hide_viewport:
                evaluated = obj.evaluated_get(depsgraph)
                corners = evaluated.bound_box
                low, high = corners[0], corners[6]
                if tuple(low) == tuple(high) == (-1.0, -1.0, -1.0):
                    low = high = (0.0, 0.0, 0.0)
                bounds = Vector(low), Vector(high)
                self._loose.append((obj, evaluated.matrix_world.copy(), bounds))

    def prepare_hover(self):
        """Build a broad phase once for the repeated rays of a modal Pick."""
        if len(self._roots) >= 64:
            self._root_spheres = self._make_spheres(
                (matrix, self._surface_bounds(obj) or self._shape(source, {source}).bounds)
                for obj, source, matrix in self._roots)
        if len(self._loose) >= 64:
            self._loose_spheres = self._make_spheres((matrix, bounds) for _, matrix, bounds in self._loose)

    @staticmethod
    def _make_spheres(boxes):
        import numpy as np

        centers, radii = [], []
        for matrix, (low, high) in boxes:
            center = matrix @ ((low + high) * 0.5)
            centers.append(tuple(center))
            # Sum of transformed half-axis lengths also bounds sheared boxes.
            half = (high - low) * 0.5
            radius = sum(half[i] * matrix.col[i].to_3d().length for i in range(3))
            # Point markers have a screen-space pick radius at any zoom.
            radii.append(float('inf') if low == high else radius)
        return np.asarray(centers), np.asarray(radii)

    @staticmethod
    def _ray_candidates(items, spheres, origin, direction):
        if spheres is None:
            return items
        import numpy as np

        centers, radii = spheres
        relative = centers - origin[:]
        unit = direction.normalized()
        if not unit.length_squared:
            return items
        perpendicular = np.cross(relative, unit[:])
        squared = np.einsum('ij,ij->i', perpendicular, perpendicular)
        # Leave room for float32 rounding in Blender's world transforms and rays.
        tolerance = 1e-5 * (1 + np.max(np.abs(relative), axis=1) + max(map(abs, origin)))
        indices = np.flatnonzero(squared <= (radii + tolerance) ** 2)
        return (items[i] for i in indices)

    def _surface_bounds(self, obj):
        if not self._modifier_surfaces:
            return None
        if obj not in self._surface_boxes:
            bounds = None
            if hosts.has_viewport_modifiers(obj):
                evaluated = obj.evaluated_get(self._depsgraph)
                corners = evaluated.bound_box
                geometry = evaluated.evaluated_geometry()
                instances = geometry.instances_pointcloud()
                if instances is not None and len(instances.points):
                    # Object.bound_box can describe just the mesh component (zero for pure instances).
                    corners, _ = editor._local_modifier_frame(obj, self._depsgraph)
                bounds = _local_bounds(corners)
            self._surface_boxes[obj] = bounds
        return self._surface_boxes[obj]

    def _surface_hit(self, obj, matrix, origin, direction):
        if obj not in self._surfaces:
            from . import frame_geometry
            evaluated = obj.evaluated_get(self._depsgraph)
            geometry = evaluated.evaluated_geometry()
            instances = geometry.instances_pointcloud()
            if evaluated.data.polygons and (instances is None or not len(instances.points)):
                parts = [(BVHTree.FromObject(obj, self._depsgraph), Matrix.Identity(4))]
            else:
                parts = frame_geometry.geometry_parts(geometry, self._depsgraph, {obj}, _surface_parts)
            self._surfaces[obj] = parts
        return min((distance for tree, local in self._surfaces[obj]
                    if (distance := _surface_distance(tree, matrix @ local, origin, direction)) is not None),
                   default=None)

    def _shape(self, source, parents):
        if source in self._shapes:
            return self._shapes[source]
        points = []
        children = []
        leaves = []
        frame_depth = 0
        offset = Matrix.Translation(-source.instance_offset)
        actual = editor.content(source)
        if actual != source:
            offset = offset @ editor.content_frame(source).inverted()
        for obj in _visible_members(actual):
            if editor.is_helper(obj) or obj.hide_viewport:
                continue
            evaluated = obj.evaluated_get(self._depsgraph)
            matrix = offset @ evaluated.matrix_world
            child = hosts.definition(obj)
            if child:
                if child in parents or child.hide_viewport:
                    continue
                nested = self._shape(child, parents | {child})
                frame_depth = max(frame_depth, 1 + nested.frame_depth)
                box = _corners(self._surface_bounds(obj) or nested.bounds)
                if not obj.hide_select:
                    children.append((obj, child, matrix))
            else:
                frame_depth = max(frame_depth, 1)
                box = [Vector(corner) for corner in evaluated.bound_box]
                if not box or all(tuple(corner) == (-1.0, -1.0, -1.0) for corner in box):
                    box = [Vector((0, 0, 0))]
                if not obj.hide_select:
                    leaves.append((obj, matrix.copy(), _local_bounds(box)))
            points.extend(matrix @ corner for corner in box)
        if points:
            bounds = tuple(Vector(f(axis) for axis in zip(*points)) for f in (min, max))
        else:
            bounds = (Vector((-0.1,) * 3), Vector((0.1,) * 3))
        shape = _Shape(bounds, children, leaves, frame_depth)
        self._shapes[source] = shape
        return shape

    def at_cursor(self, region, rv3d, event):
        xy = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
        if not (0 <= xy.x < region.width and 0 <= xy.y < region.height):
            return [], []
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, xy)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)
        return self.cast(origin, direction, screen=(region, rv3d, xy))

    def cast(self, origin, direction, *, include_members=True, screen=None):
        """Collect both previews in one traversal of the visible group hierarchy."""
        groups = []
        members = []

        def consider(path, obj, matrix, bounds):
            if _box_distance(origin, direction, matrix, bounds) is None:
                # Also test the small screen-space marker for point-like objects.
                if bounds[0] != bounds[1] or screen is None:
                    return
            if obj not in self._geometry:
                self._geometry[obj] = _pick_geometry(obj, self._depsgraph)
            tree, vertices, edges = self._geometry[obj]
            if tree is None:
                distance = _screen_hit(origin, direction, matrix, vertices, edges, screen)
            else:
                distance = _surface_distance(tree, matrix, origin, direction)
            if distance is not None:
                target = obj.get("_gm_mirror_of") or obj
                members.append(_MemberHit(path, target, matrix, bounds, distance))

        for obj, matrix, bounds in self._ray_candidates(self._loose, self._loose_spheres, origin, direction):
            consider(self._parents.get(obj, ()), obj, matrix, bounds)

        def visit(path, obj, source, matrix, seen):
            shape = self._shape(source, seen)
            surface = self._surface_bounds(obj)
            bounds = surface or shape.bounds
            distance = _box_distance(origin, direction, matrix, bounds)
            if distance is None and not (screen and bounds[0] == bounds[1]):
                return
            start = len(members)
            group_start = len(groups)
            if include_members:
                for leaf, local, leaf_bounds in shape.leaves:
                    consider(path, leaf, matrix @ local, leaf_bounds)
            for child_obj, child, local in shape.children:
                if child not in seen:
                    visit((*path, child_obj), child_obj, child, matrix @ local, seen | {child})
            if include_members and surface is not None:
                depth = self._surface_hit(obj, matrix, origin, direction)
                if depth is not None:
                    tolerance = max(1e-5, abs(depth) * 1e-6)
                    # Surfaces removed by a boolean or moved by a deformer are no longer pickable.
                    matched = []
                    front_member = False
                    for hit in members[start:]:
                        if abs(hit.distance - depth) <= tolerance:
                            front_member = True
                            matched.append(hit)
                        elif hit.distance > depth:
                            # Preserve overlap cycling for member surfaces still present behind the first.
                            probe = origin + direction * (hit.distance - 2 * tolerance)
                            behind = self._surface_hit(obj, matrix, probe, direction)
                            if behind is not None and abs(behind - 2 * tolerance) <= tolerance:
                                matched.append(hit)
                    if not front_member:
                        matched.append(_MemberHit(path, path[-1], matrix, bounds, depth))
                else:
                    # Wire and point markers can be picked in screen space.
                    matched = [h for h in members[start:] if h.obj in self._geometry and self._geometry[h.obj][0] is None]
                members[start:] = matched
                nested_paths = {h.path[:i] for h in matched for i in range(len(path) + 1, len(h.path) + 1)}
                groups[group_start:] = [h for h in groups[group_start:] if h.path in nested_paths]
            if include_members:
                distance = min((members[i].distance for i in range(start, len(members))), default=None)
            if distance is not None:
                groups.append(_Hit(path, matrix, bounds, distance))

        for obj, source, matrix in self._ray_candidates(self._roots, self._root_spheres, origin, direction):
            path = (*self._parents.get(obj, ()), obj.get("_gm_mirror_of") or obj)
            visit(path, obj, source, matrix, {source})
        if self._open_frames:
            distances = {}
            for member in members:
                for i in range(1, len(member.path) + 1):
                    path = member.path[:i]
                    if path in self._open_frames:
                        distances[path] = min(distances.get(path, member.distance), member.distance)
            for path, distance in distances.items():
                matrix, bounds = self._open_frames[path]
                groups.append(_Hit(path, matrix, bounds, distance))
        groups.sort(key=lambda hit: (hit.distance, tuple(obj.name_full for obj in hit.path)))
        members.sort(key=lambda hit: (hit.distance, hit.obj.name_full, tuple(obj.name_full for obj in hit.path)))
        return groups, members


def _visible_members(source):
    """A member can remain visible through a second collection link."""
    pending = [source]
    visited = set()
    members = set()
    while pending:
        collection = pending.pop()
        if collection in visited or collection.hide_viewport:
            continue
        visited.add(collection)
        pending.extend(collection.children)
        for obj in collection.objects:
            if obj not in members:
                members.add(obj)
                yield obj


def _right_click_cancel(operator, context, event):
    """Right-click leaves the pick tool and is not passed on to the viewport menu."""
    if event.type != 'RIGHTMOUSE':
        return None
    if event.value == 'PRESS':
        operator.cancel(context)
        operator._right_done = True
    if event.value == 'RELEASE':
        if not getattr(operator, '_right_done', False):
            operator.cancel(context)
        return {'CANCELLED'}
    return {'RUNNING_MODAL'}


def _pick_chain(hits, chosen):
    """Every group level from the outside in, up to the level under the cursor."""
    if chosen is None:
        return []
    chain = [hit for hit in hits if chosen.path[:len(hit.path)] == hit.path]
    chain.sort(key=lambda hit: len(hit.path))
    return chain


class PickFrames:
    """Merge hover highlights into the ordinary frame pass for this viewport."""
    def __init__(self, group, member):
        self.targets = {}
        self.group = group.path[-1] if group else None
        self.member = member.obj if member else None
        for obj, hit in ((self.member, member), (self.group, group)):
            if hit:
                corners = [hit.matrix @ point for point in _corners(hit.bounds)]
                self.targets[obj] = (hit.matrix, corners)
        self.drawn = set()

    def matches(self, host, corners):
        owner = host.get("_gm_mirror_of") or host
        target = self.targets.get(owner)
        if target is None:
            return False
        matrix, hovered = target
        # Shared group members can have a frame at more than one world position.
        same_box = all((a - b).length_squared < 1e-8 for a, b in zip(corners, hovered))
        if not same_box and not (owner == self.member and host.matrix_world == matrix):
            return False
        self.drawn.add(owner)
        return True

    def draw_missing(self, context, style, labels):
        if self.member and self.group and self.member != self.group:
            labels.parents.setdefault(self.member, self.group)
        for host, (matrix, corners) in self.targets.items():
            if host in self.drawn:
                continue
            editor._draw_screen_box(context.region, context.region_data, corners, pick_outline.COLOR[:3],
                                    host, host.name, True, False, style, context.preferences.system.ui_scale,
                                    [], True, placement=matrix, labels=labels)


class GROUPMANAGER_OT_pick(bpy.types.Operator):
    bl_idname = "groupmanager.pick"
    bl_label = "Pick"
    bl_description = "Left-click to select the highlighted object, or middle-click to select its group"

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.area and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        self._area = context.area
        self._region = next((r for r in self._area.regions if r.type == 'WINDOW'
                             and r.x <= event.mouse_x < r.x + r.width
                             and r.y <= event.mouse_y < r.y + r.height), None)
        if self._region is None:
            self._region = next(r for r in self._area.regions if r.type == 'WINDOW')
        self._index = _PickIndex.from_view(context)
        self._index.prepare_hover()
        self._depth = 1
        self._choice = 0
        self._hits = []
        self._members = []
        self._signature = None
        self._outline = None
        self._middle_press = None
        self._shift_press = None
        self._click_timer = None
        self._pending_object = None
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), 'WINDOW', 'POST_PIXEL')
        self._refresh(context, event)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _refresh(self, context, event):
        region = self._region
        rv3d = region.data or context.space_data.region_3d
        self._hits, self._members = self._index.at_cursor(region, rv3d, event)
        signature = (tuple(hit.path for hit in self._hits),
                     tuple((hit.path, hit.obj) for hit in self._members))
        if signature != self._signature:
            self._signature = signature
            self._choice = 0
            self._depth = next((len(hit.path) for hit in self._members if hit.path),
                               max((len(hit.path) for hit in self._hits), default=1))
        self._update_choice()

    def _update_choice(self):
        maximum = max((len(hit.path) for hit in self._hits), default=1)
        self._depth = max(1, min(self._depth, maximum))
        groups = {hit.path: hit for hit in self._hits if len(hit.path) == self._depth}
        self._options = []
        covered = set()
        for member in self._members:
            if not member.path:
                self._options.append((None, member))
            elif len(member.path) >= self._depth:
                path = member.path[:self._depth]
                self._options.append((groups[path], member))
                covered.add(path)
        self._options.extend((group, None) for path, group in groups.items() if path not in covered)
        self._choice %= max(len(self._options), 1)
        self._hit, self._member = self._options[self._choice] if self._options else (None, None)
        pointer = self._region.as_pointer()
        if self._hit or self._member:
            editor._pick_frames[pointer] = PickFrames(self._hit, self._member)
        else:
            editor._pick_frames.pop(pointer, None)
        self._area.tag_redraw()

    def _draw(self):
        context = bpy.context
        if context.area != self._area or context.region != self._region:
            return
        region = self._region
        rv3d = region.data or context.region_data
        if rv3d is None:
            return
        style = editor._bounds_style(context, preview=True)
        if self._member:
            width = style.width if style else preferences.get(context).bounds_width
            self._draw_member(rv3d, width)
        self._draw_hints(context)

    def _draw_member(self, rv3d, width):
        hit = self._member
        if self._outline is None:
            self._outline = pick_outline.PickOutline()
        size = (self._region.width, self._region.height)
        if not self._outline.draw(hit.obj, self._index._depsgraph, hit.matrix, rv3d.perspective_matrix, size, width):
            # Lights, empties and other objects without a surface get a flat marker.
            point = view3d_utils.location_3d_to_region_2d(self._region, rv3d, hit.matrix.translation)
            if point:
                x, y = point
                vertices = [(x, y+6, 0), (x+6, y, 0), (x, y-6, 0), (x-6, y, 0), (x, y+6, 0)]
                shader = editor._polyline_shader()
                shader.bind()
                shader.uniform_float("color", pick_outline.COLOR)
                shader.uniform_float("viewportSize", size)
                shader.uniform_float("lineWidth", width)
                batch_for_shader(shader, 'LINE_STRIP', {"pos": vertices}).draw(shader)
    def _draw_hints(self, context):
        region = self._region
        scale = context.preferences.system.ui_scale
        margin = round(10 * scale)
        right, bottom, left = region.width, 0, 0
        for other in self._area.regions:
            if other.width <= 1 or other.height <= 1:
                continue
            if other.type == 'TOOLS' and other.x == region.x:
                left = other.width
            elif other.type in {'ASSET_SHELF', 'ASSET_SHELF_HEADER', 'HEADER'} and other.alignment == 'BOTTOM':
                bottom = max(bottom, other.y + other.height - region.y)
        right -= margin
        width = min(right - left - margin, round(300 * scale))
        blf.size(0, round(10 * scale))
        icon_font = editor._icon_font
        if icon_font >= 0:
            blf.size(icon_font, round(11 * scale))
        step = ("", "LMB", "×2  One Level")
        deep = ("Shift+", "LMB", "×2  Deep / Exit")
        hints = ((("", "LMB", "Object"), ("", "MMB", "Group")), (step, deep),
                 (("", "Wheel", "Level"), ("Shift+", "Wheel", "Overlap"), ("Esc / ", "RMB", "Cancel")))
        lines = []
        gap = blf.dimensions(0, "   ")[0]
        for row in hints:
            runs, used = [], 0
            for prefix, button, action in row:
                icon = (icon_font, _MOUSE_GLYPHS[button]) if icon_font >= 0 else (0, button)
                parts = [(0, prefix), icon, (0, " " + action)]
                hint_width = sum(blf.dimensions(font, value)[0] for font, value in parts)
                if runs and used + gap + hint_width > width:
                    lines.append((runs, used))
                    runs, used = [], 0
                if runs:
                    runs.append((0, "   "))
                    used += gap
                runs.extend(parts)
                used += hint_width
            lines.append((runs, used))
        if self._member:
            chain = self._member.path
            if not chain or chain[-1] != self._member.obj:
                chain = (*chain, self._member.obj)
            path = [obj.name for obj in chain]
            suffix = f"   [{self._choice + 1}/{len(self._options)}]" if len(self._options) > 1 else ""
            label = ' › '.join(path) + suffix
            while len(path) > 1 and blf.dimensions(0, label)[0] > width:
                path.pop(0)
                label = '… › ' + ' › '.join(path) + suffix
            name = path[-1]
            while name and blf.dimensions(0, label)[0] > width:
                name = name[1:]
                label = '…' + name + suffix
            lines.insert(0, ([(0, label)], blf.dimensions(0, label)[0]))
        fonts = (0, icon_font) if icon_font >= 0 else (0,)
        for font in fonts:
            blf.color(font, *pick_outline.COLOR)
            blf.enable(font, blf.SHADOW)
            blf.shadow(font, 3, 0, 0, 0, 0.9)
        try:
            for i, (runs, used) in enumerate(reversed(lines)):
                x = max(left + margin, right - used)
                y = bottom + margin + i * round(14 * scale)
                for font, value in runs:
                    blf.position(font, x, y - round(scale) if font == icon_font else y, 0)
                    blf.draw(font, value)
                    x += blf.dimensions(font, value)[0]
        finally:
            for font in fonts:
                blf.disable(font, blf.SHADOW)

    def cancel(self, context):
        editor._pick_frames.pop(self._region.as_pointer(), None)
        if self._click_timer is not None:
            context.window_manager.event_timer_remove(self._click_timer)
            self._click_timer = None
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        self._outline = None
        self._area.tag_redraw()

    def modal(self, context, event):
        if event.type in {'ESC', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        handled = _right_click_cancel(self, context, event)
        if handled is not None:
            return handled
        if event.type == 'TIMER' and self._pending_object and monotonic() >= self._pending_object[0]:
            member = self._pending_object[1]
            self.cancel(context)
            try:
                _select_picked(context, member.path, member.obj)
            except (ValueError, RuntimeError) as exc:
                self.report({'WARNING'}, str(exc))
                return {'CANCELLED'}
            return {'FINISHED'}
        if (event.type == 'LEFTMOUSE' and not (event.alt or event.ctrl or event.oskey)
                and (event.shift or editor._click_objects(context))):
            if event.value in {'PRESS', 'DOUBLE_CLICK'}:
                if editor._view3d_under_mouse(event) != self._region:
                    self._shift_press = None
                    return {'RUNNING_MODAL'}
                # Blender passes double clicks to Python modal handlers as a second PRESS.
                now = monotonic()
                pos = Vector((event.mouse_x, event.mouse_y))
                last = self._shift_press
                self._shift_press = now, pos, event.shift
                self._refresh(context, event)
                if event.value == 'DOUBLE_CLICK' or (last and last[2] == event.shift
                        and now - last[0] <= editor._click_window(context)
                        and (pos - last[1]).length <= context.preferences.inputs.drag_threshold_mouse):
                    self.cancel(context)
                    return _navigate_hit(self, context, self._member, event.shift)
                self._pending_object = None
                if not event.shift and self._member:
                    delay = editor._click_window(context)
                    self._pending_object = now + delay, self._member
                    if self._click_timer is None:
                        self._click_timer = context.window_manager.event_timer_add(0.05, window=context.window)
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS':
            self._shift_press = None
            self._pending_object = None
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if self._middle_press is not None:
                start, _ = self._middle_press
                delta = Vector((event.mouse_x, event.mouse_y)) - start
                if delta.length > context.preferences.inputs.drag_threshold_mouse:
                    self._middle_press = (start, None)
            self._refresh(context, event)
        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS':
            step = 1 if event.type == 'WHEELUPMOUSE' else -1
            if event.shift:
                self._choice += step
            else:
                self._depth += step
                self._choice = 0
            self._update_choice()
        elif event.type == 'MIDDLEMOUSE' and event.value == 'PRESS':
            if event.shift or event.ctrl or event.alt:
                return {'PASS_THROUGH'}
            self._refresh(context, event)
            if self._hit is None:
                return {'PASS_THROUGH'}
            self._middle_press = (Vector((event.mouse_x, event.mouse_y)), self._hit.path)
        elif (event.type, event.value) in {('LEFTMOUSE', 'PRESS'), ('MIDDLEMOUSE', 'RELEASE')}:
            self._refresh(context, event)
            if event.type == 'LEFTMOUSE' and self._member:
                groups, target = self._member.path, self._member.obj
            elif event.type == 'MIDDLEMOUSE':
                press, self._middle_press = self._middle_press, None
                if press is None:
                    return {'PASS_THROUGH'}
                if not self._hit or press[1] != self._hit.path:
                    return {'RUNNING_MODAL'}
                groups, target = self._hit.path[:-1], self._hit.path[-1]
            else:
                return {'RUNNING_MODAL'}
            self.cancel(context)
            try:
                _select_picked(context, groups, target)
            except (RuntimeError, ValueError) as exc:
                self.report({'WARNING'}, str(exc))
                return {'CANCELLED'}
            return {'FINISHED'}
        elif event.type == 'MIDDLEMOUSE' or event.type.startswith('NDOF'):
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


def _select_picked(context, groups, obj):
    """Open every group around this object, then select the object itself."""
    if groups and groups[-1] == obj:
        groups = groups[:-1]
    if groups != tuple(editor.path(context)):
        if groups:
            editor.close_peers(context)
            editor.open_path(context, groups)
        else:
            editor.back(context, all_levels=True)
    layer = context.view_layer
    layer.update()
    if layer.objects.get(obj.name) != obj:
        raise ValueError(f'"{obj.name}" is not in this view')
    if obj.hide_get(view_layer=layer):
        obj.hide_set(False, view_layer=layer)
    editor._select(layer, obj)


def _navigate_hit(operator, context, member, deep):
    try:
        route = tuple(editor.path(context))
        target_path = member.path if member else ()
        if deep and route and target_path[:len(route)] != route:
            editor.back(context, all_levels=True)
        elif member and member.path:
            if deep or route == member.path:
                destination = member.path
            else:
                common = 0
                for current, target in zip(route, member.path):
                    if current != target:
                        break
                    common += 1
                destination = member.path[:common + 1]
            child = member.path[len(destination)] if len(destination) < len(member.path) else member.obj
            if child != destination[-1]:
                _select_picked(context, destination, child)
            elif route != destination:
                editor.close_peers(context)
                editor.open_path(context, destination)
        elif route:
            editor.back(context, all_levels=deep)
        else:
            return {'CANCELLED'}
    except (ValueError, RuntimeError) as exc:
        operator.report({'WARNING'}, str(exc))
        return {'CANCELLED'}
    return {'FINISHED'}


class GROUPMANAGER_OT_select_front_member(bpy.types.Operator):
    bl_idname = "groupmanager.select_front_member"
    bl_label = "Select Visible Group Member"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.area and context.area.type == 'VIEW_3D'
                and context.region and context.region.type == 'WINDOW' and bool(editor.path(context)))

    def invoke(self, context, event):
        region = context.region
        space = context.space_data
        tool = context.workspace.tools.from_space_view3d_mode('OBJECT', create=False)
        if (not tool or tool.idname not in {'builtin.select', 'builtin.select_box', 'builtin.select_lasso'}
                or editor._view3d_under_mouse(event) != region):
            return {'PASS_THROUGH'}
        shading = space.shading
        if ((shading.type == 'WIREFRAME' and shading.show_xray_wireframe)
                or (shading.type == 'SOLID' and shading.show_xray)):
            return {'PASS_THROUGH'}
        route = tuple(editor.path(context))
        index = _PickIndex.from_view(context)
        _, hits = index.at_cursor(region, region.data or context.region_data, event)
        if len(hits) < 2 or hits[0].path[:len(route)] != route:
            return {'PASS_THROUGH'}
        front = hits[0]
        target = front.path[len(route)] if len(front.path) > len(route) else front.obj
        editor._select(context.view_layer, target)
        return {'FINISHED'}


class GROUPMANAGER_OT_deep_edit(bpy.types.Operator):
    bl_idname = "groupmanager.deep_edit"
    bl_label = "Navigate Group"
    bl_description = "Ctrl-double-click to enter one group. Outside the current group, Ctrl-click goes back one level and Ctrl-double-click exits all levels"
    bl_options = {'UNDO'}
    deep: bpy.props.BoolProperty(name="Deepest Level", default=True)

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.area and context.area.type == 'VIEW_3D'
                and context.region and context.region.type == 'WINDOW')

    def invoke(self, context, event):
        region = context.region
        if editor._view3d_under_mouse(event) != region or (not self.deep and not editor._click_objects(context)):
            return {'PASS_THROUGH'}
        index = _PickIndex.from_view(context)
        _, members = index.at_cursor(region, region.data or context.region_data, event)
        route = tuple(editor.path(context))
        member = members[0] if members else None
        if event.value == 'DOUBLE_CLICK' or self.deep:
            if not route and not (member and member.path):
                return {'PASS_THROUGH'}
            return self._double_click(context, route, member)
        if not route and not (member and member.path):
            if member and not member.path:
                editor._select(context.view_layer, member.obj)
                return {'FINISHED'}
            return {'PASS_THROUGH'}
        # Keep the first target until the double-click window expires.
        self._route = route
        self._member = member
        self._position = Vector((event.mouse_x, event.mouse_y))
        self._deadline = monotonic() + editor._click_window(context)
        self._timer = context.window_manager.event_timer_add(0.04, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)

    def _double_click(self, context, route, member):
        outside = bool(route and (not member or member.path[:len(route)] != route))
        return _navigate_hit(self, context, member, self.deep or outside)

    def modal(self, context, event):
        if event.type in {'ESC', 'WINDOW_DEACTIVATE'} or tuple(editor.path(context)) != self._route:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'DOUBLE_CLICK'}:
            pos = Vector((event.mouse_x, event.mouse_y))
            if (event.ctrl and not (event.shift or event.alt or event.oskey)
                    and monotonic() <= self._deadline and editor._view3d_under_mouse(event) == context.region
                    and (pos - self._position).length <= context.preferences.inputs.drag_threshold_mouse):
                self.cancel(context)
                return self._double_click(context, self._route, self._member)
        elif event.type != 'TIMER' or monotonic() < self._deadline:
            return {'PASS_THROUGH'}
        self.cancel(context)
        member = self._member
        entering = member and member.path[:len(self._route)] == self._route and len(member.path) > len(self._route)
        result = _navigate_hit(self, context, None, False) if self._route and not entering else {'CANCELLED'}
        return result if event.type == 'TIMER' else result | {'PASS_THROUGH'}


CLASSES = (GROUPMANAGER_OT_pick, GROUPMANAGER_OT_deep_edit, GROUPMANAGER_OT_select_front_member)
