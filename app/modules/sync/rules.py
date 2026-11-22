import hashlib, json

def entry_key(row):
    # 幂等键只取业务内容：同一批退款重算、重试、切换来回都得到同一个键，
    # 会员中心按此键去重，重复下发不会重复加减。依据行号只用于复核定位，不进键。
    base={'refund':row['id'],'member':row['member'],'ref':row['ref'],'points':row['points'],
          'restored':row['restored'],'expired_refund':row['expired_refund']}
    return 'sync-'+hashlib.sha256(json.dumps(base,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]

def build_batch(state):
    entries=[]
    for r in state['refund_audit']:
        entries.append({'refund':r['id'],'member':r['member'],'ref':r['ref'],'points':r['points'],
                        'restored':r['restored'],'expired_refund':r['expired_refund'],
                        'line':r['line'],'key':entry_key(r)})
    restored=sum(e['restored'] for e in entries)
    expired=sum(e['expired_refund'] for e in entries)
    b=state['balance']
    # 发布门槛：恢复额+过期损失=退款合计，且与门店余额账守恒、无未决退款差异。
    consistent=(restored+expired==b['refunded'] and b['points_balanced']
                and b['row_balanced'] and not state['refund_issues'])
    seal=hashlib.sha256(json.dumps(entries,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'entries':entries,'count':len(entries),'restored':restored,'expired_refund':expired,
            'refunded':b['refunded'],'store_balance':b['remaining'],'consistent':consistent,'seal':seal}

def apply(state):
    batch=build_batch(state)
    state['sync_batch']=batch
    state['views']['sync']={'title':'退货同步','rows':batch['entries'],
        'metrics':{'同步条目':batch['count'],'恢复额':batch['restored'],'过期损失':batch['expired_refund']}}
