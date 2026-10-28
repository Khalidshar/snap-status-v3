# Codex task: Snap variety catalogs (commands 8–12)

Repository: `Khalidshar/snap-status-v3`, base `main` (b9ec8ae or later).
Work branch: `codex/variety-catalogs-20261028`. Open a **draft** PR. Do not merge it.

## Goal

Five reviewed Arabic catalogs, one per Snap variety command, in the same shape and with the
same guarantees as `apps/snap-bot/snap_bot/data/did_you_know.ar.json` and
`apps/snap-bot/snap_bot/did_you_know.py`. This task is **data, validation and tests only**.
Claude wires the catalogs into rendering and the Telegram listener afterwards; do not touch
those files.

| command | key | catalog file | items |
|---|---|---|---|
| 8 📱 التقنية | `tech` | `data/tech.ar.json` | 30 |
| 9 👗 الأناقة والجمال | `style` | `data/style.ar.json` | 30 |
| 10 🧠 لغز اليوم | `riddle` | `data/riddle.ar.json` | 30 (question + answer) |
| 11 😂 مواقف طريفة | `funny` | `data/funny.ar.json` | 30 |
| 12 🌙 قبل النوم | `night` | `data/night.ar.json` | 30 |

## Files you may create or change

- `apps/snap-bot/snap_bot/data/{tech,style,riddle,funny,night}.ar.json` (new)
- `apps/snap-bot/snap_bot/catalogs.py` (new): loader + validator, no network, no I/O except
  reading the bundled JSON
- `apps/snap-bot/tests/test_catalogs.py` (new)

Nothing else. In particular **do not edit**: `owner_commands.py`, `server_preview.py`,
`render.py`, `did_you_know.py`, `release.py`, `studio_core/*`, `config/*`, any workflow,
`scripts/snap_app_mutations.json`.

## Catalog shape

```json
{"schema": 1, "language": "ar", "sacred": false, "kind": "tech",
 "items": [{"id": "tech-01", "text": "…", "source": "…", "source_url": "https://…",
            "verified_at": "2026-10-28"}]}
```

- `riddle` items have `question` and `answer` instead of `text`, and no `source`/`source_url`
  (original riddles); add `"source": "original"`.
- `funny` and `night` are original light text: `"source": "original"`, no `source_url`.
- `tech` and `style` are factual: every item needs a real, specific, reachable
  `source_url` on an allowlisted host (see `HOSTS` in `did_you_know.py` for the pattern; add
  a separate allowlist in `catalogs.py`, e.g. official vendor docs, standards bodies,
  government/health sites). No blogs, no social media.
- ids `<kind>-NN`, unique; texts unique across the catalog; each text ≤ 120 characters
  (riddle question ≤ 120, answer ≤ 60); no leading/trailing spaces; no control or bidi
  characters (reuse the same rules as `safe_text`).

## Content rules (strict)

- Modern Standard Arabic, clear, family-friendly, suitable for a Saudi audience of all ages.
- **Nothing religious, political, sectarian, medical-advice, financial-advice or about real
  named private people.** No mockery of any group, region, dialect, gender or disability.
- `style`: general grooming/fashion tips only; no products, brands, prices or body shaming.
- `funny`: gentle everyday situations; no insults, no adult content.
- `night`: calm reflective lines (not religious texts, not verses, not hadith).
- Facts in `tech`/`style` must be true as of the source; prefer stable facts.
- Spelling and punctuation must pass the existing proofreading rules in
  `content_agents.proofread` (no double spaces, no space before punctuation, no repeated
  punctuation, no tatweel, balanced quotes/brackets) and `translation_requirement` (Latin only
  for approved abbreviations in `arabic_agent.ABBREVIATIONS`).

## `catalogs.py`

- `KINDS = ("tech", "style", "riddle", "funny", "night")`
- `load(kind: str) -> list[dict]`: reads `data/<kind>.ar.json`, validates the whole file
  (schema/language/sacred false/kind/ids/uniqueness/lengths/hosts/dates not in the future)
  and raises `ValueError` on any problem.
- `body(item, kind) -> str`: the text that will be rendered (riddle: question and answer on
  two lines).
- Run every rendered body through `content_agents.proofread` and
  `content_agents.translation_requirement` inside `load`, so a bad line never loads.

## Tests (`test_catalogs.py`)

- every catalog loads and has 30 items;
- duplicate id, duplicate text, too long, unsafe character, wrong host, future date, missing
  key, `sacred: true` and a proofreading violation are each rejected (build bad copies in a
  temp dir);
- no item in any catalog contains words from a small banned list you define (religious terms
  such as «قال الله», «رسول الله», «آية», «حديث»; political terms) — a guard, not a filter.

## Checks before you push

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest apps/snap-bot/tests/test_catalogs.py -q
```

## Red lines (same as the owner's)

No server access, no deploy, no merge, no sending anything, no scheduling, nothing that
costs money, no secrets in code or logs, never touch V1 repos or `/opt/studio-v2`.
