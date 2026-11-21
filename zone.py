"""Project the observed circle, never undisclosed later phases."""
from math import hypot, inf


def distance(a, b):
    return hypot(a[0] - b[0], a[1] - b[1])


class Zone:
    def __init__(self, obs):
        z = obs['zone']
        self.center = z['center']
        self.radius = z['radius']
        self.next = z['next_center']
        self.next_radius = z['next_radius']
        self.hold = max(0., z['shrink_start'] - obs['t'])
        self.end = max(self.hold, z['shrink_end'] - obs['t'])
        self.dps = z['dps']

    def margin(self, p, dt=0.):
        if dt <= self.hold:
            return self.radius - distance(p, self.center)
        if dt >= self.end:
            return self.next_margin(p)
        u = (dt - self.hold) / (self.end - self.hold)
        x = self.center[0] + u * (self.next[0] - self.center[0])
        y = self.center[1] + u * (self.next[1] - self.center[1])
        return self.radius + u * (self.next_radius - self.radius) - hypot(p[0] - x, p[1] - y)

    def next_margin(self, p):
        return self.next_radius - distance(p, self.next)

    def deadline(self, p, buffer=0.):
        """Seconds until a static point becomes unsafe in the known phase."""
        if self.margin(p) < buffer:
            return 0.
        if self.next_margin(p) >= buffer:
            return inf
        lo, hi = self.hold, self.end
        for _ in range(12):
            mid = (lo + hi) * .5
            if self.margin(p, mid) >= buffer:
                lo = mid
            else:
                hi = mid
        return lo

    def travel(self, start, path, speed, *, delay=0., offset=0., slack=0.):
        """Return (end time, projected damage); sample inside each route segment."""
        time, exposed = offset, 0.
        a = start
        for b in path:
            length = distance(a, b)
            if not length:
                continue
            duration = length / speed
            samples = max(1, int(length / 7) + 1)
            for i in range(samples):
                f = (i + .5) / samples
                p = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
                if self.margin(p, time + duration * f + slack) < 0:
                    exposed += duration / samples
            a = b
            time += duration
        if delay:
            exposed += max(0., min(delay, time + delay + slack - self.deadline(a)))
            time += delay
        return time, exposed * self.dps
