#!/usr/bin/env python3
"""
JK BMS CLI - Command-line interface for JK BMS (Battery Management System) devices.

Supports communication via Bluetooth Low Energy (BLE) to read and control
JK BMS devices including JK-B1A8S20P, JK-BP1A16S1, JK-BMS-Q36, and similar models.

Each command that talks to a device is self-contained: it connects, performs the
operation, and disconnects within a single process run. This is required because a
BLE connection cannot persist across separate CLI invocations.

Usage:
    python jk_bms_cli.py scan
    python jk_bms_cli.py connect <address>
    python jk_bms_cli.py read <address> <command>
    python jk_bms_cli.py write <address> <command> <value>
    python jk_bms_cli.py monitor <address>
    python jk_bms_cli.py raw <address> <hex>
    python jk_bms_cli.py info
"""

import argparse
import asyncio
import binascii
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import IntFlag, IntEnum
from typing import Optional, List, Dict, Tuple, Callable, Any

try:
    from bleak import BleakScanner, BleakClient, BleakError
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    print("Error: bleak library not installed. Install with: pip install bleak")
    sys.exit(1)


# ============================================================================
# Constants
# ============================================================================

# BLE GATT layout (verified against real hardware):
#   Service  0000ffe0-...  (vendor specific)
#     Char    0000ffe1-...  (write + write-without-response + notify)  <-- data
#     Char    0000ffe2-...  (write-without-response)
JK_BMS_DATA_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
JK_BMS_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"

# Frame headers
#   BMS  -> phone (read / notify responses)
FRAME_HEADER_MAIN = bytes([0x55, 0xAA, 0xEB, 0x90])
#   phone -> BMS  (write / request frames, per protocol doc)
FRAME_HEADER_WRITE = bytes([0x55, 0xAA, 0x7E, 0xE7])
# Cloud/server frame header (not used over BLE)
FRAME_HEADER_CLOUD = bytes([0x7E, 0x81, 0xAA, 0x55])

# Frame types / sub-commands
FRAME_TYPE_DATA_01 = 0x01  # Device information
FRAME_TYPE_DATA_03 = 0x03  # Real-time monitoring
FRAME_TYPE_DATA_06 = 0x06  # Detailed logs
FRAME_TYPE_DATA_96 = 0x96  # Extended data export

# BLE MTU and timing
BLE_MTU = 23
BLE_WRITE_SIZE = 20  # MTU - 3 header bytes
SEND_INTERVAL_MS = 20

# Connection robustness.
# The JK BMS (Beken BLE module bridging the BMS UART) needs a cooldown after a
# session before it will accept a new connection, and single connect attempts are
# flaky. We therefore retry with a growing backoff.
CONNECT_TIMEOUT_S = 15     # per-attempt connect timeout
CONNECT_ATTEMPTS = 5       # total connect attempts
RETRY_WAIT_S = 8           # base wait between attempts (scaled by attempt index)
READ_WAIT_S = 3.0          # how long to wait for a data frame after a request

# Cloud server (for reference)
CLOUD_SERVER = "iot.jk-bms.com"
CLOUD_PORT = 8091
WEBSOCKET_URL = "ws://192.168.108.15:8080"

# Heartbeat intervals
HEARTBEAT_INTERVAL_MS = 30000
REALTIME_INTERVAL_MS = 30000
RECONNECT_INTERVAL_MS = 5000

# Device name keywords used to recognise JK BMS devices during a scan.
BMS_NAME_KEYWORDS = ("JK", "BMS", "JKTECH")


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class CellData:
    """Individual cell voltage data."""
    index: int
    voltage: float  # mV
    voltage_str: str = ""


@dataclass
class TemperatureData:
    """Temperature sensor data."""
    sensor_id: int
    temperature: float  # Celsius


@dataclass
class BMSStatus:
    """Overall BMS status flags."""
    charge_mos: bool = False
    discharge_mos: bool = False
    heating: bool = False
    balance: bool = False
    dry_contact_1: bool = False
    dry_contact_2: bool = False


@dataclass
class AlarmFlags(IntFlag):
    """Alarm bitmask flags."""
    OVER_VOLTAGE = 0x0001
    UNDER_VOLTAGE = 0x0002
    OVER_TEMP = 0x0004
    UNDER_TEMP = 0x0008
    OVER_CURRENT_CHARGE = 0x0010
    OVER_CURRENT_DISCHARGE = 0x0020
    SHORT_CIRCUIT = 0x0040
    BALANCE_FAILURE = 0x0080
    MOS_OVER_TEMP = 0x0100
    COMM_FAULT = 0x0200
    ADC_FAULT = 0x0400
    EEPROM_ERROR = 0x0800


@dataclass
class BMSInfo:
    """Complete BMS information."""
    # Basic info
    cell_count: int = 0
    cell_count_parallel: int = 0
    pack_voltage: float = 0.0
    pack_current: float = 0.0
    soc: int = 0  # Percentage
    soh: int = 0  # Percentage
    cycle_count: int = 0

    # Cell data
    cells: List[CellData] = field(default_factory=list)
    max_cell_voltage: float = 0.0  # mV
    min_cell_voltage: float = 0.0  # mV
    max_cell_diff: float = 0.0  # mV

    # Temperature
    temperatures: List[TemperatureData] = field(default_factory=list)
    max_temp: float = 0.0
    min_temp: float = 0.0

    # Status
    alarm_mask: int = 0
    status: BMSStatus = field(default_factory=BMSStatus)

    # Device info
    model: str = ""
    serial: str = ""
    firmware_version: str = ""
    device_name: str = ""
    rated_capacity: float = 0.0  # Ah
    battery_type: str = "Unknown"  # LFP, NMC, LTO
    cell_series: int = 0
    max_charge_current: float = 0.0
    max_discharge_current: float = 0.0
    device_address: int = 0

    # Raw data
    raw_data: bytes = b""
    timestamp: float = 0.0

    def __str__(self) -> str:
        """Formatted string representation."""
        lines = []
        lines.append("=" * 60)
        lines.append("  JK BMS Device Information")
        lines.append("=" * 60)

        if self.model or self.serial:
            lines.append(f"  Model:      {self.model}")
            lines.append(f"  Serial:     {self.serial}")
        if self.firmware_version:
            lines.append(f"  Firmware:   {self.firmware_version}")

        lines.append(f"\n  Cell Count: {self.cell_count}")
        lines.append(f"  Pack Voltage:  {self.pack_voltage:.2f} V")
        lines.append(f"  Pack Current:  {self.pack_current:.2f} A")
        lines.append(f"  SOC:           {self.soc}%")
        lines.append(f"  SOH:           {self.soh}%")
        lines.append(f"  Cycle Count:   {self.cycle_count}")
        lines.append(f"  Rated Capacity:{self.rated_capacity:.1f} Ah")
        lines.append(f"  Battery Type:  {self.battery_type}")
        lines.append(f"  Cell Series:   {self.cell_series}")

        if self.cells:
            lines.append(f"\n  Cell Voltages:")
            lines.append(f"  {'Cell':>6} {'Voltage (mV)':>12} {'Voltage (V)':>12}")
            lines.append(f"  {'─' * 32}")
            for cell in self.cells:
                lines.append(
                    f"  {cell.index:>6} {cell.voltage:>12.1f} {cell.voltage / 1000:>12.3f}"
                )

        if self.temperatures:
            lines.append(f"\n  Temperatures:")
            lines.append(f"  {'Sensor':>8} {'Temperature (°C)':>18}")
            lines.append(f"  {'─' * 28}")
            for temp in self.temperatures:
                lines.append(
                    f"  {temp.sensor_id:>8} {temp.temperature:>18.1f}"
                )
            lines.append(f"  Max: {self.max_temp:.1f}°C  Min: {self.min_temp:.1f}°C")

        lines.append(f"\n  Max Cell Voltage: {self.max_cell_voltage:.1f} mV")
        lines.append(f"  Min Cell Voltage: {self.min_cell_voltage:.1f} mV")
        lines.append(f"  Max Cell Diff:    {self.max_cell_diff:.1f} mV")

        # Status
        status_str = []
        if self.status.charge_mos:
            status_str.append("CHARGE_ON")
        if self.status.discharge_mos:
            status_str.append("DISCHARGE_ON")
        if self.status.heating:
            status_str.append("HEATING")
        if self.status.balance:
            status_str.append("BALANCING")
        if self.status.dry_contact_1:
            status_str.append("DRY1")
        if self.status.dry_contact_2:
            status_str.append("DRY2")
        lines.append(f"  Status: {', '.join(status_str) if status_str else 'IDLE'}")

        # Alarms
        if self.alarm_mask:
            alarms = []
            try:
                for flag in AlarmFlags:
                    if self.alarm_mask & flag.value:
                        alarms.append(flag.name)
            except ValueError:
                pass
            lines.append(f"  ⚠ ALARMS: {', '.join(alarms)}")
        else:
            lines.append("  ✓ No alarms")

        lines.append("=" * 60)
        return "\n".join(lines)


# ============================================================================
# Protocol Parser
# ============================================================================


class JKProtocolParser:
    """Parser for JK BMS protocol frames."""

    @staticmethod
    def hex_to_bytes(hex_str: str) -> bytes:
        """Convert hex string to bytes."""
        hex_str = hex_str.replace(" ", "").replace(":", "").replace("0x", "")
        return bytes.fromhex(hex_str)

    @staticmethod
    def bytes_to_hex(data: bytes, separator: str = " ") -> str:
        """Convert bytes to hex string."""
        return separator.join(f"{b:02X}" for b in data)

    @staticmethod
    def parse_little_endian_u16(data: bytes, offset: int = 0) -> int:
        """Parse 16-bit unsigned integer (little-endian)."""
        return struct.unpack_from("<H", data, offset)[0]

    @staticmethod
    def parse_little_endian_i16(data: bytes, offset: int = 0) -> int:
        """Parse 16-bit signed integer (little-endian)."""
        return struct.unpack_from("<h", data, offset)[0]

    @staticmethod
    def parse_little_endian_u32(data: bytes, offset: int = 0) -> int:
        """Parse 32-bit unsigned integer (little-endian)."""
        return struct.unpack_from("<I", data, offset)[0]

    @staticmethod
    def parse_little_endian_i32(data: bytes, offset: int = 0) -> int:
        """Parse 32-bit signed integer (little-endian)."""
        return struct.unpack_from("<i", data, offset)[0]

    @staticmethod
    def parse_little_endian_f32(data: bytes, offset: int = 0) -> float:
        """Parse 32-bit float (little-endian, IEEE 754)."""
        return struct.unpack_from("<f", data, offset)[0]

    @staticmethod
    def parse_ascii_string(data: bytes, start: int, length: int) -> str:
        """Parse ASCII string from bytes."""
        end = start + length
        raw = data[start:end]
        # Remove null terminators and trailing spaces
        return raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()

    @staticmethod
    def crc8(data: bytes) -> int:
        """Calculate CRC8 checksum (simple XOR-based)."""
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ 0x7
                else:
                    crc <<= 1
                crc &= 0xFF
        return crc

    @staticmethod
    def _xor_checksum(frame: bytes) -> bytes:
        """Two-byte little-endian checksum: XOR of all preceding bytes."""
        x = 0
        for b in frame:
            x ^= b
        return struct.pack("<H", x & 0xFFFF)

    @staticmethod
    def validate_checksum(data: bytes) -> bool:
        """Validate frame checksum."""
        if len(data) < 4:
            return False
        # Last 2 bytes are checksum
        checksum_offset = len(data) - 2
        if checksum_offset < 4:
            return False
        stored_checksum = JKProtocolParser.parse_little_endian_u16(data, checksum_offset)
        # Calculate checksum of all bytes except checksum
        calc_checksum = 0
        for i in range(checksum_offset):
            calc_checksum ^= data[i]
        return calc_checksum == (stored_checksum & 0xFF)

    @staticmethod
    def serial_crypt(data: str, key: str) -> str:
        """Encrypt/decrypt using serial XOR algorithm."""
        result = []
        key_bytes = JKProtocolParser.hex_to_bytes(key)
        data_bytes = JKProtocolParser.hex_to_bytes(data)
        for i, byte in enumerate(data_bytes):
            key_byte = key_bytes[i % len(key_bytes)]
            result.append(f"{byte ^ key_byte:02X}")
        return "".join(result)

    @staticmethod
    def generate_write_frame(subcmd: int, payload: bytes = b"",
                             sn: bytes = b"\x00" * 16) -> bytes:
        """Build a phone -> BMS request frame.

        Layout (per protocol doc):
            55 AA 7E E7 | SubCmd(1) | Length(2, LE) | SN(16) | Payload | Chk(2)
        """
        body = (
            FRAME_HEADER_WRITE
            + bytes([subcmd])
            + struct.pack("<H", len(payload))
            + sn
            + payload
        )
        return body + JKProtocolParser._xor_checksum(body)

    @staticmethod
    def generate_request_frame(frame_type: int, payload: bytes = b"") -> bytes:
        """Build a request frame using the main (55 AA EB 90) header.

        Kept for compatibility / raw experimentation.
        """
        header = FRAME_HEADER_MAIN + bytes([frame_type])
        frame = header + struct.pack("<H", len(payload)) + payload
        return frame + JKProtocolParser._xor_checksum(frame)

    @staticmethod
    def parse_cell_voltages(data: bytes, start: int, count: int) -> List[CellData]:
        """Parse cell voltage data (2 bytes per cell, little-endian, mV)."""
        cells = []
        for i in range(count):
            offset = start + i * 2
            if offset + 2 > len(data):
                break
            voltage_mv = JKProtocolParser.parse_little_endian_u16(data, offset)
            cells.append(CellData(
                index=i + 1,
                voltage=voltage_mv,
                voltage_str=f"{voltage_mv / 1000:.3f}V"
            ))
        return cells

    @staticmethod
    def parse_temperatures(data: bytes, start: int, count: int) -> List[TemperatureData]:
        """Parse temperature data (1 byte per sensor, Celsius)."""
        temps = []
        for i in range(count):
            offset = start + i
            if offset >= len(data):
                break
            temp = data[offset] - 40  # Offset by 40
            temps.append(TemperatureData(sensor_id=i + 1, temperature=temp))
        return temps


# ============================================================================
# BLE Service
# ============================================================================


class JKBMSService:
    """JK BMS BLE service handler.

    Manages a single connection session: connect (with retry), enable
    notifications, send request frames, collect responses, and disconnect
    cleanly.
    """

    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.parser = JKProtocolParser()
        self._data_callback: Optional[Callable] = None
        self._connected = False
        self._notify_started = False
        self._received: List[bytes] = []
        self._window_start = 0
        self._response_event: Optional[asyncio.Event] = None
        self._response_data: Optional[bytes] = None

    @property
    def is_connected(self) -> bool:
        return bool(self.client is not None and self.client.is_connected)

    # ---- Connection -------------------------------------------------------

    async def connect(self, address: str,
                      timeout: int = CONNECT_TIMEOUT_S,
                      attempts: int = CONNECT_ATTEMPTS,
                      retry_wait: int = RETRY_WAIT_S) -> bool:
        """Connect to the BMS device, retrying with a growing backoff.

        The device needs a cooldown after a previous session and single connect
        attempts are flaky, so we retry several times.
        """
        self._response_event = asyncio.Event()
        last_err: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                self.client = BleakClient(address, timeout=timeout)
                await self.client.connect()
                self._connected = True
                print(f"Connected to {address} (attempt {attempt}/{attempts})")
                return True
            except (BleakError, TimeoutError, OSError) as e:
                last_err = e
                # Make sure a half-open client is dropped before retrying.
                self.client = None
                if attempt < attempts:
                    wait = retry_wait * attempt
                    print(
                        f"  connect attempt {attempt} failed "
                        f"({type(e).__name__}); retrying in {wait}s..."
                    )
                    await asyncio.sleep(wait)
            except Exception as e:  # unexpected; report and stop
                print(f"  connect attempt {attempt} error: {e!r}")
                self.client = None
                break

        print(f"Connection failed after {attempts} attempt(s): {last_err}")
        return False

    async def disconnect(self):
        """Disconnect cleanly (stop notifications first)."""
        client = self.client
        if client is not None and client.is_connected:
            try:
                if self._notify_started:
                    try:
                        await client.stop_notify(JK_BMS_DATA_CHAR_UUID)
                    except Exception:
                        pass
                    self._notify_started = False
                await client.disconnect()
            except Exception as e:
                print(f"Disconnect warning: {e}")
        self._connected = False
        self.client = None
        print("Disconnected")

    # ---- Notifications ----------------------------------------------------

    async def enable_notify(self):
        """Subscribe to notifications on the data characteristic."""
        if not self.is_connected:
            return
        if self._response_event is None:
            self._response_event = asyncio.Event()
        await self.client.start_notify(JK_BMS_DATA_CHAR_UUID, self._on_notify)
        self._notify_started = True
        print("Notifications enabled")

    def _on_notify(self, sender: Any, data: bytes):
        """Handle an incoming notification."""
        d = bytes(data)
        self._received.append(d)
        if len(self._received) > 500:
            self._received = self._received[-500:]

        if self._data_callback:
            try:
                self._data_callback(d)
            except Exception:
                pass

        # Only protocol frames (55 AA EB 90 ...) count as a response.
        if d[:4] == FRAME_HEADER_MAIN and len(d) >= 10:
            self._response_data = d
            if self._response_event is not None:
                self._response_event.set()

    def received_recent(self, n: int = 200) -> List[bytes]:
        """Return the most recent received notification payloads."""
        return self._received[-n:]

    def received_in_window(self, n: int = 200) -> List[bytes]:
        """Return notifications received since the last sent command."""
        return self._received[self._window_start:][-n:]

    # ---- Commands ---------------------------------------------------------

    async def send_command(self, frame_type: int, payload: bytes = b"") -> bytes:
        """Send a phone -> BMS request frame and reset the response latch."""
        frame = self.parser.generate_write_frame(frame_type, payload)
        print(f"Sending request: sub=0x{frame_type:02X} len={len(frame)}")
        print(f"  frame: {self.parser.bytes_to_hex(frame)}")
        self._response_data = None
        self._window_start = len(self._received)
        if self._response_event is not None:
            self._response_event.clear()
        await self.client.write_gatt_char(
            JK_BMS_DATA_CHAR_UUID, frame, response=False
        )
        return frame

    async def read_data(self, frame_type: int = FRAME_TYPE_DATA_03,
                        wait_s: float = READ_WAIT_S) -> Optional[BMSInfo]:
        """Send a request and wait for a protocol frame response."""
        await self.send_command(frame_type)
        got = False
        if self._response_event is not None:
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=wait_s)
                got = True
            except asyncio.TimeoutError:
                got = False
        if got and self._response_data is not None:
            return self.parse_response(self._response_data)
        return None

    # ---- Parsing ----------------------------------------------------------

    def parse_response(self, data: bytes) -> Optional[BMSInfo]:
        """Parse a response frame from BMS."""
        if len(data) < 10:
            return None

        # Check for main frame header
        if data[:4] == FRAME_HEADER_MAIN:
            return self._parse_main_frame(data)
        # Check for cloud frame header
        elif data[:4] == FRAME_HEADER_CLOUD:
            return self._parse_cloud_frame(data)

        return None

    def _parse_main_frame(self, data: bytes) -> Optional[BMSInfo]:
        """Parse main protocol frame."""
        frame_type = data[4]
        if frame_type == FRAME_TYPE_DATA_03:
            return self._parse_data_03(data)
        elif frame_type == FRAME_TYPE_DATA_01:
            return self._parse_data_01(data)
        elif frame_type == FRAME_TYPE_DATA_06:
            return self._parse_data_06(data)
        elif frame_type == FRAME_TYPE_DATA_96:
            return self._parse_data_96(data)
        # Unknown type: return a minimal info carrying the raw frame.
        return BMSInfo(raw_data=data, timestamp=time.time())

    def _parse_data_03(self, data: bytes) -> BMSInfo:
        """Parse DATA_03 (real-time monitoring) frame."""
        info = BMSInfo(raw_data=data, timestamp=time.time())

        if len(data) < 10:
            return info

        # Parse header fields (after header[5]+length[2]+cell_count[2] = offset 9)
        length = self.parser.parse_little_endian_u16(data, 5)
        info.cell_count = self.parser.parse_little_endian_u16(data, 7)
        info.pack_voltage = self.parser.parse_little_endian_f32(data, 9)
        info.pack_current = self.parser.parse_little_endian_f32(data, 13)
        info.soc = self.parser.parse_little_endian_u16(data, 17)
        info.soh = self.parser.parse_little_endian_u16(data, 19)
        info.cycle_count = self.parser.parse_little_endian_u16(data, 21)

        # Parse cell voltages (starting around offset 25)
        cell_start = 25
        num_cells = min(info.cell_count, 32)  # Max 32 cells
        info.cells = self.parser.parse_cell_voltages(data, cell_start, num_cells)

        if info.cells:
            info.max_cell_voltage = max(c.voltage for c in info.cells)
            info.min_cell_voltage = min(c.voltage for c in info.cells)
            info.max_cell_diff = info.max_cell_voltage - info.min_cell_voltage

        # Parse temperatures (around offset 25 + cells*2)
        temp_start = cell_start + num_cells * 2
        num_temps = min(8, max(0, (len(data) - temp_start)))
        info.temperatures = self.parser.parse_temperatures(data, temp_start, num_temps)

        if info.temperatures:
            info.max_temp = max(t.temperature for t in info.temperatures)
            info.min_temp = min(t.temperature for t in info.temperatures)

        # Parse alarm mask (around offset 39)
        alarm_offset = temp_start + num_temps
        if len(data) > alarm_offset + 4:
            info.alarm_mask = self.parser.parse_little_endian_u32(data, alarm_offset)

        # Parse status flags
        status_offset = alarm_offset + 4
        if len(data) > status_offset + 1:
            status_bytes = data[status_offset]
            info.status.charge_mos = bool(status_bytes & 0x01)
            info.status.discharge_mos = bool(status_bytes & 0x02)
            info.status.heating = bool(status_bytes & 0x04)
            info.status.balance = bool(status_bytes & 0x08)

        return info

    def _parse_data_01(self, data: bytes) -> BMSInfo:
        """Parse DATA_01 (device information) frame."""
        info = self._parse_data_03(data)  # Start with same base

        # Additional device info fields
        if len(data) > 80:
            # Parse device model and serial
            info.model = self.parser.parse_ascii_string(data, 80, 16)
            info.serial = self.parser.parse_ascii_string(data, 96, 16)
            info.firmware_version = self.parser.parse_ascii_string(data, 112, 16)
            info.device_name = self.parser.parse_ascii_string(data, 128, 16)

        return info

    def _parse_data_06(self, data: bytes) -> BMSInfo:
        """Parse DATA_06 (detailed logs) frame."""
        info = BMSInfo(raw_data=data, timestamp=time.time())
        # Log parsing would go here
        return info

    def _parse_data_96(self, data: bytes) -> BMSInfo:
        """Parse DATA_96 (extended data export) frame."""
        info = BMSInfo(raw_data=data, timestamp=time.time())
        # Extended data parsing would go here
        return info

    def _parse_cloud_frame(self, data: bytes) -> BMSInfo:
        """Parse cloud protocol frame."""
        info = BMSInfo(raw_data=data, timestamp=time.time())
        # Cloud frame parsing
        return info


# ============================================================================
# Scanning
# ============================================================================


def _is_bms_device(name: Optional[str], adv: AdvertisementData) -> bool:
    """Return True if an advertisement looks like a JK BMS device."""
    for s in adv.service_uuids:
        if s in (JK_BMS_DATA_CHAR_UUID, JK_BMS_SERVICE_UUID):
            return True
    if name:
        up = name.upper()
        if any(kw in up for kw in BMS_NAME_KEYWORDS):
            return True
    return False


async def scan_devices(timeout: int = 5) -> List[Tuple[str, Optional[str], AdvertisementData]]:
    """Scan for nearby JK BMS devices.

    Returns a list of (address, name, advertisement) tuples.
    """
    print(f"Scanning for JK BMS devices ({timeout}s)...")
    # bleak 3.x: discover() no longer accepts a `detection` callback and needs
    # return_adv=True to expose per-device advertisement data (name/RSSI).
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    devices: List[Tuple[str, Optional[str], AdvertisementData]] = []
    # bleak 3.x returns {address: (BLEDevice, AdvertisementData)}.
    for address, (device, adv) in found.items():
        name = getattr(device, "name", None)
        if _is_bms_device(name, adv):
            devices.append((address, name, adv))
    return devices


# ============================================================================
# CLI Commands
# ============================================================================


class JKBMSCli:
    """Command-line interface for JK BMS."""

    def __init__(self):
        self.parser = JKProtocolParser()

    # ---- Helpers ----------------------------------------------------------

    def _report(self, info: Optional[BMSInfo], service: JKBMSService,
                context: str = ""):
        """Print a parsed BMSInfo, or a diagnostic of the raw data received."""
        if context:
            print(f"\n--- {context} " + "-" * max(0, 40 - len(context)))
        if info is not None:
            print(info)
            return

        rec = service.received_in_window(200)
        if not rec:
            print("  (no data received)")
            return
        counts = Counter(rec)
        print(f"  (no data frame received; {len(rec)} notifications in window)")
        for d, n in counts.most_common(5):
            preview = d.hex(" ") if len(d) <= 24 else d[:20].hex(" ") + " ..."
            printable = d.decode("ascii", errors="replace") if all(
                32 <= b < 127 or b in (10, 13) for b in d) else ""
            suffix = f"  ({printable!r})" if printable else ""
            print(f"    x{n}: {preview}{suffix}")

    async def _with_connection(self, address: str, attempts: int,
                               retry_wait: int, coro_fn):
        """Connect, run coro_fn(service), then disconnect. Returns exit code."""
        service = JKBMSService()
        if not await service.connect(address, attempts=attempts,
                                     retry_wait=retry_wait):
            print("Failed to connect.")
            return 1
        try:
            await service.enable_notify()
            return await coro_fn(service)
        finally:
            await service.disconnect()
        return 0

    # ---- Commands ---------------------------------------------------------

    async def cmd_scan(self, args: argparse.Namespace) -> int:
        """Scan for BMS devices."""
        devices = await scan_devices(args.timeout)

        if not devices:
            print("No JK BMS devices found.")
            return 0

        print(f"\nFound {len(devices)} JK BMS device(s):\n")
        print(f"{'Address':<20} {'Name':<24} {'RSSI':>6}")
        print("─" * 52)
        for address, name, adv in devices:
            print(f"{address:<20} {name or 'Unknown':<24} {adv.rssi:>6}")
        return 0

    async def cmd_connect(self, args: argparse.Namespace) -> int:
        """Connection test: connect, report device identity, disconnect."""
        def _to_text(b):
            return b.decode("ascii", errors="replace").strip() if b else ""

        async def run(service: JKBMSService) -> int:
            print(f"\nConnection OK to {args.address}")
            di_uuids = {
                "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
                "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
                "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
                "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer",
            }
            found_any = False
            for svc in service.client.services:
                for ch in svc.characteristics:
                    if ch.uuid in di_uuids:
                        try:
                            val = await service.client.read_gatt_char(ch.uuid)
                            print(f"  {di_uuids[ch.uuid]}: {_to_text(val)}")
                            found_any = True
                        except Exception as e:
                            print(f"  {di_uuids[ch.uuid]}: (read failed: {e})")
            if not found_any:
                print("  (no Device Information service exposed)")
            return 0

        return await self._with_connection(
            args.address, args.attempts, args.retry_wait, run
        )

    async def cmd_read(self, args: argparse.Namespace) -> int:
        """Read data from the BMS."""
        command = args.command.lower()
        frame_map = {
            "info": FRAME_TYPE_DATA_01,
            "status": FRAME_TYPE_DATA_03,
            "realtime": FRAME_TYPE_DATA_03,
            "logs": FRAME_TYPE_DATA_06,
            "extended": FRAME_TYPE_DATA_96,
        }

        async def run(service: JKBMSService) -> int:
            if command == "all":
                for ft in (FRAME_TYPE_DATA_01, FRAME_TYPE_DATA_03,
                           FRAME_TYPE_DATA_06, FRAME_TYPE_DATA_96):
                    info = await service.read_data(ft, wait_s=args.wait)
                    self._report(info, service, context=f"read (type 0x{ft:02X})")
            else:
                info = await service.read_data(frame_map[command], wait_s=args.wait)
                self._report(info, service, context=f"read {command}")
            return 0

        return await self._with_connection(
            args.address, args.attempts, args.retry_wait, run
        )

    async def cmd_write(self, args: argparse.Namespace) -> int:
        """Write configuration to the BMS."""
        async def run(service: JKBMSService) -> int:
            payload = self.build_write_payload(args.command, args.value)
            if payload is None:
                print(f"Invalid command: {args.command}")
                return 1
            # Control-enable requests use sub-command 0x1A.
            await service.send_command(0x1A, payload)
            print(f"Sent write command: {args.command} = {args.value}")
            await asyncio.sleep(1.0)
            return 0

        return await self._with_connection(
            args.address, args.attempts, args.retry_wait, run
        )

    async def cmd_monitor(self, args: argparse.Namespace) -> int:
        """Continuous monitoring for a fixed duration."""
        interval_s = args.interval / 1000.0

        async def run(service: JKBMSService) -> int:
            end = time.time() + args.duration
            print(f"Monitoring {args.address} every {args.interval}ms "
                  f"for {args.duration}s (Ctrl+C to stop)...")
            while time.time() < end and service.is_connected:
                start = time.time()
                info = await service.read_data(
                    FRAME_TYPE_DATA_03, wait_s=min(interval_s, 5.0)
                )
                self._report(info, service, context="monitor")
                elapsed = time.time() - start
                sleep_for = max(0.0, interval_s - elapsed)
                await asyncio.sleep(sleep_for)
            return 0

        try:
            return await self._with_connection(
                args.address, args.attempts, args.retry_wait, run
            )
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
            return 0

    async def cmd_raw(self, args: argparse.Namespace) -> int:
        """Send raw hex data."""
        async def run(service: JKBMSService) -> int:
            data = self.parser.hex_to_bytes(args.data)
            await service.client.write_gatt_char(
                JK_BMS_DATA_CHAR_UUID, data, response=False
            )
            print(f"Sent {len(data)} bytes: {args.data}")
            await asyncio.sleep(1.0)
            self._report(None, service, context="raw (raw notifications)")
            return 0

        return await self._with_connection(
            args.address, args.attempts, args.retry_wait, run
        )

    async def cmd_info(self, args: argparse.Namespace) -> int:
        """Show protocol information."""
        print("JK BMS CLI - Battery Management System Control")
        print("=" * 60)
        print(f"\nBLE GATT (verified on hardware):")
        print(f"  Service:   {JK_BMS_SERVICE_UUID}")
        print(f"  Data Char: {JK_BMS_DATA_CHAR_UUID}  (write + notify)")
        print(f"\nFrame Headers:")
        print(f"  BMS -> phone (read):  {self.parser.bytes_to_hex(FRAME_HEADER_MAIN, '')}")
        print(f"  phone -> BMS (write): {self.parser.bytes_to_hex(FRAME_HEADER_WRITE, '')}")
        print(f"  Cloud (server):       {self.parser.bytes_to_hex(FRAME_HEADER_CLOUD, '')}")
        print(f"\nFrame Types / Sub-commands:")
        print(f"  0x01 - Device Information")
        print(f"  0x03 - Real-time Monitoring")
        print(f"  0x06 - Detailed Logs")
        print(f"  0x96 - Extended Data Export")
        print(f"\nConnection Settings:")
        print(f"  BLE MTU:           {BLE_MTU}")
        print(f"  Write Size:        {BLE_WRITE_SIZE} bytes")
        print(f"  Connect Timeout:   {CONNECT_TIMEOUT_S}s")
        print(f"  Connect Attempts:  {CONNECT_ATTEMPTS}")
        print(f"  Retry Base Wait:   {RETRY_WAIT_S}s")
        print(f"\nCloud Server:")
        print(f"  Host: {CLOUD_SERVER}:{CLOUD_PORT}")
        print(f"  WebSocket: {WEBSOCKET_URL}")
        print(f"\nHeartbeat:")
        print(f"  Interval: {HEARTBEAT_INTERVAL_MS}ms")
        print(f"  Reconnect: {RECONNECT_INTERVAL_MS}ms")
        return 0

    # ========================================================================
    # Write payload builders
    # ========================================================================

    def build_write_payload(self, command: str, value: str) -> Optional[bytes]:
        """Build write command payload."""
        commands = {
            "balance": self._payload_balance,
            "charge": self._payload_charge,
            "discharge": self._payload_discharge,
            "protection": self._payload_protection,
            "calibration": self._payload_calibration,
        }
        handler = commands.get(command.lower())
        if handler:
            return handler(value)
        return None

    def _payload_balance(self, value: str) -> bytes:
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x01, 0x01 if enabled else 0x00])

    def _payload_charge(self, value: str) -> bytes:
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x02, 0x01 if enabled else 0x00])

    def _payload_discharge(self, value: str) -> bytes:
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x03, 0x01 if enabled else 0x00])

    def _payload_protection(self, value: str) -> bytes:
        return bytes([0x04, 0x00])

    def _payload_calibration(self, value: str) -> bytes:
        return bytes([0x05, 0x00])


# ============================================================================
# Argument Parsing
# ============================================================================


def _add_connection_args(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--attempts", type=int, default=CONNECT_ATTEMPTS,
        help=f"connection attempts (default: {CONNECT_ATTEMPTS})"
    )
    parser.add_argument(
        "--retry-wait", type=int, default=RETRY_WAIT_S,
        help=f"base seconds to wait between connection attempts "
             f"(default: {RETRY_WAIT_S})"
    )


def create_argument_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="jk_bms_cli",
        description="JK BMS CLI - Control JK BMS devices via Bluetooth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s scan                              Scan for BMS devices
  %(prog)s connect AA:BB:CC:DD:EE:FF       Test the connection
  %(prog)s read AA:BB:CC:DD:EE:FF status   Read real-time status
  %(prog)s read AA:BB:CC:DD:EE:FF all      Read all data types
  %(prog)s write AA:BB:CC:DD:EE:FF balance on
  %(prog)s monitor AA:BB:CC:DD:EE:FF -i 3000 -d 30
  %(prog)s raw AA:BB:CC:DD:EE:FF 55AAEB90030000
  %(prog)s info                             Show protocol info
        """
    )

    subparsers = parser.add_subparsers(dest="cmd", help="Command to execute")

    # Scan
    scan_parser = subparsers.add_parser("scan", help="Scan for BMS devices")
    scan_parser.add_argument("--timeout", "-t", type=int, default=5,
                             help="scan duration in seconds (default: 5)")

    # Connect
    connect_parser = subparsers.add_parser("connect", help="Test connection to a device")
    connect_parser.add_argument("address", help="MAC address of the BMS device")
    _add_connection_args(connect_parser)

    # Read
    read_parser = subparsers.add_parser("read", help="Read data from BMS")
    read_parser.add_argument("address", help="MAC address of the BMS device")
    read_parser.add_argument("command",
                             choices=["info", "status", "realtime", "logs",
                                      "extended", "all"],
                             help="data to read")
    read_parser.add_argument("--wait", type=float, default=READ_WAIT_S,
                             help="seconds to wait for a data frame (default: 3)")
    _add_connection_args(read_parser)

    # Write
    write_parser = subparsers.add_parser("write", help="Write configuration to BMS")
    write_parser.add_argument("address", help="MAC address of the BMS device")
    write_parser.add_argument("command",
                             choices=["balance", "charge", "discharge",
                                      "protection", "calibration"],
                             help="setting to modify")
    write_parser.add_argument("value",
                             help="value to set (on/off, true/false, 1/0, yes/no)")
    _add_connection_args(write_parser)

    # Monitor
    monitor_parser = subparsers.add_parser("monitor", help="Continuous monitoring")
    monitor_parser.add_argument("address", help="MAC address of the BMS device")
    monitor_parser.add_argument("--interval", "-i", type=int, default=3000,
                                help="update interval in ms (default: 3000)")
    monitor_parser.add_argument("--duration", "-d", type=int, default=15,
                                help="how long to monitor in seconds (default: 15)")
    _add_connection_args(monitor_parser)

    # Raw
    raw_parser = subparsers.add_parser("raw", help="Send raw hex data")
    raw_parser.add_argument("address", help="MAC address of the BMS device")
    raw_parser.add_argument("data", help="hex data to send (e.g. 55AAEB90030000)")
    _add_connection_args(raw_parser)

    # Info
    subparsers.add_parser("info", help="Show protocol information")

    return parser


# ============================================================================
# Main Entry Point
# ============================================================================


async def main() -> int:
    """Main entry point."""
    parser = create_argument_parser()
    args = parser.parse_args()

    cmd = getattr(args, "cmd", None)
    if not cmd:
        parser.print_help()
        return 0

    cli = JKBMSCli()
    exit_code = 0
    try:
        if cmd == "scan":
            exit_code = await cli.cmd_scan(args)
        elif cmd == "connect":
            exit_code = await cli.cmd_connect(args)
        elif cmd == "read":
            exit_code = await cli.cmd_read(args)
        elif cmd == "write":
            exit_code = await cli.cmd_write(args)
        elif cmd == "monitor":
            exit_code = await cli.cmd_monitor(args)
        elif cmd == "raw":
            exit_code = await cli.cmd_raw(args)
        elif cmd == "info":
            exit_code = await cli.cmd_info(args)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
