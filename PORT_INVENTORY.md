# Port inventory: fork 0.7.9 → upstream 3.10.2

Written 2026-08-22, before any code was touched.

## The situation in numbers

    common ancestor      b4c3194, 2026-01-04
    our commits since          9   (+1 999 lines, 23 files)
    upstream commits since   795   (95 files, +23 489 / −6 074)

This is not a merge. Upstream deleted the entire transport and command
layer — `nkbAPI.py`, `nkbcommand.py`, `nkbconnect.py`, `nkblistener.py`,
`nkbprotocol.py` are all gone, the protocol now lives in an external library
("transport reconnection delegated to the library", release 3.8.3). Two of
those files carry our changes. Replaying 795 commits onto an architecture
that no longer exists would produce hundreds of conflicts in files that were
removed, and an integration nobody could reason about afterwards.

It is a port: start from upstream, re-apply intent feature by feature.

## The anchor: what must not change

26 entities, 25 covers and one switch. Every single one of them has a
unique_id of the form:

    nikobus_yaml_cover_<12 hex>
    nikobus_yaml_group_cover_<12 hex>

built in `__init__.py` as:

```python
digest = sha1(":".join(sorted([up_code, down_code, stop_code]))).hexdigest()[:12]
unique_id = f"{DOMAIN}_yaml_cover_{digest}"
```

The identity is derived from the **Nikobus bus codes**, sorted, not from the
name or the order in the file. That is a deliberate and good choice:
renaming a cover in YAML keeps the entity; only rewiring changes it.

Home Assistant recognises an entity by its unique_id. If a port produces
different ones, all 26 entities are orphaned, the new ones take the same
names with a `_2` suffix, and every automation, scene and HomeKit assignment
that points at them breaks at once.

**This is the constraint the whole port has to be built around.**

## The problem underneath it

Upstream `__init__.py`:

```python
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
```

Upstream accepts no YAML configuration at all. It is not that the YAML cover
feature is merely absent upstream — upstream has moved in the opposite
direction, in line with Home Assistant core pushing integrations away from
YAML. Entities come from discovered modules instead.

So this installation is not "upstream plus tweaks". It runs on a
configuration model that upstream deliberately does not have. Any port has
to carry that model forward as a fork feature; adopting upstream's model
would mean re-creating all 26 entities from scratch under different
identities.

## The nine commits

| # | commit | intent | upstream now | port cost |
|---|---|---|---|---|
| 1 | `d0e67b3` | discovery toggle in the config flow | has `discovery_mixin.py`, own toggle | **check, likely drop** |
| 2 | `2a27f23` | **YAML covers and switches** + travel calculator | config-entry only, no YAML | **carry forward, large** |
| 3 | `8b2142b` | missing config files treated as empty | `nkbconfig.py` still exists, rewritten | check, small |
| 4 | `d6e6fe8` | hide cover button entities, adjust IDs | naming moved to `nkbnames.py` | **carry forward, ID-critical** |
| 5 | `7524bcd` | helpers refactor, suggested entity IDs | `nkbnames.py` | **carry forward, ID-critical** |
| 6 | `f1179ac` | suggested IDs for single-area covers | — | **carry forward, ID-critical** |
| 7 | `50ae5ed` | queue cover repeats as burst blocks | command layer gone, library owns it | **re-think against the library** |
| 8 | `4799dd0` | release 0.7.9 (cover work, actuator) | — | fold into the rest |
| 9 | `fc823dc` | mirror bus buttons, stop groups | weak match only | **carry forward, medium** |

Commits 4, 5 and 6 are the ones that decide whether entity IDs survive.
Commit 2 is the one the whole installation stands on.

## What upstream brings that we want

- **`NikobusConnectionSensor`** in `sensor.py`: an enum sensor, diagnostic
  category, fed by the coordinator. Exactly the connection feedback we were
  about to build by hand so Watchtower could see a dead serial writer. On
  21.08.2026 the integration knew ("Writer is not available") and told
  nobody; upstream fixed that class of problem generically.
- **`nkbtravelcalculator.py`** (65 lines) against our 171-line
  `helpers/travelcalculator.py`. Different scope — ours drives YAML covers
  with position estimation — but worth diffing before carrying ours over.
- **Robust reconnect** with exponential backoff, delegated to the library
  (3.8.3). This is the fix for the failure mode of 21.08.2026.
- **A `tests/` directory.** Upstream ships tests now, which is the single
  biggest lever for making future updates safe.
- `router.py`, `nkbstorage.py`, `nkbreconcile.py`, `repairs.py`,
  `quality_scale.yaml`, `py.typed` — a much more maintainable base.

## Unknowns to settle before porting

1. **Does the new library work with this installation?** The config entry
   here is `has_feedbackmodule: false, prior_gen3: true` and now runs over
   a plain FTDI serial adapter. 3.10.0 added "setup without PC-Link", which
   suggests this constellation is supported, but it is untested here.
2. **Does upstream's discovery see these actuators at all?** If not, the
   YAML model is not merely preferred, it is required.
3. **What happens to the 26 entities on first start of a ported build?**
   Must be answered on a copy, never on the live system.

## Recommended order

1. Bring up upstream 3.10.2 unmodified in a **throwaway Home Assistant**
   against a copy of this configuration. Find out whether the hardware
   talks to the new library at all. Nothing else is worth planning until
   that is known.
2. Port the YAML cover model (commit 2) onto the new base, keeping the
   sha1-of-bus-codes unique_id byte for byte.
3. Port the naming and hiding behaviour (commits 4-6) and compare the
   resulting entity registry against the 26 identities recorded here.
4. Re-think the burst queue (commit 7) against what the library offers -
   it may already be unnecessary.
5. Port button mirroring (commit 9).
6. Adopt upstream's connection sensor and wire a Watchtower check to it.

Steps 2-6 each need a test in upstream's `tests/`, which is what makes the
next update cheaper than this one.

## Safety net before any of this touches the house

- Back up `.storage/core.entity_registry`, `.storage/core.device_registry`
  and the `nikobus:` block of `configuration.yaml`.
- Record the 26 entity_id/unique_id pairs (done, see above) so a mismatch
  is detectable rather than merely felt.
- Keep 0.7.9 installable: it is a tagged commit on this fork, and rollback
  is a HACS downgrade plus a restart.
