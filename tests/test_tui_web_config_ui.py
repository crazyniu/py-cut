"""配置页 UI 契约的轻量回归；保留全部现有目录选择和 .env 协议。"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from pycut import settings, web_config


def test_page_has_navigation_save_state_and_live_feedback():
    page = web_config.page()
    assert "DIRECTORY_FIELDS" not in page and "LLM_FIELDS" not in page
    assert 'id="page-state"' in page
    assert 'data-state="loading"' in page
    assert 'class="bottom"' in page
    assert 'id="status" role="status" aria-live="polite"' in page
    assert 'data-panel="directories"' in page
    assert 'data-panel="llm"' in page
    assert 'aria-pressed="true"' in page
    assert "b.setAttribute('aria-current','page')" in page
    assert 'function markState(' in page
    assert 'new ResizeObserver(measureBottom)' in page
    assert "form.requestSubmit()" in page
    assert "window.addEventListener('beforeunload'" in page


def test_labels_and_directory_fields_preserve_existing_protocol():
    page = web_config.page()
    assert page.count('class="optional is-required">必填') == len(settings.REQUIRED)
    for key in settings.FIELDS:
        assert f'id="{key}"' in page
        assert f'id="{key}-help"' in page
        assert f'id="{key}-error"' in page
    for key in settings.MULTI_DIRECTORIES:
        assert f'class="directory-list" data-key="{key}"' in page
        assert f'class="add-directory" data-key="{key}"' in page
        assert f'name="{key}" type="hidden"' in page
    assert 'class="optional">可选' in page


def test_mobile_layout_and_footer_have_focus_safe_space():
    page = web_config.page()
    assert '@media(max-width:520px)' in page
    assert '@media(max-width:760px)' in page
    assert 'calc(var(--savebar-height) + 48px)' in page
    assert 'scroll-margin-bottom:calc(var(--savebar-height) + 32px)' in page
    assert '.directory-row input{grid-column:1/-1}' in page
    assert '.directory-list:empty:before' in page


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_embedded_config_script_has_valid_javascript(tmp_path):
    page = web_config.page()
    scripts = re.findall(r'<script>(.*?)</script>', page, re.DOTALL)
    assert len(scripts) == 1
    path = tmp_path / "config-ui.js"
    path.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
