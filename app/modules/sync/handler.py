from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store, syncstore
router=APIRouter()

@router.get('/sync')
def page(request:Request):
    from app.main import DB_PATH
    def extra(state):
        publish=syncstore.view(DB_PATH,state['manifest']['run_id'])
        batch=state.get('sync_batch')
        return {'publish':publish,'batch':batch,
                'merged':syncstore.merge(batch['entries'],publish['entries']) if batch else []}
    return render(request,'sync',extra=extra)

@router.post('/api/sync')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=7)

@router.get('/api/sync/{run_id}')
def status(run_id:str):
    from app.main import DB_PATH
    store.get(DB_PATH,run_id)
    return syncstore.view(DB_PATH,run_id)

@router.post('/api/sync/{run_id}/publish')
def publish(run_id:str,payload:dict=Body(default={})):
    from app.main import DB_PATH
    run=store.get(DB_PATH,run_id)
    return syncstore.publish(DB_PATH,run,lose=payload.get('lose',[]),drop=payload.get('drop',[]),
                             corrupt=payload.get('corrupt',[]),fail_switch=bool(payload.get('fail_switch',False)))

@router.post('/api/sync/{run_id}/retry')
def retry(run_id:str,payload:dict=Body(default={})):
    from app.main import DB_PATH
    run=store.get(DB_PATH,run_id)
    return syncstore.retry(DB_PATH,run,lose=payload.get('lose',[]),drop=payload.get('drop',[]))
