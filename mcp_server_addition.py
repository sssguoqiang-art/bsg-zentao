"""
mcp_server.py 新增内容 —— 在原有 5 个工具的基础上，新增以下内容：

1. 在 import 区新增：
   from tools.report_tools import assemble_daily_report, save_daily_report, \
       assemble_weekly_report, save_weekly_report

2. 在 list_tools() 的 return 列表末尾，继续追加以下两个 Tool 定义

3. 在 _dispatch() 中追加对应的分发逻辑
"""

# ── 追加到 list_tools() 的 return 列表 ────────────────────────────────────────

WEEKLY_TOOLS = """
        # ── 报告工具3：周汇总 ──────────────────────────────────────────────────
        types.Tool(
            name="zentao_weekly_report",
            description=(
                "生成本周周汇总数据包，包含平台项目和游戏项目的上一/当前/下一版本完整信息。"
                "涵盖版本交付情况、延期需求、版本调整、重点需求跟进、线上Bug等。"
                "调用后由 Claude 生成两份报告：效能周汇总（管理会议版）和效能周报（老板版）。"
                "用户说'帮我出周报'、'生成周汇总'、'出本周汇总'时调用此工具。"
                "注意：专题进展（AI伴侣/Web5/性能/招聘）和效能工作内容不来自禅道，"
                "Claude 生成报告时需输出占位符，提示用户人工补充。"
            ),
            inputSchema={
                "type": "object",
                "properties": {},   # 无需参数，固定抓取平台+游戏两个项目
                "required": [],
            },
        ),

        # ── 报告工具4：保存周报 ────────────────────────────────────────────────
        types.Tool(
            name="zentao_save_weekly_report",
            description=(
                "将 Claude 生成的周汇总或周报内容保存到本地文件。"
                "通常在 Claude 生成报告正文后自动调用，用户无需手动触发。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要保存的报告内容（Markdown 格式）。",
                    },
                    "report_type": {
                        "type": "string",
                        "description": (
                            "报告类型：weekly_summary（效能周汇总，管理会议版）"
                            "或 weekly_report（效能周报，老板版）。"
                        ),
                        "enum": ["weekly_summary", "weekly_report"],
                    },
                },
                "required": ["content", "report_type"],
            },
        ),
"""

# ── 追加到 _dispatch() ────────────────────────────────────────────────────────

DISPATCH_ADDITION = """
    # ── 报告工具3：周汇总 ──────────────────────────────────────────────────────
    elif name == "zentao_weekly_report":
        log.info("工具调用：zentao_weekly_report")
        return assemble_weekly_report(client)

    # ── 报告工具4：保存周报 ────────────────────────────────────────────────────
    elif name == "zentao_save_weekly_report":
        content     = args["content"]
        report_type = args.get("report_type", "weekly_summary")
        log.info("工具调用：zentao_save_weekly_report（type=%s）", report_type)

        path = save_weekly_report(content, report_type=report_type)
        return {
            "saved":   True,
            "path":    path,
            "message": f"报告已保存到：{path}",
        }
"""
