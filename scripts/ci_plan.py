#!/usr/bin/env python3
"""Full-backend CI planning, exact-SHA evidence and fail-closed verdict."""
import argparse
import json
import os
import re
import subprocess
import sys

BACKEND_SCOPES = ('gateway', 'legacy', 'account', 'prediction')
REQUIRED_SCOPES = BACKEND_SCOPES + ('portable',)
SCOPES = REQUIRED_SCOPES + ('trend-curve',)


def route(paths):
    # Paths remain diagnostic evidence only: every branch/PR/main candidate
    # receives the complete backend partition, including paused N-leg tests.
    # Legacy includes trend-curve, so its standalone job remains unselected.
    return {'scopes': list(REQUIRED_SCOPES), 'test_n_leg': '1',
            'reason': 'full backend and portable coverage for every CI candidate'}


def changed_paths(base, head, cwd=None):
    # No rename detection: both old and new paths route; NUL protects odd filenames.
    raw = subprocess.check_output(['git', 'diff', '--name-only', '--no-renames', '-z', base, head, '--'], cwd=cwd)
    return [path.decode('utf-8', errors='surrogateescape') for path in raw.split(b'\0') if path]


def required(needs):
    try:
        if needs['plan']['result'] != 'success':
            return False, 'plan did not succeed'
        plan = json.loads(needs['plan']['outputs']['plan'])
        scopes = plan['scopes']
        if (scopes != list(REQUIRED_SCOPES) or plan['test_n_leg'] != '1'
                or not isinstance(plan['reason'], str) or not plan['reason']):
            return False, 'invalid full-backend plan'
        for scope in SCOPES:
            result = needs[scope.replace('-', '_')]['result']
            expected = 'success' if scope in scopes else 'skipped'
            if result != expected:
                return False, f'{scope}: expected {expected}, got {result}'
        return True, 'all backend and portable checks succeeded with N-leg coverage'
    except (KeyError, TypeError, ValueError):
        return False, 'missing or malformed plan/job results'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'required'])
    args = parser.parse_args()
    if args.command == 'required':
        try:
            ok, reason = required(json.load(sys.stdin))
        except ValueError:
            ok, reason = False, 'invalid job-result JSON'
        print(('PASS: ' if ok else 'FAIL: ') + reason)
        return 0 if ok else 1
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if head != os.environ['GITHUB_SHA']:
        raise SystemExit('checkout SHA does not match event candidate')
    base = os.environ.get('BASE_SHA', '')
    if not re.fullmatch('[0-9a-f]{40}', base) or base == '0' * 40:
        paths = []
    else:
        try:
            paths = changed_paths(base, head)
        except subprocess.CalledProcessError:
            paths = []  # Missing history cannot reduce backend coverage.
    plan = route(paths)
    plan.update({'sha': head, 'base_sha': base, 'changed_paths': paths})
    encoded = json.dumps(plan, ensure_ascii=True, separators=(',', ':'))
    print(encoded)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'plan={encoded}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f'## CI routing\nCandidate SHA: `{head}`\n\nBase SHA: `{base}`\n\n'
                     f'Scopes: {", ".join(plan["scopes"]) or "none"}; TEST_N_LEG={plan["test_n_leg"]}\n\n'
                     f'{plan["reason"]}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
