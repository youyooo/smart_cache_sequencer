# SPDX-License-Identifier: GPL-3.0-or-later

"""Singleton storage for cache manager, renderer, server, prefetch, and track manager."""

_manager = None
_renderer = None
_server = None
_prefetch = None
_track_manager = None


def get_singletons():
    return _manager, _renderer, _server, _prefetch, _track_manager


def set_singletons(manager, renderer, server, prefetch, track_manager=None):
    global _manager, _renderer, _server, _prefetch, _track_manager
    _manager = manager
    _renderer = renderer
    _server = server
    _prefetch = prefetch
    _track_manager = track_manager


def clear_singletons():
    global _manager, _renderer, _server, _prefetch, _track_manager
    _manager = None
    _renderer = None
    _server = None
    _prefetch = None
    _track_manager = None
