# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Smart Cache Sequencer",
    "author": "Smart Cache",
    "version": (1, 0, 15),
    "blender": (4, 4, 0),
    "location": "Sequencer > Sidebar > Smart Cache",
    "description": "AE-inspired disk caching for VSE strips with layered cache and position-independent hashing",
    "category": "Sequencer",
}

import bpy
import traceback
import os
import tempfile

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


def apply_system_cache_settings(scene):
    """Apply our cache settings to Blender's system VSE cache."""
    settings = scene.smart_cache
    se = scene.sequence_editor
    if not se:
        return
    try:
        se.cache_memory_limit = settings.memory_cache_limit_mb
        print(f"[Smart Cache] Set memory cache limit to {settings.memory_cache_limit_mb} MB")
    except Exception as e:
        print(f"[Smart Cache] Could not set memory cache limit: {e}")


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
    for strip in se.strips:
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


def init_singletons(scene):
    """Initialize cache singletons synchronously. Returns (success, error_msg)."""
    print("[Smart Cache] Initializing...")
    try:
        settings = scene.smart_cache
        if not settings:
            msg = "No smart_cache settings on scene"
            print(f"[Smart Cache] ERROR: {msg}")
            return (False, msg)

        cache_dir = get_blend_cache_dir(scene)
        if not cache_dir:
            msg = "Could not resolve cache directory"
            print(f"[Smart Cache] ERROR: {msg}")
            return (False, msg)

        print(f"[Smart Cache] Cache dir: {cache_dir}")

        max_size = settings.max_cache_size_gb
        print(f"[Smart Cache] Max size: {max_size} GB")

        print("[Smart Cache] Creating CacheManager...")
        manager = cm_mod.CacheManager(cache_dir, max_size_gb=max_size)
        manager.ensure_dirs()
        print(f"[Smart Cache] Manager created OK")

        print("[Smart Cache] Creating CacheRenderManager...")
        renderer = cr_mod.CacheRenderManager(manager)
        print(f"[Smart Cache] Renderer created OK")

        print("[Smart Cache] Creating CachePlaybackController...")
        server = cs_mod.CachePlaybackController(manager)
        print(f"[Smart Cache] Server created OK")

        print("[Smart Cache] Creating PrefetchManager...")
        prefetch = cs_mod.PrefetchManager(manager, renderer, settings.prefetch_lookahead)
        print(f"[Smart Cache] Prefetch created OK")

        cache_core.set_singletons(manager, renderer, server, prefetch)
        print(f"[Smart Cache] Singletons set")

        apply_system_cache_settings(scene)
        auto_cache_all(scene)

        print(f"[Smart Cache] Initialized OK: {cache_dir}")
        return (True, "")

    except Exception as e:
        print(f"[Smart Cache] INIT FAILED: {e}")
        tb = traceback.format_exc()
        print(tb)
        return (False, f"{e}")


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
        print("[Smart Cache] User enabled plugin")
        ok, err = init_singletons(context.scene)
        if ok:
            cache_handlers.register_handlers()
    else:
        print("[Smart Cache] User disabled plugin")
        cleanup_singletons(context)
        cache_handlers.unregister_handlers()


def register():
    cache_ui.register()
    bpy.types.Scene.smart_cache = bpy.props.PointerProperty(
        type=cache_ui.SmartCacheSettings,
        update=_on_enabled_change,
    )
    print("[Smart Cache] Plugin registered")


def unregister():
    cleanup_singletons(bpy.context)
    cache_handlers.unregister_handlers()
    cache_ui.unregister()
    if hasattr(bpy.types.Scene, 'smart_cache'):
        del bpy.types.Scene.smart_cache
    print("[Smart Cache] Plugin unregistered")


if __name__ == "__main__":
    register()
