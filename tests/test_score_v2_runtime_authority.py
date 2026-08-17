from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from tianlai.events import PerformanceEvent
from tianlai.oscillator import OscillatorInstrument
from tianlai.score_v2_runtime_authority import (
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT,
    SCORE_V2_RUNTIME_AUTHORITY_CONTRACT,
    ScoreV2OscillatorRuntimeAuthority,
    ScoreV2RuntimeAuthorityError,
    open_score_v2_oscillator_runtime_authority,
)
import tianlai.score_v2_runtime_authority as authority_module
import tianlai.oscillator as oscillator_module


def _load_renderer_fixture_module():
    path = Path(__file__).with_name("test_score_v2_renderer.py")
    name = "_tianlai_score_v2_renderer_authority_fixture"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Score-v2 renderer test fixtures")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_RENDERER_FIXTURE = _load_renderer_fixture_module()
_SOURCE_MODULES = (
    "events",
    "instrument",
    "oscillator",
    "tuning",
)


def test_windows_path_and_handle_ctime_may_differ_but_identity_may_not() -> None:
    path_status = SimpleNamespace(
        st_dev=7,
        st_ino=11,
        st_size=13,
        st_mtime_ns=17,
        st_ctime_ns=19,
        st_birthtime_ns=29,
    )
    handle_status = SimpleNamespace(
        st_dev=7,
        st_ino=11,
        st_size=13,
        st_mtime_ns=17,
        st_ctime_ns=23,
        st_birthtime_ns=29,
    )

    with mock.patch(
        "tianlai.score_v2_runtime_authority._is_windows_runtime",
        return_value=True,
    ):
        assert authority_module._same_source_object(
            path_status,
            handle_status,
        )
        assert not authority_module._same_handle_snapshot(
            path_status,
            handle_status,
        )
        for field in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_birthtime_ns",
        ):
            changed = SimpleNamespace(**vars(handle_status))
            setattr(changed, field, getattr(changed, field) + 1)
            assert not authority_module._same_source_object(
                path_status,
                changed,
            )
        path_without_birthtime = SimpleNamespace(
            **{
                key: value
                for key, value in vars(path_status).items()
                if key != "st_birthtime_ns"
            }
        )
        handle_without_birthtime = SimpleNamespace(
            **{
                key: value
                for key, value in vars(handle_status).items()
                if key != "st_birthtime_ns"
            }
        )
        assert authority_module._same_source_object(
            path_without_birthtime,
            handle_without_birthtime,
        )
        assert not authority_module._same_handle_snapshot(
            path_without_birthtime,
            handle_without_birthtime,
        )

    with mock.patch(
        "tianlai.score_v2_runtime_authority._is_windows_runtime",
        return_value=False,
    ):
        assert not authority_module._same_source_object(
            path_status,
            handle_status,
        )


def _copy_runtime_sources(
    root: Path,
    *,
    corrupt_module: str | None = None,
) -> None:
    import tianlai

    package = Path(tianlai.__file__).resolve().parent
    destination = root / "tianlai"
    destination.mkdir(parents=True)
    for module_name in _SOURCE_MODULES:
        payload = (package / f"{module_name}.py").read_bytes()
        if module_name == corrupt_module:
            payload += b"\n# deliberately different loaded-source fixture\n"
        (destination / f"{module_name}.py").write_bytes(payload)


def _fingerprint(
    root: Path,
    manifest: Path,
    *,
    sample_rate: int,
    generation: str,
) -> dict[str, object]:
    import numpy as np

    records = []
    for module_name in _SOURCE_MODULES:
        label = f"tianlai/{module_name}.py"
        payload = (root / label).read_bytes()
        records.append(
            {"path": label, "sha256": hashlib.sha256(payload).hexdigest()}
        )
    records.sort(key=lambda item: item["path"])
    aggregate = hashlib.sha256(
        "".join(
            f"{item['sha256']}  {item['path']}\n" for item in records
        ).encode("utf-8")
    ).hexdigest()
    empty = hashlib.sha256(b"").hexdigest()
    return {
        "algorithm": "sha256-path-content-v1",
        "manifest": {
            "path": manifest.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        },
        "render_python_closure": {
            "algorithm": "ast-render-import-closure-v1",
            "entry_modules": [
                f"tianlai.{module_name}" for module_name in _SOURCE_MODULES
            ],
            "file_count": len(records),
            "files": records,
            "sha256": aggregate,
        },
        "runtime_dependencies": {
            "python": {"version": "test"},
            "numpy": {"version": str(np.__version__)},
            "generation": generation,
        },
        "local_implementation": {"path": None, "sha256": None},
        "resource_verification": {"path": None, "sha256": None},
        "pitch_calibration": {"path": None, "sha256": None},
        "runtime_asset_graph": {
            "algorithm": "constructed-runtime-asset-graph-v1",
            "sample_rate_hz": sample_rate,
            "file_count": 0,
            "total_bytes": 0,
            "region_count": 0,
            "sha256": empty,
        },
    }


def _bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    corrupt_module: str | None = None,
):
    _copy_runtime_sources(tmp_path, corrupt_module=corrupt_module)
    monkeypatch.setattr(_RENDERER_FIXTURE, "_fingerprint", _fingerprint)
    return _RENDERER_FIXTURE._bundle(tmp_path, monkeypatch)


def _code(error: pytest.ExceptionInfo[ScoreV2RuntimeAuthorityError]) -> str:
    assert str(error.value) == error.value.code
    return error.value.code


def test_active_lease_exposes_bound_evidence_and_immutable_detached_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)

    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        evidence = json.loads(authority.acquisition_canonical_bytes)
        assert evidence["contract"] == (
            SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT
        )
        assert evidence["document_authority"] is False
        assert evidence["active_lease_required"] is True
        assert evidence["bindings"]["performance_bundle_sha256"] == (
            bundle.artifact_sha256
        )
        assert evidence["bindings"]["runtime_source_sha256"] == (
            bundle.runtime_source_sha256
        )
        assert evidence["bindings"]["effective_manifest_sha256"] == (
            authority.effective_manifest_sha256
        )
        roots = evidence["loaded_python_generation"]["projection"]["roots"]
        assert roots
        assert all(root["source"]["sha256"] for root in roots)

        authority.dispatch_event(
            PerformanceEvent(
                sample=0,
                sequence=0,
                type="note_on",
                payload={"note_id": 1, "midi_note": 60.0, "velocity": 0.5},
            )
        )
        block = authority.render_block(4)
        assert block.shape == (4, 2)
        assert block.flags.writeable is False
        with pytest.raises(ValueError):
            block.setflags(write=True)
        consumed = authority.finish_execution()
        assert consumed["contract"] == SCORE_V2_RUNTIME_AUTHORITY_CONTRACT
        assert hashlib.sha256(authority.consumed_canonical_bytes).hexdigest() == (
            authority.consumed_sha256
        )


def test_lease_is_revoked_and_source_descriptors_are_closed_on_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    retained: ScoreV2OscillatorRuntimeAuthority | None = None
    descriptors: tuple[int, ...] = ()
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        retained = authority
        state = authority_module._LEASES[id(authority)][1]
        descriptors = tuple(source.descriptor for source in state.held_sources)
        authority.finish_execution()

    assert retained is not None
    with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
        retained.checkpoint()
    assert _code(caught) == "authority.lease_inactive"
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_finish_is_single_use_and_serialized_evidence_cannot_recreate_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        evidence = json.loads(authority.acquisition_canonical_bytes)
        authority.finish_execution()
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.finish_execution()
        assert _code(caught) == "authority.lease_inactive"
        with pytest.raises(TypeError):
            ScoreV2OscillatorRuntimeAuthority(**evidence)
        forged = object.__new__(ScoreV2OscillatorRuntimeAuthority)
        object.__setattr__(forged, "_token", object())
        with pytest.raises(ScoreV2RuntimeAuthorityError) as forged_error:
            forged.checkpoint()
        assert _code(forged_error) == "authority.lease_inactive"


def test_loaded_method_change_permanently_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    original = OscillatorInstrument.render_frame
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        monkeypatch.setattr(
            OscillatorInstrument,
            "render_frame",
            lambda self: (0.0, 0.0),
        )
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.checkpoint(full_sources=False)
        assert _code(caught) == "authority.loaded_code_changed"
        monkeypatch.setattr(OscillatorInstrument, "render_frame", original)
        with pytest.raises(ScoreV2RuntimeAuthorityError) as inactive:
            authority.checkpoint(full_sources=False)
        assert _code(inactive) == "authority.lease_inactive"


def test_loaded_method_code_change_permanently_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    function = OscillatorInstrument.render_frame
    original_code = function.__code__
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        function.__code__ = (lambda self: (0.0, 0.0)).__code__
        try:
            with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
                authority.checkpoint(full_sources=False)
            assert _code(caught) == "authority.loaded_code_changed"
        finally:
            function.__code__ = original_code
        with pytest.raises(ScoreV2RuntimeAuthorityError) as inactive:
            authority.checkpoint(full_sources=False)
        assert _code(inactive) == "authority.lease_inactive"


def test_loaded_global_change_permanently_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    original = oscillator_module.math
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        monkeypatch.setattr(oscillator_module, "math", object())
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.render_block(1)
        assert _code(caught) == "authority.loaded_code_changed"
        monkeypatch.setattr(oscillator_module, "math", original)
        with pytest.raises(ScoreV2RuntimeAuthorityError) as inactive:
            authority.render_block(1)
        assert _code(inactive) == "authority.lease_inactive"


def test_factory_provenance_change_permanently_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        state = authority_module._LEASES[id(authority)][1]
        provenance = state.instrument._tianlai_factory_provenance
        assert provenance is not None
        provenance["factory_route"] = "forged"
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.checkpoint(full_sources=False)
        assert _code(caught) == "authority.generation_changed"
        provenance["factory_route"] = authority_module.SCORE_V2_RUNTIME_FACTORY_ROUTE
        with pytest.raises(ScoreV2RuntimeAuthorityError) as inactive:
            authority.checkpoint(full_sources=False)
        assert _code(inactive) == "authority.lease_inactive"


def test_factory_static_instance_change_permanently_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        state = authority_module._LEASES[id(authority)][1]
        state.instrument.gain += 0.1
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.checkpoint(full_sources=False)
        assert _code(caught) == "authority.generation_changed"
        with pytest.raises(ScoreV2RuntimeAuthorityError) as inactive:
            authority.checkpoint(full_sources=False)
        assert _code(inactive) == "authority.lease_inactive"


def test_held_source_content_change_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        state = authority_module._LEASES[id(authority)][1]
        source = next(
            item
            for item in state.held_sources
            if item.label == "tianlai/oscillator.py"
        )
        Path(source.path).write_bytes(Path(source.path).read_bytes() + b"\n")
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.checkpoint()
        assert _code(caught) == "authority.generation_changed"


def test_held_source_path_replacement_revokes_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        state = authority_module._LEASES[id(authority)][1]
        source = next(
            item
            for item in state.held_sources
            if item.label == "tianlai/oscillator.py"
        )
        real_lstat = os.lstat

        def changed_path_identity(value: object) -> os.stat_result:
            observed = real_lstat(value)
            if Path(value).resolve() != Path(source.path).resolve():
                return observed
            fields = list(observed)
            fields[1] = int(fields[1]) + 1
            return os.stat_result(fields)

        # Windows can deny a physical replace while the proof descriptor is
        # open.  Inject the exact post-replace pathname observation instead;
        # the descriptor remains real and open throughout this check.
        monkeypatch.setattr(
            authority_module.os,
            "lstat",
            changed_path_identity,
        )
        with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
            authority.checkpoint()
        assert _code(caught) == "authority.generation_changed"


def test_closure_source_must_match_the_actual_loaded_module_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(
        tmp_path,
        monkeypatch,
        corrupt_module="oscillator",
    )
    with pytest.raises(ScoreV2RuntimeAuthorityError) as caught:
        with open_score_v2_oscillator_runtime_authority(bundle, executor_id):
            pass
    assert _code(caught) == "authority.loaded_source_mismatch"
