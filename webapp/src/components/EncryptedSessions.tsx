import { useCallback, useEffect, useState } from 'react';
import { getHarnessIpc, getJSON } from '../lib/transport';

type SealReply<T> = ({ ok: true } & T) | { ok: false; code: string; error: string };
type KeyStatus = { configured: boolean; enabled: boolean; protection: 'os' | 'none'; keyAvailable: boolean };
type BackendStatus = { configured: boolean; enabled: boolean; unlocked: boolean };
type SealBridge = {
  status: () => Promise<SealReply<KeyStatus>>;
  enable: () => Promise<SealReply<{ recoveryCode: string | null }>>;
  disable: () => Promise<SealReply<Record<string, never>>>;
  recover: (code: string) => Promise<SealReply<Record<string, never>>>;
};

const button = 'min-h-11 px-3 py-2 rounded-md border border-edge text-txt hover:bg-panel2 disabled:opacity-50';
const input = 'min-h-11 rounded-md border border-edge bg-bg text-txt px-3 py-2 w-full font-mono';

function isSealBridge(value: unknown): value is SealBridge {
  if (typeof value !== 'object' || value === null) return false;
  const fields = value as Record<string, unknown>;
  return ['status', 'enable', 'disable', 'recover'].every((name) => typeof fields[name] === 'function');
}

function sealBridge(): SealBridge | null {
  const seal: unknown = getHarnessIpc()?.seal;
  return isSealBridge(seal) ? seal : null;
}

export default function EncryptedSessions() {
  const bridge = sealBridge();
  const [keys, setKeys] = useState<KeyStatus | null>(null);
  const [backend, setBackend] = useState<BackendStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [recoveryCode, setRecoveryCode] = useState('');
  const [copied, setCopied] = useState(false);
  const [code, setCode] = useState('');

  const load = useCallback(async () => {
    if (!bridge) return;
    const [k, b] = await Promise.all([bridge.status(), getJSON<BackendStatus>('/api/seal/status')]);
    if (k.ok) setKeys(k);
    else setError(k.error);
    setBackend(b);
  }, [bridge]);

  useEffect(() => {
    load().catch(() => setError('Could not read the encryption status.'));
  }, [load]);

  async function run<T>(action: () => Promise<SealReply<T>>, done?: (r: { ok: true } & T) => void) {
    setBusy(true);
    setError('');
    try {
      const reply = await action();
      if (!reply.ok) setError(reply.error);
      else done?.(reply);
      await load();
    } catch {
      setError('The request failed. Try again.');
    } finally {
      setBusy(false);
    }
  }

  if (!bridge) {
    return <p className="text-ui-11 text-muted">Encrypted sessions need the desktop app.</p>;
  }
  if (!keys || !backend) {
    return <p className="text-ui-11 text-muted">{error || 'Loading...'}</p>;
  }
  const locked = keys.configured && !backend.unlocked;

  return (
    <div className="space-y-3">
      <p className="text-ui-11 text-muted">
        New sessions are encrypted on disk with a key in your OS key store. Existing sessions do not
        change. The model still gets the full conversation.
      </p>
      {keys.protection === 'none' ? (
        <p className="text-ui-11 text-warn">
          This system has no OS key store (Keychain, Credential Manager, or Secret Service).
          Encrypted sessions are not available.
        </p>
      ) : (
        <label className="flex items-center gap-2 text-ui-12 text-txt">
          <input
            type="checkbox"
            checked={keys.enabled}
            disabled={busy || (locked && !keys.enabled)}
            onChange={(e) => {
              if (e.target.checked) {
                void run(() => bridge.enable(), (r) => { if (r.recoveryCode) setRecoveryCode(r.recoveryCode); });
              } else {
                void run(() => bridge.disable());
              }
            }}
          />
          Encrypt new sessions
        </label>
      )}
      {recoveryCode && (
        <div className="space-y-2 rounded-md border border-warn/60 p-3">
          <p className="text-ui-11 text-txt">
            Save this recovery code now. It shows one time only. Without it, a lost OS key store
            makes encrypted sessions unreadable.
          </p>
          <code className="block select-all text-ui-13 font-mono text-txt">{recoveryCode}</code>
          <div className="flex gap-2">
            <button
              type="button"
              className={button}
              onClick={() => {
                navigator.clipboard.writeText(recoveryCode).then(() => setCopied(true), () => setCopied(false));
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button type="button" className={button} onClick={() => { setRecoveryCode(''); setCopied(false); }}>
              I saved the code
            </button>
          </div>
        </div>
      )}
      {locked && (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void run(() => bridge.recover(code), () => setCode(''));
          }}
        >
          <p className="text-ui-11 text-warn">
            Encrypted sessions are locked: the OS key store did not give the key. Enter your
            recovery code to unlock them.
          </p>
          <input
            className={input}
            aria-label="Recovery code"
            autoComplete="off"
            spellCheck={false}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button type="submit" className={button} disabled={busy || !code.trim()}>Unlock</button>
        </form>
      )}
      {error && <p className="text-ui-11 text-risk" role="alert">{error}</p>}
      <ul className="list-disc pl-4 text-ui-10 text-muted space-y-1">
        <li>Encrypted: transcripts, titles, attachments, tool output, search index, todos, goal, scratch, and memory that encrypted sessions save.</li>
        <li>Not encrypted: your workspace files, and what providers and MCP servers receive.</li>
        <li>Encrypted sessions do not use workers, the browser tool, or CLI pilots yet.</li>
        <li>Deleting an encrypted session destroys its key.</li>
      </ul>
    </div>
  );
}
