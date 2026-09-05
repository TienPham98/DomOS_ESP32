# Read-only regression checks; no services or network settings are changed.
$ErrorActionPreference = 'Stop'
$startupPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'start-domos.ps1'
$parseErrors = @()
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $startupPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }

# Import only helper definitions, never the script's startup/exit operations.
$functions = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)
foreach ($function in $functions) {
    . ([scriptblock]::Create($function.Extent.Text))
}
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
function Write-StartupLog { param([string]$Message) }
function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

& {
    function Select-String { [pscustomobject]@{Line='DOMOS_HOST_IP=192.0.2.20'} }
    function Get-NetIPAddress { $null }
    Assert-True (-not (Test-ConfiguredHostAddress)) 'A missing host address must fail even when API ports are open.'
}
& {
    function Select-String { [pscustomobject]@{Line='DOMOS_HOST_IP=192.0.2.20'} }
    function Get-NetIPAddress { [pscustomobject]@{AddressState='Tentative'} }
    Assert-True (-not (Test-ConfiguredHostAddress)) 'An address still undergoing conflict detection must not be treated as ready.'
}
& {
    function Select-String { [pscustomobject]@{Line='DOMOS_HOST_IP=192.0.2.20'} }
    function Get-NetIPAddress { [pscustomobject]@{AddressState='Preferred'} }
    Assert-True (Test-ConfiguredHostAddress) 'An active configured host address should pass.'
}
& {
    function Select-String { }
    Assert-True (-not (Test-ConfiguredHostAddress)) 'Missing .env host configuration should fail.'
}
& {
    function Invoke-WebRequest { throw 'Connection refused' }
    Assert-True (-not (Test-HttpEndpoint 'http://127.0.0.1:8000/health')) 'A failed HTTP request should fail startup validation.'
}
& {
    function Invoke-WebRequest { [pscustomobject]@{StatusCode=503} }
    Assert-True (-not (Test-HttpEndpoint 'http://127.0.0.1:8000/health')) 'An unhealthy HTTP response should fail validation.'
}
& {
    function Invoke-WebRequest { [pscustomobject]@{StatusCode=200} }
    Assert-True (Test-HttpEndpoint 'http://127.0.0.1:8000/health') 'A successful HTTP health check should pass.'
}
& {
    function Test-ListeningPort { $true }
    function Start-Process { throw 'Duplicate service launch attempted.' }
    Start-HiddenService -Name 'already running' -Port 8000
}
'PASS: 8 startup regression checks (no network or service mutations).'
