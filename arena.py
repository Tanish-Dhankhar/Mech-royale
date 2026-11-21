"""Static arena geometry. All movement points are mech feet, in world meters."""

from array import array
from heapq import heappop, heappush
from math import floor, hypot, inf, log, radians, sqrt, tan
from pathlib import Path
from time import perf_counter
import json
import sys


_EPS = 1e-7
_Z_EPS = 0.015  # Observations quantize positions to centimeters.
_SUPPORT_TOL = 0.35
_DIAGONAL = sqrt(0.5)
_DIRECTIONS = ((1, 0), (0, 1), (-1, 0), (0, -1),
               (1, 1), (-1, 1), (-1, -1), (1, -1))


def _expired(deadline):
    return deadline is not None and perf_counter() >= deadline


def _lerp(a, b, t):
    return a + (b - a) * t


def _segment_point(a, b, t):
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))


def _interval(p, dp, lo, hi):
    """Times on the unit segment for which lo <= p + t*dp <= hi."""
    if lo > hi:
        return None
    if abs(dp) < _EPS:
        return (0.0, 1.0) if lo <= p <= hi else None
    a, b = (lo - p) / dp, (hi - p) / dp
    a, b = max(0.0, min(a, b)), min(1.0, max(a, b))
    return (a, b) if a <= b else None


def _intersect(a, b):
    if a is not None and b is not None:
        lo, hi = max(a[0], b[0]), min(a[1], b[1])
        if lo <= hi:
            return lo, hi
    return None


def _disk_interval(x, y, dx, dy, radius):
    return _quadratic_interval(dx * dx + dy * dy, x * dx + y * dy, x * x + y * y - radius * radius)


def _quadratic_interval(aa, bb, cc):
    """Clip the inside of a disk or ellipsoid to the unit segment."""
    if aa < _EPS * _EPS:
        return (0.0, 1.0) if cc <= 0 else None
    det = bb * bb - aa * cc
    if det < 0:
        return None
    root = sqrt(det)
    lo, hi = max(0.0, (-bb - root) / aa), min(1.0, (-bb + root) / aa)
    return (lo, hi) if lo <= hi else None


def _grid_segments(x, y, dx, dy, step, lo=0.0, hi=1.0):
    """Yield square cells in ray order, with entry/exit times; origin is zero."""
    i, j = floor((x + lo * dx) / step), floor((y + lo * dy) / step)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    tx = ((i + (sx > 0)) * step - x) / dx if dx else inf
    ty = ((j + (sy > 0)) * step - y) / dy if dy else inf
    dtx, dty = abs(step / dx) if dx else inf, abs(step / dy) if dy else inf
    while lo < hi:
        end = min(hi, tx, ty)
        if end > lo:
            yield i, j, lo, end
        if end >= hi:
            break
        if tx <= end:
            i, tx = i + sx, tx + dtx
        if ty <= end:
            j, ty = j + sy, ty + dty
        lo = max(lo, end)


class _Index:
    def __init__(self, step=24.0):
        self.step = step
        self.cells = {}

    def add(self, obj, x0, y0, x1, y1):
        step = self.step
        for j in range(floor((y0 - _EPS) / step), floor((y1 + _EPS) / step) + 1):
            for i in range(floor((x0 - _EPS) / step), floor((x1 + _EPS) / step) + 1):
                self.cells.setdefault((i, j), []).append(obj)

    def box(self, x0, y0, x1, y1):
        step, cells = self.step, self.cells
        i, j, ni, nj = floor(x0 / step), floor(y0 / step), floor(x1 / step), floor(y1 / step)
        if i == ni and j == nj:
            return cells.get((i, j), ())
        return dict.fromkeys(obj for iy in range(j, nj + 1) for ix in range(i, ni + 1)
                             for obj in cells.get((ix, iy), ()))

    def ray(self, a, b):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        if x1 - x0 <= self.step and y1 - y0 <= self.step:
            return self.box(x0, y0, x1, y1)
        return dict.fromkeys(obj for i, j, _, _ in _grid_segments(
            a[0], a[1], b[0] - a[0], b[1] - a[1], self.step)
                             for obj in self.cells.get((i, j), ()))


def _ring_points(flat):
    points = (tuple(tuple(p) for p in flat) if flat and isinstance(flat[0], (tuple, list))
              else tuple(zip(flat[::2], flat[1::2])))
    return points[:-1] if points and points[0] == points[-1] else points


def _point_poly(x, y, edges):
    inside = False
    for ax, ay, ex, ey, _ in edges:
        if (ay > y) != (ay + ey > y) and x < ax + ex * (y - ay) / ey:
            inside = not inside
    return inside


class _Prism:
    __slots__ = ('base', 'top', 'bounds', 'rings', 'edges')

    def __init__(self, data):
        self.base, self.top = data['base'], data['top']
        rings = [_ring_points(flat) for flat in (data['ring'], *data.get('holes', ()))]
        xs, ys = zip(*rings[0])
        self.bounds = min(xs), min(ys), max(xs), max(ys)
        self.rings = tuple(tuple((ax, ay, bx - ax, by - ay, (bx - ax)**2 + (by - ay)**2)
                                 for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1])
                                 if (ax, ay) != (bx, by)) for ring in rings)
        self.edges = tuple(edge for ring in self.rings for edge in ring)

    def contains(self, x, y):
        x0, y0, x1, y1 = self.bounds
        return (x0 <= x <= x1 and y0 <= y <= y1 and _point_poly(x, y, self.rings[0])
                and not any(_point_poly(x, y, ring) for ring in self.rings[1:]))

    def touches(self, x, y, r):
        x0, y0, x1, y1 = self.bounds
        if x < x0 - r or x > x1 + r or y < y0 - r or y > y1 + r:
            return False
        if self.contains(x, y):
            return True
        rr = max(0.0, r - _EPS)**2
        for ax, ay, ex, ey, ee in self.edges:
            ox, oy = x - ax, y - ay
            t = min(1.0, max(0.0, (ox * ex + oy * ey) / ee))
            if (ox - ex * t)**2 + (oy - ey * t)**2 < rr:
                return True
        return False

    def ray_hit(self, a, b):
        span = _interval(a[2], b[2] - a[2], self.base, self.top)
        if span is None:
            return False
        # Ray endpoints may be located on a surface; an interior contact is solid.
        lo, hi = max(span[0], _EPS), min(span[1], 1 - _EPS)
        if lo > hi:
            return False
        x, y = _lerp(a[0], b[0], lo), _lerp(a[1], b[1], lo)
        bx, by = _lerp(a[0], b[0], hi), _lerp(a[1], b[1], hi)
        x0, y0, x1, y1 = self.bounds
        if max(x, bx) < x0 or min(x, bx) > x1 or max(y, by) < y0 or min(y, by) > y1:
            return False
        if self.contains(x, y) or self.contains(bx, by):
            return True
        dx, dy = bx - x, by - y
        for ax, ay, ex, ey, ee in self.edges:
            ox, oy = ax - x, ay - y
            det = dx * ey - dy * ex
            if abs(det) > _EPS * _EPS:
                if 0 <= (ox * ey - oy * ex) / det <= 1 and 0 <= (ox * dy - oy * dx) / det <= 1:
                    return True
            elif abs(ox * ey - oy * ex) <= _EPS * sqrt(ee):
                if _interval(-ox * ex - oy * ey, dx * ex + dy * ey, 0.0, ee) is not None:
                    return True
        return False

    def spans(self, a, b, radius):
        """Exact XY intervals where the swept disk overlaps this solid footprint."""
        x, y = a[0], a[1]
        dx, dy = b[0] - x, b[1] - y
        x0, x1 = min(x, b[0]), max(x, b[0])
        y0, y1 = min(y, b[1]), max(y, b[1])
        px0, py0, px1, py1 = self.bounds
        r = max(0.0, radius - _EPS)
        if x1 + r < px0 or x0 - r > px1 or y1 + r < py0 or y0 - r > py1:
            return ()
        bands = []
        for ax, ay, ex, ey, ee in self.edges:
            bx, by = ax + ex, ay + ey
            if (max(ax, bx) < x0 - r or min(ax, bx) > x1 + r or
                    max(ay, by) < y0 - r or min(ay, by) > y1 + r):
                continue
            ox, oy = x - ax, y - ay
            width = r * sqrt(ee)
            strip = _intersect(_interval(ox * ex + oy * ey, dx * ex + dy * ey, 0.0, ee),
                               _interval(ox * ey - oy * ex, dx * ey - dy * ex, -width, width))
            if strip is not None:
                bands.append(strip)
            if r > 0:
                cap = _disk_interval(ox, oy, dx, dy, r)
                if cap is not None:
                    bands.append(cap)
                cap = _disk_interval(x - bx, y - by, dx, dy, r)
                if cap is not None:
                    bands.append(cap)
        if not bands:
            return ((0.0, 1.0),) if self.contains(x, y) else ()
        spans = []
        end = 0.0
        for lo, hi in sorted(bands):
            if lo <= end:
                if spans and hi > end:
                    spans[-1] = (spans[-1][0], hi)
                elif not spans:
                    spans.append((0.0, hi))
            elif self.contains(x + (end + lo) * dx / 2, y + (end + lo) * dy / 2):
                if spans:
                    spans[-1] = (spans[-1][0], hi)
                else:
                    spans.append((0.0, hi))
            else:
                spans.append((lo, hi))
            end = max(end, hi)
        if end < 1 and self.contains(x + (end + 1) * dx / 2, y + (end + 1) * dy / 2):
            spans[-1] = (spans[-1][0], 1.0)
        return spans


class _Terrain:
    _CHUNK = 8  # Terrain cells, not world meters.

    def __init__(self, desc, path):
        self.nx, self.ny = desc['nx'], desc['ny']
        self.ox, self.oy, self.step = desc['minX'], desc['minY'], desc['step']
        self.scale = desc.get('scale', 0.01)
        self.samples = heights = array('h', path.read_bytes())
        if sys.byteorder != 'little':
            heights.byteswap()
        if len(heights) != self.nx * self.ny:
            raise ValueError('terrain dimensions do not match the height data')
        self.high = max(heights) * self.scale
        self.chunk_size = self._CHUNK * self.step
        self.chunks = chunks = {}
        nx, ny, scale = self.nx, self.ny, self.scale
        for j0 in range(0, ny, self._CHUNK):
            for i0 in range(0, nx, self._CHUNK):
                i1, j1 = min(i0 + self._CHUNK, nx - 1), min(j0 + self._CHUNK, ny - 1)
                low, high, gx, gy = inf, -inf, 0, 0
                for j in range(j0, j1 + 1):
                    k = j * nx + i0
                    row = heights[k:k + i1 - i0 + 1]
                    low, high = min(low, min(row)), max(high, max(row))
                    gx = max(gx, max((abs(a - b) for a, b in zip(row, row[1:])), default=0))
                    if j < j1:
                        gy = max(gy, max(abs(a - b) for a, b in zip(row, heights[k + nx:k + nx + len(row)])))
                chunks[i0 // self._CHUNK, j0 // self._CHUNK] = (
                    low * scale, high * scale, gx * scale / self.step, gy * scale / self.step)
        self.last_chunk = ((nx - 1) // self._CHUNK, (ny - 1) // self._CHUNK)

    def ground(self, x, y):
        nx, ny, samples = self.nx, self.ny, self.samples
        gx = min(nx - 1, max(0.0, (x - self.ox) / self.step))
        gy = min(ny - 1, max(0.0, (y - self.oy) / self.step))
        i, j = int(gx), int(gy)
        ti, tj, k = gx - i, gy - j, j * nx + i
        di, dj = int(i + 1 < nx), nx if j + 1 < ny else 0
        return _lerp(_lerp(samples[k], samples[k + di], ti),
                     _lerp(samples[k + dj], samples[k + dj + di], ti), tj) * self.scale

    def _chunk(self, i, j):
        return self.chunks[min(self.last_chunk[0], max(0, i)), min(self.last_chunk[1], max(0, j))]

    def _pieces(self, x, y, dx, dy, lo, hi):
        """Bilinear height in a grid cell is a quadratic along the ray."""
        nx, ny, samples, step, scale = self.nx, self.ny, self.samples, self.step, self.scale
        x, y, dx, dy = x / step, y / step, dx / step, dy / step
        for i, j, t0, t1 in _grid_segments(x, y, dx, dy, 1.0, lo, hi):
            i, j = min(nx - 1, max(0, i)), min(ny - 1, max(0, j))
            fx, fy = min(nx - 1, max(0.0, x + dx * t0)) - i, min(ny - 1, max(0.0, y + dy * t0)) - j
            ux = min(nx - 1, max(0.0, x + dx * t1)) - i - fx
            uy = min(ny - 1, max(0.0, y + dy * t1)) - j - fy
            k, di, dj = j * nx + i, int(i + 1 < nx), nx if j + 1 < ny else 0
            base = samples[k]
            gx, gy = samples[k + di] - base, samples[k + dj] - base
            cross = samples[k + dj + di] - base - gx - gy
            yield (t0, t1, (base + gx * fx + gy * fy + cross * fx * fy) * scale,
                   (gx * ux + gy * uy + cross * (fx * uy + fy * ux)) * scale, cross * ux * uy * scale)

    def clear(self, a, b):
        if min(a[2], b[2]) >= self.high - _Z_EPS:
            return True
        x, y, dx, dy = a[0] - self.ox, a[1] - self.oy, b[0] - a[0], b[1] - a[1]
        z, dz = a[2], b[2] - a[2]
        for i, j, lo, hi in _grid_segments(x, y, dx, dy, self.chunk_size):
            if min(z + dz * lo, z + dz * hi) >= self._chunk(i, j)[1] - _Z_EPS:
                continue
            for t0, t1, c, p, q in self._pieces(x, y, dx, dy, lo, hi):
                c -= z + dz * t0
                p -= dz * (t1 - t0)
                if max(c, c + p + q) > _Z_EPS:
                    return False
                if q < 0 and 0 < (peak := -p / (2 * q)) < 1 and c + peak * (p + peak * q) > _Z_EPS:
                    return False
        return True

    def uphill(self, a, b, grade):
        x, y, dx, dy = a[0] - self.ox, a[1] - self.oy, b[0] - a[0], b[1] - a[1]
        cap = hypot(dx, dy) * grade
        for i, j, lo, hi in _grid_segments(x, y, dx, dy, self.chunk_size):
            _, _, gx, gy = self._chunk(i, j)
            if abs(dx) * gx + abs(dy) * gy <= cap:
                continue
            for t0, t1, _, p, q in self._pieces(x, y, dx, dy, lo, hi):
                if max(p, p + 2 * q) > cap * (t1 - t0) + _EPS:
                    return False
        return True

    def profile(self, a, b, lo, hi):
        """Exact ground extrema and the steepest uphill grade on part of a path."""
        x, y, dx, dy = a[0] - self.ox, a[1] - self.oy, b[0] - a[0], b[1] - a[1]
        size = self.chunk_size
        i, j = floor((x + dx * lo) / size), floor((y + dy * lo) / size)
        if i == floor((x + dx * hi) / size) and j == floor((y + dy * hi) / size):
            low, high, _, _ = self._chunk(i, j)
            if low == high:
                return low, high, 0.0
        low, high, rise = inf, -inf, 0.0
        length = hypot(dx, dy)
        for t0, t1, c, p, q in self._pieces(x, y, dx, dy, lo, hi):
            low, high = min(low, c, c + p + q), max(high, c, c + p + q)
            if q and 0 < (peak := -p / (2 * q)) < 1:
                z = c + peak * (p + peak * q)
                low, high = min(low, z), max(high, z)
            if length and t1 - t0 > _EPS:
                rise = max(rise, max(p, p + 2 * q) / ((t1 - t0) * length))
        return low, high, rise


class _Grid:
    def __init__(self, arena, spacing):
        self.spacing = spacing
        self.minx, self.miny, maxx, maxy = arena.bounds
        self.nx = floor((maxx - self.minx) / spacing) + 1
        self.ny = floor((maxy - self.miny) / spacing) + 1
        self.elevations = [arena.ground(self.minx + i * spacing, self.miny + j * spacing)
                           for j in range(self.ny) for i in range(self.nx)]
        self.points = {}
        self.edges = {}

    def indices(self, point):
        return round((point[0] - self.minx) / self.spacing), round((point[1] - self.miny) / self.spacing)

    def position(self, node):
        j, i = divmod(node, self.nx)
        return (self.minx + i * self.spacing, self.miny + j * self.spacing, self.elevations[node])


class Arena:
    def __init__(self, map_dir: str, mech: dict):
        directory = Path(map_dir)
        if (directory / 'v2' / 'world.json').is_file():
            directory /= 'v2'
        with (directory / 'world.json').open(encoding='utf-8') as f:
            world = json.load(f)
        terrain = world['terrain']
        self._terrain = _Terrain(terrain, directory / terrain.get('file', 'terrain.i16'))
        bounds = world.get('bounds')
        if bounds:
            self.bounds = tuple(bounds[k] for k in ('minX', 'minY', 'maxX', 'maxY'))
        else:
            self.bounds = (terrain['minX'], terrain['minY'],
                           terrain['minX'] + (terrain['nx'] - 1) * terrain['step'],
                           terrain['minY'] + (terrain['ny'] - 1) * terrain['step'])
        self._mech = mech
        self._prisms, self._trees = _Index(), _Index()
        for data in world.get('prisms', ()):
            prism = _Prism(data)
            self._prisms.add(prism, *prism.bounds)
        trees = world.get('trees', {})
        fields, data = trees.get('fields', ()), trees.get('data', ())
        for i in range(0, len(data), len(fields) or 1):
            tree = dict(zip(fields, data[i:i + len(fields)]))
            x, y, z = tree['x'], tree['y'], tree['z']
            cx, cy = tree.get('crownX', x), tree.get('crownY', y)
            height, base = tree['height'], tree['crownBase']
            crown, trunk = tree.get('crownR', 0), tree.get('trunkR', 0)
            item = (x, y, z, z + base, trunk, cx, cy, z + (height + base) / 2, crown, (height - base) / 2)
            self._trees.add(item, min(x - trunk, cx - crown), min(y - trunk, cy - crown),
                            max(x + trunk, cx + crown), max(y + trunk, cy + crown))
        rules = world.get('rules', {})
        density, threshold = rules.get('canopyDensity', 0.35), rules.get('canopyBlockT', 0.25)
        self._canopy_budget = -log(min(1.0, threshold)) / density if density > 0 and threshold > 0 else inf
        # Viewer maxStep/maxSlopeDeg are not simulation parameters.
        minx, miny, maxx, maxy = self.bounds
        spacing = min(5.0, max(1.0, min(maxx - minx, maxy - miny) / 16))
        self._grids = (_Grid(self, spacing), _Grid(self, spacing / 2))
        self._grid_stats = None

    def ground(self, x, y) -> float:
        return self._terrain.ground(x, y)

    def _inside(self, x, y, margin=0.0):
        minx, miny, maxx, maxy = self.bounds
        return minx + margin <= x <= maxx - margin and miny + margin <= y <= maxy - margin

    def clear(self, a, b) -> bool:
        return (self._inside(a[0], a[1]) and self._inside(b[0], b[1])
                and not any(prism.ray_hit(a, b) for prism in self._prisms.ray(a, b))
                and self._terrain.clear(a, b))

    def can_see(self, a, b) -> bool:
        if not self.clear(a, b):
            return False
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        length, budget = hypot(dx, dy, dz), self._canopy_budget
        for x, y, z, top, trunk, cx, cy, cz, crown, half in self._trees.ray(a, b):
            if trunk > 0 and _intersect(_interval(a[2], dz, z, top),
                                         _disk_interval(a[0] - x, a[1] - y, dx, dy, trunk)) is not None:
                return False
            if budget != inf and crown > 0 and half > 0:
                px, py, pz = (a[0] - cx) / crown, (a[1] - cy) / crown, (a[2] - cz) / half
                qx, qy, qz = dx / crown, dy / crown, dz / half
                chord = _quadratic_interval(qx * qx + qy * qy + qz * qz, px * qx + py * qy + pz * qz,
                                            px * px + py * py + pz * pz - 1)
                if chord is not None:
                    budget -= length * (chord[1] - chord[0])
                    if budget < 0:
                        return False
        return True

    def _body(self, x0, y0, x1, y1, radius):
        return self._prisms.box(min(x0, x1) - radius, min(y0, y1) - radius,
                                max(x0, x1) + radius, max(y0, y1) + radius)

    @staticmethod
    def _fits_prisms(x, y, z, radius, height, prisms):
        return not any(p.base < z + height - _EPS and p.top > z + _EPS and p.touches(x, y, radius)
                       for p in prisms)

    def fits(self, p, radius, height) -> bool:
        x, y, z = p
        return (self._inside(x, y, radius) and z + _Z_EPS >= self.ground(x, y)
                and self._fits_prisms(x, y, z, radius, height, self._body(x, y, x, y, radius)))

    def _floor(self, point, radius, height, step):
        """Fitting ground or a legal low step; never promote a node to a rooftop."""
        x, y, z = point
        if not self._inside(x, y, radius):
            return None
        prisms = self._body(x, y, x, y, radius)
        if self._fits_prisms(x, y, z, radius, height, prisms):
            return point
        for top in sorted({p.top for p in prisms if 0 < p.top - z <= step + _EPS and p.touches(x, y, radius)}):
            if self._fits_prisms(x, y, top, radius, height, prisms):
                return x, y, top
        return None

    def _supported(self, point, radius, height):
        x, y, z = point
        if not self._inside(x, y, radius):
            return None
        ground = self.ground(x, y)
        prisms = self._body(x, y, x, y, radius)
        if not prisms:
            return (x, y, ground) if abs(z - ground) <= _SUPPORT_TOL else None
        candidates = [ground] if abs(z - ground) <= _SUPPORT_TOL else []
        candidates.extend(p.top for p in prisms if p.top >= ground and abs(z - p.top) <= _SUPPORT_TOL
                          and p.touches(x, y, radius))
        for support in sorted(candidates, key=lambda v: abs(v - z)):
            if self._fits_prisms(x, y, support, radius, height, prisms):
                return x, y, support
        return None

    def _walk(self, a, b, radius, height, step, grade, *, deadline=None):
        if a[:2] == b[:2]:
            return abs(a[2] - b[2]) <= _Z_EPS
        # A ray through a polygon hole is free; an edge of the disk clipping the
        # hole wall is not. Gather the exact occupied spans before checking steps.
        prisms = self._body(a[0], a[1], b[0], b[1], radius)
        if not prisms:
            return self._terrain.uphill(a, b, grade)
        spans = []
        for prism in prisms:
            if deadline is not None and perf_counter() >= deadline:
                return False
            spans.extend((lo, hi, prism) for lo, hi in prism.spans(a, b, radius) if hi > lo)
        if not spans:
            return (abs(a[2] - self.ground(a[0], a[1])) <= _Z_EPS
                    and abs(b[2] - self.ground(b[0], b[1])) <= _Z_EPS and self._terrain.uphill(a, b, grade))
        cuts = sorted({0.0, 1.0, *(t for lo, hi, _ in spans for t in (lo, hi))})
        previous = a[2]
        for lo, hi in zip(cuts, cuts[1:]):
            if deadline is not None and perf_counter() >= deadline:
                return False
            mid = (lo + hi) / 2
            active = sorted((p for left, right, p in spans if left <= mid <= right), key=lambda p: p.top)
            low, high, uphill = self._terrain.profile(a, b, lo, hi)
            x, y, _ = _segment_point(a, b, lo)
            floor_start = self.ground(x, y)
            surface = -inf
            for p in active:
                # At a legal step the rim meets the wall before the center does.
                if p.top > max(low, surface) + _EPS and p.base < max(high, surface) + height - _EPS:
                    if p.top > previous + step + _EPS:
                        return False
                    surface = p.top
            # A step into a ceiling is not walkable even if the ledge alone is.
            if any(p.top > max(low, surface) + _EPS and
                   p.base < max(high, surface) + height - _EPS for p in active):
                return False
            new_z = max(floor_start, surface)
            if abs(previous - new_z) > step + _EPS:
                return False  # A large drop needs the separately checked descent.
            if surface < high - _Z_EPS and uphill > grade + _EPS:
                return False
            x, y, _ = _segment_point(a, b, hi)
            previous = max(self.ground(x, y), surface)
        return abs(previous - b[2]) <= _Z_EPS

    def walkable(self, a, b, radius, height, step_up, max_slope_deg) -> bool:
        a, b = self._supported(a, radius, height), self._supported(b, radius, height)
        return (a is not None and b is not None and
                self._walk(a, b, radius, height, step_up, tan(radians(max_slope_deg))))

    def _stat(self, stats, key, default):
        return stats.get(key, self._mech.get(key, default))

    def _motion(self, stats):
        return (self._stat(stats, 'radius', 2.5), self._stat(stats, 'height', 9.0),
                self._stat(stats, 'step_up', 1.2), tan(radians(self._stat(stats, 'max_slope_deg', 50))))

    def neighbors(self, point, distance, radius, height) -> list[tuple]:
        return list(self._neighbors(point, distance, radius, height, self._mech.get('step_up', 1.2)))

    def _neighbors(self, point, distance, radius, height, step):
        for dx, dy in _DIRECTIONS:
            d = distance * _DIAGONAL if dx and dy else distance
            x, y = point[0] + dx * d, point[1] + dy * d
            p = self._floor((x, y, self.ground(x, y)), radius, height, step)
            if p is not None:
                yield p

    def _grid_point(self, grid, node, motion):
        if node not in grid.points:
            grid.points[node] = self._floor(grid.position(node), *motion[:3])
        return grid.points[node]

    def _grid_edges(self, grid, node, motion):
        if node not in grid.edges:
            a = grid.points[node]
            nx, ny, spacing = grid.nx, grid.ny, grid.spacing
            j, i = divmod(node, nx)
            edges = []
            for dx, dy in _DIRECTIONS:
                x, y = i + dx, j + dy
                if 0 <= x < nx and 0 <= y < ny:
                    adj = y * nx + x
                    b = self._grid_point(grid, adj, motion)
                    if b is not None and self._walk(a, b, *motion):
                        length = spacing / _DIAGONAL if dx and dy else spacing
                        edges.append((adj, length + 0.4 * max(0, b[2] - a[2])))
            grid.edges[node] = edges
        return grid.edges[node]

    def _links(self, grid, point, motion, *, toward_point, deadline):
        i, j = grid.indices(point)
        links = {}
        for ring in (1, 2):
            for y in range(max(0, j - ring), min(grid.ny, j + ring + 1)):
                for x in range(max(0, i - ring), min(grid.nx, i + ring + 1)):
                    if ring > 1 and max(abs(x - i), abs(y - j)) != ring:
                        continue
                    if _expired(deadline):
                        return {}
                    node = y * grid.nx + x
                    candidate = self._grid_point(grid, node, motion)
                    if candidate is None:
                        continue
                    a, b = (candidate, point) if toward_point else (point, candidate)
                    if self._walk(a, b, *motion):
                        links[node] = hypot(b[0] - a[0], b[1] - a[1]) + 0.4 * max(0, b[2] - a[2])
            if links:
                break
        return links

    def _search(self, grid, start, goal, motion, limit, deadline):
        ends = self._links(grid, goal, motion, toward_point=True, deadline=deadline)
        if not ends:
            return None, 0
        costs = self._links(grid, start, motion, toward_point=False, deadline=deadline)
        queue, parents = [], {}
        nx, spacing = grid.nx, grid.spacing
        gx, gy = goal[0] - grid.minx, goal[1] - grid.miny

        def priority(node, cost):
            y, x = divmod(node, nx)
            return cost + 1.18 * hypot(gx - x * spacing, gy - y * spacing)

        for node, cost in costs.items():
            heappush(queue, (priority(node, cost), cost, node))
        expanded = 0
        while queue and expanded < limit and not _expired(deadline):
            _, cost, node = heappop(queue)
            if cost != costs[node]:
                continue
            if node in ends:
                route = [goal] if grid.points[node] != goal else []
                while node is not None:
                    p = grid.points[node]
                    if p != start:
                        route.append(p)
                    node = parents.get(node)
                route.reverse()
                return route, expanded
            expanded += 1
            for adj, edge_cost in self._grid_edges(grid, node, motion):
                new = cost + edge_cost
                if new < costs.get(adj, inf):
                    costs[adj], parents[adj] = new, node
                    heappush(queue, (priority(adj, new), new, adj))
        return None, expanded

    def route(self, start, goal, stats, *, limit=700, deadline=None) -> list[tuple] | None:
        """Return a checked route, or None when no path is found before deadline.

        Deadline is absolute perf_counter time. A short direct edge may still
        be checked after it, but no graph search or alternate-goal scan begins.
        """
        direct_deadline = None if hypot(goal[0] - start[0], goal[1] - start[1]) <= 12.0 else deadline
        if _expired(direct_deadline):
            return None
        motion = self._motion(stats)
        radius, height, step, _ = motion
        start = self._supported(start, radius, height)
        if start is None:
            return None
        end = self._supported(goal, radius, height)
        if end is None:
            if _expired(deadline):
                return None
            # Keep the requested elevation even when offsetting from a wall.
            for distance in (0.75, 1.5):
                options = [p for p in self._neighbors(goal, distance, radius, height, step)
                           if abs(p[2] - goal[2]) <= _SUPPORT_TOL]
                if options:
                    end = min(options, key=lambda p: hypot(p[0] - start[0], p[1] - start[1]))
                    break
        if end is None:
            return None
        if (not _expired(direct_deadline) and self._walk(start, end, *motion, deadline=direct_deadline)
                and not _expired(direct_deadline)):
            return [] if start == end else [end]
        if _expired(deadline):
            return None
        if limit <= 0 or any(self._floor((p[0], p[1], self.ground(p[0], p[1])), radius, height, step) != p
                             for p in (start, end)):
            # This graph models ground only. An upper-floor target requires an
            # actual checked jet route, not a path to the ground beneath it.
            return None
        if motion != self._grid_stats:
            self._grid_stats = motion
            for grid in self._grids:
                grid.points.clear()
                grid.edges.clear()
        coarse_limit = max(1, 2 * limit // 3)
        result, used = self._search(self._grids[0], start, end, motion, coarse_limit, deadline)
        if _expired(deadline):
            return None
        if result is not None or used >= limit:
            return result
        result, _ = self._search(self._grids[1], start, end, motion, limit - used, deadline)
        return None if _expired(deadline) else result

    def _air_clear(self, a, b, radius, height):
        if not (self._inside(a[0], a[1], radius) and self._inside(b[0], b[1], radius)
                and self._terrain.clear(a, b)):
            return False
        for p in self._body(a[0], a[1], b[0], b[1], radius):
            span = _interval(a[2], b[2] - a[2], p.base - height + _EPS, p.top - _EPS)
            if span is not None and p.spans(_segment_point(a, b, span[0]), _segment_point(a, b, span[1]), radius):
                return False
        return True

    def _rim_candidates(self, point, radius):
        x, y, z = point
        margin = radius + 0.75
        for p in self._body(x, y, x, y, radius):
            if abs(p.top - z) > _SUPPORT_TOL or not p.touches(x, y, radius):
                continue
            for ax, ay, ex, ey, ee in p.edges:
                t = min(1.0, max(0.0, ((x - ax) * ex + (y - ay) * ey) / ee))
                edge_x, edge_y, scale = ax + t * ex, ay + t * ey, margin / sqrt(ee)
                yield edge_x - ey * scale, edge_y + ex * scale
                yield edge_x + ey * scale, edge_y - ex * scale

    def _descent_clear(self, a, landing, radius, height):
        over = (landing[0], landing[1], a[2])
        if not (self._air_clear(a, over, radius, height) and self._air_clear(over, landing, radius, height)):
            return False
        # Once past the roof rim the mech may start falling before it reaches
        # the landing XY. Keep that whole remaining corridor free of lower roofs.
        support, obstacles = [], []
        for p in self._body(a[0], a[1], over[0], over[1], radius):
            if p.top <= landing[2] + _EPS or p.base >= a[2] + height - _EPS:
                continue
            spans = p.spans(a, over, radius)
            if abs(p.top - a[2]) <= _EPS:
                support.extend(spans)
            else:
                obstacles.extend(spans)
        rim = 0.0
        for lo, hi in sorted(support):
            if lo > rim + _EPS:
                break
            rim = max(rim, hi)
        return not any(hi > rim + _EPS for _, hi in (*support, *obstacles))

    def descent(self, start, toward, stats, *, deadline=None) -> tuple | None:
        """Select a ground landing reachable off the current elevated surface."""
        radius, height, step, _ = self._motion(stats)
        takeoff = self._supported(start, radius, height)
        if takeoff is None:
            takeoff = tuple(start)
        x, y, z = takeoff
        candidates = {(x, y), *self._rim_candidates(takeoff, radius)}
        for distance in (radius + 1, radius + 4, radius + 10, radius + 18):
            for dx, dy in _DIRECTIONS:
                d = distance * _DIAGONAL if dx and dy else distance
                candidates.add((x + dx * d, y + dy * d))
        ordered = sorted(candidates, key=lambda p: (hypot(p[0] - x, p[1] - y) +
                          0.35 * hypot(p[0] - toward[0], p[1] - toward[1]), p))
        for lx, ly in ordered[:48]:
            if _expired(deadline):
                return None
            land = self._floor((lx, ly, self.ground(lx, ly)), radius, height, step)
            if land is None or z - land[2] < max(0.4, step):
                continue
            if self._descent_clear(takeoff, land, radius, height):
                return land
        return None

    def rooftop(self, start, goal, stats, energy, *, deadline=None) -> tuple | None:
        """A verified launch, roof landing, and exit. Boost vertically first."""
        motion = self._motion(stats)
        radius, height, step, _ = motion
        roof = self._supported(goal, radius, height)
        if roof is None or roof[2] - self.ground(roof[0], roof[1]) <= step + _Z_EPS:
            return None
        if not any(p.contains(roof[0], roof[1]) and abs(p.top - roof[2]) <= _Z_EPS
                   for p in self._body(roof[0], roof[1], roof[0], roof[1], radius)):
            return None
        drain = self._stat(stats, 'jet_drain', 15.0)
        rise = self._stat(stats, 'max_rise_speed', 10.0)
        gravity = self._stat(stats, 'gravity', 22.0)
        net = self._stat(stats, 'jet_accel', 34.0) - gravity
        jump = min(rise, self._stat(stats, 'jump_speed', 11.0))
        speed = self._stat(stats, 'walk_speed', 11.7)
        accel = self._stat(stats, 'air_accel', 12.0)
        if min(rise, net, gravity, speed, accel) <= 0 or energy <= 0:
            return None
        cap_time = max(0.0, (rise - jump) / net)
        cap_gain = jump * cap_time + net * cap_time**2 / 2
        cruise = roof[2] + 0.5
        candidates = {*self._rim_candidates(roof, radius), tuple(start[:2])}
        ordered = sorted(candidates, key=lambda p: (hypot(p[0] - start[0], p[1] - start[1]) +
                          hypot(p[0] - roof[0], p[1] - roof[1]), p))
        for x, y in ordered[:24]:
            if _expired(deadline):
                return None
            launch = self._floor((x, y, self.ground(x, y)), radius, height, step)
            if launch is None:
                continue
            up = cruise - launch[2]
            if up <= 0:
                continue
            climb = ((sqrt(jump**2 + 2 * net * up) - jump) / net if up <= cap_gain
                     else cap_time + (up - cap_gain) / rise)
            distance = hypot(x - roof[0], y - roof[1])
            crossing = 2 * sqrt(distance / accel) if distance <= speed * speed / accel else distance / speed + speed / accel
            if drain * (climb + crossing + 0.4) > energy:
                continue
            launch_air, roof_air = (x, y, cruise), (roof[0], roof[1], cruise)
            roof_peak = roof_air[0], roof_air[1], cruise + rise * crossing + rise * rise / (2 * gravity)
            if not (self._air_clear(launch, launch_air, radius, height) and
                    self._air_clear(launch_air, roof_air, radius, height) and
                    self._air_clear(roof_peak, roof, radius, height)):
                continue
            # Continuous boost can climb during the crossing, so reject overhead
            # objects anywhere in that vertical band, not just at cruise height.
            if any(p.top > cruise + _Z_EPS and p.base < roof_peak[2] + height - _Z_EPS and
                   p.spans(launch_air, roof_air, radius) for p in self._body(x, y, roof[0], roof[1], radius)):
                continue
            exit_point = self.descent(roof, start, stats, deadline=deadline)
            if exit_point is not None and self.route(start, launch, stats, limit=180, deadline=deadline) is not None:
                return launch, roof, exit_point
        return None
