param(
    [switch] $InstallLocalCompatibilitySoundFonts
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# 本安装器以 Windows 10/11 x64 + Windows PowerShell 5.1 为第一目标。
# 音源与二进制都固定版本并校验 SHA-256；上游内容变更时宁可明确失败，
# 也不会把未知文件装进项目。
# 默认只安装 FluidSynth 本地运行时与 Python 环境。GeneralUser GS 和 TimGM
# 都不在天籁 public/trusted 许可边界内，只有调用方显式传入
# -InstallLocalCompatibilitySoundFonts 时才用于本机兼容/测试。
$root = $PSScriptRoot
$resourceRoot = Join-Path $root "音源\通用"
$cache = Join-Path $root "音源\下载缓存"

$generalUserVersion = "2.0.3"
$generalUserUrl = "https://github.com/mrbumpy409/GeneralUser-GS/raw/refs/heads/main/GeneralUser-GS.sf2"
$generalUserSha256 = "9575028C7A1F589F5770FCCC8CFF2734566AF40CD26ED836944E9A5152688CFE"
$generalUserLicenseUrl = "https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/main/documentation/LICENSE.txt"
$generalUserLicenseSha256 = "7B32EFEFDF95CE38A043799F0659853DDC00FBAA14D8C50F0ACA16B9B8B405BE"

$timGmVersion = "1.3"
$timGmArchiveName = "timgm6mb-soundfont_1.3.orig.tar.gz"
$timGmUrl = "https://deb.debian.org/debian/pool/main/t/timgm6mb-soundfont/$timGmArchiveName"
$timGmArchiveSha256 = "AF8F3A00E416DFB262BCAA904A1C84DF04A51B72BBC1313AED012BC754BDF99B"
$timGmSoundFontSha256 = "C5378B62028C920CB11E4803327983FEE2F2CDFF5DC89C708E39DA417E51C854"
$timGmCopyrightUrl = "https://sources.debian.org/data/main/t/timgm6mb-soundfont/1.3-5/debian/copyright"

$fluidSynthVersion = "2.5.6"
$fluidSynthArchiveName = "fluidsynth-v2.5.6-win10-x64-cpp11.zip"
$fluidSynthUrl = "https://github.com/FluidSynth/fluidsynth/releases/download/v2.5.6/$fluidSynthArchiveName"
$fluidSynthArchiveSha256 = "A4B8BD4F133B7B6770537F6C18B2B2B93579338D51E26F777D025E40E15A7E81"
$fluidSynthLicenseUrl = "https://raw.githubusercontent.com/FluidSynth/fluidsynth/v2.5.6/LICENSE"

$generalUser = Join-Path $resourceRoot "GeneralUser-GS.sf2"
$generalUserLicense = Join-Path $resourceRoot "GeneralUser-GS-LICENSE.txt"
$timLocalCompatibility = Join-Path $resourceRoot "TimGM6mb.sf2"
$timCopyright = Join-Path $resourceRoot "TimGM6mb-COPYRIGHT.txt"
$fluidRoot = Join-Path $resourceRoot "fluidsynth"

New-Item -ItemType Directory -Force -Path $resourceRoot, $cache | Out-Null

function Get-Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Receive-File([string] $Url, [string] $Output) {
    if (Test-Path -LiteralPath $Output) {
        Remove-Item -LiteralPath $Output -Force
    }

    $curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if ($null -ne $curl) {
        # Windows 10 较早随附的 curl 不认识 --retry-all-errors，保持参数兼容。
        & $curl.Source --http1.1 -L --fail --retry 8 --retry-delay 5 -o $Output $Url
        if ($LASTEXITCODE -ne 0) {
            throw "下载失败（curl 退出码 $LASTEXITCODE）：$Url"
        }
        return
    }

    # Windows PowerShell 5.1 默认协议可能过旧，显式启用 TLS 1.2。
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Output
}

function Move-FileAtomically([string] $Part, [string] $Destination) {
    if (Test-Path -LiteralPath $Destination) {
        [IO.File]::Replace($Part, $Destination, $null)
    } else {
        [IO.File]::Move($Part, $Destination)
    }
}

function Get-VerifiedFile(
    [string] $Name,
    [string] $Url,
    [string] $Destination,
    [string] $ExpectedSha256
) {
    $expected = $ExpectedSha256.ToUpperInvariant()
    if (Test-Path -LiteralPath $Destination) {
        $installedHash = Get-Sha256 $Destination
        if ($installedHash -eq $expected) {
            Write-Host "$Name 已存在且校验通过。"
            return
        }
        Write-Warning "$Name 的现有文件校验不符，将在新文件验证通过后原子替换。"
    }

    $part = "$Destination.part"
    try {
        Receive-File $Url $part
        $actual = Get-Sha256 $part
        if ($actual -ne $expected) {
            throw "$Name SHA-256 不匹配。期望 $expected，实际 $actual。"
        }
        Move-FileAtomically $part $Destination
    } finally {
        if (Test-Path -LiteralPath $part) {
            Remove-Item -LiteralPath $part -Force
        }
    }
}

function Get-PinnedLicenseFile(
    [string] $Name,
    [string] $Url,
    [string] $Destination
) {
    # 许可证 URL 已固定到相应上游版本。上游未发布独立许可证摘要，因此不虚构
    # SHA-256；音源与可执行二进制本体仍必须通过上面的固定摘要校验。
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-Item -LiteralPath $Destination).Length -gt 100) {
            return
        }
    }
    $part = "$Destination.part"
    try {
        Receive-File $Url $part
        if ((Get-Item -LiteralPath $part).Length -lt 100) {
            throw "$Name 许可证文件异常短，已拒绝安装。"
        }
        Move-FileAtomically $part $Destination
    } finally {
        if (Test-Path -LiteralPath $part) {
            Remove-Item -LiteralPath $part -Force
        }
    }
}

function Install-GeneralUser {
    Get-VerifiedFile "GeneralUser GS LICENSE" $generalUserLicenseUrl $generalUserLicense $generalUserLicenseSha256
    Get-VerifiedFile "GeneralUser GS $generalUserVersion" $generalUserUrl $generalUser $generalUserSha256
}

function Install-TimGmLocalCompatibility {
    $archive = Join-Path $cache $timGmArchiveName
    Get-VerifiedFile "TimGM $timGmVersion 源码包" $timGmUrl $archive $timGmArchiveSha256

    if ((Test-Path -LiteralPath $timLocalCompatibility) -and ((Get-Sha256 $timLocalCompatibility) -eq $timGmSoundFontSha256)) {
        Write-Host "TimGM $timGmVersion 已存在且校验通过。"
    } else {
        $extract = Join-Path $cache "timgm6mb-1.3-extracted"
        if (Test-Path -LiteralPath $extract) {
            Remove-Item -LiteralPath $extract -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $extract | Out-Null

        $tar = Get-Command "tar.exe" -ErrorAction SilentlyContinue
        if ($null -eq $tar) {
            throw "未找到 Windows 自带的 tar.exe，无法解压 TimGM 本机兼容音源。"
        }
        & $tar.Source -xf $archive -C $extract
        if ($LASTEXITCODE -ne 0) {
            throw "无法解压 TimGM 源码包（tar 退出码 $LASTEXITCODE）：$archive"
        }
        $found = Get-ChildItem -LiteralPath $extract -Recurse -Filter "TimGM6mb.sf2" | Select-Object -First 1
        if ($null -eq $found) {
            throw "TimGM 源码包中未找到 TimGM6mb.sf2。"
        }
        $actual = Get-Sha256 $found.FullName
        if ($actual -ne $timGmSoundFontSha256) {
            throw "解出的 TimGM6mb.sf2 SHA-256 不匹配。期望 $timGmSoundFontSha256，实际 $actual。"
        }
        $part = "$timLocalCompatibility.part"
        try {
            Copy-Item -LiteralPath $found.FullName -Destination $part -Force
            if ((Get-Sha256 $part) -ne $timGmSoundFontSha256) {
                throw "复制 TimGM6mb.sf2 后二次校验失败。"
            }
            Move-FileAtomically $part $timLocalCompatibility
        } finally {
            if (Test-Path -LiteralPath $part) {
                Remove-Item -LiteralPath $part -Force
            }
        }
    }

    Get-PinnedLicenseFile "TimGM $timGmVersion" $timGmCopyrightUrl $timCopyright
}

function Install-FluidSynth {
    $archive = Join-Path $cache $fluidSynthArchiveName
    Get-VerifiedFile "FluidSynth $fluidSynthVersion Windows x64" $fluidSynthUrl $archive $fluidSynthArchiveSha256

    $versionMarker = Join-Path $fluidRoot ".tianlai-version"
    $dll = Join-Path $fluidRoot "bin\libfluidsynth-3.dll"
    $installedVersion = ""
    if (Test-Path -LiteralPath $versionMarker) {
        $installedVersion = (Get-Content -LiteralPath $versionMarker -Raw).Trim()
    }
    $installedLicense = Join-Path $fluidRoot "LICENSE"
    if (
        (Test-Path -LiteralPath $dll) -and
        (Test-Path -LiteralPath $installedLicense) -and
        ((Get-Item -LiteralPath $installedLicense).Length -gt 100) -and
        ($installedVersion -eq $fluidSynthVersion)
    ) {
        Write-Host "FluidSynth $fluidSynthVersion 已存在。"
        return
    }

    $extract = Join-Path $cache "fluidsynth-$fluidSynthVersion-extracted"
    if (Test-Path -LiteralPath $extract) {
        Remove-Item -LiteralPath $extract -Recurse -Force
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force

    $packageDll = Get-ChildItem -LiteralPath $extract -Recurse -Filter "libfluidsynth-3.dll" | Select-Object -First 1
    if ($null -eq $packageDll) {
        throw "FluidSynth $fluidSynthVersion 压缩包中未找到 libfluidsynth-3.dll。"
    }
    $packageRoot = $packageDll.Directory.Parent.FullName
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "bin\fluidsynth.exe"))) {
        throw "FluidSynth $fluidSynthVersion 压缩包目录结构异常。"
    }

    $stage = Join-Path $resourceRoot ("fluidsynth.installing." + $PID)
    $previous = Join-Path $resourceRoot ("fluidsynth.previous." + $PID)
    foreach ($stale in @($stage, $previous)) {
        if (Test-Path -LiteralPath $stale) {
            Remove-Item -LiteralPath $stale -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -Path (Join-Path $packageRoot "*") -Destination $stage -Recurse -Force
    Get-PinnedLicenseFile "FluidSynth $fluidSynthVersion" $fluidSynthLicenseUrl (Join-Path $stage "LICENSE")
    [IO.File]::WriteAllText((Join-Path $stage ".tianlai-version"), "$fluidSynthVersion`r`n", (New-Object Text.UTF8Encoding($false)))

    try {
        if (Test-Path -LiteralPath $fluidRoot) {
            Move-Item -LiteralPath $fluidRoot -Destination $previous
        }
        Move-Item -LiteralPath $stage -Destination $fluidRoot
        if (Test-Path -LiteralPath $previous) {
            Remove-Item -LiteralPath $previous -Recurse -Force
        }
    } catch {
        if ((-not (Test-Path -LiteralPath $fluidRoot)) -and (Test-Path -LiteralPath $previous)) {
            Move-Item -LiteralPath $previous -Destination $fluidRoot
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force
        }
    }
}

function Ensure-PythonEnvironment {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -ne $py) {
            $version = & $py.Source -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]"3.11") {
                throw "天籁要求 Python 3.11 或更高版本，py -3 当前选择的是 $version。"
            }
            & $py.Source -3 -m venv (Join-Path $root ".venv")
        } else {
            $systemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
            if ($null -eq $systemPython) {
                throw "未找到 Python 3。请先安装 Python 3.11 或更高版本。"
            }
            $version = & $systemPython.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]"3.11") {
                throw "天籁要求 Python 3.11 或更高版本，当前 python.exe 为 $version。"
            }
            & $systemPython.Source -m venv (Join-Path $root ".venv")
        }
        if ($LASTEXITCODE -ne 0) {
            throw "创建项目虚拟环境失败。"
        }
    }
    $venvVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or [version]$venvVersion -lt [version]"3.11") {
        throw "现有 .venv 使用 Python $venvVersion，但天籁要求 3.11 或更高版本。请删除该可重建虚拟环境后重新运行安装器。"
    }
    & $python -m pip install -r (Join-Path $root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Python 依赖安装失败。"
    }
}

$generalUserReady = $false
$timGmReady = $false
$fluidSynthReady = $false
$problems = New-Object System.Collections.Generic.List[string]

if ($InstallLocalCompatibilitySoundFonts) {
    Write-Warning (
        "正在显式安装仅限本机兼容/测试的 SoundFont。GeneralUser GS 上游说明承认" +
        "部分样本来源无法完全确认；TimGM 的 GPL-2.0 条款没有明确的渲染音频输出例外。" +
        "两者都不得进入天籁 public/trusted 发布链路，也不会互相静默兜底。"
    )
    try {
        Install-GeneralUser
        $generalUserReady = $true
    } catch {
        $problems.Add("GeneralUser GS $generalUserVersion 本机兼容安装失败：$($_.Exception.Message)")
        Write-Warning $problems[$problems.Count - 1]
    }

    try {
        Install-TimGmLocalCompatibility
        $timGmReady = $true
    } catch {
        $problems.Add("TimGM $timGmVersion 本机兼容安装失败：$($_.Exception.Message)")
        Write-Warning $problems[$problems.Count - 1]
    }
} else {
    Write-Host (
        "已跳过 GeneralUser GS 与 TimGM；它们不是默认资源。" +
        "如只为私有本机兼容/测试，可显式传入 -InstallLocalCompatibilitySoundFonts。"
    )
}

try {
    Install-FluidSynth
    $fluidSynthReady = $true
} catch {
    $problems.Add("FluidSynth $fluidSynthVersion 安装失败：$($_.Exception.Message)")
    Write-Warning $problems[$problems.Count - 1]
}

if (-not $fluidSynthReady) {
    throw "FluidSynth 本地运行时安装失败。`r`n - $($problems -join "`r`n - ")"
}
if (
    $InstallLocalCompatibilitySoundFonts -and
    -not ($generalUserReady -or $timGmReady)
) {
    throw "显式请求的两个本机兼容 SoundFont 均未安装成功。`r`n - $($problems -join "`r`n - ")"
}

Ensure-PythonEnvironment

if ($generalUserReady) {
    Write-Warning "GeneralUser GS 本机兼容副本已就绪（不进入 public/trusted）：$generalUser"
}
if ($timGmReady) {
    Write-Warning "TimGM 本机兼容副本已就绪（不进入 public/trusted）：$timLocalCompatibility"
}
Write-Host "FluidSynth $fluidSynthVersion 已就绪：$fluidRoot"
Write-Host "本机 SoundFont 巡检也必须显式传入：tools\批量巡检乐器.py --soundfont <路径>"
