"""Literal selection and aggregate contracts for independent CI machines."""
from scripts import ci_partition


def test_full_partition_collection_is_not_narrowed_by_inherited_shard(tmp_path):
    import json,os,subprocess,sys,shutil
    from pathlib import Path
    from tests.test_ci_optional import policy_root
    policy_root(tmp_path)
    source=Path(__file__).resolve().parents[1]
    for name in ('ci_partition.py','ci_evidence.py'):
        shutil.copyfile(source/'scripts'/name,tmp_path/'scripts'/name)
    (tmp_path/'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
    run=subprocess.run([sys.executable,str(tmp_path/'scripts/ci_partition.py')],cwd=tmp_path,
        env=dict(os.environ,CI_TEST_SCOPE='prediction-1',PYTHONSAFEPATH='1',
            PYTHONPATH=str(tmp_path/'scripts')),capture_output=True,text=True)
    assert run.returncode==0,run.stdout+run.stderr
    proof=json.loads(run.stdout)
    assert proof['complete_count']==308 and proof['partition_count']==6
    assert proof['optional_count']==273 and proof['retired_count']==29


def test_real_prediction_shards_execute_two_workers_without_losing_literal_ids(tmp_path):
    import json,os,subprocess,sys
    from pathlib import Path
    from tests.test_ci_optional import policy_root
    policy_root(tmp_path)
    sample=tmp_path/'tests/test_lp_shard_sample.py'
    sample.write_text('import pytest\n@pytest.mark.xdist_group("literal_group")\n'
        '@pytest.mark.parametrize("case",["one@literal","two"])\ndef test_group(case): pass\n'+
        ''.join(f'def test_case_{index}(): pass\n' for index in range(24)))
    nodes=['tests/test_lp_shard_sample.py::test_group[one@literal]',
        'tests/test_lp_shard_sample.py::test_group[two]']+[
        f'tests/test_lp_shard_sample.py::test_case_{index}' for index in range(24)]
    executed=[]
    for index in range(1,4):
        directory=tmp_path/f'evidence-{index}'
        env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONPATH=str(Path(__file__).resolve().parents[1]),
            CI_TEST_SCOPE=f'prediction-{index}',CI_TEST_WORKERS='2',CI_TEST_SOURCE_SHA='a'*40,
            CI_TEST_METRICS=str(directory/'metrics.json'))
        run=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider',
            '-p','scripts.ci_partition','-p','scripts.ci_test_metrics','-p','xdist.plugin',
            '-n','2','--dist=loadgroup','--ci-required','--rootdir='+str(tmp_path),str(sample)],cwd=tmp_path,env=env,capture_output=True,text=True)
        assert run.returncode==0,run.stdout+run.stderr
        metrics=json.loads((directory/'metrics.json').read_text())
        expected=ci_partition.shard_nodeids('prediction',nodes,index)
        assert metrics['selected_nodeids']==expected
        assert sorted(row['nodeid'] for row in metrics['results'])==expected
        assert metrics['workers']==2 and metrics['exit_status']==0 and metrics['complete']
        grouped=[row for row in metrics['results'] if '::test_group[' in row['nodeid']]
        if grouped:
            assert len(grouped)==2
            assert len({row['phases']['call']['worker'] for row in grouped})==1
        executed+=expected
    assert sorted(executed)==sorted(nodes) and len(executed)==len(set(executed))


def test_prediction_shards_cover_required_nodes_once(tmp_path,monkeypatch):
    nodes = [
        'tests/test_lp_example.py::test_buy[yes]',
        'tests/test_lp_example.py::test_buy[no]',
        'tests/test_lp_example.py::test_cancel',
        'tests/test_prediction_cloud.py::test_owner[air]',
        'tests/test_prediction_client.py::test_owner[mini]',
        'tests/test_prediction_runtime.py::test_stop',
    ]
    shards = [ci_partition.shard_nodeids('prediction', nodes, index) for index in range(1, 4)]
    assert sorted(sum(shards, [])) == sorted(nodes)
    assert all(not set(left) & set(right) for i, left in enumerate(shards) for right in shards[i+1:])
    for group in (nodes[:2], nodes[3:5]):
        assert sum(set(group) <= set(shard) for shard in shards) == 1
    assert shards == [ci_partition.shard_nodeids('prediction', list(reversed(nodes)), i) for i in range(1, 4)]
    # Exercise the public pytest selector, not only the pure selector helper.
    from tests.test_ci_optional import policy_root
    policy_root(tmp_path)
    (tmp_path/'tests/test_lp_example.py').write_text('import pytest\n@pytest.mark.parametrize("side",["yes","no"])\ndef test_buy(side): pass\ndef test_cancel(): pass\n')
    for name,owner in (('cloud','air'),('client','mini')):
        (tmp_path/f'tests/test_prediction_{name}.py').write_text('import pytest\npytestmark=pytest.mark.xdist_group("prediction_cloud_ports")\n'
            f'@pytest.mark.parametrize("owner",[{owner!r}])\ndef test_owner(owner): pass\n')
    (tmp_path/'tests/test_prediction_runtime.py').write_text('def test_stop(): pass\n')
    paths=sorted({node.split('::',1)[0] for node in nodes})
    for index in range(1,4):
        monkeypatch.setenv('CI_TEST_SCOPE',f'prediction-{index}')
        actual=ci_partition.collect(tmp_path,paths,tmp_path/f'prediction-{index}.json',required=True)
        assert sorted(actual)==shards[index-1]


def test_advisory_shards_cover_nodes_once_and_split_lp410(tmp_path,monkeypatch):
    nodes = [
        'tests/test_lp410_launchd_recovery.py::test_installer_retains_verified_manifest_after_ready_and_refresh[default]',
        'tests/test_lp410_launchd_recovery.py::test_installer_retains_verified_manifest_after_ready_and_refresh[custom]',
        'tests/test_lp410_launchd_recovery.py::test_failed_loaded_preflight_is_read_only_recoverable[legacy]',
        'tests/test_lp410_launchd_recovery.py::test_explicit_restart_recovers_only_verified_failed_loaded_service[failed]',
        'tests/test_lp410_launchd_recovery.py::test_failed_loaded_recovery_rejects_uncertain_or_changed_identity[changed]',
        'tests/test_lp410_launchd_recovery.py::test_failed_restart_never_rearms_before_complete_teardown[writer]',
        'tests/test_lp410_launchd_recovery.py::test_failed_restart_checks_process_started_after_final_observation[late]',
        'tests/test_deploy_release.py::test_rollback',
    ]
    shards = [ci_partition.shard_nodeids('release-advisory', nodes, index) for index in range(1, 5)]
    assert sorted(sum(shards, [])) == sorted(nodes)
    assert sum(any('test_lp410_' in node for node in shard) for shard in shards) == 2
    assert all(not set(left) & set(right) for i, left in enumerate(shards) for right in shards[i+1:])
    from tests.test_ci_optional import policy_root
    from scripts.ci_evidence import optional_policy
    policy_root(tmp_path)
    approved=optional_policy(tmp_path)['optional_nodeids']
    actual=[]
    for index in range(1,5):
        monkeypatch.setenv('CI_TEST_SCOPE',f'release-advisory-{index}')
        selected=ci_partition.collect(tmp_path,approved,tmp_path/f'advisory-{index}.json')
        assert sorted(selected)==ci_partition.shard_nodeids('release-advisory',approved,index)
        actual.append(selected)
    assert sorted(sum(actual,[]))==approved
    assert sum(any(node.startswith('tests/test_lp410_launchd_recovery.py::') for node in selected) for selected in actual)==2
    monkeypatch.setenv('CI_TEST_SCOPE','prediction-1')
    path=tmp_path/'tests/test_lp_business_sample.py';path.write_text('def test_money(): pass\n')
    required=[]
    for index in range(1,4):
        monkeypatch.setenv('CI_TEST_SCOPE',f'prediction-{index}')
        required+=ci_partition.collect(tmp_path,[str(path)],tmp_path/f'business-{index}.json',required=True)
    assert required==['tests/test_lp_business_sample.py::test_money']


def test_required_rejects_missing_duplicate_or_failed_shards(tmp_path):
    import copy
    import pytest
    from scripts import ci_evidence
    nodes = ['tests/test_lp_example.py::test_buy[a]', 'tests/test_lp_example.py::test_cancel',
             'tests/test_prediction_runtime.py::test_stop']
    proof = {'partitions': {'gateway': ['tests/test_frontend_gateway.py::test_ok'],
                            'legacy': ['tests/test_dashboard_web.py::test_ok'],
                            'account': ['tests/test_account_api.py::test_ok'], 'prediction': nodes}}
    records = []
    for scope in ('gateway', 'legacy', 'account', 'prediction-1', 'prediction-2', 'prediction-3', 'portable'):
        selected = (ci_partition.shard_nodeids('prediction', nodes, int(scope[-1])) if scope.startswith('prediction-')
                    else proof['partitions'].get(scope, ['acceptance/test_prediction_arbitrage_scenarios.py::test_ok']))
        records.append(dict(scope=scope,source_sha='a'*40,run_id='22',run_attempt='2',status='success',
                            exit_status=0,selected_nodeids=selected))
    assert ci_evidence.validate_required_shards(records, proof, 'a'*40, '22', '2') is None
    # Advisory is a separate verdict: its failure is not an input to required.
    invalid_cases=[records[:-1],records+[records[0]]]
    for key,value in (('status','failure'),('exit_status',False),('run_id',22),('run_attempt',True),('source_sha','b'*40)):
        invalid=copy.deepcopy(records);invalid[3][key]=value;invalid_cases.append(invalid)
    for invalid in invalid_cases:
        with pytest.raises(ValueError):
            ci_evidence.validate_required_shards(invalid, proof, 'a'*40, '22', '2')

    # The public aggregate also verifies the actual payloads, hashes and node union.
    import json,os,zipfile,io,shutil
    from tests.test_deployment_preflight import evidence,SHA
    root=tmp_path/'source';root.mkdir()
    root,api=evidence.__wrapped__(root)
    directory=tmp_path/'artifacts';directory.mkdir()
    for artifact in api.artifacts:
        target=directory/artifact['name'];target.mkdir()
        with zipfile.ZipFile(io.BytesIO(api.downloads[artifact['id']])) as archive:
            for name in archive.namelist():(target/name).write_bytes(archive.read(name))
    env=dict(GITHUB_SHA=SHA,GITHUB_RUN_ID='22',GITHUB_RUN_ATTEMPT='2',GITHUB_EVENT_NAME='push')
    assert ci_evidence.validate_required_directory(root,directory,env) is None
    # Exercise the workflow's real required entry point on the same seven payloads.
    import subprocess,sys
    from pathlib import Path
    from tests.test_ci_routing import RequiredTests
    for name in ('ci_plan.py','ci_evidence.py','ci_partition.py','deployment_preflight.py'):
        shutil.copy(Path(__file__).resolve().parents[1]/'scripts'/name,root/'scripts'/name)
    needs=RequiredTests().needs([])
    needs['release_advisory']={'result':'failure'}
    def aggregate(path):
        return subprocess.run([sys.executable,str(root/'scripts/ci_plan.py'),'required','--evidence',str(path)],
            input=json.dumps(needs),text=True,capture_output=True,cwd=root,env=dict(os.environ,**env))
    result=aggregate(directory)
    assert result.returncode == 0, result.stdout + result.stderr
    for key,value in (('GITHUB_SHA',True),('GITHUB_RUN_ID',22),('GITHUB_RUN_ATTEMPT','0'),('GITHUB_EVENT_NAME','pull_request')):
        with pytest.raises(ValueError):ci_evidence.validate_required_directory(root,directory,dict(env,**{key:value}))
    for mutation in ('missing','extra','wrong-run','failed','hash','node-union'):
        altered=tmp_path/mutation;shutil.copytree(directory,altered)
        shard=altered/f'ci-prediction-1-{SHA}-22-2'
        if mutation=='missing':shutil.rmtree(shard)
        elif mutation=='extra':shutil.copytree(shard,altered/f'ci-prediction-4-{SHA}-22-2')
        else:
            metadata=json.loads((shard/'evidence.json').read_text())
            if mutation=='wrong-run':metadata['run_id']='23'
            elif mutation=='failed':metadata['status']='failure';metadata['exit_status']=1
            elif mutation=='hash':(shard/'metrics.json').write_text('{}')
            else:
                from tests.test_deployment_preflight import literal_junit
                import hashlib
                metrics=json.loads((shard/'metrics.json').read_text())
                new_node='tests/test_lp_example.py::test_uncollected'
                row=copy.deepcopy(metrics['results'][0]);row.update(nodeid=new_node,execution_nodeid=new_node)
                metrics['results'].append(row);metrics['results'].sort(key=lambda row:row['nodeid'])
                metrics['selected_nodeids']=sorted(metrics['selected_nodeids']+[new_node])
                metadata['selected_nodeids']=metrics['selected_nodeids']
                files={'metrics.json':json.dumps(metrics).encode(),
                    'selected-nodeids.json':json.dumps(metrics['selected_nodeids']).encode(),'junit.xml':literal_junit(metrics)}
                for name,data in files.items():
                    (shard/name).write_bytes(data)
                    key={'metrics.json':'metrics_sha256','selected-nodeids.json':'selected_nodeids_sha256','junit.xml':'junit_sha256'}[name]
                    metadata['environment'][key]=hashlib.sha256(data).hexdigest()
            (shard/'evidence.json').write_text(json.dumps(metadata))
        with pytest.raises(ValueError):ci_evidence.validate_required_directory(root,altered,env)
        result=aggregate(altered)
        assert result.returncode != 0, mutation


def test_collection_through_symlink_preserves_literal_nodeids(tmp_path):
    from tests.test_ci_optional import policy_root, ROLLBACK
    source=tmp_path/'source';source.mkdir();policy_root(source)
    (source/'tests/test_lp_d2_service_lifecycle.py').write_text(
        'def test_supported_rollback_uses_current_api_without_disposable_record_audit(): pass\n'
        'def test_new(): pass\n')
    (source/'tests/test_lp_symlink.py').write_text(
        'import pytest\n@pytest.mark.parametrize("case", ["one@literal", "two"])\n'
        'def test_identity(case): pass\n')
    entrance=tmp_path/'entrance';entrance.symlink_to(source,target_is_directory=True)
    paths=['tests/test_lp_d2_service_lifecycle.py','tests/test_lp_symlink.py']
    expected=['tests/test_lp_d2_service_lifecycle.py::test_new',
        'tests/test_lp_symlink.py::test_identity[one@literal]',
        'tests/test_lp_symlink.py::test_identity[two]']
    full=ci_partition.collect(entrance,paths,tmp_path/'full.json')
    required=ci_partition.collect(entrance,paths,tmp_path/'required.json',required=True)
    assert sorted(full)==sorted(expected+[ROLLBACK])
    assert sorted(required)==expected


def test_required_cli_reports_artifact_failure_reason(tmp_path):
    import io,json,os,shutil,subprocess,sys,zipfile
    from pathlib import Path
    from tests.test_ci_routing import RequiredTests
    from tests.test_deployment_preflight import evidence,SHA
    root=tmp_path/'source';root.mkdir();root,api=evidence.__wrapped__(root)
    for name in ('ci_plan.py','ci_evidence.py','ci_partition.py','deployment_preflight.py'):
        shutil.copy(Path(__file__).resolve().parents[1]/'scripts'/name,root/'scripts'/name)
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    env=dict(os.environ,GITHUB_SHA=SHA,GITHUB_RUN_ID='22',GITHUB_RUN_ATTEMPT='2',GITHUB_EVENT_NAME='push')
    def aggregate(body):
        return subprocess.run([sys.executable,str(root/'scripts/ci_plan.py'),'required','--evidence',str(artifacts)],
            cwd=root,env=env,input=body,text=True,capture_output=True)
    bad_job=aggregate('{')
    assert bad_job.returncode!=0 and 'invalid job-result JSON' in bad_job.stdout
    needs=json.dumps(RequiredTests().needs([]))
    absent=aggregate(needs)
    assert absent.returncode!=0
    assert 'artifact validation failed:' in absent.stdout and 'required artifact directory' in absent.stdout
    assert 'invalid job-result JSON' not in absent.stdout
    for artifact in api.artifacts:
        target=artifacts/artifact['name'];target.mkdir()
        with zipfile.ZipFile(io.BytesIO(api.downloads[artifact['id']])) as archive:
            for name in archive.namelist():(target/name).write_bytes(archive.read(name))
    (artifacts/f'ci-prediction-1-{SHA}-22-2'/'metrics.json').write_text('{}')
    damaged=aggregate(needs)
    assert damaged.returncode!=0
    assert 'artifact validation failed:' in damaged.stdout and 'hash' in damaged.stdout.lower()
    assert 'invalid job-result JSON' not in damaged.stdout
    assert len(damaged.stdout)<512 and 'Traceback' not in damaged.stderr


def test_required_pr_evidence_binds_merge_ref_and_stays_non_deployable(tmp_path):
    import io,json,os,shutil,subprocess,sys,zipfile
    from pathlib import Path
    import pytest
    from scripts import ci_evidence
    from tests.test_ci_routing import RequiredTests
    from tests.test_deployment_preflight import NOW,SHA,evidence,pack,preflight
    root=tmp_path/'source';root.mkdir()
    root,api=evidence.__wrapped__(root)
    directory=tmp_path/'artifacts';directory.mkdir()
    ref='refs/pull/473/merge'
    workflow_ref='raymizzou/open_trader/.github/workflows/ci.yml@'+ref
    for artifact in api.artifacts:
        target=directory/artifact['name'];target.mkdir()
        with zipfile.ZipFile(io.BytesIO(api.downloads[artifact['id']])) as archive:
            files={name:archive.read(name) for name in archive.namelist()}
        metadata=json.loads(files['evidence.json'])
        metadata.update(event_name='pull_request',ref=ref,workflow_ref=workflow_ref)
        files['evidence.json']=json.dumps(metadata).encode()
        api.downloads[artifact['id']]=pack(files)
        for name,data in files.items():(target/name).write_bytes(data)
    env=dict(GITHUB_SHA=SHA,GITHUB_RUN_ID='22',GITHUB_RUN_ATTEMPT='2',
             GITHUB_EVENT_NAME='pull_request',GITHUB_REF=ref)
    assert ci_evidence.validate_required_directory(root,directory,env) is None
    for name in ('ci_plan.py','ci_evidence.py','ci_partition.py','deployment_preflight.py'):
        shutil.copy(Path(__file__).resolve().parents[1]/'scripts'/name,root/'scripts'/name)
    needs=json.dumps(RequiredTests().needs([]))
    def aggregate(environment):
        return subprocess.run([sys.executable,str(root/'scripts/ci_plan.py'),'required','--evidence',str(directory)],
            cwd=root,env=dict(os.environ,**environment),input=needs,text=True,capture_output=True)
    result=aggregate(env)
    assert result.returncode==0,result.stdout+result.stderr
    api.run.update(event='pull_request',head_branch='feature')
    with pytest.raises(preflight.PreflightError):preflight.verify_ci(api,SHA,root,NOW)
    for artifact in api.artifacts:
        files={item.name:item.read_bytes() for item in (directory/artifact['name']).iterdir()}
        scope=json.loads(files['evidence.json'])['scope']
        with pytest.raises(preflight.PreflightError):
            preflight.verify_artifact_files(files,scope,api.run,root)
    # Identical candidate/run evidence cannot cross an event or PR-ref boundary.
    for key,value in (('GITHUB_EVENT_NAME','push'),('GITHUB_EVENT_NAME','workflow_dispatch'),
                      ('GITHUB_REF','refs/pull/474/merge')):
        altered=dict(env,**{key:value})
        with pytest.raises(ValueError):ci_evidence.validate_required_directory(root,directory,altered)
        assert aggregate(altered).returncode!=0
    shard=directory/f'ci-prediction-1-{SHA}-22-2'/'evidence.json'
    original=shard.read_bytes()
    for key,value in [('event_name','push'),('event_name','workflow_dispatch'),
            ('ref','refs/pull/474/merge'),('ref','refs/pull/473/head'),('ref','refs/heads/main'),('ref',None),
            ('workflow_ref','raymizzou/open_trader/.github/workflows/ci.yml@refs/pull/474/merge'),
            ('workflow_ref','raymizzou/open_trader/.github/workflows/ci.yml@refs/pull/473/head'),
            ('workflow_ref','raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main')]:
        metadata=json.loads(original)
        if value is None:metadata.pop(key)
        else:metadata[key]=value
        shard.write_text(json.dumps(metadata))
        with pytest.raises(ValueError):ci_evidence.validate_required_directory(root,directory,env)
        result=aggregate(env)
        assert result.returncode!=0,(key,value,result.stdout,result.stderr)
        shard.write_bytes(original)
    # Main push and manual aggregation keep their existing exact-main contract.
    for event in ('push','workflow_dispatch'):
        main_env=dict(env,GITHUB_EVENT_NAME=event,GITHUB_REF='refs/heads/main')
        for artifact in api.artifacts:
            path=directory/artifact['name']/'evidence.json'
            metadata=json.loads(path.read_text())
            metadata.update(event_name=event,ref='refs/heads/main',
                workflow_ref='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main')
            path.write_text(json.dumps(metadata))
        assert ci_evidence.validate_required_directory(root,directory,main_env) is None
        result=aggregate(main_env)
        assert result.returncode==0,result.stdout+result.stderr


def test_required_rejects_invalid_event_ref(tmp_path):
    import pytest
    from scripts import ci_evidence
    from tests.test_deployment_preflight import SHA
    env=dict(GITHUB_SHA=SHA,GITHUB_RUN_ID='22',GITHUB_RUN_ATTEMPT='2',GITHUB_EVENT_NAME='pull_request')
    for ref in (None,'',True,'refs/pull/0/merge','refs/pull/0473/merge','refs/pull/-1/merge',
                'refs/pull/473/head','refs/heads/main','refs/pull/473/merge\n'):
        invalid=dict(env)
        if ref is not None:invalid['GITHUB_REF']=ref
        with pytest.raises(ValueError,match='invalid aggregate CI'):
            ci_evidence.validate_required_directory(tmp_path,tmp_path,invalid)
    with pytest.raises(ValueError,match='invalid aggregate CI'):
        ci_evidence.validate_required_directory(tmp_path,tmp_path,
            dict(env,GITHUB_EVENT_NAME='pull_request_target',GITHUB_REF='refs/pull/473/merge'))
