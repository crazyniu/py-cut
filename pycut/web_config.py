"""只在本机提供 .env 配置页面，不执行视频任务。"""
from __future__ import annotations
import html
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import config, settings

_SERVER = None
_TOKEN = secrets.token_urlsafe(32)
_LOCK = threading.Lock()


def page() -> str:
    rows = []
    for key, (label, help_text) in settings.FIELDS.items():
        if key == 'PYCUT_MAIN_URL':
            rows.append('</fieldset><fieldset><legend>LLM 网关</legend><p>用于人脸兜底和字幕定位。备用网关可选，密钥不会回显。</p>')
        kind = 'password' if key.endswith('_KEY') else 'text'
        rows.append(f'<label for="{key}">{html.escape(label)}</label><input id="{key}" name="{key}" type="{kind}" autocomplete="off" aria-describedby="{key}-help"><small id="{key}-help">{html.escape(help_text)}</small>')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>pycut · 配置</title>
<style>
:root{color-scheme:dark;--bg:#101820;--fg:#eaf0f4;--muted:#b4c1cd;--line:#415363;--accent:#80d5c2}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}main{max-width:900px;margin:auto;padding:40px 24px 110px}h1{font-size:30px;margin:0 0 8px}p,small{color:var(--muted)}fieldset{border:0;border-top:1px solid var(--line);margin:32px 0;padding:20px 0;display:grid;grid-template-columns:210px minmax(0,1fr);gap:8px 24px}legend{font-size:22px;font-weight:650;padding-right:20px}fieldset p{grid-column:1/-1;margin:0 0 16px}label{align-self:center}input{width:100%;font:inherit;color:var(--fg);background:#1b2935;border:1px solid var(--line);border-radius:6px;padding:10px 12px;caret-color:var(--accent)}input::placeholder{color:var(--muted);opacity:1}small{grid-column:2;margin-bottom:16px}button{flex-shrink:0;white-space:nowrap;font:inherit;border:0;border-radius:6px;padding:10px 20px;background:var(--accent);color:#10211e;cursor:pointer}button:hover{background:#a4e7d9}button:disabled{opacity:.6;cursor:wait}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}footer{position:fixed;bottom:0;left:0;right:0;background:#1b2935;padding:16px 24px;border-top:1px solid var(--line)}footer div{max-width:852px;margin:auto;display:flex;gap:20px;align-items:center}#status{white-space:pre-line}#status.error{color:#ffb6aa}::selection{background:var(--accent);color:#10211e}code{overflow-wrap:anywhere}@media(max-width:600px){main{padding:24px 18px 140px}fieldset{grid-template-columns:1fr}small{grid-column:1}footer div{align-items:flex-start;flex-direction:column;gap:8px}}
</style><main><h1>视频处理配置</h1><p>保存后回到终端选择处理功能。也可以直接编辑项目根目录的 <code>.env</code>；每次开始任务都会重新读取。</p><p id="location"></p><p id="details"></p><p id="overrides"></p><form id="config"><fieldset><legend>视频目录</legend><p>填写运行本程序的电脑上的路径。相对路径以项目根目录为准。</p>''' + ''.join(rows) + '''</fieldset></form></main><footer><div><button type="submit" form="config" id="save">保存配置</button><span id="status" role="status" aria-live="polite">正在读取配置…</span></div></footer>
<script>
const token=TOKEN_VALUE, form=document.getElementById('config'), status=document.getElementById('status'), save=document.getElementById('save');
let dirty=false;form.addEventListener('input',()=>{dirty=true;status.textContent='有未保存的修改';});window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
function showOverrides(fields){document.getElementById('overrides').textContent=fields.length?'外部环境变量优先：'+fields.join('、')+'。这里保存的是 .env；要使这些修改生效，请先移除对应环境变量并重启程序。':'';}
async function load(){try{const r=await fetch('/api/config',{headers:{'X-Config-Token':token}});if(!r.ok)throw Error('读取失败');const data=await r.json();for(const [k,v] of Object.entries(data.values)){form.elements[k].value=v;}for(const k of data.configured_keys){form.elements[k].placeholder='已配置，留空保持不变';}document.getElementById('location').textContent='配置文件：'+data.path;showOverrides(data.overrides);document.getElementById('details').textContent=data.problems.length?'待配置：'+data.problems.join('；'):'';status.textContent=data.problems.length?`还有 ${data.problems.length} 项待配置`:'目录已就绪，可以返回终端开始处理';}catch(e){status.className='error';status.textContent='读取失败，请刷新重试';}}
form.addEventListener('submit',async e=>{e.preventDefault();save.disabled=true;status.className='';status.textContent='正在保存…';try{const values=Object.fromEntries(new FormData(form));const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json','X-Config-Token':token},body:JSON.stringify(values)});const data=await r.json();if(!r.ok)throw Error(data.error);dirty=false;showOverrides(data.overrides);document.getElementById('details').textContent=data.problems.length?'待配置：'+data.problems.join('；'):'';status.textContent=data.problems.length?`已保存；还有 ${data.problems.length} 项待配置`:'已保存，目录已就绪。返回终端即可开始';for(const input of form.querySelectorAll('input[type=password]')){input.value='';input.placeholder='留空保留已有密钥';}}catch(e){status.className='error';status.textContent=e.message;}finally{save.disabled=false;}});load();
</script></html>'''.replace('TOKEN_VALUE', json.dumps(_TOKEN))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 不记录请求中的配置或密钥

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
        if self.path != '/api/config':
            return self.reply(404, {'error':'页面不存在'})
        if not self.authorized():
            return self.reply(403, {'error':'无效的配置会话，请刷新页面'})
        with _LOCK:
            config.refresh()
            values = settings.effective()
            configured = [k for k,v in values.items() if k.endswith('_KEY') and not config._is_placeholder(v)]
            self.reply(200, {'values':{k:('' if k.endswith('_KEY') else v) for k,v in values.items()}, 'configured_keys':configured, 'overrides':settings.overrides(), 'problems':settings.problems(values), 'path':str(config.PROJECT_ROOT / '.env')})

    def do_POST(self):
        if self.path != '/api/config':
            return self.reply(404, {'error':'页面不存在'})
        if not self.authorized():
            return self.reply(403, {'error':'无效的配置会话，请刷新页面'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32768:
                raise ValueError('配置请求大小无效')
            values = json.loads(self.rfile.read(size))
            if not isinstance(values, dict):
                raise ValueError('配置格式无效')
            with _LOCK:
                settings.save(values)
                config.refresh()
                errors = settings.problems(settings.effective())
            self.reply(200, {'problems':errors, 'overrides':settings.overrides()})
        except (ValueError, TypeError, OSError):
            self.reply(400, {'error':'保存失败：请检查目录是否存在、网关地址格式和 .env 写入权限'})


def start(*, open_browser=True) -> str:
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
