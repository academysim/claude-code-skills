---
name: screenshot
description: Claude cannot see your screen — this gives it eyes. Captures the Windows desktop, one monitor, a named window, or a pixel region to a PNG that Claude then reads. Use when the user says "look at this", "what's on my screen", "see this error", "read this dialog", "why does my UI look wrong", "what does this say" — or whenever they are describing something visible on screen that would be faster to simply look at. Works with fullscreen Direct3D games, which most screenshot tools capture as a black frame.
---

# Screenshot

**The problem:** Claude Code reads files and runs commands, but it cannot see your screen.
So you end up typing out what an error dialog says, describing a broken layout, or
transcribing a game's HUD by hand — slowly, and with transcription mistakes.

This captures the screen to a PNG that Claude reads directly.

```bash
python screenshot/shot.py [options]
```

Always Read the printed path afterwards — capturing alone shows Claude nothing.

## Options

| Option | Purpose |
|---|---|
| *(none)* | whole desktop |
| `--monitor N` | monitor N, `0` = primary |
| `--window TEXT` | window whose title contains TEXT, case-insensitive |
| `--region X,Y,W,H` | explicit pixel region |
| `--delay SEC` | wait before capturing, so you can alt-tab first |
| `--list-windows` | visible windows with titles and rects |
| `--list-monitors` | monitors with sizes and positions |
| `--method dda\|gdi` | force a capture backend, default `auto` |
| `--max-edge PX` | long-edge downscale, default 1568 |

## Crop, or the text will be unreadable

Claude downscales images to roughly **1568px on the long edge**. On a 1920×1080 display a
full-desktop grab arrives at 1568×882, which is usually fine. On an ultrawide it is far
worse: a 5120×1440 desktop lands at 1568×440, enough to see *which* app is open and nothing
like enough to read a dialog.

- **Reading text?** Crop to it — `--window` for a titled window, `--region` for a panel or
  HUD element inside a fullscreen app.
- **General "what is going on"?** Full desktop is fine.
- Unsure what is open? `--list-windows` first, then crop to the rect it reports.

Any capture up to 1568px wide is sent at full resolution with no downscaling at all.

## Regions inside a fullscreen game

Game HUDs and debug panels are not separate windows, so `--window` cannot reach them. Take a
full-desktop shot first, estimate the panel's fraction of the frame, convert to pixels, then
`--region` that box. One refinement pass is normal.

## Backends, and why the order matters

`auto` tries **ddagrab** (Desktop Duplication API) first and falls back to **gdigrab**.

This ordering is the whole trick. ddagrab captures fullscreen Direct3D games — verified
against BeamNG.drive in D3D12 fullscreen — where gdigrab commonly returns a black frame.
gdigrab is the fallback for sessions where DXGI is unavailable, such as some remote-desktop
connections.

ddagrab cannot capture a region spanning two monitors; the script detects that and falls
back automatically.

## Notes

- Monitor layouts change between sessions on machines with docking or profile switchers.
  Never assume a layout from an earlier conversation — run `--list-monitors` if geometry
  matters.
- PNGs go to `~/.claude/screenshots/`, last 25 kept, older ones deleted automatically.
- Capture costs a little GPU time. If a benchmark or stability measurement is running that
  is sensitive to GPU load, say so before capturing repeatedly.
- This is a still frame on demand, not live video. Claude cannot watch you work in real
  time; each capture is a round trip of a few seconds. For "what happened during that run",
  record a clip and analyse it afterwards — see the `clipwatch` skill.

## Requirements

Windows, Python 3.8+, and `ffmpeg` on `PATH`. No third-party Python packages.
