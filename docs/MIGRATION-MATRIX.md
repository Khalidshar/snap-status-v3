# V3 Legacy Migration Matrix

This matrix is evidence-driven. Old repositories remain untouched.

| Capability | Legacy evidence | V3 decision | Notes |
|---|---|---|---|
| Snap agents | `snap-bot/agents.py` | WRAP | Preserve behavior behind AgentAdapter; Golden tests before replacement. |
| Snap jobs/state | `job_manager.py`, `database.py`, `cache.py` | REFACTOR | Recover proven semantics; V3 state contract is authoritative. |
| Snap Telegram delivery | `snap.py` + workflow secrets `TG_TOKEN/TG_CHAT` | WRAP | Telegram remains Snap control/delivery surface. |
| Snap scheduler | `.github/workflows/snap.yml` | REFACTOR | Legacy long shift is useful evidence, but V3 scheduler remains disabled until vertical slice passes. |
| Status orchestrator | `agents/orchestrator.py` | WRAP | Protected agent behavior. |
| Translation | `agents/translation.py` | KEEP + EXTEND OUTSIDE | Existing agent detects English. Actual EN→AR translation is an external service followed by proofreading; sacred text excluded. |
| Arabic QA | `agents/arabic_core.py` | WRAP | Protected validation role. |
| Proofreading | `agents/proofreading.py` | WRAP | Run after translation for non-sacred prose. |
| Fact/source | `agents/fact_source.py` | WRAP | Keep source validation role. |
| Design director | `agents/design_director.py` | WRAP | Keep RTL/design checks. |
| Design rotation | `agents/design_rotation.py` | WRAP | Persist memory safely across Actions runs. |
| Media verification | `agents/media_verification.py` | WRAP | Feed validated local assets to renderer. |
| Voice | `agents/voice.py` | WRAP | Keep audio validation; human-sounding voice provider remains pluggable. |
| Final QA | `agents/final_qa.py` | WRAP | V3 Quality Gate remains final publication authority. |
| Content repair | `agents/content_repair.py` | WRAP/REFACTOR | Fit into time-budgeted Repair Engine and DLQ/replay. |
| Quran source | `quran_source.py` | WRAP | Independent reference verification + Sacred Lock + post-render checks. |
| Religious sources | `religious_sources.py`, verified JSON datasets | WRAP | Never AI-rewrite locked text. |
| Status renderer | `run.py`, templates, `qa.py` | REFACTOR | Recover proven render/layout behavior behind V3 Renderer contract. |
| Status scheduling | `.github/workflows/tick.yml` | REFACTOR | Preserve due-kind behavior; same V3 pipeline for manual/scheduled jobs. |
| WhatsApp delivery | legacy workflow + runtime | WRAP | Manual-first; automatic Status publishing remains isolated and disabled until readiness gate. |
| Fonts | legacy font assets/setup | KEEP after license/compatibility review | Pin render environment before Golden visual tests. |

## Legacy state lessons recovered

The old Snap workflow restores `state` and Quran cache between Actions runs and serializes long-running shifts. The old Status workflow uses cache keys for sent/idempotency-like behavior, Quran cache, memory, QA artifacts, agent artifacts, and serialized concurrency. V3 will preserve these successful operational ideas but place them behind explicit Job, Delivery, Idempotency and state contracts.

## Next recovery order

1. Agent Registry with exact inputs/outputs.
2. Sources Registry and network calls.
3. Actual design providers and token variable names.
4. Renderer/FFmpeg pipeline map.
5. Vertical Snap slice with delivery disabled until QA passes.
6. Status manual slice, then Outbox.
