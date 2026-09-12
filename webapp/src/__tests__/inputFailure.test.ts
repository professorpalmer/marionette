import { expect, it } from 'vitest';
import { inputFailureMessage } from '../lib/inputFailure';
import { streamErrorText } from '../components/conversation/streamTerminal';

it.each(['input_commit_uncertain', 'input_delivery_uncertain', 'input_stop_uncertain'])('keeps %s uncertainty visible on browser and native streams', code => {
  const browser = Object.assign(new Error('private body'), { status: 503, body: { code, error: 'private path' } });
  const native = { status: 503, code, message: 'private path' };
  for (const error of [browser, native]) {
    const copy = streamErrorText(error);
    expect(copy).toContain('could not be confirmed');
    expect(copy).toContain('Review your draft');
    expect(copy).not.toContain('private');
    expect(copy).not.toContain('Send again to retry');
    expect(copy).not.toContain('Saved inputs');
  }
});

it('distinguishes an attachment limit from a broken backend without echoing body text', () => {
  expect(streamErrorText({ status: 409, code: 'input_stopped', message: 'private' }))
    .toBe('[error] Stop cancelled this input. Review your draft before sending again.');
  expect(streamErrorText({ status: 503, code: 'input_attachment_limit', message: 'private' }))
    .toBe('[error] Attachment limits exceeded. Reduce the attachments in your draft before sending.');
  expect(streamErrorText({ status: 503, code: 'backend_error' })).toContain('backend request failed');
});

it.each([
  ['input_document_missing', 'missing'],
  ['input_lock_failed', 'storage is temporarily unavailable'],
  ['input_storage_unavailable', 'storage is temporarily unavailable'],
  ['input_owner_required', 'Open a workspace'],
  ['input_owner_invalid', 'Open a workspace'],
  ['input_corrupt', 'could not be verified'],
  ['input_attachment_corrupt', 'no longer matches'],
  ['input_attachment_unavailable', 'missing or unavailable'],
  ['input_attachment_unknown', 'missing or unavailable'],
  ['input_archive_unavailable', 'could not be restored'],
  ['input_archive_stale', 'could not be restored'],
])('maps %s to honest recovery copy', (code, needle) => {
  const message = inputFailureMessage({ status: 503, code, message: 'private body' });
  expect(message).toBeTruthy();
  expect(message!.toLowerCase()).toContain(needle.toLowerCase());
  expect(message).not.toContain('Saved inputs');
  expect(message).not.toContain('private');
  expect(streamErrorText({ status: 503, code })).toBe(`[error] ${message}`);
});

it.each(['queue_session_unbound', 'pilot_not_ready'])('surfaces workspace messaging for %s instead of input-review copy', code => {
  const message = inputFailureMessage({ status: 409, body: { code, error: 'private' } });
  expect(message).toMatch(/workspace|project session/i);
  expect(message).not.toMatch(/needs review/i);
  expect(message).not.toContain('Saved inputs');
  expect(streamErrorText({ status: 409, code })).toBe(`[error] ${message}`);
});

it('maps a leaked delivery-attempt body to recovery copy', () => {
  const native = {
    ok: false,
    code: 'input_transition_invalid',
    error: 'Input has no current delivery attempt.',
  };
  const message = inputFailureMessage(native);
  expect(message).toBeTruthy();
  expect(message).not.toContain('delivery attempt');
  expect(streamErrorText(native)).toBe(`[error] ${message}`);
});

it('keeps the vague default only for truly unknown input_* codes', () => {
  expect(inputFailureMessage({ code: 'input_totally_novel_future_code' }))
    .toBe('The input needs review. Review your draft before sending again.');
  expect(inputFailureMessage({ code: 'something_else' })).toBeNull();
});
