"""Voice timers (`helpers/convo_timers.py`): setting, listing and cancelling, the bounds, each due timer handed out once
(twice in a row, from two connections, from several threads at once, after reopening the database), and the ones that rang
while nobody could be told. In-memory and temporary SQLite databases and given times: no clock is waited for."""
import os
import sqlite3
import tempfile
import threading
import unittest

from plugins._convo.helpers.convo_timers import BOUNDS, LONGEST, MOST, NO_SCOPE, TOO_MANY, UNAVAILABLE, Timers

NOW = 1_800_000_000.0


class Memory(unittest.TestCase):
    """One in-memory database, as a connection of the timers' own."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.timers = Timers(lambda: self.db)

    def test_a_new_timer(self):
        timer = self.timers.add('chat-1', 600, '  pasta   water ', now=NOW)
        self.assertEqual(timer, {'id': timer['id'], 'scope': 'chat-1', 'label': 'pasta water', 'due': NOW + 600, 'created': NOW, 'state': 'pending'})
        self.assertRegex(timer['id'], r'^[0-9a-f]{8}$')
        self.assertEqual(self.timers.pending('chat-1', now=NOW + 100), [{**timer, 'left': 500.0}])
        self.assertEqual(self.timers.pending(now=NOW + 700), [{**timer, 'left': 0.0}], 'a due timer nobody claimed yet is still pending')
        self.assertEqual(self.timers.add('chat-1', 5, now=NOW)['label'], '')
        self.assertEqual(len(self.timers.add('chat-1', 5, 'x' * 500, now=NOW)['label']), 80)

    def test_pending_is_per_scope_and_soonest_first(self):
        late, soon, other = self.timers.add('a', 900, 'late', now=NOW), self.timers.add('a', 60, 'soon', now=NOW), self.timers.add('b', 5, now=NOW)
        self.assertEqual([timer['id'] for timer in self.timers.pending('a', now=NOW)], [soon['id'], late['id']])
        self.assertEqual([timer['id'] for timer in self.timers.pending('b', now=NOW)], [other['id']])
        self.assertEqual([timer['id'] for timer in self.timers.pending(now=NOW)], [other['id'], soon['id'], late['id']])
        self.assertEqual(self.timers.pending('nobody', now=NOW), [])

    def test_the_bounds_of_a_timer(self):
        for seconds in (0, 0.5, 0.999, -5, LONGEST + 1, 10 ** 9, float('nan'), float('inf'), 'soon', None, '', [], {}):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError) as caught:
                    self.timers.add('chat', seconds, now=NOW)
                self.assertEqual(str(caught.exception), BOUNDS)
        self.assertEqual(self.timers.pending(now=NOW), [])
        for seconds in (1, 1.0, '90', 2.5, LONGEST):
            self.assertEqual(self.timers.add('chat', seconds, now=NOW)['due'], NOW + float(seconds))

    def test_a_scope_is_required(self):
        for scope in ('', '   ', None):
            for call in (lambda: self.timers.add(scope, 5), lambda: self.timers.cancel(scope), lambda: self.timers.missed(scope)):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertEqual(str(caught.exception), NO_SCOPE)
        with self.assertRaises(ValueError):
            self.timers.pending('')

    def test_at_most_twenty_pending_timers_for_one_scope(self):
        timers = [self.timers.add('chat', 60 + number, now=NOW) for number in range(MOST)]
        with self.assertRaises(ValueError) as caught:
            self.timers.add('chat', 60, now=NOW)
        self.assertEqual(str(caught.exception), TOO_MANY)
        self.assertEqual(len(self.timers.pending('chat', now=NOW)), MOST)
        self.timers.add('another chat', 60, now=NOW)                     # the bound is per scope
        self.timers.cancel('chat', timers[0]['id'])
        self.timers.add('chat', 60, now=NOW)                             # a cancelled timer frees its place
        with self.assertRaises(ValueError):
            self.timers.add('chat', 60, now=NOW)
        rang = [timer for timer in self.timers.due(now=NOW + 61) if timer['scope'] == 'chat']
        self.assertEqual(len(rang), 2)                                   # and so does each one that rang
        self.timers.add('chat', 60, now=NOW)
        self.timers.add('chat', 60, now=NOW)
        with self.assertRaises(ValueError):
            self.timers.add('chat', 60, now=NOW)

    def test_cancelling_the_latest(self):
        first, second = self.timers.add('chat', 600, 'eggs', now=NOW), self.timers.add('chat', 30, 'tea', now=NOW + 5)
        self.timers.add('other', 30, 'theirs', now=NOW + 9)
        for which in ('', 'latest', ' Latest '):
            with self.subTest(which=which):
                kept = self.timers.add('chat', 10, 'newest', now=NOW + 10)
                self.assertEqual(self.timers.cancel('chat', which), [{**kept, 'state': 'cancelled'}], 'the one set last, not the one due first or last')
        self.assertEqual(self.timers.cancel('chat'), [{**second, 'state': 'cancelled'}])
        self.assertEqual(self.timers.cancel('chat'), [{**first, 'state': 'cancelled'}])
        self.assertEqual(self.timers.cancel('chat'), [], 'nothing left: nothing cancelled, and another scope is never touched')
        self.assertEqual([timer['label'] for timer in self.timers.pending(now=NOW)], ['theirs'])

    def test_cancelling_by_id_or_label(self):
        eggs, tea = self.timers.add('chat', 600, 'Boiled Eggs', now=NOW), self.timers.add('chat', 300, 'tea', now=NOW)
        plain, tea_two = self.timers.add('chat', 100, now=NOW), self.timers.add('chat', 200, 'Tea', now=NOW)
        other = self.timers.add('other', 100, 'tea', now=NOW)
        self.assertEqual(self.timers.cancel('chat', 'coffee'), [])
        self.assertEqual(self.timers.cancel('chat', other['id']), [], "another scope's timer cannot be cancelled by its id")
        self.assertEqual(self.timers.cancel('chat', plain['id'].upper()), [{**plain, 'state': 'cancelled'}])
        self.assertEqual({timer['id'] for timer in self.timers.cancel('chat', ' TEA ')}, {tea['id'], tea_two['id']}, 'every timer with that label')
        self.assertEqual(self.timers.cancel('chat', 'the boiled eggs timer'), [{**eggs, 'state': 'cancelled'}], 'a label inside what was said')
        again = self.timers.add('chat', 600, 'laundry in the dryer', now=NOW)
        unlabelled = self.timers.add('chat', 600, now=NOW)
        self.assertEqual(self.timers.cancel('chat', 'laundry'), [{**again, 'state': 'cancelled'}], 'what was said inside a label')
        self.assertEqual(self.timers.cancel('chat', 'laundry'), [])
        self.assertEqual([timer['id'] for timer in self.timers.pending('chat', now=NOW)], [unlabelled['id']], 'an unlabelled timer matches no label')
        self.assertEqual([timer['id'] for timer in self.timers.pending('other', now=NOW)], [other['id']])

    def test_a_cancelled_timer_never_rings(self):
        self.timers.add('chat', 10, 'gone', now=NOW)
        self.timers.cancel('chat')
        self.assertEqual(self.timers.due(now=NOW + 100), [])
        self.assertEqual(self.timers.missed('chat', now=NOW + 100), [])

    def test_a_due_timer_is_handed_out_once(self):
        soon, later = self.timers.add('a', 10, 'soon', now=NOW), self.timers.add('b', 20, 'later', now=NOW)
        far = self.timers.add('a', 500, 'far', now=NOW)
        self.assertEqual(self.timers.due(now=NOW + 9.9), [])
        self.assertEqual(self.timers.due(now=NOW + 10), [{**soon, 'state': 'fired', 'fired': NOW + 10}], 'due at its second, of whatever scope')
        self.assertEqual(self.timers.due(now=NOW + 10), [], 'asked again: nothing')
        self.assertEqual(self.timers.due(now=NOW + 400), [{**later, 'state': 'fired', 'fired': NOW + 400}])
        self.assertEqual(self.timers.due(now=NOW + 400), [])
        self.assertEqual([timer['id'] for timer in self.timers.pending(now=NOW + 400)], [far['id']])
        self.assertEqual(self.timers.cancel('a', soon['id']), [], 'a timer that rang can no longer be cancelled')

    def test_several_due_at_once_come_soonest_first(self):
        ids = [self.timers.add('chat', seconds, now=NOW)['id'] for seconds in (30, 10, 20)]
        self.assertEqual([timer['id'] for timer in self.timers.due(now=NOW + 60)], [ids[1], ids[2], ids[0]])

    def test_missed_timers_wait_until_told(self):
        rang, other = self.timers.add('chat', 60, 'oven', now=NOW), self.timers.add('elsewhere', 60, now=NOW)
        still = self.timers.add('chat', 9000, 'later', now=NOW)
        self.assertEqual(self.timers.missed('chat', now=NOW + 100), [], 'not rung yet: not missed')
        self.timers.due(now=NOW + 100)
        expected = [{**rang, 'state': 'fired', 'fired': NOW + 100}]
        self.assertEqual(self.timers.missed('chat', now=NOW + 200), expected)
        self.assertEqual(self.timers.missed('chat', now=NOW + 300), expected, 'asking does not settle it')
        self.assertEqual([timer['id'] for timer in self.timers.missed('elsewhere', now=NOW + 300)], [other['id']])
        self.assertIs(self.timers.told(rang['id'], now=NOW + 301), True)
        self.assertEqual(self.timers.missed('chat', now=NOW + 302), [])
        self.assertIs(self.timers.told(rang['id']), False, 'already told')
        self.assertIs(self.timers.told(still['id']), False, 'a pending timer cannot be told')
        self.assertIs(self.timers.told('no-such-id'), False)
        self.assertIs(self.timers.told(None), False)
        self.assertEqual([timer['id'] for timer in self.timers.missed('elsewhere', now=NOW + 302)], [other['id']], 'telling one leaves the others')

    def test_a_timer_that_rang_long_ago_is_no_longer_missed(self):
        old, recent = self.timers.add('chat', 60, 'old', now=NOW), self.timers.add('chat', 5000, 'recent', now=NOW)
        self.timers.due(now=NOW + 20000)                                  # both claimed late, after a long stop
        self.assertEqual([timer['label'] for timer in self.timers.missed('chat', now=NOW + 5000 + 7200)], ['recent'],
                         'the window counts from when the timer was due, not from when it was claimed')
        self.assertEqual([timer['label'] for timer in self.timers.missed('chat', now=NOW + 20000, window=30000)], ['old', 'recent'])
        self.assertEqual(self.timers.missed('chat', now=NOW + 20000, window=10), [])
        self.assertEqual([timer['id'] for timer in self.timers.missed('chat', now=NOW + 60 + 7200)], [old['id'], recent['id']])

    def test_only_its_own_table_is_made_and_touched(self):
        self.db.execute('CREATE TABLE turns(id TEXT PRIMARY KEY, content TEXT)')
        self.db.execute("INSERT INTO turns VALUES('t1','kept')")
        self.db.commit()
        before = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(before, [('turns',)], 'nothing is made before the first use')
        timer = self.timers.add('chat', 10, now=NOW)
        self.timers.due(now=NOW + 20)
        self.timers.told(timer['id'])
        tables = {name for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'turns', 'voice_timers'})
        self.assertEqual(self.db.execute('SELECT * FROM turns').fetchall(), [('t1', 'kept')])

    def test_old_finished_timers_are_forgotten_and_pending_ones_never(self):
        rang, cancelled = self.timers.add('chat', 10, 'rang', now=NOW), self.timers.add('chat', 10, 'cancelled', now=NOW)
        waiting = self.timers.add('chat', LONGEST, 'waiting', now=NOW)
        self.timers.cancel('chat', cancelled['id'])
        self.assertEqual([timer['id'] for timer in self.timers.due(now=NOW + 10)], [rang['id']])
        self.timers.add('chat', 10, now=NOW + 86400)                      # a day later: both are still on record
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_timers').fetchone()[0], 4)
        self.timers.add('chat', 10, now=NOW + 8 * 86400)                  # eight days later: only what is pending, and the new ones
        labels = {label for (label,) in self.db.execute('SELECT label FROM voice_timers')}
        self.assertEqual(labels, {'waiting', ''})
        self.assertIn(waiting['id'], {timer['id'] for timer in self.timers.pending('chat', now=NOW)})

    def test_rows_may_be_tuples_or_named_rows(self):
        self.db.row_factory = sqlite3.Row                                 # Convo's journal connection reads rows this way
        timer = self.timers.add('chat', 10, 'tea', now=NOW)
        self.assertEqual(self.timers.pending('chat', now=NOW), [{**timer, 'left': 10.0}])
        self.assertEqual(self.timers.due(now=NOW + 10), [{**timer, 'state': 'fired', 'fired': NOW + 10}])

    def test_a_database_that_cannot_be_used_is_one_plain_sentence(self):
        self.db.close()
        for call in (lambda: self.timers.add('chat', 10), lambda: self.timers.pending(), lambda: self.timers.due(),
                     lambda: self.timers.cancel('chat'), lambda: self.timers.missed('chat'), lambda: self.timers.told('x')):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertEqual(str(caught.exception), UNAVAILABLE)
            self.assertIsInstance(caught.exception.__cause__, sqlite3.Error, 'the database error is kept for the log, not for the user')


class Shared(unittest.TestCase):
    """The journal's own connection handed in, together with the lock that guards it."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE turns(id TEXT PRIMARY KEY)')
        self.lock = threading.RLock()
        self.timers = Timers(lambda: self.db, lock=self.lock)

    def test_a_call_inside_the_owners_transaction_joins_it(self):
        self.timers.pending()                                             # the table exists before the transaction below
        with self.lock:
            self.db.execute("INSERT INTO turns VALUES('open')")           # the owner's transaction is open from here
            self.assertTrue(self.db.in_transaction)
            timer = self.timers.add('chat', 10, now=NOW)
            self.assertTrue(self.db.in_transaction, "the timer did not commit the owner's work")
            self.db.rollback()                                            # the owner gives up: its work and the timer go together
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0], 0)
        self.assertEqual(self.timers.pending('chat', now=NOW), [])
        self.assertNotIn(timer['id'], [row[0] for row in self.db.execute('SELECT id FROM voice_timers')])

    def test_a_table_first_made_inside_a_transaction_that_was_undone_is_made_again(self):
        with self.lock:
            self.db.execute("INSERT INTO turns VALUES('open')")
            self.timers.add('chat', 10, now=NOW)                          # first use: the table is made inside the owner's transaction
            self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='voice_timers'").fetchone()[0], 0)
        timer = self.timers.add('chat', 10, now=NOW)
        self.assertEqual(self.timers.pending('chat', now=NOW), [{**timer, 'left': 10.0}])

    def test_outside_a_transaction_each_change_is_committed_at_once(self):
        self.timers.add('chat', 10, now=NOW)
        self.assertFalse(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(len(self.timers.pending('chat', now=NOW)), 1)

    def test_the_given_lock_is_held_for_every_call(self):
        held = []

        class Watched:
            def __enter__(inner):
                held.append('in')
                return self.lock.__enter__()

            def __exit__(inner, *error):
                held.append('out')
                return self.lock.__exit__(*error)
        timers = Timers(lambda: self.db, lock=Watched())
        timer = timers.add('chat', 10, now=NOW)
        timers.pending()
        timers.due(now=NOW + 20)
        timers.missed('chat', now=NOW + 20)
        timers.told(timer['id'])
        timers.cancel('chat')
        self.assertEqual(held, ['in', 'out'] * 6)


class OnDisk(unittest.TestCase):
    """A database file, opened by several connections the way the journal is."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, 'journal.sqlite3')
        self.opened = []
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.close)
        first = sqlite3.connect(self.path)
        first.execute('PRAGMA journal_mode=WAL')                          # as Convo's journal is: the file keeps this mode
        first.close()

    def close(self):
        for db in self.opened:
            db.close()
        self.opened.clear()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.opened.append(db)
        return db

    def test_two_connections_hand_a_timer_out_once(self):
        one, two = Timers(self.connect), Timers(self.connect)
        first, second = one.add('chat', 10, 'first', now=NOW), two.add('chat', 20, 'second', now=NOW)
        self.assertEqual([timer['id'] for timer in two.pending('chat', now=NOW)], [first['id'], second['id']], 'each sees what the other set')
        self.assertEqual([timer['id'] for timer in one.due(now=NOW + 15)], [first['id']])
        self.assertEqual(two.due(now=NOW + 15), [], 'the other connection gets nothing for the same timer')
        self.assertEqual([timer['id'] for timer in two.due(now=NOW + 25)], [second['id']])
        self.assertEqual(one.due(now=NOW + 25), [])
        self.assertEqual([timer['id'] for timer in one.missed('chat', now=NOW + 30)], [first['id'], second['id']])
        self.assertIs(two.told(first['id']), True)
        self.assertEqual([timer['id'] for timer in one.missed('chat', now=NOW + 30)], [second['id']])
        self.assertEqual(len(self.opened), 2, 'one connection each, asked for once')

    def test_timers_survive_closing_and_reopening_the_database(self):
        before = Timers(self.connect)
        due_soon, due_later = before.add('chat', 60, 'soon', now=NOW), before.add('chat', 3600, 'later', now=NOW)
        rang = before.add('chat', 5, 'rang before the stop', now=NOW)
        self.assertEqual([timer['id'] for timer in before.due(now=NOW + 6)], [rang['id']])
        self.close()                                                      # the process stops here

        after = Timers(self.connect)
        self.assertEqual([timer['id'] for timer in after.pending('chat', now=NOW + 600)], [due_soon['id'], due_later['id']])
        self.assertEqual([timer['label'] for timer in after.missed('chat', now=NOW + 600)], ['rang before the stop'],
                         'one that rang and was never said is still owed')
        self.assertEqual([timer['id'] for timer in after.due(now=NOW + 600)], [due_soon['id']], 'due while everything was stopped: handed out now')
        self.assertEqual(after.due(now=NOW + 600), [], 'and only once')
        self.close()

        again = Timers(self.connect)
        self.assertEqual(again.due(now=NOW + 700), [], 'a restart does not ring it a second time')
        self.assertEqual([timer['label'] for timer in again.missed('chat', now=NOW + 700)], ['rang before the stop', 'soon'])
        self.assertEqual([timer['id'] for timer in again.pending('chat', now=NOW + 700)], [due_later['id']])

    def test_many_threads_asking_at_once_each_timer_goes_to_exactly_one(self):
        setter = Timers(self.connect)
        ids = {setter.add('scope-%d' % (number % 7), 1 + number % 5, now=NOW)['id'] for number in range(60)}
        shared, start, claimed, failures = Timers(self.connect), threading.Barrier(12), [], []

        def ask(timers):
            try:
                start.wait(5)
                for _ in range(3):
                    claimed.extend(timer['id'] for timer in timers.due(now=NOW + 10))
            except Exception as error:      # reported by the assertion below, not lost in a thread
                failures.append(error)
        # Six threads with a Timers of their own and six sharing one (which opens a connection for each of them).
        threads = [threading.Thread(target=ask, args=(Timers(self.connect) if number < 6 else shared,)) for number in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(failures, [])
        self.assertEqual(len(claimed), 60, 'no timer was handed out twice')
        self.assertEqual(set(claimed), ids, 'and none was lost')
        self.assertEqual(setter.pending(now=NOW + 10), [])

    def test_the_bound_holds_when_many_set_timers_at_once(self):
        start, added, refused = threading.Barrier(10), [], []

        def add(timers):
            start.wait(5)
            for _ in range(5):
                try:
                    added.append(timers.add('chat', 60, now=NOW)['id'])
                except ValueError as error:
                    refused.append(str(error))
        threads = [threading.Thread(target=add, args=(Timers(self.connect),)) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual((len(added), len(set(added)), set(refused)), (MOST, MOST, {TOO_MANY}))
        self.assertEqual(len(refused), 30)
        self.assertEqual(len(Timers(self.connect).pending('chat', now=NOW)), MOST)

    def test_one_object_used_from_another_thread_opens_its_own_connection(self):
        bound = []

        def connect():
            bound.append(sqlite3.connect(self.path, timeout=10))          # usable only in the thread that opened it
            return bound[-1]
        timers = Timers(connect)
        timer = timers.add('chat', 10, now=NOW)
        seen = []

        def elsewhere():
            seen.append(timers.pending('chat', now=NOW))
            seen.append(timers.pending('chat', now=NOW + 1))
            bound[-1].close()                                             # closed where it was opened
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join(10)
        self.assertEqual(seen, [[{**timer, 'left': 10.0}], [{**timer, 'left': 9.0}]])
        self.assertEqual(len(bound), 2, 'one connection for each thread, asked for once')
        bound[0].close()


if __name__ == '__main__':
    unittest.main()
