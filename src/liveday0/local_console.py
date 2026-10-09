"""Interactive trusted local console and a fixed synthetic demo."""
import json
import sys
from uuid import uuid4

from liveday0.exceptions import InterpretationRevoked
from liveday0.local_extraction import SYNTHETIC_EXAMPLES
from liveday0.local_host import LocalMemorySession


def terminal_confirmation(approval):
    # Deliberately separate from request JSON/stdin. A model flag cannot answer.
    with open('/dev/tty','r+') as terminal:
        terminal.write('\n请审阅这一次操作：'+approval.action+'\n')
        terminal.write(json.dumps(approval.payload,ensure_ascii=False,indent=2)+'\n')
        terminal.write('仅批准上述完整范围请输入 SAVE；其他输入取消：')
        terminal.flush()
        return terminal.readline().strip()=='SAVE'


def run_synthetic_demo():
    """Fixed synthetic inputs only; consent fixture is not real human auth evidence."""
    confirmations=[]
    def synthetic_consent(approval):
        confirmations.append({'action':approval.action,'fingerprint':approval.fingerprint})
        return True
    session=LocalMemorySession(confirm=synthetic_consent)
    texts=list(SYNTHETIC_EXAMPLES);steps=[]
    def call(command):
        result=session.handle(command);steps.append({'action':command['action'],'result':result});return result
    captured=call({'action':'capture','text':texts[0]});receipt=captured['outcomes'][0]
    replay=call({'action':'capture','text':texts[0],'message_id':captured['message_id'],'occurred_at':captured['occurred_at']})
    assert replay['outcomes'][0]['card_ids']==receipt['card_ids'] and not replay['outcomes'][0]['created']
    assert '等待朋友帮忙' in json.dumps(call({'action':'query','query':'合成搬家'}),ensure_ascii=False)
    call({'action':'correct','card_id':str(receipt['card_ids'][0]),'expected_version':1,'text':texts[1]})
    corrected=call({'action':'query','query':'合成搬家'})
    assert '搬家完成' in json.dumps(corrected,ensure_ascii=False) and '等待朋友帮忙' not in json.dumps(corrected,ensure_ascii=False)
    other=call({'action':'capture','text':texts[2]})['outcomes'][0]
    interpreted=call({'action':'interpret','evidence_id':str(other['evidence_id']),'intent_id':str(uuid4())})
    assert interpreted['evidence_id']==other['evidence_id']
    call({'action':'delete','kind':'card','id':str(receipt['card_ids'][0])})
    try: session.handle({'action':'interpret','evidence_id':str(receipt['evidence_id'])})
    except InterpretationRevoked: steps.append({'action':'automatic-after-delete','result':'blocked'})
    else: raise AssertionError('revoked source accepted automatic interpretation')
    restored=call({'action':'restore','evidence_id':str(receipt['evidence_id']),'intent_id':str(uuid4())})
    assert restored['card_ids']!=receipt['card_ids']
    try: session.handle({'action':'interpret','evidence_id':str(receipt['evidence_id'])})
    except InterpretationRevoked: steps.append({'action':'automatic-after-restore','result':'still-blocked'})
    else: raise AssertionError('explicit restore reopened automatic interpretation')
    call({'action':'delete','kind':'evidence','id':str(receipt['evidence_id'])})
    secret=call({'action':'capture','text':texts[0],'sensitivity':'secret'})
    assert secret['outcomes'][0]['reason']=='secret_source'
    final=call({'action':'query','query':'合成搬家'})
    assert '搬家完成' not in json.dumps(final,ensure_ascii=False) and '等待朋友帮忙' not in json.dumps(final,ensure_ascii=False)
    return {'synthetic':True,'adapter_production_ready':False,'identity_source':'current-os-login',
        'consent':'synthetic fixture; not real human authentication validation',
        'steps':steps,'confirmations':confirmations,'surviving_evidence_id':other['evidence_id']}


def main(*, demo=False):
    if demo:
        print(json.dumps(run_synthetic_demo(),ensure_ascii=False,default=str,indent=2));return
    if not sys.stdin.isatty():
        raise ValueError('local interactive commands require a human terminal; use --demo only for fixed synthetic inputs')
    session=LocalMemorySession(confirm=terminal_confirmation)
    print('LiveDay0 合成本地入口：仅识别以下固定样例，不代表通用自然语言理解。')
    print(json.dumps(list(SYNTHETIC_EXAMPLES),ensure_ascii=False,indent=2))
    print('输入 JSON 命令：capture/query/correct/delete/interpret/restore。不能传 tenant。输入 quit 退出。')
    while True:
        try: line=input('liveday0> ')
        except EOFError: break
        if line.strip()=='quit': break
        try:
            result=session.handle(json.loads(line))
            print(json.dumps(result,ensure_ascii=False,default=str,indent=2))
        except Exception as error:
            # No request/prompt dumps or persistent interaction log.
            print(json.dumps({'error':type(error).__name__,'message':str(error)},ensure_ascii=False))
