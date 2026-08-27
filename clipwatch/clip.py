#!/usr/bin/env python3
"""Digest an existing screen recording so Claude can look at it.

Two passes, because a 4-minute 60fps clip sampled at 2s is 120 frames and far too
many tokens to read one by one:

  sheet  - tile many small timestamped frames into one image, to find WHEN
  zoom   - pull full-resolution frames around a timestamp, to read WHAT

Recording is left to whatever already records (NVIDIA ShadowPlay), so nothing here
adds GPU load while a sim is running.
"""
import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT_DIR = Path.home() / ".claude" / "clipwatch"
FONT = "C:/Windows/Fonts/consola.ttf"
MAX_EDGE = 1568          # Claude downscales past this; sending more is wasted bytes.
TILES_PER_SHEET = 24
DEFAULT_EVERY = 2.0      # Incidents get missed above a 2s sampling interval.
MAX_SHEETS = 6
KEEP = 60
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi"}

def clip_roots():
    """Where recordings live, most specific first.

    Override with CLAUDE_CLIPS_DIR (os.pathsep-separated). Otherwise look in the usual
    capture folders, falling back to Videos itself so OBS and manual saves are found.
    """
    env = os.environ.get("CLAUDE_CLIPS_DIR")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    home = Path.home()
    known = [home / "Videos" / "NVIDIA",     # ShadowPlay
             home / "Videos" / "Captures"]   # Xbox Game Bar
    found = [p for p in known if p.is_dir()]
    return found or [home / "Videos"]


def iter_clips():
    """Every recording under every root, newest first, deduped across overlapping roots."""
    seen = {}
    for root in clip_roots():
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in VIDEO_EXT and p.is_file():
                seen.setdefault(p.resolve(), p)
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode == 0, (p.stderr or "").strip()


def probe(path):
    """Video properties, or None if the file has no readable moov atom."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,codec_name",
         "-show_entries", "format=duration", "-of", "default=nw=1", str(path)],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return None
    info = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    try:
        return {
            "width": int(info["width"]),
            "height": int(info["height"]),
            "duration": float(info["duration"]),
            "codec": info.get("codec_name", "?"),
            "fps": info.get("r_frame_rate", "?"),
        }
    except (KeyError, ValueError):
        return None


def all_clips():
    """Every recording under the ShadowPlay tree, newest first."""
    return iter_clips()


def resolve(spec):
    """Turn 'latest', a game-name substring, or a path into one clip path."""
    if spec and Path(spec).is_file():
        return Path(spec), None
    clips = all_clips()
    if not clips:
        return None, ("no recordings found under " +
                      ", ".join(str(r) for r in clip_roots()) +
                      ". Set CLAUDE_CLIPS_DIR to point somewhere else.")
    if not spec or spec.lower() == "latest":
        pool = clips
        what = "latest recording"
    else:
        needle = spec.lower()
        pool = [c for c in clips if needle in str(c).lower()]
        what = f"latest clip matching {spec!r}"
        if not pool:
            return None, (f"no recording matches {spec!r}. "
                          f"Run 'list' to see what is available.")
    # A clip still being written has no moov atom; skip to the newest finished one.
    for c in pool:
        if probe(c):
            return c, None
    return None, (f"{what} is still being recorded (no moov atom yet) and no finished "
                  f"clip matches. Stop the recording in ShadowPlay, then retry.")


def label_for(t):
    """Timestamp with no colons, which would otherwise need escaping in the filter."""
    if t < 60:
        return f"{t:.1f}s"
    return f"{int(t // 60)}m{t % 60:04.1f}s"


def escape_filter_text(s):
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def extract(clip, t, dest, crop=None, scale=None, label=None):
    """One frame at time t. Input-side -ss keeps seeking fast and still frame-accurate."""
    vf = []
    if crop:
        vf.append(f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}")
    if scale:
        vf.append(f"scale={scale[0]}:{scale[1]}:flags=lanczos")
    if label:
        size = max(14, int((scale[1] if scale else 200) * 0.16))
        vf.append(
            f"drawtext=fontfile='{FONT.replace(':', chr(92) + ':')}'"
            f":text='{escape_filter_text(label)}'"
            f":x=6:y=6:fontsize={size}:fontcolor=white"
            f":box=1:boxcolor=black@0.65:boxborderw=4"
        )
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    tail = ["-i", str(clip), "-frames:v", "1", "-y", str(dest)]
    if vf:
        tail = tail[:2] + ["-vf", ",".join(vf)] + tail[2:]
    # Hardware decode helps a lot on 5120x1440 HEVC, but is not present everywhere.
    for accel in (["-hwaccel", "auto"], []):
        ok, err = run(base + accel + ["-ss", f"{t:.3f}"] + tail)
        if ok and dest.exists() and dest.stat().st_size > 0:
            return True, None
    return False, err


def tile_sheet(frames, cols, dest, tile_w, tile_h):
    """Tile PNGs into a grid, padding the last sheet so the tile filter completes."""
    rows = math.ceil(len(frames) / cols)
    work = frames[0].parent / "seq"
    work.mkdir(exist_ok=True)
    for i, f in enumerate(frames):
        shutil.copy(f, work / f"f{i:04d}.png")
    # tile= emits nothing until cols*rows inputs arrive, so top up with black.
    blank = work / "blank.png"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={tile_w}x{tile_h}", "-frames:v", "1", "-y", str(blank)])
    for i in range(len(frames), cols * rows):
        shutil.copy(blank, work / f"f{i:04d}.png")
    ok, err = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-framerate", "1", "-i", str(work / "f%04d.png"),
        "-vf", f"tile={cols}x{rows}:margin=4:padding=3:color=#202020",
        "-frames:v", "1", "-y", str(dest),
    ])
    shutil.rmtree(work, ignore_errors=True)
    return ok, err


def parse_crop(s, info):
    if not s:
        return None
    try:
        nums = [int(v) for v in s.split(",")]
        if len(nums) != 4:
            raise ValueError
    except ValueError:
        sys.exit("ERROR: --crop wants X,Y,W,H (four integers)")
    x, y, cw, ch = nums
    cw = min(cw, info["width"] - x)
    ch = min(ch, info["height"] - y)
    if cw <= 0 or ch <= 0 or x < 0 or y < 0:
        sys.exit(f"ERROR: --crop {s} falls outside the {info['width']}x{info['height']} frame")
    return (x, y, cw, ch)


def prune():
    files = sorted(OUT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)


def cmd_list(args):
    clips = all_clips()
    if not clips:
        print("no recordings under " + ", ".join(str(r) for r in clip_roots()))
        return 1
    for c in clips[: args.limit]:
        info = probe(c)
        rel = next((c.relative_to(r) for r in clip_roots() if r in c.parents), c)
        if info is None:
            print(f"  [RECORDING IN PROGRESS - not readable yet]  {rel}")
        else:
            mins = int(info["duration"] // 60)
            secs = info["duration"] % 60
            print(f"  {mins}m{secs:04.1f}s  {info['width']}x{info['height']} "
                  f"{info['codec']}  {c.stat().st_size // (1024 * 1024)} MB  {rel}")
    return 0


def cmd_sheet(args):
    clip, err = resolve(args.clip)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 3
    info = probe(clip)
    crop = parse_crop(args.crop, info)
    src_w, src_h = (crop[2], crop[3]) if crop else (info["width"], info["height"])

    start = max(0.0, args.start)
    end = min(info["duration"], args.end if args.end is not None else info["duration"])
    if end <= start:
        print(f"ERROR: empty time range {start}..{end} (clip is "
              f"{info['duration']:.1f}s long)", file=sys.stderr)
        return 3

    every = args.every
    times = []
    t = start
    while t < end:
        times.append(t)
        t += every
    if not times:
        times = [start]

    sheets_needed = math.ceil(len(times) / TILES_PER_SHEET)
    capped = False
    if sheets_needed > args.max_sheets:
        keep = args.max_sheets * TILES_PER_SHEET
        step = len(times) / keep
        times = [times[int(i * step)] for i in range(keep)]
        capped = True

    cols = args.cols
    tile_w = max(2, int(MAX_EDGE / cols) // 2 * 2)
    tile_h = max(2, int(tile_w * src_h / src_w) // 2 * 2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    written = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for s_idx in range(math.ceil(len(times) / TILES_PER_SHEET)):
            chunk = times[s_idx * TILES_PER_SHEET:(s_idx + 1) * TILES_PER_SHEET]
            frames = []
            for i, ft in enumerate(chunk):
                fp = tmp / f"s{s_idx}_{i:04d}.png"
                ok, e = extract(clip, ft, fp, crop, (tile_w, tile_h), label_for(ft))
                if ok:
                    frames.append(fp)
                else:
                    print(f"  warning: no frame at {label_for(ft)} ({e or 'failed'})",
                          file=sys.stderr)
            if not frames:
                continue
            dest = OUT_DIR / f"sheet_{stamp}_{s_idx + 1}.png"
            ok, e = tile_sheet(frames, cols, dest, tile_w, tile_h)
            if ok:
                written.append((dest, chunk[0], chunk[-1], len(frames)))
            else:
                print(f"ERROR: tiling failed. {e}", file=sys.stderr)

    if not written:
        print("ERROR: no frames could be extracted", file=sys.stderr)
        return 1

    prune()
    print(f"clip: {clip}")
    print(f"      {info['width']}x{info['height']} {info['codec']} "
          f"{info['duration']:.1f}s | sampling {start:.1f}..{end:.1f}s every {every}s"
          + (f" | cropped to {crop[2]}x{crop[3]} at {crop[0]},{crop[1]}" if crop else ""))
    if capped:
        print(f"      NOTE: {args.max_sheets}-sheet cap hit; sampling thinned to "
              f"{len(times)} frames spread evenly. Narrow with --start/--end for full "
              f"{every}s coverage.")
    for dest, t0, t1, n in written:
        print(f"{dest}\n      tiles {label_for(t0)}..{label_for(t1)} "
              f"({n} frames, {tile_w}x{tile_h} each)")
    return 0


def cmd_zoom(args):
    clip, err = resolve(args.clip)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 3
    info = probe(clip)
    crop = parse_crop(args.crop, info)
    src_w, src_h = (crop[2], crop[3]) if crop else (info["width"], info["height"])

    scale = None
    if max(src_w, src_h) > MAX_EDGE:
        f = MAX_EDGE / max(src_w, src_h)
        scale = (max(2, int(src_w * f) // 2 * 2), max(2, int(src_h * f) // 2 * 2))

    half = args.window / 2
    times, t = [], max(0.0, args.at - half)
    stop = min(info["duration"], args.at + half)
    while t <= stop + 1e-6:
        times.append(t)
        t += args.every
    if not times:
        times = [min(max(0.0, args.at), info["duration"])]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    written = []
    for i, ft in enumerate(times):
        dest = OUT_DIR / f"zoom_{stamp}_{i:02d}_{label_for(ft).replace('.', 'p')}.png"
        ok, e = extract(clip, ft, dest, crop, scale, None)
        if ok:
            written.append((dest, ft))
        else:
            print(f"  warning: no frame at {label_for(ft)} ({e or 'failed'})",
                  file=sys.stderr)

    if not written:
        print("ERROR: no frames could be extracted", file=sys.stderr)
        return 1

    prune()
    print(f"clip: {clip}")
    print(f"      {info['width']}x{info['height']} {info['codec']} {info['duration']:.1f}s"
          + (f" | cropped to {crop[2]}x{crop[3]} at {crop[0]},{crop[1]}" if crop else "")
          + (f" | scaled to {scale[0]}x{scale[1]}" if scale else " | full resolution"))
    if scale:
        print("      NOTE: downscaled to fit. Add --crop to read small text at 1:1.")
    for dest, ft in written:
        print(f"{dest}   t={label_for(ft)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Digest a screen recording for Claude.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list recordings, newest first")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("sheet", help="contact sheet of timestamped frames (find WHEN)")
    p.add_argument("clip", nargs="?", default="latest",
                   help="path, game-name substring, or 'latest' (default)")
    p.add_argument("--start", type=float, default=0.0, metavar="SEC")
    p.add_argument("--end", type=float, default=None, metavar="SEC")
    p.add_argument("--every", type=float, default=DEFAULT_EVERY, metavar="SEC")
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--crop", metavar="X,Y,W,H", help="crop each frame before tiling")
    p.add_argument("--max-sheets", type=int, default=MAX_SHEETS)
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("zoom", help="full-resolution frames around a moment (read WHAT)")
    p.add_argument("clip", nargs="?", default="latest")
    p.add_argument("--at", type=float, required=True, metavar="SEC")
    p.add_argument("--window", type=float, default=2.0, metavar="SEC",
                   help="total span centred on --at (default 2)")
    p.add_argument("--every", type=float, default=0.5, metavar="SEC")
    p.add_argument("--crop", metavar="X,Y,W,H")
    p.set_defaults(func=cmd_zoom)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
