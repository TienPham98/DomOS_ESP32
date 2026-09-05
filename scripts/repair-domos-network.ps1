[CmdletBinding(SupportsShouldProcess)]
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$repairLog = Join-Path $runtimeDir 'network-repair.log'

function Write-RepairLog([string]$Message) {
    Add-Content -LiteralPath $repairLog -Value "$(Get-Date -Format s) $Message"
    Write-Output $Message
}

function Test-SameSubnet([ipaddress]$Left, [ipaddress]$Right, [int]$PrefixLength) {
    $leftBytes = $Left.GetAddressBytes()
    $rightBytes = $Right.GetAddressBytes()
    for ($i = 0; $i -lt 4; $i++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $PrefixLength - 8 * $i))
        $mask = (255 -shl (8 - $bits)) -band 255
        if (($leftBytes[$i] -band $mask) -ne ($rightBytes[$i] -band $mask)) {
            return $false
        }
    }
    return $true
}

function Invoke-Netsh([string[]]$NetshArguments) {
    $result = & "$env:SystemRoot\System32\netsh.exe" @NetshArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Network configuration failed: $result"
    }
}

try {
    $entry = @(Select-String -LiteralPath (Join-Path $projectRoot '.env') -Pattern '^DOMOS_HOST_IP=')
    if ($entry.Count -ne 1) { throw 'Expected exactly one DOMOS_HOST_IP in root .env.' }
    $targetText = $entry[0].Line.Split('=', 2)[1].Trim().Trim('"').Trim("'")
    $target = [ipaddress]::Parse($targetText)
    $bytes = $target.GetAddressBytes()
    if ($bytes.Length -ne 4 -or -not (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )) { throw 'DOMOS_HOST_IP must be a private IPv4 LAN address.' }

    $physicalIndices = @(Get-NetAdapter -Physical | Where-Object Status -eq Up |
        Select-Object -ExpandProperty InterfaceIndex)
    $candidates = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.InterfaceIndex -in $physicalIndices -and $_.AddressState -eq 'Preferred' -and
        (Test-SameSubnet ([ipaddress]$_.IPAddress) $target $_.PrefixLength)
    })
    if (@($candidates.InterfaceIndex | Select-Object -Unique).Count -ne 1) {
        throw 'Cannot uniquely identify a physical LAN adapter on the configured subnet.'
    }
    $adapterAddress = $candidates | Sort-Object SkipAsSource | Select-Object -First 1
    $index = $adapterAddress.InterfaceIndex
    $targetAssignments = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $targetText `
        -ErrorAction SilentlyContinue)
    $activeAssignment = @($targetAssignments | Where-Object {
        $_.InterfaceIndex -eq $index -and $_.AddressState -eq 'Preferred'
    })
    if ($activeAssignment.Count) {
        Write-RepairLog 'The configured DomOS host address is already active.'
        exit 0
    }
    $staleAssignments = @($targetAssignments | Where-Object {
        $_.InterfaceIndex -ne $index
    } | Select-Object InterfaceIndex,PrefixLength,SkipAsSource)
    Write-RepairLog "The configured DomOS address is missing on $($adapterAddress.InterfaceAlias)."
    if (-not $Apply) {
        Write-RepairLog 'Check only. Run this script with -Apply as Administrator to add the configured address.'
        exit 2
    }
    if (-not $PSCmdlet.ShouldProcess($adapterAddress.InterfaceAlias,
        'Persist the DomOS address from .env alongside DHCP')) { exit 0 }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Windows Administrator permission is required to restore the DomOS LAN address.'
    }

    # ARP checks the exact destination on this LAN, including peers that ignore ping.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DomOsAddressProbe {
    [DllImport("iphlpapi.dll", ExactSpelling = true)]
    public static extern int SendARP(uint destination, uint source, byte[] mac, ref uint length);
}
'@
    $mac = New-Object byte[] 6
    [uint32]$macLength = 6
    $probe = [DomOsAddressProbe]::SendARP(
        [BitConverter]::ToUInt32($bytes, 0),
        [BitConverter]::ToUInt32(([ipaddress]$adapterAddress.IPAddress).GetAddressBytes(), 0),
        $mac, [ref]$macLength)
    if ($probe -eq 0) { throw 'The configured host address is already in use by another LAN device.' }
    if ($probe -ne 67) { throw "Address conflict check was inconclusive (ARP error $probe)." }

    $before = Get-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4
    $beforeDns = @((Get-DnsClientServerAddress -InterfaceIndex $index `
        -AddressFamily IPv4).ServerAddresses)
    $beforeDefaultRoutes = @(Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 `
        -DestinationPrefix '0.0.0.0/0' | Sort-Object NextHop,RouteMetric |
        ForEach-Object { "$($_.NextHop)|$($_.RouteMetric)|$($_.Protocol)" })
    $state = @{
        InterfaceIndex = $index
        Dhcp = [string]$before.Dhcp
        Addresses = @($candidates | Select-Object IPAddress,PrefixLength,SkipAsSource)
        Dns = $beforeDns
        DefaultRoutes = $beforeDefaultRoutes
        PreviousDomOsAssignments = $staleAssignments
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runtimeDir 'network-before-repair.json')

    $addressAdded = $false
    $removedAssignments = @()
    try {
        # Enable coexistence before adding the secondary address: ordinary
        # static configuration disables DHCP. Neither command supplies a
        # gateway or DNS server.
        foreach ($store in @('persistent', 'active')) {
            Invoke-Netsh @('interface','ipv4','set','interface',"interface=$index",
                'dhcpstaticipcoexistence=enabled',"store=$store")
        }
        # Move only the DomOS alias away from disconnected adapters; preserve
        # all primary addresses and every adapter's DHCP, gateway and DNS.
        foreach ($assignment in $staleAssignments) {
            Remove-NetIPAddress -InterfaceIndex $assignment.InterfaceIndex `
                -IPAddress $targetText -Confirm:$false -ErrorAction Stop
            $removedAssignments += $assignment
        }
        Invoke-Netsh @('interface','ipv4','add','address',"name=$index",
            "address=$targetText/$($adapterAddress.PrefixLength)",
            'skipassource=true','store=persistent')
        $addressAdded = $true

        $deadline = (Get-Date).AddSeconds(15)
        do {
            $added = Get-NetIPAddress -InterfaceIndex $index -IPAddress $targetText -ErrorAction SilentlyContinue
            if ($added.AddressState -eq 'Duplicate') { throw 'Windows detected a duplicate LAN address.' }
            if ($added.AddressState -eq 'Preferred') { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        if ($added.AddressState -ne 'Preferred') { throw 'The DomOS address did not become usable.' }
        $after = Get-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4
        if ($before.Dhcp -ne $after.Dhcp) { throw 'DHCP state changed unexpectedly.' }
        $afterDns = @((Get-DnsClientServerAddress -InterfaceIndex $index `
            -AddressFamily IPv4).ServerAddresses)
        $afterDefaultRoutes = @(Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 `
            -DestinationPrefix '0.0.0.0/0' | Sort-Object NextHop,RouteMetric |
            ForEach-Object { "$($_.NextHop)|$($_.RouteMetric)|$($_.Protocol)" })
        if (($beforeDns -join '|') -ne ($afterDns -join '|')) {
            throw 'DNS configuration changed unexpectedly.'
        }
        if (($beforeDefaultRoutes -join '|') -ne ($afterDefaultRoutes -join '|')) {
            throw 'Default route changed unexpectedly.'
        }
    } catch {
        # Only roll back the DomOS aliases changed by this invocation.
        if ($addressAdded) {
            Remove-NetIPAddress -InterfaceIndex $index -IPAddress $targetText `
                -Confirm:$false -ErrorAction SilentlyContinue
        }
        foreach ($assignment in $removedAssignments) {
            Invoke-Netsh @('interface','ipv4','add','address',
                "name=$($assignment.InterfaceIndex)",
                "address=$targetText/$($assignment.PrefixLength)",
                'skipassource=true','store=persistent')
        }
        if ($before.Dhcp -eq 'Enabled') {
            Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Enabled
        }
        throw
    }
    Write-RepairLog 'DomOS LAN address is active and persistent; existing DHCP and DNS are preserved.'
    exit 0
} catch {
    Write-RepairLog "ERROR: $($_.Exception.Message)"
    exit 1
}
