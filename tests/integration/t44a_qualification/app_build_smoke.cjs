// tests/integration/t44a_qualification/app_build_smoke.cjs
// Fresh populated original installer profile; this proves readiness, not compilation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const crypto = require('node:crypto');

(async () => {
  assert.equal(process.getuid(), 501);
  assert.equal(process.getgid(), 20);
  const cgroup = Object.fromEntries(['memory.max', 'memory.swap.max', 'memory.peak',
    'memory.events', 'cpu.max', 'pids.max'].map(name => [name,
    fs.readFileSync('/sys/fs/cgroup/' + name, 'utf8').trim()]));
  assert.equal(cgroup['memory.max'], String(4608 * 1024 * 1024));
  assert.equal(cgroup['memory.swap.max'], '0');
  assert.equal(cgroup['pids.max'], '128');
  const [quota, period] = cgroup['cpu.max'].split(/\s+/).map(Number);
  assert.equal(quota / period, 4);
  assert.equal(process.env.GOMEMLIMIT, '3500MiB');
  for (const directory of ['/source', '/tools', '/scripts', '/secrets', '/fixture']) {
    assert.equal(fs.readFileSync(directory + '/readiness', 'utf8'), 'populated\n');
    assert.throws(() => fs.writeFileSync(directory + '/must-refuse', 'x'), /EROFS|EACCES/);
  }
  for (const file of ['/source/pnpm-lock.yaml', '/source/.pnpmfile.cjs',
    '/tools/app-pnpm/pnpm', '/scripts/app_build_smoke.cjs', '/fixture/data/readiness']) {
    assert.ok(fs.readFileSync(file).length);
    assert.throws(() => { const fd = fs.openSync(file, 'a'); fs.closeSync(fd); }, /EROFS|EACCES/);
  }
  for (const file of ['pnpm-lock.yaml', '.pnpmfile.cjs']) {
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync('/source/' + file)).digest('hex'),
      crypto.createHash('sha256').update(fs.readFileSync('/work/app/project/' + file)).digest('hex'));
  }
  for (const directory of ['/work', '/work/app/project', '/work/app/store',
    '/work/app/pnpm-cache', '/home/probe']) {
    const file = directory + '/readiness-write';
    fs.writeFileSync(file, 'populated write check\n');
    assert.equal(fs.readFileSync(file, 'utf8'), 'populated write check\n');
    fs.unlinkSync(file);
  }
  assert.deepEqual(Object.keys(os.networkInterfaces()), ['lo']);
  assert.equal(fs.readFileSync('/proc/net/route', 'utf8').trim().split('\n').length, 1);
  await new Promise((resolve, reject) => {
    const socket = net.connect({host: '198.51.100.1', port: 80});
    socket.once('connect', () => reject(new Error('Unexpected nonloopback access')));
    socket.once('error', resolve);
    socket.setTimeout(2000, () => { socket.destroy(); reject(new Error('Expected immediate refusal')); });
  });
  console.log(JSON.stringify({readiness: 'PASS_BUILD_PROFILE', uid: process.getuid(),
    gid: process.getgid(), interfaces: os.networkInterfaces(), node: process.version,
    cgroup, GOMEMLIMIT: process.env.GOMEMLIMIT}));
})().catch(error => { console.error(String(error)); process.exitCode = 1; });
