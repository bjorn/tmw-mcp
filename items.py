"""
Item name lookup from client-data XML files.
"""

import os
import xml.etree.ElementTree as ET

CLIENT_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'client-data')

_name_cache: dict[int, str] = {}


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
    """Parse a single XML file for item names."""
    try:
        tree = ET.parse(path)
        for item in tree.findall('.//item'):
            item_id = item.get('id')
            name = item.get('name')
            if item_id and name:
                try:
                    _name_cache[int(item_id)] = name
                except ValueError:
                    pass
    except ET.ParseError:
        pass


def item_name(item_id: int) -> str:
    """Get item name by ID, or 'item#ID' if unknown."""
    if not _name_cache:
        load_item_names()
    return _name_cache.get(item_id, f'item#{item_id}')
