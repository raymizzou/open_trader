from copy import deepcopy
from datetime import UTC,datetime,timedelta
import pytest
from open_trader import prediction_arbitrage_health as health
from open_trader.polymarket_lp_monitor import unknown_summary


class CapturedNotifier:
    def __init__(self):self.messages=[];self.fail=False
    def notify(self,title,message):
        if self.fail:raise OSError('offline notification boundary')
        self.messages.append((title,message))


def sample():
    value=unknown_summary()
    for name in ('orders','cancellations','pools','balance','fills'):
        value[name].update(status='normal',reason='已确认')
    value['fills'].update(items=[])
    return value


def test_monitor_alerts_transitions_recovery_and_each_fill_once(tmp_path):
    now=datetime(2026,10,26,2,tzinfo=UTC)
    notifier=CapturedNotifier();value=sample()
    state=tmp_path/'lp-notify.json'
    alerts=health.LPMonitorAlerts(state_path=state)
    alerts.observe(value,notifier,now=now)
    assert notifier.messages==[]  # first complete observation is the baseline
    value['orders'].update(status='abnormal',reason='连续五分钟缺单')
    alerts.observe(value,notifier,now=now)
    alerts.observe(value,notifier,now=now)
    assert len(notifier.messages)==1 and '异常' in notifier.messages[-1][0]
    value['orders'].update(status='unknown',reason='挂单未读全')
    alerts.observe(value,notifier,now=now)
    assert len(notifier.messages)==2 and '未知' in notifier.messages[-1][0]
    value['orders'].update(status='normal',reason='已恢复')
    alerts.observe(value,notifier,now=now)
    assert len(notifier.messages)==3 and '恢复' in notifier.messages[-1][0]
    a=dict(id='fill-a',order_id='a',market='same-market',side='BUY',quantity='40',price='.35',time=now.isoformat())
    b={**a,'id':'fill-b','order_id':'b'}
    value['fills'].update(status='attention',items=[a,b])
    alerts.observe(value,notifier,now=now);alerts.observe(value,notifier,now=now)
    assert len(notifier.messages)==5  # same market/time are two exact fill identities
    restarted=health.LPMonitorAlerts(state_path=state)
    restarted.observe(value,notifier,now=now)
    assert len(notifier.messages)==5
    notifier.fail=True
    value['fills']['items'].append({**a,'id':'fill-c','order_id':'c'})
    restarted.observe(value,notifier,now=now)
    assert len(notifier.messages)==5
    notifier.fail=False
    resumed=health.LPMonitorAlerts(state_path=state)
    resumed.observe(value,notifier,now=now);resumed.observe(value,notifier,now=now)
    assert len(notifier.messages)==6 and 'c' in notifier.messages[-1][1]
    # Existing quiet-hours clock: Shanghai 00:00 suppresses; retain pending.
    night=now.replace(hour=16)
    value['fills']['items'].append({**a,'id':'fill-d','order_id':'d'})
    resumed.observe(value,notifier,now=night)
    assert len(notifier.messages)==6
    resumed.observe(value,notifier,now=night+timedelta(hours=10))
    resumed.observe(value,notifier,now=night+timedelta(hours=10))
    assert len(notifier.messages)==7 and 'd' in notifier.messages[-1][1]
    from open_trader.notifications import CompositeNotifier,FeishuWebhookNotifier
    attempts=[]
    def post_json(*args):
        attempts.append(args[1])
        return {'code':1 if len(attempts)==1 else 0}
    composite=CompositeNotifier([FeishuWebhookNotifier(webhook_url='http://offline.invalid',post_json=post_json)])
    direct=health.LPMonitorAlerts();initial=sample()
    direct.observe(initial,composite,now=now)
    initial['balance'].update(status='unknown',reason='余额未读全')
    direct.observe(initial,composite,now=now)
    assert len(direct.pending)==1
    direct.observe(initial,composite,now=now);direct.observe(initial,composite,now=now)
    assert len(attempts)==2 and direct.pending==[]  # CompositeNotifier swallowing cannot fake delivery success


def test_health_loop_checks_lp_every_minute_even_when_nleg_paused(tmp_path,capsys):
    from tests.test_prediction_arbitrage_health import _paused_healthz
    start=datetime(2026,10,26,2,tzinfo=UTC);clock=[start]
    polls=[];health_polls=[];notifier=CapturedNotifier()
    class Stop(Exception):pass
    def sleep(seconds):
        clock[0]+=timedelta(seconds=seconds)
        if clock[0]>start+timedelta(seconds=241):raise Stop
    def fetch_lp(url,timeout):
        polls.append((clock[0]-start).total_seconds())
        result=sample()
        if len(polls)>1:result['orders'].update(status='abnormal',reason='缺单超过五分钟')
        return result
    def fetch_health(*args):health_polls.append((clock[0]-start).total_seconds());return _paused_healthz()
    with pytest.raises(Stop):
        health.run_service(notifier,url='http://127.0.0.1:8769',interval_seconds=180,
            lp_monitor=True,lp_monitor_state=tmp_path/'state.json',fetch_lp_monitor=fetch_lp,
            fetch_state=lambda *_:pytest.fail('paused N_LEG must not read its state'),fetch_healthz=fetch_health,
            now_fn=lambda:clock[0],sleep_fn=sleep)
    assert polls==[60,120,180,240]
    assert health_polls==[180]  # existing independent health cadence is unchanged
    assert len([title for title,_ in notifier.messages if '东京做市' in title])==1
    with pytest.raises(SystemExit) as help_exit:health.main(['--help'])
    assert help_exit.value.code==0 and '--lp-monitor' in capsys.readouterr().out
