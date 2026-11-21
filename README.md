# Cutline — Mech Royale

A deterministic `mechfight/1` bot for the Builders Buildathon. Cutline routes aggressively toward cubes and useful weapons, only contests valuable loot with a checked escape, and values zone position and HP above kills. It keeps its gun and chooses the secondary for the angles and opponents it expects to fight. The [decision plan](approach.md) describes the intended behavior.

The runtime uses Python's standard library. `mechfight.py` adapts the [official kit's](https://www.joinbuilders.org/buildathon/tracks/builders) Python wrapper to drain queued input directly from the Linux pipe. The scorer mounts the map; there is no runtime network or disk state. The bot does not have access to private match seeds or other bots' inputs.

## Verify and submit

Use a copy of the official kit next to this directory or set `KIT` to its absolute path. From this directory:

```sh
KIT=${KIT:-$(realpath ../mechfight-kit)}
PYTHONDONTWRITEBYTECODE=1 MECH_KIT="$KIT" python3 -m unittest discover -s tests -v
docker build --platform linux/amd64 -t mech-royale-final .
python3 "$KIT/check/check.py" --map-dir /arena -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --memory-swap 512m \
  --pids-limit 128 --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  --cap-drop ALL -v "$KIT/arena:/arena:ro" mech-royale-final
```

Local verification: 18 deterministic scenario tests passed. The official recording and two 300-observation synthetic stress streams (15 opponents with 24 crates, and 24 crates around the changed policy paths) passed under these Docker limits with zero deadline misses, stale actions, invalid output, or stderr. Candidate p99 was 3.9 ms, 4.4 ms, and 3.3 ms respectively.

The conformance checker does not simulate the bot's actions. Practice placement must be measured on the scorer. Publish the **contents of this directory** to a public GitHub repository with `Dockerfile` at its root and submit that URL on the track page. Keep the submitted commit accessible until results are published.
