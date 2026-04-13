"""
user_knowledge.py

用户级知识库管理模块。

两区设计：
  Profile 区（profile.json）：用户主动配置的身份信息和偏好（引导式填写）
  Memory 区（memory.jsonl） ：系统自动提取 + 用户确认的使用习惯（对话积累）

存储位置：~/.bsg-zentao/
  profile.json   - Profile 区，结构化 JSON
  memory.jsonl   - Memory 区，每行一条记忆（JSONL 格式，便于追加）

Memory 状态流转：
  pending_confirm → confirmed（用户点"记住"）
  pending_confirm → rejected（用户点"不用"）
  confirmed 可被 delete
  全部可被 reset（区分：只清 Memory / 连 Profile 一起清）
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── 存储路径 ────────────────────────────────────────────────────────────────

CONFIG_DIR    = Path.home() / ".bsg-zentao"
PROFILE_PATH  = CONFIG_DIR / "profile.json"
MEMORY_PATH   = CONFIG_DIR / "memory.jsonl"

# ─── 枚举常量 ─────────────────────────────────────────────────────────────────

ROLES = ["开发", "测试", "产品", "美术", "管理", "其他"]

DEPARTMENTS = [
    "产品部", "美术部", "PHP1组", "PHP2组", "Web组",
    "测试部", "游戏部（Cocos）", "效能部", "其他"
]

COMMON_TASKS = [
    "查自己名下的 Bug",
    "查版本进度",
    "生成日报",
    "生成周汇总",
    "查延期情况",
    "Bug 分析",
    "版本复盘",
]

OUTPUT_PREFERENCES = ["摘要优先", "明细优先"]

MEMORY_SOURCES  = ["auto", "user"]   # auto=系统提取, user=用户主动添加
MEMORY_STATUSES = ["pending_confirm", "confirmed", "rejected"]


# ─── Profile 区 ──────────────────────────────────────────────────────────────

def get_profile() -> Optional[dict]:
    """读取 Profile，不存在时返回 None。"""
    if not PROFILE_PATH.exists():
        return None
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_profile(data: dict) -> dict:
    """
    保存 Profile。
    data 应包含以下字段（均可选，未填写字段保留旧值）：
      name             姓名
      zentao_account   禅道账号
      department       部门
      role             角色（开发/测试/产品/美术/管理/其他）
      primary_project  主要关注项目（"平台项目" / "游戏项目" / "两者"）
      common_tasks     常用操作（列表）
      output_pref      输出偏好（"摘要优先" / "明细优先"）
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # 合并旧数据（保留未传字段的旧值）
    existing = get_profile() or {}
    now_str  = datetime.now().isoformat(timespec="seconds")

    merged = {**existing, **data}
    merged["updated_at"] = now_str
    if "setup_at" not in merged:
        merged["setup_at"] = now_str

    PROFILE_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return merged


def profile_to_context(profile: dict) -> str:
    """将 Profile 格式化为注入 Prompt 的自然语言文本。"""
    lines = ["【用户身份】"]
    if profile.get("name"):
        lines.append(f"姓名：{profile['name']}")
    if profile.get("zentao_account"):
        lines.append(f"禅道账号：{profile['zentao_account']}")
    if profile.get("department"):
        lines.append(f"部门：{profile['department']}")
    if profile.get("role"):
        lines.append(f"角色：{profile['role']}")
    if profile.get("primary_project"):
        lines.append(f"主要关注项目：{profile['primary_project']}")
    if profile.get("common_tasks"):
        tasks = profile["common_tasks"]
        if isinstance(tasks, list):
            lines.append(f"常用操作：{' / '.join(tasks)}")
    if profile.get("output_pref"):
        lines.append(f"输出偏好：{profile['output_pref']}")
    return "\n".join(lines)


# ─── Memory 区 ───────────────────────────────────────────────────────────────

def _new_memory_id() -> str:
    """生成唯一记忆 ID，格式：mem_{日期}_{短uuid}"""
    date_str  = datetime.now().strftime("%Y%m%d")
    short_uid = uuid.uuid4().hex[:6]
    return f"mem_{date_str}_{short_uid}"


def _read_all_memories() -> list[dict]:
    """读取所有记忆（包含所有状态）。"""
    if not MEMORY_PATH.exists():
        return []
    memories = []
    for line in MEMORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            memories.append(json.loads(line))
        except Exception:
            continue
    return memories


def _write_all_memories(memories: list[dict]) -> None:
    """覆盖写入所有记忆。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(m, ensure_ascii=False) for m in memories]
    MEMORY_PATH.write_text("\n".join(lines) + ("\n" if lines else ""),
                           encoding="utf-8")


def get_memories(status_filter: Optional[str] = None) -> list[dict]:
    """
    获取记忆列表。
    status_filter: None=全部, "confirmed", "pending_confirm", "rejected"
    """
    memories = _read_all_memories()
    if status_filter:
        memories = [m for m in memories if m.get("status") == status_filter]
    return memories


def add_memory(
    content: str,
    source: str = "auto",
    tags: Optional[list[str]] = None,
    status: str = "pending_confirm",
) -> dict:
    """
    添加一条记忆。
    source: "auto"（系统提取）或 "user"（用户主动添加，直接 confirmed）
    返回新建的记忆 entry。
    """
    if source == "user":
        status = "confirmed"   # 用户主动添加的直接确认

    entry = {
        "id":        _new_memory_id(),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source":    source,
        "status":    status,
        "content":   content,
        "tags":      tags or [],
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with MEMORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def update_memory_status(memory_id: str, new_status: str) -> bool:
    """
    更新某条记忆的状态（confirmed / rejected）。
    返回是否成功找到并更新。
    """
    memories = _read_all_memories()
    updated  = False
    for m in memories:
        if m.get("id") == memory_id:
            m["status"]     = new_status
            m["updated_at"] = datetime.now().isoformat(timespec="seconds")
            updated = True
            break
    if updated:
        _write_all_memories(memories)
    return updated


def delete_memory(memory_id: str) -> bool:
    """删除指定记忆，返回是否成功找到并删除。"""
    memories  = _read_all_memories()
    filtered  = [m for m in memories if m.get("id") != memory_id]
    if len(filtered) == len(memories):
        return False
    _write_all_memories(filtered)
    return True


def reset_memories(keep_profile: bool = True) -> dict:
    """
    重置知识库。
    keep_profile=True：只清 Memory 区，保留 Profile
    keep_profile=False：Memory + Profile 都清
    返回 {"deleted_memories": N, "profile_cleared": bool}
    """
    memories = _read_all_memories()
    count    = len(memories)
    _write_all_memories([])   # 清空 Memory

    profile_cleared = False
    if not keep_profile and PROFILE_PATH.exists():
        PROFILE_PATH.unlink()
        profile_cleared = True

    return {"deleted_memories": count, "profile_cleared": profile_cleared}


def reset_memories_by_source(source: str) -> int:
    """
    按来源清除记忆。source="auto" 只清系统提取的，source="user" 只清用户添加的。
    返回删除数量。
    """
    memories = _read_all_memories()
    kept     = [m for m in memories if m.get("source") != source]
    deleted  = len(memories) - len(kept)
    _write_all_memories(kept)
    return deleted


def memories_to_context(confirmed_only: bool = True) -> str:
    """
    将记忆格式化为注入 Prompt 的自然语言文本。
    confirmed_only=True：只取已确认的记忆注入（pending 不注入，避免干扰）
    """
    status   = "confirmed" if confirmed_only else None
    memories = get_memories(status_filter=status)
    if not memories:
        return ""
    lines = ["【使用习惯记忆】"]
    for m in memories:
        ts   = m.get("timestamp", "")[:10]   # 只取日期部分
        text = m.get("content", "")
        src  = "（用户填写）" if m.get("source") == "user" else ""
        lines.append(f"· [{ts}] {text}{src}")
    return "\n".join(lines)


# ─── 综合上下文注入 ───────────────────────────────────────────────────────────

def get_user_context() -> str:
    """
    获取完整用户上下文，用于注入到 Claude 的 Prompt。
    包含：Profile 区 + 已确认的 Memory 区。
    """
    parts   = []
    profile = get_profile()
    if profile:
        parts.append(profile_to_context(profile))

    mem_ctx = memories_to_context(confirmed_only=True)
    if mem_ctx:
        parts.append(mem_ctx)

    if not parts:
        return "（用户尚未配置个人知识库，建议运行 user_profile_setup 完成初始化）"

    return "\n\n".join(parts)


# ─── 格式化展示（供 MCP 工具返回给 Claude 展示给用户）──────────────────────────

def format_memories_for_display() -> str:
    """格式化所有记忆用于展示（包含 ID，方便用户操作）。"""
    memories = _read_all_memories()
    if not memories:
        return "记忆库为空。"

    confirmed = [m for m in memories if m.get("status") == "confirmed"]
    pending   = [m for m in memories if m.get("status") == "pending_confirm"]
    rejected  = [m for m in memories if m.get("status") == "rejected"]

    sections = []

    if confirmed:
        lines = ["✅ 已确认记忆"]
        for m in confirmed:
            ts  = m.get("timestamp", "")[:10]
            src = "用户" if m.get("source") == "user" else "系统"
            lines.append(f"  [{m['id']}] {m['content']}（{ts} · {src}添加）")
        sections.append("\n".join(lines))

    if pending:
        lines = ["💡 待确认记忆（尚未生效）"]
        for m in pending:
            ts  = m.get("timestamp", "")[:10]
            lines.append(f"  [{m['id']}] {m['content']}（{ts}）")
        sections.append("\n".join(lines))

    if rejected:
        lines = [f"❌ 已拒绝记忆（{len(rejected)} 条，不参与上下文注入）"]
        sections.append("\n".join(lines))

    total = len(memories)
    sections.append(f"共 {total} 条记忆（已确认 {len(confirmed)} · 待确认 {len(pending)} · 已拒绝 {len(rejected)}）")

    return "\n\n".join(sections)


def format_profile_for_display() -> str:
    """格式化 Profile 用于展示。"""
    profile = get_profile()
    if not profile:
        return "尚未配置个人 Profile。"

    lines = ["📋 你的个人配置"]
    field_labels = {
        "name":            "姓名",
        "zentao_account":  "禅道账号",
        "department":      "部门",
        "role":            "角色",
        "primary_project": "主要关注项目",
        "common_tasks":    "常用操作",
        "output_pref":     "输出偏好",
        "setup_at":        "首次配置时间",
        "updated_at":      "最近更新时间",
    }
    for key, label in field_labels.items():
        val = profile.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            val = " / ".join(val)
        lines.append(f"  {label}：{val}")

    return "\n".join(lines)
