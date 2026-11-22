const syncOps=document.querySelector('#sync-ops');
if(syncOps){
 const runId=document.body.dataset.runId;
 const out=document.querySelector('#result');
 const first=()=>syncOps.dataset.first?[syncOps.dataset.first]:[];
 const call=async(url,body)=>{
  out.textContent='提交中…';
  try{
   const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   const data=await response.json();
   if(!response.ok)throw Error(data.detail);
   location.reload();
  }catch(e){out.textContent=e.message;}
 };
 document.querySelector('#sync-publish').addEventListener('click',()=>call('/api/sync/'+runId+'/publish',{
  lose:document.querySelector('#sim-lose').checked?first():[],
  corrupt:document.querySelector('#sim-corrupt').checked?first():[],
  fail_switch:document.querySelector('#sim-fail').checked}));
 document.querySelector('#sync-retry').addEventListener('click',()=>call('/api/sync/'+runId+'/retry',{
  lose:document.querySelector('#sim-lose').checked?first():[]}));
}
