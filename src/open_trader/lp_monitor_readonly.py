"""Separate read-only LP summary listener; never a prediction-service proxy."""
from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .polymarket_lp_monitor import SECTIONS, mapping, moment, unknown_summary

SUMMARY_PATH = '/api/prediction-arbitrage/lp/monitor'
LABELS = dict(orders='当前挂单', cancellations='撤单情况', pools='双池运行', balance='余额与可用资金', fills='本钱包成交')
STATUS = dict(normal='正常',attention='注意',abnormal='异常',unknown='未知')
FIELDS = {
    'as_of','timezone','status','reason','counts','items','trial','income','unassigned','sell','observed_at',
    'count','target','shortage_seconds','budget_usd','used_usd','last_change_at','last_change_reason',
    'desired_running','effective_running','fatal','eligible_candidates','candidate_last_success_at','candidate_failure_seconds',
    'balance_usd','allowance_usd','capacity_usd','checked_at','inventory_cost_usd','hour','today','amount_usd',
    'success','rejected','unknown','blocked','stuck','basis','request_id','order_id','condition_id','token_id','side',
    'pool','outcome','requested_at','id','trade_id','market','price','quantity','time','coverage','boot_reference',
    'total','by_reason','available',
}


def safe_summary(value):
    """Closed display field projection. Never forward credentials or arbitrary objects."""
    if not isinstance(value, dict) or any(not isinstance(value.get(k),dict)
            or value[k].get('status') not in STATUS or not isinstance(value[k].get('reason'),str) for k in SECTIONS):
        return unknown_summary()
    def clean(v):
        if isinstance(v,dict): return {k:clean(x) for k,x in v.items() if k in FIELDS}
        if isinstance(v,list): return [clean(x) for x in v]
        if v is None or type(v) in (int,float,bool): return v
        return str(v)[:2000]
    result={k:clean(value[k]) for k in SECTIONS} | {k:clean(value.get(k)) for k in ('as_of','timezone')}
    defaults=unknown_summary()
    for key in SECTIONS:
        data=result[key]
        mapping_fields={'orders':('counts',),'cancellations':('hour','today'),
                        'pools':('items',),'fills':('hour','today')}.get(key,())
        list_fields=('items','stuck') if key=='cancellations' else ('items',) if key in ('orders','fills') else ()
        malformed=any(data.get(field) is not None and not isinstance(data[field],dict) for field in mapping_fields)
        malformed=malformed or any(not isinstance(data.get(field,[]),list) for field in list_fields)
        if key in ('cancellations','fills'):
            malformed=malformed or any(not isinstance(data.get(field),dict) for field in mapping_fields)
        if malformed:
            result[key]={**defaults[key],'reason':'明细来源不完整，当前情况未知'}
        else:
            result[key]={**defaults[key],**data}
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_upstream(url):
    try:
        parsed=urlsplit(url)
        host=ipaddress.ip_address(parsed.hostname or '')
        if (parsed.scheme != 'http' or not host.is_loopback or parsed.username is not None
                or parsed.password is not None or parsed.path != SUMMARY_PATH or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535):
            raise ValueError('fixed_loopback_summary_url_required')
    except (ValueError,TypeError) as exc:
        raise ValueError('fixed_loopback_summary_url_required') from exc
    return url


def fetch_summary(url, *, timeout=5):
    validate_upstream(url)
    try:
        opener=build_opener(ProxyHandler({}),_NoRedirect())
        with opener.open(Request(url,headers={'Accept':'application/json'}),timeout=timeout) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: return unknown_summary()
            result=safe_summary(json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite'))))
            from datetime import UTC,datetime
            at=moment(result.get('as_of'))
            if at is None or not 0 <= (datetime.now(UTC)-at).total_seconds() <= 90:
                return unknown_summary()
            return result
    except Exception:
        return unknown_summary()


def _text(value):
    if value is None: return '未知'
    if value is True: return '是'
    if value is False: return '否'
    return escape(str(value),quote=True)


def _pairs(values):
    return '<dl>'+''.join('<dt>'+_text(label)+'</dt><dd>'+_text(value)+'</dd>' for label,value in values)+'</dl>'


def _table(rows,columns):
    if not rows: return '<p>暂无已确认明细</p>'
    return '<div class="table-wrap"><table><thead><tr>'+''.join('<th scope="col">'+_text(label)+'</th>' for _,label in columns)+'</tr></thead><tbody>'+''.join(
        '<tr>'+''.join('<td>'+_text(mapping(row).get(key))+'</td>' for key,_ in columns)+'</tr>' for row in rows)+'</tbody></table></div>'


def render_page(value):
    value=safe_summary(value)
    cards=[]
    for key in SECTIONS:
        data=value[key];state=data['status']
        details=''
        if key=='orders':
            counts=mapping(data.get('counts'))
            big='试挂 '+_text(counts.get('trial'))+' / 收益 '+_text(counts.get('income'))
            details=_pairs([('未归池买单',counts.get('unassigned')),('卖单',counts.get('sell')),('确认时间',data.get('observed_at'))])
            details+=_table(data.get('items'), [('order_id','订单'),('condition_id','市场'),('side','方向'),('pool','所属池')])
        elif key=='pools':
            big='候选 '+_text(data.get('eligible_candidates'))
            details=_pairs([('想开',data.get('desired_running')),('实际开',data.get('effective_running')),
                ('致命错误',data.get('fatal')),('候选上次刷新成功',data.get('candidate_last_success_at'))])
            for pool,label in [('trial','试挂池'),('income','收益池')]:
                row=mapping(mapping(data.get('items')).get(pool))
                details+='<h3>'+label+'</h3>'+_pairs([(name,row.get(field)) for field,name in [
                    ('count','当前挂单'),('target','目标'),('budget_usd','预算（USD）'),('used_usd','已占用（USD）'),
                    ('last_change_at','最近补位或换单'),('last_change_reason','时间来源说明')]])
        elif key=='balance':
            big='可用 $'+_text(data.get('capacity_usd'))
            details=_pairs([(label,data.get(field)) for field,label in [('balance_usd','余额（USD）'),('allowance_usd','授权额度（USD）'),
                ('capacity_usd','还能用来挂单（USD）'),('inventory_cost_usd','当前库存成本（USD）'),('checked_at','读取时间')]])
        elif key=='cancellations':
            big='卡单 '+_text(len(data.get('stuck',[])) if mapping(data.get('hour')).get('success') is not None else None)
            for window,label in [('hour','最近一小时'),('today','Tokyo 今天')]:
                row=mapping(data.get(window));details+='<h3>'+label+'</h3>'+_pairs([(text,row.get(field)) for field,text in
                    [('success','已确认撤完'),('rejected','被拒'),('unknown','结果未知'),('blocked','被拦下')]])
            details+=_pairs([('审计覆盖',data.get('coverage'))])
            details+=_table(data.get('items'), [('order_id','订单'),('outcome','结果'),('requested_at','请求时间')])
        else:
            big='今天 '+_text(mapping(data.get('today')).get('count'))+' 笔'
            for window,label in [('hour','最近一小时'),('today','Tokyo 今天')]:
                row=mapping(data.get(window));details+='<h3>'+label+'</h3>'+_pairs([('成交笔数',row.get('count')),('成交金额（USD）',row.get('amount_usd'))])
            details+=_pairs([('当前库存成本（USD）',data.get('inventory_cost_usd'))])
            details+=_table(data.get('items'), [('market','市场'),('pool','所属池'),('side','方向'),('price','价格'),('quantity','数量'),('time','成交时间')])
        cards.append('<details data-lp-monitor="'+key+'" class="monitor-card '+state+'"><summary><span>'+LABELS[key]+'</span><strong>'+
            big+'</strong><b>'+STATUS[state]+'</b><span>'+_text(data['reason'])+'</span><small>展开明细</small></summary>'+details+'</details>')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>东京做市现况 · 只读</title><style>
:root{color-scheme:light dark;--bg:#f6f5f2;--card:#fff;--ink:#1f2328;--line:#c5c9d0;--ok:#166534;--warn:#854d0e;--bad:#b91c1c;--unk:#475569}
@media(prefers-color-scheme:dark){:root{--bg:#16181b;--card:#20242a;--ink:#eee;--line:#667085;--ok:#86efac;--warn:#fde047;--bad:#fca5a5;--unk:#cbd5e1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:16px}.monitor-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;align-items:start}.monitor-card{min-width:0;padding:16px;background:var(--card);border:1px solid var(--line);border-top:5px solid var(--unk);border-radius:10px;overflow-wrap:anywhere}.normal{border-top-color:var(--ok)}.attention{border-top-color:var(--warn)}.abnormal{border-top-color:var(--bad)}summary{cursor:pointer;min-height:48px}summary:focus-visible{outline:3px solid #4c8eff;outline-offset:4px}summary>*{display:block}summary strong{font-size:20px}summary b{font-size:16px}h1{font-size:24px}h3{font-size:16px}dl{margin:8px 0}dt{font-weight:600}dd{margin:0 0 8px}.table-wrap{overflow:auto}table{border-collapse:collapse}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left}small{font-size:14px}@media(max-width:1000px){.monitor-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.monitor-cards{grid-template-columns:1fr}}
</style></head><body><main><h1>东京做市现况 · 只读</h1><p>今天按 Asia/Tokyo 零点统计。每分钟刷新。未知不代表零。</p><p>读取时间：'''+_text(value.get('as_of'))+'</p><section class="monitor-cards" aria-label="做市五项现况">'+''.join(cards)+'</section></main></body></html>'


def validate_bind(host):
    try:
        if not isinstance(host,str) or '%' in host: raise ValueError('literal_address_required')
        address=ipaddress.ip_address(host)
        tailnet=(address.version==4 and address in ipaddress.ip_network('100.64.0.0/10')
            or address.version==6 and address in ipaddress.ip_network('fd7a:115c:a1e0::/48'))
        if not address.is_loopback and not tailnet: raise ValueError('readonly_bind_not_allowed')
    except (ValueError,TypeError) as exc:
        raise ValueError('bind_requires_numeric_loopback_or_explicit_tailscale_ip') from exc
    return str(address)


def create_readonly_server(*, host='127.0.0.1', port=8780, upstream_url='http://127.0.0.1:8769'+SUMMARY_PATH):
    # Bind policy is deliberately independent of the trading service's loopback rule.
    address=ipaddress.ip_address(validate_bind(host))
    validate_upstream(upstream_url)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send_body(self,code,body,kind):
            self.send_response(code);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            try:
                authority=urlsplit('http://'+self.headers.get('Host',''))
                if ipaddress.ip_address(authority.hostname or '') != address or authority.port != self.server.server_port:
                    raise ValueError('host_mismatch')
            except ValueError:
                self.send_body(403,b'Host refused','text/plain; charset=utf-8');return
            if self.path not in ('/',SUMMARY_PATH):
                self.send_body(404,b'Not found','text/plain; charset=utf-8');return
            summary=fetch_summary(upstream_url)
            if self.path=='/': self.send_body(200,render_page(summary).encode(),'text/html; charset=utf-8')
            else:self.send_body(200,json.dumps(summary,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        def reject(self):self.send_body(405,b'Read only','text/plain; charset=utf-8')
        do_POST=do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_HEAD=reject
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        address_family=socket.AF_INET6 if address.version==6 else socket.AF_INET
    return Server((str(address),port),Handler)


def main(argv=None):
    parser=argparse.ArgumentParser(description='东京 LP 只读现况（需单独部署批准）')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--upstream-url',default='http://127.0.0.1:8769'+SUMMARY_PATH)
    args=parser.parse_args(argv)
    server=create_readonly_server(host=args.host,port=args.port,upstream_url=args.upstream_url)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
