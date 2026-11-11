# dual-end-classroom

WorkBuddy skill：产出「**教师端 + 学生端 + 内网同步服务器**」的双端实时课堂系统。

一份源码 `app.src.html` 用角色标记 `#if T` / `#if S` 裁出两个 HTML，配套零依赖的
`sync_server.py`（静态托管 + SSE 下发 + POST 广播）与离线二维码编码器 `qr.js`。
**全程局域网、断外网可用，数据不经过任何第三方云。**

> 当前版本：**v4 · 2026-11-11**（v4 三件事：**加分独立成页** + 手机扫 `/m` 二维码当遥控器、大屏改**举手抢答**按先后编号、学生端**本课任务**弹窗；v3.1 小组积分：全员通关 10/7/5 + 整组过关每关 +2 + 教师分；v3 学情 AI 分析：12 家厂商、默认 DeepSeek、填个 Key 就能用）

---

## ⚠️ 先读：这是「技能定义」，不是独立软件

clone 下来**不能直接双击运行**。本技能是**一份可复用的骨架 + 构建脚本 + 验收脚本 +
踩坑清单**，真正的产物要按下面的「快速开始」构建出来。

| 依赖 | 说明 | 必需 |
|---|---|---|
| **WorkBuddy** | 技能加载机制（`~/.workbuddy/skills/`） | 推荐 |
| **Python** ≥ 3.8 | 跑 `build_dual.py` / `verify.py` / `sync_server.py`（纯标准库，无 pip 依赖） | ✅ 必需 |
| **Node**（可选） | `verify.py` 用它做 `node --check` 语法检查；没有就跳过 | 可选 |
| **Edge / Chrome**（可选） | `e2e_ui.py` 真浏览器端到端测试 | 可选 |

## 安装

```bash
git clone https://github.com/jiashiqing/dual-end-classroom.git
```

把整个目录放进用户级技能目录（Windows 示例）：

```
C:\Users\<你的用户名>\.workbuddy\skills\dual-end-classroom\
```

确保 `SKILL.md` 位于该目录根下（同级应看到 `assets/`、`references/`、`scripts/`）。

## 快速开始

```bash
# 1) 起课：拷骨架 + 换内容（改 CONFIG.LEVELS / CONFIG.MINS / content(lv)）
cp assets/app.src.html assets/qr.js  <你的项目>/

# 2) 构建：出教师端 + 学生端 + 服务器 + qr.js
python scripts/build_dual.py <你的项目>/app.src.html --out <你的项目>/dist --name 我的课堂

# 3) 验收：三条基础 + 三条专项都绿再交付
python scripts/verify.py <你的项目>/dist
python scripts/smoke_sync.py --dir <你的项目>/dist --reclaim
python scripts/e2e_ui.py --dir <你的项目>/dist      # 需 Edge
python scripts/probe_ai.py --dir <你的项目>/dist        # 学情 AI（自带假模型）
python scripts/probe_score.py --dir <你的项目>/dist     # 小组积分
python scripts/probe_taskscore.py --dir <你的项目>/dist # 加分页 / 手机加分台 / 抢答 / 任务

# 4) 上课
双击 dist/start_server.bat
教师机开 http://127.0.0.1:8099/t  →  控制台·课前设置 里有课堂码和二维码
学生机开 http://本机IP:8099/s 或直接扫码
```

## 目录

```
├── SKILL.md                    技能定义（何时用/工作流/硬前提/硬性规则/验收清单）
├── assets/
│   ├── app.src.html            骨架源码（两端合一 + 角色标记，v4）
│   ├── mobile_score.html       手机加分台（构建时改名「加分台.html」，GET /m 直出）
│   ├── qr.js                   离线二维码编码器（零依赖，13 KB）
│   ├── sync_server.py          内网同步服务器（零依赖；Range / netinfo / /api/llm 大模型转发 / 端口顺延）
│   └── start_server.bat        双击启动（ASCII + CRLF）
├── references/
│   ├── 01-双端架构.md           一份源码两个产物、页签顺序、CONFIG vs cfg、状态放哪
│   ├── 02-同步协议与服务端.md    消息格式、本班/大厅双频道、ping 保活、八个端点
│   ├── 03-课堂管控组件.md        二维码/分组/每关时长/积分(含手机端)/抢答/任务/进度墙/看板/证书门闩
│   ├── 04-踩坑清单.md           现象 → 原因 → 解法（A 架构 / B 同步 / C 页面 / D 交付）
│   ├── 05-交付与验收.md         脚本参数与输出解读、人工清单、故障对照
│   └── 06-评分规则模板.md       积分规则说明模板（发给老师/学生，交付时随项目给一份）
└── scripts/
    ├── build_dual.py           源码 → 两个角色文件（并复制 qr.js / 服务器）
    ├── verify.py               静态体检（角色隔离 / 外链 / 语法 / 标记残留）
    ├── smoke_sync.py           协议冒烟（广播 / 跨频道隔离 / 掉线回收）
    ├── e2e_ui.py               真浏览器报到 → 答题 → 上报
    ├── probe_ai.py             学情 AI 分析（自带假模型，不吃真 Key）
    ├── probe_score.py          小组积分（分值 / 幂等 / 两榜 / 清空）
    └── probe_taskscore.py      加分独立页 / 手机加分台 / 抢答 / 本课任务
```

## 这个技能解决什么

| 教室里的真实问题 | 骨架里的对应设计 |
|---|---|
| 各校机房分辨率参差、还常断外网 | 全离线、1366×768 起步、零外链 |
| 学生输网址太慢 | 控制台生成**离线二维码**，扫码直进学生端（码里带组数 `?g=`） |
| 各班分组数不一样 | 分组数 2–12 当配置，联动加分卡 / 进度墙 / 学情；报到页经「大厅」频道实时跟变 |
| 每关难度不同，时长不能一刀切 | `cfg.mins[]` 每关独立，只在真换关时重置倒计时 |
| 课上点加分要点两层菜单，还挡住逐关开关 | **加分独立成页签**；右栏二维码，老师手机扫进来当遥控器（手机只发意图，记账永远在教师机） |
| 举手了不知道谁先举的 | 大屏**举手抢答**队列：按第一次举手的时间排队，名字前挂 1/2/3；老师点一下就算答过 |
| 学生不知道这节课要干什么 | 老师发布**本课任务** → 学生进课堂弹一次 → 关掉后顶栏「任务」随时再看 |
| 投屏只看得见进度，说不清谁卡在哪 | 大屏进度墙 + 在线/已通关/平均关数/**当前领先** + 全屏详情看板（挂舞台根节点，Esc 可关） |
| 「哪个组最好」凭印象，学生不服 | **小组积分**：冲线 10/7/5（第 4 个起 3）+ 整组过关每关 +2 + 教师分；大屏实时榜 + 进度墙积分列，下课导出评优表 |
| 学生没通关就想领证书 | 证书门闩两道（`locked` 压暗 + 点击拦截） |
| 下课后只知道「第 3 关只剩 8 人」，不知道补什么 | 学情页 **AI 分析**：12 家模型任选、默认 DeepSeek、填个 Key 就出课后分析（流式） |

## 许可

内部教研用途。骨架与脚本可自由改造；`qr.js` 为自带实现的 QR 编码器（byte 模式 / RS 纠错 / 8 掩码罚分择优）。
