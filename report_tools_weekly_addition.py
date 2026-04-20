"""
report_tools.py 新增部分 —— 周汇总

在原 report_tools.py 末尾追加以下内容：
  assemble_weekly_report(client)  → 周汇总完整数据包
  save_weekly_report(content)     → 保存周报文件
"""

import logging
from datetime import date

from bsg_zentao.client import ZentaoClient
from bsg_zentao.utils import (
    get_report_path,
    weekday_cn, fmt_date_full,
)
from tools.data_tools import get_versions, get_version_requirements, get_version_bugs
from tools.calc_daily import (
    calc_summary, calc_dept_progress, calc_online_bugs, calc_next_workload,
)
from tools.calc_weekly import (
    calc_version_delivery, calc_version_adjustments,
    calc_key_requirements, calc_weekly_delay,
)
from tools.report_tools import _format_dept_progress, _format_dept_workload, _project_name

log = logging.getLogger(__name__)

_WEEKLY_REPORT_LABELS = {
    "weekly_summary": "效能周汇总",
    "weekly_report": "效能周报",
}


def _make_weekly_variant_filename(ref: date, report_type: str) -> str:
    year, week, _ = ref.isocalendar()
    label = _WEEKLY_REPORT_LABELS.get(report_type)
    if not label:
        raise ValueError(f"不支持的周报类型：{report_type}")
    return f"{year}{week:02d}_{label}.md"

# ─── 单项目周数据组装 ─────────────────────────────────────────────────────────

def _assemble_project_weekly(
    client: ZentaoClient,
    project_id: str,
    today: date,
    force_refresh: bool = False,
) -> dict:
    """
    组装单个项目的周汇总数据。
    返回 prev / curr / next 三个版本的完整数据包。
    """
    log.info("  [%s] 识别版本…", project_id)
    versions = get_versions(client, project_id, force_refresh=force_refresh)
    prev = versions.get("prev")
    curr = versions.get("curr")
    nxt  = versions.get("next")

    warnings = []

    # ── 上一版本 ──────────────────────────────────────────────────────────────
    prev_data = None
    if prev:
        log.info("  [%s] 拉取上一版本需求池（%s）…", project_id, prev["name"])
        prev_req   = get_version_requirements(client, prev["id"], project_id, force_refresh=force_refresh)
        prev_pools = [p for p in prev_req["pools"] if p.get("task_status") != "cancel"]
        prev_data  = {
            "info":     prev,
            "delivery": calc_version_delivery(prev_pools),
            "summary":  calc_summary(prev_pools),
        }
    else:
        warnings.append("⚠️ 未找到上一版本，可能版本已归档。")

    # ── 当前版本 ──────────────────────────────────────────────────────────────
    curr_data = None
    if curr:
        log.info("  [%s] 拉取当前版本需求池（%s）…", project_id, curr["name"])
        curr_req       = get_version_requirements(client, curr["id"], project_id, force_refresh=force_refresh)
        curr_bugs_data = get_version_bugs(client, curr["id"], project_id, force_refresh=force_refresh)

        curr_pools      = [p for p in curr_req["pools"] if p.get("task_status") != "cancel"]
        curr_td         = curr_req["task_details"]
        php_map         = curr_req["php_member_map"]
        bugs            = curr_bugs_data["bugs"]

        curr_data = {
            "info":            curr,
            "summary":         calc_summary(curr_pools),
            "dept_progress":   _format_dept_progress(
                                   calc_dept_progress(curr_pools, curr_td, curr["end"], php_map)
                               ),
            "delay_list":      calc_weekly_delay(curr_pools, curr_td, php_map, today),
            "adjustments":     calc_version_adjustments(curr_pools),
            "key_requirements":calc_key_requirements(curr_pools),
            "online_bugs":     calc_online_bugs(bugs),
            "review_stat":     curr_req.get("review_stat", {}),
        }
    else:
        warnings.append("⚠️ 未找到当前版本，请检查禅道账号权限。")

    # ── 下一版本 ──────────────────────────────────────────────────────────────
    next_data = None
    if nxt:
        log.info("  [%s] 拉取下一版本需求池（%s）…", project_id, nxt["name"])
        next_req   = get_version_requirements(client, nxt["id"], project_id, force_refresh=force_refresh)
        next_pools = [p for p in next_req["pools"] if p.get("task_status") != "cancel"]
        next_td    = next_req["task_details"]
        next_php   = next_req["php_member_map"]
        workload   = calc_next_workload(next_pools, next_td, next_php, nxt["end"])

        next_data = {
            "info":           nxt,
            "summary": {
                "total":     workload["total"],
                "ordered":   workload["ordered"],
                "unordered": workload["unordered"],
            },
            "dept_workload":  _format_dept_workload(workload["dept_workload"]),
            "unordered_list": workload["unordered_list"],
            "key_requirements": calc_key_requirements(next_pools),
        }

    return {
        "project_id":   project_id,
        "project_name": _project_name(project_id),
        "prev_version": prev_data,
        "curr_version": curr_data,
        "next_version": next_data,
        "warnings":     warnings,
    }


# ─── 主函数：组装双项目周汇总数据包 ──────────────────────────────────────────

def assemble_weekly_report(client: ZentaoClient, force_refresh: bool = False) -> dict:
    """
    组装周汇总所需的全部数据，包含平台项目和游戏项目。

    返回结构：
    {
        "meta": {
            "date":     "4月9日",
            "weekday":  "周四",
            "warnings": [...],
        },
        "platform": {   # 平台项目（project_id=10）
            "project_id":   "10",
            "project_name": "平台项目",
            "prev_version": {info, delivery, summary} | None,
            "curr_version": {info, summary, dept_progress, delay_list,
                             adjustments, key_requirements, online_bugs} | None,
            "next_version": {info, summary, dept_workload,
                             unordered_list, key_requirements} | None,
            "warnings": [...],
        },
        "game": {       # 游戏项目（project_id=51）
            ...          # 同上结构
        },
        "report_paths": {
            "weekly_summary": "…/报告/周汇总/202616_效能周汇总.md",
            "weekly_report":  "…/报告/周汇总/202616_效能周报.md",
        },
    }

    Claude 拿到此数据后：
      1. 分别为平台/游戏项目生成版本状态表、延期表、调整表、重点需求表
      2. 生成风险研判和待讨论问题
      3. 专题进展（Web5/性能/AI伴侣/招聘）使用占位符，提示人工补充
      4. 效能工作推进使用占位符，提示人工补充
      5. 调用 save_weekly_report() 保存文件
    """
    today = date.today()
    log.info("开始组装周汇总数据…")

    all_warnings = []

    log.info("  ── 平台项目 ──")
    platform_data = _assemble_project_weekly(client, "10", today, force_refresh=force_refresh)
    all_warnings.extend(platform_data.get("warnings", []))

    log.info("  ── 游戏项目 ──")
    game_data = _assemble_project_weekly(client, "51", today, force_refresh=force_refresh)
    all_warnings.extend(game_data.get("warnings", []))

    summary_path = get_report_path("周汇总", _make_weekly_variant_filename(today, "weekly_summary"))
    report_path = get_report_path("周汇总", _make_weekly_variant_filename(today, "weekly_report"))

    log.info("周汇总数据组装完成。")
    return {
        "meta": {
            "date":     fmt_date_full(today.isoformat()),
            "weekday":  weekday_cn(today),
            "warnings": all_warnings,
        },
        "platform":    platform_data,
        "game":        game_data,
        "report_path": str(summary_path),
        "report_paths": {
            "weekly_summary": str(summary_path),
            "weekly_report": str(report_path),
        },
    }


# ─── 保存周报文件 ─────────────────────────────────────────────────────────────

def save_weekly_report(
    content: str,
    report_type: str = "weekly_summary",
    today: date | None = None,
) -> str:
    """
    把 Claude 生成的周汇总/周报内容保存到本地文件。
    返回保存路径字符串。
    """
    ref      = today or date.today()
    filename = _make_weekly_variant_filename(ref, report_type)
    path     = get_report_path("周汇总", filename)
    path.write_text(content, encoding="utf-8")
    log.info("%s已保存：%s", _WEEKLY_REPORT_LABELS.get(report_type, "周报"), path)
    return str(path)
