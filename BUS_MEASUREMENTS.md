# What this Nikobus installation actually answers

Measured 2026-08-22 on the live installation. Everything here is observation,
not inference from documentation — the code comments and the upstream
maintainer's assumptions were the starting point, and two of them turned out
to describe something different from what happens here.

## The installation

    PC-Link                 sticker 11024009
    actuators               3, all with sticker 01343 (a model number, not an
                            address - identical on all three)
    feedback module         none        (has_feedbackmodule: false)
    generation              prior_gen3: true
    transport               FTDI FT232R, /dev/ttyUSB1 in the HA container,
                            9600 baud, CR-terminated frames, Windows-1252
    entities                26, all YAML-defined covers driven by button codes
    module config           none — nikobus_module_config.json does not exist

Consequence of the last two lines: `dict_module_data` is empty, so the poll
loop iterates over nothing. On top of that `_get_update_interval()` returns
`None` for `prior_gen3`, disabling the timer entirely. **This installation
does not poll anything, and `refresh_interval: 60` in the config entry has no
effect.**

## Method

The serial port is held exclusively by Home Assistant, so the Nikobus config
entry was disabled over the websocket API for each run and re-enabled
immediately afterwards. Commands were built with the integration's own
`nkbprotocol.make_pc_link_command` — hand-rolled frames get the CRCs wrong.

Pacing: 0.8 s listening plus 0.7 s of silence per command. The integration
itself uses `COMMAND_EXECUTION_DELAY = 0.7 s` in normal operation, so the bus
saw nothing it does not see on an ordinary day.

**0x15 and 0x16 were deliberately skipped.** They are the write commands
(`make_pc_link_command(0x15, address, channel_states)` sets outputs). Sent
without a data part their effect is undefined, and undefined here means
relays.

## Response latency

**0.20–0.21 s**, without exception, across every answer in every run.

This matters more than it looks. `NikoBusController/scan.py` — the address
scanner in this project — uses `TIMEOUT = 0.06`. Over this USB path it would
miss every single answer and report an empty bus regardless of what is out
there. The tool dates from the era of the TCP-serial bridge at
172.20.20.230:4196, which no longer exists; latency over that path may well
have been lower.

## Which function codes answer

Swept 0x00–0xFF against address 9E62. **11 of 254 answered, all between 0x10
and 0x20. Everything from 0x21 to 0xFF is silent.**

Every answer starts `$05<code>` — the acknowledgement echoing the command
code — followed by the payload.

| code | payload | meaning |
|---|---|---|
| 0x10 | `$2E629EFFFF…FF` (32×F) | inventory, empty |
| 0x11 | `$18629E0050043FFF` | address inventory, constant |
| 0x1D | `$1CFF629E9BC10000` + **MM SS** + 2 bytes | **a running clock** |
| 0x1F | `$18FF629E45650000` | constant |
| 0x14, 0x18, 0x19, 0x1B, 0x1C, 0x1E, 0x20 | `$0EFF629E00E2` | `$0E` = `IGNORE_ANSWER`, "unknown response" |
| 0x12, 0x17 | — | **no answer** |

`#A` (the ASCII inventory command the coordinator uses) answers
`$18629E0050073FFF`, constant.

### No output state feedback

0x12 and 0x17 are exactly the output-state queries `get_output_state()` uses.
They are silent here. Confirmed empirically, not just from the upstream
comment that older PC-Links "can't sustain the poll cadence" — the issue is
not cadence, the modules simply do not answer at all.

Output state feedback arrives as `$1C` frames pushed by a **feedback module**
(`FEEDBACK_MODULE_ANSWER = "$1C"`). Without that hardware there is none.

### Address byte order

The address travels low byte first. The answer `$18629E…` therefore belongs
to address **9E62**, and `make_pc_link_command(func, "9E62")` produces
`…629E…` on the wire. Both readings were tested against each other:

    9E62  ->  0x10 answers,   0x12 / 0x17 silent
    629E  ->  nothing answers at all

## 0x1D is a clock

The two bytes after the constant prefix are **minutes and seconds**, each one
byte, hexadecimal, with the seconds rolling over at 60:

    0x14 0x3A = 20:58   ->   0x15 0x01 = 21:01

Verified over 43 samples across 141 s, sampled every 3.1 s:

    total elapsed by the clock:  141 s
    total elapsed in reality:    141.0 s
    samples deviating by >1.5 s: 0 of 43

Driving a blind has **no effect** on it. The `faehrt-zu` and `ruhe` phases are
indistinguishable; it advances by 1 per second throughout.

### Why this is the right liveness signal

A constant answer only proves that something replies — it can come from a
cache. That is exactly how a dead weather station passed for healthy on
2026-08-21: valid value, dead device. A clock cannot do that. If it stands
still while real time passes, the device has stopped, even though it still
acknowledges.

The tolerance needs no calibration either; it follows from physics. Between
two queries the clock must have advanced by the elapsed time, ±2 s.

## Confirmed live, in production

The ping went into the integration on 22.08.2026 and was watched on the
running installation. Three consecutive polls, read out of the Home Assistant
log:

    12:50:27.037  $051D$1CFF629E 9BC1 0001 05 17 06A76C   05:23
    12:50:57.107  $051D$1CFF629E 9BC1 0001 05 35 ...      05:53   +30 s
    12:51:27.163  $051D$1CFF629E 9BC1 0001 05 3B ...      06:23   +30 s

Poll cadence 30.06 s and 30.06 s, clock advance +30 s and +30 s. Round-trip
time 54 ms, measured from "Sending command" to "Message received" - shorter
than the 0.20-0.21 s recorded during the sweep, because that figure included
the fixed listening window rather than the answer itself.

The command goes through `queue_command` like every other one; the log shows
`Queueing command` / `Dequeued command` / `Processing command` around it, so
the ping shares the pacing with the drive commands instead of competing with
them.

### How often does a ping go unanswered?

This decides HEARTBEAT_FAILURE_THRESHOLD, and it was the last guessed number
left in the design. The threshold started at 3 - about 90 s of silence - on the
assumption that a lost answer is normal on a bus that also carries button
traffic. That assumption was never tested, and everything measured here points
the other way:

* 0x1D answered on every attempt of the 254-code sweep.
* All 43 samples of the clock run came back, none missing.
* The query is serialised through the same queue as every drive command, so it
  never competes with one for the line.

Watched live on 22.08.2026 between 11:38:01 and 11:43:32 UTC: 12 consecutive
polls, every gap exactly 30 s, no unanswered ping. That is a short window and
it is reported as one - it rules out a bus that drops answers routinely, and
nothing more. A longer run was started and deliberately called off; the
threshold was set on the evidence above rather than on a day of statistics.

If lost answers ever do show up on an otherwise healthy bus, this is the number
to raise, and by then it would be a measurement instead of a judgement.

The two directions of error are not symmetric, which is what makes 1 the right
number rather than merely a permissible one. Reacting to a lost frame that
meant nothing costs up to 30 s of "not responding" in Apple Home, which heals
by itself on the next poll and cannot reach anybody's phone - Watchtower probes
every 60 s and needs two consecutive failures, so a single-poll blip is
invisible to it. Not reacting to a real outage is the two hours of 21.08.2026.

### The "constant" bytes are not entirely constant

The four skipped bytes read `9BC1 0000` throughout the sweep and `9BC1 0001`
here. Only the last one moved, and it moved between two sessions hours apart
while MM:SS kept wrapping - which is what an hour counter looks like. That is
an observation, not a conclusion: one increment is not a measurement, and
nothing depends on it. The parser skips those bytes, and the liveness
arithmetic is modulo one hour on MM:SS alone, so it is correct either way.

If it ever matters - it would widen the unambiguous comparison window from one
hour to several days - it can be settled by reading the byte twice across a
known interval, the same way the clock itself was settled.

## Wrong turns, so nobody repeats them

**Read as a 32-bit integer, the clock looks like an activity counter.** The
first analysis treated `0A12710C` as one number. At the seconds rollover from
58 to 01 that produces an apparent jump of 199, and since the phases with a
rollover happened to be the ones containing a blind movement, the conclusion
"movement makes the counter climb seven times faster" fell out — plausible,
reproducible-looking, and wrong. What killed it was a control: sampling the
same unchanged state twice. The value moved anyway.

**Never conclude a state signal without an idle control.** The second run
bracketed every drive with measured idle stretches; that is what exposed the
rollover.

**The queried device may not be what you assume.** 9E62 answers the inventory
but not output states, which suggests it is not an output module at all —
possibly the PC-Link itself. All conclusions above are about that one device.

## Open questions

- **Is 9E62 the PC-Link or an actuator?** Unresolved. For the liveness ping it
  hardly matters — it sits behind the serial line either way. For output
  states it would matter, but those do not exist here regardless.
- **The three actuator addresses are unknown.** `#A` returns only one address,
  and the stickers carry a model number, not addresses. Finding them means
  sweeping 65 536 addresses: about 5 hours one at a time, or roughly one hour
  pipelined at 14× the normal command rate. Not worth it unless there is
  reason to believe the actuators answer 0x12 — and the one device we can ask
  does not.
- **A `.nkb` project file** from the original Nikobus PC software would list
  every module and address. Upstream 3.10.0 can even read those directly. If
  that file exists anywhere, it answers the previous question in a minute.

## Raw data

`#A` and the constant codes, identical across all five stages of the state
test (before, open, closed, open again):

    #A     $18629E0050073FFF97C01D
    0x10   $0510$2E629EFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFB0BC77
    0x11   $0511$18629E0050043FFFCE9004
    0x1F   $051F$18FF629E45650000CB871F

Clock samples, first and last of the 43:

    [  0.1s] 133A6D8D   19:58
    [141.1s] 16132733   22:19
