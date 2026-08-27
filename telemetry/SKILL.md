---
name: telemetry
description: Some questions cannot be answered from a picture. A screenshot cannot tell you a brake trace, a slip angle, or how hard you actually braked — a number can. Records live sim driving data as CSV from BeamNG.drive, Assetto Corsa, Live for Speed and rFactor (OutGauge UDP) or from iRacing (SDK shared memory), then analyses it. Use for "how hard was I braking", "compare those two laps", "what speed was I carrying there", "did I ever hit full throttle", "why does the car feel wrong" — any question about what the vehicle actually did.
---

# Telemetry

**The problem:** you can show Claude a screenshot of a sim, and it can read the speedo. It
cannot tell you how hard you braked into turn 3, how long you were at full throttle, or
whether you overlapped throttle and brake. Those are traces, not pictures, and no amount of
image analysis recovers them.

This records the numbers directly, at 50+ Hz, for no token cost at all.

```bash
python telemetry/telemetry.py selftest              # verify without a sim running
python telemetry/telemetry.py listen --seconds 120  # OutGauge sims
python telemetry/telemetry.py iracing --hz 30       # iRacing
python telemetry/telemetry.py summary <file.csv>
```

Both recorders write the same CSV — `t, speed_mph, throttle, brake, gear, rpm, steer_deg,
clutch` — so `summary` reads either. Files land in `~/.claude/telemetry/`.

## OutGauge sims (BeamNG.drive, Assetto Corsa, Live for Speed, rFactor)

**OutGauge must be enabled in the sim first.** In BeamNG, search the Options for
**OutGauge** — it has moved between versions, so search rather than following a fixed menu
path. Enable it, set the target to `127.0.0.1` and the port to `4444`, then start the
listener before driving.

If nothing arrives, the listener says so after a few seconds rather than sitting silent.

The OutGauge protocol carries no steering channel, so `steer_deg` is blank for these
recordings. That is the protocol, not a bug. Speed, throttle, brake, clutch, gear and RPM
are all present.

## iRacing

Requires the **sim itself** running — the background `iRacingService` is not enough, and the
reader says so rather than failing obscurely. `--list` dumps every available channel with
its units.

Stated plainly: the reader is unit-tested against a synthetic shared-memory block matching
the published layout, but has **not** been verified against a live iRacing session. Treat
your first live run as a test — check `--list` returns sensible channel names before
trusting a recording.

## Reading `summary`

Two details matter, because both were bugs first:

- **`peak decel` is measured over `--min-dt` windows** (default 0.25s). A single 50 Hz step
  divides by a dt small enough that timing jitter alone reads as tens of g. An early version
  of this tool confidently reported **24 g** on a clean synthetic trace for exactly that
  reason.
- **`hard brakes` groups consecutive over-threshold samples into one event**, reporting its
  peak and the speed it scrubbed. Counting per sample turned a single braking zone into
  **61** "events". Tune with `--brake-g`, default 0.5 g.

Gaps over 1s in the samples are reported explicitly, because they silently understate both
distance and event counts.

## Pairing with screen capture

Telemetry says *what the car did*; a screen recording shows *what it looked like*. A hard
brake at t=41s in the CSV is a timestamp you can take straight to `clipwatch zoom --at 41`.

## Verifying without a sim

`selftest` checks the OutGauge decoder over real loopback UDP — building a packet, sending
it, decoding it back — and the iRacing parser against a synthetic shared-memory block. It
needs no sim running and no network. Run it first if anything looks wrong.

## Requirements

Python 3.8+. No third-party packages, no compiled extensions. The iRacing reader is
Windows-only (shared memory); OutGauge works anywhere.
