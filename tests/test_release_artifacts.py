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


class ReleaseContracts(unittest.TestCase):
    def test_version_is_strict_and_shell_safe(self):
        for tag in ('v1.2.3', 'v0.0.0', 'v12.2.3-rc.1'):
            self.assertEqual(release.validate_tag(tag), tag)
        for tag in ('v01.2.3', 'v1.2.3-rc.0', 'v1.2.3-rc.01', 'v1.2.3\n',
                    'v1.2.3;id', '1.2.3', '--all', 'v1.2.3+build', 'v1.2.3-rc.1/x'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate_tag(tag)

    def test_only_latest_exact_main_ci_and_trusted_required(self):
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        sha = 'a'*40
        run = dict(id=100, run_attempt=2, head_sha=sha, head_branch='main', event='push',
                   path='.github/workflows/ci.yml', status='completed', conclusion='success',
                   updated_at='2026-09-29T00:00:00Z', check_suite_id=90)
        check = dict(name='required', head_sha=sha, status='completed', conclusion='success',
                     app={'id':15368}, check_suite={'id':90}, details_url='https://github.com/o/r/actions/runs/100/job/123')
        jobs = [dict(name='plan', conclusion='success'), dict(name='required', conclusion='success')]
        jobs += [dict(name=x, conclusion='skipped') for x in release.JOBS]
        self.assertEqual(release.validate_ci([run], [check], jobs, sha, now)['id'], 100)
        for key, value in [('head_sha','b'*40),('event','pull_request'),('head_branch','other'),
                           ('path','.github/workflows/other.yml'),('status','in_progress'),
                           ('conclusion','failure'),('updated_at','2026-09-20T00:00:00Z')]:
            bad = dict(run, **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_ci([bad], [check], jobs, sha, now)
        for bad in [dict(check, app={'id':1}), dict(check, check_suite={'id':89}),
                    dict(check, conclusion='failure'), dict(check, head_sha='b'*40)]:
            with self.assertRaises(ValueError):
                release.validate_ci([run], [bad], jobs, sha, now)
        newer = dict(run, id=101, status='in_progress', conclusion=None)
        with self.assertRaises(ValueError):
            release.validate_ci([run,newer], [check], jobs, sha, now)
        with self.assertRaises(ValueError):
            release.validate_ci([run], [check], jobs[:-1], sha, now)

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


class EvidenceFixtures(unittest.TestCase):
    def archive(self, sha='a'*40, scope='gateway', lock='b'*64):
        import io
        import zipfile
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('identity.txt',f'source_sha={sha}\nscope={scope}\nTEST_N_LEG=1\n')
            z.writestr('result.txt',f'scope={scope} source_sha={sha} exit_status=0\n')
            z.writestr('lock-sha256.txt',f'{lock}  uv.lock\n')
            z.writestr('dependency-manifest.json',json.dumps(dict(source_sha=sha,source_state='clean',lock_sha256=lock)))
            z.writestr('image.json','[{}]');z.writestr('test.log','test results')
        return stream.getvalue()

    def test_archive_identity_and_lock_fail_closed(self):
        data=self.archive()
        release.validate_evidence_zip(data,'a'*40,'gateway','b'*64)
        for sha,scope,lock in [('c'*40,'gateway','b'*64),('a'*40,'prediction','b'*64),('a'*40,'gateway','c'*64)]:
            with self.assertRaises(ValueError):release.validate_evidence_zip(data,sha,scope,lock)

    def test_expired_artifact_blocks_download(self):
        now=datetime(2026,9,30,tzinfo=timezone.utc)
        sha='a'*40
        run=dict(id=100,run_attempt=1,head_sha=sha,head_branch='main',event='push',path='.github/workflows/ci.yml',
                 status='completed',conclusion='success',updated_at='2026-09-29T00:00:00Z',check_suite_id=90)
        jobs=[dict(name=n,conclusion='success' if n in ('plan','required','gateway') else 'skipped') for n in ('plan','required',*release.JOBS)]
        checks=[dict(name='required',head_sha=sha,status='completed',conclusion='success',app={'id':15368},check_suite={'id':90})]
        class API:
            repository='o/r'
            def get(self,path):return run
            def pages(self,path,key):
                if key=='workflow_runs':return [run]
                if key=='jobs':return jobs
                if key=='check_runs':return checks
                return [dict(name=f'ci-gateway-{sha}',id=1,expired=True,expires_at='2026-09-29T00:00:00Z')]
            def raw(self,path):raise AssertionError('Expired archive must never be downloaded')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'uv.lock').write_text('lock')
            with self.assertRaisesRegex(ValueError,'Expired'):
                release.trusted_evidence(API(),sha,root,root,now)


class FullArtifactRoundTrip(GitRoundTrip):
    def test_full_build_restore_and_manifest_tampering(self):
        from unittest.mock import patch
        import sys
        sys.path.insert(0,str(ROOT/'scripts'))
        self.addCleanup(lambda:sys.path.remove(str(ROOT/'scripts')))
        import verify_release_artifacts as verifier
        identity=release.source_identity(self.repo,'v1.2.3')
        class API:
            repository='o/r'
            def get(self,path):
                if path.startswith('git/ref/tags/'):return {'object':{'sha':identity['tag_object_sha'],'type':'tag'}}
                if path.startswith('git/tags/'):return {'object':{'sha':identity['source_sha'],'type':'commit'}}
                return {'status':'ahead'}
        ci={'source_sha':self.sha,'artifacts':[],'tested_scopes':[],'documentation_only':True}
        validation={'source_sha':self.sha,**{k:True for k in ('clean','git_identity_verified','prediction_identity_verified','code_root_verified')}}
        out=self.root/'assets'
        def runtime(bundle,sha,output):
            (output/'installation.log').write_text('fixture successful locked installation')
            return validation
        with patch.object(release,'trusted_evidence',return_value=ci),patch.object(release,'validate_runtime',side_effect=runtime):
            manifest=release.build(self.repo,'v1.2.3',out,API())
        verifier.verify(out,self.root/'verified',execute_code=True,expected_sha=self.sha)
        with self.assertRaisesRegex(ValueError,'independently trusted'):verifier.verify(out,self.root/'untrusted',execute_code=True)
        with self.assertRaisesRegex(ValueError,'independently trusted'):verifier.verify(out,self.root/'wrong-expected',execute_code=True,expected_sha='b'*40)
        with patch.object(verifier,'verify_code_root',side_effect=AssertionError('Writer must never execute artifact code')):
            verifier.verify(out,self.root/'writer-verified',execute_code=False)
        with patch.object(verifier,'verify',side_effect=ValueError('untrusted bundle')) as check:
            with self.assertRaisesRegex(ValueError,'untrusted bundle'):release.upload_draft(out,API())
            self.assertIs(check.call_args.kwargs['execute_code'],False)
        self.assertEqual(manifest['tree_sha'],self.git('rev-parse','HEAD^{tree}'))
        import shutil
        for defect in ('INSTALL.txt','installation.log','extra','duplicate'):
            altered=self.root/('assets-'+defect);shutil.copytree(out,altered)
            incomplete=copy.deepcopy(manifest)
            if defect in ('INSTALL.txt','installation.log'):(altered/defect).unlink()
            elif defect=='extra':(altered/'extra.txt').write_text('unexpected')
            else:
                record=dict(name='ci-gateway-fixture.zip',scope='gateway',sha256='b'*64)
                incomplete['ci']['artifacts']=[record,record]
                release.write_json(altered/'ci-evidence.json',incomplete['ci'])
            incomplete['assets']={p.name:release.sha256(p) for p in altered.iterdir() if p.name not in ('release-manifest.json','SHA256SUMS')}
            release.write_json(altered/'release-manifest.json',incomplete)
            sums={p.name:release.sha256(p) for p in altered.iterdir() if p.name!='SHA256SUMS'}
            (altered/'SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k,v in sums.items()))
            with self.subTest(defect=defect),self.assertRaises(ValueError):verifier.verify(altered,self.root/('rejected-inventory-'+defect))
        for field,value in [('source_sha','c'*40),('tree_sha','c'*40),('lock_sha256','c'*64),
                            ('prediction_compatibility',dict(manifest['prediction_compatibility'],reader_generation=3))]:
            modified=dict(manifest,**{field:value})
            release.write_json(out/'release-manifest.json',modified)
            sums={p.name:release.sha256(p) for p in out.iterdir() if p.name!='SHA256SUMS'}
            (out/'SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k,v in sums.items()))
            with self.subTest(field=field),self.assertRaises((ValueError,subprocess.CalledProcessError)):
                verifier.verify(out,self.root/('rejected-'+field))


class DraftWriteBoundaries(unittest.TestCase):
    def test_existing_published_immutable_or_wrong_target_never_writes(self):
        from unittest.mock import patch
        import sys
        sys.path.insert(0,str(ROOT/'scripts'));self.addCleanup(lambda:sys.path.remove(str(ROOT/'scripts')))
        import verify_release_artifacts as verifier
        manifest={'repository':'o/r','tag':'v1.2.3','source_sha':'a'*40,'ci':{}}
        for changes in ({'draft':False},{'immutable':True},{'target_commitish':'b'*40}):
            existing=dict(id=1,tag_name='v1.2.3',target_commitish='a'*40,draft=True,immutable=False,**{})
            existing.update(changes)
            class API:
                repository='o/r'
                def pages(self,path):return [existing]
                def raw(self,*args,**kwargs):raise AssertionError('Rejected release must not write')
            with patch.object(verifier,'verify',return_value=manifest),patch.object(release,'remote_tag_matches'),patch.object(release,'trusted_evidence',return_value={}):
                with self.subTest(changes=changes),self.assertRaises(ValueError):release.upload_draft(Path('/fixture'),API())

    def test_remote_tag_change_during_collection_prevents_creation(self):
        from unittest.mock import patch
        import sys
        sys.path.insert(0,str(ROOT/'scripts'));self.addCleanup(lambda:sys.path.remove(str(ROOT/'scripts')))
        import verify_release_artifacts as verifier
        manifest={'repository':'o/r','tag':'v1.2.3','source_sha':'a'*40,'ci':{}}
        class API:
            repository='o/r'
            def pages(self,path):return []
            def raw(self,*args,**kwargs):raise AssertionError('Changed tag must not write')
        with patch.object(verifier,'verify',return_value=manifest),patch.object(release,'trusted_evidence',return_value={}),patch.object(release,'remote_tag_matches',side_effect=[None,ValueError('tag moved')]):
            with self.assertRaisesRegex(ValueError,'tag moved'):release.upload_draft(Path('/fixture'),API())

    def test_rerun_started_while_collecting_evidence_rejects(self):
        now=datetime(2026,9,30,tzinfo=timezone.utc);sha='a'*40
        run=dict(id=100,run_attempt=1,head_sha=sha,head_branch='main',event='push',path='.github/workflows/ci.yml',status='completed',conclusion='success',updated_at='2026-09-29T00:00:00Z',check_suite_id=90)
        jobs=[dict(name=n,conclusion='success' if n in ('plan','required') else 'skipped') for n in ('plan','required',*release.JOBS)]
        checks=[dict(name='required',head_sha=sha,status='completed',conclusion='success',app={'id':15368},check_suite={'id':90})]
        for mutation in ('attempt','newrun'):
            class API:
                repository='o/r';gets=0;lists=0
                def get(self,path):
                    self.gets+=1
                    return dict(run,run_attempt=2,status='in_progress',conclusion=None) if mutation=='attempt' and self.gets>1 else run
                def pages(self,path,key):
                    if key=='workflow_runs':
                        self.lists+=1
                        return [run,dict(run,id=101,status='queued',conclusion=None)] if mutation=='newrun' and self.lists>1 else [run]
                    if key=='jobs':return jobs
                    if key=='check_runs':return checks
                    return []
            with tempfile.TemporaryDirectory() as t:
                root=Path(t);(root/'uv.lock').write_text('lock')
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):release.trusted_evidence(API(),sha,root,root,now)


if __name__=='__main__':unittest.main()
