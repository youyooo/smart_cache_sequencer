# SPDX-License-Identifier: GPL-3.0-or-later

"""Track management for VSE: auto-pair video/audio, sync move, channel lock/solo."""

import bpy


class TrackManager:
    """Manages VSE track relationships.

    Features:
    - Auto-detection and pairing of video strips with their audio tracks
    - Synchronized channel movement for paired strips
    - Channel locking (prevent selection/movement)
    - Channel solo (mute all but soloed channel)
    """

    def __init__(self):
        self._track_pairs: dict[int, int] = {}
        self._locked_channels: set[int] = set()
        self._solo_channels: set[int] = set()
        self._last_strip_count: int = -1

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_video_audio_pairs(self, context) -> list[tuple]:
        """Detect video strips and their associated audio strips.

        Traverses all MOVIE strips and finds paired SOUND strips via:
        - Shared Sound datablock reference (strip.sound pointer comparison)
        - Name prefix matching as fallback

        Returns:
            list of (video_strip, audio_strip) tuples.
        """
        se = context.scene.sequence_editor
        if not se:
            return []

        pairs: list[tuple] = []
        for strip in se.strips:
            if strip.type not in ('MOVIE',):
                continue

            audio_strip = self._find_paired_audio(se, strip)
            if audio_strip:
                pairs.append((strip, audio_strip))

        return pairs

    def _find_paired_audio(self, se, video_strip):
        """Find the SOUND strip paired with *video_strip*.

        Precedence:
          1. Sound datablock reference (strip.sound)
          2. Name prefix matching
        """
        sound = getattr(video_strip, 'sound', None)
        if sound:
            match = self._find_audio_by_sound_ref(se, sound)
            if match:
                return match

        return self._find_audio_by_name(se, video_strip)

    @staticmethod
    def _find_audio_by_sound_ref(se, sound_data):
        """Find a SOUND strip referencing the same Sound datablock."""
        for s in se.strips:
            if s.type != 'SOUND':
                continue
            if getattr(s, 'sound', None) is sound_data:
                return s
        return None

    @staticmethod
    def _find_audio_by_name(se, video_strip):
        """Fallback: match by base name up to the first extension or suffix."""
        base = video_strip.name.rsplit('.', 1)[0] if '.' in video_strip.name else video_strip.name

        for s in se.strips:
            if s.type != 'SOUND':
                continue
            s_base = s.name.rsplit('.', 1)[0] if '.' in s.name else s.name
            if s_base == base or s_base == base + '_audio':
                return s
        return None

    # ------------------------------------------------------------------
    # Auto-pairing
    # ------------------------------------------------------------------

    def auto_pair_strips(self, context) -> int:
        """Move each detected audio strip to the channel directly
        below its paired video strip.

        Returns:
            Number of strips that were re-paired.
        """
        se = context.scene.sequence_editor
        if not se:
            return 0

        pairs = self.detect_video_audio_pairs(context)
        count = 0

        for video_strip, audio_strip in pairs:
            target = video_strip.channel - 1
            if target < 1:
                continue
            if audio_strip.channel == target:
                continue

            audio_strip.channel = target
            self._track_pairs[video_strip.channel] = target
            count += 1

        if count:
            se.update_tag()

        return count

    def check_and_auto_pair(self, context) -> int:
        """Lightweight check that only scans when the total strip count
        has changed.  Returns number of new pairings (0 if nothing changed)."""
        se = context.scene.sequence_editor
        if not se:
            self._last_strip_count = -1
            return 0

        current = len(se.strips)
        if current == self._last_strip_count:
            return 0

        self._last_strip_count = current
        return self.auto_pair_strips(context)

    # ------------------------------------------------------------------
    # Synchronised movement
    # ------------------------------------------------------------------

    def sync_move(self, context, video_name: str, new_channel: int):
        """Move the named video strip to *new_channel* and follow its
        paired audio strip.  Uses bpy.ops.sequencer.strip_move() when
        possible, falling back to direct channel assignment."""
        se = context.scene.sequence_editor
        if not se:
            return

        video_strip = se.strips.get(video_name)
        if not video_strip:
            return

        old_channel = video_strip.channel
        audio_channel = self._track_pairs.get(old_channel)
        delta = new_channel - old_channel

        # Move video strip
        video_strip.channel = new_channel

        # Move paired audio strip
        if audio_channel is not None and delta != 0:
            new_audio_ch = audio_channel + delta
            if new_audio_ch >= 1:
                audio_strip = self._find_audio_on_channel(se, audio_channel)
                if audio_strip:
                    # Try bpy.ops first (works in operator context)
                    try:
                        old_active = se.active_strip
                        saved = [s for s in se.strips if s.select]

                        for s in se.strips:
                            s.select = False
                        audio_strip.select = True
                        se.active_strip = audio_strip
                        bpy.ops.sequencer.strip_move(axis='Y', offset=delta)

                        for s in se.strips:
                            s.select = False
                        for s in saved:
                            s.select = True
                        se.active_strip = old_active
                    except RuntimeError:
                        # Fallback outside operator context
                        audio_strip.channel = new_audio_ch

                    self._track_pairs.pop(old_channel, None)
                    self._track_pairs[new_channel] = new_audio_ch

    @staticmethod
    def _find_audio_on_channel(se, channel):
        """Return the first SOUND strip on *channel*, or None."""
        for s in se.strips:
            if s.type == 'SOUND' and s.channel == channel:
                return s
        return None

    # ------------------------------------------------------------------
    # Channel lock / solo
    # ------------------------------------------------------------------

    def set_channel_lock(self, channel: int, locked: bool):
        """Lock or unlock *channel*.  Locked strips cannot be selected."""
        if locked:
            self._locked_channels.add(channel)
        else:
            self._locked_channels.discard(channel)
        self._apply_lock_state()

    def _apply_lock_state(self):
        """Enforce locked state on all strips in locked channels."""
        scene = bpy.context.scene
        se = getattr(scene, 'sequence_editor', None)
        if not se:
            return
        for strip in se.strips:
            if strip.channel in self._locked_channels:
                strip.select = False
                strip.select_left_handle = False
                strip.select_right_handle = False

    def set_channel_solo(self, channel: int, solo: bool):
        """Solo *channel* — mute all strips on other channels."""
        if solo:
            self._solo_channels.add(channel)
        else:
            self._solo_channels.discard(channel)
        self._apply_solo_state()

    def _apply_solo_state(self):
        """Mute/un-mute strips according to solo state."""
        scene = bpy.context.scene
        se = getattr(scene, 'sequence_editor', None)
        if not se:
            return
        if not self._solo_channels:
            # Un-mute everything if no solo channels
            for strip in se.strips:
                strip.mute = False
            return

        for strip in se.strips:
            strip.mute = strip.channel not in self._solo_channels

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def track_pairs(self) -> dict:
        return dict(self._track_pairs)

    @property
    def locked_channels(self) -> set:
        return set(self._locked_channels)

    @property
    def solo_channels(self) -> set:
        return set(self._solo_channels)

    def is_channel_locked(self, channel: int) -> bool:
        return channel in self._locked_channels

    def is_channel_soloed(self, channel: int) -> bool:
        return channel in self._solo_channels

    def clear(self):
        """Reset all track state."""
        self._track_pairs.clear()
        self._locked_channels.clear()
        self._solo_channels.clear()
        self._last_strip_count = -1
