# CLAUDE.md – Common Shrew

Context for Claude (or anyone) working on this repo from any machine.

## What this is
**Common Shrew** is a cross-platform desktop tool for macOS, Windows and Linux
(Python + Tkinter, ffmpeg underneath) that compresses
videos so each file ends up just below a target size (default **5 GB**). It follows Vimeo's
upload recommendations and offers size presets for other platforms. It's named after the
common shrew, which shrinks in winter (Dehnel's phenomenon). Hovering the title shows a
tooltip about this, and clicking it opens https://en.wikipedia.org/wiki/Common_shrew.
A "Support on Ko-fi" button in the header opens `KOFI_URL`
(https://ko-fi.com/S7Z228X1UL). The README shows the same Ko-fi badge at the top and in
"Support".

Owner: Jost (GitHub: **sonarkon**, repo `sonarkon/Common-Shrew`). License: **MIT**, © 2026 sonarkon. Conversation language with him is **German**. The app UI, code, comments,
README and commit messages are **English**.

## Files
| Path | Purpose |
|---|---|
| `common_shrew.py` | The whole app in one file: analysis, planning, ffmpeg commands, GUI, CLI |
| `Common Shrew.command` | Double-click launcher for macOS. It asks y/n before installing Homebrew, ffmpeg, python-tk and `tkinterdnd2` into `.venv/`, then starts the app |
| `Common Shrew.bat` | Windows launcher: Python and ffmpeg via winget (on request), `tkinterdnd2` in `.venv\`, then starts with `pythonw`/`pyw` so no console window appears. CRLF line endings come from `.gitattributes` |
| `common-shrew.sh` | Linux launcher: ffmpeg and Tkinter via apt, dnf, pacman or zypper (`sudo`, on request), `tkinterdnd2` in `.venv/` |
| `tools/make_samples.sh` | Generates synthetic test clips (PCM audio, 120 fps without audio, scope, portrait) into `samples/` |
| `tools/gui_screenshot.py` | Headless GUI smoke test plus screenshot |
| `docs/screenshot.png` | Screenshot used in the README |
| `docs/header.jpg` | README header banner, 1280×400 rendered at 2× (from `docs/banner/header.html`). Both banners use the light "book page" style: paper `#fefeca`, black text and rust accent. Keep the project's banners in this style; the app itself stays dark |
| `docs/social-preview.png` | GitHub social preview banner, 1280×640. Built from `docs/banner/` (HTML + Playwright); the shrew engraving is public domain (EB9, 1886) and its credit must stay on the banner and in the README |

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
   - Replacement audio: `probe_audio()` reads the external file, and `with_audio(info, a)`
     returns a MediaInfo copy with `audio_path` / `audio_duration` set. `make_plan` then
     writes the length check into `notes`.
     - Lengths within `AUDIO_LEN_TOL` (0.5 s) count as matching.
     - `Plan.audio_copy` is the final copy decision: copying is dropped when the
       replacement is shorter than the video, because padding needs re-encoding.
4. **`build_cmd(plan, settings, out, pass_no)`:**
   - Pass 1 → null muxer.
   - Pass 2 / single pass → final file with `-progress pipe:1`.
   - Default codec is **x265** (preset `medium`), because size-limited encodes benefit most
     from HEVC efficiency. x265 two-pass runs through `-x265-params pass=N:stats=…`, and pass 1
     uses `slow-firstpass=0` for speed.
   - Colour tags come from the source and fall back to BT.709.
   - A silent `anullsrc` track is added when the source has no audio.
   - `Settings.video_copy` ("Don't re-encode video") takes a separate, single-pass path:
     `_plan_video_copy` and `_build_cmd_video_copy`.
     - The video is `-c:v copy` (HEVC gets the `hvc1` tag in MP4); there is no target-size
       enforcement, only a warning note.
     - Codecs outside `MP4_VIDEO_OK` go to `.mov`.
     - Both builders share `_audio_args`.
     - The GUI greys out codec, preset, resolution, fps, the video checkboxes, deinterlace
       and keyframes.
   - With replacement audio, input 1 is that file (mapped `1:a:0`). Encoded audio gets
     `-af apad`, then `-shortest`, so the output always has the video's length: longer
     audio is cut, shorter audio is padded with silence. Copied audio gets `-shortest`
     only. There is no offset/sync option yet.
   - Hardware decoding: `-hwaccel videotoolbox` is used when `Settings.hw_decode` is on
     (the default) and `hwdec_available()` is true, which only happens on macOS. If a run
     fails with it, `Job.run()` retries once with software decoding. Encoding stays
     x265/x264 unless a VideoToolbox codec is chosen.
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
   - Preview hover-scrub: the preview is a `tk.Canvas`. On first selection or hover,
     `SCRUB_WORKERS` (3) threads extract up to `SCRUB_FRAMES` (60) frames from the first to
     the last frame (`scrub_times`).
     - Frames are extracted coarse-to-fine (`scrub_order`), so coverage is even early on.
     - They arrive as `("scrub", iid, idx, png)` messages.
     - Mouse x maps linearly: far left = first frame, far right = last frame.
     - While frames are loading, the nearest loaded frame is shown.
   - Display frame rates with `fmt_fps()` (25, 29.97, 23.976 – never "25.000").
   - Queue columns:
     - Duration, Resolution, Size and Output have fixed widths measured from the real font
       (`colw`), because macOS fonts run wider.
     - The file column gets the remaining width (`relabel_rows`), and names are shortened
       in the middle (`elide`).
     - Replacement audio is per item (`items[iid]["audio"]`). `eff(iid)` gives the effective
     MediaInfo and must be used for plans, output paths and `Job`.
     - The Audio panel's "Replace audio" button acts on the selected rows; dropping one
       audio file onto the queue does the same.
     - Length mismatches ask for confirmation twice: when the file is assigned and on
       Compress.
     - Rows with replaced audio show "♪".
   - Output shows the estimate until encoding starts (`items[iid]["status"]`), then the
       progress or the result.
   - `Tooltip`: light colours. On macOS it uses `MacWindowStyle help` instead of
     `overrideredirect`, because the latter rendered black there.
   - Drag & drop is optional via `tkinterdnd2`; the app falls back to plain `tk.Tk()`.
   - Settings layout:
     - Three equal columns (`uniform`): left Output above Advanced, middle Video, right
       Audio above Save.
     - All panels share the label-column width `lab_w`, so their fields line up.
     - The platform preset is its own full-width row ("Platform"), because next to the
       target size it got clipped.
     - The default window is 1320 px wide.
   - The x264/x265 preset is a snapping `ttk.Scale` (ultrafast … veryslow) bound to
     `V["preset"]`. It is disabled for VideoToolbox codecs.
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
python3 common_shrew.py --cli samples/hd_pcm.mov --size 15MB --replace-audio samples/audio_shorter.mp3
#   expect an audio stream ≈ 20 s (padded); with audio_longer.wav it is cut to 20 s
python3 common_shrew.py --cli out.mp4 --copy-video --replace-audio samples/audio_longer.wav
#   expect a video stream bit-identical to out.mp4 (compare: ffmpeg -i X -map 0:v -c copy -f md5 -)

# GUI
.venv/bin/python common_shrew.py samples/*
xvfb-run -a -s "-screen 0 1320x930x24" .venv/bin/python tools/gui_screenshot.py /tmp/s.png samples/*
```
- Check the result sizes stay ≤ target. Two-pass x264/x265 should land within the 2 %
  margin.
- `*_videotoolbox` encoders exist only on macOS, so test them on a Mac.
- In headless Linux screenshots, glyphs like ≈ → ✓ ▾ may be missing from the fonts. They
  render fine on macOS.
- Linux without python3-tk: `uv python install 3.12` gives a standalone Python with Tk 9.

## Platforms
- `IS_MAC` / `IS_WIN` switch the platform-specific bits; everything else is shared.
- **macOS only:** VideoToolbox. The two Apple hardware codecs are added to `CODECS`, and
  the "Hardware decoding" checkbox is shown, only when `IS_MAC`.
- **Windows:**
  - Every subprocess call passes `**POPEN_KW` (`CREATE_NO_WINDOW`) so ffmpeg doesn't
    flash a console window. Keep that on any new `subprocess` call.
  - Text pipes use `encoding="utf-8", errors="replace"`, otherwise non-ASCII file names
    break ffprobe JSON on cp1252 systems.
- `find_tool()` looks beyond `PATH`: Homebrew, linuxbrew and snap on macOS/Linux; winget
  Links/Packages, scoop, choco and `C:\ffmpeg\bin` on Windows. A freshly installed
  ffmpeg is found without restarting the shell.
- In-app "install ffmpeg" (`ffmpeg_install_cmd`) uses brew on macOS and winget on Windows.
  On Linux it shows the package-manager command, because installing needs `sudo`.
- Tested so far: Linux (CI-like container) and macOS (Jost). **Windows is untested.**
  Treat Windows bug reports as likely real.

## Conventions
- Keep it a **single-file app** with no dependencies beyond the standard library, except
  the optional `tkinterdnd2`.
- Keep all three platforms working: don't add macOS-only calls without an `IS_MAC`
  guard, and when a launcher changes, update the other launchers too.
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
- Possible next steps:
  - per-file settings;
  - remembering settings between runs;
  - an app bundle (py2app/Platypus) instead of the `.command` file;
  - duration warnings for platform presets (e.g. X: 140 s);
  - **hardware encoding/decoding on Windows/Linux** (optional, discussed with Jost):
    - NVIDIA: `hevc_nvenc` / `h264_nvenc` encoders, `-hwaccel cuda` decoding.
    - Intel: `hevc_qsv` / `h264_qsv` encoders, `-hwaccel qsv`.
    - AMD: `hevc_amf` / `h264_amf` encoders.
    - Linux: VAAPI (`hevc_vaapi` plus `-vaapi_device`, needs `format=nv12,hwupload`
      in the filter chain).
    - Detect support via `ffmpeg -encoders` / `-hwaccels`, plus a test encode, because
      a listed encoder can still fail without the GPU or driver.
    - These are single-pass, so treat them like VideoToolbox: larger safety margin,
      less precise size.
    - Can't be tested without the hardware.
