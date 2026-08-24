**简体中文** | [English](MCP.en.md)

# 天籁 MCP 接口

本文描述天籁 `1.0.0` 的 stdio MCP。它把可编辑、可复算的音乐工程交给 AI Agent。
Agent 拿到的不是一个
不可解释的“一句话转音频”按钮，而是一组细粒度工具：读取合同、
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
.\.venv\Scripts\python.exe -m tianlai.mcp_entry
```

第二条命令启动 stdio 服务。客户端配置示例：

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_entry"],
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

macOS 内运行的 MCP 客户端同样使用项目 `.venv/bin/python` 的真实绝对路径；
Apple Silicon 与 Intel 的解释器必须匹配当前原生宿主。安装与配置示例见
[macOS 快速开始](macOS快速开始.md)。虚拟环境不得跨操作系统或 CPU 架构复用。

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
      "args": ["-m", "tianlai.mcp_entry"],
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
`output/mcp/` 候选树，不能借候选参数查看任意输入根。新的普通候选位于
`output/mcp/<安全化曲名（无身份 Hash）>/<candidate_id>/`；清单内绑定 Hash 的
`work_id` 仍是身份，通常不等于这个父目录名。

v0.7 的持久创作工程使用更窄的独立边界。客户端只传小写 ASCII
`project_key`；服务端把它映射到固定的
`output/mcp/authoring-projects/<project_key>/`。接口不接受工程绝对路径、`..`、
盘符、斜杠或链接目录，也不在结果中返回本机路径。候选检查同样只接受服务端已经
返回的 `work_id` 与 `candidate_id`，并重新校验候选及其工程绑定。

## 当前 50 个工具

服务实际注册下列 50 个工具；原有 27 个工具的名称和参数保持兼容：

| 工具 | 写音频/项目文件 | 作用 |
| --- | --- | --- |
| `score_and_roster_format` | 否 | 返回当前 score/roster 合同、规则和最小示例。 |
| `list_instruments` | 否 | 分范围检索正式调色板，返回路由分类、奏法、音域与音高模式。 |
| `diagnose_runtime` | 否 | 返回有界、脱敏的运行时、平台、目录、资源汇总与可选能力自检。 |
| `plan_resource_restore` | 否 | 按乐器、资源族或组生成许可/体积/本机状态恢复计划，不下载或安装。 |
| `import_midi` | 否 | 兼容入口：读取本机 MIDI，返回 score 和不可执行草稿。 |
| `import_musicxml` | 否 | 兼容入口：读取 MusicXML/XML/MXL，返回 score 与报告。 |
| `import_score_project` | 否 | 推荐入口：统一导入并返回 Hash 绑定的三文档工程包。 |
| `confirm_roster` | 否 | 用创作者逐声部指派把草稿提升为正式 roster。 |
| `upgrade_score` | 否 | 把 legacy score 升级为带稳定 `event_id` 的 score v1。 |
| `get_score_slice` | 否 | 按声部、事件或小节读取有界片段与基线 Hash。 |
| `patch_score` | 否 | 原子应用 Hash/旧值绑定的事件补丁，返回新 score。 |
| `compare_score_versions` | 否 | 按稳定事件身份比较两份 score。 |
| `validate_project` | 否 | 编译并检查 score、roster 和演奏设置，不实例化乐器。 |
| `check_project_readiness` | 否 | 检查项目合同及 roster 实际引用乐器的资源引用，不解码或试播音频。 |
| `locate` | 否 | 重新编译当前项目，将秒数窗口映射到计划事件。 |
| `locate_rendered_candidate` | 否 | 从已保存候选的回执和计划定位实际听到的秒数。 |
| `compare_rendered_candidates` | 否 | 比较两个候选绑定的 score、roster、配置、计划和混音身份。 |
| `render` | **是** | 渲染新的候选目录、合奏、可选分轨、回执和许可旁车。 |
| `create_authoring_project` | **是** | 在专用命名空间创建空白、乐器中立的持久创作工程。 |
| `open_authoring_project` | 否 | 读取当前或指定不可变修订的轻量身份与文档 Hash。 |
| `get_authoring_snapshot` | 否 | 读取指定修订的三文档快照和有界 readiness；不返回路径。 |
| `save_authoring_project` | **是** | 用 `expected_revision` 做 CAS 保存；冲突不覆盖，旧修订不变。 |
| `check_authoring_readiness` | 否 | 检查指定修订的硬合同和建议性证据；`review` 不会被升级为阻断。 |
| `render_authoring_revision` | **是** | 只渲染调用方明确给出的不可变修订，不跟随可能变化的当前指针。 |
| `inspect_authoring_candidate` | 否 | 按工程和候选 ID 验证身份链，返回 workflow 管理状态、脱敏回执、自检及可选协作证据。 |
| `locate_authoring_candidate` | 否 | 按工程和候选 ID 将渲染秒数映射回事件，不接受或返回路径。 |
| `compare_authoring_candidates` | 否 | 在同一工程内比较两个已验证候选的谱面、配置、计划与混音身份。 |
| `creative_workflow_guide` | 否 | 返回模式、诚信边界、work charter 模板、既有复核阶段、证据和决策合同，以及 `orchestration_performance` 内不新增 phase 的轻量成境 / Qiyun 位置指引；不注入宪法全文。 |
| `get_music_constitution_clauses` | 否 | 校验本地官方宪法完整 Hash 后，按 ID 返回至多 12 条中英文条文；未知 ID 拒绝。 |
| `create_creative_workflow` | **是** | 创建 `off`、`audit` 或 `iterate` 工作流；`composition_governance` 默认 `true`、可显式关闭；此 stdio 边界把终审身份真实冻结为 `agent`。 |
| `open_creative_workflow` | 否 | 验证并打开当前或指定不可变 workflow 修订，不做隐式全历史遍历。 |
| `verify_creative_workflow_history` | 否 | 显式、有界地验证从当前指针回到创世修订的完整父链。 |
| `activate_creative_workflow` | **是** | 冻结单曲 work charter，以及可选的、少量激活的官方或自定义宪法条款。 |
| `inspect_workflow_composition` | 否 | 返回当前有效宪章的主张索引；可校验草拟的作品展开图并生成只读全曲事实、依赖缺口与问题，不评分或改谱。 |
| `record_workflow_composition_map` | **是** | 在本轮其他工作之前冻结唯一一份、只描述当前作品的展开图，并绑定本轮完整 score 与有效宪章。 |
| `preflight_workflow_charter_amendment` | 否 | 在复核期，或 revise 后但尚未改谱时，只读计算拟修宪的精确影响与重构成本，不激活修改。 |
| `commit_workflow_charter_amendment` | **是** | 在 revise 后、保存替代乐谱前，凭精确预检 Hash 与成本确认向线性修宪账本追加一项。 |
| `record_workflow_review` | **是** | 记录 `agent` 的阶段复核；受治理的三相必须回答本轮全曲问题，且不能借 MCP 自称 creator、listener、engine 或 validator。 |
| `record_workflow_evidence` | **是** | 记录非阻断的 promise conflict 或 aesthetic risk；不会自动改谱或改音频。 |
| `record_verified_workflow_hard_failure` | **是** | 由服务端重跑可信 readiness，只把精确复现的阻断 issue 写成 hard failure。 |
| `register_workflow_exception` | **是** | 登记有证据、有代价和恢复方式的例外；hard failure 永不可赦免。 |
| `record_workflow_derivation` | **是** | 记录稀缺的段落级必然性推导：事件或 end-exclusive 小节/拍号范围构成锚点，声部仅过滤；既有材料必须早于目标，备选必须用 `premise_indexes` 指回前提；非阻断、不改谱。 |
| `record_workflow_fork` | **是** | 稀疏声明同一作品的整曲备选：分支必须是已记录的完整候选且包含当前候选；每次读取重算 ID，并复验谱面锚点与候选绑定；不排序、不阻断，也不是 epoch/LCA 谱系。 |
| `render_workflow_candidate` | **是** | 原子衔接“预留 → 真实受管渲染 → 候选复验 → workflow 记录”，不接收路径或自述授权。 |
| `attach_workflow_candidate_for_audit` | **是** | 按候选 ID 把既有候选接入 audit；它保持 unmanaged，不能冒充受管完成态。 |
| `decide_workflow_iteration` | **是** | 在冻结的 agent 权限下 accept/revise/recommend/preserve/stop；以复核、证据处置、推导和宪章清偿固定裁决依据。revise 还会预先冻结 `revision_scope` 与 `withdrawal_condition`，下一轮通过 `prior_revision_assessment` 显式决定 promote、保留基线或未决；范围通过不代表审美更优。 |
| `record_workflow_authoring_revision` | **是** | 把另行 CAS 保存的新 authoring 修订绑定为下一轮，不替 Agent 暗改乐谱。 |
| `rollback_creative_workflow` | **是** | 选择较早的不可变候选锚点；可同时以 `prior_revision_assessment=retain_baseline/inconclusive` 结清挑战者，不覆盖工程修订，也不删除后来候选。 |
| `cancel_workflow_render` | **是** | 取消唯一当前预留，不删除任何已经发布的候选。 |
| `stop_creative_workflow` | **是** | 按冻结的 agent 权限停止流程；不会伪造创作者批准。 |

表中的“否”表示不写音频或项目文件；导入和候选检查工具仍会读取已授权的本机
文件。`diagnose_runtime` 和 `check_project_readiness` 严格被动：不加载外部原生库，
不启动 `tar`、`bsdtar` 或任何外部程序，也不创建临时文件。macOS x86_64 唯一额外
执行的是当前进程内只读 `sysctlbyname` 身份查询；它不启动进程、不写盘、不联网。实际 MCP 输出目标
`output/mcp` 的可写性仅是
依据目录或父目录元数据与权限做出的无写入估计，不是实际写入验证。
`plan_resource_restore` 不联网、不下载、不解压、不安装，也不产生持久写入。
`patch_score` 返回新的内存对象，保存位置由客户端决定。authoring 与 workflow 的
写工具只在专用工程根内发布不可变修订或候选；`render`、
`render_authoring_revision` 和 `render_workflow_candidate` 会写音频。
workflow 的每次状态变化同样是 CAS 修订，不会覆盖旧状态。

authoring 工具统一返回 `tianlai.authoring_mcp_result`。语义失败仍是正常的结构化
MCP 结果，`ok=false`，并带稳定的 `error.code`、`message_key`、`stage`、
`retryable` 和无路径的逻辑位置。CAS 冲突代码为
`authoring_project.revision_conflict`；客户端应重新读取快照、合并，再用新的
`expected_revision` 保存，而不是无条件重试旧请求。

workflow 工具统一返回 `tianlai.creative_workflow_mcp_result`。成功结果带完整、无路径的
当前快照与 `next_action`；语义失败也保持正常 MCP 结果，并建议先
`open_creative_workflow` 刷新状态。`workflow_revision_conflict` 可重试，但必须先读取
新修订并重新判断，不能把旧请求盲重放。

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
validate_project → check_project_readiness
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

v0.7 的持久 authoring 闭环不再要求客户端自己保管三份游离文档：

```text
create_authoring_project
    ↓
get_authoring_snapshot → 编辑完整 documents
    ↓
save_authoring_project(expected_revision=快照修订)
    ↓
check_authoring_readiness(revision=保存结果修订)
    ↓
render_authoring_revision(expected_revision=同一修订)
    ↓
inspect_authoring_candidate → locate_authoring_candidate
    ↓
读取指定修订 → 修改 → CAS 保存 → 渲染新候选
    ↓
compare_authoring_candidates → 创作者听取 A/B
```

`render_authoring_revision` 只返回工程、修订、作品和候选 ID。后续工具通过这些 ID
在固定工程目录内重新解析并校验候选，所以 Agent 可以完成闭环，而无需获得磁盘路径。
候选检查报告是证据，不是审美裁决；技术阻断与 `project_review` 的建议性
`review` 决定继续分离。`inspect_authoring_candidate` 还明确返回
`workflow_authorized`、`workflow_recorded`、`workflow_accepted` 三个不同事实。
`workflow_managed` 只兼容性表示历史预留确实有效；只有 shape 合法的自述
`authoring_workflow` 不构成权限，普通 authoring 渲染也不会被伪装成上层完成态。

> 升级兼容提示：用 `1.0.0` 只读打开 `0.9.x` authoring project 不会改写磁盘；
> 仅打开或保存完全相同的文档也不会触发迁移。第一次实际内容保存会为
> `tianlai-project.json` 增加 `save_sequence` / `current_save_event_sha256`，
> 为新 `revision.json` 增加 `first_save_sequence` / `parent_revision`，并建立
> `.tianlai/save-events/`。这是单向的因果记录升级：`1.0.0` 能读取旧工程，
> 但 `0.9.x` 无法再打开保存后的工程。如需保留降级能力，请在首次修改保存前
> 复制完整工程目录。

可选的 v0.7 治理闭环位于 authoring 之上：

```text
creative_workflow_guide
    → 可选 get_music_constitution_clauses（至多 12 条）
    ↓
create_creative_workflow(mode=off|audit|iterate, composition_governance=true)
    ↓
activate_creative_workflow(work_charter, 可选 constitution + active_clauses)
    ↓
inspect_workflow_composition（取得有效宪章 claim IDs）
    → 草拟当前作品的展开图
    → inspect_workflow_composition（只读全曲镜子）
    → record_workflow_composition_map（本轮其他工作之前）
    ↓
record_workflow_review(intent → symbolic_structure → orchestration_performance；
                       逐题回答本轮完整问题集，配器复核内含轻量成境位置审视)
    ↓
record_workflow_evidence / record_verified_workflow_hard_failure
    ↓
render_workflow_candidate（内部预留、渲染、复验并记录）
    ↓
record_workflow_review(render_report；真实听过才可 audio_audition)
    ↓
record_workflow_derivation（可选；仅关键转移）/ register_workflow_exception（可选）
    ↓
record_workflow_fork（可选；已有多个完整候选且当前候选属于 branches 时）
    ↓
decide_workflow_iteration（最新策略显式提交 charter_settlement；accept 全覆盖）
    ├─ accept / preserve / stop
    └─ revise
         ├─ 不修宪：CAS save_authoring_project
         └─ 需要修宪：在 reviewing 期，或 revise 后但尚未改谱时完成预检：
              commit_workflow_charter_amendment（回显精确 Hash 与成本；必须先于改谱）
                  → CAS save_authoring_project
         → record_workflow_authoring_revision
         → 下一轮重建展开图、重做全曲镜子与三相复核
```

作品展开图不是固定曲式模板，而是先把**这一首作品**的完整序列因果关系定构出来：
每个稳定节点说明其作用、依赖的宪章主张、沿用或变化的既有材料、角色变化、稀缺资源、
结尾回应与仍待回答的问题。输入只来自当前有效宪章和当前 score；历史作品、偏好案例、
获奖理由与其他作品的片段都不进入生成依据。`inspect_workflow_composition` 只把全谱事实、
位置、依赖覆盖与缺口变成可追问记录；它不套用固定曲式、不评分、不自动修改，也没有
试听音频。

在受治理的 `intent`、`symbolic_structure` 与 `orchestration_performance` 三相中，复核不再是
勾选“已看过”：调用方必须回答镜子针对本轮 score、有效宪章和展开图生成的完整问题集，
并引用真实的 claim、节点或事件。关键转移的推导还必须绑定这些宪章主张、展开图节点和
已回答的问题；这样“先推导、再写作”成为可复验的过程，而不是只依赖模型自觉。

成境 / Qiyun 不增加 review phase，而是在 `orchestration_performance` 中审视完整候选的
具体位置：哪里虽已正确存在，却还缺少流动、层次、呼吸、距离、余响或周边生命。结果
可以是零新增；不得把它执行成固定周期的点缀清单，也不要求为每个微小伴生细节补一条
推导。删除、静音或前后完整候选比较可以帮助发现损失，但没有实际听审时只能记录
`aesthetic_risk` 或待验证假设，不能声称差异已经成立。若修改触及身份核、主要和声因果、
段落职能、高潮依据、结尾回应或宪章主张，就退出这次微观审视，走正式修订；需要改变
宪章时沿用现有修宪预检，不新增阶段、账本或 Schema。审视结论折入既有配器复核回答，
不增加可打分的“成境题”；机器只能确认提示已给出和引用合法，不能证明模型真的想明白。

如果迭代意见确实要求改变宪章，必须在 `reviewing` 期，或作出 `revise` 决定后但尚未
改谱时，先做只读预检。预检逐项列出受影响
主张、展开图依赖、推导、复核与证据解释，以及最小重构范围；范围越大，明确成本越高。
只有先作出 `revise` 决定，再逐字段回显同一预检 Hash 和精确成本，修宪才会追加到单线、
只增不改的账本，并从下一轮生效。扩大范围必须重新预检，且提交必须早于任何替代 score
保存，避免先大规模重盖、再用新宪章追认。它不是第二套父版本树；新一轮也不会继承旧
展开图或旧问题答案，必须按新 score 与有效宪章重新定构并复核全曲。非 hard 依据必须
命中受影响主张或集合根域；预检的最小输入快照进入 Hash，提交与历史重开会从 workflow
持久记录重算成本。内部线性保存事件只证明“改谱发生在确认代价之后”，不成为新的产品
版本树。

这些机器合同只能证明材料绑定、问题覆盖和账本一致，不能证明作品好听，也不能冒充
人类听审。正式候选始终是一首完整作品；事件、小节和秒窗只用于全曲中的定位、证据和
推导，不作为片段成品写入候选目录。

`next_action` 只是在当前已验证状态上的程序导航，不是审美判断。`iterate` 不会自行
生成乐谱修改：Agent 仍须读取 authoring 快照、提出明确改动并用 CAS 保存；workflow
只记录为什么改、依据是什么、付出了什么以及新修订是哪一个。`rollback` 也是选择，
不是覆盖。

宪法条款不是全曲提示词；v0.2 用它们在关键位置提出问题，而不是直接生成答案，也不
要求尚在萌发或仅改变周边气息的每个细节先给出理由。推导同样是稀缺论证，不是配额：
每轮默认上限为 8，设为 0 可关闭，
无需为了用完预算逐小节制造理由。其锚点必须是非空 `event_ids` 和/或完整的
`[start_bar:start_beat, end_bar:end_beat)` 半开范围；`part_ids` 只能过滤，不能独立
锚定；候选秒窗也只能补充渲染定位，不能替代事件或谱面范围锚点。
`established_material` 必须严格早于目标，每条备选的非空 `premise_indexes` 必须指回
本推导前提。承诺兑现、身份稳定且实质备选关闭后，应当 accept、preserve 或 stop，
不得为了迭代而迭代。

fork 也不是每轮必做的关卡。只有确有多个完整候选需要并存时才记录；它是稀疏的
whole-work alternative declaration，不是 epoch/LCA lineage，不能据此推断祖先、合并点
或因果演化。每次读取都会由正文重算 fork ID，复验锚定 score 的规范化 Hash、event / part /
半开 range 的存在与对应关系，以及 branches 确实引用此前已记录候选；当前迭代候选必须
在 branches 中。没有真实多解时，直接进入裁决即可。

新决策不能只列 `evidence_ids` 后把其余主张留在日志里不管。`review_ids` 固定裁决采用的
准确复核；accept 的所选集合必须覆盖四相，试听裁决还必须选中同一 agent 的
`audio_audition`。`evidence_dispositions` 必须恰好覆盖本轮全部非 hard-failure 证据：
accept 只能关闭为 `resolved` / `accepted_risk` / `excepted`，promise conflict 不可直接
标成 accepted risk；revise / recommend 必须至少有一个 `revision_target`，其中 `revise`
还必须填写 `expected_audible_change`。每个 disposition 的 `evidence_id` 必须同时列入顶层
`evidence_ids`；`basis_ids` 所引 review、evidence、exception 或 derivation ID 必须分别
列入对应的顶层选择。preserve、stop 以及独立终止可以把未决项如实冻结到
`open_evidence_ids`，不会伪造“已经解决”。记录 ID、谱面事件/声部指涉和例外目标会在
读取时重验；对 `resolved`，机器只检查引用已选择、禁止自指且依赖无环，不证明解决理由
在音乐或审美上成立。这仍是审计合同，不是自动审美评分器。

新策略还要求 revise 在编辑前提交 `revision_scope` 和 `withdrawal_condition`。有限范围用
`allowed_document_paths` 必须与 `documents` 使用完全相同的键，并对每份文档声明至多
1024 条精确 RFC 6901 叶子路径；每条路径至多 1024 个字符且 UTF-8 编码至多 1024 字节，
不授予前缀/通配权限。score 音符只能通过稳定 `event_ids` 修改，有限范围不能重排；
`bar_ranges` 还要求 `end >= start`。范围校验覆盖合同 causal fence 之后直到绑定目标的每个
持久化 save state；中间一度越权，即使最终 documents 恢复，也会拒绝，不能“先重盖再回填”。
`record_workflow_authoring_revision` 同时把目标 documents 与基线做实际差异核对；整曲重构必须显式
确认扩大改动面、下游兼容重做和跑题风险。下一轮 decide 或 rollback 以
`prior_revision_assessment` 结清该合同；只有 `promote_challenger` 才允许把挑战者当作后续
基线，`retain_baseline` / `inconclusive` 均保留原完整候选。该闭环只在同一 workflow 与同一
authoring project 链内有效；停止仍保留基线，但新 workflow 或跨项目不继承它，也不建立
全局父版本树。机器证明的是声明、范围与因果闭环，不证明旋律、层次或“更好听”。
渲染前若撤回条件已成立，可用 `candidate_id=null` 的当前 `report_only` 复核支持
`retain_baseline` / `inconclusive` 并 rollback，不得声称已听过挑战者。rollback 后继续编辑时，
内容从 contract baseline 读取，保存 CAS 父修订取 `authoring_causal_fence.anchor_revision`。

新 accept 会随 terminal 写入时间点 `acceptance_gate`：只重验本轮已记录的 hard failure，
并绑定 authoring revision、候选 manifest Hash、hard-failure 内容 ID 与该次 readiness
结果 Hash；没有已记录 hard failure 时 readiness Hash 为 `null`。历史读取不会按当前环境
重跑它，因此 gate 不是当前 readiness，也不证明没有未记录问题或作品审美成立。
兼容读取不靠回填旧历史：基础 legacy、Claim Lifecycle、acceptance gate 与
`charter_settlement_profile=affirmative-promise-ledger-v1` 的旧 shape 均保持原语义。
`composition_governance_profile=whole-work-derivation-and-bounded-amendment-v1` 与
`revision_contract_profile=bounded-change-and-explicit-challenger-settlement-v1` 可以组合：
前者负责作品展开图、全曲问题与有界修宪，后者只负责修订范围及挑战者闭环；启用其一不会
伪造另一份合同。
启用 composition governance 才要求每轮作品展开图、全曲问题复核和有界修宪；每个新决策也继续显式携带
`derivation_ids` 与 `charter_settlement`，accept 的清偿必须覆盖全部肯定性宪章承诺，
preserve/stop 可部分或为空。旧 shape 只读兼容：旧 terminal 保持原语义且不回填，
其中缺 gate 的旧 accept 仍标记为 `legacy_unfrozen`。普通旧工作流继续在修宪前策略内
单调升级，不会因读取或普通 transition 被暗中增加新步骤；只有显式记录作品展开图时
才开启新治理，且不能降级。如果当前迭代已经有活动，新治理从下一轮开始强制执行；
本版 MCP 新建工作流默认启用修订合同，并默认组合 composition governance；仅在明确需要旧流程时传
`composition_governance=false`。若要测模型不借助项目能力时的裸表现，应完全不接入 MCP，
而不是关闭若干工具后把结果误称为裸能力。

直接终止理由中，`budget_exhausted` 是可机器核验的窄声明：只有某项正数冻结预算已达
上限、当前迭代达到 fork 上限，或历史达到上限时才允许；设为 0 的关闭项不算耗尽。
`no_material_improvement`、`external_blocker` 等其他理由仍是终审 authority 的如实声明，
合同只验证其权限与形状，不证明审美已经停滞或外部事实必然成立。

中英文官方宪法的固定 Hash 法源副本作为 package data 随 wheel 发布，并与音乐创作
参考笔记中的原文做 byte-for-byte 同步检查；条文查询不依赖运行目录存在仓库 `docs/`。

`render_workflow_candidate` 不接受 `workflow_authorization`、候选路径或输出路径。
服务端先发布唯一当前预留，从该不可变状态取得精确 authorization，渲染器在昂贵工作
前再次验证预留仍为当前且未消费，随后把同一绑定写入回执和候选 manifest，最后再由
workflow 复验并记录。断线后若状态仍是 `candidate_pending`，按新的
`expected_revision` 重调会复用同一不可变候选；也可显式 `cancel_workflow_render`。

“坏”可被证据复核，不等于“好”能被单一目标函数保证。普通
`record_workflow_evidence` 的 schema 根本不允许 hard failure，也把 reporter 固定为
`agent`；只有 `record_verified_workflow_hard_failure` 能在服务端重新运行 readiness，
并写入它实际复现的 `decision=block` issue。promise conflict 与 aesthetic risk 不会
自动阻断或触发修改；故意粗糙、失真、Lo-fi 噪声等可由有证据的例外保护，而 hard
failure 不可赦免。接受仍是特定宪章下的上下文决定，不是“客观好听”证明。

该 stdio MCP 没有可信的人类身份通道，因此由它创建的 workflow 永久冻结
`final_authority=agent`，工具参数也没有 `final_authority` 或 reviewer 身份开关。
它不会把 Agent 的调用写成 creator/listener 决定。需要人类终审时，宿主必须通过
可信入口调用核心 API，或让 Agent 以 `human_review_required` 停止并交还控制权。
完整状态机和字段合同见 [创作工作流](创作工作流.md)。

两条链不是同一个文件权限外壳：CLI 导入写三文件并提供 loss policy，MCP 导入
返回内存 bundle；CLI 候选默认位于 `output/候选/`，MCP 候选位于
`output/mcp/`；MCP 的配器、预检、定位与渲染工具默认使用
`instrument_scope="formal"`，CLI 则主要在 `roster-promote` 阶段确认调色板。
两种入口各自保留自己的路径、权限与编制合同。

新会话可先调用 `diagnose_runtime(check_level="quick")`。它是运行时快速自检，不是音频探针；
完整的项目资源检查放在 `validate_project` 之后说明。

### 1. 读取当前合同和调色板

每个新会话先调用：

```text
score_and_roster_format()
list_instruments()
```

省略参数时，`list_instruments` 使用 `instrument_scope="formal"`、
`detail_level="summary"` 和 `limit=32`，从当前 103 个正式可调用声音入口返回
第一页；`catalog_count=103`、`has_more` 和 `next_offset` 描述整个目录及后续页。
显式传入 `instrument_scope="curated"` 可读取 25 个作者策展入口；formal 返回项中
的 `curated` 字段同时标出它是否属于该子集。
既有客户端仍可使用兼容参数 `trusted_only`：`true` 对应 `curated`，`false` 对应
`formal`。新调用推荐直接使用 `instrument_scope`。

顶层 `curation_state` 表示策展标记是否已载入，正常发行树返回 `available`，并由
`curated_count=25` 给出策展入口总数。formal 目录独立提供完整检索；当运行环境
返回 `curation_state="unavailable"` 时，条目的 `curated` 使用 `null` 表示当前未载入
该标记。

[`可信乐器.json`](../可信乐器.json) 是 curated 子集的版本化数据源。客户端与
Agent 提示词不应固化数量，而应读取 `curation_state`、`curated_count` 和每个条目
的 `curated` 字段；上面的 25 描述当前发行树。

正式范围当前按 `routing_class` 分为三类：

| `routing_class` | 当前数量 | 配器用途 |
| --- | ---: | --- |
| `instrument` | 68 | 旋律、和声、低音、织体等常规乐器声部。 |
| `percussion` | 27 | 现代鼓组与管弦打击，包括定音鼓、钟琴等有音高打击乐。 |
| `effect` | 8 | 环境、拟音与效果声事件。 |

成功结果的顶层 `routing_class_semantics` 还会返回这三类的机器可读定义，Agent 可
直接据此选择普通 assignment、逐键 `kit` 或 ambience/effect 声部。

大型调色板可在服务端组合筛选并分页：

| 参数 | 作用 |
| --- | --- |
| `query` | 在乐器路径、显示名、实现类型和奏法名中做不区分大小写的文本检索。 |
| `category` | 按乐器路径的第一级分类筛选，例如 `管弦乐`。 |
| `routing_class` | 选择 `instrument`、`percussion` 或 `effect`。 |
| `articulation` | 只返回支持指定奏法的入口。 |
| `pitch_mode` | 选择 `pitched`、`ignore`、`fixed` 或 `unspecified`。 |
| `detail_level` | `summary` 适合发现和分页且为默认值；`full` 返回完整音域、奏法与能力合同。 |
| `offset` / `limit` | 从零起分页；`limit` 为 1–256，默认 32。 |

例如，检索支持持续奏法的管弦旋律乐器：

```text
list_instruments(
  instrument_scope="formal",
  category="管弦乐",
  routing_class="instrument",
  articulation="sustain",
  pitch_mode="pitched",
  query="长笛",
  detail_level="summary",
  offset=0,
  limit=16
)
```

返回中的 `catalog_count` 是所选范围总数，`matched_count` 是筛选命中数，`count`
是本页条目数；`has_more=true` 时继续使用 `next_offset`。确定目标后，以
`query` 锁定路径并以 `detail_level="full"` 读取完整合同：

```text
list_instruments(
  instrument_scope="formal",
  query="管弦乐/木管组/长笛",
  detail_level="full",
  offset=0,
  limit=1
)
```

完整条目中优先读取
`articulation_range_contracts[articulation].midi_ranges`；该字段已经解析奏法继承，
可直接用于写音。顶层 `range` 则提供整件乐器的总体音域视图。

`pitch_mode="pitched"` 按谱面音高选择或移调；`ignore` 用合法原生键位选择样本或
变体，谱面键位不同时可在 assignment 或 kit 项中使用 `transpose` 对齐；`fixed`
则把谱面音符直接路由到该入口声明的 `fixed_midi_note`。

### 2. 统一导入

```text
import_score_project(
  source_path="乐谱/曲目/某曲/MusicXML/某曲.mxl",
  instrument_scope="formal",
  candidate_limit=8
)
```

成功结果的 `bundle` 包含：

- score v1；
- 可持久化的 `import_report`；
- `executable=false` 的 `roster_draft`；
- 与源文件和 score 绑定的 SHA-256；
- 每声部有限数量的非执行候选提示。

候选提示、轨道名称、Program Change、CC7、CC11 和轨道顺序作为导入信息保存在
报告与草稿中，正式路由由 `confirm_roster` 写入。旧 `import_midi` 与
`import_musicxml` 为兼容调用保留；新工程优先使用统一入口，让 MIDI 与 MusicXML
共享同一工程导入合同。

MCP 统一导入返回内存 bundle，客户端可选择持久化位置；CLI 入口另行提供
`loss-policy`。客户端检查 warnings/report 后，可把整套三文档持久化。导入报告
与 score、draft 一起保存，明确记录重复、倚音、踏板、弯音、歌词、排版和厂商
控制信息等源格式语义在当前 score 合同中的表示情况。

### 3. 显式确认 roster

```text
confirm_roster(
  score=...,
  roster_draft=...,
  assignments=[
    {"part": "Piano", "instrument": "键盘乐器/钢琴"},
    {
      "part": "Violin",
      "instrument": "管弦乐/弦乐组/小提琴",
      "articulation_map": {"arco": "sustain"}
    }
  ],
  instrument_scope="formal"
)
```

普通声部提交 `instrument`，打击声部提交逐键 `kit`。工具会先重验 draft 绑定的
score Hash，再要求 score 的每个 part 恰好出现一次，并检查乐器存在性、许可状态
及所选范围。`articulation_map` 的键是 score 中的奏法标记，值是目标乐器在
`list_instruments` 中声明的奏法；上例把 score 的 `arco` 映射为小提琴的 `sustain`。

一个打击声部可以在同一份 assignment 中逐键展开为多个执行器。下面的映射已与
当前正式目录中的底鼓、边击军鼓和闭合踩镲能力合同对齐：

```json
{
  "part": "Drums",
  "kit": {
    "C2": "现代鼓组/底鼓",
    "D2": "现代鼓组/边击军鼓",
    "A1": {"instrument": "现代鼓组/闭合踩镲", "transpose": 9}
  }
}
```

score 中写 `C2`、`D2`、`A1` 即可选择相应鼓件；底鼓与军鼓的 `fixed` 路由直接
落到各自的 `fixed_midi_note`，闭合踩镲则把 `A1 + 9` 对齐到合法原生选择键
`F#2`。每个 kit 项既可直接写乐器路径，也可写 `{instrument, transpose}`。

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
  instrument_scope="formal"
)
```

它与正式渲染共享结构、时间坐标、许可、可信策略、路由、音域和演奏计划检查，
但不会打开 WAV/SFZ。`resources.level="catalog_only"` 与
`ready_to_render=null` 只说明本次没有检查实际音频资源。没有传
`render_profile` 时，预检和渲染都解析同一份版本化默认配置；但一次省略 profile
的预检，不能用来证明后来另一份自定义 profile 也会过门。

#### 分级项目复核

`validate_project`、`check_project_readiness` 与 `render` 都返回
`project_review`。它把“当前请求是否满足硬执行合同”和“一个可渲染的创作选择是否
值得试听复核”分成两条独立结果：

- 结构、时间坐标、许可、乐器范围、显式路由、实际可演奏性和资源预算等硬合同问题
  继续进入 `issues`，并据此决定 `ok`、`status` 与是否可以渲染；
- 音域画像、发音补偿、自动奏法覆盖、协奏覆盖信息和同源齐奏候选进入只读
  `project_review`；它们提供证据和复核方向，不改变硬门禁；
- `compatibility` 是默认音域模式：在已声明的硬可演奏范围内保留扩展音区、边缘音色
  与实验性写法，同时报告值得关注的证据；`strict_hq` 仍是创作者显式选择的严格
  高质量画像门禁。

典型返回形状如下：

```json
{
  "project_review": {
    "$schema": "https://tianlai.local/schemas/project-review.schema.json",
    "kind": "tianlai.project_review",
    "schema_version": 1,
    "status": "review_recommended",
    "review_recommended": true,
    "continuation_allowed": true,
    "blocking_count": 0,
    "review_count": 2,
    "advisory_count": 1,
    "binding": {
      "score_sha256": "...",
      "roster_sha256": "...",
      "performance_plan_sha256": "..."
    },
    "items": [
      {
        "id": "selfcheck-0123456789abcdef0123",
        "level": "warning",
        "decision": "review",
        "blocking": false,
        "code": "range.outside_current_hq_candidate",
        "scope": {"executor_id": "violin", "part_id": "Violin"},
        "evidence": {"affected_note_count": 3},
        "suggestions": ["保留当前写法并先试听实际音色。"],
        "automatic_change": false
      }
    ]
  }
}
```

每个 item 的 `id` 由稳定 code、定位范围和证据生成；`binding` 把整份复核绑定到
本次 score、roster 与演奏计划 Hash。`level=warning` 表示建议优先试听，
`level=info` 提供覆盖或上下文信息；两者的 `blocking` 均为 `false`，也不需要
`force` 或 `ignore` 参数。客户端可把 `scope` 用于定位声部、执行器、事件或乐器，
把 `evidence` 与 `suggestions` 展示给创作者。所有 item 都固定
`automatic_change=false`，因此复核报告本身不会修改 score、roster、演奏计划或音频。
完整机器合同见 `schemas/project-review.schema.json`，未来 UI 可直接按该 Schema
校验和展示，而不必推测字段含义。

三个入口返回相同语义：预检适合在渲染前查看，资源就绪检查在同一复核旁补充实际
引用状态，成功渲染则把与实际候选输入一致的复核结果一并返回。硬合同继续由
`issues` 表达，现有客户端可以渐进采用 `project_review`。CLI 的
`project-render` JSON 结果使用同一个 `project_review` 键；`ensemble`（包括
`--plan-only`）还会在输出目录写出 `创作自检.json`，供不进入 MCP 的工作流复核
同一份分级结果。

#### 运行时与项目资源自检

建议用下列有界流程补上实际资源就绪性：

```text
diagnose_runtime(check_level="quick")
validate_project(score=..., roster=..., render_profile=...)
check_project_readiness(
  score=...,
  roster=...,
  render_profile=...,
  instrument_scope="formal",
  verify_references=true
)
```

`diagnose_runtime` 的 `quick` 检查 manifest 显式引用；`references` 还会展开专用 SFZ
的样本引用，所以更慢。两种级别都严格被动：不加载外部原生库，不启动 `tar`、
`bsdtar` 或任何外部程序，不创建临时文件，也不联网、下载、安装、解码或试播音频。
macOS x86_64 的只读进程内 `sysctlbyname` 身份查询属于被动平台检查，而不是主动能力探针。
实际 MCP 输出目标 `output/mcp` 的 `writable_estimate` 只是基于文件系统元数据与权限的被动估计；
`probe_performed=false` 表示没有执行实际写入探针。

`check_project_readiness` 重用同一项目预检，再检查当前 roster 实际引用的乐器。
`ready_for_render_attempt=true` 汇总表示合同预检、资源引用、平台判断与输出位置
评估均已就绪；随后由 `render` 执行乐器实例化、音频处理和候选写入。

macOS x86_64 下，MCP 自检会直接核验当前进程的 Rosetta 状态。确认原生 Intel 才会
令平台与渲染环境就绪；确认转译或无法读取身份信息都会失败关闭，readiness 不授权客户端
继续调用 `render`。协议本身不能强迫绕过 readiness 的客户端遵守这项治理决定。

资源缺失时，把 `restore_plan_handoff.instrument_ids` 原样交给：

```text
plan_resource_restore(
  instrument_ids=readiness.restore_plan_handoff.instrument_ids
)
```

恢复计划只返回去重后的资源族、本机状态、估算下载/安装体积与许可义务；它不联网、
不下载、不解压、不安装、不产生持久写入。实际恢复仍由使用者在本机审阅许可与体积后显式执行，
完成后重跑 `check_project_readiness`。

这三个自检/规划工具只返回安全的相对乐器/资源族身份、状态、计数和稳定 issue code。
输出会脱敏用户名、本机绝对路径、环境变量值、下载 URL 和原生加载器错误细节；
需要运维级路径时，由使用者在本机运行 `tianlai-doctor` 查看，不把该报告直接塞进 Agent 上下文。

为避免 Agent 手工复制时漏掉厅堂、分轨或缓存参数，`validate_project` 会返回可
直接交给 `render` 的：

```json
{
  "render_handoff": {
    "render_profile": {"kind": "tianlai.render_profile", "...": "..."},
    "expected_render_profile_sha256": "64位小写SHA-256",
    "instrument_scope": "formal"
  }
}
```

正式渲染同时传入这三个字段；若 profile 被中途改写，`render` 会在创建候选目录
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
  instrument_scope=validation.render_handoff.instrument_scope
)
```

未显式覆盖时使用版本化默认 render profile。成功结果返回 `candidate_id`、
`candidate_directory`、`mix_wav`、计划、回执、许可旁车、可选分轨、音域诊断、
`project_review`、混音报告和缓存遥测。

### 5. 从实际候选定位

听到 `34.2` 秒有问题时，优先使用：

```text
locate_rendered_candidate(
  candidate_directory="安全化曲名/候选ID",
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
  before_candidate_directory="安全化曲名/候选1",
  after_candidate_directory="安全化曲名/候选2"
)
```

比较工具区分 score、roster、render profile、演奏计划与回执身份。它说明“哪里
变了”，不判断哪一版更好听；创作者仍需直接 A/B 两个 `合奏.wav`。

## 候选不可变规则

下面的目录与工件合同描述当前 workflow 受管的 Candidate v2（以及旧 v1 语义）。
direct `project-render-v2` 发布的 Candidate v3 是独立 Score-v2/runtime-evidence 闭集，
不接受 `authoring_workflow`，也不会被 workflow accept 冒充。MCP 渲染默认写入：

```text
output/mcp/<安全化曲名（无身份 Hash）>/<唯一候选 ID>/
```

曲名父目录只用于干净地归组作品，不承担身份。`候选.json` 内的 `work_id` 继续作为
绑定 Hash 的作品身份，通常不等于父目录名；候选目录名则必须继续与
`candidate_id` 完全一致。加载与完整性核验接受当前干净父目录，也兼容历史
`<work_id>/<candidate_id>/` 布局。authoring 与 workflow 的内部候选仍由
`output/mcp/authoring-projects/` 专用命名空间管理，不套用普通候选的目录整理规则。

每个目录最后写入 `候选.json`，绑定：

- `score.json`；
- `roster.json`；
- `render-profile.json`；
- 演奏计划 Hash；
- `渲染后自检.json`（由 v3 回执间接绑定）；
- `渲染回执.json` 及其 Hash。

候选一旦用于定位、比较或听审，就应当作不可变快照。不要就地编辑这些文件，也
不要把新音频复制进旧候选冒充同一代。正常迭代创建新 `candidate_id` 并设置
`parent_candidate_id`。

当前 workflow 推导只保存在不可变 workflow 历史和迭代决策的 `derivation_ids` 引用中；
Candidate v2 与 Candidate v3 闭集本身都不内嵌这份 provenance；完成或停止也不会生成可随候选搬运的账本。
这项能力尚未完成。后续应由独立的 Candidate Provenance Envelope / portable bundle
绑定原候选 manifest Hash、终局 workflow 修订与所选推导，并保持候选目录逐字节不变。
不得在 accept 后向既有候选或 render receipt 追加旁车文件，否则会破坏其闭集完整性。

成功的 `render` 结果同时返回渲染后自检路径、完整报告与有界 `summary`。硬合同
已经在候选发布前验证；`warning` 是需要结合分轨和实际收听复核的风险证据，不是
Agent 可以据此自动改增益、滤波、修相位或裁剪尾音的授权。指标和判定边界见
[渲染后自检](渲染后自检.md)。

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

`suggest` 针对 roster 明确声明的关系生成有界相对增益草稿，固定
`executable=false`、`audio_modified=false`、`creator_review_required`。Agent
先定位候选片段，再由创作者决定是否调整增益、音区、力度、时值或配器。报告
保留分析窗口、关系与创作者复核状态，便于把机器指标与实际听审对应起来。

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

缓存损坏时会安全回退到普通渲染。缓存主要加速第二次混音；首次冷渲染执行完整
乐器链路。每次成功渲染还会写出 `缓存遥测.json`，其中
`total/accounted/unaccounted` 闭合记录命中、未命中与绕过；候选清单按 SHA-256
绑定该文件，单独改写遥测会被候选加载拒绝。`use_stem_cache=false` 同时关闭
这两层迭代缓存；需要跨机器证明候选未被整体替换时，仍应另外保存候选清单 Hash
或使用签名发布记录。

## 使用与发布建议

- MCP 以离线渲染方式生成可复算候选、分轨、回执与诊断报告；
- MIDI/MusicXML 导入同时返回报告，便于创作者确认进入 score 的音乐语义；
- 推荐依次运行 `validate_project`、`check_project_readiness` 和 `render`，让合同、
  资源与正式候选共享同一范围和 render profile；
- 每一步都可读取 Hash 绑定的 `project_review`；先处理硬合同门禁，再结合稳定 ID、
  定位范围与证据试听非阻断复核项；
- 乐器路由、奏法映射和项目修改均使用显式输入，便于复查和重复执行；
- 候选定位把实际听到的时间映射回计划事件，适合继续局部编辑与 A/B；
- 诊断指标为旋律、和声、配器与混音决策提供客观参考，最终取舍由创作者听审；
- 长曲首次渲染完成后，分轨缓存可加速后续重混；
- 正式发布前结合人工听审，核对输入作品、音源许可、署名和输出权利。

CLI 的同一闭环见
[从乐谱到第二次渲染](从乐谱到第二次渲染.md)，实时实现状态见
[当前状态](当前状态.md)。
