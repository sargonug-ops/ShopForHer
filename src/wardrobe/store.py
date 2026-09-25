from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wardrobe.errors import WardrobeError

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    account_type TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    privacy TEXT,
    pin_count INTEGER,
    owner_username TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS board_sections (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id),
    name TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    id TEXT PRIMARY KEY,
    board_id TEXT REFERENCES boards(id),
    section_id TEXT REFERENCES board_sections(id),
    title TEXT,
    description TEXT,
    alt_text TEXT,
    link TEXT,
    dominant_color TEXT,
    creative_type TEXT,
    created_at TEXT,
    media_type TEXT,
    parent_pin_id TEXT,
    missing_at TEXT,
    raw_json TEXT NOT NULL,
    last_sync_run_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pin_images (
    pin_id TEXT NOT NULL REFERENCES pins(id),
    item_index INTEGER NOT NULL,
    variant TEXT,
    width INTEGER,
    height INTEGER,
    source_url TEXT,
    local_path TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    PRIMARY KEY (pin_id, item_index)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    boards_seen INTEGER NOT NULL DEFAULT 0,
    sections_seen INTEGER NOT NULL DEFAULT 0,
    pins_seen INTEGER NOT NULL DEFAULT 0,
    images_downloaded INTEGER NOT NULL DEFAULT 0,
    images_skipped INTEGER NOT NULL DEFAULT 0,
    pins_marked_missing INTEGER NOT NULL DEFAULT 0,
    error_text TEXT,
    board_cursors TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS pins_board_id ON pins(board_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert_account(self, *, account_id: str, username: str, account_type: str | None) -> None:
        existing = self.conn.execute("SELECT id, username FROM account").fetchone()
        if existing is not None and existing["id"] != account_id:
            raise WardrobeError(
                f"This archive belongs to @{existing['username']} ({existing['id']}), "
                f"not {account_id}. Use a different data directory."
            )
        self.conn.execute(
            """
            INSERT INTO account (id, username, account_type, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                account_type = excluded.account_type,
                updated_at = excluded.updated_at
            """,
            (account_id, username, account_type, utc_now()),
        )
        self.conn.commit()

    def upsert_board(self, board: dict[str, Any]) -> None:
        owner = board.get("owner") or {}
        owner_username = owner.get("username") if isinstance(owner, dict) else None
        self.conn.execute(
            """
            INSERT INTO boards (id, name, description, privacy, pin_count, owner_username, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                privacy = excluded.privacy,
                pin_count = excluded.pin_count,
                owner_username = excluded.owner_username,
                updated_at = excluded.updated_at
            """,
            (
                str(board["id"]),
                board.get("name"),
                board.get("description"),
                board.get("privacy"),
                board.get("pin_count"),
                owner_username,
                utc_now(),
            ),
        )
        self.conn.commit()

    def ensure_section(self, *, section_id: str, board_id: str) -> None:
        """Insert a section id from a pin without wiping a name we already stored."""
        self.conn.execute(
            """
            INSERT INTO board_sections (id, board_id, name, updated_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (section_id, board_id, utc_now()),
        )

    def upsert_section(self, *, section_id: str, board_id: str, name: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO board_sections (id, board_id, name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                board_id = excluded.board_id,
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (section_id, board_id, name, utc_now()),
        )
        self.conn.commit()

    def upsert_pin(self, pin: dict[str, Any], *, board_id: str, sync_run_id: int) -> None:
        section_id = pin.get("board_section_id")
        if section_id is not None:
            section_id = str(section_id)
            self.ensure_section(section_id=section_id, board_id=board_id)
        media = pin.get("media") or {}
        media_type = media.get("media_type") if isinstance(media, dict) else None
        self.conn.execute(
            """
            INSERT INTO pins (
                id, board_id, section_id, title, description, alt_text, link,
                dominant_color, creative_type, created_at, media_type, parent_pin_id,
                missing_at, raw_json, last_sync_run_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                board_id = excluded.board_id,
                section_id = excluded.section_id,
                title = excluded.title,
                description = excluded.description,
                alt_text = excluded.alt_text,
                link = excluded.link,
                dominant_color = excluded.dominant_color,
                creative_type = excluded.creative_type,
                created_at = excluded.created_at,
                media_type = excluded.media_type,
                parent_pin_id = excluded.parent_pin_id,
                missing_at = NULL,
                raw_json = excluded.raw_json,
                last_sync_run_id = excluded.last_sync_run_id,
                updated_at = excluded.updated_at
            """,
            (
                str(pin["id"]),
                board_id,
                section_id,
                pin.get("title"),
                pin.get("description"),
                pin.get("alt_text"),
                pin.get("link"),
                pin.get("dominant_color"),
                pin.get("creative_type"),
                pin.get("created_at"),
                media_type,
                pin.get("parent_pin_id"),
                json.dumps(pin),
                sync_run_id,
                utc_now(),
            ),
        )
        self.conn.commit()

    def get_pin_image(self, pin_id: str, item_index: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT pin_id, item_index, variant, width, height, source_url,
                   local_path, byte_size, sha256
            FROM pin_images
            WHERE pin_id = ? AND item_index = ?
            """,
            (pin_id, item_index),
        ).fetchone()

    def upsert_pin_image(
        self,
        *,
        pin_id: str,
        item_index: int,
        variant: str,
        width: int | None,
        height: int | None,
        source_url: str,
        local_path: str,
        byte_size: int,
        sha256: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO pin_images (
                pin_id, item_index, variant, width, height, source_url,
                local_path, byte_size, sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pin_id, item_index) DO UPDATE SET
                variant = excluded.variant,
                width = excluded.width,
                height = excluded.height,
                source_url = excluded.source_url,
                local_path = excluded.local_path,
                byte_size = excluded.byte_size,
                sha256 = excluded.sha256
            """,
            (pin_id, item_index, variant, width, height, source_url, local_path, byte_size, sha256),
        )
        self.conn.commit()

    def delete_pin_images_except(self, pin_id: str, keep_indexes: set[int]) -> list[str]:
        rows = self.conn.execute(
            "SELECT item_index, local_path FROM pin_images WHERE pin_id = ?",
            (pin_id,),
        ).fetchall()
        removed: list[str] = []
        for row in rows:
            if row["item_index"] in keep_indexes:
                continue
            if row["local_path"]:
                removed.append(row["local_path"])
            self.conn.execute(
                "DELETE FROM pin_images WHERE pin_id = ? AND item_index = ?",
                (pin_id, row["item_index"]),
            )
        self.conn.commit()
        return removed

    def mark_board_pins_missing(self, board_id: str, sync_run_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE pins
            SET missing_at = ?
            WHERE board_id = ?
              AND missing_at IS NULL
              AND (last_sync_run_id IS NULL OR last_sync_run_id != ?)
            """,
            (utc_now(), board_id, sync_run_id),
        )
        self.conn.commit()
        return cursor.rowcount

    def begin_sync_run(self) -> tuple[int, dict[str, Any], dict[str, int]]:
        """Resume the latest unfinished run, or start a new one."""
        latest = self.conn.execute(
            """
            SELECT id, status, finished_at, board_cursors,
                   boards_seen, sections_seen, pins_seen,
                   images_downloaded, images_skipped, pins_marked_missing
            FROM sync_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is not None and latest["status"] in {"running", "failed"}:
            self.conn.execute(
                """
                UPDATE sync_runs
                SET status = 'running', finished_at = NULL, error_text = NULL
                WHERE id = ?
                """,
                (latest["id"],),
            )
            self.conn.commit()
            cursors = json.loads(latest["board_cursors"] or "{}")
            counts = {
                "boards_seen": latest["boards_seen"],
                "sections_seen": latest["sections_seen"],
                "pins_seen": latest["pins_seen"],
                "images_downloaded": latest["images_downloaded"],
                "images_skipped": latest["images_skipped"],
                "pins_marked_missing": latest["pins_marked_missing"],
            }
            return latest["id"], cursors, counts
        started = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO sync_runs (started_at, status, board_cursors)
            VALUES (?, 'running', '{}')
            """,
            (started,),
        )
        self.conn.commit()
        counts = {
            "boards_seen": 0,
            "sections_seen": 0,
            "pins_seen": 0,
            "images_downloaded": 0,
            "images_skipped": 0,
            "pins_marked_missing": 0,
        }
        return int(cursor.lastrowid), {}, counts

    def update_sync_run(
        self,
        run_id: int,
        *,
        cursors: dict[str, Any],
        counts: dict[str, int],
    ) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs
            SET boards_seen = ?,
                sections_seen = ?,
                pins_seen = ?,
                images_downloaded = ?,
                images_skipped = ?,
                pins_marked_missing = ?,
                board_cursors = ?
            WHERE id = ?
            """,
            (
                counts["boards_seen"],
                counts["sections_seen"],
                counts["pins_seen"],
                counts["images_downloaded"],
                counts["images_skipped"],
                counts["pins_marked_missing"],
                json.dumps(cursors),
                run_id,
            ),
        )
        self.conn.commit()

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        cursors: dict[str, Any],
        counts: dict[str, int],
        error_text: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs
            SET status = ?,
                finished_at = ?,
                error_text = ?,
                boards_seen = ?,
                sections_seen = ?,
                pins_seen = ?,
                images_downloaded = ?,
                images_skipped = ?,
                pins_marked_missing = ?,
                board_cursors = ?
            WHERE id = ?
            """,
            (
                status,
                utc_now(),
                error_text,
                counts["boards_seen"],
                counts["sections_seen"],
                counts["pins_seen"],
                counts["images_downloaded"],
                counts["images_skipped"],
                counts["pins_marked_missing"],
                json.dumps(cursors),
                run_id,
            ),
        )
        self.conn.commit()

    def gallery_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                b.id AS board_id,
                b.name AS board_name,
                b.description AS board_description,
                b.privacy AS board_privacy,
                s.id AS section_id,
                s.name AS section_name,
                p.id AS pin_id,
                p.title AS pin_title,
                p.alt_text AS alt_text,
                p.link AS link,
                p.media_type AS media_type,
                p.dominant_color AS dominant_color,
                p.created_at AS created_at,
                i.item_index AS item_index,
                i.local_path AS local_path
            FROM boards b
            LEFT JOIN pins p ON p.board_id = b.id AND p.missing_at IS NULL
            LEFT JOIN board_sections s ON s.id = p.section_id
            LEFT JOIN pin_images i ON i.pin_id = p.id
            ORDER BY b.name, s.name, p.created_at, p.id, i.item_index
            """
        ).fetchall()
