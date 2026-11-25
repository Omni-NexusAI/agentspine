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
        self.assertEqual(self.store.job(job['id'])['prompt'], 'Write the weekly report')      # the task itself is what was asked for
        again = self.store.submit(self.session['id'], 'browser', 0, 't', 'k', 'Write the weekly report')
        self.assertEqual(again['id'], job['id'])                                              # the same request again is the same task
        self.assertEqual(self.kinds('job.steered'), [('job.steered', 'added')] * 2)

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

    def test_an_older_convo_reads_the_journal_and_ignores_the_changes(self):
        d = self.make()
        self.host.busy = True
        self.job()
        d.steer(self.store.jobs('a')[0]['id'], 'Use euros')
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT prompt FROM jobs')], ['Write the weekly report'])
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
