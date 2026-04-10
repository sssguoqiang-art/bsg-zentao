# BSG 禅道助手

基于 Claude Code 的禅道数据查询和报告生成工具。

安装完成后，直接用中文跟 Claude Code 对话：

```
帮我出今天的平台项目日报
现在线上有多少个Bug？
这个版本还有几天发布，有没有延期风险？
```

---

## 使用前提

以下三项需要提前满足：

- ✅ **Claude Code 已安装并登录**
- ✅ **Python 3.10 或以上版本**（见下方安装说明）
- ✅ **电脑网络能访问禅道内网**（`https://cd.baa360.cc:20088`）

---

## 第一次使用（新用户安装）

> 只需要做一次，后续不用重复。

### 步骤一：安装 Python 3.12

macOS 系统自带的 Python 版本太旧（3.9），需要先安装新版本。

打开 Mac 的「终端」应用（在 Spotlight 搜索「终端」即可找到），复制粘贴以下命令运行：

```bash
# 第一行：安装 Homebrew（Mac 上的软件管理工具，如果已有可跳过）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 第二行：用 Homebrew 安装 Python 3.12
brew install python@3.12
```

### 步骤二：下载工具代码

在终端运行：

```bash
git clone https://github.com/sssguoqiang-art/bsg-zentao.git
cd bsg-zentao
```

这会把代码下载到当前目录下的 `bsg-zentao` 文件夹。

### 步骤三：安装依赖

```bash
python3.12 -m pip install -r requirements.txt
```

### 步骤四：安装 Skill 到 Claude Code

Skill 是工具的「接口规范文件」，安装后 Claude Code 才能正确理解禅道的数据。

在终端运行：

```bash
# 进入代码目录（如果还没进入的话）
cd bsg-zentao

# 安装 Skill
claude skill install bsg-zentao-api.skill
```

### 步骤五：配置禅道账号

```bash
python3.12 setup_config.py
```

按提示输入你的禅道账号和密码。
- 密码输入时不会显示字符，直接输入后回车即可
- 脚本会自动验证账号是否正确
- 账号密码只保存在你自己电脑上，不会上传到任何地方

### 步骤六：注册工具到 Claude Code

```bash
claude mcp add bsg-zentao python3.12 $(pwd)/mcp_server.py
```

> 注意：`$(pwd)` 会自动填入当前目录路径，在 `bsg-zentao` 文件夹内运行才正确。

---

完成以上六步后，**重启 Claude Code**，即可开始使用。

---

## 后续使用（已安装用户）

> 工具安装好之后，日常使用只需打开 Claude Code 直接提问，无需任何操作。

---

## 工具有更新时（同步最新版本）

当维护者更新了工具代码后，你需要同步更新。

在终端进入代码目录，运行以下命令：

```bash
# 进入代码目录
cd bsg-zentao

# 拉取最新代码
git pull

# 如果依赖有变化，重新安装
python3.12 -m pip install -r requirements.txt
```

> **什么时候需要更新？**
> 维护者会通知你，或者你发现工具数据不对时，可以运行上面的命令同步一次。

---

## 禅道账号变更时

如果你需要更换禅道账号或密码，在终端运行：

```bash
cd bsg-zentao
python3.12 setup_config.py
```

按提示选择「重新配置」，输入新的账号密码即可。旧的账号信息会被覆盖。

---

## 这个仓库包含什么

```
bsg-zentao/
│
│  ── 给 Claude Code 读的文件 ──
├── CLAUDE.md              业务规则（Claude 靠这个理解禅道业务）
├── bsg-zentao-api.skill   禅道接口规范 Skill
│
│  ── 用户操作文件 ──
├── README.md              本文件
├── requirements.txt       Python 依赖列表
├── .gitignore             排除敏感数据
├── setup_config.py        账号配置脚本
├── mcp_server.py          Claude Code 调用工具的入口
│
│  ── 核心代码 ──
├── bsg_zentao/
│   ├── client.py          禅道接口客户端
│   ├── constants.py       业务常量
│   └── utils.py           工具函数
└── tools/
    ├── data_tools.py      数据获取工具
    ├── calc_daily.py      日报计算逻辑
    └── report_tools.py    报告数据组装
```

以下内容自动生成在你的电脑上，**不会上传到 git**：

```
~/.bsg-zentao/
├── config.json    你的账号密码（仅本机）
├── 缓存/          当天接口数据缓存
└── 报告/
    ├── 日报/
    ├── 周汇总/
    ├── Bug界定/
    └── 版本复盘/
```

---

## 使用示例

打开 Claude Code，直接输入：

**生成报告：**
```
帮我出今天的平台项目日报
```

**查询数据：**
```
平台项目当前版本还有几天发布？
线上现在有多少个活跃Bug？
这个版本有哪些需求还没下单？
```

**综合分析：**
```
这个版本交付有风险吗？
```

---

## 常见问题

**Q：提示「配置文件不存在」**
运行账号配置脚本：`python3.12 setup_config.py`

**Q：提示「登录失败」**
检查账号密码是否正确，确认电脑网络能访问禅道。

**Q：报告数据不对或数据很旧**
清除缓存后重试：
```bash
rm -rf ~/.bsg-zentao/缓存/
```

**Q：提示「No module named mcp」之类的错误**
重新安装依赖：`python3.12 -m pip install -r requirements.txt`

**Q：接口字段变了，数据出现异常**
通知维护者更新工具，更新后运行 `git pull` 同步即可。

---

## 当前支持的功能

| 功能 | 状态 |
|---|---|
| 日报生成 | ✅ 可用 |
| 自由问答（版本 / Bug / 需求） | ✅ 可用 |
| 周汇总 | 🔜 开发中 |
| Bug 界定 | 🔜 开发中 |
| 版本复盘 | 🔜 开发中 |

---

## 版本历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-04-09 | 日报场景上线，含自由问答数据工具 |
