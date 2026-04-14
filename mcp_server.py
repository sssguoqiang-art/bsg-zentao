"""
mcp_server.py

MCP Server 入口。
Claude Code 通过这里调用所有工具。

注册了三类工具：
  数据工具（原子）：Claude 自由调用，用于回答自由问题
    - zentao_get_versions       获取项目版本列表
    - zentao_get_requirements   获取版本需求池数据
    - zentao_get_bugs           获取版本 Bug 数据

  报告工具（复合）：用于生成固定格式报告
    - zentao_daily_report       生成并保存日报
    - zentao_version_review     生成版本复盘数据包
    - zentao_bug_review         生成 Bug 复盘预分类数据包
    - zentao_save_report        保存 Claude 生成的报告内容到文件

  知识库工具（用户级）：管理个人 Profile 和 Memory
    - user_get_context          获取用户上下文（每次对话开始时调用）
    - user_profile_setup        引导式配置个人 Profile
    - user_memory_view          查看所有记忆
    - user_memory_manage        管理记忆（确认/拒绝/删除/重置）

启动方式（Claude Code 配置）：
  claude mcp add bsg-zentao python /path/to/bsg-zentao/mcp_server.py

用户交互示例：
  "帮我出今天的日报"          → Claude 调用 zentao_daily_report
  "平台项目当前有多少线上bug" → Claude 调用 zentao_get_bugs
  "这个版本交付有风险吗"      → Claude 调用 zentao_get_versions + zentao_get_requirements
  "帮我出Bug界定报告"         → Claude 调用 zentao_bug_review
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
from bsg_zentao.user_knowledge import (
    get_user_context, save_profile, get_profile,
    add_memory, update_memory_status, delete_memory, reset_memories, reset_memories_by_source,
    format_memories_for_display, format_profile_for_display,
    ROLES, DEPARTMENTS, COMMON_TASKS, OUTPUT_PREFERENCES,
)
from tools.data_tools import get_versions, get_version_requirements, get_version_bugs
from tools.report_tools import assemble_daily_report, save_daily_report
from tools.calc_bug_review import calc_bug_review, save_bug_review_report
from tools.report_tools_review import assemble_review_report, save_review_report

# ─── 日志配置 ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ─── 全局客户端（懒加载）──────────────────────────────────────────────────────

_client: ZentaoClient | None = None


def _get_client() -> ZentaoClient:
    global _client
    if _client is None:
        log.info("初始化禅道客户端…")
        _client = ZentaoClient()
    return _client


def _project_choices() -> str:
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

        # ── 报告工具2：版本复盘 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_version_review",
            description=(
                "生成版本复盘数据包，包含外部Bug复盘、内部Bug复盘、版本需求趋势等完整数据。\n"
                "调用后由 Claude 根据数据包生成复盘报告 Markdown 文本，再调用 zentao_save_report 保存。\n\n"
                "报告三段式结构：一、外部Bug复盘 → 二、内部Bug复盘 → 三、版本复盘"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": (
                            f"项目 ID。可选值：{_project_choices()}。"
                            "如果用户未指定，优先使用平台项目（10）。"
                        ),
                    },
                    "version": {
                        "type": "string",
                        "description": (
                            "目标版本。"
                            "'auto' = 自动识别最近已交付版本（默认）。"
                            "或填具体版本 ID，如 '394'。"
                        ),
                        "default": "auto",
                    },
                },
                "required": ["project_id"],
            },
        ),

        # ── 报告工具3：Bug 界定预分类 ──────────────────────────────────────────
        types.Tool(
            name="zentao_bug_review",
            description=(
                "生成 Bug 复盘预分类数据包，包含外部Bug责任界定、内部Bug责任界定、低质量任务识别。\n"
                "调用后由 Claude 根据数据包生成 Markdown 格式预分类报告。\n\n"
                "五部分固定结构：\n"
                "  一、部门 Bug 数量总览\n"
                "  二、疑似非Bug清单（type=performance 外部Bug）\n"
                "  三、外部 Bug 责任界定\n"
                "  四、内部 Bug 责任界定\n"
                "  五、低质量任务\n"
                "  复盘会前 To-Do\n\n"
                "用户说「帮我出Bug界定报告」/「出预分类」/「复盘预分类」/「Bug界定」时调用此工具。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {
                        "type": "string",
                        "description": (
                            "版本 ID。可不传，不传时自动识别最近已交付版本。"
                            "需指定特定版本时传入版本 ID（从 zentao_get_versions 获取）。"
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            f"项目 ID。可选值：{_project_choices()}。"
                            "未指定时优先使用平台项目（10）。"
                        ),
                    },
                },
                "required": ["project_id"],
            },
        ),

        # ── 报告工具4：保存报告 ────────────────────────────────────────────────
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
                        "description": "报告类型：daily（日报）、review（版本复盘）、bug_review（Bug界定）。",
                        "enum": ["daily", "review", "bug_review"],
                    },
                    "version_name": {
                        "type": "string",
                        "description": "版本名称，report_type=review/bug_review 时必传，如 'V2.11.0（0408）'。",
                    },
                },
                "required": ["content", "report_type"],
            },
        ),

        # ── 知识库工具1：获取用户上下文 ───────────────────────────────────────
        types.Tool(
            name="user_get_context",
            description=(
                "获取当前用户的个人知识库上下文，包含 Profile（身份配置）和已确认的 Memory（使用习惯）。"
                "每次对话开始时应自动调用此工具，将结果作为背景信息用于后续所有回答。"
                "如果返回'尚未配置'，主动引导用户运行 user_profile_setup。"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── 知识库工具2：配置 Profile ─────────────────────────────────────────
        types.Tool(
            name="user_profile_setup",
            description=(
                "引导式配置用户个人 Profile，包括姓名、禅道账号、部门、角色、"
                "主要关注项目、常用操作、输出偏好。"
                "以下任意表达均应触发此工具："
                "'帮我配置知识库'、'帮我更新知识库'、'修改我的配置'、'更新个人信息'、"
                "'重新配置'、'我要改配置'、'帮我设置个人信息'、'更新我的资料'。"
                "支持部分更新，未传字段保留原值。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":            {"type": "string", "description": "姓名"},
                    "zentao_account":  {"type": "string", "description": "禅道账号（登录用户名）"},
                    "department":      {"type": "string", "description": f"部门，可选：{'、'.join(DEPARTMENTS)}"},
                    "role":            {"type": "string", "description": f"角色，可选：{'、'.join(ROLES)}"},
                    "primary_project": {"type": "string", "description": "主要关注项目：平台项目 / 游戏项目 / 两者"},
                    "common_tasks":    {"type": "array",  "items": {"type": "string"}, "description": f"常用操作列表，可选：{'、'.join(COMMON_TASKS)}"},
                    "output_pref":     {"type": "string", "description": f"输出偏好，可选：{'、'.join(OUTPUT_PREFERENCES)}"},
                },
                "required": [],
            },
        ),

        # ── 知识库工具3：查看记忆 ─────────────────────────────────────────────
        types.Tool(
            name="user_memory_view",
            description=(
                "查看用户当前的所有记忆，包含已确认、待确认、已拒绝三种状态。"
                "同时展示 Profile 配置摘要。"
                "以下任意表达均应触发此工具："
                "'看看你记住了什么'、'查看我的知识库'、'我的记忆'、'知识库里有什么'、"
                "'你都记了什么'、'帮我看下知识库'。"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── 知识库工具4：管理记忆 ─────────────────────────────────────────────
        types.Tool(
            name="user_memory_manage",
            description=(
                "管理记忆：确认、拒绝、删除某条记忆，或批量重置。"
                "以下任意表达均应触发此工具："
                "'记住它'、'帮我记住'、'不用记'、'删掉这条'、"
                "'清空记忆'、'清空我的知识库'、'重置知识库'、'完全重置知识库'。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["confirm", "reject", "delete", "reset_auto", "reset_all", "reset_full", "add"],
                        "description": (
                            "操作类型："
                            "confirm=确认某条待确认记忆（需 memory_id）；"
                            "reject=拒绝某条待确认记忆（需 memory_id）；"
                            "delete=删除某条已确认记忆（需 memory_id）；"
                            "reset_auto=只清系统自动提取的记忆，保留用户手动添加的和 Profile；"
                            "reset_all=清空所有记忆（Memory 区），保留 Profile；"
                            "reset_full=清空所有记忆 + Profile，完全重置；"
                            "add=手动添加一条记忆（需 content，直接进入 confirmed 状态）"
                        ),
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "记忆 ID，格式如 mem_20260413_abc123，从 user_memory_view 返回结果中获取。confirm/reject/delete 操作必传。",
                    },
                    "content": {
                        "type": "string",
                        "description": "记忆内容，add 操作必传。",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表，add 操作可选。",
                    },
                },
                "required": ["action"],
            },
        ),
    ]


# ─── 工具执行 ─────────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )]
    except FileNotFoundError as e:
        return [types.TextContent(type="text", text=json.dumps({
            "error": "配置文件不存在",
            "message": str(e),
            "action": "请先运行初始化命令：python setup_config.py",
        }, ensure_ascii=False))]
    except RuntimeError as e:
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


# ─── 需要 Zentao 连接的工具集合 ──────────────────────────────────────────────

_ZENTAO_TOOLS = {
    "zentao_get_versions", "zentao_get_requirements",
    "zentao_get_bugs", "zentao_daily_report", "zentao_save_report",
    "zentao_version_review", "zentao_bug_review",
}


async def _dispatch(name: str, args: dict[str, Any]) -> Any:

    # ── 数据工具1：版本列表 ──────────────────────────────────────────────────
    if name == "zentao_get_versions":
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_versions（project=%s）", project_id)
        return get_versions(_get_client(), project_id)

    # ── 数据工具2：需求池 ────────────────────────────────────────────────────
    elif name == "zentao_get_requirements":
        version_id = args["version_id"]
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_requirements（version=%s）", version_id)
        data = get_version_requirements(_get_client(), version_id, project_id)
        return {
            "version_id":        data["version_id"],
            "project_id":        data["project_id"],
            "pools":             data["pools"],
            "review_stat":       data["review_stat"],
            "pms":               data["pms"],
            "current_delivered": data["current_delivered"],
            "_note": "task_details 和 php_member_map 供 calc 层内部使用，不在此返回。",
        }

    # ── 数据工具3：Bug 数据 ──────────────────────────────────────────────────
    elif name == "zentao_get_bugs":
        version_id = args["version_id"]
        project_id = args["project_id"]
        log.info("工具调用：zentao_get_bugs（version=%s）", version_id)
        return get_version_bugs(_get_client(), version_id, project_id)

    # ── 报告工具1：日报 ──────────────────────────────────────────────────────
    elif name == "zentao_daily_report":
        project_id = args["project_id"]
        log.info("工具调用：zentao_daily_report（project=%s）", project_id)
        return assemble_daily_report(_get_client(), project_id)

    # ── 报告工具2：版本复盘 ──────────────────────────────────────────────────
    elif name == "zentao_version_review":
        project_id = args["project_id"]
        version    = args.get("version", "auto")
        log.info("工具调用：zentao_version_review（project=%s，version=%s）", project_id, version)
        return assemble_review_report(_get_client(), project_id, version)

    # ── 报告工具3：Bug 界定预分类 ────────────────────────────────────────────
    elif name == "zentao_bug_review":
        project_id = args["project_id"]
        version_id = args.get("version_id")
        log.info("工具调用：zentao_bug_review（project=%s，version=%s）", project_id, version_id or "auto")

        # version_id 未传时，自动识别最近已交付版本
        if not version_id:
            versions = get_versions(_get_client(), project_id)
            prev = versions.get("prev")
            curr = versions.get("curr")
            # 优先取上一版本（已交付），无上一版本时取当前版本
            target = prev or curr
            if not target:
                raise RuntimeError("无法识别目标版本，请手动传入 version_id。")
            version_id = target["id"]
            log.info("  自动识别版本：%s（ID=%s）", target.get("name"), version_id)

        return calc_bug_review(_get_client(), version_id, project_id)

    # ── 报告工具4：保存报告 ──────────────────────────────────────────────────
    elif name == "zentao_save_report":
        content      = args["content"]
        project_id   = args.get("project_id", "10")
        report_type  = args.get("report_type", "daily")
        version_name = args.get("version_name", "")
        log.info("工具调用：zentao_save_report（type=%s）", report_type)

        if report_type == "daily":
            path = save_daily_report(content, project_id)
        elif report_type == "review":
            if not version_name:
                raise ValueError("report_type=review 时必须传 version_name 参数")
            path = save_review_report(content, version_name)
        elif report_type == "bug_review":
            if not version_name:
                raise ValueError("report_type=bug_review 时必须传 version_name 参数")
            path = save_bug_review_report(content, version_name)
        else:
            raise ValueError(f"暂不支持的报告类型：{report_type}")

        return {"saved": True, "path": path, "message": f"报告已保存到：{path}"}

    # ── 知识库工具1：获取用户上下文 ──────────────────────────────────────────
    elif name == "user_get_context":
        log.info("工具调用：user_get_context")
        context = get_user_context()
        profile = get_profile()
        return {
            "context": context,
            "has_profile": profile is not None,
            "hint": "请将 context 字段内容作为用户背景信息，用于后续所有回答。" if profile else "用户尚未配置 Profile，建议引导运行 user_profile_setup。",
        }

    # ── 知识库工具2：配置 Profile ────────────────────────────────────────────
    elif name == "user_profile_setup":
        log.info("工具调用：user_profile_setup")
        data = {k: v for k, v in args.items() if v is not None}
        if not data:
            return {
                "current": format_profile_for_display(),
                "guide": (
                    "请提供以下信息来配置你的个人知识库：\n"
                    f"  姓名、禅道账号、部门（{' / '.join(DEPARTMENTS[:5])}…）、"
                    f"角色（{'、'.join(ROLES)}）、"
                    "主要关注项目（平台项目/游戏项目/两者）、"
                    f"常用操作（多选，可选：{'、'.join(COMMON_TASKS[:4])}…）、"
                    f"输出偏好（{'、'.join(OUTPUT_PREFERENCES)}）"
                ),
            }
        save_profile(data)
        return {
            "saved": True,
            "profile": format_profile_for_display(),
            "message": "✅ Profile 已更新。下次对话开始时会自动加载这些信息。",
        }

    # ── 知识库工具3：查看记忆 ────────────────────────────────────────────────
    elif name == "user_memory_view":
        log.info("工具调用：user_memory_view")
        return {
            "profile":  format_profile_for_display(),
            "memories": format_memories_for_display(),
            "tip": "可以说'删掉[ID]那条'或'清空记忆'来管理记忆。",
        }

    # ── 知识库工具4：管理记忆 ────────────────────────────────────────────────
    elif name == "user_memory_manage":
        action    = args["action"]
        memory_id = args.get("memory_id")
        content   = args.get("content")
        tags      = args.get("tags", [])
        log.info("工具调用：user_memory_manage（action=%s）", action)

        if action == "confirm":
            if not memory_id: raise ValueError("confirm 操作需要 memory_id")
            ok = update_memory_status(memory_id, "confirmed")
            return {"success": ok, "message": "✅ 记忆已确认。" if ok else f"未找到记忆 {memory_id}"}
        elif action == "reject":
            if not memory_id: raise ValueError("reject 操作需要 memory_id")
            ok = update_memory_status(memory_id, "rejected")
            return {"success": ok, "message": "❌ 记忆已拒绝。" if ok else f"未找到记忆 {memory_id}"}
        elif action == "delete":
            if not memory_id: raise ValueError("delete 操作需要 memory_id")
            ok = delete_memory(memory_id)
            return {"success": ok, "message": "🗑️ 记忆已删除。" if ok else f"未找到记忆 {memory_id}"}
        elif action == "reset_auto":
            deleted = reset_memories_by_source("auto")
            return {"success": True, "message": f"🗑️ 已清除 {deleted} 条自动提取的记忆，手动添加和 Profile 保留。"}
        elif action == "reset_all":
            result = reset_memories(keep_profile=True)
            return {"success": True, "message": f"🗑️ 已清除 {result['deleted_memories']} 条记忆，Profile 配置保留。"}
        elif action == "reset_full":
            result = reset_memories(keep_profile=False)
            return {"success": True, "message": f"🗑️ 已完全重置：清除 {result['deleted_memories']} 条记忆 + Profile 配置。"}
        elif action == "add":
            if not content: raise ValueError("add 操作需要 content")
            entry = add_memory(content=content, source="user", tags=tags)
            return {"success": True, "memory_id": entry["id"], "message": f"✅ 记忆已添加（{entry['id']}），立即生效。"}
        else:
            raise ValueError(f"未知 action：{action}")

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
