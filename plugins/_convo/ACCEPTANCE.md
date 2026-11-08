# Convo release gate

The code checkpoint is a development preview. Passing isolated tests does not
complete the approved first-functional-release plan.

The bounded plugin-local test/recovery phase is closed in [LOCAL_TEST_RESULTS.md](LOCAL_TEST_RESULTS.md). Remaining sections are release/deployment gates, not reasons to rebuild an image for ordinary plugin-source iteration.

## Before deployment

- First run plugin-owned offline tests in existing host images using `tests/in-image.ps1`; image rebuilds and GPU reserve requirements do not apply to this bounded, model-free stage. The actual loader/toggle and independent Plugin Doctor OFF-target checks are included. Live browser/audio acceptance remains separate.

- Record both target host versions and their resolved built-in/user plugin paths.
- Preserve native Kokoro/Whisper settings, voice/blend values, browser device
  preferences, legacy plugin toggles and Convo rollback journal.
- Inventory GPU ownership and disk headroom. Build only with adequate storage
  reserve. Do not prune, stop other workloads or reuse existing voice volumes.
- Build the model-free runtime and optional audio.cpp image in an isolated
  deployment. Record image IDs, engine revision and CUDA architecture.
- Verify Convo-only private networking and fresh environment-injected tokens.
  No browser-side token, public port or automatic provider fallback is permitted.

## Host and browser acceptance

- Agent Spine and Agent Zero: authenticated/unauthenticated HTTP/WebSocket,
  reconnect, explicit stop, page closure, native sidebar and fallback panel.
- One microphone owner; short click starts Convo, hold/explicit dictation flushes
  through native dictation, release suppresses the following click. Preserve
  device selection, voice blending and native text-chat behavior.
- Toggle off/on and rollback: restore composer ownership, do not retain duplicate
  handlers, lose transcript events, reset preferences or overwrite newer config.
- Retarget during capture, classifier, model, TTS, playback and compaction.
  Old results/jobs remain labeled and bound to their original chat.
- Confirm playback starts/completions separately. Never quote an entire generated
  response as heard after an interruption partway through its first phrase.

## Behavioral acceptance corpus

Use recorded/consented examples of thinking aloud, unfinished thoughts, long
pauses, another person's conversation, TV/audio playback, name mentions versus
direct address, dictation, interrupted requests and explicit delegated tasks.
Record false responses, missed requests, clarification rate and every proposed
versus actually dispatched action. No unauthorized action dispatch is acceptable.
These examples require a real classifier, not canned policy outputs.

Test direct-operation timeouts and uncertain outcomes without duplicate dispatch.
Test busy targets, simultaneous native text activity, explicit steering, job
cancellation, completion at a conversational opening and crash recovery without
replaying uncertain work. Stop voice while a job runs and verify it continues.

## Continuity and models

- At least 200 real turns with repeated compaction, delayed transcripts, edits,
  summaries completing after new turns, utility failures and pending jobs.
  Compare full event history before/after. A capacity limit must be recoverable;
  it must not reset conversation or discard visible history.
- Verify requested input modality, actual context/media budget and cancellation
  behavior for every enabled role. Audio input alone is not duplex support.
- Verify clone snapshots remain unchanged through profile edits/model reloads,
  unsupported TTS controls are disabled, and external API semantics are truthful.
- Exercise download metadata, explicit consent, cancellation, resumption, low
  disk space, corrupt bytes and registration only after complete checksums. No
  model should load on startup or download on first speech request.

## Measurements and privacy

Measure user endpoint to first audible speech (not isolated tokens/sec), playback
interrupt latency, provider cancellation/detachment within two seconds, sustained
audio production, missed/false responses, RAM/VRAM and compaction impact under
combined load. Report p50/p95 plus hardware/configuration and run duration.

Observe network traffic in local-only mode. Verify no ambient/raw audio history,
unrelated transcript retention, credential logs or automatic remote fallback.
Exercise opt-in remote ambient processing separately with visible indication.

Only promote after these measurements, both-host compatibility and explicit user
listening acceptance. Source tests and a healthy startup endpoint are insufficient.
