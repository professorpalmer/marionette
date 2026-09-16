const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const vm = require('node:vm');
function bridge(shell, opts) {
  const handlers = new Map();
  const mod = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'fs-bridge.cjs'), 'utf8'), {
    module: mod, require: name => name === 'electron' ? { shell } : name.startsWith('./') ? require(path.join(__dirname, name)) : require(name),
  });
  mod.exports.registerFsBridge({ handle: (name, fn) => handlers.set(name, fn) }, opts);
  return handlers.get('fs:openPath');
}
test('native open uses exact real fixtures and denies missing or unauthorized requests', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mar-open-'));
  const file = path.join(dir, 'space %20 #.txt');
  fs.writeFileSync(file, 'fixture');
  const opened = [], revealed = [];
  const shell = { openPath: async p => { opened.push(p); return ''; }, showItemInFolder: p => revealed.push(p) };
  const open = bridge(shell, { isAllowedSender: e => e.allowed });
  try {
    assert.equal(typeof open, 'function');
    for (const p of [file, pathToFileURL(file).href, dir]) assert.equal((await open({ allowed: true }, p)).ok, true);
    assert.deepEqual(opened, [fs.realpathSync.native(file), fs.realpathSync.native(file), fs.realpathSync.native(dir)]);
    const localhost = pathToFileURL(file);
    localhost.hostname = 'localhost';
    assert.equal((await open({ allowed: true }, localhost.href)).ok, true);
    const homeRelative = path.relative(os.homedir(), file);
    assert.equal((await open({ allowed: true }, '~/' + homeRelative)).ok, true);
    for (const p of ['relative.txt', 'https://example.com/x', 'file://server/share/a', '//server/share', '\\\\server\\share', '/tmp/a\n.txt', 'file:///tmp/%00.txt', 'file:///tmp/%2Fetc', path.join(dir, 'missing')]) {
      assert.equal((await open({ allowed: true }, p)).ok, false, p);
    }
    assert.equal((await open({}, file)).ok, false);
    assert.equal((await bridge(shell)({}, file)).ok, false);
    for (const ext of ['sh', 'app', 'exe', 'command', 'ps1', 'bat', 'cmd', 'py', 'js', 'lnk', 'url', 'webloc', 'desktop']) {
      const unsafe = path.join(dir, 'target.' + ext);
      fs.writeFileSync(unsafe, 'fixture');
      assert.equal((await open({ allowed: true }, unsafe)).action, 'revealed');
    }
    if (process.platform !== 'win32') {
      fs.chmodSync(file, 0o755);
      assert.equal((await open({ allowed: true }, file)).action, 'revealed');
      fs.chmodSync(file, 0o644);
      const alias = path.join(dir, 'alias.txt');
      fs.symlinkSync(path.join(dir, 'target.sh'), alias);
      assert.equal((await open({ allowed: true }, alias)).action, 'revealed');
      assert.equal((await open({ allowed: true }, 'C:\\file.txt')).ok, false);
      assert.equal((await open({ allowed: true }, 'file:///C:/file.txt')).ok, false);
    }
    const appDir = path.join(dir, 'bundle.app');
    fs.mkdirSync(appDir);
    assert.equal((await open({ allowed: true }, appDir)).action, 'revealed');
    shell.openPath = async () => 'No application associated';
    assert.match((await open({ allowed: true }, file)).error, /No application associated/);
    shell.openPath = async () => { throw new Error('EACCES'); };
    assert.match((await open({ allowed: true }, file)).error, /EACCES/);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('production sender guard denies subframes, foreign windows and foreign documents', () => {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const indexPath = path.resolve(os.tmpdir(), 'marionette-sender-fixture', 'index.html');
  const mainFrame = { url: pathToFileURL(indexPath).href };
  const sender = { mainFrame, isDestroyed: () => false };
  const context = { win: { webContents: sender, isDestroyed: () => false }, URL, require, resolveDistIndex: () => indexPath, isDev: false, viteUrl: null };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function isAllowedSender(event)'), source.indexOf('// Binary images bypass')), context);
  assert.equal(context.isAllowedSender({ sender, senderFrame: mainFrame }), true);
  assert.equal(context.isAllowedSender({ sender, senderFrame: { url: mainFrame.url } }), false);
  assert.equal(context.isAllowedSender({ sender: { ...sender }, senderFrame: mainFrame }), false);
  mainFrame.url = 'https://foreign.example/';
  assert.equal(context.isAllowedSender({ sender, senderFrame: mainFrame }), false);
  assert.match(source, /registerFsBridge\(ipcMain, \{ isAllowedSender \}\)/);
});
