**简体中文** | [English](Linux快速开始.en.md)

# Linux / WSL 快速开始

本页面向在 Linux 环境中使用 CLI、MCP 或自建 Agent 的开发者。最短路径只安装
Python 运行时与 MCP 依赖，用项目自带的程序音色生成第一份 WAV；它不会自动下载
数 GB 的第三方音源。

## 支持范围

请把下面几层状态分开理解：

| 层级 | 支持情况 | 说明 |
| --- | --- | --- |
| 源码与 portable 自检 | 支持 Ubuntu 22.04+ x86_64、WSL2 x86_64 和 64 位 CPython 3.11–3.14 | 不等于所有第三方采样都已安装 |
| CLI 与 MCP 最小链 | 支持环境建立、环境诊断、程序音色首次出声和 stdio MCP 编辑闭环 | Windows 宿主没有随包提供 WSL 转发桥 |
| 29 件自研程序音色 | 不依赖第三方音频资产，可直接使用 | 其余声音入口需要单独恢复资源 |
| 74 件外部资源 | 环境诊断提供跨平台 Python 恢复入口，`plan` 可解析全部 15 个资源族 | 下载量、上游可用性、许可条件和系统解包依赖因资源而异；大型资源不在 CI 中下载 |

Windows 10/11 x64 仍是 `1.0.0` 的完整参考平台。Linux 已覆盖核心程序音色、
portable 自检、CLI 与 MCP；大型第三方资源的覆盖范围与 Windows 不完全相同。

WSL 用户最好把源码解压或检出到 Linux 文件系统，例如
`/home/alice/src/tianlai`。不要让 Windows 与 Linux 共用同一个 `.venv`；两种
环境的解释器、启动脚本和二进制依赖不能混用。在 `/mnt/c`、`/mnt/d` 等 Windows
挂载盘运行通常也会增加大量小文件读写的耗时。

## 1. 准备系统依赖

天籁在 Linux 上需要 x86_64 宿主与 64 位 CPython 3.11–3.14。Ubuntu 24.04
x86_64 自带的 Python 3.12 在支持范围内；如果当前发行版的 `python3` 仍是 3.10，
请先安装受支持的解释器，再把它的绝对路径传给启动脚本。

不受支持的操作系统、架构或解释器会在创建环境前被拒绝。不要为了天籁替换发行版的
系统 Python；可以使用并列安装、版本管理器或较新的发行版提供受支持解释器。

Ubuntu 24.04，或默认 `python3` 已在支持范围内的 Debian 系发行版，可先安装：

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  libsndfile1 libarchive-tools ca-certificates
python3 --version
```

`libsndfile1` 是 SoundFile 读取和写入音频时的系统级后备依赖。最小程序音色不需要
FluidSynth，也不需要系统音频设备；天籁写出 WAV，试听使用什么播放器由用户决定。
`libarchive-tools` 提供 `bsdtar`，供统一恢复清单中的风笛、西班牙吉他和手风琴
三个 7z 资源族做安全预检与解包；GNU tar 不能替代它。

## 2. 建立环境并第一次出声

进入源码包根目录。直接运行：

```bash
cd /home/alice/src/tianlai
bash ./bootstrap_linux.sh
```

脚本会：

1. 选择受支持的 64 位 Python；
2. 创建项目自己的 `.venv`；
3. 安装天籁核心和 MCP 可选依赖；
4. 运行环境诊断；
5. 用不依赖外部采样的参考振荡器生成
   `output/首次出声/参考振荡器.wav`。

需要指定解释器时：

```bash
bash ./bootstrap_linux.sh --python /usr/bin/python3.12
```

需要把安装与出声拆开检查时：

```bash
bash ./bootstrap_linux.sh --skip-smoke
mkdir -p "$PWD/output/首次出声"
"$PWD/.venv/bin/python" -m tianlai render \
  --instrument "$PWD/乐器/测试工具/参考振荡器/乐器.json" \
  --events "$PWD/examples/c_major.events.json" \
  --output "$PWD/output/首次出声/参考振荡器.wav"
```

这条 render 同时经过事件解析、程序乐器、音频写出和原子发布路径。它证明当前
Python 环境可以运行天籁并产生有效 WAV，但不证明 VPO、VCSL、FreePats 等大型
采样已经可用。

随时可以重新查看本机的真实资源状态：

```bash
"$PWD/.venv/bin/python" -m tianlai.doctor --start "$PWD"
```

缺少外部音源时，环境诊断会报告 `missing`。在 Linux 上，全部 74 件外部资源均
会映射到跨平台 Python 恢复入口。这不表示大型归档已经下载，也不替代使用者对
上游许可和网络可用性的确认。缺失不阻止参考振荡器出声；如果已经出现部分资源、
Hash 不符或清单引用损坏，则应当修复，不能把它当作正常缺失跳过。

## 3. 验证 portable 合同

首次安装时可以让脚本同时安装开发依赖并运行 portable 测试：

```bash
bash ./bootstrap_linux.sh --portable-tests
```

已有环境也可以直接运行：

```bash
"$PWD/.venv/bin/python" -m pip install -e ".[dev,mcp]"
"$PWD/.venv/bin/python" -m pytest -q \
  -m "not external_assets and not listening"
```

这是干净源码包应通过的测试合同。`external_assets` 需要实际第三方音源，
`listening` 需要冻结的试听材料；它们不属于 portable 失败项，也不应被伪装成
portable 已经覆盖。

## 4. 接入 MCP / Agent

下面的配置适用于客户端本身运行在 Linux，或运行在 WSL Remote 环境。`command`
和 `cwd` 必须写真实的 Linux 绝对路径；JSON 不会展开 `~`、`$HOME` 或 shell
变量。

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "/home/alice/src/tianlai/.venv/bin/python",
      "args": ["-m", "tianlai.mcp_entry"],
      "cwd": "/home/alice/src/tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "/home/alice/scores:/mnt/d/shared-scores"
      }
    }
  }
}
```

Linux 用冒号分隔多个 `TIANLAI_INPUT_ROOTS`，Windows 用分号。这里只应加入确实
准备交给 Agent 的谱面目录；相对输入路径仍从 `cwd` 指向的天籁根目录解析。

配置前可先确认入口和依赖可导入：

```bash
"$PWD/.venv/bin/python" -c \
  "import tianlai.mcp_server; print('Tianlai MCP import: OK')"
```

真正的服务使用 stdio。客户端启动后，推荐先调用：

```text
score_and_roster_format()
list_instruments()
```

然后按以下闭环工作：

```text
import_score_project → confirm_roster → validate_project
    → check_project_readiness → render(**render_handoff)
    → locate_rendered_candidate → get_score_slice → patch_score
    → validate_project → check_project_readiness
    → render(parent_candidate_id=..., **render_handoff)
    → compare_rendered_candidates
```

Windows 宿主上的 MCP 客户端不能直接执行
`/home/.../.venv/bin/python`。请让客户端运行在 WSL/Remote 环境，或者使用
Windows `.venv\Scripts\python.exe` 入口。源码包不提供 Windows 到 WSL 的宿主
转发桥。

完整工具、权限与不可变候选规则见 [MCP 接口](MCP.md)。

## 5. 导入自己的 MIDI / MusicXML

CLI 使用相同的 Linux 路径约定：

```bash
"$PWD/.venv/bin/python" -m tianlai project-import \
  --input "/home/alice/scores/demo.musicxml" \
  --output "$PWD/乐谱/曲目/demo/导入-01"
```

导入只生成 score、报告和 `executable=false` 的 roster 草稿，不会凭 MIDI
Program Change 自动取得正式乐器路由。下一步按
[从乐谱到第二次渲染](从乐谱到第二次渲染.md) 显式确认配器、渲染候选、按秒定位
并产生第二个版本。

## 6. 大型音源

先读取恢复计划：

```bash
"$PWD/.venv/bin/python" -m tianlai.resource_restore \
  --home "$PWD" plan
```

该命令不会下载资源，只显示统一恢复清单中的 15 族、74 件入口。确认许可、下载量
和磁盘空间后，可以按计划使用同一模块的 `install` 子命令。7z 资源需要
`bsdtar`（Ubuntu 的 `libarchive-tools`）；缺少依赖时会在下载前给出错误。

恢复器不会在资源缺失或不匹配时静默换用其他音色。Windows 的完整参考流程见
[Windows 安装与巡检](Windows安装与巡检.md)。

## 常见问题

### `No supported 64-bit Python 3.11-3.14 was found`

当前 `python3` 版本不在支持范围，或不是 64 位。安装受支持解释器后显式传入：

```bash
bash ./bootstrap_linux.sh --python /absolute/path/to/python3.12
```

### `could not create .venv`

Debian / Ubuntu 通常缺少对应版本的 `venv` 包。安装与解释器版本匹配的
`python3-venv` 或 `python3.12-venv`，删除不完整的 `.venv` 后重试。

### 报告 `.venv` 是 Windows environment

当前目录已经有 Windows 虚拟环境。不要跨平台复用它；在 WSL 的 Linux 文件系统
中使用另一份源码目录，或先把旧环境移开再创建 Linux `.venv`。

### `soundfile` / `libsndfile` 加载失败

先安装系统库并重新检查环境：

```bash
sudo apt install -y libsndfile1
"$PWD/.venv/bin/python" -c "import soundfile; print(soundfile.__version__)"
```

### MCP 客户端显示服务未连接

依次确认：

1. `command` 指向 Linux `.venv/bin/python` 的绝对路径；
2. `cwd` 是包含 `pyproject.toml`、`乐器/` 和 `可信乐器.json` 的源码根；
3. `args` 使用 `["-m", "tianlai.mcp_entry"]`；
4. 客户端进程确实运行在 Linux/WSL，而不是 Windows 宿主；
5. 直接导入 `tianlai.mcp_server` 时没有依赖错误。

stdio 服务等待客户端握手时没有普通交互界面；不要把“终端里没有提示符”误判成
服务挂起。

### 中文路径或 `/mnt/c` 下很慢

天籁支持 UTF-8 项目路径，但 WSL 挂载的 Windows 文件系统处理大量小文件通常比
Linux 文件系统慢。建议把源码、`.venv` 和频繁写入的输出放在 WSL 的 Linux
文件系统中。若第三方 MCP 客户端自身不能处理中文路径，可使用简短的 ASCII 绝对
路径；无需修改天籁的数据合同。
