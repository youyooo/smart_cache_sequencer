# SPDX-License-Identifier: GPL-3.0-or-later

"""System cache management — take over VSE native cache.

Disables Blender's built-in VSE cache (use_cache_raw, use_cache_final)
when the plugin is active, and saves original settings so they can be
cleanly restored on plugin unload.
"""

import traceback

_original_cache_settings = {}  # scene.name -> dict of original values


def takeover_system_cache(scene):
    """Disable native VSE cache and save original settings.

    Saves current use_cache_raw / use_cache_final values, then sets
    both to False so the plugin is the sole caching authority.

    Returns True if takeover was successful, False otherwise.
    """
    se = scene.sequence_editor
    if not se:
        return False

    scene_name = scene.name
    if scene_name not in _original_cache_settings:
        _original_cache_settings[scene_name] = {
            'use_cache_raw': bool(se.use_cache_raw),
            'use_cache_final': bool(se.use_cache_final),
        }

    se.use_cache_raw = False
    se.use_cache_final = False
    print(f"[Smart Cache] System cache taken over for scene '{scene_name}'")
    return True


def restore_system_cache(scene):
    """Restore original native VSE cache settings.

    Safe to call multiple times — only restores once.
    In Blender 5.x restores use_cache_raw and use_cache_final booleans.
    """
    se = scene.sequence_editor
    if not se:
        return

    scene_name = scene.name
    if scene_name in _original_cache_settings:
        orig = _original_cache_settings.pop(scene_name)
        try:
            se.use_cache_raw = orig['use_cache_raw']
        except Exception:
            pass
        try:
            se.use_cache_final = orig['use_cache_final']
        except Exception:
            pass
        print(f"[Smart Cache] System cache restored for scene '{scene_name}': "
              f"use_cache_raw={orig['use_cache_raw']}, "
              f"use_cache_final={orig['use_cache_final']}")
    else:
        print(f"[Smart Cache] No saved settings to restore for scene '{scene_name}'")


def is_taken_over(scene):
    """Check if native cache has been taken over for the given scene.

    A scene is considered "taken over" when the plugin has previously
    disabled its native cache and saved the original values.
    """
    se = scene.sequence_editor
    if not se:
        return False
    return scene.name in _original_cache_settings


def restore_if_taken_over(context):
    """Convenience helper to restore from a Blender context.

    Safe to call with None context — becomes a silent no-op.
    """
    if context is None:
        return
    scene = getattr(context, 'scene', None)
    if scene is not None:
        restore_system_cache(scene)


def get_cache_info(scene):
    """Get current native cache state and usage for display.

    Returns a dict with:
        taken_over (bool)
        use_cache_raw (bool)
        use_cache_final (bool)
        cache_raw_size (int)  — MB, read-only from Blender
        cache_final_size (int) — MB, read-only from Blender
    """
    se = scene.sequence_editor
    if not se:
        return {}
    return {
        'taken_over': is_taken_over(scene),
        'use_cache_raw': se.use_cache_raw,
        'use_cache_final': se.use_cache_final,
        'cache_raw_size': se.cache_raw_size,
        'cache_final_size': se.cache_final_size,
    }
