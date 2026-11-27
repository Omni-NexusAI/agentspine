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
DECIDER_HOSTS = {"host.docker.internal", "localhost"}       # the previous this-machine-only rule (decision plugin config, rollback-safe mirror)
# Host names that are this machine or Convo's own containers; with loopback and private-network addresses they are "local".
LOCAL_NAMES = {"localhost", "host.docker.internal", "convo-runtime", "convo-audio", "kokoro-worker"}
# Name endings reserved for a private network: mDNS, home routers, RFC 8375/6761, ICANN's .internal and Tailscale's MagicDNS.
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost", ".ts.net")
# Tailscale's IPv4 range (shared address space, never routed on the public internet); its IPv6 range is unique local already.
TAILNET = ipaddress.ip_network("100.64.0.0/10")
# A one-label machine or container name. It must start with a letter: an all-digit or 0x name is an IPv4 address in disguise.
LOCAL_LABEL = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
# What a credential environment reference may hold: the name of a variable, never a key or an address.
ENV_REFERENCE = re.compile(r"[A-Za-z0-9_]{1,128}")
CREDENTIAL_LABELS = {"roles.conversation": "Conversation model", "roles.classifier": "Classifier", "roles.tts": "Speech output",
                     "sidecar": "Conversation gateway"}
CLASSIFIER_KINDS = ("auto", "decision", "chat")
CONTEXT_WINDOW = (1024, 1_000_000)
PROVIDER_ID = re.compile(r"[a-z0-9_]{1,64}")


# The journal key of a conversation that spans chats (settings `conversation_scope`): a key that can never be a chat's id.
GLOBAL_SCOPE = "convo:global"

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
    # The classifier's decision source: '' = the registered decision backend above (unchanged), "system_one" = System One's own
    # decider (the card's "Use System One's decider" toggle), "convo" = Convo's own decision classifier of `kind` at `url`.
    # With roles.classifier.provider set, the classifier catalog derives these on every write (convo_classifier_catalog.normalize).
    "decider": {"source": "", "kind": "d1_llama_cpp", "url": "", "model": ""},
    "style": "",
    # Default answer length. "completion" is today's behaviour: a delegated job is spoken about only when it ends.
    "response_detail": "balanced",
    "spoken_updates": "completion",
    "activation_names": [],
    "hotkey": "Ctrl+Shift+Space",
    # One combination per action, "" = off (HOTKEY_* below). The older single `hotkey` above stays as a second one for starting and stopping.
    "hotkeys": {"toggle_conversation": "Alt+Shift+C", "mute": "Alt+Shift+M", "push_to_talk": "Alt+Shift+Space", "dictate": "Alt+Shift+D",
                "stop_reply": "Alt+Shift+S", "open_panel": "", "open_settings": "", "run_action": "", "dismiss_action": ""},
    "sidecar": {"url": "http://convo-runtime:8090", "token_env": "CONVO_SIDECAR_TOKEN", "remote": False, "managed": True},   # Convo runs its own gateway
    # provider: an Agent Zero provider id ('' = the endpoint `url` exactly as entered). key_source "provider" uses Agent Zero's
    # own key for that provider when it holds one (Convo's write-only key otherwise); "convo" always uses Convo's key. api_base
    # overrides the provider's own address. Speech: provider is a speech catalog id ('' = derived from `backend`), connection says
    # how audio.cpp is reached ('' = derived), and `backend` stays the runtime protocol the provider resolves to. Classifier: provider
    # is a classifier catalog id ('' = the legacy endpoint `url` exactly as entered), kind auto|decision|chat (auto = the provider's
    # first kind), context_window as System One's decider; `url` (chat) and `decider.*` (decision) stay what the runtime reads.
    "roles": {
        "conversation": {"provider": "", "key_source": "provider", "api_base": "", "url": "http://host.docker.internal:8080/v1", "model": "", "token_env": "", "remote": False, "capabilities": {"audio_input": True, "cancellation": True, "tool_calls": True}},
        "classifier": {"provider": "", "key_source": "provider", "api_base": "", "context_window": 8192, "kind": "auto", "url": "", "model": "", "token_env": "", "remote": False, "capabilities": {"audio_input": True, "cancellation": True}},
        "tts": {"backend": "openai", "provider": "", "connection": "", "key_source": "provider", "accepts_instructions": False, "url": "http://convo-audio:8080/v1", "model": "", "voice": "", "token_env": "", "remote": False, "capabilities": {"audio_output": True, "cloning": True},
                # Advanced synthesis controls for your own audio.cpp server (convo_synthesis): a profile and overrides, applied per reply.
                "tuning": {"profile_id": "", "overrides": {}}},
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
    "first_clause_speech": True,      # speak the first full sentence, then the rest as one request (False = the whole reply at once)
    "recent_remarks_seconds": 0,      # keep recently ignored speech in memory as context for a later admitted turn (0 = off, max 60)
    "answer_tokens": 1024,            # the longest spoken answer the conversation model may write (thinking is asked off for voice)
    # Which conversation voice belongs to: "chat" (the selected chat's own conversation, as before) or "global" (one conversation
    # across all chats, journalled under GLOBAL_SCOPE; voice started with no chat selected is always that one). In a conversation
    # across chats, `task_chat` says where a task goes when the user names no chat: "new" (a new chat, reused for the later tasks
    # of that voice session) or "selected" (the chat selected in the interface; a new one when none is).
    "conversation_scope": "chat",
    "task_chat": "new",
    # What the voice agent may do (convo_tools groups): a group switched off is not offered to the model at all. Permissions, not
    # behaviour: what it takes to run a tool that is offered is decided by convo_policy.authorize.
    "voice_tools": {"web": True, "memory": True, "tasks": True, "system": True, "timers": True, "chats": True, "senses": True,
                    "interface": True},
    # Who writes the summary chapters (convo_summarizer): "conversation" (the conversation model, from its own cached start) or
    # "utility" (Agent Zero's utility model: only when it runs on your own machines, or with utility_summary_consent).
    "summary_writer": "conversation",
    "utility_summary_consent": False,  # consent for a utility model outside your network to receive the conversation
    "barge_in_seconds": 1.0,         # your confirmed speech over a reply after which it stops, whatever the classifier says
    # The sound panel (browser): the silence that ends your turn, and the one speech threshold in dBFS (quieter audio is muted and
    # never counts as speech; -66 = off). The panel keeps its own values per microphone and speaker; these are the defaults.
    "end_of_turn_seconds": 0.9,
    "noise_gate_db": -50,
    # Who the assistant is: the name shown in the panel ('' = the profile's title) and the Agent Zero profile it speaks as
    # ('' = the chat's own profile). Delegated work always goes to the chat, with that chat's profile.
    "voice_name": "",
    "voice_profile": "",
    # Developer view only, both off by default: journal rows that are not turns, and generated text that was never delivered.
    "show_diagnostic_events": False,
    "show_rejected_responses": False,
    # Where the typed speech gate acts (convo_policy.GateProfile; a test keeps this equal to GATE_DEFAULTS). Measured for one decision model.
    "policy_gate": {"respond_min": 0.30, "ambient_respond_min": 0.90, "interrupt_min": 0.60, "ignore_min": 1.0, "name_min": 0.90,
                    "clarify_min": 0.30, "clarify_elsewhere_max": 0.50},
}


def local_host(host):
    """This machine, Convo's own containers or your network: the one rule for what counts as local, for every model connection
    (conversation model, classifier, decision server, speech, gateway). Addresses: loopback, private (RFC 1918, IPv6 unique local),
    not link-local or unspecified, and a Tailscale tailnet (100.64.0.0/10). Names: Convo's own, a one-label machine name and the
    private-use endings in LOCAL_SUFFIXES (a *.ts.net name is taken to be in your tailnet). Judged as written; nothing is looked
    up. Anything else is public."""
    host = (host or "").lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in LOCAL_NAMES or bool(LOCAL_LABEL.fullmatch(host)) or (host.endswith(LOCAL_SUFFIXES) and not host.startswith("."))
    return address.is_loopback or (address.is_private and not address.is_link_local and not address.is_unspecified) \
        or (address.version == 4 and address in TAILNET)


def endpoint(value):
    url = value.get("url", "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ContractError("Configure an explicit HTTP(S) endpoint without embedded credentials")
    local = local_host(parsed.hostname)
    if not local and not value.get("remote", False):
        raise ContractError("Remote endpoint requires explicit remote-provider opt-in: " + parsed.hostname + " is outside this machine and "
                            "your network (LAN or Tailscale), so turn on “Allow remote processing” to use it.")
    if not local and parsed.scheme != "https":
        raise ContractError("Remote providers require HTTPS: " + parsed.hostname + " is outside this machine and your network (LAN or Tailscale).")
    return url


class AddressRefused(ContractError):
    """A classifier or decision-server address the locality rule refuses. `code`: `remote` (a public address without the
    classifier's remote consent) or `invalid` (anything else, including a public address over plain http)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


CLASSIFIER_REMOTE = ("A public classifier address needs “Allow remote processing” for the classifier: it would receive your "
                     "microphone audio.")
CLASSIFIER_INVALID = ("The classifier address must be an http(s) address with a valid port and no user name, password, query or "
                      "fragment, for example http://host.docker.internal:<port>.")


def classifier_url(url, remote=False, path=True):
    """A classifier's or decision server's address under the conversation model's rule (`endpoint`): this machine and private
    networks as they are, http or https; a public address only with the classifier's remote consent (`roles.classifier.remote`)
    and over HTTPS. Never credentials, a query or a fragment; a valid port. `path=False` also refuses a path (a server root that
    the decision route is appended to). Returns the address without a trailing '/'; raises AddressRefused."""
    try:
        if not isinstance(url, str):
            raise ValueError()
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        parsed.port                                      # an invalid port ("localhost:abc", out of range) raises ValueError here
    except (ValueError, TypeError) as exc:
        raise AddressRefused("invalid", CLASSIFIER_INVALID) from exc
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password or parsed.query or parsed.fragment \
            or (not path and parsed.path not in ("", "/")):
        raise AddressRefused("invalid", CLASSIFIER_INVALID)
    if not local_host(host):
        if remote is not True:
            raise AddressRefused("remote", CLASSIFIER_REMOTE)
        if parsed.scheme != "https":
            raise AddressRefused("invalid", "A public classifier address must use HTTPS.")
    return url.rstrip("/")


def decider_endpoint(url, remote=False):
    """The decision server root in `decider.url` (the decision route /v1/systemone is appended to it, so no path): the classifier
    rule above, with public addresses only under the classifier's remote consent and over HTTPS."""
    return classifier_url(url, remote, path=False)


def credential_reference_problem(name):
    """Why `name` cannot be a credential environment reference, or '' when it can (empty, or a variable name). The value itself is
    never part of the answer: a key pasted into this field must not travel back in an error."""
    if name == "" or (isinstance(name, str) and ENV_REFERENCE.fullmatch(name)):
        return ""
    text = name.strip() if isinstance(name, str) else ""
    if "://" in text or text.lower().startswith(("http:", "https:", "www.")):
        hint = " This looks like an address: addresses go in API base URL."
    elif len(text) >= 16 and " " not in text:
        hint = " This looks like a key itself: enter keys in the API key field, where they are stored masked."
    else:
        hint = ""
    return "Credential environment reference must be the name of an environment variable, such as MY_API_KEY (letters, digits and underscores)." + hint


def credential_reference_problems(config):
    """{setting path: problem} for every saved credential environment reference that is not a variable name (each role's and
    the gateway's). A settings read reports these instead of failing, so the field can be cleared."""
    places = [("roles." + name, role) for name, role in (config.get("roles") or {}).items() if isinstance(role, dict)]
    places.append(("sidecar", config.get("sidecar") if isinstance(config.get("sidecar"), dict) else {}))
    found = {}
    for prefix, node in places:
        name = node.get("token_env", "")
        if name and credential_reference_problem(name):
            found[prefix + ".token_env"] = credential_reference_problem(name)
    return found


def credential_reference_label(path):
    return CREDENTIAL_LABELS.get(path.rsplit(".", 1)[0], path.rsplit(".", 1)[0])


def refuse_new_credential_references(before, after):
    """Refuse a credential environment reference that changed in this write and is not a variable name. One saved before this
    check (or by hand) never blocks unrelated changes; it is reported by the settings read until it is cleared."""
    for path, problem in credential_reference_problems(after).items():
        if _lookup_path(before, path) != _lookup_path(after, path):
            raise ContractError(credential_reference_label(path) + ": " + problem)


def _lookup_path(value, path):
    for part in path.split("."):
        value = value.get(part, "") if isinstance(value, dict) else ""
    return value


def credentials(value):
    name = value.get("token_env", "")
    if name and credential_reference_problem(name):
        raise ContractError(credential_reference_problem(name))
    # _convo_key is injected only by the host's private settings resolver.
    # Explicit environment references remain authoritative and never fall back.
    token = os.environ.get(name, "") if name else value.get('_convo_key', '')
    return {"Authorization": "Bearer " + token} if token else {}


# Hotkeys: what a combination may be made of (webui/hotkeys.js holds the same tables; tests/fixtures/hotkey_cases.json holds the two
# together). The key is a KeyboardEvent.code, letters and digits without their prefix. A key that types needs Ctrl, Alt or Meta; the
# reserved ones belong to the browser or to Agent Zero's page (Meta counts as Ctrl there: a Mac uses Cmd for the same).
HOTKEY_MODIFIERS = ("Ctrl", "Alt", "Shift", "Meta")
HOTKEY_QUIET = re.compile(r"F(?:[1-9]|1[0-9]|2[0-4])|MediaPlayPause|MediaStop|MediaTrackNext|MediaTrackPrevious|Pause|ScrollLock")
HOTKEY_KEY = re.compile(r"[A-Z]|[0-9]|Numpad[0-9]|Space|Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown|Insert|Minus|Equal|BracketLeft|BracketRight|"
                        r"Backslash|Semicolon|Quote|Comma|Period|Slash|Backquote|IntlBackslash|IntlRo|IntlYen|"
                        r"Numpad(?:Add|Subtract|Multiply|Divide|Decimal)|" + HOTKEY_QUIET.pattern)
HOTKEY_RESERVED = frozenset(("Ctrl+W Ctrl+T Ctrl+N Ctrl+R Ctrl+L Ctrl+F Ctrl+Q Ctrl+Shift+W Ctrl+Shift+T Ctrl+Shift+N Ctrl+Shift+R Ctrl+A Ctrl+C "
                             "Ctrl+V Ctrl+X Ctrl+Z Ctrl+Y Ctrl+Shift+Z F5 Ctrl+F5 Shift+F5 F11 F12 Alt+F4 Ctrl+F4 Alt+ArrowLeft Alt+ArrowRight "
                             "Alt+Space").split()) | {"Ctrl+%d" % digit for digit in range(10)}


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
    if not 0.3 <= c.get("trigger", 0.7) <= 0.9:
        raise ContractError("The compaction threshold must be between 30% and 90% of the context window")
    for key in ("direct_timeout", "clarification_cooldown", "audio_token_reserve", "max_audio_seconds"):
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]):
            raise ContractError("Invalid numeric setting: " + key)
    for key in ("speculative_reply", "main_listen_decision", "first_clause_speech"):
        if type(result[key]) is not bool:
            raise ContractError(key + " must be boolean")
    for key, limit in (("continuation_seconds", 15), ("recent_remarks_seconds", 60)):
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]) or not 0 <= result[key] <= limit:
            raise ContractError(key + " must be a number from 0 to " + str(limit))
    if type(result["answer_tokens"]) is not int or not 256 <= result["answer_tokens"] <= 4096:
        raise ContractError("answer_tokens must be a whole number from 256 to 4096")
    if result["conversation_scope"] not in ("chat", "global"):
        raise ContractError("conversation_scope must be one of: chat, global")
    if result["task_chat"] not in ("new", "selected"):
        raise ContractError("task_chat must be one of: new, selected")
    switches = result["voice_tools"]
    if not isinstance(switches, dict) or set(switches) != set(DEFAULTS["voice_tools"]) or any(type(item) is not bool for item in switches.values()):
        raise ContractError("voice_tools must hold one true/false switch for each of: " + ", ".join(DEFAULTS["voice_tools"]))
    if not isinstance(result["summary_writer"], str) or result["summary_writer"] not in ("conversation", "utility"):
        raise ContractError("summary_writer must be one of: conversation, utility")
    if type(result["utility_summary_consent"]) is not bool:
        raise ContractError("utility_summary_consent must be boolean")
    for key, low, high in (("barge_in_seconds", 0.4, 2.0), ("end_of_turn_seconds", 0.5, 2.0), ("noise_gate_db", -66, -3)):
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]) or not low <= result[key] <= high:
            raise ContractError("%s must be a number from %s to %s" % (key, low, high))
    for key in ("show_diagnostic_events", "show_rejected_responses"):
        if type(result[key]) is not bool:
            raise ContractError(key + " must be boolean")
    name = result["voice_name"]
    if not isinstance(name, str) or len(name) > 64 or any(ord(ch) < 32 for ch in name):
        raise ContractError("The voice name must be at most 64 characters on one line")
    profile = result["voice_profile"]
    if not isinstance(profile, str) or len(profile) > 64 or any(not (ch.isascii() and (ch.isalnum() or ch in "_.-")) for ch in profile):
        raise ContractError("The voice profile must be an Agent Zero profile name")
    try:
        from .convo_synthesis import validate_tuning
        result["roles"]["tts"]["tuning"] = validate_tuning(result["roles"]["tts"].get("tuning"))
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
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
    _validate_hotkeys(result["hotkeys"])
    _validate_providers(result)
    return result


def _hotkey(value):
    """(canonical combination, '') for one that may be used, else ('', why not); the words never repeat the value."""
    parts = value.split("+")
    key, modifiers = parts.pop(), set(parts)
    if len(value) > 40 or len(modifiers) != len(parts) or modifiers - set(HOTKEY_MODIFIERS) or not HOTKEY_KEY.fullmatch(key):
        return "", "must be empty or a combination like Alt+M, Ctrl+Shift+Space or F8"
    if not HOTKEY_QUIET.fullmatch(key) and not modifiers - {"Shift"}:
        return "", "needs Ctrl, Alt or Meta (or a function or media key), or it would be taken from typing"
    plain = (modifiers - {"Meta"}) | ({"Ctrl"} if "Meta" in modifiers else set())
    if "+".join([name for name in HOTKEY_MODIFIERS if name in plain] + [key]) in HOTKEY_RESERVED:
        return "", "is used by the browser or by Agent Zero"
    return "+".join([name for name in HOTKEY_MODIFIERS if name in modifiers] + [key]), ""


def _validate_hotkeys(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS["hotkeys"]) or any(not isinstance(item, str) for item in value.values()):
        raise ContractError("hotkeys must hold one combination (or nothing) for each of: " + ", ".join(DEFAULTS["hotkeys"]))
    used = {}
    for action, text in value.items():
        if not text:
            continue
        combination, problem = _hotkey(text)
        if problem:
            raise ContractError("The hotkey for " + action + " " + problem)
        if combination in used:
            raise ContractError("The hotkeys for " + used[combination] + " and " + action + " are the same")
        used[combination] = action


def _validate_providers(result):
    """Provider, decider and drafting settings. Each is additive: empty or default values keep the previous behaviour."""
    if not isinstance(result["voice_message_draft"], str) or result["voice_message_draft"] not in VOICE_MESSAGE_DRAFT:
        raise ContractError("Voice message drafting must be one of: " + ", ".join(VOICE_MESSAGE_DRAFT))
    conversation, classifier, tts = result["roles"]["conversation"], result["roles"]["classifier"], result["roles"]["tts"]
    for role in (conversation, classifier, tts):
        if not isinstance(role.get("key_source", "provider"), str) or role.get("key_source", "provider") not in KEY_SOURCES:
            raise ContractError("Key source must be one of: " + ", ".join(KEY_SOURCES))
    for name, role in (("Conversation", conversation), ("Classifier", classifier)):
        provider = role.get("provider", "")
        if not isinstance(provider, str) or (provider and not PROVIDER_ID.fullmatch(provider)):
            raise ContractError(name + " provider must be an Agent Zero provider id")
        api_base = role.get("api_base", "")
        if not isinstance(api_base, str) or len(api_base) > 2000:
            raise ContractError("Invalid provider field: api_base")
        if api_base:
            endpoint({"url": api_base, "remote": role.get("remote", False)})
    window = classifier.get("context_window", 8192)
    if type(window) is not int or not CONTEXT_WINDOW[0] <= window <= CONTEXT_WINDOW[1]:
        raise ContractError("Classifier context window must be a whole number from 1024 to 1000000")
    if not isinstance(classifier.get("kind", "auto"), str) or classifier.get("kind", "auto") not in CLASSIFIER_KINDS:
        raise ContractError("Classifier type must be one of: " + ", ".join(CLASSIFIER_KINDS))
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
        raise ContractError("Classifier decision settings must be an object")
    if not isinstance(decider.get("source", ""), str) or decider.get("source", "") not in DECIDER_SOURCES:
        raise ContractError("Classifier decision source must be empty, system_one or convo")
    if not isinstance(decider.get("kind"), str) or decider.get("kind") not in DECIDER_KINDS:
        raise ContractError("Classifier decision backend must be one of: " + ", ".join(DECIDER_KINDS))
    for field in ("url", "model"):
        if not isinstance(decider.get(field, ""), str) or len(decider.get(field, "")) > 2000:
            raise ContractError("Invalid classifier decision field: " + field)
    if decider.get("url"):
        decider_endpoint(decider["url"], classifier.get("remote", False))


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
    from .convo_system_one import readiness
    selected = config['system_one']['enabled']
    system_one = readiness(config) if selected else {'ready': False, 'state': 'disabled', 'capabilities': {}}
    return {key: copy.deepcopy(config[key]) for key in (
        "enabled", "ambient_enabled", "remote_ambient_consent", "transcription_enabled",
        "developer_mode", "ai_link_development_mode", "hotkey", "hotkeys", "max_audio_seconds", "echo_guard", "activation_names",
        "voice_message_draft", "barge_in_seconds", "end_of_turn_seconds", "noise_gate_db", "voice_name",
        "show_diagnostic_events", "show_rejected_responses", "conversation_scope", "task_chat") } | {
        "remote_processing": any(r.get("remote", False) for r in config["roles"].values()) or config["sidecar"].get("remote", False) or system_one.get('remote', False),
        "system_one": system_one,
        # Only the conversation model's own audio capability decides this. A classifier that cannot take audio
        # (or an unavailable/text-only decider) never downgrades a direct-audio conversation into transcript input;
        # start and readiness reject the combination explicitly instead (input_modality_conflict).
        "text_input_required": not config['roles']['conversation']['capabilities'].get('audio_input', False),
    }
