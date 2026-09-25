from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from wardrobe.errors import PinterestAuthError, PinterestError

T = TypeVar("T")

MAX_RATE_LIMIT_RETRIES = 5
AUTH_FAILURE_HINT = (
    " A trial Pinterest app can only read the account that owns the app. "
    "Run `wardrobe auth` while logged in as that account, or ask Pinterest for broader access."
)


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """Exponential delay with jitter. `attempt` starts at 1."""
    base = min(60.0, 2.0**attempt)
    return base + rng.random()


def error_from_response(response: httpx.Response) -> PinterestError:
    message = response.text
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        message = str(body["message"])
    if response.status_code in {401, 403}:
        return PinterestAuthError(
            f"Pinterest HTTP {response.status_code}: {message}.{AUTH_FAILURE_HINT}",
            status_code=response.status_code,
        )
    return PinterestError(
        f"Pinterest HTTP {response.status_code}: {message}",
        status_code=response.status_code,
    )


def send_with_retries(
    send: Callable[[], httpx.Response],
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_unauthorized: Callable[[], None] | None = None,
    max_rate_retries: int = MAX_RATE_LIMIT_RETRIES,
) -> httpx.Response:
    """Send once, refresh on a single 401, and honor 429 Retry-After."""
    generator = rng or random.Random()
    refreshed = False
    rate_attempts = 0
    while True:
        response = send()
        if response.status_code == 401 and on_unauthorized is not None and not refreshed:
            on_unauthorized()
            refreshed = True
            continue
        if response.status_code == 429 and rate_attempts < max_rate_retries:
            rate_attempts += 1
            delay = retry_after_seconds(response)
            if delay is None:
                delay = backoff_seconds(rate_attempts, generator)
            sleep(delay)
            continue
        if response.status_code >= 400:
            raise error_from_response(response)
        return response
