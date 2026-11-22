// tests/integration/t44a_qualification/held_actor.cjs
// One genuine full-app request, an executing PG row-query gate, and one native commit.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {projectRequire, secret} = require('./app_runtime_smoke.cjs');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const pause = () => new Promise(resolve => setTimeout(resolve, 100));

function verifyWitness(row, key, owner) {
  assert.ok(Number.isInteger(row.pid) && row.pid > 0); assert.notEqual(row.pid, owner);
  assert.equal(row.state, 'active'); assert.equal(row.wait_event_type, 'Lock');
  assert.equal(row.wait_event, 'advisory'); assert.deepEqual(row.blockers, [owner]);
  assert.equal(row.query, 'COPY (SELECT public.t44a_held_gate(' + key +
    ') AS zero) TO STDOUT (FORMAT "binary");', 'Not the exact executing row-query gate');
}

async function runHeldReader({api, editor, projectUuid, identity, safe}) {
  const binding = JSON.parse(fs.readFileSync('/work/held-binding.json', 'utf8'));
  const evidence = {status: 'FAIL', binding_sha256: hash(fs.readFileSync('/work/held-binding.json'))};
  const pg = new (projectRequire('pg').Client)({host: '127.0.0.1', port: 5432,
    user: 'postgres', database: 'probe', password: secret().pg});
  let locked = false;
  const query = {exploreName: 'held_orders', dimensions: [],
    metrics: ['held_orders_total_amount', 'held_orders_snapshot_marker'], filters: {},
    limit: 100, sorts: [], tableCalculations: [], additionalMetrics: []};
  const base = '/api/v2/projects/' + projectUuid + '/query/';
  async function submit() {
    const compiled = await api(editor, 'POST', '/api/v1/projects/' + projectUuid +
      '/explores/held_orders/compileQuery', query);
    assert.equal(typeof compiled.query, 'string');
    assert.ok(!compiled.compilationErrors?.length);
    assert.match(compiled.query, /held_orders/);
    safe(compiled.query);
    const job = await api(editor, 'POST', base + 'metric-query',
      {query, context: 'exploreView', invalidateCache: true});
    assert.match(job.queryUuid, /^[0-9a-f-]{36}$/);
    return {queryUuid: job.queryUuid, compiledSQL: compiled.query, sql_sha256: hash(compiled.query)};
  }
  async function complete(job, total, snapshot) {
    for (let n = 0; n < 900; n++) {
      const result = await api(editor, 'GET', base + job.queryUuid);
      if (result.status === 'ready') {
        assert.equal(result.rows.length, 1);
        assert.equal(Number(result.rows[0].held_orders_total_amount.value.raw), total);
        assert.equal(Number(result.rows[0].held_orders_snapshot_marker.value.raw), snapshot);
        return {...job, total, snapshot, result, completed_monotonic_ns: process.hrtime.bigint().toString()};
      }
      assert.ok(!['error', 'expired', 'cancelled'].includes(result.status), 'Held query failed');
      await pause();
    }
    assert.fail('Held query completion deadline');
  }
  try {
    await pg.connect();
    await pg.query('BEGIN');
    await pg.query('SET LOCAL statement_timeout = 90000');
    const owner = (await pg.query('SELECT pg_backend_pid() AS pid')).rows[0].pid;
    assert.ok(Number.isInteger(binding.key) && binding.key > 0 && binding.key < 2 ** 31);
    await pg.query('SELECT pg_advisory_xact_lock($1::bigint)', [binding.key]);
    locked = true;
    evidence.lock = {owner_pid: owner, key: binding.key};
    const before = await api(editor, 'GET', '/api/v1/user');
    assert.equal(before.userUuid, identity.userUuid); assert.equal(before.role, 'member');
    const held = await submit();
    evidence.heldSubmitted = held;
    let witness;
    for (let n = 0; n < 900; n++) {
      await pg.query('SELECT pg_stat_clear_snapshot()');
      const rows = (await pg.query(
        "SELECT a.pid,a.state,a.wait_event_type,a.wait_event,a.query," +
        "pg_blocking_pids(a.pid) AS blockers FROM pg_locks l JOIN pg_stat_activity a USING(pid) " +
        "WHERE l.locktype='advisory' AND l.classid=0 AND l.objid=$1::oid " +
        "AND l.objsubid=1 AND NOT l.granted", [binding.key])).rows;
      assert.ok(rows.length <= 1, 'Ambiguous native query admission');
      if (rows.length) {
        const row = rows[0]; safe(row.query);
        verifyWitness(row, binding.key, owner);
        witness = {...row, key: binding.key, queryUuid: held.queryUuid,
          sql_sha256: held.sql_sha256, observed_monotonic_ns: process.hrtime.bigint().toString()};
        break;
      }
      const pending = await api(editor, 'GET', base + held.queryUuid);
      assert.ok(!['ready', 'error', 'expired', 'cancelled'].includes(pending.status),
        'No executing hold: completed, pruned, failed or cached query');
      await pause();
    }
    assert.ok(witness, 'No unambiguous executing native COPY gate; do not commit');
    evidence.executionWitness = witness;
    const commit = spawnSync('/work/venv/bin/python', ['/scripts/held_reader.py', 'commit'],
      {encoding: 'utf8', timeout: 90000, maxBuffer: 2 * 1024 ** 2,
        env: {...process.env, PGPASSFILE: '/secrets/pgpass-python'}});
    safe((commit.stdout || '') + (commit.stderr || ''));
    evidence.writer = {status: commit.status, stdout: commit.stdout, stderr: commit.stderr,
      finished_monotonic_ns: process.hrtime.bigint().toString()};
    assert.equal(commit.status, 0, 'Coherent native writer failed');
    const native = JSON.parse(fs.readFileSync('/work/held-native.json', 'utf8'));
    assert.equal(native.status, 'PASS_COHERENT_NATIVE_COMMIT');
    assert.equal(native.before.snapshot, binding.before.snapshot);
    // Recheck exact waiter AFTER verified commit and BEFORE releasing the lock.
    const stillWaiting = (await pg.query(
      "SELECT pid FROM pg_locks WHERE locktype='advisory' AND classid=0 " +
      "AND objid=$1::oid AND objsubid=1 AND NOT granted", [binding.key])).rows;
    assert.deepEqual(stillWaiting, [{pid: witness.pid}]);
    await pg.query('COMMIT'); locked = false;
    evidence.released_monotonic_ns = process.hrtime.bigint().toString();
    evidence.held = await complete(held, 350, binding.before.snapshot);
    evidence.fresh = await complete(await submit(), 360, native.after.snapshot);
    assert.notEqual(evidence.held.queryUuid, evidence.fresh.queryUuid);
    assert.equal(evidence.held.sql_sha256, evidence.fresh.sql_sha256);
    const after = await api(editor, 'GET', '/api/v1/user');
    assert.equal(after.userUuid, identity.userUuid); assert.equal(after.role, 'member');
    evidence.actor = {userUuid: identity.userUuid, role: 'member'};
    evidence.status = 'PASS_HELD_350_FRESH_360_FULL_APP';
  } catch (error) {
    safe(String(error)); evidence.error = String(error); throw error;
  } finally {
    try { if (locked) await pg.query('ROLLBACK'); } finally {
      await pg.end();
      const text = JSON.stringify(evidence, null, 2); safe(text);
      assert.ok(Buffer.byteLength(text) <= 2 * 1024 ** 2);
      fs.writeFileSync('/work/held-reader.json', text + '\n');
    }
  }
}
module.exports = {runHeldReader, verifyWitness};
