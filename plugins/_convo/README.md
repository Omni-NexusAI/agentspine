# Convo

Convo is a voice-conversation plugin for Agent Zero. You speak, the conversation model hears your own audio, and the answer is spoken back in the voice you chose; longer work is handed to the selected Agent Zero chat. It is a plugin-owned successor to Enhanced Speech, keeps the host's dictation, and does not modify Agent Zero's core.

**Status:** in development. It is used daily on a test host and is not a stable release yet. The automated tests cover the code paths described here; they do not replace listening tests with a real microphone, real models and a real room. [ACCEPTANCE.md](ACCEPTANCE.md) lists what must hold before a stable release.

The companion plugin `plugins/convo_d1_decider` is a development-only backend that connects a decision model to Convo's classifier.

## How it works

**Listening**

- The browser finds where an utterance begins and ends and uploads the completed utterance as 16 kHz mono audio. A conversation model that accepts audio input hears it directly (voice, tone, pace); a text-only model gets Whisper's transcript instead.
- Input is completed turns only. Nothing is streamed into the model while you speak, and Convo does not claim duplex operation.

**Deciding whether and when to answer**

- A separate classifier reads short probes while you speak and then the finished utterance. It runs beside the conversation model, never in place of it, and there is no fallback to another classifier.
- In a conversation you started, a finished turn is presumed to be meant for the assistant and is answered, unless the classifier is confident it was not: a short acknowledgement, or talk to someone else. Ambient and name listening use stricter thresholds.
- The conversation model may still decline a turn: `[SILENT]` (noise, speech meant for someone else, a request not to respond) or `[WAIT]` (an unfinished thought). A declined turn is not spoken, shown or stored as a reply. What you say next within `continuation_seconds` joins an unfinished thought and is answered as one.
- A finished turn the classifier could not judge (slow, busy or failing) is asked once more. In a conversation you started it is then answered without the classifier's reading, and you are told once.
- Behaviour comes from meaning, never from particular words: no code path matches a phrase or a word list, in any language.

**Speaking over a reply**

- Detected speech only lowers the playback volume. The classifier then reads it: a request, a question or a stop ends the reply with a short fade; an acknowledgement or side talk lets it go on.
- Independently, your confirmed voice over the reply for `barge_in_seconds` ends it whatever the classifier says. Stop releases the microphone at once.
- The history keeps what you actually heard of an interrupted reply and marks it interrupted.

**Speaking**

- A reply is spoken phrase by phrase through the speech provider: the first full sentences early, the rest in as few requests as possible, in order, with one voice, one seed and one language for the whole reply.
- Markdown, reasoning, tool markup and the hidden header lines are never spoken. A model that writes a tool call as plain text has that markup removed and read as a tool proposal.
- A buffered WAV per phrase is the default. Incremental PCM playback is an opt-in, experimental path for a speech server that provides it.

**History and memory**

- An audio turn asks the model to begin with hidden `USER:` and `LANGUAGE:` lines. The first is stored as the turn's words: the history is the model's own report of what it heard.
- With transcripts turned on, Whisper runs afterwards in Convo's own worker process. Its text is kept beside the model's line and becomes the record only when the model reported nothing or the two disagree strongly. A declined turn keeps no Whisper text, so noise never becomes "speech" in the history.
- There is no turn limit. In quiet moments older turns are summarised into chapters, with short standing notes (your name, the language you asked for, lasting preferences). Speech stops that work and it resumes later; it never delays, cancels or refuses a turn. A request that still does not fit leaves its oldest turns out of that one request.
- The full event journal (SQLite, under `usr/plugins/_convo/data`) is kept apart from what the model sees. Once anything is summarised the model can search it with `recall_conversation`.

**Tools and delegation**

- The voice agent does quick things itself and hands longer work to an Agent Zero chat's agent. Its tools: search the web and read a public page; read long-term memory and (after your yes) save to it; hand over a task, check on it, change it and cancel it; check the machine and the voice services; the time anywhere; timers and reminders; list chats and read one; open a chat, Settings, the file browser or a link; speak slower or faster; listen again to the last sounds it heard; look through a camera or at a screen you share with a button; look up what was said earlier in the conversation.
- Settings has one switch per group of tools ("What the voice agent may do"). A group that is off is not offered to the model at all. A model that takes tools in writing is offered a smaller set.
- Read-only tools run directly, up to three in a row in one reply. A small visible change (a timer, opening something) runs directly in a conversation you started. Work that is started, stopped or saved runs when your own words asked for it; when Convo cannot tell that, it asks you first ("shall I go ahead?") instead of refusing. Your spoken yes or no is read from your own utterance by a separate, isolated request, so no text in the conversation can answer for you; the panel shows the same question with Run and Dismiss. Nothing that changes anything runs right after the model has read a tool's result.
- Web search uses Agent Zero's own search service through a ladder: the ordinary request first; when it returns nothing, the news category and up to four further engines the service has loaded. At most three requests and eight seconds per search; the same search within 90 seconds is answered from its last result. No paid search key is used. When the search gets nothing (the engines refuse, or return nothing), the question is handed to the chat's agent, which looks it up with its own browser, and the answer is spoken when it comes back.
- Reading a page is Convo's own bounded fetch of public addresses only, without cookies or sign-ins.
- The camera and the screen are offered only while you share them and the model has been measured to accept images. One frame is taken when the model asks, and it is never stored.
- Work that is going on can be changed by voice or with Steer in the panel: with the Steering plugin (`plugins/steering`) the task goes on and takes the change in at its next step; a change that comes too late becomes a task of its own. Without that plugin a message to a running task interrupts the step it is on, and the button says so.
- A handed-over task is queued after a short hold, so an immediate correction can replace it. The model says it is queued, never that it is done. Jobs keep the chat they were submitted for and continue when voice stops; a finished task is spoken in the conversation that asked for it.
- Every tool use is journaled and shown in the panel, and a result reaches the model labelled as untrusted data.

**Permissions and reasoning**

- Two small menus sit beside the chat's name in the Convo panel, in the style of Agent Zero's own model menu.
- **Permissions**: *Ask when unsure* (as before: it asks before it starts, changes or saves anything it is not sure you asked for), *Plan* (planning mode, below) and *Allow without asking* (it does what you ask without asking first; it still asks when text from the web or a chat could have asked for something). Each conversation keeps its own choice; Settings, Conversation, sets what a new one starts with. When Convo does ask, *Always* (a button on the question, or saying so) runs it and stops asking about that kind of work; the menu lists those and takes them back.
- **Reasoning**: how much the conversation model thinks before it answers. *Auto* answers at once and lets the model think first when a question needs it (it says so in a few words, then thinks); *Off* never thinks; *Low*, *Medium* and *High* think on every answer, which makes the first words come later. It can also be changed by voice ("think harder about this", "answer quickly"). Which levels a model really takes is measured by its connection check; until then the menu says so, and a model that only switches thinking on or off treats all three levels alike.

**Planning**

- Say that you want to plan something, choose Plan in the Permissions menu or set a hotkey for it. Convo may also switch it on itself when you clearly start planning, and says so: it only makes it more careful. It ends only on your word.
- While planning it talks and looks things up, more than usual in one reply. Anything that starts, changes or saves something is asked about once ("we're still planning, do that now?") and only that one thing runs on your yes. Tasks that had not started yet wait. The voice's speed changes at once, and stopping a task you plainly ask to stop does too.
- The plan (goal, decisions, open questions, steps, ideas set aside) is written by Agent Zero's main chat model beside the conversation, so nothing waits for it, when that model runs on your own machines or you allowed it to receive the planning conversation (Settings, Connections, Who writes the plans); otherwise by the conversation model in quiet moments, stopping the moment you speak. It shows in the panel, where you can edit it; what you change stands. A model outside your network writes it every third exchange.
- Nothing you write in the plan is lost: an edit Convo wrote under asks which one to keep, "What stood here before" lists the texts that were replaced and puts one back, before a line you wrote yourself is left out or changed the model is asked about exactly that line, and an unsaved edit is saved to its own plan when another one takes its place.
- It survives a reload, a lost connection, a restart and stopping voice; the next start says that you are still planning. One plan is worked on per conversation; earlier ones, handed-over ones too, can be gone on with, by their name or from the panel's list. The plan in hand is put away only on your word.
- Finishing writes the final plan, says it in a few sentences and asks whether to hand it to the chat's agent as one task. When the final text takes longer than a moment, Convo says it is writing it up and the conversation goes on; it is read out with the question as soon as it is ready. If you say no it stays in the panel, to copy, hand over later or go on with. "Yes, but …" takes your change along. It is handed over once, exactly as it was read out; asking to hand over a plan you are still working on finishes it and reads it out first. A plan whose task is cancelled or fails is there again, and going on with one whose task has not started yet takes that task back.
- With a conversation model on your own machines (or when you switch it on) it can also look at the files of the chat's project or work folder: read-only. Files and lines that look as if they held keys or passwords are left out; a secret that looks like ordinary text cannot be recognised, so keep such files out of the folder.

**One conversation across chats**

- By default a conversation belongs to the selected chat. With "One conversation across chats" (or when voice is started with no chat selected) there is one conversation, with one memory, whichever chat is open; selecting another chat only changes where work goes by default.
- A task goes to the chat you name, or to a new chat, or to the selected chat, as the setting says. Finished tasks are reported back in that one conversation.

**Echo protection and the Sound panel**

- `echo_guard` is adaptive (default), strict or off. The guard compares the microphone with the exact playback output and releases only original microphone samples once independent speech is confirmed; echo and ambiguous audio are withheld.
- The Sound panel has the noise gate with a level meter, Measure room, Interrupt after, End of turn, echo protection, the output device with a test sound, microphone processing and, on phones, the audio mode. Values are kept per device in the browser; saved settings are only the defaults.

**Also**

- One voice conversation at a time, owned by one page. Another device is refused with the owner named unless it takes over; a dropped connection can re-attach for 20 seconds.
- The assistant speaks as the selected chat's agent profile, or a pinned one. The persona is extracted in the background and never blocks a turn.
- The orb on the composer: a tap toggles Convo, a longer hold starts the host's hands-free dictation, a still longer hold is push-to-talk. One microphone capture at a time.
- Developer mode adds a diagnostics panel: content-free timing marks per turn, the classifier's probabilities, "What the model saw / wrote" for the last ten turns, the memory view, test clips, "Record raw and sent" and "Save for replay".

## Setup

Enable the plugin in Agent Zero's plugin manager and turn on "Enable Convo composer control" in Convo's settings. Convo's own runtime (the gateway that calls your models during a conversation) starts with Agent Zero whenever Convo is on and needs nothing installed. Running a gateway yourself is an advanced choice on the "Convo runtime" card; `sidecar/` holds its development packaging.

Convo needs three connections. All three are cards on the Connections tab of Convo's settings (the gear in the Convo panel, or the plugin's Config). Opening the settings contacts nothing. The conversation card checks its connection by itself when you change its address or model; the other cards check when you press Check server or Find models & voices.

1. **Conversation model.** An OpenAI-compatible chat-completions address such as `http://<host>:<port>/v1`, served by a local server or by a cloud provider. For direct audio choose a model that accepts audio input. Once the address and model are entered, Convo lists the server's models and, when yours is listed, sends three tiny requests to learn whether it takes audio, calls tools natively and accepts images (on a cloud model these are small paid requests); it then uses the model accordingly. Test connection, in the card's Advanced Settings, runs the same check on demand. The same setting appears as a model type in Agent Zero's preset editor.
2. **Classifier.** One of: a decision model (it returns a probability for every kind of speech; served by a local decision server and connected through `convo_d1_decider`), System One's decider (see [SYSTEM_ONE.md](SYSTEM_ONE.md)), or an OpenAI-compatible chat model. Only a decision model gives the measured speech gate; a chat classifier keeps fixed thresholds and readiness says so. In an audio conversation the classifier must accept audio; one that cannot is refused at start.
3. **Speech output.** Kokoro (already loaded in Agent Zero, with its voices cached), an audio.cpp server you run, llama.cpp, Unsloth Studio, OpenAI, or any OpenAI-compatible speech endpoint such as `http://<host>:<port>/v1`. Audio.cpp managed by Convo, with Voice Studio for clone profiles, is optional and needs separate, explicitly approved setup; see `sidecar/AGENTS.md`.

Addresses on this machine and on your own network (LAN, Tailscale) are used as they are. Any other address needs that connection's remote consent and HTTPS. Keys are stored by the host, bound to the address they were entered for, and never returned to the browser. The speech gate thresholds were measured for one decision model on synthetic speech: measure them again (`tests/calibrate_gate.py`) for another model or for real voices.

## Tests

Python, from the repository root:

```sh
python -m unittest discover -s plugins/_convo/tests -p "test_*.py"
python -m unittest plugins.convo_d1_decider.tests.test_provisioner
```

Node, from `plugins`:

```sh
node --experimental-vm-modules --test _convo/tests/*.test.mjs
```

- The tests use temporary directories and scripted providers. They touch no installed settings, model or service.
- The gateway route tests need `httpx` and Starlette in the interpreter. Tests for optional parts (Gradio, FastAPI, Flask, throwaway certificates) skip when their package is missing.
- The System One tests (`test_system_one_consumer.py`, `test_classifier_acceptance.py`, `test_decider_reference.py`) skip unless they find System One's SDK: set `CONVO_SYSTEM_ONE_TEST_SDK` to a folder that holds its `media_decision.py` and `SYSTEM_ONE_TEST_CORE` to one that holds its `decision.py`. A System One core checkout holds both, so both can point at it.
- `tests/update-persistent.ps1` copies the plugin's source into an installed Agent Zero host. It previews by default and copies only with `-Apply`; Convo must be switched off first (`-WebOnly` allows browser files while it is on). It never touches settings, `data/` or toggles, backs up every file it replaces, and restores with `-Rollback -BackupStamp`. `-Plugin convo_d1_decider` deploys the companion plugin. Its targets are fixed to the maintainer's hosts; change them before using it elsewhere.
- `tests/live-*.mjs` check the real interface on an isolated loopback test host with a throwaway headless browser, for example `node tests/live-ui-smoke.mjs http://127.0.0.1:<port>/`. They refuse any other address, start no voice session, and put back any setting they change. They are not part of the Node run above; run them after deploying to a test host.

## Privacy and limits

- Local first. Defaults point at local addresses and nothing is activated without an explicit choice.
- No automatic fallback. When a model, classifier or speech provider fails, Convo reports it; it never switches to another provider.
- A remote address needs consent for that connection and HTTPS. Ambient listening with a remote classifier needs its own consent.
- Conversation audio is held in memory only and never written to disk. The exceptions are developer mode's "Record raw and sent", test clips and "Save for replay", each only when you press it, each listed and deletable.
- A camera picture goes to the model for one reply and is never stored or journaled.
- Timing marks hold names and numbers, never audio, words or credentials.
- Summaries are written by the conversation model. Agent Zero's utility model writes them only when it runs on your own machine or network, or with your consent.
- Enabling the plugin installs nothing and starts or downloads no model. Whisper is used only from a model file that is already complete on disk. At the start of a conversation, a router that loads models on demand is asked to load the configured ones, and the page says what it is waiting for.
- Turning the plugin off ends voice sessions and releases the microphone. Histories and settings stay, and work already handed to a chat continues.

## Not done yet

- Listening acceptance with a real microphone, real models and a real room, and the measurements in [ACCEPTANCE.md](ACCEPTANCE.md).
- Voice agent modes (tuned harness, native duplex, auto) are a design only: [VOICE_AGENT_MODES.md](VOICE_AGENT_MODES.md). Today's runtime is the hybrid foundation it describes.
- The decision model does not carry out spoken commands itself: measured on held-out speech it could not tell a spoken yes, no, "repeat that" or "slower" from other speech, so those go through the conversation model.
- Opening apps on your PC and tools from MCP servers are not built.
- Incremental or duplex audio input. The model receives completed turns.
- Interruption: the thresholds are starting points not yet measured on real overlapped speech, an interruption shorter than about half a second of voice is not detected, and there is no undo for a reply that was cut.
- The speech gate is fitted to one decision model. The decision plugin is development-only and still carries that model's address default, question set and thresholds; these are to become per-model profiles.
- Not proved live: cache reuse and slot pinning on a real llama.cpp server during memory upkeep, Whisper load times in the worker, and incremental PCM playback under combined load.
- Voice Studio: an operation whose outcome is uncertain blocks further Studio writes and a voice start, and there is no reset yet.
- Open observations: a repeated acknowledgement is sometimes answered twice, and background noise is sometimes answered.

## Design notes and attribution

- [AGENTS.md](AGENTS.md) is the binding contract for this plugin; `webui/AGENTS.md` and `sidecar/AGENTS.md` cover the interface and the gateway.
- [SYSTEM_ONE.md](SYSTEM_ONE.md) and [VOICE_AGENT_MODES.md](VOICE_AGENT_MODES.md) are design notes.
- The echo guard (`webui/echo-guard.js`), the continuation tracker (`helpers/convo_continuation.py`) and the phrase pacing (`helpers/convo_firstclause.py`) are adapted from the same owner's standalone speech-to-speech fork (Apache-2.0); each file's header names its source.
- `sidecar/audiocpp/NOTICE.md` records the vendored speech runtime, the adapted Voice Studio and the audio.cpp engine revision; the Apache-2.0 text is in `sidecar/audiocpp/LICENSE.speech-to-speech`. No model weights or voice profiles are bundled, and model licences are shown before an explicit download.
