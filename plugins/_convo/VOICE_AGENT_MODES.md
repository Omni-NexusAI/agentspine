# Voice agent modes — design (not implemented)

Written 2026-10-31 from the user's direction. **Nothing here is built yet.** It is built after the user has tested today's core, so those results can shape it. Each build step needs its own approval; downloads, GPU loads and training stay separate checkpoints.

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
