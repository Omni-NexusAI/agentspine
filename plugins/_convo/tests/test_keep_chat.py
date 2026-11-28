"""A chat used by voice alone is still there after a restart (Host.keep, live 2026-11-25): Agent Zero saves a chat only when its own agent
has worked in it. And what was heard in it stays in the model's history (convo_history) when the user's words were not captured."""
import asyncio
import datetime
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from .behavior_harness import SPEAK, script
    from .test_behavior_corpus import Scenario
else:
    from behavior_harness import SPEAK, script
    from test_behavior_corpus import Scenario

host = importlib.import_module('plugins._convo.helpers.convo_host')
history = importlib.import_module('plugins._convo.helpers.convo_history')
NOW = datetime.datetime(2026, 11, 25, 1, 15, tzinfo=datetime.timezone.utc)


class Chat:
    def __init__(self, ident='chat', name=None, running=False, kind='user'):
        self.id, self.name, self.running, self.agent0 = ident, name, running, object()
        self.type, self.data, self.last_message = types.SimpleNamespace(value=kind), {}, None

    def is_running(self):
        return self.running


class Keep(unittest.TestCase):
    """Against stand-ins shaped like Agent Zero's `AgentContext` and `persist_chat` (v2.11)."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder, self.chats, self.saved, self.removed, self.dirty = folder.name, {}, [], [], []
        self.naming, self.while_saving = {'automatic_naming': True}, None
        registry = types.SimpleNamespace(get=lambda ident: self.chats.get(ident))

        def save(context):
            Path(self.folder, context.id).mkdir(exist_ok=True)
            Path(self.folder, context.id, 'chat.json').write_text('{}')
            self.saved.append((context.id, context.name, context.last_message))
            if self.while_saving:
                self.while_saving()
        persist = types.SimpleNamespace(get_chat_folder_path=lambda ident: os.path.join(self.folder, ident), CHAT_FILE_NAME='chat.json',
                                        save_tmp_chat=save, remove_chat=self.removed.append)

        def plugin_config(name, agent=None):
            if isinstance(self.naming, Exception):
                raise self.naming
            return self.naming
        zone = types.SimpleNamespace(now=lambda: NOW)
        modules = {'agent': types.SimpleNamespace(AgentContext=registry),
                   'helpers.persist_chat': persist, 'helpers.plugins': types.SimpleNamespace(get_plugin_config=plugin_config),
                   'helpers.localization': types.SimpleNamespace(Localization=types.SimpleNamespace(get=lambda: zone)),
                   'helpers.state_monitor_integration': types.SimpleNamespace(mark_dirty_all=lambda reason='': self.dirty.append(reason))}
        package = types.ModuleType('helpers')
        for name, module in modules.items():
            if name.startswith('helpers.'):
                setattr(package, name.split('.')[1], module)
        patcher = patch.dict(sys.modules, {'helpers': package, **modules})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = host.Host()

    def add(self, **fields):
        chat = Chat(**fields)
        self.chats[chat.id] = chat
        return chat

    def test_a_chat_only_voice_was_used_in_is_saved_and_later_named(self):
        chat = self.add()
        self.assertTrue(self.host.keep('chat'))                                    # the first turn: nothing captured yet
        self.assertEqual(self.saved, [('chat', None, NOW)])
        self.assertFalse(self.host.keep('chat'))                                   # nothing new to save
        self.assertTrue(self.host.keep('chat', 'Which persona profile is this?'))
        self.assertEqual((chat.name, self.saved[-1][1], self.dirty), ('Which persona profile is this?',) * 2 + (['convo.keep'],))
        self.assertFalse(self.host.keep('chat', 'Something else was said first'))
        self.assertEqual(len(self.saved), 2)

    def test_stopping_voice_saves_when_the_chat_was_last_used(self):
        chat = self.add(name='Mine')
        self.host.keep('chat')
        chat.last_message = None
        self.assertTrue(self.host.keep('chat', 'hello there', final=True))
        self.assertEqual((self.saved[-1], chat.name), (('chat', 'Mine', NOW), 'Mine'))

    def test_a_chat_its_agent_is_working_in_is_left_to_agent_zero(self):
        self.add(running=True)
        for final in (False, True):
            self.assertFalse(self.host.keep('chat', 'words to name it', final=final))
        self.assertEqual((self.saved, self.chats['chat'].name), ([], None))

    def test_a_removed_chat_is_never_written_back(self):
        self.assertFalse(self.host.keep('gone', 'words', final=True))
        self.add()
        self.while_saving = self.chats.clear                                       # removed while it was being written
        self.assertFalse(self.host.keep('chat'))
        self.assertEqual(self.removed, ['chat'])

    def test_a_chat_replaced_meanwhile_is_not_removed(self):
        self.add()
        self.while_saving = lambda: self.chats.update(chat=Chat())
        self.assertTrue(self.host.keep('chat'))
        self.assertEqual(self.removed, [])

    def test_the_hidden_voice_context_is_never_saved(self):
        self.add(kind='background')
        self.assertFalse(self.host.keep('chat', 'words', final=True))
        self.assertEqual(self.saved, [])

    def test_it_is_not_named_where_the_user_turned_automatic_names_off(self):
        self.add()
        self.naming = {'automatic_naming': False}
        self.assertTrue(self.host.keep('chat', 'Which persona profile is this?'))
        self.assertEqual(self.saved, [('chat', None, NOW)])
        self.add(ident='other')
        self.naming = RuntimeError('no such plugin')
        self.host.keep('other', 'Which persona profile is this?')
        self.assertEqual(self.chats['other'].name, 'Which persona profile is this?')

    def test_a_name_is_one_short_line_cut_between_words(self):
        for words, title in (('Okay, um, I am looking at... installing the Nemotron 3.5 Bolt.', 'Okay, um, I am looking at…'),
                             ('  "hello\n there" ', 'hello there'), ('', ''), ('...', ''), ('a' * 60, 'a' * 39 + '…')):
            self.assertEqual(host.chat_title(words), title)
            self.assertLessEqual(len(host.chat_title(words)), host.CHAT_NAME_LIMIT)


class Voice(Scenario):
    def kept(self, scenario, **fields):
        async def body(h):
            kept = []
            h.host.keep = lambda target, words='', final=False: kept.append((target, words, final))
            extra = await scenario(h, kept)

            async def done():
                await asyncio.gather(*h.handler._keeping) if getattr(h.handler, '_keeping', None) else None
            await done()
            return list(kept), extra                 # a snapshot: closing the session keeps the chat once more
        return self.play(body, **fields)[0]

    def test_every_answered_turn_keeps_its_chat_and_the_first_words_name_it(self):
        async def scenario(h, kept):
            await h.candidate('one', h.audio('one'), final=True)
            await h.settle()
            h.model_deltas = [('USER: And in Osaka?\nWarmer, sir.', .2)]
            await h.until(h.now() + 2)
            await h.candidate('two', h.audio('two'), final=True)
            await h.settle()
        kept, _ = self.kept(scenario, classifier={'one': script(SPEAK, .2), 'two': script(SPEAK, .2)},
                            model_deltas=[('USER: How warm is it in Tokyo?\nMild, sir.', .2)])
        self.assertEqual(kept, [('a', '', False), ('a', 'How warm is it in Tokyo?', False)])

    def test_a_declined_turn_never_names_the_chat(self):
        async def scenario(h, kept):
            await h.candidate('one', h.audio('one'), final=True)
            await h.settle()
            return h.store.first_words('a')
        _, words = self.kept(scenario, classifier={'one': script(SPEAK, .2)},
                             model_deltas=[('USER: This is only a test.\n', .1), ('[SILENT] asked not to', .1)])
        self.assertEqual(words, '')

    def test_a_failure_to_save_never_reaches_the_reply(self):
        async def scenario(h):
            def broken(target, words='', final=False):
                raise OSError('disk full')
            h.host.keep = broken
            await h.candidate('one', h.audio('one'), final=True)
            await h.settle()
            await asyncio.gather(*h.handler._keeping)
            return len(h.audible('one'))
        with self.assertLogs('_convo', 'WARNING'):
            heard, _ = self.play(scenario, classifier={'one': script(SPEAK, .2)}, model_deltas=[('USER: Hello there.\nHello, sir.', .2)])
        self.assertGreater(heard, 0)

    def test_stopping_saves_only_when_something_was_said(self):
        async def stop(h, kept):
            await h.handler._end('browser')

        async def said(h, kept):
            await h.candidate('one', h.audio('one'), final=True)
            await h.settle()
            await h.handler._end('browser')
        self.assertEqual(self.kept(stop)[0], [])
        kept = self.kept(said, classifier={'one': script(SPEAK, .2)}, model_deltas=[('USER: Hello there.\nHello, sir.', .2)])[0]
        self.assertEqual(kept[-1], ('a', 'Hello there.', True))


class HeardAnswers(unittest.TestCase):
    SEARCH = {'name': 'web_search', 'argument': 'Bolt E4B draft model', 'at': 'Tuesday, 24 November 2026, 8:17 PM', 'result': 'DFlash draft head', 'ok': True}

    def test_a_heard_answer_from_a_look_up_stays_when_the_users_words_were_lost(self):
        turns = [{'transcript': 'Do you recommend it?', 'assistant': 'The Q4 one.'},
                 {'assistant': 'The drafter is DFlash, sir.', 'interrupted': True, 'tools': [self.SEARCH]},
                 {'transcript': 'Is the drafter compatible?', 'assistant': 'Yes.'}]
        for native in (False, True):
            found = history.messages(turns, audio_input=True, tool_messages=native)
            self.assertEqual([one['role'] for one in found], ['user', 'assistant'] * 3)
            self.assertIn('Bolt E4B draft model', found[2]['content'])
            self.assertIn('untrusted data', found[2]['content'])
            self.assertEqual(found[3]['content'], 'The drafter is DFlash, sir.' + history.INTERRUPTED)
            self.assertNotIn('captured', repr(found))

    def test_a_look_up_nothing_was_heard_of_and_an_answer_to_no_words_stay_left_out(self):
        self.assertEqual(history.messages([{'tools': [self.SEARCH]}, {'assistant': 'The audio was unclear.'}], audio_input=True), [])

    def test_with_words_the_tool_use_is_as_before(self):
        found = history.messages([{'transcript': 'Which drafter?', 'assistant': 'DFlash.', 'tools': [self.SEARCH]}], audio_input=True, tool_messages=True)
        self.assertEqual([one['role'] for one in found], ['user', 'assistant', 'tool', 'assistant'])


if __name__ == '__main__':
    unittest.main()
