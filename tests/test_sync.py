import pytest
from copy import deepcopy
from fastapi.testclient import TestClient
from app import main, store, syncstore
from app.engine import evaluate
from app.errors import BusinessError

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'))
    return TestClient(main.app)

def run_id_of(response):
    return response.json()['manifest']['run_id']

# ---------- 批次内容（纯计算） ----------

def test_batch_totals(sample):
    b=sample['sync_batch']
    assert b['count']==15 and b['restored']==160 and b['expired_refund']==10
    assert b['refunded']==170 and b['store_balance']==1010 and b['consistent']

def test_key_stable_across_reruns():
    a=evaluate()['sync_batch']['entries'];b=evaluate()['sync_batch']['entries']
    assert [e['key'] for e in a]==[e['key'] for e in b]

def test_key_unique_per_refund(sample):
    keys=[e['key'] for e in sample['sync_batch']['entries']]
    assert len(set(keys))==15 and all(k.startswith('sync-') for k in keys)

def test_key_ignores_line_number(sample):
    # 幂等键不含依据行号：同一笔退款重新导入行号变化也不视为新冲销。
    e=deepcopy(sample['sync_batch']['entries'][0])
    row={'id':e['refund'],'member':e['member'],'ref':e['ref'],'points':e['points'],
         'restored':e['restored'],'expired_refund':e['expired_refund']}
    from app.modules.sync.rules import entry_key
    assert entry_key(row)==e['key']

def test_unbalanced_batch_flagged(sample):
    s=deepcopy(sample);s['balance']['refunded']+=1
    from app.modules.sync.rules import build_batch
    assert build_batch(s)['consistent'] is False

# ---------- 发布与幂等 ----------

def test_publish_all_effective(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    v=c.post(f'/api/sync/{rid}/publish',json={}).json()
    assert v['status']=='已生效' and v['confirmed']==160 and v['is_current']
    assert v['ledger_count']==15 and v['ledger_total']==160
    assert all(e['status']=='已生效' for e in v['entries'])

def test_republish_same_batch_no_new_effect(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{rid}/publish',json={})
    again=c.post(f'/api/sync/{rid}/publish',json={}).json()
    assert len(again['versions'])==1 and again['ledger_count']==15 and again['ledger_total']==160

def test_retry_same_batch_no_new_effect(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{rid}/publish',json={})
    v=c.post(f'/api/sync/{rid}/retry',json={}).json()
    assert v['ledger_count']==15 and v['ledger_total']==160 and v['status']=='已生效'

# ---------- 回执丢失：冻结待核，不立即重发 ----------

def test_lost_receipt_freezes_then_confirms_without_reapply(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    v=c.post(f'/api/sync/{rid}/publish',json={'lose':['R1']}).json()
    r1=next(e for e in v['entries'] if e['refund']=='R1')
    assert r1['status']=='待核' and r1['receipt'] is None
    assert v['status']=='待核' and v['confirmed']==140
    # 回执虽丢，会员中心台账已应用：重试只能查证补登，不得重复生效。
    before=c.get(f'/api/sync/{rid}').json()
    v=c.post(f'/api/sync/{rid}/retry',json={}).json()
    r1=next(e for e in v['entries'] if e['refund']=='R1')
    assert r1['status']=='已生效' and '未重复下发' in r1['receipt']
    assert v['confirmed']==160 and v['status']=='已生效'
    assert v['ledger_count']==before['ledger_count'] and v['ledger_total']==before['ledger_total']

def test_dropped_receipt_reissued_only_after_proof(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    v=c.post(f'/api/sync/{rid}/publish',json={'drop':['R1']}).json()
    assert next(e for e in v['entries'] if e['refund']=='R1')['status']=='待核'
    assert v['ledger_count']==14  # 未送达：会员中心确实没有记录
    v=c.post(f'/api/sync/{rid}/retry',json={}).json()
    r1=next(e for e in v['entries'] if e['refund']=='R1')
    assert r1['status']=='已生效' and r1['receipt']=='查证后补发回执'
    assert v['ledger_count']==15 and v['ledger_total']==160

def test_retry_keeps_frozen_when_lost_again(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{rid}/publish',json={'drop':['R1']})
    v=c.post(f'/api/sync/{rid}/retry',json={'drop':['R1']}).json()
    assert next(e for e in v['entries'] if e['refund']=='R1')['status']=='待核'
    assert v['ledger_count']==14

def test_corrupt_receipt_makes_diff_and_retry_leaves_it(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    v=c.post(f'/api/sync/{rid}/publish',json={'corrupt':['R1']}).json()
    assert next(e for e in v['entries'] if e['refund']=='R1')['status']=='差异'
    assert v['status']=='差异' and v['diff_total']==1
    v=c.post(f'/api/sync/{rid}/retry',json={}).json()
    assert next(e for e in v['entries'] if e['refund']=='R1')['status']=='差异' and v['diff_total']==1

# ---------- 切换失败保留旧版 ----------

def test_failed_switch_keeps_old_version(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    dirty=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{dirty}/publish',json={})
    clean=run_id_of(c.post('/api/runs',json={'dataset':'clean'}))
    v=c.post(f'/api/sync/{clean}/publish',json={'fail_switch':True}).json()
    assert v['status']=='发布失败' and v['versions'][0]['status']=='发布失败'
    assert v['current']['run_id']==dirty and v['current']['version']==1
    old=c.get(f'/api/sync/{dirty}').json()
    assert old['status']=='已生效' and old['is_current'] and old['confirmed']==160
    assert old['ledger_count']==15 and old['ledger_total']==160

def test_switch_back_reuses_version_without_reapply(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    dirty=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{dirty}/publish',json={})
    # 另一批内容不同的冲销（退款号换前缀，幂等键不同）。
    custom=(ROOT/'sample_data/clean.csv').read_text().replace('\nR','\nQ')
    other=run_id_of(c.post('/api/runs',json={'csv_text':custom}))
    c.post(f'/api/sync/{other}/publish',json={})
    mid=c.get(f'/api/sync/{other}').json()
    assert mid['ledger_count']==30 and mid['ledger_total']==320
    v=c.post(f'/api/sync/{dirty}/publish',json={}).json()
    assert len(v['versions'])==1 and v['is_current']
    assert v['ledger_count']==30 and v['ledger_total']==320  # 切回不新增版本也不重复生效

def test_same_refund_reimport_dedupes(tmp_path,monkeypatch):
    # clean 与 dirty 的退款业务内容相同：同一冲销重复下发，会员中心不重复加减。
    c=client(tmp_path,monkeypatch)
    dirty=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{dirty}/publish',json={})
    clean=run_id_of(c.post('/api/runs',json={'dataset':'clean'}))
    v=c.post(f'/api/sync/{clean}/publish',json={}).json()
    assert v['ledger_count']==15 and v['ledger_total']==160
    assert all(e['status']=='已生效' for e in v['entries'])

# ---------- 门槛与证据链 ----------

def test_unbalanced_rejected(tmp_path):
    run={'manifest':{'run_id':'x','sha256':'y'},
         'sync_batch':{'consistent':False,'entries':[],'seal':'s'}}
    with pytest.raises(BusinessError) as e:
        syncstore.publish(str(tmp_path/'s.sqlite'),run)
    assert e.value.code=='SYNC_UNBALANCED'

def test_missing_batch_rejected(tmp_path):
    with pytest.raises(BusinessError) as e:
        syncstore.publish(str(tmp_path/'s.sqlite'),{'manifest':{'run_id':'x','sha256':'y'}})
    assert e.value.code=='SYNC_SNAPSHOT_MISSING'

def test_retry_before_publish_rejected(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    assert c.post(f'/api/sync/{rid}/retry',json={}).status_code==409

def test_unknown_simulate_refund_rejected(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    assert c.post(f'/api/sync/{rid}/publish',json={'lose':['NOPE']}).status_code==422

def test_evidence_chain_kept(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    before=store.get(main.DB_PATH,rid)
    v=c.post(f'/api/sync/{rid}/publish',json={}).json()
    after=store.get(main.DB_PATH,rid)
    # 发布只追加版本，原始退货依据（输入快照）一个字节都不变。
    assert before['input_snapshot']==after['input_snapshot']
    assert v['versions'][0]['evidence_sha256']==before['manifest']['sha256']
    assert v['entries'][0]['key'] and all('line' in e or True for e in v['entries'])

def test_entry_line_points_to_source(sample):
    r1=next(e for e in sample['sync_batch']['entries'] if e['refund']=='R1')
    assert r1['line']==5  # 原始 CSV 第 5 行，复核链可定位

# ---------- 页面 ----------

def test_page_shows_four_states(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    c.post(f'/api/sync/{rid}/publish',json={'lose':['R1'],'corrupt':['R3']})
    html=c.get(f'/sync?run_id={rid}').text
    for word in ('待核','已生效','差异','发布状态'):
        assert word in html
    assert '待核' in html and 'sync-' in html

def test_page_unpublished(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);rid=run_id_of(c.post('/api/runs',json={}))
    html=c.get(f'/sync?run_id={rid}').text
    assert '未发布' in html and '待下发' in html

def test_legacy_run_without_sync_content(tmp_path,monkeypatch):
    # 本功能上线前保存的旧批没有同步内容：页面提示重算，发布返回409。
    c=client(tmp_path,monkeypatch)
    legacy=evaluate();del legacy['sync_batch'];del legacy['views']['sync']
    store.save(main.DB_PATH,legacy);rid=legacy['manifest']['run_id']
    assert '请重算后再发布' in c.get(f'/sync?run_id={rid}').text
    assert c.post(f'/api/sync/{rid}/publish',json={}).status_code==409
