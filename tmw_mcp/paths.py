"""Filesystem paths for logs, state, and persistent history.

Follows the XDG Base Directory Specification (well-defined on Linux,
respected by sensible tooling on macOS/Windows): persistent logs and
session state live under ``${XDG_STATE_HOME:-~/.local/state}/tmw-mcp/``.

The cache directory (downloaded zips) is owned by :mod:`tmw_mcp.resources`
and follows ``${XDG_CACHE_HOME:-~/.cache}/tmw-mcp/``.

Tests can redirect everything by setting ``XDG_STATE_HOME`` to a
tempdir before importing the modules that read these paths.
"""

from __future__ import annotations

import os

APP = 'tmw-mcp'


def state_dir() -> str:
    """Directory for persistent logs/state. Created on first access."""
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    path = os.path.join(base, APP)
    os.makedirs(path, exist_ok=True)
    return path


def bot_log_path() -> str:
    """Current session event log (overwritten at MCP server startup)."""
    return os.path.join(state_dir(), 'bot_log.txt')


def bot_state_path() -> str:
    """Snapshot of game state, rewritten every tick (legacy bot.py mode)."""
    return os.path.join(state_dir(), 'bot_state.txt')


def chat_log_path() -> str:
    """Append-only persistent record of every chat/whisper line."""
    return os.path.join(state_dir(), 'chat_history.log')


def npc_log_path() -> str:
    """Append-only persistent record of every NPC dialog line."""
    return os.path.join(state_dir(), 'npc_history.log')


def mcp_startup_log_path() -> str:
    """Daemon-side startup log (written even when stderr isn't visible)."""
    return os.path.join(state_dir(), 'mcp_startup.log')


def mcp_shim_startup_log_path() -> str:
    """Shim-side startup log (written even when stderr isn't visible)."""
    return os.path.join(state_dir(), 'mcp_shim_startup.log')
