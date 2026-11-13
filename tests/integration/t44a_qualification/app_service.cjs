// tests/integration/t44a_qualification/app_service.cjs
// Real signed S3 operations precede normal migration and production app startup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {projectRequire, secret} = require('./app_runtime_smoke.cjs');
const {S3Client, PutObjectCommand, GetObjectCommand, DeleteObjectCommand} = projectRequire('@aws-sdk/client-s3');

async function main() {
  const s = secret();
  async function normal(path, args, env, cwd) {
    const child = spawn(path, args, {env, cwd, stdio: 'inherit'});
    const signal = () => child.kill('SIGTERM'); process.once('SIGTERM', signal);
    try { await new Promise((resolve, reject) => {
      child.once('error', reject); child.once('exit', code => code === 0 ? resolve() : reject(new Error('Normal service process failed')));
    }); } finally { process.removeListener('SIGTERM', signal); }
  }
  if (process.argv[2] === 'garage') {
    await normal('/tools/garage', ['-c', '/scripts/garage.toml', 'server', '--single-node', '--default-bucket'],
      {...process.env, GARAGE_DEFAULT_ACCESS_KEY: s.s3Access, GARAGE_DEFAULT_SECRET_KEY: s.s3Secret,
        RUST_LOG: 'netapp=info,garage=info,garage::server=warn,garage_api_common::generic_server=warn',
        GARAGE_DEFAULT_BUCKET: 't44a-results'}, '/work');
    return;
  }
  const canvas = JSON.parse(fs.readFileSync('/scripts/canvas-prebuild.json', 'utf8'));
  const canvasDir = path.dirname(projectRequire.resolve('canvas/package.json'));
  assert.equal(projectRequire('canvas/package.json').version, '3.2.1');
  assert.ok(!fs.existsSync(path.join(canvasDir, 'build')), 'Canvas installation must be fresh');
  const installerDir = path.resolve(canvasDir, '../prebuild-install');
  assert.equal(require(path.join(installerDir, 'package.json')).version, '7.1.3');
  for (const [name, expected] of Object.entries(canvas.installer.file_sha256)) {
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(installerDir, name))).digest('hex'), expected);
  }
  const archive = '/work/canvas-prebuild/' + canvas.file;
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex'), canvas.sha256);
  await normal(process.execPath, [path.join(installerDir, 'bin.js'), '-r', 'napi',
    '--local-prebuilds', '/work/canvas-prebuild'], {...process.env}, canvasDir);
  const installed = fs.readdirSync(path.join(canvasDir, 'build'), {recursive: true})
    .map(name => 'build/' + name).filter(name => fs.statSync(path.join(canvasDir, name)).isFile()).sort();
  assert.deepEqual(installed, Object.keys(canvas.members).sort());
  for (const [name, identity] of Object.entries(canvas.members)) {
    const file = path.join(canvasDir, name);
    assert.equal(fs.statSync(file).mode & 0o777, identity.mode);
    const data = fs.readFileSync(file);
    assert.equal(data.length, identity.bytes);
    assert.equal(crypto.createHash('sha256').update(data).digest('hex'), identity.sha256);
  }
  const png = projectRequire('canvas').createCanvas(1, 1).toBuffer('image/png');
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  console.log('PASS_FROZEN_CANVAS_PREBUILD_OFFLINE_INSTALL_LOAD_AND_PNG');
  const opts = {endpoint: 'http://127.0.0.1:3900', region: 'garage', forcePathStyle: true,
    maxAttempts: 1, credentials: {accessKeyId: s.s3Access, secretAccessKey: s.s3Secret}};
  const client = new S3Client(opts);
  const input = {Bucket: 't44a-results', Key: 'synthetic-readiness'};
  const body = Buffer.from('t44a-owned-storage-readiness\n');
  let ready = false;
  for (let n = 0; n < 90; n++) {
    try { await client.send(new PutObjectCommand({...input, Body: body})); ready = true; break; }
    catch (error) {
      if (!['ECONNREFUSED', 'ECONNRESET'].includes(error.code)) throw error;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  assert.ok(ready, 'Garage readiness deadline');
  const get = await client.send(new GetObjectCommand(input));
  const beforeBody = Buffer.from(await get.Body.transformToByteArray());
  assert.equal(get.$metadata.httpStatusCode, 200); assert.deepEqual(beforeBody, body);
  const hash = data => crypto.createHash('sha256').update(data).digest('hex');
  const reviewerS3Probe = {correctBefore: {status: get.$metadata.httpStatusCode, body_sha256: hash(beforeBody)}};
  assert.notEqual(s.wrongS3, s.s3Secret);
  const wrong = new S3Client({...opts, credentials: {...opts.credentials, secretAccessKey: s.wrongS3}});
  try { await wrong.send(new GetObjectCommand(input)); assert.fail('Wrong S3 secret accepted'); }
  catch (error) {
    assert.equal(error.$metadata?.httpStatusCode, 403);
    assert.ok(['SignatureDoesNotMatch', 'AccessDenied'].includes(error.name));
    reviewerS3Probe.wrongSecret = {status: error.$metadata.httpStatusCode, error: error.name};
  } finally { wrong.destroy(); }
  const again = await client.send(new GetObjectCommand(input));
  const afterBody = Buffer.from(await again.Body.transformToByteArray());
  assert.equal(again.$metadata.httpStatusCode, 200); assert.deepEqual(afterBody, beforeBody);
  reviewerS3Probe.correctAfter = {status: again.$metadata.httpStatusCode, body_sha256: hash(afterBody)};
  console.log(JSON.stringify({reviewerS3Probe}));
  await client.send(new DeleteObjectCommand(input)); client.destroy();
  console.log('PASS_GARAGE_AUTHENTICATED_PUT_GET_AND_BAD_SECRET_403');
  const env = {...process.env, NODE_ENV: 'production', PORT: '8080', SITE_URL: 'http://127.0.0.1:8080',
    LIGHTDASH_SECRET: s.appSecret,
    PGCONNECTIONURI: 'postgresql://postgres:' + s.pg + '@127.0.0.1:5432/lightdash',
    S3_ENDPOINT: 'http://127.0.0.1:3900', S3_BUCKET: 't44a-results', S3_REGION: 'garage',
    S3_AUTH_MODE: 'default', S3_USE_CREDENTIALS_FROM: 'env', S3_FORCE_PATH_STYLE: 'true',
    S3_ACCESS_KEY: s.s3Access, S3_SECRET_KEY: s.s3Secret,
    RESULTS_S3_ENDPOINT: 'http://127.0.0.1:3900', NATS_ENABLED: 'false', SCHEDULER_ENABLED: 'false',
    SECURE_COOKIES: 'false', HEADLESS: 'true'};
  await normal(process.execPath, ['/work/app/project/packages/backend/dist/scripts/migrate/index.js'], env, '/work/app/project/packages/backend');
  console.log('PASS_NORMAL_PRODUCTION_MIGRATIONS');
  await normal(process.execPath, ['/work/app/project/packages/backend/dist/index.js'], env, '/work/app/project/packages/backend');
}
main().catch(error => {
  const message = String(error);
  const values = Object.values(secret());
  const leaked = values.some(v => message.includes(v));
  console.error(JSON.stringify({service_error: values.reduce((s, v) => s.split(v).join('[REDACTED]'), message),
    raw_secret_leak: leaked}));
  process.exitCode = 1;
});
