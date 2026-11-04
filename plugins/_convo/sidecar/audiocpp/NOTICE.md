# Reused speech runtime

`supervisor.py`, `profile_library.py`, the model configuration and native decoder
patch are vendored from the newer speech-to-speech integration at source commit
`1b315b4`. Its Apache-2.0 license is retained in `LICENSE.speech-to-speech`.
The original standalone checkout and services are not modified by this package.

Convo adds `gateway.py`, `downloads.py` and its own Docker packaging. No model
weights, private voice profiles or existing service image is bundled.
The explicitly approved temporary `../gradio_studio.py` adapts the original
`integrations/audio-cpp/gradio_voice_studio.py` orange theme, `build_app`
Create/Library/Playground layout, tuning form and ZIP interchange. Source SHA256:
`a966e6c8d5dc36a101800e07e087e2120df940391e0358a900efaf1dd550d444`.
Apache-2.0 attribution is retained in `LICENSE.speech-to-speech`. The adaptation
replaces standalone URL/library widgets, filesystem profile mutations and
browser-direct network/audio with Convo-owned callbacks and host authentication.
The engine is built from https://github.com/0xShug0/audio.cpp at
`238ab6a9e321c17de8e120559f57efeedaeb1345`; its license is copied from that build.
Model licenses are separate and shown before an explicit download.

The reused supervisor's measured guard thresholds are admission safeguards,
not performance claims about a user's hardware. Native incremental PCM stays
disabled in this preview. This image has not yet been built or listening-tested.
