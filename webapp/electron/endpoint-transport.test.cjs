const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const path = require('node:path');

test('actual preload/main IPC passes only identity headers for JSON, upload and stream', async () => {
  const source=fs.readFileSync(path.join(__dirname,'main.cjs'),'utf8');
  const handlers=new Map(); const listeners=new Map(); const requests=[]; const exposed={};
  const main={Buffer,setTimeout,harnessToken:'main-owned',backendPort:1,startInFlight:null,
    require:name=>require(path.resolve(__dirname,name)),tryRefreshBackendPortFromMarker(){},logMain(){},
    ipcMain:{handle:(name,fn)=>handlers.set(name,fn),on:(name,fn)=>listeners.set(name,fn),once(){},removeListener(){}},
    wireStreamResponse: require('./stream-bridge.cjs').wireStreamResponse,
    sanitizedStreamConnError:require('./stream-bridge.cjs').sanitizedStreamConnError,
  };
  function request(options,callback){
    requests.push(options);
    const req=new EventEmitter();
    req.write=()=>{};req.destroy=()=>{};
    req.end=()=>queueMicrotask(()=>{
      const res=Object.assign(new PassThrough(),{statusCode:200,headers:{}});
      callback(res);
      res.end(options.path==='/stream'?'data: {"kind":"done"}\n\n':'{"saved":[]}');
    });return req;
  }
  main.http={request,get(options,callback){const req=request(options,callback);req.end();return req;}};
  const context=vm.createContext(main);
  vm.runInContext(source.slice(source.indexOf('function authToken()'),source.indexOf('ipcMain.handle("secrets:save"')),context);
  vm.runInContext(source.slice(source.indexOf('ipcMain.handle("harness:uploadFile"'),source.indexOf('// Native folder picker')),context);
  vm.runInContext(source.slice(source.indexOf('ipcMain.on("harness:stream"'),source.indexOf('// ---- native bridges')),context);
  const bus=new EventEmitter();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'preload.cjs'),'utf8'),{process:{env:{}},require:()=>({
    contextBridge:{exposeInMainWorld:(name,value)=>{exposed[name]=value;}},
    ipcRenderer:{invoke:(name,...args)=>handlers.get(name)({},...args),
      on:(...args)=>bus.on(...args),removeListener:(...args)=>bus.removeListener(...args),
      send:(name,...args)=>listeners.get(name)?.({sender:{isDestroyed:()=>false,send:(channel,value)=>bus.emit(channel,{},value)}},...args)},
  })});
  const identity={'X-Harness-Protocol':'1','X-Harness-Endpoint':'endpoint','X-Harness-Boot':'boot','X-Harness-Token':'forbidden','Other':'ignored'};
  const bridge=exposed.harnessIPC;
  assert.equal(bridge.endpointHeaders,true);
  assert.throws(() => bridge.requestJSON('DELETE','/write',{},'correlation',identity), /Unsupported JSON request method/);
  assert.equal(requests.length,0);
  await bridge.requestJSON('POST','/write',{},'correlation',identity);
  await bridge.uploadFile({name:'a.txt',type:'text/plain',bytes:Buffer.from('a')},identity);
  await new Promise((resolve,reject)=>bridge.stream('/stream',()=>{},resolve,reject,identity));
  assert.equal(requests.length,3);
  for(const request of requests){
    assert.equal(request.host,'127.0.0.1');
    assert.equal(request.port,1);
    assert.equal(request.headers['X-Harness-Endpoint'],'endpoint');
    assert.equal(request.headers['X-Harness-Boot'],'boot');
    assert.equal(request.headers['X-Harness-Protocol'],'1');
    assert.equal(request.headers['X-Harness-Token'],'main-owned');
    assert.equal(request.headers.Other,undefined);
  }
});
