// Running access/network readiness, deliberately independent of DuckDB.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const net = require('node:net');
const {Client} = require('/tools/smoke/node_modules/pg');
const role = process.argv[2];
(async () => {
  assert.equal(process.getuid(), 501); assert.equal(process.getgid(), 20);
  for (const p of ['/source', '/tools', '/scripts', '/secrets', ...(role === 'producer' ? [] : ['/fixture'])]) {
    assert.equal(fs.readFileSync(p + '/readiness', 'utf8'), 'populated\n');
    assert.throws(() => fs.writeFileSync(p + '/must-refuse', 'x'), /EROFS|EACCES/);
  }
  for (const p of ['/source/packages/warehouses/src/warehouseClients/DuckdbWarehouseClient.ts', '/source/packages/common/src/index.ts', '/scripts/client.ts', '/scripts/pins.json', '/tools/exe.linux-x64/pnpm', '/tools/linux-x64/bin/esbuild', '/secrets/catalog.json', '/fixture/data/readiness']) {
    assert.ok(fs.readFileSync(p).length > 0, `Unreadable intended input ${p}`);
    if (!(role === 'producer' && p.startsWith('/fixture')))
      assert.throws(() => { const fd=fs.openSync(p,'a'); fs.closeSync(fd); }, /EROFS|EACCES/);
  }
  for (const p of ['/work', '/home/probe', ...(role === 'producer' ? ['/fixture'] : [])]) {
    fs.writeFileSync(p + '/readiness-write', 'x'); fs.unlinkSync(p + '/readiness-write');
  }
  assert.deepEqual(Object.keys(os.networkInterfaces()), ['lo']);
  const routes = fs.readFileSync('/proc/net/route', 'utf8').trim().split('\n');
  assert.equal(routes.length, 1, 'Non-loopback route exists');
  await new Promise((resolve, reject) => {
    const s = net.connect({host:'198.51.100.1', port:80});
    s.once('connect', () => reject(new Error('External numeric egress succeeded')));
    s.once('error', resolve); s.setTimeout(2000, () => {s.destroy(); reject(new Error('Egress was not immediately refused'));});
  });
  if (role !== 'installer') {
    const secret = JSON.parse(fs.readFileSync('/secrets/catalog.json', 'utf8'));
    let last;
    for (let i=0; i<40; i++) {
      const pg = new Client({host:'127.0.0.1',database:'probe',user:'postgres',password:secret.adminPassword});
      try {await pg.connect(); assert.equal((await pg.query('SELECT 1 AS n')).rows[0].n,1); const rules=(await pg.query("SELECT auth_method FROM pg_hba_file_rules WHERE type='host'")).rows; assert.ok(rules.length); assert.ok(rules.every(r=>r.auth_method==='scram-sha-256')); await pg.end(); last=null; break;}
      catch(e) {last=e; await pg.end().catch(()=>{}); await new Promise(r=>setTimeout(r,250));}
    }
    if (last) throw last;
  }
  console.log(JSON.stringify({readiness:'PASS',role,uid:process.getuid(),gid:process.getgid(),interfaces:os.networkInterfaces(),routes}));
})().catch(e => {console.error(String(e)); process.exitCode=1;});
