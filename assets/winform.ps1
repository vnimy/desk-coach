param(
	[string]$PlanPath,
	[string]$Slot,
	[string]$Title = "Reminder",
	[string]$SchedulePath = "",
	[string]$PythonExe = "",
	[string]$ScriptPath = "",
	[double]$Scale = 0,
	[string]$CapturePath = "",
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
# High-DPI: the process declares DPI awareness (per-monitor v2 when available, with
# graceful fallbacks) and the whole layout is designed on a 96 DPI grid, then multiplied
# by the monitor scale factor. Fonts use pixel units so glyph rasterization always
# matches the layout: text stays crisp on 100%..300% screens instead of being
# bitmap-stretched (blurry) by Windows. The layout also clamps itself into the working
# area, so the bottom button bar is never pushed off small/scaled screens.
#
# Closing rules: the window may only be dismissed by
#   - the OK button
#   - the dropdown's "never again" item
#   - the window close button (X) / Alt+F4
#   - ESC
# Opening the schedule, and pausing today, must NOT close it.
# The schedule button also stays enabled after a click: the user may close the
# schedule page and want to open it again from the same popup.
#
# Exit codes (read back by reminder.py):
#   0 = acknowledged (OK / X / ESC), no state change
#   3 = never show this slot again

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# DPI awareness. MUST run before the first WinForms / GDI+ call in this
# process: the awareness is locked on first use and cannot be changed later.
# ---------------------------------------------------------------------------
Add-Type -Namespace Native -Name DpiHelper -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true)]
public static extern bool SetProcessDpiAwarenessContext(IntPtr value);

[DllImport("user32.dll", SetLastError = true)]
public static extern bool SetProcessDPIAware();

[DllImport("shcore.dll", SetLastError = true)]
public static extern int SetProcessDpiAwareness(int value);
'@

# 0 = unaware, 1 = system aware, 2 = per-monitor aware (v2)
$script:awareness = 0
try {
	# -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
	if ([Native.DpiHelper]::SetProcessDpiAwarenessContext([IntPtr](-4))) { $script:awareness = 2 }
} catch { }
if ($script:awareness -eq 0) {
	try { if ([Native.DpiHelper]::SetProcessDpiAwareness(2) -eq 0) { $script:awareness = 2 } } catch { }
}
if ($script:awareness -eq 0) {
	# Legacy fallback (Vista+): system DPI aware, crisp on the primary monitor.
	try { if ([Native.DpiHelper]::SetProcessDPIAware()) { $script:awareness = 1 } } catch { }
}

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

# ---------------------------------------------------------------------------
# Layout metrics, designed on a 96 DPI grid (logical pixels). Px() converts a
# logical value to physical pixels using the current scale factor.
# ---------------------------------------------------------------------------
$DLG_W = 680         # dialog width
$PAD = 24            # horizontal padding
$HEADER_H = 84       # green header band
$GAP = 16            # vertical gap between blocks
$BODY_H = 352        # steps panel (scrollable)
$TRAP_H = 132        # trap panel (scrollable)
$STAT_H = 20         # status line height (hidden when it has no text)
$BTN_GAP = 10
$BTN_H = 44          # touch-friendly button height
$PAD_BOTTOM = 16
$BODY_H_MIN = 140    # shrink floors used when the screen is too short
$TRAP_H_MIN = 88

function Get-AutoScale {
	# DPI of the monitor the dialog will appear on (the primary screen).
	try {
		$g = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
		$dpi = $g.DpiX
		$g.Dispose()
		if ($dpi -ge 96) { return [double]$dpi / 96.0 }
	} catch { }
	# Fallback: the user's configured log pixels (HKCU, per-user scaling).
	try {
		$v = (Get-ItemProperty -Path "HKCU:\Control Panel\Desktop" -Name LogPixels -ErrorAction Stop).LogPixels
		if ($v -ge 96) { return [double]$v / 96.0 }
	} catch { }
	return 1.0
}

if ($Scale -gt 0) {
	# Forced scale: preview / debug knob (e.g. -Scale 2 to preview a 200% screen).
	$script:scale = $Scale
} else {
	$script:scale = Get-AutoScale
}
if ($script:scale -lt 1.0) { $script:scale = 1.0 }
if ($script:scale -gt 4.0) { $script:scale = 4.0 }

function Px([double]$v) { [int][math]::Round($v * $script:scale) }

function PxFont {
	param(
		[double]$px,
		[System.Drawing.FontStyle]$style = [System.Drawing.FontStyle]::Regular
	)
	return New-Object System.Drawing.Font($FONT, [double](Px($px)), $style, [System.Drawing.GraphicsUnit]::Pixel)
}

# Status line: hidden unless there is something to say (the old static hint is gone).
function Set-Status([string]$text, [System.Drawing.Color]$color) {
	if (-not $script:status) { return }
	if ([string]::IsNullOrWhiteSpace($text)) {
		$script:status.Visible = $false
		return
	}
	$script:status.Text = $text
	$script:status.ForeColor = $color
	$script:status.Visible = $true
}

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

# ---------------------------------------------------------------------------
# Build-Ui lays out every control for the given scale. It is re-run whenever
# the dialog moves to a monitor with a different DPI (DpiChanged below).
# ---------------------------------------------------------------------------
function Build-Ui {
	param([double]$newScale)
	$script:scale = $newScale

	# Drop the previous layout (disposes old controls and their fonts).
	if ($script:form) {
		while ($script:form.Controls.Count -gt 0) {
			$c = $script:form.Controls[0]
			$script:form.Controls.RemoveAt(0)
			$c.Dispose()
		}
	}
	if ($script:menu) { $script:menu.Dispose(); $script:menu = $null }

	# Clamp the dialog into the working area so short/scaled screens never cut
	# the bottom button bar off; the two scrollable panels absorb the deficit.
	$wa = $null
	try { $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea } catch { }
	$dlgW = $DLG_W
	if ($wa -and ((Px $dlgW) -gt [int]($wa.Width * 0.92))) {
		$dlgW = [int][math]::Floor(($wa.Width * 0.92) / $script:scale)
		if ($dlgW -lt 480) { $dlgW = 480 }
	}
	$cw = $dlgW - 2 * $PAD

	$bodyH = $BODY_H
	$trapH = $TRAP_H
	$total = $HEADER_H + $GAP + $bodyH + $GAP + $trapH + $GAP + $STAT_H + $BTN_GAP + $BTN_H + $PAD_BOTTOM
	$script:form.ClientSize = New-Object System.Drawing.Size((Px $dlgW), (Px $total))
	if (-not $script:form.IsHandleCreated) { $null = $script:form.Handle }
	if ($wa) {
		$nc = $script:form.Height - $script:form.ClientSize.Height
		$maxLogical = [int][math]::Floor(([int]($wa.Height * 0.92) - $nc) / $script:scale)
		if ($total -gt $maxLogical) {
			$deficit = $total - $maxLogical
			$cut = [math]::Min($deficit, [math]::Max(0, $trapH - $TRAP_H_MIN))
			$trapH = $trapH - $cut
			$deficit = $deficit - $cut
			if ($deficit -gt 0) {
				$cut = [math]::Min($deficit, [math]::Max(0, $bodyH - $BODY_H_MIN))
				$bodyH = $bodyH - $cut
			}
			$total = $HEADER_H + $GAP + $bodyH + $GAP + $trapH + $GAP + $STAT_H + $BTN_GAP + $BTN_H + $PAD_BOTTOM
			$script:form.ClientSize = New-Object System.Drawing.Size((Px $dlgW), (Px $total))
		}
	}

	$bodyY = $HEADER_H + $GAP
	$trapY = $bodyY + $bodyH + $GAP
	$statY = $trapY + $trapH + $GAP
	$btnY = $statY + $STAT_H + $BTN_GAP

	$header = New-Object System.Windows.Forms.Panel
	$header.BackColor = [System.Drawing.Color]::FromArgb(16, 122, 102)
	$header.Location = New-Object System.Drawing.Point(0, 0)
	$header.Size = New-Object System.Drawing.Size((Px $dlgW), (Px $HEADER_H))
	$script:form.Controls.Add($header)

	$lblTime = New-Object System.Windows.Forms.Label
	$lblTime.Text = $m.time
	$lblTime.Font = PxFont 27 ([System.Drawing.FontStyle]::Bold)
	$lblTime.ForeColor = [System.Drawing.Color]::White
	$lblTime.Location = New-Object System.Drawing.Point((Px 24), (Px 14))
	$lblTime.Size = New-Object System.Drawing.Size((Px 160), (Px 42))
	$header.Controls.Add($lblTime)

	$lblName = New-Object System.Windows.Forms.Label
	$lblName.Text = $mName
	$lblName.Font = PxFont 20 ([System.Drawing.FontStyle]::Bold)
	$lblName.ForeColor = [System.Drawing.Color]::White
	$lblName.Location = New-Object System.Drawing.Point((Px 196), (Px 16))
	$lblName.Size = New-Object System.Drawing.Size((Px ($dlgW - 196 - $PAD)), (Px 32))
	$header.Controls.Add($lblName)

	$lblDose = New-Object System.Windows.Forms.Label
	$lblDose.Text = $mDose
	$lblDose.Font = PxFont 13
	$lblDose.ForeColor = [System.Drawing.Color]::FromArgb(227, 242, 237)
	$lblDose.Location = New-Object System.Drawing.Point((Px 196), (Px 52))
	$lblDose.Size = New-Object System.Drawing.Size((Px ($dlgW - 196 - $PAD)), (Px 24))
	$header.Controls.Add($lblDose)

	$bodyPanel = New-Object System.Windows.Forms.Panel
	$bodyPanel.Location = New-Object System.Drawing.Point((Px $PAD), (Px $bodyY))
	$bodyPanel.Size = New-Object System.Drawing.Size((Px $cw), (Px $bodyH))
	$bodyPanel.AutoScroll = $true
	$bodyPanel.BackColor = [System.Drawing.Color]::White
	$script:form.Controls.Add($bodyPanel)

	$bodyLabel = New-Object System.Windows.Forms.Label
	$bodyLabel.MaximumSize = New-Object System.Drawing.Size((Px ($cw - 56)), 0)
	$bodyLabel.AutoSize = $true
	$bodyLabel.Location = New-Object System.Drawing.Point((Px 4), (Px 4))
	$bodyLabel.Font = PxFont 16
	$bodyLabel.ForeColor = [System.Drawing.Color]::FromArgb(35, 35, 35)
	$bodyLabel.Text = $bodyText
	$bodyPanel.Controls.Add($bodyLabel)

	$trapPanel = New-Object System.Windows.Forms.Panel
	$trapPanel.Location = New-Object System.Drawing.Point((Px $PAD), (Px $trapY))
	$trapPanel.Size = New-Object System.Drawing.Size((Px $cw), (Px $trapH))
	$trapPanel.AutoScroll = $true
	$trapPanel.BackColor = [System.Drawing.Color]::FromArgb(255, 243, 227)
	$script:form.Controls.Add($trapPanel)

	$trapLabel = New-Object System.Windows.Forms.Label
	$trapLabel.MaximumSize = New-Object System.Drawing.Size((Px ($cw - 60)), 0)
	$trapLabel.AutoSize = $true
	$trapLabel.Location = New-Object System.Drawing.Point((Px 8), (Px 8))
	$trapLabel.Font = PxFont 15
	$trapLabel.ForeColor = [System.Drawing.Color]::FromArgb(140, 74, 12)
	$trapLabel.Text = $ui.trap + " " + $mTrap
	$trapPanel.Controls.Add($trapLabel)

	# Status line: reports the result of opening the schedule / pausing.
	# Occupies its own band and never overlaps the buttons; hidden when empty.
	$script:status = New-Object System.Windows.Forms.Label
	$script:status.Font = PxFont 12
	$script:status.ForeColor = [System.Drawing.Color]::FromArgb(107, 107, 107)
	$script:status.Location = New-Object System.Drawing.Point((Px $PAD), (Px $statY))
	$script:status.Size = New-Object System.Drawing.Size((Px $cw), (Px $STAT_H))
	$script:form.Controls.Add($script:status)
	Set-Status $ui.footer ([System.Drawing.Color]::FromArgb(107, 107, 107))

	# Bottom left: open schedule. Deliberately does NOT close the window.
	$script:btnSchedule = New-Object System.Windows.Forms.Button
	$script:btnSchedule.Text = $ui.schedule
	$script:btnSchedule.Font = PxFont 13
	$script:btnSchedule.Size = New-Object System.Drawing.Size((Px 148), (Px $BTN_H))
	$script:btnSchedule.Location = New-Object System.Drawing.Point((Px $PAD), (Px $btnY))
	$script:btnSchedule.FlatStyle = "Flat"
	$script:btnSchedule.BackColor = [System.Drawing.Color]::FromArgb(238, 238, 235)
	$script:btnSchedule.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
	$script:btnSchedule.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(205, 205, 200)
	$script:form.Controls.Add($script:btnSchedule)

	$scheduleReady = $false
	if (-not [string]::IsNullOrWhiteSpace($SchedulePath)) {
		if ($SchedulePath -match '^https?://') { $scheduleReady = $true }
		elseif (Test-Path $SchedulePath) { $scheduleReady = $true }
	}

	if (-not $scheduleReady) {
		$script:btnSchedule.Enabled = $false
		$script:btnSchedule.ForeColor = [System.Drawing.Color]::Silver
	} else {
		$script:btnSchedule.Add_Click({
			# Stays clickable: the user may close the schedule page and want it
			# back. A disabled button turned that into a dead end.
			if (Open-Schedule) {
				Set-Status $ui.scheduleOpened ([System.Drawing.Color]::FromArgb(107, 107, 107))
			} else {
				Set-Status $ui.scheduleFailed ([System.Drawing.Color]::FromArgb(156, 74, 34))
			}
		})
	}

	# Bottom right: OK button (closes) + dropdown, flush with the content edge.
	$xMore = $dlgW - $PAD - 38
	$xOk = $xMore - 6 - 150

	$script:btn = New-Object System.Windows.Forms.Button
	$script:btn.Text = $ui.ok
	$script:btn.Font = PxFont 15 ([System.Drawing.FontStyle]::Bold)
	$script:btn.Size = New-Object System.Drawing.Size((Px 150), (Px $BTN_H))
	$script:btn.Location = New-Object System.Drawing.Point((Px $xOk), (Px $btnY))
	$script:btn.FlatStyle = "Flat"
	$script:btn.BackColor = [System.Drawing.Color]::FromArgb(16, 122, 102)
	$script:btn.ForeColor = [System.Drawing.Color]::White
	$script:btn.FlatAppearance.BorderSize = 0
	$script:btn.Add_Click({ $script:form.Close() })
	$script:form.Controls.Add($script:btn)

	$script:btnMore = New-Object System.Windows.Forms.Button
	$script:btnMore.Text = [char]0x25BE
	$script:btnMore.Font = PxFont 13
	$script:btnMore.Size = New-Object System.Drawing.Size((Px 38), (Px $BTN_H))
	$script:btnMore.Location = New-Object System.Drawing.Point((Px $xMore), (Px $btnY))
	$script:btnMore.FlatStyle = "Flat"
	$script:btnMore.BackColor = [System.Drawing.Color]::FromArgb(12, 98, 82)
	$script:btnMore.ForeColor = [System.Drawing.Color]::White
	$script:btnMore.FlatAppearance.BorderSize = 0
	$script:form.Controls.Add($script:btnMore)

	$script:menu = New-Object System.Windows.Forms.ContextMenuStrip
	$script:menu.Font = PxFont 13
	$script:menu.ShowImageMargin = $false

	# Pause today: writes state via a hidden child process, then stays open.
	$script:itemPause = $script:menu.Items.Add($ui.pauseToday)
	$script:itemPause.Add_Click({
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
			Set-Status $ui.paused ([System.Drawing.Color]::FromArgb(15, 110, 86))
			$script:itemPause.Enabled = $false
		} else {
			Set-Status $ui.pauseFailed ([System.Drawing.Color]::FromArgb(156, 74, 34))
		}
	})

	# Never again: permanently disables this slot, then closes.
	$script:itemDisable = $script:menu.Items.Add($ui.disable)
	$script:itemDisable.Add_Click({ $script:action = 3; $script:form.Close() })

	$script:btnMore.Add_Click({
		$script:menu.Show($script:btnMore, 0, $script:btnMore.Height)
	})

	$script:form.AcceptButton = $script:btn
	$script:form.CancelButton = $script:btn
}

$script:form = New-Object System.Windows.Forms.Form
$script:form.Text = $Title
$script:form.FormBorderStyle = "FixedSingle"
$script:form.MaximizeBox = $false
$script:form.MinimizeBox = $false
$script:form.TopMost = $true
$script:form.BackColor = [System.Drawing.Color]::White
# Layout is fully manual (scaled by Px); disable WinForms font auto-scaling.
$script:form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None
$script:form.StartPosition = "CenterScreen"
$script:form.Font = PxFont 13

$script:form.Add_Shown({
	$script:form.Activate()
	if (-not $CapturePath) { [System.Media.SystemSounds]::Exclamation.Play() }
})

# Re-layout when the dialog lands on a monitor with a different DPI. If the
# runtime does not deliver the event, the startup layout still matches the
# monitor the dialog opens on, so nothing breaks.
try {
	$script:form.Add_DpiChanged({
		param($sender, $e)
		try {
			if ($e.SuggestedRectangle.Width -gt 0) { $sender.Bounds = $e.SuggestedRectangle }
			Build-Ui ([double]$e.DeviceDpiNew / 96.0)
		} catch { }
	})
} catch { }

Build-Ui $script:scale

if ($ParseOnly) {
	Write-Output ("PARSE OK  time=" + $m.time + "  name=" + $mName + "  steps=" + @($mSteps).Count + "  scheduleBtn=" + $script:btnSchedule.Enabled + "  menuItems=" + $script:menu.Items.Count + "  browser=" + (Get-DefaultBrowser) + "  awareness=" + $script:awareness + "  scale=" + $script:scale + "  client=" + $script:form.ClientSize)
	exit 0
}

if (-not [string]::IsNullOrWhiteSpace($CapturePath)) {
	# Off-screen render to a PNG (visual smoke test; no dialog, no sound).
	$geo = @()
	$geo += ("awareness=" + $script:awareness + "  scale=" + $script:scale + "  form=" + $script:form.Size + "  client=" + $script:form.ClientSize)
	foreach ($c in $script:form.Controls) {
		$geo += ("  " + $c.GetType().Name + "  bounds=" + $c.Bounds + "  text=[" + $c.Text + "]")
	}
	[System.IO.File]::WriteAllText(($CapturePath + ".geo.txt"), ($geo -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
	$form.StartPosition = "Manual"
	$form.Location = New-Object System.Drawing.Point(-4000, 200)
	$form.Show()
	[System.Windows.Forms.Application]::DoEvents()
	Start-Sleep -Milliseconds 400
	[System.Windows.Forms.Application]::DoEvents()
	$w = [int]$form.Width
	$h = [int]$form.Height
	$bmp = New-Object System.Drawing.Bitmap($w, $h)
	$rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
	$form.DrawToBitmap($bmp, $rect)
	$bmp.Save($CapturePath, [System.Drawing.Imaging.ImageFormat]::Png)
	$bmp.Dispose()
	$form.Close()
	exit $script:action
}

[void]$form.ShowDialog()
exit $script:action
