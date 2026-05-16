"""Monster name lookup from the resource overlay.

Reads ``monsters.xml`` from the
:class:`~tmw_mcp.resources.ResourceManager` zip overlay populated
from the update host on first login (or from the ``TMW_CLIENT_DATA``
filesystem override).
"""

import logging
import xml.etree.ElementTree as ET

from .resources import default_manager

log = logging.getLogger(__name__)

_name_cache: dict[int, str] = {}
_loaded = False


def load_monster_names() -> None:
    """Populate the monster name cache if the resource overlay is ready.

    ``monsters.xml`` may pull in additional ``<monster>`` entries via
    ``<include name="..."/>`` directives (e.g. ``mods/monsters.xml``);
    we follow them recursively the same way :mod:`tmw_mcp.items` does.
    """
    global _loaded
    if _loaded:
        return
    rm = default_manager()
    if not rm.ready():
        return

    visited: set[str] = set()
    stack: list[str] = ['monsters.xml']
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            data = rm.open(current)
        except FileNotFoundError:
            log.debug('monster include not found: %s', current)
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            log.debug('parse error in %s', current)
            continue
        for monster in root.iter('monster'):
            monster_id = monster.get('id')
            name = monster.get('name')
            if monster_id and name:
                try:
                    _name_cache[int(monster_id)] = name
                except ValueError:
                    pass
        for inc in root.iter('include'):
            ref = inc.get('name')
            if ref and ref not in visited:
                stack.append(ref)

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
