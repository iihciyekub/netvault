[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = (
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
)

$RepoUrl = if ($env:NETVAULT_UPDATE_URL) {
    $env:NETVAULT_UPDATE_URL
} else {
    "https://github.com/iihciyekub/netvault.git"
}
$ReleaseTag = $env:NETVAULT_RELEASE_TAG
$UvInstallUrl = if ($env:NETVAULT_UV_INSTALL_URL) {
    $env:NETVAULT_UV_INSTALL_URL
} else {
    "https://astral.sh/uv/install.ps1"
}

function Find-Uv {
    $Command = Get-Command uv -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    $Candidates = @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $HOME ".cargo\bin\uv.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return $Candidate
        }
    }
    return $null
}

function Get-GitHubSlug([string]$Url) {
    $Normalized = $Url -replace "^git\+", ""
    $Normalized = $Normalized -replace "\.git/?$", ""
    $Normalized = $Normalized.TrimEnd("/")

    if ($Normalized -match "^https://github\.com/(?<slug>[^/\s]+/[^/\s]+)$") {
        return $Matches.slug
    }
    if ($Normalized -match "^git@github\.com:(?<slug>[^/\s]+/[^/\s]+)$") {
        return $Matches.slug
    }
    return $null
}

function Assert-LastExitCode([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE."
    }
}

function Invoke-WithRetry([scriptblock]$Operation, [string]$Action) {
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        try {
            $Result = & $Operation
            return $Result
        } catch {
            if ($Attempt -eq 3) {
                throw "$Action failed after $Attempt attempts: $($_.Exception.Message)"
            }
            Start-Sleep -Seconds $Attempt
        }
    }
}

function Get-UvToolBinDir([string]$UvExecutable) {
    $Output = & $UvExecutable tool dir --bin 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Resolved = ([string]($Output | Select-Object -Last 1)).Trim()
        if ($Resolved) {
            return $Resolved
        }
    }
    if ($env:UV_TOOL_BIN_DIR) {
        return $env:UV_TOOL_BIN_DIR
    }
    if ($env:XDG_BIN_HOME) {
        return $env:XDG_BIN_HOME
    }
    return (Join-Path $HOME ".local\bin")
}

$UvPath = Find-Uv
if (-not $UvPath) {
    Write-Host "uv was not found. Installing uv from $UvInstallUrl ..."
    $UvInstallerPath = Join-Path ([IO.Path]::GetTempPath()) ("netvault-uv-install-{0}.ps1" -f [Guid]::NewGuid())
    try {
        Invoke-WithRetry {
            Invoke-WebRequest `
                -Uri $UvInstallUrl `
                -OutFile $UvInstallerPath `
                -TimeoutSec 120 `
                -UseBasicParsing
        } "uv installer download"
        & $UvInstallerPath
        Assert-LastExitCode "uv installation"
    } finally {
        Remove-Item $UvInstallerPath -Force -ErrorAction SilentlyContinue
    }
    $UvPath = Find-Uv
}
if (-not $UvPath) {
    throw "uv was installed but could not be located. Open a new PowerShell window and run this installer again."
}

$GitHubSlug = Get-GitHubSlug $RepoUrl
if (-not $ReleaseTag -and $GitHubSlug) {
    $Headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "NetVault installer"
    }
    $Release = Invoke-WithRetry {
        Invoke-RestMethod `
            -Uri "https://api.github.com/repos/$GitHubSlug/releases/latest" `
            -Headers $Headers `
            -TimeoutSec 120
    } "latest NetVault release lookup"
    $ReleaseTag = $Release.tag_name
    if (-not $ReleaseTag) {
        throw "GitHub did not return a latest release tag for $GitHubSlug."
    }
}
if ($ReleaseTag -and $ReleaseTag -notmatch "^v\d+\.\d+\.\d+$") {
    throw "Invalid NetVault release tag: $ReleaseTag"
}

if ($GitHubSlug -and $ReleaseTag) {
    $ReleaseVersion = $ReleaseTag.Substring(1)
    $PackageUrl = "https://github.com/$GitHubSlug/releases/download/$ReleaseTag/netvault-$ReleaseVersion-py3-none-any.whl"
} else {
    $Suffix = if ($ReleaseTag) { "@$ReleaseTag" } else { "" }
    $NormalizedRepoUrl = $RepoUrl -replace "^git\+", ""
    $PackageUrl = "git+$NormalizedRepoUrl$Suffix"
}

$SourceLabel = if ($ReleaseTag) { $ReleaseTag } else { "from $RepoUrl" }
Write-Host "Installing NetVault $SourceLabel ..."
& $UvPath tool install --force $PackageUrl
Assert-LastExitCode "NetVault installation"

try {
    & $UvPath tool update-shell
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "NetVault was installed, but PATH could not be updated automatically."
    }
} catch {
    Write-Warning "NetVault was installed, but PATH could not be updated automatically."
}

$ToolBinDir = Get-UvToolBinDir $UvPath
$NvPath = Join-Path $ToolBinDir "nv.exe"
$NetVaultPath = Join-Path $ToolBinDir "netvault.exe"
if (-not (Test-Path $NvPath) -or -not (Test-Path $NetVaultPath)) {
    throw "NetVault was installed, but its CLI commands were not both created in $ToolBinDir."
}

Write-Host ""
Write-Host "NetVault installed."
Write-Host ""
Write-Host "Try:"
Write-Host "  nv login https://iiaide.com/nv"
Write-Host "  nv list"
Write-Host ""
Write-Host "If nv is not available in this PowerShell window yet, open a new one and try again."

$InstalledVersion = (& $NvPath --version | Select-Object -Last 1)
Assert-LastExitCode "NetVault version check"
Write-Host $InstalledVersion
if ($ReleaseTag -and ([string]$InstalledVersion) -notlike "*$($ReleaseTag.Substring(1))*") {
    throw "Expected NetVault $($ReleaseTag.Substring(1)), but the installed command reported: $InstalledVersion"
}
