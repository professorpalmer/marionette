// Controlled process fixture. No providers, user state, or listening sockets.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawn} = require('node:child_process');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const root = path.resolve(__dirname, '../..');
const dir = process.argv[3];
const receiptPath = path.join(dir, 'receipt');
if (process.argv[2] === 'host') {
  const receipt = {schema:1, owner:'external', port:12345, pid:process.pid,
    endpoint_id:'fixture-endpoint', boot_id:'fixture-boot', launch_id:'fixture-launch',
    repo_root:root, token_file:path.join(dir,'token'),
    environment:{source_sha:'fixture-sha', source_digest:'fixture-digest'}};
  fs.writeFileSync(receipt.token_file, 'fixture-token');
  fs.writeFileSync(receiptPath, JSON.stringify(receipt));
  let task;
  process.on('message', msg => {
    if (msg.kind === 'start') {
      if (task) throw Error('Task replayed');
      task = spawn(process.execPath, ['-e', `const fs=require('fs'); const t=setInterval(()=>{if(fs.existsSync(process.argv[1])){fs.writeFileSync(process.argv[2],'completed once');clearInterval(t)}},10)`, path.join(dir,'release'), path.join(dir,'result')], {stdio:'ignore'});
      task.on('exit', () => process.send({kind:'completed'}));
      process.send({kind:'started'});
    } else if (msg.kind === 'probe') {
      process.send({kind:'response', id:msg.id, status:msg.drift ? 409 : 200, body:receipt});
    } else if (msg.kind === 'stop') {
      if (task && task.exitCode === null) task.kill();
      process.exit(0);
    }
  });
  process.on('disconnect', () => { if(task && task.exitCode === null) task.kill(); process.exit(0); });
  if (process.env.BACKEND_LIFETIME_SOCKET === '1') {
    const server = require('node:http').createServer((req,res)=>{
      const authorized = req.url === '/api/backend/lifetime'
        && req.headers['x-harness-token'] === 'fixture-token'
        && req.headers['x-harness-boot'] === receipt.boot_id
        && req.headers['x-harness-endpoint'] === receipt.endpoint_id;
      res.writeHead(!authorized ? 403 : fs.existsSync(path.join(dir,'drift')) ? 409 : 200);
      res.end(JSON.stringify(receipt));
    });
    server.on('error', error=>{ console.error(error.code); process.exit(3); });
    server.listen(0,'127.0.0.1',()=>{
      receipt.port=server.address().port;
      fs.writeFileSync(receiptPath,JSON.stringify(receipt));
      process.send({kind:'ready'});
    });
  } else process.send({kind:'ready'});
} else {
  let pending;
  process.on('message', msg => { if(msg.kind === 'response') pending(msg); });
  const fakeHttp = {get(options, callback) {
    const request = new EventEmitter();
    request.destroy = err => request.emit('error', err);
    pending = msg => {
      const response = new PassThrough();
      response.statusCode = msg.status;
      callback(response);
      response.end(JSON.stringify(msg.body));
    };
    process.send({kind:'probe', headers:options.headers});
    return request;
  }};
  const moduleContext = vm.createContext({module:{exports:{}}, Map, Promise,
    require: name => name === 'node:http' && process.env.BACKEND_LIFETIME_SOCKET !== '1' ? fakeHttp : require(name)});
  vm.runInContext(fs.readFileSync(path.join(root,'webapp/electron/backend-attach.cjs'),'utf8'), moduleContext);
  const main = fs.readFileSync(path.join(root,'webapp/electron/main.cjs'),'utf8');
  const ctx = vm.createContext({process:{env:{MARIONETTE_BACKEND_RECEIPT:receiptPath}},
    attachBackend:moduleContext.module.exports.attachBackend, resolveRepoRoot:()=>root,
    backend:null, backendOwned:false, backendPort:0, harnessToken:'',
    refreshAllowedLoopbackAliases(){}, unlockAdoptedBackend(){}, unlinkMarkerIfOwned(){if(ctx.backendOwned) throw Error('unlink attempted');},
    killBackendTree(){throw Error('borrowed process kill attempted');}});
  vm.runInContext(main.slice(main.indexOf('async function _startBackendOnce()'),main.indexOf('// ---- transport seam')),ctx);
  vm.runInContext(main.slice(main.indexOf('async function cleanupBackend()'),main.indexOf('app.on("window-all-closed"')),ctx);
  (async()=>{
    try {
      await ctx._startBackendOnce();
      await ctx.cleanupBackend();
      process.send({kind:'attached', port:ctx.backendPort});
      process.exit(0);
    } catch(e) {process.send({kind:'refused', error:e.message});process.exit(2);}
  })();
}
