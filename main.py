#!/usr/bin/env python3
"""
HID over GATT (HOGP) client using Bleak.

Replicates the Android Bluetooth stack logic when connecting to a HID device
(e.g. smart ring): discover HID service, enable Report notifications,
write Exit Suspend to HID Control Point, and handle incoming reports.

Supports battery level reading, optional periodic keepalives, and automatic
reconnect on disconnect.

Usage:
  pip install bleak
  python main.py                                    # connect to default device, reconnect every 3s
  python main.py AA:BB:CC:DD:EE:FF                 # connect by address
  python main.py "My Ring" --keepalive-interval 30  # enable keepalive every 30s
  python main.py --reconnect-delay 5                # reconnect after 5s (default 3)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Callable, Optional

# Set to True or RING_BLE_NOTIFY_VERBOSE=1 to print every notification (noisy)
NOTIFY_VERBOSE = os.environ.get("RING_BLE_NOTIFY_VERBOSE", "").lower() in ("1", "true", "yes")
# Set RING_BLE_REPORT_LAYOUT=1 to log every HID Report notification as len + hex (for reverse-engineering IMU layout)
REPORT_LAYOUT_VERBOSE = os.environ.get("RING_BLE_REPORT_LAYOUT", "").lower() in ("1", "true", "yes")

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError

# Default HID device address (smart ring)
DEFAULT_DEVICE_ADDRESS = "B0:B3:53:EB:40:8D"

# HID over GATT UUIDs (from Android GattService.java)
HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"
HID_CHAR_UUIDS = {
    "information": "00002a4a-0000-1000-8000-00805f9b34fb",   # HID Information
    "report_map": "00002a4b-0000-1000-8000-00805f9b34fb",   # HID Report Map
    "control_point": "00002a4c-0000-1000-8000-00805f9b34fb", # HID Control Point
    "report": "00002a4d-0000-1000-8000-00805f9b34fb",        # Report (Get/Set/Notify)
}
# Client Characteristic Configuration Descriptor (enable notification)
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
# HID Control Point values
HID_CONTROL_POINT_SUSPEND = bytes([0x00])
HID_CONTROL_POINT_EXIT_SUSPEND = bytes([0x01])
# CCCD value: enable notification (little-endian 0x0001)
CCCD_NOTIFY_ENABLE = bytes([0x01, 0x00])

# Standard Battery Service (BLE GATT)
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Environmental Sensing (0x181A) - optional sensor/IMU-related service to discover
ENVIRONMENTAL_SENSING_SERVICE_UUID = "0000181a-0000-1000-8000-00805f9b34fb"

# Vendor write characteristics (for enable/command; try e.g. 01 or 0201 to enable IMU stream)
VENDOR_CHAR_AE41_UUID = "0000ae41-0000-1000-8000-00805f9b34fb"  # write-no-response, service 0xAE40
VENDOR_CHAR_FFF2_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"  # write-no-response, service 0xFFF0


def notification_handler(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
    """Handle incoming notifications from the device."""
    report_uuid_lower = (characteristic.uuid or "").lower().replace("-", "")
    if REPORT_LAYOUT_VERBOSE and report_uuid_lower == HID_CHAR_UUIDS["report"].replace("-", ""):
        print(f"[Report layout] len={len(data)} hex={data.hex()}")
    if NOTIFY_VERBOSE:
        print(f"[Notify] len={len(data)} {characteristic.uuid} (handle={characteristic.handle}) data={data.hex()} ({list(data)})")


def _find_battery_char(client: BleakClient) -> Optional[BleakGATTCharacteristic]:
    """Find standard Battery Level characteristic from client.services. Returns None if not found."""
    for service in client.services:
        if not service.uuid or service.uuid.lower().replace("-", "") != BATTERY_SERVICE_UUID.replace("-", ""):
            continue
        for char in service.characteristics:
            if char.uuid and char.uuid.lower().replace("-", "") == BATTERY_LEVEL_CHAR_UUID.replace("-", ""):
                if "read" in (char.properties or []):
                    return char
    return None


async def _read_battery_level(client: BleakClient, char: BleakGATTCharacteristic) -> Optional[int]:
    """Read battery level characteristic; return 0-100 or None on failure."""
    try:
        value = await client.read_gatt_char(char.uuid)
        if value is not None and len(value) >= 1:
            return min(100, max(0, int(value[0])))
    except Exception:
        pass
    return None


def _pick_keepalive_char(
    client: BleakClient,
    battery_char: Optional[BleakGATTCharacteristic],
    keepalive_mode: str,
) -> Optional[BleakGATTCharacteristic]:
    """Pick a safe characteristic for periodic keepalive reads. Prefer battery if mode is battery and available."""
    if keepalive_mode == "battery" and battery_char is not None:
        return battery_char
    # Fallback: first readable characteristic that is not HID Control Point
    for service in client.services:
        for char in service.characteristics:
            if "read" not in (char.properties or []):
                continue
            uuid_lower = (char.uuid or "").lower()
            if uuid_lower == HID_CHAR_UUIDS["control_point"]:
                continue
            return char
    return battery_char


async def _keepalive_loop(
    client: BleakClient,
    interval_sec: float,
    char: BleakGATTCharacteristic,
    is_connected: Callable[[], bool],
    log_battery: bool = False,
    battery_state: Optional[dict] = None,
    on_battery_updated: Optional[Callable[[int], None]] = None,
) -> None:
    """Background task: periodically read the given characteristic while connected. Exits when disconnected."""
    while is_connected() and interval_sec > 0:
        try:
            await asyncio.sleep(interval_sec)
            if not is_connected():
                break
            print("[Keepalive] sending read")
            data = await client.read_gatt_char(char.uuid)
            if data is not None and len(data) >= 1:
                level = min(100, max(0, int(data[0])))
                if battery_state is not None:
                    if level != battery_state.get("last"):
                        print(f"Battery: {level}%")
                        battery_state["last"] = level
                    if on_battery_updated is not None:
                        on_battery_updated(level)
                elif log_battery:
                    print(f"Battery: {level}%")
        except asyncio.CancelledError:
            break
        except Exception as e:
            if is_connected():
                print(f"Keepalive read failed: {e}")


async def _battery_poll_loop(
    client: BleakClient,
    battery_char: BleakGATTCharacteristic,
    interval_sec: float,
    is_connected: Callable[[], bool],
    battery_state: dict,
    on_battery_updated: Optional[Callable[[int], None]] = None,
) -> None:
    """Dedicated task: periodically read battery characteristic. Exits when disconnected."""
    while is_connected() and interval_sec > 0:
        try:
            await asyncio.sleep(interval_sec)
            if not is_connected():
                break
            level = await _read_battery_level(client, battery_char)
            if level is not None:
                if level != battery_state.get("last"):
                    print(f"Battery: {level}%")
                    battery_state["last"] = level
                if on_battery_updated is not None:
                    on_battery_updated(level)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if is_connected():
                print(f"Battery poll failed: {e}")


def _gatt_tree_string(client: BleakClient) -> str:
    """Build a full GATT tree string (services, characteristics, descriptors) from client.services."""
    lines = []
    for service in client.services:
        lines.append(f"Service {service.uuid} (handle {service.handle})")
        for char in service.characteristics:
            props = ",".join(char.properties) if char.properties else ""
            lines.append(f"  Char {char.uuid} handle={char.handle} [{props}]")
            for desc in char.descriptors:
                lines.append(f"    Descriptor {desc.uuid} handle={desc.handle}")
    return "\n".join(lines)


async def find_hid_device(name_or_address: Optional[str] = None, timeout: float = 10.0):
    """Scan for a BLE device by name or address."""
    print("Scanning for BLE devices...")
    if name_or_address and len(name_or_address) == 17 and ":" in name_or_address:
        # Assume it's an address
        device = await BleakScanner.find_device_by_address(name_or_address, timeout=timeout)
    else:
        device = await BleakScanner.find_device_by_name(name_or_address, timeout=timeout)
    return device


async def run_hid_client(
    address_or_name: Optional[str] = None,
    *,
    keepalive_interval: float = 5.0,
    keepalive_mode: str = "battery",
    battery_poll_interval: float = 60.0,
    on_battery_updated: Optional[Callable[[int], None]] = None,
    gatt_dump_file: Optional[str] = None,
    write_ae41_hex: Optional[str] = None,
    write_fff2_hex: Optional[str] = None,
) -> None:
    """
    Connect to a HID device, enable Report notifications, and send Exit Suspend.

    Pass a BLE address (e.g. "AA:BB:CC:DD:EE:FF") or device name to connect.
    If None, the first device advertising HID service (0x1812) will be used.

    keepalive_interval: seconds between keepalive reads (0 = disabled, default 5).
    keepalive_mode: which characteristic to use for keepalive (battery, read-report, vendor).
    battery_poll_interval: seconds between battery reads when battery char exists (0 = disabled, default 60).
    on_battery_updated: optional callback(level: int) when battery level is read or changes.
    gatt_dump_file: if set, write full GATT tree (services/characteristics/descriptors) to this file path.
    write_ae41_hex: if set (e.g. "01"), write this hex payload to vendor char 0xAE41 (write-no-response).
    write_fff2_hex: if set (e.g. "01"), write this hex payload to vendor char 0xFFF2 (write-no-response).
    """
    target = address_or_name if address_or_name else DEFAULT_DEVICE_ADDRESS
    device = await find_hid_device(target)
    if device is None and not address_or_name:
        print(f"Default device not found: {DEFAULT_DEVICE_ADDRESS}")
        print("Specify an address: python main.py AA:BB:CC:DD:EE:FF")
        return

    if device is None:
        print(f"Device not found: {target}")
        return

    print(f"Connecting to {device.address}...")
    async with BleakClient(device, timeout=20.0) as client:
        if not client.is_connected:
            print("Failed to connect")
            return
        print("Connected. Discovering services...")

        if gatt_dump_file:
            try:
                tree = _gatt_tree_string(client)
                with open(gatt_dump_file, "w", encoding="utf-8") as f:
                    f.write(tree)
                print(f"GATT tree written to {gatt_dump_file}")
            except Exception as e:
                print(f"GATT dump failed: {e}")

        hid_service = None
        report_char = None
        control_point_char = None

        for service in client.services:
            if service.uuid and service.uuid.lower() == HID_SERVICE_UUID:
                hid_service = service
                break

        if hid_service:
            # Standard HID over GATT path
            print(f"HID service found: {hid_service.uuid}")
            for char in hid_service.characteristics:
                uuid_lower = (char.uuid or "").lower()
                if uuid_lower == HID_CHAR_UUIDS["report"]:
                    report_char = char
                elif uuid_lower == HID_CHAR_UUIDS["control_point"]:
                    control_point_char = char
                print(f"  Char: {char.uuid} handle={char.handle} props={char.properties}")

            if report_char and ("notify" in report_char.properties or "indicate" in report_char.properties):
                await client.start_notify(report_char.uuid, notification_handler)
                print("Report notifications enabled")
            if control_point_char and ("write" in control_point_char.properties or "write-without-response" in control_point_char.properties):
                await client.write_gatt_char(
                    control_point_char.uuid,
                    HID_CONTROL_POINT_EXIT_SUSPEND,
                    response="write" in control_point_char.properties,
                )
                print("HID Control Point written: Exit Suspend (0x01)")
            for char in hid_service.characteristics:
                if "read" in char.properties and (char.uuid or "").lower() in (HID_CHAR_UUIDS["information"], HID_CHAR_UUIDS["report_map"]):
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        label = " (HID Report Map)" if (char.uuid or "").lower().replace("-", "") == HID_CHAR_UUIDS["report_map"].replace("-", "") else ""
                        print(f"  Read {char.uuid}{label}: len={len(value)} hex={value.hex()}")
                    except Exception as e:
                        print(f"  Read {char.uuid} failed: {e}")
            # Optional: discover and subscribe/read Environmental Sensing (0x181A) if present
            env_sensing_uuid = ENVIRONMENTAL_SENSING_SERVICE_UUID.lower().replace("-", "")
            for service in client.services:
                if (service.uuid or "").lower().replace("-", "") != env_sensing_uuid:
                    continue
                print(f"Environmental Sensing service (0x181A) found: {service.uuid}")
                for char in service.characteristics:
                    props = ",".join(char.properties) if char.properties else ""
                    print(f"  Char {char.uuid} [{props}]")
                    if char.properties and ("notify" in char.properties or "indicate" in char.properties):
                        try:
                            await client.start_notify(char.uuid, notification_handler)
                            print(f"    -> Notifications enabled")
                        except Exception as e:
                            print(f"    -> Notify failed: {e}")
                    if "read" in (char.properties or []):
                        try:
                            value = await client.read_gatt_char(char.uuid)
                            print(f"  Read {char.uuid}: len={len(value)} hex={value.hex()}")
                        except Exception as e:
                            print(f"  Read {char.uuid} failed: {e}")
        else:
            # No HID service: device uses vendor GATT (e.g. smart ring). Dump full tree and enable all notifications.
            print("HID service (0x1812) not found — device uses vendor GATT.")
            print("Full GATT tree:")
            notify_count = 0
            for service in client.services:
                print(f"  Service {service.uuid} (handle {service.handle})")
                for char in service.characteristics:
                    props = ",".join(char.properties) if char.properties else ""
                    print(f"    Char {char.uuid} handle={char.handle} [{props}]")
                    for desc in char.descriptors:
                        print(f"      Descriptor {desc.uuid} handle={desc.handle}")
                    # Enable notify/indicate on every characteristic that supports it
                    if char.properties and ("notify" in char.properties or "indicate" in char.properties):
                        try:
                            await client.start_notify(char.uuid, notification_handler)
                            notify_count += 1
                            print(f"    -> Notifications enabled")
                        except Exception as e:
                            print(f"    -> Notify failed: {e}")
            print(f"Enabled notifications on {notify_count} characteristic(s).")
            # Optional: write to vendor write characteristics (e.g. enable IMU / stream)
            for hex_payload, char_uuid_norm in (
                (write_ae41_hex, VENDOR_CHAR_AE41_UUID.lower().replace("-", "")),
                (write_fff2_hex, VENDOR_CHAR_FFF2_UUID.lower().replace("-", "")),
            ):
                if not hex_payload:
                    continue
                payload = bytes.fromhex(hex_payload.replace(" ", ""))
                for service in client.services:
                    for char in service.characteristics:
                        if (char.uuid or "").lower().replace("-", "") != char_uuid_norm:
                            continue
                        if "write-without-response" not in (char.properties or []) and "write" not in (char.properties or []):
                            continue
                        try:
                            await client.write_gatt_char(
                                char.uuid,
                                payload,
                                response="write" in (char.properties or []),
                            )
                            print(f"  Wrote {char.uuid}: hex={hex_payload} ({len(payload)} bytes)")
                        except Exception as e:
                            print(f"  Write {char.uuid} failed: {e}")
                        break
            # Read readable characteristics in vendor / sensor services (incl. Environmental Sensing 0x181A)
            vendor_uuids = ("0000ae40", "0000ae00", "0000fff0", "0000181a")
            for service in client.services:
                suuid = (service.uuid or "").lower().replace("-", "")
                if not any(suuid.startswith(u.replace("-", "")) for u in vendor_uuids):
                    continue
                for char in service.characteristics:
                    if "read" not in (char.properties or []):
                        continue
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        print(f"  Read {char.uuid}: {value.hex()} ({list(value)})")
                    except Exception as e:
                        print(f"  Read {char.uuid} failed: {e}")

        # Battery: standard service first, then optional vendor heuristic; track changes and notify callback
        battery_state: dict = {"last": None}
        battery_char = _find_battery_char(client)
        if battery_char:
            level = await _read_battery_level(client, battery_char)
            if level is not None:
                battery_state["last"] = level
                print(f"Battery: {level}%")
                if on_battery_updated is not None:
                    on_battery_updated(level)
            # Enable battery notifications if supported so we get updates as they happen
            if battery_char.properties and ("notify" in battery_char.properties or "indicate" in battery_char.properties):
                def _battery_notification_handler(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
                    if data is not None and len(data) >= 1:
                        level_val = min(100, max(0, int(data[0])))
                        if level_val != battery_state.get("last"):
                            print(f"Battery: {level_val}%")
                            battery_state["last"] = level_val
                        if on_battery_updated is not None:
                            on_battery_updated(level_val)
                try:
                    await client.start_notify(battery_char.uuid, _battery_notification_handler)
                except Exception as e:
                    print(f"Battery notifications not enabled: {e}")
        else:
            # Vendor fallback: try first readable char in vendor services; if single byte 0-100, log as possible battery
            vendor_uuids = ("0000ae40", "0000ae00", "0000fff0", "0000181a")
            for service in client.services:
                suuid = (service.uuid or "").lower().replace("-", "")
                if not any(suuid.startswith(u.replace("-", "")) for u in vendor_uuids):
                    continue
                for char in service.characteristics:
                    if "read" not in (char.properties or []):
                        continue
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        if value is not None and len(value) == 1 and 0 <= value[0] <= 100:
                            print(f"Possible battery (vendor {char.uuid}): {value[0]}%")
                            if on_battery_updated is not None:
                                on_battery_updated(value[0])
                    except Exception:
                        pass
                    break
                break

        keepalive_char = _pick_keepalive_char(client, battery_char, keepalive_mode)
        log_battery_on_keepalive = keepalive_mode == "battery" and battery_char is not None

        print("Running. Press Ctrl+C to disconnect.")
        keepalive_task = None
        battery_poll_task = None
        if keepalive_interval > 0 and keepalive_char:
            keepalive_task = asyncio.create_task(
                _keepalive_loop(
                    client,
                    keepalive_interval,
                    keepalive_char,
                    lambda: client.is_connected,
                    log_battery=log_battery_on_keepalive,
                    battery_state=battery_state if battery_char else None,
                    on_battery_updated=on_battery_updated,
                )
            )
        if battery_poll_interval > 0 and battery_char is not None:
            battery_poll_task = asyncio.create_task(
                _battery_poll_loop(
                    client,
                    battery_char,
                    battery_poll_interval,
                    lambda: client.is_connected,
                    battery_state,
                    on_battery_updated=on_battery_updated,
                )
            )
        try:
            while client.is_connected:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
            if battery_poll_task is not None:
                battery_poll_task.cancel()
                try:
                    await battery_poll_task
                except asyncio.CancelledError:
                    pass
        print("Disconnecting...")


async def run_hid_client_with_reconnect(
    address_or_name: Optional[str] = None,
    *,
    reconnect_delay: float = 3.0,
    keepalive_interval: float = 5.0,
    keepalive_mode: str = "battery",
    battery_poll_interval: float = 60.0,
    on_battery_updated: Optional[Callable[[int], None]] = None,
    gatt_dump_file: Optional[str] = None,
    write_ae41_hex: Optional[str] = None,
    write_fff2_hex: Optional[str] = None,
) -> None:
    """
    Run the HID client indefinitely, reconnecting after disconnect.

    reconnect_delay: seconds to wait before reconnecting (default 3).
    keepalive_interval: seconds between keepalive reads (0 = disabled, default 5).
    keepalive_mode: which characteristic to use for keepalive (battery, read-report, vendor).
    battery_poll_interval: seconds between battery reads when battery char exists (0 = disabled, default 60).
    on_battery_updated: optional callback(level: int) when battery level is read or changes.
    gatt_dump_file: if set, write full GATT tree to this file on each connect.
    write_ae41_hex: if set, write this hex to 0xAE41 on each connect (vendor path only).
    write_fff2_hex: if set, write this hex to 0xFFF2 on each connect (vendor path only).
    """
    target = address_or_name if address_or_name else DEFAULT_DEVICE_ADDRESS
    while True:
        try:
            await run_hid_client(
                address_or_name,
                keepalive_interval=keepalive_interval,
                keepalive_mode=keepalive_mode,
                battery_poll_interval=battery_poll_interval,
                on_battery_updated=on_battery_updated,
                gatt_dump_file=gatt_dump_file,
                write_ae41_hex=write_ae41_hex,
                write_fff2_hex=write_fff2_hex,
            )
        except asyncio.CancelledError:
            raise
        except (BleakError, asyncio.TimeoutError, OSError, ConnectionError) as e:
            print(f"Disconnected: {e}")
            print(f"Will retry in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)
        except KeyboardInterrupt:
            raise
        else:
            # run_hid_client returned normally (e.g. device not found); still retry
            print(f"Connection ended. Will retry in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HID over GATT client with battery, optional keepalive, and auto-reconnect."
    )
    parser.add_argument(
        "device",
        nargs="?",
        default=None,
        help="BLE address (AA:BB:CC:DD:EE:FF) or device name. Default: %(default)s",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=float(os.environ.get("RING_BLE_RECONNECT_DELAY", "3")),
        help="Seconds to wait before reconnecting after disconnect (default: 3, env: RING_BLE_RECONNECT_DELAY)",
    )
    parser.add_argument(
        "--keepalive-interval",
        type=float,
        default=float(os.environ.get("RING_BLE_KEEPALIVE_INTERVAL", "5")),
        help="Seconds between keepalive reads; 0 = disabled (default: 5, env: RING_BLE_KEEPALIVE_INTERVAL)",
    )
    parser.add_argument(
        "--keepalive-mode",
        choices=["battery", "read-report", "vendor"],
        default=os.environ.get("RING_BLE_KEEPALIVE_MODE", "battery"),
        help="Characteristic to use for keepalive (default: battery, env: RING_BLE_KEEPALIVE_MODE)",
    )
    parser.add_argument(
        "--battery-poll-interval",
        type=float,
        default=float(os.environ.get("RING_BLE_BATTERY_POLL_INTERVAL", "60")),
        help="Seconds between battery reads when battery char exists; 0 = disabled (default: 60, env: RING_BLE_BATTERY_POLL_INTERVAL)",
    )
    parser.add_argument(
        "--gatt-dump",
        type=str,
        default=os.environ.get("RING_BLE_GATT_DUMP_FILE", ""),
        metavar="FILE",
        help="Write full GATT tree to FILE on connect (env: RING_BLE_GATT_DUMP_FILE)",
    )
    parser.add_argument(
        "--write-ae41",
        type=str,
        default=None,
        metavar="HEX",
        help="On connect (vendor path), write HEX to char 0xAE41 (e.g. 01). No spaces.",
    )
    parser.add_argument(
        "--write-fff2",
        type=str,
        default=None,
        metavar="HEX",
        help="On connect (vendor path), write HEX to char 0xFFF2 (e.g. 01). No spaces.",
    )
    args = parser.parse_args()

    asyncio.run(
        run_hid_client_with_reconnect(
            args.device,
            reconnect_delay=args.reconnect_delay,
            keepalive_interval=args.keepalive_interval,
            keepalive_mode=args.keepalive_mode,
            battery_poll_interval=args.battery_poll_interval,
            gatt_dump_file=args.gatt_dump or None,
            write_ae41_hex=args.write_ae41,
            write_fff2_hex=args.write_fff2,
        )
    )


if __name__ == "__main__":
    main()
