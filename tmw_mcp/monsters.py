"""Monster name lookup from client-data ``monsters.xml``.

Reads from the ResourceManager (zip overlay populated from the update
host), or, if ``TMW_CLIENT_DATA`` is set, from that directory.
"""

import xml.etree.ElementTree as ET

from .resources import default_manager

_name_cache: dict[int, str] = {}
_loaded = False


def load_monster_names() -> None:
    """Populate the monster name cache if the resource overlay is ready."""
    global _loaded
    if _loaded:
        return
    rm = default_manager()
    if not rm.ready():
        return
    try:
        data = rm.open('monsters.xml')
    except FileNotFoundError:
        return
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return
    for monster in root.iter('monster'):
        monster_id = monster.get('id')
        name = monster.get('name')
        if monster_id and name:
            try:
                _name_cache[int(monster_id)] = name
            except ValueError:
                pass
    if _name_cache:
        _loaded = True


def monster_name(species_id: int) -> str:
    """Get monster display name by species ID, or ``species:ID`` if unknown."""
    load_monster_names()
    return _name_cache.get(species_id, f'species:{species_id}')


def _reset_for_tests() -> None:
    global _loaded
    _name_cache.clear()
    _loaded = False
