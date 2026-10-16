# 提醒计划格式、运行状态与平台实现

## 〇、先读这个：推荐服务化

现在**首选的服务形态**是常驻进程 `service.py`（启动服务=提醒生效，停止服务=整体停用）。
服务模式的完整说明见 **`references/service.md`**，改服务代码前必读。

本文档余下部分描述引擎侧（`reminder.py`）的数据格式，两种形态共用。

## 一、两种运行形态

| 形态 | 排期由谁负责 | 启动方式 | 适用 |
|---|---|---|---|
| **服务**（推荐） | 服务内置的定时线程 | `service.py start` | 需要页面联动、统一开关、常驻 |
| 传统多触发器 | 系统调度器 | `install --mode timer` | 不想常驻进程 |

两种形态共用同一套 `plan.json` / `state.json` / 弹窗，可随时切换。

### 传统形态的触发时怎么知道该弹哪一条

各平台都**不支持**给同一任务的多个触发器分别传参数，所以触发时不传时间。
脚本改为读取**当前系统时间**，去 plan 里匹配对应的日程 —— 触发时刻就是日程时刻。

匹配带 **±5 分钟容差**（`MATCH_TOLERANCE`），以防调度器轻微延迟导致匹配不上。
也可以用 `--at HH:MM` 手动指定时刻来匹配。

## 二、数据结构：动作库 + 时间点引用

计划用**两层结构**组织，这是为了避免同一个动作的文案在多处重复：

```json
{
  "actions": {
    "breath": {
      "name": "呼吸收腹",
      "dose": "保持 10 秒 × 3 轮",
      "muscles": "腹横肌 · 膈肌",
      "color": "blue",
      "steps": ["...", "..."],
      "trap": "..."
    }
  },
  "slots": [
    { "id": "0900", "time": "09:00", "action": "breath", "dose": "10 秒 × 3",
      "cue": "手放肚脐下两指。鼻子吸气 4 秒...", "visibility": 5 },
    { "id": "1115", "time": "11:15", "action": "breath", "dose": "10 秒 × 3",
      "cue": "跟 9:00 那组一样...", "visibility": 5, "manual": true }
  ]
}
```

**为什么要这样分**：同一个动作（如「呼吸收腹」）一天出现 3 次，但**每次的展示文案不同**
（时间轴上的 `cue` 是针对性的一句话），而**动作本身的做法完全相同**（`steps` / `trap`）。
把做法抽到 `actions` 里，一处修改三处生效 —— 弹窗、页面时间轴、页面分步说明。

`load_plan()` 会把引用展开成扁平结构，下游代码不必各自处理引用。

### actions 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | **是** | 动作名 |
| `dose` | 否 | **完整剂量**，如 `保持 10 秒 × 3 轮`。弹窗与分步说明用这个 |
| `muscles` | 否 | 目标肌群，如 `腹横肌 · 膈肌` |
| `color` | 否 | 时间轴配色：`blue` / `teal` / `purple` / `amber` / `gray`，缺省 blue |
| `steps[]` | **是** | 分步文案，4–6 步 |
| `trap` | 否 | 易错点。强烈建议每条都写 |

## 三、slots 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | **是** | 唯一短标识，如 `1030`，用于命令行定位、接口参数、永久关闭 |
| `time` | **是** | `HH:MM` 24 小时制 |
| `action` | 推荐 | 指向 `actions` 的键。**推荐总是用它**，只有一次性动作才内联写 |
| `dose` | 否 | **时间轴上的短剂量**，如 `10 秒 × 3`。比动作库的完整剂量短，为了排版不换行 |
| `cue` | 否 | 时间轴卡片上的一句话要点。建议写，且**要针对这个时间点**，不要照搬动作说明 |
| `visibility` | 否 | 隐蔽度 1–5，页面画成圆点。5 = 完全看不出来 |
| `manual` | 否 | `true` = **不参与自动调度**，只能由用户在页面上手动点开 |
| `name` / `muscles` / `steps` / `trap` / `color` | 否 | 内联覆盖动作库的同名字段（一般不需要） |

约束：`id` 只能含字母数字（会拼进参数与接口 query）；`days` 在同一份 plan 内共享。

### 关于「自动」与「手动」

一套日程通常分两类时间点，**都放进 plan，用 `manual` 区分**：

| | 骨架（默认） | 加分项（`"manual": true`） |
|---|---|---|
| 到点自动弹 | 会 | 不会 |
| 页面/接口手动弹出 | 能 | 能 |

加分项不该到点打扰用户，但想练时得能一键调出来，所以不能省掉。
**不要只写在页面里而不进 plan** —— 那样用户在页面上点了也没反应（这正是本技能早期的教训）。

## 四、页面级内容（`page` 字段）

日程表页面由技能模板 `assets/schedule-template.html` 渲染，内容全部来自 plan：

| 字段 | 说明 |
|---|---|
| `title` | 页面大标题 |
| `subtitle` | 标题下的一行说明 |
| `page.stats[]` | 顶部三个数据卡。`{"auto": "count"}` 表示自动填时间点总数，否则用 `{"value": "...", "label": "..."}` |
| `page.principles[]` | 三条原则卡：`{"name", "desc"}` |
| `page.sections` | `{"morning": "...", "afternoon": "..."}`，上午/下午的分区标题 |
| `page.break` | 中间的休息提示：`{"title", "time", "heading", "desc"}` |
| `page.howtoTitle` | 分步说明段的标题 |
| `page.panels[]` | 底部两个面板：`{"title", "items": [{"bold", "text"}], "note"}` |
| `page.warning` | 最底部的医学提示 |

上午/下午按 `time` 的小时数自动分区（< 12 为上午，>= 14 为下午），无需手工指派。
分步说明段按**动作在 slots 中首次出现的顺序**列出，同一动作只出现一次。

## 五、state.json 运行状态

**内容与状态分离**：`plan.json` 管文案，`state.json` 管开关，两者同目录，便于整套搬迁。

```json
{
	"enabled": true,
	"pausedUntil": null,
	"disabledSlots": [],
	"taskName": "CoreReminder",
	"schedule": "D:/path/schedule.html"
}
```

| 字段 | 含义 |
|---|---|
| `enabled` | **总开关**。改为 `false` 后所有提醒都不弹（这是「用配置文件标记是否启用」的落点） |
| `pausedUntil` | 暂停到这个时刻（ISO 时间）。`null` 表示未暂停 |
| `disabledSlots` | 已「不再提醒」的环节 id 列表，只影响列出的环节 |
| `taskName` | install 时写入，供后续 `status` / `uninstall` / 服务定位计划任务使用 |

判断顺序（`check_active`）：总开关 → 单环节永久关闭 → 暂停时段。任一条命中即不弹。

## 六、命令参考

```bash
# 引擎
python scripts/reminder.py preview   --plan plan.json [--slot 1030] [--show]
python scripts/reminder.py install   --plan plan.json --name core [--mode service|timer]
python scripts/reminder.py status    --plan plan.json [--name core]
python scripts/reminder.py pause     --plan plan.json [--date YYYY-MM-DD]
python scripts/reminder.py resume    --plan plan.json
python scripts/reminder.py disable   --plan plan.json --slot 1030
python scripts/reminder.py enable    --plan plan.json --slot 1030
python scripts/reminder.py schedule  --plan plan.json
python scripts/reminder.py uninstall --name core
python scripts/reminder.py render    --plan plan.json [--at HH:MM] [--force]

# 服务
python scripts/service.py start   --plan plan.json [--port 8768] [--no-task]
python scripts/service.py stop    --plan plan.json
python scripts/service.py status  --plan plan.json
python scripts/service.py run     --plan plan.json      # 前台，便于看输出
```

- `preview --show` 会真实弹一次窗体，可直接测试弹窗上的按钮
- `render --force` 忽略开关状态强制弹出，用于排查「为什么到点没弹」
- `install --dry-run` 先看会生成什么任务与触发器
- `winform.ps1 -ParseOnly` 只做参数解析与控件构建、不显示窗体，用于自检（会打印 awareness / scale / client 尺寸）
- `winform.ps1 -Scale 2 -CapturePath preview.png` 可离屏渲染任意缩放档位的预览图（不弹窗、不响铃），用于排查高分屏问题

## 七、弹窗交互（Windows）

### 关闭规则

窗口**只能**由这四种方式关闭：

| 方式 | 退出码 | 效果 |
|---|---|---|
| 「知道了」按钮 | 0 | 不做任何变更 |
| 右下 ▾ →「不再提醒」 | 3 | 把当前 slot id 追加进 `disabledSlots` |
| 窗口关闭按钮 ×（或 Alt+F4） | 0 | 不做任何变更 |
| ESC | 0 | 不做任何变更（绑定在 CancelButton 上） |

**下列操作不允许关闭窗口**，否则用户读到一半就丢了内容：

| 操作 | 行为 |
|---|---|
| 左下「日程表」 | 用系统默认浏览器打开（服务地址优先），窗口保持不动，**按钮保持可点**（关掉页面后能再点开）；状态行提示「已打开日程表」 |
| 右下 ▾ →「暂停本日所有提醒」 | 写 `pausedUntil`（今日 23:59:59），窗口保持不动，底部状态行提示已暂停，菜单项置灰 |

设计理由：打开日程表和暂停提醒都属于「辅助动作」，用户点完还要继续看当前这条动作说明。
只有「知道了」表达「我看完了」，「不再提醒」表达「以后都不想看」——这两者才该关窗。
**「日程表」按钮点一次后不禁用**：曾实现为点过就置灰，结果用户关掉日程表页面后想再看就没法点了（死路）。
置灰只是「已打开过」的伪反馈，浏览器窗口本身就是反馈，所以按钮永远可点。

### 关键实现点

- **暂停由窗体自己执行**：窗体用 `Start-Process` 拉起一个隐藏的子进程跑
  `reminder.py pause --plan <plan>`，靠 Python 写 `state.json` 保证中文编码可控，再读子进程退出码决定提示文案。
  因此 `reminder.py` 通过 `-PythonExe` / `-ScriptPath` 两个参数把解释器与脚本路径传给窗体。
- **默认浏览器解析**：优先读
  `HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice` 的 `ProgId`，
  再去 `HKCU/HKLM\Software\Classes\<ProgId>\shell\open\command` 取可执行文件路径。
  这样即使 `.html` 被关联到了编辑器，日程表也一定在浏览器里打开。
  解析不到时回落到 `Start-Process`（走 shell 关联）。
- **高分屏适配（winform.ps1 内建）**：进程在触碰任何 WinForms/GDI 之前先声明 DPI 感知
  （优先 Per-Monitor V2，失败退回 shcore、再退回 system aware；感知一旦锁定不可更改，所以必须最先执行）。
  整套布局按 96 DPI 网格设计，运行时乘以显示器缩放系数，字体用像素单位精确匹配栅格——
  文字不再被系统位图拉伸发虚，任意 100%–300% 缩放下按钮都完整不被遮挡。
  窗体高度超出工作区时自动压缩两个可滚动面板，底部按钮栏永远可见。
- **状态行不再常驻**：底部那行灰色文字只在有话要说时出现（已暂停 / 打开失败等），
  且独占一个不与按钮重叠的条带。旧版它常驻并压住按钮顶部 2px（透明标签把按钮上沿擦掉一条白），已修复。
- **退出码只承载状态变更**：PowerShell 只返回码，**状态落盘一律由 Python 完成**。

macOS / Linux 的弹窗没有按钮（系统对话框能力有限），降级为纯文本展示，开关仍通过命令行或服务接口控制。

### ui 字段与控件对应

| 字段 | 用途 |
|---|---|
| `ui.ok` | 「知道了」按钮 |
| `ui.trap` | 易错点前缀 |
| `ui.footer` | 底部状态行初始文字；**留空或缺省时整行隐藏**（不要再放操作说明，按钮语义用户看得懂） |
| `ui.schedule` | 「日程表」按钮 |
| `ui.scheduleOpened` | 日程表打开成功后状态行文字（缺省则不显示） |
| `ui.scheduleFailed` | 打开失败时状态行文字 |
| `ui.pauseToday` | 下拉菜单项一 |
| `ui.paused` | 暂停成功后状态行文字 |
| `ui.pauseFailed` | 暂停失败时状态行文字 |
| `ui.disable` | 下拉菜单项二 |

## 八、踩过的坑（改代码前务必先读）

1. **不要用 automation 做固定文案提醒。** 曾用 `automation_update` 定时推送这类内容，结果是：规则本身正常（ACTIVE、触发时间算准），但每次触发都会**真的拉起一个 AI 会话去现场生成文案**。用户侧看到的就是「到点了它一直在思考」，服务端繁忙（502/503）时直接卡死不产出。
   **结论：内容固定、不需要 AI 临场判断的定时推送，一律走本地系统调度器。**

2. **PowerShell 脚本必须纯 ASCII —— 连注释都不能有中文。** 这是最容易犯、也最难查的坑。
   PowerShell 5.1 读取无 BOM 的 .ps1 时按本地代码页（中文 Windows 是 GBK）解析。UTF-8 的中文/中文标点会产生**奇数个字节**，末尾那个字节（≥0x81）会与紧随其后的 ASCII 字符配成 GBK 双字节，**把换行符或 ASCII 字符吞掉**。
   症状非常迷惑：报错行号与实际文件行号对不上，且报出莫须有的「在此对象上找不到属性 Text」。
   **规矩：`assets/winform.ps1` 及一切内嵌 PowerShell 的字符串，注释也只写英文。**

3. **`slots` 是数组，不是顶层键。** 渲染端要用 `foreach ($s in $plan.slots)` 按 `id` 匹配。若沿用旧扁平格式的 `$plan.PSObject.Properties[$id]`，会静默取不到值。

4. **弹窗不自动关闭 = 必须放大任务执行时限。** 计划任务的 `ExecutionTimeLimit` 到期会**强行杀掉进程**，窗体会毫无征兆消失。模板设的是 12 小时。改交互模式时务必同步检查。

5. **`(Get-ScheduledTask).State` 是枚举**，没有 `.ToUpper()`，要 `.ToString()` 后才能转大写。

6. **Windows 显示 UI 需要交互式登录**（`LogonType Interactive`）。锁屏不弹、关机跳过且不补播 —— 系统机制，不是故障。

7. **`MultipleInstances IgnoreNew`**：上一个弹窗没关、下个时间点又到了时，不会堆叠窗体。

8. **Linux cron 下没有图形会话环境**，`zenity` 需要 `DISPLAY`。脚本已做 zenity → notify-send → 终端输出三级降级；真出问题检查 crontab 里是否有 `DISPLAY=:0`。

9. **Windows 上优先用 pythonw.exe**，否则每次投递会闪一个黑框命令行窗口。`python_for_schedule()` 自动探测。

10. **编辑文案不需要重装。** 改 `steps` / `trap` / `dose` 立刻生效。
    **只有增删 slot、改 time、改 days 才需要重新 install**（先 uninstall 再 install 最干净）。

11. **交付未编码的 .bat 会乱码。** cmd 按本地代码页解析批处理，中文必须存成 **GBK**，且必须用 **CRLF** 行尾（LF 会让 cmd 拆坏多行语句）。做法：用 Python 以 `encoding='gbk', newline='\r\n'` 写出。

12. **从 shell 里 `Popen(DETACHED_PROCESS)` 拉起的后台进程不可靠。** 在带 Job Object 的环境
    （受限终端、某些沙箱、CI）里，父进程的作业一关闭，子进程就被回收，服务会在启动成功后几秒内静默消失。
    **正确做法是交给任务计划程序托管**：`schtasks /run /tn <任务名>`。任务由系统服务拉起，与任何终端脱钩。
    这也是 `service.py start` 首选 `schtasks` 的原因；直接 spawn 只作为兜底。

13. **`schtasks.exe` 可能被安全策略黑名单拦下。** 调用它会抛 `PermissionError: [WinError 5] 拒绝访问`
    或被明确阻止。因此 `run_schtasks()` 必须捕获异常并返回一个「失败」结果对象，
    让 `start` 能自动降级到直接 spawn，而不是直接崩掉。

14. **服务化后必须给 HTTP 响应加 CORS 头。** 日程表页面可能以 `file://` 直接打开，
    此时对 `http://127.0.0.1` 发的是跨源请求（`Origin: null`），
    不加 `Access-Control-Allow-Origin` 会被浏览器直接拦掉，表现是「点按钮毫无反应」。
    另外用 POST 且不带自定义头属于简单请求，不会触发 OPTIONS 预检。

15. **服务停止时不能只 kill 进程。** 还要 `schtasks /end` 结束任务实例，
    否则任务会卡在「正在运行」状态，下次 `schtasks /run` 启不动。

16. **服务进程的 pid 要从端口反查，不能依赖 Popen 句柄。** 进程是计划任务拉起的，拿不到句柄。
    用 `netstat -ano` 找 `127.0.0.1:<port>` 的 LISTENING 行，取最后一列即 pid。

17. **命令行的 `render` 必须支持 `--slot`，且与服务的调用参数保持一致。** 服务内部是用
    `render --plan X --slot <id>` 拉起弹窗的；如果 `render` 只接受 `--at`，服务每次触发都会以
    `error: unrecognized arguments: --slot` 退出，**表现为「服务在跑但到点永远不弹」**，而且
    这个错误只落在子进程的 stderr 里、主进程日志看不到，极难察觉。
    改 `render` 的参数时，务必回头核对 `service.py` 里 `Scheduler.spawn()` 拼的命令行。

18. **凡是被 `subprocess` 拼出来的命令行参数，都要有覆盖测试。** 上面第 17 条之所以漏掉，是因为
    只测了「接口能否返回 200」，没测「子进程真的活下来」。
    验证方式：直接调 `Scheduler.spawn(slot, path)` 再检查 `proc.poll() is None`，
    比等真实到点（或看 HTTP 状态码）可靠得多。

19. **不要让页面内容与 plan.json 各存一份。** 早期形态是页面 HTML 硬编码时间点和动作名、
    plan 里另存一份，结果两边必然失同步 —— 用户看到「页面上有 11:15、但到点不弹」，
    根因是页面 11 个点、plan 只有 7 个，本就互不相干。
    **正确做法：页面由服务用技能模板 + plan 数据渲染**（见 `render_page`），
    并给 plan 补 `cue` / `visibility` / `muscles` 等展示字段。
    判断标准：**改一处配置，页面和弹窗应当同时变**。若不会，说明又有重复了。

20. **同一动作在多处出现时，做法文案要抽到动作库。** 否则「呼吸收腹」在一天里出现 3 次，
    改一次要改 3 处，必然漏。用 `actions` + `slots[].action` 引用。
    `load_plan()` 负责把引用展开成扁平结构，下游代码不必各自处理。

21. **往 HTML 里注入 JSON 时要转义 `</`。** 直接把 `json.dumps` 的结果写进 `<script>` 里，
    如果数据中出现 `</script>` 就会提前闭合脚本块。做法：`payload.replace("</", "<\\/")`。

22. **注入点的写法要让模板在「未被注入」时仍然合法。** 模板里写
    `window.__PLAN__ = null;` 作为注入点，服务替换它为真实数据；以 `file://` 直接打开时
    保持 `null`，页面的启动逻辑据此回落到 `/api/plan`。若把注入点写成裸占位符
    （如 `__PLAN_JSON__`），直接打开会因语法错误整页白屏。

23. **不要把文件路径存进 `state.json`。** 早期 `install` 会把日程表 HTML 的路径写进
    `state["schedule"]`，结果那个文件被删除后，state 里留了一条指向不存在文件的失效路径，
    而 `status` 命令还会把它显示成「日程表 = ...」，看着像配置正常、实际早已无效。
    **规则：state 只存运行状态（开关、暂停、任务名），不存资源路径。**
    路径要么写在 plan 里（配置），要么由服务按 port 推导（约定），不要复制到 state。

24. **判断服务是否在运行，要以端口为准，不能以 `service.json` 里记的 pid 为准。**
    服务可能是被计划任务直接拉起的（`schtasks /run`，绕过 `service.py start`），
    这时记录里的 pid 就是旧的；一旦拿旧 pid 去查进程，就会误报「服务未运行」——
    而实际上服务跑得好好的、提醒照常弹。
    **正确做法（`probe_service`）**：先探端口，端口通就是活着；再拿端口反查真实 pid，
    与记录不一致就顺手刷新 `service.json`。`start` / `stop` / `status` 三个命令都走这个探测。

25. **卸载必须能「不依赖任何配置文件」地工作。** 这是本技能最危险的隐患点。
    它注册了登录自启任务；用户停用技能时若没清理，**每次开机都会尝试启动不存在的脚本而报错**。
    而用户最可能的顺序是「先删目录 → 后来才想起卸载」，此时：
    - `state.json` 没了 → 按名字删任务的方案失效（拿不到任务名）
    - 技能目录没了 → 清理脚本本身也没了
    **解法**：
    - 清理脚本按**任务动作里记录的脚本路径**识别任务（匹配技能目录名），不依赖任何状态文件
    - 安装时把清理脚本**复制一份到用户的配置目录**，并生成双击即用的入口
    - 卸载命令不需要 `--plan` 也能跑（`cleanup` 子命令）
    验证方式：删掉 `state.json` 后跑 `cleanup -DryRun`，应仍能列出任务。

26. **轮询型接口要静音日志，否则会淹掉真正的记录。** 网页每 30 秒拉一次 `/api/status`，
    另外浏览器每次开页面都会请求 `/favicon.ico`。这些例行请求若全写日志，
    一天下来会把「弹出」「跳过」这类**真正有用的记录**埋掉，排查时根本翻不到。
    做法：维护一份 `QUIET_PATHS`，只对**成功**的例行请求静音（4xx/5xx 仍要留痕，否则出问题查不到）。
    判断成功与否需要在 `_send()` 里记下响应码供后续读取。

27. **改技能名不是「重命名」那么简单，是一次带迁移的改动。** 2026-10-15 把技能名从
    `scheduled-desktop-reminder` 改成 `desk-coach`，涉及五件事，漏一件就出事：

    | 事项 | 后果 |
    |---|---|
    | 目录名 + `SKILL.md` 的 `name:` 必须同步改 | 两者不一致技能系统识别不到 |
    | **必须重新 `install`** | 计划任务记录的是脚本**绝对路径**，路径变了任务就指向不存在的文件，**开机登录必然报错** |
    | 清理脚本的识别标记要更新 | 只认新名会导致旧任务成为清不掉的孤儿 |
    | Linux 的 cron 标记要兼容旧名 | 只认新标记则旧块残留，与新的并存 → **同一提醒弹两次** |
    | 用户的卸载入口副本要刷新 | 它存在用户目录里，`install` 时会重新生成 |

    **推荐做法：识别名单同时保留新旧两个名字**（`cleanup.ps1` 的 `$needles`、
    `reminder.py` 的 `SKILL_IDS`），这样改名前后注册的任务都能被正确清理。
    Linux 的 `_strip_block` / `status` 也按双标记匹配，避免残留重复块。

    另外两点经验：
    - **技能列表缓存靠目录签名失效**（`~/.workbuddy/.skill-list-cache.json` 里的
      `signature` 是文件数+哈希），改目录名后自动重建，**不需要手工改缓存**。
    - 改名时旧目录可能被占用（`Device or resource busy`）。若内部文件可移动、
      只有目录本身锁着，用「新建目录 → 搬内容 → 删空壳」绕过。
