import time
from types import SimpleNamespace


class FakeDevice:
    def __init__(self, host: str, alias: str, features: dict[str, SimpleNamespace]) -> None:
        self.host = host
        self.alias = alias
        self.model = "P100"
        self.device_type = SimpleNamespace(value="plug")
        self.device_info = SimpleNamespace(firmware_version="1.0.0", hardware_version="1.0")
        self.features = features
        self._last_update_time = None
        self.update_calls = 0

    async def update(self) -> None:
        self.update_calls += 1
        self._last_update_time = time.monotonic()

    async def disconnect(self) -> None:
        return None


def make_features() -> dict[str, SimpleNamespace]:
    return {
        "current_consumption": SimpleNamespace(value=5.0),
        "voltage": SimpleNamespace(value=230.0),
        "current": SimpleNamespace(value=0.2),
        "consumption_today": SimpleNamespace(value=120.0),
        "consumption_this_month": SimpleNamespace(value=4500.0),
        "rssi": SimpleNamespace(value=-50.0),
    }
