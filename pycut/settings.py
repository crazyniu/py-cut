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
    'PYCUT_REFERENCE_DIR': ('话术目录', '每个子文件夹放一个话术视频及同名 SRT，例如 a/1.mp4、a/1.srt；递归读取'),
    'PYCUT_CLIP_DIR': ('混剪切片目录', '添加用户已准备的切片目录，递归合并；也用于字幕擦除和去重'),
    'PYCUT_PIP_DIR': ('画中画切片目录', '可添加多个目录，递归读取；留空或关闭画中画时不叠加'),
    'PYCUT_INTRO_DIR': ('前贴目录', '可添加多个目录，递归读取；留空或没有视频时跳过，保留前贴原声和字幕'),
    'PYCUT_OUTPUT_DIR': ('成片输出目录', '混剪成片的输出根目录，可自动创建；字幕擦除和去重默认另存输入目录同级'),
    'PYCUT_STICKER_DIR': ('贴纸目录', '可选；留空时使用现有在线贴纸'),
    'PYCUT_MAIN_URL': ('主网关地址', 'OpenAI 兼容 API 地址'),
    'PYCUT_MAIN_MODEL': ('主模型', '填写支持视觉检测的模型'),
    'PYCUT_MAIN_KEY': ('主网关 API Key', '留空保留已有密钥'),
    'PYCUT_BACKUP_URL': ('备用网关地址', '可选'),
    'PYCUT_BACKUP_MODEL': ('备用模型', '可选'),
    'PYCUT_BACKUP_KEY': ('备用 API Key', '留空保留已有密钥'),
}
DIRECTORIES = tuple(k for k in FIELDS if k.endswith('_DIR'))
MULTI_DIRECTORIES = ('PYCUT_CLIP_DIR', 'PYCUT_PIP_DIR', 'PYCUT_INTRO_DIR')
REQUIRED = ('PYCUT_REFERENCE_DIR', 'PYCUT_CLIP_DIR')


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


def directories(value) -> list[Path]:
    """Resolve legacy single paths, JSON path arrays or repeated CLI values.

    Keep JSON inside a single .env value so spaces, commas and Windows drive
    letters never become ambiguous path separators. Deduplicate aliases here;
    recursive scanners additionally deduplicate files under overlapping roots.
    """
    if value is None or value == '':
        return []
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith('['):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError('多目录格式无效，请使用路径列表') from exc
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError('目录列表只能包含文本路径')
        else:
            value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError('目录必须是路径或路径列表')
    result = []
    for part in value:
        if not isinstance(part, (str, Path)):
            raise ValueError('目录列表只能包含文本路径')
        if isinstance(part, str) and any(c in part for c in ('\n', '\r', '\x00')):
            raise ValueError('目录路径不能包含换行或空字符')
        if isinstance(part, str) and part.strip().startswith('['):
            paths = directories(part)
        else:
            paths = [directory(str(part).strip())] if str(part).strip() else []
        for path in paths:
            if path not in result:
                result.append(path)
    return result


def encoded_directories(paths) -> str:
    """CLI/Web transport; empty lists disable optional libraries."""
    resolved = directories(paths)
    return json.dumps([str(p) for p in resolved], ensure_ascii=False) if resolved else ''


def effective() -> dict[str, str]:
    values = read_env()
    effective_values = {k: os.environ.get(k, values.get(k, '')) for k in FIELDS}
    if not effective_values['PYCUT_MAIN_KEY']:
        effective_values['PYCUT_MAIN_KEY'] = os.environ.get('DEEPSEEK_API_KEY', values.get('DEEPSEEK_API_KEY', ''))
    return effective_values


def overrides() -> list[str]:
    from . import config
    return [FIELDS[k][0] for k in FIELDS if k in os.environ and k not in config._dotenv_values]


def problems(values: dict[str, str], *, require_all: bool = True, keys=None) -> list[str]:
    errors = []
    for key in DIRECTORIES:
        if keys is not None and key not in keys:
            continue
        value = values.get(key, '').strip()
        label = FIELDS[key][0]
        try:
            paths = directories(value) if key in MULTI_DIRECTORIES else ([directory(value)] if value else [])
        except ValueError as exc:
            errors.append(f'{label}：{exc}')
            continue
        if not paths:
            if require_all and key in REQUIRED:
                errors.append(f'{label}尚未配置')
            continue
        for index, path in enumerate(paths, 1):
            suffix = f'第 {index} 项' if key in MULTI_DIRECTORIES else ''
            if key == 'PYCUT_OUTPUT_DIR':
                if path.exists() and not path.is_dir():
                    errors.append(f'{label}{suffix}不是目录')
            elif not path.is_dir():
                errors.append(f'{label}{suffix}不存在或无法访问：{path}')
    for key in ('PYCUT_MAIN_URL', 'PYCUT_BACKUP_URL'):
        if keys is not None and key not in keys:
            continue
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
    for key in MULTI_DIRECTORIES:
        if key in updates:
            updates[key] = encoded_directories(updates[key])
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
