# Plugin Doctor

- Independent developer plugin; never import a target plugin or depend on Convo.
- Read-only diagnostics by default. Explicit refresh requires the target to be OFF.
- No source edits, test execution, shell, network, model operations, settings reads, log dumping, or Docker access from the host API.
- Inspect bounded source metadata/syntax only. Do not return source text, config values, symlink targets or full exceptions.
- Missing, invalid or linked manifests and exhausted scan budgets must not report success. Refresh fails closed for active/unknown targets or non-boolean confirmation.
- Use native auth/CSRF, plugin roots, toggles and cache refresh. Never patch core.
- Resolve source by ordered manifest ownership; a user-data-only directory must not hide built-in source. Retain a missing-manifest fallback for genuinely broken installs.
- Load only Doctor's own fixed diagnostic helper freshly through the native module loader; native toggle/API-cache refresh must not leave its repaired helper stale. Never dynamically load a target's source.
- Execution/fault-injection tests run outside the live host with isolated state.
- Keep runtime user files untracked; only this developer plugin's source is versioned, never baked into release images.
- Verify `python -m unittest discover -s usr/plugins/plugin_doctor/tests -v` and the Convo in-image host smoke test.
