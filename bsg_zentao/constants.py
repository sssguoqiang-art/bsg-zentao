"""
bsg_zentao/constants.py

所有业务常量的唯一来源。
规则：接口原始值在这里定义，显示名称也在这里映射。
其他文件只从这里导入，不自己定义常量。
"""

# ─── 项目 ID（execution/all 中 executionStats[n].project 的值）────────────────

# 活跃项目
PROJECT_PLATFORM  = "10"   # 平台项目
PROJECT_EGC       = "17"   # EGC项目
PROJECT_AI        = "33"   # AI伴侣
PROJECT_FG        = "48"   # FG项目
PROJECT_GAME      = "51"   # 游戏项目
PROJECT_INTERNAL  = "106"  # 内部需求
PROJECT_SG        = "336"  # SG项目

# 已归档项目（有历史版本数据，复盘时可能遇到）
PROJECT_GAME2     = "83"   # 游戏2组
PROJECT_CC_V1     = "160"  # CC项目初版
PROJECT_CC_MID    = "178"  # CC中台
PROJECT_CC_HUB    = "181"  # CC总台
PROJECT_CC_HALL   = "182"  # CC游戏大厅
PROJECT_CC_MCH    = "183"  # CC商户后台
PROJECT_PLATFORM2 = "262"  # 平台2部
PROJECT_MIGRATE   = "333"  # 新平台数据迁移

# 供 Claude Code 引导用户选择项目时使用
# 格式：{显示名: project_id}
ACTIVE_PROJECTS = {
    "平台项目": PROJECT_PLATFORM,
    "游戏项目": PROJECT_GAME,
    "EGC项目":  PROJECT_EGC,
    "AI伴侣":   PROJECT_AI,
    "FG项目":   PROJECT_FG,
    "SG项目":   PROJECT_SG,
}


# ─── 部门 ID → 接口原始名称（用于数据匹配，不用于输出展示）─────────────────────
# ⚠️ 查询逻辑、数据过滤必须用接口原始名称，不能用显示名称

DEPT_MAP = {
    "1":  "产品部",
    "27": "美术部",
    "42": "Cocos部",
    "43": "Web部",
    "44": "PHP1部",
    "45": "测试部",
    "47": "PHP2部",
    "48": "效能部",
    "51": "Social项目",
}

# ─── 接口原始名称 → 对外显示名称（只在最终输出时使用）──────────────────────────
# ⚠️ 只在报告渲染阶段做转换，查询和计算阶段始终用接口原始名称

DEPT_DISPLAY = {
    # 部门名
    "产品部":    "产品组",
    "美术部":    "美术组",
    "Cocos部":   "Cocos组",
    "Web部":     "Web组",
    "PHP1部":    "PHP1组",
    "PHP2部":    "PHP2组",
    "测试部":    "测试组",
    "效能部":    "效能组",
    "Social项目": "Social项目",
    # 项目名
    "平台部":    "平台项目",
    "游戏部":    "游戏项目",
}


def to_display(raw_name: str) -> str:
    """接口原始名称 → 对外显示名称，用于报告输出阶段。"""
    return DEPT_DISPLAY.get(raw_name, raw_name)


# ─── 日报报告中展示的部门（固定顺序，使用接口原始名称）──────────────────────────
# ⚠️ 顺序影响报告中部门进度表的行顺序，不要随意调整

REPORT_DEPTS_RAW = ["美术部", "PHP1部", "PHP2部", "Web部", "Cocos部"]

# 报告输出时对应的显示名称（与 REPORT_DEPTS_RAW 一一对应）
REPORT_DEPTS_DISPLAY = [to_display(d) for d in REPORT_DEPTS_RAW]


# ─── taskDetails 的 dept_key → 接口原始部门名称────────────────────────────────
# pool/browse 返回的 taskDetails 结构：
#   {taskID: {art: [...], devel: [...], cocos: [...], web: [...], qa: [...], ...}}
# devel 需要额外按 phpGroup/成员归属 细分为 PHP1部/PHP2部

TASK_DETAIL_DEPT_MAP = {
    "art":    "美术部",
    "cocos":  "Cocos部",
    "web":    "Web部",
    "test":   "Cocos部",   # 部分版本中 Cocos 子任务用 test key
    # "devel" → 需结合 phpGroup 或成员归属判断，不在此映射
    # "qa"    → 测试部，不在 REPORT_DEPTS_RAW 中，日报不展示
    # "design"→ 产品部，不在 REPORT_DEPTS_RAW 中，日报不展示
}

# ─── PHP 组归属（pool browse 的 users 字段 group_id → 接口原始部门名）──────────
# ⚠️ phpGroup 字段不可靠（计划填写，不一定有数据）
# 正确做法：优先用 phpGroup，无值时从子任务 finishedBy/assignedTo 在 users 中推断

PHPGROUP_DEPT_MAP = {
    "44": "PHP1部",   # PHP1大组
    "46": "PHP1部",   # AI组归属PHP1
    "47": "PHP2部",   # PHP2大组
}


def build_php_member_map(users: dict) -> dict[str, str]:
    """
    从 pool/browse 返回的 users 字段构建 {username: 接口原始部门名} 映射。
    users 结构：{group_id: {username: displayname, ...}, ...}
    用于 phpGroup 为空时，通过子任务执行人推断 PHP 部门归属。
    """
    mapping: dict[str, str] = {}
    for group_id, members in users.items():
        dept = PHPGROUP_DEPT_MAP.get(str(group_id))
        if not dept or not isinstance(members, dict):
            continue
        for username in members:
            if username and username != "0":
                mapping[username] = dept
    return mapping


# ─── 需求类别（pool 对象的 category 字段）────────────────────────────────────

CATEGORY_DISPLAY = {
    "version":   "版本需求",
    "operation": "运维需求",
    "internal":  "内部需求",
}

# ─── 任务状态分组（pool 对象的 taskStatus 字段）──────────────────────────────

# 已完成（不再需要跟进）
STATUS_DONE = frozenset({"done", "closed", "cancel"})

# 测试阶段
STATUS_TESTING = frozenset({"waittest", "testing"})

# 开发阶段（还在推进中）
STATUS_DEV = frozenset({"wait", "doing", "pause", "rejected", "reviewing", "unsure"})

# ─── Bug 分类（bug 对象的 classification 字段）───────────────────────────────

# 线上 Bug：外部开发Bug(1) + 外部历史Bug(2)
ONLINE_BUG_CLASSIFICATIONS = frozenset({"1", "2"})

# 内部复盘 Bug：内部开发Bug(4) + 内部历史Bug(5)
INTERNAL_BUG_CLASSIFICATIONS = frozenset({"4", "5"})

# ─── 标签 ID（pool 对象的 poolTags 字段，逗号分隔）──────────────────────────

TAG_CHADAN    = "11"   # 插单
TAG_JIDAN     = "19"   # 急单
TAG_YINGYING  = "17"   # 运营重点
TAG_KUABAN    = "15"   # 跨版本
TAG_IMPORTANT = "3"    # 重要
TAG_BUG2REQ   = "9"    # Bug转需求
TAG_HIGH_RISK = "13"   # 高危
TAG_GOLD      = "18"   # 金币改动
TAG_APPEND    = "21"   # 追加版本
TAG_ASAP      = "22"   # 做完就出

# ─── 部门 ID → 项目 ID（workassignsummary 接口查询时使用）────────────────────────
# 不同部门挂在不同 project 下，调用 workassignsummary 时需传对应 project_id

DEPT_TO_PROJECT = {
    "1":  "10",   # 产品部 → 平台项目
    "27": "10",   # 美术部 → 平台项目
    "42": "10",   # Cocos部 → 平台项目
    "43": "10",   # Web部 → 平台项目
    "44": "10",   # PHP1部 → 平台项目
    "45": "10",   # 测试部 → 平台项目
    "47": "10",   # PHP2部 → 平台项目
    "48": "10",   # 效能部 → 平台项目
    "10": "51",   # 游戏（旧dept）→ 游戏项目
    "51": "51",   # 游戏项目部门
}

# 按人查询时，如果不指定 dept，依次尝试这些部门（平台所有部门）
MEMBER_QUERY_DEPTS = ["44", "47", "43", "42", "27", "45", "1", "48"]


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def get_category_display(pool: dict) -> str:
    """从 pool 对象取需求类别的显示名称。"""
    return CATEGORY_DISPLAY.get(pool.get("category", ""), pool.get("category", "—"))


def is_unordered(pool: dict) -> bool:
    """判断需求是否未下单（taskID 为空或 '0'）。"""
    return str(pool.get("taskID", "") or "") in ("", "0")


def get_main_task_id(bug: dict) -> str | None:
    """
    从 Bug 对象取关联主任务 ID。

    mainTaskId 字段类型不一致（已实测）：
      - 无关联时为 int(0)
      - 有关联时为 string，如 "45160"
    统一转换，无关联返回 None。
    """
    mid = bug.get("mainTaskId", 0)
    if mid and mid != 0 and str(mid) != "0":
        return str(mid)
    return None


def has_tag(pool: dict, tag_id: str) -> bool:
    """判断需求是否包含指定标签。"""
    tags = str(pool.get("poolTags", "") or "")
    return tag_id in tags.split(",")
