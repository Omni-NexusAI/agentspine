"""BP3e at the transport: only a provider that is still finishing a request it tracks is "busy" (wait); every other
refusal stays an ordinary error. The sidecar signals the tracked case with HTTP 409 and a `running`/`draining` state."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from plugins._convo.helpers import convo_transport as transport
from plugins._convo.helpers.convo_contract import SpeechBusy


def response(status, body=None, raw=None):
    request = httpx.Request('POST', 'http://sidecar/speech')
    return httpx.Response(status, content=raw, request=request) if raw is not None else httpx.Response(status, json=body, request=request)


def run(coroutine):
    return asyncio.run(coroutine)


class StatusMapping(unittest.TestCase):
    def test_a_tracked_running_or_draining_request_is_busy_and_names_its_state(self):
        for state in ('running', 'draining'):
            with self.assertRaises(SpeechBusy) as raised:
                run(transport.speech_status(response(409, {'detail': {'state': state, 'blocked': True}})))
            self.assertEqual(raised.exception.state, state)

    def test_everything_else_is_an_ordinary_error(self):
        cases = {
            'uncertain outcome needs the operator, not a wait': response(409, {'detail': {'state': 'uncertain', 'blocked': True}}),
            'unknown state': response(409, {'detail': {'state': 'idle'}}),
            'no state at all': response(409, {'detail': {}}),
            'detail is text': response(409, {'detail': 'running'}),
            'body is a list': response(409, ['running']),
            'body is not JSON': response(409, raw=b'<html>conflict</html>'),
            'empty body': response(409, raw=b''),
            'wrong status, right state': response(500, {'detail': {'state': 'running'}}),
            'rejected request': response(400, {'detail': 'Invalid speech phrase'}),
            'not found': response(404, {'detail': {'state': 'draining'}}),
        }
        for name, case in cases.items():
            with self.subTest(name), self.assertRaises(httpx.HTTPStatusError):
                run(transport.speech_status(case))

    def test_success_passes_through(self):
        self.assertIsNone(run(transport.speech_status(response(200, raw=b'RIFF'))))

    def test_busy_is_not_a_contract_or_value_error(self):
        """Code that catches ValueError/ContractError for real failures must not swallow a wait."""
        self.assertFalse(issubclass(SpeechBusy, ValueError))
        self.assertFalse(issubclass(SpeechBusy, transport.ContractError))


class OverTheWire(unittest.TestCase):
    """The real Sidecar methods against a mock HTTP server, for both the buffered and the streamed route."""

    def sidecar(self, handler):
        sidecar = transport.Sidecar.__new__(transport.Sidecar)
        sidecar.url, sidecar.headers = 'http://sidecar.invalid', {'Authorization': 'Bearer test'}
        real = httpx.AsyncClient

        class Mocked(real):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)
        self.patcher = patch.object(transport.httpx, 'AsyncClient', Mocked)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        return sidecar

    ROLE = {'capabilities': {'native_pcm': True, 'audio_output': True}}

    def test_buffered_speech_raises_busy_for_a_draining_synthesis(self):
        sidecar = self.sidecar(lambda request: httpx.Response(409, json={'detail': {'state': 'draining', 'blocked': True}}))
        with self.assertRaises(SpeechBusy):
            run(sidecar.speech({}, 'hello'))

    def test_buffered_speech_keeps_an_uncertain_conflict_an_error(self):
        sidecar = self.sidecar(lambda request: httpx.Response(409, json={'detail': {'state': 'uncertain', 'blocked': True}}))
        with self.assertRaises(httpx.HTTPStatusError):
            run(sidecar.speech({}, 'hello'))

    def test_buffered_speech_still_returns_audio(self):
        sidecar = self.sidecar(lambda request: httpx.Response(200, content=b'RIFF....', headers={'content-type': 'audio/wav'}))
        self.assertEqual(run(sidecar.speech({}, 'hello')), b'RIFF....')

    def test_the_streamed_route_raises_busy_before_any_audio(self):
        sidecar = self.sidecar(lambda request: httpx.Response(409, json={'detail': {'state': 'running', 'blocked': True}}))

        async def first():
            async for _ in sidecar.speech_stream(self.ROLE, 'hello'):
                self.fail('no audio can exist before the provider is admitted')
        with self.assertRaises(SpeechBusy):
            run(first())

    def test_the_streamed_route_keeps_other_conflicts_errors(self):
        sidecar = self.sidecar(lambda request: httpx.Response(409, json={'detail': {'state': 'uncertain'}}))

        async def first():
            async for _ in sidecar.speech_stream(self.ROLE, 'hello'):
                pass
        with self.assertRaises(httpx.HTTPStatusError):
            run(first())


if __name__ == '__main__':
    unittest.main()
