import io
import json
import time
import traceback

import pytest
import requests

from open_trader.polymarket_trading import (
    KeychainError, load_keychain_secret, load_predict_api_key, store_keychain_secret,
)


def test_ssm_reads_pinned_bundle_using_only_instance_role(monkeypatch, caplog):
    monkeypatch.setenv('OPEN_TRADER_CREDENTIAL_BACKEND', 'tencent-ssm')
    monkeypatch.setenv('OPEN_TRADER_SSM_REGION', 'ap-hongkong')
    monkeypatch.setenv('OPEN_TRADER_SSM_SECRET', 'prediction-test')
    monkeypatch.setenv('OPEN_TRADER_SSM_VERSION', 'v1')
    monkeypatch.setenv('OPEN_TRADER_SSM_ROLE', 'prediction-reader')
    # Long-lived credentials must never override the instance role.
    monkeypatch.setenv('TENCENTCLOUD_SECRET_ID', 'forbidden-long-lived-id')
    monkeypatch.setenv('TENCENTCLOUD_SECRET_KEY', 'forbidden-long-lived-key')
    calls = []
    def forbid_keychain(*args, **kwargs):
        raise AssertionError("Keychain fallback is forbidden")
    monkeypatch.setattr("subprocess.run", forbid_keychain)
    class MetadataConnection:
        def __init__(self, host, timeout):
            assert host == 'metadata.tencentyun.com' and timeout <= 5
        def request(self, method, path):
            assert method == 'GET'
            assert path == '/latest/meta-data/cam/security-credentials/prediction-reader'
        def getresponse(self):
            response = io.BytesIO(json.dumps(dict(Code='Success', TmpSecretId='temporary-id',
                TmpSecretKey='temporary-key', Token='temporary-token', ExpiredTime=int(time.time())+600)).encode())
            response.status = 200
            return response
        def close(self): pass
    monkeypatch.setattr('http.client.HTTPConnection', MetadataConnection)
    def request(self, **kwargs):
        calls.append(kwargs)
        assert kwargs['url'].rstrip('/') == 'https://ssm.tencentcloudapi.com'
        assert 'temporary-id' in kwargs['headers']['Authorization']
        assert kwargs['headers']['X-TC-Token'] == 'temporary-token'
        assert json.loads(kwargs['data']) == dict(SecretName='prediction-test', VersionId='v1')
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'Response': {'SecretName': 'prediction-test', 'VersionId': 'v1',
            'SecretString': json.dumps({'com.open-trader.polymarket': {'signing-private-key': 'wallet-sentinel'},
                                       'com.open-trader.predict': {'api-key': 'predict-sentinel'}}),
            'RequestId': 'test'}}).encode()
        return response
    monkeypatch.setattr(requests.Session, 'request', request)
    caplog.set_level('DEBUG')
    assert load_keychain_secret('signing-private-key') == 'wallet-sentinel'
    assert load_predict_api_key() == 'predict-sentinel'
    assert len(calls) == 1
    assert 'sentinel' not in caplog.text and 'temporary-token' not in caplog.text
    with pytest.raises(KeychainError):
        store_keychain_secret('signing-private-key', 'must-not-be-written')


def test_unknown_backend_fails_closed_without_keychain(monkeypatch):
    monkeypatch.setenv('OPEN_TRADER_CREDENTIAL_BACKEND', 'typo')
    calls = []
    with pytest.raises(KeychainError):
        load_keychain_secret('builder-key', run=lambda *a, **k: calls.append(a))
    assert calls == []


@pytest.mark.parametrize('failure', ['denied', 'expired', 'missing', 'wrong-version'])
def test_ssm_failure_is_redacted_and_never_uses_keychain(monkeypatch, failure, caplog):
    for key, value in dict(CREDENTIAL_BACKEND='tencent-ssm', SSM_REGION='ap-hongkong',
                           SSM_SECRET='failure-'+failure, SSM_VERSION='v1', SSM_ROLE='reader').items():
        monkeypatch.setenv('OPEN_TRADER_'+key, value)
    class MetadataConnection:
        def __init__(self, *a, **k): pass
        def request(self, *a): pass
        def getresponse(self):
            response = io.BytesIO(json.dumps(dict(Code='Success', TmpSecretId='temp-id', TmpSecretKey='temp-key',
                Token='temp-token', ExpiredTime=0 if failure == 'expired' else int(time.time())+600)).encode())
            response.status = 200
            return response
        def close(self): pass
    monkeypatch.setattr('http.client.HTTPConnection', MetadataConnection)
    def request(self, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        body = {'SecretName': 'failure-'+failure, 'VersionId': 'other' if failure == 'wrong-version' else 'v1',
                'SecretString': '{}', 'RequestId': 'test'}
        if failure == 'denied':
            body = {'Error': {'Code': 'UnauthorizedOperation', 'Message': 'private-sentinel'}, 'RequestId': 'test'}
        response._content = json.dumps({'Response': body}).encode()
        return response
    monkeypatch.setattr(requests.Session, 'request', request)
    def forbid(*a, **k): raise AssertionError('Keychain fallback')
    monkeypatch.setattr('subprocess.run', forbid)
    caplog.set_level('DEBUG')
    with pytest.raises(KeychainError):
        try:
            load_keychain_secret('signing-private-key')
        except KeychainError:
            assert 'private-sentinel' not in traceback.format_exc()
            raise
    assert 'private-sentinel' not in caplog.text
