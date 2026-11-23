import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    digest TEXT UNIQUE NOT NULL,
    result TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '待复核'
);
CREATE TABLE IF NOT EXISTS netting_commands (
    lineage TEXT NOT NULL,
    version INTEGER NOT NULL,
    command_id TEXT NOT NULL,
    run_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes TEXT,
    evidence TEXT NOT NULL,
    PRIMARY KEY(lineage, version)
);
CREATE INDEX IF NOT EXISTS netting_commands_run ON netting_commands(lineage, run_digest);"""

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    return con

def _command(result):
    netting=result.get('netting')
    snapshot=result.get('input_snapshot') or {}
    if not netting or not snapshot.get('lineage'): return None,None
    return netting,snapshot['lineage']

def _issuable(netting):
    return netting.get('status')=='可出指令' and (netting.get('checks') or {}).get('ok') is True

def _latest_issued(con,lineage):
    """最近一版真正可出令的指令；暂缓版只入台账，不顶替它。"""
    row=con.execute("SELECT command_id FROM netting_commands WHERE lineage=? AND status='可出指令' ORDER BY version DESC LIMIT 1",(lineage,)).fetchone()
    return row['command_id'] if row else None

def _effective(con,lineage,result):
    netting=result['netting']
    return netting['command_id'] if _issuable(netting) else _latest_issued(con,lineage)

def _register(con, result):
    """指令台账：同一批次结果重跑复用同一版；状态变化才新增版本，不累加净额。"""
    netting,lineage=_command(result)
    if not netting: return
    digest=result['manifest']['sha256']
    row=con.execute('SELECT * FROM netting_commands WHERE lineage=? AND run_digest=?',(lineage,digest)).fetchone()
    if row is None:
        latest=con.execute('SELECT * FROM netting_commands WHERE lineage=? ORDER BY version DESC LIMIT 1',(lineage,)).fetchone()
        status='可出指令' if _issuable(netting) else '暂缓'
        version=(latest['version']+1) if latest else 1
        con.execute('INSERT INTO netting_commands(lineage,version,command_id,run_digest,run_id,policy,status,supersedes,evidence) VALUES(?,?,?,?,?,?,?,?,?)',
            (lineage,version,netting['command_id'],digest,result['manifest']['run_id'],netting['policy'],status,
             latest['command_id'] if latest else None,
             json.dumps({'command_id':netting['command_id'],'policy':netting['policy'],'status':netting['status'],
                         'checks':netting.get('checks'),'summary':netting.get('summary'),
                         'global_blocks':netting.get('global_blocks',[]),'basis':netting.get('basis'),
                         'run_id':result['manifest']['run_id']},ensure_ascii=False)))
    result['command_ledger']=_ledger(con,lineage,result)
    result['netting_effective']=_load(con,result['command_ledger']['effective'])

def _ledger(con,lineage,result):
    versions=[{'version':r['version'],'command_id':r['command_id'],'policy':r['policy'],'status':r['status'],
               'supersedes':r['supersedes'],'run_id':r['run_id']}
              for r in con.execute('SELECT * FROM netting_commands WHERE lineage=? ORDER BY version',(lineage,))]
    return {'lineage':lineage,'versions':versions,'effective':_effective(con,lineage,result)}

def _load(con,command_id,lineage=None):
    if not command_id: return None
    if lineage:
        row=con.execute('SELECT * FROM netting_commands WHERE lineage=? AND command_id=? ORDER BY version DESC LIMIT 1',(lineage,command_id)).fetchone()
    else:
        row=con.execute('SELECT * FROM netting_commands WHERE command_id=? ORDER BY version DESC LIMIT 1',(command_id,)).fetchone()
    if not row: return None
    evidence=json.loads(row['evidence'])
    evidence.update(command_id=row['command_id'],version=row['version'],ledger_status=row['status'],run_id=row['run_id'])
    return evidence

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
        _register(con,result)
        con.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),run_id))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return refresh(path,result)

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def refresh(path, result):
    """读取时刷新台账：生效指令按当前结果与数据库状态计算，不沿用旧快照。

    归档状态同时重算，使“生效指令沿用上一版”与“净额对不上整批暂缓”在页面上一致。
    """
    netting,lineage=_command(result)
    if not netting: return result
    with connect(path) as con:
        result['command_ledger']=_ledger(con,lineage,result)
        result['netting_effective']=_load(con,result['command_ledger']['effective'],lineage)
    from app.modules.archive.rules import archive_decision
    result['archive']=archive_decision(result)
    view=result.get('views',{}).get('archive')
    if view is not None:
        view['metrics'].update({'净额指令':result['archive']['netting']['command_id'] or '—',
                                '净额_CNY':result['archive']['netting']['net_total'] or '—',
                                '状态':result['archive']['status']})
    return result

def commands(path, run_id):
    result=get(path,run_id)
    ledger=result.get('command_ledger') or {'lineage':None,'versions':[],'effective':None}
    with connect(path) as con:
        return {**ledger,'effective_command':_load(con,ledger.get('effective'),ledger.get('lineage'))}

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)
