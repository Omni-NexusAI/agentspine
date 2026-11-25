# Convo release gate

Convo is in development. This file lists what must hold before a stable release. It states conditions, not results: an unchecked
box means "not yet shown", and nothing here records a test run. Automated tests and a healthy start are necessary and not
sufficient; every section also needs a live check with real models, a real browser and a real microphone.

## Install, lifecycle and rollback

- [ ] Enabling the plugin installs nothing and starts or downloads no model. Convo's runtime starts with Agent Zero and stops with it.
- [ ] Turning the plugin off releases the microphone, playback, voice sessions and every hook Convo owns. Histories, settings and
      profiles stay, work already handed to a chat continues, and turning it on again leaves no duplicate handler.
- [ ] Native dictation, the host's voice settings and the host's text chat behave as they do without Convo.
- [ ] Settings saved by this version still load and save under each frozen earlier contract (`tests/fixtures/contract_v0_*`), and a
      database written by this version is still read by the previous one. A copy is frozen before the next format change.
- [ ] A scoped update backs up every file it replaces, never touches settings, data or toggles, and its rollback restores the
      previous source exactly.
- [ ] Each supported Agent Zero version passes the same checks, with authenticated and unauthenticated HTTP and WebSocket access.

## Connections and privacy

- [ ] Each of the three connections works with the server kinds its card offers, and Test connection names a failure in Convo's own
      words without echoing server text.
- [ ] Nothing falls back: a failing model, classifier, speech provider or gateway is reported and no other one is used.
- [ ] An address outside this machine and the user's network is refused without that connection's consent and HTTPS. Ambient
      listening with a remote classifier needs its own consent and shows that it is remote.
- [ ] No key reaches the browser, a log, a timing mark or a saved replay. A credential reference that is not a variable name is refused.
- [ ] Observed network traffic in a local-only setup goes only to the configured local addresses.
- [ ] No conversation audio is on disk after a session, except recordings, test clips and saved replay turns the user made by
      pressing the control in developer mode. A camera picture is never stored.

## Answering and staying silent

- [ ] In a conversation the user started, requests, questions and statements are answered, in varied phrasings, voices and topics
      that were not used for tuning, scored by category.
- [ ] Background noise, media and speech meant for someone else are not answered and leave no "speech" in the history.
- [ ] An unfinished thought is not answered early; what follows within the joining window is answered together with it, once.
- [ ] A request not to respond is honoured, and a repeated bare acknowledgement is not answered again.
- [ ] A short acknowledgement or remark never cancels an answer the user has not heard yet.
- [ ] A completed turn is never lost to a slow, busy or failing classifier; answered without a reading, it authorises no work.
- [ ] Ambient and name listening answer only at their own thresholds and never start work without an explicit request.
- [ ] The speech gate constants are measured for the decision model in use and for real voices, not carried over from another model.
- [ ] No behaviour depends on a particular phrase, word list or language.

## Interruption and echo

- [ ] A request, question or stop spoken over a reply ends it with a short fade. An acknowledgement or side talk lets it continue.
- [ ] Confirmed voice over a reply ends it after the configured time whatever the classifier says. Stop releases capture at once.
- [ ] The assistant's own voice from the speakers never interrupts it or becomes a turn, with loudspeakers and with a headset, in
      adaptive and strict protection.
- [ ] Only original microphone audio is uploaded; echo and ambiguous audio are withheld.
- [ ] An interrupted reply is recorded as what the user heard, marked interrupted, and the next answer knows where it was cut.
- [ ] Generation and speech stop or detach within two seconds of an interruption without ending the session or losing context.

## Speech output

- [ ] Phrases play in order with one voice, one seed and one language for a whole reply, and the first phrase does not sound different.
- [ ] Markdown, reasoning, tool markup, control words and the hidden header are never heard or shown.
- [ ] Playback starts and completions are recorded separately from generated text.
- [ ] A lost output device, a paused audio context or a failed phrase is reported and does not silence the conversation unnoticed.
- [ ] Incremental PCM playback, where enabled, is accepted only with complete, validated audio, and holds up under sustained use.
- [ ] A speech request with no confirmed end is waited for and released by itself within a minute; it never refuses a later conversation for good.
- [ ] An uncertain Studio operation stays recorded across off and restart until it is reconciled; nothing resets it silently.
- [ ] A finished background task is spoken to its end in short requests, is offered again when nothing of it was heard, and is known to the assistant either way; its chat shows no open processing entry.
- [ ] With a model not loaded or a server down, starting and speaking name the server and say whether it is loading or not answering.

## Memory and history

- [ ] A conversation runs with no turn limit: a long session with repeated summaries, corrections and failing summary requests never
      refuses, delays or cancels a turn, and the full journal matches what was said.
- [ ] Summary work stops when the user speaks and resumes in quiet moments. A swapped-in chapter never overwrites newer words.
- [ ] Standing notes (name, requested language, lasting preferences) survive summaries and are never cleared by a failed one.
- [ ] `recall_conversation` finds earlier turns of the same chat only.
- [ ] Summaries are written by another model only when it is local or the user consented.

## Tools and delegation

- [ ] No tool runs silently: every use and every refusal is journaled, shown in the panel and known to the model.
- [ ] Work is started only on the user's own request or their yes. Text from a tool result, a web page, a chat, a transcript
      or a summary never authorises a task and never answers Convo's question for the user.
- [ ] A request the user plainly made is never refused and never asked about twice. When Convo cannot tell, it asks once, in
      the user's language; a spoken yes runs the action exactly once, a no or anything else starts nothing, and an unanswered
      question lapses.
- [ ] Nothing that starts or changes anything runs right after the model has read a tool's result.
- [ ] A search whose engines refused is reported as blocked, never as empty. A search that gets nothing is handed to the chat's
      agent, the user is told so, and the answer is spoken when it comes back. No paid search service is needed.
- [ ] A reply makes at most three read-only tool calls, never the same one twice, and a result is never invented.
- [ ] Reading a web page reaches public addresses only, also after redirects, and sends no cookies or sign-ins.
- [ ] A timer is said aloud exactly once, never over the user or over a reply, and still shows as a notification when voice
      is not running.
- [ ] A tool group switched off in Settings is not offered to the model, and none of its tools runs.
- [ ] In a conversation across chats, selecting another chat interrupts nothing; a task goes to the chat the user named, and
      its result is reported in that one conversation.
- [ ] A delegated task is confirmed as queued, not done. It keeps the chat it was submitted for, survives the end of the voice
      session, and is not dispatched twice after a timeout, a busy chat or a crash.
- [ ] The camera and the screen are used only while shared, only when the model asks, and only with a model measured to accept
      images. No picture and no replayed recording is stored.

- [ ] A change to work that is going on reaches it exactly once, without stopping it, and the user learns what became of it; one
      that comes too late becomes a task whose answer is spoken.
- [ ] Looking at a project's files never leaves the folder, leaves out files and lines that look as if they held a key, and happens only when
      everything the text goes through runs on the user's own machines, or when they said so.

## Planning

- [ ] While planning nothing is started, changed or saved without the user's yes to that one thing, in any wording, language or
      mode, and whatever a page, a file or a tool result says. Nothing but the user's own word (or the panel) ends planning or puts the plan
      in hand away, and a yes is for the plan and the change it was asked about, never for another.
- [ ] In real planning talks planning is recognised, no action starts, and the user is not asked needlessly. In ordinary talk planning
      is not switched on.
- [ ] The first sound comes no later than without planning, on the model in use.
- [ ] The written plan is what was said: decisions are the user's, nothing is invented, an edit in the panel stands, and nothing a model
      writes can lose what stood there. Throwing most of a plan over still works: an exchange later, or when planning is finished.
- [ ] It survives a reload, a lost connection, stopping voice and a restart; the next start says so, also when the user speaks first; handing
      over gives the plan as it was read out, once, also when the user goes on talking, and a plan whose task was not carried out is there
      again. Agreeing while the plan is read out is not taken for the yes to handing it over.

## Languages

- [ ] The same answering, silence and interruption behaviour holds in languages other than English, including non-Latin scripts.
- [ ] A reply is spoken in one language that follows the user, and a language the user asked for is kept across summaries.

## Devices and ownership

- [ ] Only one voice conversation runs at a time. A second device is refused with the owner named.
- [ ] Take-over ends the other conversation, tells its page, and two simultaneous take-overs have one winner.
- [ ] A dropped connection re-attaches from the same page within the grace period, without a second microphone or a lost turn.
- [ ] One microphone capture at a time across Convo, dictation and the host recorder.

## Measurements and listening

- [ ] End of speech to first audible reply, interruption latency, cancellation time, missed and false answers, and memory upkeep
      cost are measured under combined load and reported as p50 and p95 with the hardware, models and run length.
- [ ] The user has listened to full conversations on each supported host and accepted them.
