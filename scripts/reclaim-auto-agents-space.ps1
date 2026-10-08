[CmdletBinding()]
param(
    [switch]$Preview,
    [string]$Distro = 'Ubuntu-20.04',
    [string]$LinuxUser = 'fuli',
    [string]$LinuxPython = '/home/fuli/miniconda3/envs/autoagents/bin/python',
    [string]$Engine = '/home/fuli/projects/auto_agents',
    [string[]]$Projects = @('/home/fuli/projects/sdgp'),
    [string]$OutputRoot = 'D:\auto-agents-storage-reclaim'
)
$ErrorActionPreference = 'Stop'

function Quote-NativeArgument([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value.Length) { return $Value }
    # Windows CreateProcess quoting, including backslashes before a quote/end.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Invoke-Native([string]$Executable, [string[]]$Arguments, [string]$Log, [int]$Timeout = 300) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = (($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw "Cannot start $Executable" }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($Timeout * 1000)) {
        # Compaction never uses this bounded native-command helper.
        $process.Kill()
        $process.WaitForExit()
        throw "Command timed out: $Executable. No further phase will run."
    }
    $text = $stdout.Result + $stderr.Result
    if ($Log) { [IO.File]::WriteAllText($Log, $text, (New-Object Text.UTF8Encoding($false))) }
    $code = $process.ExitCode
    $process.Dispose()
    return [PSCustomObject]@{ Code=$code; Text=$text }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Log, [int]$Timeout = 300) {
    $result = Invoke-Native $Executable $Arguments $Log $Timeout
    if ($result.Code -ne 0) { throw "Command failed ($($result.Code)): $Executable. $($result.Text.Trim())" }
    return $result.Text
}

function Get-ReclaimDisks {
    $entries = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | ForEach-Object {
        $item = Get-ItemProperty $_.PSPath
        if ($item.DistributionName -in @($Distro, 'docker-desktop-data')) {
            $base = $item.BasePath -replace '^\\\\\?\\', ''
            $file = Join-Path $base 'ext4.vhdx'
            if (-not (Test-Path -LiteralPath $file)) { throw "Registered disk is missing: $file" }
            [PSCustomObject]@{ Distro=$item.DistributionName; Path=$file; BeforeBytes=(Get-Item -LiteralPath $file).Length }
        }
    })
    if (-not ($entries | Where-Object { $_.Distro -eq $Distro })) { throw "WSL distribution not registered: $Distro" }
    return $entries
}

function Set-ReclaimPhase([string]$Phase, [string]$Directory) {
    $value = [PSCustomObject]@{ Phase=$Phase; Time=(Get-Date).ToString('o'); Pid=$PID }
    $value | ConvertTo-Json | Set-Content (Join-Path $Directory 'status.json') -Encoding UTF8
    Write-Host "[$(Get-Date -Format HH:mm:ss)] $Phase"
}

function Invoke-LinuxCleanup([string]$Directory, [switch]$Preflight) {
    $request = Join-Path $Directory 'cleanup-request.json'
    @{engine=$Engine; projects=$Projects} | ConvertTo-Json | Set-Content $request -Encoding UTF8
    $linuxRequest = (Invoke-Checked 'wsl.exe' @('-d',$Distro,'-u',$LinuxUser,'--exec','wslpath','-u',$request) '').Trim()
    $arguments = @('-d',$Distro,'-u',$LinuxUser,'--exec',$LinuxPython,($Engine+'/scripts/reclaim_wsl_space.py'),'--request',$linuxRequest)
    if ($Preflight) { $arguments += '--preflight' }
    $log = Join-Path $Directory $(if ($Preflight) {'preflight.log'} else {'cleanup.log'})
    $text = Invoke-Checked 'wsl.exe' $arguments $log 600
    # No models, prompts or progress text: the helper emits one JSON receipt.
    $receipt = $text | ConvertFrom-Json
    if (-not $receipt.ok) { throw "Linux cleanup did not complete: $log" }
}

function Stop-ReclaimServices([string]$Docker, [string[]]$Containers, [string]$Directory, [bool]$DockerWasRunning) {
    if ($Containers.Count) { $null = Invoke-Checked $Docker (@('stop','--time','30')+$Containers) (Join-Path $Directory 'docker-stop.log') 120 }
    if ($DockerWasRunning) {
        Get-Service 'com.docker.service' -ErrorAction SilentlyContinue | Stop-Service -Force
        Get-Process 'Docker Desktop','com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force
    }
    $null = Invoke-Checked 'wsl.exe' @('--shutdown') (Join-Path $Directory 'wsl-shutdown.log') 120
    Start-Sleep -Seconds 3
    $running = Invoke-Checked 'wsl.exe' @('--list','--running','--quiet') '' 30
    if (($running -replace "`0",'').Trim()) { throw 'WSL restarted before compaction. Close WSL applications and try again.' }
}

function Compact-ReclaimDisk($Disk, [string]$Directory) {
    # Refuse an in-use disk; do not attach a writable filesystem.
    $handle = [IO.File]::Open($Disk.Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $handle.Dispose()
    $log = Join-Path $Directory ($Disk.Distro+'-compact.log')
    if (Get-Command Optimize-VHD -ErrorAction SilentlyContinue) {
        Optimize-VHD -Path $Disk.Path -Mode Full -ErrorAction Stop 4>&1 | Out-File $log -Encoding UTF8
    } else {
        $commands = Join-Path $Directory ($Disk.Distro+'-diskpart.txt')
        @('select vdisk file="'+$Disk.Path+'"','compact vdisk','exit') | Set-Content $commands -Encoding Unicode
        # Wait without a timeout. Never kill a compaction process.
        & diskpart.exe /s $commands 2>&1 | Tee-Object -FilePath $log | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "DiskPart failed: $log" }
        $text = Get-Content $log -Raw
        if ($text -notmatch '(?i)successfully compacted|成功.*压缩|成功.*壓縮') { throw "DiskPart did not confirm success: $log" }
    }
    return [PSCustomObject]@{ Distro=$Disk.Distro; Path=$Disk.Path; BeforeBytes=$Disk.BeforeBytes;
        AfterBytes=(Get-Item -LiteralPath $Disk.Path).Length; Log=$log }
}

function Restore-ReclaimServices([string]$Docker, [string[]]$Containers, [string]$Directory, [bool]$DockerWasRunning) {
    $null = Invoke-Checked 'wsl.exe' @('-d',$Distro,'-u',$LinuxUser,'--exec','/bin/true') (Join-Path $Directory 'wsl-restore.log') 60
    if (-not $DockerWasRunning) { return }
    Get-Service 'com.docker.service' -ErrorAction SilentlyContinue | Start-Service
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $desktop)) { throw 'Docker Desktop executable is missing; restore manually.' }
    Start-Process -FilePath $desktop | Out-Null
    $ready = $false
    for ($i=0; $i -lt 36; $i++) {
        if ((Invoke-Native $Docker @('info') '' 10).Code -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) { throw 'Docker is not ready; original container IDs are in running-containers.json.' }
    if ($Containers.Count) { $null = Invoke-Checked $Docker (@('start')+$Containers) (Join-Path $Directory 'docker-restore.log') 120 }
    $after = @((Invoke-Checked $Docker @('ps','--no-trunc','-q') '' 30) -split '\r?\n' | Where-Object { $_ })
    foreach ($id in $Containers) { if ($id -notin $after) { throw "Original container not restored: $id" } }
}

function Invoke-Reclaim {
    $disks = @(Get-ReclaimDisks)
    if ($Preview) {
        [PSCustomObject]@{ Preview=$true; Distro=$Distro; User=$LinuxUser; Engine=$Engine; Projects=$Projects;
            Disks=$disks; Actions=@('owned cleanup','fstrim','stop Docker/WSL','compact','restore containers') } | ConvertTo-Json -Depth 6 | Out-Host
        return 0
    }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the CMD launcher, which requests Windows administrator permission.'
    }
    $directory = Join-Path $OutputRoot (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    New-Item -ItemType Directory -Force $directory | Out-Null
    $docker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
    $containers = @(); $dockerRunning=$false; $stopping=$false; $failure=$null; $restoreFailure=$null; $results=@()
    $before = (Get-PSDrive -Name ([IO.Path]::GetPathRoot($disks[0].Path).Substring(0,1))).Free
    try {
        Set-ReclaimPhase 'check-idle' $directory
        Invoke-LinuxCleanup $directory -Preflight
        if (Test-Path $docker) {
            $probe = Invoke-Native $docker @('info') '' 15
            $dockerRunning = $probe.Code -eq 0
            if (-not $dockerRunning -and @(Get-Process 'Docker Desktop','com.docker.backend' -ErrorAction SilentlyContinue).Count) {
                throw 'Docker Desktop is running but its engine is unavailable. Wait for Docker or quit it before reclaiming.'
            }
            if ($dockerRunning) { $containers=@((Invoke-Checked $docker @('ps','--no-trunc','-q') '' 30) -split '\r?\n' | Where-Object { $_ }) }
        }
        ConvertTo-Json -InputObject @($containers) -Depth 2 | Set-Content (Join-Path $directory 'running-containers.json') -Encoding UTF8
        Set-ReclaimPhase 'clean-owned-files' $directory
        Invoke-LinuxCleanup $directory
        Set-ReclaimPhase 'trim-linux' $directory
        $null = Invoke-Checked 'wsl.exe' @('-d',$Distro,'-u','root','--exec','/usr/sbin/fstrim','-v','/') (Join-Path $directory 'ubuntu-trim.log') 180
        if ($dockerRunning -and ($disks | Where-Object { $_.Distro -eq 'docker-desktop-data' })) {
            $trim = Invoke-Native 'wsl.exe' @('-d','docker-desktop','-u','root','--exec','/sbin/fstrim','-av') (Join-Path $directory 'docker-trim.log') 180
            if ($trim.Code -ne 0) { Write-Warning 'Docker trim unavailable; only its previously free blocks can compact.' }
        }
        # Recheck immediately before shutting down all distributions.
        Invoke-LinuxCleanup $directory -Preflight
        if ($dockerRunning) {
            $now=@((Invoke-Checked $docker @('ps','--no-trunc','-q') '' 30) -split '\r?\n' | Where-Object { $_ })
            if ($containers.Count -ne $now.Count -or @($containers | Where-Object { $_ -notin $now }).Count) {
                throw 'Running containers changed during cleanup. Retry once their state is stable.'
            }
        }
        Set-ReclaimPhase 'stop-services' $directory
        $stopping=$true
        Stop-ReclaimServices $docker $containers $directory $dockerRunning
        foreach ($disk in $disks) {
            Set-ReclaimPhase ('compact-'+$disk.Distro) $directory
            $results += Compact-ReclaimDisk $disk $directory
        }
    } catch { $failure=$_.Exception.Message; Write-Warning $failure }
    finally {
        if ($stopping) {
            try {
                Set-ReclaimPhase 'restore-services' $directory
                Restore-ReclaimServices $docker $containers $directory $dockerRunning
            } catch { $restoreFailure=$_.Exception.Message; Write-Warning $restoreFailure }
        }
        $status = if ($failure -or $restoreFailure) { 'needs-attention' } else { 'complete' }
        $result=[PSCustomObject]@{ Status=$status; WindowsFreeBefore=$before;
            WindowsFreeAfter=(Get-PSDrive -Name ([IO.Path]::GetPathRoot($disks[0].Path).Substring(0,1))).Free;
            Disks=$results; Containers=$containers; Error=$failure; RestoreError=$restoreFailure; Logs=$directory }
        $result | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $directory 'result.json') -Encoding UTF8
        Set-ReclaimPhase $status $directory
        $result | ConvertTo-Json -Depth 6 | Out-Host
    }
    if ($failure -or $restoreFailure) { return 1 }
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    $mutex = New-Object Threading.Mutex($false, 'Local\AutoAgentsStorageReclaim')
    $acquired=$false; $exitCode=1
    try {
        try { $acquired=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired=$true }
        if (-not $acquired) { throw 'Another cleanup/compaction is running.' }
        $result = @(Invoke-Reclaim); $exitCode=[int]$result[-1]
        if ($result.Count -gt 1) { $result[0..($result.Count-2)] | Write-Output }
    } catch { Write-Warning $_.Exception.Message }
    finally { if($acquired){$mutex.ReleaseMutex()};$mutex.Dispose() }
    exit $exitCode
}
