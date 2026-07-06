#!/usr/bin/env node
/**
 * Generate platform icons from build/assets/icon-source.png.
 * - build/icon.ico (Windows): multi-size ICO with alpha preserved
 * - build/icon.png (Linux/fallback): 512x512 PNG with alpha
 *
 * Mac .icns is still produced by scripts/make_icon.sh (iconutil/sips).
 * Run: npm run make-icon   (from webapp/)
 */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import pngToIco from "png-to-ico";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webappRoot = path.resolve(__dirname, "..");
const src = path.join(webappRoot, "build/assets/icon-source.png");
const buildDir = path.join(webappRoot, "build");
const outIco = path.join(buildDir, "icon.ico");
const outPng = path.join(buildDir, "icon.png");

const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256];

async function main() {
  const srcBuf = await readFile(src);
  const png512 = await sharp(srcBuf)
    .resize(512, 512, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } })
    .png()
    .toBuffer();
  await writeFile(outPng, png512);

  const pngBuffers = await Promise.all(
    ICO_SIZES.map((size) =>
      sharp(srcBuf)
        .resize(size, size, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } })
        .png()
        .toBuffer()
    )
  );
  const ico = await pngToIco(pngBuffers);
  await writeFile(outIco, ico);

  console.log(`Wrote ${outIco} (${ICO_SIZES.join(", ")}px with alpha)`);
  console.log(`Wrote ${outPng} (512x512 with alpha)`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
