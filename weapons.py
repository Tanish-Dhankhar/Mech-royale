"""Fire control for the mechfight/1 observation and action protocol."""

import math
from dataclasses import dataclass


_ZERO = (0.0, 0.0, 0.0)
_DEFAULTS = {
    "gun": {"speed": 252.0, "range": 230.0, "spread_deg": 0.6, "mag": 12, "damage": 45.0},
    "laser": {"range": 280.0, "damage": 315.0, "charge": 0.75, "min_charge": 0.15,
              "charge_turn_mult": 1.0, "cookoff": 0.6},
    "lightning": {"range": 45.0, "cone_deg": 30.0, "energy": 3.0, "cooldown": 0.25, "short_s": 2.0},
    "mortar": {"speed": 72.0, "gravity": 22.0, "ttl": 7.0, "splash_radius": 7.0},
}


def _wrap(angle):
    return (angle + math.pi) % math.tau - math.pi


def _advance(pos, vel, t):
    return pos[0] + vel[0] * t, pos[1] + vel[1] * t, pos[2] + vel[2] * t


def _angles(x, y, z):
    return math.atan2(y, x), math.atan2(z, math.hypot(x, y))


def _direction(aim):
    yaw, pitch = aim
    c = math.cos(pitch)
    return c * math.cos(yaw), c * math.sin(yaw), math.sin(pitch)


def _toward(delta, limit):
    return max(-limit, min(limit, delta))


@dataclass(slots=True)
class _Body:
    id: int | None
    kind: str
    pos: tuple
    vel: tuple
    radius: float
    height: float

    def center(self, t):
        x, y, z = _advance(self.pos, self.vel, t)
        return x, y, z + self.height * 0.5


@dataclass(slots=True)
class _Pose:
    pos: tuple
    vel: tuple
    aim: tuple
    offset: float
    height: float
    muzzle: float
    turn: float
    pitch: float
    min_pitch: float
    max_pitch: float

    def command(self, aim):
        return _wrap(aim[0]), max(self.min_pitch, min(self.max_pitch, aim[1]))

    def actual(self, command, delay, turn_mult=1.0):
        yaw, pitch = self.aim
        return (
            yaw + _toward(_wrap(command[0] - yaw), self.turn * turn_mult * delay),
            pitch + _toward(command[1] - pitch, self.pitch * delay),
        )

    def base(self, delay, mortar=False):
        x, y, z = _advance(self.pos, self.vel, delay)
        return x, y, z + (self.height + 0.5 if mortar else self.muzzle)


def _intersection(origin, velocity, body, start, limit, *, moving, pad=0.0, margin=0.0):
    """First ray/segment entry into the (possibly moving) vertical cylinder."""
    px, py, pz = body.pos
    bx, by, bz = body.vel
    x, y, z = origin[0] - px - bx * start, origin[1] - py - by * start, origin[2] - pz - bz * start
    vx, vy, vz = velocity
    if moving:
        vx, vy, vz = vx - bx, vy - by, vz - bz
    radius = body.radius + pad - margin
    c = x * x + y * y - radius * radius
    a = vx * vx + vy * vy
    lo, hi = 0.0, limit
    if a > 1e-12:
        b = x * vx + y * vy
        disc = b * b - a * c
        if disc < 0.0:
            return None
        root = math.sqrt(disc)
        lo, hi = max(lo, (-b - root) / a), min(hi, (-b + root) / a)
    elif c > 0.0:
        return None
    bottom, top = margin - pad, body.height + pad - margin
    if abs(vz) > 1e-9:
        enter, leave = (bottom - z) / vz, (top - z) / vz
        lo, hi = max(lo, min(enter, leave)), min(hi, max(enter, leave))
    elif not bottom <= z <= top:
        return None
    return lo if lo <= hi else None


def _intercept(base, body, delay, speed, offset):
    """Intercept a linearly moving center, including the barrel's forward offset."""
    cx, cy, cz = body.center(delay)
    x, y, z = cx - base[0], cy - base[1], cz - base[2]
    vx, vy, vz = body.vel
    a = vx * vx + vy * vy + vz * vz - speed * speed
    b = x * vx + y * vy + z * vz - offset * speed
    c = x * x + y * y + z * z - offset * offset
    disc = b * b - a * c
    if disc >= 0.0 and c > 0.0:
        # With the target initially outside the muzzle, this is the first entering root.
        denom = math.sqrt(disc) - b
        if denom > 0.0:
            t = c / denom
            return _angles(x + vx * t, y + vy * t, z + vz * t)
    return _angles(x, y, z)


class Weapons:
    def __init__(self, config: dict, arena):
        self.arena = arena
        match = config.get("match", {})
        self.dt = 1.0 / match.get("tick_hz", 20)
        self.steps = match.get("decide_every", 2)
        self.window = self.dt * self.steps
        self.mech = config.get("mech", {})
        self.npc_range = config.get("npc", {}).get("aggro_range", 45.0)
        self.size_per_cube = config.get("cubes", {}).get("size_per_cube", 0.05)
        self.dimensions = {
            kind: (config.get(section, {}).get("radius", radius), config.get(section, {}).get("height", height))
            for kind, section, radius, height in (
                ("mech", "mech", 2.5, 9.0), ("npc", "npc", 1.8, 3.5), ("crate", "crates", 1.4, 2.4),
            )
        }
        self.gun_radius = config.get("projectile", {}).get("gun_radius", 0.3)
        self.rules = {kind: defaults | config.get("weapons", {}).get(kind, {}) for kind, defaults in _DEFAULTS.items()}
        self.short_until = -math.inf

    def _body(self, entity, kind=None):
        eid = entity.get("id")
        kind = kind or entity.get("kind")
        if kind not in self.dimensions:
            kind = "crate" if eid is not None and eid >= 1000 else "npc" if eid is not None and eid >= 100 else "mech"
        size = 1.0 + self.size_per_cube * entity.get("cubes", 0) if kind == "mech" else 1.0
        radius, height = self.dimensions[kind]
        return _Body(eid, kind, entity["pos"], entity.get("vel", _ZERO), radius * size, height * size)

    def _pose(self, own):
        stats = own.get("stats", {})
        size = 1.0 + self.size_per_cube * own.get("cubes", 0)
        radius, height = self.dimensions["mech"]
        return _Pose(
            own["pos"], own.get("vel", _ZERO), own.get("aim", (0.0, 0.0)),
            stats.get("radius", radius * size) + 0.5, stats.get("height", height * size),
            stats.get("muzzle_height", self.mech.get("muzzle_height", 7.0) * size),
            stats.get("turn_rate", self.mech.get("turn_rate", 4.0)),
            stats.get("pitch_rate", self.mech.get("pitch_rate", 3.0)),
            stats.get("min_pitch", self.mech.get("min_pitch", -0.7)),
            stats.get("max_pitch", self.mech.get("max_pitch", 1.3)),
        )

    def _delay(self, seconds):
        return self.dt * max(1, math.ceil((seconds - 1e-8) / self.dt))

    def _ray(self, pose, direction, delay, target, others, reach, *, speed=0.0, spread=0.0):
        base = pose.base(delay)
        origin = _advance(base, direction, pose.offset)
        if speed:
            distance = math.dist(origin, target.center(delay))
            margin = min(min(target.radius, target.height * 0.5) * 0.45, spread * distance * 0.65)
            velocity = tuple(component * speed for component in direction)
            limit, pad = reach / speed, self.gun_radius
        else:
            margin, velocity, limit, pad = 0.0, direction, reach, 0.0
        hit = _intersection(origin, velocity, target, delay, limit, moving=bool(speed), pad=pad, margin=margin)
        if hit is None or not self.arena.clear(base, _advance(origin, velocity, hit)):
            return False
        # The selected target must be the first plausible contact.
        return not any(
            _intersection(origin, velocity, other, delay, hit, moving=bool(speed), pad=pad) is not None
            for other in others
        )

    def _gun(self, gun, pose, command, target, others, turn_mult):
        if gun is None:
            return False
        _, state, stats = gun
        delay = self._delay(max(state.get("cooldown", 0.0), state.get("reload", 0.0)))
        if delay > self.window or state.get("reload", 0.0) > 0.0 or state.get("ammo", stats["mag"]) <= 0:
            return False
        direction = _direction(pose.actual(command, delay, turn_mult))
        return self._ray(pose, direction, delay, target, others, stats["range"],
                         speed=stats["speed"], spread=math.radians(stats["spread_deg"]))

    def _reload(self, obs, pose, gun):
        if gun is None:
            return False
        _, state, stats = gun
        if not 0 < state.get("ammo", stats["mag"]) <= stats["mag"] // 2 or state.get("reload", 0.0) > 0.0:
            return False
        if any(event.get("type") == "damaged" for event in obs.get("events", ())):
            return False
        base = pose.base(0.0)
        for actor in obs.get("mechs", ()):
            body = self._body(actor)
            center = body.center(0.0)
            reach = self.npc_range if body.kind == "npc" else self.rules["laser"]["range"]
            if math.dist(base, center) <= reach and self.arena.clear(center, base):
                return False
        return True

    def _lightning(self, weapon, pose, command, target, others, energy, turn_mult):
        _, state, stats = weapon
        delay = self._delay(state.get("cooldown", 0.0))
        interval = self._delay(stats["cooldown"])
        if delay > self.window:
            return False
        # Holding one action can fire more than once with a fast cooldown.
        shots = 1 + int((self.window - delay + 1e-8) / interval)
        if energy < shots * stats["energy"]:
            return False
        cone_cos, reach = math.cos(math.radians(stats["cone_deg"])), stats["range"]
        for _ in range(shots):
            actual = pose.actual(command, delay, turn_mult)
            base = pose.base(delay)
            direction = _direction(actual)
            origin = _advance(base, direction, pose.offset)
            center = target.center(delay)
            if math.dist(origin, center) > reach + pose.offset:
                return False
            # A barrel touching cover does not put its origin beyond that cover.
            if not self.arena.clear(base, origin):
                origin = base
            distance = math.dist(origin, center)
            cos_yaw, sin_yaw = math.cos(actual[0]), math.sin(actual[0])

            def in_cone(point):
                x, y = point[0] - origin[0], point[1] - origin[1]
                return cos_yaw * x + sin_yaw * y >= cone_cos * math.hypot(x, y)

            # Lightning uses a yaw wedge, not a cone around the barrel's pitch.
            if distance > reach or not in_cone(center) or not self.arena.clear(origin, center):
                return False
            for other in others:
                point = other.center(delay)
                if math.dist(origin, point) <= distance and in_cone(point) and self.arena.clear(origin, point):
                    return False
            delay += interval
        return True

    def _laser_release(self, stats, pose, target, others, charge, turn_mult):
        # Releasing before minimum charge queues the shot instead of canceling it.
        delay = self._delay(stats["min_charge"] - charge)
        if delay > self.window:
            return None
        base = pose.base(delay)
        x, y, z = target.center(delay)
        command = pose.command(_angles(x - base[0], y - base[1], z - base[2]))
        direction = _direction(pose.actual(command, delay, turn_mult))
        return command if self._ray(pose, direction, delay, target, others, stats["range"]) else None

    def _mortar_arcs(self, pose, base, delay, target, stats):
        cx, cy, cz = target.center(delay)
        x, y, z = cx - base[0], cy - base[1], cz - base[2]
        vx, vy, vz = target.vel
        speed, gravity = stats["speed"], stats["gravity"]
        # Long leads are only predictable for stationary crates. Limit actor arcs
        # further for targets moving quickly or changing height in the air.
        if target.kind == "crate":
            max_time = stats["ttl"]
        else:
            max_time = min(stats["ttl"], 2.1 if math.hypot(vx, vy) > 4.0 or abs(vz) > 1.0 else 3.4)

        def residual(t):
            return ((x + vx * t) ** 2 + (y + vy * t) ** 2 + (z + vz * t + 0.5 * gravity * t * t) ** 2
                    - (pose.offset + speed * t) ** 2)

        left, value = 0.0, residual(0.0)
        steps = min(36, max(1, math.ceil(max_time / 0.20)))
        for k in range(1, steps + 1):
            right = max_time * k / steps
            next_value = residual(right)
            if (value > 0.0) != (next_value > 0.0):
                lo, hi = left, right
                for _ in range(12):
                    mid = (lo + hi) * 0.5
                    if (residual(mid) > 0.0) == (value > 0.0):
                        lo = mid
                    else:
                        hi = mid
                t = (lo + hi) * 0.5
                raw = _angles(x + vx * t, y + vy * t, z + vz * t + 0.5 * gravity * t * t)
                if pose.min_pitch <= raw[1] <= pose.max_pitch:
                    yield pose.command(raw), t
            left, value = right, next_value

    def _self_safe(self, pose, impact, time, splash):
        # Do not rely on blast occlusion or continued movement to escape our own shot.
        x, y, z = pose.pos
        vx, vy, vz = pose.vel
        dx, dy = impact[0] - x, impact[1] - y
        horizon = min(time, 0.65)
        speed2 = vx * vx + vy * vy
        near = min(horizon, max(0.0, (dx * vx + dy * vy) / speed2)) if speed2 else 0.0
        gap = max(0.0, math.hypot(dx - vx * near, dy - vy * near) - (pose.offset - 0.5))
        end_z = z + vz * horizon
        vertical = max(min(z, end_z) - impact[2], impact[2] - max(z, end_z) - pose.height, 0.0)
        return math.hypot(gap, vertical) > splash + 1.0

    def _mortar_trace(self, pose, base, direction, delay, flight, target, others, stats):
        splash, speed, gravity = stats["splash_radius"], stats["speed"], stats["gravity"]
        origin = _advance(base, direction, pose.offset)
        if not self.arena.clear(base, origin):
            return False
        velocity = tuple(c * speed for c in direction)
        horizon = min(stats["ttl"], flight + max(0.25, 2.0 * splash / speed))
        # The shell's horizontal trajectory is linear. Discard bodies outside
        # that swept corridor before testing each chord in three dimensions.
        nearby = []
        for other in others:
            x, y, _ = _advance(other.pos, other.vel, delay)
            x, y = x - origin[0], y - origin[1]
            vx, vy = velocity[0] - other.vel[0], velocity[1] - other.vel[1]
            speed2 = vx * vx + vy * vy
            near = min(horizon, max(0.0, (x * vx + y * vy) / speed2)) if speed2 else 0.0
            if (x - vx * near) ** 2 + (y - vy * near) ** 2 <= other.radius ** 2:
                nearby.append(other)
        steps = min(40, max(1, math.ceil(horizon / 0.18)))
        start, a = 0.0, origin
        for k in range(1, steps + 1):
            end = horizon * k / steps
            x, y, z = _advance(origin, velocity, end)
            b = (x, y, z - 0.5 * gravity * end * end)
            span = end - start
            chord = ((b[0] - a[0]) / span, (b[1] - a[1]) / span, (b[2] - a[2]) / span)
            hit = _intersection(a, chord, target, delay + start, span, moving=True)
            if hit is None:
                limit, endpoint = span, b
            else:
                limit, endpoint = hit, _advance(a, chord, hit)
            if not self.arena.clear(a, endpoint):
                # Locate the first terrain/prism impact. A wall between the blast
                # and target blocks the splash, even if the XY distance is small.
                lo, hi = 0.0, limit
                for _ in range(8):
                    mid = (lo + hi) * 0.5
                    if self.arena.clear(a, _advance(a, chord, mid)):
                        lo = mid
                    else:
                        hi = mid
                limit, endpoint, hit = lo, _advance(a, chord, lo), None
            for other in nearby:
                if _intersection(a, chord, other, delay + start, limit, moving=True) is not None:
                    return False
            time = delay + start + limit
            if hit is not None:
                return self._self_safe(pose, endpoint, time, splash)
            if limit < span:
                center = target.center(time)
                return (math.dist(endpoint, center) <= splash * 0.85 and self._self_safe(pose, endpoint, time, splash)
                        and self.arena.clear(endpoint, center))
            start, a = end, b
        return False

    def _mortar(self, weapon, pose, target, others, gun_hit, turn_mult):
        _, state, stats = weapon
        delay = self._delay(state.get("cooldown", 0.0))
        if delay > self.window:
            return None, False
        base = pose.base(delay, mortar=True)
        splash = stats["splash_radius"]
        if not self._self_safe(pose, target.center(delay), delay + 0.65, splash):
            return None, False
        track = None
        for command, flight in self._mortar_arcs(pose, base, delay, target, stats):
            center = target.center(delay + flight)
            if center[2] < self.arena.ground(center[0], center[1]) or not self._self_safe(pose, center, delay + flight, splash):
                continue
            aim = pose.actual(command, delay, turn_mult)
            # A small angular miss may still land a useful splash. Don't take over
            # a working gun aim merely to spend multiple ticks turning to a lob.
            error = math.hypot(_wrap(command[0] - aim[0]), command[1] - aim[1])
            tolerance = splash * 0.6 / (pose.offset + stats["speed"] * flight)
            if error <= tolerance and self._mortar_trace(pose, base, _direction(aim), delay, flight, target, others, stats):
                return command, True
            if track is None and not gun_hit and error > tolerance:
                if self._mortar_trace(pose, base, _direction(command), delay, flight, target, others, stats):
                    track = command
        return track, False

    def act(self, obs: dict, target: dict | None, *, urgent: bool, reserve: float,
            aim: tuple[float, float] | None = None) -> dict:
        now = obs.get("t", obs.get("tick", 0) * self.dt)
        if any(event.get("type") == "effect" and event.get("effect") == "short" for event in obs.get("events", ())):
            self.short_until = max(self.short_until, now + self.rules["lightning"]["short_s"])
        own = obs["self"]
        pose = self._pose(own)
        fallback = pose.command(aim if aim is not None else pose.aim)
        weapons = {}
        for slot, entry in enumerate(own.get("weapons", ())):
            if entry and entry.get("kind") in self.rules:
                kind = entry["kind"]
                weapons[kind] = slot, entry, self.rules[kind] | entry.get("stats", {})
        gun, laser = weapons.get("gun"), weapons.get("laser")
        fire = [False, False]
        action = {"aim": fallback, "fire": fire}
        charge = laser[1].get("charge", 0.0) if laser else 0.0
        if target is None:
            if not urgent and not charge and self._reload(obs, pose, gun):
                action["reload"] = True
            return action

        target_id = target.get("id")
        crate = target.get("kind") is None and any(
            entity is target or (target_id is not None and entity.get("id") == target_id)
            for entity in obs.get("crates", ())
        )
        body = self._body(target, "crate" if crate else None)
        if urgent and (body.kind == "crate" or gun is None):
            return action
        others = [
            self._body(entity, kind)
            for field, kind in (("mechs", None), ("crates", "crate"))
            for entity in obs.get(field, ())
            if entity is not target and (body.id is None or entity.get("id") != body.id)
            and entity.get("hp", 1) > 0
        ]
        turn_mult = laser[2]["charge_turn_mult"] if charge else 1.0
        base = pose.base(self.dt)
        cx, cy, cz = body.center(self.dt)
        direct = pose.command(_angles(cx - base[0], cy - base[1], cz - base[2]))
        command = direct
        if gun:
            gun_delay = min(self.window, self._delay(max(gun[1].get("cooldown", 0.0), gun[1].get("reload", 0.0))))
            command = pose.command(_intercept(pose.base(gun_delay), body, gun_delay, gun[2]["speed"], pose.offset))
        gun_hit = self._gun(gun, pose, command, body, others, turn_mult)
        if urgent:
            # Keep the escape-facing command when it admits a shot, or when the
            # target cannot be hit this decision. Dropping the laser trigger releases it.
            if command != fallback and self._gun(gun, pose, fallback, body, others, turn_mult):
                fire[gun[0]] = True
            elif gun_hit:
                action["aim"], fire[gun[0]] = command, True
            return action

        finishes = gun_hit and target.get("hp", math.inf) <= gun[2]["damage"]
        if laser and now >= self.short_until:
            stats = laser[2]
            if charge:
                gun_command = command
                release = self._laser_release(stats, pose, body, others, charge, turn_mult)
                # A partial charge is worth releasing early for a likely finish,
                # particularly against a mortar carrier. Never sit on full charge.
                fraction = min(1.0, max(charge, stats["min_charge"]) / max(self.dt, stats["charge"]))
                minimum = stats.get("min_frac", 0.35)
                damage = stats["damage"] * (minimum + (1.0 - minimum) * fraction)
                if "mortar" in target.get("weapons", ()):
                    damage *= 1.0 + stats["cookoff"]
                enough = charge + self.window * 0.5 >= stats["charge"] or target.get("hp", math.inf) <= damage
                if release is not None and enough:
                    command = release
                elif not finishes and charge < stats["charge"] - 1e-8 and self._ray(
                        pose, _direction(direct), self.dt, body, others, stats["range"]):
                    command, fire[laser[0]] = direct, True
                if gun:
                    fire[gun[0]] = gun_hit if command == gun_command else self._gun(
                        gun, pose, command, body, others, turn_mult)
                action["aim"] = command
                return action
            if not finishes and self._delay(laser[1].get("cooldown", 0.0)) <= self.window:
                time_to_align = max(abs(_wrap(direct[0] - pose.aim[0])) / max(1e-9, pose.turn * stats["charge_turn_mult"]),
                                    abs(direct[1] - pose.aim[1]) / max(1e-9, pose.pitch))
                if time_to_align <= stats["charge"] + self.window and self._ray(
                        pose, _direction(direct), self.dt, body, others, stats["range"]):
                    fire[laser[0]] = True
                    if not gun_hit:
                        command = direct

        mortar = weapons.get("mortar")
        if mortar and not finishes:
            arc, mortar_hit = self._mortar(mortar, pose, body, others, gun_hit, turn_mult)
            if arc is not None:
                command, fire[mortar[0]] = arc, mortar_hit
                gun_hit = self._gun(gun, pose, command, body, others, turn_mult)

        lightning = weapons.get("lightning")
        if lightning and not finishes:
            available = own.get("energy", 0.0) - reserve
            if self._lightning(lightning, pose, command, body, others, available, turn_mult):
                fire[lightning[0]] = True
            elif command != direct and (not gun_hit or target.get("charging", False)) and self._lightning(
                    lightning, pose, direct, body, others, available, turn_mult):
                command, fire[lightning[0]] = direct, True
                gun_hit = self._gun(gun, pose, command, body, others, turn_mult)
        if gun:
            fire[gun[0]] = gun_hit
        action["aim"] = command
        return action
