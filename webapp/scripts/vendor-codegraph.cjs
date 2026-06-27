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

// ---- Strip symlinks that escape the vendor dir ----
// npm leaves absolute .bin shims (e.g. node_modules/.bin/semver -> /opt/homebrew/...)
// in the global install. codesign REFUSES to sign an .app bundle that contains a
// symlink pointing outside the bundle ("invalid destination for symbolic link in
// bundle"), which breaks the signed DMG build. These .bin shims are CLI entrypoints
// the packaged app never invokes (it calls the codegraph binary directly), so we
// remove any symlink whose resolved target falls outside the vendor dir. Internal
// relative symlinks (if any) that stay inside the bundle are preserved.
(function stripExternalSymlinks(root) {
  const destReal = fs.realpathSync(root);
  let removed = 0;
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        let target;
        try {
          target = fs.realpathSync(full);
        } catch (_) {
          // dangling/broken symlink -> remove it (also illegal in a signed bundle)
          fs.rmSync(full, { force: true });
          removed++;
          continue;
        }
        if (!target.startsWith(destReal + path.sep) && target !== destReal) {
          fs.rmSync(full, { force: true });
          removed++;
        }
      } else if (entry.isDirectory()) {
        walk(full);
      }
    }
  };
  walk(root);
  console.log(`Stripped ${removed} external/broken symlink(s) from the vendor dir (codesign safety).`);
})(dest);

// NOTE: we KEEP the native better-sqlite3 build. The electron-as-node approach was abandoned
// (codegraph worker_threads recurse under ELECTRON_RUN_AS_NODE -- see .hermes/plans verdict).
// The viable path bundles a REAL node binary, which needs the native module (rebuilt for that
// node's ABI at package time). Do NOT prune it.

// Print vendored size
const sizeInBytes = getVendoredSize(dest);
const sizeInMB = (sizeInBytes / (1024 * 1024)).toFixed(2);
console.log(`Vendoring complete. Total size: ${sizeInMB} MB`);


// ---- Bundle a real node binary (ABI must match codegraph's prebuilt better_sqlite3) ----
// electron-as-node does NOT work (codegraph worker_threads recurse). We ship a real node binary.
// The prebuilt better_sqlite3 in codegraph is Node ABI 127 (node v22), so we bundle node v22.
(function vendorNode() {
  const { execSync } = require("child_process");
  const nodeVendorDir = path.join(__dirname, "..", "node-vendor");
  // Resolve the real node binary path (follow symlinks).
  let nodeBin = "";
  try {
    nodeBin = execSync("node -e \"process.stdout.write(process.execPath)\"", { encoding: "utf8" }).trim();
    nodeBin = fs.realpathSync(nodeBin);
  } catch (e) {
    console.error("Could not resolve node binary to vendor:", e.message);
    return;
  }
  // Sanity: warn if the vendored node ABI will not match codegraph's native module.
  try {
    const abi = execSync(`"${nodeBin}" -e "process.stdout.write(process.versions.modules)"`, { encoding: "utf8" }).trim();
    if (abi !== "127") {
      console.warn(`WARNING: bundling node ABI ${abi} but codegraph better_sqlite3 expects ABI 127 (node v22). codegraph may fail to load SQLite. Use a node v22 binary.`);
    }
  } catch (_) {}
  fs.rmSync(nodeVendorDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(nodeVendorDir, "bin"), { recursive: true });
  const dest = path.join(nodeVendorDir, "bin", "node");
  fs.copyFileSync(nodeBin, dest);
  fs.chmodSync(dest, 0o755);
  const sz = (fs.statSync(dest).size / (1024 * 1024)).toFixed(1);
  console.log(`Vendored node binary (${sz} MB) from ${nodeBin} -> ${dest}`);
})();
