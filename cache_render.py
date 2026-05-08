# SPDX-License-Identifier: GPL-3.0-or-later

"""Frame-by-frame VSE strip rendering to disk cache with layered cache support.

Rendering strategy (two methods, tried in order):
  1. bpy.ops.render.render(write_still=True)
     Works from any context, no viewport needed.  Sets scene.render.use_sequencer.
  2. bpy.ops.render.opengl(sequencer=True) via temp_override
     Faster but needs a valid 3D Viewport area for the OpenGL context.

Layered cache integration:
  - Newly rendered frames are stored in both SSD (on-disk) and RAM (hot cache).
  - On-demand rendering (L2 cold path) renders to temp and returns bytes.
"""

import bpy
import os
import tempfile
import time

from . import cache_key as ck


# ── Shared render helpers ──────────────────────────────────────────────
# These are module-level so both CacheRenderManager and CacheManager can
# reuse the same save/restore/render logic without circular imports.

def _find_viewport_area():
    """Find the first 3D Viewport area for OpenGL rendering context."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                return area
    return None


def _find_window():
    """Find an open window for context override."""
    wm = bpy.context.window_manager
    if wm.windows:
        return wm.windows[0]
    return None


def _do_raw_render(scene, output_path) -> bool:
    """Attempt rendering using two strategies.  Returns True if output file exists."""

    # ── Method 1: Full render pipeline (no viewport required) ─────
    try:
        # Ensure there's always an active camera so render.render() doesn't bail
        if not scene.camera:
            cam_data = bpy.data.cameras.new("__sc_temp_cam")
            cam_obj = bpy.data.objects.new("__sc_temp_cam", cam_data)
            scene.camera = cam_obj

        bpy.ops.render.render(write_still=True)
        if os.path.exists(output_path):
            return True
    except Exception as e:
        print(f"[Smart Cache]   render.render() failed: {e}")

    # ── Method 2: OpenGL render via viewport ──────────────────────
    try:
        viewport_area = _find_viewport_area()
        win = _find_window()
        if viewport_area and win:
            with bpy.context.temp_override(
                window=win,
                area=viewport_area,
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


def _render_strip_frame(
    scene, strip, frame: int, output_path: str, layer: int = 0,
    fmt: str = "PNG", quality: int = 90,
) -> bool:
    """Render a single frame of a strip to a cached image file.

    Supports PNG (lossless), JPEG (lossy/small), and EXR (HDR/32-bit) formats.
    Saves and restores all scene state (mutes, render settings, frame, camera).
    Handles modifier enable/disable for layer 0 (base) vs layer 1 (with modifiers).

    Returns True if the output file was successfully created.
    """
    se = scene.sequence_editor
    if not se:
        return False

    render = scene.render

    # ── Save state ────────────────────────────────────────────────
    original_frame = scene.frame_current
    original_filepath = render.filepath
    original_format = render.image_settings.file_format
    original_color_mode = render.image_settings.color_mode
    original_quality = render.image_settings.quality
    original_color_depth = render.image_settings.color_depth
    original_res_x = render.resolution_x
    original_res_y = render.resolution_y
    original_use_sequencer = render.use_sequencer
    original_mutes = {}
    for s in se.strips:
        original_mutes[s.name] = s.mute
    original_camera = scene.camera

    # ── Setup scene ───────────────────────────────────────────────
    mod_enabled = {}
    try:
        # Mute all strips except target
        for s in se.strips:
            s.mute = True
        strip.mute = False

        # L0: disable modifiers
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
        if fmt == "EXR":
            render.image_settings.file_format = "OPEN_EXR"
            render.image_settings.color_mode = "RGBA"
            render.image_settings.color_depth = "32"
        elif fmt == "JPEG":
            render.image_settings.file_format = "JPEG"
            render.image_settings.color_mode = "RGB"
            render.image_settings.quality = quality
        else:  # PNG
            render.image_settings.file_format = "PNG"
            render.image_settings.color_mode = "RGBA"
            render.image_settings.quality = quality

        # ── Render ────────────────────────────────────────────────
        return _do_raw_render(scene, output_path)

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
        render.image_settings.color_depth = original_color_depth
        render.resolution_x = original_res_x
        render.resolution_y = original_res_y
        scene.camera = original_camera
        scene.frame_set(original_frame)


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
        self._viewport_area = _find_viewport_area()

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
        if self._cancel_flag or not self._render_queue:
            self._is_rendering = False
            self._is_internal_rendering = False
            return None
        return 0.05

    # ── Single frame render ───────────────────────────────────────────

    def _render_single_frame(self, strip_name: str, frame: int, layer: int):
        """Render a single frame to disk, then store in RAM cache (hot path).

        After a successful render the frame bytes are also stored in the
        RAM hot cache so subsequent accesses are served from memory.
        """
        scene = bpy.context.scene
        se = scene.sequence_editor
        if not se:
            return

        strip = self._find_strip(se, strip_name)
        if strip is None:
            return

        settings = bpy.context.scene.smart_cache
        fmt = settings.cache_format
        quality = settings.jpeg_quality if fmt == "JPEG" else settings.cache_quality
        output_path = self.cache_manager.get_frame_path(strip, frame, layer, fmt=fmt)
        if os.path.exists(output_path):
            # Already cached on disk — record in index if missing
            self.cache_manager.record_cached_frame(strip, frame, layer, output_path)
            return

        self._is_internal_rendering = True
        t0 = time.time()
        try:
            rendered = _render_strip_frame(scene, strip, frame, output_path, layer, fmt=fmt, quality=quality)
            elapsed_ms = (time.time() - t0) * 1000.0
            self.cache_manager.record_render(elapsed_ms)

            if rendered and os.path.exists(output_path):
                self.cache_manager.record_cached_frame(strip, frame, layer, output_path)

                # Newly rendered frames are hot — promote to RAM cache
                with open(output_path, 'rb') as f:
                    frame_bytes = f.read()
                key = ck.compute_frame_key(strip, frame, layer)
                self.cache_manager.store_ram_frame(key, frame_bytes)

                print(f"[Smart Cache] ✓ Cached: {strip_name} frame {frame} "
                      f"→ {os.path.basename(output_path)} (RAM+SSD)")
            elif os.path.exists(output_path):
                # File exists but render claimed failure — record anyway
                self.cache_manager.record_cached_frame(strip, frame, layer, output_path)
            else:
                print(f"[Smart Cache] ✗ Render FAILED: {strip_name} frame {frame} "
                      f"— output file missing")

        except Exception as e:
            print(f"[Smart Cache] ✗ Render FAILED: {strip_name} frame {frame}: {e}")
            import traceback
            traceback.print_exc()

        finally:
            self._is_internal_rendering = False

    # ── On-Demand render (L2 cold path) ───────────────────────────────

    def render_on_demand(self, strip, frame: int, layer: int = 0) -> bytes | None:
        """Render a frame on demand without disk caching.

        Renders to a temp file, reads the bytes back, removes the temp file.
        Does NOT write to the persistent cache or update the index.
        Does NOT store in the RAM hot cache (designed for infrequent access).

        Returns image bytes or None on failure.
        """
        settings = bpy.context.scene.smart_cache
        fmt = settings.cache_format
        quality = settings.jpeg_quality if fmt == "JPEG" else settings.cache_quality
        ext_map = {"PNG": ".png", "JPEG": ".jpg", "EXR": ".exr"}
        scene = bpy.context.scene
        fd, tmp_path = tempfile.mkstemp(suffix=ext_map.get(fmt, ".png"))
        os.close(fd)

        self._is_internal_rendering = True
        try:
            rendered = _render_strip_frame(scene, strip, frame, tmp_path, layer, fmt=fmt, quality=quality)
            if rendered and os.path.exists(tmp_path):
                with open(tmp_path, 'rb') as f:
                    frame_bytes = f.read()
                print(f"[Smart Cache] ✓ On-Demand: {strip.name} frame {frame} "
                      f"({len(frame_bytes) / 1024:.0f} KB)")
                return frame_bytes

            print(f"[Smart Cache] ✗ On-Demand FAILED: {strip.name} frame {frame}")
            return None

        except Exception as e:
            print(f"[Smart Cache] ✗ On-Demand error: {strip.name} frame {frame}: {e}")
            import traceback
            traceback.print_exc()
            return None

        finally:
            self._is_internal_rendering = False
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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
