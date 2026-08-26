
# JK_BMS_Monitor

JK (Ji Kong) BMS Monitor

## JK BMS CLI

A Python command-line interface for controlling JK BMS (Battery Management System) devices via Bluetooth Low Energy (BLE).

### Supported Devices

- JK-B1A8S20P (4/8S LiFePo4 Battery BMS) - test hardware
- JK-BP1A16S1 (16S Lithium Battery BMS)
- JK-BMS-Q36 (36S Battery Management System)
- JK-BP1A20S, JK-BP1A24S, JK-BP1A32S, JK-BP1A36S, JK-BP1A40S

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Or manually
pip install bleak bleak-retry-connector
```

#### Linux/Ubuntu Setup

```bash
# Install Bluetooth development libraries
sudo apt-get install libbluetooth-dev

# Install bleak
pip install bleak
```

#### macOS Setup

```bash
# Install bleak with macOS backend
pip install bleak
```

#### Windows Setup

```bash
# Install bleak with Windows backend
pip install bleak
```

### Usage

#### Basic Commands

```bash
# Scan for nearby BMS devices
python jk_bms_cli.py scan

# Test the connection (connect, report device identity, disconnect)
python jk_bms_cli.py connect AA:BB:CC:DD:EE:FF

# Read device information
python jk_bms_cli.py read AA:BB:CC:DD:EE:FF info

# Read real-time status
python jk_bms_cli.py read AA:BB:CC:DD:EE:FF status

# Read detailed logs
python jk_bms_cli.py read AA:BB:CC:DD:EE:FF logs

# Read all data
python jk_bms_cli.py read AA:BB:CC:DD:EE:FF all

# Enable balancing
python jk_bms_cli.py write AA:BB:CC:DD:EE:FF balance on

# Enable charging
python jk_bms_cli.py write AA:BB:CC:DD:EE:FF charge on

# Enable discharging
python jk_bms_cli.py write AA:BB:CC:DD:EE:FF discharge on

# Start continuous monitoring (3 second intervals, 15s duration)
python jk_bms_cli.py monitor AA:BB:CC:DD:EE:FF

# Start monitoring with custom interval (5 seconds) and duration (30s)
python jk_bms_cli.py monitor AA:BB:CC:DD:EE:FF --interval 5000 --duration 30

# Send raw hex data
python jk_bms_cli.py raw AA:BB:CC:DD:EE:FF 55AAEB90030000

# Show protocol information
python jk_bms_cli.py info
```

> **Note:** Each command that talks to a device is self-contained — it connects,
> performs the operation, and disconnects within a single run. A BLE connection
> cannot persist across separate CLI invocations, so there is no separate
> `connect`/`disconnect` session to manage; just pass the device address to the
> command you want to run.

#### Command Reference

| Command | Description | Arguments |
|---------|-------------|-----------|
| `scan` | Scan for BMS devices | `--timeout` - Scan duration (default: 5s) |
| `connect` | Test connection to BMS device | `address` - MAC address |
| `read` | Read data from BMS | `address`, then `info`/`status`/`realtime`/`logs`/`extended`/`all`, `--wait` |
| `write` | Write configuration | `address`, then `balance`/`charge`/`discharge`/`protection`/`calibration`, `value` |
| `monitor` | Continuous monitoring | `address`, `--interval` (ms), `--duration` (s) |
| `raw` | Send raw hex data | `address`, `data` - Hex string |
| `info` | Show protocol info | None |

All device commands also accept `--attempts` (default 5) and `--retry-wait` (default 8s)
to control connection retry behaviour.

### Protocol Details

#### BLE Services

- **Data Service:** `0000ffe1-0000-1000-8000-00805f9b34fb`
- **Service UUID:** `0000ffe0-0000-1000-00805f9b34fb`

#### Frame Types

| Type | Hex | Description |
|------|-----|-------------|
| DATA_01 | `01` | Device information/configuration |
| DATA_03 | `03` | Real-time monitoring data |
| DATA_06 | `06` | Detailed logs |
| DATA_96 | `0x96` | Extended data export |

#### Frame Structure

```
+--------+--------+--------+--------+--------+--------+
| 55 AA  | EB 90  | Type   | Length | ...    | ...    |
+--------+--------+--------+--------+--------+--------+
```

### Output Format

The CLI displays BMS information in a formatted table:

```
============================================================
  JK BMS Device Information
============================================================
  Model:      JK-BP1A16S1
  Serial:     JKxxxxxxxx
  Firmware:   V1.0.0
  
  Cell Count: 16
  Pack Voltage:  52.40 V
  Pack Current:  10.50 A
  SOC:           85%
  SOH:           98%
  Cycle Count:   150
  Rated Capacity:100.0 Ah
  Battery Type:  LFP
  
  Cell Voltages:
    Cell Voltage (mV) Voltage (V)
    ────────────────────────────
       1         3300.0      3.300
       2         3301.0      3.301
       ...
  
  Status: CHARGE_ON, DISCHARGE_ON, BALANCING
  ✓ No alarms
============================================================
```

### Troubleshooting

#### Bluetooth Permission Issues (Linux)

```bash
# Add user to bluetooth group
sudo usermod -aG bluetooth $USER

# Or use sudo
sudo python jk_bms_cli.py scan
```

#### Connection Issues

1. Ensure the BMS device is powered on
2. Make sure the device is in pairing mode
3. Check that Bluetooth is enabled: `sudo hciconfig hci0` (should be `UP RUNNING`)
   and `sudo rfkill list` (Bluetooth should not be `Soft blocked: yes`)
4. Try increasing scan timeout: `python jk_bms_cli.py scan --timeout 10`

The JK BMS needs a few seconds to reset its BLE stack after a session, so a single
connect attempt can time out. The CLI retries automatically (5 attempts by default,
with a growing backoff). If connections keep failing, wait ~20-30s and try again,
or increase attempts: `python jk_bms_cli.py read <addr> status --attempts 8`

#### Data Not Showing

1. Wait a moment for the BMS to respond
2. Try reading specific data types: `python jk_bms_cli.py read status`
3. Check raw data: `python jk_bms_cli.py raw 55AAEB90030000`

### Requirements

- Python 3.8+
- bleak library
- Bluetooth adapter (BLE 4.0+)

### License

MIT License
