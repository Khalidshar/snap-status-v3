# Codex task: the King Fahd Complex Hafs font for sacred cards (asset + coverage only)

Repository `Khalidshar/snap-status-v3`, base `main` (28009bb or later).
Branch `codex/quran-font-20261028`. Open a **draft** PR. Do not merge. Do not skip CI.

## Goal

The Quran text in `apps/snap-bot/snap_bot/data/faith/quran_subset.json` uses Uthmani marks
that the current card font may not draw. Bundle the official King Fahd Complex Hafs font
from the **same owner-approved package** and prove every character of every faith text is
covered. Claude builds the sacred card layout afterwards; do not touch rendering.

## Source

Only the official package already approved by the owner:
`https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip`,
SHA-256 `227E6B1564D980F2BD09C2C35EBFB0330AC268C79A7C247CD1AB665BC635F245`.
Use its `kfgqpc_hafs_v30-font/kfgqpc_hafs_v30.ttf` byte for byte (never subset, convert or
modify it). Refuse the zip if its SHA-256 differs.

## Files you may create

- `assets/fonts/kfgqpc/kfgqpc_hafs_v30.ttf` (exact bytes from the zip)
- `assets/fonts/kfgqpc/manifest.json`: `{"schema":1,"family":…,"file":"kfgqpc_hafs_v30.ttf",
  "sha256":…,"source_zip":…,"source_zip_sha256":…,"publication_date":"2026-09-30",
  "license_url":…,"license_note":…}` — record the Complex's published terms of use for the
  font (URL + short factual note; if you cannot find explicit terms, say so plainly in
  `license_note` and in the PR; do not invent a licence).
- `tools/fetch_quran_font.py`: downloads the zip once, checks its SHA-256, extracts only that
  ttf and writes the manifest. CI never downloads.
- `apps/snap-bot/snap_bot/quran_font.py`: `path()` and `verify()` (file exists, SHA-256
  matches the manifest, manifest SHA of the zip is the approved one). No network.
- `apps/snap-bot/tests/test_quran_font.py`:
  - the font and manifest verify; a changed byte or wrong zip SHA is rejected;
  - **coverage:** every character of every text returned by `snap_bot.faith.text(...)` for
    all `faith.KINDS` is in the font's charset (use `fc-query --format=%{charset}` exactly
    the way `snap_bot/render.py` already does; skip only if `fc-query` is absent locally —
    CI has it). Report which characters are missing if any. If some are missing, list them
    in the PR description and **do not** change any text.

## Do not touch

`assets/fonts/manifest.json` (it is pinned by hash elsewhere), `render.py`, `fact_layout.py`,
`font_admission.py`, `did_you_know.py`, `faith.py`, any data under `data/faith/`,
`owner_commands.py`, `server_preview.py`, `release.py`, `config/*`, workflows,
`scripts/snap_app_mutations.json`.

## Checks

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest apps/snap-bot/tests/test_quran_font.py apps/snap-bot/tests/test_faith.py -q
```

## Red lines

No server, no deploy, no merge, no sending, no scheduling, nothing that costs money, no
secrets. Never modify sacred text or the font file.
