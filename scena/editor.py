"""Edit definition members in-place in the original Blender scene."""

from contextlib import contextmanager
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, IntProperty
from mathutils import Matrix

from . import branches, hosts, menu_hooks
from .edit_layer import content, content_frame, is_helper
from . import edit_layer


_ROUTE = "_gm_world_edit_path"
_SOURCES = "_gm_world_edit_sources"
_PEERS = "_gm_world_edit_peers"
_LAYER = "_gm_world_edit_view_layer"
_MESH_EDIT = "_scena_mesh_edit"
_PREV_COLLECTION = "_gm_before_edit_collection"
_suspended = set()
_shape_index = None
_frame_cache = {}
_extent_cache = {}
_line_shader = None
_LEVEL_MEMBER_COLOR = (0.33, 0.64, 1.0)
# Private-use glyphs from Blender's outliner_ob_* SVGs, in font order.
_icon_path = str(Path(__file__).with_name("object_icons.ttf"))
_icon_font = -1
_collection_icon = chr(0xE010)
_type_icons = {kind: chr(0xE000 + index) for index, kind in enumerate((
    'ARMATURE', 'CAMERA', 'CURVE', 'CURVES', 'EMPTY', 'FONT', 'GREASEPENCIL', 'LATTICE',
    'LIGHT', 'LIGHT_PROBE', 'MESH', 'META', 'POINTCLOUD', 'SPEAKER', 'SURFACE', 'VOLUME',
))}


def _drop_bounds():
    """Drop world-space frame corners. The next draw builds one shared shape index."""
    global _shape_index
    _shape_index = None
    _frame_cache.clear()
    _extent_cache.clear()


def _shapes(context):
    """One shape index per depsgraph update, shared by every frame on screen."""
    global _shape_index
    if _shape_index is None:
        from .picking import _PickIndex
        _shape_index = _PickIndex([], context.evaluated_depsgraph_get())
    return _shape_index


def _placement_matrix(host, source, graph):
    if host == edit_layer.open_host(source):
        return content_frame(source) @ Matrix.Translation(source.instance_offset)
    return host.evaluated_get(graph).matrix_world.copy()
_overlay_buttons = {}
_pick_frames = {}
_open_frame_rects = {}
_controls_running = False
_draw_handle = None
_dismissed_preview = None
_mesh_session_opened_group = False
_pending_object_click = None
_label_focus = {}
_pending_name_click = None
_pending_empty_clicks = None
_empty_click_window = None
_pending_group_clicks = None
_pending_level_clicks = None


def _click_names(context):
    from . import preferences
    prefs = preferences.get(context)
    return prefs is None or prefs.click_names


def _click_objects(context):
    from . import preferences
    prefs = preferences.get(context)
    return prefs is None or prefs.click_objects


def _read(scene, key):
    values = scene.get(key)
    return [values.get(str(i)) for i in range(len(values))] if values is not None else []


def _write(scene, key, items):
    if items:
        scene[key] = {str(i): obj for i, obj in enumerate(items)}
    elif key in scene:
        del scene[key]


def path(context):
    return _read(context.scene, _ROUTE) if context.scene else []


def peers(context):
    """Other groups open beside the active one. The active group is the last item of path()."""
    return _read(context.scene, _PEERS) if context.scene else []


def frame(context):
    sources = _read(context.scene, _SOURCES)
    return content_frame(sources[-1]) if sources else Matrix.Identity(4)


def _owns(host, obj):
    source = hosts.definition(host) if host else None
    return bool(obj and source and not is_helper(obj) and content(source).all_objects.get(obj.name) == obj)


def in_scope(context, obj):
    route = path(context)
    if not route:
        return True
    return _owns(route[-1], obj)


def owner_host(context, obj):
    """Open group that contains this object, if it is one of the groups being edited."""
    route = path(context)
    if not route or obj is None:
        return None
    for host in (route[-1], *peers(context)):
        if obj == host or _owns(host, obj):
            return host
    return None


def join_host(context, host):
    """Add a group to the current edit and leave the active group unchanged."""
    global _dismissed_preview
    route = path(context)
    if host is None or not route or host == route[-1] or host in peers(context):
        return
    open_peer(context, host)
    _dismissed_preview = None
    _redraw()


def _chain_to_group(context, host):
    """Placement path from the current edit down to a group nested inside it."""
    route = path(context)
    if not route:
        return [host]

    def search(source, chain, seen):
        for obj in content(source).all_objects:
            if obj is None or edit_layer.is_helper(obj):
                continue
            if obj == host:
                return chain + [obj]
            nested = hosts.definition(obj)
            if nested and nested not in seen and not nested.library and not nested.override_library:
                found = search(nested, chain + [obj], seen | {nested})
                if found:
                    return found
        return None

    for base in (route[-1], *peers(context)):
        source = hosts.definition(base)
        if not source:
            continue
        prefix = list(route) if base == route[-1] else route[:-1] + [base]
        found = search(source, prefix, {source})
        if found:
            return found
    return None


def _click_window(context):
    """Match Blender's own double-click interval."""
    inputs = getattr(getattr(context, "preferences", None), "inputs", None)
    milliseconds = getattr(inputs, "mouse_double_click_time", 350) or 350
    return max(0.2, float(milliseconds) / 1000.0)


def _handle_name_press(context, host, location=None):
    """Select on the first press and navigate on the second press at the same label."""
    global _pending_name_click
    from time import perf_counter
    now = perf_counter()
    last = _pending_name_click
    if last and now - last[0] <= _click_window(context):
        same = location == last[2] if location is None or last[2] is None else (
            location[0] == last[2][0] and
            max(abs(location[i] - last[2][i]) for i in (1, 2)) <= context.preferences.inputs.drag_threshold_mouse)
        if same and (location is not None or host == last[1]):
            _pending_name_click = None
            _enter_named_group(context, last[1])
            return True
    _pending_name_click = None
    layer = context.view_layer
    _label_focus.pop(layer.as_pointer(), None)
    if host is None:
        return False
    route = path(context)
    if route and (host == route[-1] or host in peers(context)):
        selectable = set(context.selectable_objects)
        members = [obj for obj in edit_layer.displayed_members(hosts.definition(host), host)
                   if obj in selectable]
        _select(layer, None)
        for obj in members:
            obj.select_set(True, view_layer=layer)
        # The open Group's carrier is hidden. Keep its focus in the overlay while
        # leaving its members selected together for Blender's object operations.
        layer.objects.active = None
        _label_focus[layer.as_pointer()] = (host.session_uid, {obj.session_uid for obj in members})
    elif host.visible_get(view_layer=layer) and not host.hide_select:
        _select(layer, host)
    elif host in route:
        # Hidden carriers cannot be Blender-selected during in-place editing.
        _select(layer, None)
        layer.objects.active = None
        _label_focus[layer.as_pointer()] = (host.session_uid, set())
    _pending_name_click = (now, host, location)
    _redraw()
    return True


def _finish_empty_click():
    """Finish the empty-space gesture by closing the current level."""
    global _pending_empty_clicks, _empty_click_window
    times = _pending_empty_clicks
    if not times or len(times) < 2:
        _pending_empty_clicks = None
        _empty_click_window = None
        return None
    from time import perf_counter
    if perf_counter() - times[-1] < _click_window(bpy.context):
        return 0.05
    _pending_empty_clicks = None
    window = _empty_click_window
    _empty_click_window = None
    try:
        if window is not None:
            with bpy.context.temp_override(window=window):
                back(bpy.context)
        else:
            back(bpy.context)
    except (ValueError, RuntimeError, ReferenceError):
        pass
    return None


def _empty_click(context):
    """Two clicks on empty space close one level, returning to the scene at the root."""
    global _pending_empty_clicks, _empty_click_window
    from time import perf_counter
    if not path(context):
        _pending_empty_clicks = None
        _empty_click_window = None
        return False
    now = perf_counter()
    window = _click_window(context)
    times = list(_pending_empty_clicks or ())
    if times and now - times[-1] > window:
        times = []
    times.append(now)
    if len(times) >= 2:
        _pending_empty_clicks = None
        _empty_click_window = None
        back(context)
        return True
    _pending_empty_clicks = times
    return False


def _contained_by_group(group, obj):
    """True when obj is this group or a member of it, including a member of a nested group."""
    if group is None or obj is None:
        return False
    if group == obj:
        return True
    source = hosts.definition(group)
    if source is None:
        return False
    pending = [source]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        members = content(current)
        if members is None:
            continue
        if members.all_objects.get(obj.name) == obj:
            return True
        for child in members.all_objects:
            nested = hosts.definition(child)
            if nested is not None and nested not in seen:
                pending.append(nested)
    return False


def _top_group_hit(context, origin, direction, objects=None, target=None):
    """The scene group whose bounds the ray enters first."""
    from .picking import _PickIndex
    if objects is None:
        objects = getattr(context, "visible_objects", ())
    index = _PickIndex(objects, context.evaluated_depsgraph_get())
    hits, _ = index.cast(origin, direction, include_members=False)
    if target is not None and not hosts.definition(target):
        hits = [hit for hit in hits if hit.path and _contained_by_group(hit.path[0], target)]
    if not hits:
        return None
    nearest = min(hits, key=lambda item: (item.distance, len(item.path)))
    return nearest.path[0] if nearest.path else None


def _group_under_mouse(context, event, obj):
    """Group instance under the cursor, including one hit through its displayed geometry."""
    if obj is not None and not is_helper(obj) and hosts.definition(obj):
        return obj
    if obj is None or is_helper(obj):
        return None
    from bpy_extras import view3d_utils
    region = _view3d_under_mouse(event)
    if region is None or region.data is None:
        return None
    xy = (event.mouse_x - region.x, event.mouse_y - region.y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region.data, xy)
    direction = view3d_utils.region_2d_to_vector_3d(region, region.data, xy)
    return _top_group_hit(context, origin, direction, target=obj)


def _group_double_click(context, host):
    """Two clicks on a group in Object Mode enter that group."""
    global _pending_group_clicks
    from time import perf_counter
    if path(context) or host is None or not hosts.definition(host):
        _pending_group_clicks = None
        return False
    now = perf_counter()
    window = _click_window(context)
    previous = _pending_group_clicks
    times = []
    if previous is not None and previous[0] == host and now - previous[1][-1] <= window:
        times = list(previous[1])
    times.append(now)
    if len(times) >= 2:
        _pending_group_clicks = None
        _click_down(context, host)
        return True
    _pending_group_clicks = (host, times)
    return False


def _level_group_hit(context, origin, direction, target=None):
    """The group directly inside the open level whose bounds the ray enters first."""
    route = path(context)
    source = hosts.definition(route[-1]) if route else None
    members = content(source) if source else None
    if members is None:
        return None
    from .picking import _PickIndex
    objects = [obj for obj in members.objects if hosts.definition(obj)]
    index = _PickIndex(objects, context.evaluated_depsgraph_get())
    hits, _ = index.cast(origin, direction, include_members=False)
    if target is not None and not hosts.definition(target):
        hits = [hit for hit in hits if hit.path and _contained_by_group(hit.path[0], target)]
    if not hits:
        return None
    nearest = min(hits, key=lambda item: (item.distance, len(item.path)))
    return nearest.path[0] if nearest.path else None


def _level_group_under_mouse(context, event, obj):
    """Child group under the cursor while a group level is open."""
    route = path(context)
    if not route:
        return None
    if obj is not None and not is_helper(obj):
        other = _specific_other_group(context, obj)
        if other is not None:
            return other
    if obj is None or is_helper(obj):
        return None
    from bpy_extras import view3d_utils
    region = _view3d_under_mouse(event)
    if region is None or region.data is None:
        return None
    xy = (event.mouse_x - region.x, event.mouse_y - region.y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region.data, xy)
    direction = view3d_utils.region_2d_to_vector_3d(region, region.data, xy)
    return _level_group_hit(context, origin, direction, target=obj)


def _level_group_double_click(context, host):
    """Two clicks on a group inside the open level enter that group."""
    global _pending_level_clicks
    from time import perf_counter
    route = path(context)
    if not route or host is None or host == route[-1]:
        _pending_level_clicks = None
        return False
    now = perf_counter()
    window = _click_window(context)
    previous = _pending_level_clicks
    times = []
    if previous is not None and previous[0] == host and now - previous[1][-1] <= window:
        times = list(previous[1])
    times.append(now)
    if len(times) >= 2:
        _pending_level_clicks = None
        _click_down(context, host)
        return True
    _pending_level_clicks = (host, times)
    return False


def _object_double_click(context, obj):
    """Two clicks on an object run Down Level for that object."""
    global _pending_object_click
    from time import perf_counter
    if obj is None or not path(context) or is_helper(obj):
        _pending_object_click = None
        return False
    now = perf_counter()
    pending = _pending_object_click
    window = _click_window(context)
    if pending is not None and pending[1] == obj and now - pending[0] <= window:
        _pending_object_click = None
        return _click_down(context, obj)
    _pending_object_click = (now, obj)
    return False


def _view3d_under_mouse(event):
    x, y = event.mouse_x, event.mouse_y
    window = bpy.context.window
    if not window or not window.screen:
        return None
    for area in window.screen.areas:
        if area.type != 'VIEW_3D' or not (area.x <= x < area.x + area.width and area.y <= y < area.y + area.height):
            continue
        canvas = None
        for region in area.regions:
            if region.width <= 1 or region.height <= 1:
                continue
            if region.x <= x < region.x + region.width and region.y <= y < region.y + region.height:
                if region.type == 'WINDOW':
                    canvas = region
                elif region.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'ASSET_SHELF', 'ASSET_SHELF_HEADER'}:
                    return None
        return canvas
    return None


def _object_under_mouse(context, event):
    from bpy_extras import view3d_utils
    region = _view3d_under_mouse(event)
    if region is None or region.data is None:
        return None
    xy = (event.mouse_x - region.x, event.mouse_y - region.y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region.data, xy)
    direction = view3d_utils.region_2d_to_vector_3d(region, region.data, xy)
    hit, _location, _normal, _index, obj, _matrix = context.scene.ray_cast(
        context.evaluated_depsgraph_get(), origin, direction)
    return obj if hit else None


def _chain_inside(base, target):
    """Path from a group down to another group inside it."""
    if base is None or target is None:
        return None
    if base == target:
        return [base]
    source = hosts.definition(base)
    if source is None:
        return None

    def search(current, chain, seen):
        members = content(current)
        if members is None:
            return None
        for obj in members.all_objects:
            if obj is None or is_helper(obj):
                continue
            if obj == target:
                return chain + [obj]
            nested = hosts.definition(obj)
            if nested and nested not in seen and not nested.library and not nested.override_library:
                found = search(nested, chain + [obj], seen | {nested})
                if found:
                    return found
        return None

    return search(source, [base], {source})


def _jump_path(context, host):
    """Route that lands on a group, keeping the shared parent levels above it."""
    route = path(context)
    if not route:
        return [host]
    if host in route:
        return route[:route.index(host) + 1]
    for index in range(len(route) - 1, -1, -1):
        chain = _chain_inside(route[index], host)
        if chain and chain[-1] == host and len(chain) > 1:
            return [*route[:index], *chain]
    return [host]


def _specific_other_group(context, obj):
    """The group under the cursor when it is not the group currently open."""
    route = path(context)
    if obj is None or is_helper(obj):
        return None
    seen = set()
    found = []
    for boxes in (_selected_boxes(context, []), _sibling_boxes(context), _member_group_boxes(context, []),
                  _ancestor_boxes(context)):
        for host, _corners in boxes:
            if host in seen or (route and host == route[-1]) or (route and host in route and _contained_by_group(route[-1], obj)):
                continue
            seen.add(host)
            if hosts.definition(host) and _contained_by_group(host, obj):
                found.append(host)
    if hosts.definition(obj) and obj not in seen and not (route and obj == route[-1]):
        found.append(obj)
    if not found:
        return None
    specific = [host for host in found if not any(host != other and _contained_by_group(host, other) for other in found)]
    return specific[-1] if specific else found[-1]


def _enter_named_group(context, host):
    """Navigate to the level represented by a viewport label."""
    global _dismissed_preview
    route = path(context)
    if host is None or (route and host == route[-1]):
        return
    _dismissed_preview = None
    if not hosts.definition(host):
        host = next((group for group in (*reversed(route), *peers(context)) if _owns(group, host)), None)
        if host is None:
            back(context, all_levels=True)
            return
    if host in route:
        levels = len(route) - route.index(host) - 1
        if levels:
            back(context, steps=levels)
        return
    if route and host in peers(context):
        activate_host(context, host)
        _redraw()
        return
    open_path(context, _jump_path(context, host))
    _redraw()


def _sibling_chain(context, host):
    """Path that replaces the current level with another group in the same parent."""
    route = path(context)
    if len(route) < 2 or host is None or host in route or host in peers(context):
        return None
    if not hosts.definition(host):
        return None
    parent = hosts.definition(route[-2])
    members = content(parent) if parent else None
    if members is None or members.all_objects.get(getattr(host, "name", "")) != host:
        return None
    return [*route[:-1], host]


def _down_target(context):
    """Selected group nested under the open level that Down Level can open."""
    route = path(context)
    candidates = []
    active = getattr(context, "active_object", None)
    if active is not None:
        candidates.append(active)
    for obj in getattr(context, "selected_objects", None) or []:
        if obj not in candidates:
            candidates.append(obj)
    for obj in candidates:
        if obj is None or not hosts.definition(obj):
            continue
        if route and obj == route[-1]:
            continue
        if not route or _chain_to_group(context, obj):
            return obj
    return None


def _selected_level_group(context):
    """Group whose level Up and Down follow. The active object wins."""
    route = path(context)
    active = getattr(context, "active_object", None)
    if active is not None and hosts.definition(active):
        return active
    for obj in getattr(context, "selected_objects", None) or []:
        if obj is not None and hosts.definition(obj):
            return obj
    if route and active is not None:
        owner = owner_host(context, active)
        if owner is not None:
            return owner
    return route[-1] if route else None


def _up_destination(context):
    """Path Up Level opens. The selected group on this path chooses how far up."""
    route = list(path(context) or [])
    if len(route) < 2:
        return None
    host = _selected_level_group(context)
    if host in route:
        index = route.index(host)
        if index == 0:
            return [route[0]]
        return route[:index]
    if host is not None:
        for index in range(len(route) - 1, -1, -1):
            chain = _chain_inside(route[index], host)
            if chain and chain[-1] == host and len(chain) > 1:
                parent = [*route[:index], *chain[:-1]]
                if parent and parent != route:
                    return parent
                break
    return route[:-1]


def _moves_deeper(route, destination):
    """True when the path enters another group instead of stepping back to an ancestor."""
    if not destination or list(destination) == list(route):
        return False
    if len(destination) < len(route) and list(route[:len(destination)]) == list(destination):
        return False
    return True


def _click_down(context, host=None):
    """Double-click on a target: Down Level into that group."""
    route = list(path(context) or [])
    if host is not None and route and host in route:
        host = None
    if host is None:
        host = _down_level_target(context)
    elif route and hosts.definition(host):
        destination = _jump_path(context, host)
        if not _moves_deeper(route, destination):
            host = _down_level_target(context)
    elif route:
        children = [item for item, _corners in _member_group_boxes(context, [])]
        matches = [item for item in children if item != host and _contained_by_group(item, host)]
        host = matches[-1] if matches else _down_level_target(context)
    if host is None:
        return False
    destination = [host] if not route else _jump_path(context, host)
    if not _moves_deeper(route, destination):
        return False
    open_path(context, destination)
    _redraw()
    return True


def _down_level_target(context):
    """A group nested under the open level. Ancestors are not a Down Level target."""
    route = path(context)
    candidates = []
    active = getattr(context, "active_object", None)
    if active is not None:
        candidates.append(active)
    for obj in getattr(context, "selected_objects", None) or []:
        if obj not in candidates:
            candidates.append(obj)
    found = []
    for obj in candidates:
        if obj is None or not hosts.definition(obj):
            continue
        if not route:
            found.append(obj)
            continue
        if obj in route:
            continue
        chain = _chain_to_group(context, obj)
        if chain and chain[-1] == obj and len(chain) > len(route):
            found.append(obj)
    if found:
        specific = [obj for obj in found if not any(
            obj != other and _contained_by_group(obj, other) for other in found)]
        return specific[0] if specific else found[0]
    if not route:
        return None
    children = [host for host, _corners in _member_group_boxes(context, [])]
    for obj in candidates:
        if obj is None:
            continue
        matches = [host for host in children if host != obj and _contained_by_group(host, obj)]
        if not matches:
            continue
        specific = [host for host in matches if not any(
            host != other and _contained_by_group(host, other) for other in matches)]
        return specific[-1] if specific else matches[-1]
    return None


def _open_from_button(context, host):
    """Open a selected group from its overlay button and make it the active one."""
    global _dismissed_preview
    if host is None:
        return
    if not path(context):
        open_path(context, [host])
        _dismissed_preview = None
        _redraw()
        return
    activate_host(context, host)


def activate_host(context, host):
    """Make a group the active one. A group that is only selected is opened first."""
    global _dismissed_preview
    route = path(context)
    if host is None or not route or host == route[-1]:
        return
    host_source = hosts.definition(host)
    if host_source and host_source == hosts.definition(route[-1]) and edit_layer.is_open(host_source):
        if host not in peers(context) and host != edit_layer.open_host(host_source):
            open_peer(context, host)
        return
    if host not in peers(context):
        join_host(context, host)
    route = path(context)
    if host not in peers(context):
        return
    _dismissed_preview = None
    scene = context.scene
    sources = _read(scene, _SOURCES)
    if len(sources) != len(route):
        return
    sources[-1] = hosts.definition(host)
    _write(scene, _SOURCES, sources)
    _write(scene, _ROUTE, route[:-1] + [host])
    _write(scene, _PEERS, [route[-1], *[item for item in peers(context) if item != host]])
    _drop_bounds()
    _redraw()


def close_edited(context, host):
    """Leave one open group. The others stay in edit."""
    route = path(context)
    if not route or host is None:
        return
    if host == route[-1]:
        others = [item for item in peers(context)
                  if item != host and hosts.definition(item) != hosts.definition(host)]
        if others:
            activate_host(context, others[0])
            close_peer(context, host)
            _redraw()
        else:
            back(context, 1)
        return
    if host in peers(context):
        close_peer(context, host)
        _redraw()
        return
    global _dismissed_preview
    _dismissed_preview = host
    _redraw()


def follow_selection(context):
    """The last selected group's group becomes active."""
    activate_host(context, owner_host(context, getattr(context, "active_object", None)))


def active_paths():
    return [route for scene in bpy.data.scenes if (route := _read(scene, _ROUTE))]


def _validate(scene, route):
    if not route:
        raise ValueError("Select a group instance first")
    members = scene.objects
    seen = set()
    for host in route:
        if host is None or members.get(host.name) != host:
            raise ValueError("The Link Group path changed. Select it again")
        source = hosts.definition(host)
        if not source or source in seen:
            raise ValueError("The group reference is invalid or cyclic")
        seen.add(source)
        members = content(source).all_objects


def _layer(scene, context):
    return scene.view_layers.get(scene.get(_LAYER, "")) or context.view_layer


def _find_layer(layer, collection):
    if layer.collection == collection:
        return layer
    for child in layer.children:
        match = _find_layer(child, collection)
        if match:
            return match
    return None


def _select(layer, obj):
    _label_focus.pop(layer.as_pointer(), None)
    for other in layer.objects.selected:
        other.select_set(False, view_layer=layer)
    if obj and layer.objects.get(obj.name) == obj and not obj.hide_get(view_layer=layer):
        obj.select_set(True, view_layer=layer)
        layer.objects.active = obj


def _set_focus(context, source, prefer=None):
    layer = _layer(context.scene, context)
    layer.update()
    work = content(source)
    active_collection = _find_layer(layer.layer_collection, work)
    if active_collection:
        layer.active_layer_collection = active_collection
    for area in context.window.screen.areas if context.window else ():
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        if space.local_view:
            for obj in work.all_objects:
                if layer.objects.get(obj.name) is not None:
                    obj.local_view_set(space, True)
        area.tag_redraw()
    _select(layer, prefer or next((o for o in work.all_objects
                                   if not is_helper(o) and not o.hide_get(view_layer=layer)), None))


def _remember(context):
    scene = context.scene
    if _LAYER not in scene:
        scene[_LAYER] = context.view_layer.name
        old = context.view_layer.active_layer_collection.collection
        if old != scene.collection:
            scene[_PREV_COLLECTION] = old


def _close_peer_hosts(scene, layer):
    for host in reversed(_read(scene, _PEERS)):
        source = hosts.definition(host) if host else None
        if source and edit_layer.is_open(source) and edit_layer.open_host(source) == host:
            edit_layer.leave(scene, layer, source)
        elif source and edit_layer.is_open(source):
            edit_layer.close_linked_peers(source, layer, host)
    if _PEERS in scene:
        del scene[_PEERS]
    _drop_bounds()


def close_peer(context, host):
    scene = context.scene
    layer = _layer(scene, context)
    source = hosts.definition(host) if host else None
    if source and edit_layer.is_open(source) and edit_layer.open_host(source) == host:
        edit_layer.leave(scene, layer, source)
        remaining = [item for item in peers(context)
                     if item != host and hosts.definition(item) != source]
    else:
        if source and edit_layer.is_open(source):
            edit_layer.close_linked_peers(source, layer, host)
        remaining = [item for item in peers(context) if item != host]
    _write(scene, _PEERS, remaining)
    _drop_bounds()


def close_peers(context):
    scene = context.scene
    if scene:
        _close_peer_hosts(scene, _layer(scene, context))


def open_peer(context, host):
    """Open one more group beside the active path, without changing which group is active."""
    scene = context.scene
    layer = _layer(scene, context)
    source = hosts.definition(host)
    if not source or source.library or source.override_library:
        raise ValueError("Make an editable local group first")
    if edit_layer.is_open(source):
        if host != edit_layer.open_host(source) and host not in peers(context):
            edit_layer.open_linked_peer(layer, source, host)
            _write(scene, _PEERS, [*peers(context), host])
            _drop_bounds()
        return
    _check_instance_depth(source)
    layer.update()
    placed = host.evaluated_get(layer.depsgraph).matrix_world.copy()
    transform = placed @ Matrix.Translation(-source.instance_offset)
    if abs(transform.to_3x3().determinant()) < 1e-10:
        raise ValueError("This Link Group's scale cannot be inverted, so member positions cannot be converted")
    edit_layer.enter(scene, layer, host, source, transform)
    _write(scene, _PEERS, [*peers(context), host])
    _drop_bounds()


def host_is_open(host):
    source = hosts.definition(host) if host else None
    return bool(source and edit_layer.is_open(source))


def _release_active_lock():
    global _dismissed_preview, _mesh_session_opened_group
    _dismissed_preview = None
    _mesh_session_opened_group = False


def _finish(context, previous=None):
    scene = context.scene
    layer = _layer(scene, context)
    _close_peer_hosts(scene, layer)
    original = scene.get(_PREV_COLLECTION)
    lcol = _find_layer(layer.layer_collection, original) if original else None
    layer.active_layer_collection = lcol or layer.layer_collection
    for key in (_ROUTE, _SOURCES, _PEERS, _LAYER, _PREV_COLLECTION):
        if key in scene:
            del scene[key]
    if previous:
        _select(layer, previous)
    _release_active_lock()


def _fold(scene, layer, keep=0):
    sources = _read(scene, _SOURCES)
    while len(sources) > keep:
        source = sources[-1]
        if source and edit_layer.is_open(source):
            edit_layer.leave(scene, layer, source)
        sources.pop()
        _write(scene, _SOURCES, sources)
    layer.update()
    _drop_bounds()


def _check_instance_depth(target):
    """An edit layer adds one collection instance on paths through this definition."""
    def references(objects):
        found = set()
        for obj in objects:
            child = obj.instance_collection if obj.instance_type == 'COLLECTION' else hosts.definition(obj)
            if child:
                found.add(child)
        return found

    cache = {}

    def lengths(collection, ancestors):
        if collection in cache:
            return cache[collection]
        longest, affected = 1, 0
        for child in references(collection.all_objects):
            if child in ancestors:
                continue
            child_length, child_affected = lengths(child, ancestors | {child})
            longest = max(longest, 1 + child_length)
            if child_affected:
                affected = max(affected, 1 + child_affected)
        if collection == target:
            affected = longest
        cache[collection] = longest, affected
        return longest, affected

    for scene in bpy.data.scenes:
        for root in references(scene.objects):
            _, depth = lengths(root, {root})
            # Blender 5.3 stops drawing leaf geometry at instance depth eight.
            if depth >= 7:
                raise ValueError(f'"{target.name}" under "{root.name}" is already {depth} levels deep;'
                                 "opening it in place would exceed Blender's instance depth. Remove one nesting level first")


def _show(context, route, start=0):
    scene = context.scene
    layer = _layer(scene, context)
    sources = _read(scene, _SOURCES)
    for host in route[start:]:
        source = hosts.definition(host)
        if not source or source.library or source.override_library:
            raise ValueError("Make an editable local group first")
        if edit_layer.is_open(source):
            raise ValueError("This group is being edited at another Link Group. Exit that level first")
        _check_instance_depth(source)
        layer.update()
        placed = host.evaluated_get(layer.depsgraph).matrix_world.copy()
        transform = placed @ Matrix.Translation(-source.instance_offset)
        if abs(transform.to_3x3().determinant()) < 1e-10:
            raise ValueError("This Link Group's scale cannot be inverted, so member positions cannot be converted")
        edit_layer.enter(scene, layer, host, source, transform)
        sources.append(source)
        _write(scene, _SOURCES, sources)
        for peer in peers(context):
            if peer and peer != host and hosts.definition(peer) == source:
                edit_layer.open_linked_peer(layer, source, peer)
    _write(scene, _ROUTE, route)
    _drop_bounds()


def _redraw():
    for wm in bpy.data.window_managers:
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    _ensure_controls()


def _overlay_live(context=None):
    context = context or bpy.context
    if active_paths():
        return True
    if getattr(context, "mode", None) != 'OBJECT':
        return False
    return bool(_selected_groups(context))


def _launch_controls():
    if _controls_running or not _overlay_live():
        return None
    try:
        bpy.ops.groupmanager.bounds_controls('INVOKE_DEFAULT')
    except RuntimeError:
        return 0.4
    return None


def _ensure_controls():
    if bpy.app.background or _controls_running or not _overlay_live():
        return
    if not bpy.app.timers.is_registered(_launch_controls):
        bpy.app.timers.register(_launch_controls, first_interval=0.05)


def sync_path(context, placements):
    """Rebind an open occurrence after a change to a path's definitions."""
    if context.scene.session_uid in _suspended:
        _write(context.scene, _ROUTE, placements)
    elif path(context):
        open_path(context, placements)


@contextmanager
def source_space(context):
    """Run a data operation in canonical coordinates, then expose the edited path again."""
    scene = context.scene
    key = scene.session_uid
    route = path(context)
    if not route or key in _suspended:
        yield
        return
    layer = _layer(scene, context)
    active = context.active_object
    selected = list(context.selected_objects)
    _fold(scene, layer)
    _suspended.add(key)
    try:
        yield
    finally:
        _suspended.discard(key)
        route = path(context)
        try:
            if route:
                _validate(scene, route)
                _show(context, route)
                # Keep the active member where the operation preserved its identity.
                try:
                    suitable = active if active and layer.objects.get(active.name) == active else None
                except ReferenceError:
                    suitable = None
                _set_focus(context, hosts.definition(route[-1]), suitable)
                for obj in selected:
                    try:
                        if layer.objects.get(obj.name) == obj and not obj.hide_get(view_layer=layer):
                            obj.select_set(True, view_layer=layer)
                    except ReferenceError:
                        pass
            else:
                _finish(context)
        except (ValueError, ReferenceError, RuntimeError):
            _fold(scene, layer)
            _finish(context)
            raise


def open_path(context, placements, unique=False, *, scope='OBJECT_DATA', stay_on_host=False):
    if not context.window or context.mode != 'OBJECT':
        raise ValueError("Enter the group from Object Mode")
    scene = context.scene
    route = list(placements)
    _validate(scene, route)
    old = path(context)
    layer = _layer(scene, context)
    _remember(context)
    # Fork in canonical source space. Temporarily open work data contain live
    # parenting anchors, which must never be copied into an independent asset.
    common = 0
    if not unique:
        for a, b in zip(old, route):
            if a != b:
                break
            common += 1
    _fold(scene, layer, common)
    try:
        with branches.BranchChanges() as changes:
            if unique:
                route = changes.independent_path(route, scope=scope)
            try:
                host = route[-1] if stay_on_host else None
                opened = route[:-1] if stay_on_host else route
                if opened:
                    _show(context, opened, common)
                    _set_focus(context, hosts.definition(opened[-1]), host)
                else:
                    _finish(context, host)
            except Exception:
                # Disassemble edit layers while a failed fork's IDs still exist.
                _fold(scene, layer, common)
                raise
    except Exception:
        _fold(scene, layer, common)
        if old:
            _show(context, old, common)
            _set_focus(context, hosts.definition(old[-1]))
        else:
            _finish(context)
        raise
    _redraw()


def scoped_local(context, obj, action):
    previous = path(context)
    if not previous:
        return action(obj)
    _validate(context.scene, previous + [obj])
    with source_space(context):
        with branches.BranchChanges() as changes:
            localized = changes.parent_path(previous + [obj])
            result = action(localized[-1])
            sync_path(context, localized[:-1])
            return result


def back(context, steps=1, all_levels=False):
    scene = context.scene
    route = path(context)
    if not route:
        return
    if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')
    keep = 0 if all_levels else max(0, len(route) - max(1, steps))
    layer = _layer(scene, context)
    _close_peer_hosts(scene, layer)
    _fold(scene, layer, keep)
    if keep:
        _write(scene, _ROUTE, route[:keep])
        _set_focus(context, hosts.definition(route[keep-1]), route[keep])
    else:
        _finish(context, route[0])
    _redraw()


@persistent
def _close_editors(_unused=None):
    for scene in list(bpy.data.scenes):
        if not scene.get(_ROUTE) and not scene.get(_SOURCES):
            continue
        scene.pop(_MESH_EDIT, None)
        win = next((w for wm in bpy.data.window_managers for w in wm.windows if w.scene == scene), None)
        if win:
            with bpy.context.temp_override(window=win):
                if bpy.context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
                    bpy.ops.object.mode_set(mode='OBJECT')
                route = path(bpy.context)
                _close_peer_hosts(scene, _layer(scene, bpy.context))
                _fold(scene, _layer(scene, bpy.context))
                _finish(bpy.context, route[0] if route else None)
        else:
            layer = scene.view_layers.get(scene.get(_LAYER, "")) or scene.view_layers[0]
            _close_peer_hosts(scene, layer)
            _fold(scene, layer)
            for key in (_ROUTE, _SOURCES, _PEERS, _LAYER, _PREV_COLLECTION):
                if key in scene:
                    del scene[key]
            _release_active_lock()
    _drop_bounds()


@persistent
def _invalidate_bounds(scene, depsgraph):
    edit_layer.sync_linked_mirrors()
    _drop_bounds()


def _bounds_style(context, preview=False, always=False):
    from types import SimpleNamespace
    from . import preferences
    prefs = preferences.get(context)
    if prefs and not prefs.show_edit_bounds and not always:
        return None
    editing = bool(path(context)) or preview or always
    if prefs is None:
        return SimpleNamespace(width=preferences.BOUNDS_WIDTH, hierarchy_gap=preferences.BOUNDS_HIERARCHY_GAP,
                               show_3d=False, style_3d='CORNERS',
                               corner_3d=preferences.BOUNDS_CORNER, show_2d=editing, style_2d='CORNERS',
                               corner_2d=preferences.BOUNDS_CORNER, fit_2d=preferences.BOUNDS_2D_FIT, show_info=True)
    return SimpleNamespace(width=prefs.bounds_width, hierarchy_gap=prefs.bounds_hierarchy_gap,
                           show_3d=bool(prefs.show_bounds_3d and editing),
                           style_3d=prefs.bounds_3d_style, corner_3d=prefs.bounds_3d_corner,
                           show_2d=bool(prefs.show_bounds_2d and editing), style_2d=prefs.bounds_2d_style,
                           corner_2d=prefs.bounds_2d_corner, fit_2d=prefs.bounds_2d_fit, show_info=prefs.show_frame_info)


def group_size(obj):
    """World size of a group's contents. Placement carriers often report zero dimensions."""
    source = hosts.definition(obj) if obj is not None else None
    if source is None:
        return tuple(obj.dimensions) if obj is not None else (0.0, 0.0, 0.0)
    low, high = _shapes(bpy.context)._shape(source, {source}).bounds
    if obj == edit_layer.open_host(source):
        scale = content_frame(source).to_scale()
    else:
        scale = obj.matrix_world.to_scale()
    return tuple(abs((high[i] - low[i]) * scale[i]) for i in range(3))


def _object_color(context, active):
    """Match the 3D Viewport theme: active object versus selected object."""
    try:
        theme = context.preferences.themes[0].view_3d
        color = theme.object_active if active else theme.object_selected
        return tuple(color)
    except (AttributeError, IndexError, TypeError):
        return (0.965, 0.741, 0.267) if active else (0.965, 0.541, 0.0)


def _frame_ids(objects):
    ids = []
    for obj in objects:
        try:
            if obj is not None:
                ids.append(obj.as_pointer())
        except ReferenceError:
            continue
    return tuple(ids)


def _cached_frames(key, build):
    cached = _frame_cache.get(key)
    if cached is None:
        cached = build()
        _frame_cache[key] = cached
    return cached


def _bound_corners(index, host, source):
    from .picking import _corners
    shape = index._shape(source, {source})
    matrix = _placement_matrix(host, source, index._depsgraph)
    return [matrix @ point for point in _corners(shape.bounds)]


def _open_boxes(context):
    route = path(context)
    if not route or not route[-1] or context.scene is None:
        return []
    hosts_open = [route[-1], *peers(context)]
    keyed = []
    for host in hosts_open:
        source = hosts.definition(host) if host else None
        if source and edit_layer.is_open(source):
            keyed.append((host, source))
    if not keyed:
        return []
    key = ("open", context.scene.session_uid, _frame_ids(host for host, _source in keyed))
    return _cached_frames(key, lambda: _open_frame_list(context, keyed))


def _open_frame_list(context, keyed):
    index = _shapes(context)
    return [(host, _bound_corners(index, host, source)) for host, source in keyed]


def preview_host(context):
    """Group under the selection that is not in this edit until Activate is clicked."""
    global _dismissed_preview
    route = path(context)
    obj = getattr(context, "active_object", None)
    try:
        if _dismissed_preview is not None and obj != _dismissed_preview:
            _dismissed_preview = None
    except ReferenceError:
        _dismissed_preview = None
    if not route or obj is None or obj == _dismissed_preview or owner_host(context, obj) is not None:
        return None
    source = hosts.definition(obj)
    if not source or source.library or source.override_library:
        return None
    if obj in peers(context) or obj == route[-1] or edit_layer.open_host(source) == obj:
        return None
    return obj


def _selected_boxes(context, skip, objects=None):
    """One box for every selected group, including a Select All selection."""
    global _dismissed_preview
    chosen = []
    seen = set(skip)
    if objects is None:
        objects = list(context.selected_objects)
        if context.active_object is not None:
            objects.append(context.active_object)
    for obj in objects:
        if obj is None or obj in seen:
            continue
        seen.add(obj)
        try:
            dismissed = obj == _dismissed_preview
        except ReferenceError:
            _dismissed_preview = None
            dismissed = False
        if dismissed:
            continue
        source = hosts.definition(obj)
        if not source or source.library or source.override_library:
            continue
        if edit_layer.open_host(source) == obj:
            continue
        try:
            original = obj.get("_gm_mirror_of")
        except ReferenceError:
            continue
        if original in seen:
            continue
        chosen.append((obj, source))
    if not chosen:
        return []
    scene = context.scene
    sid = scene.session_uid if scene is not None else 0
    key = ("selected", sid, _frame_ids(host for host, _source in chosen))
    return _cached_frames(key, lambda: _selected_frame_list(context, chosen))


def _selected_frame_list(context, chosen):
    index = _shapes(context)
    boxes = []
    for host, source in chosen:
        try:
            boxes.append((host, _bound_corners(index, host, source)))
        except ReferenceError:
            continue
    return boxes


def _member_group_boxes(context, skip):
    """Groups directly inside an open group. Deeper groups stay hidden until that level is open."""
    route = path(context)
    if not route:
        return []
    scene = context.scene
    sid = scene.session_uid if scene is not None else 0
    opened = (route[-1], *peers(context))
    key = ("members", sid, _frame_ids(opened), _frame_ids(skip))
    return _cached_frames(key, lambda: _member_frame_list(context, opened, skip))


def _member_frame_list(context, opened, skip):
    """Direct child groups of every selected open group, each in that group's own place."""
    from .picking import _corners
    index = _shapes(context)
    boxes = []
    seen = set()
    for host in opened:
        source = hosts.definition(host) if host else None
        if not source or not edit_layer.is_open(source):
            continue
        primary = edit_layer.open_host(source)
        if host == primary or primary is None:
            matrix = _placement_matrix(host, source, index._depsgraph)
        else:
            matrix = host.matrix_world.copy()
        shape = index._shape(source, {source})
        for obj, child, local in shape.children:
            if child.library or getattr(child, "override_library", None):
                continue
            try:
                key = (host.as_pointer(), obj.as_pointer())
            except ReferenceError:
                continue
            if key in seen or obj in skip:
                continue
            seen.add(key)
            child_shape = index._shape(child, {source, child})
            try:
                points = _corners(child_shape.bounds)
                boxes.append((obj, [matrix @ local @ point for point in points]))
            except ReferenceError:
                continue
    return boxes


def _sibling_boxes(context):
    """Other groups beside the current level, inside the same parent. Drawn in gray."""
    route = path(context)
    if len(route) < 2:
        return []
    parent = route[-2]
    parent_source = hosts.definition(parent) if parent else None
    if not parent_source:
        return []
    scene = context.scene
    sid = scene.session_uid if scene is not None else 0
    key = ("siblings", sid, parent.as_pointer(), _frame_ids((route[-1], *peers(context))))
    return _cached_frames(key, lambda: _sibling_frame_list(context, parent, parent_source, route))


def _sibling_frame_list(context, parent, parent_source, route):
    from .picking import _corners
    index = _shapes(context)
    matrix = _placement_matrix(parent, parent_source, index._depsgraph)
    shape = index._shape(parent_source, {parent_source})
    skip = {route[-1], *peers(context)}
    boxes = []
    for obj, child, local in shape.children:
        if obj in skip or child.library or getattr(child, "override_library", None):
            continue
        child_shape = index._shape(child, {parent_source, child})
        try:
            boxes.append((obj, [matrix @ local @ point for point in _corners(child_shape.bounds)]))
        except ReferenceError:
            continue
    return boxes


def _ancestor_boxes(context):
    """Parent levels of the current group. Drawn in gray behind the active level."""
    route = path(context)
    if len(route) < 2 or context.scene is None:
        return []
    key = ("ancestors", context.scene.session_uid, _frame_ids(route[:-1]))
    return _cached_frames(key, lambda: _ancestor_frame_list(context, route))


def _ancestor_frame_list(context, route):
    index = _shapes(context)
    boxes = []
    for host in route[:-1]:
        source = hosts.definition(host) if host else None
        if not source:
            continue
        boxes.append((host, _bound_corners(index, host, source)))
    return boxes


def _level_member_boxes(context, route, space):
    groups, objects = [], []
    graph = context.evaluated_depsgraph_get()
    index = _shapes(context)
    for host in (route[-1], *peers(context)):
        source = hosts.definition(host)
        if not edit_layer.is_open(source):
            continue
        for obj in edit_layer.displayed_members(source, host):
            if not obj.visible_get(view_layer=context.view_layer, viewport=space):
                continue
            child = hosts.definition(obj)
            key = ("level_member", graph.as_pointer(), obj.as_pointer())
            if child:
                points = _cached_frames(key, lambda: _bound_corners(index, obj, child))
                groups.append((obj, points))
            else:
                points = _cached_frames(key, lambda: _object_world_corners(obj.evaluated_get(graph)))
                objects.append((obj, points))
    return groups, objects


def _screen_edge_lines(points):
    """The twelve edges of the projected box. Each edge is drawn once."""
    marks = []
    for index, origin in enumerate(points):
        if origin is None:
            continue
        for bit in (1, 2, 4):
            if index & bit:
                continue
            other = points[index ^ bit]
            if other is None:
                continue
            marks.append((origin.x, origin.y, 0.0))
            marks.append((other.x, other.y, 0.0))
    return marks


def _clipped_box_projection(corners, points, region, rv3d):
    """Project the visible box, including edge intersections with the near plane."""
    from bpy_extras import view3d_utils

    matrix = rv3d.perspective_matrix
    near = matrix[2] + matrix[3]
    distances = [near.dot(corner.to_4d()) for corner in corners]
    visible = [p for p, d in zip(points, distances) if p is not None and d >= 0]
    # Intersections extend real edges towards the viewport boundary. Do not draw
    # a synthetic edge across the view between two freshly cut endpoints.
    original_count = len(visible)
    for i, d in enumerate(distances):
        if d >= 0:
            continue
        for bit in (1, 2, 4):
            j = i ^ bit
            if distances[j] <= 0:
                continue
            cut = corners[i].lerp(corners[j], d / (d - distances[j]))
            p = view3d_utils.location_3d_to_region_2d(region, rv3d, cut)
            if p is not None:
                visible.append(p)
    return visible, original_count


def _group_frame_projection(visible, original_count, margin):
    """Offset the projected hull by the same margin in open and closed groups."""
    from mathutils import Vector
    from mathutils.geometry import convex_hull_2d

    hull = convex_hull_2d(visible) if len(visible) > 2 else range(len(visible))
    points = [visible[i] for i in hull]
    originals = [i < original_count for i in hull]
    if len(points) < 3 or not all(originals):
        # Preserve the actual near-plane intersections when the box crosses the eye.
        return points, originals

    normals = []
    for a, b in zip(points, points[1:] + points[:1]):
        edge = b - a
        normals.append(Vector((edge.y, -edge.x)).normalized())

    shifted = []
    for i, point in enumerate(points):
        prev, curr = normals[i - 1], normals[i]
        det = prev.cross(curr)
        if abs(det) < 1e-6:
            shifted.append(point + curr * margin)
            continue
        miter = Vector((curr.y - prev.y, prev.x - curr.x)) / det
        shifted.append(point + miter * margin)
    return shifted, originals


def _group_outline_lines(visible, originals, corner_size=None):
    """Draw only the perimeter of the projected group box."""
    if len(visible) < 2:
        return []
    hull = list(range(len(visible)))
    edges = zip(hull, hull[1:] + hull[:1]) if len(hull) > 2 else zip(hull, hull[1:])
    lines = []
    for i, j in edges:
        if not originals[i] and not originals[j]:
            continue
        a, b = visible[i], visible[j]
        delta = b - a
        if delta.length < 0.5:
            continue
        if corner_size is None:
            lines.extend((a.to_3d(), b.to_3d()))
        else:
            tick = delta * min(0.45, corner_size / delta.length)
            if originals[i]:
                lines.extend((a.to_3d(), (a + tick).to_3d()))
            if originals[j]:
                lines.extend((b.to_3d(), (b - tick).to_3d()))
    return lines


def _screen_corner_marks(points, pixels):
    """Fixed-length ticks at the eight corners, measured in viewport pixels."""
    marks = []
    for index, origin in enumerate(points):
        if origin is None:
            continue
        for bit in (1, 2, 4):
            other = points[index ^ bit]
            if other is None:
                continue
            delta = other - origin
            span = delta.length
            if span < 1.0:
                continue
            step = min(pixels, span * 0.45)
            end = origin + delta * (step / span)
            marks.append((origin.x, origin.y, 0.0))
            marks.append((end.x, end.y, 0.0))
    return marks


def _overlay_label(host, name, corners, info):
    """The name drawn above a group frame."""
    return name


def _dashed_lines(lines, region, scale):
    """Dash projected edges in pixels, clipping to the viewport before subdivision."""
    from math import hypot
    dash, period = 7.0 * scale, 11.0 * scale
    segments = []
    for start, end in zip(lines[::2], lines[1::2]):
        x, y = start[:2]
        dx, dy = end[0] - x, end[1] - y
        length = hypot(dx, dy)
        if length < 0.01:
            continue
        ux, uy = dx / length, dy / length
        lo, hi = 0.0, length
        for origin, unit, extent in ((x, ux, region.width), (y, uy, region.height)):
            if abs(unit) < 1e-10:
                if not 0 <= origin <= extent:
                    hi = -1
                    break
                continue
            a, b = -origin / unit, (extent - origin) / unit
            lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
        if lo >= hi:
            continue
        cursor = lo - lo % period
        while cursor < hi:
            a, b = max(cursor, lo), min(cursor + dash, hi)
            if a < b:
                segments.extend(((x + a * ux, y + a * uy, 0), (x + b * ux, y + b * uy, 0)))
            cursor += period
    return segments


def _padded_box_corners(corners, region, rv3d, pixels):
    """Expand along the box axes using the pixel size at its center depth."""
    center = sum(corners[1:], corners[0].copy()) / len(corners)
    matrix = rv3d.perspective_matrix
    clip_w = matrix[3].dot(center.to_4d())
    projection_scale = matrix[0].to_3d().length * region.width
    if clip_w <= 0 or projection_scale <= 0:
        return corners
    distance = 2.0 * clip_w * pixels / projection_scale
    axes = [(corners[i] - corners[0]).normalized() for i in (1, 2, 4)]
    for i, axis in enumerate(axes):
        if not axis.length_squared:
            axes[i] = axes[(i + 1) % 3].cross(axes[(i + 2) % 3]).normalized()
    expanded = []
    for index, point in enumerate(corners):
        corner = point.copy()
        for i, axis in enumerate(axes):
            corner += axis * (distance if index & (1 << i) else -distance)
        expanded.append(corner)
    return expanded


def _rect_corner_marks(min_x, min_y, max_x, max_y, pixels):
    """Fixed-length ticks at the four corners of the viewport rectangle."""
    corners = (
        (min_x, min_y, (max_x, min_y), (min_x, max_y)),
        (max_x, min_y, (min_x, min_y), (max_x, max_y)),
        (max_x, max_y, (min_x, max_y), (max_x, min_y)),
        (min_x, max_y, (max_x, max_y), (min_x, min_y)),
    )
    marks = []
    for x, y, *neighbors in corners:
        for nx, ny in neighbors:
            dx, dy = nx - x, ny - y
            span = (dx * dx + dy * dy) ** 0.5
            if span < 1.0:
                continue
            step = min(pixels, span * 0.45)
            marks.append((x, y, 0.0))
            marks.append((x + dx * (step / span), y + dy * (step / span), 0.0))
    return marks


class _FrameLabels:
    """Place each icon and name as one rectangle against the drawn strokes."""
    def __init__(self, region, margin):
        self.region = region
        self.margin = margin
        self.pending = []
        self.columns = {}
        self.frames = {}
        self.parents = {}
        self.pick = None

    def _columns(self, left, right):
        return range(int(max(0, left)) // 96, int(min(self.region.width, right)) // 96 + 1)

    def _reserve(self, obstacle):
        left, right = obstacle[:2]
        for column in self._columns(left, right):
            self.columns.setdefault(column, set()).add(obstacle)

    def strokes(self, points):
        for a, b in zip(points[::2], points[1::2]):
            if a[0] > b[0]:
                a, b = b, a
            self._reserve((a[0], b[0], a[1], b[1], 0))

    def place(self, host, anchor_x, anchor_y, width, height):
        gap = self.margin
        screen = (gap, gap, max(gap, self.region.width - width - gap),
                  max(gap, self.region.height - height - gap))
        parent = self.frames.get(self.parents.get(host))
        frame = self.frames.get(host)
        limits = screen
        position = None
        if parent is not None:
            inside = (max(screen[0], parent[0] + gap), max(screen[1], parent[1] + gap),
                      min(screen[2], parent[2] - width - gap), min(screen[3], parent[3] - height - gap))
            if inside[0] <= inside[2] and inside[1] <= inside[3]:
                position = self._free_position(anchor_x, anchor_y, width, height, inside, frame)
                if position is not None:
                    limits = inside
        if position is None:
            position = self._free_position(anchor_x, anchor_y, width, height, screen, frame)
        elif frame and self._frame_gap(frame, *position, width, height) > height / 2:
            nearby = self._free_position(anchor_x, anchor_y, width, height, screen, frame)
            if nearby and (self._frame_gap(frame, *nearby, width, height) + gap
                           < self._frame_gap(frame, *position, width, height)):
                position, limits = nearby, screen
        if frame and position and position[1] < frame[3]:
            # When displaced downward, tuck the label just inside its own upper-left corner.
            inset = gap + 1.0
            left = max(limits[0], frame[0] + inset)
            top = min(limits[3], frame[3] - inset - height)
            near_corner = (left, max(limits[1], frame[1] + inset, top - height / 2),
                           min(limits[2], frame[2] - width - inset, left + height), top)
            if left <= near_corner[2] and near_corner[1] <= top:
                tucked = self._free_position(left, top, width, height, near_corner)
                if tucked is not None:
                    position = tucked
        left, bottom = position or (min(max(anchor_x, gap), screen[2]), min(max(anchor_y, gap), screen[3]))
        self._reserve((left, left + width, bottom, bottom, height))
        return left, bottom

    @staticmethod
    def _frame_gap(frame, left, bottom, width, height):
        dx = max(frame[0] - left - width, left - frame[2], 0)
        dy = max(frame[1] - bottom - height, bottom - frame[3], 0)
        return (dx * dx + dy * dy) ** 0.5

    def _free_position(self, anchor_x, anchor_y, width, height, limits, frame=None):
        gap = self.margin
        min_x, min_y, max_x, max_y = limits
        x = min(max(anchor_x, min_x), max_x)
        y = min(max(anchor_y, min_y), max_y)
        best = None
        candidates = [x, x + height, x - height, x + width / 2, x - width / 2, x + width, x - width]
        if frame:
            candidates.extend((frame[2] + gap + 1, frame[0] - width - gap - 1,
                               (frame[0] + frame[2] - width) / 2, frame[2] - width))
            # Narrow gaps between adjacent frames may start between the regular samples.
            nearby = set()
            for column in self._columns(x - width, x + 2 * width):
                nearby.update(self.columns.get(column, ()))
            edges = set()
            for x1, x2, y1, y2, thickness in nearby:
                if min(y1, y2) <= frame[3] + height and max(y1, y2) + thickness >= frame[1] - height:
                    edges.update((x2 + gap + 1, x1 - width - gap - 1))
            candidates.extend(sorted((v for v in edges if abs(v - x) <= width), key=lambda v: (abs(v - x), v)))
        candidates.extend((min_x, max_x))
        for left in dict.fromkeys(min(max(candidate, min_x), max_x) for candidate in candidates):
            if best is not None and abs(left - anchor_x) >= best[0]:
                continue
            lo, hi = left - gap, left + width + gap
            obstacles = set()
            for column in self._columns(lo, hi):
                obstacles.update(self.columns.get(column, ()))
            blocked = []
            for x1, x2, y1, y2, thickness in obstacles:
                if x2 < lo or x1 > hi:
                    continue
                if x1 != x2:
                    slope = (y2 - y1) / (x2 - x1)
                    low = y1 + (max(lo, x1) - x1) * slope
                    high = y1 + (min(hi, x2) - x1) * slope
                else:
                    low, high = y1, y2
                blocked.append((min(low, high) - height - gap, max(low, high) + thickness + gap))
            below = above = y
            blocked.sort()
            for start, end in blocked:
                if start < above < end:
                    above = end
            for start, end in reversed(blocked):
                if start < below < end:
                    below = start
            for bottom in (below, above):
                if min_y <= bottom <= max_y:
                    score = abs(left - anchor_x) + abs(bottom - anchor_y)
                    if frame:
                        score += 6 * max(0, self._frame_gap(frame, left, bottom, width, height) - gap - 1)
                    if best is None or score < best[0]:
                        best = (score, left, bottom)
            if best and best[0] == 0:
                break
        return best[1:] if best else None

    def draw(self, buttons, depths=None):
        if depths:
            max_depth = max(depths.values()) + 1
            self.pending.sort(key=lambda label: depths.get(label[0], max_depth))
        for host, name, tint, left, top in self.pending:
            _draw_frame_name(self, host, name, tint, left, top, buttons)


def _most_specific_host(hosts_shown, obj):
    """Displayed group that owns obj, preferring the innermost one."""
    matches = [host for host in hosts_shown if host == obj or _contained_by_group(host, obj)]
    if not matches:
        return None
    narrow = [host for host in matches if not any(
        host != other and _contained_by_group(host, other) and (other == obj or _contained_by_group(other, obj))
        for other in matches)]
    return narrow[-1] if narrow else matches[-1]


def _highlight_hosts(context, hosts_shown):
    """Active and selected info boxes, following the objects that are highlighted now."""
    active = getattr(context, "active_object", None)
    selected = list(getattr(context, "selected_objects", None) or [])
    if active is not None and active not in selected:
        selected.append(active)
    active_host = None
    selected_hosts = set()
    for obj in selected:
        host = _most_specific_host(hosts_shown, obj)
        if host is None:
            continue
        if obj == active:
            active_host = host
        else:
            selected_hosts.add(host)
    if active_host is None and not _mirror_of_shown(active, hosts_shown):
        route = path(context)
        if route and route[-1] in hosts_shown:
            active_host = route[-1]
    selected_hosts.discard(active_host)
    return active_host, selected_hosts


def _mirror_of_shown(obj, hosts_shown):
    """A linked copy whose real object already has a frame in this draw."""
    try:
        original = obj.get("_gm_mirror_of") if obj is not None else None
    except ReferenceError:
        return False
    return original in hosts_shown


def _object_frame_types(context):
    """Object types that use the group frame while selected."""
    from . import preferences
    prefs = preferences.get(context)
    if prefs is None:
        return set()
    return {identifier for attr, identifier, _name, _description in preferences._OBJECT_FRAME_TYPES
            if getattr(prefs, attr, False)}


def _object_world_corners(obj):
    """Eight world corners of an object's bounds, in the order the frame lines expect."""
    from mathutils import Vector
    matrix = obj.matrix_world
    local = obj.bound_box
    if not local or all(tuple(corner) == (-1.0, -1.0, -1.0) for corner in local):
        from .picking import _corners
        radius = obj.empty_display_size if obj.type == 'EMPTY' else 0.1
        return [matrix @ point for point in _corners(((-radius,) * 3, (radius,) * 3))]
    return [matrix @ Vector(local[index]) for index in (0, 1, 3, 2, 4, 5, 7, 6)]


def _plain_object_boxes(context, space, local_objects=None):
    """Eligible object frames for the current selection or isolated viewport."""
    kinds = _object_frame_types(context)
    if not kinds:
        return []
    boxes = []
    seen = set()
    objects = local_objects if local_objects is not None else context.selected_objects
    for obj in objects:
        if obj is None or obj in seen:
            continue
        seen.add(obj)
        try:
            kind = obj.type
        except ReferenceError:
            continue
        if kind not in kinds or is_helper(obj) or hosts.definition(obj):
            continue
        if local_objects is None and not _outline_highlighted(space, obj):
            continue
        try:
            boxes.append((obj, _object_world_corners(obj)))
        except (ReferenceError, TypeError, ValueError):
            continue
    return boxes


def _outline_highlighted(space, obj):
    """Blender is drawing this object's selection outline."""
    overlay = getattr(space, "overlay", None)
    if overlay is None or not getattr(overlay, "show_outline_selected", False):
        return False
    try:
        return bool(obj.select_get() and not obj.hide_get() and not obj.hide_viewport)
    except ReferenceError:
        return False


def _draw_bounds():
    context = bpy.context
    route = path(context)
    region = context.region
    rv3d = context.region_data
    if region is not None:
        _open_frame_rects.pop(region.as_pointer(), None)
    space = getattr(context, "space_data", None)
    object_mode = context.mode == 'OBJECT'
    group_mode = bool(route) and object_mode
    outline = bool(getattr(getattr(space, "overlay", None), "show_outline_selected", False)) if space is not None else False
    in_local_view = bool(space and space.local_view)
    preview = object_mode and not route and (outline or in_local_view)
    object_frames = object_mode and (outline or in_local_view) and bool(_object_frame_types(context))
    if region is None or rv3d is None or space is None or not space.overlay.show_overlays or not object_mode:
        if region is not None:
            _overlay_buttons.pop(region.as_pointer(), None)
        return
    pick = _pick_frames.get(region.as_pointer())
    if not group_mode and not preview and not object_frames and not pick:
        _overlay_buttons.pop(region.as_pointer(), None)
        if _selected_groups(context):
            _ensure_controls()
        return
    from . import preferences
    prefs = preferences.get(context)
    show_groups = prefs is None or prefs.show_edit_bounds
    style = _bounds_style(context, preview=preview or object_frames or bool(pick), always=object_frames)
    local_objects = {obj for obj in context.visible_objects if obj.local_view_get(space)} if in_local_view else None
    plain = _plain_object_boxes(context, space, local_objects) if object_frames and style is not None else []
    boxes = []
    level_groups = set()
    level_objects = set()
    if show_groups and style is not None and (group_mode or preview):
        if group_mode:
            children, members = _level_member_boxes(context, route, space)
            level_groups = {host for host, _ in children}
            level_objects = {obj for obj, _ in members}
            plain = members + [item for item in plain if item[0] not in level_objects]
            if peers(context):
                current = route[-1]
                direct = set(edit_layer.displayed_members(hosts.definition(current), current))
                level_groups.intersection_update(direct)
                level_objects.intersection_update(direct)
            boxes = [*_open_boxes(context), *children]
        boxes.extend(_selected_boxes(context, [host for host, _ in boxes], local_objects))
    if in_local_view:
        boxes = [item for item in boxes if item[0] in local_objects]
        plain = [item for item in plain if item[0] in local_objects]
    if not boxes and not plain and not (pick and show_groups and style):
        _overlay_buttons.pop(region.as_pointer(), None)
        return
    pointer = region.as_pointer()
    _overlay_buttons[pointer] = []
    ui_scale = getattr(getattr(context.preferences, "system", None), "ui_scale", 1.0) or 1.0
    labels = _FrameLabels(region, 3.0 + style.width / 2)
    if pick and show_groups:
        pick.drawn.clear()
        labels.pick = pick
    ancestor_gray = (0.55, 0.55, 0.55)
    if not group_mode:
        active_object = getattr(context, "active_object", None)
        for host, corners in boxes:
            try:
                selected = host.select_get(view_layer=context.view_layer)
                if not in_local_view and not _outline_highlighted(space, host):
                    continue
                name = host.name
                highlighted = selected and host == active_object
            except ReferenceError:
                continue
            color = _object_color(context, highlighted) if selected else ancestor_gray
            _draw_screen_box(region, rv3d, corners, color, host, name,
                             highlighted, False, style, ui_scale, _overlay_buttons[pointer], True, labels=labels)
        _draw_plain_object_frames(context, region, rv3d, plain, style, ui_scale, _overlay_buttons[pointer], labels)
        if labels.pick:
            labels.pick.draw_missing(context, style, labels)
        labels.draw(_overlay_buttons[pointer])
        _ensure_controls()
        return
    if not show_groups:
        _draw_plain_object_frames(context, region, rv3d, plain, style, ui_scale, _overlay_buttons[pointer], labels)
        labels.draw(_overlay_buttons[pointer])
        _ensure_controls()
        return
    relation_boxes = [] if in_local_view else [*_ancestor_boxes(context), *_sibling_boxes(context)]
    active_object = context.active_object
    selected_objects = set(context.selected_objects)
    layer = context.view_layer
    focus = _label_focus.get(layer.as_pointer())
    if focus and (layer.objects.active is not None or
                  focus[1] != {obj.session_uid for obj in layer.objects.selected}):
        _label_focus.pop(layer.as_pointer(), None)
        focus = None
    focused = focus[0] if focus else None
    open_leaf = route[-1] if route else None
    open_hosts = {open_leaf, *peers(context)} if route else set()

    depths = {host: depth for depth, host in enumerate(route)}
    depths.update((host, len(route) - 1) for host in peers(context))
    parents = {}
    selected_frames = selected_objects.copy()
    for host in depths:
        source = hosts.definition(host)
        if edit_layer.is_open(source):
            members = edit_layer.displayed_members(source, host)
            for member in members:
                parents[member] = host
                if host.session_uid == focused and host not in open_hosts:
                    selected_frames.add(member)

    entries = []
    drawn = set()
    for host, corners in (*relation_boxes, *boxes):
        try:
            marker = corners[0]
            key = (host.as_pointer(), round(marker.x, 2), round(marker.y, 2), round(marker.z, 2))
        except (ReferenceError, IndexError, TypeError):
            continue
        if key in drawn:
            continue
        try:
            mirrored = host.get("_gm_mirror_of")
            name = mirrored.name if mirrored is not None else host.name
            current_level = host == open_leaf
            is_active = host.session_uid == focused or (host == active_object and host in selected_objects)
            editing = host in open_hosts or mirrored in open_hosts
        except ReferenceError:
            continue
        drawn.add(key)
        color = ancestor_gray
        if is_active:
            color = _object_color(context, True)
        elif host in selected_frames:
            color = _object_color(context, False)
        elif current_level:
            color = _LEVEL_MEMBER_COLOR
        elif editing:
            color = _object_color(context, False)
        elif host in level_groups:
            color = _LEVEL_MEMBER_COLOR
        entries.append(((current_level, is_active), host, name, is_active, editing, corners, color))
    labels.parents = parents
    buttons = _overlay_buttons[pointer]
    _draw_plain_object_frames(context, region, rv3d, plain, style, ui_scale, buttons,
                              labels, level_objects, selected_frames=selected_frames)
    entries.sort(key=lambda entry: (-depths.get(entry[1], len(route)), entry[0]))
    for _order, host, name, is_active, editing, corners, color in entries:
        rect = _draw_screen_box(region, rv3d, corners, color, host, name, is_active, editing,
                                style, ui_scale, buttons, group_mode, labels=labels,
                                frame_style='FRAME' if host == open_leaf else
                                            'DASHED' if route and host == route[0] else None)
        if host == open_leaf and rect is not None and (style.show_2d or style.show_3d):
            _open_frame_rects[pointer] = (host, rect)

    # Reserve ancestor names first, independently of frame draw order.
    if labels.pick:
        labels.pick.draw_missing(context, style, labels)
    labels.draw(buttons, depths)
    _ensure_controls()


def _draw_plain_object_frames(context, region, rv3d, boxes, style, ui_scale, buttons,
                              labels=None, level_objects=(), *, selected_frames=None):
    active_object = getattr(context, "active_object", None)
    for host, corners in boxes:
        try:
            owner = host.get("_gm_mirror_of") or host
            name = owner.name
            selected = host in selected_frames if selected_frames is not None else host.select_get(view_layer=context.view_layer)
            highlighted = selected and host == active_object
        except ReferenceError:
            continue
        color = _LEVEL_MEMBER_COLOR if host in level_objects else (0.55, 0.55, 0.55)
        if selected:
            color = _object_color(context, highlighted)
        _draw_screen_box(region, rv3d, corners, color, host, name,
                         highlighted, False, style, ui_scale, buttons, True, labels=labels)


def _button_hit(region, x, y):
    for left, bottom, width, height, action, host in reversed(_overlay_buttons.get(region.as_pointer(), ())):
        if left <= x <= left + width and bottom <= y <= bottom + height:
            return action, host
    return None, None


def _group_extent_points(host, placement=None):
    """World corners of the objects in a group, not the single surrounding box."""
    source = hosts.definition(host)
    if source is None or host is None:
        return None
    key = (bpy.context.scene.session_uid, host.as_pointer())
    if placement is not None:
        key += (placement.copy().freeze(),)
    cached = _extent_cache.get(key)
    if cached is not None:
        return cached
    from .picking import _corners
    index = _shapes(bpy.context)
    placed = placement if placement is not None else _placement_matrix(host, source, index._depsgraph)
    points = []

    def walk(shape, matrix, seen):
        for _obj, local, bounds in shape.leaves:
            points.extend(matrix @ local @ corner for corner in _corners(bounds))
        for _obj, child, local in shape.children:
            if child in seen:
                continue
            walk(index._shape(child, seen | {child}), matrix @ local, seen | {child})

    walk(index._shape(source, {source}), placed, {source})
    _extent_cache[key] = points
    return points


def _aabb_center(points):
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    zs = [point.z for point in points]
    from mathutils import Vector
    return Vector((
        (min(xs) + max(xs)) * 0.5,
        (min(ys) + max(ys)) * 0.5,
        (min(zs) + max(zs)) * 0.5,
    ))


def _rect_area(limits):
    return max(0.0, limits[2] - limits[0]) * max(0.0, limits[3] - limits[1])


def _padded_rect(rect, pad):
    return (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)


def _screen_limits(points):
    if not points:
        return None
    return (min(point.x for point in points), min(point.y for point in points),
            max(point.x for point in points), max(point.y for point in points))


def _polyline_shader():
    global _line_shader
    if _line_shader is None:
        import gpu
        _line_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    return _line_shader


def _draw_screen_box(region, rv3d, corners, color, host, name, active, editing, style, ui_scale, buttons, group_mode, *, placement=None, labels=None, frame_style=None):
    """Viewport-aligned rectangle around the group's world bounds, with its name above."""
    from bpy_extras import view3d_utils
    import gpu
    from gpu_extras.batch import batch_for_shader

    points = [view3d_utils.location_3d_to_region_2d(region, rv3d, corner) for corner in corners]
    projected = [point for point in points if point is not None]
    if not projected:
        return
    pad = 4.0
    box_limits = _screen_limits(projected)
    flat_limits = box_limits
    if style is not None and style.show_2d and style.fit_2d == "GROUP":
        extent = _group_extent_points(host, placement)
        if extent:
            extent_points = [view3d_utils.location_3d_to_region_2d(region, rv3d, corner) for corner in extent]
            extent_points = [point for point in extent_points if point is not None]
            extent_limits = _screen_limits(extent_points)
            overlaps = extent_limits and not (
                extent_limits[2] < box_limits[0] - 30 or box_limits[2] < extent_limits[0] - 30
                or extent_limits[3] < box_limits[1] - 30 or box_limits[3] < extent_limits[1] - 30
            )
            if overlaps and _rect_area(extent_limits) <= _rect_area(box_limits) * 1.05:
                flat_limits = extent_limits
            elif corners:
                delta = _aabb_center(corners) - _aabb_center(extent)
                moved = [point + delta for point in extent]
                moved_points = [view3d_utils.location_3d_to_region_2d(region, rv3d, point) for point in moved]
                moved_points = [point for point in moved_points if point is not None]
                moved_limits = _screen_limits(moved_points)
                if moved_limits:
                    flat_limits = moved_limits
    if style and (style.show_2d or style.show_3d):
        gap = style.hierarchy_gap * max(0.8, ui_scale)
        source = hosts.definition(host)
        if source:
            # A group's nesting depth is independent of which levels are open.
            depth = _shapes(bpy.context)._shape(source, {source}).frame_depth
            margin = gap * depth
            if style.show_2d and style.fit_2d == 'GROUP':
                pad += margin
    box_fit = style and style.show_2d and style.fit_2d == 'BOX'
    if box_fit or (style and style.show_3d):
        padded = _padded_box_corners(corners, region, rv3d, 4.0)
        points = [view3d_utils.location_3d_to_region_2d(region, rv3d, point) for point in padded]
        visible = [point for point in points if point is not None]
        if source:
            visible, original_count = _clipped_box_projection(padded, points, region, rv3d)
            visible, originals = _group_frame_projection(visible, original_count, margin)
    if box_fit and visible:
        # Fit to the actual expanded 3D frame, even when only its 2D overlay is shown.
        frame_rect = _padded_rect(_screen_limits(visible), max(4.0, 5.0 * ui_scale, style.width))
    else:
        frame_rect = _padded_rect(flat_limits, pad)
    flat_min_x, flat_min_y, flat_max_x, flat_max_y = frame_rect
    label_x, label_y = flat_min_x, flat_max_y
    if style and style.show_3d and not style.show_2d and visible:
        frame_rect = _screen_limits(visible)
        left, _, _, top = frame_rect
        anchor = min(visible, key=lambda p: (p.x - left) ** 2 + (p.y - top) ** 2)
        label_x, label_y = anchor.x, anchor.y
    immediate = labels is None
    if immediate and style and style.show_info:
        labels = _FrameLabels(region, 3.0 + style.width / 2)
    if labels and labels.pick and labels.pick.matches(host, corners):
        from .pick_outline import COLOR
        color = COLOR[:3]
    tint = (*color, 1.0)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    if group_mode and style is not None and (style.show_3d or style.show_2d):
        lines = (
            (flat_min_x, flat_min_y, 0.0), (flat_max_x, flat_min_y, 0.0),
            (flat_max_x, flat_min_y, 0.0), (flat_max_x, flat_max_y, 0.0),
            (flat_max_x, flat_max_y, 0.0), (flat_min_x, flat_max_y, 0.0),
            (flat_min_x, flat_max_y, 0.0), (flat_min_x, flat_min_y, 0.0),
        )
        shader = _polyline_shader()
        batch = batch_for_shader(shader, 'LINES', {"pos": lines})
        shader.bind()
        shader.uniform_float("color", tint)
        shader.uniform_float("viewportSize", (region.width, region.height))
        shader.uniform_float("lineWidth", style.width)
        if style.show_3d:
            box_style = frame_style or style.style_3d
            if source:
                tick = style.corner_3d * ui_scale if box_style == 'CORNERS' else None
                solid = _group_outline_lines(visible, originals, tick)
            elif box_style in {'FRAME', 'DASHED'}:
                solid = _screen_edge_lines(points)
            else:
                solid = _screen_corner_marks(points, style.corner_3d * ui_scale)
            if solid:
                if labels is not None and style.show_info:
                    labels.strokes(solid)
                visible_lines = _dashed_lines(solid, region, ui_scale) if box_style == 'DASHED' else solid
                if visible_lines:
                    batch_for_shader(shader, 'LINES', {"pos": visible_lines}).draw(shader)
        if style.show_2d:
            flat_style = frame_style or style.style_2d
            if flat_style == 'FRAME':
                if labels is not None and style.show_info:
                    labels.strokes(lines)
                batch.draw(shader)
            else:
                if flat_style == 'DASHED':
                    flat = _dashed_lines(lines, region, ui_scale)
                else:
                    flat = _rect_corner_marks(flat_min_x, flat_min_y, flat_max_x, flat_max_y, style.corner_2d * ui_scale)
                if flat:
                    if labels is not None and style.show_info:
                        labels.strokes(lines if flat_style == 'DASHED' else flat)
                    batch_for_shader(shader, 'LINES', {"pos": flat}).draw(shader)
    if group_mode and style is not None and style.show_info:
        labels.frames[host] = frame_rect
        labels.pending.append((host, name, tint, label_x, label_y))
        if immediate:
            labels.draw(buttons)
    gpu.state.blend_set('NONE')
    return frame_rect


def _draw_frame_name(layout, host, name, tint, flat_min_x, flat_max_y, buttons):
    import blf
    blf.size(0, 15)
    blf.color(0, *tint)
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    region = layout.region
    label = name
    name_width, name_height = blf.dimensions(0, label)
    owner = host.get("_gm_mirror_of") or host
    definition = hosts.definition(owner)
    icon = _collection_icon if definition else _type_icons.get(owner.type)
    if _icon_font < 0:
        icon = None
    icon_space = 18 if icon else 0
    label_width = icon_space + name_width
    # Include the icon, descenders and font shadow below the baseline.
    label_height = max(name_height, 16.0) + 4.0
    text_x, bottom = layout.place(host, flat_min_x, flat_max_y + 4.0, label_width, label_height)
    text_y = bottom + 4.0
    if icon:
        blf.size(_icon_font, 15)
        blf.color(_icon_font, *tint)
        blf.position(_icon_font, text_x, text_y - 1, 0)
        blf.draw(_icon_font, icon)
    blf.position(0, text_x + icon_space, text_y, 0.0)
    blf.draw(0, label)
    blf.disable(0, blf.SHADOW)
    if _click_names(bpy.context) and (definition or path(bpy.context)):
        buttons.append((region.x + text_x, region.y + bottom, label_width, label_height, 'ENTER', owner))


class GM_OT_edit(bpy.types.Operator):
    bl_idname = "groupmanager.edit"
    bl_label = "Edit"
    bl_description = ("Edit the selected groups in place. The last selected group stays active, "
                      "and Add puts objects into that group")
    bl_options = {'REGISTER', 'UNDO'}

    unique: BoolProperty(name="Make Single User and Edit", default=False)

    @classmethod
    def poll(cls, context):
        return bool(context.window and context.mode == 'OBJECT' and hosts.definition(context.active_object))

    def execute(self, context):
        global _mesh_session_opened_group
        _mesh_session_opened_group = False
        try:
            active = context.active_object
            current = path(context)
            selected = []
            seen = set()
            for obj in context.selected_objects:
                if obj is None or obj in seen or not hosts.definition(obj):
                    continue
                seen.add(obj)
                selected.append(obj)
            if active not in selected and hosts.definition(active):
                selected.append(active)
            if current and not self.unique and not _owns(current[-1], active):
                self._open_alongside(context, selected)
                return {'FINISHED'}
            parent = []
            targets = selected
            if current and _owns(current[-1], active):
                parent = list(current)
                targets = [obj for obj in selected if _owns(current[-1], obj)] or [active]
            chosen = {}
            order = []
            for obj in targets:
                source = hosts.definition(obj)
                if source is None:
                    continue
                if source not in chosen:
                    order.append(source)
                if source not in chosen or obj == active:
                    chosen[source] = obj
            if not chosen:
                raise ValueError("Select a group instance first")
            primary = chosen.get(hosts.definition(active), chosen[order[0]])
            others = [chosen[source] for source in order if chosen[source] != primary]
            saved = list(peers(context))
            close_peers(context)
            try:
                open_path(context, parent + [primary], self.unique)
            except Exception:
                for host in saved:
                    try:
                        open_peer(context, host)
                    except (ValueError, RuntimeError):
                        pass
                raise
            if not self.unique:
                warnings = []
                for obj in others:
                    try:
                        open_peer(context, obj)
                    except (ValueError, RuntimeError) as exc:
                        warnings.append(str(exc))
                for obj in targets:
                    if obj == primary or obj in peers(context):
                        continue
                    try:
                        open_peer(context, obj)
                    except (ValueError, RuntimeError) as exc:
                        warnings.append(str(exc))
                if warnings:
                    self.report({'WARNING'}, warnings[0])
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

    def _open_alongside(self, context, selected):
        """Open more groups without leaving the ones already being edited."""
        warnings = []
        route = path(context)
        open_hosts = [route[-1], *peers(context)]
        for obj in selected:
            source = hosts.definition(obj)
            if source is None or obj in open_hosts:
                continue
            try:
                open_peer(context, obj)
            except (ValueError, RuntimeError) as exc:
                warnings.append(str(exc))
                continue
            open_hosts.append(obj)
        if warnings:
            self.report({'WARNING'}, warnings[0])


def _selected_groups(context):
    found = []
    seen = set()
    active = getattr(context, "active_object", None)
    objects = list(getattr(context, "selected_objects", None) or [])
    if active is not None:
        objects.append(active)
    for obj in objects:
        if obj is None or obj in seen:
            continue
        source = hosts.definition(obj)
        if not source or source.library or source.override_library:
            continue
        seen.add(obj)
        found.append(obj)
    return found


def _open_group_hosts(context):
    route = path(context)
    if not route or not route[-1]:
        return []
    found = []
    for host in (route[-1], *peers(context)):
        try:
            if host is not None and host not in found:
                found.append(host)
        except ReferenceError:
            continue
    return found


def _mode_targets(context):
    """Groups that share Blender's mode. Several selected groups are edited together."""
    obj = getattr(context, "active_object", None)
    if getattr(context, "mode", None) == 'OBJECT':
        selected = _selected_groups(context)
        if selected:
            return selected
        if path(context) and (owner_host(context, obj) is not None or
                              (obj is None and any(owner_host(context, item) for item in context.selected_objects))):
            return _open_group_hosts(context)
        return []
    if path(context) and (owner_host(context, obj) is not None or hosts.definition(obj)):
        return _open_group_hosts(context)
    return []


def _editable_meshes(context, source):
    layer = _layer(context.scene, context)
    found = []
    for obj in content(source).all_objects:
        try:
            editable = (not is_helper(obj) and obj.type == 'MESH' and obj.data is not None
                        and obj.library is None and obj.override_library is None
                        and obj.data.library is None
                        and layer.objects.get(obj.name) == obj
                        and not obj.hide_get(view_layer=layer)
                        and not obj.hide_select)
        except ReferenceError:
            continue
        if editable:
            found.append(obj)
    found.sort(key=lambda obj: obj.name)
    return found


_GROUP_MODES = ('OBJECT', 'EDIT', 'SCULPT', 'VERTEX_PAINT', 'WEIGHT_PAINT', 'TEXTURE_PAINT')
_mode_pie_draw = None


def _expose_groups(context, selected_hosts):
    """Open every requested group and keep the ones already open."""
    for host in selected_hosts:
        source = hosts.definition(host)
        if not source:
            continue
        if edit_layer.is_open(source):
            route = path(context)
            owner = edit_layer.open_host(source)
            if host != owner and (not route or host != route[-1]) and host not in peers(context):
                try:
                    open_peer(context, host)
                except (ValueError, RuntimeError):
                    pass
            continue
        if not path(context):
            open_path(context, [host])
        elif _chain_inside(path(context)[-1], host):
            open_path(context, _jump_path(context, host))
        else:
            _open_from_button(context, host)


def _session_meshes(context):
    seen = set()
    meshes = []
    opened = set()
    for host in _open_group_hosts(context):
        source = hosts.definition(host)
        if not source or source in opened or not edit_layer.is_open(source):
            continue
        opened.add(source)
        for obj in _editable_meshes(context, source):
            if obj not in seen:
                seen.add(obj)
                meshes.append(obj)
    return meshes


def _end_nested_mesh_edit(context):
    scene = context.scene
    state = scene.get(_MESH_EDIT)
    if state is None:
        return
    layer = context.view_layer
    for host in reversed(_read(state, "opened")):
        if host and host in peers(context):
            close_peer(context, host)
    if state.get("child_level") and len(path(context)) > state["start_depth"]:
        back(context, steps=len(path(context)) - state["start_depth"])
    if state["from_scene"] and path(context):
        back(context, all_levels=True)
    for obj in list(layer.objects.selected):
        obj.select_set(False, view_layer=layer)
    for obj in _read(state, "selection"):
        if obj and layer.objects.get(obj.name) == obj:
            obj.select_set(True, view_layer=layer)
    active = state.get("active")
    layer.objects.active = active if active and layer.objects.get(active.name) == active else None
    del scene[_MESH_EDIT]


def _watch_mesh_edit_exit():
    # Native Object Mode and Tab can leave multi-object Edit Mode directly.
    pending = False
    for wm in bpy.data.window_managers:
        for window in wm.windows:
            if _MESH_EDIT not in window.scene:
                continue
            with bpy.context.temp_override(window=window):
                if bpy.context.mode == 'OBJECT':
                    _end_nested_mesh_edit(bpy.context)
                else:
                    pending = True
    return 0.15 if pending else None


def _edit_nested_meshes(context):
    scene = context.scene
    layer = context.view_layer
    preferred = context.active_object
    # Opening a subgroup changes the viewport selection. Capture the user's
    # actual edit targets before exposing any of its members.
    targets = sorted(context.selected_objects, key=lambda obj: obj != preferred)
    scene[_MESH_EDIT] = {"from_scene": not path(context)}
    state = scene[_MESH_EDIT]
    _write(state, "selection", context.selected_objects)
    if preferred:
        state["active"] = preferred
    opened = []
    meshes = []
    seen = set()

    def visit(host):
        source = hosts.definition(host)
        if source in seen or source.library or source.override_library:
            return
        seen.add(source)
        if not edit_layer.is_open(source):
            if not path(context):
                open_path(context, [host])
            else:
                open_peer(context, host)
                opened.append(host)
                _write(state, "opened", opened)
        for obj in sorted(content(source).all_objects, key=lambda obj: obj.name):
            if is_helper(obj) or obj.hide_select or obj.library or obj.override_library:
                continue
            child = hosts.definition(obj)
            if child and edit_layer.open_host(child) == obj:
                visit(obj)
            elif not obj.visible_get(view_layer=layer):
                continue
            elif child:
                visit(obj)
            elif obj.type == 'MESH' and obj.data and obj.data.is_editable:
                meshes.append(obj)

    try:
        route = path(context)
        if (route and len(targets) == 1 and hosts.definition(preferred)
                and _owns(route[-1], preferred) and not edit_layer.is_open(hosts.definition(preferred))):
            state["start_depth"] = len(route)
            state["child_level"] = True
            open_path(context, [*route, preferred])
        for obj in targets:
            if hosts.definition(obj):
                visit(obj)
            elif (obj.type == 'MESH' and not is_helper(obj) and obj.is_editable
                  and obj.data and obj.data.is_editable and obj.visible_get(view_layer=layer)):
                meshes.append(obj)
        if not meshes:
            raise ValueError("No visible editable mesh inside the selected groups")
        for obj in list(layer.objects.selected):
            obj.select_set(False, view_layer=layer)
        for obj in meshes:
            obj.select_set(True, view_layer=layer)
        layer.objects.active = preferred if preferred in meshes else meshes[0]
        if bpy.ops.object.mode_set(mode='EDIT') != {'FINISHED'} or context.mode != 'EDIT_MESH':
            raise ValueError("Blender could not enter mesh Edit Mode")
    except Exception:
        _end_nested_mesh_edit(context)
        raise
    if not bpy.app.timers.is_registered(_watch_mesh_edit_exit):
        bpy.app.timers.register(_watch_mesh_edit_exit, first_interval=0.15)


def _enter_member_edit(context, mode='EDIT'):
    """Open the chosen groups, then switch all of their meshes into one Blender mode."""
    global _mesh_session_opened_group
    if _MESH_EDIT in context.scene:
        if mode == 'EDIT' and context.mode == 'EDIT_MESH':
            return
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        _end_nested_mesh_edit(context)
        if mode == 'OBJECT':
            return
    if mode == 'OBJECT':
        leaving_mesh = context.mode != 'OBJECT'
        close_group = leaving_mesh and _mesh_session_opened_group
        if leaving_mesh and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        if close_group and path(context):
            back(context, all_levels=True)
        return
    targets = _mode_targets(context)
    if not targets:
        raise ValueError("Select a group instance first")
    if context.mode != 'OBJECT':
        if not bpy.ops.object.mode_set.poll():
            raise RuntimeError("Leave the current mode before editing these groups")
        bpy.ops.object.mode_set(mode='OBJECT')
    if mode == 'EDIT':
        _edit_nested_meshes(context)
        return
    if not path(context):
        _mesh_session_opened_group = True
    _expose_groups(context, targets)
    meshes = _session_meshes(context)
    if not meshes:
        raise ValueError("This group has no editable meshes")
    layer = _layer(context.scene, context)
    active = context.active_object if context.active_object in meshes else meshes[0]
    for obj in list(layer.objects.selected):
        obj.select_set(False, view_layer=layer)
    for obj in meshes:
        obj.select_set(True, view_layer=layer)
    layer.objects.active = active
    try:
        bpy.ops.object.mode_set(mode=mode)
    except RuntimeError:
        if mode == 'EDIT':
            raise
        for obj in list(layer.objects.selected):
            if obj != active:
                obj.select_set(False, view_layer=layer)
        bpy.ops.object.mode_set(mode=mode)


class _GroupModeLayout(menu_hooks.Layout):
    def menu_pie(self):
        return _GroupModeLayout(self._layout.menu_pie())

    def operator_enum(self, operator, prop, *args, **kwargs):
        if (operator, prop) != ("object.mode_set", "mode"):
            return self._layout.operator_enum(operator, prop, *args, **kwargs)
        modes = bpy.types.Object.bl_rna.properties["mode"]
        for identifier in _GROUP_MODES:
            item = modes.enum_items[identifier]
            text = bpy.app.translations.pgettext_iface(item.name, modes.translation_context)
            self._layout.operator("groupmanager.set_mode", text=text, icon=item.icon).mode = identifier


def _geo_scatter_mode_draw(draw):
    return (draw.__module__.endswith(".geo_scatter.gpl_script.ui.ui_pie")
            and draw.__name__ == "new_object_mode_pie_draw")


def _install_mode_pie():
    global _mode_pie_draw
    _mode_pie_draw = menu_hooks.install(
        bpy.types.VIEW3D_MT_object_mode_pie, _GroupModeLayout, _mode_targets,
        compatible_draw=_geo_scatter_mode_draw,
    )


def _remove_mode_pie():
    global _mode_pie_draw
    menu_hooks.restore(_mode_pie_draw)
    _mode_pie_draw = None


class GM_OT_set_mode(bpy.types.Operator):
    bl_idname = "groupmanager.set_mode"
    bl_label = "Set Group Mode"
    bl_description = "Edit the selected meshes and selected groups' contents, keeping the active mesh"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=tuple((identifier, identifier.replace('_', ' ').title(), "") for identifier in _GROUP_MODES),
        default='EDIT',
    )

    @classmethod
    def poll(cls, context):
        return bool(_mode_targets(context))

    def execute(self, context):
        if self.mode != 'OBJECT' and not _mode_targets(context):
            return {'CANCELLED'}
        try:
            _enter_member_edit(context, self.mode)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_bounds_controls(bpy.types.Operator):
    bl_idname = "groupmanager.bounds_controls"
    bl_label = "Edit Bounds Controls"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _controls_running
        if _controls_running:
            return {'CANCELLED'}
        _controls_running = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        global _controls_running, _pending_object_click, _pending_name_click, _pending_empty_clicks, _pending_group_clicks, _pending_level_clicks
        if not _overlay_live(context):
            _controls_running = False
            _overlay_buttons.clear()
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            _overlay_buttons.clear()
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'DOUBLE_CLICK'}:
            region = _view3d_under_mouse(event)
            if event.shift or event.ctrl or region is None:
                _pending_name_click = None
                _pending_object_click = None
                _pending_empty_clicks = None
                _pending_group_clicks = None
                _pending_level_clicks = None
                return {'PASS_THROUGH'}
            if _click_names(context):
                _, host = _button_hit(region, event.mouse_x, event.mouse_y)
                try:
                    area = next(a for a in context.window.screen.areas if region in a.regions[:])
                    with context.temp_override(area=area, region=region):
                        if _handle_name_press(context, host, (region.as_pointer(), event.mouse_x, event.mouse_y)):
                            return {'RUNNING_MODAL'}
                except (ValueError, RuntimeError, ReferenceError):
                    _pending_name_click = None
        return {'PASS_THROUGH'}


def _empty_outside_group(context, event):
    from bpy_extras import view3d_utils
    from .picking import _PickIndex

    region = context.region
    view = region.data or context.region_data
    host = path(context)[-1]
    xy = (event.mouse_x - region.x, event.mouse_y - region.y)
    cached_host, rect = _open_frame_rects.get(region.as_pointer(), (None, None))
    if cached_host != host:
        corners = _bound_corners(_shapes(context), host, hosts.definition(host))
        projected = [view3d_utils.location_3d_to_region_2d(region, view, point) for point in corners]
        rect = _screen_limits([point for point in projected if point is not None])
    if rect and rect[0] <= xy[0] <= rect[2] and rect[1] <= xy[1] <= rect[3]:
        return False
    if _button_hit(region, event.mouse_x, event.mouse_y)[0]:
        return False
    # Include selection-locked surfaces as well as wire and point objects.
    origin = view3d_utils.region_2d_to_origin_3d(region, view, xy)
    direction = view3d_utils.region_2d_to_vector_3d(region, view, xy)
    hit, _, _, _, obj, _ = context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction)
    if hit and obj.visible_get(view_layer=context.view_layer, viewport=context.space_data):
        return False
    _, members = _PickIndex.from_view(context).at_cursor(region, view, event)
    return not members


class GM_OT_back(bpy.types.Operator):
    bl_idname = "groupmanager.back"
    bl_label = "Back"
    bl_description = "Go back one level. Shift-click to return directly to the scene"
    bl_options = {'REGISTER', 'UNDO'}

    steps: IntProperty(name="Levels", default=1, min=1, options={'SKIP_SAVE'})
    all: BoolProperty(name="Return to Scene", default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return bool(context.window and path(context))

    def invoke(self, context, event):
        global _pending_name_click
        if event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
            # Blender forwards clicks through the clear part of overlapping sidebars.
            # Use its dispatched region so those pixels remain part of the viewport.
            if (context.mode != 'OBJECT' or not _click_objects(context)
                    or not context.area or context.area.type != 'VIEW_3D'
                    or not context.region or context.region.type != 'WINDOW'):
                return {'PASS_THROUGH'}
            _pending_name_click = None
            if not _empty_outside_group(context, event):
                return {'PASS_THROUGH'}
        if event.shift:
            self.all = True
        return self.execute(context)

    def execute(self, context):
        try:
            back(context, self.steps, self.all)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_object_mode(bpy.types.Operator):
    bl_idname = "groupmanager.object_mode"
    bl_label = "Object Mode"
    bl_description = "Leave Group Mode and return to Object Mode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.window and context.mode == 'OBJECT')

    def execute(self, context):
        if not path(context):
            return {'FINISHED'}
        try:
            back(context, all_levels=True)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_up_level(bpy.types.Operator):
    bl_idname = "groupmanager.up_level"
    bl_label = "Up Level"
    bl_description = "Go up from the selected group's level"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.window and context.mode == 'OBJECT' and _up_destination(context))

    def execute(self, context):
        parent = _up_destination(context)
        if not parent:
            return {'CANCELLED'}
        try:
            open_path(context, parent)
            _redraw()
        except (ValueError, RuntimeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class GM_OT_down_level(bpy.types.Operator):
    bl_idname = "groupmanager.down_level"
    bl_label = "Down Level"
    bl_description = "Enter the selected group's level"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.window and context.mode == 'OBJECT' and _down_level_target(context))

    def execute(self, context):
        try:
            if not _click_down(context):
                return {'CANCELLED'}
        except (ValueError, RuntimeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (GM_OT_edit, GM_OT_set_mode, GM_OT_back, GM_OT_object_mode,
           GM_OT_up_level, GM_OT_down_level, GM_OT_bounds_controls)


_msgbus_owner = object()


_redirecting_mode = False
_MODE_FROM_CONTEXT = {
    'EDIT_MESH': 'EDIT',
    'SCULPT': 'SCULPT',
    'PAINT_VERTEX': 'VERTEX_PAINT',
    'PAINT_WEIGHT': 'WEIGHT_PAINT',
    'PAINT_TEXTURE': 'TEXTURE_PAINT',
}


def _redirect_group_mode():
    """The viewport mode button edits the carrier mesh. Use the group's members instead."""
    global _redirecting_mode
    if _redirecting_mode:
        return
    context = bpy.context
    obj = getattr(context, "active_object", None)
    target = _MODE_FROM_CONTEXT.get(getattr(context, "mode", None))
    if obj is None or target is None or _MESH_EDIT in context.scene:
        return
    if not hosts.definition(obj) and not (target == 'EDIT' and owner_host(context, obj)
                                         and any(hosts.definition(item) for item in context.selected_objects)):
        return
    _redirecting_mode = True
    try:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        _enter_member_edit(context, target)
    except (ValueError, RuntimeError, ReferenceError):
        pass
    finally:
        _redirecting_mode = False


def _on_active_changed():
    context = bpy.context
    if getattr(context, "mode", None) != 'OBJECT' or (context.scene and _MESH_EDIT in context.scene):
        return
    try:
        follow_selection(context)
    except (AttributeError, ReferenceError, RuntimeError):
        return
    _redraw()


def register():
    global _draw_handle, _icon_font
    _install_mode_pie()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_post):
        handlers.append(_close_editors)
    bpy.app.handlers.depsgraph_update_post.append(_invalidate_bounds)
    bpy.msgbus.subscribe_rna(key=(bpy.types.LayerObjects, "active"), owner=_msgbus_owner,
                             args=(), notify=_on_active_changed)
    bpy.msgbus.subscribe_rna(key=(bpy.types.Object, "mode"), owner=_msgbus_owner,
                             args=(), notify=_redirect_group_mode)
    if not bpy.app.background:
        import blf
        _icon_font = blf.load(_icon_path)
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_bounds, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _draw_handle, _pending_name_click, _icon_font
    _remove_mode_pie()
    if bpy.app.timers.is_registered(_watch_mesh_edit_exit):
        bpy.app.timers.unregister(_watch_mesh_edit_exit)
    _close_editors()
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if _icon_font >= 0:
        import blf
        blf.unload(_icon_path)
        _icon_font = -1
    bpy.app.handlers.depsgraph_update_post.remove(_invalidate_bounds)
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_post):
        handlers.remove(_close_editors)
    _drop_bounds()
    _pick_frames.clear()
    _overlay_buttons.clear()
    _open_frame_rects.clear()
    _label_focus.clear()
    if bpy.app.timers.is_registered(_finish_empty_click):
        bpy.app.timers.unregister(_finish_empty_click)
    _pending_name_click = None
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
