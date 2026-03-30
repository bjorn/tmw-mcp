"""
TMX map collision parser.

Reads TMX (Tiled) map files from the client-data repository and extracts
the collision layer to determine walkable tiles.
"""

import heapq
import math
import os
import xml.etree.ElementTree as ET

CLIENT_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'client-data')
MAPS_PATH = os.path.join(CLIENT_DATA_PATH, 'maps')


class CollisionMap:
    """Parsed collision data for a single map."""

    def __init__(self, name: str, width: int, height: int, data: list[list[bool]]):
        self.name = name
        self.width = width
        self.height = height
        # data[y][x] = True means walkable
        self.data = data

    def is_walkable(self, x: int, y: int) -> bool:
        if 0 <= x < self.width and 0 <= y < self.height:
            return self.data[y][x]
        return False

    def render_around(self, px: int, py: int, radius: int = 10,
                      beings: dict = None, items: dict = None) -> str:
        """Render a text view of the map around position (px, py).

        Legend: . = walkable, # = wall, @ = player, ? = being, $ = item
        """
        lines = []
        for y in range(py - radius, py + radius + 1):
            row = []
            for x in range(px - radius, px + radius + 1):
                if x == px and y == py:
                    row.append('@')
                elif beings and any(b.x == x and b.y == y for b in beings.values()):
                    # Find the being at this position
                    for b in beings.values():
                        if b.x == x and b.y == y:
                            if b.max_hp > 0 and b.name:
                                row.append('M')  # Monster with name
                            elif b.max_hp > 0:
                                row.append('m')  # Monster unnamed
                            else:
                                row.append('N')  # NPC
                            break
                elif items and any(i.x == x and i.y == y for i in items.values()):
                    row.append('$')
                elif not self.is_walkable(x, y):
                    row.append('#')
                else:
                    row.append('.')
            lines.append(''.join(row))
        return '\n'.join(lines)


    def find_path(self, sx: int, sy: int, gx: int, gy: int,
                  max_nodes: int = 50000) -> list[tuple[int, int]] | None:
        """A* pathfinding from (sx,sy) to (gx,gy). Returns list of (x,y) or None."""
        if not self.is_walkable(gx, gy) or not self.is_walkable(sx, sy):
            return None
        if sx == gx and sy == gy:
            return [(sx, sy)]

        SQRT2 = math.sqrt(2)
        # Chebyshev-style heuristic for 8-dir movement
        def h(x, y):
            dx, dy = abs(x - gx), abs(y - gy)
            return max(dx, dy) + (SQRT2 - 1) * min(dx, dy)

        # (f, counter, x, y)
        counter = 0
        open_heap = [(h(sx, sy), counter, sx, sy)]
        g_score = {(sx, sy): 0.0}
        came_from = {}
        visited = 0

        DIRS = [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]

        while open_heap:
            f, _, cx, cy = heapq.heappop(open_heap)
            if cx == gx and cy == gy:
                # Reconstruct path
                path = [(gx, gy)]
                while (cx, cy) in came_from:
                    cx, cy = came_from[(cx, cy)]
                    path.append((cx, cy))
                path.reverse()
                return path

            cur_g = g_score.get((cx, cy))
            if cur_g is None or f - h(cx, cy) > cur_g + 1e-6:
                continue  # stale entry

            visited += 1
            if visited > max_nodes:
                return None

            for ddx, ddy in DIRS:
                nx, ny = cx + ddx, cy + ddy
                if not self.is_walkable(nx, ny):
                    continue
                # Prevent corner-cutting for diagonals
                if ddx != 0 and ddy != 0:
                    if not self.is_walkable(cx + ddx, cy) or not self.is_walkable(cx, cy + ddy):
                        continue
                cost = SQRT2 if (ddx != 0 and ddy != 0) else 1.0
                ng = cur_g + cost
                if ng < g_score.get((nx, ny), float('inf')):
                    g_score[(nx, ny)] = ng
                    counter += 1
                    heapq.heappush(open_heap, (ng + h(nx, ny), counter, nx, ny))
                    came_from[(nx, ny)] = (cx, cy)

        return None


# Cache of loaded collision maps
_cache: dict[str, CollisionMap] = {}


def load_collision(map_name: str) -> CollisionMap | None:
    """Load collision data for a map.

    map_name should be like '029-1' (without .tmx extension).
    """
    if map_name in _cache:
        return _cache[map_name]

    tmx_path = os.path.join(MAPS_PATH, map_name + '.tmx')
    if not os.path.exists(tmx_path):
        return None

    tree = ET.parse(tmx_path)
    root = tree.getroot()

    map_width = int(root.get('width', 0))
    map_height = int(root.get('height', 0))

    # Find the Collision layer
    collision_data = None
    for layer in root.findall('layer'):
        if layer.get('name', '').lower() == 'collision':
            data_elem = layer.find('data')
            if data_elem is not None and data_elem.get('encoding') == 'csv':
                csv_text = data_elem.text.strip()
                rows = []
                values = [int(v.strip()) for v in csv_text.split(',') if v.strip()]
                # Convert flat list to 2D grid
                for y in range(map_height):
                    row = []
                    for x in range(map_width):
                        idx = y * map_width + x
                        if idx < len(values):
                            # GID 0 = no tile = walkable
                            # GID 1 = collision tile 1 (walkable marker in some maps)
                            # GID 2 = collision tile 2 (wall)
                            # Treat 0 and 1 as walkable, 2 as wall
                            row.append(values[idx] != 2)
                        else:
                            row.append(False)
                    rows.append(row)
                collision_data = rows
            break

    if collision_data is None:
        return None

    cmap = CollisionMap(map_name, map_width, map_height, collision_data)
    _cache[map_name] = cmap
    return cmap
