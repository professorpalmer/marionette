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

  // The bundled node's ABI MUST match codegraph's prebuilt better_sqlite3, or the
  // packaged app falls back to WASM SQLite and then cannot even open the existing
  // native-format .codegraph DB (exit 1 -> the panel shows UNSUPPORTED). codegraph
  // ships a binding for Node ABI 127 (node v22). The build machine's default `node`
  // may be a newer major (e.g. v23 = ABI 131), so we must NOT blindly vendor PATH's
  // node. Probe candidate binaries and pick one whose ABI matches; fail LOUDLY if
  // none is found rather than silently shipping a broken codegraph.
  const REQUIRED_ABI = "127";
  const abiOf = (bin) => {
    try {
      return execSync(`"${bin}" -e "process.stdout.write(process.versions.modules)"`, { encoding: "utf8" }).trim();
    } catch (_) {
      return "";
    }
  };
  // A candidate is only usable if it runs STANDALONE after a plain file copy. Homebrew
  // node kegs dynamically link @rpath/libnode.NNN.dylib, so copying just bin/node yields
  // a binary that aborts with "Library not loaded" once detached from the keg. Official
  // node.js distro binaries (and ~/.hermes/node) are self-contained. Validate by copying
  // to a temp path and executing it there.
  const isStandalone = (bin) => {
    const os = require("os");
    const tmp = path.join(os.tmpdir(), `vn-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    try {
      fs.copyFileSync(bin, tmp);
      fs.chmodSync(tmp, 0o755);
      const out = execSync(`"${tmp}" -e "process.stdout.write(process.versions.modules)"`, { encoding: "utf8" }).trim();
      return out === REQUIRED_ABI;
    } catch (_) {
      return false;
    } finally {
      try { fs.rmSync(tmp, { force: true }); } catch (_) {}
    }
  };
  const which = (cmd) => {
    try {
      return execSync(`command -v ${cmd}`, { encoding: "utf8", shell: "/bin/bash" }).trim();
    } catch (_) {
      return "";
    }
  };
  // Candidate node binaries, in preference order: explicit env override, then any
  // node@22 keg / nvm v22, then PATH's node as a last resort.
  const candidates = [];
  if (process.env.PMHARNESS_VENDOR_NODE) candidates.push(process.env.PMHARNESS_VENDOR_NODE);
  // Prefer self-contained official-distro nodes (no libnode.dylib dependency).
  candidates.push(`${process.env.HOME || ""}/.hermes/node/bin/node`);
  for (const g of [
    "/opt/homebrew/opt/node@22/bin/node",
    "/usr/local/opt/node@22/bin/node",
    "/opt/homebrew/Cellar/node@22",
    `${process.env.HOME || ""}/.nvm/versions/node`,
  ]) {
    try {
      if (g.includes("Cellar") || g.includes(".nvm")) {
        // expand a versions dir: pick the highest v22.x
        if (fs.existsSync(g)) {
          for (const v of fs.readdirSync(g).sort().reverse()) {
            if (v.startsWith("22") || v.startsWith("v22")) {
              candidates.push(path.join(g, v, "bin", "node"));
            }
          }
        }
      } else if (fs.existsSync(g)) {
        candidates.push(g);
      }
    } catch (_) {}
  }
  const pathNode = which("node");
  if (pathNode) candidates.push(pathNode);

  let nodeBin = "";
  for (const c of candidates) {
    try {
      const real = fs.realpathSync(c);
      // Must both match ABI AND survive a standalone copy (Homebrew kegs fail the latter).
      if (abiOf(real) === REQUIRED_ABI && isStandalone(real)) {
        nodeBin = real;
        break;
      }
    } catch (_) {}
  }
  if (!nodeBin) {
    const tried = candidates.join(", ") || "(none found)";
    throw new Error(
      `vendor-codegraph: could not find a node binary with ABI ${REQUIRED_ABI} ` +
      `(required by codegraph's better_sqlite3). Tried: ${tried}. ` +
      `Install node v22 (e.g. 'brew install node@22') or set PMHARNESS_VENDOR_NODE ` +
      `to a v22 node binary. Refusing to ship a codegraph that falls back to WASM.`
    );
  }
  console.log(`Selected node ABI ${REQUIRED_ABI} for vendoring: ${nodeBin}`);
  fs.rmSync(nodeVendorDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(nodeVendorDir, "bin"), { recursive: true });
  const dest = path.join(nodeVendorDir, "bin", "node");
  fs.copyFileSync(nodeBin, dest);
  fs.chmodSync(dest, 0o755);
  const sz = (fs.statSync(dest).size / (1024 * 1024)).toFixed(1);
  console.log(`Vendored node binary (${sz} MB) from ${nodeBin} -> ${dest}`);
})();
