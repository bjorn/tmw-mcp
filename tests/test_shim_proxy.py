"""Tests for the self-restartable MCP shim.

These tests do not talk to the real TMW MCP daemon. Instead each test launches
a tiny Python script as a "fake daemon" that speaks the same line-delimited
JSON-RPC dialect on stdin/stdout. That lets us exercise the proxy's
roundtrip, notification forwarding, restart sequence, and SIGTERM
escalation without any game server.

Run with: .venv/bin/python -m unittest client.tests.test_shim_proxy
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap
import unittest

CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)

import mcp_shim
from mcp_shim import DaemonProxy, DaemonRestartingError


# A polite fake daemon: replies "ok:<name>" to call_tool, exits cleanly on
# quit, emits a channel notification when its echo() RPC is called.
FAKE_DAEMON_OK = textwrap.dedent('''
    import json, sys, time
    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    # Eager startup notification so tests can prove notifications work
    # without any RPC traffic.
    emit({"jsonrpc": "2.0", "method": "channel",
          "params": {"text": "[Bot] fake online"}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "list_tools":
            tools = [
                {"name": "tmw_say", "description": "say something",
                 "inputSchema": {"type": "object",
                                 "properties": {"message": {"type": "string"}},
                                 "required": ["message"]}},
                {"name": "tmw_state", "description": "show state",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]
            emit({"jsonrpc": "2.0", "id": rid, "result": tools})
        elif method == "call_tool":
            name = (req.get("params") or {}).get("name")
            args = (req.get("params") or {}).get("arguments") or {}
            emit({"jsonrpc": "2.0", "id": rid,
                  "result": f"ok:{name}:{args}"})
            # Also push a side-channel notification so the test can verify
            # both directions of traffic are interleaved correctly.
            emit({"jsonrpc": "2.0", "method": "channel",
                  "params": {"text": f"[Note] called {name}"}})
        elif method == "quit":
            emit({"jsonrpc": "2.0", "id": rid, "result": "bye"})
            time.sleep(0.05)
            sys.exit(0)
        else:
            emit({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "unknown"}})
''').strip()


# A rude fake daemon: it answers list_tools but ignores quit. Used to test
# SIGTERM escalation.
FAKE_DAEMON_STUBBORN = textwrap.dedent('''
    import json, signal, sys, time
    # Refuse to die from SIGTERM; only SIGKILL works on us.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "list_tools":
            emit({"jsonrpc": "2.0", "id": rid, "result": []})
        elif method == "quit":
            # Silently refuse and keep running.
            pass
        elif method == "call_tool":
            emit({"jsonrpc": "2.0", "id": rid, "result": "ok"})
        else:
            emit({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "?"}})
''').strip()


# A fake that never responds to list_tools, so start() raises. Used to test
# the daemon-not-ready error path.
FAKE_DAEMON_SILENT = textwrap.dedent('''
    import sys, time
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        # Drop everything on the floor.
''').strip()


def _argv_for(script: str) -> list[str]:
    return [sys.executable, '-c', script]


class _Capture:
    """Collects upstream channel notifications via the proxy hook."""

    def __init__(self):
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


class DaemonProxyRoundtripTest(unittest.IsolatedAsyncioTestCase):

    async def test_list_tools_and_call_tool(self):
        cap = _Capture()
        proxy = DaemonProxy(
            argv=_argv_for(FAKE_DAEMON_OK),
            notify_upstream=cap,
        )
        await proxy.start()
        try:
            self.assertEqual({t['name'] for t in proxy.tools},
                             {'tmw_say', 'tmw_state'})
            result = await proxy.request(
                'call_tool',
                {'name': 'tmw_say', 'arguments': {'message': 'hi'}},
            )
            self.assertIn('ok:tmw_say', result)
            # The fake also sends a notification after call_tool. Give the
            # reader task a moment to surface it.
            await asyncio.sleep(0.1)
            self.assertIn('[Bot] fake online', cap.lines)
            self.assertTrue(any('[Note] called tmw_say' in line
                                for line in cap.lines))
        finally:
            await proxy.stop_graceful()
            await proxy._kill()


class DaemonProxyRestartTest(unittest.IsolatedAsyncioTestCase):

    async def test_quit_and_respawn_works(self):
        cap = _Capture()
        proxy = DaemonProxy(
            argv=_argv_for(FAKE_DAEMON_OK),
            notify_upstream=cap,
        )
        # Speed the recovery window so tests stay quick.
        old_clean = mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS
        mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS = 0.05
        try:
            await proxy.start()
            first_pid = proxy.proc.pid
            result = await proxy.restart()
            self.assertEqual(result, 'graceful')
            self.assertNotEqual(proxy.proc.pid, first_pid)
            # After restart, call_tool still works.
            r = await proxy.request(
                'call_tool',
                {'name': 'tmw_state', 'arguments': {}},
            )
            self.assertIn('ok:tmw_state', r)
        finally:
            mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS = old_clean
            await proxy.stop_graceful()
            await proxy._kill()

    async def test_sigterm_escalation_when_quit_ignored(self):
        # Stubborn fake ignores quit AND SIGTERM, so we should escalate all
        # the way to SIGKILL. Use tiny timeouts for test speed.
        old_q, old_t, old_kr, old_cr = (
            mcp_shim.QUIT_GRACE_SECONDS,
            mcp_shim.SIGTERM_GRACE_SECONDS,
            mcp_shim.SIGKILL_RECOVERY_SECONDS,
            mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS,
        )
        mcp_shim.QUIT_GRACE_SECONDS = 0.5
        mcp_shim.SIGTERM_GRACE_SECONDS = 0.5
        mcp_shim.SIGKILL_RECOVERY_SECONDS = 0.05
        mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS = 0.05
        try:
            cap = _Capture()
            proxy = DaemonProxy(
                argv=_argv_for(FAKE_DAEMON_STUBBORN),
                notify_upstream=cap,
            )
            await proxy.start()
            first_pid = proxy.proc.pid
            mode = await proxy.restart()
            self.assertEqual(mode, 'sigkill')
            self.assertNotEqual(proxy.proc.pid, first_pid)
        finally:
            mcp_shim.QUIT_GRACE_SECONDS = old_q
            mcp_shim.SIGTERM_GRACE_SECONDS = old_t
            mcp_shim.SIGKILL_RECOVERY_SECONDS = old_kr
            mcp_shim.CLEAN_QUIT_RECOVERY_SECONDS = old_cr
            await proxy.stop_graceful()
            await proxy._kill()


class DaemonProxyNotReadyTest(unittest.IsolatedAsyncioTestCase):

    async def test_request_during_restart_fails_fast(self):
        cap = _Capture()
        proxy = DaemonProxy(
            argv=_argv_for(FAKE_DAEMON_OK),
            notify_upstream=cap,
        )
        await proxy.start()
        try:
            # Manually transition into the 'stopping' state and confirm that
            # request() raises immediately rather than hanging.
            proxy._state = 'stopping'
            with self.assertRaises(DaemonRestartingError):
                await proxy.request('call_tool', {'name': 'x', 'arguments': {}})
        finally:
            proxy._state = 'running'
            await proxy.stop_graceful()
            await proxy._kill()

    async def test_start_failure_when_daemon_silent(self):
        # A daemon that never answers list_tools must not block the shim
        # forever: list_tools timeout fires, start() raises.
        old_timeout = mcp_shim.LIST_TOOLS_TIMEOUT
        mcp_shim.LIST_TOOLS_TIMEOUT = 0.3
        try:
            proxy = DaemonProxy(
                argv=_argv_for(FAKE_DAEMON_SILENT),
                notify_upstream=_Capture(),
            )
            with self.assertRaises(Exception):
                await proxy.start()
            self.assertEqual(proxy.state, 'stopped')
        finally:
            mcp_shim.LIST_TOOLS_TIMEOUT = old_timeout


if __name__ == '__main__':
    unittest.main()
