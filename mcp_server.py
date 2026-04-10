"""
mcp_server.py

MCP Server 入口。
Claude Code 通过这里调用所有工具。

注册了两类工具：
  数据工具（原子）：Claude 自由调用，用于回答自由问题
    - zentao_get_versions       获取项目版本列表
    - zentao_get_requirements   获取版本需求池数据
    - zentao_get_bugs           获取版本 Bug 数据

  报告工具（复合）：用于生成固定格式报告
    - zentao_daily_report       生成并保存日报
    - zentao_save_report        保存 Claude 生成的报告内容到文件

启动方式（Claude Code 配置）：
  claude mcp add bsg-zentao python /path/to/bsg-zentao/mcp_server.py

用户交互示例：
  "帮我出今天的日报"          → Claude 调用 zentao_daily_report
  "平台项目当前有多少线上bug" → Claude 调用 zentao_get_bugs
  "这个版本交付有风险吗"      → Claude 调用 zentao_get_versions + zentao_get_requirements
"""

import json
import logging
import sys
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import ACTIVE_PROJECTS
from tools.data_tools import get_versions, get_version_requirements, get_version_bugs
from tools.report_tools import assemble_daily_report, save_daily_report

# ─── 日志配置 ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,   # MCP 协议走 stdout，日志必须输出到 stderr
)
log = logging.getLogger(__name__)

# ─── 全局客户端（懒加载，首次工具调用时初始化）────────────────────────────────

_client: ZentaoClient | None = None


def _get_client() -> ZentaoClient:
    """获取禅道客户端，首次调用时登录，后续复用同一 Session。"""
    global _client
    if _client is None:
        log.info("初始化禅道客户端…")
        _client = ZentaoClient()
    return _client


def _project_choices() -> str:
    """生成可选项目列表文本，用于工具描述。"""
    return "、".join(f"{name}({pid})" for name, pid in ACTIVE_PROJECTS.items())


# ─── MCP Server 初始化 ────────────────────────────────────────────────────────

server = Server("bsg-zentao")


# ─── 工具列表 ─────────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # ── 数据工具1：版本列表 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_get_versions",
            description=(
                "获取禅道项目的版本信息，包括当前版本、下一版本、上一版本。"
                "用于回答：'这个版本还有几天发布'、'当前版本是哪个'、'是否今天发布'等问题。"
                f"可选项目：{_project_choices()}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": (
                            "项目 ID。"
                            f"可选值：{_project_choices()}。"
                            "如果用户未指定，优先使用平台项目（10）。"
                        ),
                    }
                },
                "required": ["project_id"],
            },
        ),

        # ── 数据工具2：需求池 ──────────────────────────────────────────────────
        types.Tool(
            name="zentao_get_requirements",
            description=(
                "获取指定版本的需求池数据，包含需求列表、各部门进度、"
                "延期情况、未下单需求、驳回记录等。"
                "用于回答：'这个版本任务量大吗'、'哪些需求还没下单'、"
                "'有没有延期风险'、'驳回记录怎么样'等问题。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {
                        "type": "string",
                        "description": "版本 ID，从 zentao_get_versions 的返回结果中获取。",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，与 version_id 对应的项目。",
                    },
                },
                "required": ["version_id", "project_id"],
            },
        ),

        # ── 数据工具3：Bug 数据 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_get_bugs",
            description=(
                "获取指定版本的 Bug 数据，包含线上 Bug 列表和统计摘要。"
                "用于回答：'线上有多少个 Bug'、'有没有高危 Bug'、"
                "'测试质量怎么样'、'Bug 分布在哪些需求上'等问题。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {
                        "type": "string",
                        "description": "版本 ID，从 zentao_get_versions 的返回结果中获取。",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，与 version_id 对应的项目。",
                    },
                },
                "required": ["version_id", "project_id"],
            },
        ),

        # ── 报告工具1：日报 ────────────────────────────────────────────────────
        types.Tool(
            name="zentao_daily_report",
            description=(
                "生成今日日报数据包，包含当前版本和下一版本的完整信息。"
                "调用后由 Claude 根据数据生成报告正文，并保存到本地文件。"
                "用户说'帮我出日报'、'生成今天的日报'、'日报'时调用此工具。"
                f"可选项目：{_project_choices()}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": (
                            "项目 ID。"
                            f"可选值：{_project_choices()}。"
                            "如果用户未指定，询问用户想要哪个项目的日报。"
                        ),
                    }
                },
                "required": ["project_id"],
            },
        ),

        # ── 报告工具2：保存报告 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_save_report",
            description=(
                "将 Claude 生成的报告内容保存到本地文件。"
                "通常在 Claude 生成报告正文后自动调用，用户无需手动触发。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要保存的报告内容（Markdown 格式）。",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，用于确定保存目录。",
                    },
                    "report_type": {
                        "type": "string",
                        "description": "报告类型：daily（日报）。后续版本将支持 weekly、bug_review、version_review。",
                        "enum": ["daily"],
                    },
                },
                "required": ["content", "project_id", "report_type"],
            },
        ),
    ]


# ─── 工具执行 ─────────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """
    工具执行入口。所有异常捕获后以 JSON 格式返回错误信息，不崩溃 Server。
    """
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )]
    except FileNotFoundError as e:
        # 配置文件不存在（用户未初始化）
        return [types.TextContent(type="text", text=json.dumps({
            "error": "配置文件不存在",
            "message": str(e),
            "action": "请先运行初始化命令：python setup_config.py",
        }, ensure_ascii=False))]
    except RuntimeError as e:
        # 登录失败、接口异常等
        return [types.TextContent(type="text", text=json.dumps({
            "error": "运行时错误",
            "message": str(e),
        }, ensure_ascii=False))]
    except Exception as e:
        log.exception("工具 %s 执行异常", name)
        return [types.TextContent(type="text", text=json.dumps({
            "error": "未知错误",
            "message": str(e),
            "tool": name,
        }, ensure_ascii=False))]


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    """根据工具名称分发到对应处理函数。"""

    client = _get_client()

    # ── 数据工具1：版本列表 ──────────────────────────────────────────────────
    if name == "zentao_get_versions":
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_versions（project=%s）", project_id)
        return get_versions(client, project_id)

    # ── 数据工具2：需求池 ────────────────────────────────────────────────────
    elif name == "zentao_get_requirements":
        version_id = args["version_id"]
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_requirements（version=%s）", version_id)
        data = get_version_requirements(client, version_id, project_id)
        # 返回给 Claude 的精简版本（去掉超大字段 task_details，Claude 不需要原始子任务）
        return {
            "version_id":    data["version_id"],
            "project_id":    data["project_id"],
            "pools":         data["pools"],           # 精简后的需求列表
            "review_stat":   data["review_stat"],     # 需求评审统计
            "pms":           data["pms"],             # PM 列表
            "current_delivered": data["current_delivered"],
            # task_details 和 php_member_map 不返回给 Claude（体积大，Claude 不需要原始子任务）
            # 这两个字段在 calc 层内部使用
            "_note": "task_details 和 php_member_map 供 calc 层内部使用，不在此返回。"
        }

    # ── 数据工具3：Bug 数据 ──────────────────────────────────────────────────
    elif name == "zentao_get_bugs":
        version_id = args["version_id"]
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_bugs（version=%s）", version_id)
        return get_version_bugs(client, version_id, project_id)

    # ── 报告工具1：日报 ──────────────────────────────────────────────────────
    elif name == "zentao_daily_report":
        project_id = args["project_id"]
        log.info("工具调用：zentao_daily_report（project=%s）", project_id)
        return assemble_daily_report(client, project_id)

    # ── 报告工具2：保存报告 ──────────────────────────────────────────────────
    elif name == "zentao_save_report":
        content     = args["content"]
        project_id  = args["project_id"]
        report_type = args.get("report_type", "daily")
        log.info("工具调用：zentao_save_report（type=%s）", report_type)

        if report_type == "daily":
            path = save_daily_report(content, project_id)
        else:
            raise ValueError(f"暂不支持的报告类型：{report_type}")

        return {
            "saved":   True,
            "path":    path,
            "message": f"报告已保存到：{path}",
        }

    else:
        raise ValueError(f"未知工具：{name}")


# ─── Server 启动 ──────────────────────────────────────────────────────────────

async def main():
    log.info("BSG 禅道 MCP Server 启动…")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="bsg-zentao",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
