from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from container_man.runtime.docker_cli import DockerCliRuntime, DockerCommandError
from container_man.services.container_transfer import (
    ContainerReceiveResult,
    ReceiveVerification,
    ContainerSendResult,
    ContainerTransferError,
    ContainerTransferService,
)
from container_man.services.filesystem import FilesystemProbe
from container_man.services.migration import MigrationError, MigrationService
from container_man.services.topology import VolumeTopologyService
from container_man.services.udp_transfer import UdpTransferError, UdpTransferService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cm", description="Container manager")
    parser.add_argument("--json", action="store_true", dest="as_json")

    subparsers = parser.add_subparsers(dest="entity", required=True)

    containers = subparsers.add_parser("containers")
    containers_sub = containers.add_subparsers(dest="action", required=True)
    containers_sub.add_parser("list")
    containers_migrate = containers_sub.add_parser("migrate")
    containers_migrate.add_argument("--container", required=True)
    containers_migrate.add_argument("--target", required=True)
    containers_migrate.add_argument("--apply", action="store_true")
    containers_migrate.add_argument("--yes", action="store_true")

    volumes = subparsers.add_parser("volumes")
    volumes_sub = volumes.add_subparsers(dest="action", required=True)
    volumes_sub.add_parser("list")
    volumes_sub.add_parser("map")
    volumes_migrate = volumes_sub.add_parser("migrate")
    volumes_migrate.add_argument("--volume", required=True)
    volumes_migrate.add_argument("--target", required=True)
    volumes_migrate.add_argument("--apply", action="store_true")
    volumes_migrate.add_argument("--yes", action="store_true")

    transfer = subparsers.add_parser("transfer")
    transfer_sub = transfer.add_subparsers(dest="action", required=True)
    transfer_send = transfer_sub.add_parser("send")
    transfer_send.add_argument("--source", required=True)
    transfer_send.add_argument("--host", required=True)
    transfer_send.add_argument("--port", required=True, type=int)
    transfer_send.add_argument("--bind-host", default="0.0.0.0")
    transfer_send.add_argument("--bind-port", default=0, type=int)
    transfer_send.add_argument("--chunk-size", default=1200, type=int)
    transfer_send.add_argument("--timeout", default=1.0, type=float)
    transfer_send.add_argument("--retries", default=10, type=int)

    transfer_receive = transfer_sub.add_parser("receive")
    transfer_receive.add_argument("--output", required=True)
    transfer_receive.add_argument("--bind-host", default="0.0.0.0")
    transfer_receive.add_argument("--port", required=True, type=int)
    transfer_receive.add_argument("--overwrite", action="store_true")
    transfer_receive.add_argument("--timeout", type=float)

    containers_transfer = containers_sub.add_parser("transfer")
    containers_transfer_sub = containers_transfer.add_subparsers(dest="transfer_action", required=True)
    containers_transfer_send = containers_transfer_sub.add_parser("send")
    containers_transfer_send.add_argument("--container", required=True)
    containers_transfer_send.add_argument("--host", required=True)
    containers_transfer_send.add_argument("--port", default=9000, type=int)
    containers_transfer_send.add_argument("--package")
    containers_transfer_send.add_argument("--bind-host", default="0.0.0.0")
    containers_transfer_send.add_argument("--bind-port", default=0, type=int)
    containers_transfer_send.add_argument("--chunk-size", default=1200, type=int)
    containers_transfer_send.add_argument("--timeout", default=1.0, type=float)
    containers_transfer_send.add_argument("--retries", default=10, type=int)
    containers_transfer_send.add_argument("--wait-confirm-port", type=int)
    containers_transfer_send.add_argument("--confirm-timeout", default=120.0, type=float)
    containers_transfer_send.add_argument("--move", action="store_true")
    containers_transfer_send.add_argument("--yes", action="store_true")

    containers_transfer_receive = containers_transfer_sub.add_parser("receive")
    containers_transfer_receive.add_argument("--package")
    containers_transfer_receive.add_argument("--port", default=9000, type=int)
    containers_transfer_receive.add_argument("--bind-host", default="0.0.0.0")
    containers_transfer_receive.add_argument("--overwrite-package", action="store_true")
    containers_transfer_receive.add_argument("--timeout", type=float)
    containers_transfer_receive.add_argument("--name")
    containers_transfer_receive.add_argument("--confirm-host")
    containers_transfer_receive.add_argument("--confirm-port", type=int)

    return parser


def print_table(rows: list[dict[str, str]]) -> None:
    if not rows:
        print("No results.")
        return
    columns = list(rows[0].keys())
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(str(row[col])))
    header = "  ".join(col.ljust(widths[col]) for col in columns)
    print(header)
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(str(row[col]).ljust(widths[col]) for col in columns))


def cmd_containers_list(runtime: DockerCliRuntime, as_json: bool) -> None:
    containers = sorted(runtime.list_containers(), key=lambda item: item.name)
    payload = [asdict(c) for c in containers]
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    rows = [
        {
            "id": c["container_id"],
            "name": c["name"],
            "status": c["status"],
        }
        for c in payload
    ]
    print_table(rows)


def cmd_volumes_list(runtime: DockerCliRuntime, as_json: bool) -> None:
    volumes = sorted(runtime.list_volumes(), key=lambda item: item.name)
    payload = [asdict(v) for v in volumes]
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    rows = [
        {
            "name": v["name"],
            "driver": v["driver"],
            "scope": v["scope"],
            "mountpoint": v["mountpoint"],
        }
        for v in payload
    ]
    print_table(rows)


def cmd_volumes_map(runtime: DockerCliRuntime, as_json: bool) -> None:
    service = VolumeTopologyService(runtime=runtime, fs_probe=FilesystemProbe())
    payload = [asdict(item) for item in service.build()]
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    rows = [
        {
            "name": item["name"],
            "device": item["device"],
            "fs_mount": item["filesystem_mount"],
            "docker_mountpoint": item["docker_mountpoint"],
            "resolved_data_path": item["resolved_data_path"],
            "used_by": ",".join(item["used_by"]) if item["used_by"] else "-",
        }
        for item in payload
    ]
    print_table(rows)


def _render_plan_payload(plan: object) -> dict:
    payload = asdict(plan)
    payload["volumes"] = sorted(payload["volumes"], key=lambda item: item["volume"])
    return payload


def _print_migration_plan(plan_payload: dict) -> None:
    print(f"mode: {plan_payload['mode']}")
    print(f"target_root: {plan_payload['target_root']}")
    print(f"required_bytes: {plan_payload['required_bytes']}")
    print(f"free_bytes: {plan_payload['free_bytes']}")
    if plan_payload["warnings"]:
        print("warnings:")
        for warning in plan_payload["warnings"]:
            print(f"  - {warning}")
    print("containers_to_stop:")
    if plan_payload["containers_to_stop"]:
        for container in plan_payload["containers_to_stop"]:
            print(f"  - {container}")
    else:
        print("  - (none)")
    rows = []
    for item in plan_payload["volumes"]:
        rows.append(
            {
                "volume": item["volume"],
                "size_bytes": str(item["size_bytes"]),
                "source_data_path": item["source_data_path"],
                "target_data_path": item["target_data_path"],
                "used_by": ",".join(item["containers"]) if item["containers"] else "-",
            }
        )
    print("volumes:")
    print_table(rows)


def cmd_volumes_migrate(runtime: DockerCliRuntime, args: argparse.Namespace) -> int:
    service = MigrationService(runtime)
    plan = service.plan_for_volume(args.volume, args.target)
    payload = _render_plan_payload(plan)
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        _print_migration_plan(payload)
    if not args.apply:
        return 0
    if not args.yes:
        print("error: --apply requires --yes confirmation", file=sys.stderr)
        return 2
    service.apply(plan)
    print("Migration completed.")
    return 0


def cmd_containers_migrate(runtime: DockerCliRuntime, args: argparse.Namespace) -> int:
    service = MigrationService(runtime)
    plan = service.plan_for_container(args.container, args.target)
    payload = _render_plan_payload(plan)
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        _print_migration_plan(payload)
    if not args.apply:
        return 0
    if not args.yes:
        print("error: --apply requires --yes confirmation", file=sys.stderr)
        return 2
    service.apply(plan)
    print("Migration completed.")
    return 0


def cmd_transfer_send(args: argparse.Namespace) -> int:
    service = UdpTransferService()
    result = service.send_file(
        source_path=args.source,
        destination_host=args.host,
        destination_port=args.port,
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        timeout_seconds=args.timeout,
        max_retries=args.retries,
        chunk_size=args.chunk_size,
    )
    payload = asdict(result)
    if args.as_json:
        print(json.dumps(payload, indent=2))
        return 0
    rows = [
        {"metric": "source", "value": payload["source"]},
        {"metric": "destination", "value": payload["destination"]},
        {"metric": "bytes_transferred", "value": str(payload["bytes_transferred"])},
        {"metric": "chunks", "value": str(payload["chunks"])},
        {"metric": "duration_seconds", "value": f"{payload['duration_seconds']:.3f}"},
        {"metric": "sha256", "value": payload["sha256"]},
    ]
    print_table(rows)
    return 0


def cmd_transfer_receive(args: argparse.Namespace) -> int:
    service = UdpTransferService()
    result = service.receive_file(
        output_path=args.output,
        bind_host=args.bind_host,
        bind_port=args.port,
        overwrite=args.overwrite,
        timeout_seconds=args.timeout,
    )
    payload = asdict(result)
    if args.as_json:
        print(json.dumps(payload, indent=2))
        return 0
    rows = [
        {"metric": "source", "value": payload["source"]},
        {"metric": "destination", "value": payload["destination"]},
        {"metric": "bytes_transferred", "value": str(payload["bytes_transferred"])},
        {"metric": "chunks", "value": str(payload["chunks"])},
        {"metric": "duration_seconds", "value": f"{payload['duration_seconds']:.3f}"},
        {"metric": "sha256", "value": payload["sha256"]},
    ]
    print_table(rows)
    return 0


def _render_container_transfer_payload(result: ContainerSendResult | ContainerReceiveResult) -> dict:
    payload = asdict(result)
    payload["transfer"] = asdict(result.transfer)
    return payload


def _render_verification_payload(verification: ReceiveVerification) -> dict[str, str]:
    return {
        "ok": str(verification.ok).lower(),
        "running": str(verification.running).lower(),
        "healthy": str(verification.healthy).lower(),
        "details": verification.details,
    }


def _default_sender_package(container: str) -> str:
    return str((Path("/tmp") / f"{container}.cm.tgz").resolve())


def _default_receiver_package() -> str:
    return str((Path("/tmp") / "cm-received-container.cm.tgz").resolve())


def cmd_containers_transfer_send(runtime: DockerCliRuntime, args: argparse.Namespace) -> int:
    service = ContainerTransferService(runtime=runtime)
    if args.move:
        if not args.yes:
            print("error: --move requires --yes confirmation", file=sys.stderr)
            return 2
        if not args.wait_confirm_port:
            print("error: --move requires --wait-confirm-port", file=sys.stderr)
            return 2
    result = service.send_container(
        container=args.container,
        destination_host=args.host,
        destination_port=args.port,
        package_path=args.package or _default_sender_package(args.container),
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        timeout_seconds=args.timeout,
        retries=args.retries,
        chunk_size=args.chunk_size,
    )
    payload = _render_container_transfer_payload(result)
    if args.as_json:
        print(json.dumps(payload, indent=2))
        return 0
    rows = [
        {"metric": "package_path", "value": payload["package_path"]},
        {"metric": "container", "value": args.container},
        {"metric": "destination", "value": payload["transfer"]["destination"]},
        {"metric": "bytes_transferred", "value": str(payload["transfer"]["bytes_transferred"])},
        {"metric": "chunks", "value": str(payload["transfer"]["chunks"])},
        {"metric": "sha256", "value": payload["transfer"]["sha256"]},
    ]
    if args.wait_confirm_port:
        verification = service.wait_for_move_confirmation(
            bind_host=args.bind_host,
            bind_port=args.wait_confirm_port,
            timeout_seconds=args.confirm_timeout,
        )
        if not verification.ok:
            raise ContainerTransferError(
                f"Receiver did not verify container as healthy: {verification.details}"
            )
        rows.extend(
            [
                {"metric": "receiver_verified", "value": "true"},
                {"metric": "receiver_status", "value": verification.details},
            ]
        )
        if args.move:
            removed_volumes = service.remove_sender_container_after_move(args.container)
            rows.append({"metric": "source_removed", "value": "true"})
            rows.append(
                {
                    "metric": "source_volumes_removed",
                    "value": ",".join(removed_volumes) if removed_volumes else "-",
                }
            )
    print_table(rows)
    return 0


def cmd_containers_transfer_receive(runtime: DockerCliRuntime, args: argparse.Namespace) -> int:
    service = ContainerTransferService(runtime=runtime)
    result = service.receive_container(
        package_path=args.package or _default_receiver_package(),
        bind_host=args.bind_host,
        bind_port=args.port,
        overwrite_package=args.overwrite_package,
        timeout_seconds=args.timeout,
        container_name=args.name,
    )
    payload = _render_container_transfer_payload(result)
    verification = service.verify_container_running(result.container_id)
    if args.confirm_host and args.confirm_port:
        service.send_move_confirmation(
            sender_host=args.confirm_host,
            sender_port=args.confirm_port,
            container_name=result.container_name,
            verification=verification,
        )
    elif args.confirm_host or args.confirm_port:
        raise ContainerTransferError("Both --confirm-host and --confirm-port are required together.")
    if args.as_json:
        payload["verification"] = _render_verification_payload(verification)
        print(json.dumps(payload, indent=2))
        return 0
    rows = [
        {"metric": "package_path", "value": payload["package_path"]},
        {"metric": "container_name", "value": payload["container_name"]},
        {"metric": "container_id", "value": payload["container_id"]},
        {"metric": "image_ref", "value": payload["image_ref"]},
        {"metric": "source", "value": payload["transfer"]["source"]},
        {"metric": "bytes_transferred", "value": str(payload["transfer"]["bytes_transferred"])},
        {"metric": "verified_running", "value": str(verification.running).lower()},
        {"metric": "verified_healthy", "value": str(verification.healthy).lower()},
        {"metric": "verification_details", "value": verification.details},
    ]
    print_table(rows)
    if not verification.ok:
        raise ContainerTransferError(verification.details)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runtime: DockerCliRuntime | None = None
        if args.entity == "containers" and args.action == "list":
            runtime = runtime or DockerCliRuntime()
            cmd_containers_list(runtime, args.as_json)
            return 0
        if args.entity == "volumes" and args.action == "list":
            runtime = runtime or DockerCliRuntime()
            cmd_volumes_list(runtime, args.as_json)
            return 0
        if args.entity == "volumes" and args.action == "map":
            runtime = runtime or DockerCliRuntime()
            cmd_volumes_map(runtime, args.as_json)
            return 0
        if args.entity == "volumes" and args.action == "migrate":
            runtime = runtime or DockerCliRuntime()
            return cmd_volumes_migrate(runtime, args)
        if args.entity == "containers" and args.action == "migrate":
            runtime = runtime or DockerCliRuntime()
            return cmd_containers_migrate(runtime, args)
        if (
            args.entity == "containers"
            and args.action == "transfer"
            and args.transfer_action == "send"
        ):
            runtime = runtime or DockerCliRuntime()
            return cmd_containers_transfer_send(runtime, args)
        if (
            args.entity == "containers"
            and args.action == "transfer"
            and args.transfer_action == "receive"
        ):
            runtime = runtime or DockerCliRuntime()
            return cmd_containers_transfer_receive(runtime, args)
        if args.entity == "transfer" and args.action == "send":
            return cmd_transfer_send(args)
        if args.entity == "transfer" and args.action == "receive":
            return cmd_transfer_receive(args)
    except DockerCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except UdpTransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ContainerTransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
