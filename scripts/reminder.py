#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时桌面提醒器 —— 把一份「提醒计划」装成本机系统级定时任务。

设计前提（重要）:
    固定文案的定时提醒不要依赖任何在线 / LLM 链路。推送这类内容不需要 AI 现场造句，
    一旦依赖模型，到点就会变成「等它慢慢生成」，服务端繁忙时直接卡死不产出。
    本脚本因此全程离线：文案来自本地 JSON，投递走操作系统自带的调度器。

架构（2026-10 重构）:
    一个计划 = 一个系统任务 + 多个触发器，绝不按时间点拆成多个任务。
    各平台都不支持给同一任务的多个触发器分别传参数，所以触发时不传时间，
    而是由脚本读取「当前系统时间」去 plan 里匹配对应的日程 —— 触发时刻就是日程时刻。

    系统任务名可以用 --name 指定（如 CoreReminder），触发器数量 = slots 数量。

运行状态与内容分离:
    plan.json   —— 提醒内容（动作文案），改它立刻生效
    state.json  —— 运行状态（总开关 / 暂停到何时 / 已永久关闭的环节），与 plan 同目录

支持平台:
    Windows -> 任务计划程序 + WinForms 弹窗（带日程表按钮与下拉操作）
    macOS   -> launchd LaunchAgent + osascript 对话框
    Linux   -> crontab + zenity / notify-send

用法:
    python reminder.py preview   --plan plan.json [--slot 1030] [--show]
    python reminder.py install   --plan plan.json --name core [--schedule 日程表.html]
    python reminder.py status    --plan plan.json --name core
    python reminder.py pause     --plan plan.json [--date YYYY-MM-DD]
    python reminder.py resume    --plan plan.json
    python reminder.py disable   --plan plan.json --slot 1030
    python reminder.py enable    --plan plan.json --slot 1030
    python reminder.py schedule  --plan plan.json
    python reminder.py uninstall --name core
    python reminder.py render    --plan plan.json [--at HH:MM] [--force]
"""

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ASSET_DIR = SCRIPT_DIR.parent / "assets"
WINFORM = ASSET_DIR / "winform.ps1"
CLEANUP = ASSET_DIR / "cleanup.ps1"

STATE_FILE = "state.json"

# 弹窗返回码
ACT_OK = 0
ACT_PAUSE_TODAY = 2
ACT_DISABLE_SLOT = 3
ACT_OPEN_SCHEDULE = 4

# 匹配触发器时允许的时间偏差（分钟），防止调度延迟导致匹配不上
MATCH_TOLERANCE = 5

# 服务默认端口，与 service.py 保持一致
DEFAULT_PORT = 8768

WEEKDAY = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
DAY_MAP_WIN = {
	"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
	"FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
}
DAY_MAP_CRON = {"MON": "1", "TUE": "2", "WED": "3", "THU": "4", "FRI": "5", "SAT": "6", "SUN": "0"}
DAY_MAP_LAUNCHD = {"MON": 2, "TUE": 3, "WED": 4, "THU": 5, "FRI": 6, "SAT": 7, "SUN": 1}


# ---------------------------------------------------------------- 计划与状态

def load_plan(plan_path):
	"""读取提醒计划 JSON，展开动作引用，再做结构校验。

	计划用「动作库 + 时间点引用」组织：slots[].action 指向 actions 里的键，
	这样同一个动作出现在多个时间点时，文案只写一份。这里把引用展开成扁平结构，
	下游（弹窗、服务、页面）就不必各自处理引用。
	"""
	path = Path(plan_path).resolve()
	if not path.exists():
		raise SystemExit("找不到提醒计划文件: %s" % path)
	try:
		plan = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise SystemExit("提醒计划 JSON 解析失败: %s" % exc)

	slots = plan.get("slots")
	if not slots:
		raise SystemExit("提醒计划里没有任何 slots，至少要有一个时间点")

	actions = plan.get("actions") or {}
	for slot in slots:
		ref = slot.get("action")
		if not ref:
			continue
		base = actions.get(ref)
		if base is None:
			raise SystemExit("slot %s 引用了不存在的动作: %s" % (slot.get("id", "?"), ref))
		# 时间点自身的字段优先，未定义的从动作库补
		for key in ("name", "muscles", "steps", "trap", "color"):
			if not slot.get(key) and base.get(key):
				slot[key] = base[key]
		# dose 分两级：slot.dose 是时间轴上的短剂量，动作库的是完整剂量
		if base.get("dose"):
			slot["doseFull"] = base["dose"]

	for slot in slots:
		for key in ("id", "time", "name", "steps"):
			if key not in slot:
				raise SystemExit("slot %s 缺少必填字段: %s（若用了 action 引用，请检查动作库里是否有 %s）"
					% (slot.get("id", "?"), key, slot.get("action", "-")))
	return plan, path


def state_path(plan_path):
	"""state.json 固定与 plan.json 同目录，便于整套方案整体搬迁。"""
	return Path(plan_path).resolve().with_name(STATE_FILE)


def load_state(plan_path):
	"""读取运行状态；文件不存在或损坏时回落到「全部启用」。"""
	path = state_path(plan_path)
	default = {"enabled": True, "pausedUntil": None, "disabledSlots": []}
	if not path.exists():
		return dict(default)
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError):
		return dict(default)
	for key, value in default.items():
		data.setdefault(key, value)
	return data


def save_state(plan_path, state):
	path = state_path(plan_path)
	path.write_text(json.dumps(state, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")
	return path


def find_slot(plan, slot_id):
	for slot in plan["slots"]:
		if slot["id"] == slot_id:
			return slot
	ids = " / ".join(s.get("id", "?") for s in plan["slots"])
	raise SystemExit("找不到 slot=%s，可用的是: %s" % (slot_id, ids))


def parse_minutes(text):
	"""把 HH:MM 转成一天内的分钟数，便于比较。"""
	try:
		hour, minute = str(text).split(":")
		hour, minute = int(hour), int(minute)
	except ValueError:
		raise SystemExit("时间格式应为 HH:MM，收到: %s" % text)
	if not (0 <= hour <= 23 and 0 <= minute <= 59):
		raise SystemExit("时间超出范围: %s" % text)
	return hour * 60 + minute


def match_slot_by_time(plan, at_text, tolerance=MATCH_TOLERANCE):
	"""按时刻匹配最接近的 slot —— 这是「一个任务多个触发器」的核心。"""
	target = parse_minutes(at_text)
	best, best_diff = None, None
	for slot in plan["slots"]:
		diff = abs(parse_minutes(slot["time"]) - target)
		if diff <= tolerance and (best_diff is None or diff < best_diff):
			best, best_diff = slot, diff
	if best is None:
		raise SystemExit("按 %s 找不到对应日程（容差 %d 分钟）" % (at_text, tolerance))
	return best


def plan_days(plan):
	days = plan.get("days") or ["MON", "TUE", "WED", "THU", "FRI"]
	out = []
	for day in days:
		key = str(day).strip().upper()[:3]
		if key not in WEEKDAY:
			raise SystemExit("无法识别的星期: %s" % day)
		if key not in out:
			out.append(key)
	return out


def check_active(plan, slot, state):
	"""判断这条提醒现在是否应该弹。返回 (是否弹, 原因)。"""
	if not state.get("enabled", True):
		return False, "提醒功能已在配置文件中关闭（state.json 的 enabled=false）"

	slot_id = slot.get("id")
	if slot_id in (state.get("disabledSlots") or []):
		return False, "这个环节已被设为「不再提醒」（state.json 的 disabledSlots 含 %s）" % slot_id

	until = state.get("pausedUntil")
	if until:
		try:
			deadline = datetime.fromisoformat(until)
		except (TypeError, ValueError):
			deadline = None
		if deadline and datetime.now() <= deadline:
			return False, "提醒已暂停到 %s" % deadline.strftime("%Y-%m-%d %H:%M")
	return True, ""


# ---------------------------------------------------------------- 通用工具

def python_for_schedule():
	"""调度器要用的解释器路径。Windows 上优先 pythonw.exe，避免每次投递闪黑框。"""
	exe = Path(sys.executable)
	candidate = exe.with_name("pythonw.exe")
	if os.name == "nt" and candidate.exists():
		return str(candidate)
	return str(exe)


def build_command(plan_path, schedule=""):
	"""构造调度器要执行的命令。注意：不传时间 —— 触发时由脚本读系统时间匹配日程。"""
	cmd = [
		python_for_schedule(),
		str(SCRIPT_DIR / "reminder.py"),
		"render",
		"--plan", str(plan_path),
	]
	if schedule:
		cmd += ["--schedule", str(schedule)]
	return cmd


def run(cmd, **kw):
	kw.setdefault("capture_output", True)
	kw.setdefault("text", True)
	kw.setdefault("encoding", "utf-8")
	kw.setdefault("errors", "replace")
	proc = subprocess.run(cmd, **kw)
	return {"code": proc.returncode, "out": (proc.stdout or ""), "err": (proc.stderr or "")}


def display_dose(slot):
	"""弹窗用完整剂量，时间轴用短剂量。"""
	return slot.get("doseFull") or slot.get("dose") or ""


def format_text(slot, ui):
	"""把一条提醒拼成纯文本，用于 macOS / Linux 与终端预览。"""
	lines = ["%s  %s" % (slot["time"], slot["name"])]
	dose = display_dose(slot)
	if dose:
		lines.append(dose)
	lines.append("")
	for index, step in enumerate(slot["steps"], start=1):
		lines.append("%s  %s" % (chr(9311 + index), step))
	if slot.get("trap"):
		lines.append("")
		lines.append("%s %s" % (ui.get("trap", "Caution:"), slot["trap"]))
	return "\n".join(lines)


def resolve_schedule(plan, state=None):
	"""决定「日程表」按钮该打开什么地址。

	服务化之后页面由服务实时渲染，所以首选 `scheduleUrl`（缺省按 port 推导）。
	只有在计划里既没有 scheduleUrl、port 也为 0 的极端情况下，才退回 `schedule`
	指向的本地文件 —— 那是给「不想跑服务」的用户留的后路。
	"""
	url = plan.get("scheduleUrl")
	if url:
		return url
	port = plan.get("port")
	if port:
		return "http://127.0.0.1:%d/" % port
	return plan.get("schedule") or ""


def open_schedule(plan, plan_path, state=None):
	"""打开日程表。服务地址走浏览器，本地文件走 shell 关联。"""
	target = resolve_schedule(plan, state)
	if not target:
		print("提醒计划里既没有 scheduleUrl 也没有 schedule，无法打开日程表")
		return False

	if target.startswith("http://") or target.startswith("https://"):
		if sys.platform.startswith("win"):
			os.startfile(target)  # noqa: S606  Windows 专用，走默认浏览器
		elif sys.platform == "darwin":
			run(["open", target])
		else:
			run(["xdg-open", target])
		print("已用默认浏览器打开: %s" % target)
		return True

	path = Path(target)
	if not path.exists():
		print("日程表文件不存在: %s" % path)
		return False
	if sys.platform.startswith("win"):
		os.startfile(str(path))  # noqa: S606
	elif sys.platform == "darwin":
		run(["open", str(path)])
	else:
		run(["xdg-open", str(path)])
	print("已打开日程表: %s" % path)
	return True


# ---------------------------------------------------------------- 投递层

def notify_windows(plan, slot, title, plan_path, slot_id, schedule):
	"""Windows: 调用自带的 WinForms 窗体模板，返回 (动作码, 错误信息)。"""
	if not WINFORM.exists():
		return None, "缺少 %s，回退到纯文本弹窗" % WINFORM
	ps_root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
	ps_exe = ps_root / "powershell.exe"
	if not ps_exe.exists():
		ps_exe = Path("powershell.exe")
	res = run([
		str(ps_exe),
		"-STA", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
		"-ExecutionPolicy", "Bypass",
		"-File", str(WINFORM),
		"-PlanPath", str(plan_path),
		"-Slot", slot_id,
		"-Title", title,
		"-SchedulePath", schedule or "",
		"-PythonExe", python_for_schedule(),
		"-ScriptPath", str(SCRIPT_DIR / "reminder.py"),
	])
	if res["code"] in (ACT_OK, ACT_PAUSE_TODAY, ACT_DISABLE_SLOT, ACT_OPEN_SCHEDULE):
		return res["code"], ""
	return None, res["err"] or res["out"]


def notify_macos(slot, ui, title):
	script = 'display alert "%s" message "%s" buttons {"%s"} default button 1' % (
		_esc(title), _esc(format_text(slot, ui)), ui.get("ok", "OK"))
	run(["osascript", "-e", 'activate application "SystemUIServer"'])
	res = run(["osascript", "-e", script])
	return res["code"] == 0, res["err"] or res["out"]


def notify_linux(slot, ui, title):
	body = format_text(slot, ui)
	head = "%s  %s" % (slot["time"], slot["name"])
	res = run(["zenity", "--info", "--no-wrap", "--width=620", "--title=" + head, "--text=" + body])
	if res["code"] == 0:
		return True, ""
	res = run(["notify-send", title, body])
	return res["code"] == 0, res["err"] or res["out"]


def _esc(text):
	return text.replace("\\", "\\\\").replace('"', '\\"')


def handle_action(action, plan, plan_path, slot):
	"""处理弹窗上用户点选的附加操作。"""
	state = load_state(plan_path)
	if action == ACT_PAUSE_TODAY:
		deadline = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
		state["pausedUntil"] = deadline.isoformat(timespec="seconds")
		save_state(plan_path, state)
		print("已暂停本日所有提醒（恢复到 %s）" % deadline.strftime("%Y-%m-%d %H:%M"))
	elif action == ACT_DISABLE_SLOT:
		slot_id = slot.get("id")
		disabled = list(state.get("disabledSlots") or [])
		if slot_id not in disabled:
			disabled.append(slot_id)
		state["disabledSlots"] = disabled
		save_state(plan_path, state)
		print("已关闭「%s」（%s）这个环节，其余提醒不受影响" % (slot.get("name", "?"), slot_id))
	elif action == ACT_OPEN_SCHEDULE:
		open_schedule(plan, plan_path)


def deliver(plan_path, slot=None, at_text=None, force=False, schedule="", silent=False):
	"""按平台投递一条提醒，并按弹窗返回码落盘状态。"""
	plan, path = load_plan(plan_path)
	if slot is None:
		slot = match_slot_by_time(plan, at_text or datetime.now().strftime("%H:%M"))

	ui = plan.get("ui", {})
	title = plan.get("title", "Reminder")
	slot_id = slot["id"]

	if not force:
		active, reason = check_active(plan, slot, load_state(path))
		if not active:
			if not silent:
				print("跳过 %s %s —— %s" % (slot["time"], slot["name"], reason))
			return True

	schedule = schedule or resolve_schedule(plan, load_state(path))
	system = sys.platform

	if system.startswith("win"):
		action, info = notify_windows(plan, slot, title, path, slot_id, schedule)
		if action is not None:
			handle_action(action, plan, path, slot)
			return True
		print("[warn] 原生弹窗投递失败，改用终端输出: %s" % info, file=sys.stderr)
	elif system == "darwin":
		ok, info = notify_macos(slot, ui, title)
		if not ok:
			print("[warn] 原生弹窗投递失败，改用终端输出: %s" % info, file=sys.stderr)
		else:
			return True
	else:
		ok, info = notify_linux(slot, ui, title)
		if not ok:
			print("[warn] 原生弹窗投递失败，改用终端输出: %s" % info, file=sys.stderr)
		else:
			return True

	print(format_text(slot, ui))
	return True


# ---------------------------------------------------------------- Windows 注册

PS_INSTALL = """
$ErrorActionPreference = "Stop"
$p = [System.IO.File]::ReadAllText("__PARAMS__", [System.Text.Encoding]::UTF8) | ConvertFrom-Json

# 一个任务，多个触发器 —— 每个 slot 时间一个触发器
$triggers = @()
foreach ($at in $p.times) {
	$triggers += New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek ([string[]]$p.days) -At $at
}

$action = New-ScheduledTaskAction -Execute $p.exe -Argument $p.argument
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName $p.task -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
$info = Get-ScheduledTask -TaskName $p.task
Write-Output ("OK  " + $p.task + "  triggers=" + @($info.Triggers).Count + "  state=" + $info.State.ToString())
"""

PS_INSTALL_LOGON = """
$ErrorActionPreference = "Stop"
$p = [System.IO.File]::ReadAllText("__PARAMS__", [System.Text.Encoding]::UTF8) | ConvertFrom-Json

# 单个触发器：登录时启动服务。提醒时间由服务自己的定时器负责，系统任务不参与排期。
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$action = New-ScheduledTaskAction -Execute $p.exe -Argument $p.argument
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# 服务是常驻进程，执行时限必须设为不限时，否则会被强杀
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $p.task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
$info = Get-ScheduledTask -TaskName $p.task
Write-Output ("OK  " + $p.task + "  trigger=AtLogOn  state=" + $info.State.ToString())
"""

PS_UNINSTALL = """
$tasks = Get-ScheduledTask -TaskName $args[0] -ErrorAction SilentlyContinue
if (-not $tasks) { Write-Output "NONE"; exit 0 }
Unregister-ScheduledTask -TaskName $tasks.TaskName -Confirm:$false
Write-Output ("REMOVED " + $tasks.TaskName)
"""

PS_STATUS = """
$t = Get-ScheduledTask -TaskName $args[0] -ErrorAction SilentlyContinue
if (-not $t) { Write-Output "NONE"; exit 0 }
$i = Get-ScheduledTaskInfo -TaskName $t.TaskName
$next = if ($i.NextRunTime) { (Get-Date $i.NextRunTime).ToString("yyyy-MM-dd HH:mm") } else { "-" }
$last = if ($i.LastRunTime) { (Get-Date $i.LastRunTime).ToString("MM-dd HH:mm") } else { "never" }
Write-Output ("task=" + $t.TaskName + "  state=" + $t.State.ToString() + "  triggers=" + @($t.Triggers).Count)
Write-Output ("last=" + $last + "  next=" + $next)
"""

PS_TOGGLE = """
$t = Get-ScheduledTask -TaskName $args[0] -ErrorAction SilentlyContinue
if (-not $t) { Write-Output "NONE"; exit 0 }
if ($args[1] -eq "pause") { Disable-ScheduledTask -TaskName $t.TaskName | Out-Null }
else { Enable-ScheduledTask -TaskName $t.TaskName | Out-Null }
$now = (Get-ScheduledTask -TaskName $t.TaskName).State.ToString()
Write-Output ($args[1].ToUpper() + "  " + $t.TaskName + "  -> " + $now)
"""


def _ps(script, *params):
	"""执行一段 PowerShell。脚本本体只用 ASCII，中文一律走 UTF-8 的参数文件。"""
	tmp = Path(os.environ.get("TEMP", ".")) / ("reminder_%d" % os.getpid())
	tmp.mkdir(exist_ok=True)
	ps_path = tmp / "run.ps1"
	ps_path.write_text(script, encoding="utf-8")
	cmd = ["powershell.exe", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_path)]
	cmd += [str(p) for p in params]
	return run(cmd)


def install_windows(plan, plan_path, name, schedule, dry_run, mode="service"):
	"""mode=service 注册「登录时启动服务」；mode=timer 注册多触发器（传统方式）。"""
	tmp = Path(os.environ.get("TEMP", ".")) / ("reminder_%d" % os.getpid())
	tmp.mkdir(exist_ok=True)
	param_file = tmp / "install_params.json"

	if mode == "timer":
		times = [slot["time"] for slot in plan["slots"]]
		days = [DAY_MAP_WIN[d] for d in plan_days(plan)]
		command = build_command(plan_path, schedule)
		params = {
			"task": name,
			"exe": command[0],
			"argument": " ".join('"%s"' % item if " " in item else item for item in command[1:]),
			"days": days,
			"times": times,
		}
		if dry_run:
			return [
				"DRY  mode=timer  task=%s" % name,
				"     %s %s" % (params["exe"], params["argument"]),
				"     days=%s" % ",".join(days),
				"     triggers(%d)=%s" % (len(times), ",".join(times)),
			]
		param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
		script = PS_INSTALL.replace("__PARAMS__", str(param_file).replace("\\", "\\\\"))
		res = _ps(script)
		detail = (res["out"] or res["err"]).strip()
		return [detail] if detail else ["FAIL code=%s" % res["code"]]

	# 服务模式：只注册一个登录触发器，提醒排期由服务自己管
	port = plan.get("port") or DEFAULT_PORT
	service_py = SCRIPT_DIR / "service.py"
	candidate = Path(sys.executable).with_name("pythonw.exe")
	exe = str(candidate) if candidate.exists() else sys.executable
	argument = '"{0}" run --plan "{1}" --port {2}'.format(service_py, plan_path, port)

	params = {"task": name, "exe": exe, "argument": argument}
	if dry_run:
		return [
			"DRY  mode=service  task=%s" % name,
			"     %s %s" % (exe, argument),
			"     trigger=AtLogOn（登录时启动服务，提醒时间由服务内置定时器负责）",
			"     服务地址 http://127.0.0.1:%d/" % port,
		]
	param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
	script = PS_INSTALL_LOGON.replace("__PARAMS__", str(param_file).replace("\\", "\\\\"))
	res = _ps(script)
	detail = (res["out"] or res["err"]).strip()
	return [detail] if detail else ["FAIL code=%s" % res["code"]]


# ---------------------------------------------------------------- macOS 注册

def _plist(label, command, days, times):
	root = ET.Element("plist", {"version": "1.0"})
	dict_el = ET.SubElement(root, "dict")

	def add_key(key):
		ET.SubElement(dict_el, "key").text = key

	add_key("Label")
	ET.SubElement(dict_el, "string").text = label

	add_key("ProgramArguments")
	args = ET.SubElement(dict_el, "array")
	for item in command:
		ET.SubElement(args, "string").text = item

	add_key("StartCalendarInterval")
	intervals = ET.SubElement(dict_el, "array")
	for day in days:
		for at in times:
			hour, minute = parse_minutes(at) // 60, parse_minutes(at) % 60
			d = ET.SubElement(intervals, "dict")
			ET.SubElement(d, "key").text = "Weekday"
			ET.SubElement(d, "integer").text = str(DAY_MAP_LAUNCHD[day])
			ET.SubElement(d, "key").text = "Hour"
			ET.SubElement(d, "integer").text = str(hour)
			ET.SubElement(d, "key").text = "Minute"
			ET.SubElement(d, "integer").text = str(minute)

	ET.indent(root, space="\t")
	return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def install_macos(plan, plan_path, name, schedule, dry_run):
	agent_dir = Path.home() / "Library" / "LaunchAgents"
	plist = agent_dir / ("%s.plist" % name)
	command = build_command(plan_path, schedule)
	if dry_run:
		return ["DRY  plist=%s  triggers=%d" % (plist, len(plan["slots"]))]
	agent_dir.mkdir(parents=True, exist_ok=True)
	plist.write_bytes(_plist(name, command, plan_days(plan), [s["time"] for s in plan["slots"]]))
	run(["launchctl", "unload", str(plist)])
	res = run(["launchctl", "load", str(plist)])
	return ["OK  %s  %s" % (name, res["err"].strip() or "loaded")]


# ---------------------------------------------------------------- Linux 注册

# 技能的历史名与现名。清理与识别逻辑按这两个名字一起匹配，这样 2026-10-15
# 改名（scheduled-desktop-reminder -> desk-coach）之前留下的残留也能被清掉。
# 改动这里时，记得同步 assets/cleanup.ps1 里的 $needles。
SKILL_IDS = ("desk-coach", "scheduled-desktop-reminder")

CRON_BEGIN = "# BEGIN %s:%%s" % SKILL_IDS[0]
CRON_END = "# END %s:%%s" % SKILL_IDS[0]


def install_linux(plan, plan_path, name, schedule, dry_run):
	cron_days = ",".join(sorted({DAY_MAP_CRON[d] for d in plan_days(plan)}))
	command = " ".join(_q(i) for i in build_command(plan_path, schedule))
	lines = [CRON_BEGIN % name]
	for slot in plan["slots"]:
		minutes = parse_minutes(slot["time"])
		lines.append("%d %d * * %s %s # %s" % (minutes % 60, minutes // 60, cron_days, command, slot["id"]))
	lines.append(CRON_END % name)

	if dry_run:
		return ["DRY  " + line for line in lines]

	current = run(["crontab", "-l"])
	old = current["out"] if current["code"] == 0 else ""
	new = _strip_block(old, name).rstrip("\n")
	new = (new + "\n" if new.strip() else "") + "\n".join(lines) + "\n"
	res = run(["crontab", "-"], input=new)
	if res["code"] != 0:
		return ["FAIL crontab: " + res["err"].strip()]
	return ["OK  %s  crontab triggers=%d" % (name, len(plan["slots"]))]


def _q(text):
	return "'%s'" % text if " " in text else text


def _cron_markers(name):
	"""返回该计划可能用过的所有 BEGIN/END 标记（含历史技能名产生的旧标记）。

	改名后如果只认新标记，升级用户 crontab 里的旧块就清不掉，会与新块并存、
	导致每条提醒弹两次。所以两种都认。
	"""
	begins, ends = set(), set()
	for skill_id in SKILL_IDS:
		begins.add("# BEGIN %s:%s" % (skill_id, name))
		ends.add("# END %s:%s" % (skill_id, name))
	return begins, ends


def _strip_block(crontab, name):
	begins, ends = _cron_markers(name)
	out, skip = [], False
	for line in crontab.splitlines():
		stripped = line.strip()
		if stripped in begins:
			skip = True
			continue
		if stripped in ends:
			skip = False
			continue
		if not skip:
			out.append(line)
	return "\n".join(out)


# ---------------------------------------------------------------- 命令入口

def cmd_preview(args):
	plan, path = load_plan(args.plan)
	ui = plan.get("ui", {})
	state = load_state(path)
	print("=" * 66)
	print("提醒计划: %s" % plan.get("title", "(未命名)"))
	print("生效星期: %s" % " ".join(plan_days(plan)))
	print("时间点数: %d" % len(plan["slots"]))
	print("日程表: %s" % (resolve_schedule(plan, state) or "(未配置)"))
	print("当前状态: enabled=%s  pausedUntil=%s  disabledSlots=%s"
		% (state.get("enabled"), state.get("pausedUntil"), state.get("disabledSlots")))
	print("=" * 66)
	for slot in plan["slots"]:
		active, reason = check_active(plan, slot, state)
		print()
		print("-" * 66)
		print(format_text(slot, ui))
		if not active:
			print()
			print("[当前不会弹] %s" % reason)

	if args.show:
		print()
		print("-" * 66)
		print("下面真实弹出一次，可以直接测试弹窗上的按钮：")
		slot = find_slot(plan, args.slot) if args.slot else plan["slots"][0]
		print("（弹的是 %s %s；点「暂停本日」或「不再提醒」会真的写进 state.json）"
			% (slot["time"], slot["name"]))
		deliver(path, slot=slot, force=True)
	return 0


def write_uninstall_entry(plan_dir):
	"""在计划目录里放一份自包含的卸载入口，返回生成的文件路径列表。

	为什么需要这个：技能目录将来可能被删掉（用户不想用了、或换设备），
	那时脚本也没了，系统里注册的调度项就没人清理 —— 每次开机登录都会尝试
	启动一个不存在的脚本而报错。所以在安装时就把清理能力**复制到用户自己的目录**，
	它不依赖技能是否还在。

	Windows 放两样：cleanup.ps1（安装时的快照）与 uninstall-reminder.bat（双击即用）。
	macOS / Linux 放一个自包含的 uninstall-reminder.sh。
	"""
	plan_dir = Path(plan_dir)
	written = []
	plan_dir.mkdir(parents=True, exist_ok=True)

	if sys.platform.startswith("win"):
		if CLEANUP.exists():
			target_ps1 = plan_dir / "cleanup.ps1"
			target_ps1.write_bytes(CLEANUP.read_bytes())
			written.append(target_ps1)

		# bat 必须 GBK 编码 + CRLF 行尾，否则 cmd 下中文乱码、多行语句被拆坏
		nl = "\r\n"
		lines = [
			"@echo off",
			"chcp 936 >nul",
			"title 卸载定时提醒服务",
			"",
			"echo 正在停止服务并删除随系统启动的计划任务……",
			"echo.",
			'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup.ps1"',
			"",
			"echo.",
			"echo 计划任务已清理，开机登录不会再启动本服务。",
			"echo 配置文件仍在当前目录，确认不再需要可手动删除整个目录。",
			"echo.",
			"pause",
		]
		target_bat = plan_dir / "uninstall-reminder.bat"
		target_bat.write_bytes((nl.join(lines) + nl).encode("gbk"))
		written.append(target_bat)
		return written

	target_sh = plan_dir / "uninstall-reminder.sh"
	target_sh.write_text(
		"""#!/bin/sh
# 卸载定时提醒：清理随登录启动的调度项。
# 自包含，不依赖技能目录是否还在。
set -u

NEEDLES="desk-coach scheduled-desktop-reminder"

# 两个名字都认（兼容 2026-10-15 改名前的残留）
# macOS: 删除指向本技能的 LaunchAgent
AGENTS="$HOME/Library/LaunchAgents"
if [ -d "$AGENTS" ]; then
	for f in "$AGENTS"/*.plist; do
		[ -f "$f" ] || continue
		if grep -q -e "desk-coach" -e "scheduled-desktop-reminder" "$f" 2>/dev/null; then
			launchctl unload "$f" 2>/dev/null
			rm -f "$f"
			echo "已移除 $f"
		fi
	done
fi

# Linux: 清掉 crontab 中指向本技能的条目
if command -v crontab >/dev/null 2>&1; then
	before=$(crontab -l 2>/dev/null)
	after=$(printf '%s\n' "$before" | grep -v -e "desk-coach" -e "scheduled-desktop-reminder")
	if [ "$before" != "$after" ]; then
		printf '%s\n' "$after" | crontab -
		echo "已清理 crontab 中的相关条目"
	fi
fi

# 结束仍在运行的服务进程
for n in $NEEDLES; do
	pkill -f "$n" 2>/dev/null && echo "已结束相关进程（$n）"
done

echo "清理完成。"
""",
		encoding="utf-8")
	target_sh.chmod(0o755)
	written.append(target_sh)
	return written


def cmd_install(args):
	plan, path = load_plan(args.plan)
	schedule = args.schedule or plan.get("schedule") or ""
	system = sys.platform
	if system.startswith("win"):
		log = install_windows(plan, path, args.name, schedule, args.dry_run, args.mode)
	elif system == "darwin":
		log = install_macos(plan, path, args.name, schedule, args.dry_run)
	else:
		log = install_linux(plan, path, args.name, schedule, args.dry_run)
	for line in log:
		print(line)

	if not args.dry_run:
		state = load_state(path)
		state["taskName"] = args.name
		# 不把日程表路径写进 state：页面已由服务实时渲染，
		# 存下来只会变成「指向已删文件」的失效路径。见 plan-schema 坑第 23 条。
		save_state(path, state)
		print("配置状态文件: %s" % state_path(path))

		# 把卸载能力复制到用户自己的目录，使技能目录被删除后仍能清理系统残留
		written = write_uninstall_entry(path.parent)
		for item in written:
			print("卸载入口: %s" % item.name)

		if args.mode == "service":
			print("下一步: python scripts/service.py start --plan \"%s\"" % path)
	return 0


def cmd_uninstall(args):
	name = args.name or _state_name(args.plan)
	system = sys.platform
	if system.startswith("win"):
		res = _ps(PS_UNINSTALL, name)
		print((res["out"] or res["err"]).strip() or "已无残留")
	elif system == "darwin":
		plist = Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % name)
		if not plist.exists():
			print("已无残留")
		else:
			run(["launchctl", "unload", str(plist)])
			plist.unlink()
			print("REMOVED %s" % plist.name)
	else:
		current = run(["crontab", "-l"])
		if current["code"] != 0:
			print("已无残留")
			return 0
		res = run(["crontab", "-"], input=_strip_block(current["out"], name) + "\n")
		print("已移除 cron 块" if res["code"] == 0 else res["err"])
	return 0


def _state_name(plan_path):
	state = load_state(plan_path)
	name = state.get("taskName")
	if not name:
		raise SystemExit("没有 --name，且 state.json 里也没记录任务名；请显式指定 --name")
	return name


def cmd_status(args):
	plan, path = load_plan(args.plan)
	state = load_state(path)
	name = args.name or state.get("taskName") or "(未记录)"

	print("提醒计划: %s" % path)
	print("系统任务: %s" % name)
	print("-- 配置状态 (%s) --" % state_path(path).name)
	print("enabled       = %s" % state.get("enabled"))
	print("pausedUntil   = %s" % state.get("pausedUntil"))
	print("disabledSlots = %s" % state.get("disabledSlots"))
	print("日程表        = %s" % (resolve_schedule(plan, state) or "(未配置)"))
	print("-- 系统任务 --")

	system = sys.platform
	if system.startswith("win"):
		res = _ps(PS_STATUS, name)
		print((res["out"] or res["err"]).strip() or "NONE")
	elif system == "darwin":
		plist = Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % name)
		print("plist=%s  %s" % (plist, "存在" if plist.exists() else "不存在"))
	else:
		current = run(["crontab", "-l"])
		begins, ends = _cron_markers(name)
		inside = False
		for line in (current["out"] if current["code"] == 0 else "").splitlines():
			stripped = line.strip()
			if stripped in begins:
				inside = True
				continue
			if stripped in ends:
				inside = False
				continue
			if inside:
				print(line)

	print("-- 各时间点 --")
	for slot in plan["slots"]:
		active, reason = check_active(plan, slot, state)
		print("%s  %-14s %s%s" % (slot["time"], slot["name"], "会弹" if active else "不弹", "" if active else "  (" + reason + ")"))
	return 0


def cmd_pause(args):
	plan, path = load_plan(args.plan)
	if args.date:
		try:
			day = datetime.strptime(args.date, "%Y-%m-%d")
		except ValueError:
			raise SystemExit("日期格式应为 YYYY-MM-DD，收到: %s" % args.date)
	else:
		day = datetime.now()
	deadline = day.replace(hour=23, minute=59, second=59, microsecond=0)

	state = load_state(path)
	state["pausedUntil"] = deadline.isoformat(timespec="seconds")
	save_state(path, state)
	print("已暂停提醒到 %s" % deadline.strftime("%Y-%m-%d %H:%M"))
	print("（各时间点的排期没有改动，恢复即照常生效）")
	return 0


def cmd_resume(args):
	plan, path = load_plan(args.plan)
	state = load_state(path)
	state["pausedUntil"] = None
	save_state(path, state)
	print("已恢复提醒")
	if not state.get("enabled", True):
		print("注意：enabled=false，总开关仍是关闭的，需要把它改成 true 才会弹")
	disabled = state.get("disabledSlots") or []
	if disabled:
		print("注意：以下环节被设为「不再提醒」，不会弹: %s" % ", ".join(disabled))
	return 0


def cmd_disable(args):
	plan, path = load_plan(args.plan)
	slot = find_slot(plan, args.slot)
	state = load_state(path)
	disabled = list(state.get("disabledSlots") or [])
	if slot["id"] not in disabled:
		disabled.append(slot["id"])
	state["disabledSlots"] = disabled
	save_state(path, state)
	print("已关闭「%s」（%s）—— 这个环节以后不再弹，其余不变" % (slot["name"], slot["id"]))
	return 0


def cmd_enable(args):
	plan, path = load_plan(args.plan)
	slot = find_slot(plan, args.slot)
	state = load_state(path)
	disabled = [s for s in (state.get("disabledSlots") or []) if s != slot["id"]]
	state["disabledSlots"] = disabled
	save_state(path, state)
	print("已重新开启「%s」（%s）" % (slot["name"], slot["id"]))
	return 0


def cmd_schedule(args):
	plan, path = load_plan(args.plan)
	open_schedule(plan, path, load_state(path))
	return 0


def cmd_render(args):
	slot = None
	if args.slot:
		plan, _ = load_plan(args.plan)
		slot = find_slot(plan, args.slot)
	deliver(
		args.plan,
		slot=slot,
		at_text=args.at,
		force=args.force,
		schedule=args.schedule or "",
	)
	return 0


def main():
	parser = argparse.ArgumentParser(description="定时桌面提醒器（一个任务 + 多个触发器）")
	sub = parser.add_subparsers(dest="cmd", required=True)

	p = sub.add_parser("preview", help="打印全部提醒文案")
	p.add_argument("--plan", required=True)
	p.add_argument("--slot", default=None, help="配合 --show 指定要实测的时间点 id")
	p.add_argument("--show", action="store_true", help="真实弹出一次窗体")
	p.set_defaults(func=cmd_preview)

	p = sub.add_parser("install", help="注册自启动（默认：登录时启动服务）")
	p.add_argument("--plan", required=True)
	p.add_argument("--name", default="Reminder")
	p.add_argument("--schedule", default="", help="日程表 HTML 路径，供弹窗上的「日程表」按钮打开")
	p.add_argument("--mode", default="service", choices=["service", "timer"],
		help="service=登录时启动服务（推荐，排期由服务负责）；timer=传统多触发器方式")
	p.add_argument("--dry-run", action="store_true")
	p.set_defaults(func=cmd_install)

	p = sub.add_parser("uninstall", help="注销任务")
	p.add_argument("--plan", default=None)
	p.add_argument("--name", default=None)
	p.set_defaults(func=cmd_uninstall)

	p = sub.add_parser("status", help="查看配置状态与任务情况")
	p.add_argument("--plan", required=True)
	p.add_argument("--name", default=None)
	p.set_defaults(func=cmd_status)

	p = sub.add_parser("pause", help="暂停提醒（默认到今日结束）")
	p.add_argument("--plan", required=True)
	p.add_argument("--date", default=None, help="暂停到该日结束，格式 YYYY-MM-DD")
	p.set_defaults(func=cmd_pause)

	p = sub.add_parser("resume", help="恢复提醒")
	p.add_argument("--plan", required=True)
	p.set_defaults(func=cmd_resume)

	p = sub.add_parser("disable", help="永久关闭某个环节")
	p.add_argument("--plan", required=True)
	p.add_argument("--slot", required=True)
	p.set_defaults(func=cmd_disable)

	p = sub.add_parser("enable", help="重新开启某个环节")
	p.add_argument("--plan", required=True)
	p.add_argument("--slot", required=True)
	p.set_defaults(func=cmd_enable)

	p = sub.add_parser("schedule", help="打开日程表")
	p.add_argument("--plan", required=True)
	p.set_defaults(func=cmd_schedule)

	p = sub.add_parser("render", help="投递单条提醒（调度器调用，按当前时间匹配日程）")
	p.add_argument("--plan", required=True)
	p.add_argument("--slot", default=None, help="直接指定时间点 id，优先级高于 --at")
	p.add_argument("--at", default=None, help="指定时刻 HH:MM，缺省用当前系统时间")
	p.add_argument("--schedule", default="")
	p.add_argument("--force", action="store_true", help="忽略开关状态强制弹出")
	p.set_defaults(func=cmd_render)

	args = parser.parse_args()
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())
