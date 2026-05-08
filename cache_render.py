# SPDX-License-Identifier: GPL-3.0-or-later

"""Frame-by-frame VSE strip rendering to disk cache.

Rendering strategy (two methods, tried in order):
  1. bpy.ops.render.render(write_still=True)
     Works from any context, no viewport needed.  Sets scene.render.use_sequencer.
  2. bpy.ops.render.opengl(sequencer=True) via temp_override
     Faster but needs a valid 3D Viewport area for the OpenGL context.
"""

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
        self._progress = {"current": 0, "total": 0, "strip_name": ""}
        # Cache a reference to the first 3D View area for OpenGL fallback
        self._viewport_area = self._find_viewport_area()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_rendering(self):
        return self._is_rendering

    @property
    def progress(self):
        return dict(self._progress)

    # ── Queueing ──────────────────────────────────────────────────────

    def queue_strip_range(self, strip, frame_start: int, frame_end: int, layer: int = 0):
        """Queue a range of frames for rendering (skips already-cached)."""
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

    def queue_missing(self, strip, frame_start: int, frame_end: int, layer: int = 0):
        """Queue only frames that are missing from cache."""
        for frame in range(frame_start, frame_end + 1):
            if not self.cache_manager.frame_exists(strip, frame, layer):
                self._render_queue.append((strip.name, frame, layer))
        self._deduplicate_queue()

    # ── Render lifecycle ──────────────────────────────────────────────

    def start_render(self):
        """Begin background rendering via timer."""
        if self._is_rendering or not self._render_queue:
            return
        self._is_rendering = True
        self._cancel_flag = False
        self._progress["total"] = len(self._render_queue)
        self._progress["current"] = 0

        if bpy.app.timers.is_registered(self._render_timer):
            bpy.app.timers.unregister(self._render_timer)
        bpy.app.timers.register(self._render_timer, first_interval=0.1)

    def cancel_render(self):
        """Stop background rendering."""
        self._cancel_flag = True
        self._is_rendering = False
        if bpy.app.timers.is_registered(self._render_timer):
            bpy.app.timers.unregister(self._render_timer)

    # ── Timer callback ────────────────────────────────────────────────

    def _render_timer(self):
        """Timer callback — renders one frame per tick."""
        if self._cancel_flag or not self._render_queue:
            self._is_rendering = False
            self._is_internal_rendering = False
            return None

        strip_name, frame, layer = self._render_queue.pop(0)
        self._progress["current"] += 1
        self._progress["strip_name"] = strip_name

        self._render_single_frame(strip_name, frame, layer)

        # Return interval in seconds — render next frame 50 ms later
        # Only continue if more work remains and not cancelled
        if self._cancel_flag or not self._render_queue:
            self._is_rendering = False
            self._is_internal_rendering = False
            return None
        return 0.05

    # ── Single frame render ───────────────────────────────────────────

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
            # Already cached — just record in index if missing
            self.cache_manager.record_cached_frame(strip, frame, layer, output_path)
            return

        render = scene.render

        # ── Save state ────────────────────────────────────────────────
        original_frame = scene.frame_current
        original_filepath = render.filepath
        original_format = render.image_settings.file_format
        original_color_mode = render.image_settings.color_mode
        original_quality = render.image_settings.quality
        original_res_x = render.resolution_x
        original_res_y = render.resolution_y
        original_use_sequencer = render.use_sequencer
        original_mutes = {}
        for s in se.strips:
            original_mutes[s.name] = s.mute
        original_camera = scene.camera

        # ── Setup scene ───────────────────────────────────────────────
        try:
            self._is_internal_rendering = True

            # Mute all strips except target
            for s in se.strips:
                s.mute = True
            strip.mute = False

            # L0: disable modifiers
            mod_enabled = {}
            if layer == 0 and hasattr(strip, "modifiers"):
                mod_enabled = {m.name: m.enable for m in strip.modifiers}
                for m in strip.modifiers:
                    m.enable = False

            # Set frame and update depsgraph
            scene.frame_set(frame)
            bpy.context.view_layer.update()

            # Make sure the VSE is included in renders
            render.use_sequencer = True
            render.filepath = output_path
            render.image_settings.file_format = "PNG"
            render.image_settings.color_mode = "RGBA"
            render.image_settings.quality = 90

            # ── Render ────────────────────────────────────────────────
            rendered = self._do_render(scene, output_path)

            # ── Record ────────────────────────────────────────────────
            if rendered and os.path.exists(output_path):
                self.cache_manager.record_cached_frame(strip, frame, layer, output_path)
                print(f"[Smart Cache] ✓ Cached: {strip_name} frame {frame} → {os.path.basename(output_path)}")
            elif os.path.exists(output_path):
                # File exists but we didn't claim success — record anyway
                self.cache_manager.record_cached_frame(strip, frame, layer, output_path)
            else:
                print(f"[Smart Cache] ✗ Render claimed OK but file missing: {output_path}")

        except Exception as e:
            print(f"[Smart Cache] ✗ Render FAILED: {strip_name} frame {frame}: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # ── Restore state ──────────────────────────────────────────
            for s in se.strips:
                if s.name in original_mutes:
                    s.mute = original_mutes[s.name]

            if layer == 0 and mod_enabled:
                for m in strip.modifiers:
                    if m.name in mod_enabled:
                        m.enable = mod_enabled[m.name]

            render.use_sequencer = original_use_sequencer
            render.filepath = original_filepath
            render.image_settings.file_format = original_format
            render.image_settings.color_mode = original_color_mode
            render.image_settings.quality = original_quality
            render.resolution_x = original_res_x
            render.resolution_y = original_res_y
            scene.camera = original_camera
            scene.frame_set(original_frame)
            self._is_internal_rendering = False

    def _do_render(self, scene, output_path) -> bool:
        """Attempt rendering using two strategies.  Returns True if output file exists."""

        # ── Method 1: Full render pipeline (no viewport required) ─────
        try:
            # Ensure there's always an active camera so render.render() doesn't bail
            if not scene.camera:
                # Create a temporary camera object
                cam_data = bpy.data.cameras.new("__sc_temp_cam")
                cam_obj = bpy.data.objects.new("__sc_temp_cam", cam_data)
                # Don't link to collection — just set as scene camera
                # (Blender allows orphan data as camera)
                scene.camera = cam_obj

            bpy.ops.render.render(write_still=True)
            if os.path.exists(output_path):
                return True
        except Exception as e:
            print(f"[Smart Cache]   render.render() failed: {e}")

        # ── Method 2: OpenGL render via viewport ──────────────────────
        try:
            if not self._viewport_area:
                self._viewport_area = self._find_viewport_area()
            win = self._find_window()
            if self._viewport_area and win:
                with bpy.context.temp_override(
                    window=win,
                    area=self._viewport_area,
                ):
                    bpy.ops.render.opengl(
                        animation=False,
                        sequencer=True,
                        write_still=True,
                        view_context=False,
                    )
                if os.path.exists(output_path):
                    print("[Smart Cache]   Used OpenGL render (sequencer=True)")
                    return True
        except Exception as e:
            print(f"[Smart Cache]   render.opengl() failed: {e}")

        return False

    # ── Context helpers ───────────────────────────────────────────────

    @staticmethod
    def _find_viewport_area():
        """Find the first 3D Viewport area for OpenGL rendering context."""
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == "VIEW_3D":
                    return area
        return None

    @staticmethod
    def _find_window():
        """Find an open window for context override."""
        wm = bpy.context.window_manager
        if wm.windows:
            return wm.windows[0]
        return None

    # ── Queue helpers ─────────────────────────────────────────────────

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
        for s in se.strips:
            if s.name == name:
                return s
        return None
