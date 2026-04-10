#!/bin/bash
# fix_repo.sh
# 一键修复 bsg-zentao 仓库目录结构
# 运行方式：bash fix_repo.sh

set -e  # 任何错误立即停止

echo "════════════════════════════════════"
echo "  BSG 禅道助手 · 仓库结构修复"
echo "════════════════════════════════════"
echo ""

# 克隆仓库到临时目录
echo "▶ 克隆仓库..."
git clone https://github.com/sssguoqiang-art/bsg-zentao.git bsg-zentao-fix
cd bsg-zentao-fix

# 创建正确的目录结构
echo "▶ 创建目录结构..."
mkdir -p bsg_zentao
mkdir -p tools

# 移动文件到正确位置
echo "▶ 移动文件..."
git mv client.py    bsg_zentao/client.py
git mv constants.py bsg_zentao/constants.py
git mv utils.py     bsg_zentao/utils.py

git mv calc_daily.py    tools/calc_daily.py
git mv data_tools.py    tools/data_tools.py
git mv report_tools.py  tools/report_tools.py

# 补充缺失的 __init__.py 文件（Python 包必须有）
echo "▶ 补充缺失文件..."
touch bsg_zentao/__init__.py
touch tools/__init__.py
git add bsg_zentao/__init__.py
git add tools/__init__.py

# 补充 .gitignore
cat > .gitignore << 'EOF'
# 用户数据（不进 git）
config.json
*.bak

# Python 运行时产物
__pycache__/
*.py[cod]
*.pyo
.Python
*.egg-info/
dist/
build/

# 虚拟环境
.venv/
venv/
env/

# 编辑器
.DS_Store
.idea/
.vscode/
*.swp

# 测试产物
.pytest_cache/
.coverage
*.log
EOF
git add .gitignore

# 提交
echo "▶ 提交..."
git add .
git commit -m "fix: 修正目录结构，补充缺失文件

- 将 client/constants/utils.py 移入 bsg_zentao/ 包目录
- 将 calc_daily/data_tools/report_tools.py 移入 tools/ 包目录
- 补充 bsg_zentao/__init__.py 和 tools/__init__.py
- 补充 .gitignore"

# 推送
echo "▶ 推送到 GitHub..."
git push

echo ""
echo "════════════════════════════════════"
echo "  ✅ 修复完成！"
echo "════════════════════════════════════"
echo ""
echo "现在可以验证：https://github.com/sssguoqiang-art/bsg-zentao"
