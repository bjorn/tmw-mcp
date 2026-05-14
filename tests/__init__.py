"""Test-suite scaffolding.

Redirect XDG_STATE_HOME (where the bot writes logs and session state)
into a fresh tempdir for the lifetime of the test process so tests
never touch the real ``~/.local/state/tmw-mcp/`` of the developer
running them. Same for XDG_CACHE_HOME, which the resource downloader
uses.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_test_state = tempfile.mkdtemp(prefix='tmw-test-state-')
_test_cache = tempfile.mkdtemp(prefix='tmw-test-cache-')
os.environ['XDG_STATE_HOME'] = _test_state
os.environ['XDG_CACHE_HOME'] = _test_cache


def _cleanup() -> None:
    shutil.rmtree(_test_state, ignore_errors=True)
    shutil.rmtree(_test_cache, ignore_errors=True)


atexit.register(_cleanup)
