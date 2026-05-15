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
    """
    global _loaded
    if _loaded:
        return
    rm = default_manager()
    if not rm.ready():
        return

    # Flat layout: a single items.xml at the overlay root.
    try:
        _parse_xml(rm.open('items.xml'))
    except FileNotFoundError:
        pass

    # Alternative layout: items/*.xml. Walk one level only.
    for name in rm.list_files('items'):
        if not name.endswith('.xml'):
            continue
        try:
            _parse_xml(rm.open(f'items/{name}'))
        except FileNotFoundError:
            continue

    if _name_cache:
        _loaded = True


def _parse_xml(data: bytes) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return
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
