[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot ".runtime-logs"
$startupLog = Join-Path $runtimeDir "startup.log"
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$script:startupFailed = $false

function Write-StartupLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $startupLog -Value "$timestamp $Message"
}

function Test-ListeningPort {
    param([int]$Port)

    return [bool](
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )
}

function Wait-ListeningPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-ListeningPort -Port $Port) {
            return $true
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Test-HttpEndpoint {
    param([string]$Uri)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Test-ConfiguredHostAddress {
    $entry = @(Select-String -LiteralPath (Join-Path $projectRoot '.env') -Pattern '^DOMOS_HOST_IP=')
    if ($entry.Count -ne 1) {
        Write-StartupLog 'ERROR: root .env must contain exactly one DOMOS_HOST_IP.'
        return $false
    }
    $configuredHost = $entry[0].Line.Split('=', 2)[1].Trim().Trim('"').Trim("'")
    $localAddress = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $configuredHost -ErrorAction SilentlyContinue |
        Where-Object AddressState -eq Preferred
    if (-not $localAddress) {
        Write-StartupLog 'ERROR: DOMOS_HOST_IP is not assigned to this PC. ESP32 cannot reach its configured gateway. Run scripts/repair-domos-network.ps1 -Apply as Administrator or reserve this address on the router.'
        return $false
    }
    return $true
}

function Find-Executable {
    param(
        [string]$Name,
        [string]$Fallback
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    if ($Fallback -and (Test-Path -LiteralPath $Fallback)) {
        return $Fallback
    }
    return $null
}

function Test-DockerEngine {
    param([string]$DockerPath)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $DockerPath info *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Invoke-DockerCommand {
    param(
        [string]$DockerPath,
        [string[]]$Arguments
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $output = & $DockerPath @Arguments 2>&1
        return @{
            ExitCode = $LASTEXITCODE
            Output   = $output
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Start-HiddenService {
    param(
        [string]$Name,
        [int]$Port,
        [string]$Executable,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$LogPrefix
    )

    if (Test-ListeningPort -Port $Port) {
        Write-StartupLog "$Name already listens on port $Port; skipped."
        return
    }

    $stdout = Join-Path $runtimeDir "$LogPrefix.stdout.log"
    $stderr = Join-Path $runtimeDir "$LogPrefix.stderr.log"
    $startParameters = @{
        FilePath               = $Executable
        WorkingDirectory       = $WorkingDirectory
        WindowStyle            = "Hidden"
        RedirectStandardOutput = $stdout
        RedirectStandardError  = $stderr
    }
    if ($Arguments) {
        $startParameters.ArgumentList = $Arguments
    }
    Start-Process @startParameters | Out-Null

    if (Wait-ListeningPort -Port $Port -TimeoutSeconds 30) {
        Write-StartupLog "$Name started on port $Port."
    } else {
        $script:startupFailed = $true
        Write-StartupLog "$Name did not open port $Port; inspect $stderr."
    }
}

Write-StartupLog "DomOS startup requested."

# PostgreSQL and Mosquitto are the only infrastructure services. The Go
# backend is deliberately excluded here so a stale container cannot run in
# parallel with the native backend on another port.
$dockerPath = Find-Executable `
    -Name "docker.exe" `
    -Fallback "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe"
$dockerReady = $false

if ($dockerPath) {
    $dockerReady = Test-DockerEngine -DockerPath $dockerPath

    if (-not $dockerReady) {
        $dockerDesktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
        if (Test-Path -LiteralPath $dockerDesktop) {
            Start-Process `
                -FilePath $dockerDesktop `
                -ArgumentList "--minimized" `
                -WindowStyle Hidden | Out-Null
            Write-StartupLog "Waiting for Docker Desktop."

            $deadline = (Get-Date).AddSeconds(120)
            do {
                Start-Sleep -Seconds 3
                $dockerReady = Test-DockerEngine -DockerPath $dockerPath
            } while (-not $dockerReady -and (Get-Date) -lt $deadline)
        }
    }
}

$postgresReady = Test-ListeningPort -Port 5432
if ($dockerReady) {
    $composeFile = Join-Path $projectRoot "backend-go\docker-compose.yml"
    # Docker Desktop may revive an old backend container because it uses
    # restart: unless-stopped. DomOS runs the native Go binary locally, so
    # explicitly stop that duplicate before starting shared infrastructure.
    $stopBackendResult = Invoke-DockerCommand `
        -DockerPath $dockerPath `
        -Arguments @("compose", "-f", $composeFile, "stop", "backend")
    if ($stopBackendResult.ExitCode -ne 0) {
        Write-StartupLog "Unable to stop the duplicate Go backend container."
    }

    $composeResult = Invoke-DockerCommand `
        -DockerPath $dockerPath `
        -Arguments @("compose", "-f", $composeFile, "up", "-d", "postgres", "mosquitto")
    Add-Content -LiteralPath $startupLog -Value ($composeResult.Output | Out-String)
    if ($composeResult.ExitCode -ne 0) {
        Write-StartupLog "Docker Compose failed with exit code $($composeResult.ExitCode)."
    }

    $deadline = (Get-Date).AddSeconds(60)
    do {
        $readyResult = Invoke-DockerCommand `
            -DockerPath $dockerPath `
            -Arguments @(
                "compose", "-f", $composeFile, "exec", "-T",
                "postgres", "pg_isready", "-U", "domos"
            )
        $postgresReady = $readyResult.ExitCode -eq 0
        if (-not $postgresReady) {
            Start-Sleep -Seconds 2
        }
    } while (-not $postgresReady -and (Get-Date) -lt $deadline)

    if ($postgresReady) {
        Write-StartupLog "PostgreSQL is ready on port 5432."
    } else {
        Write-StartupLog "PostgreSQL did not become ready; Go backend was not started."
    }

    if (Wait-ListeningPort -Port 1883 -TimeoutSeconds 30) {
        Write-StartupLog "Mosquitto is ready on port 1883."
    } else {
        Write-StartupLog "Mosquitto did not open port 1883."
    }
} else {
    Write-StartupLog "Docker is unavailable; PostgreSQL and Mosquitto were not started."
}

$goDir = Join-Path $projectRoot "backend-go"
$goExecutable = Join-Path $goDir "server.exe"
if ($postgresReady) {
    $goSources = Get-ChildItem -LiteralPath $goDir -Recurse -File -Filter "*.go"
    $latestSource = $goSources |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    $needsBuild = -not (Test-Path -LiteralPath $goExecutable)
    if (-not $needsBuild -and $latestSource) {
        $needsBuild = $latestSource.LastWriteTimeUtc -gt (
            Get-Item -LiteralPath $goExecutable
        ).LastWriteTimeUtc
    }

    if ($needsBuild) {
        $goPath = Find-Executable -Name "go.exe" -Fallback ""
        if ($goPath) {
            Write-StartupLog "Building the Go backend because sources are newer."
            $previousPreference = $ErrorActionPreference
            Push-Location -LiteralPath $goDir
            try {
                $ErrorActionPreference = "SilentlyContinue"
                $buildOutput = & $goPath build -o $goExecutable .\cmd\server 2>&1
                $buildExitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousPreference
                Pop-Location
            }
            Add-Content -LiteralPath $startupLog -Value ($buildOutput | Out-String)
            if ($buildExitCode -ne 0) {
                Write-StartupLog "Go backend build failed."
                $goExecutable = $null
            }
        } else {
            Write-StartupLog "Go compiler is unavailable for the required rebuild."
            $goExecutable = $null
        }
    }

    if ($goExecutable -and (Test-Path -LiteralPath $goExecutable)) {
        Start-HiddenService `
            -Name "Go Core Backend" `
            -Port 8081 `
            -Executable $goExecutable `
            -Arguments "" `
            -WorkingDirectory $goDir `
            -LogPrefix "backend-go"
    } else {
        Write-StartupLog "Go backend executable is unavailable."
    }
}

$pythonDir = Join-Path $projectRoot "backend-python"
$pythonExecutable = Join-Path $pythonDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $pythonExecutable) {
    Start-HiddenService `
        -Name "Python Voice Gateway" `
        -Port 8000 `
        -Executable $pythonExecutable `
        -Arguments "-m uvicorn main:app --host 0.0.0.0 --port 8000" `
        -WorkingDirectory $pythonDir `
        -LogPrefix "backend-python"
} else {
    Write-StartupLog "Python virtual environment is unavailable."
}

$dashboardDir = Join-Path $projectRoot "dashboard-next"
$nodePath = Find-Executable -Name "node.exe" -Fallback "$env:ProgramFiles\nodejs\node.exe"
$nextCli = Join-Path $dashboardDir "node_modules\next\dist\bin\next"
$nextBuild = Join-Path $dashboardDir ".next\BUILD_ID"
if ($nodePath -and (Test-Path -LiteralPath $nextCli) -and (Test-Path -LiteralPath $nextBuild)) {
    Start-HiddenService `
        -Name "Next.js Dashboard" `
        -Port 3000 `
        -Executable $nodePath `
        -Arguments "`"$nextCli`" start --hostname 0.0.0.0 --port 3000" `
        -WorkingDirectory $dashboardDir `
        -LogPrefix "dashboard-next"
} else {
    Write-StartupLog "Dashboard production build is unavailable; run npm install and npm run build."
}

# Port checks alone do not prove that the ESP32's configured address exists.
# Report failure to Task Scheduler so its configured retries actually run.
if (-not (Test-ConfiguredHostAddress)) { $script:startupFailed = $true }
foreach ($endpoint in @(
    'http://127.0.0.1:8000/health',
    'http://127.0.0.1:8081/healthz',
    'http://127.0.0.1:3000'
)) {
    if (-not (Test-HttpEndpoint -Uri $endpoint)) {
        $script:startupFailed = $true
        Write-StartupLog "ERROR: HTTP health check failed for $endpoint."
    }
}
if (-not $postgresReady -or -not (Test-ListeningPort -Port 1883)) {
    $script:startupFailed = $true
}
if ($script:startupFailed) {
    Write-StartupLog 'DomOS startup incomplete; check the errors above.'
    exit 1
}
Write-StartupLog "DomOS startup finished; host address and HTTP health checks passed."
exit 0
