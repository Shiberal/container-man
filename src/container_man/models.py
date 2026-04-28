from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ContainerSummary:
    container_id: str
    name: str
    status: str


@dataclass(slots=True)
class VolumeSummary:
    name: str
    driver: str
    mountpoint: str
    scope: str


@dataclass(slots=True)
class VolumeTopology:
    name: str
    docker_mountpoint: str
    resolved_data_path: str
    filesystem_mount: str
    device: str
    used_by: list[str]
