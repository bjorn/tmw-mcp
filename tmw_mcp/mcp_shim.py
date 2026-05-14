#!/usr/bin/env python3
"""
TMW MCP shim: a tiny proxy that owns the MCP stdio connection to Claude Code
and forwards everything to a daemon subprocess (``mcp_server.py --shim``).

The shim adds one new MCP tool, ``tmw_restart``, which:

1. Sends a JSON-RPC ``quit`` request to the daemon (which then sends CMSG_QUIT
   to the TMW map server and exits cleanly).
2. Escalates to SIGTERM, then SIGKILL, if the daemon does not exit in time.
3. Waits briefly for the TMW server's account-online entry to clear.
4. Respawns the daemon.
5. Emits a ``[Bot] daemon restarted`` channel notification upstream.

Stdlib only. No new pip deps.

The shim is also responsible for:

* Querying the daemon's tool list (``list_tools`` RPC) at startup and after
  each restart, re-registering tools with the MCP server, and pushing a
  ``ToolListChangedNotification`` so Claude Code refreshes its tool view if
  the daemon added or removed tools.
* Forwarding daemon stderr to its own stderr so Claude Code's ``/mcp`` log
  surfaces daemon startup / login errors.
* Forwarding daemon channel notifications upstream as MCP
  ``notifications/claude/channel`` messages.

Usage (via ``.mcp.json``):
    { "mcpServers": { "tmw-bot": { "command": ".venv/bin/python",
        "args": ["client/mcp_shim.py"] } } }
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
from typing import Any

from .paths import mcp_shim_startup_log_path

# Logging goes to stderr; stdout is the MCP protocol pipe.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stderr,
)
log = logging.getLogger('mcp_shim')

CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Escalation timings for tmw_restart (overridable by tests).
QUIT_GRACE_SECONDS = 5.0
SIGTERM_GRACE_SECONDS = 5.0
SIGKILL_RECOVERY_SECONDS = 15.0
CLEAN_QUIT_RECOVERY_SECONDS = 1.5
LIST_TOOLS_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# DaemonProxy: owns one daemon subprocess and a JSON-RPC channel to it
# ---------------------------------------------------------------------------


class DaemonRestartingError(RuntimeError):
    """Raised when a tool call arrives while the daemon is down or restarting.

    Surfaced upward as a clean error so Claude can retry instead of hanging.
    """


class DaemonProxy:
    """Manages the lifecycle of one daemon subprocess and proxies JSON-RPC.

    The instance is intentionally re-creatable: every call to ``start()``
    spawns a fresh process. ``restart()`` chains ``stop_graceful()``,
    ``stop_forced()`` as needed, and a final ``start()``.
    """

    def __init__(
        self,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        notify_upstream: Any = None,
    ):
        # Spawn the daemon via ``python -m tmw_mcp.mcp_server --shim`` so the
        # package layout works whether we're running from a source checkout
        # or an installed PyPI package.
        self.argv = argv or [sys.executable, '-m', 'tmw_mcp.mcp_server', '--shim']
        self.env = env if env is not None else os.environ.copy()
        # ``notify_upstream`` is a callable ``(text: str) -> Awaitable[None]``
        # invoked when the daemon emits a JSON-RPC notification, plus
        # synthetic ``[Bot] ...`` messages from the shim itself.
        self.notify_upstream = notify_upstream

        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._state = 'stopped'  # stopped | starting | running | stopping
        self._tools: list[dict] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the daemon subprocess and load its tool list."""
        if self._state != 'stopped':
            raise RuntimeError(f'cannot start in state {self._state!r}')
        self._state = 'starting'
        log.info('Spawning daemon: %s', ' '.join(self.argv))
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=CLIENT_DIR,
        )
        self._pending = {}
        self._next_id = 1
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name='daemon-stdout-reader',
        )
        self._stderr_task = asyncio.create_task(
            self._forward_stderr(), name='daemon-stderr-forwarder',
        )
        # Pull the tool list. If the daemon dies before responding, the
        # request future is cancelled by ``_read_stdout`` when the pipe
        # closes. Bubble that error up.
        try:
            tools = await asyncio.wait_for(
                self.request('list_tools', {}),
                timeout=LIST_TOOLS_TIMEOUT,
            )
        except Exception as e:
            log.error('Daemon did not respond to list_tools: %s', e)
            await self._kill()
            self._state = 'stopped'
            raise
        if not isinstance(tools, list):
            raise RuntimeError(f'list_tools returned non-list: {tools!r}')
        self._tools = tools
        self._state = 'running'
        log.info('Daemon ready with %d tools', len(self._tools))

    async def stop_graceful(self, timeout: float = QUIT_GRACE_SECONDS) -> bool:
        """Send ``quit`` RPC. Return True if process exited within timeout."""
        if self.proc is None or self.proc.returncode is not None:
            return True
        self._state = 'stopping'
        try:
            # Fire and forget: the daemon exits, which closes the pipe, which
            # in turn unblocks the pending future. We don't need the response.
            req_id = self._alloc_id()
            line = json.dumps({
                'jsonrpc': '2.0', 'id': req_id, 'method': 'quit', 'params': {},
            }) + '\n'
            try:
                self.proc.stdin.write(line.encode('utf-8'))
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, AttributeError):
                pass
        except Exception as e:
            log.warning('Failed to send quit: %s', e)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop_forced(
        self,
        sigterm_timeout: float = SIGTERM_GRACE_SECONDS,
    ) -> str:
        """Escalate to SIGTERM, then SIGKILL. Returns 'sigterm' or 'sigkill'."""
        if self.proc is None or self.proc.returncode is not None:
            return 'already_dead'
        try:
            self.proc.terminate()
        except ProcessLookupError:
            return 'already_dead'
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=sigterm_timeout)
            return 'sigterm'
        except asyncio.TimeoutError:
            pass
        try:
            self.proc.kill()
        except ProcessLookupError:
            return 'sigkill'
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        return 'sigkill'

    async def _kill(self) -> None:
        if self.proc is None:
            return
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DaemonRestartingError('daemon exited'))
        self._pending.clear()

    async def restart(self) -> str:
        """Run the full restart sequence. Returns a short status string."""
        async with self._lock:
            self._state = 'stopping'
            mode = 'graceful'
            clean = await self.stop_graceful()
            recovery = CLEAN_QUIT_RECOVERY_SECONDS
            if not clean:
                escalation = await self.stop_forced()
                mode = escalation
                recovery = SIGKILL_RECOVERY_SECONDS if escalation == 'sigkill' \
                    else CLEAN_QUIT_RECOVERY_SECONDS
            # Make sure background tasks are torn down before respawn.
            await self._kill()
            self._state = 'stopped'

            # Give TMW a moment to release the account-online entry before
            # we try to log in again.
            if recovery > 0:
                await asyncio.sleep(recovery)

            await self.start()
            return mode

    # -- JSON-RPC primitives ------------------------------------------------

    def _alloc_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    async def request(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and await the response."""
        if self._state not in ('starting', 'running'):
            raise DaemonRestartingError('daemon restarting')
        if self.proc is None or self.proc.stdin is None:
            raise DaemonRestartingError('daemon not started')
        loop = asyncio.get_running_loop()
        req_id = self._alloc_id()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        line = json.dumps({
            'jsonrpc': '2.0', 'id': req_id,
            'method': method, 'params': params or {},
        }) + '\n'
        try:
            self.proc.stdin.write(line.encode('utf-8'))
            await self.proc.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise DaemonRestartingError(f'send failed: {e}') from e
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    log.info('Daemon stdout closed')
                    break
                try:
                    obj = json.loads(line.decode('utf-8'))
                except Exception as e:
                    log.warning('Bad JSON from daemon: %s (line=%r)', e, line)
                    continue
                req_id = obj.get('id')
                if req_id is None:
                    # Notification.
                    await self._handle_notification(obj)
                else:
                    fut = self._pending.pop(req_id, None)
                    if fut is None or fut.done():
                        log.warning('Orphan response id=%s', req_id)
                        continue
                    if 'error' in obj:
                        err = obj['error']
                        msg = err.get('message') if isinstance(err, dict) else str(err)
                        fut.set_exception(RuntimeError(msg or 'daemon error'))
                    else:
                        fut.set_result(obj.get('result'))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Reader task crashed')
        finally:
            # Whatever the reason, no more responses can arrive: fail any
            # pending requests so callers don't hang forever.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        DaemonRestartingError('daemon pipe closed')
                    )
            self._pending.clear()

    async def _forward_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                try:
                    sys.stderr.buffer.write(b'[daemon] ' + line)
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Stderr forwarder crashed')

    async def _handle_notification(self, obj: dict) -> None:
        method = obj.get('method')
        params = obj.get('params') or {}
        if method == 'channel' and self.notify_upstream is not None:
            text = params.get('text', '')
            log.info('SHIM forwarding channel notif: %s', text[:80])
            try:
                await self.notify_upstream(text)
            except Exception:
                log.exception('Upstream notify failed')

    # -- accessors ----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def tools(self) -> list[dict]:
        return list(self._tools)


# ---------------------------------------------------------------------------
# MCP server: low-level handlers that proxy to the daemon
# ---------------------------------------------------------------------------


def _build_restart_tool() -> dict:
    return {
        'name': 'tmw_restart',
        'description': (
            'Hard-restart the TMW bot daemon. Sends a clean quit to the '
            'map server so re-login succeeds immediately. Useful when the '
            'bot is wedged or after a code change.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    }


async def run_shim(proxy: DaemonProxy | None = None) -> None:
    """Entry point: spawn the daemon, wire up MCP, run until stdin closes."""
    import mcp.types as types
    from mcp.server.lowlevel.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.shared.message import SessionMessage

    async def _notify_upstream(text: str) -> None:
        # Forwarded via the active server session captured at first request.
        # Wire format matches mcp_server.py's direct FastMCP path byte-for-
        # byte: a JSON-RPC notification with method='notifications/claude/
        # channel' and params={'content': text}. Claude Code recognises this
        # as a wake-up signal when the server advertises the matching
        # experimental capability during initialize.
        nonlocal_session = state.get('session')
        if nonlocal_session is None:
            log.debug('Drop notification (no session yet): %s', text[:80])
            return
        try:
            notif = types.JSONRPCNotification(
                method='notifications/claude/channel',
                params={'content': text},
                jsonrpc='2.0',
            )
            msg = SessionMessage(
                message=types.JSONRPCMessage.model_validate(notif.model_dump())
            )
            await nonlocal_session.send_message(msg)
        except Exception:
            log.exception('Channel notification send failed')

    state: dict[str, Any] = {'session': None}

    if proxy is None:
        proxy = DaemonProxy(notify_upstream=_notify_upstream)
    else:
        proxy.notify_upstream = _notify_upstream

    try:
        await proxy.start()
    except Exception:
        log.exception('Initial daemon start failed')
        raise

    server: Server = Server('tmw-bot', instructions=(
        'TMW game bot for The Mana World MMORPG. Use tmw_state to see the '
        'game world, then use other tools to act. Channel notifications '
        'will alert you to chat messages, NPC dialogs, combat, and map '
        'changes.'
    ))

    def _tools_as_mcp() -> list[types.Tool]:
        out = []
        for t in proxy.tools:
            out.append(types.Tool(
                name=t['name'],
                title=t.get('title'),
                description=t.get('description') or '',
                inputSchema=t.get('inputSchema')
                or {'type': 'object', 'properties': {}},
            ))
        # Synthesize tmw_restart locally; it is not forwarded to the daemon.
        r = _build_restart_tool()
        out.append(types.Tool(
            name=r['name'],
            description=r['description'],
            inputSchema=r['inputSchema'],
        ))
        return out

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _tools_as_mcp()

    # No-op prompts / resources handlers. FastMCP advertises these
    # capabilities by default, and Claude Code's MCP wiring expects them
    # in the initialize response — without them the server's wire shape
    # differs from the direct-FastMCP path (mcp_server.py without --shim)
    # and channel notifications stop being surfaced to the conversation.
    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return []

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return []

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> Any:
        if state.get('session') is None:
            # Capture the session for outbound notifications.
            try:
                state['session'] = server.request_context.session
            except LookupError:
                pass

        if name == 'tmw_restart':
            mode = await proxy.restart()
            await _notify_upstream(f'[Bot] daemon restarted ({mode})')
            # Tool list might have changed across the restart; let the client
            # know so it re-queries.
            try:
                sess = state.get('session')
                if sess is not None:
                    await sess.send_tool_list_changed()
            except Exception:
                log.exception('Tool list changed notify failed')
            return [types.TextContent(type='text', text=f'Daemon restarted ({mode})')]

        try:
            result = await proxy.request('call_tool', {
                'name': name, 'arguments': arguments or {},
            })
        except DaemonRestartingError as e:
            return [types.TextContent(
                type='text', text=f'daemon restarting: {e}',
            )]
        except Exception as e:
            return [types.TextContent(type='text', text=f'Error: {e}')]

        text = result if isinstance(result, str) else json.dumps(result)
        return [types.TextContent(type='text', text=text)]

    async with stdio_server() as (read_stream, write_stream):
        # Mirror mcp_server.py's direct (FastMCP) path: no notification
        # options, only the claude/channel experimental capability. This
        # is what Claude Code expects in order to surface
        # notifications/claude/channel as inline events.
        init_options = server.create_initialization_options(
            experimental_capabilities={'claude/channel': {}},
        )
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            # Tear down the daemon on our way out.
            with contextlib.suppress(Exception):
                await proxy.stop_graceful(timeout=2.0)
            with contextlib.suppress(Exception):
                await proxy._kill()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Console script entry point: run the self-restartable MCP shim."""
    import anyio

    # Mirror mcp_server.py: log startup failures to a file so we can diagnose
    # things even when Claude Code does not surface stderr.
    _fh = logging.FileHandler(
        mcp_shim_startup_log_path(), mode='w',
    )
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    ))
    logging.getLogger().addHandler(_fh)

    log.info('MCP shim starting (pid=%d, cwd=%s)', os.getpid(), os.getcwd())
    try:
        anyio.run(run_shim)
    except Exception:
        log.exception('MCP shim crashed')
        raise


if __name__ == '__main__':
    main()
