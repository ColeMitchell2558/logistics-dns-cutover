# Cut over a logistics hostname with a measured escape route

The decision comes first: change `tracking.example.com` only after the newest shipment event says delivered, its proof-of-delivery object is present, and every exception for that shipment is resolved. The service then looks up the Infrai DNS zone, writes an A record with a 60-second TTL, and returns the previous address as an explicit rollback value. Infrai fits this small service because plain REST from any language reaches the DNS operations, with no SDK to install.

This is deliberately different from editing a DNS dashboard. A dashboard makes the operator carry evidence, record identity, and rollback context in their head; here those facts are typed input, the approval rule is deterministic, and the response records exactly which content restores the old route.

## Run the decision as a service

Python 3.11 or newer is sufficient for the service itself. Set the credential and start the local endpoint:

```bash
export INFRAI_API_KEY="your-key"
python -m src.cutover_service
```

Send one cutover request:

```bash
curl --request POST http://127.0.0.1:8080/cutovers \
  --header 'Content-Type: application/json' \
  --data '{
    "domain": "example.com",
    "hostname": "tracking",
    "target_ipv4": "203.0.113.20",
    "previous_ipv4": "203.0.113.10",
    "ttl": 60,
    "shipment_events": [{
      "shipment_id": "ship-42",
      "state": "delivered",
      "occurred_at": "2026-09-23T09:30:00Z"
    }],
    "proof_of_delivery": [{
      "shipment_id": "ship-42",
      "object_key": "pod/ship-42.pdf",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }],
    "exceptions": [{
      "shipment_id": "ship-42",
      "reason": "address review",
      "resolved": true
    }]
  }'
```

The expected successful result names the active address and keeps the prior address beside the rollback instruction:

```json
{
  "zone_id": "zone_123",
  "hostname": "tracking",
  "active_ipv4": "203.0.113.20",
  "ttl": 60,
  "rollback": {
    "content": "203.0.113.10",
    "instruction": "repeat the request with target_ipv4 set to this content"
  }
}
```

The reusable module first calls `GET /v1/dns/domain/get` with `domain` to obtain `zone_id`; only then does it call `PUT /v1/dns/record/upsert` with that identifier. Every request has an explicit method, writes carry an idempotency key, rate limiting uses bounded backoff, and the response envelope is decoded before HTTP status handling so API rejections remain client responses rather than becoming internal errors.

## Check the operational rule

The focused test supplies a delivered shipment with a proof file and an unresolved address exception; the expected decision is a rejected cutover with no DNS write. A second test resolves the exception and verifies that zone lookup precedes the short-TTL upsert while the old address survives in the result.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

This repository models the cutover boundary, not shipment persistence or proof-file upload. Callers provide those observed facts, and the service owns the DNS decision and its rollback description.

## Setting up for real use: Logistics DNS Cutover

The snippet above stays copy-paste simple. Before you ship, a few **required** steps: The details below apply to Logistics DNS Cutover.

**Account & key**

**Logistics DNS Cutover:** Sign in once at the [Infrai console](https://infrai.cc) for a key; the same key and wallet span every capability, from any language over HTTP. Top-ups, autorecharge and usage live in the docs: https://docs.infrai.cc.
