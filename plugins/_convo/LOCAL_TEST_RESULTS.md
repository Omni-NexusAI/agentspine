# Local test and recovery phase — 2026-09-08

Status: **complete within the offline, plugin-local scope**. Not a release promotion, live deployment or certification of the complete Convo roadmap.

## Verified checkpoint

| Layer | Result | What was exercised |
| --- | --- | --- |
| Convo local Python | 53 passed | Mocked runtime/gateway/Studio, policy, journal, migration, settings, jobs, cancellation and recovery |
| Plugin Doctor Python | 4 passed | Broken source without execution, excluded configuration, manifest/path validation, scan limits, linked-manifest rejection |
| Browser logic, Node | 10 passed | Audio admission, session/capture ownership, interruption, retargeting, native dictation, disable/status-loss teardown |
| Reuse policy, PowerShell | 49 passed | Matching fixture accepted; altered isolation, commands, mounts, resources and offline flags rejected without Docker |
| Existing Agent Spine standard image | 40 passed | 5 real-host smoke checks + 31 Convo contracts + 4 Doctor checks |
| Existing Agent Zero v2.11 image | 40 passed | Same suite against the second installed host runtime |

Image checks overlap the local suites; these are not 80 additional unique unit tests.

The real-host checks use the installed plugin loader and API/security code. They verify:

- Three ON/OFF cycles in one process restore Kokoro, Whisper, remote-TTS and capability hooks, including originally absent attributes; cached OFF adapters cannot reactivate themselves.
- Native provider/legacy configuration fixture bytes survive each cycle unchanged.
- Independent Doctor diagnoses a broken OFF plugin, then refreshes an externally repaired fixture without importing it or enabling it. Active/unknown targets and missing or non-boolean confirmation are rejected.
- Actual Flask routing rejects unauthenticated requests and absent/invalid CSRF, then accepts valid requests for Doctor and Convo. No HTTP server is started.
- The actual Convo WebSocket handler loads and the host security gate enforces session authentication, CSRF token and cookie matching. No live Socket.IO handshake is claimed.

## Reproduction

Run from the host repository root using the existing development Python environment (with the declared sidecar dependencies) and Node:

```text
python -m unittest discover -s plugins/_convo/tests -v
python -m unittest discover -s usr/plugins/plugin_doctor/tests -v
node --test plugins/_convo/tests/audio.test.mjs
node --experimental-vm-modules --test plugins/_convo/tests/ui.test.mjs
```

In PowerShell:

```powershell
& ./plugins/_convo/tests/image-policy.test.ps1
& ./plugins/_convo/tests/in-image.ps1 -SourceContainer agentspine-standard-v9.9 -Name convo-test-standard-doctor-20260908 -Reuse
& ./plugins/_convo/tests/in-image.ps1 -SourceContainer agent-zero-v2.11 -Name convo-test-v211-doctor-20260908 -Reuse
```

Those named test containers are retained, stopped, and validated before reuse. On a different machine, omit `-Reuse` and choose a fresh test name after reviewing the runner. No implicit pull/install/cleanup occurs.

Resolved existing images:

- Standard: `sha256:612ac80162ad0e44ded343c7cfc5000414c3cc4ceef2c6e30531a7a6d7521baf`
- v2.11: `sha256:a38802ee1ddb83747836521931a99b8f6af000c586e1176478c533dcb08de8c4`

Both use their baked `/git/agent-zero` source and `/opt/venv-a0/bin/python`, not normal application startup. Source mounts are read-only; user state is empty bounded tmpfs. The root filesystem is read-only, network disabled, no GPU/devices/host ports/user volumes/Docker socket. No image rebuild, model download or original-host startup was performed.

Measured writable layers remained **32 KiB / 36 KiB**, excluding shared images, Docker metadata/logs and tmpfs/swap. Scratch limits total 400 MiB; log limit is 1 MiB; operational free-space allowance is 1 GiB. Windows C: had approximately 51.8 GiB free at closeout. These are not model-sidecar build or GPU capacity estimates.

## Scope remaining outside this phase

Live browser/microphone acceptance, real classifier judgment and echo handling, sustained speech, combined-model latency/VRAM, actual sidecar deployment and user listening acceptance remain unverified. The implementation gaps listed in README remain open. Native settings fixtures and mocked speech are not substitutes for those checks.

Observed non-failing warnings: existing FastAPI startup/shutdown deprecations in the vendored supervisor, Node's experimental VM-module warning, and an unclosed-event-loop ResourceWarning during the real-host HTTP smoke path. The latter's ownership is not yet attributed; this run does not certify live process resource hygiene. No warning was suppressed to obtain a pass.

Recovery remains **disable → independent diagnosis → narrow fix while OFF → isolated tests/refresh → explicit re-enable** in the same build. A wedged shared Python process can still require an approved same-image restart. No merge, automatic deployment or permission to interrupt production follows from this checkpoint.
