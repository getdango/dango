import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import DuckdbWarehouseClient from '/source/packages/warehouses/src/warehouseClients/DuckdbWarehouseClient';
const requireWarehouse = createRequire('/work/deploy/package.json');
const api = requireWarehouse('@duckdb/node-api');
const apiManifest = requireWarehouse('@duckdb/node-api/package.json');
const mode = process.argv[2];
const pins = JSON.parse(fs.readFileSync('/scripts/pins.json', 'utf8'));
const secret = JSON.parse(fs.readFileSync('/secrets/catalog.json', 'utf8'));
const password = mode === 'wrong-credential' ? secret.wrongPassword : secret.readerPassword;
const client = new DuckdbWarehouseClient({
    type: 'duckdb', connectionType: 'ducklake', schema: 'sales',
    catalogAlias: 'probe', threads: 2, requireUserCredentials: false,
    catalog: {type: 'postgres', host: '127.0.0.1', port: 5432,
        database: 'probe', user: 'probe_reader', password},
    dataPath: {type: 'local', path: '/fixture/data'},
} as any, {sharedResourceLimits: {memoryLimit: '1536MB', threads: 2}});
(async () => {
    assert.equal(apiManifest.version, '2.0.0-r.1');
    assert.match(api.version(), /^v?2\./);
    if (mode === 'wrong-credential') {
        let error;
        try {await client.runQuery('SELECT SUM(amount)::INTEGER AS total FROM probe.sales.orders');}
        catch (e) {error = e;}
        assert.ok(error, 'Wrong credential must reject the actual Lightdash query');
        const message = String(error);
        for (const value of [secret.readerPassword, secret.wrongPassword, secret.adminPassword])
            assert.ok(!message.includes(value), 'Error exposed a synthetic secret');
        assert.match(message, /password authentication failed for user "probe_reader"/i);
        console.log(JSON.stringify({mode, status: 'EXPECTED_REFUSAL', error: message, apiVersion: apiManifest.version, engineVersion: api.version()}));
    } else {
        const identity = await client.runQuery("SELECT version() AS engine, current_database() AS database");
        assert.match(String(identity.rows[0].engine), /^v?2\./);
        const extensions = await client.runQuery("SELECT extension_name, extension_version, installed_from FROM duckdb_extensions() WHERE loaded ORDER BY extension_name");
        for (const [name, pin] of Object.entries(pins.extensions) as [string, any][]) {
            const loaded = extensions.rows.find((r: any) => r.extension_name === name);
            assert.ok(loaded, `Missing loaded extension ${name}`);
            assert.ok(String(loaded.extension_version).startsWith(pin.revision), `Extension revision ${name}`);
        }
        const signed = await client.runQuery("SELECT current_setting('allow_unsigned_extensions') AS unsigned");
        assert.equal(signed.rows[0].unsigned, false);
        const result = await client.runQuery('SELECT SUM(amount)::INTEGER AS total FROM probe.sales.orders');
        assert.equal(Number(result.rows[0].total), 350);
        let readonlyError;
        try {await client.runQuery('UPDATE probe.sales.orders SET amount = 999 WHERE id = 1');}
        catch (e) {readonlyError = e;}
        assert.ok(readonlyError, 'Read-only attach must refuse UPDATE');
        const message = String(readonlyError);
        assert.match(message, /read.only|read only|read-only/i);
        for (const value of [secret.readerPassword, secret.wrongPassword, secret.adminPassword])
            assert.ok(!message.includes(value), 'Error exposed a synthetic secret');
        console.log(JSON.stringify({mode, status: 'PASS_CLIENT_ORACLES', apiVersion: apiManifest.version, engineVersion: api.version(), identity, extensions, result, readonlyError: message}));
    }
})().then(() => process.exit(0), error => {
    const message = String(error);
    const containsSecret = [secret.readerPassword, secret.wrongPassword, secret.adminPassword].some(value => message.includes(value));
    console.log(JSON.stringify({mode, status: 'FAIL', message: containsSecret ? '[SECRET EXPOSURE DETECTED; raw withheld]' : message}));
    process.exit(1);
});
