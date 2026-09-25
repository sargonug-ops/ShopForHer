from __future__ import annotations

import json
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from wardrobe.config import Config
from wardrobe.errors import PinterestAuthError
from wardrobe.http_retry import send_with_retries

AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
SCOPES = (
    "user_accounts:read",
    "boards:read",
    "boards:read_secret",
    "pins:read",
    "pins:read_secret",
)
ACCESS_REFRESH_SKEW_SECONDS = 24 * 60 * 60


def authorize_url(*, app_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def authorization_code_from_query(query: str, expected_state: str) -> str:
    params = parse_qs(query, keep_blank_values=True)
    received = params.get("state", [None])[0]
    if received != expected_state:
        raise PinterestAuthError("OAuth state did not match. Refusing the authorization code.")
    error = params.get("error", [None])[0]
    if error:
        description = params.get("error_description", [""])[0]
        raise PinterestAuthError(f"Pinterest authorization failed: {error} {description}".strip())
    code = params.get("code", [None])[0]
    if not code:
        raise PinterestAuthError("Pinterest did not return an authorization code.")
    return code


def tokens_from_response(body: dict, *, now: float | None = None) -> dict:
    """Normalize a token response. Expiries come from the payload, not fixed lifetimes."""
    current = time.time() if now is None else now
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not access or not refresh:
        raise PinterestAuthError(
            "Pinterest did not return both an access token and a refresh token. "
            "Run `wardrobe auth` again."
        )
    refresh_expires_at = body.get("refresh_token_expires_at")
    if refresh_expires_at is None and body.get("refresh_token_expires_in") is not None:
        refresh_expires_at = current + float(body["refresh_token_expires_in"])
    access_expires_at = None
    if body.get("expires_in") is not None:
        access_expires_at = current + float(body["expires_in"])
    return {
        "access_token": access,
        "refresh_token": refresh,
        "scope": body.get("scope"),
        "access_token_expires_at": access_expires_at,
        "refresh_token_expires_at": refresh_expires_at,
    }


class TokenStore:
    """Atomic token file. A refresh rotates the refresh token, so the new file must land."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict | None:
        temporary = self._temporary_path()
        promoted = self._read(temporary)
        if promoted is not None:
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            return promoted
        for candidate in (self.path, self._backup_path()):
            payload = self._read(candidate)
            if payload is not None:
                return payload
        return None

    def save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary_path()
        data = json.dumps(payload, indent=2)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if self.path.exists():
            backup = self._backup_path()
            os.replace(self.path, backup)
            os.chmod(backup, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        self._fsync_directory(self.path.parent)

    def _read(self, path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and payload.get("refresh_token"):
            return payload
        return None

    def _backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _temporary_path(self) -> Path:
        return self.path.with_name(self.path.name + ".tmp")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _post_token(config: Config, form: dict, *, client: httpx.Client | None = None) -> dict:
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = send_with_retries(
            lambda: http.post(
                TOKEN_URL,
                data=form,
                auth=httpx.BasicAuth(config.app_id, config.app_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )
        body = response.json()
    finally:
        if owns_client:
            http.close()
    if not isinstance(body, dict):
        raise PinterestAuthError("Pinterest token response was not a JSON object.")
    return tokens_from_response(body)


def exchange_code(config: Config, code: str, *, client: httpx.Client | None = None) -> dict:
    return _post_token(
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            "continuous_refresh": "true",
        },
        client=client,
    )


def refresh_tokens(
    config: Config,
    refresh_token: str,
    *,
    client: httpx.Client | None = None,
) -> dict:
    return _post_token(
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        client=client,
    )


def access_token_is_fresh(tokens: dict, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    access = tokens.get("access_token")
    expires_at = tokens.get("access_token_expires_at")
    if not access or expires_at is None:
        return False
    return float(expires_at) - current > ACCESS_REFRESH_SKEW_SECONDS


def ensure_access_token(
    config: Config,
    store: TokenStore,
    *,
    now: float | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Return a usable access token, refreshing and saving the rotated refresh token first."""
    tokens = store.load()
    if tokens is None:
        raise PinterestAuthError("No Pinterest tokens saved. Run `wardrobe auth` first.")
    if access_token_is_fresh(tokens, now=now):
        return str(tokens["access_token"])
    current = time.time() if now is None else now
    refresh_expires = tokens.get("refresh_token_expires_at")
    if refresh_expires is not None and float(refresh_expires) <= current:
        raise PinterestAuthError(
            "The Pinterest refresh token has expired. Run `wardrobe auth` again."
        )
    refreshed = refresh_tokens(config, str(tokens["refresh_token"]), client=client)
    store.save(refreshed)
    return str(refreshed["access_token"])


def wait_for_authorization_code(
    redirect_uri: str,
    expected_state: str,
    *,
    timeout: float = 300,
) -> str:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    callback_path = parsed.path or "/"
    if host == "localhost":
        host = "127.0.0.1"

    result: dict[str, str | None] = {"code": None, "error": None}
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            incoming = urlparse(self.path)
            if incoming.path != callback_path:
                self.send_error(404)
                return
            try:
                code = authorization_code_from_query(incoming.query, expected_state)
            except PinterestAuthError as exc:
                result["error"] = str(exc)
                body = str(exc).encode("utf-8")
                status = 400
            else:
                result["code"] = code
                body = b"Authorization received. You can close this window."
                status = 200
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            ready.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise PinterestAuthError(
            f"Could not listen on {redirect_uri}. Is port {port} already in use?"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not ready.wait(timeout):
            raise PinterestAuthError("Timed out waiting for the Pinterest authorization callback.")
    finally:
        server.shutdown()
        server.server_close()
    if result["error"]:
        raise PinterestAuthError(result["error"])
    if not result["code"]:
        raise PinterestAuthError("Pinterest did not return an authorization code.")
    return str(result["code"])


def run_authorization(
    config: Config,
    *,
    opener: Callable[[str], object] = webbrowser.open,
    timeout: float = 300,
) -> dict:
    config.require_app_credentials()
    state = secrets.token_urlsafe(32)
    url = authorize_url(app_id=config.app_id, redirect_uri=config.redirect_uri, state=state)
    print("Opening the Pinterest consent page.")
    print(url)
    opener(url)
    code = wait_for_authorization_code(config.redirect_uri, state, timeout=timeout)
    tokens = exchange_code(config, code)
    TokenStore(config.tokens_path).save(tokens)
    return tokens
