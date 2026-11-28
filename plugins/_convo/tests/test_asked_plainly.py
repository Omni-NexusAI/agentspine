"""Live 2026-11-25. A plain question about "the voice agent" was silenced by the conversation model although the classifier was sure
a request was made (.99), because only "was it said to me" (.59) was looked at: the turn is asked about once more (Realtime
`asked_plainly`). And "Do it." after an offer was answered about something else: the assistant's last heard words now stand last in
the request, the same for a held reply and the admitted turn (Realtime._last_said)."""
import unittest

if __package__:
    from .behavior_harness import script, typed_decision
    from .test_belongs import Base, conversation, marks
else:
    from behavior_harness import script, typed_decision
    from test_belongs import Base, conversation, marks

SILENT = [("USER: do you know if the voice agent has a global chat\nLANGUAGE: English\n[SILENT]", .1)]
WORDLESS = [("[SILENT]", .1)]
REQUEST = typed_decision(explicit=.99, share=.6, context_note="")          # sure of a request, not sure it was said to the assistant
REMARK = typed_decision(explicit=.2, share=.6, context_note="")


class AskedPlainly(Base):
    def decline(self, reading, deltas):
        async def body(h):
            await conversation(h)
            before = len(h.answers)
            h.model_deltas = deltas
            await h.candidate("doubt", h.audio("doubt"), final=True)
            await h.settle()
            return h.answers[before:], [m[2] for m in marks(h, "model.silent_confirm")]
        return self.play(body, also={"doubt": script(reading, .2)}, config={"speculative_reply": False})

    def test_a_request_the_model_silenced_is_asked_about_once_more(self):
        requests, confirmed = self.decline(REQUEST, SILENT)
        self.assertEqual((len(requests), confirmed), (2, [{"why": "request"}]))
        self.assertIn("You chose [SILENT], but this was said to you", requests[1]["system"])
        self.assertNotIn("You chose [SILENT]", requests[0]["system"])

    def test_a_remark_it_silenced_stays_silent(self):
        requests, confirmed = self.decline(REMARK, SILENT)
        self.assertEqual((len(requests), confirmed), (1, []))

    def test_silence_without_any_words_stays_silent(self):
        requests, confirmed = self.decline(REQUEST, WORDLESS)
        self.assertEqual((len(requests), confirmed), (1, []))


class LastSaid(Base):
    def test_the_last_heard_words_stand_last_and_the_held_reply_is_still_used(self):
        async def body(h):
            await conversation(h, reply="I can open the settings for you if you like.")
            before = len(h.answers)
            h.model_deltas = [("USER: do it\nLANGUAGE: English\nOpening them now.", .1)]
            await h.candidate("ask", h.audio("ask"), final=True)
            await h.settle()
            return h.answers[before:], [m[2] for m in marks(h, "spec.discard")], [m for m in marks(h, "spec.promote")]
        requests, discarded, promoted = self.play(body, config={"speculative_reply": True})
        self.assertEqual(discarded, [])
        self.assertTrue(promoted)
        for request in requests:
            late = request["system"].rsplit("The last thing the user heard you say: ", 1)
            self.assertEqual(len(late), 2)
            self.assertTrue(late[1].startswith('"I can open the settings for you if you like."'), late[1][:80])
            self.assertNotIn("\n", late[1])                         # nothing follows it

    def test_nothing_is_added_before_anything_was_heard(self):
        async def body(h):
            await h.candidate("first", h.audio("first"), final=True)
            await h.settle()
            return h.answers
        for request in self.play(body):
            self.assertNotIn("The last thing the user heard you say", request["system"])


if __name__ == "__main__":
    unittest.main()
