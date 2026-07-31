param(
    [string[]] $ResourceFamily = @(),
    [string[]] $ResourceGroup = @(),
    [switch] $RestorableOnly,
    [switch] $LegacyOnly,
    [switch] $PlanOnly,
    [switch] $RestartDownload,
    [switch] $Yes
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$utf8 = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$root = $PSScriptRoot
$resourceRoot = Join-Path $root "音源"
$restoreManifest = Join-Path $root "resource_restore_manifest.json"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if ($env:TIANLAI_RESTORABLE_ONLY -eq "1") {
    $RestorableOnly = $true
}
if ($RestorableOnly -and $LegacyOnly) {
    throw "-RestorableOnly 与 -LegacyOnly 不能同时使用。"
}
if ($ResourceFamily.Count -gt 0 -or $ResourceGroup.Count -gt 0) {
    $RestorableOnly = $true
}
if (-not (Test-Path -LiteralPath $restoreManifest -PathType Leaf)) {
    throw "缺少 Git 管理的音源恢复清单：$restoreManifest"
}

function Expand-Selection([string[]] $Values) {
    $expanded = New-Object System.Collections.Generic.List[string]
    foreach ($value in $Values) {
        foreach ($item in "$value".Split(",")) {
            $trimmed = $item.Trim()
            if ($trimmed.Length -gt 0 -and -not $expanded.Contains($trimmed)) {
                $expanded.Add($trimmed)
            }
        }
    }
    return $expanded.ToArray()
}

function Format-ByteSize([long] $Bytes) {
    if ($Bytes -ge 1TB) {
        return "{0:N2} TiB" -f ($Bytes / 1TB)
    }
    if ($Bytes -ge 1GB) {
        return "{0:N2} GiB" -f ($Bytes / 1GB)
    }
    if ($Bytes -ge 1MB) {
        return "{0:N2} MiB" -f ($Bytes / 1MB)
    }
    if ($Bytes -ge 1KB) {
        return "{0:N2} KiB" -f ($Bytes / 1KB)
    }
    return "$Bytes B"
}

$manifest = Get-Content -LiteralPath $restoreManifest -Raw -Encoding UTF8 |
    ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or
    $manifest.kind -ne "tianlai.resource_restore_manifest") {
    throw "无法识别的音源恢复清单版本。"
}

$requestedFamilies = @(Expand-Selection $ResourceFamily)
$requestedGroups = @(Expand-Selection $ResourceGroup)
$knownFamilies = @($manifest.families | ForEach-Object { $_.id })
$knownGroups = @(
    $manifest.families |
        ForEach-Object { $_.group } |
        Select-Object -Unique
)
foreach ($familyId in $requestedFamilies) {
    if ($knownFamilies -notcontains $familyId) {
        throw "未知资源族：$familyId。可用值：$($knownFamilies -join ', ')"
    }
}
foreach ($groupId in $requestedGroups) {
    if ($knownGroups -notcontains $groupId) {
        throw "未知资源组：$groupId。可用值：$($knownGroups -join ', ')"
    }
}

$selectedFamilies = @(
    if ($LegacyOnly) {
        @()
    } elseif ($requestedFamilies.Count -eq 0 -and
        $requestedGroups.Count -eq 0) {
        $manifest.families
    } else {
        $manifest.families | Where-Object {
            $requestedFamilies -contains $_.id -or
            $requestedGroups -contains $_.group
        }
    }
)

[long] $missingDownloadBytes = 0
[long] $missingInstalledBytes = 0
[int] $selectedInstrumentCount = 0
Write-Host "Git 管理的可恢复资源计划："
foreach ($family in $selectedFamilies) {
    $selectedInstrumentCount += @($family.instrument_ids).Count
    $target = Join-Path $resourceRoot (
        "$($family.install.target)".Replace("/", "\")
    )
    $archive = Join-Path (
        Join-Path $resourceRoot "下载缓存"
    ) "$($family.archive.filename)"
    $state = if (Test-Path -LiteralPath $target -PathType Container) {
        "已存在，安装时完整复核"
    } elseif (Test-Path -LiteralPath $target) {
        "冲突：目标不是目录"
    } else {
        "缺失"
    }
    if ($state -eq "缺失") {
        $missingInstalledBytes += [long]$family.install.tree.bytes
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            $missingDownloadBytes += [long]$family.archive.estimated_bytes
        }
    }
    foreach ($derived in @($family.install.derived)) {
        $derivedTarget = Join-Path $resourceRoot (
            "$($derived.target)".Replace("/", "\")
        )
        if (-not (Test-Path -LiteralPath $derivedTarget -PathType Container)) {
            $missingInstalledBytes += [long]$derived.tree.bytes
        }
    }
    Write-Host (
        "  - {0,-28} {1,2} 件  {2}  {3}" -f
        $family.id,
        @($family.instrument_ids).Count,
        $family.license.expression,
        $state
    )
}
Write-Host (
    (
        "本轮资源族：{0} 组 / {1} 件乐器；预计仍需下载 {2}，" +
        "新增磁盘占用约 {3}。"
    ) -f
    $selectedFamilies.Count,
    $selectedInstrumentCount,
    (Format-ByteSize $missingDownloadBytes),
    (Format-ByteSize $missingInstalledBytes)
)
Write-Host (
    (
        "完整 38 件缺口的冻结上限为下载 {0}、安装后 {1}；" +
        "建议至少保留 {2} 空间用于归档和同卷 staging。"
    ) -f
    (Format-ByteSize ([long]$manifest.totals.estimated_download_bytes)),
    (Format-ByteSize (
        [long]$manifest.totals.installed_bytes_including_derived
    )),
    (Format-ByteSize ([long]$manifest.totals.recommended_free_bytes))
)
if (-not $RestorableOnly -and -not $LegacyOnly) {
    Write-Host (
        "此外还会复核或安装既有资源：FluidSynth、Salamander 钢琴、CP80、" +
        "SIMPK 击弦古钢琴、班卓琴与 VPO；它们不计入上面的 38 件统计。"
    )
}
if ($LegacyOnly) {
    Write-Host "本轮只运行既有资源安装器，不处理上述 38 件资源族。"
}
Write-Host (
    "钢弦吉他只从 FreePats 官方地址原样安装，不镜像/重打包；" +
    "MTG Solo Sax 使用 CC-BY-4.0，公开输出时需保留署名。"
)

if ($PlanOnly) {
    Write-Host "只读计划完成；未下载、解压或修改任何音源。"
    exit 0
}

if (-not $Yes) {
    $answer = Read-Host "确认后请输入 INSTALL（其他输入取消）"
    if ($answer -cne "INSTALL") {
        throw "用户取消安装。"
    }
}

if (-not $RestorableOnly) {
    Write-Host "开始安装项目本地 FluidSynth（不默认安装许可未进入公开边界的通用 SoundFont）..."
    & (Join-Path $root "安装通用音源.ps1")

    Write-Host "开始安装高拟真钢琴（Salamander Grand Piano）..."
    & (Join-Path $root "乐器\键盘乐器\钢琴\获取音源.ps1")

    Write-Host "开始安装 Yamaha CP80 电钢琴（Greg Sullivan E-Pianos）..."
    & (Join-Path $root "乐器\键盘乐器\获取GregSullivan电钢琴音源.ps1")

    Write-Host "开始安装 SIMPK 1793 击弦古钢琴实录（约 1.5 GB 下载）..."
    & (Join-Path $root "乐器\键盘乐器\击弦古钢琴\获取音源.ps1")

    Write-Host "开始安装 CC0 ganjo 班卓琴..."
    & (Join-Path $root "乐器\世界乐器\班卓琴\获取音源.ps1")

    Write-Host "开始安装高拟真管弦音源（Virtual Playing Orchestra，体积较大）..."
    & (Join-Path $root "安装VPO音源.ps1")
}

if ($selectedFamilies.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Host "尚无项目虚拟环境，先安装最小运行环境（不下载大型音源）..."
        & (Join-Path $root "bootstrap_windows.ps1") -SkipSmoke
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "最小运行环境完成后仍未找到 Python：$venvPython"
    }
    $restoreArguments = New-Object System.Collections.Generic.List[string]
    foreach ($item in @(
        "-m",
        "tianlai.resource_restore",
        "--home",
        $root,
        "install",
        "--yes"
    )) {
        $restoreArguments.Add($item)
    }
    foreach ($familyId in $requestedFamilies) {
        $restoreArguments.Add("--family")
        $restoreArguments.Add($familyId)
    }
    foreach ($groupId in $requestedGroups) {
        $restoreArguments.Add("--group")
        $restoreArguments.Add($groupId)
    }
    if ($RestartDownload) {
        $restoreArguments.Add("--restart-download")
    }
    & $venvPython $restoreArguments.ToArray()
    if ($LASTEXITCODE -ne 0) {
        throw "统一音源恢复器退出码为 $LASTEXITCODE。"
    }
}

Write-Host (
    "所选音源安装与完整树核验完成。GeneralUser GS 与 TimGM 未进入默认/" +
    "public/trusted 链路；正式乐器继续走各自专用实现。"
)
