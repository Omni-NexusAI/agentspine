# Voice agent modes — design (not implemented)

First written 2026-10-31 from the user's direction; this revision 2026-11-04. **Nothing here is built.** Today's Convo is the
**Hybrid** foundation, and this design must not change Hybrid's behaviour or defaults. Building starts only after you have
tested today's core. Each build step needs its own approval. Downloads, GPU loads, training and new dependencies are
separate checkpoints within a step.

UI mockups (static HTML, dark and light, never deployed with the plugin): [`docs/convo-voice-modes/`](../../docs/convo-voice-modes/index.html).
Screenshots of each page are in `docs/convo-voice-modes/screens/`.

## Decisions for you

Each item has a recommendation. Nothing is built until you answer the ones for the step in question.

1. **Where the choice lives.** *Recommended:* a **Voice agent mode** card at the top of Settings → Connections, above the
   three cards it affects. The other option is a fourth tab, which hides the cause from its effect.
2. **The words.** *Recommended:* "Voice agent mode", with the options **Auto**, **Hybrid**, **Native** ("listens and talks at
   once") and **Tuned** ("a model trained to take turns"). Each has one plain sentence. "Duplex" never appears in the interface.
   You may prefer other names; tell me now, because they appear in settings, refusals and diagnostics.
3. **The default.** *Recommended:* Hybrid stays the default for everyone, so nothing changes on update. Auto can be chosen but
   is not the default until a second mode exists; decide again then.
4. **When Auto's pick changes.** *Recommended:* Convo will not start until you choose. This applies when Auto would pick a
   different mode than last time for the same model, and also to a first pick that is not Hybrid. The other option is to
   start anyway and show the change. That is visible, but you only learn about it after it has happened.
5. **Who approves delegated work in Native and Tuned.** *Recommended:* the classifier still checks every request, with the
   same rule as Hybrid (an explicit request at 0.9 or more). In those modes the Classifier card shrinks to that one job. You
   can switch delegated work off instead, so the model can only talk. Not recommended: trusting the voice model's own judgement.
6. **Native's first version.** *Recommended:* active listening only, with no ambient or name listening, because in Native
   everything you say goes to the model. Audio passes through Convo's server, so your key never reaches the browser. The other
   option is for the browser to connect to the provider directly with short-lived tokens: lower latency, but a credential then
   sits in the browser.
7. **"Research this model".** *Recommended:* Agent Zero's main chat model sends only the model name to its web search and
   drafts a profile. You confirm each claim separately, and every claim starts unticked. A draft can never change an address,
   a key or a setting.
8. **Learning over time.** *Recommended:* Convo learns only from your clicks. "Keep Hybrid for this model" is remembered on this
   machine. Measured results are shown to you and never applied by themselves. Learning shared across users is out of scope.
9. **Build the realtime connection once.** *Recommended:* the same server-side realtime client serves two modes. Hybrid uses it
   for the model's own voice, with Convo still deciding the turns. Native uses it with the model deciding. This puts Hybrid's
   own voice (step 5) on that client rather than on chat completions. It follows reports, still to be verified, that local
   omni runtimes expose speech output through a realtime route (see Sources).
10. **A new gateway dependency.** Both step 5 and step 6 need a WebSocket client in Convo's gateway image (FastAPI, httpx and
    plain uvicorn, `sidecar/requirements.txt`, have none). *Recommended:* approve it as part of step 5, built from source with
    the image. It is never installed at activation.

## What you would see

- **Connections opens with the Voice agent mode card** (mockup 1). There are four options, each with one sentence and a badge:
  *Available*, *Picks Hybrid* (Auto), *Not in this version*, *Not with this model* or *Needs setup (n)*.
- **"Who does what"** sits under the options. It answers the same four questions in every mode: *Hears you*, *Decides when to
  answer*, *Speaks*, *Approves actions*. Comparing modes means comparing four lines.
- **The cards below change state, never content.** A card is *Required*, has a *Limited role* (the classifier approving
  delegated work only), or is *Not used (kept)*. A card that is not used folds to one dashed line that names what is set and
  why it is unused, with a button to open it anyway. Saved values stay (mockups 3 and 4).
- **Auto always shows its answer:** "Auto picks Hybrid for gemma-4-E4B-it", one sentence of why, the five numbered steps
  behind it, and "Always use Hybrid" to override (mockup 2).
- **An unavailable mode says what failed and what fixes it.** Convo refuses to start with the same words the card shows,
  followed by "Nothing was sent." (mockup 5).
- **"What Convo knows about this model"** is a table of claims. Each claim carries its own label: *Checked here*, *Tested by
  Convo*, *You said*, *Vendor says* or *Draft, not used* (mockups 2 and 6).
- **Elsewhere the mode is read-only.** The panel chip says "Listening · Hybrid". The preset editor shows one line ("Auto picks
  Hybrid with this model"). Diagnostics shows the resolved mode and the step that decided it (mockup 7).

---

## 1. Vocabulary

| Term | Meaning | Not to be confused with |
|---|---|---|
| **Voice agent mode** (`voice_agent.mode`) | How a conversation is run: `auto`, `hybrid`, `native`, `tuned` | The **listening mode** `active` / `ambient` / `name` (the `mode` field of `convo.start`). Code uses `agent_mode` for the new one and never bare `mode` |
| **Hybrid** | Convo's harness takes the turns. The model hears each completed utterance. The classifier decides. Any voice | |
| **Native** | The model listens and speaks at the same time, on one continuous connection ("full duplex": both directions at once, like a phone call rather than a walkie-talkie) | Hybrid with the model's own voice: still turn-based |
| **Tuned** | A fine-tuned *conversation* model plus the harness it was trained with. It decides every few tens of milliseconds whether to talk, wait or stop, from live transcription | A fine-tuned *decision* model, such as your d1 fine-tunes. That is Hybrid, in the Classifier card |
| **Voice profile** | What Convo knows about one model: claims, each with evidence | **Provider Profiles**, the coming plugin that keeps per-provider address defaults (section 4.10) |
| **Check** | A bounded probe on your click, on this machine | **Test**, a maintainers' live run recorded in Convo's list |

## 2. The modes

| | **Hybrid** (today) | **Native** | **Tuned** |
|---|---|---|---|
| Idea | An omni model hears your audio; Convo's harness handles turn-taking; any voice | The model itself listens and speaks at once | Codacus' micro-turn video and Speakrail: a fine-tuned model plus a harness built for it |
| Who hears | The conversation model: your completed utterance as audio. The classifier hears short probes | The model, continuously while the microphone is open | Live transcription (text fragments as you speak) |
| Who decides when to talk | Convo: voice activity → classifier → speech gate → (optional) speculative reply | The model's own turn detection | The tuned model's decision every tick, plus the harness rules |
| Talking over the user | Probes during playback, the interrupt decision, ducking | Built into the model; Convo stops playback when the model says it was interrupted | Overlap decisions (continue / yield / listen) |
| Voice | Speech output (default); the model's own voice once step 5 exists | The model's own voice. Speech output only if the model can answer in text, which gives up talk-over timing | Speech output (required) |
| Delegated work | Classifier: explicit request ≥ 0.9, then `route_tool` | Same check on the request that preceded the tool call | Same check, after the harness's tool hold |
| Examples (unverified, see Sources) | LFM3-Omni-2B, Gemma 4 with audio input, gpt-audio-class chat models | OpenAI Realtime models, GPT-Live-1, NemotronLabs VoiceChat | Speakrail's Gemma 4 LoRA; your future fine-tunes |

Today's Convo is the Hybrid foundation: direct audio in, a classifier in parallel with an optionally held speculative reply,
first-clause speech output, echo guard and ducking. It borrows the tuned mode's *policies* (speculation, continuation, gate
floors) and uses off-the-shelf models; no tuned decision model exists yet.

---

## 3. Interface

### 3.1 Where the choice lives, and what is seen first

- A new card, `webui/voice-mode-field.html` (methods in `voice-mode-setup.js`, joined by `compose` like the other cards), is
  composed **first** in the Connections stack of `settings-controls.html`. Connections is the section that opens first, so in
  both the plugin Config modal and the canvas gear modal the first thing a person sees is the mode. The cards under it are the
  ones the mode changes.
- Not a fourth tab: tabs are topics. A separate tab would put the cause (mode) on one screen and its effects (folded or
  required cards) on another.
- The card is a `<fieldset class="convo-card convo-mode-card">` with a legend and a `.convo-model-note`. It uses host field
  classes and host tokens only, like the Connections cards (`settings-split.test.mjs` rules). Its parts, top to bottom:
  1. **Options**: a native radio group. Each option is a label holding the radio, the name, a one-line explanation and a badge.
  2. **Auto's answer** (only while Auto is chosen): a status box (3.5).
  3. **Mode options** (only for the chosen mode): Native's *Delegated work* and *Voice*; Tuned's *Harness* and decision-floor
     line; Hybrid's *Voice* once step 5 exists.
  4. **What the mode still needs** (only when something is missing): a checklist with a link to the card that fixes each item.
  5. **Who does what**: a `<dl>` with four rows. Each value names the card and model and adds one plain sentence.
  6. **What Convo knows about \<model\>**: a `<details>` holding the claims table and "Check what it can do" (and "Research
     this model" for an unknown model).
  7. "Why some modes are not available": the same disclosure the provider selects already use.

### 3.2 Plain words (the copy)

| Option | One sentence on the card |
|---|---|
| Auto | Convo picks the mode that fits your conversation model, and shows you why. |
| Hybrid | Your model hears each thing you say once you finish. A classifier decides whether to answer. Replies are spoken in the voice you choose. |
| Native · listens and talks at once | The model hears you the whole time and can be interrupted, like a phone call. It speaks with its own voice. |
| Tuned · a model trained to take turns | A fine-tuned model decides many times a second whether to talk, wait or stop, inside a harness made for it. Needs a separate voice. |

Privacy notes appear where they apply. Native: "In Native mode the model receives everything you say while the microphone is
open, not only what is meant for it." Research: "Sent: the model name only." Nothing on a card implies streaming input while
Hybrid is chosen (existing contract).

### 3.3 What each mode does to the other cards

States: **normal**; **Required** (solid tag); **Limited role** (dotted tag); **Not used (kept)** (the card folds to a dashed
one-line summary with "Show the … settings"). Values are never removed or reset by a mode change. A setting a mode fixes is
shown disabled with its reason (Native: "Accepts audio input: always on in Native mode").

| Card or setting | Hybrid | Native | Tuned |
|---|---|---|---|
| Conversation model | Required, as today | Required. Adds "Check realtime connection"; audio input locked on | Required (the fine-tuned model) |
| Classifier | Required: decides every turn | Limited: approves delegated work only; not needed when Delegated work is Off | Limited: approves delegated work only |
| Speech output | Required (or the model's own voice from step 5) | Not used (kept); choosable under Voice | Required |
| Speech input (new, step 7) | Not shown | Not shown | Required |
| Echo protection (Voice & Audio) | As today | As today; Strict adds "Strict stops you interrupting the model" | As today |
| Ambient / name listening (Conversation) | As today | Note: "Not available in Native"; values kept | Note: "Not available in Tuned yet" |
| Tune: speech gate, turn engine (diagnostics) | Apply | Header "Applies in Hybrid only"; values kept and editable | Harness floors shown read-only; Convo's gate "Applies in Hybrid only" |

The card states come from the server's resolver answer (`voice_mode.cards`), never from the browser's own reading. There is
one source, the same one that refuses start.

### 3.4 When a mode is unavailable

Three kinds, each with its own badge and border style (always with an icon and words):

| Kind | Badge | Can it be chosen? | Says | What makes it available |
|---|---|---|---|---|
| Not built | *Not in this version* (dotted) | No: native `disabled`, reason as visible text tied by `aria-describedby` | "A later Convo update adds it." | An update |
| A check failed or a requirement is impossible with this model | *Not with this model* (dashed) | Yes, so it is ready when the model changes | What was checked, where, when, and the result (no server text) | "Choose a model that …, then Check what it can do", or "Use Hybrid" |
| Something is missing | *Needs setup (n)* (dashed, warning) | Yes | A checklist: each item with icon and words | A "Go to …" link per item |

Start is refused with the same words (section 4.5). Convo never starts in another mode or with another model instead.

### 3.5 Auto: what it picked, why, and the override

- The Auto option's badge always reads "Picks *X*" (or "Would pick *X*" while another mode is chosen).
- While Auto is chosen, a status box shows "**Auto picks Hybrid** for gemma-4-E4B-it" and one sentence of why. The `<details>`
  "How Auto decided" lists the same five steps every time, with the step that decided in bold and marked "Used":
  1. Your choice. 2. Modes you chose for this model before. 3. Convo's list. 4. Possible on this machine. 5. Default (Hybrid).
- **Rule: claims choose, checks allow.** A claim (vendor, your own, Convo's list) can make Auto *prefer* a mode. Only a check on
  this machine, or Hybrid's existing readiness, lets Auto *pick* it.
- **Override:** "Always use *X*" selects that radio, as an ordinary draft change saved with Save. Any radio is also an override.
- **No silent change:** the pick is made when a conversation starts and kept for it. If the pick differs from the last start
  with the same model, or a first pick is not Hybrid, the card shows a banner with the reason. The banner offers "Use *X*" and
  "Keep *Y* for this model", and start is refused until one is chosen (decision 4).
- **Learning by click only:** "Keep *Y* for this model" is stored on this machine as a *You said* preference (step 2 of the
  resolution).

### 3.6 Evidence

Every claim about a model is a row: *Can it …* / *Answer* / *How Convo knows*. Each row has its own label, source and date.
Labels, strongest first:

| Label | Means | Border |
|---|---|---|
| Checked here | A check you ran on this machine passed or failed (date; the fingerprint of provider, address and model) | solid, heavy |
| Tested by Convo | The maintainers ran it live on the isolated test host and recorded the result (date, setup) | solid, heavy |
| You said | You declared or confirmed it. Used, never verified | double |
| Vendor says | The model's own documentation (source address written out, read date) | dashed |
| Draft, not used | Agent Zero drafted it; ignored until confirmed | dotted, italic |

When two labels disagree, the row shows both, and the stronger one decides. For example, a vendor claim of "yes" sits beside
a failed check, and the check decides. "Checked here" proves a route, not quality: audio input passing says nothing about
understanding speech. A row "answer well in a conversation?" stays *Unknown* until a live listening test.

### 3.7 "Check what it can do"

A button in the facts panel. It runs only on your click and is refused while a voice session is live. Each check is bounded
and shows what it sent ("one short synthetic clip", "opened and closed a session, sent no audio"):

- the models list (today's Test connection);
- the audio-input probe (today's Test audio input);
- the realtime handshake (step 3; section 4.3);
- the own-voice probe (step 5). The button text says it may cost a few tokens on a cloud provider.

### 3.8 "Research this model" (step 4)

1. Shown only for a model with no profile (or as "Research again"). The click opens a dialog that says what happens. Agent
   Zero's main chat model searches the web for this model's documentation and drafts what it can do, with sources. *Sent:* the
   model name only, no audio, keys, addresses or settings. It takes about a minute; you can close settings and the draft
   waits. Nothing changes until you review.
2. While it runs: a status line ("Researching … started 14:02") with Stop. Under reduced motion the ring is static and the words
   carry the state.
3. **Review:** a table of claims (*Keep* checkbox, claim, what the draft says, source address as text plus a quote of at most
   200 characters as plain text). **Every box starts unticked**, and "Keep ticked claims" stays disabled until one is ticked. A
   claim with no source cannot be kept as a vendor claim. Kept claims are stored as *Vendor says · you confirmed*, never as
   tested. "Discard draft" drops it.
4. A draft only describes the model. It cannot change an address, a key, the mode or any setting, and Auto still needs a check
   before picking a mode on its strength.

### 3.9 Other places

- **Canvas panel chip** (`main.html`): "Listening · Hybrid", or "Auto · Hybrid" where it fits; "Not started · Native" with the
  refusal. The mode a conversation started in never changes during it.
- **Preset editor** (`model-type-mount.js` copy of the conversation card): one read-only line, "Voice agent mode: Auto picks
  Hybrid with this model", with "Change in Convo settings". When the model changes, the line says what Auto would pick (from
  `voice_mode_preview`). The overview row reads "llama.cpp / gemma-4-E4B-it · Hybrid (Auto)". The preset editor never edits
  the mode, because it autosaves.
- **Diagnostics** (developer mode): the Live strip gets "Mode: Hybrid · picked by Auto · step 3, Convo's list (vendor says)".
  Native and Tuned turn marks are listed in 4.8. Tune shows "Applies in Hybrid only" above the speech gate and turn engine
  when another mode is in effect.
- **Conversation tab and Voice & Audio:** notes only, as in 3.3. No setting moves.

### 3.10 Narrow canvas, keyboard, themes, motion

- **Narrow** (viewport under 600 px, or a card under 560 px through the existing `convo-model` container query): each option
  stacks its badge under the name; "Who does what" becomes label over value; the facts table becomes one claim per block. Tabs
  keep 44 px targets and nothing scrolls sideways. Settings never render inside the canvas panel; the gear opens the modal.
- **Keyboard:** native radios (Tab enters at the chosen mode, arrows move between enabled modes, disabled ones are skipped) and
  native `<details>`. After "Always use *X*", focus moves to that radio. The research dialog wraps Tab itself, as the Convo
  modal already does, and Escape closes the dialog only. No meaning sits in a tooltip or `title` (the host rewrites `title`).
- **Themes:** host tokens only (`--color-text`, `--color-border`, `--color-highlight`, `--color-error-text`,
  `--color-warning-text`, `--color-panel`, `--color-background`); no new hex colours in the card's CSS
  (`settings-split.test.mjs`). Every state has an icon and words; trust levels and badge kinds also differ by border style, so
  nothing depends on colour. Mockups show both themes.
- **Reduced motion:** no transition when a card folds or the mode changes, the research ring stops, and diagnostics already
  slow to 1 Hz.

### 3.11 Structural guards this touches (step 1)

- `tests/settings-split.test.mjs`: `CARDS` gains `voice-mode-field.html`, composed before the three Connections cards. The
  "Connections cards read alike" rule stays for those three. The mode card follows the same host-token and no-inline-style rules.
- `tests/test_settings_coverage.py`: `voice_agent.mode` is bound by the radio's `x-model`. Keys added in later steps are bound
  in the step that adds them, never pending for long.
- `tests/binding-guard.test.mjs`: every `:disabled` reading `ui.voiceMode` is strictly boolean.
- `tests/live-ui-smoke.mjs`: counts one visible stack per tab, so the card must sit inside the Connections stack, not beside it.

---

## 4. Back end

### 4.1 Settings and migration

Step 1 adds one key, outside `capabilities{}`:

```json
"voice_agent": {"mode": "hybrid"}
```

- **Validation** (`convo_contract._validate_providers` or a sibling): `voice_agent` must be an object, and `mode` a string
  matching `[a-z_]{1,32}`. The four known names are the vocabulary. **An unknown name is kept, not refused**, so that settings
  saved by a newer Convo (say `"native"` saved by step 6 and read by step 1, or a mode added later) still load. Start and
  readiness then say "This Convo does not know the voice agent mode '*x*' …". A refused setting would make the whole settings
  file unreadable after a rollback.
- **Absent = `hybrid`.** Existing installs change nothing. No derived field is written and no normalizer is needed.
- **Later keys, each added only by its own step:** `voice_agent.hybrid_voice` (`speech` | `own`, step 5),
  `voice_agent.native_tools` (`classifier` | `off`) and `voice_agent.native_voice` (`own` | `speech`) in step 6,
  `voice_agent.harness` (an installed harness id) and `roles.stt` (a fourth role with the same connection rules) in step 7.
- **Rollback:** before step 1 changes the format, freeze `tests/fixtures/contract_v0_3` (today's `convo_contract.py`,
  `convo_policy.py`, `speech_providers.py`, `convo_model_catalog.py`), as the existing rule requires. Add `voice_agent` to
  `NEW_KEYS` in `test_rollback_compat.py`. Old code merges unknown top-level keys and keeps them on save
  (`test_an_old_editor_saving_a_new_configuration_keeps_every_new_key`). A `roles.stt` passes the old role validation (string
  fields, known capabilities, a local URL).
- **Known rollback gap, stated:** code from before step 1 ignores `voice_agent` and runs Hybrid. Rolling back past step 1 with
  Native chosen therefore runs Hybrid. `tests/update-persistent.ps1 -Rollback` prints a warning when the saved `voice_agent.mode`
  is not `hybrid`. This is a one-line check in step 1.
- `setup_patch` accepts `voice_agent.*` paths automatically once `voice_agent` is in `DEFAULTS`.

### 4.2 Voice profiles: storage, Convo's list, matching

| Store | Where | Written by | Read by the resolver |
|---|---|---|---|
| **Convo's list** (curated) | `helpers/voice_profiles.json`, shipped read-only with the source | maintainers, in a reviewed commit | yes |
| **Your profiles** | `<plugin config dir>/data/voice-profiles.json` (beside `connections.json`) | confirmations, "Keep … for this model", check results; always through `setup_store.transaction` under the settings lock | yes |
| **Drafts** | `<plugin config dir>/data/voice-profile-drafts.json` | research (step 4) and hand imports | **never**: kept in a separate file so that "drafts do not count" holds by construction |

An entry (the same schema in all three stores; `helpers/voice_profiles.py` validates it and drops anything it does not know):

```json
{"id": "gemma-4", "role": "conversation",
 "match": {"providers": ["*"], "models": ["gemma-4-*"]},
 "prefers": "hybrid",
 "facts": {
   "audio_in":  {"value": true,  "evidence": "vendor", "source": "https://…", "read": "2026-11-04"},
   "audio_out": {"value": false, "evidence": "vendor", "source": "https://…", "read": "2026-11-04"},
   "listens_while_talking": {"value": false, "evidence": "vendor"},
   "protocols": {"value": ["chat_completions"], "evidence": "vendor"},
   "tool_calls": {"value": true, "evidence": "vendor"}},
 "gate": null,
 "notes": "plain text, at most 280 characters"}
```

- Fact names and values come from fixed vocabularies. `protocols` ⊂ {`chat_completions`, `openai_realtime`, `openai_live`,
  `nvidia_voicechat`, `harness:<id>`}; the last three are added only with their adapters. `evidence` ∈ {`checked`, `tested`,
  `user`, `vendor`}, plus `agent` in drafts only. `source` must be `https://` and is shown as text. Notes are clipped and
  rendered as text.
- **`role: classifier` entries** may carry `gate`: measured speech-gate floors with their date, method and data kind (for
  example d1-omni-600M, synthetic speech, `calibrate_gate.py`). Readiness can then say whether the floors in effect were
  measured for the classifier in use (an optional step; section 5). Floors stay a per-model fact, re-fit for every fine-tune;
  they are never borrowed.
- **Matching is deterministic** and server-side only:
  - normalize the model id: lower-case; drop an organisation prefix up to the last `/`; drop a `:tag` (Ollama and llama.cpp
    quantisation tags); drop a `.gguf` or `-gguf` suffix;
  - match `providers` (Agent Zero provider ids or `*`) and `models` (globs on the normalized id);
  - an exact name beats a glob, and a glob with more literal characters beats a shorter one;
  - a tie between entries that prefer different modes is **no match** ("ambiguous: matches gemma-4-* and gemma-*-it"), shown
    in the facts panel.
  - Your profiles match the exact (provider, normalized model) only.
  - Cases live in `tests/fixtures/voice-profile-match-cases.json`.
- **Fact precedence:** a fresh check here > you said > tested > vendor. A conflict is kept and shown, never resolved silently.
- **Initial list (step 2).** Vendor-level entries only, re-read from the vendors' own pages at build time. *Tested by Convo*
  entries are added only after a live run on the isolated test host with a recorded result. Today that covers only the
  classifier d1-omni-600M (the gate measurements of 2026-10-15/16, synthetic speech). No conversation model has been tested
  live with Convo yet: section 3 of `UI_BACKEND_CYCLE_RESULTS_20261015.md` used a stand-in.
- **Export and import by hand:** an export writes your confirmed profiles as JSON. An import lands in **drafts** and goes
  through the same review as research.
- **Later, optional:** a decision model could suggest which list entry an unknown name resembles. Its answer would be a draft
  to confirm, never part of Auto's deterministic path.

### 4.3 Capability checks (step 3; own-voice probe in step 5)

All checks are explicit, bounded, refused while a voice session is live, and run with keys attached server-side. No server
text is echoed (as `convo_setup._connection_problem` and `convo_model_catalog.verdict` do today). Results are stored in
`voice-profiles.json` under `checked`, keyed by provider and normalized model, with a fingerprint of provider, address, model
and key *source* (never the key) and the date. A changed fingerprint makes the result stale ("checked for another address").

| Check | Request | Pass means |
|---|---|---|
| Models list | Today's `discover` (GET `/models`) | The address answers and lists the model |
| Audio input | Today's `audio_probe` (one 0.8 s synthetic tone) | The route takes audio. It says nothing about understanding |
| Realtime handshake | Open the protocol's session (OpenAI Realtime: a WebSocket to the provider's realtime route for the model), wait at most 5 s for its session-created event, close. No audio, no response request | The model offers a realtime connection here |
| Own voice (step 5) | One tiny text request asking for audio output on the protocol the profile names (at most about 1 s of audio), never played automatically | Audio came back |

There is no network detection of a harness. A harness is a reviewed manifest file you install (step 7).

### 4.4 One resolver for Auto

`helpers/voice_modes.py` is pure: no network, no writes, no keys.

```text
resolve(config, profiles, checks, last_picks, built) -> Resolution
  chosen      = config.voice_agent.mode (absent: "hybrid")
  eligible[m] = (ok, reason, missing[]) for every mode m:
                built?  hybrid: today's start checks, unchanged
                        native: fresh realtime check + approver (classifier ready, or native_tools off) + active listening
                        tuned:  installed harness made for this model + speech input + speech output
  if chosen != "auto": mode = chosen; step = 1
  else: preferred = [remembered for this model, your profile's prefers, Convo's list prefers, "hybrid"]
        mode = first preferred m with eligible[m].ok; step = where it came from
        none eligible -> refusal listing why each preferred mode is not possible
  confirm     = auto and (mode != last_picks[model] or (no last pick and mode != "hybrid"))
  cards       = card states for the mode (section 3.3)
  flow        = the four "Who does what" rows
  facts       = the merged claims with evidence, for display
```

- **One implementation, server-side.** The browser never mirrors it, because only the server can read your profiles and
  checks. `setup_read` returns `voice_mode` (the resolution for the saved settings). A new pure action, `voice_mode_preview`,
  takes the *unsaved* draft fields: mode, conversation provider, model and API base, plus the classifier and speech-output
  summaries. It returns the same structure, so the card follows edits before Save, debounced at 300 ms with stale answers
  dropped (like Check server). It contacts nothing and writes nothing. `convo.start` calls the same function on the saved
  settings and is the authority. Diagnostics read the start's answer.
- **Session-stable:** the resolution is stored in the session state at start. Settings edits during a session do not change
  it, just as today's System One check refuses mid-session decision changes.
- `last_picks` is written (to `voice-profiles.json`) only when a conversation actually starts in Auto, or when you press
  "Use *X*" / "Keep *Y* for this model".

### 4.5 Readiness rows and start refusals

New readiness rows, appended through the existing `_hook_rows` pattern (a failing hook is one `unavailable` row, never a
failed read):

| id | When | States |
|---|---|---|
| `voice_mode` | always | ready ("Hybrid, you chose it" / "Auto picks Hybrid: Convo's list, vendor says") · missing (the refusal text) · unavailable (unknown mode name) |
| `voice_mode_confirm` | Auto's pick needs your choice | missing |
| `native_realtime`, `native_approver`, `native_listening` | Native chosen or preferred | ready / unverified (no fresh check) / missing |
| `own_voice` | Hybrid with `hybrid_voice: own` | ready / unverified / missing |
| `tuned_harness`, `speech_input` | Tuned | ready / missing |

`incremental_input` is **unchanged** through step 5 ("completed turns only …"). Step 6 changes that contract for Native only:
`unverified` ("streamed while you speak, in Native; not yet accepted by a listening test") until a live listening acceptance.
`AGENTS.md` is updated in the same step.

Start refusals are returned as data (`ok: false, refused: true, message`), in Convo's words. The panel adds "Nothing was sent."

| Situation | Message |
|---|---|
| Mode not built | "Native mode is not in this version of Convo. Choose Hybrid, or Auto." |
| Unknown mode name | "This Convo does not know the voice agent mode '*x*' (saved by a newer version). Choose Hybrid, or Auto." |
| Auto: nothing eligible | "Auto found no mode this version of Convo can run with *model*: *reasons*. Choose another conversation model or mode." |
| Auto: pick changed | "Auto now picks Native for *model* instead of Hybrid. Choose in Convo settings before starting." |
| Native: no fresh realtime check | "Native mode needs a checked realtime connection to *model*. Press Check what it can do in Connections." |
| Native: no approver | "Native mode needs a classifier to approve delegated work, or Delegated work set to Off." |
| Native: ambient or name listening | "Native mode listens actively only. Ambient and name listening are not available in Native." |
| Own voice not checked | "Hybrid with the model's own voice needs a passing voice check for *model*. Press Check what it can do." |
| Tuned: harness, speech input, speech output | "Tuned mode needs a harness made for *model*; none is installed." / "… needs live speech input: set it up in Connections → Speech input." / "… needs a speech output: choose one in Connections → Speech output." |

### 4.6 The seam in `api/realtime.py`

Hybrid is today's code path and stays byte-for-byte the same; the seam is two guards and a registry.

- **At start** (`_process`, `event == "convo.start"`, line 137): the common checks stay first (enabled, AI Link, credential
  references, managed runtime; lines 140–149). Then:
  ```python
  resolution = voice_modes.for_start(config)          # same pure resolver + the profile store
  if resolution.refusal: raise ValueError(resolution.refusal)
  if resolution.mode != 'hybrid':
      engine = ENGINES.get(resolution.mode)            # step 1: {} ; step 6: {'native': NativeEngine}; step 7: tuned
      if engine is None: raise ValueError(resolution.not_built_message)
      return await engine(self).start(sid, data, config, resolution)
  # ... the existing Hybrid start from line 150, unchanged; state['agent_mode'] = resolution.public()
  ```
- **Events** (after the session lookup and the `preparing` / `convo.stop` handling, lines 213–222): if `state.get('engine')`
  is set, every other `convo.*` event goes to `engine.handle(event, data)`. A Hybrid session has no engine, so the existing
  `interrupt`, `retarget`, `notice`, `candidate`, `probe`, `transcript`, `playback` and `turn` handling runs untouched.
  `on_disconnect` (line 64), `disable` and `_cancel` (line 90) call `engine.cancel()` / `engine.close()` when one exists,
  under the same two-second join rule.
- **Engine interface** (`helpers/convo_engines.py`): `start`, `handle`, `cancel(reason)`, `close`, plus `_mark` for timing
  marks. Adapters reuse the shared pieces rather than copying them:
  - the session and epoch store, journal (`accept_turn`, `settle`, `delivered`), dispatcher and `host.direct`;
  - `policy.route_tool`, `ACTION_HOLD` and the reply guard;
  - `_system_prompt`'s voice rules, persona and bounded context;
  - the PCM playback events (`audio.begin` / `chunk` / `end` / `abort`), which already exist for incremental playback.
- **Step 6a, before Native:** extract delegated-work execution (proposal → check → `route_tool` → hold → submit or direct
  call) from `_turn` (around line 1038) into `helpers/convo_actions.py`. This is a behaviour-preserving refactor, proven by
  the behaviour corpus and the classifier-acceptance suite before and after. It is the one Hybrid-adjacent change, and it is
  its own approval.
- **Where the realtime client lives:** in Convo's gateway (`sidecar/app.py`), like `/answer` and `/speech` today, so model
  keys stay in the gateway's private settings (`attach_keys`). The host relays audio between the browser's existing
  authenticated socket and a new gateway WebSocket route. This needs a WebSocket client in the gateway image (decision 10).
  Protocol adapters (`openai_realtime` first; `openai_live` and `nvidia_voicechat` only after their APIs are read and
  checked) sit under one Native engine.
- **Browser** (`audio.js`, step 5/6): the echo guard already releases only original microphone samples and reports withheld
  time as silence. In Native the released frames are streamed (withheld time as silence keeps the timing) instead of being cut
  into utterances by `SpeechBoundary`. Hybrid's capture path is unchanged.

### 4.7 Each mode, piece by piece

| Piece | Hybrid (today, unchanged) | Native | Tuned |
|---|---|---|---|
| Classifier | Every completed utterance plus probes during playback | Only the request that preceded a tool call (its audio, from the model server's input segment), when `native_tools: classifier` | Only the request before a tool call |
| Speech gate floors | `policy_gate`, measured per classifier model | Not used; the model's own turn detection | The harness manifest's floors, per tuned model |
| Speculative reply, `[LISTEN]`, continuation, overheard remarks | Optional settings | Not used (the model hears continuously) | Not used: the harness owns holding and prefill |
| First-clause speech | Optional | Only with `native_voice: speech` | Applies |
| Delegated work | Explicit request ≥ 0.9 → `route_tool` → 0.6 s hold → queue | The model's function call → the same check → `route_tool` → hold → queue. The result goes back as the function output; the spoken confirmation says "queued", never "done" | After the harness's tool hold: the same check, route and hold |
| Speech output | Speech output, or own voice (step 5) | Own voice (default) or speech output (text answers) | Speech output (required) |
| Own voice | Step 5: realtime client with Convo-controlled turns (the protocol's turn detection off; Convo commits the completed utterance) | Realtime client with the model's turn detection | No |
| Barge-in | Probes, interrupt gate, ducking | The model's interruption event: Convo stops playback at once and records where it stopped | The harness's overlap decisions drive duck or stop |
| Echo protection | As today | Adaptive or Off; Strict blocks interruptions and is warned | As today |
| Listening modes | Active, ambient, name | Active only (decision 6) | Active only at first |
| Journal | Completed turns; delivered phrases | User turns from the model server's transcripts; assistant audio marked delivered up to where playback stopped | Final transcription segments; delivered phrases |
| Locality and keys | `local_host` for every connection; remote needs consent plus HTTPS | The same rule for `ws://` / `wss://` (public needs consent and `wss`); keys in the gateway only | The same rule for speech input and the harness server |

### 4.8 Diagnostics

- A mark `mode.resolved` (typed fields only: mode, step, evidence kind, whether it was confirmed; no model text), and the Live
  strip line.
- **Native marks:** `rt.speech_started`, `rt.speech_stopped`, `rt.response_started`, `rt.audio_first`, `rt.interrupted`
  (with the playback position), `rt.tool_call`, `tool.check`, `tool.result`.
- **Tuned marks:** per-turn counts of talk / wait / stop decisions, tick latency (p50 and max over the turn), and
  transcription partial and final delays.
- The Speech gate section shows "Not used in Native" or the harness's floors in Tuned. All marks keep the existing bounds
  (20 turns, 2000 marks) and the content-free rule.

### 4.9 System One's decider

- In **Hybrid** the classifier slot is unchanged: Convo's own decision classifier, System One's decider, or a chat model.
  Your fine-tuned **decision** models go there, with their own measured floors (a classifier profile's `gate`).
- In **Native and Tuned** the classifier (any of those three) has one job: approving delegated work. Its readiness rows
  (`decider`, `speech_gate`) say "used for approvals only".
- Auto never selects, changes or loads a classifier or decider, and never touches System One's settings. A mode change never
  registers or unregisters a decision backend (`convo_d1_decider` is untouched).
- A **Tuned** harness may one day use System One's typed questions for its decisions. That would be declared in its manifest
  and measured, not assumed.

### 4.10 Provider Profiles (the coming plugin)

- The names stay apart: Convo's per-model facts are **voice profiles**; Provider Profiles keeps per-provider defaults
  (addresses, the model last used with each provider) and reaches Convo's cards through the existing
  `onProviderProfileChanged` hook (`model-role-setup.js`).
- Convo never writes Provider Profiles' data. Voice profiles are keyed by the same Agent Zero provider ids, so one model
  selection means the same thing in both.
- If Provider Profiles later publishes a model's declared modalities, Convo may show them as one more source, labelled
  "Provider Profiles says" at vendor level. They never count as checked or tested.

---

## 5. Build order

Every step is its own approval. Production (`agent-zero-v2.11`) is never touched; live checks run on the isolated test host
(`127.0.0.1:5028`) after `tests/update-persistent.ps1 -Target test`. Offline tests run without models, GPU or network.

| Step | What | Files (main) | Offline tests | Live checks on the isolated host | Needs your approval for |
|---|---|---|---|---|---|
| **1** | Mode setting and card; Hybrid only; Native and Tuned shown as "Not in this version"; start refuses them | `convo_contract.py` (DEFAULTS, validation), `helpers/voice_modes.py` (explicit choice only), `convo_setup.read` (row), `api/realtime.py` (start guard), `webui/voice-mode-field.html`, `voice-mode-setup.js`, `setup-store.js` (compose), `settings-controls.html`, `main.html` (chip), freeze `tests/fixtures/contract_v0_3`, rollback warning in `update-persistent.ps1` | `test_voice_modes.py`; `test_rollback_compat.py` (`NEW_KEYS`); `test_settings_coverage.py`; `settings-split.test.mjs` (`CARDS`); `voice-mode-ui.test.mjs`; `binding-guard.test.mjs`; a behaviour-corpus run showing Hybrid unchanged | `live-ui-smoke.mjs` (card first in Connections, both modals, both themes, 390 px); Native start refused with its message; a Hybrid voice turn still works; rollback warning printed | The step |
| **2** | Auto, voice profiles, Convo's list, facts panel (read-only evidence), `voice_mode_preview`, the preset-editor line, the diagnostics line | `helpers/voice_profiles.py`, `helpers/voice_profiles.json`, `voice_modes.py` (resolver), `api/conversation.py` (preview action), `conversation-model-field.html` (preset line), `diagnostics.html` | Matching cases fixture; resolver table tests (each step, ties, unknown names, confirm rule); preview is pure (no network, no write) | Auto shows its pick and steps for the configured model; changing the model in the preset editor updates the line; Auto change banner and refusal | The initial list's entries (each re-read from its vendor page, with dates) |
| **3** | "Check what it can do" (models, audio input, realtime handshake), the `checked` store, "Keep *X* for this model" | `voice_profiles.py` (store), `convo_model_catalog.py` (handshake beside `audio_probe`), card methods | Stubbed WebSocket and HTTP; refusals while live; fingerprint staleness; no server text echoed | The handshake against a local server and, only if you provide a key, a cloud realtime model | Any cloud call (costs, consent) |
| **4** | "Research this model" with drafts and review | `voice_profiles.py` (drafts), a host research call (Agent Zero's main model with web search in a temporary context: the facility is to be confirmed on v2.11), the review UI | Draft schema validation (drops unknown fields, non-https sources, long quotes); confirm and discard; the resolver never reads drafts | One research run on the test host, with the search provider you choose | Which Agent Zero facility runs it, and whether it may use web search |
| **5** | Gateway realtime client; Hybrid with the model's own voice (Convo-controlled turns); own-voice check | `sidecar/app.py` (WebSocket route), `sidecar/requirements.txt`, `helpers/convo_transport.py`, `convo_engines.py`, `audio.js` (PCM playback reuse), `voice_mode` rows | Protocol adapter against a scripted server; epochs, interruption, two-second cancel; behaviour corpus unchanged for `hybrid_voice: speech` | LFM3-Omni-2B (or another omni model) on the test host: route, one spoken reply, interruption; no listening claim without a listening test | The gateway dependency; copying or downloading the model (reviewed manifest) and any GPU load |
| **6a** | Extract delegated-work execution to `convo_actions.py`, behaviour-preserving | `api/realtime.py`, `helpers/convo_actions.py` | Behaviour corpus and classifier acceptance before and after, identical | A Hybrid delegated task on the test host | The refactor |
| **6** | Native engine (OpenAI Realtime protocol first), the classifier as approver, active listening only, Native marks | `helpers/convo_native.py`, `convo_engines.py`, `api/realtime.py` (registry), `audio.js` (streaming capture), diagnostics, `AGENTS.md` (`incremental_input` contract) | A scripted realtime server: barge-in, truncation, tool call → check → hold, revoked epochs, disconnect cleanup | A realtime model on the test host: talk-over, interruption, a delegated task approved and one refused, latency marks | A cloud key and costs, or a local duplex runtime; the privacy note's wording |
| **7** | Tuned engine, harness manifest format, speech input role (`roles.stt`), Tuned marks | `helpers/convo_tuned.py`, harness manifest schema, `roles.stt` across contract, setup and cards | A scripted harness and transcription stream; floors per model | Your fine-tuned model with its harness on the test host | Tied to your fine-tuning work: model files, training and downloads stay separate checkpoints |
| optional | Classifier profiles carry measured floors; readiness says whether the floors in effect were measured for the classifier in use | `voice_profiles.py`, `convo_setup.read` | Floors-mismatch row | Readiness on the test host | Whether you want it |

---

## 6. What could not be checked from this session

This design was written in a cloud session that cannot reach your laptop, the isolated test host (`127.0.0.1:5028`), local
model servers or the separate System One repository, and it ran no models, GPU, training or Docker. These stay **live checks**,
not facts:

- the mode card's rendering inside the real host modals (the mockups imitate host tokens and classes; they are not the host);
- which Agent Zero facility can run "Research this model" with web search in an isolated context on v2.11;
- whether Agent Zero's image and Convo's gateway image already have a WebSocket client (the repo's `requirements.txt` lists
  `python-socketio`; the gateway's lists none);
- whether socket.io between the browser and the host can carry continuous 20–40 ms PCM frames with acceptable delay;
- every model capability in section 7. The vendors' pages could not be opened from here (below).

## 7. Sources

**Re-check on 2026-11-04, limited.** From this session, `huggingface.co`, `docs.liquid.ai`, `catalog.ngc.nvidia.com` and
`openai.com` did not resolve: the environment's network policy blocks them. **None of the vendor pages could be read.**
What follows comes from web-search summaries of those pages on 2026-11-04. It is recorded as *unverified*, and every item
must be re-read from the vendor's own page before it enters Convo's list (step 2) or an adapter (steps 5–7). The 2026-10-31
reading is kept beside it.

| Item | Recorded 2026-10-31 | Search summaries, 2026-11-04 (unverified) |
|---|---|---|
| LFM3-Omni-2B (Liquid AI) — https://huggingface.co/LiquidAI/LFM3-Omni-2B, https://docs.liquid.ai/lfm/models/lfm3-omni-2b | text/image/video/audio in; text, speech or interleaved out | Same modalities; open weights under the "LFM Open License v1.0". Runtimes: Transformers (speech out), SGLang (text out only), llama.cpp support reported but possibly still awaiting merge; speech output reported through a llama.cpp **realtime** WebSocket route, not chat completions. Full duplex not stated by the vendor |
| NemotronLabs VoiceChat (NVIDIA) — https://catalog.ngc.nvidia.com/orgs/nim/nvidia/models/nemotron-labs-voicechat | open, English only, ~11B, full duplex | "Real-time speech-to-speech full duplex"; English only; weights on Hugging Face under OpenMDW 1.1 (the NIM container under NVIDIA's evaluation licence); a **bidirectional WebSocket interface with function calling**, of its own. OpenAI Realtime compatibility not found. Release date and size (11B vs 12B) inconsistent between pages |
| GPT-Live (OpenAI) — https://openai.com/index/continuous-voice-interaction-with-gpt-live/ | API not generally available yet | **Possibly changed:** an "introducing GPT-Live-1 in the API" post dated 2026-09-10 and a changelog entry calling it generally available; model id `gpt-live-1`; sessions on its own route (not the Realtime route); listens and speaks at once; hands reasoning and tools to a backend model. Must be re-read before it is planned as an adapter |
| OpenAI Realtime API — https://developers.openai.com (Realtime guide) | `gpt-realtime` | WebRTC, WebSocket and SIP; current models reported as `gpt-realtime-2.1` and `-2.1-mini`; output is audio *or* text; function calling; `server_vad` / `semantic_vad`, or turn detection off (push-to-talk, which is what Hybrid's own voice would use, decision 9); with WebSocket the client handles truncation after an interruption |
| Speakrail — https://github.com/speakrail/speakrail | (see the backend reference audit in the Convo plan, Part B) | Apache-2.0; a Gemma 4 12B QAT LoRA and a turn head on Voxtral Mini 4B Realtime that classifies speaking / finished / pausing / backchannel every 80 ms; **external TTS required** (its default is research-only, non-commercial); English only. Author's latency claims not verified |
| Codacus micro-turn video | Qwen3.6-35B-A3B LoRA | The video found ("How I Built an End-to-End Local Voice Agent, and Made It Fast") is not confirmed to cover micro-turns. The only description of a Qwen3.6-35B-A3B micro-turn LoRA is on a third party's model card. **Not confirmed**; the decision tokens are unknown |
| Gemma 4 audio input | Gemma 4 with audio input | Audio input reported for the E2B and E4B sizes (up to 30 s); llama.cpp serves it with a multimodal projector; community reports call it unreliable there |

**To re-read the vendor pages from a session like this one**, allow those hosts in the environment's network settings:
`huggingface.co`, `docs.liquid.ai`, `catalog.ngc.nvidia.com`, `openai.com`, `developers.openai.com`, `github.com`,
`youtube.com`, `ai.google.dev`. Or re-read them from your own machine.
