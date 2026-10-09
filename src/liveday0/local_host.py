"""Trusted local OS session for the synthetic prototype, not an HTTP API.

The console is the human command channel. The adapter only returns data and
never receives the host/service or permission to dispatch human commands.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
import pwd
import socket
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, NotFound
from liveday0.extraction_contract import ExtractionRequest
from liveday0.local_extraction import (DeterministicLocalAdapter, saved_request,
    source_material, validate_and_gate)
from liveday0.serialization import canonical_json, fingerprint


@dataclass(frozen=True)
class LocalApproval:
    action: str
    canonical_request: str = field(repr=False)

    @property
    def payload(self):
        return json.loads(self.canonical_request)

    @property
    def fingerprint(self):
        return fingerprint(self.canonical_request)


def local_tenant_identity():
    """Use the real OS UID, not USER/HOME environment variables or request JSON.

    This is local-machine scope, not portable server authentication. A host rename
    changes the namespace. No credential or user mapping file is created.
    """
    uid=os.getuid()
    if uid != os.geteuid():
        raise AuthorizationRequired('setuid sessions are unsupported')
    pwd.getpwuid(uid)  # Require an existing logged-in OS principal.
    return uuid5(NAMESPACE_URL,canonical_json(['liveday0:synthetic-local-user:v1',socket.gethostname(),uid]))


class LocalMemorySession:
    synthetic = True

    def __init__(self, *, adapter=None, confirm=None):
        self.tenant_id=local_tenant_identity()
        self._adapter=adapter or DeterministicLocalAdapter()
        self._confirm=confirm
        self._approved=set()
        self._read_grants=set()
        self._service=MemoryService(self.tenant_id,explicit_save_authorizer=self._explicit_check)
        self._service.ensure_tenant()

    def _approve(self, action, payload):
        approval=LocalApproval(action,canonical_json(payload))
        key=(action,approval.fingerprint)
        if key not in self._approved:
            if self._confirm is None or self._confirm(approval) is not True:
                raise AuthorizationRequired('local human confirmation is required for this exact action')
            self._approved.add(key)

    def _explicit_check(self, check):
        if check.tenant_id != self.tenant_id:
            return False
        if check.phase=='read':
            return (check.intent_id,check.payload['evidence_id']) in self._read_grants
        if check.phase=='commit':
            return ('restore-exact-output',check.fingerprint) in self._approved
        return False

    @staticmethod
    def _shape(command, required, optional=()):
        if not isinstance(command,dict) or set(command)-set(required)-set(optional) or set(required)-set(command):
            raise ValueError('unsupported local command fields; tenant, identity and authorization flags are not accepted')

    @staticmethod
    def _request(command):
        has_id='message_id' in command; has_time='occurred_at' in command
        if has_id != has_time:
            raise ValueError('a replay must preserve both message_id and occurred_at')
        message=str(UUID(command['message_id'])) if has_id else str(uuid4())
        when=command['occurred_at'] if has_time else datetime.now(timezone.utc)
        return ExtractionRequest.build(message,'1',command['text'],when,
            sensitivity=command.get('sensitivity','ordinary'),authority='user',source_kind='conversation')

    def handle(self, command):
        command=deepcopy(command)
        if len(canonical_json(command).encode('utf-8'))>64_000:
            raise ValueError('local command exceeds bounded input')
        if not isinstance(command,dict):
            raise ValueError('local command must be an object')
        action=command.get('action')
        if action in {'capture','correct'}:
            fields={'action','text'} | ({'card_id','expected_version'} if action=='correct' else set())
            self._shape(command,fields,{'message_id','occurred_at','sensitivity'})
            return self._capture_or_correct(command)
        if action=='query':
            self._shape(command,{'action','query'})
            if not isinstance(command['query'],str) or len(command['query'])>2000:
                raise ValueError('query must be bounded text')
            return {'synthetic':True,'context':self._service.recall(command['query'])}
        if action=='delete':
            self._shape(command,{'action','kind','id'})
            if command['kind'] not in {'card','evidence'}:
                raise ValueError('delete kind must be card or evidence')
            target=UUID(command['id'])
            prepared=self._service.read_deletion(command['kind'],target)
            self._approve('delete',prepared.payload)
            fn=self._service.delete_card if command['kind']=='card' else self._service.delete_evidence
            fn(target,expected_deletion=prepared)
            return {'synthetic':True,'deleted':str(target),'kind':command['kind']}
        if action in {'interpret','restore'}:
            self._shape(command,{'action','evidence_id'},{'intent_id'})
            return self._interpret(command)
        raise ValueError('supported actions: capture, query, correct, delete, interpret, restore')

    def _inspect(self, kind, target):
        with tenant_transaction(self.tenant_id,mode='read') as conn:
            if kind=='card':
                value=conn.execute("""SELECT c.id,c.card_type,c.lifecycle,c.current_version,v.body
                    FROM semantic_cards c JOIN semantic_card_versions v ON
                    v.tenant_id=c.tenant_id AND v.card_id=c.id AND v.version=c.current_version
                    WHERE c.tenant_id=%s AND c.id=%s""",(self.tenant_id,target)).fetchone()
            else:
                value=conn.execute('SELECT id,status,version,content,object_ref FROM evidence WHERE tenant_id=%s AND id=%s',
                    (self.tenant_id,target)).fetchone()
            if value is None: raise NotFound('target not found in the local user session')
        if len(canonical_json(value).encode('utf-8'))>64_000:
            raise ValueError('target exceeds the bounded local review; no truncation')
        return value

    def _capture_or_correct(self, command):
        request=self._request(command)
        decisions=validate_and_gate(request,self._adapter)
        accepted=[d for d in decisions if d.reason is None]
        if len(accepted)>1:
            raise ValueError('local entry supports one accepted span per message')
        result={'synthetic':True,'adapter_production_ready':False,'message_id':request.source.source_id,
            'occurred_at':request.occurred_at.isoformat(),'outcomes':[]}
        if not decisions:
            result['outcomes']=[{'status':'rejected','reason':'unsupported_synthetic_text'}]
            return result
        if command['action']=='correct':
            if len(decisions)!=1 or len(accepted)!=1:
                raise ValueError('correction needs one fully valid proposal and an explicit target')
            target=UUID(command['card_id']);version=command['expected_version']
            if type(version) is not int or version<1: raise ValueError('expected_version must be a positive integer')
            current=self._inspect('card',target);evidence,semantic=source_material(request,accepted[0])
            if current['card_type']!=semantic.card_type: raise ValueError('correction cannot change card type')
            self._approve('correct',{'target':current,'expected_version':version,
                'source':asdict(evidence),'replacement':asdict(semantic)})
            result['outcomes']=[self._service.correct_card(target,evidence,semantic.body,expected_version=version)]
            return result
        for item in decisions:
            if item.reason:
                result['outcomes'].append({'status':'rejected','reason':item.reason});continue
            evidence,semantic=source_material(request,item)
            if item.needs_confirmation:
                self._approve('save-sensitive',{'source':asdict(evidence),'semantic':asdict(semantic)})
            receipt=self._service.observe(evidence,semantics=[semantic])
            result['outcomes'].append({'status':'stored' if receipt['created'] else 'duplicate',**receipt})
        return result

    def _interpret(self, command):
        eid=UUID(command['evidence_id']);intent=UUID(command['intent_id']) if 'intent_id' in command else uuid4()
        explicit=command['action']=='restore'
        if explicit:
            self._approve('read-revoked-source',{'evidence_id':eid,'intent_id':intent})
            self._read_grants.add((intent,str(eid)))
            prepared=self._service.read_explicit_reinterpretation(eid,explicit_save_intent_id=intent)
        else: prepared=self._service.read_evidence_interpretation(eid)
        request=saved_request(prepared);decisions=validate_and_gate(request,self._adapter)
        if len(decisions)!=1 or any(d.reason for d in decisions):
            return {'synthetic':True,'intent_id':str(intent),'status':'rejected',
                'reasons':[d.reason or 'local_entry_requires_one_span' for d in decisions] or ['unsupported_synthetic_text']}
        semantics=[replace(d.proposal.semantic_input.to_semantic_input(),canonical_key=None) for d in decisions]
        provenance=decisions[0].proposal.producer.to_dict()
        if any(d.proposal.producer.to_dict()!=provenance for d in decisions):
            raise ValueError('one bounded interpretation must have one producer')
        if any(d.needs_confirmation for d in decisions) and not explicit:
            self._approve('interpret-sensitive',{'input':prepared.payload,'intent_id':intent,
                'semantics':[asdict(s) for s in semantics],'provenance':provenance})
        if explicit:
            # Acquire approval before entering core. The callback itself is a
            # read-only lookup of this exact contract, including source epoch.
            self._approve('restore-exact-output',{
                'contract':'liveday0:interpretation-intent:v1','intent_id':intent,
                'input':prepared.payload,'output':{'trace':None,
                    'semantics':[asdict(s) for s in semantics],'provenance':provenance}})
            receipt=self._service.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,
                semantics=semantics,provenance=provenance)
        else:
            receipt=self._service.commit_evidence_interpretation(prepared,intent_id=intent,
                semantics=semantics,provenance=provenance)
        return {'synthetic':True,'intent_id':str(intent),**receipt}
