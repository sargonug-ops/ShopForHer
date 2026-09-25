from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from wardrobe.errors import PinterestAuthError
from wardrobe.http_retry import send_with_retries

API_ROOT = "https://api.pinterest.com/v5"
PAGE_SIZE = 100
VARIANT_PREFERENCE = ("1200x", "600x", "400x300", "150x150")
STILL_MEDIA_TYPES = {"image", "multiple_images", "multiple_mixed"}


def largest_variant(images: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """Pick the largest API variant. Never rewrite a URL onto `/originals/`."""
    if not images:
        return None
    for name in VARIANT_PREFERENCE:
        details = images.get(name)
        if isinstance(details, dict) and details.get("url"):
            return name, details
    return None


def still_images(pin: dict[str, Any]) -> list[dict[str, Any]]:
    """Image downloads for one pin. Video pins and video carousel frames contribute nothing."""
    media = pin.get("media") or {}
    if not isinstance(media, dict):
        return []
    media_type = media.get("media_type")
    if media_type == "image":
        chosen = largest_variant(media.get("images") or {})
        if chosen is None:
            return []
        name, details = chosen
        return [
            {
                "index": 0,
                "variant": name,
                "url": details["url"],
                "width": details.get("width"),
                "height": details.get("height"),
            }
        ]
    if media_type not in {"multiple_images", "multiple_mixed"}:
        return []
    frames: list[dict[str, Any]] = []
    for index, item in enumerate(media.get("items") or []):
        if not isinstance(item, dict):
            continue
        chosen = largest_variant(item.get("images") or {})
        if chosen is None:
            continue
        name, details = chosen
        frames.append(
            {
                "index": index,
                "variant": name,
                "url": details["url"],
                "width": details.get("width"),
                "height": details.get("height"),
            }
        )
    return frames


class PinterestClient:
    def __init__(
        self,
        access_token: str,
        *,
        on_unauthorized: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = access_token
        self._on_unauthorized = on_unauthorized
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> PinterestClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _rotate_after_unauthorized(self) -> None:
        if self._on_unauthorized is None:
            raise PinterestAuthError(
                "Pinterest rejected the access token. Run `wardrobe auth` again."
            )
        self._token = self._on_unauthorized()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"

        def send() -> httpx.Response:
            return self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )

        response = send_with_retries(
            send,
            sleep=self._sleep,
            rng=self._rng,
            on_unauthorized=self._rotate_after_unauthorized if self._on_unauthorized else None,
        )
        body = response.json()
        if not isinstance(body, dict):
            raise PinterestAuthError(f"Expected a JSON object from {path}.")
        return body

    def iter_pages(
        self,
        path: str,
        *,
        bookmark: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield `(items, next_bookmark)` until Pinterest returns a null bookmark."""
        next_bookmark = bookmark
        while True:
            query: dict[str, Any] = {"page_size": PAGE_SIZE}
            if params:
                query.update(params)
            if next_bookmark:
                query["bookmark"] = next_bookmark
            payload = self.get(path, query)
            items = payload.get("items") or []
            if not isinstance(items, list):
                items = []
            raw_bookmark = payload.get("bookmark")
            next_bookmark = raw_bookmark if isinstance(raw_bookmark, str) and raw_bookmark else None
            yield items, next_bookmark
            if not next_bookmark:
                return

    def user_account(self) -> dict[str, Any]:
        return self.get("/user_account")

    def iter_boards(self) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self.iter_pages("/boards")

    def iter_sections(
        self, board_id: str
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self.iter_pages(f"/boards/{board_id}/sections")

    def iter_pins(
        self,
        board_id: str,
        *,
        bookmark: str | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        return self.iter_pages(f"/boards/{board_id}/pins", bookmark=bookmark)
