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
from datetime import datetime, timedelta
from html import unescape
from typing import Optional
from urllib.parse import parse_qs, urlparse

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


def _to_float(value) -> float:
    try:
        return float(str(value or "").strip() or 0)
    except Exception:
        return 0.0


def _parse_date(value: str):
    text = str(value or "").strip()
    if not text or text.startswith("0000-00-00"):
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        item = (item or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


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


def _extract_urls(text: str) -> list[str]:
    text = text or ""
    matches = re.findall(r"https?://[-A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%]+", text)
    urls = []
    for item in matches:
        cleaned = item.rstrip(".,;:，。；：）)]}")
        if cleaned:
            urls.append(cleaned)
    return urls


def _extract_first_url(text: str) -> str:
    urls = _extract_urls(text)
    return urls[0] if urls else ""


def _strip_urls(text: str) -> str:
    text = text or ""
    for url in _extract_urls(text):
        text = text.replace(url, " ")
    text = re.sub(r"\s+", " ", text).strip(" ，,;；：:\n\t")
    return text


def _rewrite_reference_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if "docs.google.com" not in host:
        return url

    if path.startswith("/document/d/"):
        parts = path.split("/")
        if len(parts) >= 4 and parts[3]:
            return f"https://docs.google.com/document/d/{parts[3]}/export?format=txt"

    if path.startswith("/spreadsheets/d/"):
        parts = path.split("/")
        if len(parts) >= 4 and parts[3]:
            query = parse_qs(parsed.query)
            fragment = parse_qs(parsed.fragment)
            gid = (query.get("gid") or fragment.get("gid") or [""])[0]
            export_url = f"https://docs.google.com/spreadsheets/d/{parts[3]}/export?format=tsv"
            if gid:
                export_url += f"&gid={gid}"
            return export_url

    return url


def _build_reference_display(raw: str, default_label: str, fallback_url: str = "") -> str:
    raw = (raw or "").strip()
    url = _extract_first_url(raw) or (fallback_url or "").strip()
    note = _strip_urls(raw)

    if url:
        label = default_label
        if default_label == "关联用例":
            if "无用例" in note and "说明文档" in note:
                note = "没有用例，依据说明文档测试"
                label = "说明文档"
            elif "说明文档" in note:
                label = "说明文档"
            elif "无用例" in note:
                note = "没有用例"
                label = "关联说明"
        elif default_label == "关联需求" and "无需求" in note:
            label = "关联任务"

        link = _format_markdown_link(label, url)
        if note:
            return f"{note}；{link}"
        return link

    return note or raw


def _describe_use_case_reference(raw_use_case: str) -> str:
    note = _strip_urls(raw_use_case)
    if not raw_use_case:
        return "无"
    if "无关联用例" in note:
        return "没有用例"
    if "无法溯源" in note:
        return "仅有文字记录，无可打开链接"
    if "无用例" in note and "说明文档" in note:
        return "没有用例，依据说明文档测试"
    if "无用例" in note:
        return "没有用例"
    return "已挂正式用例/说明链接"


def _describe_use_case_coverage(signal: str, raw_use_case: str) -> str:
    note = _strip_urls(raw_use_case)
    if not raw_use_case:
        return "无"
    if "无关联用例" in note:
        return "没有用例"
    if "无法溯源" in note:
        return "无法判断（没有可打开的正式用例）"
    if "无用例" in note and "说明文档" in note:
        return "没有用例，依据说明文档测试"
    if "无用例" in note:
        return "没有用例"

    mapping = {
        "covered": "已覆盖",
        "related": "未明显覆盖完整测试点",
        "not_found": "未覆盖",
        "unreadable": "无法判断",
        "missing": "无",
        "untraceable": "无法判断（没有可打开的正式用例）",
        "no_case": "没有用例",
    }
    return mapping.get(signal, "无法判断")


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


def _looks_like_noise_phenomenon(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text):
        return True
    if re.fullmatch(r"[\d\s]+", text):
        return True
    if any(kw in text for kw in ["哪套、哪个平台", "哪个环境", "fgfghre@gmail.com", "123456"]):
        return True
    return False


def _normalize_phenomenon_text(text: str) -> str:
    text = _strip_html(text or "")
    text = text.replace("问题描述：", "").replace("问题描述:", "")
    text = text.replace("功能位置：", "").replace("功能位置:", "")
    text = re.sub(r"\s+", " ", text).strip(" ：:;；，,")
    return _one_line(text, 80)


def _clean_step_issue_line(line: str) -> str:
    line = _strip_html(line or "")
    line = re.sub(r"^[0-9]+[.、:\s]+", "", line).strip()
    line = re.sub(r"\s+", " ", line).strip(" ：:;；，,")
    if not line:
        return ""
    if _looks_like_noise_phenomenon(line):
        return ""
    if any(kw in line for kw in ["哪套、哪个平台", "哪个环境", "交付环境", "前置条件", "设备：", "版本："]):
        return ""
    if line in {"交付", "环境", "前置条件", "[步骤]", "[结果]", "[期望]"}:
        return ""
    if line.startswith("正确应该"):
        return ""
    if line.startswith("原型中有"):
        return "缺少" + line.removeprefix("原型中有")
    if "原型中是" in line or "与原型不一致" in line:
        prefix = re.split(r"原型中是|与原型不一致", line)[0].strip(" ：:")
        if prefix:
            return f"{prefix}与原型不一致"
        return "页面展示与原型不一致"
    return line


def _title_context_for_phenomenon(title: str) -> str:
    clean = _clean_title_for_match(title)
    clean = re.sub(r"问题排查处理|数据有问题|页面问题|问题$", "", clean).strip(" ，,")
    clean = re.sub(r"(异常|错误)$", "", clean).strip(" ，,")
    clean = clean.replace("  ", " ").strip()
    return clean


def _rewrite_title_to_phenomenon(title: str) -> str:
    raw_title = title or ""
    clean = _clean_title_for_match(raw_title)
    if not clean:
        return ""

    tags = [tag.strip() for tag in re.findall(r"【([^】]+)】", raw_title) if tag.strip() and "Bug" not in tag]
    m = re.search(r"(.+?)等于1天，显示成days", clean)
    if m:
        prefix = m.group(1).strip(" ，,")
        if not prefix:
            prefix = tags[-1] if tags else ""
        clean = f"{prefix}配置为1天时显示成 days"
    elif clean.startswith("等于1天，显示成days"):
        prefix = tags[-1] if tags else ""
        clean = f"{prefix}配置为1天时显示成 days" if prefix else "配置为1天时显示成 days"
    clean = clean.replace("没配置", "未配置")
    clean = clean.replace("显示这个符号", "显示对应标识")
    clean = clean.replace("点击后显示空白", "点击后出现空白")
    clean = clean.replace("搜索出两个一样的结果", "搜索结果出现两个相同结果")

    if "页面问题" in clean:
        context = _title_context_for_phenomenon(clean)
        if context:
            return _one_line(f"{context}存在多个展示或交互异常", 80)
    if "数据有问题" in clean:
        context = _title_context_for_phenomenon(clean)
        if context:
            return _one_line(f"{context}存在数据异常", 80)
    return _one_line(clean, 80)


def _extract_phenomenon_from_steps(steps: str, title: str = "") -> str:
    text = _strip_html(steps)
    if not text:
        return ""

    body = re.split(r"\[结果\]|\[期望\]", text)[0]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    issues: list[str] = []
    context = ""

    for raw in lines:
        raw = raw.strip()
        if raw.startswith("功能位置：") or raw.startswith("功能位置:"):
            context = raw.split("：", 1)[-1].split(":", 1)[-1].strip()
            continue
        if raw.startswith("问题描述：") or raw.startswith("问题描述:"):
            issue = _clean_step_issue_line(raw.split("：", 1)[-1].split(":", 1)[-1])
            if issue:
                issues.append(issue)
            continue
        issue = _clean_step_issue_line(raw)
        if not issue:
            continue
        if any(issue == existing or issue in existing for existing in issues):
            continue
        issues.append(issue)

    issues = _dedupe_keep_order(issues)
    issue_markers = ["异常", "错误", "空白", "不一致", "对不上", "缺少", "未", "没有", "还是", "显示", "遮挡", "重复", "无法", "数据"]
    if len(issues) >= 2 and not any(marker in issues[0] for marker in issue_markers):
        issues = issues[1:]
    if not issues:
        return ""

    if len(issues) == 1:
        issue = issues[0]
        if context and context not in issue and not any(part and part in issue for part in re.split(r"[-/]", context) if len(part) >= 3):
            return _one_line(f"{context}中{issue}", 80)
        return _one_line(issue, 80)

    title_context = _title_context_for_phenomenon(title)
    prefix = title_context or context
    issue_text = "；".join(issues[:3])
    if prefix:
        return _one_line(f"{prefix}存在多个问题：{issue_text}", 80)
    return _one_line(issue_text, 80)


def _phenomenon_context_label(*texts: str) -> str:
    for raw in texts:
        text = _title_context_for_phenomenon(raw or "")
        text = re.sub(r"(存在多个问题|页面问题|数据有问题|问题排查处理|问题)$", "", text).strip(" ，,")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return ""


def _page_like_context(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if text.endswith(("页面", "页", "报表", "列表", "弹窗")):
        return text
    return f"{text}页面"


def _pick_primary_clause(clauses: list[str]) -> str:
    def score(text: str) -> int:
        scoring = [
            ("无法", 100),
            ("失败", 95),
            ("空白", 90),
            ("错误", 85),
            ("异常", 80),
            ("不一致", 78),
            ("对不上", 76),
            ("未回到顶部", 74),
            ("被遮挡", 72),
            ("缺少", 68),
            ("显示", 60),
        ]
        best = 0
        for kw, value in scoring:
            if kw in text:
                best = max(best, value)
        return best

    ordered = sorted(
        ((score(clause), idx, clause) for idx, clause in enumerate(clauses)),
        key=lambda item: (-item[0], item[1]),
    )
    return ordered[0][2] if ordered else ""


def _condense_phenomenon(text: str, title: str = "") -> str:
    text = _normalize_phenomenon_text(text)
    if not text:
        return ""

    text = text.replace("点击后出现空白游戏列表", "点击后为空白列表")
    text = text.replace("点击后显示空白游戏列表", "点击后为空白列表")
    text = text.replace("显示对应标识", "显示入口")
    text = text.replace("无法保存成功", "无法保存")
    text = text.replace("部分banner", "部分 banner")
    text = text.replace("banner被", "banner 被")
    text = text.replace("配置为1天时", "配置 1 天时")
    text = text.replace("显示成 days", "文案显示成 days")
    text = text.replace("活动配置页面", "活动配置页")
    text = text.replace("门票兑换免费旋转次数的打码倍数配置", "免费旋转打码倍数")
    text = re.sub(r"^.+?后，刷新(?:大厅)?(?:页面)?，未回到顶部，", "刷新页面后未回到顶部，", text)
    text = re.sub(
        r"^(.+?)的统计与(?:具体)?活动(?:记录)?统计的数据对不上.*$",
        r"\1与活动统计数据不一致",
        text,
    )
    text = re.sub(
        r"^免费旋转，未配置免费旋转次数也显示入口，点击后为空白列表$",
        "未配置免费旋转次数时仍显示免费旋转入口，点击后为空白列表",
        text,
    )

    prefix = ""
    issue_text = text
    if "存在多个问题：" in text:
        prefix, issue_text = text.split("存在多个问题：", 1)
    elif "存在多个问题:" in text:
        prefix, issue_text = text.split("存在多个问题:", 1)
    clauses = _dedupe_keep_order([part.strip(" ，,") for part in re.split(r"[；;]", issue_text) if part.strip(" ，,")])
    context = _phenomenon_context_label(prefix, title, text)

    if len(clauses) > 1:
        if any("发送成功人数" in clause for clause in clauses) and ("登录人数" in text or "登录人数" in title):
            return "发送记录中的发送成功人数和登录人数统计不一致"

        has_proto_mismatch = any("与原型不一致" in clause for clause in clauses)
        has_missing = any(clause.startswith("缺少") for clause in clauses)
        if has_proto_mismatch and has_missing:
            return _one_line(f"{_page_like_context(context)}字段缺失，展示与原型不一致", 40)
        if has_proto_mismatch and context:
            return _one_line(f"{_page_like_context(context)}展示与原型不一致", 40)

        if any(any(flag in clause for flag in ["对不上", "不一致", "统计是0", "统计进去了"]) for clause in clauses):
            if context and any(word in context for word in ["报表", "统计", "人数", "记录"]):
                if context.endswith("数据"):
                    return _one_line(f"{context}不一致", 40)
                return _one_line(f"{context}统计异常", 40)

        primary = _pick_primary_clause(clauses)
        if primary:
            if context and context not in primary and not primary.startswith(("刷新", "未配置", "配置", "点击")):
                return _one_line(f"{context}{primary}", 40)
            return _one_line(primary, 40)

    return _one_line(text, 40)


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
    raw = (url or "").strip()
    actual_url = _extract_first_url(raw) or raw
    actual_url = _rewrite_reference_url(actual_url)
    if not actual_url:
        return ""
    try:
        if "cd.baa360.cc:20088" in actual_url and client is not None:
            resp = client._session.get(actual_url, timeout=20, allow_redirects=True)
        else:
            resp = requests.get(actual_url, timeout=20, verify=False, allow_redirects=True)
        if resp.status_code in (401, 403):
            return "【链接需要权限，无法读取正文】"
        if resp.status_code != 200:
            return ""
        ctype = resp.headers.get("content-type", "")
        if (
            "text/html" not in ctype
            and "text/plain" not in ctype
            and "text/tab-separated-values" not in ctype
            and "text/csv" not in ctype
            and "application/vnd.ms-excel" not in ctype
        ):
            return ""
        text = resp.text
        if "text/plain" in ctype or "tab-separated-values" in ctype or "text/csv" in ctype:
            return _one_line(_strip_html(text), 600)
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            title = _one_line(_strip_html(m.group(1)), 80)
            if title:
                body = _one_line(_strip_html(text), 520)
                return f"{title} {body}".strip()
        return _one_line(_strip_html(text), 600)
    except Exception:
        return ""


def _severity_display(b: dict) -> str:
    return SEV_FULL_MAP.get(str(b.get("severity", "") or ""), str(b.get("severity", "") or ""))


def _resolve_phenomenon(b: dict, cause: str, steps: str = "") -> str:
    phe = (b.get("phenomenon") or "").strip()
    step_result = _extract_phenomenon_from_steps(steps, b.get("title", ""))
    if phe and not _looks_like_noise_phenomenon(phe):
        phe = _normalize_phenomenon_text(phe)
        if step_result and len(step_result) >= len(phe) + 6:
            return _condense_phenomenon(step_result, b.get("title", ""))
        return _condense_phenomenon(phe, b.get("title", ""))
    if step_result:
        return _condense_phenomenon(step_result, b.get("title", ""))
    step_result = _extract_result_from_steps(steps)
    if step_result and not _looks_like_noise_phenomenon(step_result):
        return _condense_phenomenon(step_result, b.get("title", ""))
    title_result = _rewrite_title_to_phenomenon(b.get("title", ""))
    if title_result:
        return _condense_phenomenon(title_result, b.get("title", ""))
    return _condense_phenomenon(_infer_phenomenon(b, cause), b.get("title", ""))


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


def _clean_title_for_match(title: str) -> str:
    title = re.sub(r"【[^】]*】", " ", title or "")
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _extract_focus_phrases(*texts: str) -> list[str]:
    stop_words = {
        "问题", "功能", "页面", "需求", "用例", "说明文档", "关联需求", "关联用例",
        "影响范围", "功能位置", "当前", "相关", "优化", "测试点", "说明",
    }
    phrases: list[str] = []
    seen: set[str] = set()

    for text in texts:
        cleaned = _strip_html(text or "")
        cleaned = _clean_title_for_match(cleaned)
        cleaned = re.sub(r"https?://[^\s]+", " ", cleaned)
        parts = re.split(r"[，。；：:\s、/|（）()【】\[\]\-_]+", cleaned)
        for part in parts:
            part = part.strip()
            if len(part) < 2 or len(part) > 24:
                continue
            if part in stop_words:
                continue
            if part not in seen:
                seen.add(part)
                phrases.append(part)
    return phrases


def _describe_bug_detail(phenomenon: str, scope: str) -> str:
    if not phenomenon or phenomenon == "【空】":
        return "Bug现象字段不完整，当前只能先按标题和上下文做初判。"
    if not scope:
        return "现象已能定位到具体问题点，但影响范围描述还不够完整，复盘时建议补一层业务影响。"
    if any(kw in scope for kw in ["玩家可感知", "用户可感知"]):
        return "该问题对用户侧可感知，虽然不一定阻断流程，但已经具备复盘价值。"
    if "不影响核心流程" in scope or "暂时不影响核心流程" in scope:
        return "该问题对用户侧可感知，但当前看起来还没有阻断核心流程。"
    if any(kw in scope for kw in ["核心流程", "严重影响", "无法使用"]):
        return "该问题已影响核心流程，优先按真实 Bug 视角处理。"
    if any(kw in scope for kw in ["后台", "展示", "核对", "影响较小", "局部"]):
        return "该问题更偏局部功能或展示核对异常，影响面相对可控。"
    return "从现象和影响范围看，这条问题已经具备进入界定的基础信息。"


def _assess_demand_alignment(
    *,
    phenomenon: str,
    bug_title: str,
    raw_demand: str,
    task_title: str,
    task_context: str,
    demand_context: str,
) -> tuple[str, str]:
    ref_text = " ".join(part for part in [task_title, task_context, demand_context] if part).strip()
    demand_note = _strip_urls(raw_demand)
    issue_phrases = _extract_focus_phrases(phenomenon, _clean_title_for_match(bug_title))
    hits = [p for p in issue_phrases if len(p) >= 3 and p in ref_text]

    if not raw_demand and not ref_text:
        return ("当前未挂关联需求或任务，暂无法判断产品是否明确到这个修改点。", "missing")

    if demand_note and any(flag in demand_note for flag in ["无需求", "无单子", "未有关联需求"]):
        if ref_text:
            return ("当前没有正式需求单，但能找到关联任务链路；从任务正文看，这个问题更像处理相关需求时暴露出的额外点，需警惕需求边界未写清。", "related")
        return ("当前没有正式需求单，无法直接判断产品是否明确写到这个修改点。", "missing")

    if not ref_text:
        return ("已挂需求链接，但当前没拿到可用于对照的需求正文，暂无法判断需求是否覆盖到这个修改点。", "missing")

    if len(hits) >= 2:
        joined = "、".join(hits[:3])
        return (f"需求正文里已能对上关键点（{joined}），说明产品侧对这个修改点有明确描述，更需要进一步确认是开发没做还是实现做错。", "explicit")
    if len(hits) == 1:
        return (f"需求链路与当前问题相关，且正文里提到了“{hits[0]}”，但还看不出是否把这个测试点写得足够细，需结合实现口径再判。", "partial")
    if task_title:
        return (f"当前能确认这条 Bug 属于《{_one_line(task_title, 24)}》这条需求链路，但从已抓到的需求正文里，还没看到直接写到这个修改点，更像需求相关但边界未明确。", "related")
    return ("已挂需求链接，但正文里没看到直接对应这个修改点的描述，需警惕产品漏写或边界未说明。", "related")


def _assess_use_case_coverage(
    *,
    phenomenon: str,
    bug_title: str,
    raw_use_case: str,
    use_case_context: str,
    task_title: str,
    demand_signal: str,
) -> tuple[str, str]:
    use_case_note = _strip_urls(raw_use_case)
    issue_phrases = _extract_focus_phrases(phenomenon, _clean_title_for_match(bug_title))
    demand_phrases = _extract_focus_phrases(task_title)
    issue_hits = [p for p in issue_phrases if len(p) >= 3 and p in use_case_context]
    demand_hits = [p for p in demand_phrases if len(p) >= 3 and p in use_case_context]

    if not raw_use_case:
        return ("当前没有关联用例，无法判断测试是否覆盖到这个测试点。", "missing")

    if "无关联用例" in use_case_note:
        return ("当前没有正式用例，无法判断测试是否覆盖到这个测试点。", "missing")

    if "无法溯源" in use_case_note:
        return ("字段里只有“无法溯源”这类文字记录，没有可点击的正式用例链接，因此当前无法点进去核对测试是否覆盖到这个测试点。", "untraceable")

    if "无用例" in use_case_note:
        if "说明文档" in use_case_note:
            return ("没有用例，依据说明文档测试；当前不能按正式用例覆盖来判断这个测试点是否被覆盖。", "no_case")
        return ("没有正式用例，当前无法按用例覆盖来判断这个测试点是否被测试到。", "no_case")

    if not use_case_context:
        return ("已挂关联用例链接，但当前无法读取正文内容，暂时无法判断用例有没有覆盖到这个测试点。", "unreadable")

    if "【链接需要权限，无法读取正文】" in use_case_context:
        return ("已挂关联用例链接，但该链接需要权限，当前没法读到用例正文，所以暂时无法判断是否覆盖到这个测试点。", "unreadable")

    if issue_hits:
        joined = "、".join(issue_hits[:3])
        return (f"用例正文里能直接对上测试点（{joined}），说明测试至少有覆盖到这一块；后续更应判断是执行漏测，还是结果校验没拦住。", "covered")

    if demand_hits or demand_signal in {"explicit", "partial", "related"}:
        hit_text = f"（已命中：{'、'.join(demand_hits[:2])}）" if demand_hits else ""
        return (f"用例与这条需求链路有关{hit_text}，但当前正文里还没看到直接覆盖这个问题测试点的描述，更像只覆盖了主流程，未明显覆盖到细分校验点。", "related")

    return ("当前拿到的用例正文里没看到和这个问题直接对应的测试点，倾向于未明显覆盖到该场景；若测试认为已覆盖，需要补充具体用例步骤佐证。", "not_found")


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


def _normalize_test_resp_label(primary_resp: str, secondary_resp: str, raw_test_resp: str) -> str:
    test_names = {"测试组", "测试部"}
    if primary_resp in test_names and not secondary_resp:
        return ""
    if secondary_resp:
        return "次责"
    if raw_test_resp == " + 测试次责":
        return "次责"
    if raw_test_resp == "；测试不担责":
        return "不担责"
    if raw_test_resp == "；测试责任待定":
        return "待定"
    return ""


def _compose_ext_responsibility_label(primary_resp: str, test_resp_label: str) -> str:
    base = primary_resp or "待确认"
    if not test_resp_label:
        return base
    if test_resp_label == "次责":
        return f"{base} + 测试次责"
    if test_resp_label == "不担责":
        return f"{base}（测试不担责）"
    if test_resp_label == "待定":
        return f"{base}（测试待定）"
    return f"{base}（测试责任：{test_resp_label}）"


def _determine_bug_status(review_rec: str) -> str:
    if review_rec == "复盘价值有限":
        return "待确认"
    return "是"


def _has_explicit_nonbug_marker(b: dict) -> bool:
    """
    仅认禅道里明确的“非Bug”标识。

    当前口径只看 bug.type == "performance"。
    resolution=tostory/external/bydesign 只能说明当前处理结果或争议状态，
    不能直接当成“非Bug标识”把数据放进外部非Bug列表。
    """
    bug_type = (b.get("type") or "").strip()
    return bug_type == "performance"


def _build_exclude_reason(b: dict, cause: str) -> str:
    exclusion = (b.get("exclusionReason") or b.get("exclusion_reason") or "").strip()
    if exclusion:
        return exclusion

    res = b.get("resolution", "")
    title = b.get("title", "")

    if res == "tostory":
        return "该问题已转为需求处理，按需求项跟踪，不纳入 Bug 复盘。"
    if res == "external":
        return "该问题当前判定为外部原因或环境因素，不纳入 Bug 复盘。"
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
    bug_title: str,
    phenomenon: str,
    scope: str,
    cause: str,
    dispute_reason: str,
    review_rec: str,
    demand: str,
    use_case: str,
    raw_demand: str = "",
    raw_use_case: str = "",
    resolution: str = "",
    primary_resp: str = "",
    secondary_resp: str = "",
    test_resp_label: str = "",
    task_title: str = "",
    task_context: str = "",
    demand_context: str = "",
    use_case_context: str = "",
) -> str:
    detail_note = _describe_bug_detail(phenomenon, scope)
    demand_note, demand_signal = _assess_demand_alignment(
        phenomenon=phenomenon,
        bug_title=bug_title,
        raw_demand=raw_demand,
        task_title=task_title,
        task_context=task_context,
        demand_context=demand_context,
    )
    use_case_note, use_case_signal = _assess_use_case_coverage(
        phenomenon=phenomenon,
        bug_title=bug_title,
        raw_use_case=raw_use_case,
        use_case_context=use_case_context,
        task_title=task_title,
        demand_signal=demand_signal,
    )

    owner_hint = "责任归属待确认。"
    if primary_resp:
        owner_hint = f"当前建议主责：{primary_resp}"
        if secondary_resp:
            owner_hint += f"；次责：{secondary_resp}"
        elif test_resp_label:
            owner_hint += f"；测试责任：{test_resp_label}"
        owner_hint += "。"

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
    elif demand_signal == "explicit":
        ai_judgment = f"原因记录指向“{cause_short}”，且需求对这个修改点写得比较明确，这条更像实现遗漏或实现错误，可按复盘 Bug 跟进。"
    elif use_case_signal == "covered":
        ai_judgment = f"原因记录指向“{cause_short}”，且用例已覆盖到该测试点，这条更需要追问测试执行和结果校验是否失守。"
    elif demand_signal in {"partial", "related"} and use_case_signal in {"related", "not_found", "missing", "unreadable", "no_case"}:
        ai_judgment = "需求链路能对上，但需求边界或用例覆盖都不够扎实，这条更适合在复盘里同时核需求明确性和测试覆盖性。"
    else:
        ai_judgment = f"原因记录指向“{cause_short}”，与当前现象能够对上，这条更像实际功能异常或实现遗漏，可按复盘 Bug 跟进。"

    lines = []
    lines.append("一、Bug详情判断：")
    detail_items = [
        f"现象：{_one_line(phenomenon or '【空】', 60)}",
        f"影响范围：{_one_line(_humanize_scope(scope) or '【空】', 40)}",
    ]
    if not _is_cause_invalid(cause):
        detail_items.append(f"原因记录：{_one_line(cause, 42)}")
    if resolution in {"tostory", "external", "bydesign"}:
        resolution_map = {
            "tostory": "当前处理结果：转需求",
            "external": "当前处理结果：外部原因",
            "bydesign": "当前处理结果：设计如此",
        }
        detail_items.append(resolution_map[resolution])
    detail_items.append(f"判断：{detail_note}")
    for idx, item in enumerate(detail_items, 1):
        lines.append(f"{idx}. {item}")
    lines.append("")

    lines.append("二、需求对照：")
    demand_items = []
    if task_title:
        demand_items.append(f"关联需求：{_one_line(task_title, 40)}")
    elif demand:
        demand_items.append("关联需求：已挂需求/任务链接")
    else:
        demand_items.append("关联需求：无")
    demand_items.append(f"判断：{demand_note}")
    for idx, item in enumerate(demand_items, 1):
        lines.append(f"{idx}. {item}")
    lines.append("")

    lines.append("三、用例对照：")
    use_case_items = []
    use_case_items.append(f"关联用例：{_describe_use_case_reference(raw_use_case)}")
    use_case_items.append(f"覆盖结论：{_describe_use_case_coverage(use_case_signal, raw_use_case)}")
    use_case_items.append(f"判断：{use_case_note}")
    for idx, item in enumerate(use_case_items, 1):
        lines.append(f"{idx}. {item}")
    lines.append("")

    lines.append("四、综合判断：")
    lines.append(f"1. AI判断：{ai_judgment}")
    lines.append(f"2. 归属建议：{owner_hint}")
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
    return title, _one_line(merged, 320)


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

    source = dict(bug)
    for key, value in detail.items():
        if value in (None, "", [], {}, "0"):
            continue
        source[key] = value

    raw_demand = (source.get("demand") or "").strip()
    raw_use_case = (source.get("useCase") or "").strip()
    demand_url = _extract_first_url(raw_demand)
    use_case_url = _extract_first_url(raw_use_case)
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
        demand_display = _build_reference_display(raw_demand, task_label or "关联需求", fallback_url=task_url(task_id) if task_id else "")
        if not task_context:
            if demand_url not in link_preview_cache:
                link_preview_cache[demand_url] = _fetch_link_preview(client, raw_demand)
            demand_context = link_preview_cache.get(demand_url, "")
    elif task_id:
        demand_display = _build_reference_display(raw_demand, task_label or "关联需求", fallback_url=task_url(task_id))
    elif raw_demand:
        demand_display = _build_reference_display(raw_demand, "关联需求")

    use_case_display = ""
    use_case_context = ""
    if raw_use_case:
        use_case_display = _build_reference_display(raw_use_case, "关联用例")
        if use_case_url:
            if use_case_url not in link_preview_cache:
                link_preview_cache[use_case_url] = _fetch_link_preview(client, raw_use_case)
            use_case_context = link_preview_cache.get(use_case_url, "")

    steps = (source.get("steps") or "").strip()

    return {
        "source_bug": source,
        "task_detail": task_detail,
        "task_title": task_title or task_name or "",
        "task_context": task_context,
        "demand_context": demand_context,
        "use_case_context": use_case_context,
        "raw_demand": raw_demand,
        "raw_use_case": raw_use_case,
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
    low_quality = report.get("low_quality", [])

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
            if item.get("test_resp_label"):
                resp += f"；测试责任：{item['test_resp_label']}"
            elif item['secondary_resp']:
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

    W("## 六、内部低质量任务预判")
    W("")
    W("> 说明：以下为 AI 结合关联 Bug 和任务详情做的任务层面预判，用于会前聚焦需求管理、测试排期、流程推进和管理动作问题。")
    W("")
    if low_quality:
        W("| 序号 | 任务 | 风险等级 | 关联Bug | 关键维度 |")
        W("| --- | --- | --- | --- | --- |")
        for i, item in enumerate(low_quality, 1):
            W(f"| {i} | {item['task_link']} | {item['risk_level']} | {item['bug_total']}（外部{item['ext_bug_cnt']} / 内部{item['int_bug_cnt']}） | {item['dimension_summary']} |")
        W("")
        for item in low_quality:
            W(f"### TASK#{item['task_id']} {item['task_link']}")
            W("")
            W(f"- 风险等级：{item['risk_level']}（{item['main_type']}）")
            W(f"- 触发维度：{item['dimension_summary']}")
            W(f"- AI预判：{item['conclusion']}")
            W("- 主要依据：")
            for idx, evidence in enumerate(item.get("evidences", [])[:5], 1):
                W(f"  {idx}. {evidence}")
            W("- 暴露问题：")
            for idx, point in enumerate(item.get("problem_points", [])[:5], 1):
                W(f"  {idx}. {point}")
            W("- 改进建议：")
            for idx, tip in enumerate(item.get("improvements", [])[:5], 1):
                W(f"  {idx}. {tip}")
            W("")
    else:
        W("暂无明显低质量任务信号。")
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


def _is_test_like_child(child: dict) -> bool:
    name = (child.get("name") or "").strip()
    ctype = str(child.get("type") or "").strip()
    if "【测试单】" in name or "【用例单】" in name or "测试用例" in name:
        return True
    if ctype == "discuss" and "测试" in name:
        return True
    return False


def _is_impl_like_child(child: dict) -> bool:
    if _is_test_like_child(child):
        return False
    name = (child.get("name") or "").strip()
    ctype = str(child.get("type") or "").strip()
    if any(tag in name for tag in ["【开发单】", "【制作单】", "【联调单】"]):
        return True
    return ctype in {"devel", "web", "study", "test"}


def _count_recent_task_changes(task_view: dict, deadline, window_days: int = 2) -> int:
    if not deadline:
        return 0
    start_dt = datetime.combine(deadline - timedelta(days=window_days), datetime.min.time())
    end_dt = datetime.combine(deadline + timedelta(days=1), datetime.max.time())
    action_types = {"edited", "assigned", "activated", "reopened", "finished", "closed", "commented"}
    total = 0
    for bucket in ("actions", "subActions"):
        for item in (task_view.get(bucket, {}) or {}).values():
            action = (item.get("action") or "").strip()
            if action not in action_types:
                continue
            raw_date = (item.get("date") or "").strip()
            if not raw_date:
                continue
            try:
                action_dt = datetime.strptime(raw_date[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if start_dt <= action_dt <= end_dt:
                total += 1
    return total


def _low_quality_main_type(dimensions: list[str]) -> str:
    dims = set(dimensions)
    if {"需求明确性风险", "流程/推进管理风险"} <= dims:
        return "综合型"
    if {"测试时间受压", "临近截止仍未收口"} & dims and "流程/推进管理风险" in dims:
        return "测试与推进混合风险"
    if "需求明确性风险" in dims:
        return "需求管理风险"
    if "测试时间受压" in dims or "临近截止仍未收口" in dims:
        return "测试与排期风险"
    if "流程/推进管理风险" in dims:
        return "流程管理风险"
    return "研发质量风险"


def _build_low_quality_problem_points(dimensions: list[str]) -> list[str]:
    dims = set(dimensions)
    points = []
    if "缺陷暴露面大" in dims or "缺陷暴露面偏大" in dims or "缺陷密度偏高" in dims:
        points.append("任务质量前置不够，问题不是零散漏点，而是成批暴露。")
    if "需求明确性风险" in dims:
        points.append("需求边界、异常场景或验收口径没有在前期收紧，产品与研发对修改点的理解不够一致。")
    if "临近截止仍未收口" in dims:
        points.append("截止日前任务仍未真正收口，排期把修复、回归和确认压到了末期。")
    if "测试时间受压" in dims:
        points.append("开发交付和测试验证贴得过近，返工会直接挤压测试窗口。")
    if "流程/推进管理风险" in dims:
        points.append("临近截止仍有较多编辑、激活或子任务变动，说明推进节奏和变更管理不稳。")
    if "协同复杂度高" in dims:
        points.append("任务协同面较大，但阶段性收口和单点负责人机制不够强。")
    if "排期消耗偏高" in dims or "延期/排期风险" in dims:
        points.append("排期估算和资源安排偏紧，容易把后续验证和交付缓冲吃掉。")
    return _dedupe_keep_order(points)


def _build_low_quality_improvements(dimensions: list[str]) -> list[str]:
    dims = set(dimensions)
    suggestions = []
    if "需求明确性风险" in dims:
        suggestions.append("需求评审时把边界、异常场景、验收口径写透，需求变更后同步更新测试点和回归范围。")
    if "缺陷暴露面大" in dims or "缺陷暴露面偏大" in dims or "缺陷密度偏高" in dims:
        suggestions.append("加强开发自测、联调走查和提测前冒烟，把批量问题前置拦截，不要等到测试末期集中暴露。")
    if "临近截止仍未收口" in dims or "测试时间受压" in dims:
        suggestions.append("把开发完成、自测完成和提测时间前移，至少给测试和回归预留完整窗口，不要把修复堆到截止日。")
    if "流程/推进管理风险" in dims:
        suggestions.append("对频繁变更、反复激活、子任务取消这类信号建立预警，按里程碑做阶段收口，而不是临门一脚再统一处理。")
    if "协同复杂度高" in dims:
        suggestions.append("多团队任务要拆清里程碑和单点责任人，减少多人并行导致的末期返工和信息错位。")
    if "排期消耗偏高" in dims or "延期/排期风险" in dims:
        suggestions.append("重新校准工时评估和资源投入，避免低估工作量把测试和上线缓冲全部吃掉。")
    return _dedupe_keep_order(suggestions)


def _compose_low_quality_conclusion(task_name: str, dimensions: list[str]) -> str:
    dims = set(dimensions)
    task_label = _one_line(task_name, 24) or "该任务"
    if "需求明确性风险" in dims and "流程/推进管理风险" in dims:
        return f"{task_label} 不是单点实现失误，更像需求管理、任务推进和质量收口同时失守导致的低质量任务。"
    if "测试时间受压" in dims and "临近截止仍未收口" in dims:
        return f"{task_label} 的核心问题在于收口过晚，修复和验证被一起挤到截止前，质量风险自然会放大。"
    if "流程/推进管理风险" in dims and "协同复杂度高" in dims:
        return f"{task_label} 的问题重点不在单个开发点，而在于协同复杂、末期调整频繁，管理收口没有兜住。"
    if "缺陷暴露面大" in dims or "缺陷暴露面偏大" in dims or "缺陷密度偏高" in dims:
        return f"{task_label} 的低质量特征主要体现在缺陷集中暴露，说明质量前置和联调拦截都不够。"
    return f"{task_label} 已出现明显任务级低质量信号，复盘时应把管理动作、排期和验证策略一起复盘。"


def _assess_low_quality_task(task_id: str, task_name: str, bugs: list[dict], task_view: dict, fetch_date) -> dict:
    task = task_view.get("task", {}) if isinstance(task_view, dict) else {}
    children_dict = (task.get("children") or {}) if isinstance(task, dict) else {}
    children = [c for c in children_dict.values() if c.get("deleted", "0") != "1"]

    total_bug = len(bugs)
    ext_bug_cnt = sum(1 for b in bugs if b.get("classification") in ("1", "2"))
    int_bug_cnt = sum(1 for b in bugs if b.get("classification") in ("4", "5"))
    high_bug_cnt = sum(1 for b in bugs if b.get("severity") in ("1", "2") or b.get("isTypical") == "1")
    req_bug_cnt = sum(
        1 for b in bugs
        if b.get("bugTypeParent") == "2"
        or any(kw in ((b.get("causeAnalysis") or "") + (b.get("tracingBack") or "") + (b.get("title") or "")) for kw in REQUIREMENT_CAUSE_KEYWORDS)
    )
    dispute_bug_cnt = sum(
        1 for b in bugs
        if b.get("resolution") in ("tostory", "external", "bydesign")
        or any(kw in ((b.get("causeAnalysis") or "") + (b.get("tracingBack") or "")) for kw in DISPUTE_KEYWORDS)
    )

    estimate = _to_float(task.get("estimate"))
    consumed = _to_float(task.get("consumed"))
    left = _to_float(task.get("left"))
    progress = int(_to_float(task.get("progress")))
    deadline = _parse_date(task.get("deadline"))
    latest_story_version = int(_to_float(task.get("latestStoryVersion")))

    impl_deadlines = sorted(
        d for c in children if _is_impl_like_child(c)
        for d in [_parse_date(c.get("deadline"))] if d
    )
    test_deadlines = sorted(
        d for c in children if _is_test_like_child(c)
        for d in [_parse_date(c.get("deadline"))] if d
    )
    latest_impl_deadline = impl_deadlines[-1] if impl_deadlines else None
    latest_test_deadline = test_deadlines[-1] if test_deadlines else None

    cancel_children = sum(1 for c in children if c.get("status") == "cancel")
    recent_change_cnt = _count_recent_task_changes(task_view, deadline, window_days=2)
    bug_density = (total_bug / estimate * 10) if estimate > 0 else 0

    dimensions: list[str] = []
    evidence: list[str] = []
    score = 0

    if total_bug >= 8:
        score += 3
        dimensions.append("缺陷暴露面大")
        evidence.append(f"关联 Bug {total_bug} 条（外部 {ext_bug_cnt} / 内部 {int_bug_cnt}）")
    elif total_bug >= 5:
        score += 2
        dimensions.append("缺陷暴露面偏大")
        evidence.append(f"关联 Bug {total_bug} 条（外部 {ext_bug_cnt} / 内部 {int_bug_cnt}）")
    elif total_bug >= 3:
        score += 1
        evidence.append(f"关联 Bug {total_bug} 条")

    if high_bug_cnt >= 1:
        score += 2
        dimensions.append("存在高等级/典型缺陷")
        evidence.append(f"高等级/典型 Bug {high_bug_cnt} 条")

    if total_bug >= 3 and bug_density >= 1.0:
        score += 1
        dimensions.append("缺陷密度偏高")
        evidence.append(f"每 10 人时约暴露 {bug_density:.1f} 条 Bug")

    if req_bug_cnt >= max(2, (total_bug + 1) // 2) or dispute_bug_cnt >= 2:
        score += 2
        dimensions.append("需求明确性风险")
        evidence.append(f"需求/争议类 Bug {max(req_bug_cnt, dispute_bug_cnt)} 条")
    elif req_bug_cnt >= 1 or str(task.get("demandReview")) in {"0", "2", "3"}:
        score += 1
        dimensions.append("需求明确性风险")
        evidence.append(f"demandReview={task.get('demandReview') or '空'}，latestStoryVersion={latest_story_version}")

    if task.get("is_delay") == "yes":
        score += 2
        dimensions.append("延期/排期风险")
        evidence.append("任务已被标记为延期")
    elif estimate > 0 and consumed >= estimate * 1.2:
        score += 1
        dimensions.append("排期消耗偏高")
        evidence.append(f"工时 {consumed:.1f}/{estimate:.1f}h")

    if deadline and deadline <= fetch_date and str(task.get("status") or "") in {"testing", "waittest", "doing", "pause"}:
        score += 1
        dimensions.append("临近截止仍未收口")
        evidence.append(f"截止日 {deadline} 时状态仍为 {task.get('status')}")
    elif deadline and left > 0 and deadline <= fetch_date:
        score += 1
        dimensions.append("临近截止仍未收口")
        evidence.append(f"截止日 {deadline} 仍剩余工时 {left:.1f}h")

    if latest_impl_deadline and latest_test_deadline:
        gap = (latest_test_deadline - latest_impl_deadline).days
        if gap <= 0:
            score += 2
            dimensions.append("测试时间受压")
            evidence.append(f"开发/联调最晚到 {latest_impl_deadline}，测试最晚也到 {latest_test_deadline}")
        elif gap == 1:
            score += 1
            dimensions.append("测试时间受压")
            evidence.append(f"开发/联调最晚到 {latest_impl_deadline}，测试最晚到 {latest_test_deadline}")
    elif total_bug >= 3:
        score += 1
        dimensions.append("测试保障信息不足")
        evidence.append("未见独立测试/用例排期")

    if cancel_children >= 2 or recent_change_cnt >= 8:
        score += 2 if cancel_children >= 5 or recent_change_cnt >= 15 else 1
        dimensions.append("流程/推进管理风险")
        evidence.append(f"取消子任务 {cancel_children} 个，截止前两天内关键动作 {recent_change_cnt} 次")

    if len(children) >= 10 and total_bug >= 5:
        score += 1
        dimensions.append("协同复杂度高")
        evidence.append(f"子任务 {len(children)} 个")

    dimensions = _dedupe_keep_order(dimensions)
    evidence = _dedupe_keep_order(evidence)

    if score >= 6:
        risk_level = "高"
    elif score >= 4:
        risk_level = "中"
    elif score >= 3:
        risk_level = "关注"
    else:
        risk_level = "低"

    return {
        "task_id": task_id,
        "task_name": (task.get("name") or task_name or "").strip(),
        "task_link": f"[TASK#{task_id} {(task.get('name') or task_name or '').strip()}]({task_url(task_id)})",
        "score": score,
        "risk_level": risk_level,
        "main_type": _low_quality_main_type(dimensions),
        "dimensions": dimensions,
        "dimension_summary": "、".join(dimensions) if dimensions else "暂无明显信号",
        "evidences": evidence,
        "evidence_summary": "；".join(evidence[:3]) if evidence else "暂无明显依据",
        "conclusion": _compose_low_quality_conclusion((task.get("name") or task_name or "").strip(), dimensions),
        "problem_points": _build_low_quality_problem_points(dimensions),
        "improvements": _build_low_quality_improvements(dimensions),
        "bug_total": total_bug,
        "ext_bug_cnt": ext_bug_cnt,
        "int_bug_cnt": int_bug_cnt,
        "high_bug_cnt": high_bug_cnt,
        "status": task.get("status") or "",
        "deadline": task.get("deadline") or "",
    }


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
    # 只有显式非Bug标识（当前仅 type=performance）才进入“外部非Bug列表”。
    ext_nonbug_candidates = [
        b for b in bugs
        if b.get("classification") in ("1", "2") and _has_explicit_nonbug_marker(b)
    ]
    ext_nonbug_ids = {str(b.get("id", "")) for b in ext_nonbug_candidates}
    all_perf = [b for b in bugs if b.get("type") == "performance"]
    non_perf = [b for b in bugs if b.get("type") != "performance"]

    ext_perf   = ext_nonbug_candidates
    ext_review = [
        b for b in non_perf
        if b.get("classification") in ("1", "2") and str(b.get("id", "")) not in ext_nonbug_ids
    ]
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
        raw_test_resp = _test_responsibility(source_bug, dispute_reason, dnames)
        test_resp_label = _normalize_test_resp_label(primary_resp, secondary_resp, raw_test_resp)
        ext_responsibility_label = _compose_ext_responsibility_label(primary_resp, test_resp_label)
        use_mid, use_mname = _get_task_ref(source_bug)
        phenomenon = ctx["phenomenon"]
        severity_label = _severity_display(source_bug)
        demand = ctx["demand_display"]
        use_case = ctx["use_case_display"]
        is_bug = _determine_bug_status(review_rec)
        judgment_reason = _build_decision_reason(
            bug_title=source_bug.get("title", ""),
            phenomenon=phenomenon,
            scope=ctx["scope"],
            cause=cause,
            dispute_reason=dispute_reason,
            review_rec=review_rec,
            demand=demand,
            use_case=use_case,
            raw_demand=ctx["raw_demand"],
            raw_use_case=ctx["raw_use_case"],
            resolution=source_bug.get("resolution", ""),
            primary_resp=primary_resp,
            secondary_resp=secondary_resp,
            test_resp_label=test_resp_label,
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
            "responsibility_label": ext_responsibility_label,
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
            "test_resp_label": test_resp_label,
            "responsibility_label": ext_responsibility_label,
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
            bug_title=source_bug.get("title", ""),
            phenomenon=phenomenon,
            scope="",
            cause=cause,
            dispute_reason=dispute_reason,
            review_rec=review_rec,
            demand=demand,
            use_case=use_case,
            raw_demand=ctx["raw_demand"],
            raw_use_case=ctx["raw_use_case"],
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

    # ── 内部低质量任务预判 ─────────────────────────────────────────────────────
    task_map: dict[str, dict] = {}
    fetch_date = datetime.strptime(fetch_time[:10], "%Y-%m-%d").date()
    for b in non_perf:
        use_mid, use_mname = _get_task_ref(b)
        if not use_mid:
            continue
        if use_mid not in task_map:
            task_map[use_mid] = {"name": use_mname, "bugs": []}
        task_map[use_mid]["bugs"].append(b)

    low_quality, watch_list = [], []
    for tid, info in sorted(task_map.items(), key=lambda x: -len(x[1]["bugs"])):
        try:
            task_view = client.fetch_task_view(tid, force_refresh=force_refresh)
        except Exception:
            task_view = {}
        assessment = _assess_low_quality_task(tid, info["name"], info["bugs"], task_view, fetch_date)
        if assessment["risk_level"] == "高":
            low_quality.append(assessment)
        elif assessment["risk_level"] in {"中", "关注"}:
            watch_list.append(assessment)
    low_quality.sort(key=lambda x: (x["score"], x["bug_total"]), reverse=True)
    watch_list.sort(key=lambda x: (x["score"], x["bug_total"]), reverse=True)

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
