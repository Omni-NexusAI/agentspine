"""Convo callbacks for the adapted original Studio, without local profile writes.

All persistent state belongs to the fixed private audio gateway. Temporary files
are upload/output artifacts only; importing a ZIP never extracts its paths.
"""
from __future__ import annotations

import base64
import io
import json
import re
import secrets
import wave
import zipfile
from pathlib import Path, PurePosixPath

from ..helpers.convo_contract import ContractError
from . import studio

MAX_FILE = 6 * 1024**2
MAX_ARCHIVE = 12 * 1024**2
TUNING_REFERENCE_MAX = 30
TUNING_FIELDS = {'model', 'clone_mode', 'max_reference_seconds', 'first_block_frames',
                 'steady_block_frames', 'left_context_frames', 'text_lookahead',
                 'phrase_flush_ms', 'temperature', 'top_k', 'top_p',
                 'repetition_penalty', 'seed'}


def profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ContractError('Select a saved Base profile')
    return value


def tuning_reference_seconds(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 1 <= value <= TUNING_REFERENCE_MAX or int(value) != value):
        raise ContractError('Tuning reference duration must be 1–30 whole seconds')
    return int(value)


def validate_wav(data):
    if not 0 < len(data) <= MAX_FILE:
        raise ContractError('Reference WAV must be smaller than 6 MiB')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getcomptype() != 'NONE' or audio.getnchannels() not in (1, 2)
                    or audio.getsampwidth() != 2 or not 8000 <= audio.getframerate() <= 96000
                    or not 0 < audio.getnframes() / audio.getframerate() <= 90):
                raise ValueError()
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * audio.getnchannels() * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError):
        raise ContractError('Use a complete PCM16 WAV reference of at most 90 seconds') from None
    return data


def clone_payload(name, language, text, data):
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 120:
        raise ContractError('Provide a profile name of at most 120 characters')
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 4000:
        raise ContractError('Provide the exact reference transcript, at most 4000 characters')
    if not isinstance(language, str) or len(language) > 40:
        raise ContractError('Invalid language')
    return dict(name=name.strip(), language=language, ref_text=text.strip(),
                ref_audio=base64.b64encode(validate_wav(data)).decode(), task_type='Base',
                x_vector_only_mode=False, provider='qwen3tts-audiocpp')


def read_archive(data):
    """Read original Studio ZIP layout, validate everything before first write."""
    if len(data) > MAX_ARCHIVE:
        raise ContractError('Profile archive exceeds 12 MiB')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(members) > 65 or len(names) != len(set(names)) or sum(x.file_size for x in members) > MAX_ARCHIVE:
                raise ContractError('Archive has too many, duplicate or oversized entries')
            for item in members:
                p = PurePosixPath(item.filename)
                if (p.is_absolute() or '..' in p.parts or '\\' in item.filename or ':' in item.filename
                        or item.flag_bits & 1 or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                    raise ContractError('Unsafe archive member')
            roots = sorted({name.split('/')[0] for name in names if name.endswith('/meta.json')})
            if not roots or len(roots) > 16:
                raise ContractError('Archive must contain 1–16 Base profiles')
            payloads = []
            for root in roots:
                profile_id(root)
                raw = archive.read(root + '/meta.json')
                if len(raw) > 65536: raise ContractError('Profile metadata exceeds limit')
                meta = json.loads(raw)
                if not isinstance(meta, dict) or meta.get('task_type', 'Base') != 'Base' or meta.get('x_vector_only_mode'):
                    raise ContractError('Only full-ICL Base profiles can be imported')
                filename = meta.get('ref_audio_filename', 'ref_audio.wav')
                if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.wav', filename):
                    raise ContractError('Profile reference must be a WAV filename')
                payloads.append(clone_payload(meta.get('name', root), meta.get('language', 'Auto'),
                                               meta.get('ref_text'), archive.read(root + '/' + filename)))
            return payloads
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, RuntimeError) as error:
        if isinstance(error, ContractError): raise
        raise ContractError('Invalid Studio profile archive') from None


class StudioAdapter:
    def __init__(self, artifacts, *, control=studio.control, preview=None):
        self.artifacts = Path(artifacts).resolve()
        self.control, self.preview_speech = control, preview

    def upload(self, filename, limit=MAX_FILE):
        path = Path(filename or '').resolve()
        if not path.is_relative_to(self.artifacts) or not path.is_file() or path.stat().st_size > limit:
            raise ContractError('Select a bounded Studio upload')
        return path.read_bytes()

    def output(self, data, suffix):
        if len(data) > MAX_ARCHIVE: raise ContractError('Studio output exceeds limit')
        self.artifacts.mkdir(parents=True, exist_ok=True)
        # Bounded total cache; explicit retention cleanup is handled by Gradio.
        total = sum(p.stat().st_size for p in self.artifacts.rglob('*') if p.is_file())
        if total + len(data) > 64 * 1024**2:
            raise ContractError('Studio temporary storage is full; close Studio and let cache retention expire')
        path = self.artifacts / (secrets.token_hex(16) + suffix)
        path.write_bytes(data)
        return str(path)

    async def inventory(self):
        result = await self.control('profiles')
        return [item for item in result.get('voices', []) if item.get('task_type', item.get('task', 'Base')) == 'Base']

    async def create(self, name, language, reference, text):
        payload = clone_payload(name, language, text, self.upload(reference))
        await self.control('create_profile', payload, confirmed=True)
        return 'Saved Base clone. Refresh the shared response-voice selector to use it in Convo.'

    async def read_profile(self, identity):
        item = await self.control('profile', item_id=profile_id(identity))
        return item['name'], item.get('language', 'Auto'), item.get('ref_text', '')

    async def edit(self, identity, name, language, text):
        # Reference bytes stay server-side. Name/text limits match clone creation.
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 120 or not isinstance(text, str) or not 0 < len(text.strip()) <= 4000:
            raise ContractError('Provide a bounded name and matching reference transcript')
        await self.control('edit_profile', {'name': name, 'language': language, 'ref_text': text},
                           item_id=profile_id(identity), confirmed=True)
        return 'Profile saved. Active response snapshots are unchanged.'

    async def export_profile(self, identity):
        identity = profile_id(identity)
        item = await self.control('profile', item_id=identity)
        audio = validate_wav(base64.b64decode(item['ref_audio'], validate=True))
        meta = {key: item.get(key) for key in ('name', 'language', 'ref_text', 'origin')}
        meta.update(profile_id=identity, task_type='Base', ref_audio_filename='ref_audio.wav', x_vector_only_mode=False)
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(identity + '/meta.json', json.dumps(meta))
            archive.writestr(identity + '/ref_audio.wav', audio)
            archive.writestr('MANIFEST.json', json.dumps([meta]))
        return self.output(content.getvalue(), '.zip')

    async def import_profiles(self, filename):
        payloads = read_archive(self.upload(filename, MAX_ARCHIVE))
        count = 0
        for payload in payloads:
            try: await self.control('create_profile', payload, confirmed=True)
            except Exception:
                raise ContractError(f'Imported {count} of {len(payloads)} profiles before the service rejected a write; refresh before retrying') from None
            count += 1
        return f'Imported {count} profiles with new identities; existing profiles were not overwritten.'

    async def preview(self, identity, text, model, tuning_profile=''):
        if self.preview_speech is None: raise ContractError('Studio preview is not configured')
        if not isinstance(tuning_profile, str): raise ContractError('Invalid shared tuning selection')
        if tuning_profile: profile_id(tuning_profile)
        result = await self.preview_speech({'voice': 'clone:' + profile_id(identity), 'text': text,
                                           'model': model, 'tuning_profile': tuning_profile})
        return self.output(base64.b64decode(result['audio'], validate=True), '.wav')

    async def tuning_read(self):
        return await self.control('tuning')

    async def tuning_save(self, identity, document):
        if not isinstance(document, dict) or set(document) - (TUNING_FIELDS | {'name', 'revision'}):
            raise ContractError('Unsupported tuning fields')
        if 'max_reference_seconds' in document:
            document = dict(document, max_reference_seconds=tuning_reference_seconds(document['max_reference_seconds']))
        await self.control('edit_tuning', document, item_id=profile_id(identity), confirmed=True)
        return 'Tuning definition saved; refresh to read its new revision.'

    async def tuning_create(self, name, source):
        await self.control('create_tuning', {'name': name, 'clone_from': profile_id(source)}, confirmed=True)
        return 'Custom tuning profile created. Refresh to edit it.'

    async def tuning_export(self):
        result = await self.control('export_tuning')
        return self.output(json.dumps(result, indent=2).encode(), '.json')

    async def tuning_import(self, filename):
        result = json.loads(self.upload(filename, 512 * 1024))
        await self.control('import_tuning', result, confirmed=True)
        return 'Tuning import saved. Refresh to review current definitions.'
