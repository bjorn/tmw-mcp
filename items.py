"""
Item name lookup from client-data XML files.

Also extracts a handful of combat-relevant attributes (attack range,
type) so the client can reason about equipped weapons / ammo without
waiting for server status updates.
"""

import os
import xml.etree.ElementTree as ET

CLIENT_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'client-data')

_name_cache: dict[int, str] = {}
# id -> {'name': str, 'attack_range': int | None, 'type': str | None}
_meta_cache: dict[int, dict] = {}


def load_item_names():
    """Load all item names from XML files."""
    if _name_cache:
        return

    # Try items.xml in client-data root (tmwa-client-data layout)
    items_xml = os.path.join(CLIENT_DATA_PATH, 'items.xml')
    if os.path.exists(items_xml):
        _load_xml(items_xml)

    # Also try items/ subdirectory (alternative layout)
    items_dir = os.path.join(CLIENT_DATA_PATH, 'items')
    if os.path.isdir(items_dir):
        for root, dirs, files in os.walk(items_dir):
            for f in files:
                if f.endswith('.xml'):
                    _load_xml(os.path.join(root, f))


def _load_xml(path: str):
    """Parse a single XML file for item names and a few combat fields."""
    try:
        tree = ET.parse(path)
        for item in tree.findall('.//item'):
            item_id = item.get('id')
            name = item.get('name')
            if not (item_id and name):
                continue
            try:
                iid = int(item_id)
            except ValueError:
                continue
            _name_cache[iid] = name
            # Parse optional attack-range attribute (present on weapons).
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
    except ET.ParseError:
        pass


def item_name(item_id: int) -> str:
    """Get item name by ID, or 'item#ID' if unknown."""
    if not _name_cache:
        load_item_names()
    return _name_cache.get(item_id, f'item#{item_id}')


def item_attack_range(item_id: int) -> int | None:
    """Return the attack-range attribute for a weapon, or None if unknown."""
    if not _name_cache:
        load_item_names()
    info = _meta_cache.get(item_id)
    return info['attack_range'] if info else None


def item_type(item_id: int) -> str | None:
    """Return the item 'type' attribute (e.g. 'equip-2hand', 'equip-ammo')."""
    if not _name_cache:
        load_item_names()
    info = _meta_cache.get(item_id)
    return info['type'] if info else None


def is_ammo(item_id: int) -> bool:
    """Return True for ammo items (Arrow, Snowball, Sling Bullet, ...)."""
    return item_type(item_id) == 'equip-ammo'
