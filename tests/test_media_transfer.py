"""Tests for media_transfer.py — pure SMB/FTP/local-save export mechanics.

Covers is_safe_bosch_url (SSRF allowlist, dedup of 3 previously-identical
copies in the source integration), sanitize_filename (path-traversal /
unsafe-char stripping), the sync urllib GET helpers (mocked — no network),
and the FTP protocol mechanics (mocked ftplib).
"""

from __future__ import annotations

import ftplib
from unittest.mock import MagicMock, patch

import pytest

from bosch_shc_camera_client.media_transfer import (
    DEFAULT_SAFE_DOMAINS,
    ftp_connect,
    ftp_exists,
    ftp_makedirs,
    is_safe_bosch_url,
    sanitize_filename,
    sync_http_get,
    sync_http_get_chunked,
    sync_http_get_to_file,
)


class TestIsSafeBoschUrl:
    """Pin every branch of is_safe_bosch_url."""

    def test_valid_boschsecurity_url(self):
        assert is_safe_bosch_url("https://residential.cbs.boschsecurity.com/x")

    def test_valid_bosch_com_url(self):
        assert is_safe_bosch_url("https://api.bosch.com/x")

    def test_rejects_http_scheme(self):
        assert not is_safe_bosch_url("http://residential.cbs.boschsecurity.com/x")

    def test_rejects_non_bosch_domain(self):
        assert not is_safe_bosch_url("https://evil.example.com/x")

    def test_rejects_domain_suffix_bypass(self):
        """A hostname merely containing the suffix as a substring must not pass."""
        assert not is_safe_bosch_url("https://notboschsecurity.com/x")

    def test_rejects_no_hostname(self):
        assert not is_safe_bosch_url("https:///path-only")

    def test_rejects_malformed_url_valueerror(self):
        """urlparse raises ValueError on e.g. unmatched IPv6 brackets — fail closed."""
        assert not is_safe_bosch_url("https://[::1")

    def test_custom_safe_domains_param(self):
        assert is_safe_bosch_url(
            "https://x.example.com/y", safe_domains=frozenset({".example.com"})
        )
        assert not is_safe_bosch_url(
            "https://x.example.com/y", safe_domains=DEFAULT_SAFE_DOMAINS
        )


class TestSanitizeFilename:
    """Pin every branch of sanitize_filename."""

    def test_plain_name_unchanged(self):
        assert sanitize_filename("Terrasse") == "Terrasse"

    def test_strips_path_traversal(self):
        assert ".." not in sanitize_filename("../../etc/passwd")

    def test_replaces_unsafe_characters(self):
        result = sanitize_filename("a/b\\c:d")
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result

    def test_truncates_to_64_chars(self):
        result = sanitize_filename("x" * 200)
        assert len(result) == 64

    def test_allows_dash_dot_space_underscore(self):
        assert sanitize_filename("Front Door-Cam_1.jpg") == "Front Door-Cam_1.jpg"


class TestSyncHttpGet:
    def test_returns_status_and_body(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"hello"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, body = sync_http_get("https://x/y", "tok", MagicMock())
        assert status == 200
        assert body == b"hello"


class TestSyncHttpGetToFile:
    def test_success_writes_file(self, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.side_effect = [b"abc", b""]
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        dest = tmp_path / "out.bin"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok = sync_http_get_to_file("https://x/y", "tok", str(dest), MagicMock())
        assert ok is True
        assert dest.read_bytes() == b"abc"

    def test_non_200_returns_false(self, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        dest = tmp_path / "out.bin"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok = sync_http_get_to_file("https://x/y", "tok", str(dest), MagicMock())
        assert ok is False
        assert not dest.exists()


class TestSyncHttpGetChunked:
    def test_returns_urlopen_result(self):
        mock_resp = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = sync_http_get_chunked("https://x/y", "tok", MagicMock())
        assert result is mock_resp
        mock_open.assert_called_once()


class TestFtpConnect:
    def test_connects_logs_in_sets_passive(self):
        mock_ftp = MagicMock()
        with patch("ftplib.FTP", return_value=mock_ftp) as mock_cls:
            result = ftp_connect("nas.local", "user", "pw")
        mock_cls.assert_called_once_with("nas.local", timeout=30)
        mock_ftp.login.assert_called_once_with("user", "pw")
        mock_ftp.set_pasv.assert_called_once_with(True)
        assert result is mock_ftp

    def test_custom_timeout(self):
        mock_ftp = MagicMock()
        with patch("ftplib.FTP", return_value=mock_ftp) as mock_cls:
            ftp_connect("nas.local", "user", "pw", timeout=5)
        mock_cls.assert_called_once_with("nas.local", timeout=5)


class TestFtpExists:
    def test_returns_true_when_size_succeeds(self):
        ftp = MagicMock()
        ftp.size.return_value = 123
        assert ftp_exists(ftp, "/a/b.jpg") is True

    def test_returns_false_on_error_perm(self):
        ftp = MagicMock()
        ftp.size.side_effect = ftplib.error_perm("550 not found")
        assert ftp_exists(ftp, "/a/b.jpg") is False

    def test_returns_false_on_other_exception(self):
        ftp = MagicMock()
        ftp.size.side_effect = OSError("boom")
        assert ftp_exists(ftp, "/a/b.jpg") is False


class TestFtpMakedirs:
    def test_creates_each_path_segment(self):
        ftp = MagicMock()
        ftp_makedirs(ftp, "/a/b/c")
        assert ftp.mkd.call_args_list == [
            (("/a",),),
            (("/a/b",),),
            (("/a/b/c",),),
        ]

    def test_ignores_already_exists_error_perm(self):
        ftp = MagicMock()
        ftp.mkd.side_effect = ftplib.error_perm("550 exists")
        # Must not raise.
        ftp_makedirs(ftp, "/a/b")

    def test_empty_path_no_op(self):
        ftp = MagicMock()
        ftp_makedirs(ftp, "")
        ftp.mkd.assert_not_called()
