const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {decideBackendReuse, buildBackendMarkerPayload, isSameCheckoutSuccessor} = require('./backend-identity.cjs');
const {buildPuppetmasterBackendEnv} = require('./inspect-isolation.cjs');

function fixture(t, error, sha = 'old', pidError = 'ESRCH') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'backend-recovery-test-'));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const state = path.join(root, 'state'); fs.mkdirSync(state);
  const marker = JSON.stringify({port:12345,pid:42,checkoutSha:sha,repoRoot:root});
  fs.writeFileSync(path.join(state, 'backend.json'), marker);
  fs.writeFileSync(path.join(state, 'token'), 'original');
  const spawns = [];
  const stops = [];
  const source = fs.readFileSync(path.join(__dirname,'main.cjs'),'utf8');
  const ctx = vm.createContext({fs,path,os:{homedir:()=>root},console:{log(){}},
    process:{env:{HOME:root},platform:process.platform,kill(pid,signal){assert.equal(signal,0);assert.equal(pid,42);if(pidError)throw Object.assign(Error(pidError),{code:pidError});},stdout:{write(){}},stderr:{write(){}}},
    ...require("./backend-lifecycle.cjs"),
    quitting:false,restarting:false,respawnTimes:[],
    consumeIntentionalRestartSignal:()=>false,startBackend:async()=>{},reinjectBackendIntoRenderer(){},
    killBackendTree:async()=>{},
    backend:null,backendOwned:false,backendPort:0,harnessToken:'new',harnessAppRunId:'run',
    isDev:false,isPackaged:true,app:{getVersion:()=> 'updated'},logMain(){},
    resolveRepoRoot:()=>root,resolveHarnessStateDir:()=>state,pmharnessHome:()=>root,
    stateFileSearchDirs:()=>[state,root],isInspectMode:()=>false,
    currentBackendIdentity:()=>({repoRoot:root,checkoutSha:'new'}),decideBackendReuse,buildBackendMarkerPayload,isSameCheckoutSuccessor,
    waitForAuthenticatedBackend:async()=>{if(error) throw error;},
    requestAuthenticatedBackendStop:async(...args)=>{stops.push(args[0]||{});return true;},
    readLiveUpdateMarker:()=>null,freePort:async()=>23456,loginShellEnv:()=>({}),
    buildPuppetmasterBackendEnv,secretVault:{injectEnv(){}},require:()=>({safeStorage:{}}),
    venvPython:()=>'/fixture/python',refreshAllowedLoopbackAliases(){},
    waitForBackend:async()=>{},shouldUnlinkBackendMarker:owned=>owned,
    spawn(py,args,opts){
      spawns.push({py,args,opts});
      fs.writeFileSync(path.join(opts.env.HARNESS_STATE_DIR,'token'),opts.env.HARNESS_TOKEN);
      const child = new EventEmitter(); child.pid=5678;
      child.stdout=new EventEmitter();child.stderr=new EventEmitter();return child;
    },
  });
  vm.runInContext(source.slice(source.indexOf('function pmharnessStateDir()'),source.indexOf('const { decideBackendPortRefresh')),ctx);
  vm.runInContext(source.slice(source.indexOf('function markerPath()'),source.indexOf('function consumeIntentionalRestartSignal()')),ctx);
  vm.runInContext(source.slice(source.indexOf('async function _startBackendOnce()'),source.indexOf('// ---- transport seam')),ctx);
  vm.runInContext(source.slice(source.indexOf("async function cleanupBackend()"),source.indexOf('app.on("window-all-closed"')),ctx);
  return {ctx,spawns,stops,state,marker,root};
}

test('packaged update relaunch recovers same state and preserves history',async t=>{
  const {ctx,spawns,state,marker}=fixture(t,Object.assign(Error('refused'),{code:'ECONNREFUSED'}));
  fs.writeFileSync(path.join(state,'history.json'),'existing history');
  await ctx._startBackendOnce();
  assert.equal(spawns.length,1);
  const env=spawns[0].opts.env;
  assert.equal(env.HARNESS_STATE_DIR,state);
  assert.equal(env.HARNESS_REPO,undefined);
  assert.equal(JSON.parse(fs.readFileSync(ctx.markerPath())).checkoutSha,'new');
  assert.equal(ctx.readPmHarnessStateFile('token'),'new');
  ctx.unlinkMarkerIfOwned(ctx.backend.pid, ctx.backendOwned);
  assert.equal(fs.readFileSync(path.join(state,'history.json'),'utf8'),'existing history');
});

test('packaged update relaunch replaces same-checkout leftover and preserves history',async t=>{
  const {ctx,spawns,stops,state}=fixture(t,null);
  fs.writeFileSync(path.join(state,'history.json'),'existing history');
  await ctx._startBackendOnce();
  assert.equal(stops.length,1);
  assert.equal(stops[0].port,12345);
  assert.equal(spawns.length,1);
  assert.equal(spawns[0].opts.env.HARNESS_STATE_DIR,state);
  assert.equal(JSON.parse(fs.readFileSync(ctx.markerPath())).checkoutSha,'new');
  assert.equal(ctx.readPmHarnessStateFile('token'),'new');
  assert.equal(fs.readFileSync(path.join(state,'history.json'),'utf8'),'existing history');
});

for(const [name,error] of [['authentication rejection',Object.assign(Error('403'),{tokenRejected:true})],['timeout',Error('timeout')],['reset',Object.assign(Error('reset'),{code:'ECONNRESET'})]]) {
  test(`packaged relaunch refuses ${name} without spawning or mutating old state`,async t=>{
    const {ctx,spawns,state,marker}=fixture(t,error);
    await assert.rejects(ctx._startBackendOnce(),e=>e.code==='BACKEND_NOT_OWNED');
    assert.equal(spawns.length,0);
    assert.equal(fs.readFileSync(path.join(state,'backend.json'),'utf8'),marker);
    assert.equal(fs.readFileSync(path.join(state,'token'),'utf8'),'original');
  });
}

test('packaged relaunch refuses a live leftover from another checkout',async t=>{
  const {ctx,spawns,state,marker,root}=fixture(t,null);
  ctx.currentBackendIdentity=()=>({repoRoot:path.join(root,'other'),checkoutSha:'new'});
  await assert.rejects(ctx._startBackendOnce(),e=>e.code==='BACKEND_NOT_OWNED');
  assert.equal(spawns.length,0);
  assert.equal(fs.readFileSync(path.join(state,'backend.json'),'utf8'),marker);
  assert.equal(fs.readFileSync(path.join(state,'token'),'utf8'),'original');
});

test('packaged relaunch refuses when authenticated stop of leftover fails',async t=>{
  const {ctx,spawns,state,marker}=fixture(t,null);
  ctx.requestAuthenticatedBackendStop=async()=>{
    throw Object.assign(new Error('HTTP 403'),{tokenRejected:true});
  };
  await assert.rejects(ctx._startBackendOnce(),e=>e.code==='BACKEND_NOT_OWNED');
  assert.equal(spawns.length,0);
  assert.equal(fs.readFileSync(path.join(state,'backend.json'),'utf8'),marker);
  assert.equal(fs.readFileSync(path.join(state,'token'),'utf8'),'original');
});

test('packaged same-checkout relaunch authenticates and reuses without ownership',async t=>{
  const {ctx,spawns}=fixture(t,null,'new');
  await ctx._startBackendOnce();
  assert.equal(spawns.length,0);assert.equal(ctx.backendPort,12345);assert.equal(ctx.backendOwned,false);
});

for (const pidError of [null, 'EPERM']) {
  test(`refused endpoint with existing or unknown PID (${pidError}) retains state`,async t=>{
    const {ctx,spawns,state,marker}=fixture(t,Object.assign(Error('refused'),{code:'ECONNREFUSED'}),'old',pidError);
    await assert.rejects(ctx._startBackendOnce(),e=>e.code==='BACKEND_NOT_OWNED');
    assert.equal(spawns.length,0);
    assert.equal(fs.readFileSync(path.join(state,'backend.json'),'utf8'),marker);
    assert.equal(fs.readFileSync(path.join(state,'token'),'utf8'),'original');
  });
}
test('normal no-marker launch preserves state and native arguments',async t=>{
  const {ctx,spawns,state}=fixture(t,null);
  fs.unlinkSync(path.join(state,'backend.json'));
  await ctx._startBackendOnce();
  assert.equal(spawns.length,1);
  assert.equal(spawns[0].opts.env.HARNESS_STATE_DIR,state);
  assert.deepEqual(Array.from(spawns[0].args),['-m','harness.cli','gui','--port','23456']);
});

for (const route of ['exit', 'cleanup']) {
  for (const markerPid of [42, 5678]) {
    test(`${route} removes only the owned child marker (disk PID ${markerPid})`, async t => {
      const {ctx,state,root} = fixture(t,null);
      fs.unlinkSync(path.join(state,'backend.json'));
      await ctx._startBackendOnce();
      const child = ctx.backend;
      const bytes = JSON.stringify({pid:markerPid,port:34567,extra:'preserve bytes'});
      const paths = [path.join(state,'backend.json'),path.join(root,'backend.json')];
      for (const p of paths) fs.writeFileSync(p,bytes);
      let killed;
      ctx.killBackendTree = async b => { killed=b; };
      if (route === 'exit') child.emit('exit',2,null);
      else await ctx.cleanupBackend();
      for (const p of paths) {
        if (markerPid === child.pid) assert.equal(fs.existsSync(p),false);
        else assert.equal(fs.readFileSync(p,'utf8'),bytes);
      }
      assert.equal(ctx.backend,null);
      assert.equal(ctx.backendOwned,false);
      if (route === 'cleanup') assert.equal(killed,child);
    });
  }
}

test('exit retains marker replaced after ownership capture', async t => {
  const {ctx,state} = fixture(t,null);
  const p = path.join(state,'backend.json');
  fs.unlinkSync(p);
  await ctx._startBackendOnce();
  const child = ctx.backend;
  const replacement = '{"pid":42,"port":34567,"replacement":true}';
  ctx.consumeIntentionalRestartSignal = () => {
    fs.writeFileSync(p,replacement);
    return false;
  };
  child.emit('exit',2,null);
  assert.equal(fs.readFileSync(p,'utf8'),replacement);
});

test('late old-child exit preserves replacement child and marker', async t => {
  const {ctx,state} = fixture(t,null);
  const p = path.join(state,'backend.json');
  fs.unlinkSync(p);
  await ctx._startBackendOnce();
  const old = ctx.backend;
  await ctx.cleanupBackend();
  const replacement = new EventEmitter(); replacement.pid=6789;
  ctx.backend=replacement;ctx.backendOwned=true;
  const bytes = JSON.stringify({pid:6789,port:34567});fs.writeFileSync(p,bytes);
  old.emit('exit',0,null);
  assert.equal(ctx.backend,replacement);
  assert.equal(ctx.backendOwned,true);
  assert.equal(fs.readFileSync(p,'utf8'),bytes);
});

test('cleanup checks state and legacy markers independently', async t => {
  const {ctx,state,root} = fixture(t,null);
  const p=path.join(state,'backend.json');fs.unlinkSync(p);
  await ctx._startBackendOnce();
  const legacy=path.join(root,'backend.json');
  const bytes='{"pid":42,"port":34567}';fs.writeFileSync(legacy,bytes);
  await ctx.cleanupBackend();
  assert.equal(fs.existsSync(p),false);
  assert.equal(fs.readFileSync(legacy,'utf8'),bytes);
});

test('adopted backend cleanup preserves marker and never shuts down', async t => {
  const {ctx,state,marker} = fixture(t,null,'new');
  await ctx._startBackendOnce();
  ctx.killBackendTree=async()=>assert.fail('adopted backend shutdown');
  await ctx.cleanupBackend();
  assert.equal(fs.readFileSync(path.join(state,'backend.json'),'utf8'),marker);
});

for (const bytes of ['invalid json', '{"pid":"5678"}', 'null']) {
  test(`cleanup preserves unverified marker ${bytes}`, async t => {
    const {ctx,state} = fixture(t,null);
    const p=path.join(state,'backend.json');fs.unlinkSync(p);
    await ctx._startBackendOnce();
    fs.writeFileSync(p,bytes);
    await ctx.cleanupBackend();
    assert.equal(fs.readFileSync(p,'utf8'),bytes);
  });
}
