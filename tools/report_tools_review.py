"""
tools/report_tools_review.py

版本复盘报告工具层：组装版本复盘所需的完整数据包，返回给 Claude 生成报告。

设计原则（与日报报告工具保持一致）：
  - 只负责"把数据拼在一起"，不生成 Markdown 文本
  - 返回结构化数据字典，Claude 拿到后生成报告正文
  - A类数据（确定性）完整填充，B类数据标注【待确认】，C类预留占位

对外暴露：
  assemble_review_report(client, project_id, version="auto")  → 复盘数据包
  save_review_report(content, version_name)                   → 保存报告文件
"""

import logging
import re
from datetime import date
from pathlib import Path

from bsg_zentao.client import ZentaoClient
from bsg_zentao.constants import ACTIVE_PROJECTS
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


# ─── 目标版本识别 ──────────────────────────────────────────────────────────────

def _resolve_target_version(
    client: ZentaoClient,
    project_id: str,
    version: str,
) -> dict:
    """
    识别版本复盘的目标版本。

    version 参数：
      "auto"     → 自动识别：取 end < today 且 id 最大的已交付版本
      版本 ID 字符串（如 "394"） → 直接使用

    返回：
    {
      "id":    "394",
      "name":  "V2.11.0（0408）",
      "begin": "2026-03-26",
      "end":   "2026-04-08",
    }
    """
    today_str = date.today().isoformat()

    # 合并 undone + closed 获取全量版本
    undone = client.fetch_versions(status="undone").get("executionStats", [])
    closed = client.fetch_versions(status="closed").get("executionStats", [])

    # 去重
    seen: set[str] = set()
    all_execs = []
    for e in undone + closed:
        eid = str(e.get("id", ""))
        if eid and eid not in seen:
            seen.add(eid)
            all_execs.append(e)

    # 只保留当前项目且 end 有效的版本
    INVALID_NAMES = {"W5", "平台组"}
    valid = [
        e for e in all_execs
        if str(e.get("project", "")) == str(project_id)
        and e.get("name", "").strip() not in INVALID_NAMES
        and re.search(r'（\d{4}）', e.get("name", ""))
        and e.get("end", "") not in ("", "0000-00-00")
    ]

    if version == "auto":
        # 取 end < today 中 id 最大的（最近已交付）
        past = [e for e in valid if e["end"] < today_str]
        if not past:
            raise RuntimeError(
                "未找到已交付版本（end < today），请手动指定 version 参数。"
            )
        target = max(past, key=lambda e: int(e["id"]))
    else:
        candidates = [e for e in valid if str(e["id"]) == str(version)]
        if not candidates:
            raise RuntimeError(
                f"未找到版本 ID={version}，请检查输入或确认该版本属于项目 {project_id}。"
            )
        target = candidates[0]

    return {
        "id":    str(target["id"]),
        "name":  target.get("name", "").strip(),
        "begin": target.get("begin", ""),
        "end":   target.get("end", ""),
    }


# ─── 主函数：组装复盘数据包 ────────────────────────────────────────────────────

def assemble_review_report(
    client: ZentaoClient,
    project_id: str,
    version: str = "auto",
) -> dict:
    """
    组装版本复盘所需的完整数据包，返回给 Claude 生成报告。

    参数：
      project_id  → 项目 ID（平台项目传 "10"）
      version     → "auto" 或具体版本 ID 字符串

    返回结构：
    {
      "meta": {
        "version_id":   "394",
        "version_name": "V2.11.0（0408）",
        "begin":        "2026-03-26",
        "end":          "2026-04-08",
        "project_id":   "10",
        "project_name": "平台项目",
      },
      "history": [          ← 时间升序，最近4个历史版本
        {version_id, version_name, ext_bug_total, ext_bug_review,
         test_dept_bugs, int_bugs, ext_reqs, int_reqs},
        ...
      ],
      "ext_bugs": {         ← 外部Bug复盘数据（A类完整，B类含结论草稿占位）
        "all_count", "review_count", "excl_count",
        "excl_list",         ← [{id, title, link, excl_reason}]
        "review_list",       ← [{id, title, link, severity_label, dept_str, is_dispute}]
        "test_dept_count", "test_bug_ids",
        "other_dept_dist",   ← {dept_name: count}
        "deep_analysis",     ← [{id, title, link, severity_label, tracing, depts: [{name, cause, step, is_dispute}]}]
      },
      "int_bugs": {         ← 内部Bug复盘数据
        "total_count", "extreme_count", "high_count",
        "extreme_dept_dist", "high_dept_dist",
        "review_list",       ← 极严重+高等+典型
        "deep_analysis",
      },
      "low_quality": [      ← 低质量任务排名（Bug数 >= 5）
        {rank, task_id, name, link, bug_count, high_extreme_count, dept, judgment_prefix},
        ...
      ],
      "req_trend": {
        "curr_ext": N,
        "curr_int": N,
      },
      "delay": {            ← 过程延期（C类，预留占位）
        "placeholder": true,
        "note": "过程延期（3.2/3.3）需人工填写，已预留占位符",
      },
      "report_path": "~/.bsg-zentao/报告/版本复盘/20260408_V2.11.0_版本复盘.md",
    }

    Claude 使用此数据包时：
      - A类数据（有值的字段）直接填入报告，无需标注
      - B类数据（有草稿的字段）填入并标注【待确认】
      - C类占位（placeholder=true）用【待补充·人工】填充
      - 1.6/2.3/2.6 节的归纳总结由 Claude 根据 deep_analysis 数据综合生成，标注【待确认】
    """
    log.info("开始组装版本复盘数据（项目=%s，版本=%s）…", project_id, version)

    # ── 步骤1：识别目标版本 ──────────────────────────────────────────────────
    log.info("  [1/4] 识别目标版本…")
    target = _resolve_target_version(client, project_id, version)
    vid    = target["id"]
    vname  = target["name"]
    log.info("  目标版本：%s（ID=%s，%s ~ %s）", vname, vid, target["begin"], target["end"])

    # ── 步骤2：拉取当前版本数据 ──────────────────────────────────────────────
    log.info("  [2/4] 拉取当前版本 Bug 数据（%s）…", vname)
    bug_result  = get_version_bugs(client, vid, project_id)
    bugs        = bug_result["bugs"]
    dept_review = bug_result["dept_review"]

    log.info("  [2/4] 拉取当前版本需求池数据…")
    pool_result = get_version_requirements(client, vid, project_id)
    pools       = [p for p in pool_result["pools"] if p.get("task_status") != "cancel"]

    # ── 步骤3：拉取历史版本趋势数据 ─────────────────────────────────────────
    log.info("  [3/4] 拉取历史版本趋势数据（最近4个）…")
    history = get_version_history(client, vid, project_id, max_count=4)
    log.info("  已获取 %d 个历史版本数据", len(history))

    # ── 步骤4：计算各板块数据 ────────────────────────────────────────────────
    log.info("  [4/4] 计算各板块数据…")

    ext_data     = calc_ext_bugs(bugs, dept_review)
    int_data     = calc_int_bugs(bugs, dept_review)
    low_quality  = calc_low_quality(pools, bugs, dept_review)
    req_counts   = calc_req_counts(pools)

    # ── 组装返回结构 ─────────────────────────────────────────────────────────
    report_path = get_report_path("版本复盘", make_review_filename(vname))

    result = {
        "meta": {
            "version_id":   vid,
            "version_name": vname,
            "begin":        target["begin"],
            "end":          target["end"],
            "project_id":   project_id,
            "project_name": _project_name(project_id),
        },
        "history":     history,
        "ext_bugs":    ext_data,
        "int_bugs":    int_data,
        "low_quality": low_quality,
        "req_trend": {
            "curr_ext": req_counts["ext_reqs"],
            "curr_int": req_counts["int_reqs"],
        },
        "delay": {
            "placeholder": True,
            "note": "过程延期（3.2/3.3）需人工填写，已预留【待补充·人工】占位符",
        },
        "report_path": str(report_path),
    }

    log.info("版本复盘数据组装完成。")
    return result


# ─── 保存报告文件 ─────────────────────────────────────────────────────────────

def save_review_report(content: str, version_name: str) -> str:
    """
    把 Claude 生成的版本复盘报告保存到本地文件。

    参数：
      content:      Claude 生成的 Markdown 格式报告文本
      version_name: 版本名称，用于生成文件名（如 "V2.11.0（0408）"）

    返回：保存路径字符串
    完整路径示例：~/.bsg-zentao/报告/版本复盘/20260408_V2.11.0_版本复盘.md
    """
    filename = make_review_filename(version_name)
    path     = get_report_path("版本复盘", filename)
    path.write_text(content, encoding="utf-8")
    log.info("版本复盘报告已保存：%s", path)
    return str(path)


# ─── 辅助：项目名称 ───────────────────────────────────────────────────────────

def _project_name(project_id: str) -> str:
    for name, pid in ACTIVE_PROJECTS.items():
        if pid == project_id:
            return name
    return f"项目{project_id}"
