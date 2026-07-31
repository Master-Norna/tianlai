"""兼容入口：运行 SIMPK 逐样本、八度感知的严格音准校准。

不要再调用通用 SFZ 根音分析器；SIMPK 1.0 的 ``rootNote`` 整体高标一个
八度，只有专用校准器会验证并保留录音的实际音区。
"""

from 校准SIMPK音源 import main


if __name__ == "__main__":
    raise SystemExit(main())
