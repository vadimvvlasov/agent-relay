"""Live API integration test: two agents exchange a task and its result.

Runs against the real HTTP API + real SQLite DB (default ./agent-relay.db),
not TestClient / scratch DB. Requires the relay running, e.g.:

    uv run uvicorn main:app

Run:

    uv run pytest test_live_exchange.py -v

Env:
    RELAY_BASE_URL (default http://127.0.0.1:8000)

Unlike test_agent_relay.py this test never drops tables -- it creates
unique agents per run and verifies the full SPEC acceptance scenario 1
through HTTP.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

BASE_URL = os.getenv("RELAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api/v1"
TIMEOUT = 10.0


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c


def _check_server(client: httpx.Client):
    try:
        r = client.get("/health", timeout=5.0)
    except httpx.ConnectError:
        pytest.skip(f"relay not running at {BASE_URL}; start with `uv run uvicorn main:app`")
    assert r.status_code == 200, f"/health -> {r.status_code}: {r.text}"
    ready = client.get("/ready")
    assert ready.status_code == 200, f"/ready -> {ready.status_code}: {ready.text}"


def _register(client: httpx.Client, name: str) -> tuple[dict, dict[str, str]]:
    suffix = uuid.uuid4().hex[:8]
    r = client.post(f"{API}/agents", json={"name": f"{name}-{suffix}"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["agent_id"]
    assert data["token"]
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_two_agents_exchange_task_and_result(client: httpx.Client):
    _check_server(client)

    sender, sender_headers = _register(client, "alice-sender")
    recipient, recipient_headers = _register(client, "bob-worker")

    # Sender -> recipient.
    task_input = "Review this Python function: def add(a, b): return a + b"
    r = client.post(
        f"{API}/tasks",
        headers={**sender_headers, "Idempotency-Key": f"live-{uuid.uuid4().hex}"},
        json={"to": recipient["agent_id"], "input": task_input},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    # Recipient claims (wait up to 5s via long poll).
    r = client.post(
        f"{API}/tasks/claim",
        headers=recipient_headers,
        json={"worker_id": "bob-laptop-1", "wait_seconds": 5},
    )
    assert r.status_code == 200, r.text
    claim = r.json()
    assert claim["task_id"] == task_id
    assert claim["from"] == sender["agent_id"]
    assert claim["input"] == task_input
    assert claim["attempt"] == 1
    assert claim["claim_token"]
    assert claim["lease_expires_at"]

    # Recipient completes.
    output = "Looks good: no off-by-one, consider type hints."
    r = client.post(
        f"{API}/tasks/{task_id}/complete",
        headers=recipient_headers,
        json={"claim_token": claim["claim_token"], "output": output},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": task_id, "status": "completed"}

    # Sender reads the result -- proves persistence in the real DB.
    r = client.get(f"{API}/tasks/{task_id}", headers=sender_headers)
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["task_id"] == task_id
    assert task["from"] == sender["agent_id"]
    assert task["to"] == recipient["agent_id"]
    assert task["input"] == task_input
    assert task["status"] == "completed"
    assert task["output"] == output
    assert task["error"] is None
    assert task["attempt_count"] == 1
    assert task["finished_at"] is not None

    # Recipient sees the same result; delivery history has no token leak.
    r = client.get(f"{API}/tasks/{task_id}", headers=recipient_headers)
    assert r.status_code == 200, r.text
    assert r.json()["output"] == output

    r = client.get(f"{API}/tasks/{task_id}/attempts", headers=sender_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["outcome"] == "completed"
    assert "claim_token" not in items[0]
