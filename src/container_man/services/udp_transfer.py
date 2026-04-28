from __future__ import annotations

import base64
import hashlib
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter


class UdpTransferError(RuntimeError):
    """Raised when a UDP transfer cannot be completed safely."""


@dataclass(slots=True)
class TransferResult:
    bytes_transferred: int
    duration_seconds: float
    chunks: int
    source: str
    destination: str
    sha256: str


class UdpTransferService:
    def __init__(self, *, default_chunk_size: int = 1200) -> None:
        if default_chunk_size <= 0:
            raise ValueError("default_chunk_size must be positive")
        self.default_chunk_size = default_chunk_size

    def send_file(
        self,
        source_path: str,
        destination_host: str,
        destination_port: int,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        timeout_seconds: float = 1.0,
        max_retries: int = 10,
        chunk_size: int | None = None,
    ) -> TransferResult:
        chunk_size = chunk_size or self.default_chunk_size
        if chunk_size <= 0:
            raise UdpTransferError("chunk_size must be positive")
        if destination_port <= 0:
            raise UdpTransferError("destination_port must be positive")
        if timeout_seconds <= 0:
            raise UdpTransferError("timeout_seconds must be positive")
        if max_retries < 1:
            raise UdpTransferError("max_retries must be at least 1")

        src = Path(source_path).expanduser().resolve()
        if not src.exists() or not src.is_file():
            raise UdpTransferError(f"Source file '{src}' not found.")

        payload = src.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
        if not chunks:
            chunks = [b""]

        destination = (destination_host, destination_port)
        started = perf_counter()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((bind_host, bind_port))
            sock.settimeout(timeout_seconds)

            start_packet = {
                "type": "start",
                "name": src.name,
                "size": len(payload),
                "sha256": digest,
                "chunks": len(chunks),
                "chunk_size": chunk_size,
            }
            self._send_with_ack(sock, destination, start_packet, "ack_start", None, max_retries)

            for index, chunk in enumerate(chunks):
                chunk_packet = {
                    "type": "chunk",
                    "seq": index,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
                self._send_with_ack(
                    sock,
                    destination,
                    chunk_packet,
                    "ack_chunk",
                    expected_seq=index,
                    max_retries=max_retries,
                )

            end_packet = {"type": "end"}
            self._send_with_ack(sock, destination, end_packet, "ack_end", None, max_retries)

        duration = perf_counter() - started
        return TransferResult(
            bytes_transferred=len(payload),
            duration_seconds=duration,
            chunks=len(chunks),
            source=str(src),
            destination=f"{destination_host}:{destination_port}",
            sha256=digest,
        )

    def receive_file(
        self,
        output_path: str,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int,
        overwrite: bool = False,
        timeout_seconds: float | None = None,
    ) -> TransferResult:
        if bind_port <= 0:
            raise UdpTransferError("bind_port must be positive")

        out = Path(output_path).expanduser().resolve()
        if out.exists() and not overwrite:
            raise UdpTransferError(
                f"Output file '{out}' already exists. Use --overwrite to replace it."
            )
        out.parent.mkdir(parents=True, exist_ok=True)

        started = perf_counter()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((bind_host, bind_port))
            if timeout_seconds is not None:
                if timeout_seconds <= 0:
                    raise UdpTransferError("timeout_seconds must be positive when set")
                sock.settimeout(timeout_seconds)
            sender: tuple[str, int] | None = None
            expected_chunks: int | None = None
            expected_sha256: str | None = None
            received_chunks: dict[int, bytes] = {}
            total_size = 0

            while True:
                data, addr = sock.recvfrom(65535)
                packet = self._decode_packet(data)
                kind = packet.get("type")

                if kind == "start":
                    sender = addr
                    expected_chunks = int(packet["chunks"])
                    expected_sha256 = str(packet["sha256"])
                    total_size = int(packet["size"])
                    received_chunks.clear()
                    self._send_packet(sock, addr, {"type": "ack_start"})
                    continue

                if sender is None or addr != sender:
                    # Ignore packets from unexpected sources.
                    continue

                if kind == "chunk":
                    seq = int(packet["seq"])
                    raw = base64.b64decode(packet["data"].encode("ascii"))
                    if seq not in received_chunks:
                        received_chunks[seq] = raw
                    self._send_packet(sock, addr, {"type": "ack_chunk", "seq": seq})
                    continue

                if kind == "end":
                    if expected_chunks is None or expected_sha256 is None:
                        raise UdpTransferError("Received end before start packet.")
                    if len(received_chunks) != expected_chunks:
                        raise UdpTransferError(
                            f"Missing chunks: expected {expected_chunks}, got {len(received_chunks)}"
                        )
                    merged = b"".join(received_chunks[index] for index in range(expected_chunks))
                    digest = hashlib.sha256(merged).hexdigest()
                    if digest != expected_sha256:
                        raise UdpTransferError("Checksum mismatch after UDP receive.")
                    if len(merged) != total_size:
                        raise UdpTransferError(
                            f"Size mismatch after UDP receive: expected {total_size}, got {len(merged)}"
                        )
                    out.write_bytes(merged)
                    self._send_packet(sock, addr, {"type": "ack_end"})
                    duration = perf_counter() - started
                    return TransferResult(
                        bytes_transferred=len(merged),
                        duration_seconds=duration,
                        chunks=expected_chunks,
                        source=f"{addr[0]}:{addr[1]}",
                        destination=str(out),
                        sha256=digest,
                    )

    def _send_with_ack(
        self,
        sock: socket.socket,
        destination: tuple[str, int],
        packet: dict[str, object],
        ack_type: str,
        expected_seq: int | None,
        max_retries: int,
    ) -> None:
        for _attempt in range(1, max_retries + 1):
            self._send_packet(sock, destination, packet)
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            if addr != destination:
                continue
            ack = self._decode_packet(data)
            if ack.get("type") != ack_type:
                continue
            if expected_seq is not None and int(ack.get("seq", -1)) != expected_seq:
                continue
            return
        raise UdpTransferError(
            f"No acknowledgement for '{packet.get('type')}' after {max_retries} attempts."
        )

    def _send_packet(
        self,
        sock: socket.socket,
        destination: tuple[str, int],
        packet: dict[str, object],
    ) -> None:
        encoded = json.dumps(packet, separators=(",", ":")).encode("utf-8")
        sock.sendto(encoded, destination)

    def _decode_packet(self, data: bytes) -> dict[str, object]:
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UdpTransferError("Invalid UDP packet payload.") from exc
        if not isinstance(decoded, dict) or "type" not in decoded:
            raise UdpTransferError("Malformed UDP packet.")
        return decoded
