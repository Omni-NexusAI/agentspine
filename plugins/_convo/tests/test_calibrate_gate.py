"""The measurement tool recommends constants from the calibration split only, uses the product's own gate, and says what it measured."""
import contextlib
import io
import json
import random
import tempfile
import unittest
import unittest.mock
from argparse import Namespace
from pathlib import Path

from plugins._convo.helpers.convo_policy import GateProfile
from plugins._convo.tests import calibrate_gate as tool

CLASSES = ('addressed', 'backchannel', 'side_conversation', 'unfinished', 'unclear')


def record(identifier, truth, split, rng, strength=.6):
    """A stylised classifier: leans toward the true class by `strength`, with noise, probabilities summing to one."""
    weights = {name: rng.random() * (1 - strength) for name in CLASSES}
    weights[truth] += strength
    total = sum(weights.values())
    attention = {name: value / total for name, value in weights.items()}
    explicit = .85 if truth == 'addressed' else .1
    return {'id': identifier, 'split': split, 'voice': 'v', 'hard': False,
            'truth': {'attention': truth, 'explicit_request': 'yes' if truth == 'addressed' else 'no', 'addressed_by_name': 'no'},
            'attention': attention, 'explicit': {'yes': explicit, 'no': 1 - explicit}, 'name': {'yes': .1, 'no': .9}}


def records(count=60, seed=5):
    rng = random.Random(seed)
    out = []
    for index in range(count):
        truth = CLASSES[index % len(CLASSES)]
        out.append(record('r%d' % index, truth, 'cal' if index % 2 == 0 else 'test', rng))
    return out


ARGS = Namespace(miss_target=.10, cost_ratio=2.0, false_target=.05, interrupt_target=.05, ignore_target=.05)       # by recall
BY_COST = Namespace(miss_target=None, cost_ratio=2.0, false_target=.05, interrupt_target=.05, ignore_target=.05)    # by cost (the tool's default)


def cost(rows, profile, ratio):
    needed = [r for r in rows if tool.wanted(r)]
    others = [r for r in rows if tool.unwanted(r)]
    return ratio * sum(not tool.answered(r, profile, 'active') for r in needed) / len(needed) + sum(tool.answered(r, profile, 'active') for r in others) / len(others)


class Calibration(unittest.TestCase):
    def test_the_recommendation_is_a_valid_gate_and_meets_its_targets_on_the_calibration_split(self):
        cal = [r for r in records() if r['split'] == 'cal']
        chosen = tool.choose(cal, [], ARGS)
        chosen.pop('_name_auroc')
        profile = GateProfile.from_settings(chosen)              # ordering and range rules hold
        needed = [r for r in cal if tool.wanted(r)]
        self.assertLessEqual(sum(not tool.answered(r, profile, 'active') for r in needed) / len(needed), ARGS.miss_target + 1e-9)
        others = [r for r in cal if tool.unwanted(r)]
        self.assertLessEqual(sum(tool.answered(r, profile, 'ambient') for r in others) / len(others), ARGS.false_target + 1e-9)
        self.assertGreaterEqual(profile.ambient_respond_min, profile.respond_min)

    def test_by_cost_the_chosen_floor_is_the_cheapest_on_the_grid_and_ties_keep_the_lower_floor(self):
        cal = [r for r in records() if r['split'] == 'cal']
        chosen = tool.choose(cal, [], BY_COST)
        costs = {tau: cost(cal, GateProfile(respond_min=tau, ambient_respond_min=1.0), BY_COST.cost_ratio) for tau in tool.GRID}
        best = min(costs.values())
        self.assertAlmostEqual(costs[chosen['respond_min']], best, places=9)
        self.assertEqual(chosen['respond_min'], min(tau for tau, value in costs.items() if abs(value - best) < 1e-9))

    def test_a_dearer_miss_never_raises_the_floor_by_cost(self):
        cal = [r for r in records() if r['split'] == 'cal']
        cheap = tool.choose(cal, [], Namespace(**{**vars(BY_COST), 'cost_ratio': 1.0}))['respond_min']
        dear = tool.choose(cal, [], Namespace(**{**vars(BY_COST), 'cost_ratio': 6.0}))['respond_min']
        self.assertLessEqual(dear, cheap)

    def test_a_target_nothing_can_meet_switches_the_rule_off_with_a_floor_of_one(self):
        cal = [r for r in records() if r['split'] == 'cal']
        useless = [dict(r, attention={name: .2 for name in CLASSES}, explicit={'yes': .5, 'no': .5}) for r in cal]      # knows nothing
        chosen = tool.choose(useless, useless, Namespace(**{**vars(BY_COST), 'interrupt_target': 0.0, 'ignore_target': 0.0}))
        self.assertEqual((chosen['interrupt_min'], chosen['ignore_min']), (1.0, 1.0))

    def test_the_held_out_split_never_influences_the_choice(self):
        data = records()
        cal = [r for r in data if r['split'] == 'cal']
        before = tool.choose(cal, [], ARGS)
        scrambled = [dict(r, attention={name: (1.0 if name == 'unclear' else 0.0) for name in CLASSES}) for r in data if r['split'] == 'test']
        after = tool.choose([r for r in data + scrambled if r['split'] == 'cal'], [], ARGS)
        self.assertEqual(before, after)

    def test_a_stricter_miss_target_never_raises_the_floor(self):
        cal = [r for r in records() if r['split'] == 'cal']
        loose = tool.choose(cal, [], Namespace(**{**vars(ARGS), 'miss_target': .30}))['respond_min']
        strict = tool.choose(cal, [], Namespace(**{**vars(ARGS), 'miss_target': .02}))['respond_min']
        self.assertLessEqual(strict, loose)

    def test_the_command_line_prints_settings_the_product_accepts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in records()), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output), unittest.mock.patch('sys.argv', ['calibrate_gate', str(path), '--json']):
                tool.main()
            settings = json.loads(output.getvalue())['policy_gate']
            self.assertEqual(set(settings), {'respond_min', 'ambient_respond_min', 'interrupt_min', 'ignore_min', 'name_min'})
            GateProfile.from_settings(settings)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), unittest.mock.patch('sys.argv', ['calibrate_gate', str(path), '--show-test']):
                tool.main()
            self.assertIn('HELD-OUT split', output.getvalue())
            self.assertIn('95% CI', output.getvalue())


if __name__ == '__main__':
    unittest.main()
