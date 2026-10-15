#!/usr/bin/env bash
# 构建 project.tar —— UGOS 部署用的源码包（setup 容器解压到 /app 挂载点）。
#
# 为什么需要它：`ugos-cli docker project-create` 会重跑 setup 的 tar 解压，
# 从而**覆盖**此前通过 `fs put` 做的单文件热更新。若 tar 落后于仓库，
# 下一次重建项目就会把已修复的缺陷悄悄回退（本仓库真实踩过：auth.py 401 修复、
# search.py 范围谓词修复都曾只存在于主机侧）。
#
# 因此：tar 必须始终由当前仓库生成，热更新后也应重建 tar，保持两者一致。
#
# 用法：bash tools/build_tar.sh
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="${OUT:-project.tar}"

INCLUDE=(app config db migrations tests eval tools requirements.txt Dockerfile)

tar -cf "$OUT" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='eval/corpus_chunks.json' \
  --exclude='*.log' \
  "${INCLUDE[@]}"

echo "已生成 $OUT（$(du -h "$OUT" | cut -f1)）"
echo "条目数：$(tar -tf "$OUT" | wc -l | tr -d ' ')"
echo "校验关键修复是否在包内："
for f in app/api/auth.py app/services/search.py app/workers/maintenance.py app/api/debug.py; do
  if tar -tf "$OUT" | grep -qx "$f"; then
    echo "  OK       $f"
  else
    echo "  MISSING  $f  <-- 包内缺少该文件，重建项目将回退修复"
    exit 1
  fi
done
