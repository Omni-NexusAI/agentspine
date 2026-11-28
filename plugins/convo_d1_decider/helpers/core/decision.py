"""Finite-choice decision clients. No host tools are called from this module."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Mapping
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Capabilities:
    operations: frozenset[str]
    context_chars: int = 16000
    max_questions: int = 32
    encoded_candidate_cache: bool = False
    confidence_semantics: str = "selected_probability"


@dataclass(frozen=True)
class TypedAnswer:
    """Score is relevance, confidence is certainty; Noul has no wire confidence."""
    operation: str
    value: str | float
    confidence: float | None
    probabilities: Mapping[str, float]
    backend: str


def _number(value, minimum=0.0, maximum=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise DecisionError("Invalid numeric decision answer")
    return float(value)


def _distribution(raw, keys):
    if not isinstance(raw, dict) or set(raw) != set(keys):
        raise DecisionError("Decision distribution does not match criteria")
    values = {key: _number(raw[key]) for key in keys}
    if abs(sum(values.values()) - 1.0) > 0.02:
        raise DecisionError("Decision distribution is not normalized")
    return values


class DecisionError(RuntimeError):
    """A backend was unavailable or returned a malformed decision."""


class DecisionTransportError(DecisionError):
    """The configured transport was invoked and failed; details stay private."""

    def __init__(self):
        super().__init__("Decision transport attempt failed")

    @property
    def attempted(self) -> bool:
        return True


@dataclass(frozen=True)
class Choice:
    id: str
    description: str


@dataclass(frozen=True)
class Decision:
    choice: str
    confidence: float
    backend: str


def _post(url: str, payload: dict, token: str, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, json.dumps(payload).encode("utf-8"), headers, method="POST")
    try:
        response = urlopen(request, timeout=timeout)
    except (OSError, TimeoutError) as error:
        raise DecisionTransportError() from error
    with response as opened_response:
        data = opened_response.read(1_048_577)
    if len(data) > 1_048_576:
        raise DecisionError("Decision response is too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise DecisionError("Decision response must be an object")
    return result


class DecisionClient:
    def __init__(self, *, backend: str, model: str, endpoint: str = "", token: str = "", timeout: float = 2.0):
        if backend not in {"jev", "openrouter", "llama_decision"}:
            raise ValueError("Unsupported decision backend")
        if not model.strip():
            raise ValueError("Decision model is required")
        if not 0.1 <= timeout <= 30:
            raise ValueError("Decision timeout must be between 0.1 and 30 seconds")
        self.backend, self.model, self.token, self.timeout = backend, model.strip(), token, timeout
        defaults = {"jev": "https://api.typesafe.ai/v1/systemone",
                    "openrouter": "https://openrouter.ai/api/alpha/decisions"}
        self.endpoint = endpoint.strip() or defaults.get(backend, "")
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("Decision endpoint must be HTTP(S)")
        if backend in {"jev", "openrouter"} and not self.endpoint.startswith("https://"):
            raise ValueError("Hosted decision services require HTTPS")

    @property
    def capabilities(self) -> Capabilities:
        hosted = self.backend in {"jev", "openrouter"}
        return Capabilities(frozenset({"choice", "noul", "score"}) if hosted else frozenset({"choice"}),
                            max_questions=32 if hosted else 1,
                            confidence_semantics="wire_confidence; noul=probability_yes; choose=selected_probability" if hosted else "selected_probability")

    async def evaluate_batch(self, state: str, questions: Mapping[str, dict]) -> dict[str, TypedAnswer]:
        """Evaluate independent questions in one request. Unsupported operations fail closed.

        Contract: https://docs.typesafe.ai/api and OpenRouter Decisions API.
        Caller owns fallback, context budget, and action authorization.
        """
        caps = self.capabilities
        if not isinstance(state, str) or not state or len(state) > caps.context_chars:
            raise ValueError("Decision state is outside the context limit")
        if not isinstance(questions, Mapping) or not 1 <= len(questions) <= caps.max_questions:
            raise ValueError("Invalid independent question count")
        prepared = {}
        for name, question in questions.items():
            if not isinstance(name, str) or not name.strip() or len(name) > 128 or not isinstance(question, dict):
                raise ValueError("Invalid question")
            operation = question.get("type")
            if operation not in caps.operations:
                raise DecisionError("Configured backend does not support this operation")
            instructions = question.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                raise ValueError("Question instructions are required")
            criteria = question.get("criteria")
            if operation == "choice" and (not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255 or any(not isinstance(k, str) or not k.strip() or len(k) > 128 or not isinstance(v, str) for k, v in criteria.items())):
                raise ValueError("Invalid choice criteria")
            if operation == "score" and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or any(not isinstance(v, str) or not v for v in criteria)):
                raise ValueError("Invalid score criteria")
            if operation == "noul" and criteria is not None and (not isinstance(criteria, dict) or set(criteria) - {"true", "false"} or any(not isinstance(v, str) for v in criteria.values())):
                raise ValueError("Invalid Noul criteria")
            prepared[name] = {"type": operation, "instructions": instructions}
            if criteria is not None:
                prepared[name]["criteria"] = deepcopy(criteria)
        if self.backend == "llama_decision":
            name, question = next(iter(prepared.items()))
            result = await self.choose(state, question["criteria"])
            return {name: TypedAnswer("choice", result.choice, result.confidence, {}, self.backend)}
        model = "~typesafe/jev-latest" if self.backend == "openrouter" and self.model == "typesafe/jev-latest" else self.model
        payload = {"state": state, "model": model, "questions": prepared}
        # Bound complete questions as well as state; never truncate a criterion.
        if len(json.dumps(payload, ensure_ascii=False)) > 64000:
            raise ValueError("Complete decision request is too large")
        try:
            result = await asyncio.to_thread(_post, self.endpoint, payload, self.token, self.timeout)
            answers = result["answers"]
            if not isinstance(answers, dict) or set(answers) != set(prepared):
                raise DecisionError("Missing or unexpected batch answers")
            parsed = {}
            for name, question in prepared.items():
                answer = answers[name]
                operation = question["type"]
                if not isinstance(answer, dict) or answer.get("type") != operation:
                    raise DecisionError("Answer operation mismatch")
                if operation == "noul":
                    value, confidence, probabilities = _number(answer["noul"]), None, {}
                elif operation == "choice":
                    probabilities = _distribution(answer["probabilities"], question["criteria"])
                    value = answer["choice"]
                    if value not in probabilities or probabilities[value] < max(probabilities.values()):
                        raise DecisionError("Invalid selected choice")
                    confidence = _number(answer["confidence"])
                else:
                    keys = [str(i) for i in range(len(question["criteria"]))]
                    probabilities = _distribution(answer["probabilities"], keys)
                    value = _number(answer["score"], 0, len(keys) - 1)
                    if abs(value - sum(int(k) * v for k, v in probabilities.items())) > 0.05:
                        raise DecisionError("Score conflicts with probability distribution")
                    if answer.get("legend") != dict(zip(keys, question["criteria"])):
                        raise DecisionError("Score legend mismatch")
                    confidence = _number(answer["confidence"])
                parsed[name] = TypedAnswer(operation, value, confidence, probabilities, self.backend)
            return parsed
        except DecisionTransportError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, OSError, TimeoutError, json.JSONDecodeError) as error:
            raise DecisionError(f"{self.backend} typed decision failed: {type(error).__name__}") from error

    async def noul(self, state: str, instructions: str) -> TypedAnswer:
        return (await self.evaluate_batch(state, {"answer": {"type": "noul", "instructions": instructions}}))["answer"]

    async def score(self, state: str, instructions: str, levels: list[str]) -> TypedAnswer:
        return (await self.evaluate_batch(state, {"answer": {"type": "score", "instructions": instructions, "criteria": levels}}))["answer"]

    async def choose(self, state: str, choices: Mapping[str, str]) -> Decision:
        if not state or len(state) > 16_000:
            raise ValueError("Decision state must be 1 to 16000 characters")
        if not 2 <= len(choices) <= 255 or any(not key or not isinstance(value, str) for key, value in choices.items()):
            raise ValueError("Decision choices must be 2 to 255 named descriptions")
        ids = list(choices)
        if self.backend in {"jev", "openrouter"}:
            model = self.model
            if self.backend == "openrouter" and model == "typesafe/jev-latest":
                model = "~typesafe/jev-latest"
            payload = {"state": state, "model": model, "questions": {"route": {
                "type": "choice", "instructions": "Select exactly one permitted next step for this agent state.",
                "criteria": dict(choices)}}}
            url = self.endpoint
        else:
            payload = {"model": self.model, "instructions": "Select exactly one permitted next step for this agent state.",
                       "schema": {"route": {"type": "enum", "choices": ids, "description": "Permitted next step"}},
                       "contexts": [state]}
            url = self.endpoint.rstrip("/")
            if not url.endswith("/v1/decision"):
                url += "/v1/decision"
        try:
            result = await asyncio.to_thread(_post, url, payload, self.token, self.timeout)
            if self.backend in {"jev", "openrouter"}:
                answer = result["answers"]["route"]
                choice = answer["choice"]
                probabilities = _distribution(answer["probabilities"], choices)
                if choice not in probabilities or probabilities[choice] < max(probabilities.values()):
                    raise DecisionError("Invalid selected choice")
                confidence = probabilities[choice]
            else:
                answer = result["results"][0]
                choice = answer["decision"]["route"]
                confidence = answer["fields"]["route"]["probability"]
            if choice not in choices or isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise DecisionError("Backend returned a choice outside the allowed set")
            return Decision(choice, float(confidence), self.backend)
        except DecisionTransportError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, OSError, TimeoutError, json.JSONDecodeError) as error:
            raise DecisionError(f"{self.backend} decision failed: {type(error).__name__}") from error


def selected_action(decision: Decision, actions: Mapping[str, dict], *, threshold: float) -> dict | None:
    """Return only a predeclared, exact host action, never model-generated arguments."""
    if not 0 <= threshold <= 1 or decision.confidence < threshold:
        return None
    action = actions.get(decision.choice)
    if not isinstance(action, dict) or not isinstance(action.get("tool_name"), str):
        return None
    arguments = action.get("tool_args", {})
    if not isinstance(arguments, dict) or not arguments or not action["tool_name"].strip():
        return None
    # Agent Zero rejects empty tool_args. Reject payloads that cannot be
    # serialized before they reach its tool validation boundary.
    try:
        if len(json.dumps(arguments)) > 16_000:
            return None
    except (TypeError, ValueError):
        return None
    return {"tool_name": action["tool_name"], "tool_args": deepcopy(arguments)}
