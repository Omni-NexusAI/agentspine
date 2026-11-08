param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$destination = 'C:\tmp\agent-zero-v211-final-candidate-usr\plugins\_convo'
$hostName = 'agent-zero-v2.11'
$details = docker inspect $hostName | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or !$details -or !$details[0].State.Running) { throw 'Verified live host is unavailable.' }
$mount = @($details[0].Mounts | Where-Object { $_.Destination -eq '/a0/usr' })
if ($mount.Count -ne 1 -or !$mount[0].RW -or $mount[0].Type -ne 'bind' -or
    $mount[0].Source.Replace('/','\').TrimEnd('\') -ne 'C:\tmp\agent-zero-v211-final-candidate-usr') {
    throw 'Persistent host mount differs; refused deployment.'
}
foreach ($path in @($destination, (Split-Path $destination), (Split-Path (Split-Path $destination)))) {
    $item = Get-Item -LiteralPath $path
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked deployment destination is not authorized.' }
}
if (!(Test-Path -LiteralPath (Join-Path $destination '.toggle-0')) -or
    (Test-Path -LiteralPath (Join-Path $destination '.toggle-1'))) { throw 'Disable Convo natively before copying source.' }
$protected = @('config.json','data','.toggle-0','.toggle-1','.toggle-default')
$files = @(Get-ChildItem -LiteralPath $source -Recurse -File | Where-Object {
    $relative = [IO.Path]::GetRelativePath($source,$_.FullName)
    $top = ($relative -split '[\\/]')[0]
    $top -notin $protected -and $_.Extension -ne '.pyc' -and $relative -notmatch '(^|[\\/])__pycache__([\\/]|$)'
})
$bytes = ($files | Measure-Object Length -Sum).Sum
if ($bytes -gt 8MB) { throw 'Source growth exceeds this reviewed bounded deployment.' }
if ((Get-PSDrive C).Free -lt (11GB + 2 * $bytes)) { throw 'Insufficient bounded deployment headroom.' }
# Snapshot identifiers only: credentials are not copied to a report or output.
$configs = @('_convo','_enhanced_speech','_kokoro_tts','_whisper_stt','ai_link_bridge') | ForEach-Object {
    $path = Join-Path (Split-Path $destination) "$_\config.json"
    [pscustomobject]@{ Path=$path; Exists=(Test-Path -LiteralPath $path); Hash=if(Test-Path -LiteralPath $path){(Get-FileHash -LiteralPath $path).Hash}else{''} }
}
$data = @(Get-ChildItem -LiteralPath (Join-Path $destination 'data') -Force | Select-Object Name,Mode)
Write-Host "Persistent source restore: $($files.Count) files, $bytes bytes; config/data/toggles excluded."
if (!$Apply) { Write-Host 'Preview only. Use -Apply after tests pass.'; return }
if (Test-Path -LiteralPath (Join-Path $destination 'plugin.yaml')) { throw 'Source already restored; review a scoped update instead of overwriting it.' }
foreach ($file in ($files | Sort-Object @{Expression={if($_.Name -eq 'plugin.yaml'){1}else{0}}},FullName)) {
    $relative = [IO.Path]::GetRelativePath($source,$file.FullName)
    $target = [IO.Path]::GetFullPath((Join-Path $destination $relative))
    if (!$target.StartsWith($destination+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Source path escaped deployment target.' }
    if (Test-Path -LiteralPath $target) { throw "Existing source requires review: $relative" }
    New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target
    if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $target).Hash) { throw "Copy verification failed: $relative" }
}
foreach ($record in $configs) {
    if ((Test-Path -LiteralPath $record.Path) -ne $record.Exists -or
        ($record.Exists -and (Get-FileHash -LiteralPath $record.Path).Hash -ne $record.Hash)) { throw 'A protected configuration changed during source restoration.' }
}
$after = @(Get-ChildItem -LiteralPath (Join-Path $destination 'data') -Force | Select-Object Name,Mode)
if (($data | ConvertTo-Json -Compress) -ne ($after | ConvertTo-Json -Compress)) { throw 'Persistent data layout changed during restore.' }
Write-Host 'RESTORE_PASS: source hashes verified; protected configuration and data layout unchanged. Convo remains OFF; refresh and enable through the native host controls.'
