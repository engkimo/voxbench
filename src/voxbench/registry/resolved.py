"""Resolved config value object."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedConfig:
    name: str
    resolved: dict[str, Any]
    hash: str

