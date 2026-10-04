"""Stopped recovery command contract, with isolated OS/systemd boundaries."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from open_trader import prediction_cloud as cloud
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def database_rows(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as con:
        return {name: con.execute('SELECT * FROM "'+name+'" ORDER BY rowid').fetchall()
                for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def stopped_cloud(tmp_path, monkeypatch, request):
    native = getattr(request, 'param', False)
    if native:
        if sys.platform != 'linux' or os.geteuid() != 0:
            pytest.skip('native service ownership requires an isolated Linux root fixture')
        caps = int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                        if line.startswith('CapEff:')), 16)
        if any(not caps & (1 << bit) for bit in (0, 1, 3, 6, 7)):
            pytest.skip('native fixture requires CHOWN/DAC_OVERRIDE/FOWNER/SETUID/SETGID')
        service = cloud.pwd.getpwnam('daemon')
        service_name = 'daemon'
    else:
        service = SimpleNamespace(pw_uid=os.getuid() or 1001, pw_gid=os.getgid())
        service_name = 'prediction'
    root = Path(__file__).resolve().parents[1]
    release = tmp_path/'release'; release.mkdir()
    for name in ['uv.lock','pyproject.toml','scripts/deployment_preflight.py',
                 'ops/prediction-service-release.json','src/open_trader/prediction_cloud.py',
                 'src/open_trader/__init__.py']:
        target = release/name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/name,target)
    for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'),('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    runtime = tmp_path/'runtime'; (runtime/'config').mkdir(parents=True)
    (runtime/'config/prediction_arbitrage.json').write_text('{}')
    store = PredictionArbitrageStore(runtime/'data'); db = store.path
    state = dict(state='paused',paused=True,stage='catalog',generation=1,attempt=8,
                 failure_count=1,last_error='OperationalError',last_error_category='operator_attention',
                 last_error_chain=['OperationalError'],metadata_completed_count=1703,
                 metadata_total_count=19148,completed_count=240,total_count=3406,
                 waiting_market_count=17445)
    with sqlite3.connect(db) as con:
        con.execute('INSERT INTO lp_preparation VALUES (1,1,?,?)',(json.dumps(state),'2026-10-03T23:05:56Z'))
        con.executemany('''INSERT INTO lp_preparation_items
            (condition_id,generation,retry_used,failure_count,state,paused,stage,error,next_retry_at,updated_at)
            VALUES (?,1,0,1,'waiting_retry',0,'metadata','ReadTimeout',?,?)''',
            [(f'condition-{i}','2026-10-03T14:50:00Z','2026-10-03T14:49:00Z') for i in range(17445)])
        con.execute('INSERT INTO lp_auto_pool VALUES (1,?)',(json.dumps({'desired_running':False,'reservation':'retained'}),))
        con.execute("INSERT INTO llm_usage VALUES ('old-audit','relation','failed','{}','2000-01-01T00:00:00Z')")
        con.execute("INSERT INTO lp_sessions VALUES ('retained-session','retained-intent','entry_submit_pending',?, '2026-10-03','2026-10-03')",
                    (json.dumps({'reservation':'UNKNOWN','order_id':'retained-order'}),))
        con.execute("INSERT INTO lp_actions VALUES ('retained-action','retained-session','entry','unknown','{}','2026-10-03','2026-10-03')")
    con.close()
    for name in ['runtime.lock','lp-preparation.lock']:
        (db.parent/name).touch(mode=0o600)
    venv=tmp_path/'venv';(venv/'bin').mkdir(parents=True)
    (venv/'bin/python').symlink_to(Path(sys.executable).resolve())
    shutil.copy2(Path(sys.prefix)/'pyvenv.cfg',venv/'pyvenv.cfg')
    site=venv/f'lib/python{sys.version_info.major}.{sys.version_info.minor}'
    site.mkdir(parents=True)
    (site/'site-packages').symlink_to(Path(sys.prefix)/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    cfg = cloud.CloudConfig(release,runtime,venv/'bin/python',service_name,sha,
                            '','','','','shadow',1,credential_backend='file',
                            credentials_file=str(tmp_path/'credentials/polymarket.json'),memory_max_bytes=738197504)
    config = tmp_path/'cloud.json'; config.write_text(json.dumps({**cfg.__dict__,
        'release_root':str(release),'runtime_root':str(runtime),'python':str(cfg.python)}));config.chmod(0o600)
    unit = tmp_path/'prediction.service';unit.write_text(cloud.render_unit(cfg));unit.chmod(0o644)
    cloud.record(cfg,'stopped')
    for directory in [runtime,runtime/'config',runtime/'data',db.parent]:directory.chmod(0o700)
    for f in runtime.rglob('*'):
        if f.is_file():f.chmod(0o600)
    backup = tmp_path/'backups'; backup.mkdir(mode=0o700)
    operation = tmp_path/'operation.lock';operation.touch(mode=0o600)
    monkeypatch.setattr(cloud,'UNIT_PATH',unit)
    monkeypatch.setattr(cloud,'CONFIG_PATH',config)
    monkeypatch.setattr(cloud,'OPERATION_LOCK',operation,raising=False)
    monkeypatch.setattr(cloud,'__file__',str(release/'src/open_trader/prediction_cloud.py'))
    monkeypatch.setattr(sys,'prefix',str(venv))  # Selected helper interpreter context.
    if native:
        for path in [runtime, *runtime.rglob('*')]:
            if path != cfg.record:
                os.chown(path, service.pw_uid, service.pw_gid)
        for path in (tmp_path, *tmp_path.parents):
            if path.is_relative_to('/tmp') and path != Path('/tmp'):
                path.chmod(path.stat().st_mode | 0o055)
    else:
        monkeypatch.setattr(os,'geteuid',lambda:0)
    # Model root-owned release/control paths and a separate service UID at the
    # stat/pwd/grp OS boundary; run genuine git, SQLite, filesystem and flock.
    if not native:
        uid = os.getuid(); gid = os.getgid()
        monkeypatch.setattr(cloud.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=uid or 1001,pw_gid=gid))
        monkeypatch.setattr(cloud.grp,'getgrnam',lambda name:SimpleNamespace(gr_gid=gid))
        original_stat = Path.lstat
        def virtual_owner(path):
            st = original_stat(path); values=list(st)
            values[4] = (uid or 1001) if path.is_relative_to(runtime) else 0
            if not path.is_relative_to(tmp_path):
                values[0] &= ~0o022  # Cloud's immutable interpreter/ancestor policy.
            if path == cfg.record or path.name.startswith('unit-backup-'):values[4]=0
            return os.stat_result(values)
        monkeypatch.setattr(Path,'lstat',virtual_owner)
        original_fstat = os.fstat
        lock_owners = {(original_stat(path).st_dev, original_stat(path).st_ino): owner
                       for path, owner in [(operation,0),(db.parent/'runtime.lock',uid or 1001),
                                           (db.parent/'lp-preparation.lock',uid or 1001), (config,0), (unit,0), (cfg.record,0)]}
        def virtual_fd_owner(fd):
            st=original_fstat(fd);values=list(st)
            values[4]=lock_owners.get((st.st_dev,st.st_ino),st.st_uid)
            for path in (config, unit, cfg.record):
                observed=original_stat(path)
                if (st.st_dev,st.st_ino)==(observed.st_dev,observed.st_ino):values[4]=0
            return os.stat_result(values)
        monkeypatch.setattr(os,'fstat',virtual_fd_owner)
        def simulated_chown(fd, owner, group):
            assert (owner,group)==(uid or 1001,gid)
            st=original_fstat(fd);lock_owners[(st.st_dev,st.st_ino)]=owner
        monkeypatch.setattr(os,'fchown',simulated_chown)
        original_read_text = Path.read_text
        def kernel_locks(path,*args,**kwargs):
            if str(path)=='/proc/locks':return ''  # Linux kernel inspection boundary.
            return original_read_text(path,*args,**kwargs)
        monkeypatch.setattr(Path,'read_text',kernel_locks)
    original_run = subprocess.run
    observations=[]; systemd={'active':False,'boot':'disabled'}
    def external(args,**kwargs):
        observations.append(tuple(str(x) for x in args))
        if args[0]=='systemctl':
            assert args[1]=='show', 'recovery must not mutate systemd'
            env=' '.join(x.removeprefix('Environment=') for x in cloud.render_unit(cfg).splitlines() if x.startswith('Environment='))
            state={'LoadState':'loaded','ActiveState':'active' if systemd['active'] else 'inactive',
                   'MainPID':'321' if systemd['active'] else '0','SubState':'running' if systemd['active'] else 'dead',
                   'FragmentPath':str(unit),'DropInPaths':'','NeedDaemonReload':'no','Environment':env,
                   'User':cfg.user,'Group':cfg.user,'WorkingDirectory':str(release),
                   'UnitFileState':systemd['boot'],'Restart':'no','NRestarts':'0','MemoryMax':'738197504',
                   'CPUQuotaPerSecUSec':'1s','TasksMax':'96'}
            state.update(systemd.get('properties',{}))
            return subprocess.CompletedProcess(args,0,'\n'.join(k+'='+v for k,v in state.items()),'')
        if args[0]=='ss':return subprocess.CompletedProcess(args,0,'','')
        assert args[0] in ['git',str(cfg.python)], 'no credential/network subprocess'
        return original_run(args,**kwargs)
    monkeypatch.setattr(subprocess,'run',external)
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('recovery attempted network'))
    return SimpleNamespace(cfg=cfg,config=config,db=db,backup=backup,operation=operation,
                           state=state,systemd=systemd,observations=observations,service=service,native=native)


def invoke(f, **changes):
    return cloud.recover_stopped_preparation(f.config,expected_sha=changes.pop('expected_sha',f.cfg.expected_sha),
        expected_generation=changes.pop('expected_generation',1),backup_root=f.backup,**changes)


def test_stopped_recovery_preserves_full_waiting_universe_and_other_tables(stopped_cloud):
    f=stopped_cloud; before=database_rows(f.db)
    result=invoke(f)
    assert result['status']=='PREPARATION_RECOVERED'
    assert result['before']=={'generation':1,'state':'paused','paused':True,'stage':'catalog'}
    assert result['after']=={'generation':2,'state':'ready','paused':False,'stage':'catalog'}
    assert result['recovered_item_count']==0
    after=database_rows(f.db)
    assert len(after['lp_preparation_items'])==17445
    assert {k:v for k,v in after.items() if k!='lp_preparation'}=={k:v for k,v in before.items() if k!='lp_preparation'}
    backup=Path(result['backup'])
    assert database_rows(backup/'consistent.sqlite3')==before
    manifest=json.loads((backup/'manifest.json').read_text())
    assert manifest['status']=='COMPLETE' and manifest['sqlite_integrity']=='ok'
    for item in manifest['files']:
        assert hashlib.sha256((backup/item['path']).read_bytes()).hexdigest()==item['sha256']
    assert 'OperationalError' not in json.dumps(result)
    assert not f.systemd['active']


def test_recovery_command_requires_explicit_generation_and_reports_redacted_result(stopped_cloud,capsys):
    f=stopped_cloud
    args=['recover-preparation','--config',str(f.config),'--expected-sha',f.cfg.expected_sha,
          '--expected-generation','1','--backup-root',str(f.backup)]
    assert cloud.main(args)==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='PREPARATION_RECOVERED' and result['after']['generation']==2
    assert str(f.cfg.credentials_file) not in json.dumps(result)
    assert cloud.main(args)==2
    rejected=json.loads(capsys.readouterr().out)
    assert rejected['status']=='BLOCKED'
    assert len(list(f.backup.iterdir()))==1  # No second backup/transaction for an obsolete generation.


def test_backup_failure_reports_partial_location_and_never_recovers(stopped_cloud,monkeypatch,capsys):
    f=stopped_cloud;before=database_rows(f.db)
    def disk_full(*a,**k):raise OSError('simulated private payload must not escape')
    monkeypatch.setattr(shutil,'copyfileobj',disk_full)
    assert cloud.main(['recover-preparation','--config',str(f.config),'--expected-sha',f.cfg.expected_sha,
                       '--expected-generation','1','--backup-root',str(f.backup)])==2
    result=json.loads(capsys.readouterr().out)
    assert result['phase']=='backup' and result['recovery_committed'] is False
    assert result['before']['generation']==1 and Path(result['backup']).is_dir()
    assert result['recovered_item_count']==0 and 'private payload' not in json.dumps(result)
    assert database_rows(f.db)==before


@pytest.mark.parametrize('case',['generation','boolean_generation','sha','production','disabled',
    'unpaused_nleg','credential_in_runtime','running','enabled_boot','caps','record','unit','dirty',
    'source','interpreter','prefix','userbase','root','database_symlink','wal_symlink','backup_permissions'])
def test_recovery_rejects_unverified_boundaries_without_changes(stopped_cloud,monkeypatch,case):
    f=stopped_cloud;before=database_rows(f.db);kwargs={}
    config=json.loads(f.config.read_text())
    if case=='generation':kwargs['expected_generation']=2
    elif case=='boolean_generation':kwargs['expected_generation']=True
    elif case=='sha':kwargs['expected_sha']='b'*40
    elif case=='production':config['mode']='production'
    elif case=='disabled':config.update(credential_backend='disabled',credentials_file='')
    elif case=='unpaused_nleg':config['n_leg_paused']=0
    elif case=='credential_in_runtime':config['credentials_file']=str(f.cfg.runtime_root/'config/secret.json')
    elif case=='running':f.systemd['active']=True
    elif case=='enabled_boot':f.systemd['boot']='enabled'
    elif case=='caps':f.systemd['properties']={'MemoryMax':'999999999'}
    elif case=='record':cloud.record(f.cfg,'ready')
    elif case=='unit':cloud.UNIT_PATH.write_text(cloud.UNIT_PATH.read_text()+'# drift\n')
    elif case=='dirty':(f.cfg.release_root/'src/open_trader/__init__.py').write_text('# drift')
    elif case=='source':monkeypatch.setattr(cloud,'__file__',str(f.cfg.release_root/'wrong.py'))
    elif case=='interpreter':monkeypatch.setattr(sys,'executable','/unverified/python')
    elif case=='prefix':monkeypatch.setattr(sys,'prefix','/unverified/venv')
    elif case=='userbase':monkeypatch.setenv('PYTHONUSERBASE','/unverified/user-site')
    elif case=='root':monkeypatch.setattr(os,'geteuid',lambda:1001)
    elif case=='database_symlink':
        original=f.db.with_suffix('.original');f.db.rename(original);f.db.symlink_to(original)
    elif case=='wal_symlink':
        wal=Path(str(f.db)+'-wal');wal.unlink(missing_ok=True)
        wal.symlink_to(f.cfg.runtime_root/'config/prediction_arbitrage.json')
    elif case=='backup_permissions':f.backup.chmod(0o755)
    f.config.write_text(json.dumps(config))
    with pytest.raises((ValueError,OSError)):invoke(f,**kwargs)
    # Remove the deliberately invalid WAL reference before inspecting SQLite.
    if case=='wal_symlink':Path(str(f.db)+'-wal').unlink()
    assert database_rows(f.db)==before
    assert not list(f.backup.iterdir())


def test_recovery_rechecks_loaded_identity_after_backup(stopped_cloud,monkeypatch):
    f=stopped_cloud;before=database_rows(f.db);original=shutil.copyfileobj
    def drift(*args,**kwargs):
        result=original(*args,**kwargs);f.systemd['active']=True;return result
    monkeypatch.setattr(shutil,'copyfileobj',drift)
    with pytest.raises(ValueError):invoke(f)
    assert database_rows(f.db)==before


@pytest.mark.parametrize('lock_name',['operation','runtime.lock','lp-preparation.lock'])
@pytest.mark.parametrize('controller_delay',[0,0.3])
def test_recovery_refuses_a_real_live_lock_owner(stopped_cloud,lock_name,controller_delay):
    import fcntl,threading,time
    f=stopped_cloud;before=database_rows(f.db)
    path=f.operation if lock_name=='operation' else f.db.parent/lock_name
    proc=subprocess.Popen([str(f.cfg.python),'-I','-u','-c',
        'import fcntl,sys; h=open(sys.argv[1],"r+"); fcntl.flock(h,fcntl.LOCK_EX); '
        'print("LOCKED",flush=True); sys.stdin.read(1)',str(path)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    ready=threading.Event();messages=[]
    def receive():messages.append(proc.stdout.readline());ready.set()
    reader=threading.Thread(target=receive,daemon=True);reader.start()
    try:
        assert ready.wait(5) and messages==['LOCKED\n']
        if controller_delay:time.sleep(controller_delay)  # Deliberate delayed controller, not synchronization.
        assert proc.poll() is None
        with pytest.raises((ValueError,BlockingIOError)):invoke(f)
        assert database_rows(f.db)==before and not list(f.backup.iterdir())
    finally:
        try:proc.communicate('\n',timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate();proc.communicate(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()


def test_backup_and_transaction_hold_all_owner_locks(stopped_cloud,monkeypatch):
    import fcntl,threading
    f=stopped_cloud;ready=threading.Event();release=threading.Event();result=[];errors=[]
    original=shutil.copyfileobj
    def held_backup(*a,**k):
        ready.set()
        assert release.wait(5)
        return original(*a,**k)
    monkeypatch.setattr(shutil,'copyfileobj',held_backup)
    def recover():
        try:result.append(invoke(f))
        except BaseException as error:errors.append(error)
    worker=threading.Thread(target=recover,daemon=True);worker.start()
    try:
        assert ready.wait(5)
        for path in [f.operation,f.db.parent/'runtime.lock',f.db.parent/'lp-preparation.lock']:
            with path.open('r+') as lock:
                with pytest.raises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert json.loads(database_rows(f.db)['lp_preparation'][0][2])['paused'] is True
    finally:
        release.set();worker.join(timeout=5)
    assert not worker.is_alive() and not errors
    assert result[0]['after']['generation']==2
    for path in [f.operation,f.db.parent/'runtime.lock',f.db.parent/'lp-preparation.lock']:
        with path.open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_transaction_failure_rolls_back_selected_items_and_retains_backup(stopped_cloud,capsys):
    f=stopped_cloud
    with sqlite3.connect(f.db) as con:
        con.execute('''INSERT INTO lp_preparation_items
            (condition_id,generation,retry_used,failure_count,state,paused,stage,error,updated_at)
            VALUES ('paused-one',1,1,2,'paused',1,'metadata','certificate','2026-10-03T14:49:00Z')''')
        con.execute("CREATE TRIGGER reject_recovery BEFORE UPDATE ON lp_preparation BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    con.close();before=database_rows(f.db)
    assert cloud.main(['recover-preparation','--config',str(f.config),'--expected-sha',f.cfg.expected_sha,
                       '--expected-generation','1','--backup-root',str(f.backup)])==2
    result=json.loads(capsys.readouterr().out)
    assert result['phase']=='transaction' and result['recovery_committed'] is False
    assert result['after']==result['before'] and result['recovered_item_count']==0
    assert database_rows(f.db)==before
    backup=Path(result['backup'])
    assert json.loads((backup/'manifest.json').read_text())['status']=='COMPLETE'
    assert database_rows(backup/'consistent.sqlite3')==before


def test_generation_changes_during_backup_are_not_overwritten(stopped_cloud,monkeypatch):
    f=stopped_cloud;original=os.fsync;changed=False
    def changed_cycle(fd):
        nonlocal changed
        result=original(fd)
        if not changed and os.fstat(fd).st_ino==f.backup.stat().st_ino:
            with sqlite3.connect(f.db) as con:con.execute('UPDATE lp_preparation SET generation=2')
            con.close();changed=True
        return result
    monkeypatch.setattr(os,'fsync',changed_cycle)
    with pytest.raises(cloud.PreparationRecoveryBlocked):invoke(f)
    state=json.loads(database_rows(f.db)['lp_preparation'][0][2])
    assert state['paused'] is True
    assert database_rows(f.db)['lp_preparation'][0][1]==2


@pytest.mark.parametrize('statement',["UPDATE lp_auto_pool SET payload='{}'",
                                     "UPDATE lp_preparation_items SET retry_used=1 WHERE paused=0"])
def test_recovery_denies_trigger_writes_outside_preparation(stopped_cloud,capsys,statement):
    f=stopped_cloud
    with sqlite3.connect(f.db) as con:
        con.execute('CREATE TRIGGER unrelated_write AFTER UPDATE ON lp_preparation BEGIN '+statement+'; END')
    con.close();before=database_rows(f.db)
    assert cloud.main(['recover-preparation','--config',str(f.config),'--expected-sha',f.cfg.expected_sha,
                       '--expected-generation','1','--backup-root',str(f.backup)])==2
    assert json.loads(capsys.readouterr().out)['recovery_committed'] is False
    assert database_rows(f.db)==before


def test_backup_whitelist_never_opens_credentials_or_unknown_runtime_files(stopped_cloud,monkeypatch):
    import builtins
    f=stopped_cloud
    credential=Path(f.cfg.credentials_file);credential.parent.mkdir(mode=0o700)
    credential.write_text('private fixture credential must never be read')
    extra=f.cfg.runtime_root/'config/unapproved-provider.json'
    extra.write_text('private fixture provider must never be copied')
    alias=f.cfg.runtime_root/'credentials';alias.symlink_to(credential.parent,target_is_directory=True)
    original_open=builtins.open;original_path_open=Path.open
    def reject_file(path):
        path=Path(path)
        if path==extra or path.resolve().is_relative_to(credential.parent):
            pytest.fail('attempted credential or unapproved runtime file read')
    def guarded_open(path,*a,**k):
        if isinstance(path,(str,os.PathLike)):reject_file(path)
        return original_open(path,*a,**k)
    def guarded_path_open(path,*a,**k):
        reject_file(path);return original_path_open(path,*a,**k)
    monkeypatch.setattr(builtins,'open',guarded_open)
    monkeypatch.setattr(Path,'open',guarded_path_open)
    result=invoke(f)
    backup=Path(result['backup'])
    assert not (backup/'runtime/config/unapproved-provider.json').exists()
    assert not (backup/'runtime/credentials').exists()
    assert not any('credential' in item['path'] or 'unapproved' in item['path']
                   for item in json.loads((backup/'manifest.json').read_text())['files'])


def test_recovery_rejects_a_credential_as_config_before_opening_it(stopped_cloud,monkeypatch):
    f=stopped_cloud;credential=Path(f.cfg.credentials_file)
    original=Path.open
    def never_open(path,*a,**k):
        if path==credential:pytest.fail('credential opened as configuration')
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'open',never_open)
    with pytest.raises(ValueError,match='official managed'):
        cloud.recover_stopped_preparation(credential,expected_sha=f.cfg.expected_sha,
            expected_generation=1,backup_root=f.backup)
    assert not list(f.backup.iterdir())


def root_sidecar_fault(f, monkeypatch):
    """Materialize the root-owned sidecar regression at the SQLite OS boundary."""
    original = sqlite3.connect
    class RootSidecars(sqlite3.Connection):
        def close(self):
            super().close()
            for suffix in ('-wal','-shm'):
                path=Path(str(f.db)+suffix)
                if path.exists():
                    os.chown(path,0,0);path.chmod(0o640)
    def connect(database,*a,**k):
        if str(database)==str(f.db) or str(database).startswith(f.db.as_uri()+'?'):
            k['factory']=RootSidecars
        return original(database,*a,**k)
    monkeypatch.setattr(sqlite3,'connect',connect)


@pytest.mark.parametrize('stopped_cloud',[True],indirect=True)
@pytest.mark.parametrize('reader_open',[False,True])
@pytest.mark.parametrize('outcome',['success','backup_failure','transaction_failure'])
def test_linux_root_recovery_leaves_service_usable_sidecars(stopped_cloud,monkeypatch,reader_open,outcome):
    f=stopped_cloud
    if outcome=='transaction_failure':
        with sqlite3.connect(f.db) as con:
            con.execute("CREATE TRIGGER abort_restore BEFORE UPDATE ON lp_preparation BEGIN SELECT RAISE(ABORT,'fixture'); END")
        con.close()
    reader=sqlite3.connect(f.db.as_uri()+'?mode=ro',uri=True) if reader_open else None
    try:
        if reader:
            reader.execute('BEGIN');reader.execute('SELECT * FROM lp_preparation').fetchall()
        root_sidecar_fault(f,monkeypatch)
        if outcome=='backup_failure':
            def disk_full(*a,**k):raise OSError('fixture full')
            monkeypatch.setattr(shutil,'copyfileobj',disk_full)
        if outcome=='success':result=invoke(f)
        else:
            with pytest.raises(cloud.PreparationRecoveryBlocked) as blocked:invoke(f)
            result=blocked.value.evidence
            assert result['recovery_committed'] is False
        for suffix in ('-wal','-shm'):
            path=Path(str(f.db)+suffix)
            if reader_open:assert path.exists()  # Existing read transaction keeps both files.
            if path.exists():
                st=path.stat()
                assert (st.st_uid,st.st_gid,st.st_mode & 0o777)==(f.service.pw_uid,f.service.pw_gid,0o600)
        def service_identity():
            os.setgroups([])
            os.setgid(f.service.pw_gid);os.setuid(f.service.pw_uid)
        probe=subprocess.run([str(f.cfg.python),'-I','-c',
            'import sqlite3,sys,json,os; c=sqlite3.connect(sys.argv[1]); '
            'c.execute("BEGIN IMMEDIATE"); print(json.dumps(dict(uid=os.getuid(),gid=os.getgid(),groups=os.getgroups(),'
            'generation=c.execute("SELECT generation FROM lp_preparation").fetchone()[0]))); c.rollback(); c.close()',
            str(f.db)],preexec_fn=service_identity,check=True,capture_output=True,text=True,timeout=5)
        probe_state=json.loads(probe.stdout)
        assert probe_state==dict(uid=f.service.pw_uid,gid=f.service.pw_gid,groups=[],
                                 generation=2 if outcome=='success' else 1)
        print(json.dumps(dict(native_linux=True,root_uid=os.getuid(),root_sidecar_fault=True,outcome=outcome,
                              concurrent_reader=reader_open,sidecars=result['sidecars'],service_probe=probe_state)))
    finally:
        if reader:reader.close()


@pytest.mark.parametrize('stopped_cloud',[True],indirect=True)
def test_linux_sidecar_cleanup_failure_is_blocked_after_truthful_commit(stopped_cloud,monkeypatch):
    f=stopped_cloud;reader=sqlite3.connect(f.db.as_uri()+'?mode=ro',uri=True)
    reader.execute('BEGIN');reader.execute('SELECT * FROM lp_preparation').fetchall()
    root_sidecar_fault(f,monkeypatch)
    try:
        def denied(*a):raise PermissionError('fixture ownership denied')
        monkeypatch.setattr(os,'fchown',denied)
        with pytest.raises(cloud.PreparationRecoveryBlocked) as blocked:invoke(f)
        evidence=blocked.value.evidence
        assert evidence['phase']=='storage_ownership' and evidence['recovery_committed'] is True
        assert evidence['after']['generation']==2 and Path(evidence['backup']).is_dir()
        assert not f.systemd['active']
    finally:
        reader.close()


def test_recovery_does_not_echo_unrecognized_preparation_fields(stopped_cloud):
    f=stopped_cloud
    with sqlite3.connect(f.db) as con:
        value={**f.state,'stage':'unapproved-sensitive-fixture-text'}
        con.execute('UPDATE lp_preparation SET payload=?',(json.dumps(value),))
    con.close()
    result=invoke(f)
    assert result['before']['stage']=='unknown'
    assert 'unapproved-sensitive' not in json.dumps(result)


def test_recovery_selects_paused_items_without_spending_waiting_retries(stopped_cloud):
    f=stopped_cloud
    with sqlite3.connect(f.db) as con:
        con.execute('''INSERT INTO lp_preparation_items
            (condition_id,generation,retry_used,failure_count,state,paused,stage,error,updated_at)
            VALUES ('paused-one',1,1,2,'paused',1,'metadata','certificate','2026-10-03T14:49:00Z')''')
    con.close();before=database_rows(f.db)['lp_preparation_items']
    result=invoke(f)
    assert result['recovered_item_count']==1
    after=database_rows(f.db)['lp_preparation_items']
    assert [row for row in after if row[0]!='paused-one']==[row for row in before if row[0]!='paused-one']
    with closing(sqlite3.connect(f.db)) as con:
        assert con.execute("SELECT state,paused,generation,retry_used FROM lp_preparation_items WHERE condition_id='paused-one'").fetchone()==('recovered',0,2,1)


@pytest.mark.parametrize('stopped_cloud',[False,True],indirect=True)
@pytest.mark.parametrize('control',['config','unit'])
def test_recovery_never_reads_a_hardlinked_control_credential(stopped_cloud,monkeypatch,control):
    f=stopped_cloud;path=f.config if control=='config' else cloud.UNIT_PATH
    secret=Path(f.cfg.credentials_file);secret.parent.mkdir(mode=0o700)
    path.rename(secret);os.link(secret,path)
    identity=secret.stat();original_open=Path.open;original_read=os.read;original_pread=os.pread
    def forbidden(st):
        if (st.st_dev,st.st_ino)==(identity.st_dev,identity.st_ino):
            pytest.fail('attempted hardlinked credential read')
    def guarded_open(path,*a,**k):
        if path.exists():forbidden(path.stat())
        return original_open(path,*a,**k)
    def guarded_read(fd,*a):forbidden(os.fstat(fd));return original_read(fd,*a)
    def guarded_pread(fd,*a):forbidden(os.fstat(fd));return original_pread(fd,*a)
    monkeypatch.setattr(Path,'open',guarded_open)
    monkeypatch.setattr(os,'read',guarded_read);monkeypatch.setattr(os,'pread',guarded_pread)
    with pytest.raises((ValueError,OSError)):invoke(f)
    assert not list(f.backup.iterdir())


@pytest.mark.parametrize('replacement_kind',['hardlink','regular'])
@pytest.mark.parametrize('control',['config','unit','runtime_config'])
def test_recovery_rejects_control_replacement_before_read_or_backup(stopped_cloud,monkeypatch,control,replacement_kind):
    f=stopped_cloud;path=f.config if control=='config' else cloud.UNIT_PATH if control=='unit' else f.cfg.runtime_root/'config/prediction_arbitrage.json'
    secret=Path(f.cfg.credentials_file);secret.parent.mkdir(mode=0o700);secret.write_text('private fixture must not be read');secret.chmod(0o600)
    original_open=os.open;original_path_open=Path.open;original_read=os.read;original_pread=os.pread
    swapped=False;identity=secret.stat();replacement_payload=path.read_bytes();replacement_mode=path.stat().st_mode & 0o777
    def replace(target):
        nonlocal swapped
        if Path(target)==path and not swapped:
            swapped=True
            path.rename(path.with_name(path.name+'.retained'))
            if replacement_kind=='hardlink':os.link(secret,path)
            else:path.write_bytes(replacement_payload);path.chmod(replacement_mode)
            swapped=True
    def forbidden(st):
        if (st.st_dev,st.st_ino)==(identity.st_dev,identity.st_ino):pytest.fail('replacement credential was read')
    def opened(target,*a,**k):replace(target);return original_open(target,*a,**k)
    def path_opened(target,*a,**k):
        replace(target)
        if target.exists():forbidden(target.stat())
        return original_path_open(target,*a,**k)
    def read(fd,*a):forbidden(os.fstat(fd));return original_read(fd,*a)
    def pread(fd,*a):forbidden(os.fstat(fd));return original_pread(fd,*a)
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(Path,'open',path_opened)
    monkeypatch.setattr(os,'read',read);monkeypatch.setattr(os,'pread',pread)
    with pytest.raises((ValueError,OSError)):invoke(f)
    assert swapped and not f.systemd['active']


@pytest.mark.parametrize('stopped_cloud',[False,True],indirect=True)
@pytest.mark.parametrize('replacement_owner',['root','service'])
@pytest.mark.parametrize('suffix',['-wal','-shm'])
def test_recovery_blocks_unknown_regular_sidecar_replacement_without_chown(stopped_cloud,monkeypatch,suffix,replacement_owner):
    f=stopped_cloud;reader=sqlite3.connect(f.db.as_uri()+'?mode=ro',uri=True)
    reader.execute('BEGIN');reader.execute('SELECT * FROM lp_preparation').fetchall()
    path=Path(str(f.db)+suffix);retained=path.with_name(path.name+'.retained')
    original=os.fsync;replaced=False;replacement_identity=None
    def replace(fd):
        nonlocal replaced,replacement_identity
        result=original(fd)
        if not replaced and os.fstat(fd).st_ino==f.backup.stat().st_ino:
            path.rename(retained);path.write_bytes(retained.read_bytes());path.chmod(0o640)
            if f.native:
                os.chown(path,0 if replacement_owner=='root' else f.service.pw_uid,
                         0 if replacement_owner=='root' else f.service.pw_gid)
            replacement_identity=path.stat();replaced=True
        return result
    monkeypatch.setattr(os,'fsync',replace)
    try:
        with pytest.raises(cloud.PreparationRecoveryBlocked) as blocked:invoke(f)
        assert replaced and blocked.value.evidence['phase']=='storage_ownership'
        actual=path.stat()
        assert (actual.st_ino,actual.st_uid,actual.st_gid,actual.st_mode & 0o777)==(
            replacement_identity.st_ino,replacement_identity.st_uid,replacement_identity.st_gid,0o640)
        assert not f.systemd['active']
    finally:
        if replaced:path.unlink();retained.rename(path)
        reader.close()


@pytest.mark.parametrize('control',['config','unit'])
@pytest.mark.parametrize('moment',['copy','after_backup'])
def test_recovery_fences_control_identity_through_backup(stopped_cloud,monkeypatch,control,moment):
    f=stopped_cloud;path=f.config if control=='config' else cloud.UNIT_PATH
    old=path.read_bytes();retained=path.with_name(path.name+'.retained');replaced=False
    original_copy=shutil.copyfileobj;original_sync=os.fsync
    def replace():
        nonlocal replaced
        if not replaced:
            path.rename(retained);path.write_bytes(old);path.chmod(retained.stat().st_mode & 0o777);replaced=True
    def copy(source,target,*a,**k):
        if moment=='copy' and os.fstat(source.fileno()).st_ino==retained_inode:replace()
        return original_copy(source,target,*a,**k)
    def sync(fd):
        result=original_sync(fd)
        if moment=='after_backup' and os.fstat(fd).st_ino==f.backup.stat().st_ino:replace()
        return result
    retained_inode=path.stat().st_ino
    monkeypatch.setattr(shutil,'copyfileobj',copy);monkeypatch.setattr(os,'fsync',sync)
    with pytest.raises(cloud.PreparationRecoveryBlocked) as blocked:invoke(f)
    evidence=blocked.value.evidence
    assert replaced and evidence['recovery_committed'] is False and Path(evidence['backup']).exists()
    assert json.loads(database_rows(f.db)['lp_preparation'][0][2])['paused'] is True
    assert not f.systemd['active']


@pytest.mark.parametrize('stopped_cloud',[False,True],indirect=True)
def test_recovery_allows_sqlite_sidecar_recreation_between_explicit_operations(stopped_cloud,monkeypatch):
    f=stopped_cloud;original_copy=shutil.copyfileobj
    with monkeypatch.context() as failure:
        def disk_full(*a,**k):raise OSError('fixture')
        failure.setattr(shutil,'copyfileobj',disk_full)
        with pytest.raises(cloud.PreparationRecoveryBlocked) as blocked:invoke(f)
        assert blocked.value.evidence['recovery_committed'] is False
    # A genuine last read/write SQLite close checkpoints and removes its own
    # sidecars between operations. No helper clears/recreates durable state.
    with closing(sqlite3.connect(f.db)) as connection:
        connection.execute('SELECT generation FROM lp_preparation').fetchall()
    assert all(not Path(str(f.db)+suffix).exists() for suffix in ('-wal','-shm'))
    result=invoke(f)
    assert result['after']['generation']==2 and result['recovered_item_count']==0
    assert len(database_rows(f.db)['lp_preparation_items'])==17445
