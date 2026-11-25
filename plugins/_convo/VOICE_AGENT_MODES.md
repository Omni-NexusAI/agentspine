# Voice agent modes — design (not implemented)

Written 2026-10-31 from the user's direction. **Nothing of these modes is built yet** (the working modes at the end are a separate matter: planning is built, live translation is prepared). It is built after the user has tested today's core, so those results can shape it. Each build step needs its own approval; downloads, GPU loads and training stay separate checkpoints.

## Why

Listening while the user speaks, deciding when to talk, and speaking in a chosen voice can be done three ways. Each way needs different models and a different harness. Convo should support all three behind one setting, plus an **Auto** choice that picks the right one for the model in use.

## The three modes

| | **Tuned harness** | **Native duplex** | **Hybrid** |
|---|---|---|---|
| Idea | Codacus' micro-turn video and Speakrail: a fine-tuned model plus a harness built for it | The model itself listens and speaks at the same time | An omni model hears your audio; Convo's harness handles turn-taking; any voice |
| Examples | The user's future fine-tunes; Speakrail's Gemma-4-12B LoRA; Codacus' Qwen3.6-35B-A3B LoRA | GPT-Live-1 (API not generally available yet); NemotronLabs VoiceChat 11B (open, English only); gpt-realtime | LFM3-Omni-2B (text/image/video/audio in; text, speech or interleaved out); Gemma 4 with audio input |
| Who hears | Streaming STT text fragments | The model, continuously | The model hears your original audio (completed turns); the classifier hears short probes |
| Who decides when to talk | Tuned decision tokens every tick, plus the harness rules | The model | Convo: voice activity → classifier → gate → speculative reply |
| Talking over the user | Overlap decisions (continue / yield / listen) | Built into the model | Probes during playback, the interrupt decision, ducking |
| Voice | External TTS (required) | The model's own voice. External TTS only when the model can answer in text, which gives up duplex timing | External TTS (cloned voice) by default, or the model's own voice when it outputs audio |
| Tools / Agent Zero | Tool hold after a decision; the host executes | The model's tool channel → Convo's tool catalog → the host (like GPT-Live handing hard work to a larger model) | Classifier routing plus the main model's tool calls (exists today) |
| Needs | A harness profile per tuned model, and streaming STT | A realtime adapter: the OpenAI Realtime protocol (OpenAI, and the standalone fork's realtime-compatible server); NemotronLabs' NIM API checked when we get there | What exists, plus an own-voice output path |

**Where Convo stands today:** the **Hybrid** foundation.
- Direct audio in.
- Classifier in parallel with a held speculative reply.
- First-clause external TTS.
- Echo guard and ducking.

It borrows the tuned mode's *policies* (speculation, continuation, gate floors) with off-the-shelf models; no tuned decision model exists yet.

## Auto mode

Resolution order, highest first. Auto always shows what it picked and why (for example "Auto → Hybrid · curated list, tested 2026-11-…").

1. **Your explicit choice.**
2. **Your confirmed profiles**, kept on this machine and learned from use: the mode you keep for a model, and its measured results (latency, false cuts, interruptions).
3. **A shipped curated list.** Each entry carries its evidence: tested by us, or only claimed by the vendor.
4. **Capability detection on Check / Test**:
   - a `/v1/realtime` endpoint → Native;
   - a passing audio-input test → audio in;
   - an audio-output probe → own voice possible;
   - a harness manifest → Tuned.
5. **Default:** Hybrid with external TTS.

Further choices:
- **"Research this model"** (optional button): Agent Zero's main model searches and drafts a profile with sources. It counts only after you confirm it, because model cards are untrusted text.
- **Matching** is deterministic, which is faster and predictable. A decision model could later fuzzy-match unknown model names to list entries.
- **Learning across users** would need a shared service, so it is out of scope. Local profiles can be exported and imported by hand.

### Model profile (sketch)

`{match, mode, input: audio|text, output: text|audio|interleaved, voice: external|own|either, duplex, protocol: chat_completions|realtime|harness:<id>, evidence: tested|vendor|user|agent, tested_on, notes}`

## Build order (each its own approval)

1. **Mode setting, model profiles and Auto.** Hybrid is the only working mode; the others show why they are not available yet.
2. **Hybrid own-voice output** (LFM3-Omni). Its local runtime is checked when the model is copied over.
3. **Native duplex** via the OpenAI Realtime protocol.
4. **Tuned harness**, tied to the user's fine-tuning work.

## Sources (read 2026-10-31)

- LFM3-Omni-2B model card and docs: https://huggingface.co/LiquidAI/LFM3-Omni-2B, https://docs.liquid.ai/lfm/models/lfm3-omni-2b
- NemotronLabs VoiceChat (NVIDIA catalog): https://catalog.ngc.nvidia.com/orgs/nim/nvidia/models/nemotron-labs-voicechat
- GPT-Live (OpenAI engineering post): https://openai.com/index/continuous-voice-interaction-with-gpt-live/
- Speakrail and the Codacus video: see the backend reference audit in the Convo plan (Part B).

# Working modes (2026-11-24)

What the voice agent does with what it hears, whichever of the modes above carries the conversation.

## Planning — built

Voice only: talk and look things up, everything else on the user's yes, a plan that is written beside the conversation and can be handed over. Contract and tests: `AGENTS.md` ("Planning mode"). It is also the first of the permission modes the user asked for later (auto, ask): its floor is applied after the permission join (`convo_policy.set_authorizer`), so a later mode can tighten what runs but never loosen planning.

## Live translation — design; stage 1 is prepared, nothing else is built

The user wants what a commercial voice translator does: the agent speaks the translation while the other person is still talking, both ways, with several people, on request or by itself. Their decisions: their own models first (a cloud interpreter later, as a choice with its own consent); one device between people, earbuds, calls and videos on the PC, and the VR headset or launcher.

**Why it needs a path of its own.** Convo treats speech over its own speech as an interruption, decides one utterance at a time and speaks one language per reply. While translating, speech goes to a separate engine inside the same session and epoch, which holds `state['task']` (so notices, timers and typed turns wait as they do for a reply), refuses `convo.candidate`, and blocks compaction. The ordinary conversation comes back unchanged.

**How it would work.** The browser cuts what it hears at tiny pauses into pieces of about 1.5 to 5 seconds. The conversation model gets each piece as audio with the last pieces as text and writes the language, what it heard and the translation (the source line is needed: it is the next piece's context and the caption). A FIFO speech line of its own speaks the translations, each in its own language. A piece may be held until more is said (a language that puts the verb last); when it falls behind, waiting pieces are joined and said shorter; nothing is dropped.

- Direction per utterance: speech in the user's language goes to the other person's, anything else to the user's. With several other languages, the one named or last heard. A key settles doubt.
- Where it speaks over people: only on a device that passes an echo check (a short test sound at the start, or the user saying so); on a loudspeaker between people it speaks in the speaker's pauses, captions run live. The PC can use two outputs; a phone cannot, so the other side gets large captions.
- Talking to the assistant meanwhile: by name, under the planning floor, so a bystander can never start anything.
- Starting by itself: asked once when another language is heard with the user's; the yes holds for that conversation.
- Privacy: other people's words are not kept unless the user switches that on, no audio is stored, and a conversation model on someone else's server needs a consent of its own for translating.

**Stage 1 comes first, and decides.** Can the user's own model translate pieces of speech well and quickly enough? Pieces cut three ways against the whole sentence, in four languages: meaning kept, words lost or doubled at the cuts, the language recognised, the time to the text, the time to the first sound with the cloned voice and with a quicker one. Go on when at least 90% of the pieces keep their meaning and the usual delay is 7 seconds or less; otherwise choose together (another model, or the cloud interpreter). The test texts and the measuring program are in `tests/translation/` and send nothing by themselves; running them uses the user's model and speech server, so it waits for the user's word.

Then: captions live; voice in pauses; voice while the person talks, where the echo check allows; two outputs and PC sound; by name and by itself; the cloud interpreter. Each is its own step.

Not settled, and not to be claimed: that any local model does this well; the delay of a cloned voice (about 2.3 s to its first sound was measured on 2026-11-18, likely the largest part); whether the launcher can hand over the PC's sound; prices and two-way behaviour of cloud interpreters beyond what their own documentation says.
