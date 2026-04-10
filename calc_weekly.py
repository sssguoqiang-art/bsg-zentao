"""
tools/calc_weekly.py

周汇总业务计算层。
复用 calc_daily 中已有的函数，补充周维度特有的计算逻辑。

对外函数：
  calc_version_delivery(pools)                              → 上一版本交付统计
  calc_version_adjustments(pools)                           → 版本调整情况
  calc_key_requirements(pools)                              → 重点需求跟进
  calc_weekly_delay(pools, task_details, php_member_map, today) → 延期情况（含天数）
"""

from datetime import date

from bsg_zentao.constants import (
    CATEGORY_DISPLAY, STATUS_DONE, STATUS_TESTING, STATUS_DEV,
    TAG_CHADAN, TAG_JIDAN, TAG_YINGYING, TAG_KUABAN,
    has_tag, to_display,
)
from bsg_zentao.utils import fmt_date, days_overdue, parse_date

# 复用日报计算层的通用函数
from tools.calc_daily import _get_php_dept


# ─── 1. 上一版本交付统计 ──────────────────────────────────────────────────────

def calc_version_delivery(pools: list[dict]) -> dict:
    """
    统计上一版本的交付完成情况。

    返回：
    {
        "total":      N,    # 总需求数（过滤 cancel）
        "done":       N,    # 已完成（done/closed）
        "testing":    N,    # 验收中（waittest/testing）
        "dev":        N,    # 还在开发（wait/doing/pause 等）
        "done_rate":  "75%",# 完成率
        "rework":     N,    # 返工数（is_postponed=True 且已完成）
        "rework_list":[{title, category}],  # 返工需求明细
    }
    """
    active = [p for p in pools if p.get("task_status") != "cancel"]
    total   = len(active)
    done    = sum(1 for p in active if p.get("task_status") in STATUS_DONE - {"cancel"})
    testing = sum(1 for p in active if p.get("task_status") in STATUS_TESTING)
    dev     = sum(1 for p in active if p.get("task_status") in STATUS_DEV)

    done_rate = f"{int(done / total * 100)}%" if total > 0 else "—"

    # 返工：已下单 + 曾被推迟（is_postponed=True），表示本版本有过返工
    rework_list = [
        {
            "title":    p.get("title", ""),
            "category": CATEGORY_DISPLAY.get(p.get("category", ""), "—"),
        }
        for p in active
        if p.get("is_postponed", False)
    ]

    return {
        "total":       total,
        "done":        done,
        "testing":     testing,
        "dev":         dev,
        "done_rate":   done_rate,
        "rework":      len(rework_list),
        "rework_list": rework_list,
    }


# ─── 2. 版本调整情况 ──────────────────────────────────────────────────────────

def calc_version_adjustments(pools: list[dict]) -> list[dict]:
    """
    识别本版本中被推迟/调整排期的需求。

    判断逻辑：
      - is_postponed=True：PM 标记了推迟
      - pool_tags 含 TAG_APPEND（追加版本=21）：被追加到本版本
      - pool_tags 含 TAG_KUABAN（跨版本=15）：跨版本持续推进

    返回字段：title, category, adjustment_type（推迟/追加/跨版本）, pool_tags
    按 adjustment_type 分组展示。
    """
    rows = []
    for pool in pools:
        if pool.get("task_status") == "cancel":
            continue

        adj_type = None
        if pool.get("is_postponed", False):
            adj_type = "推迟"
        elif has_tag(pool, "21"):   # TAG_APPEND
            adj_type = "追加"
        elif has_tag(pool, TAG_KUABAN):
            adj_type = "跨版本"

        if not adj_type:
            continue

        rows.append({
            "title":           pool.get("title", ""),
            "category":        CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "adjustment_type": adj_type,
            "task_status":     pool.get("task_status", ""),
        })

    # 推迟优先，跨版本最后
    _order = {"推迟": 0, "追加": 1, "跨版本": 2}
    rows.sort(key=lambda r: _order.get(r["adjustment_type"], 9))
    return rows


# ─── 3. 重点需求跟进 ──────────────────────────────────────────────────────────

def calc_key_requirements(pools: list[dict]) -> list[dict]:
    """
    筛选重点需求（运营重点、插单、急单、跨版本）。

    标签过滤（pool_tags 逗号分隔的 tag ID）：
      TAG_YINGYING = "17"  运营重点
      TAG_CHADAN   = "11"  插单
      TAG_JIDAN    = "19"  急单
      TAG_KUABAN   = "15"  跨版本

    返回字段：title, category, task_status, pm, tags_display（标签中文列表）
    只返回未完成的重点需求（已完成的无需持续关注）。
    """
    TAG_LABELS = {
        TAG_YINGYING: "运营重点",
        TAG_CHADAN:   "插单",
        TAG_JIDAN:    "急单",
        TAG_KUABAN:   "跨版本",
    }

    rows = []
    for pool in pools:
        if pool.get("task_status") in STATUS_DONE:
            continue
        if pool.get("task_status") == "cancel":
            continue

        matched_tags = [
            label for tid, label in TAG_LABELS.items()
            if has_tag(pool, tid)
        ]
        if not matched_tags:
            continue

        rows.append({
            "title":        pool.get("title", ""),
            "category":     CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "task_status":  pool.get("task_status", ""),
            "pm":           pool.get("pm", "—"),
            "tags_display": "、".join(matched_tags),
            "deadline":     pool.get("deadline", "") or "",
            "deadline_fmt": fmt_date(pool.get("deadline", "") or ""),
        })

    return rows


# ─── 4. 周维度延期情况 ────────────────────────────────────────────────────────

def calc_weekly_delay(
    pools: list[dict],
    task_details: dict,
    php_member_map: dict,
    today: date,
) -> list[dict]:
    """
    周维度延期情况：已超过截止日且未完成的需求。

    与日报的 calc_delay_list 区别：
      - 周报按超期天数降序（超期最久的排前面）
      - 额外返回 dept（责任部门）字段，便于表格展示
      - 不区分今日截止 vs 其他（周报粒度更粗）

    过滤条件：
      - task_status 不在 STATUS_DONE
      - deadline < today（已超期，不包含今天）
      - deadline 字段有效

    返回字段：title, category, deadline_fmt, overdue_days, task_status, dept
    """
    today_str = today.isoformat()
    rows = []

    for pool in pools:
        if pool.get("task_status") in STATUS_DONE:
            continue
        if pool.get("task_status") == "cancel":
            continue

        dl = pool.get("deadline", "") or ""
        if not dl or dl == "0000-00-00" or dl >= today_str:
            continue

        overdue = days_overdue(dl, today)
        if overdue <= 0:
            continue

        # 推断责任部门
        dept = _infer_dept(pool, task_details, php_member_map)

        rows.append({
            "title":        pool.get("title", ""),
            "category":     CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "deadline_fmt": fmt_date(dl),
            "overdue_days": overdue,
            "task_status":  pool.get("task_status", ""),
            "dept":         dept,
        })

    rows.sort(key=lambda r: r["overdue_days"], reverse=True)
    return rows


def _infer_dept(pool: dict, task_details: dict, php_member_map: dict) -> str:
    """
    推断需求的主要责任部门，用于周报延期表格展示。
    优先 PHP 归属推断，其次通过 task_details 判断有哪些部门参与。
    """
    from tools.calc_daily import _get_dept_subtasks
    from bsg_zentao.constants import REPORT_DEPTS_RAW

    dept_subs = _get_dept_subtasks(pool, task_details, php_member_map)
    if not dept_subs:
        return "—"

    # 取有未完成子任务的部门，按 REPORT_DEPTS_RAW 顺序返回第一个
    for dept in REPORT_DEPTS_RAW:
        subs = dept_subs.get(dept, [])
        if any(s.get("status") in STATUS_DEV for s in subs):
            return to_display(dept)

    # 全部完成则取第一个参与部门
    for dept in REPORT_DEPTS_RAW:
        if dept in dept_subs:
            return to_display(dept)

    return "—"
