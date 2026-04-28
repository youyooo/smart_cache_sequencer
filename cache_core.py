# SPDX-License-Identifier: GPL-3.0-or-later

"""Singleton storage for cache manager, renderer, server, and prefetch."""

_manager = None
_renderer = None
_server = None
_prefetch = None


def get_singletons():
    return _manager, _renderer, _server, _prefetch


def set_singletons(manager, renderer, server, prefetch):
    global _manager, _renderer, _server, _prefetch
    _manager = manager
    _renderer = renderer
    _server = server
    _prefetch = prefetch


def clear_singletons():
    global _manager, _renderer, _server, _prefetch
    _manager = None
    _renderer = None
    _server = None
    _prefetch = None
