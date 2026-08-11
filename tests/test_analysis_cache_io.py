from __future__ import annotations

import json
from pathlib import Path

from tianlai.analysis_cache import CollaborationAnalysisCache
from tianlai.canonical_json import canonical_json_bytes


def test_analysis_cache_reuses_canonical_tuple_identity_and_payload(
    tmp_path: Path,
) -> None:
    cache = CollaborationAnalysisCache(tmp_path / "analysis")
    tuple_identity = {
        "format": "test.analysis.identity",
        "version": 1,
        "parts": ("cello", "旋律"),
    }
    tuple_payload = {"bands": (1, 2, 3), "note": "line 1\nline 2"}
    kind = 'test/"雪"'

    stored = cache.store(
        tuple_identity,
        kind=kind,
        payload=tuple_payload,
    )
    assert stored.status == "stored"
    entry = next((tmp_path / "analysis").rglob("*.json"))
    encoded = entry.read_bytes()
    assert encoded == canonical_json_bytes(json.loads(encoded))

    list_identity = {
        **tuple_identity,
        "parts": ["cello", "旋律"],
    }
    loaded = cache.load(list_identity, kind=kind)
    assert loaded.hit
    assert loaded.payload == {
        "bands": [1, 2, 3],
        "note": "line 1\nline 2",
    }

    # Tuple/list equivalence is part of the canonical JSON contract.  Reusing
    # the precomputed bytes must not turn a semantic cache hit into conflict.
    assert (
        cache.store(
            list_identity,
            kind=kind,
            payload={
                "bands": [1, 2, 3],
                "note": "line 1\nline 2",
            },
        ).status
        == "exists"
    )


def test_analysis_cache_store_keeps_strict_identity_keys(tmp_path: Path) -> None:
    cache = CollaborationAnalysisCache(tmp_path / "analysis")
    result = cache.store(
        {"valid": {1: "not-a-string-key"}},
        kind="test",
        payload={"value": 1},
    )
    assert result.status == "invalid_input"
