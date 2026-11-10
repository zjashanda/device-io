"""CLI: ``device-io <command>``.

Exit codes are part of the contract so a shell caller can branch without
parsing text:

0  success
1  usage / contract error (bad spec, unreadable fixture, unknown format)
2  resolution failed (a required role is missing/ambiguous/conflicting)
3  operation unsupported on this platform (e.g. clearusb on Linux)
4  backend unavailable (wrong OS, missing collector, PowerShell absent)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, clearusb, core, drivers, render, schema
from .backends import BackendError, BackendUnavailable
from .roles import SpecError, load_spec

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_RESOLUTION = 2
EXIT_UNSUPPORTED = 3
EXIT_BACKEND = 4


def _add_backend_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "linux", "windows", "fixture"],
        help="observation backend (default: auto = current OS)",
    )
    parser.add_argument("--fixture", help="snapshot JSON for the fixture backend (path or fixtures/ name)")
    parser.add_argument("--sysfs-root", help="override /sys for offline Linux tests")
    parser.add_argument("--plugin", action="append", default=[], help="adapter plugin JSON (repeatable)")


def _add_emit_flags(parser: argparse.ArgumentParser, choices=render.FORMATS) -> None:
    parser.add_argument("--format", "-f", default="tree", choices=list(choices))
    parser.add_argument("--out", "-o", help="write output to this file instead of stdout")


def _load_plugins(paths: List[str]) -> List[str]:
    notices: List[str] = []
    for path in paths:
        notices.extend(drivers.load_plugin(path))
    return notices


def _emit(text: str, out: Optional[str]) -> None:
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _snapshot(args: argparse.Namespace):
    return core.collect_snapshot(
        backend=args.backend,
        fixture=getattr(args, "fixture", None),
        sysfs_root=getattr(args, "sysfs_root", None),
        include_audio=not getattr(args, "no_audio", False),
        include_disabled=getattr(args, "include_disabled", False),
    )


def cmd_ports(args: argparse.Namespace) -> int:
    for notice in _load_plugins(args.plugin):
        print(notice, file=sys.stderr)
    document = _snapshot(args).to_dict(include_boards=True)
    _emit(render.render_document(document, args.format), args.out)
    return EXIT_OK


def cmd_boards(args: argparse.Namespace) -> int:
    for notice in _load_plugins(args.plugin):
        print(notice, file=sys.stderr)
    document = _snapshot(args).to_dict(include_boards=True)
    if args.format == "json":
        payload = {
            "schema": schema.SCHEMAS["boards"],
            "generated_at": document["generated_at"],
            "backend": document["backend"],
            "os": document["os"],
            "boards": document["boards"],
        }
        _emit(render.dumps(payload), args.out)
    else:
        _emit(render.render_document(document, args.format), args.out)
    return EXIT_OK


def cmd_audio(args: argparse.Namespace) -> int:
    document = _snapshot(args).to_dict(include_boards=True)
    if args.format == "json":
        _emit(render.dumps({"schema": schema.SCHEMAS["snapshot"], "generated_at": document["generated_at"], "audio": document["audio"]}), args.out)
        return EXIT_OK
    lines = []
    for item in document["audio"]:
        lines.append(
            "{0}\t{1}\t{2}\t{3}".format(
                item.get("kind"), item.get("name"), item.get("channels") or "-", item.get("board_key") or "-"
            )
        )
    _emit("\n".join(lines) if lines else "（未发现音频端点）", args.out)
    return EXIT_OK


def cmd_roles(args: argparse.Namespace) -> int:
    for notice in _load_plugins(args.plugin):
        print(notice, file=sys.stderr)
    spec = load_spec(args.spec)
    snapshot = _snapshot(args)
    document = core.resolve_roles(snapshot, spec, strict=args.strict)
    text = render.render_role_map(document, args.format)
    _emit(text, args.out if args.out else None)
    if args.save:
        Path(args.save).write_text(render.dumps(document) + "\n", encoding="utf-8")
    return EXIT_OK if document["ok"] else EXIT_RESOLUTION


def cmd_driver(args: argparse.Namespace) -> int:
    for notice in _load_plugins(args.plugin):
        print(notice, file=sys.stderr)
    if args.action == "list":
        payload = {
            "schema": drivers.PLUGIN_SCHEMA,
            "adapters": [adapter.to_dict() for adapter in drivers.adapters()],
        }
        if args.format == "json":
            _emit(render.dumps(payload), args.out)
        else:
            lines = [
                "{0:<20} {1:<12} {2:<7} {3}".format(
                    adapter.id, adapter.family, adapter.confidence, adapter.display
                )
                for adapter in drivers.adapters()
            ]
            _emit("\n".join(lines), args.out)
        return EXIT_OK

    required: List[str] = []
    if args.require:
        required.extend(item.strip() for item in args.require.split(",") if item.strip())
    if args.spec:
        spec = load_spec(args.spec)
        required.extend(
            str(role["driver"]).strip()
            for role in spec["roles"]
            if role.get("driver")
        )
    if not required:
        print("driver check needs --require <family,...> or --spec <roles.json>", file=sys.stderr)
        return EXIT_USAGE
    document = drivers.check_drivers(_snapshot(args), required=required, os_name=None, dry_run=args.dry_run)
    if args.format == "json":
        _emit(render.dumps(document), args.out)
    else:
        lines = ["驱动检查 {0}  ok={1}".format(document["generated_at"], document["ok"])]
        for entry in document["drivers"]:
            lines.append(
                "  {0:<10} {1:<8} ports={2}  {3}".format(
                    entry["family"],
                    entry["status"],
                    ",".join(entry.get("ports") or []) or "-",
                    entry.get("evidence") or "",
                )
            )
            if entry.get("advice"):
                lines.append("             建议: {0}".format(entry["advice"]))
        _emit("\n".join(lines), args.out)
    return EXIT_OK if document["ok"] else EXIT_RESOLUTION


def cmd_clearusb(args: argparse.Namespace) -> int:
    document = clearusb.plan(apply=args.apply, board=args.board, timeout=args.timeout)
    if args.format == "json":
        _emit(render.dumps(document), args.out)
    else:
        _emit(clearusb.render(document), args.out)
    if not document["supported"]:
        return EXIT_UNSUPPORTED
    if args.apply and (document.get("summary") or {}).get("failed"):
        return EXIT_USAGE
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    report = core.doctor()
    if args.format == "json":
        _emit(render.dumps(report), args.out)
        return EXIT_OK
    lines = ["device-io {0}  os={1}  {2}".format(__version__, report["os"], report["generated_at"])]
    for name, entry in sorted(report["backends"].items()):
        lines.append(
            "  {0:<9} {1:<6} {2}".format(name, "OK" if entry["available"] else "NO", entry["reason"])
        )
    shell = clearusb.powershell()
    lines.append("  powershell: {0}".format(shell or "not found"))
    _emit("\n".join(lines), args.out)
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    documents = schema.all_schemas()
    if args.list or not args.name:
        _emit("\n".join(sorted(documents)), args.out)
        return EXIT_OK
    if args.name not in documents:
        print(
            "unknown schema {0!r}; available: {1}".format(args.name, ", ".join(sorted(documents))),
            file=sys.stderr,
        )
        return EXIT_USAGE
    _emit(render.dumps(documents[args.name]), args.out)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="device-io",
        description="Canonical device I/O layer: enumerate USB/serial/audio, resolve port roles, clean ghosts, check drivers.",
    )
    parser.add_argument("--version", action="version", version="device-io {0}".format(__version__))
    subparsers = parser.add_subparsers(dest="command", required=True)

    ports = subparsers.add_parser("ports", help="list serial ports grouped by physical board")
    _add_backend_flags(ports)
    _add_emit_flags(ports)
    ports.add_argument("--no-audio", action="store_true", help="skip audio endpoints")
    ports.add_argument("--include-disabled", action="store_true", help="include disabled devices")
    ports.set_defaults(func=cmd_ports)

    boards = subparsers.add_parser("boards", help="list physical boards (JSON contract available)")
    _add_backend_flags(boards)
    _add_emit_flags(boards)
    boards.set_defaults(func=cmd_boards)

    audio = subparsers.add_parser("audio", help="list audio endpoints")
    _add_backend_flags(audio)
    _add_emit_flags(audio, ("plain", "json"))
    audio.set_defaults(func=cmd_audio)

    roles = subparsers.add_parser("roles", help="resolve port roles from a spec")
    roles.add_argument("--spec", required=True, help="roles spec JSON (ark.device-roles-spec.v1)")
    _add_backend_flags(roles)
    roles.add_argument("--format", "-f", default="text", choices=["text", "json"])
    roles.add_argument("--out", "-o")
    roles.add_argument("--save", help="also write the full role map JSON here")
    strict_group = roles.add_mutually_exclusive_group()
    strict_group.add_argument("--strict", dest="strict", action="store_true", default=None)
    strict_group.add_argument("--no-strict", dest="strict", action="store_false")
    roles.set_defaults(func=cmd_roles)

    driver = subparsers.add_parser("driver", help="list adapters or check required drivers")
    driver.add_argument("action", choices=["list", "check"])
    driver.add_argument("--require", help="comma separated driver families")
    driver.add_argument("--spec", help="roles spec; its role drivers become required")
    driver.add_argument("--dry-run", action="store_true", default=True)
    _add_backend_flags(driver)
    driver.add_argument("--format", "-f", default="text", choices=["text", "json"])
    driver.add_argument("--out", "-o")
    driver.set_defaults(func=cmd_driver)

    clear = subparsers.add_parser("clearusb", help="preview or remove Windows ghost devices")
    clear.add_argument("--apply", action="store_true", help="actually remove (default: preview)")
    clear.add_argument("--board", help="limit to one USB VID:PID, e.g. 1a86:7523")
    clear.add_argument("--timeout", type=int, default=clearusb.DEFAULT_TIMEOUT)
    clear.add_argument("--format", "-f", default="text", choices=["text", "json"])
    clear.add_argument("--out", "-o")
    clear.set_defaults(func=cmd_clearusb)

    doctor = subparsers.add_parser("doctor", help="report backend availability")
    doctor.add_argument("--format", "-f", default="text", choices=["text", "json"])
    doctor.add_argument("--out", "-o")
    doctor.set_defaults(func=cmd_doctor)

    sch = subparsers.add_parser("schema", help="print a versioned JSON Schema")
    sch.add_argument("name", nargs="?", help="snapshot | roles-spec | role-map | clear-plan | driver-check")
    sch.add_argument("--list", action="store_true")
    sch.add_argument("--out", "-o")
    sch.set_defaults(func=cmd_schema)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SpecError as exc:
        print("spec error: {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except schema.SchemaError as exc:
        print("schema error: {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except BackendUnavailable as exc:
        print("backend unavailable: {0}".format(exc), file=sys.stderr)
        return EXIT_BACKEND
    except BackendError as exc:
        print("backend error: {0}".format(exc), file=sys.stderr)
        return EXIT_BACKEND
    except clearusb.ClearUsbError as exc:
        print("clearusb error: {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
