import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins._convo.helpers.convo_contract import Capabilities, ContractError, endpoint, merge, validate_settings
from plugins._convo.helpers.convo_migration import atomic_json, migrate, read_config, rollback
from plugins._convo.helpers.convo_policy import Decision, PolicyGate, route_tool
from plugins._convo.helpers.convo_store import Store


class ContractTests(unittest.TestCase):
    def test_capabilities_are_independent_and_boolean(self):
        self.assertFalse(Capabilities.parse({"audio_input": True}).duplex)
        for invalid in ({"duplex": True}, {"audio_input": "false"}, {"magic": True}, {"incremental_input": True}):
            with self.assertRaises(ContractError):
                Capabilities.parse(invalid)

    def test_no_implicit_remote_or_embedded_secrets(self):
        for url in ("https://provider.example/v1", "http://169.254.169.254", "http://user:secret@localhost", "file:///tmp/x"):
            with self.assertRaises(ContractError):
                endpoint({"url": url})
        self.assertEqual(endpoint({"url": "https://provider.example/v1", "remote": True}), "https://provider.example/v1")
        self.assertFalse(validate_settings({})["enabled"])

    def test_unknown_false_and_empty_config_survive(self):
        self.assertEqual(merge({"a": {"x": True, "y": "old"}}, {"a": {"x": False, "y": "", "future": 4}}), {"a": {"x": False, "y": "", "future": 4}})

    def test_tuning_profile_is_a_bounded_string(self):
        for value in (None, 42, [], 'x' * 2001):
            with self.assertRaises(ContractError):
                validate_settings({'roles':{'tts':{'tuning_profile':value}}})
        for value in ('', 'voice-fast'):
            self.assertEqual(validate_settings({'roles':{'tts':{'tuning_profile':value}}})['roles']['tts']['tuning_profile'],value)


class PolicyTests(unittest.TestCase):
    def test_invalid_nan_and_stale_fail_silent(self):
        gate = PolicyGate()
        for value in ({}, {"speech": "speak", "action": "direct", "confidence": float("nan")}, {"speech": "hmm", "action": "direct", "confidence": 1}):
            self.assertEqual(Decision.parse(value), Decision())
        self.assertEqual(gate.evaluate(Decision("speak", "direct", 1, True), epoch=0, current_epoch=1, episode=1), Decision())

    def test_speech_does_not_authorize_actions(self):
        result = PolicyGate().evaluate(Decision("speak", "delegate", .99, False), epoch=0, current_epoch=0, episode=1)
        self.assertEqual(result.speech, "speak")
        self.assertEqual(result.action, "none")
        self.assertEqual(route_tool("code", result), "deny")
        self.assertEqual(route_tool("code", Decision("silent", "direct", 1, True)), "delegate")

    def test_clarify_once_per_episode_and_cooldown(self):
        clock = [100.0]
        gate = PolicyGate(clock=lambda: clock[0])
        ask = Decision("clarify", "none", .8)
        def run(episode):
            return gate.evaluate(ask, epoch=0, current_epoch=0, episode=episode).speech
        self.assertEqual(run(1), "clarify")
        self.assertEqual(run(2), "silent")
        clock[0] = 170
        self.assertEqual(run(1), "silent")
        self.assertEqual(run(2), "clarify")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "convo.sqlite3"
        self.store = Store(self.path)
        self.s = self.store.start("browser", "chat-a")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add_turn(self, number):
        self.s = self.store.session(self.s["id"], "browser")
        turn = str(number)
        self.assertTrue(self.store.accept_turn(self.s["id"], "browser", self.s["epoch"], turn, {"transcript": "Remember item " + turn}))
        self.assertTrue(self.store.settle(self.s["id"], "browser", self.s["epoch"], turn, "Noted " + turn))

    def test_one_owner_no_stale_delivery_and_no_audio_retention(self):
        with self.assertRaises(ValueError):
            self.store.start("another", "chat-b")
        with self.assertRaises(ValueError):
            self.store.session(self.s["id"], "another")
        self.store.interrupt(self.s["id"], "browser")
        self.assertFalse(self.store.append(self.s["id"], "browser", 0, "", "text", {}))
        with self.assertRaises(ValueError):
            self.store.append(self.s["id"], "browser", 1, "", "text", {"audio": "secret"})

    def test_200_turns_compact_without_deleting_events(self):
        for n in range(200):
            self.add_turn(n)
            if n % 11 == 0:
                snapshot = self.store.snapshot(self.s["id"], "browser")
                if snapshot:
                    self.assertTrue(self.store.splice(snapshot, "Stable decisions " + str(n)))
        self.assertEqual(len(self.store.events("chat-a", limit=500)), 418)
        self.assertLess(len(self.store.context("chat-a")["turns"]), 18)

    def test_append_during_summary_is_allowed_but_edited_prefix_is_not(self):
        for n in range(10):
            self.add_turn(n)
        snapshot = self.store.snapshot(self.s["id"], "browser")
        self.store.interrupt(self.s["id"], "browser")
        self.add_turn(10)
        self.assertTrue(self.store.splice(snapshot, "prefix summary"))
        self.add_turn(11)
        snapshot = self.store.snapshot(self.s["id"], "browser")
        self.store.transcript(self.s["id"], "browser", "4", "corrected")
        self.assertFalse(self.store.splice(snapshot, "stale summary"))

    def test_retarget_invalidates_summary_and_preserves_jobs(self):
        for n in range(8):
            self.add_turn(n)
        snapshot = self.store.snapshot(self.s["id"], "browser")
        job = self.store.submit(self.s["id"], "browser", 0, "7", "request", "Build something")
        self.assertEqual(job, self.store.submit(self.s["id"], "browser", 0, "7", "request", "Build something"))
        self.store.retarget(self.s["id"], "browser", "chat-b")
        self.assertFalse(self.store.splice(snapshot, "wrong chat"))
        self.assertEqual(self.store.job(job["id"])["target"], "chat-a")
        self.store.stop(self.s["id"], "browser")
        self.assertEqual(self.store.job(job["id"])["status"], "queued")

    def test_restart_marks_unreconciled_work_uncertain(self):
        job = self.store.submit(self.s["id"], "browser", 0, "1", "r", "work")
        self.assertTrue(self.store.transition(job["id"], "queued", "dispatching"))
        self.assertFalse(self.store.transition(job["id"], "queued", "dispatching"))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.job(job["id"])["status"], "uncertain")
        with self.assertRaises(ValueError):
            self.store.session(self.s["id"], "browser")


class MigrationTests(unittest.TestCase):
    def test_merge_rollback_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "_enhanced_speech" / "config.json"
            new = root / "_convo" / "config.json"
            atomic_json(old, {"tts": {"voice": "old", "blend": 70}, "future": True})
            atomic_json(new, {"tts": {"voice": "new"}, "convo": {"enabled": False}})
            original = read_config(new)
            self.assertTrue(migrate(root)["changed"])
            migrate(root, apply=True)
            self.assertEqual(read_config(new)["tts"], {"voice": "new", "blend": 70})
            self.assertTrue(migrate(root, apply=True)["already_migrated"])
            rollback(root)
            self.assertEqual(read_config(new), original)
            self.assertEqual(read_config(old)["tts"]["voice"], "old")

    def test_rollback_refuses_newer_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrate(root, apply=True)
            atomic_json(root / "_convo" / "config.json", {"new": 1})
            with self.assertRaises(ValueError):
                rollback(root)


if __name__ == "__main__":
    unittest.main()
