# Convo

## Contract

- Built-in, plugin-owned successor to Enhanced Speech. Do not modify Agent Zero core.
- Existing speech services, standalone worktrees, user profiles and settings are not deployment or lifecycle targets.
- Default to local endpoints, explicit activation and no ambient retention. Never fall back to another provider.
- Behaviour comes from meaning, never from particular words: no code path matches a phrase, word list or one test's sentence.
  Kinds of speech (acknowledgements, requests not to respond, unfinished thoughts, noise, follow-ups) are described as categories to
  the classifier and the conversation model (examples in a description only illustrate), and rules state general principles (what
  an answer may rest on), never a fix for one question. Live checks use varied phrasings, voices and topics kept apart from anything
  tuned on, scored by category; the user's own test sentences are never the whole check.
- Voice agent modes (Tuned harness, Native duplex, Hybrid, Auto) are a design only, in `VOICE_AGENT_MODES.md`; today's runtime is the Hybrid foundation. Do not describe the other modes as available.
- `helpers/convo_system_one.py` is the optional generic decision consumer;
  `convo.system_one` is default-off and names an explicitly registered backend.
  Host-runtime-owned registrations survive exact namespace refresh without
  loading/unloading models or altering Main/Utility/Embedding routes. The SDK's
  effective `consumers.convo.enabled` gate remains independent. Readiness proves
  registration only; absent backend or revoked scope is unavailable, never a
  fallback. Extract WAV PCM exactly without transcoding and keep direct main
  audio unchanged. See `SYSTEM_ONE.md` for the provisioning and callable contract.
- Gate audible output and actions separately. Provider text, transcripts and summaries are data, not authorization.
- Use session/turn epochs for cancellation and explicit chat targeting. Voice shutdown must not cancel delegated jobs.
- Browser speech detection creates candidates, not interruptions. One bounded
  classifier operation and one coalesced latest WAV observation are separate from
  the active response. Only semantic admission revokes its epoch, with one timing exception: during overlap (the user's own
  voice confirmed over the assistant's playback) the safety yield admits after `barge_in_seconds` of confirmed voice whatever
  the classifier says (twice that after a confident "go on"); final capture alone becomes a durable user turn. Reject stale sequence/epoch results, except
  the bounded same-candidate prior-epoch transition while admission is in flight.
  Confident unrelated speech preserves the reply; uncertainty/provider failure
  ducks playback without granting action authority. Final clarification still
  passes the episode/cooldown gate. Explicit Stop releases capture immediately.
- A completed turn is never lost to one slow or failed classifier call: it is
  asked once more by the same classifier, only after the first call has released
  it and within a bound, with no fallback; a probe is never retried. A native
  backend that is still inside a call it could not cancel reports a typed
  `PolicyBusy`: a completed turn waits for it, briefly, and a probe does not. Backends are told Convo's deadline
  (`convo_system_one.CALL_SECONDS` = `CLASSIFIER_DEADLINE`, kept equal by a test) and end such a call by then, so a call that hangs
  costs one decision, not the classifier for the turns after it (the decision plugin bounds its HTTP call by it). In a conversation the user started, a completed
  turn the classifier still could not judge (busy for the whole wait, too slow or failing after its retry, or refusing the input)
  is answered without a reading (`UNJUDGED`: presumed for the assistant, no explicit request, so nothing the classifier alone may
  grant follows; `candidate.state` reason `unjudged`, mark `policy.unjudged`): the conversation model decides, `[SILENT]` and
  `[WAIT]` stay open to it, and the user is told once (`notice kind: info`) until the classifier answers again. Ambient and name
  listening hand such a turn back (`degraded`), as before; so does a probe. Measured 2026-11-17: one stalled call lost a question. A reply whose
  first speech request meets a provider still draining a detached synthesis waits
  for it (typed `SpeechBusy`, polled, bounded) instead of failing; any other
  speech refusal stays an error. The conversation model's own audio capability
  alone decides whether the browser sends audio or transcripts: a classifier that
  cannot take audio is refused at start and flagged in readiness
  (`input_modality_conflict`), never silently turning an audio conversation into text.
- Speech is gated by the classifier's probabilities, not by its single most likely class. A typed
  classifier (a System One decision backend) returns a probability for every attention class
  (`convo_policy.ATTENTION`) and the policy envelope carries the class, all five probabilities and
  the explicit-request and name probabilities. `PolicyGate` answers a completed utterance when
  P(addressed)+P(unclear) reaches `policy_gate.respond_min` (active listening) or
  `ambient_respond_min`; speech still going on stops the assistant only when
  P(addressed)xP(explicit request) reaches `interrupt_min`, and is waved away only at
  `ignore_min` (1.0 = never: the default, because the model it was measured on cannot do it
  reliably). Only the typed consumer can switch this on (`Decision.parse(..., typed=True)`); a JSON
  classifier's claims about itself are ignored. A classifier that returns only a confidence is
  reported in readiness as an explicit `unsupported` speech gate and keeps the fixed thresholds
  (0.85 answer, 0.9 tools); nothing falls back to another classifier. Outside a conversation the user started, work with side
  effects keeps its own rule, an explicit request at 0.9 or more, whatever the speech gate decides (see the conversation rule
  below for active listening). The defaults
  are measured (`tests/calibrate_gate.py`) for
  one decision model on synthetic speech: re-measure them for another model or for real voices,
  never borrow another system's constant. The constants belong to one wording of the attention
  question as well: they were refitted when its "speech to another person" cue stopped calling that
  speech "naming someone" (requests that begin with the assistant's name were the worst-read
  class), and `tests/test_system_one_consumer.py` pins the wording so a change forces a re-measure.
  A clarifying question is rare and rationed: only when
  P(unclear) reaches `clarify_min` and acknowledgement + talk to others + unfinished thought stay
  under `clarify_elsewhere_max`, once per ambiguity episode, then `clarification_cooldown` (60 s,
  never lower). With d1-omni-600M it is effectively dormant (the model almost never says unclear).
- A reply is spoken and shown only after `helpers/convo_reply_guard.py` has seen it: a model that writes a tool call as plain
  text (`<tool_call>`, Gemma 4's `<|tool_call>`, `<function=...>`, `[TOOL_CALLS]`...) has the markup dropped and kept (bounded,
  `captured`); `helpers/convo_tools.parse_text_call` reads the known formats (Gemma 4, Qwen/Hermes, `<function=…>`, bare JSON)
  back into a structured proposal that meets exactly the checks a structured one meets. An unreadable or disallowed one is
  reported and nothing runs. An empty answer is asked again from the same model (`EMPTY_NUDGE`, then without tools), then reported with
  its reason (thinking used the answer budget, only a tool call, nothing). The guard is a text filter only.
- Waiting for the model (`_noticing`): after `SLOW_REPLY` seconds without anything of an answer the panel says which model is waited
  for, after a look-up also for what, and for how long (every `SLOW_AGAIN` seconds; a server on another machine is called slow after
  `VERY_SLOW`); a model its own server reports as loading, or a server that does not answer, is said instead (`_stalled`, marks
  `stall.*`). Mark `answer.waited` {seconds}. A conversation model on another machine that sent nothing for `HUNG_REPLY` seconds is
  asked once more, the first request ended first (`_patient`, mark `answer.asked_again`); a local one may be loading and is not.
- Conversation rule (active listening, `convo_policy.conversation_verdict`): Convo has no turn-taking model fine-tuned for this,
  so a finished turn in a conversation the user started is presumed meant for the assistant and answered, unless the classifier
  is confident it was not: a short acknowledgement (at most 1.2 s of speech, judged by a second reading WITHOUT the assistant's
  last words, P(addressed)+P(unclear) under .25), or talk to someone else (P(side) .9 or more) that no check during the speech
  contradicted (peak respond evidence under .5). Talk to someone else is compared with the conversation before it is left
  unanswered (`_weigh`, `convo_voice_tools.belongs`): one request to the conversation model with the summary, the last turns (the
  newest request's audio while its words are not known) and the utterance, one word back, no tool, bounded by `BELONGS_SECONDS`. It
  runs beside the candidate loop (`weigh_task`), so nothing waits for it; the page is sent a final `listen` (no duck, the utterance
  stays open) and the utterance is then decided again with the same classifier reading. `YES`: answered (`reason` `belongs`),
  taking over an answer nothing was heard of, after one already being heard; when the user has gone on speaking it is held to be
  joined (`cont.hold`). `OTHER` (it could be for the assistant and the model cannot tell): Convo ASKS, once in a while, what it understood
  and whether it was meant for it (`reason` `unsure`, `UNSURE`; the model may still write [SILENT]; no tool can run). Only within
  `UNSURE_SECONDS` of the utterance, with no answer being prepared or heard and no question open, and rationed by `PolicyGate.may_ask`
  (one per episode, which ends with a plain request, and the cooldown). What the user says next (`_answers_unsure`: the question was
  heard, nothing answered since, within `UNSURE_ANSWER_SECONDS`) is its answer: a short "yes" is not an acknowledgement then (`reason`
  `clarified`), and the ASKED check hears what the question was about together with it, so the work is not asked about twice. The
  classifier's explicit reading is NOT the trigger: measured 2026-11-25 (`.runtime/convo-live-test/unclear_sweep_20261125.py`, 312 clips
  in two synthetic voices), of what it took for talk to someone else it read .9 or more on no request to the assistant and on 2 of 23
  requests to another person. Anything else: ignored as before. Never for an acknowledgement, outside active listening, before anything was
  said, or, for a conversation model on another machine, when `speculative_reply` is off (its audio then would not have gone there).
  Only the newest fragment is compared (joined to the request before it, it would always belong to it).
  It grants no action. The decision model cannot do this: measured 2026-11-24 on 48 labelled remarks in two synthetic voices
  (`.runtime/convo-live-test/context_sweep_20261124.py`, d1-omni-600M), told the conversation it read side talk and television as
  part of it as surely as the user's additions (AUC .39-.68 with the conversation in six wordings, .64-.76 from the exact words as text).
  The panel is told why (`candidate.state` `reason`). Measured 2026-11-08 on the
  corpus (`.runtime/convo-live-test/conversation_rule_20261108.py`): told what the assistant had just said, d1-omni-600M reads
  nearly anything after it as an acknowledgement, so the reading with that line only nominates and the plain one decides; the rule
  answered 96-100% of requests, short questions and statements and 8% of acknowledgements, where the old gate under the same
  context answered 0-52% of them. It also answers television and other speakers (one-on-one by design; shared rooms use ambient
  listening and its floors). An `unfinished` thought is answered too, after `UNFINISHED_WAIT` + `PROBE_INTERVAL` and never while
  a following utterance is still open; what follows first joins it, and a joined whole judged not for the assistant withdraws the
  waiting answer (`_withdraw`). The classifier's attention wording stays the measured one (a wording that counted statements as
  addressed was measured worse and reverted).
- A finished turn answered only by that presumption (`convo_policy.reads_as_request` false: the gate did not pass it on its own, no
  explicit work, no check during the speech at .5 or more; an acknowledgement, a remark, a misread request or an unjudged turn) never
  cancels an answer the user has not heard yet (`_unheard`: a reply being prepared or generated, or phrases still playing; not an
  unfinished thought still waiting for its continuation, which what follows takes over as before). It is answered after that answer
  (`_after_reply`, same epoch, `candidate.state` `after_reply`, mark `turn.after_reply`), once the page has acknowledged its phrases
  or, at most, once their audio would have ended (played on from when each was sent) plus `AFTER_REPLY_MARGIN`, never later than
  `AFTER_REPLY_WAIT`. A request, a confident command and a continuation (merge) still take over. The waiting turn holds the earlier
  reply: cancelling it (a barge-in, Stop) cancels both, within the usual bound. Waiting costs a misread request a delay, never the
  request. Measured live 2026-11-17: "Perfect, thank you" (.001 for the assistant) cancelled a reply as it began.
- The conversation model may still decline a turn the rule admitted (`helpers/convo_control.py`, offered only in a conversation
  the user started, never in a tool's follow-up): a reply that begins `[SILENT]` (asked not to respond, no words meant for it: noise,
  someone else, media, its own voice) or `[WAIT]` (a plainly unfinished thought), optionally with a short reason, is neither shown,
  spoken nor stored as a reply; a reply that is only one bracketed or parenthesised meta statement counts as silent; a leading
  square-bracketed stage direction is dropped. The turn keeps the user's words; a silent one leaves the model's history,
  a `[WAIT]` one stays in it (the user's words, no reply; `convo_history.hidden`), so a thought finished after the continuation window
  still reads as one (measured live 2026-11-24: a question said in two halves, the second 7 s later, was lost and the model denied
  having heard it) (`content.held`, journal `turn.held {decision, reason}`, live `notice kind: held` for the developer view), and a `[WAIT]` turn
  stays open: what the user says next within `continuation_seconds` joins it through the continuation merge and is answered as one;
  with nothing more for `WAIT_AGAIN` seconds (3.5) the
  model is asked once more, told that the user went quiet: it answers what makes sense as it is (it had waited on complete questions),
  or writes `[WAIT]` again and nothing is said. A turn that took over earlier fragments but whose own words were not captured shows theirs
  (`_history`). A reply to words that are, almost to the word, a phrase the assistant spoke within `OWN_VOICE_SECONDS`, or a run
  of four or more words from inside one, is dropped as silent (`_own_voice`, mark `turn.own_voice`): its own voice from a
  loudspeaker or a loud headset (live 2026-11-25). A `[SILENT]` on a turn whose words the model itself reported is asked about once
  more when the classifier is sure of the address (respond evidence >= .9) or sure a request was made in the user's own audio
  (`policy.verified`, the explicit reading on its own; mark `model.silent_confirm` with `why`); the second answer stands, so a
  request not to respond stays silent. The same reading ends an ambiguity episode (a typed decision's `confidence` is capped by
  "was it said to me", so it must not be used for either). `INSTRUCTION` says the user's talk about the assistant, or aloud to
  themself, is still said to it.
- The assistant's last heard words stand last in the late part of every request (`_last_said`, the same text in `_compose` and
  `_turn`, no clock in it): a short reply is read against them (live 2026-11-25: "Do it." after an offer was answered about a
  running task, whose status stood nearer the audio).
- Each piece of speech carries the browser echo guard's latest verdict (`guard`: state names and numbers, never audio), kept as
  `guard_*` fields of `candidate.received`. Observational: nothing is decided from it.
- A task handed to a chat's agent carries the voice conversation's lines only, never that chat's own messages (`_relevant`
  `chat_messages=False`).
- Speech over the assistant's reply (`convo_policy.overlap_disposition`; the browser sends `overlap`, `voiced_s` and, at the onset,
  `heard {phrase_id, played_s}`): read without the assistant's last words; a request, question or stop yields (`candidate.admitted`
  with `fade_ms` 100), a confident acknowledgement or side talk lets it go on (`continue`), anything else keeps it ducked
  (`listen`); the safety yield ends it by time alone, decided before a busy classifier is waited for (a probe arriving while a
  timed-out classifier call still runs waits at most `CLASSIFIER_DEADLINE` for it, and a newer probe replaces it), counting only the
  browser's voiced seconds (periodic speech above the speech threshold, not loudness alone), after `barge_in_seconds` or the
  browser's own per-device `barge_in_s` (0.4-2.0). A yielded reply's turn keeps what the user heard (completed phrases plus the
  words of the playing phrase estimated from its playback position, Speakrail's played-words estimate) and is marked interrupted
  (`Store.interrupted`, journal `turn.interrupted`); the next answer's system text says where it was cut. Overlap thresholds are
  starting points from finished clips, not yet measured on real overlapped speech; the undo window is not built.
- Conversation memory: an audio turn asks the conversation model to begin with one `USER:` line (`helpers/convo_user_line.py`,
  the standalone fork's pattern), taken off the stream before anything is shown or spoken and stored as the turn's words
  (`Store.transcript(source="model")`, kept in `model_line`; the instruction asks for the words exactly as heard, and
  `[SILENT]` when there were none). The model's line is the record; Whisper's text is kept in `whisper` and becomes the record only
  when the model reported nothing or the two disagree strongly (`convo_store.disagree`: under half as long, or another script; the
  turn is flagged `heard_differently`). `transcript` always holds the record, so older Convo reads the same words. Whisper is given a
  joined utterance whole (memory only, once), by the server, after the reply was heard, and never for a turn the model declined
  ([SILENT]/[WAIT]/[LISTEN]) or left without a reply: such a turn keeps no Whisper text at all (`Store.held`/`forget_whisper`; a late
  one is refused), so background noise never becomes "speech" in the history. There is no turn limit: the model sees summary chapters of the older
  conversation, then every turn they do not cover (`helpers/convo_memory.py`; chapters are appended in a new `memory_chapters` table,
  the `memories` row keeps the joined text for rollback, a correction in a summarised turn marks only its chapter stale, the
  conversation model writes them in quiet moments, and a request that still does not fit leaves its oldest turns out of that one
  request; a turn is never refused). Standing notes (name, the reply language asked for, lasting preferences; `memory_standing`,
  at most `STANDING_TOKENS`) are rewritten only with a chapter, in the same step, and sit after the chapters. Once anything is
  summarised the model is offered the read-only `recall_conversation` (Convo's own journal, this target only, FTS5 trigram with a
  plain-scan fallback; `memory_recall` is an index rebuilt from the turns). Sizes calibrate from the server's own count of
  summary requests that carry no earlier audio, kept per script of the history (`Realtime._calibrate`/`_ratio`, `token_ratios`: a
  ratio measured on one script never sizes another, which uses the conservative default; `convo_memory.calibrated`, 5% conservative); `reply.done` marks `prompt_tokens`/`cached_tokens` when
  the server reports them. The system text is two parts joined by `LATE_MARK` (`convo_history`): before it only what every request
  shares until a chapter swap (rules, persona, chapters, standing notes); after it this pass's instructions, clock, context and tool
  result, which the gateway sends at the front of the latest message, after the history (`sidecar/app.py split_system`), so the
  server can reuse its cached start.
  - Summary requests reuse that start (`Realtime._summarize`, `_upkeep_request`): the conversation's stable system text, the same tools and
    the same history, then ONE instruction last (`convo_memory.cached_instruction`, marked `UPKEEP`): "summarise the first N exchanges of
    the conversation above" (a chapter's turns are always the head of the history; N is counted as the history shows them, `exchanges`;
    a stale chapter's turns, chapters to condense, or a head the window had to trim go after it as data), on the conversation's slot, at
    most `2 x CHAPTER_TOKENS` out. Upkeep output is never passed through the reply header filter (a note may begin "Language: …"). The
    Compactor marks the turns with `covers`, which never reaches the utility model.
  - Compactor steps (`run`): owed standing notes, a stale chapter, a new chapter, condensing; each step fails on its own (a failing one
    is reported and the next still runs). A chapter is kept (or swapped in) the moment its text exists; its standing notes are a later
    step (`owed`, memory only), joined to the kept chapter while it waits, else their own change (`Store.set_standing`), so being
    stopped while notes are written never loses the chapter. Notes come from the user's own words only (never replies or tool
    results), as data, not instructions; NONE, nothing or a failure keeps the notes (never cleared by the summariser). A stale chapter
    whose turns exceed `verbatim_high` keeps its text (`Store.settle_chapter`); condensing carries a pending correction (`stale`).
  - On a cloud provider (`remote`) nothing stops upkeep: the writing goes on beside speech and replies (`_stop_upkeep`, `_cancel` and
    `_compact` leave it alone), and a written chapter is kept until no reply is being prepared (`_quiet`, `_swap`), so a reply keeps
    the request it was prepared with. The rest of this rule is for the user's own model server, where both would compete.
  - Speech stops upkeep: any candidate (the user speaking), a turn or a spoken notice cancels a summary being written and a warm-up
    (`_stop_upkeep`); `_compact` never starts while the user speaks (`_speaking`; the candidate loop itself asking is not speaking). It is
    restarted at every quiet moment: a reply's end, an utterance that was ignored, a spoken notice's end, and voice start. A warm-up of
    the conversation's own start that was stopped, or a start a one-slot notice replaced, is owed (`warm_owed`) and redone in the next
    compaction run.
  - Swaps (`Compactor` `hold`/`ready`/`swap`/`preview`): once the server holds this conversation's start (after its first request,
    `state.replied`) a written chapter is kept (at most one per conversation, nothing more written until it is swapped in; a swap
    re-checks its snapshot, so a kept chapter never overwrites newer words). One slot or unknown: swapped in as soon as a reply's text is
    complete, then the new start is warmed (`_swap_and_warm`, `_warm`: the same start with `WARM_TEXT` last, `warm: true`, one token)
    while the reply is spoken. Two or more slots (llama.cpp): warmed in the spare slot from the preview, swapped in only once warm, and
    the conversation moves to that slot (`_compaction`); after `WARM_FAILURES` (3) failed spare-slot warm-ups in a row (not counting ones
    stopped by speech) the chapter is swapped in on the conversation's slot anyway (developer `fallback` `warm_failed_swapped`). A cloud
    provider (`remote`) is never warmed or pinned; its chapter is swapped in when a reply's text is complete or in the next quiet
    moment.
  - Slots (`_discover_slots` at voice start, gateway `/slots`): llama.cpp's `/props` `total_slots` or `/slots`, with each slot's own
    context (`n_ctx`, per slot when the server reports it); anything else (an older gateway, no answer, another server) is one slot and
    unpinned. Only a llama.cpp server of the user's own gets `slot` on every conversation, summary, warm-up and spoken-notice request
    (`_pinned`; the gateway turns it into `id_slot`); a notice goes to a spare slot when there is one.
  - The window in effect (`_window`) is the smaller of the saved one and the server's per-slot `n_ctx`; the chapter budget uses it
    (`_compaction_settings`) and its bands leave room for the answer, this turn's audio, the fixed rules (`STABLE_TOKENS`, 2.4k) and the
    standing notes (`convo_memory.Budget`; 24k keeps the plan's numbers). A request's estimate also counts overheard clips
    (`context_audio`) and, for a tool's follow-up, the words before the call and the tool's result. When the server still answers that a
    reply's first pass does not fit (gateway `reason: context_full`), it is asked once more with the older half of the word-for-word
    turns left out of that request (`ctx.retry` mark); a later failure is reported as a provider error.
  - Who writes chapters (`helpers/convo_summarizer.py`, settings `summary_writer` conversation|utility, `utility_summary_consent` default
    off, both outside `capabilities{}`): the conversation model by default; Agent Zero's utility model (`Host.utility_model`) only when
    where its requests really go (`convo_host.resolve_base`, Agent Zero's order: the model's `kwargs` api_base/base_url, its api_base, the
    provider's kwargs, the provider's documented local default) is on the user's machines/network (`local_host`), or with consent; an
    address that cannot be resolved is not local.
    Refused writes nothing and no other model writes instead (developer `fallback`). The Conversation card has the select and the
    consent switch, and its `summary_writer` readiness row says which model writes them.
  Developer mode shows memory (diagnostics "Memory", `memory_view`
  {target}, `convo_inspect.memory_view`): the estimated memory against the window and its budgets, each chapter with the turns it
  covers, when it was written, its size and whether it waits to be rewritten, the standing notes, the word-for-word part, and the
  memory events seen since Agent Zero started (`convo_inspect.compaction` `kind`: `compaction` written and swapped in or deferred and
  why, `written` kept for its swap, `swap` with its slot, `warm` with its time and slot or stopped by speech, `fallback` slots unknown or
  the summary writer refused; memory only); a request that left turns out says how many (`ctx.estimate` `ctx_left_out`). Tests:
  `test_memory.py`, `test_memory_upkeep.py` (the shared byte-identical start, speech stopping upkeep, one-slot and spare-slot swaps, no
  warm-up or pin for a cloud provider, the utility writer's consent, a 300-exchange English/Russian/Hindi soak with 20 s and failing
  summaries), `test_memory_upkeep_review.py` (notes after the chapter, steps failing alone, oversized stale chapters, NONE keeps notes,
  restarts after ignored speech and notices, the window in effect, the overflow retry, warm-up fallback, per-script sizes). Not proved live: a real llama.cpp server's cache reuse and `id_slot` behaviour, warm-up and re-read times, and summary
  quality from the cached start.
  The hidden header is `USER:` then `LANGUAGE:` (`helpers/convo_user_line.py`), removed on every pass; a header line longer than
  `SCAN` is dropped up to its newline (at most `DISCARD` characters), never spoken, its start kept (cut to `LIMIT`) as the words. A turn that carries the user's audio also says, after the
  history, that the model hears the recording itself (`convo_user_line.HEARING`: voice, tone, accent, pace and sounds; it may
  describe them when asked; earlier turns are text), the gateway names the audio part (`sidecar/app.py` `AUDIO_LABEL`), and the
  control-word rule forbids bringing up audio quality unprompted only: a model told nothing concluded "I only receive your words
  as text" (2026-11-18). History goes through one rule shared by the gateway and the
  developer view (`helpers/convo_history.py`): declined and superseded turns never appear; every assistant message follows the user's
  words, else the turn's audio while it is still in memory (`RECENT_AUDIO` turns, never stored); an exchange with neither in
  which a tool was used and its answer heard shows the dated tool use in the user's place (2026-11-25: such an answer was left out and
  the model then denied having given it); any other exchange with neither is left out
  on both sides (no placeholder: "[their words were not captured]" primed replies about audio quality that then filled the history);
  consecutive user messages are joined; an interrupted reply says so. Markdown is never heard or remembered
  (`helpers/convo_plaintext.spoken`, by the syntax's form, never by words): each piece of a reply is cleaned as it is cut from it
  (`say`, knowing whether the piece begins a line: a "- " after ". " is not a bullet), a spoken notice whole, and the whole reply
  before it is settled, so heard and generated text compare like for like; the live draft uses the browser copy
  (`webui/plain-text.js`; both read `tests/fixtures/plain-text-cases.json`). Every voice system text carries `HONESTY` (a general rule about
  what an answer may rest on: the clock, tool results, the conversation and what can be worked out reliably from them; check what may
  have changed since training with a tool, else say so; never invent a fact or a tool's result; read short follow-ups in the light of
  the conversation) and the user's local date, time, zone and today's UTC offset (`Host.local_time`, `clock_text`,
  computed once per turn so a held parallel reply still matches). Voice answers ask local servers not to think
  (`chat_template_kwargs.enable_thinking=false`, never sent to a cloud provider), reasoning is never spoken, and `answer_tokens`
  (default 1024) bounds an answer.
- Tools: the catalog (`helpers/convo_tools.py` `CATALOG`). Every tool has its arguments (several, typed, checked by `validate` before
  anything runs: unknown fields dropped, a choice matched whatever its case, text over its limit refused, never cut), a description
  that says what it is for and when to use it in model-agnostic words, a permission group, and a kind that decides what it takes to
  run it:
  - `read` (web_search, web_read, memory_read, task_status, system_status, world_time, chats, listen_back, recall_conversation,
    camera_context, screen_context): runs directly, also in a tool's follow-up.
  - `quick` (set_timer, open, voice_speed): a small change the user sees and can undo; runs directly in a conversation the user
    started, never in a follow-up.
  - `work` (delegate_task, cancel_task, remember): starts or stops work, or saves something lasting.
  One list per conversation, in a fixed order (`offered`), sent on EVERY request of the conversation, a tool's follow-up included, so
  the request's start never changes; Convo, not the list, decides what runs. The list depends only on: the user's permission
  switches (settings `voice_tools`, one per group: web, memory, tasks, system, timers, chats, senses, interface; a group switched off
  is not offered at all), a chapter swap (`recall_conversation`), a shared camera or screen with a model measured to accept images
  (`camera_context`, `screen_context`), whether the model hears audio (`listen_back`), and how it takes tools: a model that takes them
  in writing (`tool_mode` 'text') is offered the small set only (`small`: eight tools; few, flat tools, never one tool with many
  actions). `tool_mode` (`convo_tools.tool_mode`): the measured record (`data/model_capabilities.json`, the connection check's
  probe) decides; the declared `tool_calls` setting counts only while nothing is measured, and in developer mode (`override`) it
  turns native tools on whatever was measured; native means `tools` + `tool_choice: "auto"`, otherwise they are
  described in the system text with the written `<tool_call>{...}</tool_call>` format `parse_text_call` reads (also Gemma, Hermes,
  `<function=…>`, Mistral `[TOOL_CALLS]`, bare JSON; the same `validate`). A request never carries both. `search_engine` (Agent
  Zero's name) is `web_search`; `camera_snapshot` (the fork's) is `camera_context`. Tool markup is recognised before the control-word
  filter. Adding a tool: one catalog entry, its executor in `convo_voice_tools` (`read` or `act`), a word in `webui/view-helpers.js`.
- What it takes to run a call (`convo_policy.authorize(tool, kind, decision, conversing, followup, untrusted, always_ask)` ->
  `run` | `confirm` | `refuse`; the ONE place this is decided, and the join for a permission system: `set_authorizer(function)`
  may return a verdict or None, after the read-only and follow-up rules):
  - read-only tools run. Nothing else runs in a tool's follow-up (the pass that has just read someone else's text): the model is
    told (`not_now`), journal `tool.refused` {reason: followup}.
  - `work` runs when the user's own audio carries the request (`verified`: a typed classifier's explicit-request probability on its
    own, 0.9 or more; never the lower of it and the attention reading). A request the user plainly made is never refused and
    never asked about twice. Without that evidence, in a conversation the user started, Convo ASKS instead of refusing
    (`confirm`): always for `remember`, when a tool's result is in the request's history (`convo_tools.results_in`), and when the
    classifier heard no request at all (explicit under 0.5: the model acting on its own initiative); a turn no typed classifier
    judged runs as before unless a tool's result is present. Outside a conversation the user started the classifier's own routing
    (`route_tool`) still applies and there is nobody to ask: unverified work is refused, and said (`not_started`).
  - Asking (`convo_voice_tools.ask`): nothing starts; one pending question per conversation (a newer one replaces it; a second one in
    the same pass is told to wait, `one_question`). It is ASKED ALOUD: the model asks the user in one short sentence in their
    language (`ask_first`; when it says nothing, Convo says the question itself), and the panel's card with Run and Dismiss
    (`action.pending` {id, name, label, summary, seconds}, mark `action.shown`, its time starts) comes when the first audio of the
    question is sent (`heard`), or at the reply's end when it could not be spoken, never before. A question nobody heard because
    the reply was stopped or taken over is withdrawn without a word (`asked_aloud`, journal `action.replaced` {by: unheard}; what
    took over holds the request and asks again if needed). Only an utterance that BEGAN once the question could be heard
    (`answerable`) is read as its answer, on its own (never joined to an earlier fragment, no held parallel reply) by ONE isolated
    request to the conversation model (`reading`: `READER` as the whole instruction, the utterance's audio or recognized text, an
    empty history, no tools: nothing in the conversation can answer for the user). Yes: the action runs exactly once (`perform`),
    the reply is told what was done, and a call for the same tool in that reply is answered, not run again. CHANGE (they agree, but
    differently): nothing runs as asked; the model makes that one call again with the change and is not asked twice
    (`granted_names`, that tool in that reply only). No, or anything else said: dropped, nothing started. Unreadable: the question
    stays open and the reply points to the Run button. Unanswered for `PENDING_SECONDS`: dropped. A yes or no is always answered
    (never taken for an acknowledgement). The decision model is NOT used for this: measured on held-out speech it recognised 0 of
    40 spoken "yes" across eight wordings (and short commands no better), so no action is decided by it. Journal `action.asked`,
    `action.confirmed` {by: voice|panel}, `action.declined` {by}, `action.expired`, `action.replaced`; never silent.
- The tool loop (`api/realtime.py` `_turn`). The calls of one model pass are carried out together (`convo_tools.pick`, at most
  `MAX_CALLS`): look-ups side by side, anything that acts one by one before them (asked for before any of their results was read, so
  no result can have asked for it), one follow-up with every result. A reply may do this `MAX_TOOL_ROUNDS` (4) times within
  `TOOL_BUDGET` seconds; an identical call is not run twice (`repeated`); a call whose arguments do not fit is not run and the model
  is told why; anything that is not read-only ends it. A look-up that can take a while has an optional `say` (`SAYS`): what the user
  is told aloud while it runs, said only when the model said nothing before the call, and not again within `SAY_AGAIN` seconds;
  never part of the call or kept. NEVER SILENT (live 2026-11-24: three look-ups, then one more call instead of an answer): an answer
  to results that says nothing, or only calls again when nothing more runs, is asked for again with the list closed (`no_tools`
  'none': same tools, `tool_choice` none), then with no tool in the request ('text': calls and results as text), then `NO_ANSWER`
  is said; an empty first answer is asked again twice, then `NO_REPLY` is said when it was plainly said to Convo; a model that fails
  before anything came is asked once more when the failure may pass (gateway `reason` 'transient': 408, 429, 5xx, a cut connection),
  else `NO_MODEL` is said beside the panel's error. The same model is asked each time, never another. Each follow-up is the first request unchanged plus, at its end, every call of the
  reply so far and its answer (`tool_turn`, and `tool_turns` from the second on: natively assistant `tool_calls` and `tool`
  messages; otherwise the written call and a results block), which says the tool finished or failed and why, in Convo's words, and
  whether more read-only calls may follow (`ONE_MORE`) or nothing more runs (`NO_MORE`); when an audio turn's first pass was only
  a call, the follow-up is asked to begin with the hidden header (`header_wanted`). An action's result is told the same way
  (`done`): the model says, in its own words and the user's language, what is now true; after a queued task only a follow-up that
  says nothing is completed with Convo's fixed "Queued that…" sentence. A task is queued after `ACTION_HOLD` (a correction within it
  supersedes the task, mark `tool.superseded`); "queued", never "done". Each use is kept in the turn's `tools` (`Store.tool_used`:
  dated call, its arguments, a short result; at most 4 per turn; kept even when the user interrupted) and journaled
  `tool.requested` / `tool.done` / `tool.failed`; the history shows it as the call and the tool's answer natively, else a dated
  note, its result labelled as untrusted data, not instructions (`convo_tools.UNTRUSTED`).
- Web search uses Agent Zero's own SearXNG through a ladder (`helpers/convo_search.py`, pure; `Host._search_ask` sends): the
  ordinary request through Agent Zero's helper; when it has no results and no direct answer (its default engines often refuse a
  home address), the news category and, in one request, up to four full web engines the service has loaded but does not ask by
  default and that follow the search's language (named from SearXNG's `/config`, never from a list of Convo's; an engine that
  refused is skipped for 15 minutes, memory only). `recency` (day, week, month, year) is the model's own word that the answer must
  be fresh: it brings the news engines in and bounds the dates; Convo never guesses it from the search's words. The sources are
  woven (`weave`), news dated over a year ago goes after fresher news (`fresh_first`), and a result that shares too little with
  the search goes last (`relevant_first`). At most three requests and 8 seconds per search; an identical search within 90 seconds
  is answered from memory (this is a cache of search results, nothing to do with the conversation's memory). No engine answering
  is a FAILED search, never an empty one. No paid search key is used. When the reply's FIRST call is a search that got nothing
  (every engine refused, or none returned a result), in a conversation the user started with tasks allowed, the question is handed
  to the chat's agent, which searches with its own browser (`SEARCH_HANDOVER`, idempotency key `<turn>:search`, mark
  `tool.search_handover`): the user asked for the lookup, so this is done, not asked; the model says so, and the answer is spoken
  when the task's result comes back. Mark `tool.search` {steps, asked, answered, refused, cached, seconds} (counts, no engine
  names). Settings shows the engines' last known state from memory (`search_health`; opening Settings never searches) and "Check
  search" sends one small search (`search_check`). The result has a dated header, SearXNG's direct answers and info boxes first,
  then the top five (title, address, snippet), at most 2,400 characters.
- The other executors (`helpers/convo_voice_tools.py` joins them to a conversation; none decides whether it may run):
  `web_read` (`convo_webread`: Convo's own bounded fetch in a thread; http/https only, hosts that resolve ONLY to public addresses,
  each redirect checked, the checked address is the one connected to, no cookies or credentials, 1.5 MB, 8 s; Agent Zero's Browser
  plugin is never used from a voice turn: it would take over the user's browser panel and read pages with their sign-ins);
  `system_status` (`convo_actions`: a fixed set of read-only readings the model can only choose between, never a free-form
  command; no addresses, user names, paths or command lines); `world_time` (the system's time-zone data); `chats` (Agent Zero's own
  registry and the chat log the window shows; chat text is untrusted data); `task_status` and `cancel_task` (this conversation's
  jobs and timers); `set_timer` (`convo_timers`: its own table `voice_timers` in the journal database, 1 s to 24 h; due timers
  become Agent Zero notifications on the job coordinator's thread, voice running or not, and are said aloud once by a running
  conversation in a quiet moment, also when they went off up to two hours before voice started); `open` (the page opens a chat,
  Settings or the file browser, or shows a link the user taps; the page answers whether it worked); `voice_speed` (this
  conversation only; sent with each speech request, Kokoro takes it as its own speed, a server that knows no speed ignores it);
  `listen_back` (`Ring`: the audio the model was already sent, at most 8 clips / 30 s / 5 minutes, memory only, cleared on Stop);
  `screen_context` (like the camera: one frame of a screen shared with a button, never stored); `remember` (Agent Zero's own
  memory save, always after the user's yes). Memory loads on first use in a background thread.
- Thinking before a voice answer: a local server is asked not to through its chat template; a provider outside the user's
  machines only in the one way Test connection measured that it accepts AND that stops the reasoning (`probe_thinking`,
  `convo_tools.THINKING_OFF`, record `thinking_off`); never a guessed field, since a refused request would fail the turn.
- Every speech request marks what it carried (`speech.sent` {chars, language, profile, seed, path}; `speech.language`
  {language, why}): names and numbers, never words.
- One conversation across chats (settings `conversation_scope` "global", or voice started with no chat selected): the journal,
  history, memory and jobs are keyed `convo_contract.GLOBAL_SCOPE` in the existing `target` columns (a key that can never be a chat
  id; no schema change). The chat selected in the interface is only the default place for work (`state['work']`, event
  `convo.work`): selecting another chat changes nothing else (no new epoch, nothing cancelled or rebuilt). Its agent is a hidden
  Agent Zero background context (`Host._voice_context`: persona, utility model, default memory), taken out of Agent Zero's
  registry so it is never listed, saved or picked for a typed message; no task is dispatched to it. A handed-over task goes
  (`convo_voice_tools.place`) to the chat the user names (`delegate_task` `chat`: a name, or "new"; none or several matching is
  told to the model, which asks), else per `task_chat`: a new chat (made as Agent Zero's own "new chat" does; the same one for the
  later tasks of that voice session) or the selected chat. A job keeps its real chat as `target` and the conversation that asked
  for it in table `job_scope` (older code ignores the table and still dispatches the job): its events, its place in the list and
  its completion notice stay with that conversation, also in a later voice session. A chat's own conversation is unchanged, and
  may hand a task to a named chat the same way.
- A finished task's result (`_notice`): recorded in the notice's turn as a `task_status` use before anything is spoken, so the model
  knows it whether or not it could be said; asked for in at most two sentences, spoken through the reply's own speech queue (`_say`:
  requests of at most `MERGE_LIMIT` characters, never one long one; measured live 2026-11-24: 1100 characters in one request met the
  speech server's audio limit and cut the reply off). When nothing of it was heard, `Store.release_notice` gives the claim back and
  the page is told (`notice_retry {job_id}`), `NOTICE_TRIES` times in all; then it is said in the panel. Every reply is also told the
  present state of this session's tasks (`_tasks_note`, per-pass part, held parallel reply included): an earlier "still running" is
  never what the model goes by. `Host.log` writes no line into the chat for a task that completed: after the agent's answer it would
  open a processing group that never ends, and any line takes `update_progress="none"` so the chat is never left marked busy.
- Mute and push-to-talk while muted are the page's alone (`webui/AGENTS.md`): the server only sees that no audio arrives.
  `convo.candidate_cancel {session_id, epoch, turn_id}` drops an utterance cut off by a mute (not a completed one), so the next is not
  answered `busy`. `notice_retry` is matched to the conversation, not the epoch. `turn.superseded {by, joined: false}` (false only in
  journals from before 2026-11-24, when a short command replaced the earlier turn instead of being joined to it) decides which history
  rows the panel hides: what was said in several parts is one "You" entry (developer mode: "said in N parts"; a part without words is
  no row), and "Reply interrupted" is a row only when some of that reply was heard.
- Setting `hotkeys` (one key combination or '' per action, `_validate_hotkeys`, public to the browser, ignored by older versions);
  `hotkey` stays as the second start/stop shortcut.
- Where `authorize` would ask about starting or cancelling work, the user's own utterance is first read alone
  (`convo_voice_tools.asked`: no history, no tool result and not what the model proposed reach that request); a YES runs it, so work
  the user plainly asked for is not asked about (measured live: the decision model read "Hand this to the main chat: ..." as .23).
  Saving to long-term memory is still always asked about, and a permission system (`set_authorizer`) decides alone.
- An utterance that stopped a reply is always put to the conversation model, whatever the classifier makes of it once it is complete
  (reason `interruption`): a reply is never stopped for nothing.
- A phrase whose speech request failed before any audio went out is asked once more (`_speak`, mark `speech.retry`); one that began,
  was refused (4xx) or found the server busy past the wait is not.
- Camera ("Share camera" in the panel, `webui/convo-store.js`): the stream stays in the page and is released on Stop; the session only
  learns `convo.camera {shared}`. Images are offered only when `data/model_capabilities.json` (`convo_tools.measured`: the measured
  record for this url and model, `image_input: true`; written by the capability probe, never in settings `capabilities`) says so;
  default not offered. When the model calls the tool, the server asks for one frame (`camera.request`), the browser answers
  `convo.camera_snapshot` (longest edge 768 px, JPEG 0.7, checked by `convo_tools.snapshot`), waited for at most `SNAPSHOT_WAIT`;
  the picture goes to the model in that reply's follow-up only and is never stored or journaled.
- Capability probe (`convo_setup.probe_capabilities`: the conversation card's connection check, which runs by itself once an
  address or a model name is committed and from Test connection, only when the model is listed; refused while a voice session is
  live): three requests at temperature 0 (thinking off on a local server). The third carries a short synthetic clip:
  `audio_input` true when the model answers it, false only when the server refuses it (400/415/422) after answering another
  request of the same check, unmeasured otherwise (`audio_verdict`), so a key, a quota or an outage never changes the record.
  The first two: one tiny tool
  (`get_time`) with `tool_choice: "required"` (a 400/422 is asked once more with "auto"), reported `native` (a structured call came back),
  `text` (a call written in the answer: `parse_text_call` reads it or it names the tool) or `ignored`; one 16 px red PNG built in code,
  `image_input` true only when the answer names its colour, false on a refusal (400/413/415/422/500/501) or a blind answer, unmeasured
  otherwise. The verdict is merged into this url+model's record in `data/model_capabilities.json` (`{"models":[{url, model, tool_mode,
  image_input, measured_at, ...}]}`, newest first, 50 kept; fields not measured keep their value), never into settings `capabilities`.
  No model text is echoed. The card shows it under the connection's status line (`conversationMeasured`: the fresh answer, else
  the readiness row `conversation_measured`). The page then writes what was measured into the two declarations an older Convo
  reads (`capabilities.audio_input`, `capabilities.tool_calls`) as an ordinary edit, except in developer mode, where those
  switches are the user's; the server never writes settings from a probe. The session reads the record once
  (`Realtime._tool_offer`), and again when the camera is shared.
- A request to do something is never answered with a control word (`convo_control.INSTRUCTION` says so: the model acts, with a
  tool when it needs one, and answers; live 2026-11-22 a model answered "set a timer" and "speak slower" with [SILENT] and an
  invented result after it).
- A bare acknowledgement of an answer with nothing new ("okay", "got it", "thanks") is left to the model (`convo_control.INSTRUCTION`:
  usually `[SILENT]`, at most one brief reply, never a reply to a repeated one); there is no word-list gate.
- The decision model is told one plain sentence (the audio is the latest thing the user said, and
  what the assistant last said that the user heard), never a JSON document; the policy questions
  are short and describe what the speaker is doing (`convo_system_one.QUESTIONS`). The wording was
  chosen by measurement against the previous wording, not by taste: change it only with a new
  measurement on held-out sentences. Activation names go into the name question and the attention
  question only. After an assistant reply that ends in a question the "had just said" line is kept
  only for a short answer (audio up to 3 s, recognized text up to 8 words): measured, the model read
  any speech after a question as its answer (acknowledgements 8% -> 93% answered, talk to someone
  else 24% -> 40%); a statement as context is always kept (`QUESTION_CONTEXT_SECONDS`). It is told only while it was the last
  thing said: when the latest turn is still unanswered (its reply not yet heard) or was answered with silence, nothing is said
  about the assistant (`_last_heard`; measured 2026-11-17 on the recorded corpus: with that line 53-75% of real requests read as
  not for the assistant, 1-13% without, so a question asked while an earlier answer was still coming read as an acknowledgement).
  The decision model reads only the first ~23 tokens of each criterion (text beyond is ignored; keep every cue
  short, decisive words first). Nothing about earlier classified turns is added to the sentence: a "previous speech
  was ignored" line did not help a late "what do you think?" and inflated talk to others (measured), so connecting
  a later question to an earlier remark is the main model's job, not the classifier's.
- The host masks anything a websocket handler raises as "Internal server error". A
  refused `convo.start` is therefore returned as data (`ok: false, refused: true,
  message`) and the browser shows that reason; other events keep raising, because
  they are protocol faults rather than choices the user can fix.
- Preserve full event history separately from compacted model context; summary splices must validate their source prefix.
- Persist runtime data under the host user plugin root, never the installed source package.
- Opt-in managed gateway uses a plugin-owned isolated runtime environment, loopback inherited socket and ephemeral authentication. Native ON/startup starts it; OFF/update/uninstall and parent death stop only the owned process. Never install dependencies or start models during activation. External gateway mode remains supported without automatic fallback.
- All unit tests must use temporary directories and stub host settings/network operations.
- Report implementation, integration checks and manual listening acceptance separately.
- General Convo settings saves must not write native provider settings; explicit native/legacy speech controls own those writes.
- Studio lifecycle commands target only the fixed authenticated `convo-audio` service. Never route lifecycle commands to an arbitrary configured external TTS endpoint.
- Model downloads require a reviewed source manifest and explicit approval. Verify immutable revision, file sizes/checksums and storage reserve before registration. Preserve partials and quarantine corrupt bytes outside registered models.
- Retain vendored runtime attribution. The explicitly approved testing-phase Studio is a plugin-owned adaptation of the original interface, behind a fixed authenticated same-origin bridge. No weights or standalone connection/library configuration belong in it.
- All settings writers, including native provider bridges and migration, share `setup_store` transactions. Sidebar, Studio and native Convo editors use revision-aware saves; never return saved credential values to the browser. Keys are host-managed, endpoint-bound and separate from configuration.
- Settings/readiness reads never manage processes. Only native ON/OFF, startup and explicit runtime controls change owned lifecycle. Gateway health, Studio availability and model readiness are separate facts.
- Studio mutations acquire the shared admission lease; session creation checks the same lease. Frozen speech includes clone, model identity, complete tuning and one seed across its phrases.
- Advanced synthesis controls for the user's own audio.cpp server (`helpers/convo_synthesis.py`, provider `audiocpp`, connection external): every
  speech request of one reply carries the fork's `tuning` envelope (`provider: qwen3tts-audiocpp`, `scope: realtime`, `overrides` with ONE seed per
  reply, and the server-resolved id, revision and complete values of the chosen profile, or with none chosen of the server's own Realtime
  selection, so a reply is never re-read per phrase); `roles.tts.tuning` {profile_id, overrides} is the saved default, `convo.tune` the
  session's own choice from the next reply on, `synthesis_read`/`synthesis_resolve` read-only server calls. `convo.tune` resolves the
  choice with the server when it is made (outside any reply), is session- not epoch-scoped, answers `{pending}` ("applies when the
  conversation is ready") while preparing/switching chat or for an unknown session (never a raised error), `{read: true}` reports what
  the conversation speaks with (`_tuning_status`), and a refusal keeps the previous choice. Replies only read the stored snapshot
  (`_reply_tuning` never awaits the server); a snapshot older than `TUNING_REFRESH` (60 s), or none yet, starts one background
  `_refresh_tuning` after the current reply (also once at session start); a failed refresh keeps the last good values, stores
  `tuning_error` and says it once as an info notice. With nothing resolved yet the envelope stays minimal (the reply's seed). Overrides that
  clash with the preset's own block sizes (`fit_problem`) are refused by `resolve` before they are used or saved. A reply's speech role is
  read once (`_tts_role`, the session's copy if the settings file is caught mid-write). Spoken notices carry the same envelope.
  `max_reference_seconds` is whole seconds; `text_lookahead` (16-512) and `phrase_flush_ms` (50-3000) stay stored but are not shown
  (the server ignores them for Convo). Each streamed phrase's `audio.begin` carries
  `prime_ms`, the fork's start-up buffer (`convo_synthesis.prime_ms`: 800/1280/1760 ms for its built-in profiles, else the first
  block). Every reply is spoken in one language, decided at its first phrase and frozen (`helpers/convo_language.reply_language`):
  the model's declared `LANGUAGE:` line mapped to the names the speech backend accepts (`speech_providers.accepted_languages`:
  audio.cpp and OpenAI-compatible Qwen3 servers take ten names plus "Auto"; OpenAI's speech API, Kokoro and undocumented servers
  have no language field and get none), with English and native names, ISO codes and regional tags; a declaration that does not map
  makes the whole reply "Auto"; no header: a non-Latin script holding a clear share of the letters (`SCRIPT_SHARE`, at least two of
  them; one Cyrillic letter or kana never decides) or a clear-majority common-word guess on the first phrase plus the user's words,
  else "Auto". The previous reply's language is never carried over. A script never overrides a declaration for the reply: a phrase
  (the first included) wholly in another script is voiced in its language and the other phrases keep the declared one; a single word
  never changes it; in a reply whose language has its own script, a phrase with none of it that clearly reads as a Latin-script
  language is voiced in that language.
  A tool's follow-up shares its reply's language; a spoken task notice uses the language of the reply that queued it. The stable
  rules (`LANGUAGE_RULE`) say: any language or accent, follow the user, keep a language they asked for. Convo never selects or creates a profile on the server.
- Who the assistant is: `voice_name` (the panel's label; '' = the profile's title) and `voice_profile` (speak as this Agent Zero
  profile; '' = the chat's own). A pinned profile's persona is built from that profile's prompt in a background thread
  (`Host._profile_prompt`, as `call_subordinate` builds another profile's agent), read once per style and reused; delegated work
  still goes to the selected chat with that chat's profile. The `history` action returns `assistant_name`; `setup_read` lists
  `profiles` [{id, title}].
- One voice conversation at a time, owned by one page (`helpers/convo_ownership.py`, new table `session_devices` {session, page, device,
  started}; `sessions` unchanged, a re-attach rewrites its `owner`; older Convo reads the database as before). `convo.start` takes `page`,
  `device` (browser and platform only) and `takeover`: with another conversation running it is refused with that device named, unless
  `takeover` names it, which ends it with the existing per-connection shutdown (`_end`: bounded cancel, then stop) after telling its page
  `convo.taken_over`; under one lock, so two take-overs have one winner (the second is refused). A connection that drops while its
  conversation has a page id is detached (its epoch moved on first, so the cancelled turn is no longer current and sends or compacts
  nothing; then work cancelled and the connection released as on any disconnect) and kept
  `GRACE_SECONDS` (20) for `convo.attach` {session_id, page} from that page, which moves it to the new connection under a new epoch
  (also when the server had not yet noticed the drop, with the same epoch-first release); the detached entry is registered before the
  release is awaited and an attach waits for that release (`ATTACH_RELEASE_WAIT`), and the conversation's own old connection still
  retiring never refuses the move (`register_connection(..., replacing=old)`); anything else is refused and the page stops. Stop, disable (plugin OFF), a
  start without a page id, a start of the same page (a reload) and the grace ending stop at once; OFF also stops detached sessions
  through `deactivate`. Status returns `voice_owner` {session, target, page, device, since} only from a running service.
  Tests: `test_voice_ownership.py`, `store-ownership.test.mjs`.
- Record raw and sent (developer mode, only on the user's press in the Sound drawer; `helpers/convo_recordings.py`, actions
  `recording_save`/`recording_list`/`recording_delete`): one utterance saved twice in `data/recordings/` (the microphone before echo protection
  and the gate at the device's rate, and the 16 kHz audio Convo sent), with an analysis aligned on their ends (levels, stretches where
  the microphone heard sound but silence or floor noise was sent, the clipped start); at most 100, listed and deletable. Like test clips,
  the one exception to "no audio stored". Tests: `test_recordings.py`.
- Developer aids (developer mode only): "What the model saw / wrote" (`helpers/convo_inspect.py`, memory only, the last `RING` = 10
  turns; captured only while the saved `developer_mode` is on (`Realtime._developer`, not the session's copy); forgotten when it goes
  off or Convo is turned OFF (`Service.deactivate`), and a conversation's turns when it ends (`clear(conversation_only=True)`; the
  compaction runs stay)): every request made for a turn (`first`, `again` = a retry, `tool follow-up`, `early` = the held parallel reply), kept by
  `Realtime._answer(state, request, turn)` with its audio in memory, and the stream tapped there before any filter (`tap`: the raw text,
  structured tool calls, the gateway's `answer.done` figures). `generate` records what comes out of each filter (`stage`: after the
  hidden header, after the tool-markup guard, after the control-word/[LISTEN] filter) and hands over the filter objects (`filters`), so
  the view shows what each took out (`removed`: header lines, markup, control word or stage direction) plus the heard line, language,
  control word and reason; `_speak` adds what was sent to speech, `convo.transcript` Whisper's text, `_run_tool` each tool's outcome. Actions:
  `inspect_turns` (the view: text only, a clip as its length, never audio bytes; `metrics` carries `inspect_revision` so the panel reads it
  again only when it changed), `replay_save {turn}` (only on the user's click: one turn WITH its audio to `data/replay/<id>.json`, at most
  `SAVED_LIMIT` files), `replay_list`, `replay_delete {id}`. A camera picture is never written; a role keeps only `ROLE_FIELDS`, never a key
  or credential reference. `metrics` still adds `last_request` (the older single-request view); `clip_save`/`clip_list`/`clip_delete` keep labelled test clips
  for measuring turn-taking models in `data/test_clips/` (`helpers/convo_clips.py`; recorded only on the user's press, never sent
  to the conversation). `status` returns `build` (`helpers/convo_build.py`, a hash of the browser files) so an open page can offer a
  reload after an update; `show_diagnostic_events` / `show_rejected_responses` gate the panel's developer rows.
- Before Studio mutation dispatch, persist the operation identity in `data/studio-admission.json`; acknowledge only complete synchronous success or definitive rejection. Transport/cancellation/opaque server failures retain uncertainty across OFF, module refresh and host restart, blocking Convo start and further Studio writes while permitting status/native OFF. Never treat interface restart as audio-idle proof. No reset may be exposed until backend-aware terminal-operation reconciliation exists; readiness must explicitly report this limitation.
- User-visible transcripts and playback-start/completion records are distinct from model drafts. Delayed transcripts update the original turn/target.
- Keep incomplete capability paths explicit in README and the release gate; no speculative/omni/native-PCM performance claim without measurements.
- Native Kokoro must be already loaded with cached selected voices. Run synchronous synthesis off the WebSocket loop, keep one tracked worker, and withhold new TTS while a detached worker is still finishing.
- Native plugin OFF must release Convo sessions and owned Python patches, pause new job dispatch, retain histories/settings and observe already-submitted jobs. Track exact patch ownership; never overwrite another plugin's replacement.
- OFF moves active sockets into a host-owned retirement registry before scheduling
  owning-loop cleanup. Removing a socket or requesting cancellation is not an
  acknowledgment: `forget_connection(..., completed=True)` follows completed
  cleanup, and any scheduled disable callback must also finish. Failed/cancelled
  cleanup and closed loops stay blocked; native ON, session start and code
  upgrade reject retirement. Namespace purge takes import, then lifecycle, then
  connection locks; teardown callbacks run before those locks, never under them.
- Lifecycle lookup is read-only after singleton creation, including cached module
  references and adapter aliases. Only fresh native ON may adopt the current
  lifecycle class, under import then control locks after all prior patch records,
  keys and callbacks are released; preserve the host-owned object and its lock.
- Use plugin-owned `tests/in-image.ps1` with existing images and bounded, empty test state. No rebuild, dependencies, user volumes, GPU or network for host-only tests. The one Doctor fixture is reused by default; additional fixtures require genuine distinct isolation. Keep scratch outside read-only source mounts.
- Persistent restoration uses `tests/install-persistent.ps1` while Convo is OFF. It copies source only, hashes every file, publishes the manifest last, and preserves settings/data/toggles. Refresh through Plugin Doctor, then explicitly enable in the actual host process; a child Python toggle is not lifecycle proof.
- AI Link development mode permits native compatibility while browser voice stays idle. Mode entry and migration require no active or retiring voice connection, under the shared settings lock. No AI Link protocol, endpoint or Unity configuration changes are implied.
- Scoped updates of an installed Convo use `tests/update-persistent.ps1` (`-Target test` by default; `-Target production` needs `-ConfirmProduction` and the user's approval): deployable source only, never `config.json`, `data/` or toggles; Convo must be OFF to apply or roll back; every replaced or removed file is backed up first and `-Rollback -BackupStamp` restores it. Test the policy without Docker via `tests/update-policy.test.ps1`.
- A right-canvas panel wrapper must carry the host's `is-active`/`is-mounted` bindings; `tests/canvas-ui.test.mjs` checks it against `tests/fixtures/host-canvas-panels.html`, which holds verbatim host markup (refresh it from the host when the host contract changes). `tests/vm-loader.mjs` is the shared browser-state loader and fails on an unlisted host import.
- Turn engine (`api/realtime.py`; each piece is its own helper module; `first_clause_speech` and joining are on by default). `speculative_reply`: for a completed utterance in
  active listening only, while no reply is being generated, the conversation model starts at the same moment as the classifier; its output is held
  (`helpers/convo_speculation.py`: nothing shown, spoken, journaled or run, tool proposals only recorded) and is replayed through the normal output
  path only if the turn is admitted AND its own request would be identical (system text, history, audio, tools); anything else (ignore, withhold,
  clarify, a classifier error or timeout, Stop, retarget, a failed or oversized held reply) cancels it within the usual two-second join. Every
  completed utterance then also reaches the conversation model, ignored ones included: say so wherever the setting is offered. `main_listen_decision`:
  outside a conversation the user started only (never offered in active listening), the model may answer exactly `[LISTEN]` (`helpers/convo_listen.py`)
  which is no reply and no assistant history; it only suppresses, and a confident
  explicit request (explicit and 0.9 or more) is asked again once without the option instead of being silenced. `continuation_seconds`: memory only,
  active listening only (`helpers/convo_continuation.py`, adapted from the standalone fork's reopen tracker): an `unfinished` completed utterance is held,
  and an admitted one whose reply has not started to be heard is remembered, until the first audio of its reply is sent, the turn ends without a reply,
  Stop/retarget/disable, or the horizon plus one turn has passed with no answer to it being prepared; a fragment starting within the horizon is joined
  (real gap as at most 1.5 s of silence, at most `MAX_REVISIONS` joins) and classified as one utterance; on admission the earlier turn is journaled
  `turn.superseded`, leaves model history and its still-queued jobs are cancelled (running work is never touched). The audio is never journaled.
  ONE REQUEST UNTIL AN ANSWER IS HEARD (live 2026-11-24: a pause of over 7 s before a slow answer, a thought of over 20 s and a short confident fragment
  each cost the user what they had said before): (1) speech that begins while the answer to the earlier part is still being prepared (`_unanswered`,
  kept with the candidate as `joins`) joins it however long the pause was; (2) what no longer fits one clip (`min(max_audio_seconds, 30)` seconds) stays
  clips of its own (`Merge.parts`, at most `MAX_PARTS` and `TOTAL_SECONDS` in all), sent to the conversation model before the newest (`earlier_audio`,
  labelled by the gateway as one thing said in several parts; the USER line covers all of them) and to Whisper as one; the classifier hears the newest
  clip; (3) after the horizon, or as a clip of its own after a pause over `SAME_BREATH`, only a request (or speech whose first checks already ended that
  answer) joins (`Merge.late`, judged on its own): a remark waits for the answer (`_after_reply`; several
  that wait for the same answer are answered as one) and, if a request ends that answer first, every one is told to it in order
  (`left_over`); (4) a turn that started or changed something (`_acted`) is never joined to beyond the horizon; (5) talk to someone else in between
  leaves the request joinable; (6) speech that ended an answer being prepared is never then left unanswered (reason `interruption`).
  The browser's pre-roll (`pre_roll_s`, 0-2, the sound kept from before the speech began, `webui/audio.js` `PRE_ROLL_SECONDS`) is not speech:
  `Continuation.plan` measures the gap and the fragment's length without it and adds only the rest of the gap as silence.
  Short commands are DECIDED on their own: while something is pending, a fragment of at most 2.5 s is classified on its own first, and one that is
  confidently an explicit request (speak, explicit, 0.9 or more, addressed) is decided by that reading; the conversation model still hears it joined to
  what came before ("stop" or "make it three" makes sense only after it). Any other short fragment is then classified joined, one extra classifier call.
  `first_clause_speech` (`helpers/convo_firstclause.py`, the fork's pacing, default on): the first phrase is the first full sentences of at least `FIRST_WORDS` (6) words (a short "Good evening, sir." alone was voiced higher and brighter, measured 2026-11-09; never a comma or a
  length cut: "Could you please clarify what you would" was heard as a pause mid-thought), then whatever waits is merged into one request of at most
  `MERGE_LIMIT` (420) characters cut after a full sentence; off, the whole reply once it has ended (one generation for a reply up to
  420 characters: the parity candidate; the default stays on until measured). Every reply uses the one ordered bounded worker, so
  reading the model never waits for a synthesis; order, per-phrase receipts, the SpeechBusy wait and the two-second cancel are unchanged.
  `recent_remarks_seconds` (`helpers/convo_remarks.py`): off by default; the audio of recently ignored completed utterances is kept in memory (at most 20 s
  in total, each forgotten after the setting) and attached once, as labelled overheard context, to the next admitted turn's conversation request only
  (`/answer` `context_audio`); it sends overheard audio to the conversation model and must be described that way. A delegated task is queued only
  after a 0.6 s quiet hold (`ACTION_HOLD`) so an immediate correction can replace it; the confirmation says "queued", never "done".
  The turn engine builds its system text only in `Realtime._system_prompt`: `convo_voice_style.voice_rules` (answer length, spoken updates, read from the saved
  settings at every response, the session copy if that read fails), the bounded persona block, then the listen/overheard lines and a bounded relevant-context block
  (`convo_context`, this target only, never the request being answered or a superseded turn). The persona is checked at a response boundary (at most every
  PERSONA_REFRESH) entirely in the background and used from the next response, never blocking or failing a turn. A delegated task carries its bounded context and is re-checked against the
  16000-character limit afterwards. A spoken job update is gated by `convo_task_updates` and a text-only one is still settled.
- Behaviour tests (`tests/behavior_harness.py`, `test_behavior_corpus.py`, `test_classifier_acceptance.py`) drive the real handler on a virtual clock with scripted providers. Baseline tests pin known gaps; `@expectedFailure` targets are named after their backend work package and are removed together with that fix. Classifier acceptance registers stub backends through `register_backend` and needs the staged System One SDK (it skips without it). These tests never replace live or listening acceptance.
- Every reply marks its end-of-answer figures: `reply.done` {chars, finish_reason (last pass), tool_calls and reasoning_chars (all
  passes), passes, prompt_tokens, cached_tokens (when the server reports them)}; `ctx.estimate` keeps `ctx_left_out` and `ctx_ratio`.
- Timing marks (`helpers/convo_metrics.py`) are bounded, typed, content-free and observational: they use the running loop's clock, drop unknown fields, and can never raise into the voice path. The `metrics` action is developer-mode only. Browser marks (`timingReport()`) use the same discipline. A measurement is never a performance claim until a live run produced it. The service also keeps every mark (with wall time; ignore reasons ride on `policy.gate`) across restarts in `timing_marks` beside the journal (`Store.keep_mark` -> the one `MarkWriter` thread, its own connection, a bounded queue that drops instead of waiting, newest `MARKS_KEPT` rows; `Store.marks()` reads them). No audio, words or credentials, ever.
- Nothing may hold up a reply (the voice loop is one thread shared with Agent Zero's other websocket handlers):
  - Whisper runs in Convo's own worker process (`helpers/convo_whisper.py` `Manager`, `helpers/convo_whisper_worker.py`): Agent Zero's
    Python (`-I -u`), started only while a voice session keeps transcripts (`transcription_enabled`) or a text-only conversation model's
    turn asks for its words (`convo.whisper`, which starts the session's watcher), and stopped when the conversation ends, transcripts
    are turned off (never for a text-only conversation model while its session lives) or the plugin is turned OFF; the watcher and a
    pending `_whisper_after` belong to the conversation, not the connection, so a re-attach keeps the loaded worker and its transcript; at most two CPU threads, `nice` 10, one job running and one waiting (the newest wins,
    the replaced one is `dropped`), `loading` answered at once. It loads only an existing, complete model file (size stable for 1 s,
    sha256 from openai-whisper's own list) by its path, so it never downloads; otherwise it is `unavailable` (said once per session as
    a notice). WAV decoded in memory, language auto-detected (the host's dictation language stays as set). A segment Whisper itself
    scores as more likely non-speech than `NO_SPEECH` (.6, its own default) is left out of the text (`left_out` counts them): Whisper
    writes words for noise while scoring them as non-speech. Start, stop and every request run off the loop.
  - The server asks for a replied-to audio turn's words after its phrases were acknowledged (`WHISPER_PLAYBACK_WAIT` bound) and sends
    `transcript.saved`; a text-only conversation model's turn asks first (`convo.whisper`, waits at most `WHISPER_TEXT_WAIT`). Only when
    the worker's Python has no Whisper does the server send `transcript.wanted` (with the turn's audio) for the browser to ask Agent
    Zero's own Whisper, and never while the host loads a model or the selected model's file is missing (`convo_whisper.host_state`).
  - Settings are read through one `convo_service.SettingsCache` per session: again only when the settings file's stat or the plugin
    toggle changed, or after 30 s; a failed read keeps the session copy (`Realtime._settings`). The managed gateway readiness wait at
    start runs in a thread. The session check before every audio chunk reads `Store.session`'s in-memory copy, cleared inside every
    session-changing transaction under the store lock (so OFF is seen by the next chunk; a rollback is simply read again). The store
    connection uses WAL with `synchronous=NORMAL`; turns, jobs and notices still write (and wait) on it, only timing marks go to the
    writer thread.
  - Tests: `tests/test_nothing_holds_a_reply.py` (stand-in Whisper and worker, virtual clock). Not proved live: real Whisper load
    and transcription times, memory per model, priority/thread caps inside the container, the browser fallback against a real host.
- `tests/live-ui-smoke.mjs` checks the real interface on an isolated loopback test host with a throwaway headless browser (private profile, muted, no installs; `tests/live-browser.mjs` refuses any non-loopback address). It starts no voice session and saves nothing. Run it after every deploy to the test host: it found a page freeze (the orb fighting the host's tooltip observer) that every simulated test missed.
- `tests/update-persistent.ps1 -Plugin convo_d1_decider` deploys the sibling development plugin `plugins/convo_d1_decider` (see its `AGENTS.md`): the decision-model backend that only Convo uses, installed beside Convo with the same preview, backup and rollback.
- `tests/update-persistent.ps1 -WebOnly` lets a running (ON) plugin receive browser assets, tests and top-level documents only; anything that could change a loaded module still needs Convo OFF.
- Acoustic replay (`tests/signals.mjs`, `replay.test.mjs`) drives the real `SpeechBoundary` with deterministic synthetic audio. Baseline tests pin today's behaviour, including known defects; `todo` tests state what a named work package will add. With `guard: 'adaptive'|'strict'` and a `reference` it drives the real `EchoGuard` the way the guard worklet does; `node tests/echo-measure.mjs` prints the false-start / detection-delay / duck-toggle tables. Synthetic signals say nothing about real rooms, voices or microphones.
- Browser echo protection is the shared setting `echo_guard` (adaptive|strict|off, default adaptive; read when a session starts, so a change applies to the next start). `adaptive`/`strict` capture through `webui/audio-worklet-guard.js` + `webui/echo-guard.js` (the user's standalone Adaptive v3, adapted); `off` loads the untouched `webui/audio-worklet.js`. The guard compares the microphone with the exact post-duck playback output and releases only original microphone samples after 450 ms of confirmed independent speech; echo and ambiguous frames are withheld and reach the boundary only as silence, or, once the user's speech over the reply is confirmed, as floor-level comfort noise (never voiced, never microphone audio), with non-echo frames within 250 ms of the user's voice kept as original audio. Predictor residuals are never uploaded. See `webui/AGENTS.md`.
- Reuse validation must retain command identity, read-only source mounts, private namespaces, no added capabilities/devices/ports, bounded scratch/resources/logs and explicit offline flags. Test the validator without contacting Docker via `tests/image-policy.test.ps1`.
- Keep independent Plugin Doctor available in development while Convo is OFF. A broken shared process can require an approved same-image process restart; do not default to image rollback.
- Conversational controls are two separate shared settings, `response_detail` (concise|balanced|detailed, default balanced) and `spoken_updates` (completion|milestones|frequent, default completion = today's behaviour: a delegated job is spoken about only when it ends). `helpers/convo_voice_style.py` builds the rule text (base rules unchanged, plus detail, task-update and the "explicit per-request preference overrides for that answer only" rule); `helpers/convo_task_updates.py` decides spoken versus text-only per job event. Permission, error and decision events are spoken at every level; queued/unknown events never. Boundaries: both settings are read from the current saved settings at the start of each response; the inherited persona and `style` at session start/retarget. They never change personality, permissions or what a job may do.
- The voice persona is inherited, not duplicated: extracted by the host utility model from the selected agent's system prompt (identity, tone, conventions only; the whole prompt is sent as JSON data), capped at 1600 chars, cached by fingerprint of [prompt, style] and shown to the voice model only as JSON data after the rules (`convo_voice_style.persona_data`). A changed host prompt is seen mid-session only through the background check the turn engine starts at a response boundary (`Host.persona_cached`, then `persona` on a miss); never wait on the prompt build or the utility model inside a response. Every prompt build (the chat's own profile and a pinned one) runs on a throwaway agent on Convo's persona thread (`Host._own_prompt`/`_profile_prompt`), never on the voice loop. The tests script the utility model: they do not prove its live extraction quality.
- Turn and delegation context is bounded and relevant, never the whole history (`helpers/convo_context.py`): at most 6 snippets, 1500 rendered characters, 280 per snippet, secrets redacted before cutting, drawn only from the selected target. A job keeps the target it was submitted for (`jobs.target`); retargeting never moves outstanding work.
- Readiness always reports `incremental_input` as `completed_clip`/`unsupported` ("completed turns only; audio input is not incremental or duplex input"). A declared capability flag is unread by the turn engine and can at most read `unverified`. No interface text may imply streaming input.
- Provider, classifier, decision-source and drafting settings are additive and outside `capabilities{}`: `voice_message_draft`, `roles.conversation.{provider,key_source,api_base}`, `roles.classifier.{provider,key_source,api_base,context_window,kind}`, `roles.tts.{provider,connection,key_source,provider_saved}`, `decider.{source,kind,url,model}` (`decider.url`: a server root, no path, under the classifier's locality rule). Empty/default values keep the previous behaviour; `roles.conversation.url`, `roles.classifier.url`, `system_one`, `decider.*` and `roles.tts.backend` stay the runtime protocol, and derived fields are written only by the catalog normalizers inside `setup_store.normalize`, which every writer stores. `tests/test_rollback_compat.py` proves the previous contracts (frozen verbatim in `tests/fixtures/contract_v0_1` and `contract_v0_2`, never edited) still load and save new settings; freeze a copy again before the next format change. Rollback safety is why the classifier normalizer mirrors a decision server root into `decider.url` only in the previous rule's form (plain http on this machine, no path) and otherwise writes '' and derives the root at call time (`convo_classifier_catalog.decision_root`).
- `setup_patch` writes a few settings now: compare-and-set per path against the base the editor loaded (absent base = absent path, absent value = removal), validated like a full save, credentials refused, AI Link admission kept, write epoch advanced, nothing written when nothing applied; paths are known top-level settings (and known roles). Key sources, first match wins: `token_env` (authoritative even when empty), Agent Zero's own key for the role's provider (`models.get_api_key`; `''` and `'None'` are unset; only `key_source: provider` with a catalog provider that names a host key), then Convo's URL-bound key (the classifier's is bound to `convo_classifier_catalog.key_address`: the decision server root while Convo's own decision classifier is in use, else `roles.classifier.url`; never an empty address). No key value ever reaches a response; the decision plugin gets the classifier's only in-process (`convo_setup.classifier_bearer`).
- Connection rules, for every model connection (conversation model, classifier, decision server, speech, gateway): `convo_contract.local_host` is the one locality rule. This machine, Convo's containers and your network are used as they are (http or https, no consent): loopback, RFC 1918 and IPv6 unique local (not link-local), Tailscale's 100.64.0.0/10, one-label machine names and the endings `.local`, `.lan`, `.home.arpa`, `.internal`, `.localhost`, `.ts.net` (a `*.ts.net` name is taken to be in your tailnet); judged as written, never looked up. Anything else needs the role's remote consent and HTTPS. Its copies (the decision plugin's `local_host`, `webui/connection-rules.js`) are held to `tests/fixtures/connection-rules-cases.json`. A credential environment reference (`token_env`) names a variable (`[A-Za-z0-9_]{1,128}`): save and `setup_patch` refuse a changed one that does not, labelled by field and never echoing the value. One saved earlier never fails a settings read or blocks unrelated saves: `read()` reports it as a `missing` readiness row carrying its `path`, Test connection says so without contacting anything, and voice start refuses with the same reason. Test connection names the failure in Convo's own words (reference, address rule, connection refused, timeout, HTTP 401/403/404/other, an unreadable list); no server text is echoed.
- Reaching a server (`helpers/convo_net.py`) applies to every role connection: setup discovery and Test, speech discovery, Check server, the audio probe, the gateway's model, classifier and speech calls, and the decision plugin. It never applies to Convo's own managed gateway, Studio or managed-runtime clients (`convo_transport`, `studio_proxy`, `managed_runtime`).
  - **This computer:** when Agent Zero runs in Docker (`/.dockerenv`, and `host.docker.internal` resolves), a loopback address is connected through `host.docker.internal`. The typed address stays the identity: Host header, TLS name, saved value, key binding and evidence. Only the address's form and where Agent Zero runs decide; nothing per machine is stored. `host.docker.internal` and every other address are used exactly as typed.
  - **Private certificates:** an https server on your own network (`local_host`) whose certificate the bundle does not trust is remembered on first contact in `data/tls-pins.json`, per origin as typed (PEM and fingerprint; nothing secret). From then on only that certificate is accepted (the only trust anchor, no name check). A different certificate raises `CertificateChanged` until `tls_accept`; `tls_forget` removes the entry. A public server is never remembered.
  - **Failures** are described in Convo's words (`describe`): refused (with the Docker published-port hint for this computer), unknown name, timeout, certificate changed or untrusted. After a refused connection, Test offers one address that answers instead (`suggestion`, one TCP connect each, nothing sent): the provider's default port on this computer, or this computer's private address for a LAN or Tailscale address.
  - **Keys:** Test connection and Check server use a key typed but not saved (`draft_key`, that request only, never stored or returned; a `token_env` still wins). `key_rebind` moves Convo's saved key to the card's address only on click.
  - **Reads:** `setup_read` reports `network`, `certificates` and `key_bound` (addresses only, never a key).
  - **Model search:** on a card whose address is on your network, model-name search uses Convo's discovery: that card's key and rules, and every name the server lists, audio models included. The list is fetched once per address and key and reused for 60 s while typing. A cloud provider keeps Agent Zero's search, and falls back to discovery when that finds nothing.
  - **Tests:** `test_convo_net.py` (real local HTTPS with throwaway certificates), `test_connection_rules.py`, `model-type-ui.test.mjs`.
- Convo's runtime (its gateway, the service that calls the models during a conversation):
  - **Default and placement:** it is Convo's own by default (`sidecar.managed` true). It is set on its own Connections card (`webui/runtime-field.html`), never behind another choice.
  - **How it runs:** it runs on Convo's environment when the one-time Studio setup made one (`data/runtime-env`), else on Agent Zero's own Python. The gateway needs only Starlette, uvicorn and httpx: `sidecar/web.py` is the small part of FastAPI it uses, rebuilt on Starlette. So it starts with Agent Zero whenever Convo is on, with nothing to install, and `require_ready` brings it up when a voice start finds it stopped (waiting up to 30 s).
  - **Hidden external fields:** with it on, `setup_store.normalize` resets the external gateway's remote consent and any public address, so those hidden fields never block a save.
- The model servers a conversation needs (`Realtime._servers`: conversation model, Convo's own classifier, an external speech server; `helpers/convo_models.py`):
  - **Told apart:** `state()` reads loaded, loading, not loaded, not answering or unknown from whatever the server offers: a router's `GET /models` `status.value`, a Voice Studio speech server's `/backend/models`, Ollama's `/api/ps`, LM Studio's `/api/v0/models`, a 503 from a llama.cpp server still loading. Only servers on this machine or the user's network are asked; a cloud provider is left alone. `load()` sends each kind's own load signal. Nothing is ever unloaded or stopped.
  - **At session start** all of them are checked side by side. A model that is not loaded is asked to load and waited for (up to 180 s), a server that does not answer is tried for `ANSWER_WAIT` seconds and then named in the refusal. The page shows `notice` events (`kind: loading`, then `ready`). A server that reports no state is not waited for.
  - **During a conversation** the step that gives nothing is looked at (`_stalled`: the conversation model after `SLOW_REPLY`, the voice after `SLOW_VOICE` or a failed phrase, the classifier when a call failed) and the user is told which server it is and whether its model is loading (asked to load when it was not) or it is not answering (`notice` `loading` / `unavailable`; `ready` when that step answers again). It never ends a turn and never switches to another server.
  - **Two models, one server:** when the conversation model and the classifier share one server, the user is told they may swap on every turn.
  - **Slow replies:** a reply with no text after 4 s says it is waiting for the model (above). The gateway allows 180 s for the first token.
  - **Failures:** the gateway says why a model call failed in Convo's words (refused key, 404, unreachable), and that text is shown as said (`ProviderError`).
- When Agent Zero's utility model fails to summarise the persona, the session still starts with the user's own persona text, and a `notice` says so. A voice start checks only the credential references of the connections in effect.
- Ownership seams: `helpers/convo_model_catalog.py` (conversation providers, base URLs, `conversation_key_copy`/`audio_probe`), `helpers/convo_classifier_catalog.py` (classifier catalog, normalizer, `classifier_check`), `helpers/speech_providers.py` (`PROVIDERS` catalog, `backend_for`, `effective_provider`, `validate_provider_saved`), `helpers/speech_discovery.py` (speech discovery), `convo_system_one.decider_rows`/`decider_check`. `api/conversation.py` delegates those actions (a refusal is typed data with no side effect) and readiness appends each catalog's rows (a failing hook is one `unavailable` row, never a failed read). Every timing mark has a log-wide `seq`; `metrics` with `cursor` returns only newer marks (at most 1000, `more`, `reset` when the log restarted).
- Compaction threshold (`compaction.trigger`, 0.3 to 0.9, default 0.7; the conversation card's "Summarise older conversation at"): how full the window gets before older turns are summarised. `convo_memory.Budget` scales the word-for-word bands with it (0.7 gives the measured sizes) and never lets them take the room of the answer, the audio and the fixed rules; `convo_memory.threshold` clamps any stored value and a non-number is the default, so a budget is never refused at run time. The contract refuses a value outside the range on save with a readable reason. `compaction.target` is no longer used on its own: `setup_store._budget` writes it a fifth below the threshold so the contract's `target < trigger` rule (and an older Convo) accepts every threshold. When the model's server refuses a request as too large (`_ContextFull`), the turn is asked once more with fewer turns as before, and `Realtime._lower_window` lowers the window in effect by a fifth for the rest of that conversation (`ctx_cap`, never under 4096), so later turns and summaries are sized to what the server takes.
- Showing a saved key (`convo_setup.key_reveal`, action `key_reveal`, the eye on a key field): the one request that returns a Convo-kept key, for one card, on an explicit click. With an Agent Zero login set (`helpers.login.is_login_required`) the whole key; without one only its last four characters (nothing for a key under 12 characters), because then anyone who reaches the page could ask. Never an environment-variable key or Agent Zero's own keys. No other response, log line or journal entry carries a key, and `setup_read`/`setup_save` are unchanged (write-only).
- Speech features learned from refusals (`Realtime._lighter_speech` / `_plain_speech`): a speech request the server refuses before any audio (a 4xx other than 401/403/409/429; also 501/502 for a streamed request) is asked once more without streamed playback, then without delivery instructions. What made it work is kept in the session (`state['speech_plain']`), so the following phrases are asked the plain way; a new conversation asks the full way once again. Nothing is written to the settings; the two declarations stay in the speech card's Advanced Settings until the positive case (turning them on by itself) is measured against a real server.
- What a reply left open (`convo_voice_tools.left_open`, read once per reply that called no tool, in a conversation with tools, after the reply's words were handed to speech): an isolated one-word request to the conversation model about the assistant's own sentence (`SAID`; no history, no tools, 4 s, never raises; skipped on the user's own single-slot llama.cpp server so its cached start is kept). `promise`: the same request is made once more with `PROMISED` at its end (act now with a tool, or say plainly it cannot and hand over; never read a second time in that reply). `offer`: `state['offer']` is kept for `OFFER_OPEN` seconds, and a finished turn of at most `OFFER_REPLY_SECONDS` of speech that the classifier took for an acknowledgement or for nobody is admitted (`reason: 'offer'`), so a short "do it" reaches the conversation model, which sees the offer next to it (`_last_said`). Convo's own readings (catalog `own`: the system check, the clock) do not close a reply to actions (`followup` counts only tools that return someone else's text), and are not `untrusted` history.
- Places the open tool knows beyond Agent Zero's own (`convo_voice_tools.SETTINGS_TABS`, `CLOSABLE`; `_named` reads the model's wording): the voice settings and their tabs, the sound controls, Voice Studio, the diagnostics, and `close` for each. The page carries each out the way its own button does (convo-store.js "The voice agent's own places"); unsaved settings are never closed by voice.
- A reply cut off by the assistant's own voice carries on (`Realtime._cut_reply`, `RESUMED`, `RESUME_WORDS`): when the words of a turn are Convo's own speech heard back (`_own_voice`) and the reply before it was interrupted with something heard, the model is asked for the rest (no audio, no tools; told what was heard, and not to repeat it or mention the interruption). The turn's user words become the neutral `RESUME_WORDS`, never the echo. With nothing left to say, or no reply cut off, the turn stays silent as before.
- A finished task's result is announced by the server (`Realtime._announce_result`, run by the timer watcher in a quiet moment: nothing prepared, said, heard or being spoken by the user). `_results_seen` notes when each result arrived; one that waited `RESULT_PATIENCE` seconds while the user was speaking until `RESULT_RECENT` seconds ago is an interjection (`INTERJECT`: a short lead-in, the result, the floor handed back). Every notice is written with the recent conversation in view (`_notice_context`, reference only). The page's own rule (four quiet seconds, `convo.notice`) stays as a fallback; `claim_notice` makes either one the only announcement. The page sends `convo.speaking` (no audio, no words) the moment speech begins, and `_speaking` counts it for `SPEECH_START_HOLD` seconds.
- An occasional acknowledgement (`COURTESY*`, `Realtime._courtesy_due`): a finished turn of at most `COURTESY_SECONDS` of speech that the classifier took for an acknowledgement is admitted (`reason: 'courtesy'`) when the last thing the user heard was a reply of at least `COURTESY_WORDS` words, or a finished task's result, to its end, and the last such opportunity was `COURTESY_PAUSE` seconds ago and not the turn before. The reply's late part then carries `COURTESY`: the model may say a few words in character or write [SILENT]. Otherwise an acknowledgement stays unanswered as before.
- Settings by voice (`change_setting`, kind `work`: the user's own request, or their yes; `convo_voice_tools._setting`): a fixed subset and nothing else. For this conversation, done by the page (`ui.setting` → `convo.ui_result`): `microphone`, `voice` (the assistant's voice silent: `store.setVoiceMuted`, a gain after everything that listens to the output, so heard-tracking is unchanged) and `camera`. Saved through `convo_setup.patch` with the session's value as its base, and followed by this conversation at once: `answer_length` (`response_detail`), `spoken_updates`, `developer_mode`, `diagnostic_events`, `rejected_replies`. Never by voice: what the voice agent may do, consents, keys, addresses, providers, models, the speech gate.
- Chats follow you: in a chat's own conversation the page sends `convo.work` when another chat is opened (as it always did across chats). The conversation, its memory and its audio stay with the chat it started in; `Realtime._work` then names the open chat, `convo_voice_tools.place` hands tasks to it, and the reply is told so for that turn. There is no "Use selected chat" button any more (`canRetarget` is always false; `convo.retarget` still exists). Settings → Advanced has one switch, "Keep one voice conversation for all chats" (`conversation_scope`), and shows the task place (`task_chat`) only with it on.
- Remote consent has no switch in the page: `remote` stays a stored, server-enforced flag (and what an older Convo reads), written by the page from the address the user chose (webui/AGENTS.md, "Consent without a switch"). The server never infers it; settings that arrive another way (import, patch) are still refused without it.
- Classifier (`roles.classifier`, `decider.*`; user-facing name "Classifier", System One's own is "System One's decider"): `helpers/convo_classifier_catalog.py` is the only catalog (setup_read `classifier_catalog`; the browser keeps no copy): local decision servers (llama.cpp verified; LM Studio, Unsloth Studio, Ollama, vLLM, oMLX not verified until Check server), TypeSafe Jev (decision, text only), OpenAI/OpenRouter/Google (chat), Other; the conversation catalog's disabled providers; an unknown id the browser offered as OpenAI-compatible is a chat classifier with its API base. `kind` auto = the provider's first kind. The normalizer derives the runtime: toggle "Use System One's decider" on (`decider.source` `system_one`) = `system_one` d1_llama_cpp; off with a decision model = `decider.source` `convo`, `decider.model`, `decider.url` (rollback-safe form only) and `system_one` d1_llama_cpp, the decision plugin (`plugins/convo_d1_decider`, see its `AGENTS.md`) resolving `decision_root` for every call; off with a chat model = `system_one` off and `roles.classifier.url`; a provider/kind Convo cannot use = off with no address; provider '' = nothing derived. `migration_view` is read-time only. Locality (one rule in `convo_contract.classifier_url`, `convo_system_one.url_refusal` and the plugin's `allowed_url`, held to one table): this machine and your network (`local_host`: LAN, Tailscale) as they are; a public address only with `roles.classifier.remote` and HTTPS; readiness then reports `remote`, so ambient also needs `remote_ambient_consent`. Readiness refuses start for an unusable System One decider (today's kinds decide from text), a decision classifier without an allowed address, and a text-only provider in an audio conversation. `decider_rows`/`readiness_rows` never contact anything. `classifier_check` (`decider_check` = the previous `{url}` request, same check) is explicit and bounded: GET `<base>/models`, for a decision model `<root>/health`, `<root>/v1/models` and ONE tiny text decision POST `<root>/v1/systemone` (no audio), no redirects, the key attached server-side and never returned, no server text echoed; a public address without consent is refused before anything is sent.

## The chat voice is used in

Agent Zero writes a chat to disk only when its own agent has worked in it (`message_loop_end`), so a chat used by voice alone was gone
after the next restart (live 2026-11-25). `Host.keep`, called in the background for every accepted turn and when voice ends
(`_keep_chat`), saves it with Agent Zero's own writer, names an unnamed chat after the first answered words
(`Store.first_words`, unless Agent Zero's automatic naming is off; its "Chat #N" number changes at a restart) and records when it was last
used. It never saves the hidden context of a conversation across chats, never a chat whose agent is working (Agent Zero saves that one),
never writes back a chat the user removed, and a failure never reaches a reply.

## Saved turn (replay format)

`data/replay/<YYYYMMDD-HHMMSS>-<8 hex>.json`, UTF-8 JSON, written only by "Save for replay" (`convo_inspect.save`), for a replay
runner outside the plugin (the test rig) to read:

- `format`: `convo-replay/1`; `id`, `saved_at`, `captured_at` (Unix seconds), `turn` (the turn id).
- `passes`: every request of the turn, in order. Each: `kind` (`first` | `again` | `tool follow-up` | `early`), `at`, and
  - `request`: exactly what Convo sent the gateway's `/answer`: `role` {provider, url, model, api_base, capabilities, context_window},
    `system_stable` and `system_late` (the system text split at `LATE_MARK`; the gateway sends the late part at the front of the latest
    message, after the history), `history` (the turn contents as built, earlier turns' in-memory `user_audio` base64 WAV included),
    `audio` (this turn's 16 kHz mono PCM16 WAV, base64, or null), `audio_s`, `transcript`, `context_audio` (overheard clips), `tools`,
    `tool_mode`, `allow_tools`, `max_tokens`, `recall`, `tool_turn` (a follow-up's call and result; a camera picture is replaced by
    `image_omitted: true`);
  - `raw` (the model's text before any filter), `tool_proposals` [{name, arguments}], `done` {finish_reason, reasoning_chars,
    tool_calls, prompt_tokens, cached_tokens}, `error`, `removed` {header, markup, control}, `filters` {user_line, language, markup,
    control, control_reason}, `after_filters` (what was shown and spoken from it).
- `spoken` (phrases sent to speech), `whisper` (Whisper's text, '' if none came), `tools` [{name, ok, result}].

## Open verdicts

Behaviours to attribute (model, Convo or both) with the replay runner before anything is "fixed" (plan change 1):

- Web search not called; "What about Kansas?"; "Write a joke."; `[SILENT]` on follow-ups.
- 2026-11-17, the user live: a repeated "okay" was answered twice (the instruction says never to reply to a repeated acknowledgement).
- 2026-11-17, the user live: background noise was sometimes answered (the conversation rule admits it; `[SILENT]` is the model's choice).

## Ownership index

- `helpers/`: configuration, migration, host adapters, SQLite journal, policy, transport and utility compaction.
- `api/`: host-authenticated voice and control entrypoints; endpoints and credentials are server-owned.
- `webui/`: native controls, shared settings and expanded Studio; see `webui/AGENTS.md`. `extensions/`: provider compatibility and explicit activation.
- `sidecar/`: authenticated model adapters and optional managed headless audio.cpp; see `sidecar/AGENTS.md`.
- `tests/`: temporary-storage, mocked-provider checks; no installed settings, models or services.

## Verification

- Dated result logs are not kept in the repository: the contract here, `README.md` and `ACCEPTANCE.md` describe the current state.
  When reporting, distinguish deployed source, actual native lifecycle/UI, model provisioning and physical listening acceptance.
- Run `python -m unittest discover -s plugins/_convo/tests -v` from the repository root.
- Sidecar route tests (`test_runtime`, `test_speech_admission`, `test_speech_stream_route`, `test_studio_outcome`, `test_policy_disconnect`, `test_gateway`) need Starlette and httpx. Run them in an existing environment that has them, never by installing into another. Set `CONVO_SYSTEM_ONE_TEST_SDK` (and `SYSTEM_ONE_TEST_CORE`) to a folder holding System One's `media_decision.py` so the classifier-acceptance and decider tests run instead of skipping.
- Run `git diff --check` and parse changed Python/JavaScript files.
- Run both `node --test plugins/_convo/tests/audio.test.mjs` and `node --experimental-vm-modules --test plugins/_convo/tests/ui.test.mjs`.
- Actual image smoke checks exercise native HTTP auth/CSRF and WebSocket admission, disabled-target diagnostics, inert OFF hooks, all speech-adapter restoration and byte-preserved native configuration. They never start the application server.
- Live model, browser and container acceptance is required before release promotion.
- The composer orb has three press outcomes (`webui/dictation.js`, constants `LATCH_MS` 2000 and `PUSH_TO_TALK_MS` 4000, provisional): a release before the first is a tap (toggle Convo); past it the host's normal hands-free dictation starts (the same recording, held without silence segmentation until release, then the host recorder's own voice-activity thresholds and waiting timeout; each utterance goes through the host's `sendVoiceMessage`, so `message_mode` draft leaves one utterance in the composer and the host stops, send sends each and keeps listening; Convo follows a host-ended dictation); past the second it is push-to-talk (listens only while held). One capture only: starting dictation closes a live Convo voice session first and never resumes it; a press while latched finishes it. Interruption (pointercancel, blur, hidden page, Escape) discards and never sends. The transcript goes to the host exactly as its own voice button hands it over (`sendVoiceMessage`), so the host's voice-message handling applies and Convo adds no setting. Past `PRE_OPEN_MS` (300) the one host recorder opens as a silent buffer (never beside a live Convo session), so the words spoken during the press are part of the dictation that follows; a release, Escape, pointercancel, blur or hidden page before `LATCH_MS` discards it and closes the stream before the tap acts. Not captured: speech before `PRE_OPEN_MS`, and the first seconds when a Convo session was live at press time. The browser's microphone indicator shows during any mouse or key press longer than `PRE_OPEN_MS`, even one that ends as a tap; a touch press never pre-opens (a phone moves its sound between call and media streams with every microphone), so touch dictation starts at `LATCH_MS`.
- Draft in composer (continuous) is the one dictation setting Convo adds (supersedes "Convo adds no setting" above): Agent Zero's Whisper keeps only send|draft, so it is host `message_mode` draft plus Convo's `voice_message_draft` continuous. `api/stt_config.py` maps a requested `continuous` to host draft + flag `continuous` (send/draft: `single`) in ONE `setup_store` transaction on Convo's file, which advances its revision only when the flag changes; `helpers/config.py` maps the host's generic save route the same way and puts a host-written `continuous` back to draft. `normalize_whisper_config` still accepts only send|draft; no host file or host-store `config` carries `continuous` (`stt_status`/`stt_config` report `voice_message_draft` beside `config`). The recorder's `deliverTranscript` decides per phrase (`dictation.js continuousDraft`: host draft, flag continuous, no host `finalTranscriptHandler`): append with the host's `updateChatInput` and keep listening, else the host's `deliverVoiceMessage` (a handler is honoured) or the older send path. Stops: an orb tap/press or push-to-talk release (the phrase in progress is added once), a host mic click (host `stop()`), Escape (added text stays; the phrase being recorded or transcribed is dropped), a manual send (`send_message_before` -> `composerSent()`; a phrase still being transcribed lands after the send has emptied the composer, waiting at most 3 s), speech playback starting (the host stops), Convo off (orb: `enabled` false or status lost; host mic: status lost). Orb and host mic alike, one stream, the 2 s / 4 s gestures and the held-silence rule unchanged. Dictation start re-reads both values from `stt_status`, never through the host's `refreshStatus` (a failed read there stops the microphone). Tests: `dictation-host.test.mjs` (live v2.11 delivery stand-in), `dictation-card.test.mjs`, `legacy-whisper-mode.test.mjs` (verbatim `fixtures/host-whisper-config.html`), `test_stt_bridge.py`; live: `tests/live-dictation-smoke.mjs` (fake microphone, transcription answered by the script).
- Developer diagnostics (`webui/diagnostics-store.js`, pure `diagnostics-model.js`, `floating-window.js`, `diagnostics.html`, wrapper `extensions/webui/right-canvas-panels/convo-diagnostics.html`) is a second canvas surface, `convo-diagnostics`, registered by the canvas extension only while the saved `developer_mode` is on (a remembered last-seen value registers it before the first settings read so a reload can restore it; that read confirms or removes it). It has no `modalPath`: Convo's own window is the only pop-out. The host has no unregister, so turning the mode off closes it, splices it out of `rightCanvas.surfaces`, unmounts it and closes the window; the host registry keeps a copy, so `canOpen` refuses. Observational only: it reads `metrics` with the cursor only while visible (1 s with a voice session, 4 s without, never in a hidden tab), takes the echo tap (60 readings) and paints levels only then. Marks add `policy.gate` mode, answer floor and all probabilities (calibrated only), the `policy.retry` reason and `ctx.estimate` (`ctx_used`/`ctx_window`, from `_check_capacity` before the check); `report()` returns `now` (the marks' clock) for the page's clock mapping. Per turn, probe marks (`final=False`) are capped at `PROBE_MARKS`, turn-ending marks (`TURN_END`) keep `END_MARKS` of room above `MARKS`, and only the newest `playback.completed` is kept. Tests: `diagnostics-model`, `floating-window`, `diagnostics-store` (verbatim v2.11 `fixtures/host-right-canvas-store.js`, `host-surfaces.js`), `canvas-ui` (`fixtures/host-right-canvas.html`), `test_metrics.py`; live: `tests/live-diagnostics-smoke.mjs`.
- Speech output is a provider choice, not a model type. `speech_providers.PROVIDERS` (mirrored in `webui/speech-catalog.js`) offers Kokoro, audio.cpp (connection `external`, the user's own server, or `managed`, Convo's runtime), llama.cpp, Unsloth Studio, OpenAI and an OpenAI-compatible endpoint; Strata is not one. `roles.tts.provider` (+ `connection`) is stored and `speech_providers.normalize` derives `backend` (still the runtime protocol), the connection, an empty URL's documented default (none for Unsloth Studio), no `native_pcm` where there is no audio.cpp PCM stream, and `capabilities.instruction_control`, true exactly where delivery instructions are honoured (OpenAI gpt-4o-mini-tts and its dated snapshots; audio.cpp external, llama.cpp, Unsloth Studio and the generic endpoint only when `roles.tts.accepts_instructions` declares it and neither a Base model nor a `clone:` voice is chosen; never Kokoro or managed audio.cpp). The gateway refuses instructions without that flag, so unusable text waits in `provider_saved` and comes back when a model honours it. `webui/speech-catalog.js` `normalizeTts` must give the same result: both run `tests/fixtures/speech-normalize-cases.json`. Switching (`switchProvider`) stashes per provider in `provider_saved` (audio.cpp per connection; an earlier flat record is still read) and never writes the legacy `saved`, which is only read for what an older configuration's backend meant. An older configuration's provider comes from its backend alone; an `openai` backend on port 8890 of this machine is offered as audio.cpp external and changes only when the user confirms.
- Speech discovery (`helpers/speech_discovery.py`) sends read-only GETs from `READ_ROUTES` only, through its own bounded `_probe` (it must report statuses), and returns what each route answered (`checks`). The user's audio.cpp server is never sent a switch, load or unload: its loaded model and current tuning selection are shown as it reports them (`server`), and a model it has not loaded is `unverified`. A speech route is recognised by a read-only GET answer (405 for a POST-only route); llama.cpp and Unsloth Studio stay "not verified" until theirs answers. OpenAI is asked only `GET /models`, never without a key or consent; managed audio.cpp is not contacted while its runtime is off. "ready" never means speech was produced. Live check: `tests/live-speech-smoke.mjs` (writes blocked in the page, draft discarded, container state compared before and after).
- The conversation model is an Agent Zero model type, one setting for all presets: `extensions/webui/modal_content_loaded/_20_convo_model_type.js` hands the host's `{ modalPath, modal }` to `webui/model-type-mount.js`, which watches only the preset editor (`plugins/_model_config/webui/main.html`) and the modals that show a preset overview (`settings/settings.html`; the plugin Config only while it shows `_model_config`), each with an observer on that modal released at `modal-closed`. It adds one section per editor body (the shared `conversation-model-field.html` inside `x-data="{ convoAutosave: true }"`) and one "Convo — provider / model" row per `.model-preset-rows`; a scan writes only what is missing and a row repaints only when the saved choice changes (the list is a live region). In the editor each change is patched 350 ms after the last one and on close against what the editor loaded; a path changed elsewhere waits for Keep mine / Use theirs, and a value the server would refuse (remote address without consent, incomplete address, context window under 4096) waits with the reason. `convo_model_catalog.normalize` writes `roles.conversation.url` from provider + `api_base` (provider '' keeps the URL as entered; a provider Convo cannot use gets ''), so a cloud provider is stored only with remote consent. Readiness rows `conversation_provider` and `conversation_key_route` (Agent Zero's key would go to a custom API base) read no key. `key_copy` puts Convo's saved key into Agent Zero's EMPTY `API_KEY_<PROVIDER>` slot (`helpers.dotenv.save_dotenv_value`, as the host's own key store) for the saved provider only, and only when that key is bound to the saved URL; it never returns the key and Convo keeps its copy. `audio_probe` sends one 0.8 s synthetic WAV to the saved model, only on click, with keys attached server-side, never echoing the provider's error text, refused while a voice session is live. Agent Zero's key is entered through its own key store (`modelConfig.saveApiKey`) on leaving the field, never emptied or revealed from Convo. Tests: `test_model_catalog.py`, `test_setup.py`, `model-type-ui.test.mjs` (verbatim `fixtures/host-preset-editor.html`, `host-preset-overview.html`, `host-models-summary.html`); live: `tests/live-model-type-smoke.mjs` (key writes, key copy and audio test intercepted in the page; settings restored).
