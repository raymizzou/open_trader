#!/usr/bin/env python3
"""Restore an exact-SHA source bundle into a new directory without modifying it."""
import argparse
import json
from pathlib import Path
import re
import sys
from release_artifacts import (HEX, SCHEMA, JOBS, REPOSITORY, git, restore_bundle, sha256, validate_compatibility,
                               validate_tag, verify_checksums, verify_code_root, validate_evidence_zip)


def verify(directory, destination, *, execute_code=False, expected_sha=None):
    directory=Path(directory).resolve()
    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):raise ValueError('Only regular release asset files allowed')
    entries=(directory/'SHA256SUMS').read_text().splitlines()
    sums={}
    for line in entries:
        if not re.fullmatch(r'[0-9a-f]{64}  [A-Za-z0-9][A-Za-z0-9_.-]*',line):raise ValueError('Malformed checksum file')
        digest,name=line.split('  ')
        if name in sums:raise ValueError('Duplicate checksum entry')
        sums[name]=digest
    verify_checksums(directory,sums)
    if {p.name for p in directory.iterdir()} != set(sums)|{'SHA256SUMS'}:raise ValueError('Unexpected release assets')
    manifest=json.loads((directory/'release-manifest.json').read_text())
    if execute_code and (not expected_sha or not re.fullmatch(HEX,expected_sha) or manifest.get('source_sha')!=expected_sha):
        raise ValueError('Executing restored code requires an independently trusted expected SHA')
    if manifest['schema_version']!=SCHEMA:raise ValueError('Unsupported manifest schema')
    validate_tag(manifest['tag'])
    if not re.fullmatch(HEX,manifest['source_sha']):raise ValueError('Invalid source identity')
    if manifest['assets']!={k:v for k,v in sums.items() if k!='release-manifest.json'}:raise ValueError('Manifest/checksum mismatch')
    if manifest['lock_sha256']!=sha256(directory/'uv.lock'):raise ValueError('Lock identity mismatch')
    validate_compatibility(manifest['prediction_compatibility'])
    evidence=json.loads((directory/'ci-evidence.json').read_text())
    if evidence!=manifest['ci'] or evidence['source_sha']!=manifest['source_sha']:raise ValueError('CI manifest identity mismatch')
    if (manifest.get('repository') != REPOSITORY or evidence.get('workflow_path') != '.github/workflows/ci.yml'
            or evidence.get('event') != 'push' or evidence.get('branch') != 'main'
            or evidence.get('required_app_id') != 15368
            or any(type(evidence.get(key)) is not int or evidence[key] <= 0
                   for key in ('run_id','run_attempt','workflow_id','check_suite_id','required_check_id','required_job_id'))):
        raise ValueError('CI provenance record mismatch')
    core={'source.bundle','uv.lock','INSTALL.txt','ci-evidence.json','artifact-verification.json','installation.log','release-manifest.json','SHA256SUMS'}
    artifacts=[a['name'] for a in evidence['artifacts']]
    if len(artifacts)!=len(set(artifacts)) or set(artifacts)&core:raise ValueError('Duplicate artifact names')
    if {p.name for p in directory.iterdir()}!=core|set(artifacts):raise ValueError('Missing core or unexpected assets')
    if (evidence.get('tested_scopes') != sorted(JOBS)
            or sorted(a['scope'] for a in evidence['artifacts']) != sorted(JOBS)):
        raise ValueError('Release requires all five CI scopes')
    run = {'head_sha':manifest['source_sha'], 'id':evidence['run_id'], 'run_attempt':evidence['run_attempt']}
    for artifact in evidence['artifacts']:
        expected_name = f"ci-{artifact['scope']}-{manifest['source_sha']}-{run['id']}-{run['run_attempt']}.zip"
        if artifact['name'] != expected_name or artifact.get('digest') != 'sha256:'+artifact['sha256']:
            raise ValueError('Evidence artifact name/digest identity mismatch')
        if artifact['name'] not in manifest['assets'] or artifact['sha256']!=manifest['assets'][artifact['name']]:raise ValueError('Evidence checksum mismatch')
        validate_evidence_zip((directory/artifact['name']).read_bytes(),manifest['source_sha'],artifact['scope'],manifest['lock_sha256'],evidence['validation_context'],run)
    restore_bundle(directory/'source.bundle',destination,manifest['source_sha'])
    if git(destination,'rev-parse','HEAD^{tree}')!=manifest['tree_sha']:raise ValueError('Restored tree differs')
    verification=json.loads((directory/'artifact-verification.json').read_text())
    if verification!=manifest['verification'] or verification['source_sha']!=manifest['source_sha'] or not all(verification[k] is True for k in ('clean','git_identity_verified','prediction_identity_verified','code_root_verified')):raise ValueError('Invalid artifact verification record')
    if sha256(destination/'uv.lock')!=manifest['lock_sha256']:raise ValueError('Restored lock differs')
    compatibility=json.loads((destination/'ops/prediction-service-release.json').read_text())
    if validate_compatibility(compatibility)!=manifest['prediction_compatibility']:raise ValueError('Restored generations differ')
    code_root=verify_code_root(destination) if execute_code else None
    if git(destination,'status','--porcelain','--untracked-files=all'):raise ValueError('Restored checkout dirty')
    if git(destination,'rev-parse','HEAD')!=manifest['source_sha']:raise ValueError('Restored Git identity differs')
    print(json.dumps({'source_sha':manifest['source_sha'],'code_root':code_root,'clean':True}))
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--expected-sha',required=True,help='Independently authenticated full Git commit SHA')
    args=parser.parse_args()
    verify(args.directory,args.destination.resolve(),execute_code=True,expected_sha=args.expected_sha)


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError) as error:sys.exit(f'Artifact verification rejected: {error}')
