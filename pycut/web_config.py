"""Loopback-only .env settings service; never executes video tasks."""
from __future__ import annotations
import html
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from . import config, settings, web_dir_browser, native_directory_picker

_SERVER = None
_TOKEN = secrets.token_urlsafe(32)
_LOCK = threading.Lock()


def page() -> str:
    from pathlib import Path
    sections = {'directories': [], 'llm': []}
    for key, (label, help_text) in settings.FIELDS.items():
        kind = 'password' if key.endswith('_KEY') else 'text'
        section = 'directories' if key.endswith('_DIR') else 'llm'
        required = key in settings.REQUIRED
        optional = ('必填' if required else '可选' if key in (
            'PYCUT_PIP_DIR', 'PYCUT_INTRO_DIR', 'PYCUT_OUTPUT_DIR',
            'PYCUT_STICKER_DIR') or key.startswith('PYCUT_BACKUP_') else '')
        badge_class = 'optional is-required' if required else 'optional'
        input_html = (f'<input id="{key}" name="{key}" type="{kind}" autocomplete="off" '
                      f'spellcheck="false" aria-describedby="{key}-help {key}-error">')
        multi = key in settings.MULTI_DIRECTORIES
        if multi:
            input_html = (f'<input id="{key}" name="{key}" type="hidden">'
                          f'<div class="directory-list" data-key="{key}"></div>'
                          f'<button type="button" class="add-directory" data-key="{key}">添加目录</button>')
        elif key.endswith('_DIR'):
            input_html = (f'<div class="input-row">{input_html}'
                          f'<button type="button" class="browse" data-key="{key}" '
                          f'aria-label="选择{html.escape(label)}">选择…</button></div>')
        if multi:
            sections[section].append(
                f'<fieldset class="field multi-directory" data-key="{key}" '
                f'aria-describedby="{key}-help {key}-error"><legend>{html.escape(label)}'
                f'<span class="{badge_class}">{optional}</span></legend>'
                f'<small id="{key}-help">{html.escape(help_text)}</small>'
                f'{input_html}<span class="field-error" id="{key}-error"></span></fieldset>')
        else:
            sections[section].append(
                f'<div class="field"><label for="{key}">{html.escape(label)}'
                f'<span class="{badge_class}">{optional}</span></label>'
                f'{input_html}'
                f'<small id="{key}-help">{html.escape(help_text)}</small>'
                f'<span class="field-error" id="{key}-error"></span></div>')
    template = Path(__file__).with_name('templates').joinpath('config.html').read_text(encoding='utf-8')
    return (template.replace('DIRECTORY_FIELDS', ''.join(sections['directories']))
            .replace('LLM_FIELDS', ''.join(sections['llm']))
            .replace('TOKEN_VALUE', json.dumps(_TOKEN))
            .replace('MULTI_FIELDS', json.dumps(settings.MULTI_DIRECTORIES))
            .replace('FIELD_LABELS', json.dumps({k: v[0] for k, v in settings.FIELDS.items()}, ensure_ascii=False)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log settings payloads or credentials.

    def reply(self, code, body, kind='application/json'):
        payload = (json.dumps(body, ensure_ascii=False) if kind == 'application/json' else body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        host = f'127.0.0.1:{self.server.server_port}'
        return (self.headers.get('Host') == host
                and self.headers.get('Origin', f'http://{host}') == f'http://{host}')

    def authorized(self):
        return self.allowed() and secrets.compare_digest(self.headers.get('X-Config-Token', ''), _TOKEN)

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {'error':'仅允许本机配置页面访问'})
        if self.path == '/':
            return self.reply(200, page(), 'text/html')
        route = urlsplit(self.path).path
        if route == '/api/dirs':
            if not self.authorized():
                return self.reply(403, {'error':'无效的配置会话，请刷新页面'})
            path = parse_qs(urlsplit(self.path).query).get('path', [''])[0] or None
            return self.reply(200, web_dir_browser.list_dir(path, config.PROJECT_ROOT))
        if route != '/api/config':
            return self.reply(404, {'error':'页面不存在'})
        if not self.authorized():
            return self.reply(403, {'error':'无效的配置会话，请刷新页面'})
        with _LOCK:
            config.refresh()
            values = settings.effective()
            configured = [k for k,v in values.items() if k.endswith('_KEY') and not config._is_placeholder(v)]
            self.reply(200, {'values':{k:('' if k.endswith('_KEY') else v) for k,v in values.items()}, 'configured_keys':configured, 'overrides':settings.overrides(), 'problems':settings.problems(values), 'path':str(config.PROJECT_ROOT / '.env')})

    def do_POST(self):
        if self.path not in ('/api/config', '/api/pick-directory'):
            return self.reply(404, {'error':'页面不存在'})
        if not self.authorized():
            return self.reply(403, {'error':'无效的配置会话，请刷新页面'})
        values = {}
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32768:
                raise ValueError('配置请求大小无效')
            values = json.loads(self.rfile.read(size))
            if not isinstance(values, dict):
                raise ValueError('配置格式无效')
            if self.path == '/api/pick-directory':
                key, initial = values.get('key'), values.get('path', '')
                if key not in settings.DIRECTORIES or not isinstance(initial, str) or len(initial) > 4096 or '\x00' in initial:
                    raise ValueError('目录选择参数无效')
                try:
                    selected = native_directory_picker.choose_directory(initial, config.PROJECT_ROOT, '选择'+settings.FIELDS[key][0])
                except native_directory_picker.PickerError as exc:
                    return self.reply(503, {'error':str(exc)})
                return self.reply(200, {'path':selected, 'cancelled':selected is None})
            with _LOCK:
                settings.save(values)
                config.refresh()
                errors = settings.problems(settings.effective())
            self.reply(200, {'problems':errors, 'overrides':settings.overrides(),
                             'values':{k:('' if k.endswith('_KEY') else v) for k,v in settings.effective().items()}})
        except (ValueError, TypeError, OSError):
            if self.path == '/api/pick-directory':
                return self.reply(400, {'error':'目录选择参数无效，请刷新页面后重试'})
            self.reply(400, {'error':'保存失败：请检查字段提示和 .env 写入权限后重试。', 'problems':settings.problems(values, require_all=False) if isinstance(values, dict) and all(isinstance(v, str) for v in values.values()) else []})


def start(*, open_browser=False) -> str:
    global _SERVER
    if _SERVER is None:
        _SERVER = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        _SERVER.daemon_threads = True
        threading.Thread(target=_SERVER.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{_SERVER.server_port}/'
    if open_browser:
        webbrowser.open(url)
    return url


def stop() -> None:
    global _SERVER
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()
        _SERVER = None
