# SPDX-License-Identifier: GPL-3.0-or-later

"""Waveform overlay management for VSE SoundStrips.

Leverages Blender's built-in waveform display (SEQUENCER_PT_sequencer_overlay_waveforms)
which is exposed in the Python API via sequence_editor.show_waveform (Blender 5.x).
No external dependencies like FFmpeg are required.
"""

import bpy


class WaveformManager:
    """Manage VSE waveform display and optional PNG waveform caching."""

    def __init__(self, cache_manager=None):
        self.cache_manager = cache_manager
        self._enabled = False

    def enable_waveforms(self, scene):
        """Enable waveform overlay on SoundStrips.

        Sets scene.sequence_editor.show_waveform = True (Blender 5.x).
        """
        se = scene.sequence_editor
        if se and hasattr(se, 'show_waveform'):
            se.show_waveform = True
            self._enabled = True
            print("[Smart Cache] Waveform overlay enabled")

    def disable_waveforms(self, scene):
        """Disable waveform overlay."""
        se = scene.sequence_editor
        if se and hasattr(se, 'show_waveform'):
            se.show_waveform = False
            self._enabled = False

    def is_enabled(self):
        return self._enabled

    def get_strip_waveform_strips(self, context) -> list:
        """Get all SoundStrips that have waveform display enabled.

        Returns list of (sound_strip, has_audio_data) tuples.
        """
        se = context.scene.sequence_editor
        if not se:
            return []
        results = []
        for s in se.strips:
            if s.type == 'SOUND':
                sound_data = getattr(s, 'sound', None)
                results.append((s, sound_data is not None))
        return results
