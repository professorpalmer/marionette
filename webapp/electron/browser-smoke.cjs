// Run with Electron, not node. Owns an isolated profile and loopback-only fixture.
const { app, BrowserWindow, webContents, ipcMain } = require("electron");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const assert = require("node:assert/strict");
const { createBrowserBridge } = require("./browser-bridge.cjs");
const { createBrowserController } = require("./browser-controller.cjs");
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "mar-browser-smoke-"));
app.setPath("userData", profile);
app.commandLine.appendSwitch("no-proxy-server");
const repo = path.resolve(__dirname, "../..");
let win, bridge, controller, server;
let context;
const exec = promisify(execFile);
const fixture = `<!doctype html><title>Browser fixture</title>
<label>Name <input id="name"></label><button id="apply" onclick="document.querySelector('#result').textContent='Saved '+document.querySelector('#name').value">Apply</button>
<div id="result">Not saved</div><input type="password" value="NEVER_SNAPSHOT_PASSWORD">
<a href="/next">Next page</a>`;
const host = `<!doctype html><style>webview{width:600px;height:400px;display:flex}</style>
<webview id="one" src="/page" partition="smoke"></webview><webview id="two" src="/second" partition="smoke" style="display:none"></webview>
<script>
const views=[...document.querySelectorAll('webview')];let active='one';
function publish(){const tabs=views.flatMap(v=>{try{return[{tabId:v.id,webContentsId:v.getWebContentsId()}]}catch{return[]}});return window.harnessIPC.setBrowserContext({sessionId:'smoke',activeTabId:active,tabs})}
views.forEach(v=>v.addEventListener('dom-ready',publish));
window.harnessIPC.onActivateBrowserTab(p=>{active=p.tabId;views.forEach(v=>v.style.display=v.id===active?'flex':'none');requestAnimationFrame(publish)});
</script>`;

async function python(action, args = {}, sessionId = "smoke") {
  const code = `import json,os,sys
from harness import desktop_browser as d,browser as b
from harness.vision import native_multimodal_user_content
d.configure(int(os.environ['SMOKE_PORT']),os.environ['SMOKE_TOKEN'])
result=getattr(b,'browser_'+sys.argv[1])(**json.loads(sys.argv[2]),session_id=sys.argv[3])
if sys.argv[1]=='screenshot':
    path=json.loads(result)['screenshot_path']
    assert d.can_view_screenshot(sys.argv[3],path)
    assert not d.can_view_screenshot('other-session',path)
    pixels=native_multimodal_user_content('Screenshot',[path])[-1]['image_url']['url']
    assert pixels.startswith('data:image/png;base64,iVBOR')
print(result)`;
  const { stdout } = await exec(path.join(repo, ".venv/bin/python"), ["-c", code, action, JSON.stringify(args), sessionId], {
    cwd: repo, env: { ...process.env, SMOKE_PORT: String(bridge.server.address().port), SMOKE_TOKEN: bridge.token }, timeout: 25000,
  });
  return stdout.trim();
}

async function ready() {
  const deadline = Date.now() + 15000;
  while ((!context || context.tabs.length !== 2 || context.tabs.some(tab => webContents.fromId(tab.webContentsId).isLoadingMainFrame())) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  assert.equal(context?.tabs.length, 2);
}

app.whenReady().then(async () => {
  server = http.createServer((req, res) => {
    res.setHeader("Content-Type", "text/html");
    res.end(req.url === "/host" ? host : req.url === "/next" ? "<title>Next</title><button>After navigation</button>" : fixture);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  win = new BrowserWindow({ width: 700, height: 500, show: true, webPreferences: {
    preload: path.join(__dirname, "preload.cjs"), webviewTag: true, contextIsolation: true, nodeIntegration: false,
  } });
  controller = createBrowserController({
    resolveGuest: id => { const guest = webContents.fromId(id); return guest?.hostWebContents === win.webContents ? guest : null; },
    activateTab: payload => win.webContents.send("browser:activateTab", payload),
    focus: guest => { win.focus(); guest.focus(); },
    screenshotDir: profile,
  });
  ipcMain.handle("browser:setContext", (event, payload) => {
    assert.equal(event.sender, win.webContents);
    const result = controller.setContext(payload); context = payload; return result;
  });
  bridge = createBrowserBridge({ dispatch: controller.dispatch });
  await bridge.listen();
  await win.loadURL("http://127.0.0.1:" + server.address().port + "/host");
  await ready();
  assert.equal(JSON.parse(await python("tabs")).length, 2);
  const snapshot = await python("snapshot");
  assert.match(snapshot, /Browser fixture/);
  assert.doesNotMatch(snapshot, /NEVER_SNAPSHOT_PASSWORD/);
  const input = snapshot.match(/(@\S+) input Name/)[1];
  const button = snapshot.match(/(@\S+) button Apply/)[1];
  assert.match(await python("type", { ref: input, text: "Marionette" }), /Typed/);
  assert.match(await python("click", { ref: button }), /Clicked/);
  assert.match(await python("get_text"), /Saved Marionette/);
  const shot = JSON.parse(await python("screenshot"));
  assert.equal(fs.existsSync(shot.screenshot_path), true);
  assert.ok(shot.width >= 600);
  const point = await webContents.fromId(context.tabs[0].webContentsId).executeJavaScript("(()=>{const r=document.querySelector('#apply').getBoundingClientRect();return {x:(r.x+r.width/2)*" + shot.width + "/innerWidth,y:(r.y+r.height/2)*" + shot.height + "/innerHeight}})()");
  assert.match(await python("input", { operation: "click", snapshot_id: shot.snapshot_id, ...point }), /Input sent/);
  assert.match(await python("get_text"), /Saved Marionette/);
  const nextShot = JSON.parse(await python("screenshot"));
  assert.match(await python("input", { operation: "keypress", snapshot_id: nextShot.snapshot_id, keys: ["Tab"] }), /Input sent/);
  assert.match(await python("input", { operation: "keypress", snapshot_id: shot.snapshot_id, keys: ["Enter"] }), /Stale/);
  assert.match(await python("tab_activate", { tab_id: "two" }), /Activated/);
  assert.equal(JSON.parse(await python("tabs"))[1].active, true);
  assert.match(await python("click", { ref: button }), /Stale/);
  assert.match(await python("navigate", { url: "http://127.0.0.1:" + server.address().port + "/next" }), /Navigated/);
  assert.match(await python("snapshot"), /After navigation/);
  assert.match(await python("snapshot", {}, "wrong-session"), /active conversation/);
  controller.clear();
  assert.match(await python("snapshot"), /active conversation/);
  console.log("PASS: Python tools -> authenticated bridge -> two real Electron webviews; snapshot, native typing/click, screenshot, activation, navigation, stale refs, wrong session, closed pane.");
}).catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  controller?.clear(); bridge?.close(); win?.destroy(); server?.close(); app.exit(process.exitCode || 0);
});
