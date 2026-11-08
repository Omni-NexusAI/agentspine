# Pure validation: dot-sourcing this file never contacts Docker or changes state.
function Assert-ConvoTestContainer {
    param($Container, [string]$ImageId, [hashtable]$ExpectedMounts)
    $h = $Container.HostConfig
    $c = $Container.Config
    if ($Container.Image -cne $ImageId -or $Container.State.Status -cne 'exited' -or
        $Container.State.Running -or $Container.State.Paused -or $Container.State.Restarting -or
        !$h.ReadonlyRootfs -or $h.NetworkMode -cne 'none' -or $h.Privileged -or
        $h.PublishAllPorts -or $h.AutoRemove -or $h.RestartPolicy.Name -cne 'no' -or
        $h.PidMode -or $h.IpcMode -cne 'private' -or $h.UTSMode -or
        $h.Runtime -cne 'runc' -or $h.CapAdd -or
        (@($h.CapDrop) -join ',') -cne 'ALL' -or
        (@($h.SecurityOpt) -join ',') -cne 'no-new-privileges' -or
        $h.Devices -or $h.DeviceRequests -or $h.DeviceCgroupRules -or $h.VolumesFrom -or $h.Binds -or
        @($h.PortBindings.PSObject.Properties).Count -or
        $h.Memory -ne 2GB -or $h.NanoCpus -ne 2000000000 -or $h.PidsLimit -ne 256 -or
        $h.LogConfig.Type -cne 'local' -or $h.LogConfig.Config.'max-size' -cne '1m' -or
        $h.LogConfig.Config.'max-file' -cne '1' -or $h.LogConfig.Config.compress -cne 'false' -or
        @($c.Cmd).Count -ne 1 -or $c.Cmd[0] -cne '/git/agent-zero/plugins/_convo/tests/in_image.py' -or
        @($c.Entrypoint).Count -ne 1 -or $c.Entrypoint[0] -cne '/opt/venv-a0/bin/python' -or
        $c.WorkingDir -cne '/git/agent-zero' -or $c.Tty -or $c.OpenStdin -or $c.Volumes) {
        throw 'Container isolation or runner identity mismatch; refusing reuse.'
    }
    $scratch = @{
        '/tmp' = 'rw,nosuid,nodev,size=256m'
        '/git/agent-zero/usr' = 'rw,nosuid,nodev,size=64m'
        '/git/agent-zero/tmp' = 'rw,nosuid,nodev,size=64m'
        '/git/agent-zero/logs' = 'rw,nosuid,nodev,size=16m'
    }
    if (@($h.Tmpfs.PSObject.Properties).Count -ne $scratch.Count) { throw 'Unexpected scratch mounts.' }
    foreach ($key in $scratch.Keys) {
        if ($h.Tmpfs.$key -cne $scratch[$key]) { throw 'Unbounded or unexpected scratch mount.' }
    }
    $requiredEnv = @('PYTHONDONTWRITEBYTECODE=1','PYTHONPATH=/git/agent-zero','CONVO_ISOLATED_TEST=1',
        'HF_HUB_OFFLINE=1','TRANSFORMERS_OFFLINE=1','HOME=/tmp','LITELLM_LOCAL_MODEL_COST_MAP=True')
    foreach ($entry in $requiredEnv) {
        $key = $entry.Split('=')[0]
        $matches = @($c.Env | Where-Object { $_.StartsWith($key + '=', [StringComparison]::Ordinal) })
        if ($matches.Count -ne 1 -or $matches[0] -cne $entry) { throw 'Missing or ambiguous offline environment.' }
    }
    if (@($Container.Mounts).Count -ne $ExpectedMounts.Count) { throw 'Unexpected mounts; refusing reuse.' }
    $seen = @{}
    foreach ($mount in $Container.Mounts) {
        if ($mount.Type -cne 'bind' -or $mount.RW -or $mount.Propagation -cne 'rprivate' -or
            !$ExpectedMounts.ContainsKey($mount.Destination) -or $seen.ContainsKey($mount.Destination) -or
            $ExpectedMounts[$mount.Destination] -ne $mount.Source) { throw 'Test source mount mismatch.' }
        $seen[$mount.Destination] = $true
    }
}
