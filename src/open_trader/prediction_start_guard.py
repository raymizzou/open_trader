"""Single-use local D2 startup permission, independent of saved RUN intent.

Only supported explicit operator operations arm. A bare Runtime can only claim;
only its proven normal full drain returns its exact claim. This record contains
identity and startup permission, never current account or financial authority.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .prediction_release import (inspect_prediction_release_checkout, load_prediction_runtime_record,
    validate_lp_validation, write_prediction_runtime_record, lp_operation_descriptor, selected_lp_config, lp_launchd_operation_lock)

SCHEMA='open_trader.lp.start-permit.v1'
CAPABILITY_SCHEMA='open_trader.lp.runtime-contract.v1'
CAPABILITY_FIELDS=('current_api_authority','no_request_replay','start_permit','reliable_control','native_drain')


class PredictionStartDenied(RuntimeError):
    """Startup has no exact, unconsumed permission; process must drain and exit78."""


class PredictionStartSuperseded(PredictionStartDenied):
    """A trusted newer selection leaves the fully drained old claim consumed."""


@dataclass(frozen=True)
class StartIdentity:
    runtime_root: str
    data_dir: str
    code_root: str
    release_sha: str
    capability_digest: str
    manager: str
    manager_identity: str
    uid: int
    gid: int
    interpreter: str
    config: str
    config_digest: str
    host: str
    port: int


@dataclass(frozen=True)
class StartClaim:
    identity: StartIdentity
    operation_nonce: str
    claim_id: str
    process_epoch: str
    pid: int


def _canonical(path):
    value=Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (value,*value.parents)):
        raise PredictionStartDenied('startup path contains a symlink')
    return value.resolve(strict=True)


def _owned(path,mode=None,*,uid=None,gid=None):
    info=path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid!=(os.getuid() if uid is None else uid)
            or (gid is not None and info.st_gid!=gid)
            or info.st_mode & 0o022 or (mode is not None and stat.S_IMODE(info.st_mode)!=mode)):
        raise PredictionStartDenied('startup path ownership or permissions invalid')
    return info


def _installed_systemd_selects(root,data,config):
    """Recognize this root in the fixed trusted generated unit only.

    Unassociated host inspection is not an ordinary-manager prerequisite.
    A positive path association, including conflicting partial selections,
    always reserves this root even if the file changes during inspection.
    """
    from .prediction_cloud import UNIT_PATH,trusted_root_path
    try:
        unit=_canonical(UNIT_PATH);trusted_root_path(unit)
        before=unit.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:return False
        fd=os.open(unit,os.O_RDONLY|os.O_NOFOLLOW)
    except (OSError,ValueError,PredictionStartDenied):return False
    try:
        with os.fdopen(fd,'rb') as handle:
            opened=os.fstat(handle.fileno());contents=handle.read()
        # Only plain tokens emitted by render_unit are interpreted. No shell,
        # systemd expansion, unit inventory or unrelated service inference.
        section='';described=False;recognized=False;selections=[]
        for line in contents.decode('utf-8').splitlines():
            if line.startswith('['):section=line
            elif section=='[Unit]' and line=='Description=OpenTrader Prediction':described=True
            elif section=='[Service]' and line.startswith('ReadWritePaths='):
                selections.extend(line.removeprefix('ReadWritePaths=').split())
            elif section=='[Service]' and line.startswith('ExecStart='):
                tokens=line.removeprefix('ExecStart=').split()
                if tokens[1:5]!=['-m','open_trader','prediction-service','--mode']:continue
                recognized=True
                for index,token in enumerate(tokens[:-1]):
                    if token in ('--data-dir','--config'):selections.append(tokens[index+1])
        associated=(described or recognized) and any(value in (str(root),str(data),str(config))
            and os.path.isabs(value) and os.path.normpath(value)==value for value in selections)
        if associated:return True
        # A changed or unsupported unassociated unit proves no association;
        # it is deliberately not a new global admission gate.
        fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size',
            'st_mtime_ns','st_ctime_ns','st_mtime','st_ctime')
        if any(getattr(before,k)!=getattr(opened,k) for k in fields):return False
        after=unit.lstat()
        if any(getattr(before,k)!=getattr(after,k) for k in fields) or unit.read_bytes()!=contents:return False
        return False
    except (OSError,UnicodeError,ValueError):return False


def identity_for_runtime(*,data_dir,prediction_config_path,code_root=None,host='127.0.0.1',port=8769):
    """Derive identity from canonical source, actual config and managed selection."""
    try:
        code=_canonical(code_root or Path(__file__).resolve().parents[2])
        data=_canonical(data_dir);root=data.parent;config=_canonical(prediction_config_path)
        interpreter=sys.executable
        ordinary=root/'prediction-service-runtime.json';systemd=root/'prediction-systemd-release.json'
        if ordinary.is_symlink() or systemd.is_symlink() or (ordinary.exists() and systemd.exists()):
            raise PredictionStartDenied('ambiguous startup manager records')
        if systemd.exists():
            record_path=systemd
            # Check root authority before trusting any identity from JSON.
            info=record_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0
                    or stat.S_IMODE(info.st_mode)!=0o640):
                raise PredictionStartDenied('systemd management record metadata invalid')
            record=load_prediction_runtime_record(record_path)
            import pwd,grp
            service=record.get('service_identity')
            if (type(service) is not dict or set(service)!={'user','uid','gid'}
                    or type(service['user']) is not str or not service['user']
                    or type(service['uid']) is not int or service['uid']<=0
                    or type(service['gid']) is not int or service['gid']<0):
                raise PredictionStartDenied('systemd service identity invalid')
            account=pwd.getpwnam(service['user']);group=grp.getgrnam(service['user'])
            uid,gid=service['uid'],service['gid']
            if account.pw_uid!=uid or account.pw_gid!=gid or group.gr_gid!=gid or info.st_gid!=gid:
                raise PredictionStartDenied('systemd account mapping changed')
            from .prediction_cloud import UNIT_PATH,trusted_root_path
            unit=_canonical(UNIT_PATH);trusted_root_path(unit)
            unit_info=unit.lstat()
            interpreter=record.get('python')
            if (not stat.S_ISREG(unit_info.st_mode) or unit_info.st_nlink!=1
                    or record.get('unit_text')!=unit.read_text()
                    or record.get('manager')!='systemd' or record.get('manager_identity')!=str(unit)
                    or record.get('runtime_root')!=str(root) or record.get('config')!=str(config)
                    or type(interpreter) is not str or not Path(interpreter).is_absolute()
                    or os.path.normpath(interpreter)!=interpreter):
                raise PredictionStartDenied('systemd selected unit or runtime changed')
            expected_exec=(f'ExecStart={interpreter} -m open_trader prediction-service --mode production '
                f'--data-dir {data} --config {config} --host {host} --port {port} '
                f'--release-manifest {code}/ops/prediction-service-release.json')
            unit_lines=unit.read_text().splitlines()
            for prefix,value in (('User=',service['user']),('Group=',service['user']),
                    ('WorkingDirectory=',str(code)),('ExecStart=',expected_exec.removeprefix('ExecStart='))):
                if [line for line in unit_lines if line.startswith(prefix)]!=[prefix+value]:
                    raise PredictionStartDenied('systemd unit identity changed')
            if config!=root/'config/prediction_arbitrage.json':raise PredictionStartDenied('systemd config layout invalid')
            for directory in (root,data,config.parent):_owned(directory,0o700,uid=uid,gid=gid)
            _owned(config,0o600,uid=uid,gid=gid)
            trusted_root_path(code)
            manager='systemd';manager_identity=str(unit)
        else:
            if _installed_systemd_selects(root,data,config):
                raise PredictionStartDenied('installed systemd runtime is missing its management record')
            uid,gid=os.getuid(),os.getgid()
            _owned(root);_owned(data);_owned(config,0o600)
            record_path=ordinary;record=load_prediction_runtime_record(record_path)
            if record is None:raise PredictionStartDenied('trusted LP validation is missing')
            info=_owned(record_path,0o600)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PredictionStartDenied('startup manager record metadata invalid')
            manager=record.get('manager')
            if manager not in ('foreground','launchd'):raise PredictionStartDenied('unsupported startup manager')
            manager_identity=str(record.get('manager_identity') or (record.get('label') if manager=='launchd' else f'foreground:{uid}'))
            if not manager_identity or manager_identity=='None':raise PredictionStartDenied('startup manager identity missing')
        if data.name!='data' or host!='127.0.0.1' or type(port) is not int or not 0<port<65536:
            raise PredictionStartDenied('startup runtime layout invalid')
        release=inspect_prediction_release_checkout(code)
        capability=code/'ops/lp-runtime-contract.json'
        payload=json.loads(capability.read_text())
        if (type(payload) is not dict or set(payload)!={'schema_version',*CAPABILITY_FIELDS}
                or payload['schema_version']!=CAPABILITY_SCHEMA
                or any(type(payload[k]) is not int or payload[k]!=1 for k in CAPABILITY_FIELDS)):
            raise PredictionStartDenied('LP capability is incompatible')
        committed=subprocess.check_output(['git','-C',str(code),'show','HEAD:ops/lp-runtime-contract.json'])
        if committed!=capability.read_bytes():raise PredictionStartDenied('LP capability is not exact tracked source')
        digest=hashlib.sha256(committed).hexdigest()
        candidate=record.get('candidate') or {}
        pending=record.get('pending_lp_validation')
        binding=pending if pending is not None else candidate.get('lp_validation')
        validate_lp_validation(binding,release_root=code,runtime_root=root,python=interpreter,
            config_path=config,config_uid=uid,config_gid=gid if manager=='systemd' else None)
        selected=dict(checkout=binding['release_root'],git_sha=binding['source_sha']) if pending is not None else candidate
        if selected.get('checkout')!=str(code) or selected.get('git_sha')!=release['git_sha']:
            raise PredictionStartDenied('startup release selection changed')
        return StartIdentity(str(root),str(data),str(code),release['git_sha'],digest,manager,
            manager_identity,uid,gid,interpreter,str(config),binding['config_selection']['sha256'],host,port)
    except PredictionStartDenied:raise
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        raise PredictionStartDenied('startup identity unavailable') from exc


def selected_trading_config(identity):
    with selected_lp_config(identity.config,uid=identity.uid,
            gid=identity.gid if identity.manager=='systemd' else None) as (configuration,selection,recheck):
        if selection['sha256']!=identity.config_digest:raise PredictionStartDenied('startup selected config changed')
        recheck()
        return configuration


def _validate_identity(identity):
    if type(identity) is not StartIdentity:raise PredictionStartDenied('startup identity invalid')
    actual=identity_for_runtime(data_dir=identity.data_dir,prediction_config_path=identity.config,
        code_root=identity.code_root,host=identity.host,port=identity.port)
    if actual!=identity:raise PredictionStartDenied('startup identity changed')


def _directory(identity,*,create=False):
    path=Path(identity.runtime_root)/'lp-start-guard'
    if create and not path.exists() and not path.is_symlink():
        _new_service_directory(identity,path)
    info=_owned(path,0o700,uid=identity.uid,gid=identity.gid)
    if not stat.S_ISDIR(info.st_mode):raise PredictionStartDenied('startup guard is not a directory')
    return path


def _new_service_directory(identity,path):
    path.mkdir(mode=0o700)
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(descriptor);named=path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o700
                or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino)):
            raise PredictionStartDenied('new startup directory identity changed')
        if identity.manager=='systemd':os.fchown(descriptor,identity.uid,identity.gid)
        os.fsync(descriptor)
    finally:os.close(descriptor)


def _read(identity):
    directory=_directory(identity);path=directory/'permit.json'
    info=_owned(path,0o600,uid=identity.uid,gid=identity.gid)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PredictionStartDenied('startup permit is not a private regular file')
    value=json.loads(path.read_text())
    keys={'schema_version','version','state','identity','operation_nonce','claim_id','process_epoch','pid'}
    if (type(value) is not dict or set(value)!=keys or value['schema_version']!=SCHEMA
            or type(value['version']) is not int or value['version']!=1 or value['state'] not in ('ARMED','CLAIMED')
            or value['identity']!=asdict(identity) or type(value['operation_nonce']) is not str or not value['operation_nonce']):
        raise PredictionStartDenied('startup permit invalid or wrong target')
    if value['state']=='ARMED':
        if any(value[k] is not None for k in ('claim_id','process_epoch','pid')):raise PredictionStartDenied('armed permit has a claim')
    elif (type(value['claim_id']) is not str or not value['claim_id'] or type(value['process_epoch']) is not str
            or not value['process_epoch'] or type(value['pid']) is not int or value['pid']<1):
        raise PredictionStartDenied('startup claim invalid')
    return value


def _write(identity,record):
    directory=_directory(identity,create=True);path=directory/'permit.json';temporary=None
    if path.exists() or path.is_symlink():
        info=_owned(path,0o600,uid=identity.uid,gid=identity.gid)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PredictionStartDenied('startup permit target invalid')
    try:
        with NamedTemporaryFile('w',dir=directory,prefix='.permit-',delete=False) as handle:
            temporary=Path(handle.name)
            if identity.manager=='systemd':os.fchown(handle.fileno(),identity.uid,identity.gid)
            os.fchmod(handle.fileno(),0o600)
            json.dump(record,handle,sort_keys=True);handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path);temporary=None
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


@contextmanager
def _operation(identity,descriptor=None):
    if identity.manager=='systemd':
        from .prediction_cloud import OPERATION_LOCK
        if os.geteuid()!=0 or descriptor is None:raise PredictionStartDenied('systemd arm requires root operation descriptor')
        path=OPERATION_LOCK;uid=0
    elif identity.manager=='launchd':
        if descriptor is None:raise PredictionStartDenied('launchd arm requires existing operation descriptor')
        path=lp_launchd_operation_lock(identity.manager_identity,uid=identity.uid);uid=identity.uid
    else:
        path=Path(identity.runtime_root)/'prediction-service-operation.lock';uid=identity.uid
    try:
        with lp_operation_descriptor(path,owner_uid=uid,descriptor=descriptor):yield
    except (OSError,ValueError) as exc:raise PredictionStartDenied('startup operation unavailable') from exc


def _record_path(identity):
    return Path(identity.runtime_root)/('prediction-systemd-release.json' if identity.manager=='systemd' else 'prediction-service-runtime.json')


def _service_actor(identity):
    if os.geteuid()!=identity.uid or os.getegid()!=identity.gid or sys.executable!=identity.interpreter:
        raise PredictionStartDenied('startup actor is not the configured service identity')


def _runtime_lock(identity):return Path(identity.data_dir)/'prediction_arbitrage/runtime.lock'


def _held_by_self(identity):
    """Require observed actual OS lock ownership, not a supplied owner boolean."""
    path=_runtime_lock(identity);info=path.stat()
    if sys.platform.startswith('linux'):
        for line in Path('/proc/locks').read_text().splitlines():
            fields=line.split()
            if len(fields)>5 and fields[1]=='FLOCK' and fields[3]=='WRITE':
                major,minor,inode=fields[5].split(':')
                if (int(fields[4])==os.getpid() and int(inode)==info.st_ino
                        and int(major,16)==os.major(info.st_dev) and int(minor,16)==os.minor(info.st_dev)):
                    return True
        return False
    # Darwin lsof does not expose BSD flock as a POSIX record-lock marker.
    # Observe the sole process with this exact file open, then independently
    # prove an actual exclusive flock exists while Runtime retains its handle.
    result=subprocess.run(['/usr/sbin/lsof','-Fpn','--',str(path)],capture_output=True,text=True,timeout=5)
    if result.returncode!=0:return False
    pids={int(line[1:]) for line in result.stdout.splitlines() if line.startswith('p')}
    if pids!={os.getpid()}:return False
    descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        else:return False
    finally:os.close(descriptor)


def arm_for_operator_start(identity,operation_nonce,*,operation_fd=None):
    """Explicit supported operator operation; keep actual owner exclusion through arm."""
    _validate_identity(identity)
    if type(operation_nonce) is not str or not operation_nonce:raise PredictionStartDenied('operation nonce required')
    with _operation(identity,operation_fd):
        if identity.manager=='systemd':
            from .prediction_cloud import unit_state
            try:state=unit_state()
            except (OSError,ValueError,subprocess.SubprocessError) as exc:
                raise PredictionStartDenied('systemd old process absence unavailable') from exc
            if state.get('MainPID')!='0' or state.get('ActiveState') not in ('inactive','failed'):
                raise PredictionStartDenied('systemd old process remains or is unknown')
        lock=_runtime_lock(identity)
        if not lock.parent.exists():
            _new_service_directory(identity,lock.parent)
        _owned(lock.parent,uid=identity.uid,gid=identity.gid)
        try:
            descriptor=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            new=True
        except FileExistsError:
            descriptor=os.open(lock,os.O_RDWR|os.O_NOFOLLOW);new=False
        try:
            opened=os.fstat(descriptor);named=lock.lstat()
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1
                    or opened.st_uid!=(os.geteuid() if new else identity.uid)
                    or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)):
                raise PredictionStartDenied('runtime owner file identity invalid')
            if new and identity.manager=='systemd':os.fchown(descriptor,identity.uid,identity.gid)
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            info=_owned(lock,uid=identity.uid,gid=identity.gid)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PredictionStartDenied('runtime owner file invalid')
            record=load_prediction_runtime_record(_record_path(identity))
            permit=Path(identity.runtime_root)/'lp-start-guard/permit.json'
            pids=[]
            if permit.exists() or permit.is_symlink():
                info=_owned(permit,0o600,uid=identity.uid,gid=identity.gid)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PredictionStartDenied('old permit metadata invalid')
                old=json.loads(permit.read_text())
                if not isinstance(old,dict) or old.get('schema_version')!=SCHEMA or old.get('state') not in ('ARMED','CLAIMED'):
                    raise PredictionStartDenied('old permit identity unknown')
                if old['state']=='CLAIMED':pids.append(old.get('pid'))
            if record:
                pid=(record.get('ready') or record.get('observed_ready') or {}).get('pid')
                if pid is not None:pids.append(pid)
            for pid in pids:
                if type(pid) is not int or pid<1:raise PredictionStartDenied('old PID unknown')
                try:os.kill(pid,0)
                except ProcessLookupError:pass
                else:raise PredictionStartDenied('old PID remains alive')
            with socket.socket() as probe:
                probe.settimeout(1)
                result=probe.connect_ex((identity.host,identity.port))
                if result!=errno.ECONNREFUSED:raise PredictionStartDenied('listener absence unproved')
            _validate_identity(identity)
            pending=record.get('pending_lp_validation')
            binding=pending if pending is not None else (record.get('candidate') or {}).get('lp_validation')
            if not isinstance(binding,dict) or binding.get('operation_nonce')!=operation_nonce:
                raise PredictionStartDenied('LP operation selection changed')
            if pending is not None:
                candidate=inspect_prediction_release_checkout(Path(identity.code_root))
                previous=record.get('previous_release')
                old=record.get('candidate')
                if isinstance(old,dict) and any(old.get(k)!=candidate.get(k) for k in ('checkout','git_sha')):
                    previous=old
                promoted={**record,'candidate':{**candidate,'lp_validation':pending},'previous_release':previous}
                promoted.pop('pending_lp_validation')
                write_prediction_runtime_record(_record_path(identity),promoted,
                    **(dict(owner=(0,identity.gid),mode=0o640) if identity.manager=='systemd' else {}))
            _write(identity,dict(schema_version=SCHEMA,version=1,state='ARMED',identity=asdict(identity),
                operation_nonce=operation_nonce,claim_id=None,process_epoch=None,pid=None))
        except BlockingIOError as exc:raise PredictionStartDenied('runtime owner remains active') from exc
        finally:os.close(descriptor)


def claim_for_start(identity,process_epoch):
    _validate_identity(identity)
    _service_actor(identity)
    try:
        if not _held_by_self(identity):raise PredictionStartDenied('startup claim requires actual owner')
        if type(process_epoch) is not str or not process_epoch:raise PredictionStartDenied('process epoch required')
        record=_read(identity)
        management=load_prediction_runtime_record(_record_path(identity))
        binding=(management.get('candidate') or {}).get('lp_validation')
        if (management.get('pending_lp_validation') is not None or not isinstance(binding,dict)
                or binding.get('operation_nonce')!=record['operation_nonce']):
            raise PredictionStartDenied('startup permit operation no longer selected')
        if record['state']!='ARMED':raise PredictionStartDenied('startup permission already claimed')
        claim=StartClaim(identity,record['operation_nonce'],uuid4().hex,process_epoch,os.getpid())
        _write(identity,dict(record,state='CLAIMED',claim_id=claim.claim_id,process_epoch=process_epoch,pid=claim.pid))
        return claim
    except PredictionStartDenied:raise
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:raise PredictionStartDenied('startup permission unavailable') from exc


def _foreground_process(identity,pid):
    """Observe the one known foreground process, listener and native owner."""
    import shlex
    command=['/bin/ps','-p',str(pid),'-ww','-o','uid=,gid=,lstart=,command=']
    observed=subprocess.run(command,capture_output=True,text=True,timeout=5,check=True)
    values=observed.stdout.strip().split(None,7)
    if len(values)!=8 or (int(values[0]),int(values[1]))!=(identity.uid,identity.gid):
        raise PredictionStartDenied('foreground process account unavailable')
    arguments=shlex.split(values[7])
    if arguments[:4]!=[identity.interpreter,'-m','open_trader','prediction-service']:
        raise PredictionStartDenied('foreground process command changed')
    options=arguments[4:]
    if len(options)%2:raise PredictionStartDenied('foreground process arguments unavailable')
    pairs=list(zip(options[::2],options[1::2]));selected=dict(pairs)
    required={'--mode':'production','--data-dir':identity.data_dir,'--config':identity.config,
        '--host':identity.host,'--port':str(identity.port),
        '--release-manifest':str(Path(identity.code_root)/'ops/prediction-service-release.json')}
    if (len(selected)!=len(pairs) or set(selected)-set(required)-{'--notifier-config'}
            or any(selected.get(k)!=v for k,v in required.items())):
        raise PredictionStartDenied('foreground process arguments changed')
    owner=_runtime_lock(identity);info=_owned(owner,uid=identity.uid,gid=identity.gid)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise PredictionStartDenied('foreground native owner metadata changed')
    if sys.platform.startswith('linux'):
        process=Path('/proc')/str(pid)
        cwd=str((process/'cwd').resolve(strict=True))
        owners=set()
        for line in Path('/proc/locks').read_text().splitlines():
            fields=line.split()
            if len(fields)>5 and fields[1]=='FLOCK' and fields[3]=='WRITE':
                major,minor,inode=fields[5].split(':')
                if (int(inode),int(major,16),int(minor,16))==(info.st_ino,os.major(info.st_dev),os.minor(info.st_dev)):
                    owners.add(int(fields[4]))
        listeners=[]
        for name in ('tcp','tcp6'):
            for line in (Path('/proc/net')/name).read_text().splitlines()[1:]:
                fields=line.split();address,port=fields[1].split(':')
                if fields[3]=='0A' and int(port,16)==identity.port:
                    listeners.append((name,address,fields[9]))
        sockets=set()
        for path in (process/'fd').iterdir():
            try:sockets.add(os.readlink(path))
            except FileNotFoundError:continue
        listener=(len(listeners)==1 and listeners[0][:2]==('tcp','0100007F')
            and 'socket:['+listeners[0][2]+']' in sockets)
    else:
        def lsof(*args):
            return subprocess.run(['/usr/sbin/lsof',*args],capture_output=True,text=True,timeout=5,check=True).stdout.splitlines()
        directories=[line[1:] for line in lsof('-a','-p',str(pid),'-d','cwd','-Fn') if line.startswith('n')]
        cwd=directories[0] if len(directories)==1 else None
        owners={int(line[1:]) for line in lsof('-Fpn','--',str(owner)) if line.startswith('p')}
        rows=lsof('-nP','-iTCP:'+str(identity.port),'-sTCP:LISTEN','-Fpn')
        pids={int(line[1:]) for line in rows if line.startswith('p')}
        addresses=[line[1:] for line in rows if line.startswith('n')]
        listener=pids=={pid} and addresses==[identity.host+':'+str(identity.port)]
    if cwd!=identity.code_root or owners!={pid} or not listener:
        raise PredictionStartDenied('foreground process listener or native ownership changed')
    descriptor=os.open(owner,os.O_RDWR|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise PredictionStartDenied('foreground native owner is not held')
    finally:os.close(descriptor)
    from .prediction_cloud import read_json
    health=read_json(identity.port)
    required_health=dict(schema_version='open_trader.prediction_service.health.v1',module='prediction_service',
        pid=pid,cwd=identity.code_root,code_root=str(Path(identity.code_root)/'src'),
        git_sha=identity.release_sha,source_state='clean',status='running',mode='production',
        production_owner=True,mutations='enabled')
    if any(health.get(k)!=v for k,v in required_health.items()) or not health.get('started_at'):
        raise PredictionStartDenied('foreground current health identity unavailable')
    return tuple(values[:7]),cwd,tuple(arguments),health['started_at']


def _prepare_foreground_operator(*,data_dir,config,host,port,restart):
    """Explicit CLI operation; use the existing lock/producer and real drain."""
    import signal,time
    from .prediction_release import prepare_lp_target
    try:
        data=_canonical(data_dir);root=data.parent;code=Path(__file__).resolve().parents[2]
        config=_canonical(config)
        if host!='127.0.0.1' or type(port) is not int or not 0<port<65536:
            raise PredictionStartDenied('foreground listener invalid')
        if (root/'prediction-systemd-release.json').exists() or _installed_systemd_selects(root,data,config):
            raise PredictionStartDenied('foreground operation cannot replace systemd management')
        _owned(root);record_path=root/'prediction-service-runtime.json'
        with lp_operation_descriptor(root/'prediction-service-operation.lock',owner_uid=os.getuid()) as operation:
            management=load_prediction_runtime_record(record_path)
            if management is not None:
                _owned(record_path,0o600)
                if management.get('manager')!='foreground':
                    raise PredictionStartDenied('foreground operation cannot replace another manager')
            old=None;old_identity=None;facts=None
            owner=data/'prediction_arbitrage/runtime.lock'
            if owner.exists():
                descriptor=os.open(owner,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        permit=root/'lp-start-guard/permit.json';_owned(permit,0o600)
                        value=json.loads(permit.read_text());old_identity=StartIdentity(**value['identity'])
                        if (old_identity.manager!='foreground' or old_identity.runtime_root!=str(root)
                                or old_identity.data_dir!=str(data)
                                or (old_identity.uid,old_identity.gid)!=(os.getuid(),os.getgid())):
                            raise PredictionStartDenied('foreground original claim identity changed')
                        old=_read(old_identity)
                        if old['state']!='CLAIMED' or old['pid']==os.getpid():
                            raise PredictionStartDenied('foreground original process claim unavailable')
                        if management is None:
                            raise PredictionStartDenied('foreground original management record unavailable')
                        binding=(management.get('candidate') or {}).get('lp_validation')
                        validate_lp_validation(binding,release_root=old_identity.code_root,runtime_root=root,
                            python=old_identity.interpreter,verify_config=False)
                        if (binding['operation_nonce']!=old['operation_nonce']
                                or binding['source_sha']!=old_identity.release_sha
                                or binding['capability_digest']!=old_identity.capability_digest
                                or binding['config_selection']!=dict(path=old_identity.config,sha256=old_identity.config_digest)):
                            raise PredictionStartDenied('foreground original retained selection changed')
                finally:os.close(descriptor)
            if old is not None:
                facts=_foreground_process(old_identity,old['pid'])
                if not restart:
                    identity=identity_for_runtime(data_dir=data,prediction_config_path=config,code_root=code,host=host,port=port)
                    if identity!=old_identity or _read(old_identity)!=old:
                        raise PredictionStartDenied('foreground healthy start selection changed')
                    if _foreground_process(old_identity,old['pid'])!=facts:
                        raise PredictionStartDenied('foreground healthy owner changed')
                    return None
            nonce=uuid4().hex
            prepare_lp_target(runtime_root=root,release_root=code,python=sys.executable,config_path=config,
                operation_nonce=nonce,host=host,port=port,operation_fd=operation)
            identity=identity_for_runtime(data_dir=data,prediction_config_path=config,code_root=code,host=host,port=port)
            if identity.manager!='foreground':raise PredictionStartDenied('foreground manager changed')
            if old is not None:
                if _read(old_identity)!=old or _foreground_process(old_identity,old['pid'])!=facts:
                    raise PredictionStartDenied('foreground old owner changed before stop')
                _validate_identity(identity)
                os.kill(old['pid'],signal.SIGTERM)
                deadline=time.monotonic()+100
                while True:
                    try:os.kill(old['pid'],0)
                    except ProcessLookupError:break
                    if time.monotonic()>=deadline:
                        raise PredictionStartDenied('foreground old process still draining')
                    time.sleep(.01)
            arm_for_operator_start(identity,nonce,operation_fd=operation)
            return identity
    except PredictionStartDenied:raise
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        raise PredictionStartDenied('foreground operator start unavailable') from exc


def _selected_operation_supersedes(claim):
    """Validate the selected target itself, without adopting its startup identity."""
    identity=claim.identity
    root=_canonical(identity.runtime_root)
    if _canonical(identity.data_dir)!=root/'data':raise PredictionStartDenied('normal drain runtime changed')
    for directory in (root,Path(identity.data_dir)):
        _owned(directory,0o700 if identity.manager=='systemd' else None,
            uid=identity.uid,gid=identity.gid if identity.manager=='systemd' else None)
    path=_record_path(identity)
    other=root/('prediction-service-runtime.json' if identity.manager=='systemd' else 'prediction-systemd-release.json')
    if other.exists() or other.is_symlink():raise PredictionStartDenied('normal drain manager is ambiguous')
    info=_owned(path,0o640 if identity.manager=='systemd' else 0o600,
        uid=0 if identity.manager=='systemd' else identity.uid,
        gid=identity.gid if identity.manager=='systemd' else None)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise PredictionStartDenied('normal drain management record invalid')
    original=path.read_bytes()
    management=load_prediction_runtime_record(path)
    if management.get('manager')!=identity.manager or management.get('manager_identity')!=identity.manager_identity:
        raise PredictionStartDenied('normal drain manager selection changed')
    if identity.manager=='systemd':
        import pwd,grp
        from .prediction_cloud import UNIT_PATH,trusted_root_path
        service=management.get('service_identity')
        if (type(service) is not dict or set(service)!={'user','uid','gid'}
                or type(service['user']) is not str or not service['user']
                or type(service['uid']) is not int or type(service['gid']) is not int
                or service['uid']!=identity.uid or service['gid']!=identity.gid):
            raise PredictionStartDenied('normal drain service selection changed')
        account=pwd.getpwnam(service['user']);group=grp.getgrnam(service['user'])
        if (account.pw_uid!=identity.uid or account.pw_gid!=identity.gid or group.gr_gid!=identity.gid
                or str(_canonical(UNIT_PATH))!=identity.manager_identity
                or management.get('runtime_root')!=str(root)
                or management.get('config')!=str(root/'config/prediction_arbitrage.json')):
            raise PredictionStartDenied('normal drain systemd mapping changed')
        trusted_root_path(_canonical(UNIT_PATH))
    pending=management.get('pending_lp_validation')
    candidate=management.get('candidate') or {}
    binding=pending if pending is not None else candidate.get('lp_validation')
    if type(binding) is not dict:raise PredictionStartDenied('normal drain selected validation missing')
    target=_canonical(binding['release_root'])
    config=_canonical(binding['config_selection']['path'])
    if identity.manager=='systemd':
        if config!=root/'config/prediction_arbitrage.json':raise PredictionStartDenied('normal drain systemd config changed')
        trusted_root_path(target)
    validate_lp_validation(binding,release_root=target,runtime_root=root,python=binding['python'],
        config_path=config,config_uid=identity.uid,config_gid=identity.gid if identity.manager=='systemd' else None)
    if pending is None and (candidate.get('checkout')!=str(target) or candidate.get('git_sha')!=binding['source_sha']):
        raise PredictionStartDenied('normal drain selected candidate changed')
    # A concurrent replacement is uncertainty, never proof of supersession.
    observed=path.lstat()
    fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns','st_mtime','st_ctime')
    if any(getattr(observed,k)!=getattr(info,k) for k in fields) or path.read_bytes()!=original:
        raise PredictionStartDenied('normal drain selection changed during validation')
    return (binding['operation_nonce']!=claim.operation_nonce
        or (str(target),binding['source_sha'],binding['capability_digest'],binding['python'],str(config),binding['config_selection']['sha256'])
        !=(identity.code_root,identity.release_sha,identity.capability_digest,identity.interpreter,identity.config,identity.config_digest))


def rearm_after_clean_drain(claim):
    """Return this exact claim, or prove it superseded; unknown failures retain owner."""
    if type(claim) is not StartClaim or claim.pid!=os.getpid():raise PredictionStartDenied('normal drain claim invalid')
    _service_actor(claim.identity)
    if not _held_by_self(claim.identity):raise PredictionStartDenied('normal drain lost actual owner')
    record=_read(claim.identity)
    if (record['state']!='CLAIMED' or any(record[k]!=getattr(claim,k) for k in ('operation_nonce','claim_id','process_epoch','pid'))):
        raise PredictionStartDenied('normal drain claim changed')
    superseded=_selected_operation_supersedes(claim)
    if _read(claim.identity)!=record:raise PredictionStartDenied('normal drain claim changed during validation')
    if superseded:
        raise PredictionStartSuperseded('fully drained startup claim superseded by trusted selected operation')
    _validate_identity(claim.identity)
    if _read(claim.identity)!=record:raise PredictionStartDenied('normal drain claim changed before rearm')
    _write(claim.identity,dict(record,state='ARMED',claim_id=None,process_epoch=None,pid=None))


def verify_launchd_self_context(identity):
    """Verify this loaded launchd process early, without health/READY evidence.

    Only a successful observation while the actual Runtime owner is held may
    be cached for the later fatal exit mapping. Unknown context keeps exit78.
    """
    import plistlib,shlex
    try:
        if sys.platform!='darwin' or identity.manager!='launchd':return False
        _validate_identity(identity)
        if not _held_by_self(identity) or str(Path.cwd().resolve())!=identity.code_root:return False
        plist=_canonical(identity.manager_identity);_owned(plist)
        payload=plistlib.loads(plist.read_bytes())
        if (type(payload) is not dict or payload.get('Label')!='com.open-trader.prediction-service'
                or payload.get('WorkingDirectory')!=identity.code_root
                or payload.get('RunAtLoad') is not True
                or payload.get('KeepAlive')!={'SuccessfulExit':False}
                or type(payload.get('ExitTimeOut')) is not int or payload['ExitTimeOut']!=0
                or payload.get('EnvironmentVariables',{}).get('PYTHONPATH')!=str(Path(identity.code_root)/'src')):
            return False
        args=payload.get('ProgramArguments')
        if type(args) is not list or any(type(a) is not str for a in args):return False
        if args[:4]!=[identity.interpreter,'-m','open_trader','prediction-service']:return False
        record=load_prediction_runtime_record(Path(identity.runtime_root)/'prediction-service-runtime.json')
        expected={'--mode':'production','--data-dir':identity.data_dir,'--config':identity.config,
            '--host':identity.host,'--port':str(identity.port),
            '--release-manifest':record['candidate']['manifest']}
        flags={}
        for i in range(4,len(args),2):
            if i+1>=len(args) or args[i] in flags:return False
            flags[args[i]]=args[i+1]
        if any(flags.get(k)!=v for k,v in expected.items()) or set(flags)-set(expected)-{'--notifier-config'}:return False
        if '--notifier-config' in flags:
            notifier=_canonical(flags['--notifier-config']);_owned(notifier,0o600)
            if not notifier.is_relative_to(Path(identity.runtime_root)):return False
        job=f'gui/{identity.uid}/{payload["Label"]}'
        loaded=subprocess.run(['/bin/launchctl','print',job],capture_output=True,text=True,timeout=5)
        if loaded.returncode:return False
        facts={};loaded_args=None;inside=False
        for line in loaded.stdout.splitlines():
            value=line.strip()
            if value=='arguments = {':
                if loaded_args is not None:return False
                loaded_args=[];inside=True;continue
            if inside:
                if value=='}':inside=False
                elif value:loaded_args.append(value.strip('"'))
            elif ' = ' in value:
                key,item=value.split(' = ',1)
                if key in ('path','working directory','pid'):
                    if key in facts:return False
                    facts[key]=item.strip('"')
        if (inside or facts.get('path')!=str(plist) or facts.get('working directory')!=identity.code_root
                or facts.get('pid')!=str(os.getpid()) or loaded_args!=args):return False
        observed=subprocess.run(['/bin/ps','-p',str(os.getpid()),'-o','pid=,uid=,command='],
            capture_output=True,text=True,timeout=5)
        process=observed.stdout.strip().split(None,2)
        if (observed.returncode or len(process)!=3 or process[:2]!=[str(os.getpid()),str(identity.uid)]
                or shlex.split(process[2])!=args):return False
        cwd=subprocess.run(['/usr/sbin/lsof','-a','-p',str(os.getpid()),'-d','cwd','-Fn'],
            capture_output=True,text=True,timeout=5)
        paths=[line[1:] for line in cwd.stdout.splitlines() if line.startswith('n')]
        return cwd.returncode==0 and paths==[identity.code_root] and _held_by_self(identity)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        return False
