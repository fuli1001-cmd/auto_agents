param([string]$ScriptPath, [string]$TemporaryRoot)
$ErrorActionPreference='Stop'
. $ScriptPath
$script:Events=New-Object 'System.Collections.Generic.List[string]'
$OutputRoot=$TemporaryRoot
$script:FailureAt=''
$script:RestoreFails=$false
function Get-ReclaimDisks { return @([PSCustomObject]@{Distro='Ubuntu-20.04';Path='D:\fake.vhdx';BeforeBytes=100}) }
function Get-PSDrive { param($Name) return [PSCustomObject]@{Free=1000} }
function Test-Path { param($Path) return $false }
function Set-ReclaimPhase { param($Phase,$Directory) $script:Events.Add($Phase) }
function Invoke-LinuxCleanup { param($Directory,[switch]$Preflight) if($Preflight){$script:Events.Add('preflight')}else{$script:Events.Add('cleanup')};if($script:FailureAt -eq 'cleanup' -and -not $Preflight){throw 'cleanup failed'} }
function Invoke-Checked { param($Executable,$Arguments,$Log,$Timeout) if($Arguments -contains '/usr/sbin/fstrim'){$script:Events.Add('trim');if($script:FailureAt -eq 'trim'){throw 'trim failed'}};return '' }
function Stop-ReclaimServices { param($Docker,$Containers,$Directory,$DockerWasRunning) $script:Events.Add('stop');if($script:FailureAt -eq 'stop'){throw 'stop failed'} }
function Compact-ReclaimDisk { param($Disk,$Directory) $script:Events.Add('compact');if($script:FailureAt -eq 'compact'){throw 'compact failed'};return [PSCustomObject]@{BeforeBytes=100;AfterBytes=50} }
function Restore-ReclaimServices { param($Docker,$Containers,$Directory,$DockerWasRunning) $script:Events.Add('restore');if($script:RestoreFails){throw 'restore failed'} }
# Administrator check is the sole Windows dependency; run this test elevated
# or patch only Invoke-Reclaim's principal check in the in-memory function.
$definition=(Get-Command Invoke-Reclaim).Definition
$definition=[regex]::Replace($definition,'(?s)\$principal = New-Object.*?\n    \$directory =','    $directory =')
Set-Item Function:\Invoke-Reclaim ([scriptblock]::Create($definition))
foreach($failure in @('','cleanup','trim','stop','compact')) {
 $script:Events.Clear();$script:FailureAt=$failure
 $code=Invoke-Reclaim
 $observed=@($script:Events)
 if($failure -eq '') {
  if($code -ne 0){throw 'Successful workflow did not succeed'}
  foreach($step in @('cleanup','trim','stop','compact','restore')){if($step -notin $observed){throw "Missing phase $step"}}
  if([array]::IndexOf($observed,'cleanup') -ge [array]::IndexOf($observed,'trim') -or [array]::IndexOf($observed,'trim') -ge [array]::IndexOf($observed,'stop')){throw 'Unsafe phase order'}
 } else {
  if($code -ne 1){throw "Failure reported success: $failure"}
  if($failure -in @('cleanup','trim') -and 'stop' -in $observed){throw 'Stopped after failed preparation'}
  if($failure -in @('stop','compact') -and 'restore' -notin $observed){throw 'Did not restore services after failure'}
 }
}
$script:Events.Clear();$script:FailureAt='';$script:RestoreFails=$true
if((Invoke-Reclaim) -ne 1){throw 'Restore failure was reported as success'}
# The quoting function is real, not mocked.
if((Quote-NativeArgument 'D:\a b\script.ps1') -ne '"D:\a b\script.ps1"'){throw 'Space quoting failed'}
if((Quote-NativeArgument 'a"b') -ne '"a\"b"'){throw 'Quote escaping failed'}
Write-Host 'Mock workflow checks passed; no cleanup, service stop or compaction executed.'
