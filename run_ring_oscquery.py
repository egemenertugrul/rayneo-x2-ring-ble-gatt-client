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

# Payload indices for the "255, 255" bytes and the byte after (adjust if protocol changes)
IDX_BYTE_10 = 10
IDX_BYTE_11 = 11
IDX_BYTE_12 = 12

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
    for node in (node_x, node_y, node_press, node_battery):
        service.add_node(node)

    return service


def _make_notification_wrapper(original_handler, service, debug=False):
    """Return a wrapper that calls the original handler and updates OSCQuery nodes via service.update_value()."""

    def wrapper(characteristic, data: bytearray):
        original_handler(characteristic, data)
        if len(data) <= IDX_BYTE_12:
            if debug:
                print(f"[OSCQuery] Skipped update: len(data)={len(data)} (need > {IDX_BYTE_12})")
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
    print("Ring nodes: /ring/X, /ring/Y, /ring/press, /ring/battery")
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
    args = parser.parse_args()

    addr_or_name = args.device
    reconnect_delay = args.reconnect_delay
    keepalive_interval = args.keepalive_interval
    keepalive_mode = args.keepalive_mode
    battery_poll_interval = args.battery_poll_interval

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
