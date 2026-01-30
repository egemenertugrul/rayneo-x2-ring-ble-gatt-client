# RayNeo X2 Smart Ring – BLE client

Headless BLE client for the **RayNeo X2 Smart Ring**. Connects over vendor GATT; also supports standard HID over GATT (HOGP) for other devices.

- **Ring-only use**: Does not require RayNeo X2 glasses; you can use the ring on its own.
- **Headless**: No GUI; run from the command line or in scripts/automation.
- **Uses GATT** — Discovers services/characteristics, enables notifications, and prints incoming data.

## Requirements

- Python 3.8+
- [Bleak](https://github.com/hbldh/bleak) (`pip install bleak`)
- Bluetooth adapter (built-in or USB) with BLE support

## Usage

pip install bleak

# Connect to default device (RayNeo X2 ring at B0:B3:53:EB:40:8D)
python hid_gatt_client.py

# Connect by address
python hid_gatt_client.py AA:BB:CC:DD:EE:FF

# Connect by device name
python hid_gatt_client.py "Your Ring Name"
