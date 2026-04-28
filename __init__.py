# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Smart Cache Sequencer",
    "author": "Smart Cache",
    "version": (1, 0, 7),
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
from . import cache_core


def get_singletons():
    return cache_core.get_singletons()


def get_blend_cache_dir(scene):
    """Resolve cache directory relative to the .blend file."""
    import os
    import tempfile
    settings = scene.smart_cache
    if not settings:
        return None
    raw_path = settings.cache_directory
    if raw_path.startswith("//"):
        blend_path = bpy.data.filepath
        if not blend_path:
            return os.path.join(tempfile.gettempdir(), "vse_smart_cache")
        base = os.path.dirname(blend_path)
        return os.path.normpath(os.path.join(base, raw_path[2:]))
    return raw_path


def auto_cache_all(scene):
    """Automatically queue all VSE strips for caching."""
    se = scene.sequence_editor
    if not se:
        return

    settings = scene.smart_cache
    manager, renderer, server, prefetch = get_singletons()
    if not manager or not renderer:
        return

    manager.max_size_bytes = int(settings.max_cache_size_gb * 1024 * 1024 * 1024)
    manager.ensure_dirs()

    count = 0
    for strip in se.sequences_all:
        if strip.type not in ('MOVIE', 'IMAGE', 'SCENE'):
            continue
        if strip.mute:
            continue

        start = strip.frame_final_start
        end = strip.frame_final_end
        renderer.queue_strip_range(strip, start, end, 0)

        if hasattr(strip, 'modifiers') and len(strip.modifiers) > 0:
            renderer.queue_strip_range(strip, start, end, 1)
        count += 1

    if count > 0:
        renderer.start_render()
        print(f"[Smart Cache] Auto-caching {count} strip(s)")
    else:
        print("[Smart Cache] No cacheable strips found")


def _do_init(scene):
    """Deferred init via timer — actually creates singletons."""
    settings = scene.smart_cache
    if not settings:
        return False

    cache_dir = get_blend_cache_dir(scene)
    if not cache_dir:
        return False

    max_size = settings.max_cache_size_gb

    manager = cm_mod.CacheManager(cache_dir, max_size_gb=max_size)
    manager.ensure_dirs()

    renderer = cr_mod.CacheRenderManager(manager)
    server = cs_mod.CachePlaybackController(manager)
    prefetch = cs_mod.PrefetchManager(manager, renderer, settings.prefetch_lookahead)

    cache_core.set_singletons(manager, renderer, server, prefetch)

    print(f"[Smart Cache] Initialized: {cache_dir}")

    auto_cache_all(scene)
    return False  # don't repeat


def init_singletons(scene):
    """Initialize cache singletons — deferred via timer to avoid blocking UI."""
    print(f"[Smart Cache] Initializing...")
    bpy.app.timers.register(lambda: _do_init(scene), first_interval=0.1)


def cleanup_singletons(context):
    """Stop rendering and disable proxy strips."""
    renderer = cache_core.get_singletons()[1]
    server = cache_core.get_singletons()[2]

    if renderer:
        renderer.cancel_render()
    if server and context:
        try:
            server.disable_cache_playback(context)
        except Exception:
            pass

    cache_core.clear_singletons()
    print("[Smart Cache] Cleaned up")


def _on_enabled_change(self, context):
    """Called when the user toggles the enabled checkbox."""
    settings = context.scene.smart_cache
    if not settings:
        return

    if settings.enabled:
        init_singletons(context.scene)
        cache_handlers.register_handlers()
    else:
        cleanup_singletons(context)
        cache_handlers.unregister_handlers()


def register():
    cache_ui.register()
    bpy.types.Scene.smart_cache = bpy.props.PointerProperty(
        type=cache_ui.SmartCacheSettings,
        update=_on_enabled_change,
    )


def unregister():
    cleanup_singletons(bpy.context)
    cache_handlers.unregister_handlers()
    cache_ui.unregister()
    if hasattr(bpy.types.Scene, 'smart_cache'):
        del bpy.types.Scene.smart_cache


if __name__ == "__main__":
    register()
