# RayNeo X2 Smart Ring – BLE client

<p align="center">
  <img width="165" height="165" alt="ring"
       src="https://github.com/user-attachments/assets/1549785d-6e3c-4950-83c1-308bdb880e8e" />
</p>

Headless BLE client for the **RayNeo X2 Smart Ring**. Connects over vendor GATT; also supports standard HID over GATT (HOGP) for other devices.

- **Ring-only use**: Does not require RayNeo X2 glasses; you can use the ring on its own.
- **Headless**: No GUI; run from the command line or in scripts/automation.
- **Uses GATT**: Discovers services/characteristics, enables notifications, and prints incoming data.
- **OSCQuery**: Optional script exposes ring data (X, Y, press) via OSCQuery + WebSocket so clients (e.g. Chataigne) get live updates without polling.

## Requirements

- Python 3.8+
- [Bleak](https://github.com/hbldh/bleak) (`pip install bleak`)
- Bluetooth adapter (built-in or USB) with BLE support

## Usage

### BLE only

```bash
pip install bleak

# Connect to default device (RayNeo X2 ring at B0:B3:53:EB:40:8D)
python main.py

# Connect by address
python main.py AA:BB:CC:DD:EE:FF

# Connect by device name
python main.py "Your Ring Name"
```

<p align="center">
  <img width="240" alt="ring0"
       src="https://github.com/user-attachments/assets/928e808d-7974-4653-b82d-0de9f5675348" />
  <img width="240" alt="ring1"
       src="https://github.com/user-attachments/assets/ba9695bf-35bc-4eec-a2a7-f5facb6e526d" />
</p>

### With OSCQuery (live values in Chataigne, etc.)

```bash
pip install -r requirements.txt

python run_ring_oscquery.py              # default device
python run_ring_oscquery.py AA:BB:CC:DD:EE:FF
python run_ring_oscquery.py "Your Ring Name"
```

OSCQuery server runs at `http://127.0.0.1:9020`; nodes: `/ring/X`, `/ring/Y`, `/ring/press`. Connect via OSCQuery in your host app to get live updates over WebSocket.
