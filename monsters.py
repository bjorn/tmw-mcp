"""
Monster name lookup from client-data monsters.xml.
"""

import os
import xml.etree.ElementTree as ET

CLIENT_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'client-data')

_name_cache: dict[int, str] = {}


def load_monster_names():
    """Load all monster names from monsters.xml."""
    if _name_cache:
        return

    monsters_xml = os.path.join(CLIENT_DATA_PATH, 'monsters.xml')
    if os.path.exists(monsters_xml):
        try:
            tree = ET.parse(monsters_xml)
            for monster in tree.findall('.//monster'):
                monster_id = monster.get('id')
                name = monster.get('name')
                if monster_id and name:
                    try:
                        _name_cache[int(monster_id)] = name
                    except ValueError:
                        pass
        except ET.ParseError:
            pass


def monster_name(species_id: int) -> str:
    """Get monster display name by species ID, or 'species:ID' if unknown."""
    if not _name_cache:
        load_monster_names()
    return _name_cache.get(species_id, f'species:{species_id}')
