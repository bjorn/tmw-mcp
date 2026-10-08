# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python client for [The Mana World](https://www.themanaworld.org/) (a tmwAthena-based MMORPG) that exposes the game as an MCP server. The intended consumer is an AI agent (Claude Code), but a human-driven CLI (`tmw_mcp/main.py`) and a legacy file-driven bot (`tmw_mcp/bot.py`) share the same game-client core.

All runtime code lives in the `tmw_mcp/` package. The only third-party dependency is `mcp`. Everything else is stdlib.

## Commands

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install -e .

# Run the whole suite (from repo root; loads tests/__init__.py which
# redirects XDG_STATE_HOME and XDG_CACHE_HOME to tempdirs so tests
# don't pollute your real ~/.local/state/tmw-mcp).
.venv/bin/python -m unittest discover

# Run a single test (still from repo root)
.venv/bin/python -m unittest tests.test_dashboard
.venv/bin/python -m unittest tests.test_shim_proxy
.venv/bin/python -m unittest tests.test_follow
.venv/bin/python -m unittest tests.test_ranged_combat
.venv/bin/python -m unittest tests.test_resources
.venv/bin/python -m unittest tests.test_credentials

# Console scripts (created by ``pip install -e .``)
tmw-mcp                                  # stdio MCP entry point (with self-restart shim)
tmw-mcp-server [--shim] [--dashboard-port N]   # daemon directly (advanced)
tmw-mcp-cli --credentials credentials.json     # interactive human REPL
tmw-mcp-register --user USERNAME --char-name CHARNAME

# Module form (works without installing)
.venv/bin/python -m tmw_mcp.mcp_shim
.venv/bin/python -m tmw_mcp.mcp_server [--shim]
.venv/bin/python -m tmw_mcp.main --credentials credentials.json
.venv/bin/python -m tmw_mcp.register --user USERNAME --char-name CHARNAME

# Regenerate PACKET_SIZES from upstream tmwa (dev tool)
python3 scripts/extract_packets.py    # prints a dict; paste over PACKET_SIZES in tmw_mcp/packets.py
```

No linter or formatter is wired up. Tests insert the repo root onto `sys.path` themselves so they run from any cwd.

## Architecture

### Three layers, one game loop

1. **Wire layer** (`tmw_mcp/net.py`, `tmw_mcp/packets.py`). `Connection` does packet framing using `PACKET_SIZES`, a table generated from the upstream tmwAthena server's protocol definition (see "Protocol source of truth" below for how to regenerate). Variable-size packets read a u16 length at offset 2; the special `0x8000` "hold notify" is a fixed 4-byte frame with no payload. `packets.py` owns every builder and parser plus `encode_pos1` / `decode_pos1` / `decode_pos2` for the packed coordinate format.
2. **Game layer** (`tmw_mcp/game.py`). A single `GameClient` walks the login -> char -> map server handshake, then dispatches incoming packets to handlers that mutate state (`player`, `beings`, `floor_items`, `inventory`, `npc_dialog`). Player actions (`walk`, `attack`, `npc_*`, `pickup`, ...) are methods on this class. A* pathing and hunt logic live here too. The map server silently drops walks beyond ~17 tiles, so `walk_path` / `walk` clamp to 10 and reissue.
3. **Frontends**, each owning its own loop and reusing `GameClient`:
   - `tmw_mcp/main.py` - interactive REPL for humans.
   - `tmw_mcp/bot.py` - legacy file-driven bot (`cmd.txt` in, `bot_log.txt` / `bot_state.txt` out). Still works, not the primary path.
   - `tmw_mcp/mcp_server.py` - the active path. Runs the game loop in a background thread and bridges events to MCP via an `asyncio.Queue` -> forwarder task -> `notifications/claude/channel` messages on the main async loop. Its MCP tools are generated from `TOOL_SPECS` in `tmw_mcp/commands.py`, which is the authoritative catalog of every command: add a tool by adding a spec there plus a dispatch branch in `execute_tool_command`.

Internal imports use Python relative form (`from .game import ...`). External callers use `from tmw_mcp.X import ...` or the console scripts.

### Self-restart shim

Consumers point their MCP config at `tmw-mcp` (the console script for `tmw_mcp/mcp_shim.py`), not at `mcp_server` directly. The shim is a thin stdio proxy that spawns `python -m tmw_mcp.mcp_server --shim` as a child and forwards JSON-RPC both ways. The `tmw_restart` MCP tool sends a graceful quit (`CMSG_QUIT` 0x018a) to the map server, waits for the child to exit, and respawns it; the MCP session and the host's conversation survive the restart, so the agent can fix a bug, restart, and verify in one session. Anything that must persist across restart belongs in `bot_state.txt` or in the daemon's own startup logic, not in shim memory. Tests in `tests/test_shim_proxy.py` exercise the proxy against a fake daemon.

### Dashboard

`tmw_mcp/dashboard.py` is a stdlib-only HTTP + SSE server started by `DashboardServer.start(port, state_provider)`. It binds localhost only, has no auth, runs on a daemon thread, and uses a bounded SSE queue so a slow browser cannot stall the game loop. Static assets live in `tmw_mcp/dashboard_static/` (shipped as package data). Off unless `--dashboard-port` is passed or `TMW_DASHBOARD_PORT` is set.

### Reactive wakeup

When the game emits something interesting (chat, whisper, NPC dialog turn, combat damage, death, map change, ferry bell effect), the MCP server pushes a `notifications/claude/channel` message so an idle agent wakes within seconds. This is a Claude Code-specific extension; other MCP hosts will silently ignore it. The list of "interesting" events is in `mcp_server.py`; expand it there.

### Data lookups

`items.py`, `monsters.py`, and `maps.py` look up names and parse TMX maps through `tmw_mcp.resources.default_manager()`. The `ResourceManager` is populated on first login: the server sends `SMSG_UPDATE_HOST` (0x0063), `game.GameClient._kick_resource_download` fires a background thread that fetches `resources.xml` from that host, downloads any zips not already in `${XDG_CACHE_HOME:-~/.cache}/tmw-mcp/<host-fingerprint>/`, verifies adler32, and exposes the union as a flat read-only overlay. Setting `TMW_CLIENT_DATA=/path/to/dir` short-circuits the whole thing and reads files from that directory instead (used in tests and for local TMX iteration).

## Conventions worth knowing

- **Account creation.** tmwAthena auto-creates an account when the username sent to the login server ends in `_M` or `_F`. `register.py` relies on this; there is no separate registration packet.
- **Chat double-naming.** Modern tmwAthena (>= 0x100408) prepends the player name server-side on public chat. Do not also prepend it client-side or messages will read "Claudius: Claudius: hello".
- **Client version.** `MIN_CLIENT_VERSION = 6`. The Mana reference client sends 8; this client matches that.
- **Walk clamp.** The map server silently rejects walk requests beyond ~17 tiles. `game.py` clamps to 10 and reissues when the destination is further; do not remove the clamp.
- **NPC dialog state machine.** Click NPC (`tmw_npc`) -> receive lines -> advance with `tmw_npc_next` -> when choices appear, select with `tmw_npc_choose` (1-based) -> close with `tmw_npc_close`. State lives in `npc_dialog`, `npc_waiting_next`, `npc_waiting_choice` on `GameClient`.
- **Credentials.** `credentials.json` is `chmod 600` (owner read/write only) and gitignored. Never log it or commit it.
- **Persistent logs.** `chat_history.log` and `npc_history.log` live under `${XDG_STATE_HOME:-~/.local/state}/tmw-mcp/` (see `tmw_mcp/paths.py`). They are append-only and grep-friendly; the bot relies on this to recover context across restarts. Do not rotate or truncate them as part of routine changes.

## Releasing to PyPI

```bash
# One-time: install build tooling in the working venv.
.venv/bin/pip install -e ".[dev]"

# Bump tmw_mcp/__init__.py:__version__ and pyproject.toml:[project].version
# to match. Commit. Tag with the version (e.g. ``git tag v0.1.0``).

# Build sdist and wheel into dist/. MANIFEST.in pulls tests/ and
# scripts/ into the sdist; dashboard_static/ is package-data on the
# wheel.
rm -rf dist build *.egg-info
.venv/bin/python -m build

# Static metadata check; long_description / classifiers / urls.
.venv/bin/twine check dist/*

# Optional: smoke-test the wheel in a throwaway venv before uploading.
F=$(mktemp -d) && python3 -m venv "$F/.venv" && \
    "$F/.venv/bin/pip" install dist/tmw_mcp-*.whl && \
    "$F/.venv/bin/tmw-mcp-cli" --help && rm -rf "$F"

# Upload to TestPyPI first.
.venv/bin/twine upload -r testpypi dist/*

# When the install from TestPyPI works end-to-end, push to real PyPI.
.venv/bin/twine upload dist/*

# Finally push the tag.
git push origin v0.1.0
```

`twine` reads ``~/.pypirc`` or ``TWINE_USERNAME``/``TWINE_PASSWORD`` env vars (use an API token starting with ``pypi-`` as the password, username ``__token__``).

## Protocol source of truth

The TMW protocol is little-endian binary over three TCP servers: login (default 6901), char (6122), map (5122). Each packet starts with a u16 id, and `PACKET_SIZES` in `tmw_mcp/packets.py` is the table that drives `net.Connection`'s framing.

That table is regenerated from `tools/protocol.py` in the upstream [tmwAthena server](https://github.com/themanaworld/tmwa) by running `scripts/extract_packets.py`. The script reads `protocol.py` from a relative path that assumes a sibling tmwa checkout next to this repo; clone tmwa wherever you like and either symlink it or adjust `proto_path` in the script. The output is a Python dict you paste over `PACKET_SIZES`. It's a dev-only convenience and never runs at install or runtime.
