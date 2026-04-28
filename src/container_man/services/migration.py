from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from container_man.runtime.docker_cli import DockerCliRuntime


class MigrationError(RuntimeError):
    """Raised when migration cannot proceed safely."""


@dataclass(slots=True)
class VolumeMoveItem:
    volume: str
    source_data_path: str
    target_data_path: str
    size_bytes: int
    containers: list[str]


@dataclass(slots=True)
class MigrationPlan:
    mode: str
    target_root: str
    volumes: list[VolumeMoveItem]
    containers_to_stop: list[str]
    required_bytes: int
    free_bytes: int
    warnings: list[str]


class MigrationService:
    def __init__(self, runtime: DockerCliRuntime) -> None:
        self.runtime = runtime

    def plan_for_volume(self, volume: str, target_root: str) -> MigrationPlan:
        target = Path(target_root).expanduser().resolve()
        source_data = self._resolve_volume_data_path(volume)
        size_bytes = self._directory_size_bytes(source_data)
        containers = self.runtime.containers_using_volume(volume)
        required_bytes = size_bytes
        free_bytes = shutil.disk_usage(target).free
        warnings = []
        if required_bytes > free_bytes:
            warnings.append("Insufficient free space on target disk.")
        item = VolumeMoveItem(
            volume=volume,
            source_data_path=str(source_data),
            target_data_path=str(target / volume),
            size_bytes=size_bytes,
            containers=containers,
        )
        return MigrationPlan(
            mode="volume",
            target_root=str(target),
            volumes=[item],
            containers_to_stop=sorted(set(containers)),
            required_bytes=required_bytes,
            free_bytes=free_bytes,
            warnings=warnings,
        )

    def plan_for_container(self, container: str, target_root: str) -> MigrationPlan:
        target = Path(target_root).expanduser().resolve()
        volumes = self.runtime.container_named_volumes(container)
        if not volumes:
            raise MigrationError(f"Container '{container}' has no named volumes.")
        volume_items: list[VolumeMoveItem] = []
        container_set: set[str] = set()
        for volume in volumes:
            source_data = self._resolve_volume_data_path(volume)
            size_bytes = self._directory_size_bytes(source_data)
            containers = self.runtime.containers_using_volume(volume)
            container_set.update(containers)
            volume_items.append(
                VolumeMoveItem(
                    volume=volume,
                    source_data_path=str(source_data),
                    target_data_path=str(target / volume),
                    size_bytes=size_bytes,
                    containers=containers,
                )
            )
        required_bytes = sum(item.size_bytes for item in volume_items)
        free_bytes = shutil.disk_usage(target).free
        warnings = []
        if required_bytes > free_bytes:
            warnings.append("Insufficient free space on target disk.")
        return MigrationPlan(
            mode="container",
            target_root=str(target),
            volumes=sorted(volume_items, key=lambda item: item.volume),
            containers_to_stop=sorted(container_set),
            required_bytes=required_bytes,
            free_bytes=free_bytes,
            warnings=warnings,
        )

    def apply(self, plan: MigrationPlan) -> None:
        if plan.warnings:
            raise MigrationError("Plan has blocking warnings. Resolve before apply.")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        stopped: list[str] = []
        backups: list[tuple[Path, Path]] = []
        created_symlinks: list[Path] = []
        created_targets: list[Path] = []
        try:
            for container in plan.containers_to_stop:
                if self.runtime.is_container_running(container):
                    self.runtime.stop_container(container)
                    stopped.append(container)

            # Phase 1: prepare and copy all volume data.
            for item in plan.volumes:
                source_data = Path(item.source_data_path)
                target_data = Path(item.target_data_path)
                target_data.parent.mkdir(parents=True, exist_ok=True)
                if target_data.exists() and any(target_data.iterdir()):
                    raise MigrationError(
                        f"Target path '{target_data}' exists and is not empty."
                    )
                if not target_data.exists():
                    target_data.mkdir(parents=True, exist_ok=True)
                    created_targets.append(target_data)
                self._copy_tree(source_data, target_data)

            # Phase 2: cutover only after every copy succeeds.
            for item in plan.volumes:
                source_data = Path(item.source_data_path)
                target_data = Path(item.target_data_path)
                backup_path = source_data.parent / f"{source_data.name}.bak.{timestamp}"
                source_data.rename(backup_path)
                backups.append((source_data, backup_path))
                source_data.symlink_to(target_data)
                created_symlinks.append(source_data)

            for container in stopped:
                self.runtime.start_container(container)
        except Exception as exc:
            self._rollback(stopped, backups, created_symlinks, created_targets)
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(str(exc)) from exc

    def _rollback(
        self,
        stopped: list[str],
        backups: list[tuple[Path, Path]],
        created_symlinks: list[Path],
        created_targets: list[Path],
    ) -> None:
        for symlink in created_symlinks:
            if symlink.is_symlink():
                symlink.unlink()
        for source_data, backup_path in reversed(backups):
            if backup_path.exists():
                backup_path.rename(source_data)
        for target in reversed(created_targets):
            if target.exists() and not any(target.iterdir()):
                target.rmdir()
        for container in stopped:
            try:
                self.runtime.start_container(container)
            except Exception:
                # Best-effort rollback for container state.
                continue

    def _resolve_volume_data_path(self, volume_name: str) -> Path:
        inspect = self.runtime.inspect_volume(volume_name)
        mountpoint = inspect.get("Mountpoint")
        if not mountpoint:
            raise MigrationError(f"Volume '{volume_name}' has no mountpoint.")
        path = Path(mountpoint).expanduser().resolve()
        try:
            if path.exists():
                return path
        except PermissionError as exc:
            raise MigrationError(
                "Cannot access Docker volume path due to permissions. "
                "For rootful Docker paths (for example /var/lib/docker/volumes), "
                "run migration with elevated privileges."
            ) from exc

        if str(path).startswith("/var/lib/docker/volumes/"):
            raise MigrationError(
                f"Volume mountpoint '{path}' is not visible from current user context. "
                "This usually means rootful Docker storage: run migration with elevated "
                "privileges (sudo) or execute from a root shell."
            )
        if not path.exists():
            raise MigrationError(f"Volume mountpoint '{path}' not found.")
        return path

    def _directory_size_bytes(self, directory: Path) -> int:
        size = 0
        for path in directory.rglob("*"):
            if path.is_file():
                size += path.stat().st_size
        return size

    def _copy_tree(self, src: Path, dst: Path) -> None:
        for path in src.rglob("*"):
            rel = path.relative_to(src)
            target = dst / rel
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_symlink():
                if target.exists():
                    target.unlink()
                target.symlink_to(path.readlink())
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
