"""Failure signatures: a stable fingerprint of "the same error" across runs.

The first meaningful error line is normalized (paths, hex addresses, numbers and gfx
targets masked) and hashed together with the component, so the same failure on
another run, shard or GPU family maps to the same signature.
"""

from __future__ import annotations

import hashlib
import re

from rocksentinel.therock_ci.extract import dedupe_key

_GFX = re.compile(r"gfx[0-9][0-9A-Za-z]*(?:-[a-z]+)?")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_:.<>\-]*")


def normalize(text: str) -> str:
    return dedupe_key(_GFX.sub("<gfx>", text)).lower()


def make_signature(error_text: str, component: str | None = None) -> str:
    """16-hex-char signature of a normalized error line plus its component."""
    basis = f"{(component or '').lower()}|{normalize(error_text)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def tokens(text: str) -> set[str]:
    """Word tokens of the normalized text, for similarity search."""
    return {t for t in _TOKEN.findall(normalize(text)) if len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of normalized tokens (v1; vector search can replace it later)."""
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0
