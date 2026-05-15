# tmw-mcp

A Python client for [The Mana World](https://www.themanaworld.org/) (a tmwAthena-based MMORPG) exposed as an [MCP](https://modelcontextprotocol.io/) server. Point any MCP-capable AI agent at it and the agent can log in, walk around, fight, trade, and talk to NPCs.

The bot also runs as an interactive CLI for humans, and ships with an optional browser dashboard.

## Please follow the server rules

The Mana World's [game rules](https://policies.themanaworld.org/game-rules/) explicitly forbid AFK botting. Treat this MCP server as an **attended assistant**: stay at the keyboard while your character is logged in, prompt the bot to respond to chat and GMs in a timely way, and disconnect before stepping away. Running it unattended may get your account banned.

## Install

```bash
pip install tmw-mcp
```

The only third-party dependency is `mcp`. Game data (maps, items, monsters, sprites) is fetched on first login from the server's update host and cached under `~/.cache/tmw-mcp/`; nothing to clone or unpack.

## Register an account

```bash
tmw-mcp-register --user MyAccount --char-name MyCharacter
```

This writes a `credentials.json` (`chmod 600`, owner read/write only) in the current directory. Treat it like any other secret. `tmw-mcp` looks for it in its own current working directory by default, so the simplest setup is to run the host from this same directory; otherwise point at the file explicitly with `TMW_CREDENTIALS_FILE` (covered below).

## Use it from an MCP client

### Recommended: Claude Code

Claude Code is the only MCP host today that wakes the agent on `notifications/claude/channel` messages, which is how the bot surfaces chat, NPC dialog, combat damage, death, map changes, and the ferry-bell effect in real time. Without that, the bot only acts when you prompt it, which is fine for short tasks but turns the game into "ask the bot what's happening" instead of "the bot tells you." If you have a choice, use this one.

```bash
claude mcp add tmw -- tmw-mcp      # writes .mcp.json in the current project
                                   # (or add -s user for ~/.claude.json)
```

Equivalent in JSON, drop into `.mcp.json` at your project root by hand:

```json
{
  "mcpServers": {
    "tmw": {
      "command": "tmw-mcp"
    }
  }
}
```

Verify with `claude mcp list`; remove with `claude mcp remove tmw`.

The reactive-wakeup channel is gated behind a Claude Code dev flag. Launch claude with `--dangerously-load-development-channels server:tmw` so the host actually subscribes to `notifications/claude/channel` from this server; without the flag every other tool still works, you just don't get the wake-up on in-game events.

### Other hosts (no wake-ups)

The tool surface is identical across hosts; what you lose without Claude Code is just the wake-up channel, every `tmw` tool itself works the same. Pick whichever fits your workflow.

**Claude Desktop, Cursor, Cline.** Same `mcpServers` shape as Claude Code, just in a different config file:

```json
{ "mcpServers": { "tmw": { "command": "tmw-mcp" } } }
```

| Host          | Config path                                                                 |
| ------------- | --------------------------------------------------------------------------- |
| Claude Desktop| `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows) |
| Cursor        | `~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (workspace)               |
| Cline         | `cline_mcp_settings.json` (open via "Cline: MCP Servers" command in VS Code, or under `~/.cline/data/settings/` for the Cline CLI)|

**VS Code Copilot extension.** Different schema (`servers` not `mcpServers`, explicit `type: "stdio"`). Drop into `.vscode/mcp.json` at the workspace root (or use "MCP: Open User Configuration"):

```json
{ "servers": { "tmw": { "type": "stdio", "command": "tmw-mcp" } } }
```

Switch Copilot Chat from "Ask" to "Agent" mode; MCP tools only show up there.

**Copilot CLI and Codex CLI.** Same `mcp add` shape as Claude Code's:

```bash
copilot mcp add tmw -- tmw-mcp   # writes ~/.copilot/mcp-config.json
codex mcp add tmw -- tmw-mcp     # writes a block into ~/.codex/config.toml
```

Verify with `<host> mcp list`; remove with `<host> mcp remove tmw`. If `tmw-mcp` isn't on `PATH` (e.g. it's only in a specific venv), use the absolute path after the `--`.

### When credentials.json isn't in the host's cwd

Claude Code, Cursor, Cline, and VS Code Copilot launch `tmw-mcp` with the workspace as cwd, so a `credentials.json` at the workspace root is found automatically. The CLI hosts (Claude Code CLI, Copilot CLI, Codex CLI) inherit your terminal's cwd. If neither lines up with where `credentials.json` lives (Claude Desktop on macOS, for instance), pass an absolute path via `TMW_CREDENTIALS_FILE`:

```json
{
  "mcpServers": {
    "tmw": {
      "command": "tmw-mcp",
      "env": {
        "TMW_CREDENTIALS_FILE": "/home/you/credentials.json"
      }
    }
  }
}
```

For the CLI hosts: `claude mcp add -e TMW_CREDENTIALS_FILE=/home/you/credentials.json tmw -- tmw-mcp` (and `--env` instead of `-e` for `copilot` / `codex`).

### Without a credentials file

If you'd rather not have a `credentials.json` on disk at all, pass `TMW_USERNAME`, `TMW_PASSWORD`, and `TMW_CHAR_NAME` in the host's `env` block directly. Mind that those secrets then live inside each host's config file, which is usually plaintext and sometimes inside a workspace dir that's easy to commit by accident.

### Self-restart via `restart`

`tmw-mcp` is fronted by a thin stdio shim that owns the actual game-client daemon as a subprocess. Calling the `restart` tool sends a clean quit to the map server, waits for the server's account-online entry to clear, and respawns the daemon, all without dropping the MCP session. The standard MCP `tools/listChanged` notification fires after the restart so any host re-syncs. It pairs especially well with Claude Code, where the conversation context survives the restart and you get a tight "fix bug, restart, verify" loop in one session.

## Configuration

| Variable                | Meaning                                                                 |
| ----------------------- | ----------------------------------------------------------------------- |
| `TMW_USERNAME`          | Account name                                                            |
| `TMW_PASSWORD`          | Account password                                                        |
| `TMW_CHAR_NAME`         | Character name (optional if `TMW_CHAR_SLOT` covers it)                  |
| `TMW_CHAR_SLOT`         | Character slot index (0, 1, or 2). Default `0`.                         |
| `TMW_GENDER`            | `M` or `F`. Only used by registration.                                  |
| `TMW_SERVER`            | Login server. Accepts `host` or `host:port`. Default `server.themanaworld.org:6901`. |
| `TMW_PORT`              | Override port only. Default `6901`.                                     |
| `TMW_WORLD`             | World name (blank for default).                                         |
| `TMW_DASHBOARD_PORT`    | Bind the browser dashboard on `127.0.0.1:PORT`. Off by default.         |
| `TMW_CLIENT_DATA`       | Skip the update-host download and read game data from this directory instead. Dev-only convenience for iterating on local TMX / item XML edits. |
| `TMW_CREDENTIALS_FILE`  | Absolute path to a `credentials.json`. Useful when your MCP host runs `tmw-mcp` from a cwd that doesn't contain one. |

Precedence: explicit CLI flags > env vars > `credentials.json` > built-in defaults. `TMW_CREDENTIALS_FILE`, if set, picks which `credentials.json` gets read; per-field env vars (`TMW_USERNAME` etc.) still override whatever's in the file.

## Browser dashboard

Set `TMW_DASHBOARD_PORT=8765` (or pass `--dashboard-port 8765` to any CLI entry) and open `http://127.0.0.1:8765/`. The dashboard shows a live tile map (collision grid, beings, floor items, walk path, hunt zone, aggro discs), HP/SP/EXP bars, hunt status, NPC dialog state, inventory, and a chat tail. Localhost-only, no auth, no extra dependencies (stdlib HTTP + SSE + vanilla JS).

## Where things live

| Kind                  | Path                                                       |
| --------------------- | ---------------------------------------------------------- |
| Downloaded game data  | `${XDG_CACHE_HOME:-~/.cache}/tmw-mcp/`                     |
| Logs and session state| `${XDG_STATE_HOME:-~/.local/state}/tmw-mcp/`               |
| Credentials file      | `./credentials.json` in your cwd (`chmod 600`, gitignored) |

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
git clone https://git.sr.ht/~thorbjorn/tmw-mcp
cd tmw-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover   # from repo root; 66 tests
```

Game data (maps, items, monsters) is fetched from the server's update host on first login and cached under `~/.cache/tmw-mcp/`, so a checkout is all you need to run the tests and the CLI. If you want to iterate on local TMX or item XML changes, point `TMW_CLIENT_DATA` at a directory that mirrors the same layout and the downloader is bypassed.

## How it works

The TMW protocol is binary, little-endian, over TCP. Three servers in sequence: login (default 6901), character (6122), map (5122). After a successful login the server emits a `SMSG_UPDATE_HOST` (0x0063) packet pointing at a small `resources.xml` manifest of ZIP files; the client downloads, hash-verifies (adler32), and presents them as a read-only overlay used for map collision, item names, and monster names.

The active code path is `tmw_mcp.mcp_shim` (the console-script `tmw-mcp`), a thin stdio proxy that owns a daemon subprocess running `tmw_mcp.mcp_server`. The shim survives daemon restarts; the daemon owns the actual game connection. Inside the daemon, a single `GameClient` (`tmw_mcp.game`) walks the login handshake and dispatches packets, while a thin MCP layer exposes every action as an MCP tool under the `tmw` namespace.

Game data layout, packet definitions, and the NPC dialog state machine are documented in `CLAUDE.md`.

## License

MIT. See `LICENSE`.

The Mana World itself is GPL'd content maintained by [its community](https://github.com/themanaworld); this client is independent original work that speaks the same wire protocol.
