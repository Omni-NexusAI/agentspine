"""Changes to work that is going on (`steer_task`, the panel's Steer, `Dispatcher.steer`): what reaches the chat's agent, what is journalled,
and what becomes of a change that came too late. The Steering plugin itself is tested in plugins/steering; here it is scripted."""
import importlib
import sqlite3
import tempfile
import types
import unittest
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .behavior_harness import DELEGATE, SPEAK, SPEAK_PLAIN, script, typed_decision
    from .test_behavior_corpus import DELEGATE_TASK
    from .test_tool_rounds import Loop, call, result_of
else:
    from behavior_harness import DELEGATE, SPEAK, SPEAK_PLAIN, script, typed_decision
    from test_behavior_corpus import DELEGATE_TASK
    from test_tool_rounds import Loop, call, result_of

from plugins._convo.helpers import convo_steering
from plugins._convo.helpers.convo_host import Dispatcher, Host
from plugins._convo.helpers.convo_store import Store

convo_host = importlib.import_module('plugins._convo.helpers.convo_host')
convo_store = importlib.import_module('plugins._convo.helpers.convo_store')
tools = importlib.import_module('plugins._convo.helpers.convo_tools')
voice_tools = importlib.import_module('plugins._convo.helpers.convo_voice_tools')


class ScriptedHost:
    """A chat's agent: `states` is what the Steering plugin says about each change ('' = it was never kept)."""
    kept = 0

    def __init__(self, plugin=True):
        self.plugin, self.busy, self.futures, self.begun, self.passed, self.forgotten, self.states = plugin, False, {}, [], [], [], {}

    def available(self, target): return not self.busy
    def at_work(self, target): return self.busy
    def log(self, *args): pass
    def cancel(self, job, future): future.cancel()

    def begin(self, job):
        self.begun.append(job['prompt'])
        self.futures[job['target']] = future = Future()
        return future

    def _keep(self, text):
        self.passed.append(text)
        if not self.plugin:
            return ''
        ScriptedHost.kept += 1                      # ids are unique, as the plugin's are
        item = 'item%d' % ScriptedHost.kept
        self.states[item] = 'waiting'
        return item

    def steer(self, job, future, text):
        if future.done():
            raise ValueError('Target is no longer executing this job')
        return self._keep(text)

    def steer_chat(self, target, text): return self._keep(text) if self.busy else ''
    def steer_state(self, target, item): return self.states.get(item, '')
    def steer_forget(self, target, item): self.forgotten.append(item)


class Dispatching(unittest.TestCase):
    def setUp(self):
        ScriptedHost.kept = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'jobs.db')
        self.session = self.store.start('browser', 'a')
        self.store.accept_turn(self.session['id'], 'browser', 0, 't', {'transcript': 'Please do it'})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, plugin=True):
        self.host = ScriptedHost(plugin)
        self.dispatcher = Dispatcher(self.store, self.host)
        self.dispatcher.start = lambda: None                        # driven by hand
        return self.dispatcher

    def job(self, text='Write the weekly report', key='k', target=None):
        return self.store.submit(self.session['id'], 'browser', 0, 't', key, text, target=target)

    def kinds(self, prefix='job.', target='a'):
        return [(e['kind'], e['payload'].get('how')) for e in self.store.events(target) if e['kind'].startswith(prefix)]

    def changes(self):
        return [(c['text'], c['status']) for c in self.store.steers()]

    def test_a_task_that_has_not_started_gets_the_change_into_its_text(self):
        d = self.make()
        self.host.busy = True
        job = self.job()
        self.assertEqual(d.steer(job['id'], '  Include the costs  '), 'added')
        self.assertEqual(d.steer(job['id'], 'And keep it to one page'), 'added')
        self.assertEqual(self.host.passed, [])
        self.host.busy = False
        d.tick()
        self.assertEqual(self.host.begun, ['Write the weekly report\n\nAdded by the user before the task started:\nInclude the costs'
                                           '\n\nAdded by the user before the task started:\nAnd keep it to one page'])
        again = self.store.submit(self.session['id'], 'browser', 0, 't', 'k', 'Write the weekly report')
        self.assertEqual(again['id'], job['id'])                                              # the same request again is the same task
        with self.assertRaises(ValueError):
            self.store.submit(self.session['id'], 'browser', 0, 't', 'k', 'Write the weekly')
        self.assertEqual(self.kinds('job.steered'), [('job.steered', 'added')] * 2)

    def test_a_change_that_came_too_late_never_goes_into_the_next_tasks_run(self):
        d = self.make()
        first = self.job('Task 1')
        d.tick()
        self.host.busy = True
        second = self.job('Task 2', 'k2')
        self.assertEqual(d.steer(first['id'], 'Use euros'), 'waiting')
        self.host.futures['a'].set_result('The report')                                       # it was writing its final answer
        self.host.busy = False
        with patch.object(convo_host.time, 'monotonic', side_effect=lambda: self.clock):
            self.clock = 100.0
            d.tick()
            self.assertEqual((self.host.begun, self.store.job(second['id'])['status']), (['Task 1'], 'queued'))      # not before that is settled
            self.host.states = dict.fromkeys(self.host.states, 'undelivered')
            d.tick()
            self.assertEqual(self.changes(), [('Use euros', 'follow_up')])
            d.tick()
        self.assertEqual(self.host.begun[:2], ['Task 1', 'Task 2'])
        self.assertIn('Use euros', [job['prompt'] for job in self.store.jobs('a')])           # a task of its own

    def test_nor_does_one_to_work_the_user_began_however_long_the_task_waited_behind_it(self):
        d = self.make()
        self.host.busy = True                                                                 # the chat's own work, begun by the user
        self.assertEqual(d.steer_chat(self.session['id'], 't', 'a', 'use euros'), 'waiting')
        second = self.job('Task 2', 'k2')
        with patch.object(convo_host.time, 'monotonic', side_effect=lambda: self.clock):
            for self.clock in (100.0, 110.0, 130.0):                                          # it writes a long final answer
                d.tick()
            self.host.busy, self.clock = False, 130.25                                        # and ends without another step
            d.tick()
            self.assertEqual(self.store.job(second['id'])['status'], 'queued')
            self.host.states = dict.fromkeys(self.host.states, 'undelivered')
            d.tick()
            self.assertEqual(self.changes(), [('use euros', 'follow_up')])

    def test_nothing_begins_again_after_the_user_stopped_it(self):
        d = self.make()
        self.host.busy = True
        d.steer_chat(self.session['id'], 't', 'a', 'and add the prices')
        self.host.busy = False
        self.host.states = dict.fromkeys(self.host.states, 'stopped')
        d.tick()
        d.tick()
        self.assertEqual((self.changes(), self.store.jobs('a'), self.host.begun), ([('and add the prices', 'dropped')], [], []))

    def test_a_change_that_is_never_settled_does_not_hold_the_chat_up_for_good(self):
        d = self.make()
        first = self.job('Task 1')
        d.tick()
        self.host.busy = True
        self.job('Task 2', 'k2')
        d.steer(first['id'], 'Use euros')
        self.host.futures['a'].set_result('The report')
        self.host.busy = False
        with patch.object(convo_host.time, 'monotonic', side_effect=lambda: self.clock):
            for self.clock, started in ((100.0, 1), (100.0 + convo_host.UNSETTLED_SECONDS - 1, 1), (100.0 + convo_host.UNSETTLED_SECONDS + 1, 2)):
                d.tick()
                self.assertEqual(len(self.host.begun), started, self.clock)

    def test_a_task_too_long_with_the_change_is_refused_and_unchanged(self):
        d = self.make()
        self.host.busy = True
        job = self.job('x' * 15000)
        with self.assertRaises(ValueError):
            d.steer(job['id'], 'y' * 1200)
        self.assertEqual(self.changes(), [])

    def test_a_running_task_gets_the_change_at_its_next_step(self):
        d = self.make()
        job = self.job()
        d.tick()
        self.assertEqual(d.steer(job['id'], 'Use euros'), 'waiting')
        self.assertEqual(self.host.passed, ['Use euros'])
        d.tick()
        self.assertEqual(self.changes(), [('Use euros', 'waiting')])
        self.host.states['item1'] = 'delivered'
        d.tick()
        self.assertEqual(self.changes(), [('Use euros', 'taken')])
        self.assertEqual(self.kinds('job.steer'), [('job.steered', 'waiting'), ('job.steer_taken', None)])
        self.host.futures['a'].set_result('Done')
        d.tick()
        self.assertEqual(self.store.job(job['id'])['status'], 'completed')
        self.assertEqual(len(self.store.jobs('a')), 1)

    def test_without_the_plugin_the_change_interrupts_and_says_so(self):
        d = self.make(plugin=False)
        job = self.job()
        d.tick()
        self.assertEqual(d.steer(job['id'], 'Use euros'), 'interrupted')
        self.assertEqual(self.changes(), [('Use euros', 'interrupted')])
        d.tick()
        self.assertEqual(d.steers, {})

    def test_a_change_too_late_for_its_run_becomes_a_task_of_its_own(self):
        d = self.make()
        job = self.job(target='b')                                                            # handed to another chat
        d.tick()
        d.steer(job['id'], 'And the weather')
        self.host.states['item1'] = 'undelivered'
        d.tick()                                                                              # its run's end is not seen yet
        self.assertEqual(self.changes(), [('And the weather', 'waiting')])
        self.host.futures['b'].set_result('Done')
        d.tick()
        self.assertEqual(self.changes(), [('And the weather', 'follow_up')])
        self.assertEqual(self.host.forgotten, ['item1'])                                      # it can never run twice
        jobs = self.store.jobs('a')
        self.assertEqual([(j['prompt'], j['status'], j['target']) for j in jobs],
                         [('Write the weekly report', 'completed', 'b'), ('And the weather', 'running', 'b')])
        follow = jobs[1]
        self.assertEqual((follow['session'], follow['turn'], self.store.scope_of(follow)), (self.session['id'], 't', 'a'))
        self.host.futures['b'].set_result('Sunny')
        d.tick()
        claimed = self.store.claim_notice(self.session['id'], 'browser', 0, follow['id'])
        self.assertEqual(claimed['result'], 'Sunny')                                          # and its answer is said

    def test_a_change_whose_task_was_stopped_or_failed_is_dropped(self):
        for end in ('cancel', 'fail'):
            with self.subTest(end):
                d = self.make()
                job = self.job(key='k' + end)
                d.tick()
                d.steer(job['id'], 'Use euros')
                self.host.states = {item: 'undelivered' for item in self.host.states}
                if end == 'cancel':
                    d.cancel(job['id'])
                else:
                    self.host.futures['a'].set_exception(RuntimeError('boom'))
                d.tick()
                self.assertEqual(self.changes()[-1], ('Use euros', 'dropped'))
                self.assertEqual([j['prompt'] for j in self.store.jobs('a') if j['prompt'] == 'Use euros'], [])
                self.assertEqual(self.host.begun, ['Write the weekly report'])

    def test_a_change_to_work_convo_did_not_start(self):
        d = self.make()
        self.assertEqual(d.steer_chat(self.session['id'], 't', 'a', 'Use euros'), '')         # nothing is going on
        self.assertEqual(self.changes(), [])
        self.host.busy = True
        self.assertEqual(d.steer_chat(self.session['id'], 't', 'a', 'Use euros'), 'waiting')
        self.host.states['item1'] = 'delivered'
        d.tick()
        self.assertEqual(self.changes(), [('Use euros', 'taken')])
        self.assertEqual(d.steer_chat(self.session['id'], 't', 'a', 'Too late'), 'waiting')
        self.host.states['item2'], self.host.busy = 'undelivered', False
        d.tick()
        self.assertEqual(self.changes()[-1], ('Too late', 'follow_up'))
        self.assertEqual([j['prompt'] for j in self.store.jobs('a')], ['Too late'])

    def test_the_run_of_a_task_in_that_chat_is_steered_as_that_task(self):
        d = self.make()
        job = self.job()
        d.tick()
        self.host.busy = True
        self.assertEqual(d.steer_chat(self.session['id'], 't', 'a', 'Use euros'), 'waiting')
        self.assertEqual(self.store.steers()[0]['job'], job['id'])

    def test_what_is_over_cannot_be_changed(self):
        d = self.make()
        job = self.job()
        d.tick()
        self.host.futures['a'].set_result('Done')
        with self.assertRaises(ValueError):
            d.steer(job['id'], 'too late')
        d.tick()
        with self.assertRaises(ValueError):
            d.steer(job['id'], 'too late')
        for text in ('', '   ', 'x' * 16001):
            with self.assertRaises(ValueError):
                d.steer(job['id'], text)
        self.assertEqual(self.changes(), [])

    def test_a_restart_no_longer_follows_a_waiting_change(self):
        d = self.make()
        job = self.job()
        d.tick()
        d.steer(job['id'], 'Use euros')
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.changes(), [('Use euros', 'dropped')])
        self.assertEqual(self.store.job(job['id'])['status'], 'uncertain')
        self.assertFalse(self.store.steer_status('item1', 'taken'))
        self.assertIsNone(self.store.follow_up('item1'))

    def test_an_older_convo_reads_the_journal_and_starts_the_task_with_its_change(self):
        d = self.make()
        self.host.busy = True
        self.job()
        d.steer(self.store.jobs('a')[0]['id'], 'Use euros')
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT prompt FROM jobs')], ['Write the weekly report' + convo_store.ADDED + 'Use euros'])
            self.assertEqual(len(db.execute('SELECT * FROM jobs').fetchone()), 8)             # the table an older Convo writes is unchanged


class Plugin(unittest.TestCase):
    def setUp(self):
        self.addCleanup(convo_steering._seen.update, {'at': None, 'module': None})
        convo_steering._seen.update(at=None, module=None)

    def test_it_is_absent_when_it_cannot_be_read_or_is_off(self):
        self.assertIsNone(convo_steering.module())                                            # no Agent Zero here
        self.assertFalse(convo_steering.available())
        self.assertEqual(convo_steering.steer(object(), 'x'), '')
        self.assertEqual(convo_steering.status(object(), 'x'), '')
        self.assertFalse(convo_steering.busy(object()))
        convo_steering.forget(object(), 'x')
        convo_steering._seen.update(at=None, module=None)
        plugins = types.SimpleNamespace(get_enabled_plugins=lambda agent: ['_convo'])
        with patch.dict('sys.modules', {'helpers': types.SimpleNamespace(plugins=plugins), 'helpers.plugins': plugins}):
            self.assertFalse(convo_steering.available())

    def test_a_failing_plugin_never_fails_convo(self):
        def broken(*args, **kwargs):
            raise RuntimeError('broken')
        rules = types.SimpleNamespace(busy=broken, status=broken, forget=broken)
        with patch.object(convo_steering, 'module', lambda: rules):
            self.assertEqual((convo_steering.busy(object()), convo_steering.status(object(), 'x')), (False, ''))
            convo_steering.forget(object(), 'x')

    def test_a_chat_whose_agent_works_unseen_is_not_free_for_a_task(self):
        context = types.SimpleNamespace(is_running=lambda: False)
        queue = types.SimpleNamespace(get_queue=lambda context: [])
        host = Host.__new__(Host)
        host.context = lambda target: context
        with patch.dict('sys.modules', {'helpers': types.SimpleNamespace(message_queue=queue), 'helpers.message_queue': queue}):
            self.assertTrue(host.available('a'))
            with patch.object(convo_steering, 'busy', lambda context: True):
                self.assertFalse(host.available('a'))
                self.assertTrue(host.at_work('a'))

    def test_the_host_steers_only_the_run_that_does_the_job(self):
        future = Future()
        sent = []
        context = types.SimpleNamespace(task=types.SimpleNamespace(_future=future), is_running=lambda: not future.done(),
                                        communicate=sent.append)
        host = Host.__new__(Host)
        host.context = lambda target: context
        agent = types.SimpleNamespace(UserMessage=lambda text: text)
        with patch.dict('sys.modules', {'agent': agent}):
            with patch.object(convo_steering, 'steer', lambda context, text: 'kept'):
                self.assertEqual(host.steer({'target': 'a'}, future, 'x'), 'kept')
            self.assertEqual(sent, [])
            self.assertEqual(host.steer({'target': 'a'}, future, 'x'), '')                    # without the plugin: Agent Zero's own
            self.assertEqual(sent, ['x'])
            with self.assertRaises(ValueError):
                host.steer({'target': 'a'}, Future(), 'x')


REPORT = 'The change was passed to the task "Write the weekly report". The work goes on and takes it in at its next step; do not say it is done.'


class Speaking(Loop):
    """The voice tool on the behaviour harness."""
    def steer(self, arguments, *, jobs=1, working=False, how='waiting', classifier=SPEAK, on=True, statuses=(), chats=(), **options):
        async def scenario(h):
            h.state['steering'] = on
            steered = []

            def steer(job, text, turn=None):
                steered.append(('task', text))
                if isinstance(how, Exception):
                    raise how
                return how
            h.service.dispatcher.steer = steer
            h.service.dispatcher.steer_chat = lambda session, turn, target, text: (steered.append((target, text)), how)[1]
            h.host.at_work = lambda target: working
            labels = ['Write the weekly report', 'Sort the holiday photos']
            for index in range(jobs):
                job = h.store.submit(h.session['id'], 'browser', h.ids()['epoch'], 't0', 'k%d' % index, labels[index])
                for expected, status in (statuses[index] if index < len(statuses) else ()):
                    h.store.transition(job['id'], expected, status, '')
            h.proposals = [call('steer_task', **arguments)]
            await self.ask(h, 't', 'ask')
            offered = 'steer_task' in (h.answers[0].get('tools') or [])
            return (result_of(h.answers[-1]) if h.answers[-1].get('tool_turn') else None), steered, offered, [p for _, p in h.events('job.steered')], h.state.get('pending')
        return self.play(scenario, classifier={'ask': script(classifier, .2)}, chats=list(chats), **options)

    def test_it_is_offered_only_with_the_plugin(self):
        self.assertTrue(self.steer({'change': 'Use euros'})[2])
        told, steered, offered, _, _ = self.steer({'change': 'Use euros'}, on=False)
        self.assertEqual((steered, offered), ([], False))
        self.assertNotIn('steer_task', tools.offered())
        self.assertIn('steer_task', tools.offered(steering=True))
        self.assertNotIn('steer_task', tools.offered(steering=True, mode='text'))             # small models: only once measured
        self.assertNotIn('steer_task', tools.offered(steering=True, groups={'tasks': False}))

    def test_the_only_task_is_the_one_meant(self):
        told, steered, _, events, _ = self.steer({'change': 'Use euros'})
        self.assertEqual(steered, [('task', 'Use euros')])
        self.assertEqual(told, tools.done('task change', REPORT))
        self.assertEqual([(e['how'], e['target']) for e in events], [('waiting', 'a')])

    def test_what_became_of_it_is_said_as_it_is(self):
        self.assertIn('was added to the task "Write the weekly report", which has not started yet', self.steer({'change': 'x'}, how='added')[0])
        self.assertIn('the step it was on was interrupted', self.steer({'change': 'x'}, how='interrupted')[0])
        self.assertIn('has just ended or is only just starting', self.steer({'change': 'x'}, how=ValueError('over'))[0])

    def test_of_several_tasks_the_words_choose_and_otherwise_the_user_is_asked(self):
        told, steered, _, _, _ = self.steer({'change': 'Use euros', 'what': 'the weekly report'}, jobs=2)
        self.assertEqual(steered, [('task', 'Use euros')])
        self.assertIn('Write the weekly report', told)
        for what in ({}, {'what': 'the laundry'}):
            told, steered, _, _, _ = self.steer({'change': 'Use euros', **what}, jobs=2)
            self.assertEqual(steered, [])
            self.assertIn('more than one task is going on', told)
            self.assertIn('Sort the holiday photos', told)

    def test_with_no_task_it_is_whatever_the_chat_is_doing(self):
        told, steered, _, _, _ = self.steer({'change': 'Use euros'}, jobs=0, working=True)
        self.assertEqual(steered, [('a', 'Use euros')])
        self.assertIn("passed to what the chat's agent is doing", told)

    def test_with_nothing_going_on_nothing_is_passed_and_new_work_is_suggested(self):
        told, steered, _, _, _ = self.steer({'change': 'Use euros'}, jobs=0)
        self.assertEqual(steered, [])
        self.assertIn('nothing is going on', told)
        self.assertIn('hand over new work instead', told)
        ended = [('queued', 'dispatching'), ('dispatching', 'running'), ('running', 'completed')]
        self.assertIn('nothing is going on', self.steer({'change': 'x'}, statuses=[ended])[0])

    def test_a_change_the_user_did_not_ask_for_is_asked_about_first(self):
        told, steered, _, _, pending = self.steer({'change': 'Use euros'}, classifier=typed_decision(explicit=.1), typed=True)
        self.assertEqual(steered, [])
        self.assertEqual(pending['name'], 'steer_task')
        self.assertIn('Nothing was started yet', told)

    def test_the_task_check_tells_what_became_of_a_change(self):
        async def scenario(h):
            await self.ask(h)                                                                 # hands a task over
            job = h.jobs()[0]
            h.store.steered(h.session['id'], 't', 'a', 'Use euros', 'waiting', job=job['id'], steer_id='s1')
            h.store.steer_status('s1', 'taken')
            h.proposals = [call('task_status')]
            await self.ask(h, 'u', 'next')
            return result_of(h.answers[-1])
        told = self.play(scenario, proposals=[DELEGATE_TASK], classifier={'ask': script(DELEGATE, .2)}, default=SPEAK_PLAIN)
        self.assertIn('Change from the user (taken in): Use euros', told)


if __name__ == '__main__':
    unittest.main()
