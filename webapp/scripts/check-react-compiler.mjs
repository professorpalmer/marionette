// Report components the React Compiler skips. It bails silently at build time
// (no warning), so a hot component can lose automatic memoization unnoticed.
// Usage: node scripts/check-react-compiler.mjs [--strict] [files...]
// --strict exits 1 when a required component is skipped.
import { transformAsync } from "@babel/core";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// Hot render paths that must stay compiled. TranscriptList itself is left out:
// the compiler correctly refuses it for the virtualizer hook (mutable API).
const REQUIRED = {
  "src/components/TranscriptList.tsx": [
    "ActivityGroup", "ThinkingBlock", "FencedCodeBlock", "Markdown", "ActionCard", "SwarmResultCard",
  ],
};

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (name === "__tests__" || name === "node_modules") continue;
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx$/.test(name)) out.push(p);
  }
  return out;
}

async function skipsIn(file) {
  const skipped = [];
  const compiled = [];
  const code = readFileSync(file, "utf8");
  await transformAsync(code, {
    filename: file,
    babelrc: false,
    configFile: false,
    parserOpts: { plugins: ["typescript", "jsx"] },
    plugins: [["babel-plugin-react-compiler", {
      logger: {
        logEvent(_filename, event) {
          const name = event.fnName ?? event.fnLoc?.identifierName ?? "(anonymous)";
          if (event.kind === "CompileSuccess") compiled.push(name);
          else if (event.kind === "CompileError" || event.kind === "CompileSkip" || event.kind === "PipelineError") {
            const detail = event.detail?.reason ?? event.detail?.options?.reason ?? event.reason ?? event.data ?? "";
            const line = event.fnLoc?.start?.line ?? event.detail?.loc?.start?.line ?? event.detail?.options?.loc?.start?.line;
            skipped.push({ name, kind: event.kind, line, reason: String(detail).split("\n")[0].slice(0, 140) });
          }
        },
      },
    }]],
  });
  return { skipped, compiled };
}

const args = process.argv.slice(2);
const strict = args.includes("--strict");
const files = args.filter((a) => a !== "--strict");
const targets = files.length ? files : walk("src");
let failures = 0;
for (const file of targets) {
  const rel = relative(process.cwd(), file);
  const { skipped, compiled } = await skipsIn(file);
  for (const s of skipped) console.log(`${rel}:${s.line ?? "?"} ${s.name} ${s.kind}: ${s.reason}`);
  for (const name of REQUIRED[rel] ?? []) {
    if (!compiled.includes(name)) {
      failures += 1;
      console.log(`REQUIRED NOT COMPILED: ${rel} ${name}`);
    }
  }
}
if (strict && failures) process.exit(1);
