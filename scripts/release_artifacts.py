#!/usr/bin/env python3
"""Exact-SHA source releases. Offline helpers are separate from explicit GitHub I/O."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

JOBS = ('gateway', 'legacy', 'account', 'prediction', 'portable')
REPOSITORY = 'raymizzou/open_trader'
# Load only the trusted tooling sibling, never code from a recovered source bundle.
_spec = importlib.util.spec_from_file_location('release_preflight_validation', Path(__file__).with_name('deployment_preflight.py'))
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)
SCHEMA = 'open_trader.source_release.v1'
VERSION = r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?'
HEX = r'[0-9a-f]{40}'


def validate_tag(tag):
    if not isinstance(tag, str) or not re.fullmatch(VERSION, tag):
        raise ValueError('Expected vX.Y.Z or vX.Y.Z-rc.N, with canonical decimal components')
    return tag


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True)+'\n')


def source_identity(repo, tag):
    validate_tag(tag)
    if git(repo, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Clean checkout required')
    ref = 'refs/tags/'+tag
    obj = git(repo, 'rev-parse', '--verify', ref)
    sha = git(repo, 'rev-parse', '--verify', ref+'^{commit}')
    if not re.fullmatch(HEX, sha) or not re.fullmatch(HEX, obj):
        raise ValueError('Expected full Git SHA-1 identities')
    if subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor',sha,'refs/remotes/origin/main'], check=False).returncode:
        raise ValueError('Tag commit is not in fetched main history')
    return {'tag':tag, 'source_sha':sha, 'tag_object_sha':obj, 'tree_sha':git(repo,'rev-parse',sha+'^{tree}')}


def validate_compatibility(value):
    if (not isinstance(value,dict) or set(value) != {'schema_version','reader_generation','contract_generation'}
            or value['schema_version'] != 'open_trader.prediction_service.release.v1'
            or any(type(value[k]) is not int or value[k] < 1 for k in ('reader_generation','contract_generation'))):
        raise ValueError('Invalid strict prediction compatibility manifest')
    return value


def validate_ci(runs, checks, jobs, sha, now, repository=REPOSITORY):
    candidates = [r for r in runs if r.get('head_sha') == sha and r.get('event') == 'push'
                  and r.get('head_branch') == 'main' and r.get('path') == '.github/workflows/ci.yml']
    if not candidates:
        raise ValueError('No exact-SHA ci.yml push/main run')
    run = max(candidates, key=lambda r:(r['id'], r['run_attempt']))
    stamp = preflight.timestamp(run['updated_at'])
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or stamp > now or now-stamp >= timedelta(days=3)):
        raise ValueError('Latest main CI is unsuccessful, pending, or older than evidence retention')
    if (repository != REPOSITORY or run.get('repository', {}).get('full_name') != repository
            or run.get('head_repository', {}).get('full_name') != repository
            or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
        raise ValueError('CI repository or attempt mismatch')
    selected = {}
    for name in ('plan', *JOBS, 'required'):
        matches = [j for j in jobs if j.get('name') == name]
        if len(matches) != 1:
            raise ValueError('Missing or duplicate CI job: '+name)
        job = matches[0]
        if (job.get('run_id') != run['id'] or job.get('head_sha') != sha
                or job.get('status') != 'completed' or job.get('conclusion') != 'success'):
            raise ValueError('CI job not successful for selected run/SHA: '+name)
        selected[name] = job
    # The retained standalone trend job is redundant with legacy and may be skipped.
    if any(j.get('name') not in (*selected, 'trend_curve') for j in jobs):
        raise ValueError('Unexpected CI job')
    required = [c for c in checks if c.get('name') == 'required']
    if len(required) != 1:
        raise ValueError('Missing or duplicate required check')
    check = required[0]
    if (check.get('app', {}).get('id') != 15368 or check.get('check_suite', {}).get('id') != run['check_suite_id']
            or check.get('head_sha') != sha or check.get('status') != 'completed' or check.get('conclusion') != 'success'
            or selected['required'].get('check_run_url') != f'https://api.github.com/repos/{repository}/check-runs/{check["id"]}'):
        raise ValueError('Missing trustworthy exact-SHA required check/job linkage')
    return run


class GitHub:
    """Use the official runner gh client; do not forward tokens across redirects ourselves."""
    def __init__(self, repository):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):
            raise ValueError('Invalid repository')
        self.repository=repository
        self.prefix='repos/'+repository

    def raw(self, path, method='GET', body=None, accept=None):
        cmd=['gh','api','--method',method, self.prefix+'/'+path]
        if accept:cmd += ['-H','Accept: '+accept]
        if body is not None:cmd += ['--input','-']
        return subprocess.check_output(cmd, input=None if body is None else json.dumps(body).encode())

    def get(self,path):return json.loads(self.raw(path))

    def pages(self,path,key=None):
        values=[]
        for page in range(1,1001):
            value=self.get(path+('&' if '?' in path else '?')+f'per_page=100&page={page}')
            items=value[key] if key else value
            values.extend(items)
            if len(items)<100:return values
        raise ValueError('Pagination limit exceeded; refusing incomplete evidence')


def validation_context(repo):
    """Source selection execution is confined to the read-only build job."""
    return {'selections': {scope: preflight.expected_selection(scope, repo) for scope in JOBS},
            'base_images': re.findall(r'^FROM (\S+)', (repo/'Dockerfile.dev').read_text(), re.MULTILINE)}


def trusted_evidence(api, sha, repo, output, now=None, *, context=None):
    now = now or datetime.now(timezone.utc)
    if api.repository != REPOSITORY:
        raise ValueError('Unexpected evidence repository')
    # The writer MUST supply authenticated precomputed data. Only build resolves it.
    if context is None:
        context = validation_context(repo)
    workflow = api.get('actions/workflows/ci.yml')
    if workflow.get('path') != '.github/workflows/ci.yml' or workflow.get('state') != 'active':
        raise ValueError('Trusted CI workflow unavailable')
    runs_path = f'actions/workflows/ci.yml/runs?head_sha={sha}&event=push'
    runs = api.pages(runs_path, 'workflow_runs')
    candidates = [r for r in runs if r.get('head_sha') == sha and r.get('event') == 'push'
                  and r.get('head_branch') == 'main' and r.get('path') == '.github/workflows/ci.yml']
    if not candidates:
        raise ValueError('No exact-SHA main workflow')
    latest = max(candidates, key=lambda r:(r['id'], r['run_attempt']))
    run = api.get(f'actions/runs/{latest["id"]}')
    if run.get('workflow_id') != workflow['id']:
        raise ValueError('CI workflow ID mismatch')
    if (run['id'], run['run_attempt']) != (latest['id'], latest['run_attempt']):
        raise ValueError('CI attempt changed before collection')
    jobs = api.pages(f'actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    checks = api.pages(f'check-suites/{run["check_suite_id"]}/check-runs?filter=latest', 'check_runs')
    validate_ci([run], checks, jobs, sha, now, api.repository)
    artifacts = api.pages(f'actions/runs/{run["id"]}/artifacts', 'artifacts')
    lock_hash = sha256(repo/'uv.lock')
    evidence = []
    for scope in sorted(JOBS):
        name = f'ci-{scope}-{sha}-{run["id"]}-{run["run_attempt"]}'
        matches = [a for a in artifacts if a['name'] == name]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous current-attempt CI evidence: '+name)
        artifact = matches[0]
        created, expires = preflight.timestamp(artifact['created_at']), preflight.timestamp(artifact['expires_at'])
        if (artifact.get('expired') is not False or not created <= now < expires
                or not timedelta(0) < expires-created <= timedelta(days=3)):
            raise ValueError('Expired or invalid CI evidence retention')
        lineage = artifact['workflow_run']
        if lineage['id'] != run['id'] or lineage['head_sha'] != sha or lineage['head_branch'] != 'main':
            raise ValueError('Artifact run/source mismatch')
        data = api.raw(f'actions/artifacts/{artifact["id"]}/zip')
        digest = hashlib.sha256(data).hexdigest()
        if artifact.get('digest') != 'sha256:'+digest:
            raise ValueError('Artifact digest mismatch or missing')
        validate_evidence_zip(data, sha, scope, lock_hash, context, run)
        filename = name+'.zip'
        (output/filename).write_bytes(data)
        evidence.append({'name':filename, 'artifact_id':artifact['id'], 'scope':scope,
                         'sha256':digest, 'digest':artifact['digest']})
    if api.get(f'actions/runs/{run["id"]}') != run:
        raise ValueError('CI changed while collecting evidence')
    selected = validate_ci(api.pages(runs_path, 'workflow_runs'), checks, jobs, sha, now, api.repository)
    if (selected['id'], selected['run_attempt']) != (run['id'], run['run_attempt']):
        raise ValueError('New CI run superseded evidence')
    return {'run_id':run['id'], 'run_attempt':run['run_attempt'], 'workflow_path':run['path'],
            'event':'push', 'branch':'main', 'source_sha':sha, 'check_suite_id':run['check_suite_id'],
            'required_app_id':15368, 'completed_at':run['updated_at'],
            'workflow_id':workflow['id'],
            'required_check_id':next(c['id'] for c in checks if c['name'] == 'required'),
            'required_job_id':next(j['id'] for j in jobs if j['name'] == 'required'),
            'url':f'https://github.com/{api.repository}/actions/runs/{run["id"]}',
            'tested_scopes':sorted(JOBS), 'validation_context':context, 'artifacts':evidence}


def validate_evidence_zip(data, sha, scope, lock_hash, context, run):
    """Validate bytes only. Never import or execute bundled source or its Makefile."""
    if scope not in JOBS or set(context['selections']) != set(JOBS):
        raise ValueError('Incomplete CI scope/selection context')
    files = preflight.read_archive(data)
    required = {'identity.txt','lock-sha256.txt','result.txt','dependency-manifest.json',
                'image.json','test.log','evidence.json'}
    if scope == 'portable':
        required |= {'partition.json','partition.log'}
    if set(files) != required:
        raise ValueError('Unexpected evidence archive contents')
    if files['identity.txt'].decode() != f'source_sha={sha}\nscope={scope}\nTEST_N_LEG=1\n':
        raise ValueError('Evidence identity mismatch')
    if files['result.txt'].decode() != f'scope={scope} source_sha={sha} exit_status=0\n':
        raise ValueError('Evidence does not prove successful test exit')
    if files['lock-sha256.txt'].decode() != f'{lock_hash}  uv.lock\n':
        raise ValueError('Evidence lock mismatch')
    if not files['test.log']:
        raise ValueError('Missing test log')
    # partition.log is stderr and is normally empty on successful collection.
    if run['head_sha'] != sha:
        raise ValueError('Evidence run SHA mismatch')
    preflight.validate_archive(data, scope, run, lock_hash, context['selections'][scope], context['base_images'])


def validate_platform():
    distro=platform.freedesktop_os_release()
    if distro.get('ID')!='ubuntu' or distro.get('VERSION_ID')!='24.04':
        raise ValueError('Artifact baseline requires Ubuntu 24.04')


def validate_installed_identity(result, restored, sha):
    root=str(restored/'src')
    if (result['git_sha']!=sha or result['source_state']!='clean' or result['checkout']!=str(restored)
            or result['package_code_root']!=root or result['prediction_code_root']!=root
            or result['python']!='3.12.14'):
        raise ValueError('Installed runtime or Prediction code identity mismatch')


def validate_runtime(bundle, sha, output):
    validate_platform()
    if platform.python_version()!='3.12.14' or platform.system()!='Linux' or platform.machine()!='x86_64':
        raise ValueError('Validation requires Python 3.12.14 on Linux x86_64')
    if subprocess.check_output(['uv','--version'],text=True).split()[1]!='0.12.19':raise ValueError('uv 0.12.19 required')
    with tempfile.TemporaryDirectory() as tmp:
        restored=Path(tmp)/'restored'
        restore_bundle(bundle,restored,sha)
        env=dict(os.environ,UV_PROJECT_ENVIRONMENT=str(Path(tmp)/'venv'),UV_CACHE_DIR=str(Path(tmp)/'uv-cache'),UV_PYTHON_DOWNLOADS='never',PYTHONDONTWRITEBYTECODE='1')
        # The installation subprocess receives no GitHub token; locked registry downloads only.
        for key in ('GH_TOKEN','GITHUB_TOKEN','GH_ENTERPRISE_TOKEN'):env.pop(key,None)
        commands=[['uv','sync','--locked','--only-group','build','--no-build'],
                  ['uv','sync','--locked','--no-default-groups','--group','build','--extra','dev','--extra','cloud-ssm','--no-install-project','--no-build-isolation'],
                  ['uv','sync','--locked','--offline','--no-default-groups','--group','build','--extra','dev','--extra','cloud-ssm','--no-build-isolation']]
        with (output/'installation.log').open('wb') as log:
            for command in commands:subprocess.run([*command,'--python',sys.executable],cwd=restored,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
        code_root=verify_code_root(restored)
        code="import json,pathlib,platform,open_trader;import open_trader.prediction_release as p;r=p.inspect_prediction_release_checkout(pathlib.Path.cwd());r.update(package_code_root=str(pathlib.Path(open_trader.__file__).resolve().parent.parent),prediction_code_root=str(pathlib.Path(p.__file__).resolve().parent.parent),python=platform.python_version());print(json.dumps(r))"
        result=json.loads(subprocess.check_output([str(Path(tmp)/'venv/bin/python'),'-I','-B','-c',code],cwd=restored,env=env,text=True))
        validate_installed_identity(result,restored,sha)
        dependencies=json.loads(subprocess.check_output([str(Path(tmp)/'venv/bin/python'),'-I','-B','-c',
            "import json,importlib.metadata;print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))"],env=env,text=True))
        if git(restored,'status','--porcelain','--untracked-files=all'):raise ValueError('Installation dirtied source')
        # Store portable assertions, not random temporary absolute paths.
        return {'source_sha':sha,'clean':True,'git_identity_verified':True,'prediction_identity_verified':True,
                'imported_code_root':'<restored>/src','code_root_verified':code_root==str(restored/'src'),
                'python':platform.python_version(),'uv':'0.12.19','dependencies':dependencies,
                'scope':'locked dependency installation and source identity; not Deployment Preflight',
                'excluded':['deployment','browser','production data','real trading']}


def create_bundle(repo, sha, destination):
    if not re.fullmatch(HEX,sha):raise ValueError('Invalid source SHA')
    with tempfile.TemporaryDirectory() as tmp:
        bare=Path(tmp)/'source.git'
        subprocess.run(['git','init','--bare',str(bare)],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['git','-C',str(bare),'fetch','--no-tags',str(repo),sha],check=True,stdout=subprocess.DEVNULL)
        git(bare,'update-ref','refs/heads/release',sha)
        git(bare,'-c','pack.threads=1','-c','core.compression=9','bundle','create',str(destination),'refs/heads/release')


def restore_bundle(bundle, destination, sha):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Restoration destination must not exist')
    heads=subprocess.check_output(['git','bundle','list-heads',str(bundle)],text=True).strip()
    if heads!=f'{sha} refs/heads/release':raise ValueError('Bundle must contain only the exact target ref')
    subprocess.run(['git','clone','--no-checkout',str(bundle),str(destination)],check=True,stdout=subprocess.DEVNULL)
    git(destination,'checkout','--detach',sha)
    if git(destination,'rev-parse','HEAD')!=sha or git(destination,'status','--porcelain','--untracked-files=all'):
        raise ValueError('Restored checkout identity mismatch')


def verify_code_root(repo):
    # Isolated interpreter, no host-installed open_trader or inherited PYTHONPATH.
    code="import sys,pathlib;sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'src'));import open_trader;print(pathlib.Path(open_trader.__file__).resolve().parent.parent)"
    actual=subprocess.check_output([sys.executable,'-I','-B','-c',code,str(repo)],text=True).strip()
    if actual!=str(Path(repo).resolve()/'src'):raise ValueError('Imported code_root differs from recovered source')
    if git(repo,'status','--porcelain','--untracked-files=all'):raise ValueError('Restoration modified source checkout')
    return actual


def verify_checksums(directory, hashes):
    if not isinstance(hashes,dict) or not hashes:raise ValueError('Missing checksums')
    for name,digest in hashes.items():
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',name) or not re.fullmatch(r'[0-9a-f]{64}',digest)
                or (directory/name).is_symlink() or not (directory/name).is_file() or sha256(directory/name)!=digest):
            raise ValueError('Invalid asset/checksum: '+name)


def remote_tag_matches(api, identity):
    remote=api.get('git/ref/tags/'+identity['tag'])
    if remote['object']['sha']!=identity['tag_object_sha']:raise ValueError('Remote tag changed')
    obj=remote['object']
    seen=set()
    while obj['type']=='tag':
        if obj['sha'] in seen or len(seen)>=10:raise ValueError('Invalid annotated tag chain')
        seen.add(obj['sha'])
        obj=api.get('git/tags/'+obj['sha'])['object']
    if obj['type']!='commit' or obj['sha']!=identity['source_sha']:raise ValueError('Remote tag commit differs')
    # API ancestry check also catches main rewrites after the original fetch.
    comparison=api.get(f'compare/{identity["source_sha"]}...main')
    if comparison.get('status') not in ('ahead','identical'):raise ValueError('Source no longer in remote main history')


def build(repo, tag, output, api):
    repo=repo.resolve(); output=output.resolve()
    if output==repo or repo in output.parents:raise ValueError('Assets must be outside checkout')
    if output.exists():raise ValueError('Output must not exist; no asset replacement')
    identity=source_identity(repo,tag)
    if git(repo,'rev-parse','HEAD')!=identity['source_sha']:raise ValueError('Build checkout must be exact tag commit')
    remote_tag_matches(api,identity)
    output.mkdir(parents=True)
    evidence=trusted_evidence(api,identity['source_sha'],repo,output)
    compatibility=validate_compatibility(json.loads((repo/'ops/prediction-service-release.json').read_text()))
    shutil.copyfile(repo/'uv.lock',output/'uv.lock')
    create_bundle(repo,identity['source_sha'],output/'source.bundle')
    verification=validate_runtime(output/'source.bundle',identity['source_sha'],output)
    write_json(output/'artifact-verification.json',verification)
    (output/'INSTALL.txt').write_text('Source-only release, validated on Ubuntu 24.04 Linux x86_64.\n'
        'Requires Git, Python 3.12.14 and uv 0.12.19. No portable virtualenv is included.\n'
        'Verify all SHA256SUMS against the independently reviewed release manifest.\n'
        f'git clone source.bundle open-trader\ncd open-trader\ngit checkout --detach {identity["source_sha"]}\n'
        'export UV_PROJECT_ENVIRONMENT="$(cd .. && pwd)/open-trader-venv"\n'
        'test ! -e "$UV_PROJECT_ENVIRONMENT" || { echo "Choose a new empty environment path"; exit 1; }\n'
        'export UV_PYTHON_DOWNLOADS=never\n'
        '# Set PYTHON_BIN to an independently verified Python 3.12.14 executable.\n'
        'uv sync --python "$PYTHON_BIN" --locked --only-group build --no-build\n'
        'uv sync --python "$PYTHON_BIN" --locked --no-default-groups --group build --extra dev --extra cloud-ssm --no-install-project --no-build-isolation\n'
        'uv sync --python "$PYTHON_BIN" --locked --offline --no-default-groups --group build --extra dev --extra cloud-ssm --no-build-isolation\n'
        'Installation requires access to the locked registries; dependencies are not bundled.\n'
        'Run scripts/verify_release_artifacts.py from a trusted reviewed checkout first.\n'
        'Artifact verification is not Deployment Preflight, host readiness, production smoke or deployment approval.\n')
    write_json(output/'ci-evidence.json',evidence)
    manifest={'schema_version':SCHEMA,**identity,'repository':api.repository,'lock_sha256':sha256(output/'uv.lock'),
              'format':'git-bundle-with-full-target-history','platform':{'os':'linux','architecture':'x86_64','runner':'ubuntu-24.04'},
              'runtime':{'python':'3.12.14','uv':'0.12.19'},'service_scope':['gateway','legacy','account','prediction'],
              'prediction_compatibility':compatibility,'ci':evidence,'verification':verification,
              'builder':{'source_sha':git(repo,'rev-parse','HEAD'),'workflow_run_id':os.environ.get('GITHUB_RUN_ID'),
                         'workflow_run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT')},
              'assets':{p.name:sha256(p) for p in sorted(output.iterdir())}}
    write_json(output/'release-manifest.json',manifest)
    hashes={p.name:sha256(p) for p in sorted(output.iterdir())}
    (output/'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name,digest in hashes.items()))
    remote_tag_matches(api,identity)
    return manifest


def asset_plan(existing, hashes, read_asset):
    seen=set()
    for asset in existing:
        name=asset['name']
        if name in seen or name not in hashes:raise ValueError('Unexpected or duplicate existing asset')
        seen.add(name)
        if hashlib.sha256(read_asset(asset)).hexdigest()!=hashes[name]:raise ValueError('Conflicting existing asset: '+name)
    return sorted(set(hashes)-seen)


def upload_draft(directory, api, *, expected_manifest_sha256=None, expected_tag=None, expected_sha=None):
    # Authenticated job output binds precomputed data before parsing/restoring artifacts.
    if (not expected_manifest_sha256 or not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_sha256)
            or sha256(directory/'release-manifest.json') != expected_manifest_sha256):
        raise ValueError('Writer requires trusted build manifest digest')
    validate_tag(expected_tag)
    if not expected_sha or not re.fullmatch(HEX, expected_sha):
        raise ValueError('Writer requires independently resolved source SHA')
    selected = json.loads((directory/'release-manifest.json').read_text())
    if not isinstance(selected, dict) or selected.get('tag') != expected_tag or selected.get('source_sha') != expected_sha:
        raise ValueError('Build manifest differs from requested tag/source SHA')
    from verify_release_artifacts import verify
    with tempfile.TemporaryDirectory() as tmp:manifest=verify(directory,Path(tmp)/'restored',execute_code=False)
    if manifest['repository']!=api.repository:raise ValueError('Artifact repository mismatch')
    remote_tag_matches(api,manifest)
    context = manifest['ci'].get('validation_context')
    if not isinstance(context, dict) or set(context.get('selections', {})) != set(JOBS):
        raise ValueError('Writer requires authenticated precomputed CI context')
    # A later failed/running CI attempt invalidates an earlier build before any writes.
    with tempfile.TemporaryDirectory() as tmp:
        current=trusted_evidence(api,manifest['source_sha'],directory,Path(tmp), context=context)
    if current!=manifest['ci']:raise ValueError('CI evidence changed before upload')
    tag=manifest['tag']
    releases=api.pages('releases')
    matches=[r for r in releases if r['tag_name']==tag]
    if len(matches)>1:raise ValueError('Ambiguous existing release')
    if matches:
        release=matches[0]
        if not release['draft'] or release.get('immutable') or release.get('target_commitish')!=manifest['source_sha']:
            raise ValueError('Existing release is published, immutable or has conflicting target')
    else:
        remote_tag_matches(api,manifest)
        release=json.loads(api.raw('releases','POST',{'tag_name':tag,'target_commitish':manifest['source_sha'],
            'name':tag,'draft':True,'prerelease':'-rc.' in tag,
            'body':'Exact-SHA source artifacts. Artifact validation only; not deployment approval.\n'
                   'Do not publish before repository protections and immutable releases are separately approved and verified.\n'
                   'All expected assets must be present and verified before manual publication.'}))
    rid=release['id']
    hashes={p.name:sha256(p) for p in directory.iterdir() if p.is_file()}
    existing=api.pages(f'releases/{rid}/assets')
    missing=asset_plan(existing,hashes,lambda a:api.raw(f'releases/assets/{a["id"]}',accept='application/octet-stream'))
    for name in missing:
        remote_tag_matches(api,manifest)
        current=api.get(f'releases/{rid}')
        if (not current['draft'] or current.get('immutable') or current['tag_name']!=tag
                or current.get('target_commitish')!=manifest['source_sha']):raise ValueError('Release identity changed during upload; stop')
        # No --clobber, no publish call, never synthesize a tag.
        subprocess.run(['gh','release','upload',tag,str(directory/name),'--repo',api.repository],check=True)
    remote_tag_matches(api,manifest)
    if asset_plan(api.pages(f'releases/{rid}/assets'),hashes,lambda a:api.raw(f'releases/assets/{a["id"]}',accept='application/octet-stream')):
        raise ValueError('Incomplete draft upload')
    final=api.get(f'releases/{rid}')
    if (not final['draft'] or final.get('immutable') or final['tag_name']!=tag
            or final.get('target_commitish')!=manifest['source_sha']):raise ValueError('Release identity unexpectedly changed')
    print(final['html_url'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['build','upload-draft'])
    parser.add_argument('--repository',required=True)
    parser.add_argument('--tag')
    parser.add_argument('--expected-manifest-sha256')
    parser.add_argument('--expected-tag')
    parser.add_argument('--expected-sha')
    parser.add_argument('--repo',type=Path,default=Path.cwd())
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();api=GitHub(args.repository)
    if args.command=='build':build(args.repo,args.tag,args.output,api)
    else:upload_draft(args.output.resolve(),api,expected_manifest_sha256=args.expected_manifest_sha256,expected_tag=args.expected_tag,expected_sha=args.expected_sha)


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,subprocess.CalledProcessError) as error:raise SystemExit(f'Release rejected: {error}')
