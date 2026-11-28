from __future__ import annotations

import asyncio
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


def voice_owner(m):
    """The running voice conversation ({session, target, page, device, since}) or None; never fails a status read."""
    try:
        instance = getattr(m, '_instance', None)       # only a service that already runs: a status read creates nothing
        return importlib.import_module(m.__package__ + '.convo_ownership').owner(instance.store) if instance is not None else None
    except Exception:
        return None


def assistant_name(config, host, target):
    """What the panel calls the assistant: the voice name, else the pinned profile's title, else the chat profile's, else Convo."""
    name = ' '.join(str(config.get('voice_name') or '').split())
    if name:
        return name
    title = getattr(host, 'profile_title', None)
    try:
        name = title(target, str(config.get('voice_profile') or '')) if title else ''
    except Exception:
        name = ''
    return name or 'Convo'


def steering_on(m):
    """Whether a change to a running task goes in at its next step (the Steering plugin is on) or interrupts it. Never fails a read."""
    try:
        return importlib.import_module(m.__package__ + '.convo_steering').available()
    except Exception:
        return False


def plans(m, store):
    return importlib.import_module(m.__package__ + '.convo_planning').of(store)


def plan_view(m, store, target):
    """The conversation's plan for the panel, or None. Never fails a read."""
    try:
        return plans(m, store).view(target)
    except Exception:
        return None


PLAN_ACTIONS = {'plan_start', 'plan_end', 'plan_save', 'plan_hand_over', 'plan_drop', 'plan_version'}


def plan_action(m, s, target, action, data):
    """The panel's plan buttons (and the hotkey). The journal decides what holds, so a voice session running in this conversation, on this
    page or another, sees it with its next reply and its next tool call. Every one is the user's own press. A refusal answers with
    words; a plan that changed meanwhile answers with the plan as it is now."""
    kept = plans(m, s.store)
    owner = voice_owner(m)
    session = s.store.session(owner['session'], active=False) if owner and owner.get('target') == target else None
    try:
        if action == 'plan_start':
            # `fresh`: the Plan button. It begins a new plan unless one is being worked on (a page that was not up to date): the one kept last
            # is gone on with by its own button, never by the way.
            new = data.get('new') is True or (data.get('fresh') is True and kept.open(target) is None)
            begun, _ = kept.begin(target, str(data.get('topic') or ''), new=new, plan_id=str(data.get('id') or ''), session=session)
            return {'ok': True, 'plan': kept.view(target), **({'taken': True} if begun.get('taken') else {})}
        kept.settle()
        plan = kept.get(str(data.get('id') or ''))
        if plan is None or plan['target'] != target:
            raise ValueError('That plan is no longer there')
        job = None
        if action == 'plan_version':
            version = data.get('version')
            return {'ok': True, 'text': kept.version(plan['id'], version if type(version) is int else 0), 'plan': kept.view(target)}
        if action == 'plan_drop' and plan['status'] == 'handed' and data.get('status') in ('open', 'kept'):
            raise ValueError(kept.HANDED)                      # (a page that was not up to date: its task runs, so it was not discarded)
        if action == 'plan_save':
            kept.write(plan['id'], data.get('text'), revision=data.get('revision'), by='user', topic=data.get('topic') or '',
                       was=data.get('was') if isinstance(data.get('was'), str) else None)
        elif action == 'plan_hand_over':
            kept.check(plan)                               # before a chat is made for it
            contract = importlib.import_module(m.__package__ + '.convo_contract')
            chat = target
            if target == contract.GLOBAL_SCOPE:
                chat = str(data.get('chat') or '')
                if chat:
                    s.host.context(chat)
                else:
                    chat = s.host.new_chat('Voice tasks')['id']
            job = kept.hand_over(plan['id'], chat, session=session)
        else:
            kept.end(plan['id'], 'dropped' if action == 'plan_drop' else 'kept', session=session)
        if action != 'plan_save':
            s.dispatcher.start()                       # what waited for the end of planning, and the plan itself when it was handed over
        return {'ok': True, 'plan': kept.view(target), **({'job': job} if job else {})}
    except ValueError as exc:
        return {'ok': False, 'code': 'conflict' if str(exc) == kept.CHANGED else 'invalid', 'error': str(exc)[:200], 'plan': plan_view(m, s.store, target)}


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
        if action in {'synthesis_read', 'synthesis_resolve'}:
            # The Advanced synthesis controls (diagnostics Tune): read the speech server's tuning profiles, or have it resolve a
            # choice. Read-only on the server: nothing is synthesised, saved or selected there. Only on the user's own audio.cpp.
            synthesis = importlib.import_module(m.__package__ + '.convo_synthesis')
            role = m.settings()['roles']['tts']
            if not synthesis.applies(role):
                return {'ok': False, 'error': 'Advanced synthesis controls apply to your own audio.cpp speech server '
                                              '(Speech output: audio.cpp, external server).'}
            contract = importlib.import_module(m.__package__ + '.convo_contract')
            net = importlib.import_module(m.__package__ + '.convo_net')
            try:
                headers = contract.credentials(role)
                if action == 'synthesis_read':
                    found = await synthesis.read(role, headers, net.client)
                    return {'ok': True, **found, 'saved': role.get('tuning') or {'profile_id': '', 'overrides': {}},
                            'bounds': synthesis.bounds(), 'native_pcm': bool((role.get('capabilities') or {}).get('native_pcm'))}
                resolved = await synthesis.resolve(role, headers, net.client, str(input.get('profile_id') or '')[:synthesis.PROFILE_ID_LIMIT],
                                                   input.get('overrides'))
                return {'ok': True, **resolved}
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)[:300]}
            except Exception as exc:
                return {'ok': False, 'error': net.describe(exc, contract.endpoint(role))[:300]}
        if action in {'setup_read', 'setup_save', 'setup_patch', 'discover', 'connection_test', 'key_rebind', 'key_reveal', 'tls_accept', 'tls_forget',
                      'import_preview', 'import_apply', 'runtime_start', 'runtime_stop'}:
            setup = importlib.import_module(m.__package__ + '.convo_setup')
            try:
                if action == 'setup_read':
                    # With Agent Zero's agent profiles, for "Speak as profile" (read-only; never fails the read).
                    result = setup.read()
                    try:
                        result['profiles'] = m.service().host.profiles()
                    except Exception:
                        result['profiles'] = []
                    return result
                if action == 'setup_save': return setup.save(input.get('config'), input.get('revision'), input.get('keys'))
                if action == 'setup_patch': return setup.patch(input.get('changes'))
                if action in {'discover', 'connection_test'}:
                    return await setup.discover(input.get('role'), input.get('provider'), check=action == 'connection_test', draft_key=input.get('draft_key'))
                if action == 'key_rebind': return setup.key_rebind(input.get('role'), input.get('provider'))
                if action == 'key_reveal': return setup.key_reveal(input.get('role'))
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
        if action in {'clip_save', 'clip_list', 'clip_delete'}:
            # Labelled test clips for measuring turn-taking models (developer mode; recorded only when the user presses Record).
            if not m.settings().get('developer_mode'):
                return {'ok': False, 'error': 'Test clips are available in developer mode.'}
            clips = importlib.import_module(m.__package__ + '.convo_clips')
            data = importlib.import_module(m.__package__ + '.convo_setup').store.config_path().parent / 'data'
            try:
                if action == 'clip_save':
                    return {'ok': True, 'id': clips.save(data, input.get('label'), input.get('audio'), input.get('device', ''))}
                if action == 'clip_list':
                    return {'ok': True, 'clips': clips.list_clips(data), 'labels': list(clips.LABELS)}
                return {'ok': clips.delete(data, input.get('id'))}
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)[:300]}
        if action in {'inspect_turns', 'replay_save', 'replay_list', 'replay_delete', 'memory_view'}:
            # "What the model saw / wrote" and the memory view (developer mode only; convo_inspect). The turns are memory only and
            # never carry audio; Save for replay writes one turn, with its audio, to data/replay only on the user's click.
            config = m.settings()
            inspector = importlib.import_module(m.__package__ + '.convo_inspect')
            if not config.get('developer_mode'):
                inspector.clear()          # developer mode is off: nothing captured earlier is kept for a later view
                return {'ok': False, 'error': 'This is available in developer mode.'}
            data = importlib.import_module(m.__package__ + '.convo_setup').store.config_path().parent / 'data'
            try:
                if action == 'inspect_turns':
                    return {'ok': True, 'turns': inspector.turns(), 'revision': inspector.revision(), 'ring': inspector.RING}
                if action == 'replay_save':
                    return {'ok': True, 'id': inspector.save(data, input.get('turn')), 'saved': inspector.saved(data)}
                if action == 'replay_list':
                    return {'ok': True, 'saved': inspector.saved(data)}
                if action == 'replay_delete':
                    return {'ok': inspector.delete(data, input.get('id')), 'saved': inspector.saved(data)}
                target = str(input.get('target', ''))
                if not target:
                    return {'ok': False, 'error': 'Choose a chat first.'}
                return {'ok': True, **inspector.memory_view(m.service().store, target, config.get('compaction') or {})}
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)[:300]}
        if action in {'recording_save', 'recording_list', 'recording_delete'}:
            # "Record raw and sent" (developer mode; saved only when the user presses Record in the Sound drawer).
            if not m.settings().get('developer_mode'):
                return {'ok': False, 'error': 'Record raw and sent is available in developer mode.'}
            recordings = importlib.import_module(m.__package__ + '.convo_recordings')
            data = importlib.import_module(m.__package__ + '.convo_setup').store.config_path().parent / 'data'
            try:
                if action == 'recording_save':
                    item, analysis = await asyncio.to_thread(recordings.save, data, input.get('raw'), input.get('sent'), input.get('pre_roll_s', 0),
                                                             input.get('device', ''), input.get('mode', ''))
                    return {'ok': True, 'id': item, 'analysis': {key: value for key, value in analysis.items() if not key.endswith('_levels')}}
                if action == 'recording_list':
                    return {'ok': True, 'recordings': recordings.list_recordings(data)}
                return {'ok': recordings.delete(data, input.get('id'))}
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)[:300]}
        if action in {'search_health', 'search_check'}:
            # Settings shows the search engines' last known state (from memory: opening Settings never sends a search); the
            # "Check search" button sends one small search through the same ladder the voice agent uses.
            try:
                if action == 'search_health':
                    instance = getattr(m, '_instance', None)       # only a service that already runs: a read creates nothing
                    state = instance.host.search_state() if instance is not None else {'state': 'unknown', 'checked_at': '', 'answered': 0, 'refused': 0}
                else:
                    state = await asyncio.wait_for(m.service().host.search_check(), 20)
                return {'ok': True, **state}
            except Exception as exc:
                return {'ok': False, 'error': 'The search service did not answer' if not isinstance(exc, ValueError) else str(exc)[:200]}
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
                    "version": "0.1.0", "live_validated": False,
                    # The browser files' identity: a page that loaded another build offers a reload (convo_build).
                    "build": importlib.import_module(m.__package__ + '.convo_build').build_id(),
                    # Who runs voice now (page, device label, since, chat), for "Voice is running on …" and Take over here.
                    "voice_owner": voice_owner(m)}
        if action == 'metrics':
            try:
                report = importlib.import_module(m.__package__ + '.convo_metrics').report(m.settings(), input.get('cursor'))
            except ValueError as exc:
                return {'ok': False, 'code': 'invalid', 'error': str(exc)}
            # Developer mode only (report refuses otherwise): the text view of the last conversation-model request.
            inspector = importlib.import_module(m.__package__ + '.convo_inspect')
            report['last_request'] = inspector.latest()
            report['inspect_revision'] = inspector.revision()      # the panel reads `inspect_turns` again only when this changes
            return report
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
            jobs = s.store.jobs(target)
            changes = {}
            for change in s.store.steers([job["id"] for job in jobs if job["status"] in ("queued", "dispatching", "running")] or ["-"]):
                changes[change["job"]] = change["status"]          # the newest change of each task still going on
            return {"ok": True, **page, "jobs": [{**job, "change": changes[job["id"]]} if job["id"] in changes else job for job in jobs],
                    "steering": steering_on(m), "plan": plan_view(m, s.store, target), "assistant_name": assistant_name(m.settings(), s.host, target)}
        if action in PLAN_ACTIONS:
            return plan_action(m, s, target, action, input)
        if action in {"cancel_job", "steer_job"}:
            job = s.store.job(str(input.get("job_id", "")))
            if job["target"] != target:
                raise ValueError("Job belongs to another chat")
            if action == "cancel_job":
                s.dispatcher.cancel(job["id"])
            else:
                return {"ok": True, "how": s.dispatcher.steer(job["id"], input.get("text", "")), "job": s.store.job(job["id"])}
            return {"ok": True, "job": s.store.job(job["id"])}
        raise ValueError("Unsupported Convo operation")
