const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
test('production preload exposes cancellable native image method', () => {
  const exposed = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'preload.cjs'), 'utf8'), {
    process: {env:{}}, require: () => ({contextBridge:{exposeInMainWorld:(key,value)=>{exposed[key]=value;}}, ipcRenderer:{}}),
  });
  assert.equal(typeof exposed.harnessIPC.requestImage, 'function');
});

const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const {startImageRequest, registerImageBridge, MAX_IMAGE_BYTES} = require('./image-bridge.cjs');
const identity = {'X-Harness-Protocol':'1','X-Harness-Endpoint':'endpoint','X-Harness-Boot':'boot','X-Harness-Token':'evil','X-Harness-Device-Token':'evil', Other:'evil'};
const backend = {port:7788, token:'main-secret'};
function fakeHTTP({status=200, headers={'content-type':'image/png'}, body=Buffer.from([1,2,3]), hang=false}={}) {
  let options, req, res;
  const request = (opts, callback) => {
    options=opts;
    req=new EventEmitter(); req.destroyed=false; req.destroy=()=>{req.destroyed=true;};
    req.end=()=>queueMicrotask(()=>{
      res=Object.assign(new PassThrough(), {statusCode:status, headers});
      callback(res);
      if (!hang && !res.destroyed) res.end(body);
    });
    return req;
  };
  return {request, get options(){return options;}, get req(){return req;}, get res(){return res;}};
}
function run(fake, overrides={}) { return startImageRequest({path:'/api/image?path=input%3As%3Asha&_r=1', identity, backend, request:fake.request, ...overrides}); }

test('helper uses exact loopback GET, main token and only endpoint pin headers', async () => {
  const fake=fakeHTTP(); const result=await run(fake).promise;
  assert.deepEqual(fake.options, {host:'127.0.0.1',port:7788,path:'/api/image?path=input%3As%3Asha&_r=1',method:'GET',headers:{'X-Harness-Token':'main-secret','X-Harness-Protocol':'1','X-Harness-Endpoint':'endpoint','X-Harness-Boot':'boot'}});
  assert.deepEqual(result,{kind:'image-response',status:200,mime:'image/png',bytes:new Uint8Array([1,2,3]),port:7788});
  assert.equal(fake.req.destroyed,true);
});
for (const path of ['http://127.0.0.1:7788/api/image?path=x','http://evil/api/image?path=x','//evil/api/image?path=x','/api/other?path=x','/api/a/../image?path=x','/api/image?path=x&token=x','/api/image?path=x&path=y','/api/image?path=x&_r=1&_r=2','/api/image?path=','/api/image?path=%00','/api/image?path=x#x','/api/image?path=%ZZ']) {
  test(`helper rejects invalid route ${path}`, async () => {
    const fake=fakeHTTP(); assert.equal((await run(fake,{path}).promise).code,'request'); assert.equal(fake.options,undefined);
  });
}
test('invalid identity headers cannot inject headers or omit pin', async () => {
  for(const id of [{}, {...identity,'X-Harness-Boot':'boot\r\nEvil: yes'}, {...identity,'X-Harness-Protocol':'2'}]) {
    const fake=fakeHTTP(); assert.equal((await run(fake,{identity:id}).promise).code,'request'); assert.equal(fake.options,undefined);
  }
});
test('redirect and HTTP errors return no body and never follow Location', async () => {
  for (const status of [302,403,409,500]) {
    const fake=fakeHTTP({status,headers:{location:'http://evil/token'},body:Buffer.from('private path token')});
    const result=await run(fake).promise;
    assert.equal(result.status,status); assert.equal(result.bytes.length,0); assert.equal(result.mime,'');
    assert.equal(JSON.stringify(result).includes('private'),false); assert.equal(fake.req.destroyed,true);
  }
});
test('declared/streamed oversize, empty and wrong MIME fail closed', async () => {
  for(const options of [
    {headers:{'content-type':'image/png','content-length':String(MAX_IMAGE_BYTES+1)}},
    {body:Buffer.alloc(MAX_IMAGE_BYTES+1)}, {body:Buffer.alloc(0)},
    {headers:{'content-type':'text/html'}},
    {headers:{'content-type':'image/png','content-length':'99'}},
  ]) {
    const fake=fakeHTTP(options); const result=await run(fake).promise;
    assert.equal(result.kind,'image-error'); assert.equal(fake.req.destroyed,true);
  }
});
test('wall clock timeout destroys stalled response and request', async () => {
  const fake=fakeHTTP({hang:true}); const result=await run(fake,{timeoutMs:10}).promise;
  assert.equal(result.code,'timeout'); assert.equal(fake.req.destroyed,true); assert.equal(fake.res.destroyed,true);
});
test('cancel destroys pending request and late data cannot change result', async () => {
  const fake=fakeHTTP({hang:true}); const handle=run(fake);
  await new Promise(resolve=>setImmediate(resolve)); handle.cancel();
  assert.equal((await handle.promise).code,'aborted'); assert.equal(fake.req.destroyed,true);
  fake.res.emit('data',Buffer.from('late')); fake.res.emit('end');
  assert.equal((await handle.promise).code,'aborted');
});
function ipcFixture() {
  const handlers=new Map(); const ipc=new EventEmitter(); ipc.handle=(name,fn)=>handlers.set(name,fn);
  let current=backend;
  const fakes=[];
  registerImageBridge(ipc,{getBackend:()=>current,isAllowedSender:e=>e.allowed===true && !e.sender.isDestroyed(),start:opts=>{
    const fake=fakeHTTP({hang:true}); fakes.push(fake); return startImageRequest({...opts,request:fake.request});
  }});
  const sender=()=>Object.assign(new EventEmitter(), {isDestroyed(){return this.dead===true;}});
  return {ipc,handlers,sender,fakes,setBackend:value=>{current=value;}};
}
test('request/cancel IDs belong to sending webContents and listeners clean up', async () => {
  const f=ipcFixture(); const a=f.sender(),b=f.sender(); const request=f.handlers.get('harness:requestImage');
  const pa=request({sender:a,allowed:true},'image-1','/api/image?path=x',identity,7788);
  const pb=request({sender:b,allowed:true},'image-1','/api/image?path=x',identity,7788);
  f.ipc.emit('harness:cancelImage',{sender:b,allowed:true},'image-1');
  assert.equal((await pb).code,'aborted'); assert.equal(f.fakes[0].req.destroyed,false);
  assert.equal(b.listenerCount('destroyed'),0);
  f.ipc.emit('harness:cancelImage',{sender:a,allowed:true},'image-1');
  assert.equal((await pa).code,'aborted'); assert.equal(a.listenerCount('destroyed'),0);
});
test('destroyed sender destroys requests and rejects late delivery', async () => {
  const f=ipcFixture(), sender=f.sender();
  const pending=f.handlers.get('harness:requestImage')({sender,allowed:true},'image-1','/api/image?path=x',identity,7788);
  sender.dead=true; sender.emit('destroyed');
  assert.equal((await pending).code,'aborted'); assert.equal(f.fakes[0].req.destroyed,true); assert.equal(sender.listenerCount('destroyed'),0);
});
test('sender, port and duplicate ID validation happen before another HTTP request', async () => {
  const f=ipcFixture(),sender=f.sender(), request=f.handlers.get('harness:requestImage');
  assert.equal((await request({sender,allowed:false},'image-1','/api/image?path=x',identity,7788)).code,'request');
  assert.equal((await request({sender,allowed:true},'image-1','/api/image?path=x',identity,7799)).code,'stale');
  const pending=request({sender,allowed:true},'image-1','/api/image?path=x',identity,7788);
  assert.equal((await request({sender,allowed:true},'image-1','/api/image?path=x',identity,7788)).code,'request');
  assert.equal(f.fakes.length,1);
  f.setBackend({...backend,port:7799}); f.ipc.emit('harness:cancelImage',{sender,allowed:true},'image-1');
  assert.equal((await pending).code,'stale');
});
test('production main registration and preload cancellation connect to actual helper', async () => {
  const handlers=new Map(); const ipc=new EventEmitter(); ipc.handle=(name,fn)=>handlers.set(name,fn);
  const sender=Object.assign(new EventEmitter(),{isDestroyed:()=>false,mainFrame:{url:'file:///app/index.html'}});
  const event={sender,senderFrame:sender.mainFrame};
  const source=fs.readFileSync(path.join(__dirname,'main.cjs'),'utf8');
  const fake=fakeHTTP({hang:true});
  vm.runInNewContext(source.slice(source.indexOf('function isAllowedSender(event)'),source.indexOf('// Native folder picker')), {
    ipcMain:ipc,backendPort:7788,authToken:()=>backend.token,win:{isDestroyed:()=>false,webContents:sender},
    URL,resolveDistIndex:()=>'/app/index.html',isDev:false,viteUrl:null,
    require:name=>name==='./image-bridge.cjs'?{registerImageBridge:(bus,opts)=>registerImageBridge(bus,{...opts,start:o=>startImageRequest({...o,request:fake.request})})}:require(name),
  });
  const exposed={}; const replies=[];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'preload.cjs'),'utf8'),{
    process:{env:{}}, require:()=>({contextBridge:{exposeInMainWorld:(k,v)=>{exposed[k]=v;}},ipcRenderer:{
      invoke:(name,...args)=>handlers.get(name)(event,...args),send:(name,...args)=>ipc.emit(name,event,...args),
    }}),
  });
  const cancel=exposed.harnessIPC.requestImage('/api/image?path=x',identity,7788,value=>replies.push(value));
  await new Promise(resolve=>setImmediate(resolve)); cancel();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(fake.req.destroyed,true); assert.deepEqual(replies,[]); assert.equal(sender.listenerCount('destroyed'),0);
  const handler=handlers.get('harness:requestImage');
  assert.equal((await handler({...event,senderFrame:{url:'file:///app/index.html'}},'image-2','/api/image?path=x',identity,7788)).code,'request');
  sender.mainFrame.url='https://evil.example';
  assert.equal((await handler(event,'image-3','/api/image?path=x',identity,7788)).code,'request');
});

test('real loopback HTTP bytes, redirect refusal, timeout and socket abort', async t => {
  const http=require('node:http');
  let redirected=0, closed=0;
  const server=http.createServer((req,res)=>{
    const mode=new URL(req.url,'http://local').searchParams.get('path');
    assert.equal(req.headers['x-harness-token'],'main-secret');
    assert.equal(req.headers['x-harness-boot'],'boot');
    if(mode==='redirect') { res.writeHead(302,{Location:'/redirected'});res.end('private');return; }
    if(req.url==='/redirected') redirected++;
    if(mode==='hang') { req.on('close',()=>closed++); return; }
    res.writeHead(200,{'Content-Type':'image/png'}); res.end(Buffer.from([0,255,128,42]));
  });
  try {
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  } catch(error) {
    if(error.code==='EPERM' || error.code==='EACCES') {t.skip(`Loopback bind denied: ${error.code}; parent must run unsandboxed`);return;}
    throw error;
  }
  t.after(()=>{server.closeAllConnections();server.close();});
  const options={identity,backend:{...backend,port:server.address().port}};
  const result=await startImageRequest({...options,path:'/api/image?path=bytes'}).promise;
  assert.deepEqual(result.bytes,new Uint8Array([0,255,128,42]));
  assert.equal((await startImageRequest({...options,path:'/api/image?path=redirect'}).promise).status,302);
  assert.equal(redirected,0);
  assert.equal((await startImageRequest({...options,path:'/api/image?path=hang',timeoutMs:30}).promise).code,'timeout');
  const pending=startImageRequest({...options,path:'/api/image?path=hang'});
  await new Promise(resolve=>setTimeout(resolve,20));pending.cancel();assert.equal((await pending.promise).code,'aborted');
  await new Promise(resolve=>setTimeout(resolve,20));assert.equal(closed,2);
});

test('timeout before response and socket error both destroy and sanitize', async () => {
  let req;
  const request=()=>{
    req=new EventEmitter();req.end=()=>{};req.destroy=()=>{req.destroyed=true;};return req;
  };
  const timeout=startImageRequest({path:'/api/image?path=x',identity,backend,request,timeoutMs:5});
  assert.equal((await timeout.promise).code,'timeout');assert.equal(req.destroyed,true);
  const broken=startImageRequest({path:'/api/image?path=x',identity,backend,request});
  req.emit('error',new Error('private path main-secret'));
  assert.deepEqual(await broken.promise,{kind:'image-error',code:'connection'});assert.equal(req.destroyed,true);
});
test('preload passes binary envelope unchanged and never sends cancellation after completion', async () => {
  const exposed={}, sent=[];let resolve;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'preload.cjs'),'utf8'),{
    process:{env:{}},require:()=>({contextBridge:{exposeInMainWorld:(k,v)=>{exposed[k]=v;}},ipcRenderer:{
      invoke:(channel,id,path,headers,port)=>{
        assert.equal(channel,'harness:requestImage');assert.equal(id,'image-1');assert.equal(path,'/api/image?path=x');assert.equal(port,7788);assert.equal(headers,identity);
        return new Promise(done=>{resolve=done;});
      },send:(...args)=>sent.push(args),
    }}),
  });
  const results=[];
  const cancel=exposed.harnessIPC.requestImage('/api/image?path=x',identity,7788,value=>results.push(value));
  const envelope={kind:'image-response',status:200,mime:'image/png',bytes:new Uint8Array([0,128,255]),port:7788};
  resolve(envelope);await new Promise(done=>setImmediate(done));cancel();
  assert.deepEqual(results,[envelope]);assert.deepEqual(sent,[]);
});
