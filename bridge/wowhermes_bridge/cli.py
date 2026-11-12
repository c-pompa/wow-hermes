"""``wowhermes-bridge`` console script: install / run / status / decode-image / probe-capture."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from . import __version__, decoder
from .app import BridgeApp, CaptureError, NoWowWindow
from .config import Config, config_path, detect_account, find_wow_dirs, windowed_fullscreen
from .installer import install
from .slots import Doorbells, SlotPublisher


def _print_report(report) -> None:  # noqa: ANN001
    for line in report.lines():
        print(line)
    for action in report.actions[:8]:
        print(f"  {action}")
    if len(report.actions) > 8:
        print(f"  ... {len(report.actions) - 8} more")


def cmd_install(args) -> int:
    config = Config.load()
    if args.wow:
        wow_dir = Path(args.wow)
        if not wow_dir.is_dir():
            print(f"error: {wow_dir} is not a directory")
            return 2
    else:
        candidates = [Path(config.wow_dir)] if config.wow_dir else []
        candidates.extend(find_wow_dirs())
        wow_dir = next((path for path in candidates if path.is_dir()), Path(""))
        if not str(wow_dir):
            print("error: no WoW directory found; pass --wow <...\\_forever_>")
            return 2

    account = detect_account(wow_dir)
    print(f"account:      {account or '(not detected)'}")
    mode = windowed_fullscreen(wow_dir)
    if mode is False:
        print("warning:      gxWindow is 0 (exclusive fullscreen): the overlay and capture need Windowed (Fullscreen)")

    try:
        report = install(
            wow_dir,
            addon_source=args.addon_src,
            slot_count=args.slots,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2
    _print_report(report)

    if not args.dry_run and report.changed:
        config.wow_dir = str(wow_dir)
        if account and not config.account:
            config.account = account
        config.slot_count = args.slots
        try:
            config.save()
            print(f"config:       saved {config.path}")
        except OSError as exc:
            print(f"warning:      could not save config: {exc}")
        print("next:         start WoW, then `/reload` once if the game was already running")
    return 0


def cmd_run(args) -> int:
    config = Config.load()
    if args.gateway:
        config.gateway_url = args.gateway
    if args.character:
        config.character = args.character
    if not config.wow_dir or not Path(config.wow_dir).is_dir():
        print("error: no WoW directory configured; run `wowhermes-bridge install --wow <path>`")
        return 2

    from .capture import make_capture

    logger = _logger(args.verbose)
    try:
        capture = make_capture(logger=logger)
        logger(f"capture: {capture.describe()}")
    except NoWowWindow as exc:
        capture = None
        print(f"capture: {exc}" if args.once else f"warning: {exc} (retrying)")
    except CaptureError as exc:
        capture = None
        print(f"capture error: {exc}")

    client = None
    token = config.effective_token()
    if token:
        from .hermes_client import HermesClient

        client = HermesClient(config.gateway_url, token)
        try:
            health = client.health()
            print(f"gateway: {config.gateway_url} ok={health.get('ok')} version={health.get('version')}")
        except Exception as exc:  # noqa: BLE001
            print(f"gateway: {config.gateway_url} unreachable ({exc}); will retry in the loop")
    else:
        print("warning: no bearer token (set WOWHERMES_TOKEN or `token` in config.toml)")

    overlay = None
    qt_app = None
    want_overlay = (config.overlay or getattr(args, "overlay", False)) and not args.no_overlay
    if want_overlay and not args.once:
        try:
            from PySide6 import QtWidgets

            from .overlay import create_overlay

            qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
            from .capture import client_rect, find_wow_window

            window = find_wow_window()
            rect = client_rect(window.hwnd) if window else None
            overlay = create_overlay(enabled=True, client_rect=rect)
        except Exception as exc:  # noqa: BLE001
            print(f"overlay: disabled ({exc})")

    app = BridgeApp(
        config, capture=capture, overlay=overlay, client=client, logger=logger,
        capture_factory=lambda: make_capture(logger=logger),
    )
    if args.once:
        app.start()
        report = app.tick()
        print(
            f"tick: captured={report.captured} frames={report.frames} records={report.records} "
            f"outbox={report.outbox_frames} decode={report.decode_reason} error={report.error}"
        )
        app.publish(bridge="up", force=True)
        return 0 if report.captured else 3

    stop = threading.Event()

    def _handle_signal(signum, _frame):  # noqa: ANN001
        logger(f"signal {signum}: stopping")
        stop.set()
        app.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, OSError):  # pragma: no cover - not on the main thread
        pass

    if qt_app is not None:
        from PySide6 import QtCore

        app.start()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)

        def _tick_once() -> None:
            app.tick()
            timer.start(int(app.next_interval() * 1000))

        timer.timeout.connect(_tick_once)
        timer.start(int(app.next_interval() * 1000))
        qt_app.exec()
        app.publish(bridge="down", force=True)
        return 0

    app.run(stop)
    return 0


def cmd_status(args) -> int:
    config = Config.load()
    lines = config.status_lines()
    publisher = SlotPublisher(config.addons_dir, count=config.slot_count)
    owned = publisher.owned_slots()
    lines.append(f"slots owned:  {len(owned)}/{config.slot_count}")
    bells = Doorbells(config.addon_root)
    lines.append(f"doorbells:    ack={bells.raised('ack')} sig={bells.raised('sig')}")
    sv = config.savedvars_path
    lines.append(f"savedvars:    {'present' if sv.is_file() else 'absent'} ({sv})")
    if not config.effective_token():
        lines.append("token:        MISSING -- set WOWHERMES_TOKEN")
    for line in lines:
        print(line)

    if config.wow_dir and Path(config.wow_dir).is_dir():
        try:
            from .capture import find_wow_window

            window = find_wow_window()
            print(f"window:       {'hwnd %d %r' % (window.hwnd, window.title) if window else 'no WoW window'}")
        except Exception as exc:  # noqa: BLE001
            print(f"window:       unavailable ({exc})")
    return 0


def cmd_decode_image(args) -> int:
    from PIL import Image

    image = Image.open(args.image).convert("RGB")
    import numpy as np

    result = decoder.decode_image(np.asarray(image), **({"order": args.order} if args.order else {}))
    if not result.ok:
        print(f"no frame ({result.reason}) after {result.attempts} hypotheses")
        return 1
    out = []
    for frame in result.frames:
        out.append(
            {
                "id": frame.id,
                "pitch": frame.pitch,
                "origin": list(frame.origin),
                "cells": frame.cells,
                "payload_hex": frame.payload.hex(),
                "records": frame.records(),
            }
        )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_probe_capture(args) -> int:
    from .capture import WIDE, make_capture

    size = WIDE if args.wide else None
    try:
        capture = make_capture(prefer=args.prefer, **({"size": size} if size else {}))
    except NoWowWindow as exc:
        print(str(exc))
        return 3
    except CaptureError as exc:
        print(f"capture error: {exc}")
        return 4
    try:
        image = capture.grab()
    finally:
        pass
    from PIL import Image

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image[:, :, :3][:, :, ::-1] if image.shape[2] == 4 else image).save(out)
    print(f"saved {out} -- {capture.describe()} -- shape {image.shape}")
    decode = decoder.decode_image(image, order="bgra" if image.shape[2] == 4 else "rgb", anisotropic=True)
    print(f"decode: {'ok' if decode.ok else 'no frame'} ({decode.reason})"
          + (f" pitch={decode.pitch} origin={decode.origin}" if decode.ok else ""))
    capture.close()
    return 0 if decode.ok else 0


def _logger(verbose: bool):
    if not verbose:
        return None

    def log(message: str) -> None:
        import time

        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    return log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wowhermes-bridge",
        description="Windows companion bridge for the WoWHermes addon (pixel strip in, slot files out).",
    )
    parser.add_argument("--version", action="version", version=f"wowhermes-bridge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    install_cmd = sub.add_parser("install", help="install the addon, 256 slot addons and doorbell pools")
    install_cmd.add_argument("--wow", help="path to the _forever_ directory")
    install_cmd.add_argument("--addon-src", help="addon/WoWHermes checkout to copy")
    install_cmd.add_argument("--slots", type=int, default=256, help="number of load-on-demand slot addons")
    install_cmd.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    install_cmd.set_defaults(func=cmd_install)

    run_cmd = sub.add_parser("run", help="run the bridge loop")
    run_cmd.add_argument("--no-overlay", action="store_true")
    run_cmd.add_argument("--overlay", action="store_true", help="show the desktop overlay (off by default; config `overlay = true`)")
    run_cmd.add_argument("--once", action="store_true", help="single tick (smoke test), then exit")
    run_cmd.add_argument("--gateway", help="override the gateway base URL")
    run_cmd.add_argument("--character", help="override the character key")
    run_cmd.add_argument("-v", "--verbose", action="store_true")
    run_cmd.set_defaults(func=cmd_run)

    status_cmd = sub.add_parser("status", help="show config, install state and window discovery")
    status_cmd.add_argument("--json", action="store_true")
    status_cmd.set_defaults(func=cmd_status)

    decode_cmd = sub.add_parser("decode-image", help="decode a screenshot of the strip")
    decode_cmd.add_argument("image")
    decode_cmd.add_argument("--order", choices=("rgb", "bgr", "bgra", "rgba"))
    decode_cmd.set_defaults(func=cmd_decode_image)

    probe_cmd = sub.add_parser("probe-capture", help="save the strip search region as a PNG")
    probe_cmd.add_argument("--out", default="probe.png")
    probe_cmd.add_argument("--wide", action="store_true", help="kept for compatibility: the default region is already 1600x400")
    probe_cmd.add_argument("--prefer", choices=("auto", "wgc", "gdi"), default="auto")
    probe_cmd.set_defaults(func=cmd_probe_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
