import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SchedulesPane from '../components/SchedulesPane';
import { api, type ScheduleInfo } from '../lib/api';

vi.mock('../lib/api', () => ({ api: {
  getSchedules: vi.fn(), addSchedule: vi.fn(), updateSchedule: vi.fn(),
  enableSchedule: vi.fn(), disableSchedule: vi.fn(), runScheduleNow: vi.fn(), getScheduleHistory: vi.fn(),
} }));
const schedule: ScheduleInfo = { id: 'a', name: 'Daily', objective: 'Inspect', cron: '0 9 * * *', repo: '/project', enabled: true, timezone: 'UTC', revision: 0 };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.getSchedules).mockResolvedValue({ schedules: [schedule] }); });

describe('SchedulesPane workflow', () => {
  it('creates an explicitly scoped schedule and blocks duplicate submits', async () => {
    let finish: (value: ScheduleInfo) => void = () => {};
    vi.mocked(api.addSchedule).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<SchedulesPane />);
    fireEvent.click(await screen.findByRole('button', { name: 'Create schedule' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New' } });
    fireEvent.change(screen.getByLabelText('Objective'), { target: { value: 'Check' } });
    fireEvent.change(screen.getByLabelText('Project path'), { target: { value: '/explicit' } });
    fireEvent.change(screen.getByLabelText('Driver'), { target: { value: 'openai/gpt-5-nano' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
    fireEvent.click(screen.getByRole('button', { name: 'Saving schedule…' }));
    expect(api.addSchedule).toHaveBeenCalledTimes(1);
    expect(api.addSchedule).toHaveBeenCalledWith(expect.objectContaining({ repo: '/explicit', name: 'New' }));
    await act(async () => finish({ ...schedule, name: 'New' }));
    expect(await screen.findByText('Schedule created')).toBeTruthy();
  });
  it('retains edit values on conflict and sends the original revision', async () => {
    vi.mocked(api.updateSchedule).mockRejectedValue({ error: 'Schedule changed; reload before saving again' });
    render(<SchedulesPane />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Daily' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Edited' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('reload');
    expect(screen.getByLabelText('Name')).toHaveValue('Edited');
    expect(api.updateSchedule).toHaveBeenCalledWith('a', expect.objectContaining({ revision: 0 }));
  });
  it('reports terminal run failures rather than saying started', async () => {
    vi.mocked(api.runScheduleNow).mockResolvedValue({ ok: false, run: { id: 'r', status: 'refused', halt_reason: 'Workspace missing' } });
    render(<SchedulesPane />);
    fireEvent.click(await screen.findByRole('button', { name: 'Run Daily now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Workspace missing');
    expect(screen.queryByText('Run started')).toBeNull();
  });
  it('keeps the row while pause is pending and refreshes after success', async () => {
    vi.mocked(api.disableSchedule).mockResolvedValue({ ...schedule, enabled: false, revision: 1 });
    render(<SchedulesPane />);
    fireEvent.click(await screen.findByRole('button', { name: 'Pause Daily' }));
    await waitFor(() => expect(api.disableSchedule).toHaveBeenCalledWith('a', 0));
    expect(await screen.findByText('Schedule paused')).toBeTruthy();
  });
});

it('ignores an older list response after a successful creation', async () => {
  let oldList: (value: { schedules: ScheduleInfo[] }) => void = () => {};
  vi.mocked(api.getSchedules).mockImplementationOnce(() => new Promise(resolve => { oldList = resolve; }));
  vi.mocked(api.getSchedules).mockResolvedValueOnce({ schedules: [{ ...schedule, name: 'Created' }] });
  vi.mocked(api.addSchedule).mockResolvedValue({ ...schedule, name: 'Created' });
  render(<SchedulesPane />);
  fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Created' } });
  fireEvent.change(screen.getByLabelText('Objective'), { target: { value: 'Check' } });
  fireEvent.change(screen.getByLabelText('Project path'), { target: { value: '/explicit' } });
  fireEvent.change(screen.getByLabelText('Driver'), { target: { value: 'openai/gpt-5-nano' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  await screen.findByRole('heading', { name: 'Created' });
  await act(async () => oldList({ schedules: [schedule] }));
  expect(screen.queryByRole('heading', { name: 'Daily' })).toBeNull();
});

it('does not show empty history while loading or reopen collapsed stale history', async () => {
  let finish: (value: Awaited<ReturnType<typeof api.getScheduleHistory>>) => void = () => {};
  vi.mocked(api.getScheduleHistory).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<SchedulesPane />);
  const button = await screen.findByRole('button', { name: 'History for Daily' });
  fireEvent.click(button);
  expect(screen.getByText('Loading history…')).toBeTruthy();
  expect(screen.queryByText('No runs yet.')).toBeNull();
  fireEvent.click(button);
  await act(async () => finish({ id: 'a', runs: [{ id: 'r', status: 'ok', halt_reason: 'Late history' }] }));
  expect(screen.queryByText(/Late history/)).toBeNull();
  expect(button).toHaveAttribute('aria-expanded', 'false');
});

it('prevents overlapping manual clicks and reports the actual successful result', async () => {
  let finish: (value: Awaited<ReturnType<typeof api.runScheduleNow>>) => void = () => {};
  vi.mocked(api.runScheduleNow).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<SchedulesPane />);
  const button = await screen.findByRole('button', { name: 'Run Daily now' });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(api.runScheduleNow).toHaveBeenCalledTimes(1);
  expect(button).toBeDisabled();
  expect(screen.getByText('Waiting for schedule result…')).toBeTruthy();
  await act(async () => finish({ ok: true, run: { id: 'r', status: 'ok' } }));
  expect(await screen.findByText('Run finished: ok')).toBeTruthy();
});

it('shows a load failure with a working refresh path, without claiming an empty list', async () => {
  vi.mocked(api.getSchedules).mockRejectedValueOnce(new Error('Offline'));
  render(<SchedulesPane />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Offline');
  expect(screen.queryByText(/No schedules configured/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh schedules' }));
  expect(await screen.findByRole('heading', { name: 'Daily' })).toBeTruthy();
});

it('retains a failed create draft and surfaces server validation', async () => {
  vi.mocked(api.addSchedule).mockRejectedValue({ error: 'Unknown IANA timezone' });
  render(<SchedulesPane />);
  fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New' } });
  fireEvent.change(screen.getByLabelText('Objective'), { target: { value: 'Check' } });
  fireEvent.change(screen.getByLabelText('Project path'), { target: { value: '/explicit' } });
  fireEvent.change(screen.getByLabelText('Driver'), { target: { value: 'openai/gpt-5-nano' } });
  fireEvent.change(screen.getByLabelText('Timezone'), { target: { value: 'Bad/Zone' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Unknown IANA timezone');
  expect(screen.getByLabelText('Timezone')).toHaveValue('Bad/Zone');
});

it('reports pause failure without optimistically changing the row', async () => {
  vi.mocked(api.disableSchedule).mockRejectedValue({ error: 'Schedule changed; reload' });
  render(<SchedulesPane />);
  fireEvent.click(await screen.findByRole('button', { name: 'Pause Daily' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('reload');
  expect(screen.getByRole('button', { name: 'Pause Daily' })).toBeTruthy();
});

it('reloads the saved revision explicitly after an edit conflict', async () => {
  vi.mocked(api.getSchedules).mockResolvedValueOnce({ schedules: [schedule] });
  vi.mocked(api.getSchedules).mockResolvedValue({ schedules: [{ ...schedule, name: 'Latest', revision: 1 }] });
  vi.mocked(api.updateSchedule).mockRejectedValueOnce({ error: 'Schedule changed; reload' });
  vi.mocked(api.updateSchedule).mockResolvedValue({ ...schedule, name: 'Latest', revision: 2 });
  render(<SchedulesPane />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Daily' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  await screen.findByRole('heading', { name: 'Latest' });
  fireEvent.click(screen.getByRole('button', { name: 'Reload saved values' }));
  expect(screen.getByLabelText('Name')).toHaveValue('Latest');
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  await screen.findByText('Schedule updated');
  expect(api.updateSchedule).toHaveBeenLastCalledWith('a', expect.objectContaining({ revision: 1 }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create schedule' })).toHaveFocus());
});

it('reuses the create request key after an uncertain response', async () => {
  vi.mocked(api.addSchedule).mockRejectedValueOnce(new Error('Connection lost'));
  vi.mocked(api.addSchedule).mockResolvedValue(schedule);
  render(<SchedulesPane />);
  fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New' } });
  fireEvent.change(screen.getByLabelText('Objective'), { target: { value: 'Check' } });
  fireEvent.change(screen.getByLabelText('Project path'), { target: { value: '/explicit' } });
  fireEvent.change(screen.getByLabelText('Driver'), { target: { value: 'openai/gpt-5-nano' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  await screen.findByRole('alert');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save schedule' })).not.toBeDisabled());
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }));
  await screen.findByText('Schedule created');
  const calls = vi.mocked(api.addSchedule).mock.calls;
  expect(calls[0][0].request_id).toBeTruthy();
  expect(calls[1][0].request_id).toBe(calls[0][0].request_id);
});

it.each([true, false])('can cancel an active manual run when recurrence enabled=%s', async enabled => {
  vi.mocked(api.getSchedules).mockResolvedValue({ schedules: [{ ...schedule, enabled }] });
  let finish: (value: Awaited<ReturnType<typeof api.runScheduleNow>>) => void = () => {};
  vi.mocked(api.runScheduleNow).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  vi.mocked(api.disableSchedule).mockResolvedValue({ ...schedule, enabled: false, revision: 1 });
  render(<SchedulesPane />);
  fireEvent.click(await screen.findByRole('button', { name: 'Run Daily now' }));
  const pause = screen.getByRole('button', { name: enabled ? 'Pause Daily' : 'Cancel run for Daily' });
  expect(pause).not.toBeDisabled();
  fireEvent.click(pause);
  expect(await screen.findByText('Schedule paused; cancellation requested')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Run Daily now' })).toBeDisabled();
  await act(async () => finish({ ok: false, run: { id: 'r', status: 'cancelled', halt_reason: 'cancelled' } }));
  expect(await screen.findByRole('alert')).toHaveTextContent('cancelled');
});

it('identifies existing daemon delivery modes without claiming a fresh session', async () => {
  vi.mocked(api.getSchedules).mockResolvedValue({ schedules: [{ ...schedule, delivery_mode: 'steer' }] });
  render(<SchedulesPane />);
  expect(await screen.findByText(/Legacy delivery: steer/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Edit Daily' }));
  expect(screen.getByText(/Existing delivery mode steer is preserved/)).toBeTruthy();
});
