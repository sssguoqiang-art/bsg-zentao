"""
setup_config.py

首次初始化脚本。
用户克隆 git 仓库后，运行此脚本完成配置：
  python setup_config.py

流程：
  1. 交互式输入禅道账号密码
  2. 验证登录是否成功
  3. 保存配置到 ~/.bsg-zentao/config.json
  4. 输出 Claude Code 的 MCP 注册命令
"""

import getpass
import json
import sys
from pathlib import Path

import requests
import urllib3

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

    _print_mcp_command()


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
