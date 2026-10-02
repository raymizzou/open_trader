"""Offline trust-boundary regressions; never contact GitHub or production."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location('deployment_preflight', Path(__file__).parents[1] / 'scripts/deployment_preflight.py')
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
SHA = 'a' * 40
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def pack(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, value in files.items():
            archive.writestr(name, value if isinstance(value, bytes) else json.dumps(value))
    return stream.getvalue()


def fixture_evidence():
    run = dict(id=22, run_number=4, run_attempt=2, workflow_id=11,
               path=preflight.WORKFLOW, event='push', head_branch='main', head_sha=SHA,
               status='completed', conclusion='success', check_suite_id=33,
               repository={'full_name': preflight.REPOSITORY}, head_repository={'full_name': preflight.REPOSITORY})
    jobs = [dict(id=100+i, name=name, run_id=22, head_sha=SHA, status='completed',
                 conclusion='success', check_run_url=f'https://api.github.com/repos/{preflight.REPOSITORY}/check-runs/{100+i}')
            for i, name in enumerate(('plan', *preflight.SCOPES, 'required'))]
    checks = [dict(id=job['id'], name=job['name'], head_sha=SHA, status='completed',
                   conclusion='success', app={'id':15368}, check_suite={'id':33}) for job in jobs]
    return run, jobs, checks


class FakeAPI:
    def __init__(self, run, jobs, checks):
        self.run, self.jobs, self.checks = run, jobs, checks
        self.runs = [run]
        self.artifacts, self.downloads, self.calls = [], {}, []
        self.tip = SHA

    def json(self, path):
        self.calls.append(path)
        if path.endswith('/git/ref/heads/main'):
            return {'object': {'sha': self.tip}}
        if '/compare/' in path:
            return {'status':'ahead', 'merge_base_commit':{'sha': SHA}}
        if path.endswith('/actions/workflows/ci.yml'):
            return {'id':11, 'path':preflight.WORKFLOW, 'state':'active'}
        if path.endswith('/actions/runs/22'):
            return copy.deepcopy(self.run)
        raise AssertionError(path)

    def items(self, path, key):
        self.calls.append(path)
        return copy.deepcopy({'workflow_runs':self.runs, 'jobs':self.jobs,
                              'check_runs':self.checks, 'artifacts':self.artifacts}[key])

    def download(self, artifact_id):
        self.calls.append(f'download:{artifact_id}')
        return self.downloads[artifact_id]


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    (tmp_path / 'uv.lock').write_text('locked bytes')
    (tmp_path / 'Dockerfile.dev').write_text('FROM python:3.12.14@sha256:' + 'c'*64 + '\n')
    run, jobs, checks = fixture_evidence()
    api = FakeAPI(run, jobs, checks)
    selection = {'roots':['tests/test_one.py'], 'marker':'not pressure and not browser', 'keyword':''}
    monkeypatch.setattr(preflight, 'expected_selection', lambda scope, root: selection)
    lock = hashlib.sha256((tmp_path/'uv.lock').read_bytes()).hexdigest()
    for i, scope in enumerate(preflight.SCOPES):
        manifest = dict(role='test-only; synthetic Git snapshot is not a release SHA', source_sha=SHA,
                        source_state='clean', python='3.12.14', platform='Linux/x86_64', lock_sha256=lock,
                        base_images=['python:3.12.14@sha256:'+'c'*64], dependencies=[['pytest','8.0']])
        image = [{'Id':'sha256:'+'d'*64, 'Config':{'Labels':{'org.open-trader.image-role':'test-only'},
                  'Env':[f'OPEN_TRADER_TEST_SOURCE_SHA={SHA}', 'OPEN_TRADER_TEST_SOURCE_STATE=clean']}}]
        files = {'dependency-manifest.json':json.dumps(manifest).encode(), 'image.json':json.dumps(image).encode()}
        env = dict(runner_os='Linux', runner_arch='X64', dependency_manifest_sha256=hashlib.sha256(files['dependency-manifest.json']).hexdigest(),
                   image_inspect_sha256=hashlib.sha256(files['image.json']).hexdigest())
        if scope == 'portable':
            partition = dict(schema_version=1, marker=selection['marker'], complete_count=4, partition_count=4,
                             counts={name:1 for name in preflight.SCOPES if name!='portable'},
                             complete_sha256='e'*64, partition_sha256='e'*64, status='success')
            files['partition.json'] = json.dumps(partition).encode()
            env['partition_sha256'] = hashlib.sha256(files['partition.json']).hexdigest()
        files['evidence.json'] = dict(schema_version=1, source_sha=SHA, repository=preflight.REPOSITORY,
            workflow_ref=f'{preflight.REPOSITORY}/{preflight.WORKFLOW}@refs/heads/main', event_name='push',
            ref='refs/heads/main', run_id='22', run_attempt='2', scope=scope, test_n_leg='1',
            workers=2 if scope=='prediction' else 1, lock_sha256=lock, status='success', exit_status=0,
            selection=selection, environment=env, evidence_role='test-only')
        archive = pack(files)
        artifact = dict(id=i+1, name=f'ci-{scope}-{SHA}-22-2', expired=False,
                        created_at='2026-10-01T12:00:00Z', expires_at='2026-10-04T12:00:00Z',
                        digest='sha256:'+hashlib.sha256(archive).hexdigest(),
                        workflow_run={'id':22, 'head_sha':SHA, 'head_branch':'main'})
        api.artifacts.append(artifact)
        api.downloads[i+1] = archive
    return tmp_path, api


def test_valid_trusted_main_push_and_current_attempt(evidence):
    root, api = evidence
    report = preflight.verify_ci(api, SHA, root, NOW)
    assert report['run_id'] == 22
    assert report['run_attempt'] == 2
    assert report['artifact_role'] == 'test-only'
    assert any('/attempts/2/jobs' in path for path in api.calls)


@pytest.mark.parametrize('field,value', [('event','pull_request'), ('head_branch','feature'), ('head_sha','b'*40),
    ('path','.github/workflows/other.yml'), ('workflow_id',12), ('status','in_progress'), ('conclusion','failure'),
    ('repository',{'full_name':'attacker/fork'}), ('head_repository',{'full_name':'attacker/fork'})])
def test_rejects_wrong_run_identity(evidence, field, value):
    root, api = evidence
    api.run[field] = value
    with pytest.raises(preflight.PreflightError):
        preflight.verify_ci(api, SHA, root, NOW)


def test_latest_failed_run_cannot_reuse_older_green(evidence):
    root, api = evidence
    api.runs.append(dict(api.run, id=23, run_number=5, conclusion='failure'))
    with pytest.raises(preflight.PreflightError):
        preflight.verify_ci(api, SHA, root, NOW)


@pytest.mark.parametrize('mutation', ['app','suite','sha','check_id','duplicate','skipped_job','missing_job'])
def test_required_check_and_jobs_are_bound_to_actions_suite(evidence, mutation):
    root, api = evidence
    required = api.checks[-1]
    if mutation == 'app': required['app']['id']=1
    elif mutation == 'suite': required['check_suite']['id']=34
    elif mutation == 'sha': required['head_sha']='b'*40
    elif mutation == 'check_id': required['id']=999
    elif mutation == 'duplicate': api.checks.append(copy.deepcopy(required))
    elif mutation == 'skipped_job': api.jobs[1]['conclusion']='skipped'
    else: api.jobs.pop(1)
    with pytest.raises(preflight.PreflightError):
        preflight.verify_ci(api, SHA, root, NOW)


@pytest.mark.parametrize('mutation', ['expired','expiry','retention','digest','missing','duplicate','old_attempt','wrong_run'])
def test_artifacts_fail_closed(evidence, mutation):
    root, api = evidence
    artifact = api.artifacts[0]
    if mutation == 'expired': artifact['expired']=True
    elif mutation == 'expiry': artifact['expires_at']='2026-10-02T12:00:00Z'
    elif mutation == 'retention': artifact['expires_at']='2026-10-05T12:00:00Z'
    elif mutation == 'digest': artifact['digest']='sha256:'+'0'*64
    elif mutation == 'missing': api.artifacts.pop(0)
    elif mutation == 'duplicate': api.artifacts.append(copy.deepcopy(artifact))
    elif mutation == 'old_attempt': artifact['name']=artifact['name'][:-1]+'1'
    else: artifact['workflow_run']['id']=21
    with pytest.raises(preflight.PreflightError):
        preflight.verify_ci(api, SHA, root, NOW)


def mutate_metadata(api, modify):
    with zipfile.ZipFile(io.BytesIO(api.downloads[1])) as archive:
        files = {name:archive.read(name) for name in archive.namelist()}
    metadata = json.loads(files['evidence.json'])
    modify(metadata)
    files['evidence.json']=metadata
    api.downloads[1]=pack(files)
    api.artifacts[0]['digest']='sha256:'+hashlib.sha256(api.downloads[1]).hexdigest()


@pytest.mark.parametrize('field,value', [('run_attempt','1'), ('source_sha','b'*40), ('test_n_leg','0'),
    ('workers',2), ('lock_sha256','0'*64), ('evidence_role','deployment'), ('exit_status',1),
    ('selection',{'roots':[]}), ('environment',{})])
def test_trusted_archive_metadata_must_match_full_contract(evidence, field, value):
    root, api=evidence
    mutate_metadata(api, lambda metadata: metadata.update({field:value}))
    with pytest.raises(preflight.PreflightError):
        preflight.verify_ci(api, SHA, root, NOW)


def test_unsafe_and_duplicate_zip_members_rejected():
    with pytest.raises(preflight.PreflightError):
        preflight.read_archive(pack({'../evidence.json':{}}))
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('evidence.json','{}')
        with pytest.warns(UserWarning): archive.writestr('evidence.json','{}')
    with pytest.raises(preflight.PreflightError): preflight.read_archive(stream.getvalue())


def test_pagination_is_complete_and_host_fixed(monkeypatch):
    calls=[]
    def fake_run(command, **kwargs):
        calls.append(command)
        page=1 if '&page=1' in command[-1] else 2
        payload={'total_count':101, 'jobs':[{'id':i} for i in (range(100) if page==1 else [100])]}
        return subprocess.CompletedProcess(command,0,json.dumps(payload).encode(),b'')
    monkeypatch.setattr(preflight.subprocess,'run',fake_run)
    assert len(preflight.GitHub().items(f'repos/{preflight.REPOSITORY}/jobs','jobs'))==101
    assert all(c[:5]==['gh','api','--hostname','github.com','--method'] for c in calls)


def test_clean_detached_release_required(tmp_path):
    def git(*args): return subprocess.check_output(['git','-C',str(tmp_path),*args],stderr=subprocess.DEVNULL).decode().strip()
    git('init'); git('config','user.name','Test'); git('config','user.email','test@example.invalid')
    (tmp_path/'uv.lock').write_text('test')
    git('add','uv.lock'); git('commit','-m','test')
    sha=git('rev-parse','HEAD')
    with pytest.raises(preflight.PreflightError): preflight.verify_checkout(tmp_path,sha)
    git('checkout','--detach')
    preflight.verify_checkout(tmp_path,sha)
    (tmp_path/'uv.lock').write_text('changed')
    with pytest.raises(preflight.PreflightError): preflight.verify_checkout(tmp_path,sha)
    git('update-index','--assume-unchanged','uv.lock')
    with pytest.raises(preflight.PreflightError): preflight.verify_checkout(tmp_path,sha)


def test_cli_rejects_unsigned_evidence_file_and_requires_runtime_python():
    with pytest.raises(SystemExit): preflight.main(['--expected-sha',SHA,'--release-root','/tmp/release','--evidence-json','proof.json'])
    with pytest.raises(SystemExit): preflight.main(['--expected-sha',SHA,'--release-root','/tmp/release'])


def runtime_lock():
    return {'requires-python':'>=3.12', 'package':[
        {'name':'open-trader','version':'0.1.0','source':{'editable':'.'},'dependencies':[{'name':'foo'}],
         'optional-dependencies':{'cloud-ssm':[{'name':'cloud'}]}},
        {'name':'foo','version':'1.0','source':{'registry':'https://pypi.org/simple'},
         'dependencies':[{'name':'linux-only','marker':"sys_platform == 'linux'"}]},
        {'name':'linux-only','version':'2.0','source':{'registry':'https://pypi.org/simple'}},
        {'name':'cloud','version':'3.0','source':{'registry':'https://pypi.org/simple'}}]}


def test_runtime_graph_applies_platform_and_exact_versions():
    from packaging.markers import default_environment
    env=default_environment(); env.update(python_version='3.12',python_full_version='3.12.14',sys_platform='linux')
    result=preflight.verify_runtime_packages(runtime_lock(), {'foo':'1.0','linux-only':'2.0'}, env, [])
    assert result == {'foo':'1.0','linux-only':'2.0'}
    env['sys_platform']='darwin'
    assert preflight.verify_runtime_packages(runtime_lock(),{'foo':'1.0'},env,[])=={'foo':'1.0'}


@pytest.mark.parametrize('mutation',['missing','version','python','ambiguous','extra','unknown_extra'])
def test_runtime_unverifiable_or_mismatched_dependencies_block(mutation):
    from packaging.markers import default_environment
    env=default_environment(); env.update(python_version='3.12',python_full_version='3.12.14',sys_platform='linux')
    lock=runtime_lock(); packages={'foo':'1.0','linux-only':'2.0'}; extras=[]
    if mutation=='missing': packages.pop('foo')
    elif mutation=='version': packages['foo']='9.0'
    elif mutation=='python': env['python_full_version']='3.11.9';env['python_version']='3.11'
    elif mutation=='ambiguous': lock['package'].append(dict(lock['package'][1],version='1.1'))
    elif mutation=='extra': extras=['cloud-ssm']
    else: extras=['unknown']
    with pytest.raises(preflight.PreflightError): preflight.verify_runtime_packages(lock,packages,env,extras)


def test_selected_main_sha_may_be_retained_ancestor(evidence):
    root, api = evidence
    api.tip='b'*40
    assert preflight.verify_ci(api,SHA,root,NOW)['run_id']==22
    original=api.json
    def diverged(path):
        if '/compare/' in path: return {'status':'diverged','merge_base_commit':{'sha':'c'*40}}
        return original(path)
    api.json=diverged
    with pytest.raises(preflight.PreflightError): preflight.verify_ci(api,SHA,root,NOW)


def test_rerun_started_during_verification_invalidates_evidence(evidence):
    root,api=evidence
    original=api.json; calls=0
    def changing(path):
        nonlocal calls
        result=original(path)
        if path.endswith('/actions/runs/22'):
            calls+=1
            if calls==2: result['run_attempt']=3;result['status']='in_progress'
        return result
    api.json=changing
    with pytest.raises(preflight.PreflightError): preflight.verify_ci(api,SHA,root,NOW)


def test_read_only_runtime_probe_uses_selected_interpreter(monkeypatch,tmp_path):
    python=tmp_path/'python';python.touch()
    calls=[]
    monkeypatch.setattr(preflight,'execute',lambda cmd: calls.append(cmd) or b'{"source_root":"release"}')
    assert preflight.probe_runtime(str(python),tmp_path,['cloud-ssm'])=={'source_root':'release'}
    assert calls[0][:4]==[str(python),'-I','-B','-c']
    assert calls[0][-2:]==[str(tmp_path),'["cloud-ssm"]']
    for key in ('PYTHONHOME','PYTHONUSERBASE'):
        monkeypatch.setenv(key,'/unexpected/runtime')
        with pytest.raises(preflight.PreflightError): preflight.probe_runtime(str(python),tmp_path,[])
        monkeypatch.delenv(key)
    (tmp_path/'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    preflight.verify_runtime_isolation(tmp_path,Path('/system'))
    (tmp_path/'pyvenv.cfg').write_text('include-system-site-packages = true\n')
    with pytest.raises(preflight.PreflightError): preflight.verify_runtime_isolation(tmp_path,Path('/system'))
    with pytest.raises(preflight.PreflightError): preflight.verify_runtime_isolation(tmp_path,tmp_path)


def test_current_uv_lock_has_unambiguous_platform_runtime_closure():
    import tomllib
    from packaging.markers import default_environment
    lock=tomllib.loads((Path(__file__).parents[1]/'uv.lock').read_text())
    installed={p['name']:p['version'] for p in lock['package']}
    env=default_environment();env.update(python_version='3.12',python_full_version='3.12.14',sys_platform='linux')
    expected=preflight.verify_runtime_packages(lock,installed,env,['cloud-ssm','browser'])
    assert expected['tencentcloud-sdk-python-ssm']=='3.1.160'
    assert 'playwright' in expected and 'ortools' in expected
    assert 'pytest' not in expected


def test_cli_never_passes_when_source_changes_during_verification(monkeypatch,tmp_path,capsys):
    calls=0
    def checkout(*args):
        nonlocal calls
        calls+=1
        if calls==2: raise preflight.PreflightError('Release checkout is dirty')
    monkeypatch.setattr(preflight,'verify_checkout',checkout)
    monkeypatch.setattr(preflight,'probe_runtime',lambda *args:{'lock_sha256':'ok'})
    monkeypatch.setattr(preflight,'verify_ci',lambda *args:{'run_id':22})
    assert preflight.main(['--expected-sha',SHA,'--release-root',str(tmp_path),'--python','/existing/python'])==1
    result=capsys.readouterr()
    assert 'FAIL' in result.err and 'PASS' not in result.out


def test_api_errors_and_incomplete_pagination_fail_closed(monkeypatch):
    monkeypatch.setattr(preflight.subprocess,'run',lambda command,**kwargs: subprocess.CompletedProcess(command,1,b'',b'HTTP 403'))
    with pytest.raises(preflight.PreflightError,match='403'):
        preflight.GitHub().json(f'repos/{preflight.REPOSITORY}/actions/workflows/ci.yml')
    api=preflight.GitHub()
    monkeypatch.setattr(api,'json',lambda path:{'total_count':2,'jobs':[{'id':1}]})
    with pytest.raises(preflight.PreflightError,match='Incomplete'):
        api.items(f'repos/{preflight.REPOSITORY}/jobs','jobs')


def test_selection_import_does_not_write_into_release(tmp_path):
    scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'ci_evidence.py').write_text('def expected_selection(scope, root):\n    return {"scope":scope}\n')
    assert preflight.expected_selection('portable',tmp_path)=={'scope':'portable'}
    assert not (scripts/'__pycache__').exists()


@pytest.mark.parametrize('service', [
    'frontend-gateway', 'legacy-dashboard', 'account-api',
    'account-sync-controller', 'prediction-service',
])
def test_forward_launchd_imports_preserve_release_for_successive_services(tmp_path, service):
    """Use daemon template environments, without installers or live services."""
    import plistlib
    import sys

    release = tmp_path/'release'
    package = release/'src/open_trader'
    package.mkdir(parents=True)
    (package/'__init__.py').write_text('from .identity import VALUE\n')
    (package/'identity.py').write_text('VALUE = 42\n')
    (release/'.gitignore').write_text('__pycache__/\n')
    (release/'uv.lock').write_text('test source identity only\n')
    def git(*args):
        return subprocess.check_output(
            ['git', '-C', str(release), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('add', '.')
    git('commit', '-m', 'Immutable simulated release')
    git('checkout', '--detach')
    sha = git('rev-parse', 'HEAD')
    preflight.verify_checkout(release, sha)

    template = (Path(__file__).parents[1]/'ops/launchd'/f'com.open-trader.{service}.plist.template').read_text()
    rendered = plistlib.loads(template.replace('OPEN_TRADER_REPO', str(release)).encode())
    # launchd gets these variables from its plist, not the deploy wrapper process.
    environment = rendered['EnvironmentVariables']
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, '-c', 'import open_trader; print(open_trader.__file__); assert open_trader.VALUE == 42'],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(package/'__init__.py')
        assert not list(release.rglob('__pycache__'))
        preflight.verify_checkout(release, sha)
        assert git('status', '--porcelain', '--untracked-files=all') == ''
    assert environment['PYTHONDONTWRITEBYTECODE'] == '1'
