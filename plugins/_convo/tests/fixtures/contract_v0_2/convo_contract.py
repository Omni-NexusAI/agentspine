"""Dependency-free, fail-closed contracts shared by host and sidecar."""
from __future__ import annotations

import copy
import base64
import binascii
import io
import ipaddress
import math
import os
import re
import wave
from dataclasses import asdict, dataclass, fields
from urllib.parse import urlsplit

from .convo_policy import GateProfile
from .speech_providers import CONNECTIONS as TTS_CONNECTIONS, KEY_SOURCES, PROVIDERS as SPEECH_PROVIDERS, validate_provider_saved, validate_saved


class SpeechBusy(Exception):
    """The speech provider is still finishing an earlier request that it tracks (for example a synthesis detached by a
    barge-in). It is a reason to wait, not a failure. A rejected request or an uncertain outcome is not this."""
    def __init__(self, state='running'):
        super().__init__('Speech provider is still finishing a previous request')
        self.state = state


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Capabilities:
    audio_input: bool = False
    incremental_input: bool = False
    audio_output: bool = False
    duplex: bool = False
    cancellation: bool = False
    tool_calls: bool = False
    cloning: bool = False
    voice_design: bool = False
    instruction_control: bool = False
    model_lifecycle: bool = False
    native_pcm: bool = False

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ContractError("Capabilities must be an object")
        known = {f.name for f in fields(cls)}
        if set(value) - known or any(type(v) is not bool for v in value.values()):
            raise ContractError("Unknown or non-boolean capability")
        result = cls(**value)
        if result.incremental_input and not result.audio_input:
            raise ContractError("Incremental input requires audio input")
        if result.duplex and not (result.incremental_input and result.audio_output and result.cancellation):
            raise ContractError("Duplex requires incremental audio, output and cancellation")
        return result

    def public(self):
        return asdict(self)


def merge(*values):
    """Deep merge; preserve unknown fields and explicit false/empty values."""
    result = {}
    for value in values:
        for key, item in (value or {}).items():
            result[key] = merge(result.get(key, {}), item) if isinstance(item, dict) and isinstance(result.get(key, {}), dict) else copy.deepcopy(item)
    return result


# Two separate conversational controls (see convo_voice_style / convo_task_updates). They shape how long answers are and
# when finished background work is announced aloud; neither touches personality, permissions or what a job may do.
RESPONSE_DETAIL = ("concise", "balanced", "detailed")
SPOKEN_UPDATES = ("completion", "milestones", "frequent")
ECHO_GUARD = ("adaptive", "strict", "off")
VOICE_MESSAGE_DRAFT = ("single", "continuous")
DECIDER_SOURCES = ("", "system_one", "convo")
DECIDER_KINDS = ("d1_llama_cpp",)
DECIDER_HOSTS = {"host.docker.internal", "localhost"}
PROVIDER_ID = re.compile(r"[a-z0-9_]{1,64}")


DEFAULTS = {
    "enabled": False,
    "ambient_enabled": False,
    "remote_ambient_consent": False,
    "transcription_enabled": False,
    # Agent Zero's "draft" voice-message handling leaves one dictated phrase in the composer ("single", the host's own behaviour);
    # "continuous" keeps adding each finished phrase until you send or stop. Agent Zero itself stores only send|draft.
    "voice_message_draft": "single",
    "developer_mode": False,
    "ai_link_development_mode": False,
    "system_one": {"enabled": False, "backend": ""},
    # The turn-taking decider: '' = the registered decision backend above (unchanged), "system_one" = System One's own decider,
    # "convo" = a separate decider of `kind` at `url`, which must be on this machine.
    "decider": {"source": "", "kind": "d1_llama_cpp", "url": "", "model": ""},
    "style": "",
    # Default answer length. "completion" is today's behaviour: a delegated job is spoken about only when it ends.
    "response_detail": "balanced",
    "spoken_updates": "completion",
    "activation_names": [],
    "hotkey": "Ctrl+Shift+Space",
    "sidecar": {"url": "http://convo-runtime:8090", "token_env": "CONVO_SIDECAR_TOKEN", "remote": False, "managed": False},
    # provider: an Agent Zero provider id ('' = the endpoint `url` exactly as entered). key_source "provider" uses Agent Zero's
    # own key for that provider when it holds one (Convo's write-only key otherwise); "convo" always uses Convo's key. api_base
    # overrides the provider's own address. Speech: provider is a speech catalog id ('' = derived from `backend`), connection says
    # how audio.cpp is reached ('' = derived), and `backend` stays the runtime protocol the provider resolves to.
    "roles": {
        "conversation": {"provider": "", "key_source": "provider", "api_base": "", "url": "http://host.docker.internal:8080/v1", "model": "", "token_env": "", "remote": False, "capabilities": {"audio_input": True, "cancellation": True, "tool_calls": True}},
        "classifier": {"url": "", "model": "", "token_env": "", "remote": False, "capabilities": {"audio_input": True, "cancellation": True}},
        "tts": {"backend": "openai", "provider": "", "connection": "", "key_source": "provider", "accepts_instructions": False, "url": "http://convo-audio:8080/v1", "model": "", "voice": "", "token_env": "", "remote": False, "capabilities": {"audio_output": True, "cloning": True}},
    },
    "compaction": {"trigger": 0.70, "target": 0.50, "recent_exchanges": 6, "context_tokens": 16384, "output_reserve": 2048},
    "direct_timeout": 30,
    "clarification_cooldown": 60,
    "audio_token_reserve": 4096,
    "max_audio_seconds": 20,
    # Browser echo protection: adaptive = playback-reference guard (releases only confirmed independent speech), strict = no barge-in
    # during playback, off = the previous capture path (native echo cancellation only).
    "echo_guard": "adaptive",
    # Turn engine (backend). All off or bounded by default; each is described in AGENTS.md. Seconds are memory-only horizons.
    "speculative_reply": False,       # start the main model on a completed utterance while the classifier decides; held until admitted
    "main_listen_decision": False,    # the main model may answer exactly [LISTEN] when the audio was not meant for it (suppress only)
    "continuation_seconds": 7,        # a following fragment within this gap is joined to the unanswered one (0 = off; memory only)
    "first_clause_speech": False,     # speak the first clause early and synthesise the next phrase while the first one plays
    "recent_remarks_seconds": 0,      # keep recently ignored speech in memory as context for a later admitted turn (0 = off, max 60)
    # Where the typed speech gate acts (convo_policy.GateProfile; a test keeps this equal to GATE_DEFAULTS). Measured for one decision model.
    "policy_gate": {"respond_min": 0.30, "ambient_respond_min": 0.90, "interrupt_min": 0.60, "ignore_min": 1.0, "name_min": 0.90,
                    "clarify_min": 0.30, "clarify_elsewhere_max": 0.50},
}


def endpoint(value):
    url = value.get("url", "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ContractError("Configure an explicit HTTP(S) endpoint without embedded credentials")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        local = address.is_loopback or (address.is_private and not address.is_link_local and not address.is_unspecified)
    except ValueError:
        local = host in {"localhost", "host.docker.internal", "convo-runtime", "convo-audio", "kokoro-worker"}
    if not local and not value.get("remote", False):
        raise ContractError("Remote endpoint requires explicit remote-provider opt-in")
    if not local and parsed.scheme != "https":
        raise ContractError("Remote providers require HTTPS")
    return url


def decider_endpoint(url):
    """A separate decider's base URL: plain http on this machine only (the host alias, localhost or a loopback address), the same
    rule the decision plugin applies. A remote decider would send the user's microphone audio off the machine."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        try:
            local = host in DECIDER_HOSTS or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if parsed.scheme != "http" or not local or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        parsed.port                                      # an invalid port ("localhost:abc", out of range) raises ValueError here
    except ValueError as exc:
        raise ContractError("The decider must be a plain http address on this machine (host.docker.internal, localhost or loopback)") from exc
    return url.rstrip("/")


def credentials(value):
    name = value.get("token_env", "")
    if name and (not isinstance(name, str) or not name.replace("_", "").isalnum()):
        raise ContractError("Token must reference an environment variable")
    # _convo_key is injected only by the host's private settings resolver.
    # Explicit environment references remain authoritative and never fall back.
    token = os.environ.get(name, "") if name else value.get('_convo_key', '')
    return {"Authorization": "Bearer " + token} if token else {}


def validate_settings(value):
    if not isinstance(value, dict):
        raise ContractError("Convo settings must be an object")
    result = merge(DEFAULTS, value)
    for key in ("enabled", "ambient_enabled", "remote_ambient_consent", "transcription_enabled", "developer_mode", "ai_link_development_mode"):
        if type(result[key]) is not bool:
            raise ContractError(key + " must be boolean")
    if not isinstance(result["roles"], dict) or not isinstance(result["sidecar"], dict):
        raise ContractError("Provider settings must be objects")
    consumer = result['system_one']
    if not isinstance(consumer, dict) or type(consumer.get('enabled')) is not bool:
        raise ContractError('System One consumer settings require a boolean enabled flag')
    backend = consumer.get('backend')
    if not isinstance(backend, str) or len(backend) > 64 or (backend and any(not (c.isascii() and (c.isalnum() or c in '_-')) for c in backend)):
        raise ContractError('System One backend must be a bounded registered identifier')
    if consumer['enabled'] and not backend:
        raise ContractError('Select a registered backend before enabling the System One consumer')
    if type(result["sidecar"].get("remote", False)) is not bool:
        raise ContractError("Sidecar remote consent must be boolean")
    if type(result['sidecar']['managed']) is not bool or (result['sidecar']['managed'] and result['sidecar']['remote']):
        raise ContractError('Managed runtime must be local and managed must be boolean')
    for field in ("url", "token_env"):
        if not isinstance(result["sidecar"].get(field, ""), str):
            raise ContractError("Invalid sidecar field")
    for role in result["roles"].values():
        if not isinstance(role, dict) or type(role.get("remote", False)) is not bool:
            raise ContractError("Invalid role configuration")
        for field in ("url", "model", "voice", "token_env", "instructions", "tuning_profile"):
            if not isinstance(role.get(field, ""), str) or len(role.get(field, "")) > 2000:
                raise ContractError("Invalid provider field: " + field)
        Capabilities.parse(role.get("capabilities", {}))
        if role.get("url"):
            endpoint(role)
    endpoint(result["sidecar"])
    if "saved" in result["roles"]["tts"]:
        try:
            result["roles"]["tts"]["saved"] = validate_saved(result["roles"]["tts"]["saved"])
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    c = result["compaction"]
    if not isinstance(c, dict) or any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in c.values()):
        raise ContractError("Context budget must contain finite positive numbers")
    if any(type(c[key]) is not int for key in ("recent_exchanges", "context_tokens", "output_reserve")):
        raise ContractError("Context counts must be integers")
    for key in ("direct_timeout", "clarification_cooldown", "audio_token_reserve", "max_audio_seconds"):
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]):
            raise ContractError("Invalid numeric setting: " + key)
    for key in ("speculative_reply", "main_listen_decision", "first_clause_speech"):
        if type(result[key]) is not bool:
            raise ContractError(key + " must be boolean")
    for key, limit in (("continuation_seconds", 15), ("recent_remarks_seconds", 60)):
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]) or not 0 <= result[key] <= limit:
            raise ContractError(key + " must be a number from 0 to " + str(limit))
    if not 1 <= result["max_audio_seconds"] <= 45 or not 1 <= result["audio_token_reserve"] <= c["context_tokens"]:
        raise ContractError("Configure a measured audio-token reserve and bounded audio turns")
    if not (0.1 <= c["target"] < c["trigger"] <= 0.9) or c["recent_exchanges"] < 6 or c["context_tokens"] <= c["output_reserve"]:
        raise ContractError("Invalid context budget")
    if not 0 < result["direct_timeout"] <= 30 or result["clarification_cooldown"] < 60:
        raise ContractError("Unsafe action deadline or clarification cooldown")
    try:
        GateProfile.from_settings(result["policy_gate"])
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if result["roles"]["tts"].get("backend", "openai") not in {"openai", "host_kokoro", "managed_audio"}:
        raise ContractError("Unknown speech adapter")
    if not isinstance(result["style"], str) or len(result["style"]) > 1600:
        raise ContractError("Voice style must be at most 1600 characters")
    if not isinstance(result["response_detail"], str) or result["response_detail"] not in RESPONSE_DETAIL:
        raise ContractError("Response detail must be one of: " + ", ".join(RESPONSE_DETAIL))
    if not isinstance(result["spoken_updates"], str) or result["spoken_updates"] not in SPOKEN_UPDATES:
        raise ContractError("Spoken updates must be one of: " + ", ".join(SPOKEN_UPDATES))
    if not isinstance(result["echo_guard"], str) or result["echo_guard"] not in ECHO_GUARD:
        raise ContractError("Echo protection must be one of: " + ", ".join(ECHO_GUARD))
    if not isinstance(result["activation_names"], list) or len(result["activation_names"]) > 8 or any(not isinstance(n, str) or not 1 <= len(n) <= 80 for n in result["activation_names"]):
        raise ContractError("Configure at most eight activation names")
    hotkey = result["hotkey"]
    if not isinstance(hotkey, str) or len(hotkey) > 80:
        raise ContractError("Invalid focused-app shortcut")
    if hotkey:
        parts = hotkey.split("+")
        if not parts[-1] or any(p not in {"Ctrl", "Alt", "Shift", "Meta"} for p in parts[:-1]) or len(set(parts)) != len(parts):
            raise ContractError("Shortcut must use Ctrl, Alt, Shift or Meta modifiers and one key")
    _validate_providers(result)
    return result


def _validate_providers(result):
    """Provider, decider and drafting settings. Each is additive: empty or default values keep the previous behaviour."""
    if not isinstance(result["voice_message_draft"], str) or result["voice_message_draft"] not in VOICE_MESSAGE_DRAFT:
        raise ContractError("Voice message drafting must be one of: " + ", ".join(VOICE_MESSAGE_DRAFT))
    conversation, tts = result["roles"]["conversation"], result["roles"]["tts"]
    for role in (conversation, tts):
        if not isinstance(role.get("key_source", "provider"), str) or role.get("key_source", "provider") not in KEY_SOURCES:
            raise ContractError("Key source must be one of: " + ", ".join(KEY_SOURCES))
    provider = conversation.get("provider", "")
    if not isinstance(provider, str) or (provider and not PROVIDER_ID.fullmatch(provider)):
        raise ContractError("Conversation provider must be an Agent Zero provider id")
    api_base = conversation.get("api_base", "")
    if not isinstance(api_base, str) or len(api_base) > 2000:
        raise ContractError("Invalid provider field: api_base")
    if api_base:
        endpoint({"url": api_base, "remote": conversation.get("remote", False)})
    if not isinstance(tts.get("provider", ""), str) or tts.get("provider", "") not in ("",) + tuple(SPEECH_PROVIDERS):
        raise ContractError("Unknown speech provider")
    if not isinstance(tts.get("connection", ""), str) or tts.get("connection", "") not in ("",) + TTS_CONNECTIONS:
        raise ContractError("Speech connection must be external or managed")
    if type(tts.get("accepts_instructions", False)) is not bool:          # "this endpoint accepts delivery instructions" (declared by the user)
        raise ContractError("Speech instructions declaration must be true or false")
    if "provider_saved" in tts:
        try:
            tts["provider_saved"] = validate_provider_saved(tts["provider_saved"])
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    decider = result["decider"]
    if not isinstance(decider, dict):
        raise ContractError("Decider settings must be an object")
    if not isinstance(decider.get("source", ""), str) or decider.get("source", "") not in DECIDER_SOURCES:
        raise ContractError("Decider source must be empty, system_one or convo")
    if not isinstance(decider.get("kind"), str) or decider.get("kind") not in DECIDER_KINDS:
        raise ContractError("Decider kind must be one of: " + ", ".join(DECIDER_KINDS))
    for field in ("url", "model"):
        if not isinstance(decider.get(field, ""), str) or len(decider.get(field, "")) > 2000:
            raise ContractError("Invalid decider field: " + field)
    if decider.get("url"):
        decider_endpoint(decider["url"])


def validate_audio(audio, maximum=45):
    """Validate the actual payload, not the browser's duration declaration."""
    try:
        if not isinstance(audio, str) or not 0 < len(audio) <= 4 * 1024 * 1024:
            raise ValueError()
        raw = base64.b64decode(audio, validate=True)
        with wave.open(io.BytesIO(raw)) as wav:
            count, rate = wav.getnframes(), wav.getframerate()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or rate not in (16000, 24000, 48000) or not 0 < count <= rate * maximum:
                raise ValueError()
            if len(wav.readframes(count)) != count * 2:
                raise ValueError()
    except (ValueError, TypeError, EOFError, wave.Error, binascii.Error) as exc:
        raise ContractError("Expected a bounded, complete mono PCM16 WAV turn") from exc
    return audio


def input_modality_conflict(config, decision_status=None):
    """Why the conversation model and the turn-policy classifier cannot work on the same input, or ''.

    Direct audio is one decision about the whole conversation: a classifier that cannot take audio never turns an
    audio conversation into transcript input behind the user's back (that would silently drop the main model's audio).
    The user is told, before and at start, which side to change. `decision_status` is System One's readiness when it is
    the selected classifier (its declared modality combinations decide), else None (the classifier's own flag decides)."""
    conversation_audio = bool(config['roles']['conversation']['capabilities'].get('audio_input'))
    if decision_status is not None:
        if not decision_status.get('ready'):
            return ''                                     # its own readiness check already says why
        combinations = decision_status.get('capabilities', {}).get('modality_combinations', [])
        if conversation_audio and ['audio', 'text'] not in combinations:
            return 'The selected decision backend must support text context plus direct audio; it cannot silently convert this conversation to transcript input.'
        if not conversation_audio and ['text'] not in combinations:
            return 'The selected decision backend does not support this text-only conversation.'
        return ''
    if conversation_audio and not config['roles']['classifier']['capabilities'].get('audio_input', False):
        return ('The conversation model takes audio, so the turn-policy classifier must too: enable its audio input, choose an '
                'audio-capable classifier, or turn off the conversation model\'s audio input for a transcript-only conversation. '
                'Convo will not silently convert this conversation to transcript input.')
    return ''


def public_settings(config):
    """Browser runtime needs neither endpoints nor credential/environment references."""
    from plugins._convo.helpers.convo_system_one import readiness
    selected = config['system_one']['enabled']
    system_one = readiness(config) if selected else {'ready': False, 'state': 'disabled', 'capabilities': {}}
    return {key: copy.deepcopy(config[key]) for key in (
        "enabled", "ambient_enabled", "remote_ambient_consent", "transcription_enabled",
        "developer_mode", "ai_link_development_mode", "hotkey", "max_audio_seconds", "echo_guard", "activation_names",
        "voice_message_draft") } | {
        "remote_processing": any(r.get("remote", False) for r in config["roles"].values()) or config["sidecar"].get("remote", False) or system_one.get('remote', False),
        "system_one": system_one,
        # Only the conversation model's own audio capability decides this. A classifier that cannot take audio
        # (or an unavailable/text-only decider) never downgrades a direct-audio conversation into transcript input;
        # start and readiness reject the combination explicitly instead (input_modality_conflict).
        "text_input_required": not config['roles']['conversation']['capabilities'].get('audio_input', False),
    }
