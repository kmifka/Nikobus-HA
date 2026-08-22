"""Nikobus Connection Manager.

Owns the transport (serial or TCP) to the Nikobus PC-Link, and knows whether
that transport is alive.

Why the connection state and the reconnect primitive live here
-------------------------------------------------------------
On 21.08.2026 the host was rebooted and the USB enumeration order changed: the
FTDI adapter this installation hangs on had been ``/dev/ttyUSB1`` before the
reboot and was ``/dev/ttyUSB0`` afterwards. Home Assistant still reported the
integration as "loaded" and all 26 cover entities stayed available, but every
single drive command died with::

    custom_components.nikobus.exceptions.NikobusSendError:
        Writer is not available for sending commands.
    nkbcommand.py, process_commands() -> nkbconnect.py send()

for two hours. Nobody noticed until somebody physically stood in front of a
blind that would not move. The integration knew the whole time and only wrote
it to the log — and it wrote that same line for every repeat of every command,
which is exactly the kind of noise that makes a log unreadable.

Two things follow, and both are implemented here:

1. ``is_connected`` — a single, cheap truth about the transport, so the
   coordinator, the connection sensor and the cover travel calculator can all
   ask the same question instead of guessing.
2. ``reconnect_with_backoff()`` — close, reopen, redo the handshake, retry
   forever with exponential capped backoff, and stay quiet about it.

Relationship to upstream
------------------------
Upstream fdebrus/Nikobus-HA 3.x moved the whole transport into the external
``nikobus-connect`` library and calls
``connection.reconnect_with_backoff(initial_delay=, max_delay=, on_attempt=)``
from the coordinator (release 3.8.3, "transport reconnection delegated to the
library"). This fork still owns its transport, so the primitive is implemented
here — but deliberately with upstream's *name and signature*, so that the port
described in PORT_INVENTORY.md only has to delete this method, not rewrite its
caller.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Callable, Literal, Optional

import serial_asyncio_fast as serial_asyncio

from .const import (
    BAUD_RATE,
    COMMANDS_HANDSHAKE,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
)
from .exceptions import (
    NikobusConnectionError,
    NikobusReadError,
    NikobusSendError,
)

_LOGGER = logging.getLogger(__name__)
_SERIAL_DEVICE_RE = re.compile(r"^(/dev/tty(USB|S)\d+|/dev/serial/by-id/.+)$")


class NikobusConnect:
    """Manages connection to a Nikobus system via IP or Serial."""

    def __init__(self, connection_string: str) -> None:
        """Initialize the connection handler with the given connection string."""
        self._connection_string = connection_string
        self._connection_type: Literal["IP", "Serial", "Unknown"] = (
            self._validate_connection_string()
        )
        self._nikobus_reader: Optional[asyncio.StreamReader] = None
        self._nikobus_writer: Optional[asyncio.StreamWriter] = None
        #: True only between a completed handshake and the next close.
        self._is_connected: bool = False
        #: Whether the current outage has already been reported at WARNING.
        #: See ``_log_outage`` for why this flag exists at all.
        self._outage_logged: bool = False

    # -------------------------
    # Public API
    # -------------------------
    @property
    def is_connected(self) -> bool:
        """Return True while the transport is open and handshaken.

        Read by the coordinator (``connection_status``), by the connection
        sensor, and — this is the part upstream does not have — by the cover
        platform before it starts a travel calculator. See cover.py for why
        that matters.
        """
        return self._is_connected

    @property
    def connection_string(self) -> str:
        """Return the configured connection string (device path or host:port)."""
        return self._connection_string

    async def connect(self) -> None:
        """Connect to the Nikobus system using the connection string and perform handshake."""
        if self._connection_type == "IP":
            await self._connect_ip()
        elif self._connection_type == "Serial":
            await self._connect_serial()
        else:
            msg = f"Invalid connection string: {self._connection_string}"
            _LOGGER.error(msg)
            raise NikobusConnectionError(msg)

        # Small settle right after transport is up
        await asyncio.sleep(0.10)

        if not await self._perform_handshake():
            msg = "Handshake failed"
            self._log_outage("Nikobus handshake failed on %s", self._connection_string)
            # Deliberately _safe_close() and not disconnect(): disconnect()
            # logs at INFO, and a reconnect loop running for hours would emit
            # that line once per attempt for no benefit.
            await self._safe_close()
            raise NikobusConnectionError(msg)

        self._is_connected = True
        self._outage_logged = False
        _LOGGER.info("Nikobus handshake successful.")

    async def ping(self) -> None:
        """Open the port briefly and close it again – used to ‘wake’ the PC-Link."""
        await self.connect()
        await self.disconnect()

    async def read(self, timeout: Optional[float] = 35.0) -> bytes:
        """Read one CR-terminated frame from the Nikobus system."""
        if not self._nikobus_reader:
            msg = "Reader is not available for reading data."
            self._log_outage(msg)
            raise NikobusReadError(msg)

        try:
            if timeout is None:
                data = await self._nikobus_reader.readuntil(b"\r")
            else:
                data = await asyncio.wait_for(
                    self._nikobus_reader.readuntil(b"\r"), timeout=timeout
                )
            return data
        except asyncio.TimeoutError as err:
            await self._safe_close()
            raise NikobusReadError(f"Read timeout after {timeout}s") from err
        except Exception as err:
            await self._safe_close()
            raise NikobusReadError(f"Failed to read data: {err}") from err

    async def send(self, command: str, timeout: Optional[float] = 3.0) -> None:
        """Send a CR-terminated command to the Nikobus system."""
        if not self._nikobus_writer:
            # This is the exact line that filled the log for two hours on
            # 21.08.2026 — once per repeat of every cover command. It stays a
            # hard error for the caller, but only the first one is loud.
            msg = "Writer is not available for sending commands."
            self._log_outage(msg)
            raise NikobusSendError(msg)

        try:
            self._nikobus_writer.write(command.encode() + b"\r")
            if timeout is None:
                await self._nikobus_writer.drain()
            else:
                await asyncio.wait_for(self._nikobus_writer.drain(), timeout=timeout)
        except asyncio.TimeoutError as err:
            await self._safe_close()
            raise NikobusSendError(
                f"Timeout while sending command '{command}'"
            ) from err
        except Exception as err:
            await self._safe_close()
            raise NikobusSendError(
                f"Failed to send command '{command}': {err}"
            ) from err

    async def disconnect(self) -> None:
        """Disconnect the connection to the Nikobus system."""
        await self._safe_close()
        _LOGGER.info("Nikobus connection disconnected.")

    async def reconnect_with_backoff(
        self,
        initial_delay: float = RECONNECT_DELAY_INITIAL,
        max_delay: float = RECONNECT_DELAY_MAX,
        on_attempt: Optional[Callable[[int, float], None]] = None,
    ) -> int:
        """Rebuild the transport, retrying forever with capped exponential backoff.

        Returns the number of attempts the reconnect took. Only ever returns on
        success; the caller stops it by cancelling the task.

        Signature and semantics are upstream 3.x's
        ``nikobus_connection.reconnect_with_backoff()``. ``on_attempt(attempt,
        delay)`` is invoked *before* each attempt so the coordinator can bump
        its counter and push a state update to the connection sensor.

        Retrying forever is the point, not an oversight. On 21.08.2026 the
        device node the config entry pointed at simply did not exist any more
        (``/dev/ttyUSB1`` after the adapters were re-enumerated). Giving up on
        ``FileNotFoundError`` would have made the integration permanently dead
        until somebody restarted Home Assistant; retrying means it heals by
        itself the moment the cable is moved back, the adapter re-enumerates,
        or a udev rule creates the node again. ``_connect_serial`` already maps
        those OS errors onto ``NikobusConnectionError``, which is caught here.

        Backoff is 5, 10, 20, 40, 60, 60, ... seconds. The cap keeps an
        unattended outage at 60 open attempts per hour — cheap enough to keep
        running for days — while still bringing the house back within a minute
        of the cause being fixed.
        """
        attempt = 0
        delay = float(initial_delay)

        while True:
            attempt += 1
            if on_attempt is not None:
                try:
                    on_attempt(attempt, delay)
                except Exception:  # pragma: no cover - a callback must not stop us
                    _LOGGER.debug("Reconnect attempt callback failed", exc_info=True)

            # Always start from a closed transport. A half-open serial FD from
            # the previous life would otherwise survive into the new one.
            await self._safe_close()

            try:
                # connect() re-runs COMMANDS_HANDSHAKE via _perform_handshake();
                # a PC-Link that was power-cycled needs it as much as a fresh
                # start does, so the handshake is not optional on reconnect.
                await self.connect()
            except asyncio.CancelledError:
                raise
            except (NikobusConnectionError, OSError) as err:
                self._log_outage(
                    "Nikobus reconnect attempt %d failed: %s — retrying in %.0fs",
                    attempt,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, float(max_delay))
                continue

            return attempt

    # -------------------------
    # Internals
    # -------------------------
    def _log_outage(self, message: str, *args: object) -> None:
        """Log the first failure of an outage loudly, every later one quietly.

        A reconnect every 60 s over a long weekend is ~4300 attempts. At
        WARNING that buries every other message in the Home Assistant log and
        trains the reader to ignore the integration entirely — which is a good
        part of why the two-hour outage on 21.08.2026 went unnoticed even
        though it *was* in the log. So: the transition working -> broken is a
        WARNING, everything while it stays broken is DEBUG, and the transition
        back is an INFO logged by the coordinator.

        The flag is cleared in ``connect()`` on success, so the next, unrelated
        outage is loud again.
        """
        if self._outage_logged:
            _LOGGER.debug(message, *args)
            return
        self._outage_logged = True
        _LOGGER.warning(message, *args)

    async def _connect_ip(self) -> None:
        """Establish an IP connection (precreate + connect the socket correctly)."""
        sock: Optional[socket.socket] = None
        try:
            host, port_str = self._connection_string.split(":", 1)
            port = int(port_str)

            # Precreate socket to apply options, then explicitly connect it
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                # Flush small telegrams immediately
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            try:
                # Keepalives to detect half-open sessions
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

            loop = asyncio.get_running_loop()
            await loop.sock_connect(sock, (host, port))  # <-- crucial: actually connect

            reader, writer = await asyncio.open_connection(sock=sock)
            self._nikobus_reader = reader
            self._nikobus_writer = writer

            _LOGGER.info("Connected to bridge %s:%d", host, port)
        except (OSError, ValueError) as err:
            if sock is not None:
                sock.close()
            await self._safe_close()
            msg = f"Failed to connect to bridge {self._connection_string} - {err}"
            self._log_outage(msg)
            raise NikobusConnectionError(msg) from err

    async def _connect_serial(self) -> None:
        """Establish a serial connection to the Nikobus system."""
        try:
            reader, writer = await serial_asyncio.open_serial_connection(
                url=self._connection_string, baudrate=BAUD_RATE
            )
            self._nikobus_reader = reader
            self._nikobus_writer = writer
            _LOGGER.info("Connected to serial port %s", self._connection_string)
        except (OSError, serial_asyncio.SerialException) as err:
            # FileNotFoundError lands here: it is an OSError, and it is what
            # /dev/ttyUSB1 raised for two hours on 21.08.2026 after the node
            # had moved to /dev/ttyUSB0. Mapping it onto NikobusConnectionError
            # (rather than letting it escape) is what lets
            # reconnect_with_backoff keep trying instead of dying.
            await self._safe_close()
            msg = f"Failed to connect to serial port {self._connection_string} - {err}"
            self._log_outage(msg)
            raise NikobusConnectionError(msg) from err

    def _validate_connection_string(self) -> Literal["IP", "Serial", "Unknown"]:
        """Validate the connection string to determine the type (IP or Serial)."""
        parts = self._connection_string.split(":", 1)
        ip_candidate = parts[0]
        try:
            ipaddress.ip_address(ip_candidate)
            return "IP"
        except ValueError:
            # Common serial device patterns
            if _SERIAL_DEVICE_RE.match(self._connection_string):
                return "Serial"
        return "Unknown"

    async def _perform_handshake(self) -> bool:
        """Perform a handshake with the Nikobus system to verify the connection.

        Uses COMMANDS_HANDSHAKE exactly as provided by your integration.
        """
        for command in COMMANDS_HANDSHAKE:
            _LOGGER.debug("Handshake: %s", command)
            if not await self._send_with_retry(command):
                return False
            # tiny pacing avoids packet coalescing quirks
            await asyncio.sleep(0.05)
        return True

    async def _send_with_retry(self, command: str) -> bool:
        """Send a command once; return True on success, False on failure."""
        try:
            await self.send(command)
            return True
        except NikobusSendError as err:
            # Routed through _log_outage rather than _LOGGER.error: the
            # handshake is re-run on every reconnect attempt, so an ERROR here
            # would be 8 lines per attempt for the whole duration of an outage.
            self._log_outage("Failed to send command: %s", err)
            return False
        except (asyncio.TimeoutError, OSError) as err:
            self._log_outage("Error during send command: %s", err)
            return False
        except Exception as err:
            _LOGGER.exception("Unhandled exception during send command: %s", err)
            return False

    async def _safe_close(self) -> None:
        """Close streams safely (idempotent)."""
        writer = self._nikobus_writer
        self._nikobus_writer = None
        self._nikobus_reader = None
        # Clear the flag before awaiting anything: a handshake send() racing
        # with this close must see "not connected" rather than a stale True.
        self._is_connected = False

        if writer is None:
            return

        try:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
            pass
