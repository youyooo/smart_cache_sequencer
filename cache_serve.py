# SPDX-License-Identifier: GPL-3.0-or-later

"""Proxy strip creation and mute management for cache playback.

After frames are cached to disk as PNG image sequences, this module
replaces original VSE strips with proxy Image Sequence strips that
point to the cached files, providing smooth scrubbing and playback.
"""

import bpy
import os

from .cache_manager import _safe_strip_name

CACHE_PREFIX = "SC_"
CACHEABLE_TYPES = {"MOVIE", "IMAGE", "SCENE"}


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

    # ── Public API ────────────────────────────────────────────────────

    def enable_cache_playback(self, context):
        """Mute original strips, replace with proxy Image Sequences."""
        se = context.scene.sequence_editor
        if not se:
            return

        self._is_active = True
        self._original_mutes.clear()
        self.proxy_strips.clear()

        # Get all strip names that have at least some L0 cache
        cached_strip_names = set()
        for entry in self.cache_manager.index.values():
            if entry["layer"] == 0:
                cached_strip_names.add(entry["strip_name"])

        if not cached_strip_names:
            self._is_active = False
            return

        # Count how many frames each strip has cached (contiguous L0)
        strip_frames: dict[str, list[int]] = {}
        for name in cached_strip_names:
            strip_frames[name] = self.cache_manager.get_cached_frames(name, layer=0)

        for strip_name, frames in strip_frames.items():
            if not frames:
                continue

            strip = self._find_strip(se, strip_name)
            if strip is None or strip.type not in CACHEABLE_TYPES:
                continue
            if strip.mute:
                continue

            # Resolve the cache directory for this strip's L0 cache
            cache_dir = self._get_l0_dir(strip_name)
            if not cache_dir or not os.path.isdir(cache_dir):
                continue

            png_files = sorted(
                f for f in os.listdir(cache_dir) if f.endswith(".png")
            )
            if not png_files:
                continue

            proxy_channel = self._next_free_channel(strip.channel + 1, se)
            proxy_name = f"{CACHE_PREFIX}{strip_name}"

            try:
                self._add_image_sequence(
                    context, se, cache_dir, png_files[0],
                    proxy_name, proxy_channel, frames[0],
                )

                # Locate the newly added strip
                proxy = se.strips.get(proxy_name)
                if not proxy:
                    continue

                # Set duration to match number of cached frames
                proxy.frame_final_duration = len(frames)
                proxy.lock = True
                proxy.select = False
                proxy.mute = False

                self.proxy_strips[strip_name] = [proxy]
            except Exception as e:
                print(f"[Smart Cache] Proxy creation failed for {strip_name}: {e}")
                import traceback
                traceback.print_exc()

        # Mute originals after creating all proxies
        for strip_name in list(strip_frames.keys()):
            s = self._find_strip(se, strip_name)
            if s:
                self._original_mutes[strip_name] = s.mute
                s.mute = True

        if not self.proxy_strips:
            self._is_active = False

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

    # ── Internals ─────────────────────────────────────────────────────

    def _add_image_sequence(
        self, context, se, directory: str, first_file: str,
        name: str, channel: int, frame_start: int,
    ):
        """Create an Image Sequence strip using the sequencer operator.

        Uses bpy.ops.sequencer.image_strip_add which properly detects
        image sequences from sequentially-named PNG files.
        """
        sequencer_area = self._find_sequencer_area(context)
        if sequencer_area:
            with bpy.context.temp_override(
                window=context.window,
                area=sequencer_area,
            ):
                bpy.ops.sequencer.image_strip_add(
                    directory=directory,
                    files=[{"name": first_file}],
                    frame_start=frame_start,
                    channel=channel,
                    set_view_transform=False,
                )
        else:
            # Fallback: try without override (may fail if no SEQUENCE_EDITOR area)
            bpy.ops.sequencer.image_strip_add(
                directory=directory,
                files=[{"name": first_file}],
                frame_start=frame_start,
                channel=channel,
                set_view_transform=False,
            )

    def _get_l0_dir(self, strip_name: str) -> str:
        """Resolve the L0 cache directory for a strip from the cache index."""
        for entry in self.cache_manager.index.values():
            if entry["strip_name"] == strip_name and entry["layer"] == 0:
                return os.path.dirname(entry["path"])
        return ""

    def _find_sequencer_area(self, context):
        """Find the first SEQUENCE_EDITOR area in the current screen."""
        screen = getattr(context, "screen", None)
        if screen:
            for area in screen.areas:
                if area.type == "SEQUENCE_EDITOR":
                    return area
        # Fallback: search all screens
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == "SEQUENCE_EDITOR":
                    return area
        return None

    @staticmethod
    def _next_free_channel(channel: int, se) -> int:
        """Find the next available channel at or above the given one."""
        occupied = {s.channel for s in se.strips}
        while channel in occupied:
            channel += 1
        return channel

    @staticmethod
    def _find_strip(se, name: str):
        return se.strips.get(name)


class PrefetchManager:
    """Pre-renders frames ahead of the playhead during idle time."""

    def __init__(self, cache_manager, cache_render, lookahead=30):
        self.cache_manager = cache_manager
        self.cache_render = cache_render
        self.lookahead = lookahead
        self._last_frame = -1

    def on_frame_change(self, current_frame: int, scene):
        """Queue uncached frames ahead of the playhead."""
        if current_frame == self._last_frame:
            return
        self._last_frame = current_frame

        if self.cache_render.is_rendering:
            return

        se = scene.sequence_editor
        if not se:
            return

        queued = 0
        for strip in se.strips:
            if strip.mute or strip.lock:
                continue
            if strip.type not in CACHEABLE_TYPES:
                continue

            start = strip.frame_final_start
            end = strip.frame_final_end
            if current_frame < start or current_frame > end:
                continue

            lookahead_end = min(current_frame + self.lookahead, end)
            for frame in range(current_frame + 1, lookahead_end):
                if not self.cache_manager.frame_exists(strip, frame, 0):
                    self.cache_render.queue_frame(strip, frame, 0)
                    queued += 1

        if queued > 0:
            self.cache_render.start_render()
