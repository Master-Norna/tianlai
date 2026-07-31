# 班卓琴（itsclipping Ganjo 六弦班卓吉他）

本目录保留“班卓琴”这一项目入口，但正式音源已覆盖为 itsclipping Ganjo：
一把 SX 品牌的 six-string guitar-banjo（六弦班卓吉他）。渲染引擎复用
`tianlai/dedicated_sfz.py`，不含通用 SoundFont 静默回退。

## 来源与许可

- 上游：itsclipping Ganjo / SX six-string guitar-banjo
- 固定版本：`v1.000 @ ccff5cd5cd3b513873a48994c07724d9d3c39e1c`
- 许可：CC0-1.0，当前 `license_status=approved`
- 许可证与上游说明随资源冻结为 `LICENSE.md`、`README.md`
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json)，复算脚本为
  [`核验资源.py`](核验资源.py)

## 映射与奏法

- 默认奏法 `normal`
- 映射：`ganjo.sfz`
- 音域：D2-C5（MIDI 38-72）
- 冻结的上游映射含 58 个区域、58 个独立采样；项目映射层精确排除 3 个
  G♯4 离群轮替后，实际启用 55 个区域、55 个采样，其中 39 个区域参与
  round robin
- 上游没有真实力度层；输入力度仅由渲染器进行连续增益塑形
- 人工试听发现 MIDI 47（上游命名 `B3.wav`）比相邻音弱约 3.5 dB；当前通过
  `sample_gain_db_overrides` 对该采样追加 `+3.2 dB`。这是响度校正，不更换
  采样、不改变音高，也不修改固定的上游资源树
- 人工试听还发现 MIDI 56（上游命名 G♯4）六轮替分成两组明显不同的触弦
  质地：旧 RR1、RR5、RR6 偏亮且触弦感突兀，旧 RR2、RR3、RR4 较统一。
  当前通过 `sample_region_exclusions` 排除前一组，并把保留的三枚连续重编号为
  三轮替；原始 CC0 WAV 与 SFZ 仍保持不变

## 调音

运行时启用逐采样音高校准（`apply_pitch_calibration=true`）。校准报告由当前
58 个根采样重新测量生成：相对上游映射的实测偏差中位为 +44.782 音分，最大绝对
偏差为 58.715 音分；渲染时按每个采样各自的实测值校正，详见
[`音准校准.json`](音准校准.json)。

## 质量状态

单音色质量状态为 `formal`；协奏、实际曲目与自动配器仍为 `untested`。旧版
FlameStudios 五弦音源的试听报告不构成本音源的质量证据，需按新音源重新渲染试听。

## 已知限制

六弦班卓吉他不是传统五弦班卓琴的“更精细版本”：它把吉他式六弦结构与演奏逻辑
带入班卓共鸣箱，缺少传统五弦班卓的短第五弦及其典型滚奏指法特征。因此本入口能
提供可信的六弦 guitar-banjo 音色，但不能冒充传统五弦班卓的完整演奏模型。
