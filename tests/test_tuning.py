import math
import unittest

from tianlai.tuning import EqualTemperament


class EqualTemperamentTests(unittest.TestCase):
    def test_reference_pitch(self) -> None:
        tuning = EqualTemperament(440.0)
        self.assertEqual(tuning.note_to_hz(69), 440.0)

    def test_octaves_and_middle_c(self) -> None:
        tuning = EqualTemperament(440.0)
        self.assertAlmostEqual(tuning.note_to_hz(81), 880.0, places=10)
        self.assertAlmostEqual(tuning.note_to_hz(60), 261.6255653005986, places=10)

    def test_fractional_note_is_supported(self) -> None:
        tuning = EqualTemperament(440.0)
        self.assertAlmostEqual(tuning.note_to_hz(69.5), 440.0 * math.sqrt(2 ** (1 / 12)))


if __name__ == "__main__":
    unittest.main()

