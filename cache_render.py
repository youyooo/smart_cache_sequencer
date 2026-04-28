# SPDX-License-Identifier: GPL-3.0-or-later

"""Frame-by-frame VSE strip rendering to disk cache."""

import bpy
import os


class CacheRenderManager:
    """Renders VSE strips to cached image files using bpy.app.timers."""

    def __init__(self, cache_manager):
        self.cache_manager = cache_manager
        self._render_queue = []
        self._is_rendering = False
        self._is_internal_rendering = False
        self._cancel_flag = False
        self._progress = {'current': 0, 'total': 0, 'strip_name': ''}
        self._timer = None

    @property
    def is_rendering(self):
        return self._is_rendering

    @property
    def progress(self):
        return dict(self._progress)

    def queue_strip_range(self, strip, frame_start: int, frame_end: int, layer: int = 0):
        """Queue a range of frames for rendering."""
        for frame in range(frame_start, frame_end + 1):
            cache_path = self.cache_manager.get_frame_path(strip, frame, layer)
            if not os.path.exists(cache_path):
                self._render_queue.append((strip.name, frame, layer))
        self._deduplicate_queue()

    def queue_frame(self, strip, frame: int, layer: int = 0):
        """Queue a single frame for rendering."""
        cache_path = self.cache_manager.get_frame_path(strip, frame, layer)
        if not os.path.exists(cache_path):
            self._render_queue.append((strip.name, frame, layer))

    def start_render(self):
        """Begin background rendering via timer."""
        if self._is_rendering or not self._render_queue:
            return
        self._is_rendering = True
        self._cancel_flag = False
        self._progress['total'] = len(self._render_queue)
        self._progress['current'] = 0

        if bpy.app.timers.is_registered(self._render_timer):
            bpy.app.timers.unregister(self._render_timer)
        bpy.app.timers.register(self._render_timer, first_interval=0.1)

    def cancel_render(self):
        """Stop background rendering."""
        self._cancel_flag = True
        self._is_rendering = False
        if bpy.app.timers.is_registered(self._render_timer):
            bpy.app.timers.unregister(self._render_timer)

    def _render_timer(self):
        """Timer callback — renders one frame per tick."""
        if self._cancel_flag or not self._render_queue:
            self._is_rendering = False
            return None

        strip_name, frame, layer = self._render_queue.pop(0)
        self._progress['current'] += 1
        self._progress['strip_name'] = strip_name

        self._render_single_frame(strip_name, frame, layer)
        return 0.05

    def _render_single_frame(self, strip_name: str, frame: int, layer: int):
        """Render a single frame of a strip to disk."""
        scene = bpy.context.scene
        se = scene.sequence_editor
        if not se:
            return

        strip = self._find_strip(se, strip_name)
        if strip is None:
            return

        output_path = self.cache_manager.get_frame_path(strip, frame, layer)
        if os.path.exists(output_path):
            return

        render = scene.render
        original_frame = scene.frame_current
        original_filepath = render.filepath
        original_format = render.image_settings.file_format
        original_color_mode = render.image_settings.color_mode
        original_quality = render.image_settings.quality
        original_res_x = render.resolution_x
        original_res_y = render.resolution_y
        original_use_sequencer_crop = render.use_crop_to_render_border if hasattr(render, 'use_crop_to_render_border') else False

        # Save mute state of all strips
        original_mutes = {s.name: s.mute for s in se.sequences_all}

        try:
            self._is_internal_rendering = True

            # Mute all strips except the target
            for s in se.sequences_all:
                s.mute = True
            strip.mute = False

            # For L0, also temporarily disable modifiers
            if layer == 0 and hasattr(strip, 'modifiers'):
                mod_enabled = {m.name: m.enable for m in strip.modifiers}
                for m in strip.modifiers:
                    m.enable = False

            # Set frame
            scene.frame_set(frame)

            # Configure render output
            render.filepath = output_path
            render.image_settings.file_format = 'PNG'
            render.image_settings.color_mode = 'RGBA'
            render.image_settings.quality = 90

            # Render single frame
            bpy.ops.render.render(
                animation=False,
                write_still=True,
                use_sequencer_scene=True,
                scene=scene.name,
            )

            # Record in cache index
            if os.path.exists(output_path):
                self.cache_manager.record_cached_frame(strip, frame, layer, output_path)

        finally:
            # Restore all strip mute states
            for s in se.sequences_all:
                if s.name in original_mutes:
                    s.mute = original_mutes[s.name]

            # Restore modifiers
            if layer == 0 and hasattr(strip, 'modifiers'):
                for m in strip.modifiers:
                    if m.name in mod_enabled:
                        m.enable = mod_enabled[m.name]

            # Restore render settings
            render.filepath = original_filepath
            render.image_settings.file_format = original_format
            render.image_settings.color_mode = original_color_mode
            render.image_settings.quality = original_quality
            render.resolution_x = original_res_x
            render.resolution_y = original_res_y

            scene.frame_set(original_frame)
            self._is_internal_rendering = False

    def _deduplicate_queue(self):
        """Remove duplicate entries from the render queue."""
        seen = set()
        unique = []
        for item in self._render_queue:
            key = (item[0], item[1], item[2])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        self._render_queue = unique

    @staticmethod
    def _find_strip(se, name: str):
        for s in se.sequences_all:
            if s.name == name:
                return s
        return None
