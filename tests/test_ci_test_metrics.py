"""Real pytest subprocess evidence; short fixtures are not business timing data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


def run_sample(root, source, workers=1, filename="test_sample.py"):
    (root/'tests').mkdir()
    (root/'tests'/filename).write_text(source)
    directory=root/'records'
    env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONPATH=str(ROOT),
             CI_TEST_METRICS=str(directory/'metrics.json'),CI_TEST_SOURCE_SHA=SHA,CI_TEST_WORKERS=str(workers))
    run=subprocess.run([sys.executable,'-m','pytest','-q','-p','scripts.ci_test_metrics',
                        '--junitxml='+str(directory/'junit.xml'),*( ['-p','xdist.plugin','-n','2','--dist=loadgroup'] if workers==2 else []),'tests'],cwd=root,env=env,capture_output=True,text=True)
    return run, directory


# Imported only when pytest executes this legacy contract module; host unittest
# discovery imports no optional pytest dependency.
def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ in ('test_real_pytest_reports_phases_nodeids_and_outcomes',
                                    'test_successful_pytest_evidence_is_accepted'):
        metafunc.parametrize('workers',[1,2],ids=['serial','xdist2'])


def test_real_pytest_reports_phases_nodeids_and_outcomes(tmp_path, workers):
    run,directory=run_sample(tmp_path,'''import pytest

@pytest.mark.xdist_group("shared_fixture")
@pytest.mark.parametrize("value",["literal@parameter"])
def test_first(value): pass
def test_second(): pass
def test_failure(): assert False, "intentional fixture failure"
@pytest.mark.skip(reason="fixture skip")
def test_skip(): pass
''',workers)
    assert run.returncode == 1, run.stdout+run.stderr
    metrics=json.loads((directory/'metrics.json').read_text())
    expected={'tests/test_sample.py::test_first[literal@parameter]':'passed','tests/test_sample.py::test_second':'passed',
              'tests/test_sample.py::test_failure':'failed','tests/test_sample.py::test_skip':'skipped'}
    assert metrics['selected_nodeids']==sorted(expected)
    assert json.loads((directory/'selected-nodeids.json').read_text())==sorted(expected)
    assert {item['nodeid']:item['outcome'] for item in metrics['results']}==expected
    assert metrics['source_sha']==SHA
    assert metrics['workers']==workers
    assert metrics['architecture']==os.uname().machine
    if sys.platform == 'darwin':
        native=subprocess.run(['/usr/sbin/sysctl','-n','machdep.cpu.brand_string'],capture_output=True,text=True)
        if native.returncode==0 and native.stdout.strip():
            assert metrics['cpu_model']==native.stdout.strip()
            assert metrics['cpu_model_source']=='sysctl:machdep.cpu.brand_string'
        else:
            assert metrics['cpu_model'] is None
            assert metrics['cpu_model_source']=='unavailable'
    elif sys.platform.startswith('linux'):
        try:
            cpuinfo=Path('/proc/cpuinfo').read_text()
        except OSError:
            cpuinfo=''
        lines=[line.split(':',1)[1].strip() for line in cpuinfo.splitlines()
               if line.split(':',1)[0].strip() in ('model name','Hardware') and ':' in line]
        if lines:
            assert metrics['cpu_model']==lines[0]
            assert metrics['cpu_model_source']=='/proc/cpuinfo'
        else:
            assert metrics['cpu_model'] is None
            assert metrics['cpu_model_source']=='unavailable'
    assert metrics['cpu_count']==os.cpu_count()
    assert metrics['wall_seconds']>=0
    assert metrics['exit_status']==1
    assert metrics['complete'] is True
    assert metrics['collection_errors']==[]
    for result in metrics['results']:
        assert 'setup' in result['phases'] and 'teardown' in result['phases']
        if result['outcome']!='skipped': assert 'call' in result['phases']
        assert all(phase['duration']>=0 for phase in result['phases'].values())
        assert {phase['worker'] for phase in result['phases'].values()} <= ({'gw0','gw1'} if workers==2 else {'serial'})
    cases=ET.parse(directory/'junit.xml').findall('.//testcase')
    assert len(cases)==4
    assert len(ET.parse(directory/'junit.xml').findall('.//failure'))==1
    assert len(ET.parse(directory/'junit.xml').findall('.//skipped'))==1


def test_collection_error_preserves_failure_evidence(tmp_path):
    run,directory=run_sample(tmp_path,'import missing_ci_metrics_fixture_dependency\n')
    assert run.returncode==2,run.stdout+run.stderr
    metrics=json.loads((directory/'metrics.json').read_text())
    assert metrics['exit_status']==2
    assert metrics['complete'] is False
    assert metrics['collection_errors']
    assert 'missing_ci_metrics_fixture_dependency' in metrics['collection_errors'][0]['detail']
    assert metrics['results']==[]
    assert metrics['selected_nodeids']==[]
    assert ET.parse(directory/'junit.xml').findall('.//error')


def test_unavailable_cpu_model_is_explicit(tmp_path):
    # Only OS identity reads are replaced inside the real pytest subprocess.
    (tmp_path/'conftest.py').write_text('''from pathlib import Path
import subprocess
original_read=Path.read_text
original_run=subprocess.run

def unavailable_read(path,*args,**kwargs):
    if str(path)=="/proc/cpuinfo": raise OSError("fixture OS identity unavailable")
    return original_read(path,*args,**kwargs)

def unavailable_run(command,*args,**kwargs):
    if command[:1]==["/usr/sbin/sysctl"]:
        return subprocess.CompletedProcess(command,1,"","fixture OS identity unavailable")
    return original_run(command,*args,**kwargs)

Path.read_text=unavailable_read
subprocess.run=unavailable_run
''')
    run,directory=run_sample(tmp_path,'def test_ok(): pass\n')
    assert run.returncode==0,run.stdout+run.stderr
    metrics=json.loads((directory/'metrics.json').read_text())
    assert metrics['cpu_model'] is None
    assert metrics['cpu_model_source']=='unavailable'
    assert metrics['cpu_model_unavailable']
    assert metrics['architecture']==os.uname().machine
    assert metrics['complete'] is True
    assert metrics['exit_status']==0


def test_successful_pytest_evidence_is_accepted(tmp_path, workers):
    filename='test_dashboard_web.py' if workers==1 else 'test_prediction_runtime.py'
    run,directory=run_sample(tmp_path,'''import pytest
@pytest.mark.xdist_group("shared_fixture")
@pytest.mark.parametrize("value",["literal@parameter"])
def test_first(value): pass
def test_second(): pass
@pytest.mark.skip(reason="fixture skip")
def test_skip(): pass
''',workers,filename)
    assert run.returncode==0,run.stdout+run.stderr
    import importlib.util
    spec=importlib.util.spec_from_file_location('real_evidence_consumer',ROOT/'scripts/ci_evidence.py')
    consumer=importlib.util.module_from_spec(spec);spec.loader.exec_module(consumer)
    (directory/'image.json').write_text('[{"Id":"sha256:fixture-test-image"}]')
    (directory/'dependency-manifest.json').write_text('{"role":"test-only fixture"}')
    env=dict(GITHUB_SHA=SHA,GITHUB_REPOSITORY='raymizzou/open_trader',
             GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
             GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/main',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2',
             RUNNER_OS='macOS' if sys.platform=='darwin' else 'Linux',RUNNER_ARCH=os.uname().machine)
    scope='legacy' if workers==1 else 'prediction'
    evidence=consumer.build_evidence(ROOT,directory,scope,'0',workers,0,env)
    expected={f'tests/{filename}::test_first[literal@parameter]':'passed',
              f'tests/{filename}::test_second':'passed',f'tests/{filename}::test_skip':'skipped'}
    assert evidence['status']=='success'
    assert evidence['selected_nodeids']==sorted(expected)
    metrics=json.loads((directory/'metrics.json').read_text())
    assert {result['nodeid']:result['outcome'] for result in metrics['results']}==expected
    assert evidence['environment']['metrics_sha256']==consumer.file_sha256(directory/'metrics.json')
    assert evidence['environment']['junit_sha256']==consumer.file_sha256(directory/'junit.xml')
