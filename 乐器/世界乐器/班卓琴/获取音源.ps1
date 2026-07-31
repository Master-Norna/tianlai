[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# ganjo 的 CC0 采样直接存放在 Git 中。安装器只抓取已审计的固定提交，
# 在临时目录验证完整工作树后才切换正式目录；已有目标只校验，不覆盖。
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$resourceRoot = Join-Path $projectRoot "音源\itsclipping"
$target = Join-Path $resourceRoot "ganjo-v1.000"

$repository = "https://github.com/sfzinstruments/ganjo.git"
$tag = "v1.000"
$commit = "ccff5cd5cd3b513873a48994c07724d9d3c39e1c"
$expectedTreeFileCount = 66
$expectedTreeBytes = 26113258
$expectedTreeSha256 = "AA16FA9940BC962EDAAE6AC48E1552889781ADE51B0D4B2BAA2762408B0AF91F"
$expectedWavFileCount = 61
$expectedWavBytes = 24480172
$expectedWavSetSha256 = "B6C7D842CAB222F5AAE00CE2128AF495A16057D56925E51D7AFD125177229A46"
$expectedLicenseSha256 = "F4E7F373B9B996950337E8D41A4A2939C2D90B7725E9BAF3D5084A22717AD328"
$expectedReadmeSha256 = "B79A853A0B8D48D6FBC7CC64B0CC56C5738572BDDB075B2B277CFFA81E90A08D"
$expectedSfzSha256 = "9717CACBD1F12C55233B5EDC85D10FAD02229B231B7BD3188C4E7BF5227F3214"

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
        throw "ganjo 音源缺少固定文件：$path"
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
        throw "$Label 必须是受管 ganjo 资源目录的直接子项：$full"
    }
    return $full
}

function Get-GanjoTreeDigest([string] $TreeRoot) {
    $resolvedRoot = [IO.Path]::GetFullPath($TreeRoot)
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "ganjo 资源树不存在：$resolvedRoot"
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
    $wavRecords = New-Object Text.StringBuilder
    $treeBytes = [Int64]0
    $wavBytes = [Int64]0
    $wavFileCount = 0
    foreach ($relative in $paths) {
        $path = $filesByPath[$relative]
        $item = Get-Item -LiteralPath $path
        $hash = (Get-Sha256 $path).ToLowerInvariant()
        [void]$treeRecords.Append($hash)
        [void]$treeRecords.Append("  ")
        [void]$treeRecords.Append($relative)
        [void]$treeRecords.Append("`n")
        $treeBytes += [Int64]$item.Length

        if ($relative.EndsWith(
            ".wav",
            [StringComparison]::OrdinalIgnoreCase
        )) {
            [void]$wavRecords.Append($hash)
            [void]$wavRecords.Append("  ")
            [void]$wavRecords.Append($relative)
            [void]$wavRecords.Append("`n")
            $wavFileCount++
            $wavBytes += [Int64]$item.Length
        }
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $treeDigest = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($treeRecords.ToString()))
        )).Replace("-", "").ToUpperInvariant()
        $sha.Initialize()
        $wavDigest = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($wavRecords.ToString()))
        )).Replace("-", "").ToUpperInvariant()
    } finally {
        $sha.Dispose()
    }

    return [pscustomobject]@{
        FileCount = $paths.Count
        TotalBytes = $treeBytes
        TreeSha256 = $treeDigest
        WavFileCount = $wavFileCount
        WavBytes = $wavBytes
        WavSetSha256 = $wavDigest
    }
}

function Assert-GanjoTree([string] $Root, [string] $GitPath) {
    Assert-FileHash $Root "LICENSE.md" $expectedLicenseSha256
    Assert-FileHash $Root "README.md" $expectedReadmeSha256
    Assert-FileHash $Root "ganjo.sfz" $expectedSfzSha256

    $digest = Get-GanjoTreeDigest $Root
    if ($digest.FileCount -ne $expectedTreeFileCount) {
        throw "ganjo 文件数不匹配。期望 $expectedTreeFileCount，实际 $($digest.FileCount)。"
    }
    if ($digest.TotalBytes -ne $expectedTreeBytes) {
        throw "ganjo 总字节数不匹配。期望 $expectedTreeBytes，实际 $($digest.TotalBytes)。"
    }
    if ($digest.TreeSha256 -ne $expectedTreeSha256) {
        throw "ganjo 完整树 SHA-256 不匹配。期望 $expectedTreeSha256，实际 $($digest.TreeSha256)。"
    }
    if ($digest.WavFileCount -ne $expectedWavFileCount) {
        throw "ganjo WAV 数不匹配。期望 $expectedWavFileCount，实际 $($digest.WavFileCount)。"
    }
    if ($digest.WavBytes -ne $expectedWavBytes) {
        throw "ganjo WAV 总字节数不匹配。期望 $expectedWavBytes，实际 $($digest.WavBytes)。"
    }
    if ($digest.WavSetSha256 -ne $expectedWavSetSha256) {
        throw "ganjo WAV 集合 SHA-256 不匹配。期望 $expectedWavSetSha256，实际 $($digest.WavSetSha256)。"
    }

    $gitDirectory = Join-Path $Root ".git"
    if (Test-Path -LiteralPath $gitDirectory) {
        $headOutput = @(
            & $GitPath -c "safe.directory=$Root" -C $Root rev-parse HEAD
        )
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            throw "无法核验 ganjo 已安装目录的 Git 提交：$Root"
        }
        $head = $headOutput | Select-Object -First 1
        if (
            [string]::IsNullOrWhiteSpace($head) -or
            $head.Trim().ToLowerInvariant() -ne $commit
        ) {
            throw "ganjo Git 提交不匹配。期望 $commit，实际 $head。"
        }
    }
    return $digest
}

$git = Get-Command "git.exe" -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $git) {
    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue
}
if ($null -eq $git) {
    throw "安装固定版本 ganjo 需要 Git。"
}

if (Test-Path -LiteralPath $target) {
    try {
        $installed = Assert-GanjoTree $target $git.Source
        Write-Host (
            "ganjo 已存在且完整校验通过：" +
            "$($installed.FileCount) 个文件，标签 $tag，$target"
        )
        return
    } catch {
        throw (
            "现有 ganjo 目录未通过固定版本核验；为避免覆盖用户资源，" +
            "安装器不会替换或删除它。请人工检查并移走该目录后重试。详情：" +
            $_.Exception.Message
        )
    }
}

New-Item -ItemType Directory -Force -Path $resourceRoot | Out-Null
$stage = Assert-ManagedDirectChild (
    Join-Path $resourceRoot ("ganjo-v1.000.installing." + [guid]::NewGuid().ToString("N"))
) $resourceRoot "ganjo 临时目录"
[void](Assert-ManagedDirectChild $target $resourceRoot "ganjo 正式目录")

New-Item -ItemType Directory -Path $stage | Out-Null
$stageMoved = $false
try {
    & $git.Source -C $stage init
    if ($LASTEXITCODE -ne 0) {
        throw "无法初始化 ganjo 临时 Git 仓库。"
    }
    & $git.Source -C $stage config core.autocrlf false
    if ($LASTEXITCODE -ne 0) {
        throw "无法固定 ganjo Git 换行设置。"
    }
    & $git.Source -C $stage remote add origin $repository
    if ($LASTEXITCODE -ne 0) {
        throw "无法登记 ganjo 官方上游：$repository"
    }
    & $git.Source -c "protocol.version=2" -C $stage fetch --depth 1 origin $commit
    if ($LASTEXITCODE -ne 0) {
        throw "无法抓取 ganjo 固定提交 $commit（标签 $tag）。"
    }
    $fetchedOutput = @(& $git.Source -C $stage rev-parse FETCH_HEAD)
    $fetchExitCode = $LASTEXITCODE
    $fetched = $fetchedOutput | Select-Object -First 1
    if (
        $fetchExitCode -ne 0 -or
        [string]::IsNullOrWhiteSpace($fetched) -or
        $fetched.Trim().ToLowerInvariant() -ne $commit
    ) {
        throw "ganjo 抓取结果不是固定提交 $commit。"
    }
    & $git.Source -c "advice.detachedHead=false" -C $stage checkout --detach $commit
    if ($LASTEXITCODE -ne 0) {
        throw "无法检出 ganjo 固定提交 $commit。"
    }

    $verified = Assert-GanjoTree $stage $git.Source

    # 目标在预检后若被其他进程创建，Directory.Move 会失败，不会覆盖。
    [IO.Directory]::Move(
        [IO.Path]::GetFullPath($stage),
        [IO.Path]::GetFullPath($target)
    )
    $stageMoved = $true

    # 切换后再核验一次；任何异常都会撤回本次新安装。
    $final = Assert-GanjoTree $target $git.Source
    Write-Host (
        "ganjo 固定版安装完成：" +
        "$($final.FileCount) 个文件，标签 $tag，提交 $commit，$target"
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
    throw $failure
} finally {
    if (Test-Path -LiteralPath $stage) {
        $safeStage = Assert-ManagedDirectChild $stage $resourceRoot "ganjo 清理目录"
        Remove-Item -LiteralPath $safeStage -Recurse -Force
    }
}
