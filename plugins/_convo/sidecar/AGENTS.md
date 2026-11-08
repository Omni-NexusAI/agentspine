# Convo sidecars

- `app.py` is the authenticated model-adapter boundary. Role endpoints come only
  from server settings; no redirects or provider fallback for inference. Role
  calls (`app.py`, `speech_stream.py`) use `helpers/convo_net.client`: loopback
  reaches this computer from inside Docker, and remembered private certificates
  are accepted.
- `studio.py` has a fixed private managed-audio destination and an operation
  allowlist. Sidecar/audio tokens remain in server environments.
- Studio pre-dispatch validation has a distinct `StudioRejected` type. Read a
  complete bounded upstream rejection before acknowledging it; transport,
  server, malformed or oversized response failures remain 5xx uncertainty so
  the host's durable admission lease cannot be cleared by error translation.
- `audiocpp/` vendors the reconciled supervisor, profile library and pinned engine
  patch. Keep NOTICE/license material with it. Convo adaptations belong in its
  headless gateway and download manager, not the standalone source checkout.
- Gateway must not expose the standalone chat proxy or GPU-guard bypass routes.
  No engine process or model download starts on module import or app startup.
- Model/profile volumes are Convo-owned and external to images. Never mount an
  existing user's speech profile/model volume as part of automatic setup.
- Downloads are explicit, public-source, revision-pinned, checksum-verified and
  cancellable/resumable. Do not transmit service credentials to download hosts.
- Buffered WAV is the preview/default output contract. Native PCM is an explicit
  experimental capability, default off; no performance/duplex acceptance claim
  before combined-load/listening tests. Speculative and duplex remain disabled.
- `speech_admission.py` owns one external speech operation across callers and
  processes through an exclusive record under the explicit persistent
  `CONVO_DATA_DIR`. Import, health and settings reads never create storage.
  Disconnect/cancellation detaches the caller promptly, but an independent
  worker drains the original response under a 35-second absolute deadline;
  late audio is never handed to another caller. Only a complete success or
  bounded complete 4xx rejection releases that operation's record. Timeout,
  transport failure, incomplete/error responses and worker shutdown remain
  blocked across OFF/restart. No automatic reset or provider lifecycle action:
  reconciliation requires explicit independent evidence of provider idleness.
- Buffered `/speech`, `/preview` and experimental `/speech-stream` share durable
  admission. PCM has an independent producer and an eight-frame delivery queue;
  consumer detach discards/wakes that queue while the producer drains to terminal
  EOF. Cancelling/closing a transport is never terminal proof. `/health` exposes
  sanitized admission state and timings separately from service readiness.
- `/speech-stream` requires authenticated, explicitly selected native-PCM audio
  capability and verified upstream PCM16 mono headers. It returns
  `application/x-ndjson`, `X-Convo-Speech-Protocol: pcm16-ndjson-v1`: one
  `speech.start`, zero-based `speech.chunk` base64 frames, then `speech.done`
  with byte/chunk counts and SHA-256 only after validated EOF and admission
  release. Every frame carries the same operation ID. A sanitized `speech.error`
  may occur before start or after partial audio, and never implies success.
  Host readers must reject missing/invalid completion and post-completion data;
  a clean HTTP EOF alone cannot confirm a complete utterance. No raw-PCM fallback.
- Imports/tests may not start a subprocess, GPU workload, service or network
  transfer. Stub every external/storage boundary in unit tests.
- `gradio_studio.py` is the explicitly approved temporary adaptation of the
  original Voice Studio. It mounts only in the owned gateway, under the host's
  authenticated `/convo-studio/` bridge. No public share, browser credentials,
  arbitrary endpoints, independent settings store or GPU guard bypass.
- `studio_adapter.py` uses fixed `studio.control` operations for profile/tuning
  writes. Original ZIP interchange is bounded and never extracts paths; imports
  allocate fresh identities. Only disposable upload/output artifacts use local
  disk. Gradio dependency installation remains separately approval-gated.
- Every Studio callback is nonqueued and guarded by host admission. Preview is
  explicit complete WAV with no autoplay; close/OFF removes the embedded page.
- Use Markdown/plain Gradio components for static presentation, not `gr.HTML`:
  its template evaluation conflicts with the host's no-unsafe-eval CSP. Never
  relax the host CSP to accommodate an embedded presentation component.
- Preview model and tuning-profile identity come from one host-injected shared
  settings snapshot; freeze resolves the clone, full tuning and seed once. The
  tuning reference limit is 1–30 whole seconds, separate from the bounded
  reference WAV upload duration. Complete WAV retains the fixed Quality sampler.
- `managed_runner.py` is the explicit Linux process entrypoint, bound to the host parent's lifetime. The gateway environment is separate from host packages; no models start here. Startup/stop health tests require explicit integration-test authorization.
