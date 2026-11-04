"""The panel reads history newest-first in pages; the polling path is unchanged."""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from plugins._convo.helpers.convo_store import Store


class HistoryPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'db'); self.addCleanup(self.store.close)
        session = self.store.start('browser', 'chat-a')
        for number in range(130):
            self.store.accept_turn(session['id'], 'browser', session['epoch'], 'turn-%03d' % number, {'input': 'audio', 'note': 'n%d' % number})
        self.store.stop(session['id'], 'browser')                      # only one voice session may be active at a time
        other = self.store.start('other', 'chat-b')
        self.store.accept_turn(other['id'], 'other', other['epoch'], 'elsewhere', {'input': 'audio'})
        self.all = self.store.events('chat-a', 0, 500)

    def test_tail_returns_the_newest_events_oldest_first(self):
        page = self.store.events_tail('chat-a', 60)
        sequences = [event['seq'] for event in page['events']]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(sequences, [event['seq'] for event in self.all[-60:]])
        self.assertTrue(page['more'])
        self.assertIsInstance(page['events'][0]['payload'], dict)

    def test_older_pages_walk_back_without_gaps_or_overlap_and_stop(self):
        seen, before, pages = [], None, 0
        while True:
            page = self.store.events_tail('chat-a', 60, before)
            pages += 1
            seen = [event['seq'] for event in page['events']] + seen
            if not page['more']:
                break
            before = page['events'][0]['seq']
        self.assertEqual(seen, [event['seq'] for event in self.all])
        self.assertEqual(pages, -(-len(self.all) // 60))

    def test_an_exact_fit_reports_nothing_earlier(self):
        small = self.store.events('chat-b', 0, 500)
        self.assertEqual(len(small), 1)
        self.assertEqual(self.store.events_tail('chat-b', 1), {'events': small, 'more': False})
        page = self.store.events_tail('chat-a', len(self.all) - 1 if len(self.all) <= 200 else 200)
        self.assertTrue(page['more'])

    def test_limit_is_bounded_and_input_is_validated(self):
        self.assertEqual(len(self.store.events_tail('chat-a', 0)['events']), 1)
        self.assertEqual(len(self.store.events_tail('chat-a', 9999)['events']), min(200, len(self.all)))
        with self.assertRaises(ValueError):
            self.store.events_tail('chat-a', 10, 'soon')

    def test_other_chats_are_never_mixed_in(self):
        page = self.store.events_tail('chat-b', 60)
        self.assertEqual([event['turn'] for event in page['events']], ['elsewhere'])
        self.assertFalse(page['more'])
        self.assertEqual(self.store.events_tail('chat-c')['events'], [])

    def test_history_page_keeps_the_polling_contract(self):
        newest = self.all[-1]['seq']
        self.assertEqual(self.store.history_page('chat-a', after=newest), {'events': [], 'more': False})
        self.assertEqual([e['seq'] for e in self.store.history_page('chat-a', after=newest - 2)['events']], [newest - 1, newest])
        self.assertEqual(len(self.store.history_page('chat-a')['events']), min(200, len(self.all)))   # polling keeps its oldest-first window
        paged = self.store.history_page('chat-a', tail=5, before=newest)
        self.assertEqual([e['seq'] for e in paged['events']], [e['seq'] for e in self.all[-6:-1]])


if __name__ == '__main__':
    unittest.main()
