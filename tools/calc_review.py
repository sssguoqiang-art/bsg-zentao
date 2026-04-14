"""
tools/calc_review.py

版本复盘计算层：从精简后的 Bug 数据和需求池数据中计算复盘所需的各板块数据。

设计原则：
  - 输入：slim_bug 列表、slim_pool 列表、deptReview 原始 dict
  - 输出：结构化数据 dict，供 report_tools_review.py 组装，再由 Claude 生成报告

对外暴露：
  calc_ext_bugs(bugs, dept_review)        → 外部Bug分组 + 深度分析数据
  calc_int_bugs(bugs, dept_review)        → 内部Bug分组 + 深度分析数据
  calc_low_quality(pools, bugs, dept_review) → 低质量任务排名
  calc_req_counts(pools)                  → 版本需求数量统计
"""

from collections import Counter
from typing import Optional

from bsg_zentao.constants import (
    DEPT_MAP, to_display,
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

# ─── 剔除原因映射（Bug.resolution 字段） ──────────────────────────────────────

RESOLUTION_LABEL = {
    "bydesign":   "非Bug，设计如此",
    "notrepro":   "非Bug，无法复现",
    "tostory":    "优化项：已下需求",
    "willnotfix": "不予解决",
}

_MANUAL = "【待补充·人工】"
_IFACE  = "【待补充·接口】"


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

def _bug_link(bug: dict) -> str:
    """生成 Bug 的 Markdown 链接。"""
    bid   = bug.get("id", "")
    title = (bug.get("title") or "").strip()
    url   = f"https://cd.baa360.cc:20088/index.php?m=bug&f=view&bugID={bid}"
    return f"[{bid} {title}]({url})"


def _task_link(task_id: str, name: str) -> str:
    """生成任务的 Markdown 链接。"""
    url = f"https://cd.baa360.cc:20088/index.php?m=task&f=view&taskID={task_id}"
    return f"[TASK#{task_id} {name}]({url})"


def _dept_ids_for_bug(bug: dict, dept_review: dict) -> list[str]:
    """
    获取 Bug 的责任部门 ID 列表（字符串）。
    优先级：ownerDept（slim_bug.owner_dept）> deptReview.depts
    """
    owner = (bug.get("owner_dept") or "").strip()
    if owner:
        return [d.strip() for d in owner.split(",") if d.strip()]
    dr = dept_review.get(str(bug.get("id", "")), {})
    return [str(d) for d in (dr.get("depts") or [])]


def _dept_names_for_bug(bug: dict, dept_review: dict) -> list[str]:
    """获取 Bug 责任部门的显示名称列表。"""
    ids = _dept_ids_for_bug(bug, dept_review)
    return [to_display(DEPT_MAP.get(d, f"部门{d}")) for d in ids]


def _is_dispute(bug: dict, dept_review: dict) -> bool:
    """判断 Bug 是否有争议。"""
    dr = dept_review.get(str(bug.get("id", "")), {})
    return str(dr.get("isDispute", "0")) == "1"


def _get_review_detail(
    dept_review: dict,
    bug_id: str,
    dept_id: str,
) -> tuple[str, str]:
    """
    获取某部门对某 Bug 的原因分析和改进举措。
    review 字段有两种结构（已实测）：
      - 未填写时为 []
      - 已填写时为 {deptId: {causeAnalysis, nextStep}} 或 [{deptId, causeAnalysis, nextStep}]
    返回 (cause, step)，未填写时返回 _IFACE 占位。
    """
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
    生成非Bug剔除原因说明。
    优先级：resolution 枚举映射 → type=performance → causeAnalysis 前缀 → 默认
    """
    res = (bug.get("type") or "")
    # slim_bug 没有 resolution，通过 type + tracingBack 推断
    # type=performance 是接口标记非Bug的主要方式
    if "performance" in res:
        # 尝试从 tracingBack 取简短说明
        tracing = (bug.get("tracing_back") or "").strip()
        if tracing:
            short = tracing[:30] + ("…" if len(tracing) > 30 else "")
            return f"优化项：{short}"
        return "优化项"
    # fallback
    return _MANUAL


def _severity_label(bug: dict, with_scope: bool = False) -> str:
    """生成严重程度标签，可附带影响范围。"""
    sev   = str(bug.get("severity") or "")
    label = SEVERITY_LABEL.get(sev, f"severity={sev}")
    if with_scope:
        scope = SEVERITY_SCOPE.get(sev, "")
        return f"{label} {scope}" if scope else label
    return label


# ─── 外部 Bug 计算 ─────────────────────────────────────────────────────────────

def calc_ext_bugs(bugs: list[dict], dept_review: dict) -> dict:
    """
    对外部 Bug 进行分组和深度分析数据整理。

    输入：
      bugs        → get_version_bugs 返回的 slim_bug 列表（全量，含内部Bug）
      dept_review → get_version_bugs 返回的 dept_review 原始 dict

    返回：
    {
      "all_count":       N,       # classification in (1,2) 总数
      "review_count":    N,       # 去掉 type=performance 后的复盘Bug数
      "excl_count":      N,       # 剔除的非Bug数
      "excl_list":       [...],   # 剔除列表（id/title/link/excl_reason）
      "review_list":     [...],   # 复盘Bug列表（含缺陷等级/部门/争议标注）
      "test_dept_count": N,       # 复盘Bug中测试部（45）归属数
      "test_bug_ids":    [...],   # 测试部归属的Bug ID列表
      "other_dept_dist": {...},   # 非测试部的部门Bug数分布
      "deep_analysis":   [...],   # 每条复盘Bug的深度分析数据
    }
    """
    ext_all    = [b for b in bugs if b.get("classification") in ONLINE_BUG_CLASSIFICATIONS]
    ext_review = [b for b in ext_all if "performance" not in (b.get("type") or "")]
    ext_excl   = [b for b in ext_all if "performance"     in (b.get("type") or "")]

    # 剔除列表
    excl_list = [
        {
            "id":          b["id"],
            "title":       (b.get("title") or "").strip(),
            "link":        _bug_link(b),
            "excl_reason": _excl_reason(b),
        }
        for b in ext_excl
    ]

    # 复盘Bug列表
    review_list = []
    for b in ext_review:
        bid      = b["id"]
        is_dis   = _is_dispute(b, dept_review)
        dept_str = "、".join(_dept_names_for_bug(b, dept_review)) or _IFACE
        review_list.append({
            "id":            bid,
            "title":         (b.get("title") or "").strip(),
            "link":          _bug_link(b),
            "severity":      str(b.get("severity") or ""),
            "severity_label": _severity_label(b, with_scope=True),
            "dept_str":      dept_str + "（争议）" if is_dis else dept_str,
            "is_dispute":    is_dis,
        })

    # 测试部归属
    test_bugs = [
        b for b in ext_review
        if "45" in _dept_ids_for_bug(b, dept_review)
    ]
    test_count  = len(test_bugs)
    test_bug_ids = [b["id"] for b in test_bugs]

    # 其他部门分布（非测试部）
    other_dist: Counter = Counter()
    for b in ext_review:
        for d in _dept_ids_for_bug(b, dept_review):
            if d != "45":
                other_dist[to_display(DEPT_MAP.get(d, f"部门{d}"))] += 1

    # 深度分析
    deep_analysis = []
    for b in ext_review:
        bid    = b["id"]
        dids   = _dept_ids_for_bug(b, dept_review)
        is_dis = _is_dispute(b, dept_review)
        depts  = []
        for did in dids:
            cause, step = _get_review_detail(dept_review, bid, did)
            dname = to_display(DEPT_MAP.get(did, f"部门{did}"))
            depts.append({
                "id":         did,
                "name":       dname,
                "cause":      cause,
                "step":       step,
                "is_dispute": is_dis and did == "45",
            })
        deep_analysis.append({
            "id":            bid,
            "title":         (b.get("title") or "").strip(),
            "link":          _bug_link(b),
            "severity_label": _severity_label(b, with_scope=True),
            "tracing":       (b.get("tracing_back") or "").strip() or _IFACE,
            "depts":         depts,
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
    """
    对内部 Bug 进行分组和深度分析数据整理。

    返回：
    {
      "total_count":      N,
      "extreme_count":    N,       # severity=1
      "high_count":       N,       # severity=2
      "extreme_dept_dist":{...},   # 极严重按部门分布
      "high_dept_dist":   {...},   # 高等缺陷按部门分布
      "review_list":      [...],   # 极严重+高等+典型（去重，按严重程度排序）
      "deep_analysis":    [...],   # 复盘Bug深度分析
    }
    """
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

    # 复盘列表：极严重 + 高等 + 典型（去重，按严重程度升序）
    seen = set()
    review_list = []
    for b in sorted(int_all, key=lambda x: int(x.get("severity") or 9)):
        bid = b["id"]
        if bid in seen:
            continue
        if (
            str(b.get("severity") or "") in ("1", "2")
            or b.get("is_typical")
        ):
            seen.add(bid)
            dept_str = "、".join(_dept_names_for_bug(b, dept_review)) or _IFACE
            review_list.append({
                "id":            bid,
                "title":         (b.get("title") or "").strip(),
                "link":          _bug_link(b),
                "severity":      str(b.get("severity") or ""),
                "severity_label": _severity_label(b),
                "dept_str":      dept_str,
                "is_typical":    bool(b.get("is_typical")),
            })

    # 深度分析（只对复盘列表中的Bug做）
    review_ids = {item["id"] for item in review_list}
    deep_analysis = []
    for b in int_all:
        if b["id"] not in review_ids:
            continue
        bid  = b["id"]
        dids = _dept_ids_for_bug(b, dept_review)
        depts = []
        for did in dids:
            cause, step = _get_review_detail(dept_review, bid, did)
            depts.append({
                "id":    did,
                "name":  to_display(DEPT_MAP.get(did, f"部门{did}")),
                "cause": cause,
                "step":  step,
            })
        deep_analysis.append({
            "id":            bid,
            "title":         (b.get("title") or "").strip(),
            "link":          _bug_link(b),
            "severity_label": _severity_label(b),
            "is_typical":    bool(b.get("is_typical")),
            "depts":         depts,
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
    min_bug_count: int = 5,
) -> list[dict]:
    """
    按 Bug 数量排名，找出本版本提测质量较差的任务。

    规则：
      - 以 bug.main_task_id 关联到 pool.task_id
      - Bug 数 >= min_bug_count 的任务才纳入
      - 极严重/高等 Bug 数单独统计（severity in 1,2）
      - 按 Bug 总数降序排列

    返回：
    [
      {
        "rank":              1,
        "task_id":           "45160",
        "name":              "会员日自动派送需求",
        "link":              "[TASK#45160 ...](url)",
        "bug_count":         37,
        "high_extreme_count": 0,
        "dept":              "PHP1组",
        "judgment_prefix":   "🔴提测质量极差",  # Claude 在后面补充说明
      }
    ]
    """
    # 按 main_task_id 统计 Bug 数（包含总数和极/高Bug数）
    bug_count:        Counter = Counter()
    high_ext_count:   Counter = Counter()

    for b in bugs:
        tid = b.get("main_task_id")
        if not tid:
            continue
        bug_count[tid] += 1
        if str(b.get("severity") or "") in ("1", "2"):
            high_ext_count[tid] += 1

    # Pool ID → Pool 映射（slim_pool 用 task_id 字段）
    pool_by_taskid = {p.get("task_id", ""): p for p in pools}

    result = []
    for tid, cnt in bug_count.most_common():
        if cnt < min_bug_count:
            break
        pool = pool_by_taskid.get(str(tid), {})
        name = (pool.get("title") or "").strip() or _MANUAL
        # 部门：从 task_details 不在此，用 pool 的 php_group 推断（显示名）
        # slim_pool 没有直接的部门字段，先留空供 report 层补充
        result.append({
            "rank":              len(result) + 1,
            "task_id":           str(tid),
            "name":              name,
            "link":              _task_link(str(tid), name) if name != _MANUAL else _MANUAL,
            "bug_count":         cnt,
            "high_extreme_count": high_ext_count.get(tid, 0),
            "dept":              _MANUAL,   # 需 report 层从 task_details 补充
            "judgment_prefix":   "🔴提测质量极差" if cnt >= 10 else "🟡提测质量需关注",
        })

    return result


# ─── 版本需求数量 ───────────────────────────────────────────────────────────────

def calc_req_counts(pools: list[dict]) -> dict:
    """
    统计当前版本的需求数量（排除已取消）。

    返回：
    {
      "ext_reqs": N,   # category in (version, operation)
      "int_reqs": N,   # category == internal
    }
    """
    active = [p for p in pools if p.get("task_status") != "cancel"]
    return {
        "ext_reqs": sum(1 for p in active if p.get("category") in ("version", "operation")),
        "int_reqs": sum(1 for p in active if p.get("category") == "internal"),
    }
