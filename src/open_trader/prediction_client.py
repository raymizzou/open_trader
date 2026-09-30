"""Local Prediction Gateway and one owned SSH tunnel; never starts the backend."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time

from .prediction_cloud import read_json, run
from .prediction_release import RELEASE_SCHEMA, inspect_prediction_release_checkout


_HEALTH_MISSING = object()


def client_ports(c):
    defaults = {'production':(8766,8769), 'shadow':(8876,8879)}
    if 'execution_port' in c:
        defaults['shadow'] = (8766,8879)
    gateway_port = c.get('gateway_port', defaults[c['mode']][0])
    tunnel_port = c.get('tunnel_port', defaults[c['mode']][1])
    if (type(gateway_port) is not int or type(tunnel_port) is not int
        or not 1024 <= gateway_port <= 65535 or not 1024 <= tunnel_port <= 65535
        or gateway_port == tunnel_port or c.get('execution_port') in (gateway_port, tunnel_port)):
        raise ValueError('distinct loopback client ports required')
    return gateway_port, tunnel_port


def validate(c):
    if not {'release_root','runtime_root','python','ssh_alias','expected_sha','mode'} <= set(c) \
       or set(c) - {'release_root','runtime_root','python','ssh_alias','expected_sha','mode','gateway_port','tunnel_port','execution_port','execution_expected_sha','cloud_expected_sha'}:
        raise ValueError('invalid client config keys')
    for key in ('release_root','runtime_root','python'):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', c[key]) or '..' in Path(c[key]).parts:
            raise ValueError('absolute paths without whitespace required')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', c['ssh_alias'])
        or not re.fullmatch(r'[0-9a-f]{40}', c['expected_sha'])
        or c['mode'] not in {'production','shadow'}):
        raise ValueError('SSH alias and explicit production or shadow mode required')
    if 'cloud_expected_sha' in c and not re.fullmatch(r'[0-9a-f]{40}', c['cloud_expected_sha']):
        raise ValueError('explicit cloud SHA required')
    if 'execution_port' in c or 'execution_expected_sha' in c:
        if (c['mode'] != 'shadow' or type(c.get('execution_port')) is not int
            or not 1024 <= c['execution_port'] <= 65535
            or not re.fullmatch(r'[0-9a-f]{40}', c.get('execution_expected_sha', ''))):
            raise ValueError('split display requires Shadow and explicit Air execution port/SHA')
    client_ports(c)
    a, b = Path(c['release_root']).resolve(), Path(c['runtime_root']).resolve()
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise ValueError('separate client runtime required')


def process_identity(pid):
    p = subprocess.run(['ps','-ww','-p',str(pid),'-o','stat=','-o','lstart=','-o','args='], capture_output=True, text=True,
                       env={**os.environ, 'LC_ALL':'C'}, timeout=5)
    if p.returncode == 1 and not p.stdout.strip():
        return None
    if p.returncode != 0 or not p.stdout.strip():
        raise ValueError('process identity unavailable')
    state, identity = p.stdout.strip().split(None, 1)
    if state.startswith('Z'):
        return None  # Exited, awaiting its parent's reap; never signal a zombie.
    return identity


def same_process(proc):
    observed = process_identity(proc['pid'])
    if observed is not None and observed != proc['identity']:
        # ps can sample R before exit and read an already-cleared cmdline.
        # Let exit settle before rechecking; never signal a different PID.
        time.sleep(0.05)
        observed = process_identity(proc['pid'])
    if observed is None:
        return False
    if observed != proc['identity']:
        raise ValueError('PID identity changed; refusing to signal')
    return True


def save(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def ports_absent(ports=(8766,8769)):
    for port in ports:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1',port))


def stop_owned(state):
    # Verify every saved identity before signaling either process.
    alive = {key: same_process(state[key]) for key in ('gateway','ssh') if state.get(key)}
    if not state:
        return
    gateway_port, tunnel_port = client_ports(state['config'])
    ports_absent(tuple(port for key, port in (('gateway',gateway_port),('ssh',tunnel_port)) if not alive.get(key)))
    for key in ('gateway','ssh'):
        if not alive.get(key):
            continue
        proc = state[key]
        if same_process(proc):
            os.kill(proc['pid'], signal.SIGTERM)
        deadline = time.monotonic()+10
        while same_process(proc):
            if time.monotonic() >= deadline:
                raise ValueError('owned process did not stop; state retained')
            time.sleep(.1)
    ports_absent((gateway_port,tunnel_port))  # A surviving unknown listener retains the record.


def client_release(c):
    identity = inspect_prediction_release_checkout(Path(c['release_root']))
    if identity['git_sha'] != c['expected_sha'] or run('git','-C',c['release_root'],'rev-parse','--abbrev-ref','HEAD') != 'HEAD':
        raise ValueError('exact immutable client release required')
    return identity


def status(c, state):
    if any(not state.get(k) or not same_process(state[k]) for k in ('ssh','gateway')):
        raise ValueError('client process missing')
    gateway_port, tunnel_port = client_ports(c)
    gateway, backend = read_json(gateway_port), read_json(tunnel_port)
    split = 'execution_port' in c
    if (gateway.get('pid') != state['gateway']['pid'] or gateway.get('prediction_only') is not (not split)
        or (split and (gateway.get('prediction_split') is not True
            or gateway.get('prediction_display_upstream_port') != tunnel_port))
        or (not split and gateway.get('prediction_upstream_status') != 'ok')
        or gateway.get('prediction_route_mode') != 'service'
        or gateway.get('cwd') != c['release_root']
        or Path(gateway.get('code_root','')).resolve() != Path(c['release_root'])/'src'):
        raise ValueError('Gateway identity or upstream mismatch')
    for health, expected in ((gateway, c['expected_sha']), (backend, c.get('cloud_expected_sha', c['expected_sha']))):
        if health.get('git_sha') != expected or health.get('source_state') != 'clean':
            raise ValueError('client/backend release mismatch')
    manifest = client_release(c)
    mode_identity = (
        dict(mode='shadow',production_owner=False,mutations='prohibited',first_violation=None)
        if c['mode'] == 'shadow' else
        dict(mode='production',production_owner=True,mutations='enabled')
    )
    required = dict(module='prediction_service',status='running',
        schema_version='open_trader.prediction_service.health.v1',**mode_identity,
        release_schema_version=RELEASE_SCHEMA,
        reader_generation=manifest['reader_generation'],contract_generation=manifest['contract_generation'])
    backend_root = Path(backend.get('cwd',''))
    if (any((backend[key] if key in backend else _HEALTH_MISSING) != value for key,value in required.items())
        or any(not isinstance(health.get('started_at'),str) or not health['started_at'] for health in (gateway,backend))
        or type(backend.get('pid')) is not int or backend['pid'] <= 0
        or not backend_root.is_absolute() or backend.get('code_root') != str(backend_root/'src')):
        raise ValueError('backend health identity unavailable')
    execution = {}
    if split:
        if backend.get('n_leg', {}).get('status') != 'paused':
            raise ValueError('cloud display must keep N-leg paused')
        try:
            air = read_json(c['execution_port'])
        except (OSError, ValueError):
            execution = {'execution_status':'unavailable', 'execution_git_sha':None}
        else:
            if (air.get('module') != 'prediction_service' or air.get('mode') != 'production'
                or air.get('production_owner') is not True or air.get('mutations') != 'enabled'
                or air.get('git_sha') != c['execution_expected_sha'] or air.get('source_state') != 'clean'):
                raise ValueError('Air execution identity mismatch')
            execution = {'execution_status':'ok', 'execution_git_sha':air['git_sha'], 'execution_pid':air.get('pid')}
    client_release(c)  # Health metadata can outlive a changed checkout.
    return {'status':'CONNECTED', 'git_sha':c['expected_sha'], 'cloud_git_sha':backend.get('git_sha'),
            **execution, 'backend_mode':backend.get('mode'),
            'gateway_pid':state['gateway']['pid'], 'ssh_pid':state['ssh']['pid'],
            'url':f'http://127.0.0.1:{gateway_port}/', 'gateway_port':gateway_port, 'tunnel_port':tunnel_port}


def client_operation(c, action):
    validate(c)
    root = Path(c['runtime_root'])
    path = root/'client.json'
    if action in ('stop','status') and not root.exists():
        return {'status':'STOPPED', 'scope':'local client only'}
    if action == 'start':
        client_release(c)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('client runtime must be owned by current user with mode 0700')
    with (root/'operation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(path.read_text()) if path.exists() else {}
        if state and state.get('config') != c:
            raise ValueError('client config changed; use original config to stop it')
        if action == 'stop':
            stop_owned(state)
            path.unlink(missing_ok=True)
            return {'status':'STOPPED', 'scope':'local client only'}
        if action == 'status' or state:
            if not state:
                return {'status':'STOPPED', 'scope':'local client only'}
            return status(c, state)
        gateway_port, tunnel_port = client_ports(c)
        ports_absent((gateway_port,tunnel_port))
        route = root/'prediction-route.json'
        route.write_text(json.dumps(dict(schema_version='open_trader.frontend_gateway.prediction_route.v1',
            mode='service',operation_id='prediction-cloud-client',updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))))
        commands = {
            'ssh':['ssh','-NT','-o','ControlMaster=no','-o','ControlPath=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                   '-o','ForwardAgent=no','-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=10',
                   '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
                   '-L',f'127.0.0.1:{tunnel_port}:127.0.0.1:8769',c['ssh_alias']],
            'gateway':[c['python'],'-m','open_trader','frontend-gateway',
                       *([] if 'execution_port' in c else ['--prediction-only']),
                       '--host','127.0.0.1','--port',str(gateway_port),
                       '--public-origin',f'http://127.0.0.1:{gateway_port}',
                       '--prediction-upstream-port',str(c.get('execution_port', tunnel_port)),
                       *(['--prediction-display-upstream-port',str(tunnel_port)] if 'execution_port' in c else []),
                       '--prediction-route-state',str(route),'--static-dir',c['release_root']+'/src/open_trader/dashboard_static'],
        }
        state = {'config':c}
        children = []
        try:
            for key, command in commands.items():
                with (root/f'{key}.log').open('wb') as log:
                    process = subprocess.Popen(command, cwd=c['release_root'], stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                        env={**os.environ, 'PYTHONPATH':c['release_root']+'/src', 'PYTHONDONTWRITEBYTECODE':'1'})
                children.append(process)
                identity = process_identity(process.pid)
                if identity is None:
                    raise ValueError('child exited during startup')
                state[key] = {'pid':process.pid,'identity':identity}
                save(path,state)
                if key == 'ssh':
                    deadline = time.monotonic()+15
                    while True:
                        try:
                            health = read_json(tunnel_port)
                            if health.get('git_sha') != c.get('cloud_expected_sha', c['expected_sha']):
                                raise ValueError('backend SHA mismatch')
                            break
                        except (OSError,ValueError):
                            if time.monotonic() >= deadline or process.poll() is not None:
                                raise ValueError('SSH/backend not ready') from None
                            time.sleep(.2)
            deadline = time.monotonic()+15
            while True:
                try:
                    return status(c,state)
                except (OSError,ValueError):
                    if time.monotonic() >= deadline:
                        raise ValueError('Gateway not ready') from None
                    time.sleep(.2)
        except Exception:
            # Popen objects still belong to this invocation, so reaping these
            # children is safe even when recording their identity failed.
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
            path.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start','status','stop'])
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/open-trader/prediction-client.json')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(client_operation(json.loads(args.config.read_text()),args.action)))
        return 0
    except Exception:
        print(json.dumps({'status':'BLOCKED','reason':'client identity/startup could not be verified; inspect local client logs'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
