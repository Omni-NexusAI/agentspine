# Persistent restoration and backend checkpoint — 2026-10-14

> **Latest checkpoint (2026-10-15):** interface and backend-defect cycle, verified in the isolated test host only — see [UI_BACKEND_CYCLE_RESULTS_20261015.md](UI_BACKEND_CYCLE_RESULTS_20261015.md). Production was not changed. The 2026-10-14 checkpoint below is preserved as historical evidence.

Convo is restored and enabled in the retained host, with AI Link development mode
enabled and browser voice idle. Native OFF/ON, disabled-plugin Doctor refresh,
saved model/voice rendering and Studio open/return were checked in the actual
host process without a restart or rebuild. Native provider and AI Link settings
remained byte-preserved. See [current results and acceptance checklist](RECOVERY_BACKEND_RESULTS_20261014.md)
for the semantic-overlap, generic System One, experimental PCM and lifecycle
changes, test counts, six-container cleanup and explicit live-audio limitations.

This supersedes earlier installed-source location and missing-source status:
source is now in persistent `/a0/usr/plugins/_convo`, not only the application
layer. The host named `agent-zero-v2.11` currently identifies as v2.13; it was not
renamed, replaced or downgraded. Existing runtime dependencies were reused.

Configured conversation, classifier provisioning, owned Studio audio/profile
service and physical listening acceptance remain open. System One and
experimental PCM remain OFF; no model was downloaded, loaded or inferred.

# Approved Studio setup and configuration import — 2026-09-11

This supersedes the dependency/configuration status in the earlier checkpoints below. It does not establish live conversation or cloning acceptance.

- Installed pinned optional Studio dependencies first in an isolated staging environment, then in Convo's persistent environment. Five real Gradio/FastAPI integration tests passed in each, using temporary data, a stub audio backend and blocked outbound sockets; `pip check` passed. Agent Zero's framework environment was not modified.
- The live setup helper's 300-second install timeout left Gradio incomplete. Reinstalling only the pinned Gradio package without dependencies repaired it; actual import/app construction and the five integration tests then passed. The setup timeout itself remains unchanged.
- Live Convo environment occupies 290,300,868 bytes (about 277 MiB); retained staging environment 290,219,928 bytes. No weights, image rebuild, host restart, cleanup or unrelated service changes were performed.
- Copied five allowlisted connection/model/voice fields from the newer standalone saved selections through preview/select/apply. Conversation retains the remote desktop Gemma endpoint and saved model. External audio.cpp TTS uses the Docker-host translation of standalone port 8890, its Qwen model and saved clone. Remote-processing designation was explicitly preserved. No saved credentials were exposed.
- Standalone has no classifier configuration to copy; that role remains blank. Standalone tuning/native-PCM/history policies and persona/routing instructions were not imported. Original installation and profile volume were untouched.
- Both copied endpoint checks failed. The user confirmed the remote desktop model is stopped; it was not started or replaced. The separate Convo-owned Studio audio/profile backend is also not configured. A rendered Studio page is not a functioning profile/audio service.
- Replaced Gradio's CSP-incompatible HTML header with Markdown without weakening CSP. Fixed the native Studio probe to use its authenticated root route rather than the host API prefix. The actual expanded Studio interface now renders in Agent Zero.
- Added concise managed-backend error handling, preservation of unavailable saved voices in the selector and guarded selection. All 31 JavaScript tests passed, including these regressions. Versioned the Studio modal/module references after an older browser copy survived a fresh tab. Final live DOM and screenshot verification show the rendered Studio frame, concise readiness warning, saved unavailable voice option, and disabled Use in Convo action; no raw Studio trace is displayed.
- Owned gateway was stopped/started through Convo controls and returned as exactly one process. Enhanced Speech, Kokoro and Whisper configuration hashes remained unchanged. Convo settings changed only through the authorized import/save flow.
- Actual configured conversation, clone creation/preview, model load, interruption timing and user listening acceptance remain unverified. Existing release limitations below still apply unless explicitly superseded here.

# Earlier interface completion checkpoint — 2026-09-11

The updated Convo source is installed and enabled in `agent-zero-v2.11`. This is a development checkpoint, not full Studio, conversation, or listening acceptance.

## Installed and checked

- Native vertical sidebar, correct selected-chat display, balanced quick controls, four shared settings sections, explicit gateway/component readiness, and optional expanded Studio entry/return.
- Shared revision-aware saves, credential redaction, explicit discovery/connection checks, selected-field import preview/apply, and immutable response voice/tuning selection. Automated tests cover these paths; existing model settings were not changed for live tests.
- Actual browser rendering checked on desktop and at 360 pixels. The host's narrow-layout dialog had 288-pixel client and scroll widths, with no Convo horizontal overflow. Studio return preserved the selected `Testing` chat and microphone-off state.
- Real rendering exposed initially disabled discovery buttons. Explicit boolean bindings fixed this; a fresh page verified all six discovery/check buttons enabled before discovery, with a regression test added.
- Native OFF/ON stopped the owned gateway and started exactly one gateway process. No host restart, image rebuild, model download/load, or dependency installation was performed for this update.
- Convo, Enhanced Speech, Kokoro, and Whisper configuration checksums remained unchanged during deployment/activation checks. Histories and user profile storage were not deployment targets.
- Independent Plugin Doctor now resolves built-in Convo source instead of mistaking its user-data directory for source. It validated 97 source files with no issues while Convo was disabled. Its own diagnostics helper can refresh without restarting the host.
- Source rollback archive retained at `/tmp/convo-before-interface-20260911.tar.gz` (267,535 bytes). Plugin source deployment archive was 964,608 bytes. Existing isolated runtime remains approximately 33 MiB; installed source approximately 1.5 MiB.

## Automated checks

- Windows Python: 117 tests ran successfully, with 10 Flask-dependent cases skipped in that environment.
- JavaScript: 29 tests passed, including the final discovery-button regression.
- Plugin Doctor: 5 tests passed.
- Reused bounded, offline Agent Zero image fixture: 94 overlapping cases passed, including real Flask authentication/CSRF/proxy cases, recovery, and Studio admission. `IN_IMAGE_PASS`; retained stopped fixture writable layer 36,864 bytes. No model/network/user-volume access.
- These are overlapping functional checks, not combined-load, speech-latency, or listening measurements.

## Remaining approval and acceptance gates

- Optional pinned Gradio Studio dependencies require approval for installation in Convo's isolated environment. Studio currently renders an explicit unavailable message; serving the gateway is not proof of Studio audio functionality.
- The separate owned audio backend is not configured/running, and conversation/classifier model fields remain blank. A complete configured conversation, clone creation/preview/selection round trip, actual interruption timing, and listening acceptance remain unverified.
- Studio writes persist an uncertainty marker before dispatch. Transport failures retain it across plugin restart and block new conflicting work. Backend-aware terminal-operation reconciliation is not implemented; do not clear this marker merely because the interface restarted. This remains a release-readiness limitation.
- Full light-theme and reduced-motion rendered acceptance, real microphone/dictation interactions, and shared-GPU performance remain open. Source/mock tests are not substitutes.
- An automation confirmation dialog stalled one test tab during cache refresh. No dialog bypass or host/browser restart was used; a fresh test tab verified installation. The stalled tab was left untouched.

## Prior installation checkpoint — 2026-09-10

Convo is installed and enabled in the user-selected `agent-zero-v2.11` host. This is a development installation, not release or listening acceptance.

## Verified

- Installed Convo frontend/backend source and independent Plugin Doctor without an image rebuild.
- Installed declared FastAPI/httpx/uvicorn requirements in Convo's separate persistent environment. FastAPI remains absent from the framework environment: its packages were not modified.
- Native HTTP login/session and CSRF flow used for settings and ON/OFF/ON operations.
- ON reached authenticated gateway health and had exactly one owned gateway process.
- OFF removed that process; ON again reached healthy state with one different process.
- Enhanced Speech source/configuration retained, with its native toggle OFF while Convo is ON.
- Enhanced Speech, Kokoro and Whisper configuration files byte-preserved across activation testing. Migration retained its own journal.
- Main panel, JavaScript store and configuration page served successfully through native host asset routes.
- Approximately 33 MiB isolated environment and 1.2 MiB installed plugin source. No models downloaded/started; no image rebuilt or Docker resources removed. Windows free capacity after setup was approximately 23.2 GiB.

## Supporting offline checks

The managed-runtime source checkpoint passed 59 Python tests (including six new runtime tests), ten Node audio/UI tests and 40 checks in the existing isolated Agent Zero image fixture. These overlap previous checks; they are not independent performance measurements.

## Not verified

Model connectivity, microphone/speaker behavior, rendered browser interactions, combined model load, quality of turn decisions and user listening acceptance remain open. The user will configure/check model connections. Full host-restart startup and parent-death cleanup have not been exercised against the user's host; native ON/OFF/ON was tested without restarting it. The separate managed audio.cpp Studio service was not installed or started by this gateway setup.

Built-in source is installed in this container's application layer; persistent configuration and gateway environment live in its user storage. Replacing the container requires reinstalling source unless it is included in that replacement. Disabling Convo preserves data and stops only its gateway; native dictation remains available independently. Enhanced Speech can be re-enabled after Convo is OFF if its legacy enhancements are needed.
