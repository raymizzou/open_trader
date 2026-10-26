from datetime import UTC, datetime
import json
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import pytest
from open_trader import lp_monitor_readonly as readonly
from open_trader.polymarket_lp_monitor import unknown_summary
from tests.test_frontend_gateway import _Upstream,_running


def test_readonly_listener_exposes_only_summary_and_page():
    upstream=_Upstream(); other=_Upstream()
    value=unknown_summary();value['orders'].update(status='normal',reason='<img src=x onerror=evil()>')
    value['private_key']='must-never-leak'
    upstream.response_body=json.dumps(value).encode()
    with _running(upstream),_running(other):
        source=f'http://127.0.0.1:{upstream.server_address[1]}/api/prediction-arbitrage/lp/monitor'
        server=readonly.create_readonly_server(host='127.0.0.1',port=0,upstream_url=source)
        with _running(server):
            base=f'http://127.0.0.1:{server.server_address[1]}'
            with urlopen(base+'/',timeout=5) as response:
                page=response.read().decode()
                assert response.headers.get('Set-Cookie') is None
                assert page.count('data-lp-monitor=')==5
                assert '<img src=x' not in page and '&lt;img' in page
                assert 'must-never-leak' not in page
                assert 'lp-cancel' not in page and 'lp-auto-enable' not in page
            with urlopen(base+'/api/prediction-arbitrage/lp/monitor',timeout=5) as response:
                body=json.load(response)
                assert body['orders']['status']=='normal' and 'private_key' not in body
            before=len(upstream.requests)
            for method,path in [('POST','/'),('PUT','/'),('DELETE','/'),('GET','/healthz'),
                    ('GET','/api/prediction-arbitrage/lp/orders'),('GET','/?url=http://example.com'),
                    ('GET','/api/prediction-arbitrage/lp/monitor?target=other')]:
                with pytest.raises(HTTPError) as error:
                    urlopen(Request(base+path,method=method),timeout=5)
                assert error.value.code in (400,404,405)
            assert len(upstream.requests)==before
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base+'/',headers={'Host':'untrusted.example'}),timeout=5)
            assert error.value.code==403
            assert len(upstream.requests)==before
            upstream.response_status=302
            upstream.response_headers=[('Location',f'http://127.0.0.1:{other.server_address[1]}/')]
            with urlopen(base+'/api/prediction-arbitrage/lp/monitor',timeout=5) as response:
                failed=json.load(response)
                assert all(failed[k]['status']=='unknown' for k in ('orders','cancellations','pools','balance','fills'))
            assert other.requests==[]
    assert all(r['method']=='GET' and r['path']=='/api/prediction-arbitrage/lp/monitor' for r in upstream.requests)
    assert all('Cookie' not in r['headers'] and 'Authorization' not in r['headers'] for r in upstream.requests)
    for bad in ['https://127.0.0.1:8769/api/prediction-arbitrage/lp/monitor',
                'http://localhost:8769/api/prediction-arbitrage/lp/monitor',
                'http://127.0.0.1:8769/api/prediction-arbitrage/lp/dashboard',
                'http://user:secret@127.0.0.1:8769/api/prediction-arbitrage/lp/monitor',
                'http://10.0.0.1:8769/api/prediction-arbitrage/lp/monitor']:
        with pytest.raises(ValueError):readonly.create_readonly_server(host='127.0.0.1',port=0,upstream_url=bad)


def test_readonly_bind_rejects_public_and_wildcard():
    for good in ('127.0.0.1','::1','100.64.0.1','100.127.255.254','fd7a:115c:a1e0::1234'):
        assert readonly.validate_bind(good)==good
    for bad in ('0.0.0.0','::','8.8.8.8','192.168.1.1','10.0.0.1','100.128.0.1','localhost',
                'tokyo-lp','fd00::1','::ffff:192.168.1.1','127.0.0.1%lo0',''):
        with pytest.raises(ValueError):readonly.validate_bind(bad)


def test_monitor_cards_render_unknown_and_escape_details():
    from pathlib import Path
    from tests.test_dashboard_web import run_dashboard_js
    for section,field in [('cancellations','hour'),('cancellations','items'),('orders','counts')]:
        malformed=unknown_summary();malformed[section][field]=None
        page=readonly.render_page(malformed)
        assert page.count('data-lp-monitor=')==5 and '未知' in page
    zero=unknown_summary();zero['cancellations']['hour']=dict(success=0,rejected=0,unknown=0,blocked=0)
    assert '卡单 0' in readonly.render_page(zero)
    sample=unknown_summary()
    sample['orders'].update(reason='<script>bad()</script>',items=[dict(order_id='<img src=x>',side='BUY',pool=None)])
    html=json.loads(run_dashboard_js('console.log(JSON.stringify(predictionLpCard({lp_dashboard:{monitor:'+json.dumps(sample)+',auto:{desired_running:false,scheduler_running:true,slots:{occupied:0}}}})));'))
    assert html.count('data-lp-monitor=')==5
    assert '<details' in html and '<summary' in html
    assert '&lt;script&gt;' in html and '<script>bad' not in html and '<img src=x>' not in html
    assert html.index('data-lp-monitor=') < html.index('data-action="lp-cancel-all"')
    assert '未知' in html
    assert 'data-lp-auto-action="enable"' in html and 'data-lp-auto-field="budget_usd"' in html
    css=(Path(__file__).parents[1]/'src/open_trader/dashboard_static/dashboard.css').read_text()
    assert '.lp-monitor-cards' in css and '@media' in css
