#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日程提醒服务 —— 常驻进程，自己管定时、自己管开关、自己管日程表页面。

为什么要有这一层:
    把「定时」从操作系统的调度器搬进自己的进程，好处是三件事合成一件 ——
    启动服务 = 提醒功能生效，关闭服务 = 提醒功能整体停用，不用再去动系统任务；
    日程表页面的开关按钮也能直接调服务接口，而不是去点一个 .bat 文件。

与 reminder.py 的分工:
    reminder.py  —— 引擎。读 plan/state、渲染弹窗、匹配日程。既可单独命令行使用，也被本服务调用。
    service.py   —— 常驻壳。HTTP 接口 + 内置定时线程 + 进程生命周期管理。

服务自己负责的事:
    - 定时：内部线程每 15 秒轮询，命中 slot 时刻即弹窗（不再依赖系统调度器的触发器）
    - 开关：暂停 / 恢复 / 永不再提醒，全部写 state.json
    - 页面：把日程表 HTML 挂在 / 上，开关按钮直接 POST 到本服务的接口

启动与停止:
    python service.py start   --plan plan.json [--port 8768]
    python service.py stop    --plan plan.json
    python service.py status  --plan plan.json
    python service.py run     --plan plan.json          # 前台运行（start 实际拉起的也是它）

API:
    GET  /                      日程表页面
    GET  /api/status            服务与提醒状态
    POST /api/pause             暂停本日全部提醒
    POST /api/resume            恢复提醒
    POST /api/disable?id=1030   永久关闭某个环节
    POST /api/enable?id=1030    重新开启某个环节
    POST /api/fire?id=1030      立即弹一次（测试用）
    POST /api/reload            重载 plan 与 state（改完文案不必重启服务）
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import reminder  # noqa: E402  同目录的引擎

DEFAULT_PORT = 8768
POLL_SECONDS = 15
SERVICE_FILE = "service.json"
LOG_FILE = "service.log"
FIRED_FILE = "fired.json"

# 页面模板。它放在技能包里，是唯一一份；每个用户的页面都由服务用这份模板
# 加上自己的 plan 数据渲染出来，因此不存在「页面内容要手工跟着 plan 改」的问题。
TEMPLATE = reminder.ASSET_DIR / "schedule-template.html"

# 清理脚本：扫描系统里所有指向本技能的计划任务并删除。
# 它按「任务动作里的脚本路径」识别，不依赖 state.json，所以技能目录被删掉之后依然有效。
CLEANUP = reminder.ASSET_DIR / "cleanup.ps1"
INJECT_TOKEN = "window.__PLAN__ = null;"

# 弹窗重叠策略：ignore = 上一个没关就跳过（默认）；stack = 允许叠多个
OVERLAP_POLICY = "ignore"


# ---------------------------------------------------------------- 路径与小工具

def service_path(plan_path):
	return Path(plan_path).resolve().with_name(SERVICE_FILE)


def log_path(plan_path):
	return Path(plan_path).resolve().with_name(LOG_FILE)


def fired_path(plan_path):
	return Path(plan_path).resolve().with_name(FIRED_FILE)


def log(plan_path, message):
	"""追加一行日志。服务是后台进程，出问题只能靠这个文件排查。"""
	stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	line = "[%s] %s\n" % (stamp, message)
	try:
		with open(log_path(plan_path), "a", encoding="utf-8") as handle:
			handle.write(line)
	except OSError:
		pass


def load_fired(plan_path):
	"""已触发记录，按日期分桶，防止服务重启后同一时刻重复弹。"""
	path = fired_path(plan_path)
	if not path.exists():
		return {}
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError):
		return {}


def save_fired(plan_path, data):
	path = fired_path(plan_path)
	# 只留最近 3 天，避免文件无限增长
	today = datetime.now().date()
	keep = {}
	for day, ids in data.items():
		try:
			if (today - datetime.strptime(day, "%Y-%m-%d").date()).days <= 3:
				keep[day] = ids
		except ValueError:
			continue
	try:
		path.write_text(json.dumps(keep, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")
	except OSError:
		pass


# ---------------------------------------------------------------- 定时线程

class Scheduler(threading.Thread):
	"""内置定时器：每 15 秒轮询一次，命中 slot 时刻就弹窗。"""

	def __init__(self, plan_path, port):
		super().__init__(daemon=True)
		self.plan_path = Path(plan_path).resolve()
		self.port = port
		self.stop_flag = threading.Event()
		self.procs = []
		self.last_error = ""

	def run(self):
		log(self.plan_path, "定时线程启动，端口 %d" % self.port)
		while not self.stop_flag.is_set():
			try:
				self.tick()
			except Exception as exc:  # 任何异常都不能让线程死掉
				self.last_error = str(exc)
				log(self.plan_path, "轮询异常: %s" % exc)
			self.stop_flag.wait(POLL_SECONDS)
		log(self.plan_path, "定时线程退出")

	def tick(self):
		"""检查当前时刻是否命中某个 slot。"""
		plan, path = reminder.load_plan(self.plan_path)
		state = reminder.load_state(path)

		# 清理已结束的弹窗进程
		self.procs = [p for p in self.procs if p.poll() is None]

		now = datetime.now()
		today = now.strftime("%Y-%m-%d")
		current = now.hour * 60 + now.minute

		# 非生效星期直接跳过
		weekday = reminder.WEEKDAY[now.weekday()]
		if weekday not in reminder.plan_days(plan):
			return

		fired = load_fired(path)
		today_fired = fired.setdefault(today, [])
		changed = False

		for slot in plan["slots"]:
			slot_id = slot["id"]
			if slot_id in today_fired:
				continue
			# manual 的环节不参与自动调度，只能由用户在日程表上手动触发
			if slot.get("manual"):
				continue
			target = reminder.parse_minutes(slot["time"])
			# 只看「刚过去或正到」的时刻，容差内视为命中
			if not (0 <= current - target <= reminder.MATCH_TOLERANCE):
				continue

			active, reason = reminder.check_active(plan, slot, state)
			today_fired.append(slot_id)
			changed = True
			if not active:
				log(path, "跳过 %s %s —— %s" % (slot["time"], slot["name"], reason))
				continue

			if self.procs and OVERLAP_POLICY == "ignore":
				log(path, "跳过 %s %s —— 上一个提醒窗口尚未关闭" % (slot["time"], slot["name"]))
				continue

			self.spawn(slot, path)

		if changed:
			save_fired(path, fired)

	def spawn(self, slot, plan_path):
		"""拉起一次弹窗。用子进程，避免阻塞轮询。"""
		candidate = Path(sys.executable).with_name("pythonw.exe")
		exe = str(candidate) if (os.name == "nt" and candidate.exists()) else sys.executable
		cmd = [exe, str(SCRIPT_DIR / "reminder.py"), "render", "--plan", str(plan_path), "--slot", slot["id"]]
		try:
			proc = subprocess.Popen(cmd, close_fds=True)
			self.procs.append(proc)
			log(plan_path, "已弹出 %s %s (pid=%d)" % (slot["time"], slot["name"], proc.pid))
		except OSError as exc:
			log(plan_path, "弹出失败: %s" % exc)

	def summary(self):
		return {
			"pollSeconds": POLL_SECONDS,
			"overlap": OVERLAP_POLICY,
			"openWindows": len([p for p in self.procs if p.poll() is None]),
			"lastError": self.last_error,
		}


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
	server_version = "ScheduleService/1.0"
	plan_path = None
	port = DEFAULT_PORT
	scheduler = None

	# 不记录常规成功请求：页面每 30 秒轮询一次 /api/status，全记下来会把
	# 真正有用的记录（弹出、跳过开关拦截）淹掉。只留异常与状态变更类请求。
	QUIET_PATHS = ("/api/status", "/api/ping", "/favicon.ico", "/")

	def log_message(self, fmt, *args):
		"""默认会往 stderr 打印；服务是后台进程，改成写文件，并过滤例行请求。

		入参形如 "%s %s %s" 配 ("GET /api/status HTTP/1.1", "200", "-")。
		"""
		if self._is_routine():
			return
		log(self.plan_path, "HTTP " + (fmt % args))

	def _is_routine(self):
		"""判断当前请求是否属于「无需记录」的例行访问。"""
		path = urlparse(self.path).path.rstrip("/") or "/"
		if path not in self.QUIET_PATHS:
			return False
		# 只有成功的例行请求才静音；4xx/5xx 仍要留痕，否则出问题查不到
		return getattr(self, "_response_code", 200) < 400

	def _send(self, code, body, content_type):
		# 记下响应码，供 _is_routine() 判断该不该写日志
		self._response_code = code
		if isinstance(body, str):
			body = body.encode("utf-8")
		self.send_response(code)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		# 页面可能以 file:// 打开，此时是非同源请求，必须放开 CORS
		self.send_header("Access-Control-Allow-Origin", "*")
		self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(body)

	def _json(self, data, code=200):
		self._send(code, json.dumps(data, ensure_ascii=False, indent="\t"), "application/json; charset=utf-8")

	def _html(self, text, code=200):
		self._send(code, text, "text/html; charset=utf-8")

	def do_OPTIONS(self):
		self._send(204, b"", "text/plain")

	def do_GET(self):
		parsed = urlparse(self.path)
		route = parsed.path.rstrip("/") or "/"

		if route == "/":
			return self.serve_schedule()
		if route == "/api/plan":
			return self.serve_plan()
		if route == "/api/status":
			return self._json(self.build_status())
		if route == "/api/ping":
			return self._json({"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
		if route == "/favicon.ico":
			# 浏览器每次打开页面都会请求它。直接回空内容，
			# 免得日志里积满无意义的 404（见 plan-schema 坑第 25 条）。
			return self._send(204, b"", "image/x-icon")
		return self._json({"ok": False, "error": "未知路径: %s" % route}, 404)

	def do_POST(self):
		parsed = urlparse(self.path)
		route = parsed.path.rstrip("/")
		query = parse_qs(parsed.query)
		slot_id = (query.get("id") or [None])[0]

		if route == "/api/pause":
			return self._json(self.action_pause())
		if route == "/api/resume":
			return self._json(self.action_resume())
		if route == "/api/disable":
			return self._json(self.action_toggle_slot(slot_id, True))
		if route == "/api/enable":
			return self._json(self.action_toggle_slot(slot_id, False))
		if route == "/api/fire":
			return self._json(self.action_fire(slot_id))
		if route == "/api/reload":
			return self._json({"ok": True, "message": "已重载（plan 与 state 每次操作都会重新读取）"})
		return self._json({"ok": False, "error": "未知路径: %s" % route}, 404)

	# -- 业务动作 --------------------------------------------------

	def serve_schedule(self):
		"""把日程表渲染出来。

		页面唯一来源是技能包里的模板；内容来自 plan.json，每次请求都重新读取，
		所以改完 plan 刷新页面即见，不需要重启服务。
		"""
		try:
			return self._html(render_page(self.plan_path))
		except SystemExit as exc:
			return self._html("<h1>日程表渲染失败</h1><p>%s</p>" % exc, 500)
		except OSError as exc:
			return self._html("<h1>模板读取失败</h1><p>%s</p>" % exc, 500)

	def serve_plan(self):
		"""返回展开后的 plan，供以 file:// 打开的页面回落使用。"""
		plan, path = reminder.load_plan(self.plan_path)
		return self._json({"ok": True, "plan": plan})

	def build_status(self):
		plan, path = reminder.load_plan(self.plan_path)
		state = reminder.load_state(path)
		disabled = state.get("disabledSlots") or []
		slots = []
		for slot in plan["slots"]:
			active, _ = reminder.check_active(plan, slot, state)
			slots.append({
				"id": slot["id"],
				"time": slot["time"],
				"name": slot["name"],
				"dose": slot.get("dose", ""),
				# manual=True 表示不自动弹，只能在页面上手动触发
				"manual": bool(slot.get("manual")),
				"off": slot["id"] in disabled,
				"active": active,
			})
		return {
			"ok": True,
			"service": "running",
			"port": self.port,
			"plan": str(path),
			"title": plan.get("title", ""),
			"enabled": state.get("enabled", True),
			"pausedUntil": state.get("pausedUntil"),
			"disabledSlots": state.get("disabledSlots") or [],
			"days": reminder.plan_days(plan),
			"slots": slots,
			"scheduler": self.scheduler.summary() if self.scheduler else {},
			"serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
		}

	def action_pause(self):
		plan, path = reminder.load_plan(self.plan_path)
		state = reminder.load_state(path)
		deadline = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
		state["pausedUntil"] = deadline.isoformat(timespec="seconds")
		reminder.save_state(path, state)
		log(path, "通过接口暂停提醒到 %s" % deadline.strftime("%Y-%m-%d %H:%M"))
		return {"ok": True, "message": "已暂停本日所有提醒", "pausedUntil": state["pausedUntil"]}

	def action_resume(self):
		plan, path = reminder.load_plan(self.plan_path)
		state = reminder.load_state(path)
		state["pausedUntil"] = None
		reminder.save_state(path, state)
		log(path, "通过接口恢复提醒")
		return {"ok": True, "message": "已恢复提醒", "pausedUntil": None}

	def action_toggle_slot(self, slot_id, disable):
		plan, path = reminder.load_plan(self.plan_path)
		if not slot_id:
			return {"ok": False, "error": "缺少参数 id"}
		try:
			slot = reminder.find_slot(plan, slot_id)
		except SystemExit as exc:
			return {"ok": False, "error": str(exc)}
		state = reminder.load_state(path)
		disabled = list(state.get("disabledSlots") or [])
		if disable:
			if slot_id not in disabled:
				disabled.append(slot_id)
		else:
			disabled = [s for s in disabled if s != slot_id]
		state["disabledSlots"] = disabled
		reminder.save_state(path, state)
		verb = "关闭" if disable else "开启"
		log(path, "通过接口%s环节 %s" % (verb, slot_id))
		return {"ok": True, "message": "已%s「%s」" % (verb, slot["name"]), "disabledSlots": disabled}

	def action_fire(self, slot_id):
		"""手动触发一条提醒（用户在日程表上点击某个时间点）。

		与自动调度的区别：暂停中（pausedUntil）不拦截 —— 用户是主动点开的，说明当下就想练；
		但被永久关闭（disabledSlots）的环节会拒绝，避免"我明明关掉了它怎么又弹"。
		"""
		plan, path = reminder.load_plan(self.plan_path)
		state = reminder.load_state(path)
		if not slot_id:
			return {"ok": False, "error": "缺少参数 id"}
		try:
			slot = reminder.find_slot(plan, slot_id)
		except SystemExit as exc:
			return {"ok": False, "error": str(exc)}

		if slot_id in (state.get("disabledSlots") or []):
			return {"ok": False, "error": "「%s」已关闭，请先在页面上开启它" % slot["name"]}

		threading.Thread(
			target=reminder.deliver,
			kwargs={"plan_path": path, "slot": slot, "force": True, "silent": True},
			daemon=True,
		).start()
		log(path, "手动触发 %s %s" % (slot["time"], slot["name"]))
		return {"ok": True, "message": "已弹出 %s %s" % (slot["time"], slot["name"])}


# ---------------------------------------------------------------- 页面渲染

def render_page(plan_path):
	"""读取技能模板，注入 plan 数据，返回最终 HTML。

	注入而非让页面自己取数，是为了让页面在离线或服务刚启动时也能立刻显示完整内容；
	底部的开关区仍走 /api/status，那部分需要服务在跑才有意义。
	"""
	if not TEMPLATE.exists():
		raise SystemExit("找不到页面模板: %s" % TEMPLATE)

	plan, _ = reminder.load_plan(plan_path)
	html = TEMPLATE.read_text(encoding="utf-8")

	title = plan.get("title") or "日程表"
	html = html.replace("__TITLE__", title, 1)

	payload = json.dumps(plan, ensure_ascii=False)
	# 防止数据里的 </script> 提前闭合脚本块
	payload = payload.replace("</", "<\\/")
	if INJECT_TOKEN not in html:
		raise SystemExit("模板缺少注入占位 %s" % INJECT_TOKEN)
	html = html.replace(INJECT_TOKEN, "window.__PLAN__ = %s;" % payload, 1)
	return html


# ---------------------------------------------------------------- 服务启停

def is_alive(pid):
	"""判断进程是否还在。"""
	if not pid:
		return False
	try:
		if os.name == "nt":
			out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
				capture_output=True, text=True, encoding="utf-8", errors="replace").stdout or ""
			return str(pid) in out
		os.kill(pid, 0)
		return True
	except (OSError, ValueError, ProcessLookupError):
		return False


def read_service(plan_path):
	path = service_path(plan_path)
	if not path.exists():
		return {}
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError):
		return {}


def task_name(plan_path):
	"""取注册时记录的计划任务名，没有就用默认值。"""
	state = reminder.load_state(plan_path)
	return state.get("taskName") or "Reminder"


class TaskResult:
	"""schtasks 的执行结果。找不到工具或被安全策略拦下时也返回一个对象，便于统一降级。"""

	def __init__(self, returncode, stdout="", stderr=""):
		self.returncode = returncode
		self.stdout = stdout
		self.stderr = stderr


def run_schtasks(*args):
	"""调用 schtasks。受限环境下可能被拦，此时返回失败码让调用方走降级分支。"""
	system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
	exe = system32 / "schtasks.exe"
	if not exe.exists():
		exe = Path("schtasks.exe")
	try:
		proc = subprocess.run([str(exe)] + list(args), capture_output=True, text=True,
			encoding="utf-8", errors="replace")
		return TaskResult(proc.returncode, proc.stdout or "", proc.stderr or "")
	except (OSError, PermissionError) as exc:
		return TaskResult(1, "", str(exc))


def run_ps(script):
	"""执行一段 PowerShell。某些环境会拦截 schtasks.exe，但 PowerShell cmdlet 仍可用。"""
	system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
	exe = system32 / "powershell.exe"
	if not exe.exists():
		exe = Path("powershell.exe")
	try:
		proc = subprocess.run([str(exe), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
			"-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
		return TaskResult(proc.returncode, proc.stdout or "", proc.stderr or "")
	except (OSError, PermissionError) as exc:
		return TaskResult(1, "", str(exc))


def start_via_task(name, path):
	"""用计划任务拉起服务。先试 schtasks，被拦则改用 PowerShell cmdlet。

	交给任务计划程序托管的好处：进程由系统服务拉起，与任何终端/作业对象脱钩，
	不会随父进程退出而被回收。
	"""
	# 先清掉可能存在的旧实例，否则任务会卡在"正在运行"而拒绝再次启动
	run_schtasks("/end", "/tn", name)
	run_ps('Start-Sleep -Milliseconds 300; try { Stop-ScheduledTask -TaskName "%s" -ErrorAction SilentlyContinue } catch {}' % name)

	res = run_schtasks("/run", "/tn", name)
	if res.returncode == 0:
		return True, "schtasks"

	res = run_ps('Start-ScheduledTask -TaskName "%s"; if ($?) { "OK" }' % name)
	if res.returncode == 0 and "OK" in (res.stdout or ""):
		return True, "powershell"

	log(path, "两种任务启动方式都失败：schtasks=%s | powershell=%s"
		% (res.stderr.strip() or res.returncode, (res.stdout or "").strip()))
	return False, ""


def cmd_start(args):
	plan, path = reminder.load_plan(args.plan)
	info = read_service(path)

	# 已在运行就直接返回（以端口为准，见 probe_service）
	already, running_info, running_pid = probe_service(path)
	if already:
		print("服务已在运行 (pid=%s)" % running_pid)
		print("日程表: http://127.0.0.1:%s/" % running_info.get("port"))
		return 0

	port = args.port or info.get("port") or plan.get("port") or DEFAULT_PORT
	started = False

	if os.name == "nt" and not args.no_task:
		name = task_name(path)
		if run_schtasks("/query", "/tn", name).returncode == 0:
			started, how = start_via_task(name, path)
			if started:
				log(path, "已通过计划任务 %s 启动服务（方式：%s）" % (name, how))

	if not started:
		candidate = Path(sys.executable).with_name("pythonw.exe")
		exe = str(candidate) if (os.name == "nt" and candidate.exists()) else sys.executable
		cmd = [exe, str(SCRIPT_DIR / "service.py"), "run", "--plan", str(path), "--port", str(port)]
		flags = 0
		if os.name == "nt":
			flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
			flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
		subprocess.Popen(cmd, close_fds=True, creationflags=flags,
			cwd=str(path.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		log(path, "计划任务不可用，已直接 spawn 服务进程")

	# 等服务就绪，避免 start 返回了却连不上
	deadline = time.time() + 15
	while time.time() < deadline:
		time.sleep(0.5)
		if port_open(port):
			break
	else:
		print("启动超时，请查看日志: %s" % log_path(path))
		return 1

	pid = find_service_pid(port)
	service_path(path).write_text(
		json.dumps({"pid": pid, "port": port, "startedAt": datetime.now().isoformat(timespec="seconds")},
			ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")
	log(path, "服务就绪 pid=%s port=%d" % (pid, port))

	print("服务已启动 (pid=%s)" % pid)
	print("日程表: http://127.0.0.1:%d/" % port)
	print("状态:   http://127.0.0.1:%d/api/status" % port)
	return 0


def find_service_pid(port):
	"""从监听端口反查进程号，比依赖 Popen 句柄更可靠（进程可能是计划任务拉起的）。"""
	if os.name == "nt":
		out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
			encoding="utf-8", errors="replace").stdout or ""
		for line in out.splitlines():
			if (":%d " % port) in line and "LISTENING" in line:
				parts = line.split()
				if parts and parts[-1].isdigit():
					return int(parts[-1])
	return None


def port_open(port):
	import socket
	with socket.socket() as sock:
		sock.settimeout(0.4)
		return sock.connect_ex(("127.0.0.1", port)) == 0


def stop_service(path):
	"""停止服务进程，返回是否本来就在运行。供 stop 与 uninstall 复用。"""
	running, info, pid = probe_service(path)
	if not running:
		service_path(path).unlink(missing_ok=True)
		return False
	if os.name == "nt":
		subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
		# 计划任务若处于"正在运行"状态，一并结束，否则下次启不动
		name = task_name(path)
		if run_schtasks("/query", "/tn", name).returncode == 0:
			run_schtasks("/end", "/tn", name)
	else:
		os.kill(pid, 15)
	for _ in range(20):
		if not is_alive(pid):
			break
		time.sleep(0.25)
	service_path(path).unlink(missing_ok=True)
	log(path, "服务已停止 pid=%s" % pid)
	return True


def cmd_stop(args):
	plan, path = reminder.load_plan(args.plan)
	if stop_service(path):
		print("服务已停止（提醒功能同时整体停用）")
	else:
		print("服务未在运行")
	return 0


def probe_service(plan_path):
	"""判断服务是否在运行。

	**以端口为准，而不是以 service.json 里记的 pid 为准。**
	服务可能是被计划任务直接拉起的（绕过 service.py start），那样记录的 pid 就是旧的；
	只要端口在监听就说明服务活着。发现 pid 过期时顺手刷新 service.json。
	"""
	info = read_service(plan_path)
	port = info.get("port") or DEFAULT_PORT
	pid = info.get("pid")

	if not port_open(port):
		return False, {}, None

	# 端口通 → 服务在跑。核对 pid，不一致就重新反查并刷新记录
	actual = find_service_pid(port)
	if actual and actual != pid:
		info = {
			"pid": actual,
			"port": port,
			"startedAt": info.get("startedAt") or datetime.now().isoformat(timespec="seconds"),
		}
		try:
			service_path(plan_path).write_text(
				json.dumps(info, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")
		except OSError:
			pass
	return True, info, actual or pid


def run_cleanup_script(dry_run=False):
	"""调用 cleanup.ps1，返回 (是否成功, 输出行列表)。"""
	if not CLEANUP.exists():
		return False, ["找不到清理脚本: %s" % CLEANUP]
	ps_root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
	exe = ps_root / "powershell.exe"
	if not exe.exists():
		exe = Path("powershell.exe")
	cmd = [str(exe), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CLEANUP)]
	if dry_run:
		cmd.append("-DryRun")
	proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
	out = (proc.stdout or proc.stderr or "").strip()
	lines = [line for line in out.splitlines() if line.strip()] or ["(无输出)"]
	return proc.returncode == 0, lines


def cmd_uninstall(args):
	"""完整卸载：停服务 + 删计划任务。

	删任务走扫描式清理（cleanup.ps1），而不是按 state.json 里记的名字删 ——
	因为用户很可能先删了目录再想起来卸载，那时名字已经拿不到了。
	"""
	plan_dir = None
	if args.plan:
		plan_dir = Path(args.plan).resolve().parent

	print("== 1. 停止服务 ==")
	if plan_dir and (plan_dir / "plan.json").exists():
		if stop_service(plan_dir / "plan.json"):
			print("服务已停止")
		else:
			print("服务未在运行")
	else:
		print("未找到计划文件，跳过；下面的清理会一并结束残留进程")

	print()
	print("== 2. 删除计划任务 ==")
	ok, lines = run_cleanup_script()
	for line in lines:
		print(line)
	if not ok:
		print("清理失败，请手动在「任务计划程序」中检查")
		return 1

	print()
	print("== 3. 收尾 ==")
	if plan_dir and plan_dir.exists():
		print("配置文件仍在: %s" % plan_dir)
		print("确认不再需要后，可手动删除该目录（含 plan.json / state.json 等）")
	else:
		print("没有需要清理的配置文件")
	print()
	print("卸载完成。开机登录时不会再尝试启动本服务。")
	return 0


def cmd_cleanup(args):
	"""只清理系统残留，不需要计划文件。技能目录被删后可用它兜底。"""
	ok, lines = run_cleanup_script(dry_run=args.dry_run)
	for line in lines:
		print(line)
	return 0 if ok else 1


def cmd_status(args):
	plan, path = reminder.load_plan(args.plan)
	running, info, pid = probe_service(path)

	print("服务状态: %s" % ("运行中" if running else "未运行"))
	if running:
		print("进程号:   %s" % pid)
		print("端口:     %s" % info.get("port"))
		if info.get("startedAt"):
			print("启动于:   %s" % info["startedAt"])
		print("日程表:   http://127.0.0.1:%s/" % info.get("port"))
	else:
		print("提示: 服务未运行期间不会有任何提醒弹出")

	state = reminder.load_state(path)
	print()
	print("-- 提醒开关 --")
	print("enabled       = %s" % state.get("enabled"))
	print("pausedUntil   = %s" % state.get("pausedUntil"))
	print("disabledSlots = %s" % (state.get("disabledSlots") or []))
	print()
	print("-- 各时间点 --")
	for slot in plan["slots"]:
		active, reason = reminder.check_active(plan, slot, state)
		print("%s  %-16s %s%s" % (slot["time"], slot["name"], "会弹" if active else "不弹",
			"" if active else "  (" + reason + ")"))

	fired = load_fired(path).get(datetime.now().strftime("%Y-%m-%d"), [])
	print()
	print("今日已触发: %s" % (", ".join(fired) if fired else "（无）"))
	return 0


def cmd_run(args):
	"""前台运行；start 拉起的也是这个分支。"""
	plan, path = reminder.load_plan(args.plan)
	port = args.port or DEFAULT_PORT

	scheduler = Scheduler(path, port)
	scheduler.start()

	Handler.plan_path = path
	Handler.port = port
	Handler.scheduler = scheduler

	server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
	log(path, "HTTP 服务监听 127.0.0.1:%d" % port)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		scheduler.stop_flag.set()
		server.server_close()
		log(path, "HTTP 服务已关闭")
	return 0


def main():
	parser = argparse.ArgumentParser(description="日程提醒服务")
	sub = parser.add_subparsers(dest="cmd", required=True)

	for name, helptext, fn in (
		("start", "启动服务（提醒功能随之生效）", cmd_start),
		("stop", "停止服务（提醒功能随之停用）", cmd_stop),
		("status", "查看服务与提醒状态", cmd_status),
		("run", "前台运行服务", cmd_run),
	):
		p = sub.add_parser(name, help=helptext)
		p.add_argument("--plan", required=True)
		p.add_argument("--port", type=int, default=None)
		p.add_argument("--no-task", action="store_true",
			help="start 时不用计划任务托管，改为直接创建进程")
		p.set_defaults(func=fn)

	# 卸载：停服务 + 删计划任务（不需要计划文件也能跑，见 cmd_uninstall）
	p = sub.add_parser("uninstall", help="卸载：停服务并删除计划任务")
	p.add_argument("--plan", default=None, help="计划文件路径，可省略")
	p.set_defaults(func=cmd_uninstall)

	# 纯清理：只删系统残留，用于技能目录已被删除后的兜底
	p = sub.add_parser("cleanup", help="只清理计划任务残留（技能删了也能用）")
	p.add_argument("--dry-run", action="store_true", help="只列出将删除什么，不实际删")
	p.set_defaults(func=cmd_cleanup)

	args = parser.parse_args()
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())
