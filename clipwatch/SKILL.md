---
name: clipwatch
description: Lets Claude watch a screen recording without blowing up its context. A 4-minute 60fps capture is thousands of frames; feeding even a fraction of them as images costs more tokens than the conversation can hold. This finds the moment first with a cheap contact sheet, then reads it at full resolution. Use for "watch that recording", "what happened in that run", "the bug shows up about a minute in", "why did it glitch", "find the moment it crashed" — anything about something that already happened on screen rather than what is on screen right now.
---

# Clipwatch

**The problem:** you have a screen recording and a question about one moment inside it.
Claude cannot watch video. Extracting frames naively is worse than useless — sampling a
4-minute clip every 2 seconds gives 120 images, which at roughly 1.1k tokens each is more
than most conversations can afford, and you still do not know which frame matters.

This does it in two passes instead.

```bash
python clipwatch/clip.py <list|sheet|zoom> [options]
```

Always Read the printed PNG paths afterwards — generating them shows Claude nothing.

## The two passes

**Pass 1 — `sheet`.** Many small timestamped frames tiled into one image, about 2k tokens
each. Tiles land around 522×146 from an ultrawide source: enough to see gross state — on
road or in a wall, dialog open or not, HUD colour, whether an overlay is drawn — and nothing
like enough to read a label. This pass answers **when**.

**Pass 2 — `zoom`.** Full-resolution frames around one timestamp, cropped to what matters.
This pass answers **what**. Anything up to 1568px wide arrives at 1:1 and small UI text is
perfectly readable.

Sheet first, always. Zooming blind wastes both passes.

## Commands

```bash
clip.py list [--limit N]
clip.py sheet [CLIP] [--start S] [--end S] [--every S] [--cols N] [--crop X,Y,W,H]
clip.py zoom  [CLIP] --at S [--window S] [--every S] [--crop X,Y,W,H]
```

`CLIP` is a path, a filename substring, or `latest` — the default. Substring and `latest`
both skip clips that are still being recorded.

Defaults: `sheet` samples **every 2s** across the whole clip, 24 tiles per sheet, up to 6
sheets. `zoom` takes a 2s span around `--at` every 0.5s.

## It does not record

Recording is left to whatever already records — NVIDIA ShadowPlay, OBS, Xbox Game Bar. That
is deliberate: a second capture pipeline would add GPU load during the very run you are
trying to measure, and everything here works on finished files afterwards, so it can never
perturb what it is measuring.

By default it looks in `~/Videos/NVIDIA` and `~/Videos/Captures`, falling back to
`~/Videos`. Point it anywhere with the **`CLAUDE_CLIPS_DIR`** environment variable
(`os.pathsep`-separated for several roots).

## Cropping

`--crop X,Y,W,H` is in source-frame pixels, and it is what makes both passes useful.

On `zoom` it is the difference between readable and not. On `sheet` it is worth using when
you already know which corner matters — cropping to a HUD panel before tiling gives 24 tiles
of readable panel instead of 24 tiles of mostly sky.

To find a region: sheet the full frame, estimate the area's fraction of it, convert to
pixels, then refine once.

## Things that will bite

- **A clip still being recorded has no `moov` atom** and will not probe. `list` flags it as
  `RECORDING IN PROGRESS`; `latest` skips past it. Stop the recording first. This looks
  exactly like a corrupt file, so check for it before calling anything broken.
- **Extraction costs roughly a second per frame** on high-resolution HEVC. A full 4-minute
  clip at 2s is about two minutes of work. Narrow with `--start`/`--end` when you already
  know roughly when.
- **The 6-sheet cap thins the sampling** rather than truncating, and says so. If you see that
  note, coverage is no longer every 2s — narrow the range instead of accepting it.
- `drawtext` needs its font path quoted and colon-escaped, or ffmpeg silently substitutes a
  different font. That is already handled; do not "simplify" the filter string.

## Reading sheets honestly

Tiles are small. An overlay that stops appearing may have been discarded, or the camera may
simply have moved past it. Treat a sheet as a pointer to a timestamp, then confirm with
`zoom` before reporting it as a finding.

## Requirements

Python 3.8+ and `ffmpeg`/`ffprobe` on `PATH`. No third-party Python packages. The timestamp
labels use a Windows font path by default; change `FONT` at the top of `clip.py` on other
platforms.
