# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Smart Cache Sequencer",
    "author": "Smart Cache",
    "version": (1, 0, 22),
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
from . import cache_prefetch as cp_mod
from . import cache_handlers
from . import cache_ui
from . import cache_core
from . import cache_system


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
    """Apply our cache settings to Blender's system VSE cache.

    In Blender 5.x this is a no-op since caching is managed via
    use_cache_raw/use_cache_final booleans (handled by takeover).
    In Blender 4.x it sets cache_memory_limit for the legacy VSE cache.
    """
    settings = scene.smart_cache
    se = scene.sequence_editor
    if not se:
        return
    try:
        if hasattr(se, 'cache_memory_limit'):
            se.cache_memory_limit = settings.memory_cache_limit_mb
            print(f"[Smart Cache] Set memory cache limit to {settings.memory_cache_limit_mb} MB")
        else:
            print(f"[Smart Cache] VSE cache managed by takeover (use_cache_raw=False)")
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
    """Initialize cache singletons synchronously. Returns (success, error_msg).

    Safe to call multiple times — previous singletons are cleaned up first.
    """
    print("[Smart Cache] Initializing...")
    # Clean up any previous singletons before creating new ones
    cleanup_singletons(None)
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
        prefetch = cp_mod.PrefetchManager(
            manager, renderer,
            lookahead=settings.prefetch_lookahead,
            strategy=settings.prefetch_strategy,
            idle_enabled=settings.idle_rendering,
            session_recovery=settings.session_recovery,
        )
        print(f"[Smart Cache] Prefetch created OK")

        cache_core.set_singletons(manager, renderer, server, prefetch)
        print(f"[Smart Cache] Singletons set")

        cache_system.takeover_system_cache(scene)

        # Try session recovery first; fall back to full auto-cache
        if prefetch.session_recovery:
            recovered = prefetch.load_session_state(scene)
            if not recovered:
                auto_cache_all(scene)
        else:
            auto_cache_all(scene)

        print(f"[Smart Cache] Initialized OK: {cache_dir}")
        return (True, "")

    except Exception as e:
        print(f"[Smart Cache] INIT FAILED: {e}")
        tb = traceback.format_exc()
        print(tb)
        return (False, f"{e}")


def cleanup_singletons(context):
    """Stop rendering, disable proxy strips, clear singletons.

    Safe to call with None context (disable_cache_playback will be skipped).
    """
    mgr, renderer, server, prefetch = cache_core.get_singletons()

    if renderer:
        renderer.cancel_render()
    if server:
        try:
            if context:
                server.disable_cache_playback(context)
            else:
                # Try to find a valid context for proxy cleanup
                se = getattr(bpy.context, 'scene', None)
                if se and getattr(se, 'sequence_editor', None):
                    server.disable_cache_playback(bpy.context)
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
    cache_system.restore_if_taken_over(bpy.context)
    cache_ui.unregister()
    if hasattr(bpy.types.Scene, 'smart_cache'):
        del bpy.types.Scene.smart_cache
    print("[Smart Cache] Plugin unregistered")


if __name__ == "__main__":
    register()
