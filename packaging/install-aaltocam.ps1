<#
.SYNOPSIS
    Install aaltocam and put a shortcut on the desktop.

.DESCRIPTION
    Finds a Python 3.11 or newer, installing one per-user through winget if
    there is none, unpacks aaltocam outside OneDrive, builds a virtual
    environment and installs into it.

    Nothing here needs administrator rights.

    This file lives in the aaltocam source tree, at packaging/install-aaltocam.ps1,
    and ships inside every release tarball. That is deliberate. It used to be
    written afresh alongside each release, which meant the same three
    PowerShell 5.1 bugs came back every time -- a quoted -c probe that finds no
    Python, native stderr aborting the run under 'Stop', and an unpack that
    left stale modules behind to be swept into the wheel. tests/test_installer.py
    now fails if any of the three reappears, so the guard travels with the code
    rather than with anyone's memory. Edit this file, not a copy of it.

    The default shell on the target machine is Windows PowerShell 5.1, where
    pwsh is not installed, so nothing here may rely on PowerShell 7 behaviour.

.PARAMETER Source
    The aaltocam tarball, or a folder already containing pyproject.toml. Defaults
    to the newest aaltocam-*.tar.gz sitting in the AALTOcam folder.

.PARAMETER Root
    Where to put the working copy and the virtual environment. Keep this out of
    OneDrive: a virtual environment is thousands of small files and syncing it
    causes churn and occasional file locks mid-install.

.PARAMETER BuildExe
    Also build a standalone aaltocam.exe with PyInstaller, for copying to a
    machine that has no Python -- the PC next to the mill, for instance.

.EXAMPLE
    .\install-aaltocam.ps1

.EXAMPLE
    .\install-aaltocam.ps1 -Source C:\path\to\aaltocam-0.9.1.tar.gz -BuildExe
#>
[CmdletBinding()]
param(
    [string] $Source,
    [string] $Root = (Join-Path $HOME 'aaltocam-work'),
    [switch] $BuildExe,
    [switch] $NoShortcut
)

$ErrorActionPreference = 'Stop'
$MinimumPython = [version]'3.11'

# PowerShell 5.1 does not define these, and this script has to run there too.
if ($null -eq $IsWindows) { $IsWindows = $true }

function Write-Step { param([string] $Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note { param([string] $Text) Write-Host "    $Text" -ForegroundColor DarkGray }

function Invoke-Native {
    <# Run a native program, treating its stderr as output rather than failure.

       Windows PowerShell 5.1 turns each stderr line from a native program into
       a NativeCommandError once output is redirected, and under
       $ErrorActionPreference = 'Stop' that ends the script mid-run. pip and
       PyInstaller both log progress to stderr, so piping this installer to a
       file would kill the build. A non-zero exit code is the failure worth
       reacting to, so that is what this throws on. #>
    param([Parameter(Mandatory)][scriptblock] $Command, [string] $What = 'A command')
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-PythonVersion {
    <# Version of a python executable, or $null if it will not run.

       Asks with -V rather than -c. Windows PowerShell 5.1 builds a native
       command line without escaping embedded double quotes, so a -c snippet
       containing them arrives at Python truncated and every interpreter on the
       machine looks broken -- the script then tries to winget-install a Python
       that is already there. -V carries no quotes to mangle and prints
       "Python 3.13.15" on anything newer than 3.3. #>
    param([string] $Exe)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # a stub that writes to stderr is a no, not a crash
    try {
        $raw = (& $Exe -V 2>&1 | Out-String)
        $found = [regex]::Match($raw, 'Python\s+(\d+)\.(\d+)')
        if (-not $found.Success) { return $null }
        return [version]('{0}.{1}' -f $found.Groups[1].Value, $found.Groups[2].Value)
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Find-Python {
    <# First interpreter at or above the minimum, searched widest-first. #>
    $candidates = @()

    # The launcher knows about every registered install; ask it for a new one.
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        $previous = $ErrorActionPreference
        # Redirecting a native program's stderr makes 5.1 raise on every line it
        # writes. Here that would be swallowed by the catch below and the
        # launcher would quietly contribute nothing, which is the same bug as
        # the one that used to stop the script outright, only harder to see.
        $ErrorActionPreference = 'Continue'
        try {
            foreach ($tag in '3.13', '3.12', '3.11', '3') {
                try {
                    # No embedded quotes here, so this one survives 5.1 intact.
                    $found = & $launcher.Source "-$tag" -c 'import sys; print(sys.executable)' 2>$null
                    if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found.Trim() }
                } catch { }
            }
        } finally {
            $ErrorActionPreference = $previous
        }
    }

    foreach ($name in 'python3', 'python') {
        Get-Command $name -All -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates += $_.Source }
    }

    if ($IsWindows) {
        # Per-user winget and python.org installs land here.
        $userRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path $userRoot) {
            Get-ChildItem $userRoot -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { $candidates += (Join-Path $_.FullName 'python.exe') }
        }
        # KiCad ships its own. Fine to build a venv from, not to install into.
        Get-ChildItem 'C:\Program Files\KiCad' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { $candidates += (Join-Path $_.FullName 'bin\python.exe') }
    }

    foreach ($exe in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path $exe)) { continue }
        # A Microsoft Store stub is on PATH but is not an interpreter.
        if ($exe -like '*WindowsApps*') { continue }
        $version = Get-PythonVersion $exe
        if ($version -and $version -ge $MinimumPython) {
            return [pscustomobject]@{ Exe = $exe; Version = $version }
        }
    }
    return $null
}

function Install-Python {
    <# Per-user winget install. No administrator rights involved. #>
    if (-not $IsWindows) { throw "No Python $MinimumPython or newer found." }
    if (-not (Get-Command 'winget' -ErrorAction SilentlyContinue)) {
        throw ("No Python $MinimumPython or newer found, and winget is not " +
               "available to install one. Get Python from python.org and " +
               "choose 'Install for me only', then run this script again.")
    }
    Write-Step 'Installing Python 3.13 for this user (no administrator rights needed)'
    Invoke-Native -What 'winget' -Command {
        & winget install --id Python.Python.3.13 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
    }
    # winget updates PATH for new processes, not this one, so search the known
    # per-user location directly rather than relying on the environment.
    $found = Find-Python
    if (-not $found) {
        throw ("Python installed but was not found afterwards. Close this " +
               "window, open a new PowerShell and run the script again.")
    }
    return $found
}

function Resolve-Source {
    param([string] $Given)
    if ($Given) {
        if (-not (Test-Path $Given)) { throw "No such path: $Given" }
        return (Resolve-Path $Given).Path
    }
    $searchDirs = @(
        $PSScriptRoot,
        (Join-Path $HOME 'OneDrive - Aalto University\Software\AALTOcam'),
        (Get-Location).Path
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($dir in $searchDirs) {
        $newest = Get-ChildItem $dir -Filter 'aaltocam-*.tar.gz' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest) { return $newest.FullName }
    }
    throw ("No aaltocam-*.tar.gz found. Pass one with -Source.")
}

function Expand-Source {
    param([string] $Archive, [string] $Destination)
    if (Test-Path (Join-Path $Archive 'pyproject.toml')) {
        return (Resolve-Path $Archive).Path      # already an unpacked tree
    }
    if (-not (Get-Command 'tar' -ErrorAction SilentlyContinue)) {
        throw "tar is needed to unpack $Archive. It ships with Windows 10 and 11."
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    # tar overwrites what it finds but never removes what the new version
    # dropped. A stale module surviving here gets swept into the wheel by
    # setuptools' package discovery and ships -- a subtly wrong build rather
    # than an error. Clear the previous unpack first.
    Get-ChildItem $Destination -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force }

    Write-Step "Unpacking $(Split-Path $Archive -Leaf)"
    Invoke-Native -What 'tar' -Command { & tar -xzf $Archive -C $Destination }

    $tree = Get-ChildItem $Destination -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
        Select-Object -First 1
    if (-not $tree) { throw "No pyproject.toml inside $Archive" }
    return $tree.FullName
}

function Get-AppIcon {
    <# Ask aaltocam to draw its own icon out to a file. One source of truth: the
       same code paints the window icon, so the shortcut cannot end up showing
       a different mark from the running program. #>
    param([string] $Python, [string] $Directory)
    $ico = Join-Path $Directory 'aaltocam.ico'
    # Painting onto a pixmap needs a Qt platform plugin, and there is not always
    # a usable one -- no display over SSH, none in a service session. Try the
    # normal path first, then fall back to Qt's offscreen backend, which draws
    # exactly the same pixels without asking anyone for a window.
    foreach ($platform in @($null, 'offscreen')) {
        $previous = $env:QT_QPA_PLATFORM
        try {
            if ($platform) { $env:QT_QPA_PLATFORM = $platform }
            & $Python -m aaltocam.gui.icons $Directory 2>&1 | Out-Null
            if (Test-Path $ico) { return $ico }
        } catch {
        } finally {
            $env:QT_QPA_PLATFORM = $previous
        }
    }
    return $null
}

function New-Shortcut {
    param([string] $Target, [string] $Name, [string] $IconPath)
    if (-not $IsWindows) { return }
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $link = Join-Path $desktop "$Name.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $Target
        $shortcut.WorkingDirectory = Split-Path $Target -Parent
        $shortcut.Description = 'aaltocam - parametric CAM for PCB milling'
        if ($IconPath -and (Test-Path $IconPath)) { $shortcut.IconLocation = $IconPath }
        $shortcut.Save()
        Write-Note "Desktop shortcut: $link"
    } catch {
        Write-Note "Could not create the desktop shortcut: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------

Write-Step 'Looking for Python 3.11 or newer'
$python = Find-Python
if ($python) {
    Write-Note "$($python.Exe)  ($($python.Version))"
} else {
    $python = Install-Python
    Write-Note "$($python.Exe)  ($($python.Version))"
}

$archive = Resolve-Source $Source
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$Root = (Resolve-Path $Root).Path
$tree = Expand-Source $archive (Join-Path $Root 'src')

$venv = Join-Path $Root '.venv'
$binDir = if ($IsWindows) { 'Scripts' } else { 'bin' }
$exeSuffix = if ($IsWindows) { '.exe' } else { '' }
$venvPython = Join-Path $venv (Join-Path $binDir "python$exeSuffix")

if (-not (Test-Path $venvPython)) {
    Write-Step "Creating the virtual environment in $venv"
    Invoke-Native -What 'venv creation' -Command { & $python.Exe -m venv $venv }
}

Write-Step 'Installing aaltocam and its dependencies'
Write-Note 'PySide6 is about 100 MB, so this takes a minute.'
Invoke-Native -What 'pip' -Command { & $venvPython -m pip install --upgrade pip --quiet }
Invoke-Native -What 'Installation' -Command { & $venvPython -m pip install "$tree[gui]" --upgrade }

$guiExe = Join-Path $venv (Join-Path $binDir "aaltocam-gui$exeSuffix")
$cliExe = Join-Path $venv (Join-Path $binDir "aaltocam$exeSuffix")
foreach ($exe in $guiExe, $cliExe) {
    if (-not (Test-Path $exe)) { throw "Expected $exe after installing, but it is missing." }
}

$iconPath = Get-AppIcon -Python $venvPython -Directory $Root
if (-not $NoShortcut) { New-Shortcut -Target $guiExe -Name 'aaltocam' -IconPath $iconPath }

if ($BuildExe) {
    # A standalone build for a machine with no Python. One folder rather than
    # one file: it starts in a second instead of unpacking Qt to temp on every
    # launch, and it is still just a folder you copy.
    Write-Step 'Building a standalone aaltocam.exe with PyInstaller'
    Invoke-Native -What 'pip' -Command { & $venvPython -m pip install pyinstaller --quiet }
    $launcher = Join-Path $Root 'aaltocam-launcher.py'
    @'
from aaltocam.gui.app import main

main()
'@ | Set-Content -Path $launcher -Encoding utf8

    $iconArg = if ($iconPath) { @('--icon', $iconPath) } else { @() }
    $built = $true
    try {
        # PyInstaller logs progress to stderr; without the guard this dies the
        # moment anyone redirects the installer's output to a file.
        Invoke-Native -What 'PyInstaller' -Command {
            & $venvPython -m PyInstaller --noconfirm --clean --windowed `
                --name aaltocam @iconArg `
                --distpath (Join-Path $Root 'dist') `
                --workpath (Join-Path $Root 'build') `
                --specpath $Root `
                --collect-all shapely `
                --collect-submodules aaltocam `
                $launcher
        }
    } catch {
        $built = $false
        Write-Note "PyInstaller failed: $($_.Exception.Message)"
    }
    if (-not $built) {
        Write-Note 'The virtual environment above still works.'
    } else {
        Write-Note "Standalone build: $(Join-Path $Root 'dist\aaltocam')"
        Write-Note 'Copy that whole folder to the other machine and run aaltocam.exe inside it.'
    }
}

$toolsFile = if ($env:APPDATA) {
    Join-Path $env:APPDATA 'aaltocam\tools.toml'
} else {
    Join-Path $HOME '.config/aaltocam/tools.toml'
}

Write-Host ''
Write-Step 'Done'
Write-Host "  GUI:  $guiExe"
Write-Host "  CLI:  $cliExe"
Write-Host "  Tool library, written on first run: $toolsFile"
Write-Host ''
Write-Host '  Start it with the desktop shortcut, or:' -ForegroundColor DarkGray
Write-Host "    & '$guiExe'" -ForegroundColor DarkGray
