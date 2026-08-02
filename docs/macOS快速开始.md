**简体中文** | [English](macOS快速开始.en.md)

# macOS 快速开始

本页面向在 Apple Silicon 或 Intel Mac 上使用天籁 CLI、MCP 或自建 Agent 的用户。
随包的 `bootstrap_macos.sh` 会建立独立 Python 环境、运行诊断并用参考振荡器生成
第一份 WAV；它不会自动下载数 GB 第三方音源，也不会把 FluidSynth 安装到系统。

> **当前验收状态：** 配置中的 macOS portable 门禁在 macOS 15 上覆盖原生 `arm64` 与
> `x86_64` 的完整 CPython 3.11–3.14 矩阵，并在 macOS 26 上复验两种架构的
> CPython 3.14。标签门禁还会让两代系统、两种架构校验同一候选 ZIP；所有任务都
> 从带 Unicode 与空格的解压路径执行启动、测试和首次出声。门禁合入后仍以
> GitHub Actions 的实际结果为准；大型第三方资源不因 portable 通过就自动获得
> 全量实机验收。

## 支持边界

| 层级 | macOS 支持情况 | 说明 |
| --- | --- | --- |
| 源码与 portable 自检 | Apple Silicon `arm64`、Intel `x86_64`，64 位 CPython 3.11–3.14 | Python 架构必须与当前原生宿主一致 |
| CLI 与 MCP 最小链 | 环境建立、doctor、参考振荡器首次出声、stdio MCP 与 portable tests | 离线写 WAV，不要求系统音频设备 |
| 29 件自研程序音色 | 不依赖第三方音频资产，可直接使用 | 其余声音入口需要单独恢复资源 |
| 74 件外部资源 | `resource_restore plan` 可解析全部 15 个资源族，安装器为跨平台 Python 入口；Mac 门禁强制验证 bsdtar / libarchive | 下载、上游可用性、许可条件、空间和大型资源本体仍需本机确认；大型资源不在 CI 中下载 |
| FluidSynth / SoundFont | 可选兼容层，可发现并实际加载系统/Homebrew 库或显式 `.dylib` 路径 | doctor 只有在动态库成功加载且必需导出存在时才标记原生层可用；不是核心首次出声依赖，当前正式入口也不靠 SoundFont 兜底 |

macOS 支持不等于 103 件入口的全部第三方音源已经在每种 Mac 上完成真实采样
渲染。使用某件外部资源前，仍以 `tianlai-doctor` 的本机结果及相应许可证据为准。

## 1. 准备原生 Python

需要 64 位 CPython 3.11–3.14，且解释器架构与 `uname -m` 一致。可以使用
[python.org 官方安装包](https://www.python.org/downloads/macos/) 或 Homebrew：

```bash
brew install python@3.13
```

先检查宿主和解释器：

```bash
uname -s
uname -m
/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true
python3 -c 'import platform, struct; print(platform.system(), platform.machine(), struct.calcsize("P") * 8)'
```

`uname -s` 应为 `Darwin`；常见架构是 Apple Silicon 的 `arm64` 或 Intel 的
`x86_64`；`sysctl.proc_translated` 不得输出 `1`。不要让原生 arm64 工程复用
x86_64/Rosetta 虚拟环境，反之亦然。启动脚本会在创建 `.venv` 前拒绝 Rosetta
翻译态，以及不受支持的版本、实现、位数、操作系统或架构。

## 2. 建立环境并第一次出声

把源码解压或检出到一个普通用户可写目录，然后运行：

```bash
cd "/Users/alice/Projects/Tianlai"
bash ./bootstrap_macos.sh
```

脚本会：

1. 查找原生 64 位 CPython 3.11–3.14；
2. 创建或复用项目自己的 `.venv`；
3. 安装天籁核心和 MCP 可选依赖；
4. 运行 `python -m tianlai.doctor --start <源码根>`；
5. 用参考振荡器生成 `output/首次出声/参考振荡器.wav` 并检查 WAV 元数据。

第一次安装 Python 包需要网络。脚本自身不使用 `sudo`，不修改 shell 配置，不安装
系统级 FluidSynth，也不下载大型音源。

指定解释器：

```bash
bash ./bootstrap_macos.sh --python /opt/homebrew/bin/python3.13
```

Intel Homebrew 通常位于 `/usr/local`，不要机械复制 Apple Silicon 的
`/opt/homebrew` 路径；以 `command -v python3.13` 的实际结果为准。

只安装并运行诊断、不生成测试 WAV：

```bash
bash ./bootstrap_macos.sh --skip-smoke
```

查看全部选项：

```bash
bash ./bootstrap_macos.sh --help
```

## 3. 运行 portable 门禁

首次建立环境时可同时安装开发依赖并运行无需外部采样的完整 portable 测试：

```bash
bash ./bootstrap_macos.sh --portable-tests
```

已有环境可直接运行：

```bash
"$PWD/.venv/bin/python" -m pip install -e ".[dev,mcp]"
"$PWD/.venv/bin/python" -m pytest -q \
  -m "not external_assets and not listening"
```

`external_assets` 需要实际第三方音源，`listening` 需要冻结试听材料；二者是独立
验收层。portable 通过不能证明所有采样、奏法和作品已经听审通过。

## 4. CLI 与 MCP

CLI 使用项目虚拟环境：

```bash
"$PWD/.venv/bin/python" -m tianlai --help
"$PWD/.venv/bin/python" -m tianlai.doctor --start "$PWD" --quick
```

macOS MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "/Users/alice/Projects/Tianlai/.venv/bin/python",
      "args": ["-m", "tianlai.mcp_entry"],
      "cwd": "/Users/alice/Projects/Tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "/Users/alice/Music/Scores:/Volumes/Shared/Scores"
      }
    }
  }
}
```

`command` 和 `cwd` 必须使用真实绝对路径；JSON 不展开 `~`、`$HOME` 或 shell
变量。macOS 与 Linux 一样用冒号分隔多个 `TIANLAI_INPUT_ROOTS`。只加入确实
准备交给 Agent 的谱面目录。完整工具与权限边界见 [MCP 接口](MCP.md)。

## 5. 外部音源恢复

先读取不会下载内容的计划：

```bash
"$PWD/.venv/bin/python" -m tianlai.resource_restore \
  --home "$PWD" plan
```

macOS 可以解析统一清单中的 15 个资源族、74 件入口。确认许可证、下载量、磁盘
空间和本机依赖后，再按计划运行同一模块的 `install` 子命令。大型第三方音源不
进入源码包，也不在普通 CI 中下载；缺失或不匹配时不会静默替换成其他音色。

部分统一资源使用 7z。恢复器只接受 `bsdtar` 能力，不会把 GNU tar 当成等价实现。
macOS 系统 `tar` 若实际为 libarchive/bsdtar 可以直接使用；若探测失败，可安装
Homebrew libarchive 并把它的 `bin` 临时加入当前终端：

```bash
brew install libarchive
export PATH="$(brew --prefix libarchive)/bin:$PATH"
bsdtar --version
```

源码包不携带大型采样。安装完成后仍要重新运行 doctor；部分安装、Hash 不符或
许可证据缺失必须修复，不能当成正常 `missing` 跳过。

## 6. 可选 FluidSynth / SoundFont

核心程序音色和首次出声不需要 FluidSynth。确需测试 SoundFont 兼容后端时，可
单独安装：

```bash
brew install fluid-synth
"$PWD/.venv/bin/python" -m pip install -r requirements-soundfont.txt
```

运行时可发现系统或 Homebrew 的 FluidSynth；也支持项目本地目录或
`TIANLAI_FLUIDSYNTH_DIR` 指向目录内的 `.dylib`，并以规范绝对路径绑定实际库身份。
不要把不明来源动态库放进搜索目录。GeneralUser GS、TimGM 及用户自行提供的
SoundFont 仍只是显式本机兼容/测试材料，不属于默认 public/trusted 路由。

## 常见问题

### `Rosetta translation is active`

当前终端处于 Rosetta 翻译态。退出该终端，从 Finder 中确认 Terminal/iTerm 未勾选
“使用 Rosetta 打开”，再打开原生终端。不要复用翻译态创建的 `.venv`；请使用新的
源码目录，或先把旧 `.venv` 移到项目外再重新运行启动脚本。

### `No supported native 64-bit Python 3.11-3.14 was found`

安装受支持的原生 CPython，并显式传入真实路径：

```bash
bash ./bootstrap_macos.sh --python "$(command -v python3.13)"
```

### 报告解释器架构与宿主不一致

比较：

```bash
uname -m
"/path/to/python" -c 'import platform; print(platform.machine())'
```

两个结果必须一致。退出 Rosetta 终端或选择匹配架构的 Python 后，使用新的源码
目录，或把不适用的 `.venv` 移开再重建。

### 报告 `.venv` 来自 Windows / Linux 或不完整

虚拟环境不能跨操作系统、架构或源码快照共用。不要覆盖其中的解释器；使用单独
检出目录，或先把旧 `.venv` 移开，再重新运行启动脚本。

### `soundfile` 或首次 WAV 检查失败

先确认 Python 环境与依赖没有混合架构：

```bash
"$PWD/.venv/bin/python" -m pip check
"$PWD/.venv/bin/python" -c 'import platform, soundfile; print(platform.machine(), soundfile.__version__)'
```

若使用了自行编译的动态库，暂时移除相关 `DYLD_*` 覆盖并重新建立干净 `.venv`。

### MCP 客户端无法连接

确认 `command` 是 `.venv/bin/python` 的绝对路径，`cwd` 是源码根，参数为
`["-m", "tianlai.mcp_entry"]`。stdio 服务等待客户端握手时没有普通交互提示，
终端无提示符不表示服务挂起。

## 许可与发布边界

macOS 兼容性不会改变任何许可证。项目代码仍为 Apache-2.0；第三方音源、输入作品
和输出音乐保持各自权利状态。恢复器只从冻结公开上游安装到用户本机，不能据此
镜像或重打包采样。发布音乐前检查 `许可与署名.json/.txt`、输入作品权利和上游
条款；详见 [音源许可政策](音源许可政策.md) 与
[输出权利说明](../OUTPUT_RIGHTS.md)。
