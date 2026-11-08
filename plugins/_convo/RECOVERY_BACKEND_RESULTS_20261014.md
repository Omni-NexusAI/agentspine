# Convo recovery and backend checkpoint

Dated 2026-10-14 (America/New_York). Development installation, not release or listening acceptance.

## Installed state

- Convo is restored to persistent `/a0/usr/plugins/_convo` storage in the user-selected `agent-zero-v2.11` container. The running host now identifies as Agent Zero v2.13; its original container name and current core were preserved.
- Convo is ON, Enhanced Speech is OFF, and **Advanced → AI Link development mode** is ON. Browser Convo voice intentionally stays idle; native speech/dictation compatibility remains available. This does not establish a successful Unity audio round trip.
- Native OFF stopped the owned gateway, native ON launched exactly one new gateway, and Plugin Doctor inspected/refreshed Convo while it was OFF. No host restart or image rebuild was used.
- Saved conversation model, speech model, voice and endpoints remain selected. The repaired selectors now display the saved models instead of a misleading placeholder. Saved choices are marked unverified rather than replaced.
- Enhanced Speech, Kokoro, Whisper and AI Link configuration hashes were unchanged across installation and activation checks. Convo changed only through the approved development-mode save. Source restoration excluded configuration, data and toggles.
- Voice Studio renders its adapted interface and returns to the same idle Convo panel. Its owned audio/profile backend remains unconfigured or unreachable; the UI warns explicitly and disables selecting an unavailable managed profile.

Open the host at [Agent Zero](http://localhost:5027/). In Plugins, open Convo or use its sidebar entry. Leave development mode on for AI Link work; turn it off explicitly before a browser conversation test. Leaving the mode does not automatically acquire the microphone.

## What changed

| Area | Implemented behavior | Remaining gate |
|---|---|---|
| Native compatibility | Weighted Kokoro voices, native Whisper/settings bridges, exact patch ownership and persistent-package imports | Actual AI Link/native audio interaction |
| Interruption decisions | Speech onset becomes a candidate, not an unconditional playback stop. Bounded audio classification runs alongside playback; acknowledgements can preserve the response, uncertainty ducks output, and admitted interruptions revoke the response epoch | Real acoustic corpus, false-interruption rate and combined model load |
| System One | Portable text/image/audio request contract, explicit capability limits, cancellation-safe native inference lease, D1 adapter and optional Convo consumer | Provision/register a real backend and validate the model; no System One model was installed or loaded |
| Incremental speech output | Explicit PCM16 framing, sequence/count/checksum validation, bounded browser scheduling and heard-versus-generated receipts | Opt-in remains OFF; sustained playback and listening tests |
| Recovery | Rapid OFF/ON waits for old connections to finish. Adapter reloads share one lifecycle owner. Uncertain speech/Studio operations survive disable/restart and block conflicting dispatch | Backend-aware automatic reconciliation is not implemented |
| Test workflow | Reuse one named Doctor fixture with bounded offline state; restore source while OFF and repair in the same build | No automatic cleanup of user resources or fallback image rollback |

The main conversation path still receives completed original audio directly. The classifier receives coalesced bounded WAV observations; this is **not** a true incremental-input or end-to-end duplex main-model adapter. Speculative main-model drafts are not enabled. Images are supported by System One's shared SDK independently of Convo; individual providers must declare supported modality combinations. D1 image requests require a verified tokenizer/template budget before inference.

System One's canonical and generated distribution sources were updated in `C:/a0-plugins`. Its existing text decision core and Main/Utility/Embedding routing were preserved. Convo's consumer defaults OFF and requires explicit registration of an already-provisioned backend plus the System One consumer opt-in. Failure does not switch to another classifier.

## Verification

These suites overlap; their counts are not a combined benchmark.

| Check | Result |
|---|---|
| Full local Convo Python suite | 267 run: 252 passed, 15 optional checks skipped |
| Reused Agent Zero image fixture | 180 passed; final `IN_IMAGE_PASS` |
| Browser state/audio tests | 47 passed; final selector repair also passed the 22 affected UI/setup checks |
| Canonical System One media SDK | 13 passed; generated assembly check current |
| Source hygiene | Convo checkout `git diff --check` passed; an existing trailing-space line in the separate standalone launcher checkout was preserved |
| Installed behavior | Native OFF/ON gateway lifecycle, disabled-plugin Doctor inspection/refresh, saved selection rendering, development-mode save, Studio open/return |

Image validation included actual host auth/CSRF and loader/recovery contracts, empty temporary user state, no external network/GPU/ports, and repeated enable/disable cycles. Existing asyncio resource/deprecation warnings were non-fatal. Neither mocks nor image checks prove the two-second contract against a real inference provider.

Screenshots from the installed host:

- [Convo panel](C:/speech-to-speech/.runtime/convo-panel-full-20261014.jpg)
- [Voice Studio and explicit backend limitation](C:/speech-to-speech/.runtime/convo-studio-20261014.jpg)

## Container and storage cleanup

Retained one isolated fixture: `convo-test-v211-doctor-20260908` (`492cb7652bbe`), now stopped. Its writable layer was 36,864 bytes; bounded scratch is a 400 MiB tmpfs and logs are capped at 1 MiB. No new test container, image or volume was created.

Removed exactly these six approved, stopped, redundant fixtures after retaining bounded evidence:

- `convo-test-standard-doctor-20260908`
- `convo-test-v211-recovery-20260908`
- `convo-test-standard-recovery3-20260908`
- `convo-test-standard-recovery2-20260908`
- `convo-test-standard-recovery-20260908`
- `convo-probe-standard-20260908`

Only those containers were removed, without force or volume deletion. Their container objects are not recoverable through Docker; retained logs/inventory and source remain. Images, volumes, unrelated containers and services were not deleted. Evidence is in `C:/speech-to-speech/.runtime/convo-fixture-recovery-20261014`.

Restored source was approximately 1.1 MiB. No dependencies, weights or GPU workloads were added during this pass. Last measured host free space was about 15.1 GiB. Docker/WSL compaction or restart was not needed or performed.

The existing **Overnight Docker cleanup** automation was only inspected: every other day at 4 a.m. New York time, targeting eligible disposable images/build cache. It excludes automatic container/volume/named-image deletion. Its disk compaction can restart Docker/required WSL only after idle checks and a ten-minute notice. This pass neither changed nor ran that automation.

## What to look for next

1. Check that Convo is visible and ON, Enhanced Speech stays OFF, and development mode shows browser voice idle. Native chat and explicit dictation should remain usable; physical dictation was not tested here.
2. Review the saved model/voice selectors and Studio round trip without losing configuration. A saved-but-unverified option is intentional, not a successful connection check.
3. For live voice, make the preserved conversation/TTS services available and provision a classifier (the existing standalone setup supplied none). If using System One, explicitly register its backend and opt in; transferring a model file alone is not registration.
4. Configure the separate Convo-owned Studio audio/profile backend before testing cloning or previews. The preserved external TTS endpoint does not provide that owned profile library.
5. Then exercise acknowledgements, thinking aloud, real interruptions, long/corrected requests and native OFF/ON. Record audible latency, false responses and combined memory/load. Keep experimental PCM OFF until deliberately testing a provider with the required protocol.

Full light-theme/reduced-motion/narrow-layout acceptance of this update, physical microphone/echo tests, live model quality, cloning round trips and user listening acceptance remain open. Standalone Adaptive v3 echo protection, camera context, true incremental model input, speculative replies, omni/duplex adapters and complete Studio editing/tuning parity are not claimed complete by this checkpoint.

No commit, push, merge, release, new automation, model fallback, protected service change or change to the standalone pipeline's admission/retention policy was performed.
