# Codex task: verified faith data for commands 13–20 (data only)

Repository `Khalidshar/snap-status-v3`, base `main`. Branch `codex/faith-data-20261028`.
Open a **draft** PR. Do not merge. Do not skip CI.

## Goal

Bundle the religious texts the old Status bot already verified, so v3 can render them
later **word for word**. This task is data, a strict validator and tests. Claude wires
rendering and the Telegram commands afterwards; do not touch those files.

Sacred text is never written, paraphrased, corrected, shortened, normalised or "improved"
by you. Every character comes from the sources below, exactly.

## Sources (read-only)

- Old bot repo `Khalidshar/status-bot` at commit `b445281` (read it from GitHub; never run
  it, never touch its server or deployment):
  - `adhkar_verified.json` (morning `am` 9 items, evening `pm` 11 items; `type` is `quran`
    or `hadith`, with `count`, `source`, `verification_url`)
  - `hadith_verified.json` (8 sahih hadith from al-Bukhari / Muslim, with `source`, `grade`)
  - `sunnah_duas.json` (duas with `source`, `grade`)
  - `content.json` keys `verses` (46 `[surah, [ayahs], scene]`) and `friday_verses`
  - `quran_source.py` (how the official text is fetched and verified)
- Quran text: the official King Fahd Complex package
  `https://download.qurancomplex.gov.sa/resources_dev/kfgqpc_hafs_v30.zip`, accepted only
  if its SHA-256 equals
  `6D4B6B2B6A290BDA1D8E3179BC6AAB7D49F6FAEDD3F2D31F7D589D5E6E99F8F4` and it has 6236 ayahs
  in 114 surahs (exactly as `quran_source.py` checks).

## Files you may create

- `tools/build_faith_data.py`: downloads the official zip once, checks the SHA-256 and
  counts, and writes `apps/snap-bot/snap_bot/data/faith/quran_subset.json` holding **only**
  the ayahs referenced by the adhkar (`type: quran`), `verses` and `friday_verses`
  (plus Surat al-Kahf 18:1–10 for the Kahf reminder). Each ayah: `surah`, `ayah`, `text`
  exactly as in the package, and `sha256` of that text. The file header records the zip
  URL, zip SHA-256, package version and build date. The script is run by you once; CI never
  downloads anything.
- `apps/snap-bot/snap_bot/data/faith/adhkar.json`, `hadith.json`, `duas.json`: copied from
  the old repo **unchanged in text**, normalised only in structure:
  `{"schema":1,"language":"ar","sacred":true,"kind":"…","source_repo":"Khalidshar/status-bot@b445281","items":[…]}`.
  Every item keeps its original `text`/`source`/`grade`/`count`/`verification_url`, plus a
  stable `id` and `sha256` of `text`. Quran-type adhkar reference ayahs by `surah`/`ayahs`
  and carry no text of their own (the text comes from `quran_subset.json`).
- `apps/snap-bot/snap_bot/faith.py`: loader + validator. No network.
  - `load(kind)` for `adhkar_am`, `adhkar_pm`, `hadith`, `dua`, `verse`, `friday`, `kahf`.
  - Validates schema, `sacred: true`, unique ids, every `sha256` matches its text, every
    referenced ayah exists in the subset with a matching `sha256`, the subset header SHA is
    the official one, hadith/dua `grade` is `صحيح` or `حسن صحيح` or `حسن` only, and no
    control/bidi characters. Raises `ValueError` on anything else.
  - `text(item)` returns the exact display text (an adhkar Quran item joins its ayahs'
    exact texts with a single space, adding nothing else).
- `apps/snap-bot/tests/test_faith.py`: every kind loads; a one-character change to any text
  breaks its `sha256` and is rejected; a missing ayah, a wrong subset header SHA, a weak
  grade, `sacred: false` and a bidi character are each rejected (bad copies in a temp dir).

## Do not touch

`owner_commands.py`, `server_preview.py`, `render.py`, `fact_layout.py`, `did_you_know.py`,
`catalogs.py`, `release.py`, `studio_core/*`, `config/*`, workflows,
`scripts/snap_app_mutations.json`.

## Checks before you push

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest apps/snap-bot/tests/test_faith.py apps/snap-bot/tests/test_catalogs.py -q
```

## Red lines

No server, no deploy, no merge, no sending, no scheduling, nothing that costs money, no
secrets. Never run, deploy or modify the old bot. Never edit sacred text.
