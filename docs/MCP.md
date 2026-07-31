# 天籁 MCP 接口

天籁 `0.5.0rc3` 通过 stdio MCP 把可编辑、可复算的音乐工程交给 AI Agent。Agent
拿到的不是一个不可解释的“一句话转音频”按钮，而是一组细粒度工具：读取合同、
选择乐器、导入谱面、明确配器、预检、渲染不可变候选、按秒定位、局部修改，再
渲染并 A/B。

天籁负责合同校验、乐器执行、分轨、合奏、Hash 绑定和客观诊断。旋律、结构、
配器意图、取舍和发布决定仍属于创作者。机器指标与语言模型都不能代替人耳。

## 安装与客户端配置

Windows 源码包用户先运行：

```cmd
安装运行环境.cmd
检查运行环境.cmd
```

安装脚本已经安装 MCP 可选依赖。手动创建开发环境时可执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
.\.venv\Scripts\python.exe -m tianlai.mcp_server
```

第二条命令启动 stdio 服务。客户端配置示例：

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_server"],
      "cwd": "C:\\path\\to\\tianlai"
    }
  }
}
```

仓库还提供可复制的 [`.mcp.json.example`](../.mcp.json.example)。复制为
`.mcp.json` 后，把两个占位路径改成自己的源码包目录即可；真实 `.mcp.json`
包含本机绝对路径，因此默认不进入 Git 或发布包。

Linux 或 WSL 内运行的 MCP 客户端请改用项目 `.venv/bin/python` 的 Linux
绝对路径；完整安装、配置示例与支持范围见
[Linux / WSL 快速开始](Linux快速开始.md)。不要把 Windows 与 Linux 的
`.venv` 混用。

`command` 必须指向项目自己的 `.venv`，`cwd` 指向天籁运行根目录。服务器不会把
音频流塞进 MCP 文本返回值；成功渲染返回本机候选目录、WAV、回执和报告路径。

## MCP 本机输入根

文件型导入工具不会获得整台电脑的任意读取权限。默认策略如下：

- 相对路径以天籁运行根目录为基准，而不是 MCP 客户端进程碰巧使用的当前目录；
- 默认允许天籁运行根及其中已有的 `乐谱/`、`examples/` 和输出目录；
- 输入必须已经存在并且是普通文件；
- 路径会先规范化再检查包含关系，`..` 或符号链接不能逃出允许根；
- Windows 可用分号分隔的 `TIANLAI_INPUT_ROOTS` 增加额外目录；它只扩展默认根，
  不会替换默认根。

扩展根不会加入相对路径搜索顺序。相对路径始终从天籁运行根解析；引用扩展目录中
的文件时应传绝对路径。

例如：

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_server"],
      "cwd": "C:\\path\\to\\tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "D:\\Scores;E:\\SharedMusicXML"
      }
    }
  }
}
```

只把确实准备交给 Agent 的谱面目录加入输入根。`score`、`roster`、补丁和查询等
结构化参数直接作为 MCP 对象传递，不需要为它们开放额外磁盘目录。

已渲染候选的定位和比较还有更窄的边界：只能读取当前运行实例的
`output/mcp/` 候选树，不能借候选参数查看任意输入根。

## 当前 15 个工具

服务实际注册下列 15 个工具：

| 工具 | 写音频/项目文件 | 作用 |
| --- | --- | --- |
| `score_and_roster_format` | 否 | 返回当前 score/roster 合同、规则和最小示例。 |
| `list_instruments` | 否 | 返回调色板、奏法、音域、音高模式、质量与许可状态。 |
| `import_midi` | 否 | 兼容入口：读取本机 MIDI，返回 score 和不可执行草稿。 |
| `import_musicxml` | 否 | 兼容入口：读取 MusicXML/XML/MXL，返回 score 与报告。 |
| `import_score_project` | 否 | 推荐入口：统一导入并返回 Hash 绑定的三文档工程包。 |
| `confirm_roster` | 否 | 用创作者逐声部指派把草稿提升为正式 roster。 |
| `upgrade_score` | 否 | 把 legacy score 升级为带稳定 `event_id` 的 score v1。 |
| `get_score_slice` | 否 | 按声部、事件或小节读取有界片段与基线 Hash。 |
| `patch_score` | 否 | 原子应用 Hash/旧值绑定的事件补丁，返回新 score。 |
| `compare_score_versions` | 否 | 按稳定事件身份比较两份 score。 |
| `validate_project` | 否 | 编译并检查 score、roster 和演奏设置，不实例化乐器。 |
| `locate` | 否 | 重新编译当前项目，将秒数窗口映射到计划事件。 |
| `locate_rendered_candidate` | 否 | 从已保存候选的回执和计划定位实际听到的秒数。 |
| `compare_rendered_candidates` | 否 | 比较两个候选绑定的 score、roster、配置、计划和混音身份。 |
| `render` | **是** | 渲染新的候选目录、合奏、可选分轨、回执和许可旁车。 |

表中的“否”表示不写音频或项目文件；导入和候选检查工具仍会读取已授权的本机
文件。`patch_score` 返回新的内存对象，保存位置由客户端决定。只有 `render`
创建正式候选工件。

## 推荐 Agent 闭环

CLI 文档使用下面的名称：

```text
project-import → roster-promote → project-render → candidate-locate
    → score-slice → score-patch → project-render → candidate-compare → A/B
```

MCP 中概念对应为：

```text
import_score_project
    ↓
confirm_roster
    ↓
render
    ↓
locate_rendered_candidate
    ↓
get_score_slice → patch_score
    ↓
render(parent_candidate_id=...)
    ↓
compare_rendered_candidates → 创作者听取 A/B
```

两条链不是同一个文件权限外壳：CLI 导入写三文件并提供 loss policy，MCP 导入
返回内存 bundle；CLI 候选默认位于 `output/候选/`，MCP 候选位于
`output/mcp/`；MCP `render` 每次默认执行 `trusted_only=true`，CLI 则主要在
`roster-promote` 阶段确认调色板。不要把一种入口的路径或权限默认值套给另一种。

### 1. 读取当前合同和调色板

每个新会话先调用：

```text
score_and_roster_format()
list_instruments(trusted_only=true, pitched_only=false)
```

不要依赖旧提示词记住的字段或乐器集合。`trusted_only=true` 是默认策展范围；
`false` 只放开未隔离的正式单音色入口，不会绕过许可隔离，也不会公开本机兼容
SoundFont。

可信调色板的唯一版本化数据源是 [`可信乐器.json`](../可信乐器.json)。文档和
Agent 提示词不应固化数量；每次会话都应通过 `list_instruments` 读取当前集合。

写音符前优先读取
`articulation_range_contracts[articulation].midi_ranges`。顶层音域只是整件乐器
的包络，可能包含奏法空洞。`strict_hq` 是失败关闭的证据闸门，不会自动改善
音源或重采样质量。

### 2. 统一导入

```text
import_score_project(
  source_path="乐谱/曲目/某曲/MusicXML/某曲.mxl",
  trusted_only=true,
  candidate_limit=8
)
```

成功结果的 `bundle` 包含：

- score v1；
- 可持久化的 `import_report`；
- `executable=false` 的 `roster_draft`；
- 与源文件和 score 绑定的 SHA-256；
- 每声部有限数量的非执行候选提示。

候选提示、轨道名称、Program Change、CC7、CC11 或轨道顺序都不会自动成为正式
路由。旧 `import_midi` 与 `import_musicxml` 为兼容调用保留；新工程优先使用统一
入口，避免 MIDI 与 MusicXML 获得不同的审计边界。

MCP 统一导入只返回内存 bundle，不替客户端执行 CLI 的 `loss-policy` 或写文件。
客户端应先检查 warnings/report，再决定拒绝本次结果还是把整套三文档持久化。
导入报告必须与 score 和 draft 一起保存并阅读。重复、倚音、踏板、弯音、歌词、
排版或厂商控制信息等
未进入 score 的语义，后续渲染无法凭空恢复。

### 3. 显式确认 roster

```text
confirm_roster(
  score=...,
  roster_draft=...,
  assignments=[
    {"part": "Piano", "instrument": "键盘乐器/钢琴"},
    {"part": "Violin", "instrument": "管弦乐/弦乐组/小提琴"}
  ],
  trusted_only=true
)
```

普通声部提交 `instrument`，打击声部提交逐键 `kit`。工具会先重验 draft 绑定的
score Hash，再要求 score 的每个 part 恰好出现一次，并检查乐器存在性、隔离与
trusted 策略。它不会用 routing hints 帮 Agent 补空缺。

角色、前后景、静态增益、自动化、座位、声部组和相对平衡关系也应由创作者明确
表达。乐器或轨道名称中的“主奏”“左手”不构成权限。

### 4. 预检与第一次渲染

在昂贵渲染前先调用：

```text
validate_project(
  score=...,
  roster=...,
  render_profile={
    "kind": "tianlai.render_profile",
    "schema_version": 1,
    "name": "preview-v1",
    "write_stems": true
  },
  trusted_only=true
)
```

它与正式渲染共享结构、时间坐标、许可、可信策略、路由、音域和演奏计划检查，
但不会打开 WAV/SFZ。`resources.level="catalog_only"` 与
`ready_to_render=null` 只说明本次没有检查实际音频资源。没有传
`render_profile` 时，预检和渲染都解析同一份版本化默认配置；但一次省略 profile
的预检，不能用来证明后来另一份自定义 profile 也会过门。

为避免 Agent 手工复制时漏掉厅堂、分轨或缓存参数，`validate_project` 会返回可
直接交给 `render` 的：

```json
{
  "render_handoff": {
    "render_profile": {"kind": "tianlai.render_profile", "...": "..."},
    "expected_render_profile_sha256": "64位小写SHA-256"
  }
}
```

正式渲染同时传入这两个字段；若 profile 被中途改写，`render` 会在创建候选目录
之前返回 `render_profile.preflight_mismatch`。这个 Hash 是本地工作流的防误用
握手，不替代候选清单或发布签名。

`render_preflight` 明确给出当前请求的 `passed`、共享厅堂尾音、分轨、有效协奏
模式、缓存开关、内存/主输出估算与逐项预算门禁。共享厅堂会显著增加全长工作
数组；`write_stems` 因逐轨渲染、逐轨写盘只增加磁盘输出估算，不会按轨数累加
峰值内存；`analyze/suggest` 的关系音频使用临时 memmap，FFT 按有界窗口批次
处理。预检失败时同一份报告仍随错误返回，不能把红灯当作可继续渲染。

然后创建候选：

```text
render(
  score=...,
  roster=...,
  title="某曲",
  render_profile=validation.render_handoff.render_profile,
  expected_render_profile_sha256=(
    validation.render_handoff.expected_render_profile_sha256
  ),
  trusted_only=true
)
```

未显式覆盖时使用版本化默认 render profile。成功结果返回 `candidate_id`、
`candidate_directory`、`mix_wav`、计划、回执、许可旁车、可选分轨、音域诊断、
混音报告和缓存遥测。

### 5. 从实际候选定位

听到 `34.2` 秒有问题时，优先使用：

```text
locate_rendered_candidate(
  candidate_directory="作品ID/候选ID",
  at_seconds=34.2
)
```

它会校验候选里的 score、roster、render profile、演奏计划和回执 Hash，再报告：

- 正在门控发声的事件；
- 近期结束、可能仍贡献采样 release 或厅堂尾音的事件；
- 即将进入的事件；
- `event_id`、part、乐器、音高、小节与拍位。

`locate` 则重新编译调用时提供的当前 score/roster，适合尚未渲染的计划检查。
已经听到某个保存候选时，不要用后来修改过的当前工程替它猜测。

尾音列表是有界候选，不是逐样本因果证明。最终仍要结合分轨与人耳。

### 6. 有冲突保护地编辑

先读取小片段：

```text
get_score_slice(
  score=...,
  query={
    "kind": "tianlai.score_slice_query",
    "schema_version": 1,
    "part_ids": ["Violin"],
    "bar_range": {"start": 12, "end": 14},
    "max_notes": 128
  }
)
```

再把返回的 `score_sha256` 写进补丁：

```text
patch_score(
  score=...,
  patch={
    "kind": "tianlai.score_patch",
    "schema_version": 1,
    "base_score_sha256": "...",
    "operations": [
      {
        "op": "update_note",
        "event_id": "violin-0042",
        "expect": {"pitch": "B5"},
        "changes": {"pitch": "A5"}
      }
    ]
  }
)
```

Hash 或 `expect` 不匹配时整批失败，不会只改一半。修改已有音符时保留
`event_id`；新增音符的 ID 由引擎确定性分配。客户端应把返回的新 score 另存为
新修订，不要覆盖候选中的 `score.json`。

### 7. 第二个候选和 A/B

用新 score 再次 `validate_project`，然后：

```text
render(
  score=新修订,
  roster=原编制或新编制,
  title="某曲",
  parent_candidate_id="candidate-第一个候选ID"
)
```

最后调用：

```text
compare_rendered_candidates(
  before_candidate_directory="作品ID/候选1",
  after_candidate_directory="作品ID/候选2"
)
```

比较工具区分 score、roster、render profile、演奏计划与回执身份。它说明“哪里
变了”，不判断哪一版更好听；创作者仍需直接 A/B 两个 `合奏.wav`。

## 候选不可变规则

MCP 渲染默认写入：

```text
output/mcp/<安全化作品 ID>/<唯一候选 ID>/
```

每个目录最后写入 `候选.json`，绑定：

- `score.json`；
- `roster.json`；
- `render-profile.json`；
- 演奏计划 Hash；
- `渲染回执.json` 及其 Hash。

候选一旦用于定位、比较或听审，就应当作不可变快照。不要就地编辑这些文件，也
不要把新音频复制进旧候选冒充同一代。正常迭代创建新 `candidate_id` 并设置
`parent_candidate_id`。

`render(overwrite=false)` 默认拒绝同名目录。受控覆盖必须同时给出明确
`output_id`、`overwrite=true` 和现有 `expected_receipt_sha256`；它是修复工具，
不是日常版本管理方式。

## score v1、`staff` 与 `voice`

每个音符必须有全谱唯一、稳定的 `event_id`。以下字段是可选谱内身份：

```json
{
  "event_id": "piano-rh-0042",
  "bar": 12,
  "beat": 1,
  "duration_beats": 2,
  "pitch": "C5",
  "tie": true,
  "staff": 1,
  "voice": "1"
}
```

- `staff` 是正整数谱表身份；
- `voice` 是非空字符串，其作用域由 `staff` 限定；
- 二者不等于 roster part，不影响乐器选择；
- 简单 score 可以省略；
- MusicXML 导入会保留它们，防止展平后把不同谱表/声部的相同音高连音错误合并。

对 MusicXML 派生 score 做 `patch_score` 时，除非有意改变谱内声部结构，否则应
保留已有 `staff/voice`。新增多声部连音时也应给出正确归属。

## 协奏分析与混音权限

roster 或 render profile 支持：

| 模式 | 诊断 | 不可执行建议 | 修改音频 | 写回项目 |
| --- | --- | --- | --- | --- |
| `manual` | 否 | 否 | 否 | 否 |
| `analyze` | 是 | 否 | 否 | 否 |
| `suggest` | 是 | 是 | 否 | 否 |

`suggest` 只针对 roster 明确声明的关系生成有界相对增益草稿，固定
`executable=false`、`audio_modified=false`、`creator_review_required`。Agent
必须先定位候选片段，再由创作者决定修改增益、音区、力度、时值、配器或什么都
不改。没有告警不等于作品已经通过完整协奏验收。

当前严格报告合同为 `mix_report.version=2`，其中
`temporal_balance.version=2`。遇到未知版本应停止自动解释并升级客户端。

## 分轨缓存

默认缓存的是 assignment 增益之前的本机 float32 原始分轨。缓存身份绑定声部
演奏内容、采样率、有效乐器参数、实际音源字节、DSP 源码和相关依赖。

只改 gain、自动化、pan、座位、厅堂、master、归一化、诊断模式或是否公开写分轨
时可以重混；修改音符、奏法、实际音源、乐器参数或 DSP 会让相关分轨失效。
在 `analyze` / `suggest` 下，同一开关还启用独立的内容寻址分析缓存：它绑定增益后
float32 内容、关系声明、分析参数和算法源码。座位、pan、厅堂或 master 变化可命中；
某轨 gain 或音频变化只让该轨指标及相关关系失效，关系声明变化只让关系指标失效。

- `use_stem_cache=false`：关闭缓存；
- `refresh_stem_cache=true`：强制重算原始分轨；分析缓存仍按新产出的实际音频内容
  重新核验，内容相同才会复用；
- `write_stems=false`：只是不写公开 PCM24 分轨，不等于关闭内部缓存。

缓存损坏时会安全回退到普通渲染。缓存主要加速第二次混音，不会消除首次冷渲染
的乐器执行成本。每次成功渲染还会写出 `缓存遥测.json`，其中
`total/accounted/unaccounted` 闭合记录命中、未命中与绕过；候选清单按 SHA-256
绑定该文件，单独改写遥测会被候选加载拒绝。`use_stem_cache=false` 同时关闭
这两层迭代缓存；需要跨机器证明候选未被整体替换时，仍应另外保存候选清单 Hash
或使用签名发布记录。

## 接口与质量边界

- MCP 只提供离线渲染，不是实时软音源；
- MIDI/MusicXML 导入不承诺保留源格式全部语义，必须阅读报告；
- `validate_project` 不证明外部采样已经安装；
- `validate_project.render_preflight` 只证明当前 render profile 通过静态资源
  预算；正式 `render` 会在接管候选输出前用同一路径再次门禁；
- 所有正式路由和项目修改都需要显式输入，不从名称或统计结果猜权限；
- 候选定位中的 release/厅堂贡献是候选证据，不是逐样本因果分析；
- 机器诊断不能判断旋律、和声、配器与审美；
- 首次长曲渲染可能需要数分钟和较高峰值内存，缓存只优化后续重混；
- `quality_tier=formal` 不代表全部音域、力度、奏法和乐器组合已经验收；
- 最终发布前仍需人工听审，并核对输入作品、音源许可、署名和输出权利。

CLI 的同一闭环见
[从乐谱到第二次渲染](从乐谱到第二次渲染.md)，实时实现状态见
[当前状态](当前状态.md)。
