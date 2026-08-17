**简体中文** | [English](realization-v1.en.md)

# Realization v1：可选演奏实现层

`tianlai.realization` 是 score 与最终 Performance JSON 之间的稀疏、可选层。score
继续描述音乐语义；realization 只描述创作者或导入演奏明确要求的逐音偏移和连续控制。
它不保存乐器 keyswitch、MIDI CC 编号、混音推子、空间参数或渲染事件。

当前版本已接入 conductor、CLI 和 candidate 证据链。解析器负责 score 绑定与引用校验，
编译器再针对实际 roster 做 capability、量化、语义近似和资源预算校验；任何未经明确授权
或无法留下 resolved evidence 的降级都必须阻断。不提供 realization，或提供
`note_overrides=[]`、`control_lanes=[]` 的文档，仍为严格 no-op。

## 最小文档

```json
{
  "kind": "tianlai.realization",
  "schema_version": 1,
  "score_sha256": "64 位小写 SHA-256",
  "defaults_profile": "tianlai.realization-defaults-v1",
  "mode": "interpreted",
  "note_overrides": [],
  "control_lanes": []
}
```

`defaults_profile` 固定所有省略值的解释规则，不能写 `latest`。`mode` 可以是：

- `interpreted`：显式数据来自创作者的演奏解释；
- `captured`：显式数据来自捕获或导入的实际演奏证据。

mode 本身不会改变任何省略参数，也不会暗中把数值锁定。捕获值是否必须保持，仍由
每个字段的 `strategy=lock` 明确声明，因此空的 captured 文档仍是 no-op。

## 逐音 override

每项通过 score v1 的稳定 `event_id` 定位：

```json
{
  "event_id": "event-000042",
  "timing_offset_ms": {
    "strategy": "add", "value": -12.0,
    "value_policy": "adapt", "semantic_policy": "exact"
  },
  "gate_ratio": {
    "strategy": "scale", "value": 0.84,
    "value_policy": "adapt", "semantic_policy": "exact"
  },
  "velocity": {
    "strategy": "lock", "value": 0.61,
    "value_policy": "adapt", "semantic_policy": "approximate"
  },
  "release_velocity": {
    "strategy": "replace", "value": 0.22,
    "value_policy": "exact", "semantic_policy": "exact"
  }
}
```

五种合并策略含义固定：

- `auto`：不带 `value`，沿用自动解释结果，对该参数为 no-op；
- `add`：在自动解释结果上加 `value`；
- `scale`：将自动解释结果乘以 `value`；
- `replace`：在 realization 合并阶段用 `value` 替换自动解释结果，后续允许已声明的
  音乐策略继续处理；
- `lock`：数值结果与 replace 相同，但禁止后续音乐自动化改写。实测起音补偿、安全
  检查和 capability 验证仍在 lock 之后执行并进入 trace。lock 锁定的是 Performance
  Plan 中的请求值和音乐自动化，不承诺超越采样网格或后端分辨率的物理声学连续精度；
  实际量化后的值与 fidelity 必须进入 resolved evidence。

除 `auto` 外，每个逐音参数都必须同时声明两项创作者决定：

- `value_policy=exact|adapt`：要求请求值原样可执行，或明确允许吸附到执行器/采样
  网格。对 timing 与 gate，它约束最终事件时刻；对 velocity，它约束后端实际力度
  分辨率；
- `semantic_policy=exact|approximate`：要求原生音乐语义，或明确接受 capability 给出的
  近似及原因。

二者不能互相代替。后端即使接受任意浮点 velocity，也可能只把“击键速度”近似成
振幅；反过来，SoundFont 的力度语义可以成立，但仍必须落到有限 MIDI 网格。resolved
trace 会同时保留 authored value、最终执行值、数值 fidelity、语义 fidelity 和来源。

字段单位与文档内边界：

| 字段 | add | scale | replace / lock |
|---|---:|---:|---:|
| `timing_offset_ms` | -60000..60000 ms | 0..16 | -60000..60000 ms |
| `gate_ratio` | -16..16 ratio | (0..16] | (0..16] |
| `velocity` | -1..1 | 0..16 | 0..1 |
| `release_velocity` | -1..1 | 0..16 | 0..1 |

add/scale 的文档内数值合法不代表合并结果一定合法。编译器会校验最终 timing、
gate 和归一化值，越界时失败，不能静默夹值。尤其 score 目前没有
`release_velocity` 基线；对它使用 add/scale 时，若 defaults/capability 也没有提供
可解析基线，编译必须 fail closed。replace/lock 不依赖继承基线。当前实现中只有明确
读取 release velocity 的 MTG solo sax 能力可以声明支持；piano、dedicated SFZ 等忽略
该值的后端必须拒绝编译，不能制造“字段已生效”的假象。

score 中一条 tie 链在现有演奏管线里会合并成一个持续发声事件，只保留链首
`event_id`。因此 realization v1 只允许 override tie-chain head；指向 continuation
`event_id` 的文档会在引用校验时阻断，并报告可用的链首 ID。这样可以避免看似接受、
渲染时却静默丢失的精度。若未来需要分别控制链中各段，必须先新增明确的链内语义，
不能把 continuation 当作独立起音。

## 稀疏 control lane

支持 `expression`、`sustain_pedal`、`una_corda` 和 `breath`，值域均为 0..1。通用
`modulation` 不属于 realization v1：现有后端分别把它解释为增益、颤音、attack latch、
频谱变化或 SoundFont CC1，并不存在稳定的音乐语义。未来应拆成 `vibrato_depth`、
`attack_shape`、`timbre` 等明确意图，再由 capability adapter 映射。lane 可以作用于
整个 part，也可以加 `voice` 限定：

```json
{
  "lane_id": "piano-pedal",
  "target": { "part_id": "Piano", "voice": "upper" },
  "control": "sustain_pedal",
  "interpolation": "step",
  "time_policy": "exact",
  "value_policy": "exact",
  "semantic_policy": "exact",
  "points": [
    { "bar": 12, "beat": 1.5, "value": 1.0 },
    { "bar": 13, "beat": 1.0, "value": 0.0 }
  ]
}
```

点必须按 `(bar, beat)` 严格递增，但首点不必位于 1:1：

- 首点前采用目标执行器 capability 明确声明的 `default_value`；realization 层绝不
  硬编码所谓统一中性值；
- capability 没有声明首点前默认值时，编译器必须阻断；
- `step` 在显式点处切换并保持到下一点；
- `linear` 只在相邻显式点之间线性插值；首点前保持 capability default，末点后
  保持末点值；
- resolved Performance Plan 必须物化实际首点前值、插值结果来源和 capability 证据。

每条 lane 必须声明 `value_policy`：

- `exact`：目标 capability 必须原样实现请求值；例如只有 128 级的控制不能冒充任意
  浮点精度，量化会阻断；
- `adapt`：创作者明确授权 capability 的 `adapt_value` 规则选择最近可执行值；resolved
  plan 必须同时保存 `requested`、`resolved` 和 `fidelity`，不能只留下适配后数值。

控制点位置另由独立的 `time_policy` 约束：

- `exact`：bar/beat 换算后的时刻必须精确落在目标采样网格；
- `adapt`：授权吸附到最近 sample，trace 同时记录逻辑时刻、sample index 与最终秒数。

因此“控制值能否精确表示”和“控制发生时刻能否精确表示”不会被混成同一个决定。

每条 lane 还必须独立声明 `semantic_policy`：

- `exact`：能力必须原生实现该音乐控制的语义；
- `approximate`：创作者明确同意 capability 声明的语义近似，resolved plan 必须记录
  `semantic_fidelity=approximated` 及具体 `reason`。

这与 `value_policy` 正交：一个后端可能精确接受数值却只近似音乐含义，也可能原生支持
含义但需要量化数值。是否具备能力由 capability 回答；是否愿意接受适配或近似由这两个
authored policy 分别回答。

capability 还声明控制的作用方式：持续作用、note-on latch 或 release gate。后两者会在
解释后的逻辑音符边界取 lane 状态；若 humanize、gate 或采样吸附把物理事件移过了控制
点，conductor 只在该 note-on/note-off 前临时物化所需状态，并在同一 sample 的音符事件
后恢复当时的正常 lane 状态。这样既不会让本音错过控制，也不会把临时踏板状态泄漏给
后续音乐。若控制只适用于某些奏法，编译器会针对该 lane 实际路由到的每个最终奏法
逐一验证；混有不适用 one-shot/奏法的 part 必须失败，而不是输出听不见的假控制。

`voice` 当前定义为该 part 中所有 `note.voice` 完全匹配的音符，即使相同 voice 名称
出现在多个 MusicXML staff 也一并命中。v1 先保存并严格验证这种意图；当前执行能力
仍以 part-scope 为主，目标乐器 capability 不支持 voice-scope 时必须拒绝执行，不能
悄悄扩大成整条 part。

同一文档内 `lane_id` 必须唯一；同一 `(part_id, voice, control)` 只能有一条 lane，
避免两条曲线的优先级歧义。

## 绑定与 API

解析入口：

```python
parse_realization_document(
    data,
    score_document=raw_score_json,
    score=parsed_score,
    expected_score_sha256=canonical_score_sha256,
)
```

`score_document` 是建立绑定证据的参数：解析器在内部用项目 canonical JSON 规则计算
其 SHA-256、解析该原始文档，再验证 realization 的 `score_sha256`。哈希与解析都来自
同一次 canonical snapshot，调用方随后修改可变 dict 也不能造成 A 内容/B hash。`score` 是可选的
已解析副本；若同时提供，必须与内部解析结果完全相等。禁止只传 `score`，因为一个
具有重叠 event ID 的其他 revision 不能证明自己就是 realization 绑定的原文。

`expected_score_sha256` 也是可选的额外调用方断言；与 `score_document` 一起使用时，
它必须同时匹配内部计算结果。单独传 expected hash 只能证明两个字符串一致，不能证明
当前内存里的某个 `ScoreDocument` 来源于该 hash。只做结构工具时可以省略所有上下文
参数。完成绑定后解析器检查：

- score 必须为带稳定 event ID 的 v1；
- 每个 note override 必须引用真实事件；
- tie continuation 不能被独立 override，必须引用 tie-chain head；
- 每个 lane 必须引用有音符的真实 part；
- voice target 必须至少命中一个音符；
- 每个控制点必须落在该 score 拍号允许的 bar/beat 范围内。

绑定校验还会把每个控制点解析为有限的 logical quarter/seconds；无法转换的超大坐标会
以结构化 `ValueError` 阻断，而不会把 `OverflowError` 留到 conductor。realization 的
独立条目上限只负责输入防护；编译阶段还会把 score note 的 note-on/off/奏法展开、
control point 展开及跨 executor fan-out 合并计数，在物化事件前统一通过 Performance
event/resource budget，不能把各层上限简单相加后继续执行。

返回的 `RealizationDocument` 及所有子对象都是 frozen dataclass，数组均转换为 tuple；
公开构造器自身也复验 finite、bounds、枚举、点顺序、嵌套类型、重复 ID 和资源上限，
不能通过绕开 parser 构造伪“validated”对象。`to_dict()` 每次返回脱离内部状态的新 JSON
对象。`empty_realization(score_sha256)` 可生成规范的空 no-op 文档。

JSON Schema 位于 `schemas/realization.schema.json`。Schema 验证文档形状；事件、声部、
voice、拍号和 Hash 的跨文档约束由上述解析 API 负责。

资源上限与精度授权也是两件事：`exact` / `adapt` 只决定是否接受量化，
不会绕过运行预算。默认的最终 Performance Plan 规范 JSON 上限为 32 MiB，
总时长（包括 tail）上限为 7200 秒；构造过程会在保留放大后的事件和 trace
之前递增计费，完成后再对整份文档精确复核。可信的非 candidate 批处理可以
显式提高 `TIANLAI_MAX_PLAN_MIB` 或 `TIANLAI_MAX_PLAN_SECONDS`；candidate 发布仍保留
固定的 32 MiB 完整性边界，避免产生一份系统自己无法重新验证的候选。
