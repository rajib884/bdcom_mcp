"""Shared pytest fixtures for the emulator-backed integration tests."""

from __future__ import annotations

from typing import Callable, Iterator

import pytest

from device_mcp.connection import DeviceConnectionManager

from .switch_emulator import SwitchEmulator


@pytest.fixture(autouse=True)
def _audit_logs_to_tmp(tmp_path, monkeypatch) -> None:
    """Send each test's connection audit logs to a temp dir, not the repo ``logs/``."""
    monkeypatch.setenv("DEVICE_MCP_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def manager() -> Iterator[DeviceConnectionManager]:
    """A real connection manager; every connection it opens is closed at teardown."""
    mgr = DeviceConnectionManager()
    yield mgr
    for info in mgr.list_connections():
        try:
            mgr.disconnect(info["host"], port=info["port"])
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass


@pytest.fixture
def make_switch() -> Iterator[Callable[..., SwitchEmulator]]:
    """Factory that starts a :class:`SwitchEmulator` and stops it after the test.

    Pass the same keyword arguments you would give ``SwitchEmulator`` (``responses``,
    ``dialect``, ``enable_password``, …); the started emulator is returned.
    """
    started: list[SwitchEmulator] = []

    def factory(**kwargs) -> SwitchEmulator:
        sw = SwitchEmulator(**kwargs).start()
        started.append(sw)
        return sw

    yield factory
    for sw in started:
        sw.stop()
