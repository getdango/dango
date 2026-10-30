#!/bin/sh
set -eu
export XDG_CACHE_HOME=/home/probe/cache XDG_CONFIG_HOME=/home/probe/config XDG_DATA_HOME=/home/probe/data
PNPM=/tools/exe.linux-x64/pnpm
if [ "$1" = lock ]; then
  "$PNPM" --dir /source --filter @lightdash/warehouses deploy --prod --ignore-scripts --lockfile-only /work/deploy --config.manage-package-manager-versions=false --offline
else
  "$PNPM" --dir /work/deploy install --offline --prod --ignore-scripts --frozen-lockfile --store-dir /work/store --config.manage-package-manager-versions=false
  export NODE_PATH=/work/deploy/node_modules:/work/deploy/node_modules/@lightdash/common/node_modules
  /tools/linux-x64/bin/esbuild /scripts/client.ts --bundle --platform=node --target=node24 --format=cjs --external:@duckdb/node-api --alias:@lightdash/common=/source/packages/common/src/index.ts --metafile=/work/compiler.json --outfile=/work/deploy/client.cjs
fi
