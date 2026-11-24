"""Blender extension entry point for Scena."""

from importlib import import_module

import bpy
from bpy.app.handlers import persistent

_MODULES = ("creation", "clipboard", "hotkeys", "preferences", "operations", "usage", "modeling", "picking", "hierarchy",
            "layout_tools", "exchange", "interchange", "origins", "tool_pies", "ui")
_registered_classes = []
_installed_hooks = []


def register():
    try:
        for name in _MODULES:
            module = import_module("." + name, __package__)
            for cls in module.CLASSES:
                bpy.utils.register_class(cls)
                _registered_classes.append(cls)
        for name in ("editor", "usage", "ui", "hotkeys", "modeling", "origins", "interchange", "instance_mirror", "modifier_inputs"):
            module = import_module("." + name, __package__)
            module.register()
            _installed_hooks.append(module)
        bpy.app.handlers.load_post.append(_refresh_groups)
        bpy.app.timers.register(_refresh_groups, first_interval=0.0)
    except Exception:
        unregister()
        raise


@persistent
def _refresh_groups(_unused=None):
    from . import hosts, instance_mirror, modifier_inputs
    hosts.refresh_modifier_names()
    hosts.refresh_definition_names()
    instance_mirror.upgrade_graphs()
    for obj in bpy.data.objects:
        instance_mirror.migrate(obj)
    instance_mirror.refresh()
    modifier_inputs.refresh()


def unregister():
    if _refresh_groups in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_refresh_groups)
    if bpy.app.timers.is_registered(_refresh_groups):
        bpy.app.timers.unregister(_refresh_groups)
    for module in reversed(_installed_hooks):
        module.unregister()
    _installed_hooks.clear()
    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes.clear()
