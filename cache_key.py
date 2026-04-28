# SPDX-License-Identifier: GPL-3.0-or-later

"""Hash-based cache key generation for VSE strips."""

import hashlib
import json

CACHEABLE_TYPES = {'MOVIE', 'IMAGE', 'SCENE'}


def _curve_mapping_data(curve_mapping):
    """Serialize curve mapping control points."""
    points = []
    for curve in curve_mapping.curves:
        curve_points = []
        for pt in curve.points:
            curve_points.append((pt.location.x, pt.location.y, pt.handle_type))
        points.append(curve_points)
    return points


def _serialize_modifier(mod):
    """Serialize a strip modifier to a hashable dict."""
    data = {'type': mod.type, 'mute': mod.mute, 'enable': mod.enable}

    if mod.type == 'BRIGHT_CONTRAST':
        data['bright'] = mod.bright
        data['contrast'] = mod.contrast
    elif mod.type == 'COLOR_BALANCE':
        cb = mod.color_balance
        data['method'] = cb.correction_method
        if cb.correction_method == 'OFFSET_POWER_SLOPE':
            data['lift'] = tuple(cb.lift)
            data['gamma'] = tuple(cb.gamma)
            data['gain'] = tuple(cb.gain)
        else:
            data['offset'] = tuple(cb.offset)
            data['power'] = tuple(cb.power)
            data['slope'] = tuple(cb.slope)
    elif mod.type == 'CURVES':
        data['curves'] = _curve_mapping_data(mod.curve_mapping)
    elif mod.type == 'HUE_CORRECT':
        data['curves'] = _curve_mapping_data(mod.curve_mapping)
    elif mod.type == 'TONEMAP':
        data['tonemap_type'] = mod.tonemap_type
        data['contrast'] = mod.contrast
        data['intensity'] = mod.intensity
        data['gamma'] = mod.gamma

    return data


def strip_base_hash(strip):
    """Compute hash of strip properties that affect raw visual output.

    Excludes timeline position (frame_start, channel) so moving a strip
    does NOT invalidate its cache.
    """
    data = {
        'type': strip.type,
    }

    if hasattr(strip, 'filepath'):
        data['filepath'] = strip.filepath
    if hasattr(strip, 'directory'):
        data['directory'] = strip.directory
    if hasattr(strip, 'elements'):
        data['elements'] = [e.filename for e in strip.elements]

    # Source content range
    data['frame_offset_start'] = strip.frame_offset_start
    data['frame_offset_end'] = strip.frame_offset_end

    # Crop
    if hasattr(strip, 'crop'):
        crop = strip.crop
        data['crop'] = {
            'top': crop.top, 'bottom': crop.bottom,
            'left': crop.left, 'right': crop.right,
        }

    # Transform (affects pixel output, not just position)
    data['use_translation'] = strip.use_translation
    data['use_transform'] = strip.use_transform
    if strip.use_translation or strip.use_transform:
        data['transform'] = {
            'offset_x': strip.transform.offset_x,
            'offset_y': strip.transform.offset_y,
            'scale_x': strip.transform.scale_x,
            'scale_y': strip.transform.scale_y,
            'rotation': strip.transform.rotation,
            'filter': strip.transform.filter,
        }

    # Image properties
    if hasattr(strip, 'use_flip_x'):
        data['flip_x'] = strip.use_flip_x
        data['flip_y'] = strip.use_flip_y
        data['color_saturation'] = strip.color_saturation

    # Color management
    if hasattr(strip, 'colorspace_settings'):
        data['colorspace'] = strip.colorspace_settings.name

    # Animation range for image sequences
    if hasattr(strip, 'animation_start'):
        data['animation_start'] = strip.animation_start
        data['animation_end'] = strip.animation_end

    # Blend properties that affect output
    data['blend_type'] = strip.blend_type
    data['blend_alpha'] = strip.blend_alpha

    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def strip_modifier_hash(strip):
    """Compute hash of the strip's modifier chain.

    Returns None if the strip has no modifiers.
    """
    if not hasattr(strip, 'modifiers') or len(strip.modifiers) == 0:
        return None

    mods = []
    for mod in strip.modifiers:
        mods.append(_serialize_modifier(mod))

    return hashlib.sha256(json.dumps(mods, sort_keys=True).encode()).hexdigest()[:12]


def cache_key(strip, frame, layer=0):
    """Compute full cache key for a strip at a specific frame.

    layer=0: base strip output (no modifiers)
    layer=1: strip + modifiers applied
    """
    base = strip_base_hash(strip)

    if layer == 0:
        return f"{base}_L0_{frame:06d}"

    mod_hash = strip_modifier_hash(strip)
    if mod_hash is None:
        return f"{base}_L0_{frame:06d}"

    return f"{base}_{mod_hash}_L1_{frame:06d}"


def compute_frame_key(strip, frame, layer=0):
    """Alias for cache_key for external callers."""
    return cache_key(strip, frame, layer)
