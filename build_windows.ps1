param(
    [string]$RepoUrl = "",
    [string]$SeedRepoDir = "",
    [string]$RecorderRepoSlug = "tscriba-recorder.git",
    [string]$Branch = "main",
    [string]$ProjectDir = "$env:USERPROFILE\Projects\Transcriba Recorder",
    [string]$PythonLauncher = "py",
    [string]$PythonVersion = "3.12",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not installed or not in PATH."
    }
}

function Get-OriginUrl {
    param([string]$RepoDir)
    if ([string]::IsNullOrWhiteSpace($RepoDir) -or -not (Test-Path $RepoDir)) {
        return ""
    }
    try {
        $url = (git -C $RepoDir remote get-url origin).Trim()
        return $url
    } catch {
        return ""
    }
}

function Build-RepoUrlFromSeed {
    param(
        [string]$SeedOriginUrl,
        [string]$TargetRepoSlug
    )
    if ([string]::IsNullOrWhiteSpace($SeedOriginUrl)) {
        return ""
    }
    if ([string]::IsNullOrWhiteSpace($TargetRepoSlug)) {
        return ""
    }
    if ($SeedOriginUrl -match "^(git@[^:]+:)(.+)/[^/]+$") {
        return "$($Matches[1])$($Matches[2])/$TargetRepoSlug"
    }
    if ($SeedOriginUrl -match "^(https?://[^/]+/.+)/[^/]+$") {
        return "$($Matches[1])/$TargetRepoSlug"
    }
    return ""
}

Write-Host "== Transcriba Recorder (Windows Build) =="
Require-Command "git"
Require-Command $PythonLauncher

if (-not (Test-Path $ProjectDir)) {
    if ([string]::IsNullOrWhiteSpace($RepoUrl) -and -not [string]::IsNullOrWhiteSpace($SeedRepoDir)) {
        $seedOrigin = Get-OriginUrl -RepoDir $SeedRepoDir
        $RepoUrl = Build-RepoUrlFromSeed -SeedOriginUrl $seedOrigin -TargetRepoSlug $RecorderRepoSlug
        if (-not [string]::IsNullOrWhiteSpace($RepoUrl)) {
            Write-Host "Derived Recorder repo URL from seed repo: $RepoUrl"
        }
    }
    if ([string]::IsNullOrWhiteSpace($RepoUrl)) {
        throw "Project directory not found and RepoUrl could not be resolved. Pass -RepoUrl or -SeedRepoDir."
    }
    Write-Host "Cloning repository..."
    git clone --branch $Branch $RepoUrl $ProjectDir
} else {
    Write-Host "Updating repository..."
    git -C $ProjectDir fetch --all --prune
    git -C $ProjectDir checkout $Branch
    git -C $ProjectDir pull --ff-only
}

Set-Location $ProjectDir

if ($Clean) {
    Write-Host "Cleaning previous build artifacts..."
    Remove-Item -Recurse -Force "build" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "dist" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue
}

Write-Host "Creating virtual environment..."
& $PythonLauncher "-$PythonVersion" -m venv .venv

$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python not found at $VenvPython"
}

Write-Host "Installing dependencies..."
& $VenvPython -m pip install --upgrade pip setuptools wheel
& $VenvPython -m pip install -r requirements.txt
& $VenvPython -m pip install pyinstaller

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "Transcriba Recorder",
    "--add-data", "transcriba_theme.json;.",
    "--add-data", "assets;assets",
    "--collect-binaries", "ctranslate2",
    "--collect-binaries", "tokenizers",
    "--collect-submodules", "ctranslate2",
    "--collect-submodules", "tokenizers",
    "--collect-submodules", "pystray",
    "--collect-all", "faster_whisper",
    "--collect-all", "PIL"
)

if (Test-Path "release_manifest.json") {
    $PyInstallerArgs += @("--add-data", "release_manifest.json;.")
}

$PyInstallerArgs += "tscriba_recorder_app.py"

Write-Host "Building Windows app with PyInstaller..."
& $VenvPython -m PyInstaller @PyInstallerArgs

$ExePath = Join-Path $ProjectDir "dist\Transcriba Recorder\Transcriba Recorder.exe"
if (Test-Path $ExePath) {
    Write-Host ""
    Write-Host "BUILD SUCCESS"
    Write-Host "Executable: $ExePath"
} else {
    throw "Build completed but executable not found at expected path: $ExePath"
}
