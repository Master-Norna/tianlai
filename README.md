**简体中文** | [English](README.en.md)

# 天籁

> **人造其序，万音各自成声。**

天籁是一套 Apache-2.0 开源、本地运行的音乐执行与迭代工作台。它把 MIDI、
MusicXML 或人和 AI 写出的可编辑乐谱变成分轨与合奏 WAV；听到问题后，可以按秒
定位到具体音符，修改一处，再得到一份可比较的新版本。

它不是“输入一句话，交付一个不可拆的音频黑箱”。天籁保留谱面、配器、演奏参数、
分轨、回执和版本关系，让创作者始终能回答：

- 这一秒是哪件乐器、哪些音符在发声；
- 这一版用了哪份谱、哪套参数和哪些音源；
- 人或 AI 修改了什么，第二版与第一版究竟哪里不同；
- 结果能否在同一资源版本上重新计算并核验。

```text
一句想法 / 一份现有乐谱
          ↓
MIDI / MusicXML / 可编辑 score
          ↓
显式选择每个声部的乐器 → 首次出声
          ↓
按秒定位问题 → 人或 AI 修改具体音符
          ↓
第二次渲染 → 机器差异 + 人耳 A/B
```

天籁负责验证合同、执行乐器、保存证据和复算结果；旋律、结构、配器意图与“是否
好听”仍由创作者决定。AI Agent 可以通过 MCP 参与同一闭环，但不会因此获得自动
选乐器、自动改谱或替代人工审美的权限。

仓库自带一个不依赖外部采样的完整示例：
[最小闭环 MusicXML](examples/最小闭环.musicxml) →
[导入、修改与二次渲染教程](docs/从乐谱到第二次渲染.md)。它可以直接验证
“导入—明确配器—首次出声—定位—补丁—二次渲染—比较”，不需要先下载数 GB
音源。

> 当前候选版本：`0.7.0rc1`
>
> **发行边界：** 正式产品是项目提供的轻量源码 ZIP。若未来发布 PyPI 的
> sdist/wheel，`tianlai-audio` 只提供可复用的 Python 引擎，不包含完整乐器目录、
> Schema、Windows 安装脚本或大型音源，不能替代正式源码发行包。

## 选择运行入口

| 环境 | 最短入口 | 当前边界 |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows 三步上手](#windows-三步上手) | `0.7.0rc1` 完整参考平台 |
| Linux / WSL | [Linux / WSL 快速开始](docs/Linux快速开始.md) | 已提供 Bash、程序音色与 MCP 入口；成功链和真实采样按层验收 |
| macOS Apple Silicon / Intel | [macOS 快速开始](docs/macOS快速开始.md) | 原生 64 位 CPython 3.11–3.14；已纳入干净源码 ZIP portable CI，真实采样另行验收 |

Linux / WSL 用户进入源码根目录后可先运行：

```bash
bash ./bootstrap_linux.sh
```

在受支持的 64 位 CPython 3.11–3.14 上，它会创建 Linux 自己的 `.venv`，安装
核心与 MCP 依赖，运行环境诊断，并生成不依赖外部采样的第一份 WAV。不要在
Windows 和 WSL 之间共用 `.venv`。支持范围、MCP stdio 配置和外部采样安装与恢复见
[Linux / WSL 快速开始](docs/Linux快速开始.md)。

macOS 用户进入源码根目录后可运行：

```bash
bash ./bootstrap_macos.sh
```

入口支持原生 Apple Silicon `arm64` 与 Intel `x86_64`，会建立当前架构自己的
`.venv`、运行环境诊断，并用参考振荡器生成第一份 WAV。不要跨操作系统或 CPU
架构共用虚拟环境。全部 74 件外部资源均已纳入 15 个资源族的跨平台 Python
恢复入口；大型第三方归档仍需由使用者在本机按许可下载并完成完整性核验。完整
边界见 [macOS 快速开始](docs/macOS快速开始.md)。

## Windows 三步上手

Windows 10/11 x64 与 64 位 CPython 3.11–3.14 是 `0.7.0rc1` 的参考环境。
以下 `cmd` 代码块都在源码包根目录的“命令提示符（cmd.exe）”执行；多行续写符
是 `^`。

1. 创建项目自己的虚拟环境，先跳过自动 smoke：

   ```cmd
   安装运行环境.cmd -SkipSmoke
   ```

2. 查看代码、目录、可信清单和本机资源的真实状态：

   ```cmd
   检查运行环境.cmd
   ```

3. 用仓库自带谱例和参考振荡器确认实际写出 WAV：

   ```cmd
   天籁.cmd render ^
     --instrument "乐器\测试工具\参考振荡器\乐器.json" ^
     --events "examples\c_major.events.json" ^
     --output "output\首次出声\参考振荡器.wav"
   ```

不传 `-SkipSmoke` 时，第一步会自动执行环境诊断和第三步的首次出声，适合直接
双击。安装过程不会下载数 GB 音源。需要采样资源时可运行
`安装可恢复音源.cmd -PlanOnly` 先看许可、体积和本机状态，再运行
`安装可恢复音源.cmd`。29 件自研入口无需第三方音频资产，其余 74 件均已接入
根级恢复流程；安装完成后仍以 `检查运行环境.cmd` 的实际复核为准。资源缺失时
渲染会明确失败，不会静默换成通用 GM 音色。

更完整的安装说明见 [Windows 最小启动](docs/Windows最小启动.md) 和
[Windows 安装与巡检](docs/Windows安装与巡检.md)；从导入一直做到第二次渲染的
逐步教程见
[从乐谱到第二次渲染](docs/从乐谱到第二次渲染.md)。

仓库还带有一个不依赖外部采样的完整流程输入
`examples/最小闭环.musicxml`，以及与它绑定的 render profile、切片查询和补丁。
它既用于快速自检，也适合在处理自己的谱面前熟悉“导入—配器—候选—定位—修改—
比较”这条主链。

## 推荐创作闭环

`0.7.0rc1` 推荐使用下面这一条主链，而不是分别调用早期导入和合奏命令后手工拼接
产物：

| 阶段 | CLI | 结果 |
| --- | --- | --- |
| 统一导入 | `project-import` | score v1、导入报告、不可执行 roster 草稿 |
| 明确配器 | `roster-promote` | 每个声部恰好一次显式路由的正式 roster |
| 首次执行 | `project-render` | 唯一目录中的候选 1、音频、分轨、回执与绑定清单 |
| 听感定位 | `candidate-locate` | 从候选实际秒数回到事件、小节、拍位和执行器 |
| 有界读取 | `score-slice` | 带 score Hash 的局部乐谱 |
| 原子修改 | `score-patch` | 冲突即整批拒绝的新 score 修订 |
| 再次执行 | `project-render --parent-candidate ...` | 保留父子关系的候选 2 |
| A/B 核对 | `candidate-compare` + 人耳 | 机器说明改了什么，人决定哪版更好 |

准备好自己的谱面后，统一导入的命令模板是：

```cmd
天籁.cmd project-import ^
  --input "乐谱\曲目\某曲\MusicXML\某曲.mxl" ^
  --output "乐谱\曲目\某曲\导入-01"
```

其中路径是占位模板，需要替换为实际文件。导入得到的 `roster-draft.json` 明确为
`executable=false`。轨道名、Program
Change、CC7、CC11 或候选提示都不能自动取得正式演奏权限；普通声部必须显式选择
`instrument`，打击声部必须显式提交 `kit`。MCP 默认的 `formal` 范围覆盖全部
103 个正式声音入口；每项的 `curated` 字段还会标明其中 25 个作者策展入口，调用方
也可以显式选择较小的 `curated` 范围。

每次 `project-render` 默认创建新的唯一候选目录，并在最后写入 `候选.json`，将
score、roster、render profile、演奏计划和渲染回执绑定起来。候选应当作不可变
快照使用：不要就地编辑其中的 JSON 或 WAV。正常修订应产生新候选并记录
`parent-candidate`；受控覆盖只用于明确修复同一目标，而且必须同时提供旧回执
Hash。引擎还会在准备阶段冻结旧候选清单 Hash，并在目录交换前后递归复验计划、
合奏、分轨和许可旁车；并发修改或不完整一代不会被发布成可见候选。

## 可编辑中间态

score v1 是天籁的权威可编辑乐谱，不是一次性导入缓存。每个音符都有全谱唯一、
稳定的 `event_id`；移动、改音高、改力度或改时值时应保留原 ID，只有新增音符才
分配新 ID。`score-patch` 同时检查基线 Hash 和可选 `expect` 旧值，因此多人或
人机协作时不会悄悄覆盖过期修改。

音符还可以携带可选的 `staff` 与 `voice`。简单的程序化谱面可以省略它们；
MusicXML 导入会保留这两个谱内身份，使同一内部 part 中、相同音高但属于不同谱表
或声部的连音不会被错误合并。它们不是 roster 的 part，也不负责乐器路由；编辑
MusicXML 派生 score 时，除非确实要改变谱内归属，否则应与 `event_id` 一起保留。

MIDI 与 MusicXML 都可能包含当前 score 无法无损表达的语义。统一导入默认
`--loss-policy reject`，需要接受降级时必须显式选择 `warn` 或 `allow`，并保存
`import-report.json`。导出 MIDI 只是带损失报告的交换副本，不是 score 的无损
逆变换。

## AI 与 MCP

MCP 服务当前实际公开 44 个工具：原有诊断、导入、编制、乐谱编辑、预检、定位、
比较和渲染保持兼容，并新增路径隔离的持久 authoring project，以及可选的 v0.7
创作工作流（宪章、少量宪法条款、分阶段复核、可信 hard failure、例外、受管渲染、
修订、回滚与审计）。
推荐 MCP 主链与 CLI 在概念上对应，但文件载体、输出根和默认乐器范围不同：

```text
diagnose_runtime(check_level="quick")
        → import_score_project → confirm_roster → validate_project
        → check_project_readiness
        → render(**render_handoff)
        → locate_rendered_candidate → get_score_slice → patch_score
        → validate_project → check_project_readiness
        → render(parent_candidate_id=..., **render_handoff)
        → compare_rendered_candidates
```

`render`、`render_authoring_revision` 与 `render_workflow_candidate` 会写入音频；
其他写工具只在专用工程根内发布不可变状态或文档修订。文件型导入受 MCP
输入根策略约束，客户端不会因为接入服务就获得整台电脑的任意读取权限。完整的
44 工具表、输入根配置和候选规则见 [MCP 接口](docs/MCP.md)；工作流的诚信边界、
状态机和断线恢复见 [创作工作流](docs/创作工作流.md)。运行时与项目就绪
自检以被动方式汇总合同、资源、平台与输出位置评估；正式 `render` 完成实际实例化、
音频处理和候选写入。macOS x86_64 会在当前进程内以只读 `sysctlbyname` 核验
Rosetta 状态；该身份检查不启动外部程序、不写文件，只有确认原生 Intel 才会让
readiness 授权继续渲染，已转译或无法核验都会令 readiness 保持阻断。缺失资源可交给
`plan_resource_restore` 生成脱敏恢复计划。
`validate_project`、`check_project_readiness` 与 `render` 还会返回同一套分级
`project_review`：硬合同问题继续由 `issues` 明确门禁；可渲染的音域、发音、奏法与
编配候选则携带稳定 ID、定位范围、证据和多个复核方向，交由创作者结合试听决定。
报告与当前 score、roster、演奏计划 Hash 绑定，不会自动改谱或改音频。
预检返回的
`render_handoff` 同时携带完整 profile 与其规范化 Hash，正式渲染可在创建候选前
拒绝误换配置。缓存、重混和不可变候选的使用边界也在该文档中说明。

## 当前能力

- 已登记 103 个声音入口，当前绑定版本完成了单乐器、单音色独立试听并标记为
  `quality_tier=formal`；它们已全部进入 MCP 默认 `formal` 调用范围，仍可像以前
  一样逐件试听，也可以按类别、路由类型、奏法、音高模式与名称查询。
- 项目开发阶段已经使用大量合奏曲检验配器、动态、空间与实际渲染；新的组合还可
  通过 `manual`、`analyze`、`suggest` 工作流继续吸收创作者与社区反馈。
- `manual`、`analyze`、`suggest` 把分析与修改分层；`suggest` 生成有界、可复核的
  诊断草稿，由创作者确认后进入下一版候选。
- 离线渲染逐轨生成 24-bit WAV、可选分轨、共享厅堂与完整回执；分轨缓存和内容
  寻址分析缓存可加速后续重混，闭合遥测及其 Hash 会随候选一并保存。
- 单乐器与合奏正式入口在写盘后重新流式读取最终 PCM，生成 Hash 绑定的
  [`渲染后自检.json`](docs/渲染后自检.md)：损坏、格式错配和明确应发声却数字
  静音属于硬错误；True Peak/LUFS、DC、相位、声道与尾音风险供创作者复核，
  不自动修改音频或强加统一审美。
- `strict_hq` 按乐器声明的音域证据合同执行高质量候选门禁，适合在正式候选阶段
  固定可复算的演奏范围；默认 `compatibility` 模式则在硬可演奏合同内保留扩展
  音区与实验性音色，并通过 `project_review` 提供复核证据。
- 分级自检只让结构、许可、路由、资源、安全预算与真实可演奏性等硬合同问题阻断
  流程；创作语境提示保持非阻断，并为未来 UI 提供稳定、Hash 绑定的复核身份。
- True Peak、LUFS、峰值、RMS、频谱、相位、音域和差异报告为排错与 A/B 提供
  客观坐标，最终取舍由创作者结合实际听感完成。

实时状态和技术细节见 [当前状态](docs/当前状态.md)。

## 创作参考

[天籁音乐宪法 v0.1](docs/音乐创作参考笔记/天籁音乐宪法-v0.1.md) 是附带给人类
创作者与 AI Agent 的非规范性创作指导，不是法律、规则引擎或项目强制政策；不
采纳不会触发项目处罚或功能限制。宪法文本采用 CC BY 4.0，但参考它创作的音乐
不会因此自动采用 CC BY，天籁项目代码也继续采用 Apache-2.0。

## 许可、输出与署名

项目自研代码、DSP、CLI/MCP、Schema、测试和配置采用
[Apache-2.0](LICENSE)，原始项目署名见 [NOTICE](NOTICE)。项目名称与标识的使用
边界见 [TRADEMARKS](TRADEMARKS.md)。

第三方音源、输入作品、MIDI/MusicXML 编码和最终 WAV 不会因为经过天籁就自动采用
Apache-2.0。项目也不会仅因执行渲染而取得使用者音乐输出的著作权。公开结果前应
同时核对本次 `许可与署名.json/.txt`、输入作品权利和上游资源条款。完整口径见
[输出权利说明](OUTPUT_RIGHTS.md) 与
[音源许可政策](docs/音源许可政策.md)。

## 目录

| 目录 | 职责 |
| --- | --- |
| `tianlai/` | 导入、乐谱、指挥、渲染、候选、诊断与 MCP 核心 |
| `乐器/` | 103 个声音入口的程序、清单和核验工件 |
| `schemas/` | 稳定 JSON 合同 |
| `examples/` | 可提交、可复算的示例输入 |
| `乐谱/` | 使用者本地谱面、score 修订和 roster，默认不进 Git |
| `音源/` | 大型运行资源与下载缓存，不进轻量源码包 |
| `output/` | 候选、作品、诊断、缓存和试听产物 |
| `docs/` | 安装、使用、接口、能力边界与许可说明 |

中文适合人浏览的目录和文件；Python 包、JSON 字段、命令与稳定标识保持英文。

## 开发与验证

贡献者先安装开发依赖，再运行 pytest：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
.\.venv\Scripts\python.exe -m pytest -q -m "not external_assets and not listening"
```

这条命令是干净源码包必须通过的 portable 合同，不要求本机先安装大型第三方
音源。需要真实采样的 `external_assets` 与冻结试听环境的 `listening` 是独立
验收层；资源完全未安装时跳过，已经出现但不完整、Hash 不符或许可实物不符时仍
必须失败。portable 验证机器合同，`external_assets` 与 `listening` 分别补充真实
资源和冻结听审验收。

文档入口见 [文档地图](docs/README.md)，完整谱面迭代约定见
[从乐谱到第二次渲染](docs/从乐谱到第二次渲染.md)。贡献代码或提交可复算
听感反馈前请看
[参与贡献](CONTRIBUTING.md)；安全边界和私密报告原则见
[安全策略](SECURITY.md)。
