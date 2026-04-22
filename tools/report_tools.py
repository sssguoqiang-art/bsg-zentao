"""
tools/report_tools.py

复合报告工具层：组装所有板块数据，返回给 MCP Server，由 Claude 生成报告。

设计原则：
  - 这一层只负责"把数据拼在一起"，不做格式化
  - 返回的是结构化数据字典，Claude 拿到后自行组织语言和格式
  - 同时处理数据校验：数据异常时返回 warnings，Claude 可以在报告中提示

对外暴露：
  assemble_daily_report(client, project_id)  → 日报完整数据包
"""

import json
import logging
from datetime import date
from pathlib import Path

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import ACTIVE_PROJECTS, REPORT_DEPTS_RAW, to_display
from bsg_zentao.utils import (
    get_report_path, make_daily_filename,
    weekday_cn, fmt_date_full,
)
from tools.data_tools import get_versions, get_version_requirements, get_version_bugs
from tools.calc_daily import (
    calc_summary, calc_dept_progress, calc_delay_list,
    calc_not_test_list, calc_test_focus, calc_online_bugs,
    calc_rejected_list, calc_next_workload,
    calc_testing_followups, calc_merge_pending,
    is_pool_in_version,
)

log = logging.getLogger(__name__)


# ─── 数据校验 ─────────────────────────────────────────────────────────────────

def _validate(versions: dict, curr_req: dict) -> list[str]:
    """
    数据合理性校验，返回警告信息列表。
    警告不阻断流程，Claude 可在报告中酌情提示。
    """
    warnings = []
    curr = versions.get("curr")

    if not curr:
        warnings.append("⚠️ 未找到当前版本，版本识别可能异常，请检查禅道数据。")
        return warnings

    # 版本截止日已过超过 7 天，可能识别错误
    if curr["remaining_days"] < -7:
        warnings.append(
            f"⚠️ 当前版本 {curr['name']} 截止日已过 {abs(curr['remaining_days'])} 天，"
            "版本识别可能有误，建议核查。"
        )

    # 需求池为空
    pools = curr_req.get("pools", [])
    if not pools:
        warnings.append(
            f"⚠️ 版本 {curr['name']} 需求池为空，数据可能拉取异常，建议重新运行。"
        )

    # 需求池数量异常（超过 200 条可能触发分页未处理的情况）
    if len(pools) >= 200:
        warnings.append(
            f"⚠️ 需求池数量达到 {len(pools)} 条，接近单页上限（200），"
            "可能存在数据截断，建议人工核查。"
        )

    return warnings


# ─── 部门进度格式化（供 Claude 使用的展示友好结构）─────────────────────────────

def _format_dept_progress(dept_progress: dict) -> list[dict]:
    """
    把部门进度计算结果转换为展示友好的列表格式。
    使用显示名称（PHP1组而非PHP1部）。
    """
    rows = []
    for dept_raw in REPORT_DEPTS_RAW:
        d = dept_progress.get(dept_raw, {})
        left    = d.get("total_left", 0.0)
        pct     = d.get("progress_pct")
        rows.append({
            "dept":            to_display(dept_raw),
            "remaining_label": d.get("remaining_label", "0"),
            "total_left":      f"{left:.0f}h" if left > 0 else "—",
            "progress_pct":    f"{pct}%" if pct is not None else "—",
        })
    return rows


def _format_dept_workload(dept_workload: dict) -> list[dict]:
    """
    把下一版本工时总览转换为展示友好的列表格式。
    """
    rows = []
    for dept_raw in REPORT_DEPTS_RAW:
        dw = dept_workload.get(dept_raw, {})
        rows.append({
            "dept":           to_display(dept_raw),
            "tasks":          dw.get("tasks", 0),
            "estimate":       f"{dw.get('estimate', 0):.0f}h",
            "tasks_in_v":     dw.get("tasks_in_v", 0),
            "estimate_in_v":  f"{dw.get('estimate_in_v', 0):.0f}h",
        })
    return rows


# ─── 主函数：组装日报完整数据包 ───────────────────────────────────────────────

def assemble_daily_report(client: ZentaoClient, project_id: str, force_refresh: bool = False) -> dict:
    """
    组装日报所需的全部数据，返回给 Claude。

    返回的数据结构：
    {
        "meta": {
            "project_id":    "10",
            "project_name":  "平台项目",
            "date":          "4月9日",
            "weekday":       "周四",
            "is_release_day": False,
            "warnings":      [...],   # 数据异常警告
        },
        "curr_version": {
            "info":         {id, name, end, remaining_days, is_release_day},
            "summary":      {total, done, testing, dev, unordered, postponed},
            "dept_progress": [...],   # 部门进度列表（显示名）
            "delay_list":    [...],   # 延期情况
            "not_test": {
                "today_due": [...],   # 今日截止（置顶）
                "other_due": [...],   # 其他临期
            },
            "test_focus":    [...],   # 测试关注
            "testing_followups": [...], # 测试处理中（带卡住部门）
            "merge_pending": [...],     # 待合并确认（带卡住部门）
            "online_bugs":   [...],   # 线上 Bug
            "rejected_list": [...],   # 驳回专栏
            "review_stat":   {unReview, pendingReview},  # 需求评审统计
        },
        "next_version": None | {
            "info":          {id, name, end, remaining_days},
            "summary":       {total, ordered, unordered},
            "dept_workload": [...],   # 工时总览列表（显示名）
            "unordered_list":[...],   # 未下单需求
        },
        "report_path": "~/.bsg-zentao/报告/日报/20260409_日报.md",  # 保存路径（供保存用）
    }

    Claude 拿到此数据后：
      1. 根据 meta.is_release_day 判断用发布核查模式还是普通模式
      2. 根据各板块数据生成结论句和报告正文
      3. 调用 save_daily_report() 保存文件
    """
    today   = date.today()
    log.info("开始组装日报数据（项目=%s）…", project_id)

    # ── 步骤1：取版本信息 ────────────────────────────────────────────────────
    log.info("  [1/4] 识别版本…")
    versions = get_versions(client, project_id, force_refresh=force_refresh)
    curr     = versions.get("curr")
    nxt      = versions.get("next")

    if not curr:
        return {
            "meta": {
                "project_id":   project_id,
                "project_name": _project_name(project_id),
                "date":         fmt_date_full(today.isoformat()),
                "weekday":      weekday_cn(today),
                "is_release_day": False,
                "warnings":     ["❌ 未找到当前版本，请检查禅道账号权限或项目配置。"],
            },
            "curr_version": None,
            "next_version":  None,
            "report_path":   None,
        }

    # ── 步骤2：取当前版本数据 ────────────────────────────────────────────────
    log.info("  [2/4] 拉取当前版本需求池（%s）…", curr["name"])
    curr_req  = get_version_requirements(client, curr["id"], project_id, force_refresh=force_refresh)
    curr_bugs = get_version_bugs(client, curr["id"], project_id, force_refresh=force_refresh)

    curr_pools       = [p for p in curr_req["pools"] if p.get("task_status") != "cancel"]
    curr_pools_in_v  = [p for p in curr_pools if is_pool_in_version(p, curr["end"])]
    curr_task_details = curr_req["task_details"]
    php_member_map   = curr_req["php_member_map"]
    bugs             = curr_bugs["bugs"]

    # ── 步骤3：计算当前版本各板块 ────────────────────────────────────────────
    log.info("  [3/4] 计算各板块数据…")
    version_end   = curr["end"]
    dept_progress = calc_dept_progress(curr_pools, curr_task_details, version_end, php_member_map)

    curr_data = {
        "info":          curr,
        "summary":       calc_summary(curr_pools_in_v),
        "dept_progress": _format_dept_progress(dept_progress),
        "delay_list":    calc_delay_list(curr_pools, curr_task_details, php_member_map, version_end, today),
        "not_test":      calc_not_test_list(curr_pools, curr_task_details, php_member_map, version_end, today),
        "test_focus":    calc_test_focus(curr_pools_in_v, bugs),
        "testing_followups": calc_testing_followups(curr_pools, curr_task_details, php_member_map, version_end),
        "merge_pending": calc_merge_pending(curr_pools, curr_task_details, php_member_map, version_end),
        "online_bugs":   calc_online_bugs(bugs),
        "rejected_list": calc_rejected_list(curr_pools_in_v),
        "review_stat":   curr_req.get("review_stat", {}),
    }

    # ── 步骤4：下一版本（有则取）──────────────────────────────────────────────
    next_data = None
    if nxt:
        log.info("  [4/4] 拉取下一版本需求池（%s）…", nxt["name"])
        next_req    = get_version_requirements(client, nxt["id"], project_id, force_refresh=force_refresh)
        next_pools  = [p for p in next_req["pools"] if p.get("task_status") != "cancel"]
        next_td     = next_req["task_details"]
        next_php_map = next_req["php_member_map"]
        next_workload = calc_next_workload(next_pools, next_td, next_php_map, nxt["end"])

        next_data = {
            "info":           nxt,
            "summary": {
                "total":    next_workload["total"],
                "ordered":  next_workload["ordered"],
                "unordered":next_workload["unordered"],
            },
            "dept_workload":  _format_dept_workload(next_workload["dept_workload"]),
            "unordered_list": next_workload["unordered_list"],
        }
    else:
        log.info("  [4/4] 无下一版本，跳过。")

    # ── 数据校验 ─────────────────────────────────────────────────────────────
    warnings = _validate(versions, curr_req)

    # ── 组装返回结构 ──────────────────────────────────────────────────────────
    report_path = get_report_path("日报", make_daily_filename(today))

    result = {
        "meta": {
            "project_id":    project_id,
            "project_name":  _project_name(project_id),
            "date":          fmt_date_full(today.isoformat()),
            "weekday":       weekday_cn(today),
            "is_release_day": curr["is_release_day"],
            "warnings":      warnings,
        },
        "curr_version": curr_data,
        "next_version":  next_data,
        "report_path":   str(report_path),
    }

    log.info("日报数据组装完成。")
    return result


# ─── 保存报告文件 ─────────────────────────────────────────────────────────────

def save_daily_report(content: str, project_id: str, today: date | None = None) -> str:
    """
    把 Claude 生成的报告内容保存到本地文件。

    参数：
      content:    Claude 生成的 Markdown 格式报告文本
      project_id: 项目 ID，用于区分不同项目的报告目录
      today:      日期（默认今天），供测试时覆盖

    返回：保存路径字符串
    """
    ref      = today or date.today()
    filename = make_daily_filename(ref)
    path     = get_report_path("日报", filename)
    path.write_text(content, encoding="utf-8")
    log.info("日报已保存：%s", path)
    return str(path)


# ─── 辅助：项目名称 ───────────────────────────────────────────────────────────

def _project_name(project_id: str) -> str:
    """根据 project_id 返回显示名称。"""
    for name, pid in ACTIVE_PROJECTS.items():
        if pid == project_id:
            return name
    return f"项目{project_id}"
