"""
setup_config.py

首次初始化脚本。
用户克隆 git 仓库后，运行此脚本完成配置：
  python setup_config.py

流程：
  1. 交互式输入禅道账号密码
  2. 验证登录是否成功
  3. 保存配置到 ~/.bsg-zentao/config.json
  4. 引导用户完成个人知识库 Profile 配置
  5. 输出 Claude Code 的 MCP 注册命令
"""

import getpass
import json
import sys
from pathlib import Path

import requests
import urllib3

# 引入知识库模块（Profile 配置）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bsg_zentao.user_knowledge import save_profile, get_profile, ROLES, DEPARTMENTS, COMMON_TASKS, OUTPUT_PREFERENCES

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL    = "https://cd.baa360.cc:20088/index.php"
CONFIG_DIR  = Path.home() / ".bsg-zentao"
CONFIG_PATH = CONFIG_DIR / "config.json"

_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
}


def verify_login(account: str, password: str) -> bool:
    """尝试登录禅道，返回是否成功。"""
    try:
        resp = requests.get(
            BASE_URL,
            params={"m": "user", "f": "login", "account": account, "password": password, "t": "json"},
            headers=_HEADERS,
            timeout=15,
            verify=False,
        )
        data = resp.json()
        return data.get("status") == "success"
    except Exception as e:
        print(f"  连接失败：{e}")
        return False


def main():
    print()
    print("════════════════════════════════════")
    print("  BSG 禅道助手 · 初始化配置")
    print("════════════════════════════════════")
    print()
    print(f"配置将保存到：{CONFIG_PATH}")
    print("账号密码仅保存在本地，不会上传到任何地方。")
    print()

    # 已有配置时询问是否覆盖
    if CONFIG_PATH.exists():
        overwrite = input("检测到已有配置，是否重新配置？(y/N) ").strip().lower()
        if overwrite != "y":
            print("取消，保留现有配置。")
            _print_mcp_command()
            return

    # 输入账号密码
    for attempt in range(1, 4):
        print(f"请输入禅道账号（第 {attempt}/3 次）：")
        account  = input("  账号：").strip()
        password = getpass.getpass("  密码：")

        if not account or not password:
            print("  账号或密码不能为空，请重新输入。")
            continue

        print("  正在验证登录…")
        if verify_login(account, password):
            print("  ✅ 登录验证成功！")
            break
        else:
            print("  ❌ 登录失败，请检查账号密码。")
            if attempt == 3:
                print("  已达到最大重试次数，退出。")
                sys.exit(1)
    else:
        sys.exit(1)

    # 保存配置
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # 同时创建报告和缓存目录
    (CONFIG_DIR / "缓存").mkdir(exist_ok=True)
    (CONFIG_DIR / "报告" / "日报").mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "报告" / "周汇总").mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "报告" / "Bug界定").mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "报告" / "版本复盘").mkdir(parents=True, exist_ok=True)

    config = {"account": account, "password": password}
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    # 设置文件权限为仅当前用户可读写（Linux/macOS）
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass  # Windows 不支持，忽略

    print()
    print("════════════════════════════════════")
    print("  ✅ 配置完成！")
    print(f"  配置文件：{CONFIG_PATH}")
    print(f"  报告目录：{CONFIG_DIR / '报告'}")
    print(f"  缓存目录：{CONFIG_DIR / '缓存'}")
    print("════════════════════════════════════")
    print()

    # 第二步：引导配置个人知识库 Profile
    _setup_profile(account)

    _print_mcp_command()


def _setup_profile(zentao_account: str):
    """交互式引导用户完成个人 Profile 配置。"""
    print()
    print("════════════════════════════════════")
    print("  第二步：配置个人知识库")
    print("════════════════════════════════════")
    print()
    print("知识库帮助系统记住你的身份和使用习惯，越用越懂你。")
    print("以下信息可以随时在 Claude Code 中说"修改我的配置"来更新。")
    print()

    # 如果已有 Profile，询问是否重新配置
    existing = get_profile()
    if existing:
        overwrite = input("检测到已有个人配置，是否重新设置？(y/N) ").strip().lower()
        if overwrite != "y":
            print("  保留现有配置，跳过。")
            return
        print()

    data = {"zentao_account": zentao_account}

    # 姓名
    name = input("你的姓名（如：张三）：").strip()
    if name:
        data["name"] = name

    # 部门
    print()
    print("所在部门：")
    for i, dept in enumerate(DEPARTMENTS, 1):
        print(f"  {i}. {dept}")
    dept_input = input(f"输入序号（1-{len(DEPARTMENTS)}）或直接输入部门名：").strip()
    if dept_input.isdigit() and 1 <= int(dept_input) <= len(DEPARTMENTS):
        data["department"] = DEPARTMENTS[int(dept_input) - 1]
    elif dept_input:
        data["department"] = dept_input

    # 角色
    print()
    print("你的角色：")
    for i, role in enumerate(ROLES, 1):
        print(f"  {i}. {role}")
    role_input = input(f"输入序号（1-{len(ROLES)}）：").strip()
    if role_input.isdigit() and 1 <= int(role_input) <= len(ROLES):
        data["role"] = ROLES[int(role_input) - 1]

    # 主要关注项目
    print()
    projects = ["平台项目", "游戏项目", "两者"]
    print("主要关注项目：")
    for i, p in enumerate(projects, 1):
        print(f"  {i}. {p}")
    proj_input = input("输入序号（1-3）：").strip()
    if proj_input.isdigit() and 1 <= int(proj_input) <= len(projects):
        data["primary_project"] = projects[int(proj_input) - 1]

    # 常用操作（多选）
    print()
    print("常用操作（多选，用逗号分隔序号，如：1,3,4）：")
    for i, task in enumerate(COMMON_TASKS, 1):
        print(f"  {i}. {task}")
    tasks_input = input("输入序号：").strip()
    if tasks_input:
        selected = []
        for idx in tasks_input.split(","):
            idx = idx.strip()
            if idx.isdigit() and 1 <= int(idx) <= len(COMMON_TASKS):
                selected.append(COMMON_TASKS[int(idx) - 1])
        if selected:
            data["common_tasks"] = selected

    # 输出偏好
    print()
    print("输出偏好：")
    for i, pref in enumerate(OUTPUT_PREFERENCES, 1):
        print(f"  {i}. {pref}")
    pref_input = input("输入序号（1-2）：").strip()
    if pref_input.isdigit() and 1 <= int(pref_input) <= len(OUTPUT_PREFERENCES):
        data["output_pref"] = OUTPUT_PREFERENCES[int(pref_input) - 1]

    # 保存
    save_profile(data)
    print()
    print("════════════════════════════════════")
    print("  ✅ 个人知识库配置完成！")
    if data.get("name"):
        print(f"  你好，{data['name']}！系统已记住你的信息。")
    print("════════════════════════════════════")
    print()


def _print_mcp_command():
    """输出 Claude Code 注册 MCP Server 的命令。"""
    script_path = Path(__file__).resolve().parent / "mcp_server.py"
    print("接下来，在终端运行以下命令将工具注册到 Claude Code：")
    print()
    print(f"  claude mcp add bsg-zentao python {script_path}")
    print()
    print("注册完成后，在 Claude Code 中输入：")
    print("  '帮我出今天的日报'")
    print("即可开始使用。")
    print()


if __name__ == "__main__":
    main()
