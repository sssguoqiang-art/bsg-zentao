"""
bsg_zentao/member_index.py

成员名称索引：维护「显示名 ↔ 禅道账号」的双向映射。

解决问题：
  zentao_get_member_tasks / Bug 按人筛选 等工具需要传入禅道账号（如 chenyi），
  但用户自然会用显示名（如 陈益）提问。
  本模块负责在两者之间做解析，并将索引持久化到本地。

索引文件：~/.bsg-zentao/member_index.json
刷新策略：手动触发（zentao_build_member_index 工具）或超 7 天自动提示
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

INDEX_PATH = Path.home() / ".bsg-zentao" / "member_index.json"

# 索引结构示例：
# {
#   "by_name":    {"陈益": {"username": "chenyi",  "dept_id": "47", "dept_name": "PHP2部"}},
#   "by_account": {"chenyi": {"display_name": "陈益", "dept_id": "47", "dept_name": "PHP2部"}},
#   "built_at":   "2026-04-15",
#   "total":      N,
# }


# ─── 持久化 ───────────────────────────────────────────────────────────────────

def load_index() -> dict:
    """加载本地成员索引，不存在则返回空结构。"""
    if not INDEX_PATH.exists():
        return {"by_name": {}, "by_account": {}, "built_at": "", "total": 0}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("读取成员索引失败：%s", e)
        return {"by_name": {}, "by_account": {}, "built_at": "", "total": 0}


def save_index(index: dict) -> None:
    """持久化成员索引到本地文件。"""
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("成员索引已保存：%s（共 %d 人）", INDEX_PATH, index.get("total", 0))


def index_age_days() -> int:
    """返回索引距今天数，索引不存在时返回 999。"""
    idx = load_index()
    built = idx.get("built_at", "")
    if not built:
        return 999
    try:
        delta = date.today() - date.fromisoformat(built)
        return delta.days
    except Exception:
        return 999


# ─── 构建索引 ─────────────────────────────────────────────────────────────────

def _this_week_range() -> tuple[str, str]:
    """返回本周一到本周日，用于拉取成员列表（覆盖有任务的人最全）。"""
    today   = date.today()
    monday  = today - timedelta(days=today.weekday())
    sunday  = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def build_member_index(client, force: bool = False) -> dict:
    """
    遍历所有平台部门，从 workassignsummary 的 users 字段提取完整成员列表，
    构建双向索引并持久化。

    参数：
      client : ZentaoClient 实例
      force  : True = 强制重建；False = 索引已有时直接返回

    返回完整索引 dict，包含 by_name、by_account、built_at、total。
    """
    from bsg_zentao.constants import MEMBER_QUERY_DEPTS, DEPT_TO_PROJECT, DEPT_MAP

    existing = load_index()
    if not force and existing.get("total", 0) > 0:
        log.info(
            "成员索引已存在（%s，共 %d 人），跳过重建。如需更新请传 force=True。",
            existing.get("built_at", "未知"), existing["total"],
        )
        return existing

    begin, end = _this_week_range()
    by_name:    dict[str, dict] = {}
    by_account: dict[str, dict] = {}

    log.info("开始构建成员索引，遍历 %d 个部门（%s—%s）…", len(MEMBER_QUERY_DEPTS), begin, end)

    for dept_id in MEMBER_QUERY_DEPTS:
        project_id = DEPT_TO_PROJECT.get(dept_id, "10")
        dept_name  = DEPT_MAP.get(dept_id, dept_id)

        try:
            raw = client.fetch_workassign(
                dept=dept_id, project=project_id,
                begin=begin, end=end, user="",
            )
        except Exception as e:
            log.warning("  部门 %s（dept=%s）拉取失败：%s，跳过", dept_name, dept_id, e)
            continue

        # users 字段结构：{group_id: {username: display_name, ...}}
        dept_users: dict[str, str] = {}
        for group_members in raw.get("users", {}).values():
            if isinstance(group_members, dict):
                dept_users.update(group_members)

        # 补充 taskUsers 中出现但 users 里没有的人
        for uname in raw.get("taskUsers", {}):
            if uname and uname not in dept_users:
                dept_users[uname] = uname  # 没有显示名时用账号名代替

        added = 0
        for uname, dname in dept_users.items():
            if not uname or uname == "0":
                continue
            display = dname if (dname and dname != uname) else uname
            record  = {"username": uname, "dept_id": dept_id, "dept_name": dept_name}
            by_name[display]    = record
            by_account[uname]   = {"display_name": display, "dept_id": dept_id, "dept_name": dept_name}
            added += 1

        log.info("  %s（dept=%s）：收录 %d 人", dept_name, dept_id, added)

    index = {
        "by_name":    by_name,
        "by_account": by_account,
        "built_at":   date.today().isoformat(),
        "total":      len(by_account),
    }
    save_index(index)
    log.info("成员索引构建完成，共 %d 人。", len(by_account))
    return index


# ─── 名称解析 ─────────────────────────────────────────────────────────────────

def resolve_member(name_or_account: str, index: Optional[dict] = None) -> Optional[dict]:
    """
    将显示名或账号解析为完整成员信息。

    解析策略（优先级递降）：
      1. 精确匹配账号（by_account）
      2. 精确匹配显示名（by_name）
      3. 模糊匹配显示名（输入字符串包含于/包含显示名）
      4. 模糊匹配账号（输入包含于账号，不区分大小写）

    返回：
      正常 → {"username": "chenyi", "display_name": "陈益", "dept_id": "47", "dept_name": "PHP2部"}
      歧义 → {"_ambiguous": True, "candidates": [...]}
      未找到 → None
    """
    if not name_or_account:
        return None

    idx        = index if index is not None else load_index()
    by_name    = idx.get("by_name", {})
    by_account = idx.get("by_account", {})

    def _pack(uname: str, dname: str, dept_id: str, dept_name: str) -> dict:
        return {
            "username":     uname,
            "display_name": dname,
            "dept_id":      dept_id,
            "dept_name":    dept_name,
        }

    # 1. 精确匹配账号
    if name_or_account in by_account:
        info = by_account[name_or_account]
        return _pack(name_or_account, info["display_name"], info["dept_id"], info["dept_name"])

    # 2. 精确匹配显示名
    if name_or_account in by_name:
        info = by_name[name_or_account]
        return _pack(info["username"], name_or_account, info["dept_id"], info["dept_name"])

    # 3. 模糊匹配显示名
    fuzzy_name = [
        (dname, info) for dname, info in by_name.items()
        if name_or_account in dname or dname in name_or_account
    ]
    if len(fuzzy_name) == 1:
        dname, info = fuzzy_name[0]
        return _pack(info["username"], dname, info["dept_id"], info["dept_name"])
    if len(fuzzy_name) > 1:
        return {
            "_ambiguous": True,
            "candidates": [
                {"username": info["username"], "display_name": dname, "dept_id": info["dept_id"], "dept_name": info["dept_name"]}
                for dname, info in fuzzy_name
            ],
        }

    # 4. 模糊匹配账号（不区分大小写）
    q = name_or_account.lower()
    fuzzy_acct = [
        (uname, info) for uname, info in by_account.items()
        if q in uname.lower()
    ]
    if len(fuzzy_acct) == 1:
        uname, info = fuzzy_acct[0]
        return _pack(uname, info["display_name"], info["dept_id"], info["dept_name"])
    if len(fuzzy_acct) > 1:
        return {
            "_ambiguous": True,
            "candidates": [
                {"username": uname, "display_name": info["display_name"], "dept_id": info["dept_id"], "dept_name": info["dept_name"]}
                for uname, info in fuzzy_acct
            ],
        }

    return None
