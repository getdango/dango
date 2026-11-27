#!/bin/sh
set -eu
export XDG_CACHE_HOME=/home/probe/cache XDG_CONFIG_HOME=/home/probe/config XDG_DATA_HOME=/home/probe/data
PNPM=/tools/exe.linux-x64/pnpm
test "$("$PNPM" --version)" = 12.3.4
if [ "$1" = graph ]; then
  "$PNPM" --dir /source --filter @lightdash/warehouses... list --prod --lockfile-only --depth Infinity --json --config.manage-package-manager-versions=false > /work/selected-graph.json
else
  PNPM_CONFIG_CACHE_DIR=/work/pnpm-cache "$PNPM" --dir /work/project --filter @lightdash/warehouses... install --offline --prod --ignore-scripts --frozen-lockfile --store-dir /work/store --config.manage-package-manager-versions=false
  export NODE_PATH=/work/deploy/node_modules:/work/deploy/node_modules/@lightdash/common/node_modules
  /tools/linux-x64/bin/esbuild /scripts/client.ts --bundle --platform=node --target=node24 --format=cjs --external:@duckdb/node-api --alias:@lightdash/common=/source/packages/common/src/index.ts --metafile=/work/compiler.json --outfile=/work/deploy/client.cjs
fi
