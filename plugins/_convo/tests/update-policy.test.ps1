$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'update-persistent.lib.ps1')

$work = Join-Path ([IO.Path]::GetTempPath()) ('convo-update-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$script:checks = 0
function Assert-True($condition, $message) { $script:checks++; if (!$condition) { throw "FAILED: $message" } }
function Assert-Throws([scriptblock]$block, $pattern, $message) {
    $script:checks++
    try { & $block } catch { if ($_.Exception.Message -match $pattern) { return }; throw "FAILED (wrong error '$($_.Exception.Message)'): $message" }
    throw "FAILED (no error): $message"
}
function Write-File($root, $relative, $text) {
    $path = Join-Path $root $relative
    New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
    Set-Content -LiteralPath $path -Value $text -Encoding UTF8 -NoNewline
}
function Hash-Of($path) { (Get-FileHash -LiteralPath $path).Hash }

try {
    # ---- fresh install into a missing destination
    $src = Join-Path $work 'src'
    Write-File $src 'plugin.yaml' 'name: _convo'
    Write-File $src 'a.py' 'one'
    Write-File $src 'sub/b.py' 'two'
    Write-File $src 'config.json' '{"leak": true}'       # protected: never deployed
    Write-File $src 'data/journal' 'private'              # protected
    Write-File $src '__pycache__/a.pyc' 'bytecode'        # never deployed
    Write-File $src 'sub/__pycache__/b.pyc' 'bytecode'
    $dest = Join-Path $work 'usr/plugins/_convo'
    $tree = Get-ConvoTree $src
    Assert-True ($tree.Keys.Count -eq 3) 'only deployable files are listed'
    Assert-True (!$tree.ContainsKey('config.json') -and !$tree.ContainsKey('data/journal')) 'protected files are not deployable'
    $plan = Get-ConvoPlan $tree (Get-ConvoTree $dest) $null
    Assert-True ($plan.Added.Count -eq 3 -and !$plan.Changed.Count -and !$plan.Removed.Count -and !$plan.Unknown.Count) 'fresh plan is all additions'
    Assert-ConvoDeployableState $dest (Join-Path $work 'usr')
    $before = Get-ConvoProtected @((Join-Path $dest 'config.json'))
    Invoke-ConvoApply $tree (Get-ConvoTree $dest) $plan $dest (Join-Path $work 'backup1') 's1'
    Assert-True (Test-Path (Join-Path $dest '.toggle-0')) 'a fresh install starts OFF'
    Assert-True (!(Test-Path (Join-Path $dest 'config.json')) -and !(Test-Path (Join-Path $dest 'data'))) 'protected files were not copied'
    Assert-True ((Hash-Of (Join-Path $dest 'sub/b.py')) -eq $tree['sub/b.py'].Hash) 'copies are hash-identical'
    Assert-ConvoProtectedUnchanged $before (Get-ConvoProtected @((Join-Path $dest 'config.json')))
    $recorded = Get-Content (Join-Path $dest '.deploy-manifest.json') -Raw | ConvertFrom-Json
    Assert-True ($recorded.'a.py' -eq $tree['a.py'].Hash) 'the deployment manifest records what was copied'

    # ---- update: change, add and remove; user data and toggles stay
    Write-File $dest 'config.json' '{"user": 1}'
    Write-File $dest 'data/journal' 'user data'
    $configHash = Hash-Of (Join-Path $dest 'config.json')
    Write-File $src 'a.py' 'one-v2'
    Write-File $src 'c.py' 'three'
    Remove-Item (Join-Path $src 'sub/b.py')
    $known = @{}; foreach ($p in $recorded.PSObject.Properties) { $known[$p.Name] = $p.Value }
    $tree2 = Get-ConvoTree $src
    $plan2 = Get-ConvoPlan $tree2 (Get-ConvoTree $dest) $known
    Assert-True (($plan2.Changed -join ',') -eq 'a.py') 'changed file detected'
    Assert-True (($plan2.Added -join ',') -eq 'c.py') 'added file detected'
    Assert-True (($plan2.Removed -join ',') -eq 'sub/b.py') 'a recorded file absent from the source is removed'
    $protected = Get-ConvoProtected @((Join-Path $dest 'config.json'), (Join-Path $dest 'data/journal'), (Join-Path $dest '.toggle-0'))
    Invoke-ConvoApply $tree2 (Get-ConvoTree $dest) $plan2 $dest (Join-Path $work 'backup2') 's2'
    Assert-ConvoProtectedUnchanged $protected (Get-ConvoProtected @((Join-Path $dest 'config.json'), (Join-Path $dest 'data/journal'), (Join-Path $dest '.toggle-0')))
    Assert-True ((Get-Content (Join-Path $dest 'a.py') -Raw) -eq 'one-v2') 'updated content deployed'
    Assert-True (!(Test-Path (Join-Path $dest 'sub/b.py'))) 'removed file is gone'

    # ---- rollback restores the exact previous state, including the previous manifest
    Invoke-ConvoRollback $dest (Join-Path $work 'backup2')
    Assert-True ((Get-Content (Join-Path $dest 'a.py') -Raw) -eq 'one') 'rollback restores the changed file'
    Assert-True ((Get-Content (Join-Path $dest 'sub/b.py') -Raw) -eq 'two') 'rollback restores the removed file'
    Assert-True (!(Test-Path (Join-Path $dest 'c.py'))) 'rollback removes the added file'
    Assert-True ((Hash-Of (Join-Path $dest 'config.json')) -eq $configHash) 'rollback leaves user config alone'
    $restored = Get-Content (Join-Path $dest '.deploy-manifest.json') -Raw | ConvertFrom-Json
    Assert-True ($restored.'a.py' -eq $tree['a.py'].Hash) 'rollback restores the previous deployment manifest'

    # ---- first rollback of a fresh install removes what it added
    Invoke-ConvoRollback $dest (Join-Path $work 'backup1')
    Assert-True (!(Test-Path (Join-Path $dest 'a.py')) -and !(Test-Path (Join-Path $dest '.deploy-manifest.json'))) 'rolling back the first install removes its files'
    Assert-True ((Hash-Of (Join-Path $dest 'config.json')) -eq $configHash) 'user config still untouched'

    # ---- refusals
    $stray = Join-Path $work 'stray/usr/plugins/_convo'
    Write-File $stray 'plugin.yaml' 'name: _convo'
    Write-File $stray 'stray.txt' 'unrecorded'
    New-Item -ItemType File -Path (Join-Path $stray '.toggle-0') | Out-Null
    $planStray = Get-ConvoPlan (Get-ConvoTree $src) (Get-ConvoTree $stray) @{}
    Assert-True ($planStray.Unknown -contains 'stray.txt') 'an unrecorded destination file is Unknown'
    Assert-Throws { Invoke-ConvoApply (Get-ConvoTree $src) (Get-ConvoTree $stray) $planStray $stray (Join-Path $work 'backup3') 's3' } 'Unrecorded files' 'unknown files block the apply'
    Assert-True (!(Test-Path (Join-Path $work 'backup3'))) 'a refused apply writes no backup and changes nothing'

    $on = Join-Path $work 'on/usr/plugins/_convo'
    Write-File $on 'plugin.yaml' 'name: _convo'
    New-Item -ItemType File -Path (Join-Path $on '.toggle-1') | Out-Null
    Assert-Throws { Assert-ConvoDeployableState $on (Join-Path $work 'on/usr') } 'Convo is ON' 'an enabled plugin is refused'
    $unmarked = Join-Path $work 'unmarked/usr/plugins/_convo'
    Write-File $unmarked 'plugin.yaml' 'name: _convo'
    Assert-Throws { Assert-ConvoDeployableState $unmarked (Join-Path $work 'unmarked/usr') } 'no explicit OFF' 'a plugin without an OFF marker is refused'

    # ---- a running plugin may receive browser assets only
    Assert-ConvoDeployableState $on (Join-Path $work 'on/usr') -AllowOn
    $script:checks++
    Assert-ConvoWebOnlyPlan @{ Added = @('extensions/webui/page-head/x.html'); Changed = @('webui/main.html', 'webui/view-helpers.js', 'tests/ui.test.mjs', 'README.md'); Removed = @('webui/old.js') }
    $script:checks++
    foreach ($bad in 'helpers/convo_store.py', 'api/conversation.py', 'plugin.yaml', 'sidecar/app.py', 'extensions/python/agent_init/x.py', 'webuix/main.html', 'testsx/a.py', 'docs/README.md', 'default_config.yaml') {
        Assert-Throws { Assert-ConvoWebOnlyPlan @{ Added = @(); Changed = @($bad); Removed = @() } } 'needs Convo OFF' "web-only refuses $bad"
    }
    Assert-Throws { Assert-ConvoWebOnlyPlan @{ Added = @(); Changed = @('webui/a.js'); Removed = @('helpers/gone.py') } } 'Removed' 'web-only refuses a non-web removal'

    $real = Join-Path $work 'real'
    New-Item -ItemType Directory -Path $real | Out-Null
    $link = Join-Path $work 'linked'
    New-Item -ItemType Junction -Path $link -Target $real | Out-Null
    Assert-Throws { Assert-ConvoDeployableState (Join-Path $link '_convo') $link } 'Linked' 'a linked destination is refused'
    Assert-Throws { Assert-ConvoDeployableState (Join-Path $link '_convo') $link -AllowOn } 'Linked' 'web-only still refuses a linked destination'
    Assert-Throws { Get-ConvoTargetPath $work '..\escape.txt' } 'escaped' 'path traversal is refused'
    Assert-Throws { Assert-ConvoProtectedUnchanged ([ordered]@{ 'x' = 'AA' }) ([ordered]@{ 'x' = 'BB' }) } 'protected file changed' 'a changed protected file is detected'
    Write-Host "UPDATE_POLICY_PASS: $script:checks checks"
} finally {
    if (Test-Path -LiteralPath (Join-Path $work 'linked')) { (Get-Item -LiteralPath (Join-Path $work 'linked')).Delete() }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
