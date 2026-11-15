import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DeviceAccess from '../components/DeviceAccess';
import { DeviceAccessClient } from '../lib/deviceAccess';

// Deliberately nonfunctional fixture credentials; never enroll against a real backend.
const fixtureCredential = 'fixture-only-not-a-real-device-credential';
const endpoint = { ok: true, protocol_version: 1, endpoint_id: 'endpoint-fixture', boot_id: 'boot-fixture', capabilities: ['endpoint_fence_v1'], device_auth: 'bounded_read_v1' };
const device = { device_id: 'device-fixture', label: 'Fixture tablet', created_at: 1, revoked_at: null, grants_revision: 1, grants: [{ operation: 'endpoint.read', resource_id: 'endpoint-fixture' }] };
const reply = (value: unknown, status = 200) => ({ kind: 'response', status, text: JSON.stringify(value), correlationId: '' });
const request = vi.fn();
vi.mock('../lib/transport', () => ({ getHarnessIpc: () => bridge }));
vi.mock('../lib/operationalDiagnostic', () => ({ desktopBridgeMissing: () => false }));
let bridge: unknown;
beforeEach(() => {
  request.mockReset();
  bridge = { endpointHeaders: true, requestJSON: request };
  request.mockImplementation(async (_method: string, path: string) => {
    if (path === '/api/endpoint') return reply(endpoint);
    if (path === '/api/devices') return reply({ ok: true, devices: [] });
    if (path === '/api/devices/enroll') return reply({ ok: true, device_id: device.device_id, credential: fixtureCredential }, 201);
    return reply({ ok: true });
  });
});
async function ready() {
  render(<DeviceAccess />);
  await screen.findByRole('button', { name: 'Enroll device' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh devices' })).toBeEnabled());
}
function draft() {
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Fixture tablet' } });
  fireEvent.click(screen.getByLabelText('Read endpoint metadata'));
}

describe('Device access', () => {
  it('offers no enabled controls on an unsupported platform', async () => {
    request.mockResolvedValue(reply({ ...endpoint, device_auth: 'disabled' }));
    await act(async () => { render(<DeviceAccess />); });
    expect(screen.getByText(/disabled or unsupported/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
  });
  it('requires explicit grants and sends exact sessions without adding permissions', async () => {
    await ready();
    expect(screen.getByLabelText('Read endpoint metadata')).not.toBeChecked();
    fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Fixture tablet' } });
    expect(screen.getByRole('button', { name: 'Enroll device' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Exact session ID'), { target: { value: 'session-fixture' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add retained content/events' }));
    fireEvent.click(screen.getByRole('button', { name: 'Enroll device' }));
    await screen.findByRole('button', { name: 'Show credential' });
    expect(request).toHaveBeenCalledWith('POST', '/api/devices/enroll', { label: 'Fixture tablet', grants: [{ operation: 'session.events.read', resource_id: 'session-fixture' }] }, '', { 'X-Harness-Protocol': '1', 'X-Harness-Endpoint': 'endpoint-fixture', 'X-Harness-Boot': 'boot-fixture' });
  });
  it('keeps the one-time credential hidden until requested, copies explicitly, and clears on dismiss', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    await ready(); draft();
    fireEvent.click(screen.getByRole('button', { name: 'Enroll device' }));
    const show = await screen.findByRole('button', { name: 'Show credential' });
    await waitFor(() => expect(show).toHaveFocus());
    expect(screen.queryByText(fixtureCredential)).not.toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.click(show);
    expect(screen.getByText(fixtureCredential)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Copy credential' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(fixtureCredential));
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss credential' }));
    expect(screen.queryByText(fixtureCredential)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enroll device' })).toBeDisabled();
    expect(JSON.stringify(request.mock.calls)).not.toContain(fixtureCredential);
  });
  it('preserves a received credential when a later list refresh fails', async () => {
    await ready(); draft();
    fireEvent.click(screen.getByRole('button', { name: 'Enroll device' }));
    await screen.findByRole('button', { name: 'Show credential' });
    request.mockResolvedValue(reply({ ok: false }, 503));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh devices' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Show credential' }));
    expect(screen.getByText(fixtureCredential)).toBeInTheDocument();
  });
  it('does not retry uncertain enrollment and preserves the draft', async () => {
    await ready(); draft();
    request.mockRejectedValueOnce(new Error(fixtureCredential));
    fireEvent.click(screen.getByRole('button', { name: 'Enroll device' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Enrollment outcome is unknown');
    expect(screen.getByLabelText('Device label')).toHaveValue('Fixture tablet');
    expect(screen.getByRole('button', { name: 'Enroll device' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh devices' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh devices' })).toBeEnabled());
    expect(request.mock.calls.filter(call => call[1] === '/api/devices/enroll')).toHaveLength(1);
    expect(screen.queryByText(fixtureCredential)).not.toBeInTheDocument();
  });
  it('shows exact grants and revocation status and handles already revoked success', async () => {
    request.mockImplementation(async (_method: string, path: string) => reply(path === '/api/endpoint' ? endpoint : path === '/api/devices' ? { ok: true, devices: [device] } : { ok: true }));
    await ready();
    expect(screen.getByText('Device ID: device-fixture')).toBeInTheDocument();
    const revoke = screen.getByRole('button', { name: 'Revoke Fixture tablet' });
    revoke.focus();
    fireEvent.click(revoke);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke Fixture tablet' })).toBeDisabled());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh devices' })).toHaveFocus());
    expect(screen.getByText('Revoked', { selector: 'span' })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('POST', '/api/devices/device-fixture/revoke', {}, '', expect.objectContaining({ 'X-Harness-Endpoint': 'endpoint-fixture' }));
  });
  it('discards a late enrollment after unmount and does not carry it into a new panel', async () => {
    let resolveEnrollment: (value: unknown) => void = () => {};
    const view = render(<DeviceAccess />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh devices' })).toBeEnabled());
    draft();
    request.mockImplementationOnce(() => new Promise(resolve => { resolveEnrollment = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'Enroll device' }));
    view.unmount();
    await act(async () => { resolveEnrollment(reply({ ok: true, device_id: device.device_id, credential: fixtureCredential })); });
    await ready();
    expect(screen.queryByRole('button', { name: 'Show credential' })).not.toBeInTheDocument();
  });
});

describe('DeviceAccessClient fencing', () => {
  it('does not follow a boot mismatch or replay a mutation', async () => {
    const client = new DeviceAccessClient(); await client.connect();
    request.mockResolvedValueOnce(reply({ ok: false, code: 'boot_mismatch' }, 409));
    await expect(client.revoke('fixture')).rejects.toThrow();
    await expect(client.list()).rejects.toThrow('Connection changed');
    expect(request).toHaveBeenCalledTimes(2);
  });
  it('rejects a response when the connection changes while it is pending', async () => {
    const client = new DeviceAccessClient(); await client.connect();
    let finish: (value: unknown) => void = () => {};
    request.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const pending = client.list();
    bridge = { endpointHeaders: true, requestJSON: request };
    finish(reply({ ok: true, devices: [device] }));
    await expect(pending).rejects.toThrow('Connection changed');
  });
  it('rejects malformed device lists instead of rendering partial authority', async () => {
    const client = new DeviceAccessClient(); await client.connect();
    request.mockResolvedValueOnce(reply({ ok: true, devices: [{ ...device, grants: [{ operation: 'commands.run', resource_id: '*' }] }] }));
    await expect(client.list()).rejects.toThrow('Invalid device list');
  });
});

it('keeps web credentials in headers and enrollment credentials out of subsequent requests', async () => {
  bridge = null;
  const fetchMock = vi.fn().mockResolvedValue({ status: 200, text: async () => JSON.stringify(endpoint) });
  vi.stubGlobal('fetch', fetchMock);
  try {
    const client = new DeviceAccessClient();
    await client.connect();
    fetchMock.mockResolvedValueOnce({ status: 201, text: async () => JSON.stringify({ ok: true, device_id: 'fixture', credential: fixtureCredential }) });
    await client.enroll('Fixture', [{ operation: 'endpoint.read', resource_id: endpoint.endpoint_id }]);
    expect(fetchMock.mock.calls[1][0]).toBe('/api/devices/enroll');
    expect(fetchMock.mock.calls[1][1].cache).toBe('no-store');
    expect(fetchMock.mock.calls[1][1].headers['X-Harness-Boot']).toBe('boot-fixture');
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain(fixtureCredential);
  } finally { vi.unstubAllGlobals(); }
});

it('ignores a late device list after its panel unmounts', async () => {
  let finish: (value: unknown) => void = () => {};
  request.mockImplementation(async (_method: string, path: string) => path === '/api/endpoint' ? reply(endpoint) : new Promise(resolve => { finish = resolve; }));
  const view = render(<DeviceAccess />);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  view.unmount();
  await act(async () => { finish(reply({ ok: true, devices: [device] })); });
  expect(screen.queryByText('Device ID: device-fixture')).not.toBeInTheDocument();
});
