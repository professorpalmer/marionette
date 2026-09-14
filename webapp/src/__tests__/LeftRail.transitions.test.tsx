import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import LeftRail from '../components/LeftRail';
import { api, type Session } from '../lib/api';
import { clearSWRCache, writeSWRCache, readSWRCache } from '../lib/useStaleWhileRevalidate';
vi.mock('../lib/api', () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: '/workspace', recents: [], is_git: false }),
  openWorkspace: vi.fn(), sessions: vi.fn(), createSession: vi.fn(), archiveSession: vi.fn(),
  workspaces: vi.fn().mockResolvedValue([]), jobs: vi.fn().mockResolvedValue([]),
} }));
vi.mock('../lib/usePolling', () => ({ usePolling: vi.fn() }));
vi.mock('../lib/useOperationalDiagnostic', () => ({ useOperationalDiagnostic: () => null }));
const old: Session = { id: 'old', title: 'Current', created: 1, active: true, repo: '/workspace' };
const created: Session = { id: 'server-id', title: 'Server title', created: 2, repo: '/workspace' };
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  localStorage.clear(); clearSWRCache(); vi.clearAllMocks();
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: [], branch: '', is_git: false, codegraph_status: 'none' });
  vi.mocked(api.sessions).mockResolvedValue([old]);
  vi.mocked(api.createSession).mockResolvedValue(created);
  vi.mocked(api.archiveSession).mockResolvedValue({ ok: true });
});
afterEach(cleanup);
it('publishes the server row and identity before background lists finish', async () => {
  const changed = vi.fn((id: string) => {
    if (id === created.id) expect(readSWRCache<Session[]>('sessions:/workspace')?.find(row => row.id === id)?.active).toBe(true);
  });
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in workspace')); });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
  expect(changed).toHaveBeenLastCalledWith('server-id');
});
it('hides the empty CTA while archive replacement is pending', async () => {
  const next = deferred<Session>();
  vi.mocked(api.createSession).mockReturnValue(next.promise);
  vi.mocked(api.archiveSession).mockImplementation(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true }]);
    return { ok: true };
  });
  render(<LeftRail jobsRefresh={0} />);
  const row = await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.contextMenu(row);
  await act(async () => { fireEvent.click(screen.getByText('Archive', { exact: true })); });
  expect(api.createSession).toHaveBeenCalled();
  expect(screen.queryByTitle('Open workspace and start a session')).toBeNull();
  await act(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true, active: false }, { ...created, active: true }]);
    next.resolve(created);
  });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
});
it('does not publish a create response after another project is selected', async () => {
  const next = deferred<Session>();
  vi.mocked(api.createSession).mockReturnValue(next.promise);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  fireEvent.click(screen.getByLabelText('New session in workspace'));
  changed.mockClear();
  await act(async () => {
    window.dispatchEvent(new CustomEvent('harness-project-selected', { detail: '/other' }));
    next.resolve(created);
  });
  expect(changed).not.toHaveBeenCalled();
});
it('keeps project plus keyboard reachable and removes decorative row icons', async () => {
  render(<LeftRail jobsRefresh={0} />);
  const row = await screen.findByRole('button', { name: 'Current', exact: true });
  expect(row.querySelector('.lucide-message-square')).toBeNull();
  expect(screen.getByLabelText('New session in workspace')).toHaveClass('opacity-0', 'group-hover:opacity-100', 'focus-visible:opacity-100');
});

it('rejects pre-create lists that resolve after the server row was published', async () => {
  const stale = deferred<Session[]>();
  writeSWRCache('sessions:/workspace', [old]);
  vi.mocked(api.sessions).mockReturnValue(stale.promise);
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in workspace')); });
  await act(async () => { stale.resolve([old]); });
  expect(screen.getByRole('button', { name: 'Server title', exact: true })).toHaveAttribute('aria-current', 'true');
  expect(changed).toHaveBeenLastCalledWith('server-id');
});
it('keeps lease exhaustion visible when automatic replacement cannot start', async () => {
  vi.mocked(api.createSession).mockRejectedValue({ code: 'lease_exhausted' });
  vi.mocked(api.archiveSession).mockImplementation(async () => {
    vi.mocked(api.sessions).mockResolvedValue([{ ...old, archived: true }]);
    return { ok: true };
  });
  render(<LeftRail jobsRefresh={0} />);
  fireEvent.contextMenu(await screen.findByRole('button', { name: 'Current', exact: true }));
  await act(async () => { fireEvent.click(screen.getByText('Archive', { exact: true })); });
  expect(screen.getByText(/too many sessions are busy right now/)).toBeVisible();
  expect(screen.getByTitle('Open workspace and start a session')).toBeVisible();
});

it('publishes an automatically created workspace session without waiting for workspace refresh', async () => {
  vi.mocked(api.getWorkspace).mockResolvedValue({ repo: '/workspace', recents: ['/other'], branch: '', is_git: false, codegraph_status: 'none' });
  const changed = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={changed} />);
  await screen.findByRole('button', { name: 'Current', exact: true });
  vi.mocked(api.getWorkspace).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.sessions).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.openWorkspace).mockResolvedValue({ ok: true, repo: '/other', branch: '', is_git: false, codegraph: 'none', created_session: true, active_session: 'workspace-server-id' });
  await act(async () => { fireEvent.click(screen.getByLabelText('New session in other')); });
  expect(changed).toHaveBeenLastCalledWith('workspace-server-id');
  expect(readSWRCache<Session[]>('sessions:/other')).toEqual([expect.objectContaining({ id: 'workspace-server-id', active: true })]);
  expect(api.createSession).not.toHaveBeenCalled();
});
