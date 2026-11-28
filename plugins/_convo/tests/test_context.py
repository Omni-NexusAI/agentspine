"""Bounded relevant context: selection, caps, redaction, target isolation and job-target preservation."""
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

from plugins._convo.helpers import convo_context as ctx
from plugins._convo.helpers.convo_context import Candidate
from plugins._convo.helpers.convo_host import Dispatcher
from plugins._convo.helpers.convo_store import Store


def many(count, text='deploy the staging website now', **extra):
    return [Candidate('user', text + ' ' + str(i), age=i, **extra) for i in range(count)]


class Selection(unittest.TestCase):
    def test_hard_caps(self):
        block = ctx.build_context('please deploy the staging website', many(40, 'deploy the staging website ' + 'padding ' * 80))
        self.assertLessEqual(len(block.snippets), 6)
        self.assertLessEqual(len(block.render()), 1500)
        self.assertTrue(all(len(s.text) <= 280 for s in block.snippets))
        tight = ctx.build_context('deploy staging website', many(10), max_snippets=99, max_chars=99999)
        self.assertLessEqual(len(tight.snippets), 6); self.assertLessEqual(len(tight.render()), 1500)

    def test_caps_hold_for_huge_snippets_and_a_tiny_budget(self):
        block = ctx.build_context('website', [Candidate('user', 'website ' + 'x' * 5000, age=i) for i in range(10)], max_chars=200)
        self.assertLessEqual(len(block.render()), 200)

    def test_only_relevant_history_is_selected(self):
        candidates = [Candidate('user', 'what is the weather in Lisbon', age=2), Candidate('user', 'rename the invoice export script', age=1),
                      Candidate('assistant', 'the invoice export script is now renamed', age=0)]
        block = ctx.build_context('why did the invoice export fail', candidates)
        texts = ' '.join(s.text for s in block.snippets)
        self.assertIn('invoice', texts); self.assertNotIn('Lisbon', texts)

    def test_nothing_relevant_means_nothing(self):
        block = ctx.build_context('tell me a joke', [Candidate('user', 'rename the invoice export script')])
        self.assertFalse(block); self.assertEqual(block.render(), '')
        self.assertEqual(block.with_task('write a limerick'), 'write a limerick')
        self.assertEqual(ctx.build_context('', []).render(), '')

    def test_newer_wins_ties(self):
        old, new = Candidate('user', 'update the router firmware OLD', age=5), Candidate('user', 'update the router firmware NEW', age=0)
        block = ctx.build_context('router firmware', [old, new], max_snippets=1)
        self.assertEqual([s.text for s in block.snippets], ['update the router firmware NEW'])
        again = ctx.build_context('router firmware', [new, old], max_snippets=1)
        self.assertEqual(block.render(), again.render())

    def test_a_pointing_request_gets_the_newest_item_only(self):
        block = ctx.build_context('do it again', [Candidate('assistant', 'archived the logs folder', age=0), Candidate('user', 'something older', age=3)])
        self.assertEqual([s.text for s in block.snippets], ['archived the logs folder'])
        self.assertFalse(ctx.build_context('banana', [Candidate('user', 'something else')]))

    def test_job_references_pull_in_the_job(self):
        jobs = [Candidate('job', 'Task: migrate the database', age=1, job='a' * 32, status='running'),
                Candidate('job', 'Task: update the readme Result: done', age=0, job='b' * 32, status='completed'),
                Candidate('user', 'what is the capital of Peru', age=0)]
        block = ctx.build_context('how is that task going', jobs)
        self.assertEqual(block.snippets[0].job, 'a' * 32)                       # running work first
        by_id = ctx.build_context('what happened with job bbbbbbbb', jobs)
        self.assertEqual(by_id.snippets[0].job, 'b' * 32)
        self.assertIn('running', block.render()); self.assertNotIn('Peru', block.render())

    def test_a_queued_job_is_never_shown_with_a_result(self):
        candidate = Candidate('job', 'Task: migrate the database', job='c' * 32, status='queued')
        line = ctx.build_context('status of the migrate task', [candidate]).snippets[0].line()
        self.assertIn('queued', line); self.assertNotIn('completed', line); self.assertNotIn('Result', line)


class Redaction(unittest.TestCase):
    SECRETS = ['sk-abcdefghijklmnop1234', 'Bearer abcdef1234567890xyz', 'api_key = hunter2hunter2', 'password: correcthorse',
               'ghp_abcdefghijklmnopqrst', 'AKIAABCDEFGHIJKLMNOP', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.abcDEFghi',
               'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1']

    def test_secret_shaped_strings_never_reach_the_block(self):
        for secret in self.SECRETS:
            block = ctx.build_context('deploy the website', [Candidate('user', 'deploy the website using ' + secret + ' for access')])
            self.assertIn('redacted', block.render(), secret)
            for fragment in (secret.split()[-1], secret[-12:]):
                self.assertNotIn(fragment, block.render(), secret)

    def test_a_secret_cut_by_the_length_limit_leaves_no_fragment(self):
        text = 'website ' + 'z' * 270 + ' sk-abcdefghijklmnop1234567890'
        block = ctx.build_context('website', [Candidate('user', text)])
        self.assertNotIn('sk-', block.render())

    def test_ordinary_text_and_job_ids_survive(self):
        job = 'f0e1d2c3' * 4
        block = ctx.build_context('status of that task', [Candidate('job', 'Task: sort 12 files by date', job=job, status='running')])
        self.assertIn('sort 12 files by date', block.render()); self.assertIn(job[:8], block.render())


class FromStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'convo.db'); self.addCleanup(self.store.close)

    def say(self, session, turn, said, heard=''):
        epoch = self.store.session(session['id'], 'o')['epoch']
        self.store.accept_turn(session['id'], 'o', epoch, turn, {'transcript': said})
        if heard:
            self.store.delivered(session['id'], 'o', epoch, turn, turn + 'p', heard)
        self.store.settle(session['id'], 'o', epoch, turn, heard)

    def test_only_the_selected_target_is_read(self):
        session = self.store.start('o', 'chatA')
        self.say(session, 't1', 'the billing report is late', 'I will check the billing report')
        session = self.store.retarget(session['id'], 'o', 'chatB')
        self.say(session, 't2', 'plan the garden layout', 'a raised bed works')
        text_b = ' '.join(c.text for c in ctx.candidates_from(self.store, 'chatB'))
        self.assertIn('garden', text_b); self.assertNotIn('billing', text_b)
        text_a = ' '.join(c.text for c in ctx.candidates_from(self.store, 'chatA'))
        self.assertIn('billing', text_a); self.assertNotIn('garden', text_a)

    def test_host_chat_messages_are_candidates_and_are_bounded(self):
        chat = [{'role': 'user', 'text': 'message %d about the quarterly forecast' % i} for i in range(100)]
        found = [c for c in ctx.candidates_from(self.store, 'chatA', chat) if c.kind.startswith('chat')]
        self.assertLessEqual(len(found), 16)
        self.assertEqual(found[0].age, 0); self.assertIn('99', found[0].text)    # the newest message is age 0
        block = ctx.build_context('the quarterly forecast', ctx.candidates_from(self.store, 'chatA', chat))
        self.assertLessEqual(len(block.snippets), 6)

    def test_job_candidates_use_the_real_status(self):
        session = self.store.start('o', 'chatA')
        self.say(session, 't1', 'migrate the database')
        epoch = self.store.session(session['id'], 'o')['epoch']
        job = self.store.submit(session['id'], 'o', epoch, 't1', 't1:job', 'migrate the database to v2')
        found = [c for c in ctx.candidates_from(self.store, 'chatA') if c.job]
        self.assertEqual((found[0].job, found[0].status), (job['id'], 'queued'))
        self.assertNotIn('Result', found[0].text)


class JobTargetSurvivesRetarget(unittest.TestCase):
    """A voice session that moves to another chat never moves a job that is already outstanding."""

    class Host:
        def __init__(self): self.began = []; self.futures = {}
        def available(self, target): return True
        def begin(self, job):
            future = Future(); self.began.append((job['id'], job['target'])); self.futures[job['id']] = future; return future
        def log(self, job, state): pass

    def test_retarget_leaves_outstanding_jobs_on_their_original_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'convo.db')
            try:
                session = store.start('o', 'chatA'); sid = session['id']
                store.accept_turn(sid, 'o', 0, 't1', {'transcript': 'migrate the database'})
                queued = store.submit(sid, 'o', 0, 't1', 't1:job', 'migrate the database')
                running = store.submit(sid, 'o', 0, 't1', 't1:job2', 'also archive the old logs')
                host = self.Host(); dispatcher = Dispatcher(store, host)
                dispatcher.tick()                                   # both are dispatched while the session still points at A
                moved = store.retarget(sid, 'o', 'chatB')
                self.assertEqual(moved['target'], 'chatB')
                for job in (queued, running):
                    self.assertEqual(store.job(job['id'])['target'], 'chatA')
                self.assertEqual(sorted(host.began), sorted([(queued['id'], 'chatA'), (running['id'], 'chatA')]))
                # A job queued after the move belongs to the new target; the old ones are unchanged.
                epoch = store.session(sid, 'o')['epoch']
                store.accept_turn(sid, 'o', epoch, 't2', {'transcript': 'plan the garden'})
                later = store.submit(sid, 'o', epoch, 't2', 't2:job', 'plan the garden')
                self.assertEqual(store.job(later['id'])['target'], 'chatB')
                self.assertEqual({j['id'] for j in store.jobs('chatA')}, {queued['id'], running['id']})
                self.assertEqual([j['id'] for j in store.jobs('chatB')], [later['id']])
                # Finishing the old job does not make it a notice for the new target.
                host.futures[queued['id']].set_result('done')
                dispatcher.tick()
                self.assertEqual(store.job(queued['id'])['status'], 'completed')
                with self.assertRaises(ValueError):
                    store.claim_notice(sid, 'o', store.session(sid, 'o')['epoch'], queued['id'])
            finally:
                store.close()

    def test_a_job_still_queued_when_the_session_moves_is_dispatched_to_its_own_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'convo.db')
            try:
                session = store.start('o', 'chatA'); sid = session['id']
                store.accept_turn(sid, 'o', 0, 't1', {'transcript': 'x'})
                job = store.submit(sid, 'o', 0, 't1', 't1:job', 'write the report')
                store.retarget(sid, 'o', 'chatB')
                host = self.Host(); Dispatcher(store, host).tick()
                self.assertEqual(host.began, [(job['id'], 'chatA')])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
