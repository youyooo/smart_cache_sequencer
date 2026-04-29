# SPDX-License-Identifier: GPL-3.0-or-later

"""Proxy strip creation and mute management for cache playback."""

import bpy
import os

from .cache_manager import _safe_strip_name

CACHE_PREFIX = "SC_"
CACHEABLE_TYPES = {'MOVIE', 'IMAGE', 'SCENE'}


class CachePlaybackController:
    """Creates proxy Image Sequence strips from cached frames."""

    def __init__(self, cache_manager):
        self.cache_manager = cache_manager
        self.proxy_strips: dict[str, list] = {}
        self._original_mutes: dict[str, bool] = {}
        self._is_active = False

    @property
    def is_active(self):
        return self._is_active

    def enable_cache_playback(self, context):
        """Mute original strips, create proxy Image Sequences from cache."""
        se = context.scene.sequence_editor
        if not se:
            return

        self._is_active = True
        self._original_mutes.clear()
        self.proxy_strips.clear()

        cached_strips = set()
        for entry in self.cache_manager.index.values():
            cached_strips.add(entry['strip_name'])

        if not cached_strips:
            self._is_active = False
            return

        for strip_name in cached_strips:
            strip = self._find_strip(se, strip_name)
            if strip is None or strip.type not in CACHEABLE_TYPES:
                continue
            if strip.mute:
                continue

            frames = self.cache_manager.get_cached_frames(strip_name, layer=0)
            if not frames:
                continue

            self._original_mutes[strip_name] = strip.mute

            cache_dir = self.cache_manager.get_cache_dir_for_strip(strip_name, layer=0)
            if not cache_dir or not os.path.exists(cache_dir):
                continue

            png_files = sorted([f for f in os.listdir(cache_dir) if f.endswith('.png')])
            if not png_files:
                continue

            first_file = os.path.join(cache_dir, png_files[0])
            proxy_channel = strip.channel + 1
            proxy_name = f"{CACHE_PREFIX}{strip_name}"

            try:
                proxy = se.strips.new_image(
                    name=proxy_name,
                    filepath=first_file,
                    channel=proxy_channel,
                    frame_start=strip.frame_final_start,
                    fit_method='STRETCH',
                )
                proxy.use_animation = True
                proxy.frame_start = frames[0]
                proxy.frame_offset_start = 0
                proxy.frame_final_duration = len(frames)
                proxy.blend_type = strip.blend_type
                proxy.blend_alpha = strip.blend_alpha
                proxy.mute = False
                proxy.lock = True
                proxy.select = False
                self.proxy_strips[strip_name] = [proxy]
            except Exception as e:
                print(f"[Smart Cache] Proxy creation failed for {strip_name}: {e}")

        for strip_name in self._original_mutes:
            strip = self._find_strip(se, strip_name)
            if strip:
                strip.mute = True

    def disable_cache_playback(self, context):
        """Remove proxy strips, restore original mute states."""
        se = context.scene.sequence_editor
        if not se:
            return

        for strip_name, proxies in self.proxy_strips.items():
            for proxy in proxies:
                if proxy.name in se.strips:
                    try:
                        se.strips.remove(proxy)
                    except RuntimeError:
                        pass

        for strip_name, was_muted in self._original_mutes.items():
            strip = self._find_strip(se, strip_name)
            if strip:
                strip.mute = was_muted

        self.proxy_strips.clear()
        self._original_mutes.clear()
        self._is_active = False

    def disable_for_render(self, context):
        """Temporarily disable cache for final rendering."""
        if self._is_active:
            self.disable_cache_playback(context)

    @staticmethod
    def _find_strip(se, name: str):
        for s in se.strips:
            if s.name == name:
                return s
        return None


class PrefetchManager:
    """Pre-renders frames ahead of the playhead during idle time."""

    def __init__(self, cache_manager, cache_render, lookahead=30):
        self.cache_manager = cache_manager
        self.cache_render = cache_render
        self.lookahead = lookahead
        self._last_frame = -1

    def on_frame_change(self, current_frame: int, scene):
        """Queue frames ahead of the playhead for prefetch."""
        if current_frame == self._last_frame:
            return
        self._last_frame = current_frame

        if self.cache_render.is_rendering:
            return

        se = scene.sequence_editor
        if not se:
            return

        for strip in se.strips:
            if strip.mute or strip.lock:
                continue
            if strip.type not in CACHEABLE_TYPES:
                continue

            start = strip.frame_final_start
            end = strip.frame_final_end
            if start > current_frame + self.lookahead:
                continue

            for frame in range(current_frame + 1, min(current_frame + self.lookahead + 1, end)):
                if not self.cache_manager.frame_exists(strip, frame, 0):
                    self.cache_render.queue_frame(strip, frame, 0)

        if self.cache_render._render_queue:
            self.cache_render.start_render()
