"""Affected local policy guards only: no real key files, requests, or database."""
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
import ast
import importlib.util
import io
import json
import socket
import sys
import unittest

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('limit_policy_capture',ROOT/'capture.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)


class LimitPolicyTests(unittest.TestCase):
    def test_preview_reports_design_and_cancelled_user_limits_without_auth(self):
        output=io.StringIO()
        with patch.object(sys,'argv',['capture.py','preview']), \
                patch.object(entry,'frozen',return_value=('release',[],None,None,None)), \
                patch.object(entry,'private_credential',side_effect=AssertionError('key access forbidden')), \
                patch.object(entry,'Ledger',side_effect=AssertionError('state forbidden')), \
                patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),redirect_stdout(output):
            entry.main()
        value=json.loads(output.getvalue())
        self.assertEqual(value['evaluation_case_count'],12)
        self.assertEqual(value['case_ids'],list(entry.ORDER))
        for key in ('user_request_cap','user_cny_cap','codex_week_stop_percent'):self.assertIsNone(value[key])
        self.assertIs(value['execution_authorized'],False)
        self.assertEqual(value['real_calls'],0)
        self.assertNotIn('max_attempts',value)

    def test_removed_quota_and_unpublished_automatic_commands_are_rejected(self):
        argvs=[['capture.py','preview','--quota','/dummy'],['capture.py','execute','--automatic'],
               ['capture.py','credential-ready']]
        for argv in argvs:
            with self.subTest(argv=argv),patch.object(sys,'argv',argv), \
                    patch.object(entry,'frozen',side_effect=AssertionError('rejected args must not preflight')), \
                    patch.object(entry,'private_credential',side_effect=AssertionError('key access forbidden')), \
                    redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):entry.main()

    def test_manual_main_requires_only_new_review_grant_price(self):
        argv=['capture.py','execute','--code-review','/dummy-review','--grant','/dummy-grant',
              '--price','/dummy-price','--credentials-file','/dummy-private-file']
        def capture(*args,**kw):
            self.assertNotIn('cap',kw)
            self.assertTrue(callable(kw['credential_reader']))
            kw['pre_send_check']()
            return {'status':'mock_body_not_real_execute','real_calls':0}
        with patch.object(sys,'argv',argv),patch.object(entry,'frozen',return_value=('release',[],None,None,None)), \
                patch.object(entry,'authorization',return_value='grantsha') as auth, \
                patch.object(entry,'capture_next',side_effect=capture), \
                patch.object(entry,'private_credential',side_effect=AssertionError('mock never reads key')), \
                redirect_stdout(io.StringIO()):
            entry.main()
        self.assertEqual(auth.call_count,2)
        auth.assert_called_with('release','/dummy-review','/dummy-grant','/dummy-price')


if __name__=='__main__':unittest.main(verbosity=2)
