# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Smart Cache Sequencer",
    "author": "Smart Cache",
    "version": (1, 0, 0),
    "blender": (4, 4, 0),
    "location": "Sequencer > Sidebar > Smart Cache",
    "description": "AE-inspired disk caching for VSE strips with layered cache and position-independent hashing",
    "category": "Sequencer",
}

import bpy

from . import cache_manager as cm_mod
from . import cache_render as cr_mod
from . import cache_serve as cs_mod
from . import cache_handlers
from . import cache_ui

# Module-level singleton storage
_singletons = {}


def get_singletons():
    """Return (manager, renderer, server, prefetch) tuple."""
    return (
        _singletons.get('manager'),
        _singletons.get('renderer'),
        _singletons.get('server'),
        _singletons.get('prefetch'),
    )


def get_blend_cache_dir():
    """Resolve cache directory relative to the .blend file."""
    import os
    settings = bpy.context.scene.smart_cache
    if not settings:
        return None
    raw_path = settings.get("cache_directory", "//vse_smart_cache")
    if raw_path.startswith("//"):
        blend_path = bpy.data.filepath
        if not blend_path:
            import tempfile
            return os.path.join(tempfile.gettempdir(), "vse_smart_cache")
        base = os.path.dirname(blend_path)
        return os.path.normpath(os.path.join(base, raw_path[2:]))
    return raw_path


def init_singletons():
    """Initialize cache singletons."""
    settings = bpy.context.scene.smart_cache
    if not settings:
        return

    cache_dir = get_blend_cache_dir()
    if not cache_dir:
        return

    max_size = settings.get("max_cache_size_gb", 10.0)

    manager = cm_mod.CacheManager(cache_dir, max_size_gb=max_size)
    manager.ensure_dirs()

    renderer = cr_mod.CacheRenderManager(manager)
    server = cs_mod.CachePlaybackController(manager)
    prefetch = cs_mod.PrefetchManager(manager, renderer, settings.get("prefetch_lookahead", 30))

    _singletons['manager'] = manager
    _singletons['renderer'] = renderer
    _singletons['server'] = server
    _singletons['prefetch'] = prefetch

    print(f"[Smart Cache] Initialized: {cache_dir}")


def cleanup_singletons():
    """Stop rendering and disable proxy strips."""
    renderer = _singletons.get('renderer')
    server = _singletons.get('server')

    if renderer:
        renderer.cancel_render()
    if server:
        try:
            server.disable_cache_playback(bpy.context)
        except Exception:
            pass

    _singletons.clear()
    print("[Smart Cache] Cleaned up")


def _on_enabled_change(self, context):
    """Called when the user toggles the enabled checkbox."""
    settings = context.scene.smart_cache
    if not settings:
        return

    if settings.enabled:
        init_singletons()
        cache_handlers.register_handlers()
    else:
        cleanup_singletons()
        cache_handlers.unregister_handlers()


def register():
    cache_ui.register()
    bpy.types.Scene.smart_cache = bpy.props.PointerProperty(
        type=cache_ui.SmartCacheSettings,
        update=_on_enabled_change,
    )


def unregister():
    cleanup_singletons()
    cache_handlers.unregister_handlers()
    cache_ui.unregister()
    if hasattr(bpy.types.Scene, 'smart_cache'):
        del bpy.types.Scene.smart_cache


if __name__ == "__main__":
    register()
