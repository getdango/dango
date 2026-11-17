// tests/integration/t44a_qualification/engine_client.cjs
// Actual compiled warehouse clients; isolated S3 inventory/read calls are distinct sessions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');
const {secret, projectRequire} = require('./app_runtime_smoke.cjs');
const cases = JSON.parse(fs.readFileSync('/scripts/engine-cases.json', 'utf8'));
const pins = JSON.parse(fs.readFileSync('/scripts/pins.json', 'utf8'));
const s = secret();
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function safe(value) {
  assert.ok(!Object.values(s).some(v => value.includes(v)), 'Raw engine secret leak');
  return value;
}
function errorMatches(test, error) {
  const text = safe(String(error));
  assert.ok(text.includes(test.error.native_type + ' Error:'));
  assert.ok(text.includes(test.error.message_contains), 'Wrong engine refusal layer');
  return {type: error.constructor.name, message: text};
}
function rows(result, expected) {
  assert.equal(result.rows.length, expected.length);
  return result.rows.map((row, i) => {
    const values = Object.values(row); assert.equal(values.length, expected[i].length);
    return values.map((v, j) => {
      if (typeof expected[i][j] === 'number' && typeof v === 'string') {
        assert.match(v, /^-?\d+(?:\.\d+)?$/); v = Number(v);
      }
      assert.equal(v, expected[i][j], 'Independent selected-client answer');
      return v;
    });
  });
}
function authError(error) {
  const text = safe(String(error));
  assert.match(text, /http/i); assert.match(text, /403/);
  assert.match(text, /synthetic-readiness/);
  assert.match(text, /Forbidden|SignatureDoesNotMatch|AccessDenied/);
  return {type: error.constructor.name, message: text};
}
function stage() {
  const root = '/work/app/project/packages/warehouses';
  const manifest = path.join(root, 'package.json');
  const entry = path.resolve(root, JSON.parse(fs.readFileSync(manifest)).main);
  const req = createRequire(manifest);
  const apiEntry = req.resolve('@duckdb/node-api');
  const apiRequire = createRequire(apiEntry);
  const bindingsEntry = apiRequire.resolve('@duckdb/node-bindings');
  const bindingsRequire = createRequire(bindingsEntry);
  for (const [entry, name] of [[apiEntry, '@duckdb/node-api'], [bindingsEntry, '@duckdb/node-bindings']]) {
    let directory = path.dirname(entry), found = false;
    for (let n = 0; n < 4; n++, directory = path.dirname(directory)) {
      const file = path.join(directory, 'package.json');
      if (!fs.existsSync(file)) continue;
      const pkg = JSON.parse(fs.readFileSync(file));
      if (pkg.name !== name) continue;
      assert.equal(pkg.version, '2.0.0-r.1'); found = true; break;
    }
    assert.ok(found, 'Missing selected API/bindings package identity');
  }
  const native = path.dirname(bindingsRequire.resolve('@duckdb/node-bindings-linux-x64'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(native, 'package.json'))).version, '2.0.0-r.1');
  const inventory = JSON.parse(fs.readFileSync('/work/engine-package-inventory.json', 'utf8'));
  const pin = inventory.packages.find(p => p.package === '@duckdb/node-bindings-linux-x64@2.0.0-r.1');
  assert.ok(pin);
  for (const file of ['duckdb.node', 'libduckdb.so']) {
    assert.equal(hash(fs.readFileSync(path.join(native, file))), pin.native['package/' + file].sha256);
  }
  // Driver identity is checked before loading it. Its version determines the unchanged upstream bundle path.
  const api = req('@duckdb/node-api');
  assert.match(api.version(), /^v2\.0\.0(?:$|\s)/);
  const directory = path.join(path.dirname(entry), 'duckdbExtensions', api.version());
  fs.mkdirSync(directory, {recursive: true});
  const bundled = path.join(directory, 'httpfs.duckdb_extension');
  const home = '/home/probe/.duckdb/extensions/v2.0.0/linux_amd64/httpfs.duckdb_extension';
  const bytes = fs.readFileSync(home); assert.equal(hash(bytes), pins.extensions.httpfs.expanded_sha256);
  assert.ok(!fs.existsSync(bundled), 'Fresh exact bundled extension required');
  fs.writeFileSync(bundled, bytes, {mode: 0o444});
  assert.equal(hash(fs.readFileSync(bundled)), pins.extensions.httpfs.expanded_sha256);
  const {DuckdbWarehouseClient} = require(entry);
  assert.equal(typeof DuckdbWarehouseClient, 'function');
  return {DuckdbWarehouseClient, identity: {engine: api.version(), package: '2.0.0-r.1',
    native: pin.native, httpfs_sha256: hash(bytes), bundled_path: bundled}};
}
const options = {resourceLimits: {memoryLimit: '128MB', threads: 1},
  sharedResourceLimits: {memoryLimit: '128MB', threads: 1}, enableInstanceCache: false};
async function extensions(client, names) {
  const result = await client.runQuery('SELECT extension_name,loaded,installed,extension_version,install_mode ' +
    'FROM duckdb_extensions() ORDER BY extension_name');
  const selected = result.rows.filter(r => names.includes(r.extension_name));
  assert.equal(selected.length, names.length);
  for (const row of selected) {
    assert.equal(row.loaded, true); assert.equal(row.installed, true);
    if (pins.extensions[row.extension_name]) assert.equal(row.extension_version, pins.extensions[row.extension_name].revision);
    else { assert.equal(row.extension_name, 'icu'); assert.equal(row.install_mode, 'STATICALLY_LINKED'); }
  }
  return {session: 'Separate runQuery; not the blob-read session', rows: selected};
}
async function native() {
  const {DuckdbWarehouseClient, identity} = stage();
  const report = {status: 'FAIL', identity, core: [], s3: []};
  try {
    const client = new DuckdbWarehouseClient({type: 'duckdb', connectionType: 'ducklake',
      catalog: {type: 'postgres', host: '127.0.0.1', port: 5432, database: 'probe', user: 'postgres', password: s.pg},
      dataPath: {type: 'local', path: '/fixture/data'}, catalogAlias: 'lake', schema: 'sales', threads: 1}, options);
    try {
      for (const test of cases.cases) {
        const row = {id: test.id, sql: test.sql}; report.core.push(row);
        let result;
        try { result = await client.runQuery(test.sql); }
        catch (error) { assert.ok(test.error); row.native_error = errorMatches(test, error); }
        if (result) { assert.ok(test.expected, 'Required engine refusal instead returned rows'); row.actual = rows(result, test.expected); }
        row.status = 'PASS';
      }
      report.local_extensions = await extensions(client, ['ducklake', 'postgres_scanner', 'icu']);
    } finally { await client.close(); }
    for (const [label, field] of [['correct_before', 's3Secret'], ['wrong_secret', 'wrongS3'], ['correct_after', 's3Secret']]) {
      const client = DuckdbWarehouseClient.createForPreAggregate({type: 'duckdb_s3', s3Config: {
        authMode: 'default', endpoint: '127.0.0.1:3900', region: 'garage', accessKey: s.s3Access,
        secretKey: s[field], forcePathStyle: true, useSsl: false, scope: [cases.s3.uri]}}, options);
      const row = {stage: label, sql: cases.s3.sql, instance_contract: 'No cache key; every runQuery uses its own normal isolated session'};
      report.s3.push(row);
      try {
        row.extension_inventory = await extensions(client, ['httpfs']);
        let result;
        try { result = await client.runQuery(cases.s3.sql); }
        catch (error) {
          assert.equal(label, 'wrong_secret'); row.native_error = authError(error);
        }
        if (result) { assert.notEqual(label, 'wrong_secret'); row.actual = rows(result, [[cases.s3.sha256, cases.s3.bytes]]); }
        row.status = 'PASS';
      } finally { await client.close(); }
    }
    report.status = 'PASS_SELECTED_LIGHTDASH_ENGINE_AND_HTTPFS_CASES';
  } finally {
    const data = safe(JSON.stringify(report, null, 2) + '\n');
    assert.ok(Buffer.byteLength(data) <= cases.limits.report);
    fs.writeFileSync('/work/engine-node.json', data);
  }
}
async function object(mode) {
  const {S3Client, PutObjectCommand, DeleteObjectCommand} = projectRequire('@aws-sdk/client-s3');
  const client = new S3Client({endpoint: 'http://127.0.0.1:3900', region: 'garage', forcePathStyle: true,
    maxAttempts: 1, credentials: {accessKeyId: s.s3Access, secretAccessKey: s.s3Secret}});
  const input = {Bucket: 't44a-results', Key: 'synthetic-readiness'};
  const body = Buffer.from(cases.s3.body); assert.equal(hash(body), cases.s3.sha256);
  try {
    for (let n = 0; ; n++) {
      try {
        const result = await client.send(mode === 'seed' ? new PutObjectCommand({...input, Body: body}) : new DeleteObjectCommand(input));
        assert.equal(result.$metadata.httpStatusCode, mode === 'seed' ? 200 : 204); break;
      } catch (error) {
        if (!['ECONNREFUSED', 'ECONNRESET'].includes(error.code) || n >= 89) throw error;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    }
    fs.writeFileSync('/work/engine-object-' + mode + '.json', safe(JSON.stringify({status: 'PASS', mode,
      uri: cases.s3.uri, body_sha256: cases.s3.sha256, bytes: body.length})) + '\n');
  } finally { client.destroy(); }
}
if (require.main === module) {
  const mode = process.argv[2];
  (mode === 'native' ? native() : ['seed', 'delete'].includes(mode) ? object(mode) : Promise.reject(new Error('Invalid engine mode')))
    .catch(error => {
      const raw = String(error); const leaked = Object.values(s).some(v => raw.includes(v));
      console.error(JSON.stringify({status: 'FAIL', raw_secret_leak: leaked,
        error: leaked ? 'Raw engine secret exposure refused' : raw})); process.exitCode = 1;
    });
}
