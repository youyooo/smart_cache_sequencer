# SPDX-License-Identifier: GPL-3.0-or-later

"""Cache index, LRU eviction, and disk management."""

import json
import os
import time

from . import cache_key as ck


class CacheManager:
    """Manages the disk cache with LRU eviction.

    Directory structure:
        strips/{strip_name}_{base_hash}/
            L0/                  # base strip output (no modifiers)
                {name}_####.png  # Blender-readable image sequence
            L1/{mod_hash}/       # strip + modifiers
                {name}_####.png
    """

    def __init__(self, base_dir: str, max_size_gb: float = 10.0):
        self.base_dir = base_dir
        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        self.index: dict[str, dict] = {}  # cache_key -> metadata
        self.strip_hashes: dict[str, dict] = {}  # strip_name -> {base_hash, mod_hash}
        self._load_index()

    @property
    def strips_dir(self):
        return os.path.join(self.base_dir, "strips")

    @property
    def index_path(self):
        return os.path.join(self.base_dir, "index.json")

    def ensure_dirs(self):
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.strips_dir, exist_ok=True)

    def get_cache_dir(self, strip, layer: int = 0) -> str:
        """Get the directory where cached frames for this strip are stored.

        Uses a naming scheme Blender recognizes as an image sequence:
            strips/{safe_name}_{hash}/L0/{safe_name}_####.png
        """
        base = ck.strip_base_hash(strip)
        safe_name = _safe_strip_name(strip.name)

        if layer == 0:
            dir_path = os.path.join(self.strips_dir, f"{safe_name}_{base}", "L0")
        else:
            mod_hash = ck.strip_modifier_hash(strip)
            dir_path = os.path.join(self.strips_dir, f"{safe_name}_{base}", "L1", mod_hash or "none")

        os.makedirs(dir_path, exist_ok=True)
        return dir_path

    def get_frame_path(self, strip, frame: int, layer: int = 0) -> str:
        """Get the expected file path for a cached frame."""
        cache_dir = self.get_cache_dir(strip, layer)
        safe_name = _safe_strip_name(strip.name)
        return os.path.join(cache_dir, f"{safe_name}_{frame:04d}.png")

    def frame_exists(self, strip, frame: int, layer: int = 0) -> bool:
        """Check if a cached frame exists on disk."""
        path = self.get_frame_path(strip, frame, layer)
        return os.path.exists(path)

    def record_cached_frame(self, strip, frame: int, layer: int, path: str):
        """Record a newly cached frame in the index."""
        key = ck.compute_frame_key(strip, frame, layer)
        file_size = os.path.getsize(path) if os.path.exists(path) else 0

        self.index[key] = {
            'strip_name': strip.name,
            'strip_hash': ck.strip_base_hash(strip),
            'frame': frame,
            'layer': layer,
            'path': path,
            'file_size': file_size,
            'created_at': time.time(),
            'last_accessed': time.time(),
        }

        self.strip_hashes[strip.name] = {
            'base_hash': ck.strip_base_hash(strip),
            'mod_hash': ck.strip_modifier_hash(strip),
        }

        self._maybe_evict()
        self._save_index()

    def touch_frame(self, strip, frame: int, layer: int = 0):
        """Mark a frame as recently accessed."""
        key = ck.compute_frame_key(strip, frame, layer)
        if key in self.index:
            self.index[key]['last_accessed'] = time.time()

    def invalidate_strip(self, strip_name: str, layer: int = None):
        """Invalidate cache for a strip."""
        keys_to_remove = []
        for key, entry in self.index.items():
            if entry['strip_name'] != strip_name:
                continue
            if layer is not None and entry['layer'] != layer:
                continue
            keys_to_remove.append(key)

        for key in keys_to_remove:
            entry = self.index.pop(key)
            if os.path.exists(entry['path']):
                try:
                    os.remove(entry['path'])
                except OSError:
                    pass

        if strip_name in self.strip_hashes:
            if layer is None:
                del self.strip_hashes[strip_name]

        self._save_index()

    def purge_all(self):
        """Delete all cache files and reset index."""
        import shutil
        if os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir)
        self.index.clear()
        self.strip_hashes.clear()

    def purge_stale(self):
        """Remove cache for strips no longer in the scene."""
        keys_to_remove = []
        for key, entry in self.index.items():
            if not self._find_strip_by_name(entry['strip_name']):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            entry = self.index.pop(key)
            if os.path.exists(entry['path']):
                try:
                    os.remove(entry['path'])
                except OSError:
                    pass
        self._save_index()

    def get_disk_usage(self) -> dict:
        """Get cache disk usage stats."""
        total_size = 0
        frame_count = 0
        strip_count = set()

        for entry in self.index.values():
            total_size += entry['file_size']
            frame_count += 1
            strip_count.add(entry['strip_name'])

        return {
            'total_size_mb': total_size / (1024 * 1024),
            'frame_count': frame_count,
            'strip_count': len(strip_count),
            'max_size_gb': self.max_size_bytes / (1024 * 1024 * 1024),
        }

    def strip_has_cache(self, strip_name: str, layer: int = 0) -> bool:
        """Check if any frames are cached for a strip at the given layer."""
        for entry in self.index.values():
            if entry['strip_name'] == strip_name and entry['layer'] == layer:
                return True
        return False

    def get_cached_frames(self, strip_name: str, layer: int = 0) -> list[int]:
        """Get list of cached frame numbers for a strip."""
        frames = []
        for entry in self.index.values():
            if entry['strip_name'] == strip_name and entry['layer'] == layer:
                frames.append(entry['frame'])
        return sorted(frames)

    def get_cache_dir_for_strip(self, strip_name: str, layer: int = 0) -> str:
        """Get the cache directory path for a strip (for proxy strip creation)."""
        for entry in self.index.values():
            if entry['strip_name'] == strip_name and entry['layer'] == layer:
                return os.path.dirname(entry['path'])
        return ""

    def _maybe_evict(self):
        """LRU eviction when cache exceeds max size."""
        total_size = sum(e['file_size'] for e in self.index.values())
        if total_size <= self.max_size_bytes:
            return

        sorted_keys = sorted(self.index.keys(), key=lambda k: self.index[k]['last_accessed'])
        for key in sorted_keys:
            if total_size <= self.max_size_bytes * 0.8:
                break
            entry = self.index.pop(key)
            total_size -= entry['file_size']
            if os.path.exists(entry['path']):
                try:
                    os.remove(entry['path'])
                except OSError:
                    pass

    def _find_strip_by_name(self, name: str):
        """Find a strip by name in the current scene's sequence editor."""
        import bpy
        scene = bpy.context.scene
        if not scene.sequence_editor:
            return None
        for s in scene.sequence_editor.strips:
            if s.name == name:
                return s
        return None

    def _load_index(self):
        if not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, 'r') as f:
                data = json.load(f)
            self.index = data.get('entries', {})
            self.strip_hashes = data.get('strip_hashes', {})
        except (json.JSONDecodeError, KeyError):
            self.index = {}
            self.strip_hashes = {}

    def _save_index(self):
        self.ensure_dirs()
        data = {
            'entries': self.index,
            'strip_hashes': self.strip_hashes,
        }
        with open(self.index_path, 'w') as f:
            json.dump(data, f, indent=2)


def _safe_strip_name(name: str) -> str:
    """Convert strip name to a filesystem-safe name."""
    safe = "".join(c if c.isalnum() or c == '_' else '_' for c in name)
    return safe[:32]
