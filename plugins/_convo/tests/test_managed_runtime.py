import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from plugins._convo.helpers import managed_runtime as managed
from plugins._convo.helpers.convo_contract import validate_settings


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = managed.ManagedRuntime(self.tmp.name)
        self.addCleanup(self.manager.stop)

    def test_missing_environment_does_not_spawn_or_download(self):
        with patch.object(managed.subprocess, 'Popen') as spawn:
            self.manager.start()
        spawn.assert_not_called()
        self.assertEqual(self.manager.public()['state'], 'setup_required')

    def test_start_once_private_socket_and_environment_only_token(self):
        process = MagicMock()
        process.poll.return_value = None
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.getsockname.return_value = ('127.0.0.1', 49100)
        listener.fileno.return_value = 7
        with patch.object(managed.sys, 'platform', 'linux'), patch.object(Path, 'is_file', return_value=True), \
             patch.object(managed.socket, 'socket', return_value=listener), \
             patch.object(managed.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(managed.threading, 'Thread'):
            self.manager.start()
            self.manager.start()
        spawn.assert_called_once()
        args, kwargs = spawn.call_args
        self.assertNotIn(self.manager.token, str(args))
        self.assertEqual(kwargs['pass_fds'], (7,))
        self.assertEqual(kwargs['env']['CONVO_SIDECAR_TOKEN'], self.manager.token)
        self.assertEqual(self.manager.public()['state'], 'starting')
        self.manager.stop()
        process.terminate.assert_called_once()
        self.assertNotIn(managed.TOKEN_ENV, os.environ)
        self.assertEqual(self.manager.public()['state'], 'stopped')

    def test_unresponsive_owned_process_is_killed_with_bounded_wait(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('owned', 1.5), 0]
        self.manager.process = process
        self.manager.stop()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_remote_managed_combination_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_settings({'sidecar': {'managed': True, 'remote': True}})
        with self.assertRaises(ValueError):
            validate_settings({'sidecar': {'managed': 'yes'}})

    def test_readiness_gate_is_not_bypassed(self):
        with patch.object(managed, 'instance', return_value=self.manager):
            with self.assertRaises(ValueError):
                managed.require_ready({'sidecar': {'managed': True}})
            managed.require_ready({'sidecar': {'managed': False}})

    def test_stale_health_response_cannot_mark_replacement_ready(self):
        previous = MagicMock()
        with patch.object(managed, 'build_opener') as network:
            self.manager._health(previous, 'http://127.0.0.1:1', 'private')
        network.return_value.open.assert_not_called()
        self.assertEqual(self.manager.public()['state'], 'stopped')


class FakeClock:
    """Virtual time for the state machine: sleeping advances it and lets a test sample the public state."""
    def __init__(self, start=1000.0):
        self.now, self.hooks = start, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in list(self.hooks):
            hook(self.now)


class GatewayStateMachineTests(unittest.TestCase):
    """A slow cold start is not a failure, and a degraded gateway is never a permanent verdict."""
    HEALTHY = {'service': 'convo', 'protocol': 1, 'studio_available': False}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.spawned, self.fetches = FakeClock(), [], []
        self.healthy_from = None
        self.manager = managed.ManagedRuntime(self.tmp.name, clock=self.clock, sleep=self.clock.sleep,
                                              fetch=self.fetch, spawn=lambda target, *args: self.spawned.append((target, args)))
        self.addCleanup(self.manager.stop)

    def fetch(self, url, token):
        self.fetches.append(self.clock.now)
        return dict(self.HEALTHY) if self.healthy_from is not None and self.clock.now >= self.healthy_from else None

    def process(self, alive=True):
        process = MagicMock()
        process.poll.return_value = None if alive else 1
        return process

    def start(self, process=None):
        process = process or self.process()
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.getsockname.return_value = ('127.0.0.1', 49100)
        listener.fileno.return_value = 7
        with patch.object(managed.sys, 'platform', 'linux'), patch.object(Path, 'is_file', return_value=True), \
             patch.object(managed.socket, 'socket', return_value=listener), \
             patch.object(managed.subprocess, 'Popen', return_value=process):
            self.manager.start()
        return process

    def run_spawned(self):
        """Run (in order) everything the manager asked to run in the background."""
        while self.spawned:
            target, args = self.spawned.pop(0)
            target(*args)

    def sample_while_waiting(self):
        samples, begun = [], self.clock.now
        self.clock.hooks.append(lambda now: samples.append((round(now - begun), self.manager.public()['state'])))
        return samples

    def test_cold_start_is_starting_then_slow_then_ready_and_never_failed(self):
        self.start()
        self.healthy_from = self.clock.now + 95                        # a cold import over a slow mount
        samples = self.sample_while_waiting()
        health = [item for item in self.spawned if item[0] == self.manager._health][0]
        self.spawned.remove(health)
        health[0](*health[1])
        states = [state for _, state in samples] + [self.manager.public()['state']]    # the last sleep precedes the healthy answer
        self.assertEqual([s for i, s in enumerate(states) if i == 0 or s != states[i - 1]], ['starting', 'slow_start', 'ready'])
        first_slow = next(second for second, state in samples if state == 'slow_start')
        self.assertTrue(19 <= first_slow <= 22, first_slow)            # SLOW_AFTER
        self.assertEqual(self.manager.public()['state'], 'ready')

    def test_health_failed_only_after_the_deadline_while_the_process_is_alive(self):
        self.start()
        samples = self.sample_while_waiting()
        health = [item for item in self.spawned if item[0] == self.manager._health][0]
        self.spawned.remove(health)
        health[0](*health[1])
        by_second = dict(samples)
        self.assertEqual({state for second, state in samples if second < 180}, {'starting', 'slow_start'})
        self.assertEqual(self.manager.public()['state'], 'health_failed')
        self.assertGreaterEqual(self.manager.public()['waited'], 180)
        self.assertIn('Not responding', self.manager.public()['label'])

    def test_degraded_gateway_recovers_through_a_single_flight_background_reprobe(self):
        self.start()
        self.manager.status = 'health_failed'                           # the old latch
        self.spawned.clear()
        self.fetches.clear()
        self.manager.public(); self.manager.public()
        self.assertEqual(len(self.spawned), 1)                          # single flight: the second call finds one pending
        self.run_spawned()
        self.assertEqual(self.manager.public()['state'], 'health_failed')   # still unhealthy; the probe ran off-line
        self.assertEqual(self.manager.public()['state'], 'health_failed')
        self.assertEqual((len(self.fetches), len(self.spawned)), (1, 0))    # re-probes are rate limited
        self.clock.now += 2
        self.manager.public()
        self.assertEqual(len(self.spawned), 0)                          # still inside REPROBE
        self.clock.now += 4
        self.healthy_from = self.clock.now
        self.manager.public()
        self.assertEqual(len(self.spawned), 1)
        self.run_spawned()
        self.assertEqual(self.manager.public()['state'], 'ready')
        self.manager.public()
        self.assertEqual(self.spawned, [])                              # a healthy gateway is not probed again

    def test_public_never_performs_io(self):
        self.start()
        self.spawned.clear(); self.fetches.clear()
        for status in ('starting', 'health_failed', 'ready', 'startup_failed', 'setup_required', 'stopped'):
            self.manager.status = status
            for _ in range(5):
                self.manager.public()
        self.assertEqual(self.fetches, [])

    def test_exited_process_is_a_startup_failure_and_is_never_reprobed(self):
        process = self.start(self.process())
        self.manager.status = 'health_failed'
        process.poll.return_value = 3
        self.spawned.clear()
        self.assertEqual(self.manager.public()['state'], 'startup_failed')
        self.assertEqual(self.spawned, [])

    def test_stale_probe_of_a_replaced_process_cannot_mark_the_replacement_ready(self):
        first = self.start()
        self.manager.status = 'health_failed'
        self.spawned.clear()
        self.manager.public()
        stale = list(self.spawned)
        self.manager.stop()
        self.clock.now += 60
        self.start(self.process())
        self.healthy_from = self.clock.now - 1
        for target, args in stale:
            target(*args)
        self.assertEqual(self.manager.status, 'starting')

    def test_explicit_start_rechecks_a_degraded_gateway_instead_of_trusting_the_latch(self):
        self.manager.__dict__['spawn'] = lambda target, *args: target(*args)   # run the probe inline
        self.start()
        self.manager.status = 'health_failed'
        self.healthy_from = self.clock.now
        with patch.object(managed, 'instance', return_value=self.manager):
            managed.require_ready({'sidecar': {'managed': True}})
            self.assertEqual(self.manager.public()['state'], 'ready')
            self.manager.status = 'health_failed'
            self.healthy_from = None
            self.clock.now += 10
            with self.assertRaisesRegex(ValueError, 'did not answer its health check'):
                managed.require_ready({'sidecar': {'managed': True}})

    def test_every_state_has_a_label_and_a_refusal(self):
        for state in managed.LABELS:
            self.assertTrue(managed.LABELS[state])
            self.assertTrue(state == 'ready' or managed.REFUSALS[state])

    def test_stderr_tail_is_bounded_private_and_replaced_atomically(self):
        process = MagicMock()
        process.stderr = [(b'x' * 2000 + b'\n')] * 1000 + [b'\xff\xfe bad bytes\n']
        self.manager._drain(process)
        log = Path(self.tmp.name) / 'usr/plugins/_convo/data' / managed.LOG_NAME
        lines = log.read_text(encoding='utf-8').splitlines()
        self.assertLessEqual(len(lines), managed.LOG_LINES)
        self.assertTrue(all(len(line) <= managed.LOG_LINE_CHARS for line in lines))
        self.assertFalse(log.with_name(log.name + '.tmp').exists())
        if os.name != 'nt':
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_instance_from_older_code_is_adopted_after_a_hot_class_swap(self):
        old = object.__new__(managed.ManagedRuntime)
        old.root, old.lock, old.process = Path(self.tmp.name), threading.RLock(), self.process()
        old.url, old.token, old.status, old.retry_after, old.studio_available = 'http://127.0.0.1:1', 't', 'health_failed', 0, False
        old.__dict__.update(clock=self.clock, fetch=self.fetch, spawn=lambda target, *args: target(*args))
        self.healthy_from = self.clock.now
        self.assertEqual(old.public()['state'], 'ready')               # the latch from before the update is not a verdict
