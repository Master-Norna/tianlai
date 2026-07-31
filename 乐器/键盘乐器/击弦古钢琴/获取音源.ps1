[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Keep this file ASCII-only so Windows PowerShell 5.1 parses it without
# depending on a UTF-8 BOM. Chinese path components are assembled by codepoint.
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$resourceDirectoryName = ([char]0x97F3).ToString() + ([char]0x6E90).ToString()
$cacheDirectoryName = (
    ([char]0x4E0B).ToString() +
    ([char]0x8F7D).ToString() +
    ([char]0x7F13).ToString() +
    ([char]0x5B58).ToString()
)
$converterName = (
    ([char]0x8F6C).ToString() +
    ([char]0x6362).ToString() +
    "SIMPK" +
    ([char]0x97F3).ToString() +
    ([char]0x6E90).ToString() +
    ".py"
)
$tuningTableName = (
    "SIMPK" +
    ([char]0x8C03).ToString() +
    ([char]0x97F3).ToString() +
    ([char]0x8868).ToString() +
    ".json"
)
$sourceEvidenceName = (
    "SIMPK" +
    ([char]0x6765).ToString() +
    ([char]0x6E90).ToString() +
    ([char]0x8BC1).ToString() +
    ([char]0x636E).ToString() +
    ".json"
)

$resourceRoot = Join-Path $projectRoot $resourceDirectoryName
$cacheRoot = Join-Path $resourceRoot $cacheDirectoryName
$target = Join-Path $resourceRoot "SIMPK_03_Clavichord"
$archive = Join-Path $cacheRoot "SIMPK_03_Clavichord.zip"
$partialArchive = $archive + ".part"
$converter = Join-Path $PSScriptRoot $converterName
$tuningTable = Join-Path $PSScriptRoot $tuningTableName
$sourceEvidence = Join-Path $PSScriptRoot $sourceEvidenceName
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

$archiveUrl = "https://topographie.simpk.de/kti/SIMPK_03_Clavichord.zip"
$archiveBytes = [Int64]1518834178
$archiveSha256 = "470721722D5D4ABAD0A6F29ADAB8D7AECC815FD695B531B73AD5D9D07DCE39A2"
$archiveEntryCount = 768
$archiveFileCount = 762
$archiveExpandedBytes = [Int64]2011246107
$licenseSha256 = "A790BDA01537A8D6635806093ABEAF72E693C55C0C3935E86F294B7D05440C49"
$presetSha256 = "F2077B5D89E14F60F2F9F5C45BA195BB87C29C04C1548EC5F446FB1F43313F35"
$sourceEvidenceSha256 = "0F427BC8F269F569D097D5AE34852B39C2C398A66E0AAFEE161CD49BBC1FDF4E"
$sampleCount = 756
$sampleBytes = [Int64]2002780260
$sampleSetSha256 = "5C71B39656E11B4CB20E8FC8CC1292298506B990C11AD13E62A40D4EB9D24940"
$tuningTableSha256 = "9EBFDF484A03DAF5DCD5BFDAF2AE41E8BEAD4E8D966662DD09109F62221753FE"
$normalSfzSha256 = "F8F6E66A399A87C20240264CF71E767165B92565404CC00406348F8F89159B1F"
$resonanceSfzSha256 = "6A4D576F1D70A2DAF36B65D8BD54BE1EFE5DE0E108BEC162B2F58CE142CEC362"

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
        throw "Required file is missing: $path"
    }
    $actual = Get-Sha256 $path
    if ($actual -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $RelativePath. Expected $ExpectedSha256, got $actual."
    }
}

function Get-SourceSampleSetSha256([string] $Root) {
    $sampleRoot = Join-Path $Root "assets\wav"
    if (-not (Test-Path -LiteralPath $sampleRoot -PathType Container)) {
        throw "SIMPK sample directory is missing: $sampleRoot"
    }
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $files = @(
        Get-ChildItem -LiteralPath $sampleRoot -Recurse -File -Filter "*.wav" |
            Sort-Object {
                $_.FullName.Substring($rootFull.Length + 1).Replace("\", "/")
            }
    )
    if ($files.Count -ne $sampleCount) {
        throw "SIMPK sample count mismatch. Expected $sampleCount, got $($files.Count)."
    }
    $bytes = [Int64](($files | Measure-Object -Property Length -Sum).Sum)
    if ($bytes -ne $sampleBytes) {
        throw "SIMPK sample byte count mismatch. Expected $sampleBytes, got $bytes."
    }

    $records = New-Object Text.StringBuilder
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($rootFull.Length + 1).Replace("\", "/")
        $hash = (Get-Sha256 $file.FullName).ToLowerInvariant()
        [void]$records.Append($hash + "  " + $relative + "`n")
    }
    $encoding = New-Object Text.UTF8Encoding($false)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($encoding.GetBytes($records.ToString()))
        return ([BitConverter]::ToString($digest)).Replace("-", "").ToUpperInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Assert-UpstreamTree([string] $Root) {
    Assert-FileHash $Root "LICENSE" $licenseSha256
    Assert-FileHash $Root "clavichord.dspreset" $presetSha256
    $actualSampleSet = Get-SourceSampleSetSha256 $Root
    if ($actualSampleSet -ne $sampleSetSha256) {
        throw "SIMPK sample-set SHA-256 mismatch. Expected $sampleSetSha256, got $actualSampleSet."
    }
}

function Assert-InstalledTree([string] $Root) {
    Assert-UpstreamTree $Root
    Assert-FileHash $Root "SOURCE.json" $sourceEvidenceSha256
    Assert-FileHash $Root "tianlai\tuning.json" $tuningTableSha256
    Assert-FileHash $Root "tianlai\normal.sfz" $normalSfzSha256
    Assert-FileHash $Root "tianlai\resonance.sfz" $resonanceSfzSha256
}

function Assert-Archive([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "SIMPK archive is missing: $Path"
    }
    $length = (Get-Item -LiteralPath $Path).Length
    if ($length -ne $archiveBytes) {
        throw "SIMPK archive size mismatch. Expected $archiveBytes, got $length."
    }
    $actual = Get-Sha256 $Path
    if ($actual -ne $archiveSha256) {
        throw "SIMPK archive SHA-256 mismatch. Expected $archiveSha256, got $actual."
    }
}

function Expand-VerifiedArchive([string] $ArchivePath, [string] $StageRoot) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $stageFull = [IO.Path]::GetFullPath($StageRoot)
    $stagePrefix = $stageFull.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    $zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        if ($zip.Entries.Count -ne $archiveEntryCount) {
            throw "SIMPK archive entry count mismatch. Expected $archiveEntryCount, got $($zip.Entries.Count)."
        }
        $seen = @{}
        $fileCount = 0
        [Int64]$expandedBytes = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace("\", "/")
            if ($seen.ContainsKey($name)) {
                throw "SIMPK archive contains a duplicate path: $name"
            }
            $seen[$name] = $true
            $parts = @($name.Split("/"))
            $unsafePart = @(
                $parts | Where-Object { $_ -eq "." -or $_ -eq ".." }
            ).Count -gt 0
            $destination = [IO.Path]::GetFullPath((Join-Path $stageFull $name))
            if (
                -not $name.StartsWith(
                    "SIMPK_03_Clavichord/",
                    [StringComparison]::Ordinal
                ) -or
                $name.StartsWith("/") -or
                $unsafePart -or
                -not $destination.StartsWith(
                    $stagePrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )
            ) {
                throw "SIMPK archive contains an unsafe path: $name"
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Force -Path $destination | Out-Null
                continue
            }
            $parent = [IO.Path]::GetDirectoryName($destination)
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile(
                $entry,
                $destination,
                $false
            )
            $fileCount += 1
            $expandedBytes += $entry.Length
        }
        if ($fileCount -ne $archiveFileCount -or $expandedBytes -ne $archiveExpandedBytes) {
            throw (
                "SIMPK expanded inventory mismatch. Expected " +
                "$archiveFileCount files/$archiveExpandedBytes bytes, got " +
                "$fileCount files/$expandedBytes bytes."
            )
        }
    } finally {
        $zip.Dispose()
    }
}

if (
    $tuningTableSha256.StartsWith("__") -or
    $normalSfzSha256.StartsWith("__") -or
    $resonanceSfzSha256.StartsWith("__")
) {
    throw "Installer is incomplete: frozen tuning/SFZ hashes have not been filled."
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual-environment Python is required: $python"
}
if (-not (Test-Path -LiteralPath $converter -PathType Leaf)) {
    throw "SIMPK converter is missing: $converter"
}
if (-not (Test-Path -LiteralPath $tuningTable -PathType Leaf)) {
    throw "SIMPK tuning table is missing: $tuningTable"
}
if ((Get-Sha256 $tuningTable) -ne $tuningTableSha256) {
    throw "The committed SIMPK tuning table does not match the installer."
}
if (-not (Test-Path -LiteralPath $sourceEvidence -PathType Leaf)) {
    throw "SIMPK source evidence is missing: $sourceEvidence"
}
if ((Get-Sha256 $sourceEvidence) -ne $sourceEvidenceSha256) {
    throw "The committed SIMPK source evidence does not match the installer."
}

if (Test-Path -LiteralPath $target) {
    Assert-InstalledTree $target
    Write-Host "SIMPK clavichord already exists and passed all checks: $target"
    return
}

New-Item -ItemType Directory -Force -Path $resourceRoot, $cacheRoot | Out-Null
if (Test-Path -LiteralPath $archive) {
    Assert-Archive $archive
} else {
    if (Test-Path -LiteralPath $partialArchive) {
        $partialLength = (Get-Item -LiteralPath $partialArchive).Length
        if ($partialLength -gt $archiveBytes) {
            throw "Partial SIMPK archive is larger than the pinned archive."
        }
    }
    if (
        -not (Test-Path -LiteralPath $partialArchive) -or
        (Get-Item -LiteralPath $partialArchive).Length -lt $archiveBytes
    ) {
        $curl = Get-Command "curl.exe" -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $curl) {
            throw "curl.exe is required for the resumable SIMPK download."
        }
        & $curl.Source `
            --location `
            --fail `
            --retry 5 `
            --continue-at - `
            --output $partialArchive `
            $archiveUrl
        $curlExitCode = $LASTEXITCODE
        if ($curlExitCode -ne 0) {
            throw "SIMPK download failed with curl exit code $curlExitCode."
        }
    }
    Assert-Archive $partialArchive
    if (Test-Path -LiteralPath $archive) {
        throw "Final SIMPK archive appeared during download; refusing to overwrite it."
    }
    [IO.File]::Move(
        [IO.Path]::GetFullPath($partialArchive),
        [IO.Path]::GetFullPath($archive)
    )
}

$stage = Join-Path $cacheRoot (
    "SIMPK_03_Clavichord.installing." + [guid]::NewGuid().ToString("N")
)
$cachePrefix = [IO.Path]::GetFullPath($cacheRoot).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
$stageFull = [IO.Path]::GetFullPath($stage)
if (-not $stageFull.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe staging path: $stageFull"
}

try {
    New-Item -ItemType Directory -Path $stageFull | Out-Null
    Expand-VerifiedArchive $archive $stageFull
    $sourceRoot = Join-Path $stageFull "SIMPK_03_Clavichord"
    Assert-UpstreamTree $sourceRoot

    Copy-Item -LiteralPath $sourceEvidence -Destination (
        Join-Path $sourceRoot "SOURCE.json"
    )
    & $python `
        $converter `
        --source-root $sourceRoot `
        --tuning-table $tuningTable `
        --require-complete-tuning
    $converterExitCode = $LASTEXITCODE
    if ($converterExitCode -ne 0) {
        throw "SIMPK conversion failed with exit code $converterExitCode."
    }
    Copy-Item -LiteralPath $tuningTable -Destination (
        Join-Path $sourceRoot "tianlai\tuning.json"
    )
    Assert-InstalledTree $sourceRoot

    if (Test-Path -LiteralPath $target) {
        throw "Target appeared during installation; refusing to overwrite it: $target"
    }
    [IO.Directory]::Move(
        [IO.Path]::GetFullPath($sourceRoot),
        [IO.Path]::GetFullPath($target)
    )
    Write-Host "SIMPK clavichord installed and verified: $target"
} finally {
    if (Test-Path -LiteralPath $stage) {
        $resolvedStage = [IO.Path]::GetFullPath($stage)
        if (-not $resolvedStage.StartsWith(
            $cachePrefix,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing unsafe cleanup path: $resolvedStage"
        }
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
