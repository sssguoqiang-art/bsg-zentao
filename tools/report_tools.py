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
from bsg_zentao.constants import ACTIVE_PROJECTS, REPORT_DEPTS_RAW, STATUS_DONE, get_env_display, to_display
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
    is_pool_in_version, _get_followup_subtasks,
)

log = logging.getLogger(__name__)

_ZERO_DT_VALUES = {"", "0000-00-00", "0000-00-00 00:00:00"}


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


def _has_valid_dt(value: str) -> bool:
    raw = (value or "").strip()
    return raw not in _ZERO_DT_VALUES


def _display_dt(value: str) -> str:
    raw = (value or "").strip()
    if not _has_valid_dt(raw):
        return ""
    return raw[5:16] if len(raw) >= 16 else raw[5:10]


def _task_view_child_map(task_view: dict) -> dict[str, dict]:
    task = (task_view or {}).get("task", {}) or {}
    children = task.get("children", {}) or {}
    result: dict[str, dict] = {}
    for key, child in children.items():
        if not isinstance(child, dict):
            continue
        child_id = str(child.get("id") or key or "")
        if child_id:
            result[child_id] = child
    return result


def _task_view_started_action_map(task_view: dict) -> dict[str, str]:
    started_map: dict[str, str] = {}
    for action in ((task_view or {}).get("subActions") or {}).values():
        if not isinstance(action, dict):
            continue
        if (action.get("action", "") or "") != "started":
            continue
        object_id = str(action.get("objectID", "") or "")
        started_at = (action.get("date", "") or "")
        if not object_id or not _has_valid_dt(started_at):
            continue
        prev = started_map.get(object_id, "")
        if not prev or started_at < prev:
            started_map[object_id] = started_at
    return started_map


def _pick_testing_started(child: dict, started_action_map: dict[str, str]) -> tuple[str, str]:
    real_started = (child or {}).get("realStarted", "") or ""
    if _has_valid_dt(real_started):
        return real_started, "realStarted"

    child_id = str((child or {}).get("id", "") or "")
    started_action = started_action_map.get(child_id, "")
    if _has_valid_dt(started_action):
        return started_action, "startedAction"

    for field, source in (("openedDate", "openedDate"), ("assignedDate", "assignedDate")):
        value = (child or {}).get(field, "") or ""
        if _has_valid_dt(value):
            return value, source
    return "", ""


def _merge_status_summary(env: str) -> str:
    if env == "require":
        return "当前仍待合并"
    if env == "noRequire":
        return "当前无需合并"
    if env == "deliver":
        return "当前已交付测试"
    if env == "fixDone":
        return "当前测试结果已回写"
    if env == "devDone":
        return "当前开发已完成"
    return f"当前状态：{get_env_display(env)}"


def _testing_handoff_summary(started_at: str, started_source: str) -> str:
    if not started_at:
        return ""
    display = _display_dt(started_at)
    if started_source == "realStarted":
        return f"测试组 {display} 开始接手"
    if started_source == "startedAction":
        return f"测试组 {display} 开始接手"
    if started_source == "openedDate":
        return f"测试单 {display} 已创建，暂未看到开始时间"
    if started_source == "assignedDate":
        return f"测试单 {display} 已分配，暂未看到开始时间"
    return ""


def _enrich_testing_followups(
    client: ZentaoClient,
    rows: list[dict],
    pools: list[dict],
    task_details: dict,
    php_member_map: dict,
    *,
    force_refresh: bool = False,
) -> list[dict]:
    """
    为“测试收口中”补充过程维度信息：
      - 各开发/美术部门最近完成时间
      - 最近一笔交付时间
      - 测试开始接手时间
      - 当前合并状态的人话摘要

    说明：
      - 开发完成时间：依赖 task/view.children.finishedDate
      - 测试开始时间：优先 realStarted，缺失时回退 openedDate / assignedDate
      - 合并时间：接口中暂无稳定字段，因此只输出当前合并状态，不输出时间
    """
    if not rows:
        return rows

    pool_map = {str(p.get("task_id", "") or ""): p for p in pools}
    cache: dict[str, dict] = {}

    for row in rows:
        task_id = str(row.get("task_id", "") or "")
        pool = pool_map.get(task_id)
        if not task_id or not pool:
            continue

        if task_id not in cache:
            cache[task_id] = client.fetch_task_view(task_id, force_refresh=force_refresh)
        child_map = _task_view_child_map(cache[task_id])
        started_action_map = _task_view_started_action_map(cache[task_id])
        dept_subs = _get_followup_subtasks(pool, task_details, php_member_map)

        delivery_rows = []
        latest_delivery_at = ""

        for dept_raw in REPORT_DEPTS_RAW:
            subs = dept_subs.get(dept_raw, [])
            finished_times: list[str] = []
            for sub in subs:
                child = child_map.get(str(sub.get("taskID", "") or ""))
                if not child:
                    continue
                status = (child.get("status", "") or sub.get("status", "") or "")
                finished_at = (child.get("finishedDate", "") or "")
                if status in (STATUS_DONE - {"cancel"}) and _has_valid_dt(finished_at):
                    finished_times.append(finished_at)

            if not finished_times:
                continue

            dept_finished_at = max(finished_times)
            delivery_rows.append({
                "dept_raw": dept_raw,
                "dept": to_display(dept_raw),
                "finished_at": dept_finished_at,
                "finished_display": _display_dt(dept_finished_at),
            })
            if not latest_delivery_at or dept_finished_at > latest_delivery_at:
                latest_delivery_at = dept_finished_at

        qa_candidates: list[tuple[str, str]] = []
        for sub in dept_subs.get("测试部", []):
            child = child_map.get(str(sub.get("taskID", "") or ""))
            if not child:
                continue
            started_at, source = _pick_testing_started(child, started_action_map)
            if started_at:
                qa_candidates.append((started_at, source))

        testing_started_at = ""
        testing_started_source = ""
        if qa_candidates:
            testing_started_at, testing_started_source = min(qa_candidates, key=lambda item: item[0])

        row["delivery_depts"] = delivery_rows
        row["delivery_summary"] = "；".join(
            f"{item['dept']} {item['finished_display']} 完成" for item in delivery_rows
        )
        row["latest_delivery_at"] = latest_delivery_at
        row["latest_delivery_display"] = _display_dt(latest_delivery_at)
        row["merge_status_summary"] = _merge_status_summary(row.get("env", "") or "")
        row["testing_started_at"] = testing_started_at
        row["testing_started_display"] = _display_dt(testing_started_at)
        row["testing_started_source"] = testing_started_source
        row["testing_handoff_summary"] = _testing_handoff_summary(testing_started_at, testing_started_source)

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
        "testing_followups": _enrich_testing_followups(
            client,
            calc_testing_followups(curr_pools, curr_task_details, php_member_map, version_end),
            curr_pools,
            curr_task_details,
            php_member_map,
            force_refresh=force_refresh,
        ),
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
