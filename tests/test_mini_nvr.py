"""Tests for mini_nvr.py — pure ffmpeg argv construction and pre-roll ring
cache mechanics, extracted byte-for-byte from the source integration's
recorder.py."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bosch_shc_camera_client.mini_nvr import (
    apply_quality,
    build_ffmpeg_args,
    build_preroll_ffmpeg_args,
    create_motion_clip_args,
    list_preroll_segments,
    newest_preroll_path,
    newest_segment_is_finalized,
    prune_and_count,
    prune_preroll_cache,
)


class TestApplyQuality:
    def test_auto_returns_unchanged(self):
        assert apply_quality("rtsp://x?inst=1", "auto") == "rtsp://x?inst=1"

    def test_low_replaces_existing_inst(self):
        assert apply_quality("rtsp://x?inst=1", "low") == "rtsp://x?inst=4"

    def test_low_appends_inst_with_question_mark(self):
        assert apply_quality("rtsp://x", "low") == "rtsp://x?inst=4"

    def test_low_appends_inst_with_ampersand_when_query_present(self):
        assert apply_quality("rtsp://x?foo=1", "low") == "rtsp://x?foo=1&inst=4"


class TestBuildFfmpegArgs:
    def test_shape_and_defaults(self):
        args = build_ffmpeg_args("rtsp://cam", "/out/%H%M.mp4")
        assert args[0] == "ffmpeg"
        assert "-i" in args
        assert args[args.index("-i") + 1] == "rtsp://cam"
        assert args[-1] == "/out/%H%M.mp4"
        assert "-segment_time" in args
        assert args[args.index("-segment_time") + 1] == "300"

    def test_custom_segment_seconds(self):
        args = build_ffmpeg_args("rtsp://cam", "/out/%H.mp4", segment_seconds=60)
        assert args[args.index("-segment_time") + 1] == "60"

    def test_low_quality_rewrites_url(self):
        args = build_ffmpeg_args("rtsp://cam?inst=1", "/out/%H.mp4", quality="low")
        assert args[args.index("-i") + 1] == "rtsp://cam?inst=4"

    def test_no_reconnect_flags(self):
        args = build_ffmpeg_args("rtsp://cam", "/out/%H.mp4")
        assert not any(a.startswith("-reconnect") for a in args)


class TestBuildPrerollFfmpegArgs:
    def test_shape(self):
        args = build_preroll_ffmpeg_args("rtsp://cam", "/ring/%H%M%S.mp4")
        assert args[0] == "ffmpeg"
        assert args[args.index("-segment_time") + 1] == "10"
        assert args[-1] == "/ring/%H%M%S.mp4"
        assert "-segment_atclocktime" not in args
        assert "-strftime_mkdir" not in args

    def test_custom_segment_seconds(self):
        args = build_preroll_ffmpeg_args(
            "rtsp://cam", "/ring/%H.mp4", segment_seconds=5
        )
        assert args[args.index("-segment_time") + 1] == "5"


class TestCreateMotionClipArgs:
    def test_shape(self):
        args = create_motion_clip_args("/out/clip.mp4")
        assert args[0] == "ffmpeg"
        assert args[args.index("-i") + 1] == "/out/clip.mp4.concat.txt"
        assert args[-1] == "/out/clip.mp4"
        assert "-y" in args


class TestListPrerollSegments:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert list_preroll_segments(str(tmp_path / "nope")) == []

    def test_sorts_oldest_first_and_skips_tiny_files(self, tmp_path):
        d = tmp_path / "cam"
        d.mkdir()
        big1 = d / "a.mp4"
        big1.write_bytes(b"x" * 2000)
        big2 = d / "b.mp4"
        big2.write_bytes(b"x" * 2000)
        tiny = d / "c.mp4"
        tiny.write_bytes(b"x")
        os.utime(big1, (1000, 1000))
        os.utime(big2, (2000, 2000))
        segs = list_preroll_segments(str(d))
        assert [os.path.basename(p) for p, _ in segs] == ["a.mp4", "b.mp4"]

    def test_skips_subdirectories(self, tmp_path):
        d = tmp_path / "cam"
        d.mkdir()
        (d / "sub").mkdir()
        assert list_preroll_segments(str(d)) == []

    def test_listdir_oserror_returns_empty(self, tmp_path):
        d = tmp_path / "cam"
        d.mkdir()
        with patch("os.listdir", side_effect=OSError):
            assert list_preroll_segments(str(d)) == []

    def test_stat_oserror_skips_file(self, tmp_path):
        """Simulate a file vanishing between the isfile() check and the
        explicit os.stat() call (real race window in production). Deletes
        the file for real inside a mocked isfile() so the later os.stat()
        raises a genuine FileNotFoundError — mocking os.stat() itself would
        also intercept coverage.py's own internal tracing calls and produce
        false "line not hit" results despite the branch genuinely running.
        """
        d = tmp_path / "cam"
        d.mkdir()
        p = d / "a.mp4"
        p.write_bytes(b"x" * 2000)

        def _isfile_then_delete(path):
            if os.path.exists(path):
                os.unlink(path)
            return True

        with patch("os.path.isfile", side_effect=_isfile_then_delete):
            assert list_preroll_segments(str(d)) == []


class TestNewestPrerollPath:
    def test_empty_dir_returns_none(self, tmp_path):
        assert newest_preroll_path(str(tmp_path)) is None

    def test_returns_newest(self, tmp_path):
        a = tmp_path / "a.mp4"
        a.write_bytes(b"x" * 2000)
        b = tmp_path / "b.mp4"
        b.write_bytes(b"x" * 2000)
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        assert newest_preroll_path(str(tmp_path)) == str(b)


class TestPrunePrerollCache:
    def test_deletes_oldest_keeping_max(self, tmp_path):
        paths = []
        for i in range(5):
            p = tmp_path / f"{i}.mp4"
            p.write_bytes(b"x" * 2000)
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)
        deleted = prune_preroll_cache(str(tmp_path), 2)
        assert deleted == 3
        remaining = sorted(os.listdir(tmp_path))
        assert remaining == ["3.mp4", "4.mp4"]

    def test_nothing_to_delete_when_under_max(self, tmp_path):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"x" * 2000)
        assert prune_preroll_cache(str(tmp_path), 10) == 0

    def test_unlink_oserror_is_swallowed(self, tmp_path):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"x" * 2000)
        with patch("os.unlink", side_effect=OSError):
            deleted = prune_preroll_cache(str(tmp_path), 0)
        assert deleted == 0


class TestPruneAndCount:
    def test_returns_remaining_count(self, tmp_path):
        for i in range(3):
            p = tmp_path / f"{i}.mp4"
            p.write_bytes(b"x" * 2000)
            os.utime(p, (1000 + i, 1000 + i))
        assert prune_and_count(str(tmp_path), 1) == 1


class TestNewestSegmentIsFinalized:
    @pytest.mark.asyncio
    async def test_finalized_when_ffprobe_reports_positive_duration(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"5.2\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            assert await newest_segment_is_finalized("/x.mp4") is True

    @pytest.mark.asyncio
    async def test_not_finalized_when_ffprobe_nonzero_exit(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            assert await newest_segment_is_finalized("/x.mp4") is False

    @pytest.mark.asyncio
    async def test_not_finalized_when_duration_unparseable(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"not-a-number", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            assert await newest_segment_is_finalized("/x.mp4") is False

    @pytest.mark.asyncio
    async def test_not_finalized_when_duration_zero(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"0.0\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            assert await newest_segment_is_finalized("/x.mp4") is False

    @pytest.mark.asyncio
    async def test_ffprobe_missing_returns_false(self):
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError),
        ):
            assert await newest_segment_is_finalized("/x.mp4") is False

    @pytest.mark.asyncio
    async def test_ffprobe_oserror_returns_false(self):
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError)):
            assert await newest_segment_is_finalized("/x.mp4") is False

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_returns_false(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await newest_segment_is_finalized(
                "/x.mp4", timeout_probe=0.01, timeout_kill_wait=0.01
            )
        assert result is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_then_kill_raises_process_lookup_error(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await newest_segment_is_finalized(
                "/x.mp4", timeout_probe=0.01, timeout_kill_wait=0.01
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_then_wait_also_times_out(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await newest_segment_is_finalized(
                "/x.mp4", timeout_probe=0.01, timeout_kill_wait=0.01
            )
        assert result is False
