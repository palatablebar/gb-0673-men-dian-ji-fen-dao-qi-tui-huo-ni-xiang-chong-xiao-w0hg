import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

CHANNEL='member-center'  # 仅会员中心一个渠道，不展开其他渠道

DDL="""
CREATE TABLE IF NOT EXISTS sync_versions(
  run_id TEXT NOT NULL, version INTEGER NOT NULL, seal TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,version));
CREATE TABLE IF NOT EXISTS sync_entries(
  run_id TEXT NOT NULL, version INTEGER NOT NULL, key TEXT NOT NULL,
  refund TEXT NOT NULL, member TEXT NOT NULL, restored INTEGER NOT NULL,
  expired_refund INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT,
  PRIMARY KEY(run_id,version,key));
CREATE TABLE IF NOT EXISTS sync_current(
  channel TEXT PRIMARY KEY, run_id TEXT NOT NULL, version INTEGER NOT NULL);
-- 会员中心台账：模拟外部会员中心，按幂等键去重，是"会员中心结果"的唯一来源。
CREATE TABLE IF NOT EXISTS member_center(
  key TEXT PRIMARY KEY, member TEXT NOT NULL, restored INTEGER NOT NULL,
  expired_refund INTEGER NOT NULL, run_id TEXT NOT NULL, version INTEGER NOT NULL,
  applied_at TEXT NOT NULL);
"""

def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(DDL)
    return con

def _now():
    return datetime.now(timezone.utc).isoformat()

def _require_batch(run):
    batch=run.get('sync_batch')
    if batch is None:
        raise BusinessError('SYNC_SNAPSHOT_MISSING','旧批缺少同步内容，请重算后再发布',409)
    return batch

def _check_simulate(batch,names):
    known={e['refund'] for e in batch['entries']}
    unknown=set(names)-known
    if unknown: raise BusinessError('BAD_SIMULATE',f'演练编号不在本批:{",".join(sorted(unknown))}')

def _dispatch(con,run_id,version,e,applied,now):
    # 幂等下发：会员中心已有同键记录则不新增生效，直接以台账为准。
    dup=con.execute('SELECT restored FROM member_center WHERE key=?',(e['key'],)).fetchone()
    if dup is not None:
        if dup['restored']==e['restored']:
            return '已生效','已存在同键记录，不重复生效'
        return '差异',f"台账{dup['restored']}≠应恢复{e['restored']}"
    con.execute('INSERT INTO member_center(key,member,restored,expired_refund,run_id,version,applied_at) VALUES(?,?,?,?,?,?,?)',
                (e['key'],e['member'],applied,e['expired_refund'],run_id,version,now))
    return None,None

def publish(path,run,lose=(),drop=(),corrupt=(),fail_switch=False):
    batch=_require_batch(run)
    if not batch['consistent']:
        raise BusinessError('SYNC_UNBALANCED','恢复额、过期损失与门店账不平，禁止发布')
    for names in (lose,drop,corrupt):
        if not isinstance(names,list): raise BusinessError('BAD_SIMULATE','演练参数须为退款号列表')
        _check_simulate(batch,names)
    if set(lose)&set(corrupt) or set(drop)&set(corrupt) or set(lose)&set(drop):
        raise BusinessError('BAD_SIMULATE','同一退款不能同时参与多种演练')
    run_id=run['manifest']['run_id'];now=_now()
    with connect(path) as con:
        current=con.execute('SELECT * FROM sync_current WHERE channel=?',(CHANNEL,)).fetchone()
        if current and current['run_id']==run_id:
            pass  # 重复发布同一批：幂等返回，不新增版本不重复下发
        else:
            same=con.execute("SELECT * FROM sync_versions WHERE run_id=? AND seal=? AND status='已发布' ORDER BY version DESC",
                             (run_id,batch['seal'])).fetchone()
            if same:
                # 内容相同的旧版直接切回：不新增版本，会员中心按幂等键天然不重复生效。
                con.execute('INSERT INTO sync_current(channel,run_id,version) VALUES(?,?,?) ON CONFLICT(channel) DO UPDATE SET run_id=excluded.run_id,version=excluded.version',
                            (CHANNEL,run_id,same['version']))
            else:
                version=con.execute('SELECT COALESCE(MAX(version),0)+1 FROM sync_versions WHERE run_id=?',(run_id,)).fetchone()[0]
                if fail_switch:
                    # 切换失败：只留失败记录，当前指针与会员中心台账都不动，旧版同步结果完整保留。
                    con.execute('INSERT INTO sync_versions(run_id,version,seal,evidence_sha256,status,created_at) VALUES(?,?,?,?,?,?)',
                                (run_id,version,batch['seal'],run['manifest']['sha256'],'发布失败',now))
                else:
                    for e in batch['entries']:
                        applied=e['restored']-1 if e['refund'] in corrupt else e['restored']
                        if e['refund'] in drop:
                            status,receipt='待核',None  # 未送达：会员中心无记录，回执无从谈起
                        else:
                            status,receipt=_dispatch(con,run_id,version,e,applied,now)
                            if status is None:
                                if e['refund'] in lose:
                                    status,receipt='待核',None  # 已应用但回执丢失：冻结待核，绝不立即重发
                                elif e['refund'] in corrupt:
                                    status,receipt='差异',f"回执{applied}≠应恢复{e['restored']}"
                                else:
                                    status,receipt='已生效',f'ack-v{version}'
                        con.execute('INSERT INTO sync_entries(run_id,version,key,refund,member,restored,expired_refund,status,receipt) VALUES(?,?,?,?,?,?,?,?,?)',
                                    (run_id,version,e['key'],e['refund'],e['member'],e['restored'],e['expired_refund'],status,receipt))
                    con.execute('INSERT INTO sync_versions(run_id,version,seal,evidence_sha256,status,created_at) VALUES(?,?,?,?,?,?)',
                                (run_id,version,batch['seal'],run['manifest']['sha256'],'已发布',now))
                    con.execute('INSERT INTO sync_current(channel,run_id,version) VALUES(?,?,?) ON CONFLICT(channel) DO UPDATE SET run_id=excluded.run_id,version=excluded.version',
                                (CHANNEL,run_id,version))
    return view(path,run_id)

def retry(path,run,lose=(),drop=()):
    batch=_require_batch(run)
    for names in (lose,drop):
        if not isinstance(names,list): raise BusinessError('BAD_SIMULATE','演练参数须为退款号列表')
        _check_simulate(batch,names)
    if set(lose)&set(drop):
        raise BusinessError('BAD_SIMULATE','同一退款不能同时参与多种演练')
    run_id=run['manifest']['run_id']
    with connect(path) as con:
        published=con.execute("SELECT * FROM sync_versions WHERE run_id=? AND status='已发布' ORDER BY version DESC",(run_id,)).fetchone()
        if not published:
            raise BusinessError('NOT_PUBLISHED','本批尚未发布，没有可重试的待核条目',409)
        version=published['version'];now=_now()
        pending=con.execute("SELECT * FROM sync_entries WHERE run_id=? AND version=? AND status='待核'",(run_id,version)).fetchall()
        for e in pending:
            dup=con.execute('SELECT restored FROM member_center WHERE key=?',(e['key'],)).fetchone()
            if dup is not None:
                # 查证证明前次已生效：只补登回执，不重复下发。
                if dup['restored']==e['restored']:
                    con.execute("UPDATE sync_entries SET status='已生效',receipt=? WHERE run_id=? AND version=? AND key=?",
                                ('查证补登：前次已生效，未重复下发',run_id,version,e['key']))
                else:
                    con.execute("UPDATE sync_entries SET status='差异',receipt=? WHERE run_id=? AND version=? AND key=?",
                                (f"台账{dup['restored']}≠应恢复{e['restored']}",run_id,version,e['key']))
                continue
            # 查证证明前次未生效：才允许补发；再次丢失则继续冻结待核。
            if e['refund'] in drop:
                continue
            con.execute('INSERT INTO member_center(key,member,restored,expired_refund,run_id,version,applied_at) VALUES(?,?,?,?,?,?,?)',
                        (e['key'],e['member'],e['restored'],e['expired_refund'],run_id,version,now))
            if e['refund'] not in lose:
                con.execute("UPDATE sync_entries SET status='已生效',receipt=? WHERE run_id=? AND version=? AND key=?",
                            ('查证后补发回执',run_id,version,e['key']))
    return view(path,run_id)

def view(path,run_id):
    with connect(path) as con:
        current=con.execute('SELECT * FROM sync_current WHERE channel=?',(CHANNEL,)).fetchone()
        versions=[dict(r) for r in con.execute('SELECT run_id,version,seal,evidence_sha256,status,created_at FROM sync_versions WHERE run_id=? ORDER BY version DESC',(run_id,))]
        published=next((v for v in versions if v['status']=='已发布'),None)
        entries=[]
        if published:
            entries=[dict(r) for r in con.execute('SELECT * FROM sync_entries WHERE run_id=? AND version=? ORDER BY rowid',(run_id,published['version']))]
        ledger=con.execute('SELECT COALESCE(SUM(restored),0) s,COUNT(*) n FROM member_center').fetchone()
        diff_total=0
        for e in entries:
            if e['status']=='差异':
                actual=con.execute('SELECT restored FROM member_center WHERE key=?',(e['key'],)).fetchone()
                diff_total+=e['restored']-(actual['restored'] if actual else 0)
    current=dict(current) if current else None
    pending=sum(1 for e in entries if e['status']=='待核')
    diffs=sum(1 for e in entries if e['status']=='差异')
    confirmed=sum(e['restored'] for e in entries if e['status']=='已生效')
    is_current=bool(current and published and current['run_id']==run_id and current['version']==published['version'])
    if not versions:
        status,status_text='未发布','未发布'
    elif not published:
        kept=f"，保留旧版 {current['run_id']} v{current['version']}" if current else ''
        status,status_text='发布失败',f"发布失败（v{versions[0]['version']}）{kept}"
    else:
        parts=[]
        if pending: parts.append(f'待核 {pending}')
        if diffs: parts.append(f'差异 {diffs}')
        status='待核' if pending else ('差异' if diffs else '已生效')
        tag=' · '.join(parts) if parts else '已生效'
        status_text=f"已发布 v{published['version']} · {tag}"+('［当前］' if is_current else '［非当前］')
    return {'run_id':run_id,'status':status,'status_text':status_text,'is_current':is_current,
            'current':current,'versions':versions,'entries':entries,
            'pending':pending,'diffs':diffs,'confirmed':confirmed,'diff_total':diff_total,
            'ledger_total':ledger['s'],'ledger_count':ledger['n']}

def merge(batch_entries,published_entries):
    by_key={e['key']:e for e in published_entries}
    merged=[]
    for e in batch_entries:
        p=by_key.get(e['key'])
        merged.append({'refund':e['refund'],'member':e['member'],'ref':e['ref'],'points':e['points'],
                       'restored':e['restored'],'expired_refund':e['expired_refund'],'line':e['line'],
                       'key':e['key'],'status':p['status'] if p else '待下发',
                       'receipt':(p['receipt'] or '') if p else ''})
    return merged
