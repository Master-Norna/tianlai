# VPO 音源许可与安装说明

本说明适用于共用同一套官方 VPO 3.2 Wave + 3.3 Standard 安装树的 30 个声音
入口，记录安装方式、固定归档、完整树摘要与混合许可边界。

## 结论

30 项均为 `grandfathered`，不是 `approved`。该状态表示它们可以按冻结的官方
发行包在使用者本机安装和渲染，但不能作为接入新音源或重新分发采样的通用许可
白名单。

这里需要区分“使用官方库渲染音乐”和“重新分发采样库”：

- 天籁的源码包不携带、托管或镜像 VPO 的 SFZ/WAV；
- 用户运行安装器时，从 VPO 官方下载链取得完整 Wave 3.2 与 Standard 3.3；
- 两个官方 ZIP 只在本机按上游说明原样合并，天籁没有生成或改写音源树；
- VPO 明确允许使用该库制作音乐，包括商业音乐；复杂的逐来源条件主要约束修改、
  重打包和再分发采样库。

因此 30 项可以作为冻结的混合许可资源参与渲染。
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

## 30 项入口映射

| 入口组 | 天籁入口 | 官方 VPO 映射族 |
|---|---|---|
| 世界/人声/电子/现代鼓组 | 民谣提琴、合唱啊声、管弦重击、牛铃 | `Strings/2nd-violin-SOLO-*`、VPO choir、orchestral-hit、cowbell 配置 |
| 弦乐 | 低音提琴、大提琴、小提琴、弦乐合奏、拨奏弦乐、颤弓弦乐 | `Strings/*-SOLO-*`、`1st-violin-SEC-*`、`all-strings-SEC-*` |
| 木管 | 大管、单簧管、短笛、双簧管、英国管、长笛 | `Woodwinds/{bassoon,clarinet,piccolo,oboe,english-horn,flute}-SOLO-*` |
| 铜管 | 大号、弱音小号、铜管合奏、小号、圆号、长号 | `Brass/*-SOLO-*` 及 VPO section；弱音小号以 trumpet SOLO 映射加项目动态滤波实现 |
| 打击乐 | 管弦钹、管弦大鼓、木琴、木鱼、三角铁、小军鼓、钟琴 | VPO `cymbals`、`bass_drum`、`xylophone`、`woodblock`、`triangle`、`snare`、`glockenspiel` 配置 |
| 键盘 | 钢片琴 | VPO `celesta` 配置 |

每项运行时采样集合与 SFZ Hash 继续由各乐器的 `资源核验.json` 冻结。以上不是
凭乐器名称推断的授权，而是 30 个 manifest 的实际 VPO 类型/配置与统一官方树的
对应清单。VPO 包内有
Paul Battersby、No Budget Orchestra 等上游已经循环、混合或整理过的采样；它们
不是最初录音者未经加工的 WAV，但它们是天籁所选择的直接上游 VPO 的原样发行文件。

## 许可边界

VPO 总许可将来源分为 Sonatina Sampling Plus、Mattias/No Budget Orchestra
CC BY-SA、VSCO2 CC0、University of Iowa 无限制使用声明等，并明确区分：

- 使用库制作音乐：允许，包括商业用途；
- 修改、重打包或再分发采样库：必须逐来源履约、署名，并保持派生库免费或仅供
  个人使用。

因此：

- 30 件乐器可以本机渲染，渲染出的正常音乐成品可以按作品本身的权利条件使用；
- VPO 作者说明音乐输出没有要求署名的意图，但部分底层 Sampling Plus / BY-SA
  正式条文本身含署名条件；在尚未取得逐来源明确豁免前，对外发布音乐时附一份
  VPO 及其来源的统一署名最稳妥，不承诺“绝对无需署名”；
- VPO 的 `Documentation/license.htm` 必须随本机音源保留；
- 天籁源码包、发布包和 Git 不携带这些音源；
- 不公开发布可被当作采样素材复用的干音、分轨或半音阶全音域试音；
- `grandfathered` 不等于 CC0，也不能作为以后接入新音源时降低白名单门槛的依据。
