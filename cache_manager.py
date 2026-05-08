# SPDX-License-Identifier: GPL-3.0-or-later

"""Cache index, LRU eviction, disk management, and layered cache.

Layered cache architecture:
  - RAM (L0 hot): in-memory PNG bytes for recently accessed frames
  - SSD (L1 warm): on-disk PNG files for cached frames
  - On-Demand (L2 cold): rendered on request, not cached
"""

import json
import os
import tempfile
import time
from collections import OrderedDict

from . import cache_key as ck


class CacheManager:
    """Manages the disk cache with LRU eviction and layered cache support.

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

        # RAM cache (L0 hot frames) — store PNG bytes for recently accessed frames
        self.ram_cache: OrderedDict[str, bytes] = OrderedDict()
        # 80 % of 512 MB default; 20 % reserved for system
        self.ram_cache_max_bytes: int = int(512 * 0.8 * 1024 * 1024)
        self.ram_cache_current_bytes: int = 0

        # Cache hit statistics
        self.ram_hits: int = 0          # frames served from RAM
        self.ssd_hits: int = 0          # frames served from disk
        self.on_demand_count: int = 0   # frames rendered on-demand (L2)

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

    def get_frame_path(self, strip, frame: int, layer: int = 0, fmt: str = "PNG") -> str:
        """Get the expected file path for a cached frame.

        Args:
            fmt: Cache format — "PNG" (.png), "JPEG" (.jpg), or "EXR" (.exr).
        """
        cache_dir = self.get_cache_dir(strip, layer)
        safe_name = _safe_strip_name(strip.name)
        ext_map = {"PNG": ".png", "JPEG": ".jpg", "EXR": ".exr"}
        ext = ext_map.get(fmt, ".png")
        return os.path.join(cache_dir, f"{safe_name}_{frame:04d}{ext}")

    def get_frame_format(self, strip_name: str, layer: int = 0) -> str:
        """Get the cached format for a strip at a given layer.

        Returns format string ("PNG", "JPEG", "EXR") or "PNG" if no frames cached.
        """
        for entry in self.index.values():
            if entry['strip_name'] == strip_name and entry['layer'] == layer:
                return entry.get('format', 'PNG')
        return "PNG"

    def frame_exists(self, strip, frame: int, layer: int = 0) -> bool:
        """Check if a cached frame exists on disk."""
        path = self.get_frame_path(strip, frame, layer)
        return os.path.exists(path)

    def record_cached_frame(self, strip, frame: int, layer: int, path: str):
        """Record a newly cached frame in the index."""
        key = ck.compute_frame_key(strip, frame, layer)
        file_size = os.path.getsize(path) if os.path.exists(path) else 0

        # Infer format from file extension
        ext = os.path.splitext(path)[1].lower()
        fmt_map = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.exr': 'EXR'}
        frame_format = fmt_map.get(ext, 'PNG')

        self.index[key] = {
            'strip_name': strip.name,
            'strip_hash': ck.strip_base_hash(strip),
            'frame': frame,
            'layer': layer,
            'path': path,
            'file_size': file_size,
            'format': frame_format,
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
        """Delete all cache files and reset index. Also clears RAM cache."""
        import shutil
        if os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir)
        self.index.clear()
        self.strip_hashes.clear()
        self.ram_cache.clear()
        self.ram_cache_current_bytes = 0

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

    # ── RAM cache (L0 hot frames) ────────────────────────────────────

    def set_ram_cache_limit(self, limit_mb: int):
        """Set RAM cache limit. Only 80% is usable (20% reserved for system)."""
        self.ram_cache_max_bytes = int(limit_mb * 0.8 * 1024 * 1024)

    def store_ram_frame(self, key: str, pixels_bytes: bytes):
        """Store rendered frame bytes in the RAM hot cache.

        If the key already exists, it is updated and moved to the
        most-recently-used position.  LRU eviction runs automatically
        if the cache exceeds its memory limit.
        """
        if key in self.ram_cache:
            old_size = len(self.ram_cache[key])
            self.ram_cache_current_bytes -= old_size
            del self.ram_cache[key]
        self.ram_cache[key] = pixels_bytes
        self.ram_cache_current_bytes += len(pixels_bytes)
        self._evict_ram_if_needed()

    def fetch_ram_frame(self, key: str) -> bytes | None:
        """Retrieve frame bytes from RAM cache.

        Moves the accessed entry to the most-recently-used position
        (O(1) via OrderedDict). Returns None on cache miss.
        """
        if key not in self.ram_cache:
            return None
        self.ram_cache.move_to_end(key)  # Mark as recently used
        self.ram_hits += 1
        return self.ram_cache[key]

    def _evict_ram_if_needed(self):
        """LRU evict from RAM until under the memory limit.

        Evicted frames are NOT removed from the SSD cache — they remain
        available as warm frames (demoted from hot to warm tier).
        """
        while self.ram_cache_current_bytes > self.ram_cache_max_bytes and self.ram_cache:
            key, data = self.ram_cache.popitem(last=False)
            self.ram_cache_current_bytes -= len(data)

    def promote_to_ram(self, strip, frame: int, layer: int = 0) -> bool:
        """Read a disk-cached frame into the RAM hot cache.

        If the frame is already in RAM, it is refreshed (moved to the
        most-recently-used position).  Returns True on success, False
        if the frame is not on disk.
        """
        path = self.get_frame_path(strip, frame, layer)
        if not os.path.exists(path):
            return False
        key = ck.compute_frame_key(strip, frame, layer)
        if key in self.ram_cache:
            self.ram_cache.move_to_end(key)
            return True
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.store_ram_frame(key, data)
            return True
        except OSError:
            return False

    def demote_to_ssd(self, strip, frame: int, layer: int = 0):
        """Remove a frame from RAM cache. The SSD (disk) copy is preserved.

        This is a hot→warm demotion: the frame was promoted to RAM but
        is now being evicted.  It remains fully accessible from the
        SSD tier.
        """
        key = ck.compute_frame_key(strip, frame, layer)
        if key in self.ram_cache:
            old_size = len(self.ram_cache[key])
            self.ram_cache_current_bytes -= old_size
            del self.ram_cache[key]

    def get_ram_usage(self) -> dict:
        """Get RAM cache usage statistics."""
        return {
            'used_bytes': self.ram_cache_current_bytes,
            'max_bytes': self.ram_cache_max_bytes,
            'frame_count': len(self.ram_cache),
        }

    def get_hit_stats(self) -> dict:
        """Get cache hit statistics per tier."""
        return {
            'ram_hits': self.ram_hits,
            'ssd_hits': self.ssd_hits,
            'on_demand_count': self.on_demand_count,
        }

    # ── L2 on-demand rendering (cold path) ───────────────────────────

    def render_on_demand(self, strip, frame: int, layer: int = 0) -> bytes | None:
        """Render a single frame on demand without disk caching.

        L2 cold path: renders the frame to a temporary file, reads the
        image bytes back, and removes the temporary file.  Does NOT write
        to the persistent cache or record in the index.

        Designed for jump-to-frame or infrequently-accessed frames in
        long sequences.

        Returns image bytes or None on failure.
        """
        from .cache_render import _render_strip_frame
        import bpy
        settings = bpy.context.scene.smart_cache
        fmt = settings.cache_format
        quality = settings.jpeg_quality if fmt == "JPEG" else settings.cache_quality
        ext_map = {"PNG": ".png", "JPEG": ".jpg", "EXR": ".exr"}
        scene = bpy.context.scene
        fd, tmp_path = tempfile.mkstemp(suffix=ext_map.get(fmt, ".png"))
        os.close(fd)
        try:
            rendered = _render_strip_frame(scene, strip, frame, tmp_path, layer, fmt=fmt, quality=quality)
            if rendered and os.path.exists(tmp_path):
                with open(tmp_path, 'rb') as f:
                    frame_bytes = f.read()
                self.on_demand_count += 1
                return frame_bytes
            return None
        except Exception as e:
            print(f"[Smart Cache] On-Demand render failed: {strip.name} frame {frame}: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ── Disk-index helpers ───────────────────────────────────────────

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
            # Preserve any extra keys (e.g. cache_state from PrefetchManager)
            self._extra_index_data = {
                k: v for k, v in data.items()
                if k not in ('entries', 'strip_hashes')
            }
        except (json.JSONDecodeError, KeyError):
            self.index = {}
            self.strip_hashes = {}
            self._extra_index_data = {}

    def _save_index(self):
        self.ensure_dirs()
        data = {
            'entries': self.index,
            'strip_hashes': self.strip_hashes,
        }
        # Preserve extra metadata written by other modules (e.g. cache_state)
        extra = getattr(self, '_extra_index_data', None)
        if extra:
            data.update(extra)
        with open(self.index_path, 'w') as f:
            json.dump(data, f, indent=2)


def _safe_strip_name(name: str) -> str:
    """Convert strip name to a filesystem-safe name."""
    safe = "".join(c if c.isalnum() or c == '_' else '_' for c in name)
    return safe[:32]
