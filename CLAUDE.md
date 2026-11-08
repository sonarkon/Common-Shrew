# CLAUDE.md – Common Shrew

Context for Claude (or anyone) working on this repo from any machine.

## What this is
**Common Shrew** is a macOS desktop tool (Python + Tkinter, ffmpeg underneath) that compresses
videos so each file ends up just below a target size (default **5 GB**). It follows Vimeo's
upload recommendations and offers size presets for other platforms. It's named after the
common shrew, which shrinks in winter (Dehnel's phenomenon). Hovering the title shows a
tooltip about this, and clicking it opens https://en.wikipedia.org/wiki/Common_shrew.
A "Support on Ko-fi" button in the header opens `KOFI_URL`
(https://ko-fi.com/S7Z228X1UL). The README shows the same Ko-fi badge at the top and in
"Support".

Owner: Jost. Conversation language with him is **German**. The app UI, code, comments,
README and commit messages are **English**.

## Files
| Path | Purpose |
|---|---|
| `common_shrew.py` | The whole app in one file: analysis, planning, ffmpeg commands, GUI, CLI |
| `Common Shrew.command` | Double-click launcher for macOS. It asks y/n before installing Homebrew, ffmpeg, python-tk and `tkinterdnd2` into `.venv/`, then starts the app |
| `tools/make_samples.sh` | Generates synthetic test clips (PCM audio, 120 fps without audio, scope, portrait) into `samples/` |
| `tools/gui_screenshot.py` | Headless GUI smoke test plus screenshot |
| `docs/screenshot.png` | Screenshot used in the README |

Git-ignored: `.venv/`, `samples/`, video files, and the launcher flags `.use_system_tk` and
`.no_dnd`.

## Architecture (`common_shrew.py`, top to bottom)
1. **Spec constants:**
   - `VIMEO_MAX_KBPS`: bitrate ceilings by pixel count.
   - `RESOLUTIONS`: bounding boxes.
   - `CODECS`, `SIZE_PRESETS`, `PRESET_SHORT`.
   - `parse_size_gb()`: decimal units, 1 GB = 1000³ bytes.
2. **`probe()` → `MediaInfo`:** reads source data via ffprobe JSON: display dimensions, SAR,
   rotation, VFR, interlacing, HDR, audio codec and bitrate.
3. **`make_plan(info, Settings)` → `Plan`:** decides output size, fps, video and audio
   bitrate, and collects human-readable `notes`.
   - Size: fits inside a box, never crops, never upscales; portrait sources use the
     rotated box.
   - Frame rate: VFR or unusual rates snap to a standard rate; anything above 60 fps is
     divided down.
   - Bitrate: budget = target × (1 − safety). The cap is the minimum of the budget, the
     Vimeo ceiling and the source bitrate (scaled by pixel ratio^0.75).
   - Audio: small targets lower the audio bitrate automatically; `audio_copy` uses the
     source audio bitrate instead.
4. **`build_cmd(plan, settings, out, pass_no)`:**
   - Pass 1 → null muxer.
   - Pass 2 / single pass → final file with `-progress pipe:1`.
   - Default codec is **x265** (preset `medium`), because size-limited encodes benefit most
     from HEVC efficiency. x265 two-pass runs through `-x265-params pass=N:stats=…`, and pass 1
     uses `slow-firstpass=0` for speed.
   - Colour tags come from the source and fall back to BT.709.
   - A silent `anullsrc` track is added when the source has no audio.
5. **`Job.run()`:**
   - Runs the passes and parses `out_time_us` for progress.
   - Cancelling deletes the partial output.
   - Logs the reduction against the source.
6. **GUI (`run_gui`):**
   - Uses the ttk **`clam`** theme with a custom dark palette `C`. The native aqua theme
     drew blank buttons on macOS, so don't switch back.
   - Background threads only push messages into a `queue.Queue`; `pump()` applies them on
     the Tk thread. Never touch widgets from a worker thread.
   - Thumbnails: ffmpeg writes PNG to a pipe, base64 → `tk.PhotoImage`. Requires Tk 8.6+.
   - `Tooltip`: light colours. On macOS it uses `MacWindowStyle help` instead of
     `overrideredirect`, because the latter rendered black there.
   - Drag & drop is optional via `tkinterdnd2`; the app falls back to plain `tk.Tk()`.
7. **CLI:** `python3 common_shrew.py --cli …`, with `--dry-run` to print the plan and the
   ffmpeg command.

## Dev & test
```sh
brew install ffmpeg python-tk                # macOS (Linux: apt install ffmpeg python3-tk)
python3 -m venv .venv && .venv/bin/pip install tkinterdnd2
tools/make_samples.sh                        # -> samples/
python3 -m py_compile common_shrew.py

# logic/encoding without GUI
python3 common_shrew.py --cli samples/scope.mov --res 1080 --dry-run    # expect 1920x804
python3 common_shrew.py --cli samples/hfr_noaudio.mp4 --size 10MB --preset ultrafast
python3 common_shrew.py --cli samples/pcm24.mov --size 3MB --copy-audio  # expect .mov output

# GUI
.venv/bin/python common_shrew.py samples/*
xvfb-run -a -s "-screen 0 1220x930x24" .venv/bin/python tools/gui_screenshot.py /tmp/s.png samples/*
```
- Check the result sizes stay ≤ target. Two-pass x264/x265 should land within the 2 %
  margin.
- `*_videotoolbox` encoders exist only on macOS, so test them on a Mac.
- In headless Linux screenshots, glyphs like ≈ → ✓ ▾ may be missing from the fonts. They
  render fine on macOS.
- Linux without python3-tk: `uv python install 3.12` gives a standalone Python with Tk 9.

## Conventions
- Keep it a **single-file app** with no dependencies beyond the standard library, except
  the optional `tkinterdnd2`.
- Don't break Python 3.9, macOS's system python3: no `match` statements and no
  `X | Y` type hints.
- Every new option needs:
  - a `Settings` field,
  - a GUI widget plus a `TIPS` entry (every option has a tooltip),
  - a CLI flag where it makes sense,
  - a refresh trigger in the `for k in (...)` list if it affects estimates.
- Platform limits in `SIZE_PRESETS` / `PRESET_SHORT` are dated (Nov 2026). Update both
  together, and update the README.
- Sizes are always decimal (GB = 10⁹ bytes) – in the UI, the README and the code.
- Output naming: `<name><suffix>.mp4` (default suffix `_vimeo`), or `.mov` when PCM audio is
  copied. Never overwrite sources.

## Ideas / open points
- No license chosen yet – ask Jost before publishing.
- Possible next steps:
  - per-file settings;
  - remembering settings between runs;
  - an app bundle (py2app/Platypus) instead of the `.command` file;
  - duration warnings for platform presets (e.g. X: 140 s).
