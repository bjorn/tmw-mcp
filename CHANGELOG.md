# Changelog

All notable changes to `tmw-mcp` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
