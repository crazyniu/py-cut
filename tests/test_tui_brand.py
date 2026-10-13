"""One local settings URL on every TUI surface, without browser focus theft."""
from collections import deque
from pathlib import Path
import urllib.request
from unittest import mock
import pytest
from pycut import tui, tui_runner, web_config, config, settings


class Screen:
    def __init__(self,size): self.size=size; self.drawn={}; self.cells={}
    def getmaxyx(self): return self.size
    def addstr(self,y,x,text,attr=0):
        assert 0<=y<self.size[0] and 0<=x<self.size[1]
        assert tui._disp_width(text)<=self.size[1]-x-1
        self.drawn[y,x]=text
        for char in text:
            self.cells[y,x]=char
            x+=tui._char_width(char)
    def erase(self): self.drawn={}; self.cells={}
    def move(self,y,x): self.cursor=(y,x)
    def clrtoeol(self):
        row,column=self.cursor
        for x in range(column,self.size[1]): self.cells[row,x]=" "
    def refresh(self): pass
    def keypad(self,enabled): pass
    def getch(self): return 27


@pytest.mark.parametrize('size',[(14,60),(20,60),(24,100)])
@pytest.mark.parametrize('surface',['home','wizard','flash','running','results','confirm','selection','count','text'])
def test_header_url_is_identical_and_visible_on_all_surfaces(size,surface):
    screen=Screen(size)
    url='http://127.0.0.1:65535/'
    with mock.patch.object(web_config,'start',return_value=url) as start,mock.patch.object(tui,'_attr',return_value=0):
        if surface=='home': tui._home_brand(screen)
        elif surface=='wizard': tui._frame(screen,'确认执行','说明')
        elif surface=='flash': tui._flash(screen,'请检查配置' * 100)
        elif surface=='running': tui_runner._draw(screen,['slice'],1,1,deque(),0,'running')
        elif surface=='confirm': tui._confirm(screen,[("目录","/路径")],[("字幕","选项",True),("去重","选项",False)])
        elif surface=='selection': tui._multi_select(screen,'选择爆款',[Path('/视频.mp4')])
        elif surface=='count': tui._set_fission_counts(screen,[Path('/视频.mp4')],{'/视频.mp4':1})
        elif surface=='text': tui._prompt_text(screen,0,2,size[1],'输入')
        else: tui_runner._draw_results(screen,[dict(command='slice',code=0,cancelled=False,events={})],1,'success',deque(),0,False)
    logo=tui._LOGO if size[0]>=20 else ('▶ PYCUT',)
    for row,line in enumerate(logo):
        assert screen.drawn[row,2]==line
        visible="".join(screen.cells.get((row,x)," ") for x in range(size[1]))
        assert visible[2:2+len(line)]==line
    settings_x=2+max(tui._disp_width(line) for line in logo)+5
    wide=len(logo)>1 and settings_x+tui._disp_width("设置素材目录和模型，保存后回到终端")<=size[1]-2
    url_row,url_x=(1,settings_x) if wide else (len(logo)+1,2)
    assert screen.drawn[url_row,url_x]==url
    visible=''.join(screen.cells.get((url_row,x),' ') for x in range(size[1]))
    assert visible[url_x:url_x+len(url)]==url
    divider=len(logo) if wide else len(logo)+2
    assert screen.drawn[divider,2]=='─'*(size[1]-4)
    visible=''.join(screen.cells.get((divider,x),' ') for x in range(size[1]))
    assert visible[2:size[1]-2]=='─'*(size[1]-4)
    descriptions=''.join(screen.drawn.values())
    assert '复制地址到浏览器打开' in descriptions
    assert ('素材目录与 LLM 配置' if wide else '素材/LLM设置') in descriptions
    if surface=="confirm":
        actions=[row for (row,column),text in screen.drawn.items() if "确认并开始处理" in text]
        assert actions and max(actions)<size[0]-2
    start.assert_called_with(open_browser=False)


def test_actual_service_accessible_without_browser_and_stops():
    web_config.stop()
    with mock.patch.object(web_config.webbrowser,'open') as browser:
        url=web_config.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url,timeout=2) as response:
                assert response.status==200
            browser.assert_not_called()
            port=web_config._SERVER.server_port
        finally:
            web_config.stop()
        assert web_config._SERVER is None
        with pytest.raises(OSError):
            opener.open(f'http://127.0.0.1:{port}/',timeout=1)


def test_missing_configuration_shows_address_without_browser():
    terminal=Screen((14,60))
    with mock.patch.object(web_config.webbrowser,'open') as browser,mock.patch.object(config,'refresh'),mock.patch.object(settings,'effective',return_value={}),mock.patch.object(tui,'_attr',return_value=0):
        try:
            assert tui._run_feature_wizard(terminal,'slice',60) is None
            assert terminal.drawn[2,2].startswith('http://127.0.0.1:')
            browser.assert_not_called()
        finally: web_config.stop()


def test_tui_start_and_w_never_launch_browser():
    terminal=Screen((14,60))
    keys=iter([ord('w'),10,ord('q')])
    terminal.getch=lambda: next(keys)
    with mock.patch.object(web_config.webbrowser,'open') as browser,mock.patch.object(tui.sys,'stdin',mock.Mock(isatty=lambda:True)),mock.patch.object(tui.sys,'stdout',mock.Mock(isatty=lambda:True)),mock.patch.object(tui.curses,'wrapper',side_effect=lambda app: tui._loop(terminal)[0]=='quit'),mock.patch.object(tui,'_attr',return_value=0):
        tui.run()
        assert terminal.drawn[2,2].startswith('http://127.0.0.1:')
        browser.assert_not_called()
        assert web_config._SERVER is None
