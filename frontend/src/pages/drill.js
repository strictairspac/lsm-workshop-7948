import{drill}from'../stores/drill.js';import{datasets}from'../stores/dataset.js';
import{listSessions,createSession,begin,readRange,writeKey,commitTxn,rollbackTxn,compact}from'../api/drill.js';
import{header}from'../components/common/header.js';import{table}from'../components/common/table.js';
import{esc,json}from'../utils/html.js';import{submit,notify}from'../hooks/feedback.js';
import{session}from'../stores/session.js';import{TXN_STATES}from'../constants/enums.js';

const stateBadge=t=>`<span class="badge ${esc(t.state)}">${esc(TXN_STATES[t.state]||t.state)}</span>`;

function keyspaceTable(view){
  const rows=Object.entries(view.live_keyspace);
  return `<h2>已提交键空间 · commit_seq=${view.commit_seq}</h2>`+
    table(['键','已提交值（逻辑版本见物理段）'],rows.map(([k,v])=>[esc(k),esc(v)]))+
    `<p class="helper">物理段 ${view.segments.length} 个；删除键保留墓碑，不显示在存活键空间。</p>`;
}

function resultBanner(t){
  const r=t.result;if(!r)return'';
  if(r.ok)return `<div class="drill-result ok"><strong>提交成功 · ${esc(t.id)}</strong><span>${esc(r.message)}</span>${r.applied_writes?`<span class="badge passed">写入 ${r.applied_writes} 键 · seq=${r.commit_seq}</span>`:''}</div>`;
  return `<div class="drill-result conflict"><strong>整笔退回 · ${esc(t.id)}</strong><span>${esc(r.message)}</span>`+
    (r.conflict_keys.length?`<div class="conflict-keys">${r.conflict_keys.map(k=>`<span class="badge failed">${esc(k)}</span>`).join('')}</div>`:'')+`</div>`;
}

function txnPanel(t,writable){
  const staged=Object.entries(t.staged);
  const snapshotRows=Object.entries(t.snapshot);
  const active=t.state==='active';
  return `<article class="txn-card ${esc(t.state)}" data-txn="${esc(t.id)}">
    <div class="txn-head"><h3>${esc(t.id)} · ${esc(t.label)}</h3>${stateBadge(t)}</div>
    <p class="helper">开始于 commit_seq=${t.started_seq}${t.ended_seq!=null?` · 结束于 ${t.ended_seq}`:''}</p>
    ${resultBanner(t)}
    <h4>读范围谓词（${t.read_ranges.length}）</h4>
    ${t.read_ranges.length?table(['下界','上界','谓词键数'],t.read_ranges.map(r=>[esc(r.lo),esc(r.hi),String(Object.keys(t.read_predicates).filter(k=>r.lo<=k&&k<=r.hi).length)])):'<p class="helper">尚未读取键范围。</p>'}
    <h4>暂存区（读己写，${staged.length} 键）</h4>
    ${staged.length?table(['键','意图','值 / 墓碑','基线版本'],staged.map(([k,intent])=>[esc(k),esc(intent.op),intent.op==='delete'?'<span class="badge failed">墓碑</span>':esc(intent.value),String(intent.base_version)])):'<p class="helper">暂存为空：读范围只会固定谓词，不会改键。</p>'}
    ${active&&writable?`
    <form class="drill-read" data-txn="${esc(t.id)}">
      <h4>按开始时状态读键范围（叠加本事务暂存）</h4>
      <div class="form-row"><input name="lo" required maxlength="64" placeholder="下界，如 sensor-01"><input name="hi" required maxlength="64" placeholder="上界，如 sensor-05"><button type="submit">读取范围</button></div>
      <pre class="drill-output" hidden></pre>
    </form>
    <form class="drill-write" data-txn="${esc(t.id)}">
      <h4>暂存覆盖或删键</h4>
      <div class="form-row"><select name="op"><option value="put">put 覆盖</option><option value="delete">delete 删键</option></select><input name="key" required maxlength="64" placeholder="键"><input name="value" maxlength="200" placeholder="put 的值（delete 忽略）"><button type="submit">暂存</button></div>
    </form>
    <div class="actions">
      <button class="primary drill-commit" data-txn="${esc(t.id)}">提交事务 →</button>
      <button class="drill-rollback" data-txn="${esc(t.id)}">人工回滚</button>
    </div>`:`<p class="helper">${active?'观察员只读：可查看快照与谓词，不能发起读写。':'事务已终态。'}${!active&&writable?'<button class="drill-replay" data-txn="'+esc(t.id)+'">幂等重放提交结果</button>':''}</p>`}
    <details><summary>事务开始时的快照（${snapshotRows.length} 键）</summary><pre class="json">${json(t.snapshot)}</pre></details>
  </article>`;
}

function segmentsPanel(view){
  return `<details class="segments-detail"><summary>物理段与逻辑版本（${view.segments.length} 段 · 点击展开）</summary>`+
    view.segments.map(seg=>`<h4>段 #${seg.id} · ${esc(seg.kind)} · ${Object.keys(seg.records).length} 记录</h4>`+
      table(['键','逻辑版本','墓碑','值'],Object.entries(seg.records).map(([k,r])=>[esc(k),String(r.version),r.deleted?'是':'否',r.deleted?'—':esc(r.value)]))).join('')+`</details>`;
}

function eventLog(view){
  const events=view.events.slice(-14).reverse();
  return `<h2>操作轨迹（最近14条）</h2>`+table(['seq','事件','说明'],events.map(e=>[String(e.seq),esc(e.event),esc(e.detail)]));
}

async function detailPage(el,id){
  const writable=session.get().role!=='viewer';
  const view=await drill.open(id);
  const txns=Object.values(view.transactions);
  const active=txns.filter(t=>t.state==='active');
  const finished=txns.filter(t=>t.state!=='active');
  el.innerHTML=header('05','两笔事务交错，按逻辑版本判定整笔成败','事务按开始时的快照读键范围，并能读到自己暂存的覆盖与删键；提交时重读谓词与待写键版本，任何增删改——哪怕值改回原样——都整笔退回。互不相交的事务可并发提交。')+
    `<div class="actions" style="margin-bottom:20px"><a href="#drill">← 返回演练场列表</a><button id="refresh-drill">刷新状态</button>${writable?'<button id="compact-drill">执行物理合并（搬动记录，不改逻辑版本）</button>':''}</div>
    <section class="panel"><div class="split"><div>${keyspaceTable(view)}</div><div>${segmentsPanel(view)}</div></div></section>
    <section class="panel"><h2>进行中的事务（${active.length}）</h2>
      ${active.length?`<div class="txn-grid">${active.map(t=>txnPanel(t,writable)).join('')}</div>`:'<div class="empty">没有进行中的事务。两班巡检可各开一笔，交错读取与暂存后再分别提交。</div>'}
      ${writable?`<form id="begin-form" class="begin-bar"><input name="label" maxlength="60" placeholder="事务标签，如 早班巡检-A"><button class="primary" type="submit">开始一笔新事务</button></form>`:''}
    </section>
    <section class="panel"><h2>已结束事务（${finished.length}）</h2>
      ${finished.length?`<div class="txn-grid finished">${finished.map(t=>txnPanel(t,writable)).join('')}</div>`:'<p class="helper">提交成功与整笔退回都会保留完整证据，包括冲突键。</p>'}
    </section>
    <section class="panel">${eventLog(view)}</section>`;
  el.querySelector('#refresh-drill').onclick=()=>detailPage(el,id);
  const compactBtn=el.querySelector('#compact-drill');
  if(compactBtn)compactBtn.onclick=()=>submit(compactBtn,async()=>{const r=await compact(id);notify(r.detail);await detailPage(el,id)});
  const beginForm=el.querySelector('#begin-form');
  if(beginForm)beginForm.onsubmit=e=>{e.preventDefault();submit(beginForm,async()=>{const label=Object.fromEntries(new FormData(beginForm)).label||'';await begin(id,label);notify('新事务已开始，快照固定在当前 commit_seq');await detailPage(el,id)})};
  el.querySelectorAll('form.drill-read').forEach(form=>form.onsubmit=e=>{e.preventDefault();submit(form,async()=>{
    const txnId=form.dataset.txn;const d=Object.fromEntries(new FormData(form));
    const r=await readRange(id,{transaction_id:txnId,lo:d.lo,hi:d.hi});
    const out=form.querySelector('.drill-output');out.hidden=false;out.textContent=JSON.stringify(r.rows,null,2);
    notify(`范围读取完成，谓词覆盖 ${r.predicate_keys.length} 个键`);await detailPageKeepOutput(el,id,txnId,d.lo,d.hi);
  })});
  el.querySelectorAll('form.drill-write').forEach(form=>form.onsubmit=e=>{e.preventDefault();submit(form,async()=>{
    const d=Object.fromEntries(new FormData(form));
    await writeKey(id,{transaction_id:form.dataset.txn,op:d.op,key:d.key,value:d.op==='delete'?null:d.value});
    notify(`已暂存 ${d.op} ${d.key}，仅本事务可见`);await detailPage(el,id);
  })});
  el.querySelectorAll('.drill-commit').forEach(btn=>btn.onclick=()=>submit(btn,async()=>{
    const r=await commitTxn(id,btn.dataset.txn);
    notify(r.ok?`${r.transaction_id} 提交成功`:`${r.transaction_id} 整笔退回，冲突键：${r.conflict_keys.join(', ')||'无'}`);
    await detailPage(el,id);
  }));
  el.querySelectorAll('.drill-rollback').forEach(btn=>btn.onclick=()=>submit(btn,async()=>{
    await rollbackTxn(id,btn.dataset.txn);notify('事务已人工回滚，暂存全部丢弃');await detailPage(el,id);
  }));
  // Finished txn: commit button is replaced by an idempotent replay link.
  el.querySelectorAll('.drill-replay').forEach(btn=>btn.onclick=()=>submit(btn,async()=>{
    const r=await commitTxn(id,btn.dataset.txn);notify('幂等重放：返回同一结果，未重复生效');window.console?.log(r);await detailPage(el,id);
  }));
}

async function detailPageKeepOutput(el,id,txnId,lo,hi){
  await detailPage(el,id);
  const form=el.querySelector(`form.drill-read[data-txn="${txnId}"]`);
  if(form){form.elements.lo.value=lo;form.elements.hi.value=hi;
    const out=form.querySelector('.drill-output');if(out){out.hidden=false;out.textContent='（范围已重新登记为谓词，展开快照可复核）'}}
}

async function listPage(el){
  const writable=session.get().role!=='viewer';
  const [rows,ds]=await Promise.all([drill.loadSessions(),datasets.load()]);
  el.innerHTML=header('05','选一个批次，做乐观事务演练','演练场从写入批次建立已提交键空间；每个逻辑键带单调版本号，事务在提交时校验读谓词与待写键版本，物理合并不改动版本。')+
    `<p class="modelnote">教学并发模型：单进程会话锁串行化状态迁移，整份状态 JSON 持久化到 MySQL，后端重启后在途事务及其谓词完整恢复。这不是 InnoDB 事务实现。</p>
    <div class="split"><section class="panel"><span class="material-tag">TXN SPECIMENS / ${rows.length}</span>`+
    (rows.length?rows.map(x=>`<article class="record"><h3>${esc(x.name)}</h3><p class="muted">${esc(x.description)||'无说明'}</p>
      <span class="badge">${x.transaction_count} 事务</span> <span class="badge">seq ${x.commit_seq}</span> <span class="badge">${x.segment_count} 物理段</span>
      <div class="actions"><a href="#drill/${x.id}">进入演练场 →</a></div></article>`).join(''):'<div class="empty">还没有演练场。右侧选择一个写入批次建立。</div>')+
    `</section><aside class="panel"><h2>${writable?'建立演练场':'输入契约'}</h2>${writable?`<form id="session-form">
      <label>演练场名称</label><input name="name" required maxlength="120" placeholder="drill-shift-20261118">
      <label>业务说明</label><input name="description" maxlength="2000" placeholder="两班巡检事务演练">
      <label>起始写入批次（可留空建立空键空间）</label><select name="dataset_id"><option value="">空键空间</option>${ds.map(x=>`<option value="${x.id}">${esc(x.name)} / ${x.operation_count}条 / ${x.distinct_keys}键</option>`).join('')}</select>
      <div class="actions"><button class="primary" type="submit">建立并进入 →</button></div></form>`:''}
      <p class="helper">建立时按批次操作顺序分配逻辑版本（delete 写墓碑）。演练场状态随每次操作落库；重名返回 409，引用不存在批次返回 404。</p></aside></div>`;
  if(writable)el.querySelector('#session-form').onsubmit=e=>{e.preventDefault();submit(e.target,async()=>{
    const d=Object.fromEntries(new FormData(e.target));d.dataset_id=d.dataset_id?Number(d.dataset_id):null;
    const created=await createSession(d);notify('演练场已建立');location.hash='drill/'+created.id;
  })};
}

export async function drillPage(el){
  const id=Number(location.hash.split('/')[1]);
  if(id)return detailPage(el,id);
  return listPage(el);
}
