#!/usr/bin/env python3
"""
Common Shrew – shrinks videos to a target size (default 5 GB) following
Vimeo's upload recommendations (help.vimeo.com, "Video compression settings"):

  * H.264 or H.265, progressive, constant frame rate (max. 60 fps)
  * Bitrate ceilings per resolution (SD 5 / 720p 10 / 1080p 20 / 2K 30 / 4K 60 Mbps)
  * AAC audio 320 kbps, 48 kHz, stereo or 5.1 – an audio track is required
    (a silent one is added if missing)
  * Square pixels, BT.709 colour tags for SDR, MP4 with faststart

Requires ffmpeg + ffprobe (brew install ffmpeg) and Python with Tkinter
(brew install python-tk). Optional drag & drop: pip install tkinterdnd2.
Without GUI:  python3 common_shrew.py --cli movie.mov --size 5

Named after the common shrew (Sorex araneus), which shrinks in winter
(Dehnel's phenomenon) and grows back in spring.
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
CODECS = {   # first entry = default
    "H.265 · x265 2-pass (precise)": "libx265",
    "H.264 · x264 2-pass (precise)": "libx264",
    "H.264 · Apple hardware (fast)": "h264_videotoolbox",
    "H.265 · Apple hardware (fast)": "hevc_videotoolbox",
}
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
    """Find ffmpeg/ffprobe even when launched by double-click without a PATH."""
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def ffmpeg_has_fps_mode():
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True).stdout
        m = re.search(r"ffmpeg version n?(\d+)", out)
        return not m or int(m.group(1)) >= 5
    except Exception:
        return True


def available_encoders():
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
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
        raise RuntimeError("ffprobe not found – run 'brew install ffmpeg'.")
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True)
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


def thumbnail_png(path, duration, w=288, h=162):
    """Grab one frame (~10 % in) as PNG bytes, fitted into w×h."""
    t = min(max(duration * 0.1, 0), max(duration - 0.5, 0))
    cmd = [FFMPEG, "-v", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease",
           "-f", "image2pipe", "-c:v", "png", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 and r.stdout else None


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
            notes.append(f"Variable/unusual frame rate {out_fps:.3f} → constant {snapped}.")
            out_fps = snapped
    if out_fps > 60.01:
        new = out_fps / math.ceil(out_fps / 60.01)
        notes.append(f"{out_fps:.2f} fps exceeds Vimeo's 60 fps → reduced to {new:.3f}.")
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
    cmd = [FFMPEG, "-hide_banner", "-y", "-nostdin", "-i", info.path]
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
                                     text=True, bufsize=1)
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
        self.log(f"  {fmt_dur(info.duration)}, {info.width}×{info.height} @ {info.fps:.3f} fps "
                 f"→ {plan.out_w}×{plan.out_h} @ {plan.out_fps:.3f} fps")
        self.log(f"  video {plan.video_kbps/1000:.2f} Mbps · audio {plan.audio_kbps} kbps "
                 f"({'5.1' if plan.audio_ch == 6 else 'stereo'}) · est. {fmt_size(plan.est_bytes)}")
        for n in plan.notes:
            self.log("  • " + n)
        tmpdir = tempfile.mkdtemp(prefix="shrew_2pass_")
        passlog = os.path.join(tmpdir, "pass")
        try:
            if self.s.codec in ("libx264", "libx265"):
                self._run(build_cmd(plan, self.s, out, 1, passlog), info.duration, 0, 0.5)
                self._run(build_cmd(plan, self.s, out, 2, passlog), info.duration, 0.5, 1)
            else:
                self._run(build_cmd(plan, self.s, out, 0), info.duration, 0, 1)
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
             "size less precise.\n"
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
    "outdir": "Where the compressed files are saved. By default next to each source file. "
              "'Choose…' picks one folder for all files, '×' resets to next-to-source.",
    "acopy": "Keep the original audio track bit-for-bit instead of re-encoding it to AAC. "
             "Best quality, but it uses more of the size budget (uncompressed PCM ≈ 2.3 Mbps "
             "for 48 kHz / 24-bit stereo) – the video bitrate is reduced accordingly.\n\n"
             "MP4 can't hold PCM audio, so in that case the output is saved as .mov "
             "(Vimeo accepts both). Videos without sound still get a silent AAC track.",
    "unit": "Unit for the target size: GB or MB (decimal, 1 GB = 1,000 MB).",
    "extra": "Additional ffmpeg output options, e.g.  -tune film  or  -metadata title=\"My Film\".\n"
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
    root.geometry(f"1220x{min(930, root.winfo_screenheight() - 70)}")
    root.minsize(980, 720)
    root.configure(bg=C["bg"])

    # ---------- fonts & theme
    base = tkfont.nametofont("TkDefaultFont")
    fam = base.actual("family")
    F = {
        "base": (fam, 13), "small": (fam, 11), "bold": (fam, 13, "bold"),
        "h1": (fam, 20, "bold"), "h2": (fam, 12, "bold"), "mono": ("Menlo", 11),
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
    cols = ("dur", "res", "size", "est", "result")
    tree = ttk.Treeview(qcard, columns=cols, show="tree headings", selectmode="extended")
    tree.heading("#0", text="  File", anchor="w")
    tree.column("#0", width=230, minwidth=150, stretch=True)
    for c, t, w in (("dur", "Duration", 74), ("res", "Resolution", 96), ("size", "Size", 70),
                    ("est", "Estimate", 128), ("result", "Result", 150)):
        tree.heading(c, text=t, anchor="w")
        tree.column(c, width=w, minwidth=50, stretch=(c == "result"), anchor="w")
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
    blank = tk.PhotoImage(width=288, height=162)
    thumb_lbl = tk.Label(pcard, image=blank, bg="#0f0f11", width=288, height=162,
                         bd=0, highlightthickness=0)
    thumb_lbl.grid(row=1, column=0, sticky="n")
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
    sets.columnconfigure(0, weight=5, minsize=400)
    sets.columnconfigure(1, weight=4)
    sets.columnconfigure(2, weight=4)

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

    oc = card(sets, "Output")
    oc.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    oc.columnconfigure(1, weight=1)

    # target size: [ 5 ] [GB ▾] [Presets ▾]
    tf = ttk.Frame(oc, style="Panel.TFrame")
    te = ttk.Entry(tf, textvariable=V["target"], width=7, font=F["base"], justify="right")
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
    mb = ttk.Menubutton(tf, text="", menu=pm, style="Small.TMenubutton", width=17)
    mb.grid(row=0, column=3, sticky="e", padx=(8, 0))

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
    Tooltip(mb, TIPS["target"])
    field(oc, 1, "Target size", tf, "target")

    # safety margin: [2.0] %
    sfm = ttk.Frame(oc, style="Panel.TFrame")
    ttk.Spinbox(sfm, from_=0, to=20, increment=0.5, textvariable=V["safety"], width=5,
                font=F["base"], justify="right").pack(side="left")
    ttk.Label(sfm, text="%", style="Panel.TLabel").pack(side="left", padx=(6, 0))
    field(oc, 2, "Safety margin", sfm, "safety")

    ttk.Separator(oc).grid(row=3, column=0, columnspan=2, sticky="we", pady=8)

    # save to: [Next to source file]  [Choose…] [×]
    of = ttk.Frame(oc, style="Panel.TFrame")
    of.columnconfigure(0, weight=1)
    dir_lbl = ttk.Label(of, text="", style="Panel.TLabel", width=12, anchor="w")
    dir_lbl.grid(row=0, column=0, sticky="we")
    dir_tip = Tooltip(dir_lbl, TIPS["outdir"])
    ttk.Button(of, text="Choose…", style="Small.TButton", command=lambda: V["outdir"].set(
        filedialog.askdirectory() or V["outdir"].get())).grid(row=0, column=1, padx=(6, 0))
    reset_btn = ttk.Button(of, text="×", width=2, style="Small.TButton",
                           command=lambda: V["outdir"].set(""))
    reset_btn.grid(row=0, column=2, padx=(4, 0))
    field(oc, 4, "Save to", of, "outdir")

    # file name: [_vimeo]  + example
    field(oc, 5, "Name suffix", ttk.Entry(oc, textvariable=V["suffix"], width=10,
                                          font=F["base"]), "suffix")
    name_ex = ttk.Label(oc, text="", style="Muted.TLabel")
    name_ex.grid(row=6, column=1, sticky="w", pady=(0, 2))

    def update_output_labels(*_):
        d = V["outdir"].get()
        if d:
            dir_lbl.config(text="📁 " + (os.path.basename(d.rstrip("/")) or d),
                           foreground=C["fg"])
            dir_tip.text = d
            reset_btn.state(["!disabled"])
        else:
            dir_lbl.config(text="Next to source", foreground=C["muted"])
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
    field(vc, 2, "Preset", combo(vc, V["preset"], PRESETS), "preset")
    field(vc, 3, "Resolution", combo(vc, V["res"], list(RESOLUTIONS), 22), "res")
    field(vc, 4, "Frame rate", combo(vc, V["fps"], FPS_CHOICES), "fps")
    chk = ttk.Frame(vc, style="Panel.TFrame")
    chk.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
    for r, (key, txt) in enumerate((("vcap", "Cap at Vimeo's recommended bitrate"),
                                    ("scap", "Never exceed source bitrate"),
                                    ("ten", "10-bit colour"))):
        cb = ttk.Checkbutton(chk, text=txt, variable=V[key])
        cb.grid(row=r, column=0, sticky="w", pady=2)
        Tooltip(cb, TIPS[key])

    ac = card(sets, "Audio")
    ac.grid(row=0, column=2, sticky="nsew")
    ac.columnconfigure(1, weight=1)
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

    ttk.Label(ac, text="ADVANCED", style="H2.TLabel").grid(row=4, column=0, columnspan=2,
                                                           sticky="w", pady=(12, 6))
    field(ac, 5, "Deinterlace", combo(ac, V["deint"], ["Auto", "On", "Off"]), "deint")
    gf = ttk.Frame(ac, style="Panel.TFrame")
    ttk.Spinbox(gf, from_=0.5, to=10, increment=0.5, textvariable=V["gop"], width=5,
                font=F["base"], justify="right").pack(side="left")
    ttk.Label(gf, text="s", style="Panel.TLabel").pack(side="left", padx=(6, 0))
    field(ac, 6, "Keyframe every", gf, "gop")
    field(ac, 7, "Extra ffmpeg args", ttk.Entry(ac, textvariable=V["extra"], width=12,
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
        iid = tree.insert("", "end", text="  " + os.path.basename(p),
                          values=("…", "…", fmt_size(os.path.getsize(p)), "analysing…", ""))
        items[iid] = dict(path=p, info=None, big=None, small=None, result="", error="")
        order.append(iid)
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
            tree.set(iid, "est", est)
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
            thumb_lbl.config(image=blank)
            pname.config(text="No video selected")
            pmeta.config(text="")
            pplan.config(text="")
            return
        it = items[iid]
        thumb_lbl.config(image=it["big"] or blank)
        pname.config(text=os.path.basename(it["path"]))
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
        pmeta.config(text=(f"{info.vcodec.upper()} · {dw}×{dh} · {info.fps:.3f} fps"
                           f"{' (VFR)' if info.vfr else ''}{' · interlaced' if info.interlaced else ''}"
                           f"{' · HDR' if info.hdr else ''}\n"
                           f"{fmt_dur(info.duration)} · {fmt_size(info.size_bytes)} · "
                           f"{info.video_kbps/1000:.1f} Mbps · {a}"))
        p, err = plan_for(iid)
        if err:
            pplan.config(text="⚠ " + err, foreground=C["warn"])
            return
        txt = (f"→  {p.out_w}×{p.out_h} · {p.out_fps:.3f} fps\n"
               f"→  video {p.video_kbps/1000:.2f} Mbps · audio {p.audio_kbps} kbps "
               f"{'5.1' if p.audio_ch == 6 else 'stereo'}\n"
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
                                 "Pick another codec or install ffmpeg via Homebrew.")
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

    def install_ffmpeg():
        brew = find_tool("brew")
        if not brew:
            messagebox.showerror(
                "Homebrew missing",
                "ffmpeg is installed via Homebrew, which isn't installed.\n\n"
                "Launch 'Common Shrew.command' by double-click instead – it offers to "
                "install Homebrew and ffmpeg.")
            return
        start_btn.config(state="disabled")
        if not state["log_open"]:
            toggle_log()
        msgs.put(("log", "Installing ffmpeg via Homebrew … (this can take a few minutes)"))
        status.config(text="Installing ffmpeg…")

        def work():
            global FFMPEG, FFPROBE
            env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
            p = subprocess.Popen([brew, "install", "ffmpeg"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, env=env)
            for line in p.stdout:
                msgs.put(("log", "  " + line.rstrip()))
            p.wait()
            FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")
            ok = p.returncode == 0 and FFMPEG and FFPROBE
            msgs.put(("log", "✓ ffmpeg installed." if ok else "✗ Installation failed."))
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
                        tree.set(m[1], "result", m[2])
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
                        tree.set(iid, "est", "—")
                        tree.set(iid, "result", "✗ unreadable")
                        tree.item(iid, tags=("err",))
                        show_preview()
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
            "ffmpeg missing", "ffmpeg/ffprobe were not found.\n\nInstall now via Homebrew?")
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
    ap.add_argument("--outdir", default="")
    ap.add_argument("--dry-run", action="store_true", help="only show plan + command")
    a = ap.parse_args(argv)
    res = "Original" if a.res == "Original" else next(
        k for k, v in RESOLUTIONS.items() if v and str(v[1]) == a.res)
    s = Settings(target_gb=a.size, safety_pct=a.safety, codec=a.codec, preset=a.preset,
                 resolution=res, fps=a.fps, audio_kbps=a.audio, audio_channels=a.channels,
                 vimeo_cap=not a.no_vimeo_cap, source_cap=not a.no_source_cap,
                 ten_bit=a.ten, deinterlace=a.deinterlace, out_dir=a.outdir,
                 audio_copy=a.copy_audio)
    stop = threading.Event()
    for f in a.files:
        if a.dry_run:
            info = probe(f)
            plan = make_plan(info, s)
            print(f"{f}: {plan.out_w}x{plan.out_h} @ {plan.out_fps:.3f}, "
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
