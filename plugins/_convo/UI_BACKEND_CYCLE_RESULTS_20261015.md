# Convo interface and backend cycle: results, 2026-10-15

Status: implemented and verified in the isolated test host only. **Nothing in production (`agent-zero-v2.11`) was changed, and
nothing was committed or pushed.** Production promotion waits for your explicit yes.

Evidence is labelled by level; do not read one level as another:

| Level | Meaning |
|---|---|
| offline | unit/behaviour tests with scripted providers, a virtual clock and stubbed hosts |
| live-ui | the real Agent Zero page and Convo plugin in the isolated host, driven by a throwaway headless browser |
| live-pipeline | the real browser capture path, real handler, real gateway and the registered decision backend, with a scripted recording as the microphone. Sections 3-4: **stand-in** conversation and speech servers. Section 5: the real decision model and your Qwen3 audio.cpp Docker voice, with a **stand-in** conversation model (no hosted key yet) |
| listening | a person hearing it: **none yet** |

## 1. What changed

**Interface (plan Part A, P1-P8):** canvas wrapper contract fixed (the panel was `opacity:0; pointer-events:none` on the host);
history-first panel with a gear modal; composer orb with per-state glyphs and optional sound hooks; scoped settings revision,
stale-editor banner and three-way merge; backend-aware Voice & Audio (native Kokoro vs external vs managed) with read-only native
summaries; read-only System One decider reference; gateway state machine with readable states.

**Backend defects (plan BP1-BP3):**

| Item | Defect | Fix |
|---|---|---|
| BP3a | a turn cut at the maximum length could exceed what the server accepts (up to a 2048-sample block more at 44.1/48 kHz) | `SpeechBoundary` cuts on the exact sample and carries the rest of that block into the next utterance |
| BP3b | playback could stay ducked forever after a withheld/degraded final | terminal decision releases the duck (unless a newer utterance is open), a new reply is never born ducked, a safety timer releases it once the user has stopped |
| BP3c | `busy`/`stale` answers were shown as a classifier outage | typed request errors: a probe is replaced, a completed turn is re-sent for a few seconds, stale is dropped |
| BP3d | a final whose classification timed out was dropped | one more try with the same classifier after the first call let go; a native backend still finishing a call reports typed `PolicyBusy` and a completed turn waits for it (bounded); probes are never retried; no fallback |
| BP3e | barge-in during synthesis made the next reply's speech request fail (HTTP 409) | typed `SpeechBusy` from the transport (only for the sidecar's tracked `running`/`draining` states), bounded wait in the reply path, also for the experimental PCM stream; `/policy` now cancels its upstream call when the caller disconnects |
| BP3f | a classifier without audio silently turned an audio conversation into Whisper text | refused at start with the reason, flagged in readiness (`input_modality_conflict`); `text_input_required` follows the conversation model only |

**Classifier semantics (plan BP4), this cycle - details and numbers in section 5:**

| Item | Before | Now |
|---|---|---|
| `attention` in the policy envelope | dropped (only a derived speak / clarify / silent survived) | carried: the class, all five probabilities, and the explicit-request and name probabilities |
| The questions | verbose policy prose | short, plain descriptions of what the speaker is doing, with the cues a listener uses; the assistant's names go in the name and attention questions |
| What the decision model is told | a JSON document with a null observation plus mode and flags | one plain sentence, plus what the assistant last said that the user heard |
| The gate | fixed 0.85 / 0.9 on the single most likely class | measured floors on the probabilities (answer, interrupt, wave away, name; active and ambient listening); work with side effects keeps its explicit-request-at-0.9 rule |
| A classifier without probabilities | not told apart from a calibrated one | readiness row "Speech gate: unsupported", fixed thresholds kept, never mistaken for calibrated, no fallback |

**Found by live checks and fixed (none of these was visible to a simulated test):**

1. **Page freeze:** the orb re-wrote `title` and watched it; the host's tooltip observer moves `title` to `data-bs-original-title` and removes it, so the two ping-ponged in microtasks and the page stopped responding as soon as the orb engaged. The orb now writes its tooltip to the host's own attribute and never watches or re-writes `title`.
2. Every refused voice start showed "Internal server error": the host masks anything a websocket handler raises. A refused start is now returned as data and shows its reason.
3. The orb returned to "listening" while buffered speech was still playing; it now reads "speaking" for as long as audio is audible.
4. The decider reference could contradict readiness when a provisioner sources the consumer opt-in itself; it now judges by the selected backend's own gate.
5. Light mode: orb states were too pale and the primary button had dark text on blue; disabled primary buttons did not look disabled; native cards had an oversized heading gap; a stale "Select or create a chat first." stayed after a chat was chosen; the sidebar menu entry could fail if clicked before the store finished loading; a null capability line raised console errors.
6. **The test browser leaked.** On Windows, closing the throwaway headless browser left the real browser process running and holding its debugging port (134 processes, 13 of them listening, had accumulated). Later runs then attached to a stale browser and the page looked broken ("Permission denied" for a microphone that was fine). The helper now closes the browser over the DevTools protocol, stops whatever was started with that run's private profile folder, and refuses to attach to a port that already answers. Both the repo helper and the rig copy are fixed; a leftover check after each run now reads 0.

## 2. Verification

| Check | Result |
|---|---|
| Python, default interpreter | 428 tests: 10 errors (five fastapi-dependent modules and the gateway tests cannot import here), 16 skipped, 2 expected failures |
| Python, `simplemem` Conda env with fastapi, `CONVO_SYSTEM_ONE_TEST_SDK` set | 466 tests (plus the 10 decision-backend plugin tests, run separately): 1 error (`soundfile` missing there; passes under the default interpreter), 15 skipped, 2 expected failures; this run executes the sidecar route tests and the classifier-acceptance suite against System One's real SDK |
| Expected failures that remain | BP5 continuation merge and BP7 direct-vs-background route: each is stated as a target test and needs its own approval (the BP4 `attention` target now passes) |
| Node (VM fixtures, replay, audio, live helper) | 149 tests: 147 pass, 0 fail, 2 todo (optional pre-roll, BP9 echo guard) |
| In-image run on the retained fixture `convo-test-v211-doctor-20260908` | `IN_IMAGE_PASS` (the main group grew from 179 to 219 tests with the gate suites) |
| `update-policy.test.ps1`, `image-policy.test.ps1` | 40 checks, 49 cases |
| Mutation checks | 24 single changes to the gate, the envelope and the live loop (floors, mode, trusting an untyped classifier, the interrupt score, the cooldown, the argmax check, work authorization, names, undelivered context, ...): all 24 are caught by at least one test (two survived the first pass and got tests); earlier cycles' fixes were each reverted in turn and caught |
| live-ui: `node tests/live-ui-smoke.mjs` on the isolated host | 20/20 (adds: the Speech gate card shows the measured defaults; readiness names the gate as calibrated or unsupported) |

## 3. Live pipeline results (stand-in models; scripted speech)

The scripted microphone is the synthetic corpus from the operating system's offline synthesizer. The conversation model and speech
provider are stand-ins; the decision backend was a deterministic stand-in until the real decision model ran (section 4).
Numbers describe the plumbing and Convo's own latencies, not any model.

| Scenario | What it showed |
|---|---|
| one addressed question | full cycle through the real stack: probe to admission about 130-160 ms, endpoint 0.92 s, final to first text 0.16-0.24 s, final to audio about 1.3 s; orb reads speaking for the whole playback |
| 25 s continuous speech | first turn cut at 19.9 s of audio and accepted (no length rejection), the remainder opened the next utterance (BP3a, in a real browser at 48 kHz) |
| barge-in while the previous reply is still being synthesised (6 s synthesis) | the next reply met the busy provider, waited for its drain and was heard with no error (BP3e; mark `speech.busy`) |
| a long request with sentence pauses | answered in fragments, each fragment its own turn, earlier replies cancelled by the next (the BP5 gap, visible live) |

Observed and deliberately **not** changed (each belongs to a gated package):
- while the assistant speaks and the user keeps talking, the duck toggles with every probe (volume pumping; BP6)
- a one-word interruption shorter than 450 ms of voiced audio is never detected (acoustic admission threshold)
- first audible output waits for a phrase of at least 60 characters ending in a stop (BP8)

## 4. Decision model (D1) lane

**Installed and run (side by side; nothing of yours was modified):** the public projector `mmproj-d1-omni-600M-F16.gguf` (441,262,144 bytes, sha256 `77e4a6b6...eda53`, verified against the publisher's listing) now sits beside your `d1-omni-600M-Q8_0.gguf` (the stalled `.part` file was left untouched). The engine is the official `ggml-org/llama.cpp` release b11630, CUDA 12.4 build plus its runtime DLLs, sha256-verified, in `D:\llama.cpp-mainline11630`. `C:\llama.cpp` and LM Studio's backend were not used or changed. `llama-server` reports `decision model type: lfm2-d1-omni`, serves `POST /v1/systemone` on loopback only with one slot, and loads in about 1.2 s. It used about 2.0 GB of GPU memory and 1.4 GB of RAM while loaded, and was stopped afterwards (`serve_d1.ps1` starts and stops exactly the process it started; it starts without your `LLAMA_API_KEY`, which it neither uses nor sends).

**In the loop:** System One's real `LiquidD1Client`, real consumer gate and real answer validation call that server through the test-only bridge, and Convo's own `register_backend` / readiness / policy / candidate loop ran unmodified in the isolated host. Registration, readiness and the decider reference all report the backend and its declared modalities (text, audio, text+audio, images; 32 questions; 16 kHz mono, 30 s).

**Latency (measured, warm, the four Convo policy questions over audio):**

| Audio in the request | Model alone | Through Convo (container, bridge, SDK gate, envelope) |
|---|---|---|
| 1 s | 80 ms | 150 ms |
| 3 s | 101 ms | 172 ms |
| 6 s | 121 ms | 215 ms |
| 10 s | 158 ms | 262 ms |
| 20 s | 275 ms | 409 ms |
| 28 s (model limit 30 s) | 398 ms | Convo refuses audio over its 20 s setting |

Live in the real browser stack, the server's own marks over 25 candidates (probes and finals up to about 4.6 s) gave p50 272 ms and max 486 ms. The 1.5 s classifier budget is realistic for this model on this hardware, with a wide margin. A text-only call takes about 28 ms.

**Decision quality: not usable as Convo frames the question today.** On the 14 synthetic clips with listener-style labels the model alone agreed on *attention* 2/14, *action* 11/14, *explicit request* 8/14, and through Convo's envelope every clip was classified silent or "clarify": **Convo would not have answered any addressed request**. In the real stack all candidates were `ignore` and the assistant stayed silent (which also confirmed the duck is released). Read-only experiments (`d1_variants.py`, `d1_binary.py`, `d1_audio_sanity.py` under `.runtime/convo-live-test`):
- the model does hear audio (a weather request is heard as weather at 1.00; whether something is a question tracks the audio), but not reliably on these robotic voices (topic correct 1/7);
- the same sentences as plain text score no better (attention 4/14 with Convo's wording, 7/14 with shorter wording), so the weakness is the decision task for a 600M model, not only the audio path;
- framing matters a lot: `state = null` or an `input_audio` message gives attention 5/14 against 2/14 for Convo's JSON `state` with a null observation, and the shorter "does the speaker make a request of the assistant?" wording gives 11/14 on that question;
- a plain binary "respond or stay silent" question reaches 8/14 on audio, 6/14 on text.

These are synthetic voices from the operating system's offline synthesizer, 14 clips, one model: it does **not** show how the model reads natural speech. It does show that this classifier needed the BP4 work (question wording and state framing, a calibrated gate instead of fixed thresholds, real consented recordings) before it could gate a conversation. That work is section 5; it fixes the framing and the gate, and shows what the model can and cannot do once they are fixed.

## 5. Speech gate (BP4): what the classifier now decides, and how well

**Method.** The question wording and the state were chosen by measurement, not taste. A labelled corpus of 225 synthetic sentences (labels by construction: 82 requests, 28 backchannels, 54 side conversations, 40 unfinished, 21 vague; 31 flagged "hard" because the label depends on context a voice cannot carry), each in three voices (two operating-system voices and one neural voice, the saved J.A.R.V.I.S clone from your Qwen3 audio.cpp Docker), 675 clips. Sentences were split in halves: one half to choose framings and thresholds ("calibration"), the other to report ("held-out"), then a third set of 62 fresh sentences written after the wording was frozen ("validation"). Differences were compared with a paired bootstrap over sentences (the three voices of one sentence are not independent evidence). Tools: `tests/calibrate_gate.py` (in the repo; takes any classifier's typed answers) and the sweep scripts under `.runtime/convo-live-test`.

**What the framing is worth** (calibration half, 82 sentences, AUROC of the probability that speech needs a reply; 0.5 is chance):

| | Convo's old request | the shipped framing | difference (95% interval) |
|---|---|---|---|
| is this speech for the assistant (request or vague, vs the rest) | 0.55 | 0.80 | +0.25 (+0.13 to +0.38) |
| request vs acknowledgement or side talk | 0.76 | 0.91 | +0.16 (+0.04 to +0.27) |
| does the speaker make a request | 0.56 | 0.88 | +0.31 (+0.17 to +0.46) |
| called by name | 0.37 | 0.74 | +0.37 (-0.07 to +0.80, not resolvable) |

The first three are resolvable improvements. Among the better framings (several wordings, with or without an "assistant is speaking" flag) no difference could be resolved with this much data, so the simplest was kept. Naming the assistant in the attention question helped a little (+0.03, 0.00 to +0.05), because "speaks to someone by name" had made requests starting with "Jarvis" read as not for the assistant. A **plain sentence** state beat the old JSON document (a null observation plus flags made the model call almost every request "unclear" or "unfinished": its mean probability that a request is addressed was 0.13).

**The model uses what the assistant just said** (8 short replies in 3 voices, with and without the assistant's last sentence in the state): 18 of 24 judgements moved toward the right answer, e.g. "Yes, please." went from a 0.02 chance of needing a reply to 0.84, "No, that's all, thanks." from 0.14 to 0.84, and "Okay." / "Mm-hmm." after a statement fell to about 0. Two pairs moved the wrong way ("Go ahead." after "Should I read them out?" fell from 0.20 to 0.04; a bare "Yes." stayed low). So the state now carries the last thing the user heard the assistant say.

**How the gate performs** (shipped floors, synthetic speech, d1-omni-600M). The honest numbers are the first two rows: the floor was chosen on the calibration half and then used on speech the choice never saw.

| Check | Requests the assistant did not answer | Other speech it answered |
|---|---|---|
| held-out half (floor 0.30 chosen on the other half) | 32% (23-43%) | 27% (21-35%) |
| fresh validation set, 62 new sentences (floor 0.30) | 37.5% (27-49%) | 10% (6-18%) |
| **shipped floor 0.15, fitted on all 225 sentences** (no independent data left; optimistic by construction) | 19.5% (15-25%) | 29% (25-34%): backchannel 13%, side talk 24%, unfinished thoughts 47% |

The half it was chosen on had said 5% missed requests: that was selection luck, which is why the shipped floor was re-fitted on everything and why 32-38% is the figure to expect from a floor picked that way.

| Constant | Shipped | Measured meaning |
|---|---|---|
| `respond_min` (active listening) | 0.15 | least cost when an unanswered request costs two unwanted answers (a tie with 0.20; the lower wins) |
| `ambient_respond_min` | 0.75 | answers 4.6% of speech that is not for the assistant; leaves 55% of requests unanswered (ambient listening is conservative by design) |
| `interrupt_min` (probability of a request, while the assistant is speaking) | 0.40 | 39.6% of requests stop the assistant, 4.9% of other speech does |
| `ignore_min` | 1.0 (off) | no floor waves away non-requests without also waving away real requests (at 0.98: 6% of requests for 21% of other speech) |
| `name_min` | 0.90 | not measurable: no floor separates a call by name from other speech (AUROC 0.72); **name mode is not reliable with this model** |

**Live check** (real browser capture, real handler and gateway, the registered D1 decision model, your Qwen3 audio.cpp Docker voice; the conversation model was the stand-in server because no OpenRouter key is entered yet; the speech is the neural voice):

| Scenario | What the gate decided | Result |
|---|---|---|
| a request ("what's the weather going to be like tomorrow afternoon?") | probes 0.94-0.96, final 0.97 -> admit | answered; first audible speech 5.2 s after the final (a 60-character phrase plus real synthesis at about real time; the stand-in speech was 1.3 s) |
| the same request, then "Mm-hmm." while the reply plays | backchannel probes 0.27 and 0.06 -> held (ducked), final 0.03 -> ignore | the reply was not cut and the backchannel got no reply |
| the same request, then "Wait, hold on, that is not what I meant." while the reply plays | probes 0.08-0.12 -> held, final 0.19 -> admit | the reply was revoked only at the end of the sentence, 3.5 s after the user started, then answered |
| a television announcement, a phone call, then a request | announcement: admitted and **answered (wrongly)**; phone call: ignore; request: admit | 1 of 2 non-requests wrongly answered, matching the measured rate |

Latency of the decision: 130-190 ms per call (p50 0.15 s over the run), faster than before because the state is shorter. The decision model (about 2.2 GB), your Qwen3 TTS model (about 6.4 GB while synthesising) and the 2.5 GB the rest of the machine was using came to 11.0 of 16 GB of video memory; the main model was a stand-in in this run, so that is not a combined-load measurement with your local Gemma. Loading the Qwen3 model took 21 s; it was unloaded again afterwards, as it was found.

**What this means.** The gate now works as designed and says what it is doing, and the mechanism is model-independent. With this decision model on these clips the classifier is a weak judge: it cannot separate a request from other speech without trading about one in five missed requests for roughly three in ten unwanted answers, it reads broadcasts and side questions as requests, it almost never labels an unfinished sentence as unfinished on synthetic speech (mean probability 0.04), and in the live run it recognised a clear "wait, hold on" over the assistant only at the end of the sentence. It is good at: acknowledgements ("mm-hmm" is answered 13% of the time at the shipped floor and 2% at 0.30), explicit requests, and anything that follows the assistant's own question. A stronger audio decision model, or real voices (which carry the turn-taking and prosody these clips lack), could change every number here; neither was measured. The classifier gating speech is therefore not better than "answer unless it clearly is not for me" until it is re-measured on your voice.

**Limits.** Synthetic speech only, one decision model, isolated sentences, one speaker per voice; the validation set shares an author with the others. No natural speech, rooms, noise, overlapping talk or real turn-taking was heard. The constants belong to this model: they are settings (Advanced > Speech gate) and `tests/calibrate_gate.py` recomputes them from labelled recordings.

## 6. The isolated environment, and how to undo it

- Container: `agent-zero-system1-local-test-2` (your existing isolated instance, port 5028, bind `C:\a0-plugins\.local-test\usr`); a fresh test chat `0BmpXEff` was created in it. Its MCP servers start on boot; no MCP tool was used.
- Test-only additions there: plugin `convo_live_test_bridge` (registers System One's real decision client with Convo; loads a hash-pinned copy of the media SDK under System One's package name, so System One's own files are untouched). It is unregistered and switched off, its files kept. One residue: the throwaway gateway token set in the host *process* environment (`CONVO_SIDECAR_TOKEN`, no file); it disappears at the next host restart and the gateway it belonged to is stopped.
- **The test host's Convo settings are no longer the original ones, on purpose.** Following your testing direction they now point the conversation role at OpenRouter (`google/gemini-2.5-flash-lite`, HTTPS, remote consent set) and the speech role at your Qwen3 audio.cpp Docker (`http://127.0.0.1:8890/v1`, model `qwen3-tts-1.7b-base-bf16`, voice `clone:2098c329bc9d`, the saved J.A.R.V.I.S profile), with System One off and Convo ON. The original settings are saved at `C:\speech-to-speech\.runtime\convo-backups\live-test-config\_convo-config.before-d1-20261015.json` (copy that file back over `plugins/_convo/config.json` with Convo OFF to return to them). No API key is stored: Convo shows only whether one is set, and none is.
- Your Qwen3 Docker container (`audio-cpp-qwen3-tts-voice-studio`, ports 8890 API / 8891 Studio) was used as is: it was running with its model unloaded, I loaded `qwen3-tts-1.7b-base-bf16` through its own control API for the tests (21 s, about 6.4 GB while synthesising) and unloaded it again; I never started, stopped or rebuilt it. To use it again the model must be loaded first (Voice Studio's model controls, or `POST http://127.0.0.1:8890/v1/backend/models/switch {"model_key":"qwen3-tts-1.7b-base-bf16"}`); until then speech requests answer HTTP 409 "No candidate model is loaded".
- Isolation check against the baselines recorded at the start (`.runtime/convo-live-test/isolation_check.py`): production's protected files (settings, five plugin configs, toggle) and its 111 chats are unchanged; the evidence instance's protected files (settings, `.env`, `secrets.env`, System One / Whisper / model configs) are unchanged, with only Convo and the bridge added as plugin folders.
- Deployment stamps (backups in `C:\speech-to-speech\.runtime\convo-backups\deploy-test-<stamp>`): 20261014-233152 initial, 20261015-000610 and 001908 (interface cycles), 004328 (BP3), 010614 / 012044 (web fixes), 012535 (start refusals), 015941 (busy backend, orb playing), 103601 (BP4 speech gate), 113036 (browser helper fix). Roll back with `update-persistent.ps1 -Target test -Rollback -BackupStamp <stamp>` while Convo is OFF.
- Host-side helpers (all under `C:\speech-to-speech\.runtime\convo-live-test`, git-ignored): stand-in servers, `start_sidecar.ps1 -Hold` (Convo's own gateway, run from the existing `simplemem` environment; nothing installed), `serve_d1.ps1 -Hold`, `live_setup.py up|down`, the corpus builders (`sentences.json`, `make_corpus2_*`, `make_val.py`), `d1_sweep.py`, `sweep_report.py`, `sweep_compare.py`, `pooled_gate.py`, scenario builder `make_scenarios.py --gate`, `e2e_run.mjs`, `analyze_run.py`, `gate_marks.py`, `isolation_check.py`.
- A stray `%TEMP%\_unused` file was left by an earlier audit agent.

## 7. Not verified, and what needs you

- **The hosted conversation model.** OpenRouter's Gemma 3 and Gemma 4 models take text, images and video but not audio (their public model list), so Gemma there would force a transcript path and defeat direct audio; `google/gemini-2.5-flash-lite` (audio-capable) is set instead and is one field to change. It needs your OpenRouter key: enter it in the test host (Convo settings > Conversation > API key); it is stored write-only and bound to that URL. None has been used, so no hosted model was exercised and the live runs used the stand-in model.
- **Natural speech.** Every number here is synthetic speech. A few consented recordings of you (requests, side talk, acknowledgements, a thought trailing off, with a line of label each) would let `tests/calibrate_gate.py` re-measure every constant; until then none of these floors is a claim about your voice.
- No listening acceptance, no physical microphone/echo/double-talk, no combined-load measurement with your local Gemma.
- BP5-BP10 (continuation merge, safety yield, action routing, first-audible, echo guard, speculative reply) are **not started**: each needs its own yes. The measurements point at which matter most: unfinished thoughts are answered 47% of the time (BP5), a clear interruption is recognised late (BP6), and first speech takes about 5 s with the real voice (BP8).
- P9 (the optional native Model Configuration entry) is not built.
- Production promotion: the same change can be applied with the scoped updater (`-Target production -ConfirmProduction agent-zero-v2.11`, Convo OFF, backup, rollback available) once you say yes. Production does not use System One, so the BP4 gate is inert there until it does; the fixed-threshold path is unchanged.

## 8. Gate fix, streaming voice, production install and end-to-end numbers (2026-10-15, evening)

**Gate fix (context after a question).** Measured with the product's own questions and state sentence on all 163 v2 sentences x 3 synthetic
voices (`d1_gate_context.py`): when the assistant's previous reply ended in a question, the line "The assistant had just said: ..." made the
decision model read almost any speech as the answer. Share answered at the shipped floor (.15), no context -> chatty reply ending in an offer:
acknowledgements 8% -> 93%, unfinished thoughts 44% -> 78%, talk to someone else 24% -> 40%, real requests 80% -> 91%; with a short
yes/no-style question as context talk to someone else reached 75% and everything else 100%. A statement as context helped (acknowledgements 8% -> 3%,
unfinished 44% -> 30%). The same line is what turns "Yes, please." from .02 into .84, so it cannot simply go. Rule shipped: after a question the
line is kept only for a short answer (audio up to 3 s, recognized text up to 8 words); after a statement it is always kept. On the measured data
that gives requests 89%, talk to someone else 27% (was 40%), unfinished 68% (was 78%), acknowledgements 93% (unchanged: "mm-hmm" after an offer
is arguably an answer). Not a cure: the model still cannot tell an answer from a wandering remark. Constant: `QUESTION_CONTEXT_SECONDS`. Five tests, mutation-checked (5/5 caught).

**Streaming voice.** The Qwen3 audio.cpp Docker (voice J.A.R.V.I.S, `clone:2098c329bc9d`) now runs in its incremental PCM mode on the test host and in production
(`roles.tts.capabilities.native_pcm`, labelled experimental in the UI). The code default is unchanged for everyone else. Production's model id was
`qwen3-tts`, which the container does not list; it is now `qwen3-tts-1.7b-base-bf16`.

**End-to-end runs** (real headless browser, scripted recordings as the fake microphone, real capture worklet and boundary, real Convo socket and gateway, real
decision model d1-omni-600M, hosted conversation model `google/gemini-2.5-flash-lite` through OpenRouter, Qwen3 streaming voice, J.A.R.V.I.S clone; synthetic
speech; 11 scenario runs: the four gate scenarios through the test bridge, then four and three again through the production decision plugin; 20 answered turns):

| Stage | Measured |
|---|---|
| silence needed to end an utterance | 0.92 s (fixed) |
| classifier call | 0.15 s median, 0.19 s worst |
| speech start -> admitted (first probe) | 0.17-0.21 s when the first probe admits |
| end of speech -> first audible sound | 5.2 s to 7.6 s, median about 6.3 s |
| of which: model first text | about 3.1 s (hosted model, audio upload) |
| of which: phrase wait + voice first bytes | about 2.1 s (reply waits for 60 characters; BP8 not built) |
| a correction while the reply plays | heard only at the end of the sentence (2.7 s after it began) |

Behaviour in those runs: a plain request is answered once; an acknowledgement after an assistant question was answered (reasonable after an offer); a phone call was
correctly ignored; a television announcement was answered (the decision model reads broadcasts as requests: known weakness); the real request after them was answered;
no tool markup reached speech; no console errors. Replies were one or two sentences, voice-sized, and each ended with an offer. All of it is synthetic speech,
a different conversation model from the one you run locally and a stand-in route for the model call: a parity check against local Gemma has not been done.

**Fallback and AI Link.** Convo OFF: the host's microphone button carries nothing of Convo, no store, no panel, no console message (3/3, test host). Convo ON with AI Link
development mode ON: the orb stays idle, a click explains and starts nothing, no session opens (3/3 on the test host and on production); the server refuses a start in that
mode (`convo.start`, covered by the offline suite). `ai_link_bridge/config.json` is byte-identical before and after the production update; Enhanced Speech stays OFF.
The decision plugin registers nothing while Convo is OFF (test + mutation).

**Production install** (agent-zero-v2.11, container not restarted): Convo OFF -> scoped update (43 files added, 35 changed, 0 removed; backups under
`.runtime/convo-backups/deploy-production-20261015-201325`; protected configs unchanged) -> `convo_d1_decider` installed beside it
(`...201338`) -> both ON -> backend `d1_llama_cpp` registered. Saved: classifier = System One / `d1_llama_cpp`, streaming voice. AI Link development mode left ON, so
browser voice is still idle until you leave that mode. Rollback: Convo OFF, `update-persistent.ps1 -Target production -ConfirmProduction agent-zero-v2.11 -Rollback -BackupStamp <stamp>`
(add `-Plugin convo_d1_decider` for the second), or just toggle either plugin OFF. Not run in production: the full UI smoke (its editing of a settings draft was blocked by the tool guard).

## 9. Batch of 2026-10-16: parallel turn engine, echo guard, dictation, settings, judgment

Six tracks built in parallel on separate files (research, backend, browser audio, dictation, settings/persona/context, judgment). Test host only; production was **not** changed in this batch.
Offline: Python 673 run (simplemem + SDK; 1 known `soundfile` error, 15 skipped, 1 expected failure = BP7 route enforcement), decision plugin 10, Node 232/232 (0 todo), policy tests 40 + 49, UI smoke 20/20 on the test host, AI Link dev-mode check 3/3.

| Area | What exists now | Evidence level |
|---|---|---|
| Parallel classification + held speculative reply (`speculative_reply`, off) | main-model generation starts with the System One call; held output, no tools/speech/history until admitted and unchanged; discarded within the 1.5 s join otherwise | unit + virtual clock; not measured live |
| Main model may answer `[LISTEN]` (`main_listen_decision`, off) | can only suppress, never grant; never suppresses an explicit request >= .9; never enters history | unit |
| Continuation / corrected long thoughts (`continuation_seconds` 7, on) | memory-only fragment join, supersede an unheard reply and its queued jobs; short confident commands answered alone | unit + mutation; not live |
| First clause + async speech queue (`first_clause_speech`, off) | virtual clock: first audible 1.6 s -> 1.2 s with TTS at 1.0 s (scripted, not measured) | unit |
| Echo guard (`echo_guard` adaptive/strict/off) | port of the standalone Adaptive v3; synthetic: 0 false starts at -10/-20/-30 dB leak, speech over echo found 0.46-0.58 s after onset | synthetic only; real room unverified |
| Stable ducking | gain changes in a 6 s reply with 0.75 s probes: 16 -> 2 | synthetic |
| Dictation | tap toggles Convo; hold 2 s latches host dictation; hold 4 s push-to-talk; Escape cancels; host `sendVoiceMessage` honours `message_mode` (verified in the live host: it writes the composer and sends only when `sendsImmediately`) | unit with virtual clock; microphone feel unverified |
| `response_detail` / `spoken_updates` | applied at the next response; today only finish/fail are spoken, so the other update levels do nothing until milestone events exist | unit |
| Persona / relevant context / delegation | automatic persona; bounded relevant context (<=6 snippets, 1500 chars, secrets redacted) for the turn and for `delegate_task`; outstanding jobs keep their original target | unit |
| Incremental input | readiness row `incremental_input: unsupported (completed clip)`; research: no stack available for the local Gemma path accepts audio incrementally | doc + research |
| Judgment | requests that start with the assistant's name 54% -> 96% answered on a held-out split; clarification asked 31 -> 2 times per 4,896 readings; floors .30 / .90 / .60 | synthetic SAPI speech, d1-omni-600M; media and trailing thoughts still answered 25-65% of the time (`GATE_JUDGMENT_20261016.md`) |

Found live and fixed: the new duck controller stored `setTimeout`/`clearTimeout` as its own properties, so every duck threw "Illegal invocation" in a real browser (no stub could show it); fixed, with a test that uses browser-strict timers. This is the reason browser code from a track is not trusted until it has run in a real page.

Re-measured after the new wording and floor (`d1_gate_context.py`, floor .30): the rule of keeping a question as context only for clips up to 3 s still holds talk to someone else at 24-33% (25% without). It lets short unfinished fragments and acknowledgements after an assistant question through (76% / 45%), because they look like answers. A statement as context lowers unfinished thoughts (39% -> 19%) but also requests (83% -> 74%) at this floor.

Not run: any end-to-end timing with speech output. The user's Qwen3 voice container and the isolated test host were both stopped overnight (not by this work); I restarted only the test host (the adopted fixture). A live run reached capture (through the echo guard), admission and first model text (2.3 s after the final) and then stopped at the missing voice; the sidecar's speech-admission lease is therefore in the "unresolved" state until the voice provider is reconciled. Speculation, first-clause speech and the streaming-voice gain still need a run with the voice container up.

### 9.1 Follow-ups (2026-10-16/17, test host)

- **Speech-output runs** with the user's Qwen3 voice (J.A.R.V.I.S, streaming) and Gemini 2.5 Flash-Lite, end of speech -> first sound: baseline 6.2-7.2 s (median about 6.8 s); speculative reply + first-clause speech 5.3-7.4 s (median about 6.0 s); with the `[LISTEN]` decision as well 5.3-5.7 s (3 turns). The hosted model's first text (about 3 s) still dominates.
- **Found live and fixed:** a held reply promoted while still generating could propose a tool a moment later on a turn the classifier had not authorized; the turn then failed with a generic error although the user had been answered. Now the late proposal is dropped (nothing runs) and a reply that was only a tool call is asked again with tools withheld (`spec.tool_dropped`). Failures now leave a content-free `reply.error` mark naming the class or one of the module's own fixed refusals.
- **Gate regression to watch:** the phone-call clip in the side-talk scenario was answered in 2 of 3 configurations (it was ignored before the judgment track's cue change that fixed requests starting with the assistant's name). One synthetic clip; not re-tuned.
- **Dictation, as the user specified:** tap toggles Convo; a 2 s hold switches on the host's normal hands-free dictation (after release it keeps listening and uses the host's own silence/timeout settings; `message_mode` send = each utterance sent and listening continues, draft = text left in the composer and dictation stops, by delegating to the host's own `sendVoiceMessage`); silence does not end the utterance while the press is still held between 2 and 4 s; past 4 s push-to-talk, transcribed once on release. Audio from 300 ms after the press is kept in the first utterance (same stream). Real-browser check on the test host (`convo-live-test/live_dictation.mjs`, real mouse events, fake microphone): 10/10 (pre-open, hands-free label, voice-activity mode after release, tap ends it, push-to-talk ignores silence, release ends it, tap still toggles Convo, no console errors). Transcription itself was not exercised: the test host's Whisper model is not loaded, and loading it would be a download.

## 10. Iteration 3 (2026-10-17/18): native model settings, speech providers, continuous dictation, diagnostics panel

Built in three phases (switch-bug fix, shared groundwork, then five parallel streams), integrated, deployed to the isolated test host only. Production is unchanged.

| Area | What exists now |
|---|---|
| Speech backend switch bug | `structuredClone` on Alpine's reactive Proxy threw after the first switch, so the select changed while the settings did not. Plain JSON copies everywhere; the select re-syncs to the backend in effect; tests now use an Alpine-like reactive store. |
| Conversation model | A "Convo conversation model" section in Agent Zero's preset editor (one setting for all presets, autosaves, conflicts shown inline) plus a "Convo" row in the preset overview and Settings -> Agent -> Models; the same card in Convo -> Connections. Agent Zero's provider keys (`API_KEY_<PROVIDER>`), Convo's own key only for a custom endpoint, an explicit "copy my Convo key" into an empty slot. Selectable: OpenAI, OpenRouter, Google, llama.cpp, vLLM, Other; every other v2.11 provider (incl. ones other plugins add) disabled with its reason. "Test audio input" sends one synthetic clip on click. |
| Turn-taking decider | "Use System One's decider" (detected; today it decides from text, so it says so and a session will not start on it) or "Separate decider for Convo" (d1-omni on a llama.cpp server on this machine, Check server). The decision plugin resolves its address per call from that choice and reloads itself when updated. |
| Speech output | Provider editor: Agent Zero built-in Kokoro; audio.cpp (external server or managed by Convo); llama.cpp and Unsloth Studio (named, standard speech protocol, "not verified" until Test connection passes); OpenAI; any OpenAI-compatible endpoint. Read-only discovery, never a model switch on your container. "Voice delivery" (formerly "Delivery instructions": tone/pace sent to the speech engine) appears only where honoured. |
| Dictation | Third voice-message mode "Draft in composer (continuous)" in Agent Zero's Whisper settings and the Convo card: hands-free dictation keeps listening and adds each phrase to the composer until you send or stop. |
| Diagnostics | Developer diagnostics turns on a "Convo diagnostics" canvas surface that pops out as a draggable, resizable window: live strip, per-turn waterfall, gate bars against floors, speculation outcome, p50/p90, context vs window, and Tune (every former Advanced setting). Settings now has Connections, Conversation, Voice & Audio and a Developer row. |

Verification: Python 792 (simplemem + SDK; only the known `soundfile` error), decision plugin tests pass, Node 402/402, update/image policy, in-image PASS (against the real v2.11 provider list). Real-browser checks on the test host: UI smoke 19/19, settings 6/6, speech 55/55 (discovery against the voice container skipped: it was stopped), decider 24/24, model type 26/26, continuous dictation 24/24 (fake microphone, transcription answered in the page, nothing sent), diagnostics 45/45, orb gestures 10/10, AI Link development mode 3/3, Convo OFF 3/3.

Found only in the real browser and fixed: (1) the decision plugin kept serving its old code after an update (a plugin toggle does not reload its Python) -> reload on change; (2) a binding that threw on its first evaluation left "Test audio input" disabled for good -> guarded bindings and a static test; (3) the host re-rendered its overview rows after the Convo row -> the row moves back to the end; (4) the v2.11 provider list has providers the older source lacks (llama_cpp, vllm, ...) -> catalog updated from a verbatim fixture.

Not verified: a real microphone, listening quality, any provider actually accepting audio, Unsloth Studio / llama.cpp speech routes, OpenAI speech with a real key, and end-to-end timing with speech (the voice container was stopped).

## 11. Iteration 4 (2026-10-19): the Classifier card

- **Card:** "Turn-taking decider" became **Classifier**, built from the same shared card factory as the Conversation model (`webui/model-role-setup.js`). It has the same fields, classes and order: Provider, Model name (Agent Zero's model search and grouping), API key (Agent Zero's key store), Context window, Accepts audio input, Allow remote processing, and Advanced (API base, Classifier type, Check server).
- **Toggle:** "Use System One's decider" sits underneath and is off by default. When on, the fields show System One's values read-only, with the verdict and a link to Agent settings -> Models.
- **Provider select:** one select with groups:
  - Local servers: llama.cpp (verified), LM Studio, Unsloth Studio, Ollama, vLLM, oMLX.
  - Cloud: TypeSafe Jev (text only), OpenAI, OpenRouter, Google.
  - Other providers: the rest of Agent Zero's providers, including ones other plugins add.
  - Other.
- **Check server:** lists the models, then sends one short text decision, only on click. On the test host it found the running d1 server ("answers the System One decision route").
- **Remote rule:** this machine and private networks pass. Public addresses need "Allow remote processing" and HTTPS. A key is sent as Bearer only when one resolves, server-side.
- **Context window** caps the conversation context a chat classifier is shown (about 3 characters per token, 1000 to 6000 characters). For a decision model the decision server's own batch size is the real limit.
- **Provider Profiles readiness:** both cards read providers, details, search, grouping and the provider-change hook from Agent Zero's `modelConfig` store at use. A provider another plugin adds is selectable with "audio not verified" (Conversation) or as a chat classifier, at its own API base with Agent Zero's key for that id. A bare local server address gets the provider's `/v1`.
- **Styling:** the three Connections cards share computed title, description, note, field and legend styles. This is checked in the real browser. Convo's other hint text now uses Agent Zero's field-description look.
- **Memory, d1-omni-600M** (Q8 model, F16 projector, `-c 16384 -ub 8192`; measured with Windows' GPU process counters during 24 decisions): about 2.4–2.5 GB of system RAM (peak 2,469 MB; 3.5 GB committed) and 2.0 GB of dedicated VRAM, plus 0.46 GB of shared GPU memory. Idle, Windows pages the RAM out.
- **Verification:**
  - Offline: Node 414/414; Python 831 (only the known `soundfile` error); decision plugin tests pass; in-image PASS.
  - Real browser on the test host: Classifier 24/24, plus the regression runs below.
- **System One bug found** (its repo is read-only here; reported, not fixed): switching its decider to llama_decision keeps `token_env`, so the Jev key would be sent as a Bearer token to the llama endpoint (`_20_system_1.html` ~349-355, `runtime.py` `client_for`).
  - Note left for the System One session, 2026-10-31: `C:\a0-plugins\docs\NOTE-decider-token-env-bug-20261019.md` (untracked; nothing else in System One changed).

## 12. Settings lockout fix and LAN/Tailscale connections (2026-10-31, test host only)

- **The lockout.** The test host's Conversation model "Credential environment reference" held an address (`http://host.docker.internal:8081/v1`, the same as its API base), not a variable name.
  - The save accepted it.
  - Every settings read then failed with "Token must reference an environment variable", so Settings would not open, the save looked failed, and Test connection only said "could not be verified".
  - The desktop address had never been saved; the API base still points at this laptop's port 8081, where nothing is listening.
- **Fix:**
  - A changed reference that is not a variable name is refused on save and autosave, labelled by field, never echoing the value.
  - One saved earlier no longer breaks a read: it is a readiness row with Clear in the card, Test connection names it without contacting anything, and voice start refuses with the same reason.
  - Test connection now names each failure: connection refused (with the LAN/Tailscale and listening-address hint), timeout, HTTP 401/403/404/other, an unreadable list.
- **Repair:** after deploying, only that one field was cleared through the normal autosave path (compare-and-set). Provider, model, key source, consent and address are unchanged.
- **Your network, for every model connection** (conversation model, classifier, decision server, speech, gateway). These need no consent and may use plain http:
  - this machine and Convo's containers;
  - RFC 1918 and IPv6 unique local addresses;
  - Tailscale's 100.64.0.0/10;
  - one-label machine names;
  - names ending `.local`, `.lan`, `.home.arpa`, `.internal`, `.localhost` or `.ts.net`.

  Before this, a Tailscale address or a machine name counted as public, so plain http was refused even with consent. The server, decision-plugin and browser copies are held to one table (`tests/fixtures/connection-rules-cases.json`).
- **Easier desktop setup:** a server you run yourself (llama.cpp, vLLM, local decision servers) now shows API base URL as a main field, with how to reach another computer over LAN or Tailscale. It is no longer an Advanced override sitting next to the credential reference field. The speech card's server hint says the same.
- **Tailscale from Agent Zero's container (read-only check, nothing printed but counts):** Tailscale runs on this laptop (3 of 12 peers online). Inside the test container a peer's MagicDNS name resolves to its tailnet address, and the tailnet is reachable.
- **Voice agent modes:** design only, in `VOICE_AGENT_MODES.md`. Nothing built.
- **Verification:**
  - Offline: Node 419/419; Python 841 (only the known `soundfile` error); decision plugin 31/31; mutation checks caught all four removed rules (read tolerance, save refusal, Tailscale range, machine names).
  - Real browser on the test host: the new `tests/live-connection-smoke.mjs` 14/14; regressions: UI 19/19, model type 26/26, Classifier 24/24, speech 54/54.
  - The model-type script now starts its key step from "Agent Zero's key", because the host is saved with Convo's own key. Every script restored the saved settings exactly.
- **Not verified:** a connection to your desktop's server. I don't have its address, and nothing on it was started or contacted.
