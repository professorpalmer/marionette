"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");
const fixture = path.join(__dirname, "fixtures", "secret-request-card.html");
const outDir = path.join(__dirname, "screenshots");

app.whenReady().then(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const win = new BrowserWindow({
    width: 720,
    height: 420,
    show: false,
    backgroundColor: "#0f1113",
    webPreferences: { sandbox: true },
  });
  await win.loadFile(fixture);
  await new Promise((r) => setTimeout(r, 200));
  const image = await win.webContents.capturePage();
  const dest = path.join(outDir, "secret-request-card.png");
  fs.writeFileSync(dest, image.toPNG());
  process.stdout.write(dest + "\n");
  app.exit(0);
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  app.exit(1);
});
