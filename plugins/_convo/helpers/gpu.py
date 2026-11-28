"""Runtime GPU inventory for the Enhanced Speech plugin.

The host can run the same Agent Spine image with or without a GPU reservation.
This helper reports only CUDA devices that the running Python runtime can use;
it deliberately does not infer availability from an image tag or host hardware.
"""

from __future__ import annotations

import re
from typing import Any


_CUDA_DEVICE_RE = re.compile(r"^cuda:(\d+)$")


def get_cuda_devices() -> list[dict[str, Any]]:
    """Return selectable CUDA devices with stable values and human labels."""
    try:
        import torch

        if not torch.cuda.is_available():
            return []
        count = int(torch.cuda.device_count())
        devices: list[dict[str, Any]] = []
        for index in range(count):
            try:
                name = str(torch.cuda.get_device_name(index)).strip()
            except Exception:
                name = ""
            name = name or f"CUDA device {index}"
            devices.append(
                {
                    "value": f"cuda:{index}",
                    "index": index,
                    "name": name,
                    "label": f"{name} (CUDA:{index})",
                }
            )
        return devices
    except Exception:
        return []


def is_cuda_policy(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized == "cuda" or _CUDA_DEVICE_RE.fullmatch(normalized) is not None


def resolve_local_device(policy: str | None) -> tuple[str, str]:
    """Resolve a persisted policy to an executable device and visible label."""
    normalized = str(policy or "auto").strip().lower()
    if normalized == "cpu":
        return "cpu", "CPU"

    devices = get_cuda_devices()
    if not devices:
        return "cpu", "CPU"

    requested_index: int | None = None
    match = _CUDA_DEVICE_RE.fullmatch(normalized)
    if match:
        requested_index = int(match.group(1))

    if normalized in {"auto", "cuda", "gpu"}:
        requested_index = 0

    if requested_index is not None:
        for device in devices:
            if device["index"] == requested_index:
                return str(device["value"]), str(device["label"])

    # A saved accelerator may no longer be reserved by this container.  Do not
    # silently select a different GPU; use CPU and make that effective state
    # visible to the runtime/card instead.
    return "cpu", "CPU"
