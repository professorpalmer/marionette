import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import NativeTaskDisclosure from '../components/NativeTaskDisclosure';
import { producerTask } from './nativeExpert.fixtures';
afterEach(cleanup);
it('preserves keyboard focus and contains click and both key phases for disclosure and Kill', () => {
  const bubbled = vi.fn(), request = vi.fn();
  render(<div onClick={bubbled} onKeyDown={bubbled} onKeyUp={bubbled}>
    <NativeTaskDisclosure task={producerTask()} kill={{ disabled: false, request }} />
  </div>);
  const worker = screen.getByRole('button', { name: /implement/ });
  expect(screen.queryByText('Keyboard disclosure')).toBeNull();
  worker.focus();
  fireEvent.keyDown(worker, { key: 'Enter' });
  fireEvent.keyUp(worker, { key: 'Enter' });
  expect(worker).toHaveFocus();
  expect(worker).toHaveAttribute('aria-expanded', 'true');
  const details = document.getElementById(worker.getAttribute('aria-controls') ?? '');
  expect(details).toHaveTextContent('Keyboard disclosure');
  expect(worker.contains(details)).toBe(false);
  fireEvent.keyDown(worker, { key: ' ', repeat: true });
  expect(worker).toHaveAttribute('aria-expanded', 'true');
  fireEvent.keyDown(worker, { key: ' ' });
  expect(worker).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByText('Keyboard disclosure')).toBeNull();
  fireEvent.click(worker);
  const kill = screen.getByRole('button', { name: 'Cancel this job' });
  kill.focus();
  fireEvent.keyDown(kill, { key: 'Enter' });
  fireEvent.keyUp(kill, { key: 'Enter' });
  expect(request).not.toHaveBeenCalled();
  fireEvent.click(kill);
  expect(request).toHaveBeenCalledTimes(1);
  expect(kill).toHaveFocus();
  expect(worker).toHaveAttribute('aria-expanded', 'true');
  expect(bubbled).not.toHaveBeenCalled();
});

it('labels task models as assignments without claiming provider use', () => {
  render(<NativeTaskDisclosure task={{ ...producerTask(), model: 'configured/model', model_kind: 'assigned' }} />);
  expect(screen.getByTitle('Model: configured/model')).toHaveTextContent('configured/model (assigned task model)');
  expect(screen.queryByText(/realized|provider used/i)).toBeNull();
});

it('retains full long role, model and instruction in a 220px disclosure without extra controls', () => {
  const role = 'role'.repeat(40), model = 'model'.repeat(32), instruction = 'instruction'.repeat(90);
  render(<div style={{ width: 220 }}><NativeTaskDisclosure task={{ ...producerTask(), role, model, instruction, model_kind: 'assigned' }} /></div>);
  const disclosure = screen.getByRole('button', { name: `${role}: running` });
  expect(disclosure).toHaveTextContent(role);
  expect(screen.getByTitle(`Model: ${model}`)).toHaveTextContent(model);
  expect(screen.queryByText(instruction)).toBeNull();
  fireEvent.click(disclosure);
  expect(screen.getByText(instruction)).toBeVisible();
  expect(screen.getAllByRole('button')).toHaveLength(1);
});
