#!/usr/bin/env python3
"""Capture the Windows screen (or one monitor / window / region) to a PNG for Claude to read.

Captures via ffmpeg ddagrab (Desktop Duplication API) so fullscreen D3D games work,
falling back to gdigrab when DXGI is unavailable (e.g. some remote-desktop sessions).
Crop and downscale happen in the same ffmpeg pass.
"""
import argparse
import ctypes
import ctypes.wintypes as w
import subprocess
import sys
import time
from pathlib import Path

OUT_DIR = Path.home() / ".claude" / "screenshots"
KEEP = 25
# Claude downscales images to ~1568px on the long edge; sending more is wasted bytes.
MAX_EDGE = 1568

# Window titles routinely contain emoji, which the default cp1252 console cannot encode.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Physical pixels, not DPI-scaled logical ones.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def monitors():
    """Visible monitors as (x, y, w, h) in virtual-desktop coordinates, primary first."""
    found = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM
    )

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT),
                    ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        found.append({
            "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
            "primary": bool(mi.dwFlags & 1),
        })
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(cb), 0)
    found.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    return [m["rect"] for m in found]


def windows():
    """Visible top-level windows with a title, as (title, (x, y, w, h))."""
    out = []
    ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, w.HWND, w.LPARAM)

    def cb(hwnd, lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return 1
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return 1
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        r = w.RECT()
        # Extended frame bounds excludes the invisible resize border Win10/11 adds.
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)
        ) != 0:
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        if rect[2] > 0 and rect[3] > 0:
            out.append((buf.value, rect))
        return 1

    ctypes.windll.user32.EnumWindows(ENUMPROC(cb), 0)
    return out


def clamp(rect, bounds):
    """Clip rect to bounds; both are (x, y, w, h). Returns None if they do not overlap."""
    x1 = max(rect[0], bounds[0])
    y1 = max(rect[1], bounds[1])
    x2 = min(rect[0] + rect[2], bounds[0] + bounds[2])
    y2 = min(rect[1] + rect[3], bounds[1] + bounds[3])
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def scaled(width, height, max_edge):
    if max(width, height) <= max_edge:
        return None
    f = max_edge / max(width, height)
    # Even dimensions keep every encoder happy.
    return (max(2, int(width * f) // 2 * 2), max(2, int(height * f) // 2 * 2))


def run_ffmpeg(source_args, vf, dest):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *source_args]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-frames:v", "1", "-y", str(dest)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode == 0 and dest.exists() and dest.stat().st_size > 0, p.stderr.strip()


def capture(target, mons, method, dest, max_edge):
    """Capture `target` (virtual coords). Returns (ok, method_used, error)."""
    errors = []

    if method in ("auto", "dda"):
        # ddagrab captures one monitor, with that monitor's own origin at 0,0.
        host = next((i for i, m in enumerate(mons) if clamp(target, m) == target), None)
        if host is None:
            errors.append("target spans monitors; ddagrab cannot cross them")
        else:
            mx, my, mw, mh = mons[host]
            local = (target[0] - mx, target[1] - my, target[2], target[3])
            vf = ["hwdownload", "format=bgra"]
            if (local[2], local[3]) != (mw, mh):
                vf.append(f"crop={local[2]}:{local[3]}:{local[0]}:{local[1]}")
            s = scaled(target[2], target[3], max_edge)
            if s:
                vf.append(f"scale={s[0]}:{s[1]}:flags=lanczos")
            ok, err = run_ffmpeg(
                ["-f", "lavfi", "-i", f"ddagrab=output_idx={host}"], vf, dest
            )
            if ok:
                return True, "ddagrab", None
            errors.append(f"ddagrab: {err.splitlines()[-1] if err else 'failed'}")

    if method in ("auto", "gdi"):
        # gdigrab takes virtual-desktop coordinates directly, so no monitor mapping.
        vf = []
        s = scaled(target[2], target[3], max_edge)
        if s:
            vf.append(f"scale={s[0]}:{s[1]}:flags=lanczos")
        ok, err = run_ffmpeg(
            ["-f", "gdigrab",
             "-offset_x", str(target[0]), "-offset_y", str(target[1]),
             "-video_size", f"{target[2]}x{target[3]}", "-i", "desktop"],
            vf, dest,
        )
        if ok:
            return True, "gdigrab", None
        errors.append(f"gdigrab: {err.splitlines()[-1] if err else 'failed'}")

    return False, None, "; ".join(errors)


def main():
    ap = argparse.ArgumentParser(description="Screenshot the Windows desktop for Claude.")
    ap.add_argument("--monitor", type=int, metavar="N", help="capture monitor N (0 = primary)")
    ap.add_argument("--window", metavar="TEXT", help="capture the window whose title contains TEXT")
    ap.add_argument("--region", metavar="X,Y,W,H", help="capture an explicit region")
    ap.add_argument("--delay", type=float, default=0, metavar="SEC", help="wait before capturing")
    ap.add_argument("--method", choices=["auto", "dda", "gdi"], default="auto")
    ap.add_argument("--max-edge", type=int, default=MAX_EDGE,
                    help=f"downscale long edge to this (default {MAX_EDGE})")
    ap.add_argument("--list-windows", action="store_true", help="list visible windows and exit")
    ap.add_argument("--list-monitors", action="store_true", help="list monitors and exit")
    args = ap.parse_args()

    mons = monitors()
    if not mons:
        print("ERROR: no monitors detected", file=sys.stderr)
        return 2

    if args.list_monitors:
        for i, (x, y, mw, mh) in enumerate(mons):
            print(f"{i}: {mw}x{mh} at {x},{y}" + ("  (primary)" if i == 0 else ""))
        return 0

    if args.list_windows:
        for title, (x, y, ww, hh) in sorted(windows(), key=lambda t: -t[1][2] * t[1][3]):
            print(f"{ww:>5}x{hh:<5} at {x:>6},{y:<6}  {title}")
        return 0

    # Virtual desktop bounding box, used as the default target and as a clip region.
    vx = min(m[0] for m in mons)
    vy = min(m[1] for m in mons)
    virtual = (vx, vy,
               max(m[0] + m[2] for m in mons) - vx,
               max(m[1] + m[3] for m in mons) - vy)

    label = "desktop"
    if args.window:
        needle = args.window.lower()
        hits = [t for t in windows() if needle in t[0].lower()]
        if not hits:
            print(f"ERROR: no visible window matching {args.window!r}. "
                  f"Run with --list-windows to see what is open.", file=sys.stderr)
            return 3
        # Largest match wins; a substring often hits a tooltip or tray window too.
        title, target = max(hits, key=lambda t: t[1][2] * t[1][3])
        label = f"window {title!r}"
        if len(hits) > 1:
            label += f" ({len(hits)} matched, largest chosen)"
    elif args.region:
        try:
            nums = [int(v) for v in args.region.split(",")]
            if len(nums) != 4:
                raise ValueError
            target = tuple(nums)
        except ValueError:
            print("ERROR: --region wants X,Y,W,H (four integers)", file=sys.stderr)
            return 3
        label = f"region {target}"
    elif args.monitor is not None:
        if not 0 <= args.monitor < len(mons):
            print(f"ERROR: monitor {args.monitor} out of range; {len(mons)} detected",
                  file=sys.stderr)
            return 3
        target = mons[args.monitor]
        label = f"monitor {args.monitor}"
    else:
        target = virtual

    # An offscreen or minimised window yields a bogus rect; clipping catches it.
    clipped = clamp(target, virtual)
    if clipped is None:
        print(f"ERROR: {label} is entirely offscreen (rect {target}); "
              f"it may be minimised.", file=sys.stderr)
        return 3
    if clipped != target:
        label += " (clipped to screen)"
        target = clipped

    if args.delay > 0:
        time.sleep(args.delay)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / f"shot_{time.strftime('%Y%m%d_%H%M%S')}.png"

    ok, used, err = capture(target, mons, args.method, dest, args.max_edge)
    if not ok:
        dest.unlink(missing_ok=True)
        print(f"ERROR: capture failed. {err}", file=sys.stderr)
        return 1

    # Keep the directory from growing without bound.
    for old in sorted(OUT_DIR.glob("shot_*.png"))[:-KEEP]:
        old.unlink(missing_ok=True)

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dest)],
        capture_output=True, text=True,
    ).stdout.strip()
    print(f"{dest}\ncaptured {label} via {used} | source {target[2]}x{target[3]} "
          f"| saved {out} | {dest.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
