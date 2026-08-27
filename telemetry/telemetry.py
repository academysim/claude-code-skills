#!/usr/bin/env python3
"""Record and analyse sim telemetry as numbers instead of pixels.

Two sources:

  outgauge   UDP, spoken by BeamNG (and Assetto Corsa, LFS, rFactor). Must be enabled
             in the sim first -- see the skill notes.
  iracing    the iRacing SDK's shared-memory block, readable while the sim runs.

Both land in the same CSV shape, so `summary` reads either. Telemetry costs no tokens
and does not guess: a slip angle or a brake trace cannot be recovered from a screenshot.
"""
import argparse
import csv
import ctypes
import mmap
import socket
import struct
import sys
import time
from pathlib import Path

OUT_DIR = Path.home() / ".claude" / "telemetry"
MPS_TO_MPH = 2.2369362920544
IRSDK_MEM = "Local\\IRSDKMemMapFileName"

# LFS OutGauge packet: 92 bytes, or 96 when the optional trailing id is present.
OUTGAUGE_FMT = "<I4sHBBfffffffIIfff16s16s"
OUTGAUGE_SIZE = struct.calcsize(OUTGAUGE_FMT)
GEARS = {0: "R", 1: "N"}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def gear_name(g):
    return GEARS.get(g, str(g - 1))


def decode_outgauge(data):
    """Decode one OutGauge datagram into a flat dict, or None if it is the wrong size."""
    if len(data) not in (OUTGAUGE_SIZE, OUTGAUGE_SIZE + 4):
        return None
    (ms, car, flags, gear, plid, speed, rpm, turbo, eng_t, fuel,
     oil_p, oil_t, dash, show, thr, brk, clu, d1, d2) = struct.unpack(
        OUTGAUGE_FMT, data[:OUTGAUGE_SIZE])
    return {
        "ms": ms,
        "car": car.decode("ascii", "replace").strip("\x00"),
        "gear": gear_name(gear),
        "speed_mph": speed * MPS_TO_MPH,
        "rpm": rpm,
        "throttle": thr,
        "brake": brk,
        "clutch": clu,
        "fuel": fuel,
        "eng_temp_c": eng_t,
        "turbo_bar": turbo,
        "flags": flags,
        "display": (d1 + b" " + d2).decode("ascii", "replace").strip("\x00 "),
    }


def build_outgauge(ms=0, car=b"BNG", gear=3, speed_mps=20.0, rpm=3000.0,
                   throttle=0.5, brake=0.0, clutch=0.0, with_id=True):
    """Construct a valid packet. Used by selftest, and handy for debugging a listener."""
    pkt = struct.pack(
        OUTGAUGE_FMT, ms, car.ljust(4, b"\x00")[:4], 0, gear, 0,
        speed_mps, rpm, 0.0, 90.0, 0.75, 4.0, 95.0, 0, 0,
        throttle, brake, clutch, b"".ljust(16, b"\x00"), b"".ljust(16, b"\x00"))
    return pkt + struct.pack("<i", 0) if with_id else pkt


# --------------------------------------------------------------------------- iRacing

IR_HEADER = struct.Struct("<iiiiiiiiiii i")          # ver..bufLen + pad
IR_VARBUF = struct.Struct("<iiii")                   # tickCount, offset, pad[2]
IR_VARHDR = struct.Struct("<iii i 32s 64s 32s")      # type, offset, count, flags, name/desc/unit
IR_TYPES = {0: ("c", 1), 1: ("?", 1), 2: ("i", 4), 3: ("I", 4), 4: ("f", 4), 5: ("d", 8)}


class IRSDK:
    """Minimal read-only reader for the iRacing shared-memory telemetry block."""

    def __init__(self, path=None):
        self.path = path
        self.mm = None
        self.vars = {}
        self.buf_offset = 0

    def open(self):
        try:
            if self.path:                     # a file on disk, for tests
                self.fh = open(self.path, "rb")
                self.mm = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)
            else:
                self.mm = mmap.mmap(-1, 0, IRSDK_MEM, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            return False
        return self._parse_header()

    def _parse_header(self):
        try:
            raw = self.mm[:IR_HEADER.size]
            (ver, status, tick, _siu, _sil, _sio,
             num_vars, var_hdr_off, num_buf, buf_len, _p0, _p1) = IR_HEADER.unpack(raw)
        except (struct.error, ValueError):
            return False
        if not (0 < num_vars < 5000) or var_hdr_off <= 0:
            return False
        self.ver, self.status, self.tick_rate = ver, status, tick
        # Newest buffer wins; iRacing rotates through numBuf of them.
        best_tick, best_off = -1, 0
        for i in range(max(1, min(num_buf, 4))):
            off = IR_HEADER.size + i * IR_VARBUF.size
            tc, bo, _, _ = IR_VARBUF.unpack(self.mm[off:off + IR_VARBUF.size])
            if tc > best_tick:
                best_tick, best_off = tc, bo
        self.buf_offset, self.buf_len = best_off, buf_len
        for i in range(num_vars):
            o = var_hdr_off + i * IR_VARHDR.size
            chunk = self.mm[o:o + IR_VARHDR.size]
            if len(chunk) < IR_VARHDR.size:
                break
            vtype, voff, vcount, _flags, name, _desc, unit = IR_VARHDR.unpack(chunk)
            n = name.split(b"\x00")[0].decode("ascii", "replace")
            if n:
                self.vars[n] = (vtype, voff, vcount,
                                unit.split(b"\x00")[0].decode("ascii", "replace"))
        return bool(self.vars)

    def get(self, name):
        v = self.vars.get(name)
        if not v:
            return None
        vtype, voff, vcount, _unit = v
        fmt, size = IR_TYPES.get(vtype, (None, 0))
        if not fmt:
            return None
        try:
            raw = self.mm[self.buf_offset + voff: self.buf_offset + voff + size * vcount]
            vals = struct.unpack("<" + fmt * vcount, raw)
        except (struct.error, ValueError, IndexError):
            return None
        return vals[0] if vcount == 1 else list(vals)

    def close(self):
        for obj in (getattr(self, "mm", None), getattr(self, "fh", None)):
            try:
                obj.close()
            except Exception:
                pass


def build_irsdk_fixture(path, samples):
    """Write a file with the same layout iRacing publishes, for testing the reader."""
    names = [("Speed", 4, "m/s"), ("Throttle", 4, "%"), ("Brake", 4, "%"),
             ("SteeringWheelAngle", 4, "rad"), ("Gear", 2, ""), ("RPM", 4, "revs/min")]
    hdr_size = IR_HEADER.size + 4 * IR_VARBUF.size
    var_hdr_off = hdr_size
    data_off = var_hdr_off + len(names) * IR_VARHDR.size
    offs, cur = {}, 0
    for n, t, _u in names:
        offs[n] = cur
        cur += IR_TYPES[t][1]
    buf_len = cur

    blob = bytearray(data_off + buf_len)
    blob[:IR_HEADER.size] = IR_HEADER.pack(2, 1, 60, 0, 0, 0, len(names),
                                           var_hdr_off, 1, buf_len, 0, 0)
    blob[IR_HEADER.size:IR_HEADER.size + IR_VARBUF.size] = IR_VARBUF.pack(1, data_off, 0, 0)
    for i, (n, t, u) in enumerate(names):
        o = var_hdr_off + i * IR_VARHDR.size
        blob[o:o + IR_VARHDR.size] = IR_VARHDR.pack(
            t, offs[n], 1, 0, n.encode().ljust(32, b"\x00"),
            b"".ljust(64, b"\x00"), u.encode().ljust(32, b"\x00"))
    for n, val in samples.items():
        t = dict((nm, ty) for nm, ty, _ in names)[n]
        fmt = IR_TYPES[t][0]
        packed = struct.pack("<" + fmt, val)
        blob[data_off + offs[n]: data_off + offs[n] + len(packed)] = packed
    Path(path).write_bytes(bytes(blob))
    return path


# --------------------------------------------------------------------------- commands

CSV_COLS = ["t", "speed_mph", "throttle", "brake", "gear", "rpm", "steer_deg", "clutch"]


def open_writer(dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fh = dest.open("w", newline="", encoding="utf-8")
    w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    return fh, w


def cmd_listen(args):
    dest = Path(args.out) if args.out else OUT_DIR / f"outgauge_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        print(f"ERROR: cannot bind {args.host}:{args.port} ({e}). "
              f"Another listener may already be running.", file=sys.stderr)
        return 1

    print(f"listening on {args.host}:{args.port} for OutGauge, writing {dest}")
    print(f"stop with Ctrl+C" + (f", or after {args.seconds}s" if args.seconds else ""))
    fh, w = open_writer(dest)
    n, bad, t0, first_seen, warned = 0, 0, time.time(), None, False
    try:
        while True:
            if args.seconds and time.time() - t0 >= args.seconds:
                break
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                if not n and time.time() - t0 > 6 and not warned:
                    print("  (nothing received yet - is OutGauge enabled and pointed here?)")
                    # A separate flag: first_seen must stay None until a packet actually
                    # arrives, or t is measured from the epoch instead of from the start.
                    warned = True
                continue
            rec = decode_outgauge(data)
            if rec is None:
                bad += 1
                continue
            if first_seen is None:
                first_seen = time.time()
                print(f"  first packet: car={rec['car'] or '?'} "
                      f"{rec['speed_mph']:.1f} mph gear={rec['gear']}")
            w.writerow({"t": round(time.time() - first_seen, 4),
                        "speed_mph": round(rec["speed_mph"], 3),
                        "throttle": round(rec["throttle"], 4),
                        "brake": round(rec["brake"], 4),
                        "clutch": round(rec["clutch"], 4),
                        "gear": rec["gear"], "rpm": round(rec["rpm"], 1)})
            n += 1
            # Long captures must survive being killed, so do not sit on a full buffer.
            if n % 500 == 0:
                fh.flush()
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        sock.close()
    if not n:
        dest.unlink(missing_ok=True)
        print(f"no OutGauge packets received ({bad} malformed). Nothing written.",
              file=sys.stderr)
        return 1
    print(f"{dest}\n{n} samples over {time.time() - (first_seen or t0):.1f}s"
          + (f", {bad} malformed packets ignored" if bad else ""))
    return 0


def cmd_iracing(args):
    ir = IRSDK(args.mmap_file)
    if not ir.open():
        print("iRacing telemetry is not available. The sim itself must be running - the "
              "background iRacingService is not enough.", file=sys.stderr)
        return 1
    print(f"connected: {len(ir.vars)} channels, tick rate {getattr(ir, 'tick_rate', '?')} Hz")
    if args.list:
        for n in sorted(ir.vars):
            vtype, _o, count, unit = ir.vars[n]
            print(f"  {n:<32} {IR_TYPES.get(vtype, ('?', 0))[0]}x{count:<3} {unit}")
        ir.close()
        return 0

    dest = Path(args.out) if args.out else OUT_DIR / f"iracing_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    fh, w = open_writer(dest)
    t0, n = time.time(), 0
    period = 1.0 / max(1.0, args.hz)
    try:
        while not args.seconds or time.time() - t0 < args.seconds:
            spd = ir.get("Speed")
            row = {
                "t": round(time.time() - t0, 4),
                "speed_mph": round((spd or 0) * MPS_TO_MPH, 3),
                "throttle": round(ir.get("Throttle") or 0, 4),
                "brake": round(ir.get("Brake") or 0, 4),
                "gear": ir.get("Gear"),
                "rpm": round(ir.get("RPM") or 0, 1),
                "steer_deg": round((ir.get("SteeringWheelAngle") or 0) * 57.2957795, 2),
            }
            w.writerow(row)
            n += 1
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        ir.close()
    print(f"{dest}\n{n} samples over {time.time() - t0:.1f}s at ~{args.hz} Hz")
    return 0


def cmd_summary(args):
    path = Path(args.csv)
    if not path.is_file():
        print(f"ERROR: no such file {path}", file=sys.stderr)
        return 3
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append({k: (float(v) if k != "gear" and v not in ("", None) else v)
                             for k, v in r.items()})
            except ValueError:
                continue
    if len(rows) < 2:
        print(f"ERROR: {path} has {len(rows)} usable rows", file=sys.stderr)
        return 3

    # Captures written before the epoch-timestamp fix start t at Unix time rather than
    # at zero. Rebase them so old files stay usable instead of reporting a 57-year run.
    rebased = False
    if rows[0]["t"] > 1_000_000:
        base = rows[0]["t"]
        for r in rows:
            r["t"] -= base
        rebased = True

    dur = rows[-1]["t"] - rows[0]["t"]
    speeds = [r["speed_mph"] for r in rows if r.get("speed_mph") is not None]
    # Trapezoidal integration of mph over seconds, converted to miles.
    dist = sum((rows[i]["speed_mph"] + rows[i - 1]["speed_mph"]) / 2
               * (rows[i]["t"] - rows[i - 1]["t"]) for i in range(1, len(rows))) / 3600.0

    # Deceleration over a single 50 Hz step divides by a tiny dt, so timing jitter alone
    # can read as tens of g. Measure over a window instead, and drop degenerate steps.
    events, peak, gaps, cur = [], 0.0, 0, None
    for i in range(1, len(rows)):
        dt = rows[i]["t"] - rows[i - 1]["t"]
        if dt > 1.0:
            gaps += 1
        j = i - 1
        while j > 0 and rows[i]["t"] - rows[j]["t"] < args.min_dt:
            j -= 1
        span = rows[i]["t"] - rows[j]["t"]
        if span < args.min_dt / 2:
            continue
        # mph/s -> g
        g = (rows[j]["speed_mph"] - rows[i]["speed_mph"]) * 0.44704 / span / 9.80665
        peak = max(peak, g)
        if g >= args.brake_g:
            # Consecutive samples above threshold are one braking event, not many.
            if cur is None:
                cur = {"t": rows[i]["t"], "g": g, "from": rows[j]["speed_mph"],
                       "to": rows[i]["speed_mph"], "brake": 0.0}
            else:
                cur["g"] = max(cur["g"], g)
                cur["to"] = rows[i]["speed_mph"]
            cur["brake"] = max(cur["brake"], max((rows[k].get("brake") or 0)
                                                 for k in range(j, i + 1)))
        elif cur is not None:
            events.append(cur)
            cur = None
    if cur is not None:
        events.append(cur)

    def frac(key, test):
        vals = [r for r in rows if r.get(key) is not None]
        return (100.0 * sum(1 for r in vals if test(r[key])) / len(vals)) if vals else 0.0

    print(f"{path.name}: {len(rows)} samples, {dur:.1f}s, ~{len(rows) / dur:.0f} Hz")
    if rebased:
        print("  NOTE: timestamps were absolute (pre-fix capture); rebased to start at 0.")
    print(f"  distance     {dist:.3f} mi")
    print(f"  speed        max {max(speeds):.1f} mph, mean {sum(speeds) / len(speeds):.1f} mph")
    print(f"  full throttle {frac('throttle', lambda v: v > 0.95):.0f}% of samples")
    print(f"  on brakes     {frac('brake', lambda v: v > 0.02):.0f}% of samples")
    print(f"  overlap       {sum(1 for r in rows if (r.get('throttle') or 0) > 0.1 and (r.get('brake') or 0) > 0.1) * 100.0 / len(rows):.0f}% "
          f"(throttle and brake together)")
    print(f"  peak decel    {peak:.2f} g  (measured over {args.min_dt}s windows)")
    print(f"  hard brakes   {len(events)} over {args.brake_g}g")
    for e in events[: args.max_events]:
        # Road tyres cannot generate much beyond ~1.5 g. Anything above that is the
        # car hitting something or being reset, whatever the brake pedal was doing --
        # a real impact often happens WHILE braking, so pedal position cannot classify.
        verdict = "IMPACT/RESET" if e["g"] > args.grip_g else (
            "braking" if e["brake"] > 0.25 else "coasting")
        print(f"     t={e['t']:7.2f}s  peak {e['g']:5.2f} g  "
              f"{e['from']:3.0f} -> {e['to']:3.0f} mph  "
              f"brake {e['brake']:.2f}  {verdict}")
    hits = sum(1 for e in events if e["g"] > args.grip_g)
    if hits:
        print(f"     ({hits} above {args.grip_g}g, beyond tyre grip - impacts, not braking)")
    if len(events) > args.max_events:
        print(f"     ... {len(events) - args.max_events} more not listed")
    if gaps:
        print(f"  NOTE: {gaps} gaps over 1s in the samples - the recording dropped out, "
              f"so distance and event counts are understated.")
    return 0


def cmd_selftest(args):
    ok = True

    print("1. OutGauge packet round trip over loopback")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = build_outgauge(ms=1234, car=b"PD01", gear=4, speed_mps=31.29,
                          rpm=4200.0, throttle=0.83, brake=0.0)
    tx.sendto(sent, ("127.0.0.1", port))
    try:
        data, _ = sock.recvfrom(4096)
        rec = decode_outgauge(data)
        assert rec, "packet did not decode"
        checks = [
            ("size", len(sent), 96),
            ("car", rec["car"], "PD01"),
            ("gear", rec["gear"], "3"),
            ("mph", round(rec["speed_mph"], 1), 70.0),
            ("throttle", round(rec["throttle"], 2), 0.83),
        ]
        for name, got, want in checks:
            good = got == want
            ok &= good
            print(f"   {'OK ' if good else 'FAIL'} {name}: {got!r}"
                  + ("" if good else f" (expected {want!r})"))
    except socket.timeout:
        ok = False
        print("   FAIL no packet received on loopback")
    finally:
        sock.close()
        tx.close()

    print("2. 92-byte packet (no trailing id) still decodes")
    r = decode_outgauge(build_outgauge(speed_mps=10.0, with_id=False))
    good = r is not None and abs(r["speed_mph"] - 22.37) < 0.05
    ok &= good
    print(f"   {'OK ' if good else 'FAIL'} {r['speed_mph']:.2f} mph" if r else "   FAIL")

    print("3. malformed packet is rejected, not misread")
    good = decode_outgauge(b"\x00" * 40) is None
    ok &= good
    print(f"   {'OK ' if good else 'FAIL'} short packet returns None")

    print("4. iRacing shared-memory reader against a synthetic block")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = build_irsdk_fixture(Path(td) / "ir.bin",
                                {"Speed": 44.7, "Throttle": 0.61, "Brake": 0.0,
                                 "SteeringWheelAngle": -0.35, "RPM": 6100.0, "Gear": 4})
        ir = IRSDK(str(f))
        opened = ir.open()
        ok &= opened
        print(f"   {'OK ' if opened else 'FAIL'} parsed header, {len(ir.vars)} channels")
        if opened:
            for name, want in (("Speed", 44.7), ("Throttle", 0.61), ("Gear", 4)):
                got = ir.get(name)
                good = got is not None and abs(float(got) - want) < 0.01
                ok &= good
                print(f"   {'OK ' if good else 'FAIL'} {name} = {got}")
        ir.close()

    print("\nSELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Record and analyse sim telemetry.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("listen", help="record OutGauge UDP (BeamNG, AC, LFS, rFactor)")
    p.add_argument("--port", type=int, default=4444)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    p.add_argument("--out", help="CSV path")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("iracing", help="sample the iRacing SDK shared memory")
    p.add_argument("--hz", type=float, default=30.0)
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--out")
    p.add_argument("--list", action="store_true", help="list channels and exit")
    p.add_argument("--mmap-file", help="read a file instead of live memory (testing)")
    p.set_defaults(func=cmd_iracing)

    p = sub.add_parser("summary", help="stats for a recorded CSV")
    p.add_argument("csv")
    p.add_argument("--brake-g", type=float, default=0.5)
    p.add_argument("--grip-g", type=float, default=1.5,
                   help="above this is an impact, not braking (default 1.5)")
    p.add_argument("--min-dt", type=float, default=0.25,
                   help="window for measuring deceleration, default 0.25s")
    p.add_argument("--max-events", type=int, default=12)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("selftest", help="verify the decoders without a running sim")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
