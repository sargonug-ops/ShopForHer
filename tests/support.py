from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from wardrobe.config import Config
from wardrobe.oauth import TokenStore
from wardrobe.pinterest import still_images

API = "https://api.pinterest.com"


def write_tokens(path: Path, *, access: str = "pina_access", refresh: str = "pinr_refresh") -> None:
    TokenStore(path).save(
        {
            "access_token": access,
            "refresh_token": refresh,
            "scope": "pins:read",
            "access_token_expires_at": 9_999_999_999,
            "refresh_token_expires_at": 9_999_999_999,
        }
    )


def config_for(tmp_path: Path) -> Config:
    return Config(
        app_id="app-id",
        app_secret="app-secret",
        redirect_uri="http://localhost:8765/callback",
        data_dir=tmp_path / "data",
    )


def image_pin(
    pin_id: str,
    *,
    section_id: str | None = None,
    title: str = "Linen dress",
    link: str = "https://shop.example/dress",
    url: str | None = None,
) -> dict:
    return {
        "id": pin_id,
        "board_id": "board-1",
        "board_section_id": section_id,
        "title": title,
        "description": "A dress",
        "alt_text": "alt",
        "link": link,
        "dominant_color": "#6E7874",
        "creative_type": "REGULAR",
        "created_at": "2020-01-01T20:10:40+00:00",
        "parent_pin_id": None,
        "media": {
            "media_type": "image",
            "images": {
                "150x150": {"url": "https://i.pinimg.com/150x150/aa.jpg", "width": 150, "height": 150},
                "600x": {"url": "https://i.pinimg.com/600x/aa.jpg", "width": 600, "height": 800},
                "1200x": {
                    "url": url or f"https://i.pinimg.com/1200x/{pin_id}.jpg",
                    "width": 1200,
                    "height": 1600,
                },
                "orig": {
                    "url": "https://i.pinimg.com/originals/aa/bb.jpg",
                    "width": 2000,
                    "height": 3000,
                },
            },
        },
    }


def board(board_id: str = "board-1", name: str = "Dresses") -> dict:
    return {
        "id": board_id,
        "name": name,
        "description": "Things to wear",
        "privacy": "SECRET",
        "pin_count": 2,
        "owner": {"username": "her"},
    }


class FakePinterest:
    """Recorded Pinterest responses keyed by path. Image bodies are addressed by URL."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.pages: dict[str, list[dict]] = {}
        self.images: dict[str, bytes] = {}
        self.fail_times: dict[str, int] = {}
        self.rate_limit_once: set[str] = set()
        self._rate_limited: set[str] = set()
        self.token_response = {
            "access_token": "pina_new",
            "refresh_token": "pinr_rotated",
            "token_type": "bearer",
            "expires_in": 2592000,
            "refresh_token_expires_in": 5184000,
            "scope": "boards:read pins:read",
        }

    def add_pages(self, key: str, pages: list[dict]) -> None:
        self.pages[key] = pages

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method == "POST" and request.url.path == "/v5/oauth/token":
            return httpx.Response(200, json=self.token_response)
        if request.url.path == "/v5/user_account":
            account = self.pages.get("account", [[]])[0][0]
            return httpx.Response(200, json=account)
        if request.url.host == "i.pinimg.com":
            body = self.images.get(str(request.url))
            if body is None:
                return httpx.Response(404, text="missing image")
            return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

        path = request.url.path
        remaining = self.fail_times.get(path, 0)
        if remaining:
            self.fail_times[path] = remaining - 1
            raise httpx.ConnectError("boom", request=request)
        if path in self.rate_limit_once and path not in self._rate_limited:
            self._rate_limited.add(path)
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})

        key = self._page_key(request)
        pages = self.pages.get(key)
        if pages is None:
            return httpx.Response(404, json={"message": f"unhandled {path}"})
        bookmark = request.url.params.get("bookmark")
        index = int(bookmark) if bookmark else 0
        if index >= len(pages):
            return httpx.Response(200, json={"items": [], "bookmark": None})
        payload = pages[index]
        next_bookmark = str(index + 1) if index + 1 < len(pages) else None
        return httpx.Response(200, json={"items": payload, "bookmark": next_bookmark})

    def _page_key(self, request: httpx.Request) -> str:
        path = request.url.path
        if path == "/v5/user_account":
            return "account"
        if path == "/v5/boards":
            return "boards"
        parts = path.strip("/").split("/")
        # v5 / boards / {id} / sections|pins
        if len(parts) == 4 and parts[0] == "v5" and parts[1] == "boards":
            return f"{parts[3]}:{parts[2]}"
        return path

    def token_form(self) -> dict[str, str]:
        posts = [call for call in self.calls if call.method == "POST"]
        if not posts:
            return {}
        parsed = parse_qs(posts[-1].content.decode())
        return {key: values[0] for key, values in parsed.items()}

    def image_calls(self) -> list[str]:
        return [str(call.url) for call in self.calls if call.url.host == "i.pinimg.com"]


def install(respx_mock, fake: FakePinterest) -> None:
    respx_mock.route().mock(side_effect=fake.handler)


def standard_archive(fake: FakePinterest, pins: list[dict] | None = None) -> None:
    fake.add_pages("account", [[{"id": "user-1", "username": "her", "account_type": "PINNER"}]])
    fake.add_pages("boards", [[board()]])
    fake.add_pages("sections:board-1", [[{"id": "section-1", "name": "Wedding"}]])
    chosen_pins = pins if pins is not None else [image_pin("pin-1", section_id="section-1")]
    fake.add_pages("pins:board-1", [chosen_pins])
    for pin in chosen_pins:
        for frame in still_images(pin):
            fake.images[frame["url"]] = f"bytes-{pin['id']}-{frame['index']}".encode()
