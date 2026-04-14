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
    if val is None:
        return "—"
    s = str(val).replace("|", "｜").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or "—"


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def md_table(headers: list[str], rows: list[list]) -> str:
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
    if not d or d == _DATE_ZERO:
        return None
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fmt_date(d: str) -> str:
    dt = parse_date(d)
    return dt.strftime("%m%d") if dt else "—"


def fmt_date_full(d: str) -> str:
    dt = parse_date(d)
    return f"{dt.month}月{dt.day}日" if dt else "—"


def days_overdue(d: str, today: date | None = None) -> int:
    dt = parse_date(d)
    if not dt:
        return 0
    ref = today or date.today()
    return (ref - dt).days


def days_since(d: str, today: date | None = None) -> int:
    if not d:
        return 0
    dt = parse_date(d[:10])
    if not dt:
        return 0
    ref = today or date.today()
    return (ref - dt).days


def is_today(d: str, today: date | None = None) -> bool:
    dt = parse_date(d)
    if not dt:
        return False
    return dt == (today or date.today())


def is_release_day(version_end: str, today: date | None = None) -> bool:
    dt = parse_date(version_end)
    if not dt:
        return False
    return dt == (today or date.today())


def remaining_days(version_end: str, today: date | None = None) -> int:
    dt = parse_date(version_end)
    if not dt:
        return 0
    ref = today or date.today()
    return (dt - ref).days


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_cn(d: date | None = None) -> str:
    ref = d or date.today()
    return WEEKDAY_CN[ref.weekday()]


# ─── 文件路径 ─────────────────────────────────────────────────────────────────

# 报告输出到项目根目录下的「报告/」文件夹，按类型分子目录
# 目录结构：bsg-zentao/报告/日报/、版本复盘/、Bug界定/、周汇总/
_PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR    = _PROJECT_ROOT / "报告"


def get_report_path(category: str, filename: str) -> Path:
    """
    生成报告文件路径，自动创建目录。
    category: "日报" / "版本复盘" / "Bug界定" / "周汇总"
    """
    dir_path = OUTPUT_DIR / category
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / filename


def make_daily_filename(today: date | None = None) -> str:
    ref = today or date.today()
    return f"{ref.strftime('%Y%m%d')}_日报.md"


def make_weekly_filename(today: date | None = None) -> str:
    ref = today or date.today()
    year, week, _ = ref.isocalendar()
    return f"{year}{week:02d}_周汇总.md"


def make_review_filename(version_name: str) -> str:
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
