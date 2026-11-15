import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { SETTINGS_PRIMARY_BUTTON, SETTINGS_QUIET_BUTTON, SettingsGroup, SettingsRow } from './SettingsLayout';
import Switch from './SettingsSwitch';
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

  const group = (rows: ReactNode) => (
    <SettingsGroup
      title="Encrypted sessions"
      description="New sessions are encrypted on disk with a key in your OS key store. Existing sessions do not change."
      testId="encrypted-sessions"
    >
      {rows}
    </SettingsGroup>
  );

  if (!bridge) {
    return group(<SettingsRow label="Encrypt new sessions" description="Encrypted sessions need the desktop app." />);
  }
  if (!keys || !backend) {
    return group(<SettingsRow label="Encrypt new sessions" state={error || 'Loading...'} />);
  }
  const locked = keys.configured && !backend.unlocked;
  const state = keys.protection === 'none' ? 'Not available' : locked ? 'Locked' : keys.enabled ? 'On' : 'Off';

  return group(<>
    <SettingsRow
      label="Encrypt new sessions"
      description={keys.protection === 'none'
        ? 'This system has no OS key store (Keychain, Credential Manager, or Secret Service).'
        : 'The model still gets the full conversation.'}
      state={state}
      control={keys.protection === 'none' ? undefined : (
        <Switch
          on={keys.enabled}
          label="Encrypt new sessions"
          disabled={busy || (locked && !keys.enabled)}
          onChange={(on) => {
            if (on) {
              void run(() => bridge.enable(), (r) => { if (r.recoveryCode) setRecoveryCode(r.recoveryCode); });
            } else {
              void run(() => bridge.disable());
            }
          }}
        />
      )}
    >
      {recoveryCode && (
        <div className="space-y-2 rounded-md border border-warn/60 p-2.5">
          <p className="text-ui-11 text-txt">
            Save this recovery code now. It shows one time only. Without it, a lost OS key store
            makes encrypted sessions unreadable.
          </p>
          <code className="block select-all text-ui-13 font-mono text-txt">{recoveryCode}</code>
          <div className="flex gap-3">
            <button
              type="button"
              className={SETTINGS_PRIMARY_BUTTON}
              onClick={() => {
                navigator.clipboard.writeText(recoveryCode).then(() => setCopied(true), () => setCopied(false));
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => { setRecoveryCode(''); setCopied(false); }}>
              I saved the code
            </button>
          </div>
        </div>
      )}
      {locked && (
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            void run(() => bridge.recover(code), () => setCode(''));
          }}
        >
          <p className="text-ui-10 text-warn">
            Encrypted sessions are locked: the OS key store did not give the key. Enter your
            recovery code to unlock them.
          </p>
          <div className="flex items-center gap-2">
            <input
              className="flex-1 bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 font-mono text-txt focus:outline-none focus:border-accent"
              aria-label="Recovery code"
              autoComplete="off"
              spellCheck={false}
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <button type="submit" className={SETTINGS_PRIMARY_BUTTON} disabled={busy || !code.trim()}>Unlock</button>
          </div>
        </form>
      )}
      {error && <p className="text-ui-10 text-risk" role="alert">{error}</p>}
    </SettingsRow>
    <SettingsRow label="What is encrypted">
      <ul className="list-disc pl-4 text-ui-10 text-faint space-y-0.5">
        <li>Encrypted: transcripts, titles, attachments, tool output, search index, todos, goal, scratch, and memory that encrypted sessions save.</li>
        <li>Not encrypted: your workspace files, and what providers and MCP servers receive.</li>
        <li>Workers run only on the agentic adapter. A job's files are plain text on disk while the job runs. Then Marionette deletes them.</li>
        <li>Flows, the browser tool, and CLI pilots are off in encrypted sessions.</li>
        <li>Deleting an encrypted session destroys its key.</li>
      </ul>
    </SettingsRow>
  </>);
}
