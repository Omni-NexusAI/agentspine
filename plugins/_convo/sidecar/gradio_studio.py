"""Convo adaptation of the original audio.cpp Gradio Voice Studio build_app.

Retains its orange theme, Create / Library / Playground organization and named
tuning editor. Standalone endpoints, profile directories, live-chat widget,
model lifecycle and GPU overrides deliberately do not cross this boundary.
See audiocpp/NOTICE.md for the upstream source and retained license.
"""
import hmac
import os
import asyncio
from pathlib import Path

from .studio_adapter import StudioAdapter, TUNING_FIELDS, TUNING_REFERENCE_MAX, tuning_reference_seconds

PATH = '/convo-studio'
CSS = '''
:root { --bg0:#fff7ed; --bg1:#fffaf5; --border:rgba(15,23,42,.12); --text:#0f172a; --muted:rgba(15,23,42,.72); }
body,.gradio-container { background:radial-gradient(1200px 800px at 10% 10%,var(--bg1),var(--bg0))!important; color:var(--text)!important; color-scheme:light; }
#header { padding:18px; border:1px solid var(--border); background:linear-gradient(135deg,rgba(249,115,22,.16),rgba(251,146,60,.07)); border-radius:18px; }
.card { border:1px solid var(--border); background:rgba(255,255,255,.95); border-radius:18px; }
.small { color:var(--muted)!important; font-size:.95em; }
.gradio-container { max-width:1400px!important; } footer { display:none!important; }
'''


def orange_theme(gr):
    return gr.themes.Soft(primary_hue='orange', secondary_hue='orange', neutral_hue='slate').set(
        body_background_fill='#fff7ed', color_accent='#f97316', color_accent_soft='#ffedd5',
        button_primary_background_fill='#f97316', button_primary_background_fill_hover='#ea580c',
        input_border_color_focus='#f97316', slider_color='#f97316')


async def preview_from_request(adapter, identity, text, headers):
    """Only host-injected shared settings are accepted, never a second UI store."""
    return await adapter.preview(identity, text, headers.get('x-convo-tts-model', ''),
                                 headers.get('x-convo-tts-tuning-profile', ''))


def build_app(adapter):
    import gradio as gr

    async def refresh():
        items = await adapter.inventory()
        choices = [(p['name'], p['id']) for p in items]
        return [[p['id'], p['name'], 'Base', p.get('language', 'Auto')] for p in items], gr.update(choices=choices, value=None), gr.update(choices=choices, value=None)

    async def tuning_refresh():
        document = await adapter.tuning_read()
        profiles = document.get('profiles', {})
        return gr.update(choices=[(p.get('name', key), key) for key, p in profiles.items()], value=None)

    async def tuning_form(identity):
        document = await adapter.tuning_read()
        profile = document.get('profiles', {}).get(identity, {})
        return {k: v for k, v in profile.items() if k in TUNING_FIELDS | {'name', 'revision'}}

    async def preview(identity, text, request: gr.Request):
        # This header is replaced by the authenticated host proxy on each request.
        # There is no independent model setting in the embedded application.
        return await preview_from_request(adapter, identity, text, request.headers)

    async def models_read(request: gr.Request):
        state = await adapter.control('models')
        catalog = await adapter.control('catalog')
        supported = request.headers.get('x-convo-tts-model', '') in {item['id'] for item in catalog.get('models', [])}
        return state, gr.update(interactive=supported), gr.update(interactive=bool(state.get('loaded_models')))

    async def model_load(confirmed, request: gr.Request):
        if confirmed is not True: raise gr.Error('Confirm loading the shared selected model first')
        model = request.headers.get('x-convo-tts-model', '')
        catalog = await adapter.control('catalog')
        if model not in {item['id'] for item in catalog.get('models', [])}:
            raise gr.Error('Select a supported Convo audio model in shared settings first')
        return await adapter.control('load', {'model_key': model}, confirmed=True)

    async def model_unload(confirmed):
        if confirmed is not True: raise gr.Error('Confirm unloading the Convo-owned model first')
        return await adapter.control('unload', {}, confirmed=True)

    with gr.Blocks(title='Convo Voice Studio', analytics_enabled=False, delete_cache=(300, 3600)) as demo:
        gr.Markdown('# Voice Studio\n\nCreate and save reusable Base clone profiles. Convo owns connections, response voice and model setup.', elem_id='header')
        gr.Markdown('Stop Convo before editing or previewing. Base full-ICL cloning and complete WAV only; VoiceDesign, CustomVoice, native PCM and GPU guard overrides are unavailable.', elem_classes=['small'])
        status = gr.Markdown('', elem_classes=['small'])
        with gr.Accordion('Convo-owned model status and lifecycle', open=False):
            gr.Markdown('The selected model comes from shared Convo settings. Loading uses only already-approved, downloaded Convo weights. No external service is a lifecycle target.')
            model_status = gr.JSON(label='Managed model state')
            model_refresh = gr.Button('Refresh model status')
            model_confirm = gr.Checkbox(label='I explicitly approve this model load/unload action', value=False)
            with gr.Row():
                model_load_button = gr.Button('Load shared selected model', interactive=False)
                model_unload_button = gr.Button('Unload Convo model', interactive=False)
        with gr.Tabs():
            with gr.Tab('Create'):
                with gr.Group():
                    gr.Markdown('### Voice Clone (Base)')
                    with gr.Row():
                        with gr.Column(scale=1, min_width=280):
                            name = gr.Textbox(label='Profile name', max_length=120)
                            language = gr.Dropdown(label='Language', choices=['Auto', 'English', 'Chinese', 'Spanish', 'French', 'German', 'Italian', 'Japanese', 'Korean', 'Portuguese', 'Russian'], value='Auto')
                            reference = gr.File(label='Reference PCM16 WAV (up to 6 MiB / 90 seconds)', file_types=['.wav'], type='filepath')
                            transcript = gr.Textbox(label='Exact reference transcript (required)', lines=4, max_length=4000)
                            gr.Markdown('Only upload a voice you are authorized to use. Reference audio stays in the Convo-owned profile service.')
                            save_clone = gr.Button('Save clone profile', variant='primary')
                        with gr.Column(scale=1, min_width=280):
                            gr.Markdown('### Full-ICL clone\nUse a clean reference recording and its matching transcript. Saved profiles appear in Library and in the shared Convo response-voice selector. Preview a saved voice in Playground.')
            with gr.Tab('Library'):
                with gr.Row():
                    with gr.Column(scale=2, min_width=320):
                        table = gr.Dataframe(headers=['id', 'name', 'task_type', 'language'], interactive=False)
                        refresh_button = gr.Button('Refresh list', variant='primary')
                        archive_in = gr.File(label='Import original Studio profile ZIP (up to 12 MiB)', file_types=['.zip'], type='filepath')
                        import_button = gr.Button('Import ZIP as new profiles')
                        archive_out = gr.File(label='Export selected profile ZIP')
                    with gr.Column(scale=1, min_width=280):
                        identity = gr.Dropdown(label='Saved profile', choices=[], allow_custom_value=True)
                        edit_name = gr.Textbox(label='Profile name', max_length=120)
                        edit_language = gr.Textbox(label='Language', max_length=40)
                        edit_text = gr.Textbox(label='Matching reference transcript', lines=4, max_length=4000)
                        edit_button = gr.Button('Save profile edits')
                        export_button = gr.Button('Export selected → ZIP')
            with gr.Tab('Playground'):
                with gr.Row():
                    with gr.Column(scale=1, min_width=280):
                        preview_id = gr.Dropdown(label='Pick a saved profile for this preview only', choices=[], allow_custom_value=True)
                        text = gr.Textbox(label='Text to synthesize', value='The voice connection is ready.', lines=4, max_length=400)
                        generate = gr.Button('Generate complete WAV', variant='primary')
                        gr.Markdown('Uses the shared Convo TTS model and saved tuning profile, frozen once for this preview; it never loads a model automatically. Complete WAV keeps the Quality sampler policy while retaining the selected reference limit and seed. Preview does not change the response voice. Audio never autoplays; use its play button.')
                    with gr.Column(scale=1, min_width=280):
                        audio = gr.Audio(label='Output audio', type='filepath', autoplay=False)
            with gr.Tab('Tuning'):
                gr.Markdown('### Named profile definitions\nBuilt-ins are immutable. Make a custom copy to edit. These are the same definitions used by Convo; selecting a response profile belongs in shared settings. Complete WAV preview uses the fixed Quality sampler policy. Native block/context knobs remain inactive.')
                tuning_id = gr.Dropdown(label='Named tuning profile', choices=[], allow_custom_value=True)
                tuning_refresh_button = gr.Button('Refresh tuning profiles')
                with gr.Row():
                    tuning_name = gr.Textbox(label='Profile name', max_length=120)
                    tuning_revision = gr.Number(label='Loaded revision', value=1, precision=0, interactive=False)
                with gr.Row():
                    tuning_temperature = gr.Slider(label='Temperature', minimum=.05, maximum=2, value=1, step=.05)
                    tuning_top_k = gr.Slider(label='Top-k', minimum=1, maximum=200, value=50, step=1)
                    tuning_top_p = gr.Slider(label='Top-p', minimum=.05, maximum=1, value=.95, step=.01)
                with gr.Row():
                    tuning_repetition = gr.Slider(label='Repetition penalty', minimum=.8, maximum=2, value=1.05, step=.01)
                    tuning_seed = gr.Textbox(label='Seed (blank for random, or uint32)', value='', max_length=10)
                    tuning_reference = gr.Slider(label='Maximum reference seconds (requires matching excerpt)', minimum=1, maximum=TUNING_REFERENCE_MAX, value=20, step=1)
                with gr.Row():
                    tuning_lookahead = gr.Slider(label='Phrase text lookahead', minimum=16, maximum=512, value=64, step=1)
                    tuning_flush = gr.Slider(label='Phrase flush (milliseconds)', minimum=50, maximum=3000, value=500, step=50)
                with gr.Accordion('Inactive native PCM fields (preserved, not editable)', open=False):
                    with gr.Row():
                        tuning_first = gr.Number(label='First block (80 ms frames)', value=4, precision=0, interactive=False)
                        tuning_steady = gr.Number(label='Steady block (80 ms frames)', value=12, precision=0, interactive=False)
                        tuning_context = gr.Number(label='Decoder context (80 ms frames)', value=25, precision=0, interactive=False)
                    tuning_model = gr.Textbox(label='Required model (preserved from named profile)', interactive=False)
                tuning_save = gr.Button('Save custom definition')
                copy_name = gr.Textbox(label='New custom profile name', max_length=120)
                tuning_copy = gr.Button('Copy selected profile')
                tuning_in = gr.File(label='Import tuning JSON', file_types=['.json'], type='filepath')
                tuning_import = gr.Button('Import tuning definitions')
                tuning_out = gr.File(label='Export tuning JSON')
                tuning_export = gr.Button('Export tuning definitions')

        tuning_fields = [tuning_name, tuning_revision, tuning_temperature, tuning_top_k,
                         tuning_top_p, tuning_repetition, tuning_seed, tuning_reference,
                         tuning_lookahead, tuning_flush, tuning_first, tuning_steady,
                         tuning_context, tuning_model]
        async def save_tuning(identity, name, revision, temperature, top_k, top_p,
                              repetition, seed, reference, lookahead, flush, first,
                              steady, context, model):
            if str(seed).strip() and (not str(seed).isdigit() or not 0 <= int(seed) <= 4294967295):
                raise gr.Error('Seed must be blank or a uint32 integer')
            return await adapter.tuning_save(identity, dict(name=name, revision=int(revision),
                temperature=temperature, top_k=int(top_k), top_p=top_p,
                repetition_penalty=repetition, seed=int(seed) if str(seed).strip() else None,
                max_reference_seconds=tuning_reference_seconds(reference), text_lookahead=int(lookahead),
                phrase_flush_ms=int(flush), first_block_frames=int(first),
                steady_block_frames=int(steady), left_context_frames=int(context),
                model=model or None, clone_mode='full_icl'))
        async def show_tuning(identity):
            result = await tuning_form(identity)
            return (result.get('name', ''), result.get('revision', 1), result.get('temperature', 1),
                    result.get('top_k', 50), result.get('top_p', .95), result.get('repetition_penalty', 1.05),
                    '' if result.get('seed') is None else str(result['seed']), result.get('max_reference_seconds', 20),
                    result.get('text_lookahead', 64), result.get('phrase_flush_ms', 500),
                    result.get('first_block_frames', 4), result.get('steady_block_frames', 12),
                    result.get('left_context_frames', 25), result.get('model') or '')

        # No queued/background callbacks: host admission applies to the request
        # executing the action, not an earlier enqueue request.
        options = dict(queue=False, api_visibility='private')
        model_refresh.click(models_read, [], [model_status, model_load_button, model_unload_button], **options)
        model_load_button.click(model_load, model_confirm, model_status, **options)
        model_unload_button.click(model_unload, model_confirm, model_status, **options)
        save_clone.click(adapter.create, [name, language, reference, transcript], status, **options)
        refresh_button.click(refresh, [], [table, identity, preview_id], **options)
        identity.change(adapter.read_profile, identity, [edit_name, edit_language, edit_text], **options)
        edit_button.click(adapter.edit, [identity, edit_name, edit_language, edit_text], status, **options)
        export_button.click(adapter.export_profile, identity, archive_out, **options)
        import_button.click(adapter.import_profiles, archive_in, status, **options)
        generate.click(preview, [preview_id, text], audio, **options)
        tuning_refresh_button.click(tuning_refresh, [], tuning_id, **options)
        tuning_id.change(show_tuning, tuning_id, tuning_fields, **options)
        tuning_save.click(save_tuning, [tuning_id, *tuning_fields], status, **options)
        tuning_copy.click(adapter.tuning_create, [copy_name, tuning_id], status, **options)
        tuning_import.click(adapter.tuning_import, tuning_in, status, **options)
        tuning_export.click(adapter.tuning_export, [], tuning_out, **options)
    return demo


def mount(app, artifacts, preview):
    """Called explicitly during the owned gateway startup after dependency approval."""
    artifacts = Path(artifacts).resolve()
    os.environ['GRADIO_TEMP_DIR'] = str(artifacts)
    os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
    import gradio as gr
    from fastapi import Request
    from fastapi.responses import JSONResponse
    upload_lock = asyncio.Lock()

    def authorized(request: Request):
        token = os.environ.get('CONVO_SIDECAR_TOKEN', '')
        return 'convo-host' if len(token) >= 24 and hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token) else None

    @app.middleware('http')
    async def protect_all_studio_paths(request, call_next):
        if request.url.path.startswith(PATH + '/') or request.url.path == PATH:
            if not authorized(request): return JSONResponse({'error': 'Host authentication required'}, status_code=401)
            if request.method == 'POST' and request.url.path.endswith('/upload'):
                async with upload_lock:
                    length = request.headers.get('content-length', '')
                    if not length.isdecimal() or int(length) > 16 * 1024**2:
                        return JSONResponse({'error': 'Bounded upload required'}, status_code=413)
                    used = sum(p.stat().st_size for p in artifacts.rglob('*') if p.is_file())
                    if used + int(length) > 64 * 1024**2:
                        return JSONResponse({'error': 'Studio temporary storage is full'}, status_code=413)
                    return await call_next(request)
        return await call_next(request)

    return gr.mount_gradio_app(app, build_app(StudioAdapter(artifacts, preview=preview)), path=PATH,
        root_path=PATH, auth_dependency=authorized, allowed_paths=[],
        blocked_paths=['/opt/models', '/opt/voices', '/a0', '/root', '/proc', '/sys', '/etc'],
        max_file_size=12 * 1024**2, show_error=False, ssr_mode=False,
        enable_monitoring=False, mcp_server=False, footer_links=[],
        theme=orange_theme(gr), css=CSS)
