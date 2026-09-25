# ShopForHer

Phase 1 is a local archive of one Pinterest account: boards, sections, pins, and image files. Later phases (crops, style modes, a shop index) are not in this package.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Put the Pinterest app id and secret in `.env`. Register the redirect URI `http://localhost:8765/callback` on the app. A trial app can only read the account that owns the app.

## Commands

```bash
wardrobe auth      # browser consent; tokens land in data/tokens.json
wardrobe sync      # boards, sections, pins, and the largest image variant
wardrobe gallery   # writes data/gallery.html and opens it
```

`data/` and `.env` are gitignored. A second sync skips image files whose URL and SHA-256 have not changed. Pins that disappear stay in the database with `missing_at` set.

```bash
pytest
```
