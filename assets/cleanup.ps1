param(
	[switch]$DryRun
)

# Cleanup helper: find and remove every scheduled task this skill created.
#
# CRITICAL: keep this file pure ASCII (including comments). Windows PowerShell 5.1
# reads a BOM-less .ps1 using the local ANSI code page, so non-ASCII bytes can pair with
# the following ASCII character and swallow newlines, corrupting later lines.
#
# Why this exists: the normal uninstall path reads the task name from state.json.
# If the user deletes the whole plan/state directory (a natural thing to do when
# retiring the skill), that information is gone and the task would be orphaned --
# every logon would then try to launch a script that no longer exists.
# So here we identify tasks by INSPECTING THEIR ACTIONS for this skill's script path,
# which works even after the skill directory itself has been deleted.

$ErrorActionPreference = "Stop"

$needle = "scheduled-desktop-reminder"
$removed = @()
$found = @()

foreach ($task in (Get-ScheduledTask -ErrorAction SilentlyContinue)) {
	$hit = $false
	foreach ($action in $task.Actions) {
		# Execute holds the interpreter, Arguments holds the script path
		$text = [string]$action.Execute + " " + [string]$action.Arguments
		if ($text -like "*$needle*") { $hit = $true; break }
	}
	if (-not $hit) { continue }

	$found += $task.TaskName
	if ($DryRun) { continue }

	# A running instance blocks removal, so stop it first
	try {
		if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue }
	} catch { }
	try {
		Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false -ErrorAction Stop
		$removed += $task.TaskName
	} catch {
		Write-Output ("FAILED " + $task.TaskName + "  " + $_.Exception.Message)
	}
}

# Also kill any service process still running from this skill, so cleanup leaves nothing behind
$killed = 0
if (-not $DryRun) {
	$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
		$_.Name -like "python*" -and $_.CommandLine -like "*$needle*"
	}
	foreach ($p in $procs) {
		try {
			Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
			$killed++
		} catch { }
	}
}

if ($found.Count -eq 0) {
	Write-Output "NONE"
	exit 0
}

if ($DryRun) {
	foreach ($name in $found) { Write-Output ("WOULD-REMOVE " + $name) }
	Write-Output ("TOTAL " + $found.Count)
	exit 0
}

foreach ($name in $removed) { Write-Output ("REMOVED " + $name) }
Write-Output ("TASKS " + $removed.Count + "  PROCESSES " + $killed)
