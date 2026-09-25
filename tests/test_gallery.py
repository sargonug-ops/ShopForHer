from __future__ import annotations

import respx

from wardrobe.gallery import render_gallery, safe_href, write_gallery
from wardrobe.store import Store
from wardrobe.sync import sync_archive
from tests.support import FakePinterest, config_for, image_pin, install, standard_archive, write_tokens


def test_safe_href_allows_only_http_and_https() -> None:
    assert safe_href("https://shop.example/dress") == "https://shop.example/dress"
    assert safe_href("http://shop.example/dress") == "http://shop.example/dress"
    assert safe_href("javascript:alert(1)") is None
    assert safe_href("data:text/html,hi") is None
    assert safe_href(None) is None


@respx.mock
def test_gallery_escapes_text_and_groups_sections(tmp_path) -> None:
    fake = FakePinterest()
    pin = image_pin(
        "pin-1",
        section_id="section-1",
        title='<script>alert(1)</script>',
        link="javascript:alert(1)",
    )
    pin["alt_text"] = "<b>alt</b>"
    standard_archive(fake, [pin])
    video = {
        "id": "pin-v",
        "title": "Clip & more",
        "link": "https://shop.example/video?q=1&ok=2",
        "media": {"media_type": "video"},
    }
    fake.pages["pins:board-1"] = [[pin, video]]
    install(respx.mock, fake)
    config = config_for(tmp_path)
    write_tokens(config.tokens_path)
    sync_archive(config)
    with Store(config.db_path) as store:
        html = render_gallery(store.gallery_rows())
        path = write_gallery(store, config.gallery_path)
    text = path.read_text(encoding="utf-8")
    assert text == html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:" not in html
    assert "https://shop.example/video?q=1&amp;ok=2" in html
    assert "Wedding" in html
    assert "Dresses" in html
    assert 'src="images/pin-1-0.jpg"' in html
    assert "No image" in html or "video" in html
    assert "Clip &amp; more" in html
