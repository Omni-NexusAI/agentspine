"""Measure the typed speech gate on labelled classifier output and recommend its constants.

Not a unit test: a repeatable measurement for whoever changes the decision model, the question wording or the voices. It reads a
JSON-lines file with one record per labelled clip,

    {"id": "A01", "split": "cal" | "test", "truth": {"attention": ..., "explicit_request": "yes"|"no", "addressed_by_name": "yes"|"no"},
     "attention": {class: probability, ...}, "explicit": {"yes": p, "no": p}, "name": {"yes": p, "no": p}}

(the classifier's own typed answers; any model that answers Convo's policy questions can dump them), and uses the product's own gate
code (convo_policy.PolicyGate / candidate_disposition) to count what Convo would have done. Thresholds are chosen on the `cal` split
only; the `test` split is evaluated, once, with --show-test, so it stays an honest check. What it measures depends entirely on what
the clips are: synthetic speech says how a model reads clear, isolated sentences, not how it behaves on real voices, rooms or noise.

Run from the repository root:
    python -m plugins._convo.tests.calibrate_gate runs/M5S4.jsonl [--probes runs/M5S4_prefix1.jsonl] [--show-test] [--json]

Targets (all on the calibration split, all overridable):
    --cost-ratio 2             active listening: the floor with the least cost when an unanswered request costs 2 false answers
    --miss-target 0.10         (instead) answer at least 90% of the speech that is for the assistant
    --false-target 0.05        answer at most 5% of the speech that is not (ambient listening) and take at most 5% for a call by name
    --interrupt-target 0.05    stop the assistant for at most 5% of the speech that is not a request to it
    --ignore-target 0.05       wave away at most 5% of the speech that is for the assistant (while it is still going on)
A target no floor can meet gives 1.0, which switches that rule off: the classifier cannot do what was asked reliably.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from plugins._convo.helpers.convo_policy import Decision, GateProfile, PolicyGate, candidate_disposition  # noqa: E402

GRID = [round(.05 * step, 2) for step in range(1, 20)] + [1.0]
RESPOND = {'addressed', 'unclear'}
MIN_BENEFIT = .05      # a rule must act on at least this share of the speech it is for, at the floor chosen, or it is switched off


def load(path):
    with open(path, encoding='utf-8') as source:
        return [json.loads(line) for line in source if line.strip()]


def decision(record):
    best = max(record['attention'], key=record['attention'].get)
    return Decision.parse({'speech': 'silent', 'action': 'none', 'confidence': 0.0, 'explicit_request': False, 'attention': best,
                           'attention_probabilities': record['attention'], 'explicit_probability': record['explicit'].get('yes', 0.0),
                           'name_probability': record['name'].get('yes', 0.0)}, typed=True)


def wilson(count, total, z=1.96):
    if not total:
        return (float('nan'), float('nan'))
    p = count / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def rate(count, total):
    return '%5.1f%% (%d/%d)  95%% CI %4.0f-%.0f%%' % ((100 * count / total) if total else float('nan'), count, total, 100 * wilson(count, total)[0], 100 * wilson(count, total)[1])


def answered(record, profile, mode):
    decided = PolicyGate(profile=profile).evaluate(decision(record), epoch=0, current_epoch=0, episode=1, mode=mode)
    return decided.speech in {'speak', 'clarify'}


def clarified(record, profile):
    """Whether a fresh gate (no cooldown yet) would ask a clarifying question about this completed utterance."""
    decided = PolicyGate(profile=profile).evaluate(decision(record), epoch=0, current_epoch=0, episode=1, mode='active')
    return decided.speech == 'clarify'


def partial(record, profile):
    return candidate_disposition(decision(record), final=False, profile=profile)


def wanted(record):
    """A request to the assistant: failing to answer it is the cost of a miss."""
    return record['truth']['attention'] == 'addressed'


def neutral(record):
    """Vague speech that might be for the assistant: a clarifying question and silence are both acceptable, so it counts for neither side."""
    return record['truth']['attention'] == 'unclear'


def unwanted(record):
    """An acknowledgement, a conversation with someone else, or a thought not finished: answering it is the cost of a false answer."""
    return not wanted(record) and not neutral(record)


def interrupt_worthy(record):
    return record['truth']['attention'] == 'addressed' and record['truth']['explicit_request'] == 'yes'


def auroc(positive, negative):
    if not positive or not negative:
        return None
    return sum((p > n) + .5 * (p == n) for p in positive for n in negative) / (len(positive) * len(negative))


def choose(records, probes, args):
    """The five constants, from the calibration records alone."""
    chosen = {}
    needed = [r for r in records if wanted(r)]
    unneeded = [r for r in records if unwanted(r)]
    if args.miss_target is not None:
        # active listening, by recall: the strictest floor that still answers (1 - miss-target) of the speech that is for the assistant
        for tau in sorted(GRID, reverse=True):
            profile = GateProfile(respond_min=tau, ambient_respond_min=1.0)
            misses = sum(not answered(r, profile, 'active') for r in needed)
            if needed and misses / len(needed) <= args.miss_target:
                chosen['respond_min'] = tau
                break
        chosen.setdefault('respond_min', GRID[0])
    else:
        # active listening, by cost: the floor with the least expected cost when one unanswered request costs `cost-ratio` false answers.
        # (For a weak classifier a recall target alone drives the floor to "answer everything"; the cost shows what that buys.)
        best = None
        for tau in GRID:
            profile = GateProfile(respond_min=tau, ambient_respond_min=1.0)
            miss = sum(not answered(r, profile, 'active') for r in needed) / len(needed) if needed else 0.0
            false = sum(answered(r, profile, 'active') for r in unneeded) / len(unneeded) if unneeded else 0.0
            cost = args.cost_ratio * miss + false
            if best is None or cost < best[0] - 1e-12:             # ties keep the lower floor: the safer way to be wrong
                best = (cost, tau)
        chosen['respond_min'] = best[1]
    # ambient listening: the loosest floor (not below the active one) that answers at most false-target of the speech that is not
    chosen['ambient_respond_min'] = 1.0
    for tau in sorted(g for g in GRID if g >= chosen['respond_min']):
        profile = GateProfile(respond_min=chosen['respond_min'], ambient_respond_min=tau)
        if unneeded and sum(answered(r, profile, 'ambient') for r in unneeded) / len(unneeded) <= args.false_target:
            chosen['ambient_respond_min'] = tau
            break
    source = probes or records
    # A rule that meets its harm target only by never acting is no rule: it needs to do some good (MIN_BENEFIT) or it is switched off (1.0).
    chosen['interrupt_min'] = 1.0
    worthy = [r for r in source if interrupt_worthy(r)]
    others = [r for r in source if not interrupt_worthy(r)]
    for tau in GRID:
        profile = GateProfile(interrupt_min=tau)
        if others and sum(partial(r, profile) == 'admit' for r in others) / len(others) <= args.interrupt_target:
            if worthy and sum(partial(r, profile) == 'admit' for r in worthy) / len(worthy) >= MIN_BENEFIT:
                chosen['interrupt_min'] = tau
            break
    chosen['ignore_min'] = 1.0
    wanting = [r for r in source if wanted(r)]
    unwanting = [r for r in source if unwanted(r)]
    for tau in [g for g in GRID if g >= .5]:
        profile = GateProfile(ignore_min=tau)
        if wanting and sum(partial(r, profile) == 'ignore' for r in wanting) / len(wanting) <= args.ignore_target:
            if unwanting and sum(partial(r, profile) == 'ignore' for r in unwanting) / len(unwanting) >= MIN_BENEFIT:
                chosen['ignore_min'] = tau
            break
    named = [r['name'].get('yes', 0.0) for r in records if r['truth']['addressed_by_name'] == 'yes']
    unnamed = [r['name'].get('yes', 0.0) for r in records if r['truth']['addressed_by_name'] == 'no']
    chosen['name_min'] = 1.0
    for tau in GRID:
        if unnamed and sum(score >= tau for score in unnamed) / len(unnamed) <= args.false_target:
            chosen['name_min'] = tau
            break
    chosen['_name_auroc'] = auroc(named, unnamed)
    return chosen


def report(title, records, probes, profile):
    print('\n--- %s' % title)
    needed = [r for r in records if wanted(r)]
    unneeded = [r for r in records if unwanted(r)]
    vague = [r for r in records if neutral(r)]
    print('active listening, completed utterance (answer at %.2f):' % profile.respond_min)
    print('   requests to the assistant that went unanswered: ' + rate(sum(not answered(r, profile, 'active') for r in needed), len(needed)))
    print('   other speech that was answered                : ' + rate(sum(answered(r, profile, 'active') for r in unneeded), len(unneeded)))
    for name in ('backchannel', 'side_conversation', 'unfinished'):
        group = [r for r in records if r['truth']['attention'] == name]
        print('       of which %-18s answered      : %s' % (name, rate(sum(answered(r, profile, 'active') for r in group), len(group))))
    print('   vague speech (either outcome is fine) answered: ' + rate(sum(answered(r, profile, 'active') for r in vague), len(vague)))
    print('   clarifying questions asked (clarify_min %.2f, elsewhere under %.2f):' % (profile.clarify_min, profile.clarify_elsewhere_max))
    print('       about requests to the assistant              : ' + rate(sum(clarified(r, profile) for r in needed), len(needed)))
    print('       about other speech (must stay at zero)       : ' + rate(sum(clarified(r, profile) for r in unneeded), len(unneeded)))
    print('       about vague speech                           : ' + rate(sum(clarified(r, profile) for r in vague), len(vague)))
    print('ambient listening (answer at %.2f):' % profile.ambient_respond_min)
    print('   requests to the assistant that went unanswered: ' + rate(sum(not answered(r, profile, 'ambient') for r in needed), len(needed)))
    print('   other speech that was answered                : ' + rate(sum(answered(r, profile, 'ambient') for r in unneeded), len(unneeded)))
    source = probes or records
    label = 'probes' if probes else 'completed clips used as probes (no --probes file)'
    worthy = [r for r in source if interrupt_worthy(r)]
    others = [r for r in source if not interrupt_worthy(r)]
    print('speech still going on (%s):' % label)
    print('   requests that stopped the assistant (>= %.2f)  : %s' % (profile.interrupt_min, rate(sum(partial(r, profile) == 'admit' for r in worthy), len(worthy))))
    print('   other speech that stopped the assistant       : ' + rate(sum(partial(r, profile) == 'admit' for r in others), len(others)))
    wanting = [r for r in source if wanted(r)]
    print('   requests waved away (>= %.2f)                  : %s' % (profile.ignore_min, rate(sum(partial(r, profile) == 'ignore' for r in wanting), len(wanting))))
    unwanting = [r for r in source if unwanted(r)]
    print('   other speech correctly waved away             : ' + rate(sum(partial(r, profile) == 'ignore' for r in unwanting), len(unwanting)))
    named = [r for r in records if r['truth']['addressed_by_name'] == 'yes']
    unnamed = [r for r in records if r['truth']['addressed_by_name'] == 'no']
    print('name mode (name probability >= %.2f):' % profile.name_min)
    print('   calls by name recognised                      : ' + rate(sum(decision(r).name_probability >= profile.name_min for r in named), len(named)))
    print('   other speech taken for a call by name         : ' + rate(sum(decision(r).name_probability >= profile.name_min for r in unnamed), len(unnamed)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('records')
    parser.add_argument('--probes', default='', help='the same clips cut short, as a probe while the person is still speaking')
    parser.add_argument('--cost-ratio', type=float, default=2.0, help='active listening: one unanswered request costs this many false answers (default)')
    parser.add_argument('--miss-target', type=float, default=None, help='active listening by recall instead: answer at least 1 - this share of the speech for the assistant')
    parser.add_argument('--false-target', type=float, default=.05)
    parser.add_argument('--interrupt-target', type=float, default=.05)
    parser.add_argument('--ignore-target', type=float, default=.05)
    parser.add_argument('--show-test', action='store_true')
    parser.add_argument('--shipped', action='store_true', help='evaluate the defaults Convo ships instead of the recommendation')
    parser.add_argument('--json', action='store_true', help='print only the recommended policy_gate settings')
    args = parser.parse_args()
    records, probes = load(args.records), (load(args.probes) if args.probes else [])
    cal = [r for r in records if r['split'] == 'cal']
    cal_probes = [r for r in probes if r['split'] == 'cal']
    chosen = choose(cal, cal_probes, args)
    auc = chosen.pop('_name_auroc')
    settings = dict(chosen)
    if args.json:
        print(json.dumps({'policy_gate': settings}, indent=2))
        return
    profile = GateProfile() if args.shipped else GateProfile.from_settings(settings)
    print('records: %d (%d calibration, %d held out); probes: %d' % (len(records), len(cal), len(records) - len(cal), len(probes)))
    print('recommended settings, chosen on the calibration split only:')
    print(json.dumps({'policy_gate': settings}, indent=2))
    if args.shipped:
        print('EVALUATING the defaults Convo ships instead: ' + json.dumps({k: getattr(profile, k) for k in settings}))
    print('name AUROC on the calibration split: %s%s' % ('n/a' if auc is None else '%.2f' % auc, '   <- the name probability is not a reliable signal for this model' if auc is not None and auc < .8 else ''))
    report('calibration split (where the constants were chosen)', cal, cal_probes, profile)
    if args.show_test:
        report('HELD-OUT split (the honest check)', [r for r in records if r['split'] == 'test'], [r for r in probes if r['split'] == 'test'], profile)


if __name__ == '__main__':
    main()
