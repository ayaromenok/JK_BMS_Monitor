# JK BMS Protocol Specification

This document describes the communication protocols used by JK BMS (Battery Management System) devices.

**Manufacturer:** JK Tech (Jiangsu Kejing)  
**Device Family:** JK-BMS (e.g., JK-BMS-A16S1, JK-BMS-Q36 variants)

---

## Table of Contents

1. [Overview](#overview)
2. [BLE (Bluetooth Low Energy) Protocol](#ble-protocol)
3. [Cloud/Server Protocol](#cloudserver-protocol)
4. [WebSocket Protocol](#websocket-protocol)
5. [Data Frame Formats](#data-frame-formats)
6. [Command Set](#command-set)
7. [Encryption & Security](#encryption--security)

---

## Overview

JK BMS devices support three communication methods:

| Method | Protocol | Purpose |
|--------|----------|---------|
| BLE (Local) | Custom binary protocol | Direct phone-to-BMS communication |
| TCP Socket (Cloud) | Encrypted binary protocol | Phone-to-server communication |
| WebSocket | JSON-like text protocol | Real-time data streaming |

---

## BLE Protocol

### Service UUIDs

```
Data Service:  0000ffe1-0000-1000-8000-00805f9b34fb
Service UUID:  0000ffe0-0000-1000-8000-00805f9b34fb
```

### Advertisement Packets

The BMS broadcasts two types of advertisement data packets:

#### Frame Type 0x01 - Battery Status Frame

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 6 bytes | MAC Address | Device MAC (little-endian) |
| 6 | 1 byte | Frame ID | Always 0x01 |
| 7 | 4 bytes | Battery Voltage | Float, unit: V |
| 11 | 4 bytes | Battery Current | Float, unit: A (positive=charging, negative=discharging) |
| 15 | 2 bytes | SOC | Percentage, 0-100 |
| 17 | 4 bytes | Max Temperature | Celsius |
| 21 | 4 bytes | Min Temperature | Celsius |
| 25 | 2 bytes | Max Cell Voltage | Millivolts |
| 27 | 2 bytes | Min Cell Voltage | Millivolts |
| 29 | 4 bytes | Alarm Mask | Bitmask of alarms |
| 33 | 1 byte | Status Flags | Bit 0: Charge, Bit 1: Discharge, Bit 2: Heat, Bit 3: Balance |
| 34 | 2 bytes | Cell Voltage Difference | Millivolts |
| 36 | 1 byte | CRC8 | Checksum |

#### Frame Type 0x02 - Device Info Frame

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 6 bytes | MAC Address | Device MAC (little-endian) |
| 6 | 1 byte | Frame ID | Always 0x02 |
| 7 | 4 bytes | Rated Capacity | Float, unit: Ah |
| 11 | 2 bytes | Cycle Count | Number of charge cycles |
| 13 | 2 bytes | SOH | State of Health, percentage |
| 15 | 4 bytes | Max Discharge Current | Float, unit: A |
| 19 | 4 bytes | Max Charge Current | Float, unit: A |
| 23 | 2 bytes | Device Address | Hex address |
| 25 | 4 bytes | Cell Series Count | Number of series cells |
| 29 | 4 bytes | Battery Type | 0=LFP, 1=NMC, 2=LTO |
| 33 | 1 byte | Charge Enable | Boolean |
| 34 | 1 byte | Discharge Enable | Boolean |
| 35 | 1 byte | Balance Enable | Boolean |
| 36 | 4 bytes | Reserved | - |
| 40 | 1 byte | CRC8 | Checksum |

### Data Frame Format (Main Protocol)

All main protocol frames use header `55AAEB90` followed by a frame type byte.

#### Frame Header Structure

```
+--------+--------+--------+--------+--------+--------+
| 55 AA  | EB 90  | Type   | Length | ...    | ...    |
+--------+--------+--------+--------+--------+--------+
```

#### Frame Types

| Type | Hex | Description |
|------|-----|-------------|
| DATA_01 | `01` | Device information / configuration |
| DATA_03 | `03` | Real-time monitoring data |
| DATA_06 | `06` | Detailed logs |
| DATA_96 | `96` | Extended data export |

### Device Information Frame (Type 0x01)

**Structure (164 bytes total):**

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0-4 | 5 bytes | Header | `55 AA EB 90 01` |
| 5-6 | 2 bytes | Length | Data length |
| 7-10 | 4 bytes | Cell Count | Number of parallel cells |
| 11-14 | 4 bytes | Pack Voltage | Float, V |
| 15-18 | 4 bytes | Pack Current | Float, A |
| 19-20 | 2 bytes | SOC | Percentage |
| 21-22 | 2 bytes | SOH | Percentage |
| 23-24 | 2 bytes | Cycle Count | - |
| 25-38 | 14 bytes | Cell Voltages | 7 cells × 2 bytes (mV each) |
| 39-42 | 4 bytes | Temperature 1-4 | 4 × 1 byte Celsius |
| 43-46 | 4 bytes | Temperature 5-8 | 4 × 1 byte Celsius |
| 47-50 | 4 bytes | Max Cell Voltage | mV |
| 51-54 | 4 bytes | Min Cell Voltage | mV |
| 55-58 | 4 bytes | Alarm Mask | Bitmask |
| 59-62 | 4 bytes | Status Flags | Charge/Discharge/Heat/Balance |
| 63-78 | 16 bytes | Reserved | - |
| 79-94 | 16 bytes | Device Info | Model, serial, etc. |
| 95-110 | 16 bytes | Firmware Version | - |
| 111-126 | 16 bytes | BMS Settings | Configuration parameters |
| 127-142 | 16 bytes | Protection Values | Over/under voltage thresholds |
| 143-158 | 16 bytes | Reserved | - |
| 159-162 | 4 bytes | CRC | Checksum |

### Real-time Monitoring Frame (Type 0x03)

**Structure (164 bytes total):**

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0-4 | 5 bytes | Header | `55 AA EB 90 03` |
| 5-6 | 2 bytes | Length | Data length |
| 7-10 | 4 bytes | Cell Count | Number of cells |
| 11-14 | 4 bytes | Pack Voltage | Float, V |
| 15-18 | 4 bytes | Pack Current | Float, A |
| 19-20 | 2 bytes | SOC | Percentage |
| 21-22 | 2 bytes | SOH | Percentage |
| 23-24 | 2 bytes | Cycle Count | - |
| 25-38 | 14 bytes | Cell Voltages | Cell-by-cell voltage (mV) |
| 39-42 | 4 bytes | Temperature | Multiple sensors |
| 43-46 | 4 bytes | Max/Min Voltage | Cell extremes |
| 47-50 | 4 bytes | Alarm Status | Active alarms |
| 51-54 | 4 bytes | Control Status | Charge/discharge state |
| 55-58 | 4 bytes | Balance Status | Active balances |
| 59-62 | 4 bytes | Reserved | - |
| 63-78 | 16 bytes | Device Data | Serial, model |
| 79-94 | 16 bytes | Settings | Configuration |
| 95-110 | 16 bytes | Protection | Thresholds |
| 111-126 | 16 bytes | Reserved | - |
| 127-142 | 16 bytes | Extended | Additional data |
| 143-158 | 16 bytes | Reserved | - |
| 159-162 | 4 bytes | CRC | Checksum |

### BLE Communication Details

- **MTU:** 23 bytes (default), can be increased
- **Write Type:** Write Without Response (for speed)
- **Split Size:** 20 bytes per BLE packet (MTU - 3 header)
- **Send Interval:** 20ms between packets
- **Connect Timeout:** 20000ms

### BLE Message Format (Phone → BMS)

```
+--------+--------+--------+--------+--------+--------+--------+
| 55 AA  | 7E E7   | SubCmd | Length | SN     | ...    | ...    |
+--------+--------+--------+--------+--------+--------+--------+
```

| Field | Size | Description |
|-------|------|-------------|
| Header | 4 bytes | `55 AA 7E E7` |
| SubCommand | 1 byte | Command type |
| Length | 2 bytes | Payload length (little-endian) |
| Serial Number | 16 bytes | Request identifier |
| Payload | Variable | Command data |
| Checksum | 2 bytes | CRC calculation |

---

## Cloud/Server Protocol

### Server Connection

```
Host: iot.jk-bms.com
Port: 8091
Protocol: TCP Socket
```

### Connection Flow

1. Connect to server
2. Send handshake message
3. Receive acknowledgment
4. Start periodic heartbeat (30s interval)
5. Receive real-time data updates

### Heartbeat

- **Interval:** 30 seconds (configurable)
- **Message:** Simple binary ping
- **Response:** Binary pong

### Message Format

All messages use encrypted binary format:

```
+--------+--------+--------+--------+--------+--------+
| 7E 81  | AA 55   | SN     | Cmd    | Length | ...    |
+--------+--------+--------+--------+--------+--------+
```

| Field | Size | Description |
|-------|------|-------------|
| Magic | 4 bytes | `7E 81 AA 55` |
| Serial Number | 16 bytes | Request ID |
| Command | 1 byte | Message type |
| Length | 2 bytes | Data length |
| Data | Variable | Payload |
| Checksum | Variable | Encryption-based |

### Message Types

| Cmd | Direction | Description |
|-----|-----------|-------------|
| 0x01 | U→S | Device connection |
| 0x02 | S→U | Connection success |
| 0x03 | U→S | Upload real-time data |
| 0x04 | S→U | Configuration request |
| 0x05 | U→S | Configuration response |
| 0x06 | S→U | Firmware update |
| 0x07 | U→S | Firmware ack |
| 0x08 | S→U | Set GPS interval |
| 0x0A | U→S | GPS location |
| 0x0B | U→S | Cell count update |
| 0x10 | S→U | Switch data request |
| 0x11 | S→U | Settings request |
| 0x16 | S→U | Device info request |
| 0x1A | S→U | Control enable request |
| 0x20 | S→U | CFG file request |
| 0x2C | S→U | Real-time data request |
| 0x30 | S→U | Detailed log request |
| 0xA0 | S→U | GPS location update |
| 0xA2 | S→U | Connection success |

---

## WebSocket Protocol

### Server Connection

```
URL: ws://192.168.108.15:8080
Protocol: WebSocket
```

### Heartbeat

- **Message:** `"ping"` (text)
- **Response:** `"pong"` (text)
- **Interval:** 30 seconds
- **Timeout:** 3 failed heartbeats = reconnect

### Message Format

JSON-like text protocol:

```json
{
  "type": "message_type",
  "data": {...},
  "timestamp": 1234567890
}
```

### Message Types

| Type | Direction | Description |
|------|-----------|-------------|
| `"ping"` | U→S | Heartbeat |
| `"pong"` | S→U | Heartbeat response |
| `"device_data"` | S→U | Real-time BMS data |
| `"config"` | S→U | Configuration update |
| `"alarm"` | S→U | Alarm notification |
| `"log"` | S→U | System log entry |

---

## Data Frame Formats

### Device Identification Frame

```
55 AA EB 90 03 5B 4A 4B 2D 50 42 31 41 31 36 53
```

Fields:
- `5B` - Device type identifier
- `4A 4B 2D 50 42 31 41 31 36 53` - "JK-BP1A16S" (model string)

### Configuration Frame Structure

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0-4 | 5 | Header | `55 AA EB 90 01/02/03` |
| 5-6 | 2 | Frame ID | Frame sequence |
| 7-8 | 2 | Cell Count | Number of cells |
| 9-12 | 4 | Pack Voltage | Float V |
| 13-16 | 4 | Pack Current | Float A |
| 17-18 | 2 | SOC | Percentage |
| 19-20 | 2 | SOH | Percentage |
| 21-22 | 2 | Cycle Count | - |
| 23-24 | 2 | Max Temp | Celsius |
| 25-26 | 2 | Min Temp | Celsius |
| 27-28 | 2 | Max Cell V | mV |
| 29-30 | 2 | Min Cell V | mV |
| 31-34 | 4 | Alarm Mask | Bitmask |
| 35-38 | 4 | Status | Flags |
| 39-42 | 4 | Reserved | - |
| 43-46 | 4 | CRC | Checksum |

### Cell Voltage Data

Each cell voltage is stored as 2 bytes (little-endian uint16):
- Unit: millivolts (mV)
- Range: 0 - 65535 mV (0 - 65.535 V)

### Temperature Data

- Unit: Celsius (°C)
- Format: Signed 16-bit integer
- Range: -40°C to +125°C

### Alarm Bitmask

| Bit | Name | Description |
|-----|------|-------------|
| 0 | Over Voltage | Cell over voltage |
| 1 | Under Voltage | Cell under voltage |
| 2 | Over Temperature | High temperature |
| 3 | Under Temperature | Low temperature |
| 4 | Over Current Charge | Charge current limit |
| 5 | Over Current Discharge | Discharge current limit |
| 6 | Short Circuit | Short circuit detected |
| 7 | Balance Failure | Balance circuit fault |
| 8 | MOS Over Temperature | MOSFET temp fault |
| 9 | Communication Fault | Comm error |
| 10 | ADC Fault | ADC error |
| 11 | EEPROM Error | Memory fault |

### Status Flags

| Bit | Name | Description |
|-----|------|-------------|
| 0 | Charge MOS | Charging MOSFET state |
| 1 | Discharge MOS | Discharging MOSFET state |
| 2 | Heating | Heating circuit active |
| 3 | Balance | Balancing active |
| 4 | Dry Contact 1 | External input 1 |
| 5 | Dry Contact 2 | External input 2 |

---

## Command Set

### Read Commands

| Command | Description | Response |
|---------|-------------|----------|
| `0x01` | Read device info | DATA_01 frame |
| `0x03` | Read real-time data | DATA_03 frame |
| `0x06` | Read detailed logs | DATA_06 frame |
| `0x96` | Read extended data | DATA_96 frame |

### Write Commands

| Command | Description | Parameters |
|---------|-------------|------------|
| `0x10` | Write settings | Configuration block |
| `0x11` | Write protection values | Thresholds |
| `0x12` | Write calibration | Calibration data |
| `0x16` | Read device config | - |
| `0x1A` | Write control enable | Enable bits |
| `0x20` | Request CFG file | File path |
| `0x2C` | Request real-time data | Interval |
| `0x30` | Request detailed log | Log type |

### Special Commands

| Command | Description |
|---------|-------------|
| `0xFF` | Factory reset |
| `0xFE` | Restart BMS |
| `0xFD` | Clear historical data |
| `0xFC` | Update BLE name |
| `0xFB` | Change password |
| `0xFA` | Pair device |

---

## Encryption & Security

### Serial Crypt Algorithm

The cloud protocol uses a custom XOR-based encryption:

```
encrypted[i] = data[i] XOR key[i % key_length]
```

Key derivation:
1. Take the Serial Number (16 bytes)
2. Use as XOR key for payload encryption
3. Last 2 bytes of encrypted data = checksum

### BLE Security

- **Pairing:** Required before communication
- **Password:** Device-specific, stored in app preferences
- **MAC Address Binding:** Device MAC used for authentication

### Message Authentication

All frames include:
- CRC8 checksum for BLE frames
- Custom checksum (2 bytes) for protocol frames
- Serial number for request/response matching

---

## Supported Device Models

Based on the source code analysis:

| Model | Series | Cells |
|-------|--------|-------|
| JK-BP1A16S1 | BP Series | 16S |
| JK-BMS-Q36 | Q36 Series | 36S |
| JK-BP1A20S | BP Series | 20S |
| JK-BP1A24S | BP Series | 24S |
| JK-BP1A32S | BP Series | 32S |
| JK-BP1A36S | BP Series | 36S |
| JK-BP1A40S | BP Series | 40S |

---

## Appendix

### Hex Utility Functions

Common operations used in protocol:

```
hexToBytes(hex: String) → Byte[]    // Convert hex string to bytes
bytesToHex(bytes: Byte[]) → String  // Convert bytes to hex string
parse(data: Byte[], start: Int, end: Int) → String  // Extract hex substring
hexTo16(bytes: Byte[], start: Int, end: Int) → Int  // Parse 16-bit LE
hexTo32(bytes: Byte[], start: Int, end: Int) → Int  // Parse 32-bit LE
serialCrypt(data: String, key: String) → String  // Encrypt/decrypt
```

### BLE GATT Characteristics

| UUID | Type | Properties |
|------|------|------------|
| `0000ffe1-...` | Data | Write, Notify |
| `0000ffe0-...` | Service | Read |

### Data Endianness

- **Multi-byte integers:** Little-endian
- **Float values:** IEEE 754, little-endian
- **String data:** ASCII, null-padded

---

*Document generated from Android decompiled source code analysis*  
*Last updated: 2024*
