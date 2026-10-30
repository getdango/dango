const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const {createRequire} = require('node:module');
const pins = require('/scripts/pins.json');
const rq = createRequire('/work/deploy/package.json');
const api = rq('@duckdb/node-api');
assert.equal(rq('@duckdb/node-api/package.json').version,pins.native.api_version);
assert.match(api.version(),/^v?2\./);
const ar = createRequire(rq.resolve('@duckdb/node-api'));
const br = createRequire(ar.resolve('@duckdb/node-bindings'));
const root = path.dirname(br.resolve('@duckdb/node-bindings-linux-x64/package.json'));
for (const name of ['libduckdb.so','duckdb.node']) {
  const matches=[];
  const visit=p=>{for(const entry of fs.readdirSync(p,{withFileTypes:true})){const f=path.join(p,entry.name);if(entry.isDirectory())visit(f);else if(entry.name===name)matches.push(f);}};
  visit(root); assert.equal(matches.length,1);
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(matches[0])).digest('hex'),pins.native[name]);
}
console.log(JSON.stringify({native:'PASS',engine:api.version(),package:pins.native.api_version,platform:process.platform,architecture:process.arch}));
