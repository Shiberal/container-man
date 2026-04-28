from __future__ import annotations

import json
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from container_man.runtime.docker_cli import DockerCliRuntime
from container_man.services.udp_transfer import TransferResult, UdpTransferService

IMAGE_ARCHIVE_NAME = "image.tar"
METADATA_NAME = "metadata.json"
HELPER_IMAGE = "alpine:3.20"


class ContainerTransferError(RuntimeError):
    """Raised when container transfer cannot be completed safely."""


@dataclass(slots=True)
class ContainerSendResult:
    package_path: str
    transfer: TransferResult


@dataclass(slots=True)
class ContainerReceiveResult:
    package_path: str
    container_id: str
    container_name: str
    image_ref: str
    transfer: TransferResult


class ContainerTransferService:
    def __init__(self, runtime: DockerCliRuntime, udp_service: UdpTransferService | None = None) -> None:
        self.runtime = runtime
        self.udp = udp_service or UdpTransferService()

    def send_container(
        self,
        container: str,
        destination_host: str,
        destination_port: int,
        *,
        package_path: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        timeout_seconds: float = 1.0,
        retries: int = 10,
        chunk_size: int = 1200,
    ) -> ContainerSendResult:
        package = Path(package_path).expanduser().resolve()
        package.parent.mkdir(parents=True, exist_ok=True)
        self.export_container_package(container, str(package))
        transfer = self.udp.send_file(
            source_path=str(package),
            destination_host=destination_host,
            destination_port=destination_port,
            bind_host=bind_host,
            bind_port=bind_port,
            timeout_seconds=timeout_seconds,
            max_retries=retries,
            chunk_size=chunk_size,
        )
        return ContainerSendResult(package_path=str(package), transfer=transfer)

    def receive_container(
        self,
        *,
        package_path: str,
        bind_host: str = "0.0.0.0",
        bind_port: int,
        overwrite_package: bool = False,
        timeout_seconds: float | None = None,
        container_name: str | None = None,
    ) -> ContainerReceiveResult:
        package = Path(package_path).expanduser().resolve()
        package.parent.mkdir(parents=True, exist_ok=True)
        transfer = self.udp.receive_file(
            output_path=str(package),
            bind_host=bind_host,
            bind_port=bind_port,
            overwrite=overwrite_package,
            timeout_seconds=timeout_seconds,
        )
        restored = self.import_container_package(str(package), container_name=container_name)
        return ContainerReceiveResult(
            package_path=str(package),
            container_id=restored["container_id"],
            container_name=restored["container_name"],
            image_ref=restored["image_ref"],
            transfer=transfer,
        )

    def export_container_package(self, container: str, output_package_path: str) -> None:
        inspect = self.runtime.inspect_container(container)
        mounts = inspect.get("Mounts", [])
        bind_mounts = [mount for mount in mounts if mount.get("Type") == "bind"]
        if bind_mounts:
            raise ContainerTransferError(
                "Bind mounts are not supported for full-container transfer. "
                "Use named volumes only."
            )

        container_name = inspect.get("Name", "").lstrip("/") or container
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        image_ref = f"cm-transfer/{container_name}:{timestamp}"

        with self._temporary_workspace("cm-transfer-export-") as tmp:
            tmp_dir = Path(tmp)
            image_tar = tmp_dir / IMAGE_ARCHIVE_NAME
            volumes_dir = tmp_dir / "volumes"
            volumes_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = tmp_dir / METADATA_NAME

            self.runtime.commit_container(container, image_ref)
            try:
                self.runtime.save_image(image_ref, str(image_tar))
            finally:
                self.runtime.remove_image(image_ref, force=True)

            volume_entries: list[dict[str, str]] = []
            for mount in mounts:
                if mount.get("Type") != "volume":
                    continue
                name = mount.get("Name")
                if not name:
                    continue
                archive_rel = f"volumes/{name}.tar"
                archive_file = tmp_dir / archive_rel
                self._archive_volume(name, archive_file)
                volume_entries.append(
                    {
                        "name": name,
                        "destination": mount.get("Destination", ""),
                        "readonly": str(bool(mount.get("RW") is False)).lower(),
                        "archive": archive_rel,
                    }
                )

            metadata = {
                "schema": 1,
                "container_name": container_name,
                "image_ref": image_ref,
                "config": self._build_runtime_config(inspect),
                "volumes": volume_entries,
                "was_running": bool(inspect.get("State", {}).get("Running", False)),
            }
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

            out = Path(output_package_path).expanduser().resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out, mode="w:gz") as package:
                package.add(metadata_path, arcname=METADATA_NAME)
                package.add(image_tar, arcname=IMAGE_ARCHIVE_NAME)
                for volume in volume_entries:
                    package.add(tmp_dir / volume["archive"], arcname=volume["archive"])

    def import_container_package(
        self,
        package_path: str,
        *,
        container_name: str | None = None,
    ) -> dict[str, str]:
        package = Path(package_path).expanduser().resolve()
        if not package.exists():
            raise ContainerTransferError(f"Package '{package}' not found.")

        with self._temporary_workspace("cm-transfer-import-") as tmp:
            tmp_dir = Path(tmp)
            with tarfile.open(package, mode="r:gz") as archive:
                archive.extractall(path=tmp_dir)

            metadata_file = tmp_dir / METADATA_NAME
            if not metadata_file.exists():
                raise ContainerTransferError(f"Package missing {METADATA_NAME}.")
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            image_ref = str(metadata["image_ref"])
            self.runtime.load_image(str(tmp_dir / IMAGE_ARCHIVE_NAME))

            volumes = metadata.get("volumes", [])
            for volume in volumes:
                name = str(volume["name"])
                self.runtime.create_volume(name)
                self._extract_archive_to_volume(tmp_dir / str(volume["archive"]), name)

            target_name = container_name or str(metadata.get("container_name", "cm-imported"))
            run_args = self._build_container_create_args(
                image_ref=image_ref,
                container_name=target_name,
                config=metadata.get("config", {}),
                volumes=volumes,
            )
            container_id = self.runtime.create_container(run_args)
            if bool(metadata.get("was_running", False)):
                self.runtime.start_container(container_id)

            return {
                "container_id": container_id,
                "container_name": target_name,
                "image_ref": image_ref,
            }

    def _archive_volume(self, volume_name: str, target_archive: Path) -> None:
        target_archive.parent.mkdir(parents=True, exist_ok=True)
        host_dir = str(target_archive.parent.resolve())
        archive_name = target_archive.name
        self.runtime.run(
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/volume:ro",
            "-v",
            f"{host_dir}:/backup",
            HELPER_IMAGE,
            "sh",
            "-c",
            f"tar -cf /backup/{archive_name} -C /volume .",
        )

    def _extract_archive_to_volume(self, archive_path: Path, volume_name: str) -> None:
        archive = archive_path.resolve()
        if not archive.exists():
            raise ContainerTransferError(f"Archive '{archive}' not found for volume '{volume_name}'.")
        host_dir = str(archive.parent)
        archive_name = archive.name
        self.runtime.run(
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/volume",
            "-v",
            f"{host_dir}:/backup:ro",
            HELPER_IMAGE,
            "sh",
            "-c",
            f"tar -xf /backup/{archive_name} -C /volume",
        )

    def _temporary_workspace(self, prefix: str) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(Path.home()))

    def _build_runtime_config(self, inspect: dict) -> dict:
        config = inspect.get("Config", {})
        host = inspect.get("HostConfig", {})
        return {
            "env": config.get("Env") or [],
            "cmd": config.get("Cmd") or [],
            "entrypoint": config.get("Entrypoint") or [],
            "working_dir": config.get("WorkingDir") or "",
            "user": config.get("User") or "",
            "labels": config.get("Labels") or {},
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "",
            "port_bindings": host.get("PortBindings") or {},
            "network_mode": host.get("NetworkMode") or "",
        }

    def _build_container_create_args(
        self,
        *,
        image_ref: str,
        container_name: str,
        config: dict,
        volumes: list[dict],
    ) -> list[str]:
        args: list[str] = ["--name", container_name]
        for env in config.get("env", []):
            args.extend(["-e", str(env)])
        for key, value in (config.get("labels") or {}).items():
            args.extend(["--label", f"{key}={value}"])
        user = str(config.get("user") or "")
        if user:
            args.extend(["--user", user])
        working_dir = str(config.get("working_dir") or "")
        if working_dir:
            args.extend(["--workdir", working_dir])
        restart = str(config.get("restart_policy") or "")
        if restart and restart != "no":
            args.extend(["--restart", restart])
        network_mode = str(config.get("network_mode") or "")
        if network_mode and network_mode != "default":
            args.extend(["--network", network_mode])

        for container_port, bindings in (config.get("port_bindings") or {}).items():
            for bind in bindings or []:
                host_ip = str(bind.get("HostIp") or "")
                host_port = str(bind.get("HostPort") or "")
                left = ""
                if host_ip:
                    left += f"{host_ip}:"
                if host_port:
                    left += host_port
                mapping = f"{left}:{container_port}" if left else container_port
                args.extend(["-p", mapping])

        for volume in volumes:
            name = str(volume["name"])
            destination = str(volume.get("destination", ""))
            readonly = str(volume.get("readonly", "false")) == "true"
            if not destination:
                continue
            mount = f"{name}:{destination}"
            if readonly:
                mount += ":ro"
            args.extend(["-v", mount])

        entrypoint = config.get("entrypoint") or []
        if entrypoint:
            args.extend(["--entrypoint", " ".join(str(item) for item in entrypoint)])

        args.append(image_ref)
        cmd = config.get("cmd") or []
        args.extend(str(part) for part in cmd)
        return args
