import { useEffect, useRef, useState } from 'react';
import { SETTINGS_PRIMARY_BUTTON, SETTINGS_QUIET_BUTTON, SettingsGroup, SettingsRow } from './SettingsLayout';
import { DeviceAccessClient, type Device, type DeviceEndpoint, type DeviceGrant, type Enrollment } from '../lib/deviceAccess';

const input = 'flex-1 bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 text-txt focus:outline-none focus:border-accent';
const grantLabels = {
  'endpoint.read': 'Endpoint metadata',
  'session.read': 'Session metadata',
  'session.events.read': 'Retained content/events',
} satisfies Record<DeviceGrant['operation'], string>;

export default function DeviceAccess() {
  const connection = useRef<DeviceAccessClient | null>(null);
  const secretFocus = useRef<HTMLButtonElement>(null);
  const labelFocus = useRef<HTMLInputElement>(null);
  const hadSecret = useRef(false);
  const refreshFocus = useRef<HTMLButtonElement>(null);
  const returnToRefresh = useRef(false);
  const [endpoint, setEndpoint] = useState<DeviceEndpoint | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [label, setLabel] = useState('');
  const [session, setSession] = useState('');
  const [grants, setGrants] = useState<DeviceGrant[]>([]);
  const [secret, setSecret] = useState<Enrollment | null>(null);
  const [shown, setShown] = useState(false);
  const [copyStatus, setCopyStatus] = useState('');
  const [uncertain, setUncertain] = useState(false);

  useEffect(() => {
    const client = new DeviceAccessClient();
    connection.current = client;
    void (async () => {
      try {
        const identity = await client.connect();
        if (connection.current !== client) return;
        setEndpoint(identity);
        if (identity.supported) {
          const list = await client.list();
          if (connection.current === client) setDevices(list);
        }
      } catch { if (connection.current === client) setError('Could not load device access. Leave this page and open it again to reconnect.'); }
      finally { if (connection.current === client) setBusy(false); }
    })();
    return () => { connection.current = null; client.close(); };
  }, []);
  useEffect(() => {
    if (secret) secretFocus.current?.focus();
    else if (hadSecret.current) labelFocus.current?.focus();
    hadSecret.current = secret !== null;
  }, [secret]);
  useEffect(() => {
    if (!busy && returnToRefresh.current) {
      returnToRefresh.current = false;
      refreshFocus.current?.focus();
    }
  }, [busy]);

  function toggle(grant: DeviceGrant) {
    setGrants(previous => previous.some(g => g.operation === grant.operation && g.resource_id === grant.resource_id)
      ? previous.filter(g => g.operation !== grant.operation || g.resource_id !== grant.resource_id) : [...previous, grant]);
  }
  async function refresh() {
    const client = connection.current;
    if (!client || busy) return;
    setBusy(true); setError('');
    try {
      const list = await client.list();
      if (connection.current === client) setDevices(list);
    } catch { if (connection.current === client) setError('Could not refresh devices. If the backend changed, leave this page and open it again.'); }
    finally { if (connection.current === client) setBusy(false); }
  }
  async function enroll() {
    const client = connection.current;
    if (!client || busy) return;
    setBusy(true); setError('');
    try {
      const result = await client.enroll(label, grants);
      if (connection.current !== client) return;
      setSecret(result); setShown(false); setCopyStatus('');
      setDevices(previous => [...previous, { device_id: result.device_id, label, grants: [...grants], created_at: Date.now() / 1000, revoked_at: null, grants_revision: 1 }]);
      setLabel(''); setGrants([]); setSession('');
    } catch {
      if (connection.current === client) {
        setUncertain(true);
        setError('Enrollment outcome is unknown. Refresh the list before you try again. If the one-time credential did not arrive, revoke the new device.');
      }
    } finally { if (connection.current === client) setBusy(false); }
  }
  async function revoke(id: string, restoreFocus: boolean) {
    const client = connection.current;
    if (!client || busy) return;
    setBusy(true); setError('');
    try {
      await client.revoke(id);
      if (connection.current !== client) return;
      returnToRefresh.current = restoreFocus;
      setDevices(previous => previous.map(d => d.device_id === id ? { ...d, revoked_at: d.revoked_at ?? Date.now() / 1000, grants_revision: d.revoked_at === null ? d.grants_revision + 1 : d.grants_revision } : d));
      if (secret?.device_id === id) { setSecret(null); setShown(false); setCopyStatus(''); }
    } catch { if (connection.current === client) setError('Marionette could not confirm the revocation. Refresh the list. If the device is still active, revoke it again.'); }
    finally { if (connection.current === client) setBusy(false); }
  }
  async function copy() {
    if (!secret) return;
    const client = connection.current;
    try {
      await navigator.clipboard.writeText(secret.credential);
      if (connection.current === client) setCopyStatus('Copied. Keep it in a private place.');
    } catch { if (connection.current === client) setCopyStatus('Copy is not available. Click Show credential and copy it by hand.'); }
  }
  const enabled = endpoint?.supported === true;
  const active = devices.filter(d => d.revoked_at === null).length;
  return <SettingsGroup
    title="Device access"
    description="Give a device read-only access to the data that you choose on this computer. A device cannot run commands, read files, or control a session."
    testId="device-access"
  >
    <SettingsRow
      label="Enrolled devices"
      description={endpoint ? <span className="break-all">Endpoint {endpoint.endpoint_id}, boot {endpoint.boot_id}</span> : undefined}
      state={busy ? <span role="status">Loading...</span> : enabled ? `${active} active` : endpoint ? 'Not available' : undefined}
      control={enabled ? <button type="button" ref={refreshFocus} className={SETTINGS_QUIET_BUTTON} disabled={busy} onClick={() => void refresh()}>Refresh devices</button> : undefined}
    >
      {error && <p role="alert" className="text-ui-10 text-risk">{error}</p>}
      {endpoint && !enabled && <p className="text-ui-10 text-faint">Device access is disabled or unsupported on this platform. No enrollment or access changes are available.</p>}
      {enabled && !devices.length && !busy && <p className="text-ui-10 text-faint">No devices enrolled.</p>}
    </SettingsRow>
    {enabled && devices.map(device => <SettingsRow
      key={device.device_id}
      label={<span className="break-all">{device.label}</span>}
      description={<>
        <span className="block break-all">Device ID: {device.device_id}</span>
        {device.grants.map(g => <span className="block break-all" key={`${g.operation}:${g.resource_id}`}>{grantLabels[g.operation]}: {g.resource_id}</span>)}
        <span className="block">Grant revision: {device.grants_revision}</span>
      </>}
      state={device.revoked_at === null ? 'Active' : 'Revoked'}
      control={<button type="button" className={SETTINGS_QUIET_BUTTON} disabled={busy || device.revoked_at !== null} onClick={event => void revoke(device.device_id, document.activeElement === event.currentTarget)} aria-label={`Revoke ${device.label}`}>{device.revoked_at === null ? 'Revoke' : 'Revoked'}</button>}
    />)}
    {enabled && <SettingsRow
      label="Enroll a device"
      description="The device gets a one-time credential. It can read only the items that you add here."
    >
      <div className="space-y-2 text-ui-11 text-muted">
        {secret && <div className="space-y-2 rounded-md border border-warn/60 p-2.5" aria-label="One-time credential">
          <p className="text-txt">Device enrolled: <span className="break-all">{secret.device_id}</span>. This credential shows only now. Dismiss it or close Settings, and it is gone from this page.</p>
          <div className="flex flex-wrap gap-3">
            <button type="button" ref={secretFocus} className={SETTINGS_PRIMARY_BUTTON} onClick={() => setShown(v => !v)}>{shown ? 'Hide credential' : 'Show credential'}</button>
            <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => void copy()}>Copy credential</button>
            <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => { setSecret(null); setShown(false); setCopyStatus(''); }}>Dismiss credential</button>
          </div>
          {shown && <pre className="whitespace-pre-wrap break-all select-all font-mono text-txt">{secret.credential}</pre>}
          {copyStatus && <p role="status">{copyStatus}</p>}
        </div>}
        <form className="space-y-2" onSubmit={event => { event.preventDefault(); void enroll(); }}>
          <fieldset disabled={busy || secret !== null || uncertain} className="space-y-2">
            <label className="flex items-center gap-2">Device label<input ref={labelFocus} className={input} value={label} maxLength={100} onChange={event => setLabel(event.target.value)} /></label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={grants.some(g => g.operation === 'endpoint.read')} onChange={() => toggle({ operation: 'endpoint.read', resource_id: endpoint.endpoint_id })} />Read endpoint metadata</label>
            <label className="flex items-center gap-2">Exact session ID<input className={input} value={session} maxLength={200} onChange={event => setSession(event.target.value)} /></label>
            <p className="text-ui-10 text-faint">Session metadata includes the title and the workspace. Retained content and events can include prompts, replies, and tool output. Events are saved copies, not a live stream. Each permission applies only to the exact session ID that you add.</p>
            <div className="flex flex-wrap gap-3">
              <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={!session.trim() || session !== session.trim() || grants.length >= 64} onClick={() => { if (!grants.some(g => g.operation === 'session.read' && g.resource_id === session)) toggle({ operation: 'session.read', resource_id: session }); }}>Add session metadata</button>
              <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={!session.trim() || session !== session.trim() || grants.length >= 64} onClick={() => { if (!grants.some(g => g.operation === 'session.events.read' && g.resource_id === session)) toggle({ operation: 'session.events.read', resource_id: session }); }}>Add retained content/events</button>
            </div>
            <ul className="space-y-1">{grants.map(g => <li key={`${g.operation}:${g.resource_id}`} className="flex items-center gap-2 break-all">{grantLabels[g.operation]}: {g.resource_id}<button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => toggle(g)} aria-label={`Remove ${grantLabels[g.operation]} for ${g.resource_id}`}>Remove</button></li>)}</ul>
            <button className={SETTINGS_PRIMARY_BUTTON} type="submit" disabled={!label.trim() || grants.length === 0 || grants.length > 64}>Enroll device</button>
          </fieldset>
        </form>
        {uncertain && <p>Look at the refreshed list and revoke each device that you do not want. Then leave this page and open it again to start a new enrollment. Your draft stays here until then.</p>}
      </div>
    </SettingsRow>}
  </SettingsGroup>;
}
