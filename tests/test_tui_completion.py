"""Completed tasks stay visible until explicitly acknowledged."""
from collections import deque
import json
from pathlib import Path
from unittest import mock

import pytest
from pycut import tui, tui_runner
from pycut.core.progress import PREFIX


def event(count, output='/结果', review=0):
    return PREFIX + json.dumps(dict(stage='编码',completed=count,total=count,success=count-review,failed=0,skipped=0,review=review,output=output))


@pytest.mark.parametrize('command',['slice','cover','dedup','montage'])
@pytest.mark.parametrize('code,cancelled,review,status',[(0,False,0,'success'),(0,False,1,'review'),(2,False,0,'error'),(130,True,0,'cancelled')])
def test_all_command_end_states_wait_for_confirmation(command,code,cancelled,review,status):
    screen=mock.Mock()
    screen.getch.side_effect=[32,-1,10]
    def run(_screen,_argv,_index,_total,history):
        history.append(event(3,review=review))
        return code,cancelled
    with mock.patch.object(tui_runner,'_run_one',side_effect=run),mock.patch.object(tui_runner,'_draw') as draw,mock.patch('curses.flushinp') as flush:
        assert tui_runner.run_jobs(screen,[[command]])==(code,False)
    assert draw.call_count==3
    assert draw.call_args.args[6]==status
    assert draw.call_args.kwargs['results'][0]['events']['编码']['total']==3
    flush.assert_called_once()


def test_distinct_job_counts_and_failure_stops_dependencies():
    screen=mock.Mock()
    screen.getch.return_value=ord('q')
    def run(_screen,argv,index,total,history):
        assert not any(PREFIX in line for line in history)
        history.append(event(index*3,output=f'/结果{index}'))
        return (2 if index==2 else 0),False
    with mock.patch.object(tui_runner,'_run_one',side_effect=run) as child,mock.patch.object(tui_runner,'_draw') as draw,mock.patch('curses.flushinp'):
        assert tui_runner.run_jobs(screen,[['slice'],['dedup'],['montage']])==(2,True)
    assert child.call_count==2
    results=draw.call_args.kwargs['results']
    assert [r['events']['编码']['completed'] for r in results]==[3,6]
    assert [r['events']['编码']['output'] for r in results]==['/结果1','/结果2']


@pytest.mark.parametrize('size',[(24,100),(10,40),(5,24)])
def test_results_without_logs_visible_on_small_terminal(size):
    screen=mock.Mock()
    screen.getmaxyx.return_value=size
    written=[]
    with mock.patch.object(tui,'_safe_add',side_effect=lambda *a:written.append(a[3])):
        tui_runner._draw_results(screen,[dict(command='cover',code=0,cancelled=False,events={})],1,'success',deque(),0,False)
    assert any('全部处理完成' in line for line in written)
    assert any('已结束' in line for line in written)
    assert any('Enter/Esc' in line for line in written)


def test_actual_child_no_output_does_not_inherit_old_stages(tmp_path):
    # Execute the real subprocess reader, not a mock Popen. No media work needed.
    (tmp_path/'pycut.py').write_text('pass\n')
    screen=mock.Mock()
    screen.getch.return_value=-1
    history=deque([event(999)],maxlen=600)
    drawn=[]
    with mock.patch.object(tui_runner,'_ROOT',tmp_path),mock.patch.object(tui_runner,'_draw',side_effect=lambda *args:drawn.append(args)):
        assert tui_runner._run_one(screen,['slice'],1,1,history)==(0,False)
    assert all(not args[7] for args in drawn)
