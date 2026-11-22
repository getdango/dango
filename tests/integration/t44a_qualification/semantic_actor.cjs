// tests/integration/t44a_qualification/semantic_actor.cjs
// Native ordinary-actor jobs compared with fixed independent fixture expectations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function field(name) {
  return name.includes('.') ? name.replace('.', '_') : 'semantic_events_' + name;
}
function normalize(value, name) {
  if (name.endsWith('_date_day')) {
    assert.equal(typeof value, 'string', 'Expected actual DATE serialization');
    assert.match(value, /^\d{4}-\d{2}-\d{2}(?:T00:00:00(?:\.000)?Z)?$/);
    return value.slice(0, 10);
  }
  const numeric = new Set(['id', 'amount', 'total_amount', 'total_units', 'ratio', 'row_count',
    'amount_count', 'min_amount', 'max_amount', 'average_amount', 'customers',
    'new_york_hour', 'new_york_offset', 'semantic_line_totals.total_line_value']);
  if (value === null) return value;
  if (numeric.has(name) && typeof value === 'string') {
    assert.match(value, /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/, 'Invalid native numeric encoding');
    value = Number(value);
  }
  if (!numeric.has(name) && typeof value === 'string') return value;
  assert.ok(typeof value === 'number' && Number.isFinite(value), 'Nonfinite/unexpected native value');
  return value;
}
function checkRows(actual, expected) {
  const key = row => JSON.stringify(row);
  actual = [...actual].sort((a, b) => key(a).localeCompare(key(b)));
  expected = [...expected].sort((a, b) => key(a).localeCompare(key(b)));
  assert.equal(actual.length, expected.length, 'Semantic row cardinality');
  for (let row = 0; row < actual.length; row++) {
    assert.equal(actual[row].length, expected[row].length);
    for (let col = 0; col < actual[row].length; col++) {
      const a = actual[row][col], e = expected[row][col];
      if (typeof e === 'number') {
        assert.equal(typeof a, 'number', 'Numeric result must not be null/string/boolean');
        assert.ok(Math.abs(a - e) <= 1e-10 * Math.max(1, Math.abs(e)), 'Independent numeric oracle mismatch');
      } else assert.equal(a, e, 'Independent exact/null/calendar oracle mismatch');
    }
  }
}
async function runSemantics({api, editor, projectUuid, identity, safe}) {
  const bytes = fs.readFileSync('/work/semantic-fixture.json'); safe(bytes.toString());
  const fixture = JSON.parse(bytes);
  assert.equal(fixture.status, 'PASS_REAL_SEMANTIC_FIXTURE_AND_BAD_KEY_REFUSALS');
  assert.equal(fixture.cases.length, 15);
  const report = {status: 'FAIL', fixture_sha256: hash(bytes), actor: identity.userUuid,
    projectUuid, calendarPolicy: fixture.calendar_policy, queries: []};
  try {
    const before = await api(editor, 'GET', '/api/v1/user');
    assert.equal(before.userUuid, identity.userUuid); assert.equal(before.role, 'member');
    for (const test of fixture.cases) {
      const fields = [...test.dimensions, ...test.metrics];
      const query = {exploreName: 'semantic_events', dimensions: test.dimensions.map(field),
        metrics: test.metrics.map(field), filters: {}, limit: 100, sorts: [],
        tableCalculations: [], additionalMetrics: []};
      if (test.filters.length) query.filters.dimensions = {id: crypto.randomUUID(), and:
        test.filters.map(([name, operator, values]) => ({id: crypto.randomUUID(),
          target: {fieldId: field(name)}, operator, values}))};
      const compiled = await api(editor, 'POST', '/api/v1/projects/' + projectUuid +
        '/explores/semantic_events/compileQuery', query);
      assert.equal(typeof compiled.query, 'string'); assert.ok(!compiled.compilationErrors?.length);
      assert.match(compiled.query, /semantic_events/); safe(compiled.query);
      const job = await api(editor, 'POST', '/api/v2/projects/' + projectUuid + '/query/metric-query',
        {query, context: 'exploreView', invalidateCache: true});
      assert.match(job.queryUuid, /^[0-9a-f-]{36}$/);
      const receipt = {name: test.name, actorUuid: identity.userUuid, projectUuid,
        queryUuid: job.queryUuid, metricQuery: query, compiledSQL: compiled.query,
        sql_sha256: hash(compiled.query), expected: test.expected};
      report.queries.push(receipt);
      let ready;
      for (let n = 0; n < 180; n++) {
        const result = await api(editor, 'GET', '/api/v2/projects/' + projectUuid + '/query/' + job.queryUuid);
        if (result.status === 'ready') { ready = result; break; }
        assert.ok(!['error', 'expired', 'cancelled'].includes(result.status),
          'Native semantic job failed: ' + test.name + ' ' + result.status);
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      assert.ok(ready, 'No completed semantic query: ' + test.name);
      receipt.result = ready;
      receipt.actual = ready.rows.map(row => fields.map(name => {
        const cell = row[field(name)]; assert.ok(cell && cell.value && Object.hasOwn(cell.value, 'raw'));
        return normalize(cell.value.raw, name);
      }));
      checkRows(receipt.actual, test.expected);
      receipt.status = 'PASS_NATIVE_JOB_AND_INDEPENDENT_ORACLE';
    }
    assert.equal(new Set(report.queries.map(q => q.queryUuid)).size, fixture.cases.length);
    const after = await api(editor, 'GET', '/api/v1/user');
    assert.equal(after.userUuid, identity.userUuid); assert.equal(after.role, 'member');
    report.status = 'PASS_SEMANTIC_MATRIX_FULL_APP';
    return report;
  } catch (error) {
    safe(String(error)); report.error = String(error); throw error;
  } finally {
    const data = JSON.stringify(report, null, 2); safe(data);
    assert.ok(Buffer.byteLength(data) <= 8 * 1024 ** 2, 'Semantic result report bound');
    fs.writeFileSync('/work/semantic-result.json', data + '\n');
  }
}
module.exports = {runSemantics, checkRows, normalize, field};
