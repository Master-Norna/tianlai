"""复算中音萨克斯全部有音高 FLAC 的 tune/FFT 校验证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.mtg_sax import generate_mtg_sax_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_mtg_sax_pitch_calibration(here / "乐器.json")
    print(f"已校验 {report['summary']['sample_count']} 个中音萨克斯采样：{here / '音准校准.json'}")


if __name__ == "__main__":
    main()
