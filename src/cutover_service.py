"""Runnable HTTP entry point for an evidence-gated logistics DNS cutover."""

from __future__ import annotations

import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.logistics_cutover import (
    CutoverRequest,
    InfraiDnsClient,
    InfraiError,
    ProofOfDelivery,
    ShipmentEvent,
    ShipmentException,
    ShipmentState,
    TransportError,
    execute_cutover,
)


def parse_request(payload: dict[str, Any]) -> CutoverRequest:
    return CutoverRequest(
        domain=str(payload["domain"]),
        hostname=str(payload["hostname"]),
        target_ipv4=str(payload["target_ipv4"]),
        previous_ipv4=str(payload["previous_ipv4"]),
        ttl=int(payload.get("ttl", 60)),
        shipment_events=tuple(
            ShipmentEvent(
                shipment_id=str(item["shipment_id"]),
                state=ShipmentState(item["state"]),
                occurred_at=datetime.fromisoformat(str(item["occurred_at"]).replace("Z", "+00:00")),
            )
            for item in payload["shipment_events"]
        ),
        proof_of_delivery=tuple(
            ProofOfDelivery(
                shipment_id=str(item["shipment_id"]),
                object_key=str(item["object_key"]),
                sha256=str(item["sha256"]),
            )
            for item in payload["proof_of_delivery"]
        ),
        exceptions=tuple(
            ShipmentException(
                shipment_id=str(item["shipment_id"]),
                reason=str(item["reason"]),
                resolved=bool(item["resolved"]),
            )
            for item in payload["exceptions"]
        ),
    )


class CutoverHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/cutovers":
            self._send(404, {"error": "route not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            api_key = os.environ["INFRAI_API_KEY"]
            result = execute_cutover(parse_request(payload), InfraiDnsClient(api_key))
            self._send(200, {
                "zone_id": result.zone_id,
                "hostname": result.hostname,
                "active_ipv4": result.active_ipv4,
                "ttl": result.ttl,
                "rollback": {
                    "content": result.rollback_content,
                    "instruction": "repeat the request with target_ipv4 set to this content",
                },
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except InfraiError as exc:
            status = exc.status if 400 <= exc.status < 500 else 502
            self._send(status, {"error": exc.code, "detail": exc.detail})
        except TransportError as exc:
            self._send(502, {"error": str(exc)})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), CutoverHandler)
    print(f"Cutover service listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
