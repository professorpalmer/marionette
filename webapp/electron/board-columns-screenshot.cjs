"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const fixture = path.join(__dirname, "fixtures", "board-columns.html");
const outDir = path.join(__dirname, "screenshots");

async function capture(win, name) {
  const image = await win.webContents.capturePage();
  const dest = path.join(outDir, name);
  fs.writeFileSync(dest, image.toPNG());
  return dest;
}

app.whenReady().then(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const win = new BrowserWindow({
    width: 1200,
    height: 720,
    show: false,
    backgroundColor: "#0f1113",
    webPreferences: { sandbox: true },
  });
  await win.loadFile(fixture);
  await new Promise((resolve) => setTimeout(resolve, 250));
  const before = await win.webContents.executeJavaScript("window.__boardState()");
  const beforePath = await capture(win, "three-columns-before.png");
  const after = await win.webContents.executeJavaScript("window.__resizeGroup(1, 6)");
  await win.webContents.executeJavaScript(
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
  );
  await new Promise((resolve) => setTimeout(resolve, 100));
  const afterPath = await capture(win, "three-columns-after.png");
  process.stdout.write(JSON.stringify({
    before,
    after,
    screenshots: { before: beforePath, after: afterPath },
  }));
  app.exit(0);
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  app.exit(1);
});
