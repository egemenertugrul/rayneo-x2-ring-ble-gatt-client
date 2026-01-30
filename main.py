#!/usr/bin/env python3
"""
HID over GATT (HOGP) client using Bleak.

Replicates the Android Bluetooth stack logic when connecting to a HID device
(e.g. smart ring): discover HID service, enable Report notifications,
write Exit Suspend to HID Control Point, and handle incoming reports.

Usage:
  pip install bleak
  python hid_gatt_client.py                    # connect to default device (B0:B3:53:EB:40:8D)
  python hid_gatt_client.py AA:BB:CC:DD:EE:FF # connect by address
  python hid_gatt_client.py "My Ring"         # connect by name
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

# Set to True or RING_BLE_NOTIFY_VERBOSE=1 to print every notification (noisy)
NOTIFY_VERBOSE = os.environ.get("RING_BLE_NOTIFY_VERBOSE", "").lower() in ("1", "true", "yes")

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic


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


def notification_handler(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
    """Handle incoming notifications from the device."""
    if NOTIFY_VERBOSE:
        print(f"[Notify] {characteristic.uuid} (handle={characteristic.handle}) data={data.hex()} ({list(data)})")


async def find_hid_device(name_or_address: Optional[str] = None, timeout: float = 10.0):
    """Scan for a BLE device by name or address."""
    print("Scanning for BLE devices...")
    if name_or_address and len(name_or_address) == 17 and ":" in name_or_address:
        # Assume it's an address
        device = await BleakScanner.find_device_by_address(name_or_address, timeout=timeout)
    else:
        device = await BleakScanner.find_device_by_name(name_or_address, timeout=timeout)
    return device


async def run_hid_client(address_or_name: Optional[str] = None):
    """
    Connect to a HID device, enable Report notifications, and send Exit Suspend.

    Pass a BLE address (e.g. "AA:BB:CC:DD:EE:FF") or device name to connect.
    If None, the first device advertising HID service (0x1812) will be used.
    """
    target = address_or_name if address_or_name else DEFAULT_DEVICE_ADDRESS
    device = await find_hid_device(target)
    if device is None and not address_or_name:
        print(f"Default device not found: {DEFAULT_DEVICE_ADDRESS}")
        print("Specify an address: python hid_gatt_client.py AA:BB:CC:DD:EE:FF")
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
                        print(f"  Read {char.uuid}: {value.hex()}")
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
            # Read readable characteristics in vendor services (skip 0x1800/0x1801 if desired, or read all)
            vendor_uuids = ("0000ae40", "0000ae00", "0000fff0")
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

        print("Running. Press Ctrl+C to disconnect.")
        try:
            while client.is_connected:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        print("Disconnecting...")


def main():
    addr_or_name = None
    if len(sys.argv) > 1:
        addr_or_name = sys.argv[1]
    asyncio.run(run_hid_client(addr_or_name))


if __name__ == "__main__":
    main()
