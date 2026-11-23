import hashlib, json
from app.modules.netting.rules import verify_command

def archive_decision(state):
    netting=state.get('netting') or {}
    effective=state.get('netting_effective') or None
    # 归档前把当前净额与最终账再核一次；生效指令（可能沿用上一版）与其依据也必须对得上。
    netting_ok=(verify_command(state, netting)['ok'] if netting else True)
    netting_ok=netting_ok and (verify_command(state, effective)['ok'] if effective else True)
    # 本次运行若未能出令（口径切换失败等），只能归档上一版指令，状态保持待处理。
    netting_ok=netting_ok and (netting.get('status')=='可出指令' if netting else True)
    ready=(state['balance']['row_balanced'] and state['balance']['money_balanced']
           and not state['balance']['blocked'] and netting_ok)
    seal=hashlib.sha256(json.dumps(state['final'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'seal':seal,'status':'可归档' if ready else '待处理','tickets':len(state['final']),
            'reasons':len(state['quarantine']),
            'netting':{'command_id':netting.get('command_id',''),'status':netting.get('status',''),
                       'net_total':(netting.get('basis') or {}).get('net_total',''),
                       'included':len(netting.get('included_tickets',[])),
                       'held':len(netting.get('held_tickets',[]))}}

def apply(state):
    state['archive']=archive_decision(state)
    rows=state['version_audit']
    state['views']['archive']={'title':'结算封存','rows':rows,
        'metrics':{'封存票':state['archive']['tickets'],'隔离证据':len(state['quarantine']),
                   '净额指令':state['archive']['netting']['command_id'] or '—',
                   '净额_CNY':state['archive']['netting']['net_total'] or '—',
                   '状态':state['archive']['status']}}
