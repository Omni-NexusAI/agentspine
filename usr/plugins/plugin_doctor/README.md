# Plugin Doctor

A small developer plugin for inspecting a broken plugin **while it remains disabled**. It is independent of Convo, so target import failures do not disable its diagnostic interface.

Install/mount this directory as `usr/plugins/plugin_doctor` in an authorized development host, enable it, and open its plugin settings. It is not installed in production or baked into release images automatically.

## Recovery loop

1. Disable the faulty target through the native plugin manager.
2. Use **Inspect source and syntax**: bounded source hashes, manifest checks and Python syntax locations. No target imports, settings values, raw logs, shell commands or model calls.
3. Apply a narrow fix with the normal editor/agent tools while the target stays OFF.
4. Explicitly refresh repaired code/cache, run the target's offline isolated tests, then explicitly enable it and check native behavior.

For Convo, use its `tests/in-image.ps1` runner against existing images. Plugin Doctor's checks are static diagnostics, not execution tests or proof of a working microphone. The host's normal cache refresh can notify browsers and clear related plugin caches; it does not toggle the target on. Existing object references may need a same-image host-process restart after code changes. Do not rebuild/roll back an image for ordinary source fixes.

The plugin does not undo external actions, delete state, bypass auth/CSRF, access Docker, execute target code, or start/stop services. A crashed/shared host process cannot diagnose itself; recover that process in the same build with any required disruption approval.
