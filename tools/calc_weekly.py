"""
tools/calc_weekly.py

周汇总计算逻辑的兼容入口。
真实实现暂存放在仓库根目录的 calc_weekly.py，这里做包内导出，
保证 mcp_server / report_tools_weekly_addition 可以稳定按 tools.* 导入。
"""

from calc_weekly import (  # noqa: F401
    calc_key_requirements,
    calc_version_adjustments,
    calc_version_delivery,
    calc_weekly_delay,
)

