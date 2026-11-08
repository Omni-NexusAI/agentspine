from helpers.api import ApiHandler, Request
from pathlib import Path


class Diagnose(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        from helpers import plugins, modules
        # Native toggles clear API caches, but do not necessarily purge Python
        # helper namespaces. Load only Doctor's fixed, pure helper afresh so a
        # repaired Doctor can recover in the same host process as well.
        diagnostics = modules.import_module(str(Path(__file__).resolve().parents[1] / 'helpers' / 'diagnostics.py'))
        roots = plugins.get_plugin_roots()
        action = input.get('action','inspect')
        if action == 'list':
            return {'ok':True, 'plugins':diagnostics.inventory(roots)}
        target = diagnostics.name(input.get('target'))
        if target == 'plugin_doctor':
            raise ValueError('Use a separate diagnostic process to repair Plugin Doctor itself')
        # This does not load the target's hooks.py or get_plugin_config().
        diagnostics.locate(roots,target)
        try:
            toggle = plugins.get_toggle_state(target)
        except Exception:
            toggle = 'unknown'
        if action == 'inspect':
            return {'ok':True, 'toggle':toggle, 'report':diagnostics.inspect(roots,target)}
        if action == 'refresh_disabled':
            if input.get('confirmed') is not True or toggle != 'disabled':
                raise ValueError('Explicit confirmation and a disabled target are required')
            plugins.after_plugin_change([target], python_change=True)
            return {'ok':True, 'toggle':'disabled', 'message':'Host cache refreshed. Target remains OFF. Rerun isolated execution tests before explicitly enabling it.'}
        raise ValueError('Unsupported diagnostic operation')
