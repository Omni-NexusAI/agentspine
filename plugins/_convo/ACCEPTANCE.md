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
- [ ] An uncertain speech or Studio operation stays recorded across off and restart until it is reconciled; nothing resets it silently.

## Memory and history

- [ ] A conversation runs with no turn limit: a long session with repeated summaries, corrections and failing summary requests never
      refuses, delays or cancels a turn, and the full journal matches what was said.
- [ ] Summary work stops when the user speaks and resumes in quiet moments. A swapped-in chapter never overwrites newer words.
- [ ] Standing notes (name, requested language, lasting preferences) survive summaries and are never cleared by a failed one.
- [ ] `recall_conversation` finds earlier turns of the same chat only.
- [ ] Summaries are written by another model only when it is local or the user consented.

## Tools and delegation

- [ ] No tool runs silently: every use and every refusal is journaled, shown in the panel and known to the model.
- [ ] Work is started only on the user's own request. Text from a tool result, a search page, a transcript or a summary never
      authorises a task, and a task that cannot be tied to the user's words is not started and says so.
- [ ] A search whose engines refused is reported as blocked; a search that worked and found nothing is reported as empty.
- [ ] At most one read-only tool call runs per reply. Further calls are reported, not run, and a result is never invented.
- [ ] A delegated task is confirmed as queued, not done. It keeps the chat it was submitted for, survives the end of the voice
      session, and is not dispatched twice after a timeout, a busy chat or a crash.
- [ ] The camera is used only while shared, only when the model asks, and only with a model measured to accept images.

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
