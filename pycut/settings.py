"""项目 .env 的读取、校验和原子保存；Web 与终端共用。"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path


def _root() -> Path:
    from . import config
    return config.PROJECT_ROOT


FIELDS = {
    'PYCUT_SOURCE_DIR': ('源视频目录', '待切片的原始视频'),
    'PYCUT_REFERENCE_DIR': ('爆款视频目录', '混剪参考视频及同名 SRT'),
    'PYCUT_CLIP_DIR': ('背景切片目录', '混剪背景素材，也用于独立字幕擦除和去重读取'),
    'PYCUT_PIP_DIR': ('画中画切片目录', '切片输出（有人脸/无人脸），也用于混剪画中画素材'),
    'PYCUT_INTRO_DIR': ('前贴目录', '随机取一支片段放在混剪前面；自动去重、加贴纸，保留字幕'),
    'PYCUT_OUTPUT_DIR': ('成片输出目录', '去重与混剪的输出根目录，可自动创建'),
    'PYCUT_STICKER_DIR': ('贴纸目录', '可选；留空时使用现有在线贴纸'),
    'PYCUT_MAIN_URL': ('主网关地址', 'OpenAI 兼容 API 地址'),
    'PYCUT_MAIN_MODEL': ('主模型', '填写支持视觉检测的模型'),
    'PYCUT_MAIN_KEY': ('主网关 API Key', '留空保留已有密钥'),
    'PYCUT_BACKUP_URL': ('备用网关地址', '可选'),
    'PYCUT_BACKUP_MODEL': ('备用模型', '可选'),
    'PYCUT_BACKUP_KEY': ('备用 API Key', '留空保留已有密钥'),
}
DIRECTORIES = tuple(k for k in FIELDS if k.endswith('_DIR'))
REQUIRED = DIRECTORIES[:6]


def read_env() -> dict[str, str]:
    path = _root() / '.env'
    if not path.exists():
        return {}
    values = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def directory(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else _root() / path).resolve()


def effective() -> dict[str, str]:
    values = read_env()
    effective_values = {k: os.environ.get(k, values.get(k, '')) for k in FIELDS}
    if not effective_values['PYCUT_MAIN_KEY']:
        effective_values['PYCUT_MAIN_KEY'] = os.environ.get('DEEPSEEK_API_KEY', values.get('DEEPSEEK_API_KEY', ''))
    return effective_values


def overrides() -> list[str]:
    from . import config
    return [FIELDS[k][0] for k in FIELDS if k in os.environ and k not in config._dotenv_values]


def problems(values: dict[str, str], *, require_all: bool = True) -> list[str]:
    errors = []
    for key in DIRECTORIES:
        value = values.get(key, '').strip()
        label = FIELDS[key][0]
        if not value:
            if require_all and key in REQUIRED:
                errors.append(f'{label}尚未配置')
            continue
        path = directory(value)
        if key == 'PYCUT_OUTPUT_DIR':
            if path.exists() and not path.is_dir():
                errors.append(f'{label}不是目录')
        elif not path.is_dir():
            errors.append(f'{label}不存在或无法访问')
    for key in ('PYCUT_MAIN_URL', 'PYCUT_BACKUP_URL'):
        value = values.get(key, '')
        if value and not value.startswith(('http://', 'https://')):
            errors.append(f'{FIELDS[key][0]}须以 http:// 或 https:// 开头')
    return errors


def save(updates: dict[str, str]) -> None:
    if set(updates) - FIELDS.keys():
        raise ValueError('包含未知配置项')
    if any(not isinstance(v, str) or '\n' in v or '\r' in v or '\x00' in v for v in updates.values()):
        raise ValueError('配置必须是单行文本')
    updates = {k: v.strip() for k, v in updates.items() if not (k.endswith('_KEY') and not v.strip())}
    errors = problems(updates, require_all=False)
    if errors:
        raise ValueError('；'.join(errors))
    path = _root() / '.env'
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    pending = dict(updates)
    output = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in updates:
            if key in pending:
                output.append(f'{key}={json.dumps(pending.pop(key), ensure_ascii=False)}')
        else:
            output.append(line)
    output.extend(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in pending.items())
    fd, name = tempfile.mkstemp(prefix='.env.', dir=_root())
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(output) + '\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
