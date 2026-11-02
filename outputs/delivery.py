"""Bounded durable Discord outbox; HTTP acknowledgement is not exactly-once."""
import json
import math
import time
import uuid

import httpx

MAX_PENDING = 100
MAX_DELIVERIES_PER_RUN = 10
MAX_PAYLOAD_BYTES = 512 * 1024


def new_delivery(payloads: list[dict]) -> dict | None:
    if not payloads:
        return None
    encoded = json.dumps(payloads, ensure_ascii=False)
    if len(encoded.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ValueError('Discord delivery exceeds 512 KiB')
    return dict(id=uuid.uuid4().hex, payloads=encoded, next_payload=0,
                created_at=time.time(), next_attempt_at=0.0, attempts=0)


def describe_delivery_error(error: Exception) -> str:
    """Safe diagnostics: exception text/tracebacks can contain webhook tokens."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTPStatusError (HTTP {error.response.status_code})"
    return type(error).__name__


def retry_delay(error: Exception, attempts: int = 0) -> float:
    backoff = min(6 * 60 * 60, 30.0 * 2 ** min(max(attempts, 0), 10))
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
        try:
            delay = float(error.response.json()['retry_after'])
            if math.isfinite(delay):
                return max(backoff, delay)
        except (ValueError, TypeError, KeyError):
            pass
        try:
            delay = float(error.response.headers.get('Retry-After', '30'))
            if math.isfinite(delay):
                return max(backoff, delay)
        except ValueError:
            pass
    return backoff


def drain_deliveries(db, output) -> int:
    """Resume immutable payloads, checkpoint each acknowledged message.

    No sleep/retry loop: at most ten deliveries per invocation. Failures are
    retained for a later run, with a not-before timestamp (including HTTP 429).
    """
    if not output.webhook_url:
        return 0
    pending = db.pending_deliveries(MAX_PENDING)
    # All intents target the current webhook. A per-row cooldown would let a
    # newer row bypass the same webhook's 429. Conservatively pause all sends
    # for any pending cooldown; no HTTP or checkpoint writes occur here.
    now = time.time()
    if any(delivery['next_attempt_at'] > now for delivery in pending):
        return 0
    completed = 0
    attempted = 0
    first_error = None
    for delivery in pending:
        if attempted >= MAX_DELIVERIES_PER_RUN:
            break
        attempted += 1
        payloads = json.loads(delivery['payloads'])
        for index in range(delivery['next_payload'], len(payloads)):
            try:
                output.send_payload(payloads[index])
            except Exception as error:
                db.update_delivery(delivery['id'], index, time.time() + retry_delay(error, delivery['attempts']),
                                   delivery['attempts'] + 1)
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
                    raise  # Stop this webhook immediately, not merely this row.
                if first_error is None:
                    first_error = error
                break  # Do not send later payloads from the failed delivery.
            # A crash here can duplicate this message on retry. Discord webhooks
            # have no idempotency key; never claim exactly-once delivery.
            db.update_delivery(delivery['id'], index + 1, 0.0, delivery['attempts'])
        else:
            db.delete_delivery(delivery['id'])
            completed += 1
    if first_error is not None:
        raise first_error
    return completed
