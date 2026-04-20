# BSG 禅道助手

基于 Claude Code 的禅道数据查询和报告生成工具，也支持在 Codex 中使用。

克隆此仓库后，在 Claude Code 或 Codex 中用自然语言直接操作禅道数据：

```
帮我出今天的平台项目日报
帮我出本周周汇总
帮我出Bug界定报告
帮我出版本复盘
现在线上有多少个Bug？
这个版本还有几天发布，有没有延期风险？
```

系统会记住你的身份和使用习惯，**越用越懂你**。

---

## 前提条件

- **Claude Code 或 Codex 已安装并登录**（至少其一）
- Python 3.11 或以上
- 网络能访问禅道内网（`https://cd.baa360.cc:20088`）

---

## 这个仓库包含什么

```
bsg-zentao/
│
│  ── 给 AI Agent 读的文件 ────────────────────────────────────────
├── CLAUDE.md              业务规则（Claude Code 默认读取）
├── AGENTS.md              Codex 工作指引（Codex 默认读取，并指向 CLAUDE.md）
├── bsg-zentao-api.skill   禅道接口规范 Skill（需安装到 Claude Code / Codex）
│
│  ── 给用户看的文件 ────────────────────────────────────────────────
├── README.md              本文件
├── requirements.txt       Python 依赖
├── .gitignore             排除敏感数据
│
│  ── 初始化 ────────────────────────────────────────────────────────
├── setup_config.py        首次运行：配置账号 + 个人知识库
│
│  ── MCP Server ────────────────────────────────────────────────────
├── mcp_server.py          Claude Code 调用工具的入口
│
│  ── 核心模块 ──────────────────────────────────────────────────────
├── bsg_zentao/
│   ├── client.py          禅道接口客户端（登录、请求、缓存）
│   ├── constants.py       业务常量（项目ID、部门映射、标签ID）
│   ├── user_knowledge.py  个人知识库（Profile + Memory 管理）
│   └── utils.py           工具函数（日期、文字处理）
│
│  ── 工具层 ────────────────────────────────────────────────────────
└── tools/
    ├── data_tools.py          原子数据工具（取版本、需求、Bug）
    ├── calc_daily.py          日报计算逻辑
    ├── calc_weekly.py         周汇总计算逻辑
    ├── calc_bug_review.py     Bug 界定预分类计算逻辑
    ├── calc_review.py         版本复盘计算逻辑
    ├── report_tools.py        日报数据组装
    └── report_tools_review.py 版本复盘数据组装
```

## Claude / Codex 默认文件说明

- `CLAUDE.md`：给 Claude Code 用的默认项目规则文件
- `AGENTS.md`：给 Codex 用的默认项目规则文件
- 本仓库会同时保留两份文件：`CLAUDE.md` 不动，`AGENTS.md` 作为 Codex 入口，并在需要时引用 `CLAUDE.md` 中的业务规则
- `bsg-zentao-api.skill` 可安装到 Claude Code 或 Codex，供两边统一使用禅道接口规范

以下内容在用户本机自动生成，**不进 git**：

```
~/.bsg-zentao/
├── config.json      账号密码（仅本机可读）
├── profile.json     个人知识库 Profile（身份与偏好配置）
├── memory.jsonl     个人知识库 Memory（对话习惯积累）
├── 缓存/            接口数据缓存（当天有效）
└── 报告/
    ├── 日报/
    ├── 周汇总/
    ├── Bug界定/
    └── 版本复盘/
```

---

## 安装步骤（Mac）

### 第一步：安装 Python 3.11+

打开「终端」，检查当前版本：

```bash
python3 --version
```

如果版本低于 3.11，用 Homebrew 安装新版本：

```bash
# 没有 Homebrew 的先安装
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 Python 3.12
brew install python@3.12
```

### 第二步：克隆仓库

```bash
git clone https://github.com/sssguoqiang-art/bsg-zentao.git
cd bsg-zentao
```

### 第三步：安装 Python 依赖

```bash
pip3 install -r requirements.txt
```

### 第四步：安装 Skill 到 AI 助手

在 Claude Code 或 Codex 中输入：

```
请安装仓库里的 bsg-zentao-api.skill 文件
```

如果你使用 Codex，安装完成后请重启 Codex；打开仓库根目录时，Codex 会自动读取 `AGENTS.md`。

### 第五步：初始化配置（账号 + 个人知识库）

```bash
python3 setup_config.py
```

脚本会引导你完成两步配置：

**第一步 · 禅道账号**
输入你的禅道账号和密码，脚本自动验证并保存到本机。

**第二步 · 个人知识库**
按提示填写姓名、部门、角色、常用操作等信息。系统会记住这些内容，让 Claude 在每次对话中自动了解你是谁、你关注什么。填写越完整，使用体验越好。

> 所有信息仅保存在本机，不会上传到任何地方。

### 第六步：注册 MCP Server 到 Claude Code

```bash
claude mcp add bsg-zentao python3 ~/bsg-zentao/mcp_server.py
```

> 路径根据你实际克隆的位置调整，例如克隆到桌面：
> `claude mcp add bsg-zentao python3 ~/Desktop/bsg-zentao/mcp_server.py`

重启 Claude Code 后即可使用。

### Codex 用户补充说明

- Codex 默认读取仓库根目录的 `AGENTS.md`
- `CLAUDE.md` 仍保留给 Claude Code 使用，不需要删除或改名
- 如果你也想在 Codex 中接入 `mcp_server.py`，请按你本机 Codex 的 MCP 配置方式注册该脚本

---

## 安装步骤（Windows）

### 第一步：安装 Python 3.11+

1. 打开浏览器，访问 [python.org/downloads](https://www.python.org/downloads/)
2. 下载最新的 Python 3.12 Windows 安装包
3. 运行安装包，**勾选「Add Python to PATH」**（重要，否则后续命令无法识别）
4. 点击「Install Now」完成安装

安装完成后，打开「命令提示符」（Win+R，输入 `cmd`）验证：

```cmd
python --version
```

### 第二步：克隆仓库

```cmd
git clone https://github.com/sssguoqiang-art/bsg-zentao.git
cd bsg-zentao
```

> 没有 git 的话，先到 [git-scm.com](https://git-scm.com/download/win) 下载安装。
> 或者直接在 GitHub 页面点「Code → Download ZIP」解压也可以。

### 第三步：安装 Python 依赖

```cmd
pip install -r requirements.txt
```

### 第四步：安装 Skill 到 AI 助手

在 Claude Code 或 Codex 中输入：

```
请安装仓库里的 bsg-zentao-api.skill 文件
```

如果你使用 Codex，安装完成后请重启 Codex；打开仓库根目录时，Codex 会自动读取 `AGENTS.md`。

### 第五步：初始化配置（账号 + 个人知识库）

```cmd
python setup_config.py
```

### 第六步：注册 MCP Server 到 Claude Code

```cmd
claude mcp add bsg-zentao python C:\Users\你的用户名\bsg-zentao\mcp_server.py
```

重启 Claude Code 后即可使用。

### Codex 用户补充说明

- Codex 默认读取仓库根目录的 `AGENTS.md`
- `CLAUDE.md` 仍保留给 Claude Code 使用，不需要删除或改名
- 如果你也想在 Codex 中接入 `mcp_server.py`，请按你本机 Codex 的 MCP 配置方式注册该脚本

---

## 使用方式

### 典型工作节奏

```
周一        周二～三      周三晚发版     周四/五复盘会
  │            │              │               │
日报 ────── 日报 ──────── Bug界定 ──────── 版本复盘
              │
           周汇总（周四管理会议前）
```

### 日报

```
帮我出今天的平台项目日报
帮我出今天的游戏项目日报
```

生成的日报自动保存到 `~/.bsg-zentao/报告/日报/YYYYMMDD_日报.md`。

### 周汇总（每周管理会议）

```
帮我出本周周汇总
```

一次性生成两份报告：

- **效能周汇总**：含平台/游戏双项目完整数据，面向各部门负责人
- **效能周报**：结论驱动，聚焦风险和决策，面向管理层

> ⚠️ 专题进展（AI伴侣/Web5/性能/招聘）和效能工作内容不来自禅道，
> Claude 会输出 `[待补充]` 占位符，需手动填写后再发送。

### Bug 界定（周三发版后）

在版本发布后（通常**周三晚**）运行，为周四/五复盘会做准备。帮你在开会前就知道：哪些Bug值得复盘、各Bug归属是什么、哪些可能有争议、哪些任务提测质量有问题。

```
帮我出Bug界定报告
出预分类
Bug界定一下
```

**报告内容（自动生成）：**

- 各部门Bug数量总览
- 疑似非Bug清单（type=performance，建议人工确认后排除）
- 外部Bug责任界定：每条Bug附 Bug现象 / 影响范围 / 可能原因 / 复盘建议
- 内部Bug责任界定：每条Bug附 Bug现象 / 可能原因 / 预测争议点
- 低质量任务分析：多维识别，区分「提测质量差」和「需求质量差」两种根因

**复盘建议**字段直接告诉你每条Bug是「建议复盘」「需会前确认」还是「复盘价值有限」，不需要逐条分析。

报告自动保存到 `~/.bsg-zentao/报告/Bug界定/`。

### 版本复盘（周四/五复盘会）

在复盘会前或复盘会上生成正式复盘文档，覆盖外部Bug、内部Bug、版本交付情况的完整分析。

```
帮我出版本复盘
生成复盘报告
出复盘
```

**自动生成的内容：**

- 一、外部Bug复盘：历史趋势折线图数据 / 非Bug剔除列表 / 实际复盘Bug列表 / 测试组责任归属 / 各部门Bug分布 / 每条Bug深度分析（现象、溯源、各部门原因+举措）/ 核心管理问题汇总
- 二、内部Bug复盘：历史趋势 / 极高缺陷分布 / 高缺陷Bug类型分析 / 复盘Bug列表 / 每条Bug深度分析 / 低质量任务 / 测试组不可容忍Bug类型
- 三、版本复盘：版本需求历史趋势

**需要人工补充的内容（报告内已标注 `【待补充·人工】`）：**

- 各条内部Bug的Bug现象（禅道接口无法直接取到）
- 3.2 过程延期部门分布和总数
- 3.3 延期任务明细记录
- 3.4 上次复盘待办项跟进
- 复盘时间

> ℹ️ 版本复盘会调用 Claude API 为每条Bug自动生成一句直观描述，在 Claude Code 中运行时 API Key 通常已自动可用。若不可用，自动退回规则推断，报告照样生成。

报告自动保存到 `~/.bsg-zentao/报告/版本复盘/`。

---

> ⚠️ **Bug界定 ≠ 版本复盘，是两件完全不同的事**
>
> | | Bug界定 | 版本复盘 |
> |---|---|---|
> | 运行时机 | 周三发版后 | 周四/五复盘会前后 |
> | 目的 | 判断哪些Bug值得复盘、归属预判 | 生成复盘会正式展示文档 |
> | 包含趋势分析 | ✗ | ✓ |
> | 需要人工补充 | 少量 | 延期记录、内部Bug现象等 |
> | 触发词 | Bug界定 / 预分类 / 出界定报告 | 版本复盘 / 出复盘 / 复盘报告 |
>
> 直接说「复盘」时，Claude Code 会先问你要哪个。

### 自由查询

```
平台项目当前版本还有几天发布？
线上现在有多少个活跃Bug？
这个版本有哪些需求还没下单？
这个版本交付有风险吗？
```

---

## 个人知识库

系统会记住你的使用习惯，让每次对话越来越顺手。

### 查看知识库

```
查看我的知识库
看看你记住了什么
知识库里有什么
```

### 修改个人配置（Profile）

```
帮我配置知识库
帮我更新知识库
修改我的配置
重新配置
```

### 记忆管理（Memory）

对话中 Claude 发现值得记住的习惯时，会主动提示：

```
💡 我注意到一个可以记住的习惯：
   「你查 Bug 时通常只看 PHP1组 + 本版本范围」

   回复「记住」确认 / 「不用」跳过 / 「改成[内容]」修改后记住
```

你也可以主动说：

```
帮我记住：我一般只看游戏项目的数据
```

### 重置知识库

| 操作     | 说法        | 说明                         |
| ------ | --------- | -------------------------- |
| 只清记忆   | `清空记忆`    | 只清系统自动记录的，保留手动添加的和 Profile |
| 清空全部记忆 | `清空我的知识库` | 清空 Memory 区，保留 Profile     |
| 完全重置   | `完全重置知识库` | 清空记忆 + Profile，从头开始        |

---

## 当前支持的场景

| 场景              | 状态   | 触发方式                              | 报告保存位置           |
| --------------- | ---- | --------------------------------- | ---------------- |
| 日报              | ✅ 可用 | "帮我出今天的日报"                        | 报告/日报/           |
| 自由问答（版本/Bug/需求） | ✅ 可用 | 直接提问                              | —                |
| 效能周汇总           | ✅ 可用 | "帮我出本周周汇总"                        | 报告/周汇总/          |
| 效能周报            | ✅ 可用 | "帮我出本周周报"                         | 报告/周汇总/          |
| 个人知识库           | ✅ 可用 | "帮我配置知识库" / "查看我的知识库"             | —                |
| Bug界定           | ✅ 可用 | "帮我出Bug界定报告" / "出预分类"             | 报告/Bug界定/        |
| 版本复盘            | ✅ 可用 | "帮我出版本复盘" / "生成复盘报告"              | 报告/版本复盘/         |

---

## 数据缓存

工具默认使用短 TTL 缓存，兼顾速度和实时性：

- `版本列表`：约 2 分钟
- `需求池`：约 1 分钟
- `Bug 数据`：约 30 秒
- `按人查任务`：当天范围约 1 分钟，历史范围更久

同一会话里连续追问时，会优先命中进程内缓存；进程重启后，如磁盘缓存仍在 TTL 内，也会复用。缓存过期后会自动重新请求禅道，不会整天都用旧数据。

如果你明确说“刷新一下”“拉最新”“重新获取最新”，Claude/Codex 会传 `force_refresh=true`，直接跳过短缓存。

当禅道暂时不可达时，系统会自动降级到当天的本地缓存，保证基础可用。

缓存文件保存在项目目录下，可直接查看：

```
bsg-zentao/缓存/
└── 20260414_版本列表_undone.json
└── 20260414_需求池_394.json
└── 20260414_Bug数据_394.json
```

手动清除缓存（数据异常时使用）：

**Mac：**

```bash
rm -rf ~/你的仓库路径/bsg-zentao/缓存/
```

**Windows：**

```cmd
rmdir /s /q 你的仓库路径\bsg-zentao\缓存
```

> ⚠️ 缓存文件不进 git，.gitignore 已排除。

---

## 常见问题

**Q：提示「配置文件不存在」**
先运行 `python3 setup_config.py`（Mac）或 `python setup_config.py`（Windows）完成初始化。

**Q：提示「登录失败」**
检查账号密码是否正确，确认网络能访问禅道内网。

**Q：报告数据不对**
先说“刷新一下”或“重新拉最新”；如果仍异常，再清除缓存后重试（命令见上方「数据缓存」章节）。

**Q：Bug界定 和 版本复盘 有什么区别？**
Bug界定是复盘会前的预分类准备材料（周三出），帮你判断哪些Bug值得复盘；版本复盘是复盘会上展示的正式报告（含趋势、深度分析）。直接说「复盘」时，Claude Code 会先问你要哪个。

**Q：版本复盘生成后还需要做什么？**
报告中标注了 `【待补充·人工】` 的部分需要手动填写，主要是：各条内部Bug现象（禅道接口无法直接取到）、延期任务明细记录、上次复盘待办项跟进、复盘时间。其余内容（Bug趋势、深度分析、各部门原因举措、低质量任务等）均自动生成。

**Q：Bug界定报告里「复盘价值有限」是什么意思？**
说明这条Bug从数据信号判断可能是优化项、配置问题或已转需求，复盘意义不大。最终是否排除由你决定，报告只是给出预判。

**Q：周汇总的专题内容是空的**
正常现象，Claude 会输出 `[待补充]` 占位符，手动填入后再发送。

**Q：如何修改已填的个人配置？**
在 Claude Code 中说「修改我的配置」，或重新运行 `python3 setup_config.py`。

**Q：工具有更新怎么同步**

Mac：

```bash
cd bsg-zentao
git pull
pip3 install -r requirements.txt
```

Windows：

```cmd
cd bsg-zentao
git pull
pip install -r requirements.txt
```

**Q：为什么仓库里同时有 `CLAUDE.md` 和 `AGENTS.md`？**
`CLAUDE.md` 给 Claude Code 用，`AGENTS.md` 给 Codex 用。两者可以同时存在：Claude 继续读 `CLAUDE.md`，Codex 读取 `AGENTS.md`，而 `AGENTS.md` 会把 Codex 引导到这套现有业务规则上。

---

## 版本历史

| 版本   | 日期         | 说明                                                                                         |
| ---- | ---------- | -------------------------------------------------------------------------------------------- |
| v1.4 | 2026-04-14 | Bug界定分析引擎升级：Bug现象推断、争议预测（数据信号驱动）、低质量任务多维根因分类；明确 Bug界定 vs 版本复盘消歧规则 |
| v1.3 | 2026-04-14 | Bug界定场景上线：新增 `calc_bug_review.py`，修正非Bug识别逻辑、争议关键词扩展、禁止 deptName 回落           |
| v1.2 | 2026-04-14 | 新增个人知识库（Profile + Memory），安装流程集成引导配置；版本复盘场景上线                                |
| v1.1 | 2026-04-10 | 新增周汇总场景，补充 Windows 安装说明                                                           |
| v1.0 | 2026-04-09 | 日报场景上线，含自由问答数据工具                                                                 |
