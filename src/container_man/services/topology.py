from __future__ import annotations

from pathlib import Path

from container_man.models import VolumeTopology
from container_man.runtime.docker_cli import DockerCliRuntime
from container_man.services.filesystem import FilesystemProbe


class VolumeTopologyService:
    def __init__(self, runtime: DockerCliRuntime, fs_probe: FilesystemProbe) -> None:
        self.runtime = runtime
        self.fs_probe = fs_probe

    def build(self) -> list[VolumeTopology]:
        volumes = self.runtime.list_volumes()
        usage = self.runtime.volume_usage()
        topologies: list[VolumeTopology] = []
        for volume in volumes:
            resolved_path = str(Path(volume.mountpoint).expanduser().resolve(strict=False))
            record = self.fs_probe.resolve_mount_for_path(resolved_path)
            topologies.append(
                VolumeTopology(
                    name=volume.name,
                    docker_mountpoint=volume.mountpoint,
                    resolved_data_path=resolved_path,
                    filesystem_mount=record.mount_point,
                    device=record.source,
                    used_by=sorted(usage.get(volume.name, [])),
                )
            )
        return sorted(topologies, key=lambda item: item.name)
