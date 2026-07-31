# 音源目录

本目录统一保存大型采样、SFZ、SoundFont、本地运行库、下载缓存、派生资源和安装
回执。轻量源码发布包只保留本说明；实际资源不进入 Git，也不随源码 ZIP 分发。

乐器目录保存相对资源路径、冻结版本、Hash、映射、行为代码和核验信息。资源缺失
或不匹配时，专用入口会明确失败，不会静默切换到通用 GM 音色。

## 覆盖范围

项目登记 103 件正式声音入口：

| 类型 | 数量 | 恢复方式 |
| --- | ---: | --- |
| 项目自研 DSP、合成、建模与拟音 | 29 | 不需要第三方音频资产 |
| 统一恢复清单覆盖的外部资源 | 38 | `resource_restore_manifest.json` |
| 既有固定版本安装器覆盖的外部资源 | 36 | 根级脚本转调各冻结安装器 |

因此 74 件外部资源入口均有根级安装路径。这里的“覆盖”表示能够从冻结公开上游在
用户本机下载、核验并安装，不表示天籁有权镜像或重打包原始采样。

## 推荐入口

最小 Python 环境安装完成后，先查看只读计划：

```cmd
安装可恢复音源.cmd -PlanOnly
```

无参数安装会先展示许可、已有状态、下载和磁盘估算，要求输入 `INSTALL` 后处理
全部 74 件外部资源：

```cmd
安装可恢复音源.cmd
```

按需选择统一清单资源：

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl
安装可恢复音源.cmd -ResourceGroup freepats
```

只处理另外 36 件既有安装器资源：

```cmd
安装可恢复音源.cmd -LegacyOnly
```

统一清单当前冻结 10 个资源族，覆盖 VCSL、FreePats、Karoryfer、
Emilyguitar 和 MTG Solo Sax 等 38 件入口。完整清单约需下载 4.44 GiB，安装
与派生后约占 6.57 GiB，建议至少预留 12 GiB；Salamander、SIMPK、VPO 等既有
安装器资源另计。实际剩余量以 `-PlanOnly` 的本机结果为准。

## 安装安全

统一恢复器实施以下合同：

- URL 只指向公开上游；
- 使用固定归档 SHA-256，或固定提交加完整解压树 SHA-256；
- 下载写入 `下载缓存/<文件>.part`，限制最大体积并支持断点续传；
- 所有归档先过冻结完整性与成员路径检查；ZIP/tar.xz 还在展开前限制声明体积，
  7z 必须有固定归档 SHA-256；展开后统一拒绝链接/重解析点并核对完整树；
- 在目标同卷的唯一 staging 中构建，完整树通过后才原子改名；
- 已存在且匹配的树只复核；不匹配目标不会被合并或覆盖；
- 安装回执写入 `.tianlai/receipts/`。

若网络端拒绝断点区间或已有 `.part` 损坏，恢复器最多自动从零重试一次。仍失败
时会保留现场并给出明确命令。确认可以重下某个所选资源族时使用：

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl -RestartDownload
```

该开关只删除清单中该归档对应的受控 `.part`，不会删除已验证缓存或正式音源树。

## 目录职责

常见布局：

```text
音源/
├─ 下载缓存/              # 可删除后重下；含断点 .part
├─ 派生/                  # 由冻结参数从上游树确定性生成
├─ .tianlai/receipts/     # 安装与完整树核验回执
├─ VCSL/
├─ FreePats/
├─ Karoryfer/
├─ Emilyguitar/
├─ MTG-Solo-Sax/
├─ VirtualPlayingOrchestra/
├─ 钢琴/
└─ 通用/
```

不要把整个 `音源/` 压进源码发布包。迁移本机资源时，应连同许可证与回执保留；
迁移后的真实状态仍用 `检查运行环境.cmd` 复核。

## 既有专用安装器

根级恢复流程会转调以下固定安装器：

- Salamander Grand Piano：官方固定提交，核验许可证、README、主 SFZ、
  641 个 FLAC 和 668 文件完整树；
- Yamaha CP80：Greg Sullivan E-Pianos 固定提交，核验 81 个 FLAC 与完整证据；
- SIMPK 1793 击弦古钢琴：固定约 1.5 GB 归档、756 个上游 WAV 和调音映射；
- itsclipping ganjo v1.000：CC0，固定提交和 66 文件完整树；
- Virtual Playing Orchestra：固定 Wave 3.2 与 Standard 3.3 官方归档，核验
  合并后的 1,922 文件完整树；
- FluidSynth：项目内 Windows x64 运行库，不要求安装到系统目录。

它们也可以从相应乐器目录的 PowerShell 脚本单独运行。已有树一致时只核验；
任何不一致都不会在原目录上合并。

## SoundFont 兼容边界

GeneralUser GS 与 TimGM 不属于默认、public/trusted 或 103 件专用乐器链路。
只有兼容旧私有工程或测试 SoundFont 后端时，才显式运行：

```powershell
.\安装通用音源.ps1 -InstallLocalCompatibilitySoundFonts
```

GeneralUser GS 的上游说明承认部分样本来源无法完全确认；TimGM 的 GPL-2.0
条款没有明确音乐输出例外。二者只限本机兼容/测试，不能据此发布天籁作品，也
不会互相静默兜底。使用者自行接入的免费或付费 SoundFont 同样由使用者核验许可。

## 许可边界

项目代码采用 Apache-2.0，但第三方采样和工具继续受各自上游许可证约束。安装器
保留许可证、署名和修改证据；公开音频前应检查本次候选生成的
`许可与署名.json/.txt`。

钢弦吉他只从 FreePats 官方包原样本机安装，不由天籁镜像或抽取；MTG Solo Sax
为 CC-BY-4.0，公开输出需要署名；VPO 等 `grandfathered` 资源只允许按冻结官方
包本机恢复，不授权重新分发采样。详细策略见
[`音源许可政策`](../docs/音源许可政策.md)、
[`VPO 音源许可与安装说明`](../docs/VPO音源许可与安装说明.md) 和
[`OUTPUT_RIGHTS.md`](../OUTPUT_RIGHTS.md)。
