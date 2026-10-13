"""One local settings URL on every TUI surface, without browser focus theft."""
from collections import deque
import urllib.request
from unittest import mock
import pytest
from pycut import tui, tui_runner, web_config, config, settings


class Screen:
    def __init__(self,size): self.size=size; self.drawn={}
    def getmaxyx(self): return self.size
    def addstr(self,y,x,text,attr=0):
        assert 0<=y<self.size[0] and 0<=x<self.size[1]
        assert tui._disp_width(text)<=self.size[1]-x-1
        self.drawn[y,x]=text
    def erase(self): self.drawn={}
    def refresh(self): pass
    def keypad(self,enabled): pass
    def getch(self): return 27


@pytest.mark.parametrize('size',[(14,60),(24,100)])
@pytest.mark.parametrize('surface',['home','wizard','flash','running','results'])
def test_header_url_is_identical_and_visible_on_all_surfaces(size,surface):
    screen=Screen(size)
    url='http://127.0.0.1:65535/'
    with mock.patch.object(web_config,'start',return_value=url) as start,mock.patch.object(tui,'_attr',return_value=0):
        if surface=='home': tui._home_brand(screen)
        elif surface=='wizard': tui._frame(screen,'确认执行','说明')
        elif surface=='flash': tui._flash(screen,'请检查配置')
        elif surface=='running': tui_runner._draw(screen,['slice'],1,1,deque(),0,'running')
        else: tui_runner._draw_results(screen,[dict(command='slice',code=0,cancelled=False,events={})],1,'success',deque(),0,False)
    assert screen.drawn[0,2]=='▶ PYCUT'
    assert screen.drawn[1,2]=='设置 '+url
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
            assert terminal.drawn[1,2].startswith('设置 http://127.0.0.1:')
            browser.assert_not_called()
        finally: web_config.stop()


def test_tui_start_and_w_never_launch_browser():
    terminal=Screen((14,60))
    keys=iter([ord('w'),10,ord('q')])
    terminal.getch=lambda: next(keys)
    with mock.patch.object(web_config.webbrowser,'open') as browser,mock.patch.object(tui.sys,'stdin',mock.Mock(isatty=lambda:True)),mock.patch.object(tui.sys,'stdout',mock.Mock(isatty=lambda:True)),mock.patch.object(tui.curses,'wrapper',side_effect=lambda app: tui._loop(terminal)[0]=='quit'),mock.patch.object(tui,'_attr',return_value=0):
        tui.run()
        assert terminal.drawn[1,2].startswith('设置 http://127.0.0.1:')
        browser.assert_not_called()
        assert web_config._SERVER is None
