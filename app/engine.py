from importlib import import_module
from pathlib import Path
from copy import deepcopy
import hashlib, json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest

def netting_lineage(raw, context):
    """同一账本的指令归入同一谱系。

    优先使用主体登记里显式给出的账本号，使迟到更正重新导入后仍能接续同一台账；
    未给出时退回源字节摘要，保证不同批次互不干扰。迟到更正只新增版本，不另起账本。
    """
    ledger=str(context.get('ledger','')).strip()
    entities=sorted({str(item.get('entity','')).strip() for item in (context.get('entities') or []) if isinstance(item,dict)})
    names=context.get('entity_names') if isinstance(context.get('entity_names'),dict) else {}
    seed={'ledger':ledger,'raw':hashlib.sha256(raw).hexdigest() if not ledger else '','entities':entities,'names':names}
    return hashlib.sha256(json.dumps(seed,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]

def evaluate(payload=None, until=None, snapshot=None, policy=None):
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    raw=(base64.b64decode(snapshot['bytes_b64'],validate=True) if snapshot is not None else (str(payload['csv_text']).encode() if 'csv_text' in payload else (ROOT/'sample_data'/f'{dataset}.csv').read_bytes()))
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    context_path=ROOT/'sample_data'/f'context_{dataset}.json'
    if not context_path.exists(): context_path=ROOT/'sample_data/context.json'
    context=deepcopy(snapshot['context']) if snapshot is not None else json.loads(context_path.read_text())
    context.update(deepcopy(payload.get('overrides',{})))
    policy=policy or payload.get('policy') or (snapshot or {}).get('policy') or context.get('netting_policy','closed_first')
    context.pop('netting_policy',None)
    lineage=(snapshot or {}).get('lineage') or netting_lineage(raw,context)
    start=time.perf_counter()
    state={'raw_bytes':raw,'context':context,'policy':policy,'lineage':lineage,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset}
    for index,key in enumerate(MODULES,1):
        if until is not None and index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'context':deepcopy(context),'policy':policy,'lineage':lineage}
    state.pop('raw_bytes',None)
    state['manifest']=make_manifest(digest(raw,{**context,'netting_policy':policy}),dataset,time.perf_counter()-start)
    return state
