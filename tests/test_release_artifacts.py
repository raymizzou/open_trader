"""Offline release identity, evidence and clean-restoration regressions."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_artifacts', ROOT/'scripts/release_artifacts.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


SHA = 'a' * 40
REPOSITORY = 'raymizzou/open_trader'
SCOPES = ('gateway', 'legacy', 'account', 'prediction', 'portable')
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
BASE_IMAGES = ['python:3.12.14@sha256:' + 'c' * 64]


def evidence_context():
    return {'selections': {
        scope: {'roots': ['acceptance/test_prediction_arbitrage_scenarios.py'] if scope == 'portable'
                else ['tests/test_' + scope + '.py'],
                'marker': 'not pressure and not browser', 'keyword': 'not LIVE' if scope == 'portable' else ''}
        for scope in SCOPES}, 'base_images': list(BASE_IMAGES)}


def ci_fixture(sha=SHA):
    run = dict(id=100, run_number=4, run_attempt=2, workflow_id=11,
               path='.github/workflows/ci.yml', event='push', head_branch='main', head_sha=sha,
               status='completed', conclusion='success', check_suite_id=90,
               updated_at='2026-10-01T12:00:00Z',
               repository={'full_name': REPOSITORY}, head_repository={'full_name': REPOSITORY})
    jobs = [dict(id=1000 + i, name=name, run_id=100, head_sha=sha, status='completed',
                 conclusion='success', check_run_url=f'https://api.github.com/repos/{REPOSITORY}/check-runs/{1000+i}')
            for i, name in enumerate(('plan', *SCOPES, 'required'))]
    checks = [dict(id=job['id'], name=job['name'], head_sha=sha, status='completed',
                   conclusion='success', app={'id':15368}, check_suite={'id':90}) for job in jobs]
    return run, jobs, checks


def archive_files(sha=SHA, scope='gateway', lock='b' * 64, context=None, run=None):
    context = evidence_context() if context is None else context
    run = ci_fixture(sha)[0] if run is None else run
    manifest = dict(role='test-only; synthetic Git snapshot is not a release SHA', source_sha=sha,
                    source_state='clean', python='3.12.14', platform='Linux/x86_64', lock_sha256=lock,
                    base_images=context['base_images'], dependencies=[['pytest', '8.0']])
    image = [{'Id':'sha256:' + 'd' * 64, 'Config':{'Labels':{'org.open-trader.image-role':'test-only'},
              'Env':[f'OPEN_TRADER_TEST_SOURCE_SHA={sha}', 'OPEN_TRADER_TEST_SOURCE_STATE=clean']}}]
    files = {'identity.txt':f'source_sha={sha}\nscope={scope}\nTEST_N_LEG=1\n'.encode(),
             'result.txt':f'scope={scope} source_sha={sha} exit_status=0\n'.encode(),
             'lock-sha256.txt':f'{lock}  uv.lock\n'.encode(),
             'test.log':b'test results: passed\n',
             'dependency-manifest.json':json.dumps(manifest).encode(), 'image.json':json.dumps(image).encode()}
    env = dict(runner_os='Linux', runner_arch='X64',
               dependency_manifest_sha256=hashlib.sha256(files['dependency-manifest.json']).hexdigest(),
               image_inspect_sha256=hashlib.sha256(files['image.json']).hexdigest())
    if scope == 'portable':
        partition = dict(schema_version=1, marker='not pressure and not browser', complete_count=4, partition_count=4,
                         counts={name:1 for name in SCOPES if name != 'portable'},
                         complete_sha256='e' * 64, partition_sha256='e' * 64, status='success')
        files['partition.json'] = json.dumps(partition).encode()
        files['partition.log'] = b''
        env['partition_sha256'] = hashlib.sha256(files['partition.json']).hexdigest()
    files['evidence.json'] = json.dumps(dict(schema_version=1, source_sha=sha, repository=REPOSITORY,
        workflow_ref=f'{REPOSITORY}/.github/workflows/ci.yml@refs/heads/main', event_name='push',
        ref='refs/heads/main', run_id=str(run['id']), run_attempt=str(run['run_attempt']), scope=scope, test_n_leg='1',
        workers=2 if scope == 'prediction' else 1, lock_sha256=lock, status='success', exit_status=0,
        selection=context['selections'][scope], environment=env, evidence_role='test-only')).encode()
    return files


def pack(files):
    import io
    import zipfile
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return stream.getvalue()


def update_json(files, name, update, rehash=True):
    data = json.loads(files[name])
    update(data)
    files[name] = json.dumps(data).encode()
    fields = {'dependency-manifest.json':'dependency_manifest_sha256', 'image.json':'image_inspect_sha256',
              'partition.json':'partition_sha256'}
    if rehash and name in fields:
        update_json(files, 'evidence.json', lambda metadata: metadata['environment'].update(
            {fields[name]:hashlib.sha256(files[name]).hexdigest()}))


class EvidenceAPI:
    repository = REPOSITORY

    def __init__(self, sha=SHA, lock='b' * 64, context=None):
        self.context = context or evidence_context()
        self.run, self.jobs, self.checks = ci_fixture(sha)
        self.runs = [self.run]
        self.artifacts, self.downloads, self.calls = [], {}, []
        for i, scope in enumerate(SCOPES):
            data = pack(archive_files(sha, scope, lock, self.context, self.run))
            self.artifacts.append(dict(id=i + 1, name=f'ci-{scope}-{sha}-100-2', expired=False,
                created_at='2026-10-01T12:00:00Z', expires_at='2026-10-04T12:00:00Z',
                digest='sha256:' + hashlib.sha256(data).hexdigest(),
                workflow_run={'id':100, 'head_sha':sha, 'head_branch':'main'}))
            self.downloads[i + 1] = data

    def get(self, path):
        self.calls.append(path)
        if path == 'actions/runs/100': return copy.deepcopy(self.run)
        if path == 'actions/workflows/ci.yml':
            return {'id':11, 'path':'.github/workflows/ci.yml', 'state':'active'}
        raise AssertionError(path)

    def pages(self, path, key=None):
        self.calls.append(path)
        return copy.deepcopy({'workflow_runs':self.runs, 'jobs':self.jobs, 'check_runs':self.checks,
                              'artifacts':self.artifacts}[key])

    def raw(self, path, *args, **kwargs):
        self.calls.append(path)
        prefix, artifact_id, suffix = path.rsplit('/', 2)
        if prefix == 'actions/artifacts' and suffix == 'zip':
            return self.downloads[int(artifact_id)]
        raise AssertionError('Unexpected write or API call: ' + path)


class CurrentCIContracts(unittest.TestCase):
    def test_all_five_scopes_succeed_for_current_attempt(self):
        run, jobs, checks = ci_fixture()
        self.assertEqual(release.validate_ci([run], checks, jobs, SHA, NOW)['id'], run['id'])
        for scope in SCOPES:
            with self.subTest(scope=scope):
                release.validate_evidence_zip(pack(archive_files(scope=scope)), SHA, scope, 'b' * 64,
                                              evidence_context(), run)

    def test_skipped_backend_or_portable_never_establishes_release_evidence(self):
        for scope in SCOPES:
            run, jobs, checks = ci_fixture()
            next(job for job in jobs if job['name'] == scope)['conclusion'] = 'skipped'
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                release.validate_ci([run], checks, jobs, SHA, NOW)

    def test_run_identity_repository_age_and_latest_attempt_fail_closed(self):
        mutations = [('head_sha', 'b' * 40), ('event', 'pull_request'), ('head_branch', 'topic'),
                     ('path', '.github/workflows/other.yml'), ('status', 'in_progress'),
                     ('conclusion', 'failure'), ('updated_at', '2026-09-29T12:00:00Z'),
                     ('updated_at', '2026-10-03T12:00:00Z'), ('updated_at', '2026-10-01T12:00:00'),
                     ('repository', {'full_name':'attacker/fork'}),
                     ('head_repository', {'full_name':'attacker/fork'}), ('run_attempt', 0), ('run_attempt', True)]
        for field, value in mutations:
            run, jobs, checks = ci_fixture()
            run[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                release.validate_ci([run], checks, jobs, SHA, NOW)
        run, jobs, checks = ci_fixture()
        for changed in (dict(run, id=101, status='queued', conclusion=None),
                        dict(run, run_attempt=3, status='in_progress', conclusion=None)):
            with self.subTest(latest=changed), self.assertRaises(ValueError):
                release.validate_ci([run, changed], checks, jobs, SHA, NOW)
        with self.assertRaises(ValueError):
            release.validate_ci([run], checks, jobs, SHA, NOW, repository='attacker/fork')

    def test_required_check_is_unique_successful_actions_and_linked_to_job(self):
        for mutation in ('app', 'suite', 'sha', 'check_id', 'status', 'conclusion', 'missing', 'duplicate'):
            run, jobs, checks = ci_fixture()
            required = checks[-1]
            if mutation == 'app': required['app']['id'] = 1
            elif mutation == 'suite': required['check_suite']['id'] = 91
            elif mutation == 'sha': required['head_sha'] = 'b' * 40
            elif mutation == 'check_id': required['id'] = 999
            elif mutation == 'status': required['status'] = 'in_progress'
            elif mutation == 'conclusion': required['conclusion'] = 'failure'
            elif mutation == 'missing': checks.pop()
            else: checks.append(copy.deepcopy(required))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                release.validate_ci([run], checks, jobs, SHA, NOW)

    def test_all_required_jobs_are_unique_successful_and_exact_run_sha(self):
        for name in ('plan', *SCOPES, 'required'):
            for mutation in ('run_id', 'head_sha', 'status', 'conclusion', 'missing', 'duplicate'):
                run, jobs, checks = ci_fixture()
                job = next(j for j in jobs if j['name'] == name)
                if mutation == 'run_id': job['run_id'] = 99
                elif mutation == 'head_sha': job['head_sha'] = 'b' * 40
                elif mutation == 'status': job['status'] = 'in_progress'
                elif mutation == 'conclusion': job['conclusion'] = 'failure'
                elif mutation == 'missing': jobs.remove(job)
                else: jobs.append(copy.deepcopy(job))
                with self.subTest(name=name, mutation=mutation), self.assertRaises(ValueError):
                    release.validate_ci([run], checks, jobs, SHA, NOW)
        run, jobs, checks = ci_fixture()
        jobs.append(dict(name='trend_curve', conclusion='skipped'))
        self.assertEqual(release.validate_ci([run], checks, jobs, SHA, NOW)['id'], 100)


class EvidenceFixtures(unittest.TestCase):
    def validate(self, files, scope='gateway', context=None, run=None):
        release.validate_evidence_zip(pack(files), SHA, scope, 'b' * 64,
                                      context or evidence_context(), run or ci_fixture()[0])

    def test_archive_identity_logs_lock_and_nleg_fail_closed(self):
        for name, content in [('identity.txt', f'source_sha={SHA}\nscope=gateway\nTEST_N_LEG=0\n'.encode()),
                              ('identity.txt', f'source_sha={"c"*40}\nscope=gateway\nTEST_N_LEG=1\n'.encode()),
                              ('result.txt', f'scope=gateway source_sha={SHA} exit_status=1\n'.encode()),
                              ('lock-sha256.txt', ('c' * 64 + '  uv.lock\n').encode()), ('test.log', b'')]:
            files = archive_files(); files[name] = content
            with self.subTest(name=name), self.assertRaises(ValueError): self.validate(files)
        for missing in archive_files():
            files = archive_files(); files.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError): self.validate(files)

    def test_evidence_metadata_requires_exact_current_contract(self):
        changes = [('schema_version', True), ('source_sha', 'c' * 40), ('repository', 'attacker/fork'),
                   ('workflow_ref', f'{REPOSITORY}/.github/workflows/ci.yml@refs/pull/1/merge'),
                   ('event_name', 'pull_request'), ('ref', 'refs/heads/topic'), ('run_id', '99'),
                   ('run_attempt', '1'), ('scope', 'legacy'), ('test_n_leg', '0'), ('workers', 2),
                   ('lock_sha256', 'c' * 64), ('status', 'failure'), ('exit_status', 1),
                   ('exit_status', False), ('evidence_role', 'deployment'), ('selection', {'roots':[]}),
                   ('environment', {})]
        for field, value in changes:
            files = archive_files()
            update_json(files, 'evidence.json', lambda metadata: metadata.update({field:value}))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.validate(files)
        files = archive_files(scope='prediction')
        update_json(files, 'evidence.json', lambda metadata: metadata.update(workers=1))
        with self.assertRaises(ValueError): self.validate(files, 'prediction')

    def test_environment_hashes_and_test_image_identity_fail_closed(self):
        for field, value in [('runner_os', 'macOS'), ('runner_arch', 'ARM64'),
                             ('dependency_manifest_sha256', '0' * 64), ('image_inspect_sha256', '0' * 64)]:
            files = archive_files()
            update_json(files, 'evidence.json', lambda metadata: metadata['environment'].update({field:value}))
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(files)
        for field, value in [('role', 'release'), ('source_sha', 'c' * 40), ('source_state', 'dirty'),
                             ('lock_sha256', 'c' * 64), ('python', '3.12.3'), ('platform', 'Darwin/arm64'),
                             ('base_images', ['python:3.12']), ('dependencies', [])]:
            files = archive_files()
            update_json(files, 'dependency-manifest.json', lambda manifest: manifest.update({field:value}))
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(files)
        for mutation in ('id', 'role', 'source', 'dirty', 'duplicate'):
            files = archive_files()
            def corrupt(images):
                if mutation == 'id': images[0]['Id'] = 'sha256:bad'
                elif mutation == 'role': images[0]['Config']['Labels']['org.open-trader.image-role'] = 'release'
                elif mutation == 'source': images[0]['Config']['Env'][0] = 'OPEN_TRADER_TEST_SOURCE_SHA=' + 'c' * 40
                elif mutation == 'dirty': images[0]['Config']['Env'][1] = 'OPEN_TRADER_TEST_SOURCE_STATE=dirty'
                else: images.append(copy.deepcopy(images[0]))
            update_json(files, 'image.json', corrupt)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(files)

    def test_portable_requires_exact_complete_backend_partition(self):
        files = archive_files(scope='portable'); files['partition.log'] = b''
        self.validate(files, 'portable')
        for field, value in [('schema_version', 2), ('marker', 'not pressure'), ('status', 'failure'),
                             ('complete_count', 3), ('partition_count', 5), ('complete_sha256', 'bad'),
                             ('partition_sha256', 'f' * 64), ('counts', {'gateway':4}),
                             ('counts', dict(gateway=True, legacy=1, account=1, prediction=1)),
                             ('counts', dict(gateway=0, legacy=2, account=1, prediction=1))]:
            files = archive_files(scope='portable')
            update_json(files, 'partition.json', lambda partition: partition.update({field:value}))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.validate(files, 'portable')
        files = archive_files(scope='portable')
        update_json(files, 'evidence.json', lambda metadata: metadata['environment'].update(partition_sha256='0' * 64))
        with self.assertRaises(ValueError): self.validate(files, 'portable')
        for missing in ('partition.json', 'partition.log'):
            files = archive_files(scope='portable'); files.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError): self.validate(files, 'portable')

    def test_archive_requires_safe_unique_regular_members_and_full_context(self):
        import io
        import warnings
        import zipfile
        for extra in ('../escape', '/escape', 'extra.txt', 'nested/test.log', 'nested\\test.log'):
            files = archive_files(); files[extra] = b'bad'
            with self.subTest(extra=extra), self.assertRaises(ValueError): self.validate(files)
        stream = io.BytesIO(pack(archive_files()))
        with zipfile.ZipFile(stream, 'a') as archive, warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            archive.writestr('test.log', b'duplicate')
        with self.assertRaises(ValueError):
            release.validate_evidence_zip(stream.getvalue(), SHA, 'gateway', 'b' * 64, evidence_context(), ci_fixture()[0])
        stream = io.BytesIO(pack(archive_files()))
        with zipfile.ZipFile(stream, 'a') as archive:
            member = zipfile.ZipInfo('link'); member.create_system = 3; member.external_attr = 0o120777 << 16
            archive.writestr(member, 'test.log')
        with self.assertRaises(ValueError):
            release.validate_evidence_zip(stream.getvalue(), SHA, 'gateway', 'b' * 64, evidence_context(), ci_fixture()[0])
        context = evidence_context(); context['selections'].pop('portable')
        with self.assertRaises(ValueError): self.validate(archive_files(), context=context)
        run = ci_fixture()[0]; run['head_sha'] = 'c' * 40
        with self.assertRaises(ValueError): self.validate(archive_files(), run=run)


class TrustedCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'uv.lock').write_text('locked bytes')
        self.output = self.root/'output'; self.output.mkdir()
        self.api = EvidenceAPI(lock=release.sha256(self.root/'uv.lock'))

    def collect(self, api=None):
        return release.trusted_evidence(api or self.api, SHA, self.root, self.output, NOW,
                                        context=evidence_context())

    def test_collects_all_five_digest_bound_current_attempt_archives_without_source_execution(self):
        from unittest.mock import patch
        with patch.object(release, 'validation_context', side_effect=AssertionError('Must use supplied data')):
            report = self.collect()
        self.assertEqual(report['tested_scopes'], sorted(SCOPES))
        self.assertNotIn('documentation_only', report)
        self.assertEqual(report['validation_context'], evidence_context())
        self.assertEqual((report['run_id'], report['run_attempt']), (100, 2))
        self.assertEqual(report['required_app_id'], 15368)
        self.assertIn('actions/runs/100/attempts/2/jobs', self.api.calls)
        self.assertEqual(len(report['artifacts']), 5)
        for artifact in report['artifacts']:
            self.assertEqual(artifact['name'], f'ci-{artifact["scope"]}-{SHA}-100-2.zip')
            self.assertEqual(artifact['digest'], 'sha256:' + release.sha256(self.output/artifact['name']))

    def test_metadata_failures_prevent_artifact_download(self):
        for mutation in ('expired', 'expiry', 'future', 'retention', 'missing', 'duplicate',
                         'legacy_name', 'old_attempt', 'wrong_run', 'wrong_sha', 'wrong_branch'):
            api = EvidenceAPI(lock=release.sha256(self.root/'uv.lock'))
            # account is first alphabetically, so rejection must precede any download.
            artifact = next(a for a in api.artifacts if a['name'].startswith('ci-account-'))
            if mutation == 'expired': artifact['expired'] = True
            elif mutation == 'expiry': artifact['expires_at'] = NOW.isoformat()
            elif mutation == 'future': artifact['created_at'] = '2026-10-03T12:00:00Z'
            elif mutation == 'retention': artifact['expires_at'] = '2026-10-05T12:00:00Z'
            elif mutation == 'missing': api.artifacts.remove(artifact)
            elif mutation == 'duplicate': api.artifacts.append(copy.deepcopy(artifact))
            elif mutation == 'legacy_name': artifact['name'] = f'ci-account-{SHA}'
            elif mutation == 'old_attempt': artifact['name'] = f'ci-account-{SHA}-100-1'
            elif mutation == 'wrong_run': artifact['workflow_run']['id'] = 99
            elif mutation == 'wrong_sha': artifact['workflow_run']['head_sha'] = 'c' * 40
            else: artifact['workflow_run']['head_branch'] = 'topic'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.collect(api)
            self.assertFalse(any('/zip' in path for path in api.calls))

    def test_artifact_api_digest_is_mandatory_and_authenticates_downloaded_bytes(self):
        for digest in (None, '', 'sha256:' + '0' * 64):
            api = EvidenceAPI(lock=release.sha256(self.root/'uv.lock'))
            next(a for a in api.artifacts if a['name'].startswith('ci-account-'))['digest'] = digest
            with self.subTest(digest=digest), self.assertRaises(ValueError): self.collect(api)

    def test_workflow_repository_and_races_never_establish_evidence(self):
        for mutation in ('repository', 'workflow', 'attempt', 'newrun'):
            api = EvidenceAPI(lock=release.sha256(self.root/'uv.lock'))
            original_get, original_pages = api.get, api.pages
            if mutation == 'repository': api.repository = 'attacker/fork'
            elif mutation == 'workflow': api.run['workflow_id'] = 12
            counts = {'get':0, 'list':0}
            def get(path):
                value = original_get(path)
                if path == 'actions/runs/100':
                    counts['get'] += 1
                    if mutation == 'attempt' and counts['get'] > 1:
                        value.update(run_attempt=3, status='in_progress', conclusion=None)
                return value
            def pages(path, key=None):
                value = original_pages(path, key)
                if key == 'workflow_runs':
                    counts['list'] += 1
                    if mutation == 'newrun' and counts['list'] > 1:
                        value.append(dict(api.run, id=101, status='queued', conclusion=None))
                return value
            api.get, api.pages = get, pages
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.collect(api)


class ReleaseContracts(unittest.TestCase):
    def test_version_is_strict_and_shell_safe(self):
        for tag in ('v1.2.3', 'v0.0.0', 'v12.2.3-rc.1'):
            self.assertEqual(release.validate_tag(tag), tag)
        for tag in ('v01.2.3', 'v1.2.3-rc.0', 'v1.2.3-rc.01', 'v1.2.3\n',
                    'v1.2.3;id', '1.2.3', '--all', 'v1.2.3+build', 'v1.2.3-rc.1/x'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate_tag(tag)

    def test_asset_retry_never_overwrites_or_publishes(self):
        expected = {'a.txt':hashlib.sha256(b'abc').hexdigest()}
        self.assertEqual(release.asset_plan([], expected, lambda a:b''), ['a.txt'])
        existing = [dict(name='a.txt', id=1)]
        self.assertEqual(release.asset_plan(existing, expected, lambda a:b'abc'), [])
        for assets in [existing, [dict(name='unexpected',id=2)], existing*2]:
            with self.assertRaises(ValueError):
                release.asset_plan(assets, expected, lambda a:b'wrong')

    def test_installed_runtime_cannot_import_from_host_or_wrong_python(self):
        root=Path('/restored');sha='a'*40
        valid=dict(git_sha=sha,source_state='clean',checkout=str(root),package_code_root=str(root/'src'),prediction_code_root=str(root/'src'),python='3.12.14')
        release.validate_installed_identity(valid,root,sha)
        for key,value in [('package_code_root','/host/src'),('prediction_code_root','/host/src'),('python','3.12.3'),('git_sha','b'*40)]:
            with self.subTest(key=key),self.assertRaises(ValueError):release.validate_installed_identity(dict(valid,**{key:value}),root,sha)

    def test_only_declared_distribution_is_baseline(self):
        from unittest.mock import patch
        with patch.object(release.platform,'freedesktop_os_release',return_value={'ID':'debian','VERSION_ID':'13'}):
            with self.assertRaises(ValueError):release.validate_platform()
        with patch.object(release.platform,'freedesktop_os_release',return_value={'ID':'ubuntu','VERSION_ID':'24.04'}):
            release.validate_platform()

    def test_remote_tag_retarget_or_main_rewrite_rejected(self):
        identity={'tag':'v1.2.3','tag_object_sha':'a'*40,'source_sha':'b'*40}
        class API:
            commit='b'*40
            status='ahead'
            def get(self,path):
                if path.startswith('git/ref/'):return {'object':{'type':'tag','sha':'a'*40}}
                if path.startswith('git/tags/'):return {'object':{'type':'commit','sha':self.commit}}
                return {'status':self.status}
        api=API();release.remote_tag_matches(api,identity)
        api.commit='c'*40
        with self.assertRaises(ValueError):release.remote_tag_matches(api,identity)
        api.commit='b'*40;api.status='diverged'
        with self.assertRaises(ValueError):release.remote_tag_matches(api,identity)

    def test_compatibility_manifest_strict(self):
        base = dict(schema_version='open_trader.prediction_service.release.v1',reader_generation=2,contract_generation=2)
        self.assertEqual(release.validate_compatibility(base), base)
        for bad in [dict(base,source_sha='a'*40),dict(base,reader_generation=True),dict(base,contract_generation=0)]:
            with self.assertRaises(ValueError):
                release.validate_compatibility(bad)


class GitRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.git('init','-b','main')
        self.git('config','user.name','Test'); self.git('config','user.email','test@example.invalid')
        (self.repo/'uv.lock').write_text('version = 1\n')
        (self.repo/'src/open_trader').mkdir(parents=True)
        (self.repo/'src/open_trader/__init__.py').write_text('')
        (self.repo/'ops').mkdir()
        (self.repo/'ops/prediction-service-release.json').write_text(json.dumps(dict(schema_version='open_trader.prediction_service.release.v1',reader_generation=2,contract_generation=2)))
        self.git('add','.'); self.git('commit','-m','fixture')
        self.sha=self.git('rev-parse','HEAD')
        self.git('tag','-a','v1.2.3','-m','annotated')
        self.git('update-ref','refs/remotes/origin/main',self.sha)

    def git(self,*args):
        return subprocess.check_output(['git',*args],cwd=self.repo,text=True,stderr=subprocess.DEVNULL).strip()

    def test_annotated_tag_resolves_commit_and_unrelated_branch_excluded(self):
        identity=release.source_identity(self.repo,'v1.2.3')
        self.assertEqual(identity['source_sha'],self.sha)
        self.assertNotEqual(identity['tag_object_sha'],self.sha)
        self.git('checkout','--orphan','secret-branch'); self.git('rm','-rf','.')
        (self.repo/'private.txt').write_text('must not be bundled')
        self.git('add','.');self.git('commit','-m','unrelated');self.git('checkout','main')
        bundle=self.root/'source.bundle'
        release.create_bundle(self.repo,self.sha,bundle)
        restored=self.root/'restored'
        release.restore_bundle(bundle,restored,self.sha)
        self.assertEqual(release.verify_code_root(restored),str(restored/'src'))
        self.assertEqual(subprocess.check_output(['git','status','--porcelain','--untracked-files=all'],cwd=restored),b'')
        self.assertFalse((restored/'private.txt').exists())
        (self.repo/'next.txt').write_text('advance');self.git('add','.');self.git('commit','-m','next')
        self.git('update-ref','refs/remotes/origin/main','HEAD')
        self.assertEqual(release.source_identity(self.repo,'v1.2.3')['source_sha'],self.sha)

    def test_non_main_tag_and_dirty_checkout_rejected(self):
        self.git('checkout','-b','side')
        (self.repo/'side').write_text('x');self.git('add','.');self.git('commit','-m','side');self.git('tag','v2.0.0')
        with self.assertRaises(ValueError):release.source_identity(self.repo,'v2.0.0')
        self.git('checkout','main');(self.repo/'dirty').write_text('x')
        with self.assertRaises(ValueError):release.source_identity(self.repo,'v1.2.3')

    def test_checksum_and_lock_tampering_rejected(self):
        out=self.root/'out'; out.mkdir()
        (out/'uv.lock').write_text('original')
        sums={'uv.lock':release.sha256(out/'uv.lock')}
        release.verify_checksums(out,sums)
        (out/'uv.lock').write_text('modified')
        with self.assertRaises(ValueError):release.verify_checksums(out,sums)
        with self.assertRaises(ValueError):release.verify_checksums(out,{'../escape':'a'*64})


class FullArtifactRoundTrip(GitRoundTrip):
    def setUp(self):
        super().setUp()
        import sys
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT/'scripts'))
        self.addCleanup(lambda:sys.path.remove(str(ROOT/'scripts')))
        import verify_release_artifacts
        self.verifier = verify_release_artifacts
        self.out = self.root/'assets'
        self.api = EvidenceAPI(self.sha, release.sha256(self.repo/'uv.lock'))
        self.context = evidence_context()
        validation = {'source_sha':self.sha, **{key:True for key in
            ('clean', 'git_identity_verified', 'prediction_identity_verified', 'code_root_verified')}}
        collect = release.trusted_evidence
        def runtime(bundle, sha, output):
            (output/'installation.log').write_text('fixture successful locked installation')
            return validation
        def trusted(api, sha, repo, output):
            return collect(api, sha, repo, output, NOW, context=self.context)
        with patch.object(release, 'trusted_evidence', side_effect=trusted), \
                patch.object(release, 'validate_runtime', side_effect=runtime), \
                patch.object(release, 'remote_tag_matches'):
            self.manifest = release.build(self.repo, 'v1.2.3', self.out, self.api)

    def reseal(self, directory, manifest):
        """Recompute local hashes so tests exercise identity checks beyond checksums."""
        release.write_json(directory/'ci-evidence.json', manifest['ci'])
        manifest['assets'] = {p.name:release.sha256(p) for p in directory.iterdir()
                              if p.name not in ('release-manifest.json', 'SHA256SUMS')}
        release.write_json(directory/'release-manifest.json', manifest)
        sums = {p.name:release.sha256(p) for p in directory.iterdir() if p.name != 'SHA256SUMS'}
        (directory/'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sums.items()))

    def test_full_build_restore_and_writer_never_executes_bundled_source(self):
        from unittest.mock import patch
        self.verifier.verify(self.out, self.root/'verified', execute_code=True, expected_sha=self.sha)
        for expected in (None, 'b' * 40):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, 'independently trusted'):
                self.verifier.verify(self.out, self.root/'untrusted', execute_code=True, expected_sha=expected)
        with patch.object(self.verifier, 'verify_code_root', side_effect=AssertionError('Do not execute source')), \
                patch.object(release, 'validation_context', side_effect=AssertionError('Do not execute selection code')):
            self.verifier.verify(self.out, self.root/'writer-verified', execute_code=False)
        digest = release.sha256(self.out/'release-manifest.json')
        with patch.object(self.verifier, 'verify', side_effect=ValueError('untrusted bundle')) as check:
            with self.assertRaisesRegex(ValueError, 'untrusted bundle'):
                release.upload_draft(self.out, self.api, expected_manifest_sha256=digest,
                                     expected_tag='v1.2.3', expected_sha=self.sha)
            self.assertIs(check.call_args.kwargs['execute_code'], False)
        self.assertEqual(self.manifest['tree_sha'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertEqual(self.manifest['ci']['tested_scopes'], sorted(SCOPES))

    def test_complete_core_inventory_and_all_five_evidence_scopes_required(self):
        import shutil
        for defect in ('INSTALL.txt', 'installation.log', 'extra', 'duplicate', 'missing_portable', 'legacy_empty'):
            altered = self.root/('assets-' + defect); shutil.copytree(self.out, altered)
            manifest = copy.deepcopy(self.manifest)
            if defect in ('INSTALL.txt', 'installation.log'): (altered/defect).unlink()
            elif defect == 'extra': (altered/'extra.txt').write_text('unexpected')
            elif defect == 'duplicate': manifest['ci']['artifacts'].append(copy.deepcopy(manifest['ci']['artifacts'][0]))
            elif defect == 'missing_portable':
                portable = next(a for a in manifest['ci']['artifacts'] if a['scope'] == 'portable')
                (altered/portable['name']).unlink(); manifest['ci']['artifacts'].remove(portable)
                manifest['ci']['tested_scopes'].remove('portable')
            else:
                for artifact in manifest['ci']['artifacts']: (altered/artifact['name']).unlink()
                manifest['ci'].update(artifacts=[], tested_scopes=[], documentation_only=True)
            self.reseal(altered, manifest)
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.verifier.verify(altered, self.root/('rejected-inventory-' + defect))

    def test_resealed_manifest_and_evidence_identity_tampering_rejected(self):
        import shutil
        cases = [('source_sha', 'c' * 40), ('tree_sha', 'c' * 40), ('lock_sha256', 'c' * 64),
                 ('repository', 'attacker/fork'), ('prediction_compatibility',
                  dict(self.manifest['prediction_compatibility'], reader_generation=3))]
        for field, value in cases:
            altered = self.root/('assets-' + field); shutil.copytree(self.out, altered)
            manifest = copy.deepcopy(self.manifest); manifest[field] = value
            self.reseal(altered, manifest)
            with self.subTest(field=field), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                self.verifier.verify(altered, self.root/('rejected-' + field))
        for field, value in [('event', 'pull_request'), ('branch', 'feature'), ('required_app_id', 1),
                             ('workflow_path', '.github/workflows/other.yml'), ('run_id', 0), ('run_attempt', True),
                             ('workflow_id', 0), ('check_suite_id', -1), ('required_check_id', 0), ('required_job_id', 0)]:
            altered = self.root/('assets-ci-' + field); shutil.copytree(self.out, altered)
            manifest = copy.deepcopy(self.manifest); manifest['ci'][field] = value
            self.reseal(altered, manifest)
            with self.subTest(ci_field=field), self.assertRaises(ValueError):
                self.verifier.verify(altered, self.root/('rejected-ci-' + field))

    def test_resealed_artifact_name_digest_or_metadata_tampering_rejected(self):
        import io
        import shutil
        import zipfile
        for defect in ('old_attempt', 'digest', 'metadata'):
            altered = self.root/('assets-artifact-' + defect); shutil.copytree(self.out, altered)
            manifest = copy.deepcopy(self.manifest); artifact = manifest['ci']['artifacts'][0]
            if defect == 'old_attempt':
                old = artifact['name']; artifact['name'] = old.replace('-100-2.zip', '-100-1.zip')
                (altered/old).rename(altered/artifact['name'])
            elif defect == 'digest': artifact['digest'] = 'sha256:' + '0' * 64
            else:
                with zipfile.ZipFile(io.BytesIO((altered/artifact['name']).read_bytes())) as archive:
                    files = {name:archive.read(name) for name in archive.namelist()}
                update_json(files, 'evidence.json', lambda evidence:evidence.update(test_n_leg='0'))
                (altered/artifact['name']).write_bytes(pack(files))
                artifact['sha256'] = release.sha256(altered/artifact['name'])
                artifact['digest'] = 'sha256:' + artifact['sha256']
            self.reseal(altered, manifest)
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.verifier.verify(altered, self.root/('rejected-artifact-' + defect))


class DraftWriteBoundaries(unittest.TestCase):
    def setUp(self):
        import sys
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sys.path.insert(0, str(ROOT/'scripts')); self.addCleanup(lambda:sys.path.remove(str(ROOT/'scripts')))
        import verify_release_artifacts
        self.verifier = verify_release_artifacts
        self.manifest = {'repository':REPOSITORY, 'tag':'v1.2.3', 'source_sha':SHA,
                         'ci':{'validation_context':evidence_context()}}
        release.write_json(self.root/'release-manifest.json', self.manifest)
        self.digest = release.sha256(self.root/'release-manifest.json')

    def test_untrusted_manifest_digest_rejected_before_parsing_restoration_or_api(self):
        from unittest.mock import patch
        for digest in (None, '', 'bad', '0' * 64):
            with patch.object(self.verifier, 'verify', side_effect=AssertionError('No parsing before authentication')), \
                    patch.object(release, 'trusted_evidence', side_effect=AssertionError('No API before authentication')):
                with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'manifest digest'):
                    release.upload_draft(self.root, object(), expected_manifest_sha256=digest,
                                         expected_tag='v1.2.3', expected_sha=SHA)

    def test_digest_valid_manifest_must_match_requested_tag_and_source_before_any_execution(self):
        from unittest.mock import patch
        for tag, sha in ((None, SHA), ('v1.2.3', None), ('v9.9.9', SHA), ('v1.2.3', 'b' * 40)):
            with patch.object(self.verifier, 'verify', side_effect=AssertionError('No restoration before identity')), \
                    patch.object(release, 'remote_tag_matches', side_effect=AssertionError('No API before identity')), \
                    patch.object(release, 'trusted_evidence', side_effect=AssertionError('No API before identity')):
                with self.subTest(tag=tag, sha=sha), self.assertRaises(ValueError):
                    release.upload_draft(self.root, object(), expected_manifest_sha256=self.digest,
                                         expected_tag=tag, expected_sha=sha)

    def test_writer_never_falls_back_to_executing_source_when_context_is_missing(self):
        from unittest.mock import patch
        class API:
            repository = REPOSITORY
        for context in (None, {}, {'selections': {'gateway': {}}}):
            manifest = copy.deepcopy(self.manifest); manifest['ci']['validation_context'] = context
            release.write_json(self.root/'release-manifest.json', manifest)
            digest = release.sha256(self.root/'release-manifest.json')
            with patch.object(self.verifier, 'verify', return_value=manifest), \
                    patch.object(release, 'remote_tag_matches'), \
                    patch.object(release, 'validation_context', side_effect=AssertionError('Never execute source')), \
                    patch.object(release, 'trusted_evidence', side_effect=AssertionError('No collection without context')):
                with self.subTest(context=context), self.assertRaises(ValueError):
                    release.upload_draft(self.root, API(), expected_manifest_sha256=digest,
                                         expected_tag='v1.2.3', expected_sha=SHA)

    def test_existing_published_immutable_or_wrong_target_never_writes(self):
        from unittest.mock import patch
        for changes in ({'draft':False}, {'immutable':True}, {'target_commitish':'b' * 40}):
            existing = dict(id=1, tag_name='v1.2.3', target_commitish=SHA, draft=True, immutable=False)
            existing.update(changes)
            class API:
                repository = REPOSITORY
                def pages(self, path): return [existing]
                def raw(self, *args, **kwargs): raise AssertionError('Rejected release must not write')
            with patch.object(self.verifier, 'verify', return_value=self.manifest), \
                    patch.object(release, 'remote_tag_matches'), \
                    patch.object(release, 'trusted_evidence', return_value=self.manifest['ci']) as evidence:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    release.upload_draft(self.root, API(), expected_manifest_sha256=self.digest,
                                     expected_tag='v1.2.3', expected_sha=SHA)
                self.assertEqual(evidence.call_args.kwargs['context'], self.manifest['ci']['validation_context'])

    def test_remote_tag_change_or_new_evidence_prevents_creation(self):
        from unittest.mock import patch
        class API:
            repository = REPOSITORY
            def pages(self, path): return []
            def raw(self, *args, **kwargs): raise AssertionError('Changed identity must not write')
        with patch.object(self.verifier, 'verify', return_value=self.manifest), \
                patch.object(release, 'trusted_evidence', return_value=self.manifest['ci']), \
                patch.object(release, 'remote_tag_matches', side_effect=[None, ValueError('tag moved')]):
            with self.assertRaisesRegex(ValueError, 'tag moved'):
                release.upload_draft(self.root, API(), expected_manifest_sha256=self.digest,
                                     expected_tag='v1.2.3', expected_sha=SHA)
        with patch.object(self.verifier, 'verify', return_value=self.manifest), \
                patch.object(release, 'trusted_evidence', return_value={}), patch.object(release, 'remote_tag_matches'):
            with self.assertRaisesRegex(ValueError, 'CI evidence changed'):
                release.upload_draft(self.root, API(), expected_manifest_sha256=self.digest,
                                     expected_tag='v1.2.3', expected_sha=SHA)


if __name__ == '__main__': unittest.main()
