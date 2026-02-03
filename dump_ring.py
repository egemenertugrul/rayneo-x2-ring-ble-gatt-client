#!/usr/bin/env python3
"""
Testing and dumping script for the RayNeo X2 Ring BLE client.

Connects once (no auto-reconnect), optionally dumps the full GATT tree to a file,
and logs HID Report notifications (len + hex) and/or all notifications for
reverse-engineering report layout and IMU.

Usage:
  python dump_ring.py                                    # connect, then wait (Ctrl+C to exit)
  python dump_ring.py --report-layout                    # log every HID Report as len + hex
  python dump_ring.py --gatt-dump gatt_tree.txt           # write GATT tree to file
  python dump_ring.py --report-layout --gatt-dump gatt.txt
  python dump_ring.py --verbose                          # log every notification (noisy)
  python dump_ring.py --write-ae41 01 --write-fff2 01    # try enable commands (vendor path)
  python dump_ring.py AA:BB:CC:DD:EE:FF --report-layout  # by address
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import main as main_module


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ring BLE test/dump: single connect, optional GATT dump and report layout logging."
    )
    parser.add_argument(
        "device",
        nargs="?",
        default=None,
        help="BLE address (AA:BB:CC:DD:EE:FF) or device name. Default: use default from main.py",
    )
    parser.add_argument(
        "--report-layout",
        action="store_true",
        help="Log every HID Report notification as len + hex (for reverse-engineering IMU layout)",
    )
    parser.add_argument(
        "--gatt-dump",
        type=str,
        default=None,
        metavar="FILE",
        help="Write full GATT tree (services/characteristics/descriptors) to FILE on connect",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every BLE notification (noisy; use with --report-layout for full report only)",
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

    main_module.REPORT_LAYOUT_VERBOSE = args.report_layout
    main_module.NOTIFY_VERBOSE = args.verbose
    gatt_dump_file = args.gatt_dump
    write_ae41_hex = args.write_ae41
    write_fff2_hex = args.write_fff2

    if args.report_layout:
        print("Report layout logging: ON (HID Report notifications will print len + hex)")
    if args.verbose:
        print("Verbose notifications: ON (all notifications will be printed)")
    if gatt_dump_file:
        print(f"GATT dump: will write tree to {gatt_dump_file}")
    if write_ae41_hex:
        print(f"Will write to 0xAE41: {write_ae41_hex}")
    if write_fff2_hex:
        print(f"Will write to 0xFFF2: {write_fff2_hex}")

    print("Connecting once (no reconnect). Press Ctrl+C to disconnect and exit.")
    asyncio.run(
        main_module.run_hid_client(
            args.device,
            keepalive_interval=0,  # no keepalive for dump session
            battery_poll_interval=0,
            gatt_dump_file=gatt_dump_file,
            write_ae41_hex=write_ae41_hex,
            write_fff2_hex=write_fff2_hex,
        )
    )
    print("Disconnected. Exiting.")


if __name__ == "__main__":
    main()
