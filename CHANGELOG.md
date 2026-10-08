# Changelog

All notable changes to `tmw-mcp` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4]

### Added
- `GameClient.emote(emote_id)` (and `packets.build_emote`).
- New `tmw_mcp.commands` module exposing `execute_tool_command`,
  `format_game_state`, `format_inventory`, and `TOOL_SPECS`, a
  JSON-schema catalog of every command that is now the single source
  of truth for the tool surface: the MCP server generates its tools
  from it, and other programs (like tmw-npc) can drive a `GameClient`
  in-process with the same commands. `state` and `inventory` are
  dispatchable commands too.

### Changed
- All tools now return `Game not connected` immediately when there is
  no game connection, instead of queuing the command and timing out
  after five seconds.

### Fixed
- Dependency pinned to `mcp>=1.0,<2`: mcp 2.x removed
  `mcp.server.fastmcp`, so fresh installs failed to start the server.

## [0.1.3]

### Changed
- The project moved from sourcehut to
  [github.com/bjorn/tmw-mcp](https://github.com/bjorn/tmw-mcp). Package
  metadata and the README clone URL point there now.

### Fixed
- Monster names no longer freeze as `species:NNNN`. `monsters.xml`
  downloads on a background thread after login, so beings seen right
  after login used to cache the fallback string permanently. The
  fallback is now resolved live at display time and self-heals once the
  name table finishes loading.
- Closing an NPC dialog now clears the dialog state, so the dashboard
  and MCP state output stop showing the previous NPC's dialog box. A map
  warp still preserves it, matching tmwAthena's behaviour of keeping
  `sd->npc_id` across `pc_setpos`.

## [0.1.2]

### Fixed
- Item and monster name lookups now walk the `<include name="..."/>`
  chain starting at `items.xml` / `monsters.xml`, so every per-item and
  per-monster XML file shipped under `items/<category>/` and
  `monsters/<category>/` lands in the in-memory caches. Previously
  `items.py` walked only the top level of `items/` -- which contains
  only subdirectories -- and resolved zero items, so inventory, floor
  drops, trade dialogs, shop listings, and the dashboard all showed
  `item#NNN` instead of real names. Cache size jumps from 0 to ~1150
  items.

## [0.1.1]

### Changed
- `credentials.json` now identifies the character by `char_name` instead
  of `char_slot`. `full_login` resolves the slot from the server's
  character list at login time. Hand-written credentials files that only
  set `char_slot` need to switch to `char_name`. `TMW_CHAR_SLOT` and the
  `--char` slot integer on `tmw-mcp-cli` are gone; `TMW_CHAR_NAME` and
  `--char NAME` take their place.
- `tmw-mcp-register --char-slot` now defaults to the first free slot
  on the account instead of slot 0, and the command will create a
  character whenever `--char-name` isn't already on the account, so it
  can be used to add a second, third, ... character. Previously the
  create path only ran on a completely empty account.

### Removed
- `gender` field in `credentials.json` and the `TMW_GENDER` env var.
  Both were vestigial: only `tmw-mcp-register` ever used the value
  (to pick the `_M`/`_F` suffix tmwAthena uses to auto-create accounts),
  and nothing read it back at runtime. The `--gender` flag on
  `tmw-mcp-register` itself stays.

### Fixed
- Character-slot cap raised from a stale "0-2" to the actual tmwAthena
  limit of 9 (slots 0 through 8). The wire layer already handled
  arbitrary counts; only the `tmw-mcp-register` CLI help text was wrong.

### Documentation
- README gained a "Using an existing account" section with a complete
  `credentials.json` example for users who already have a TMW account.

## [0.1.0]

Initial release.
