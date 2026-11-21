# Mech Royale — final-submission plan

**Be aggressive about loot, not kills.** Get a secondary and early upgrades before the field thins, preserve the HP and energy those resources buy, and reach the last circles with solid cover and a route out. Attack to secure power, defend a rotation, or win the last duel. Do not chase merely because someone is low.

This is the candidate to build for the final, not a throwaway starter. Keep the candidate, its tests, and its packaging in `1st_sub/`. The parent folder holds the unmodified kit and [repository inventory](../approach.md#2-whats-already-in-the-folder).

## 1. Optimize the real scoring objective

| Round | What it rewards | What the same bot does |
| --- | --- | --- |
| Practice: 48 matches; top 32 qualify | The 15 house bots act as a squad, share vision, and never fight each other. Surviving alone does not thin the field. | Fight isolated pursuers when they threaten the loot route or block the next position. Convert the safe fights into cubes, then break contact and reset. Do not assume opponents will shoot each other. |
| Qualifier: 16 matches; top 16 advance | Fresh seeds, 16-way free-for-all. A few early deaths can sink the run. | Let others trade. Collect resources while they are occupied, then claim a position the next circle will keep. |
| Final: 64 matches | Mean `16 - place` in 16-way free-for-alls on new seeds. Kills score nothing directly. | Keep taking low-risk power until position matters more. In the last duel, convert the advantage rather than hiding until the zone forces an even trade. |

Use one observation-driven policy; no guessed match mode or private-seed assumptions. The [official track](https://www.joinbuilders.org/buildathon/tracks/builders) gives the format, a **1.15** starter baseline, and four submissions per hour; the [live leaderboard](https://www.joinbuilders.org/buildathon/leaderboard) gives the actual qualification line. Equal scores favor the earlier submission. Don't trade a safer final policy for a small practice gain without evidence that the change also helps in free-for-alls.

## 2. Decision policy

On each observation, update damage, threat, inventory, and zone state. Commit to one movement goal in this order: **avoid lethal zone exposure → break crossfire or reset an untenable fight → secure next-circle position when time is tight → pursue high-value loot or repair → improve position**. Fire only if it serves the current goal or converts an exceptionally cheap finish. Keep a goal until it loses value, the route stalls, or a safety gate changes; do not restart the plan every 100 ms.

### Loot: actively route toward power

For each plausible loot goal, estimate **ETA to acquire, exposure on the way and at the goal, HP/energy spent, and time to reach cover within the next circle**. Reject options without a verified exit or with two independent hostile firing lines. Rank the rest by immediate power gained versus travel and exposure; use actual route time, not straight-line distance.
- Check the full bounded set of local exit samples in zone-margin order. A collision or exposed firing lane can rule out all the inward headings without ruling out a safe lateral exit.

- **Opening priorities:** rank the first cube (first upgrade), a playable secondary, and reaching the three-cube upgrade above routine detours. Prefer loose cubes and floor pickups to noisy farming. If none are reachable, shoot an isolated crate from a safe angle: seven base gun hits for a fresh 300 HP crate before scaling. Take an isolated drone only if its two cubes beat alternatives and its retaliation won't spoil the route or a needed HP reset.
- **Search when the bot sees nothing worth taking.** Move along covered waypoints that reveal unscanned ground while making progress toward the next circle. Briefly look into loot-bearing lanes from cover when no credible threat needs the torso; don't spin in the open. Remember recently seen pickups with decaying confidence and revoke the goal when you reach a vantage where it should be visible but isn't. Random spawns mean search routes should cover useful ground, not visit fixed “loot spots.”
- If the ordinary search ring leaves us stationary with no current firing line, try safe torso-relative waypoints at 15 m and 5 m to find openings between the compass headings.
- **Caps and size:** the default +4% HP and damage per cube stop at ten, but body size keeps growing. Past the stat cap, justify detours by upgrade progress or other concrete value. Recheck collision clearance with live radius/height. For both cubes and weapons, test horizontal pickup range and the 4 m vertical limit; don't camp below unreachable roof loot. Cubes are automatic; weapons need `take` and confirmation from inventory or events.
- **Supply drops:** on announcement, score a covered staging position and approach near landing; don't wait in the ring for the drop to land. Earliest completion is `max(route_eta, lands_in) + capture_s`, less banked progress only if we already hold the ring and can stay. Full HP, full energy, and three cubes make a safe drop a strong reset. Contest only for a meaningful repair or upgrade, with cover, a safe exit, and at most one credible threat. A credible threat has a firing line at our route or ring, or can plausibly open one before capture completes. If a second threat opens a line, two bodies stall capture, or the zone cuts the exit, leave even if progress is nearly full. No kills required.
- **Upgrade offers:** score the actual offer IDs and effects from `hello.config.upgrades`, then send the choice index. Use mobility when route slack repeatedly binds; armor or plating for survivability; regen or energy for repeated resets; weapon upgrades for a weapon that already lands shots. Armor doesn't reduce zone damage. Never key decisions to display text or assume a particular choice appears.

### Position: make the exit part of the approach

- Index the static map once at `hello`. Use bilinear terrain, prism footprints with holes, and bridge undersides. Precompute a coarse ground grid (start at ~5 m), cache bounded route searches, and validate local movement using live radius, height, step and slope limits. Treat trees as vision cover only; solid prisms and terrain stop shots. Reject corner cuts and replan on measurable lack of progress.
- Project the circle to the expected arrival time using the observed shrink window. Sample at waypoints, not just at the destination, so a path tangent that crosses the moving circle doesn't pass as safe. Use acceleration, turning of the movement vector, and uphill detours when estimating travel. As a starting guard, require time to **finish the detour, reach a covered exit, and keep at least 3 s of slack** before the route becomes unsafe. This buffer is a tuning hypothesis, not a game constant. In an emergency choose the reachable route with the lowest projected zone damage, not the shortest route through a wall.
- Choose cover with a vantage point and an alternate exit; if alternatives are similar, choose the one deeper into the projected next circle. Enter late circles before cover fills, but don't run to the middle of open ground just because it's the center. When the radius nears zero, shrink the desired margin and minimize expected zone damage rather than rejecting all routes.
- Ground cover is the default. Use a roof only if verified clearance, jet energy, sightlines, and a descent route all beat the ground alternative. Budget energy for both movement and exit, and don't count on regen during sustained boost or lightning. Never pursue roof loot blind; after an elevated spawn, choose a checked descent if no rooftop route is viable.

### Fight: change the angle, not the objective

- **Opening and midgame:** flank only to secure a loot target, clear a rotation, or exploit an opponent already occupied without opening another line. Approach behind solid cover, then shoot from a different angle than the last one shown; move before repeating the peek. Rear hits deal ×1.5 by default, so keep our torso facing the dominant threat while moving laterally. A flank is an advantage, not a requirement to fight.
- **Defensive gun window:** with no normal target, allow a gun-only shot at 120–160 m (upper bound excluded) if one visible opponent has a clear line, is within 15° of us in yaw and pitch, and is moving toward us at ≥4 m/s or visibly charging a laser. Require a hit prediction from the live gun controller. A second firing line can veto even beyond 160 m. Save movement, equipment swaps, an active own laser charge, healing, and urgent travel ahead of this shot; it does not seed pursuit or flank memory.
- **Pursuit:** cross at most one additional cover transition, and only if a finish matters, the hit is likely, and no second credible line or closing route appears. Low enemy HP alone is not permission. Remembered actors in `obs.mechs` can be behind solid cover; confirm geometry before turning exposure into a chase. Use sound and damage bearings to avoid tunnel vision, not as precise locations.
- **Reset:** break contact if multiple firing lines open, projected damage makes the trade unfavorable, or remaining energy is needed for escape. Reload in cover. Healing requires the configured quiet interval since damage **dealt or taken** and being inside the zone; don't waste the reset on speculative shots or crate farming. Keep just enough threat awareness to catch a new angle while recovering.
- If retreat would leave us stationary under one known firing line, try up to two covered detours. Keep only a short route that stays in the projected zone, does not pass close to the attacker, and opens no additional credible line at the checked points.
- **Three or more alive in the late game:** hold or improve the next-circle position. Ignore a duel you don't need to enter. When you must shoot, prefer a quick finish that removes a threat to our position and leaves cover from the other survivors.
- **Last duel (`obs.alive == 2`):** compare HP when shown, cover, range, ready burst, energy, and zone timing. If ahead, pressure from a safe angle while the advantage holds; force the opponent to cross an exposed route rather than surrendering cover to chase. If behind, break LOS and change angles or heal when the zone permits. When the zone forces contact, choose the timing and route that minimize zone damage and favor our ready weapon. Don't treat missing enemy HP as a known lead.
- During urgent rotation or escape in the last duel, a ready lightning arc may short a charging laser on the already selected target. Keep the same aim and movement, require a clear first contact on the first tick, and preserve the energy reserve after movement drain.

### Weapons: keep the gun and adapt the secondary

| Weapon | Use it when | Never assume |
| --- | --- | --- |
| Gun | Always retain it for reload/charge gaps and general pressure. Solve a positive flight-time intercept using observed target velocity and live speed; gate the shot against the moving cylinder, terrain, and solid cover with the actual torso slew, muzzle height, and barrel offset. | Desired aim is actual aim, or a remembered opponent is shootable. Don't fire or reload into a window without checking what follows. |
| Laser | Prefer for clear midrange off-angle burst. Start charge when the movement penalty is affordable and a credible release window exists. Release on an aligned clear shot; leave danger rather than holding charge indefinitely. | Releasing cancels a charge: it fires, possibly after the minimum charge time. Holding past the limit also fires. |
| Lightning | Prefer for tight cover fights, especially against charging lasers. Check the actual aim cone, LOS, and energy left after the shot. | The intended target is first in the arc: a closer NPC or crate can take it. |
| Mortar | Use on verified ballistic arcs with useful predicted splash or a blocking position to dislodge; reject arcs that hit our own cover or put us in splash range. | A clear XY bearing implies a clear arc, or a nearby wall won't block splash. |

Swap a secondary only for a material improvement that survives the equip cooldown and immediate danger. A charging laser can't be swapped. Use current weapon stats for policy and geometry; the [protocol](../protocol.md#7-rules-of-the-world) documents defaults and signature effects.

## 3. Implementation order, one release candidate

1. **Runtime and geometry.** Copy the Python wrapper and Dockerfile into `1st_sub/`, change the entrypoint to `bot.py`, and pin the image base. Keep protocol transport separate from `Bot.act(obs)`. Put collision, LOS, route ETA, and zone projection behind a small map module so loot, rotation, and combat score the same world. Load `/arena` via `hello.map.dir`, not a bundled second map.
2. **Decision and economy.** Build the loot shortlist, shared exit gate, upgrade picks, and threat-aware route commitment. Include drop staging/abort and covered reset before tuning combat. Keep shortlist and route work bounded per observation; use last validated path or a checked local step when search is exhausted.
3. **Combat and conversion.** Add gun intercept and torso gating, each secondary's fire control, conditional peeks/pursuit, and the `alive`-based endgame. Test policy through observations and returned actions. Retain only path/goal state, recent threat and damage history, and weapon state that the current observation doesn't already provide.
4. **Profile and package.** Use real tick deltas or `obs.t`, not observation counts. Aim for zero deadline misses and **p99 ≤ 10 ms** in the constrained container, including synthetic dense observations and a route invalidation. Keep startup comfortably under 20 s. Skip a full simulator and opponent-specific training; use deterministic local cases plus the official scorer. Output protocol only on stdout and check local stderr: a wrapper fallback can hide a policy exception behind a valid action.

## 4. Acceptance before submitting

Use deterministic observation sequences for decisions and the actual map for geometry. The original fixture tests transport, not tactics: it has 300 observations, ends at 36.8 s, and has no drop or upgrade offer.

| Scenario | Required outcome |
| --- | --- |
| Safe laser and first cube on a covered detour; exposed low-HP rival nearby | Pursue loot; don't trade the route for the kill. Resolve the first offer by index. |
| Roof pickup above us; low bridge on direct route; viable ground detour | Reject the false pickup and blocked route; take the reachable alternative. A high bridge with sufficient head clearance remains passable. |
| Nearly captured drop, then a second hostile firing line opens | Abort immediately toward the validated exit. In the same setup with the second line absent, complete the safe capture. |
| Midrange crossing target; aim initially turned away; target later disappears behind a building | Lead the target only once actual aim permits a shot. Suppress fire through solid cover even while the actor remains in memory. |
| Laser charge and closing escape window; swapped secondary not yet equipped | Prefer escape over a speculative charge/peek; respect release and equip behavior. Lightning conserves the energy needed for the exit. |
| Wounded target rounds a corner; third party opens a line | Stop pursuit. If the third-party line is absent and the finish protects a valued goal, permit one cover transition only. |
| Three alive; the other two fight; our next-circle cover is safe | Hold the position or take a cheap finish from cover; never turn this into a chase. With two alive, use a verified advantage to close, and re-angle if behind. |
| Imminent shrink with no positive margin and a skipped observation | Use elapsed time and choose the lowest-damage reachable rotation; do not idle because no goal meets the normal margin. |

From the project root, after the candidate exists:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 mechfight-kit/check/check.py -- python3 -u 1st_sub/bot.py
docker build --platform linux/amd64 -t mech-royale-final 1st_sub
python3 mechfight-kit/check/check.py --map-dir /arena -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --memory-swap 512m \
  --pids-limit 128 --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  --cap-drop ALL -v "$PWD/mechfight-kit/arena:/arena:ro" mech-royale-final
```

For the public GitHub submission, **publish the contents of `1st_sub/` at the submission repo root** so the Dockerfile isn't nested. Include the candidate README and MIT license; keep the submission commit under 20 MB and the linux/amd64 build within 10 minutes. Don't copy the kit, archive, tests, or local traces into the runtime image. Keep the chosen commit fetchable through results.

**Lock decision:** require targeted scenarios, sandbox conformance, and a qualifying practice result. Record commit, score and qualification line. If match breakdowns are available, prioritize fewer bottom-four exits, more top-four finishes, and fewer fatal rotations; kills are diagnostic only. If all you have is the aggregate, don't infer why it changed. Where two qualifying candidates look similar, choose the one that better preserves HP, cover, and loot access under free-for-all scenarios. Explicitly select it before the cutoff if it isn't the highest practice scorer.
