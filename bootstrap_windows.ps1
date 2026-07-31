param(
    [switch] $SkipSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# 本脚本由“安装运行环境.cmd”以单个 PowerShell 进程的 ExecutionPolicy
# Bypass 启动。它不会写入或修改用户、系统的持久执行策略。
$utf8 = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$root = $PSScriptRoot
$venvRoot = Join-Path $root ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Program,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]] $Arguments
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program 退出码为 $LASTEXITCODE。"
    }
}

function Get-PythonLauncher {
    $candidates = New-Object System.Collections.Generic.List[hashtable]
    $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        foreach ($selector in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) {
            $candidates.Add(@{
                Label = "py $selector"
                Program = $py.Source
                Prefix = @($selector)
            })
        }
    }
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidates.Add(@{
            Label = "python"
            Program = $python.Source
            Prefix = @()
        })
    }

    $observations = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidates) {
        try {
            $facts = Get-PythonFacts `
                -Program $candidate.Program `
                -Prefix $candidate.Prefix
        } catch {
            $observations.Add("$($candidate.Label)：不可用")
            continue
        }
        if ($facts.Implementation -ne "cpython") {
            $observations.Add(
                "$($candidate.Label)：$($facts.Implementation)，不是 CPython"
            )
            continue
        }
        if ($facts.Version -lt [version]"3.11" -or
            $facts.Version -ge [version]"3.15") {
            $observations.Add(
                "$($candidate.Label)：Python $($facts.Version)，不在已验收的 3.11–3.14 范围"
            )
            continue
        }
        if ($facts.Bits -ne 64) {
            $observations.Add(
                "$($candidate.Label)：$($facts.Bits) 位，不是 64 位"
            )
            continue
        }
        return @{
            Program = $candidate.Program
            Prefix = $candidate.Prefix
            Facts = $facts
        }
    }
    $detail = if ($observations.Count -gt 0) {
        "检测结果：" + ($observations -join "；")
    } else {
        "PATH 中没有 py.exe 或 python.exe。"
    }
    throw (
        "未找到可用的 64 位 CPython 3.11–3.14。$detail" +
        "安装或修复 Python 后，请重新双击安装运行环境.cmd。"
    )
}

function Get-PythonFacts {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Program,
        [string[]] $Prefix = @()
    )
    $facts = & $Program @Prefix -c "import struct,sys; print(sys.implementation.name+'|'+str(sys.version_info.major)+'.'+str(sys.version_info.minor)+'|'+str(struct.calcsize('P')*8))" 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $facts) {
        throw "无法读取 Python 版本与架构。"
    }
    $parts = "$facts".Trim().Split("|")
    if ($parts.Count -ne 3) {
        throw "Python 返回了无法识别的版本信息：$facts"
    }
    return @{
        Implementation = $parts[0]
        Version = [version]$parts[1]
        Bits = [int]$parts[2]
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $root "pyproject.toml"))) {
    throw "当前目录不是完整的天籁源码发布包：缺少 pyproject.toml。"
}
if (-not (Test-Path -LiteralPath (Join-Path $root "乐器\测试工具\参考振荡器\乐器.json"))) {
    throw "当前目录不是完整的天籁源码发布包：缺少参考振荡器。"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-PythonLauncher
    $facts = $launcher.Facts
    if ($facts.Implementation -ne "cpython") {
        throw "天籁当前要求 CPython，检测到 $($facts.Implementation)。"
    }
    if ($facts.Version -lt [version]"3.11" -or
        $facts.Version -ge [version]"3.15") {
        throw "天籁当前验收 Python 3.11–3.14，检测到 $($facts.Version)。"
    }
    if ($facts.Bits -ne 64) {
        throw "天籁要求 64 位 Python，当前检测到 $($facts.Bits) 位。"
    }
    Write-Host "正在创建项目虚拟环境：$venvRoot"
    Invoke-Checked -Program $launcher.Program -Arguments @(
        $launcher.Prefix + @("-m", "venv", $venvRoot)
    )
}

$venvFacts = Get-PythonFacts -Program $venvPython
if ($venvFacts.Implementation -ne "cpython") {
    throw (
        "现有 .venv 使用 $($venvFacts.Implementation)，不是 CPython。" +
        "请移走可重建的 .venv 后重新运行。"
    )
}
if ($venvFacts.Version -lt [version]"3.11" -or
    $venvFacts.Version -ge [version]"3.15") {
    throw (
        "现有 .venv 使用 Python $($venvFacts.Version)，不在已验收的 3.11–3.14 范围。" +
        "请移走可重建的 .venv 后重新运行。"
    )
}
if ($venvFacts.Bits -ne 64) {
    throw "现有 .venv 使用 $($venvFacts.Bits) 位 Python；项目要求 64 位。"
}

Push-Location $root
try {
    # editable 构建只需要项目声明的 setuptools。先把它装进可复用的
    # .venv，再关闭一次性隔离构建环境，避免每次重跑都重复下载构建工具。
    Write-Host "正在检查 Python 构建工具..."
    Invoke-Checked -Program $venvPython -Arguments @(
        "-m",
        "pip",
        "--disable-pip-version-check",
        "install",
        "setuptools>=77"
    )

    Write-Host "正在安装天籁核心与 MCP 入口（不安装大型音源或 FluidSynth）..."
    Invoke-Checked -Program $venvPython -Arguments @(
        "-m",
        "pip",
        "--disable-pip-version-check",
        "install",
        "--no-build-isolation",
        "-e",
        "${root}[mcp]"
    )

    Write-Host "正在检查运行布局、目录与乐器资源状态..."
    Invoke-Checked -Program $venvPython -Arguments @(
        "-m",
        "tianlai.doctor",
        "--start",
        $root
    )

    if (-not $SkipSmoke) {
        $smokeDirectory = Join-Path $root "output\首次出声"
        $smokeWave = Join-Path $smokeDirectory "参考振荡器.wav"
        New-Item -ItemType Directory -Force -Path $smokeDirectory | Out-Null
        Write-Host "正在渲染无需外部音源的参考振荡器..."
        Invoke-Checked -Program $venvPython -Arguments @(
            "-m",
            "tianlai",
            "render",
            "--instrument",
            (Join-Path $root "乐器\测试工具\参考振荡器\乐器.json"),
            "--events",
            (Join-Path $root "examples\c_major.events.json"),
            "--output",
            $smokeWave
        )
        if (-not (Test-Path -LiteralPath $smokeWave)) {
            throw "参考振荡器命令成功返回，但没有生成 WAV：$smokeWave"
        }
        Write-Host "首次出声测试通过：$smokeWave"
    }
} finally {
    Pop-Location
}

Write-Host "天籁最小运行环境已就绪。大型音源可在需要时单独恢复。"
