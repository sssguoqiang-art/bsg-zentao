# BSG 禅道助手

基于 Claude Code 的禅道数据查询和报告生成工具。

用自然语言与 Claude Code 对话，直接获取禅道数据和报告：

```
帮我出今天的平台项目日报
现在线上有多少个Bug？
这个版本还有几天发布，有没有延期风险？
下一版本哪些需求还没下单？
```

---

## 这个仓库包含什么

```
bsg-zentao/
│
│  ── 给 Claude 读的文件 ──
├── CLAUDE.md              业务规则（Claude 靠这个理解禅道业务，生成准确报告）
├── bsg-zentao-api.skill   禅道接口规范（Claude Code 在接口相关任务时自动调用）
│
│  ── 给用户看的文件 ──
├── README.md              本文件，安装和使用说明
├── requirements.txt       Python 依赖
├── .gitignore             排除敏感数据
│
│  ── 初始化脚本 ──
├── setup_config.py        首次运行，配置禅道账号密码
│
│  ── MCP Server ──
├── mcp_server.py          Claude Code 调用工具的入口
│
│  ── 核心模块 ──
├── bsg_zentao/
│   ├── client.py          禅道接口客户端（登录、请求、缓存）
│   ├── constants.py       业务常量（项目ID、部门映射、状态定义）
│   └── utils.py           工具函数（日期、文字处理）
│
│  ── 工具层 ──
└── tools/
    ├── data_tools.py      原子数据工具（取版本、取需求、取Bug）
    ├── calc_daily.py      日报计算逻辑（7个板块的业务计算）
    └── report_tools.py    报告数据组装（交给 Claude 生成正文）
```

**以上所有文件都进 git，团队共享。**

以下内容在用户本机自动生成，**不进 git**：
```
~/.bsg-zentao/
├── config.json    账号密码（仅本机可读）
├── 缓存/          接口数据缓存（当天有效）
└── 报告/          生成的报告文件
    ├── 日报/
    ├── 周汇总/
    ├── Bug界定/
    └── 版本复盘/
```

---

## 关于 bsg-zentao-api.skill

`bsg-zentao-api.skill` 是禅道接口调用规范的 Claude Skill 文件。

**它是什么**：记录了禅道所有接口的字段映射、枚举值、调用陷阱和注意事项，
是通过浏览器逐个接口实测后沉淀的可信文档。

**它做什么**：当 Claude Code 需要处理任何与禅道接口相关的任务时（包括调试脚本、
新增功能、排查数据异常），会自动调用此 Skill 获取接口规范，确保生成的代码和逻辑与实际接口一致。

**为什么放进 git**：接口会随禅道版本升级而变化。把 Skill 和代码放在一起，
每次接口变更时同步更新 Skill，保证规范和代码始终对齐。

**安装方式**：
1. 打开 [claude.ai](https://claude.ai)
2. 进入「Settings → Skills」
3. 上传 `bsg-zentao-api.skill` 文件

安装后，所有对话中涉及禅道接口的问题，Claude 都会自动参考此规范。

---

## 环境要求

- Python 3.11 或以上
- [Claude Code](https://claude.ai/code) 已安装并登录
- 网络能访问禅道内网（`https://cd.baa360.cc:20088`）

---

## 安装步骤

**第一步：克隆仓库**

```bash
git clone <仓库地址>
cd bsg-zentao
```

**第二步：安装 Python 依赖**

```bash
pip install -r requirements.txt
```

**第三步：安装 Skill（可选，但推荐）**

在 claude.ai → Settings → Skills 上传 `bsg-zentao-api.skill`。

**第四步：初始化禅道账号配置**

```bash
python setup_config.py
```

按提示输入禅道账号密码，脚本自动验证登录并保存到本机。
完成后会显示下一步命令。

**第五步：注册到 Claude Code**

```bash
claude mcp add bsg-zentao python /你的完整路径/bsg-zentao/mcp_server.py
```

完成后重启 Claude Code，即可开始使用。

---

## 使用方式

直接在 Claude Code 中用中文提问：

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

Claude 会自动判断需要哪些数据，调用对应工具，用中文回答。

---

## 数据缓存说明

工具会缓存当天的接口数据，同一天内重复提问不会重复请求禅道接口，响应更快。

缓存存放在 `~/.bsg-zentao/缓存/`，按日期自动隔离，次日自动重新拉取。

手动清除缓存（数据不对时使用）：
```bash
rm -rf ~/.bsg-zentao/缓存/
```

---

## 常见问题

**Q：提示"配置文件不存在"**
先运行 `python setup_config.py` 完成初始化。

**Q：提示"登录失败"**
检查账号密码是否正确，确认当前网络能访问禅道内网。

**Q：报告数据为空或不正常**
删除缓存后重新运行：`rm -rf ~/.bsg-zentao/缓存/`

**Q：想重新配置账号密码**
重新运行 `python setup_config.py`，选择 `y` 覆盖现有配置。

**Q：接口字段变了怎么办**
禅道升级后如果字段有变化，代码中的字段探针（`schema_probe.py`）会自动告警。
告知维护者更新 `bsg-zentao-api.skill` 和相关代码即可。

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
