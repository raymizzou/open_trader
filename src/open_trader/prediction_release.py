from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager,ExitStack
from datetime import datetime
import json
import os
import fcntl
import stat
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from typing import Mapping


RELEASE_SCHEMA = "open_trader.prediction_service.release.v1"
RUNTIME_SCHEMA = "open_trader.prediction_service.runtime.v1"
RUNTIME_STATES = {"maintenance", "ready", "failed", "stopped"}
PREFLIGHT_SCHEMA = "open_trader.prediction_service.preflight.v1"


@dataclass(frozen=True)
class PredictionReleaseManifest:
    schema_version: str
    reader_generation: int
    contract_generation: int


def load_prediction_release_manifest(path: Path) -> PredictionReleaseManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction release manifest is unreadable: {path}") from exc
    required = {"schema_version", "reader_generation", "contract_generation"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("prediction release manifest has invalid keys")
    if payload["schema_version"] != RELEASE_SCHEMA:
        raise ValueError("prediction release manifest has invalid schema")
    for key in ("reader_generation", "contract_generation"):
        if type(payload[key]) is not int or payload[key] < 1:
            raise ValueError(f"prediction release manifest has invalid {key}")
    return PredictionReleaseManifest(
        schema_version=RELEASE_SCHEMA,
        reader_generation=payload["reader_generation"],
        contract_generation=payload["contract_generation"],
    )


def load_prediction_runtime_record(path: Path) -> dict[str, object] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction runtime record is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RUNTIME_SCHEMA:
        raise ValueError("prediction runtime record has invalid schema")
    if not isinstance(payload.get("state"), str) or payload["state"] not in RUNTIME_STATES:
        raise ValueError("prediction runtime record has invalid state")
    return payload


def write_prediction_runtime_record(
    path: Path, payload: Mapping[str, object], *, owner: tuple[int, int] | None = None,
    mode: int = 0o600, migrate_root_private: bool = False,
) -> None:
    state = payload.get("state")
    if not isinstance(state, str) or state not in RUNTIME_STATES:
        raise ValueError("prediction runtime state is invalid")
    record = {**dict(payload), "schema_version": RUNTIME_SCHEMA}
    path = Path(path)
    if owner is not None:
        if (type(owner) is not tuple or len(owner)!=2 or any(type(v) is not int or v<0 for v in owner)
                or mode not in (0o600,0o640)):
            raise ValueError('invalid runtime record publication identity')
        if path.exists() or path.is_symlink():
            info=path.lstat()
            legacy=(migrate_root_private is True and os.geteuid()==0 and owner[0]==0
                and mode==0o640 and info.st_uid==0 and info.st_gid==0 and stat.S_IMODE(info.st_mode)==0o600)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=owner[0]
                    or (not legacy and (info.st_gid!=owner[1] or stat.S_IMODE(info.st_mode)!=mode))):
                raise ValueError('runtime record target ownership invalid')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            if owner is not None:os.fchown(handle.fileno(),*owner)
            os.fchmod(handle.fileno(),mode)
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def lp_operation_descriptor(path, *, owner_uid, descriptor=None):
    """Acquire once, or prove an inherited descriptor owns this exact flock."""
    path=Path(path)
    owned=descriptor is None
    if owned:descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if type(descriptor) is not int or descriptor<0:raise ValueError('operation descriptor invalid')
        info=path.lstat();opened=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=owner_uid
                or info.st_mode & 0o022 or (info.st_dev,info.st_ino)!=(opened.st_dev,opened.st_ino)):
            raise ValueError('operation lock identity invalid')
        if not owned:
            probe=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:raise ValueError('operation descriptor has no retained flock')
            finally:os.close(probe)
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield descriptor
    finally:
        if owned:os.close(descriptor)


def inspect_prediction_release_checkout(
    checkout: Path, manifest_path: Path | None = None
) -> dict[str, object]:
    """Return independently verified release identity for a clean checkout."""
    checkout = Path(checkout).resolve()
    try:
        sha = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"release checkout cannot be verified: {checkout}") from exc
    if status:
        raise ValueError(f"release root is dirty: {checkout}")
    manifest = (checkout / "ops" / "prediction-service-release.json") if manifest_path is None else Path(manifest_path).resolve()
    try:
        relative = manifest.relative_to(checkout).as_posix()
    except ValueError as exc:
        raise ValueError("release manifest must be tracked by checkout") from exc
    try:
        subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", relative],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "-e", f"HEAD:{relative}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release manifest must be tracked by checkout") from exc
    release = load_prediction_release_manifest(manifest)
    return {
        "checkout": str(checkout),
        "git_sha": sha,
        "source_state": "clean",
        "reader_generation": release.reader_generation,
        "contract_generation": release.contract_generation,
        "manifest": str(manifest),
    }


def managed_release_identity_error(facts: Mapping[str, object]) -> str | None:
    """Explain why independently observed launchd evidence is not managed."""
    if facts.get("label_path") != facts.get("expected_plist_path"):
        return "managed launchd plist path is not verified"
    if facts.get("launchd_cwd") != facts.get("process_cwd"):
        return "managed launchd cwd disagrees with process cwd"
    expected_cwd = facts.get("expected_cwd")
    if expected_cwd and facts.get("process_cwd") != expected_cwd:
        return "managed process cwd is not verified"
    release_manifest = facts.get("release_manifest")
    process_cwd = facts.get("process_cwd")
    if not release_manifest or not process_cwd:
        return "managed launchd release manifest is not verified"
    try:
        manifest = Path(str(release_manifest)).resolve()
        checkout = Path(str(process_cwd)).resolve()
        manifest.relative_to(checkout)
        if not manifest.is_file():
            return "managed launchd release manifest is not verified"
    except (OSError, RuntimeError, ValueError):
        return "managed launchd release manifest is not verified"
    try:
        pid = int(facts["pid"])
        listener_pid = int(facts["listener_pid"])
    except (KeyError, TypeError, ValueError):
        return "managed PID evidence is not verified"
    if facts.get("listener_count") != 1 or listener_pid != pid:
        return "managed listener PID disagrees with managed PID"
    if facts.get("listener_addr") != "127.0.0.1:8769":
        return "managed listener is not verified"
    health = facts.get("health")
    if not isinstance(health, Mapping):
        return "managed health is unavailable"
    if health.get("pid") != pid:
        return "managed health PID disagrees with managed PID"
    if health.get("cwd") != facts.get("process_cwd"):
        return "managed health cwd is not verified"
    if (
        health.get("schema_version") != "open_trader.prediction_service.health.v1"
        or health.get("module") != "prediction_service"
        or health.get("status") != "running"
        or health.get("mode") != "production"
        or health.get("production_owner") is not True
        or health.get("mutations") != "enabled"
        or health.get("release_schema_version") != RELEASE_SCHEMA
        or not isinstance(health.get("git_sha"), str)
        or not isinstance(health.get("started_at"), str)
        or type(health.get("reader_generation")) is not int
        or type(health.get("contract_generation")) is not int
        or not health.get("started_at")
    ):
        return "managed health is not production-ready"
    arguments = facts.get("arguments")
    if not isinstance(arguments, list):
        return "managed launchd arguments are not verified"
    args = [str(item) for item in arguments]

    def value(flag: str) -> str | None:
        indexes = [index for index, item in enumerate(args) if item == flag]
        if len(indexes) != 1 or indexes[0] + 1 >= len(args):
            return None
        return args[indexes[0] + 1]

    if "-m" not in args:
        return "managed launchd arguments are not verified"
    module_index = args.index("-m")
    if args[module_index + 1:module_index + 3] != ["open_trader", "prediction-service"]:
        return "managed launchd arguments are not verified"
    if value("--mode") != "production":
        return "managed launchd arguments use a non-production mode"
    if value("--data-dir") != facts.get("expected_data_dir"):
        return "managed launchd data root is not verified"
    if value("--host") != "127.0.0.1" or value("--port") != "8769":
        return "managed launchd listener arguments are not verified"
    loaded_manifest = value("--release-manifest")
    try:
        loaded_manifest_path = Path(loaded_manifest).resolve() if loaded_manifest else None
    except (OSError, RuntimeError):
        loaded_manifest_path = None
    if loaded_manifest_path != manifest:
        return "managed launchd release manifest is not verified"
    lock_owner_pids = facts.get("lock_owner_pids")
    if lock_owner_pids is None:
        lock_owner_pid = facts.get("lock_owner_pid")
        lock_owner_pids = [] if lock_owner_pid is None else [lock_owner_pid]
    try:
        observed_lock_pids = {int(item) for item in lock_owner_pids}
    except (TypeError, ValueError):
        return "prediction runtime lock owner is not verified"
    if observed_lock_pids != {pid}:
        return "prediction runtime lock owner is not verified"
    return None


def ready_evidence_from_health(
    health: Mapping[str, object], *, pid: int, cwd: str, listener: str,
    stdout: str, stderr: str,
) -> dict[str, object]:
    """Build the persisted observation without trusting the prior record."""
    return {
        "pid": pid,
        "cwd": cwd,
        "listener": listener,
        "health_schema": health.get("schema_version"),
        "health_module": health.get("module"),
        "health_status": health.get("status"),
        "mode": health.get("mode"),
        "production_owner": health.get("production_owner"),
        "mutations": health.get("mutations"),
        "git_sha": health.get("git_sha"),
        "release_schema_version": health.get("release_schema_version"),
        "reader_generation": health.get("reader_generation"),
        "contract_generation": health.get("contract_generation"),
        "process_started_at": health.get("started_at"),
        "logs": {"stdout": stdout, "stderr": stderr},
    }


def archive_prediction_runtime_record(
    path: Path, audit_dir: Path, *, reason: str,
) -> Path:
    """Durably preserve exact record bytes before recovery replaces them."""
    path = Path(path)
    audit_dir = Path(audit_dir)
    raw = path.read_bytes()
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    safe_reason = "".join(character if character.isalnum() or character in "-_" else "_" for character in reason)
    target = audit_dir / f"{path.name}.recovery-{stamp}-{safe_reason}-{os.getpid()}.bak"
    audit_dir.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile("wb", dir=audit_dir, prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return target


LP_CAPABILITY_SCHEMA='open_trader.lp.runtime-contract.v1'
LP_VALIDATION_SCHEMA='open_trader.lp.validation.v1'
_LP_CAPABILITIES=('current_api_authority','no_request_replay','start_permit','reliable_control','native_drain')


def inspect_lp_capability(checkout):
    """Strict tracked lower bound, independent of the unchanged reader manifest."""
    import hashlib
    checkout=Path(checkout).resolve()
    path=checkout/'ops/lp-runtime-contract.json'
    raw=path.read_bytes();value=json.loads(raw)
    if (type(value) is not dict or set(value)!={'schema_version',*_LP_CAPABILITIES}
            or value['schema_version']!=LP_CAPABILITY_SCHEMA
            or any(type(value[k]) is not int or value[k]!=1 for k in _LP_CAPABILITIES)):
        raise ValueError('incompatible LP runtime capability')
    committed=subprocess.check_output(['git','-C',str(checkout),'show','HEAD:ops/lp-runtime-contract.json'])
    if raw!=committed:raise ValueError('LP runtime capability is not exact tracked source')
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def selected_lp_config(path, *, uid, gid=None):
    """Pin the actual private selected file while the existing loader parses it."""
    import hashlib
    from .polymarket_trading import load_trading_config
    path=Path(path).expanduser().absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('LP selected config path is not canonical')
    path=path.resolve(strict=True)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    def identity(info):
        return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mtime,info.st_ctime)
    try:
        initial=os.fstat(descriptor)
        if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink!=1 or initial.st_uid!=uid
                or (gid is not None and initial.st_gid!=gid) or stat.S_IMODE(initial.st_mode)!=0o600):
            raise ValueError('LP selected config metadata invalid')
        def recheck():
            if (identity(os.fstat(descriptor))!=identity(initial)
                    or identity(path.lstat())!=identity(initial)
                    or any(p.is_symlink() for p in (path,*path.parents))):
                raise ValueError('LP selected config file changed')
        recheck()
        with os.fdopen(os.dup(descriptor),'rb') as stream:raw=stream.read()
        configuration=load_trading_config(path)
        recheck()
        yield configuration,dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest()),recheck
    finally:os.close(descriptor)


def validate_lp_validation(binding,*,release_root,runtime_root,python,config_path=None,
                           config_uid=None,config_gid=None,verify_config=True):
    """Check an internally retained exact binding, never infer validation from READY."""
    import hashlib,re
    source=inspect_prediction_release_checkout(release_root)
    root=Path(release_root).resolve();runtime_root=Path(runtime_root).resolve()
    keys={'schema_version','source_sha','release_root','runtime_root','python','runtime','capability_digest','ci','operation_nonce','config_selection'}
    if (type(binding) is not dict or set(binding)!=keys or binding['schema_version']!=LP_VALIDATION_SCHEMA
            or binding['source_sha']!=source['git_sha'] or binding['release_root']!=str(root)
            or binding['runtime_root']!=str(runtime_root) or binding['python']!=str(python)
            or type(binding['operation_nonce']) is not str or not binding['operation_nonce']
            or binding['capability_digest']!=inspect_lp_capability(root)):
        raise ValueError('LP validation binding does not match selected runtime')
    selection=binding['config_selection']
    if (type(selection) is not dict or set(selection)!={'path','sha256'}
            or type(selection['path']) is not str or not Path(selection['path']).is_absolute()
            or os.path.normpath(selection['path'])!=selection['path']
            or type(selection['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',selection['sha256'])):
        raise ValueError('LP retained config selection invalid')
    if verify_config:
        with selected_lp_config(config_path or selection['path'],uid=os.getuid() if config_uid is None else config_uid,gid=config_gid) as (_,actual,recheck):
            if actual!=selection:raise ValueError('LP selected config does not match validation')
            recheck()
    report=binding['runtime']
    if (type(report) is not dict or set(report)!={'python','python_version','platform','source_root','lock_sha256','extras','dependencies','provenance'}
            or report['python']!=str(python) or report['source_root']!=str(root)
            or report['lock_sha256']!=hashlib.sha256((root/'uv.lock').read_bytes()).hexdigest()
            or type(report['extras']) is not list or type(report['dependencies']) is not dict or not report['dependencies']
            or any(type(report[k]) is not str or not report[k] for k in ('python_version','platform','provenance'))):
        raise ValueError('LP retained interpreter/lock report invalid')
    ci=binding['ci'];scopes=('gateway','legacy','account','prediction','portable')
    if (type(ci) is not dict or set(ci)!={'run_id','run_attempt','artifact_role','run_url','artifacts'}
            or any(type(ci[k]) is not int or ci[k]<1 for k in ('run_id','run_attempt'))
            or ci['artifact_role']!='test-only' or ci['run_url']!=f'https://github.com/raymizzou/open_trader/actions/runs/{ci["run_id"]}'
            or type(ci['artifacts']) is not list or len(ci['artifacts'])!=5):
        raise ValueError('LP trusted CI identity invalid')
    for scope,artifact in zip(scopes,ci['artifacts']):
        if (type(artifact) is not dict or set(artifact)!={'scope','id','digest'} or artifact['scope']!=scope
                or type(artifact['id']) is not int or artifact['id']<1
                or type(artifact['digest']) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}',artifact['digest'])):
            raise ValueError('LP trusted artifact identity invalid')
    if len({v['id'] for v in ci['artifacts']})!=5:raise ValueError('duplicate LP trusted artifact')
    return binding


def lp_launchd_operation_lock(manager_identity,*,uid):
    """The existing installer's lock beside its exact declared plist."""
    if type(manager_identity) is not str or not manager_identity:
        raise ValueError('launchd manager identity required')
    plist=Path(manager_identity)
    if (not plist.is_absolute() or str(plist)!=os.path.normpath(str(plist))
            or any(p.is_symlink() for p in (plist,*plist.parents))):
        raise ValueError('launchd manager path invalid')
    parent=plist.parent.resolve(strict=True);info=parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_mode & 0o022:
        raise ValueError('launchd manager directory invalid')
    if plist.exists():
        info=plist.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=uid or info.st_mode & 0o022):
            raise ValueError('launchd manager plist invalid')
    return parent/'.com.open-trader.prediction-service.release.lock'


def validate_lp_management_refresh(*,runtime_root,release_root,python,config_path,
        manager_identity,record_path,operation_fd,expected_record):
    """Read exact retained launchd proof without changing startup selection."""
    import importlib.util
    def canonical(value):
        path=Path(value).expanduser().absolute()
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('LP refresh path contains a symlink')
        return path.resolve(strict=True)
    root=canonical(runtime_root);release=canonical(release_root)
    config=canonical(config_path);record_path=Path(record_path)
    if (record_path!=root/'prediction-service-runtime.json' or operation_fd is None
            or type(expected_record) is not dict):
        raise ValueError('LP refresh management context invalid')
    def stable(info):
        return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    with ExitStack() as context:
        context.enter_context(lp_operation_descriptor(
            lp_launchd_operation_lock(manager_identity,uid=os.getuid()),
            owner_uid=os.getuid(),descriptor=operation_fd))
        directories=[]
        for path in (root,root/'data'):
            info=path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid()
                    or info.st_mode & 0o022):
                raise ValueError('LP refresh runtime metadata invalid')
            directories.append((path,(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid)))
        descriptor=os.open(record_path,os.O_RDONLY|os.O_NOFOLLOW)
        context.callback(os.close,descriptor)
        initial=os.fstat(descriptor)
        if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink!=1
                or initial.st_uid!=os.getuid() or stat.S_IMODE(initial.st_mode)!=0o600):
            raise ValueError('LP refresh record metadata invalid')
        with os.fdopen(os.dup(descriptor),'rb') as stream:raw=stream.read()
        current=load_prediction_runtime_record(record_path)
        if (current!=expected_record or current.get('manager')!='launchd'
                or current.get('manager_identity')!=manager_identity
                or current.get('pending_lp_validation') is not None):
            raise ValueError('LP refresh record selection changed')
        def recheck_record():
            if (stable(os.fstat(descriptor))!=stable(initial)
                    or stable(record_path.lstat())!=stable(initial)
                    or record_path.read_bytes()!=raw
                    or load_prediction_runtime_record(record_path)!=current):
                raise ValueError('LP refresh record changed')
            for path,identity in directories:
                info=path.lstat()
                if (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid)!=identity:
                    raise ValueError('LP refresh runtime changed')
        recheck_record()
        _,selection,recheck_config=context.enter_context(selected_lp_config(config,uid=os.getuid()))
        source=inspect_prediction_release_checkout(release)
        identity={key:value for key,value in source.items() if key!='manifest'}
        receipts=[]
        for entry in (current.get('candidate'),current.get('previous_release')):
            if (isinstance(entry,dict)
                    and {key:value for key,value in entry.items()
                         if key not in ('lp_validation','manifest')}==identity
                    and entry.get('lp_validation') is not None):
                receipts.append(entry['lp_validation'])
        if not receipts or any(value!=receipts[0] for value in receipts[1:]):
            raise ValueError('LP refresh retained validation missing or ambiguous')
        binding=validate_lp_validation(receipts[0],release_root=release,runtime_root=root,
            python=python,config_path=config)
        if binding['config_selection']!=selection:
            raise ValueError('LP refresh selected config changed')
        spec=importlib.util.spec_from_file_location('lp_refresh_runtime_verifier',release/'scripts/deployment_preflight.py')
        verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
        if verifier.probe_runtime(str(python),release,binding['runtime']['extras'])!=binding['runtime']:
            raise ValueError('LP refresh retained runtime changed')
        validate_lp_validation(binding,release_root=release,runtime_root=root,python=python,config_path=config)
        recheck_config();recheck_record()
        return binding


@contextmanager
def _initial_systemd_owner(root,user):
    # This is fresh filesystem creation under the already verified root
    # operation. Existing objects never receive an ownership/mode repair.
    if os.geteuid()!=0:raise ValueError('initial systemd owner creation requires root')
    for directory in (root/'data',root/'data/prediction_arbitrage'):
        try:os.mkdir(directory,0o700);created=True
        except FileExistsError:created=False
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            opened=os.fstat(descriptor);named=directory.lstat()
            if (not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)
                    or (created and opened.st_uid!=os.geteuid())):
                raise ValueError('initial systemd owner directory identity invalid')
            if created:
                os.fchown(descriptor,user.pw_uid,user.pw_gid)
                os.fchmod(descriptor,0o700)
                os.fsync(descriptor)
            info=os.fstat(descriptor)
            if (info.st_uid!=user.pw_uid or info.st_gid!=user.pw_gid or info.st_mode & 0o022
                    or (info.st_dev,info.st_ino)!=(directory.lstat().st_dev,directory.lstat().st_ino)):
                raise ValueError('initial systemd owner directory metadata invalid')
        finally:os.close(descriptor)
        if created:
            parent=os.open(directory.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
    owner=root/'data/prediction_arbitrage/runtime.lock'
    try:descriptor=os.open(owner,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created=True
    except FileExistsError:descriptor=os.open(owner,os.O_RDWR|os.O_NOFOLLOW);created=False
    try:
        opened=os.fstat(descriptor);named=owner.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1
                or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)
                or (created and opened.st_uid!=os.geteuid())):
            raise ValueError('initial systemd owner file identity invalid')
        if created:
            os.fchown(descriptor,user.pw_uid,user.pw_gid)
            os.fchmod(descriptor,0o600)
            os.fsync(descriptor)
            parent=os.open(owner.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        info=os.fstat(descriptor);named=owner.lstat()
        if (info.st_uid!=user.pw_uid or info.st_gid!=user.pw_gid or info.st_mode & 0o022
                or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino)):
            raise ValueError('initial systemd owner file metadata invalid')
        yield descriptor
    finally:os.close(descriptor)


def prepare_lp_target(*,runtime_root,release_root,python,operation_nonce,host='127.0.0.1',port=8769,
                      manager='foreground',manager_identity=None,record_path=None,operation_fd=None,systemd_config=None,
                      config_path=None,require_prepared=False,expected_sha=None,extras=None):
    """Manager-owned trusted producer; capture existing verifier once under operation lock."""
    import fcntl,hashlib,socket,errno
    root=Path(runtime_root).resolve();release=Path(release_root).resolve()
    if type(operation_nonce) is not str or not operation_nonce:raise ValueError('LP operation nonce required')
    if extras is not None and (type(extras) not in (list,tuple) or any(type(v) is not str or v not in ('cloud-ssm','browser') for v in extras)):
        raise ValueError('LP runtime extras invalid')
    requested_extras=None if extras is None else sorted(set(extras))
    publication={}
    if manager=='systemd':
        from .prediction_cloud import OPERATION_LOCK,UNIT_PATH,service_user,trusted_layout,systemd_record_metadata
        c=systemd_config
        if (os.geteuid()!=0 or operation_fd is None or c is None or c.mode!='production'
                or c.runtime_root!=root or c.release_root!=release or str(c.python)!=str(python)):
            raise ValueError('trusted root systemd operation required')
        record_path=c.record;lock=OPERATION_LOCK;manager_identity=str(UNIT_PATH)
    else:
        record_path=Path(record_path) if record_path is not None else root/'prediction-service-runtime.json'
        if manager=='launchd':
            if operation_fd is None:raise ValueError('launchd prepare requires existing operation descriptor')
            lock=lp_launchd_operation_lock(manager_identity,uid=os.getuid())
        else:lock=root/'prediction-service-operation.lock'
    with ExitStack() as context:
        context.enter_context(lp_operation_descriptor(lock,owner_uid=0 if manager=='systemd' else os.getuid(),descriptor=operation_fd))
        if manager=='systemd':
            trusted_layout(c)
            user=service_user(c)
            publication=dict(owner=(0,user.pw_gid),mode=0o640,migrate_root_private=True)
            if record_path.exists() or record_path.is_symlink():systemd_record_metadata(c,allow_legacy=True)
        created_root_identity=None
        if manager=='launchd' and not require_prepared:
            # First installation has no runtime root yet. Setup stays under
            # the adopted launchd operation FD; it conveys no start permission.
            if record_path!=root/'prediction-service-runtime.json':
                raise ValueError('LP management record runtime metadata invalid')
            missing=[];anchor=root
            while True:
                try:anchor.lstat()
                except FileNotFoundError:
                    missing.append(anchor.name);anchor=anchor.parent
                else:break
            if missing:
                descriptor=os.open(anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                context.callback(os.close,descriptor)
                named=anchor.lstat();opened=os.fstat(descriptor)
                if (not stat.S_ISDIR(named.st_mode)
                        or (named.st_dev,named.st_ino)!=(opened.st_dev,opened.st_ino)):
                    raise ValueError('LP runtime parent changed')
                for component in reversed(missing):
                    created=True
                    try:os.mkdir(component,mode=0o700,dir_fd=descriptor)
                    except FileExistsError:created=False
                    child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                    context.callback(os.close,child)
                    info=os.fstat(child);named=os.stat(component,dir_fd=descriptor,follow_symlinks=False)
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid()
                            or info.st_mode & 0o022
                            or (created and stat.S_IMODE(info.st_mode)!=0o700)
                            or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino)):
                        raise ValueError('LP newly initialized runtime metadata invalid')
                    descriptor=child
                created_root_identity=(info.st_dev,info.st_ino)
        def check_record_metadata():
            if manager=='systemd':
                if record_path.exists() or record_path.is_symlink():systemd_record_metadata(c,allow_legacy=True)
                return
            info=root.lstat()
            if created_root_identity is not None and (info.st_dev,info.st_ino)!=created_root_identity:
                raise ValueError('LP initialized runtime root changed')
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o022
                    or record_path!=root/'prediction-service-runtime.json'):
                raise ValueError('LP management record runtime metadata invalid')
            if record_path.exists() or record_path.is_symlink():
                info=record_path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                        or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600):
                    raise ValueError('LP management record metadata invalid')
        check_record_metadata()
        original=load_prediction_runtime_record(record_path)
        config_path=(root/'config/prediction_arbitrage.json' if manager in ('systemd','launchd') else root/'prediction.json') if config_path is None else Path(config_path)
        if manager=='systemd' and config_path!=root/'config/prediction_arbitrage.json':raise ValueError('systemd selected config layout invalid')
        _,selection,recheck_config=context.enter_context(selected_lp_config(config_path,
            uid=user.pw_uid if manager=='systemd' else os.getuid(),gid=user.pw_gid if manager=='systemd' else None))
        source=inspect_prediction_release_checkout(release);digest=inspect_lp_capability(release)
        if manager=='systemd' and source['git_sha']!=c.expected_sha:raise ValueError('systemd target source changed')
        if expected_sha is not None and source['git_sha']!=expected_sha:raise ValueError('LP selected source changed')
        retained=[]
        pending=None if original is None else original.get('pending_lp_validation')
        if require_prepared:
            if not isinstance(pending,dict) or pending.get('operation_nonce')!=operation_nonce or pending.get('config_selection')!=selection:
                raise ValueError('LP forwarded nonce or config selection changed')
            retained.append(pending)
        if original is not None and not require_prepared:
            for selected in (original.get('candidate'),original.get('previous_release')):
                if (isinstance(selected,dict) and selected.get('checkout')==str(release)
                        and selected.get('git_sha')==source['git_sha'] and selected.get('lp_validation') is not None):
                    retained.append(selected['lp_validation'])
        if len(retained)>1 and retained[0]!=retained[1]:raise ValueError('ambiguous retained LP validation')
        if retained:
            binding=dict(validate_lp_validation(retained[0],release_root=release,runtime_root=root,python=python,verify_config=False))
            if requested_extras is not None and not set(requested_extras)<=set(binding['runtime']['extras']):
                if require_prepared:raise ValueError('LP forwarded runtime extras changed')
                # A new runtime requirement needs the actual trusted verifier;
                # config-only reselection still reuses its unchanged report.
                retained=[]
        if retained:
            # Reuse the existing verifier's local isolated-runtime probe. This
            # is not another CI/backend run or an expired artifact download.
            import importlib.util
            spec=importlib.util.spec_from_file_location('lp_retained_runtime_verifier',release/'scripts/deployment_preflight.py')
            verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
            if verifier.probe_runtime(str(python),release,binding['runtime']['extras'])!=binding['runtime']:
                raise ValueError('retained LP runtime changed')
            binding['operation_nonce']=operation_nonce
            binding['config_selection']=selection
        else:
            command=[str(python),str(release/'scripts/deployment_preflight.py'),'--expected-sha',source['git_sha'],
                '--release-root',str(release),'--python',str(python)]
            for extra in requested_extras or ():command.extend(['--extra',extra])
            result=subprocess.run(command,check=True,capture_output=True,text=True,
                env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(release/'src')})
            proof=json.loads(result.stdout)
            if (type(proof) is not dict or set(proof)!={'status','source_sha','release_root','deployment_unit','runtime','ci'}
                    or proof['status']!='PASS' or proof['source_sha']!=source['git_sha'] or proof['release_root']!=str(release)
                    or proof['deployment_unit']!='source checkout plus existing Python environment'):
                raise ValueError('trusted LP verifier result identity invalid')
            binding=dict(schema_version=LP_VALIDATION_SCHEMA,source_sha=source['git_sha'],release_root=str(release),
                runtime_root=str(root),python=str(python),runtime=proof['runtime'],capability_digest=digest,
                ci=proof['ci'],operation_nonce=operation_nonce,config_selection=selection)
        validate_lp_validation(binding,release_root=release,runtime_root=root,python=python,
            config_path=config_path,config_uid=user.pw_uid if manager=='systemd' else os.getuid(),
            config_gid=user.pw_gid if manager=='systemd' else None)
        owner_directories=[]
        if manager=='launchd' and not require_prepared:
            # An interrupted record may predate data-directory setup. Only
            # the verified explicit producer can initialize missing components.
            descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            context.callback(os.close,descriptor)
            info=os.fstat(descriptor)
            owner_directories.append((root,info.st_dev,info.st_ino))
            for component in ('data','prediction_arbitrage'):
                created=True
                try:os.mkdir(component,0o700,dir_fd=descriptor)
                except FileExistsError:created=False
                child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                context.callback(os.close,child)
                info=os.fstat(child);named=os.stat(component,dir_fd=descriptor,follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o022
                        or (created and stat.S_IMODE(info.st_mode)!=0o700)
                        or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino)):
                    raise ValueError('LP initial data directory metadata invalid')
                path=root/'data' if component=='data' else root/'data/prediction_arbitrage'
                owner_directories.append((path,info.st_dev,info.st_ino))
                descriptor=child
        def recheck_selection():
            check_record_metadata()
            for path,device,inode in owner_directories:
                info=path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o022
                        or (info.st_dev,info.st_ino)!=(device,inode)):
                    raise ValueError('LP initialized data directory changed')
            if load_prediction_runtime_record(record_path)!=original:
                raise ValueError('LP runtime selection changed during verification')
            recheck_config()
        recheck_selection()
        if original is None:
            owner=root/'data/prediction_arbitrage/runtime.lock'
            if manager=='systemd':descriptor=context.enter_context(_initial_systemd_owner(root,user))
            else:
                owner.parent.mkdir(parents=True,exist_ok=True)
                descriptor=context.enter_context(owner.open('a+')).fileno()
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex((host,port))!=errno.ECONNREFUSED:raise ValueError('initial LP listener absence unproved')
            document=dict(state='stopped',manager=manager,manager_identity=manager_identity or f'foreground:{os.getuid()}',
                candidate=source,previous_release=None,pending_lp_validation=binding)
            if manager=='systemd':
                from .prediction_cloud import systemd_record_identity
                document.update(systemd_record_identity(c))
            recheck_selection()
            write_prediction_runtime_record(record_path,document,**publication)
        else:
            # A prospective target cannot replace the installed unit identity.
            # The root installer promotes it only after checking/replacing that unit.
            document={**original,'pending_lp_validation':binding}
            recheck_selection()
            write_prediction_runtime_record(record_path,document,**publication)
        return operation_nonce
