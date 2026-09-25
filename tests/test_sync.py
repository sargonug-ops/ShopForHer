from __future__ import annotations

import httpx
import pytest
import respx

from wardrobe.errors import PinterestAuthError
from wardrobe.http_retry import error_from_response
from wardrobe.store import Store
from wardrobe.sync import sync_archive
from tests.support import FakePinterest, config_for, image_pin, install, standard_archive, write_tokens


@respx.mock
def test_sync_downloads_largest_variant_and_keeps_section_name(tmp_path) -> None:
    fake = FakePinterest()
    standard_archive(fake)
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    counts = sync_archive(config)
    assert counts["images_downloaded"] == 1
    assert counts["images_skipped"] == 0
    assert fake.image_calls() == ["https://i.pinimg.com/1200x/pin-1.jpg"]
    with Store(config.db_path) as store:
        section = store.conn.execute("SELECT name FROM board_sections").fetchone()
        pin = store.conn.execute("SELECT raw_json, section_id, missing_at FROM pins").fetchone()
        image = store.conn.execute("SELECT variant, source_url, sha256 FROM pin_images").fetchone()
    assert section["name"] == "Wedding"
    assert pin["section_id"] == "section-1"
    assert pin["missing_at"] is None
    assert "Linen dress" in pin["raw_json"]
    assert image["variant"] == "1200x"
    assert image["source_url"] == "https://i.pinimg.com/1200x/pin-1.jpg"
    assert "/originals/" not in image["source_url"]
    assert (config.images_dir / "pin-1-0.jpg").is_file()


@respx.mock
def test_second_sync_does_not_redownload_unchanged_images(tmp_path) -> None:
    fake = FakePinterest()
    standard_archive(fake)
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    sync_archive(config)
    counts = sync_archive(config)
    assert counts["images_downloaded"] == 0
    assert counts["images_skipped"] == 1
    assert len(fake.image_calls()) == 1


@respx.mock
def test_missing_pin_is_kept_and_reappears(tmp_path) -> None:
    fake = FakePinterest()
    pins = [
        image_pin("pin-1", section_id="section-1"),
        image_pin("pin-2", section_id="section-1", title="Second"),
    ]
    standard_archive(fake, pins)
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    sync_archive(config)

    fake.pages["pins:board-1"] = [[image_pin("pin-2", section_id="section-1", title="Second")]]
    counts = sync_archive(config)
    assert counts["pins_marked_missing"] == 1
    with Store(config.db_path) as store:
        missing = store.conn.execute("SELECT missing_at FROM pins WHERE id = 'pin-1'").fetchone()
        image = store.conn.execute("SELECT local_path FROM pin_images WHERE pin_id = 'pin-1'").fetchone()
    assert missing["missing_at"] is not None
    assert image is not None
    assert (config.data_dir / image["local_path"]).is_file()

    fake.pages["pins:board-1"] = [pins]
    sync_archive(config)
    with Store(config.db_path) as store:
        restored = store.conn.execute("SELECT missing_at FROM pins WHERE id = 'pin-1'").fetchone()
    assert restored["missing_at"] is None


@respx.mock
def test_removed_carousel_frame_drops_its_file(tmp_path) -> None:
    fake = FakePinterest()
    carousel = {
        "id": "pin-c",
        "title": "Two looks",
        "link": "https://shop.example/looks",
        "media": {
            "media_type": "multiple_images",
            "items": [
                {
                    "title": "First",
                    "description": "One",
                    "link": "https://shop.example/one",
                    "images": {"1200x": {"url": "https://i.pinimg.com/1200x/frame-0.jpg", "width": 1200, "height": 1200}},
                },
                {
                    "title": "Second",
                    "description": "Two",
                    "link": "https://shop.example/two",
                    "images": {"600x": {"url": "https://i.pinimg.com/600x/frame-1.jpg", "width": 600, "height": 800}},
                },
            ],
        },
    }
    standard_archive(fake, [carousel])
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    sync_archive(config)
    assert (config.images_dir / "pin-c-0.jpg").is_file()
    assert (config.images_dir / "pin-c-1.jpg").is_file()

    carousel["media"]["items"] = carousel["media"]["items"][:1]
    fake.pages["pins:board-1"] = [[carousel]]
    fake.images.pop("https://i.pinimg.com/600x/frame-1.jpg", None)
    sync_archive(config)
    with Store(config.db_path) as store:
        indexes = [row["item_index"] for row in store.conn.execute("SELECT item_index FROM pin_images ORDER BY item_index")]
    assert indexes == [0]
    assert (config.images_dir / "pin-c-0.jpg").is_file()
    assert not (config.images_dir / "pin-c-1.jpg").exists()
    with Store(config.db_path) as store:
        raw = store.conn.execute("SELECT raw_json FROM pins WHERE id = 'pin-c'").fetchone()["raw_json"]
    assert "https://shop.example/one" in raw


@respx.mock
def test_video_pin_stores_metadata_without_an_image_file(tmp_path) -> None:
    fake = FakePinterest()
    video = {
        "id": "pin-v",
        "title": "Clip",
        "media": {"media_type": "video", "cover_image_url": "https://i.pinimg.com/videos/cover.jpg"},
    }
    standard_archive(fake, [video])
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    sync_archive(config)
    assert fake.image_calls() == []
    assert list(config.images_dir.glob("*")) == []
    with Store(config.db_path) as store:
        media_type = store.conn.execute("SELECT media_type FROM pins").fetchone()["media_type"]
    assert media_type == "video"


@respx.mock
def test_failed_page_resumes_without_marking_seen_pins_missing(tmp_path) -> None:
    fake = FakePinterest()
    standard_archive(
        fake,
        [image_pin("pin-1", section_id="section-1"), image_pin("pin-2", section_id="section-1", title="Later")],
    )
    fake.pages["pins:board-1"] = [
        [image_pin("pin-1", section_id="section-1")],
        [image_pin("pin-2", section_id="section-1", title="Later")],
    ]
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)

    calls = {"pins": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/boards/board-1/pins":
            calls["pins"] += 1
            if calls["pins"] == 2:
                raise httpx.ConnectError("dropped", request=request)
        return fake.handler(request)

    respx.mock.route().mock(side_effect=flaky)
    with pytest.raises(httpx.ConnectError):
        sync_archive(config)
    assert (config.images_dir / "pin-1-0.jpg").is_file()
    assert fake.image_calls().count("https://i.pinimg.com/1200x/pin-1.jpg") == 1

    counts = sync_archive(config)
    assert counts["pins_marked_missing"] == 0
    with Store(config.db_path) as store:
        rows = store.conn.execute(
            "SELECT id, missing_at FROM pins ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["missing_at"]) for row in rows] == [("pin-1", None), ("pin-2", None)]
    assert fake.image_calls().count("https://i.pinimg.com/1200x/pin-1.jpg") == 1


def test_trial_auth_failure_names_the_limitation() -> None:
    response = httpx.Response(401, json={"message": "Authentication failed."})
    error = error_from_response(response)
    assert isinstance(error, PinterestAuthError)
    assert "trial" in str(error).lower()
