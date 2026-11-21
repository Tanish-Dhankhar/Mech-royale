"""Mech Royale policy: acquire power, leave an exit, convert in the last circle."""
from dataclasses import dataclass
from math import atan2, ceil, cos, hypot, pi, sin
from time import perf_counter

import mechfight
from arena import Arena
from weapons import Weapons
from zone import Zone, distance


def angle(a, b):
    return atan2(b[1] - a[1], b[0] - a[0])


def turn(a, b):
    return abs((a - b + pi) % (2 * pi) - pi)


def above(p, h):
    return p[0], p[1], p[2] + h


def offset(p, yaw, length):
    return p[0] + cos(yaw) * length, p[1] + sin(yaw) * length, p[2]


def direction(p, q, scale=3.):
    d = max(scale, distance(p, q))
    return [(q[0] - p[0]) / d, (q[1] - p[1]) / d]


@dataclass(slots=True)
class Intent:
    kind: str
    p: tuple
    reward: float = 0.
    item: dict | None = None
    wait: float = 0.
    acquire: float = 0.
    path: list | None = None
    roof: tuple | None = None
    detour: bool = False

    @property
    def key(self):
        return self.kind, self.item['id'] if self.item is not None else None

@dataclass(slots=True)
class CoverRun:
    foe: dict
    home: tuple
    edge: tuple
    phase: str
    since: float
    until: float
    laser: bool = False


class Bot:
    name = 'Cutline'

    def __init__(self, hello):
        self.cfg = hello['config']
        self.arena = Arena(hello['map']['dir'], self.cfg['mech'])
        self.weapons = Weapons(self.cfg, self.arena)
        self.direct_threat_reach = {kind: rules['range'] for kind, rules in self.weapons.rules.items()
                                    if kind != 'mortar'}
        mortar = self.weapons.rules['mortar']
        self.mortar_reach = mortar['speed'] ** 2 / mortar['gravity'] + mortar['splash_radius']
        self.upgrades = {u['id']: u['effects'] for u in self.cfg['upgrades']}
        self.items = {}
        self.attackers = {}
        self.threat_bearing = None
        self.last_damage = self.last_combat = float('-inf')
        self.incoming = 0.
        self.intent = None
        self.replan = self.anchor_until = 0.
        self.anchor_key = None
        self.anchor_point = None
        self.route_fail = {}
        self.drop_abort = {}
        self.scan_marks = {}
        self.pursuit = None
        self.cover_run = self.cover_history = None
        self.cover_retry = 0.
        self.flanked_at = float('-inf')
        self.shot_at = float('-inf')
        self.shot_target = None
        self.shot_origin = None
        self.progress_at = None
        self.stalled = 0.
        self.climbing = False
        self.last_t = None

    def act(self, obs):
        self.stop_work = perf_counter() + .023
        self.searches = 0
        self.obs, self.me, self.t = obs, obs['self'], obs['t']
        self.p = tuple(self.me['pos'])
        self.stats = self.me['stats']
        self.r, self.h = self.stats['radius'], self.stats['height']
        self.speed = self.stats['walk_speed'] * .87
        self.reserve = min(self.me['max_energy'], max(24., self.me['max_energy'] * .27))
        self.zone = Zone(obs)
        self.enemies = [e for e in obs['mechs'] if e['kind'] == 'mech']
        self.npcs = [e for e in obs['mechs'] if e['kind'] == 'npc']
        self._observe_events()
        self.exposure_cache = {}
        self.dangers = self._lines(self.p, .45)
        self.main = next((e for e in self.dangers if 'pos' in e), None)
        self._remember_loot()
        self.margin = self.zone.margin(self.p)
        self.next_margin = self.zone.next_margin(self.p)
        self.pinched = self._must_rotate()
        self.rush = self.margin < 0 or self.zone.deadline(self.p) < 3.
        wounded = self.me['hp'] / self.me['max_hp']
        duel = obs['alive'] == 2
        duel_foe = next((e for e in self.enemies if self._shot_line(e)), None) if duel else None
        ahead = duel_foe is not None and self._advantage(duel_foe) > .16
        self.unsafe = len(self.dangers) >= 2 or (
            self.main is not None and not ahead and (
                wounded < .44 or (wounded < .76 and self.incoming > .27)
                or (self.me['energy'] < self.reserve * .45 and self.incoming > .14)
            )
        )
        self._choose(wounded, duel, ahead)
        aim, target = self._target(duel, ahead)
        critical = self.rush or self.unsafe or self.intent.kind == 'escape'
        move, boost, jump = self._move(target, critical)
        urgent = critical or self.pinched
        interrupt = duel and urgent and self.intent.kind in ('escape', 'rotate')
        reserve = self.reserve
        if interrupt and boost:
            drain = 'boost_drain' if self.me['grounded'] and not jump else 'jet_drain'
            reserve += self.stats[drain] * self.weapons.window
        # An automatic top-off must not lock the gun just as it steps out.
        on_cover_leg = self.intent.kind == 'cover' and self.cover_run.phase in ('stage', 'out', 'return')
        action = self.weapons.act(obs, target, urgent=urgent or on_cover_leg, reserve=reserve, aim=aim,
                                  interrupt=interrupt)
        action.update(tick=obs['tick'], move=move, boost=boost, jump=jump)
        if self.intent.kind == 'cover':
            if target is not None:
                self._cover_trigger(action['fire'])
        # A defensive lightning interrupt or a cover burst is not a pursuit cue.
        elif target and any(
                w and (not f and w.get('charge', 0.) > 0. if w['kind'] == 'laser'
                       else f and not (interrupt and w['kind'] == 'lightning'))
                for w, f in zip(self.me['weapons'], action['fire'])):
            self.shot_target, self.shot_at, self.shot_origin = target, self.t, self.p
        self._equip(action, critical)
        if (target is None and not critical and not self.pinched and obs['alive'] > 1
                and self.intent.kind not in ('heal', 'flank', 'cover') and 'drop' not in action):
            foe = self._defensive_target()
            if foe is not None:
                # Use the gun-only controller path; don't steer or remember a missed shot.
                shot = self.weapons.act(obs, foe, urgent=True, reserve=self.reserve, aim=aim)
                if shot['fire'][0]:
                    action.update(shot)
                    action.pop('reload', None)
        if obs['offer']:
            action['pick'] = self._pick(obs['offer'])
        return action

    def _observe_events(self):
        dt = 0. if self.last_t is None else max(0., self.t - self.last_t)
        self.last_t = self.t
        self.incoming *= .57 ** dt
        for ev in self.obs['events']:
            kind = ev['type']
            if kind == 'hit':
                self.last_combat = self.t
            elif kind == 'damaged':
                self.last_combat = self.t
                if ev['weapon'] != 'zone':
                    self.last_damage = self.t
                    self.incoming += ev['amount'] / self.me['max_hp']
                    if ev.get('bearing') is not None:
                        self.threat_bearing = ev['bearing']
                    if ev.get('by') is not None:
                        self.attackers[ev['by']] = self.t
            elif kind == 'kill':
                self.attackers.pop(ev['target'], None)
                if self.pursuit and self.pursuit[0] == ev['target']:
                    self.pursuit = None
            elif kind in ('took', 'take_failed') and ev.get('reason') not in ('range', 'busy'):
                self.items.pop(ev['id'], None)
        self.attackers = {k: v for k, v in self.attackers.items() if self.t - v < 2.1}
        if not self.progress_at or distance(self.progress_at[0], self.p) > 1.4:
            self.progress_at = self.p, self.t
            self.stalled = 0.
        else:
            self.stalled = self.t - self.progress_at[1]
        self.route_fail = {k: v for k, v in self.route_fail.items() if v > self.t}
        self.drop_abort = {k: v for k, v in self.drop_abort.items() if v > self.t}

    def _line(self, source, p, *, body_height=.57):
        if source['kind'] == 'npc':
            height = 2.5
        else:
            height = self.cfg['mech']['muzzle_height'] * (1 + self.cfg['cubes']['size_per_cube'] * source.get('cubes', 0))
        return self.arena.clear(above(source['pos'], height), above(p, self.h * body_height))

    def _lines(self, p, horizon):
        key = (*p, round(horizon, 1))
        if key in self.exposure_cache:
            return self.exposure_cache[key]
        found = []
        for actor in self.enemies + self.npcs:
            d = distance(actor['pos'], p)
            recent = actor['id'] in self.attackers
            if actor['kind'] == 'npc':
                threatening = d < (49 if recent else 44)
            else:
                reach = 150 if actor.get('charging') or 'laser' in actor.get('weapons', ()) else 115
                facing = turn(actor['aim'][0], angle(actor['pos'], p)) < 1.15
                vel = actor.get('vel', (0, 0))
                bearing = angle(actor['pos'], p)
                closing = vel[0] * cos(bearing) + vel[1] * sin(bearing) > 4
                threatening = (recent and d < 230) or d < 37 or (d < reach and (facing or closing))
            if not threatening:
                continue
            if self._line(actor, p):
                found.append(actor)
            elif horizon > 1. and actor['kind'] == 'mech' and (closing or recent) and d < min(90, 29 + horizon * 10):
                v = actor.get('vel', (0., 0., 0.))
                q = tuple(actor['pos'][i] + v[i] * min(1.5, horizon) for i in range(3))
                if self._line({**actor, 'pos': q}, p):
                    found.append(actor)
        found.sort(key=lambda e: (e['id'] not in self.attackers, e['kind'] == 'npc', distance(e['pos'], p)))
        if distance(p, self.p) < 4. and self.t - self.last_damage < .8:
            known = {a['id'] for a in self.enemies + self.npcs}
            found.extend({'id': i} for i in self.attackers if i not in known)
        self.exposure_cache[key] = found
        return found

    def _visible_from_here(self, p):
        d = distance(self.p, p)
        cfg = self.cfg['vision']
        if d > cfg['range'] + self.stats.get('vision_bonus', 0.):
            return False
        near = d <= cfg['near_range'] + self.stats.get('near_range_bonus', 0.)
        if not near and turn(self.me['aim'][0], angle(self.p, p)) > cfg['fov_deg'] * pi / 360:
            return False
        ray = self.arena.clear if near or not cfg.get('canopy_blocks', True) else self.arena.can_see
        return ray(above(self.p, self.stats['eye_height']), above(p, .5))

    def _remember_loot(self):
        for kind in ('cubes', 'pickups'):
            visible = {obj['id'] for obj in self.obs[kind]}
            for obj in self.obs[kind]:
                self.items[obj['id']] = kind, obj, self.t
            for ident, (tag, obj, seen) in list(self.items.items()):
                if tag != kind or ident in visible:
                    continue
                if self.t - seen > 9 or (distance(self.p, obj['pos']) < 75 and self._visible_from_here(obj['pos'])):
                    del self.items[ident]
        tile = (int(self.p[0] // 24), int(self.p[1] // 24))
        self.scan_marks[tile] = self.t

    def _must_rotate(self):
        if self.margin < 0:
            return True
        if self.next_margin >= min(9., self.zone.next_radius * .25):
            return False
        if self.zone.next_radius < 1. and self.next_margin > -3. and self.zone.end < 3.:
            return False
        need = max(0., -self.next_margin + min(12., self.zone.next_radius * .25))
        return self.zone.deadline(self.p, min(3., self.zone.radius * .2)) < max(5.5, need / self.speed * 1.6 + 4.5)

    def _ground(self, p):
        return p[0], p[1], self.arena.ground(p[0], p[1])

    def _walk(self, a, b):
        return self.arena.walkable(a, b, self.r, self.h, self.stats['step_up'], self.stats['max_slope_deg'])

    def _anchor(self, mode):
        key = mode, tuple(self.zone.next), tuple(e['id'] for e in self.dangers[:3])
        if key == self.anchor_key and self.t < self.anchor_until:
            return self.anchor_point
        yaw = angle(self.p, self.zone.next)
        rotate = mode == 'rotate'
        escape = mode == 'escape'
        if escape and self.main:
            yaw = angle(self.main['pos'], self.p)
            if self.next_margin < 8.:
                yaw = angle(self.p, self.zone.next)
        circle = self.zone.next_radius
        missing = max(0., -self.next_margin + min(22., circle * .35))
        length = max(16., missing) if rotate else 17.
        desired = self._ground(self.zone.next if rotate and circle < 14 else offset(self.p, yaw, length))
        if not rotate:
            points = [self.p, desired]
        else:
            points = [desired]
            if circle < 14:
                points.append(self.p)
        radius = 8. if rotate and circle < 25. else 15.
        points.extend(self.arena.neighbors(desired if rotate else self.p, radius, self.r, self.h))
        if rotate and not any(self.arena.fits(q, self.r, self.h) for q in points):
            points.extend(self.arena.neighbors(desired, radius * 2, self.r, self.h))
        candidates = []
        for q in points:
            if not self.arena.fits(q, self.r, self.h) or (not rotate and not self._walk(self.p, q)):
                continue
            d = distance(self.p, q)
            hits = len(self._lines(q, .45)) if self.enemies or self.npcs else 0
            if not rotate and self.zone.margin(q, d / self.speed + 3) < 0:
                continue
            m = self.zone.next_margin(q)
            positional = min(m, min(33., circle * .48))
            tile = int(q[0] // 24), int(q[1] // 24)
            recent = self.t - self.scan_marks.get(tile, -100.) < 14
            novelty = (0. if recent else 13.) if mode == 'search' else 0.
            scan = 8. if mode == 'search' and d > 8. else 0.
            covered = self._cover(q) if mode != 'search' or d < 50 else 0.
            if escape:
                novelty += min(d, 15.) * .85
                if self.main:
                    novelty += (distance(self.main['pos'], q) - distance(self.main['pos'], self.p)) * .58
            if mode == 'duel' and self.main:
                old = angle(self.main['pos'], self.p)
                fresh = turn(old, angle(self.main['pos'], q))
                novelty += min(16., fresh * 19.)
            if rotate:
                _, harm = self.zone.travel(self.p, [q], self.speed)
                positional -= harm / max(22., self.me['hp'] * .065)
            score = positional * (.9 if rotate else .36) + covered + novelty + scan - 28. * hits - d * .12
            candidates.append((score, q))
            if perf_counter() > self.stop_work and candidates:
                break
        point = max(candidates, default=(0., self.p if not rotate else desired), key=lambda row: row[0])[1]
        if mode == 'search' and distance(self.p, point) < .9 and not self.dangers:
            point = self._search_step()
        self.anchor_key, self.anchor_point = key, point
        self.anchor_until = self.t + (.48 if rotate else .9)
        return point

    def _search_step(self):
        # The fixed compass ring can miss a traversable doorway or narrow lane.
        yaw = self.me['aim'][0]
        for reach in (15., 5.):
            for turn_index in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8):
                if perf_counter() >= self.stop_work:
                    return self.p
                q = self._ground(offset(self.p, yaw + turn_index * pi / 8, reach))
                if (self.zone.margin(q, reach / self.speed + 3.) >= 0
                        and self._walk(self.p, q)
                        and not ((self.enemies or self.npcs) and self._lines(q, .45))):
                    return q
        return self.p

    def _cover(self, q):
        if self.main:
            return 11. if not self._line(self.main, q) else 0.
        count = 0
        for yaw in (0., pi * .5, pi, -pi * .5):
            if not self.arena.clear(above(q, self.h * .58), above(offset(q, yaw, 14.), self.h * .58)):
                count += 1
        # Full enclosure is not a vantage point.
        return (0., 7., 11., 8., 0.)[count]

    def _route(self, p, q, *, priority=False):
        if distance(p, q) < .8 and abs(p[2] - q[2]) < 1.:
            return []
        if self.searches >= 2 or perf_counter() > self.stop_work:
            return None
        key = tuple(round(n / 6) for n in (*p, *q))
        if key in self.route_fail:
            return None
        self.searches += 1
        limit = (600 if priority else 250) if self.searches == 1 else 160
        path = self.arena.route(p, q, self.stats, limit=limit, deadline=self.stop_work)
        if path is None:
            self.route_fail[key] = self.t + (.48 if priority else 1.2)
        return path

    def _reachable_loot(self, obj, radius):
        p = tuple(obj['pos'])
        if distance(self.p, p) <= radius - .08 and abs(self.p[2] - p[2]) < 4.:
            return self.p
        q = self._ground(p)
        if abs(q[2] - p[2]) >= 4.:
            return p
        if self.arena.fits(q, self.r, self.h):
            return q
        nearby = [n for n in self.arena.neighbors(q, radius * .75, self.r, self.h) if abs(p[2] - n[2]) < 4.]
        return min(nearby, default=q, key=lambda n: distance(n, self.p))

    def _need_cubes(self):
        now = self.me['cubes']
        cfg = self.cfg['cubes']
        threshold = next((x for x in cfg['upgrade_thresholds'] if x > now), None)
        if now == 0:
            return 27.
        if threshold is not None and threshold - now <= 2:
            return 21. if now < cfg['stat_cap'] else 11.
        return 13. if now < cfg['stat_cap'] else 4.

    def _weapon_value(self, kind):
        near = sum(distance(e['pos'], self.p) < 52 for e in self.enemies)
        laser = any(e.get('charging') or 'laser' in e.get('weapons', ()) for e in self.enemies if distance(e['pos'], self.p) < 60)
        if kind == 'laser':
            return 33. - min(10, near * 5) + 5 * (self.zone.next_radius > 90)
        if kind == 'lightning':
            return 22. + min(8, near * 5) + 10 * laser + 7 * (self.zone.next_radius < 56)
        if kind == 'mortar':
            return 20. + min(5, near * 3) + 3 * (self.zone.next_radius < 45)
        return 0.

    def _pickup_value(self, obj):
        weapons = self.me['weapons']
        kinds = {w['kind'] for w in weapons if w}
        kind = obj['kind']
        if kind == 'gun' or kind in kinds:
            return 0.
        value = self._weapon_value(kind)
        if None in weapons:
            return value + 8.
        old = next(w for w in weapons if w['kind'] != 'gun')
        gain = value - self._weapon_value(old['kind'])
        return gain if gain >= 7. and not old.get('charge', 0.) else 0.

    def _loot(self, health):
        result = []
        cube = self._need_cubes()
        for tag, obj, seen in self.items.values():
            d = distance(self.p, obj['pos'])
            if d > (110 if health > .55 else 58):
                continue
            value = cube if tag == 'cubes' else self._pickup_value(obj)
            if not value:
                continue
            radius = self.cfg['cubes' if tag == 'cubes' else 'loot']['pickup_radius']
            p = self._reachable_loot(obj, radius)
            result.append(Intent(tag, p, value - (self.t - seen) * .75, obj))
        drops = self.cfg.get('drops', {})
        repair = (self.me['max_hp'] - self.me['hp']) / self.me['max_hp']
        for drop in self.obs['drops']:
            ident = drop['id']
            if ident in self.drop_abort:
                continue
            p = self._ground(drop['pos'])
            d = distance(self.p, p)
            value = cube * drops.get('cubes', 3) * .73 + 55 * repair + 13 * (1 - self.me['energy'] / self.me['max_energy'])
            if d > 185 or value < 19.:
                continue
            near_ring = [e for e in self.enemies if distance(e['pos'], p) <= drop['radius'] + 1.]
            if near_ring or drop['holder'] not in (None, self.me['id']):
                # To steal a nearly won drop, first clear a lone, already vulnerable holder from cover.
                if len(near_ring) == 1 and not self.unsafe and near_ring[0].get('hp', float('inf')) < self.me['hp'] * .22 and d > drop['radius']:
                    enemy = near_ring[0]
                    if self._shot_line(enemy) and value >= 38. and len(self._lines(p, 2.)) < 2 and self._cover(self.p) > 0:
                        result.append(Intent('contest', self.p, value * .58, enemy, acquire=1.8))
                continue
            if drop['lands_in'] > d / self.speed + 1.0:
                if drop['lands_in'] > d / self.speed + 9.:
                    continue
                samples = self.arena.neighbors(p, drop['radius'] + 9., self.r, self.h)
                if not samples:
                    continue
                q = min(samples, key=lambda n: distance(n, self.p) - self._cover(n) * 1.8)
                result.append(Intent('stage', q, value * .74, drop, wait=drop['lands_in']))
                continue
            progress = drop['progress'] if drop['holder'] == self.me['id'] else 0.
            options = [p, *self.arena.neighbors(p, drop['radius'] * .63, self.r, self.h)]
            if d < drop['radius'] - 1. and abs(self.p[2] - p[2]) < 4.:
                options.append(self.p)
            options = [q for q in options if self.arena.fits(q, self.r, self.h)]
            if options:
                q = min(options, key=lambda n: distance(n, self.p) + 15 * len(self._lines(n, 1.6)) - self._cover(n))
                result.append(Intent('drop', q, value + 15 * progress, drop, drop['lands_in'], max(0., drop['capture_s'] - progress)))
        if not self.dangers and health > .77 and self.obs['alive'] > 3:
            gun = next((w for w in self.me['weapons'] if w and w['kind'] == 'gun'), None)
            if gun:
                dmg = gun['stats']['damage'] * (1 + self.cfg['cubes']['damage_per_cube'] * min(self.me['cubes'], self.cfg['cubes']['stat_cap']))
                for crate in self.obs['crates']:
                    d = distance(self.p, crate['pos'])
                    if d <= 70 and self._shot_line(crate):
                        aim_pos = self.p if d > 5 else self._ground(offset(self.p, angle(crate['pos'], self.p), 5.))
                        result.append(Intent('farm', aim_pos, cube + (8. if None in self.me['weapons'] else 0.) - d * .12,
                                             crate, acquire=gun['stats']['cooldown'] * (crate.get('hp', 300) / dmg + 1)))
                if not result and health > .9:
                    for npc in self.npcs:
                        if 48 < distance(self.p, npc['pos']) < 77 and self._shot_line(npc):
                            result.append(Intent('farm', self.p, cube * 1.2, npc, acquire=2.7))
        return result

    def _exit(self, p, elapsed, q):
        """Price a reachable exit; never certify cover from an unchecked straight line."""
        candidates = self.arena.neighbors(p, 10., self.r, self.h)
        if self._cover(p) and self.zone.margin(p, elapsed + 3.) >= 0:
            candidates.append(p)
        candidates.sort(key=lambda n: self.zone.margin(n, elapsed + distance(p, n) / self.speed + 3.), reverse=True)
        for n in candidates:
            if perf_counter() > self.stop_work:
                break
            eta = distance(p, n) / self.speed
            if self.zone.margin(n, elapsed + eta + 3.) < 0 or len(self._lines(n, .45)) > 1:
                continue
            if self._walk(p, n) and (self._cover(n) > 0 or not self._lines(q, 1.2)):
                return eta
        return None

    def _plan_loot(self, health):
        scored = []
        for order, g in enumerate(self._loot(health)):
            eta = distance(self.p, g.p) / self.speed
            score = g.reward - max(eta, g.wait) * 1.7 - g.acquire * 2.1
            if self.intent and g.key == self.intent.key:
                score += 6.
            scored.append((score, order, g))
        scored.sort(key=lambda row: (-row[0], row[1]))
        best, best_score = None, 1.
        for score, _, g in scored[:7]:
            if score <= best_score or perf_counter() > self.stop_work:
                break
            eta = distance(self.p, g.p) / self.speed
            if self.zone.margin(g.p, max(eta, g.wait) + g.acquire + 3.) < 0:
                continue
            lines = self._lines(g.p, min(4., max(eta, g.wait) + g.acquire))
            if len(lines) > 1 or (g.kind in ('farm', 'stage') and lines) or (lines and g.reward < 32.):
                continue
            previous = self.intent
            if previous and g.key == previous.key and distance(g.p, previous.p) < 2.8 and self.stalled < 1.4 and previous.path:
                path = previous.path
                g.p = previous.p
                g.roof = previous.roof
            elif abs(self.arena.ground(g.p[0], g.p[1]) - g.p[2]) > 4. and abs(self.p[2] - g.p[2]) > 4.:
                if score < 15. or lines or self.dangers or self.next_margin < 6.:
                    continue
                roof = self.arena.rooftop(self.p, g.p, self.stats, max(0., self.me['energy'] - self.reserve), deadline=self.stop_work)
                if roof is None:
                    continue
                g.roof = roof
                path = self._route(self.p, roof[0])
            else:
                path = self._route(self.p, g.p)
            if path is None:
                continue
            end, harm = self.zone.travel(self.p, path, self.speed, slack=3.)
            hold = max(end, g.wait) + g.acquire
            if g.roof:
                launch, top, landing = g.roof
                hold += 2.2 + (top[2] - launch[2]) / max(4., self.stats['max_rise_speed'] * .65)
                hold += distance(top, landing) / self.speed + 1.
                exit_eta = 0. if self.zone.margin(landing, hold + 3.) >= 0 and not self._lines(landing, .45) else None
            else:
                end_point = path[-1] if path else self.p
                arrival = self._lines(end_point, .65)
                approach = self._lines(path[len(path) // 2], .6) if len(path) > 2 else self.dangers
                exit_eta = self._exit(end_point, hold, g.p) if len({e['id'] for e in (*arrival, *approach)}) < 2 else None
            if not harm and exit_eta is not None and self.zone.margin(g.p, hold + 3.) >= 0:
                actual = score - 1.7 * max(0., hold - max(eta, g.wait) - g.acquire) - .55 * exit_eta - 9. * len(lines)
                if actual > best_score:
                    g.path = path
                    best, best_score = g, actual
        return best

    def _route_intent(self, kind, p, item=None):
        if self.intent and self.intent.kind == kind and distance(p, self.intent.p) < 3. and self.intent.path and self.stalled < 1.4:
            return self.intent
        return Intent(kind, p, item=item, path=self._route(self.p, p, priority=kind == 'rotate'))

    def _escape_intent(self):
        single_line = len(self.dangers) == 1 and self.main is not None
        old = self.intent
        if old and old.detour:
            if single_line and old.path and self.stalled < 1.4 and self._safe_escape_path(old.path):
                return old
            self.anchor_key = self.intent = None
        goal = self._anchor('escape')
        if not single_line or distance(self.p, goal) >= 1.4:
            return self._route_intent('escape', goal)

        source = self.main['pos']
        yaw = angle(source, self.p) if self.next_margin >= 8 else angle(self.p, self.zone.next)
        exits = [self._ground(offset(self.p, yaw, 17.)), *self.arena.neighbors(self.p, 15., self.r, self.h)]
        covered = []
        for q in exits:
            if perf_counter() >= self.stop_work:
                break
            d = distance(self.p, q)
            if (self.zone.margin(q, d / self.speed + 3.) >= 0 and self.arena.fits(q, self.r, self.h)
                    and not self._line(self.main, q) and not self._lines(q, .45)):
                m = min(self.zone.next_margin(q), 33., self.zone.next_radius * .48)
                covered.append((.36 * m + .85 * min(d, 15.) + .58 * distance(source, q) - .12 * d, q))
        covered.sort(reverse=True)
        for _, q in covered[:2]:
            path = self._route(self.p, q)
            if path and self._safe_escape_path(path):
                return Intent('escape', q, path=path, detour=True)
        return self._route_intent('escape', goal)

    def _safe_escape_path(self, path):
        end = path[-1]
        eta, harm = self.zone.travel(self.p, path, self.speed, slack=3.)
        if (eta > 3. or harm or self.zone.margin(end, eta + 3.) < 0
                or self._line(self.main, end) or self._lines(end, .45)):
            return False
        source = self.main['pos']
        closest_allowed = max(self.r * 2, distance(self.p, source) - self.r)
        a = self.p
        for b in path:
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = hypot(dx, dy)
            if length:
                f = max(0., min(1., ((source[0] - a[0]) * dx + (source[1] - a[1]) * dy) / length**2))
                if distance((a[0] + f * dx, a[1] + f * dy), source) < closest_allowed:
                    return False
                samples = ceil(length / 4.)
                for k in range(1, samples):
                    if perf_counter() >= self.stop_work:
                        return False
                    f = k / samples
                    q = a[0] + f * dx, a[1] + f * dy, a[2] + f * (b[2] - a[2])
                    if any(e['id'] != self.main['id'] for e in self._lines(q, .45)):
                        return False
            if any(e['id'] != self.main['id'] for e in self._lines(b, .45)):
                return False
            a = b
        return True

    def _advantage(self, foe):
        hp = foe.get('hp')
        value = 0. if hp is None else (self.me['hp'] - hp) / self.me['max_hp']
        if self.obs['alive'] == 2:
            our_deadline = min(10., self.zone.deadline(self.p))
            their_deadline = min(10., self.zone.deadline(foe['pos']))
            value += (our_deadline - their_deadline) * .015
        d = distance(self.p, foe['pos'])
        own = [w for w in self.me['weapons'] if w is not None]
        theirs = foe.get('weapons', ())
        for w in own:
            if w['cooldown'] < .25:
                if w['kind'] == 'laser' and 50 < d < w['stats']['range'] or w['kind'] == 'lightning' and d < w['stats']['range']:
                    value += .12
                elif w['kind'] == 'gun' and w.get('ammo', 0) > 2 and not w.get('reload', 0):
                    value += .06
        if foe.get('charging') and not any(w['kind'] == 'lightning' and d < w['stats']['range'] for w in own):
            value -= .15
        if 'lightning' in theirs and d < self.cfg['weapons']['lightning']['range']:
            value -= .08
        if self.me['energy'] < self.reserve:
            value -= .08
        return value

    def _choose(self, health, duel, ahead):
        old = self.intent
        if old and old.kind == 'drop':
            drop = next((d for d in self.obs['drops'] if d['id'] == old.item['id']), None)
            if drop:
                contested = any(distance(e['pos'], drop['pos']) <= drop['radius'] for e in self.enemies)
                danger = len(self._lines(old.p, 2.)) >= 2
                if contested or danger or self.pinched:
                    self.drop_abort[drop['id']] = self.t + 5.
                    if self.margin >= 0 and not self.rush:
                        self.intent = self._escape_intent()
                        return
        if old and old.roof and self.climbing and not (self.rush or self.unsafe):
            return
        if self.pinched:
            self.cover_run = None
            self.intent = self._route_intent('rotate', self._anchor('rotate'))
            return
        if self.unsafe:
            run = self.cover_run
            if run and not self.rush:
                run.foe = next((e for e in self.enemies if e['id'] == run.foe['id']), run.foe)
                if self._cover_home_safe(run) and (run.phase != 'return' or self.t < run.until):
                    self._cover_return(run)
                    return
            self.cover_run = None
            self.cover_retry = self.t + .65
            self.intent = self._escape_intent()
            return
        if self.cover_run:
            if self.rush:
                self.cover_run = None
            elif self._follow_cover():
                return
        if old and old.kind == 'pursue' and self.pursuit and self.t < self.pursuit[1]:
            if distance(self.p, old.p) > 2. and self.stalled < 1.2 and self.zone.margin(old.p, 3.) >= 0:
                return
        if old and old.kind == 'escape' and self.t - self.last_damage < 1.5 and len(self.dangers) == 0:
            self.intent = self._route_intent('heal', self._anchor('heal'))
            return
        # Reconsider distant loot only a few times per second. Danger and zone checks above are per tick.
        if old and old.kind in ('cubes', 'pickups', 'drop', 'stage', 'farm', 'contest') and self.t < self.replan:
            present = (old.item['id'] in self.items if old.kind in ('cubes', 'pickups') else
                       any(e['id'] == old.item['id'] for e in (self.obs['drops'] if old.kind in ('drop', 'stage') else self.obs['crates'] + self.obs['mechs'])))
            if present and self.stalled < 1.4 and self.zone.margin(old.p, 3.) >= 0 and len(self._lines(old.p, 1.6)) < 2:
                return
        healthy_reset = health < .78 and not self.dangers and self.t - self.last_combat < self.stats['regen_delay'] + 3.
        late = self.obs['alive'] <= 3 and self.zone.next_radius <= 70.
        if self.t >= self.replan or old is None or (old.kind in ('cubes', 'pickups') and old.item['id'] not in self.items):
            self.replan = self.t + .36
            loot = self._plan_loot(health) if not late or health < .73 or None in self.me['weapons'] else None
            if loot and (not healthy_reset or loot.kind not in ('farm', 'contest')):
                self.intent = loot
                return
        if healthy_reset:
            self.intent = self._route_intent('heal', self._anchor('heal'))
            return
        if duel and self.main and not ahead and self._advantage(self.main) < -.12:
            self.intent = self._route_intent('reangle', self._anchor('duel'))
            return
        if self._start_cover():
            return
        if late or duel:
            self.intent = self._route_intent('hold', self._anchor('hold'))
            return
        if self._commit_flank():
            return
        self.intent = self._route_intent('search', self._anchor('search'))

    def _shot_line(self, actor):
        height = (2.0 if actor.get('kind') == 'npc' else .9 if actor['id'] >= 1000 else 4.5)
        return self.arena.clear(above(self.p, self.stats['muzzle_height']), above(actor['pos'], height))

    def _defensive_target(self):
        gun, secondary = self.me['weapons']
        if (not self.enemies or not gun or gun['kind'] != 'gun' or gun.get('ammo', 0) <= 0
                or gun.get('reload', 0.) > 0 or gun['cooldown'] > self.weapons.window + 1e-8
                or (secondary and secondary.get('charge', 0.) > 0.)):
            return None

        line, candidate = None, None
        x, y, z = above(self.p, self.h * .57)
        muzzle = self.cfg['mech']['muzzle_height']
        size = self.cfg['cubes']['size_per_cube']
        for foe in self.enemies:
            fx, fy, fz = foe['pos']
            dx, dy = x - fx, y - fy
            d = hypot(dx, dy)
            yaw, pitch = foe['aim']
            if turn(yaw, atan2(dy, dx)) > pi / 12:
                continue
            kinds = foe.get('weapons', ())
            origin = fx, fy, fz + muzzle * (1. + size * foe.get('cubes', 0))
            direct = (abs(pitch - atan2(z - origin[2], d)) <= pi / 12
                      and any(d <= self.direct_threat_reach.get(kind, 0.) for kind in kinds))
            # Veto crossfire at the other weapon's range, not our engagement cap.
            if not direct and not ('mortar' in kinds and d <= self.mortar_reach):
                continue
            if not self.arena.clear(origin, (x, y, z)):
                continue
            if line is not None:
                return None
            line = foe
            vx, vy, _ = foe['vel']
            if (direct and 120. <= d < 160.
                    and (vx * dx + vy * dy >= 4. * d or (foe.get('charging') and 'laser' in kinds))):
                candidate = foe
        if candidate is not None and (not self.dangers or self.dangers[0]['id'] == candidate['id']):
            return candidate
        return None

    def _cover_center(self, foe, lead=0.):
        x, y, z = foe['pos']
        vx, vy, vz = foe.get('vel', (0., 0., 0.))
        height = self.cfg['mech']['height'] * (1 + self.cfg['cubes']['size_per_cube'] * foe.get('cubes', 0))
        return x + vx * lead, y + vy * lead, z + vz * lead + height * .5

    def _cover_ray(self, p, foe, lead=0.):
        return self.arena.clear(above(p, self.stats['muzzle_height']), self._cover_center(foe, lead))

    def _cover_occluded(self, p, foe):
        # No favorable facing, distance, or foliage can substitute for solid occlusion.
        return (not self._line(foe, p) and not self._line(foe, p, body_height=.85)
                and not self._cover_ray(p, foe))

    def _cover_aim(self, foe):
        x, y, z = self._cover_center(foe, self.weapons.window)
        dx, dy = x - self.p[0], y - self.p[1]
        return atan2(dy, dx), atan2(z - self.p[2] - self.stats['muzzle_height'], hypot(dx, dy))

    def _cover_crossfire(self, p, target):
        if any(e['id'] != target for e in self._lines(p, .45)):
            return True
        # _lines is the placement threat model, not a cap on hostile weapon reach.
        for foe in self.enemies:
            if foe['id'] == target:
                continue
            d = distance(foe['pos'], p)
            if d <= 115. or (foe['id'] not in self.attackers
                             and turn(foe['aim'][0], angle(foe['pos'], p)) >= 1.15):
                continue
            if any(d <= (self.mortar_reach if k == 'mortar' else self.direct_threat_reach.get(k, 0.))
                   for k in foe.get('weapons', ())) and self._line(foe, p):
                return True
        return False

    def _cover_rushed(self, p, foe):
        dx, dy = p[0] - foe['pos'][0], p[1] - foe['pos'][1]
        d = hypot(dx, dy)
        vx, vy, _ = foe.get('vel', (0., 0., 0.))
        closing = (dx * vx + dy * vy) / max(1., d)
        return d < 22. or (closing > 4. and d - closing * 1.6 < 22.)

    def _cover_wait(self, p, foe):
        x, y, z = self._cover_center(foe)
        d = hypot(x - p[0], y - p[1], z - p[2] - self.stats['muzzle_height'])
        wait = float('inf')
        for w in self.me['weapons']:
            if not w:
                continue
            stats, kind, cooldown = w['stats'], w['kind'], w['cooldown']
            reach = stats['range'] if kind != 'mortar' else stats['speed'] ** 2 / stats['gravity']
            if d >= reach:
                continue
            if kind == 'gun':
                reload = w.get('reload', 0.)
                if w.get('ammo', 0) <= 0 and not reload:
                    continue
                cooldown = max(cooldown, reload)
            elif kind == 'laser':
                if self.t < self.weapons.short_until:
                    continue
            elif kind == 'lightning' and self.me['energy'] - self.reserve < stats['energy']:
                continue
            wait = min(wait, cooldown)
        return wait

    def _cover_lane(self, a, b, target, deadline):
        if perf_counter() >= deadline:
            return False
        steps = max(1, ceil(distance(a, b) / 3.5))
        for i in range(1, steps):
            if perf_counter() >= deadline:
                return False
            f = i / steps
            p = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
            if self._cover_crossfire(p, target):
                return False
        return True

    def _find_cover_pair(self, foe, exposed):
        # Only use a short, reversible walking leg; don't spend navigation searches on a peek.
        deadline = min(self.stop_work, perf_counter() + .0035)
        target = foe['id']
        yaw = angle(self.p, foe['pos'])
        previous = self.cover_history
        if previous and (previous[0] != target or self.t - previous[2] > 8.):
            previous = None
        same_edge = None
        for reach in (6., 10., 14.):
            for i in (2, -2, 3, -3, 1, -1, 4, -4, 0, 5, -5, 6):
                if perf_counter() >= deadline:
                    return same_edge
                q = self._ground(offset(self.p, yaw + i * pi / 6, reach))
                home, edge = (q, self.p) if exposed else (self.p, q)
                wait = self._cover_wait(edge, foe)
                if wait > 1.8 or self._cover_rushed(edge, foe):
                    continue
                # Include staging, both walking legs, a burst and some settling time.
                horizon = max(wait, reach / self.speed if exposed else 0.) + 2. * reach / self.speed + 2.
                if min(self.zone.margin(home, horizon), self.zone.margin(edge, horizon)) < self.r + 1.:
                    continue
                if not self.arena.fits(q, self.r, self.h):
                    continue
                if exposed:
                    inside_home = self._ground(offset(home, angle(home, edge), 1.))
                    if not self._cover_occluded(home, foe) or not self._cover_occluded(inside_home, foe):
                        continue
                elif not self._cover_ray(edge, foe):
                    continue
                # The walking controller stops short of the exact waypoint.
                inside = self._ground(offset(edge, angle(edge, home), 1.))
                if (not self._cover_ray(inside, foe, .25) or self._cover_crossfire(q, target)
                        or not self._walk(home, edge) or not self._walk(edge, home)
                        or not self._cover_lane(home, edge, target, deadline)):
                    continue
                if previous is None or distance(previous[1], edge) >= max(5., self.r * 2):
                    return home, edge
                if same_edge is None:
                    same_edge = home, edge
        return same_edge

    def _start_cover(self):
        if (self.rush or self.obs['alive'] <= 1 or not self.me['grounded'] or not self.enemies
                or self.t < self.cover_retry or any(w and w.get('charge', 0.) for w in self.me['weapons'])):
            return False
        self.cover_retry = self.t + .55
        exposed = len(self.dangers) == 1
        if exposed:
            foe = self.main
            if not foe or foe['kind'] != 'mech':
                return False
        else:
            if self.dangers:
                return False
            foe = min(self.enemies, key=lambda e: (e['id'] not in self.attackers, distance(self.p, e['pos'])))
        if (distance(self.p, foe['pos']) > 112. or self._cover_rushed(self.p, foe)
                or not (self._cover_ray(self.p, foe) if exposed else self._cover_occluded(self.p, foe))
                or self._cover_crossfire(self.p, foe['id'])):
            return False
        pair = self._find_cover_pair(foe, exposed)
        if pair is None:
            return False
        home, edge = pair
        self.cover_run = CoverRun(foe, home, edge, 'stage', self.t, self.t + 2.1 + distance(self.p, home) / self.speed)
        return self._follow_cover()

    def _cover_home_safe(self, run):
        back = distance(self.p, run.home)
        return (self.me['grounded'] and back < 16.
                and min(self.zone.margin(run.home), self.zone.margin(run.home, back / self.speed * 1.5 + 1.)) >= self.r
                and self._cover_occluded(run.home, run.foe)
                and not self._cover_crossfire(run.home, run.foe['id'])
                and self._walk(self.p, run.home))

    def _cover_phase(self, run, phase, seconds):
        run.phase, run.since, run.until = phase, self.t, self.t + seconds

    def _cover_return(self, run):
        if run.phase != 'return':
            if run.phase in ('out', 'fire'):
                self.cover_history = run.foe['id'], run.edge, self.t
            self._cover_phase(run, 'return', .65 + 1.65 * distance(self.p, run.home) / self.speed)
        self.intent = Intent('cover', run.home)
        return True

    def _cover_abort(self, escape=False):
        self.cover_run = None
        self.cover_retry = self.t + .7
        if escape:
            self.intent = self._escape_intent()
        return escape

    def _follow_cover(self):
        run = self.cover_run
        target = run.foe['id']
        foe = next((e for e in self.enemies if e['id'] == target), None)
        if foe is not None:
            run.foe = foe
        back = distance(self.p, run.home)
        if not self._cover_home_safe(run):
            return self._cover_abort(escape=bool(self.dangers) or self._cover_crossfire(self.p, target))
        if run.phase == 'return':
            if back <= .8 and self._cover_occluded(self.p, run.foe):
                self._cover_phase(run, 'reset', .35)
            elif (back <= .65 or self.t >= run.until
                  or (self.t - run.since > .65 and self.stalled > .65)):
                return self._cover_abort(escape=bool(self.dangers) or self._cover_crossfire(self.p, target))
            else:
                return self._cover_return(run)
        if run.phase == 'reset':
            if back > .8 or not self._cover_occluded(self.p, run.foe):
                return self._cover_return(run)
            if self.t >= run.until or foe is None or self._cover_rushed(run.home, foe):
                self.cover_run = None
                return False
            self.intent = Intent('cover', run.home)
            return True
        if (foe is None or any(self._cover_rushed(p, foe) for p in (self.p, run.edge, run.home))
                or self._cover_crossfire(self.p, target)):
            return self._cover_return(run)
        if self.t >= run.until:
            return self._cover_return(run) if run.phase != 'stage' else self._cover_abort()

        walk = distance(self.p, run.edge)
        loop = (walk + distance(run.edge, run.home)) / self.speed
        if (min(self.zone.margin(run.edge, loop + 2.), self.zone.margin(run.home, loop + 2.)) < self.r + 1.
                or not self._cover_ray(run.edge, foe, .25) or self._cover_crossfire(run.edge, target)
                or not self._walk(run.edge, run.home)
                or not self._cover_lane(run.home, run.edge, target, self.stop_work)):
            return self._cover_return(run)
        if run.phase == 'stage':
            if back > .8:
                if self.t - run.since > .65 and self.stalled > .65:
                    return self._cover_abort(escape=bool(self.dangers))
            elif self._cover_occluded(self.p, foe):
                yaw, pitch = self._cover_aim(foe)
                if (self._cover_wait(run.edge, foe) <= self.weapons.window + 1e-8
                        and turn(self.me['aim'][0], yaw) < .48 and abs(self.me['aim'][1] - pitch) < .3):
                    self._cover_phase(run, 'out', .55 + 1.55 * walk / self.speed)
            elif back <= .65:
                return self._cover_abort(escape=bool(self.dangers))
            if run.phase == 'stage':
                self.intent = Intent('cover', run.home)
                return True
        if run.phase == 'out':
            if walk <= .8 and self._cover_ray(self.p, foe):
                self._cover_phase(run, 'fire', .6)
            elif (not self._walk(self.p, run.edge) or (self.t - run.since > .65 and self.stalled > .65)
                  or not self._cover_lane(self.p, run.edge, target, self.stop_work)):
                return self._cover_return(run)
            else:
                self.intent = Intent('cover', run.edge)
                return True
        laser = next((w for w in self.me['weapons'] if w and w['kind'] == 'laser'), None)
        if ((run.laser and laser and laser['cooldown'] > self.weapons.window and not laser.get('charge', 0.))
                or (not (laser and laser.get('charge', 0.)) and self._cover_wait(self.p, foe) > self.weapons.window + 1e-8)
                or not self._cover_ray(self.p, foe)):
            return self._cover_return(run)
        self.intent = Intent('cover', self.p)
        return True

    def _cover_trigger(self, fire):
        run = self.cover_run
        instant = charging = False
        for w, trigger in zip(self.me['weapons'], fire):
            if not w:
                continue
            if w['kind'] == 'laser':
                charging = trigger or bool(w.get('charge', 0.))
                if trigger and not run.laser:
                    run.laser = True
                    run.until = min(run.since + 1.25, self.t + w['stats']['charge'] + .2)
            else:
                instant |= trigger
        # A laser trigger is only a charge; cooldown in the next observation confirms its release.
        if instant and not charging:
            self._cover_return(run)

    def _commit_flank(self):
        if self.intent and self.intent.kind == 'flank' and self.t - self.flanked_at < 2.8:
            if self.stalled < 1.4 and len(self.dangers) < 2 and self.zone.margin(self.intent.p, 4.) > 0:
                return True
        foe = self.main
        if not foe or len(self.dangers) != 1 or foe['kind'] != 'mech' or self.t - self.flanked_at < 3.5:
            return False
        pressing = foe['id'] in self.attackers or self.next_margin < 12.
        if not pressing or distance(self.p, foe['pos']) > 115 or self._advantage(foe) < -.18:
            return False
        old = angle(foe['pos'], self.shot_origin if self.shot_target and self.shot_target['id'] == foe['id'] else self.p)
        options = []
        for q in self.arena.neighbors(self.p, 12., self.r, self.h):
            yaw = angle(foe['pos'], q)
            if self.zone.margin(q, 4.) < 0 or len(self._lines(q, 1.)) > 1 or not self._walk(self.p, q):
                continue
            if self._line(foe, q) and not self.arena.clear(above(self.p, self.h / 2), above(offset(self.p, angle(self.p, q), 18.), self.h / 2)):
                continue
            options.append((turn(yaw, foe['aim'][0]) + turn(yaw, old) + .4 * self._cover(q), q))
        if not options:
            return False
        q = max(options)[1]
        self.intent = self._route_intent('flank', q, foe)
        self.flanked_at = self.t
        return True

    def _target(self, duel, ahead):
        g = self.intent
        if g.kind == 'cover':
            run = self.cover_run
            return self._cover_aim(run.foe), run.foe if run.phase == 'fire' else None
        loud = next((s for s in self.obs['sounds'] if s['kind'] in ('laser_charge', 'boost', 'lightning', 'jet', 'gun') and s['dist'] == 'near'), None)
        if self.main:
            fallback = angle(self.p, self.main['pos']), 0.
        elif self.threat_bearing is not None and self.t - self.last_damage < 1.6:
            fallback = self.threat_bearing, 0.
        elif loud:
            fallback = loud['bearing'], 0.
        elif distance(self.p, g.p) > 1.:
            fallback = angle(self.p, g.p) + (.64 * sin(self.t * 1.4) if g.kind == 'search' else 0.), 0.
        else:
            scan = .35 if g.kind == 'search' else .22 if g.kind == 'hold' else 0.
            fallback = self.me['aim'][0] + scan, -.05
        if g.kind in ('farm', 'contest') and not self.unsafe and not self.rush:
            pool = self.npcs if g.item['id'] < 1000 and g.kind == 'farm' else self.enemies if g.kind == 'contest' else self.obs['crates']
            actor = next((e for e in pool if e['id'] == g.item['id']), None)
            if actor and self._shot_line(actor):
                return fallback, actor
        if self.obs['alive'] <= 1 or (g.kind == 'heal' and not self.dangers):
            return fallback, None
        if g.kind == 'flank' and distance(self.p, g.p) > 3. and self.t - self.last_damage > .45:
            return fallback, None
        best, value = None, -float('inf')
        hp = self.me['hp']
        lob = any(w and w['kind'] == 'mortar' and w['cooldown'] < .12 for w in self.me['weapons'])
        for foe in self.enemies + [e for e in self.dangers if e.get('kind') == 'npc']:
            d = distance(self.p, foe['pos'])
            if d > max(200., self.cfg['vision']['range']):
                continue
            foe_hp = foe.get('hp', float('inf'))
            recent = foe['id'] in self.attackers
            exit_block = g.kind in ('rotate', 'escape') and d < 64 and turn(angle(self.p, foe['pos']), angle(self.p, g.p)) < .8
            loot_block = g.kind in ('pickups', 'cubes', 'drop', 'stage') and distance(g.p, foe['pos']) < 25.
            threat = recent or exit_block or loot_block or (d < 40. and foe is self.main) or (g.kind == 'flank' and g.item['id'] == foe['id'])
            distraction = turn(foe['aim'][0], angle(foe['pos'], self.p)) > 1.35
            cheap = (foe_hp < min(210., hp * .24) and d < 110 and (distraction or foe is self.main)
                     and not self.unsafe and not self.pinched and (g.kind not in ('cubes', 'pickups', 'drop', 'stage') or threat)
                     and (self.obs['alive'] > 3 or self._cover(self.p) > 0))
            opportunity = g.kind == 'search' and foe is self.main and distraction and self._advantage(foe) >= .08 and d < 105
            if not (threat or cheap or opportunity or (duel and (ahead or self.pinched or d < 80))):
                continue
            if self.unsafe and not (recent and d < 72 or exit_block) or self.rush and not (recent or exit_block):
                continue
            if not self._shot_line(foe):
                if recent and self.t - self.shot_at < 1.2 and self.shot_target and foe['id'] == self.shot_target['id'] and foe_hp < hp * .23:
                    self._pursue(foe, g, duel)
                if not (lob and recent and not self.unsafe and not self.pinched and d > 20.):
                    continue
            rank = 38 * recent + 28 * exit_block + 17 * loot_block + 24 * cheap + 10 * distraction + 12 * (foe is self.main) - d * .19
            if rank > value:
                best, value = foe, rank
        return fallback, best

    def _pursue(self, foe, g, duel):
        if self.unsafe or self.pinched or len(self.dangers) > 1 or g.kind in ('heal', 'escape', 'drop'):
            return
        if not (duel or g.kind in ('rotate', 'flank', 'pickups', 'cubes') and distance(foe['pos'], g.p) < 26.):
            return
        lock = self.pursuit
        if lock and lock[0] == foe['id']:
            if self.t <= lock[1] and self.intent.kind == 'pursue':
                return
            if self.t < lock[1] + 4.:
                return
        for q in sorted(self.arena.neighbors(self.p, 9., self.r, self.h), key=lambda n: distance(n, foe['pos'])):
            if self.zone.margin(q, 4.) >= 0 and self._walk(self.p, q) and self._line(foe, q) and len(self._lines(q, 1.)) < 2:
                self.pursuit = foe['id'], self.t + 1.35
                self.intent = Intent('pursue', q, item=foe, path=[q])
                break

    def _projectile_risk(self, move):
        danger = 0.
        for shot in self.obs['projectiles'][:14]:
            p, v = shot['pos'], shot['vel']
            if distance(p, self.p) > (62 if shot['kind'] == 'mortar' else 115):
                continue
            dx, dy = p[0] - self.p[0], p[1] - self.p[1]
            vx, vy = v[0] - move[0] * self.speed, v[1] - move[1] * self.speed
            t = -(dx * vx + dy * vy) / max(1., vx * vx + vy * vy)
            if not .04 < t < .55:
                continue
            shell = shot['kind'] == 'mortar'
            z = p[2] + v[2] * t - (.5 * self.cfg['weapons']['mortar']['gravity'] * t * t if shell else 0.)
            r = self.r + (5. if shell else .45)
            if self.p[2] - (4. if shell else .4) < z < self.p[2] + self.h + .4 and hypot(dx + vx * t, dy + vy * t) < r:
                danger += 2. if shell else 1.
        return danger

    def _move(self, target, critical):
        g = self.intent
        if g.roof:
            launch, top, _ = g.roof
            horizontal = distance(self.p, top)
            if self.climbing and horizontal < 1.35:
                if self.me['grounded'] and abs(self.p[2] - top[2]) < 1.6:
                    self.climbing = False
                    g.path = g.roof = None
                return direction(self.p, g.p, 5.), False, False
            if self.climbing and self.me['energy'] > self.reserve * .45:
                if self.p[2] >= top[2] + .35:
                    return direction(self.p, top, 5.), True, False
                return [0., 0.], True, self.me['grounded']
            if distance(self.p, launch) < 1.25 and not critical and self.me['energy'] > self.reserve:
                if hypot(*self.me['vel'][:2]) > 3.:
                    return [0., 0.], False, False
                self.climbing = True
                return [0., 0.], True, self.me['grounded']
        else:
            self.climbing = False
        target_point = g.p
        if g.path:
            while len(g.path) > 1 and distance(self.p, g.path[0]) < 1.8 and abs(self.p[2] - g.path[0][2]) < 2.:
                g.path.pop(0)
            target_point = g.path[0]
        elevated = self.p[2] > self.arena.ground(self.p[0], self.p[1]) + 3.2
        if elevated and (not g.roof or critical) and (not g.path or target_point[2] < self.p[2] - 3.):
            landing = self.arena.descent(self.p, g.p, self.stats, deadline=self.stop_work)
            if landing:
                return direction(self.p, landing), False, False
        if g.kind == 'drop' and distance(self.p, g.p) < 1.15:
            base = [0., 0.]
        elif distance(self.p, target_point) < (.65 if g.kind == 'cover' else .9):
            base = [0., 0.]
        else:
            base = direction(self.p, target_point)
        room = self.me['energy'] - self.reserve
        boost = g.kind != 'cover' and self.me['grounded'] and hypot(*base) > .7 and (
            critical and self.me['energy'] > (3. if self.rush else 10.)
            or room > 18. and (g.kind == 'rotate' or g.kind in ('cubes', 'pickups', 'drop') and distance(self.p, g.p) > 24.)
        )
        # Use real velocity to validate near-term motion while turning or braking.
        def step(v):
            speed = self.stats['boost_speed'] if boost else self.stats['walk_speed']
            vx, vy = self.me['vel'][:2]
            q = (self.p[0] + v[0] * speed * .25 + vx * .08, self.p[1] + v[1] * speed * .25 + vy * .08, self.p[2])
            return q if elevated else self._ground(q)

        base_ok = not any(base) or self._walk(self.p, step(base))
        bad = self._projectile_risk(base) if self.obs['projectiles'] else 0.
        shuffle = bool(target and g.kind in ('hold', 'reangle', 'flank') and self.main and distance(self.p, g.p) < 4. and g.kind != 'flank')
        if base_ok and bad == 0 and not shuffle:
            return base, boost, False
        yaw = angle(self.p, target_point) if any(base) else angle(self.p, target['pos']) if target else self.me['aim'][0]
        options = [(base, step(base), bad)] if base_ok else []
        for a in (yaw + .7, yaw - .7, yaw + 1.4, yaw - 1.4, yaw + 2.4, yaw - 2.4):
            if perf_counter() > self.stop_work and options:
                break
            v = [cos(a), sin(a)]
            q = step(v)
            if g.kind == 'cover':
                run = self.cover_run
                if (not self._walk(q, run.home) or self._cover_crossfire(q, run.foe['id'])
                        or run.phase in ('reset', 'stage') and not self._cover_occluded(q, run.foe)):
                    continue
            if self._walk(self.p, q) and (g.kind != 'drop' or distance(q, g.item['pos']) < g.item['radius'] - .3):
                options.append((v, q, self._projectile_risk(v) if bad else 0.))
        if not options:
            return [0., 0.], False, False
        def score(option):
            _, q, risk = option
            gain = distance(self.p, target_point) - distance(q, target_point)
            margin = self.zone.margin(q, 1.5)
            safe = min(0., margin) * (4. if self.pinched else 2.)
            if shuffle and self.main:
                flank = turn(angle(self.main['pos'], self.p), angle(self.main['pos'], q))
                return gain * .35 + safe + min(1.8, flank * 23.) - risk * 7.
            return gain + safe - risk * 7.
        v, _, _ = max(options, key=score)
        return v, boost and any(v), False

    def _equip(self, action, critical):
        if critical or self.intent.kind != 'pickups' or len(self.dangers) >= 2:
            return
        ident = self.intent.item['id']
        reach = self.cfg['loot']['pickup_radius']
        for item in self.obs['pickups']:
            if item['id'] != ident or distance(item['pos'], self.p) > reach or abs(item['pos'][2] - self.p[2]) > 4 or not self._pickup_value(item):
                continue
            weapons = self.me['weapons']
            if None not in weapons:
                slot = next(i for i, w in enumerate(weapons) if w['kind'] != 'gun')
                if weapons[slot].get('charge', 0) or any(action.get('fire', ())):
                    return
                action['drop'] = slot
            action['take'] = ident
            return

    def _pick(self, offer):
        weights = {'hp': 25., 'armor': 28., 'walk_speed': 22., 'boost_speed': 13., 'energy': 9.,
                   'energy_regen': 11., 'regen_per_s': 12., 'regen_delay': 14., 'turn_rate': 11.,
                   'damage': 18., 'cooldown': 17., 'charge': 15., 'reload': 9., 'range': 7., 'mag': 8.,
                   'hearing': 2., 'boost_drain': 10., 'speed': 10., 'splash_radius': 13.}
        if self.next_margin < 12. or self.stalled > .9:
            weights['walk_speed'] += 18.
            weights['boost_speed'] += 8.
        if self.incoming > .17:
            weights['armor'] += 13.
        weapons = {w['kind']: w['stats'] for w in self.me['weapons'] if w}
        vision = self.cfg['vision']
        additive_base = {'near_range_bonus': vision['near_range'], 'vision_bonus': vision['range']}
        scores = []
        for choice in offer['choices']:
            score = 0.
            for effect in self.upgrades.get(choice['id'], ()):
                category, _, prop = effect['path'].removeprefix('weapons.').partition('.')
                stats = self.stats if category == 'mech' else weapons.get(category)
                if stats is None:
                    continue
                base = max(1., abs(stats.get(prop, 0.) + additive_base.get(prop, 0.)))
                scale = abs(effect['mul'] - 1.) if 'mul' in effect else abs(effect.get('add', 0.)) / base
                if prop == 'armor':
                    scale *= 2.5
                score += scale * weights.get(prop, 4.) * (1. if category == 'mech' or category == 'gun' else .87)
            scores.append(score)
        return max(range(len(scores)), key=scores.__getitem__)


if __name__ == '__main__':
    mechfight.run(Bot)
