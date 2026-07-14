"""Smoke test: package imports cleanly."""

import bosch_shc_camera_client


def test_import() -> None:
    assert bosch_shc_camera_client.local_rcp
    assert bosch_shc_camera_client.smb
