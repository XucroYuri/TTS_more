from __future__ import annotations

import hashlib

from app.models import ScriptRevision
from app.semantic_models import SourceSpan


def sha256_source(text: str) -> str:
    """Hash the exact stored source text as UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def py_index_to_utf16(text: str, index: int) -> int:
    """Convert a Python code-point index to a UTF-16 code-unit offset."""
    if index < 0 or index > len(text):
        raise ValueError("python index out of range")
    return len(text[:index].encode("utf-16-le")) // 2


def utf16_to_py_index(text: str, offset: int) -> int:
    """Convert a UTF-16 code-unit offset to a Python code-point index."""
    if offset < 0:
        raise ValueError("UTF-16 offset out of range")

    units = 0
    for index, char in enumerate(text):
        if units == offset:
            return index
        width = len(char.encode("utf-16-le")) // 2
        if units < offset < units + width:
            raise ValueError("UTF-16 offset splits a surrogate pair")
        units += width

    if units == offset:
        return len(text)
    raise ValueError("UTF-16 offset out of range")


def validate_source_span(span: SourceSpan, source: ScriptRevision) -> None:
    """Verify that a source span refers to one exact immutable source slice."""
    if span.source_revision_id != source.revision_id:
        raise ValueError("source revision mismatch")

    expected_hash = source.source_sha256 or sha256_source(source.source_markdown)
    if span.source_sha256 != expected_hash:
        raise ValueError("source hash mismatch")

    start = utf16_to_py_index(source.source_markdown, span.start_utf16)
    end = utf16_to_py_index(source.source_markdown, span.end_utf16)
    if source.source_markdown[start:end] != span.text:
        raise ValueError("source span text mismatch")
