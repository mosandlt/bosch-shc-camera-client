"""Pure Mini-NVR helpers: ffmpeg argv construction and pre-roll ring cache mechanics.

Extracted from the HA integration's `recorder.py` (a much larger module that
also owns per-camera subprocess lifecycle, coordinator locks/caches, and
crash-respawn bookkeeping — none of that lives here). Everything in this
module takes only plain values (strings, ints, paths, lists) and returns
plain values, with no HA-integration coupling — filesystem I/O and one
`ffprobe` subprocess call are the only side effects, matching the same style
already used by `tls_proxy.py`'s network calls in this library.
"""

from __future__ import annotations

import asyncio
import os
import re

DEFAULT_SEGMENT_SECONDS = 300  # 5 minutes, wall-aligned
PREROLL_SEGMENT_SECONDS = 10  # short segments for fine-grained pre-roll
PREROLL_MIN_SIZE_BYTES = 1024  # discard sub-1 KB corrupt segments
TIMEOUT_SEGMENT_PROBE = 5.0
TIMEOUT_KILL_WAIT = 2.0


def apply_quality(rtsp_url: str, quality: str) -> str:
    """Return a copy of rtsp_url with inst= adjusted for the requested quality.

    "low"  -> inst=4 (~1.9 Mbps, LOCAL only -- REMOTE rejects inst=4)
    "auto" -> inst=1 (~30 Mbps, unchanged)
    If the URL has no inst= parameter and quality is "low", append &inst=4.
    """
    if quality != "low":
        return rtsp_url
    if "inst=" in rtsp_url:
        return re.sub(r"inst=\d+", "inst=4", rtsp_url)
    sep = "&" if "?" in rtsp_url else "?"
    return rtsp_url + sep + "inst=4"


def build_ffmpeg_args(
    rtsp_url: str,
    segment_pattern: str,
    *,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    quality: str = "auto",
) -> list[str]:
    """Return the exact ffmpeg argv used to record the continuous stream.

    Pattern is fed to ``-f segment`` via ``-strftime 1`` -- ffmpeg substitutes
    ``%Y/%m/%d/%H/%M`` from the wall clock and creates parent directories
    implicitly via ``-segment_format mp4`` + ``-strftime_mkdir 1``.
    """
    effective_url = apply_quality(rtsp_url, quality)
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        # Force TCP -- RTP-over-UDP through the loopback proxy is fragile and
        # the TLS proxy already rewrites SETUP to TCP-interleaved anyway.
        "-rtsp_transport",
        "tcp",
        # Bosch cameras can take longer than ffmpeg's 5s/5MB defaults to
        # deliver a decodable SPS/PPS when a second concurrent RTSP session
        # is opened against the same camera (e.g. pre-roll ring alongside
        # the live view) -- without this, ffmpeg exits rc=234 "Could not
        # find codec parameters ... unspecified size" before ever writing
        # a frame (GitHub #64).
        "-analyzeduration",
        "10M",
        "-probesize",
        "10M",
        # -reconnect_* are HTTP-only options and crash ffmpeg with rc=8 on
        # rtsp:// inputs. The integration's own watcher handles respawn on
        # TLS-proxy renewal gaps instead.
        "-i",
        effective_url,
        "-map",
        "0",  # include all streams (video + AAC audio)
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_format",
        "mp4",
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-strftime_mkdir",
        "1",
        "-movflags",
        "+faststart",
        segment_pattern,
    ]


def build_preroll_ffmpeg_args(
    rtsp_url: str,
    pattern: str,
    *,
    segment_seconds: int = PREROLL_SEGMENT_SECONDS,
    quality: str = "auto",
) -> list[str]:
    """ffmpeg args for short segments to a pre-roll ring cache directory.

    No -segment_atclocktime, no -strftime_mkdir (ring dir is pre-created).
    No -reconnect* -- those are HTTP-only and crash ffmpeg rc=8 on rtsp://
    inputs. `quality` mirrors build_ffmpeg_args() -- the ring previously
    always requested the full inst=1 stream regardless of the user's
    nvr_quality option (GitHub #64 follow-up).
    """
    effective_url = apply_quality(rtsp_url, quality)
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        # See build_ffmpeg_args() -- same rc=234 codec-probe failure hits the
        # pre-roll ring's own concurrent RTSP session (GitHub #64).
        "-analyzeduration",
        "10M",
        "-probesize",
        "10M",
        "-i",
        effective_url,
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_format",
        "mp4",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-movflags",
        "+faststart",
        pattern,
    ]


def create_motion_clip_args(output_path: str) -> list[str]:
    """Return ffmpeg argv to concat a pre-built concat-list file into one MP4 clip.

    ``output_path + ".concat.txt"`` is the concat-demuxer list file; building
    that file's contents is the caller's responsibility (it lists the actual
    segment paths, which is orchestration, not pure argv construction).
    """
    concat_file = output_path + ".concat.txt"
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]


def list_preroll_segments(cam_dir: str) -> list[tuple[str, float]]:
    """Return [(path, mtime)] sorted oldest-first for one camera's cache dir."""
    out: list[tuple[str, float]] = []
    if not os.path.isdir(cam_dir):
        return out
    try:
        names = os.listdir(cam_dir)
    except OSError:
        return out
    for name in names:
        full = os.path.join(cam_dir, name)
        if not os.path.isfile(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        if st.st_size < PREROLL_MIN_SIZE_BYTES:
            continue
        out.append((full, st.st_mtime))
    out.sort(key=lambda x: x[1])
    return out


def newest_preroll_path(cam_dir: str) -> str | None:
    """Return the currently-newest pre-roll segment path, or None if empty."""
    segs = list_preroll_segments(cam_dir)
    return segs[-1][0] if segs else None


def prune_preroll_cache(cam_dir: str, max_segments: int) -> int:
    """Delete oldest segments keeping max_segments newest. Returns count deleted."""
    segs = list_preroll_segments(cam_dir)
    to_delete = segs[: max(0, len(segs) - max_segments)]
    deleted = 0
    for path, _ in to_delete:
        try:
            os.unlink(path)
            deleted += 1
        except OSError:
            pass
    return deleted


def prune_and_count(cam_dir: str, max_segments: int) -> int:
    """Prune then return remaining segment count."""
    prune_preroll_cache(cam_dir, max_segments)
    return len(list_preroll_segments(cam_dir))


async def newest_segment_is_finalized(
    path: str,
    *,
    timeout_probe: float = TIMEOUT_SEGMENT_PROBE,
    timeout_kill_wait: float = TIMEOUT_KILL_WAIT,
) -> bool:
    """Return True if `path` already has a readable duration (moov atom
    finalized), i.e. ffmpeg has already rotated past it rather than still
    writing it.

    Proves finalization the same way ``-movflags +faststart`` itself
    requires: only a container whose moov atom is present at the front can
    be parsed by ``ffprobe`` -- a still-open segment has no moov atom yet and
    ffprobe fails. False (probe failure, timeout, ffprobe missing) is the
    safe default for callers that fall back to a drop-newest heuristic.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return False
    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_probe
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_kill_wait)
        except TimeoutError:
            pass
        return False
    if proc.returncode != 0:
        return False
    try:
        return float(stdout_bytes.strip()) > 0
    except ValueError:
        return False
