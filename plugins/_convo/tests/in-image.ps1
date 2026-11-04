param(
    [ValidateSet('agentspine-standard-v9.9','agent-zero-v2.11')]
    [string]$SourceContainer = 'agent-zero-v2.11',
    [ValidatePattern('^convo-test-[a-zA-Z0-9-]+$')]
    [string]$Name = 'convo-test-v211-doctor-20260908',
    [switch]$Reuse
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'image-policy.ps1')
$pluginPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compatPath = (Resolve-Path (Join-Path $pluginPath '../_enhanced_speech')).Path
$doctorPath = (Resolve-Path (Join-Path $pluginPath '../../usr/plugins/plugin_doctor')).Path
$before = (Get-PSDrive -Name ([IO.Path]::GetPathRoot($pluginPath).TrimEnd('\').TrimEnd(':'))).Free
if ($before -lt 1GB) { throw 'Keep at least 1 GiB free for this bounded, no-build test. No cleanup was performed.' }
$imageId = docker inspect $SourceContainer --format '{{.Image}}'
if ($LASTEXITCODE -ne 0) { throw 'Existing host container/image unavailable. No pull or startup attempted.' }
$details = docker image inspect $imageId | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or !$details) { throw 'Local image unavailable; no pull attempted.' }
if ($details[0].Config.Volumes -and $details[0].Config.Volumes.PSObject.Properties.Count) {
    throw 'Image declares volumes; review and mask them before testing to avoid anonymous volume creation.'
}
$runArgs = @('run','--name',$Name,'--pull','never','--network','none','--read-only',
    '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','256','--memory','2g','--cpus','2',
    '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=1','--log-opt','compress=false',
    '--tmpfs','/tmp:rw,nosuid,nodev,size=256m','--tmpfs','/git/agent-zero/usr:rw,nosuid,nodev,size=64m',
    '--tmpfs','/git/agent-zero/tmp:rw,nosuid,nodev,size=64m','--tmpfs','/git/agent-zero/logs:rw,nosuid,nodev,size=16m',
    '--env','PYTHONDONTWRITEBYTECODE=1','--env','PYTHONPATH=/git/agent-zero','--env','CONVO_ISOLATED_TEST=1',
    '--env','HF_HUB_OFFLINE=1','--env','TRANSFORMERS_OFFLINE=1','--env','HOME=/tmp',
    '--env','LITELLM_LOCAL_MODEL_COST_MAP=True',
    '--mount',"type=bind,source=$pluginPath,target=/git/agent-zero/plugins/_convo,readonly",
    '--mount',"type=bind,source=$compatPath,target=/git/agent-zero/plugins/_enhanced_speech,readonly",
    '--mount',"type=bind,source=$doctorPath,target=/git/agent-zero/usr/plugins/plugin_doctor,readonly",
    '--workdir','/git/agent-zero','--entrypoint','/opt/venv-a0/bin/python',$imageId,
    '/git/agent-zero/plugins/_convo/tests/in_image.py')
Write-Host "Testing $SourceContainer image $imageId without starting that container."
$existingId = docker ps -a --filter "name=^/$Name$" --format '{{.ID}}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot inventory the named reusable fixture.' }
if ($Reuse -or $existingId) {
    $existing = docker inspect $Name | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$existing) { throw 'Named test container does not exist.' }
    $container = $existing[0]
    $expectedMounts = @{
        '/git/agent-zero/plugins/_convo' = $pluginPath
        '/git/agent-zero/plugins/_enhanced_speech' = $compatPath
        '/git/agent-zero/usr/plugins/plugin_doctor' = $doctorPath
    }
    Assert-ConvoTestContainer $container $imageId $expectedMounts
    docker start -a $Name
} else { & docker @runArgs }
$testExit = $LASTEXITCODE
docker inspect --size $Name --format 'Test container {{.Name}}: writable layer={{.SizeRw}} bytes; exit={{.State.ExitCode}}'
Write-Host "Test container retained (stopped): $Name. No images, volumes or existing containers removed."
if ($testExit -ne 0) { throw "Isolated plugin tests failed ($testExit). The original host was not changed." }
