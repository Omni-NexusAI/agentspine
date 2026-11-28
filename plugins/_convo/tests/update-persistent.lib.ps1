# Pure deployment logic: dot-sourcing never contacts Docker. Every function takes explicit paths so the
# policy test can run it against temporary directories. Windows PowerShell 5.1 compatible.

$script:ConvoProtectedTop = @('config.json', 'data', '.toggle-0', '.toggle-1', '.toggle-default', '.deploy-manifest.json')
$script:ConvoManifestName = '.deploy-manifest.json'

function Test-ConvoDeployable {
    param([string]$Relative)
    $parts = $Relative -split '[\\/]'
    if ($parts[0] -in $script:ConvoProtectedTop) { return $false }
    if ($parts -contains '__pycache__' -or $Relative.EndsWith('.pyc')) { return $false }
    return $true
}

function Get-ConvoTree {
    # Relative (forward-slash) path -> @{Full=; Hash=; Bytes=} for deployable files only.
    param([Parameter(Mandatory)][string]$Root)
    $result = @{}
    if (!(Test-Path -LiteralPath $Root)) { return $result }
    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    foreach ($file in Get-ChildItem -LiteralPath $rootFull -Recurse -File -Force) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked file refused: $($file.FullName)" }
        $relative = $file.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
        if (!(Test-ConvoDeployable $relative)) { continue }
        $result[$relative] = @{ Full = $file.FullName; Hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash; Bytes = $file.Length }
    }
    return $result
}

function Get-ConvoPlan {
    # Added / Changed / Removed / Unknown. Removal is limited to files recorded by the previous deployment (or an
    # explicit baseline); a destination file nobody recorded is Unknown and blocks the apply.
    param($Source, $Dest, $Known)
    $plan = @{ Added = @(); Changed = @(); Removed = @(); Unknown = @(); Unchanged = 0 }
    foreach ($key in ($Source.Keys | Sort-Object)) {
        if (!$Dest.ContainsKey($key)) { $plan.Added += $key }
        elseif ($Dest[$key].Hash -ne $Source[$key].Hash) { $plan.Changed += $key }
        else { $plan.Unchanged++ }
    }
    foreach ($key in ($Dest.Keys | Sort-Object)) {
        if ($Source.ContainsKey($key)) { continue }
        if ($Known -and $Known.ContainsKey($key)) { $plan.Removed += $key } else { $plan.Unknown += $key }
    }
    return $plan
}

function Assert-ConvoWebOnlyPlan {
    # A running (ON) plugin may only receive static browser assets (the host reads them from disk on every request) and
    # files the host never imports (tests, top-level documents), so no loaded Python module can go stale.
    # Anything else needs Convo OFF.
    param($Plan)
    foreach ($label in 'Added', 'Changed', 'Removed') {
        foreach ($key in $Plan[$label]) {
            if ($key -notmatch '^(webui/|extensions/webui/|tests/)' -and $key -notmatch '^[^/]+\.md$') {
                throw "A running plugin accepts only browser assets (webui/, extensions/webui/), tests and documents; $label '$key' needs Convo OFF."
            }
        }
    }
}

function Assert-ConvoDeployableState {
    # Convo must be OFF (or not yet installed) and the destination must not be a link. -AllowOn is for web-asset-only updates.
    param([Parameter(Mandatory)][string]$Dest, [string]$UsrRoot, [switch]$AllowOn)
    foreach ($path in @($Dest, (Split-Path $Dest), $UsrRoot) | Where-Object { $_ }) {
        if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Linked deployment destination is not authorized: $path"
        }
    }
    if ($AllowOn) { return }
    if (Test-Path -LiteralPath $Dest) {
        if (Test-Path -LiteralPath (Join-Path $Dest '.toggle-1')) { throw 'Convo is ON. Disable it natively before deploying.' }
        if (!(Test-Path -LiteralPath (Join-Path $Dest '.toggle-0')) -and (Test-Path -LiteralPath (Join-Path $Dest 'plugin.yaml'))) {
            throw 'Convo has no explicit OFF marker. Disable it natively before deploying.'
        }
    }
}

function Get-ConvoProtected {
    # Hash-only snapshot of files an update must never change. Missing files are recorded as null.
    param([string[]]$Paths)
    $out = [ordered]@{}
    foreach ($path in $Paths) {
        $out[$path] = if (Test-Path -LiteralPath $path -PathType Leaf) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash } else { $null }
    }
    return $out
}

function Assert-ConvoProtectedUnchanged {
    param($Before, $After)
    foreach ($key in $Before.Keys) {
        if ($Before[$key] -ne $After[$key]) { throw "A protected file changed during deployment: $key" }
    }
}

function Get-ConvoTargetPath {
    # Resolves a relative path under a root and refuses any escape.
    param([string]$Root, [string]$Relative)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $full = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (!$full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) { throw "Path escaped its root: $Relative" }
    return $full
}

function Invoke-ConvoApply {
    param([Parameter(Mandatory)]$Source, [Parameter(Mandatory)]$Dest, [Parameter(Mandatory)]$Plan,
          [Parameter(Mandatory)][string]$DestRoot, [Parameter(Mandatory)][string]$BackupRoot, [string]$Stamp)
    if ($Plan.Unknown.Count) { throw "Unrecorded files exist in the destination; review them first: $($Plan.Unknown -join ', ')" }
    $backupFiles = Join-Path $BackupRoot 'files'
    New-Item -ItemType Directory -Path $backupFiles -Force | Out-Null
    $manifest = [ordered]@{ stamp = $Stamp; destination = $DestRoot; entries = @() }
    foreach ($key in @($Plan.Changed) + @($Plan.Removed)) {
        $existing = Get-ConvoTargetPath $DestRoot $key
        $copy = Get-ConvoTargetPath $backupFiles $key
        New-Item -ItemType Directory -Path (Split-Path $copy) -Force | Out-Null
        Copy-Item -LiteralPath $existing -Destination $copy
        if ((Get-FileHash -LiteralPath $existing).Hash -ne (Get-FileHash -LiteralPath $copy).Hash) { throw "Backup verification failed: $key" }
        $manifest.entries += [ordered]@{ path = $key; action = $(if ($Plan.Removed -contains $key) { 'removed' } else { 'changed' }); sha256 = (Get-FileHash -LiteralPath $copy).Hash }
    }
    foreach ($key in @($Plan.Added)) { $manifest.entries += [ordered]@{ path = $key; action = 'added'; sha256 = $null } }
    $previous = Join-Path $DestRoot $script:ConvoManifestName
    if (Test-Path -LiteralPath $previous) {
        Copy-Item -LiteralPath $previous -Destination (Join-Path $backupFiles $script:ConvoManifestName)
        $manifest.entries += [ordered]@{ path = $script:ConvoManifestName; action = 'changed'; sha256 = (Get-FileHash -LiteralPath $previous).Hash }
    } else {
        $manifest.entries += [ordered]@{ path = $script:ConvoManifestName; action = 'added'; sha256 = $null }
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $BackupRoot 'rollback-manifest.json') -Encoding UTF8
    New-Item -ItemType Directory -Path $DestRoot -Force | Out-Null
    if (!(Test-Path -LiteralPath (Join-Path $DestRoot '.toggle-0')) -and !(Test-Path -LiteralPath (Join-Path $DestRoot 'plugin.yaml'))) {
        New-Item -ItemType File -Path (Join-Path $DestRoot '.toggle-0') | Out-Null
    }
    $copies = @($Plan.Added) + @($Plan.Changed) | Sort-Object @{ Expression = { if ($_ -eq 'plugin.yaml') { 1 } else { 0 } } }, @{ Expression = { $_ } }
    foreach ($key in $copies) {
        $target = Get-ConvoTargetPath $DestRoot $key
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item -LiteralPath $Source[$key].Full -Destination $target -Force
        if ((Get-FileHash -LiteralPath $target).Hash -ne $Source[$key].Hash) { throw "Copy verification failed: $key" }
    }
    foreach ($key in @($Plan.Removed)) { Remove-Item -LiteralPath (Get-ConvoTargetPath $DestRoot $key) -Force }
    $deployed = [ordered]@{}
    foreach ($key in ($Source.Keys | Sort-Object)) { $deployed[$key] = $Source[$key].Hash }
    $deployed | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DestRoot $script:ConvoManifestName) -Encoding UTF8
}

function Invoke-ConvoRollback {
    param([Parameter(Mandatory)][string]$DestRoot, [Parameter(Mandatory)][string]$BackupRoot)
    $manifest = Get-Content -LiteralPath (Join-Path $BackupRoot 'rollback-manifest.json') -Raw | ConvertFrom-Json
    if ([IO.Path]::GetFullPath($manifest.destination).TrimEnd('\') -ne [IO.Path]::GetFullPath($DestRoot).TrimEnd('\')) {
        throw 'The backup belongs to a different destination; refusing rollback.'
    }
    foreach ($entry in $manifest.entries) {
        $target = Get-ConvoTargetPath $DestRoot $entry.path
        if ($entry.action -eq 'added') {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
            continue
        }
        $saved = Get-ConvoTargetPath (Join-Path $BackupRoot 'files') $entry.path
        if ((Get-FileHash -LiteralPath $saved).Hash -ne $entry.sha256) { throw "Backup file changed since it was taken: $($entry.path)" }
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item -LiteralPath $saved -Destination $target -Force
    }
}
