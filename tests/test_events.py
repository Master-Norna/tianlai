import unittest

from tianlai.events import parse_performance_document


class PerformanceDocumentTests(unittest.TestCase):
    def test_events_are_converted_to_sample_positions(self) -> None:
        document = parse_performance_document(
            {
                "sample_rate": 48000,
                "tail_seconds": 0.5,
                "events": [
                    {"time": 0.125, "type": "note_on", "note_id": 1, "midi_note": 69},
                    {"time": 0.25, "type": "note_off", "note_id": 1},
                ],
            }
        )
        self.assertEqual(document.events[0].sample, 6000)
        self.assertEqual(document.events[1].sample, 12000)
        self.assertEqual(document.total_samples, 36000)
        self.assertNotIn("release_velocity", document.events[1].payload)

    def test_explicit_release_velocity_is_validated_and_preserved(self) -> None:
        document = parse_performance_document(
            {
                "events": [
                    {
                        "time": 0,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": 60,
                    },
                    {
                        "time": 1,
                        "type": "note_off",
                        "note_id": 1,
                        "release_velocity": 0.375,
                    },
                ]
            }
        )
        self.assertEqual(
            document.events[1].payload["release_velocity"],
            0.375,
        )

    def test_duplicate_active_note_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "already active"):
            parse_performance_document(
                {
                    "events": [
                        {"time": 0, "type": "note_on", "note_id": 1, "midi_note": 60},
                        {"time": 1, "type": "note_on", "note_id": 1, "midi_note": 64},
                    ]
                }
            )

    def test_fractional_control_is_preserved(self) -> None:
        document = parse_performance_document(
            {
                "events": [
                    {"time": 0, "type": "control", "name": "sustain_pedal", "value": 0.734}
                ]
            }
        )
        self.assertEqual(document.events[0].payload["value"], 0.734)

    def test_named_articulation_is_supported(self) -> None:
        document = parse_performance_document(
            {"events": [{"time": 0, "type": "articulation", "name": "pizzicato"}]}
        )
        self.assertEqual(document.events[0].payload["name"], "pizzicato")

    def test_note_on_rejects_two_pitch_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_performance_document(
                {
                    "events": [
                        {
                            "time": 0,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 69,
                            "pitch_hz": 440,
                            "velocity": 0.8,
                        }
                    ]
                }
            )

    def test_public_events_reject_private_sampler_controls(self) -> None:
        for private_field in ("_sample_ignore_pitch", "_sample_random_value"):
            with self.subTest(private_field=private_field):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"events\[0\].*unknown fields.*{private_field}",
                ):
                    parse_performance_document(
                        {
                            "events": [
                                {
                                    "time": 0,
                                    "type": "note_on",
                                    "note_id": 1,
                                    "midi_note": 60,
                                    private_field: True,
                                }
                            ]
                        }
                    )

    def test_event_typo_is_rejected_instead_of_reaching_backend(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"events\[0\].*unknown fields.*veloctiy",
        ):
            parse_performance_document(
                {
                    "events": [
                        {
                            "time": 0,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 60,
                            "veloctiy": 0.8,
                        }
                    ]
                }
            )

    def test_document_and_tuning_unknown_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "performance document.*unknown fields.*event",
        ):
            parse_performance_document({"event": [], "events": []})
        with self.assertRaisesRegex(ValueError, "tuning.*unknown fields.*a4"):
            parse_performance_document(
                {"tuning": {"a4": 442.0}, "events": []}
            )


if __name__ == "__main__":
    unittest.main()
