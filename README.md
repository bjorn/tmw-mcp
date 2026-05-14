# tmw-mcp

A Python client for [The Mana World](https://www.themanaworld.org/) (a tmwAthena-based MMORPG) exposed as an [MCP](https://modelcontextprotocol.io/) server. Point any MCP-capable AI agent at it and the agent can log in, walk around, fight, trade, and talk to NPCs.

The bot also runs as an interactive CLI for humans, and ships with an optional browser dashboard.

## Install

```bash
pip install tmw-mcp
```

The only third-party dependency is `mcp`. Game data (maps, items, monsters, sprites) is fetched on first login from the server's update host and cached under `~/.cache/tmw-mcp/`; nothing to clone or unpack.

## Register an account

```bash
tmw-mcp-register --user MyAccount --char-name MyCharacter
```

This writes a `credentials.json` (mode 0o600) in the current directory. Treat it like any other secret. You can skip the file entirely and use environment variables instead (see [Configuration](#configuration) below).

## Use it from an MCP client

Every MCP host uses the same JSON shape. Drop this into your host's config file (paths below) and restart it.

```json
{
  "mcpServers": {
    "tmw": {
      "command": "tmw-mcp",
      "env": {
        "TMW_USERNAME": "MyAccount",
        "TMW_PASSWORD": "...",
        "TMW_CHAR_NAME": "MyCharacter"
      }
    }
  }
}
```

Where to put the file:

| Host          | Config path                                                                 |
| ------------- | --------------------------------------------------------------------------- |
| Claude Code   | `.mcp.json` at your project root (or `~/.claude.json` for user-level)       |
| Claude Desktop| `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows) |
| Cursor        | `~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (workspace)               |
| Cline         | `cline_mcp_settings.json` (open via "Cline: MCP Servers" command in VS Code)|

If you'd rather keep credentials in a file than env vars, leave `env` empty and let `tmw-mcp` read `credentials.json` from the cwd it's launched in.

### Claude Code only: reactive wakeups and self-restart

`tmw-mcp` ships two Claude Code-specific extensions that other MCP hosts will silently ignore:

* **Reactive wakeups.** Chat messages, NPC dialog, combat damage, death, map changes, and ferry-bell effects are pushed as `notifications/claude/channel` messages so an idle agent wakes within seconds instead of polling.
* **`tmw_restart` tool.** A thin stdio shim owns the daemon subprocess. Calling `tmw_restart` sends a clean quit to the map server, waits for the account-online entry to clear, and respawns the daemon, all without dropping the MCP session. Great for "fix bug, restart, verify" loops on Claude Code.

Other hosts can still call every other `tmw_*` tool exactly the same way; they just won't be woken up automatically.

## Configuration

| Variable                | Meaning                                                                 |
| ----------------------- | ----------------------------------------------------------------------- |
| `TMW_USERNAME`          | Account name                                                            |
| `TMW_PASSWORD`          | Account password                                                        |
| `TMW_CHAR_NAME`         | Character name (optional if `TMW_CHAR_SLOT` covers it)                  |
| `TMW_CHAR_SLOT`         | Character slot index (0, 1, or 2). Default `0`.                         |
| `TMW_GENDER`            | `M` or `F`. Only used by registration.                                  |
| `TMW_SERVER`            | Login server host. Accepts `host:port`. Default `server.themanaworld.org`.|
| `TMW_PORT`              | Override port only.                                                     |
| `TMW_WORLD`             | World name (blank for default).                                         |
| `TMW_DASHBOARD_PORT`    | Bind the browser dashboard on `127.0.0.1:PORT`. Off by default.         |
| `TMW_CLIENT_DATA`       | Skip the update-host download and read game data from this directory. Useful for development against a checked-out `tmwa-client-data`. |
| `TMW_CREDENTIALS_FILE`  | Absolute path to a `credentials.json`. Useful when your MCP host runs `tmw-mcp` from a cwd that doesn't contain one. |

Precedence: explicit CLI flags > env vars > `credentials.json` > built-in defaults.

## Browser dashboard

Set `TMW_DASHBOARD_PORT=8765` (or pass `--dashboard-port 8765` to any CLI entry) and open `http://127.0.0.1:8765/`. The dashboard shows a live tile map (collision grid, beings, floor items, walk path, hunt zone, aggro discs), HP/SP/EXP bars, hunt status, NPC dialog state, inventory, and a chat tail. Localhost-only, no auth, no extra dependencies (stdlib HTTP + SSE + vanilla JS).

## Where things live

| Kind                  | Path                                                       |
| --------------------- | ---------------------------------------------------------- |
| Downloaded game data  | `${XDG_CACHE_HOME:-~/.cache}/tmw-mcp/`                     |
| Logs and session state| `${XDG_STATE_HOME:-~/.local/state}/tmw-mcp/`               |
| Credentials file      | `./credentials.json` in your cwd (mode 0o600, gitignored)  |

## Console scripts

`pip install` creates four entry points:

| Command             | What it runs                                                  |
| ------------------- | ------------------------------------------------------------- |
| `tmw-mcp`           | Self-restartable MCP stdio entry point. **This is what MCP clients should call.** |
| `tmw-mcp-server`    | The daemon itself, useful for standalone testing.             |
| `tmw-mcp-cli`       | Interactive REPL for humans.                                  |
| `tmw-mcp-register`  | Create an account on the server.                              |

The same modules also work via `python -m tmw_mcp.mcp_shim` etc, so you don't need to install to try them.

## Running from source

```bash
git clone https://github.com/bjorn/tmw-mcp
cd tmw-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover   # from repo root; 66 tests
```

For development against local client-data:

```bash
git clone https://github.com/themanaworld/tmwa-client-data ../client-data
export TMW_CLIENT_DATA=../client-data
.venv/bin/tmw-mcp-cli --credentials credentials.json
```

## How it works

The TMW protocol is binary, little-endian, over TCP. Three servers in sequence: login (default 6901), character (6122), map (5122). After a successful login the server emits a `SMSG_UPDATE_HOST` (0x0063) packet pointing at a small `resources.xml` manifest of ZIP files; the client downloads, hash-verifies (adler32), and presents them as a read-only overlay used for map collision, item names, and monster names.

The active code path is `tmw_mcp.mcp_shim` (the console-script `tmw-mcp`), a thin stdio proxy that owns a daemon subprocess running `tmw_mcp.mcp_server`. The shim survives daemon restarts; the daemon owns the actual game connection. Inside the daemon, a single `GameClient` (`tmw_mcp.game`) walks the login handshake and dispatches packets, while a thin MCP layer exposes every action as a `tmw_*` tool.

Game data layout, packet definitions, and the NPC dialog state machine are documented in `CLAUDE.md`.

## License

MIT. See `LICENSE`.

The Mana World itself is GPL'd content maintained by [its community](https://github.com/themanaworld); this client is independent original work that speaks the same wire protocol.
