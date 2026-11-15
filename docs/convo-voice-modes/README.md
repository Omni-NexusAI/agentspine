# Convo voice agent modes: UI mockups

Static HTML mockups for the voice agent modes design in
[`plugins/_convo/VOICE_AGENT_MODES.md`](../../plugins/_convo/VOICE_AGENT_MODES.md). They live at the repository root,
outside `plugins/_convo`, so they are never deployed with the plugin.

**Design only.** Nothing here is wired to Convo, and no runtime code changes with these files. Model names, dates and
results in the mockups are **examples**, not test results.

## Viewing

Open `index.html` in a browser from a checkout. No server or network is needed: one stylesheet (`mockup.css`), one small
script (`mockup.js`) and inline icons. Every page shows Agent Zero's dark and light themes side by side, and has controls
for one theme only and for phone width.

| Page | Shows | Screenshot |
|---|---|---|
| [`index.html`](index.html) | The idea, which card does what in each mode, the evidence labels | — |
| [`1-hybrid.html`](1-hybrid.html) | Hybrid chosen: what a person sees first (build step 1) | [`screens/1-hybrid.png`](screens/1-hybrid.png) |
| [`2-auto.html`](2-auto.html) | Auto's pick, why, the override; Auto's pick changing | [`screens/2-auto.png`](screens/2-auto.png) |
| [`3-native.html`](3-native.html) | Native: folded cards, privacy note, delegated work and voice | [`screens/3-native.png`](screens/3-native.png) |
| [`4-tuned.html`](4-tuned.html) | Tuned: harness, live speech input, a missing requirement | [`screens/4-tuned.png`](screens/4-tuned.png) |
| [`5-unavailable.html`](5-unavailable.html) | Not in this version, not with this model, the start refusal | [`screens/5-unavailable.png`](screens/5-unavailable.png) |
| [`6-unknown-model.html`](6-unknown-model.html) | A model Convo doesn't know; "Research this model" and the draft review | [`screens/6-unknown-model.png`](screens/6-unknown-model.png) |
| [`7-narrow-and-elsewhere.html`](7-narrow-and-elsewhere.html) | Phone width, the panel chip, the preset editor, diagnostics | [`screens/7-narrow-and-elsewhere.png`](screens/7-narrow-and-elsewhere.png) |

## How the mockups relate to the real interface

- **Host layer.** Agent Zero's tokens (`--color-*`, `.light-mode`) and its field, toggle and button classes are copied from
  `webui/index.css`, `webui/css/settings.css` and `webui/css/buttons.css`.
- **Convo layer.** Convo's existing card classes (`convo-card`, `convo-model-card`, `convo-model-note`, `convo-model-why`,
  `field`, …) imitate the plugin's own markup.
- **Proposed layer.** The `convo-mode-*` classes use host tokens only (no hex colours), as the Connections cards must
  (`plugins/_convo/tests/settings-split.test.mjs`).
- **Not the host.** The mockups imitate the host; they are not it. Rendering inside the real modals is a live check on the
  isolated test host once a build step is approved.
- Checked here: every page renders without script errors in both themes, each screen keeps its own radio selection, every
  label and description reference resolves, and nothing scrolls sideways at 390 px.
