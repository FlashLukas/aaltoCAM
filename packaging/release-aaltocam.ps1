<#
.SYNOPSIS
    Build a standalone aaltocam.exe from the newest tarball and publish it.

.DESCRIPTION
    The whole release path in one command: find the newest aaltocam-*.tar.gz,
    throw away the previously unpacked source, unpack the new one, install it
    into a virtual environment, build the executable with PyInstaller, and copy
    the result back beside the tarballs as aaltocam-<version>-win64.

    The installer it runs is the one inside the tarball, at
    packaging/install-aaltocam.ps1, not whatever copy happens to be sitting in
    this folder. So the installer and the source it installs always come from
    the same release, and a fix to the installer travels with the code that
    needed it. A copy in this folder is used only if the tarball predates the
    move and carries none.

    Nothing here needs administrator rights.

.PARAMETER Source
    A specific tarball. Defaults to the newest aaltocam-*.tar.gz in -Publish.

.PARAMETER Root
    Working area for the source tree, the virtual environment and the build.
    Keep it out of OneDrive: a virtual environment is thousands of small files
    and syncing it causes churn and occasional file locks mid-install.

.PARAMETER Publish
    Where the tarballs live and where the built folder is copied to. Defaults
    to the folder this script is in.

.PARAMETER Zip
    Also write aaltocam-<version>-win64.zip next to the folder. One file is far
    kinder to OneDrive than 350 loose ones, and easier to carry on a stick.

.PARAMETER NoCopy
    Build but do not publish.

.EXAMPLE
    .\release-aaltocam.ps1

.EXAMPLE
    .\release-aaltocam.ps1 -Zip

.EXAMPLE
    .\release-aaltocam.ps1 -Source .\aaltocam-0.13.0.tar.gz
#>
[CmdletBinding()]
param(
    [string] $Source,
    [string] $Root    = (Join-Path $HOME 'aaltocam-work'),
    [string] $Publish = $PSScriptRoot,
    [switch] $Zip,
    [switch] $NoCopy
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string] $Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note { param([string] $Text) Write-Host "    $Text" -ForegroundColor DarkGray }

# Anything PyInstaller leaves behind that predates this moment is stale, and
# publishing a stale build is the one failure that looks like success.
$started = Get-Date

# --- the tarball -----------------------------------------------------------

if ($Source) {
    if (-not (Test-Path $Source)) { throw "No such file: $Source" }
    $tarball = (Resolve-Path $Source).Path
} else {
    $newest = Get-ChildItem (Join-Path $Publish 'aaltocam-*.tar.gz') -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "No aaltocam-*.tar.gz found in $Publish" }
    $tarball = $newest.FullName
}
Write-Step "Newest release: $(Split-Path $tarball -Leaf)"

# --- unpack, clearing whatever was there ------------------------------------

# tar overwrites the files it finds but never removes ones the new version
# deleted. Left in place they are swept into the wheel and shipped.
$srcDir = Join-Path $Root 'src'
if (Test-Path $srcDir) {
    Write-Step 'Clearing the previously unpacked source'
    Remove-Item -Recurse -Force $srcDir
}
New-Item -ItemType Directory -Force -Path $srcDir | Out-Null

Write-Step "Unpacking $(Split-Path $tarball -Leaf)"
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # tar chatters on stderr; 5.1 calls that fatal
try {
    & tar -xzf $tarball -C $srcDir
    if ($LASTEXITCODE -ne 0) { throw "tar failed (exit code $LASTEXITCODE)." }
} finally {
    $ErrorActionPreference = $previous
}

$tree = Get-ChildItem $srcDir -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
    Select-Object -First 1
if (-not $tree) { throw "No pyproject.toml inside $tarball" }
$tree = $tree.FullName

# --- the installer, taken from the tarball ----------------------------------

$installer = Join-Path $tree 'packaging\install-aaltocam.ps1'
if (Test-Path $installer) {
    Write-Note 'Using the installer shipped inside the tarball.'
} else {
    $installer = Join-Path $PSScriptRoot 'install-aaltocam.ps1'
    if (-not (Test-Path $installer)) {
        throw ("This tarball carries no packaging\install-aaltocam.ps1 and there " +
               "is no copy in $PSScriptRoot to fall back on.")
    }
    Write-Note ("This tarball predates packaging\install-aaltocam.ps1; falling " +
                "back to the copy in this folder.")
}

# --- install and build -----------------------------------------------------

# -Source is the unpacked tree, so the installer skips its own unpack step and
# there is exactly one copy of the source in play.
Write-Step 'Installing and building, which takes a couple of minutes'
& $installer -Source $tree -Root $Root -BuildExe

$dist = Join-Path $Root 'dist\aaltocam'
$exe  = Join-Path $dist 'aaltocam.exe'
if (-not (Test-Path $exe)) {
    throw "PyInstaller produced no $exe. Its error is in the output above."
}
if ((Get-Item $exe).LastWriteTime -lt $started) {
    throw ("aaltocam.exe was not rebuilt -- it dates from " +
           "$((Get-Item $exe).LastWriteTime). PyInstaller failed; its error " +
           "is in the output above. Refusing to publish the older build.")
}

# --- version, read from what was actually installed ------------------------

# From pyproject.toml rather than the file name, so a mislabelled tarball
# cannot publish itself under the wrong version.
$pyproject = Join-Path $tree 'pyproject.toml'
$match = Select-String -Path $pyproject -Pattern '^\s*version\s*=\s*"([^"]+)"' |
    Select-Object -First 1
if (-not $match) { throw "No version line in $pyproject" }
$version = $match.Matches[0].Groups[1].Value
Write-Step "Built aaltocam $version"

if ($NoCopy) {
    Write-Host ''
    Write-Step 'Done, not published'
    Write-Host "  Build: $dist"
    return
}

# --- publish ---------------------------------------------------------------

$name   = "aaltocam-$version-win64"
$target = Join-Path $Publish $name
if (Test-Path $target) {
    Write-Note "Replacing the existing $name"
    Remove-Item -Recurse -Force $target
}
Write-Step "Publishing to $target"
Copy-Item -Recurse $dist $target

if ($Zip) {
    $archive = "$target.zip"
    if (Test-Path $archive) { Remove-Item -Force $archive }
    Write-Step 'Compressing'
    Compress-Archive -Path (Join-Path $target '*') -DestinationPath $archive
    Write-Note ("{0}  ({1:N0} MB)" -f $archive, ((Get-Item $archive).Length / 1MB))
}

$files = Get-ChildItem $target -Recurse -File
Write-Host ''
Write-Step 'Done'
Write-Host "  Version:   $version"
Write-Host "  Published: $target"
Write-Host ("  Contents:  {0} files, {1:N0} MB" -f $files.Count,
            (($files | Measure-Object Length -Sum).Sum / 1MB))
Write-Host ''
Write-Host '  Copy that whole folder to the mill PC and run aaltocam.exe inside it.' -ForegroundColor DarkGray
Write-Host '  Try it here with:' -ForegroundColor DarkGray
Write-Host "    & '$(Join-Path $target 'aaltocam.exe')'" -ForegroundColor DarkGray
