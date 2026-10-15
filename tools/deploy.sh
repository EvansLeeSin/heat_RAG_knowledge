#!/usr/bin/env bash
# 热更新单文件到 NAS 并校验类型/大小（避免 UGOS 在目标存在时创建同名目录）。
#
# 用法：
#   tools/deploy.sh app/services/search.py app/api/search.py
#   tools/deploy.sh            # 部署下面 FILES 列出的默认集合（烟测用）
#
# 背景（踩坑记录）：
#   - ugos-cli fs put 不会覆盖已存在文件，且某些情况下会在目标位置建**同名目录**；
#     必须先 `fs rm`，再 `fs put`，最后用 `fs ls` 校验类型为文件、大小与本地一致。
#   - 改完代码需 `docker restart knowledge-api` / `knowledge-worker` 才生效。
set -euo pipefail

UGOS_BIN="${UGOS_BIN:-/c/Evans/Soft/ugos-cli-x86_64-pc-windows-msvc/ugos-cli.exe}"
: "${UGOS_HOST:?Set UGOS_HOST in the environment}"
: "${UGOS_USER:?Set UGOS_USER in the environment}"
: "${UGOS_PASSWORD:?Set UGOS_PASSWORD in the environment}"
REMOTE_ROOT="${REMOTE_ROOT:-/volume3/docker/heat-knowledge/srv}"
RESTART="${RESTART:-}"

g() { MSYS_NO_PATHCONV=1 "$UGOS_BIN" --host "$UGOS_HOST" --user "$UGOS_USER" --password "$UGOS_PASSWORD" "$@"; }

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  FILES=(
    app/services/search.py
    app/services/chunker.py
    app/api/search.py
    app/api/debug.py
    app/main.py
    app/core/config.py
    app/workers/tasks.py
    app/workers/maintenance.py
  )
fi

fail=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "SKIP (not a local file): $f"; fail=1; continue; }
  local_size=$(wc -c < "$f" | tr -d ' ')
  remote_dir="$REMOTE_ROOT/$(dirname "$f")"
  remote_name="$(basename "$f")"
  # 注意：`fs put <LOCAL> <REMOTE_DIR>` 第二个参数是**目录**；
  # 若误传文件路径，UGOS 会创建同名目录，补丁实际不会生效。
  g fs rm "$remote_dir/$remote_name" >/dev/null 2>&1 || true
  g fs put "./$f" "$remote_dir" >/dev/null 2>&1 || true
  # 校验：文件名必须存在且类型为 py（若出现 dir 说明踩到 UGOS 目录坑）
  row=$(g fs ls "$remote_dir" 2>/dev/null | grep -E "\| $remote_name " || true)
  if [ -z "$row" ]; then
    echo "FAIL  $f  (remote missing)"; fail=1; continue
  fi
  case "$row" in
    *"| dir "*) echo "FAIL  $f  (remote is a DIRECTORY)"; fail=1; continue ;;
  esac
  echo "OK    $f  local=${local_size}B  remote_row=${row}"
done

if [ -n "$RESTART" ]; then
  for c in $RESTART; do g docker restart "$c" >/dev/null 2>&1 && echo "restarted $c"; done
fi
exit $fail
