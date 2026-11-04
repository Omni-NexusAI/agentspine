"""The speech gate: what Convo does with a typed probability distribution from the classifier, and what it says when there is none.

A typed classifier (a System One decision backend) returns a probability for every attention class. The gate answers from those
probabilities with measured constants instead of a fixed confidence cut-off on the single most likely class. A classifier that
returns only a self-reported confidence cannot be calibrated: it keeps the fixed thresholds and says so, it is never mistaken
for a calibrated one and nothing falls back to another classifier. Tool use keeps its own, stricter rule either way."""
import math
import unittest

from plugins._convo.helpers import convo_policy as policy
from plugins._convo.helpers.convo_contract import DEFAULTS, ContractError, validate_settings
from plugins._convo.helpers.convo_policy import Decision, GateProfile, PolicyGate, candidate_disposition, route_tool

CLASSES = ('addressed', 'backchannel', 'side_conversation', 'unfinished', 'unclear')
PROFILE = GateProfile(respond_min=.30, ambient_respond_min=.70, interrupt_min=.50, ignore_min=.80, name_min=.50)


def spread(**given):
    """A full five-class distribution: the named classes keep their mass, the rest of it is shared by the other classes."""
    others = [c for c in CLASSES if c not in given]
    left = 1 - sum(given.values())
    return {**{c: left / len(others) for c in others}, **given}


def envelope(explicit=.0, name=.0, action='none', confidence=.9, attention=None, **given):
    """What the System One consumer hands to Convo: the legacy fields plus the attention class and its distribution."""
    distribution = spread(**given)
    return {'speech': 'silent', 'action': action, 'confidence': confidence, 'explicit_request': explicit >= .9, 'addressed_by_name': name >= .9,
            'context_note': '', 'attention': attention or max(distribution, key=distribution.get), 'attention_probabilities': distribution,
            'explicit_probability': explicit, 'name_probability': name}


def typed(**kwargs):
    return Decision.parse(envelope(**kwargs), typed=True)


def final(decision, *, mode='active', gate=None, episode=1):
    """A completed utterance through the same two steps the live loop uses: the gate's decision, then the disposition."""
    gate = gate or PolicyGate(profile=PROFILE)
    decided = gate.evaluate(decision, epoch=0, current_epoch=0, episode=episode, mode=mode)
    return decided.speech, candidate_disposition(decided, final=True, profile=PROFILE)


def partial(decision):
    return candidate_disposition(decision, final=False, profile=PROFILE)


class Parsing(unittest.TestCase):
    def test_a_typed_envelope_carries_the_attention_class_and_every_probability(self):
        decision = typed(explicit=.8, name=.2, addressed=.6, unfinished=.2)
        self.assertEqual(decision.attention, 'addressed')
        self.assertTrue(decision.calibrated)
        self.assertEqual(set(dict(decision.distribution)), set(CLASSES))
        self.assertAlmostEqual(decision.probability('addressed'), .6)
        self.assertAlmostEqual(sum(dict(decision.distribution).values()), 1)
        self.assertEqual((decision.explicit_probability, decision.name_probability), (.8, .2))
        self.assertAlmostEqual(decision.respond_mass + decision.elsewhere_mass, 1)

    def test_an_untyped_classifier_cannot_switch_the_gate_into_calibrated_mode(self):
        decision = Decision.parse(envelope(addressed=.9))             # a JSON classifier says whatever it likes about itself
        self.assertFalse(decision.calibrated)
        self.assertEqual((decision.attention, decision.distribution, decision.explicit_probability), ('', (), 0.0))
        self.assertEqual((decision.speech, decision.action), ('silent', 'none'))

    def test_a_legacy_decision_has_no_distribution(self):
        decision = Decision.parse({'speech': 'speak', 'action': 'none', 'confidence': .9}, typed=True)
        self.assertFalse(decision.calibrated)                           # nothing to calibrate, so nothing is pretended
        self.assertEqual(decision.speech, 'speak')

    def test_a_malformed_distribution_fails_closed(self):
        good = envelope(addressed=.6)
        bad = {
            'a class is missing': {**good, 'attention_probabilities': {k: v for k, v in good['attention_probabilities'].items() if k != 'unclear'}},
            'an unknown class': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'shouting': 0.0}},
            'not a number': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': 'high'}},
            'NaN': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': math.nan}},
            'infinite': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': math.inf}},
            'a boolean': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': True}},
            'negative': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': -.1}},
            'above one': {**good, 'attention_probabilities': {**good['attention_probabilities'], 'unclear': 1.2}},
            'does not sum to one': {**good, 'attention_probabilities': {k: v * .8 for k, v in good['attention_probabilities'].items()}},
            'not an object': {**good, 'attention_probabilities': [.2] * 5},
            'class is not the most likely': {**good, 'attention': 'backchannel'},
            'unknown class': {**good, 'attention': 'shouting'},
            'class without a distribution': {k: v for k, v in good.items() if k != 'attention_probabilities'},
            'distribution without a class': {k: v for k, v in good.items() if k != 'attention'},
            'explicit above one': {**good, 'explicit_probability': 1.5},
            'explicit not a number': {**good, 'explicit_probability': 'yes'},
            'name negative': {**good, 'name_probability': -1},
        }
        for why, value in bad.items():
            with self.subTest(why):
                self.assertEqual(Decision.parse(value, typed=True), Decision())

    def test_missing_explicit_and_name_probabilities_count_as_zero(self):
        value = {k: v for k, v in envelope(addressed=.6).items() if k not in {'explicit_probability', 'name_probability'}}
        decision = Decision.parse(value, typed=True)
        self.assertEqual((decision.explicit_probability, decision.name_probability), (0.0, 0.0))
        self.assertTrue(decision.calibrated)


class FinalSpeech(unittest.TestCase):
    def test_a_spread_distribution_is_answered_where_the_fixed_cutoff_would_have_stayed_silent(self):
        """Most likely class 'addressed' at only 0.35 (the old rule needed 0.85): 45% of the mass is for the assistant."""
        decision = typed(addressed=.35, side_conversation=.25, backchannel=.15, unfinished=.15, unclear=.10)
        self.assertEqual(final(decision), ('speak', 'admit'))

    def test_the_floor_is_inclusive_and_below_it_a_completed_utterance_is_ignored(self):
        self.assertEqual(final(typed(addressed=.22, unclear=.05, side_conversation=.4, backchannel=.2, unfinished=.13)), ('silent', 'ignore'))     # .27 for the assistant
        self.assertEqual(final(typed(addressed=.30, unclear=0.0, side_conversation=.4, backchannel=.2, unfinished=.1)), ('speak', 'admit'))     # exactly the floor

    def test_clear_cases(self):
        cases = {
            'a request': (typed(addressed=.9), ('speak', 'admit')),
            'a backchannel': (typed(backchannel=.9), ('silent', 'ignore')),
            'a side conversation': (typed(side_conversation=.9), ('silent', 'ignore')),
            'an unfinished sentence': (typed(unfinished=.9), ('silent', 'ignore')),
            'unclear but probably for the assistant': (typed(unclear=.6, addressed=.1), ('clarify', 'admit')),
        }
        for why, (decision, expected) in cases.items():
            with self.subTest(why):
                self.assertEqual(final(decision), expected)

    def test_a_clarifying_question_is_asked_once_per_episode_and_not_again_inside_the_cooldown(self):
        gate = PolicyGate(clock=lambda: 1000.0, profile=PROFILE)
        unclear = typed(unclear=.6, addressed=.1)
        self.assertEqual(final(unclear, gate=gate, episode=1), ('clarify', 'admit'))
        self.assertEqual(final(unclear, gate=gate, episode=1), ('silent', 'ignore'))
        self.assertEqual(final(unclear, gate=gate, episode=2), ('silent', 'ignore'))

    def test_ambient_listening_needs_much_more_than_active_listening(self):
        middling = typed(addressed=.45, side_conversation=.3, backchannel=.1, unfinished=.1, unclear=.05)
        self.assertEqual(final(middling, mode='active'), ('speak', 'admit'))
        self.assertEqual(final(middling, mode='ambient'), ('silent', 'ignore'))
        self.assertEqual(final(typed(addressed=.75), mode='ambient'), ('speak', 'admit'))

    def test_a_stale_epoch_still_wins_over_any_distribution(self):
        gate = PolicyGate(profile=PROFILE)
        self.assertEqual(gate.evaluate(typed(addressed=.99), epoch=0, current_epoch=1, episode=1), Decision())
        self.assertEqual(gate.evaluate(typed(addressed=.99), epoch=0, current_epoch=0, episode=1, fresh=False), Decision())

    def test_the_gate_keeps_the_classifier_evidence_it_decided_from(self):
        decided = PolicyGate(profile=PROFILE).evaluate(typed(addressed=.9, explicit=.95), epoch=0, current_epoch=0, episode=1)
        self.assertEqual((decided.attention, decided.calibrated, decided.explicit_probability), ('addressed', True, .95))


class Clarification(unittest.TestCase):
    """A clarifying question is rare, in character and rationed: only for speech the classifier itself calls "might be for the assistant,
    no clear request", never for what is clearly an acknowledgement, background or someone else, once per ambiguity, then a cooldown.
    Measured (judgment corpus, d1-omni-600M): the previous rule asked about acknowledgements, side remarks and unfinished thoughts."""

    UNCLEAR = dict(unclear=.6, addressed=.1)                                # the rest is spread: .1 each over three classes

    def clock(self):
        self.now = getattr(self, 'now', 1000.0)
        return lambda: self.now

    def gate(self, **profile):
        return PolicyGate(clock=self.clock(), profile=GateProfile.from_settings({**PROFILE.__dict__, **profile}))

    def test_firm_unclear_speech_is_asked_about(self):
        self.assertEqual(final(typed(**self.UNCLEAR), gate=self.gate())[0], 'clarify')

    def test_an_acknowledgement_is_never_asked_about_even_when_unclear_leads_the_rest(self):
        # unclear is the most likely addressed-ish class, but the mass is mostly an acknowledgement: stay quiet
        decision = typed(backchannel=.45, unclear=.35, addressed=.1)
        self.assertEqual(final(decision, gate=self.gate())[0], 'silent')

    def test_background_or_someone_else_is_never_asked_about(self):
        for why, decision in {'side talk': typed(side_conversation=.45, unclear=.35, addressed=.1),
                              'unfinished thought': typed(unfinished=.5, unclear=.3, addressed=.1)}.items():
            with self.subTest(why):
                self.assertEqual(final(decision, gate=self.gate())[0], 'silent')

    def test_unclear_below_the_floor_for_asking_is_silence_not_a_question(self):
        weak = typed(unclear=.25, addressed=.2, side_conversation=.15, backchannel=.2, unfinished=.2)     # would be asked about at clarify_min .2
        self.assertEqual(final(weak, gate=self.gate(clarify_min=.2, clarify_elsewhere_max=1.0))[0], 'clarify')
        self.assertEqual(final(weak, gate=self.gate(clarify_min=.3, clarify_elsewhere_max=1.0))[0], 'silent')

    def test_the_unclear_floor_for_asking_is_inclusive(self):
        edge = typed(unclear=.3, addressed=.1)                                  # exactly clarify_min; the rest is spread (.2 each)
        self.assertEqual(final(edge, gate=self.gate(clarify_min=.3, clarify_elsewhere_max=1.0))[0], 'clarify')
        self.assertEqual(final(edge, gate=self.gate(clarify_min=.35, clarify_elsewhere_max=1.0))[0], 'silent')

    def test_the_elsewhere_limit_is_inclusive_of_the_limit_itself(self):
        edge = typed(unclear=.4, addressed=.1, backchannel=.2, side_conversation=.2, unfinished=.1)          # exactly .5 elsewhere
        self.assertEqual(final(edge, gate=self.gate(clarify_elsewhere_max=.5))[0], 'silent')
        self.assertEqual(final(edge, gate=self.gate(clarify_elsewhere_max=.55))[0], 'clarify')

    def test_asked_once_per_ambiguity_even_after_the_cooldown(self):
        gate = self.gate()
        unclear = typed(**self.UNCLEAR)
        self.assertEqual(final(unclear, gate=gate, episode=1)[0], 'clarify')
        self.now += 3600                                                    # long after the cooldown
        self.assertEqual(final(unclear, gate=gate, episode=1)[0], 'silent')      # the same fragment is not asked about again

    def test_a_new_ambiguity_waits_for_the_cooldown(self):
        gate = self.gate()
        unclear = typed(**self.UNCLEAR)
        self.assertEqual(final(unclear, gate=gate, episode=1)[0], 'clarify')
        self.now += 59
        self.assertEqual(final(unclear, gate=gate, episode=2)[0], 'silent')
        self.now += 1                                                       # the default is 60 s; the gate never goes below that
        self.assertEqual(final(unclear, gate=gate, episode=2)[0], 'clarify')

    def test_a_silenced_question_does_not_start_the_cooldown(self):
        gate = self.gate()
        self.assertEqual(final(typed(backchannel=.45, unclear=.35, addressed=.1), gate=gate)[0], 'silent')
        self.assertEqual(final(typed(**self.UNCLEAR), gate=gate)[0], 'clarify')         # nothing was asked, so nothing is cooling down

    def test_a_clear_request_is_answered_whatever_the_question_rules_say(self):
        gate = self.gate()
        self.assertEqual(final(typed(**self.UNCLEAR), gate=gate)[0], 'clarify')
        self.assertEqual(final(typed(addressed=.9), gate=gate, episode=2)[0], 'speak')   # asking never rations answers


class Partials(unittest.TestCase):
    """Speech that is still going on can only stop the assistant when it is clearly a request to the assistant."""

    def test_an_explicit_request_to_the_assistant_interrupts(self):
        self.assertEqual(partial(typed(addressed=.9, explicit=.95)), 'admit')

    def test_the_interrupt_score_is_addressed_times_explicit(self):
        self.assertEqual(partial(typed(addressed=.9, explicit=.3)), 'withhold')               # .27
        self.assertEqual(partial(typed(addressed=.55, explicit=.8)), 'withhold')              # .44
        self.assertEqual(partial(typed(addressed=.8, explicit=.65)), 'admit')                 # .52

    def test_speech_for_someone_else_is_ignored_only_when_the_classifier_is_sure(self):
        self.assertEqual(partial(typed(backchannel=.9)), 'ignore')
        self.assertEqual(partial(typed(side_conversation=.5, backchannel=.2, unfinished=.1, addressed=.1, unclear=.1)), 'ignore')        # .8 elsewhere: inclusive
        self.assertEqual(partial(typed(side_conversation=.5, backchannel=.2, unfinished=.05, addressed=.125, unclear=.125)), 'withhold')  # .75
        self.assertEqual(partial(typed(unfinished=.85)), 'ignore')

    def test_unclear_speech_never_interrupts(self):
        self.assertEqual(partial(typed(unclear=.9, explicit=.99)), 'withhold')

    def test_an_addressed_statement_that_is_not_a_request_does_not_interrupt(self):
        self.assertEqual(partial(typed(addressed=.9, explicit=.0)), 'withhold')


class NameMode(unittest.TestCase):
    def test_a_typed_name_probability_is_judged_by_the_measured_floor(self):
        result = envelope(addressed=.9, name=.6)
        self.assertTrue(policy.addressed_by_name(Decision.parse(result, typed=True), result, PROFILE))
        result = envelope(addressed=.9, name=.4)
        self.assertFalse(policy.addressed_by_name(Decision.parse(result, typed=True), result, PROFILE))

    def test_an_untyped_classifier_is_judged_by_its_own_flag(self):
        for flag in (True, False):
            result = {'speech': 'speak', 'action': 'none', 'confidence': .9, 'addressed_by_name': flag}
            self.assertIs(policy.addressed_by_name(Decision.parse(result), result, PROFILE), flag)
        result = {'speech': 'speak', 'action': 'none', 'confidence': .9, 'addressed_by_name': 'yes'}
        self.assertFalse(policy.addressed_by_name(Decision.parse(result), result, PROFILE))


class Actions(unittest.TestCase):
    """Whatever the speech gate decides, work with side effects keeps the strict rule: explicit request at 0.9 or more."""

    def test_speaking_does_not_authorize_work(self):
        decided = PolicyGate(profile=PROFILE).evaluate(typed(addressed=.95, action='delegate', explicit=.5, confidence=.95), epoch=0, current_epoch=0, episode=1)
        self.assertEqual((decided.speech, decided.action), ('speak', 'none'))
        self.assertEqual(route_tool('code', decided), 'deny')

    def test_an_explicit_high_confidence_request_keeps_its_action(self):
        decided = PolicyGate(profile=PROFILE).evaluate(typed(addressed=.95, action='delegate', explicit=.95, confidence=.95), epoch=0, current_epoch=0, episode=1)
        self.assertEqual((decided.speech, decided.action), ('speak', 'delegate'))

    def test_a_confident_action_below_the_strict_floor_is_dropped(self):
        decided = PolicyGate(profile=PROFILE).evaluate(typed(addressed=.95, action='direct', explicit=.95, confidence=.85), epoch=0, current_epoch=0, episode=1)
        self.assertEqual(decided.action, 'none')

    def test_a_clarifying_turn_never_carries_work(self):
        decided = PolicyGate(profile=PROFILE).evaluate(typed(unclear=.6, addressed=.1, action='delegate', explicit=.95, confidence=.95), epoch=0, current_epoch=0, episode=1)
        self.assertEqual((decided.speech, decided.action), ('clarify', 'none'))

    def test_an_explicit_work_request_is_admitted_even_when_the_utterance_would_not_be_spoken_to(self):
        decision = typed(addressed=.5, action='direct', explicit=.95, confidence=.95)           # .625 for the assistant: under the ambient floor
        self.assertEqual(final(decision, mode='ambient'), ('silent', 'admit'))


class FixedThresholds(unittest.TestCase):
    """A classifier without a distribution keeps today's behaviour exactly, so nothing changes for it silently."""

    def test_dispositions_are_unchanged(self):
        cases = [
            (Decision('silent', 'none', .85), False, 'ignore'), (Decision('silent', 'none', .84), False, 'withhold'),
            (Decision('speak', 'none', .85), True, 'admit'), (Decision('speak', 'none', .84), True, 'withhold'),
            (Decision('speak', 'none', .95), False, 'withhold'), (Decision('speak', 'none', .9, True), False, 'admit'),
            (Decision('clarify', 'none', .7), True, 'admit'), (Decision('clarify', 'none', .69), True, 'withhold'),
            (Decision('clarify', 'none', .9), False, 'withhold'),
            (Decision('silent', 'direct', .95, True), True, 'admit'), (Decision('silent', 'direct', .95, False), True, 'withhold'),
        ]
        for decision, is_final, expected in cases:
            with self.subTest(decision=decision, final=is_final):
                self.assertEqual(candidate_disposition(decision, final=is_final), expected)
                self.assertEqual(candidate_disposition(decision, final=is_final, profile=PROFILE), expected)

    def test_the_gate_still_answers_at_the_fixed_cutoff_only(self):
        gate = PolicyGate(profile=PROFILE)
        self.assertEqual(gate.evaluate(Decision('speak', 'none', .84), epoch=0, current_epoch=0, episode=1).speech, 'silent')
        self.assertEqual(gate.evaluate(Decision('speak', 'none', .85), epoch=0, current_epoch=0, episode=1).speech, 'speak')


class Profile(unittest.TestCase):
    def test_defaults_are_the_documented_defaults_and_are_valid(self):
        self.assertEqual(GateProfile.from_settings(None), GateProfile.from_settings({}))
        self.assertEqual(GateProfile.from_settings(DEFAULTS['policy_gate']), GateProfile.from_settings(None))
        self.assertEqual(set(DEFAULTS['policy_gate']), {'respond_min', 'ambient_respond_min', 'interrupt_min', 'ignore_min', 'name_min', 'clarify_min', 'clarify_elsewhere_max'})

    def test_missing_values_take_the_defaults_and_unknown_keys_are_ignored(self):
        profile = GateProfile.from_settings({'respond_min': .4, 'future': 1})
        self.assertEqual(profile.respond_min, .4)
        self.assertEqual(profile.ignore_min, GateProfile.from_settings(None).ignore_min)

    def test_invalid_values_are_refused(self):
        bad = [{'respond_min': 0}, {'respond_min': 1.1}, {'respond_min': True}, {'respond_min': 'low'}, {'respond_min': math.nan},
               {'respond_min': .6, 'ambient_respond_min': .4},          # ambient listening can never be looser than active
               {'ignore_min': .4}, {'interrupt_min': 0}, {'name_min': 1.5}, {'clarify_min': 0}, {'clarify_min': 1.2}, {'clarify_elsewhere_max': 0}, {'clarify_elsewhere_max': True}]
        for value in bad:
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    GateProfile.from_settings(value)

    def test_the_settings_contract_validates_the_gate(self):
        self.assertEqual(validate_settings({})['policy_gate'], DEFAULTS['policy_gate'])
        self.assertEqual(validate_settings({'policy_gate': {'respond_min': .4}})['policy_gate']['respond_min'], .4)
        with self.assertRaises(ContractError):
            validate_settings({'policy_gate': {'respond_min': 2}})
        with self.assertRaises(ContractError):
            validate_settings({'policy_gate': 'strict'})


class Reporting(unittest.TestCase):
    def test_a_typed_classifier_reports_a_calibrated_gate_with_its_constants(self):
        report = policy.gate_report(PROFILE, typed=True)
        self.assertEqual(report['state'], 'ready')
        self.assertIn('30%', report['detail'])
        self.assertIn('70%', report['detail'])

    def test_a_classifier_without_distributions_is_reported_as_unsupported_not_as_calibrated(self):
        report = policy.gate_report(PROFILE, typed=False)
        self.assertEqual(report['state'], 'unsupported')
        self.assertIn('0.85', report['detail'])
        self.assertNotIn('30%', report['detail'])
        self.assertIn('probabilit', report['detail'])


if __name__ == '__main__':
    unittest.main()
