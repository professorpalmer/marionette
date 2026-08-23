"use strict";

const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const fixture = path.join(__dirname, "fixtures", "feed-lurch.html");

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 800,
    height: 560,
    show: false,
    backgroundColor: "#111111",
    webPreferences: { sandbox: true },
  });
  await win.loadFile(fixture);
  await new Promise((r) => setTimeout(r, 150));
  const pinned = await win.webContents.executeJavaScript("window.__pinToEnd()");
  const frames = [];
  for (let i = 0; i < 12; i++) {
    const step = await win.webContents.executeJavaScript("window.__growTail(2)");
    frames.push(step);
    await win.webContents.executeJavaScript(
      "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    );
  }
  const lurches = frames.filter((f) => {
    const jumped = Math.abs(f.after.scrollTop - (f.before.scrollTop + f.deltaHeight)) > 2;
    const crossed = f.before.overflow <= 1 && f.after.overflow > 8 && f.after.distance > 2;
    return jumped || crossed;
  });
  process.stdout.write(JSON.stringify({
    ok: lurches.length === 0,
    pinned,
    frames: frames.length,
    lurches: lurches.length,
    last: frames[frames.length - 1],
  }));
  app.exit(lurches.length ? 2 : 0);
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  app.exit(1);
});
