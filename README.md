# BSG 禅道助手

基于 Claude Code 的禅道数据查询和报告生成工具。

克隆此仓库后，在 Claude Code 中用自然语言直接操作禅道数据：

```
帮我出今天的平台项目日报
帮我出本周周汇总
现在线上有多少个Bug？
这个版本还有几天发布，有没有延期风险？
```

系统会记住你的身份和使用习惯，**越用越懂你**。

---

## 前提条件

- **Claude Code 已安装并登录**（必须）
- Python 3.11 或以上
- 网络能访问禅道内网（`https://cd.baa360.cc:20088`）

---

## 这个仓库包含什么

```
bsg-zentao/
│
│  ── 给 Claude Code 读的文件 ──────────────────────────────
├── CLAUDE.md              业务规则（Claude 靠这个理解禅道业务，生成准确报告）
├── bsg-zentao-api.skill   禅道接口规范 Skill（需安装到 Claude Code）
│
│  ── 给用户看的文件 ────────────────────────────────────────
├── README.md              本文件
├── requirements.txt       Python 依赖
├── .gitignore             排除敏感数据
│
│  ── 初始化 ────────────────────────────────────────────────
├── setup_config.py        首次运行：配置账号 + 个人知识库
│
│  ── MCP Server ────────────────────────────────────────────
├── mcp_server.py          Claude Code 调用工具的入口
│
│  ── 核心模块 ──────────────────────────────────────────────
├── bsg_zentao/
│   ├── client.py          禅道接口客户端（登录、请求、缓存）
│   ├── constants.py       业务常量（项目ID、部门映射、标签ID）
│   ├── user_knowledge.py  个人知识库（Profile + Memory 管理）
│   └── utils.py           工具函数（日期、文字处理）
│
│  ── 工具层 ────────────────────────────────────────────────
└── tools/
    ├── data_tools.py      原子数据工具（取版本、需求、Bug）
    ├── calc_daily.py      日报计算逻辑
    ├── calc_weekly.py     周汇总计算逻辑
    └── report_tools.py    报告数据组装（日报 + 周汇总）
```

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

### 第四步：安装 Skill 到 Claude Code

在 Claude Code 中输入：

```
请安装仓库里的 bsg-zentao-api.skill 文件
```

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

### 第四步：安装 Skill 到 Claude Code

在 Claude Code 中输入：

```
请安装仓库里的 bsg-zentao-api.skill 文件
```

### 第五步：初始化配置（账号 + 个人知识库）

```cmd
python setup_config.py
```

脚本会引导你完成两步配置：

**第一步 · 禅道账号**
输入你自己的禅道账号和密码，脚本自动验证。

**第二步 · 个人知识库**
按提示填写姓名、部门、角色、常用操作等信息，让 Claude 记住你是谁。

### 第六步：注册 MCP Server 到 Claude Code

在命令提示符中，先确认仓库的完整路径，然后运行：

```cmd
claude mcp add bsg-zentao python C:\Users\你的用户名\bsg-zentao\mcp_server.py
```

> 把路径替换成你实际的仓库位置，例如：
> `claude mcp add bsg-zentao python C:\Users\zhangsan\Desktop\bsg-zentao\mcp_server.py`

重启 Claude Code 后即可使用。

---

## 使用方式

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

- **效能周汇总**：含平台/游戏双项目完整数据
- **效能周报**：结论驱动，聚焦风险和决策

> ⚠️ 专题进展（AI伴侣/Web5/性能/招聘）和效能工作内容不来自禅道，
> Claude 会输出 `[待补充]` 占位符，需手动填写后再发送。

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

可随时更新姓名、部门、角色、关注项目、输出偏好等。

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

| 操作 | 说法 | 说明 |
|---|---|---|
| 只清记忆 | `清空记忆` | 只清系统自动记录的，保留手动添加的和 Profile |
| 清空全部记忆 | `清空我的知识库` | 清空 Memory 区，保留 Profile |
| 完全重置 | `完全重置知识库` | 清空记忆 + Profile，从头开始 |

---

## 当前支持的场景

| 场景 | 状态 | 触发方式 |
|---|---|---|
| 日报 | ✅ 可用 | "帮我出今天的日报" |
| 自由问答（版本/Bug/需求） | ✅ 可用 | 直接提问 |
| 效能周汇总 | ✅ 可用 | "帮我出本周周汇总" |
| 效能周报 | ✅ 可用 | "帮我出本周周报" |
| 个人知识库 | ✅ 可用 | "帮我配置知识库" / "查看我的知识库" |
| Bug界定 | 🔜 开发中 | — |
| 版本复盘 | 🔜 开发中 | — |

---

## 数据缓存

工具缓存当天的接口数据，同一天重复提问不会重复请求禅道，响应更快。

手动清除缓存（数据异常时使用）：

**Mac：**

```bash
rm -rf ~/.bsg-zentao/缓存/
```

**Windows：**

```cmd
rmdir /s /q %USERPROFILE%\.bsg-zentao\缓存
```

---

## 常见问题

**Q：提示「配置文件不存在」**
先运行 `python setup_config.py`（Windows）或 `python3 setup_config.py`（Mac）完成初始化。

**Q：提示「登录失败」**
检查账号密码是否正确，确认网络能访问禅道内网。

**Q：报告数据不对**
清除缓存后重新运行（命令见上方「数据缓存」章节）。

**Q：Windows 提示「python 不是内部命令」**
安装 Python 时没有勾选「Add Python to PATH」，重新安装并勾选该选项。

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

---

## 版本历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.2 | 2026-04-14 | 新增个人知识库（Profile + Memory），安装流程集成引导配置 |
| v1.1 | 2026-04-10 | 新增周汇总场景，补充 Windows 安装说明 |
| v1.0 | 2026-04-09 | 日报场景上线，含自由问答数据工具 |
