"""
tools/calc_daily.py

日报业务计算层。
接收来自 data_tools 的精简数据，计算各板块所需的结构化结果，
返回给 MCP 工具层，再由 Claude 组织成最终报告。

计算层不做任何输出格式化，只返回纯数据结构。
格式化（Markdown 表格、标注符号等）由 Claude 负责。

对外函数：
  calc_summary(pools)                                    → 需求总览统计
  calc_dept_progress(pools, task_details, version_end, php_member_map) → 部门进度
  calc_delay_list(pools, task_details, php_member_map, today)          → 延期情况
  calc_not_test_list(pools, task_details, php_member_map, version_end, today) → 未到测试
  calc_test_focus(pools, bugs)                           → 测试关注
  calc_online_bugs(bugs)                                 → 线上Bug
  calc_rejected_list(pools)                              → 驳回专栏
  calc_next_workload(pools, task_details, php_member_map, version_end) → 下一版本工时
"""

from collections import Counter
from datetime import date

from bsg_zentao.constants import (
    CATEGORY_DISPLAY, PHPGROUP_DEPT_MAP, REPORT_DEPTS_RAW,
    STATUS_DEV, STATUS_DONE, STATUS_TESTING,
    TASK_DETAIL_DEPT_MAP, ONLINE_BUG_CLASSIFICATIONS,
    to_display,
)
from bsg_zentao.utils import fmt_date, days_overdue, parse_date

# ─── 单子类型名称（通过 ordertype 字段判断，不依赖 storyAssessText）─────────────
# 旧脚本用 storyAssessText 含 ">是<" 判断验收单，Skill 已明确这是错误的
# 正确做法：用 ordertype=12 判断验收单

_ORDERTYPE_NAME: dict[str, str] = {
    "1":  "需求单",   # design
    "2":  "开发单",   # devel
    "3":  "分析单",   # devel
    "4":  "排查单",   # devel
    "5":  "开发单",   # cocos
    "6":  "分析单",   # cocos
    "7":  "排查单",   # cocos
    "8":  "开发单",   # web
    "9":  "分析单",   # web
    "10": "排查单",   # web
    "11": "制作单",   # art
    "12": "验收单",   # art（ordertype=12，不是 storyAssessText）
    "13": "测试单",   # qa
    "14": "用例单",   # qa
    "15": "联调单",   # devel
    "16": "检查单",   # design
}

_DATE_ZERO = "0000-00-00"


def _ordertype_name(sub: dict) -> str:
    """从子任务对象取单子类型名称。"""
    return _ORDERTYPE_NAME.get(str(sub.get("ordertype", "") or ""), "其他")


# ─── 内部：PHP 部门归属推断 ───────────────────────────────────────────────────

def _get_php_dept(pool: dict, task_details: dict, php_member_map: dict) -> str:
    """
    推断需求的 PHP 部门归属（PHP1部 或 PHP2部）。

    优先级（Skill 规范）：
      1. pool.php_group → PHPGROUP_DEPT_MAP 直接映射
      2. php_group 为空时，遍历 devel 子任务的 finishedBy/assignedTo，
         在 php_member_map 中查找归属
      3. 均无法判断时返回空字符串

    ⚠️ phpGroup 字段不可靠，是计划填写的预期分组，不一定有数据，
    因此必须有 fallback 逻辑。
    """
    php_group = pool.get("php_group", "") or ""
    if php_group in PHPGROUP_DEPT_MAP:
        return PHPGROUP_DEPT_MAP[php_group]

    # fallback：从子任务执行人推断
    task_id    = pool.get("task_id", "") or ""
    devel_subs = task_details.get(task_id, {}).get("devel", [])
    for sub in devel_subs:
        if sub.get("deleted") == "1":
            continue
        for field in ("finishedBy", "assignedTo"):
            person = sub.get(field, "") or ""
            dept   = php_member_map.get(person, "")
            if dept:
                return dept
    return ""


# ─── 内部：取需求的各部门子任务 ──────────────────────────────────────────────

def _get_dept_subtasks(
    pool: dict,
    task_details: dict,
    php_member_map: dict,
) -> dict[str, list[dict]]:
    """
    返回 {接口原始部门名: [子任务, ...]}。
    只包含 REPORT_DEPTS_RAW 中定义的部门（美术部/PHP1部/PHP2部/Web部/Cocos部）。
    已删除的子任务（deleted="1"）已排除。

    devel 类子任务：通过 _get_php_dept 推断归属 PHP1部 或 PHP2部。
    其余类型：通过 TASK_DETAIL_DEPT_MAP 直接映射。
    """
    task_id = pool.get("task_id", "") or ""
    if not task_id:
        return {}

    detail = task_details.get(task_id)
    if not isinstance(detail, dict):
        return {}

    # PHP 部门只推断一次
    php_dept: str | None = None
    result: dict[str, list[dict]] = {}

    for dept_key, subs in detail.items():
        if not isinstance(subs, list):
            continue

        if dept_key == "devel":
            if php_dept is None:
                php_dept = _get_php_dept(pool, task_details, php_member_map)
            dept = php_dept
        else:
            dept = TASK_DETAIL_DEPT_MAP.get(dept_key, "")

        if dept not in REPORT_DEPTS_RAW:
            continue

        valid = [s for s in subs if isinstance(s, dict) and s.get("deleted") != "1"]
        if valid:
            result.setdefault(dept, []).extend(valid)

    return result


# ─── 1. 需求总览统计 ──────────────────────────────────────────────────────────

def calc_summary(pools: list[dict]) -> dict:
    """
    统计版本需求总览数字。

    返回：
    {
        "total":    N,   # 总数（已过滤 cancel）
        "done":     N,   # 已完成（done/closed）
        "testing":  N,   # 测试中（waittest/testing）
        "dev":      N,   # 开发中（wait/doing/pause/rejected/reviewing/unsure）
        "unordered":N,   # 未下单（taskID 为空或 "0"）
        "postponed":N,   # 已延期（isPostponed=True）
    }
    """
    # 过滤已取消的需求
    active_pools = [p for p in pools if p.get("task_status") != "cancel"]

    total     = len(active_pools)
    done      = sum(1 for p in active_pools if p.get("task_status") in STATUS_DONE - {"cancel"})
    testing   = sum(1 for p in active_pools if p.get("task_status") in STATUS_TESTING)
    dev       = sum(1 for p in active_pools if p.get("task_status") in STATUS_DEV)
    unordered = sum(1 for p in active_pools if p.get("is_unordered", False))
    postponed = sum(1 for p in active_pools if p.get("is_postponed", False))

    return {
        "total":     total,
        "done":      done,
        "testing":   testing,
        "dev":       dev,
        "unordered": unordered,
        "postponed": postponed,
    }


# ─── 2. 部门进度 ──────────────────────────────────────────────────────────────

def calc_dept_progress(
    pools: list[dict],
    task_details: dict,
    version_end: str,
    php_member_map: dict,
) -> dict[str, dict]:
    """
    计算各部门进度表。

    过滤条件：pool.deadline <= version_end
    ⚠️ 使用 deadline（任务实际截止日期），不是 deliveryDate（PM 计划日期）。
       旧脚本在此处用了 deliveryDate，是已知 bug，此处已修正。

    返回：{接口原始部门名: {remaining_tasks, remaining_label, total_left, total_consumed, progress_pct}}

    remaining_label 示例："3（制作单2、验收单1）"
    progress_pct：消耗工时 / 总工时，百分比整数，无工时时为 None
    """
    stats: dict[str, dict] = {
        d: {
            "remaining_tasks": 0,
            "total_left":      0.0,
            "total_consumed":  0.0,
            "_type_cnt":       Counter(),   # 内部用，最终删除
        }
        for d in REPORT_DEPTS_RAW
    }

    for pool in pools:
        # 已完成的需求不统计剩余工时
        if pool.get("task_status") in STATUS_DONE:
            continue

        # 版本交付过滤：用 deadline
        dl = pool.get("deadline", "") or ""
        if not dl or dl == _DATE_ZERO or dl > version_end:
            continue

        dept_subs = _get_dept_subtasks(pool, task_details, php_member_map)
        for dept, subs in dept_subs.items():
            for sub in subs:
                status   = sub.get("status", "")
                left     = sub.get("left", 0.0)
                consumed = sub.get("consumed", 0.0)
                stats[dept]["total_consumed"] += consumed
                if status in STATUS_DEV:
                    stats[dept]["remaining_tasks"] += 1
                    stats[dept]["total_left"]      += left
                    stats[dept]["_type_cnt"][_ordertype_name(sub)] += 1

    # 生成 remaining_label 和 progress_pct，清理内部字段
    result = {}
    for dept in REPORT_DEPTS_RAW:
        s         = stats[dept]
        n         = s["remaining_tasks"]
        type_cnt  = s["_type_cnt"]
        consumed  = s["total_consumed"]
        left      = s["total_left"]
        total_wh  = consumed + left

        if n == 0:
            label = "0"
        else:
            detail = "、".join(
                f"{name}{cnt}" for name, cnt in type_cnt.most_common()
            )
            label = f"{n}（{detail}）"

        result[dept] = {
            "remaining_tasks": n,
            "remaining_label": label,
            "total_left":      round(left, 1),
            "total_consumed":  round(consumed, 1),
            "progress_pct":    int(consumed / total_wh * 100) if total_wh > 0 else None,
        }

    return result


# ─── 3. 延期情况 ──────────────────────────────────────────────────────────────

def calc_delay_list(
    pools: list[dict],
    task_details: dict,
    php_member_map: dict,
    today: date,
) -> list[dict]:
    """
    找出所有已超期的子任务。
    条件：子任务 deadline < today AND status in STATUS_DEV AND 主需求未完成。

    按（部门 + 截止日）去重，同一需求同一部门同一截止日只出一条。
    按超期天数降序排列（最严重的在前）。

    返回字段：
      title, category, dept（显示名）, deadline_display, overdue_days
    """
    today_str = today.isoformat()
    rows: list[dict] = []

    for pool in pools:
        if pool.get("task_status") in STATUS_DONE:
            continue

        dept_subs = _get_dept_subtasks(pool, task_details, php_member_map)
        seen: set = set()

        for dept, subs in dept_subs.items():
            for sub in subs:
                if sub.get("status") not in STATUS_DEV:
                    continue
                dl = sub.get("deadline", "") or ""
                if not dl or dl == _DATE_ZERO or dl >= today_str:
                    continue  # 未超期

                key = (dept, dl)
                if key in seen:
                    continue
                seen.add(key)

                rows.append({
                    "title":           pool.get("title", ""),
                    "category":        CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
                    "dept":            to_display(dept),
                    "deadline":        dl,
                    "deadline_display": fmt_date(dl),
                    "overdue_days":    days_overdue(dl, today),
                })

    rows.sort(key=lambda r: r["overdue_days"], reverse=True)
    return rows


# ─── 4. 未到测试·临期 ────────────────────────────────────────────────────────

def calc_not_test_list(
    pools: list[dict],
    task_details: dict,
    php_member_map: dict,
    version_end: str,
    today: date,
) -> dict:
    """
    找出未到测试且在版本截止范围内的需求，分为今日截止和其他临期两组。

    条件：pool.deadline <= version_end AND taskStatus in STATUS_DEV
    ⚠️ 同样用 deadline，不是 deliveryDate。

    今日截止（today_due）：deadline == today，需在报告中置顶标注
    其他临期（other_due）：deadline 在今天之后但 <= version_end

    返回：
    {
        "today_due": [...],   # 今日截止，按进度升序（进度低的优先关注）
        "other_due": [...],   # 其他临期，按截止日升序
    }

    每条数据字段：
      title, category, blocked_depts（显示名列表）,
      deadline, deadline_display, overdue_days, progress, is_postponed
    """
    today_str  = today.isoformat()
    today_due  = []
    other_due  = []

    for pool in pools:
        if pool.get("task_status") not in STATUS_DEV:
            continue

        dl = pool.get("deadline", "") or ""
        if not dl or dl == _DATE_ZERO or dl > version_end:
            continue

        # 卡住的部门：该需求下还有未完成子任务的部门
        dept_subs = _get_dept_subtasks(pool, task_details, php_member_map)
        blocked = [
            to_display(dept)
            for dept, subs in dept_subs.items()
            if any(s.get("status") in STATUS_DEV for s in subs)
        ]

        overdue = days_overdue(dl, today)
        # 进度字符串取数字部分
        prog_str = str(pool.get("progress", "0") or "0").rstrip("%")
        try:
            progress_int = int(prog_str)
        except ValueError:
            progress_int = 0

        row = {
            "title":           pool.get("title", ""),
            "category":        CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "blocked_depts":   blocked,
            "deadline":        dl,
            "deadline_display": fmt_date(dl),
            "overdue_days":    overdue,
            "progress":        f"{progress_int}%",
            "progress_int":    progress_int,   # 排序用，不输出
            "is_postponed":    pool.get("is_postponed", False),
        }

        if dl == today_str:
            today_due.append(row)
        else:
            other_due.append(row)

    # 今日截止：进度低的优先（更需要关注）
    today_due.sort(key=lambda r: r["progress_int"])
    # 其他临期：按截止日升序
    other_due.sort(key=lambda r: r["deadline"])

    # 清理排序辅助字段
    for r in today_due + other_due:
        r.pop("progress_int", None)

    return {"today_due": today_due, "other_due": other_due}


# ─── 5. 测试关注 ──────────────────────────────────────────────────────────────

def calc_test_focus(pools: list[dict], bugs: list[dict]) -> list[dict]:
    """
    测试中且有问题的需求。
    条件：taskStatus == "testing" AND（有 active Bug 或 env=="require"）

    Bug 统计：
      - status == "active"
      - type != "performance"（非Bug，排除）
      - 通过 main_task_id 关联到需求

    返回字段：title, category, active_bugs（数量）, env
    """
    # 建立 task_id → active bug 数 映射
    active_bug_map: dict[str, int] = {}
    for b in bugs:
        if b.get("status") != "active":
            continue
        if b.get("type") == "performance":      # 非Bug，排除
            continue
        mid = b.get("main_task_id")
        if mid:
            active_bug_map[mid] = active_bug_map.get(mid, 0) + 1

    rows: list[dict] = []
    for pool in pools:
        if pool.get("task_status") != "testing":
            continue
        task_id     = pool.get("task_id", "") or ""
        active_bugs = active_bug_map.get(task_id, 0)
        env         = pool.get("env", "") or ""

        if active_bugs == 0 and env != "require":
            continue

        rows.append({
            "title":       pool.get("title", ""),
            "category":    CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "active_bugs": active_bugs,
            "env":         env,
            "env_display": "需合并" if env == "require" else env,
        })

    return rows


# ─── 6. 线上 Bug ─────────────────────────────────────────────────────────────

def calc_online_bugs(bugs: list[dict]) -> list[dict]:
    """
    当前活跃的线上 Bug。
    条件：classification in {1,2} AND status == "active" AND type != "performance"

    type=="performance" 是非Bug记录（旧脚本误区：有时会把性能记录统计进来），
    必须排除。

    返回字段：id, title, severity（严重程度）, deadline_display
    按 severity 升序（极>高>中>低，数值越小越严重）
    """
    rows: list[dict] = []
    for b in bugs:
        if b.get("status") != "active":
            continue
        if str(b.get("classification", "")) not in ONLINE_BUG_CLASSIFICATIONS:
            continue
        if b.get("type") == "performance":      # 非Bug，排除
            continue

        rows.append({
            "id":              b.get("id", ""),
            "title":           b.get("title", ""),
            "severity":        str(b.get("severity", "") or ""),
            "deadline":        b.get("deadline", "") or "",
            "deadline_display": fmt_date(b.get("deadline", "") or ""),
        })

    rows.sort(key=lambda r: r["severity"] or "9")
    return rows


# ─── 7. 驳回专栏 ─────────────────────────────────────────────────────────────

def calc_rejected_list(pools: list[dict]) -> list[dict]:
    """
    有驳回记录的未完成需求。
    数据来源：pool.rejected_count（来自 rejectedTaskStat 字段，已实测确认结构）

    只展示未完成状态的需求（已完成的驳回记录无需关注）。
    按驳回次数降序（驳回最多的优先关注）。

    返回字段：title, category, rejected_count, task_status
    """
    rows: list[dict] = []
    for pool in pools:
        if pool.get("task_status") in STATUS_DONE:
            continue
        cnt = int(pool.get("rejected_count", 0) or 0)
        if cnt <= 0:
            continue
        rows.append({
            "title":         pool.get("title", ""),
            "category":      CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
            "rejected_count": cnt,
            "task_status":   pool.get("task_status", ""),
        })

    rows.sort(key=lambda r: r["rejected_count"], reverse=True)
    return rows


# ─── 8. 下一版本工时总览 ─────────────────────────────────────────────────────

def calc_next_workload(
    pools: list[dict],
    task_details: dict,
    php_member_map: dict,
    version_end: str,
) -> dict:
    """
    下一版本各部门工时总览及未下单需求列表。

    版本交付判断（下一版本用 delivery_date，不是 deadline）：
      delivery_date <= version_end → 属于本版本交付范围
      delivery_date 为空且未完成  → 视为本版本交付（兜底逻辑）

    ⚠️ 此处用 delivery_date 是正确的，与当前版本进度统计用 deadline 不同：
      - 当前版本：用 deadline（实际截止日）过滤，判断哪些任务"应在本版交付"
      - 下一版本：用 delivery_date（PM 计划日）展示，判断 PM 计划本版交付的需求

    返回：
    {
        "total":         N,   # 需求总数
        "ordered":       N,   # 已下单数
        "unordered":     N,   # 未下单数
        "dept_workload": {
            接口原始部门名: {
                "tasks":          N,     # 全量任务数
                "estimate":       N,     # 全量预估工时
                "tasks_in_v":     N,     # 版本交付任务数
                "estimate_in_v":  N,     # 版本交付预估工时
            }
        },
        "unordered_list": [  # 未下单需求明细
            {title, category, pm, record_days}
        ]
    }
    """
    STATUS_DONE_ALL = STATUS_DONE | {"cancel"}

    total     = len([p for p in pools if p.get("task_status") != "cancel"])
    ordered   = sum(1 for p in pools if not p.get("is_unordered", False) and p.get("task_status") != "cancel")
    unordered = total - ordered

    dept_workload: dict[str, dict] = {
        d: {"tasks": 0, "estimate": 0.0, "tasks_in_v": 0, "estimate_in_v": 0.0}
        for d in REPORT_DEPTS_RAW
    }
    unordered_list: list[dict] = []

    for pool in pools:
        if pool.get("task_status") == "cancel":
            continue

        # 未下单需求：收集展示信息，不计算工时
        if pool.get("is_unordered", False):
            rd = pool.get("record_date", "") or ""
            rd_date = parse_date(rd)
            days = (date.today() - rd_date).days if rd_date else 0
            unordered_list.append({
                "title":       pool.get("title", ""),
                "category":    CATEGORY_DISPLAY.get(pool.get("category", ""), "—"),
                "pm":          pool.get("pm", "—"),
                "record_date": fmt_date(rd),
                "record_days": days,
            })
            continue

        # 版本交付判断（用 delivery_date）
        ddl = pool.get("delivery_date", "") or ""
        if not ddl or ddl == _DATE_ZERO:
            in_v = pool.get("task_status") not in STATUS_DONE_ALL
        else:
            in_v = ddl <= version_end

        task_id    = pool.get("task_id", "") or ""
        detail     = task_details.get(task_id)
        if not isinstance(detail, dict):
            continue

        # PHP 部门只推断一次
        php_dept: str | None = None
        dept_est_map: dict[str, float] = {}

        for dept_key, subs in detail.items():
            if not isinstance(subs, list):
                continue

            if dept_key == "devel":
                if php_dept is None:
                    php_dept = _get_php_dept(pool, task_details, php_member_map)
                dept = php_dept
            else:
                dept = TASK_DETAIL_DEPT_MAP.get(dept_key, "")

            if dept not in REPORT_DEPTS_RAW:
                continue

            non_deleted = [s for s in subs if isinstance(s, dict) and s.get("deleted") != "1"]
            if not non_deleted:
                continue
            # 全部子任务都是 cancel → 该部门不计入
            if all(s.get("status") == "cancel" for s in non_deleted):
                continue

            est = sum(s.get("estimate", 0.0) for s in non_deleted)
            dept_est_map[dept] = dept_est_map.get(dept, 0.0) + est

        for dept, est in dept_est_map.items():
            dept_workload[dept]["tasks"]    += 1
            dept_workload[dept]["estimate"] += est
            if in_v:
                dept_workload[dept]["tasks_in_v"]    += 1
                dept_workload[dept]["estimate_in_v"] += est

    # 工时取整
    for d in REPORT_DEPTS_RAW:
        dw = dept_workload[d]
        dw["estimate"]      = round(dw["estimate"], 1)
        dw["estimate_in_v"] = round(dw["estimate_in_v"], 1)

    # 未下单列表按记录天数降序（记录越久越需要催）
    unordered_list.sort(key=lambda r: r["record_days"], reverse=True)

    return {
        "total":          total,
        "ordered":        ordered,
        "unordered":      unordered,
        "dept_workload":  dept_workload,
        "unordered_list": unordered_list,
    }
