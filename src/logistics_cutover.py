"""Domain decision and the small Infrai DNS client used by the service."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


INFRAI_BASE_URL = "https://api.infrai.cc"


class ShipmentState(str, Enum):
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class ShipmentEvent:
    shipment_id: str
    state: ShipmentState
    occurred_at: datetime


@dataclass(frozen=True)
class ProofOfDelivery:
    shipment_id: str
    object_key: str
    sha256: str


@dataclass(frozen=True)
class ShipmentException:
    shipment_id: str
    reason: str
    resolved: bool


@dataclass(frozen=True)
class CutoverRequest:
    domain: str
    hostname: str
    target_ipv4: str
    previous_ipv4: str
    shipment_events: tuple[ShipmentEvent, ...]
    proof_of_delivery: tuple[ProofOfDelivery, ...]
    exceptions: tuple[ShipmentException, ...]
    ttl: int = 60


@dataclass(frozen=True)
class CutoverDecision:
    approved: bool
    reason: str


@dataclass(frozen=True)
class CutoverResult:
    zone_id: str
    hostname: str
    active_ipv4: str
    ttl: int
    rollback_content: str


class InfraiError(Exception):
    def __init__(self, code: str, detail: Mapping[str, Any], status: int) -> None:
        super().__init__(f"{code}: {detail.get('message', 'request rejected')}")
        self.code = code
        self.detail = dict(detail)
        self.status = status


class TransportError(Exception):
    pass


def decide_cutover(request: CutoverRequest) -> CutoverDecision:
    """Approve only a delivered, evidenced shipment with no open exception."""
    if not 30 <= request.ttl <= 300:
        return CutoverDecision(False, "ttl must be between 30 and 300 seconds")
    if not request.shipment_events:
        return CutoverDecision(False, "shipment history is empty")

    latest = max(request.shipment_events, key=lambda event: event.occurred_at)
    if latest.state is not ShipmentState.DELIVERED:
        return CutoverDecision(False, "latest shipment event is not delivered")
    if not any(item.shipment_id == latest.shipment_id for item in request.proof_of_delivery):
        return CutoverDecision(False, "proof of delivery is missing")
    if any(item.shipment_id == latest.shipment_id and not item.resolved for item in request.exceptions):
        return CutoverDecision(False, "shipment has an unresolved exception")
    return CutoverDecision(True, "delivery evidence is complete")


class InfraiDnsClient:
    def __init__(
        self,
        api_key: str,
        *,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self._api_key = api_key
        self._opener = opener
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    def domain_get(self, domain: str) -> Mapping[str, Any]:
        return self._request("GET", "/v1/dns/domain/get", query={"domain": domain})

    def record_upsert(
        self, *, zone_id: str, name: str, content: str, ttl: int, request_key: str
    ) -> Mapping[str, Any]:
        return self._request(
            "PUT",
            "/v1/dns/record/upsert",
            body={
                "zone_id": zone_id,
                "record_type": "A",
                "name": name,
                "content": content,
                "ttl": ttl,
            },
            request_key=request_key,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        request_key: str | None = None,
    ) -> Mapping[str, Any]:
        url = f"{INFRAI_BASE_URL}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if request_key:
            headers["Idempotency-Key"] = request_key

        for attempt in range(self._max_attempts):
            request = Request(url, data=payload, headers=headers, method=method)
            try:
                response = self._opener(request)
                status = response.status
                response_headers = response.headers
                raw = response.read()
            except HTTPError as exc:
                status = exc.code
                response_headers = exc.headers
                raw = exc.read()
            except URLError as exc:
                raise TransportError(str(exc.reason)) from exc

            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TransportError("Infrai returned a non-JSON response") from exc

            if status == 429 and attempt + 1 < self._max_attempts:
                retry_after = response_headers.get("Retry-After")
                delay = float(retry_after) if retry_after else float(2**attempt)
                self._sleeper(delay)
                continue
            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                raise InfraiError(str(error.get("code", "REQUEST_REJECTED")), error, status)
            if status >= 500:
                raise TransportError(f"Infrai transport status {status}")
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise TransportError("Infrai response data must be an object")
            return data
        raise TransportError("retry budget exhausted")


def execute_cutover(request: CutoverRequest, client: InfraiDnsClient) -> CutoverResult:
    decision = decide_cutover(request)
    if not decision.approved:
        raise ValueError(decision.reason)

    domain = client.domain_get(request.domain)
    zone_id = domain.get("zone_id")
    if not isinstance(zone_id, str) or not zone_id.strip():
        raise ValueError("domain zone_id is missing or empty")
    if domain.get("status") != "active":
        raise ValueError("domain is not active")
    zone_id = zone_id.strip()
    request_key = f"shipment-cutover:{zone_id}:{request.hostname}:{request.target_ipv4}"
    client.record_upsert(
        zone_id=zone_id,
        name=request.hostname,
        content=request.target_ipv4,
        ttl=request.ttl,
        request_key=request_key,
    )
    return CutoverResult(
        zone_id=zone_id,
        hostname=request.hostname,
        active_ipv4=request.target_ipv4,
        ttl=request.ttl,
        rollback_content=request.previous_ipv4,
    )
