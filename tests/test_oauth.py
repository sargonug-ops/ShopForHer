from __future__ import annotations

import json
import stat

import httpx
import pytest
import respx

from wardrobe.errors import PinterestAuthError
from wardrobe.oauth import (
    TokenStore,
    authorization_code_from_query,
    authorize_url,
    ensure_access_token,
    exchange_code,
    tokens_from_response,
)
from tests.support import config_for


def test_authorize_url_includes_scopes_and_state() -> None:
    url = authorize_url(app_id="123", redirect_uri="http://localhost:8765/callback", state="abc")
    assert url.startswith("https://www.pinterest.com/oauth/?")
    assert "client_id=123" in url
    assert "user_accounts%3Aread" in url or "user_accounts:read" in url
    assert "boards%3Aread_secret" in url or "boards:read_secret" in url
    assert "pins%3Aread_secret" in url or "pins:read_secret" in url
    assert "state=abc" in url


def test_state_mismatch_is_rejected() -> None:
    with pytest.raises(PinterestAuthError, match="state"):
        authorization_code_from_query("code=abc&state=nope", "expected")


def test_token_expiries_come_from_the_response() -> None:
    saved = tokens_from_response(
        {
            "access_token": "pina_a",
            "refresh_token": "pinr_a",
            "expires_in": 100,
            "refresh_token_expires_at": 1_700_000_000,
            "scope": "pins:read",
        },
        now=1_000,
    )
    assert saved["access_token_expires_at"] == 1_100
    assert saved["refresh_token_expires_at"] == 1_700_000_000


def test_token_file_replaces_atomically_and_keeps_the_previous_token(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.save(
        {
            "access_token": "pina_old",
            "refresh_token": "pinr_old",
            "access_token_expires_at": 1,
            "refresh_token_expires_at": 2,
        }
    )
    store.save(
        {
            "access_token": "pina_new",
            "refresh_token": "pinr_new",
            "access_token_expires_at": 3,
            "refresh_token_expires_at": 4,
        }
    )
    assert json.loads(path.read_text())["refresh_token"] == "pinr_new"
    backup = path.with_name("tokens.json.bak")
    assert json.loads(backup.read_text())["refresh_token"] == "pinr_old"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_load_promotes_a_synced_temp_file(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    temporary = path.with_name("tokens.json.tmp")
    temporary.write_text(json.dumps({"access_token": "pina", "refresh_token": "pinr_tmp"}))
    loaded = TokenStore(path).load()
    assert loaded is not None
    assert loaded["refresh_token"] == "pinr_tmp"
    assert json.loads(path.read_text())["refresh_token"] == "pinr_tmp"
    assert not temporary.exists()


@respx.mock
def test_code_exchange_requests_continuous_refresh(tmp_path) -> None:
    route = respx.post("https://api.pinterest.com/v5/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "pina_code",
                "refresh_token": "pinr_code",
                "expires_in": 2592000,
                "refresh_token_expires_in": 5184000,
                "scope": "pins:read",
            },
        )
    )
    exchange_code(config_for(tmp_path), "the-code")
    body = route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "continuous_refresh=true" in body
    assert "code=the-code" in body


@respx.mock
def test_ensure_access_token_saves_rotated_refresh_token_before_returning(tmp_path) -> None:
    config = config_for(tmp_path)
    store = TokenStore(config.tokens_path)
    store.save(
        {
            "access_token": "pina_stale",
            "refresh_token": "pinr_old",
            "access_token_expires_at": 0,
            "refresh_token_expires_at": 9_999_999_999,
        }
    )
    respx.post("https://api.pinterest.com/v5/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "pina_fresh",
                "refresh_token": "pinr_rotated",
                "expires_in": 2592000,
                "refresh_token_expires_in": 5184000,
            },
        )
    )
    access = ensure_access_token(config, store, now=1_000)
    assert access == "pina_fresh"
    assert store.load()["refresh_token"] == "pinr_rotated"
