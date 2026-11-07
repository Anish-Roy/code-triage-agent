"""Small text helpers shared by trimming and signature generation."""

from __future__ import annotations

import re

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)
