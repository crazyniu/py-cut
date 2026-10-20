import assert from 'node:assert/strict';
import test from 'node:test';
import { connected, rpc, statusColor, statusNames, terminal, useSummary } from './api.ts';

test('cancelling remains active until the backend confirms a terminal result', () => {
  for (const status of ['queued', 'running', 'cancelling']) assert.equal(terminal({ status }), false);
  for (const status of ['completed', 'review', 'partial', 'failed', 'cancelled', 'interrupted']) assert.equal(terminal({ status }), true);
  assert.equal(statusNames.cancelling, '正在停止');
  assert.equal(statusColor('cancelling'), 'orange');
});

test('browser preview rejects processing requests instead of inventing results', async () => {
  assert.equal(connected, false);
  await assert.rejects(rpc('jobs.submit', { kind: 'montage' }), /桌面服务未连接/);
});

test('material use summaries preserve source offset and visible body interval', () => {
  assert.equal(useSummary({ role: 'pip', details: { offset: 1.25, start: 0, duration: 2.5 } }), '画中画 · 素材起点 1.25 秒 · 正文位置 0.00 秒 · 取用 2.50 秒');
  assert.equal(useSummary({ role: 'subtitle' }), '字幕');
});
