'use strict';
const assert = require('node:assert/strict');
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const profile = process.env.FEED_PROFILE || fs.mkdtempSync(path.join(os.tmpdir(), 'mar-feed-'));
fs.mkdirSync(profile, { recursive: true });
const reportPath = process.env.FEED_REPORT || path.join(profile, 'report.json');
fs.writeFileSync(reportPath, JSON.stringify({ ok: false, state: 'running' }));
const timeout = setTimeout(() => { fs.writeFileSync(reportPath, JSON.stringify({ ok: false, error: 'Fixture timed out' })); app.exit(1); }, 45000);
// Isolated renderer, no preload, IPC, user profile, or backend connection.
app.setPath('userData', profile);
app.whenReady().then(async () => {
  const win = new BrowserWindow({ width: 900, height: 760, show: true, webPreferences: { sandbox: true, backgroundThrottling: false } });
  win.webContents.on('console-message', event => { if (event.level === 'error') process.stderr.write(event.message + '\n'); });
  const run = async code => { const result = await win.webContents.executeJavaScript(`Promise.resolve().then(() => { return ${code}; }).then(value => ({value}), error => ({error: String(error.stack || error)}))`); if (result.error) throw new Error(result.error); return result.value; };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const results = [];
  const grow = async () => {
    await run('window.feedFixture.grow()');
    await pause(150);
    return run('window.feedFixture.measure()');
  };
  const pinned = result => {
    assert.deepEqual(result.unexpectedRequests, []);
    assert.ok(Math.abs(result.tailDistance) <= 1, JSON.stringify(result));
    assert.ok(result.latestBottom <= result.composerTop, JSON.stringify(result));
  };
  win.webContents.setUserAgent(win.webContents.getUserAgent().replace(/ Electron\/[^ ]+/g, ''));
  await win.loadURL(process.env.FEED_FIXTURE_URL || 'http://127.0.0.1:5288/fixtures/feed-attachment.html');
  await pause(700);
  for (const session of ['fixture-A', 'fixture-B', 'fixture-A']) {
    await run(`document.querySelector('[data-switch="${session}"]').click()`);
    await pause(1200);
    await run(`document.querySelector('textarea[placeholder="Message the pilot..."]').focus()`);
    win.webContents.insertText('stream fixture');
    await pause(100);
    await run(`document.querySelector('button[aria-label="Send"]').click()`);
    await pause(200);
    for (let i = 0; i < 5; i++) { const result = await grow(); pinned(result); results.push(result); }
    const bounds = await run(`(() => { const r = document.querySelector('[data-testid="transcript-feed-scrollport"]').getBoundingClientRect(); return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}; })()`);
    win.webContents.sendInputEvent({ type: 'mouseWheel', ...bounds, deltaY: 360, deltaX: 0 });
    await pause(350);
    const released = await run('window.feedFixture.measure()');
    assert.ok(released.tailDistance > 50, `Wheel did not release: ${JSON.stringify(released)}`);
    const afterGrowth = await grow();
    assert.ok(afterGrowth.tailDistance > 50, JSON.stringify(afterGrowth));
    assert.ok(Math.abs(afterGrowth.scrollTop - released.scrollTop) <= 1, JSON.stringify({ released, afterGrowth }));
    results.push({released, afterGrowth});
    await run(`document.querySelector('[data-testid="jump-to-latest"]').click()`);
    await pause(200);
    const resumed = await grow();
    pinned(resumed);
    results.push({resumed});
  }
  const bounds = await run(`(() => { const r = document.querySelector('[data-testid="transcript-feed-scrollport"]').getBoundingClientRect(); return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}; })()`);
  win.webContents.sendInputEvent({ type: 'mouseWheel', ...bounds, deltaY: 280, deltaX: 0 });
  await pause(350);
  const reading = await run('window.feedFixture.measure()');
  assert.ok(reading.tailDistance > 50);
  for (const id of ['fixture-B', 'fixture-A']) {
    await run(`document.querySelector('[data-switch="${id}"]').click()`);
    await pause(1200);
  }
  const restored = await run('window.feedFixture.measure()');
  assert.ok(reading.anchorKey, JSON.stringify(reading));
  assert.equal(restored.anchorKey, reading.anchorKey);
  assert.ok(Math.abs(restored.anchorOffset - reading.anchorOffset) <= 1, JSON.stringify({reading, restored}));
  assert.ok(restored.tailDistance > 50);
  results.push({reading, restored});
  await run(`document.querySelector('[data-testid="jump-to-latest"]').click()`);
  await pause(200);
  pinned(await run('window.feedFixture.measure()'));
  const report = JSON.stringify({ ok: true, results }, null, 2);
  clearTimeout(timeout);
  fs.writeFileSync(reportPath, report);
  process.stdout.write(report);
  app.exit(0);
}).catch(error => { clearTimeout(timeout); fs.writeFileSync(reportPath, JSON.stringify({ ok: false, error: String(error.stack || error) })); process.stderr.write(String(error.stack || error)); app.exit(1); });
