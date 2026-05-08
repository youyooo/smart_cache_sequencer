# SPDX-License-Identifier: GPL-3.0-or-later

"""Enhanced prefetch manager with playhead pre-rendering, idle background
rendering, and session state recovery.

This module replaces the basic PrefetchManager that was previously in
cache_serve.py with a more capable version supporting three operating modes:

  - Playhead Prefetch: Queue frames N frames ahead of the current playhead
    position during playback. Audio-track strips are prioritized.
  - Idle Rendering: Background fill of uncached frames when the renderer
    is idle, using a configurable scanning strategy.
  - Session Recovery: Auto-restore cache state on project load so users
    don't need to manually re-cache after reopening a .blend file.
"""

import bpy
import json
import os
import time

from .cache_key import CACHEABLE_TYPES


class PrefetchManager:
    """Pre-renders frames ahead of the playhead and during idle time.

    Three operating modes:
      - Playhead Prefetch: Queue frames N frames ahead of current position
      - Idle Rendering: Background fill of uncached frames when renderer is idle
      - Session Recovery: Auto-restore cache state on project load
    """

    def __init__(self, cache_manager, cache_render,
                 lookahead=30, strategy='balanced',
                 idle_enabled=True, session_recovery=True):
        self.cache_manager = cache_manager
        self.cache_render = cache_render
        self.lookahead = lookahead
        self.strategy = strategy           # 'conservative', 'balanced', 'aggressive'
        self.idle_enabled = idle_enabled
        self.session_recovery = session_recovery
        self._last_frame = -1
        self._session_id = str(time.time())

    @property
    def queue_length(self):
        """Number of pending frames in the render queue."""
        if hasattr(self.cache_render, '_render_queue'):
            return len(self.cache_render._render_queue)
        return 0

    # ── Playhead Prefetch ────────────────────────────────────────────

    def on_playhead_move(self, current_frame: int, scene):
        """Queue uncached frames ahead of the playhead during playback.

        Only fires during active animation playback and when the renderer
        is not already busy.  Prioritises strips with audio tracks.
        """
        if current_frame == self._last_frame:
            return
        self._last_frame = current_frame

        if self.cache_render.is_rendering:
            return

        se = scene.sequence_editor
        if not se:
            return

        # Collect cacheable strips at the current playhead position
        active_strips = []
        for strip in se.strips:
            if strip.mute or strip.lock:
                continue
            if strip.type not in CACHEABLE_TYPES:
                continue
            if strip.frame_final_start <= current_frame <= strip.frame_final_end:
                active_strips.append(strip)

        if not active_strips:
            return

        # Audio-aware: strips with an audio channel come first
        active_strips.sort(key=_audio_priority)

        effective_lookahead = self._compute_lookahead()

        layers = [0]
        has_modifiers = any(
            hasattr(s, 'modifiers') and len(s.modifiers) > 0
            for s in active_strips
        )
        if has_modifiers:
            layers.append(1)

        queued = 0
        for strip in active_strips:
            end = strip.frame_final_end
            lookahead_end = min(current_frame + effective_lookahead, end)
            for frame in range(current_frame + 1, lookahead_end + 1):
                for layer in layers:
                    if not self.cache_manager.frame_exists(strip, frame, layer):
                        self.cache_render.queue_frame(strip, frame, layer)
                        queued += 1

        if queued > 0:
            self.cache_render.start_render()
            print(f"[Smart Cache] Prefetch: queued {queued} frame(s) ahead of playhead")

    # ── Idle Rendering ───────────────────────────────────────────────

    def on_idle(self, scene):
        """Timer callback — render uncached frames when renderer is idle.

        Designed to be called from a bpy.app.timers callback every ~2 seconds.
        The strategy setting controls how aggressively we scan for work:

          - conservative: only the strip at the current playhead position
          - balanced:     strips overlapping a 120-frame window around playhead
          - aggressive:   all cacheable strips in the timeline
        """
        if not self.idle_enabled:
            return

        if self.cache_render.is_rendering:
            return

        se = scene.sequence_editor
        if not se:
            return

        current_frame = scene.frame_current
        candidates = []

        if self.strategy == 'conservative':
            # Only the topmost strip at current playhead
            for strip in reversed(se.strips):
                if strip.mute or strip.lock:
                    continue
                if strip.type not in CACHEABLE_TYPES:
                    continue
                s, e = strip.frame_final_start, strip.frame_final_end
                if s <= current_frame <= e:
                    candidates.append(strip)
                    break

        elif self.strategy == 'balanced':
            # Strips in a 120-frame window around playhead
            region_start = current_frame - 60
            region_end = current_frame + 60
            for strip in se.strips:
                if strip.mute or strip.lock:
                    continue
                if strip.type not in CACHEABLE_TYPES:
                    continue
                s, e = strip.frame_final_start, strip.frame_final_end
                if e >= region_start and s <= region_end:
                    candidates.append(strip)

        else:  # aggressive
            # All cacheable, unmuted, unlocked strips
            for strip in se.strips:
                if strip.mute or strip.lock:
                    continue
                if strip.type not in CACHEABLE_TYPES:
                    continue
                candidates.append(strip)

        if not candidates:
            return

        # Queue a small batch of missing frames per tick (avoids blocking UI)
        batch_limit = 5
        total_queued = 0

        for strip in candidates:
            s = strip.frame_final_start
            e = strip.frame_final_end
            for frame in range(s, e + 1):
                if total_queued >= batch_limit:
                    break
                if not self.cache_manager.frame_exists(strip, frame, 0):
                    self.cache_render.queue_frame(strip, frame, 0)
                    total_queued += 1
            if total_queued >= batch_limit:
                break

        if total_queued > 0:
            self.cache_render.start_render()

    # ── Session Recovery ─────────────────────────────────────────────

    def save_session_state(self):
        """Persist current cache state alongside the cache index.

        Adds a ``cache_state`` section to *index.json* recording which
        strips were cached, their frame ranges, and a session identifier.
        """
        if not self.cache_manager:
            return

        # Collect snapshot of currently cached strips and their ranges
        strip_frames = {}
        for entry in list(self.cache_manager.index.values()):
            name = entry['strip_name']
            if name not in strip_frames:
                strip_frames[name] = {'frames': [], 'layers': set()}
            strip_frames[name]['frames'].append(entry['frame'])
            strip_frames[name]['layers'].add(entry['layer'])

        ranges = {}
        for name, info in strip_frames.items():
            sorted_frames = sorted(set(info['frames']))
            if sorted_frames:
                ranges[name] = {
                    'frame_start': min(sorted_frames),
                    'frame_end': max(sorted_frames),
                    'layers': sorted(info['layers']),
                }

        index_path = self.cache_manager.index_path
        if os.path.exists(index_path):
            try:
                with open(index_path, 'r') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}

        data['cache_state'] = {
            'session_id': self._session_id,
            'last_updated': time.time(),
            'strips': ranges,
        }

        try:
            os.makedirs(os.path.dirname(index_path), exist_ok=True)
            with open(index_path, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            print(f"[Smart Cache] Failed to save session state: {e}")

    def load_session_state(self, scene) -> bool:
        """Restore cache state after project load.

        Reads ``cache_state`` from *index.json* and re-queues any frames
        that are missing from the cache (e.g. from an interrupted session).

        Returns True if session state was found and processed, False otherwise.
        """
        if not self.session_recovery:
            return False
        if not self.cache_manager:
            return False

        index_path = self.cache_manager.index_path
        if not os.path.exists(index_path):
            return False

        try:
            with open(index_path, 'r') as f:
                data = json.load(f)

            cache_state = data.get('cache_state', {})
            stored_strips = cache_state.get('strips', {})
            if not stored_strips:
                return False

            se = scene.sequence_editor
            if not se:
                return True  # state exists but no SE yet — still "found"

            missing_count = 0
            for strip_name, info in stored_strips.items():
                strip = se.strips.get(strip_name)
                if not strip:
                    continue
                if strip.mute:
                    continue
                if strip.type not in CACHEABLE_TYPES:
                    continue

                frame_start = info.get('frame_start', strip.frame_final_start)
                frame_end = info.get('frame_end', strip.frame_final_end)
                layers = info.get('layers', [0])

                for layer in layers:
                    for frame in range(frame_start, frame_end + 1):
                        if not self.cache_manager.frame_exists(strip, frame, layer):
                            self.cache_render.queue_frame(strip, frame, layer)
                            missing_count += 1

            if missing_count > 0:
                self.cache_render.start_render()
                print(f"[Smart Cache] Session recovery: re-queued {missing_count} missing frame(s)")
            else:
                print("[Smart Cache] Session recovery: all frames present")

            return True

        except (json.JSONDecodeError, OSError, KeyError) as e:
            print(f"[Smart Cache] Session recovery failed: {e}")
            return False

    # ── Configuration Updates ────────────────────────────────────────

    def update_config(self, *, lookahead=None, strategy=None,
                      idle_enabled=None, session_recovery=None):
        """Update prefetch configuration at runtime."""
        if lookahead is not None:
            self.lookahead = lookahead
        if strategy is not None:
            self.strategy = strategy
        if idle_enabled is not None:
            self.idle_enabled = idle_enabled
        if session_recovery is not None:
            self.session_recovery = session_recovery

    # ── Internals ────────────────────────────────────────────────────

    def _compute_lookahead(self) -> int:
        """Compute effective lookahead based on current strategy."""
        if self.strategy == 'conservative':
            return self.lookahead
        elif self.strategy == 'aggressive':
            return self.lookahead * 2
        else:  # balanced
            return int(self.lookahead * 1.5)


# ── Module-level helpers ────────────────────────────────────────────

def _audio_priority(strip) -> int:
    """Return 0 if strip has an audio track, 1 otherwise."""
    if hasattr(strip, 'audio') and strip.audio is not None:
        return 0
    return 1
