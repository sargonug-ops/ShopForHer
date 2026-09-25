from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_DATA_DIR = "data"


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into the environment without overriding existing vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    redirect_uri: str
    data_dir: Path

    @classmethod
    def from_env(cls, *, env_file: Path | None = None, data_dir: Path | None = None) -> Config:
        load_dotenv(env_file or Path.cwd() / ".env")
        root = data_dir or Path(os.environ.get("WARDROBE_DATA_DIR", DEFAULT_DATA_DIR))
        if not root.is_absolute():
            root = Path.cwd() / root
        return cls(
            app_id=os.environ.get("PINTEREST_APP_ID", "").strip(),
            app_secret=os.environ.get("PINTEREST_APP_SECRET", "").strip(),
            redirect_uri=os.environ.get("PINTEREST_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
            data_dir=root,
        )

    def require_app_credentials(self) -> None:
        from wardrobe.errors import PinterestAuthError

        missing = [
            name
            for name, value in (
                ("PINTEREST_APP_ID", self.app_id),
                ("PINTEREST_APP_SECRET", self.app_secret),
            )
            if not value
        ]
        if missing:
            raise PinterestAuthError(
                "Missing "
                + " and ".join(missing)
                + ". Copy .env.example to .env and fill in the Pinterest app credentials."
            )

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wardrobe.sqlite"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def gallery_path(self) -> Path:
        return self.data_dir / "gallery.html"
