# Optional System One decision consumer

Convo can consume generic typed System One decisions over text, decoded RGB
images and complete PCM16 audio. This adapter does not discover, load, download,
start, stop or choose a model. It never changes Main, Utility or Embedding routes.

## Explicit configuration

Convo configuration (under the existing `convo` settings object):

```json
{"system_one":{"enabled":false,"backend":""}}
```

Enabling requires a local registration identifier such as `voice-policy`. Unknown
configuration keys are preserved. The saved `roles.classifier` connection remains
unchanged and unselected while System One is enabled. Selected-backend errors
must surface as unavailable; do not fall back to the saved classifier.

System One's effective consumer config independently requires:

```json
{"enabled":true,"consumers":{"convo":{"enabled":true}}}
```

The effective provider must include native plugin OFF. SDK consumer configuration
is rechecked after inference to discard revoked results. Host epoch checks remain
mandatory. Registered remote backends, and a decision server on a public address,
require Convo's explicit remote ambient consent before receiving ambient observations.

The default host configuration provider derives the effective `enabled` flag
from native System One ownership. It does not read or enable `main.enabled`,
utility, or embedding modes. A provisioner must add the explicit consumer opt-in
without changing those existing modes.

## Provisioning contract

A host provisioner explicitly supplies a ready generic SDK backend:

```python
from plugins._convo.helpers import convo_system_one

registration = convo_system_one.register_backend(
    "voice-policy", backend,
    sdk=system_one_media_sdk,
    config_provider=read_effective_system_one_config,
    remote=False,
)
```

The optional `sdk` default imports the installed System One SDK lazily at
registration, not at Convo import/start/readiness. No staging path is a runtime
dependency. Registration uses the host `helpers.runtime` owner and survives exact
Convo namespace refresh/OFF/ON. It never enables either plugin. Register the same
native client for a model to preserve its worker lease across cancellation.

`unregister_backend("voice-policy", registration)` removes only that exact
registration and never unloads its model. Registration changes invalidate late
results. Duplicate names are rejected rather than silently replacing a backend.

## Realtime integration

```python
status = convo_system_one.readiness(effective_convo_config)
result = await convo_system_one.policy(
    existing_policy_payload,
    config=read_effective_convo_config,
)
```

`readiness` returns `ready`, `state`, `code`, `detail`, and sanitized capabilities.
Ready means registered and opted in, not measured inference, latency or listening
acceptance. Use a callable config provider in `policy` for live Convo revocation.

The request accepts the existing policy payload's `audio` (bounded complete
base64 WAV), `transcript`, `context`, and `ambient`. Optional `images` must be a
tuple of the registered SDK's `ImageInput` instances containing decoded RGB
bytes. No URL or path fetching is possible. WAV samples and rate are extracted
exactly; unsupported rates/modalities fail under backend capabilities. If audio
is present its transcript is ignored for classification rather than substituted
for the waveform. The model's state is one plain sentence (the audio is the latest
thing the user said; what the assistant last said that the user heard, bounded and
quoted; recognized text only for a text-only conversation, quoted as data) rather
than a JSON document: that framing was measured against the earlier one and read
the decision model far better (see `UI_BACKEND_CYCLE_RESULTS_20261015.md`). After a reply
that ended in a question, that line is dropped unless the answer is short (up to 3 s of audio):
told the assistant had just asked something, the model read nearly any speech as the answer.
The sentence carries nothing about earlier classified turns: a line such as "the previous speech was ignored as a
passing remark" was measured against a later bare "what do you think?" and did not help (83% answered with and
without it) while it made talk to someone else look more like a request; it lifted overall discrimination only
under the old side-cue wording (by masking its name defect) and gains nothing resolvable under the shipped one. The main model, not the classifier, links a
late question to an earlier remark.

The decision model reads only the first ~23 tokens of every criterion description; the rest is ignored (measured:
a different tail changed the answer by 0, a trimmed tail changed it, because the cut falls mid-clause). The
side-conversation cue therefore must not spend its words on "naming someone" (that made requests that begin with the
assistant's name the worst-read class) and a broadcast cue past the cut never reached the model. The wording and the
gate floors fitted to it are one measured unit (`GATE_JUDGMENT_20261016.md`).

`plugins/convo_d1_decider` is the development provisioner that registers a served
d1-omni-style decision model (a `/v1/systemone` server such as llama-server) as backend
`d1_llama_cpp` for Convo only, until System One ships its own. It carries pinned copies of System
One's `decision.py` and `media_decision.py` and adds nothing to Agent Zero's chat, tool or model path.

The Classifier card (`roles.classifier`, `helpers/convo_classifier_catalog.py`) selects it: a
decision-model provider, or the toggle "Use System One's decider" (`decider.source` `system_one`),
writes `system_one = {enabled: true, backend: d1_llama_cpp}` through the settings normalizer; a chat
model switches the consumer off. Addresses follow the classifier's locality rule (this machine and
private networks; a public one only with the classifier's remote consent, over HTTPS). A backend on
a public address reports `remote` in readiness, so ambient observations also need Convo's separate
remote ambient consent. Convo's own decision classifier receives its key as a Bearer
(`convo_setup.classifier_bearer`); System One's decider receives none from Convo.

Four fixed choice questions produce attention, requested action, explicit-request
and name-addressing decisions. Return shape matches Convo's policy envelope:
`speech`, `action`, `confidence`, `explicit_request`, `addressed_by_name`, and an
empty `context_note`, plus the typed evidence the speech gate decides from:
`attention` (the most likely class), `attention_probabilities` (all five classes),
`explicit_probability` and `name_probability`. A backend must therefore return a
probability for every criterion of a choice question (the SDK already requires
it); a classifier that cannot is reported by Convo as an unsupported speech gate.
Backchannels/side conversation never authorize actions;
tools still require the host policy/epoch/permission gates. No generative
transcription or action is performed by this adapter.

The adapter does not claim streaming input or Duplex, and carries no model-name-
specific logic. SDK-only model restrictions remain with that model's adapter.

## Verification

`test_system_one_consumer.py` uses the actual staged/canonical SDK against inert
native stubs, temporary host runtime ownership and synthetic WAVs. Set
`CONVO_SYSTEM_ONE_TEST_SDK` and `SYSTEM_ONE_TEST_CORE` to alternate fixture paths.
No models, provider requests, services, installed config or GPU are touched.
