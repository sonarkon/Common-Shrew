#!/usr/bin/env python3
"""
Common Shrew – shrinks videos to a target size (default 5 GB) following
Vimeo's upload recommendations (help.vimeo.com, "Video compression settings"):

  * H.264 or H.265, progressive, constant frame rate (max. 60 fps)
  * Bitrate ceilings per resolution (SD 5 / 720p 10 / 1080p 20 / 2K 30 / 4K 60 Mbps)
  * AAC audio 320 kbps, 48 kHz, stereo or 5.1 – an audio track is required
    (a silent one is added if missing)
  * Square pixels, BT.709 colour tags for SDR, MP4 with faststart

Runs on macOS, Windows and Linux. Requires ffmpeg + ffprobe and Python 3.9+ with Tkinter:
  macOS:   brew install ffmpeg python-tk
  Windows: winget install Gyan.FFmpeg   (python.org Python already includes Tkinter)
  Linux:   sudo apt install ffmpeg python3-tk   (or dnf / pacman equivalents)
Optional drag & drop: pip install tkinterdnd2. The launchers in the repo set all this up.
Without GUI:  python3 common_shrew.py --cli movie.mov --size 5

Named after the common shrew (Sorex araneus), which shrinks in winter
(Dehnel's phenomenon) and grows back in spring.

MIT License – Copyright (c) 2026 sonarkon – https://github.com/sonarkon/Common-Shrew
"""

import base64
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
import dataclasses
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Vimeo spec
# --------------------------------------------------------------------------

# Bitrate ceiling (kbps) by output pixel count – upper end of Vimeo's ranges
VIMEO_MAX_KBPS = [
    (640 * 480, 5_000),      # SD
    (1280 * 720, 10_000),    # 720p
    (1920 * 1080, 20_000),   # 1080p
    (2560 * 1440, 30_000),   # 2K
    (10 ** 12, 60_000),      # 4K and above
]
STANDARD_FPS = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60]

# Bounding boxes (landscape). The video is fitted inside – never cropped/padded/upscaled.
RESOLUTIONS = {
    "Original": None,
    "4K UHD – fit 3840×2160": (3840, 2160),
    "2K – fit 2560×1440": (2560, 1440),
    "1080p – fit 1920×1080": (1920, 1080),
    "720p – fit 1280×720": (1280, 720),
    "SD – fit 854×480": (854, 480),
}
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
# keeps ffmpeg from flashing a console window for every call on Windows
POPEN_KW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}

CODECS = {   # first entry = default
    "H.265 · x265 2-pass (precise)": "libx265",
    "H.264 · x264 2-pass (precise)": "libx264",
}
if IS_MAC:   # Apple VideoToolbox hardware encoders exist only on macOS
    CODECS.update({
        "H.264 · Apple hardware (fast)": "h264_videotoolbox",
        "H.265 · Apple hardware (fast)": "hevc_videotoolbox",
    })
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]
FPS_CHOICES = ["Original"] + [str(f) for f in STANDARD_FPS]
# Target-size presets (decimal units). Platform limits as of Nov 2026 – they change often.
SIZE_PRESETS = [
    "5 GB – Vimeo default · LinkedIn",
    "4 GB – Facebook · TikTok (API)",
    "3 GB – Vimeo Free (storage cap)",
    "2 GB – Telegram · WhatsApp (as document) · Pinterest",
    "1 GB – Threads",
    "512 MB – X / Twitter (free)",
    "500 MB – Discord Nitro",
    "50 MB – Discord Nitro Basic",
    "20 MB – Discord free · e-mail attachment",
]


# short name shown on the preset button, keyed by "<num> <unit>"
PRESET_SHORT = {
    "5 GB": "Vimeo · LinkedIn", "4 GB": "Facebook · TikTok", "3 GB": "Vimeo Free",
    "2 GB": "Telegram · WhatsApp", "1 GB": "Threads", "512 MB": "X / Twitter",
    "500 MB": "Discord Nitro", "50 MB": "Discord Basic", "20 MB": "Discord · e-mail",
}


def parse_size_gb(text):
    """'5 GB – …', '750 MB', '2.5' -> size in GB (decimal)."""
    m = re.match(r"\s*([\d]+(?:[.,]\d+)?)\s*(TB|GB|MB|KB|T|G|M|K)?", str(text), re.I)
    if not m:
        raise ValueError(f"Can't read target size '{text}'")
    v = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "GB").upper()[0]
    gb = v * {"T": 1000, "G": 1, "M": 1e-3, "K": 1e-6}[unit]
    if gb <= 0:
        raise ValueError("Target size must be greater than 0")
    return gb


VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".mkv", ".avi", ".mts", ".m2ts",
             ".mpg", ".mpeg", ".webm", ".wmv", ".dv", ".ts", ".3gp"}


def find_tool(name):
    """Find ffmpeg/ffprobe even when launched by double-click without a full PATH."""
    p = shutil.which(name)
    if p:
        return p
    if IS_WIN:
        la = os.environ.get("LOCALAPPDATA", "")
        dirs = [os.path.join(la, "Microsoft", "WinGet", "Links"),   # winget install
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                             "ffmpeg", "bin"),
                r"C:\ffmpeg\bin",
                os.path.join(os.path.expanduser("~"), "scoop", "shims"),
                r"C:\ProgramData\chocolatey\bin"]
        exe = name + ".exe"
        # winget also unpacks into …\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin
        pk = os.path.join(la, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(pk):
            for d in os.listdir(pk):
                if d.lower().startswith("gyan.ffmpeg"):
                    for sub in os.listdir(os.path.join(pk, d)):
                        dirs.append(os.path.join(pk, d, sub, "bin"))
    else:
        exe = name
        dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                "/home/linuxbrew/.linuxbrew/bin", "/snap/bin", "/usr/bin"]
    for d in dirs:
        c = os.path.join(d, exe)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def ffmpeg_has_fps_mode():
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                             **POPEN_KW).stdout
        m = re.search(r"ffmpeg version n?(\d+)", out)
        return not m or int(m.group(1)) >= 5
    except Exception:
        return True


_HWDEC = {}


def hwdec_available():
    """True on macOS when ffmpeg can decode via VideoToolbox (cached)."""
    if "ok" not in _HWDEC:
        ok = False
        if IS_MAC and FFMPEG:
            try:
                out = subprocess.run([FFMPEG, "-hide_banner", "-hwaccels"],
                                     capture_output=True, text=True, timeout=10,
                                     **POPEN_KW).stdout
                ok = "videotoolbox" in out.split()
            except Exception:
                ok = False
        _HWDEC["ok"] = ok
    return _HWDEC["ok"]


def available_encoders():
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, **POPEN_KW).stdout
    except Exception:
        return set()
    return {line.split()[1] for line in out.splitlines()
            if len(line.split()) > 1 and line.startswith(" V")}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    vfr: bool
    interlaced: bool
    has_audio: bool
    audio_channels: int
    vcodec: str = ""
    acodec: str = ""
    audio_kbps: int = 0   # first audio track
    color_primaries: str = ""
    color_trc: str = ""
    colorspace: str = ""
    pix_fmt: str = ""
    sar: str = "1:1"
    rotation: int = 0
    size_bytes: int = 0
    video_kbps: int = 0   # source video bitrate (0 = unknown)

    @property
    def hdr(self):
        return self.color_trc in ("smpte2084", "arib-std-b67")


def _rate(s):
    try:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def _audio_kbps(a):
    br = int(a.get("bit_rate", 0) or 0) // 1000
    if br:
        return br
    if a.get("codec_name", "").startswith("pcm"):
        bits = int(a.get("bits_per_sample", 0) or 0) or 24
        return int(int(a.get("sample_rate", 48000)) * int(a.get("channels", 2)) * bits / 1000)
    return 320


MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}


def probe(path):
    if not FFPROBE:
        raise RuntimeError("ffprobe not found – install ffmpeg (see README).")
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **POPEN_KW)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe: {out.stderr.strip() or 'unreadable file'}")
    data = json.loads(out.stdout)
    vs = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")), None)
    if vs is None:
        raise RuntimeError("No video stream found.")
    aud = [s for s in data["streams"] if s.get("codec_type") == "audio"]
    fmt = data.get("format", {})
    dur = float(fmt.get("duration") or vs.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("Could not determine duration.")
    r_fps, a_fps = _rate(vs.get("r_frame_rate", "0/1")), _rate(vs.get("avg_frame_rate", "0/1"))
    fps = a_fps or r_fps or 25.0
    vfr = bool(r_fps and a_fps and abs(r_fps - a_fps) / max(r_fps, a_fps) > 0.01)
    rot = 0
    for sd in vs.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if "rotate" in vs.get("tags", {}):
        rot = int(vs["tags"]["rotate"])
    size = int(fmt.get("size", 0) or 0) or os.path.getsize(path)
    vk = int(vs.get("bit_rate", 0) or 0) // 1000
    if not vk:
        total = int(fmt.get("bit_rate", 0) or 0) // 1000 or int(size * 8 / dur / 1000)
        ak = sum(int(a.get("bit_rate", 0) or 0) for a in aud) // 1000
        vk = max(0, total - ak)
    return MediaInfo(
        path=path, duration=dur,
        width=int(vs.get("width", 0)), height=int(vs.get("height", 0)),
        fps=fps, vfr=vfr,
        interlaced=vs.get("field_order", "progressive") not in ("progressive", "unknown", ""),
        has_audio=bool(aud),
        audio_channels=int(aud[0].get("channels", 2)) if aud else 0,
        acodec=aud[0].get("codec_name", "") if aud else "",
        audio_kbps=_audio_kbps(aud[0]) if aud else 0,
        vcodec=vs.get("codec_name", ""),
        color_primaries=vs.get("color_primaries", ""),
        color_trc=vs.get("color_transfer", ""),
        colorspace=vs.get("color_space", ""),
        pix_fmt=vs.get("pix_fmt", ""),
        sar=vs.get("sample_aspect_ratio", "1:1") or "1:1",
        rotation=rot, size_bytes=size, video_kbps=vk,
    )


SCRUB_FRAMES = 60   # max. hover-scrub frames per clip (first … last frame)
SCRUB_WORKERS = 3   # parallel ffmpeg calls while extracting them


def scrub_times(duration, fps):
    """Evenly spaced timestamps from the first to the (almost) last frame."""
    fps = fps or 25.0
    n = max(2, min(SCRUB_FRAMES, int(duration * fps)))
    last = max(0.0, duration - 1.5 / fps)
    return [last * i / (n - 1) for i in range(n)]


def scrub_order(n):
    """Coarse-to-fine order (ends, middle, quarters, …) so coverage is even early on."""
    out, seen, step = [], set(), n - 1
    for i in (0, n - 1):
        out.append(i)
        seen.add(i)
    while step >= 1:
        for i in range(0, n, step):
            if i not in seen:
                out.append(i)
                seen.add(i)
        step //= 2
    out += [i for i in range(n) if i not in seen]
    return out


def thumbnail_png(path, duration, w=288, h=162, t=None):
    """Grab one frame (default ~10 % in) as PNG bytes, fitted into w×h."""
    if t is None:
        t = duration * 0.1
    t = min(max(t, 0.0), max(duration, 0.0))
    for attempt in (t, t - 0.25, t - 1.0):   # seeking right at the end may yield nothing
        cmd = [FFMPEG, "-v", "error", "-nostdin", "-ss", f"{max(attempt, 0):.3f}", "-i", path,
               "-frames:v", "1",
               "-vf", f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease",
               "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        r = subprocess.run(cmd, capture_output=True, timeout=60, **POPEN_KW)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        if attempt <= 0:
            break
    return None


# --------------------------------------------------------------------------
# Settings & planning
# --------------------------------------------------------------------------

@dataclass
class Settings:
    target_gb: float = 5.0          # target size (decimal, 1 GB = 1000³ bytes)
    safety_pct: float = 2.0
    codec: str = "libx265"
    preset: str = "medium"
    resolution: str = "Original"
    fps: str = "Original"
    audio_kbps: int = 320
    audio_channels: str = "Auto"    # Auto / Stereo / 5.1
    audio_copy: bool = False        # keep original audio untouched
    vimeo_cap: bool = True
    source_cap: bool = True         # never exceed source video bitrate
    ten_bit: bool = False
    deinterlace: str = "Auto"       # Auto / On / Off
    keyframe_sec: float = 2.0
    hw_decode: bool = True          # decode the source via VideoToolbox (macOS)
    suffix: str = "_vimeo"
    out_dir: str = ""
    extra_args: str = ""


@dataclass
class Plan:
    info: MediaInfo
    out_w: int
    out_h: int
    out_fps: float
    video_kbps: int
    audio_kbps: int
    audio_ch: int
    est_bytes: int
    notes: list = field(default_factory=list)


def display_dims(info):
    w, h = info.width, info.height
    try:  # non-square pixels -> square (Vimeo requirement)
        n, d = (int(x) for x in info.sar.split(":"))
        if n and d and n != d:
            w = int(round(w * n / d))
    except Exception:
        pass
    if abs(info.rotation) in (90, 270):
        w, h = h, w
    return w, h


def fit_into(w, h, box):
    """Scale w×h to fit inside box keeping aspect ratio. No crop, no upscale."""
    if not box:
        return w, h, 1.0
    bw, bh = box
    if h > w:          # portrait video -> portrait box
        bw, bh = bh, bw
    s = min(bw / w, bh / h, 1.0)
    return w * s, h * s, s


def vimeo_cap_for(w, h):
    px = w * h
    for limit, kbps in VIMEO_MAX_KBPS:
        if px <= limit * 1.05:
            return kbps
    return VIMEO_MAX_KBPS[-1][1]


def make_plan(info, s: Settings):
    notes = []
    dw, dh = display_dims(info)
    fw, fh, sc = fit_into(dw, dh, RESOLUTIONS.get(s.resolution))
    out_w = max(2, int(round(fw / 2) * 2))
    out_h = max(2, int(round(fh / 2) * 2))
    if RESOLUTIONS.get(s.resolution) and sc >= 1.0:
        notes.append("Source already fits the chosen size – kept original resolution.")

    if s.fps != "Original":
        out_fps = float(s.fps)
    else:
        out_fps = info.fps
        if out_fps <= 61 and (info.vfr or not any(abs(out_fps - f) < 0.01 for f in STANDARD_FPS)):
            snapped = min(STANDARD_FPS, key=lambda f: abs(f - out_fps))
            notes.append(f"Variable/unusual frame rate {fmt_fps(out_fps)} → constant {fmt_fps(snapped)} fps.")
            out_fps = snapped
    if out_fps > 60.01:
        new = out_fps / math.ceil(out_fps / 60.01)
        notes.append(f"{fmt_fps(out_fps)} fps exceeds Vimeo's 60 fps → reduced to {fmt_fps(new)} fps.")
        out_fps = new

    if s.audio_channels == "Stereo":
        ach = 2
    elif s.audio_channels == "5.1":
        ach = 6
    else:
        ach = 6 if info.audio_channels >= 6 else 2
    if not info.has_audio:
        notes.append("No audio – adding a silent stereo track (required by Vimeo).")
        ach = 2
    akbps = s.audio_kbps if ach == 2 else max(s.audio_kbps, 384)
    copy_audio = s.audio_copy and info.has_audio
    if copy_audio:
        akbps = info.audio_kbps or 320
        ach = info.audio_channels
        notes.append(f"Audio copied unchanged ({info.acodec}, ~{akbps} kbps)"
                     + ("" if info.acodec in MP4_AUDIO_OK else " – output saved as .mov, "
                        "since MP4 can't hold this audio format."))

    overhead = min(8_000_000, s.target_gb * 1e9 * 8 * 0.01)
    budget_bits = s.target_gb * 1e9 * 8 * (1 - s.safety_pct / 100.0) - overhead
    total_kbps = budget_bits / info.duration / 1000
    if not copy_audio and akbps > total_kbps * 0.2 and akbps > 96:   # small targets: audio must not eat the budget
        new_a = max(64, min(akbps, int(total_kbps * 0.2) // 32 * 32))
        if new_a < akbps:
            notes.append(f"Small target – audio reduced to {new_a} kbps to leave room for video.")
            akbps = new_a
    vkbps = int(total_kbps - akbps)

    if s.vimeo_cap:
        cap = vimeo_cap_for(out_w, out_h)
        if vkbps > cap:
            vkbps = cap
            notes.append(f"Capped at Vimeo's recommended {cap/1000:.0f} Mbps for this resolution.")
    if s.source_cap and info.video_kbps:
        src = int(info.video_kbps * min(1.0, (out_w * out_h) / max(1, dw * dh)) ** 0.75)
        if 0 < src < vkbps:
            vkbps = max(src, min(500, vkbps))
            notes.append(f"Capped at source bitrate (~{src/1000:.1f} Mbps) – a higher rate "
                         "would only add size.")
    if vkbps < 150:
        raise RuntimeError(
            f"Too long for {fmt_size(s.target_gb * 1e9)} – only {max(vkbps, 0)} kbps would remain. "
            "Raise the target size or lower the resolution/audio bitrate.")
    est = int((vkbps + akbps) * 1000 / 8 * info.duration)
    if info.interlaced and s.deinterlace != "Off":
        notes.append("Interlaced source → deinterlacing (bwdif).")
    if info.hdr:
        notes.append("HDR source → 10-bit, HDR metadata tags kept.")
    return Plan(info, out_w, out_h, out_fps, vkbps, akbps, ach, est, notes)


def output_ext(info, s: Settings):
    if s.audio_copy and info and info.has_audio and info.acodec not in MP4_AUDIO_OK:
        return ".mov"
    return ".mp4"


def output_path(path, s: Settings, info=None):
    base = os.path.splitext(os.path.basename(path))[0]
    d = s.out_dir or os.path.dirname(os.path.abspath(path))
    p = os.path.join(d, f"{base}{s.suffix}{output_ext(info, s)}")
    if os.path.abspath(p) == os.path.abspath(path):
        p = os.path.join(d, f"{base}{s.suffix}_1.mp4")
    return p


# --------------------------------------------------------------------------
# ffmpeg command
# --------------------------------------------------------------------------

def _fps_str(f):
    for num, val in ((24000, 23.976), (30000, 29.97), (60000, 59.94)):
        if abs(f - val) < 0.01:
            return f"{num}/1001"
    return f"{f:.6g}"


def build_cmd(plan: Plan, s: Settings, out, pass_no=0, passlog=None):
    """pass_no: 0 = single pass, 1/2 = two-pass."""
    info = plan.info
    cmd = [FFMPEG, "-hide_banner", "-y", "-nostdin"]
    if s.hw_decode and hwdec_available():
        # frames are copied back to system memory automatically for the CPU filters;
        # unsupported formats fall back to software decoding inside ffmpeg
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-i", info.path]
    if not info.has_audio and pass_no != 1:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    vf = []
    if (info.interlaced and s.deinterlace == "Auto") or s.deinterlace == "On":
        vf.append("bwdif=mode=send_frame:parity=auto:deint=all")
    vf.append(f"scale={plan.out_w}:{plan.out_h}:flags=lanczos")
    vf.append("setsar=1")
    vf.append(f"fps={_fps_str(plan.out_fps)}")
    ten = s.ten_bit or info.hdr
    if s.codec.endswith("videotoolbox"):
        pix = "p010le" if ten else "nv12"
    else:
        pix = "yuv420p10le" if ten else "yuv420p"
    vf.append(f"format={pix}")

    cmd += ["-map", "0:v:0"]
    if pass_no != 1:
        cmd += ["-map", "0:a:0" if info.has_audio else "1:a:0"]
    cmd += ["-vf", ",".join(vf)]
    cmd += ["-fps_mode", "cfr"] if ffmpeg_has_fps_mode() else ["-vsync", "cfr"]

    v = plan.video_kbps
    gop = max(1, int(round(plan.out_fps * s.keyframe_sec)))
    cmd += ["-c:v", s.codec, "-b:v", f"{v}k"]
    if s.codec == "libx264":
        cmd += ["-preset", s.preset, "-profile:v", "high10" if ten else "high",
                "-maxrate", f"{int(v*1.5)}k", "-bufsize", f"{v*3}k",
                "-g", str(gop), "-keyint_min", str(max(1, gop // 2)), "-sc_threshold", "40"]
        if pass_no:
            cmd += ["-pass", str(pass_no), "-passlogfile", passlog]
    elif s.codec == "libx265":
        x265p = [f"keyint={gop}", f"min-keyint={max(1, gop//2)}",
                 f"vbv-maxrate={int(v*1.5)}", f"vbv-bufsize={v*3}", "log-level=error"]
        if info.hdr:
            x265p += ["hdr10-opt=1", "repeat-headers=1"]
        if pass_no:
            x265p += [f"pass={pass_no}", f"stats={passlog}.log"]
            if pass_no == 1:   # fast analysis pass – negligible quality impact
                x265p += ["slow-firstpass=0"]
        cmd += ["-preset", s.preset, "-tag:v", "hvc1", "-x265-params", ":".join(x265p)]
    else:  # VideoToolbox
        cmd += ["-maxrate", f"{int(v*1.3)}k", "-bufsize", f"{v*2}k", "-g", str(gop),
                "-allow_sw", "1"]
        if s.codec == "hevc_videotoolbox":
            cmd += ["-tag:v", "hvc1", "-profile:v", "main10" if ten else "main"]
        else:
            cmd += ["-profile:v", "high"]

    # colour tags: keep source, default to BT.709 (SDR)
    def tag(x):
        return x if x not in ("", "unknown", "unspecified", "reserved") else "bt709"
    cmd += ["-color_primaries", tag(info.color_primaries), "-color_trc", tag(info.color_trc),
            "-colorspace", tag(info.colorspace)]

    if pass_no == 1:
        return cmd + ["-an", "-f", "null", "-progress", "pipe:1", "-nostats", os.devnull]

    if s.audio_copy and info.has_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.audio_kbps}k", "-ar", "48000",
                "-ac", str(plan.audio_ch)]
    if not info.has_audio:
        cmd += ["-shortest"]
    cmd += ["-map_metadata", "0", "-movflags", "+faststart"]
    if s.extra_args.strip():
        cmd += s.extra_args.split()
    return cmd + ["-progress", "pipe:1", "-nostats", out]


# --------------------------------------------------------------------------
# Encoding job
# --------------------------------------------------------------------------

class Cancelled(Exception):
    pass


class Job:
    def __init__(self, path, settings, log, progress, stop_event, info=None):
        self.path, self.s, self.info = path, settings, info
        self.log, self.progress, self.stop = log, progress, stop_event
        self.proc = None

    def _run(self, cmd, duration, frac_from, frac_to):
        self.log("$ " + " ".join(_q(c) for c in cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, **POPEN_KW)
        err_tail = []

        def read_err():
            for line in self.proc.stderr:
                err_tail.append(line.rstrip())
                del err_tail[:-40]
        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        speed = ""
        for line in self.proc.stdout:
            if self.stop.is_set():
                self.proc.terminate()
                break
            k, _, val = line.strip().partition("=")
            if k == "speed":
                speed = val.strip()
            elif k in ("out_time_us", "out_time_ms"):
                try:
                    sec = int(val) / 1e6
                except ValueError:
                    continue
                f = min(1.0, max(0.0, sec / duration))
                self.progress(frac_from + (frac_to - frac_from) * f, speed)
        self.proc.wait()
        t.join(timeout=2)
        if self.stop.is_set():
            raise Cancelled("Cancelled.")
        if self.proc.returncode != 0:
            raise RuntimeError("ffmpeg error:\n" + "\n".join(err_tail[-15:]))

    def run(self):
        info = self.info or probe(self.path)
        plan = make_plan(info, self.s)
        out = output_path(self.path, self.s, info)
        self.log(f"▶ {os.path.basename(self.path)}")
        self.log(f"  {fmt_dur(info.duration)}, {info.width}×{info.height} @ {fmt_fps(info.fps)} fps "
                 f"→ {plan.out_w}×{plan.out_h} @ {fmt_fps(plan.out_fps)} fps")
        self.log(f"  video {plan.video_kbps/1000:.2f} Mbps · audio {plan.audio_kbps} kbps "
                 f"({'5.1' if plan.audio_ch == 6 else 'stereo'}) · est. {fmt_size(plan.est_bytes)}")
        for n in plan.notes:
            self.log("  • " + n)
        tmpdir = tempfile.mkdtemp(prefix="shrew_2pass_")
        passlog = os.path.join(tmpdir, "pass")
        def encode(s):
            if s.codec in ("libx264", "libx265"):
                self._run(build_cmd(plan, s, out, 1, passlog), info.duration, 0, 0.5)
                self._run(build_cmd(plan, s, out, 2, passlog), info.duration, 0.5, 1)
            else:
                self._run(build_cmd(plan, s, out, 0), info.duration, 0, 1)
        try:
            try:
                encode(self.s)
            except RuntimeError:
                if not (self.s.hw_decode and hwdec_available()) or self.stop.is_set():
                    raise
                self.log("  ⚠ Hardware decoding failed – retrying with software decoding.")
                encode(dataclasses.replace(self.s, hw_decode=False))
        except BaseException:
            if os.path.exists(out):
                try:
                    os.remove(out)
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        size = os.path.getsize(out)
        red = reduction(info.size_bytes, size)
        flag = "✓" if size <= self.s.target_gb * 1e9 else "⚠ ABOVE TARGET"
        self.log(f"  {flag} {out}")
        self.log(f"    {fmt_size(info.size_bytes)} → {fmt_size(size)}  ({red})")
        if size > self.s.target_gb * 1e9:
            self.log("    → increase the safety margin and run again.")
        return out, size


def reduction(src, dst):
    if not src:
        return "–"
    pct = (1 - dst / src) * 100
    if abs(pct) < 0.5:
        return "±0 %"
    return f"−{pct:.0f} %" if pct > 0 else f"+{-pct:.0f} %"


def _q(s):
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def fmt_dur(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_fps(f):
    """25.0 -> '25', 29.97003 -> '29.97', 23.976 -> '23.976'."""
    if abs(f - round(f)) < 0.005:
        return str(int(round(f)))
    return f"{f:.3f}".rstrip("0").rstrip(".")


def fmt_size(b):
    if b >= 1e9:
        return f"{b/1e9:.2f} GB"
    if b >= 1e6:
        return f"{b/1e6:.0f} MB"
    return f"{b/1e3:.0f} KB"


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

APP_NAME = "Common Shrew"
WIKI_URL = "https://en.wikipedia.org/wiki/Common_shrew"
KOFI_URL = "https://ko-fi.com/S7Z228X1UL"

TIPS = {
    "kofi": "Common Shrew is free. If it saves you time, you can support its development "
            "with a coffee on Ko-fi.\n\nOpens ko-fi.com/S7Z228X1UL in your browser.",
    "title": "Common Shrew is named after Sorex araneus, the common shrew: to survive winter it "
             "shrinks its body by about 18 %, its skull by around 15 % and its brain by up to "
             "20–30 % – then grows back in spring. This is called Dehnel's phenomenon.\n\n"
             "Your videos shrink too – the originals stay untouched.\n\n"
             "Click to read more on Wikipedia.",
    "target": "Maximum size of each output file. Pick a preset or type your own value, "
              "e.g. 7.5 GB or 750 MB\n(decimal: 1 GB = 1,000 MB = 1,000,000,000 bytes). "
              "The video bitrate is calculated from this size and the duration.\n\n"
              "Presets reflect platform upload limits (Nov 2026). Platforms also limit duration, "
              "and limits change –\ncheck the platform if in doubt. YouTube allows up to 256 GB, "
              "so no preset is needed there.",
    "safety": "Percentage of the target size held back as reserve so the file reliably stays under it.\n"
              "2 % is plenty for 2-pass x264/x265. Use 5–8 % for Apple hardware encoding.",
    "codec": "H.265 (default) gives roughly 25–40 % better quality than H.264 at the same file "
             "size – ideal when the size limit is tight – but encodes slower.\n\n"
             "x264 / x265: 2-pass encoding, hits the size precisely.\n"
             "Apple hardware (VideoToolbox): much faster, slightly lower quality per MB, "
             "size less precise (macOS only).\n"
             "H.264 is the most compatible choice if the file is also used outside Vimeo.",
    "preset": "Speed vs. efficiency for x264/x265. Slower presets give better quality at the "
              "same file size.\n'medium' is a good balance for x265; 'slow' looks slightly better "
              "but takes about twice as long. Ignored by Apple hardware encoding.",
    "res": "Output frame size. The video is scaled to fit INSIDE the chosen box, keeping its "
           "aspect ratio –\nnever cropped, never letterboxed, never upscaled. "
           "Portrait videos use the rotated box.\nExample: 4096×1716 in 'fit 1920×1080' → 1920×804.",
    "fps": "Original keeps the source frame rate; variable frame rates (phones) become the nearest "
           "standard constant rate.\nVimeo supports up to 60 fps – higher rates are reduced "
           "automatically (e.g. 120 → 60).",
    "abr": "AAC audio bitrate. Vimeo recommends 320 kbps. 5.1 always uses at least 384 kbps.",
    "ach": "Auto: keeps 5.1 if the source has 6+ channels, otherwise stereo.\n"
           "Videos without sound get a silent stereo track – Vimeo requires one.",
    "deint": "Vimeo needs progressive video. Auto deinterlaces only sources flagged as "
             "interlaced (e.g. 1080i camcorder/broadcast footage).",
    "gop": "Seconds between keyframes. 2 s is the usual streaming value – shorter gives "
           "faster seeking but a slightly larger file.",
    "vcap": "Limit the bitrate to Vimeo's recommended maximum for the output resolution\n"
            "(SD 5 · 720p 10 · 1080p 20 · 2K 30 · 4K 60 Mbps). Vimeo re-encodes anything higher "
            "anyway,\nso the file may end up well below the target size.",
    "scap": "Never encode at a higher bitrate than the source already uses – that would only "
            "make the file bigger\nwithout improving quality (adjusted when downscaling).",
    "ten": "Encode with 10-bit colour depth: smoother gradients, less banding, slightly slower.\n"
           "HDR sources (PQ / HLG) always use 10-bit.",
    "suffix": "Text appended to the original file name. The output is always MP4 "
              "(or MOV when original PCM audio is kept).",
    "outdir": "Where the compressed files are saved – by default next to each source file. "
              "Click to pick one folder for all files; '×' resets to next-to-source.",
    "acopy": "Keep the original audio track bit-for-bit instead of re-encoding it to AAC. "
             "Best quality, but it uses more of the size budget (uncompressed PCM ≈ 2.3 Mbps "
             "for 48 kHz / 24-bit stereo) – the video bitrate is reduced accordingly.\n\n"
             "MP4 can't hold PCM audio, so in that case the output is saved as .mov "
             "(Vimeo accepts both). Videos without sound still get a silent AAC track.",
    "unit": "Unit for the target size: GB or MB (decimal, 1 GB = 1,000 MB).",
    "hwdec": "Read (decode) the source video with the Mac's media engine instead of the CPU. "
             "Speeds up both passes – especially for HEVC and ProRes sources – and leaves more "
             "CPU for the encoder. The image is identical; unsupported formats fall back to "
             "software automatically, and a failed run is retried without it. macOS only.",
    "extra": "Extra ffmpeg output options, e.g.  -tune film  or  -metadata title=\"My Film\".\n"
             "For experts – applied to the final pass.",
}

C = {  # colour palette (dark)
    "bg": "#17181b", "panel": "#212226", "field": "#2b2c31", "line": "#34353b",
    "fg": "#e8e8ea", "muted": "#9a9ca3", "accent": "#3d8bfd", "accent_hi": "#5a9dff",
    "ok": "#44c486", "tip_bg": "#f6f6f8", "tip_fg": "#1c1c1f", "tip_line": "#b9bac0",
    "warn": "#f0b43c", "err": "#ef5d5d", "sel": "#2f4f80",
}


class Tooltip:
    font = None   # set by the GUI

    def __init__(self, widget, text, delay=450):
        # single line breaks become spaces (the label wraps itself), blank lines stay
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        self.w, self.text, self.delay = widget, text, delay
        self.tip = self.after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self.after_id = self.w.after(self.delay, self._show)

    def _cancel(self):
        if self.after_id:
            self.w.after_cancel(self.after_id)
            self.after_id = None

    def _show(self):
        import tkinter as tk
        if self.tip:
            return
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip = tip = tk.Toplevel(self.w, bg=C["tip_line"])
        tip.withdraw()
        if sys.platform == "darwin":
            try:  # native help-tag window: draws reliably on macOS, never steals focus
                tip.tk.call("::tk::unsupported::MacWindowStyle", "style", tip._w,
                            "help", "noActivates")
            except tk.TclError:
                tip.wm_overrideredirect(True)
        else:
            tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tip, text=self.text, justify="left", bg=C["tip_bg"], fg=C["tip_fg"],
                 padx=11, pady=8, wraplength=460, font=Tooltip.font,
                 bd=0, highlightthickness=0).pack(padx=1, pady=1)
        tip.update_idletasks()
        sw = tip.winfo_screenwidth()
        x = min(x, sw - tip.winfo_reqwidth() - 8)
        tip.wm_geometry(f"+{x}+{y}")
        tip.deiconify()
        tip.lift()

    def _hide(self, _=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        root = tk.Tk()
        dnd = False

    root.title(APP_NAME)
    root.geometry(f"{min(1320, root.winfo_screenwidth() - 40)}x"
                  f"{min(930, root.winfo_screenheight() - 70)}")
    root.minsize(980, 720)
    root.configure(bg=C["bg"])

    # ---------- fonts & theme
    base = tkfont.nametofont("TkDefaultFont")
    fam = base.actual("family")
    F = {
        "base": (fam, 13), "small": (fam, 11), "bold": (fam, 13, "bold"),
        "h1": (fam, 20, "bold"), "h2": (fam, 12, "bold"), "mono": (tkfont.nametofont("TkFixedFont").actual("family"), 11),
    }
    base.configure(size=13)
    Tooltip.font = F["small"]
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["field"],
                 bordercolor=C["line"], lightcolor=C["line"], darkcolor=C["line"],
                 troughcolor=C["field"], focuscolor=C["accent"], font=F["base"],
                 insertcolor=C["fg"], selectbackground=C["sel"], selectforeground=C["fg"])
    st.configure("TFrame", background=C["bg"])
    st.configure("Panel.TFrame", background=C["panel"])
    st.configure("TLabel", background=C["bg"], foreground=C["fg"])
    st.configure("Panel.TLabel", background=C["panel"])
    st.configure("Muted.TLabel", background=C["panel"], foreground=C["muted"], font=F["small"])
    st.configure("MutedBg.TLabel", background=C["bg"], foreground=C["muted"], font=F["small"])
    st.configure("H1.TLabel", background=C["bg"], font=F["h1"])
    st.configure("H2.TLabel", background=C["panel"], foreground=C["muted"], font=F["h2"])
    st.configure("TButton", background=C["field"], foreground=C["fg"], padding=(14, 7),
                 borderwidth=0, font=F["base"])
    st.map("TButton", background=[("disabled", C["panel"]), ("pressed", C["line"]),
                                  ("active", C["line"])],
           foreground=[("disabled", C["muted"])])
    st.configure("Accent.TButton", background=C["accent"], foreground="#ffffff",
                 font=F["bold"], padding=(22, 9))
    st.map("Accent.TButton", background=[("disabled", C["line"]), ("pressed", C["accent"]),
                                         ("active", C["accent_hi"])],
           foreground=[("disabled", C["muted"])])
    st.configure("Small.TButton", padding=(10, 4), font=F["small"])
    st.configure("Kofi.TButton", background=C["field"], foreground=C["fg"],
                 padding=(12, 5), font=F["small"], borderwidth=1, bordercolor=C["line"],
                 lightcolor=C["field"], darkcolor=C["field"])
    st.map("Kofi.TButton", background=[("active", C["sel"]), ("pressed", C["sel"])])
    st.configure("Small.TMenubutton", background=C["field"], foreground=C["fg"],
                 padding=(10, 4), font=F["small"], borderwidth=0, arrowcolor=C["field"])
    st.map("Small.TMenubutton", background=[("active", C["line"]), ("pressed", C["line"])])
    st.configure("TCheckbutton", background=C["panel"], foreground=C["fg"], font=F["base"],
                 indicatorbackground=C["field"], indicatorforeground=C["fg"])
    st.map("TCheckbutton", background=[("active", C["panel"])],
           indicatorbackground=[("selected", C["accent"])],
           indicatorforeground=[("selected", "#ffffff")])
    for w in ("TCombobox", "TSpinbox", "TEntry"):
        st.configure(w, fieldbackground=C["field"], background=C["field"],
                     foreground=C["fg"], arrowcolor=C["muted"], padding=(8, 5),
                     bordercolor=C["line"], lightcolor=C["field"], darkcolor=C["field"])
        st.map(w, fieldbackground=[("disabled", C["panel"]), ("readonly", C["field"])],
               foreground=[("disabled", C["muted"]), ("readonly", C["fg"])],
               arrowcolor=[("disabled", C["line"])],
               bordercolor=[("focus", C["accent"])],
               selectbackground=[("readonly", C["field"])],
               selectforeground=[("readonly", C["fg"])])
    root.option_add("*TCombobox*Listbox.background", C["field"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", F["base"])
    st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                 foreground=C["fg"], rowheight=48, borderwidth=0, font=F["base"])
    st.map("Treeview", background=[("selected", C["sel"])])
    st.configure("Treeview.Heading", background=C["panel"], foreground=C["muted"],
                 font=F["small"], borderwidth=0, padding=(6, 6))
    st.map("Treeview.Heading", background=[("active", C["panel"])])
    st.configure("Horizontal.TScale", background=C["accent"], troughcolor=C["field"],
                 bordercolor=C["panel"], lightcolor=C["accent"], darkcolor=C["accent"],
                 sliderlength=18, sliderthickness=18, troughrelief="flat", groovewidth=6)
    st.map("Horizontal.TScale", background=[("disabled", C["line"]), ("active", C["accent_hi"])])
    st.configure("Horizontal.TProgressbar", background=C["accent"], troughcolor=C["field"],
                 bordercolor=C["field"], lightcolor=C["accent"], darkcolor=C["accent"],
                 thickness=8)
    st.configure("Vertical.TScrollbar", background=C["line"], troughcolor=C["panel"],
                 bordercolor=C["panel"], lightcolor=C["line"], darkcolor=C["line"],
                 arrowcolor=C["muted"], gripcount=0)
    st.map("Vertical.TScrollbar", background=[("active", C["muted"])])

    msgs = queue.Queue()
    stop_event = threading.Event()
    state = {"running": False, "log_open": False}
    items = {}      # iid -> dict(path, info, thumb_big, thumb_small, result, error)
    order = []      # iids in queue order

    # ---------- variables
    V = dict(
        target=tk.StringVar(value="5"), unit=tk.StringVar(value="GB"),
        acopy=tk.BooleanVar(value=False), safety=tk.DoubleVar(value=2.0),
        codec=tk.StringVar(value=list(CODECS)[0]), preset=tk.StringVar(value="medium"),
        res=tk.StringVar(value="Original"), fps=tk.StringVar(value="Original"),
        abr=tk.StringVar(value="320"), ach=tk.StringVar(value="Auto"),
        vcap=tk.BooleanVar(value=True), scap=tk.BooleanVar(value=True),
        ten=tk.BooleanVar(value=False), deint=tk.StringVar(value="Auto"),
        hwdec=tk.BooleanVar(value=True),
        gop=tk.DoubleVar(value=2.0), suffix=tk.StringVar(value="_vimeo"),
        outdir=tk.StringVar(value=""), extra=tk.StringVar(value=""),
    )

    def settings():
        return Settings(
            target_gb=parse_size_gb(f'{V["target"].get()} {V["unit"].get()}'), safety_pct=float(V["safety"].get()),
            codec=CODECS[V["codec"].get()], preset=V["preset"].get(),
            resolution=V["res"].get(), fps=V["fps"].get(),
            audio_kbps=int(float(V["abr"].get())), audio_channels=V["ach"].get(),
            audio_copy=V["acopy"].get(),
            vimeo_cap=V["vcap"].get(), source_cap=V["scap"].get(), ten_bit=V["ten"].get(),
            deinterlace=V["deint"].get(), keyframe_sec=float(V["gop"].get()),
            hw_decode=V["hwdec"].get(),
            suffix=V["suffix"].get() or "_vimeo", out_dir=V["outdir"].get(),
            extra_args=V["extra"].get())

    # ---------- layout
    outer = ttk.Frame(root, padding=(20, 16, 20, 16))
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(1, weight=1)

    hdr = ttk.Frame(outer)
    hdr.grid(row=0, column=0, sticky="we", pady=(0, 12))
    title = ttk.Label(hdr, text=APP_NAME, style="H1.TLabel", cursor="hand2")
    title.pack(side="left")
    title.bind("<Button-1>", lambda e: webbrowser.open(WIKI_URL))
    Tooltip(title, TIPS["title"])
    ttk.Label(hdr, text="   Shrinks videos to a target size – tuned to Vimeo's upload specs",
              style="MutedBg.TLabel").pack(side="left", pady=(6, 0))
    kofi = ttk.Button(hdr, text="☕  Support on Ko-fi", style="Kofi.TButton",
                      cursor="hand2", command=lambda: webbrowser.open(KOFI_URL))
    kofi.pack(side="right")
    Tooltip(kofi, TIPS["kofi"])
    ff_lbl = ttk.Label(hdr, text="", style="MutedBg.TLabel")
    ff_lbl.pack(side="right", padx=(0, 16), pady=(6, 0))

    def card(parent, title):
        f = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10, 14, 12))
        if title:
            ttk.Label(f, text=title.upper(), style="H2.TLabel").grid(
                row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        return f

    # --- top: queue + preview
    top = ttk.Frame(outer)
    top.grid(row=1, column=0, sticky="nsew")
    top.columnconfigure(0, weight=1)
    top.rowconfigure(0, weight=1)

    qcard = card(top, "Queue")
    qcard.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    qcard.columnconfigure(0, weight=1)
    qcard.rowconfigure(1, weight=1)
    cols = ("dur", "res", "size", "out")
    tree = ttk.Treeview(qcard, columns=cols, show="tree headings", selectmode="extended")
    tree.heading("#0", text="  File", anchor="w")
    # column widths from the real font metrics (macOS fonts run wider than Linux ones)
    fbase = tkfont.Font(font=F["base"])
    fhead = tkfont.Font(font=F["small"])

    def colw(sample, head):
        return max(fbase.measure(sample), fhead.measure(head)) + 22
    tree.column("#0", width=240, minwidth=90, stretch=False)  # sized in relabel_rows()
    for c, t, sample in (("dur", "Duration", "88:88:88"),
                         ("res", "Resolution", "8888×8888"),
                         ("size", "Size", "88.88 GB"),
                         ("out", "Output", "≈ 88.88 GB (−88 %)")):
        tree.heading(c, text=t, anchor="w")
        w = colw(sample, t)
        tree.column(c, width=w, minwidth=w, stretch=False, anchor="w")

    def elide(text, px, font):
        """Shorten in the middle so start, date and extension stay visible."""
        if font.measure(text) <= px:
            return text
        keep_end = min(len(text) // 2, 8)
        lo, hi = 1, len(text) - keep_end
        best = text[:1] + "…" + text[-keep_end:]
        while lo <= hi:   # binary search for the longest head that fits
            mid = (lo + hi) // 2
            cand = text[:mid] + "…" + text[-keep_end:]
            if font.measure(cand) <= px:
                best, lo = cand, mid + 1
            else:
                hi = mid - 1
        return best

    def relabel_rows(_=None):
        # the file column gets whatever the fixed columns leave over (min. 90 px)
        others = sum(int(tree.column(c, "width")) for c in cols)
        avail = tree.winfo_width() - others - 2
        if avail > 0:
            tree.column("#0", width=max(90, avail))
        # thumbnail (~64 px) + indent are part of the tree column
        px = max(40, int(tree.column("#0", "width")) - 64 - 32)
        for i in order:
            if i in items:
                tree.item(i, text="  " + elide(os.path.basename(items[i]["path"]), px, fbase))
    tree.bind("<Configure>", lambda e: root.after_idle(relabel_rows), add="+")
    tree.tag_configure("ok", foreground=C["ok"])
    tree.tag_configure("warn", foreground=C["warn"])
    tree.tag_configure("err", foreground=C["err"])
    tree.tag_configure("busy", foreground=C["accent_hi"])
    tree.grid(row=1, column=0, sticky="nsew")
    vsb = ttk.Scrollbar(qcard, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    vsb.grid(row=1, column=1, sticky="ns")

    drop_hint = tk.Label(
        qcard, bg=C["panel"], fg=C["muted"], font=F["base"], justify="center",
        text=("⬇\n\nDrop videos or folders here\nor click “Add videos…”" if dnd
              else "Click “Add videos…” to start\n\n(drag & drop: pip install tkinterdnd2)"))
    drop_hint.place(in_=tree, relx=0.5, rely=0.55, anchor="center")

    qbtns = ttk.Frame(qcard, style="Panel.TFrame")
    qbtns.grid(row=2, column=0, columnspan=2, sticky="we", pady=(10, 0))

    pcard = card(top, "Preview")
    pcard.grid(row=0, column=1, sticky="nsew")
    TW, TH = 288, 162
    blank = tk.PhotoImage(width=TW, height=TH)
    thumb = tk.Canvas(pcard, width=TW, height=TH, bg="#0f0f11", bd=0, highlightthickness=0,
                      cursor="sb_h_double_arrow")
    thumb.grid(row=1, column=0, sticky="n")
    t_img = thumb.create_image(TW // 2, TH // 2, image=blank, anchor="center")
    t_bar_bg = thumb.create_rectangle(0, TH - 4, TW, TH, fill="#000000", width=0,
                                      state="hidden")
    t_bar = thumb.create_rectangle(0, TH - 4, 0, TH, fill=C["accent"], width=0,
                                   state="hidden")
    t_tc_bg = thumb.create_rectangle(0, 0, 0, 0, fill="#000000", width=0, state="hidden")
    t_tc = thumb.create_text(TW - 8, TH - 10, anchor="se", text="", fill="#ffffff",
                             font=F["small"], state="hidden")
    t_hint_bg = thumb.create_rectangle(0, 0, 0, 0, fill="#000000", width=0, state="hidden")
    t_hint = thumb.create_text(8, TH - 10, anchor="sw", text="⇆ hover to scrub",
                               fill="#d0d0d4", font=F["small"], state="hidden")
    scrub = {"iid": None, "hover": False}

    def _boxed(text_item, bg_item, show):
        st_ = "normal" if show else "hidden"
        thumb.itemconfigure(text_item, state=st_)
        if show:
            x1, y1, x2, y2 = thumb.bbox(text_item)
            thumb.coords(bg_item, x1 - 5, y1 - 2, x2 + 5, y2 + 2)
            thumb.tag_raise(text_item)
        thumb.itemconfigure(bg_item, state=st_)

    def current_item():
        sel = tree.selection()
        iid = sel[0] if sel else (order[0] if order else None)
        return iid if iid in items else None

    def ensure_scrub(iid):
        it = items.get(iid)
        if not it or not it["info"] or it["scrub_started"]:
            return
        it["scrub_started"] = True
        path, dur = it["path"], it["info"].duration
        times = it["scrub_times"] = scrub_times(dur, it["info"].fps)
        todo = queue.Queue()
        for i in scrub_order(len(times)):
            todo.put(i)

        def work():
            while iid in items:
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                png = thumbnail_png(path, dur, TW, TH, t=times[i])
                if png:
                    msgs.put(("scrub", iid, i, png))
        for _ in range(SCRUB_WORKERS):
            threading.Thread(target=work, daemon=True).start()

    def on_thumb_motion(e):
        iid = current_item()
        if not iid or not items[iid]["info"]:
            return
        it = items[iid]
        ensure_scrub(iid)
        scrub["hover"] = True
        times = it.get("scrub_times") or [0.0]
        n = len(times)
        frac = min(max(e.x / (TW - 1), 0.0), 1.0)
        idx = int(round(frac * (n - 1)))      # far left = first frame, far right = last
        frames = it["scrub"]
        # nearest frame that's already extracted
        img = None
        for d in range(n):
            for j in (idx - d, idx + d):
                if 0 <= j < n and frames.get(j) is not None:
                    img = frames[j]
                    break
            if img:
                break
        thumb.itemconfigure(t_img, image=img or it["big"] or blank)
        thumb.coords(t_bar, 0, TH - 4, max(3, frac * TW), TH)
        thumb.itemconfigure(t_bar, state="normal")
        thumb.itemconfigure(t_bar_bg, state="normal")
        thumb.itemconfigure(t_tc, text=f"{fmt_dur(times[idx])} / {fmt_dur(it['info'].duration)}")
        _boxed(t_tc, t_tc_bg, True)
        _boxed(t_hint, t_hint_bg, False)

    def on_thumb_leave(_=None):
        scrub["hover"] = False
        iid = current_item()
        thumb.itemconfigure(t_img, image=(items[iid]["big"] if iid else None) or blank)
        for it_ in (t_bar, t_bar_bg):
            thumb.itemconfigure(it_, state="hidden")
        _boxed(t_tc, t_tc_bg, False)
        _boxed(t_hint, t_hint_bg, bool(iid and items[iid]["big"]))

    thumb.bind("<Motion>", on_thumb_motion)
    thumb.bind("<Enter>", on_thumb_motion)
    thumb.bind("<Leave>", on_thumb_leave)
    pname = ttk.Label(pcard, text="No video selected", style="Panel.TLabel", font=F["bold"],
                      wraplength=288)
    pname.grid(row=2, column=0, sticky="w", pady=(8, 2))
    pmeta = ttk.Label(pcard, text="", style="Muted.TLabel", justify="left", wraplength=288)
    pmeta.grid(row=3, column=0, sticky="nw")
    pplan = ttk.Label(pcard, text="", style="Panel.TLabel", justify="left", wraplength=288,
                      font=F["small"])
    pplan.grid(row=4, column=0, sticky="nw", pady=(8, 0))

    # --- settings
    sets = ttk.Frame(outer)
    sets.grid(row=2, column=0, sticky="we", pady=(12, 0))
    for i in range(3):
        sets.columnconfigure(i, weight=1, uniform="sets")

    def field(parent, r, label, widget, tip, c=0):
        lab = ttk.Label(parent, text=label, style="Panel.TLabel")
        lab.grid(row=r, column=c, sticky="w", padx=(0, 10), pady=4)
        widget.grid(row=r, column=c + 1, sticky="we", pady=4)
        Tooltip(lab, TIPS[tip])
        Tooltip(widget, TIPS[tip])
        return widget

    def combo(parent, var, values, width=12, ro=True):
        return ttk.Combobox(parent, textvariable=var, values=values, width=width,
                            state="readonly" if ro else "normal", font=F["base"])

    # left column: Output (size) above Advanced · right column: Audio above Save
    _lf = tkfont.Font(font=F["base"])
    lab_w = max(_lf.measure(t) for t in ("Target size", "Platform", "Safety margin", "Deinterlace",
                                          "Keyframe every", "ffmpeg args", "Bitrate (kbps)",
                                          "Channels", "Save to", "Name suffix")) + 14
    lcol = ttk.Frame(sets)
    lcol.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    rcol = ttk.Frame(sets)
    rcol.grid(row=0, column=2, sticky="nsew")
    for col in (lcol, rcol):
        col.columnconfigure(0, weight=1)
        col.rowconfigure(1, weight=1)

    def panel(parent, title, row):
        f = card(parent, title)
        f.grid(row=row, column=0, sticky="nsew", pady=(0, 12) if row == 0 else 0)
        f.columnconfigure(0, minsize=lab_w)
        f.columnconfigure(1, weight=1)
        return f

    oc = panel(lcol, "Output", 0)

    # target size: [ 5 ] [GB ▾] [Presets ▾]
    tf = ttk.Frame(oc, style="Panel.TFrame")
    te = ttk.Entry(tf, textvariable=V["target"], width=5, font=F["base"], justify="right")
    te.grid(row=0, column=0, sticky="w")
    uc = combo(tf, V["unit"], ["GB", "MB"], 3)
    uc.grid(row=0, column=1, sticky="w", padx=(6, 0))
    tf.columnconfigure(2, weight=1)
    Tooltip(uc, TIPS["unit"])
    pm = tk.Menu(tf, tearoff=0, bg=C["field"], fg=C["fg"], activebackground=C["accent"],
                 activeforeground="#ffffff", bd=0, font=F["base"])

    def apply_preset(label):
        num, unit = label.split("–")[0].split()
        V["target"].set(num)
        V["unit"].set(unit)
    for label in SIZE_PRESETS:
        pm.add_command(label=label, command=lambda l=label: apply_preset(l))
    mb = ttk.Menubutton(oc, text="", menu=pm, style="Small.TMenubutton")

    def update_preset_btn(*_):
        key = f'{V["target"].get().strip().replace(",", ".")} {V["unit"].get()}'
        try:  # normalise "5.0" -> "5"
            n = float(key.split()[0])
            key = f"{n:g} {V['unit'].get()}"
        except (ValueError, IndexError):
            pass
        mb.config(text=f"{PRESET_SHORT.get(key, 'Custom')}  ▾")
    V["target"].trace_add("write", update_preset_btn)
    V["unit"].trace_add("write", update_preset_btn)
    update_preset_btn()
    field(oc, 1, "Target size", tf, "target")
    field(oc, 2, "Platform", mb, "target")

    # safety margin: [2.0] %
    sfm = ttk.Frame(oc, style="Panel.TFrame")
    ttk.Spinbox(sfm, from_=0, to=20, increment=0.5, textvariable=V["safety"], width=5,
                font=F["base"], justify="right").pack(side="left")
    ttk.Label(sfm, text="%", style="Panel.TLabel").pack(side="left", padx=(6, 0))
    field(oc, 3, "Safety margin", sfm, "safety")

    # save to: [Next to source file]  [Choose…] [×]
    sv = panel(rcol, "Save", 1)
    of = ttk.Frame(sv, style="Panel.TFrame")
    of.columnconfigure(0, weight=1)
    # one button that shows the current target and opens the folder picker
    dir_lbl = ttk.Button(of, text="", style="Small.TButton", command=lambda: V["outdir"].set(
        filedialog.askdirectory() or V["outdir"].get()))
    dir_lbl.grid(row=0, column=0, sticky="we")
    dir_tip = Tooltip(dir_lbl, TIPS["outdir"])
    reset_btn = ttk.Button(of, text="×", width=2, style="Small.TButton",
                           command=lambda: V["outdir"].set(""))
    reset_btn.grid(row=0, column=2, padx=(4, 0))
    field(sv, 1, "Save to", of, "outdir")

    # file name: [_vimeo]  + example
    field(sv, 2, "Name suffix", ttk.Entry(sv, textvariable=V["suffix"], width=10,
                                          font=F["base"]), "suffix")
    name_ex = ttk.Label(sv, text="", style="Muted.TLabel")
    name_ex.grid(row=3, column=1, sticky="w", pady=(0, 2))

    def update_output_labels(*_):
        d = V["outdir"].get()
        if d:
            name = os.path.basename(d.rstrip("/\\")) or d
            dir_lbl.config(text="📁 " + (name if len(name) <= 16 else name[:15] + "…"))
            dir_tip.text = d
            reset_btn.state(["!disabled"])
        else:
            dir_lbl.config(text="Next to source…")
            dir_tip.text = TIPS["outdir"]
            reset_btn.state(["disabled"])
        ext = ".mp4"
        sel = tree.selection()
        name = "movie"
        try:
            iid = sel[0] if sel else (order[0] if order else None)
            if iid:
                name = os.path.splitext(os.path.basename(items[iid]["path"]))[0]
                ext = output_ext(items[iid]["info"], settings())
        except Exception:
            pass
        short = name if len(name) <= 14 else name[:13] + "…"
        name_ex.config(text=f"→ {short}{V['suffix'].get()}{ext}")
    V["outdir"].trace_add("write", update_output_labels)
    V["suffix"].trace_add("write", update_output_labels)

    vc = card(sets, "Video")
    vc.grid(row=0, column=1, sticky="nsew", padx=(0, 12))
    vc.columnconfigure(1, weight=1)
    field(vc, 1, "Codec", combo(vc, V["codec"], list(CODECS), 26), "codec")
    # preset slider: ultrafast (0) … veryslow (8)
    pf = ttk.Frame(vc, style="Panel.TFrame")
    p_idx = tk.DoubleVar(value=PRESETS.index(V["preset"].get()))
    p_scale = ttk.Scale(pf, from_=0, to=len(PRESETS) - 1, orient="horizontal",
                        variable=p_idx, style="Horizontal.TScale")
    p_scale.grid(row=0, column=0, columnspan=3, sticky="we", pady=(4, 0))
    ttk.Label(pf, text="faster", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
    p_name = ttk.Label(pf, text="", style="Panel.TLabel", font=F["small"])
    p_name.grid(row=1, column=1)
    ttk.Label(pf, text="better", style="Muted.TLabel").grid(row=1, column=2, sticky="e")
    pf.columnconfigure(0, uniform="ends")
    pf.columnconfigure(2, uniform="ends")
    pf.columnconfigure(1, weight=1)

    def on_preset_slide(*_):
        i = int(round(p_idx.get()))
        if abs(p_idx.get() - i) > 1e-6:
            p_idx.set(i)            # snap to whole steps
        if V["preset"].get() != PRESETS[i]:
            V["preset"].set(PRESETS[i])
        p_name.config(text=PRESETS[i])
    p_idx.trace_add("write", on_preset_slide)
    on_preset_slide()
    field(vc, 2, "Preset", pf, "preset")
    Tooltip(p_scale, TIPS["preset"])

    def codec_state(*_):  # presets only apply to x264/x265
        hw = CODECS[V["codec"].get()].endswith("videotoolbox")
        p_scale.state(["disabled"] if hw else ["!disabled"])
        p_name.config(text="n/a (hardware)" if hw else PRESETS[int(round(p_idx.get()))])
    V["codec"].trace_add("write", codec_state)
    field(vc, 3, "Resolution", combo(vc, V["res"], list(RESOLUTIONS), 22), "res")
    field(vc, 4, "Frame rate", combo(vc, V["fps"], FPS_CHOICES), "fps")
    chk = ttk.Frame(vc, style="Panel.TFrame")
    chk.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
    checks = [("vcap", "Cap at Vimeo's recommended bitrate"),
              ("scap", "Never exceed source bitrate"),
              ("ten", "10-bit colour")]
    if IS_MAC:   # VideoToolbox decoding only exists on macOS
        checks.append(("hwdec", "Hardware decoding (Apple)"))
    for r, (key, txt) in enumerate(checks):
        cb = ttk.Checkbutton(chk, text=txt, variable=V[key])
        cb.grid(row=r, column=0, sticky="w", pady=2)
        Tooltip(cb, TIPS[key])

    ac = panel(rcol, "Audio", 0)
    acb = ttk.Checkbutton(ac, text="Don't compress audio", variable=V["acopy"])
    acb.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
    Tooltip(acb, TIPS["acopy"])
    abr_w = field(ac, 2, "Bitrate (kbps)", combo(ac, V["abr"],
                  ["128", "192", "256", "320", "384", "512"], 8, ro=False), "abr")
    ach_w = field(ac, 3, "Channels", combo(ac, V["ach"], ["Auto", "Stereo", "5.1"]), "ach")

    def audio_state(*_):
        if V["acopy"].get():
            abr_w.state(["disabled"])
            ach_w.state(["disabled"])
        else:
            abr_w.state(["!disabled"])
            ach_w.state(["!disabled"])
            ach_w.state(["readonly"])
        update_output_labels()
    V["acopy"].trace_add("write", audio_state)

    adv = panel(lcol, "Advanced", 1)
    field(adv, 1, "Deinterlace", combo(adv, V["deint"], ["Auto", "On", "Off"]), "deint")
    gf = ttk.Frame(adv, style="Panel.TFrame")
    ttk.Spinbox(gf, from_=0.5, to=10, increment=0.5, textvariable=V["gop"], width=5,
                font=F["base"], justify="right").pack(side="left")
    ttk.Label(gf, text="s", style="Panel.TLabel").pack(side="left", padx=(6, 0))
    field(adv, 2, "Keyframe every", gf, "gop")
    field(adv, 3, "ffmpeg args", ttk.Entry(adv, textvariable=V["extra"], width=12,
          font=F["base"]), "extra")

    # --- progress, log, actions
    bottom = ttk.Frame(outer)
    bottom.grid(row=3, column=0, sticky="we", pady=(14, 0))
    bottom.columnconfigure(0, weight=1)
    pbar = ttk.Progressbar(bottom, maximum=1000, style="Horizontal.TProgressbar")
    pbar.grid(row=0, column=0, sticky="we", pady=(0, 6))
    status = ttk.Label(bottom, text="Ready", style="MutedBg.TLabel")
    status.grid(row=1, column=0, sticky="w")

    actions = ttk.Frame(bottom)
    actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
    log_btn = ttk.Button(actions, text="Show log")
    log_btn.pack(side="left", padx=(0, 8))
    stop_btn = ttk.Button(actions, text="Cancel", state="disabled")
    stop_btn.pack(side="left", padx=(0, 8))
    start_btn = ttk.Button(actions, text="Compress", style="Accent.TButton")
    start_btn.pack(side="left")

    logf = ttk.Frame(outer, style="Panel.TFrame", padding=8)
    logf.columnconfigure(0, weight=1)
    log = tk.Text(logf, height=9, wrap="word", font=F["mono"], bg=C["panel"], fg=C["fg"],
                  insertbackground=C["fg"], relief="flat", highlightthickness=0, bd=0)
    lsb = ttk.Scrollbar(logf, command=log.yview)
    log.config(yscrollcommand=lsb.set)
    log.grid(row=0, column=0, sticky="nsew")
    lsb.grid(row=0, column=1, sticky="ns")

    def toggle_log():
        state["log_open"] = not state["log_open"]
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        grow = 210
        if state["log_open"]:
            logf.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
            log_btn.config(text="Hide log")
            maxh = root.winfo_screenheight() - 80
            root.geometry(f"{w}x{min(h + grow, maxh)}")
        else:
            logf.grid_remove()
            log_btn.config(text="Show log")
            root.geometry(f"{w}x{max(h - grow, 720)}")
    log_btn.config(command=toggle_log)

    def logline(m):
        log.insert("end", m + "\n")
        log.see("end")

    # ---------- queue handling
    def add_paths(paths):
        added = False
        for p in paths:
            if os.path.isdir(p):
                for dp, _, fs in os.walk(p):
                    for f in sorted(fs):
                        if os.path.splitext(f)[1].lower() in VIDEO_EXT and not f.startswith("."):
                            added |= add_one(os.path.join(dp, f))
            elif os.path.isfile(p):
                added |= add_one(p)
        refresh_hint()
        if added and not tree.selection():
            tree.selection_set(order[0])

    def add_one(p):
        p = os.path.abspath(p)
        if any(items[i]["path"] == p for i in order):
            return False
        iid = tree.insert("", "end", text="",
                          values=("…", "…", fmt_size(os.path.getsize(p)), "analysing…"))
        items[iid] = dict(path=p, info=None, big=None, small=None, result="", error="",
                          status=None,   # set once encoding starts: replaces the estimate
                          scrub={}, scrub_started=False, scrub_times=None)
        order.append(iid)
        root.after_idle(relabel_rows)
        threading.Thread(target=analyse, args=(iid, p), daemon=True).start()
        return True

    def analyse(iid, p):
        try:
            info = probe(p)
            msgs.put(("info", iid, info))
            png = thumbnail_png(p, info.duration)
            if png:
                msgs.put(("thumb", iid, png))
        except Exception as e:
            msgs.put(("info_err", iid, str(e)))

    def remove_selected():
        if state["running"]:
            return
        for iid in tree.selection():
            tree.delete(iid)
            order.remove(iid)
            items.pop(iid, None)
        refresh_hint()
        show_preview()

    def clear_all():
        if state["running"]:
            return
        for iid in list(order):
            tree.delete(iid)
        order.clear()
        items.clear()
        refresh_hint()
        show_preview()

    def refresh_hint():
        if order:
            drop_hint.place_forget()
        else:
            drop_hint.place(in_=tree, relx=0.5, rely=0.55, anchor="center")

    def add_dialog():
        add_paths(filedialog.askopenfilenames(title="Choose videos", filetypes=[
            ("Video", " ".join("*" + e for e in sorted(VIDEO_EXT))), ("All files", "*")]))

    ttk.Button(qbtns, text="Add videos…", command=add_dialog).pack(side="left")
    ttk.Button(qbtns, text="Remove", command=remove_selected).pack(side="left", padx=8)
    ttk.Button(qbtns, text="Clear", command=clear_all).pack(side="left")
    total_lbl = ttk.Label(qbtns, text="", style="Muted.TLabel")
    total_lbl.pack(side="right")

    if dnd:
        def on_drop(e):
            add_paths(root.tk.splitlist(e.data))
            return e.action
        for w in (tree, drop_hint, qcard):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", on_drop)
            w.dnd_bind("<<DropEnter>>", lambda e: (qcard.configure(style="Panel.TFrame"),
                                                   drop_hint.configure(fg=C["accent_hi"]),
                                                   e.action)[-1])
            w.dnd_bind("<<DropLeave>>", lambda e: (drop_hint.configure(fg=C["muted"]),
                                                   e.action)[-1])

    # ---------- estimates & preview
    def plan_for(iid, s=None):
        it = items.get(iid)
        if not it or not it["info"]:
            return None, None
        try:
            return make_plan(it["info"], s or settings()), None
        except Exception as e:
            return None, str(e)

    def refresh_estimates(*_):
        try:
            s = settings()
        except (tk.TclError, ValueError):
            return
        tot_in = tot_out = 0
        for iid in order:
            it = items[iid]
            if not it["info"]:
                continue
            p, err = plan_for(iid, s)
            if p:
                tot_in += it["info"].size_bytes
                tot_out += p.est_bytes
                est = f"≈ {fmt_size(p.est_bytes)} ({reduction(it['info'].size_bytes, p.est_bytes)})"
            else:
                est = "too long ✗"
            if not it["status"]:          # estimate only until encoding has started
                tree.set(iid, "out", est)
                tree.item(iid, tags=() if p else ("warn",))
        total_lbl.config(text=(f"{len(order)} file(s) · {fmt_size(tot_in)} → "
                               f"≈ {fmt_size(tot_out)} ({reduction(tot_in, tot_out)})")
                         if tot_in else "")
        show_preview()

    pending = {"id": None}

    def schedule_refresh(*_):
        if pending["id"]:
            root.after_cancel(pending["id"])
        pending["id"] = root.after(250, refresh_estimates)

    for k in ("target", "unit", "acopy", "safety", "codec", "res", "fps", "abr", "ach", "vcap", "scap", "ten",
              "deint"):
        V[k].trace_add("write", schedule_refresh)

    def show_preview(*_):
        update_output_labels()
        sel = tree.selection()
        iid = sel[0] if sel else (order[0] if order else None)
        if not iid or iid not in items:
            on_thumb_leave()
            pname.config(text="No video selected")
            pmeta.config(text="")
            pplan.config(text="")
            return
        it = items[iid]
        if not scrub["hover"]:
            on_thumb_leave()
        ensure_scrub(iid)
        pname.config(text=elide(os.path.basename(it["path"]), TW, tkfont.Font(font=F["bold"])))
        info = it["info"]
        if it["error"]:
            pmeta.config(text=it["error"])
            pplan.config(text="")
            return
        if not info:
            pmeta.config(text="Analysing…")
            return
        dw, dh = display_dims(info)
        a = ("no audio" if not info.has_audio else
             f"{info.audio_channels} ch audio")
        pmeta.config(text=(f"{info.vcodec.upper()} · {dw}×{dh} · {fmt_fps(info.fps)} fps"
                           f"{' (VFR)' if info.vfr else ''}{' · interlaced' if info.interlaced else ''}"
                           f"{' · HDR' if info.hdr else ''}\n"
                           f"{fmt_dur(info.duration)} · {fmt_size(info.size_bytes)} · "
                           f"{info.video_kbps/1000:.1f} Mbps · {a}"))
        p, err = plan_for(iid)
        if err:
            pplan.config(text="⚠ " + err, foreground=C["warn"])
            return
        txt = (f"→  {p.out_w}×{p.out_h} · {fmt_fps(p.out_fps)} fps\n"
               f"→  video {p.video_kbps/1000:.2f} Mbps\n"
               f"→  audio {'copy' if settings().audio_copy and info.has_audio else ''}"
               f"{'' if settings().audio_copy and info.has_audio else str(p.audio_kbps) + ' kbps'}"
               f" · {'5.1' if p.audio_ch == 6 else 'stereo' if p.audio_ch == 2 else str(p.audio_ch) + ' ch'}\n"
               f"→  ≈ {fmt_size(p.est_bytes)}  ({reduction(info.size_bytes, p.est_bytes)})")
        if it["result"]:
            txt += f"\n\n✓ Done: {it['result']}"
        if p.notes:
            txt += "\n\n• " + "\n• ".join(p.notes)
        pplan.config(text=txt, foreground=C["fg"])

    tree.bind("<<TreeviewSelect>>", show_preview)
    tree.bind("<Delete>", lambda e: remove_selected())
    tree.bind("<BackSpace>", lambda e: remove_selected())

    # ---------- encoding
    def worker(iids, s):
        n = len(iids)
        for i, iid in enumerate(iids):
            if stop_event.is_set():
                break
            it = items.get(iid)
            if not it or not it["info"]:
                continue
            msgs.put(("row", iid, "Starting…", "busy"))

            def prog(f, speed, i=i, iid=iid):
                msgs.put(("progress", (i + f) / n,
                          f"File {i+1} of {n} · {f*100:4.1f} %" + (f" · {speed}" if speed else "")))
                msgs.put(("row", iid, f"Encoding {f*100:.0f} %", "busy"))
            try:
                out, size = Job(it["path"], s, lambda m: msgs.put(("log", m)), prog,
                                stop_event, it["info"]).run()
                red = reduction(it["info"].size_bytes, size)
                over = size > s.target_gb * 1e9
                msgs.put(("row", iid, f"{'⚠ ' if over else '✓ '}{fmt_size(size)} ({red})",
                          "warn" if over else "ok"))
                it["result"] = f"{fmt_size(size)} ({red} vs. source)"
            except Cancelled:
                msgs.put(("row", iid, "Cancelled", "warn"))
                msgs.put(("log", "  ✗ Cancelled."))
                break
            except Exception as e:
                msgs.put(("row", iid, "✗ Failed – see log", "err"))
                msgs.put(("log", f"  ✗ {os.path.basename(it['path'])}: {e}"))
        msgs.put(("done", None))

    def start():
        todo = [i for i in order if items[i]["info"]]
        if not todo:
            messagebox.showinfo(APP_NAME, "Add some videos first.")
            return
        try:
            s = settings()
        except (tk.TclError, ValueError) as e:
            messagebox.showerror("Invalid setting", str(e))
            return
        if s.codec not in available_encoders():
            messagebox.showerror("Codec", f"Your ffmpeg doesn't support '{s.codec}'. "
                                 "Pick another codec or install a full ffmpeg build (see README).")
            return
        bad = [os.path.basename(items[i]["path"]) for i in todo if plan_for(i, s)[1]]
        if bad:
            messagebox.showerror("Too long", "These files don't fit the target size:\n\n"
                                 + "\n".join(bad))
            return
        if s.out_dir and not os.path.isdir(s.out_dir):
            messagebox.showerror("Output folder", "The output folder doesn't exist.")
            return
        existing = [output_path(items[i]["path"], s, items[i]["info"]) for i in todo
                    if os.path.exists(output_path(items[i]["path"], s, items[i]["info"]))]
        if existing and not messagebox.askyesno(
                "Overwrite?", f"{len(existing)} output file(s) already exist. Overwrite?"):
            return
        stop_event.clear()
        state["running"] = True
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        pbar["value"] = 0
        threading.Thread(target=worker, args=(todo, s), daemon=True).start()

    def stop():
        stop_event.set()
        stop_btn.config(state="disabled")
        status.config(text="Cancelling…")

    start_btn.config(command=start)
    stop_btn.config(command=stop)

    def ffmpeg_install_cmd():
        """(command, description) for installing ffmpeg here, or (None, manual hint)."""
        if IS_MAC:
            brew = find_tool("brew")
            if brew:
                return [brew, "install", "ffmpeg"], "Homebrew"
            return None, ("Homebrew isn't installed. Launch 'Common Shrew.command' instead – it "
                          "offers to install Homebrew and ffmpeg – or see https://brew.sh")
        if IS_WIN:
            winget = shutil.which("winget")
            if winget:
                return [winget, "install", "--id", "Gyan.FFmpeg", "-e",
                        "--accept-source-agreements", "--accept-package-agreements"], "winget"
            return None, ("winget isn't available. Download ffmpeg from "
                          "https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH.")
        return None, ("Install ffmpeg with your package manager, e.g.\n\n"
                      "  sudo apt install ffmpeg\n  sudo dnf install ffmpeg\n"
                      "  sudo pacman -S ffmpeg\n\nor start 'common-shrew.sh', which offers to "
                      "do it for you.")

    def install_ffmpeg():
        cmd, how = ffmpeg_install_cmd()
        if not cmd:
            messagebox.showinfo("Install ffmpeg", how)
            return
        start_btn.config(state="disabled")
        if not state["log_open"]:
            toggle_log()
        msgs.put(("log", f"Installing ffmpeg via {how} … (this can take a few minutes)"))
        status.config(text="Installing ffmpeg…")

        def work():
            global FFMPEG, FFPROBE
            env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", env=env,
                                 **POPEN_KW)
            for line in p.stdout:
                msgs.put(("log", "  " + line.rstrip()))
            p.wait()
            FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")
            ok = p.returncode == 0 and FFMPEG and FFPROBE
            msgs.put(("log", "✓ ffmpeg installed." if ok else
                      "✗ Installation failed – or ffmpeg isn't on PATH yet: restart the app."))
            msgs.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def pump():
        try:
            while True:
                m = msgs.get_nowait()
                kind = m[0]
                if kind == "log":
                    logline(m[1])
                elif kind == "progress":
                    pbar["value"] = m[1] * 1000
                    status.config(text=m[2])
                elif kind == "row":
                    if m[1] in items:
                        items[m[1]]["status"] = m[2]
                        tree.set(m[1], "out", m[2])
                        tree.item(m[1], tags=(m[3],))
                elif kind == "info":
                    iid, info = m[1], m[2]
                    if iid in items:
                        items[iid]["info"] = info
                        dw, dh = display_dims(info)
                        tree.set(iid, "dur", fmt_dur(info.duration))
                        tree.set(iid, "res", f"{dw}×{dh}")
                        refresh_estimates()
                elif kind == "info_err":
                    iid = m[1]
                    if iid in items:
                        items[iid]["error"] = m[2]
                        items[iid]["status"] = "✗ unreadable"
                        tree.set(iid, "out", "✗ unreadable")
                        tree.item(iid, tags=("err",))
                        show_preview()
                elif kind == "scrub":
                    iid, idx = m[1], m[2]
                    if iid in items:
                        try:
                            items[iid]["scrub"][idx] = tk.PhotoImage(
                                data=base64.b64encode(m[3]).decode())
                        except tk.TclError:
                            pass
                elif kind == "thumb":
                    iid = m[1]
                    if iid in items:
                        try:
                            img = tk.PhotoImage(data=base64.b64encode(m[2]).decode())
                            items[iid]["big"] = img
                            f = max(1, math.ceil(max(img.width() / 64, img.height() / 40)))
                            items[iid]["small"] = img.subsample(f, f)
                            tree.item(iid, image=items[iid]["small"])
                            show_preview()
                        except tk.TclError:
                            pass
                elif kind == "done":
                    was = state["running"]
                    state["running"] = False
                    start_btn.config(state="normal")
                    stop_btn.config(state="disabled")
                    if was:
                        pbar["value"] = 1000 if not stop_event.is_set() else pbar["value"]
                        status.config(text="Cancelled" if stop_event.is_set() else "Done ✓")
                        logline("— finished —")
                    else:
                        status.config(text="Ready")
                    update_ff_label()
                    show_preview()
        except queue.Empty:
            pass
        root.after(120, pump)

    def update_ff_label():
        ff_lbl.config(text=("ffmpeg ✓" if FFMPEG and FFPROBE else "ffmpeg missing ✗")
                      + ("   ·   drag & drop ✓" if dnd else ""))

    def on_close():
        if state["running"] and not messagebox.askyesno(
                "Quit", "Compression is still running. Cancel it and quit?"):
            return
        stop_event.set()
        root.after(300, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)
    update_ff_label()
    pump()
    if not FFMPEG or not FFPROBE:
        root.after(300, lambda: messagebox.askyesno(
            "ffmpeg missing", "ffmpeg/ffprobe were not found.\n\nInstall it now?")
            and install_ffmpeg())
    # files passed on the command line (e.g. dropped on the launcher)
    args = [a for a in sys.argv[1:] if os.path.exists(a)]
    if args:
        root.after(200, lambda: add_paths(args))
    root.mainloop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run_cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="common_shrew.py",
                                 description=f"{APP_NAME} – shrink videos to a target size "
                                             "following Vimeo's upload recommendations")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--size", type=parse_size_gb, default=5.0,
                    help="target size, e.g. 5, 4GB, 750MB (default 5 GB)")
    ap.add_argument("--safety", type=float, default=2.0, help="safety margin in %%")
    ap.add_argument("--codec", default="libx265", choices=list(CODECS.values()))
    ap.add_argument("--preset", default="medium", choices=PRESETS)
    ap.add_argument("--res", default="Original", choices=["Original", "2160", "1440",
                                                           "1080", "720", "480"])
    ap.add_argument("--fps", default="Original", choices=FPS_CHOICES)
    ap.add_argument("--audio", type=int, default=320)
    ap.add_argument("--channels", default="Auto", choices=["Auto", "Stereo", "5.1"])
    ap.add_argument("--copy-audio", action="store_true", help="keep original audio untouched")
    ap.add_argument("--no-vimeo-cap", action="store_true")
    ap.add_argument("--no-source-cap", action="store_true")
    ap.add_argument("--10bit", dest="ten", action="store_true")
    ap.add_argument("--deinterlace", default="Auto", choices=["Auto", "On", "Off"])
    ap.add_argument("--no-hwdec", action="store_true",
                    help="don't use VideoToolbox hardware decoding (macOS)")
    ap.add_argument("--outdir", default="")
    ap.add_argument("--dry-run", action="store_true", help="only show plan + command")
    a = ap.parse_args(argv)
    res = "Original" if a.res == "Original" else next(
        k for k, v in RESOLUTIONS.items() if v and str(v[1]) == a.res)
    s = Settings(target_gb=a.size, safety_pct=a.safety, codec=a.codec, preset=a.preset,
                 resolution=res, fps=a.fps, audio_kbps=a.audio, audio_channels=a.channels,
                 vimeo_cap=not a.no_vimeo_cap, source_cap=not a.no_source_cap,
                 ten_bit=a.ten, deinterlace=a.deinterlace, out_dir=a.outdir,
                 hw_decode=not a.no_hwdec,
                 audio_copy=a.copy_audio)
    stop = threading.Event()
    for f in a.files:
        if a.dry_run:
            info = probe(f)
            plan = make_plan(info, s)
            print(f"{f}: {plan.out_w}x{plan.out_h} @ {fmt_fps(plan.out_fps)} fps, "
                  f"{plan.video_kbps} kbps, est {fmt_size(plan.est_bytes)}")
            for n in plan.notes:
                print("  •", n)
            print(" ".join(_q(c) for c in build_cmd(plan, s, output_path(f, s, info), 2, "/tmp/p")))
            continue

        def prog(x, speed):
            print(f"\r  {x*100:5.1f} %  {speed}   ", end="", flush=True)
        Job(f, s, lambda m: print("\r" + m), prog, stop).run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        run_cli(sys.argv[2:])
    else:
        run_gui()
