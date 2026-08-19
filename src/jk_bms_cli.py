#!/usr/bin/env python3
"""
JK BMS CLI - Command-line interface for JK BMS (Battery Management System) devices.

Supports communication via Bluetooth Low Energy (BLE) to read and control
JK BMS devices including JK-B1A8S20P, JK-BP1A16S1, JK-BMS-Q36, and similar models.

Usage:
    python jk_bms_cli.py scan
    python jk_bms_cli.py connect <mac_address>
    python jk_bms_cli.py read <command>
    python jk_bms_cli.py write <command> <value>
    python jk_bms_cli.py monitor
"""

import argparse
import asyncio
import binascii
import struct
import sys
import time
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

# BLE Service UUIDs
JK_BMS_DATA_SERVICE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
JK_BMS_SERVICE_UUID = "0000ffe0-0000-1000-00805f9b34fb"

# Frame headers
FRAME_HEADER_MAIN = bytes([0x55, 0xAA, 0xEB, 0x90])
FRAME_HEADER_CLOUD = bytes([0x7E, 0x81, 0xAA, 0x55])

# Frame types
FRAME_TYPE_DATA_01 = 0x01  # Device information
FRAME_TYPE_DATA_03 = 0x03  # Real-time monitoring
FRAME_TYPE_DATA_06 = 0x06  # Detailed logs
FRAME_TYPE_DATA_96 = 0x96  # Extended data export

# BLE MTU and timing
BLE_MTU = 23
BLE_WRITE_SIZE = 20  # MTU - 3 header bytes
SEND_INTERVAL_MS = 20
CONNECT_TIMEOUT_S = 20

# Cloud server (for reference)
CLOUD_SERVER = "iot.jk-bms.com"
CLOUD_PORT = 8091
WEBSOCKET_URL = "ws://192.168.108.15:8080"

# Heartbeat intervals
HEARTBEAT_INTERVAL_MS = 30000
REALTIME_INTERVAL_MS = 30000
RECONNECT_INTERVAL_MS = 5000

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
    def generate_request_frame(frame_type: int, payload: bytes = b"") -> bytes:
        """Generate a request frame to send to BMS."""
        header = FRAME_HEADER_MAIN + bytes([frame_type])
        length = len(payload)
        frame = header + struct.pack("<H", length) + payload
        # Add checksum (last 2 bytes)
        checksum = 0
        for b in frame:
            checksum ^= b
        frame += struct.pack("<H", checksum & 0xFFFF)
        return frame

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
# BLE Client
# ============================================================================


class JKBMSService:
    """JK BMS BLE service handler."""

    def __init__(self, client: BleakClient):
        self.client = client
        self.parser = JKProtocolParser()
        self._data_callback: Optional[Callable] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    async def connect(self, mac_or_address: str, timeout: int = CONNECT_TIMEOUT_S) -> bool:
        """Connect to BMS device."""
        try:
            self.client = BleakClient(mac_or_address, timeout=timeout)
            await self.client.connect()
            self._connected = True
            print(f"Connected to {mac_or_address}")
            return True
        except BleakError as e:
            print(f"Connection failed: {e}")
            return False

    async def disconnect(self):
        """Disconnect from BMS device."""
        if self.client.is_connected:
            await self.client.disconnect()
            self._connected = False
            print("Disconnected")

    async def scan_devices(self, timeout: int = 5) -> List[Tuple[BLEDevice, AdvertisementData]]:
        """Scan for nearby BMS devices."""
        print(f"Scanning for JK BMS devices ({timeout}s)...")
        devices = []

        # Use the correct BleakScanner API
        found_devices = await BleakScanner.discover(
            timeout=timeout,
            detection=lambda d, a: self._is_bms_device(d, a)
        )

        for device in found_devices:
            # Get advertisement data if available
            devices.append((device, None))

        return devices

    def _is_bms_device(self, device: BLEDevice, adv: AdvertisementData) -> bool:
        """Check if device is a JK BMS device."""
        # Check for JK BMS service UUIDs or name patterns
        if JK_BMS_DATA_SERVICE_UUID in adv.service_uuids:
            return True
        if JK_BMS_SERVICE_UUID in adv.service_uuids:
            return True
        # Check device name
        name = adv.local_name or ""
        if any(kw in name.upper() for kw in ["JK", "BMS", "JKTECH"]):
            return True
        return False

    async def send_command(self, frame_type: int, payload: bytes = b"") -> Optional[bytes]:
        """Send a command frame to the BMS and wait for response."""
        frame = self.parser.generate_request_frame(frame_type, payload)
        print(f"Sending command: type=0x{frame_type:02X}, payload_len={len(payload)}")
        print(f"Frame: {self.parser.bytes_to_hex(frame)}")

        try:
            # Write to characteristic
            await self.client.write_gatt_char(
                JK_BMS_DATA_SERVICE_UUID,
                frame,
                response=False  # Write without response for speed
            )

            # Wait for response notification
            await asyncio.sleep(0.5)
            return None  # Response comes via notification callback
        except BleakError as e:
            print(f"Send error: {e}")
            return None

    async def read_data(self, frame_type: int = FRAME_TYPE_DATA_03) -> Optional[BMSInfo]:
        """Read data from BMS (convenience method)."""
        await self.send_command(frame_type)
        # Response received via notification - wait for it
        await asyncio.sleep(1.0)
        return None

    async def start_monitoring(self, interval_ms: int = REALTIME_INTERVAL_MS):
        """Start continuous monitoring mode."""
        print(f"Starting monitoring (interval: {interval_ms}ms)...")
        print("Press Ctrl+C to stop")

        async def monitor_loop():
            while self.is_connected:
                try:
                    info = await self.read_data(FRAME_TYPE_DATA_03)
                    if info:
                        print(info)
                except Exception as e:
                    print(f"Monitor error: {e}")
                await asyncio.sleep(interval_ms / 1000.0)

        asyncio.create_task(monitor_loop())

    async def set_notification(self, enabled: bool = True):
        """Enable/disable notifications on data characteristic."""
        if enabled:
            await self.client.start_notify(
                JK_BMS_DATA_SERVICE_UUID,
                self._data_handler
            )
            print("Notifications enabled")
        else:
            await self.client.stop_notify(JK_BMS_DATA_SERVICE_UUID)
            print("Notifications disabled")

    def _data_handler(self, sender: int, data: bytes):
        """Handle incoming data from BMS."""
        if self._data_callback:
            self._data_callback(data)
        # Parse and display
        info = self.parse_response(data)
        if info:
            print(info)

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
        info = BMSInfo(raw_data=data, timestamp=time.time())

        if frame_type == FRAME_TYPE_DATA_03:
            # Real-time monitoring frame
            info = self._parse_data_03(data)
        elif frame_type == FRAME_TYPE_DATA_01:
            # Device information frame
            info = self._parse_data_01(data)
        elif frame_type == FRAME_TYPE_DATA_06:
            # Detailed logs
            info = self._parse_data_06(data)
        elif frame_type == FRAME_TYPE_DATA_96:
            # Extended data
            info = self._parse_data_96(data)

        return info

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
        num_temps = min(8, (len(data) - temp_start) // 1)
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
        if len(data) > status_offset + 4:
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
# CLI Commands
# ============================================================================


class JKBMSCli:
    """Command-line interface for JK BMS."""

    def __init__(self):
        self.service: Optional[JKBMSService] = None
        self.parser = JKProtocolParser()

    async def cmd_scan(self, args: argparse.Namespace):
        """Scan for BMS devices."""
        timeout = getattr(args, 'timeout', 5)
        scanner = JKBMSService.__new__(JKBMSService)
        scanner.client = None  # Not needed for scanning

        devices = await scanner.scan_devices(timeout)

        if not devices:
            print("No JK BMS devices found.")
            return

        print(f"\nFound {len(devices)} device(s):\n")
        print(f"{'MAC Address':<20} {'Name':<20} {'RSSI':>6}")
        print(f"{'─' * 46}")
        for device, adv in devices:
            name = device.name or "Unknown"
            rssi = adv.rssi if adv and adv.rssi else 0
            print(f"{device.address:<20} {name:<20} {rssi:>6}")

    async def cmd_connect(self, args: argparse.Namespace):
        """Connect to a BMS device."""
        address = args.address
        print(f"Connecting to {address}...")

        self.service = JKBMSService.__new__(JKBMSService)
        self.service.client = None

        connected = await self.service.connect(address)
        if not connected:
            print("Failed to connect.")
            return

        # Enable notifications
        await self.service.set_notification(True)

    async def cmd_read(self, args: argparse.Namespace):
        """Read data from BMS."""
        if not self.service or not self.service.is_connected:
            print("Not connected. Use 'connect' first.")
            return

        command = args.command.lower()

        if command == "info":
            # Read device info (DATA_01)
            await self.service.send_command(FRAME_TYPE_DATA_01)
            await asyncio.sleep(1.0)
        elif command == "status" or command == "realtime":
            # Read real-time data (DATA_03)
            await self.service.send_command(FRAME_TYPE_DATA_03)
            await asyncio.sleep(1.0)
        elif command == "logs":
            # Read detailed logs (DATA_06)
            await self.service.send_command(FRAME_TYPE_DATA_06)
            await asyncio.sleep(1.0)
        elif command == "extended":
            # Read extended data (DATA_96)
            await self.service.send_command(FRAME_TYPE_DATA_96)
            await asyncio.sleep(1.0)
        elif command == "all":
            # Read all data types
            for frame_type in [FRAME_TYPE_DATA_01, FRAME_TYPE_DATA_03,
                               FRAME_TYPE_DATA_06, FRAME_TYPE_DATA_96]:
                await self.service.send_command(frame_type)
                await asyncio.sleep(0.5)
        else:
            print(f"Unknown read command: {command}")
            print("Available: info, status, logs, extended, all")

    async def cmd_write(self, args: argparse.Namespace):
        """Write configuration to BMS."""
        if not self.service or not self.service.is_connected:
            print("Not connected. Use 'connect' first.")
            return

        command = args.command.lower()
        value = args.value

        # Build command payload based on command type
        payload = self.build_write_payload(command, value)
        if payload is None:
            print(f"Invalid command: {command}")
            return

        await self.service.send_command(0x10, payload)  # Write command type
        print(f"Sent write command: {command} = {value}")

    async def cmd_monitor(self, args: argparse.Namespace):
        """Start continuous monitoring."""
        if not self.service or not self.service.is_connected:
            print("Not connected. Use 'connect' first.")
            return

        interval = getattr(args, 'interval', 3000)  # Default 3s

        try:
            await self.service.start_monitoring(interval)
            # Keep running
            while self.service.is_connected:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")

    async def cmd_disconnect(self, args: argparse.Namespace):
        """Disconnect from BMS."""
        if self.service:
            await self.service.disconnect()
            self.service = None

    async def cmd_raw(self, args: argparse.Namespace):
        """Send raw hex data."""
        if not self.service or not self.service.is_connected:
            print("Not connected. Use 'connect' first.")
            return

        hex_data = args.data
        try:
            data = self.parser.hex_to_bytes(hex_data)
            await self.service.client.write_gatt_char(
                JK_BMS_DATA_SERVICE_UUID,
                data,
                response=False
            )
            print(f"Sent {len(data)} bytes: {hex_data}")
        except Exception as e:
            print(f"Error: {e}")

    async def cmd_info(self, args: argparse.Namespace):
        """Show device information."""
        print("JK BMS CLI - Battery Management System Control")
        print("=" * 60)
        print(f"\nBLE Service UUIDs:")
        print(f"  Data Service:  {JK_BMS_DATA_SERVICE_UUID}")
        print(f"  Service UUID:  {JK_BMS_SERVICE_UUID}")
        print(f"\nFrame Headers:")
        print(f"  Main Protocol: {self.parser.bytes_to_hex(FRAME_HEADER_MAIN, '')}")
        print(f"  Cloud Protocol:{self.parser.bytes_to_hex(FRAME_HEADER_CLOUD, '')}")
        print(f"\nFrame Types:")
        print(f"  0x01 - Device Information")
        print(f"  0x03 - Real-time Monitoring")
        print(f"  0x06 - Detailed Logs")
        print(f"  0x96 - Extended Data Export")
        print(f"\nConnection Settings:")
        print(f"  BLE MTU:           {BLE_MTU}")
        print(f"  Write Size:        {BLE_WRITE_SIZE} bytes")
        print(f"  Send Interval:     {SEND_INTERVAL_MS}ms")
        print(f"  Connect Timeout:   {CONNECT_TIMEOUT_S}s")
        print(f"\nCloud Server:")
        print(f"  Host: {CLOUD_SERVER}:{CLOUD_PORT}")
        print(f"  WebSocket: {WEBSOCKET_URL}")
        print(f"\nHeartbeat:")
        print(f"  Interval: {HEARTBEAT_INTERVAL_MS}ms")
        print(f"  Reconnect: {RECONNECT_INTERVAL_MS}ms")
        print(f"\nSupported Frame Formats:")
        print(f"  DATA_01: 164 bytes (device config)")
        print(f"  DATA_03: 164 bytes (real-time data)")
        print(f"  DATA_06: Variable (detailed logs)")
        print(f"  DATA_96: Variable (extended export)")

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def build_write_payload(self, command: str, value: str) -> Optional[bytes]:
        """Build write command payload."""
        # This is a simplified version - actual implementation would need
        # more detailed command mapping
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
        """Balance enable/disable payload."""
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x01, 0x01 if enabled else 0x00])

    def _payload_charge(self, value: str) -> bytes:
        """Charge MOS control payload."""
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x02, 0x01 if enabled else 0x00])

    def _payload_discharge(self, value: str) -> bytes:
        """Discharge MOS control payload."""
        enabled = value.lower() in ("on", "true", "1", "yes")
        return bytes([0x03, 0x01 if enabled else 0x00])

    def _payload_protection(self, value: str) -> bytes:
        """Protection settings payload."""
        # Simplified - would need actual parameter parsing
        return bytes([0x04, 0x00])

    def _payload_calibration(self, value: str) -> bytes:
        """Calibration data payload."""
        return bytes([0x05, 0x00])


# ============================================================================
# Main Entry Point
# ============================================================================


def create_argument_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="jk_bms_cli",
        description="JK BMS CLI - Control JK BMS devices via Bluetooth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s scan                          Scan for BMS devices
  %(prog)s connect AA:BB:CC:DD:EE:FF    Connect to device
  %(prog)s read info                     Read device information
  %(prog)s read status                   Read real-time status
  %(prog)s read logs                     Read detailed logs
  %(prog)s read all                      Read all data types
  %(prog)s write balance on              Enable balancing
  %(prog)s write charge on               Enable charging
  %(prog)s write discharge on            Enable discharging
  %(prog)s monitor                       Start continuous monitoring
  %(prog)s raw 55AAEB90030000            Send raw hex data
  %(prog)s disconnect                    Disconnect device
  %(prog)s info                          Show protocol info
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for BMS devices")
    scan_parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=5,
        help="Scan timeout in seconds (default: 5)"
    )

    # Connect command
    connect_parser = subparsers.add_parser("connect", help="Connect to BMS device")
    connect_parser.add_argument(
        "address",
        help="MAC address or UUID of the BMS device"
    )

    # Read command
    read_parser = subparsers.add_parser("read", help="Read data from BMS")
    read_parser.add_argument(
        "command",
        choices=["info", "status", "realtime", "logs", "extended", "all"],
        help="Data to read"
    )

    # Write command
    write_parser = subparsers.add_parser("write", help="Write configuration to BMS")
    write_parser.add_argument(
        "command",
        choices=["balance", "charge", "discharge", "protection", "calibration"],
        help="Setting to modify"
    )
    write_parser.add_argument(
        "value",
        help="Value to set (on/off, true/false, 1/0, yes/no)"
    )

    # Monitor command
    monitor_parser = subparsers.add_parser("monitor", help="Start continuous monitoring")
    monitor_parser.add_argument(
        "--interval", "-i",
        type=int,
        default=3000,
        help="Update interval in ms (default: 3000)"
    )

    # Raw command
    raw_parser = subparsers.add_parser("raw", help="Send raw hex data")
    raw_parser.add_argument(
        "data",
        help="Hex data to send (e.g., 55AAEB90030000)"
    )

    # Disconnect command
    subparsers.add_parser("disconnect", help="Disconnect from device")

    # Info command
    subparsers.add_parser("info", help="Show protocol information")

    return parser


async def main():
    """Main entry point."""
    parser = create_argument_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cli = JKBMSCli()

    try:
        if args.command == "scan":
            await cli.cmd_scan(args)
        elif args.command == "connect":
            await cli.cmd_connect(args)
        elif args.command == "read":
            await cli.cmd_read(args)
        elif args.command == "write":
            await cli.cmd_write(args)
        elif args.command == "monitor":
            await cli.cmd_monitor(args)
        elif args.command == "raw":
            await cli.cmd_raw(args)
        elif args.command == "disconnect":
            await cli.cmd_disconnect(args)
        elif args.command == "info":
            await cli.cmd_info(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if cli.service:
            await cli.service.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
