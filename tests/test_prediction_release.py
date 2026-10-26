from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.prediction_release import (
    load_prediction_release_manifest,
    load_prediction_runtime_record,
    write_prediction_runtime_record,
)


def test_tracked_prediction_release_manifest_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    release = load_prediction_release_manifest(
        root / "ops" / "prediction-service-release.json"
    )

    assert release.schema_version == "open_trader.prediction_service.release.v1"
    assert release.reader_generation > 0
    assert release.contract_generation > 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "wrong", "reader_generation": 1, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 0, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": True, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 1, "contract_generation": 1, "extra": 1},
    ],
)
def test_release_manifest_rejects_wrong_shape(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="prediction release manifest"):
        load_prediction_release_manifest(path)


def test_runtime_record_is_atomically_replaced_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_release as release_module

    path = tmp_path / "prediction-service-runtime.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = release_module.os.replace

    def replace(source: str, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(release_module.os, "replace", replace)
    previous = {"git_sha": "old", "reader_generation": 1, "contract_generation": 1}
    write_prediction_runtime_record(path, {
        "state": "maintenance",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:00:00+08:00",
        "failure_reason": "",
    })
    write_prediction_runtime_record(path, {
        "state": "failed",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:01:00+08:00",
        "failure_reason": "candidate_not_ready",
    })

    record = load_prediction_runtime_record(path)
    assert record is not None
    assert record["schema_version"] == "open_trader.prediction_service.runtime.v1"
    assert record["state"] == "failed"
    assert record["previous_release"] == previous
    assert len(replacements) == 2
    assert all(destination == path for _source, destination in replacements)
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_runtime_record_rejects_unknown_state_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "prediction-service-runtime.json"
    with pytest.raises(ValueError, match="runtime state"):
        write_prediction_runtime_record(path, {"state": "starting"})
    path.write_text('{"schema_version":"wrong","state":"ready"}', encoding="utf-8")
    with pytest.raises(ValueError, match="runtime record"):
        load_prediction_runtime_record(path)


def test_runtime_writer_keeps_the_fixed_schema(tmp_path: Path) -> None:
    path = tmp_path / "prediction-service-runtime.json"

    write_prediction_runtime_record(
        path, {"schema_version": "wrong", "state": "ready"}
    )

    record = load_prediction_runtime_record(path)
    assert record is not None
    assert record["schema_version"] == "open_trader.prediction_service.runtime.v1"


@pytest.mark.parametrize("state", [[], {}])
def test_runtime_record_rejects_unhashable_state(
    tmp_path: Path, state: object
) -> None:
    path = tmp_path / "prediction-service-runtime.json"

    with pytest.raises(ValueError, match="runtime state"):
        write_prediction_runtime_record(path, {"state": state})
    path.write_text(
        json.dumps({
            "schema_version": "open_trader.prediction_service.runtime.v1",
            "state": state,
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="runtime record"):
        load_prediction_runtime_record(path)


NEW_CI_SCOPES = ('gateway', 'legacy', 'account', 'prediction-1', 'prediction-2', 'prediction-3', 'portable')
OLD_CI_SCOPES = ('gateway', 'legacy', 'account', 'prediction', 'portable')


def _lp_validation_release(path, scopes, *, contract_source=None):
    """Real clean selected source and private config; no admission or source doubles."""
    import hashlib
    import shutil
    import subprocess
    import sys
    from open_trader.prediction_release import LP_VALIDATION_SCHEMA, inspect_lp_capability
    root = path/'release';root.mkdir(parents=True)
    (root/'ops').mkdir();(root/'scripts').mkdir()
    project = Path(__file__).parents[1]
    for name in ('prediction-service-release.json', 'lp-runtime-contract.json'):
        shutil.copyfile(project/'ops'/name,root/'ops'/name)
    (root/'scripts/deployment_preflight.py').write_text(f'SCOPES = {scopes!r}\n' if contract_source is None else contract_source)
    (root/'uv.lock').write_text('independent selected release lock\n')
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()
    git('init','-q');git('add','.')
    git('-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-qm','selected immutable release')
    git('checkout','--detach','-q');sha=git('rev-parse','HEAD')
    runtime=path/'runtime';runtime.mkdir()
    config=runtime/'prediction.json';config.write_text(json.dumps(dict(signer_address='0x'+'a'*40,wallet_address='0x'+'a'*40)));config.chmod(0o600)
    binding=dict(schema_version=LP_VALIDATION_SCHEMA,source_sha=sha,release_root=str(root),runtime_root=str(runtime),
        python=sys.executable,operation_nonce='independent-operator-operation',capability_digest=inspect_lp_capability(root),
        config_selection=dict(path=str(config),sha256=hashlib.sha256(config.read_bytes()).hexdigest()),
        runtime=dict(python=sys.executable,python_version=sys.version,platform=sys.platform,source_root=str(root),
                     lock_sha256=hashlib.sha256((root/'uv.lock').read_bytes()).hexdigest(),extras=[],
                     dependencies={'offline-fixture':'test-only'},provenance='independent local test fixture'),
        ci=dict(run_id=22,run_attempt=2,artifact_role='test-only',
                run_url='https://github.com/raymizzou/open_trader/actions/runs/22',
                artifacts=[dict(scope=scope,id=index,digest='sha256:'+str(index)*64) for index,scope in enumerate(scopes,1)]))
    return root,runtime,config,binding


def test_lp_validation_accepts_selected_release_shard_contract(tmp_path):
    import copy
    import sys
    from open_trader.prediction_release import validate_lp_validation
    root,runtime,config,binding=_lp_validation_release(tmp_path,NEW_CI_SCOPES)
    original=copy.deepcopy(binding)
    assert validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)==original
    assert binding==original


def test_lp_validation_preserves_old_selected_release_contract(tmp_path):
    import copy
    import sys
    from open_trader.prediction_release import validate_lp_validation
    old_root,old_runtime,old_config,old_binding=_lp_validation_release(tmp_path/'old',OLD_CI_SCOPES)
    assert validate_lp_validation(old_binding,release_root=old_root,runtime_root=old_runtime,
                                  python=sys.executable,config_path=old_config)==old_binding
    root,runtime,config,binding=_lp_validation_release(tmp_path/'new',NEW_CI_SCOPES)
    binding['ci']['artifacts']=copy.deepcopy(old_binding['ci']['artifacts'])
    with pytest.raises(ValueError,match='LP trusted CI identity'):
        validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)


@pytest.mark.parametrize('mutation', ['missing','extra','order','scope','id','bool-id','digest','unknown-contract'])
def test_lp_validation_rejects_wrong_or_incomplete_shard_contract(tmp_path,mutation):
    import sys
    from open_trader.prediction_release import validate_lp_validation
    scopes=NEW_CI_SCOPES if mutation!='unknown-contract' else ('gateway','legacy','account','prediction-1','portable')
    root,runtime,config,binding=_lp_validation_release(tmp_path,scopes)
    artifacts=binding['ci']['artifacts']
    if mutation=='missing':artifacts.pop()
    elif mutation=='extra':artifacts.append(dict(scope='extra',id=8,digest='sha256:'+'8'*64))
    elif mutation=='order':artifacts[0],artifacts[1]=artifacts[1],artifacts[0]
    elif mutation=='scope':artifacts[1]['scope']=artifacts[0]['scope']
    elif mutation=='id':artifacts[1]['id']=artifacts[0]['id']
    elif mutation=='bool-id':artifacts[0]['id']=True
    elif mutation=='digest':artifacts[0]['digest']='sha256:not-a-digest'
    with pytest.raises(ValueError,match='LP (trusted|selected CI)'):
        validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)


def test_lp_validation_reads_scopes_without_executing_contract(tmp_path):
    import sys
    from open_trader.prediction_release import validate_lp_validation
    sentinel=tmp_path/'contract-executed'
    script=f'SCOPES = {NEW_CI_SCOPES!r}\nfrom pathlib import Path\nPath({str(sentinel)!r}).touch()\n'
    root,runtime,config,binding=_lp_validation_release(tmp_path,NEW_CI_SCOPES,contract_source=script)
    writes=[]
    original_class=sys.__class__
    class ObservedInterpreter(original_class):
        def __setattr__(self,name,value):
            if name in ('dont_write_bytecode','pycache_prefix'):writes.append(name)
            super().__setattr__(name,value)
    try:
        sys.__class__=ObservedInterpreter
        result=validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)
    finally:sys.__class__=original_class
    assert result==binding
    assert not sentinel.exists(), 'selected contract source was executed'
    assert writes==[], 'selected contract mutated interpreter bytecode state'


def test_lp_validation_accepts_real_preflight_source(tmp_path):
    import sys
    from open_trader.prediction_release import validate_lp_validation
    script=(Path(__file__).resolve().parents[1]/'scripts/deployment_preflight.py').read_text()
    root,runtime,config,binding=_lp_validation_release(tmp_path,NEW_CI_SCOPES,contract_source=script)
    assert validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)==binding


@pytest.mark.parametrize('shape',['missing','ambiguous','nonliteral','augmented'])
def test_lp_validation_rejects_nonliteral_or_ambiguous_scopes(tmp_path,shape):
    import sys
    from open_trader.prediction_release import validate_lp_validation
    literal=f'SCOPES = {NEW_CI_SCOPES!r}\n'
    scripts={'missing':'OTHER = ()\n','ambiguous':literal+literal,
        'nonliteral':f'SCOPES = tuple({NEW_CI_SCOPES!r})\n','augmented':literal+'SCOPES += ()\n'}
    root,runtime,config,binding=_lp_validation_release(tmp_path,NEW_CI_SCOPES,contract_source=scripts[shape])
    with pytest.raises(ValueError,match='LP selected CI contract'):
        validate_lp_validation(binding,release_root=root,runtime_root=runtime,python=sys.executable,config_path=config)
