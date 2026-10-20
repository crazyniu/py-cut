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
    stdin: { contents: `export { default as App } from './src/App'; export { MediaCard } from './src/common'; export { default as TaskProgress } from './src/TaskProgress';`, resolveDir: process.cwd() },
    bundle: true, platform: 'node', format: 'esm', packages: 'external', write: false,
  });
  await writeFile(filename, result.outputFiles[0].text);
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.isTauri = true;
  globalThis.window = { __TAURI_INTERNALS__: {}, matchMedia: () => ({ matches: false }) };
  const { App, MediaCard, TaskProgress } = await import(pathToFileURL(filename));
  const render = (component, props) => renderToStaticMarkup(createElement(MantineProvider, { forceColorScheme: 'dark' }, createElement(component, props)));

  test('native startup reads projects without blocking on Codex authentication', () => {
    const html = render(App);
    assert.match(html, /正在读取项目/);
    assert.doesNotMatch(html, /正在检查本机 Codex|请先安装并登录 Codex/);
    assert.match(html, /<button[^>]*mantine-NavLink-root/);
    assert.doesNotMatch(html, /<a[^>]*mantine-NavLink-root/);
  });

  test('media captions follow the image instead of covering the preview', () => {
    const html = render(MediaCard, { thumbnail: createElement('img', { src: 'asset.png', alt: '' }), title: 'A long video filename', label: 'Preview video', onOpen() {} });
    assert.match(html, /<\/div><div class="[^\"]*media-card-caption/);
    assert.match(html, /aria-label="Preview video"/);
  });

  test('cancelled batches retain unfinished counts and distinct status', () => {
    const html = render(TaskProgress, { job: { id: 'cancelled', status: 'cancelled', kind: 'dedup', planned: 3, completed: 1, failed: 0, review: 0, items: [] } });
    assert.match(html, /已取消/);
    assert.match(html, /已处理 1 \/ 3/);
    assert.match(html, /未完成 2/);
    assert.doesNotMatch(html, /等待处理|正在准备素材/);
  });
} finally {
  await rm(directory, { recursive: true, force: true });
}
