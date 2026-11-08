"""Explicit, revision-pinned public-model downloads into Convo-owned storage."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urljoin, quote

import httpx


PRESETS = {
    "qwen3-tts-1.7b-base-bf16": {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "directory": "Qwen3-TTS-12Hz-1.7B-Base", "hardware": "CUDA build; supervisor requires 10,500 MiB free before load. Combined-load latency unmeasured."},
    "qwen3-tts-0.6b-base-bf16": {"repo": "Qwen/Qwen3-TTS-12Hz-0.6B-Base", "directory": "Qwen3-TTS-12Hz-0.6B-Base", "hardware": "CUDA build; supervisor requires 8,000 MiB free before load. Combined-load latency unmeasured."},
}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def valid_file(name):
    path = PurePosixPath(name)
    return bool(name) and "\\" not in name and not path.is_absolute() and all(p not in (".", "..") and not p.startswith(".") and ":" not in p for p in path.parts)


def manifest(model, metadata):
    preset = PRESETS[model]
    revision = metadata.get("sha", "")
    license_name = metadata.get("cardData", {}).get("license", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or license_name != "apache-2.0":
        raise ValueError("Model revision/license must be reviewed before download")
    files = []
    for item in metadata.get("siblings", []):
        name = item.get("rfilename", "")
        if not valid_file(name) or Path(name).suffix not in {".json", ".safetensors", ".txt", ".md"} and name != "LICENSE":
            continue
        lfs = item.get("lfs") or {}
        size = lfs.get("size", item.get("size"))
        checksum = lfs.get("sha256") or item.get("blobId", "")
        algorithm = "sha256" if lfs else "git-sha1"
        if type(size) is not int or not 0 <= size <= 32 * 1024**3 or not re.fullmatch(r"[0-9a-f]{64}" if lfs else r"[0-9a-f]{40}", checksum):
            raise ValueError("Source does not provide verified size/checksum metadata")
        files.append({"name": name, "size": size, "checksum": checksum, "algorithm": algorithm})
    names = [f["name"] for f in files]
    if not 1 <= len(files) <= 128 or len(set(names)) != len(names) or "config.json" not in names or not any(n.endswith(".safetensors") for n in names):
        raise ValueError("Incomplete model manifest")
    result = {**preset, "model": model, "revision": revision, "license": license_name, "source": "https://huggingface.co/" + preset["repo"], "files": files, "size": sum(f["size"] for f in files)}
    result["id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


async def public_get(client, url, headers=None):
    # Model redirects never carry host/sidecar/provider credentials. Do not follow
    # arbitrary hosts or accept plaintext/local destinations from metadata.
    for _ in range(6):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or not (host == "huggingface.co" or host.endswith(".huggingface.co") or host.endswith(".hf.co")):
            raise ValueError("Unexpected model download destination")
        response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        if response.status_code not in (301, 302, 303, 307, 308):
            response.raise_for_status(); return response
        location = response.headers.get("location", ""); await response.aclose()
        url = urljoin(url, location)
    raise ValueError("Too many model download redirects")


class Downloads:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.tasks = {}
        self.states = {}
        self.previews = {}

    async def inspect(self, model):
        if model not in PRESETS: raise ValueError("Unknown recommended model")
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = await public_get(client, "https://huggingface.co/api/models/" + PRESETS[model]["repo"] + "?blobs=true")
            try:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2 * 1024**2: raise ValueError("Model metadata too large")
                    chunks.append(chunk)
                result = manifest(model, json.loads(b"".join(chunks)))
            finally: await response.aclose()
        self.previews[model] = result
        return result

    def status(self, model):
        if model not in PRESETS: raise ValueError("Unknown model")
        return self.states.get(model, {"state": "idle", "model": model})

    def start(self, model, manifest_id):
        preview = self.previews.get(model)
        if not preview or preview["id"] != manifest_id: raise ValueError("Inspect and approve the exact manifest first")
        if any(not t.done() for t in self.tasks.values()): raise ValueError("A download is already active")
        self.states[model] = {"state": "downloading", "model": model, "bytes": 0, "size": preview["size"], "revision": preview["revision"]}
        self.tasks[model] = asyncio.create_task(self.run(preview))
        return self.status(model)

    async def cancel(self, model):
        task = self.tasks.get(model)
        if task and not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        return self.status(model)

    def inside(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root: raise ValueError("Model path escaped owned storage")
        return path

    def headroom(self, required=0):
        usage = shutil.disk_usage(self.root)
        if usage.free - required < max(5 * 1024**3, usage.total * .05):
            raise ValueError("Insufficient storage reserve; existing files were retained")

    async def run(self, plan):
        model = plan["model"]; state = self.states[model]
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            destination = self.inside(plan["directory"])
            if destination.exists(): raise ValueError("Model directory already exists; nothing was overwritten")
            staging = self.inside(".downloads/" + plan["id"])
            staging.mkdir(parents=True, exist_ok=True)
            atomic(staging / "convo-manifest.json", plan)
            existing = sum((staging / f["name"]).stat().st_size for f in plan["files"] if (staging / f["name"]).is_file())
            self.headroom(max(0, plan["size"] - existing))
            async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
                for item in plan["files"]:
                    path = self.inside(".downloads/" + plan["id"] + "/" + item["name"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    size = path.stat().st_size if path.exists() else 0
                    if size > item["size"]: raise ValueError("Partial file exceeds approved size")
                    hasher = hashlib.sha256() if item["algorithm"] == "sha256" else hashlib.sha1(b"blob " + str(item["size"]).encode() + b"\0")
                    if size:
                        with path.open("rb") as source:
                            while chunk := source.read(1024**2):
                                hasher.update(chunk); await asyncio.sleep(0)
                    if size < item["size"]:
                        url = plan["source"] + "/resolve/" + plan["revision"] + "/" + quote(item["name"], safe="/")
                        response = await public_get(client, url, {"Range": f"bytes={size}-", "Accept-Encoding": "identity"} if size else {"Accept-Encoding": "identity"})
                        try:
                            if size and (response.status_code != 206 or not response.headers.get("content-range", "").startswith(f"bytes {size}-")):
                                raise ValueError("Server did not honor safe resumption; partial file retained")
                            with path.open("ab") as output:
                                async for chunk in response.aiter_bytes():
                                    self.headroom()
                                    if size + len(chunk) > item["size"]: raise ValueError("Download exceeded approved size")
                                    output.write(chunk); hasher.update(chunk); size += len(chunk)
                                    state["file"] = item["name"]; state["bytes"] = state.get("verified_bytes", 0) + size
                                output.flush(); os.fsync(output.fileno())
                        finally: await response.aclose()
                    if size != item["size"] or hasher.hexdigest() != item["checksum"]:
                        # Preserve corrupt bytes for inspection without poisoning the next resume.
                        quarantine = self.inside('.rejected/' + plan['id'] + '/' + os.urandom(8).hex())
                        quarantine.parent.mkdir(parents=True, exist_ok=True)
                        path.rename(quarantine)
                        raise ValueError("Model checksum failed; untrusted file quarantined, not registered")
                    state["verified_bytes"] = state.get("verified_bytes", 0) + size
            # Never replace an installed model; only a fully verified new tree is registered.
            if destination.exists(): raise ValueError("Model appeared during download; registration withheld")
            staging.rename(destination)
            state.update(state="complete", bytes=plan["size"])
        except asyncio.CancelledError:
            state.update(state="paused"); raise
        except Exception:
            state.update(state="failed", error="Download did not complete. Partial files retained; inspect storage/source metadata before retrying.")
