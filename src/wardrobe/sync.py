from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from wardrobe.config import Config
from wardrobe.errors import PinterestAuthError, PinterestError
from wardrobe.http_retry import send_with_retries
from wardrobe.oauth import TokenStore, ensure_access_token, refresh_tokens
from wardrobe.pinterest import PinterestClient, still_images
from wardrobe.store import Store

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def extension_for(content_type: str | None, url: str) -> str:
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base in CONTENT_TYPE_EXTENSIONS:
            return CONTENT_TYPE_EXTENSIONS[base]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    if suffix in {".jpg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cursor_state(cursors: dict, board_id: str) -> dict:
    state = cursors.get(board_id)
    if isinstance(state, dict):
        return state
    return {}


def sync_archive(config: Config, *, client: httpx.Client | None = None) -> dict:
    config.require_app_credentials()
    store = Store(config.db_path)
    token_store = TokenStore(config.tokens_path)
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    owns_http = client is None
    try:
        access_token = ensure_access_token(config, token_store, client=http)

        def on_unauthorized() -> str:
            current = token_store.load()
            if current is None:
                raise PinterestAuthError("No Pinterest tokens saved. Run `wardrobe auth` first.")
            refreshed = refresh_tokens(config, str(current["refresh_token"]), client=http)
            token_store.save(refreshed)
            return str(refreshed["access_token"])

        api = PinterestClient(access_token, on_unauthorized=on_unauthorized, client=http)
        run_id, cursors, counts = store.begin_sync_run()
        try:
            _walk(config, store, api, http, run_id, cursors, counts)
        except Exception as exc:
            store.finish_sync_run(
                run_id,
                status="failed",
                cursors=cursors,
                counts=counts,
                error_text=str(exc),
            )
            raise
        store.finish_sync_run(run_id, status="ok", cursors=cursors, counts=counts)
        return counts
    finally:
        store.close()
        if owns_http:
            http.close()


def _walk(
    config: Config,
    store: Store,
    api: PinterestClient,
    http: httpx.Client,
    run_id: int,
    cursors: dict,
    counts: dict,
) -> None:
    account = api.user_account()
    account_id = str(account.get("id") or account.get("username") or "")
    username = account.get("username")
    if not account_id or not username:
        raise PinterestError("Pinterest user account response did not include an id and username.")
    store.upsert_account(
        account_id=account_id,
        username=str(username),
        account_type=account.get("account_type"),
    )

    boards: list[dict] = []
    for page, _bookmark in api.iter_boards():
        boards.extend(page)
    counts["boards_seen"] = len(boards)
    for board in boards:
        store.upsert_board(board)
    store.update_sync_run(run_id, cursors=cursors, counts=counts)

    seen_board_ids: set[str] = set()
    for board in boards:
        board_id = str(board["id"])
        seen_board_ids.add(board_id)
        state = _cursor_state(cursors, board_id)
        if state.get("done"):
            counts["pins_marked_missing"] += store.mark_board_pins_missing(board_id, run_id)
            continue
        section_count = 0
        for page, _bookmark in api.iter_sections(board_id):
            for section in page:
                store.upsert_section(
                    section_id=str(section["id"]),
                    board_id=board_id,
                    name=section.get("name"),
                )
                section_count += 1
        counts["sections_seen"] += section_count
        bookmark = state.get("bookmark")
        for page, next_bookmark in api.iter_pins(board_id, bookmark=bookmark):
            for pin in page:
                store.upsert_pin(pin, board_id=board_id, sync_run_id=run_id)
                downloaded, skipped = _store_pin_images(config, store, http, pin)
                counts["pins_seen"] += 1
                counts["images_downloaded"] += downloaded
                counts["images_skipped"] += skipped
            if next_bookmark:
                cursors[board_id] = {"bookmark": next_bookmark}
            else:
                cursors[board_id] = {"done": True}
            store.update_sync_run(run_id, cursors=cursors, counts=counts)
        if not cursors.get(board_id, {}).get("done"):
            cursors[board_id] = {"done": True}
            store.update_sync_run(run_id, cursors=cursors, counts=counts)
        counts["pins_marked_missing"] += store.mark_board_pins_missing(board_id, run_id)
        store.update_sync_run(run_id, cursors=cursors, counts=counts)

    known_boards = store.conn.execute("SELECT id FROM boards").fetchall()
    for row in known_boards:
        if row["id"] in seen_board_ids:
            continue
        counts["pins_marked_missing"] += store.mark_board_pins_missing(row["id"], run_id)
    store.update_sync_run(run_id, cursors=cursors, counts=counts)


def _store_pin_images(
    config: Config,
    store: Store,
    http: httpx.Client,
    pin: dict,
) -> tuple[int, int]:
    pin_id = str(pin["id"])
    frames = still_images(pin)
    downloaded = 0
    skipped = 0
    keep: set[int] = set()
    config.images_dir.mkdir(parents=True, exist_ok=True)
    for frame in frames:
        index = int(frame["index"])
        keep.add(index)
        source_url = str(frame["url"])
        existing = store.get_pin_image(pin_id, index)
        if existing is not None and existing["source_url"] == source_url and existing["sha256"]:
            local = config.data_dir / existing["local_path"]
            if local.is_file() and sha256_file(local) == existing["sha256"]:
                skipped += 1
                continue
        payload, content_type = _download(http, source_url)
        digest = sha256_bytes(payload)
        extension = extension_for(content_type, source_url)
        relative = f"images/{pin_id}-{index}{extension}"
        destination = config.data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and sha256_file(destination) == digest:
            skipped += 1
        else:
            partial = destination.with_suffix(destination.suffix + ".partial")
            partial.write_bytes(payload)
            os.replace(partial, destination)
            downloaded += 1
        store.upsert_pin_image(
            pin_id=pin_id,
            item_index=index,
            variant=str(frame["variant"]),
            width=frame.get("width"),
            height=frame.get("height"),
            source_url=source_url,
            local_path=relative,
            byte_size=len(payload),
            sha256=digest,
        )
    for relative in store.delete_pin_images_except(pin_id, keep):
        _remove_image_file(config, relative)
    return downloaded, skipped


def _download(http: httpx.Client, url: str) -> tuple[bytes, str | None]:
    response = send_with_retries(lambda: http.get(url))
    return response.content, response.headers.get("content-type")


def _remove_image_file(config: Config, relative: str) -> None:
    path = (config.data_dir / relative).resolve()
    images = config.images_dir.resolve()
    try:
        path.relative_to(images)
    except ValueError:
        return
    if path.is_file():
        path.unlink()
