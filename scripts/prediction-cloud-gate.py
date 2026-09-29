#!/usr/bin/env python3
"""Read-only two-host gate. Never installs, restarts, rolls back or submits."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from open_trader.prediction_cloud import credential_backend, load_config
from open_trader.prediction_client import client_operation, client_ports, client_release, validate
from open_trader.prediction_release import inspect_prediction_release_checkout


def checked(command, *, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError('gate command failed: ' + command[0])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['readiness','smoke'])
    parser.add_argument('--client-config', type=Path, required=True)
    parser.add_argument('--service-config', type=Path, required=True, help='local copy of non-secret remote config')
    parser.add_argument('--remote-config', required=True, help='absolute path on CVM')
    parser.add_argument('--operator-evidence', type=Path, required=True)
    parser.add_argument('--browser-runtime', type=Path, required=True, help='local root containing node_modules')
    args = parser.parse_args()
    end = 'BLOCKED' if args.action == 'readiness' else 'ROLLBACK'
    try:
        local = json.loads(args.client_config.read_text())
        validate(local)
        cloud = load_config(args.service_config)
        if cloud.mode != local['mode']:
            raise ValueError('client and cloud service modes mismatch')
        evidence = json.loads(args.operator_evidence.read_text())
        if cloud.expected_sha != local['expected_sha'] or evidence.get('git_sha') != cloud.expected_sha:
            raise ValueError('two-host SHA or operator evidence mismatch')
        # These are explicit operator attestations, not automated proof. Credentialless
        # paused Shadow replaces owner-cutover/metadata attestations with an exact
        # independent-runtime match; the remote component profile is checked below.
        if credential_backend(cloud) == 'disabled':
            if evidence.get('independent_runtime_root') != str(cloud.runtime_root):
                raise ValueError('independent Shadow runtime evidence mismatch')
            required_attestations = ('resources_reviewed',)
        else:
            required_attestations = ('old_owner_stopped', 'metadata_isolation_verified', 'resources_reviewed')
        for field in required_attestations:
            if evidence.get(field) is not True or not evidence.get(field+'_evidence'):
                raise ValueError('operator handoff/isolation/resource evidence missing')
        if not Path(args.remote_config).is_absolute():
            raise ValueError('absolute remote config required')
        release = Path(local['release_root'])
        identity = inspect_prediction_release_checkout(release)
        if identity['git_sha'] != cloud.expected_sha or checked(['git','-C',str(release),'rev-parse','--abbrev-ref','HEAD']).strip() != 'HEAD':
            raise ValueError('local immutable release mismatch')
        action = 'preflight' if args.action == 'readiness' else 'smoke'
        remote = ('cd '+shlex.quote(str(cloud.release_root))+' && '+shlex.join([
            'env', 'PYTHONPATH='+str(cloud.release_root/'src'), 'PYTHONDONTWRITEBYTECODE=1', str(cloud.python),
            '-m','open_trader.prediction_cloud',action,'--config',args.remote_config]))
        output = checked(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ForwardAgent=no',
                          '-o','ConnectTimeout=10',local['ssh_alias'],remote])
        result = json.loads(output)
        if result.get('release_root') != str(cloud.release_root) or result.get('runtime_root') != str(cloud.runtime_root):
            raise ValueError('remote release/runtime root mismatch')
        if (result.get('mode') != cloud.mode or result.get('git_sha') != cloud.expected_sha
            or result.get('n_leg_paused') != cloud.n_leg_paused
            or result.get('credential_backend') != credential_backend(cloud)
            or result.get('status') != ('PRECHECK_OK' if action == 'preflight' else 'BACKEND_SMOKE_OK')):
            raise ValueError('remote gate evidence mismatch')
        env = {**os.environ, 'PYTHONSAFEPATH':'1', 'PYTHONPATH':str(release)+':'+str(release/'src'),
               'NODE_PATH':str(args.browser_runtime/'node_modules'),
               'OPEN_TRADER_SMOKE_URL':f"http://127.0.0.1:{client_ports(local)[0]}/"}
        runner = args.browser_runtime/'node_modules/.bin/playwright'
        if args.action == 'readiness':
            checked([local['python'],'-c', 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel="chrome",headless=True); b.close(); p.stop()'],cwd=release,env=env)
            checked(['node','-e','const {chromium}=require("playwright"); (async()=>{const b=await chromium.launch({headless:true}); await b.close()})().catch(()=>process.exit(1))'],cwd=release,env=env)
            checked([str(runner),'test','tests/e2e/production-smoke.spec.ts','--config=playwright.config.ts','--project=chromium','--list'],cwd=release,env=env)
            client_release(local)
            print('READY')
        else:
            before = client_operation(local,'status')
            if before['status'] != 'CONNECTED':
                raise ValueError('local client not connected')
            log = Path(local['runtime_root'])/'gateway.log'
            text = log.read_text()
            if 'frontend_gateway_runtime:' not in text or any(word in text.lower() for word in ('traceback','fatal','exception','error')):
                raise ValueError('local Gateway log missing or contains errors')
            checked([local['python'],'-m','pytest','-q','-m','browser'],cwd=release,env=env)
            checked([str(runner),'test','tests/e2e/production-smoke.spec.ts','--config=playwright.config.ts','--project=chromium'],cwd=release,env=env)
            if client_operation(local,'status') != before:
                raise ValueError('client changed during browser smoke')
            after = json.loads(checked(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                '-o','ForwardAgent=no','-o','ConnectTimeout=10',local['ssh_alias'],remote]))
            if after != result:
                raise ValueError('backend changed during browser smoke')
            client_release(local)
            print('HEALTHY')
        return 0
    except Exception as exc:
        # Exception messages from this coordinator contain no SDK payloads.
        print(f'cloud gate: {type(exc).__name__}')
        print(end)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
