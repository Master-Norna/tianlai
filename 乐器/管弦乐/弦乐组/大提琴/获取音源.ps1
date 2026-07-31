$ErrorActionPreference = "Stop"

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\..\.."))
& (Join-Path $root "安装VPO音源.ps1")
