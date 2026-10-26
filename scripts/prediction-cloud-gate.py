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
from open_trader.prediction_client import (client_operation, client_ports, client_release,
    validate, observe_local_lp_peer, require_fresh_instance_observation)
from open_trader.prediction_release import inspect_prediction_release_checkout


def checked(command, *, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError('gate command failed: ' + command[0])
    return result.stdout


def remote_identity(result, cloud, action):
    """Validate each remote observation; only display snapshots may move."""
    expected = dict(release_root=str(cloud.release_root), runtime_root=str(cloud.runtime_root),
        mode=cloud.mode, git_sha=cloud.expected_sha, n_leg_paused=cloud.effective_n_leg_paused,
        configured_n_leg_paused=cloud.n_leg_paused,
        credential_backend=credential_backend(cloud), candidate_exclusions=cloud.candidate_exclusions,
        status='PRECHECK_OK' if action == 'preflight' else 'BACKEND_SMOKE_OK')
    if (type(result.get('candidate_exclusions')) is not bool
        or type(result.get('n_leg_paused')) is not int
        or type(result.get('configured_n_leg_paused')) is not int
        or any(key not in result or result[key] != value for key, value in expected.items())):
        raise ValueError('remote gate evidence mismatch')
    if action == 'smoke':
        if (type(result.get('pid')) is not int or result['pid'] <= 0
            or any(not isinstance(result.get(key), str) or not result[key]
                   for key in ('started_at', 'systemd_started_at'))):
            raise ValueError('remote process identity missing')
        if cloud.mode == 'shadow' and cloud.effective_n_leg_paused and credential_backend(cloud) != 'disabled':
            from datetime import UTC, datetime
            from decimal import Decimal
            from open_trader.polymarket_lp_risk import _freshness
            snapshot = result.get('display_snapshot')
            if (not isinstance(snapshot, dict)
                or any(not isinstance(snapshot.get(key), dict)
                       for key in ('account', 'catalog', 'candidates', 'rewards'))
                or 'history' not in snapshot):
                raise ValueError('remote display snapshot evidence missing')
            _freshness(snapshot['account'].get('checked_at'), datetime.now(UTC),
                       'account_facts', max_age=Decimal(60))
    stable = {key:value for key,value in result.items() if key != 'display_snapshot'}
    if cloud.lp_instance_id is not None:
        import re
        capability=result.get('lp_instance_isolation')
        expected=dict(schema_version='open_trader.lp.local_instance.v1',
            ownership_policy='local_only_full_wallet_v1',
            stage='staged' if action=='preflight' else 'running',instance_id=cloud.lp_instance_id,
            source_sha=cloud.expected_sha,code_root=str(cloud.release_root),runtime_root=str(cloud.runtime_root),
            config_path=str(cloud.runtime_root/'config/prediction_arbitrage.json'),venue='polymarket')
        if (not isinstance(capability,dict) or any(capability.get(k)!=v for k,v in expected.items())
                or not re.fullmatch(r'0x[0-9a-f]{40}',str(capability.get('account_id','')))
                or not re.fullmatch(r'[0-9a-f]{64}',str(capability.get('config_sha256','')))
                or action=='smoke' and (capability.get('enabled') is not True or not capability.get('process_epoch'))):
            raise ValueError('remote LP instance identity mismatch')
        require_fresh_instance_observation(capability)
        stable['lp_instance_isolation']={k:v for k,v in capability.items() if k!='observed_at'}
    return stable


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
        peer = None
        if 'lp_parallel_peer' in local:
            if cloud.mode!='production' or cloud.lp_instance_id is None:
                raise ValueError('parallel LP requires explicit production instances')
            peer=observe_local_lp_peer(local)
            if peer['instance_id']==cloud.lp_instance_id:
                raise ValueError('parallel LP instances must differ')
        evidence = json.loads(args.operator_evidence.read_text())
        if cloud.expected_sha != local.get('cloud_expected_sha',local['expected_sha']) or evidence.get('git_sha') != cloud.expected_sha:
            raise ValueError('two-host SHA or operator evidence mismatch')
        # Shadow reads never take trading ownership, even with SSM credentials.
        if cloud.mode == 'shadow':
            if evidence.get('independent_runtime_root') != str(cloud.runtime_root):
                raise ValueError('independent Shadow runtime evidence mismatch')
            required_attestations = ('resources_reviewed',) + (
                ('metadata_isolation_verified',) if credential_backend(cloud) == 'tencent-ssm' else ()
            )
        else:
            required_attestations = ('resources_reviewed',) + (
                ('metadata_isolation_verified',) if credential_backend(cloud)=='tencent-ssm' else ()) + (
                ('old_owner_stopped',) if peer is None else ())
        for field in required_attestations:
            if evidence.get(field) is not True or not evidence.get(field+'_evidence'):
                raise ValueError('operator handoff/isolation/resource evidence missing')
        if not Path(args.remote_config).is_absolute():
            raise ValueError('absolute remote config required')
        release = Path(local['release_root'])
        identity = inspect_prediction_release_checkout(release)
        if identity['git_sha'] != local['expected_sha'] or checked(['git','-C',str(release),'rev-parse','--abbrev-ref','HEAD']).strip() != 'HEAD':
            raise ValueError('local immutable release mismatch')
        action = 'preflight' if args.action == 'readiness' else 'smoke'
        remote = ('cd '+shlex.quote(str(cloud.release_root))+' && '+shlex.join([
            'env', 'PYTHONPATH='+str(cloud.release_root/'src'), 'PYTHONDONTWRITEBYTECODE=1', str(cloud.python),
            '-m','open_trader.prediction_cloud',action,'--config',args.remote_config]))
        output = checked(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ForwardAgent=no',
                          '-o','ConnectTimeout=10',local['ssh_alias'],remote])
        result = json.loads(output)
        identity_before = remote_identity(result, cloud, action)
        if peer is not None and identity_before['lp_instance_isolation']['account_id']!=peer['account_id']:
            raise ValueError('parallel LP wallet mismatch')
        env = {**os.environ, 'PYTHONSAFEPATH':'1', 'PYTHONPATH':str(release)+':'+str(release/'src'),
               'NODE_PATH':str(args.browser_runtime/'node_modules'),
               'OPEN_TRADER_SMOKE_URL':f"http://127.0.0.1:{client_ports(local)[0]}/"}
        runner = args.browser_runtime/'node_modules/.bin/playwright'
        if args.action == 'readiness':
            checked([local['python'],'-c', 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel="chrome",headless=True); b.close(); p.stop()'],cwd=release,env=env)
            checked(['node','-e','const {chromium}=require("playwright"); (async()=>{const b=await chromium.launch({headless:true}); await b.close()})().catch(()=>process.exit(1))'],cwd=release,env=env)
            checked([str(runner),'test','tests/e2e/production-smoke.spec.ts','--config=playwright.config.ts','--project=chromium','--list'],cwd=release,env=env)
            client_release(local)
            if peer is not None and observe_local_lp_peer(local)!=peer:
                raise ValueError('local LP peer changed during readiness')
            print('READY')
        else:
            before = client_operation(local,'status')
            if 'execution_port' in local and (before.get('execution_status') != 'ok'
                or before.get('execution_git_sha') != local['execution_expected_sha']):
                raise ValueError('Air execution unavailable or identity mismatch')
            if before['status'] != 'CONNECTED':
                raise ValueError('local client not connected')
            log = Path(local['runtime_root'])/'gateway.log'
            checked([local['python'],str(release/'scripts/check_production_log.py'),
                     '--file',str(log),'--require-text','frontend_gateway_runtime:'],cwd=release,env=env)
            checked([local['python'],'-m','pytest','-q','-m','browser'],cwd=release,env=env)
            checked([str(runner),'test','tests/e2e/production-smoke.spec.ts','--config=playwright.config.ts','--project=chromium'],cwd=release,env=env)
            if client_operation(local,'status') != before:
                raise ValueError('client changed during browser smoke')
            after = json.loads(checked(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                '-o','ForwardAgent=no','-o','ConnectTimeout=10',local['ssh_alias'],remote]))
            if remote_identity(after, cloud, action) != identity_before:
                raise ValueError('backend changed during browser smoke')
            client_release(local)
            if peer is not None and observe_local_lp_peer(local)!=peer:
                raise ValueError('local LP peer changed during smoke')
            print('HEALTHY')
        return 0
    except Exception as exc:
        # Exception messages from this coordinator contain no SDK payloads.
        print(f'cloud gate: {type(exc).__name__}')
        print(end)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
