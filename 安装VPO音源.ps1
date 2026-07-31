$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Virtual Playing Orchestra 的官方安装方式是把 Wave 3.2 与 Standard 3.3
# 解到同一目录。天籁不镜像或改写该音源，只从上游地址取得固定归档，在临时
# 目录验证完整合并树后原子安装到 音源/。
$root = $PSScriptRoot
$resourceRoot = [IO.Path]::GetFullPath((Join-Path $root "音源"))
$cache = Join-Path $resourceRoot "下载缓存"
$destination = Join-Path $resourceRoot "VirtualPlayingOrchestra"

$waveArchiveName = "Virtual-Playing-Orchestra3-2-wave-files.zip"
$waveUrl = "https://virtualplaying.com/go/virtual-playing-orchestra-v3-2-wave-files-archive/"
$waveBytes = 616114842
$waveSha256 = "CA8F1E0B56EEDE35314994646E5F1F307EC349616C967FBECF627C43AA646E90"

$scriptArchiveName = "Virtual-Playing-Orchestra3-3-standard-scripts.zip"
$scriptUrl = "https://virtualplaying.com/go/virtual-playing-orchestra-v3-3-standard-scripts/"
$scriptBytes = 544010
$scriptSha256 = "F0F2BF0E42D2A39C5F49401ADDCFFA840FD8F5525670F5945BF5093A5442BDA5"

$expectedTreeDirectory = "Virtual-Playing-Orchestra3"
$expectedTreeFileCount = 1922
$expectedTreeBytes = 724695982
$expectedTreeSha256 = "B06390C70D9D701481BC6DB0CF13B6ED6F3EF6B660DAC9A51034B9BE368DF317"
$expectedLicenseSha256 = "852E3BE507B193625EAF76BD18F4740209287781FB95F2A06D78AE9205D4682E"

New-Item -ItemType Directory -Force -Path $resourceRoot, $cache | Out-Null

function Get-Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Assert-ManagedDirectChild(
    [string] $Path,
    [string] $Parent,
    [string] $Label
) {
    $full = [IO.Path]::GetFullPath($Path)
    $expectedParent = [IO.Path]::GetFullPath($Parent)
    $actualParent = [IO.Path]::GetDirectoryName($full)
    if (-not [string]::Equals(
        $actualParent,
        $expectedParent,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$Label 必须是受管资源目录的直接子项：$full"
    }
    return $full
}

function Receive-File([string] $Url, [string] $Output) {
    if (Test-Path -LiteralPath $Output) {
        Remove-Item -LiteralPath $Output -Force
    }

    $curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if ($null -ne $curl) {
        # 兼容 Windows 10 较早随附的 curl，不使用 --retry-all-errors。
        & $curl.Source --http1.1 -L --fail --retry 8 --retry-delay 5 -o $Output $Url
        if ($LASTEXITCODE -ne 0) {
            throw "下载失败（curl 退出码 $LASTEXITCODE）：$Url"
        }
        return
    }

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

function Get-VerifiedArchive(
    [string] $Name,
    [string] $Url,
    [string] $Destination,
    [long] $ExpectedBytes,
    [string] $ExpectedSha256
) {
    $expectedHash = $ExpectedSha256.ToUpperInvariant()
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $item = Get-Item -LiteralPath $Destination
        if (
            $item.Length -eq $ExpectedBytes -and
            (Get-Sha256 $Destination) -eq $expectedHash
        ) {
            Write-Host "$Name 已存在且校验通过。"
            return
        }
        Write-Warning "$Name 的缓存不符合固定版本，将在新归档验证后原子替换。"
    }

    $part = "${Destination}.part"
    try {
        Receive-File $Url $part
        $item = Get-Item -LiteralPath $part
        if ($item.Length -ne $ExpectedBytes) {
            throw "$Name 大小不匹配。期望 ${ExpectedBytes}，实际 $($item.Length)。"
        }
        $actual = Get-Sha256 $part
        if ($actual -ne $expectedHash) {
            throw "$Name SHA-256 不匹配。期望 ${expectedHash}，实际 ${actual}。"
        }
        Move-FileAtomically $part $Destination
    } finally {
        if (Test-Path -LiteralPath $part) {
            Remove-Item -LiteralPath $part -Force
        }
    }
}

function Get-VpoTreeDigest([string] $TreeRoot) {
    $resolvedRoot = [IO.Path]::GetFullPath($TreeRoot)
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "VPO 树不存在：$resolvedRoot"
    }

    $files = @(Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File)
    $totalBytes = [long](($files | Measure-Object -Property Length -Sum).Sum)
    $paths = New-Object "string[]" $files.Count
    $filesByPath = @{}
    for ($index = 0; $index -lt $files.Count; $index++) {
        $file = $files[$index]
        $relative = $file.FullName.Substring($resolvedRoot.Length + 1).Replace(
            [IO.Path]::DirectorySeparatorChar,
            "/"
        )
        $paths[$index] = $relative
        $filesByPath[$relative] = $file.FullName
    }
    [Array]::Sort($paths, [StringComparer]::Ordinal)

    $records = [Text.StringBuilder]::new()
    foreach ($relative in $paths) {
        $fileHash = (Get-Sha256 $filesByPath[$relative]).ToLowerInvariant()
        [void]$records.Append($fileHash)
        [void]$records.Append("  ")
        [void]$records.Append($relative)
        [void]$records.Append("`n")
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($records.ToString())
        $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace(
            "-",
            ""
        ).ToUpperInvariant()
    } finally {
        $sha.Dispose()
    }
    return [pscustomobject]@{
        FileCount = $files.Count
        TotalBytes = $totalBytes
        Sha256 = $digest
    }
}

function Assert-VpoTree([string] $InstallRoot) {
    $tree = Join-Path $InstallRoot $expectedTreeDirectory
    $digest = Get-VpoTreeDigest $tree
    if ($digest.FileCount -ne $expectedTreeFileCount) {
        throw "VPO 文件数不匹配。期望 ${expectedTreeFileCount}，实际 $($digest.FileCount)。"
    }
    if ($digest.TotalBytes -ne $expectedTreeBytes) {
        throw "VPO 总字节数不匹配。期望 ${expectedTreeBytes}，实际 $($digest.TotalBytes)。"
    }
    if ($digest.Sha256 -ne $expectedTreeSha256) {
        throw "VPO 完整树 SHA-256 不匹配。期望 ${expectedTreeSha256}，实际 $($digest.Sha256)。"
    }

    $license = Join-Path $tree "Documentation\license.htm"
    if ((Get-Sha256 $license) -ne $expectedLicenseSha256) {
        throw "VPO 许可证据 Hash 不匹配：$license"
    }
    return $digest
}

try {
    $installed = Assert-VpoTree $destination
    Write-Host (
        "Virtual Playing Orchestra 已存在且完整校验通过：" +
        "$($installed.FileCount) 个文件，$destination"
    )
    return
} catch {
    if (Test-Path -LiteralPath $destination) {
        Write-Warning "现有 VPO 目录未通过固定版本核验，将先构建完整新目录：$($_.Exception.Message)"
    }
}

$waveZip = Join-Path $cache $waveArchiveName
$scriptZip = Join-Path $cache $scriptArchiveName
Get-VerifiedArchive "VPO Wave Files 3.2" $waveUrl $waveZip $waveBytes $waveSha256
Get-VerifiedArchive "VPO Standard Orchestra 3.3" $scriptUrl $scriptZip $scriptBytes $scriptSha256

$stage = Assert-ManagedDirectChild (
    Join-Path $resourceRoot ("VirtualPlayingOrchestra.installing." + $PID)
) $resourceRoot "VPO 临时目录"
$previous = Assert-ManagedDirectChild (
    Join-Path $resourceRoot ("VirtualPlayingOrchestra.previous." + $PID)
) $resourceRoot "VPO 备份目录"
[void](Assert-ManagedDirectChild $destination $resourceRoot "VPO 正式目录")

foreach ($managed in @($stage, $previous)) {
    if (Test-Path -LiteralPath $managed) {
        Remove-Item -LiteralPath $managed -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $stage | Out-Null

$destinationMoved = $false
try {
    Expand-Archive -LiteralPath $waveZip -DestinationPath $stage -Force
    Expand-Archive -LiteralPath $scriptZip -DestinationPath $stage -Force
    $verified = Assert-VpoTree $stage

    if (Test-Path -LiteralPath $destination) {
        Move-Item -LiteralPath $destination -Destination $previous
        $destinationMoved = $true
    }
    Move-Item -LiteralPath $stage -Destination $destination

    if (Test-Path -LiteralPath $previous) {
        Remove-Item -LiteralPath $previous -Recurse -Force
    }
    Write-Host (
        "Virtual Playing Orchestra 安装完成并通过全树校验：" +
        "$($verified.FileCount) 个文件，$destination"
    )
} catch {
    if (
        $destinationMoved -and
        (-not (Test-Path -LiteralPath $destination)) -and
        (Test-Path -LiteralPath $previous)
    ) {
        Move-Item -LiteralPath $previous -Destination $destination
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
