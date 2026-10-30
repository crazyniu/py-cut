import json, tempfile, os
import pytest
from pathlib import Path
from unittest import mock
from pycut import web_config,config,settings,native_directory_picker

@pytest.mark.skipif(os.environ.get('PYCUT_TEST_BROWSER')!='1',reason='Real Chromium QA is opt-in; runtime has no browser dependency')
def test_browser_multidirectory_flow(tmp_path):
 from playwright.sync_api import sync_playwright
 with tempfile.TemporaryDirectory() as td:
  root=Path(td)
  for name in ('话术/a','混剪一','混剪二','画中画','前贴'):(root/name).mkdir(parents=True)
  (root/'.env').write_text('PYCUT_MAIN_KEY=test-private-secret\nPYCUT_REFERENCE_DIR='+str(root/'话术')+'\nPYCUT_CLIP_DIR='+str(root/'混剪一')+'\n')
  with mock.patch.object(config,'PROJECT_ROOT',root),mock.patch.object(config,'refresh'),mock.patch.dict(os.environ,{},clear=True),mock.patch.object(native_directory_picker,'choose_directory',side_effect=[native_directory_picker.PickerError('当前无图形桌面，请手动输入路径'),None,str(root/'画中画')]) as picker,sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
   page=browser.new_page(viewport={'width':1280,'height':1000});page.set_default_timeout(5000)
   errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
   url=web_config.start()
   try:
    page.goto(url);page.wait_for_function("()=>!loading")
    assert page.locator('#PYCUT_SOURCE_DIR').count()==1
    group=page.locator('.directory-list[data-key=PYCUT_CLIP_DIR]')
    assert group.locator('input').input_value()==str(root/'混剪一')
    page.locator('.add-directory[data-key=PYCUT_CLIP_DIR]').click()
    group.locator('input').nth(1).fill(str(root/'混剪二'))
    group.locator('.browse').nth(1).click();page.wait_for_function('()=>!picking')
    assert '手动输入路径' in page.locator('#status').inner_text()
    assert group.locator('input').nth(1).input_value()==str(root/'混剪二')
    group.locator('.browse').nth(1).click();page.wait_for_function('()=>!picking')
    assert '已取消选择' in page.locator('#status').inner_text()
    assert group.locator('input').nth(1).input_value()==str(root/'混剪二')
    group.locator('.browse').nth(1).click();page.wait_for_function('()=>!picking')
    assert picker.call_count==3
    assert group.locator('input').nth(0).input_value()==str(root/'混剪一')
    assert group.locator('input').nth(1).input_value()==str(root/'画中画')
    for key in ('PYCUT_PIP_DIR','PYCUT_INTRO_DIR'):
     for name in ('画中画','前贴'):
      page.locator(f'.add-directory[data-key={key}]').click()
      page.locator(f'.directory-list[data-key={key}] input').last.fill(str(root/name))
    # Same path is merged on save; removing a row leaves unrelated rows intact.
    page.locator('.add-directory[data-key=PYCUT_CLIP_DIR]').click()
    group.locator('input').nth(2).fill(str(root/'混剪一'))
    page.locator('#save').click();page.wait_for_function('()=>!saving')
    assert group.locator('input').count()==2
    assert json.loads(settings.read_env()['PYCUT_CLIP_DIR'])==[str(root/'混剪一'),str(root/'画中画')]
    page.reload();page.wait_for_function('()=>!loading')
    assert group.locator('input').count()==2
    for key in ('PYCUT_PIP_DIR','PYCUT_INTRO_DIR'):
     assert page.locator(f'.directory-list[data-key={key}] input').count()==2
    page.locator('.add-directory[data-key=PYCUT_CLIP_DIR]').click()
    group.locator('input').nth(2).fill(str(root/'不存在'))
    before=(root/'.env').read_bytes()
    page.locator('#save').click();page.wait_for_function('()=>!saving')
    assert group.locator('input').nth(2).get_attribute('aria-invalid')=='true'
    assert '第 3 项' in group.locator('.field-error').nth(2).inner_text()
    assert (root/'.env').read_bytes()==before
    group.locator('.remove-directory').nth(2).click()
    assert group.locator('input').count()==2
    page.locator('#save').click();page.wait_for_function('()=>!saving')
    page.screenshot(path=str(tmp_path/'multidir-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(tmp_path/'multidir-mobile.png'),full_page=True)
    assert page.evaluate('()=>document.documentElement.scrollWidth')<=390
    assert page.locator('fieldset[data-key=PYCUT_CLIP_DIR] .is-required').inner_text()=='必填'

    page.evaluate('()=>window.scrollTo(0,document.documentElement.scrollHeight)')
    assert page.evaluate("()=>document.querySelector('#PYCUT_STICKER_DIR').getBoundingClientRect().bottom < document.querySelector('.bottom').getBoundingClientRect().top")

    page.locator('#PYCUT_OUTPUT_DIR').fill(str(root/'out'))
    assert page.locator('#page-state').inner_text()=='未保存'
    page.keyboard.press('Control+s')
    page.wait_for_function('()=>!saving')
    assert page.locator('#page-state').inner_text()=='已保存'
    page.get_by_role('button',name='LLM 网关',exact=True).click()
    assert page.locator('#PYCUT_MAIN_KEY').input_value()==''
    assert page.locator('#PYCUT_MAIN_KEY').get_attribute('placeholder')=='已配置，留空保持不变'
    assert 'test-private-secret' not in page.content()
    # Explicitly invalid JSON submitted directly is rejected; CSRF/token still apply.
    feedback=page.evaluate("""async()=>{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json','X-Config-Token':token},body:JSON.stringify({PYCUT_CLIP_DIR:'[broken'})});return r.status;}""")
    assert feedback==400
    assert page.evaluate("async()=>{const r=await fetch('/api/config');return r.status;}")==403
    assert not errors,errors
    print('Real Chromium QA: multi-row browse target/cancel/add/remove, duplicate merge, save/reload, indexed invalid/no-write, key redaction, invalid JSON, CSRF and 390px overflow: PASS')
   finally:
    browser.close();web_config.stop()
