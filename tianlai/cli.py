from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from . import __version__
from .events import parse_performance_document
from .instrument import create_instrument
from .renderer import load_json_object, render_to_wav_atomic
from .runtime_layout import discover_runtime_layout


def _default_midi_roster_draft_path(score_output: Path) -> Path:
    name = score_output.name
    suffix = ".score.json"
    if name.lower().endswith(suffix):
        return score_output.with_name(
            name[: -len(suffix)] + ".roster-draft.json"
        )
    return score_output.with_name(score_output.stem + ".roster-draft.json")


def _write_json_atomic(
    path: Path,
    value: object,
    *,
    overwrite: bool = False,
) -> None:
    if path.exists() and not overwrite:
        raise ValueError(
            f"输出已存在，默认拒绝覆盖: {path};确认后传 --overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog_path(value: str | None) -> Path:
    if value:
        return Path(value)
    return discover_runtime_layout(require_catalog=True).catalog


def _load_json_value(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _enforce_cli_manifest_availability(
    manifest: dict[str, object],
    *,
    allow_local_compatibility_soundfont: bool = False,
) -> None:
    """Keep the public single-instrument CLI inside the licence boundary."""

    if manifest.get("license_status") == "quarantined":
        name = str(manifest.get("name") or manifest.get("id") or "instrument")
        raise ValueError(
            f"{name}: license_status=quarantined; public CLI render/validate is disabled"
        )
    if manifest.get("type") == "soundfont":
        if not allow_local_compatibility_soundfont:
            raise ValueError(
                "type=soundfont is a local compatibility/test backend and is "
                "disabled on the public CLI path; pass "
                "--allow-local-compatibility-soundfont only for an explicit "
                "private/local test"
            )
        print(
            "warning: type=soundfont is running in explicit local compatibility/"
            "test mode; this does not approve the selected bank or rendered audio "
            "for Tianlai's public/trusted release path",
            file=sys.stderr,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tianlai", description="Tianlai deterministic headless instrument renderer"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser(
        "doctor",
        help="check runtime layout, catalogue, trust policy and resource readiness",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--quick", action="store_true")
    doctor.add_argument("--require-all-resources", action="store_true")

    render = subcommands.add_parser("render", help="render a performance JSON document to WAV")
    render.add_argument("--instrument", required=True, help="instrument manifest JSON")
    render.add_argument("--events", required=True, help="performance event JSON")
    render.add_argument("--output", required=True, help="output 24-bit stereo WAV")
    render.add_argument(
        "--allow-local-compatibility-soundfont",
        action="store_true",
        help=(
            "explicit private/local test only: allow a type=soundfont manifest; "
            "never grants public/trusted approval"
        ),
    )

    validate = subcommands.add_parser("validate", help="validate an instrument and event document")
    validate.add_argument("--instrument", required=True, help="instrument manifest JSON")
    validate.add_argument("--events", required=True, help="performance event JSON")
    validate.add_argument(
        "--allow-local-compatibility-soundfont",
        action="store_true",
        help=(
            "explicit private/local test only: allow a type=soundfont manifest; "
            "never grants public/trusted approval"
        ),
    )

    pitch = subcommands.add_parser("analyze-pitch", help="measure a sample near an expected pitch")
    pitch.add_argument("--audio", required=True, help="WAV or FLAC sample")
    pitch.add_argument("--expected-hz", required=True, type=float, help="expected fundamental in Hz")

    catalog = subcommands.add_parser("catalog", help="list all instrument manifests")
    catalog.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    catalog.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    catalog.add_argument(
        "--include-quarantined",
        action="store_true",
        help="audit only: include formal instruments blocked by licence quarantine",
    )
    catalog.add_argument(
        "--include-local-compatibility",
        action="store_true",
        help="audit only: include local-only type=soundfont compatibility manifests",
    )

    progress = subcommands.add_parser(
        "progress",
        help=(
            "report the historical 98-instrument upgrade ledger "
            "(not the current catalogue total)"
        ),
    )
    progress.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    progress.add_argument(
        "--registry",
        default=None,
        help=(
            "optional Markdown registry override; default: packaged registry"
        ),
    )
    progress.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    ensemble = subcommands.add_parser(
        "ensemble", help="render a score through the conductor into stems and a mix"
    )
    ensemble.add_argument("--score", required=True, help="score document JSON")
    ensemble.add_argument("--roster", required=True, help="roster document JSON")
    ensemble.add_argument("--output", required=True, help="output directory")
    ensemble.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    ensemble.add_argument(
        "--expression",
        default=None,
        choices=("ensemble", "strict"),
        help="override render profile expression",
    )
    ensemble.add_argument(
        "--range-mode",
        default=None,
        choices=("compatibility", "strict_hq"),
        help=(
            "compatibility reports range-contract diagnostics without changing "
            "legacy rendering; strict_hq fails closed unless the exact profile "
            "has verified high-quality coverage"
        ),
    )
    ensemble.add_argument("--seed", type=int, default=None, help="override profile seed")
    ensemble.add_argument(
        "--master-gain-db", type=float, default=None, help="override profile mix-bus gain"
    )
    normalization = ensemble.add_mutually_exclusive_group()
    normalization.add_argument(
        "--normalize-peak-db",
        type=float,
        default=None,
        help="override profile target peak in dBFS (e.g. -1)",
    )
    normalization.add_argument(
        "--no-normalize",
        action="store_true",
        help="override profile and preserve the mix's unnormalized level",
    )
    ensemble.add_argument(
        "--collaboration-mode",
        choices=("manual", "analyze", "suggest"),
        default=None,
        help=(
            "override roster collaboration mode; analyze writes objective "
            "stem/relation diagnostics, suggest also emits bounded gain "
            "recommendations, and neither mode changes audio"
        ),
    )
    ensemble.add_argument(
        "--render-profile",
        help=(
            "versioned tianlai.render_profile JSON; default is preview-v1 "
            "(shared hall, -1 dBFS normalization, stems and cache enabled)"
        ),
    )
    ensemble.add_argument(
        "--plan-only", action="store_true", help="write the performance plan and stop"
    )
    ensemble.add_argument(
        "--no-stems", action="store_true", help="write only the mix, not the stems"
    )
    ensemble.add_argument(
        "--no-stem-cache",
        action="store_true",
        help=(
            "disable the verified pre-gain stem cache; by default it is "
            "shared under the output directory's parent"
        ),
    )
    ensemble.add_argument(
        "--refresh-stem-cache",
        action="store_true",
        help=(
            "ignore existing stem-cache entries for this render and "
            "rerender the raw stems; conflicting valid entries are preserved"
        ),
    )
    ensemble.add_argument(
        "--stem-cache-directory",
        help=(
            "cache root; default: <output parent>/.tianlai-cache/stems"
        ),
    )
    space = ensemble.add_mutually_exclusive_group()
    space.add_argument(
        "--hall",
        dest="hall",
        action="store_true",
        default=None,
        help="override profile and enable the deterministic default shared hall",
    )
    space.add_argument(
        "--dry",
        dest="hall",
        action="store_false",
        help="override profile and disable the shared hall",
    )
    space.add_argument(
        "--space-config",
        help="JSON object with SpaceConfig fields; enables a configured shared hall",
    )

    project_render = subcommands.add_parser(
        "project-render",
        help=(
            "render score+roster as a new immutable, receipt-bound candidate "
            "(recommended public workflow)"
        ),
    )
    project_render.add_argument("--score", required=True, help="score-v1 JSON")
    project_render.add_argument("--roster", required=True, help="formal roster JSON")
    project_render.add_argument(
        "--title",
        help="work title; default: score title or score filename",
    )
    project_render.add_argument(
        "--render-profile",
        help="optional tianlai.render_profile JSON; default: preview-v1",
    )
    project_render.add_argument(
        "--output-root",
        help="candidate root; default: TIANLAI_OUTPUT_DIR/候选",
    )
    project_render.add_argument(
        "--output-id",
        help="explicit candidate ID; default: unique timestamp+Hash ID",
    )
    project_render.add_argument(
        "--parent-candidate",
        help="optional parent candidate ID for revision lineage",
    )
    project_render.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    project_render.add_argument(
        "--overwrite",
        action="store_true",
        help="replace one named candidate only with receipt Hash confirmation",
    )
    project_render.add_argument(
        "--expected-receipt-sha256",
        help="required with --overwrite when the candidate directory exists",
    )

    candidate_locate = subcommands.add_parser(
        "candidate-locate",
        help="locate a heard timestamp from a saved candidate receipt and plan",
    )
    candidate_locate.add_argument(
        "--candidate",
        required=True,
        help="candidate directory or 候选.json",
    )
    candidate_locate.add_argument("--at", required=True, type=float, help="seconds")
    candidate_locate.add_argument(
        "--tail-lookback",
        type=float,
        default=5.0,
        help="seconds before the anchor to list as possible release/space sources",
    )
    candidate_locate.add_argument(
        "--upcoming",
        type=float,
        default=2.0,
        help="seconds after the anchor to list",
    )
    candidate_locate.add_argument("--max-events", type=int, default=128)
    candidate_locate.add_argument("--output", help="optional result JSON")
    candidate_locate.add_argument("--overwrite", action="store_true")

    candidate_compare = subcommands.add_parser(
        "candidate-compare",
        help="compare two saved candidates and their bound source revisions",
    )
    candidate_compare.add_argument("--before", required=True)
    candidate_compare.add_argument("--after", required=True)
    candidate_compare.add_argument("--max-changes", type=int, default=256)
    candidate_compare.add_argument("--output", help="optional result JSON")
    candidate_compare.add_argument("--overwrite", action="store_true")

    import_midi = subcommands.add_parser(
        "import-midi", help="convert a Standard MIDI File into a score document"
    )
    import_midi.add_argument("--midi", required=True, help="input .mid file")
    import_midi.add_argument("--output", required=True, help="score document JSON to write")
    import_midi.add_argument(
        "--roster-draft-output",
        help=(
            "non-executable roster draft JSON to write; defaults beside "
            "--output as <name>.roster-draft.json"
        ),
    )
    import_midi.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing score/draft outputs only after explicit confirmation",
    )

    import_musicxml = subcommands.add_parser(
        "import-musicxml",
        help="convert a MusicXML score (.musicxml/.xml/.mxl) into a score document",
    )
    import_musicxml.add_argument(
        "--musicxml", required=True, help="input .musicxml, .xml or compressed .mxl score"
    )
    import_musicxml.add_argument(
        "--output", required=True, help="score document JSON to write"
    )
    import_musicxml.add_argument("--overwrite", action="store_true")

    export_midi = subcommands.add_parser(
        "export-midi",
        help="export a score to an explicitly lossy Standard MIDI editing copy",
    )
    export_midi.add_argument("--score", required=True, help="input score JSON")
    export_midi.add_argument(
        "--roster",
        help="optional roster JSON for safer General MIDI preview mapping",
    )
    export_midi.add_argument("--output", required=True, help="output .mid file")
    export_midi.add_argument(
        "--report-output",
        help="loss report JSON; defaults beside the MIDI",
    )
    export_midi.add_argument(
        "--allow-lossy",
        action="store_true",
        help="acknowledge every blocking semantic loss listed in the report",
    )
    export_midi.add_argument("--overwrite", action="store_true")

    project_import = subcommands.add_parser(
        "project-import",
        help="import MIDI/MusicXML into one hash-bound score/report/roster-draft bundle",
    )
    project_import.add_argument(
        "--input",
        required=True,
        help=".mid/.midi/.musicxml/.xml/.mxl source",
    )
    project_import.add_argument(
        "--output",
        required=True,
        help="new dedicated import generation directory",
    )
    project_import.add_argument(
        "--basename",
        help="output filename prefix; default: source stem",
    )
    project_import.add_argument(
        "--loss-policy",
        choices=("reject", "warn", "allow"),
        default="reject",
        help=(
            "reject unsupported source semantics by default; warn/allow "
            "preserve them in import-report.json but cannot reconstruct them"
        ),
    )
    project_import.add_argument(
        "--open-palette",
        action="store_true",
        help="show all non-quarantined candidates instead of the trusted palette",
    )
    project_import.add_argument(
        "--candidate-limit",
        type=int,
        default=8,
        help="non-executable routing hints per part (1..16)",
    )
    project_import.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    project_import.add_argument("--overwrite", action="store_true")

    roster_promote = subcommands.add_parser(
        "roster-promote",
        help="confirm every imported part explicitly and create an executable roster",
    )
    roster_promote.add_argument("--score", required=True, help="bound score-v1 JSON")
    roster_promote.add_argument(
        "--draft",
        required=True,
        help="bound non-executable roster draft JSON",
    )
    roster_promote.add_argument(
        "--assignments",
        help="JSON array or {assignments:[...]} with explicit part routes",
    )
    roster_promote.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="PART=INSTRUMENT",
        help="repeat for simple melodic assignments; kits use --assignments",
    )
    roster_promote.add_argument(
        "--collaboration",
        help="optional collaboration settings JSON object",
    )
    roster_promote.add_argument("--name", help="formal roster name")
    roster_promote.add_argument("--output", required=True, help="formal roster JSON")
    roster_promote.add_argument(
        "--open-palette",
        action="store_true",
        help="allow an explicit non-quarantined instrument outside trusted palette",
    )
    roster_promote.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    roster_promote.add_argument("--overwrite", action="store_true")

    upgrade_score = subcommands.add_parser(
        "upgrade-score",
        help="upgrade a legacy score to score-v1 with stable event IDs",
    )
    upgrade_score.add_argument("--score", required=True, help="input score JSON")
    upgrade_score.add_argument("--output", required=True, help="score-v1 JSON to write")
    upgrade_score.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output only after explicit confirmation",
    )

    score_slice = subcommands.add_parser(
        "score-slice",
        help="read a bounded score-v1 fragment by part, event ID or bar range",
    )
    score_slice.add_argument("--score", required=True, help="input score-v1 JSON")
    score_slice.add_argument("--query", required=True, help="slice query JSON")
    score_slice.add_argument("--output", help="optional result JSON; otherwise stdout")
    score_slice.add_argument("--overwrite", action="store_true")

    score_patch = subcommands.add_parser(
        "score-patch",
        help="apply a hash-bound event-ID patch and write a new score revision",
    )
    score_patch.add_argument("--score", required=True, help="base score-v1 JSON")
    score_patch.add_argument("--patch", required=True, help="score patch JSON")
    score_patch.add_argument("--output", required=True, help="new score revision JSON")
    score_patch.add_argument(
        "--result-output",
        help="optional full patch result/diff JSON",
    )
    score_patch.add_argument("--overwrite", action="store_true")

    score_compare = subcommands.add_parser(
        "score-compare",
        help="compare two score-v1 revisions by stable event ID",
    )
    score_compare.add_argument("--before", required=True, help="earlier score JSON")
    score_compare.add_argument("--after", required=True, help="later score JSON")
    score_compare.add_argument("--max-changes", type=int, default=256)
    score_compare.add_argument("--output", help="optional result JSON; otherwise stdout")
    score_compare.add_argument("--overwrite", action="store_true")

    capabilities = subcommands.add_parser(
        "capabilities", help="report what every instrument declares it can play"
    )
    capabilities.add_argument(
        "--root",
        default=None,
        help="instrument catalog root; default: TIANLAI_HOME/乐器",
    )
    capabilities.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    capabilities.add_argument(
        "--include-quarantined",
        action="store_true",
        help="audit only: include formal instruments blocked by licence quarantine",
    )
    capabilities.add_argument(
        "--include-local-compatibility",
        action="store_true",
        help="audit only: include local-only type=soundfont compatibility manifests",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .doctor import main as doctor_main

            forwarded: list[str] = []
            if args.json:
                forwarded.append("--json")
            if args.quick:
                forwarded.append("--quick")
            if args.require_all_resources:
                forwarded.append("--require-all-resources")
            return doctor_main(forwarded)

        if args.command == "progress":
            from .quality import load_upgrade_progress

            progress = load_upgrade_progress(
                _catalog_path(args.root),
                Path(args.registry) if args.registry else None,
            )
            if args.json:
                print(json.dumps(progress.to_dict(), ensure_ascii=False, indent=2))
            else:
                counts = progress.counts
                print("历史 98 件升级账本（不是当前声音入口总数）")
                print(
                    f"Total: {progress.total} | fallback: {counts['fallback']} | "
                    f"candidate: {counts['candidate']} | formal: {counts['formal']} | "
                    "collaboration: "
                    + ", ".join(
                        f"{status}={count}"
                        for status, count in progress.collaboration_counts.items()
                    )
                )
                for entry in progress.entries:
                    if entry.quality_tier != "fallback":
                        print(
                            f"{entry.upgrade_id} {entry.relative_path}: "
                            f"{entry.quality_tier} ({entry.upgrade_status})"
                        )
            return 0

        if args.command == "upgrade-score":
            from .score import upgrade_legacy_score_to_v1

            upgraded = upgrade_legacy_score_to_v1(
                load_json_object(args.score)
            )
            output = Path(args.output)
            _write_json_atomic(
                output,
                upgraded,
                overwrite=args.overwrite,
            )
            print(output.resolve())
            return 0

        if args.command == "score-slice":
            from .score_ops import slice_score

            result = slice_score(
                load_json_object(args.score),
                load_json_object(args.query),
            )
            if args.output:
                output = Path(args.output)
                _write_json_atomic(
                    output,
                    result,
                    overwrite=args.overwrite,
                )
                print(output.resolve())
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "score-patch":
            from .score_ops import apply_score_patch

            result = apply_score_patch(
                load_json_object(args.score),
                load_json_object(args.patch),
            )
            output = Path(args.output)
            result_output = (
                Path(args.result_output)
                if args.result_output
                else None
            )
            if (
                result_output is not None
                and output.resolve() == result_output.resolve()
            ):
                raise ValueError(
                    "新乐谱输出与 patch 结果输出不能是同一文件"
                )
            # Preflight every target before writing either file, so a refused
            # overwrite cannot leave a half-published revision pair.
            for target in (output, result_output):
                if (
                    target is not None
                    and target.exists()
                    and not args.overwrite
                ):
                    raise ValueError(
                        f"输出已存在，默认拒绝覆盖: {target};"
                        "确认后传 --overwrite"
                    )
            _write_json_atomic(
                output,
                result["score"],
                overwrite=args.overwrite,
            )
            if result_output is not None:
                _write_json_atomic(
                    result_output,
                    result,
                    overwrite=args.overwrite,
                )
            print(
                f"{result['before_score_sha256']} -> "
                f"{result['after_score_sha256']}"
            )
            print(output.resolve())
            return 0

        if args.command == "score-compare":
            from .score_ops import compare_scores

            result = compare_scores(
                load_json_object(args.before),
                load_json_object(args.after),
                max_changes=args.max_changes,
            )
            if args.output:
                output = Path(args.output)
                _write_json_atomic(
                    output,
                    result,
                    overwrite=args.overwrite,
                )
                print(output.resolve())
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "candidate-locate":
            from .candidate import locate_candidate

            result = locate_candidate(
                args.candidate,
                at_seconds=args.at,
                tail_lookback_seconds=args.tail_lookback,
                upcoming_seconds=args.upcoming,
                max_events=args.max_events,
            )
            if args.output:
                output = Path(args.output)
                _write_json_atomic(
                    output,
                    result,
                    overwrite=args.overwrite,
                )
                print(output.resolve())
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "candidate-compare":
            from .candidate import compare_candidates

            result = compare_candidates(
                args.before,
                args.after,
                max_changes=args.max_changes,
            )
            if args.output:
                output = Path(args.output)
                _write_json_atomic(
                    output,
                    result,
                    overwrite=args.overwrite,
                )
                print(output.resolve())
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "project-render":
            from .candidate import (
                candidate_publication,
                canonical_json_sha256,
                prepare_candidate_target,
                publish_candidate_metadata,
            )
            from .capability import load_capabilities
            from .conductor import ExpressionSettings, build_plan
            from .ensemble import render_plan
            from .preflight import enforce_roster_availability
            from .project_review import build_project_review_safely
            from .render_profile import parse_render_profile
            from .resource_limits import (
                validate_render_request_resource_limits,
                validate_score_resource_limits,
            )
            from .roster import parse_roster_document
            from .score import parse_score_document

            raw_score = load_json_object(args.score)
            raw_roster = load_json_object(args.roster)
            score = parse_score_document(raw_score)
            validate_score_resource_limits(raw_score, score)
            table = load_capabilities(_catalog_path(args.root))
            roster = parse_roster_document(raw_roster, table)
            enforce_roster_availability(roster)
            profile = parse_render_profile(
                load_json_object(args.render_profile)
                if args.render_profile
                else None
            )
            settings = ExpressionSettings.from_dict(
                {
                    "mode": profile.expression,
                    "range_mode": profile.range_mode,
                    "humanize": {"seed": profile.seed},
                }
            )
            plan = build_plan(score, roster, settings)
            resource_preflight = validate_render_request_resource_limits(
                plan,
                write_stems=profile.write_stems,
                space=profile.space,
                collaboration_mode=profile.collaboration_mode,
                stem_cache_enabled=profile.use_stem_cache,
            )
            plan_sha256 = canonical_json_sha256(plan.to_dict())
            project_review = build_project_review_safely(
                plan,
                roster,
                binding={
                    "score_sha256": canonical_json_sha256(raw_score),
                    "roster_sha256": canonical_json_sha256(raw_roster),
                    "performance_plan_sha256": plan_sha256,
                },
            )
            title = (
                args.title
                or str(raw_score.get("title", "")).strip()
                or Path(args.score).stem
            )
            output_root = (
                Path(args.output_root)
                if args.output_root
                else discover_runtime_layout().output / "候选"
            )
            target = prepare_candidate_target(
                output_root,
                title,
                plan_sha256=plan_sha256,
                output_id=args.output_id,
                overwrite=args.overwrite,
                expected_receipt_sha256=(
                    args.expected_receipt_sha256
                ),
            )
            for item in project_review["items"]:
                if item["level"] == "warning":
                    print(
                        f"review[{item['code']}]: {item['message']}",
                        file=sys.stderr,
                    )
            with candidate_publication(target) as staging:
                result = render_plan(
                    plan,
                    staging.directory,
                    write_stems=profile.write_stems,
                    master_gain_db=profile.master_gain_db,
                    normalize_peak_db=profile.normalize_peak_db,
                    space=profile.space,
                    collaboration_mode=profile.collaboration_mode,
                    stem_cache_directory=(
                        output_root.parent / ".tianlai-cache" / "stems"
                        if profile.use_stem_cache
                        else None
                    ),
                    analysis_cache_directory=(
                        output_root.parent
                        / ".tianlai-cache"
                        / "analysis"
                        if profile.use_stem_cache
                        else None
                    ),
                    refresh_stem_cache=profile.refresh_stem_cache,
                    _acquire_output_lock=False,
                )
                if not result.receipt_path:
                    raise ValueError(
                        "渲染成功但没有生成可绑定的渲染回执"
                    )
                if not result.post_render_check_path:
                    raise ValueError(
                        "渲染成功但没有生成可绑定的渲染后自检"
                    )
                staging_root = staging.directory.resolve()
                mix_relative = Path(result.mix_path).resolve().relative_to(
                    staging_root
                )
                receipt_relative = Path(
                    result.receipt_path
                ).resolve().relative_to(staging_root)
                post_render_check_relative = Path(
                    result.post_render_check_path
                ).resolve().relative_to(staging_root)
                manifest = publish_candidate_metadata(
                    staging,
                    title=title,
                    score=raw_score,
                    roster=raw_roster,
                    render_profile=profile.to_dict(),
                    receipt_path=result.receipt_path,
                    plan_sha256=plan_sha256,
                    parent_candidate_id=args.parent_candidate,
                )
            print(
                json.dumps(
                    {
                        "kind": "tianlai.project_render_result",
                        "schema_version": 1,
                        "ok": True,
                        "candidate_id": target.candidate_id,
                        "candidate_directory": str(
                            target.directory.resolve()
                        ),
                        "candidate_manifest": str(
                            (
                                target.directory
                                / "候选.json"
                            ).resolve()
                        ),
                        "mix_wav": str(
                            (target.directory / mix_relative).resolve()
                        ),
                        "render_receipt": str(
                            (
                                target.directory / receipt_relative
                            ).resolve()
                        ),
                        "post_render_check": str(
                            (
                                target.directory
                                / post_render_check_relative
                            ).resolve()
                        ),
                        "post_render_check_summary": (
                            result.post_render_check_summary
                        ),
                        "duration_seconds": result.duration_seconds,
                        "mix_peak": result.mix_peak,
                        "score_sha256": manifest["project"]["score"][
                            "canonical_sha256"
                        ],
                        "roster_sha256": manifest["project"]["roster"][
                            "canonical_sha256"
                        ],
                        "render_profile_sha256": manifest["project"][
                            "render_profile"
                        ]["canonical_sha256"],
                        "performance_plan_sha256": plan_sha256,
                        "render_preflight": resource_preflight,
                        "project_review": project_review,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "project-import":
            from .capability import load_capabilities
            from .project_import import import_project, write_import_bundle
            from .trust import load_trusted_instruments

            catalog_path = _catalog_path(args.root)
            capabilities = load_capabilities(catalog_path)
            trusted_only = not args.open_palette
            trusted = (
                load_trusted_instruments(
                    catalog_path.parent / "可信乐器.json",
                    capabilities,
                )
                if trusted_only
                else None
            )
            bundle = import_project(
                args.input,
                capabilities=capabilities,
                trusted_only=trusted_only,
                trusted_instruments=trusted,
                candidate_limit=args.candidate_limit,
            )
            parser_warnings = [
                str(warning)
                for warning in bundle["import_report"].get("warnings", [])
                if not (
                    str(warning).startswith("共 ")
                    and str(warning).endswith(" 小节")
                )
            ]
            if parser_warnings and args.loss_policy == "reject":
                raise ValueError(
                    "导入检测到当前 score 不能无损表达的源语义；"
                    "默认拒绝发布导入包。检查后可传 "
                    "--loss-policy warn 或 allow。"
                    " 首项: "
                    + parser_warnings[0]
                )
            if parser_warnings and args.loss_policy == "warn":
                for warning in parser_warnings:
                    print(f"warning: {warning}", file=sys.stderr)
            bundle["import_report"]["loss_policy"] = args.loss_policy
            bundle["import_report"]["semantic_loss_warning_count"] = len(
                parser_warnings
            )
            basename = args.basename or Path(args.input).stem
            if (
                not basename
                or Path(basename).name != basename
                or basename in {".", ".."}
            ):
                raise ValueError("--basename 必须是单个安全文件名前缀")
            paths = write_import_bundle(
                bundle,
                args.output,
                overwrite=args.overwrite,
                filenames={
                    "score": f"{basename}.score.json",
                    "import_report": f"{basename}.import-report.json",
                    "roster_draft": f"{basename}.roster-draft.json",
                },
            )
            print(
                f"导入 {len(bundle['score']['parts'])} 个声部；"
                f"{len(parser_warnings)} 项源语义警告；"
                "roster 草稿仍不可执行"
            )
            print(json.dumps(paths, ensure_ascii=False, indent=2))
            return 0

        if args.command == "roster-promote":
            from .capability import load_capabilities
            from .project_import import promote_roster
            from .trust import load_trusted_instruments

            catalog_path = _catalog_path(args.root)
            capabilities = load_capabilities(catalog_path)
            trusted_only = not args.open_palette
            trusted = (
                load_trusted_instruments(
                    catalog_path.parent / "可信乐器.json",
                    capabilities,
                )
                if trusted_only
                else None
            )
            assignments: list[dict[str, object]] = []
            if args.assignments:
                raw_assignments = _load_json_value(args.assignments)
                if isinstance(raw_assignments, dict):
                    raw_assignments = raw_assignments.get("assignments")
                if not isinstance(raw_assignments, list):
                    raise ValueError(
                        "--assignments 必须是数组或 {assignments:[...]} 对象"
                    )
                assignments.extend(raw_assignments)
            for raw in args.assign:
                if "=" not in raw:
                    raise ValueError(
                        "--assign 必须写成 PART=INSTRUMENT"
                    )
                part_id, instrument = raw.split("=", 1)
                if not part_id.strip() or not instrument.strip():
                    raise ValueError(
                        "--assign 的 PART 与 INSTRUMENT 均不能为空"
                    )
                assignments.append(
                    {
                        "part": part_id.strip(),
                        "instrument": instrument.strip(),
                    }
                )
            if not assignments:
                raise ValueError(
                    "请用 --assign 或 --assignments 显式确认每个声部"
                )
            collaboration = (
                _load_json_value(args.collaboration)
                if args.collaboration
                else None
            )
            if collaboration is not None and not isinstance(
                collaboration,
                dict,
            ):
                raise ValueError("--collaboration 必须是 JSON 对象")
            roster = promote_roster(
                load_json_object(args.draft),
                load_json_object(args.score),
                assignments,
                capabilities,
                trusted_only=trusted_only,
                trusted_instruments=trusted,
                name=args.name,
                collaboration=collaboration,
            )
            output = Path(args.output)
            _write_json_atomic(
                output,
                roster,
                overwrite=args.overwrite,
            )
            print(
                f"正式编制已确认: {len(roster['assignments'])} 个显式路由"
            )
            print(output.resolve())
            return 0

        if args.command == "import-midi":
            from .midi_import import build_roster_draft, read_midi
            from .score import parse_score_document

            document, report = read_midi(args.midi)
            # 立刻按乐谱层的规则回读一遍:导入器产出的东西必须自己就能通过校验,
            # 否则错误会拖到渲染时才暴露。
            parse_score_document(document)
            output = Path(args.output)
            draft_output = (
                Path(args.roster_draft_output)
                if args.roster_draft_output
                else _default_midi_roster_draft_path(output)
            )
            if output.resolve() == draft_output.resolve():
                raise ValueError("score 输出与 roster 草稿输出不能是同一个文件")
            draft = build_roster_draft(document, report)
            for target in (output, draft_output):
                if target.exists() and not args.overwrite:
                    raise ValueError(
                        f"输出已存在，默认拒绝覆盖: {target};"
                        "确认后传 --overwrite"
                    )
            _write_json_atomic(
                output,
                document,
                overwrite=args.overwrite,
            )
            _write_json_atomic(
                draft_output,
                draft,
                overwrite=args.overwrite,
            )
            print(f"{report.title}:{len(report.parts)} 个声部,"
                  f"{report.tempo_changes} 处速度变化,{report.meter_changes} 处拍号变化")
            for part in report.parts:
                tag = " [打击通道]" if part["percussion"] else ""
                print(f"  {part['id']:<24s} 通道 {part['channel_1based']:2d} "
                      f"{part['note_count']:5d} 音  {part['range']}{tag}")
                if part["noteheads"]:
                    print(f"      符头: {', '.join(part['noteheads'])}")
            for warning in report.warnings:
                print(f"  note: {warning}")
            print(output.resolve())
            print(draft_output.resolve())
            return 0

        if args.command == "export-midi":
            from .midi_export import MidiExportLossError, export_midi

            output = Path(args.output)
            report_output = (
                Path(args.report_output)
                if args.report_output
                else output.with_suffix(
                    output.suffix + ".export-report.json"
                )
            )
            if output.resolve() == report_output.resolve():
                raise ValueError("MIDI 输出与 loss report 不能是同一文件")
            for target in (output, report_output):
                if target.exists() and not args.overwrite:
                    raise ValueError(
                        f"输出已存在，默认拒绝覆盖: {target};"
                        "确认后传 --overwrite"
                    )
            score_data = load_json_object(args.score)
            roster_data = (
                load_json_object(args.roster)
                if args.roster
                else None
            )
            try:
                report = export_midi(
                    score_data,
                    output,
                    roster=roster_data,
                    allow_lossy=args.allow_lossy,
                    overwrite=args.overwrite,
                )
            except MidiExportLossError as exc:
                _write_json_atomic(
                    report_output,
                    exc.report,
                    overwrite=args.overwrite,
                )
                print(
                    f"loss report: {report_output.resolve()}",
                    file=sys.stderr,
                )
                raise
            _write_json_atomic(
                report_output,
                report,
                overwrite=args.overwrite,
            )
            print(
                f"{report['track_count']} 轨，"
                f"{report['blocking_loss_count']} 项已确认的阻断性语义损失"
            )
            print(output.resolve())
            print(report_output.resolve())
            return 0

        if args.command == "import-musicxml":
            from .musicxml_import import read_musicxml
            from .score import parse_score_document

            document, report = read_musicxml(args.musicxml)
            # 导入结果必须立即通过内部乐谱合同，不能把歧义拖到正式渲染才暴露。
            parse_score_document(document)
            output = Path(args.output)
            _write_json_atomic(
                output,
                document,
                overwrite=args.overwrite,
            )
            print(
                f"{report.title}:{len(report.parts)} 个声部,"
                f"{report.tempo_changes} 处速度标记,"
                f"{report.meter_changes} 处拍号标记"
            )
            for part in report.parts:
                tag = " [打击乐]" if part.get("percussion") else ""
                print(
                    f"  {part['id']:<24s} {part['note_count']:5d} 音  "
                    f"{part['range']}{tag}"
                )
            for warning in report.warnings:
                print(f"  note: {warning}")
            print(output.resolve())
            return 0

        if args.command == "capabilities":
            from .capability import load_capabilities

            table = load_capabilities(_catalog_path(args.root))
            table = {
                key: capability
                for key, capability in table.items()
                if capability.quality_tier is not None
                and (
                    args.include_local_compatibility
                    or capability.implementation_type != "soundfont"
                )
                and (
                    args.include_quarantined
                    or capability.license_status != "quarantined"
                )
            }
            if args.json:
                print(
                    json.dumps(
                        [table[key].to_dict() for key in sorted(table)],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            for key in sorted(table):
                capability = table[key]
                span = (
                    f"{capability.note_min:.0f}~{capability.note_max:.0f}"
                    if capability.note_min is not None and capability.note_max is not None
                    else ("固定音高" if capability.ignores_pitch else "未声明")
                )
                names = ", ".join(capability.articulations) or "(无)"
                print(f"{key}\n    音域 {span} | 奏法 {names} [{capability.articulation_source}]")
                if capability.articulation_playable_ranges:
                    overrides = []
                    for name, ranges in capability.articulation_playable_ranges:
                        spans = ", ".join(
                            f"{low:g}~{high:g}" for low, high in ranges
                        )
                        overrides.append(f"{name}={spans}")
                    print(
                        "    奏法专属音域 "
                        + "; ".join(overrides)
                        + "（未列奏法继承全局）"
                    )
            print(f"Total: {len(table)}")
            return 0

        if args.command == "ensemble":
            from .canonical_json import canonical_json_sha256
            from .capability import load_capabilities
            from .conductor import ExpressionSettings, build_plan
            from .ensemble import render_plan
            from .preflight import enforce_roster_availability
            from .project_review import build_project_review_safely
            from .render_profile import (
                parse_render_profile,
                profile_with_overrides,
            )
            from .resource_limits import (
                validate_render_request_resource_limits,
                validate_score_resource_limits,
            )
            from .roster import parse_roster_document
            from .score import parse_score_document
            from .space import SpaceConfig

            raw_score = load_json_object(args.score)
            score = parse_score_document(raw_score)
            validate_score_resource_limits(raw_score, score)
            table = load_capabilities(_catalog_path(args.root))
            raw_roster = load_json_object(args.roster)
            roster = parse_roster_document(raw_roster, table)
            enforce_roster_availability(roster)
            profile = parse_render_profile(
                load_json_object(args.render_profile)
                if args.render_profile
                else None
            )
            if args.no_normalize:
                profile_document = profile.to_dict()
                profile_document["normalize_peak_db"] = None
                profile = parse_render_profile(profile_document)
            if args.space_config:
                explicit_space: SpaceConfig | bool | None = (
                    SpaceConfig.from_dict(
                        load_json_object(args.space_config)
                    )
                )
                if explicit_space is None:
                    explicit_space = False
            elif args.hall is not None:
                explicit_space = SpaceConfig() if args.hall else False
            else:
                explicit_space = None
            profile = profile_with_overrides(
                profile,
                expression=args.expression,
                range_mode=args.range_mode,
                seed=args.seed,
                master_gain_db=args.master_gain_db,
                normalize_peak_db=args.normalize_peak_db,
                space=explicit_space,
                collaboration_mode=args.collaboration_mode,
                write_stems=False if args.no_stems else None,
                use_stem_cache=False if args.no_stem_cache else None,
                refresh_stem_cache=(
                    True if args.refresh_stem_cache else None
                ),
            )
            space = profile.space
            settings = ExpressionSettings.from_dict(
                {
                    "mode": profile.expression,
                    "range_mode": profile.range_mode,
                    "humanize": {"seed": profile.seed},
                }
            )
            plan = build_plan(score, roster, settings)
            resource_preflight = validate_render_request_resource_limits(
                plan,
                write_stems=profile.write_stems,
                space=profile.space,
                collaboration_mode=profile.collaboration_mode,
                stem_cache_enabled=profile.use_stem_cache,
            )
            plan_sha256 = canonical_json_sha256(plan.to_dict())
            project_review = build_project_review_safely(
                plan,
                roster,
                binding={
                    "score_sha256": canonical_json_sha256(raw_score),
                    "roster_sha256": canonical_json_sha256(raw_roster),
                    "performance_plan_sha256": plan_sha256,
                },
            )
            directory = Path(args.output)
            for item in project_review["items"]:
                if item["level"] == "warning":
                    print(
                        f"review[{item['code']}]: {item['message']}",
                        file=sys.stderr,
                    )
            print(
                f"{len(plan.parts)} 个执行器,{sum(len(part.trace) for part in plan.parts)} 个音符,"
                f"{plan.duration_seconds:.2f}s"
            )
            if args.plan_only:
                directory.mkdir(parents=True, exist_ok=True)
                plan_path = directory / "演奏计划.json"
                plan_path.write_text(
                    json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                profile_path = directory / "渲染配置.json"
                profile_path.write_text(
                    json.dumps(
                        profile.to_dict(),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                preflight_path = directory / "资源预检.json"
                preflight_path.write_text(
                    json.dumps(
                        resource_preflight,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                review_path = directory / "创作自检.json"
                review_path.write_text(
                    json.dumps(
                        project_review,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"演奏计划: {plan_path.resolve()}")
                print(f"渲染配置: {profile_path.resolve()}")
                print(f"资源预检: {preflight_path.resolve()}")
                print(f"创作自检: {review_path.resolve()}")
                return 0
            result = render_plan(
                plan,
                directory,
                write_stems=profile.write_stems,
                master_gain_db=profile.master_gain_db,
                normalize_peak_db=profile.normalize_peak_db,
                space=space,
                collaboration_mode=profile.collaboration_mode,
                stem_cache_directory=(
                    None
                    if not profile.use_stem_cache
                    else (
                        Path(args.stem_cache_directory)
                        if args.stem_cache_directory
                        else directory.parent
                        / ".tianlai-cache"
                        / "stems"
                    )
                ),
                analysis_cache_directory=(
                    None
                    if not profile.use_stem_cache
                    else (
                        (
                            Path(args.stem_cache_directory).parent
                            / "analysis"
                        )
                        if args.stem_cache_directory
                        else directory.parent
                        / ".tianlai-cache"
                        / "analysis"
                    )
                ),
                refresh_stem_cache=profile.refresh_stem_cache,
            )
            profile_path = directory / "渲染配置.json"
            _write_json_atomic(
                profile_path,
                profile.to_dict(),
                overwrite=True,
            )
            review_path = directory / "创作自检.json"
            _write_json_atomic(
                review_path,
                project_review,
                overwrite=True,
            )
            print(f"渲染配置: {profile_path.resolve()}")
            print(f"创作自检: {review_path.resolve()}")
            if result.plan_path:
                print(f"演奏计划: {Path(result.plan_path).resolve()}")
            for stem in result.stems:
                print(f"  {stem.executor_id:16s} 峰值 {stem.peak:.4f} 最大复音 {stem.peak_voices}")
            if result.pre_normalize_peak is not None:
                print(
                    f"归一前峰值 {result.pre_normalize_peak:.4f}"
                    f" → 施加 {result.normalize_gain_db:+.2f} dB"
                )
            print(f"总线峰值 {result.mix_peak:.4f}")
            if result.stem_cache is not None:
                cache = result.stem_cache
                print(
                    "原始分轨缓存 "
                    f"命中 {cache['hits']} / 未命中 {cache['misses']} / "
                    f"绕过 {cache['bypassed']} / 写入 {cache['writes']}"
                )
            if result.analysis_cache is not None:
                cache = result.analysis_cache
                print(
                    "协奏分析缓存 "
                    f"分轨命中 {cache['stem']['hits']} / "
                    f"未命中 {cache['stem']['misses']} / "
                    f"绕过 {cache['stem']['bypassed']}；"
                    f"关系命中 {cache['relation']['hits']} / "
                    f"未命中 {cache['relation']['misses']} / "
                    f"绕过 {cache['relation']['bypassed']}"
                )
            if result.cache_telemetry_path:
                print(
                    "缓存遥测: "
                    f"{Path(result.cache_telemetry_path).resolve()}"
                )
            print(Path(result.mix_path).resolve())
            if result.receipt_path:
                print(f"渲染回执: {Path(result.receipt_path).resolve()}")
            if result.mix_report_path:
                summary = (result.mix_report or {}).get("summary", {})
                print(
                    "混音诊断: "
                    f"{Path(result.mix_report_path).resolve()} "
                    f"(告警 {summary.get('warning_count', 0)})"
                )
            if result.license_sidecar_path:
                print(
                    "许可清单: "
                    f"{Path(result.license_sidecar_path).resolve()}"
                )
            if result.attribution_path:
                print(
                    "署名说明: "
                    f"{Path(result.attribution_path).resolve()}"
                )
            post_render_check_path = getattr(
                result,
                "post_render_check_path",
                None,
            )
            if post_render_check_path:
                print(
                    "渲染后自检: "
                    f"{Path(post_render_check_path).resolve()}"
                )
            post_render_check_summary = getattr(
                result,
                "post_render_check_summary",
                None,
            )
            if post_render_check_summary:
                summary = post_render_check_summary
                status = str(summary.get("status", "unknown"))
                counts = []
                for key, label in (
                    ("blocking_count", "阻断"),
                    ("review_count", "待复核"),
                    ("advisory_count", "提示"),
                ):
                    value = summary.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        counts.append(f"{label} {value}")
                suffix = f" ({', '.join(counts)})" if counts else ""
                print(f"渲染后自检状态: {status}{suffix}")
            return 0

        if args.command == "catalog":
            from .catalog import discover_instruments

            entries = [
                entry
                for entry in discover_instruments(_catalog_path(args.root))
                if entry.quality_tier is not None
                and (
                    args.include_local_compatibility
                    or entry.implementation_type != "soundfont"
                )
                and (
                    args.include_quarantined
                    or entry.license_status != "quarantined"
                )
            ]
            if args.json:
                print(json.dumps([entry.to_dict() for entry in entries], ensure_ascii=False, indent=2))
            else:
                for entry in entries:
                    patch = (
                        f" bank={entry.bank} program={entry.program}"
                        if entry.program is not None
                        else ""
                    )
                    quality = f" quality={entry.quality_tier}" if entry.quality_tier else ""
                    collaboration = (
                        f" collaboration={entry.collaboration_review_status}"
                        if entry.collaboration_review_status
                        else ""
                    )
                    license_status = (
                        f" license={entry.license_status}"
                        if entry.license_status
                        else ""
                    )
                    print(
                        f"{entry.category} / {entry.name} "
                        f"[{entry.implementation_type}]{patch}{quality}"
                        f"{collaboration}{license_status}"
                    )
                print(f"Total: {len(entries)}")
            return 0

        if args.command == "analyze-pitch":
            from .analysis import analyze_file_pitch

            measurement = analyze_file_pitch(args.audio, args.expected_hz)
            print(f"Measured: {measurement.measured_hz:.4f} Hz")
            print(f"Expected: {measurement.expected_hz:.4f} Hz")
            print(f"Detune: {measurement.detune_cents:+.3f} cents")
            return 0

        if args.command == "render":
            manifest_path = Path(args.instrument).resolve()
            manifest = load_json_object(manifest_path)
            _enforce_cli_manifest_availability(
                manifest,
                allow_local_compatibility_soundfont=(
                    args.allow_local_compatibility_soundfont
                ),
            )
            result = render_to_wav_atomic(
                args.instrument,
                args.events,
                args.output,
            )
            print(
                f"Rendered {result.duration_seconds:.3f}s at {result.sample_rate} Hz "
                f"({result.frame_count} frames, peak {result.peak_active_voices} voices)"
            )
            print(Path(args.output).resolve())
            if result.license_sidecar_path:
                print(
                    "许可清单: "
                    f"{Path(result.license_sidecar_path).resolve()}"
                )
            if result.attribution_path:
                print(
                    "署名说明: "
                    f"{Path(result.attribution_path).resolve()}"
                )
            post_render_check_path = getattr(
                result,
                "post_render_check_path",
                None,
            )
            if post_render_check_path:
                print(
                    "渲染后自检: "
                    f"{Path(post_render_check_path).resolve()}"
                )
            post_render_check_summary = getattr(
                result,
                "post_render_check_summary",
                None,
            )
            if post_render_check_summary:
                summary = post_render_check_summary
                status = str(summary.get("status", "unknown"))
                counts = []
                for key, label in (
                    ("blocking_count", "阻断"),
                    ("review_count", "待复核"),
                    ("advisory_count", "提示"),
                ):
                    value = summary.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        counts.append(f"{label} {value}")
                suffix = f" ({', '.join(counts)})" if counts else ""
                print(f"渲染后自检状态: {status}{suffix}")
            return 0

        manifest_path = Path(args.instrument).resolve()
        manifest = load_json_object(manifest_path)
        _enforce_cli_manifest_availability(
            manifest,
            allow_local_compatibility_soundfont=(
                args.allow_local_compatibility_soundfont
            ),
        )
        performance = parse_performance_document(load_json_object(args.events))
        instrument = create_instrument(
            manifest,
            performance.sample_rate,
            base_directory=str(manifest_path.parent),
        )
        try:
            print(
                f"Valid: {len(performance.events)} events, "
                f"{performance.total_samples / performance.sample_rate:.3f}s, "
                f"instrument={instrument.__class__.__name__}"
            )
            return 0
        finally:
            close = getattr(instrument, "close", None)
            if callable(close):
                close()
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        preflight = getattr(error, "preflight", None)
        if isinstance(preflight, dict):
            print(
                json.dumps(
                    {"render_preflight": preflight},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
        return 2
