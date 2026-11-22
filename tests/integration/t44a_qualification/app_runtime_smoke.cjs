// tests/integration/t44a_qualification/app_runtime_smoke.cjs
// Same populated mounts/profile/namespace as the actual service, before expensive work.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const {createRequire} = require('node:module');
const projectRequire = createRequire('/work/app/project/packages/backend/package.json');
const secret = () => JSON.parse(fs.readFileSync('/secrets/app.json', 'utf8'));

async function smoke(component) {
  const profiles = {garage: [0.25, 256], app: [2.25, 2560], actor: [1, 1024]};
  const [cpus, memory] = profiles[component];
  const heaps = {garage: 128, app: 2048, actor: 640};
  assert.equal(process.env.NODE_OPTIONS, '--max-old-space-size=' + heaps[component]);
  assert.equal(process.getuid(), 501);
  assert.equal(process.getgid(), 20);
  const metrics = Object.fromEntries(['memory.max', 'memory.swap.max', 'pids.max', 'cpu.max']
    .map(k => [k, fs.readFileSync('/sys/fs/cgroup/' + k, 'utf8').trim()]));
  assert.equal(metrics['memory.max'], String(memory * 1024 ** 2));
  assert.equal(metrics['memory.swap.max'], '0');
  assert.equal(metrics['pids.max'], '128');
  const [quota, period] = metrics['cpu.max'].split(/\s+/).map(Number);
  assert.equal(quota / period, cpus);
  for (const dir of ['/source', '/tools', '/scripts', '/secrets', '/fixture']) {
    assert.ok(fs.readFileSync(dir + '/readiness').length);
    assert.throws(() => fs.writeFileSync(dir + '/must-refuse', 'x'), /EROFS|EACCES/);
  }
  for (const file of ['/tools/garage', '/scripts/garage.toml', '/secrets/app.json',
    '/fixture/data/readiness']) {
    assert.ok(fs.readFileSync(file).length);
    assert.throws(() => { const fd = fs.openSync(file, 'a'); fs.closeSync(fd); }, /EROFS|EACCES/);
  }
  for (const dir of ['/work', '/home/probe', '/work/garage', '/work/actor-home']) {
    const file = dir + '/readiness-write'; fs.writeFileSync(file, 'owned');
    assert.equal(fs.readFileSync(file, 'utf8'), 'owned'); fs.unlinkSync(file);
  }
  assert.deepEqual(Object.keys(os.networkInterfaces()), ['lo']);
  assert.equal(fs.readFileSync('/proc/net/route', 'utf8').trim().split('\n').length, 1);
  await new Promise((resolve, reject) => {
    const socket = net.connect({host: '198.51.100.1', port: 80});
    socket.once('connect', () => reject(new Error('External egress succeeded')));
    socket.once('error', error => {
      assert.ok(['ENETUNREACH', 'EHOSTUNREACH'].includes(error.code)); resolve();
    });
    socket.setTimeout(2000, () => { socket.destroy(); reject(new Error('Egress refusal deadline')); });
  });
  const pg = new (projectRequire('pg').Client)({host: '127.0.0.1', port: 5432,
    user: 'postgres', database: 'probe', password: secret().pg});
  await pg.connect();
  try {
    const result = await pg.query('SELECT 1 AS n, current_user AS actor, current_database() AS db');
    assert.deepEqual(result.rows, [{n: 1, actor: 'postgres', db: 'probe'}]);
    const rules = await pg.query("SELECT auth_method FROM pg_hba_file_rules WHERE type='host'");
    assert.ok(rules.rows.length && rules.rows.every(row => row.auth_method === 'scram-sha-256'));
  } finally { await pg.end(); }
  console.log(JSON.stringify({readiness: 'PASS_RUNTIME_PROFILE', component, metrics,
    authenticatedPG: true, interfaces: ['lo']}));
}
module.exports = {smoke, projectRequire, secret};
if (require.main === module) smoke(process.argv[2]).catch(() => {
  console.error('Runtime profile smoke failed'); process.exitCode = 1;
});
