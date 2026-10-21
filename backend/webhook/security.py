"""Raw-byte HMAC-SHA256 webhook signature verification (INV-1: Verify Before Parse)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional


class WebhookSignatureError(Exception):
    """Raised when X-Hub-Signature-256 is missing, malformed, or does not match the payload."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def compute_github_signature(raw_body: bytes, secret: str) -> str:
    """Compute the expected GitHub X-Hub-Signature-256 header for raw_body."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    secret: str,
) -> bool:
    """Verify GitHub HMAC-SHA256 signature on raw request bytes BEFORE parsing JSON (INV-1).

    Raises WebhookSignatureError with status_code 401 (missing/malformed) or 403 (mismatch).
    """
    if not isinstance(raw_body, (bytes, bytearray)):
        raise TypeError("INV-1 Violation: raw_body must be raw bytes before JSON parsing")

    if not signature_header or not signature_header.strip():
        raise WebhookSignatureError("Missing X-Hub-Signature-256 header", status_code=401)

    if not signature_header.startswith("sha256="):
        raise WebhookSignatureError(
            "Malformed X-Hub-Signature-256 header: must start with 'sha256='",
            status_code=401,
        )

    expected = compute_github_signature(bytes(raw_body), secret)
    if not hmac.compare_digest(signature_header.strip(), expected):
        raise WebhookSignatureError("Invalid X-Hub-Signature-256 signature", status_code=403)

    return True
