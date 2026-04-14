"""
bsg_zentao/utils.py

通用工具函数。
只处理两类事情：
  1. 文字处理（清洗、格式化，让输出安全且好看）
  2. 日期计算（超期天数、距今天数等）

不包含任何业务逻辑，不导入 constants。
"""

import re
from datetime import date, datetime
from pathlib import Path


# ─── 文字处理 ─────────────────────────────────────────────────────────────────

def safe(val) -> str:
    """
    把任意值转成适合放进 Markdown 表格的安全字符串。
    处理以下问题：
      - None / 空值 → "—"
      - 竖线 | → 全角｜（防止破坏表格格式）
      - 换行符 → 空格
      - 连续空白 → 单个空格
    """
    if val is None:
        return "—"
    s = str(val).replace("|", "｜").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or "—"


def strip_html(html: str) -> str:
    """
    去除 HTML 标签，返回纯文本。
    禅道的需求描述、任务说明等字段为 HTML 格式，传给 Claude 前需要清理。
    """
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def md_table(headers: list[str], rows: list[list]) -> str:
    """
    生成 Markdown 表格字符串。
    所有单元格自动经过 safe() 处理。
    rows 为空时返回空字符串（调用方判断是否展示"无"）。
    """
    if not rows:
        return ""
    header_row = "| " + " | ".join(headers) + " |"
    sep_row    = "| " + " | ".join("---" for _ in headers) + " |"
    data_rows  = [
        "| " + " | ".join(safe(cell) for cell in row) + " |"
        for row in rows
    ]
    return "\n".join([header_row, sep_row] + data_rows) + "\n"


# ─── 日期处理 ─────────────────────────────────────────────────────────────────

_DATE_ZERO = "0000-00-00"


def parse_date(d: str) -> date | None:
    """
    把禅道返回的日期字符串解析为 date 对象。
    无效日期（空字符串、"0000-00-00"）返回 None。
    """
    if not d or d == _DATE_ZERO:
        return None
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fmt_date(d: str) -> str:
    """
    把日期字符串格式化为 "MMDD" 形式，用于报告中的简短展示。
    无效日期返回 "—"。
    示例：
      "2026-04-15" → "0415"
      ""           → "—"
    """
    dt = parse_date(d)
    return dt.strftime("%m%d") if dt else "—"


def fmt_date_full(d: str) -> str:
    """
    把日期字符串格式化为 "M月D日" 形式，用于报告标题等场合。
    示例：
      "2026-04-15" → "4月15日"
    """
    dt = parse_date(d)
    return f"{dt.month}月{dt.day}日" if dt else "—"


def days_overdue(d: str, today: date | None = None) -> int:
    """
    计算日期距今已超期多少天。
    返回值 > 0 表示已超期，= 0 表示今天截止，< 0 表示还未到期（不会出现，调用方自行判断）。
    无效日期返回 0。
    """
    dt = parse_date(d)
    if not dt:
        return 0
    ref = today or date.today()
    return (ref - dt).days


def days_since(d: str, today: date | None = None) -> int:
    """
    计算日期距今已过去多少天（用于"需求记录了多久还没下单"等场景）。
    无效日期返回 0。
    """
    if not d:
        return 0
    # 兼容带时间的字符串，如 "2026-02-11 15:27:56"
    dt = parse_date(d[:10])
    if not dt:
        return 0
    ref = today or date.today()
    return (ref - dt).days


def is_today(d: str, today: date | None = None) -> bool:
    """判断日期字符串是否是今天（用于识别今日截止的任务）。"""
    dt = parse_date(d)
    if not dt:
        return False
    return dt == (today or date.today())


def is_release_day(version_end: str, today: date | None = None) -> bool:
    """
    判断今天是否是发布日（version end == today）。
    动态判断，不依赖星期几硬编码。
    """
    dt = parse_date(version_end)
    if not dt:
        return False
    return dt == (today or date.today())


def remaining_days(version_end: str, today: date | None = None) -> int:
    """
    计算距版本截止还剩多少天。
    返回 0 表示今天发布，负数表示已过期（版本识别可能有误，调用方处理）。
    """
    dt = parse_date(version_end)
    if not dt:
        return 0
    ref = today or date.today()
    return (dt - ref).days


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_cn(d: date | None = None) -> str:
    """返回中文星期，如 "周三"。"""
    ref = d or date.today()
    return WEEKDAY_CN[ref.weekday()]


# ─── 文件路径 ─────────────────────────────────────────────────────────────────

OUTPUT_DIR = Path.home() / ".bsg-zentao" / "报告"


def get_report_path(category: str, filename: str) -> Path:
    """
    生成报告文件路径，自动创建目录。

    category: 报告类型，对应子目录名，如 "日报"、"周汇总"、"Bug界定"、"版本复盘"
    filename: 文件名，如 "20260409_日报.md"

    完整路径示例：
      ~/.bsg-zentao/报告/日报/20260409_日报.md
    """
    dir_path = OUTPUT_DIR / category
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / filename


def make_daily_filename(today: date | None = None) -> str:
    """生成日报文件名，如 "20260409_日报.md"。"""
    ref = today or date.today()
    return f"{ref.strftime('%Y%m%d')}_日报.md"


def make_weekly_filename(today: date | None = None) -> str:
    """
    生成周汇总文件名，包含年份和周数，如 "202615_周汇总.md"。
    周数使用 ISO 标准（周一为第一天）。
    """
    ref = today or date.today()
    year, week, _ = ref.isocalendar()
    return f"{year}{week:02d}_周汇总.md"


def make_review_filename(version_name: str) -> str:
    """
    生成版本复盘文件名，如 "20260408_V2.11.0_版本复盘.md"。
    从版本名称中提取 MMDD（如 "V2.11.0（0408）" → "0408"），
    拼合当前年份生成日期前缀。
    无法提取时降级为当天日期前缀。
    """
    m = re.search(r'（(\d{4})）', version_name)
    if m:
        mmdd = m.group(1)
        year = date.today().year
        date_prefix = f"{year}{mmdd}"
    else:
        date_prefix = date.today().strftime("%Y%m%d")
    base = version_name.split("（")[0].strip()
    safe_base = re.sub(r'[\\/:*?"<>|]', "", base)
    return f"{date_prefix}_{safe_base}_版本复盘.md"
