from fastapi.testclient import TestClient
from app import main,store
from app.engine import evaluate
from app.errors import BusinessError
import json, pytest

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'))
    return TestClient(main.app)
def test_api_run_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();b=c.post('/api/runs',json={}).json();assert a['manifest']['run_id']==b['manifest']['run_id'];assert len(c.get('/api/runs').json())==1
def test_review_stale_http(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();url='/api/runs/'+a['manifest']['run_id']+'/review';assert c.post(url,json={'revision':1}).status_code==200;assert c.post(url,json={'revision':1}).status_code==409
def test_all_modules_and_pages(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    for k in main.MODULES:
        assert c.post('/api/'+k,json={}).status_code==200
        assert c.get('/'+k).status_code==200
def test_unknown_batch_http(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/runs/missing').status_code==404
def test_bad_sample_http(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.post('/api/runs',json={'dataset':'../x'}).status_code==422
def test_persistence_after_new_connection(tmp_path,sample):
    path=str(tmp_path/'durable.sqlite');store.save(path,sample);assert store.get(path,sample['manifest']['run_id'])['balance']['due']=='22540.41'
def test_invalid_review_does_not_change(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);s=c.post('/api/runs',json={}).json();rid=s['manifest']['run_id'];assert c.post('/api/runs/'+rid+'/review',json={'revision':0}).status_code==422;assert c.get('/api/runs/'+rid).json()['revision']==1

def test_clean_navigation_run_preserved(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    for k in main.MODULES:
        html=c.get('/'+k+'?run_id='+rid).text
        assert '?run_id='+rid in html
        assert rid in html
    assert c.get('/api/export/'+rid).status_code==200

def test_custom_csv_snapshot_and_navigation(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    original=(ROOT/'sample_data/clean.csv').read_text()
    # 改成独立于所有仓内样例的证据编号，追踪其跨页与重跑行为。
    first=original.splitlines()[1].split(',')[0]
    custom=original.replace(first,first+'-CUSTOM')
    result=c.post('/api/runs',json={'csv_text':custom}).json()
    rid=result['manifest']['run_id']
    assert '-CUSTOM' in str(result['records']) or '-CUSTOM' in str(result['quarantine'])
    for key in main.MODULES:
        response=c.get('/'+key+'?run_id='+rid)
        assert response.status_code==200
        assert '?run_id='+rid in response.text
    repeated=c.post('/api/runs/'+rid+'/rerun',json={})
    assert repeated.status_code==200
    assert repeated.json()['manifest']['run_id']==rid
    assert repeated.json()['manifest']['sha256']==result['manifest']['sha256']

def test_netting_rerun_no_duplicate_command(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    first=c.post('/api/runs',json={}).json()
    rid=first['manifest']['run_id']
    command=first['netting']['command_id']
    again=c.post('/api/runs/'+rid+'/rerun',json={}).json()
    assert again['netting']['command_id']==command
    ledger=again['command_ledger']
    assert [v['command_id'] for v in ledger['versions']].count(command)==1
    assert ledger['effective']==command
    assert again['netting']['basis']['net_total']==first['netting']['basis']['net_total']

def test_netting_policy_switch_creates_version(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    first=c.post('/api/runs',json={}).json()
    rid=first['manifest']['run_id']
    switched=c.post('/api/runs/'+rid+'/rerun',json={'policy':'all_confirmed'}).json()
    assert switched['netting']['policy']=='all_confirmed'
    versions=switched['command_ledger']['versions']
    assert len({v['command_id'] for v in versions})==len(versions)
    assert switched['command_ledger']['effective']==switched['netting']['command_id']
    assert switched['netting']['basis']['net_total']!=first['netting']['basis']['net_total']

def test_netting_held_keeps_previous_command_effective(tmp_path,monkeypatch):
    import copy
    path=str(tmp_path/'ledger.sqlite')
    good=evaluate({'dataset':'clean'})
    stored=store.save(path,good)
    previous=stored['command_ledger']['effective']
    assert previous==good['netting']['command_id']
    # 同一谱系内出现整批暂缓的新结果：只入台账，生效指令沿用上一版。
    held=copy.deepcopy(good)
    held['netting']['status']='暂缓'
    held['netting']['fingerprint']='held-fingerprint'
    held['netting']['command_id']='NET-heldtest'
    held['netting']['basis']['net_total']='0.00'
    held['manifest']={**held['manifest'],'run_id':'held-run','sha256':'held-digest'}
    stored_held=store.save(path,held)
    versions=stored_held['command_ledger']['versions']
    assert len(versions)==2 and versions[-1]['status']=='暂缓'
    assert stored_held['command_ledger']['effective']==previous
    assert stored_held['netting_effective']['command_id']==previous
    # 重跑同一暂缓结果不新增版本。
    store.save(path,held)
    assert len(store.commands(path,'held-run')['versions'])==2

def test_export_keeps_source_and_netting(tmp_path,monkeypatch):
    import zipfile, io
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={}).json()
    rid=run['manifest']['run_id']
    payload=c.get('/api/export/'+rid).content
    bundle=zipfile.ZipFile(io.BytesIO(payload))
    assert bundle.read('source.csv')==(main.ROOT/'sample_data/dirty.csv').read_bytes()
    evidence=json.loads(bundle.read('netting.json'))
    assert evidence['netting']['command_id']==run['netting']['command_id']
    assert evidence['ledger']['effective']==run['command_ledger']['effective']
    assert json.loads(bundle.read('manifest.json'))['run_id']==rid

def test_policy_switch_failure_keeps_previous_command(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    csv_text=(ROOT/'sample_data/clean.csv').read_text()
    entities=[{'ticket':f'{i:05d}','entity':'E-102'} for i in range(1,21)]
    run=c.post('/api/runs',json={'csv_text':csv_text,'overrides':{
        'entities':entities,
        'boat_charges':[{'ref':'SHIP-102','entity':'E-102','amount':'1500.00','state':'closed'}],
        'pending_corrections':[{'ticket':'00005','ref':'RESHIP-5','detail':'复秤未闭合'}]}}).json()
    rid=run['manifest']['run_id']
    # 已闭合票先出令：其余19票照常出令，00005 暂缓。
    assert run['netting']['status']=='可出指令'
    previous=run['command_ledger']['effective']
    assert previous==run['netting']['command_id']
    assert [t['ticket'] for t in run['netting']['held_tickets']]==['00005']
    # 切换全票确认口径：该主体仍有未闭合复秤，本次整户暂缓，只入台账不顶替上一版。
    held=c.post('/api/runs/'+rid+'/rerun',json={'policy':'all_confirmed'}).json()
    assert held['netting']['status']=='暂缓'
    assert held['command_ledger']['effective']==previous
    assert held['netting_effective']['command_id']==previous
    assert held['archive']['status']=='待处理'
    assert [(v['version'],v['status']) for v in held['command_ledger']['versions']]==[(1,'可出指令'),(2,'暂缓')]

def test_switch_back_reuses_previous_version(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    first=c.post('/api/runs',json={}).json()
    rid=first['manifest']['run_id']
    previous=first['command_ledger']['effective']
    switched=c.post('/api/runs/'+rid+'/rerun',json={'policy':'all_confirmed'}).json()
    assert switched['netting']['command_id']!=previous
    back=c.post('/api/runs/'+rid+'/rerun',json={'policy':'closed_first'}).json()
    # 回到原口径：结果与首版同摘要，复用同一指令，不新增版本。
    assert back['netting']['command_id']==previous
    assert back['command_ledger']['effective']==previous
    assert len(back['command_ledger']['versions'])==2
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={}).json()
    ledger=c.get('/api/runs/'+run['manifest']['run_id']+'/commands').json()
    assert ledger['effective']==run['netting']['command_id']
    assert ledger['effective_command']['command_id']==ledger['effective']
    assert c.get('/api/runs/missing/commands').status_code==404

def test_netting_page_policy_selector(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    html=c.get('/netting?policy=all_confirmed').text
    assert '全票确认后轧差' in html and '已闭合票先出令' in html
    assert c.get('/netting?policy=bogus').status_code==422
