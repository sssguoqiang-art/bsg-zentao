"""
tools/data_tools.py

原子数据工具层：从禅道接口取数据，精简字段后返回给 Claude。

设计原则：
  - 每个函数只做一件事：取数据 + 字段精简
  - 不做业务计算（延期判断、进度统计等在 calc_daily.py 做）
  - 精简字段：原始接口数据 60+ 字段/条，精简后保留 15 个以内
  - Token 消耗对比：原始 ~10万 tokens → 精简后 ~1万 tokens

对外暴露函数：
  get_versions(client, project_id)            → 版本信息
  get_version_requirements(client, vid, pid)  → 需求池数据（精简）
  get_version_bugs(client, vid, pid)          → Bug 数据（精简）
  get_member_tasks(client, username, ...)     → 按人查询任务（支持显示名/账号）
"""

from datetime import date
from typing import Optional

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import (
    PHPGROUP_DEPT_MAP, TASK_DETAIL_DEPT_MAP, REPORT_DEPTS_RAW,
    build_php_member_map, is_unordered, get_main_task_id,
    ONLINE_BUG_CLASSIFICATIONS,
    DEPT_TO_PROJECT, MEMBER_QUERY_DEPTS, DEPT_MAP,
)
from bsg_zentao.member_index import load_index, resolve_member
from bsg_zentao.utils import parse_date, remaining_days, is_release_day


# ─── 工具函数：版本识别逻辑 ───────────────────────────────────────────────────

def _identify_versions(execs: list[dict], project_id: str, today: date) -> dict:
    today_str = today.isoformat()
    valid = [
        e for e in execs
        if str(e.get("project", "")) == str(project_id)
        and e.get("end", "") not in ("", "0000-00-00")
    ]
    if not valid:
        return {"curr": None, "next": None, "prev": None}

    curr_candidates = [e for e in valid if e["end"] >= today_str]
    curr = min(curr_candidates, key=lambda e: e["end"]) if curr_candidates else None
    if curr is None:
        curr = max(valid, key=lambda e: e["end"])

    next_candidates = [e for e in valid if e["end"] > curr["end"]]
    nxt = min(next_candidates, key=lambda e: e["end"]) if next_candidates else None

    prev_candidates = [e for e in valid if e["end"] < curr["end"]]
    prev = max(prev_candidates, key=lambda e: e["end"]) if prev_candidates else None

    def _build(e: dict) -> dict:
        end_str = e["end"]
        return {
            "id":             str(e["id"]),
            "name":           e.get("name", "").strip(),
            "begin":          e.get("begin", ""),
            "end":            end_str,
            "status":         e.get("status", ""),
            "remaining_days": remaining_days(end_str, today),
            "is_release_day": is_release_day(end_str, today),
            "hours": {
                "estimate":  e.get("hours", {}).get("totalEstimate", 0),
                "consumed":  e.get("hours", {}).get("totalConsumed", 0),
                "left":      e.get("hours", {}).get("totalLeft", 0),
                "progress":  e.get("hours", {}).get("progress", 0),
            },
        }

    return {
        "curr": _build(curr),
        "next": _build(nxt) if nxt else None,
        "prev": _build(prev) if prev else None,
    }


# ─── 工具函数：子任务精简 ─────────────────────────────────────────────────────

def _slim_subtask(sub: dict) -> dict:
    return {
        "taskID":     sub.get("taskID", ""),
        "type":       sub.get("type", ""),
        "ordertype":  str(sub.get("ordertype", "") or ""),
        "status":     sub.get("status", ""),
        "deadline":   sub.get("deadline", "") or "",
        "estimate":   float(sub.get("estimate", 0) or 0),
        "consumed":   float(sub.get("consumed", 0) or 0),
        "left":       float(sub.get("left", 0) or 0),
        "finishedBy": sub.get("finishedBy", "") or "",
        "assignedTo": sub.get("assignedTo", "") or "",
        "deleted":    str(sub.get("deleted", "0")),
    }


# ─── 工具函数：需求池精简 ─────────────────────────────────────────────────────

def _slim_pool(pool: dict, pms: dict, bug_stat: dict, rejected_stat: dict) -> dict:
    task_id  = str(pool.get("taskID", "") or "")
    pm_user  = pool.get("pm", "") or ""
    bug_info = bug_stat.get(task_id, {})
    return {
        "id":             str(pool.get("id", "")),
        "task_id":        task_id,
        "title":          pool.get("title", "") or "",
        "category":       pool.get("category", "") or "",
        "task_status":    pool.get("taskStatus", "") or "",
        "is_unordered":   is_unordered(pool),
        "is_postponed":   bool(pool.get("isPostponed", False)),
        "env":            pool.get("env", "") or "",
        "deadline":       pool.get("deadline", "") or "",
        "delivery_date":  pool.get("deliveryDate", "") or "",
        "progress":       pool.get("progress", "0") or "0",
        "estimate":       float(pool.get("mainEstimate", 0) or 0),
        "consumed":       float(pool.get("mainConsumed", 0) or 0),
        "left":           float(pool.get("mainLeft", 0) or 0),
        "php_group":      str(pool.get("phpGroup", "") or ""),
        "pm":             pms.get(pm_user, pm_user) or "—",
        "pool_tags":      pool.get("poolTags", "") or "",
        "story_id":       str(pool.get("storyId", "") or "0"),
        "bug_total":      int(bug_info.get("total", 0)),
        "bug_main":       int(bug_info.get("mainTaskCount", 0)),
        "bug_sub":        int(bug_info.get("subTaskCount", 0)),
        "rejected_count": int(rejected_stat.get(task_id, 0) or 0),
        "record_date":    pool.get("recordDate", "") or (pool.get("taskOpenedDate", "") or "")[:10],
    }


def _slim_task_details(task_details: dict) -> dict:
    result = {}
    for task_id, dept_map in task_details.items():
        if not isinstance(dept_map, dict):
            continue
        slimmed = {}
        for dept_key, subs in dept_map.items():
            if not isinstance(subs, list):
                continue
            slimmed[dept_key] = [_slim_subtask(s) for s in subs if isinstance(s, dict)]
        result[str(task_id)] = slimmed
    return result


# ─── 对外函数1：版本信息 ──────────────────────────────────────────────────────

def get_versions(client: ZentaoClient, project_id: str, force_refresh: bool = False) -> dict:
    today = date.today()
    data  = client.fetch_versions(status="undone", force_refresh=force_refresh)
    execs = data.get("executionStats", [])
    result = _identify_versions(execs, project_id, today)
    result["project_id"] = project_id
    return result


# ─── 对外函数2：需求池数据 ────────────────────────────────────────────────────

def get_version_requirements(
    client: ZentaoClient,
    version_id: str,
    project_id: str,
    force_refresh: bool = False,
) -> dict:
    raw = client.fetch_pool(version_id, project_id, force_refresh=force_refresh)
    pools_raw      = raw.get("pools", [])
    task_details   = raw.get("taskDetails", {})
    pms            = raw.get("pms", {})
    users          = raw.get("users", {})
    bug_stat       = raw.get("associatedBugStat", {})
    rejected_stat  = raw.get("rejectedTaskStat", {})
    review_stat    = raw.get("statisticsReviewStory", {})

    php_member_map = build_php_member_map(users)
    slim_pools     = [_slim_pool(p, pms, bug_stat, rejected_stat) for p in pools_raw if isinstance(p, dict)]
    slim_details   = _slim_task_details(task_details)

    return {
        "version_id":        version_id,
        "project_id":        project_id,
        "pools":             slim_pools,
        "task_details":      slim_details,
        "php_member_map":    php_member_map,
        "pms":               pms,
        "review_stat":       review_stat,
        "current_delivered": raw.get("currentDelivered", ""),
    }


# ─── 对外函数3：Bug 数据 ──────────────────────────────────────────────────────

def get_version_bugs(
    client: ZentaoClient,
    version_id: str,
    project_id: str,
    force_refresh: bool = False,
) -> dict:
    raw = client.fetch_bugs(version_id, project_id, force_refresh=force_refresh)
    slim_bugs = [
        _slim_bug(b)
        for b in raw.get("bugs", [])
        if isinstance(b, dict) and b.get("deleted", "0") != "1"
    ]
    return {
        "version_id":  version_id,
        "bugs":        slim_bugs,
        "stat":        raw.get("stat", {}),
        "dept_review": raw.get("deptReview", {}),
    }


def _slim_bug(b: dict) -> dict:
    return {
        "id":             str(b.get("id", "")),
        "title":          b.get("title", "") or "",
        "classification": str(b.get("classification", "") or ""),
        "type":           b.get("type", "") or "",
        "status":         b.get("status", "") or "",
        "severity":       str(b.get("severity", "") or ""),
        "resolution":     b.get("resolution", "") or "",
        "is_typical":     str(b.get("isTypical", "0")) == "1",
        "is_dispute":     str(b.get("isDispute", "0")) == "1",
        "deadline":       b.get("deadline", "") or "",
        "opened_date":    b.get("openedDate", "") or "",
        "main_task_id":   get_main_task_id(b),
        "owner_dept":     b.get("ownerDept", "") or "",
        "cause_analysis": (b.get("causeAnalysis") or "").strip(),
        "dispute_remark": (b.get("disputeRemark") or "").strip(),
        "tracing_back":   (b.get("tracingBack") or "").strip(),
        "exclusion_reason": (b.get("exclusionReason") or "").strip(),
        "scope_influence": (b.get("scopeInfluence") or "").strip(),
        "phenomenon":     (b.get("phenomenon") or "").strip(),
        "demand":         (b.get("demand") or "").strip(),
        "use_case":       (b.get("useCase") or "").strip(),
    }


# ─── 对外函数4：历史版本趋势数据 ─────────────────────────────────────────────

def get_version_history(
    client: ZentaoClient,
    current_vid: str,
    project_id: str,
    max_count: int = 4,
    force_refresh: bool = False,
) -> list[dict]:
    import re as _re
    INVALID_NAMES = {"W5", "平台组"}

    undone_data = client.fetch_versions(status="undone", force_refresh=force_refresh)
    closed_data = client.fetch_versions(status="closed", force_refresh=force_refresh)
    all_execs   = (
        undone_data.get("executionStats", [])
        + closed_data.get("executionStats", [])
    )

    seen_ids: set[str] = set()
    unique_execs = []
    for e in all_execs:
        eid = str(e.get("id", ""))
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            unique_execs.append(e)

    valid_vids = sorted(
        [
            int(e["id"]) for e in unique_execs
            if str(e.get("project", "")) == str(project_id)
            and str(e["id"]) != str(current_vid)
            and int(e["id"]) < int(current_vid)
            and e.get("name", "").strip() not in INVALID_NAMES
            and _re.search(r'（\d{4}）', e.get("name", ""))
            and e.get("end", "") not in ("", "0000-00-00")
        ],
        reverse=True,
    )[:max_count]

    id_to_name = {str(e["id"]): e.get("name", "").strip() for e in unique_execs}

    history = []
    for vid_int in reversed(valid_vids):
        vid_str = str(vid_int)
        vname   = id_to_name.get(vid_str, vid_str)

        bug_result  = get_version_bugs(client, vid_str, project_id, force_refresh=force_refresh)
        bugs        = bug_result["bugs"]
        stat        = bug_result["stat"]
        dept_review = bug_result["dept_review"]

        ext_total = int(stat.get("classification_1", 0)) + int(stat.get("classification_2", 0))
        int_total = int(stat.get("classification_4", 0)) + int(stat.get("classification_5", 0))

        ext_review_bugs  = [b for b in bugs if b.get("classification") in ONLINE_BUG_CLASSIFICATIONS and "performance" not in (b.get("type") or "")]
        ext_review_count = len(ext_review_bugs)

        test_dept_count = 0
        for b in ext_review_bugs:
            owner = (b.get("owner_dept") or "").strip()
            if owner:
                if "45" in owner.split(","):
                    test_dept_count += 1
            else:
                dr = dept_review.get(str(b.get("id", "")), {})
                if 45 in (dr.get("depts") or []) or "45" in [str(d) for d in (dr.get("depts") or [])]:
                    test_dept_count += 1

        pool_result  = get_version_requirements(client, vid_str, project_id, force_refresh=force_refresh)
        active_pools = [p for p in pool_result["pools"] if p.get("task_status") != "cancel"]
        ext_reqs = sum(1 for p in active_pools if p.get("category") in ("version", "operation"))
        int_reqs = sum(1 for p in active_pools if p.get("category") == "internal")

        history.append({
            "version_id":     vid_str,
            "version_name":   vname,
            "ext_bug_total":  ext_total,
            "ext_bug_review": ext_review_count,
            "test_dept_bugs": test_dept_count,
            "int_bugs":       int_total,
            "ext_reqs":       ext_reqs,
            "int_reqs":       int_reqs,
        })

    return history


# ─── 对外函数5：按人查询任务 ────────────────────────────────────────────────────

def get_member_tasks(
    client: ZentaoClient,
    username: str,
    begin: str,
    end: str,
    dept_id: str = "",
    project_id: str = "",
    execution_id: str = "",
    force_refresh: bool = False,
) -> dict:
    """
    按人查询任务详情。

    username 支持两种输入方式：
      - 禅道账号（如 chenyi）：直接查询
      - 显示名（如 陈益）    ：自动通过本地成员索引解析为账号

    解析策略：
      1. 输入含中文 → 尝试本地成员索引解析
      2. 已知 dept_id → 直接查该部门（跳过多部门遍历）
      3. 无 dept_id → 从索引取 dept_id，再逐一遍历 MEMBER_QUERY_DEPTS 兜底

    返回结构：
    {
        "username":      "chenyi",
        "display_name":  "陈益",
        "dept_id":       "47",
        "dept_name":     "PHP2部",
        "period":        {"begin": "2026-04-15", "end": "2026-04-15"},
        "task_summary":  {"total": N, "estimate": N, "consumed": N, ...},
        "tasks":         [精简任务列表],
        "work_times":    {"total_hours": N, "by_day": {"15": N, ...}},
        "found_in_dept": True,
    }
    找不到时返回 {"found_in_dept": False, "message": "说明"}
    """

    # ── Step 1：名字解析 ─────────────────────────────────────────────────────
    is_display_name    = any("\u4e00" <= c <= "\u9fff" for c in username)
    resolved_username  = username
    resolved_display   = username
    resolved_dept_id   = dept_id
    resolved_dept_name = DEPT_MAP.get(dept_id, dept_id) if dept_id else ""

    if is_display_name:
        idx    = load_index()
        result = resolve_member(username, idx)

        if result is None:
            return {
                "found_in_dept": False,
                "username":      username,
                "period":        {"begin": begin, "end": end},
                "message": (
                    f"成员索引中找不到「{username}」。"
                    f"索引共 {idx.get('total', 0)} 人，最后更新：{idx.get('built_at', '未知')}。"
                    f"如果此人是新入职，请先调用 zentao_build_member_index 刷新索引。"
                ),
            }

        if result.get("_ambiguous"):
            candidates = result["candidates"]
            return {
                "found_in_dept": False,
                "username":      username,
                "period":        {"begin": begin, "end": end},
                "message": (
                    f"「{username}」匹配到多个成员，请明确指定禅道账号：" +
                    "、".join(f"{c['display_name']}（{c['username']}）" for c in candidates)
                ),
                "candidates": candidates,
            }

        resolved_username  = result["username"]
        resolved_display   = result["display_name"]
        if not resolved_dept_id:
            resolved_dept_id   = result["dept_id"]
            resolved_dept_name = result["dept_name"]

    # ── Step 2：确定要查询的部门列表 ─────────────────────────────────────────
    depts_to_try = [resolved_dept_id] if resolved_dept_id else MEMBER_QUERY_DEPTS

    # ── Step 3：调用接口查询 ──────────────────────────────────────────────────
    for d in depts_to_try:
        pid = project_id or DEPT_TO_PROJECT.get(d, "10")
        raw = client.fetch_workassign(
            dept=d, project=pid,
            begin=begin, end=end,
            user=resolved_username,
            execution=execution_id or "",
            force_refresh=force_refresh,
        )

        task_users = raw.get("taskUsers", {})
        if resolved_username not in task_users:
            continue

        info = task_users[resolved_username]

        # 账号输入时，从返回数据补充显示名
        if not is_display_name:
            users_flat: dict[str, str] = {}
            for group_members in raw.get("users", {}).values():
                if isinstance(group_members, dict):
                    users_flat.update(group_members)
            resolved_display = users_flat.get(resolved_username, resolved_username)

        wt          = raw.get("workTimes", {}).get(resolved_username, {})
        total_hours = sum(float(v or 0) for v in wt.values())
        by_day      = {k: float(v or 0) for k, v in wt.items() if float(v or 0) > 0}

        slim_tasks = [
            {
                "id":        str(t.get("id", "")),
                "name":      t.get("name", "") or "",
                "status":    t.get("status", "") or "",
                "estimate":  float(t.get("estimate", 0) or 0),
                "consumed":  float(t.get("consumed", 0) or 0),
                "left":      float(t.get("left", 0) or 0),
                "deadline":  t.get("deadline", "") or "",
                "ordertype": str(t.get("ordertype", "") or ""),
                "req_name":  t.get("reqName", "") or "",
            }
            for t in (info.get("task") or [])
            if isinstance(t, dict)
        ]

        dept_result = resolved_dept_id if resolved_dept_id else d
        return {
            "username":     resolved_username,
            "display_name": resolved_display,
            "dept_id":      dept_result,
            "dept_name":    DEPT_MAP.get(dept_result, dept_result),
            "period":       {"begin": begin, "end": end},
            "task_summary": {
                "total":         int(info.get("count", 0) or 0),
                "estimate":      float(info.get("estimate", 0) or 0),
                "consumed":      float(info.get("consumed", 0) or 0),
                "done_estimate": float(info.get("done_estimate", 0) or 0),
                "done_consumed": float(info.get("done_consumed", 0) or 0),
            },
            "tasks":      slim_tasks,
            "work_times": {
                "total_hours": round(total_hours, 1),
                "by_day":      by_day,
            },
            "found_in_dept": True,
        }

    return {
        "found_in_dept": False,
        "username":      resolved_username,
        "display_name":  resolved_display,
        "period":        {"begin": begin, "end": end},
        "message": (
            f"已尝试部门列表 {depts_to_try}，均未找到用户 {resolved_username}（{resolved_display}）的任务数据。"
            f"可能原因：该时间范围内无任务分配。"
        ),
    }
