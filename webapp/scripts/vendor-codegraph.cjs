const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

function getVendoredSize(dir) {
  let size = 0;
  const files = fs.readdirSync(dir);
  for (const file of files) {
    const filePath = path.join(dir, file);
    const stats = fs.statSync(filePath);
    if (stats.isDirectory()) {
      size += getVendoredSize(filePath);
    } else {
      size += stats.size;
    }
  }
  return size;
}

function findCodegraph() {
  // 1. Try via npm root -g
  try {
    const npmRoot = execSync("npm root -g", { encoding: "utf8" }).trim();
    const p = path.join(npmRoot, "@colbymchenry/codegraph");
    if (fs.existsSync(p)) {
      return p;
    }
  } catch (e) {
    console.log("npm root -g failed or codegraph not there:", e.message);
  }

  // 2. Try common system paths
  const commonPaths = [
    "/opt/homebrew/lib/node_modules/@colbymchenry/codegraph",
    "/usr/local/lib/node_modules/@colbymchenry/codegraph",
    path.join(process.env.HOME || "", ".local/lib/node_modules/@colbymchenry/codegraph")
  ];
  for (const p of commonPaths) {
    if (fs.existsSync(p)) {
      return p;
    }
  }

  // 3. Fallback: npm pack
  console.log("Global codegraph not found. Trying npm pack...");
  const tempDir = path.join(__dirname, "..", "codegraph-temp-pack");
  if (fs.existsSync(tempDir)) {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
  fs.mkdirSync(tempDir, { recursive: true });
  try {
    execSync("npm pack @colbymchenry/codegraph", { cwd: tempDir, stdio: "inherit" });
    const files = fs.readdirSync(tempDir).filter(f => f.endsWith(".tgz"));
    if (files.length > 0) {
      const tgzPath = path.join(tempDir, files[0]);
      execSync(`tar -xzf ${tgzPath} --strip-components=1`, { cwd: tempDir });
      execSync("npm install --production --no-audit --no-fund", { cwd: tempDir, stdio: "inherit" });
      return tempDir;
    }
  } catch (e) {
    console.error("npm pack fallback failed:", e.message);
  }

  return null;
}

const source = findCodegraph();
if (!source) {
  console.error("Error: Could not locate @colbymchenry/codegraph globally or via npm pack.");
  process.exit(1);
}

console.log(`Found codegraph source at: ${source}`);

const dest = path.join(__dirname, "..", "codegraph-vendor");
if (fs.existsSync(dest)) {
  console.log(`Cleaning existing vendor dir: ${dest}`);
  fs.rmSync(dest, { recursive: true, force: true });
}

console.log(`Copying codegraph to vendor dir: ${dest}`);
fs.cpSync(source, dest, { recursive: true, force: true });

// If we used the temp pack, clean it up
const tempDir = path.join(__dirname, "..", "codegraph-temp-pack");
if (source === tempDir && fs.existsSync(tempDir)) {
  fs.rmSync(tempDir, { recursive: true, force: true });
}

// NOTE: we KEEP the native better-sqlite3 build. The electron-as-node approach was abandoned
// (codegraph worker_threads recurse under ELECTRON_RUN_AS_NODE -- see .hermes/plans verdict).
// The viable path bundles a REAL node binary, which needs the native module (rebuilt for that
// node's ABI at package time). Do NOT prune it.

// Print vendored size
const sizeInBytes = getVendoredSize(dest);
const sizeInMB = (sizeInBytes / (1024 * 1024)).toFixed(2);
console.log(`Vendoring complete. Total size: ${sizeInMB} MB`);
