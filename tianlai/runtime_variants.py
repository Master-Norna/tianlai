"""Capture runtime variant selections without steering the renderer.

The capture layer is deliberately observational.  Backends may publish the
choice directory for a condition and a receipt for the choice they actually
made, but this module never asks them to take a particular branch.  A later
probe workflow can use these records to plan exhaustive renders; the records
themselves are not a claim that such renders have happened.

Onset certification is intentionally conservative: an independent proof
builder accepts only exact built-in backends for which it can reconstruct the
complete note-on attack selection path.  This includes the oscillator, the
top-level SampleInstrument, and the exact DedicatedSfzInstrument attack-layer
adapter.  Dedicated SFZ release triggers are a separate note-off phase and are
never smuggled into an attack receipt.  Capture-only receipts never certify
themselves.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import math
from numbers import Real
import re
from typing import Any, Iterator

from .canonical_json import canonical_json_bytes as _project_canonical_json_bytes


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RuntimeVariantError(ValueError):
    """A backend supplied an unsafe or internally inconsistent receipt."""


class RuntimeVariantNotCertifiable(RuntimeVariantError):
    """The observation is valid capture data but cannot prove full coverage."""


def _reject_nonfinite(value: Any, label: str = "variant record") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeVariantError(f"{label} contains a non-string key")
            _reject_nonfinite(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{label}[{index}]")
        return
    if isinstance(value, Real) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise RuntimeVariantError(f"{label} contains a non-finite number")


def _canonical_bytes(value: Any) -> bytes:
    _reject_nonfinite(value)
    try:
        return _project_canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise RuntimeVariantError(
            f"runtime variant record is not canonical JSON: {error}"
        ) from error


def _canonical_copy(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _json_values_match(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/number equality alias."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        return left.keys() == right.keys() and all(
            _json_values_match(left[key], right[key]) for key in left
        )
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            _json_values_match(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _sample_catalogs_match(left: Any, right: Any) -> bool:
    """Compare validated catalogs without representation-specific hashes."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_fields = {
        key: value for key, value in left.items() if key != "condition_sha256"
    }
    right_fields = {
        key: value for key, value in right.items() if key != "condition_sha256"
    }
    return _json_values_match(left_fields, right_fields)


def _runtime_variant_proofs_match(left: Any, right: Any) -> bool:
    """Compare validated proofs without representation-only JSON hashes."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    representation_hashes = {
        "top_level_contract_sha256",
        "proof_sha256",
    }
    def semantic_fields(value: dict[str, Any]) -> dict[str, Any]:
        fields = {
            key: item
            for key, item in value.items()
            if key not in representation_hashes
        }
        contract = fields.get("top_level_contract")
        if isinstance(contract, dict):
            contract_fields = dict(contract)
            attack = contract_fields.get("attack_phase_contract")
            if isinstance(attack, dict):
                contract_fields["attack_phase_contract"] = {
                    key: item
                    for key, item in attack.items()
                    if key
                    not in {
                        "retained_bundle_sha256",
                        "static_audio_bundle_sha256",
                    }
                }
            fields["top_level_contract"] = contract_fields
        return fields

    left_fields = semantic_fields(left)
    right_fields = semantic_fields(right)
    return _json_values_match(left_fields, right_fields)


def _reject_absolute_path_strings(
    value: Any,
    label: str = "variant record",
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_absolute_path_strings(item, f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_absolute_path_strings(item, f"{label}[{index}]")
        return
    if not isinstance(value, str):
        return
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or (
        len(normalized) >= 3
        and normalized[1] == ":"
        and normalized[2] == "/"
    ):
        raise RuntimeVariantError(
            f"{label} must not expose an absolute filesystem path"
        )


def stable_variant_sha256(kind: str, payload: Any) -> str:
    """Hash a typed, canonical JSON identity."""

    if not isinstance(kind, str) or not kind:
        raise RuntimeVariantError("variant identity kind must be non-empty")
    return hashlib.sha256(
        _canonical_bytes({"kind": kind, "payload": payload})
    ).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RuntimeVariantError(f"{label} must be a lowercase SHA-256")
    return value


def _require_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return one JSON integer without accepting Python's bool subclass."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeVariantError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise RuntimeVariantError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeVariantError(f"{label} must be at most {maximum}")
    return value


def _require_boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeVariantError(f"{label} must be boolean")
    return value


def _require_schema_version(value: Any, label: str) -> None:
    version = _require_integer(value, label)
    if version != 1:
        raise RuntimeVariantError(f"{label} is unsupported")


def _require_finite_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise RuntimeVariantError(f"{label} must be a finite number")
    return float(value)


def _expect_exact_keys(
    value: Any,
    required: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeVariantError(f"{label} must be an object")
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required)
    if missing:
        raise RuntimeVariantError(
            f"{label} is missing fields: {', '.join(missing)}"
        )
    if extra:
        raise RuntimeVariantError(
            f"{label} has unknown fields: {', '.join(extra)}"
        )
    return value


def onset_sampled_condition(
    *,
    final_articulation: str,
    midi_note: int,
    velocity: int,
    sample_rate_hz: int,
) -> dict[str, Any]:
    """Return the canonical sampled pitch/velocity/articulation condition.

    Repeat observations intentionally receive the same identifier.  Repeating
    an already sampled condition can help a human judge consistency, but it
    never increases the declared condition coverage.
    """

    if not isinstance(final_articulation, str) or not final_articulation:
        raise RuntimeVariantError("final_articulation must be non-empty")
    if isinstance(midi_note, bool) or not isinstance(midi_note, int):
        raise RuntimeVariantError("midi_note must be an integer")
    if not 0 <= midi_note <= 127:
        raise RuntimeVariantError("midi_note must be between 0 and 127")
    if isinstance(velocity, bool) or not isinstance(velocity, int):
        raise RuntimeVariantError("velocity must be an integer")
    if not 1 <= velocity <= 127:
        raise RuntimeVariantError("velocity must be between 1 and 127")
    if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int):
        raise RuntimeVariantError("sample_rate_hz must be an integer")
    if not 8_000 <= sample_rate_hz <= 384_000:
        raise RuntimeVariantError(
            "sample_rate_hz must be between 8000 and 384000"
        )
    return {
        "context": "isolated_attack",
        "final_articulation": final_articulation,
        "midi_note": midi_note,
        "velocity": velocity,
        "sample_rate_hz": sample_rate_hz,
        "tuning": "equal_temperament_a4_440",
        "event_contract": "isolated_note_id_1_no_private_sampler_controls",
    }


def onset_sampled_condition_id(
    *,
    final_articulation: str,
    midi_note: int,
    velocity: int,
    sample_rate_hz: int,
) -> str:
    condition = onset_sampled_condition(
        final_articulation=final_articulation,
        midi_note=midi_note,
        velocity=velocity,
        sample_rate_hz=sample_rate_hz,
    )
    return stable_variant_sha256(
        "onset-isolated-sampled-condition-v1",
        condition,
    )


def _validate_sampled_condition_payload(value: Any) -> dict[str, Any]:
    condition = _expect_exact_keys(
        _canonical_copy(value),
        {
            "context",
            "final_articulation",
            "midi_note",
            "velocity",
            "sample_rate_hz",
            "tuning",
            "event_contract",
        },
        "sampled_condition",
    )
    expected = onset_sampled_condition(
        final_articulation=condition["final_articulation"],
        midi_note=condition["midi_note"],
        velocity=condition["velocity"],
        sample_rate_hz=condition["sample_rate_hz"],
    )
    if not _json_values_match(condition, expected):
        raise RuntimeVariantError("sampled_condition is not canonical")
    return condition


@dataclass(slots=True)
class RuntimeVariantCapture:
    """One context-local collection of catalogs and actual selections."""

    _catalogs: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _selections: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _sealed: bool = field(default=False, init=False, repr=False)

    def record_selection(
        self,
        *,
        catalog: dict[str, Any],
        choice_sha256: str,
        actual_selector: dict[str, Any],
    ) -> int:
        """Record one already-made backend choice.

        Catalogs are content-addressed and deduplicated.  The method validates
        that the selected choice belongs to the supplied catalog, so an
        instrumentation mistake fails closed while capture is enabled.
        """

        if self._sealed:
            raise RuntimeVariantError(
                "runtime variant capture is sealed"
            )
        safe_catalog = _canonical_copy(catalog)
        _reject_absolute_path_strings(safe_catalog, "catalog")
        component_sha256 = _require_sha256(
            safe_catalog.get("component_sha256"),
            "catalog component_sha256",
        )
        condition_sha256 = _require_sha256(
            safe_catalog.get("condition_sha256"),
            "catalog condition_sha256",
        )
        if not isinstance(safe_catalog.get("deterministic_single"), bool):
            raise RuntimeVariantError(
                "catalog deterministic_single must be boolean"
            )
        choices = safe_catalog.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeVariantError(
                "a successful selection catalog must contain choices"
            )
        known_choices = {
            _require_sha256(
                choice.get("choice_sha256") if isinstance(choice, dict) else None,
                "catalog choice_sha256",
            )
            for choice in choices
        }
        selected = _require_sha256(choice_sha256, "selected choice_sha256")
        if selected not in known_choices:
            raise RuntimeVariantError(
                "selected runtime choice is absent from its catalog"
            )

        catalog_sha256 = stable_variant_sha256(
            "runtime-variant-catalog-v1",
            safe_catalog,
        )
        previous = self._catalogs.get(catalog_sha256)
        if previous is not None and previous != safe_catalog:
            raise RuntimeVariantError("runtime variant catalog hash collision")
        self._catalogs[catalog_sha256] = safe_catalog

        raw_domains = safe_catalog.get("unexhausted_domains", [])
        if not isinstance(raw_domains, list):
            raise RuntimeVariantError(
                "catalog unexhausted_domains must be an array"
            )
        domain_names: list[str] = []
        for domain in raw_domains:
            if (
                not isinstance(domain, dict)
                or not isinstance(domain.get("domain"), str)
                or not domain["domain"]
            ):
                raise RuntimeVariantError(
                    "catalog contains an invalid unexhausted domain"
                )
            domain_names.append(domain["domain"])

        safe_actual_selector = _canonical_copy(actual_selector)
        _reject_absolute_path_strings(
            safe_actual_selector,
            "actual_selector",
        )
        selection_index = len(self._selections)
        self._selections.append(
            {
                "selection_index": selection_index,
                "component_sha256": component_sha256,
                "condition_sha256": condition_sha256,
                "choice_sha256": selected,
                "catalog_sha256": catalog_sha256,
                "deterministic_single": safe_catalog[
                    "deterministic_single"
                ],
                "unexhausted_domains": sorted(set(domain_names)),
                "actual_selector": safe_actual_selector,
                # A top-level composite wrapper may finalize this provisional
                # leaf choice with the route/discard decision it actually
                # made.  A direct SampleInstrument intentionally leaves it
                # null.
                "wrapper_outcome": None,
            }
        )
        return selection_index

    def finalize_selection(
        self,
        selection_index: int,
        *,
        wrapper_outcome: dict[str, Any],
    ) -> None:
        """Finalize one provisional leaf choice from a composite wrapper."""

        if self._sealed:
            raise RuntimeVariantError("runtime variant capture is sealed")
        if (
            isinstance(selection_index, bool)
            or not isinstance(selection_index, int)
            or not 0 <= selection_index < len(self._selections)
        ):
            raise RuntimeVariantError(
                "wrapper outcome selection_index is invalid"
            )
        selection = self._selections[selection_index]
        if selection["wrapper_outcome"] is not None:
            raise RuntimeVariantError(
                "runtime variant selection was finalized more than once"
            )
        safe_outcome = _canonical_copy(wrapper_outcome)
        _reject_absolute_path_strings(
            safe_outcome,
            "wrapper_outcome",
        )
        selection["wrapper_outcome"] = safe_outcome

    @property
    def selection_count(self) -> int:
        return len(self._selections)

    def receipt(self) -> dict[str, Any]:
        """Return a stable, source-path-free capture document."""

        selections = _canonical_copy(self._selections)
        catalogs = [
            {
                "catalog_sha256": catalog_sha256,
                "catalog": _canonical_copy(catalog),
            }
            for catalog_sha256, catalog in sorted(self._catalogs.items())
        ]
        payload = {
            "schema_version": 1,
            "kind": "runtime_variant_selection_receipt",
            "claim": "capture_only_not_variant_certification",
            "selection_count": len(selections),
            "all_conditions_deterministic_single": bool(selections)
            and all(
                selection["deterministic_single"]
                for selection in selections
            ),
            "catalogs": catalogs,
            "selections": selections,
        }
        return {
            **payload,
            "receipt_sha256": stable_variant_sha256(
                "runtime-variant-selection-receipt-v1",
                payload,
            ),
        }

    def seal(self) -> None:
        """Prevent any mutation after the capture context has exited."""

        self._sealed = True


def _validate_dedicated_sfz_wrapper_outcome(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    outcome = _expect_exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "phase",
            "articulation",
            "layer_index",
            "wrapper_role_sha256",
            "event_sequence",
            "target_midi",
            "velocity",
            "key_velocity_match",
            "wrapper_velocity_gain",
            "route_committed",
            "committed_amplitude",
            "final_status",
        },
        label,
    )
    _require_schema_version(
        outcome["schema_version"],
        f"{label}.schema_version",
    )
    if outcome["kind"] != "dedicated_sfz_wrapper_outcome":
        raise RuntimeVariantError(f"{label}.kind is invalid")
    if outcome["phase"] not in {
        "note_on_attack",
        "note_off_release_trigger",
    }:
        raise RuntimeVariantError(f"{label}.phase is invalid")
    if (
        not isinstance(outcome["articulation"], str)
        or not outcome["articulation"]
    ):
        raise RuntimeVariantError(
            f"{label}.articulation must be non-empty"
        )
    for field in ("layer_index", "event_sequence"):
        if (
            isinstance(outcome[field], bool)
            or not isinstance(outcome[field], int)
            or outcome[field] < 0
        ):
            raise RuntimeVariantError(
                f"{label}.{field} must be a non-negative integer"
            )
    _require_sha256(
        outcome["wrapper_role_sha256"],
        f"{label}.wrapper_role_sha256",
    )
    for field in ("target_midi", "velocity"):
        if (
            isinstance(outcome[field], bool)
            or not isinstance(outcome[field], Real)
            or not math.isfinite(float(outcome[field]))
        ):
            raise RuntimeVariantError(f"{label}.{field} must be finite")
    if not isinstance(outcome["key_velocity_match"], bool):
        raise RuntimeVariantError(
            f"{label}.key_velocity_match must be boolean"
        )
    if not isinstance(outcome["route_committed"], bool):
        raise RuntimeVariantError(
            f"{label}.route_committed must be boolean"
        )
    for field in ("wrapper_velocity_gain", "committed_amplitude"):
        item = outcome[field]
        if item is not None and (
            isinstance(item, bool)
            or not isinstance(item, Real)
            or not math.isfinite(float(item))
        ):
            raise RuntimeVariantError(
                f"{label}.{field} must be finite or null"
            )
    status = outcome["final_status"]
    if status == "discarded_key_or_velocity_mismatch":
        expected = {
            "key_velocity_match": False,
            "wrapper_velocity_gain": None,
            "route_committed": False,
            "committed_amplitude": None,
        }
    elif status == "discarded_wrapper_gain_threshold":
        gain = outcome["wrapper_velocity_gain"]
        if (
            gain is None
            or float(gain) < 0.0
            or float(gain) > 1.0e-9
        ):
            raise RuntimeVariantError(
                f"{label}.wrapper_velocity_gain is inconsistent"
            )
        expected = {
            "key_velocity_match": True,
            "wrapper_velocity_gain": gain,
            "route_committed": False,
            "committed_amplitude": None,
        }
    elif status in {
        "retained_attack_voice",
        "retained_release_trigger_voice",
    }:
        gain = outcome["wrapper_velocity_gain"]
        amplitude = outcome["committed_amplitude"]
        if gain is None or float(gain) <= 1.0e-9 or amplitude is None:
            raise RuntimeVariantError(
                f"{label} retained outcome is incomplete"
            )
        expected = {
            "key_velocity_match": True,
            "wrapper_velocity_gain": gain,
            "route_committed": True,
            "committed_amplitude": amplitude,
        }
    else:
        raise RuntimeVariantError(f"{label}.final_status is invalid")
    if (
        status == "retained_attack_voice"
        and outcome["phase"] != "note_on_attack"
    ) or (
        status == "retained_release_trigger_voice"
        and outcome["phase"] != "note_off_release_trigger"
    ):
        raise RuntimeVariantError(
            f"{label}.final_status belongs to another selection phase"
        )
    for field, expected_value in expected.items():
        if not _json_values_match(outcome[field], expected_value):
            raise RuntimeVariantError(
                f"{label}.{field} is inconsistent with final_status"
            )
    return outcome


def validate_runtime_variant_selection_receipt(
    receipt: Any,
) -> dict[str, Any]:
    """Strictly validate a capture-only receipt and all catalog bindings.

    This function does *not* certify variant coverage.  Its result retains the
    explicit ``capture_only_not_variant_certification`` claim; certification
    requires an exact built-in top-level contract plus the stricter
    :func:`certify_deterministic_single_observation` checks below.
    """

    safe = _canonical_copy(receipt)
    _reject_absolute_path_strings(safe, "selection_receipt")
    value = _expect_exact_keys(
        safe,
        {
            "schema_version",
            "kind",
            "claim",
            "selection_count",
            "all_conditions_deterministic_single",
            "catalogs",
            "selections",
            "receipt_sha256",
        },
        "selection_receipt",
    )
    _require_schema_version(
        value["schema_version"],
        "selection_receipt schema_version",
    )
    if value["kind"] != "runtime_variant_selection_receipt":
        raise RuntimeVariantError("selection_receipt kind is invalid")
    if value["claim"] != "capture_only_not_variant_certification":
        raise RuntimeVariantError(
            "selection_receipt must remain capture-only"
        )
    if (
        isinstance(value["selection_count"], bool)
        or not isinstance(value["selection_count"], int)
        or value["selection_count"] < 0
    ):
        raise RuntimeVariantError(
            "selection_receipt selection_count must be non-negative"
        )
    if not isinstance(value["all_conditions_deterministic_single"], bool):
        raise RuntimeVariantError(
            "selection_receipt deterministic flag must be boolean"
        )
    if not isinstance(value["catalogs"], list):
        raise RuntimeVariantError("selection_receipt catalogs must be an array")
    if not isinstance(value["selections"], list):
        raise RuntimeVariantError(
            "selection_receipt selections must be an array"
        )
    if value["selection_count"] != len(value["selections"]):
        raise RuntimeVariantError(
            "selection_receipt selection_count is inconsistent"
        )

    catalog_by_sha: dict[str, dict[str, Any]] = {}
    for index, raw_record in enumerate(value["catalogs"]):
        label = f"selection_receipt.catalogs[{index}]"
        record = _expect_exact_keys(
            raw_record,
            {"catalog_sha256", "catalog"},
            label,
        )
        catalog_sha256 = _require_sha256(
            record["catalog_sha256"],
            f"{label}.catalog_sha256",
        )
        catalog = _expect_exact_keys(
            record["catalog"],
            {
                "algorithm",
                "claim",
                "component_sha256",
                "condition",
                "condition_sha256",
                "selector_domain",
                "partitions",
                "has_selector_gaps",
                "choices",
                "unexhausted_domains",
                "deterministic_single",
            },
            f"{label}.catalog",
        )
        if catalog["claim"] != (
            "choice_directory_only_not_variant_certification"
        ):
            raise RuntimeVariantError(
                f"{label}.catalog must remain a capture-only directory"
            )
        _require_sha256(
            catalog["component_sha256"],
            f"{label}.catalog.component_sha256",
        )
        _require_sha256(
            catalog["condition_sha256"],
            f"{label}.catalog.condition_sha256",
        )
        if not isinstance(catalog["condition"], dict):
            raise RuntimeVariantError(
                f"{label}.catalog.condition must be an object"
            )
        if not isinstance(catalog["selector_domain"], dict):
            raise RuntimeVariantError(
                f"{label}.catalog.selector_domain must be an object"
            )
        if not isinstance(catalog["partitions"], list):
            raise RuntimeVariantError(
                f"{label}.catalog.partitions must be an array"
            )
        if not isinstance(catalog["has_selector_gaps"], bool):
            raise RuntimeVariantError(
                f"{label}.catalog.has_selector_gaps must be boolean"
            )
        if not isinstance(catalog["choices"], list) or not catalog["choices"]:
            raise RuntimeVariantError(
                f"{label}.catalog.choices must be non-empty"
            )
        choice_hashes: set[str] = set()
        for choice_index, choice in enumerate(catalog["choices"]):
            if not isinstance(choice, dict):
                raise RuntimeVariantError(
                    f"{label}.catalog.choices[{choice_index}] must be an object"
                )
            choice_sha256 = _require_sha256(
                choice.get("choice_sha256"),
                f"{label}.catalog.choices[{choice_index}].choice_sha256",
            )
            if choice_sha256 in choice_hashes:
                raise RuntimeVariantError(
                    f"{label}.catalog repeats a choice hash"
                )
            choice_hashes.add(choice_sha256)
        if not isinstance(catalog["unexhausted_domains"], list):
            raise RuntimeVariantError(
                f"{label}.catalog.unexhausted_domains must be an array"
            )
        if not isinstance(catalog["deterministic_single"], bool):
            raise RuntimeVariantError(
                f"{label}.catalog.deterministic_single must be boolean"
            )
        if catalog["algorithm"] == "sample-select-region-partition-v1":
            _validate_sample_catalog_condition(
                catalog,
                label=f"{label}.catalog.condition",
            )
            _validate_sample_catalog_numeric_fields(
                catalog,
                label=f"{label}.catalog",
            )
        expected_catalog_sha256 = stable_variant_sha256(
            "runtime-variant-catalog-v1",
            catalog,
        )
        if catalog_sha256 != expected_catalog_sha256:
            raise RuntimeVariantError(
                f"{label}.catalog_sha256 does not bind its catalog"
            )
        if catalog_sha256 in catalog_by_sha:
            raise RuntimeVariantError(
                "selection_receipt contains a duplicate catalog"
            )
        catalog_by_sha[catalog_sha256] = catalog

    deterministic_flags: list[bool] = []
    for index, raw_selection in enumerate(value["selections"]):
        label = f"selection_receipt.selections[{index}]"
        selection = _expect_exact_keys(
            raw_selection,
            {
                "selection_index",
                "component_sha256",
                "condition_sha256",
                "choice_sha256",
                "catalog_sha256",
                "deterministic_single",
                "unexhausted_domains",
                "actual_selector",
                "wrapper_outcome",
            },
            label,
        )
        selection_index = _require_integer(
            selection["selection_index"],
            f"{label}.selection_index",
            minimum=0,
        )
        if selection_index != index:
            raise RuntimeVariantError(
                f"{label}.selection_index must be contiguous"
            )
        component_sha256 = _require_sha256(
            selection["component_sha256"],
            f"{label}.component_sha256",
        )
        condition_sha256 = _require_sha256(
            selection["condition_sha256"],
            f"{label}.condition_sha256",
        )
        choice_sha256 = _require_sha256(
            selection["choice_sha256"],
            f"{label}.choice_sha256",
        )
        catalog_sha256 = _require_sha256(
            selection["catalog_sha256"],
            f"{label}.catalog_sha256",
        )
        catalog = catalog_by_sha.get(catalog_sha256)
        if catalog is None:
            raise RuntimeVariantError(
                f"{label} refers to an absent catalog"
            )
        if component_sha256 != catalog["component_sha256"]:
            raise RuntimeVariantError(
                f"{label} component differs from its catalog"
            )
        if condition_sha256 != catalog["condition_sha256"]:
            raise RuntimeVariantError(
                f"{label} condition differs from its catalog"
            )
        known_choices = {
            choice["choice_sha256"] for choice in catalog["choices"]
        }
        if choice_sha256 not in known_choices:
            raise RuntimeVariantError(
                f"{label} choice is absent from its catalog"
            )
        if selection["deterministic_single"] is not catalog[
            "deterministic_single"
        ]:
            raise RuntimeVariantError(
                f"{label} deterministic flag differs from its catalog"
            )
        raw_domains = catalog["unexhausted_domains"]
        expected_domains = sorted(
            {
                domain.get("domain")
                for domain in raw_domains
                if isinstance(domain, dict)
                and isinstance(domain.get("domain"), str)
                and domain["domain"]
            }
        )
        if len(expected_domains) != len(
            {
                domain.get("domain")
                for domain in raw_domains
                if isinstance(domain, dict)
            }
        ):
            raise RuntimeVariantError(
                f"{label} catalog has an invalid unexhausted domain"
            )
        if selection["unexhausted_domains"] != expected_domains:
            raise RuntimeVariantError(
                f"{label} unexhausted domains differ from its catalog"
            )
        if not isinstance(selection["actual_selector"], dict):
            raise RuntimeVariantError(
                f"{label}.actual_selector must be an object"
            )
        if catalog["algorithm"] == "sample-select-region-partition-v1":
            _validate_sample_actual_selector_integer_fields(
                selection["actual_selector"],
                label=f"{label}.actual_selector",
            )
        wrapper_outcome = selection["wrapper_outcome"]
        if wrapper_outcome is not None:
            _validate_dedicated_sfz_wrapper_outcome(
                wrapper_outcome,
                label=f"{label}.wrapper_outcome",
            )
        deterministic_flags.append(bool(selection["deterministic_single"]))

    referenced_catalogs = {
        selection["catalog_sha256"] for selection in value["selections"]
    }
    if referenced_catalogs != set(catalog_by_sha):
        raise RuntimeVariantError(
            "selection_receipt contains an unconsumed catalog"
        )
    expected_all_single = bool(value["selections"]) and all(
        deterministic_flags
    )
    if value["all_conditions_deterministic_single"] is not expected_all_single:
        raise RuntimeVariantError(
            "selection_receipt aggregate deterministic flag is inconsistent"
        )
    expected_receipt_sha256 = stable_variant_sha256(
        "runtime-variant-selection-receipt-v1",
        {
            key: item
            for key, item in value.items()
            if key != "receipt_sha256"
        },
    )
    if _require_sha256(
        value["receipt_sha256"],
        "selection_receipt.receipt_sha256",
    ) != expected_receipt_sha256:
        raise RuntimeVariantError(
            "selection_receipt self hash is invalid"
        )
    return value


def _selection_receipt_semantic_projection(
    receipt: dict[str, Any],
) -> dict[str, Any]:
    catalogs = []
    catalog_by_sha: dict[str, dict[str, Any]] = {}
    for record in receipt["catalogs"]:
        catalog = {
            key: item
            for key, item in record["catalog"].items()
            if key != "condition_sha256"
        }
        catalogs.append({"catalog": catalog})
        catalog_by_sha[record["catalog_sha256"]] = catalog
    selections = []
    for selection in receipt["selections"]:
        projected_selection = {
            key: item
            for key, item in selection.items()
            if key not in {"condition_sha256", "catalog_sha256"}
        }
        projected_selection["catalog"] = catalog_by_sha[
            selection["catalog_sha256"]
        ]
        selections.append(projected_selection)
    projected = {
        key: item
        for key, item in receipt.items()
        if key not in {"catalogs", "selections", "receipt_sha256"}
    }
    projected["catalogs"] = catalogs
    projected["selections"] = selections
    return projected


def runtime_variant_selection_receipts_match(
    left: Any,
    right: Any,
) -> bool:
    """Compare two strict receipts by JSON-number semantics, not byte hashes."""

    left_receipt = validate_runtime_variant_selection_receipt(left)
    right_receipt = validate_runtime_variant_selection_receipt(right)
    left_projection = _selection_receipt_semantic_projection(left_receipt)
    right_projection = _selection_receipt_semantic_projection(right_receipt)
    left_catalogs = left_projection.pop("catalogs")
    unmatched_catalogs = list(right_projection.pop("catalogs"))
    if not _json_values_match(left_projection, right_projection):
        return False
    for catalog in left_catalogs:
        match = next(
            (
                index
                for index, candidate in enumerate(unmatched_catalogs)
                if _json_values_match(catalog, candidate)
            ),
            None,
        )
        if match is None:
            return False
        unmatched_catalogs.pop(match)
    return not unmatched_catalogs


def _validate_declared_top_level_contract(
    contract: Any,
) -> dict[str, Any]:
    """Validate the common declaration emitted by a leaf built-in backend."""

    if contract is None:
        raise RuntimeVariantError(
            "trusted top-level backend omitted its runtime variant contract"
        )
    safe = _canonical_copy(contract)
    value = _expect_exact_keys(
        safe,
        {
            "schema_version",
            "kind",
            "backend",
            "audio_selection_model",
            "capture_completeness",
            "expected_component_sha256s",
            "expected_selection_count",
        },
        "top_level_contract",
    )
    _require_schema_version(
        value["schema_version"],
        "top-level contract schema_version",
    )
    if value["kind"] != "top_level_runtime_variant_contract":
        raise RuntimeVariantError("top-level contract kind is invalid")
    if not isinstance(value["expected_component_sha256s"], list):
        raise RuntimeVariantError(
            "top-level contract expected components must be an array"
        )
    component_hashes = [
        _require_sha256(item, "top-level expected component")
        for item in value["expected_component_sha256s"]
    ]
    if component_hashes != sorted(set(component_hashes)):
        raise RuntimeVariantError(
            "leaf top-level expected components must be unique and sorted"
        )
    if (
        isinstance(value["expected_selection_count"], bool)
        or not isinstance(value["expected_selection_count"], int)
        or value["expected_selection_count"] < 0
    ):
        raise RuntimeVariantError(
            "top-level expected_selection_count must be non-negative"
        )
    return value


def _validate_embedded_top_level_contract_scalar_fields(
    contract: Any,
    *,
    label: str,
) -> dict[str, Any]:
    """Validate typed scalars carried by a portable proof contract."""

    if not isinstance(contract, dict):
        raise RuntimeVariantError(f"{label} must be an object")
    _require_schema_version(
        contract.get("schema_version"),
        f"{label}.schema_version",
    )
    _require_integer(
        contract.get("expected_selection_count"),
        f"{label}.expected_selection_count",
        minimum=0,
    )

    attack = contract.get("attack_phase_contract")
    if attack is not None:
        if not isinstance(attack, dict):
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract must be an object"
            )
        public_note_id = _require_integer(
            attack.get("public_note_id"),
            f"{label}.attack_phase_contract.public_note_id",
            minimum=0,
        )
        if public_note_id != 1:
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.public_note_id must be 1"
            )
        _require_integer(
            attack.get("note_on_sequence"),
            f"{label}.attack_phase_contract.note_on_sequence",
            minimum=0,
        )
        _require_finite_number(
            attack.get("selector_random_value"),
            f"{label}.attack_phase_contract.selector_random_value",
        )
        bindings = attack.get("ordered_layer_bindings")
        if not isinstance(bindings, list):
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.ordered_layer_bindings "
                "must be an array"
            )
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                raise RuntimeVariantError(
                    f"{label}.attack_phase_contract.ordered_layer_bindings"
                    f"[{index}] must be an object"
                )
            for field in ("selection_index", "layer_index"):
                _require_integer(
                    binding.get(field),
                    f"{label}.attack_phase_contract.ordered_layer_bindings"
                    f"[{index}].{field}",
                    minimum=0,
                )
            for field in (
                "wrapper_velocity_gain",
                "effective_static_gain",
            ):
                _require_finite_number(
                    binding.get(field),
                    f"{label}.attack_phase_contract.ordered_layer_bindings"
                    f"[{index}].{field}",
                )
            for field in (
                "route_retained",
                "static_audio_contributing",
            ):
                _require_boolean(
                    binding.get(field),
                    f"{label}.attack_phase_contract.ordered_layer_bindings"
                    f"[{index}].{field}",
                )
            for field in (
                "wrapper_role_sha256",
                "component_sha256",
                "catalog_sha256",
                "choice_sha256",
                "wrapper_outcome_sha256",
            ):
                _require_sha256(
                    binding.get(field),
                    f"{label}.attack_phase_contract.ordered_layer_bindings"
                    f"[{index}].{field}",
                )
        for field in (
            "retained_layer_indexes",
            "static_audio_contributing_layer_indexes",
        ):
            indexes = attack.get(field)
            if not isinstance(indexes, list):
                raise RuntimeVariantError(
                    f"{label}.attack_phase_contract.{field} must be an array"
                )
            for index, item in enumerate(indexes):
                _require_integer(
                    item,
                    f"{label}.attack_phase_contract.{field}[{index}]",
                    minimum=0,
                )

    cycle = contract.get("finite_rr_cycle_contract")
    if cycle is not None:
        if not isinstance(cycle, dict):
            raise RuntimeVariantError(
                f"{label}.finite_rr_cycle_contract must be an object"
            )
        period = _require_integer(
            cycle.get("variation_period"),
            f"{label}.finite_rr_cycle_contract.variation_period",
            minimum=2,
            maximum=MAX_NATURAL_FINITE_RR_VARIANTS,
        )
        _require_integer(
            cycle.get("variation_slot"),
            f"{label}.finite_rr_cycle_contract.variation_slot",
            minimum=0,
            maximum=period - 1,
        )
        counts = cycle.get("ordered_layer_candidate_counts")
        if not isinstance(counts, list):
            raise RuntimeVariantError(
                f"{label}.finite_rr_cycle_contract."
                "ordered_layer_candidate_counts must be an array"
            )
        for index, count in enumerate(counts):
            _require_integer(
                count,
                f"{label}.finite_rr_cycle_contract."
                f"ordered_layer_candidate_counts[{index}]",
                minimum=1,
            )
        _require_sha256(
            cycle.get("slot_bundle_sha256"),
            f"{label}.finite_rr_cycle_contract.slot_bundle_sha256",
        )
    return contract


def _validate_embedded_top_level_contract_hash_bindings(
    contract: dict[str, Any],
    *,
    selection_receipt: dict[str, Any],
    label: str,
) -> None:
    """Recompute every hash derived from an embedded attack contract.

    The enclosing contract/proof hashes protect the stored JSON bytes, but do
    not by themselves prove that nested bundle hashes describe those bytes.
    Each representation validates its own derived hashes here before live
    semantic comparison is allowed to ignore the two number-sensitive bundle
    hashes.
    """

    attack = contract.get("attack_phase_contract")
    if attack is None:
        return
    assert isinstance(attack, dict)
    bindings = attack["ordered_layer_bindings"]
    selections = selection_receipt["selections"]
    if (
        contract["expected_selection_count"] != len(bindings)
        or len(bindings) != len(selections)
    ):
        raise RuntimeVariantError(
            f"{label}.attack_phase_contract binding count is inconsistent"
        )

    selection_indexes: list[int] = []
    for index, binding in enumerate(bindings):
        selection_index = binding["selection_index"]
        selection_indexes.append(selection_index)
        if not 0 <= selection_index < len(selections):
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.ordered_layer_bindings"
                f"[{index}].selection_index is outside the receipt"
            )
        wrapper_outcome = selections[selection_index]["wrapper_outcome"]
        if wrapper_outcome is None:
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.ordered_layer_bindings"
                f"[{index}] has no receipt wrapper outcome"
            )
        expected_wrapper_hash = stable_variant_sha256(
            "dedicated-sfz-wrapper-outcome-v1",
            wrapper_outcome,
        )
        if _require_sha256(
            binding.get("wrapper_outcome_sha256"),
            f"{label}.attack_phase_contract.ordered_layer_bindings"
            f"[{index}].wrapper_outcome_sha256",
        ) != expected_wrapper_hash:
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.ordered_layer_bindings"
                f"[{index}].wrapper_outcome_sha256 does not bind its receipt"
            )
    if selection_indexes != list(range(len(bindings))):
        raise RuntimeVariantError(
            f"{label}.attack_phase_contract selection indexes are not contiguous"
        )

    retained_bundle = [
        binding for binding in bindings if binding["route_retained"]
    ]
    static_audio_bundle = [
        binding
        for binding in bindings
        if binding["static_audio_contributing"]
    ]
    expected_retained_indexes = [
        binding["layer_index"] for binding in retained_bundle
    ]
    expected_static_indexes = [
        binding["layer_index"] for binding in static_audio_bundle
    ]
    if attack["retained_layer_indexes"] != expected_retained_indexes:
        raise RuntimeVariantError(
            f"{label}.attack_phase_contract retained indexes are inconsistent"
        )
    if (
        attack["static_audio_contributing_layer_indexes"]
        != expected_static_indexes
    ):
        raise RuntimeVariantError(
            f"{label}.attack_phase_contract static-audio indexes are inconsistent"
        )

    for field, algorithm, bundle in (
        (
            "retained_bundle_sha256",
            "dedicated-sfz-retained-attack-bundle-v1",
            retained_bundle,
        ),
        (
            "static_audio_bundle_sha256",
            "dedicated-sfz-static-audio-attack-bundle-v1",
            static_audio_bundle,
        ),
    ):
        expected_hash = stable_variant_sha256(algorithm, bundle)
        if _require_sha256(
            attack.get(field),
            f"{label}.attack_phase_contract.{field}",
        ) != expected_hash:
            raise RuntimeVariantError(
                f"{label}.attack_phase_contract.{field} is invalid"
            )

    cycle = contract.get("finite_rr_cycle_contract")
    if cycle is None:
        return
    assert isinstance(cycle, dict)
    slot_bundle = [
        {
            "layer_index": binding["layer_index"],
            "choice_sha256": binding["choice_sha256"],
            "route_retained": binding["route_retained"],
            "wrapper_outcome_sha256": binding["wrapper_outcome_sha256"],
        }
        for binding in bindings
    ]
    expected_slot_hash = stable_variant_sha256(
        "dedicated-sfz-finite-rr-slot-bundle-v1",
        slot_bundle,
    )
    if _require_sha256(
        cycle.get("slot_bundle_sha256"),
        f"{label}.finite_rr_cycle_contract.slot_bundle_sha256",
    ) != expected_slot_hash:
        raise RuntimeVariantError(
            f"{label}.finite_rr_cycle_contract.slot_bundle_sha256 is invalid"
        )


def _validate_builtin_factory_provenance(
    instrument: Any,
    manifest: dict[str, Any],
    *,
    sampled_condition: dict[str, Any],
) -> None:
    """Bind a certification call to the manifest that built this instance."""

    from .instrument import factory_manifest_sha256

    provenance = getattr(
        instrument,
        "_tianlai_factory_provenance",
        None,
    )
    if provenance is None:
        raise RuntimeVariantNotCertifiable(
            "trusted built-in backend lacks factory manifest provenance"
        )
    value = _expect_exact_keys(
        _canonical_copy(provenance),
        {
            "schema_version",
            "manifest_sha256",
            "sample_rate_hz",
            "factory_route",
        },
        "factory_provenance",
    )
    _require_schema_version(
        value["schema_version"],
        "factory provenance schema_version",
    )
    if value["factory_route"] != (
        "builtin_manifest_dispatch_no_implementation"
    ):
        raise RuntimeVariantNotCertifiable(
            "backend did not originate from the trusted built-in manifest route"
        )
    expected_manifest_sha256 = factory_manifest_sha256(manifest)
    if _require_sha256(
        value["manifest_sha256"],
        "factory provenance manifest_sha256",
    ) != expected_manifest_sha256:
        raise RuntimeVariantNotCertifiable(
            "certification manifest differs from the instance construction manifest"
        )
    sample_rate = value["sample_rate_hz"]
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate != sampled_condition["sample_rate_hz"]
        or sample_rate != getattr(instrument, "sample_rate", None)
    ):
        raise RuntimeVariantNotCertifiable(
            "factory provenance sample rate differs from the sampled condition"
        )


def _trusted_top_level_contract(
    instrument: Any,
    manifest: Any,
    *,
    sampled_condition: dict[str, Any],
    selection_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact allow-listed backend declaration.

    Exact type equality is intentional: wrappers and local subclasses may
    perform another RR/layer/model choice outside a captured SampleInstrument.
    They remain uncertifiable until they receive their own exhaustive contract.
    """

    from .dedicated_sfz import DedicatedSfzInstrument
    from .oscillator import OscillatorInstrument
    from .sampler import SampleInstrument

    if not isinstance(manifest, dict):
        raise RuntimeVariantNotCertifiable(
            "instrument manifest provenance is unavailable"
        )
    if manifest.get("implementation") is not None:
        raise RuntimeVariantNotCertifiable(
            "local implementation factories cannot certify built-in backend provenance"
        )
    if type(instrument) is DedicatedSfzInstrument:
        if manifest.get("type") != "dedicated_sfz":
            raise RuntimeVariantNotCertifiable(
                "built-in DedicatedSfzInstrument instance did not originate "
                "from a dedicated_sfz manifest route"
            )
        return _trusted_dedicated_sfz_attack_contract(
            instrument,
            manifest,
            sampled_condition=sampled_condition,
            selection_receipt=selection_receipt,
        )
    if type(instrument) not in {OscillatorInstrument, SampleInstrument}:
        raise RuntimeVariantNotCertifiable(
            "top-level backend has no trusted complete audio-selection contract"
        )
    _validate_builtin_factory_provenance(
        instrument,
        manifest,
        sampled_condition=sampled_condition,
    )
    value = _validate_declared_top_level_contract(
        instrument.runtime_variant_contract()
    )
    component_hashes = value["expected_component_sha256s"]
    if type(instrument) is OscillatorInstrument:
        if manifest.get("type") != "oscillator":
            raise RuntimeVariantNotCertifiable(
                "built-in oscillator instance did not originate from an "
                "oscillator manifest route"
            )
        expected = {
            "schema_version": 1,
            "kind": "top_level_runtime_variant_contract",
            "backend": "builtin_oscillator",
            "audio_selection_model": (
                "code_deterministic_no_runtime_choices"
            ),
            "capture_completeness": "no_audio_selection_components",
            "expected_component_sha256s": [],
            "expected_selection_count": 0,
        }
    else:
        if manifest.get("type") != "sample":
            raise RuntimeVariantNotCertifiable(
                "built-in sample instance did not originate from a sample "
                "manifest route"
            )
        if len(component_hashes) != 1:
            raise RuntimeVariantError(
                "built-in sample contract must declare exactly one component"
            )
        expected = {
            "schema_version": 1,
            "kind": "top_level_runtime_variant_contract",
            "backend": "builtin_sample_instrument",
            "audio_selection_model": "sample_region_selector_v1",
            "capture_completeness": (
                "all_audio_selection_delegated_to_runtime_variant_capture"
            ),
            "expected_component_sha256s": component_hashes,
            "expected_selection_count": 1,
        }
    if not _json_values_match(value, expected):
        raise RuntimeVariantError(
            "trusted top-level backend returned an inconsistent contract"
        )
    return {
        **value,
        "manifest_type": str(manifest["type"]),
        "factory_route": "builtin_manifest_dispatch_no_implementation",
    }


def _validate_sample_choice_integer_fields(
    raw_choice: Any,
    *,
    label: str,
) -> dict[str, Any]:
    choice = _expect_exact_keys(
        raw_choice,
        {
            "choice_sha256",
            "catalog_position",
            "random_min",
            "random_max",
            "round_robin_position",
            "round_robin_length",
            "jitter",
        },
        label,
    )
    _require_integer(
        choice["catalog_position"],
        f"{label}.catalog_position",
        minimum=0,
    )
    position = choice["round_robin_position"]
    length = choice["round_robin_length"]
    if (position is None) != (length is None):
        raise RuntimeVariantError(
            f"{label} must define both round-robin position and length"
        )
    if position is not None:
        position = _require_integer(
            position,
            f"{label}.round_robin_position",
            minimum=1,
        )
        length = _require_integer(
            length,
            f"{label}.round_robin_length",
            minimum=1,
        )
        if position > length:
            raise RuntimeVariantError(
                f"{label}.round_robin_position exceeds its length"
            )
    return choice


def _validate_sample_catalog_condition(
    catalog: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    condition = _expect_exact_keys(
        catalog["condition"],
        {
            "pitch_hz",
            "target_midi",
            "velocity",
            "pitch_bucket",
            "velocity_bucket",
        },
        label,
    )
    for field in ("pitch_hz", "target_midi", "velocity"):
        _require_finite_number(
            condition[field],
            f"{label}.{field}",
        )
    for field in ("pitch_bucket", "velocity_bucket"):
        _require_integer(
            condition[field],
            f"{label}.{field}",
        )
    if _require_sha256(
        catalog["condition_sha256"],
        f"{label} hash",
    ) != stable_variant_sha256(
        "sample-region-condition-v1",
        condition,
    ):
        raise RuntimeVariantError(
            f"{label} hash is invalid"
        )
    return condition


def _validate_sample_actual_selector_integer_fields(
    raw_selector: Any,
    *,
    label: str,
) -> dict[str, Any]:
    selector = _expect_exact_keys(
        raw_selector,
        {
            "random_value",
            "round_robin_counter_before",
            "candidate_count",
            "candidate_index",
        },
        label,
    )
    _require_finite_number(
        selector["random_value"],
        f"{label}.random_value",
    )
    _require_integer(
        selector["round_robin_counter_before"],
        f"{label}.round_robin_counter_before",
        minimum=0,
    )
    candidate_count = _require_integer(
        selector["candidate_count"],
        f"{label}.candidate_count",
        minimum=1,
    )
    candidate_index = _require_integer(
        selector["candidate_index"],
        f"{label}.candidate_index",
        minimum=0,
    )
    if candidate_index >= candidate_count:
        raise RuntimeVariantError(
            f"{label}.candidate_index must be below candidate_count"
        )
    return selector


def _validate_sample_catalog_numeric_fields(
    catalog: dict[str, Any],
    *,
    label: str,
) -> None:
    selector_domain = _expect_exact_keys(
        catalog["selector_domain"],
        {"name", "minimum", "maximum", "bounds"},
        f"{label}.selector_domain",
    )
    for field in ("minimum", "maximum"):
        _require_finite_number(
            selector_domain[field],
            f"{label}.selector_domain.{field}",
        )

    for choice_index, raw_choice in enumerate(catalog["choices"]):
        choice_label = f"{label}.choices[{choice_index}]"
        choice = _validate_sample_choice_integer_fields(
            raw_choice,
            label=choice_label,
        )
        for field in ("random_min", "random_max"):
            _require_finite_number(
                choice[field],
                f"{choice_label}.{field}",
            )
        jitter = _expect_exact_keys(
            choice["jitter"],
            {
                "pitch_random_cents",
                "amplitude_random_db",
                "delay_random_seconds",
            },
            f"{choice_label}.jitter",
        )
        for field, value in jitter.items():
            _require_finite_number(
                value,
                f"{choice_label}.jitter.{field}",
            )

    for partition_index, raw_partition in enumerate(catalog["partitions"]):
        partition_label = f"{label}.partitions[{partition_index}]"
        if not isinstance(raw_partition, dict):
            raise RuntimeVariantError(
                f"{partition_label} must be an object"
            )
        kind = raw_partition.get("kind")
        required = (
            {"kind", "probe_value", "status", "choice_sha256s", "value"}
            if kind == "point"
            else {
                "kind",
                "probe_value",
                "status",
                "choice_sha256s",
                "minimum",
                "maximum",
            }
        )
        partition = _expect_exact_keys(
            raw_partition,
            required,
            partition_label,
        )
        if kind not in {"point", "open_interval"}:
            raise RuntimeVariantError(
                f"{partition_label}.kind is invalid"
            )
        _require_finite_number(
            partition["probe_value"],
            f"{partition_label}.probe_value",
        )
        numeric_fields = (
            ("value",)
            if kind == "point"
            else ("minimum", "maximum")
        )
        for field in numeric_fields:
            _require_finite_number(
                partition[field],
                f"{partition_label}.{field}",
            )

    for domain_index, raw_domain in enumerate(
        catalog["unexhausted_domains"]
    ):
        domain_label = f"{label}.unexhausted_domains[{domain_index}]"
        domain = _expect_exact_keys(
            raw_domain,
            {
                "domain",
                "choice_sha256",
                "minimum",
                "maximum",
                "exhaustive",
            },
            domain_label,
        )
        if not isinstance(domain["domain"], str) or not domain["domain"]:
            raise RuntimeVariantError(
                f"{domain_label}.domain must be non-empty text"
            )
        _require_sha256(
            domain["choice_sha256"],
            f"{domain_label}.choice_sha256",
        )
        for field in ("minimum", "maximum"):
            _require_finite_number(
                domain[field],
                f"{domain_label}.{field}",
            )
        if _require_boolean(
            domain["exhaustive"],
            f"{domain_label}.exhaustive",
        ):
            raise RuntimeVariantError(
                f"{domain_label}.exhaustive must be false"
            )


def _validate_sample_single_catalog(
    catalog: dict[str, Any],
    selection: dict[str, Any],
    *,
    expected_pitch_hz: float,
    expected_midi: float,
    expected_velocity: float,
    expected_random_value: float | None = None,
) -> None:
    if catalog["algorithm"] != "sample-select-region-partition-v1":
        raise RuntimeVariantNotCertifiable(
            "sample selector catalog algorithm is not certifiable"
        )
    condition = _validate_sample_catalog_condition(
        catalog,
        label="sample catalog condition",
    )
    expected_catalog_condition = {
        "pitch_hz": float(expected_pitch_hz),
        "target_midi": float(expected_midi),
        "velocity": float(expected_velocity),
        "pitch_bucket": round(expected_midi),
        "velocity_bucket": round(expected_velocity * 127.0),
    }
    if not _json_values_match(condition, expected_catalog_condition):
        raise RuntimeVariantNotCertifiable(
            "sample catalog belongs to another pitch/velocity condition"
        )
    selector_domain = _expect_exact_keys(
        catalog["selector_domain"],
        {"name", "minimum", "maximum", "bounds"},
        "sample catalog selector_domain",
    )
    if not _json_values_match(
        selector_domain,
        {
            "name": "_sample_random_value",
            "minimum": 0.0,
            "maximum": 1.0,
            "bounds": "closed",
        },
    ):
        raise RuntimeVariantNotCertifiable(
            "sample selector domain is not the exhaustive closed [0,1] domain"
        )
    if catalog["deterministic_single"] is not True:
        raise RuntimeVariantNotCertifiable(
            "sample condition has more than one runtime variant"
        )
    if catalog["has_selector_gaps"] is not False:
        raise RuntimeVariantNotCertifiable(
            "sample selector has an uncovered random-domain gap"
        )
    if catalog["unexhausted_domains"]:
        raise RuntimeVariantNotCertifiable(
            "sample condition contains an unexhausted random/jitter domain"
        )
    if len(catalog["choices"]) != 1:
        raise RuntimeVariantNotCertifiable(
            "sample condition does not have exactly one reachable choice"
        )
    choice = _validate_sample_choice_integer_fields(
        catalog["choices"][0],
        label="sample catalog sole choice",
    )
    sole_choice = choice["choice_sha256"]
    for field in ("random_min", "random_max"):
        if (
            isinstance(choice[field], bool)
            or not isinstance(choice[field], Real)
            or not math.isfinite(float(choice[field]))
        ):
            raise RuntimeVariantError(
                f"sample choice {field} must be finite"
            )
    jitter = _expect_exact_keys(
        choice["jitter"],
        {
            "pitch_random_cents",
            "amplitude_random_db",
            "delay_random_seconds",
        },
        "sample choice jitter",
    )
    if any(
        isinstance(item, bool)
        or not isinstance(item, Real)
        or not math.isfinite(float(item))
        or float(item) != 0.0
        for item in jitter.values()
    ):
        raise RuntimeVariantNotCertifiable(
            "sample choice has a random jitter domain"
        )
    if selection["choice_sha256"] != sole_choice:
        raise RuntimeVariantError(
            "sample selection differs from its sole catalog choice"
        )
    partitions = catalog["partitions"]
    if not partitions:
        raise RuntimeVariantNotCertifiable(
            "sample selector catalog has no domain partitions"
        )
    for index, partition in enumerate(partitions):
        if not isinstance(partition, dict):
            raise RuntimeVariantError(
                f"sample selector partition {index} must be an object"
            )
        kind = partition.get("kind")
        required = (
            {"kind", "probe_value", "status", "choice_sha256s", "value"}
            if kind == "point"
            else {
                "kind",
                "probe_value",
                "status",
                "choice_sha256s",
                "minimum",
                "maximum",
            }
        )
        _expect_exact_keys(
            partition,
            required,
            f"sample selector partition {index}",
        )
        if kind not in {"point", "open_interval"}:
            raise RuntimeVariantError(
                f"sample selector partition {index} kind is invalid"
            )
        partition_label = f"sample selector partition {index}"
        _require_finite_number(
            partition["probe_value"],
            f"{partition_label}.probe_value",
        )
        if kind == "point":
            _require_finite_number(
                partition["value"],
                f"{partition_label}.value",
            )
        else:
            _require_finite_number(
                partition["minimum"],
                f"{partition_label}.minimum",
            )
            _require_finite_number(
                partition["maximum"],
                f"{partition_label}.maximum",
            )
        if partition.get("status") != "choices" or partition.get(
            "choice_sha256s"
        ) != [sole_choice]:
            raise RuntimeVariantNotCertifiable(
                "sample selector domain is not deterministic-single everywhere"
            )
    selector = _validate_sample_actual_selector_integer_fields(
        selection["actual_selector"],
        label="sample actual_selector",
    )
    candidate_count = selector["candidate_count"]
    candidate_index = selector["candidate_index"]
    round_robin_counter = selector["round_robin_counter_before"]
    if (
        candidate_count != 1
        or candidate_index != 0
        or round_robin_counter != 0
    ):
        raise RuntimeVariantNotCertifiable(
            "sample observation consumed RR/multiple candidates or prior state"
        )
    random_value = selector["random_value"]
    if (
        isinstance(random_value, bool)
        or not isinstance(random_value, Real)
        or not math.isfinite(float(random_value))
        or not 0.0 <= float(random_value) <= 1.0
    ):
        raise RuntimeVariantError(
            "sample actual selector random_value is invalid"
        )
    if (
        expected_random_value is not None
        and float(random_value) != float(expected_random_value)
    ):
        raise RuntimeVariantNotCertifiable(
            "sample actual selector belongs to another event identity or phase"
        )
    matching_partitions = []
    for partition in partitions:
        if partition["kind"] == "point":
            matches = float(random_value) == float(partition["value"])
        else:
            matches = (
                float(partition["minimum"])
                < float(random_value)
                < float(partition["maximum"])
            )
        if matches:
            matching_partitions.append(partition)
    if len(matching_partitions) != 1:
        raise RuntimeVariantError(
            "sample actual selector does not map to exactly one catalog partition"
        )
    if matching_partitions[0]["choice_sha256s"] != [
        selection["choice_sha256"]
    ]:
        raise RuntimeVariantError(
            "sample actual selector partition differs from the selected choice"
        )


def _finite_rr_catalog_choice_cycle(
    catalog: dict[str, Any],
) -> list[str]:
    """Return one finite RR cycle, rejecting random partitions and jitter."""

    if catalog["algorithm"] != "sample-select-region-partition-v1":
        raise RuntimeVariantNotCertifiable(
            "sample selector catalog algorithm is not certifiable"
        )
    if catalog["has_selector_gaps"] is not False:
        raise RuntimeVariantNotCertifiable(
            "finite RR selector has an uncovered random-domain gap"
        )
    if catalog["unexhausted_domains"]:
        raise RuntimeVariantNotCertifiable(
            "finite RR selector contains a continuous jitter domain"
        )
    partitions = catalog["partitions"]
    if not partitions:
        raise RuntimeVariantNotCertifiable(
            "finite RR selector catalog has no domain partitions"
        )
    cycle: list[str] | None = None
    for index, partition in enumerate(partitions):
        if not isinstance(partition, dict):
            raise RuntimeVariantError(
                f"finite RR selector partition {index} must be an object"
            )
        kind = partition.get("kind")
        required = (
            {"kind", "probe_value", "status", "choice_sha256s", "value"}
            if kind == "point"
            else {
                "kind",
                "probe_value",
                "status",
                "choice_sha256s",
                "minimum",
                "maximum",
            }
        )
        _expect_exact_keys(
            partition,
            required,
            f"finite RR selector partition {index}",
        )
        if kind not in {"point", "open_interval"}:
            raise RuntimeVariantError(
                f"finite RR selector partition {index} kind is invalid"
            )
        partition_label = f"finite RR selector partition {index}"
        _require_finite_number(
            partition["probe_value"],
            f"{partition_label}.probe_value",
        )
        if kind == "point":
            _require_finite_number(
                partition["value"],
                f"{partition_label}.value",
            )
        else:
            _require_finite_number(
                partition["minimum"],
                f"{partition_label}.minimum",
            )
            _require_finite_number(
                partition["maximum"],
                f"{partition_label}.maximum",
            )
        choices = partition["choice_sha256s"]
        if partition["status"] != "choices" or not isinstance(
            choices, list
        ) or not choices:
            raise RuntimeVariantNotCertifiable(
                "finite RR selector domain is not fully playable"
            )
        normalized = [
            _require_sha256(
                choice,
                f"finite RR selector partition {index} choice",
            )
            for choice in choices
        ]
        if len(normalized) != len(set(normalized)):
            raise RuntimeVariantError(
                "finite RR selector partition repeats a choice"
            )
        if cycle is None:
            cycle = normalized
        elif normalized != cycle:
            # Different random-domain partitions require a separate steered
            # enumeration protocol.  The natural prewarm protocol below only
            # certifies a selector-independent finite RR cycle.
            raise RuntimeVariantNotCertifiable(
                "random-partition choices require audit-steered enumeration"
            )
    assert cycle is not None
    validated_choices = [
        _validate_sample_choice_integer_fields(
            choice,
            label=f"finite RR catalog choice {index}",
        )
        for index, choice in enumerate(catalog["choices"])
    ]
    catalog_choices = {
        _require_sha256(
            choice["choice_sha256"],
            "finite RR catalog choice",
        )
        for choice in validated_choices
    }
    if set(cycle) != catalog_choices:
        raise RuntimeVariantNotCertifiable(
            "finite RR catalog choices differ from its domain cycle"
        )
    for choice in validated_choices:
        jitter = choice["jitter"]
        if not isinstance(jitter, dict) or any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            or float(value) != 0.0
            for value in jitter.values()
        ):
            raise RuntimeVariantNotCertifiable(
                "finite RR catalog contains random jitter"
            )
    return cycle


def _validate_sample_finite_rr_catalog(
    catalog: dict[str, Any],
    selection: dict[str, Any],
    *,
    expected_catalog: dict[str, Any],
    expected_random_value: float,
    variation_slot: int,
) -> int:
    _validate_sample_catalog_condition(
        catalog,
        label="finite RR catalog condition",
    )
    if not _sample_catalogs_match(catalog, expected_catalog):
        raise RuntimeVariantNotCertifiable(
            "finite RR receipt catalog differs from the live exact layer catalog"
        )
    cycle = _finite_rr_catalog_choice_cycle(catalog)
    selector = _validate_sample_actual_selector_integer_fields(
        selection["actual_selector"],
        label="finite RR actual_selector",
    )
    random_value = _require_finite_number(
        selector["random_value"],
        "finite RR actual_selector.random_value",
    )
    if random_value != float(expected_random_value):
        raise RuntimeVariantNotCertifiable(
            "finite RR selector belongs to another event identity or phase"
        )
    expected_index = variation_slot % len(cycle)
    round_robin_counter = selector["round_robin_counter_before"]
    candidate_count = selector["candidate_count"]
    candidate_index = selector["candidate_index"]
    if (
        round_robin_counter != variation_slot
        or candidate_count != len(cycle)
        or candidate_index != expected_index
        or selection["choice_sha256"] != cycle[expected_index]
    ):
        raise RuntimeVariantNotCertifiable(
            "finite RR receipt does not match its natural prewarm cycle slot"
        )
    return len(cycle)


def _exact_sample_component_sha256(engine: Any) -> str:
    """Return one nested exact SampleInstrument component identity."""

    from .sampler import SampleInstrument

    if type(engine) is not SampleInstrument:
        raise RuntimeVariantNotCertifiable(
            "Dedicated SFZ attack layer is not an exact SampleInstrument"
        )
    contract = _validate_declared_top_level_contract(
        engine.runtime_variant_contract()
    )
    component_hashes = contract["expected_component_sha256s"]
    expected = {
        "schema_version": 1,
        "kind": "top_level_runtime_variant_contract",
        "backend": "builtin_sample_instrument",
        "audio_selection_model": "sample_region_selector_v1",
        "capture_completeness": (
            "all_audio_selection_delegated_to_runtime_variant_capture"
        ),
        "expected_component_sha256s": component_hashes,
        "expected_selection_count": 1,
    }
    if len(component_hashes) != 1 or not _json_values_match(
        contract,
        expected,
    ):
        raise RuntimeVariantError(
            "Dedicated SFZ nested sample layer returned an inconsistent contract"
        )
    return component_hashes[0]


def _trusted_dedicated_sfz_attack_contract(
    instrument: Any,
    manifest: dict[str, Any],
    *,
    sampled_condition: dict[str, Any],
    selection_receipt: dict[str, Any],
    finite_rr_slot: int | None = None,
) -> dict[str, Any]:
    """Reconstruct the exact Dedicated SFZ note-on attack bundle.

    ``DedicatedSfzInstrument._trigger_layers`` asks every attack-layer sampler
    to select a region and may then discard that voice because the selected
    key/velocity does not really cover the requested condition, or because an
    SFZ crossfade produces zero gain.  Therefore a flat list of nested sample
    receipts is not a completeness proof.  This adapter binds the receipt to
    the exact ordered attack layers, regenerates each live catalog, and records
    both the consumed selectors and the subset retained by the wrapper.
    """

    from .events import PerformanceEvent, event_pitch_hz
    from .tuning import EqualTemperament

    _validate_builtin_factory_provenance(
        instrument,
        manifest,
        sampled_condition=sampled_condition,
    )
    reported_articulation = sampled_condition["final_articulation"]
    if reported_articulation == "__default__":
        resolved_articulation = instrument.default_articulation
        note_on_sequence = 0
    else:
        resolved_articulation = reported_articulation
        note_on_sequence = 1
    runtime = instrument.articulations.get(resolved_articulation)
    if runtime is None:
        raise RuntimeVariantNotCertifiable(
            "sampled articulation is absent from the live Dedicated SFZ backend"
        )
    attack_layers = runtime.attack_layers
    if not attack_layers:
        raise RuntimeVariantNotCertifiable(
            "Dedicated SFZ articulation has no attack layers"
        )
    selections = selection_receipt["selections"]
    if len(selections) != len(attack_layers):
        raise RuntimeVariantNotCertifiable(
            "capture did not observe every Dedicated SFZ note-on attack layer"
        )

    tuning = EqualTemperament()
    source_event = PerformanceEvent(
        sample=0,
        sequence=note_on_sequence,
        type="note_on",
        payload={
            "note_id": 1,
            "midi_note": float(sampled_condition["midi_note"]),
            "velocity": float(sampled_condition["velocity"]) / 127.0,
        },
    )
    previous_articulation = instrument.articulation
    try:
        instrument.articulation = resolved_articulation
        playback_payload = instrument._playback_payload(
            source_event,
            tuning,
        )
    finally:
        instrument.articulation = previous_articulation

    target_midi = float(playback_payload["midi_note"])
    velocity = float(playback_payload["velocity"])
    layer_event = PerformanceEvent(
        sample=0,
        sequence=note_on_sequence,
        type="note_on",
        payload=playback_payload,
    )
    pitch_hz = event_pitch_hz(layer_event, tuning)
    expected_random_value = float(
        playback_payload["_sample_random_value"]
    )
    catalog_by_sha = {
        record["catalog_sha256"]: record["catalog"]
        for record in selection_receipt["catalogs"]
    }

    ordered_components: list[str] = []
    bindings: list[dict[str, Any]] = []
    retained_indexes: list[int] = []
    static_audio_indexes: list[int] = []
    finite_rr_candidate_counts: list[int] = []
    half_velocity_step = 0.5 / 127.0
    for layer_index, (layer, selection) in enumerate(
        zip(attack_layers, selections, strict=True)
    ):
        engine = layer.engine
        component_sha256 = _exact_sample_component_sha256(engine)
        ordered_components.append(component_sha256)
        if selection["selection_index"] != layer_index:
            raise RuntimeVariantError(
                "Dedicated SFZ attack receipt selection order is invalid"
            )
        if selection["component_sha256"] != component_sha256:
            raise RuntimeVariantNotCertifiable(
                "Dedicated SFZ attack receipt component is bound to another layer"
            )
        catalog = catalog_by_sha[selection["catalog_sha256"]]
        expected_catalog = engine._runtime_variant_catalog(
            pitch_hz=pitch_hz,
            velocity=velocity,
            target_midi=target_midi,
        )
        if not _sample_catalogs_match(catalog, expected_catalog):
            raise RuntimeVariantNotCertifiable(
                "Dedicated SFZ attack receipt catalog differs from the live "
                "exact layer catalog for this condition"
            )
        if finite_rr_slot is None:
            _validate_sample_single_catalog(
                catalog,
                selection,
                expected_pitch_hz=pitch_hz,
                expected_midi=target_midi,
                expected_velocity=velocity,
                expected_random_value=expected_random_value,
            )
        else:
            finite_rr_candidate_counts.append(
                _validate_sample_finite_rr_catalog(
                    catalog,
                    selection,
                    expected_catalog=expected_catalog,
                    expected_random_value=expected_random_value,
                    variation_slot=finite_rr_slot,
                )
            )

        engine._ensure_runtime_variant_identity()
        records = engine._runtime_variant_choice_records
        if not isinstance(records, dict):
            raise RuntimeVariantError(
                "Dedicated SFZ sample layer omitted its live choice identities"
            )
        matching_regions = [
            region
            for region in engine.regions
            if records.get(id(region), {}).get("choice_sha256")
            == selection["choice_sha256"]
        ]
        if len(matching_regions) != 1:
            raise RuntimeVariantError(
                "Dedicated SFZ selected choice does not identify one live region"
            )
        region = matching_regions[0]
        key_matches = (
            region.key_min is None
            or region.key_max is None
            or region.key_min - 0.5
            <= target_midi
            <= region.key_max + 0.5
        )
        velocity_matches = (
            region.velocity_min - half_velocity_step
            <= velocity
            <= region.velocity_max + half_velocity_step
        )
        metadata = layer.region_runtime.get(region.stable_key)
        if metadata is None:
            raise RuntimeVariantError(
                "Dedicated SFZ live region has no wrapper runtime metadata"
            )
        wrapper_gain = float(metadata.velocity_gain(velocity))
        if not math.isfinite(wrapper_gain) or wrapper_gain < 0.0:
            raise RuntimeVariantError(
                "Dedicated SFZ wrapper velocity gain is invalid"
            )
        if not key_matches or not velocity_matches:
            retention = "discarded_key_or_velocity_mismatch"
            route_retained = False
            outcome_wrapper_gain: float | None = None
        elif wrapper_gain <= 1.0e-9:
            retention = "discarded_wrapper_gain_threshold"
            route_retained = False
            outcome_wrapper_gain = wrapper_gain
        else:
            retention = "retained_attack_voice"
            route_retained = True
            outcome_wrapper_gain = wrapper_gain
            retained_indexes.append(layer_index)

        effective_static_gain = (
            float(engine.gain)
            * float(region.gain)
            * (velocity ** float(engine.velocity_exponent))
            * wrapper_gain
        )
        if not math.isfinite(effective_static_gain):
            raise RuntimeVariantError(
                "Dedicated SFZ retained layer static gain is invalid"
            )
        static_audio_contributing = (
            route_retained and abs(effective_static_gain) > 1.0e-15
        )
        if static_audio_contributing:
            static_audio_indexes.append(layer_index)
        expected_wrapper_outcome = {
            "schema_version": 1,
            "kind": "dedicated_sfz_wrapper_outcome",
            "phase": "note_on_attack",
            "articulation": resolved_articulation,
            "layer_index": layer_index,
            "wrapper_role_sha256": stable_variant_sha256(
                "dedicated-sfz-wrapper-role-v1",
                {
                    "phase": "note_on_attack",
                    "articulation": resolved_articulation,
                    "layer_index": layer_index,
                },
            ),
            "event_sequence": note_on_sequence,
            "target_midi": target_midi,
            "velocity": velocity,
            "key_velocity_match": bool(
                key_matches and velocity_matches
            ),
            "wrapper_velocity_gain": outcome_wrapper_gain,
            "route_committed": route_retained,
            "committed_amplitude": (
                effective_static_gain if route_retained else None
            ),
            "final_status": retention,
        }
        actual_wrapper_outcome = selection["wrapper_outcome"]
        if actual_wrapper_outcome is None:
            raise RuntimeVariantNotCertifiable(
                "Dedicated SFZ provisional attack selection was never finalized"
            )
        if not _json_values_match(
            actual_wrapper_outcome,
            expected_wrapper_outcome,
        ):
            raise RuntimeVariantNotCertifiable(
                "Dedicated SFZ actual wrapper commit differs from the "
                "reconstructed retained attack bundle"
            )
        bindings.append(
            {
                "selection_index": layer_index,
                "layer_index": layer_index,
                "role": "note_on_attack_layer",
                "wrapper_role_sha256": actual_wrapper_outcome[
                    "wrapper_role_sha256"
                ],
                "component_sha256": component_sha256,
                "catalog_sha256": selection["catalog_sha256"],
                "choice_sha256": selection["choice_sha256"],
                "retention": retention,
                "route_retained": route_retained,
                "wrapper_velocity_gain": wrapper_gain,
                "effective_static_gain": effective_static_gain,
                "static_audio_contributing": static_audio_contributing,
                "wrapper_outcome_sha256": stable_variant_sha256(
                    "dedicated-sfz-wrapper-outcome-v1",
                    actual_wrapper_outcome,
                ),
            }
        )

    if not retained_indexes:
        raise RuntimeVariantNotCertifiable(
            "Dedicated SFZ condition retained no note-on attack voice"
        )
    retained_bundle = [
        binding for binding in bindings if binding["route_retained"]
    ]
    static_audio_bundle = [
        binding
        for binding in bindings
        if binding["static_audio_contributing"]
    ]
    attack_phase_contract = {
        "capture_scope": "note_on_attack_only",
        "reported_articulation": reported_articulation,
        "resolved_articulation": resolved_articulation,
        "public_note_id": 1,
        "note_on_sequence": note_on_sequence,
        "selector_random_value": expected_random_value,
        "ordered_layer_bindings": bindings,
        "retained_layer_indexes": retained_indexes,
        "static_audio_contributing_layer_indexes": static_audio_indexes,
        "retained_bundle_sha256": stable_variant_sha256(
            "dedicated-sfz-retained-attack-bundle-v1",
            retained_bundle,
        ),
        "static_audio_bundle_sha256": stable_variant_sha256(
            "dedicated-sfz-static-audio-attack-bundle-v1",
            static_audio_bundle,
        ),
    }
    contract = {
        "schema_version": 1,
        "kind": "top_level_runtime_variant_contract",
        "backend": "builtin_dedicated_sfz",
        "audio_selection_model": (
            "dedicated_sfz_attack_layer_bundle_v1"
            if finite_rr_slot is None
            else "dedicated_sfz_finite_rr_attack_bundle_v1"
        ),
        "capture_completeness": (
            "all_note_on_attack_layer_selectors_and_retained_bundle_reconstructed"
        ),
        # This is a multiset.  Two ordered wrapper roles may legitimately
        # delegate to content-identical SampleInstrument components.
        "expected_component_sha256s": sorted(ordered_components),
        "expected_selection_count": len(attack_layers),
        "manifest_type": str(manifest["type"]),
        "factory_route": "builtin_manifest_dispatch_no_implementation",
        "attack_phase_contract": attack_phase_contract,
    }
    if finite_rr_slot is not None:
        variation_period = 1
        for candidate_count in finite_rr_candidate_counts:
            variation_period = math.lcm(
                variation_period,
                candidate_count,
            )
        if variation_period <= 1:
            raise RuntimeVariantNotCertifiable(
                "finite RR certification requires more than one cycle slot"
            )
        if not 0 <= finite_rr_slot < variation_period:
            raise RuntimeVariantNotCertifiable(
                "finite RR variation slot is outside its cycle"
            )
        slot_bundle = [
            {
                "layer_index": binding["layer_index"],
                "choice_sha256": binding["choice_sha256"],
                "route_retained": binding["route_retained"],
                "wrapper_outcome_sha256": binding[
                    "wrapper_outcome_sha256"
                ],
            }
            for binding in bindings
        ]
        contract["finite_rr_cycle_contract"] = {
            "enumeration": "natural_same_condition_prewarm_cycle",
            "variation_period": variation_period,
            "variation_slot": finite_rr_slot,
            "ordered_layer_candidate_counts": (
                finite_rr_candidate_counts
            ),
            "slot_bundle_sha256": stable_variant_sha256(
                "dedicated-sfz-finite-rr-slot-bundle-v1",
                slot_bundle,
            ),
        }
    return contract


MAX_NATURAL_FINITE_RR_VARIANTS = 64


def dedicated_sfz_finite_rr_variation_period(
    *,
    instrument: Any,
    manifest: Any,
    sampled_condition: Any,
) -> int:
    """Return the exact finite natural RR period for one attack condition.

    Random partitions are intentionally excluded from this natural-prewarm
    protocol because the isolated public event fixes note id and sequence.
    They require the later audit-steered protocol.  Continuous jitter remains
    unbounded and therefore uncertifiable.
    """

    from .dedicated_sfz import DedicatedSfzInstrument
    from .events import PerformanceEvent, event_pitch_hz
    from .sampler import SampleInstrument
    from .tuning import EqualTemperament

    condition = _validate_sampled_condition_payload(sampled_condition)
    if not isinstance(manifest, dict):
        raise RuntimeVariantNotCertifiable(
            "instrument manifest provenance is unavailable"
        )
    if manifest.get("implementation") is not None:
        raise RuntimeVariantNotCertifiable(
            "local implementation factories cannot certify finite RR"
        )
    if (
        type(instrument) is not DedicatedSfzInstrument
        or manifest.get("type") != "dedicated_sfz"
    ):
        raise RuntimeVariantNotCertifiable(
            "finite natural RR planning currently requires exact DedicatedSfzInstrument"
        )
    _validate_builtin_factory_provenance(
        instrument,
        manifest,
        sampled_condition=condition,
    )
    reported = condition["final_articulation"]
    if reported == "__default__":
        articulation = instrument.default_articulation
        sequence = 0
    else:
        articulation = reported
        sequence = 1
    runtime = instrument.articulations.get(articulation)
    if runtime is None or not runtime.attack_layers:
        raise RuntimeVariantNotCertifiable(
            "finite RR articulation has no live attack layers"
        )
    event = PerformanceEvent(
        0,
        sequence,
        "note_on",
        {
            "note_id": 1,
            "midi_note": float(condition["midi_note"]),
            "velocity": float(condition["velocity"]) / 127.0,
        },
    )
    previous = instrument.articulation
    tuning = EqualTemperament()
    try:
        instrument.articulation = articulation
        payload = instrument._playback_payload(event, tuning)
    finally:
        instrument.articulation = previous
    layer_event = PerformanceEvent(0, sequence, "note_on", payload)
    pitch_hz = event_pitch_hz(layer_event, tuning)
    target_midi = float(payload["midi_note"])
    velocity = float(payload["velocity"])
    period = 1
    for layer in runtime.attack_layers:
        if type(layer.engine) is not SampleInstrument:
            raise RuntimeVariantNotCertifiable(
                "finite RR layer is not an exact SampleInstrument"
            )
        catalog = layer.engine._runtime_variant_catalog(
            pitch_hz=pitch_hz,
            velocity=velocity,
            target_midi=target_midi,
        )
        cycle = _finite_rr_catalog_choice_cycle(catalog)
        period = math.lcm(period, len(cycle))
        if period > MAX_NATURAL_FINITE_RR_VARIANTS:
            raise RuntimeVariantNotCertifiable(
                "finite RR cycle exceeds the bounded 64-slot audit limit"
            )
    return period


def prewarm_dedicated_sfz_variation_slot(
    *,
    instrument: Any,
    manifest: Any,
    sampled_condition: Any,
    variation_slot: int,
) -> None:
    """Advance an exact fresh Dedicated SFZ through real, fully drained hits."""

    from .dedicated_sfz import DedicatedSfzInstrument
    from .events import PerformanceEvent
    from .tuning import EqualTemperament

    variation_slot = _require_integer(
        variation_slot,
        "finite RR variation_slot",
        minimum=0,
    )
    if variation_slot == 0:
        return
    condition = _validate_sampled_condition_payload(sampled_condition)
    period = dedicated_sfz_finite_rr_variation_period(
        instrument=instrument,
        manifest=manifest,
        sampled_condition=condition,
    )
    if not 0 <= variation_slot < period:
        raise RuntimeVariantNotCertifiable(
            "finite RR variation_slot is outside its live cycle"
        )
    if type(instrument) is not DedicatedSfzInstrument:
        raise RuntimeVariantNotCertifiable(
            "finite RR prewarm requires exact DedicatedSfzInstrument"
        )
    if instrument.active_voice_count or instrument.routes:
        raise RuntimeVariantNotCertifiable(
            "finite RR prewarm requires a fresh silent instrument"
        )
    for runtime in instrument.articulations.values():
        for layer in (*runtime.attack_layers, *runtime.release_layers):
            if layer.engine._round_robin_counters:
                raise RuntimeVariantNotCertifiable(
                    "finite RR prewarm requires fresh selector state"
                )

    reported = condition["final_articulation"]
    if reported == "__default__":
        articulation = instrument.default_articulation
        note_on_sequence = 0
    else:
        articulation = reported
        note_on_sequence = 1
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "articulation",
                {"name": articulation},
            ),
            EqualTemperament(),
        )
    tuning = EqualTemperament()
    note_on_payload = {
        "note_id": 1,
        "midi_note": float(condition["midi_note"]),
        "velocity": float(condition["velocity"]) / 127.0,
    }
    release_sample = max(1, round(0.05 * instrument.sample_rate))
    maximum_drain_frames = 60 * instrument.sample_rate
    for _ in range(variation_slot):
        instrument.handle_event(
            PerformanceEvent(
                0,
                note_on_sequence,
                "note_on",
                dict(note_on_payload),
            ),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(
                release_sample,
                note_on_sequence + 1,
                "note_off",
                {
                    "note_id": 1,
                    "release_velocity": 0.5,
                },
            ),
            tuning,
        )
        drained = False
        for _frame in range(maximum_drain_frames):
            instrument.render_frame()
            if instrument.active_voice_count == 0:
                drained = True
                break
        if not drained or instrument.routes:
            raise RuntimeVariantNotCertifiable(
                "finite RR prewarm voice tail did not fully drain"
            )


def certify_deterministic_single_observation(
    *,
    instrument: Any,
    manifest: Any,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int = 0,
) -> dict[str, Any]:
    """Create the only phase-one ``all_runtime_variants`` proof.

    The incoming receipt is capture-only evidence.  It becomes useful only
    after this function proves the exact top-level backend is allow-listed,
    every expected audio-selection component was actually consumed, and every
    consumed sample catalog has one choice with no gaps, RR, or jitter.
    """

    variation_slot = _require_integer(
        variation_slot,
        "deterministic-single variation_slot",
        minimum=0,
    )
    condition_payload = _validate_sampled_condition_payload(
        sampled_condition
    )
    condition = _require_sha256(condition_id, "condition_id")
    expected_condition_id = stable_variant_sha256(
        "onset-isolated-sampled-condition-v1",
        condition_payload,
    )
    if condition != expected_condition_id:
        raise RuntimeVariantError(
            "condition_id does not bind the sampled condition payload"
        )
    if variation_slot != 0:
        raise RuntimeVariantNotCertifiable(
            "deterministic-single certification permits only variation slot 0"
        )
    receipt = validate_runtime_variant_selection_receipt(selection_receipt)
    contract = _trusted_top_level_contract(
        instrument,
        manifest,
        sampled_condition=condition_payload,
        selection_receipt=receipt,
    )
    expected_count = contract["expected_selection_count"]
    if receipt["selection_count"] != expected_count:
        raise RuntimeVariantNotCertifiable(
            "capture did not consume every top-level audio-selection component"
        )
    selections = receipt["selections"]
    actual_components = sorted(
        selection["component_sha256"] for selection in selections
    )
    if actual_components != contract["expected_component_sha256s"]:
        raise RuntimeVariantNotCertifiable(
            "captured components differ from the top-level completeness contract"
        )
    catalog_by_sha = {
        record["catalog_sha256"]: record["catalog"]
        for record in receipt["catalogs"]
    }

    if contract["backend"] == "builtin_oscillator":
        if selections or receipt["catalogs"]:
            raise RuntimeVariantError(
                "built-in oscillator unexpectedly consumed a selection catalog"
            )
    elif contract["backend"] == "builtin_sample_instrument":
        if any(
            selection["wrapper_outcome"] is not None
            for selection in selections
        ):
            raise RuntimeVariantNotCertifiable(
                "direct SampleInstrument receipt contains a composite wrapper outcome"
            )
        if receipt["all_conditions_deterministic_single"] is not True:
            raise RuntimeVariantNotCertifiable(
                "sample capture is not deterministic-single"
            )
        target_midi = float(condition_payload["midi_note"])
        velocity = float(condition_payload["velocity"]) / 127.0
        pitch_hz = 440.0 * (
            2.0 ** ((target_midi - 69.0) / 12.0)
        )
        expected_catalog = instrument._runtime_variant_catalog(
            pitch_hz=pitch_hz,
            velocity=velocity,
            target_midi=target_midi,
        )
        for selection in selections:
            catalog = catalog_by_sha[selection["catalog_sha256"]]
            if not _sample_catalogs_match(catalog, expected_catalog):
                raise RuntimeVariantNotCertifiable(
                    "sample receipt catalog differs from the live exact "
                    "SampleInstrument catalog for this sampled condition"
                )
            _validate_sample_single_catalog(
                catalog,
                selection,
                expected_pitch_hz=pitch_hz,
                expected_midi=target_midi,
                expected_velocity=velocity,
            )
    elif contract["backend"] == "builtin_dedicated_sfz":
        # The exact ordered layer catalogs, selector event identity, wrapper
        # discard decisions, and retained attack bundle were independently
        # reconstructed while creating the trusted condition contract above.
        if receipt["all_conditions_deterministic_single"] is not True:
            raise RuntimeVariantNotCertifiable(
                "Dedicated SFZ attack capture is not deterministic-single"
            )
    else:  # pragma: no cover - exact contract validator makes this unreachable.
        raise RuntimeVariantNotCertifiable(
            "top-level backend contract is not phase-one certifiable"
        )

    payload = {
        "schema_version": 1,
        "kind": "deterministic_single_runtime_variant_proof",
        "certification_protocol": "tianlai-deterministic-single-v1",
        "claim": "all_runtime_variants_at_one_sampled_condition",
        "condition_id": condition,
        "sampled_condition": condition_payload,
        "variation_slot": 0,
        "top_level_contract": contract,
        "top_level_contract_sha256": stable_variant_sha256(
            "top-level-runtime-variant-contract-v1",
            contract,
        ),
        "selection_receipt_sha256": receipt["receipt_sha256"],
        "catalog_sha256s": sorted(catalog_by_sha),
        "component_sha256s": actual_components,
        "choice_sha256s": sorted(
            selection["choice_sha256"] for selection in selections
        ),
    }
    return {
        **payload,
        "proof_sha256": stable_variant_sha256(
            "deterministic-single-runtime-variant-proof-v1",
            payload,
        ),
    }


def certify_finite_rr_observation(
    *,
    instrument: Any,
    manifest: Any,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    """Certify one real render in a complete natural finite-RR cycle."""

    from .dedicated_sfz import DedicatedSfzInstrument

    condition_payload = _validate_sampled_condition_payload(
        sampled_condition
    )
    condition = _require_sha256(condition_id, "condition_id")
    if condition != stable_variant_sha256(
        "onset-isolated-sampled-condition-v1",
        condition_payload,
    ):
        raise RuntimeVariantError(
            "condition_id does not bind the sampled condition payload"
        )
    variation_slot = _require_integer(
        variation_slot,
        "finite RR variation_slot",
        minimum=0,
    )
    if not isinstance(manifest, dict):
        raise RuntimeVariantNotCertifiable(
            "instrument manifest provenance is unavailable"
        )
    if manifest.get("implementation") is not None:
        raise RuntimeVariantNotCertifiable(
            "local implementation factories cannot certify finite RR provenance"
        )
    if (
        type(instrument) is not DedicatedSfzInstrument
        or manifest.get("type") != "dedicated_sfz"
    ):
        raise RuntimeVariantNotCertifiable(
            "finite RR proof requires exact built-in DedicatedSfzInstrument"
        )
    receipt = validate_runtime_variant_selection_receipt(
        selection_receipt
    )
    contract = _trusted_dedicated_sfz_attack_contract(
        instrument,
        manifest,
        sampled_condition=condition_payload,
        selection_receipt=receipt,
        finite_rr_slot=variation_slot,
    )
    expected_count = contract["expected_selection_count"]
    if receipt["selection_count"] != expected_count:
        raise RuntimeVariantNotCertifiable(
            "finite RR capture omitted an attack selection component"
        )
    components = sorted(
        selection["component_sha256"]
        for selection in receipt["selections"]
    )
    if components != contract["expected_component_sha256s"]:
        raise RuntimeVariantNotCertifiable(
            "finite RR captured components differ from the live wrapper contract"
        )
    cycle = contract["finite_rr_cycle_contract"]
    catalog_by_sha = {
        record["catalog_sha256"]: record["catalog"]
        for record in receipt["catalogs"]
    }
    payload = {
        "schema_version": 1,
        "kind": "finite_rr_runtime_variant_proof",
        "certification_protocol": "tianlai-natural-finite-rr-cycle-v1",
        "claim": "one_rendered_slot_of_complete_finite_rr_cycle",
        "condition_id": condition,
        "sampled_condition": condition_payload,
        "variation_slot": variation_slot,
        "variation_period": cycle["variation_period"],
        "slot_bundle_sha256": cycle["slot_bundle_sha256"],
        "top_level_contract": contract,
        "top_level_contract_sha256": stable_variant_sha256(
            "top-level-runtime-variant-contract-v1",
            contract,
        ),
        "selection_receipt_sha256": receipt["receipt_sha256"],
        "catalog_sha256s": sorted(catalog_by_sha),
        "component_sha256s": components,
        "choice_sha256s": sorted(
            selection["choice_sha256"]
            for selection in receipt["selections"]
        ),
    }
    return {
        **payload,
        "proof_sha256": stable_variant_sha256(
            "finite-rr-runtime-variant-proof-v1",
            payload,
        ),
    }


def certify_runtime_variant_observation(
    *,
    instrument: Any,
    manifest: Any,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int = 0,
) -> dict[str, Any]:
    """Choose the narrowest trusted proof protocol for one observation."""

    variation_slot = _require_integer(
        variation_slot,
        "runtime variant observation variation_slot",
        minimum=0,
    )
    if variation_slot == 0:
        try:
            return certify_deterministic_single_observation(
                instrument=instrument,
                manifest=manifest,
                selection_receipt=selection_receipt,
                condition_id=condition_id,
                sampled_condition=sampled_condition,
                variation_slot=0,
            )
        except RuntimeVariantNotCertifiable:
            pass
    return certify_finite_rr_observation(
        instrument=instrument,
        manifest=manifest,
        selection_receipt=selection_receipt,
        condition_id=condition_id,
        sampled_condition=sampled_condition,
        variation_slot=variation_slot,
    )


def validate_deterministic_single_proof_document(
    proof: Any,
    *,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    """Validate hashes/bindings without trusting the claimed backend.

    This is suitable for a portable proof's internal integrity check.  A
    conductor-facing approval must additionally call
    :func:`validate_deterministic_single_observation_proof` with a live exact
    built-in instrument so a local backend cannot forge the contract name.
    """

    safe = _canonical_copy(proof)
    value = _expect_exact_keys(
        safe,
        {
            "schema_version",
            "kind",
            "certification_protocol",
            "claim",
            "condition_id",
            "sampled_condition",
            "variation_slot",
            "top_level_contract",
            "top_level_contract_sha256",
            "selection_receipt_sha256",
            "catalog_sha256s",
            "component_sha256s",
            "choice_sha256s",
            "proof_sha256",
        },
        "runtime_variant_proof",
    )
    _require_schema_version(
        value["schema_version"],
        "runtime_variant_proof schema_version",
    )
    if value["kind"] != "deterministic_single_runtime_variant_proof":
        raise RuntimeVariantError("runtime_variant_proof kind is invalid")
    if (
        value["certification_protocol"]
        != "tianlai-deterministic-single-v1"
    ):
        raise RuntimeVariantError(
            "runtime_variant_proof certification protocol is unsupported"
        )
    if value["claim"] != (
        "all_runtime_variants_at_one_sampled_condition"
    ):
        raise RuntimeVariantError("runtime_variant_proof claim is invalid")
    expected_sampled_condition = _validate_sampled_condition_payload(
        sampled_condition
    )
    stored_sampled_condition = _validate_sampled_condition_payload(
        value["sampled_condition"]
    )
    if not _json_values_match(
        stored_sampled_condition,
        expected_sampled_condition,
    ):
        raise RuntimeVariantError(
            "runtime_variant_proof binds another sampled condition payload"
        )
    expected_condition = _require_sha256(condition_id, "condition_id")
    if expected_condition != stable_variant_sha256(
        "onset-isolated-sampled-condition-v1",
        expected_sampled_condition,
    ):
        raise RuntimeVariantError(
            "condition_id does not bind the sampled condition payload"
        )
    if value["condition_id"] != expected_condition:
        raise RuntimeVariantError(
            "runtime_variant_proof binds another sampled condition"
        )
    variation_slot = _require_integer(
        variation_slot,
        "runtime_variant_proof variation_slot argument",
        minimum=0,
    )
    stored_variation_slot = _require_integer(
        value["variation_slot"],
        "runtime_variant_proof variation_slot",
        minimum=0,
    )
    if variation_slot != 0 or stored_variation_slot != 0:
        raise RuntimeVariantError(
            "deterministic-single proof requires variation slot 0"
        )
    receipt = validate_runtime_variant_selection_receipt(selection_receipt)
    if value["selection_receipt_sha256"] != receipt["receipt_sha256"]:
        raise RuntimeVariantError(
            "runtime_variant_proof binds another selection receipt"
        )
    contract = _validate_embedded_top_level_contract_scalar_fields(
        value["top_level_contract"],
        label="runtime_variant_proof top_level_contract",
    )
    _validate_embedded_top_level_contract_hash_bindings(
        contract,
        selection_receipt=receipt,
        label="runtime_variant_proof top_level_contract",
    )
    expected_contract_hash = stable_variant_sha256(
        "top-level-runtime-variant-contract-v1",
        contract,
    )
    if _require_sha256(
        value["top_level_contract_sha256"],
        "runtime_variant_proof.top_level_contract_sha256",
    ) != expected_contract_hash:
        raise RuntimeVariantError(
            "runtime_variant_proof top-level contract hash is invalid"
        )
    catalog_hashes = sorted(
        record["catalog_sha256"] for record in receipt["catalogs"]
    )
    component_hashes = sorted(
        selection["component_sha256"]
        for selection in receipt["selections"]
    )
    choice_hashes = sorted(
        selection["choice_sha256"] for selection in receipt["selections"]
    )
    for field, expected in (
        ("catalog_sha256s", catalog_hashes),
        ("component_sha256s", component_hashes),
        ("choice_sha256s", choice_hashes),
    ):
        if value[field] != expected:
            raise RuntimeVariantError(
                f"runtime_variant_proof {field} differs from its receipt"
            )
    expected_proof_hash = stable_variant_sha256(
        "deterministic-single-runtime-variant-proof-v1",
        {
            key: item for key, item in value.items() if key != "proof_sha256"
        },
    )
    if _require_sha256(
        value["proof_sha256"],
        "runtime_variant_proof.proof_sha256",
    ) != expected_proof_hash:
        raise RuntimeVariantError("runtime_variant_proof self hash is invalid")
    return value


def validate_finite_rr_proof_document(
    proof: Any,
    *,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    """Validate portable finite-RR proof integrity without trusting backend."""

    safe = _canonical_copy(proof)
    value = _expect_exact_keys(
        safe,
        {
            "schema_version",
            "kind",
            "certification_protocol",
            "claim",
            "condition_id",
            "sampled_condition",
            "variation_slot",
            "variation_period",
            "slot_bundle_sha256",
            "top_level_contract",
            "top_level_contract_sha256",
            "selection_receipt_sha256",
            "catalog_sha256s",
            "component_sha256s",
            "choice_sha256s",
            "proof_sha256",
        },
        "finite_rr_runtime_variant_proof",
    )
    _require_schema_version(
        value["schema_version"],
        "finite RR proof schema_version",
    )
    if value["kind"] != "finite_rr_runtime_variant_proof":
        raise RuntimeVariantError("finite RR proof kind is invalid")
    if (
        value["certification_protocol"]
        != "tianlai-natural-finite-rr-cycle-v1"
    ):
        raise RuntimeVariantError(
            "finite RR certification protocol is unsupported"
        )
    if value["claim"] != (
        "one_rendered_slot_of_complete_finite_rr_cycle"
    ):
        raise RuntimeVariantError("finite RR proof claim is invalid")
    condition_payload = _validate_sampled_condition_payload(
        sampled_condition
    )
    stored_condition_payload = _validate_sampled_condition_payload(
        value["sampled_condition"]
    )
    expected_condition = _require_sha256(condition_id, "condition_id")
    if expected_condition != stable_variant_sha256(
        "onset-isolated-sampled-condition-v1",
        condition_payload,
    ):
        raise RuntimeVariantError(
            "condition_id does not bind the sampled condition"
        )
    if (
        value["condition_id"] != expected_condition
        or not _json_values_match(
            stored_condition_payload,
            condition_payload,
        )
    ):
        raise RuntimeVariantError(
            "finite RR proof binds another sampled condition"
        )
    variation_slot = _require_integer(
        variation_slot,
        "finite RR proof variation_slot argument",
        minimum=0,
    )
    stored_variation_slot = _require_integer(
        value["variation_slot"],
        "finite RR proof variation_slot",
        minimum=0,
    )
    if stored_variation_slot != variation_slot:
        raise RuntimeVariantError(
            "finite RR proof variation_slot is invalid"
        )
    period = _require_integer(
        value["variation_period"],
        "finite RR proof variation_period",
        minimum=2,
        maximum=MAX_NATURAL_FINITE_RR_VARIANTS,
    )
    if not 0 <= variation_slot < period:
        raise RuntimeVariantError(
            "finite RR proof variation_period is invalid"
        )
    _require_sha256(
        value["slot_bundle_sha256"],
        "finite RR proof slot_bundle_sha256",
    )
    receipt = validate_runtime_variant_selection_receipt(
        selection_receipt
    )
    if value["selection_receipt_sha256"] != receipt["receipt_sha256"]:
        raise RuntimeVariantError(
            "finite RR proof binds another selection receipt"
        )
    contract = _validate_embedded_top_level_contract_scalar_fields(
        value["top_level_contract"],
        label="finite RR proof top_level_contract",
    )
    _validate_embedded_top_level_contract_hash_bindings(
        contract,
        selection_receipt=receipt,
        label="finite RR proof top_level_contract",
    )
    if value["top_level_contract_sha256"] != stable_variant_sha256(
        "top-level-runtime-variant-contract-v1",
        contract,
    ):
        raise RuntimeVariantError(
            "finite RR proof top-level contract hash is invalid"
        )
    cycle = contract.get("finite_rr_cycle_contract")
    if not isinstance(cycle, dict):
        raise RuntimeVariantError(
            "finite RR proof cycle contract must be an object"
        )
    cycle_period = _require_integer(
        cycle["variation_period"],
        "finite RR proof cycle variation_period",
        minimum=2,
        maximum=MAX_NATURAL_FINITE_RR_VARIANTS,
    )
    cycle_slot = _require_integer(
        cycle["variation_slot"],
        "finite RR proof cycle variation_slot",
        minimum=0,
        maximum=cycle_period - 1,
    )
    if (
        cycle_period != period
        or cycle_slot != variation_slot
        or cycle.get("slot_bundle_sha256")
        != value["slot_bundle_sha256"]
    ):
        raise RuntimeVariantError(
            "finite RR proof cycle differs from its top-level contract"
        )
    expected_fields = {
        "catalog_sha256s": sorted(
            record["catalog_sha256"]
            for record in receipt["catalogs"]
        ),
        "component_sha256s": sorted(
            selection["component_sha256"]
            for selection in receipt["selections"]
        ),
        "choice_sha256s": sorted(
            selection["choice_sha256"]
            for selection in receipt["selections"]
        ),
    }
    for field, expected in expected_fields.items():
        if value[field] != expected:
            raise RuntimeVariantError(
                f"finite RR proof {field} differs from its receipt"
            )
    expected_hash = stable_variant_sha256(
        "finite-rr-runtime-variant-proof-v1",
        {
            key: item
            for key, item in value.items()
            if key != "proof_sha256"
        },
    )
    if value["proof_sha256"] != expected_hash:
        raise RuntimeVariantError("finite RR proof self hash is invalid")
    return value


def validate_runtime_variant_proof_document(
    proof: Any,
    *,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    variation_slot = _require_integer(
        variation_slot,
        "runtime variant proof variation_slot",
        minimum=0,
    )
    if not isinstance(proof, dict):
        raise RuntimeVariantError("runtime variant proof must be an object")
    if proof.get("kind") == "finite_rr_runtime_variant_proof":
        return validate_finite_rr_proof_document(
            proof,
            selection_receipt=selection_receipt,
            condition_id=condition_id,
            sampled_condition=sampled_condition,
            variation_slot=variation_slot,
        )
    return validate_deterministic_single_proof_document(
        proof,
        selection_receipt=selection_receipt,
        condition_id=condition_id,
        sampled_condition=sampled_condition,
        variation_slot=variation_slot,
    )


def validate_deterministic_single_observation_proof(
    proof: Any,
    *,
    instrument: Any,
    manifest: Any,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    """Recompute an observation proof from trusted runtime state."""

    safe = validate_deterministic_single_proof_document(
        proof,
        selection_receipt=selection_receipt,
        condition_id=condition_id,
        sampled_condition=sampled_condition,
        variation_slot=variation_slot,
    )
    expected = certify_deterministic_single_observation(
        instrument=instrument,
        manifest=manifest,
        selection_receipt=selection_receipt,
        condition_id=condition_id,
        sampled_condition=sampled_condition,
        variation_slot=variation_slot,
    )
    if not _runtime_variant_proofs_match(safe, expected):
        raise RuntimeVariantError(
            "runtime variant proof does not match its contract and receipt"
        )
    return safe


def validate_runtime_variant_observation_proof(
    proof: Any,
    *,
    instrument: Any,
    manifest: Any,
    selection_receipt: Any,
    condition_id: str,
    sampled_condition: Any,
    variation_slot: int,
) -> dict[str, Any]:
    """Recompute either a deterministic-single or finite-RR proof."""

    safe = validate_runtime_variant_proof_document(
        proof,
        selection_receipt=selection_receipt,
        condition_id=condition_id,
        sampled_condition=sampled_condition,
        variation_slot=variation_slot,
    )
    if safe["kind"] == "finite_rr_runtime_variant_proof":
        expected = certify_finite_rr_observation(
            instrument=instrument,
            manifest=manifest,
            selection_receipt=selection_receipt,
            condition_id=condition_id,
            sampled_condition=sampled_condition,
            variation_slot=variation_slot,
        )
    else:
        expected = certify_deterministic_single_observation(
            instrument=instrument,
            manifest=manifest,
            selection_receipt=selection_receipt,
            condition_id=condition_id,
            sampled_condition=sampled_condition,
            variation_slot=variation_slot,
        )
    if not _runtime_variant_proofs_match(safe, expected):
        raise RuntimeVariantError(
            "runtime variant proof does not match its live contract and receipt"
        )
    return safe


_ACTIVE_CAPTURE: ContextVar[RuntimeVariantCapture | None] = ContextVar(
    "tianlai_runtime_variant_capture",
    default=None,
)


def current_runtime_variant_capture() -> RuntimeVariantCapture | None:
    """Return the capture for this context, or ``None`` during normal render."""

    return _ACTIVE_CAPTURE.get()


@contextmanager
def capture_runtime_variants() -> Iterator[RuntimeVariantCapture]:
    """Observe backend selections made inside one task/thread context."""

    capture = RuntimeVariantCapture()
    token = _ACTIVE_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_CAPTURE.reset(token)
        capture.seal()
