"""Run with python3 test_oc_pool.py; no server or network is used."""

import json
import urllib.error
from unittest.mock import patch

import oc_pool as pool

SECRET = 'private prompt, URL, headers and provider message'
POOL = {'password': SECRET, 'servers': [{'port': 1, 'pid': 1}]}
SUCCESS = {'info': {'tokens': {'input': 7}, 'cost': 0.1},
           'parts': [{'type': 'text', 'text': 'answer'}]}


def call(replies, meta=None):
    replies = iter(replies)

    def request(_pool, _port, method, path, *args, **kwargs):
        if path == '/session':
            return {'id': 'test'}
        if path.endswith('/message'):
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value
        return None

    meta = {} if meta is None else meta
    with patch.object(pool, '_load', return_value=POOL), \
            patch.object(pool, '_healthy', return_value=True), \
            patch.object(pool, '_req', side_effect=request):
        result = pool.generate('provider/model', SECRET, meta=meta)
    report = meta['generation']
    assert report['seconds'] >= 0
    assert len(report['failures']) <= 2
    assert SECRET not in json.dumps(report)
    return result, meta


def test_no_pool():
    for state in (None, {'servers': []}):
        meta = {}
        with patch.object(pool, '_load', return_value=state):
            assert pool.generate('provider/model', SECRET, meta=meta) is None
        assert meta['generation']['attempts'] == 0
        assert meta['generation']['failures'][0]['category'] == 'no_pool'


def test_unhealthy_pool_exhausts_two_attempts():
    meta = {}
    with patch.object(pool, '_load', return_value=POOL), \
            patch.object(pool, '_healthy', return_value=False), \
            patch.object(pool, '_revive', return_value=False):
        assert pool.generate('provider/model', SECRET, meta=meta) is None
    assert meta['generation']['attempts'] == 2
    assert [e['category'] for e in meta['generation']['failures']] == ['unhealthy_pool'] * 2


def test_wrapped_socket_timeout():
    result, meta = call([urllib.error.URLError(TimeoutError(SECRET)), SUCCESS])
    assert result == 'answer'
    assert meta['generation']['failures'][0]['category'] == 'timeout'


def test_success_resets_telemetry_and_preserves_usage():
    result, meta = call([SUCCESS], {'tokens': {'input': 3}, 'generation': {'old': True}})
    assert result == 'answer'
    assert meta['tokens']['input'] == 10
    assert meta['generation']['ok'] and meta['generation']['attempts'] == 1
    assert meta['generation']['failures'] == []


def test_provider_error_keeps_usage_and_does_not_retry():
    result, meta = call([{'info': {'error': {'name': SECRET,
                          'data': {'statusCode': 429, 'message': SECRET}},
                          'tokens': {'input': 5}, 'cost': 0.2}, 'parts': []}])
    assert result is None and meta['tokens']['input'] == 5 and meta['cost'] == 0.2
    error = meta['generation']['failures'][0]
    assert error['category'] == 'provider_error' and error['status'] == 429
    assert meta['generation']['attempts'] == 1


def test_http_retry_is_visible_after_success():
    error = urllib.error.HTTPError(SECRET, 429, SECRET, {'secret': SECRET}, None)
    result, meta = call([error, SUCCESS])
    assert result == 'answer' and meta['generation']['attempts'] == 2
    assert meta['generation']['failures'][0]['category'] == 'http_error'
    assert meta['generation']['failures'][0]['status'] == 429


def test_timeout_and_transport_failures_are_bounded():
    result, meta = call([TimeoutError(SECRET), urllib.error.URLError(SECRET)])
    assert result is None and not meta['generation']['ok']
    assert [e['category'] for e in meta['generation']['failures']] == ['timeout', 'transport_error']


def test_invalid_status_is_not_copied():
    result, meta = call([{'info': {'error': {'data': {'statusCode': SECRET}}}, 'parts': []}])
    assert result is None and meta['generation']['failures'][0]['status'] is None


def test_empty_and_invalid_responses():
    result, meta = call([{'info': {}, 'parts': []}])
    assert result == '' and meta['generation']['failures'][0]['category'] == 'empty_response'
    result, meta = call([{'private': SECRET}, {'private': SECRET}])
    assert result is None
    assert [e['category'] for e in meta['generation']['failures']] == ['invalid_response'] * 2


if __name__ == '__main__':
    tests = [value for name, value in globals().copy().items() if name.startswith('test_')]
    for test in tests:
        test()
    print(f'{len(tests)} checks passed')
