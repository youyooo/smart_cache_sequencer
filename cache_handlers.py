# SPDX-License-Identifier: GPL-3.0-or-later

"""Blender app handlers for cache integration."""

import bpy
from bpy.app.handlers import persistent

from . import cache_key as ck
from . import cache_core as sc


def _get():
    return sc.get_singletons()


@persistent
def on_frame_change_pre(scene):
    settings = getattr(scene, 'smart_cache', None)
    if not settings or not settings.enabled:
        return

    manager, renderer, server, prefetch, track_manager = _get()
    if not manager or not renderer:
        return

    # Skip during internal rendering
    if renderer._is_internal_rendering:
        return

    frame = scene.frame_current

    # If cache playback is active, touch accessed frames for LRU
    if server and server.is_active:
        for strip_name in server.proxy_strips:
            strip = server._find_strip(scene.sequence_editor, strip_name)
            if strip:
                manager.touch_frame(strip, frame, 0)

    # Trigger playhead prefetch during playback
    if prefetch and not renderer.is_rendering and settings.auto_cache_on_playback:
        if scene.is_animation_playing:
            prefetch.on_playhead_move(frame, scene)

    # Auto-pair new audio strips when the timeline structure changes
    if track_manager:
        context = bpy.context
        if context and getattr(context, 'scene', None) is scene:
            track_manager.check_and_auto_pair(context)


@persistent
def on_depsgraph_update(scene, depsgraph):
    """Detect VSE changes and invalidate cache when strip content changes."""
    settings = getattr(scene, 'smart_cache', None)
    if not settings or not settings.enabled:
        return

    manager, renderer, server, prefetch, track_manager = _get()
    if not manager:
        return

    se = scene.sequence_editor
    if not se:
        return

    for strip in se.strips:
        if strip.type not in ('MOVIE', 'IMAGE', 'SCENE'):
            continue
        if strip.name not in manager.strip_hashes:
            continue

        stored = manager.strip_hashes[strip.name]
        current_base = ck.strip_base_hash(strip)

        if current_base != stored['base_hash']:
            manager.invalidate_strip(strip.name)
        else:
            current_mod = ck.strip_modifier_hash(strip)
            stored_mod = stored.get('mod_hash')
            if current_mod != stored_mod:
                manager.invalidate_strip(strip.name, layer=1)


@persistent
def on_save_pre(dummy):
    scene = bpy.context.scene
    settings = getattr(scene, 'smart_cache', None)
    if not settings:
        return
    manager, _, _, prefetch, _ = _get()
    if manager:
        manager._save_index()
    if prefetch:
        prefetch.save_session_state()


@persistent
def on_load_post(dummy):
    scene = bpy.context.scene
    settings = getattr(scene, 'smart_cache', None)
    if not settings:
        return
    manager, _, _, prefetch, _ = _get()
    if manager:
        manager._load_index()
    if prefetch:
        prefetch.load_session_state(scene)


@persistent
def on_render_pre(scene):
    settings = getattr(scene, 'smart_cache', None)
    if not settings:
        return
    _, _, server, _, _ = _get()
    if server and server.is_active:
        server.disable_for_render(scene)


def _idle_timer():
    """Timer callback — triggers idle background rendering every ~2 seconds.

    Registered as a Blender persistent timer so it survives across
    playhead scrubs and short idle pauses.
    """
    scene = bpy.context.scene
    settings = getattr(scene, 'smart_cache', None)
    if not settings or not settings.enabled:
        return 2.0

    _, _, _, prefetch, _ = _get()
    if prefetch:
        prefetch.on_idle(scene)

    return 2.0  # reschedule every 2 seconds


def register_handlers():
    handlers = [
        (bpy.app.handlers.frame_change_pre, on_frame_change_pre),
        (bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
        (bpy.app.handlers.save_pre, on_save_pre),
        (bpy.app.handlers.load_post, on_load_post),
        (bpy.app.handlers.render_pre, on_render_pre),
    ]
    for handler_list, handler in handlers:
        if handler not in handler_list:
            handler_list.append(handler)

    if not bpy.app.timers.is_registered(_idle_timer):
        bpy.app.timers.register(_idle_timer, first_interval=2.0, persistent=True)


def unregister_handlers():
    handlers = [
        (bpy.app.handlers.frame_change_pre, on_frame_change_pre),
        (bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
        (bpy.app.handlers.save_pre, on_save_pre),
        (bpy.app.handlers.load_post, on_load_post),
        (bpy.app.handlers.render_pre, on_render_pre),
    ]
    for handler_list, handler in handlers:
        if handler in handler_list:
            handler_list.remove(handler)

    if bpy.app.timers.is_registered(_idle_timer):
        bpy.app.timers.unregister(_idle_timer)
