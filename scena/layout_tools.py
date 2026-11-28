"""Editable layouts whose generated slots remain ordinary, shared Blender placements."""

import math

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty
from mathutils import Matrix, Vector

from . import editor
from .exchange_ids import copy_block
from .hosts import definition


SETTINGS = "_gm_layout_settings"
SLOTS = "_gm_layout_slots"
INDEX = "_gm_layout_index"
EXCLUDED = "_gm_layout_excluded"

MODES = (
    ("LINEAR", "Linear", "Evenly space Link Groups along a local axis"),
    ("RADIAL", "Radial", "Arrange Link Groups in a ring around a local axis, starting here"),
    ("MIRROR", "Mirror", "Add one mirrored Link Group across a local plane"),
)
AXES = tuple((axis, axis, "") for axis in "XYZ")


def _slots(root):
    """ID pointers survive renaming; check parenting to disambiguate duplicated seeds."""
    saved = root.get(SLOTS, {})
    return {
        int(index): obj
        for index, obj in saved.items()
        if isinstance(obj, bpy.types.Object) and obj.parent == root
    }


def _owner(obj):
    if obj and obj.parent and INDEX in obj:
        slots = obj.parent.get(SLOTS, {})
        if slots.get(str(obj[INDEX])) == obj:
            return obj.parent
    return None


def _holes(root):
    return {int(i) for i in root.get(EXCLUDED, "").split(",") if i}


def _slot_matrix(mode, axis, index, count, distance, radius):
    normal = Vector([float(a == axis) for a in "XYZ"])
    if mode == "LINEAR":
        return Matrix.Translation(normal * distance * index)
    if mode == "MIRROR":
        return Matrix.Translation(normal * (2 * distance)) @ Matrix.Scale(-1, 4, normal)
    transverse = Vector((0, 1, 0)) if axis == "X" else Vector((1, 0, 0))
    center = -radius * transverse
    turn = Matrix.Rotation(math.tau * index / count, 4, axis)
    return Matrix.Translation(center) @ turn @ Matrix.Translation(-center)


def _ensure_writable(root):
    if not root or not root.is_editable or root.override_library:
        raise ValueError("This Link Group is read-only. Make a local version first.")
    if not root.users_collection or any(not coll.is_editable or coll.override_library
                                        for coll in root.users_collection):
        raise ValueError("The Link Group's collection must be editable.")


def _removal_check(root, objects):
    if not objects:
        return
    users = bpy.data.user_map(subset=set(objects))
    containers = set(root.users_collection)
    # The ID owning a scene's master collection is the Scene itself.
    containers.update(scene for scene in bpy.data.scenes if scene.collection in containers)
    for obj in objects:
        _ensure_writable(obj)
        outside = users[obj] - containers - {root}
        if outside or obj.children or obj.constraints or obj.animation_data:
            raise ValueError(f'"{obj.name}" has other users or animation. Extract this slot first.')
        if definition(obj) != definition(root):
            raise ValueError(f'"{obj.name}" uses another group definition. Extract this slot first.')


def _new_slot(root, index):
    # A linked Object copy keeps the placement's geometry node inputs and modifiers.
    obj = copy_block(root)
    try:
        obj.name = f"{root.name} · {index + 1:02d}"
        for prop in (SETTINGS, SLOTS, EXCLUDED):
            if prop in obj:
                del obj[prop]
        obj[INDEX] = index
        obj.animation_data_clear()
        obj.constraints.clear()
        obj.parent_type = 'OBJECT'
        obj.parent = root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.delta_location = (0, 0, 0)
        obj.delta_rotation_euler = (0, 0, 0)
        obj.delta_rotation_quaternion = (1, 0, 0, 0)
        obj.delta_scale = (1, 1, 1)
        for coll in root.users_collection:
            coll.objects.link(obj)
        return obj
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise


def update_pattern(root, mode, axis, count, spacing, radius):
    """Update existing slots in place and recycle no extracted indices."""
    _ensure_writable(root)
    if not definition(root):
        raise ValueError("Select a Link Group.")
    if mode not in {item[0] for item in MODES} or axis not in "XYZ" or len(axis) != 1:
        raise ValueError("The array type or axis is invalid.")
    count = 2 if mode == "MIRROR" else int(count)
    if count < 1 or radius < 0 or not all(map(math.isfinite, (spacing, radius))):
        raise ValueError("Enter a valid count, distance, and radius.")
    current = _slots(root)
    desired = set(range(1, count)) - _holes(root)
    dropped = [obj for idx, obj in current.items() if idx not in desired]
    _removal_check(root, dropped)
    matrices = {i: _slot_matrix(mode, axis, i, count, spacing, radius) for i in desired}
    for i in desired & current.keys():
        _ensure_writable(current[i])

    created = {}
    previous = {i: obj.matrix_basis.copy() for i, obj in current.items() if i in desired}
    old_settings = dict(root[SETTINGS]) if SETTINGS in root else None
    had_slots = SLOTS in root
    try:
        for i in sorted(desired - current.keys()):
            created[i] = _new_slot(root, i)
        slots = {i: current[i] if i in current else created[i] for i in desired}
        for i, obj in slots.items():
            obj.matrix_basis = matrices[i]
        root[SETTINGS] = dict(mode=mode, axis=axis, count=count, spacing=spacing, radius=radius)
        root[SLOTS] = {str(i): obj for i, obj in slots.items()}
    except Exception:
        for i, mat in previous.items():
            current[i].matrix_basis = mat
        if old_settings is not None:
            root[SETTINGS] = old_settings
        elif SETTINGS in root:
            del root[SETTINGS]
        if had_slots:
            root[SLOTS] = {str(i): obj for i, obj in current.items()}
        elif SLOTS in root:
            del root[SLOTS]
        for obj in created.values():
            bpy.data.objects.remove(obj, do_unlink=True)
        raise
    for obj in dropped:
        bpy.data.objects.remove(obj, do_unlink=True)
    return slots


def extract_slot(obj):
    root = _owner(obj)
    if not root:
        raise ValueError("Select the array member to extract.")
    _ensure_writable(root)
    _ensure_writable(obj)
    parent = root.parent if root.parent_type == 'OBJECT' else None
    world = obj.matrix_world.copy()
    location, rotation, scale = world.decompose()
    basis = Matrix.LocRotScale(location, rotation, scale)
    residual = max(abs(world[r][c] - basis[r][c]) for r in range(4) for c in range(4))
    tolerance = 1e-5 * max(1, *(abs(v) for row in world.to_3x3() for v in row))
    if not parent and residual > tolerance:
        raise ValueError("This slot has shear. A separate object cannot store it. Fix the array root's non-uniform scale first.")
    if parent:
        try:
            parent_inverse = parent.matrix_world.inverted() @ world @ basis.inverted()
        except ValueError:
            raise ValueError("A parent has zero scale, so this world transform cannot be kept.") from None
    index = int(obj[INDEX])
    holes = _holes(root) | {index}
    remaining = _slots(root)
    del remaining[index]
    obj.parent = parent
    obj.matrix_parent_inverse = parent_inverse if parent else Matrix.Identity(4)
    obj.matrix_basis = basis
    del obj[INDEX]
    root[EXCLUDED] = ",".join(str(i) for i in sorted(holes))
    root[SLOTS] = {str(i): member for i, member in remaining.items()}


class GROUPMANAGER_OT_layout_pattern(bpy.types.Operator):
    bl_idname = "groupmanager.layout_pattern"
    bl_label = "Edit Layout"
    bl_description = "Create or update a shared array or mirror in the root Link Group's local space"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(name="Layout", items=MODES, default='LINEAR')
    axis: EnumProperty(name="Local Axis", items=AXES, default='X')
    count: IntProperty(name="Count (including start)", default=4, min=1, max=10000)
    spacing: FloatProperty(name="Spacing", default=2.0, subtype='DISTANCE')
    radius: FloatProperty(name="Radius", default=2.0, min=0.0, subtype='DISTANCE')

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return context.mode == 'OBJECT' and bool(definition(active)) and editor.in_scope(context, active)

    def invoke(self, context, event):
        active = context.active_object
        root = _owner(active) or active
        for prop, value in root.get(SETTINGS, {}).items():
            if not self.properties.is_property_set(prop):
                setattr(self, prop, value)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        layout.prop(self, "axis", expand=True)
        if self.mode != 'MIRROR':
            layout.prop(self, "count")
        if self.mode == 'RADIAL':
            layout.prop(self, "radius")
        elif self.mode == 'MIRROR':
            layout.prop(self, "spacing", text="Mirror Offset")
        else:
            layout.prop(self, "spacing")
        if editor.path(context):
            layout.label(text="This layout updates the current shared definition.", icon='INFO')

    def execute(self, context):
        root = _owner(context.active_object) or context.active_object
        try:
            with editor.source_space(context):
                updated = update_pattern(root, self.mode, self.axis, self.count, self.spacing, self.radius)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        root.select_set(True)
        context.view_layer.objects.active = root
        self.report({'INFO'}, f"Updated {len(updated)} generated Link Groups.")
        return {'FINISHED'}


class GROUPMANAGER_OT_layout_extract(bpy.types.Operator):
    bl_idname = "groupmanager.layout_extract"
    bl_label = "Extract This Slot"
    bl_description = "Keep this slot's world transform and shared definition so it can be moved or made unique"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return context.mode == 'OBJECT' and _owner(active) is not None and editor.in_scope(context, active)

    def execute(self, context):
        try:
            context.view_layer.update()
            slot = context.active_object
            with editor.source_space(context):
                extract_slot(slot)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def draw(layout, context):
    active = context.active_object
    root = _owner(active) or active if definition(active) else None
    if root and SETTINGS in root:
        info = root[SETTINGS]
        count = info["count"]
        omitted = sum(i < count for i in _holes(root))
        layout.label(text=f"Slots {count} · Generated {len(_slots(root))} · Extracted {omitted}")
    layout.operator("groupmanager.layout_pattern")
    layout.operator("groupmanager.layout_extract")


CLASSES = ()
