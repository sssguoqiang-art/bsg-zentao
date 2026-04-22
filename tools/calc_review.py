"""
tools/calc_review.py

版本复盘计算层：从精简后的 Bug 数据和需求池数据中计算复盘所需的各板块数据。

对外暴露：
  calc_ext_bugs(bugs, dept_review)                          → 外部Bug分组 + 深度分析数据
  calc_int_bugs(bugs, dept_review)                          → 内部Bug分组 + 深度分析数据
  calc_low_quality(pools, bugs, dept_review,
                   task_details, php_member_map)            → 低质量任务排名（含主要部门）
  calc_req_counts(pools)                                    → 版本需求数量统计
"""

import re
from collections import Counter
from typing import Optional

from bsg_zentao.constants import (
    DEPT_MAP, TASK_DETAIL_DEPT_MAP, to_display,
    ONLINE_BUG_CLASSIFICATIONS, INTERNAL_BUG_CLASSIFICATIONS,
)

# ─── 严重程度标签 ──────────────────────────────────────────────────────────────

SEVERITY_LABEL = {
    "1": "🔴极严重",
    "2": "🔴高等缺陷",
    "3": "🟡中等缺陷",
    "4": "🟢低等缺陷",
}

SEVERITY_SCOPE = {
    "1": "严重影响使用",
    "2": "影响核心功能",
    "3": "影响不大",
    "4": "影响极小",
}

_MANUAL = "【待补充·人工】"
_IFACE  = "【待补充·接口】"


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

def _bug_link(bug: dict) -> str:
    bid   = bug.get("id", "")
    title = (bug.get("title") or "").strip()
    url   = f"https://cd.baa360.cc:20088/index.php?m=bug&f=view&bugID={bid}"
    return f"[{bid} {title}]({url})"


def _task_link(task_id: str, name: str) -> str:
    url = f"https://cd.baa360.cc:20088/index.php?m=task&f=view&taskID={task_id}"
    return f"[TASK#{task_id} {name}]({url})"


def _dept_ids_for_bug(bug: dict, dept_review: dict) -> list[str]:
    """责任部门 ID 列表：ownerDept 优先，否则 deptReview.depts"""
    owner = (bug.get("owner_dept") or "").strip()
    if owner:
        return [d.strip() for d in owner.split(",") if d.strip()]
    dr = dept_review.get(str(bug.get("id", "")), {})
    return [str(d) for d in (dr.get("depts") or [])]


def _dept_names_for_bug(bug: dict, dept_review: dict) -> list[str]:
    ids = _dept_ids_for_bug(bug, dept_review)
    return [to_display(DEPT_MAP.get(d, f"部门{d}")) for d in ids]


def _is_dispute(bug: dict, dept_review: dict) -> bool:
    # slim_bug 已直接解析 isDispute 字段，优先使用
    if bug.get("is_dispute"):
        return True
    # 备用：从 deptReview 读取
    dr = dept_review.get(str(bug.get("id", "")), {})
    return str(dr.get("isDispute", "0")) == "1"


def _get_review_detail(dept_review: dict, bug_id: str, dept_id: str) -> tuple[str, str]:
    dr     = dept_review.get(str(bug_id), {})
    review = dr.get("review", {})
    if isinstance(review, list):
        item = next(
            (x for x in review if str(x.get("deptId", "")) == str(dept_id)),
            {},
        )
    else:
        item = (review or {}).get(str(dept_id), {})
    cause = (item.get("causeAnalysis") or "").strip()
    step  = (item.get("nextStep")      or "").strip()
    return cause or _IFACE, step or _IFACE


def _excl_reason(bug: dict) -> str:
    """
    非Bug剔除原因（单行，严格清除换行/URL，适合 Markdown 表格单元格）。
    ⚠️ 必须保证输出不含 \\n，否则会破坏表格行结构。
    """
    exclusion = (bug.get("exclusion_reason") or "").strip()
    if exclusion:
        clean = re.sub(r'https?://\S+', '', exclusion)
        clean = re.sub(r'&\w+;', '', clean)
        clean = re.sub(r'[\n\r\t]', ' ', clean)
        clean = re.sub(r'\s{2,}', ' ', clean).strip()
        if clean:
            return clean

    if "performance" in (bug.get("type") or ""):
        tracing = (bug.get("tracing_back") or "").strip()
        if tracing:
            # 严格清理：去URL → 去HTML实体 → 换行变空格 → 去多余空白
            clean = re.sub(r'https?://\S+', '', tracing)
            clean = re.sub(r'&\w+;', '', clean)
            clean = re.sub(r'[\n\r\t]', ' ', clean)
            clean = re.sub(r'\s{2,}', ' ', clean).strip()
            # 去掉 "现象：" "原因：" 等前缀标签，只保留描述内容
            clean = re.sub(r'^(现象|原因|环境)[\uff1a:]\s*', '', clean)
            if clean:
                return f"优化项：{clean[:25]}{'…' if len(clean) > 25 else ''}"
        return "优化项"
    return _MANUAL


def _severity_label(bug: dict, with_scope: bool = False) -> str:
    sev   = str(bug.get("severity") or "")
    label = SEVERITY_LABEL.get(sev, f"severity={sev}")
    if with_scope:
        scope_text = (bug.get("scope_influence") or "").strip()
        if scope_text:
            return f"{label} {scope_text}"
        scope = SEVERITY_SCOPE.get(sev, "")
        return f"{label} {scope}" if scope else label
    return label


def _get_task_main_dept(task_id: str, task_details: dict, php_member_map: dict) -> str:
    """
    从 task_details 推断任务的主要责任部门（按消耗工时最多的部门）。
    devel 类型的子任务通过 php_member_map 区分 PHP1/PHP2。
    """
    td = task_details.get(str(task_id), {})
    if not td:
        return _MANUAL

    dept_hours: dict[str, float] = {}

    for dept_key, subs in td.items():
        if not isinstance(subs, list):
            continue
        active_subs = [s for s in subs if str(s.get("deleted", "0")) != "1"]
        if not active_subs:
            continue

        if dept_key == "devel":
            php1_h = php2_h = 0.0
            for s in active_subs:
                person   = s.get("finishedBy") or s.get("assignedTo") or ""
                dept_raw = php_member_map.get(person, "")
                consumed = float(s.get("consumed", 0) or 0)
                if dept_raw == "PHP1部":
                    php1_h += consumed
                elif dept_raw == "PHP2部":
                    php2_h += consumed
            if php1_h > 0 or php2_h > 0:
                dept_hours["PHP1组"] = dept_hours.get("PHP1组", 0) + php1_h
                dept_hours["PHP2组"] = dept_hours.get("PHP2组", 0) + php2_h
            else:
                # phpGroup 为空时保守标注
                total = sum(float(s.get("consumed", 0) or 0) for s in active_subs)
                dept_hours["开发组"] = dept_hours.get("开发组", 0) + total

        elif dept_key == "qa":
            total = sum(float(s.get("consumed", 0) or 0) for s in active_subs)
            dept_hours["测试组"] = dept_hours.get("测试组", 0) + total

        elif dept_key == "design":
            total = sum(float(s.get("consumed", 0) or 0) for s in active_subs)
            dept_hours["产品组"] = dept_hours.get("产品组", 0) + total

        elif dept_key in TASK_DETAIL_DEPT_MAP:
            raw  = TASK_DETAIL_DEPT_MAP[dept_key]
            disp = to_display(raw)
            total = sum(float(s.get("consumed", 0) or 0) for s in active_subs)
            dept_hours[disp] = dept_hours.get(disp, 0) + total

    if not dept_hours:
        return _MANUAL

    # 取工时最多的部门
    main_dept = max(dept_hours, key=lambda k: dept_hours[k])
    return main_dept


# ─── 外部 Bug 计算 ─────────────────────────────────────────────────────────────

def calc_ext_bugs(bugs: list[dict], dept_review: dict) -> dict:
    ext_all    = [b for b in bugs if b.get("classification") in ONLINE_BUG_CLASSIFICATIONS]
    ext_review = [b for b in ext_all if "performance" not in (b.get("type") or "")]
    ext_excl   = [b for b in ext_all if "performance"     in (b.get("type") or "")]

    excl_list = [
        {
            "id":          b["id"],
            "title":       (b.get("title") or "").strip(),
            "link":        _bug_link(b),
            "excl_reason": _excl_reason(b),
        }
        for b in ext_excl
    ]

    review_list = []
    for b in ext_review:
        bid    = b["id"]
        is_dis = _is_dispute(b, dept_review)
        dept_str = "、".join(_dept_names_for_bug(b, dept_review)) or _IFACE
        if is_dis:
            dept_str += "（争议）"
        review_list.append({
            "id":             bid,
            "title":          (b.get("title") or "").strip(),
            "link":           _bug_link(b),
            "severity":       str(b.get("severity") or ""),
            "severity_label": _severity_label(b, with_scope=True),
            "dept_str":       dept_str,
            "is_dispute":     is_dis,
        })

    test_bugs    = [b for b in ext_review if "45" in _dept_ids_for_bug(b, dept_review)]
    test_count   = len(test_bugs)
    test_bug_ids = [b["id"] for b in test_bugs]

    other_dist: Counter = Counter()
    for b in ext_review:
        for d in _dept_ids_for_bug(b, dept_review):
            if d != "45":
                other_dist[to_display(DEPT_MAP.get(d, f"部门{d}"))] += 1

    deep_analysis = []
    for b in ext_review:
        bid    = b["id"]
        dids   = _dept_ids_for_bug(b, dept_review)
        is_dis = _is_dispute(b, dept_review)
        depts  = []
        for did in dids:
            cause, step = _get_review_detail(dept_review, bid, did)
            depts.append({
                "id":         did,
                "name":       to_display(DEPT_MAP.get(did, f"部门{did}")),
                "cause":      cause,
                "step":       step,
                "is_dispute": is_dis and did == "45",
            })
        deep_analysis.append({
            "id":             bid,
            "title":          (b.get("title") or "").strip(),
            "link":           _bug_link(b),
            "severity_label": _severity_label(b, with_scope=True),
            "phenomenon":     (b.get("phenomenon") or "").strip(),
            "scope_influence": (b.get("scope_influence") or "").strip(),
            "demand":         (b.get("demand") or "").strip(),
            "use_case":       (b.get("use_case") or "").strip(),
            "dispute_remark": (b.get("dispute_remark") or "").strip(),
            "exclusion_reason": (b.get("exclusion_reason") or "").strip(),
            "cause_analysis": (b.get("cause_analysis") or "").strip(),
            "tracing":        (b.get("tracing_back") or "").strip() or _IFACE,
            "depts":          depts,
        })

    return {
        "all_count":       len(ext_all),
        "review_count":    len(ext_review),
        "excl_count":      len(ext_excl),
        "excl_list":       excl_list,
        "review_list":     review_list,
        "test_dept_count": test_count,
        "test_bug_ids":    test_bug_ids,
        "other_dept_dist": dict(other_dist.most_common()),
        "deep_analysis":   deep_analysis,
    }


# ─── 内部 Bug 计算 ─────────────────────────────────────────────────────────────

def calc_int_bugs(bugs: list[dict], dept_review: dict) -> dict:
    int_all = [
        b for b in bugs
        if  b.get("classification") in INTERNAL_BUG_CLASSIFICATIONS
        and "performance" not in (b.get("type") or "")
    ]

    extreme = [b for b in int_all if str(b.get("severity") or "") == "1"]
    high    = [b for b in int_all if str(b.get("severity") or "") == "2"]

    def _dept_cnt(bug_list: list[dict]) -> dict:
        cnt: Counter = Counter()
        for b in bug_list:
            for d in _dept_ids_for_bug(b, dept_review):
                cnt[to_display(DEPT_MAP.get(d, f"部门{d}"))] += 1
        return dict(cnt.most_common())

    seen = set()
    review_list = []
    for b in sorted(int_all, key=lambda x: int(x.get("severity") or 9)):
        bid = b["id"]
        if bid in seen:
            continue
        if str(b.get("severity") or "") in ("1", "2") or b.get("is_typical"):
            seen.add(bid)
            is_dis   = _is_dispute(b, dept_review)
            dept_str = "、".join(_dept_names_for_bug(b, dept_review)) or _IFACE
            if is_dis:
                dept_str += "（争议）"
            review_list.append({
                "id":             bid,
                "title":          (b.get("title") or "").strip(),
                "link":           _bug_link(b),
                "severity":       str(b.get("severity") or ""),
                "severity_label": _severity_label(b),
                "dept_str":       dept_str,
                "is_typical":     bool(b.get("is_typical")),
                "is_dispute":     is_dis,
            })

    review_ids = {item["id"] for item in review_list}
    deep_analysis = []
    for b in int_all:
        if b["id"] not in review_ids:
            continue
        bid   = b["id"]
        dids  = _dept_ids_for_bug(b, dept_review)
        is_dis = _is_dispute(b, dept_review)
        depts = []
        for did in dids:
            cause, step = _get_review_detail(dept_review, bid, did)
            depts.append({
                "id":         did,
                "name":       to_display(DEPT_MAP.get(did, f"部门{did}")),
                "cause":      cause,
                "step":       step,
                "is_dispute": is_dis,
            })
        deep_analysis.append({
            "id":             bid,
            "title":          (b.get("title") or "").strip(),
            "link":           _bug_link(b),
            "severity_label": _severity_label(b),
            "is_typical":     bool(b.get("is_typical")),
            "is_dispute":     is_dis,
            "depts":          depts,
        })

    return {
        "total_count":       len(int_all),
        "extreme_count":     len(extreme),
        "high_count":        len(high),
        "extreme_dept_dist": _dept_cnt(extreme),
        "high_dept_dist":    _dept_cnt(high),
        "review_list":       review_list,
        "deep_analysis":     deep_analysis,
    }


# ─── 低质量任务 ────────────────────────────────────────────────────────────────

def calc_low_quality(
    pools: list[dict],
    bugs: list[dict],
    dept_review: dict,
    task_details: Optional[dict] = None,
    php_member_map: Optional[dict] = None,
    min_bug_count: int = 5,
) -> list[dict]:
    """
    按 Bug 数量排名低质量任务。

    使用 pool.bug_total（associatedBugStat.total）作为 Bug 计数。
    主要部门通过 task_details + php_member_map 推断（按消耗工时最多的部门）。

    返回：[{rank, task_id, name, link, bug_count, high_extreme_count, main_dept, judgment_prefix}]
    """
    high_ext_by_task: Counter = Counter()
    for b in bugs:
        tid = b.get("main_task_id")
        if tid and str(b.get("severity") or "") in ("1", "2"):
            high_ext_by_task[str(tid)] += 1

    result = []
    for p in sorted(pools, key=lambda x: -int(x.get("bug_total") or 0)):
        cnt = int(p.get("bug_total") or 0)
        if cnt < min_bug_count:
            break
        tid  = str(p.get("task_id") or "")
        name = (p.get("title") or "").strip()

        # 主要部门：从 task_details 推断
        if task_details is not None and php_member_map is not None:
            main_dept = _get_task_main_dept(tid, task_details, php_member_map)
        else:
            main_dept = _MANUAL

        result.append({
            "rank":               len(result) + 1,
            "task_id":            tid,
            "name":               name,
            "link":               _task_link(tid, name) if tid and name else _MANUAL,
            "bug_count":          cnt,
            "high_extreme_count": high_ext_by_task.get(tid, 0),
            "main_dept":          main_dept,
            "judgment_prefix":    "🔴提测质量差" if cnt >= 20 else "🟡提测质量需关注",
        })

    return result


# ─── 版本需求数量 ───────────────────────────────────────────────────────────────

def calc_req_counts(pools: list[dict]) -> dict:
    active = [p for p in pools if p.get("task_status") != "cancel"]
    return {
        "ext_reqs": sum(1 for p in active if p.get("category") in ("version", "operation")),
        "int_reqs": sum(1 for p in active if p.get("category") == "internal"),
    }
