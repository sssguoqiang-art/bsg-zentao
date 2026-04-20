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
from typing import Optional

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import DEPT_MAP
from bsg_zentao.utils import get_report_path

# ─── URL 生成 ─────────────────────────────────────────────────────────────────

def bug_url(bid: str) -> str:
    return f"https://cd.baa360.cc:20088/index.php?m=bug&f=view&bugID={bid}"

def task_url(tid: str) -> str:
    return f"https://cd.baa360.cc:20088/index.php?m=task&f=view&taskID={tid}"

# ─── 常量 ─────────────────────────────────────────────────────────────────────

SEV_MAP = {"1": "极", "2": "高", "3": "中", "4": "低"}

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
        return [DEPT_MAP.get(i.strip(), f"部门{i}") for i in raw.split(",") if i.strip()]
    bid = str(b.get("id", ""))
    dr_depts = (dept_review.get(bid) or {}).get("depts", [])
    if dr_depts:
        return [DEPT_MAP.get(str(d), f"部门{d}") for d in dr_depts]
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
    vname       = raw.get("versionName", f"版本{version_id}")
    fetch_time  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        cause = _get_cause(b)
        ext_perf_list.append({
            "id":         str(b.get("id", "")),
            "title":      b.get("title", ""),
            "phenomenon": _infer_phenomenon(b, cause),
            "resolution": b.get("resolution", ""),
            "cause":      cause[:80] if cause else "【空】",
        })

    # ── 外部Bug 逐条分析 ───────────────────────────────────────────────────────
    ext_analyzed = []
    for b in ext_review:
        bid    = str(b.get("id", ""))
        cause  = _get_cause(b)
        dnames = _get_dept_names(b, dept_review)
        dispute_reason, review_rec = _predict_dispute(b, dnames, cause)
        ctype  = _classify_type(b, dispute_reason, cause)
        btype  = BUG_TYPE_MAP.get(b.get("bugTypeParent", ""), "待定")
        test_r = _test_responsibility(b, dispute_reason, dnames)
        use_mid, use_mname = _get_task_ref(b)

        ext_analyzed.append({
            "id":              bid,
            "title":           b.get("title", ""),
            "phenomenon":      _infer_phenomenon(b, cause),
            "severity":        SEV_MAP.get(b.get("severity", ""), b.get("severity", "")),
            "bug_type":        btype,
            "test_resp":       test_r,
            "dept_names":      dnames,
            "cause":           cause[:80] if not _is_cause_invalid(cause) else "【空】",
            "impact":          _assess_impact(b, cause),
            "cls_type":        ctype,
            "dispute_reason":  dispute_reason,
            "review_rec":      review_rec,
            "judgment":        _build_judgment(b, dispute_reason, ctype, dnames, cause),
            "task_id":         use_mid,
            "task_name":       use_mname,
            "resolution":      b.get("resolution", ""),
        })

    # ── 内部Bug 逐条分析 ───────────────────────────────────────────────────────
    int_analyzed = []
    for b in int_review:
        bid    = str(b.get("id", ""))
        cause  = _get_cause(b)
        dnames = _get_dept_names(b, dept_review)
        dispute_reason, review_rec = _predict_dispute(b, dnames, cause)
        ctype  = _classify_type(b, dispute_reason, cause)
        btype  = BUG_TYPE_MAP.get(b.get("bugTypeParent", ""), "待定")
        use_mid, use_mname = _get_task_ref(b)

        int_analyzed.append({
            "id":              bid,
            "title":           b.get("title", ""),
            "phenomenon":      _infer_phenomenon(b, cause),
            "severity":        SEV_MAP.get(b.get("severity", ""), b.get("severity", "")),
            "is_typical":      b.get("isTypical") == "1",
            "bug_type":        btype,
            "dept_names":      dnames,
            "cause":           cause[:80] if not _is_cause_invalid(cause) else "【空】",
            "cls_type":        ctype,
            "dispute_reason":  dispute_reason,
            "review_rec":      review_rec,
            "judgment":        _build_judgment(b, dispute_reason, ctype, dnames, cause),
            "task_id":         use_mid,
            "task_name":       use_mname,
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
        "ext_analyzed": ext_analyzed,
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
