"""Pure protocol-v2 phase rules; financial effects are confirmed elsewhere."""
from __future__ import annotations
from copy import deepcopy
from .durable_operations import PHASE_TEXT, ERROR_TEXT

TEXT = PHASE_TEXT | {
 'PAY_AUTHORIZE':'가상 카드의 승인 결과를 확인해요',
 'PAY_AUTHORIZE_REPLACEMENT':'동의한 새 금액의 승인을 확인해요',
 'PAY_CAPTURE':'가상 결제 확정 결과를 확인해요',
 'PAY_VOID':'기존 승인 해제를 확인해요',
 'VOID_NEW_AUTH':'변경하지 못한 새 승인을 정리해요',
 'VOID_OLD_AUTH':'이전 금액의 승인 보류를 해제해요',
 'PAY_CHECK':'제조 전에 유효한 승인을 확인해요',
}
PAY_PHASES = {'PAY_AUTHORIZE','PAY_AUTHORIZE_REPLACEMENT','PAY_CAPTURE',
              'PAY_VOID','VOID_NEW_AUTH','VOID_OLD_AUTH'}
FAULTS = {'none','authorize_reply_lost','capture_reply_lost','void_reply_lost',
          'notification_duplicate','notification_late'}


def next_phase(op, phase):
    op['phase']=phase;op['message']=TEXT.get(phase,'처리 결과를 확인해요')


def public(op):
    from .durable_operations import public_operation
    return public_operation(op) | {'protocol_version':2,'phase_version':op['phase_version']}


def auth_command(op,phase):
    if phase in {'PAY_AUTHORIZE','PAY_AUTHORIZE_REPLACEMENT','VOID_NEW_AUTH'}:
        auth=op['new_auth']
    else:
        auth=op['old_auth']
    action=('AUTHORIZE' if phase in {'PAY_AUTHORIZE','PAY_AUTHORIZE_REPLACEMENT'} else
            'CAPTURE' if phase=='PAY_CAPTURE' else 'VOID')
    if auth is None:raise ValueError('NO_CHARGE phases must skip financial calls')
    result=deepcopy(auth)|{'action':action,'operation_key':f"{op['id']}:{phase}"}
    if action=='AUTHORIZE':result['card_token']=op['card_token']
    return result


def transport_fault(op,c):
    fault=op['payment_fault']
    if fault == {'AUTHORIZE':'authorize_reply_lost','CAPTURE':'capture_reply_lost','VOID':'void_reply_lost'}[c['action']]:
        return 'drop_reply'
    return {'notification_duplicate':'duplicate_notification','notification_late':'late_notification'}.get(fault,'none')


def expire(op, now):
    """A timeout is an abort decision, never evidence that approval did not happen."""
    if op['action']!='transfer' or op['decision']!='UNDECIDED' or now<op['recovery']['deadline_at']:
        return False
    op['decision']='ABORT';op['failure']='CONFIRMATION_EXPIRED'
    op['history'].append({'phase':op['phase'],'result':'ABORT_RECORDED','label':'변경 대기 기한이 지나 새 작업을 정리해요'})
    phase=op['phase']
    if phase=='DECIDE':
        next_phase(op,'VOID_NEW_AUTH' if op['new_auth'] and op['new_auth']!=op['old_auth'] else 'ABORT_TARGET')
    elif phase=='HOLD':next_phase(op,'ABORT_TARGET')
    # FREEZE must be reconfirmed before unfreeze. An in-flight AUTHORIZE must
    # first return its immutable receipt; a 404 would not justify cleanup success.
    return True


def rejected(op,message=None):
    op['status']='REJECTED'
    op['message']=message or ERROR_TEXT.get(op.get('failure'),'원래 주문과 혜택은 그대로예요. 새 작업을 정리했어요.')


def transition(op,result):
    phase=op['phase']
    if phase=='DECIDE':
        op['decision']='COMMIT';next_phase(op,'RELEASE_SOURCE');return
    if phase=='FINALIZE':
        op['status']='COMPLETED';op['message']=('같은 주문으로 매장을 바꿨어요. 매장·결제 기록도 확인했어요.' if op['action']=='transfer' else '주문·매장·결제의 처리를 확인했어요.');return
    if not result['ok']:
        op['failure']=result.get('code','UNCONFIRMED')
        if phase in {'PAY_AUTHORIZE','PAY_AUTHORIZE_REPLACEMENT'} and op['failure'] in {'DECLINED','LIMIT_EXCEEDED'}:
            op['decision']='ABORT'
            if op['action']=='transfer':next_phase(op,'ABORT_TARGET')
            else:rejected(op,'가상 카드 승인이 거절됐어요. 주문이나 혜택을 소모하지 않았어요.')
            return
        if phase=='ADMIT':
            op['decision']='ABORT'
            if op['new_auth']:next_phase(op,'VOID_NEW_AUTH')
            else:rejected(op,'매장이 주문을 받지 못했어요. 결제 없이 종료했어요.')
            return
        if phase=='HOLD' and op['decision']!='COMMIT':
            op['decision']='ABORT';next_phase(op,'ABORT_TARGET');return
        if phase=='FREEZE':
            rejected(op,'원래 매장 상태가 바뀌어 변경하지 않았어요. 기존 주문을 확인해 주세요.');return
        # Once money may have moved or a COMMIT exists, never guess rollback.
        op['recovery']['state']='REVIEW_REQUIRED'
        op['message']='서버 기록이 예상과 달라 추가 확인이 필요해요. 새 결제로 처리하지 않았어요.'
        return
    if phase in {'PAY_AUTHORIZE','PAY_AUTHORIZE_REPLACEMENT'}:
        if op['decision']=='ABORT':next_phase(op,'VOID_NEW_AUTH')
        else:next_phase(op,'DECIDE' if op['action']=='transfer' else 'ADMIT')
    elif phase=='FREEZE':next_phase(op,'ABORT_TARGET' if op['decision']=='ABORT' else 'HOLD')
    elif phase=='HOLD':
        needs_new=op['new_auth'] is not None and op['new_auth']!=op['old_auth']
        next_phase(op,'PAY_AUTHORIZE_REPLACEMENT' if needs_new else 'DECIDE')
    elif phase=='RELEASE_SOURCE':next_phase(op,'ACTIVATE')
    elif phase=='ACTIVATE':
        next_phase(op,'VOID_OLD_AUTH' if op['old_auth'] and op['new_auth']!=op['old_auth'] else 'FINALIZE')
    elif phase=='VOID_NEW_AUTH':
        if op['action']=='transfer':next_phase(op,'ABORT_TARGET')
        else:rejected(op,'매장 접수를 완료하지 못해 새 승인 보류를 해제했어요.')
    elif phase=='ABORT_TARGET':next_phase(op,'UNFREEZE')
    elif phase=='UNFREEZE':rejected(op)
    elif phase=='PAY_CAPTURE':op['decision']='COMMIT';next_phase(op,'CLAIM')
    elif phase=='CANCEL':next_phase(op,'PAY_VOID' if op['old_auth'] else 'FINALIZE')
    elif phase=='PAY_CHECK':next_phase(op,op['action'].upper())
    else:next_phase(op,'FINALIZE')
