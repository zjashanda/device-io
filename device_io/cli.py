"""CLI: ``device-io <command>``.

Exit codes are part of the contract so a shell caller can branch without
parsing text:

0  success
1  usage / contract error (bad spec, unreadable fixture, unknown format)
2  resolution failed (a required role is missing/ambiguous/conflicting)
3  operation unsupported on this platform (e.g. clearusb on Linux)
4  backend unavailable (wrong OS, missing collector, PowerShell absent, PDU unreachable)
5  write accepted but not verified (the read-back never observed the requested state)
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
EXIT_UNVERIFIED = 5

_PDU_ACTIONS = ("status", "outlet-status", "power-on", "power-off", "power-cycle", "doctor", "plan")


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


def cmd_pdu(args: argparse.Namespace) -> int:
    """Generic PDU power control: dry-run by default, verified writes only."""
    from .pdu import adapter as pdu, contract, credentials, modbus
    from .pdu import text as pdu_text
    from .pdu.snmp import SnmpError

    def emit(document):
        if args.format == "json":
            _emit(render.dumps(document), args.out)
        else:
            renderer = getattr(pdu_text, "render_" + args.action.replace("-", "_"), None)
            if renderer is None:
                renderer = pdu_text.render_plan
            _emit(renderer(document), args.out)

    if args.action == "doctor":
        document = {
            "schema": contract.SCHEMA_STATUS,
            "generated_at": schema.now_bj(),
            "backends": pdu.backend_report(),
            "encoding": {
                "read": {"1": "off", "2": "on"},
                "write": {"1": "on", "2": "off"},
                "inverted": True,
            },
            "credential": dict(credentials.describe_sources(secret_file=args.community_file), value="***"),
            "oid_table": contract.oid_table(args.outlet),
            "warnings": [
                "power-cycle requires --apply and --confirm-write",
                "all-sockets writes are refused by design",
            ],
        }
        if args.format == "json":
            _emit(render.dumps(document), args.out)
        else:
            _emit(pdu_text.render_doctor(document), args.out)
        return EXIT_OK

    if args.pdu_backend == "modbus":
        refusal = modbus.refusal()
        refusal["generated_at"] = schema.now_bj()
        if args.format == "json":
            _emit(render.dumps(refusal), args.out)
        else:
            lines = ["Modbus RTU 后端不可用：{0}".format(refusal["reason"])]
            lines.extend("  缺少：{0}".format(item) for item in refusal["unknown"])
            lines.extend("  启用前置：{0}".format(item) for item in refusal["required_before_enable"])
            _emit("\n".join(lines), args.out)
        return EXIT_UNSUPPORTED

    host = contract.validate_host(args.host)
    # Validate the outlet and the second switch before any credential or socket
    # work, so a bad invocation fails on the argument it got wrong.
    if args.action in ("outlet-status", "power-on", "power-off", "power-cycle"):
        outlet = contract.validate_outlet(args.outlet)
    else:
        outlet = contract.validate_outlet(args.outlet) if args.outlet is not None else None
    if args.action == "power-cycle" and args.apply and not args.confirm_write:
        raise contract.PduContractError(
            "power-cycle cuts power: pass --confirm-write together with --apply"
        )
    community = None
    if args.simulate:
        transport = pdu.simulated_transport(pdu.parse_simulated_states(args.simulate_state))
        client = pdu.build_client(host, args.port, transport=transport, timeout=args.snmp_timeout)
        transport_desc = "memory (simulated PDU, no network)"
        probe_http = False
    elif args.action == "plan" or (args.action in ("power-on", "power-off", "power-cycle") and not args.apply):
        client = None
        transport_desc = "offline (plan only)"
        probe_http = False
    else:
        community = credentials.resolve_community(secret_file=args.community_file)
        client = pdu.build_client(
            host,
            args.port,
            community=community,
            timeout=args.snmp_timeout,
            attempts=args.snmp_attempts,
        )
        transport_desc = client.transport.describe()
        probe_http = not args.no_probe

    pdu_adapter = pdu.PduAdapter(
        client,
        host,
        port=args.port,
        outlet=outlet,
        community=community,
        transport_desc=transport_desc,
        probe_http=probe_http,
        poll_timeout=args.timeout,
        poll_interval=args.interval,
    )
    try:
        if args.action == "status":
            document = pdu_adapter.status([outlet] if outlet is not None else None)
            emit_status = True
        elif args.action == "outlet-status":
            if outlet is None:
                raise contract.PduContractError("outlet-status needs --outlet 1..8")
            document = pdu_adapter.status([outlet])
            document["outlets"] = [pdu_adapter.read_outlet(outlet)]
            emit_status = True
        elif args.action == "plan":
            action = args.plan_action or "power-cycle"
            document = pdu_adapter.plan(action)
            emit_status = False
        elif args.action == "power-on":
            document = pdu_adapter.set_state("on", apply=args.apply, timeout_s=args.timeout)
            emit_status = False
        elif args.action == "power-off":
            document = pdu_adapter.set_state("off", apply=args.apply, timeout_s=args.timeout)
            emit_status = False
        elif args.action == "power-cycle":
            document = pdu_adapter.power_cycle(
                apply=args.apply,
                confirm_write=args.confirm_write,
                hold_s=args.hold,
                timeout_s=args.timeout,
                restore_attempts=args.restore_attempts,
            )
            emit_status = False
        else:  # pragma: no cover - argparse restricts the choices
            raise contract.PduContractError("unknown pdu action {0!r}".format(args.action))
    except SnmpError as exc:
        print("pdu backend error: {0}".format(credentials.scrub(str(exc), [community] if community else [])), file=sys.stderr)
        return EXIT_BACKEND
    finally:
        pdu_adapter.close()

    if args.format == "json":
        _emit(render.dumps(document), args.out)
    elif emit_status:
        _emit(pdu_text.render_outlet(document) if args.action == "outlet-status" else pdu_text.render_status(document), args.out)
    else:
        _emit(pdu_text.render_plan(document), args.out)
    if document.get("apply") and document.get("executed") and not document.get("verified"):
        return EXIT_UNVERIFIED
    if document.get("apply") and not document.get("verified") and not document.get("executed"):
        return EXIT_OK
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

    pdu = subparsers.add_parser(
        "pdu",
        help="generic PDU power control over SNMP (dry-run by default; writes need --apply)",
        description=(
            "Generic PDU power control adapted from pudCorntrol.rar. Read-only unless --apply is "
            "given; power-cycle also needs --confirm-write. The outlet is always explicit and "
            "all-sockets writes are refused. Every write is verified by reading the socket back."
        ),
    )
    pdu.add_argument("action", choices=list(_PDU_ACTIONS))
    pdu.add_argument("--host", "--pdu-ip", dest="host", help="PDU address (required except for doctor)")
    pdu.add_argument("--port", type=int, default=161, help="SNMP UDP port (default 161)")
    pdu.add_argument("--outlet", "-n", type=int, help="socket number 1..8 (required for writes)")
    pdu.add_argument("--apply", action="store_true", help="actually send the write (default: dry-run)")
    pdu.add_argument("--confirm-write", action="store_true", help="second switch required by power-cycle")
    pdu.add_argument("--hold", type=float, default=10.0, help="power-cycle off window in seconds (vendor waitTime 10)")
    pdu.add_argument("--timeout", type=float, default=30.0, help="read-back poll budget per transition (seconds)")
    pdu.add_argument("--interval", type=float, default=1.0, help="read-back poll interval (seconds)")
    pdu.add_argument("--snmp-timeout", type=float, default=2.0, help="per-request SNMP timeout")
    pdu.add_argument("--snmp-attempts", type=int, default=2, help="SNMP datagram attempts")
    pdu.add_argument("--restore-attempts", type=int, default=2, help="power-cycle restore retries")
    pdu.add_argument("--no-probe", action="store_true", help="skip the HTTP pre-flight probe")
    pdu.add_argument("--pdu-backend", default="snmp", choices=["snmp", "modbus"], help="PDU backend (modbus is BLOCKED: serial parameters unknown)")
    pdu.add_argument("--community-file", help="secret file with the SNMP community (mode 600)")
    pdu.add_argument("--simulate", action="store_true", help="use the in-process simulated PDU (never opens a socket)")
    pdu.add_argument("--simulate-state", help="with --simulate: 1=on,2=off,...")
    pdu.add_argument("--plan-action", choices=["power-on", "power-off", "power-cycle"], help="action for `pdu plan`")
    pdu.add_argument("--format", "-f", default="text", choices=["text", "json"])
    pdu.add_argument("--out", "-o")
    pdu.set_defaults(func=cmd_pdu)

    doctor = subparsers.add_parser("doctor", help="report backend availability")
    doctor.add_argument("--format", "-f", default="text", choices=["text", "json"])
    doctor.add_argument("--out", "-o")
    doctor.set_defaults(func=cmd_doctor)

    sch = subparsers.add_parser("schema", help="print a versioned JSON Schema")
    sch.add_argument(
        "name",
        nargs="?",
        help="snapshot | roles-spec | role-map | clear-plan | driver-check | pdu-status | pdu-plan",
    )
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
