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

> 当前候选版本：`0.5.0rc2`
>
> **发行边界：** 正式产品是项目提供的轻量源码 ZIP。若未来发布 PyPI 的
> sdist/wheel，`tianlai-audio` 只提供可复用的 Python 引擎，不包含完整乐器目录、
> Schema、Windows 安装脚本或大型音源，不能替代正式源码发行包。

## 选择运行入口

| 环境 | 最短入口 | 当前边界 |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows 三步上手](#windows-三步上手) | `0.5.0rc2` 完整参考平台 |
| Linux / WSL | [Linux / WSL 快速开始](docs/Linux快速开始.md) | 已提供 Bash、程序音色与 MCP 入口；成功链和真实采样按层验收 |

Linux / WSL 用户进入源码根目录后可先运行：

```bash
bash ./bootstrap_linux.sh
```

在受支持的 64 位 CPython 3.11–3.14 上，它会创建 Linux 自己的 `.venv`，安装
核心与 MCP 依赖，运行环境诊断，并生成不依赖外部采样的第一份 WAV。不要在
Windows 和 WSL 之间共用 `.venv`。支持范围、MCP stdio 配置和外部采样限制见
[Linux / WSL 快速开始](docs/Linux快速开始.md)。

## Windows 三步上手

Windows 10/11 x64 与 64 位 CPython 3.11–3.14 是 `0.5.0rc2` 的参考环境。
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

`0.5.0rc2` 推荐使用下面这一条主链，而不是分别调用早期导入和合奏命令后手工拼接
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
`instrument`，打击声部必须显式提交 `kit`。默认可信调色板是策展边界，不是许可
豁免；隔离资源和仅限本机兼容的 SoundFont 不会因打开调色板而进入公共链路。

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

MCP 服务当前实际公开 15 个工具，覆盖合同读取、乐器发现、统一导入、显式编制
确认、乐谱切片/补丁/比较、预检、当前计划定位、已渲染候选定位/比较和正式渲染。
推荐 MCP 主链与 CLI 在概念上对应，但文件载体、输出根和默认 trusted 门禁不同：

```text
import_score_project → confirm_roster → validate_project
        → render(**render_handoff)
        → locate_rendered_candidate → get_score_slice → patch_score
        → validate_project → render(parent_candidate_id=..., **render_handoff)
        → compare_rendered_candidates
```

只有 `render` 写入音频；其他工具返回内存对象或读取已有候选。文件型导入受 MCP
输入根策略约束，客户端不会因为接入服务就获得整台电脑的任意读取权限。完整的
15 工具表、输入根配置和候选规则见 [MCP 接口](docs/MCP.md)。预检返回的
`render_handoff` 同时携带完整 profile 与其规范化 Hash，正式渲染可在创建候选前
拒绝误换配置。缓存、重混和不可变候选的使用边界也在该文档中说明。

## 当前能力与边界

- 已登记 103 个声音入口，当前绑定版本完成了单乐器、单音色独立试听并标记为
  `quality_tier=formal`；这不等于全部音区、力度、奏法、运行变体或专家级评价
  均已验收。
- 多乐器配器、动态、空间和真实作品仍缺少系统性的正式合奏验收，因此
  `collaboration_review_status` 与单音色质量分开维护。
- `manual`、`analyze`、`suggest` 都不会自动改谱或改变音频；`suggest` 只生成
  有界、不可执行的诊断草稿。
- 当前渲染是离线执行，不是实时软音源。首次冷渲染仍逐轨执行；长曲、多声部和
  共享厅堂可能需要较长时间与较高峰值内存。分轨缓存与内容寻址分析缓存主要加速
  后续重混；启用缓存的渲染会留下闭合遥测，候选渲染还会在候选清单中绑定其
  Hash，但缓存不能消除首次演奏成本；`write_stems=true` 时，热重混仍需重写
  公开分轨并计算厅堂与最终混音，不能把“缓存全命中”理解成零 I/O。
- 采样重放仍有重采样质量升级空间；音源质量、覆盖音域和某种组合下的听感不能由
  Schema 或测试自动保证。`strict_hq` 是失败关闭的证据闸门，不是音质增强开关。
- 峰值、RMS、频谱、相位、音域和差异报告是排错仪表，不会判断旋律、编曲或作品
  是否成立。最终候选仍需人耳 A/B。

实时状态和更细的限制见 [当前状态](docs/当前状态.md)。

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
必须失败。测试通过只证明相应机器合同成立，不代表所有音色与作品已经通过人工
听审。

文档入口见 [文档地图](docs/README.md)，完整谱面迭代约定见
[从乐谱到第二次渲染](docs/从乐谱到第二次渲染.md)。贡献代码或提交可复算
听感反馈前请看
[参与贡献](CONTRIBUTING.md)；安全边界和私密报告原则见
[安全策略](SECURITY.md)。
