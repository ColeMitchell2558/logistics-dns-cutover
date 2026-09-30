from datetime import datetime, timezone

from src.logistics_cutover import (
    CutoverRequest,
    ProofOfDelivery,
    ShipmentEvent,
    ShipmentException,
    ShipmentState,
    decide_cutover,
    execute_cutover,
)


def request_with(*, unresolved: bool) -> CutoverRequest:
    return CutoverRequest(
        domain="example.com",
        hostname="tracking",
        target_ipv4="203.0.113.20",
        previous_ipv4="203.0.113.10",
        ttl=60,
        shipment_events=(
            ShipmentEvent("ship-42", ShipmentState.DELIVERED, datetime(2026, 9, 23, tzinfo=timezone.utc)),
        ),
        proof_of_delivery=(ProofOfDelivery("ship-42", "pod/ship-42.pdf", "a" * 64),),
        exceptions=(ShipmentException("ship-42", "address review", not unresolved),),
    )


def test_open_exception_blocks_dns_cutover() -> None:
    decision = decide_cutover(request_with(unresolved=True))

    assert decision.approved is False
    assert decision.reason == "shipment has an unresolved exception"


def test_approved_cutover_fetches_zone_before_upsert_and_keeps_rollback() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def domain_get(self, domain: str) -> dict[str, str]:
            self.calls.append(("domain_get", {"domain": domain}))
            return {"zone_id": "zone_123", "status": "active"}

        def record_upsert(self, **fields: object) -> dict[str, str]:
            self.calls.append(("record_upsert", fields))
            return {"record_id": "record_123"}

    client = RecordingClient()
    result = execute_cutover(request_with(unresolved=False), client)  # type: ignore[arg-type]

    assert [name for name, _ in client.calls] == ["domain_get", "record_upsert"]
    assert client.calls[1][1]["zone_id"] == "zone_123"
    assert client.calls[1][1]["ttl"] == 60
    assert result.rollback_content == "203.0.113.10"


def test_cutover_rejects_empty_zone_id_before_upsert() -> None:
    class EmptyZoneClient:
        def domain_get(self, domain: str) -> dict[str, str]:
            return {"zone_id": " ", "status": "active"}

        def record_upsert(self, **fields: object) -> dict[str, str]:
            raise AssertionError("record_upsert must not be called")

    try:
        execute_cutover(request_with(unresolved=False), EmptyZoneClient())  # type: ignore[arg-type]
    except ValueError as exc:
        assert str(exc) == "domain zone_id is missing or empty"
    else:
        raise AssertionError("empty zone_id must be rejected")


def test_cutover_rejects_non_active_domain_before_upsert() -> None:
    class PendingDomainClient:
        def domain_get(self, domain: str) -> dict[str, str]:
            return {"zone_id": "zone_123", "status": "pending"}

        def record_upsert(self, **fields: object) -> dict[str, str]:
            raise AssertionError("record_upsert must not be called")

    try:
        execute_cutover(request_with(unresolved=False), PendingDomainClient())  # type: ignore[arg-type]
    except ValueError as exc:
        assert str(exc) == "domain is not active"
    else:
        raise AssertionError("non-active domain must be rejected")
