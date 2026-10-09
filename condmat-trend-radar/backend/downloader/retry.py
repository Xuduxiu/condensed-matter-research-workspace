from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


TRANSIENT_FAILURES = {
    "connection_error",
    "empty_pdf",
    "file_io_error",
    "http_rate_limited",
    "http_server_error",
    "invalid_pdf_structure",
    "not_pdf_content",
    "pdf_too_small",
    "request_timeout",
    "tls_error",
    "truncated_pdf",
}
MANUAL_REVIEW_FAILURES = {
    "access_denied",
    "encrypted_pdf",
    "institutional_auth_required",
    "legal_restriction",
    "no_legal_oa_location",
    "pdf_too_large",
}


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    delay_seconds: float | None
    status: str
    failure_class: str = "unknown"


def retry_delay(
    attempt_number: int,
    *,
    base_seconds: float = 5.0,
    maximum_seconds: float = 3600.0,
) -> float:
    attempt = max(1, attempt_number)
    return min(maximum_seconds, base_seconds * (2 ** (attempt - 1)))


def failure_class_for(
    *,
    http_status: int | None = None,
    exception: BaseException | None = None,
) -> str:
    if http_status in {401, 403, 407}:
        return "access_denied"
    if http_status == 451:
        return "legal_restriction"
    if http_status in {404, 410}:
        return "not_found"
    if http_status == 429:
        return "http_rate_limited"
    if http_status in {408, 425}:
        return "request_timeout"
    if http_status is not None and http_status >= 500:
        return "http_server_error"
    name = type(exception).__name__.casefold() if exception else ""
    message = str(exception or "").casefold()
    if "timeout" in name or "timed out" in message:
        return "request_timeout"
    if "connect" in name or "network" in message or "dns" in message:
        if "ssl" in name or "ssl" in message or "tls" in message or "handshake" in message:
            return "tls_error"
        return "connection_error"
    if "ssl" in name or "ssl" in message or "tls" in message or "handshake" in message:
        return "tls_error"
    if "size limit" in message or "too large" in message:
        return "pdf_too_large"
    return "request_error"


def classify_failure(
    attempt_number: int,
    *,
    http_status: int | None = None,
    max_attempts: int = 5,
    failure_class: str | None = None,
    retry_after_seconds: float | None = None,
) -> RetryDecision:
    kind = failure_class or failure_class_for(http_status=http_status)
    if kind in MANUAL_REVIEW_FAILURES:
        return RetryDecision(False, None, "manual_review", kind)
    retryable_code = (
        http_status is None
        or http_status in {408, 425, 429}
        or (http_status is not None and http_status >= 500)
    )
    retryable_kind = kind in TRANSIENT_FAILURES
    retryable = (retryable_code or retryable_kind) and attempt_number < max_attempts
    if retryable:
        delay = retry_delay(attempt_number)
        if retry_after_seconds is not None:
            delay = min(86400.0, max(delay, float(retry_after_seconds)))
        return RetryDecision(True, delay, "retryable_failed", kind)
    exhausted_transient = retryable_code or retryable_kind
    return RetryDecision(
        False,
        None,
        "manual_review" if exhausted_transient else "permanent_failed",
        kind,
    )


def next_attempt_iso(delay_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
