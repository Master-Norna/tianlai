[**简体中文** | English](score-v2.en.md)

# Score v2：精确、可迁移的作品语义

Score v2 的目标不是发明一种更啰嗦的 MIDI，也不是把 MusicXML 的排版树原样搬进
执行器。它解决的是一个更基础的问题：把创作者真正想保留的音乐事实，和某一次演奏、
某一种音源、某一个交换格式能够表达的内容分开。

内部协作合同由四层组成：

1. **score**：作品是什么。音符、精确谱面时间、书写音高与实音、调律、关系和曲式；
2. **realization**：创作者要求怎样演奏，以及是否接受数值量化或语义近似；
3. **roster / capability**：当前执行器实际上能做到什么、分辨率多高、哪些只是近似；
4. **Performance Plan**：这一次最终采用的时间、数值、适配和证据。

“执行器能做”不等于“创作者愿意让它这样做”。因此 exact/adapt 和
exact/approximate 的授权属于 realization，不能由 capability 或导入器替创作者猜。

## 精确时间

Score v2 的逻辑时间使用规范化有理数：

```json
{"numerator": 1, "denominator": 7}
```

位置由稳定的 `measure_id` 和 `offset_quarters` 表示，时值使用
`duration_quarters`。分子、分母在求最大公约数之前就受限，分母必须为正；解析后符号、
约分形式唯一。七连音、嵌套比例和极短错位不会在进入指挥层前被六位小数或旧版浮点
容差合并。

时间线的第一项 meter 与 tempo 都必须覆盖首小节的零偏移；后续 meter 只能出现在小节线，
tempo 仍可在小节中途改变。中间小节的末端只能写成下一小节的零偏移，不能用两个坐标
表示同一时刻；整条时间线的最终端点仍可作为 phrase 的结束位置。逻辑 note 可以跨小节，
但不能越过总时间线。`actual_duration_quarters` 与拍号分开，因此弱起小节无需伪造拍号。

有理时间只描述作品时间。转换到秒和整数采样发生在执行边界，并记录 requested、
resolved、fidelity 以及创作者的 time policy。

## 音高与调律

一个音符同时保存：

- `written_pitch`：step、精确 alter、octave 和可选 accidental spelling；
- `sounding_pitch`：在作品调律中的精确实音；
- 顶层 tuning：稳定 `tuning_id`、受支持的调律系统和参考频率。

这样 B♭ 单簧管的书写 C、实音 B♭，以及 half-sharp 的拼写和实音可以同时存在。
导出 MIDI 时如果只能用整数音高或 pitch bend 近似，损失属于导出策略，不能反向改写
score。

## 稳定身份与关系

measure、meter、tempo、part、note、tie、phrase 等实体都有文档内稳定 ID。连音不是
音符上的一个含糊布尔值，而是显式引用两个事件的关系；它要求同一 part、同一精确实音、
时间严格连续且每端至多一条边。由于引用已经消除了旧格式的推断歧义，作者明确写出的
v2 tie 可以跨 staff 或 voice。Phrase 必须声明 `part_id`，不能靠全谱位置猜所属声部。
后续的 slur、tuplet、ornament、pedal、
glissando 和 repeat 也遵循同一原则：关系和实体分开，引用必须存在且唯一。

一项记谱事件未来可能展开成多次发音，因此执行计划会分别使用：

- `source_event_id`：作品中的记谱事件；
- `occurrence_id`：曲式展开后的某一次发生；
- `note_id`：某个 executor 内部的一次 note-on / note-off 配对。

在 occurrence 合同正式接通前，任何会产生一对多展开的曲式或装饰音都必须 fail closed，
不能解析后忽略。

## 两种哈希，两个用途

`canonical_json_sha256` 是精确的**文档修订身份**：它忽略空白和对象键顺序，但仍区分
`1` 与 `1.0`、省略默认值与显式默认值、v1 与 v2。Realization、候选、原子 patch 和
创作修订继续绑定这一身份，不能用“看起来等价”绕过旧版本检查。

Score v2 另提供带域、带版本的 render projection hash。它只证明两个文档对某一版执行
语义的投影一致，可用于迁移收据、缓存或诊断；它不宣称两份谱面的全部记谱或排版语义
相同。

## 扩展与未知语义

扩展使用命名空间、版本、影响类别、required 标记和 payload。未知但明确为 optional 且
inaudible 的扩展可以原样往返；未知 required 或 audible 扩展必须阻断。这样系统可以
承认“我还不会执行”，而不是把无法理解的数据静默当作不存在。

单项上限不能替代整份文档预算。实现还会累计限制 note、articulation、meter group、
relation，以及所有 extension payload 的节点数、UTF-8 字节数和规范 JSON 字节数；小节
时值的公共分母与累计位置也有 bit-length 预算，避免大量互素分母把一个看似不大的 JSON
放大成巨型整数。公共 Schema 负责可表达的结构边界，跨数组累计、引用和精确时间关系由
语义解析器继续 fail closed。

Typed `to_dict()` 输出是规范表示：显式的空可选数组会归一化为省略。原始文档中“写了
空数组”与“没有这个字段”的修订差异仍由 `ScoreSourceSnapshot` 的 source document hash
保留，不会被 typed 规范化冒充成同一作者修订。

## v1 兼容与迁移

Score v1 仍走原有 float bar/beat 合同；它的既有 Performance Plan canonical hash 由固定
黄金向量保护。v2 不能通过修改旧解析分支来偷偷改变 v1 的和弦、连音、人性化或计划
字节。

已实现的 v1 → v2 API 产生一项显式、不可分割的 bundle，并保留 part/event ID。bundle
包含四项：v2 `score`；从 score 移出的 `sample_rate` 与 `tail_seconds` 所组成的
`render_settings`；与新 score Hash 绑定、当前只保存逐音 velocity 的
`performance_facts`；以及 `receipt`。收据绑定源文档 Hash、目标文档 Hash、目标 render
projection Hash、render settings Hash 和 performance facts Hash，bundle 每次序列化前
都会复验全部交叉引用；收据自身另有带域 Hash。

v1 没有 provenance 能可靠区分谱面 timing 与导入的演奏 timing，所以迁移不会擅自把
bar/beat/duration 搬进 realization：这些值仍作为 score 坐标保留。逐音 velocity 才会
分离为 performance fact。数值音高缺少书写拼法时采用确定性的升号倾向拼法，v1 拍号
分子映射成单一 meter group，被省略的 v1 默认值则显式物化；三类决定都以结构化 issue
写入收据，而不是伪装成原作者输入。

数值转换以 v1 **已解析值**的十进制文本为准，并精确转为有理数，不调用有损的
`limit_denominator`。如果约分后的精确表示超过 `1,000,000` 的分母上限，或超出 JSON
安全整数边界，迁移会带位置和错误码 fail closed，不会静默近似。这个迁移 API 当前不
接收或重绑 realization；为新 score 生成 realization 仍需后续独立、可验证的迁移合同。

命令行入口会读取显式 score v1，原子写出包含 score、render settings、
performance facts 和 receipt 的完整 bundle：

```console
tianlai migrate-score-v2 --score 乐谱/作品.score.json --output output/作品.score-v2-migration.json
```

该命令不会渲染 v2，也不会就地覆盖源 score。Score 文档 Schema 位于
`schemas/score-v2.schema.json`，迁移 bundle Schema 位于
`schemas/score-v2-migration.schema.json`；后者的外部 `$ref` 与前者一同随正式
源码包发布。

当前首批实现只开放能够完整验证和往返的线性核心。MusicXML/MIDI 导入对尚未进入 v2
映射的 tuplet、踏板、呼吸、孤立 tie、曲式等先产生明确损失；默认 reject 不再把它们
误报成无损。

当前 v2 已具备独立 typed model、Draft 2020-12 Schema、可信 source snapshot、版本化
render projection，以及隔离的 exact-time 编译器。时间编译器只接受可信 snapshot，以精确
有理数积分 tempo，并按 `nearest-ties-to-even` 生成 requested/resolved 采样证据；支持的
采样率为 8,000–384,000 Hz。它是受 32 MiB 默认输出预算约束的时间基础，不是可渲染
PerformancePlan，也没有回接旧的 float conductor。这里有意不提供“先压成 float 再说”
的快捷桥接：正式入口只在奏法、力度、关系、作者同意、能力与运行时代全部闭合后才把
受限子集编译为 performance transport，不会让新格式表面精确、执行时又静默退回旧容差。

在时间层之上，`score_v2_plan` 已提供第一版封存计划基础。它要求显式的
`sample_time_policy`（`exact` 或 `adapt`）和有理数力度映射；可把语义一致的显式 tie 链
折叠为一个 occurrence，并同时保留原事件、关系、书写音高、实音和采样适配证据。当前
合同仍标记为 `not-render-authority`：phrase、扩展、多奏法或未解析的力度会 fail closed，
计划也尚未绑定 roster、乐器能力和语义近似授权，因此不能直接交给 renderer。

## 执行同意配置：把“能不能”与“愿不愿意”分开

`score_v2_execution_profile` 是创作者的显式授权文档，不是乐器能力声明，也不是可直接
渲染的计划。它分别记录采样时间适配、逐音力度、音高、音域和奏法的数值策略与语义
策略；作品级调律另有独立的数值与语义许可。`exact` 表示不接受变化，`adapt` 只授权可追踪的数值适配，`approximate` 只授权
已说明原因的语义近似。三者不能相互替代。

力度表使用规范有理数；调律、逐音力度、音高和奏法必须同时通过执行器实际能力与这份同意
配置。换句话说，capability 回答“后端能做到什么”，execution profile 回答“作者愿意
接受什么”，后续适配器只能取两者交集，并把 requested/resolved/fidelity 证据写进计划。
本版 phrase policy 仅允许 `reject`，避免在尚无版本化塑形算法时偷偷添加演绎。

该文档的 Schema 位于 `schemas/score-v2-execution-profile.schema.json`。它有独立的规范
JSON Hash 和资源上限，但当前仍只是一项同意合同；在 roster、manifest generation、
音高/奏法能力与运行时指纹全部绑定之前，它不会把 `score_v2_plan` 升级为 render
authority。

`score_v2_capability_source` 进一步冻结 roster 所选中的原始 manifest 字节、文件身份、
规范 Hash、能力投影，以及逐 executor 应用 overrides 后的有效 manifest Hash。自定义
implementation 会被明确标成不可执行；当前 runtime sample fingerprint 仍是
`not_captured`。因此这层能证明普通换代竞态下的“同一批能力事实”，但不会把尚未捕获
的运行时资源冒充为证据。

`score_v2_capability_adapter` 随后只取 score/plan、execution profile 与上述能力事实的
交集。首版安全子集要求一声部一 executor，拒绝 kit、transpose、duration scaling、
dynamic compression、自动奏法、gain automation 和 runtime overrides；每个 occurrence
都保留调律、音高、力度、音域和奏法的 requested/resolved/fidelity 证据。它仍标记为
`score-v2-capability-adapter-v1-not-render-authority`：尚未绑定 runtime fingerprint，
也尚未生成 renderer performance events。

`score_v2_runtime_source` 再按 executor 绑定有效 manifest、旧运行指纹、Python 渲染闭包、
运行依赖与聚合资产图。现有旧接口只能顺序重算这些来源，不能把多个执行器的文件冻结成
同一时刻的原子代；逐资产描述符、lazy asset 的固定代、onset evidence 与工厂实例也尚未
捕获。因此该产物明确是 `not-render-authority`，只能证明“本次观察到并再次核对的来源”，
不能代替渲染事务内的最终复验。

`score_v2_performance` 把已经授权并解析后的音高、力度和奏法写入旧 performance 协议，
同时为每条事件保存 occurrence、role、sequence 与权威整数采样点。规范 JSON 回读后会再次
经过旧 parser，并逐事件证明浮点秒仍还原到同一个采样点；同一采样内仍按精确作品时间
排序，同一精确时刻先 note-off，再按 occurrence 紧邻发送 articulation 与 note-on。位于
`frame_count` 的终点事件会保留，但合同标记为 `pending_v2_renderer`：正确执行需要在恰好
生成 N 帧后派发这些事件，不能偷偷多渲染一帧。首版 `tail_seconds=0`，所以这也不代表
自然释放尾音已被保留。该 bundle 仍是可验证运输证据，不是可发布音频的渲染权威。
首版也不会把起止端点舍入到同一采样的单音解释成“零采样脉冲”；这种音符在 plan 层以
`plan.zero_sample_duration` 明确拒绝，等待未来另行定义可听语义。

## 首版正式渲染：运行时代权威与 Candidate v3

`project-render-v2` 是 direct Score-v2 的第一条正式 CLI，不会先把 v2 降成 v1，也不会
隐式拆开 v1 → v2 迁移 bundle。它要求调用方显式提供 score、正式 roster、作者的
execution profile 与采样率：

```console
tianlai project-render-v2 \
  --score 乐谱/作品.score-v2.json \
  --roster 乐谱/作品.roster.json \
  --execution-profile 乐谱/作品.execution-profile.json \
  --sample-rate 48000
```

三个 JSON 输入分别通过描述符读取并保存文件身份与 Hash，在编译、渲染和发布边界重复
复验；采样率则作为显式数值绑定到整条链。合同不把三个 JSON 的顺序捕获宣称成跨文件
原子快照，也不宣称抵抗恶意 ABA。首版还要求
**源码工作区布局**：乐器目录的父目录就是项目根，其中的 `tianlai/` 必须是当前进程实际
加载的包；`--root` 可以显式选择该工作区内的乐器目录，但不能借此混用另一套代码。

固定编译链把 score、roster、execution profile、`score_v2_plan`、capability source/plan、
runtime source 与 performance bundle 逐层绑定。随后才创建不可转移、单次消费的活跃
runtime lease。lease 固定原始 manifest、effective manifest、工厂代、已加载 Python 模块
到受持有源文件描述符的映射，并在事务内持续 checkpoint；Candidate 元数据仍在 lease
上下文内生成，退出上下文时执行最后一次完整源码复验。任何 checkpoint 失败都会阻止候选
目录变成可见代。保存下来的 acquisition/consumption JSON 只是历史证据，不能在别的进程
或下一次渲染中重新取得运行时权威。

渲染器消费同一 lease 和 performance transport，恰好写出 `frame_count == N` 帧，再按
sidecar 顺序派发 `sample == N` 的终点事件；不会为了释放音而偷偷多生成一帧。float64
stereo 流在身份绑定的私有描述符上增量量化为 stereo PCM24 WAV，经 sealed、no-replace
安装成为 `合奏.wav`。回执同时绑定 float 流 Hash、PCM24 文件 Hash/字节数、由描述符捕获
的 WAV header、runtime manifest、lease acquisition/consumption、performance bundle 与
`渲染后自检.json`；因此“曾经算过这些样本”不能替代“候选中就是这些 PCM24 字节”。

正式产物使用 `候选.json` 的 **Candidate v3**（`version: 3`）。它以固定文件集合封闭 direct
v2 score、roster、execution profile、全部编译证据、运行时代证据、PCM24、渲染后自检与
正式回执，并沿用候选发布事务的 closed-world 核验和最终目录交换。`candidate-verify` 能按
版本核验该代；成功只证明本地描述符所见字节与绑定自洽，不证明作者身份、来源，也不保证
命令返回后现场目录绝对不可变。

这个 formal slice 故意只承诺一个完整而窄的集合：单 part / 单 executor、内置 oscillator、
manifest 与运行资产图都明确声明 **零外部音频资产**、stereo PCM24、无 stem/space/
normalization，以及 `tail=0`。migration wrapper、`render_settings`、performance facts、
realization、sampled backend、自定义 factory、lazy/external asset、kit 与多 executor 仍会
fail closed；自然释放尾音也尚未承诺。原有 `project-render`、score v1 计划与 Candidate
v1/v2 验证路径保持原行为，不会自动切换到这条 v2 管线。
