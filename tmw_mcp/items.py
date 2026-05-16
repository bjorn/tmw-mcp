"""
Item name lookup from the resource overlay.

Reads ``items.xml`` (and any ``items/*.xml`` shards) from the
:class:`~tmw_mcp.resources.ResourceManager` zip overlay populated
from the update host on first login. ``TMW_CLIENT_DATA`` points the
overlay at a filesystem directory instead for local development. The
parser also pulls a few combat-relevant attributes (attack range,
type) so the client can reason about equipped weapons / ammo without
waiting for server status updates.
"""

import logging
import xml.etree.ElementTree as ET

from .resources import default_manager

log = logging.getLogger(__name__)

_name_cache: dict[int, str] = {}
# id -> {'name': str, 'attack_range': int | None, 'type': str | None}
_meta_cache: dict[int, dict] = {}
_loaded = False


def load_item_names():
    """Populate the item caches from the resource overlay if available.

    Safe to call repeatedly; subsequent calls are a no-op once at least
    one item has been registered. If the resource manager is not ready
    yet (no update host fetched, no override dir), this returns quietly
    and the next lookup will simply fall back to the synthetic name.

    The TMW client data ships per-item XML leaves under
    ``items/<category>/itemNNNN_X.xml`` reached via a chain of
    ``<include name="..."/>`` directives starting at ``items.xml``. We
    follow that chain here so every reachable item lands in the cache.
    """
    global _loaded
    if _loaded:
        return
    rm = default_manager()
    if not rm.ready():
        return

    _load_with_includes('items.xml')

    if _name_cache:
        _loaded = True


def _load_with_includes(path: str) -> None:
    """Resolve ``<include name="..."/>`` directives recursively from ``path``.

    Each visited XML file is also passed through :func:`_parse_xml` to
    pick up any inline ``<item>`` entries (leaves), so include hubs that
    happen to also carry items still work. Missing referenced files are
    logged at debug level and skipped -- the manifest is not perfectly
    consistent across overlays.
    """
    rm = default_manager()
    visited: set[str] = set()
    stack: list[str] = [path]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            data = rm.open(current)
        except FileNotFoundError:
            log.debug('include not found: %s', current)
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            log.debug('parse error in %s', current)
            continue
        # Parse any inline <item> entries on this node.
        _parse_root(root)
        # Queue any <include> children.
        for inc in root.iter('include'):
            name = inc.get('name')
            if name and name not in visited:
                stack.append(name)


def _parse_xml(data: bytes) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return
    _parse_root(root)


def _parse_root(root: ET.Element) -> None:
    for item in root.iter('item'):
        item_id = item.get('id')
        name = item.get('name')
        if not (item_id and name):
            continue
        try:
            iid = int(item_id)
        except ValueError:
            continue
        _name_cache[iid] = name
        ar = item.get('attack-range')
        try:
            ar_int = int(ar) if ar is not None else None
        except ValueError:
            ar_int = None
        _meta_cache[iid] = {
            'name': name,
            'attack_range': ar_int,
            'type': item.get('type') or None,
        }


def item_name(item_id: int) -> str:
    """Get item name by ID, or ``item#ID`` if unknown."""
    load_item_names()
    return _name_cache.get(item_id, f'item#{item_id}')


def item_attack_range(item_id: int) -> int | None:
    """Return the ``attack-range`` attribute for a weapon, or None."""
    load_item_names()
    info = _meta_cache.get(item_id)
    return info['attack_range'] if info else None


def item_type(item_id: int) -> str | None:
    """Return the ``type`` attribute (e.g. ``equip-2hand``, ``equip-ammo``)."""
    load_item_names()
    info = _meta_cache.get(item_id)
    return info['type'] if info else None


def is_ammo(item_id: int) -> bool:
    """True for ammo items (Arrow, Snowball, Sling Bullet, ...)."""
    return item_type(item_id) == 'equip-ammo'


def _reset_for_tests() -> None:
    """Drop the module-level caches. Used by tests; not part of the API."""
    global _loaded
    _name_cache.clear()
    _meta_cache.clear()
    _loaded = False
