"""
Item name lookup from client-data XML files.
"""

import os
import xml.etree.ElementTree as ET

CLIENT_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'client-data')
ITEMS_DIR = os.path.join(CLIENT_DATA_PATH, 'items')

_name_cache: dict[int, str] = {}


def load_item_names():
    """Load all item names from XML files."""
    if _name_cache:
        return

    for root, dirs, files in os.walk(ITEMS_DIR):
        for f in files:
            if not f.endswith('.xml'):
                continue
            path = os.path.join(root, f)
            try:
                tree = ET.parse(path)
                for item in tree.findall('.//item'):
                    item_id = item.get('id')
                    name = item.get('name')
                    if item_id and name:
                        _name_cache[int(item_id)] = name
            except ET.ParseError:
                pass


def item_name(item_id: int) -> str:
    """Get item name by ID, or 'item#ID' if unknown."""
    if not _name_cache:
        load_item_names()
    return _name_cache.get(item_id, f'item#{item_id}')
