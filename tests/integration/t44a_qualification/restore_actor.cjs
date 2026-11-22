// tests/integration/t44a_qualification/restore_actor.cjs
// Quiescent object/BI state and fresh restored actors; values/cookies stay private.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {projectRequire, secret} = require('./app_runtime_smoke.cjs');
const {S3Client, ListObjectsV2Command, PutObjectCommand, GetObjectCommand} = projectRequire('@aws-sdk/client-s3');
const s = secret(); const privateValues = Object.values(s);
const hash = b => crypto.createHash('sha256').update(b).digest('hex');
const mode = process.argv[2];
const baseline = JSON.parse(fs.readFileSync('/work/app-slice.json', 'utf8'));
assert.equal(baseline.status, 'PASS_RESTORE_ORIGINAL_350');
function safe(text) { assert.ok(!privateValues.some(v => text.includes(v)), 'Raw restore credential exposure'); return text; }
function save(name, value) {
  const text = safe(JSON.stringify(value, null, 2)); assert.ok(Buffer.byteLength(text) <= 8 * 1024 ** 2);
  fs.writeFileSync('/work/' + name, text + '\n');
}
async function state() {
  const pg = new (projectRequire('pg').Client)({host: '127.0.0.1', user: 'postgres', database: 'lightdash', password: s.pg});
  await pg.connect();
  const tables = {}; const credentials = {};
  try {
    const names = await pg.query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename");
    for (const {tablename} of names.rows) {
      const quoted = '"' + tablename.replaceAll('"', '""') + '"';
      const rows = await pg.query('SELECT row_to_json(t)::text AS row FROM public.' + quoted + ' t');
      tables[tablename] = hash(JSON.stringify(rows.rows.map(r => r.row).sort()));
    }
    const namesWithSecrets = await pg.query("SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='encrypted_credentials' ORDER BY table_name");
    assert.ok(namesWithSecrets.rows.length);
    for (const {table_name} of namesWithSecrets.rows) credentials[table_name] = tables[table_name];
    const dbs = await pg.query("SELECT datname,oid::text FROM pg_database WHERE datname IN ('probe','lightdash') ORDER BY datname");
    const rights = {};
    for (const name of ['organization_memberships', 'project_memberships', 'space_user_access',
      'space_group_access', 'project_group_access', 'spaces', 'knex_migrations']) {
      assert.ok(tables[name], 'Missing protected BI table'); rights[name] = tables[name];
    }
    return {tables, credentials, rights, dbs: dbs.rows};
  } finally { await pg.end(); }
}
function native() {
  const r = spawnSync('/work/venv/bin/python', ['/scripts/restore_native.py'], {encoding: 'utf8', timeout: 90000,
    maxBuffer: 2 * 1024 ** 2, env: {...process.env, PGPASSFILE: '/secrets/pgpass-python'}});
  safe((r.stdout || '') + (r.stderr || ''));
  assert.equal(r.status, 0, 'Native restore identity failed: ' + (r.stderr || 'No child stderr captured'));
  return JSON.parse(r.stdout);
}
async function objects(original) {
  const options = {endpoint: 'http://127.0.0.1:3900', region: 'garage', forcePathStyle: true, maxAttempts: 1,
    credentials: {accessKeyId: s.s3Access, secretAccessKey: s.s3Secret}};
  const client = new S3Client(options); const Bucket = 't44a-results';
  const control = {Bucket, Key: 'restore-control'}; const body = Buffer.from('owned-complete-restore-350\n');
  try {
    if (original) await client.send(new PutObjectCommand({...control, Body: body}));
    else for (let n = 0; ; n++) {
      try { const ready = await client.send(new GetObjectCommand(control));
        assert.equal(hash(Buffer.from(await ready.Body.transformToByteArray())), hash(body)); break; }
      catch (error) { if (!['ECONNREFUSED', 'ECONNRESET'].includes(error.code) || n >= 90) throw error;
        await new Promise(resolve => setTimeout(resolve, 500)); }
    }
    const rows = {}; let token;
    do {
      const list = await client.send(new ListObjectsV2Command({Bucket, ContinuationToken: token}));
      for (const obj of list.Contents || []) {
        assert.ok(Object.keys(rows).length < 1024);
        assert.ok(Number.isSafeInteger(obj.Size) && obj.Size >= 0 && obj.Size <= 8 * 1024 ** 2);
        assert.ok(Object.values(rows).reduce((n, r) => n + r.bytes, obj.Size) <= 32 * 1024 ** 2);
        assert.ok(!rows[obj.Key]);
        const result = await client.send(new GetObjectCommand({Bucket, Key: obj.Key}));
        assert.equal(result.$metadata.httpStatusCode, 200); assert.equal(result.ContentLength, obj.Size);
        const bytes = Buffer.from(await result.Body.transformToByteArray());
        assert.equal(bytes.length, obj.Size); rows[obj.Key] = {bytes: bytes.length, sha256: hash(bytes)};
      }
      if (list.IsTruncated) assert.ok(list.NextContinuationToken && list.NextContinuationToken !== token);
      token = list.IsTruncated ? list.NextContinuationToken : undefined;
    } while (token);
    assert.ok(Object.keys(rows).length > 1, 'Real app result objects missing');
    assert.deepEqual(rows[control.Key], {bytes: body.length, sha256: hash(body)});
    const wrong = new S3Client({...options, credentials: {...options.credentials, secretAccessKey: s.wrongS3}});
    let denial;
    try { await wrong.send(new GetObjectCommand(control)); assert.fail('Wrong restored S3 secret accepted'); }
    catch (e) { assert.equal(e.$metadata?.httpStatusCode, 403); assert.ok(['AccessDenied', 'SignatureDoesNotMatch'].includes(e.name)); denial = {status: 403, name: e.name}; }
    finally { wrong.destroy(); }
    const recovery = await client.send(new GetObjectCommand(control));
    assert.equal(recovery.$metadata.httpStatusCode, 200);
    assert.equal(hash(Buffer.from(await recovery.Body.transformToByteArray())), hash(body));
    return {objects: rows, wrong: denial, recovered_hash: hash(body)};
  } finally { client.destroy(); }
}
async function actors() {
  const requests = []; const queries = [];
  async function api(jar, method, path, body, status = 200) {
    const response = await fetch('http://127.0.0.1:8080' + path, {method, headers: {'Content-Type': 'application/json', Origin: 'http://127.0.0.1:8080', Cookie: jar.cookie || ''},
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(90000)});
    const cookies = response.headers.getSetCookie();
    if (cookies.length) { jar.cookie = cookies.map(c => c.split(';', 1)[0]).join('; '); privateValues.push(jar.cookie, ...cookies.map(c => c.split(';', 1)[0].split('=').slice(1).join('='))); }
    const text = await response.text(); requests.push({method, path, status: response.status});
    assert.equal(response.status, status, 'Restored request failed: ' + safe(text));
    const parsed = JSON.parse(text);
    if (status === 403) { safe(text); assert.equal(parsed.error.name, 'ForbiddenError'); assert.equal(parsed.error.statusCode, 403); return {status, error: parsed.error.name}; }
    return parsed.results;
  }
  const id = baseline.restoreIdentity; const owner = {}; const editor = {};
  for (const [jar, email, password, uuid, role] of [[owner, 'owner@t44a.invalid', s.ownerPassword, id.ownerUuid, 'admin'], [editor, 'editor@t44a.invalid', s.editorPassword, id.editorUuid, 'member']]) {
    await api(jar, 'POST', '/api/v1/login', {email, password});
    const identity = await api(jar, 'GET', '/api/v1/user'); assert.equal(identity.userUuid, uuid); assert.equal(identity.role, role);
  }
  for (const [filters, expected] of [[{}, 350], [{dimensions: {id: crypto.randomUUID(), and: [
    {id: crypto.randomUUID(), target: {fieldId: 'orders_order_date_day'}, operator: 'greaterThanOrEqual', values: ['2026-09-01']},
    {id: crypto.randomUUID(), target: {fieldId: 'orders_order_date_day'}, operator: 'lessThan', values: ['2026-10-01']}]}}, 300]]) {
    const query = {...id.metricQuery, filters};
    const compiled = await api(editor, 'POST', '/api/v1/projects/' + id.projectUuid + '/explores/orders/compileQuery', query);
    assert.equal(typeof compiled.query, 'string'); assert.ok(!compiled.compilationErrors?.length);
    const submitted = await api(editor, 'POST', '/api/v2/projects/' + id.projectUuid + '/query/metric-query', {query, context: 'exploreView', invalidateCache: true});
    assert.ok(!baseline.queries.some(q => q.queryUuid === submitted.queryUuid));
    let ready;
    for (let n = 0; n < 180; n++) {
      const r = await api(editor, 'GET', '/api/v2/projects/' + id.projectUuid + '/query/' + submitted.queryUuid);
      if (r.status === 'ready') { ready = r; break; }
      assert.ok(!['error', 'expired', 'cancelled'].includes(r.status)); await new Promise(resolve => setTimeout(resolve, 500));
    }
    assert.equal(ready?.rows.length, 1); assert.equal(Number(ready.rows[0].orders_total_amount.value.raw), expected);
    queries.push({queryUuid: submitted.queryUuid, generatedSQL: compiled.query, result: ready, expected});
  }
  const chartPath = '/api/v1/saved/' + id.chartUuid + '/results';
  const accessible = await api(owner, 'POST', chartPath, {}); assert.equal(Number(accessible.rows[0].orders_total_amount.value.raw), 350);
  const denied = await api(editor, 'POST', chartPath, {}, 403);
  return {identity: id, requests, queries, private: {owner: 350, editor: denied}};
}
async function main() {
  const before = JSON.parse(fs.readFileSync('/work/restore-original.json', 'utf8'));
  if (mode === 'original') {
    save('restore-original.json', {status: 'PASS_QUIESCENT_ORIGINAL_STATE', bi: await state(), native: native(), s3: await objects(true), identity: baseline.restoreIdentity});
  } else if (mode === 'pre-app') {
    assert.deepEqual(await state(), before.bi);
    assert.deepEqual(native(), before.native);
    assert.deepEqual(await objects(false), before.s3);
    save('restore-pre-app.json', {status: 'PASS_RESTORED_BEFORE_APP', bi: before.bi, native: before.native, s3: before.s3});
  } else {
    assert.equal(mode, 'queries');
    for (let n = 0; ; n++) {
      try { const ready = await fetch('http://127.0.0.1:8080/api/v1/health', {signal: AbortSignal.timeout(2000)}); if (ready.ok) break; }
      catch { /* normal migrations and app startup readiness */ }
      assert.ok(n < 180, 'Restored app readiness deadline'); await new Promise(resolve => setTimeout(resolve, 500));
    }
    const frontend = await fetch('http://127.0.0.1:8080/'); assert.equal(frontend.status, 200);
    const html = await frontend.text(); assert.equal(hash(html), baseline.frontend.index_sha256);
    const asset = await fetch('http://127.0.0.1:8080' + baseline.frontend.asset); assert.equal(asset.status, 200);
    assert.equal(hash(Buffer.from(await asset.arrayBuffer())), baseline.frontend.asset_sha256);
    const actual = await actors(); const after = await state();
    assert.deepEqual(after.credentials, before.bi.credentials); assert.deepEqual(after.rights, before.bi.rights);
    assert.deepEqual(native(), before.native);
    save('restore-replacement.json', {status: 'PASS_REPLACEMENT_APP_350_300_PRIVATE_403', ...actual, native: before.native, frontend: baseline.frontend});
  }
}
// Original inventory is created only after app writers have stopped.
if (mode === 'original') fs.writeFileSync('/work/restore-original.json', '{}\n');
main().catch(error => { console.error(safe(String(error))); process.exitCode = 1; });
