"""Shared test helpers and fixtures for Tapo exporter tests."""

import time
from types import SimpleNamespace


class FakeDevice:  # pylint: disable=too-many-instance-attributes
    """Fake Tapo plug device used by unit tests."""

    def __init__(self, host: str, alias: str, features: dict[str, SimpleNamespace]) -> None:
        """Create a fake Tapo device for unit tests."""
        self.host = host
        self.alias = alias
        self.model = "P100"
        self.device_type = SimpleNamespace(value="plug")
        self.device_info = SimpleNamespace(firmware_version="1.0.0", hardware_version="1.0")
        self.features = features
        self._last_update_time: float | None = None
        self.update_calls = 0

    async def update(self) -> None:
        """Record a fake update call and timestamp."""
        self.update_calls += 1
        self._last_update_time = time.monotonic()

    async def disconnect(self) -> None:
        """Stub disconnect for fake devices."""
        return None


def make_features() -> dict[str, SimpleNamespace]:
    """Create a default set of fake device feature values for unit tests."""
    return {
        "current_consumption": SimpleNamespace(value=5.0),
        "voltage": SimpleNamespace(value=230.0),
        "current": SimpleNamespace(value=0.2),
        "consumption_today": SimpleNamespace(value=120.0),
        "consumption_this_month": SimpleNamespace(value=4500.0),
        "rssi": SimpleNamespace(value=-50.0),
    }
