# Claude Code skills: let Claude see your screen, your recordings, and your sim

Claude Code can read your files and run your commands, but it cannot see your screen. So you
end up typing out what an error dialog says, describing a broken layout in prose, or
transcribing a game's HUD by hand.

These three skills close that gap on Windows, using nothing but Python 3 and `ffmpeg`.

| Skill | The problem it solves |
|---|---|
| **[screenshot](screenshot/)** | Claude cannot see your screen. Captures the desktop, one monitor, a window, or a pixel region — including fullscreen Direct3D games, which most capture tools return as a black frame. |
| **[clipwatch](clipwatch/)** | Claude cannot watch video, and naive frame extraction blows up its context. Finds the moment with a cheap contact sheet, then reads it at full resolution. |
| **[telemetry](telemetry/)** | A screenshot cannot tell you a brake trace. Records live sim driving data as CSV from BeamNG, Assetto Corsa, LFS, rFactor (OutGauge) or iRacing (SDK), and analyses it. |

No third-party Python packages. No compiled extensions. No API keys.

## Install

Clone into your user-level skills directory, and they load in every project:

```bash
git clone https://github.com/academysim/claude-code-skills.git ~/.claude/skills-public
```

Then move or copy `screenshot/`, `clipwatch/` and `telemetry/` into `~/.claude/skills/`.
Restart your Claude Code session — skills are read at startup.

Verify:

```bash
python ~/.claude/skills/screenshot/shot.py --list-monitors
python ~/.claude/skills/clipwatch/clip.py list
python ~/.claude/skills/telemetry/telemetry.py selftest
```

Each skill advertises itself through its description, so you can just say *"look at my
screen"* or *"watch that recording"* and Claude will reach for the right one.

## Requirements

- **Windows** for `screenshot` and the iRacing reader. `clipwatch` and OutGauge work anywhere.
- **Python 3.8+**
- **`ffmpeg` and `ffprobe` on `PATH`** for `screenshot` and `clipwatch`

## Things these encode that are easy to get wrong

Every one of these was measured, and several were bugs first. They are the reason the skills
are worth more than the hour it takes to write them.

**Capture fullscreen games with `ddagrab`, not `gdigrab`.** The Desktop Duplication API
captures fullscreen Direct3D; the GDI grabber commonly returns a black frame. Verified
against BeamNG.drive in D3D12 fullscreen.

**Crop, or you cannot read anything.** Images are downscaled to roughly 1568px on the long
edge. A 5120×1440 ultrawide desktop arrives at 1568×440 — you can see which app is open, not
what a dialog says. Anything up to 1568px wide arrives at 1:1.

**Two passes beat one for video.** A 4-minute 60fps clip sampled every 2s is 120 frames,
roughly 130k tokens read individually. A contact sheet costs ~2k and tells you *when*; one
cropped full-resolution frame then tells you *what*.

**Deceleration needs a time window.** Measured across a single 50 Hz step, timing jitter
alone reads as tens of g — an early build reported a confident **24 g** on a clean trace.
Measure over ~0.25s, and group consecutive over-threshold samples into one braking event, or
a single braking zone becomes 61 "events".

**A recording still being written has no `moov` atom.** It will not probe, and it looks
exactly like a corrupt file. Detect it rather than reporting a bug.

**`drawtext` silently substitutes a font** unless its path is both quoted and colon-escaped
inside the filter string. You get output either way; it is just the wrong font.

**Do not add a second capture pipeline to measure a thing.** `clipwatch` deliberately does
not record — it reads what ShadowPlay, OBS or Game Bar already wrote, after the fact, so it
cannot add GPU load to the run it is measuring.

## Configuration

- `CLAUDE_CLIPS_DIR` — where `clipwatch` looks for recordings (`os.pathsep`-separated).
  Defaults to `~/Videos/NVIDIA` and `~/Videos/Captures`, falling back to `~/Videos`.
- Output goes to `~/.claude/screenshots/`, `~/.claude/clipwatch/` and `~/.claude/telemetry/`,
  each pruned to a fixed number of recent files.
- `clipwatch` uses a Windows font path for timestamp labels; change `FONT` at the top of
  `clip.py` on other platforms.

## Status

Built and used on a real project — a BeamNG-based driving simulator — rather than written as
a demo. The iRacing SDK reader is the one component unit-tested against a synthetic memory
block rather than a live session; its skill file says so plainly.

Issues and pull requests welcome, particularly ports of `screenshot` to macOS and Linux.

## Licence

MIT
