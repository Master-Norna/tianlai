[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Keep this file ASCII-only so Windows PowerShell 5.1 can parse it without
# depending on a UTF-8 BOM. The resource directory name below is U+97F3 U+6E90.
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$resourceDirectoryName = ([char]0x97F3).ToString() + ([char]0x6E90).ToString()
$cacheDirectoryName = (
    ([char]0x4E0B).ToString() +
    ([char]0x8F7D).ToString() +
    ([char]0x7F13).ToString() +
    ([char]0x5B58).ToString()
)
$resourceRoot = Join-Path $projectRoot $resourceDirectoryName
$cacheRoot = Join-Path $resourceRoot $cacheDirectoryName
$target = Join-Path $resourceRoot "GregSullivan.E-Pianos"

$repository = "https://github.com/sfzinstruments/GregSullivan.E-Pianos.git"
$commit = "8c3e581acda3594b553948ff0222d4f84a698376"
$cp80SfzSha256 = "4C9FA22DDEBCC56A026E711C0D6A4EEF7A20C0905F7C6F482466F040A4FA9C3F"
$licenseSha256 = "E6BC9E9C474700B708F568BAC9E5A8A9BCB2B1DAD53442F5BA449FCB848B8E76"
$readmeSha256 = "9F9A7D4B205ABB9C2FD2D03012E56C7B25AEF552EA022BFECB9009FEBAA1A4DE"
$sampleSetSha256 = "ABBB9B2F9F3ECDB50AC39B6D3F15AD068FC2D09592864DEEEE1CF857B8D174DA"
$sampleCount = 81
$sampleBytes = [Int64]11003179

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
        throw "Required source evidence is missing: $path"
    }
    $actual = Get-Sha256 $path
    if ($actual -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $RelativePath. Expected $ExpectedSha256, got $actual."
    }
}

function Get-Cp80SampleSetSha256([string] $Root) {
    $sampleRoot = Join-Path $Root "CP80\Samples"
    if (-not (Test-Path -LiteralPath $sampleRoot -PathType Container)) {
        throw "CP80 sample directory is missing: $sampleRoot"
    }

    $files = @(
        Get-ChildItem -LiteralPath $sampleRoot -File -Filter "*.flac" |
            Sort-Object -Property Name
    )
    if ($files.Count -ne $sampleCount) {
        throw "CP80 sample count mismatch. Expected $sampleCount, got $($files.Count)."
    }
    $bytes = [Int64](($files | Measure-Object -Property Length -Sum).Sum)
    if ($bytes -ne $sampleBytes) {
        throw "CP80 sample byte count mismatch. Expected $sampleBytes, got $bytes."
    }

    $records = New-Object Text.StringBuilder
    foreach ($file in $files) {
        $relative = "CP80/Samples/" + $file.Name
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

function Assert-InstalledTree([string] $Root, [string] $GitPath) {
    Assert-FileHash $Root "CP80\CP80.sfz" $cp80SfzSha256
    Assert-FileHash $Root "LICENSE" $licenseSha256
    Assert-FileHash $Root "README.md" $readmeSha256

    $actualSampleSet = Get-Cp80SampleSetSha256 $Root
    if ($actualSampleSet -ne $sampleSetSha256) {
        throw "CP80 sample-set SHA-256 mismatch. Expected $sampleSetSha256, got $actualSampleSet."
    }

    $gitDirectory = Join-Path $Root ".git"
    if (Test-Path -LiteralPath $gitDirectory) {
        # Capture the native exit code before running another pipeline. In
        # Windows PowerShell 5.1, piping a native command directly through
        # Select-Object can leave a misleading LASTEXITCODE.
        $headOutput = @(
            & $GitPath -c "safe.directory=$Root" -C $Root rev-parse HEAD
        )
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            throw "Unable to verify the installed Git commit: $Root"
        }
        $head = $headOutput | Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($head)) {
            throw "Git returned no commit while verifying: $Root"
        }
        if ($head.Trim().ToLowerInvariant() -ne $commit) {
            throw "Installed Git commit mismatch. Expected $commit, got $($head.Trim())."
        }
    }
}

$git = Get-Command "git.exe" -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $git) {
    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue
}
if ($null -eq $git) {
    throw "Git is required to install and verify the pinned CP80 source."
}

if (Test-Path -LiteralPath $target) {
    # Never merge into, delete, or replace an existing resource directory.
    Assert-InstalledTree $target $git.Source
    Write-Host "Greg Sullivan CP80 already exists and passed all checks: $target"
    return
}

New-Item -ItemType Directory -Force -Path $resourceRoot, $cacheRoot | Out-Null
$stage = Join-Path $cacheRoot ("GregSullivan.E-Pianos.installing." + [guid]::NewGuid().ToString("N"))
$cachePrefix = [IO.Path]::GetFullPath($cacheRoot).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
$stageFull = [IO.Path]::GetFullPath($stage)
if (-not $stageFull.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe staging path: $stageFull"
}

try {
    & $git.Source clone --no-checkout $repository $stage
    if ($LASTEXITCODE -ne 0) {
        throw "Git clone failed: $repository"
    }
    & $git.Source -c "advice.detachedHead=false" -C $stage checkout --detach $commit
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to check out pinned commit $commit."
    }

    Assert-InstalledTree $stage $git.Source
    if (Test-Path -LiteralPath $target) {
        throw "Target appeared during installation; refusing to overwrite it: $target"
    }
    # Directory.Move fails if the destination appears between the check above
    # and the rename. Move-Item would instead merge the staging directory into
    # an existing directory, which violates the no-overwrite contract.
    [IO.Directory]::Move($stageFull, [IO.Path]::GetFullPath($target))
    Write-Host "Greg Sullivan CP80 installed and verified: $target"
} finally {
    if (Test-Path -LiteralPath $stage) {
        $resolvedStage = [IO.Path]::GetFullPath($stage)
        if (-not $resolvedStage.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe cleanup path: $resolvedStage"
        }
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
