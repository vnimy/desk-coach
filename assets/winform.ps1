param(
	[string]$PlanPath,
	[string]$Slot,
	[string]$Title = "Reminder",
	[string]$SchedulePath = "",
	[string]$PythonExe = "",
	[string]$ScriptPath = "",
	[switch]$ParseOnly
)

# WinForms renderer, invoked by scripts/reminder.py via -File.
#
# CRITICAL: this file MUST stay pure ASCII, including its comments.
# Windows PowerShell 5.1 reads a BOM-less .ps1 using the local ANSI code page (GBK on
# Chinese Windows). If non-ASCII bytes appear here, a trailing byte can pair up with the
# following ASCII character, which swallows newlines and shifts every later line. That
# silently corrupts the script (wrong line numbers, bogus property errors).
# All user-visible Chinese text therefore lives in the UTF-8 plan JSON, never here.
#
# Closing rules: the window may only be dismissed by
#   - the OK button
#   - the dropdown's "never again" item
#   - the window close button (X) / Alt+F4
#   - ESC
# Opening the schedule, and pausing today, must NOT close it.
#
# Exit codes (read back by reminder.py):
#   0 = acknowledged (OK / X / ESC), no state change
#   3 = never show this slot again

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PlanPath)) { exit 1 }

$raw = [System.IO.File]::ReadAllText($PlanPath, [System.Text.Encoding]::UTF8)
$plan = $raw | ConvertFrom-Json

$ui = $plan.ui
if ($null -eq $ui) {
	$ui = @{
		ok = "OK"; trap = "Note:"; footer = ""
		schedule = "Schedule"; scheduleOpened = "Opened"; scheduleFailed = "Open failed"
		pauseToday = "Pause today"; paused = "Paused for today"; pauseFailed = "Pause failed"
		disable = "Never again"
	}
}

# Slots live in a "slots" array, not as top-level keys.
$m = $null
foreach ($candidate in $plan.slots) {
	if ($candidate.id -eq $Slot) { $m = $candidate; break }
}
if ($null -eq $m) { exit 1 }

# A slot may reference a shared action ("action": "breath"). Resolve the reference so
# the same wording is maintained in one place. Slot-level fields win when present.
$act = $null
if ($m.action -and $plan.actions) {
	$act = $plan.actions.PSObject.Properties[$m.action].Value
}

function Pick-Text($own, $shared) {
	if (-not [string]::IsNullOrWhiteSpace($own)) { return $own }
	if ($shared) { return $shared }
	return ""
}

$mName = Pick-Text $m.name $(if ($act) { $act.name })
$mDose = Pick-Text $(if ($act) { $act.dose }) $m.dose
$mTrap = Pick-Text $(if ($act) { $act.trap }) $m.trap
$mSteps = if ($m.steps) { $m.steps } elseif ($act) { $act.steps } else { @() }

$script:action = 0

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$FONT = "Microsoft YaHei UI"

# Resolve the default browser via the http URL association, so the schedule always
# opens in a browser even if .html happens to be associated with an editor.
function Get-DefaultBrowser {
	$progId = $null
	try {
		$progId = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice" -ErrorAction Stop).ProgId
	} catch { }
	if (-not $progId) { return $null }
	$cmd = $null
	foreach ($root in @("HKCU:\Software\Classes", "HKLM:\Software\Classes")) {
		try {
			$cmd = (Get-ItemProperty -Path ($root + "\" + $progId + "\shell\open\command") -ErrorAction Stop)."(default)"
			if ($cmd) { break }
		} catch { }
	}
	if (-not $cmd) { return $null }
	if ($cmd -match '^\s*"([^"]+)"') { return $matches[1] }
	if ($cmd -match '^\s*(\S+?\.exe)') { return $matches[1] }
	return $null
}

function Open-Schedule {
	try {
		# http(s) target: hand straight to the default browser.
		if ($SchedulePath -match '^https?://') {
			$browser = Get-DefaultBrowser
			if ($browser -and (Test-Path $browser)) {
				Start-Process -FilePath $browser -ArgumentList ('"' + $SchedulePath + '"')
			} else {
				Start-Process -FilePath $SchedulePath
			}
			return $true
		}
		if (-not (Test-Path $SchedulePath)) { return $false }
		$browser = Get-DefaultBrowser
		if ($browser -and (Test-Path $browser)) {
			Start-Process -FilePath $browser -ArgumentList ('"' + $SchedulePath + '"')
		} else {
			# Fall back to the shell association for .html
			Start-Process -FilePath $SchedulePath
		}
		return $true
	} catch {
		return $false
	}
}

$sb = New-Object System.Text.StringBuilder
$i = 0
foreach ($s in $mSteps) {
	$i++
	$num = [char](9311 + $i)
	$null = $sb.AppendLine($num + "  " + $s)
	if ($i -lt @($mSteps).Count) { $null = $sb.AppendLine() }
}
$bodyText = $sb.ToString()

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Size = New-Object System.Drawing.Size(680, 730)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font($FONT, 10)

$header = New-Object System.Windows.Forms.Panel
$header.BackColor = [System.Drawing.Color]::FromArgb(16, 122, 102)
$header.Location = New-Object System.Drawing.Point(0, 0)
$header.Size = New-Object System.Drawing.Size(680, 84)
$form.Controls.Add($header)

$lblTime = New-Object System.Windows.Forms.Label
$lblTime.Text = $m.time
$lblTime.Font = New-Object System.Drawing.Font($FONT, 20, [System.Drawing.FontStyle]::Bold)
$lblTime.ForeColor = [System.Drawing.Color]::White
$lblTime.Location = New-Object System.Drawing.Point(24, 16)
$lblTime.Size = New-Object System.Drawing.Size(160, 34)
$header.Controls.Add($lblTime)

$lblName = New-Object System.Windows.Forms.Label
$lblName.Text = $mName
$lblName.Font = New-Object System.Drawing.Font($FONT, 15, [System.Drawing.FontStyle]::Bold)
$lblName.ForeColor = [System.Drawing.Color]::White
$lblName.Location = New-Object System.Drawing.Point(180, 16)
$lblName.Size = New-Object System.Drawing.Size(476, 30)
$header.Controls.Add($lblName)

$lblDose = New-Object System.Windows.Forms.Label
$lblDose.Text = $mDose
$lblDose.Font = New-Object System.Drawing.Font($FONT, 10)
$lblDose.ForeColor = [System.Drawing.Color]::FromArgb(206, 235, 226)
$lblDose.Location = New-Object System.Drawing.Point(182, 52)
$lblDose.Size = New-Object System.Drawing.Size(476, 22)
$header.Controls.Add($lblDose)

$bodyPanel = New-Object System.Windows.Forms.Panel
$bodyPanel.Location = New-Object System.Drawing.Point(24, 100)
$bodyPanel.Size = New-Object System.Drawing.Size(632, 356)
$bodyPanel.AutoScroll = $true
$bodyPanel.BackColor = [System.Drawing.Color]::White
$form.Controls.Add($bodyPanel)

$bodyLabel = New-Object System.Windows.Forms.Label
$bodyLabel.MaximumSize = New-Object System.Drawing.Size(576, 0)
$bodyLabel.AutoSize = $true
$bodyLabel.Location = New-Object System.Drawing.Point(4, 4)
$bodyLabel.Font = New-Object System.Drawing.Font($FONT, 12)
$bodyLabel.ForeColor = [System.Drawing.Color]::FromArgb(35, 35, 35)
$bodyLabel.Text = $bodyText
$bodyPanel.Controls.Add($bodyLabel)

$trapPanel = New-Object System.Windows.Forms.Panel
$trapPanel.Location = New-Object System.Drawing.Point(24, 468)
$trapPanel.Size = New-Object System.Drawing.Size(632, 130)
$trapPanel.AutoScroll = $true
$trapPanel.BackColor = [System.Drawing.Color]::FromArgb(255, 243, 227)
$form.Controls.Add($trapPanel)

$trapLabel = New-Object System.Windows.Forms.Label
$trapLabel.MaximumSize = New-Object System.Drawing.Size(572, 0)
$trapLabel.AutoSize = $true
$trapLabel.Location = New-Object System.Drawing.Point(8, 8)
$trapLabel.Font = New-Object System.Drawing.Font($FONT, 11)
$trapLabel.ForeColor = [System.Drawing.Color]::FromArgb(140, 74, 12)
$trapLabel.Text = $ui.trap + " " + $mTrap
$trapPanel.Controls.Add($trapLabel)

# Status line: also used to report the result of opening the schedule / pausing.
$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = $ui.footer
$statusLabel.Font = New-Object System.Drawing.Font($FONT, 8)
$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
$statusLabel.Location = New-Object System.Drawing.Point(24, 600)
$statusLabel.Size = New-Object System.Drawing.Size(560, 18)
$form.Controls.Add($statusLabel)

# Bottom left: open schedule. Deliberately does NOT close the window.
$btnSchedule = New-Object System.Windows.Forms.Button
$btnSchedule.Text = $ui.schedule
$btnSchedule.Font = New-Object System.Drawing.Font($FONT, 10)
$btnSchedule.Size = New-Object System.Drawing.Size(148, 40)
$btnSchedule.Location = New-Object System.Drawing.Point(24, 616)
$btnSchedule.FlatStyle = "Flat"
$btnSchedule.BackColor = [System.Drawing.Color]::FromArgb(238, 238, 235)
$btnSchedule.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
$btnSchedule.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(205, 205, 200)
$form.Controls.Add($btnSchedule)

$scheduleReady = $false
if (-not [string]::IsNullOrWhiteSpace($SchedulePath)) {
	if ($SchedulePath -match '^https?://') { $scheduleReady = $true }
	elseif (Test-Path $SchedulePath) { $scheduleReady = $true }
}

if (-not $scheduleReady) {
	$btnSchedule.Enabled = $false
	$btnSchedule.ForeColor = [System.Drawing.Color]::Silver
} else {
	$btnSchedule.Add_Click({
		if (Open-Schedule) {
			# Keep the label as-is; only grey it out so the window stays put.
			$btnSchedule.Enabled = $false
			$btnSchedule.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
			$statusLabel.Text = $ui.footer
		} else {
			$statusLabel.Text = $ui.scheduleFailed
		}
	})
}

# Bottom right: OK button (closes) + dropdown
$btn = New-Object System.Windows.Forms.Button
$btn.Text = $ui.ok
$btn.Font = New-Object System.Drawing.Font($FONT, 11, [System.Drawing.FontStyle]::Bold)
$btn.Size = New-Object System.Drawing.Size(150, 40)
$btn.Location = New-Object System.Drawing.Point(440, 616)
$btn.FlatStyle = "Flat"
$btn.BackColor = [System.Drawing.Color]::FromArgb(16, 122, 102)
$btn.ForeColor = [System.Drawing.Color]::White
$btn.FlatAppearance.BorderSize = 0
$btn.Add_Click({ $form.Close() })
$form.Controls.Add($btn)

$btnMore = New-Object System.Windows.Forms.Button
$btnMore.Text = [char]0x25BE
$btnMore.Font = New-Object System.Drawing.Font($FONT, 10)
$btnMore.Size = New-Object System.Drawing.Size(38, 40)
$btnMore.Location = New-Object System.Drawing.Point(594, 616)
$btnMore.FlatStyle = "Flat"
$btnMore.BackColor = [System.Drawing.Color]::FromArgb(12, 98, 82)
$btnMore.ForeColor = [System.Drawing.Color]::White
$btnMore.FlatAppearance.BorderSize = 0
$form.Controls.Add($btnMore)

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$menu.Font = New-Object System.Drawing.Font($FONT, 10)
$menu.ShowImageMargin = $false

# Pause today: writes state via a hidden child process, then stays open.
$itemPause = $menu.Items.Add($ui.pauseToday)
$itemPause.Add_Click({
	$ok = $false
	if ($PythonExe -and $ScriptPath -and (Test-Path $PythonExe) -and (Test-Path $ScriptPath)) {
		try {
			$argLine = '"' + $ScriptPath + '" pause --plan "' + $PlanPath + '"'
			$proc = Start-Process -FilePath $PythonExe -ArgumentList $argLine -WindowStyle Hidden -Wait -PassThru
			$ok = ($proc.ExitCode -eq 0)
		} catch {
			$ok = $false
		}
	}
	if ($ok) {
		$statusLabel.Text = $ui.paused
		$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(15, 110, 86)
		$itemPause.Enabled = $false
	} else {
		$statusLabel.Text = $ui.pauseFailed
		$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(156, 74, 34)
	}
})

# Never again: permanently disables this slot, then closes.
$itemDisable = $menu.Items.Add($ui.disable)
$itemDisable.Add_Click({ $script:action = 3; $form.Close() })

$btnMore.Add_Click({
	$menu.Show($btnMore, 0, $btnMore.Height)
})

$form.AcceptButton = $btn
$form.CancelButton = $btn

$form.Add_Shown({
	$form.Activate()
	[System.Media.SystemSounds]::Exclamation.Play()
})

if ($ParseOnly) {
	Write-Output ("PARSE OK  time=" + $m.time + "  name=" + $mName + "  steps=" + @($mSteps).Count + "  scheduleBtn=" + $btnSchedule.Enabled + "  menuItems=" + $menu.Items.Count + "  browser=" + (Get-DefaultBrowser))
	exit 0
}

[void]$form.ShowDialog()
exit $script:action
