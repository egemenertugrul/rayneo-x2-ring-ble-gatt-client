#!/usr/bin/env python3
"""
RayNeo X2 Ring BLE client with OSCQuery exposure.

Starts an OSCQuery server, then runs the BLE client from main.py. Incoming
notification payloads are parsed (bytes at indices 10, 11, 12) and exposed
as OSCQuery nodes so clients can discover and read them via OSCQuery only.

Usage:
  pip install -r requirements.txt
  python run_ring_oscquery.py                                    # connect to default device (auto-reconnect)
  python run_ring_oscquery.py AA:BB:CC:DD:EE:FF                  # connect by address
  python run_ring_oscquery.py "My Ring" --keepalive-interval 15  # keepalive every 15s to prevent ring sleep
  python run_ring_oscquery.py --help                            # show all options

  Options can also be set via env: RING_BLE_RECONNECT_DELAY, RING_BLE_KEEPALIVE_INTERVAL,
  RING_BLE_KEEPALIVE_MODE, RING_BLE_BATTERY_POLL_INTERVAL (CLI overrides env).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

# Payload indices for pointer/touch (adjust if protocol changes)
IDX_BYTE_10 = 10
IDX_BYTE_11 = 11
IDX_BYTE_12 = 12

# IMU: optional parsing from HID report (bytes 0-5 accel XYZ, 6-11 gyro XYZ as int16 LE). Set REPORT_MIN_LEN_FOR_IMU to 0 to disable.
ACCEL_START = 0
ACCEL_BYTES = 6  # 3 x int16 LE
GYRO_START = 6
GYRO_BYTES = 6   # 3 x int16 LE
REPORT_MIN_LEN_FOR_IMU = 12  # need at least 12 bytes to parse accel + gyro
# Scale: int16 LSB to physical (e.g. 16384 = 1g for accel; adjust per device)
IMU_ACCEL_SCALE = 1.0 / 16384.0
IMU_GYRO_SCALE = 1.0 / 16384.0

HTTP_PORT = 9020
OSC_PORT = 9020
OSCQUERY_SERVICE_NAME = "RayNeo-X2-Ring"


def _create_oscquery_service_and_nodes():
    """Create OSCQuery service and ring nodes; return service. Updates go through service.update_value() so HTTP GET returns current values."""
    from tinyoscquery.queryservice import OSCQueryService
    from tinyoscquery.shared.node import OSCAccess, OSCQueryNode

    service = OSCQueryService(OSCQUERY_SERVICE_NAME, HTTP_PORT, OSC_PORT)

    node_x = OSCQueryNode(
        full_path="/ring/X", value=[0], type_=[int], access=OSCAccess.READWRITE_VALUE
    )
    node_y = OSCQueryNode(
        full_path="/ring/Y", value=[0], type_=[int], access=OSCAccess.READWRITE_VALUE
    )
    node_press = OSCQueryNode(
        full_path="/ring/press", value=[0], type_=[int], access=OSCAccess.READWRITE_VALUE
    )
    node_battery = OSCQueryNode(
        full_path="/ring/battery", value=[0], type_=[int], access=OSCAccess.READWRITE_VALUE
    )
    nodes = [node_x, node_y, node_press, node_battery]
    # IMU (optional): accel/gyro from HID report bytes 0-11 when report length >= REPORT_MIN_LEN_FOR_IMU
    if REPORT_MIN_LEN_FOR_IMU > 0:
        for name in ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"):
            nodes.append(
                OSCQueryNode(
                    full_path=f"/ring/{name}",
                    value=[0.0],
                    type_=[float],
                    access=OSCAccess.READWRITE_VALUE,
                )
            )
    for node in nodes:
        service.add_node(node)

    return service


def _parse_int16_le(data: bytearray, offset: int) -> int:
    """Parse int16 little-endian from data at offset; return 0 if out of range."""
    if offset + 2 > len(data):
        return 0
    low, high = data[offset], data[offset + 1]
    v = low | (high << 8)
    return v - 65536 if v >= 32768 else v


def _make_notification_wrapper(original_handler, service, debug=False):
    """Return a wrapper that calls the original handler and updates OSCQuery nodes via service.update_value()."""

    def wrapper(characteristic, data: bytearray):
        original_handler(characteristic, data)
        # IMU: parse accel/gyro from first 12 bytes when report is long enough
        if REPORT_MIN_LEN_FOR_IMU > 0 and len(data) >= REPORT_MIN_LEN_FOR_IMU:
            try:
                ax = _parse_int16_le(data, ACCEL_START + 0) * IMU_ACCEL_SCALE
                ay = _parse_int16_le(data, ACCEL_START + 2) * IMU_ACCEL_SCALE
                az = _parse_int16_le(data, ACCEL_START + 4) * IMU_ACCEL_SCALE
                gx = _parse_int16_le(data, GYRO_START + 0) * IMU_GYRO_SCALE
                gy = _parse_int16_le(data, GYRO_START + 2) * IMU_GYRO_SCALE
                gz = _parse_int16_le(data, GYRO_START + 4) * IMU_GYRO_SCALE
                service.update_value("/ring/accel_x", ax)
                service.update_value("/ring/accel_y", ay)
                service.update_value("/ring/accel_z", az)
                service.update_value("/ring/gyro_x", gx)
                service.update_value("/ring/gyro_y", gy)
                service.update_value("/ring/gyro_z", gz)
            except Exception as e:
                if debug:
                    print(f"[OSCQuery] IMU parse failed: {e}")
        # Pointer/touch: bytes 10, 11, 12
        if len(data) <= IDX_BYTE_12:
            if debug:
                print(f"[OSCQuery] Skipped pointer update: len(data)={len(data)} (need > {IDX_BYTE_12})")
            return
        x_val = data[IDX_BYTE_10]
        y_val = data[IDX_BYTE_11]
        press_val = data[IDX_BYTE_12]
        try:
            service.update_value("/ring/X", x_val)
            service.update_value("/ring/Y", y_val)
            service.update_value("/ring/press", press_val)
            if debug:
                print(
                    f"[OSCQuery] Updated: X={x_val} Y={y_val} press={press_val}"
                )
        except Exception as e:
            print(f"[OSCQuery] Update failed: {e}")

    return wrapper


def _shutdown_oscquery_service(service):
    """Stop the OSCQuery HTTP/aiohttp server and unregister zeroconf so port 9020 is released."""
    try:
        if getattr(service, "_use_aiohttp", False):
            loop = getattr(service, "_aiohttp_loop", None)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        elif getattr(service, "http_server", None) is not None:
            service.http_server.shutdown()
    except Exception as e:
        print(f"OSCQuery HTTP shutdown: {e}")
    try:
        zc = getattr(service, "_zeroconf", None)
        if zc is not None:
            zc.unregister_all_services()
            zc.close()
    except Exception as e:
        print(f"OSCQuery zeroconf shutdown: {e}")


def main():
    import main as main_module

    _service = _create_oscquery_service_and_nodes()
    print(f"OSCQuery server: {OSCQUERY_SERVICE_NAME} http={HTTP_PORT} osc={OSC_PORT}")
    if getattr(_service, "_use_aiohttp", False):
        print(f"WebSocket on same port (OSCQuery spec): ws://127.0.0.1:{_service.wsPort} — use LISTEN to get live updates")
    else:
        print(f"WebSocket for live value updates: ws://127.0.0.1:{_service.wsPort} (in HOST_INFO)")
    ring_nodes = "/ring/X, /ring/Y, /ring/press, /ring/battery"
    if REPORT_MIN_LEN_FOR_IMU > 0:
        ring_nodes += ", /ring/accel_x, /ring/accel_y, /ring/accel_z, /ring/gyro_x, /ring/gyro_y, /ring/gyro_z"
    print(f"Ring nodes: {ring_nodes}")
    original_handler = main_module.notification_handler
    main_module.notification_handler = _make_notification_wrapper(original_handler, _service)

    def _on_battery_updated(level: int) -> None:
        try:
            _service.update_value("/ring/battery", level)
        except Exception as e:
            print(f"[OSCQuery] Battery update failed: {e}")
    print("Notification handler wrapped: BLE updates will be pushed to OSCQuery nodes.")
    print("Press Ctrl+C to disconnect and exit.")

    parser = argparse.ArgumentParser(
        description="RayNeo X2 Ring BLE client with OSCQuery. Options match main.py (env as defaults)."
    )
    parser.add_argument(
        "device",
        nargs="?",
        default=None,
        help="BLE address (AA:BB:CC:DD:EE:FF) or device name. Default: use default from main.py",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=float(os.environ.get("RING_BLE_RECONNECT_DELAY", "3")),
        help="Seconds before reconnecting after disconnect (default: 3, env: RING_BLE_RECONNECT_DELAY)",
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
        help="Characteristic for keepalive (default: battery, env: RING_BLE_KEEPALIVE_MODE)",
    )
    parser.add_argument(
        "--battery-poll-interval",
        type=float,
        default=float(os.environ.get("RING_BLE_BATTERY_POLL_INTERVAL", "60")),
        help="Seconds between battery reads; 0 = disabled (default: 60, env: RING_BLE_BATTERY_POLL_INTERVAL)",
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

    addr_or_name = args.device
    reconnect_delay = args.reconnect_delay
    keepalive_interval = args.keepalive_interval
    keepalive_mode = args.keepalive_mode
    battery_poll_interval = args.battery_poll_interval
    gatt_dump_file = args.gatt_dump or None
    write_ae41_hex = args.write_ae41
    write_fff2_hex = args.write_fff2

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(
        main_module.run_hid_client_with_reconnect(
            addr_or_name,
            reconnect_delay=reconnect_delay,
            keepalive_interval=keepalive_interval,
            keepalive_mode=keepalive_mode,
            battery_poll_interval=battery_poll_interval,
            on_battery_updated=_on_battery_updated,
            gatt_dump_file=gatt_dump_file,
            write_ae41_hex=write_ae41_hex,
            write_fff2_hex=write_fff2_hex,
        )
    )

    def on_sigint():
        print("\nShutting down...")
        task.cancel()

    try:
        if signal.getsignal(signal.SIGINT) != signal.SIG_IGN:
            signal.signal(signal.SIGINT, lambda s, f: on_sigint())
    except (AttributeError, ValueError):
        pass  # Windows / no main thread

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        on_sigint()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    except asyncio.CancelledError:
        pass
    finally:
        _shutdown_oscquery_service(_service)
        loop.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
