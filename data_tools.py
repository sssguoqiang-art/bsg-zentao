"""
tools/data_tools.py

原子数据工具层：从禅道接口取数据，精简字段后返回给 Claude。

设计原则：
  - 每个函数只做一件事：取数据 + 字段精简
  - 不做业务计算（延期判断、进度统计等在 calc_daily.py 做）
  - 精简字段：原始接口数据 60+ 字段/条，精简后保留 15 个以内
  - Token 消耗对比：原始 ~10万 tokens → 精简后 ~1万 tokens

对外暴露三个函数：
  get_versions(client, project_id)            → 版本信息
  get_version_requirements(client, vid, pid)  → 需求池数据（精简）
  get_version_bugs(client, vid, pid)          → Bug 数据（精简）
"""

from datetime import date
from typing import Optional

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import (
    PHPGROUP_DEPT_MAP, TASK_DETAIL_DEPT_MAP, REPORT_DEPTS_RAW,
    build_php_member_map, is_unordered, get_main_task_id,
)
from bsg_zentao.utils import parse_date, remaining_days, is_release_day


# ─── 工具函数：版本识别逻辑 ───────────────────────────────────────────────────

def _identify_versions(execs: list[dict], project_id: str, today: date) -> dict:
    """
    从版本列表中识别当前版本、下一版本、上一版本。

    识别规则（已在 bsg-zentao-api Skill 中确认）：
      当前版本：end >= today 中 end 最小的（最近即将到来的交付版本）
      下一版本：end > curr.end 中 end 最小的
      上一版本：end < today 中 end 最大的（最近已交付的版本）

    过滤条件：
      - project 与 project_id 匹配
      - end 字段有效（非空、非 "0000-00-00"）
    """
    today_str = today.isoformat()

    # 只取当前项目的版本，过滤无效 end 日期
    valid = [
        e for e in execs
        if str(e.get("project", "")) == str(project_id)
        and e.get("end", "") not in ("", "0000-00-00")
    ]

    if not valid:
        return {"curr": None, "next": None, "prev": None}

    # 当前版本
    curr_candidates = [e for e in valid if e["end"] >= today_str]
    curr = min(curr_candidates, key=lambda e: e["end"]) if curr_candidates else None

    # 没有未来版本时，取 end 最大的作为当前版本（避免返回 None）
    if curr is None:
        curr = max(valid, key=lambda e: e["end"])

    # 下一版本
    next_candidates = [e for e in valid if e["end"] > curr["end"]]
    nxt = min(next_candidates, key=lambda e: e["end"]) if next_candidates else None

    # 上一版本
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
            # 工时摘要（来自 hours 子对象）
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
    """
    精简单个子任务对象，只保留计算所需字段。

    保留字段说明：
      taskID      → 子任务 ID
      type        → 职能类型（devel/art/web/cocos/qa/design）
      ordertype   → 单子类型编码（2=开发单、11=制作单、13=测试单、12=验收单 等）
      status      → 任务状态
      deadline    → 截止日期（延期判断用）
      estimate    → 预估工时
      consumed    → 已消耗工时
      left        → 剩余工时
      finishedBy  → 完成人（PHP 组归属推断用）
      assignedTo  → 当前指派人（PHP 组归属备用）
      deleted     → 是否已删除（"1"=已删除，过滤时排除）
    """
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
    """
    精简单个需求池对象，只保留报告所需字段。
    原始对象 60+ 字段 → 精简后 ~18 个字段。

    关键字段说明：
      deadline vs deliveryDate：
        deadline     = 任务实际截止日期，用于版本交付过滤和延期判断
        deliveryDate = PM 填写的计划日期，用于规划展示
        两个字段并存，含义不同，不可混用（Skill 已明确）

      php_dept：
        PHP 部门归属，优先用 phpGroup 字段推断
        phpGroup 为空时，调用方用 php_member_map 从子任务执行人推断
        这里只存原始 phpGroup，归属推断在 calc 层做

      bug_total：
        来自 associatedBugStat[taskID].total（已实测确认结构）

      rejected_count：
        来自 rejectedTaskStat[taskID]（驳回专栏使用）
    """
    task_id  = str(pool.get("taskID", "") or "")
    pm_user  = pool.get("pm", "") or ""
    bug_info = bug_stat.get(task_id, {})

    return {
        # 基础标识
        "id":           str(pool.get("id", "")),
        "task_id":      task_id,
        "title":        pool.get("title", "") or "",
        # 分类
        "category":     pool.get("category", "") or "",    # version/operation/internal
        # 状态
        "task_status":  pool.get("taskStatus", "") or "",
        "is_unordered": is_unordered(pool),
        "is_postponed": bool(pool.get("isPostponed", False)),
        "env":          pool.get("env", "") or "",         # require/devel/deliver/devDone/fixDone
        # 日期（两个字段保留，含义不同）
        "deadline":     pool.get("deadline", "") or "",    # 实际截止，过滤和延期判断
        "main_deadline":pool.get("mainDeadline", "") or "", # 主任务截止，部分页面显示口径走这里
        "delivery_date":pool.get("deliveryDate", "") or "",# 计划日期，规划展示
        # 进度
        "progress":     pool.get("progress", "0") or "0", # 字符串，如 "43%"
        # 工时
        "estimate":     float(pool.get("mainEstimate", 0) or 0),
        "consumed":     float(pool.get("mainConsumed", 0) or 0),
        "left":         float(pool.get("mainLeft", 0) or 0),
        # PHP 归属（原始字段，归属推断在 calc 层）
        "php_group":    str(pool.get("phpGroup", "") or ""),
        # PM 展示名（直接换成真实姓名）
        "pm":           pms.get(pm_user, pm_user) or "—",
        # 标签（逗号分隔的 tag ID 字符串）
        "pool_tags":    pool.get("poolTags", "") or "",
        # 关联需求 ID
        "story_id":     str(pool.get("storyId", "") or "0"),
        # Bug 统计（来自 associatedBugStat）
        "bug_total":    int(bug_info.get("total", 0)),
        "bug_main":     int(bug_info.get("mainTaskCount", 0)),
        "bug_sub":      int(bug_info.get("subTaskCount", 0)),
        # 驳回次数（来自 rejectedTaskStat）
        "rejected_count": int(rejected_stat.get(task_id, 0) or 0),
        # 未下单需求的记录日期
        "record_date":  pool.get("recordDate", "") or (pool.get("taskOpenedDate", "") or "")[:10],
    }


def _slim_task_details(task_details: dict) -> dict:
    """
    精简 taskDetails 字段，每个子任务只保留计算所需字段。
    taskDetails 原始结构：{taskID: {art: [subtask,...], devel: [...], ...}}
    """
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

def get_versions(client: ZentaoClient, project_id: str) -> dict:
    """
    获取指定项目的版本信息（当前、下一、上一）。

    返回结构：
    {
        "project_id": "10",
        "curr": {id, name, begin, end, remaining_days, is_release_day, hours},
        "next": {...} | None,
        "prev": {...} | None,
    }

    用法示例：
        versions = get_versions(client, "10")
        curr = versions["curr"]
        print(f"当前版本：{curr['name']}，距发布 {curr['remaining_days']} 天")
    """
    today = date.today()
    data  = client.fetch_versions(status="undone")
    execs = data.get("executionStats", [])

    result = _identify_versions(execs, project_id, today)
    result["project_id"] = project_id
    return result


# ─── 对外函数2：需求池数据 ────────────────────────────────────────────────────

def get_version_requirements(
    client: ZentaoClient,
    version_id: str,
    project_id: str,
) -> dict:
    """
    获取指定版本的需求池数据，字段精简后返回。

    返回结构：
    {
        "version_id":    "395",
        "project_id":    "10",
        "pools":         [slim_pool, ...],   # 精简后的需求列表（含已取消，调用方过滤）
        "task_details":  {taskID: {art: [slim_subtask], devel: [...], ...}},
        "php_member_map":{username: "PHP1部"/"PHP2部"},  # 用于 phpGroup 为空时推断
        "pms":           {username: realname},
        "review_stat":   {unReview: N, pendingReview: N},  # 需求评审统计
        "current_delivered": "2026-04-15",  # 当前版本交付日期
    }

    注意：
      - pools 保留所有状态（含 cancel），调用方根据场景自行过滤
      - task_details 子任务已精简，deleted="1" 的子任务保留（调用方过滤）
      - php_member_map 由 pool browse 返回的 users 字段构建，用于 PHP 归属推断
    """
    raw = client.fetch_pool(version_id, project_id)

    pools_raw      = raw.get("pools", [])
    task_details   = raw.get("taskDetails", {})
    pms            = raw.get("pms", {})
    users          = raw.get("users", {})
    bug_stat       = raw.get("associatedBugStat", {})
    rejected_stat  = raw.get("rejectedTaskStat", {})
    review_stat    = raw.get("statisticsReviewStory", {})

    # 构建 PHP 成员映射（用于 phpGroup 为空时的归属推断）
    php_member_map = build_php_member_map(users)

    # 精简需求池数据
    slim_pools = [
        _slim_pool(p, pms, bug_stat, rejected_stat)
        for p in pools_raw
        if isinstance(p, dict)
    ]

    # 精简子任务数据
    slim_details = _slim_task_details(task_details)

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
) -> dict:
    """
    获取指定版本的全量 Bug 数据，字段精简后返回。

    返回结构：
    {
        "version_id": "395",
        "bugs":       [slim_bug, ...],
        "stat": {
            "count":            N,   # 总数
            "activate":         N,   # 激活数
            "resolved":         N,   # 已处理数
            "postponed":        N,   # 已推迟数
            "classification_1": N,   # 外部开发 Bug
            "classification_2": N,   # 外部历史 Bug
            "classification_4": N,   # 内部开发 Bug
            "classification_5": N,   # 内部历史 Bug
        },
        "dept_review": {...},  # Bug 界定场景使用，日报场景忽略即可
    }

    slim_bug 字段说明：
      id             → Bug ID
      title          → Bug 标题
      classification → 来源分类（1=外部开发、2=外部历史、4=内部开发、5=内部历史）
      type           → Bug 类别（"performance"=非Bug，复盘时剔除）
      status         → active/resolved/closed
      severity       → 严重程度（1=极、2=高、3=中、4=低）
      is_typical     → 是否典型 Bug
      is_dispute     → 是否有争议
      deadline       → 截止日期
      main_task_id   → 关联主任务 ID（None 表示无关联）
      owner_dept     → 责任部门 ID（逗号分隔，转部门名用 DEPT_MAP）
    """
    raw = client.fetch_bugs(version_id, project_id)

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
    """
    精简单个 Bug 对象。
    原始对象 100+ 字段 → 精简后保留核心复盘字段。

    mainTaskId 类型不一致处理（已实测）：
      无关联：int(0) 或 string "0" → None
      有关联：string，如 "45160"  → "45160"
    """
    return {
        "id":             str(b.get("id", "")),
        "title":          b.get("title", "") or "",
        "classification": str(b.get("classification", "") or ""),
        "type":           b.get("type", "") or "",  # "performance" = 非Bug
        "status":         b.get("status", "") or "",
        "severity":       str(b.get("severity", "") or ""),
        "is_typical":     str(b.get("isTypical", "0")) == "1",
        "is_dispute":     str(b.get("isDispute", "0")) == "1",
        "deadline":       b.get("deadline", "") or "",
        "opened_date":    b.get("openedDate", "") or "",
        "main_task_id":   get_main_task_id(b),   # None 或 string ID
        "owner_dept":     b.get("ownerDept", "") or "",  # 逗号分隔的部门 ID
        "resolution":     b.get("resolution", "") or "",
        "cause_analysis": (b.get("causeAnalysis") or "").strip(),
        "dispute_remark": (b.get("disputeRemark") or "").strip(),
        "tracing_back":   (b.get("tracingBack") or "").strip(),
        "exclusion_reason": (b.get("exclusionReason") or "").strip(),
        "scope_influence": (b.get("scopeInfluence") or "").strip(),
        "phenomenon":     (b.get("phenomenon") or "").strip(),
        "demand":         (b.get("demand") or "").strip(),
        "use_case":       (b.get("useCase") or "").strip(),
    }
