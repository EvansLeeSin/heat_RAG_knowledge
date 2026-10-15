#!/usr/bin/env bash
# 校验 NAS 运行代码与本地仓库是否逐字节一致（发现"热更新静默失效"造成的版本漂移）。
#
# 背景：`ugos-cli fs put <LOCAL> <REMOTE_DIR>` 第二个参数是**目录**。历史上多次写成
# 目标文件路径，UGOS 实际创建了同名目录并把文件放进去，补丁从未生效，导致 NAS 长期
# 运行旧代码（曾出现 NAS 上 auth.py 为旧版、缺少 Key 时返回 422 而非 401）。
#
# 用法：bash tools/verify_deploy.sh
# 退出码：0 = 全部一致；1 = 存在漂移/缺失
set -uo pipefail

UGOS_BIN="${UGOS_BIN:-/c/Evans/Soft/ugos-cli-x86_64-pc-windows-msvc/ugos-cli.exe}"
: "${UGOS_HOST:?Set UGOS_HOST in the environment}"
: "${UGOS_USER:?Set UGOS_USER in the environment}"
: "${UGOS_PASSWORD:?Set UGOS_PASSWORD in the environment}"
REMOTE_ROOT="${REMOTE_ROOT:-/volume3/docker/heat-knowledge/srv}"
WORK="${WORK:-/c/Users/tianmai/AppData/Local/Temp/hk-verify}"

g() { MSYS_NO_PATHCONV=1 "$UGOS_BIN" --host "$UGOS_HOST" --user "$UGOS_USER" --password "$UGOS_PASSWORD" "$@"; }

rm -rf "$WORK"; mkdir -p "$WORK"
drift=0; n=0
while IFS= read -r f; do
  n=$((n + 1))
  mkdir -p "$WORK/$(dirname "$f")"
  # fs get 的 LOCAL 参数在 Windows 下不接受正斜杠路径，须先 cd 到目标目录只给文件名
  ( cd "$WORK/$(dirname "$f")" && g fs get "$REMOTE_ROOT/$f" "$(basename "$f")" >/dev/null 2>&1 )
  if [ ! -f "$WORK/$f" ]; then
    echo "MISSING  $f"
    drift=$((drift + 1)); continue
  fi
  local_hash=$(sha256sum "$f" | cut -c1-16)
  remote_hash=$(sha256sum "$WORK/$f" | cut -c1-16)
  if [ "$local_hash" = "$remote_hash" ]; then
    echo "OK       $f"
  else
    echo "DRIFT    $f  local=$local_hash remote=$remote_hash"
    drift=$((drift + 1))
  fi
done < <(find app -name "*.py" | sort)

echo
echo "已校验 $n 个文件，漂移 $drift 个"
[ "$drift" -eq 0 ] || exit 1
