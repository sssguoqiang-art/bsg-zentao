"""
tools/calc_bug_review.py

Bug 复盘预分类计算逻辑。

对外暴露：
    calc_bug_review(client, version_id, project_id) -> dict
    save_bug_review_report(content, vname) -> str

核心规则（Bug归因裁判手册 v1.1 + 0408数据验证 + 模板v2优化）：
  ① type=performance 外部Bug → 疑似非Bug区块，不做归属分析
  ② type=performance 内部Bug → 从总量排除，不出现在报告中
  ③ bugTypeParent=4 ≠ 非Bug（实际是用户体验类Bug，正常参与复盘）
  ④ 责任部门：ownerDept → deptReview.depts → 空（禁止回落到 deptName）
  ⑤ 争议预测：不依赖 isDispute 人工标记，从数据信号推断争议原因和复盘价值
  ⑥ 低质量任务：多维度识别，区分"提测质量差"/"需求质量差"/"混合型"
"""

import re
from collections import Counter
from datetime import datetime
from html import unescape
from typing import Optional

import requests

from bsg_zentao.client import BASE_URL, ZentaoClient, _parse_json
from bsg_zentao.constants import DEPT_MAP, to_display
from bsg_zentao.utils import get_report_path

# ─── URL 生成 ─────────────────────────────────────────────────────────────────

def bug_url(bid: str) -> str:
    return f"https://cd.baa360.cc:20088/index.php?m=bug&f=view&bugID={bid}"

def task_url(tid: str) -> str:
    return f"https://cd.baa360.cc:20088/index.php?m=task&f=view&taskID={tid}"

# ─── 常量 ─────────────────────────────────────────────────────────────────────

SEV_MAP = {"1": "极", "2": "高", "3": "中", "4": "低"}
SEV_FULL_MAP = {
    "1": "极严重",
    "2": "高等缺陷",
    "3": "中等缺陷",
    "4": "低等缺陷",
}

BUG_TYPE_MAP = {
    "1": "开发Bug",
    "2": "产品Bug",
    "3": "美术Bug",
    "4": "用户体验Bug",  # ⚠️ bugTypeParent=4 是用户体验，不是非Bug
    "5": "测试Bug",
    "6": "需求问题",
}

# 需求分歧类关键词（触发争议预测）
DISPUTE_KEYWORDS = [
    "需求如此", "需求未提到", "需求不清晰", "需求没写", "需求有歧义",
    "需求没提", "需求未提及", "需求未明确", "需求无说明",
    "之前没有要求", "原来没有", "历史功能", "以前已验收",
]

# 建议类关键词（需外部信息确认）
SUGGEST_KEYWORDS = ["皮肤套", "资源未", "美术", "多语言", "文案"]

# 低质量任务：需求类根因关键词
REQUIREMENT_CAUSE_KEYWORDS = [
    "需求", "产品", "设计缺失", "未定义", "边界未", "场景未", "需求文档",
]


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _get_dept_names(b: dict, dept_review: dict) -> list[str]:
    """ownerDept → deptReview.depts → 空（⚠️ 禁止回落到 deptName）"""
    raw = (b.get("ownerDept") or "").strip()
    if raw:
        return [to_display(DEPT_MAP.get(i.strip(), f"部门{i}")) for i in raw.split(",") if i.strip()]
    bid = str(b.get("id", ""))
    dr_depts = (dept_review.get(bid) or {}).get("depts", [])
    if dr_depts:
        return [to_display(DEPT_MAP.get(str(d), f"部门{d}")) for d in dr_depts]
    return []


def _is_cause_invalid(cause: str) -> bool:
    c = (cause or "").strip()
    if not c:
        return True
    if re.fullmatch(r"[\d\s]+", c):
        return True
    if len(c) <= 1:
        return True
    return False


def _get_cause(b: dict) -> str:
    """优先取 causeAnalysis，为空取 tracingBack"""
    ca = (b.get("causeAnalysis") or "").strip()
    tb = (b.get("tracingBack") or "").strip()
    return ca or tb


def _one_line(text: str, limit: int = 80) -> str:
    text = re.sub(r"[\r\n\t]+", " ", (text or "").strip())
    text = re.sub(r"\s{2,}", " ", text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _strip_html(text: str) -> str:
    text = text or ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_task_id_from_url(url: str) -> str:
    url = (url or "").strip()
    m = re.search(r"taskID=(\d+)", url)
    return m.group(1) if m else ""


def _extract_story_id_from_url(url: str) -> str:
    url = (url or "").strip()
    m = re.search(r"(?:storyID|story)=(\d+)", url)
    return m.group(1) if m else ""


def _extract_result_from_steps(steps: str) -> str:
    text = _strip_html(steps)
    if not text:
        return ""
    m = re.search(r"\[结果\]\s*(.*?)(?=\[期望\]|$)", text, re.S)
    if m:
        result = _one_line(m.group(1).strip(), 80)
        if result and result not in {"[结果]", "[期望]", "[步骤]"}:
            return result
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marker_lines = {"[步骤]", "[结果]", "[期望]", "环境：", "前置条件："}
    for line in lines:
        if line in marker_lines:
            continue
        if re.match(r"^[0-9]+[.、]", line):
            return _one_line(line, 80)
    for line in lines:
        if line in marker_lines:
            continue
        if "[步骤]" in line or "[结果]" in line or "[期望]" in line:
            continue
        if any(kw in line for kw in ["哪个平台", "哪个环境", "环境：", "前置条件"]):
            continue
        if len(line) >= 8:
            return _one_line(line, 80)
    return ""


def _fetch_bug_detail(client: ZentaoClient, bug_id: str) -> dict:
    try:
        resp = client._session.get(
            BASE_URL,
            params={"m": "bug", "f": "view", "bugID": str(bug_id), "getData": "1", "t": "json"},
            timeout=30,
        )
        outer = _parse_json(resp.text)
        data = outer.get("data")
        inner = _parse_json(data) if isinstance(data, str) else outer
        return inner.get("bug", {}) if isinstance(inner, dict) else {}
    except Exception:
        return {}


def _fetch_task_detail(client: ZentaoClient, task_id: str) -> dict:
    try:
        resp = client._session.get(
            BASE_URL,
            params={"m": "task", "f": "view", "taskID": str(task_id), "getData": "1", "t": "json"},
            timeout=30,
        )
        data = _parse_json(resp.text)
        return data.get("task", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fetch_link_preview(client: Optional[ZentaoClient], url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        if "cd.baa360.cc:20088" in url and client is not None:
            resp = client._session.get(url, timeout=20, allow_redirects=True)
        else:
            resp = requests.get(url, timeout=20, verify=False, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        ctype = resp.headers.get("content-type", "")
        if "text/html" not in ctype and "text/plain" not in ctype:
            return ""
        text = resp.text
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            title = _one_line(_strip_html(m.group(1)), 80)
            if title:
                return title
        return _one_line(_strip_html(text), 120)
    except Exception:
        return ""


def _severity_display(b: dict) -> str:
    return SEV_FULL_MAP.get(str(b.get("severity", "") or ""), str(b.get("severity", "") or ""))


def _resolve_phenomenon(b: dict, cause: str, steps: str = "") -> str:
    phe = (b.get("phenomenon") or "").strip()
    if phe:
        return phe
    step_result = _extract_result_from_steps(steps)
    if step_result:
        return step_result
    return _infer_phenomenon(b, cause)


def _resolve_scope(b: dict, cause: str) -> str:
    scope = (b.get("scopeInfluence") or "").strip()
    if scope:
        return scope
    return _assess_impact(b, cause)


def _humanize_scope(scope: str) -> str:
    scope = (scope or "").strip()
    if not scope:
        return ""
    normalized = scope.replace("【", "").replace("】", " ")
    normalized = re.sub(r"\s{2,}", " ", normalized).strip()

    mapping = {
        "严重 涉及资金/结算流程，影响玩家实际权益": "影响较重，可能直接影响玩家资金或结算结果",
        "较大 高等级缺陷，影响范围广或影响核心功能": "影响范围较大，已经触及核心功能",
        "较大 后台阻断性异常，影响运营/客服正常处理业务": "会直接影响后台业务处理，运营或客服侧感知明显",
        "影响不大 后台数据展示问题，不影响线上玩家": "主要影响后台展示或核对，对线上玩家影响较小",
        "影响不大 属改进/优化类，现有功能可正常使用": "更偏优化或改进项，现有功能仍可正常使用",
        "一般 功能性异常，玩家可感知但不影响核心流程": "玩家可感知到异常，但暂时不影响核心流程",
        "一般 中等级缺陷，影响范围有限": "影响范围相对有限，主要是局部功能异常",
        "影响不大 低等级缺陷，不影响核心流程": "整体影响较小，不影响核心流程",
        "影响极小": "整体影响很小",
        "有一定影响": "整体已有一定影响",
        "影响较大": "整体影响较大",
        "影响不大": "整体影响较小",
    }
    return mapping.get(normalized, normalized)


def _split_responsibility(dept_names: list[str]) -> tuple[str, str, str]:
    seen = set()
    ordered = []
    for name in dept_names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)

    if not ordered:
        return "待确认", "", "待确认"

    test_names = {"测试组", "测试部"}
    primary = [d for d in ordered if d not in test_names]
    secondary = [d for d in ordered if d in test_names]

    if not primary and secondary:
        primary_str = "、".join(secondary)
        secondary_str = ""
    else:
        primary_str = "、".join(primary) if primary else "待确认"
        secondary_str = "、".join(secondary)

    label_parts = [primary_str]
    if secondary_str:
        label_parts.append(secondary_str)
    return primary_str, secondary_str, "、".join([p for p in label_parts if p])


def _determine_bug_status(review_rec: str) -> str:
    if review_rec == "复盘价值有限":
        return "待确认"
    return "是"


def _build_exclude_reason(b: dict, cause: str) -> str:
    exclusion = (b.get("exclusionReason") or b.get("exclusion_reason") or "").strip()
    if exclusion:
        return exclusion

    res = b.get("resolution", "")
    title = b.get("title", "")

    if res == "bydesign":
        return "该问题属于设计如此或体验调整，不纳入 Bug 复盘。"
    if "performance" in (b.get("type") or ""):
        if any(kw in title for kw in ["文案", "样式", "字体", "颜色", "图标", "左对齐", "展示"]):
            return "该问题在禅道中标记为 performance，更接近展示/体验优化，不纳入 Bug 复盘。"
        if any(kw in cause for kw in ["优化", "调整", "展示", "设计如此"]):
            return "该问题在禅道中标记为 performance，且原因描述偏优化项，按非 Bug 处理。"
        return "该问题在禅道中标记为 performance，按当前规则归为非 Bug，不纳入复盘。"
    return "该问题不满足纳入复盘条件，按非 Bug 处理。"


def _build_decision_reason(
    *,
    phenomenon: str,
    scope: str,
    cause: str,
    dispute_reason: str,
    review_rec: str,
    demand: str,
    use_case: str,
    resolution: str = "",
    primary_resp: str = "",
    secondary_resp: str = "",
    task_title: str = "",
    task_context: str = "",
    demand_context: str = "",
    use_case_context: str = "",
) -> str:
    evidence_parts = []
    if phenomenon and phenomenon != "【空】":
        evidence_parts.append(f"Bug详情现象：{_one_line(phenomenon, 40)}")
    if scope:
        evidence_parts.append(f"影响范围：{_one_line(_humanize_scope(scope), 24)}")
    if task_title:
        evidence_parts.append(f"已挂关联需求：《{_one_line(task_title, 28)}》")
    elif demand:
        evidence_parts.append("已挂关联需求")
    if use_case:
        evidence_parts.append("已挂关联用例")
    if not _is_cause_invalid(cause):
        evidence_parts.append(f"原因记录：{_one_line(cause, 42)}")
    if resolution in {"tostory", "external", "bydesign"}:
        resolution_map = {
            "tostory": "当前处理结果：转需求",
            "external": "当前处理结果：外部原因",
            "bydesign": "当前处理结果：设计如此",
        }
        evidence_parts.append(resolution_map[resolution])

    owner_hint = "责任归属待确认。"
    if primary_resp and primary_resp != "待确认":
        owner_hint = f"当前建议主责：{primary_resp}"
        if secondary_resp:
            owner_hint += f"；次责：{secondary_resp}"
        owner_hint += "。"

    context_text = ""
    if task_context:
        context_text = "关联需求内容显示该问题位于对应功能链路中。"
    elif demand_context:
        context_text = "已挂需求链接内容可支持该问题与需求链路存在关联。"
    elif use_case_context:
        context_text = "关联用例信息可作为该问题验证范围的辅助依据。"

    cause_short = _one_line(cause, 36) if not _is_cause_invalid(cause) else ""
    if dispute_reason:
        ai_judgment = f"当前争议明显，核心点在于{dispute_reason}；从现象看问题已具备复盘价值，但是否按 Bug 归类建议会前先确认。"
    elif review_rec == "复盘价值有限":
        ai_judgment = "当前更偏优化项、需求边界或非标准 Bug，是否纳入正式复盘建议再确认。"
    elif _is_cause_invalid(cause):
        if task_context or demand_context:
            ai_judgment = "原因记录不足，但它与当前需求链路有关，说明不是孤立问题；建议先纳入复盘，再补充责任依据。"
        elif use_case_context:
            ai_judgment = "原因记录不足，但已有用例可作验证依据，建议先纳入复盘，再补充责任依据。"
        else:
            ai_judgment = "原因记录不足，但现象明确且可感知，建议先纳入复盘，再补充责任依据。"
    elif resolution == "tostory":
        ai_judgment = f"原因记录指向“{cause_short}”，但当前已转需求处理，说明团队更倾向按需求口径处理；复盘时建议确认这是否属于原需求边界遗漏。"
    elif resolution == "external":
        ai_judgment = f"原因记录指向“{cause_short}”，但当前处理结果为外部原因，说明系统自身缺陷证据不足；复盘时建议先确认是否属于外部数据或环境问题。"
    elif resolution == "bydesign":
        ai_judgment = f"原因记录指向“{cause_short}”，且当前记录为设计如此，说明问题更可能落在需求或产品口径；复盘时建议先确认是否属于预期行为。"
    elif task_context:
        ai_judgment = f"原因记录指向“{cause_short}”，且已挂需求内容与当前功能链路一致，这条更像实现遗漏或逻辑异常，可按复盘 Bug 跟进。"
    elif demand_context:
        ai_judgment = f"原因记录指向“{cause_short}”，且已挂需求与当前现象直接相关，这条可按复盘 Bug 跟进。"
    elif use_case_context:
        ai_judgment = f"原因记录指向“{cause_short}”，且关联用例可作为验证依据，这条问题可按复盘 Bug 跟进。"
    else:
        ai_judgment = f"原因记录指向“{cause_short}”，与当前现象能够对上，这条更像实际功能异常或实现遗漏，可按复盘 Bug 跟进。"

    lines = []
    if evidence_parts:
        lines.append("证据摘要：")
        for idx, part in enumerate(evidence_parts[:5], 1):
            lines.append(f"{idx}. {part}")
    if context_text:
        lines.append(f"辅助信息：{context_text}")
    lines.append(f"AI判断：{ai_judgment}")
    lines.append(f"归属建议：{owner_hint}")
    return "\n".join(lines)


def _resolve_version_name(raw: dict, version_id: str) -> str:
    vid = str(version_id)
    for key in ("showVersions", "unshowVersions"):
        versions = raw.get(key, {})
        if isinstance(versions, dict) and vid in versions:
            return str(versions[vid]).strip()

    execution_versions = raw.get("executionVersions", {})
    if isinstance(execution_versions, dict):
        item = execution_versions.get(vid) or execution_versions.get(int(version_id)) if version_id.isdigit() else None
        if isinstance(item, dict):
            return (item.get("name") or "").strip() or f"版本{version_id}"
        if item:
            return str(item).strip()

    return f"版本{version_id}"


def _resolve_version_name_from_client(client: ZentaoClient, version_id: str, force_refresh: bool = False) -> str:
    vname = _resolve_version_name({}, version_id)
    for status in ("undone", "closed"):
        data = client.fetch_versions(status=status, force_refresh=force_refresh)
        for e in data.get("executionStats", []):
            if str(e.get("id", "")) == str(version_id):
                return (e.get("name") or "").strip() or vname
    return vname


def _format_markdown_link(label: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"[{label}]({url})"


def _summarize_task_context(task_detail: dict) -> tuple[str, str]:
    if not task_detail:
        return "", ""
    title = (task_detail.get("storyTitle") or task_detail.get("name") or "").strip()
    parts = [
        _strip_html(task_detail.get("storySpec") or ""),
        _strip_html(task_detail.get("storyCustomDemandSpec") or ""),
        _strip_html(task_detail.get("storyVerify") or ""),
        _strip_html(task_detail.get("desc") or ""),
    ]
    merged = " ".join(part for part in parts if part)
    merged = re.sub(r"\s{2,}", " ", merged).strip()
    return title, _one_line(merged, 180)


def _enrich_bug_context(
    client: ZentaoClient,
    bug: dict,
    bug_detail_cache: dict[str, dict],
    task_detail_cache: dict[str, dict],
    link_preview_cache: dict[str, str],
) -> dict:
    bid = str(bug.get("id", ""))
    if bid not in bug_detail_cache:
        bug_detail_cache[bid] = _fetch_bug_detail(client, bid)
    detail = bug_detail_cache.get(bid) or {}

    source = {**bug, **detail}

    demand_url = (source.get("demand") or "").strip()
    use_case_url = (source.get("useCase") or "").strip()
    task_id, task_name = _get_task_ref(source)
    if not task_id and demand_url:
        task_id = _extract_task_id_from_url(demand_url)
    task_detail = {}
    if task_id:
        if task_id not in task_detail_cache:
            task_detail_cache[task_id] = _fetch_task_detail(client, task_id)
        task_detail = task_detail_cache.get(task_id) or {}

    task_title, task_context = _summarize_task_context(task_detail)
    task_label = task_title or task_name or (f"TASK#{task_id}" if task_id else "")
    demand_display = ""
    demand_context = ""
    if demand_url:
        demand_display = _format_markdown_link(task_label or "关联需求", demand_url)
        if not task_context:
            if demand_url not in link_preview_cache:
                link_preview_cache[demand_url] = _fetch_link_preview(client, demand_url)
            demand_context = link_preview_cache.get(demand_url, "")
    elif task_id:
        demand_display = _format_markdown_link(task_label or "关联需求", task_url(task_id))

    use_case_display = ""
    use_case_context = ""
    if use_case_url:
        use_case_display = _format_markdown_link("关联用例", use_case_url)
        if use_case_url not in link_preview_cache:
            link_preview_cache[use_case_url] = _fetch_link_preview(client, use_case_url)
        use_case_context = link_preview_cache.get(use_case_url, "")

    steps = (source.get("steps") or "").strip()

    return {
        "source_bug": source,
        "task_detail": task_detail,
        "task_title": task_title or task_name or "",
        "task_context": task_context,
        "demand_context": demand_context,
        "use_case_context": use_case_context,
        "phenomenon": _resolve_phenomenon(source, _get_cause(source), steps),
        "scope": _resolve_scope(source, _get_cause(source)),
        "demand_display": demand_display,
        "use_case_display": use_case_display,
    }


def render_bug_review_markdown(report: dict) -> str:
    lines: list[str] = []

    def W(line: str = ""):
        lines.append(line)

    vname = report.get("vname", "未知版本")
    fetch_time = report.get("fetch_time", "")
    ext_nonbugs = report.get("ext_nonbugs", [])
    ext_review_rows = report.get("ext_review_rows", [])
    ext_analyzed = report.get("ext_analyzed", [])
    int_review_rows = report.get("int_review_rows", [])
    int_analyzed = report.get("int_analyzed", [])

    W(f"# Bug界定报告 · {vname}")
    W("")
    if fetch_time:
        W(f"> 生成时间：{fetch_time}")
        W("> 说明：`是否Bug`、`责任归属`、`判定理由`、`争议预判` 为 AI 预判/生成内容，仅供复盘前参考；`是否争议` 仍沿用当前规则判断。")
        W("")

    W("## 一、外部非Bug列表")
    W("")
    W("| 序号 | 标题 | 原因 | 剔除理由 |")
    W("| --- | --- | --- | --- |")
    if ext_nonbugs:
        for i, item in enumerate(ext_nonbugs, 1):
            W(f"| {i} | {item['link']} | {item['reason']} | {item['exclude_reason']} |")
    else:
        W("| 1 | 无 | 无 | 无 |")
    W("")

    W("## 二、外部复盘Bug列表")
    W("")
    W("| 序号 | 标题 | 缺陷等级 | Bug现象 | 责任部门 |")
    W("| --- | --- | --- | --- | --- |")
    if ext_review_rows:
        for i, item in enumerate(ext_review_rows, 1):
            W(f"| {i} | {item['link']} | {item['severity_label']} | {item['phenomenon']} | {item['responsibility_label']} |")
    else:
        W("| 1 | 无 | 无 | 无 | 无 |")
    W("")

    W("## 三、外部Bug界定")
    W("")
    if ext_analyzed:
        for item in ext_analyzed:
            W(f"### {item['id']} {item['link']}")
            W("")
            W(f"- Bug现象：{item['phenomenon']}")
            W(f"- 缺陷等级：{item['severity_label']}")
            W(f"- 是否Bug（AI预判）：{item['is_bug']}")
            resp = f"主责：{item['primary_resp']}"
            if item['secondary_resp']:
                resp += f"；次责：{item['secondary_resp']}"
            W(f"- 责任归属（AI预判）：{resp}")
            W("- 判定理由（AI生成）：")
            for reason_line in (item["judgment_reason"] or "").splitlines():
                W(f"  {reason_line}")
            if item.get("demand"):
                W(f"- 关联需求：{item['demand']}")
            if item.get("use_case"):
                W(f"- 关联用例：{item['use_case']}")
            W(f"- 是否争议：{'是' if item['is_dispute'] else '否'}")
            if item.get("dispute_prediction"):
                W(f"- 争议预判（AI生成）：{item['dispute_prediction']}")
            W("")
    else:
        W("暂无外部复盘Bug。")
        W("")

    W("## 四、内部复盘Bug列表")
    W("")
    W("| 序号 | 标题 | 缺陷等级 | 是否典型 | 责任部门 |")
    W("| --- | --- | --- | --- | --- |")
    if int_review_rows:
        for i, item in enumerate(int_review_rows, 1):
            W(f"| {i} | {item['link']} | {item['severity_label']} | {item['is_typical']} | {item['responsibility_label']} |")
    else:
        W("| 1 | 无 | 无 | 无 | 无 |")
    W("")

    W("## 五、内部Bug界定")
    W("")
    if int_analyzed:
        for item in int_analyzed:
            W(f"### {item['id']} {item['link']}")
            W("")
            W(f"- Bug现象：{item['phenomenon']}")
            W(f"- 缺陷等级：{item['severity_label']}")
            W(f"- 是否典型：{'是' if item['is_typical'] else '否'}")
            W(f"- 是否Bug（AI预判）：{item['is_bug']}")
            resp = f"主责：{item['primary_resp']}"
            if item['secondary_resp']:
                resp += f"；次责：{item['secondary_resp']}"
            W(f"- 责任归属（AI预判）：{resp}")
            W("- 判定理由（AI生成）：")
            for reason_line in (item["judgment_reason"] or "").splitlines():
                W(f"  {reason_line}")
            if item.get("demand"):
                W(f"- 关联需求：{item['demand']}")
            if item.get("use_case"):
                W(f"- 关联用例：{item['use_case']}")
            W(f"- 是否争议：{'是' if item['is_dispute'] else '否'}")
            if item.get("dispute_prediction"):
                W(f"- 争议预判（AI生成）：{item['dispute_prediction']}")
            W("")
    else:
        W("暂无内部复盘Bug。")
        W("")

    return "\n".join(lines)


def _infer_phenomenon(b: dict, cause: str) -> str:
    """
    从 Bug 标题 + 溯源推断 Bug 现象（客观描述，不是原因）。

    策略：
    1. 清除标题中的标签前缀（【线上Bug】【编号】等），提取核心描述
    2. 如果溯源本身就是现象描述（不含"需求/原因"等主观判断词），优先用溯源
    3. 兜底直接用清理后的标题
    """
    title = b.get("title", "")
    # 清除常见前缀标签：【线上Bug】【内部Bug】【编号/数字】【日期】等
    clean = re.sub(r"【[^】]{0,10}】", "", title).strip()
    clean = re.sub(r"\s+", " ", clean)

    # 溯源如果是客观现象描述（不含"需求/原因/理解"等主观词），可作为现象补充
    cause_is_objective = cause and not _is_cause_invalid(cause) and not any(
        kw in cause for kw in ["需求", "原因", "理解", "未提", "没写", "应该", "建议"]
    )

    if cause_is_objective and len(cause) > len(clean):
        return cause[:80]

    return clean[:80] if clean else title[:80]


def _assess_impact(b: dict, cause: str) -> str:
    """评估外部 Bug 对线上的影响范围（内部 Bug 不调用）"""
    sev   = b.get("severity", "")
    res   = b.get("resolution", "")
    btp   = b.get("bugTypeParent", "")
    title = b.get("title", "")

    is_backend = any(kw in title for kw in ["总台", "后台", "运营", "客服", "报表", "统计"])
    if is_backend:
        if any(kw in title for kw in ["无法操作", "无法登录", "系统错误"]):
            return "【较大】后台阻断性异常，影响运营/客服正常处理业务"
        if any(kw in title for kw in ["报表", "统计", "导出", "列表"]):
            return "【影响不大】后台数据展示问题，不影响线上玩家"

    if any(kw in title for kw in ["优化", "改善"]):
        return "【影响不大】属改进/优化类，现有功能可正常使用"

    money_kw = ["充值", "提现", "奖励", "金币", "打码", "结算"]
    if any(kw in title for kw in money_kw) and sev in ("1", "2"):
        return "【严重】涉及资金/结算流程，影响玩家实际权益"

    if sev == "2":
        return "【较大】高等级缺陷，影响范围广或影响核心功能"

    minor_kw = ["文案", "多语言", "翻译", "文字", "样式", "图标", "提示语"]
    if any(kw in title for kw in minor_kw):
        return "【影响不大】文案/视觉层面问题，不影响功能使用"

    func_kw = ["弹窗", "按钮", "跳转", "页面", "显示"]
    if any(kw in title for kw in func_kw) and sev == "3":
        return "【一般】功能性异常，玩家可感知但不影响核心流程"

    default = {
        "1": "【严重】极高等级缺陷，建议优先核实影响范围",
        "2": "【较大】高等级缺陷，需关注实际影响面",
        "3": "【一般】中等级缺陷，影响范围有限",
        "4": "【影响不大】低等级缺陷，不影响核心流程",
    }
    return default.get(sev, "【一般】影响范围待评估")


def _predict_dispute(b: dict, dept_names: list, cause: str) -> tuple[str, str]:
    """
    ⑤ 从数据信号预测争议原因和复盘建议。
    不依赖人工 isDispute 标记（周三运行时还没标注）。

    返回：(dispute_reason, review_recommendation)
        dispute_reason:       空字符串 = 无争议；非空 = 具体争议描述
        review_recommendation: "建议复盘" / "需会前确认" / "复盘价值有限"

    判断逻辑（按优先级）：
    A. Bug 性质存疑（是否真的是Bug）
       → resolution=external/tostory，或溯源明确是优化/配置
       → 复盘价值有限，应先确认是否排除
    B. Bug 边界不清（归属争议）
       → 溯源含需求分歧关键词，或无归属信息
       → 需会前确认改动边界或补充溯源
    C. 溯源信息不足
       → causeAnalysis/tracingBack 为空或无效
       → 无法判断，需补充
    D. 根因清晰，有明确改进空间
       → 以上均不命中
       → 建议复盘
    """
    res = b.get("resolution", "")
    btp = b.get("bugTypeParent", "")

    # A. Bug 性质存疑
    if res == "external":
        return ("配置/外部环境问题，非代码缺陷，属于Bug的性质存疑", "复盘价值有限")
    if res == "tostory":
        return ("已转为需求处理，是否属于Bug存疑", "复盘价值有限")
    optimize_kw = ["优化", "改善", "建议", "之前没有要求", "原来没有要求"]
    if any(kw in cause for kw in optimize_kw) and not _is_cause_invalid(cause):
        return ("溯源描述属优化/改进性质，Bug边界不清晰", "复盘价值有限")

    # B. Bug 边界不清（归属争议）
    matched_kw = next((kw for kw in DISPUTE_KEYWORDS if kw in cause), None)
    if matched_kw:
        return (
            f"溯源含「{matched_kw}」，Bug改动边界存在争议，责任归属需对照需求文档裁定",
            "需会前确认",
        )
    if not dept_names:
        return ("责任部门未填写（ownerDept 与 deptReview 均为空），无法界定归属", "需会前确认")

    # C. 溯源信息不足
    if _is_cause_invalid(cause):
        return ("溯源缺失或无效，无法评估是否值得复盘", "需会前确认")

    # D. 根因清晰
    return ("", "建议复盘")


def _classify_type(b: dict, dispute_reason: str, cause: str) -> str:
    """确定 / 建议 / 争议"""
    if dispute_reason:
        return "争议"
    if any(kw in cause for kw in SUGGEST_KEYWORDS):
        return "建议"
    if b.get("bugTypeParent") == "3":
        return "建议"
    return "确定"


def _test_responsibility(b: dict, dispute_reason: str, dept_names: list) -> str:
    """外部 Bug 测试责任标注"""
    res   = b.get("resolution", "")
    cause = (b.get("causeAnalysis") or "").strip()
    btp   = b.get("bugTypeParent", "")

    if res in ("external", "tostory"):
        return "；测试不担责"
    if btp == "2" or "转需求" in cause:
        return "；测试不担责"
    if not dept_names:
        return "；测试责任待定"
    return " + 测试次责"


def _build_judgment(b: dict, dispute_reason: str, cls_type: str,
                    dept_names: list, cause: str) -> str:
    """生成归属判定说明文字"""
    res = b.get("resolution", "")

    if cls_type == "争议":
        lines = ["**复盘需明确**：", f"- {dispute_reason}"]
        if "需求文档" in dispute_reason or "改动边界" in dispute_reason:
            lines.append("适用手册规则 1（需求分歧裁定）。")
        if "ownerDept" in dispute_reason:
            lines.append("复盘前请责任方补充 ownerDept 与溯源分析。")
        return " ".join(lines)

    if cls_type == "建议":
        if "皮肤套" in cause or "资源未" in cause:
            return "**需确认**：本期美术是否已交付对应皮肤资源？若未交付则归属修正为美术 Bug。"
        if "多语言" in cause or "文案" in cause:
            return "**需确认**：相关文案是否在需求文档中明确定义？→ 有定义但实现错误 = 开发 Bug；→ 未定义 = 产品 Bug（适用手册规则 1）。"
        return f"**需确认**：溯源描述（\"{cause[:40]}\"），请补充具体改动关联任务。"

    if res == "fixed" and cause:
        return f"{cause[:60]}。归属明确，无争议。"
    return "规则清晰，归属无争议。"


def _get_task_ref(b: dict) -> tuple[Optional[str], str]:
    """获取关联主任务 ID 和名称"""
    mid   = b.get("mainTaskId", 0)
    mname = (b.get("mainTaskName") or "").strip()
    tid   = b.get("task", "0")
    tname = (b.get("taskName") or "").strip()
    use_mid = str(mid) if mid and mid != 0 and str(mid) != "0" else (
              tid if tid not in ("0", 0, None, "") else None)
    return use_mid, mname or tname


def _classify_lq_root_cause(bugs: list, non_perf_bugs: dict) -> tuple[str, list[str]]:
    """
    ⑥ 低质量任务根因分类。

    输入：
        bugs：该任务的 Bug 摘要列表（含 id/sev/isTypical/cause/btp 字段）
        non_perf_bugs：全量非performance Bug id → Bug对象，用于取详细字段

    返回：
        (root_type, dimensions)
        root_type: "提测质量差" / "需求质量差" / "混合型"
        dimensions: 触发的低质量维度标签列表
    """
    dims = []
    total = len(bugs)
    high_cnt = sum(1 for b in bugs if b["sev"] in ("1", "2") or b["isTypical"] == "1")

    # 基础维度
    if total >= 5:
        dims.append(f"Bug数量多（{total}条）")
    if high_cnt >= 1:
        dims.append(f"含高等级/典型Bug×{high_cnt}")

    # 判断是否有主流程异常（极/高Bug存在且标题含核心功能词）
    core_kws = ["主流程", "无法", "空白", "失败", "异常", "错误", "缺失"]
    has_core_fail = any(
        b["sev"] in ("1", "2") and any(kw in b.get("title", "") for kw in core_kws)
        for b in bugs
    )
    if has_core_fail:
        dims.append("主流程异常")

    # 同类根因重复（causeAnalysis 前8字高度相似）
    causes = [b.get("cause", "")[:8] for b in bugs if not _is_cause_invalid(b.get("cause", ""))]
    if len(causes) >= 3:
        most_common, cnt = Counter(causes).most_common(1)[0]
        if cnt >= 2 and most_common.strip():
            dims.append(f"同类根因重复（×{cnt}）")

    # 争议集中（多条Bug有争议信号）
    dispute_cnt = sum(1 for b in bugs if b.get("has_dispute"))
    if dispute_cnt >= 2:
        dims.append(f"需求边界不清晰（{dispute_cnt}条争议）")

    # ── 根因分类 ──────────────────────────────────────────────────────────────
    # 统计"需求类"Bug数量：bugTypeParent=2 或 causeAnalysis 含需求分歧关键词
    req_cnt = sum(
        1 for b in bugs
        if b.get("btp") == "2"
        or any(kw in b.get("cause", "") for kw in REQUIREMENT_CAUSE_KEYWORDS)
    )
    req_ratio = req_cnt / total if total > 0 else 0

    if req_ratio >= 0.6:
        root_type = "需求质量差"
        dims.append("多条Bug源于需求描述不完整")
    elif req_ratio >= 0.3:
        root_type = "混合型"
        dims.append("部分Bug源于需求，部分源于自测不足")
    else:
        root_type = "提测质量差"

    return root_type, dims


# ─── 主分析函数 ───────────────────────────────────────────────────────────────

def calc_bug_review(
    client: ZentaoClient,
    version_id: str,
    project_id: str,
    force_refresh: bool = False,
) -> dict:
    """
    返回结构：
    {
        vname, vid, fetch_time,
        ext_perf:      [...]  # 疑似非Bug（type=performance 外部Bug）
        ext_analyzed:  [...]  # 外部Bug责任界定
        int_analyzed:  [...]  # 内部Bug责任界定（极/高/典型）
        low_quality:   [...]  # 低质量任务
        watch_list:    [...]  # 关注任务（Bug数≥2）
        dept_ext, dept_int,   # 部门Bug统计
        summary: {            # 摘要数字（供报告头部四格卡）
            ext_review_cnt, int_review_cnt,
            ext_dispute_cnt, int_dispute_cnt,
            lq_task_cnt,
        }
        total_raw, non_bug_cnt, ext_total, int_total, int_other
    }
    """
    raw         = client.fetch_bugs(version_id, project_id, force_refresh=force_refresh)
    bugs        = raw.get("bugs", [])
    dept_review = raw.get("deptReview", {})
    vname       = raw.get("versionName", "") or _resolve_version_name(raw, version_id)
    if vname == f"版本{version_id}":
        vname = _resolve_version_name_from_client(client, version_id, force_refresh=force_refresh)
    fetch_time  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bug_detail_cache: dict[str, dict] = {}
    task_detail_cache: dict[str, dict] = {}
    link_preview_cache: dict[str, str] = {}

    # ── 非Bug分流 ──────────────────────────────────────────────────────────────
    all_perf = [b for b in bugs if b.get("type") == "performance"]
    non_perf = [b for b in bugs if b.get("type") != "performance"]

    ext_perf   = [b for b in all_perf  if b.get("classification") in ("1", "2")]
    ext_review = [b for b in non_perf  if b.get("classification") in ("1", "2")]
    int_bugs   = [b for b in non_perf  if b.get("classification") in ("4", "5")]
    int_review = [b for b in int_bugs
                  if b.get("severity") in ("1", "2") or b.get("isTypical") == "1"]

    # ── 部门统计 ───────────────────────────────────────────────────────────────
    dept_ext: dict[str, int] = {}
    dept_int: dict[str, int] = {}
    for b in ext_review:
        for nm in (_get_dept_names(b, dept_review) or ["未归属"]):
            dept_ext[nm] = dept_ext.get(nm, 0) + 1
    for b in int_review:
        for nm in (_get_dept_names(b, dept_review) or ["未归属"]):
            dept_int[nm] = dept_int.get(nm, 0) + 1

    # ── 疑似非Bug 整理 ─────────────────────────────────────────────────────────
    ext_perf_list = []
    for b in ext_perf:
        ctx = _enrich_bug_context(client, b, bug_detail_cache, task_detail_cache, link_preview_cache)
        source_bug = ctx["source_bug"]
        cause = _get_cause(source_bug)
        ext_perf_list.append({
            "id":         str(source_bug.get("id", "")),
            "title":      source_bug.get("title", ""),
            "link":       f"[{str(source_bug.get('id', ''))} {source_bug.get('title', '')}]({bug_url(str(source_bug.get('id', '')))})",
            "phenomenon": ctx["phenomenon"],
            "resolution": source_bug.get("resolution", ""),
            "reason":     _one_line(cause or "【空】", 100),
            "exclude_reason": _build_exclude_reason(source_bug, cause),
        })

    # ── 外部Bug 逐条分析 ───────────────────────────────────────────────────────
    ext_analyzed = []
    ext_review_rows = []
    for b in ext_review:
        ctx = _enrich_bug_context(client, b, bug_detail_cache, task_detail_cache, link_preview_cache)
        source_bug = ctx["source_bug"]
        bid    = str(source_bug.get("id", ""))
        cause  = _get_cause(source_bug)
        dnames = _get_dept_names(source_bug, dept_review)
        dispute_reason, review_rec = _predict_dispute(source_bug, dnames, cause)
        ctype  = _classify_type(source_bug, dispute_reason, cause)
        primary_resp, secondary_resp, responsibility_label = _split_responsibility(dnames)
        use_mid, use_mname = _get_task_ref(source_bug)
        phenomenon = ctx["phenomenon"]
        severity_label = _severity_display(source_bug)
        demand = ctx["demand_display"]
        use_case = ctx["use_case_display"]
        is_bug = _determine_bug_status(review_rec)
        judgment_reason = _build_decision_reason(
            phenomenon=phenomenon,
            scope=ctx["scope"],
            cause=cause,
            dispute_reason=dispute_reason,
            review_rec=review_rec,
            demand=demand,
            use_case=use_case,
            resolution=source_bug.get("resolution", ""),
            primary_resp=primary_resp,
            secondary_resp=secondary_resp,
            task_title=ctx["task_title"],
            task_context=ctx["task_context"],
            demand_context=ctx["demand_context"],
            use_case_context=ctx["use_case_context"],
        )

        ext_review_rows.append({
            "id":                  bid,
            "link":                f"[{bid} {source_bug.get('title', '')}]({bug_url(bid)})",
            "severity_label":      severity_label,
            "phenomenon":          _one_line(phenomenon or "【空】", 80),
            "responsibility_label": responsibility_label,
        })

        ext_analyzed.append({
            "id":              bid,
            "title":           source_bug.get("title", ""),
            "link":            f"[{source_bug.get('title', '')}]({bug_url(bid)})",
            "phenomenon":      phenomenon or "【空】",
            "severity":        SEV_MAP.get(source_bug.get("severity", ""), source_bug.get("severity", "")),
            "severity_label":  severity_label,
            "is_bug":          is_bug,
            "primary_resp":    primary_resp,
            "secondary_resp":  secondary_resp,
            "responsibility_label": responsibility_label,
            "cause":           cause[:80] if not _is_cause_invalid(cause) else "【空】",
            "impact":          ctx["scope"],
            "cls_type":        ctype,
            "dispute_reason":  dispute_reason,
            "dispute_prediction": dispute_reason,
            "review_rec":      review_rec,
            "judgment":        _build_judgment(source_bug, dispute_reason, ctype, dnames, cause),
            "judgment_reason": judgment_reason,
            "task_id":         use_mid,
            "task_name":       use_mname,
            "resolution":      source_bug.get("resolution", ""),
            "demand":          demand,
            "use_case":        use_case,
            "is_dispute":      ctype == "争议",
        })

    # ── 内部Bug 逐条分析 ───────────────────────────────────────────────────────
    int_analyzed = []
    int_review_rows = []
    for b in int_review:
        ctx = _enrich_bug_context(client, b, bug_detail_cache, task_detail_cache, link_preview_cache)
        source_bug = ctx["source_bug"]
        bid    = str(source_bug.get("id", ""))
        cause  = _get_cause(source_bug)
        dnames = _get_dept_names(source_bug, dept_review)
        dispute_reason, review_rec = _predict_dispute(source_bug, dnames, cause)
        ctype  = _classify_type(source_bug, dispute_reason, cause)
        use_mid, use_mname = _get_task_ref(source_bug)
        primary_resp, secondary_resp, responsibility_label = _split_responsibility(dnames)
        phenomenon = ctx["phenomenon"]
        severity_label = _severity_display(source_bug)
        demand = ctx["demand_display"]
        use_case = ctx["use_case_display"]
        is_bug = _determine_bug_status(review_rec)
        judgment_reason = _build_decision_reason(
            phenomenon=phenomenon,
            scope="",
            cause=cause,
            dispute_reason=dispute_reason,
            review_rec=review_rec,
            demand=demand,
            use_case=use_case,
            resolution=source_bug.get("resolution", ""),
            primary_resp=primary_resp,
            secondary_resp=secondary_resp,
            task_title=ctx["task_title"],
            task_context=ctx["task_context"],
            demand_context=ctx["demand_context"],
            use_case_context=ctx["use_case_context"],
        )

        int_review_rows.append({
            "id":                   bid,
            "link":                 f"[{bid} {source_bug.get('title', '')}]({bug_url(bid)})",
            "severity_label":       severity_label,
            "is_typical":           "是" if source_bug.get("isTypical") == "1" else "否",
            "responsibility_label": responsibility_label,
        })

        int_analyzed.append({
            "id":              bid,
            "title":           source_bug.get("title", ""),
            "link":            f"[{source_bug.get('title', '')}]({bug_url(bid)})",
            "phenomenon":      phenomenon or "【空】",
            "severity":        SEV_MAP.get(source_bug.get("severity", ""), source_bug.get("severity", "")),
            "severity_label":  severity_label,
            "is_bug":          is_bug,
            "is_typical":      source_bug.get("isTypical") == "1",
            "primary_resp":    primary_resp,
            "secondary_resp":  secondary_resp,
            "responsibility_label": responsibility_label,
            "cause":           cause[:80] if not _is_cause_invalid(cause) else "【空】",
            "cls_type":        ctype,
            "dispute_reason":  dispute_reason,
            "dispute_prediction": dispute_reason,
            "review_rec":      review_rec,
            "judgment":        _build_judgment(source_bug, dispute_reason, ctype, dnames, cause),
            "judgment_reason": judgment_reason,
            "task_id":         use_mid,
            "task_name":       use_mname,
            "demand":          demand,
            "use_case":        use_case,
            "is_dispute":      ctype == "争议",
        })

    # ── 低质量任务识别 ─────────────────────────────────────────────────────────
    # 构建 task_map，顺便把用于根因分析的字段带进去
    task_map: dict = {}
    for b in non_perf:
        use_mid, use_mname = _get_task_ref(b)
        if not use_mid:
            continue
        cause = _get_cause(b)
        dnames = _get_dept_names(b, dept_review)
        dispute_reason, _ = _predict_dispute(b, dnames, cause)
        if use_mid not in task_map:
            task_map[use_mid] = {"name": use_mname, "bugs": []}
        task_map[use_mid]["bugs"].append({
            "id":         str(b.get("id", "")),
            "title":      b.get("title", ""),
            "sev":        b.get("severity", ""),
            "isTypical":  b.get("isTypical", "0"),
            "btp":        b.get("bugTypeParent", ""),
            "cause":      cause,
            "has_dispute": bool(dispute_reason),
        })

    low_quality, watch_list = [], []
    for tid, info in sorted(task_map.items(), key=lambda x: -len(x[1]["bugs"])):
        blist     = info["bugs"]
        total_cnt = len(blist)
        high_cnt  = sum(1 for bx in blist if bx["sev"] in ("1", "2") or bx["isTypical"] == "1")

        if total_cnt >= 5 or high_cnt >= 1:
            root_type, dims = _classify_lq_root_cause(blist, {})
            low_quality.append({
                "task_id":   tid,
                "task_name": info["name"],
                "total":     total_cnt,
                "high":      high_cnt,
                "root_type": root_type,
                "dims":      dims,
            })
        elif total_cnt >= 2:
            watch_list.append({
                "task_id":   tid,
                "task_name": info["name"],
                "total":     total_cnt,
            })

    # ── 摘要统计 ───────────────────────────────────────────────────────────────
    ext_dispute_cnt = sum(1 for b in ext_analyzed if b["cls_type"] == "争议")
    int_dispute_cnt = sum(1 for b in int_analyzed if b["cls_type"] == "争议")

    return {
        "vname":        vname,
        "vid":          version_id,
        "fetch_time":   fetch_time,
        "ext_perf":     ext_perf_list,
        "ext_nonbugs":  ext_perf_list,
        "ext_review_rows": ext_review_rows,
        "ext_analyzed": ext_analyzed,
        "int_review_rows": int_review_rows,
        "int_analyzed": int_analyzed,
        "low_quality":  low_quality,
        "watch_list":   watch_list,
        "dept_ext":     dept_ext,
        "dept_int":     dept_int,
        "summary": {
            "ext_review_cnt":   len(ext_analyzed),
            "int_review_cnt":   len(int_analyzed),
            "ext_dispute_cnt":  ext_dispute_cnt,
            "int_dispute_cnt":  int_dispute_cnt,
            "lq_task_cnt":      len(low_quality),
            "nonbug_cnt":       len(ext_perf_list),
        },
        "total_raw":   len(bugs),
        "non_bug_cnt": len(all_perf),
        "ext_total":   len(ext_perf) + len(ext_review),
        "int_total":   len(int_bugs),
        "int_other":   len([b for b in int_bugs
                            if b.get("severity") not in ("1", "2")
                            and b.get("isTypical") != "1"]),
    }


# ─── 报告保存 ─────────────────────────────────────────────────────────────────

def save_bug_review_report(content: str, vname: str) -> str:
    """保存 Claude 生成的 MD 报告到本地，返回保存路径。"""
    filename = re.sub(r'[\\/:*?"<>|]', '', vname).strip()
    path = get_report_path("Bug界定", f"{filename} 复盘预分类报告.md")
    path.write_text(content, encoding="utf-8")
    return str(path)
