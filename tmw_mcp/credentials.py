"""Credentials loader: env vars + optional JSON file.

The same set of knobs that ``credentials.json`` has accepts an
environment variable so MCP clients (Claude Desktop, Cursor, Cline,
Claude Code) can pass them through their built-in secrets UI without
ever putting a JSON file on disk.

Precedence (highest first):

1. Keyword arguments to :func:`load_credentials` (typically the
   caller's parsed CLI flags).
2. Environment variables.
3. ``credentials.json`` at the given path (if it exists and parses).
4. Built-in defaults: ``server.themanaworld.org:6901``, slot 0,
   gender ``M``, world ``''``.

Env vars (all optional):

* ``TMW_USERNAME``
* ``TMW_PASSWORD``
* ``TMW_SERVER`` (host or ``host:port``)
* ``TMW_PORT``
* ``TMW_CHAR_SLOT`` (0, 1, 2)
* ``TMW_CHAR_NAME``
* ``TMW_GENDER`` (``M`` or ``F``)
* ``TMW_WORLD`` (optional world tag, blank for default)
* ``TMW_CREDENTIALS_FILE`` (absolute path to a credentials.json; wins
  over any path the caller passes, and lets an MCP host config point
  at a file outside the user's cwd without resorting to a wrapper
  script)
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_FILE = 'credentials.json'

_DEFAULTS: dict = {
    'server': 'server.themanaworld.org',
    'port': 6901,
    'char_slot': 0,
    'gender': 'M',
    'world': '',
}

# (creds_key, env_var, cast)
_ENV_MAP: list[tuple[str, str, type]] = [
    ('username', 'TMW_USERNAME', str),
    ('password', 'TMW_PASSWORD', str),
    # ``TMW_SERVER`` is special: it may include a port suffix.
    ('port', 'TMW_PORT', int),
    ('char_slot', 'TMW_CHAR_SLOT', int),
    ('char_name', 'TMW_CHAR_NAME', str),
    ('gender', 'TMW_GENDER', str),
    ('world', 'TMW_WORLD', str),
]


def load_credentials(path: str | None = None, **overrides) -> dict:
    """Return a credentials dict assembled from env, file, and overrides.

    ``path`` is a JSON file path; if None or the file doesn't exist,
    that layer is skipped silently. ``TMW_CREDENTIALS_FILE`` in the
    environment overrides ``path``. ``overrides`` keys (e.g. ``user``,
    ``password``, ``server``) win over everything else; pass ``None``
    or ``''`` to mean "no override at this layer".
    """
    out = dict(_DEFAULTS)

    file_env = os.environ.get('TMW_CREDENTIALS_FILE')
    if file_env:
        path = file_env

    if path and os.path.exists(path):
        try:
            with open(path) as f:
                out.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            log.warning('Could not read %s: %s', path, e)

    server_env = os.environ.get('TMW_SERVER')
    if server_env:
        host, _, port_part = server_env.partition(':')
        if host:
            out['server'] = host
        if port_part:
            try:
                out['port'] = int(port_part)
            except ValueError:
                log.warning('Bad port in TMW_SERVER=%r', server_env)

    for key, var, cast in _ENV_MAP:
        val = os.environ.get(var)
        if val is None or val == '':
            continue
        try:
            out[key] = cast(val)
        except ValueError:
            log.warning('Bad value for %s=%r', var, val)

    for k, v in overrides.items():
        if v is None or v == '':
            continue
        # Map common CLI flag names (``user``) onto the canonical key.
        if k == 'user':
            k = 'username'
        out[k] = v

    return out


def password_for_safety_check(path: str | None = None) -> str | None:
    """Return the configured password, if any, for outbound-message
    leak detection. Used by :func:`tmw_mcp.bot.is_safe_message`.

    Honours ``TMW_CREDENTIALS_FILE`` via :func:`load_credentials`.
    """
    if path is None:
        path = DEFAULT_FILE if os.path.exists(DEFAULT_FILE) else None
    creds = load_credentials(path)
    return creds.get('password')
