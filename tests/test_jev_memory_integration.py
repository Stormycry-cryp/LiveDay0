"""Synthetic HTTP -> real judgment parser -> PostgreSQL -> selected recall."""
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from uuid import uuid4

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.extraction_contract import ExtractionRequest
from liveday0.jev import JevHTTP, MODEL, PATH
from liveday0.judgments import MemoryJudgments
from liveday0.local_extraction import DeterministicLocalAdapter


@contextmanager
def scripted_judgment_server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, dict(self.headers), payload))
            answers = {}
            for name, question in payload['questions'].items():
                if question['type'] == 'choice':
                    choices = {'disposition': 'keep', 'speech_mode': 'direct_statement',
                               'subject': 'user', 'persistence': 'durable', 'category': 'event',
                               'source_support': 'supported',
                               'event_assignment': 'candidate_0' if payload['state'].get('candidates') else 'new_event'}
                    chosen = choices[name]
                    answers[name] = {'type': 'choice', 'choice': chosen, 'confidence': 1,
                                     'probabilities': {option: int(option == chosen) for option in question['criteria']}}
                else:
                    answers[name] = {'type': 'score', 'score': 3, 'confidence': 1,
                                     'legend': {str(i): value for i, value in enumerate(question['criteria'])},
                                     'probabilities': {str(i): int(i == 3) for i in range(4)}}
            body = json.dumps({'id': 'task_synthetic_integration', 'model': MODEL, 'answers': answers,
                               'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_capture_append_replay_and_recall_over_http(service):
    adapter = DeterministicLocalAdapter()
    first = ExtractionRequest.build(str(uuid4()), '1', '合成生活：周末搬家，等待朋友来帮忙。', datetime.now(timezone.utc))
    second = ExtractionRequest.build(str(uuid4()), '1', '合成生活：朋友已来帮忙，搬家完成。', datetime.now(timezone.utc))
    authorized_payloads = []

    def allow_synthetic_fixture(payload):
        authorized_payloads.append(payload)
        return True

    with scripted_judgment_server() as (port, seen):
        judgments = MemoryJudgments(JevHTTP(fixture_port=port, authorize_send=allow_synthetic_fixture, timeout=2))
        configured = MemoryService(service.tenant_id, judgments=judgments)
        created = configured.capture(first, extractor=adapter)['outcomes'][0]
        appended = configured.capture(second, extractor=adapter)['outcomes'][0]
        assert created['status'] == appended['status'] == 'stored'
        assert created['route'] == 'new_event' and appended['route'] == 'existing_event'
        assert appended['event_id'] == created['card_ids'][0]
        assert len(seen) == 2
        assert all(len(payload['questions']) == 7 for _, _, payload in seen)
        replay = configured.capture(second, extractor=adapter)['outcomes'][0]
        assert replay['status'] == 'duplicate' and len(seen) == 2
        context = configured.recall('朋友帮忙搬家进展')
        assert len(seen) == 3
        recalled = context['layers']['events'][0]
        assert recalled['id'] == str(appended['event_id'])
        assert recalled['body']['current_result'] == '搬家完成'

    assert [payload for _, _, payload in seen] == authorized_payloads
    assert all(path == PATH and 'Authorization' not in headers for path, headers, _ in seen)
    assert list(seen[1][2]['state']['candidates']) == ['candidate_0']
    assert 'id' not in seen[1][2]['state']['candidates']['candidate_0']
    assert list(seen[-1][2]['questions']) == ['candidate_0']
    with tenant_transaction(service.tenant_id, mode='read') as conn:
        assert conn.execute('SELECT count(*) AS n FROM semantic_cards').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM evidence').fetchone()['n'] == 2
        assert conn.execute('SELECT count(*) AS n FROM event_deltas').fetchone()['n'] == 1
