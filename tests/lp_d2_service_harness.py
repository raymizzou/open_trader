"""Real offline child-service harness; only OS/SDK/SQLite/time boundaries vary."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import fcntl
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time


class ServiceChild:
    def __init__(self,root,*,scenario):
        self.root=Path(root);self.scenario=scenario;self.process=None;self.http_thread=None
        self.release_root=self.root/'release';self.runtime_root=self.root/'runtime'
        self.data_dir=self.runtime_root/'data';self.events=self.runtime_root/'events'
        self.config_path=(self.runtime_root/'config/prediction_arbitrage.json' if 'launchd-nested' in scenario
            else self.root/'selected-private.json' if 'launchd-sibling' in scenario else self.runtime_root/'prediction.json')
        self.events.mkdir(parents=True);self.data_dir.mkdir()
        self.clock_path=self.events/'clock';self.clock_path.write_text('0')
        self.permit_path=self.runtime_root/'lp-start-guard'/'permit.json'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.base=f'http://127.0.0.1:{self.port}'
        self.output=self.root/'child.log';self.http_result=[];self.http_errors=[];self.manual_thread=None;self.manual_result=[];self.manual_errors=[];self.http_handles={}

    def marker(self,name):return self.events/name
    def release(self,name):self.marker('release-'+name).touch()
    def advance(self,value):
        temporary=self.clock_path.with_suffix('.new');temporary.write_text(str(value));temporary.replace(self.clock_path)
    def wait(self,name,timeout=15):
        deadline=time.monotonic()+timeout
        while not self.marker(name).exists():
            if self.process and self.process.poll() is not None:
                raise AssertionError(f'child exited before {name}: {self.output.read_text()}')
            if time.monotonic()>=deadline:
                import signal
                self.process.send_signal(signal.SIGUSR1)
                threading.Event().wait(.1)
                diagnostics={}
                for path in ('/api/prediction-arbitrage/lp/auto/state','/api/prediction-arbitrage/lp/dashboard'):
                    try:diagnostics[path]=self.request(path)
                    except Exception as exc:diagnostics[path]=str(exc)
                raise AssertionError(f'waiting {name}: {self.output.read_text()}\n{json.dumps(diagnostics,default=str)}')
            threading.Event().wait(.01)

    def start(self,*,expected_running=True):
        source=Path(__file__).parents[1]
        for name in ('src','ops','scripts'):
            shutil.copytree(source/name,self.release_root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        tests=self.release_root/'tests';tests.mkdir()
        (tests/'__init__.py').write_text('')
        for name in ('lp_d2_service_harness.py','test_lp_order_registration_contract.py','test_deployment_preflight.py','test_lp_d2_service_lifecycle.py','test_lp_d2_startup_integration.py','test_lp_d2_service_cutover_repairs.py'):
            shutil.copyfile(source/'tests'/name,tests/name)
        for name in ('pyproject.toml','uv.lock','Dockerfile.dev'):
            shutil.copyfile(source/name,self.release_root/name)
        # Existing external GitHub artifact fixture supplies only the minimal
        # active/retired partition inputs. The application and new lifecycle
        # source above are the actual candidate bytes.
        from tests.test_deployment_preflight import evidence
        scratch=self.root/'ci-fixture';scratch.mkdir()
        evidence.__wrapped__(scratch)
        for path in (scratch/'tests').glob('*.py'):
            if not (tests/path.name).exists():shutil.copyfile(path,tests/path.name)
        # Isolate the fixed installed-unit path for every actual child and
        # manager subprocess. Fixed-port probes, when requested, use only the
        # actual fixture's ephemeral listener.
        (self.release_root/'src/sitecustomize.py').write_text("""
import os,socket
from pathlib import Path
from open_trader import prediction_cloud
prediction_cloud.UNIT_PATH=Path(__file__).parent/'fixture-absent-systemd.service'
port=os.environ.get('D2_FIXTURE_MANAGER_PORT')
if port:
    native=socket.socket.connect_ex
    def isolated_listener_probe(sock,address):
        if address==('127.0.0.1',8769):address=('127.0.0.1',int(port))
        return native(sock,address)
    socket.socket.connect_ex=isolated_listener_probe
""")
        # Explicitly authorized fixture Git only; never operates on the task branch.
        for args in (['init','-q'],['add','.'],['-c','user.name=D2 fixture','-c','user.email=d2@invalid','commit','-qm','offline candidate'],['checkout','--detach','-q']):
            subprocess.run(['git','-C',str(self.release_root),*args],check=True,capture_output=True)
        self.sha=subprocess.check_output(['git','-C',str(self.release_root),'rev-parse','HEAD'],text=True).strip()
        config=self.config_path;config.parent.mkdir(parents=True,exist_ok=True);config.write_text(json.dumps(dict(signer_address='0x'+'a'*40,wallet_address='0x'+'a'*40)));config.chmod(0o600)
        self.launch(self.scenario,expected_running=expected_running)

    def launch(self,scenario,*,expected_running=None):
        assert self.process is None or self.process.poll() is not None
        if hasattr(self,'log_handle'):self.log_handle.close()
        for name in ('service-starting','process-finalizing','metadata-admission-closed'):
            self.marker(name).unlink(missing_ok=True)
        self.output=self.root/('child-'+scenario+'.log')
        self.log_handle=self.output.open('w')
        env=dict(os.environ,PYTHONPATH=str(self.release_root/'src')+os.pathsep+str(self.release_root),PYTHONDONTWRITEBYTECODE='1',OPEN_TRADER_NLEG_PAUSED='1',OPEN_TRADER_DISABLE_NLEG_BACKGROUND='1')
        self.process=subprocess.Popen([sys.executable,'-m','tests.lp_d2_service_harness','child',str(self.runtime_root),str(self.port),scenario,str(self.config_path)],cwd=getattr(self,'caller_cwd',self.release_root),env=env,stdout=self.log_handle,stderr=subprocess.STDOUT)
        self.wait('service-starting')
        if expected_running is not None:
            self.wait_state(lambda s:s.get('effective_running') is expected_running)

    def request(self,path,payload=None):
        from urllib.request import Request,build_opener,ProxyHandler,HTTPCookieProcessor
        from http.cookiejar import CookieJar
        from open_trader.lp_auto_cli import _Client
        if payload is not None:
            client=_Client(self.base,4)
            return client.write(path,payload)
        with build_opener(ProxyHandler({})).open(self.base+path,timeout=3) as response:return json.load(response)
    def state(self):return self.request('/api/prediction-arbitrage/lp/auto/state')
    def pause(self):return self.request('pause',{'confirm':True})
    def wait_state(self,predicate,timeout=15):
        deadline=time.monotonic()+timeout;last=None
        while time.monotonic()<deadline:
            if self.process.poll() is not None:raise AssertionError(self.output.read_text())
            try:
                last=self.state()
                if predicate(last):return last
            except (OSError,ValueError):pass
            threading.Event().wait(.01)
        raise AssertionError(f'public state not reached: {last}\n{self.output.read_text()}')
    def begin_settings_save(self):
        result=self.request('config',{'api_retry_interval_seconds':61,'expected_config_version':1})
        assert result['settings_pending'] is True
        self.wait('settings-entered')
    def begin_manual_read(self):
        from open_trader.lp_auto_cli import _Client
        client=_Client(self.base,40)
        bootstrap=client.request('/api/prediction-arbitrage/venues')
        def request():
            try:self.manual_result.append(client.request('/api/prediction-arbitrage/lp/sessions/absent-original-group/stop',{},csrf=bootstrap['csrf_token']))
            except BaseException as exc:self.manual_errors.append(exc)
        self.manual_thread=threading.Thread(target=request);self.manual_thread.start()
        self.wait('manual-entered')
    def wait_manual(self):
        self.manual_thread.join(5)
        assert not self.manual_thread.is_alive() and not self.manual_errors,self.manual_errors
        assert len(self.manual_result)==1 and self.manual_result[0]['state']=='none',self.manual_result
        assert self.manual_result[0].get('session_id') is None,self.manual_result
    def begin_held_http(self,name="http"):
        # Hold an actual accepted HTTP body read. This endpoint is deliberately
        # independent of display-cache refresh and the SDK read worker names.
        from open_trader.lp_auto_cli import _Client
        client=_Client(self.base,4)
        bootstrap=client.request('/api/prediction-arbitrage/venues')
        payload=b'{"confirm":true}'
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=40)
        connection.putrequest('POST','/api/prediction-arbitrage/lp/auto/pause')
        connection.putheader('Content-Type','application/json')
        connection.putheader('Content-Length',str(len(payload)))
        connection.putheader('Origin',self.base)
        connection.putheader('X-CSRF-Token',bootstrap['csrf_token'])
        connection.putheader('Cookie','; '.join(cookie.name+'='+cookie.value for cookie in client.cookies))
        connection.endheaders();connection.send(payload[:1])
        results,errors=[],[]
        if name=='http':self.http_result,self.http_errors=results,errors
        def finish_body():
            try:
                deadline=time.monotonic()+40
                while not self.marker('release-'+name).exists():
                    assert time.monotonic()<deadline,'held HTTP body real watchdog'
                    threading.Event().wait(.01)
                connection.send(payload[1:])
                response=connection.getresponse()
                results.append((response.status,response.read()))
            except BaseException as exc:errors.append(exc)
            finally:connection.close();self.marker(name+'-ended').touch()
        thread=threading.Thread(target=finish_body)
        self.http_handles[name]=(thread,results,errors)
        if name=='http':self.http_thread=thread
        thread.start()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            # The health request is itself one active handler; the unfinished
            # body is the other real handler, before any business mutation.
            if self.request('/healthz')['http_load']['active']>=1+len(self.http_handles)+int(self.manual_thread is not None):
                self.marker(name+'-entered').touch();return
            threading.Event().wait(.01)
        raise AssertionError('unfinished HTTP body was not admitted')
    def begin_held_read_http(self,name='http'):
        # An actual accepted incomplete GET header holds the handler without
        # writing control or changing durable RUN during the fault regression.
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=40)
        connection.connect()
        connection.sock.sendall(b'GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nX-Held: ')
        results,errors=[],[]
        def finish_header():
            try:
                deadline=time.monotonic()+40
                while not self.marker('release-'+name).exists():
                    assert time.monotonic()<deadline,'held read-only HTTP real watchdog'
                    threading.Event().wait(.01)
                connection.sock.sendall(b'offline\r\n\r\n')
                response=http.client.HTTPResponse(connection.sock);response.begin()
                results.append((response.status,response.read()))
            except BaseException as exc:errors.append(exc)
            finally:connection.close();self.marker(name+'-ended').touch()
        thread=threading.Thread(target=finish_header)
        self.http_handles[name]=(thread,results,errors)
        thread.start()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.request('/healthz')['http_load']['active']>=1+len(self.http_handles):
                self.marker(name+'-entered').touch();return
            threading.Event().wait(.01)
        raise AssertionError('unfinished read-only HTTP header was not admitted')

    def wait_http(self,name='http'):
        thread,results,errors=self.http_handles[name]
        thread.join(5);assert not thread.is_alive()
        assert not errors,errors
        return results[0]
    def assert_owned(self):
        assert self.process.poll() is None,self.output.read_text()
        path=self.data_dir/'prediction_arbitrage/runtime.lock'
        with path.open('a+') as handle:
            try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:pass
            else:raise AssertionError('real child owner released before native drain')
    def assert_owner_available(self):
        with (self.data_dir/'prediction_arbitrage/runtime.lock').open('a+') as handle:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    def wait_exit(self):return self.process.wait(timeout=15)
    def actual_posts(self):return len(list(self.events.glob('post-call-*')))
    def __enter__(self):return self
    def __exit__(self,*args):
        for resource in ('post','reader','reader-shutdown','notready','business','metadata','http','control','settings','manual','optional'):self.release(resource)
        for name in self.http_handles:self.release(name)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=5)
                raise AssertionError('offline child failed real watchdog cleanup; no production process was targeted')
        for thread,_,_ in self.http_handles.values():thread.join(5)
        if self.manual_thread:self.manual_thread.join(5)
        if hasattr(self,'log_handle'):self.log_handle.close()
        if args[0] is not None and self.output.exists():print(self.output.read_text())


def child(runtime_root,port,scenario,config_path):
    """Install external boundaries, arm explicitly, then run the real service."""
    import sqlite3,faulthandler,signal
    faulthandler.register(signal.SIGUSR1,all_threads=True)
    import pytest
    from polymarket.models.clob.order_response import AcceptedOrder
    from polymarket.models.clob.rewards import CurrentReward,CurrentRewardConfig
    from polymarket.models.clob.order_book import OrderBookLevel
    from open_trader import prediction_start_guard,polymarket_trading,polymarket_lp_market_reader
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_service import serve_prediction_service
    from tests.test_lp_order_registration_contract import _adapter,_SDKPublicClient,WALLET,CONDITION_ID,TOKEN_ID,NO_TOKEN_ID
    release_root=Path(__file__).resolve().parents[1]
    root=Path(runtime_root);config_path=Path(config_path);events=root/'events';now=datetime(2026,10,16,12,tzinfo=UTC)
    def mark(name):(events/name).touch()
    control_native=[];owner_clock_observations=[0]
    def business_clock():
        # The injected clock observes real native-thread completion. Two
        # subsequent owner clock boundaries let the real owner loop collect
        # the result before the second already-accepted public observation.
        if (scenario=='late_control' and control_native
                and not control_native[0].is_alive()
                and threading.current_thread().name=='MainThread'):
            owner_clock_observations[0]+=1
            if owner_clock_observations[0]>=2:mark('late-control-observation-ready')
        return float((events/'clock').read_text())
    def utc_clock():return now+timedelta(seconds=business_clock())
    def hold(name):
        mark(name+'-entered');deadline=time.monotonic()+40
        while not (events/('release-'+name)).exists():
            assert time.monotonic()<deadline,'independent real native watchdog: '+name
            threading.Event().wait(.01)
        mark(name+'-ended')
    if scenario=='rollback-current-repair-lock-order':
        from open_trader.polymarket_lp_current_state import CurrentLPState
        from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
        from open_trader.prediction_arbitrage_execution import PredictionExecutionService
        snapshot_code=CurrentLPState.snapshot.__code__
        reservations_code=PredictionExecutionService.lp_auto_reservations.__code__
        configure_code=LPD2LiveExecution.configure.__code__
        reservations_admitted=threading.Event()
        configure_admitted=threading.Event()
        admitted={}
        reservations_observed=threading.Event()
        configure_observed=threading.Event()
        def wait_phase(phase):
            deadline=time.monotonic()+10
            while not phase.is_set():
                assert time.monotonic()<deadline,'real lock scheduling watchdog'
                threading.Event().wait(.001)
        def record_admission(frame,name,live):
            caller=frame.f_back
            while caller is not None and caller.f_code.co_name not in ('do_GET','do_POST'):
                caller=caller.f_back
            assert caller is not None
            handler=caller.f_locals['self']
            expected=('/api/prediction-arbitrage/lp/auto/reservations' if name=='reservations'
                else '/api/prediction-arbitrage/lp/auto/config')
            assert handler.path==expected
            assert not live.state._lock._is_owned()
            assert not live._settings_lock._is_owned()
            proof=dict(method=frame.f_code.co_name,self_id=id(frame.f_locals['self']),
                live_id=id(live),request_path=handler.path,handler_id=id(handler),
                thread_id=threading.get_ident(),entered_at=time.monotonic())
            (events/('repair-'+name+'-admission.json')).write_text(json.dumps(proof))
            mark('repair-'+name+'-admitted')
        observation_done=threading.Event()
        def finish_observation():
            if reservations_observed.is_set() and configure_observed.is_set():
                observation_done.set()
                threading.settrace(None)
        def configure_lines(frame,event,arg):
            if observation_done.is_set():
                frame.f_trace=None
                return None
            if event=='line' and frame.f_locals['self']._settings_lock._is_owned():
                mark('repair-configure-settings-held')
                configure_observed.set()
                finish_observation()
                frame.f_trace=None
                return None
            return configure_lines
        def observe_lock_order(frame,event,arg):
            # Only actual target/caller frames can perform an arm-file read.
            # One-shot memory phases end all line/FS tracing after observation.
            if observation_done.is_set() or event!='call':return None
            if (frame.f_code is snapshot_code and frame.f_back.f_code is reservations_code
                    and not reservations_observed.is_set()):
                if not (events/'repair-lock-arm').exists():return None
                assert frame.f_locals['self']._lock._is_owned()
                mark('repair-reservations-state-held')
                reservations_observed.set()
                finish_observation()
                assert frame.f_locals['self'] is admitted['live'].state
                wait_phase(configure_observed)
                return None
            if frame.f_code is reservations_code and not reservations_admitted.is_set():
                if not (events/'repair-lock-arm').exists():return None
                execution=frame.f_locals['self'];live=execution._lp_d2
                record_admission(frame,'reservations',live)
                admitted.update(execution=execution,live=live)
                reservations_admitted.set()
                wait_phase(configure_admitted)
                return None
            if frame.f_code is configure_code and not configure_observed.is_set():
                if not (events/'repair-lock-arm').exists():return None
                live=frame.f_locals['self']
                assert reservations_admitted.is_set() and live is admitted['live']
                assert frame.f_back.f_locals['self'] is admitted['execution']
                record_admission(frame,'configure',live)
                configure_admitted.set()
                wait_phase(reservations_observed)
                return configure_lines
            return None
        threading.settrace(observe_lock_order)
    optional_lock=threading.Lock()
    optional_counts=dict(calls=0,active=0,max_active=0)
    def hold_optional():
        with optional_lock:
            optional_counts['calls']+=1;optional_counts['active']+=1
            optional_counts['max_active']=max(optional_counts['max_active'],optional_counts['active'])
            (events/'optional-native-counts.json').write_text(json.dumps(optional_counts))
        try:hold('optional')
        finally:
            with optional_lock:
                optional_counts['active']-=1
                (events/'optional-native-counts.json').write_text(json.dumps(optional_counts))
    class ClockMeta(type):
        def __instancecheck__(cls,value):return isinstance(value,datetime)
    class BusinessDateTime(datetime,metaclass=ClockMeta):
        @classmethod
        def now(cls,tz=None):return utc_clock() if tz is not None else utc_clock().replace(tzinfo=None)
    class Public(_SDKPublicClient):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.fixture_identity=str(id(self))
        def close(self):
            (events/('public-close-'+self.fixture_identity)).touch()
            owned=events/'public-client-created'
            if owned.exists() and owned.read_text()==self.fixture_identity:
                (events/'public-client-closed').write_text(self.fixture_identity)
        def list_market_rewards(self,*,condition_id,sponsored):
            return (CurrentReward(condition_id=CONDITION_ID,rewards_max_spread=10,rewards_min_size=Decimal('1'),
                rewards_config=(CurrentRewardConfig(id=1,asset_address='0x2791bca1f2de4661ed88a30c99a7a9449aa84174',start_date=now-timedelta(days=1),end_date=now+timedelta(days=1),rate_per_day=Decimal('24')),)),)
        def list_current_rewards(self,*,sponsored):
            if scenario=='consumer-ci-http-empty':return ()
            if 'optional-hourly' in scenario and threading.current_thread().name=='prediction-lp-history-monitor' and (events/'hourly-first-source-complete').exists() and not sponsored:
                hold_optional()
            if 'producer-prep-absent' in scenario:
                mark('optional-ended')
                return ()
            if 'producer-prep-failure' in scenario and threading.current_thread().name=='prediction-lp-history-monitor':
                try:raise TimeoutError('offline current catalog unavailable')
                finally:mark('optional-ended')
            if 'optional-catalog' in scenario and threading.current_thread().name=='prediction-lp-history-monitor' and not sponsored:
                hold_optional()
            return self.list_market_rewards(condition_id=CONDITION_ID,sponsored=sponsored) if not sponsored else ()
        def list_markets(self,**kwargs):
            if scenario.startswith('consumer-ci-metadata-') or scenario=='consumer-ci-prune-stop':
                from polymarket.models.gamma.market import Market
                return tuple(Market.parse_response(dict(id=cid[-4:],conditionId=cid,
                    slug='metadata-349',question='Metadata snapshot',outcomes='["Yes", "No"]',
                    clobTokenIds='["349001", "349002"]',events=[])) for cid in kwargs.get('condition_ids',()))
            if 'optional-metadata' in scenario:hold_optional()
            if 'producer-prep-metadata' in scenario:
                try:raise TimeoutError('offline bounded metadata unavailable')
                finally:mark('optional-ended')
            return super().list_markets(**kwargs)
        def get_order_books(self,*,token_ids):
            candidate=('producer-candidate' in scenario and threading.current_thread().name=='prediction-lp-candidate-scan-monitor')
            if candidate and not (events/'release-optional').exists():hold_optional()
            trial=('producer-trial' in scenario and threading.current_thread().name=='prediction-lp-candidate-maintenance-monitor')
            if trial:hold_optional()
            result=super().get_order_books(token_ids=token_ids)
            if trial:
                result=tuple(book for book in result if book.token_id!=NO_TOKEN_ID)
                mark('producer-trial-missing-no')
            if candidate:mark('producer-candidate-source-'+str(len(list(events.glob('producer-candidate-source-*')))+1))
            return result
        def get_order_book(self,*,token_id):
            if token_id==NO_TOKEN_ID and scenario=='reader' and threading.current_thread().name=='lp-reader-source':hold('reader')
            book=super().get_order_book(token_id=token_id)
            return book.model_copy(update={'timestamp':utc_clock(),'bids':(OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')),OrderBookLevel(price=Decimal('.38'),size=Decimal('1000'))),'asks':(OrderBookLevel(price=Decimal('.41'),size=Decimal('1000')),)})
    if scenario.startswith(('consumer-ci-current-','automatic-consumer-ci-current-')):
        # Immutable external SDK fixture data for the current counterpart of
        # the restart cases. Fresh source reads are made by the real Reader.
        ci_payload=json.loads((events/'ci-current-input.json').read_text())
        market_model=type(_SDKPublicClient(now).get_market(id='market-1'))
        ci_markets=tuple(market_model.model_validate(row) for row in ci_payload['markets'])
        def ci_get_market(public,*,id):
            return next(row for row in ci_markets if str(row.id)==str(id) or row.condition_id==id)
        def ci_get_book(public,*,token_id):
            from polymarket.models.clob.order_book import OrderBook
            market=next(row for row in ci_markets if token_id in (row.outcomes.yes.token_id,row.outcomes.no.token_id))
            return OrderBook(market=market.condition_id,asset_id=token_id,timestamp=utc_clock(),
                bids=(OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')),),
                asks=(OrderBookLevel(price=Decimal('.41'),size=Decimal('1000')),),
                min_order_size=Decimal('1'),tick_size=Decimal('.01'),neg_risk=False,hash='actual-current-fixture')
        Public.get_market=ci_get_market;Public.get_order_book=ci_get_book
        Public.list_markets=lambda public,**kw:ci_markets
        def ci_market_rewards(public,*,condition_id,sponsored):
            # Selected sponsored=True is the provider's COMBINED total, so
            # its native-only market still repeats the exact native config.
            assert condition_id in {row.condition_id for row in ci_markets}
            return (CurrentReward(condition_id=condition_id,rewards_max_spread=10,rewards_min_size=Decimal('20'),
                rewards_config=(CurrentRewardConfig(id=next(i for i,row in enumerate(ci_markets,1) if row.condition_id==condition_id),
                    asset_address='0x2791bca1f2de4661ed88a30c99a7a9449aa84174',start_date=now-timedelta(days=1),
                    end_date=now+timedelta(days=1),rate_per_day=Decimal('24')),)),)
        Public.list_market_rewards=ci_market_rewards
        Public.list_current_rewards=lambda public,*,sponsored:() if sponsored else tuple(reward for market in ci_markets
            for reward in ci_market_rewards(public,condition_id=market.condition_id,sponsored=sponsored))
    import io
    from open_trader.polymarket_trading import PolymarketTradingClient,TradingConfig
    from tests.test_lp_order_registration_contract import _SDKAccountClient
    def public_http(request,**kw):
        if request.full_url.startswith(polymarket_trading.LP_COMPETITIVENESS_ENDPOINT+'?'):
            payload={'data':[{'condition_id':CONDITION_ID,'market_competitiveness':10}],'next_cursor':'LTE='}
        elif request.full_url==polymarket_trading.LP_PRICE_HISTORY_ENDPOINT and request.data:
            if 'optional-history' in scenario:hold_optional()
            body=json.loads(request.data)
            payload={'history':{token:[{'t':t,'p':.4} for t in range(body['start_ts'],body['end_ts']+1,60)] for token in body['markets']}}
            if 'producer-prep-partial' in scenario:
                payload['history'].pop(NO_TOKEN_ID,None);mark('optional-ended')
        else:raise AssertionError('uncontrolled external HTTP boundary: '+request.full_url)
        return io.BytesIO(json.dumps(payload).encode())
    sdk=_SDKAccountClient(now)
    if scenario.endswith('consumer-nleg-paused-run'):
        sdk.orders=();sdk.positions=()
    if 'rollback-current' in scenario:
        from tests.test_lp_order_registration_contract import _open_order
        sdk.orders=(_open_order('api-existing-buy','BUY',price='.40',original='10',matched='2').model_copy(update={'created_at':now}),)
        sdk.positions=(dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,outcome='YES',
            size=Decimal('3'),average_price=Decimal('.20')),)
        if scenario.endswith('incomplete'):sdk.positions=None
    if 'manual-preserved' in scenario:
        from tests.test_lp_order_registration_contract import _open_order
        sdk.orders=(_open_order('manual-no-order','BUY',price='.36',original='100',token_id=NO_TOKEN_ID,outcome='NO'),
            _open_order('manual-yes-order','BUY',price='.27',original='100'))
        sdk.positions=()
    if 'producer-candidate' in scenario or 'producer-trial' in scenario:
        # Candidate exploration excludes already-participating markets.
        # This current account is truthfully empty; persisted LP PAUSE prevents buys.
        sdk.orders=();sdk.positions=()
    if scenario=='consumer-ci-independent-cancel':
        from tests.test_lp_order_registration_contract import _open_order
        model=type(_open_order('fixture-type','BUY',price='.40',original='20'))
        sdk.orders=tuple(model.model_validate(row) for row in json.loads((events/'ci-current-orders.json').read_text()))
        sdk.positions=()
    if 'ci-http-empty' in scenario:sdk.orders=();sdk.positions=()
    if scenario=='consumer-ci-http-legacy-manual':
        from tests.test_lp_order_registration_contract import _open_order
        sdk.orders=(_open_order('manual-order','BUY',price='.36',original='100',token_id=NO_TOKEN_ID,outcome='NO'),)
        sdk.positions=()
    if scenario.startswith(('consumer-ci-current-','automatic-consumer-ci-current-')):
        from tests.test_lp_order_registration_contract import _open_order
        from polymarket.models.data.portfolio import Position
        order_model=type(_open_order('fixture-type','BUY',price='.40',original='20'))
        sdk.orders=tuple(order_model.model_validate(row) for row in ci_payload['orders'])
        sdk.positions=tuple(Position.model_validate(row) for row in ci_payload['positions'])
        if ci_payload['mode']=='incomplete-positions':sdk.positions=None
        if ci_payload['mode']=='incomplete-orders':sdk.list_open_orders=lambda **kw:None
        if ci_payload['mode']=='foreign':
            sdk.orders=tuple(row.model_copy(update={'maker_address':'0x'+'b'*40}) for row in sdk.orders)
    native_cancel=sdk.cancel_orders
    def cancel_orders(**kwargs):
        mark('cancel-call-'+str(len(list(events.glob('cancel-call-*')))+1))
        if scenario=='consumer-ci-independent-cancel':
            (events/'ci-exact-cancel-ids.json').write_text(json.dumps(list(kwargs['order_ids'])))
            mark('ci-cancel-entered');deadline=time.monotonic()+5
            try:
                while not (events/'release-ci-cancel').exists():
                    assert time.monotonic()<deadline,'Independent runtime-owned cancel watchdog'
                    threading.Event().wait(.01)
                return native_cancel(**kwargs)
            finally:mark('ci-cancel-ended')
        return native_cancel(**kwargs)
    sdk.cancel_orders=cancel_orders
    read_balance=sdk.get_balance_allowance
    if scenario.endswith('incomplete'):
        sdk.list_positions=lambda **kw:None
    balance_calls=[]
    class NewProviderLimit(Exception):
        @property
        def retry_after_seconds(self):
            mark('new-provider-limit-observed')
            return 120
    def balance(**kw):
        if scenario=='resume-reader' and threading.current_thread().name=='lp-reader-source':
            balance_calls.append(business_clock())
            (events/('native-balance-call-'+str(len(balance_calls)))).write_text(str(balance_calls[-1]))
            if len(balance_calls)==1:
                hold('reader')
                raise NewProviderLimit()
            if len(balance_calls)==2:hold('reader-shutdown')
        if scenario=='reader' and threading.current_thread().name=='lp-reader-source':hold('reader')
        return read_balance(**kw)
    sdk.get_balance_allowance=balance
    if scenario.startswith('consumer-ci-metadata-') or scenario=='consumer-ci-prune-stop':
        def unavailable_financial_facts(**kwargs):
            raise OSError('offline account financial facts unavailable')
        sdk.get_balance_allowance=unavailable_financial_facts
    native_orders=sdk.list_open_orders
    reader_order_calls=[0]
    def current_orders(**kw):
        if 'optional-share' in scenario and threading.current_thread().name=='prediction-lp-share-watch':hold_optional()
        if ('optional-' in scenario or 'producer-' in scenario) and threading.current_thread().name=='lp-reader-source':
            mark('optional-current-read-'+str(len(list(events.glob('optional-current-read-*')))+1))
        if scenario.endswith('consumer-nleg-paused-run') and threading.current_thread().name=='lp-reader-source':
            number=len(list(events.glob('nleg-current-read-*')))+1
            mark('nleg-current-read-'+str(number))
        if scenario=='resume-reader' and threading.current_thread().name=='lp-reader-source':
            reader_order_calls[0]+=1
            mark('native-reader-orders-'+str(reader_order_calls[0]))
            mark('native-reader-orders-at-'+str(business_clock()))
        return native_orders(**kw)
    sdk.list_open_orders=current_orders
    def public_factory():
        import inspect
        public=Public(now)
        # The real adapter also owns short-lived catalog/reward clients.
        # Record the distinct factory lease retained for snapshot reads.
        if inspect.currentframe().f_back.f_code.co_name=='_lp_snapshot_public_client':
            (events/'public-client-created').write_text(public.fixture_identity)
        return public
    adapter=PolymarketTradingClient(TradingConfig(WALLET,WALLET),sdk,urlopen_fn=public_http,public_client_factory=public_factory)
    if scenario=='consumer-ci-independent-cancel':
        native_ci_close=adapter.close
        def close_ci_adapter():
            if (events/'ci-cancel-entered').exists():assert (events/'ci-cancel-ended').exists()
            result=native_ci_close();mark('ci-client-closed');return result
        adapter.close=close_ci_adapter
    def sign_limit(**kw):
        mark('sdk-sign-entered')
        return dict(kw,order_type='GTC' if kw.get('expiration') is None else 'GTD')
    sdk.create_limit_order=sign_limit
    def post(order):
        number=len(list(events.glob('post-call-*')))+1;mark('post-call-'+str(number));hold('post')
        return AcceptedOrder(order_id='child-order-'+str(number),status='live',making_amount=Decimal('0'),taking_amount=Decimal('0'),trade_ids=(),transactions_hashes=())
    sdk.post_order=post
    if scenario.startswith('consumer-ci-native-'):
        # New scenario-only external SDK timing. Normal ServiceChild paths
        # above retain their original native behavior.
        native_phase='signing' if '-signing' in scenario else 'post'
        def ci_native_sign(**kw):
            mark('ci-sign-call-'+str(len(list(events.glob('ci-sign-call-*')))+1))
            (events/'ci-last-sign.json').write_text(json.dumps(kw,default=str))
            caller=sys._getframe(1)
            while caller and caller.f_code.co_name!='_buy_admitted':caller=caller.f_back
            assert caller is not None
            actual=caller.f_locals;record=actual['record'];action=actual['action'];token=actual['token']
            (events/'ci-native-identity.json').write_text(json.dumps(dict(request_id=action.request_id,
                cycle_generation=token.cycle_generation,batch_id=token.batch_id,round_number=token.round_number,
                price=str(record.price),quantity=str(record.quantity),expiration=record.expiration,
                condition_id=record.condition_id,token_id=record.token_id,requested_at=str(record.requested_at))))
            if native_phase=='signing' and (events/'ci-arm').exists():
                try:hold('ci-native')
                finally:mark('ci-sdk-ended')
                if not any(tag in scenario for tag in ('-config-','-auto-','-stale-','-generation-')):
                    raise TimeoutError('offline current signing completed without POST')
            return dict(kw,order_type='GTC' if kw.get('expiration') is None else 'GTD')
        def ci_native_post(order):
            number=len(list(events.glob('post-call-*')))+1;mark('post-call-'+str(number))
            if native_phase=='post' and (events/'ci-arm').exists():
                try:hold('ci-native')
                finally:mark('ci-sdk-ended')
                raise TimeoutError('offline current entered POST receipt unknown')
            # An actual API order after this successful external POST is
            # independent complete venue evidence, including explicit matched0.
            from tests.test_lp_order_registration_contract import _open_order
            oid='child-order-'+str(number)
            row=_open_order(oid,'BUY',price=str(order['price']),original=str(order['size']),
                token_id=order['token_id'],order_type=order['order_type'],expiration=order.get('expiration')).model_copy(update={'created_at':utc_clock()})
            sdk.orders=(*sdk.orders,row)
            return AcceptedOrder(order_id=oid,status='live',making_amount=Decimal('0'),taking_amount=Decimal('0'),trade_ids=(),transactions_hashes=())
        sdk.create_limit_order=ci_native_sign;sdk.post_order=ci_native_post
        native_ci_adapter_close=adapter.close
        def ci_native_close():
            if (events/'ci-native-entered').exists():assert (events/'ci-sdk-ended').exists()
            result=native_ci_adapter_close();mark('ci-client-closed');return result
        adapter.close=ci_native_close
    import atexit
    atexit.register(lambda:mark('process-finalizing'))
    if not (root/'data/prediction_arbitrage/prediction_arbitrage.sqlite3').exists():
        store=PredictionArbitrageStore(root/'data')
        if scenario.startswith('consumer-ci-fence-'):
            expected_fence=1 if scenario=='consumer-ci-fence-1' else 2
            assert store.advance_minimum_reader_generation(expected_fence)==expected_fence
        import hashlib
        account=hashlib.sha256(WALLET.encode()).hexdigest()
        store.lp_save_current_control(dict(account_id=account,process_epoch='seed',request_id='running',request_version=1,desired_running=(scenario.startswith('consumer-ci-native-auto-') or scenario.endswith('consumer-nleg-paused-run') or not (scenario.startswith('rollback-current') or scenario.startswith('consumer-'))),requested_at=now,actor=None))
        if not any(name in scenario for name in ('startup-unconfigured','manual-preserved','nleg-paused-run')):
            store.lp_save_current_settings(account,expected_config_version=0,changes=dict(budget_usd='40',target_buy_count=1),actor='offline operator')
        if scenario.endswith('consumer-nleg-paused-run'):
            store.n_leg_safety_config_write(1,dict(max_per_trade_cost_units=10_000_000,
                max_total_unsettled_capital_units=20_000_000,max_partial_fill_loss_units=10_000_000,
                max_auto_repair_loss_units=10_000_000))
            store.n_leg_mode_control_write(mode='MANUAL',contract_generation=7,
                qualification_policy_version=3,safety_config_version=1,enabled_execution_scope_version=[])
            store.n_leg_create_batch(dict(execution_batch_id='nleg-accounting-batch',
                opportunity_episode_id='nleg-accounting-episode',episode_lineage_id='nleg-accounting-lineage',
                mode='MANUAL',state='INCIDENT',entry_fingerprint='nleg-accounting-entry',
                total_unsettled_capital_units=1_000_000,reservation_units=1_000_000,
                reservations=[dict(remaining_units=0,holding_units=1_000_000)],legs=[dict(receipt=dict(state='REJECTED'))]))
            store.n_leg_acknowledge_incident('nleg-accounting-batch',
                acknowledgement=dict(actor='test',reconciliation='fresh_clean'))
        if scenario=='consumer-incompatible':store.advance_minimum_reader_generation(2)
        if scenario=='consumer-reconcile-locked' or scenario.startswith('consumer-ci-metadata-') or scenario=='consumer-ci-prune-stop':
            recovery=store.create_recovery_execution({'reason':'offline unresolved non-LP incident'},idempotency_key='actual-incident-seed')
            store.open_incident(recovery['execution_id'],{'reason':'offline unresolved non-LP incident'})
        if 'stale-report' in scenario:
            store.lp_create_session('old-report-only','old-report-key',state='entry_open',payload=dict(
                market_id='historical-market',condition_id=CONDITION_ID,token_id=TOKEN_ID,outcome='YES',
                price=Decimal('.50'),quantity=Decimal('20'),residual_quantity=Decimal('12'),
                realized_trade_pnl=Decimal('.382'),review_at='2026-09-15T00:00:00Z',
                entry_order_id='old-history-order',owned_order_ids=['old-history-order']))
        if 'optional-history' not in scenario and 'producer-prep-partial' not in scenario:
            store.lp_save_price_history(CONDITION_ID,TOKEN_ID,[],dict(state='known',amplitude=Decimal('.005'),checked_at=now,valid_until=now+timedelta(days=1)))
            store.lp_save_price_history(CONDITION_ID,NO_TOKEN_ID,[],dict(state='known',amplitude=Decimal('.005'),checked_at=now,valid_until=now+timedelta(days=1)))
    real_connect=sqlite3.connect;failures=[0]
    import itertools
    sql_serial=itertools.count(1)
    sql_opened=[];sql_closed=[];sql_active=set();sql_details={};consumer_observations={}
    store_module=sys.modules[PredictionArbitrageStore.__module__]
    store_factory_code=PredictionArbitrageStore._connection.__code__
    reader_fence_code=store_module.read_minimum_reader_generation.__code__
    def sql_ownership_snapshot():
        unclassified=[key for key,detail in sql_details.items() if detail['ownership']=='unclassified-store-origin']
        assert not unclassified,[(key,sql_details[key]) for key in unclassified]
        owned={key for key,detail in sql_details.items() if detail['ownership'] in ('store-factory','reader-fence')}
        assert len(sql_opened)==len(set(sql_opened))
        assert set(sql_closed)<=set(sql_opened)
        return dict(opened=sorted(owned),closed=sorted(owned.intersection(sql_closed)),
            active=sorted(owned.intersection(sql_active)),
            all_origins=dict(opened=list(sql_opened),closed=list(sql_closed),
                explicit_close_unobserved=sorted(sql_active),details=dict(sql_details),
                limitation='Other persistent catalog and finalizer-owned scratch close outcomes remain unobserved/UNKNOWN; open handles do not prove unfinished native I/O.'))
    class NativeSQL(sqlite3.Connection):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.fixture_connection_id=next(sql_serial)
            sql_opened.append(self.fixture_connection_id)
            caller=sys._getframe(2)
            same_store_module=caller.f_globals is store_module.__dict__
            ownership=('store-factory' if same_store_module and caller.f_code is store_factory_code else
                'reader-fence' if same_store_module and caller.f_code is reader_fence_code else
                'unclassified-store-origin' if same_store_module else 'other')
            sql_details[self.fixture_connection_id]=dict(path=str(args[0]),caller=caller.f_code.co_qualname,
                module=caller.f_globals.get('__name__'),source_file=str(Path(caller.f_code.co_filename).resolve()),
                ownership=ownership)
            sql_active.add(self.fixture_connection_id)
        def close(self):
            result=super().close()
            if self.fixture_connection_id in sql_active:
                sql_active.remove(self.fixture_connection_id)
                sql_closed.append(self.fixture_connection_id)
            return result
        def execute(self,sql,*a,**kw):
            name=threading.current_thread().name
            if name=='MainThread' and sql=='PRAGMA journal_mode=WAL':mark('actual-store-sql-'+scenario)
            if (scenario=='consumer-reconcile-error' and name=='MainThread'
                    and sql.startswith('SELECT * FROM incidents WHERE acknowledged_at IS NULL')):
                mark('actual-reconcile-sql-failed')
                raise sqlite3.OperationalError('offline reconciliation storage unavailable')
            if name=='MainThread' and sql=='SELECT payload FROM lp_auto_pool WHERE singleton=1':
                mark('startup-control-settings-read-'+scenario)
                caller=sys._getframe(1).f_code.co_name
                if caller in ('lp_load_current_control','lp_load_current_settings'):
                    kind='control' if caller=='lp_load_current_control' else 'settings'
                    mark('startup-'+kind+'-read-'+scenario)
                    if scenario=='startup-'+kind+'-failure':
                        raise sqlite3.OperationalError('actual startup '+kind+' unavailable')
                if scenario.startswith('launchd-context'):
                    raise sqlite3.OperationalError('actual startup control unavailable')
            if name=='lp-control-save' and sql.startswith('INSERT INTO lp_auto_pool'):
                failures[0]+=1;mark('control-call-'+str(failures[0]))
                if scenario!='late_control' and not scenario.startswith('resume-') and not scenario.startswith('consumer-ci-native-'):
                    mark('control-failure-'+str(failures[0]))
                    raise sqlite3.OperationalError('actual control save failure')
            if (name=='MainThread' or ('rollback-current' in scenario and 'producer-' not in scenario)) and (
                    'lp_order_registrations' in sql or 'FROM lp_sessions' in sql
                    or 'INTO lp_sessions' in sql or 'UPDATE lp_sessions' in sql):
                mark('legacy-bookkeeping-unavailable');raise sqlite3.OperationalError('legacy LP bookkeeping unavailable')
            if name=='lp-business-writer' and sql=='BEGIN IMMEDIATE':
                # This optional audit write never acquires SQLite's write lock
                # or succeeds. Its actual external I/O lifetime still drains.
                if not (events/'business-entered').exists():hold('business')
                mark('business-record-unavailable')
                raise sqlite3.OperationalError('disposable business records unavailable')
            if name=='lp-d2-manual-command' and sql=='BEGIN' and scenario=='reader':
                hold('manual')
            result=super().execute(sql,*a,**kw)
            if name=='lp-settings-save' and sql=='COMMIT' and 'repair-lock-order' in scenario:
                mark('repair-settings-commit-'+str(len(list(events.glob('repair-settings-commit-*')))+1))
            if name=='lp-settings-save' and sql=='COMMIT' and scenario=='reader':hold('settings')
            if name=='lp-control-save' and sql=='COMMIT' and scenario=='late_control':
                control_native.append(threading.current_thread())
                hold('control')
            if sql=='COMMIT' and name=='lp-metadata-writer' and not (events/'metadata-entered').exists():hold('metadata')
            return result
    real_from_keychain=PolymarketTradingClient.from_keychain
    def runtime_sdk(cls,config):
        permit=json.loads((root/'lp-start-guard/permit.json').read_text())
        assert permit['state']=='CLAIMED' and permit['pid']==os.getpid(),permit
        with (root/'data/prediction_arbitrage/runtime.lock').open('a+') as owner:
            try:fcntl.flock(owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:pass
            else:raise AssertionError('SDK construction before actual runtime ownership')
        mark('runtime-sdk-factory-'+scenario)
        if scenario in ('startup-sdk-failure','consumer-core-init-failure','consumer-legacy-no-generation'):
            def external_keychain(command,**kw):
                values={'signing-private-key':'0x'+'1'*64,'builder-key':'00000000-0000-0000-0000-000000000001','builder-secret':'Zml4dHVyZQ==','builder-passphrase':'offline-fixture'}
                return subprocess.CompletedProcess(command,0,values[command[command.index('-a')+1]]+'\n','')
            def external_sdk_init(**kw):
                mark('external-sdk-init-failed')
                raise OSError('offline external SDK initialization failed')
            return real_from_keychain(config,client_factory=external_sdk_init,run=external_keychain,public_client_factory=public_factory)
        return adapter
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=NativeSQL))))
        if scenario=='rollback-current-repair-lock-order':
            native_thread_join=threading.Thread.join
            def observed_settings_join(thread,*args,**kwargs):
                result=native_thread_join(thread,*args,**kwargs)
                if thread.name=='lp-settings-save':
                    (events/'repair-settings-native-join.json').write_text(json.dumps(dict(
                        ident=thread.ident,alive_after_join=thread.is_alive(),
                        committed=len(list(events.glob('repair-settings-commit-*'))))))
                return result
            patch.setattr(threading.Thread,'join',observed_settings_join)
        if scenario.startswith('consumer-ci-native-'):
            from open_trader import prediction_runtime as ci_native_runtime
            from open_trader.prediction_service import _PredictionHTTPServer
            ci_http_init=_PredictionHTTPServer.__init__
            def ci_http_created(server,*args,**kwargs):
                result=ci_http_init(server,*args,**kwargs)
                (events/'ci-real-http-server.json').write_text(json.dumps(dict(daemon_threads=server.daemon_threads)))
                return result
            patch.setattr(_PredictionHTTPServer,'__init__',ci_http_created)
            original_start=ci_native_runtime.PredictionRuntime.start
            original_begin_stop=ci_native_runtime.PredictionRuntime.begin_stop
            original_ci_stop=ci_native_runtime.PredictionRuntime.stop
            ci_started_resources=[]
            def ci_started(runtime):
                result=original_start(runtime)
                ci_started_resources.append((runtime.store,runtime.lp))
                (events/'ci-started.json').write_text(json.dumps(dict(state=runtime.state,owner=runtime.production_owner)))
                return result
            def ci_begin_stop(runtime):
                result=original_begin_stop(runtime)
                mark('release-ci-scheduler')
                (events/'ci-normal-stop-context.json').write_text(json.dumps(dict(
                    fatal_id=runtime.lp_d2.state.snapshot().current.fatal_id,
                    control_fatal=runtime.lp_d2.control.status().fatal)))
                (events/'ci-stopping.json').write_text(json.dumps(dict(state=runtime.state,
                    owner=runtime.production_owner,store_retained=runtime.store is ci_started_resources[0][0],
                    lp_retained=runtime.lp is ci_started_resources[0][1],
                    client_retained=runtime._prediction_trading is adapter)))
                return result
            def ci_stopped(runtime):
                result=original_ci_stop(runtime)
                (events/'ci-stopped.json').write_text(json.dumps(dict(state=runtime.state,owner=runtime.production_owner)))
                return result
            patch.setattr(ci_native_runtime.PredictionRuntime,'stop',ci_stopped)
            patch.setattr(ci_native_runtime.PredictionRuntime,'start',ci_started)
            patch.setattr(ci_native_runtime.PredictionRuntime,'begin_stop',ci_begin_stop)
            from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
            ci_run_code=LPD2LiveExecution.run_once.__code__
            ci_buy_code=LPD2LiveExecution._buy_admitted.__code__
            def ci_completed_current(frame,event,arg):
                if event=='return' and frame.f_code is ci_buy_code and arg is not None:
                    (events/'ci-buy-completed.json').write_text(json.dumps(dict(request_id=arg.identity.request_id,
                        status=arg.status.value,ended=(events/'ci-sdk-ended').exists())))
                if (event=='return' and frame.f_code is ci_run_code
                        and threading.current_thread().name=='prediction-lp-auto'
                        and frame.f_locals['self'].state.snapshot().observation is not None
                        and ('-auto-' not in scenario or (events/'ci-native-entered').exists())
                        and not (events/'ci-current-completed').exists()):
                    # The actual run has released its native lock. Park only
                    # this scenario's next scheduler invocation, not its read.
                    mark('ci-current-completed');hold('ci-scheduler')
                return ci_completed_current
            threading.settrace(ci_completed_current)
            if '-captured-' in scenario:
                from open_trader.prediction_arbitrage_execution import PredictionExecutionService
                from open_trader.prediction_service import _PredictionHTTPServer
                ci_original_submit=PredictionExecutionService.lp_submit_entry
                ci_original_load=_PredictionHTTPServer.http_load_snapshot
                def ci_captured_submit(execution,payload):
                    if payload.get('idempotency_key')=='queued-http-entry':hold('ci-captured')
                    return ci_original_submit(execution,payload)
                def ci_actual_load(server):
                    result=ci_original_load(server)
                    if (events/'ci-stopping.json').exists():
                        temp=events/'ci-http-load.new';temp.write_text(json.dumps(result));temp.replace(events/'ci-http-load.json')
                    return result
                patch.setattr(PredictionExecutionService,'lp_submit_entry',ci_captured_submit)
                patch.setattr(_PredictionHTTPServer,'http_load_snapshot',ci_actual_load)
        if scenario=='consumer-ci-independent-cancel':
            from open_trader import prediction_runtime as ci_runtime
            native_ci_start=ci_runtime.PredictionRuntime.start
            ci_resources=[]
            def observe_ci_start(runtime):
                result=native_ci_start(runtime)
                ci_resources.append((runtime.store,runtime.lp))
                return result
            patch.setattr(ci_runtime.PredictionRuntime,'start',observe_ci_start)
            native_ci_begin_stop=ci_runtime.PredictionRuntime.begin_stop
            def observe_ci_stop(runtime):
                result=native_ci_begin_stop(runtime)
                if (events/'ci-cancel-entered').exists() and not (events/'ci-cancel-ended').exists():
                    (events/'ci-native-stopping.json').write_text(json.dumps(dict(state=runtime.state,
                        production_owner=runtime.production_owner,store_retained=runtime.store is ci_resources[0][0],
                        lp_retained=runtime.lp is ci_resources[0][1],client_retained=runtime._prediction_trading is adapter)))
                return result
            patch.setattr(ci_runtime.PredictionRuntime,'begin_stop',observe_ci_stop)
            from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
            current_run=LPD2LiveExecution.run_once.__code__
            def exact_current_boundary(frame,event,arg):
                if (event=='return' and frame.f_code is current_run
                        and threading.current_thread().name=='prediction-lp-auto'
                        and not (events/'ci-current-captured').exists()
                        and len(frame.f_locals['self'].state.snapshot().active_orders)==5):
                    mark('ci-current-captured');deadline=time.monotonic()+15
                    while not (events/'release-ci-scheduler').exists():
                        assert time.monotonic()<deadline,'actual completed current observation watchdog'
                        threading.Event().wait(.01)
                return exact_current_boundary
            threading.settrace(exact_current_boundary)
        if scenario.startswith(('consumer-ci-current-','automatic-consumer-ci-current-')):
            from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
            ci_snapshot_run=LPD2LiveExecution.run_once.__code__
            from open_trader.polymarket_lp import PolymarketLPService
            ci_history_code=PolymarketLPService.refresh_price_history.__code__
            ci_port_code=type(adapter.lp_reader_source_port()).read.__code__
            ci_manual_code=LPD2LiveExecution.manual_entry.__code__
            def ci_observed_frame(frame,event,arg):
                if event=='exception' and frame.f_code is ci_port_code:
                    with (events/'ci-current-source-errors.jsonl').open('a') as out:
                        out.write(json.dumps(dict(component=frame.f_locals.get('component'),error=repr(arg[1])))+'\n')
                if event=='return' and frame.f_code is ci_manual_code:
                    observed=frame.f_locals['self'].state.snapshot().observation
                    if observed is not None:
                        (events/'ci-manual-frame.json').write_text(json.dumps({name:dict(complete=value.complete,
                            error=value.error,value=str(value.value)) for name,value in observed.source_frame.sources.items()}))
                if event=='return' and frame.f_code is ci_history_code and isinstance(arg,dict) and arg.get('preparation_outcome')=='success':
                    (events/'ci-current-history-produced.json').write_text(json.dumps(arg,default=str))
                if (event=='return' and frame.f_code is ci_snapshot_run
                        and threading.current_thread().name=='prediction-lp-auto'
                        and not (events/'ci-current-observed.json').exists()):
                    snapshot=frame.f_locals['self'].state.snapshot()
                    observation=snapshot.observation
                    if observation is not None:
                        raw=observation.source_frame
                        (events/'ci-current-observed.json').write_text(json.dumps(dict(account=raw.account,
                            order_ids=[row['order_id'] for row in raw.orders],orders_complete=raw.orders_complete,
                            positions_complete=raw.positions_complete,sources={name:dict(complete=value.complete,
                                error=value.error,checked_at=str(value.checked_at)) for name,value in raw.sources.items()})))
                        hold('ci-current-scheduler')
                return ci_observed_frame
            threading.settrace(ci_observed_frame)
        if scenario.startswith('consumer-ci-fence-'):
            _install_ci_fence_observers(patch,events,scenario,consumer_observations)
        if scenario=='consumer-ci-prune-stop':
            from open_trader import prediction_runtime as runtime_module
            from tests.test_lp_metadata_cache_followup import _exercise_actual_runtime_prune_stop
            native_prune_start=runtime_module.PredictionRuntime.start
            def actual_prune_start(runtime):
                result=native_prune_start(runtime)
                _exercise_actual_runtime_prune_stop(runtime,adapter,patch,real_connect)
                (events/'ci-prune-observations.json').write_text(json.dumps(dict(
                    state=runtime.state,production_owner=runtime.production_owner)))
                mark('ci-prune-assertions-passed')
                return result
            patch.setattr(runtime_module.PredictionRuntime,'start',actual_prune_start)
        if scenario.startswith('consumer-ci-metadata-'):
            from open_trader import prediction_runtime as runtime_module
            from tests.test_lp_metadata_async_persistence import _exercise_actual_runtime_metadata_stop
            native_metadata_start=runtime_module.PredictionRuntime.start
            def actual_metadata_start(runtime):
                result=native_metadata_start(runtime)
                _exercise_actual_runtime_metadata_stop(runtime,adapter,patch,
                    scenario.endswith('-active'),real_connect)
                (events/'ci-metadata-observations.json').write_text(json.dumps(dict(
                    state=runtime.state,production_owner=runtime.production_owner,
                    active_producers=scenario.endswith('-active'))))
                mark('ci-metadata-assertions-passed')
                return result
            patch.setattr(runtime_module.PredictionRuntime,'start',actual_metadata_start)
        if scenario=='repair-owner-loop-fault':
            import socketserver
            native_select=socketserver._ServerSelector.select
            def external_selector_io(selector,*a,**kw):
                if (threading.current_thread().name=='MainThread'
                        and (events/'owner-loop-fault-arm').exists()
                        and not (events/'owner-loop-fault-fired').exists()):
                    mark('owner-loop-fault-fired')
                    raise OSError('offline owner-loop selector failure')
                return native_select(selector,*a,**kw)
            patch.setattr(socketserver._ServerSelector,'select',external_selector_io)
        if scenario in ('consumer-core-init-failure','consumer-legacy-no-generation','consumer-incompatible','consumer-reconcile-error','consumer-reconcile-locked'):
            from open_trader import prediction_runtime as runtime_module
            native_start=runtime_module.PredictionRuntime.start
            native_release=runtime_module._RuntimeOwnershipLock.release
            native_store_init=PredictionArbitrageStore.__init__
            native_runtime_init=runtime_module.PredictionRuntime.__init__
            if scenario in ('consumer-legacy-no-generation','consumer-incompatible'):
                def selected_constructor(runtime,*args,**kwargs):
                    kwargs['reader_generation']=None if scenario=='consumer-legacy-no-generation' else 1
                    return native_runtime_init(runtime,*args,**kwargs)
                patch.setattr(runtime_module.PredictionRuntime,'__init__',selected_constructor)
            native_generation=runtime_module.read_minimum_reader_generation
            consumer_observations['generation_probes']=[]
            def read_generation(path):
                consumer_observations['generation_probes'].append(str(path))
                return native_generation(path)
            patch.setattr(runtime_module,'read_minimum_reader_generation',read_generation)
            def store_constructed(store,*args,**kwargs):
                result=native_store_init(store,*args,**kwargs)
                consumer_observations['actual_store_constructed']=True
                return result
            def release_owner(owner):
                if owner.held:
                    snapshot=sql_ownership_snapshot()
                    assert snapshot['opened'] and not snapshot['active'],snapshot
                    with (root/'data/prediction_arbitrage/runtime.lock').open('a+') as probe:
                        try:fcntl.flock(probe.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:consumer_observations['excluded_before_release']=True
                        else:raise AssertionError('owner absent before actual release')
                    consumer_observations['connections_at_owner_release']=snapshot
                return native_release(owner)
            def observe_start(runtime):
                try:
                    result=native_start(runtime)
                    if scenario.startswith('consumer-reconcile-'):
                        consumer_observations.update(state=runtime.state,production_owner=runtime.production_owner,
                            scheduler_started=runtime._lp_auto_scheduler is not None,
                            monitor_thread_started=getattr(runtime.monitor,'_thread',None) is not None,
                            history_started=runtime._history_thread is not None,
                            permit=json.loads((root/'lp-start-guard/permit.json').read_text()))
                        with (root/'data/prediction_arbitrage/runtime.lock').open('a+') as probe:
                            try:fcntl.flock(probe.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError:consumer_observations['excluded_before_cleanup']=True
                            else:raise AssertionError('nonready runtime lost actual owner')
                        (events/'consumer-observations.json').write_text(json.dumps(consumer_observations))
                        hold('notready')
                    return result
                except Exception as exc:
                    consumer_observations.update(state=runtime.state,error_type=type(exc).__name__,error=str(exc),
                        fatal_type=type(runtime._fatal).__name__,
                        cause_type=type(runtime._fatal.__cause__).__name__,
                        permit=json.loads((root/'lp-start-guard/permit.json').read_text()))
                    try:native_start(runtime)
                    except RuntimeError as retry:
                        assert 'cannot start from FAILED' in str(retry),retry
                        consumer_observations['repeat_start_denied']=True
                    else:raise AssertionError('failed runtime allowed repeated start')
                    (events/'consumer-observations.json').write_text(json.dumps(consumer_observations))
                    raise
            patch.setattr(PredictionArbitrageStore,'__init__',store_constructed)
            patch.setattr(runtime_module._RuntimeOwnershipLock,'release',release_owner)
            patch.setattr(runtime_module.PredictionRuntime,'start',observe_start)
            native_stop=runtime_module.PredictionRuntime.stop
            def stop_once(runtime):
                result=native_stop(runtime)
                native_stop(runtime)
                consumer_observations['after_idempotent_stop']=runtime.state
                (events/'consumer-observations.json').write_text(json.dumps(consumer_observations))
                return result
            patch.setattr(runtime_module.PredictionRuntime,'stop',stop_once)
        if 'optional-' in scenario or 'producer-' in scenario:
            from open_trader import prediction_runtime as runtime_module
            native_start=runtime_module.PredictionRuntime.start
            native_stop=runtime_module.PredictionRuntime.stop
            if 'optional-hourly' in scenario:
                native_constructor=runtime_module.PredictionRuntime.__init__
                def selected_history_wait(stop_event,seconds):
                    # Controlled existing timing port: one real completed source
                    # pass precedes the next actual native catalog operation.
                    if not (events/'hourly-first-source-complete').exists():
                        assert seconds==3600
                        mark('hourly-first-source-complete')
                        return False
                    return stop_event.wait(seconds)
                def hourly_constructor(runtime,*args,**kwargs):
                    kwargs['history_wait']=selected_history_wait
                    return native_constructor(runtime,*args,**kwargs)
                patch.setattr(runtime_module.PredictionRuntime,'__init__',hourly_constructor)
            if 'producer-candidate' in scenario or 'producer-prep-' in scenario:
                native_history=runtime_module.PolymarketLPService.refresh_price_history
                def observed_history(lp,*args,**kwargs):
                    result=native_history(lp,*args,**kwargs)
                    (events/'producer-preparation-result.json').write_text(json.dumps(result,default=str))
                    return result
                patch.setattr(runtime_module.PolymarketLPService,'refresh_price_history',observed_history)
            if 'producer-trial' in scenario:
                native_scan=runtime_module.PolymarketLPService.refresh_candidates
                def observed_trial_scan(lp,*args,**kwargs):
                    result=native_scan(lp,*args,**kwargs)
                    if result.get('recommendations') and not (events/'producer-trial-scan-published').exists():
                        temporary=(events/'clock').with_suffix('.new')
                        temporary.write_text('31');temporary.replace(events/'clock')
                        mark('producer-trial-scan-published')
                    return result
                patch.setattr(runtime_module.PolymarketLPService,'refresh_candidates',observed_trial_scan)
            native_close=PolymarketTradingClient.close
            native_release=runtime_module._RuntimeOwnershipLock.release
            def optional_started(runtime):
                result=native_start(runtime)
                assert runtime.state=='RUNNING' and runtime.production_owner
                assert runtime._lp_auto_scheduler is not None
                assert runtime._lp_thread is None and runtime._book_sampler_thread is None
                try:native_start(runtime)
                except RuntimeError as exc:assert 'cannot start from RUNNING' in str(exc)
                else:raise AssertionError('running current Runtime allowed repeated start')
                (events/'optional-runtime-started').write_text(json.dumps(dict(state=runtime.state,
                    scheduler_count=len([t for t in threading.enumerate() if t.name=='prediction-lp-auto']),
                    obsolete_sampler=runtime._book_sampler_thread is not None)))
                return result
            native_begin_stop=runtime_module.PredictionRuntime.begin_stop
            def optional_begin_stop(runtime):
                result=native_begin_stop(runtime)
                if not (events/'optional-ended').exists():
                    assert runtime.state=='STOPPING' and runtime.production_owner
                    assert runtime.lp is not None and runtime.execution is not None
                    assert runtime.store is not None and runtime._prediction_trading is adapter
                    assert (runtime._candidate_scan_thread if 'producer-candidate' in scenario else runtime._candidate_maintenance_thread if 'producer-trial' in scenario else runtime._lp_share_thread if 'optional-share' in scenario else runtime._history_thread).is_alive()
                    mark('optional-stopping-retained')
                return result
            patch.setattr(runtime_module.PredictionRuntime,'begin_stop',optional_begin_stop)
            def optional_stopped(runtime):
                try:
                    result=native_stop(runtime)
                except RuntimeError:
                    assert runtime.state=='STOPPING' and runtime.production_owner
                    assert runtime.lp is not None and runtime.execution is not None
                    assert runtime.store is not None and runtime._prediction_trading is adapter
                    worker=(runtime._lp_share_thread if 'optional-share' in scenario else runtime._candidate_maintenance_thread if 'producer-trial' in scenario else runtime._candidate_scan_thread if 'producer-candidate' in scenario else runtime._history_thread)
                    assert worker is not None and worker.is_alive()
                    assert not (events/'optional-ended').exists()
                    mark('optional-stopping-retained')
                    raise
                assert runtime.state=='STOPPED'
                mark('optional-runtime-stopped')
                if 'producer-candidate' in scenario:
                    (events/'producer-stopped.json').write_text(json.dumps(dict(state=runtime.state,queue_accepted=runtime.queue_lp_candidate_refresh())))
                return result
            def optional_closed(client):
                assert (events/'optional-ended').exists()
                result=native_close(client);mark('optional-trading-closed');return result
            def optional_released(owner):
                if owner.held:
                    assert (events/'optional-ended').exists() and (events/'optional-trading-closed').exists()
                    snap=sql_ownership_snapshot();assert snap['opened'] and not snap['active'],snap
                    (events/'optional-store-ownership.json').write_text(json.dumps(snap))
                    mark('optional-owner-released')
                return native_release(owner)
            patch.setattr(runtime_module.PredictionRuntime,'start',optional_started)
            patch.setattr(runtime_module.PredictionRuntime,'stop',optional_stopped)
            patch.setattr(PolymarketTradingClient,'close',optional_closed)
            patch.setattr(runtime_module._RuntimeOwnershipLock,'release',optional_released)
        if scenario.startswith('consumer-ci-http-'):
            from open_trader import prediction_runtime as ci_http_runtime
            def forbidden_ci_nleg(*args,**kwargs):
                mark('unexpected-nleg-construction')
                raise AssertionError('paused current HTTP must not construct N-leg client or solver')
            patch.setattr(ci_http_runtime.SolverServerOwner,'__init__',forbidden_ci_nleg)
            patch.setattr(ci_http_runtime.PredictTradingClient,'from_keychain',forbidden_ci_nleg)
        if scenario.endswith('consumer-nleg-paused-run'):
            from open_trader import prediction_runtime as runtime_module
            def forbidden(*args,**kwargs):
                mark('unexpected-nleg-construction')
                raise AssertionError('N-leg pause must suppress native non-LP construction')
            patch.setattr(runtime_module.SolverServerOwner,'__init__',forbidden)
            patch.setattr(runtime_module.PredictTradingClient,'from_keychain',forbidden)
        if scenario in ('consumer-resource-order','consumer-disabled-background','consumer-solver-order','consumer-cross-start-failure'):
            import asyncio
            from open_trader import prediction_runtime as runtime_module
            from open_trader.predict_source import PredictSource
            from open_trader.predict_cross_venue import PredictCrossVenueMonitor,LlmCrossVenueEquivalenceValidator
            from open_trader.polymarket_trading import PredictConfig
            order_events=[];runtime_holder=[]
            consumer_observations['events']=order_events
            def event(label):order_events.append(label)
            class EmptyStream:
                async def __anext__(self):
                    await asyncio.sleep(10)
                    raise StopAsyncIteration
                async def close(self):pass
            class ExternalPublic:
                async def list_events(self,**kw):return []
                async def get_order_books(self,**kw):return []
                async def subscribe(self,*args,**kw):return EmptyStream()
                async def close(self):pass
            def predict_http(request,**kw):
                assert '/v1/markets' in request.full_url,request.full_url
                return io.BytesIO(b'{"success":true,"data":[],"cursor":null}')
            class CurrentCross:
                # This public injected lifecycle wrapper constructs and forwards
                # to the real cross monitor after the actual Runtime resources
                # exist. It supplies no state, readiness or native completion.
                async def start(self):
                    runtime=runtime_holder[0]
                    source=PredictSource(PredictConfig(WALLET),key_loader=lambda:'offline-fixture',urlopen_fn=predict_http)
                    self.monitor=PredictCrossVenueMonitor(predict_source=source,
                        polymarket_monitor=runtime.monitor,validator=LlmCrossVenueEquivalenceValidator(runtime.store),
                        gamma_lookup=lambda *args,**kwargs:(),store=runtime.store,
                        shadow_observer=getattr(runtime.monitor,'_shadow_observer',None))
                    consumer_observations['cross_observer_same']=self.monitor._shadow_observer is runtime.monitor._shadow_observer
                    await self.monitor.start();event('cross.start')
                async def stop(self):
                    await self.monitor.stop();event('cross.stop')
                def snapshot(self):return self.monitor.snapshot()
            native_runtime_init=runtime_module.PredictionRuntime.__init__
            def runtime_init(runtime,*args,**kwargs):
                kwargs.update(n_leg_paused=False,enable_n_leg_background=scenario=='consumer-solver-order',cross_venue_monitor=CurrentCross())
                result=native_runtime_init(runtime,*args,**kwargs)
                runtime_holder.append(runtime)
                return result
            patch.setattr(runtime_module.PredictionRuntime,'__init__',runtime_init)
            if scenario=='consumer-cross-start-failure':
                native_task=asyncio.create_task
                def external_task(coroutine,*args,**kwargs):
                    if coroutine.cr_code is PredictCrossVenueMonitor._run.__code__:
                        coroutine.close()
                        mark('external-cross-task-start-failed')
                        raise OSError('offline cross task creation failed')
                    return native_task(coroutine,*args,**kwargs)
                patch.setattr(asyncio,'create_task',external_task)
            def observe(cls,name,label,*,after=False,amend=None):
                native=getattr(cls,name)
                def forward(value,*args,**kwargs):
                    if amend:kwargs=amend(kwargs)
                    if not after:event(label)
                    result=native(value,*args,**kwargs)
                    if after:event(label)
                    return result
                patch.setattr(cls,name,forward)
            observe(runtime_module._RuntimeOwnershipLock,'acquire','owner.acquire',after=True)
            native_generation=runtime_module.read_minimum_reader_generation
            def generation(*args,**kwargs):
                result=native_generation(*args,**kwargs);event('generation.read');return result
            patch.setattr(runtime_module,'read_minimum_reader_generation',generation)
            observe(PredictionArbitrageStore,'__init__','store.construct')
            native_policy=PredictionArbitrageStore.apply_safety_policy
            def policy(store,value,*,git_sha):
                assert isinstance(value,dict)
                consumer_observations['policy_sha']=git_sha
                result=native_policy(store,value,git_sha=git_sha);event('policy.apply');return result
            patch.setattr(PredictionArbitrageStore,'apply_safety_policy',policy)
            observe(runtime_module.PolymarketMonitor,'__init__','monitor.construct',
                amend=lambda kwargs:{**kwargs,'public_client_factory':ExternalPublic})
            observe(runtime_module.PolymarketMonitor,'set_auto_eat_observer','auto_eat.bind',after=True)
            observe(runtime_module.PolymarketMonitor,'start','polymarket.start',after=True)
            observe(runtime_module.PolymarketMonitor,'stop','polymarket.stop',after=True)
            observe(runtime_module.PredictionExecutionService,'__init__','execution.construct')
            observe(runtime_module.PredictionExecutionService,'reconcile_startup','reconcile')
            observe(runtime_module.SolverServerOwner,'__init__','solver.construct')
            observe(runtime_module.SolverServerOwner,'close','solver.close',after=True)
            observe(runtime_module.PredictionLiveResolver,'start','resolver.start',after=True)
            observe(runtime_module.PredictionLiveResolver,'stop','resolver.stop',after=True)
            observe(runtime_module.PredictionMonitorSelectionDriver,'start','selection.start',after=True)
            observe(runtime_module.PredictionMonitorSelectionDriver,'stop','selection.stop',after=True)
            observe(PolymarketTradingClient,'close','trading.close',after=True)
            native_sdk=runtime_sdk
            def constructed_sdk(cls,config):
                result=native_sdk(cls,config);event('client.construct');return result
            runtime_sdk=constructed_sdk
            native_start=runtime_module.PredictionRuntime.start
            def started(runtime):
                result=native_start(runtime)
                assert runtime.state=='RUNNING' and runtime.production_owner
                consumer_observations.update(state=runtime.state,production_owner=runtime.production_owner,
                    resolver_is_none=runtime.live_resolver is None,selection_is_none=runtime.monitor_selection_driver is None,
                    shadow_exists=runtime.n_leg_shadow is not None,
                    monitor_shadow_callable=callable(getattr(runtime.monitor,'_shadow_observer',None)),
                    cross_unavailable=isinstance(runtime.cross_venue_monitor,runtime_module._UnavailableCrossVenueMonitor))
                try:native_start(runtime)
                except RuntimeError as exc:
                    assert 'cannot start from RUNNING' in str(exc)
                    consumer_observations['repeat_start_denied']=True
                else:raise AssertionError('repeated production start accepted')
                return result
            patch.setattr(runtime_module.PredictionRuntime,'start',started)
            native_release=runtime_module._RuntimeOwnershipLock.release
            def released(owner):
                if owner.held:
                    snapshot=sql_ownership_snapshot()
                    consumer_observations['connections_at_owner_release']=snapshot
                    (events/'consumer-observations.json').write_text(json.dumps(consumer_observations))
                    assert snapshot['opened'] and not snapshot['active'],snapshot
                    assert snapshot['opened']==snapshot['closed'],snapshot
                    event('owner.release')
                    (events/'consumer-observations.json').write_text(json.dumps(consumer_observations))
                return native_release(owner)
            patch.setattr(runtime_module._RuntimeOwnershipLock,'release',released)
            native_stop=runtime_module.PredictionRuntime.stop
            def stopped(runtime):
                result=native_stop(runtime)
                native_stop(runtime)
                return result
            patch.setattr(runtime_module.PredictionRuntime,'stop',stopped)
        patch.setattr(polymarket_trading.PolymarketTradingClient,'from_keychain',classmethod(runtime_sdk))
        patch.setattr(polymarket_trading,'datetime',BusinessDateTime)
        patch.setattr(polymarket_lp_market_reader,'datetime',BusinessDateTime)
        # A real adapter snapshot establishes its retained public connection,
        # distinct from the producer's disposable catalog/reward clients.
        if not scenario.startswith('automatic') and not scenario.endswith('incomplete') and 'foreground-cli' not in scenario and not scenario.startswith(('startup-','consumer-')):
            adapter.lp_snapshot(dict(market_id='market-1',condition_id=CONDITION_ID,
                token_id=TOKEN_ID,outcome='Yes',price=Decimal('.39'),quantity=Decimal('1')))
        from open_trader.prediction_release import prepare_lp_target
        real_run=subprocess.run
        launchd=scenario.startswith('launchd-context') or 'launchd-' in scenario
        launchd_plist=root/'com.open-trader.prediction-service.plist'
        launchd_args=[sys.executable,'-m','open_trader','prediction-service','--mode','production',
            '--data-dir',str(root/'data'),'--config',str(config_path),
            '--host','127.0.0.1','--port',str(port),'--release-manifest',str(release_root/'ops/prediction-service-release.json')]
        if launchd:
            # Model only the guard's Darwin view for these launchd scenarios.
            # The interpreter and process-global Linux stdlib remain real.
            if sys.platform.startswith('linux'):
                from types import SimpleNamespace
                patch.setattr(prediction_start_guard,'sys',SimpleNamespace(**{**vars(sys),'platform':'darwin'}))
            import plistlib
            launchd_plist.write_bytes(plistlib.dumps(dict(Label='com.open-trader.prediction-service',
                WorkingDirectory=str(Path.cwd()),ProgramArguments=launchd_args,
                EnvironmentVariables=dict(PYTHONPATH=str(Path.cwd()/'src')),
                RunAtLoad=True,KeepAlive={'SuccessfulExit':False},ExitTimeOut=0)))
        api=(offline_github_fixture(release_root,root/'ci-api')
            if not scenario.startswith(('automatic','operator')) else None)
        def external_process(command,*a,**kw):
            if (os.environ.get('D2_FIXTURE_FOREGROUND_OLD_PID') and isinstance(command,list)
                    and command[:2]==['/bin/ps','-p']
                    and command[2]==os.environ['D2_FIXTURE_FOREGROUND_OLD_PID']):
                # Read the actual selected child UID/GID/start time. Only its
                # fixture entry command is translated to the production CLI
                # command that the native exec boundary represents.
                result=real_run(command,*a,**kw)
                if result.returncode:return result
                args=json.loads(os.environ['D2_FIXTURE_FOREGROUND_OLD_ARGS'])
                if os.environ.get('D2_FIXTURE_FOREGROUND_WRONG_ARGS'):
                    args[args.index('--data-dir')+1]=str(root/'other-data')
                import shlex
                fields=result.stdout.strip().split(None,7)
                assert len(fields)==8,result.stdout
                return subprocess.CompletedProcess(command,0,' '.join(fields[:7])+' '+shlex.join(args)+'\n','')
            if launchd and isinstance(command,list) and command[0]=='/bin/launchctl':
                if scenario=='launchd-context-unavailable':
                    return subprocess.CompletedProcess(command,1,'','indeterminate launchd context')
                pid=os.getpid()+(1 if scenario=='launchd-context-wrong-pid' else 0)
                text='path = '+str(launchd_plist)+'\nworking directory = '+str(Path.cwd())+'\npid = '+str(pid)+'\narguments = {\n'+'\n'.join(launchd_args)+'\n}\n'
                return subprocess.CompletedProcess(command,0,text,'')
            if launchd and isinstance(command,list) and command[:2]==['/bin/ps','-p']:
                import shlex
                return subprocess.CompletedProcess(command,0,f'{os.getpid()} {os.getuid()} '+shlex.join(launchd_args)+'\n','')
            if (launchd and sys.platform.startswith('linux') and isinstance(command,list)
                    and command==['/usr/sbin/lsof','-Fpn','--',str(root/'data/prediction_arbitrage/runtime.lock')]):
                # Translate actual Linux kernel ownership to the modeled lsof
                # format; never supply a canned owner for a free/wrong file.
                path=Path(command[-1]);info=path.stat();holders=set()
                for line in Path('/proc/locks').read_text().splitlines():
                    fields=line.split()
                    if len(fields)>5 and fields[1]=='FLOCK' and fields[3]=='WRITE':
                        major,minor,inode=fields[5].split(':')
                        if (int(inode)==info.st_ino and int(major,16)==os.major(info.st_dev)
                                and int(minor,16)==os.minor(info.st_dev)):
                            holders.add(int(fields[4]))
                if holders!={os.getpid()}:return subprocess.CompletedProcess(command,1,'','no exact self flock')
                descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:pass
                    else:raise AssertionError('kernel owner must exclude an independent descriptor')
                finally:os.close(descriptor)
                return subprocess.CompletedProcess(command,0,f'p{os.getpid()}\nn{path}\n','')
            if launchd and isinstance(command,list) and command[0]=='/usr/sbin/lsof' and 'cwd' in command:
                return subprocess.CompletedProcess(command,0,f'p{os.getpid()}\nfcwd\nn{Path.cwd()}\n','')
            if (isinstance(command,list) and len(command)>2
                    and command[1]==str(release_root/'scripts/deployment_preflight.py')
                    and '--expected-sha' in command):
                import importlib.util,io
                from contextlib import redirect_stdout,redirect_stderr
                spec=importlib.util.spec_from_file_location('actual_offline_preflight',command[1])
                verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
                verifier.GitHub=lambda:api  # external GitHub boundary only
                out,err=io.StringIO(),io.StringIO()
                with redirect_stdout(out),redirect_stderr(err):code=verifier.main(command[2:])
                if kw.get('check') and code:raise subprocess.CalledProcessError(code,command,out.getvalue(),err.getvalue())
                return subprocess.CompletedProcess(command,code,out.getvalue(),err.getvalue())
            return real_run(command,*a,**kw)
        patch.setattr(subprocess,'run',external_process)
        if 'foreground-cli' in scenario and not scenario.startswith('automatic'):
            from open_trader.prediction_service import main
            native_exec=os.execv
            native_kill=os.kill
            def external_signal(pid,signum):
                if (signum==signal.SIGTERM and str(pid)==os.environ.get('D2_FIXTURE_FOREGROUND_OLD_PID')):
                    (events/'foreground-term-sent').write_text(str(pid))
                return native_kill(pid,signum)
            patch.setattr(os,'kill',external_signal)
            def external_exec(executable,arguments):
                assert executable==sys.executable
                assert arguments[:4]==[sys.executable,'-m','open_trader','prediction-service']
                assert not any(value.startswith('--operator-') for value in arguments)
                (events/'foreground-native-exec').write_text(json.dumps(dict(pid=os.getpid(),arguments=arguments)))
                # Actual process replacement, same PID. The executable target
                # reinstalls only this fixture's external SDK/SQL/OS clocks,
                # then calls the unchanged bare production service entry.
                native_exec(executable,[sys.executable,'-m','tests.lp_d2_service_harness','child',
                    str(root),str(port),'automatic-rollback-current-foreground-cli',str(config_path)])
            patch.setattr(os,'execv',external_exec)
            mark('service-starting')
            action='--operator-restart' if scenario.startswith('operator-restart') else '--operator-start'
            raise SystemExit(main(['--mode','production',action,'--data-dir',str(root/'data'),
                '--config',str(config_path),'--host','127.0.0.1','--port',str(port),
                '--release-manifest',str(release_root/'ops/prediction-service-release.json')]))
        if not scenario.startswith('automatic'):
            from contextlib import ExitStack
            with ExitStack() as operation:
                operation_fd=None
                if launchd:
                    handle=operation.enter_context((launchd_plist.parent/'.com.open-trader.prediction-service.release.lock').open('a+'))
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    operation_fd=handle.fileno()
                prepare_lp_target(runtime_root=root,release_root=Path.cwd(),python=sys.executable,
                    operation_nonce='offline-operator-recovery' if scenario.startswith('operator') else 'offline-operation-1',
                    host='127.0.0.1',port=port,
                    config_path=None if 'launchd-nested' in scenario else config_path,
                    manager='launchd' if launchd else 'foreground',
                    manager_identity=str(launchd_plist) if launchd else None,operation_fd=operation_fd)
                identity=prediction_start_guard.identity_for_runtime(data_dir=root/'data',prediction_config_path=config_path,code_root=Path.cwd(),host='127.0.0.1',port=port)
                prediction_start_guard.arm_for_operator_start(identity,
                    'offline-operator-recovery' if scenario.startswith('operator') else 'offline-operation-1',operation_fd=operation_fd)
        def observe_external_writer():
            while not (events/'process-finalizing').exists():
                status=adapter.lp_metadata_persistence_status()
                if status['state'] in ('STOPPING','STOPPED'):
                    mark('metadata-admission-closed');return
                threading.Event().wait(.01)
        threading.Thread(target=observe_external_writer,daemon=True).start()
        mark('service-starting')
        result=serve_prediction_service(data_dir=root/'data',prediction_config_path=config_path,host='127.0.0.1',port=port,mode='production',release_manifest_path=release_root/'ops/prediction-service-release.json',lp_clock=business_clock,lp_utc_now=utc_clock)
        raise SystemExit(result)



def _install_ci_fence_observers(patch,events,scenario,observations):
    """Call through real fence/observer/lifecycle ports in only CI fence children."""
    import asyncio,json
    from open_trader import prediction_runtime as module
    from open_trader.polymarket_monitor import PolymarketMonitor
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    names=[];observations['events']=names
    def save():(events/'ci-fence-observations.json').write_text(json.dumps(observations))
    native_generation=module.read_minimum_reader_generation
    def generation(*args,**kwargs):
        result=native_generation(*args,**kwargs);names.append('fence.read');return result
    patch.setattr(module,'read_minimum_reader_generation',generation)
    class Stream:
        async def __anext__(self):await asyncio.Future()
        async def close(self):pass
    class Public:
        async def list_events(self,**kwargs):return []
        async def get_order_books(self,**kwargs):return []
        async def subscribe(self,*args,**kwargs):return Stream()
        async def close(self):pass
    native_monitor=PolymarketMonitor.__init__
    def monitor(instance,*args,**kwargs):
        return native_monitor(instance,*args,**(kwargs|dict(public_client_factory=Public)))
    patch.setattr(PolymarketMonitor,'__init__',monitor)
    def observe_setter(name,label):
        native=getattr(PolymarketMonitor,name)
        def setter(instance,*args,**kwargs):
            result=native(instance,*args,**kwargs);names.append(label);return result
        patch.setattr(PolymarketMonitor,name,setter)
    for kind in ('ready','observation','auto_eat','failure'):
        observe_setter('set_'+kind+'_observer',kind+'.bind')
    native_store=PredictionArbitrageStore.__init__
    def store(instance,*args,**kwargs):
        observations['writable_store_constructed']=True
        return native_store(instance,*args,**kwargs)
    patch.setattr(PredictionArbitrageStore,'__init__',store)
    native_start=module.PredictionRuntime.start
    def start(runtime):
        try:
            result=native_start(runtime)
            observations.update(state=runtime.state,production_owner=runtime.production_owner,
                legacy_retired=runtime.legacy_retired,reader_generation=runtime._reader_generation,
                n_leg_paused=runtime.n_leg_paused)
            save();return result
        except Exception as exc:
            observations.update(state=runtime.state,production_owner=runtime.production_owner,
                error_type=type(exc).__name__,error=str(exc))
            save();raise
    patch.setattr(module.PredictionRuntime,'start',start)
    native_stop=module.PredictionRuntime.stop
    def stop(runtime):
        result=native_stop(runtime)
        observations['after_stop']=runtime.state;save();return result
    patch.setattr(module.PredictionRuntime,'stop',stop)

def offline_github_fixture(release,scratch):
    """Controlled external GitHub data, checked by the unchanged real verifier.

    No supplied PASS JSON: the manager invokes the real preflight; its Git,
    selected-interpreter and artifact/partition verification execute normally.
    """
    import ast,hashlib,importlib.util,io,re,zipfile
    from tests.test_deployment_preflight import evidence,pack,literal_junit
    scratch.mkdir()
    _,api=evidence.__wrapped__(scratch)
    sha=subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    previous_sha=api.run['head_sha']
    def bind(value):return json.loads(json.dumps(value).replace(previous_sha,sha))
    api.run=bind(api.run);api.runs=[api.run];api.jobs=bind(api.jobs);api.checks=bind(api.checks);api.tip=sha
    spec=importlib.util.spec_from_file_location('actual_fixture_ci',release/'scripts/ci_evidence.py')
    ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)
    spec=importlib.util.spec_from_file_location('actual_fixture_partition',release/'scripts/ci_partition.py')
    partition=importlib.util.module_from_spec(spec);spec.loader.exec_module(partition)
    parts={}
    for scope,paths in ci.service_partitions(release).items():
        parts[scope]=[]
        for path in paths:
            tree=ast.parse((release/path).read_text())
            names=[n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_')]
            parts[scope].extend(path+'::'+name for name in names)
    retired=json.loads((release/'scripts/ci_nleg_retired.json').read_text())['retired']
    retired_nodes=[v['path']+'::test_ok' for v in retired]
    proof=partition.verify_partition(sum(parts.values(),[])+retired_nodes,parts,retired_nodes,release)
    lock=hashlib.sha256((release/'uv.lock').read_bytes()).hexdigest()
    bases=re.findall(r'^FROM (\S+)',(release/'Dockerfile.dev').read_text(),re.MULTILINE)
    fresh=datetime.now(UTC)
    downloads={}
    for artifact in api.artifacts:
        with zipfile.ZipFile(io.BytesIO(api.downloads[artifact['id']])) as archive:
            files={name:archive.read(name) for name in archive.namelist()}
        metadata=json.loads(files['evidence.json']);scope=metadata['scope']
        nodes=sorted(parts[scope]) if scope!='portable' else ['acceptance/test_prediction_arbitrage_scenarios.py::test_ok']
        metrics=json.loads(files['metrics.json']);metrics.update(source_sha=sha,selected_nodeids=nodes,results=[])
        for node in nodes:
            metrics['results'].append(dict(nodeid=node,execution_nodeid=node,outcome='passed',phases={
                phase:dict(duration=.1,outcome='passed',worker='gw0' if scope=='prediction' else 'serial')
                for phase in ('setup','call','teardown')}))
        files['metrics.json']=json.dumps(metrics).encode()
        files['selected-nodeids.json']=json.dumps(nodes).encode()
        files['junit.xml']=literal_junit(metrics)
        manifest=json.loads(files['dependency-manifest.json']);manifest.update(source_sha=sha,lock_sha256=lock,base_images=bases)
        files['dependency-manifest.json']=json.dumps(manifest).encode()
        images=json.loads(files['image.json']);images[0]['Config']['Env']=[f'OPEN_TRADER_TEST_SOURCE_SHA={sha}','OPEN_TRADER_TEST_SOURCE_STATE=clean']
        files['image.json']=json.dumps(images).encode()
        if scope=='portable':files['partition.json']=json.dumps(proof).encode()
        env=metadata['environment']
        for name,key in [('dependency-manifest.json','dependency_manifest_sha256'),('image.json','image_inspect_sha256'),
                         ('metrics.json','metrics_sha256'),('selected-nodeids.json','selected_nodeids_sha256'),('junit.xml','junit_sha256')]:
            env[key]=hashlib.sha256(files[name]).hexdigest()
        if scope=='portable':env['partition_sha256']=hashlib.sha256(files['partition.json']).hexdigest()
        selection=ci.expected_selection(scope,release)
        metadata.update(source_sha=sha,lock_sha256=lock,selected_nodeids=nodes,selection=selection,
            selection_sha256=ci.selection_sha256(selection),policy=ci.retirement_policy(release))
        files['evidence.json']=json.dumps(metadata).encode()
        data=pack(files);downloads[artifact['id']]=data
        artifact.update(name=f'ci-{scope}-{sha}-22-2',digest='sha256:'+hashlib.sha256(data).hexdigest(),
            created_at=fresh.isoformat(),expires_at=(fresh+timedelta(days=1)).isoformat(),
            workflow_run=dict(id=22,head_sha=sha,head_branch='main'))
    api.downloads=downloads
    return api


if __name__=='__main__':
    assert sys.argv[1]=='child'
    child(Path(sys.argv[2]),int(sys.argv[3]),sys.argv[4],Path(sys.argv[5]))
