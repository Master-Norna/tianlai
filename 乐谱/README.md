# 乐谱目录

`乐谱/` 保存使用者自己的谱面来源、天籁 score 修订、补丁和正式编制。它是创作
工作区，不是公开示例目录；默认不进入 Git 或轻量源码发布包。

天籁把 score v1 作为权威可编辑中间态。MIDI/MusicXML 是来源或交换格式，
候选 WAV 是一次执行快照；两者都不应取代曲目目录里的 score 修订历史。

下文 `cmd` 代码块在项目根目录的“命令提示符（cmd.exe）”执行，多行续写符为
`^`。

## 每首作品一个目录

推荐结构：

```text
乐谱/
└─ 曲目/
   └─ 某曲/
      ├─ MIDI/
      │  └─ 某曲.mid
      ├─ MusicXML/
      │  └─ 某曲.mxl
      ├─ 导入-01/
      │  ├─ 某曲.score.json
      │  ├─ 某曲.import-report.json
      │  └─ 某曲.roster-draft.json
      ├─ 某曲.roster.json
      ├─ 某曲.rev02.score.json
      ├─ 某曲.rev02.patch-result.json
      ├─ patches/
      │  └─ patch-01.json
      ├─ 某曲.render-profile.json       # 可选
      ├─ 某曲_作曲生成器.py              # 可选
      └─ 来源说明.md                     # 需要时记录作品/谱面权利
```

没有使用某种来源格式时不必创建空目录。`导入-01/` 是一次不可覆盖的导入代次；
需要换 loss policy 或换源文件时使用 `导入-02/`，不要把新结果混进旧三文档包。

候选不放在这里：

- CLI `project-render` 默认写 `output/候选/<作品 ID>/<候选 ID>/`；
- MCP `render` 写 `output/mcp/<作品 ID>/<候选 ID>/`；
- 确认用于对外发布的成品可以复制或重新导出到 `output/作品/`，不要移动或改写
  原候选证据。

候选目录由回执和 `候选.json` 绑定，应按不可变快照使用。

## 推荐流程

### 1. 统一导入

```cmd
天籁.cmd project-import ^
  --input "乐谱\曲目\某曲\MusicXML\某曲.mxl" ^
  --output "乐谱\曲目\某曲\导入-01"
```

MIDI 只需把 `--input` 换成 `.mid` 或 `.midi`。默认 loss policy 为 `reject`：
发现无法表达的源语义时拒绝发布导入包。若创作者确认接受降级，改用新的输出目录并
显式传入 `--loss-policy warn`，再完整保存和阅读 `import-report.json`。

每次成功导入生成：

- score v1；
- 记录源格式、降级和 Hash 的 import report；
- `executable=false` 的 roster draft；
- 有限数量的路由候选提示。

这三个文件彼此绑定。不要单独替换其中一个，也不要把 roster draft 当成正式编制。

### 2. 明确配器

```cmd
天籁.cmd roster-promote ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --draft "乐谱\曲目\某曲\导入-01\某曲.roster-draft.json" ^
  --assign "Piano=键盘乐器/钢琴" ^
  --assign "Violin=管弦乐/弦乐组/小提琴" ^
  --output "乐谱\曲目\某曲\某曲.roster.json"
```

普通声部明确选择 `instrument`；打击乐通过 assignments JSON 提交逐键 `kit`。
`--assign` 左侧必须是 `score.parts[].id` / draft `assignment.part`，不是显示名称
`parts[].name`。
轨道名、Program Change、CC7/CC11、轨道顺序和 routing hints 都不能自动取得
执行权限。声部角色、增益、自动化、座位、组与相对平衡关系也应由创作者显式
决定。

### 3. 第一个候选

```cmd
天籁.cmd project-render ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --roster "乐谱\曲目\某曲\某曲.roster.json" ^
  --title "某曲"
```

记录返回的 `candidate_id`。不要编辑候选里的 `score.json`、`roster.json`、
WAV 或回执；它们描述的是这一次已经发生的执行。

### 4. 从秒数回到 score

```cmd
天籁.cmd candidate-locate ^
  --candidate "output\候选\作品ID\候选ID" ^
  --at 34.2 ^
  --output "output\诊断\某曲-34.2秒.json"
```

定位结果给出当时活动、近期结束和即将进入的事件，以及 `event_id`、part、
乐器、小节和拍位。近期事件只是 release/厅堂来源候选，仍需结合分轨与听感。

先用 `score-slice` 读取有界片段和 `score_sha256`，再用 `score-patch` 写新修订。
补丁同时绑定基线 Hash、事件 ID 和可选旧值；冲突时整批拒绝。

```cmd
天籁.cmd score-patch ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --patch "乐谱\曲目\某曲\patches\patch-01.json" ^
  --output "乐谱\曲目\某曲\某曲.rev02.score.json" ^
  --result-output "乐谱\曲目\某曲\某曲.rev02.patch-result.json"
```

### 5. 第二个候选和 A/B

```cmd
天籁.cmd project-render ^
  --score "乐谱\曲目\某曲\某曲.rev02.score.json" ^
  --roster "乐谱\曲目\某曲\某曲.roster.json" ^
  --title "某曲" ^
  --parent-candidate "candidate-第一版ID"
```

```cmd
天籁.cmd candidate-compare ^
  --before "output\候选\作品ID\候选1" ^
  --after "output\候选\作品ID\候选2" ^
  --output "output\诊断\某曲-候选1-候选2.json"
```

机器比较负责说明 score、roster、profile、计划和混音身份发生了什么变化；最终
仍应直接 A/B 两个 `合奏.wav`。

## score v1 编辑规则

每个音符必须有全谱唯一、稳定的 `event_id`：

- 移动音符、改音高、力度、奏法或时值时保留原 ID；
- 只有新增音符才分配新 ID；
- 删除后重新添加是新事件，不应复用旧 ID 冒充连续身份；
- 稳定 ID 使候选定位、补丁冲突和版本比较在多轮迭代后仍可复算。

音符还支持可选的 `staff` 和 `voice`：

```json
{
  "event_id": "piano-0042",
  "bar": 8,
  "beat": 1,
  "duration_beats": 2,
  "pitch": "C5",
  "tie": true,
  "staff": 1,
  "voice": "1"
}
```

简单程序化 score 可以省略这两个字段。MusicXML 导入会保留它们，因为 MusicXML
的 voice 由 staff 限定；展平到同一个天籁 part 后，连音仍需区分不同谱表和谱内
声部。`staff/voice` 不是 roster part，也不选择乐器。编辑 MusicXML 派生谱面时，
除非确实改变谱内结构，否则应保留。

## 生成器和唯一真源

程序化作品必须明确哪一份是创作真源：

- 生成器为真源：修改 `<曲名>_作曲生成器.py` 后重新生成，不要在即将被覆盖的
  JSON 中积累手改；
- score 为真源：直接维护 score 修订，不再运行会无条件覆盖它的旧生成器；
- roster 只描述路由、角色、增益、自动化、座位和执行参数，不把这些混入 score；
- render profile 单独保存，便于比较“改谱”和“只改执行/混音”。

只改 gain、pan、座位、厅堂、master 或归一设置时，原始分轨缓存通常可以直接
重混；改音符、奏法、音源、有效乐器参数或 DSP 时相关分轨会重新执行。

## 导入与导出边界

MusicXML 当前可以保留音符、和弦、拍号、速度、力度、常见奏法、连音、移调实音、
已映射打击乐，以及用于多声部连音归属的 `staff/voice`。重复、倚音、歌词和排版
等不能无损表达的内容以 import report 为准。

MIDI 保留逐音力度、速度变化和可审计轨道证据，但不把 Program Change 自动映射为
专用乐器，也不把 CC7/CC11 猜成 dB。踏板、弯音、触后和设备消息不保证无损进入
score。

需要回到制谱软件时可用 `export-midi` 生成带 loss report 的编辑副本。MIDI 不
保留稳定事件身份、全部奏法、微分音、乐句与专用乐器语义，因此不是 score 的
无损逆变换。

## 版本控制与权利

本目录默认不提交仓库。MIDI/MusicXML 除了承载作品，也可能包含受保护的编曲、
校订、录入或导出成果；“网上能下载”不等于可以复制、修改、渲染或再分发。

公开演示优先使用原创、明确开放许可，或作品和具体谱面编码都已进入公有领域的
材料。作品进入公有领域，不代表现代校订、编配或下载得到的 MIDI/MusicXML 也
自动开放。

天籁不会因为导入或渲染而取得输入文件或输出音乐的著作权，也不会消除第三方
音源的条款。公开前应确认作品、具体谱面版本和录入或编配成果均允许当前用途；
完整口径见 [`OUTPUT_RIGHTS.md`](../OUTPUT_RIGHTS.md)。

完整命令、补丁示例和失败排查见
[`从乐谱到第二次渲染`](../docs/从乐谱到第二次渲染.md)。
