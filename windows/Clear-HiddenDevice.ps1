#requires -Version 5.1
<#
.SYNOPSIS
    Preview (default) or remove Windows ghost USB serial devices.

.DESCRIPTION
    Windows keeps PnP instances for hardware that is no longer attached, so a
    new board can show up on a higher COM number than expected. This script
    lists those absent USB serial ports and, only with -Apply, removes them.

    Safety rules:
      * preview is the default; nothing changes without -Apply;
      * a device that is present, or whose presence is unknown, is never a
        candidate and is re-checked immediately before any removal;
      * only absent (phantom) USB/FTDIBUS/USB2MCOM serial-port instances are
        considered, so an upstream hub or composite parent is never removed;
      * removal uses pnputil with no visible console window.

    Emits exactly one JSON object:
      {"items":[{instance_id,class,friendly_name,parent,reason,removed,error}],
       "summary":{"candidates":N,"removed":N,"failed":N}}

.PARAMETER Json
    Emit JSON. Accepted for an explicit contract with device_io.clearusb; JSON
    is the only output format.

.PARAMETER Apply
    Actually remove the candidates. Requires an elevated session.

.PARAMETER Board
    Limit to one USB identity, e.g. "1a86:7523".

.PARAMETER TimeoutSeconds
    Per-device pnputil timeout.

.EXAMPLE
    pwsh -NoProfile -File windows/Clear-HiddenDevice.ps1 -Json

.EXAMPLE
    pwsh -NoProfile -File windows/Clear-HiddenDevice.ps1 -Json -Apply -Board 1a86:7523
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$Apply,
    [string]$Board = '',
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-ArkAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch {
        return $false
    }
}

function Initialize-ArkConfigurationManager {
    if ('ArkDeviceIo.ConfigurationManager' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace ArkDeviceIo {
    public static class ConfigurationManager {
        private const int CR_SUCCESS = 0;
        private const int CR_BUFFER_SMALL = 0x1A;
        private const int CM_LOCATE_DEVNODE_PHANTOM = 1;

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern int CM_Locate_DevNodeW(out uint deviceNode, string deviceId, int flags);

        [DllImport("cfgmgr32.dll")]
        private static extern int CM_Get_Parent(out uint parentNode, uint deviceNode, int flags);

        [DllImport("cfgmgr32.dll")]
        private static extern int CM_Get_Device_ID_Size(out uint length, uint deviceNode, int flags);

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern int CM_Get_Device_IDW(uint deviceNode, StringBuilder buffer, int bufferLength, int flags);

        public static string GetParent(string requestedId) {
            uint child;
            // CM_LOCATE_DEVNODE_PHANTOM resolves non-present devnodes too, which is
            // exactly what ghost grouping needs.
            if (String.IsNullOrWhiteSpace(requestedId) || CM_Locate_DevNodeW(out child, requestedId, CM_LOCATE_DEVNODE_PHANTOM) != CR_SUCCESS) return null;
            uint parent;
            if (CM_Get_Parent(out parent, child, 0) != CR_SUCCESS) return null;
            uint length;
            if (CM_Get_Device_ID_Size(out length, parent, 0) != CR_SUCCESS) return null;
            for (int attempt = 0; attempt < 2; attempt++) {
                var buffer = new StringBuilder(checked((int)length + 1));
                int result = CM_Get_Device_IDW(parent, buffer, buffer.Capacity, 0);
                if (result == CR_SUCCESS) return buffer.ToString();
                if (result != CR_BUFFER_SMALL || CM_Get_Device_ID_Size(out length, parent, 0) != CR_SUCCESS) return null;
            }
            return null;
        }
    }
}
'@
}

function Get-ArkNativeParent {
    param([string]$InstanceId)
    Initialize-ArkConfigurationManager
    return [ArkDeviceIo.ConfigurationManager]::GetParent($InstanceId)
}

function Get-ArkParent {
    param([string]$InstanceId, [hashtable]$Cache)
    if ($Cache.ContainsKey($InstanceId)) { return [string]$Cache[$InstanceId] }

    $parent = ''
    try { $parent = [string](Get-ArkNativeParent $InstanceId) } catch { $parent = '' }
    if (-not $parent) {
        try {
            # Point lookup only: a bulk property query can attach a parent to the
            # wrong instance on large trees.
            $value = @(Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop)
            $matching = @($value | Where-Object {
                $_.PSObject.Properties.Name -contains 'InstanceId' -and
                [string]::Equals([string]$_.InstanceId, $InstanceId, [StringComparison]::OrdinalIgnoreCase)
            })
            if ($matching.Count -eq 1) { $parent = [string](@($matching[0].Data)[0]) }
        }
        catch { $parent = '' }
    }
    $Cache[$InstanceId] = $parent
    return $parent
}

function Get-ArkHardwareIds {
    param([string]$InstanceId)
    try {
        $item = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName 'DEVPKEY_Device_HardwareIds' -ErrorAction Stop
        if ($null -eq $item -or $null -eq $item.Data) { return @() }
        return @($item.Data | ForEach-Object { [string]$_ })
    }
    catch {
        return @()
    }
}

function Invoke-ArkRemoval {
    param([string]$InstanceId, [int]$Timeout)
    if ($InstanceId -match '["\r\n]' -or $InstanceId.EndsWith('\')) {
        throw 'Invalid device instance identifier.'
    }
    $pnputil = (Get-Command pnputil.exe -ErrorAction Stop).Source
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $pnputil
    $start.Arguments = '/remove-device "' + $InstanceId + '"'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    $started = $false
    try {
        if (-not $process.Start()) { throw 'Could not start pnputil.' }
        $started = $true
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout * 1000)) {
            throw 'pnputil timed out; removal state requires verification.'
        }
        if (-not $stdout.Wait(2000) -or -not $stderr.Wait(2000)) {
            throw 'pnputil output timed out.'
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output   = ($stdout.Result + $stderr.Result).Replace($InstanceId, '[device]')
        }
    }
    finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill()
            [void]$process.WaitForExit(3000)
        }
        $process.Dispose()
    }
}

if ($env:OS -ne 'Windows_NT') {
    [Console]::Error.WriteLine(
        'ark.device-io: Clear-HiddenDevice.ps1 requires Windows; the PnP configuration manager is unavailable on this host.'
    )
    exit 3
}

$boardPattern = ''
if ($Board) {
    $boardMatch = [regex]::Match($Board, '^([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})$')
    if (-not $boardMatch.Success) {
        [Console]::Error.WriteLine('ark.device-io: -Board expects vid:pid (four hex digits each), e.g. 1a86:7523')
        exit 5
    }
    $boardPattern = 'VID_' + $boardMatch.Groups[1].Value + '&PID_' + $boardMatch.Groups[2].Value
}

if ($Apply -and -not (Test-ArkAdministrator)) {
    [Console]::Error.WriteLine('ark.device-io: -Apply requires an elevated PowerShell session; run without -Apply to preview.')
    exit 5
}

try {
    $devices = @(Get-PnpDevice -ErrorAction Stop)
}
catch {
    [Console]::Error.WriteLine('ark.device-io: Get-PnpDevice failed: ' + $_.Exception.Message)
    exit 4
}

$parents = @{}
$items = @()

foreach ($device in $devices) {
    # Unknown presence is never a target: only an explicit False is a ghost.
    if (-not ($device.PSObject.Properties.Name -contains 'Present')) { continue }
    if ($device.Present -isnot [bool]) { continue }
    if ([bool]$device.Present) { continue }

    $instanceId = [string]$device.InstanceId
    if (-not $instanceId) { continue }

    $className = [string]$device.Class
    $isUsbPort = ($className -eq 'Ports') -and ($instanceId -match '^(USB|FTDIBUS|USB2MCOM)\\')
    if (-not $isUsbPort) { continue }

    if ($boardPattern) {
        $blob = $instanceId + ' ' + ((@(Get-ArkHardwareIds $instanceId)) -join ' ')
        if ($blob -notmatch [regex]::Escape($boardPattern)) { continue }
    }

    $parent = ''
    try { $parent = Get-ArkParent $instanceId $parents } catch { $parent = '' }

    $items += [pscustomobject][ordered]@{
        instance_id   = $instanceId
        class         = $className
        friendly_name = [string]$device.FriendlyName
        parent        = $parent
        reason        = 'hidden_usb_port'
        removed       = $false
        error         = $null
    }
}

if ($Apply) {
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items[$index]
        $instanceId = [string]$item.instance_id

        # Re-check presence immediately before the destructive call.
        $current = @(Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue)
        if ($current.Count -eq 0) {
            $item.error = 'instance disappeared before removal'
            continue
        }
        if ($current[0].PSObject.Properties.Name -contains 'Present' -and $current[0].Present -is [bool] -and [bool]$current[0].Present) {
            $item.error = 'device is present; refusing to remove'
            continue
        }

        try {
            $native = Invoke-ArkRemoval $instanceId $TimeoutSeconds
            if ($native.ExitCode -notin @(0, 3010)) {
                throw ('pnputil failed with exit code ' + $native.ExitCode + ': ' + [string]$native.Output)
            }
            $after = @(Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue)
            if ($after.Count -eq 0) {
                $item.removed = $true
            }
            elseif ($native.ExitCode -eq 3010) {
                $item.removed = $false
                $item.error = 'removal requires a reboot; not performed'
            }
            else {
                $item.error = 'instance is still enumerated after removal'
            }
        }
        catch {
            $item.error = $_.Exception.Message
        }
    }
}

$document = [ordered]@{
    items   = @($items)
    summary = [ordered]@{
        candidates = $items.Count
        removed    = @($items | Where-Object { $_.removed }).Count
        failed     = @($items | Where-Object { $null -ne $_.error }).Count
    }
}

ConvertTo-Json -InputObject $document -Depth 8 -Compress
