"""Full-catalog MCP routing coverage without opening or rendering audio assets."""

from __future__ import annotations

import importlib.util
import unittest


_HAS_MCP = importlib.util.find_spec("mcp") is not None


def _probe_note(capability, articulation: str | None) -> float:
    ranges = capability.ranges_for(articulation)
    if ranges:
        low, high = ranges[0]
        return (low + high) / 2.0
    if capability.fixed_midi_note is not None:
        return float(capability.fixed_midi_note)
    if capability.note_min is not None and capability.note_max is not None:
        return (capability.note_min + capability.note_max) / 2.0
    return 60.0


def _score(note: float, articulation: str | None = None) -> dict:
    event = {
        "event_id": "probe-0001",
        "bar": 1,
        "beat": 1.0,
        "duration_beats": 1.0,
        "pitch": note,
        "velocity": 0.5,
    }
    if articulation is not None:
        event["articulation"] = articulation
    return {
        "schema_version": 1,
        "title": "MCP formal instrument probe",
        "sample_rate": 48_000,
        "tail_seconds": 0.25,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 120.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [{"id": "Probe", "notes": [event]}],
    }


def _roster(instrument: str) -> dict:
    return {
        "assignments": [
            {
                "part": "Probe",
                "executor_id": "probe",
                "instrument": instrument,
            }
        ]
    }


@unittest.skipUnless(_HAS_MCP, "optional mcp package is not installed")
class MCPFormalInstrumentScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tianlai import mcp_server

        cls.m = mcp_server
        cls.capabilities = mcp_server._caps()
        cls.formal = mcp_server._formal_set()

    def test_default_scope_is_exactly_all_103_public_formal_entries(self) -> None:
        listed = self.m.list_instruments(detail_level="summary", limit=128)

        self.assertTrue(listed["ok"])
        self.assertEqual(listed["instrument_scope"], "formal")
        self.assertEqual(listed["catalog_count"], 103)
        self.assertEqual(
            {item["instrument"] for item in listed["instruments"]},
            self.formal,
        )
        self.assertNotIn("测试工具/参考振荡器", self.formal)

    def test_all_103_instruments_and_213_articulation_routes_compile(self) -> None:
        failures = []
        route_count = 0
        for instrument in sorted(self.formal):
            capability = self.capabilities[instrument]
            articulations = capability.articulations or (None,)
            for articulation in articulations:
                route_count += 1
                result = self.m.validate_project(
                    _score(
                        _probe_note(capability, articulation),
                        articulation,
                    ),
                    _roster(instrument),
                    instrument_scope="formal",
                    render_profile={
                        "kind": "tianlai.render_profile",
                        "schema_version": 1,
                        "name": "mcp-catalog-probe",
                        "space": {"enabled": False},
                        "write_stems": False,
                        "collaboration_mode": "manual",
                        "use_stem_cache": False,
                    },
                )
                if not result["ok"]:
                    failures.append(
                        {
                            "instrument": instrument,
                            "articulation": articulation,
                            "issues": result.get("issues", []),
                        }
                    )

        self.assertEqual(route_count, 213)
        self.assertEqual(failures, [])

    def test_all_27_percussion_entries_compile_as_explicit_kit_routes(self) -> None:
        failures = []
        percussion = [
            self.capabilities[instrument]
            for instrument in sorted(self.formal)
            if self.capabilities[instrument].routing_class == "percussion"
        ]
        for capability in percussion:
            target = _probe_note(capability, capability.default_articulation)
            transpose = int(round(target - 60.0))
            roster = {
                "assignments": [
                    {
                        "part": "Probe",
                        "kit": {
                            "C4": {
                                "instrument": capability.relative_path,
                                "transpose": transpose,
                            }
                        },
                    }
                ]
            }
            result = self.m.validate_project(
                _score(60.0),
                roster,
                instrument_scope="formal",
                hall=False,
                write_stems=False,
                collaboration_mode="manual",
                use_stem_cache=False,
            )
            if not result["ok"]:
                failures.append(
                    {
                        "instrument": capability.relative_path,
                        "issues": result.get("issues", []),
                    }
                )

        self.assertEqual(len(percussion), 27)
        self.assertEqual(failures, [])

    def test_ignore_pitch_kit_route_requires_a_valid_selector_key(self) -> None:
        without_transpose = {
            "assignments": [
                {
                    "part": "Probe",
                    "kit": {"D3": "现代鼓组/高音通鼓"},
                }
            ]
        }
        with_transpose = {
            "assignments": [
                {
                    "part": "Probe",
                    "kit": {
                        "D3": {
                            "instrument": "现代鼓组/高音通鼓",
                            "transpose": 10,
                        }
                    },
                }
            ]
        }

        rejected = self.m.validate_project(
            _score(50.0),
            without_transpose,
            instrument_scope="formal",
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )
        accepted = self.m.validate_project(
            _score(50.0),
            with_transpose,
            instrument_scope="formal",
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )

        self.assertFalse(rejected["ok"])
        self.assertTrue(accepted["ok"], accepted.get("issues"))


if __name__ == "__main__":
    unittest.main()
