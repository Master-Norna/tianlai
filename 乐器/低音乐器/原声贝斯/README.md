[中文](README.md) | [English](README.en.md)

# 原声贝斯(formal)

Karoryfer Meatbass 低音提琴,拨奏为主、弓奏备选。本目录是 98 项清单 SAM-10 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Karoryfer Samples: Meatbass (1958 Otto Rubner 低音提琴)
- 版本:master @ ac9e859564bd,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `pizzicato`:`Programs/04_pizz.sfz`
- `arco`:`Programs/02_arco_3vel.sfz`

默认奏法 `pizzicato`;pitch_mode `pitched`。适配器会执行上游
`<control>` 的 CC 初值：CC107 默认只选择 basic map，CC103=127 正确开启
延音，因此每个音只触发一套映射，拨奏和弓奏在 200 ms 后均不会异常静音。

资源核验后实际采用 264 个拨奏 region（确定性随机变体）和 102 个弓奏
region（Round Robin）；弓奏的 102 个 region 含有效循环边界。旧报告把多套
CC107 映射同时加载并虚报成数千 region，已经作废。

## 音域

E1(28) - G3(55)

## 调音

366 个根采样窄窗 FFT 诊断的残差中位为 -2.394 c；个别低弦弱基频会碰到
窄窗 ±180 c 搜索边界，所以该最大值只作诊断，不作为放行依据。最终门槛从
真实 manifest 渲染 pizzicato / arco 的 MIDI 28、42、55，再以 ±1800 c
宽搜索检查；六个探针均无整八度错误且在 ±35 c 内。

## 试听

固定事件:`examples/原声贝斯_奏法.events.json`;
渲染 12.20 s,峰值 0.443506,
RMS 0.037189,削波 0;
WAV SHA-256 `d979ae3c…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

民谣/流行取向录音;拨奏为主入口,古典独奏弓奏见低音提琴入口。低弦的
基频可能比二次谐波弱，验收算法必须同时看周期性和奇次谐波，不能只取最大
频谱峰。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
