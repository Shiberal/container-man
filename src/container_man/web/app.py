from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


@dataclass(slots=True)
class ClusterNode:
    name: str
    base_url: str
    token: str | None = None


class ClusterProxy:
    def __init__(self, nodes: list[ClusterNode]) -> None:
        self.nodes = {node.name: node for node in nodes}

    def list_nodes(self) -> list[dict[str, Any]]:
        return [
            {"name": node.name, "base_url": node.base_url, "token_set": bool(node.token)}
            for node in self.nodes.values()
        ]

    def request(
        self,
        node_name: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        node = self.nodes.get(node_name)
        if node is None:
            raise KeyError(node_name)
        headers = {"Authorization": f"Bearer {node.token}"} if node.token else {}
        response = requests.request(
            method=method,
            url=f"{node.base_url.rstrip('/')}{path}",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_cluster_overview(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for node in self.nodes.values():
            headers = {"Authorization": f"Bearer {node.token}"} if node.token else {}
            try:
                response = requests.get(
                    f"{node.base_url.rstrip('/')}/api/local/overview",
                    headers=headers,
                    timeout=3,
                )
                response.raise_for_status()
                payload = response.json()
                items.append(
                    {
                        "name": node.name,
                        "base_url": node.base_url,
                        "status": "online",
                        "overview": payload,
                    }
                )
            except requests.RequestException as exc:
                items.append(
                    {
                        "name": node.name,
                        "base_url": node.base_url,
                        "status": "offline",
                        "error": str(exc),
                    }
                )
        return items


def _load_cluster_nodes() -> list[ClusterNode]:
    config_path = Path(
        os.getenv(
            "CM_CLUSTER_NODES_FILE",
            str(Path.home() / ".config" / "container-man" / "cluster_nodes.json"),
        )
    )
    if not config_path.exists():
        return []
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    nodes: list[ClusterNode] = []
    for item in raw:
        nodes.append(
            ClusterNode(
                name=str(item["name"]),
                base_url=str(item["base_url"]),
                token=str(item["token"]) if item.get("token") else None,
            )
        )
    return nodes


def create_app() -> FastAPI:
    app = FastAPI(title="container-man-web", version="0.1.0")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    cluster = ClusterProxy(_load_cluster_nodes())

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"request": request, "nodes": cluster.list_nodes()},
        )

    @app.get("/api/cluster/nodes")
    def cluster_nodes() -> list[dict[str, Any]]:
        return cluster.list_nodes()

    @app.get("/api/cluster/overview")
    def cluster_overview() -> list[dict[str, Any]]:
        return cluster.get_cluster_overview()

    @app.get(
        "/api/cluster/nodes/{node_name}/overview",
        responses={
            404: {"description": "Node not found"},
            502: {"description": "Node unreachable"},
        },
    )
    def cluster_node_overview(node_name: str) -> dict[str, Any]:
        try:
            return cluster.request(node_name, "GET", "/api/local/overview")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Node not found") from exc
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.api_route(
        "/api/cluster/nodes/{node_name}/{full_path:path}",
        methods=["GET", "POST"],
        responses={
            404: {"description": "Node not found"},
            502: {"description": "Node unreachable"},
        },
    )
    async def cluster_proxy(node_name: str, full_path: str, request: Request) -> JSONResponse:
        try:
            payload: dict[str, Any] | None = None
            if request.method == "POST":
                payload = await request.json()
            result = cluster.request(node_name, request.method, f"/{full_path}", payload=payload)
            return JSONResponse(content=result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Node not found") from exc
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def run() -> None:
    host = os.getenv("CM_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("CM_WEB_PORT", "8080"))
    uvicorn.run("container_man.web.app:create_app", host=host, port=port, factory=True)
