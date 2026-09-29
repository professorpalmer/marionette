const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const vm = require('node:vm');
const path = require('node:path');

function loadRotate() {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const start = source.indexOf('const ELECTRON_LOG_MAX_BYTES');
  const end = source.indexOf('rotateElectronLog();', start);
  const ctx = vm.createContext({fs, os, path});
  vm.runInContext(source.slice(start, end), ctx);
  return ctx.rotateElectronLog;
}

test('electron.log rotates once it passes the cap, and not before', () => {
  const rotate = loadRotate();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'elog-'));
  const file = path.join(dir, 'electron.log');
  fs.writeFileSync(file, 'x'.repeat(100));
  rotate(file, 1000);
  assert.equal(fs.existsSync(`${file}.1`), false);
  fs.writeFileSync(file, 'x'.repeat(2000));
  rotate(file, 1000);
  assert.equal(fs.existsSync(file), false);
  assert.equal(fs.statSync(`${file}.1`).size, 2000);
  rotate(path.join(dir, 'missing.log'), 1000);
});
