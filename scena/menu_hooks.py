"""Route individual operators in shared Blender menus."""

from functools import wraps
from types import SimpleNamespace


def native_draw(menu):
    current = menu.draw
    callbacks = (current,)
    if getattr(current, "__globals__", None) is menu._dyn_ui_initialize.__globals__:
        callbacks = getattr(current, "_draw_funcs", callbacks)
    for draw in callbacks:
        # A decorated add-on callback can carry Blender's name and attributes.
        # Its globals identify the module that actually implements it.
        if (getattr(draw, "__globals__", {}).get("__name__") == "bl_ui.space_view3d"
                and getattr(draw, "__qualname__", "") == menu.__name__ + ".draw"):
            return draw
    return None


def install(menu, layout_type, active=None):
    original = native_draw(menu)
    if original is None:
        return None

    @wraps(original)
    def draw(self, context):
        if active is None or active(context):
            self = SimpleNamespace(layout=layout_type(self.layout))
        return original(self, context)

    callbacks = menu._dyn_ui_initialize()
    callbacks[callbacks.index(original)] = draw
    return callbacks, draw, original


def restore(hook):
    if hook:
        callbacks, draw, original = hook
        if draw in callbacks:
            callbacks[callbacks.index(draw)] = original


class Layout:
    def __init__(self, layout):
        object.__setattr__(self, "_layout", layout)

    def __getattr__(self, name):
        return getattr(self._layout, name)

    def __setattr__(self, name, value):
        setattr(self._layout, name, value)
