"""显式授权的 BeatAPI Jev 免费模型接口；不读取凭据、不重试或切换模型。"""
from __future__ import annotations

from copy import deepcopy
import http.client
import json
import math
import socket
from threading import Event, Timer


MODEL = 'jev-1.14-free'
HOST = 'api.beatapi.io'
PATH = '/v1/systemone'
MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 262_144
MAX_QUESTIONS = 64
MAX_RESPONSE_ID_LENGTH = 256


class JudgmentFailure(ValueError):
    """只包含固定原因码，不包含来源、服务正文或凭据。"""


def _number(value, low=0, high=1):
    # 先检查区间，避免极大 Python 整数在转成浮点数时触发 OverflowError。
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise JudgmentFailure('invalid_number')
    return value


def _json_bytes(value, maximum=MAX_REQUEST_BYTES):
    # 限制深度，并拒绝隐式转换键名、非 JSON 对象和非有限数值。
    def walk(item, depth):
        if depth > 32:
            raise JudgmentFailure('invalid_json_depth')
        if item is None or type(item) in (str, bool, int, float):
            if type(item) in (int, float):
                try:
                    finite = math.isfinite(item)
                except OverflowError:
                    finite = False
                if not finite:
                    raise JudgmentFailure('invalid_number')
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                walk(child, depth + 1)
            return
        raise JudgmentFailure('invalid_json_value')
    try:
        walk(value, 0)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise JudgmentFailure('invalid_json_value') from None
    if len(raw) > maximum:
        raise JudgmentFailure('request_too_large')
    return raw


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise JudgmentFailure('invalid_response_schema')


def validate_questions(questions):
    if type(questions) is not dict or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise JudgmentFailure('invalid_questions')
    for name, question in questions.items():
        if type(name) is not str or not 1 <= len(name) <= 128:
            raise JudgmentFailure('invalid_questions')
        _keys(question, {'type', 'instructions', 'criteria'})
        if type(question['instructions']) not in (str, dict, list) or not question['instructions']:
            raise JudgmentFailure('invalid_questions')
        criteria = question['criteria']
        if question['type'] == 'choice':
            if (type(criteria) is not dict or not 2 <= len(criteria) <= 255
                    or any(type(k) is not str or not k or len(k) > 128 for k in criteria)):
                raise JudgmentFailure('invalid_questions')
            if any(v is not None and type(v) not in (str, dict, list) for v in criteria.values()):
                raise JudgmentFailure('invalid_questions')
        elif question['type'] == 'score':
            if (type(criteria) is not list or not 2 <= len(criteria) <= 10
                    or any(type(v) is not str or not v for v in criteria)):
                raise JudgmentFailure('invalid_questions')
        else:
            raise JudgmentFailure('invalid_questions')


def validate_response(response, questions):
    """校验答案与调用方构造的问题逐一绑定；不能靠类型正确证明语义正确。"""
    _json_bytes(response, MAX_RESPONSE_BYTES)
    _keys(response, {'id', 'model', 'answers', 'usage'})
    if (type(response['id']) is not str or not response['id'].strip()
            or len(response['id']) > MAX_RESPONSE_ID_LENGTH):
        raise JudgmentFailure('invalid_response_id')
    if response['model'] != MODEL:
        raise JudgmentFailure('model_mismatch')
    _keys(response['usage'], {'input_tokens', 'output_tokens'})
    if any(type(v) is not int or not 0 <= v <= 10_000_000 for v in response['usage'].values()):
        raise JudgmentFailure('invalid_usage')
    _keys(response['answers'], questions)
    for name, question in questions.items():
        answer = response['answers'][name]
        kind = question['type']
        fields = {'type', 'probabilities', 'confidence'} | ({'choice'} if kind == 'choice' else {'score', 'legend'})
        _keys(answer, fields)
        if answer['type'] != kind:
            raise JudgmentFailure('answer_type_mismatch')
        _number(answer['confidence'])
        options = list(question['criteria']) if kind == 'choice' else [str(i) for i in range(len(question['criteria']))]
        probabilities = answer['probabilities']
        _keys(probabilities, options)
        for probability in probabilities.values():
            _number(probability)
        # Live BeatAPI responses round probabilities and scores to two decimals.
        # Bound that quantization error rather than requiring exact float sums;
        # retain the original values and reject larger inconsistencies.
        sum_tolerance = min(0.05, len(options) * 0.005) + 1e-6
        if not math.isclose(math.fsum(probabilities.values()), 1, rel_tol=0, abs_tol=sum_tolerance):
            raise JudgmentFailure('invalid_probability_sum')
        if kind == 'choice':
            choice = answer['choice']
            if type(choice) is not str or choice not in probabilities:
                raise JudgmentFailure('unknown_choice')
            if probabilities[choice] < max(probabilities.values()):
                raise JudgmentFailure('choice_not_maximum')
        else:
            expected_legend = {str(i): text for i, text in enumerate(question['criteria'])}
            _keys(answer['legend'], expected_legend)
            if answer['legend'] != expected_legend:
                raise JudgmentFailure('score_legend_mismatch')
            score = _number(answer['score'], 0, len(options) - 1)
            expected_score = math.fsum(i * probabilities[str(i)] for i in range(len(options)))
            score_tolerance = 0.005 * (1 + sum(range(len(options)))) + 1e-6
            if not math.isclose(score, expected_score, rel_tol=0, abs_tol=score_tolerance):
                raise JudgmentFailure('score_distribution_mismatch')
    return deepcopy(response)


def _strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise JudgmentFailure('duplicate_json_key')
            result[key] = value
        return result
    def invalid(_):
        raise JudgmentFailure('invalid_json')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise JudgmentFailure('invalid_json') from None


class JevHTTP:
    """固定 BeatAPI HTTPS 和免费模型；fixture_port 仅连接无凭据的 127.0.0.1。

    authorize_send 接收脱离原对象的完整请求字典，并须返回精确的 True。
    它只批准本次发送，不代表保存授权。已连接 I/O 有总期限；系统 DNS
    解析仍受操作系统控制。本接口不跟随跳转、不使用代理、不自动重试。
    """
    production_ready = False

    def __init__(self, *, api_key=None, authorize_send=None, fixture_port=None, timeout=15.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be in (0, 30] seconds')
        if fixture_port is not None:
            if type(fixture_port) is not int or not 1 <= fixture_port <= 65535 or api_key is not None:
                raise ValueError('loopback requires a valid port and no credential')
        elif (type(api_key) is not str or not 1 <= len(api_key) <= 512
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ValueError('an explicit API credential is required')
        if authorize_send is not None and not callable(authorize_send):
            raise ValueError('authorize_send must be callable')
        self.fixture = fixture_port is not None
        self._port, self._key, self._timeout = fixture_port, api_key, timeout
        self._authorize = authorize_send

    def evaluate(self, state, questions):
        payload = {'state': state, 'model': MODEL, 'questions': questions}
        # 先冻结并检查尺寸，再请求授权；回调不能改变实际发送的内容。
        raw = _json_bytes(payload)
        frozen = _strict_json(raw)
        validate_questions(frozen['questions'])
        if type(frozen['state']) not in (str, dict, list):
            raise JudgmentFailure('invalid_state')
        try:
            approved = self._authorize(deepcopy(frozen)) if self._authorize else None
        except Exception:
            raise JudgmentFailure('outbound_approval_failed') from None
        if approved is not True:
            raise JudgmentFailure('outbound_approval_required')
        return validate_response(self._post(raw), frozen['questions'])

    def _post(self, body):
        connection, timer = None, None
        expired = Event()
        try:
            connection = (http.client.HTTPConnection('127.0.0.1', self._port, timeout=self._timeout)
                          if self.fixture else http.client.HTTPSConnection(HOST, timeout=self._timeout))
            connection.connect()
            connected_socket = connection.sock
            def abort():
                expired.set()
                try:
                    connected_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = Timer(self._timeout, abort)
            timer.daemon = True
            timer.start()
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
            if not self.fixture:
                headers['Authorization'] = 'Bearer ' + self._key
            connection.request('POST', PATH, body, headers)
            response = connection.getresponse()
            if response.status != 200:
                raise JudgmentFailure('http_error')
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise JudgmentFailure('invalid_content_type')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise JudgmentFailure('invalid_content_encoding')
            length = response.getheader('Content-Length')
            if length is not None:
                if not length.isascii() or not length.isdigit():
                    raise JudgmentFailure('invalid_content_length')
                if len(length) > 9 or int(length) > MAX_RESPONSE_BYTES:
                    raise JudgmentFailure('response_too_large')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if expired.is_set():
                raise JudgmentFailure('timeout')
            if len(raw) > MAX_RESPONSE_BYTES:
                raise JudgmentFailure('response_too_large')
            if length is not None and len(raw) != int(length):
                raise JudgmentFailure('invalid_content_length')
            return _strict_json(raw)
        except (OSError, http.client.HTTPException) as exc:
            raise JudgmentFailure('timeout' if expired.is_set() or isinstance(exc, TimeoutError)
                                  else 'transport_error') from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if connection is not None:
                connection.close()
