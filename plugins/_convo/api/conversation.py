from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from helpers.api import ApiHandler, Request


# action -> (helper module, function); the function receives the request and returns typed data (or an awaitable of it).
OWNED = {
    'conversation_key_copy': ('convo_model_catalog', 'key_copy'),
    'audio_probe': ('convo_model_catalog', 'audio_probe'),
    'classifier_check': ('convo_classifier_catalog', 'check'),
    'decider_check': ('convo_system_one', 'decider_check'),      # the previous request shape ({url, model, kind}), same check
}


def module():
    root = Path(__file__).resolve().parent.parent
    prefix = "usr.plugins" if root.parent.parent.name == "usr" else "plugins"
    return importlib.import_module(prefix + "._convo.helpers.convo_service")


class Conversation(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = input.get("action", "status")
        m = module()
        if action == 'studio_open':
            importlib.import_module(m.__package__ + '.studio_proxy').attach(self.app)
            return {'ok': True}
        if action in {'setup_read', 'setup_save', 'setup_patch', 'discover', 'connection_test', 'key_rebind', 'tls_accept', 'tls_forget',
                      'import_preview', 'import_apply', 'runtime_start', 'runtime_stop'}:
            setup = importlib.import_module(m.__package__ + '.convo_setup')
            try:
                if action == 'setup_read': return setup.read()
                if action == 'setup_save': return setup.save(input.get('config'), input.get('revision'), input.get('keys'))
                if action == 'setup_patch': return setup.patch(input.get('changes'))
                if action in {'discover', 'connection_test'}:
                    return await setup.discover(input.get('role'), input.get('provider'), check=action == 'connection_test', draft_key=input.get('draft_key'))
                if action == 'key_rebind': return setup.key_rebind(input.get('role'), input.get('provider'))
                if action == 'tls_accept': return setup.tls_accept(input.get('url'))
                if action == 'tls_forget': return setup.tls_forget(input.get('url'))
                if action == 'import_preview': return setup.preview(input.get('document'))
                if action == 'import_apply': return setup.apply(input.get('document'), input.get('paths'), input.get('revision'))
                managed = importlib.import_module(m.__package__ + '.managed_runtime')
                config = m.settings()
                if not config['sidecar'].get('managed'):
                    raise ValueError('External services are not Convo lifecycle targets')
                if action == 'runtime_start':
                    if not config['enabled']: raise ValueError('Enable Convo before starting its runtime')
                    managed.configure(config)
                else:
                    with m.studio_request_guard('POST', '/runtime/stop', allow_uncertain=True):
                        managed.instance().stop()
                return setup.read()
            except setup.store.Conflict as exc:
                return {'ok': False, 'code': 'conflict', 'error': str(exc)}
            except ValueError as exc:
                return {'ok': False, 'code': 'invalid', 'error': str(exc)}
        if action in OWNED:
            # Explicit actions owned by the conversation-model and classifier catalogs. Each answers with typed data; a
            # refusal (including "not available yet") has no side effect.
            setup = importlib.import_module(m.__package__ + '.convo_setup')
            owner, function = OWNED[action]
            try:
                result = getattr(importlib.import_module(m.__package__ + '.' + owner), function)(input)
                return await result if inspect.isawaitable(result) else result
            except setup.store.Conflict as exc:
                return {'ok': False, 'code': 'conflict', 'error': str(exc)}
            except ValueError as exc:
                return {'ok': False, 'code': 'invalid', 'error': str(exc)}
        if action == "status":
            config = m.settings()
            contract = importlib.import_module(m.__package__ + ".convo_contract")
            managed = importlib.import_module(m.__package__ + '.managed_runtime')
            runtime = managed.instance().public() if config['sidecar'].get('managed') else {'mode': 'external'}
            setup = importlib.import_module(m.__package__ + '.convo_setup')
            admission = importlib.import_module(m.__package__ + '.studio_admission')
            # The scoped revision lets an open editor notice, without a write, that its settings changed elsewhere.
            return {"ok": True, "settings": contract.public_settings(config), "runtime": runtime,
                    "settings_revision": setup.store.settings_revision(setup.store.read(setup.store.config_path())),
                    "studio_admission": admission.status(setup.store.config_path().parent / 'data' / admission.FILENAME),
                    "version": "0.1.0", "live_validated": False}
        if action == 'metrics':
            try:
                return importlib.import_module(m.__package__ + '.convo_metrics').report(m.settings(), input.get('cursor'))
            except ValueError as exc:
                return {'ok': False, 'code': 'invalid', 'error': str(exc)}
        if action == 'studio':
            config = m.settings()
            importlib.import_module(m.__package__ + '.managed_runtime').require_ready(config)
            if config['sidecar'].get('remote'):
                raise ValueError('Managed Studio requires a local Convo sidecar')
            s = m.service()
            if input.get('operation') == 'preview' and input.get('confirmed') is not True:
                raise ValueError('Explicit preview request required')
            transport = importlib.import_module(m.__package__ + '.convo_transport')
            method = 'POST' if input.get('confirmed') is True else 'GET'
            result = await m.studio_operation(transport.Sidecar(config['sidecar']),
                {key: input.get(key) for key in ('operation','payload','confirmed','item_id')}, method)
            # Profile reference audio and backend credentials stay server-side.
            def public(value):
                if isinstance(value, dict): return {k: public(v) for k,v in value.items() if k not in {'ref_audio','authorization','token','token_env'}}
                if isinstance(value, list): return [public(v) for v in value]
                return value
            return {'ok': True, 'data': public(result)}
        migration = importlib.import_module(m.__package__ + ".convo_migration")
        if action in {"migration_preview", "migrate", "rollback"}:
            root = m.runtime_root() / "usr" / "plugins"
            result = migration.rollback(root) if action == "rollback" else migration.migrate(root, apply=action == "migrate")
            return {"ok": True, **result}
        s = m.service()
        target = str(input.get("target", ""))
        s.host.context(target)
        if action == "history":
            page = s.store.history_page(target, after=input.get("after", 0), tail=input.get("tail"), before=input.get("before"))
            return {"ok": True, **page, "jobs": s.store.jobs(target)}
        if action in {"cancel_job", "steer_job"}:
            job = s.store.job(str(input.get("job_id", "")))
            if job["target"] != target:
                raise ValueError("Job belongs to another chat")
            if action == "cancel_job":
                s.dispatcher.cancel(job["id"])
            else:
                text = str(input.get("text", "")).strip()
                if not text or len(text) > 16000:
                    raise ValueError("Provide a bounded steering instruction")
                s.dispatcher.steer(job["id"], text)
            return {"ok": True, "job": s.store.job(job["id"])}
        raise ValueError("Unsupported Convo operation")
