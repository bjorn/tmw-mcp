# tmw-mcp

A Python client for [The Mana World](https://www.themanaworld.org/) MMORPG, exposed as an MCP server so an AI assistant can play the game natively.

## Quick Start

```bash
pip install mcp

# Register a game account (only needed once)
python3 register.py --user USERNAME --char-name CHARNAME

# Play via Claude Code with MCP
claude --dangerously-load-development-channels server:tmw-bot
```

Configure the MCP server in `.mcp.json`:

```json
{ "mcpServers": { "tmw-bot": { "command": "python3", "args": ["mcp_server.py"] } } }
```

## How It Works

The MCP server exposes game actions as tools (`tmw_walk`, `tmw_say`, `tmw_attack`, etc.) and pushes game events (chat, combat, NPC dialog) as channel notifications that wake the AI from idle.

## Project Structure

```
mcp_server.py    - MCP server entry point
bot.py           - Legacy standalone bot (file-based I/O)
game.py          - Game client: login, state tracking, actions
packets.py       - Binary protocol: packet builders and parsers
net.py           - TCP connection with packet framing
maps.py          - TMX collision map parser (minimap)
items.py         - Item name lookup from client-data XML
monsters.py      - Monster name lookup
main.py          - Interactive CLI client (for humans)
register.py      - Account registration
observer.py      - Observer/spectator client
```

## Client Data

`maps.py`, `items.py`, and `monsters.py` read game assets (maps, item definitions, monster definitions) from a `client-data` directory expected at `../client-data` relative to this repository.

Clone [tmwa-client-data](https://github.com/themanaworld/tmwa-client-data) next to this repository:

```bash
git clone https://github.com/themanaworld/tmwa-client-data.git ../client-data
```

## License

The custom client code is original work. It connects to The Mana World, an open-source MMORPG.
