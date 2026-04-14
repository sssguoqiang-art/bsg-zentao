"""
tools/report_tools_review.py

版本复盘报告工具层：拉取数据 + 直接生成符合 Gamma 模板的 Markdown 文件。

设计变更（v2）：
  不再返回数据包给 Claude 自由发挥，而是在工具内直接生成标准 Markdown，
  确保格式与 OB/Gamma 模板完全一致。

对外暴露：
  assemble_review_report(client, project_id, version)
    → 生成报告、保存文件、返回摘要给 Claude Code
  save_review_report(content, version_name)
    → 保存 Claude 手动生成的内容（兼容旧调用）
"""

import logging
import re
from datetime import date
from pathlib import Path

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import ACTIVE_PROJECTS, DEPT_MAP, to_display
from bsg_zentao.utils import get_report_path, make_review_filename
from tools.data_tools import (
    get_version_requirements,
    get_version_bugs,
    get_version_history,
)
from tools.calc_review import (
    calc_ext_bugs,
    calc_int_bugs,
    calc_low_quality,
    calc_req_counts,
)

log = logging.getLogger(__name__)

_MANUAL = "【待补充·人工】"
_IFACE  = "【待补充·接口】"
_CONFIRM = "【待确认】"


# ─── 目标版本识别 ──────────────────────────────────────────────────────────────

def _resolve_target_version(client: ZentaoClient, project_id: str, version: str) -> dict:
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

    INVALID_NAMES = {"W5", "平台组"}
    valid = [
        e for e in all_execs
        if str(e.get("project", "")) == str(project_id)
        and e.get("name", "").strip() not in INVALID_NAMES
        and re.search(r'（\d{4}）', e.get("name", ""))
        and e.get("end", "") not in ("", "0000-00-00")
    ]

    if version == "auto":
        past = [e for e in valid if e["end"] < today_str]
        if not past:
            raise RuntimeError("未找到已交付版本（end < today），请手动指定 version 参数。")
        target = max(past, key=lambda e: int(e["id"]))
    else:
        candidates = [e for e in valid if str(e["id"]) == str(version)]
        if not candidates:
            raise RuntimeError(f"未找到版本 ID={version}。")
        target = candidates[0]

    return {
        "id":    str(target["id"]),
        "name":  target.get("name", "").strip(),
        "begin": target.get("begin", ""),
        "end":   target.get("end", ""),
    }


# ─── Markdown 生成 ─────────────────────────────────────────────────────────────

def _generate_markdown(
    vname: str,
    ext: dict,
    int_: dict,
    low_quality: list[dict],
    history: list[dict],
    req_curr_ext: int,
    req_curr_int: int,
) -> str:
    """根据计算结果生成完整的三段式复盘 Markdown。"""

    lines = []
    W  = lines.append
    NL = lambda: lines.append("")

    # ══════════════════════════════════════════════════════════
    #  标题
    # ══════════════════════════════════════════════════════════
    W(f"# {vname}版本复盘")
    NL()
    W(f"**部门：** 效能组、测试组 **复盘时间：** {_MANUAL}")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════
    #  一、外部Bug复盘
    # ══════════════════════════════════════════════════════════
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
    # 结论（B类草稿）
    trend = "较往期有明显降低" if history and ext['review_count'] < history[-1]['ext_bug_review'] else "与上版本基本持平"
    W(f"**结论：** {vname}版本外部Bug反馈数量{ext['all_count']}条，其中复盘Bug数量为{ext['review_count']}条，{trend}。{_CONFIRM}")
    NL()
    W("---")
    NL()

    # 1.2 非Bug剔除列表
    W("### 1.2 外部Bug 非Bug剔除列表")
    NL()
    W("| 序号  | Bug标题 | 剔除原因 |")
    W("| --- | --- | --- |")
    for i, b in enumerate(ext["excl_list"], 1):
        W(f"| {i}   | {b['link']} | {b['excl_reason']} |")
    NL()
    W(f"**结论：** 当前版本外部共产生{ext['all_count']}条Bug，其中有{ext['excl_count']}条属于优化、非Bug或配置问题，不再进行Bug复盘。")
    NL()
    W("---")
    NL()

    # 1.3 实际复盘Bug列表
    W("### 1.3 外部Bug 实际复盘Bug列表")
    NL()
    W("| 序号  | Bug标题 | 缺陷等级/影响 | Bug现象 | 责任部门 |")
    W("| --- | --- | --- | --- | --- |")
    for i, b in enumerate(ext["review_list"], 1):
        W(f"| {i}   | {b['link']} | {b['severity_label']} | {_MANUAL} | {b['dept_str']} |")
    NL()
    dis_bugs  = [b for b in ext["review_list"] if b["is_dispute"]]
    dis_count = len(dis_bugs)
    no_dis    = ext["review_count"] - dis_count
    if dis_count:
        dis_ids = "、".join(b["id"] for b in dis_bugs)
        W(f"**结论：** 当前版本对{ext['review_count']}条Bug进行了Bug复盘，其中{dis_count}条测试组存在争议（{dis_ids}），{no_dis}条无争议。")
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
    test_trend = "较上版本有所下降" if prev_test is not None and ext["test_dept_count"] < prev_test else "与上版本持平"
    if ext["test_dept_count"]:
        W(f"**结论：** {ext['review_count']}条Bug中有{ext['test_dept_count']}条和测试组相关（{test_ids_str}），{test_trend}。{_CONFIRM}")
    else:
        W(f"**结论：** 本版本复盘Bug中无测试组责任Bug。{_CONFIRM}")
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
    W(f"**结论：** {_CONFIRM}")
    NL()
    W("---")
    NL()

    # 外部Bug深度分析
    for i, b in enumerate(ext["deep_analysis"], 1):
        W(f"### 深度分析（{i}）")
        NL()
        W(f"**Bug标题：** {b['link']}")
        W(f"**Bug现象：** {_MANUAL}")
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

    # 1.6 核心管理问题（B类：依据深度分析归纳草稿）
    W("### 1.6 外部Bug复盘总结 核心管理问题")
    NL()
    W("| 序号  | 管理问题类别 | 涉及Bug | 涉及部门 | 待办项 |")
    W("| --- | --------------------- | ---------- | ------ | ---------------- |")
    # 自动归纳（B类草稿）
    mgmt_rows = _infer_mgmt_issues(ext)
    for i, row in enumerate(mgmt_rows, 1):
        W(f"| {i}   | {row['issue']} | {row['bug_ids']} | {row['depts']} | {row['todo']} |")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════
    #  二、内部Bug复盘
    # ══════════════════════════════════════════════════════════
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
        W(f"**内部Bug趋势概况：** 本版本内部Bug延续下降趋势，较上版本减少{diff}条（降幅{pct}%）。{_CONFIRM}")
    else:
        W(f"**内部Bug趋势概况：** {_CONFIRM}")
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
    ec = int_["extreme_count"]
    hc = int_["high_count"]
    e_ids = "、".join(b["id"] for b in int_["review_list"] if b["severity"] == "1")
    h_ids = "、".join(b["id"] for b in int_["review_list"] if b["severity"] == "2")
    W(f"**重要缺陷分布结论：** 当前版本重要缺陷共有{ec + hc}条：")
    NL()
    W(f"1. 极严重Bug {ec}条，{'涉及（' + e_ids + '）' if ec else '本版本无极严重Bug'}")
    W(f"2. 高等缺陷Bug共{hc}条{'（' + h_ids + '）；' + _CONFIRM if hc else '，本版本无高等缺陷Bug'}")
    NL()
    W("---")
    NL()

    # 2.3 高缺陷Bug类型分析（B类）
    W("### 2.3 内部Bug 高缺陷Bug类型分析")
    NL()
    W("| 序号 | Bug类型 | 涉及部门 |")
    W("| --- | --- | --- |")
    type_rows = _infer_high_bug_types(int_)
    for i, row in enumerate(type_rows, 1):
        W(f"| {i} | {row['type_desc']} | {row['depts']} |")
    NL()
    W("---")
    NL()

    # 2.4 复盘Bug列表
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

    # 内部Bug深度分析
    W("### 内部Bug 典型Bug复盘")
    NL()
    for i, b in enumerate(int_["deep_analysis"], 1):
        W(f"#### 深度分析（{i}）— {b['id']}")
        NL()
        W(f"**Bug标题：** {b['link']}")
        W(f"**Bug现象：** {_MANUAL}")
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

    # 2.5 低质量任务分析
    W("### 2.5 低质量任务分析")
    NL()
    W("| 任务名称 | Bug数 | 含极/高 | 主要部门 | 管理判断 |")
    W("| ----------------------- | ---- | ---- | ----- | -------------------------- |")
    if low_quality:
        for lq in low_quality:
            high_str = str(lq["high_extreme_count"]) if lq["high_extreme_count"] else "—"
            W(f"| {lq['link']} | {lq['bug_count']}   | {high_str}    | {_MANUAL} | {lq['judgment_prefix']}；{_CONFIRM} |")
    else:
        W(f"| — | — | — | — | 本版本无Bug≥5的任务 |")
    NL()
    W(f"**结论：** {_CONFIRM}")
    NL()
    W("---")
    NL()

    # 2.6 测试组不可容忍Bug类型（B类）
    W("### 2.6 测试组不可容忍的Bug类型总结")
    NL()
    W("| Bug类型 | 本版本数量 | 判定标准 |")
    W("| ---------- | ----- | ------------------ |")
    tol_rows = _infer_intolerable_types(int_)
    for row in tol_rows:
        W(f"| {row['type']} | {row['count']} | {row['standard']} |")
    NL()
    W("---")
    NL()

    # ══════════════════════════════════════════════════════════
    #  三、版本复盘
    # ══════════════════════════════════════════════════════════
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
    W(f"**版本概况：** {_MANUAL}")
    NL()
    W("---")
    NL()

    # 3.2 延期情况（C类）
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

    # 3.3 延期任务记录（C类）
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

    # 3.4 上周复盘待办项（C类）
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


# ─── B类辅助归纳函数 ────────────────────────────────────────────────────────────

def _infer_mgmt_issues(ext: dict) -> list[dict]:
    """依据深度分析数据归纳外部Bug核心管理问题（B类草稿）。"""
    rows = []
    for b in ext["deep_analysis"]:
        bug_id = b["id"]
        for d in b["depts"]:
            if not rows or rows[-1]["bug_ids"] != bug_id:
                rows.append({
                    "issue":   f"{d['name']}：{_infer_issue_type(d['cause'])}",
                    "bug_ids": bug_id,
                    "depts":   d["name"],
                    "todo":    d["step"][:20] + "…" if len(d["step"]) > 20 else d["step"],
                })
    # 若无法归纳，给空占位
    if not rows:
        rows = [{"issue": _MANUAL, "bug_ids": _MANUAL, "depts": _MANUAL, "todo": _MANUAL}]
    return rows[:6]  # 最多6行


def _infer_issue_type(cause: str) -> str:
    """从 causeAnalysis 简短推断问题类别。"""
    if cause == _IFACE or not cause:
        return _CONFIRM
    if any(k in cause for k in ["规范", "约束", "标准"]):
        return "历史规范缺失"
    if any(k in cause for k in ["需求", "产品"]):
        return "需求描述不完整"
    if any(k in cause for k in ["测试", "用例", "认知"]):
        return "测试覆盖遗漏"
    if any(k in cause for k in ["接口", "前后端", "格式"]):
        return "前后端接口规范未约定"
    return cause[:15] + "…" if len(cause) > 15 else cause


def _infer_high_bug_types(int_: dict) -> list[dict]:
    """依据高缺陷Bug的原因归纳类型（B类草稿）。"""
    from collections import defaultdict
    type_dept: dict[str, list] = defaultdict(list)

    for b in int_["deep_analysis"]:
        if b["severity_label"] not in ("🔴极严重", "🔴高等缺陷"):
            continue
        for d in b["depts"]:
            cause = d["cause"]
            t = _infer_issue_type(cause)
            key = t
            dept = d["name"]
            if dept not in type_dept[key]:
                type_dept[key].append(dept)

    rows = []
    for type_desc, depts in type_dept.items():
        cnt = len([b for b in int_["deep_analysis"]
                   if b["severity_label"] in ("🔴极严重", "🔴高等缺陷")])
        dept_str = "、".join(f"{d}（{depts.count(d)}）" for d in set(depts))
        rows.append({"type_desc": f"{type_desc}（{cnt}条）", "depts": dept_str})

    if not rows:
        rows = [{"type_desc": _MANUAL, "depts": _MANUAL}]
    return rows


def _infer_intolerable_types(int_: dict) -> list[dict]:
    """从高缺陷Bug原因归纳测试组不可容忍类型（B类草稿）。"""
    rows = []
    counts: dict[str, int] = {}
    for b in int_["deep_analysis"]:
        for d in b["depts"]:
            if d["id"] == "45":  # 测试组
                t = _infer_issue_type(d["cause"])
                counts[t] = counts.get(t, 0) + 1

    standards = {
        "需求描述不完整": "需求不明确，表述不清晰，测试推进困难",
        "历史规范缺失":   "历史遗留场景未纳入测试范围",
        "测试覆盖遗漏":   "测试用例未覆盖，漏测",
    }
    for t, cnt in counts.items():
        rows.append({
            "type":     t,
            "count":    cnt,
            "standard": standards.get(t, _CONFIRM),
        })

    if not rows:
        rows = [
            {"type": _MANUAL, "count": _MANUAL, "standard": _MANUAL},
            {"type": _MANUAL, "count": _MANUAL, "standard": _MANUAL},
        ]
    return rows


# ─── 主函数：组装 + 生成 + 保存 ───────────────────────────────────────────────

def assemble_review_report(
    client: ZentaoClient,
    project_id: str,
    version: str = "auto",
) -> dict:
    """
    完整版本复盘流程：拉数据 → 计算 → 生成 Markdown → 保存文件 → 返回摘要。

    返回给 Claude Code 的是摘要（不是数据包），Claude Code 只需告知用户：
      - 文件保存路径
      - 需要人工补充的内容清单（C类 + 未填写的B类）
    """
    log.info("开始版本复盘（项目=%s，版本=%s）…", project_id, version)

    # 1. 识别版本
    log.info("  [1/4] 识别目标版本…")
    target = _resolve_target_version(client, project_id, version)
    vid    = target["id"]
    vname  = target["name"]
    log.info("  目标版本：%s（ID=%s，%s ~ %s）", vname, vid, target["begin"], target["end"])

    # 2. 拉取当前版本数据
    log.info("  [2/4] 拉取Bug数据…")
    bug_result  = get_version_bugs(client, vid, project_id)
    bugs        = bug_result["bugs"]
    dept_review = bug_result["dept_review"]

    log.info("  [2/4] 拉取需求池数据…")
    pool_result = get_version_requirements(client, vid, project_id)
    pools       = [p for p in pool_result["pools"] if p.get("task_status") != "cancel"]

    # 3. 拉取历史趋势
    log.info("  [3/4] 拉取历史版本趋势数据…")
    history = get_version_history(client, vid, project_id, max_count=4)
    log.info("  已获取 %d 个历史版本", len(history))

    # 4. 计算各板块
    log.info("  [4/4] 计算各板块数据…")
    ext         = calc_ext_bugs(bugs, dept_review)
    int_        = calc_int_bugs(bugs, dept_review)
    low_quality = calc_low_quality(pools, bugs, dept_review)
    req_counts  = calc_req_counts(pools)

    # 5. 生成 Markdown
    log.info("  生成 Markdown…")
    markdown = _generate_markdown(
        vname        = vname,
        ext          = ext,
        int_         = int_,
        low_quality  = low_quality,
        history      = history,
        req_curr_ext = req_counts["ext_reqs"],
        req_curr_int = req_counts["int_reqs"],
    )

    # 6. 保存文件
    filename = make_review_filename(vname)
    fpath    = get_report_path("版本复盘", filename)
    fpath.write_text(markdown, encoding="utf-8")
    log.info("  已保存：%s", fpath)

    # 7. 返回摘要
    iface_fields = sum(
        1 for b in ext["deep_analysis"] + int_["deep_analysis"]
        for d in b["depts"]
        if "【待补充·接口】" in d["cause"] or "【待补充·接口】" in d["step"]
    )

    return {
        "status":       "success",
        "version_name": vname,
        "saved_path":   str(fpath),
        "stats": {
            "ext_all":     ext["all_count"],
            "ext_review":  ext["review_count"],
            "int_total":   int_["total_count"],
            "high_extreme": int_["extreme_count"] + int_["high_count"],
        },
        "manual_required": [
            "各条复盘Bug的Bug现象描述（1.3 / 2.4）",
            "外部Bug核心管理问题总结（1.6，已生成草稿，需确认）",
            "3.2 过程延期部门分布和总数",
            "3.3 延期任务记录（5行）",
            "3.4 上周复盘待办项（5行）",
            "复盘时间",
        ],
        "iface_pending": iface_fields,
        "hint": (
            f"⚠️ 有 {iface_fields} 个字段标注【待补充·接口】，"
            "说明各部门尚未在禅道填写原因/举措，复盘会前需催促填写完毕后可重新生成。"
            if iface_fields else
            "✅ 各部门禅道填写数据已全部读取。"
        ),
    }


# ─── 保存报告文件（兼容旧调用）────────────────────────────────────────────────

def save_review_report(content: str, version_name: str) -> str:
    filename = make_review_filename(version_name)
    path     = get_report_path("版本复盘", filename)
    path.write_text(content, encoding="utf-8")
    log.info("版本复盘报告已保存：%s", path)
    return str(path)


# ─── 辅助 ─────────────────────────────────────────────────────────────────────

def _project_name(project_id: str) -> str:
    for name, pid in ACTIVE_PROJECTS.items():
        if pid == project_id:
            return name
    return f"项目{project_id}"
