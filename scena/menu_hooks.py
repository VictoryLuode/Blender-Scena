"""Route individual operators in shared Blender menus."""

from functools import wraps
from types import SimpleNamespace


def install(menu, layout_type, active, *, compatible_draw=None):
    callbacks = menu._dyn_ui_initialize()
    for index, original in enumerate(callbacks):
        native = (original.__module__ == "bl_ui.space_view3d"
                  and original.__qualname__ == menu.__name__ + ".draw")
        if not native and not (compatible_draw and compatible_draw(original)):
            continue

        @wraps(original)
        def draw(self, context):
            if not active(context):
                return original(self, context)
            return original(SimpleNamespace(layout=layout_type(self.layout)), context)

        callbacks[index] = draw
        return callbacks, draw, original
    return None


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
