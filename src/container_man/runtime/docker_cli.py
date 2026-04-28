from __future__ import annotations

import json
import subprocess
from pathlib import Path

from container_man.models import ContainerSummary, VolumeSummary


class DockerCommandError(RuntimeError):
    """Raised when a docker command fails."""


class DockerCliRuntime:
    def _run_docker(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                ["docker", *args],
                check=True,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise DockerCommandError("docker binary not found in PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip()
            raise DockerCommandError(
                f"docker {' '.join(args)} failed: {stderr or 'unknown error'}"
            ) from exc
        return proc.stdout

    def _run_docker_bytes(self, *args: str) -> bytes:
        try:
            proc = subprocess.run(
                ["docker", *args],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise DockerCommandError("docker binary not found in PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace").strip()
            raise DockerCommandError(
                f"docker {' '.join(args)} failed: {stderr or 'unknown error'}"
            ) from exc
        return proc.stdout

    def list_containers(self) -> list[ContainerSummary]:
        raw = self._run_docker(
            "ps",
            "-a",
            "--format",
            "{{json .}}",
        )
        containers: list[ContainerSummary] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            containers.append(
                ContainerSummary(
                    container_id=row.get("ID", ""),
                    name=row.get("Names", ""),
                    status=row.get("Status", ""),
                )
            )
        return containers

    def list_volumes(self) -> list[VolumeSummary]:
        raw = self._run_docker(
            "volume",
            "ls",
            "--format",
            "{{json .}}",
        )
        volumes: list[VolumeSummary] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            name = row.get("Name", "")
            inspect_raw = self._run_docker("volume", "inspect", name)
            inspect = json.loads(inspect_raw)[0]
            volumes.append(
                VolumeSummary(
                    name=name,
                    driver=row.get("Driver", ""),
                    mountpoint=inspect.get("Mountpoint", ""),
                    scope=inspect.get("Scope", ""),
                )
            )
        return volumes

    def volume_usage(self) -> dict[str, list[str]]:
        containers_raw = self._run_docker("ps", "-a", "-q")
        container_ids = [line.strip() for line in containers_raw.splitlines() if line.strip()]
        usage: dict[str, list[str]] = {}
        for container_id in container_ids:
            inspect_raw = self._run_docker("inspect", container_id)
            data = json.loads(inspect_raw)[0]
            container_name = data.get("Name", "").lstrip("/") or container_id
            mounts = data.get("Mounts", [])
            for mount in mounts:
                if mount.get("Type") != "volume":
                    continue
                volume_name = mount.get("Name")
                if not volume_name:
                    continue
                usage.setdefault(volume_name, []).append(container_name)
        return usage

    def inspect_container(self, container_ref: str) -> dict:
        inspect_raw = self._run_docker("inspect", container_ref)
        return json.loads(inspect_raw)[0]

    def inspect_volume(self, volume_name: str) -> dict:
        inspect_raw = self._run_docker("volume", "inspect", volume_name)
        return json.loads(inspect_raw)[0]

    def container_named_volumes(self, container_ref: str) -> list[str]:
        data = self.inspect_container(container_ref)
        volumes: list[str] = []
        for mount in data.get("Mounts", []):
            if mount.get("Type") != "volume":
                continue
            name = mount.get("Name")
            if name:
                volumes.append(name)
        return sorted(set(volumes))

    def containers_using_volume(self, volume_name: str) -> list[str]:
        usage = self.volume_usage()
        return sorted(set(usage.get(volume_name, [])))

    def is_container_running(self, container_ref: str) -> bool:
        data = self.inspect_container(container_ref)
        state = data.get("State", {})
        return bool(state.get("Running", False))

    def stop_container(self, container_ref: str, timeout_seconds: int = 20) -> None:
        self._run_docker("stop", "-t", str(timeout_seconds), container_ref)

    def start_container(self, container_ref: str) -> None:
        self._run_docker("start", container_ref)

    def commit_container(self, container_ref: str, image_ref: str) -> None:
        self._run_docker("commit", container_ref, image_ref)

    def remove_image(self, image_ref: str, *, force: bool = False) -> None:
        args = ["rmi"]
        if force:
            args.append("--force")
        args.append(image_ref)
        self._run_docker(*args)

    def save_image(self, image_ref: str, output_path: str) -> None:
        out = Path(output_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        self._run_docker("save", "-o", str(out), image_ref)

    def load_image(self, input_path: str) -> str:
        data = self._run_docker_bytes("load", "-i", input_path)
        return data.decode("utf-8", errors="replace").strip()

    def create_volume(self, volume_name: str) -> None:
        self._run_docker("volume", "create", volume_name)

    def create_container(self, args: list[str]) -> str:
        return self._run_docker("create", *args).strip()

    def run(self, *args: str) -> str:
        return self._run_docker(*args)
