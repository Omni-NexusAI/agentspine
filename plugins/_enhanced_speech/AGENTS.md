# Enhanced Speech compatibility shim

- Convo owns runtime hooks, provider adapters and microphone integration.
- Keep historical import/API paths usable by older host settings code.
- Do not add active WebUI or lifecycle patches here: exactly one microphone owner.
- User configuration stays intact for explicit, reversible migration to `_convo`.
- Image overlays must replace legacy hooks with these no-op files, not leave old hooks in a base image.
