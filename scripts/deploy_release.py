#!/usr/bin/env python3
"""Forward source-release entrypoint. Existing installers retain rollback duties.

Run fresh Host Readiness first, and Production Smoke afterward. This wrapper
checks trusted CI and source/runtime identity immediately before one explicitly
selected installer action. It neither grants deployment permission nor rolls back.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run_preflight(args):
    command = [str(args.python), '-B', str(args.release_root / 'scripts/deployment_preflight.py'),
               '--expected-sha', args.expected_sha, '--release-root', str(args.release_root),
               '--python', str(args.python)]
    for extra in args.extra:
        command.extend(['--extra', extra])
    result = subprocess.run(command, cwd=args.release_root, check=False)
    if result.returncode:
        raise ValueError('deployment preflight failed; no installer was run')


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError('an absolute path is required')
    # Keep the virtualenv interpreter spelling: resolving its executable symlink
    # would select the base interpreter and lose the environment under review.
    return Path(os.path.abspath(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--release-root', required=True, type=absolute_path)
    parser.add_argument('--runtime-root', required=True, type=absolute_path)
    parser.add_argument('--python', required=True, type=absolute_path)
    parser.add_argument('--extra', action='append', default=[], choices=['cloud-ssm', 'browser'])
    kinds = parser.add_subparsers(dest='kind', required=True)
    dashboard = kinds.add_parser('dashboard')
    dashboard.add_argument('--mode', required=True, choices=['gateway', 'legacy', 'stack'])
    account = kinds.add_parser('account')
    account.add_argument('--evidence-out', type=absolute_path)
    prediction = kinds.add_parser('prediction-launchd')
    prediction.add_argument('--mode', required=True, choices=['production', 'shadow'])
    prediction.add_argument('--config', type=absolute_path)
    prediction.add_argument('--n-leg-paused', choices=['0', '1'])
    cloud = kinds.add_parser('prediction-systemd')
    cloud.add_argument('--config', required=True, type=absolute_path)
    cloud.add_argument('--action', choices=['install', 'start'], default='install')
    args = parser.parse_args(argv)
    args.release_root = args.release_root.resolve()
    args.runtime_root = args.runtime_root.resolve()
    try:
        common = ['--repo-root', str(args.release_root), '--runtime-root', str(args.runtime_root),
                  '--python', str(args.python)]
        config_bytes = None
        if args.kind == 'prediction-systemd':
            config_bytes = args.config.read_bytes()
            config = json.loads(config_bytes)
            expected = {'expected_sha': args.expected_sha, 'release_root': str(args.release_root),
                        'runtime_root': str(args.runtime_root), 'python': str(args.python)}
            if any(config.get(key) != value for key, value in expected.items()):
                raise ValueError('cloud config does not bind the selected SHA, release, runtime and interpreter')
            command = ['bash', str(args.release_root / 'scripts/prediction-systemd.sh'),
                       args.action, '--config', str(args.config)]
        elif args.kind == 'dashboard':
            command = ['bash', str(args.release_root / 'scripts/install_dashboard_launchd.sh'),
                       *common, '--mode', args.mode]
        elif args.kind == 'account':
            command = ['bash', str(args.release_root / 'scripts/install_account_release.sh'), *common]
            if args.evidence_out:
                command.extend(['--evidence-out', str(args.evidence_out)])
        else:
            command = ['bash', str(args.release_root / 'scripts/install_prediction_service_launchd.sh'),
                       *common, '--mode', args.mode, '--expected-sha', args.expected_sha]
            if args.config:
                command.extend(['--config', str(args.config)])
            if args.n_leg_paused is not None:
                command.extend(['--n-leg-paused', args.n_leg_paused])
        run_preflight(args)
        if config_bytes is not None and args.config.read_bytes() != config_bytes:
            raise ValueError('cloud config changed during preflight')
        environment = dict(os.environ, OPEN_TRADER_PYTHON=str(args.python),
                           PYTHONPATH=str(args.release_root / 'src'), PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run(command, cwd=args.release_root, env=environment, check=False).returncode
    except (OSError, ValueError) as error:
        print(f'BLOCKED: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
