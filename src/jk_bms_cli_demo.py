#!/usr/bin/env python3
"""
JK BMS CLI - Demo/Test Mode

This script demonstrates the protocol parsing capabilities
without requiring actual BLE hardware.

Usage:
    python jk_bms_cli_demo.py
    python jk_bms_cli_demo.py parse <hex_data>
    python jk_bms_cli_demo.py frame <type>
"""

import sys
import time
import struct


class JKProtocolParser:
    """Parser for JK BMS protocol frames (demo version)."""

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
        return int.from_bytes(data[offset:offset+2], 'little')

    @staticmethod
    def parse_little_endian_i16(data: bytes, offset: int = 0) -> int:
        """Parse 16-bit signed integer (little-endian)."""
        value = int.from_bytes(data[offset:offset+2], 'little')
        return value - 0x10000 if value >= 0x8000 else value

    @staticmethod
    def parse_little_endian_u32(data: bytes, offset: int = 0) -> int:
        """Parse 32-bit unsigned integer (little-endian)."""
        return int.from_bytes(data[offset:offset+4], 'little')

    @staticmethod
    def parse_little_endian_f32(data: bytes, offset: int = 0) -> float:
        """Parse 32-bit float (little-endian, IEEE 754)."""
        import struct
        return struct.unpack_from('<f', data, offset)[0]

    @staticmethod
    def parse_ascii_string(data: bytes, start: int, length: int) -> str:
        """Parse ASCII string from bytes."""
        end = start + length
        raw = data[start:end]
        return raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()

    @staticmethod
    def crc8(data: bytes) -> int:
        """Calculate CRC8 checksum."""
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
    def parse_cell_voltages(data: bytes, start: int, count: int) -> list:
        """Parse cell voltage data (2 bytes per cell, little-endian, mV)."""
        cells = []
        for i in range(count):
            offset = start + i * 2
            if offset + 2 > len(data):
                break
            voltage_mv = JKProtocolParser.parse_little_endian_u16(data, offset)
            cells.append({
                'index': i + 1,
                'voltage_mv': voltage_mv,
                'voltage_v': voltage_mv / 1000.0
            })
        return cells

    @staticmethod
    def parse_temperatures(data: bytes, start: int, count: int) -> list:
        """Parse temperature data (1 byte per sensor, Celsius, offset 40)."""
        temps = []
        for i in range(count):
            offset = start + i
            if offset >= len(data):
                break
            temp = data[offset] - 40  # Offset by 40
            temps.append({
                'sensor_id': i + 1,
                'temperature': temp
            })
        return temps

    @staticmethod
    def parse_data_03(data: bytes) -> dict:
        """Parse DATA_03 (real-time monitoring) frame."""
        result = {}

        if len(data) < 10:
            return {'error': 'Data too short'}

        # Parse header
        header = data[:4]
        frame_type = data[4]
        length = JKProtocolParser.parse_little_endian_u16(data, 5)

        result['header'] = JKProtocolParser.bytes_to_hex(header, '')
        result['frame_type'] = f"0x{frame_type:02X}"
        result['length'] = length

        # Parse fields (after header[5]+length[2]+cell_count[2] = offset 9)
        result['cell_count'] = JKProtocolParser.parse_little_endian_u16(data, 7)
        result['pack_voltage'] = JKProtocolParser.parse_little_endian_f32(data, 9)
        result['pack_current'] = JKProtocolParser.parse_little_endian_f32(data, 13)
        result['soc'] = JKProtocolParser.parse_little_endian_u16(data, 17)
        result['soh'] = JKProtocolParser.parse_little_endian_u16(data, 19)
        result['cycle_count'] = JKProtocolParser.parse_little_endian_u16(data, 21)

        # Parse cell voltages
        cell_start = 25
        num_cells = min(result['cell_count'], 32)
        result['cells'] = JKProtocolParser.parse_cell_voltages(data, cell_start, num_cells)

        if result['cells']:
            result['max_cell_voltage'] = max(c['voltage_mv'] for c in result['cells'])
            result['min_cell_voltage'] = min(c['voltage_mv'] for c in result['cells'])
            result['max_cell_diff'] = result['max_cell_voltage'] - result['min_cell_voltage']

        # Parse temperatures
        temp_start = cell_start + num_cells * 2
        num_temps = min(8, max(0, len(data) - temp_start))
        result['temperatures'] = JKProtocolParser.parse_temperatures(data, temp_start, num_temps)

        if result['temperatures']:
            result['max_temp'] = max(t['temperature'] for t in result['temperatures'])
            result['min_temp'] = min(t['temperature'] for t in result['temperatures'])

        # Parse alarm mask
        alarm_offset = temp_start + num_temps
        if len(data) > alarm_offset + 4:
            result['alarm_mask'] = JKProtocolParser.parse_little_endian_u32(data, alarm_offset)

        # Parse status flags
        status_offset = alarm_offset + 4
        if len(data) > status_offset + 1:
            status_byte = data[status_offset]
            result['status'] = {
                'charge_mos': bool(status_byte & 0x01),
                'discharge_mos': bool(status_byte & 0x02),
                'heating': bool(status_byte & 0x04),
                'balance': bool(status_byte & 0x08),
            }

        return result

    @staticmethod
    def generate_frame(frame_type: int, cell_count: int = 16,
                       voltage: float = 52.4, current: float = 10.5,
                       soc: int = 85) -> bytes:
        """Generate a sample DATA_03 frame for testing."""
        # Header
        frame = bytearray([0x55, 0xAA, 0xEB, 0x90, frame_type])

        # Length (2 bytes LE)
        length = 154  # Typical DATA_03 length
        frame.extend(length.to_bytes(2, 'little'))

        # Cell count (2 bytes LE)
        frame.extend(cell_count.to_bytes(2, 'little'))

        # Pack voltage (4 bytes LE float)
        frame.extend(struct.pack('<f', voltage))

        # Pack current (4 bytes LE float)
        frame.extend(struct.pack('<f', current))

        # SOC (2 bytes LE)
        frame.extend(soc.to_bytes(2, 'little'))

        # SOH (2 bytes LE)
        frame.extend([98, 0])  # 98%

        # Cycle count (2 bytes LE)
        frame.extend([150, 0])  # 150 cycles

        # Cell voltages (2 bytes LE each, mV)
        base_voltage = 3300  # 3.3V per cell
        for i in range(cell_count):
            voltage_mv = base_voltage + (i % 5) * 10  # Slight variation
            frame.extend(voltage_mv.to_bytes(2, 'little'))

        # Temperatures (1 byte each, +40 offset)
        for i in range(8):
            frame.extend([(25 + i) + 40])  # 25-32°C

        # Alarm mask (4 bytes LE)
        frame.extend([0, 0, 0, 0])  # No alarms

        # Status flags (1 byte)
        frame.extend([0x0F])  # All enabled

        # Pad to typical length
        while len(frame) < 164:
            frame.extend([0])

        # Add checksum (2 bytes LE)
        checksum = 0
        for b in frame:
            checksum ^= b
        frame.extend(checksum.to_bytes(2, 'little'))

        return bytes(frame)


def print_parsed_data(result: dict):
    """Print parsed data in formatted way."""
    print("\n" + "=" * 60)
    print("  JK BMS Data Frame Analysis")
    print("=" * 60)

    print(f"\n  Header:       {result.get('header', 'N/A')}")
    print(f"  Frame Type:   {result.get('frame_type', 'N/A')}")
    print(f"  Length:       {result.get('length', 'N/A')}")

    print(f"\n  Cell Count:   {result.get('cell_count', 0)}")
    print(f"  Pack Voltage: {result.get('pack_voltage', 0):.2f} V")
    print(f"  Pack Current: {result.get('pack_current', 0):.2f} A")
    print(f"  SOC:          {result.get('soc', 0)}%")
    print(f"  SOH:          {result.get('soh', 0)}%")
    print(f"  Cycle Count:  {result.get('cycle_count', 0)}")

    if 'cells' in result and result['cells']:
        print(f"\n  Cell Voltages:")
        print(f"  {'Cell':>6} {'Voltage (mV)':>12} {'Voltage (V)':>12}")
        print(f"  {'─' * 32}")
        for cell in result['cells']:
            print(f"  {cell['index']:>6} {cell['voltage_mv']:>12.1f} {cell['voltage_v']:>12.3f}")

        print(f"\n  Max Cell Voltage: {result.get('max_cell_voltage', 0):.1f} mV")
        print(f"  Min Cell Voltage: {result.get('min_cell_voltage', 0):.1f} mV")
        print(f"  Max Cell Diff:    {result.get('max_cell_diff', 0):.1f} mV")

    if 'temperatures' in result and result['temperatures']:
        print(f"\n  Temperatures:")
        print(f"  {'Sensor':>8} {'Temperature (°C)':>18}")
        print(f"  {'─' * 28}")
        for temp in result['temperatures']:
            print(f"  {temp['sensor_id']:>8} {temp['temperature']:>18.1f}")

        print(f"\n  Max Temp: {result.get('max_temp', 0):.1f}°C")
        print(f"  Min Temp: {result.get('min_temp', 0):.1f}°C")

    if 'status' in result:
        status = result['status']
        status_str = []
        if status.get('charge_mos'):
            status_str.append("CHARGE_ON")
        if status.get('discharge_mos'):
            status_str.append("DISCHARGE_ON")
        if status.get('heating'):
            status_str.append("HEATING")
        if status.get('balance'):
            status_str.append("BALANCING")
        print(f"  Status: {', '.join(status_str) if status_str else 'IDLE'}")

    if 'alarm_mask' in result:
        if result['alarm_mask']:
            print(f"  ⚠ ALARMS: 0x{result['alarm_mask']:08X}")
        else:
            print(f"  ✓ No alarms")

    print("=" * 60)


def main():
    """Main entry point."""
    parser = JKProtocolParser()

    if len(sys.argv) < 2:
        # Demo mode - generate and parse a sample frame
        print("JK BMS CLI - Demo Mode")
        print("=" * 60)
        print("\nGenerating sample DATA_03 frame...")

        # Generate sample frame
        sample_frame = parser.generate_frame(
            frame_type=0x03,
            cell_count=16,
            voltage=52.4,
            current=10.5,
            soc=85
        )

        print(f"\nGenerated frame ({len(sample_frame)} bytes):")
        print(f"  {parser.bytes_to_hex(sample_frame)}")

        # Parse the frame
        print("\nParsing frame...")
        result = parser.parse_data_03(sample_frame)
        print_parsed_data(result)

        print("\n\nGenerating sample DATA_01 frame...")
        sample_frame_01 = parser.generate_frame(
            frame_type=0x01,
            cell_count=16,
            voltage=52.4,
            current=10.5,
            soc=85
        )

        print(f"\nGenerated frame ({len(sample_frame_01)} bytes):")
        print(f"  {parser.bytes_to_hex(sample_frame_01)}")

        print("\nDone.")
        return

    command = sys.argv[1].lower()

    if command == "parse":
        if len(sys.argv) < 3:
            print("Usage: python jk_bms_cli_demo.py parse <hex_data>")
            return

        hex_data = sys.argv[2]
        try:
            data = parser.hex_to_bytes(hex_data)
            print(f"Parsing {len(data)} bytes...")
            result = parser.parse_data_03(data)
            print_parsed_data(result)
        except Exception as e:
            print(f"Error: {e}")

    elif command == "frame":
        if len(sys.argv) < 3:
            print("Usage: python jk_bms_cli_demo.py frame <type>")
            return

        frame_type = int(sys.argv[2], 16)
        try:
            frame = parser.generate_frame(frame_type)
            print(f"Generated frame (type=0x{frame_type:02X}, {len(frame)} bytes):")
            print(f"  {parser.bytes_to_hex(frame)}")
        except Exception as e:
            print(f"Error: {e}")

    elif command == "help":
        print("JK BMS CLI - Demo Mode")
        print("=" * 60)
        print("\nUsage:")
        print("  python jk_bms_cli_demo.py              # Demo mode")
        print("  python jk_bms_cli_demo.py parse <hex>  # Parse hex data")
        print("  python jk_bms_cli_demo.py frame <type> # Generate frame")
        print("  python jk_bms_cli_demo.py help         # Show this help")
    else:
        print(f"Unknown command: {command}")
        print("Use 'help' for usage information")


if __name__ == "__main__":
    main()
