#!/usr/bin/env bash
# 提交前静态检查（PITFALLS #73 教训）。
#
# 背景：`handle_event` 里引用了未定义变量引发 UnboundLocalError，异常被兜底 except
# 吞成一行**无堆栈**日志，话单全 UNKNOWN 却长时间无人发现。这类「未定义变量」缺陷
# 动态测试极难覆盖（要走特定分支才触发），但 pyflakes 静态一抓就准。
#
# 用法：bash tools/precheck.sh      （退出码 0 = 通过）
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0

echo "[1/2] py_compile（语法）"
if python3 -m compileall -q src/ >/dev/null 2>&1; then
  echo "  OK"
else
  echo "  FAIL: src/ 存在语法错误"
  python3 -m compileall -q src/ 2>&1 | tail -5 | sed 's/^/  /'
  fail=1
fi

echo "[2/2] pyflakes（undefined name 类）"
if ! python3 -c "import pyflakes" 2>/dev/null; then
  echo "  SKIP: 未安装 pyflakes —— 建议 pip install pyflakes"
else
  out="$(python3 -m pyflakes src/ 2>&1 | grep -E 'undefined name|referenced before assignment' || true)"
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | sed 's/^/  /'
    echo "  FAIL: 存在未定义变量引用 —— 运行期可能抛 UnboundLocalError 且被兜底 except 吞掉"
    fail=1
  else
    echo "  OK"
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "[precheck] 全部通过"
else
  echo "[precheck] 未通过 —— 请修复后再提交"
fi
exit $fail
