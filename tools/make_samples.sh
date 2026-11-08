#!/bin/bash
# Generate small synthetic test clips covering the tricky cases.
# Usage: tools/make_samples.sh [outdir]   (default: samples/ – git-ignored)
set -e
OUT="${1:-samples}"
mkdir -p "$OUT"
q() { ffmpeg -v error -y "$@"; }
# 1080p25 + PCM audio
q -f lavfi -i testsrc2=size=1920x1080:rate=25 -f lavfi -i sine=f=440:sample_rate=48000 -t 20 \
  -c:v libx264 -crf 10 -c:a pcm_s16le "$OUT/hd_pcm.mov"
# 720p120, no audio  -> fps reduction + silent track
q -f lavfi -i testsrc2=size=1280x720:rate=120 -t 10 -c:v libx264 -crf 10 "$OUT/hfr_noaudio.mp4"
# 4096x1716 scope + AAC -> aspect-ratio fit (e.g. 1920x804)
q -f lavfi -i testsrc2=size=4096x1716:rate=24 -f lavfi -i sine=f=300 -t 8 \
  -c:v libx264 -crf 14 -c:a aac "$OUT/scope.mov"
# portrait 1080x1920 -> rotated box
q -f lavfi -i mandelbrot=size=1080x1920:rate=30 -t 6 -c:v libx264 -crf 14 "$OUT/portrait.mp4"
# 720p + 24-bit PCM -> "Don't compress audio" saves .mov
q -f lavfi -i testsrc2=size=1280x720:rate=25 -f lavfi -i sine=f=500:sample_rate=48000 -t 6 \
  -c:v libx264 -crf 12 -c:a pcm_s24le -ac 2 "$OUT/pcm24.mov"
echo "Samples written to $OUT/"
