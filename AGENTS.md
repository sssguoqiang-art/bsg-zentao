# BSG 禅道助手 · Codex 工作指引

本文件供 Codex 使用。
`CLAUDE.md` 是仓库中现有的 Claude 业务规则文件，保持原样，不要替换或删改；当任务涉及禅道业务判断、报告口径、字段含义、输出风格时，Codex 应以 `CLAUDE.md` 作为权威业务来源。

## 适用范围

- 本文件作用于当前目录及其所有子目录。
- 若后续子目录新增更深层的 `AGENTS.md`，则更近一层优先。

## Codex 开工前先做什么

- 先读 `README.md` 了解安装方式、目录结构和使用场景。
- 涉及日报、周汇总、Bug 界定、版本复盘时，再读 `CLAUDE.md` 对应章节，不要凭常识推断业务规则。
- 涉及禅道接口字段、调用姿势、Referer/warmup 等接口细节时，优先使用 `bsg-zentao-api` skill。

## 关键约定

- 不要修改 `CLAUDE.md`，除非用户明确要求。
- 不要把账号、密码、token 等敏感信息写入仓库；本地配置应放在 `~/.bsg-zentao/config.json` 或环境变量中。
- `~/.bsg-zentao/` 下的配置、缓存、报告属于本机数据，不进 git。
- Python 不要默认依赖系统 `python3`；优先使用 Python 3.12+，建议使用项目虚拟环境 `.venv`。

## 推荐命令

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup_config.py
```

如本地已经存在 `.venv`，优先复用：

```bash
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
```

## 代码和文档修改规则

- 改动报告逻辑、版本识别、Bug 归类、输出口径时，同步检查 `CLAUDE.md` 是否已有明确规则；有规则就遵守，没有规则再和用户确认。
- 改动安装方式、默认文件约定、使用入口时，同步更新 `README.md`。
- 如果禅道接口返回字段变化，优先使用 `scripts/schema_probe.py` 探针定位差异；必要时同步更新 `bsg-zentao-api.skill` 的来源内容。

## 文件速览

- `README.md`：安装、场景、FAQ
- `CLAUDE.md`：Claude 业务规则，也是 Codex 处理业务判断时的权威参考
- `bsg-zentao-api.skill`：禅道接口 Skill 打包文件
- `setup_config.py`：初始化账号与个人知识库
- `mcp_server.py`：本地 MCP Server 入口
- `bsg_zentao/`：客户端、常量、知识库、工具函数
- `tools/`：日报、周汇总、Bug 界定、版本复盘等核心逻辑

## 报告类任务的最低要求

- 结论必须数据驱动，不能脑补。
- 用户未明确指定项目时，默认按平台项目处理；只有明确说“平台项目+游戏项目 / 双项目”时才同时输出双项目内容。
- 当前版本 / 下一版本 / 上一版本判断遵循 `CLAUDE.md`。
- 非 Bug 识别、PHP 组归属、deadline / deliveryDate 用法遵循 `CLAUDE.md` 与 `bsg-zentao-api` skill。
- 发现规则冲突时，先以用户明确要求为准；否则暂停并说明冲突点。
