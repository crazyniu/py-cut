"""Batch progress counts actual output videos, not attempted variants or stages."""
from collections import deque
from unittest import mock
import json
import pytest
from pycut import tui, tui_runner
from pycut.core.progress import PREFIX
from test_tui_layout import Screen


def jobs(n=10, count=3):
    return [['montage', '--source', f'/话术/{i}/1.mp4', '--fission', str(count)] for i in range(n)]


@pytest.mark.parametrize('size', [(24,100), (14,60)])
def test_normal_dashboard_only_shows_total_finished_and_one_bar(size):
    batch = tui_runner._montage_plan(jobs())
    batch.update(completed=6, current_planned=3)
    screen = Screen(size=size)
    events = {'裂变生成': {'success': 1, 'completed': 2, 'total': 3, 'failed': 1},
              '混剪合成': {'completed': 2, 'total': 5, 'detail': '合成画中画'}}
    with mock.patch.object(tui, '_attr', return_value=0):
        tui_runner._draw(screen, jobs()[0], 3, 10, deque(['technical filter log']), 0,
                         'running', events, batch=batch)
    text = '\n'.join(t for _,_,t in screen.printed)
    assert '需要生成 30 个视频 · 已生成 7 个' in text
    assert '10 个话术视频' in text and '每个生成 3 条' in text and '23%' in text
    assert sum(t.startswith('[') and ('█' in t or '░' in t) for _,_,t in screen.printed) == 1
    assert all(t not in text for t in ('2/5','2/3','合成画中画','technical filter log','流程'))


def test_error_logs_visible_without_opening_details():
    batch = tui_runner._montage_plan(jobs(2,3))
    batch.update(completed=1, current_planned=3)
    screen = Screen(size=(24,100))
    with mock.patch.object(tui, '_attr', return_value=0):
        tui_runner._draw(screen, jobs()[0],1,2,deque(['[失败] FFmpeg Invalid argument']),0,
                         'error',results=[{}],batch=batch)
    text='\n'.join(t for _,_,t in screen.printed)
    assert '已生成 1 个' in text and 'FFmpeg Invalid argument' in text
    assert '错误日志' in text


@pytest.mark.parametrize('code,cancelled', [(2,False),(130,True)])
def test_prior_success_and_partial_current_are_retained_on_failure_or_cancel(code,cancelled):
    screen=mock.Mock()
    screen.getch.return_value=10
    snapshots=[]
    def run(_screen,argv,index,total,history,batch):
        snapshots.append(dict(batch))
        success=3 if index==1 else 1
        history.append(PREFIX+json.dumps(dict(stage='裂变生成',completed=3,total=3,success=success,failed=3-success)))
        history.append(PREFIX+json.dumps(dict(stage='混剪合成',completed=5,total=5,success=5)))
        return (0,False) if index==1 else (code,cancelled)
    with mock.patch.object(tui_runner,'_run_one',side_effect=run) as child, mock.patch.object(tui_runner,'_draw') as draw, mock.patch('curses.flushinp'):
        assert tui_runner.run_jobs(screen,jobs())==(code,False)
    assert child.call_count==2
    assert snapshots[1]['completed']==3
    batch=draw.call_args.kwargs['batch']
    assert batch['completed']==4 and batch['total']==30
    assert draw.call_args.args[6]==('cancelled' if cancelled else 'error')


def test_missing_completion_event_is_not_assumed_success():
    assert tui_runner._finished_videos({'混剪合成':{'success':5}},3)==0
    assert tui_runner._finished_videos({'裂变生成':{'success':99}},3)==3
