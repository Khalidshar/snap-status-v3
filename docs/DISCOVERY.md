# Phase 0 — Discovery / Legacy Recovery Policy

## Source repositories (read-only during V2 build)

- `Khalidshar/snap-bot`
- `Khalidshar/status-bot`

The legacy repositories are references and known-good behavior sources. V2 must not modify their production workflows, schedulers, secrets, agents, or runtime while being built.

## Recovery rule

When a V2 capability becomes complicated or uncertain:

1. Identify the equivalent known-good legacy path.
2. Record its inputs, outputs, dependencies, network calls and failure behavior.
3. Prefer `WRAP` through an adapter when behavior is already correct.
4. Use `REFACTOR` only when the contract can be preserved and Golden Tests prove parity.
5. Use `REPLACE` only for a technical implementation that is unsafe/unmaintainable; preserve the user-visible contract.
6. Never copy secrets or production state into source control.

## Agents already observed in status-bot

- Orchestrator
- Translation
- Arabic Core
- Proofreading
- Fact & Source
- Design Director
- Design Rotation
- Media Verification
- Voice
- Final QA
- Content Repair

Their roles remain protected during migration. The current Translation agent primarily detects untranslated English; V2 needs an actual English→Arabic translation service followed by Arabic proofreading, while preserving sacred-text restrictions.

## Snap legacy observation

`snap-bot` contains an existing agent layer and mature rendering/content behavior. It is a recovery/reference source for the Snap vertical slice rather than a target for modification.

## Required next inventory

- Full Agent Registry and Golden inputs/outputs
- Commands and scheduler map
- Network/egress calls
- News/religious/media/audio sources
- Three actual design providers and their environment variable names (never values)
- FFmpeg pipelines and fonts
- Existing QA and recovery paths
- GitHub Actions state/cache/artifact behavior

## Migration labels

`KEEP | WRAP | REFACTOR | REPLACE | DO NOT MIGRATE`
