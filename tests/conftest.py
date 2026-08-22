"""Test bootstrap for the Nikobus fork.

Home Assistant is not installable in this environment (no ``homeassistant``
package, no ``voluptuous``, no ``serial_asyncio_fast``, no ``aiofiles``), so
the tests cannot import the integration the way Home Assistant does. Two
things make them run anyway:

1. **Stubbed third-party modules.** Minimal stand-ins for the handful of
   Home Assistant symbols the modules under test actually touch are placed in
   ``sys.modules`` before anything is imported. They are deliberately dumb:
   the point is to exercise *this fork's* logic, not to re-implement Home
   Assistant.

2. **A synthetic ``custom_components.nikobus`` package.** The real
   ``custom_components/nikobus/__init__.py`` pulls in voluptuous and six
   Home Assistant platform modules, and it is also the file that derives the
   YAML cover unique_ids — the one file this work is not allowed to touch. So
   the package is registered in ``sys.modules`` by hand, with ``__path__``
   pointing at the real directory but without executing that ``__init__.py``.
   Submodules (``nkbconnect``, ``coordinator``, ``sensor``, ``cover``,
   ``helpers.travelcalculator``, ...) then import normally, relative imports
   and all, including ``nkblistener``'s absolute
   ``from custom_components.nikobus.exceptions import ...``.

The heavy siblings the coordinator pulls in but that none of these tests
exercise (``nkbAPI``, ``nkbconfig``, ``nkbactuator``, ``discovery``) are
pre-registered as stubs for the same reason.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "nikobus"


# ---------------------------------------------------------------------------
# Third-party stubs
# ---------------------------------------------------------------------------
def _module(name: str, **attrs: object) -> types.ModuleType:
    """Create and register a stub module."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _StubCallable:
    """Callable placeholder that records nothing and returns None."""

    def __call__(self, *args: object, **kwargs: object) -> None:
        return None


def _callback(func):
    """Stand-in for homeassistant.core.callback (identity decorator)."""
    return func


class DeviceInfo(dict):
    """Stand-in for homeassistant's DeviceInfo TypedDict."""


class DeviceEntry:  # pragma: no cover - only needed as an import target
    """Stand-in for homeassistant's DeviceEntry."""


class EntityCategory:
    """Stand-in for homeassistant.const.EntityCategory."""

    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


class SensorDeviceClass:
    """Stand-in for homeassistant.components.sensor.SensorDeviceClass."""

    ENUM = "enum"


class _Entity:
    """Bare minimum of homeassistant.helpers.entity.Entity."""

    hass = None
    entity_id: str | None = None
    _attr_name: str | None = None

    def async_write_ha_state(self) -> None:
        """Record that a state write happened."""
        self.state_writes = getattr(self, "state_writes", 0) + 1

    async def async_added_to_hass(self) -> None:
        return None

    async def async_will_remove_from_hass(self) -> None:
        return None


class SensorEntity(_Entity):
    """Stand-in for homeassistant.components.sensor.SensorEntity."""


class CoverEntity(_Entity):
    """Stand-in for homeassistant.components.cover.CoverEntity."""


class RestoreEntity(_Entity):
    """Stand-in for homeassistant.helpers.restore_state.RestoreEntity."""

    async def async_get_last_state(self):
        return None


class CoverEntityFeature:
    """Stand-in for the cover feature flags (plain ints are enough)."""

    OPEN = 1
    CLOSE = 2
    SET_POSITION = 4
    STOP = 8


class CoverDeviceClass:
    """Stand-in for homeassistant.components.cover.CoverDeviceClass."""

    SHUTTER = "shutter"


class UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


class ConfigEntryNotReady(HomeAssistantError):
    """Stand-in for homeassistant.exceptions.ConfigEntryNotReady."""


class DataUpdateCoordinator:
    """Stand-in for the coordinator base class.

    Only what NikobusDataCoordinator actually uses: the constructor keyword
    arguments, ``async_update_listeners`` (counted, because several tests
    assert the connection sensor is pushed a state change) and the refresh
    entry points.
    """

    def __init__(self, hass, logger, name=None, update_method=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval
        self.data = None
        self.listener_updates = 0
        self.listeners: list = []

    def async_add_listener(self, update_callback, context=None):
        """Register an entity callback and return the unsubscribe function.

        The YAML cover entities use this to be told when availability flips;
        the real DataUpdateCoordinator has the same contract.
        """
        self.listeners.append(update_callback)

        def _unsub() -> None:
            if update_callback in self.listeners:
                self.listeners.remove(update_callback)

        return _unsub

    def async_update_listeners(self) -> None:
        self.listener_updates += 1
        for callback_ in list(self.listeners):
            callback_()

    async def async_refresh(self) -> None:
        if self.update_method is not None:
            self.data = await self.update_method()

    async def async_request_refresh(self) -> None:
        await self.async_refresh()


class CoordinatorEntity(_Entity):
    """Stand-in for homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, _item):
        # CoordinatorEntity[NikobusDataCoordinator] must stay subscriptable.
        return cls

    @property
    def available(self) -> bool:
        return True


class _AnyStub:
    """Accepts any constructor arguments and does nothing.

    Used for the coordinator's collaborators that these tests do not exercise.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


class SerialException(OSError):
    """Stand-in for serial's SerialException (a subclass of OSError, as in pyserial)."""


def _install_stubs() -> None:
    """Register every third-party stub in sys.modules."""
    _module("serial_asyncio_fast", open_serial_connection=_StubCallable(), SerialException=SerialException)
    _module("aiofiles", open=_StubCallable())
    _module("voluptuous")

    ha = _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=object, callback=_callback, ServiceCall=object)
    _module("homeassistant.const", EntityCategory=EntityCategory, PERCENTAGE="%")
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module(
        "homeassistant.exceptions",
        HomeAssistantError=HomeAssistantError,
        ConfigEntryNotReady=ConfigEntryNotReady,
    )
    _module("homeassistant.util", slugify=lambda value: str(value).lower())

    _module("homeassistant.components")
    _module(
        "homeassistant.components.sensor",
        SensorEntity=SensorEntity,
        SensorDeviceClass=SensorDeviceClass,
    )
    _module(
        "homeassistant.components.cover",
        CoverEntity=CoverEntity,
        CoverEntityFeature=CoverEntityFeature,
        CoverDeviceClass=CoverDeviceClass,
        ATTR_POSITION="position",
    )

    _module("homeassistant.helpers")
    _module("homeassistant.helpers.area_registry", async_get=_StubCallable())
    _module("homeassistant.helpers.entity", DeviceInfo=DeviceInfo, Entity=_Entity)
    _module(
        "homeassistant.helpers.device_registry",
        DeviceEntry=DeviceEntry,
        DeviceInfo=DeviceInfo,
        async_get=_StubCallable(),
    )
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.entity_registry", async_get=_StubCallable())
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    _module(
        "homeassistant.helpers.event",
        async_track_time_interval=lambda *a, **k: _StubCallable(),
        async_track_state_change_event=lambda *a, **k: _StubCallable(),
        async_call_later=lambda *a, **k: _StubCallable(),
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
        CoordinatorEntity=CoordinatorEntity,
    )
    ha.helpers = sys.modules["homeassistant.helpers"]


def _install_package() -> None:
    """Register custom_components.nikobus without running its __init__.py."""
    parent = types.ModuleType("custom_components")
    parent.__path__ = [str(REPO_ROOT / "custom_components")]
    sys.modules["custom_components"] = parent

    pkg = types.ModuleType("custom_components.nikobus")
    pkg.__path__ = [str(PKG_DIR)]
    pkg.__package__ = "custom_components.nikobus"
    # Same literal as the real __init__.py; a few submodules read it from there.
    pkg.HUB_IDENTIFIER = "nikobus_hub"
    sys.modules["custom_components.nikobus"] = pkg
    parent.nikobus = pkg

    # Siblings the coordinator imports but no test exercises.
    _module("custom_components.nikobus.nkbAPI", NikobusAPI=_AnyStub)
    _module("custom_components.nikobus.nkbconfig", NikobusConfig=_AnyStub)
    _module("custom_components.nikobus.nkbactuator", NikobusActuator=_AnyStub)
    _module("custom_components.nikobus.discovery", NikobusDiscovery=_AnyStub)


_install_stubs()
_install_package()


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------
class FakeBus:
    """Minimal hass.bus."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_listen(self, _event, _handler):
        return lambda: None

    def async_fire(self, event, data=None):
        self.fired.append((event, data or {}))


class FakeStates:
    """Minimal hass.states."""

    def get(self, _entity_id):
        return None


class FakeHass:
    """Minimal HomeAssistant stand-in."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.bus = FakeBus()
        self.states = FakeStates()
        self.background_tasks: list[asyncio.Task] = []

    @property
    def loop(self):
        return asyncio.get_running_loop()

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.background_tasks.append(task)
        return task

    def async_create_background_task(self, coro, name=None, eager_start=False):
        return self.async_create_task(coro, name=name)


class FakeReader:
    """StreamReader stand-in that yields queued frames, then blocks."""

    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = list(frames or [])

    async def readuntil(self, _separator: bytes = b"\r") -> bytes:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.Event().wait()  # never returns
        raise AssertionError("unreachable")


class FakeWriter:
    """StreamWriter stand-in that records everything written to it."""

    def __init__(self, sink: list[bytes]) -> None:
        self.sink = sink
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sink.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeEntry:
    """ConfigEntry stand-in."""

    def __init__(self, data: dict | None = None) -> None:
        self.entry_id = "test-entry"
        self.data = data or {}
        self.options: dict = {}
        self.runtime_data = None


@pytest.fixture
def hass() -> FakeHass:
    """Return a fresh fake Home Assistant instance."""
    return FakeHass()


@pytest.fixture
def instant_sleep(monkeypatch):
    """Replace asyncio.sleep with a recorder so backoff waits are free.

    Returns the list of requested delays, which is what the backoff tests
    assert on: the sequence has to grow and then stay at the cap.
    """
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return delays
