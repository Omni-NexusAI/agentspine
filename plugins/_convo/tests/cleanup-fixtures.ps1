param([switch]$Apply)
$ErrorActionPreference = 'Stop'
# This is a reviewed exact-target cleanup, not a general Docker pruning tool.
$keepName = 'convo-test-v211-doctor-20260908'
$retireNames = @(
    'convo-test-standard-doctor-20260908',
    'convo-test-v211-recovery-20260908',
    'convo-test-standard-recovery3-20260908',
    'convo-test-standard-recovery2-20260908',
    'convo-test-standard-recovery-20260908',
    'convo-probe-standard-20260908'
)
$inspect = docker inspect --size $keepName @retireNames | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or @($inspect).Count -ne 7) {
    throw 'The reviewed fixture set changed. Nothing was removed; audit it again.'
}
$approved = @()
foreach ($item in $inspect) {
    $name = $item.Name.TrimStart('/')
    if ($item.State.Running -or $item.State.Paused -or $item.HostConfig.NetworkMode -ne 'none') {
        throw "Fixture $name is active or no longer isolated. Nothing was removed."
    }
    if (@($item.Mounts | Where-Object { $_.Type -ne 'bind' -or $_.RW }).Count) {
        throw "Fixture $name has persisted or writable mounts. Nothing was removed."
    }
    if ($name -eq $keepName) { continue }
    if ($name -notin $retireNames) { throw 'Unexpected target. Nothing was removed.' }
    $approved += [pscustomobject]@{
        name=$name; id=$item.Id; image=$item.Image; status=$item.State.Status
        exit_code=$item.State.ExitCode; writable_bytes=$item.SizeRw
        command=$item.Config.Cmd; mounts=@($item.Mounts | Select-Object Type,Source,Destination,RW)
    }
}
if ($approved.Count -ne 6) { throw 'Incomplete reviewed fixture set. Nothing was removed.' }
$approved | Select-Object name,status,exit_code,writable_bytes | Format-Table
Write-Host "Keep: $keepName. Images, volumes, networks and other containers are not targets."
if (!$Apply) { Write-Host 'Preview only; pass -Apply for the user-approved exact fixture cleanup.'; exit 0 }
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../../'))
# The owning checkout is beneath C:\speech-to-speech\.runtime. Resolve the
# operational record within it; never use a broad workspace for deletion.
$recordRoot = Join-Path $workspace 'convo-fixture-recovery-20261014'
if ((Get-PSDrive -Name C).Free -lt 1GB) { throw 'Insufficient room for bounded recovery records.' }
$record = New-Item -ItemType Directory -Path $recordRoot -Force
if ($record.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Recovery path must not be a reparse point.' }
[IO.File]::WriteAllText((Join-Path $record.FullName 'inventory.json'), ($approved | ConvertTo-Json -Depth 8))
foreach ($item in $approved) {
    $log = docker logs --tail 300 $item.id 2>&1 | Out-String
    if ($log.Length -gt 1048576) { $log = $log.Substring($log.Length - 1048576) }
    [IO.File]::WriteAllText((Join-Path $record.FullName ($item.name + '.log')), $log)
}
foreach ($item in $approved) {
    # Revalidate identity and mounts immediately before the non-forced removal.
    $now = docker inspect $item.id | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $now[0].Name.TrimStart('/') -ne $item.name -or
        $now[0].State.Running -or @($now[0].Mounts | Where-Object { $_.Type -ne 'bind' -or $_.RW }).Count) {
        throw "Fixture $($item.name) changed; remaining cleanup stopped."
    }
    docker rm $item.id
    if ($LASTEXITCODE -ne 0) { throw "Removal failed for $($item.name); no force or broader cleanup attempted." }
}
Write-Host "Retired six fixtures. Bounded metadata/log recovery record: $($record.FullName)"
Write-Host 'Container objects were removed; source mounts and shared images remain available for recreation.'
