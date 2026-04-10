# BSG 禅道助手

基于 Claude Code 的禅道数据查询和报告生成工具。

克隆此仓库后，在 Claude Code 中用自然语言直接操作禅道数据：

```
帮我出今天的平台项目日报
现在线上有多少个Bug？
这个版本还有几天发布，有没有延期风险？
下一版本哪些需求还没下单？
```

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
├── setup_config.py        首次运行，配置禅道账号密码
│
│  ── MCP Server ────────────────────────────────────────────
├── mcp_server.py          Claude Code 调用工具的入口
│
│  ── 核心模块 ──────────────────────────────────────────────
├── bsg_zentao/
│   ├── client.py          禅道接口客户端（登录、请求、缓存）
│   ├── constants.py       业务常量（项目ID、部门映射）
│   └── utils.py           工具函数（日期、文字处理）
│
│  ── 工具层 ────────────────────────────────────────────────
└── tools/
    ├── data_tools.py      原子数据工具（取版本、需求、Bug）
    ├── calc_daily.py      日报计算逻辑
    └── report_tools.py    报告数据组装
```

**以上所有文件进 git，团队共享。**

以下内容在用户本机自动生成，**不进 git**：

```
~/.bsg-zentao/
├── config.json    账号密码（仅本机可读）
├── 缓存/          接口数据缓存（当天有效）
└── 报告/
    ├── 日报/
    ├── 周汇总/
    ├── Bug界定/
    └── 版本复盘/
```

---

## 安装步骤

### 第一步：克隆仓库

```bash
git clone https://github.com/sssguoqiang-art/bsg-zentao.git
cd bsg-zentao
```

### 第二步：安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 第三步：安装 Skill 到 Claude Code

`bsg-zentao-api.skill` 是禅道接口调用规范，安装后 Claude Code 在处理任何禅道相关任务时会自动参考，确保接口字段准确无误。

在 Claude Code 中输入：

```
请安装仓库里的 bsg-zentao-api.skill 文件
```

或在终端中直接运行：

```bash
claude /path/to/bsg-zentao/bsg-zentao-api.skill
```

### 第四步：配置禅道账号

```bash
python setup_config.py
```

按提示输入禅道账号密码，脚本自动验证登录并保存到本机 `~/.bsg-zentao/config.json`。

### 第五步：注册 MCP Server 到 Claude Code

```bash
claude mcp add bsg-zentao python ~/bsg-zentao/mcp_server.py
```

> 路径根据你实际克隆的位置调整，例如克隆到桌面则为：
> `claude mcp add bsg-zentao python ~/Desktop/bsg-zentao/mcp_server.py`

---

## 使用方式

注册完成后，在 Claude Code 中直接用中文提问：

**生成报告：**
```
帮我出今天的平台项目日报
```

**查询数据：**
```
平台项目当前版本还有几天发布？
线上现在有多少个活跃Bug？
这个版本有哪些需求还没下单？
有没有延期超过3天的任务？
```

**综合分析：**
```
这个版本交付有风险吗？
```

Claude Code 会自动判断需要哪些数据，调用对应工具，用中文回答或生成报告。

生成的报告自动保存到 `~/.bsg-zentao/报告/日报/YYYYMMDD_日报.md`。

---

## 关于 bsg-zentao-api.skill

这是禅道接口调用规范的 Skill 文件，记录了所有接口的字段映射、枚举值和调用陷阱，由浏览器逐个接口实测后沉淀而成。

**为什么要安装**：禅道接口字段多、部分字段行为与文档不符，Skill 是已验证的可信规范。Claude Code 处理禅道相关任务时会自动加载，确保生成代码和逻辑与实际接口一致。

**为什么跟代码放一起**：接口会随禅道升级变化。Skill 和代码同仓库管理，接口变更时一起更新，始终保持对齐。

---

## 数据缓存

工具缓存当天的接口数据，同一天重复提问不会重复请求禅道接口，响应更快。

手动清除缓存（数据异常时使用）：

```bash
rm -rf ~/.bsg-zentao/缓存/
```

---

## 常见问题

**Q：提示"配置文件不存在"**
先运行 `python setup_config.py` 完成初始化。

**Q：提示"登录失败"**
检查账号密码是否正确，确认网络能访问禅道内网。

**Q：报告数据不对**
删除缓存后重新运行：`rm -rf ~/.bsg-zentao/缓存/`

**Q：想重新配置账号**
重新运行 `python setup_config.py`，选 `y` 覆盖现有配置。

**Q：接口字段变了怎么办**
脚本中的字段探针会自动告警，告知维护者更新 `bsg-zentao-api.skill` 和代码即可，然后重新推到 git，用户重新 pull 即可同步。

---

## 当前支持的场景

| 场景 | 状态 | 触发方式 |
|---|---|---|
| 日报 | ✅ 可用 | "帮我出今天的日报" |
| 自由问答（版本/Bug/需求） | ✅ 可用 | 直接提问 |
| 周汇总 | 🔜 开发中 | — |
| Bug界定 | 🔜 开发中 | — |
| 版本复盘 | 🔜 开发中 | — |

---

## 版本历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-04-09 | 日报场景上线，含自由问答数据工具 |
