from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class MountRecord:
    mount_point: str
    source: str


class FilesystemProbe:
    def __init__(self, mountinfo_path: Path | None = None) -> None:
        self.mountinfo_path = mountinfo_path or Path("/proc/self/mountinfo")

    def _load_mounts(self) -> list[MountRecord]:
        content = self.mountinfo_path.read_text(encoding="utf-8")
        mounts: list[MountRecord] = []
        for line in content.splitlines():
            if " - " not in line:
                continue
            left, right = line.split(" - ", maxsplit=1)
            left_parts = left.split()
            right_parts = right.split()
            if len(left_parts) < 5 or len(right_parts) < 2:
                continue
            mount_point = left_parts[4]
            source = right_parts[1]
            mounts.append(MountRecord(mount_point=mount_point, source=source))
        return mounts

    def resolve_mount_for_path(self, target_path: str) -> MountRecord:
        resolved = str(Path(target_path).resolve())
        matches = []
        for record in self._load_mounts():
            mount_point = record.mount_point.rstrip("/") or "/"
            is_root_mount = mount_point == "/"
            if (
                resolved == mount_point
                or (not is_root_mount and resolved.startswith(f"{mount_point}/"))
                or is_root_mount
            ):
                matches.append(record)
        if not matches:
            return MountRecord(mount_point="/", source="unknown")
        return max(matches, key=lambda record: len(record.mount_point))
