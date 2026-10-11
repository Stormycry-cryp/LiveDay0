"""Directed runner/ledger checks. Artificial review PASS is never quality evidence."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fixed_capture', ROOT / 'capture.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release, cls.cases, cls.c, cls.glm, cls.local = entry.frozen()
        package = sys.modules['liveday0']
        cls.local_adapter = __import__('liveday0.local_extraction', fromlist=['DeterministicLocalAdapter'])
        cls.evidence = Path(tempfile.mkdtemp(prefix='ld0-fixed-capture-offline-'))
        cls.connect, cls.resolve = socket.socket.connect, socket.getaddrinfo
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                assert sock.family == socket.AF_INET and address[0] == '127.0.0.1'
            return cls.connect(sock, address)
        def resolve(host, *args, **kwargs):
            assert host == '127.0.0.1', 'external DNS forbidden'
            return cls.resolve(host, *args, **kwargs)
        socket.socket.connect, socket.getaddrinfo = connect, resolve

    @classmethod
    def tearDownClass(cls):
        socket.socket.connect, socket.getaddrinfo = cls.connect, cls.resolve
        print(json.dumps({'evidence_root':str(cls.evidence),'real_model_calls':0,'credentials_read':False,'database':'not_loaded'}))

    def setUp(self):
        self.root = self.evidence / self._testMethodName
        self.root.mkdir(mode=0o700)
        self.state = self.root / 'state'
        self.posts = []
        owner = self
        self.mode = 'ok'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                assert self.path == '/api/paas/v4/chat/completions'
                assert 'Authorization' not in self.headers
                payload = json.loads(body)
                owner.posts.append(payload)
                if owner.mode == 'timeout': time.sleep(0.2)
                content = json.loads(payload['messages'][1]['content'])['content']
                raw = json.dumps(owner.response(content), ensure_ascii=False).encode()
                if owner.mode == 'schema': raw = b'{"PRIVATE_PROVIDER_DETAIL":true}'
                if owner.mode == 'large': raw = b' ' * 64001
                if owner.mode == 'usage':
                    value=owner.response(content); value['usage']['total_tokens']=999
                    raw=json.dumps(value).encode()
                try:
                    self.send_response(500 if owner.mode == 'http' else 200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    @classmethod
    def response(cls, text):
        # Reuse the existing pure fixture shape; arbitrary legal event body is
        # not an oracle or an assertion that this text was understood correctly.
        example=next(iter(cls.local_adapter.SYNTHETIC_EXAMPLES))
        request=cls.c.ExtractionRequest.build('fixture-shape','1',example,'2026-10-09T00:00:00+00:00')
        p=cls.local_adapter.DeterministicLocalAdapter().extract(request).to_dict()['proposals'][0]
        for key in ('schema_version','proposal_id','source','producer'): p.pop(key)
        p.update(span_start=0,span_end=len(text),revision_intent=p.pop('revision')['intent'])
        semantic=p['semantic_input'];semantic.pop('canonical_key');semantic.pop('valid_at')
        for key in cls.c._BODY_OPTIONAL['event']: semantic['body'].setdefault(key,None)
        return {'model':entry.MODEL,'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150},
                'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'proposals':[p]},ensure_ascii=False),
                'reasoning_content':'PRIVATE_PROVIDER_DETAIL'}}], 'unrecognized':'PRIVATE_PROVIDER_DETAIL'}

    def run_next(self, report=None, review=None, timeout=1):
        return entry.capture_next(self.state,self.release,self.cases,self.c,self.glm,self.local,
            grant_sha='fixture_not_authorization',fixture_port=self.server.server_port,
            report=report,review=review,timeout=timeout)

    def ledger(self):
        return entry.Ledger(self.state,self.release,'fixture_not_authorization',True)

    def review(self, split='development', verdict='pass', forge=False):
        with_context=self.ledger()
        try: rows=with_context.captures(split)
        finally: with_context.close()
        folder=self.root / ('review-'+str(len(list(self.root.glob('review-*')))))
        folder.mkdir()
        captures=folder/'captures.jsonl'
        captures.write_bytes(b''.join((json.dumps(x,ensure_ascii=False)+'\n').encode() for x in rows))
        report,template=folder/'report.json',folder/'review.jsonl'
        for args in (['ingest','--captures',str(captures),'--out',str(report)],
                     ['template','--report',str(report),'--out',str(template)]):
            subprocess.run([sys.executable,'-I','-S','-B',str(ROOT/'prepare.py'),*args,'--split',split],
                env={},check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        judgments=[]
        for line in template.read_text().splitlines():
            row=json.loads(line)
            if row['case_id'] in {x['case_id'] for x in rows}:
                row['provenance_verified']=True
                for group in ('required','forbidden'):
                    for key in row[group]:
                        row[group][key]='pass' if group=='required' else verdict
                        row['notes'][key]='Artificial fixture operator judgment; NOT semantic evidence.'
            judgments.append(row)
        template.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in judgments))
        if forge:
            data=json.loads(report.read_text()); data['origin']='forged report'
            report.write_text(json.dumps(data))
        return report,template

    def assert_stopped(self):
        self.assertTrue((self.state/'STOP.json').is_file())
        before=len(self.posts)
        with self.assertRaises(entry.Stop): self.run_next()
        self.assertEqual(len(self.posts),before)

    def test_full12_gates_and_bound(self):
        report=review=None
        for i in range(12):
            result=self.run_next(report,review)
            self.assertEqual(result['case_id'],entry.ORDER[i])
            self.assertEqual(result['attempts'],i+1)
            report,review=self.review('development' if i<6 else 'heldout')
        ledger=self.ledger()
        try: entry.human_gate(ledger,'heldout',report,review)
        finally: ledger.close()
        self.assertEqual(len(self.posts),12)
        self.assertEqual(result['estimated_cny'],'14.4703488')
        with self.assertRaises(entry.Stop): self.run_next(report,review)
        self.assertEqual(len(self.posts),12)
        for p in self.state.rglob('*'):
            self.assertEqual(p.stat().st_mode & 0o077,0)
        saved=b''.join(p.read_bytes() for p in self.state.rglob('capture.json'))
        self.assertNotIn(b'PRIVATE_PROVIDER_DETAIL',saved)
        self.assertTrue(all(not (p/'accepted.json').is_file() or json.loads((p/'accepted.json').read_text())['fixture'] for p in self.state.glob('gate-*')))

    def test_missing_review_no_second_request(self):
        self.run_next()
        with self.assertRaisesRegex(entry.Stop,'previous_case_human_review_required'): self.run_next()
        self.assertEqual(len(self.posts),1)
        self.assertEqual(len(list(self.state.glob('reserved-*'))),1)

    def test_unjudged_wait_then_completed_review(self):
        self.run_next(); report,review=self.review(verdict='unjudged')
        with self.assertRaisesRegex(entry.Stop,'human_review_incomplete_waiting'): self.run_next(report,review)
        self.assertFalse((self.state/'STOP.json').exists())
        report,review=self.review()
        self.run_next(report,review)
        self.assertEqual(len(self.posts),2)

    def test_forbidden_failure_stops(self):
        self.run_next(); report,review=self.review(verdict='fail')
        with self.assertRaisesRegex(entry.Stop,'human_semantic_failure'): self.run_next(report,review)
        self.assertEqual(len(self.posts),1); self.assert_stopped()

    def test_report_forgery_stops(self):
        self.run_next(); report,review=self.review(forge=True)
        with self.assertRaisesRegex(entry.Stop,'capture_report_not_derived'): self.run_next(report,review)
        self.assertEqual(len(self.posts),1); self.assert_stopped()

    def test_http_failure_consumes(self):
        self.mode='http'
        with self.assertRaises(Exception): self.run_next()
        self.assertEqual(len(self.posts),1)
        self.assertEqual(len(list(self.state.glob('reserved-*'))),1)
        self.assertFalse((self.state/'attempt-01/capture.json').exists()); self.assert_stopped()

    def test_schema_failure_consumes(self):
        self.mode='schema'
        with self.assertRaises(Exception): self.run_next()
        self.assertEqual(len(self.posts),1); self.assert_stopped()
        self.assertNotIn(b'PRIVATE_PROVIDER_DETAIL',b''.join(p.read_bytes() for p in self.state.rglob('*.json')))

    def test_usage_failure_consumes(self):
        self.mode='usage'
        with self.assertRaises(Exception): self.run_next()
        self.assertEqual(len(self.posts),1); self.assert_stopped()

    def test_response_bound_consumes(self):
        self.mode='large'
        with self.assertRaises(Exception): self.run_next()
        self.assertEqual(len(self.posts),1); self.assert_stopped()

    def test_timeout_consumes(self):
        self.mode='timeout'
        with self.assertRaises(Exception): self.run_next(timeout=0.05)
        self.assertEqual(len(self.posts),1); self.assert_stopped()

    def test_reserved_without_result_unknown(self):
        ledger=self.ledger()
        try: ledger.claim(self.cases[0],'a'*64)
        finally: ledger.close()
        with self.assertRaisesRegex(entry.Stop,'interrupted_or_unknown'): self.run_next()
        self.assertEqual(len(self.posts),0); self.assert_stopped()

    def test_lock_rejects_parallel(self):
        ledger=self.ledger()
        try:
            with self.assertRaises(BlockingIOError): self.ledger()
            self.assertFalse((self.state/'STOP.json').exists())
        finally: ledger.close()
        self.assertEqual(len(self.posts),0)

    def test_state_loss_cannot_reset(self):
        ledger=self.ledger();ledger.close()
        self.state.rename(self.root/'preserved-state')
        with self.assertRaisesRegex(entry.Stop,'deleted_state_not_a_new_run'): self.run_next()
        self.assertFalse(self.state.exists());self.assertEqual(len(self.posts),0)

    def test_attempt_loss_cannot_reset(self):
        self.run_next()
        (self.state/'attempt-01').rename(self.root/'preserved-attempt')
        with self.assertRaisesRegex(entry.Stop,'ledger_unverifiable'): self.run_next()
        self.assertEqual(len(self.posts),1);self.assert_stopped()

    def test_capture_tamper_stops(self):
        self.run_next();p=self.state/'attempt-01/capture.json';p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaisesRegex(entry.Stop,'capture_changed'): self.run_next()
        self.assertEqual(len(self.posts),1);self.assert_stopped()

    def test_scope_no_new_process_reset(self):
        self.run_next()
        with self.assertRaisesRegex(entry.Stop,'state_scope_mismatch'):
            entry.Ledger(self.state,self.release,'different_grant',True)
        self.assertEqual(len(self.posts),1);self.assert_stopped()

    def test_authorization_artifacts_no_transport(self):
        now=datetime.now(timezone.utc)
        scope={'capture_manifest_sha256':self.release,'model':entry.MODEL,'endpoint':entry.ENDPOINT}
        docs=[{**scope,'scope':'code_review_only'},
              {**scope,'scope':'new_fictional_evaluation_only','case_ids':list(entry.ORDER),
               'retain_fictional_captures':True,'expires_utc':(now+timedelta(hours=1)).isoformat()},
              {**scope,'source':'https://docs.bigmodel.cn/cn/guide/start/pricing',
               'input_cny_per_million':'0.8','output_cny_per_million':'2.8','cache_storage_free':True,
               'operator_verified':True,'read_utc':now.isoformat()}]
        paths=[self.root/(str(i)+'.json') for i in range(3)]
        def write():
            for p,d in zip(paths,docs):p.write_bytes(entry.encode(d))
        write();self.assertEqual(entry.authorization(self.release,*paths),entry.sha(paths[1].read_bytes()))
        for i,key,value in [(0,'scope','execution'),(1,'expires_utc',(now-timedelta(seconds=1)).isoformat()),
                (1,'case_ids',True),(1,'case_ids',list(reversed(entry.ORDER))),
                (1,'case_ids',list(entry.ORDER[:-1])),(2,'output_cny_per_million','3'),
                (2,'operator_verified',False),(2,'capture_manifest_sha256','wrong'),
                (2,'read_utc',(now-timedelta(days=2)).isoformat())]:
            old=docs[i][key];docs[i][key]=value;write()
            with self.assertRaises(entry.Stop):entry.authorization(self.release,*paths)
            docs[i][key]=old;write()
        for key,value in [('max_cny','15'),('max_attempts',12),('execution_mode','agent_file_launch')]:
            docs[1][key]=value;write()
            with self.assertRaises(entry.Stop):entry.authorization(self.release,*paths)
            del docs[1][key];write()
        self.assertEqual(len(self.posts),0);self.assertFalse(self.state.exists())

    def test_stop_during_preclaim_wait(self):
        ledger=self.ledger()
        try:
            entry.exclusive(self.state/'STOP.json',{'code':'human_stop','retry_authorized':False})
            with self.assertRaisesRegex(entry.Stop,'run_permanently_stopped'):
                ledger.claim(self.cases[0],'a'*64)
        finally: ledger.close()
        self.assertEqual(len(self.posts),0)
        self.assertEqual(len(list(self.state.glob('reserved-*'))),0)
        self.assert_stopped()

    def test_preflight_seal_no_automatic_resume(self):
        self.run_next()
        entry.seal_existing_real_state(self.state)  # Pure helper, fixture path; no execute/getpass.
        self.assert_stopped();self.assertEqual(len(self.posts),1)

    def test_fee_accounting_has_no_user_ceiling(self):
        self.run_next()
        report,review=self.review()
        self.run_next(report,review)
        self.assertEqual(len(self.posts),2)
        self.assertEqual(len(list(self.state.glob('reserved-*'))),2)
        plan=entry.read(self.state/'plan.json')[1]
        self.assertNotIn('max_cny',plan);self.assertNotIn('max_attempts',plan)
        self.assertEqual(plan['case_ids'],list(entry.ORDER))
        self.assertFalse((self.state/'STOP.json').exists())
        one=entry.call_accounting(self.glm)
        self.assertFalse(hasattr(one,'_limit'))
        one.reserve()
        with self.assertRaisesRegex(self.glm.ProviderFailure,'duplicate_call'):one.reserve()
        one.finish('validated',{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150})
        self.assertEqual(one.snapshot()['calls'],1)
        self.assertEqual(one.snapshot()['estimated_cny'],str(entry.RESERVE))
        self.assertNotIn('reserved_cny',one.snapshot())
        two=entry.call_accounting(self.glm);two.reserve()
        with self.assertRaisesRegex(self.glm.ProviderFailure,'usage_outside_preflight'):
            two.finish('validated',{'prompt_tokens':100,'completion_tokens':50,'total_tokens':999})
        self.assertEqual(two.snapshot()['entry']['status'],'usage_outside_preflight')


if __name__=='__main__':unittest.main(verbosity=2)
