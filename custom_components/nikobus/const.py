"""Constants for the Nikobus integration."""

from typing import Final

# =============================================================================
# General
# =============================================================================
DOMAIN: Final[str] = "nikobus"
BRAND: Final[str] = "Niko"

# Device-registry identifier of the Nikobus bridge (hub). Upstream 3.x keeps it
# here, so the port finds it in the expected place. NOTE: __init__.py, cover.py,
# light.py and switch.py each still carry their own local copy of the same
# literal; those are deliberately left untouched (they sit next to the YAML cover
# identity code, which must not be modified), so the value has to stay "nikobus_hub".
HUB_IDENTIFIER: Final[str] = "nikobus_hub"

# =============================================================================
# Configuration Keys
# =============================================================================
CONF_CONNECTION_STRING: Final[str] = "connection_string"
CONF_REFRESH_INTERVAL: Final[str] = "refresh_interval"
CONF_HAS_FEEDBACK_MODULE: Final[str] = "has_feedbackmodule"
CONF_HAS_PC_LINK: Final[str] = "has_pclink"
CONF_PRIOR_GEN3: Final[str] = "prior_gen3"
CONF_DISABLE_DISCOVERY: Final[str] = "disable_discovery"

# YAML-defined covers
CONF_COVERS: Final[str] = "covers"
CONF_GROUP_COVERS: Final[str] = "group_covers"
CONF_COVER_NAME: Final[str] = "name"
CONF_COVER_UP_CODE: Final[str] = "up_code"
CONF_COVER_DOWN_CODE: Final[str] = "down_code"
CONF_COVER_STOP_CODE: Final[str] = "stop_code"
CONF_TRAVEL_UP_TIME: Final[str] = "travel_up_time"
CONF_TRAVEL_DOWN_TIME: Final[str] = "travel_down_time"
CONF_COVER_SIGNAL_REPEAT: Final[str] = "cover_signal_repeat"
CONF_COVER_AS_SWITCH: Final[str] = "as_switch"
CONF_COVER_AREA: Final[str] = "area"

# Physical bus buttons that drive a cover. These are the addresses a wall button
# puts on the bus, which are distinct from the up/down/stop codes the integration
# sends. The module reacts to them on its own, so they only mirror the state.
CONF_BUTTON_UP_CODES: Final[str] = "button_up_codes"
CONF_BUTTON_DOWN_CODES: Final[str] = "button_down_codes"

# A group that exists only to mirror a physical button can be kept out of the UI.
CONF_COVER_HIDDEN: Final[str] = "hidden"

# How far member positions may differ (in percent) before a group still counts as
# "all at the same height" and may be stopped with a single group command.
CONF_GROUP_STOP_TOLERANCE: Final[str] = "group_stop_tolerance"
DEFAULT_GROUP_STOP_TOLERANCE: Final[int] = 5

# Seconds a cover may sit at an end position before a stop is sent to release the
# relay in the cabinet. Any new action cancels it. 0 disables the cleanup.
CONF_END_STOP_CLEANUP_DELAY: Final[str] = "end_stop_cleanup_delay"
DEFAULT_END_STOP_CLEANUP_DELAY: Final[int] = 300

# =============================================================================
# Serial Connection
# =============================================================================
BAUD_RATE: Final[int] = 9600
COMMANDS_HANDSHAKE: Final[list[str]] = [
    "++++",
    "ATH0",
    "ATZ",
    "$10110000B8CF9D",
    "#L0",
    "#E0",
    "#L0",
    "#E1",
]
EXPECTED_HANDSHAKE_RESPONSE: Final[str] = "$0511"
HANDSHAKE_TIMEOUT: Final[int] = 60  # Timeout for handshake in seconds

# =============================================================================
# Buttons
# =============================================================================
REFRESH_DELAY: Final[float] = 0.5  # Delay before retrieving status after button press
DIMMER_DELAY: Final[int] = 1  # Delay before retrieving dimmer status
SHORT_PRESS: Final[float] = 1.0  # Short press duration in seconds
MEDIUM_PRESS: Final[int] = 2  # Medium press duration in seconds
LONG_PRESS: Final[float] = 3.0  # Long press duration threshold in seconds
BUTTON_TIMER_THRESHOLDS: Final[tuple[int, int, int]] = (1, 2, 3)

# =============================================================================
# Listener
# =============================================================================
BUTTON_COMMAND_PREFIX: Final[str] = "#N"
IGNORE_ANSWER: Final[str] = "$0E"  # Unknown response
FEEDBACK_REFRESH_COMMAND: Final[tuple[str, str]] = ("$1012", "$1017")
FEEDBACK_MODULE_ANSWER: Final[str] = "$1C"
MANUAL_REFRESH_COMMAND: Final[tuple[str, str]] = ("$0512", "$0517")
COMMAND_PROCESSED: Final[tuple[str, str]] = ("$0515", "$0516")
DEVICE_ADDRESS_INVENTORY: Final[str] = "$18"
DEVICE_INVENTORY: Final[tuple[str, str]] = ("$0510$2E", "$0522$1E")

# =============================================================================
# Command Execution
# =============================================================================
COMMAND_EXECUTION_DELAY: Final[float] = 0.7  # Delay between command executions
COMMAND_REPEAT_BURST_DELAY: Final[float] = 0.1  # Delay between repeat burst commands
COMMAND_ACK_WAIT_TIMEOUT: Final[int] = 15  # Timeout for command ACK
COMMAND_ANSWER_WAIT_TIMEOUT: Final[int] = 5  # Timeout for each loop waiting for an answer
MAX_ATTEMPTS: Final[int] = 3  # Maximum retry attempts

# =============================================================================
# Reconnect
# =============================================================================
# On 21.08.2026 the host was rebooted and the USB enumeration order changed: the
# FTDI adapter this Nikobus installation hangs on had been /dev/ttyUSB1 before
# the reboot and was /dev/ttyUSB0 afterwards. Home Assistant still reported the
# integration as "loaded" and all 26 cover entities stayed available, but every
# single drive command died with
#
#     custom_components.nikobus.exceptions.NikobusSendError:
#         Writer is not available for sending commands.
#     nkbcommand.py, process_commands() -> nkbconnect.py send()
#
# for two hours. Nobody noticed until somebody stood in front of a blind that
# would not move. The integration knew the whole time and only wrote it to the
# log. These two values drive the supervised reconnect that closes that gap.
#
# Names and values are taken verbatim from upstream 3.x so that porting this
# fork onto that base is a no-op for these constants.
RECONNECT_DELAY_INITIAL: Final[int] = 5  # First retry delay in seconds
RECONNECT_DELAY_MAX: Final[int] = 60  # Cap on exponential-backoff delay

# =============================================================================
# Heartbeat (is the installation still alive?)
# =============================================================================
# The reconnect above only knows whether the *transport* is open. It cannot see
# a PC-Link that is still acknowledging on an open serial port while the rest of
# the installation has stopped doing anything. This block is the answer to that
# second question, and every value in it comes from a measurement run performed
# on this installation on 22.08.2026.
#
# What this installation actually answers
# ---------------------------------------
# It has NO feedback module and runs prior_gen3. All 254 possible function codes
# were tried on the bus:
#
#   * 0x12 and 0x17 (the output-state queries the coordinator polls with):
#     NO answer at all. There is no output-state feedback on this installation.
#     That is confirmed and it is not fixable from software.
#   * Only 0x10, 0x11, 0x14, 0x18, 0x19, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F and 0x20
#     answer at all. Everything from 0x21 upwards stays silent.
#   * Most of those answer with $0E, "unknown response".
#   * Response time was 0.20-0.21 s throughout, for every code that answered.
#
# The one useful answer: 0x1D carries a running clock
# ---------------------------------------------------
# 0x1D sent to address 9E62 answers with a frame that contains a CLOCK:
#
#     $051D $1C FF 629E 9BC1 0000 <MM> <SS> <CRC16> <CRC8>
#
# MM and SS are one byte each, read as hexadecimal, and the seconds roll over at
# 60 rather than at 0xFF:
#
#     0x14 0x3A = 20:58   ->   0x15 0x01 = 21:01
#
# Over 43 samples in 141 s that clock ran exactly in step with the wall clock:
# zero samples deviated by more than 1.5 s. Driving the shutters up and down
# while sampling did not disturb it.
#
# Why a clock, and not just "something answered"
# ----------------------------------------------
# A constant answer only proves that *someone* replies; the reply can come out
# of a buffer that a hung device never stops serving. That is exactly how a
# weather station slipped through on 21.08.2026 - a valid, plausible reading
# from a device that had been dead for hours. A clock cannot do that. If it
# stands still while real time passes, the device is stuck, no matter how
# politely it keeps acknowledging.
HEARTBEAT_FUNCTION_CODE: Final[int] = 0x1D

# The address the clock lives at. 9E62 is what was measured here on 22.08.2026;
# other installations will have a different one, which is why this is only the
# default and can be overridden per config entry (CONF_HEARTBEAT_ADDRESS). If no
# address is configured the heartbeat stays switched off rather than guessing:
# a wrong address answers $0E or not at all, which would look exactly like a
# dead installation and would take all 26 covers down for no reason.
CONF_HEARTBEAT_ADDRESS: Final[str] = "heartbeat_address"
DEFAULT_HEARTBEAT_ADDRESS: Final[str] = "9E62"

# One query every 30 s. The answer takes 0.21 s and goes through the same
# command queue as everything else, which spaces commands by
# COMMAND_EXECUTION_DELAY (0.7 s), so the ping costs well under 3% of the bus.
HEARTBEAT_INTERVAL: Final[int] = 30

# Seconds the clock may disagree with the elapsed wall time before the sample is
# rejected. NOT a guess: the clock has a resolution of one second, so any single
# comparison is already quantised by +-1 s, and the 43 samples of 22.08.2026
# never drifted further than 1.5 s from the wall clock. +-2 s is that measured
# spread rounded up to the next whole second.
HEARTBEAT_CLOCK_TOLERANCE: Final[float] = 2.0

# The clock wraps at 60:00, so all arithmetic on it is modulo one hour.
HEARTBEAT_CLOCK_PERIOD: Final[int] = 3600

# How many consecutive bad samples before the covers are declared unavailable.
# 1 sample at 30 s: the first unanswered ping counts.
#
# This started at 3 (90 s) on the assumption that a lost answer is normal on a
# bus that also carries button traffic. That assumption was never measured, and
# on this installation it does not appear to hold: 0x1D answered on every
# attempt of the 254-code sweep, all 43 samples of the clock run came back, and
# the query is serialised through the same queue as every drive command, so it
# never competes with one. On a bus this quiet, "no answer" is information, and
# waiting for it to happen three times only delays acting on it.
#
# The two directions of error are not symmetric here, which is what makes 1
# defensible:
#
#   a lost frame that means nothing -> the covers show as unavailable for up to
#       30 s and then come back by themselves. Nothing is commanded, nothing
#       breaks, and the Watchtower check does not alarm either: it probes every
#       60 s and needs two consecutive failures, so a single-poll blip cannot
#       reach anybody's phone.
#   a real outage -> at 3 it stayed invisible for 90 s. On 21.08.2026 it stayed
#       invisible for two hours, and the whole point of this ping is to make
#       that interval short.
#
# So the cost of being too eager is a brief "not responding" in Apple Home, and
# the cost of being too patient is the failure this was built for. If the
# measurement below ever shows lost answers on an otherwise healthy bus, this is
# the number to raise - and by then it would be a measurement, not a guess.
#
# Measured 22.08.2026 over 37 min of live polling: see BUS_MEASUREMENTS.md.
HEARTBEAT_FAILURE_THRESHOLD: Final[int] = 1

# =============================================================================
# Discovery
# =============================================================================
DEVICE_TYPES: Final[dict[str, dict[str, str | int]]] = {
    "01": {
        "Category": "Module",
        "Model": "05-000-02",
        "Channels": 12,
        "Name": "Switch Module",
    },
    "02": {
        "Category": "Module",
        "Model": "05-001-02",
        "Channels": 6,
        "Name": "Roller Shutter Module",
    },
    "03": {
        "Category": "Module",
        "Model": "05-007-02",
        "Channels": 12,
        "Name": "Dimmer Module",
    },
    "04": {
        "Category": "Button",
        "Model": "05-342",
        "Channels": 2,
        "Name": "Button with 2 Operation Points",
    },
    "06": {
        "Category": "Button",
        "Model": "05-346",
        "Channels": 4,
        "Name": "Button with 4 Operation Points",
    },
    "08": {
        "Category": "Module",
        "Model": "05-201",
        "Name": "PC Logic",
    },
    "09": {
        "Category": "Module",
        "Model": "05-002-02",
        "Channels": 4,
        "Name": "Compact Switch Module",
    },
    "0A": {
        "Category": "Module",
        "Model": "05-200",
        "Name": "PC Link",
    },
    "0C": {
        "Category": "Button",
        "Model": "05-348",
        "Channels": 4,
        "Name": "IR Button with 4 Operation Points",
    },
    "12": {
        "Category": "Button",
        "Model": "05-349",
        "Channels": 8,
        "Name": "Button with 8 Operation Points",
    },
    "1F": {
        "Category": "Button",
        "Model": "05-311",
        "Channels": 2,
        "Name": "RF Transmitter with 2 Operation Points",
    },
    "23": {
        "Category": "Button",
        "Model": "05-312",
        "Channels": 4,
        "Name": "RF Transmitter with 4 Operation Points",
    },
    "25": {
        "Category": "Button",
        "Model": "05-311",
        "Channels": 1,
        "Name": "Portable RF Transmitter with 1 Operation Point",
    },
    "28": {
        "Category": "Button",
        "Model": "05-7X5",
        "Channels": 2,
        "Name": "Motion Detector",
    },
    "31": {
        "Category": "Module",
        "Model": "05-002-02",
        "Channels": 4,
        "Name": "Compact Switch Module",
    },
    "32": {
        "Category": "Module",
        "Model": "05-008-02",
        "Channels": 4,
        "Name": "Compact Dim Controller",
    },
    "37": {
        "Category": "Module",
        "Model": "05-206",
        "Channels": 6,
        "Name": "Modular Interface 6 inputs",
    },
    "3D": {
        "Category": "Button",
        "Model": "05-312",
        "Channels": 52,
        "Name": "RF Transmitter, 52 operation points",
    },
    "3F": {
        "Category": "Button",
        "Model": "05-060-02",
        "Channels": 2,
        "Name": "Feedback Button with 2 Operation Points",
    },
    "40": {
        "Category": "Button",
        "Model": "05-064-02",
        "Channels": 4,
        "Name": "Feedback Button with 4 Operation Points",
    },
    "41": {
        "Category": "Button",
        "Model": "05-078-02",
        "Channels": 8,
        "Name": "Feedback Button with 8 Operation Points",
    },
    "42": {
        "Category": "Module",
        "Model": "05-207",
        "Name": "Feedback Module",
    },
    "43": {
        "Category": "Button",
        "Model": "05-058",
        "Channels": 4,
        "Name": "Universal interface",
    },
    "44": {
        "Category": "Button",
        "Model": "05-058",
        "Channels": 8,
        "Name": "Switch Interface",
    },
}

CHANNEL_MAPPING: Final[dict[int, str]] = {
    0: "Channel 1",
    1: "Channel 2",
    2: "Channel 3",
    3: "Channel 4",
    4: "Channel 5",
    5: "Channel 6",
    6: "Channel 7",
    7: "Channel 8",
    8: "Channel 9",
    9: "Channel 10",
    10: "Channel 11",
    11: "Channel 12",
}

KEY_MAPPING: Final[dict[int, dict[str, str]]] = {
    1: {"1A": "8"},
    2: {"1A": "8", "1B": "C"},
    4: {"1A": "8", "1B": "C", "1C": "0", "1D": "4"},
    8: {
        "1A": "A",
        "1B": "E",
        "1C": "2",
        "1D": "6",
        "2A": "8",
        "2B": "C",
        "2C": "0",
        "2D": "4",
    },
}

KEY_MAPPING_MODULE: Final[dict[int, dict[int, str]]] = {
    1: {1: "8"},
    2: {1: "8", 3: "C"},
    4: {0: "0", 1: "8", 2: "4", 3: "C"},
    8: {0: "0", 1: "8", 2: "4", 3: "C", 4: "2", 5: "A", 6: "6", 7: "E"},
}

# =============================================================================
# Switch
# =============================================================================
SWITCH_MODE_MAPPING: Final[dict[int, str]] = {
    0: "M01 (On / off)",
    1: "M02 (On, with operating time)",
    2: "M03 (Off, with operation time)",
    3: "M04 (Pushbutton)",
    4: "M05 (Impulse)",
    5: "M06 (Delayed off (long up to 2h))",
    6: "M07 (Delayed on (long up to 2h))",
    7: "M08 (Flashing)",
    8: "M11 (Delayed off (short up to 50sec.))",
    9: "M12 (Delayed on (short up to 50sec.))",
    10: "M14 (Light scene on)",
    11: "M15 (Light scene on / off)",
}

SWITCH_TIMER_MAPPING: Final[dict[int, list[str | None]]] = {
    0: ["10s", "0.5s", "0s"],
    1: ["1m", "1s", "1s"],
    2: ["2m", "2s", "2s"],
    3: ["3m", "3s", "3s"],
    4: ["4m", "4s", None],
    5: ["5m", "5s", None],
    6: ["6m", "6s", None],
    7: ["7m", "7s", None],
    8: ["8m", "8s", None],
    9: ["9m", "9s", None],
    10: ["15m", "15s", None],
    11: ["30m", "20s", None],
    12: ["45m", "25s", None],
    13: ["60m", "30s", None],
    14: ["90m", "40s", None],
    15: ["120m", "50s", None],
}

# =============================================================================
# Roller
# =============================================================================
ROLLER_MODE_MAPPING: Final[dict[int, str]] = {
    0: "M01 (Open - stop - close)",
    1: "M02 (Open)",
    2: "M03 (Close)",
    3: "M04 (Stop)",
    4: "M05 (Interface- and RF-control)",
    5: "M06 (Open with operating time)",
    6: "M07 (Close with operating time)",
}

ROLLER_TIMER_MAPPING: Final[dict[int, list[str | None]]] = {
    0: ["Turned off", None, None],
    1: ["0,4 s (impuls)", None, None],
    2: ["6 s", None, None],
    3: ["8 s", None, None],
    4: ["10 s", None, None],
    5: ["12 s", None, None],
    6: ["6 s", None, None],
    7: ["14 s", None, None],
    8: ["16 s", None, None],
    9: ["18 s", None, None],
    10: ["20 s", None, None],
    11: ["25 s", None, None],
    12: ["30 s", None, None],
    13: ["40 s", None, None],
    14: ["50 s", None, None],
    15: ["60 s", None, None],
    16: ["90 s", None, None],
}

# =============================================================================
# Dimmer
# =============================================================================
DIMMER_MODE_MAPPING: Final[dict[int, str]] = {
    0: "M01 (Dim on/off (2 buttons))",
    1: "M02 (Dim on/off (4 buttons))",
    2: "M03 (Light scene on/off)",
    3: "M04 (Light scene on)",
    4: "M05 (On (if necessary with operating time))",
    5: "M06 (Off (eventually with operating time))",
    6: "M07 (Delayed off)",
    7: "M08 (Flashing)",
    8: "M11 (Preset on/off)",
    9: "M12 (Preset on)",
    10: "M13 (Dim on/off (1key))",
    11: "M14 (Dim on/off memory (1key))",
}

DIMMER_TIMER_MAPPING: Final[dict[int, list[str | None]]] = {
    0: ["1,0 V", "T2=Dimming time on; Dimming time off=1s", "1 s"],
    1: ["1,5 V", "T2=Dimming time off; Dimming time on=1s", "2 s"],
    2: ["2,0 V", "T2=Dimming time off; Dimming time on", "4 s"],
    3: ["2,5 V", None, "6 s"],
    4: ["3,0 V", None, "8 s"],
    5: ["3,0 V", None, "10 s"],
    6: ["4,0 V", None, "15 s"],
    7: ["4,5 V", None, "20 s"],
    8: ["5,0 V", None, "30 s"],
    9: ["5,5 V", None, "40 s"],
    10: ["6,0 V", None, "1 m"],
    12: ["7,0 V", None, "2 m"],
    13: ["7,5 V", None, "3 m"],
    14: ["8,0 V", None, "4 m"],
    15: ["8,5 V", None, "5 m"],
    16: ["9,5 V", None, None],
    17: ["10,0 V", None, None],
}
