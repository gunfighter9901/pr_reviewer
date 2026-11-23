"""Milestone 1 Verification Tests: Webhook Ingress Contract (INV-1, INV-2)."""

from __future__ import annotations

import json
import time
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.webhook.app import create_app
from backend.webhook.router import IdempotentWebhookRouter
from backend.webhook.security import compute_github_signature, verify_github_signature

TEST_SECRET = "m1_super_secret_hmac_key"


def _sample_pr_payload(action: str = "opened", pr_number: int = 42) -> Dict[str, Any]:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "title": "Add payment retry logic",
            "body": "Implements idempotent retry handler",
            "head": {"sha": "abc1234def5678", "ref": "feat/payment-retry"},
            "base": {"sha": "00011122233344", "ref": "main"},
        },
        "repository": {
            "full_name": "acme-corp/payment-service",
            "private": False,
        },
    }


@pytest.fixture()
def webhook_env() -> tuple[TestClient, IdempotentWebhookRouter, Settings]:
    settings = Settings(_env_file=None, GITHUB_WEBHOOK_SECRET=TEST_SECRET)
    router = IdempotentWebhookRouter()
    app = create_app(settings=settings, router=router)
    client = TestClient(app)
    return client, router, settings


def test_inv1_reject_unsigned_request_before_json_parse(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """INV-1: Unsigned webhook requests are rejected with 401 before parsing even invalid JSON."""
    client, router, _ = webhook_env
    raw_malformed_body = b"{this is not valid json at all!!!"

    response = client.post(
        "/webhooks/github",
        content=raw_malformed_body,
        headers={
            "X-GitHub-Delivery": "deliv-unsigned-1",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert response.json()["status"] == "unauthorized"
    assert len(router.queued_jobs) == 0


def test_inv1_reject_bad_signature_before_json_parse(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """INV-1: Forged HMAC-SHA256 signature returns 403 even when body is malformed JSON."""
    client, router, _ = webhook_env
    raw_malformed_body = b"{not-json"
    forged_sig = compute_github_signature(raw_malformed_body, "wrong_secret_key")

    response = client.post(
        "/webhooks/github",
        content=raw_malformed_body,
        headers={
            "X-Hub-Signature-256": forged_sig,
            "X-GitHub-Delivery": "deliv-forged-1",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403
    assert response.json()["status"] == "unauthorized"
    assert len(router.queued_jobs) == 0


def test_inv1_validly_signed_malformed_json_returns_400_never_500(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """INV-1: Valid HMAC signature over malformed JSON must return 400 Bad Request, never 500."""
    client, router, _ = webhook_env
    raw_malformed_body = b'{"action": "opened", "pull_request": [TRUNCATED'
    valid_sig = compute_github_signature(raw_malformed_body, TEST_SECRET)

    response = client.post(
        "/webhooks/github",
        content=raw_malformed_body,
        headers={
            "X-Hub-Signature-256": valid_sig,
            "X-GitHub-Delivery": "deliv-malformed-1",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert response.json()["status"] == "bad_request"
    assert len(router.queued_jobs) == 0


def test_valid_signed_pr_webhook_enqueues_under_200ms(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """Signed pull_request webhook (opened/synchronize/reopened) returns 202 Accepted in < 200ms."""
    client, router, _ = webhook_env
    payload_bytes = json.dumps(_sample_pr_payload(action="opened")).encode("utf-8")
    valid_sig = compute_github_signature(payload_bytes, TEST_SECRET)

    start = time.perf_counter()
    response = client.post(
        "/webhooks/github",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": valid_sig,
            "X-GitHub-Delivery": "deliv-valid-1",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert response.status_code == 202
    assert elapsed_ms < 200.0
    body = response.json()
    assert body["status"] == "queued"
    assert body["delivery_id"] == "deliv-valid-1"
    assert len(router.queued_jobs) == 1
    assert router.queued_jobs[0].repo == "acme-corp/payment-service"
    assert router.queued_jobs[0].pr_number == 42
    assert router.queued_jobs[0].commit_sha == "abc1234def5678"


def test_inv2_idempotent_duplicate_delivery_dropped(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """INV-2: Duplicate X-GitHub-Delivery IDs are acknowledged (200/202) and dropped without re-enqueueing."""
    client, router, _ = webhook_env
    payload_bytes = json.dumps(_sample_pr_payload(action="synchronize")).encode("utf-8")
    valid_sig = compute_github_signature(payload_bytes, TEST_SECRET)
    headers = {
        "X-Hub-Signature-256": valid_sig,
        "X-GitHub-Delivery": "deliv-dup-99",
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }

    first = client.post("/webhooks/github", content=payload_bytes, headers=headers)
    second = client.post("/webhooks/github", content=payload_bytes, headers=headers)
    third = client.post("/webhooks/github", content=payload_bytes, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "queued"

    assert second.status_code in (200, 202)
    assert second.json()["status"] == "duplicate"

    assert third.status_code in (200, 202)
    assert third.json()["status"] == "duplicate"

    # Must only have enqueued the job ONCE
    assert len(router.queued_jobs) == 1


def test_event_and_action_filtering(
    webhook_env: tuple[TestClient, IdempotentWebhookRouter, Settings],
) -> None:
    """Non-pull_request events and closed/labeled PR actions are ignored without enqueueing."""
    client, router, _ = webhook_env

    # 1. issue_comment event
    comment_bytes = json.dumps({"action": "created", "comment": {"body": "lgtm"}}).encode("utf-8")
    sig1 = compute_github_signature(comment_bytes, TEST_SECRET)
    res1 = client.post(
        "/webhooks/github",
        content=comment_bytes,
        headers={
            "X-Hub-Signature-256": sig1,
            "X-GitHub-Delivery": "deliv-ignore-event",
            "X-GitHub-Event": "issue_comment",
        },
    )
    assert res1.status_code == 202
    assert res1.json()["status"] == "ignored"

    # 2. pull_request 'closed' action
    closed_bytes = json.dumps(_sample_pr_payload(action="closed")).encode("utf-8")
    sig2 = compute_github_signature(closed_bytes, TEST_SECRET)
    res2 = client.post(
        "/webhooks/github",
        content=closed_bytes,
        headers={
            "X-Hub-Signature-256": sig2,
            "X-GitHub-Delivery": "deliv-ignore-action",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert res2.status_code == 202
    assert res2.json()["status"] == "ignored"
    assert len(router.queued_jobs) == 0


def test_verify_signature_requires_raw_bytes() -> None:
    """INV-1: Calling verify_github_signature with a parsed str/dict instead of raw bytes raises TypeError."""
    with pytest.raises(TypeError, match="INV-1 Violation"):
        verify_github_signature("not-bytes", "sha256=123", TEST_SECRET)  # type: ignore[arg-type]
