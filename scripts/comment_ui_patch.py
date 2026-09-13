from pathlib import Path

p = Path("app/main.py")
text = p.read_text(encoding="utf-8")
start = text.index("    comment_panel = r'''<style>")
end = text.index("\n    html=html.replace", start)
replacement = """    comment_panel = r'''<style>
.comment-actor-inline{grid-column:1/-1;margin-top:2px;padding:10px 12px;border:1px solid rgba(23,23,22,.08);border-radius:11px;background:#f8f7f4;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.comment-actor-inline .cai-main{min-width:220px;flex:1}.comment-actor-inline .cai-title{font-size:10px;font-weight:750}.comment-actor-inline .cai-meta{font-size:9px;color:#7b776f;margin-top:3px;overflow-wrap:anywhere}.comment-actor-inline .cai-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:9px}.comment-actor-inline input[type=number]{width:62px;padding:5px;border:1px solid rgba(23,23,22,.11);border-radius:8px;background:#fff}.comment-actor-inline .cai-state{font-size:9px;font-weight:750}.comment-actor-inline .cai-warn{color:#9a5c18}.comment-actor-inline input:disabled{opacity:.45}
</style><script>
(()=>{
const commentSources=new Set(['x','tiktok','instagram','facebook']);
const escComment=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
async function patchCommentSource(source,payload){const r=await fetch(`/api/sources/${source}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let data={};try{data=await r.json()}catch(_){ }if(!r.ok)throw new Error(data.detail||'Could not update comment Actor setting');return data}
async function decorateCommentActors(){
  const host=document.getElementById('view-sources');if(!host)return;
  let reg={};try{const r=await fetch('/api/sources');if(!r.ok)return;reg=await r.json()}catch(_){return}
  host.querySelectorAll('[data-edit-source]').forEach(btn=>{
    const source=btn.dataset.editSource;if(!commentSources.has(source))return;
    const row=btn.closest('.actor');if(!row)return;
    const c=reg[source]||{};const verified=String(c.comment_deepening_status||'unverified')==='verified';const enabled=!!c.comment_enabled;const max=Number(c.comment_max_per_parent||40);const actor=c.comment_actor_id||'No comment Actor configured';
    let box=row.querySelector('.comment-actor-inline');if(!box){box=document.createElement('div');box.className='comment-actor-inline';row.appendChild(box)}
    const sig=[actor,verified,enabled,max].join('|');if(box.dataset.sig===sig)return;box.dataset.sig=sig;
    box.innerHTML=`<div class="cai-main"><div class="cai-title">Comments / replies Actor</div><div class="cai-meta">${escComment(actor)}</div></div><div class="cai-controls"><span class="cai-state ${verified?'':'cai-warn'}">${verified?(enabled?'VERIFIED · ON':'VERIFIED · OFF'):'NEEDS LIVE VERIFICATION'}</span><label><input type="checkbox" data-comment-toggle="${source}" ${enabled?'checked':''} ${verified?'':'disabled'}> Include comments</label><label>max / parent <input type="number" min="1" max="1000" value="${max}" data-comment-max="${source}"></label></div>`;
    const toggle=box.querySelector('[data-comment-toggle]');if(toggle)toggle.onchange=async()=>{const wanted=toggle.checked;toggle.disabled=true;try{await patchCommentSource(source,{comment_enabled:wanted});box.dataset.sig='';await decorateCommentActors()}catch(e){toggle.checked=!wanted;alert(e.message)}finally{if(verified)toggle.disabled=false}};
    const maxInput=box.querySelector('[data-comment-max]');if(maxInput)maxInput.onchange=async()=>{const value=Math.max(1,Math.min(1000,Number(maxInput.value||40)));try{await patchCommentSource(source,{comment_max_per_parent:value});box.dataset.sig='';await decorateCommentActors()}catch(e){alert(e.message)}};
  });
}
let running=false;const schedule=()=>{if(running)return;running=true;setTimeout(()=>Promise.resolve(decorateCommentActors()).finally(()=>{running=false}),0)};
const sourceHost=document.getElementById('view-sources');if(sourceHost)new MutationObserver(schedule).observe(sourceHost,{childList:true,subtree:true});
setTimeout(decorateCommentActors,0);
})();
</script>'''"""
p.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

p = Path("app/registry.py")
text = p.read_text(encoding="utf-8")
needle = '        "comment_output_mapping": deepcopy(output_mapping or {}),\n        "comment_enabled": True,\n'
if needle not in text:
    raise SystemExit("Could not locate comment verification enable default")
text = text.replace(
    needle,
    '        "comment_output_mapping": deepcopy(output_mapping or {}),\n        "comment_enabled": False,\n',
    1,
)
p.write_text(text, encoding="utf-8")
print("Comment Actor controls moved into Sources & Actors and verification now stays OFF until explicitly enabled.")
