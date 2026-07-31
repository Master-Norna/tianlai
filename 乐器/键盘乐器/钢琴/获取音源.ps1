[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Salamander 的采样直接存放在 Git 中，不使用 Git LFS。安装器只抓取已审计的
# 固定提交，在临时目录验证完整工作树后才切换正式目录。
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$resourceRoot = Join-Path $projectRoot "音源\钢琴"
$target = Join-Path $resourceRoot "SalamanderGrandPiano"

$repository = "https://github.com/sfzinstruments/SalamanderGrandPiano.git"
$commit = "3382bf9496bba2486f5ab0de55a264d1dfc38404"
$expectedTreeFileCount = 668
$expectedTreeBytes = 748451483
$expectedTreeSha256 = "FCF5B194A5E19057F006138F7EC852C0B1354E12D65C8250546F9CDB5CBDDB82"
$expectedFlacFileCount = 641
$expectedFlacBytes = 748397030
$expectedFlacSetSha256 = "1FA0E381904391B759CB3E82FF60BC54716AD99FD30ED07FD49C2128EF6239E5"
$expectedLicenseSha256 = "E6BC9E9C474700B708F568BAC9E5A8A9BCB2B1DAD53442F5BA449FCB848B8E76"
$expectedReadmeSha256 = "BE275B843D10A22E614E5F52BD414FE2CBDCBFD6165894B1DCCA738E8CBF391A"
$expectedSfzSha256 = "C8B282F03FDB2D9E6BE24A99DF0D97A05E7ECE718D1A14E0B882C518161F7837"

function Get-Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Assert-FileHash(
    [string] $Root,
    [string] $RelativePath,
    [string] $ExpectedSha256
) {
    $path = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "钢琴音源缺少固定文件：$path"
    }
    $actual = Get-Sha256 $path
    if ($actual -ne $ExpectedSha256) {
        throw "$RelativePath SHA-256 不匹配。期望 $ExpectedSha256，实际 $actual。"
    }
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
        throw "$Label 必须是受管钢琴资源目录的直接子项：$full"
    }
    return $full
}

function Get-SalamanderTreeDigest([string] $TreeRoot) {
    $resolvedRoot = [IO.Path]::GetFullPath($TreeRoot)
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "Salamander 资源树不存在：$resolvedRoot"
    }

    $gitPrefix = ".git/"
    $filesByPath = @{}
    $paths = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File -Force |
            ForEach-Object {
                $relative = $_.FullName.Substring($resolvedRoot.Length + 1).Replace(
                    [IO.Path]::DirectorySeparatorChar,
                    "/"
                )
                if (-not $relative.StartsWith(
                    $gitPrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    $filesByPath[$relative] = $_.FullName
                    $relative
                }
            }
    )
    [Array]::Sort($paths, [StringComparer]::Ordinal)

    $treeRecords = New-Object Text.StringBuilder
    $flacRecords = New-Object Text.StringBuilder
    $treeBytes = [Int64]0
    $flacBytes = [Int64]0
    $flacFileCount = 0
    foreach ($relative in $paths) {
        $path = $filesByPath[$relative]
        $item = Get-Item -LiteralPath $path
        $hash = (Get-Sha256 $path).ToLowerInvariant()
        [void]$treeRecords.Append($hash)
        [void]$treeRecords.Append("  ")
        [void]$treeRecords.Append($relative)
        [void]$treeRecords.Append("`n")
        $treeBytes += [Int64]$item.Length

        if (
            $relative.StartsWith(
                "Samples/",
                [StringComparison]::Ordinal
            ) -and
            $relative.EndsWith(
                ".flac",
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            [void]$flacRecords.Append($hash)
            [void]$flacRecords.Append("  ")
            [void]$flacRecords.Append($relative)
            [void]$flacRecords.Append("`n")
            $flacFileCount++
            $flacBytes += [Int64]$item.Length
        }
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $treeDigest = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($treeRecords.ToString()))
        )).Replace("-", "").ToUpperInvariant()
        $sha.Initialize()
        $flacDigest = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($flacRecords.ToString()))
        )).Replace("-", "").ToUpperInvariant()
    } finally {
        $sha.Dispose()
    }

    return [pscustomobject]@{
        FileCount = $paths.Count
        TotalBytes = $treeBytes
        TreeSha256 = $treeDigest
        FlacFileCount = $flacFileCount
        FlacBytes = $flacBytes
        FlacSetSha256 = $flacDigest
    }
}

function Assert-SalamanderTree([string] $Root, [string] $GitPath) {
    Assert-FileHash $Root "LICENSE" $expectedLicenseSha256
    Assert-FileHash $Root "README.md" $expectedReadmeSha256
    Assert-FileHash $Root "Salamander Grand Piano V3.sfz" $expectedSfzSha256

    $digest = Get-SalamanderTreeDigest $Root
    if ($digest.FileCount -ne $expectedTreeFileCount) {
        throw "Salamander 文件数不匹配。期望 $expectedTreeFileCount，实际 $($digest.FileCount)。"
    }
    if ($digest.TotalBytes -ne $expectedTreeBytes) {
        throw "Salamander 总字节数不匹配。期望 $expectedTreeBytes，实际 $($digest.TotalBytes)。"
    }
    if ($digest.TreeSha256 -ne $expectedTreeSha256) {
        throw "Salamander 完整树 SHA-256 不匹配。期望 $expectedTreeSha256，实际 $($digest.TreeSha256)。"
    }
    if ($digest.FlacFileCount -ne $expectedFlacFileCount) {
        throw "Salamander FLAC 数不匹配。期望 $expectedFlacFileCount，实际 $($digest.FlacFileCount)。"
    }
    if ($digest.FlacBytes -ne $expectedFlacBytes) {
        throw "Salamander FLAC 总字节数不匹配。期望 $expectedFlacBytes，实际 $($digest.FlacBytes)。"
    }
    if ($digest.FlacSetSha256 -ne $expectedFlacSetSha256) {
        throw "Salamander FLAC 集合 SHA-256 不匹配。期望 $expectedFlacSetSha256，实际 $($digest.FlacSetSha256)。"
    }

    $gitDirectory = Join-Path $Root ".git"
    if (Test-Path -LiteralPath $gitDirectory) {
        $headOutput = @(
            & $GitPath -c "safe.directory=$Root" -C $Root rev-parse HEAD
        )
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            throw "无法核验 Salamander 已安装目录的 Git 提交：$Root"
        }
        $head = $headOutput | Select-Object -First 1
        if (
            [string]::IsNullOrWhiteSpace($head) -or
            $head.Trim().ToLowerInvariant() -ne $commit
        ) {
            throw "Salamander Git 提交不匹配。期望 $commit，实际 $head。"
        }
    }
    return $digest
}

$git = Get-Command "git.exe" -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $git) {
    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue
}
if ($null -eq $git) {
    throw "安装固定版本 Salamander Grand Piano 需要 Git。"
}

try {
    $installed = Assert-SalamanderTree $target $git.Source
    Write-Host (
        "Salamander Grand Piano 已存在且完整校验通过：" +
        "$($installed.FileCount) 个文件，$target"
    )
    return
} catch {
    if (Test-Path -LiteralPath $target) {
        Write-Warning "现有钢琴音源未通过固定版本核验，将先构建完整新目录：$($_.Exception.Message)"
    }
}

New-Item -ItemType Directory -Force -Path $resourceRoot | Out-Null
$stage = Assert-ManagedDirectChild (
    Join-Path $resourceRoot ("SalamanderGrandPiano.installing." + [guid]::NewGuid().ToString("N"))
) $resourceRoot "Salamander 临时目录"
$previous = Assert-ManagedDirectChild (
    Join-Path $resourceRoot ("SalamanderGrandPiano.previous." + [guid]::NewGuid().ToString("N"))
) $resourceRoot "Salamander 备份目录"
[void](Assert-ManagedDirectChild $target $resourceRoot "Salamander 正式目录")

New-Item -ItemType Directory -Path $stage | Out-Null
$destinationMoved = $false
$stageMoved = $false
try {
    & $git.Source -C $stage init
    if ($LASTEXITCODE -ne 0) {
        throw "无法初始化 Salamander 临时 Git 仓库。"
    }
    & $git.Source -C $stage config core.autocrlf false
    if ($LASTEXITCODE -ne 0) {
        throw "无法固定 Salamander Git 换行设置。"
    }
    & $git.Source -C $stage remote add origin $repository
    if ($LASTEXITCODE -ne 0) {
        throw "无法登记 Salamander 官方上游：$repository"
    }
    & $git.Source -c "protocol.version=2" -C $stage fetch --depth 1 origin $commit
    if ($LASTEXITCODE -ne 0) {
        throw "无法抓取 Salamander 固定提交 $commit。"
    }
    $fetchedOutput = @(& $git.Source -C $stage rev-parse FETCH_HEAD)
    $fetchExitCode = $LASTEXITCODE
    $fetched = $fetchedOutput | Select-Object -First 1
    if (
        $fetchExitCode -ne 0 -or
        [string]::IsNullOrWhiteSpace($fetched) -or
        $fetched.Trim().ToLowerInvariant() -ne $commit
    ) {
        throw "Salamander 抓取结果不是固定提交 $commit。"
    }
    & $git.Source -c "advice.detachedHead=false" -C $stage checkout --detach $commit
    if ($LASTEXITCODE -ne 0) {
        throw "无法检出 Salamander 固定提交 $commit。"
    }

    $verified = Assert-SalamanderTree $stage $git.Source

    if (Test-Path -LiteralPath $target) {
        [IO.Directory]::Move(
            [IO.Path]::GetFullPath($target),
            [IO.Path]::GetFullPath($previous)
        )
        $destinationMoved = $true
    }
    [IO.Directory]::Move(
        [IO.Path]::GetFullPath($stage),
        [IO.Path]::GetFullPath($target)
    )
    $stageMoved = $true

    # 切换后再核验一次；任何切换异常都会把原目录恢复到正式路径。
    $final = Assert-SalamanderTree $target $git.Source
    Write-Host (
        "Salamander Grand Piano 固定版安装完成：" +
        "$($final.FileCount) 个文件，提交 $commit，$target"
    )
} catch {
    $failure = $_
    if ($stageMoved -and (Test-Path -LiteralPath $target)) {
        [IO.Directory]::Move(
            [IO.Path]::GetFullPath($target),
            [IO.Path]::GetFullPath($stage)
        )
        $stageMoved = $false
    }
    if ($destinationMoved -and (Test-Path -LiteralPath $previous)) {
        [IO.Directory]::Move(
            [IO.Path]::GetFullPath($previous),
            [IO.Path]::GetFullPath($target)
        )
        $destinationMoved = $false
    }
    throw $failure
} finally {
    if (Test-Path -LiteralPath $stage) {
        $safeStage = Assert-ManagedDirectChild $stage $resourceRoot "Salamander 清理目录"
        Remove-Item -LiteralPath $safeStage -Recurse -Force
    }
}

if (Test-Path -LiteralPath $previous) {
    try {
        $safePrevious = Assert-ManagedDirectChild $previous $resourceRoot "Salamander 旧目录"
        Remove-Item -LiteralPath $safePrevious -Recurse -Force
    } catch {
        Write-Warning "新版本已安装，但旧目录未能清理：$previous。$($_.Exception.Message)"
    }
}
