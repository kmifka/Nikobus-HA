"""Nikobus Command Handler."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from .nkbprotocol import make_pc_link_command, calculate_group_number
from .const import (
    COMMAND_EXECUTION_DELAY,
    COMMAND_REPEAT_BURST_DELAY,
    COMMAND_ACK_WAIT_TIMEOUT,
    COMMAND_ANSWER_WAIT_TIMEOUT,
    HEARTBEAT_FUNCTION_CODE,
    MAX_ATTEMPTS,
)
from .exceptions import NikobusError, NikobusSendError, NikobusTimeoutError

_LOGGER = logging.getLogger(__name__)


class NikobusCommandHandler:
    """Handles command processing for Nikobus."""

    def __init__(
        self,
        hass: Any,
        coordinator: Any,
        nikobus_connection: Any,
        nikobus_listener: Any,
        nikobus_module_states: dict[str, bytearray],
    ) -> None:
        """Initialize the command handler."""
        self._coordinator = coordinator
        self._running: bool = False
        self._command_task: asyncio.Task | None = None
        self._command_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._command_completion_handlers: dict[str, Callable[[], Awaitable[None]]] = {}

        self.nikobus_connection = nikobus_connection
        self.nikobus_listener = nikobus_listener
        self.nikobus_module_states = nikobus_module_states

    async def start(self) -> None:
        """Start the command processing loop."""
        self._running = True
        loop = self._coordinator.hass.loop
        self._command_task = loop.create_task(self.process_commands())

    async def stop(self) -> None:
        """Stop the command processing loop."""
        self._running = False
        if self._command_task:
            self._command_task.cancel()
            try:
                await self._command_task
            except asyncio.CancelledError:
                _LOGGER.info("Command processing task was cancelled.")
            self._command_task = None

    def reset(self) -> None:
        """Drop everything queued against the connection that just died.

        Called by the coordinator between a successful reconnect and
        restarting the worker (upstream 3.x calls the identically named
        ``command.reset()`` at the same point).

        Replaying the queue would be worse than dropping it: the entries are
        cover drive commands, and a blind that was told to close two minutes
        ago - while the bus was dead and the user has since given up - must not
        start moving the moment the cable is plugged back in. Waiters on a
        dropped future are released by their own COMMAND_ACK_WAIT_TIMEOUT.
        """
        dropped = 0
        while True:
            try:
                item = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
            future: asyncio.Future | None = item.get("future")
            if future is not None and not future.done():
                # set_exception, not cancel(): a cancelled future makes the
                # waiting asyncio.wait_for raise CancelledError, which would
                # then travel up through the coordinator's refresh as if the
                # refresh task itself had been cancelled. A plain error is what
                # actually happened and what the caller can handle.
                future.set_exception(
                    NikobusSendError(
                        "Connection lost before the command reached the bus."
                    )
                )
            self._command_queue.task_done()
        if dropped:
            _LOGGER.warning(
                "Dropped %d Nikobus command(s) queued against the lost connection.",
                dropped,
            )

    async def clear_command_queue(self) -> None:
        """Clear all pending commands in the queue."""
        while True:
            try:
                self._command_queue.get_nowait()
                self._command_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def process_commands(self) -> None:
        """Process commands from the queue."""
        _LOGGER.info("Nikobus Command Processing starting.")
        while self._running:
            try:
                command_item = await self._command_queue.get()
                command = command_item["command"]
                batch = command_item.get("batch")
                address = command_item.get("address")
                future: asyncio.Future | None = command_item.get("future")
                completion_handler: Callable[[], Awaitable[None]] | None = (
                    command_item.get("completion_handler")
                )

                _LOGGER.debug("Dequeued command: %s", command)
                _LOGGER.debug(
                    "Processing command: %s with address: %s", command, address
                )

                try:
                    if batch:
                        for idx, batch_command in enumerate(batch):
                            await self.send_command(batch_command)
                            if idx < len(batch) - 1:
                                await asyncio.sleep(COMMAND_REPEAT_BURST_DELAY)
                        if completion_handler and callable(completion_handler):
                            _LOGGER.debug("Calling completion handler for command batch")
                            await completion_handler()
                    elif not address:
                        await self.send_command(command)
                        if completion_handler and callable(completion_handler):
                            _LOGGER.debug("Calling completion handler for command without address")
                            await completion_handler()
                    else:
                        result = await self.send_command_get_answer(
                            command,
                            address,
                            raw_answer=bool(command_item.get("raw_answer")),
                            max_attempts=command_item.get("max_attempts"),
                        )
                        if future and not future.done():
                            future.set_result(result)
                        if completion_handler and callable(completion_handler):
                            _LOGGER.debug("Calling completion handler")
                            await completion_handler()
                except Exception as err:
                    _LOGGER.error(
                        "Error processing command %s: %s", command, err, exc_info=True
                    )
                    if future and not future.done():
                        future.set_exception(err)
                finally:
                    self._command_queue.task_done()

                await asyncio.sleep(COMMAND_EXECUTION_DELAY)
            except Exception as err:
                _LOGGER.error(
                    "Error in command processing loop: %s", err, exc_info=True
                )

    async def get_output_state(self, address: str, group: int) -> str:
        """Get the output state of a module."""
        _LOGGER.debug("Getting output state - Address: %s, Group: %s", address, group)
        command_code = 0x12 if int(group) == 1 else 0x17
        command = make_pc_link_command(command_code, address)
        future = self._coordinator.hass.loop.create_future()
        await self.queue_command(command, address, future=future)
        return await asyncio.wait_for(future, timeout=COMMAND_ACK_WAIT_TIMEOUT)

    async def get_system_clock(self, address: str) -> str:
        """Ask the installation for its running clock and return the raw frame.

        This is the liveness ping. It deliberately looks exactly like
        ``get_output_state`` above: build the PC-Link command, create a future,
        hand both to ``queue_command`` and wait. It does NOT write to
        ``nikobus_connection`` itself.

        That is a requirement, not a preference. Every other command in this
        integration is paced by the single worker in ``process_commands``, which
        sleeps ``COMMAND_EXECUTION_DELAY`` (0.7 s) between items. A ping that
        wrote straight to the transport would slip past that pacing and put two
        telegrams on the bus at once - on an installation that has no output
        feedback at all (see const.HEARTBEAT_FUNCTION_CODE for the 22.08.2026
        measurements), a collision is not something the integration would ever
        find out about; it would simply lose a shutter command.

        The answer is returned raw rather than run through
        ``_parse_state_from_message``: that helper slices a fixed 12-hex-digit
        module state out of the frame, and the clock frame has a different
        layout. Decoding it is the heartbeat's job (see nkbheartbeat.py).
        """
        command = make_pc_link_command(HEARTBEAT_FUNCTION_CODE, address)
        _LOGGER.debug("Heartbeat: querying clock at %s with %s", address, command)
        future = self._coordinator.hass.loop.create_future()
        await self.queue_command(
            command,
            address,
            future=future,
            raw_answer=True,
            # A single attempt, unlike the MAX_ATTEMPTS = 3 every other query
            # gets. Retrying a ping buys nothing - the next one is 30 s away -
            # and it costs a lot: an unanswered attempt occupies the single
            # queue worker for COMMAND_ANSWER_WAIT_TIMEOUT (5 s), so three of
            # them would block every cover command in the house for 15 s out of
            # every 30 while the installation is down. Missing an answer is
            # exactly what the failure threshold is there to absorb.
            max_attempts=1,
        )
        return await asyncio.wait_for(future, timeout=COMMAND_ACK_WAIT_TIMEOUT)

    async def send_command_get_answer(
        self,
        command: str,
        address: str,
        raw_answer: bool = False,
        max_attempts: int | None = None,
    ) -> str:
        """Send a command and wait for an answer from the Nikobus system.

        ``raw_answer`` returns the complete received frame instead of the
        12-hex-digit module state, and ``max_attempts`` overrides the global
        MAX_ATTEMPTS. Only the heartbeat uses either; every existing caller
        keeps the old behaviour.
        """
        _LOGGER.debug(
            "Sending command %s to address %s, waiting for answer", command, address
        )
        wait_ack, wait_answer = self._prepare_ack_and_answer_signals(command, address)
        state = await self._wait_for_ack_and_answer(
            command,
            wait_ack,
            wait_answer,
            raw_answer=raw_answer,
            max_attempts=max_attempts,
        )
        if state is None:
            attempts = MAX_ATTEMPTS if max_attempts is None else max_attempts
            raise NikobusTimeoutError(
                f"Failed to receive state for command '{command}' after "
                f"{attempts} attempts."
            )
        return state

    def _prepare_ack_and_answer_signals(
        self, command: str, address: str
    ) -> tuple[str, str]:
        """
        Prepare the acknowledgment and answer signals based on the command prefix.
        For example, command "$1E..." produces ACK="$05XX" and answer signal using a mapped prefix.
        """
        command_prefix = command[:3]
        command_part = command[3:5]
        ack_signal = f"$05{command_part}"

        prefix_mapping = {
            "$1E": "$0EFF",
            "$05": "$1C",
            "$10": "$1C",
        }
        # Keyed on the FUNCTION code, not on the frame prefix: every command
        # built by make_pc_link_command starts with "$10", so the mapping above
        # cannot tell 0x12 from 0x1D. Measured on 22.08.2026, the clock answer
        # carries an extra FF byte between the length field and the address:
        #
        #   0x12 answer (module state):  $1C     629E ...
        #   0x1D answer (clock):         $1C FF  629E 9BC1 0000 MM SS ...
        #
        # Without this entry the answer signal would be "$1C629E", which does
        # not occur in the clock frame - the ping would then time out on a
        # perfectly healthy installation and take the covers down with it.
        function_answer_prefix_mapping = {
            f"{HEARTBEAT_FUNCTION_CODE:02X}": "$1CFF",
        }
        answer_prefix = function_answer_prefix_mapping.get(
            command_part
        ) or prefix_mapping.get(command_prefix, "$1C")
        answer_signal = f"{answer_prefix}{address[2:]}{address[:2]}"

        _LOGGER.debug(
            "Prepared signals: ACK=%s, ANSWER=%s, COMMAND=%s, ADDRESS=%s",
            ack_signal,
            answer_signal,
            command,
            address,
        )
        return ack_signal, answer_signal

    async def _wait_for_ack_and_answer(
        self,
        command: str,
        wait_ack: str,
        wait_answer: str,
        raw_answer: bool = False,
        max_attempts: int | None = None,
    ) -> str:
        """Wait for an acknowledgment and answer from the Nikobus system with retries.

        ``max_attempts`` defaults to the global MAX_ATTEMPTS; the heartbeat ping
        passes 1 so an unanswered ping cannot hold the single queue worker for
        three full COMMAND_ANSWER_WAIT_TIMEOUT windows (see get_system_clock).
        """
        attempts = MAX_ATTEMPTS if max_attempts is None else max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                await self.nikobus_connection.send(command)
                _LOGGER.debug(
                    "Attempt %d/%d waiting for ACK: %s, ANSWER: %s",
                    attempt,
                    attempts,
                    wait_ack,
                    wait_answer,
                )
                state = await self._wait_for_ack_and_answer_state(
                    wait_ack, wait_answer, raw_answer=raw_answer
                )
                if state is not None:
                    _LOGGER.debug("Received valid state from device.")
                    return state
            except (NikobusSendError, NikobusTimeoutError) as err:
                _LOGGER.warning("Attempt %d error: %s", attempt, err, exc_info=True)
                if attempt == attempts:
                    raise
            except Exception as err:
                _LOGGER.error(
                    "Unhandled exception on attempt %d: %s", attempt, err, exc_info=True
                )
                if attempt == attempts:
                    raise NikobusError(f"Unhandled exception: {err}") from err
        raise NikobusTimeoutError(
            f"Failed to receive ACK and state for command '{command}' after "
            f"{attempts} attempts."
        )

    async def _wait_for_ack_and_answer_state(
        self, wait_ack: str, wait_answer: str, raw_answer: bool = False
    ) -> str | None:
        """Wait for both acknowledgment and answer signals, then extract the state.

        With ``raw_answer`` the complete frame is returned instead of the
        12-hex-digit module state - see ``get_system_clock``.
        """
        ack_received = False
        answer_received = False
        state: str | None = None
        loop = self._coordinator.hass.loop
        end_time = loop.time() + COMMAND_ACK_WAIT_TIMEOUT

        while loop.time() < end_time:
            try:
                remaining = end_time - loop.time()
                message = await asyncio.wait_for(
                    self.nikobus_listener.response_queue.get(),
                    timeout=min(COMMAND_ANSWER_WAIT_TIMEOUT, remaining),
                )
                _LOGGER.debug("Message received: %s", message)
                if wait_ack in message:
                    _LOGGER.debug("ACK received")
                    ack_received = True
                if wait_answer in message:
                    _LOGGER.debug("Answer received")
                    state = (
                        message
                        if raw_answer
                        else self._parse_state_from_message(message, wait_answer)
                    )
                    answer_received = True
                if ack_received and answer_received:
                    return state
            except asyncio.TimeoutError:
                _LOGGER.debug("Timeout while waiting for ACK/Answer")
                break
            except Exception as err:
                _LOGGER.error(
                    "Error while waiting for messages: %s", err, exc_info=True
                )
                raise NikobusError(f"Error while waiting for messages: {err}") from err

        return None

    def _parse_state_from_message(self, message: str, answer_signal: str) -> str:
        """Parse and return the state from a received message."""
        state_index = message.find(answer_signal) + len(answer_signal) + 2
        return message[state_index : state_index + 12]

    async def set_output_state(
        self,
        address: str,
        channel: int,
        value: int,
        completion_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Set the output state of a module."""
        _LOGGER.debug(
            "Setting output state - Address: %s, Channel: %d, Value: %d",
            address,
            channel,
            value,
        )
        group = calculate_group_number(channel)
        command_code = 0x15 if int(group) == 1 else 0x16

        values = await self._prepare_values_for_command(address, group)
        values[(channel - 1) % 6] = value

        command = make_pc_link_command(command_code, address, values)
        await self.queue_command(
            command, address, completion_handler=completion_handler
        )
        _LOGGER.debug("Command successfully queued.")

    async def _prepare_values_for_command(self, address: str, group: int) -> bytearray:
        """Prepare the bytearray values for a command using the latest coordinator state."""
        return self._coordinator.get_bytearray_group_state(address, group)[
            :6
        ] + bytearray([0xFF])

    async def queue_command(
        self,
        command: str,
        address: str | None = None,
        future: asyncio.Future[str] | None = None,
        completion_handler: Callable[[], Awaitable[None]] | None = None,
        raw_answer: bool = False,
        max_attempts: int | None = None,
    ) -> None:
        """Queue a command for processing.

        ``raw_answer`` makes the worker resolve ``future`` with the complete
        received frame instead of the extracted module state, and
        ``max_attempts`` overrides the global MAX_ATTEMPTS for this one item.
        Only the heartbeat ping sets either; everything else keeps the old
        behaviour.
        """
        _LOGGER.debug("Queueing command: %s", command)
        command_item = {
            "command": command,
            "address": address,
            "future": future,
            "completion_handler": completion_handler,
            "raw_answer": raw_answer,
            "max_attempts": max_attempts,
        }
        await self._command_queue.put(command_item)
        _LOGGER.debug("Command queued: %s", command)

    async def queue_command_batch(
        self,
        commands: list[str],
        completion_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Queue a batch of commands to be executed back-to-back."""
        if not commands:
            return
        _LOGGER.debug("Queueing command batch: %s", commands)
        command_item = {
            "command": commands[0],
            "batch": commands,
            "completion_handler": completion_handler,
        }
        await self._command_queue.put(command_item)
        _LOGGER.debug("Command batch queued: %d commands", len(commands))

    async def send_command(self, command: str) -> None:
        """Send a command to the Nikobus system."""
        _LOGGER.debug("Sending command: %s", command)
        try:
            await self.nikobus_connection.send(command)
        except NikobusError as err:
            _LOGGER.error("Failed to send command %s: %s", command, err, exc_info=True)
            raise

    async def set_output_states(
        self,
        address: str,
        completion_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Prepare and queue the output states for a module."""
        _LOGGER.debug("Preparing to set output states for module %s", address)
        channel_states = self.nikobus_module_states[address][:6] + bytearray([0xFF])
        await self.queue_command(
            make_pc_link_command(0x15, address, channel_states),
            address,
            completion_handler=completion_handler,
        )

        # If the module has more than 6 channels, send a second group command.
        if self._coordinator.get_module_channel_count(address) > 6:
            channel_states = self.nikobus_module_states[address][6:12] + bytearray(
                [0xFF]
            )
            await self.queue_command(
                make_pc_link_command(0x16, address, channel_states),
                address,
                completion_handler=completion_handler,
            )
