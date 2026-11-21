import gzip
import json
import math
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from arena import Arena
from bot import Bot
from weapons import Weapons
from zone import Zone


KIT = Path(os.environ.get('MECH_KIT', Path(__file__).resolve().parents[2] / 'mechfight-kit'))
with gzip.open(KIT / 'check' / 'stream.jsonl.gz', 'rt') as stream:
    HELLO = json.loads(next(stream))
    EXAMPLE = json.loads(next(stream))


def box(x0, x1, y0, y1, base=0., top=18., kind='building'):
    return {'kind': kind, 'ring': [x0, y0, x1, y0, x1, y1, x0, y1], 'holes': [], 'base': base, 'top': top}


def weapon(kind, **fields):
    defaults = {'kind': kind, 'cooldown': 0., 'stats': HELLO['config']['weapons'][kind]}
    if kind == 'gun':
        defaults.update(ammo=12, reload=0.)
    if kind == 'laser':
        defaults['charge'] = 0.
    return {**defaults, **fields}


def obs_at(t=0., pos=(0., 0., 0.), alive=9, aim=(0., 0.)):
    msg = deepcopy(EXAMPLE)
    msg.update(t=t, tick=round(t * 20), alive=alive, offer=None)
    for name in ('beams', 'crates', 'cubes', 'drops', 'events', 'mechs', 'pickups', 'projectiles', 'sounds'):
        msg[name] = []
    msg['self'].update(id=0, pos=pos, vel=[0., 0., 0.], aim=list(aim), hp=1400., max_hp=1400.,
                       energy=100., max_energy=100., in_zone=True, grounded=True, cubes=0,
                       upgrades=[], stats=deepcopy(HELLO['config']['mech']), weapons=[weapon('gun'), None])
    msg['zone'] = {'center': [0., 0.], 'radius': 130., 'next_center': [0., 0.], 'next_radius': 108.,
                   'shrink_start': t + 30., 'shrink_end': t + 50., 'dps': 20.}
    return msg


def foe(ident, x, y, *, hp=900., yaw=math.pi, vel=(0., 0., 0.), charging=False):
    return {'id': ident, 'kind': 'mech', 'pos': [x, y, 0.], 'vel': vel,
            'aim': [yaw, 0.], 'hp': hp, 'max_hp': 1400., 'weapons': ['gun'], 'charging': charging}


def defensive_obs(t=0.):
    o = obs_at(t, pos=(-77., 0., 0.), aim=(0., -.02))
    o['zone'].update(radius=158., next_radius=145.)
    o['mechs'] = [foe(4, 51., 0., vel=(-7., 0., 0.))]
    return o


class CandidateScenarios(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.hello = deepcopy(HELLO)
        self.hello['map']['dir'] = temp.name
        self.data = Path(temp.name) / 'v2'
        self.data.mkdir()
        (self.data / 'terrain.i16').write_bytes(bytes(321 * 321 * 2))

    def write_map(self, *prisms):
        world = {
            'version': 2,
            'bounds': {'minX': -159., 'maxX': 159., 'minY': -159., 'maxY': 159.},
            'terrain': {'file': 'terrain.i16', 'minX': -160., 'minY': -160., 'step': 1., 'nx': 321, 'ny': 321, 'scale': .01},
            'prisms': prisms,
            'trees': {'fields': ['x', 'y', 'z', 'height', 'crownR', 'crownBase', 'trunkR', 'osm', 'crownX', 'crownY'], 'data': []},
            'rules': {'canopyDensity': .35, 'canopyBlockT': .25},
        }
        (self.data / 'world.json').write_text(json.dumps(world))

    def test_loot_beats_a_kill_and_pick_checks_height(self):
        self.write_map()
        bot = Bot(self.hello)
        o = obs_at()
        o['pickups'] = [{'id': 8000, 'kind': 'laser', 'pos': [0., 0., 13.]},
                        {'id': 8001, 'kind': 'lightning', 'pos': [2., 1., 0.]}]
        o['cubes'] = [{'id': 5000, 'pos': [6., 7., 0.]}]
        o['mechs'] = [foe(1, 60., 0., hp=35., yaw=0.)]
        o['offer'] = {'id': 1, 'expires': 5., 'choices': [{'id': 'sensors'}, {'id': 'plating'}, {'id': 'lightning_chain'}]}
        action = bot.act(o)
        self.assertEqual(action.get('take'), 8001)
        self.assertEqual(action.get('pick'), 1)
        # Only the installed gun's magazine upgrade is relevant to this loadout.
        o['t'], o['tick'] = .1, 2
        o['offer'] = {'id': 2, 'expires': 5., 'choices': [{'id': 'sensors'}, {'id': 'laser_damage'}, {'id': 'gun_mag'}]}
        self.assertEqual(bot.act(o)['pick'], 2)
        self.assertFalse(any(action.get('fire', ())))

    def test_drop_holds_uncontested_then_aborts_to_escape(self):
        self.write_map()
        bot = Bot(self.hello)
        o = obs_at(95.)
        o['self']['hp'] = 740.
        o['drops'] = [{'id': 20000, 'pos': [0., 0., 0.], 'radius': 8., 'lands_in': 0., 'capture_s': 4.,
                       'progress': 3.6, 'holder': 0}]
        self.assertEqual(bot.act(o)['move'], [0., 0.])
        o['t'], o['tick'] = 95.1, 1902
        o['mechs'] = [foe(1, 23., 0.), foe(2, 5., 27., yaw=-1.7)]
        action = bot.act(o)
        self.assertGreater(math.hypot(*action['move']), .5)
        self.assertLess(sum(m * e for m, e in zip(action['move'], (23., 0.))), 0.)

    def test_navigation_detours_and_distinguishes_bridge_clearance(self):
        self.write_map(box(-4., 4., -14., 14.), box(30., 42., -10., 10., 5., 7., 'bridge'),
                       box(60., 72., -10., 10., 12., 14., 'bridge'))
        cfg = self.hello['config']['mech']
        arena = Arena(self.hello['map']['dir'], cfg)
        route = arena.route((-20., 0., 0.), (20., 0., 0.), cfg)
        self.assertIsNotNone(route)
        self.assertGreater(max(abs(p[1]) for p in route), 14.)
        self.assertEqual(tuple(route[-1]), (20., 0., 0.))
        walk = lambda a, b: arena.walkable(a, b, cfg['radius'], cfg['height'], cfg['step_up'], cfg['max_slope_deg'])
        self.assertFalse(walk((25., 0., 0.), (48., 0., 0.)))
        self.assertTrue(walk((54., 0., 0.), (78., 0., 0.)))
        self.assertTrue(arena.clear((54., 0., 7.), (78., 0., 7.)))

    def test_roof_pickup_requires_enough_jet_energy_and_a_ground_exit(self):
        self.write_map(box(5., 17., -7., 7., top=10.))
        cfg = self.hello['config']['mech']
        arena = Arena(self.hello['map']['dir'], cfg)
        start, roof = (0., 0., 0.), (9., 0., 10.)
        self.assertIsNone(arena.rooftop(start, roof, cfg, 12.))
        launch, top, exit_point = arena.rooftop(start, roof, cfg, 73.)
        self.assertEqual(top, roof)
        self.assertEqual(exit_point[2], 0.)
        self.assertTrue(arena.fits(exit_point, cfg['radius'], cfg['height']))
        o = obs_at(pos=launch)
        o['pickups'] = [{'id': 8000, 'kind': 'laser', 'pos': list(roof)}]
        action = Bot(self.hello).act(o)
        self.assertTrue(action['jump'])
        self.assertTrue(action['boost'])
        self.assertNotIn('take', action)

    def test_mortar_uses_a_checked_arc_and_wont_self_splash(self):
        self.write_map()
        cfg = self.hello['config']
        ctl = Weapons(cfg, Arena(self.hello['map']['dir'], cfg['mech']))
        o = obs_at(145., alive=2)
        o['self']['weapons'] = [weapon('gun', cooldown=1.), weapon('mortar')]
        target = foe(1, 75., 0.)
        o['mechs'] = [target]
        o['self']['aim'] = ctl.act(o, target, urgent=False, reserve=27.)['aim']
        self.assertTrue(ctl.act(o, target, urgent=False, reserve=27.)['fire'][1])
        target['pos'] = [4., 0., 0.]
        self.assertFalse(ctl.act(o, target, urgent=False, reserve=27.)['fire'][1])

    def test_gun_leads_and_waits_for_actual_torso(self):
        self.write_map(box(29., 31., -8., 8.))
        cfg = self.hello['config']
        weapons = Weapons(cfg, Arena(self.hello['map']['dir'], cfg['mech']))
        o = obs_at(110., aim=(2.2, 0.), alive=2)
        target = foe(1, 63., 23., vel=(0., 13., 0.))
        o['mechs'] = [target]
        first = weapons.act(o, target, urgent=False, reserve=27.)
        self.assertFalse(first.get('fire', [False])[0])
        self.assertGreater(first['aim'][0], math.atan2(23., 63.) + .025)
        o['self']['aim'] = first['aim']
        self.assertTrue(weapons.act(o, target, urgent=False, reserve=27.)['fire'][0])
        target['pos'] = [63., 0., 0.]
        o['self']['aim'] = [0., -.04]
        self.assertFalse(weapons.act(o, target, urgent=False, reserve=27.).get('fire', [False])[0])

    def test_laser_release_and_lightning_escape_reserve(self):
        self.write_map()
        cfg = self.hello['config']
        ctl = Weapons(cfg, Arena(self.hello['map']['dir'], cfg['mech']))
        o = obs_at(140., alive=2, aim=(0., -.035))
        target = foe(1, 78., 0.)
        o['mechs'] = [target]
        o['self']['weapons'] = [weapon('gun', cooldown=1.), weapon('laser')]
        first = ctl.act(o, target, urgent=False, reserve=27.)
        self.assertTrue(first['fire'][1])
        o['self']['aim'] = first['aim']
        o['self']['weapons'][1]['charge'] = .75
        self.assertFalse(ctl.act(o, target, urgent=False, reserve=27.).get('fire', [False, False])[1])
        self.assertFalse(ctl.act(o, target, urgent=True, reserve=27.).get('fire', [False, False])[1])
        target['pos'] = [28., 0., 0.]
        o['self']['weapons'][1] = weapon('lightning', cooldown=1.)
        self.assertFalse(ctl.act(o, target, urgent=False, reserve=27.).get('fire', [False, False])[1])
        o['self']['weapons'][1]['cooldown'] = 0.
        o['self']['energy'] = 28.
        self.assertFalse(ctl.act(o, target, urgent=False, reserve=27.).get('fire', [False, False])[1])

    def test_late_position_beats_unnecessary_crossfire(self):
        self.write_map()
        bot = Bot(self.hello)
        o = obs_at(306., alive=3)
        o['zone']['next_radius'] = 25.
        o['mechs'] = [foe(1, 53., 28., yaw=-math.pi / 2, hp=220.), foe(2, 53., -28., yaw=math.pi / 2)]
        a = bot.act(o)
        self.assertFalse(any(a.get('fire', ())))
        self.assertLessEqual(math.hypot(*a['move']), 1.)
        o = obs_at(308., alive=2, aim=(0., -.04))
        o['zone']['next_radius'] = 25.
        o['mechs'] = [foe(1, 54., 0., hp=220.)]
        first = bot.act(o)
        o['self']['aim'] = first['aim']
        o['t'], o['tick'] = 308.1, 6162
        self.assertTrue(bot.act(o)['fire'][0])

    def test_defensive_gun_fires_while_moving_toward_loot(self):
        self.write_map()
        o = defensive_obs()
        o['self']['weapons'][1] = weapon('laser')
        o['cubes'] = [{'id': 5000, 'pos': [-77., 13., 0.]}]
        action = Bot(self.hello).act(o)
        self.assertEqual(action['fire'], [True, False])
        self.assertGreater(action['move'][1], .9)
        self.assertNotIn('reload', action)

    def test_defensive_gun_fires_on_aligned_charge_without_closing(self):
        self.write_map()
        o = defensive_obs()
        actor = foe(7, 78., 0., charging=True)
        actor['weapons'] = ['laser']
        o['mechs'] = [actor]
        self.assertEqual(Bot(self.hello).act(o)['fire'], [True, False])

    def test_defensive_gun_respects_threat_and_readiness_vetoes(self):
        self.write_map()
        for label in ('turned away', 'retreating', 'stationary', 'unknown loadout',
                      'bad aim', 'cooldown', 'reload', 'empty', 'unknown ammo', 'short live range',
                      'own laser charging', 'second line', 'second line beyond 160',
                      'urgent shrink', 'healing'):
            with self.subTest(label=label):
                o = defensive_obs()
                actor, = o['mechs']
                own = o['self']
                gun = own['weapons'][0]
                if label == 'turned away':
                    actor['aim'][0] = 0.
                elif label == 'retreating':
                    actor['vel'] = [7., 0., 0.]
                elif label == 'stationary':
                    actor['vel'] = [0., 0., 0.]
                elif label == 'unknown loadout':
                    actor['weapons'] = []
                elif label == 'bad aim':
                    own['aim'][0] = 1.
                elif label == 'cooldown':
                    gun['cooldown'] = .14
                elif label == 'reload':
                    gun['reload'] = 1.
                elif label == 'empty':
                    gun['ammo'] = 0
                elif label == 'unknown ammo':
                    del gun['ammo']
                elif label == 'short live range':
                    gun['stats'] = {**gun['stats'], 'range': 90.}
                elif label == 'own laser charging':
                    own['weapons'][1] = weapon('laser', charge=.6)
                elif label in ('second line', 'second line beyond 160'):
                    x = 20. if label == 'second line' else 83.
                    o['mechs'].append(foe(5, x, 35., yaw=math.atan2(-35., -77. - x)))
                elif label == 'urgent shrink':
                    o['zone'].update(next_radius=35., shrink_start=0., shrink_end=2.)
                elif label == 'healing':
                    own['hp'] = 1030.
                    o['events'] = [{'type': 'damaged', 'weapon': 'zone', 'amount': 20}]
                self.assertEqual(Bot(self.hello).act(o)['fire'], [False, False])

    def test_defensive_gun_keeps_cover_and_retreat_priorities(self):
        self.write_map(box(-2., 2., -5., 5.))
        self.assertFalse(Bot(self.hello).act(defensive_obs())['fire'][0])
        self.write_map()
        bot = Bot(self.hello)
        o = defensive_obs()
        o['self']['hp'] = 500.
        o['mechs'][0]['pos'][0] = -12.
        o['events'] = [{'type': 'damaged', 'weapon': 'gun', 'amount': 45, 'by': 4, 'bearing': 0.}]
        self.assertLess(bot.act(o)['move'][0], -.5)
        later = defensive_obs(.1)
        later['self']['hp'] = 500.
        action = bot.act(later)
        self.assertFalse(action['fire'][0])
        self.assertLess(action['move'][0], -.5)

    def test_defensive_gun_does_not_hijack_later_loot(self):
        self.write_map(box(-2., 2., 1., 6.))
        bot = Bot(self.hello)
        o = defensive_obs()
        o['pickups'] = [{'id': 8000, 'kind': 'laser', 'pos': [-77., 15., 0.]}]
        self.assertEqual(bot.act(o)['fire'], [True, False])

        later = defensive_obs(.8)
        later['self']['pos'] = [-77., 9., 0.]
        later['pickups'] = o['pickups']
        action = bot.act(later)
        self.assertEqual(action['fire'], [False, False])
        self.assertGreater(action['move'][1], .9)

        arrival = defensive_obs(1.4)
        arrival['self']['pos'] = [-77., 15., 0.]
        arrival['self']['weapons'][1] = weapon('mortar')
        arrival['pickups'] = o['pickups']
        arrival['mechs'][0]['pos'][1] = 15.
        action = bot.act(arrival)
        self.assertEqual(action['take'], 8000)
        self.assertEqual(action['drop'], 1)
        self.assertEqual(action['fire'], [False, False])

    def test_zero_radius_and_skipped_ticks_choose_inward(self):
        self.write_map()
        bot = Bot(self.hello)
        bot.act(obs_at(399., pos=(29., 0., 0.), alive=4))
        o = obs_at(410., pos=(29., 0., 0.), alive=4)
        o['self']['in_zone'] = False
        o['zone'] = {'center': [0., 0.], 'radius': 5., 'next_center': [0., 0.], 'next_radius': 0.,
                     'shrink_start': 390., 'shrink_end': 415., 'dps': 260.}
        self.assertLess(bot.act(o)['move'][0], -.5)
        self.assertEqual(Zone(o).margin((0., 0.), 6.), 0.)


if __name__ == '__main__':
    unittest.main()
