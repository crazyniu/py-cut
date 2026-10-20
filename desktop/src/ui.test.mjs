import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MantineProvider } from '@mantine/core';

// Render the shipped components with effects inactive; no worker or media is created.
const directory = await mkdtemp(path.resolve('node_modules/.pycut-ui-'));
try {
  const filename = path.join(directory, 'components.mjs');
  const result = await build({
    stdin: { contents: `export { default as App } from './src/App'; export { MediaCard, PageHeading } from './src/common'; export { default as TaskProgress } from './src/TaskProgress'; export { default as LibraryPage } from './src/LibraryPage';`, resolveDir: process.cwd() },
    bundle: true, platform: 'node', format: 'esm', packages: 'external', write: false,
  });
  await writeFile(filename, result.outputFiles[0].text);
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.isTauri = true;
  globalThis.window = { __TAURI_INTERNALS__: {}, matchMedia: () => ({ matches: false }) };
  const { App, MediaCard, PageHeading, TaskProgress, LibraryPage } = await import(pathToFileURL(filename));
  const render = (component, props) => renderToStaticMarkup(createElement(MantineProvider, { forceColorScheme: 'dark' }, createElement(component, props)));

  test('native startup reads projects without blocking on Codex authentication', () => {
    const html = render(App);
    assert.match(html, /正在读取项目/);
    assert.doesNotMatch(html, /正在检查本机 Codex|请先安装并登录 Codex/);
    assert.match(html, /<button[^>]*mantine-NavLink-root/);
    assert.doesNotMatch(html, /<a[^>]*mantine-NavLink-root/);
  });

  test('grid and list switches share a connected button group', () => {
    const html = render(LibraryPage, { project: { id: 'project', name: 'Test' } });
    assert.match(html, /mantine-ButtonGroup-group/);
    assert.match(html, /aria-label="显示方式"/);
    assert.match(html, /aria-label="缩略图视图"/);
    assert.match(html, /aria-label="列表视图"/);
  });

  test('all media captions stay inside the portrait image', () => {
    const html = render(MediaCard, { thumbnail: createElement('img', { src: 'asset.png', alt: '' }), title: 'A long video filename', label: 'Preview video', onOpen() {} });
    assert.match(html, /media-card-image[^>]*><img[^>]*><div class="[^"]*media-card-caption/);
    assert.match(html, /aria-label="Preview video"/);
  });

  test('processing stays in the preview without decorative completion labels', () => {
    const header = render(PageHeading, { title: '视频去重任务' });
    assert.doesNotMatch(header, /status-tag/);
    const html = render(TaskProgress, { job: { id: 'running', status: 'running', kind: 'dedup', planned: 1, completed: 0, failed: 0, review: 0,
      items: [{ id: 'clip', name: 'clip.mp4', path: '/clip.mp4', status: 'running', progress: { stage: '编码', completed: 25, total: 100 } }] } });
    assert.match(html, /thumbnail-progress/);
    assert.match(html, /处理中/);
    assert.doesNotMatch(html, /batch-summary|本批视频|整批视频进度|已处理|25%|编码/);
    const completed = render(TaskProgress, { job: { id: 'completed', status: 'completed', planned: 1, completed: 1, failed: 0, review: 0,
      items: [{ id: 'clip', name: 'clip.mp4', output_id: 'out', status: 'completed' }],
      outputs: [{ id: 'out', name: 'clip_1.mp4', path: '/clip_1.mp4' }] } });
    assert.doesNotMatch(completed, /status-tag|thumbnail-progress/);
    assert.match(completed, /title="clip_1.mp4"/);
    assert.doesNotMatch(completed, /title="clip.mp4"/);
  });

} finally {
  await rm(directory, { recursive: true, force: true });
}
