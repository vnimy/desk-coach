# 定时桌面提醒器

把一份「训练计划」变成一台会自己提醒你的本机小程序。

到点弹出窗口，显示完整的动作分步说明和易错点；配套一个日程表网页，可以查看全天安排、一键补做、随时暂停。

**全程离线，不联网、不依赖大模型。**

---

## 它解决什么问题

办公族想做点核心训练或拉伸，最难的通常不是「不知道怎么做」，而是两件事：

1. **到点会忘** —— 靠意志力记住一天 7 个时间点不现实
2. **想起也不会做** —— 脑子里只剩「练核心」三个字，具体怎么呼吸、腰怎么摆、哪块肌肉该用力，全忘了

所以这个工具的重点不只是「提醒」，而是**提醒里带着能照着做的完整说明**。每条提醒包含：

- 分步操作（① ② ③，每步一句话）
- 剂量（保持几秒、做几轮）
- 目标肌群
- **易错点** —— 最容易做错的地方，这是判断自己有没有做对的唯一现场依据

## 核心特性

| 特性 | 说明 |
|---|---|
| **定时弹窗** | 到点弹出，显示完整说明，停留到你手动关闭（不会 60 秒就消失） |
| **日程表网页** | 全天安排一览；改配置刷新即见，不用重启 |
| **三层开关** | 总开关 → 暂停到今天 → 单独关掉某一环节 |
| **手动补做** | 点任一时间点立即弹出该条提醒，不必等到点 |
| **两类时间点** | 「自动弹」（骨架，到点必弹）与「手动补做」（加分项，不打扰但随时可调出） |
| **单份文案** | 同一动作一天出现多次时，做法只写一份，改一处三处生效 |
| **启停即生效** | 启动服务 = 提醒生效；停止服务 = 整体停用 |
| **跨平台** | Windows / macOS / Linux |

## 工作原理

分成两个阶段，这是理解它的关键：

```
配置阶段（一次性，需要 AI 或手工编排）
  编排计划  →  撰写文案  →  注册自启
                                ↓
运行阶段（长期，电脑自己转）
  服务启动  →  每 15 秒轮询  →  命中时间点  →  弹出提醒
                                        ↓
                                  日程表网页 / 三层开关 / 手动补做
```

**运行期完全不需要 AI 参与。** 提醒内容来自本地 JSON，投递走本地进程，所以不存在「到点了但它在慢慢生成」的问题。

### 调用关系

```
  系统时间到点 ──┐
                ├──→  service.py  ──→  reminder.py  ──→  弹出提醒框
  你点网页按钮 ──┘        │
                         └──→  state.json（开关状态）
```

- `service.py` —— 常驻服务：内置定时线程 + HTTP 接口 + 页面渲染
- `reminder.py` —— 引擎：读配置、匹配日程、渲染弹窗
- `state.json` —— 只存运行状态（开关、暂停、任务名）

## 快速开始

### 环境要求

- Python 3.8+（Windows 建议用 `pythonw.exe`，避免弹窗时闪黑框）
- Windows / macOS / Linux

### 安装

把本目录放到技能目录下：

```bash
# 用户级（所有项目可用）
cp -r scheduled-desktop-reminder ~/.workbuddy/skills/

# 或项目级
cp -r scheduled-desktop-reminder <项目>/.workbuddy/skills/
```

### 配置

复制 `assets/plan.example.json` 改成自己的计划（见下方《配置文件》），放到一个长期目录，例如：

```
~/.workbuddy/my-reminder/plan.json
```

**不要放临时目录** —— 计划的绝对路径会被写进系统任务和服务，文件被移动或删除会导致投递失败。

### 安装并启动

```bash
# 1. 先看文案对不对
python scripts/reminder.py preview --plan ~/.workbuddy/my-reminder/plan.json

# 2. 注册为登录自启
python scripts/reminder.py install --plan ~/.workbuddy/my-reminder/plan.json --name MyReminder

# 3. 启动服务
python scripts/service.py start --plan ~/.workbuddy/my-reminder/plan.json

# 4. 确认在跑
python scripts/service.py status --plan ~/.workbuddy/my-reminder/plan.json
```

启动后打开 <http://127.0.0.1:8768/> 就是日程表页面。

## 配置文件

`plan.json` 是**两层结构**，这样组织是为了避免同一份文案在多处重复：

```json
{
	"title": "工位核心训练日程表",
	"days": ["MON", "TUE", "WED", "THU", "FRI"],
	"port": 8768,

	"actions": {
		"breath": {
			"name": "呼吸收腹",
			"dose": "保持 10 秒 × 3 轮",
			"muscles": "腹横肌 · 膈肌",
			"color": "blue",
			"steps": [
				"坐到椅子上，背离开椅靠大约一拳的距离……",
				"用鼻子慢慢吸气，数 4 秒，让肚子像气球一样往外鼓起来……"
			],
			"trap": "最常犯的错是一边吸气一边把肚子收进去，那是憋气。"
		}
	},

	"slots": [
		{
			"id": "0900", "time": "09:00", "action": "breath",
			"dose": "10 秒 × 3",
			"cue": "手放肚脐下两指。鼻子吸气 4 秒把肚子鼓起来……",
			"visibility": 5
		}
	]
}
```

- **`actions`** = 动作库。一个动作的**做法**只写一份（`steps` / `trap` / 完整 `dose`）
- **`slots`** = 时间点。用 `action` 引用动作库，自己只写这个时间点特有的东西（`cue` / 短 `dose` / `visibility`）

为什么值得这样分：同一个「呼吸收腹」一天出现 3 次，**做法完全相同，但每次的提示语不同**。抽开之后，改动作做法只需改一处，弹窗、网页时间轴、网页分步说明**三处同时生效**。

### 关键字段

| 字段 | 说明 |
|---|---|
| `slots[].manual` | `true` = 不自动弹，只能手工点开。用于「加分项」 |
| `slots[].cue` | 网页卡片上的一句话要点，建议针对该时间点写，不要照搬动作说明 |
| `slots[].visibility` | 隐蔽度 1–5，网页上画成圆点。5 = 完全看不出来 |
| `actions[].color` | 网页配色：`blue` / `teal` / `purple` / `amber` / `gray` |
| `page` | 网页级内容：标题、三条原则、分区标题、底部面板、医学提示 |

完整字段规范见 [`references/plan-schema.md`](references/plan-schema.md)。

## 常用命令

### 服务

```bash
python scripts/service.py start     --plan plan.json    # 启动（提醒生效）
python scripts/service.py stop      --plan plan.json    # 停止（提醒停用）
python scripts/service.py status    --plan plan.json    # 查看状态
python scripts/service.py run       --plan plan.json    # 前台运行，便于看输出
python scripts/service.py uninstall --plan plan.json    # 卸载：停服务 + 删计划任务
python scripts/service.py cleanup   --dry-run           # 只清理系统残留，可先预演
```

### 引擎

```bash
python scripts/reminder.py preview --plan plan.json [--show]  # 预览文案，--show 真实弹一次
python scripts/reminder.py status  --plan plan.json           # 查看各时间点状态
python scripts/reminder.py pause   --plan plan.json           # 暂停到今天结束
python scripts/reminder.py resume  --plan plan.json           # 恢复
python scripts/reminder.py disable --plan plan.json --slot 1030   # 关闭某环节
python scripts/reminder.py enable  --plan plan.json --slot 1030   # 重新开启
python scripts/reminder.py render  --plan plan.json --slot 1030 --force  # 立即弹一次
python scripts/reminder.py uninstall --name MyReminder        # 卸载
```

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 日程表页面 |
| GET | `/api/status` | 服务与提醒状态 |
| GET | `/api/plan` | 展开后的计划数据 |
| POST | `/api/pause` | 暂停本日全部 |
| POST | `/api/resume` | 恢复 |
| POST | `/api/disable?id=1030` | 关闭某环节 |
| POST | `/api/enable?id=1030` | 开启某环节 |
| POST | `/api/fire?id=1030` | 立即弹出该条提醒 |

只监听 `127.0.0.1`，不对外暴露。

## 目录结构

```
scheduled-desktop-reminder/
├── SKILL.md                        技能说明（供 AI 读取的工作流）
├── README.md                       本文件
├── scripts/
│   ├── service.py                  常驻服务：定时线程 + HTTP + 页面渲染
│   └── reminder.py                 引擎：读配置、匹配日程、渲染弹窗
├── assets/
│   ├── schedule-template.html      日程表网页模板（唯一一份）
│   ├── winform.ps1                 Windows 弹窗渲染器
│   ├── cleanup.ps1                 系统残留清理（安装时复制一份到用户目录）
│   └── plan.example.json           完整示例计划（11 个时间点）
└── references/
    ├── plan-schema.md              字段规范 + 24 条踩坑清单
    ├── service.md                  服务架构与排查
    └── training-design.md          训练内容编排方法论
```

## 推荐用法：配合 AI 编排内容

配置里最花时间的不是填时间，而是**写文案** —— 每个动作怎么分步、剂量给多少、容易错在哪。

`references/training-design.md` 就是给这件事写的编排指南，核心是先问清楚四件事：

1. **训练目的** —— 服务于某项运动，还是办公健康 / 体态 / 康复
2. **执行场景** —— 工位坐着 / 居家 / 健身房（决定动作要不要隐蔽）
3. **作息与时间窗** —— 注意开工和下班那两个时间点常常不准，往前或往后挪 15–30 分钟
4. **医学禁忌** —— 腰椎间盘突出、孕期、高血压等

**同一个「练核心」，为了蝶泳和为了缓解久坐腰痛，编排出来是两套完全不同的动作。** 所以务必先问目的，再选动作。

## 设计取舍

### 为什么不用平台的定时任务功能

试过，不行的原因是：平台的定时任务在触发时会真的拉起一个 AI 会话去**现场生成**这条提醒。内容明明是固定的，却依赖模型，结果就是到点了你看到它「一直在思考」，服务端繁忙时直接卡死不产出。

所以这里的原则是：**内容固定、不需要 AI 临场判断的定时推送，一律走本地系统调度器或本地进程。**

### 为什么做成常驻服务而不是系统任务

系统任务能做定时，但做不了这三件事：统一的启停开关、网页按钮联动、运行状态查询。

服务化之后，系统里那唯一一个任务退化成「登录时把服务拉起来」，排期完全归服务自己管。

### 为什么窗口不自动关闭

提醒的目的是让你**读完并照做**。60 秒自动关闭会导致读到一半就没了。所以窗口只能由这四种方式关闭：「知道了」/「不再提醒」/ 关闭按钮 / ESC。

其中「打开日程表」和「暂停本日」**不会**关窗 —— 它们是辅助动作，你点完还要继续看当前这条说明。

### 已知限制

- 需要**电脑开机且已登录**才会弹；锁屏时不弹，关机期间的提醒不补播（系统机制，非故障）
- 上一个弹窗没关时，下一个时间点会跳过（避免屏幕上叠一摞窗口）
- 状态存在本地，**不做跨设备同步**

## 跨平台说明

| 环节 | Windows | macOS | Linux |
|---|---|---|---|
| 调度器 | 任务计划程序 | launchd | crontab |
| 弹窗载体 | WinForms 窗体 | `osascript display alert` | zenity → notify-send |
| 依赖 | PowerShell 5.1 + .NET | 系统自带 | zenity 或 notify-send |

macOS / Linux 的弹窗没有按钮（系统对话框能力有限），降级为纯文本展示，开关仍通过网页或命令行控制。

## 常见问题

**到点没弹？**

按顺序查：① 服务在跑吗（`service.py status`）② 今天生效吗（`days` 字段）③ 这条是 `manual` 吗 ④ 是不是在暂停期或被单独关掉了 ⑤ 看 `service.log`，所有跳过都会写明原因。

**改了文案没生效？**

- 改 `steps` / `trap` / `dose` / `cue`：立即生效，网页刷新即见
- 改时间点或星期：**不需要重装**，服务下一轮轮询即读到
- 改端口：需要重启服务

**想加一个「每周一三五才做」的环节？**

`days` 目前是整份计划共享的，无法给单个时间点单独指定。变通做法是拆成两份计划，各配一套服务（不同端口）。

**怎么彻底卸载？**

安装时会自动在你的配置目录里生成两个卸载入口：

```
uninstall-reminder.bat    双击即卸载
cleanup.ps1               它调用的清理脚本
```

**直接双击 `uninstall-reminder.bat`** 就能停掉服务并删除随开机启动的计划任务。

也可用命令行：

```bash
python scripts/service.py uninstall --plan plan.json   # 停服务 + 删任务
python scripts/service.py cleanup                      # 只清残留（技能删了也能用）
python scripts/service.py cleanup --dry-run            # 先预演，看会删什么
```

然后手动删掉 `plan.json` / `state.json` 所在目录即可。

> **为什么卸载入口要复制到你的目录里？**
> 因为卸载最容易被忽略的时间点是「你先删了技能目录、后来才想起卸载」。这时脚本已经没有了，系统里注册的计划任务就成了孤儿——**每次开机登录都会尝试启动一个不存在的脚本而报错**。
> 所以清理能力在安装时就复制了一份到你的配置目录，它不依赖技能是否还在。清理脚本按「任务动作里记录的脚本路径」来识别，所以哪怕技能目录已经删除，它照样能找到并清理干净。

## 许可

个人使用，自由取用。
