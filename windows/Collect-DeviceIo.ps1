#requires -Version 5.1
<#
.SYNOPSIS
    Read-only Windows device collector for ark-device-io.

.DESCRIPTION
    Emits exactly one JSON object with schema "ark.device-io.raw.v1". Nothing is
    written, removed or installed: the script only reads CIM/PnP state.

    All interpretation (interface letters, board grouping, driver family) lives
    in Python (device_io/backends/windows.py), so this script stays a thin,
    reviewable observation layer.

.PARAMETER Json
    Emit the raw JSON document. Accepted for an explicit contract with the
    Python backend; JSON is the only output format this script produces.

.PARAMETER MaxDevices
    Safety valve for pathological hosts. 0 (default) means "no limit".

.EXAMPLE
    pwsh -NoProfile -File windows/Collect-DeviceIo.ps1 -Json
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [ValidateRange(0, 200000)]
    [int]$MaxDevices = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ArkBeijingNow {
    return (Get-Date).ToUniversalTime().AddHours(8).ToString('yyyy-MM-ddTHH:mm:ss.fff')
}

function Test-ArkBool {
    param([object]$Value)
    if ($null -eq $Value) { return $false }
    if ($Value -is [bool]) { return [bool]$Value }
    return $false
}

function Get-ArkPropertyValue {
    param([string]$InstanceId, [string]$KeyName)
    try {
        $item = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName $KeyName -ErrorAction Stop
        if ($null -eq $item) { return '' }
        $data = $item.Data
        if ($null -eq $data) { return '' }
        if ($data -is [array]) {
            if ($data.Length -eq 0) { return '' }
            return [string]$data[0]
        }
        return [string]$data
    }
    catch {
        return ''
    }
}

function Get-ArkPropertyList {
    param([string]$InstanceId, [string]$KeyName)
    try {
        $item = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName $KeyName -ErrorAction Stop
        if ($null -eq $item -or $null -eq $item.Data) { return @() }
        $values = @()
        foreach ($value in @($item.Data)) {
            $text = [string]$value
            if ($text) { $values += $text }
        }
        return $values
    }
    catch {
        return @()
    }
}

# CIM/PnP cmdlets only exist on Windows. Fail loudly and cheaply instead of
# emitting a half-empty document that would look like "no devices connected".
if ($env:OS -ne 'Windows_NT') {
    [Console]::Error.WriteLine(
        'ark.device-io: Collect-DeviceIo.ps1 requires Windows; Get-CimInstance/Get-PnpDevice are unavailable on this host.'
    )
    exit 3
}

$required = @('Get-CimInstance', 'Get-PnpDevice', 'Get-PnpDeviceProperty')
foreach ($name in $required) {
    if (-not (Get-Command -Name $name -ErrorAction SilentlyContinue)) {
        [Console]::Error.WriteLine('ark.device-io: required cmdlet not found: ' + $name)
        exit 3
    }
}

$warnings = @()
if (-not $Json) {
    Write-Warning 'Collect-DeviceIo.ps1 always emits JSON; -Json is implied.'
}

# Base index from CIM: Service, Manufacturer, Description, HardwareID are not
# exposed by Get-PnpDevice, so both views are joined on the instance id.
$entities = @{}
try {
    foreach ($entity in @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop)) {
        $entityId = [string]$entity.PNPDeviceID
        if ($entityId) {
            $entities[$entityId.ToUpperInvariant()] = $entity
        }
    }
}
catch {
    $warnings += ('Win32_PnPEntity query failed: ' + $_.Exception.Message)
}

$deviceRows = @()
$serialRows = @()
$audioRows = @()

try {
    $pnpDevices = @(Get-PnpDevice -ErrorAction Stop)
}
catch {
    [Console]::Error.WriteLine('ark.device-io: Get-PnpDevice failed: ' + $_.Exception.Message)
    exit 4
}

$seen = 0
foreach ($device in $pnpDevices) {
    $instanceId = [string]$device.InstanceId
    if (-not $instanceId) { continue }
    if ($MaxDevices -gt 0 -and $seen -ge $MaxDevices) {
        $warnings += ('device limit {0} reached; remaining devices were not read' -f $MaxDevices)
        break
    }
    $seen++

    $entity = $null
    $key = $instanceId.ToUpperInvariant()
    if ($entities.ContainsKey($key)) { $entity = $entities[$key] }

    $status = ''
    if ($null -ne $entity) { $status = [string]$entity.Status }
    if (-not $status) { $status = [string]$device.Status }

    $className = [string]$device.Class
    if (-not $className -and $null -ne $entity) { $className = [string]$entity.PNPClass }

    $friendlyName = [string]$device.FriendlyName
    if (-not $friendlyName -and $null -ne $entity) { $friendlyName = [string]$entity.Name }

    $service = ''
    $manufacturer = ''
    $description = ''
    $hardwareIds = @()
    $containerFromCim = ''
    if ($null -ne $entity) {
        $service = [string]$entity.Service
        $manufacturer = [string]$entity.Manufacturer
        $description = [string]$entity.Description
        $hardwareIds = @($entity.HardwareID | ForEach-Object { [string]$_ })
        $containerFromCim = [string]$entity.ContainerID
    }
    if (-not $service) { $service = Get-ArkPropertyValue $instanceId 'DEVPKEY_Device_Service' }
    if ($hardwareIds.Length -eq 0) {
        $hardwareIds = @(Get-ArkPropertyList $instanceId 'DEVPKEY_Device_HardwareIds')
    }

    $containerId = Get-ArkPropertyValue $instanceId 'DEVPKEY_Device_ContainerId'
    if (-not $containerId) { $containerId = $containerFromCim }
    $parent = Get-ArkPropertyValue $instanceId 'DEVPKEY_Device_Parent'
    $location = Get-ArkPropertyValue $instanceId 'DEVPKEY_Device_LocationInfo'

    $present = $false
    if ($device.PSObject.Properties.Name -contains 'Present') {
        $present = Test-ArkBool $device.Present
    }

    $deviceRows += [pscustomobject][ordered]@{
        instance_id  = $instanceId
        class        = $className
        friendly_name = $friendlyName
        status       = $status
        present      = $present
        service      = $service
        parent       = $parent
        container_id = $containerId
        location     = $location
        hardware_ids = @($hardwareIds)
        manufacturer = $manufacturer
        device_desc  = $description
    }

    # Serial ports: a COM name either in the friendly name or in the dedicated
    # PortName property. This is a pure read; the port is not opened.
    $comName = ''
    $comMatch = [regex]::Match($friendlyName, '(COM\d+)', 'IgnoreCase')
    if ($comMatch.Success) {
        $comName = $comMatch.Groups[1].Value.ToUpperInvariant()
    }
    if (-not $comName) {
        $portName = Get-ArkPropertyValue $instanceId 'DEVPKEY_Device_PortName'
        $portMatch = [regex]::Match($portName, '(COM\d+)', 'IgnoreCase')
        if ($portMatch.Success) { $comName = $portMatch.Groups[1].Value.ToUpperInvariant() }
    }
    if ($comName -and $className -eq 'Ports') {
        $serialRows += [pscustomobject][ordered]@{
            name        = $comName
            instance_id = $instanceId
            status      = $status
            device_desc = $description
        }
    }

    if ($className -eq 'AudioEndpoint') {
        $audioRows += [pscustomobject][ordered]@{
            instance_id = $instanceId
            name        = $friendlyName
            state       = $status
            class       = $className
        }
    }
}

$document = [ordered]@{
    schema       = 'ark.device-io.raw.v1'
    os           = 'windows'
    generated_at = (Get-ArkBeijingNow)
    devices      = @($deviceRows)
    serial_ports = @($serialRows)
    audio        = @($audioRows)
    warnings     = @($warnings)
}

ConvertTo-Json -InputObject $document -Depth 8 -Compress
