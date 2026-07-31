# Windows 最小启动

源码发布包根目录下的 `安装运行环境.cmd` 是第一次使用的最小入口。直接
双击，或在命令提示符中运行：

```cmd
安装运行环境.cmd
```

它只在这一个 PowerShell 进程中使用 `ExecutionPolicy Bypass`，不会永久
修改当前用户或系统的执行策略。脚本会：

1. 检查 64 位 CPython 3.11–3.14；
2. 创建或复用项目内的 `.venv`；
3. 以 editable 方式安装核心和 MCP 可选依赖；
4. 运行环境诊断 `python -m tianlai.doctor`；
5. 用项目自带的参考振荡器生成
   `output\首次出声\参考振荡器.wav`。

这个过程不安装 FluidSynth，也不下载任何大型音源。外部采样库缺失会在
环境诊断报告中标记为 `missing`，但不会阻止参考振荡器完成第一次出声。
首次运行仍需要联网安装 Python 核心与 MCP 依赖。

环境诊断的 `ready` 表示运行时目录、实现文件、许可证据和 manifest/SFZ
引用均可解析；为了保持巡检轻量，它不会在每次启动时重新读取并哈希数 GB
音频。音源安装器会使用固定归档摘要，或固定提交加完整树摘要完成资源核验。
大型采样的计划、许可、空间与恢复方式见
[`Windows 安装与巡检`](Windows安装与巡检.md)。

只重建环境并执行环境诊断、不生成测试 WAV 时，可以执行：

```cmd
安装运行环境.cmd -SkipSmoke
```

需要机器可读巡检结果时，在环境安装完成后执行：

```powershell
.\.venv\Scripts\python.exe -m tianlai.doctor --json
```

要求所有正式乐器资源均已恢复并以非零退出码阻止缺失状态时，加上：

```powershell
.\.venv\Scripts\python.exe -m tianlai.doctor --json --require-all-resources
```

SoundFont 只是显式本机兼容功能，不属于最小核心依赖。确需该后端时可单独
安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-soundfont.txt
```
