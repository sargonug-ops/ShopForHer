from __future__ import annotations

import httpx
import respx

from wardrobe.pinterest import PinterestClient, largest_variant, still_images


def test_largest_variant_prefers_1200x_and_does_not_rewrite_the_url() -> None:
    name, details = largest_variant(
        {
            "orig": {"url": "https://i.pinimg.com/originals/aa/bb.jpg", "width": 4000, "height": 4000},
            "600x": {"url": "https://i.pinimg.com/600x/aa/bb.jpg", "width": 600, "height": 800},
            "1200x": {"url": "https://i.pinimg.com/1200x/aa/bb.jpg", "width": 1200, "height": 1600},
        }
    )
    assert name == "1200x"
    assert details["url"] == "https://i.pinimg.com/1200x/aa/bb.jpg"
    assert "/originals/" not in details["url"]


def test_video_and_video_frames_are_not_downloaded() -> None:
    video = {"id": "1", "media": {"media_type": "video", "video_url": "https://example.com/v.mp4"}}
    assert still_images(video) == []
    mixed = {
        "id": "2",
        "media": {
            "media_type": "multiple_mixed",
            "items": [
                {"images": {"600x": {"url": "https://i.pinimg.com/600x/one.jpg", "width": 600, "height": 600}}},
                {"video_url": "https://example.com/clip.mp4"},
                {"images": {"1200x": {"url": "https://i.pinimg.com/1200x/three.jpg", "width": 1200, "height": 1200}}},
            ],
        },
    }
    frames = still_images(mixed)
    assert [frame["index"] for frame in frames] == [0, 2]
    assert frames[1]["url"] == "https://i.pinimg.com/1200x/three.jpg"


@respx.mock
def test_pagination_follows_bookmark_until_null() -> None:
    route = respx.get("https://api.pinterest.com/v5/boards").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"id": "1"}], "bookmark": "page-2"}),
            httpx.Response(200, json={"items": [{"id": "2"}], "bookmark": None}),
        ]
    )
    with PinterestClient("pina_test") as client:
        pages = list(client.iter_boards())
    assert [item["id"] for page, _bookmark in pages for item in page] == ["1", "2"]
    assert pages[0][1] == "page-2"
    assert pages[1][1] is None
    assert route.calls[0].request.url.params["page_size"] == "100"
    assert "bookmark" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["bookmark"] == "page-2"


@respx.mock
def test_429_honors_retry_after() -> None:
    slept: list[float] = []
    respx.get("https://api.pinterest.com/v5/user_account").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"id": "user-1", "username": "her"}),
        ]
    )
    with PinterestClient("pina_test", sleep=slept.append) as client:
        account = client.user_account()
    assert account["username"] == "her"
    assert slept == [0.0]


@respx.mock
def test_401_refreshes_once_and_retries() -> None:
    tokens = {"access": "pina_old"}

    def rotate() -> str:
        tokens["access"] = "pina_new"
        return tokens["access"]

    route = respx.get("https://api.pinterest.com/v5/user_account").mock(
        side_effect=[
            httpx.Response(401, json={"code": 2, "message": "Authentication failed."}),
            httpx.Response(200, json={"id": "user-1", "username": "her"}),
        ]
    )
    with PinterestClient("pina_old", on_unauthorized=rotate) as client:
        account = client.user_account()
    assert account["username"] == "her"
    assert tokens["access"] == "pina_new"
    assert route.calls[1].request.headers["Authorization"] == "Bearer pina_new"
