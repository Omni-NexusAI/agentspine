$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'image-policy.ps1')
$mounts = @{'/git/agent-zero/plugins/_convo'='C:\test\convo'; '/git/agent-zero/plugins/_enhanced_speech'='C:\test\legacy'; '/git/agent-zero/usr/plugins/plugin_doctor'='C:\test\doctor'}
$fixture = @{
    Image='sha256:test'; State=@{Status='exited'; Running=$false; Paused=$false; Restarting=$false}
    HostConfig=@{
        ReadonlyRootfs=$true; NetworkMode='none'; Privileged=$false; PublishAllPorts=$false; AutoRemove=$false
        RestartPolicy=@{Name='no'}; PidMode=''; IpcMode='private'; UTSMode=''; Runtime='runc'
        CapDrop=@('ALL'); SecurityOpt=@('no-new-privileges'); PortBindings=@{}
        Memory=2GB; NanoCpus=2000000000; PidsLimit=256
        LogConfig=@{Type='local'; Config=@{'max-size'='1m'; 'max-file'='1'; compress='false'}}
        Tmpfs=@{'/tmp'='rw,nosuid,nodev,size=256m'; '/git/agent-zero/usr'='rw,nosuid,nodev,size=64m'; '/git/agent-zero/tmp'='rw,nosuid,nodev,size=64m'; '/git/agent-zero/logs'='rw,nosuid,nodev,size=16m'}
    }
    Config=@{
        Cmd=@('/git/agent-zero/plugins/_convo/tests/in_image.py'); Entrypoint=@('/opt/venv-a0/bin/python')
        WorkingDir='/git/agent-zero'; Tty=$false; OpenStdin=$false
        Env=@('PYTHONDONTWRITEBYTECODE=1','PYTHONPATH=/git/agent-zero','CONVO_ISOLATED_TEST=1','HF_HUB_OFFLINE=1','TRANSFORMERS_OFFLINE=1','HOME=/tmp','LITELLM_LOCAL_MODEL_COST_MAP=True')
    }
    Mounts=@($mounts.Keys | ForEach-Object { @{Type='bind'; Source=$mounts[$_]; Destination=$_; RW=$false; Propagation='rprivate'} })
} | ConvertTo-Json -Depth 12
Assert-ConvoTestContainer ($fixture | ConvertFrom-Json) 'sha256:test' $mounts
$cases = @(
    @{path='Image'; value='wrong'}, @{path='State.Status'; value='created'}, @{path='State.Running'; value=$true},
    @{path='State.Paused'; value=$true}, @{path='State.Restarting'; value=$true},
    @{path='HostConfig.ReadonlyRootfs'; value=$false}, @{path='HostConfig.NetworkMode'; value='host'},
    @{path='HostConfig.Privileged'; value=$true}, @{path='HostConfig.PublishAllPorts'; value=$true},
    @{path='HostConfig.AutoRemove'; value=$true}, @{path='HostConfig.RestartPolicy.Name'; value='always'},
    @{path='HostConfig.PidMode'; value='host'}, @{path='HostConfig.IpcMode'; value='host'},
    @{path='HostConfig.UTSMode'; value='host'}, @{path='HostConfig.Runtime'; value='nvidia'},
    @{path='HostConfig.CapAdd'; value=@('SYS_ADMIN')}, @{path='HostConfig.CapDrop'; value=@()},
    @{path='HostConfig.SecurityOpt'; value=@('seccomp=unconfined')},
    @{path='HostConfig.Devices'; value=@(@{PathOnHost='/dev/sda'})},
    @{path='HostConfig.DeviceRequests'; value=@(@{Count=-1})}, @{path='HostConfig.DeviceCgroupRules'; value=@('a *:* rwm')},
    @{path='HostConfig.VolumesFrom'; value=@('production')}, @{path='HostConfig.Binds'; value=@('/:/host')},
    @{path='HostConfig.PortBindings'; value=@{'80/tcp'=@(@{HostPort='8080'})}},
    @{path='HostConfig.Memory'; value=0}, @{path='HostConfig.NanoCpus'; value=0}, @{path='HostConfig.PidsLimit'; value=-1},
    @{path='HostConfig.LogConfig.Type'; value='json-file'}, @{path='HostConfig.LogConfig.Config.max-size'; value='1g'},
    @{path='HostConfig.LogConfig.Config.max-file'; value='100'}, @{path='HostConfig.LogConfig.Config.compress'; value='true'},
    @{path='HostConfig.Tmpfs'; value=@{'/tmp'='rw'}},
    @{path='Config.Cmd'; value=@('/git/agent-zero/plugins/_convo/tests/in_image.py','extra')},
    @{path='Config.Entrypoint'; value=@('/bin/sh')}, @{path='Config.WorkingDir'; value='/a0'},
    @{path='Config.Tty'; value=$true}, @{path='Config.OpenStdin'; value=$true}, @{path='Config.Volumes'; value=@{'/data'=@{}}},
    @{path='Config.Env'; value=@('CONVO_ISOLATED_TEST=1')}, @{path='Mounts'; value=@()}
)
$count = 1
foreach ($case in $cases) {
    $candidate = $fixture | ConvertFrom-Json
    $parts = $case.path.Split('.')
    $owner = $candidate
    foreach ($part in $parts[0..($parts.Count-1)] | Select-Object -SkipLast 1) { $owner = $owner.$part }
    $owner | Add-Member -NotePropertyName $parts[-1] -NotePropertyValue $case.value -Force
    $rejected = $false
    try { Assert-ConvoTestContainer $candidate 'sha256:test' $mounts } catch { $rejected = $true }
    if (!$rejected) { throw "Unsafe reuse accepted: $($case.path)" }
    $count++
}
foreach ($mutation in @('writable','source','destination','duplicate','volume','propagation','env-duplicate','scratch-size')) {
    $candidate = $fixture | ConvertFrom-Json
    switch ($mutation) {
        writable { $candidate.Mounts[0].RW=$true }
        source { $candidate.Mounts[0].Source='C:\user-data' }
        destination { $candidate.Mounts[0].Destination='/var/run/docker.sock' }
        duplicate { $candidate.Mounts[0]=$candidate.Mounts[1] }
        volume { $candidate.Mounts[0].Type='volume' }
        propagation { $candidate.Mounts[0].Propagation='rshared' }
        env-duplicate { $candidate.Config.Env += 'CONVO_ISOLATED_TEST=0' }
        scratch-size { $candidate.HostConfig.Tmpfs.'/tmp'='rw,nosuid,nodev,size=20g' }
    }
    $rejected=$false
    try { Assert-ConvoTestContainer $candidate 'sha256:test' $mounts } catch { $rejected=$true }
    if (!$rejected) { throw "Unsafe reuse accepted: $mutation" }
    $count++
}
Write-Output "IMAGE_POLICY_PASS: $count offline cases; Docker was not contacted."
