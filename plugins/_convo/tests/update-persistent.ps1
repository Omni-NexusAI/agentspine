# Scoped, reversible Convo source deployment into an already-installed Agent Zero host.
#   -Target test        the adopted isolated container (default; preview unless -Apply)
#   -Target production  agent-zero-v2.11 (needs -ConfirmProduction agent-zero-v2.11 and your approval)
# Source files only: config.json, data/, toggles and every other plugin are never touched. Convo must be OFF.
# Every replaced or removed file is backed up first; -Rollback restores a backup stamp.
param(
    [ValidateSet('test', 'production')][string]$Target = 'test',
    [switch]$Apply,
    [switch]$Rollback,
    [string]$BackupStamp = '',
    [string]$BaselineManifest = '',
    [string]$ConfirmProduction = '',
    [ValidateSet('_convo', 'convo_d1_decider')][string]$Plugin = '_convo',   # convo_d1_decider: the development decision-model backend that only Convo uses
    [switch]$WebOnly      # allow a running (ON) plugin to receive browser assets only (webui/, extensions/webui/)
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'update-persistent.lib.ps1')

$image = 'sha256:a38802ee1ddb83747836521931a99b8f6af000c586e1176478c533dcb08de8c4'
$targets = @{
    production = @{ Container = 'agent-zero-v2.11'; Bind = 'C:\tmp\agent-zero-v211-final-candidate-usr'; RequireRunning = $true }
    test       = @{ Container = 'agent-zero-system1-local-test-2'; Bind = 'C:\a0-plugins\.local-test\usr'; RequireRunning = $false }
}
$spec = $targets[$Target]
$other = $targets[$(if ($Target -eq 'production') { 'test' } else { 'production' })]
$source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($Plugin -ne '_convo') { $source = (Resolve-Path (Join-Path $PSScriptRoot "..\..\$Plugin")).Path }
$backups = 'C:\speech-to-speech\.runtime\convo-backups'

$details = docker inspect $spec.Container | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or !$details) { throw "Container $($spec.Container) was not found; nothing changed." }
$container = $details[0]
if ($container.Image -ne $image) { throw 'The container image differs from the reviewed host image; refusing.' }
$mount = @($container.Mounts | Where-Object { $_.Destination -eq '/a0/usr' })
if ($mount.Count -ne 1 -or !$mount[0].RW -or $mount[0].Type -ne 'bind') { throw 'The /a0/usr mount is not the expected writable bind; refusing.' }
$bind = $mount[0].Source.Replace('/', '\').TrimEnd('\')
if ($bind -ne $spec.Bind) { throw "The persistent mount ($bind) differs from the reviewed target; refusing." }
if ($bind -eq $other.Bind) { throw 'Test and production share a mount; refusing.' }
if ($spec.RequireRunning -and !$container.State.Running) { throw 'The verified live host is not running.' }
if ($Target -eq 'production' -and ($Apply -or $Rollback) -and $ConfirmProduction -ne $spec.Container) {
    throw "Production changes need -ConfirmProduction $($spec.Container) and your approval."
}

$usr = $spec.Bind
$dest = Join-Path $usr "plugins\$Plugin"
try { Assert-ConvoDeployableState $dest $usr -AllowOn:$WebOnly }
catch {
    # A preview may inspect a live ON plugin; changing anything may not.
    if ($Apply -or $Rollback) { throw }
    Write-Host "NOTE: an apply would be refused now: $($_.Exception.Message)"
}
$protectedPaths = @('settings.json', '.env', 'secrets.env') | ForEach-Object { Join-Path $usr $_ }
foreach ($name in '_convo', 'convo_d1_decider', '_enhanced_speech', '_kokoro_tts', '_whisper_stt', 'ai_link_bridge', 'system_1', '_model_config') {
    $protectedPaths += Join-Path $usr "plugins\$name\config.json"
}
$before = Get-ConvoProtected $protectedPaths

if ($Rollback) {
    if (!$BackupStamp) { throw 'Name the backup with -BackupStamp.' }
    $root = Join-Path $backups "deploy-$Target-$BackupStamp"
    if (!(Test-Path -LiteralPath (Join-Path $root 'rollback-manifest.json'))) { throw "No backup at $root." }
    if ($WebOnly) {
        $recorded = Get-Content -LiteralPath (Join-Path $root 'rollback-manifest.json') -Raw | ConvertFrom-Json
        $paths = @($recorded.entries | Where-Object { $_.path -ne '.deploy-manifest.json' } | ForEach-Object { $_.path })
        Assert-ConvoWebOnlyPlan @{ Added = @(); Changed = $paths; Removed = @() }
    }
    Invoke-ConvoRollback $dest $root
    Assert-ConvoProtectedUnchanged $before (Get-ConvoProtected $protectedPaths)
    Write-Host "ROLLBACK_PASS: $Target restored from $BackupStamp. Refresh through Plugin Doctor, then enable natively."
    return
}

$known = $null
$manifestPath = Join-Path $dest '.deploy-manifest.json'
if (Test-Path -LiteralPath $manifestPath) { $record = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }
elseif ($BaselineManifest) { $record = Get-Content -LiteralPath $BaselineManifest -Raw | ConvertFrom-Json }
if ($record) { $known = @{}; foreach ($property in $record.PSObject.Properties) { $known[$property.Name] = $property.Value } }

$sourceTree = Get-ConvoTree $source
$destTree = Get-ConvoTree $dest
$plan = Get-ConvoPlan $sourceTree $destTree $known
$bytes = ($plan.Added + $plan.Changed | ForEach-Object { $sourceTree[$_].Bytes } | Measure-Object -Sum).Sum
Write-Host ("[{0}] {1}: added {2}, changed {3}, removed {4}, unrecorded {5}, unchanged {6}; {7} bytes to copy" -f $Target, $spec.Container, $plan.Added.Count, $plan.Changed.Count, $plan.Removed.Count, $plan.Unknown.Count, $plan.Unchanged, $bytes)
foreach ($label in 'Added', 'Changed', 'Removed', 'Unknown') { foreach ($key in $plan[$label]) { Write-Host "  $label $key" } }
if ($plan.Unknown.Count) { throw 'Unrecorded destination files exist; review them or pass -BaselineManifest. Nothing changed.' }
if ($WebOnly) { Assert-ConvoWebOnlyPlan $plan }
if (!$Apply) { Write-Host 'Preview only. Use -Apply after the offline tests pass.'; return }
if ((Get-PSDrive C).Free -lt (1GB + 2 * [long]$bytes)) { throw 'Insufficient headroom for a backed-up deployment.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Invoke-ConvoApply $sourceTree $destTree $plan $dest (Join-Path $backups "deploy-$Target-$stamp") $stamp
Assert-ConvoProtectedUnchanged $before (Get-ConvoProtected $protectedPaths)
if ($WebOnly) { Write-Host "UPDATE_PASS: $Target stamp $stamp; browser assets only, protected configuration unchanged. Reload the page to load them; the plugin state was not touched." }
else { Write-Host "UPDATE_PASS: $Target stamp $stamp; protected configuration unchanged. Convo is still OFF: refresh through Plugin Doctor, then enable natively in the host process." }
