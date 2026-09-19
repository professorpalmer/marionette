const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { validateReceipt, attachBackend } = require('./backend-attach.cjs');
const { isSameCheckoutSuccessor } = require('./backend-identity.cjs');
const receipt = { schema: 1, owner: 'external', port: 12345, pid: 42,
  endpoint_id: 'e', boot_id: 'b', launch_id: 'l', repo_root: '/repo',
  token_file: '/token', environment: {source_sha:'abc', source_digest:'digest'} };

test('attachment requires every identity component and exact environment', () => {
  for (const key of ['endpoint_id', 'boot_id', 'launch_id', 'pid', 'port']) {
    assert.throws(() => validateReceipt(receipt, {...receipt, [key]:'other'}), /identity/);
  }
  assert.throws(() => validateReceipt(receipt, {...receipt, environment:{}}), /environment/);
  assert.doesNotThrow(() => validateReceipt(receipt, receipt));
});

function mainSeam(overrides = {}) {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const ctx = vm.createContext({ process: {env:{MARIONETTE_BACKEND_RECEIPT:'/receipt'}},
    require: name => require(path.resolve(__dirname, name)), backendOwned:false,
    backend:null, backendPort:0, harnessToken:'', attachBackend,
    resolveRepoRoot:()=>'/repo', refreshAllowedLoopbackAliases(){},
    unlinkMarkerIfOwned(){ assert.equal(ctx.backendOwned, false); },
    killBackendTree(){ throw Error('borrowed backend killed'); }, ...overrides });
  vm.runInContext(source.slice(source.indexOf('async function _startBackendOnce()'), source.indexOf('// ---- transport seam')), ctx);
  vm.runInContext(source.slice(source.indexOf('async function cleanupBackend()'), source.indexOf('app.on("window-all-closed"')), ctx);
  return ctx;
}

test('actual main entrypoint attaches and quit preserves borrowed backend', async () => {
  const ctx = mainSeam({attachBackend:async()=>({receipt, token:'test'})});
  await ctx._startBackendOnce();
  assert.equal(ctx.backendPort, receipt.port);
  assert.equal(ctx.backendOwned, false);
  await ctx.cleanupBackend();
});

test('actual main entrypoint fails closed without reaching spawn or marker reuse', async () => {
  const ctx = mainSeam({attachBackend:async()=>{throw Error('backend interrupted');}});
  await assert.rejects(ctx._startBackendOnce(), /interrupted/);
});

module.exports = { mainSeam, receipt };

test('stale marker cannot authorize killing or replacing an unowned backend', async () => {
  const ctx = mainSeam({process:{env:{}}, app:{getVersion:()=>''},
    currentBackendIdentity:()=>({}), readPmHarnessStateFile:name=>name==='token'?'test':'{"port":12345,"pid":42}',
    waitForAuthenticatedBackend:async()=>{}, isSameCheckoutSuccessor, requestAuthenticatedBackendStop(){throw Error('unowned leftover stopped');},
    decideBackendReuse:()=>({action:'replace',reason:'identity_mismatch',marker:{pid:42,port:12345}}),
    logMain(){}, freePort(){throw Error('spawn attempted');}});
  await assert.rejects(ctx._startBackendOnce(), e=>e.code==='BACKEND_NOT_OWNED');
});

test('actual cleanup shuts down its owned real subprocess', async () => {
  const {spawn} = require('node:child_process');
  const {once} = require('node:events');
  const {shutdownOwnedBackendTree} = require('./backend-lifecycle.cjs');
  const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{detached:process.platform!=='win32',stdio:'ignore'});
  const exited=once(child,'exit');
  const ctx=mainSeam({process:{env:{}},backend:child,backendOwned:true,
    unlinkMarkerIfOwned(){},killBackendTree:b=>shutdownOwnedBackendTree({platform:process.platform,child:b,
      spawnSync:require('node:child_process').spawnSync,sleep:ms=>new Promise(r=>setTimeout(r,ms)),graceMs:100})});
  try {await ctx.cleanupBackend(); await exited; assert.equal(ctx.backendOwned,false);}
  finally {if(child.exitCode===null && child.signalCode===null) child.kill('SIGKILL');}
});
