# Convo

Convo adds a local-first voice conversation alongside Agent Zero's text chat. It is a built-in successor to Enhanced Speech, retaining native dictation while connecting a conversational model to background agent work.

**Development status:** implementation in progress. A local Agent Zero v2.11 installation has passed gateway activation/recovery checks; full voice use is not certified. Existing model services and the standalone pipeline remain unchanged. See [local installation checks](INSTALLATION_TEST_RESULTS.md).

The plugin-local testing and same-build recovery phase is complete; see [local validation results](LOCAL_TEST_RESULTS.md). This closes the offline test scope, not the full functional-release plan below.

## Implemented source checkpoint

The October recovery pass adds semantic overlap decisions, an optional generic
System One text/image/audio consumer, native weighted-speech compatibility, and
experimental framed PCM playback. See [System One provisioning](SYSTEM_ONE.md).
These additions are tested with synthetic providers; actual model and listening
acceptance remain required.

- Capability-based configuration, authenticated host WebSocket and authenticated model sidecar. No browser-selected service URLs, provider fallback or bundled weights.
- Separate conversation/classifier/TTS roles, direct WAV input and Whisper text compatibility; buffered speech from an explicit compatible API, response-scoped native Kokoro on newer hosts, or managed audio.cpp Base cloning.
- Application-level speech/action gates, silence on uncertainty, one clarification per ambiguity episode with a 60-second cooldown, cancellation epochs and bounded input/output queues. No speculative model concurrency is enabled.
- SQLite event journal, full visible history, heard-versus-generated speech, delayed transcript reconciliation, explicit chat retargeting, and immutable-prefix background compaction through the host utility model.
- Persistent background jobs with submit/inspect/cancel/explicit-steer semantics, busy-target queueing, uncertain-outcome recovery and target-linked completion notices. Voice shutdown leaves jobs intact.
- Native composer orb, click/hold dictation, accessible dictation control, focused-app shortcut, sidebar extension and fallback panel.
- Cached voice-persona extraction at session start/retarget, constrained style overlay, bounded direct web search and already-loaded scoped memory retrieval.
- Headless audio.cpp packaging with a private authenticated control surface, persistent models/profiles and explicit load/unload. Native Studio provides clone creation, preview, tuning interchange and download inspection/approval/progress/pause/resume for two Base presets.
- Reversible, idempotent legacy JSON migration; old built-in import paths remain compatibility shims. Native voice/blending settings remain authoritative and general Convo saves do not overwrite them.

These are implemented code paths, **not a certification that they work together on either deployed host**. See [ACCEPTANCE.md](ACCEPTANCE.md) for outstanding release gates.

## Automatic gateway startup (Linux hosts)

Convo can own its gateway process inside the existing host container. This avoids a separate launch script on each use or a full image rebuild. It does not start the conversation, classifier or TTS model servers. The separately packaged audio.cpp Studio service remains a separate setup requirement.

After explicitly approving dependencies, the installer can run `python plugins/_convo/setup_runtime.py --approve-dependencies` once from the host root. It installs only the declared web-runtime requirements into `usr/plugins/_convo/data/runtime-env`, not the framework Python environment. No weights are downloaded. Then enable **Automatically run Convo's local gateway** in settings. External deployments retain their existing behavior by default.

Native plugin ON and host startup launch the configured gateway. OFF, update and uninstall stop only the owned process; parent-process death also stops it. Credentials are ephemeral and stay server-side; the inherited listener is loopback-only. The sidebar reports gateway state. Missing dependencies or failed health checks block requests rather than selecting another service. Histories, configuration and delegated jobs survive shutdown.

## External gateway development setup

1. Review the plugin and legacy shims together using an existing compatible host image and plugin-only bind mounts. Ordinary plugin source tests do not require a host rebuild. Do not overlay the shims without `_convo`. Keep existing host/plugin config and toggle backups. Use the migration preview before applying; rollback refuses to overwrite newer edits.
2. Select the existing host Docker network through `CONVO_HOST_NETWORK`. Configure a fresh `CONVO_SIDECAR_TOKEN` in both the host and Convo runtime environments. Credentials are environment references, not browser settings values.
3. Review `sidecar/compose.yaml` before any deployment. Its default service is the model adapter on private port 8090; the separate `managed-audio` profile adds Convo-owned audio.cpp with fresh `convo-models` and `convo-voices` volumes. That profile requires a separate fresh `CONVO_AUDIO_TOKEN`, shared by runtime and audio service. No existing speech volume or service is mounted or managed. Starting the supervisor does not load models.
4. For managed audio, set the appropriate CUDA architecture at build time. Use native Studio to inspect the source/revision/license/size, approve a download, explicitly load a model, and create a clone profile from an authorized reference WAV plus exact transcript. Downloads verify every file before directory registration; cancellation preserves partials and failed checksums are quarantined. Storage reserve is enforced. No automatic re-download or overwrite of an installed model.
5. Configure explicit conversation and classifier URLs/model IDs. Audio input does **not** imply incremental input or duplex. Prefer an audio-capable classifier for local ambient mode; text-only compatibility is available in active mode through native Whisper. Whisper itself must be prepared through the host's controls. Remote ambient processing requires its own opt-in.
6. Choose `managed_audio` and the exact loaded model/clone IDs, or an explicit external speech endpoint/voice, or supported local native Kokoro. Preview native Kokoro voices first so both pipeline and embeddings are already cached; Convo refuses to load/download them implicitly. Managed Base capabilities are fixed to the adapter contract; unsupported instruction/voice-design controls are not inferred from family names. The [Qwen variant table](https://github.com/QwenLM/Qwen3-TTS#released-models-description-and-download) distinguishes Base cloning from other variants.
7. Enable the plugin and its composer control, then select a chat before starting. The microphone is acquired only after the authenticated sidecar handshake and persona setup. Click toggles Convo; holding for 450 ms enters native dictation. Changing the selected text chat alone does not silently retarget voice—use **Use selected chat**.

Model selection applies at the next session. Speech configuration is frozen per response; managed audio additionally freezes clone material, content revision/hash, engine epoch and supervisor instance. Native Kokoro freezes its response config. Generic external APIs must provide their own stable voice-selection semantics; mutable external clone-profile freezing is not yet certified.

Runtime data lives under `usr/plugins/_convo/data`; model/profile data lives in the separate managed volumes. Disabling Convo or stopping voice does not delete either or cancel background work. Existing source checkouts, FasterQwen, Groxaxo and external model servers remain untouched.

## Isolated tests and same-build recovery

Run `tests/in-image.ps1` from PowerShell with `-SourceContainer agentspine-standard-v9.9` or `-SourceContainer agent-zero-v2.11`. The existing host is only inspected, never started or modified. The runner uses its already-installed image with read-only mounts for Convo, legacy shims and the independent developer **Plugin Doctor**. User data is empty temporary storage. No dependency install, model download, GPU, external network, host port or Docker socket is used. Sidecar/FastAPI tests remain in the separate local test environment.

Scratch is capped at **400 MiB** (tmpfs; may use swap), logs at **1 MiB**. September 8 image checks used **32–36 KiB** of writable layer each, excluding Docker metadata/logs and shared image layers. Keep **1 GiB** free for this bounded path; this is an operational allowance, not a CUDA/model build estimate. The default is the existing Doctor fixture, automatically reused after identity/isolation validation. Do not make a new container per probe. A different fixture requires a distinct isolation need. No cleanup is automatic.

### AI Link development mode

Keep Convo ON and Enhanced Speech OFF. In **Advanced**, enable **AI Link
development mode** after ending browser voice. This keeps native speech hooks and
weighted Kokoro/Whisper settings available while browser Convo stays idle; AI Link
uses its unchanged protocol, endpoints and native audio path. Leaving this mode
does not resume microphone capture. Native dictation remains an explicit control.

### Experimental incremental speech

**Voice & Audio → Experimental incremental PCM playback** is opt-in, not the
stable default. (With the Qwen3 audio.cpp voice it is the fastest path measured: first audio
bytes about 2.1 s after the request against 2.8-4.6 s for a buffered WAV; the owner of this
installation runs it with that voice.) The provider must explicitly describe PCM16 mono audio headers.
The gateway independently drains and tracks each request; the host validates its
framed sequence, byte count and checksum. The browser uses the existing audio
context, and only a complete played phrase is acknowledged. No WAV/headerless
fallback or inference capability is guessed from a model name. Combined-load,
sustained playback and listening acceptance remain open.

An uncertain speech or Studio request remains recorded across OFF/restart.
Do not clear its admission record based on a healthy gateway alone. Independently
verify provider idleness before explicit operator reconciliation; no automatic
reset is available.

Reuse refuses altered commands, writable/unexpected mounts, device/GPU access, exposed ports, weakened isolation, missing resource/log limits, unbounded scratch or missing offline flags. Run `tests/image-policy.test.ps1` in PowerShell to check that guard without Docker.

Disable Convo through the native plugin manager to invalidate voice sessions, cancel/detach voice and compaction, restore owned Python hooks, and pause new job dispatch. Existing authorized host jobs continue and remain tracked; histories/settings/profiles stay intact. Convo's disable/status-loss handlers release microphone/playback, and the native frontend refresh removes UI extensions; reload the page if old frontend modules remain cached. Use Plugin Doctor while OFF, apply the fix, refresh and rerun tests, then explicitly re-enable. A stale singleton or wedged shared host process may still require an approved **same-image process restart**. Disabling is not an undo for external actions.

## Verification

From the host repository root:

```sh
python -m unittest discover -s plugins/_convo/tests -v
python -m unittest discover -s usr/plugins/plugin_doctor/tests -v
node --test plugins/_convo/tests/audio.test.mjs
node --experimental-vm-modules --test plugins/_convo/tests/ui.test.mjs
```

The offline suites cover a 200-turn journal/compaction simulation, retarget races, cancellation/detachment, download checksums, native-setting preservation, headless gateway auth and disable/re-enable recovery. The image runner additionally exercises actual HTTP auth/CSRF routing, WebSocket security admission, host loader/toggle functions and Plugin Doctor against a deliberately broken OFF fixture, without importing that fixture. These checks do not measure first-audible latency, acoustic judgment or GPU capacity. The reused supervisor emits FastAPI lifecycle deprecation warnings; its wrapper owns startup/shutdown explicitly. See the dated results for other observed warnings.

## Still incomplete / not release-ready

- Full deployed host integrations, sidecar image builds, real browser/microphone cutover, updater compatibility and live recovery rehearsal. Offline same-process loader/disable recovery has passed in both installed host images; this is not live microphone acceptance.
- Adaptive coupled-echo protection from standalone, measured semantic endpointing, classifier selection/quality corpus, classifier/main-model combined scheduling and performance instrumentation. Current capture uses browser AEC plus a bounded energy VAD; current inference classifies before answering and does not run speculative drafts.
- Native camera-context adapter, mid-session persona invalidation, optional compaction-model override, true incremental model input, and duplex/omni adapters remain incomplete. Experimental native PCM output and the optional completed-observation System One media adapter are implemented, not live-certified.
- Native Studio profile editing/deletion and full form-based tuning parity, conversation/classifier download presets, and live metadata/download verification. The current download catalog covers audio.cpp Base TTS only.
- Combined-load benchmarks, sustained playback, ambient privacy/network observation and user listening acceptance. Ambient mode must not be treated as unattended-ready.

Implementation is developed on a dedicated feature branch against Agent Spine development. The user-approved development installation is restored in persistent storage on the retained Agent Zero host; see [the October checkpoint](RECOVERY_BACKEND_RESULTS_20261014.md). This is not release acceptance or a merge/publish authorization. No model download/load or protected external-service lifecycle change is implied.
