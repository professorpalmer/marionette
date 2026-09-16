const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { fileURLToPath } = require('node:url');

const CONTROL = /[\x00-\x1f\x7f]/;
const UNSAFE = /\.(app|exe|com|command|sh|bash|zsh|fish|ps1|psm1|bat|cmd|py|pyw|js|mjs|cjs|vbs|vbe|wsf|wsh|scr|msi|lnk|url|webloc|desktop|jar|workflow|scpt|applescript|jse|hta|cpl|pif|reg|scf|gadget|application|pl|rb|php)(?:[\\/]|$)/i;

function localPath(input) {
  if (typeof input !== 'string' || !input || CONTROL.test(input)) throw new Error('Invalid local path');
  let target = input;
  if (/^file:/i.test(target)) {
    const url = new URL(target);
    if (url.hostname && url.hostname !== 'localhost') throw new Error('Remote file hosts are not supported');
    if (url.search || url.hash) throw new Error('File URL must encode literal query or fragment characters');
    target = fileURLToPath(url);
  } else if (/^[a-z][a-z\d+.-]*:/i.test(target) && !/^[A-Za-z]:[\\/]/.test(target)) {
    throw new Error('Unsupported path scheme');
  }
  if (target.startsWith('~/')) target = path.join(os.homedir(), target.slice(2));
  if (CONTROL.test(target) || /^[\\/]{2}/.test(target)) throw new Error('Invalid or network path');
  if (process.platform === 'win32' ? !/^[A-Za-z]:[\\/]/.test(target) || target.slice(2).includes(':') : !target.startsWith('/') || /^\/[A-Za-z]:[\\/]/.test(target) || target.includes('\\')) {
    throw new Error('An absolute path for this computer is required');
  }
  return target;
}

async function openLocalPath(input, shell) {
  try {
    const requested = localPath(input);
    const target = await fs.realpath(requested);
    // Revalidate symlink destinations, including network shares on Windows.
    localPath(target);
    const stat = await fs.stat(target);
    if (!stat.isFile() && !stat.isDirectory()) throw new Error('Only regular files and folders can be opened');
    if (UNSAFE.test(requested) || UNSAFE.test(target) || (stat.isFile() && process.platform !== 'win32' && (stat.mode & 0o111))) {
      shell.showItemInFolder(target);
      return { ok: true, action: 'revealed' };
    }
    const error = await shell.openPath(target);
    return error ? { ok: false, error } : { ok: true, action: 'opened' };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}
module.exports = { openLocalPath };
