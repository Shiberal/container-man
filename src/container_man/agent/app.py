from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from container_man.runtime.docker_cli import DockerCliRuntime
from container_man.services.container_transfer import ContainerTransferService
from container_man.services.filesystem import FilesystemProbe
from container_man.services.migration import MigrationService
from container_man.services.topology import VolumeTopologyService


class TransferSendRequest(BaseModel):
    container: str
    host: str
    remove_after_verification: bool = False
    confirm_timeout: float = 120.0


class TransferReceiveRequest(BaseModel):
    package_path: str = "/tmp/cm-received-container.cm.tgz"
    container_name: str | None = None


class VolumeMigrateRequest(BaseModel):
    volume: str
    target: str
    apply: bool = False


class ContainerMigrateRequest(BaseModel):
    container: str
    target: str
    apply: bool = False


class JobManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._jobs: dict[str, Future] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, name: str, fn: Callable[[], Any]) -> str:
        job_id = str(uuid.uuid4())
        future = self._pool.submit(fn)
        with self._lock:
            self._jobs[job_id] = future
            self._meta[job_id] = {"id": job_id, "name": name}
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            future = self._jobs.get(job_id)
            meta = self._meta.get(job_id)
        if future is None or meta is None:
            raise KeyError(job_id)
        if future.running():
            return {**meta, "status": "running"}
        if not future.done():
            return {**meta, "status": "queued"}
        try:
            return {**meta, "status": "done", "result": future.result()}
        except Exception as exc:  # pragma: no cover
            return {**meta, "status": "failed", "error": str(exc)}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._jobs.keys())
        return [self.get(job_id) for job_id in ids]


def create_app() -> FastAPI:
    app = FastAPI(title="container-man-agent", version="0.1.0")
    runtime = DockerCliRuntime()
    jobs = JobManager()

    @app.get("/api/local/overview")
    def local_overview() -> dict[str, Any]:
        containers = runtime.list_containers()
        volumes = runtime.list_volumes()
        return {
            "containers_total": len(containers),
            "containers_running": len([c for c in containers if "Up" in c.status]),
            "volumes_total": len(volumes),
        }

    @app.get("/api/local/containers")
    def local_containers() -> list[dict[str, Any]]:
        return [asdict(item) for item in runtime.list_containers()]

    @app.get("/api/local/volumes")
    def local_volumes() -> list[dict[str, Any]]:
        return [asdict(item) for item in runtime.list_volumes()]

    @app.get("/api/local/volumes/map")
    def local_volumes_map() -> list[dict[str, Any]]:
        service = VolumeTopologyService(runtime=runtime, fs_probe=FilesystemProbe())
        return [asdict(item) for item in service.build()]

    @app.post("/api/local/migrate/volume")
    def migrate_volume(req: VolumeMigrateRequest) -> dict[str, Any]:
        service = MigrationService(runtime)
        plan = service.plan_for_volume(req.volume, req.target)
        if req.apply:
            service.apply(plan)
        return asdict(plan)

    @app.post("/api/local/migrate/container")
    def migrate_container(req: ContainerMigrateRequest) -> dict[str, Any]:
        service = MigrationService(runtime)
        plan = service.plan_for_container(req.container, req.target)
        if req.apply:
            service.apply(plan)
        return asdict(plan)

    @app.post("/api/local/transfer/send")
    def transfer_send(req: TransferSendRequest) -> dict[str, str]:
        service = ContainerTransferService(runtime=runtime)

        def _task() -> dict[str, Any]:
            result = service.send_container(
                container=req.container,
                destination_host=req.host,
                destination_port=9000,
                package_path=f"/tmp/{req.container}.cm.tgz",
            )
            payload: dict[str, Any] = asdict(result)
            if req.remove_after_verification:
                verification = service.wait_for_move_confirmation(
                    bind_host="0.0.0.0",
                    bind_port=9100,
                    timeout_seconds=req.confirm_timeout,
                )
                payload["verification"] = asdict(verification)
                if not verification.ok:
                    raise RuntimeError(verification.details)
                removed = service.remove_sender_container_after_move(req.container)
                payload["removed_source"] = True
                payload["removed_volumes"] = removed
            return payload

        return {"job_id": jobs.submit("transfer-send", _task)}

    @app.post("/api/local/transfer/receive")
    def transfer_receive(req: TransferReceiveRequest) -> dict[str, str]:
        service = ContainerTransferService(runtime=runtime)

        def _task() -> dict[str, Any]:
            result = service.receive_container(
                package_path=req.package_path,
                bind_host="0.0.0.0",
                bind_port=9000,
                overwrite_package=True,
                container_name=req.container_name,
            )
            verification = service.verify_container_running(result.container_id)
            source_host = result.transfer.source.rsplit(":", maxsplit=1)[0]
            service.send_move_confirmation(
                sender_host=source_host,
                sender_port=9100,
                container_name=result.container_name,
                verification=verification,
            )
            payload = asdict(result)
            payload["verification"] = asdict(verification)
            return payload

        return {"job_id": jobs.submit("transfer-receive", _task)}

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return jobs.list()

    @app.get("/api/jobs/{job_id}", responses={404: {"description": "Job not found"}})
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    return app


def run() -> None:
    host = os.getenv("CM_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("CM_AGENT_PORT", "8081"))
    uvicorn.run("container_man.agent.app:create_app", host=host, port=port, factory=True)
