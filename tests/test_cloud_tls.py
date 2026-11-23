"""Cloud requests verify against system roots plus the Bosch private CA."""

import ssl

import aiohttp
import pytest

from bosch_shc_camera_client import cloud, cloud_tls
from bosch_shc_camera_client.client import BoschCameraClient
from bosch_shc_camera_client.cloud_tls import cloud_ssl_context


def test_context_verifies_and_trusts_bosch_ca() -> None:
    ctx = cloud_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    subjects = [c["subject"] for c in ctx.get_ca_certs()]
    assert any(("commonName", "Video CA 2A") in rdn for s in subjects for rdn in s)


def test_context_is_shared() -> None:
    assert cloud_ssl_context() is cloud_ssl_context()
    assert cloud_tls._context is cloud_ssl_context()


async def _token() -> str:
    return "t"


class _Recorder:
    """Fake session that records the kwargs of every request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, *args, **kwargs):
        self.calls.append(kwargs)
        raise aiohttp.ClientError("stop")

    get = put = post = request = _record


@pytest.mark.asyncio
async def test_cloud_requests_pass_context() -> None:
    session = _Recorder()
    await cloud.cloud_put_json(session, "t", "https://x/y", {})  # type: ignore[arg-type]
    client = BoschCameraClient(session, _token)  # type: ignore[arg-type]
    for call in (client.list_cameras,):
        try:
            await call()
        except Exception:
            pass
    assert len(session.calls) == 2
    assert all(c["ssl"] is cloud_ssl_context() for c in session.calls)
