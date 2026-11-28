from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import urllib.error
import urllib.request
from typing import Any


DEFAULT_URLS = (
    "http://kokoro-worker:8891",
    "http://agentspine-kokoro-gpu:8891",
    "http://kokoro-gpu-worker:8891",
    "http://host.docker.internal:51101",
)

_last_working_url = ""


def _find_worker(
    remote_url: str,
    remote_token: str,
    timeout: float,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Probe all aliases together so dead Docker DNS names cannot serialize delays."""
    urls = _candidate_urls(remote_url)
    errors: dict[str, str] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(urls)))
    futures = {
        executor.submit(_get_json, url, remote_token, max(0.5, min(timeout, 1.5))): url
        for url in urls
    }
    try:
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                payload = future.result()
                if payload.get("success"):
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    return url, payload, errors
                errors[url] = str(payload.get("error") or "Worker health check failed.")
            except Exception as exc:
                errors[url] = str(exc)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return "", {}, errors


def _request_json(url: str, payload: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url.rstrip("/") + "/synthesize", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _get_json(url: str, token: str, timeout: float) -> dict[str, Any]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url.rstrip("/") + "/health", headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _candidate_urls(remote_url: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    configured = str(remote_url or "").strip().rstrip("/")
    preferred = [configured] if configured and configured not in DEFAULT_URLS else []
    if _last_working_url:
        preferred.insert(0, _last_working_url)
    for value in (*preferred, *DEFAULT_URLS):
        normalized = str(value or "").strip().rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def check_worker(remote_url: str = "", remote_token: str = "", remote_timeout: float = 3.0) -> dict[str, Any]:
    global _last_working_url
    url, payload, errors = _find_worker(
        remote_url,
        remote_token,
        max(0.5, min(float(remote_timeout or 3.0), 1.5)),
    )
    if url:
        _last_working_url = url
        return {
            "success": True,
            "url": url,
            "device": payload.get("device", ""),
            "device_policy": payload.get("device_policy", ""),
            "pipeline_loaded": bool(payload.get("pipeline_loaded")),
            "checked_urls": _candidate_urls(remote_url),
            "errors": errors,
        }
    return {
        "success": False,
        "url": "",
        "checked_urls": _candidate_urls(remote_url),
        "errors": errors,
        "error": "No reachable Kokoro worker was found.",
    }


async def synthesize(
    *,
    sentences: list[str],
    voice: str,
    secondary_voice: str,
    voice_blend: int,
    speed: float,
    remote_url: str,
    remote_token: str,
    remote_timeout: float,
) -> str:
    global _last_working_url
    payload = {
        "sentences": sentences,
        "voice": voice,
        "voice2": secondary_voice or "",
        "blend": voice_blend,
        "speed": speed,
    }
    errors: dict[str, str] = {}
    candidate, _health, probe_errors = await asyncio.to_thread(
        _find_worker,
        remote_url,
        remote_token,
        max(0.5, min(float(remote_timeout or 3.0), 1.5)),
    )
    errors.update(probe_errors)
    if not candidate:
        raise RuntimeError("Remote Kokoro worker is unavailable: " + "; ".join(f"{url}: {err}" for url, err in errors.items()))

    try:
        result = await asyncio.to_thread(_request_json, candidate, payload, remote_token, remote_timeout)
    except Exception as exc:
        _last_working_url = ""
        raise RuntimeError(f"Remote Kokoro worker request failed at {candidate}: {exc}") from exc

    if not (result.get("success") or result.get("audio") or result.get("audio_base64") or result.get("wav")):
        raise RuntimeError(result.get("error") or "Remote worker did not return success.")
    _last_working_url = candidate

    audio = result.get("audio") or result.get("audio_base64") or result.get("wav")
    if not audio:
        raise RuntimeError(result.get("error") or "Remote Kokoro worker did not return audio.")
    try:
        base64.b64decode(audio, validate=True)
    except Exception as exc:
        raise RuntimeError("Remote Kokoro worker returned invalid base64 audio.") from exc
    return audio
