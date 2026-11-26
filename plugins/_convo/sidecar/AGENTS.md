# Convo sidecars

- `web.py` provides FastAPI's decorators, dependencies and `HTTPException` on Starlette alone, so the gateway runs on Agent Zero's own Python. Voice Studio (gradio, which brings FastAPI) mounts the gateway inside a FastAPI app instead (`managed_runner.py`).
- `/answer` builds the model's history with `helpers/convo_history.py` (`history_messages`): declined and superseded turns never
  appear, every assistant message follows the user's words or the turn's in-memory audio (a model that takes audio), an exchange with
  neither is left out on both sides (never a placeholder), consecutive user messages (the current one included) are joined so roles
  alternate, and an interrupted reply says so; `max_tokens`
  comes from `answer_tokens` (64-8192, default 1024); local servers are asked not to think (`chat_template_kwargs`); a provider outside the user's machines only through `thinking_off`,
  the request fields Test connection measured that it accepts (one of `convo_tools.THINKING_OFF`, anything else is refused); reasoning is counted, never yielded, and `answer.done` reports the finish reason, reasoning length and tool calls.
- `/answer` tools (`offered_tools`): the request names the conversation's offered set (`tools`) and `tool_mode`; an explicit 'native'
  (Convo sets it from the measured record or the declared `tool_calls` override; a request without a mode still needs the
  declaration) sends `helpers/convo_tools.schemas` and `tool_choice: "auto"` on every request, follow-ups included (the
  start never changes); 'text' sends none (the system text describes them in writing). `no_tools` closes them for a reply's last passes:
  'none' keeps the list with `tool_choice: "none"`, 'text' sends no tool at all and the calls and results as text. At most
  `MAX_PROPOSALS` calls are read from one pass; the rest are left out, never the reply failed. An error that may pass carries
  `reason: "transient"` (`TRANSIENT` statuses, a cut connection); the host decides whether to ask again. `earlier_audio` (at most
  `MAX_EARLIER_PARTS` clips): what the user said before this turn's audio in the same request, labelled as one thing said in parts. An older request without `tools` keeps
  `allow_tools`/`recall`. A follow-up's tool turns (`tool_turns`: every call of the reply so far, at most `MAX_TOOL_TURNS`; or the single `tool_turn`) are
  appended after the latest message (`tool_turn_messages`: an assistant `tool_calls` message and a `tool` message natively, else
  the written call and a trailing results block; a camera or screen picture as one validated `image_url` user part with its
  label, and replayed recordings (`audio`, at most `MAX_CONTEXT_CLIPS`, only for a role with `audio_input`) as user audio parts;
  neither is ever stored). A speech request's `speed` (0.5 to 2) is passed on as `speed`. History tool uses are call + `tool` messages only when tools are sent natively,
  else a dated note. A `"tool_calls": null` delta or null fields add nothing; unparseable streamed arguments become `{}`.
- `/answer` slot and warm-up (`pin_slot`): a request's `slot` (0-255) becomes `id_slot` and `warm: true` becomes one generated token,
  only for a server on this machine or the user's network (`local_server`); never for a cloud provider. The host sends `slot` only
  after `/slots` found llama.cpp. `/slots` {role} answers {kind: llama.cpp, slots} from the server root's `/props` (`total_slots`) or
  `/slots` (a list), plus `n_ctx` (each slot's own context: `default_generation_settings.n_ctx`, else the top-level one, or the
  smallest `/slots` item's; `slot_context`), {kind: unknown, slots: 1} otherwise, {kind: remote, slots: 0} for a public address, which
  is never asked: two read-only GETs at most, short timeouts, no server text returned.
- `/answer` context overflow: a refusal (400/413/422/500/503) whose bounded body says the request does not fit the context
  (`context_overflow`, `CONTEXT_MARKERS`) is sent as `error` with `reason: context_full` and Convo's own words, never the server's text,
  so the host can retry with fewer turns.
- Speech requests (`/speech`, `speech_stream.py`) send a reply's synthesis envelope (`reply_tuning`, shape and size checked) as the
  server's `tuning` for the user's own audio.cpp server, and the assistant language when known; the managed runtime keeps its own
  frozen snapshot.
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
  late audio is never handed to another caller. Only a complete success or a
  bounded complete definite refusal releases that operation's record: any 4xx,
  or a 5xx whose body names a known terminal state (`raise_for_provider`,
  `DEFINITE_STATES`: the speech server's 502 "native-stream-not-incremental",
  generation already finished) or a complete answer Convo cannot use
  (`LOCAL_TERMINAL`). A refusal is reported (`provider_state` in the error and
  in `/health` last_operation). Timeout, transport failure, incomplete or
  unrecognised 5xx responses and worker shutdown have no confirmed end and are settled, never left blocking for good (the user's
  decision of 2026-11-24, after a record refused every later conversation): the provider is asked how the request ended where it
  named it (`X-TTS-Request-Id`, `/audio/outcomes/<id>`; the id and key stay in memory), else the record is released after `GRACE`
  (3 s when the provider ended the connection or answered an error, 20 s when Convo stopped waiting). A record of a process that is
  gone, or one that cannot be read, is released once older than `RECORD_STALE` (`expire`, at the next request or `/health`). A
  running operation is never expired, nothing is sent to the provider again, and no provider lifecycle action is taken. Until then
  the answer is 409 `uncertain` with `clears_in_ms`, which callers wait out like a busy server (`speech_status`); a starting
  conversation waits too (`Realtime._speech_settled`). `generation-limit` (the speech server's own audio limit) is a definite end.
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
  A very short phrase (`is_short`: at most 2 words and 24 characters) is
  synthesised whole (the same frozen request with `response_format: wav`,
  `stream: false`, decoded to the same PCM16 frames), and a stream refused as
  "not incremental" before any audio was published is asked once more, whole,
  inside the same admission. Whole-phrase output uses the server's buffered
  sampler (Quality's), not the reply's preset sampler.
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
