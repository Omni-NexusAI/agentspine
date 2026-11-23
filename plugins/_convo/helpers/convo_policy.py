"""Speech permission is independent of permission to invoke tools.

A classifier that returns a typed probability for every attention class (a System One decision backend) is gated on those
probabilities with measured constants (`GateProfile`), not on a fixed cut-off for the single most likely class. A classifier
that returns only a self-reported confidence cannot be calibrated: it keeps the fixed thresholds below, is reported as
unsupported, and is never mistaken for a calibrated one. Neither case falls back to another classifier. Permission to invoke
tools keeps its own, stricter rule (explicit request at 0.9 or more) whatever the speech gate decides."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

ATTENTION = ("addressed", "backchannel", "side_conversation", "unfinished", "unclear")
EPSILON = 1e-9    # threshold checks are inclusive and must not depend on float noise in a sum of decimals

# Defaults of the typed gate. They mirror DEFAULTS["policy_gate"] in convo_contract (a test keeps the two equal) and were
# measured, not borrowed. They belong to ONE wording of the attention question (convo_system_one.QUESTIONS): change either and re-measure both.
# Measured with the sweep scripts under .runtime/convo-live-test and tests/calibrate_gate.py on the d1-omni-600M decision model over 225 labelled
# synthetic sentences in three voices (two OS voices and one neural voice), 675 completed clips (with the endpoint's trailing quiet) and 1350
# probes, chosen on all of them pooled; the held-out check is a separate 114-sentence judgment corpus in two OS voices:
#   respond_min = .30    the least-cost floor when an unanswered request costs two false answers (.30 over .25 on 300 resamples: 187 votes to 84)
#   ambient_respond_min  at most 5% of speech that is not for the assistant answered (4.4% at .90)
#   interrupt_min        at most 5% of speech that is not a request stops the assistant (4.3% at .60)
#   ignore_min = 1.0     no floor waves away non-requests without also waving away 5% of real ones: the rule is off by default
#   name_min = 0.9       not measurable (no floor separates calls by name from other speech); kept at the strictness Convo always had
#   clarify_min = .30    a clarifying question needs the classifier itself to say "might be for the assistant, no clear request" this firmly;
#                        d1-omni-600M almost never does (P(unclear) >= .30 in none of 1,131 readings of the shipped wording and in 4 of 30,795 across
#                        every wording tried, none of them vague speech), so the question is effectively dormant: the restrained outcome, not a defect
#   clarify_elsewhere_max = .50   never ask when this much of the mass is acknowledgement, talk to someone else or an unfinished thought
GATE_DEFAULTS = {"respond_min": .30, "ambient_respond_min": .90, "interrupt_min": .60, "ignore_min": 1.0, "name_min": .90,
                 "clarify_min": .30, "clarify_elsewhere_max": .50}


def _probability(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def _evidence(value):
    """The typed part of a classifier envelope, validated. {} when the envelope carries none, None when it is malformed."""
    attention, probabilities = value.get("attention"), value.get("attention_probabilities")
    if attention is None and probabilities is None:
        return {}
    if attention not in ATTENTION or not isinstance(probabilities, dict) or set(probabilities) != set(ATTENTION):
        return None
    numbers = [probabilities[name] for name in ATTENTION]
    if not all(_probability(number) for number in numbers) or abs(sum(numbers) - 1) > .02 or probabilities[attention] + EPSILON < max(numbers):
        return None
    explicit, name = value.get("explicit_probability", 0.0), value.get("name_probability", 0.0)
    if not _probability(explicit) or not _probability(name):
        return None
    return {"attention": attention, "distribution": tuple((key, float(probabilities[key])) for key in ATTENTION),
            "explicit_probability": float(explicit), "name_probability": float(name)}


@dataclass(frozen=True)
class Decision:
    speech: str = "silent"
    action: str = "none"
    confidence: float = 0.0
    explicit_request: bool = False
    attention: str = ""
    distribution: tuple = ()          # ((class, probability), ...) in ATTENTION order; empty when the classifier gave none
    explicit_probability: float = 0.0
    name_probability: float = 0.0

    @property
    def calibrated(self):
        return bool(self.distribution)

    def probability(self, name):
        return next((value for key, value in self.distribution if key == name), 0.0)

    @property
    def respond_mass(self):
        """The chance the speech deserves a reply: a request, or something that may be one."""
        return self.probability("addressed") + self.probability("unclear")

    @property
    def elsewhere_mass(self):
        """The chance it needs none: an acknowledgement, a conversation with someone else, or a thought still being formed."""
        return self.probability("backchannel") + self.probability("side_conversation") + self.probability("unfinished")

    @classmethod
    def parse(cls, value, *, typed=False):
        """`typed` is true only for an envelope built by Convo's own typed consumer from validated answers. A JSON classifier can
        say anything about itself, so a distribution it reports is never believed."""
        try:
            speech, action = value["speech"], value["action"]
            if isinstance(value['confidence'], bool) or not isinstance(value['confidence'], (int, float)):
                return cls()
            confidence = float(value["confidence"])
            if speech not in {"speak", "silent", "clarify"} or action not in {"none", "prepare", "direct", "delegate"} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                return cls()
            explicit = value.get("explicit_request") is True
            evidence = _evidence(value) if typed else {}
            if evidence is None:
                return cls()
            return cls(speech, action, confidence, explicit, **evidence)
        except (KeyError, TypeError, ValueError, AttributeError):
            return cls()


@dataclass(frozen=True)
class GateProfile:
    """Where the typed gate draws its lines. Every value is the probability, from 0 to 1, at which the gate acts."""
    respond_min: float = GATE_DEFAULTS["respond_min"]                  # a completed utterance is answered at this chance of being for the assistant
    ambient_respond_min: float = GATE_DEFAULTS["ambient_respond_min"]  # the same, when only listening in the background
    interrupt_min: float = GATE_DEFAULTS["interrupt_min"]              # speech still going on stops the assistant at this chance of being a request to it
    ignore_min: float = GATE_DEFAULTS["ignore_min"]                    # speech still going on is ignored at this chance of needing no reply
    name_min: float = GATE_DEFAULTS["name_min"]                        # the assistant counts as called by name at this chance
    clarify_min: float = GATE_DEFAULTS["clarify_min"]                  # a clarifying question needs at least this chance of "unclear" (and more than "addressed")
    clarify_elsewhere_max: float = GATE_DEFAULTS["clarify_elsewhere_max"]  # ...and less than this chance of acknowledgement, talk to others or an unfinished thought

    @classmethod
    def from_settings(cls, value=None):
        value = {} if value is None else value
        if not isinstance(value, dict):
            raise ValueError("The speech gate settings must be an object")
        numbers = {}
        for key, default in GATE_DEFAULTS.items():
            item = value.get(key, default)
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or not 0 < item <= 1:
                raise ValueError("Speech gate " + key + " must be a probability above 0 and at most 1")
            numbers[key] = float(item)
        if numbers["ambient_respond_min"] < numbers["respond_min"]:
            raise ValueError("Ambient listening cannot answer at a lower chance than active listening; raise ambient_respond_min or lower respond_min")
        if numbers["ignore_min"] < .5:
            raise ValueError("Speech is only ignored while still going on when it is more likely than not to need no reply (ignore_min of at least 0.5)")
        return cls(**numbers)


class PolicyGate:
    def __init__(self, cooldown=60, clock=time.monotonic, profile=None):
        self.cooldown = max(60, cooldown)
        self.clock = clock
        self.profile = profile or GateProfile()
        self.last_clarification = -float("inf")
        self.clarified_episode = None

    def evaluate(self, decision, *, epoch, current_epoch, episode, fresh=True, mode="active"):
        if epoch != current_epoch or not fresh:
            return Decision()
        action = decision.action
        if action == "prepare" and decision.confidence < .85:
            action = "none"
        if action in {"direct", "delegate"} and not (decision.explicit_request and decision.confidence >= .9):
            action = "none"
        if decision.calibrated:
            return self._typed(decision, action, episode, mode)
        if decision.speech == "clarify":
            action = "none"
            if decision.confidence < .7 or self.clarified_episode == episode or self.clock() - self.last_clarification < self.cooldown:
                return Decision("silent", "none", decision.confidence)
            self.last_clarification = self.clock()
            self.clarified_episode = episode
        elif decision.speech == "speak" and decision.confidence < .85:
            return Decision("silent", "none", decision.confidence)
        return Decision(decision.speech, action, decision.confidence, decision.explicit_request)

    def _typed(self, decision, action, episode, mode):
        """A completed utterance, from the classifier's probabilities: answer, ask once, or stay quiet."""
        floor = self.profile.respond_min if mode == "active" else self.profile.ambient_respond_min
        if decision.respond_mass + EPSILON < floor:
            return replace(decision, speech="silent", action=action)
        if decision.probability("addressed") + EPSILON >= decision.probability("unclear"):
            return replace(decision, speech="speak", action=action)
        # "Might be for the assistant" is not a licence to ask: ask only when the classifier itself says unclear, firmly, and the rest of
        # its mass is not clearly an acknowledgement, background or someone else (measured: without this, the old rule asked about
        # acknowledgements, side remarks and unfinished thoughts). One question per ambiguity episode, then a cooldown.
        if decision.probability("unclear") + EPSILON < self.profile.clarify_min or decision.elsewhere_mass >= self.profile.clarify_elsewhere_max:
            return replace(decision, speech="silent", action="none")
        if self.clarified_episode == episode or self.clock() - self.last_clarification < self.cooldown:
            return replace(decision, speech="silent", action="none")
        self.last_clarification = self.clock()
        self.clarified_episode = episode
        return replace(decision, speech="clarify", action="none")


# A conversation the user started (active listening) presumes that what they say is meant for the assistant. Convo has no turn-taking
# model fine-tuned for this (Speakrail and the Codacus harness each train one), so the general decision model is asked what it can
# judge and the presumption decides the rest: a finished turn is answered unless the classifier is confident it was not for the
# assistant, either a short acknowledgement or talk to someone else that no check during the speech contradicted. An unfinished
# thought is answered too, a moment later (UNFINISHED_WAIT), and joined if more follows first. Starting points, re-measured per
# decision model on real voices like the gate floors above.
CONVERSATION = {"backchannel_min": .60, "backchannel_seconds": 1.2, "ack_respond_max": .25, "side_min": .90, "probe_respond_min": .50}
UNFINISHED_WAIT = 1.5      # seconds after an unfinished thought ends before its answer is heard, for the rest of it to arrive


# Speech over the assistant's reply, once the browser's echo guard has confirmed it is the user's own voice (the reply is already
# ducked): read without the assistant's last words (that line makes nearly anything read as an acknowledgement, measured 2026-11-08),
# a request, a question or "stop" yields; a confident acknowledgement or talk to someone else lets the reply go on; anything else
# keeps it ducked and asks again. A safety yield ends the reply after `barge_in_seconds` of confirmed voice whatever the reading
# (twice that after a confident "go on"), so a classifier that cannot tell never keeps talking over the user. Starting points from
# the corpus's plain readings of finished clips; not yet measured on real overlapped speech.
OVERLAP = {"yield_min": .50, "continue_min": .70, "continue_respond_max": .25}


def overlap_disposition(decision, *, voiced=None, safety=1.0, continuing=False):
    """('admit' | 'continue' | 'listen', reason) for speech over the assistant's reply. `voiced`: seconds of the user's confirmed voice
    so far; `continuing`: the previous reading of this speech was a confident 'continue'."""
    if decision.calibrated:
        respond = decision.respond_mass
        elsewhere = decision.probability("backchannel") + decision.probability("side_conversation")
        if respond + EPSILON >= OVERLAP["yield_min"]:
            verdict = "admit"
        elif elsewhere + EPSILON >= OVERLAP["continue_min"] and respond < OVERLAP["continue_respond_max"]:
            verdict = "continue"
        else:
            verdict = "listen"
    elif decision.speech in ("speak", "clarify") and decision.confidence >= .85:
        verdict = "admit"
    elif decision.speech == "silent" and decision.confidence >= .85:
        verdict = "continue"
    else:
        verdict = "listen"
    if verdict != "admit" and voiced is not None and voiced + EPSILON >= safety * (2 if verdict == "continue" or continuing else 1):
        return "admit", "safety"
    return verdict, "request" if verdict == "admit" else ""


def respond_evidence(decision):
    """How strongly one reading says the speech is for the assistant (0 to 1), for weighing the checks made while it went on."""
    if decision.calibrated:
        return decision.respond_mass
    return decision.confidence if decision.speech in ("speak", "clarify") else 0.0


def conversation_verdict(decision, *, seconds=None, peak=0.0, ack_respond=None):
    """Active listening, a finished turn, before the gate: ('admit', '') or ('ignore', why). `seconds` is how long the speech itself
    lasted (without the endpoint's trailing quiet) when known; `peak` the strongest respond_evidence of the checks during it;
    `ack_respond` the chance a short turn that looked like an acknowledgement is for the assistant, read without the assistant's last
    words (convo_system_one._acknowledgement; absent when it did not look like one)."""
    if decision.calibrated:
        if (seconds is not None and seconds <= CONVERSATION["backchannel_seconds"]
                and ack_respond is not None and ack_respond + EPSILON < CONVERSATION["ack_respond_max"]):
            return "ignore", "backchannel"
        if (decision.probability("side_conversation") + EPSILON >= CONVERSATION["side_min"]
                and peak + EPSILON < CONVERSATION["probe_respond_min"]):
            return "ignore", "for_someone_else"
        return "admit", ""
    # A classifier without probabilities: only its own confident "not for the assistant" leaves a turn unanswered.
    if decision.speech == "silent" and decision.action == "none" and decision.confidence >= .9:
        return "ignore", "not_addressed"
    return "admit", ""


def presumed(decision):
    """The gate's decision for a turn the conversation presumes is for the assistant: spoken to (a question stays a question)."""
    return decision if decision.speech in ("speak", "clarify") else replace(decision, speech="speak")


def reads_as_request(decision, *, peak=0.0):
    """Whether the classifier itself heard a request in a finished turn: the gate passed it on its own (`decision` is the gate's,
    before the conversation's presumption), work was asked for explicitly, or a check made while the user spoke read it as for the
    assistant. Only such a turn takes over an answer the user has not heard yet; one answered only because the conversation presumes
    speech is for the assistant (an acknowledgement, a remark, a misread request, or a turn no classifier could judge) waits for that
    answer and is answered after it instead. Waiting costs a misread request a delay, never the request; taking over cost the user
    the answer they were waiting for (2026-11-17: "Perfect, thank you", read .001 for the assistant, cancelled a reply as it began)."""
    if decision.speech in ("speak", "clarify") or (decision.action in {"direct", "delegate"} and decision.explicit_request):
        return True
    return peak + EPSILON >= CONVERSATION["probe_respond_min"]


def candidate_disposition(decision, *, final, profile=None):
    """Observation is not a turn. Partial speech needs explicit interruption evidence.

    A completed utterance must already have been through `PolicyGate.evaluate`, which decides whether it is spoken to."""
    if decision.calibrated:
        return _typed_disposition(decision, final=final, profile=profile or GateProfile())
    if decision.speech == 'silent' and decision.confidence >= .85 and decision.action == 'none':
        return 'ignore'
    if decision.speech == 'speak' and decision.confidence >= .85:
        if final or (decision.explicit_request and decision.confidence >= .9):
            return 'admit'
    if final and decision.speech == 'clarify' and decision.confidence >= .7:
        return 'admit'  # Caller must apply PolicyGate cooldown first; never tools.
    if final and decision.action in {'direct', 'delegate'} and decision.explicit_request and decision.confidence >= .9:
        return 'admit'
    return 'withhold'


def _typed_disposition(decision, *, final, profile):
    if final:
        if decision.action in {'direct', 'delegate'} and decision.explicit_request and decision.confidence >= .9:
            return 'admit'
        return 'admit' if decision.speech in {'speak', 'clarify'} else 'ignore'
    # Still speaking: only a request to the assistant may stop it, and only clearly; only clear talk to someone else is waved away.
    if decision.probability('addressed') * decision.explicit_probability + EPSILON >= profile.interrupt_min:
        return 'admit'
    if decision.elsewhere_mass + EPSILON >= profile.ignore_min:
        return 'ignore'
    return 'withhold'


def addressed_by_name(decision, result, profile=None):
    """Whether name-mode listening may answer. A typed classifier gives a probability, judged by the measured floor."""
    if decision.calibrated:
        return decision.name_probability + EPSILON >= (profile or GateProfile()).name_min
    return result.get("addressed_by_name") is True


def gate_report(profile, *, typed):
    """What the speech gate is doing, in words, for the readiness list: calibrated, or explicitly unsupported."""
    if typed:
        percent = lambda value: str(round(value * 100)) + '%'
        return {"state": "ready", "detail": (
            "Calibrated on the classifier's probabilities: a completed utterance is answered when the chance it is for the assistant reaches "
            + percent(profile.respond_min) + " (active listening) or " + percent(profile.ambient_respond_min) + " (ambient listening). Speech still going on "
            "stops the assistant at a " + percent(profile.interrupt_min) + " chance of being a request to it, and is waved away at a " + percent(profile.ignore_min)
            + " chance of needing no reply. The defaults were measured for one decision model on synthetic speech; re-measure them for any other model or for real voices.")}
    return {"state": "unsupported", "detail": (
        "This classifier returns a self-reported confidence, not a probability for each kind of speech, so its gate cannot be calibrated. "
        "Convo keeps the fixed thresholds (answer at 0.85 confidence, tool use at 0.9) and does not switch to another classifier. "
        "A System One decision backend returns the probabilities a calibrated gate needs.")}


EXPLICIT_MIN = .9      # the chance, by the classifier's reading of the user's audio, that they made a request


def verified(decision):
    """Whether the user's own audio carries a request, as the classifier heard it: the one piece of evidence a model's tool call
    cannot supply for itself. A typed classifier gives that probability directly, and it is read on its own. Folding in "was it
    said to the assistant" (the reading that is weakest right after the assistant has spoken) refused a plainly spoken request
    whose explicit reading was 0.99 (2026-11-18). An untyped classifier is believed only when explicit and confident; a turn
    nobody judged carries no evidence."""
    if decision.calibrated:
        return decision.explicit_probability + EPSILON >= EXPLICIT_MIN
    return bool(decision.explicit_request and decision.confidence >= .9)


DIRECT_TOOLS = frozenset({"web_search", "camera_context", "memory_read", "recall_conversation"})


def route_tool(name, decision):
    if not decision.explicit_request or decision.confidence < .9 or decision.action not in {"direct", "delegate"}:
        return "deny"
    return "direct" if name in DIRECT_TOOLS and decision.action == "direct" else "delegate"
