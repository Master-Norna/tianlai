**简体中文** | [English](VPO音源许可与安装说明.en.md)

# VPO 音源许可与安装说明

本说明适用于共用同一套官方 VPO 3.2 Wave + 3.3 Standard 安装树的 31 个声音
入口。其中 30 个入口使用 VPO 的混合许可内容并登记为 `grandfathered`；另 1 个
中提琴入口只读取该树内的 VSCO2-CE CC0 子树，单独登记为 `approved`。本文记录
安装方式、固定归档、完整树摘要与两种许可边界。

## 结论

30 项混合许可入口均为 `grandfathered`，不是 `approved`。该状态表示它们可以按
冻结的官方发行包在使用者本机安装和渲染，但不能作为接入新音源或重新分发采样
的通用许可白名单。中提琴入口因运行时范围被限制在已核验的 VSCO2-CE CC0 子树，
不属于这 30 项例外。

这里需要区分“使用官方库渲染音乐”和“重新分发采样库”：

- 天籁的源码包不携带、托管或镜像 VPO 的 SFZ/WAV；
- 用户运行安装器时，从 VPO 官方下载链取得完整 Wave 3.2 与 Standard 3.3；
- 两个官方 ZIP 只在本机按上游说明原样合并，天籁没有生成或改写音源树；
- VPO 汇总说明表达了允许使用该库制作音乐（包括商业音乐）的意图，但该说明不是
  对每个底层权利人的统一豁免；实际使用仍由命中的逐组件正式许可控制。

因此 30 项可以在冻结安装边界内参与渲染，但本文不对所有商业用途、广告用途或
所有输出形态作无条件授权承诺。
若以后改为由天籁托管、拆分、转换或重新发布采样，当前结论立即失效，必须重新做
逐来源与派生链许可核对。

## 安装链证据

官方说明要求把 Wave Files 与 Standard Orchestra 脚本解压到同一个
`Virtual-Playing-Orchestra3/` 目录：

- [Virtual Playing Orchestra 官方项目与许可页](https://virtualplaying.com/virtual-playing-orchestra/comment-page-1/)
- Wave 3.2 官方下载跳转最终指向
  `archive.org/download/virtual-playing-orchestra-3-2-wave-files/`
- Standard 3.3 使用 VPO 官方 `virtualplaying.com/go/` 下载入口

固定归档：

| 归档 | 字节数 | SHA-256 |
|---|---:|---|
| `Virtual-Playing-Orchestra3-2-wave-files.zip` | 616,114,842 | `CA8F1E0B56EEDE35314994646E5F1F307EC349616C967FBECF627C43AA646E90` |
| `Virtual-Playing-Orchestra3-3-standard-scripts.zip` | 544,010 | `F0F2BF0E42D2A39C5F49401ADDCFFA840FD8F5525670F5945BF5093A5442BDA5` |

按官方顺序先解 Wave、再解 Standard 后：

- 1,922 个文件，共 724,695,982 bytes；
- 逐文件摘要应与冻结树一致；
- 完整树摘要：
  `B06390C70D9D701481BC6DB0CF13B6ED6F3EF6B660DAC9A51034B9BE368DF317`。

完整树摘要算法：以 `Virtual-Playing-Orchestra3/` 为根，递归枚举普通文件；相对
路径统一为 `/` 并按 Unicode ordinal、区分大小写升序；每个文件写入
`<小写文件 SHA-256><两个空格><相对路径>\n`，最后对无 BOM UTF-8 记录流求
SHA-256。

根级 [`安装VPO音源.ps1`](../安装VPO音源.ps1) 固定并校验以上两个归档，先在临时
目录合并和验证完整树，再原子替换正式目录。三个乐器目录中的 VPO 获取脚本只作为
兼容入口转调该统一安装器。

## 30 项 `grandfathered` 入口映射

| 入口组 | 天籁入口 | 官方 VPO 映射族 |
|---|---|---|
| 世界/人声/电子/现代鼓组 | 民谣提琴、合唱啊声、管弦重击、牛铃 | `Strings/2nd-violin-SOLO-*`、VPO choir、orchestral-hit、cowbell 配置 |
| 弦乐 | 低音提琴、大提琴、小提琴、弦乐合奏、拨奏弦乐、颤弓弦乐 | `Strings/*-SOLO-*`、`1st-violin-SEC-*`、`all-strings-SEC-*` |
| 木管 | 大管、单簧管、短笛、双簧管、英国管、长笛 | `Woodwinds/{bassoon,clarinet,piccolo,oboe,english-horn,flute}-SOLO-*` |
| 铜管 | 大号、弱音小号、铜管合奏、小号、圆号、长号 | `Brass/*-SOLO-*` 及 VPO section；弱音小号以 trumpet SOLO 映射加项目动态滤波实现 |
| 打击乐 | 管弦钹、管弦大鼓、木琴、木鱼、三角铁、小军鼓、钟琴 | VPO `cymbals`、`bass_drum`、`xylophone`、`woodblock`、`triangle`、`snare`、`glockenspiel` 配置 |
| 键盘 | 钢片琴 | VPO `celesta` 配置 |

第 31 个入口是 `管弦乐/弦乐组/中提琴`。它虽然共用同一安装树，但运行时
`sample_subtree` 被限制为 `libs/VSCO2-CE/Strings/Viola Section`，许可证据为
`libs/VSCO2-CE/LICENSE.txt` 与 VPO 汇总说明中的 VSCO2-CE CC0 声明，因此保持
`approved / CC0-1.0`；它不应被计入 30 项混合许可例外。

每项运行时采样集合与 SFZ Hash 继续由各乐器的 `资源核验.json` 冻结。以上不是
凭乐器名称推断的授权，而是 30 个 manifest 的实际 VPO 类型/配置与统一官方树的
对应清单。VPO 包内有
Paul Battersby、No Budget Orchestra 等上游已经循环、混合或整理过的采样；它们
不是最初录音者未经加工的 WAV，但它们是天籁所选择的直接上游 VPO 的原样发行文件。

## 许可边界

VPO 汇总说明列出 Sonatina Sampling Plus 1.0、Mattias Westlund CC BY-SA 3.0、
No Budget Orchestra CC BY-SA 4.0、VSCO2/stamperadam CC0、University of Iowa
使用声明等来源。它不是整棵树的一份单一许可证，也不能替代各子库随附的正式
条款。

尤其需要保留下列边界：

- VPO 作者说明其意图是不限制使用该库制作音乐，包括商业音乐，也不打算要求输出
  署名；这是汇总说明中的意图陈述，不等于每个底层作者都明确放弃了正式条款；
- [Creative Commons Sampling Plus 1.0 正式条款](https://creativecommons.org/licenses/sampling%2B/1.0/legalcode.en)
  要求衍生使用是善意、部分且高度变换性的，要求保留相应署名与许可告知，并排除
  为其他产品或服务所作的广告/促销使用；推广该衍生作品或其作者的情形除外；
- CC BY-SA 3.0 / 4.0 组件各自含署名和相同方式共享条件。具体输出是否以及如何构成
  对相应许可材料的分享或改编，取决于实际命中的组件和使用方式，不能由 VPO 家族
  名称作统一推断；
- 修改、重打包或再分发 SFZ/WAV 时，必须逐来源履约并保留许可证据；VPO 的汇总说明
  还要求派生采样库保持免费或仅供个人使用。

因此：

- 30 件混合许可乐器可以在冻结边界内本机渲染；对外发布时仍须按实际组件与用途
  复核，天籁不承诺所有商业用途或广告用途均无附加条件；
- 正常音乐成品与可复用采样包不是同一分发物，但这种区别本身不会覆盖底层许可；
- 在尚未取得逐来源明确豁免前，对外发布音乐时必须保留天籁生成的 `许可与署名`
  sidecar，其中包含 VPO、Paul Battersby 及汇总说明所列组件来源的保守统一署名；
  该 sidecar 是履约辅助，不是新的授权或法律结论；
- VPO 的 `Documentation/license.htm` 必须随本机音源保留；
- 天籁源码包、发布包和 Git 不携带这些音源；
- 不公开发布可被当作采样素材复用的干音、分轨或半音阶全音域试音；
- `grandfathered` 不等于 CC0，也不能作为以后接入新音源时降低白名单门槛的依据；
- 第 31 个中提琴入口只按其已核验的 VSCO2-CE CC0 子树处理，不把 CC0 结论外推到
  VPO 的其他目录。
