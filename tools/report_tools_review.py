"""
tools/report_tools_review.py  v4

版本复盘报告工具层：拉取数据 + 调用 Claude API 生成 Bug 描述 + 输出标准三段式 Markdown。

ABC 三分类：
  A类 — 确定性数据，直接写入
  B类 — Claude 生成实质草稿，标注【待确认】供审阅
  C类 — 接口真正拿不到：延期 / 待办 / 复盘时间 / 内部Bug现象
"""

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import ACTIVE_PROJECTS, DEPT_MAP, to_display
from bsg_zentao.utils import get_report_path, make_review_filename
from tools.data_tools import get_version_requirements, get_version_bugs, get_version_history
from tools.calc_review import calc_ext_bugs, calc_int_bugs, calc_low_quality, calc_req_counts

log = logging.getLogger(__name__)

_MANUAL  = "【待补充·人工】"
_IFACE   = "【待补充·接口】"
_CONFIRM = "【待确认】"


# ══════════════════════════════════════════════════════════════════════════════
#  Claude API 生成 Bug 描述
# ══════════════════════════════════════════════════════════════════════════════

def _gen_bug_descriptions(all_bugs: list[dict]) -> dict[str, str]:
    """
    调用 Claude API 为每条复盘Bug生成一行直观的人话描述（H1 标题）。
    输入：外部+内部深度分析列表合并后的 all_bugs。
    返回：{bug_id: 描述字符串}。
    API 不可用时退回规则推断。
    """
    if not all_bugs:
        return {}

    # 规则推断（退回用）
    fallback: dict[str, str] = {}
    for b in all_bugs:
        tracing = b.get("tracing", "") or ""
        title   = b.get("title", "") or ""
        m = re.search(r'现象[：:]([^\n]+)', tracing)
        if m:
            fallback[b["id"]] = m.group(1).strip()[:30]
        else:
            clean = re.sub(r'【[^】]+】', '', title).strip()
            fallback[b["id"]] = clean[:25] if clean else title[:25]

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic()  # 自动读取 ANTHROPIC_API_KEY 环境变量

        items = []
        for b in all_bugs:
            tracing   = (b.get("tracing", "") or "").strip()[:300]
            title     = b.get("title", "") or ""
            causes    = [
                d["cause"] for d in b.get("depts", [])
                if d.get("cause") and d["cause"] != _IFACE
            ]
            cause_str = causes[0][:100] if causes else ""
            line = f"ID={b['id']} 标题={title}"
            if tracing:
                line += f" 溯源={tracing}"
            if cause_str:
                line += f" 原因={cause_str}"
            items.append(line)

        prompt = (
            "以下是若干Bug信息（标题/溯源/原因）。"
            "请为每个Bug生成一句中文描述（10-20字），"
            "要求：口语化、让非技术人员也能一眼看懂，描述核心现象或问题本质，不要用「Bug」这个词。"
            "只输出 JSON，格式：{\"bugID\": \"描述\", ...}\n\n"
            + "\n".join(items)
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if m:
            api_result = json.loads(m.group())
            return {**fallback, **{str(k): v for k, v in api_result.items()}}

    except Exception as e:
        log.warning("生成Bug描述 API 调用失败，使用规则推断：%s", e)

    return fallback


# ══════════════════════════════════════════════════════════════════════════════
#  版本识别
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_target_version(client, project_id: str, version: str) -> dict:
    today_str = date.today().isoformat()
    undone = client.fetch_versions(status="undone").get("executionStats", [])
    closed = client.fetch_versions(status="closed").get("executionStats", [])
    seen: set[str] = set()
    all_execs = []
    for e in undone + closed:
        eid = str(e.get("id", ""))
        if eid and eid not in seen:
            seen.add(eid)
            all_execs.append(e)
    INVALID = {"W5", "平台组"}
    valid = [
        e for e in all_execs
        if str(e.get("project", "")) == str(project_id)
        and e.get("name", "").strip() not in INVALID
        and re.search(r'（\d{4}）', e.get("name", ""))
        and e.get("end", "") not in ("", "0000-00-00")
    ]
    if version == "auto":
        past = [e for e in valid if e["end"] < today_str]
        if not past:
            raise RuntimeError("未找到已交付版本，请手动指定 version 参数。")
        target = max(past, key=lambda e: int(e["id"]))
    else:
        candidates = [e for e in valid if str(e["id"]) == str(version)]
        if not candidates:
            raise RuntimeError(f"未找到版本 ID={version}。")
        target = candidates[0]
    return {"id": str(target["id"]), "name": target.get("name", "").strip(),
            "begin": target.get("begin", ""), "end": target.get("end", "")}


# ══════════════════════════════════════════════════════════════════════════════
#  文本处理工具
# ══════════════════════════════════════════════════════════════════════════════

def _extract_phenomenon(tracing: str) -> str:
    """从 tracingBack 提取Bug现象（外部Bug专用）。"""
    if not tracing or tracing == _IFACE:
        return _MANUAL
    m = re.search(r'现象[：:]([^\n]+)', tracing)
    if m:
        return m.group(1).strip()
    lines = [
        l.strip() for l in tracing.split('\n')
        if l.strip() and not l.strip().startswith('http') and len(l.strip()) > 5
    ]
    return lines[0][:60] if lines else _MANUAL


def _severity_label(bug: dict, with_scope: bool = False) -> str:
    LABEL = {"1": "🔴极严重", "2": "🔴高等缺陷", "3": "🟡中等缺陷", "4": "🟢低等缺陷"}
    SCOPE = {"1": "严重影响使用", "2": "影响核心功能", "3": "影响不大", "4": "影响极小"}
    sev   = str(bug.get("severity") or "")
    label = LABEL.get(sev, f"severity={sev}")
    if with_scope and sev in SCOPE:
        return f"{label} {SCOPE[sev]}"
    return label


# ══════════════════════════════════════════════════════════════════════════════
#  B类分析生成函数
# ══════════════════════════════════════════════════════════════════════════════

def _gen_1_6_mgmt_issues(ext: dict) -> list[dict]:
    rows = []
    seen_bugs = set()
    for b in ext["deep_analysis"]:
        bid = b["id"]
        if bid in seen_bugs:
            continue
        seen_bugs.add(bid)
        for d in b["depts"]:
            cause = d["cause"]
            step  = d["step"]
            dept  = d["name"]
            if cause == _IFACE or not cause:
                issue = f"原因待填写（{_CONFIRM}）"
            elif any(k in cause for k in ["规范", "约束", "标准"]):
                issue = "历史规范缺失，遗留问题未清理"
            elif any(k in cause for k in ["需求", "产品", "历史"]):
                issue = "需求描述不完整，历史需求缺少场景约束"
            elif any(k in cause for k in ["认知", "用例", "测试"]):
                issue = "测试覆盖遗漏，历史遗留场景未纳入"
            elif any(k in cause for k in ["接口", "前后端", "格式", "null", "NULL", "数组"]):
                issue = "前后端接口规范未约定"
            else:
                # 清理多行后截断
                c_clean = re.sub(r'[\n\r]', ' ', cause).strip()
                issue = c_clean[:20] + ("…" if len(c_clean) > 20 else "")
            # 待办项清理多行
            step_clean = re.sub(r'[\n\r]', ' ', step).strip() if step != _IFACE else _MANUAL
            todo = step_clean[:25] + ("…" if len(step_clean) > 25 else step_clean) if step_clean != _MANUAL else _MANUAL
            rows.append({"issue": issue, "bug_ids": bid, "depts": dept, "todo": todo})
    return rows or [{"issue": _MANUAL, "bug_ids": _MANUAL, "depts": _MANUAL, "todo": _MANUAL}]


def _gen_2_3_high_types(int_: dict) -> list[dict]:
    """2.3 高缺陷Bug类型分析（B类）：同类型合并计数。"""
    type_depts: dict[str, list[str]] = defaultdict(list)

    for b in int_["deep_analysis"]:
        if b["severity_label"] not in ("🔴极严重", "🔴高等缺陷"):
            continue
        for d in b["depts"]:
            cause = d["cause"]
            dept  = d["name"]
            # 归类：同一 Bug 的争议方原因未填时，从标题关键词推断
            if cause == _IFACE or not cause:
                title = b["title"]
                if any(k in title for k in ["缺失", "功能", "不通", "异常", "未显示"]):
                    t = "需求描述缺失，功能点未在需求中提及"
                else:
                    t = "需求相关问题（原因待确认）"
            elif any(k in cause for k in ["理解", "了解不足", "偏差", "误"]):
                t = "需求理解偏差，核心数据逻辑实现错误"
            elif any(k in cause for k in ["需求", "未提及", "缺失", "没有", "历史"]):
                t = "需求描述缺失，功能点未在需求中提及"
            elif any(k in cause for k in ["规范", "约束"]):
                t = "历史规范缺失"
            else:
                c_clean = re.sub(r'[\n\r]', ' ', cause).strip()
                t = c_clean[:20]
            type_depts[t].append(dept)

    rows = []
    for t, depts in type_depts.items():
        cnt = len(depts)
        dept_cnt = Counter(depts)
        dept_str = "、".join(f"{d}（{c}）" for d, c in dept_cnt.most_common())
        rows.append({"type_desc": f"{t}（{cnt}条）", "depts": dept_str})
    return rows or [{"type_desc": _MANUAL, "depts": _MANUAL}]


def _gen_2_6_intolerable(int_: dict) -> list[dict]:
    STANDARDS = {
        "需求不明确":       "需求不明确，表述不清晰，没有把事情交代清楚，测试推进困难",
        "核心数据逻辑实现错误": "出错原因为需求理解错误，或核心逻辑未自测，很不应该",
        "历史遗留场景未覆盖":  "历史遗留场景未纳入测试范围，需补充用例库",
    }
    counts: dict[str, int] = {}
    for b in int_["deep_analysis"]:
        for d in b["depts"]:
            cause = d["cause"]
            if cause == _IFACE:
                t = "需求不明确"
            elif any(k in cause for k in ["理解", "了解不足", "偏差"]):
                t = "核心数据逻辑实现错误"
            elif any(k in cause for k in ["用例", "历史", "遗留"]):
                t = "历史遗留场景未覆盖"
            else:
                continue
            counts[t] = counts.get(t, 0) + 1

    rows = [{"type": t, "count": c, "standard": STANDARDS.get(t, _CONFIRM)}
            for t, c in counts.items()]
    return rows or [
        {"type": _MANUAL, "count": _MANUAL, "standard": _MANUAL},
        {"type": _MANUAL, "count": _MANUAL, "standard": _MANUAL},
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  Markdown 生成
# ══════════════════════════════════════════════════════════════════════════════

def _generate_markdown(
    vname, ext, int_, low_quality, history,
    req_curr_ext, req_curr_int,
    bug_descriptions: dict[str, str],
) -> str:
    lines = []
    W  = lines.append
    NL = lambda: lines.append("")

    # ── 标题 ─────────────────────────────────────────────────────────────────
    W(f"# {vname}版本复盘")
    NL()
    W(f"**部门：** 效能组、测试组 **复盘时间：** {_MANUAL}")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════════════════
    #  一、外部Bug复盘
    # ══════════════════════════════════════════════════════════════════════
    W("## 一、外部Bug复盘")
    NL()

    # 1.1 外部Bug概览
    W(f"### 1.1 {vname} 外部Bug概览")
    NL()
    W("**外部Bug数量趋势：**")
    NL()
    W("<!-- 📊 折线图 -->")
    NL()
    W("| 版本 | Bug数量 |")
    W("| --- | --- |")
    for h in history:
        W(f"| {h['version_name']} | {h['ext_bug_review']} |")
    W(f"| {vname} | {ext['review_count']} |")
    NL()
    W(f"**当前版本Bug数：{ext['review_count']}**")
    NL()
    W(f"**外部Bug反馈总数：{ext['all_count']}**")
    NL()
    prev_review = history[-1]["ext_bug_review"] if history else None
    if prev_review is not None:
        trend = "明显降低" if ext["review_count"] < prev_review * 0.7 else \
                "有所降低" if ext["review_count"] < prev_review else \
                "基本持平" if ext["review_count"] == prev_review else "有所上升"
        conclusion = (f"{vname}版本外部Bug反馈数量{ext['all_count']}条，"
                      f"其中复盘Bug数量为{ext['review_count']}条，"
                      f"反馈数量和复盘数量较往期都有{trend}。")
    else:
        conclusion = (f"{vname}版本外部Bug反馈数量{ext['all_count']}条，"
                      f"其中复盘Bug数量为{ext['review_count']}条。")
    W(f"**结论：** {conclusion}")
    NL()
    W("---")
    NL()

    # 1.2 非Bug剔除列表（A类，excl_reason 已在 calc_review._excl_reason 严格清洗）
    W("### 1.2 外部Bug 非Bug剔除列表")
    NL()
    W("| 序号  | Bug标题 | 剔除原因 |")
    W("| --- | --- | --- |")
    for i, b in enumerate(ext["excl_list"], 1):
        W(f"| {i}   | {b['link']} | {b['excl_reason']} |")
    NL()
    W(f"**结论：** 当前版本外部共产生{ext['all_count']}条Bug，"
      f"其中有{ext['excl_count']}条属于优化、非Bug或配置问题，不再进行Bug复盘。")
    NL()
    W("---")
    NL()

    # 1.3 实际复盘Bug列表（Bug现象从tracingBack提取）
    W("### 1.3 外部Bug 实际复盘Bug列表")
    NL()
    W("| 序号  | Bug标题 | 缺陷等级/影响 | Bug现象 | 责任部门 |")
    W("| --- | --- | --- | --- | --- |")
    for i, b in enumerate(ext["review_list"], 1):
        da = next((d for d in ext["deep_analysis"] if d["id"] == b["id"]), None)
        phenomenon = _extract_phenomenon(da["tracing"]) if da else _MANUAL
        W(f"| {i}   | {b['link']} | {b['severity_label']} | {phenomenon} | {b['dept_str']} |")
    NL()
    dis_bugs  = [b for b in ext["review_list"] if b["is_dispute"]]
    dis_count = len(dis_bugs)
    no_dis    = ext["review_count"] - dis_count
    if dis_count:
        dis_ids = "、".join(b["id"] for b in dis_bugs)
        W(f"**结论：** 当前版本对{ext['review_count']}条Bug进行了Bug复盘，"
          f"其中{dis_count}条测试组存在争议（{dis_ids}），{no_dis}条无争议。")
    else:
        W(f"**结论：** 当前版本对{ext['review_count']}条Bug进行了Bug复盘，均无争议。")
    NL()
    W("---")
    NL()

    # 1.4 测试组趋势
    W(f"### 1.4 {vname} 外部Bug责任归属 — 测试组")
    NL()
    W("**测试组Bug数量趋势：**")
    NL()
    W("<!-- 📊 折线图 -->")
    NL()
    W("| 版本            | Bug数量 |")
    W("| ------------- | ----- |")
    for h in history:
        W(f"| {h['version_name']}  | {h['test_dept_bugs']}     |")
    W(f"| {vname} | {ext['test_dept_count']}     |")
    NL()
    test_ids_str = "、".join(ext["test_bug_ids"])
    prev_test = history[-1]["test_dept_bugs"] if history else None
    if ext["test_dept_count"]:
        test_trend = "较上个版本Bug数量有明显下降" if (prev_test and ext["test_dept_count"] < prev_test) else ""
        W(f"**结论：** {ext['review_count']}条Bug中有{ext['test_dept_count']}条和测试相关"
          f"（{test_ids_str}），{test_trend}。")
    else:
        W(f"**结论：** 本版本{ext['review_count']}条复盘Bug中无测试组责任Bug。")
    NL()
    W("---")
    NL()

    # 1.5 其他部门分布
    W(f"### 1.5 {vname} 外部Bug责任归属 — 其它部门")
    NL()
    W("**其它部门Bug数量分布：**")
    NL()
    W("<!-- 📊 柱状图 -->")
    NL()
    W("| 部门 | Bug数量 |")
    W("| --- | --- |")
    for dept, cnt in ext["other_dept_dist"].items():
        W(f"| {dept} | {cnt} |")
    NL()
    if ext["other_dept_dist"]:
        dept_list = "、".join(f"{d}{c}条" for d, c in ext["other_dept_dist"].items())
        W(f"**结论：** 本版本各部门外部Bug数量均较少（{dept_list}），分布较为分散。")
    else:
        W(f"**结论：** 本版本无其他部门外部Bug。")
    NL()
    W("---")
    NL()

    # 外部Bug深度分析（H1标题由 Claude API 生成）
    for i, b in enumerate(ext["deep_analysis"], 1):
        W(f"### 深度分析（{i}）")
        NL()
        # H1 描述行（A类：Claude API 生成）
        desc = bug_descriptions.get(b["id"], "")
        if desc:
            W(f"# {desc}")
            NL()
        W(f"**Bug标题：** {b['link']}")
        phenomenon = _extract_phenomenon(b["tracing"])
        W(f"**Bug现象：** {phenomenon}")
        W(f"**缺陷等级：** {b['severity_label']}")
        W(f"**溯源：** {b['tracing']}")
        NL()
        for d in b["depts"]:
            if d["is_dispute"]:
                W(f"**{d['name']} · 争议**")
                NL()
                W(f"- 争议：{d['cause']}")
                W(f"- 举措：{d['step']}")
            else:
                W(f"**{d['name']}**")
                NL()
                W(f"- 原因：{d['cause']}")
                W(f"- 举措：{d['step']}")
            NL()
        W("---")
        NL()

    # 1.6 核心管理问题（B类）
    W("### 1.6 外部Bug复盘总结 核心管理问题")
    NL()
    W("| 序号  | 管理问题类别 | 涉及Bug | 涉及部门 | 待办项 |")
    W("| --- | --------------------- | ---------- | ------ | ---------------- |")
    for i, row in enumerate(_gen_1_6_mgmt_issues(ext), 1):
        W(f"| {i}   | {row['issue']} | {row['bug_ids']} | {row['depts']} | {row['todo']} |")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════════════════
    #  二、内部Bug复盘
    # ══════════════════════════════════════════════════════════════════════
    W("## 二、内部Bug复盘")
    NL()

    # 2.1 内部Bug概览
    W(f"### 2.1 {vname} 内部Bug概览")
    NL()
    W("**内部Bug数量趋势：**")
    NL()
    W("<!-- 📊 折线图，按版本展示 -->")
    NL()
    W("| 版本 | 内部Bug总数 |")
    W("| --- | --- |")
    for h in history:
        W(f"| {h['version_name']} | {h['int_bugs']} |")
    W(f"| {vname} | {int_['total_count']} |")
    NL()
    W(f"**当前版本内部Bug总数：{int_['total_count']}**")
    NL()
    prev_int = history[-1]["int_bugs"] if history else None
    if prev_int and int_["total_count"] < prev_int:
        diff = prev_int - int_["total_count"]
        pct  = int(diff / prev_int * 100)
        W(f"**内部Bug趋势概况：** 本版本内部Bug延续下降趋势，较上版本减少{diff}条（降幅{pct}%）。")
    else:
        W(f"**内部Bug趋势概况：** 本版本内部Bug总数{int_['total_count']}条。{_CONFIRM}")
    NL()
    W("---")
    NL()

    # 2.2 重要缺陷分布
    W("### 2.2 内部Bug 重要缺陷分布")
    NL()
    W("**极严重Bug数量：**")
    NL()
    W("<!-- 📊 柱状图 -->")
    NL()
    W("| 部门 | Bug数量 |")
    W("| --- | --- |")
    if int_["extreme_dept_dist"]:
        for dept, cnt in int_["extreme_dept_dist"].items():
            W(f"| {dept} | {cnt} |")
    else:
        W("| 本版本无极严重Bug | 0 |")
    NL()
    W("**高严重Bug数量：**")
    NL()
    W("<!-- 📊 柱状图 -->")
    NL()
    W("| 部门 | Bug数量 |")
    W("| --- | --- |")
    if int_["high_dept_dist"]:
        for dept, cnt in int_["high_dept_dist"].items():
            W(f"| {dept} | {cnt} |")
    else:
        W("| 本版本无高等缺陷Bug | 0 |")
    NL()
    ec  = int_["extreme_count"]
    hc  = int_["high_count"]
    h_ids = "、".join(b["id"] for b in int_["review_list"] if b["severity"] == "2")
    high_dept_desc = "、".join(f"{d}占{c}条" for d, c in int_["high_dept_dist"].items())
    high_bugs_dispute = [b for b in int_["review_list"] if b["is_dispute"] and b["severity"] == "2"]
    dispute_note = f"其中{len(high_bugs_dispute)}条产品对责任定义有争议" if high_bugs_dispute else ""
    W(f"**重要缺陷分布结论：** 当前版本重要缺陷共有{ec + hc}条：")
    NL()
    W(f"1. 极严重Bug {ec}条，{'本版本无极严重Bug' if ec == 0 else '涉及（' + '、'.join(b['id'] for b in int_['review_list'] if b['severity'] == '1') + '）'}")
    if hc:
        dispute_str = f"；{dispute_note}" if dispute_note else ""
        W(f"2. 高等缺陷Bug共{hc}条（{h_ids}）；{high_dept_desc}{dispute_str}")
    else:
        W(f"2. 高等缺陷Bug 0条，本版本无高等缺陷Bug")
    NL()
    W("---")
    NL()

    # 2.3 高缺陷Bug类型分析（B类，合并同类）
    W("### 2.3 内部Bug 高缺陷Bug类型分析")
    NL()
    W("| 序号 | Bug类型 | 涉及部门 |")
    W("| --- | --- | --- |")
    for i, row in enumerate(_gen_2_3_high_types(int_), 1):
        W(f"| {i} | {row['type_desc']} | {row['depts']} |")
    NL()
    W("---")
    NL()

    # 2.4 复盘Bug列表（A类）
    W("### 2.4 复盘Bug列表（高缺陷Bug+典型Bug）")
    NL()
    W("| 序号  | Bug标题 | 缺陷等级 | 责任部门 |  归属类型  |")
    W("| --- | --- | --- | --- | :----: |")
    for i, b in enumerate(int_["review_list"], 1):
        tag = "**争议**" if b["is_dispute"] else "**确定**"
        W(f"| {i}   | {b['link']} | {b['severity_label']} | {b['dept_str']} | {tag} |")
    NL()
    W("---")
    NL()

    # 内部Bug深度分析（H1标题由 Claude API 生成）
    W("### 内部Bug 典型Bug复盘")
    NL()
    for i, b in enumerate(int_["deep_analysis"], 1):
        W(f"#### 深度分析（{i}）— {b['id']}")
        NL()
        # H1 描述行（A类：Claude API 生成）
        desc = bug_descriptions.get(b["id"], "")
        if desc:
            W(f"# {desc}")
            NL()
        W(f"**Bug标题：** {b['link']}")
        W(f"**Bug现象：** {_MANUAL}")  # 内部Bug无tracingBack，C类
        W(f"**缺陷等级：** {b['severity_label']}")
        NL()
        for d in b["depts"]:
            if d["is_dispute"]:
                W(f"**{d['name']} · 争议**")
                NL()
                W(f"- 争议：{d['cause']}")
                W(f"- 举措：{d['step']}")
            else:
                W(f"**{d['name']}**")
                NL()
                W(f"- 原因：{d['cause']}")
                W(f"- 举措：{d['step']}")
            NL()
        W("---")
        NL()

    # 2.5 低质量任务分析（主要部门从task_details推断）
    W("### 2.5 低质量任务分析")
    NL()
    W("| 任务名称 | Bug数 | 含极/高 | 主要部门 | 管理判断 |")
    W("| ----------------------- | ---- | ---- | ----- | -------------------------- |")
    if low_quality:
        for lq in low_quality:
            high_str = str(lq["high_extreme_count"]) if lq["high_extreme_count"] else "—"
            if lq["bug_count"] >= 20:
                judgment = f"{lq['judgment_prefix']}；本版本Bug数量最高，需重点关注提测质量"
            else:
                judgment = f"{lq['judgment_prefix']}；Bug较集中，建议提测前加强自测"
            W(f"| {lq['link']} | {lq['bug_count']}   | {high_str}    | {lq['main_dept']} | {judgment} |")
    else:
        W(f"| — | — | — | — | 本版本无Bug≥5的任务 |")
    NL()
    if low_quality:
        top = low_quality[0]
        high_note = f"，其中{top['high_extreme_count']}条为高等/极严重缺陷" if top["high_extreme_count"] else ""
        W(f"**结论：** 本版本Bug集中度最高的任务为{top['link']}，共{top['bug_count']}条Bug{high_note}。"
          f"建议复盘该任务的提测流程，加强功能自测和用例覆盖。")
    else:
        W(f"**结论：** 本版本各任务Bug数量均在合理范围内，无明显低质量任务。")
    NL()
    W("---")
    NL()

    # 2.6 测试组不可容忍Bug类型（B类）
    W("### 2.6 测试组不可容忍的Bug类型总结")
    NL()
    W("| Bug类型      | 本版本数量 | 判定标准               |")
    W("| ---------- | ----- | ------------------ |")
    for row in _gen_2_6_intolerable(int_):
        W(f"| {row['type']} | {row['count']} | {row['standard']} |")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════════════════
    #  三、版本复盘
    # ══════════════════════════════════════════════════════════════════════
    W("## 三、版本复盘")
    NL()

    # 3.1 版本需求趋势
    W("### 3.1 版本需求趋势")
    NL()
    W("<!-- 📊 折线图 -->")
    NL()
    W("| 版本 | 外部需求 | 内部需求 |")
    W("| --- | --- | --- |")
    for h in history:
        W(f"| {h['version_name']} | {h['ext_reqs']} | {h['int_reqs']} |")
    W(f"| {vname} | {req_curr_ext} | {req_curr_int} |")
    NL()
    prev_ext_reqs = history[-1]["ext_reqs"] if history else None
    prev_int_reqs = history[-1]["int_reqs"] if history else None
    if prev_ext_reqs:
        ext_trend = "有所降低" if req_curr_ext < prev_ext_reqs else "有所增加" if req_curr_ext > prev_ext_reqs else "基本持平"
        int_trend = "有所降低" if req_curr_int < prev_int_reqs else "有所增加" if req_curr_int > prev_int_reqs else "基本持平"
        W(f"**版本概况：** 本版本外部需求{req_curr_ext}项、内部需求{req_curr_int}项，"
          f"较上版本外部需求{ext_trend}、内部需求{int_trend}。{_MANUAL}")
    else:
        W(f"**版本概况：** 本版本外部需求{req_curr_ext}项、内部需求{req_curr_int}项。{_MANUAL}")
    NL()
    W("---")
    NL()

    # 3.2 / 3.3 / 3.4 — C类
    W("### 3.2 延期情况")
    NL()
    W("<!-- 📊 柱状图 -->")
    NL()
    W("**过程延期次数部门分布：**")
    NL()
    W("| 部门    | 延期次数 |")
    W("| ----- | ---- |")
    for _ in range(5):
        W(f"| {_MANUAL} | {_MANUAL} |")
    NL()
    W(f"**当前版本过程延期总数：** {_MANUAL}")
    NL()
    W(f"**延期情况：** {_MANUAL}")
    NL()
    W("---")
    NL()

    W("### 3.3 延期任务记录")
    NL()
    W("| 序号  | 延期任务标题 | 任务来源 | 延期次数 | 负责部门 | 任务起止时间 | 是否插单 | 需求是否明确 |")
    W("| --- | --------------------------------------------------------------------------------------------- | :--: | :--: | :---: | ------------- | :--: | :----: |")
    for _ in range(5):
        m = _MANUAL
        W(f"| {m} | {m} | {m} | {m} | {m} | {m} | {m} | {m} |")
    NL()
    W("---")
    NL()

    W("### 3.4 上周复盘待办项")
    NL()
    W("| 序号  | 待办 | 部门 | 截止时间 |")
    W("| --- | --- | --- | --- |")
    for _ in range(5):
        W(f"| {_MANUAL} | {_MANUAL} | {_MANUAL} | {_MANUAL} |")
    NL()
    W("---")
    NL()

    W("## THANK YOU")
    NL()
    W(f"复盘时间：{_MANUAL}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════════════════════

def assemble_review_report(client: ZentaoClient, project_id: str, version: str = "auto") -> dict:
    log.info("开始版本复盘（项目=%s，版本=%s）…", project_id, version)

    target = _resolve_target_version(client, project_id, version)
    vid, vname = target["id"], target["name"]
    log.info("  目标版本：%s（ID=%s）", vname, vid)

    log.info("  [1/4] 拉取Bug数据…")
    bug_result  = get_version_bugs(client, vid, project_id)

    log.info("  [2/4] 拉取需求池数据…")
    pool_result   = get_version_requirements(client, vid, project_id)
    pools         = [p for p in pool_result["pools"] if p.get("task_status") != "cancel"]
    task_details  = pool_result.get("task_details", {})
    php_member_map = pool_result.get("php_member_map", {})

    log.info("  [3/4] 拉取历史版本趋势数据…")
    history = get_version_history(client, vid, project_id, max_count=4)

    log.info("  [4/4] 计算各板块数据…")
    ext         = calc_ext_bugs(bug_result["bugs"], bug_result["dept_review"])
    int_        = calc_int_bugs(bug_result["bugs"], bug_result["dept_review"])
    low_quality = calc_low_quality(
        pools, bug_result["bugs"], bug_result["dept_review"],
        task_details=task_details,
        php_member_map=php_member_map,
    )
    req_counts  = calc_req_counts(pools)

    # 调用 Claude API 生成 Bug 描述（外部+内部合并）
    log.info("  生成Bug描述（调用 Claude API）…")
    all_da = ext["deep_analysis"] + int_["deep_analysis"]
    bug_descriptions = _gen_bug_descriptions(all_da)

    log.info("  生成 Markdown…")
    markdown = _generate_markdown(
        vname=vname, ext=ext, int_=int_, low_quality=low_quality,
        history=history,
        req_curr_ext=req_counts["ext_reqs"],
        req_curr_int=req_counts["int_reqs"],
        bug_descriptions=bug_descriptions,
    )

    fpath = get_report_path("版本复盘", make_review_filename(vname))
    fpath.write_text(markdown, encoding="utf-8")
    log.info("  已保存：%s", fpath)

    iface_cnt = markdown.count(_IFACE)

    return {
        "status":       "success",
        "version_name": vname,
        "saved_path":   str(fpath),
        "stats": {
            "ext_all":      ext["all_count"],
            "ext_review":   ext["review_count"],
            "int_total":    int_["total_count"],
            "high_extreme": int_["extreme_count"] + int_["high_count"],
        },
        "manual_required": [
            "各条内部Bug现象描述（深度分析 Bug现象字段）",
            "3.2 过程延期部门分布和总数",
            "3.3 延期任务记录（5行）",
            "3.4 上周复盘待办项（5行）",
            "复盘时间、版本概况补充说明",
        ],
        "hint": (
            f"⚠️ {iface_cnt}处标注【待补充·接口】，说明各部门尚未在禅道填写原因/举措，"
            "催促填写后可重新生成。" if iface_cnt else
            "✅ 禅道各部门填写数据已全部读取，报告已生成。"
        ),
    }


def save_review_report(content: str, version_name: str) -> str:
    filename = make_review_filename(version_name)
    path     = get_report_path("版本复盘", filename)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _project_name(project_id: str) -> str:
    for name, pid in ACTIVE_PROJECTS.items():
        if pid == project_id:
            return name
    return f"项目{project_id}"
