from __future__ import annotations

import argparse
import sys

import httpx

from wardrobe.config import Config
from wardrobe.errors import PinterestAuthError, WardrobeError
from wardrobe.gallery import open_gallery, write_gallery
from wardrobe.oauth import run_authorization
from wardrobe.pinterest import PinterestClient
from wardrobe.store import Store
from wardrobe.sync import sync_archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wardrobe")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Authorize this machine against the Pinterest account that owns the app")
    sub.add_parser("sync", help="Download boards, sections, pins, and images")
    gallery = sub.add_parser("gallery", help="Write data/gallery.html grouped by board")
    gallery.add_argument(
        "--no-open",
        action="store_true",
        help="Write the HTML file without opening it",
    )
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        if args.command == "auth":
            return _auth(config)
        if args.command == "sync":
            return _sync(config)
        if args.command == "gallery":
            return _gallery(config, open_browser=not args.no_open)
    except (WardrobeError, OSError, httpx.HTTPError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 2


def _auth(config: Config) -> int:
    tokens = run_authorization(config)
    with PinterestClient(str(tokens["access_token"])) as client:
        account = client.user_account()
    username = account.get("username") or "unknown"
    print(f"Saved tokens for @{username}.")
    print(
        "A trial app can only read the Pinterest account that owns the app. "
        "If this is the wrong account, reconnect before syncing."
    )
    return 0


def _sync(config: Config) -> int:
    counts = sync_archive(config)
    print(
        "Sync finished. "
        f"boards={counts['boards_seen']} "
        f"sections={counts['sections_seen']} "
        f"pins={counts['pins_seen']} "
        f"images_downloaded={counts['images_downloaded']} "
        f"images_skipped={counts['images_skipped']} "
        f"pins_marked_missing={counts['pins_marked_missing']}"
    )
    return 0


def _gallery(config: Config, *, open_browser: bool) -> int:
    if not config.db_path.is_file():
        raise WardrobeError("No archive yet. Run `wardrobe sync` first.")
    with Store(config.db_path) as store:
        path = write_gallery(store, config.gallery_path)
    print(path)
    if open_browser:
        open_gallery(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
