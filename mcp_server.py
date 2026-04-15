"""
mcp_server.py  —  BSG 禅道 MCP Server

工具清单：
  数据工具（原子）：
    - zentao_get_versions          获取项目版本列表
    - zentao_get_requirements      获取版本需求池数据
    - zentao_get_bugs              获取版本 Bug 数据
    - zentao_get_member_tasks      按人查询任务（支持显示名自动解析）
    - zentao_build_member_index    构建/刷新全公司成员名称索引

  报告工具（复合）：
    - zentao_daily_report          生成并保存日报
    - zentao_version_review        【版本复盘】正式复盘文档
    - zentao_bug_review            【Bug界定】复盘前预分类材料
    - zentao_save_report           保存 Claude 生成的报告内容

  知识库工具：
    - user_get_context             获取用户上下文
    - user_profile_setup           配置个人 Profile
    - user_memory_view             查看记忆
    - user_memory_manage           管理记忆

⚠️ Bug界定 vs 版本复盘 区分规则：
  【版本复盘】触发词：版本复盘 / 出复盘 / 复盘报告
  【Bug界定】触发词：Bug界定 / 预分类 / 界定报告
  只说"复盘"时必须先询问用户要哪个。
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
from bsg_zentao.member_index import build_member_index, load_index, index_age_days
from bsg_zentao.user_knowledge import (
    get_user_context, save_profile, get_profile,
    add_memory, update_memory_status, delete_memory, reset_memories, reset_memories_by_source,
    format_memories_for_display, format_profile_for_display,
    ROLES, DEPARTMENTS, COMMON_TASKS, OUTPUT_PREFERENCES,
)
from tools.data_tools import get_versions, get_version_requirements, get_version_bugs, get_member_tasks
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
                            f"项目 ID。可选值：{_project_choices()}。"
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
                    "version_id": {"type": "string", "description": "版本 ID，从 zentao_get_versions 返回结果中获取。"},
                    "project_id": {"type": "string", "description": "项目 ID，与 version_id 对应的项目。"},
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
                    "version_id": {"type": "string", "description": "版本 ID，从 zentao_get_versions 返回结果中获取。"},
                    "project_id": {"type": "string", "description": "项目 ID，与 version_id 对应的项目。"},
                },
                "required": ["version_id", "project_id"],
            },
        ),

        # ── 数据工具4：按人查询任务 ───────────────────────────────────────────
        types.Tool(
            name="zentao_get_member_tasks",
            description=(
                "按人查询任务：获取指定成员在某时间范围内的任务列表、工时汇总、完成状况。\n"
                "支持直接传入显示名（如「陈益」）或禅道账号（如 chenyi），工具会自动解析。\n"
                "无需知道对方属于哪个部门，工具会自动检索全部平台部门。\n\n"
                "以下表达均应触发此工具：\n"
                "  '今天 [XX] 的工作情况' / '[XX] 今天在做什么' / '[XX] 这周任务'\n"
                "  '[XX] 有多少任务' / '[XX] 工时如何' / '[XX] 完成了哪些'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "禅道账号（如 liuhf）或显示名（如 刘海峰），工具会自动解析。",
                    },
                    "begin": {"type": "string", "description": "查询起始日期，YYYY-MM-DD 格式。"},
                    "end":   {"type": "string", "description": "查询结束日期，YYYY-MM-DD 格式。可与 begin 相同（单天查询）。"},
                    "dept_id": {
                        "type": "string",
                        "description": (
                            "部门 ID，可选。已知对方部门时传入可加快查询。"
                            "可选值：44（PHP1部）、47（PHP2部）、43（Web部）、42（Cocos部）、27（美术部）、45（测试部）、1（产品部）、48（效能部）"
                        ),
                    },
                    "project_id":   {"type": "string", "description": "项目 ID，可选。不传时根据 dept_id 自动判断。"},
                    "execution_id": {"type": "string", "description": "版本 ID，可选。只看某版本任务时传入，空 = 不限。"},
                },
                "required": ["username", "begin", "end"],
            },
        ),

        # ── 数据工具5：构建成员索引 ───────────────────────────────────────────
        types.Tool(
            name="zentao_build_member_index",
            description=(
                "构建或刷新全公司成员名称索引，建立「显示名 ↔ 禅道账号」的双向映射。\n"
                "首次使用按名字查人之前、有新人入职时，需要运行此工具。\n\n"
                "以下表达应触发此工具：\n"
                "  '刷新成员索引' / '更新人员列表' / '构建成员索引' / '索引找不到人' / '有新人入职'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "description": "是否强制重建。True = 无论是否已有索引都重建；False = 已有索引时跳过（默认）。",
                        "default": False,
                    },
                },
                "required": [],
            },
        ),

        # ── 报告工具1：日报 ────────────────────────────────────────────────────
        types.Tool(
            name="zentao_daily_report",
            description=(
                "生成今日日报数据包，包含当前版本和下一版本的完整信息。"
                "调用后由 Claude 生成报告正文，完成后自动调用 zentao_save_report 保存，无需询问用户。"
                "用户说'帮我出日报'、'生成今天的日报'、'日报'时调用此工具。"
                f"可选项目：{_project_choices()}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": f"项目 ID。可选值：{_project_choices()}。如果用户未指定，询问想要哪个项目。",
                    }
                },
                "required": ["project_id"],
            },
        ),

        # ── 报告工具2：版本复盘 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_version_review",
            description=(
                "【版本复盘报告】——生成复盘会上展示的正式文档。\n"
                "包含：外部Bug复盘（趋势+深度）/ 内部Bug复盘 / 版本需求趋势 / 延期分析。\n\n"
                "触发词：'帮我出版本复盘' / '生成复盘报告' / '出复盘' / '复盘报告'\n\n"
                "⚠️ 与 Bug界定 的区别：版本复盘 = 正式文档；Bug界定 = 复盘前预分类（用 zentao_bug_review）\n"
                "只说'复盘'时，必须先询问是'版本复盘报告'还是'Bug界定预分类'。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": f"项目 ID。可选值：{_project_choices()}。未指定时优先平台项目（10）。",
                    },
                    "version": {
                        "type": "string",
                        "description": "'auto' = 自动识别最近已交付版本；或填具体版本 ID 如 '394'。",
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
                "【Bug界定预分类】——生成复盘前的预分类准备材料。\n"
                "帮助判断哪些Bug值得复盘、各Bug归属是什么、哪些任务质量有问题。\n"
                "输出：部门Bug总览 / 疑似非Bug / 外部Bug界定 / 内部Bug界定 / 低质量任务。\n\n"
                "触发词：'Bug界定' / '出界定报告' / 'Bug预分类' / '复盘前准备'\n\n"
                "⚠️ 与版本复盘的区别：Bug界定 = 预分类材料；版本复盘 = 正式文档（用 zentao_version_review）\n"
                "只说'复盘'时，必须先询问是'版本复盘报告'还是'Bug界定预分类'。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {
                        "type": "string",
                        "description": "版本 ID，可不传（自动识别最近已交付版本）。",
                    },
                    "project_id": {
                        "type": "string",
                        "description": f"项目 ID。可选值：{_project_choices()}。未指定时优先平台项目（10）。",
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
                    "content":      {"type": "string", "description": "要保存的报告内容（Markdown 格式）。"},
                    "project_id":   {"type": "string", "description": "项目 ID，用于确定保存目录。"},
                    "report_type":  {"type": "string", "description": "报告类型：daily / review / bug_review。", "enum": ["daily", "review", "bug_review"]},
                    "version_name": {"type": "string", "description": "版本名称，report_type=review/bug_review 时必传，如 'V2.11.0（0408）'。"},
                },
                "required": ["content", "report_type"],
            },
        ),

        # ── 知识库工具1：获取用户上下文 ───────────────────────────────────────
        types.Tool(
            name="user_get_context",
            description=(
                "获取当前用户的个人知识库上下文，包含 Profile 和已确认的 Memory。"
                "每次对话开始时应自动调用，将结果作为背景信息用于后续所有回答。"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── 知识库工具2：配置 Profile ─────────────────────────────────────────
        types.Tool(
            name="user_profile_setup",
            description=(
                "引导式配置用户个人 Profile（姓名、禅道账号、部门、角色等）。"
                "触发词：'帮我配置知识库' / '修改我的配置' / '更新个人信息' / '我要改配置'。"
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
                "查看用户当前的所有记忆（已确认、待确认、已拒绝）及 Profile 摘要。"
                "触发词：'看看你记住了什么' / '查看我的知识库' / '知识库里有什么'。"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── 知识库工具4：管理记忆 ─────────────────────────────────────────────
        types.Tool(
            name="user_memory_manage",
            description=(
                "管理记忆：确认、拒绝、删除某条记忆，或批量重置。"
                "触发词：'记住它' / '帮我记住' / '不用记' / '删掉这条' / '清空记忆'。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["confirm", "reject", "delete", "reset_auto", "reset_all", "reset_full", "add"],
                        "description": (
                            "confirm=确认待确认记忆；reject=拒绝；delete=删除已确认；"
                            "reset_auto=清自动记忆；reset_all=清所有记忆保留Profile；"
                            "reset_full=全部重置；add=手动添加"
                        ),
                    },
                    "memory_id": {"type": "string", "description": "记忆 ID，confirm/reject/delete 必传。"},
                    "content":   {"type": "string", "description": "记忆内容，add 必传。"},
                    "tags":      {"type": "array", "items": {"type": "string"}, "description": "标签列表，add 可选。"},
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
            "error": "配置文件不存在", "message": str(e),
            "action": "请先运行初始化命令：python setup_config.py",
        }, ensure_ascii=False))]
    except RuntimeError as e:
        return [types.TextContent(type="text", text=json.dumps({
            "error": "运行时错误", "message": str(e),
        }, ensure_ascii=False))]
    except Exception as e:
        log.exception("工具 %s 执行异常", name)
        return [types.TextContent(type="text", text=json.dumps({
            "error": "未知错误", "message": str(e), "tool": name,
        }, ensure_ascii=False))]


# ─── 分发 ─────────────────────────────────────────────────────────────────────

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

    # ── 数据工具4：按人查询任务 ──────────────────────────────────────────────
    elif name == "zentao_get_member_tasks":
        username     = args["username"]
        begin        = args["begin"]
        end          = args["end"]
        dept_id      = args.get("dept_id", "")
        project_id   = args.get("project_id", "")
        execution_id = args.get("execution_id", "")
        log.info("工具调用：zentao_get_member_tasks（user=%s，%s—%s）", username, begin, end)
        return get_member_tasks(_get_client(), username, begin, end, dept_id, project_id, execution_id)

    # ── 数据工具5：构建成员索引 ──────────────────────────────────────────────
    elif name == "zentao_build_member_index":
        force = bool(args.get("force", False))
        log.info("工具调用：zentao_build_member_index（force=%s）", force)
        idx = build_member_index(_get_client(), force=force)
        age = index_age_days()
        return {
            "success":   True,
            "total":     idx.get("total", 0),
            "built_at":  idx.get("built_at", ""),
            "age_days":  age,
            "message":   (
                f"✅ 成员索引构建完成，共 {idx.get('total', 0)} 人，保存于 ~/.bsg-zentao/member_index.json。"
                if force or idx.get("total", 0) > 0 else
                "⚠️ 索引构建完成但为空，请检查禅道网络是否可达。"
            ),
            "sample": list(idx.get("by_name", {}).items())[:5],  # 返回前5条供确认
        }

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

        if not version_id:
            versions = get_versions(_get_client(), project_id)
            prev     = versions.get("prev")
            curr     = versions.get("curr")
            target   = prev or curr
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
        # 顺便提示成员索引状态
        age = index_age_days()
        index_hint = "" if age <= 7 else f"⚠️ 成员索引已 {age} 天未更新，按名字查人可能失败，建议调用 zentao_build_member_index 刷新。"
        return {
            "context":     context,
            "has_profile": profile is not None,
            "hint": "请将 context 字段内容作为用户背景信息，用于后续所有回答。" if profile else "用户尚未配置 Profile，建议引导运行 user_profile_setup。",
            "index_hint":  index_hint,
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
            "saved":   True,
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
