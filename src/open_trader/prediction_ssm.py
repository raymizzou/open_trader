"""Read a pinned SSM bundle using CVM metadata, never permanent cloud keys."""
from functools import lru_cache
import http.client
import json
import logging
import os
import re
import time


class SSMError(RuntimeError):
    def __init__(self):
        super().__init__('ssm_credentials_unavailable')


def load_ssm_secret(service: str, account: str) -> str:
    try:
        refs = tuple(os.environ[f'OPEN_TRADER_SSM_{key}'] for key in (
            'REGION', 'SECRET', 'VERSION', 'ROLE'))
        if any(not re.fullmatch(r'[A-Za-z0-9_./-]{1,128}', ref) for ref in refs):
            raise ValueError
        if refs[2] == 'SSM_Current':
            raise ValueError  # Require an immutable operator-selected version.
        value = _bundle(*refs)[service][account]
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        return value
    except Exception:
        # Neither cloud responses nor exception chains may contain credentials.
        raise SSMError() from None


@lru_cache(maxsize=1)
def _bundle(region: str, name: str, version: str, role: str) -> dict:
    from tencentcloud.common.credential import Credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.ssm.v20190923 import models, ssm_client

    # SDK debug logging includes signed request headers and response bodies.
    logging.getLogger('tencentcloud_sdk_common').disabled = True
    # The SDK's role helper has unbounded metadata IO. Use a bounded, direct
    # request (no environment proxy or redirects), then let the SDK sign SSM.
    connection = http.client.HTTPConnection('metadata.tencentyun.com', timeout=3)
    try:
        connection.request('GET', '/latest/meta-data/cam/security-credentials/' + role)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        token = json.loads(raw)
    finally:
        connection.close()
    if token.get('Code') != 'Success' or token.get('ExpiredTime', 0) <= time.time() + 30:
        raise ValueError
    for field in ('TmpSecretId', 'TmpSecretKey', 'Token'):
        if not isinstance(token.get(field), str) or not token[field].strip():
            raise ValueError
    credential = Credential(token['TmpSecretId'], token['TmpSecretKey'], token['Token'])
    profile = ClientProfile()
    profile.httpProfile = HttpProfile(reqTimeout=5)
    client = ssm_client.SsmClient(credential, region, profile)
    request = models.GetSecretValueRequest()
    request.SecretName, request.VersionId = name, version
    result = client.GetSecretValue(request)
    if result.SecretName != name or result.VersionId != version or not result.SecretString:
        raise ValueError
    bundle = json.loads(result.SecretString)
    if not isinstance(bundle, dict):
        raise ValueError
    return bundle
