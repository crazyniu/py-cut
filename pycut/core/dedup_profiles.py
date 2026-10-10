"""Local, non-secret dedup account presets (versioned JSON).

Profiles are deliberately limited to existing, reviewed CLI options: they do not
contain account credentials, remote endpoints, destination paths or arbitrary
ffmpeg arguments. The no-profile CLI path remains unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from .. import config

VERSION = 1
MAX_JSON_BYTES = 64 * 1024
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# A fixed allow-list prevents typos and sensitive fields from silently leaking
# into the report. Defaults are taken from the parser, never duplicated here.
PROFILE_FIELDS = frozenset({
    "preset", "seed", "mirror", "speed", "border", "badge",
    "phash_min", "psnr_min", "workers",
})
_BOOLEAN = frozenset({"mirror", "speed", "border", "badge"})
_FLOAT = frozenset({"phash_min", "psnr_min"})
_INTEGER = frozenset({"seed", "workers"})


class ProfileError(ValueError):
    """Invalid profile schema or ambiguous CLI configuration."""


def _record(namespace: argparse.Namespace, field: str) -> None:
    current = set(getattr(namespace, "_explicit_profile_fields", ()))
    current.add(field)
    namespace._explicit_profile_fields = current


class ExplicitValue(argparse.Action):
    """Store existing CLI value, remembering it was explicitly supplied."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        _record(namespace, self.dest)


class ExplicitBoolean(argparse.BooleanOptionalAction):
    """Support --flag/--no-flag and distinguish them from argparse defaults."""

    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(parser, namespace, values, option_string=option_string)
        _record(namespace, self.dest)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ProfileError(f"JSON 存在重复键: {key}")
        out[key] = value
    return out


def _valid_value(key: str, value: object) -> bool:
    if key == "preset":
        return isinstance(value, str) and value in config.DEDUP_PRESETS
    if key in _BOOLEAN:
        return type(value) is bool
    if key in _INTEGER:
        # JSON bool is an int in Python; reject it and prohibit risky huge worker counts.
        return type(value) is int and 0 <= value <= (64 if key == "workers" else 2**32 - 1)
    if key in _FLOAT:
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 <= value <= (64 if key == "phash_min" else 100)
        )
    return False


def load_account_profile(path: str | Path, account: str) -> dict:
    """Return validated overrides for exactly one local account alias.

    Raises ProfileError without ever embedding raw JSON values in diagnostics.
    """
    if not _ACCOUNT_NAME.fullmatch(account):
        raise ProfileError("账号别名仅允许 1～64 位英数、点、下划线与连字符")
    file = Path(path).expanduser()
    try:
        with file.open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
    except OSError as exc:
        raise ProfileError(f"无法读取本地预设文件: {type(exc).__name__}") from exc
    if len(data) > MAX_JSON_BYTES:
        raise ProfileError("预设 JSON 过大（上限 64 KiB）")
    try:
        raw = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"预设 JSON 解析失败: {type(exc).__name__}") from exc

    if type(raw) is not dict or set(raw) != {"version", "accounts"}:
        raise ProfileError("顶层只能包含 version 与 accounts")
    if type(raw["version"]) is not int or raw["version"] != VERSION:
        raise ProfileError(f"只支持版本 version={VERSION}")
    accounts = raw["accounts"]
    if type(accounts) is not dict or not 1 <= len(accounts) <= 100:
        raise ProfileError("accounts 必须包含 1～100 个账号对象")
    for name in accounts:
        if not isinstance(name, str) or not _ACCOUNT_NAME.fullmatch(name):
            raise ProfileError("accounts 中含非法账号别名")
    if account not in accounts:
        raise ProfileError(f"找不到账号别名: {account}")
    overrides = accounts[account]
    if type(overrides) is not dict or not overrides:
        raise ProfileError(f"账号 {account} 需要至少一个合法配置项")
    for key, value in overrides.items():
        if key not in PROFILE_FIELDS:
            # Echo the field NAME only. Never print the rejected value.
            raise ProfileError(f"不支持或不允许的配置字段: {key}")
        if not _valid_value(key, value):
            raise ProfileError(f"配置字段取值不合法: {key}")
    return dict(overrides)


def apply_account_profile(args: argparse.Namespace) -> dict | None:
    """Apply account defaults unless the corresponding CLI flag was explicit.

    Returns a non-sensitive, report-safe snapshot or None with no profile.
    No-profile mode does not change any argparse destination.
    """
    profile_file = getattr(args, "profile_file", "")
    account = getattr(args, "account", "")
    if not profile_file and not account:
        return None
    if not profile_file or not account:
        raise ProfileError("--account 与 --profile-file 必须同时提供")
    profile = load_account_profile(profile_file, account)
    explicit = set(getattr(args, "_explicit_profile_fields", ()))
    for field, value in profile.items():
        if field not in explicit:
            setattr(args, field, value)
    effective = {field: getattr(args, field) for field in sorted(PROFILE_FIELDS)}
    sources = {
        field: ("cli" if field in explicit else
                "account" if field in profile else "default")
        for field in sorted(PROFILE_FIELDS)
    }
    return {
        "version": VERSION,
        "account": account,
        "effective": effective,
        "sources": sources,
    }
