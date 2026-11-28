"""Bounded, non-generative decision API for plugin consumers.

No model discovery, downloads, loading, HTTP, file access or host execution.
Consumers supply configuration and an explicitly provisioned backend instance.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import threading
from typing import Mapping

from .decision import Capabilities, DecisionError, TypedAnswer, _distribution, _number


@dataclass(frozen=True)
class ImageInput:
    """Already decoded RGB pixels; never a URL, filename or encoded image bomb."""
    pixels: bytes
    width: int
    height: int


@dataclass(frozen=True)
class AudioInput:
    """Interleaved signed PCM16 little-endian samples with explicit metadata."""
    pcm16: bytes
    sample_rate: int
    channels: int = 1


@dataclass(frozen=True)
class DecisionRequest:
    questions: Mapping[str, dict]
    text: str | None = None
    images: tuple[ImageInput, ...] = ()
    audio: AudioInput | None = None


@dataclass(frozen=True)
class MediaCapabilities(Capabilities):
    # Each entry is a supported exact combination, not independent promises.
    modality_combinations: frozenset[frozenset[str]] = frozenset({frozenset({'text'})})
    max_images: int = 0
    max_image_pixels: int = 0
    audio_sample_rates: frozenset[int] = frozenset()
    audio_channels: frozenset[int] = frozenset()
    max_audio_seconds: float = 0
    max_media_bytes: int = 0
    max_request_chars: int = 64000


def prepare_request(request: DecisionRequest, caps: MediaCapabilities) -> DecisionRequest:
    """Snapshot and validate the entire request before any model invocation."""
    if not isinstance(request, DecisionRequest):
        raise ValueError('Expected DecisionRequest')
    if request.text is not None and (not isinstance(request.text, str) or not request.text.strip() or len(request.text) > caps.context_chars):
        raise ValueError('Decision text is outside the context limit')
    if not isinstance(request.images, tuple):
        raise ValueError('Images must be an immutable tuple')
    modes = frozenset(mode for mode, present in (
        ('text', request.text is not None), ('images', bool(request.images)),
        ('audio', request.audio is not None)) if present)
    if modes not in caps.modality_combinations:
        raise DecisionError('Backend does not support this modality combination')
    total = 0
    if len(request.images) > caps.max_images:
        raise ValueError('Too many images')
    for image in request.images:
        if not isinstance(image, ImageInput) or type(image.width) is not int or type(image.height) is not int:
            raise ValueError('Invalid image metadata')
        if image.width <= 0 or image.height <= 0 or image.width * image.height > caps.max_image_pixels:
            raise ValueError('Image dimensions exceed limit')
        if type(image.pixels) is not bytes or len(image.pixels) != image.width * image.height * 3:
            raise ValueError('Image must contain exact RGB pixels')
        total += len(image.pixels)
    if request.audio is not None:
        audio = request.audio
        if not isinstance(audio, AudioInput) or type(audio.sample_rate) is not int or type(audio.channels) is not int:
            raise ValueError('Invalid audio metadata')
        if audio.sample_rate <= 0 or audio.channels <= 0 or audio.sample_rate not in caps.audio_sample_rates or audio.channels not in caps.audio_channels:
            raise ValueError('Unsupported audio format')
        if type(audio.pcm16) is not bytes or not audio.pcm16 or len(audio.pcm16) % (2 * audio.channels):
            raise ValueError('Invalid PCM16 payload')
        if len(audio.pcm16) / (2 * audio.channels * audio.sample_rate) > caps.max_audio_seconds:
            raise ValueError('Audio duration exceeds backend limit')
        total += len(audio.pcm16)
    if total > caps.max_media_bytes:
        raise ValueError('Media payload exceeds backend limit')
    questions = request.questions
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= caps.max_questions:
        raise ValueError('Invalid question count')
    prepared = {}
    for name, question in questions.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or not isinstance(question, dict):
            raise ValueError('Invalid question')
        operation = question.get('type')
        if operation not in caps.operations:
            raise DecisionError('Backend does not support this operation')
        instructions = question.get('instructions')
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError('Question instructions are required')
        criteria = question.get('criteria')
        if operation == 'choice' and (not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255 or any(not isinstance(k, str) or not k.strip() or len(k) > 128 or not isinstance(v, str) for k, v in criteria.items())):
            raise ValueError('Invalid choice criteria')
        if operation == 'score' and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or any(not isinstance(v, str) or not v for v in criteria)):
            raise ValueError('Invalid score criteria')
        if operation == 'noul' and criteria is not None and (not isinstance(criteria, dict) or set(criteria) - {'true', 'false'} or any(not isinstance(v, str) for v in criteria.values())):
            raise ValueError('Invalid Noul criteria')
        prepared[name] = {'type': operation, 'instructions': instructions}
        if criteria is not None:
            prepared[name]['criteria'] = deepcopy(criteria)
    if len(json.dumps({'state': request.text, 'questions': prepared}, ensure_ascii=False)) > caps.max_request_chars:
        raise ValueError('Complete decision request exceeds limit')
    return DecisionRequest(prepared, request.text, request.images, request.audio)


def normalize_answers(result, questions, backend):
    """Native decision distributions only; generated chat JSON is not an adapter."""
    try:
        answers = result['answers']
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise DecisionError('Missing or unexpected decision answers')
        parsed = {}
        for name, question in questions.items():
            answer, operation = answers[name], question['type']
            if not isinstance(answer, dict) or answer.get('type', operation) != operation:
                raise DecisionError('Answer operation mismatch')
            if operation == 'noul':
                value, confidence, probabilities = _number(answer['noul']), None, {}
            elif operation == 'choice':
                probabilities = _distribution(answer['probabilities'], question['criteria'])
                value = answer['choice']
                if not isinstance(value, str) or value not in probabilities or probabilities[value] < max(probabilities.values()):
                    raise DecisionError('Invalid selected choice')
                confidence = _number(answer['confidence'])
            elif operation == 'score':
                keys = [str(i) for i in range(len(question['criteria']))]
                probabilities = _distribution(answer['probabilities'], keys)
                value = _number(answer['score'], 0, len(keys) - 1)
                if abs(value - sum(int(k) * v for k, v in probabilities.items())) > .05:
                    raise DecisionError('Score conflicts with distribution')
                if answer.get('legend') != dict(zip(keys, question['criteria'])):
                    raise DecisionError('Score legend mismatch')
                confidence = _number(answer['confidence'])
            else:
                raise DecisionError('Unsupported answer operation')
            parsed[name] = TypedAnswer(operation, value, confidence, probabilities, backend)
        return parsed
    except (KeyError, TypeError, ValueError) as error:
        raise DecisionError('Malformed native decision response') from error


class NativeDecisionClient:
    """Injected synchronous native evaluator; capabilities belong to this backend."""
    def __init__(self, *, backend: str, capabilities: MediaCapabilities, evaluate):
        self.backend, self.capabilities, self._evaluate = backend, capabilities, evaluate
        self._worker_lease = threading.Lock()
        self._worker_future = None

    async def evaluate_request(self, request: DecisionRequest) -> dict[str, TypedAnswer]:
        prepared = prepare_request(request, self.capabilities)
        if not self._worker_lease.acquire(blocking=False):
            raise DecisionError('Native decision worker is still busy')
        evaluator = self._evaluate

        def invoke():
            try:
                result = evaluator(prepared)
                return normalize_answers(result, prepared.questions, self.backend)
            finally:
                # Only the actual worker releases ownership. Cancelling its async
                # observer cannot interrupt Python/native code in this thread.
                self._worker_lease.release()

        try:
            future = asyncio.create_task(asyncio.to_thread(invoke))
        except BaseException:
            self._worker_lease.release()
            raise
        self._worker_future = future

        def finished(task):
            if not task.cancelled():
                task.exception()  # Observe late failures after caller cancellation.
            if self._worker_future is task:
                self._worker_future = None

        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except Exception as error:
            raise DecisionError('Native decision invocation failed') from error

    async def evaluate_batch(self, state: str, questions: Mapping[str, dict]):
        return await self.evaluate_request(DecisionRequest(questions, text=state))


class LiquidD1Client(NativeDecisionClient):
    """Explicitly provisioned d1-omni-600M. Never downloads or loads a model.

    Host owns device/dtype (recommended fp16 GPU/fp32 CPU), lifecycle and
    cancellation. Calls are single completed observations, not streaming/Duplex.
    RGB conversion and numpy import occur only when that modality is requested.
    Local image caps are defensive policy, not claimed model maxima.
    """
    def __init__(self, model, *, image_converter=None, audio_converter=None,
                 image_prompt_validator=None):
        if not callable(getattr(model, 'system_one', None)):
            raise ValueError('Injected model must expose native system_one')
        caps = MediaCapabilities(
            operations=frozenset({'choice', 'noul', 'score'}),
            modality_combinations=frozenset(map(frozenset, (
                ('text',), ('images',), ('audio',), ('text', 'images'), ('text', 'audio')))),
            max_images=4, max_image_pixels=4_194_304,
            audio_sample_rates=frozenset({16000}), audio_channels=frozenset({1}),
            max_audio_seconds=30, max_media_bytes=50_331_648)

        def evaluate(request):
            kwargs = {}
            if request.images:
                # D1 truncates its image-path state/question text at 896 tokens.
                # Only the provisioner's tokenizer/template-aware validator can
                # prove retention; a character-count approximation is not proof.
                if not callable(image_prompt_validator):
                    raise DecisionError('D1 image decisions require a tokenizer-aware prompt validator')
                if image_prompt_validator(request, max_text_tokens=896) is not True:
                    raise DecisionError('D1 image prompt retention was not verified')
                convert = image_converter or _pil_rgb
                kwargs['images'] = [convert(image) for image in request.images]
            if request.audio is not None:
                kwargs['audio'] = (audio_converter or _numpy_pcm16)(request.audio)
            return model.system_one(request.text, request.questions, **kwargs)
        super().__init__(backend='liquid_d1', capabilities=caps, evaluate=evaluate)


def _pil_rgb(image):
    from PIL import Image
    return Image.frombytes('RGB', (image.width, image.height), image.pixels)


def _numpy_pcm16(audio):
    import numpy as np
    return np.frombuffer(audio.pcm16, dtype='<i2').astype(np.int16, copy=True)


class ConsumerDecisionClient:
    """Per-call opt-in gate for Convo or another named plugin consumer.

    config_provider returns System One configuration; backend is injected, never
    selected through global Main/Utility/Embedding settings. Revocation while
    inference is in flight invalidates its answer. Host still owns execution.
    """
    def __init__(self, *, consumer: str, config_provider, backend):
        if not isinstance(consumer, str) or not consumer.strip():
            raise ValueError('Consumer name is required')
        self.consumer, self._config_provider, self.backend = consumer, config_provider, backend

    @property
    def capabilities(self):
        caps = self.backend.capabilities
        if isinstance(caps, MediaCapabilities):
            return caps
        return MediaCapabilities(caps.operations, caps.context_chars, caps.max_questions,
                                 caps.encoded_candidate_cache, caps.confidence_semantics)

    def _scope(self):
        config = self._config_provider()
        if not isinstance(config, Mapping) or config.get('enabled') is not True:
            raise DecisionError('System One is disabled')
        consumers = config.get('consumers', {})
        scope = consumers.get(self.consumer) if isinstance(consumers, Mapping) else None
        if not isinstance(scope, Mapping) or scope.get('enabled') is not True:
            raise DecisionError('Decision consumer is disabled')
        return deepcopy(dict(scope))

    async def evaluate_request(self, request: DecisionRequest):
        scope = self._scope()
        if hasattr(self.backend, 'evaluate_request'):
            result = await self.backend.evaluate_request(request)
        else:
            # Existing hosted/text clients retain their original wire contract.
            if not isinstance(request, DecisionRequest) or request.images or request.audio is not None:
                raise DecisionError('Configured text backend does not support media')
            result = await self.backend.evaluate_batch(request.text, request.questions)
        if self._scope() != scope:
            raise DecisionError('Decision consumer configuration changed')
        return result

    async def evaluate_batch(self, state: str, questions: Mapping[str, dict]):
        return await self.evaluate_request(DecisionRequest(questions, text=state))
