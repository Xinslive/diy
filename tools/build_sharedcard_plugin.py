"""Bundle reviewed preview layout and live adapter into the self-contained plugin.

Never embeds snapshot/business data. Existing management and export APIs survive.
"""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/com.cwq.sharedcard"
MARKER = "\n# === Shared-card continuity bundle ===\n"
FRONT_MARKER = "\n// === Shared-card continuity bundle ===\n"


def build():
    main = PLUGIN / "backend/main.py"
    legacy = main.read_text().split(MARKER)[0]
    if '    register_workspace(registrar, ctx)' not in legacy:
        legacy += '\n    register_workspace(registrar, ctx)\n'
    # Protect source mutations while a group still occupies its bank evidence.
    for name in ('reparse_statement', 'delete_statement'):
        start = legacy.index('    def ' + name + '(')
        end = legacy.find('\n    def ', start + 1)
        if end < 0:
            end = len(legacy)
        block = legacy[start:end]
        needle = '        stmt = _stmt(ctx, stmt_id, books)\n'
        if '_protect_statement_matches(ctx, stmt, books)' not in block:
            block = block.replace(needle, needle + '        _protect_statement_matches(ctx, stmt, books)\n', 1)
        legacy = legacy[:start] + block + legacy[end:]
    for name in ('delete_card', 'delete_member', 'assign'):
        start = legacy.index('    def ' + name + '(')
        end = legacy.find('\n    def ', start + 1)
        if end < 0:
            end = len(legacy)
        block = legacy[start:end]
        if name == 'delete_card' and '_recon_rows(ctx, card_id, books)' not in block:
            block = block.replace('        books = _books(ctx)\n', '        books = _books(ctx)\n        if _recon_rows(ctx, card_id, books):\n            _fail("这张卡已有核对或归属历史，请保留卡片及原始证据。", 409)\n', 1)
        if name == 'delete_member' and '_recon_rows(ctx, card_id, books)' not in block:
            block = block.replace('                ctx.data.delete(MEMBER_TABLE,', '                if int(item["card_id"]) != int(card_id):\n                    _fail("这个成员不属于这张卡。", 404)\n                if any(r["kind"] == "GROUP" and r["book_id"] == book["id"] for r in _recon_rows(ctx, card_id, books)):\n                    _fail("这本账仍有有效核对组，请先核实并撤销关联。", 409)\n                ctx.data.delete(MEMBER_TABLE,', 1)
        if name == 'assign' and '所选交易已有核对关系' not in block:
            block = block.replace('        wanted = {int(item) for item in row_ids}\n', '        wanted = {int(item) for item in row_ids}\n        if any(r["kind"] == "CLAIM" and r.get("bank_row_id") in wanted for r in _recon_rows(ctx, stmt["card_id"], books)):\n            _fail("所选交易已有核对关系，请先在工作台撤销。", 409)\n', 1)
        legacy = legacy[:start] + block + legacy[end:]
    projection = (ROOT / 'tools/sharedcard_redesign.py').read_text()
    projection = projection[projection.index('def cents('):projection.index('\ndef write_private(')]
    adapter = (ROOT / 'tools/sharedcard_live.py').read_text()
    main.write_text(legacy + MARKER + 'from collections import defaultdict, Counter\nfrom datetime import time\nimport re\n' + projection + '\n' + adapter)

    template = (ROOT / 'tools/sharedcard-preview.html').read_text()
    html, rest = template.split('<style>', 1)
    css, rest = rest.split('</style>', 1)
    script = rest.split('<script>')[1].split('</script>')[0]
    combo = (ROOT / 'tools/sharedcard-period-combobox.js').read_text().replace('export function', 'function', 1)
    old_combo_start = script.index('/** Bounded, searchable controls.')
    old_combo_end = script.index('  function render() {', old_combo_start)
    script = script[:old_combo_start] + combo + '\n' + script[old_combo_end:]
    script = script.replace('(() => {', '', 1).rsplit('})();', 1)[0]
    script = script.replace("  const root = document.getElementById('shared-card-v2');\n  const D = JSON.parse(document.getElementById('shared-card-v2-data').textContent);", "  const root = container.querySelector('#shared-card-v2');")
    script = script.replace("const ledgerChoices = [...new Set([...months,...months.map(previousMonth)])].sort();", "const ledgerChoices = D.ledger.months.map(m=>m.month);")
    script = script.replace('const latestMonth =', 'let latestMonth =')
    script = script.replace("groups:[],owners:new Map()", "groups:D.groups||[],owners:new Map()")
    # Capture financial writes before the old demonstration event handler.
    script = script.replace("  root.addEventListener('click',event=>{", LIVE_EVENTS + "\n  root.addEventListener('click',event=>{", 1)
    start = script.index('  restore(window.openai?')
    script = script[:start] + '\n  render();\n'
    # Replace every demonstration write with the live capture handlers; guard
    # against accidental fallthrough if an unsupported action is introduced.
    script = re.sub(r"    else if\(action==='confirm'\).*?\n    else if\(action==='page'\)", "    else if(action==='confirm'||action==='revoke'||action==='resolve-owner')return;\n    else if(action==='page')", script, flags=re.S)
    script = re.sub(r"    if\(target.dataset.ownerTx\).*?\n    if\(target.dataset.bankPick\)", "    if(target.dataset.ownerTx)return;\n    if(target.dataset.bankPick)", script, flags=re.S)
    # No stale hardcoded production facts or preview-only copy in the live UI.
    script = re.sub(r'  function renderImport\(\).*?\n(?=/\*\* Bounded, searchable controls\.)', '', script, flags=re.S)
    script = script.replace('    renderImport();', '    root.querySelector(\'#scv-import\').replaceChildren();')
    script = script.replace("'请核实后选择':'归属待定'", "'请核实后选择':'归属待定'")
    script = script.replace('原归属证据保留；更改仅在本页演示', '确认后保存；原归属证据保留')
    script = script.replace('本页指定 / 更改归属', '指定 / 更改归属')
    script = script.replace('本页核对', '人工核对').replace('本页指定', '人工指定')
    script = script.replace('本页已核对', '已核对').replace('确认核对（本页）', '确认核对')
    script = script.replace('本页已关联核对组', '已关联核对组')
    script = script.replace("if(txGroup(tx.id))return", "if(txGroup(tx.id)?.stale)return '<span class=\"scv-tag scv-tag-red\">关联变更待复核</span>';if(txGroup(tx.id))return", 1)
    script = script.replace("    const targetBook=entries[0].book_id;", "    const targetBook=entries[0].book_id;\n    if(entries.some(e=>!e.reference_ok)||!D.can_write_books.includes(targetBook))return {...base,text:'所选分录缺少唯一标识或没有账套写权限，不能确认。'};")
    script = script.replace("${D.books.map(b=>`<option value=\"${b.id}\"${ownerOf(tx)===b.id?' selected':''}>", "${D.books.filter(b=>D.can_write_books.includes(b.id)).map(b=>`<option value=\"${b.id}\"${ownerOf(tx)===b.id?' selected':''}>")
    script = script.replace('当前期间没有可演示的月度收入分录', '当前期间没有可用的月度收入分录')
    script = script.replace('账面线目前汇总 8 个登记账套的 1001 科目', '账面线汇总当前成员账套的登记科目')
    script = script.replace('当前登记的是 1001 库存现金，可能包含现金等非本卡业务。“待删除”和“优境”均在范围内，不能把登记科目余额合计直接当成这张卡的应有余额。', '登记科目可能包含现金等非本卡业务；成员范围应逐一核实，登记科目合计不能直接当作本卡应有余额。')
    script = script.replace('查看7笔归属冲突', '查看归属冲突')
    script = script.replace('“待删除”仍登记在卡下，7 笔交易同时留有它与“优境”的归属。这里保留原记录，需核实范围及逐笔归属。', '历史归属证据完整保留；同一交易存在多本账认领时，先核实归属再确认核对。')
    script = script.replace('生产快照 ${D.captured_at.slice(0,10)} ${D.captured_at.slice(11,16)}（北京时间）', '实时读取 ${new Date(D.captured_at).toLocaleString(\'zh-CN\', {hour12:false})}')
    begin = script.index('  function groupBankDate(g)')
    end = script.index('\n  function workPane()')
    script = script[:begin] + RECORDS_PANE + script[end:]
    html = html.replace('<span class="scv-preview-tag">流程预览</span>', '<span class="scv-preview-tag">2.0 连续资金线</span>')
    html = html.replace('确认与撤销仅在本页演示', '核对关系持久保存 · 原件与凭证可追溯')
    css = css.replace('var(--scv-panel,#fff)', 'var(--scv-surface,#fff)').replace('var(--scv-border,#d8dde5)', 'var(--scv-line,#d8dde5)')
    css += '\n#shared-card-v2{overflow:visible}#shared-card-v2[aria-busy=true]{opacity:.7;pointer-events:none}#shared-card-v2 .scv-month{flex:0 0 auto;min-width:65px}#shared-card-v2 .scv-months{display:flex;flex-wrap:nowrap;overflow-x:auto}#shared-card-v2 .scv-appbar{display:none}\n'
    css += '#shared-card-v2 .scv-root-message{padding:24px}#shared-card-v2 .scv-period-search{width:150px;margin:8px 0}\n'
    front = PLUGIN / 'frontend/index.js'
    legacy_front = (PLUGIN / 'frontend/management.js').read_text()
    # **不再给外层加那条页签条**（2026-10-31）：详情页顶栏整条去掉了 ——
    # 卡名在工作区标题下面已经写着，页签在工作区自己的导航里也有一份，
    # 两排导航并排摆着只会让人分不清各自管什么。外层只保留 cardTab 作为
    # 「工作区 / 管理成员 / 管理原件」这三屏之间的内部开关。
    legacy_front = legacy_front.replace("const cardTab = host.ref('statements')", "const cardTab = host.ref('flow')")
    legacy_front = legacy_front.replace("const keepTab = same ? cardTab.value : 'statements'", "const keepTab = same ? cardTab.value : 'flow'")
    legacy_front = legacy_front.replace("  const h = host.h", "  const h = host.h\n  const Workspace = sharedCardWorkspace(host)", 1)
    for selector in ('scv-content',):
        needle = "root.querySelector('#" + selector + "').innerHTML="
        line_start = script.index(needle)
        line_end = script.index(';\n', line_start)
        expression = script[line_start + len(needle):line_end]
        script = script[:line_start] + "sharedCardSafeReplace(root.querySelector('#" + selector + "')," + expression + ')' + script[line_end:]
    safe_dom = (ROOT / 'tools/sharedcard_safe_dom.js').read_text()
    entry = legacy_front + FRONT_MARKER + safe_dom + '\n' + WORKSPACE_COMPONENT + '\nexport function mountSharedCardTimeline(container, D, host, manage) {\n  sharedCardSafeReplace(container, ' + json.dumps(html, ensure_ascii=False) + ');\n' + script + '\n}\n'
    front.write_text(entry)
    style = PLUGIN / 'frontend/style.css'
    old_style = style.read_text().split('/* === Shared-card continuity bundle === */')[0]
    style.write_text(old_style + '\n/* === Shared-card continuity bundle === */\n' + css)
    manifest_path = PLUGIN / 'plugin.json'
    manifest = json.loads(manifest_path.read_text())
    # 2.1.9：工作台按用户意见调整版面（去掉顶部摘要条与「原始资料」那一页、
    # 两条资金线与搜索栏互换位置、勾选不再置顶、工作台上的「查看归属冲突」
    # 跳转与「汇总收入核对」按钮、查找框与收付方向挪进「银行交易」栏头、
    # 左右两块面板对齐），「对账单明细」那一页整页去掉，
    # 「导出账面明细账」挪到「成员账套」那一页；
    # 栏头里的控件改为垂直居中，左右两栏表头上方那一行统一 45px 高，
    # 两边表头横向对齐。
    # **必须升版本** —— 宿主只接受「更高版本」的升级，同版本重装会被
    # DOWNGRADE_OR_REPLAY_REJECTED 挡掉。
    manifest['version'] = '2.1.9'
    # **2.1.0 起要用宿主 SDK 1.5.0**（跨账套一次读）。装到旧宿主上会被
    # 宿主在**安装时**按区间挡掉，并说清「请先更新宿主」—— 比装上了却
    # 一路报 TypeError 好。
    manifest['api_version'] = '>=1.5.0 <2.0.0'
    manifest['description'] = '以银行卡为主线接续原始对账单，保留交叠来源；账面按宿主账簿引擎连接跨期余额与逐笔凭证。支持跨月、多笔同向等额人工核对、原子占用与撤销、归属冲突复核、限高可搜索月份选择。核对记录单独成页；确认核对只算被选中那本账；跨账套一次读，读的次数不再随账套数涨。只对账，不制证。'
    if not any(t['name'] == 'plugin_275b391c351a_recon' for t in manifest['tables']):
        manifest['tables'].append({'name': 'plugin_275b391c351a_recon', 'schema': 'public', 'scope': 'book'})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


WORKSPACE_COMPONENT = r'''
function sharedCardWorkspace(host) {
  return { name:'SharedCardContinuity', props:['cardId','onManage'], setup(props) {
    const model=host.ref(null), error=host.ref('');let mounted=null, disposed=false;
    host.onMounted(async()=>{try{const result=await host.api.get(`/cards/${props.cardId}/workspace`);if(!disposed)model.value=result;}catch(e){if(!disposed)error.value=String(e?.message||e);}});
    host.onUnmounted(()=>{disposed=true;mounted=null;});
    return ()=> error.value?host.h('div',{role:'alert',class:'sc-notice sc-notice-danger'},[error.value,host.h('button',{class:'sc-btn',onClick:()=>props.onManage('files')},'管理原始资料')]):!model.value?host.h('p',{role:'status'},'正在连接银行与账面资金线…'):model.value.empty?host.h('div',{class:'sc-empty'},['还没有可连接的银行交易。',host.h('button',{class:'sc-btn',onClick:()=>props.onManage('files')},'导入原件 / 登记成员')]):host.h('div',{ref:node=>{if(node&&mounted!==node){mounted=node;mountSharedCardTimeline(node,model.value,host,(kind,id)=>props.onManage(kind,id));}}});
  }};
}
'''

RECORDS_PANE = r'''
  function groupBankDate(g){const snaps=(g.bank_snapshot||[]).map(t=>t.tx_date).filter(Boolean).sort();if(snaps.length)return snaps[0];return (g.bank||[]).map(id=>txMap.get(id)?.tx_date).filter(Boolean).sort()[0]||'';}
  function groupPeriods(g){const snaps=[...new Set((g.entry_snapshot||[]).map(e=>e.period).filter(Boolean))];if(snaps.length)return snaps.join('、');return [...new Set((g.entries||[]).map(id=>entryMap.get(id)?.period).filter(Boolean))].join('、');}
  function groupRow(g,canRevoke){const can=Array.isArray(D.can_write_books),locked=can&&!D.can_write_books.includes(g.book_id);return `<div class="scv-group"><span>${esc(bookMap.get(g.book_id)?.name||g.book_id)} · ${(g.bank||[]).length} 笔银行 ↔ ${(g.entries||[]).length} 笔账面 <span class="scv-tag ${g.stale?'scv-tag-red':'scv-tag-green'}">${g.stale?'来源已变化，待复核':'合计 '+cash(g.amount_cents)}</span><span class="scv-subline">${esc((g.created_at||'').slice(0,10))} · 银行 ${esc(groupBankDate(g))} · 账面 ${esc(groupPeriods(g))}</span></span>${canRevoke?button('撤销本组','revoke',`data-group="${g.id}"`,locked):''}</div>`;}
  function recordsPane() {const active=state.groups||[],revoked=(D.history||[]).filter(g=>!g.active);
    const bank=active.reduce((n,g)=>n+(g.bank||[]).length,0),entries=active.reduce((n,g)=>n+(g.entries||[]).length,0);
    return `<div class="scv-toolbar"><div><h2>已保存的核对记录</h2><p class="scv-subline">银行交易与账面分录的连接关系 · 跨月关联 · 刷新后保留</p></div><span class="scv-small scv-muted">${active.length} 组生效 · 覆盖 ${bank} 笔银行 / ${entries} 笔账面</span></div><section class="scv-panel" aria-label="生效中的核对组"><div class="scv-panel-head"><h2>生效中</h2><span class="scv-small scv-muted">撤销后两侧恢复待核对，原始资料与凭证保留</span></div>${active.length?active.map(g=>groupRow(g,true)).join(''):'<div class="scv-panel-body"><div class="scv-empty">还没有保存过核对记录。在「对账工作台」核实两侧对应关系后确认，这里会留下可追溯的记录。</div></div>'}</section>${revoked.length?`<section class="scv-panel" style="margin-top:12px" aria-label="已撤销的核对组"><div class="scv-panel-head"><h2>已撤销</h2><span class="scv-small scv-muted">原始资料与凭证保留</span></div>${revoked.map(g=>groupRow(g,false)).join('')}</section>`:''}`;}
'''

LIVE_EVENTS = r'''
  let writing=false;
  const bankSnapshot=t=>Object.fromEntries(['id','card_id','tx_date','tx_time','direction','amount_cents','balance_cents'].map(k=>[k,t[k]??null]));
  const entrySnapshot=e=>Object.fromEntries(['id','book_id','voucher_id','voucher_no','voucher_date','period','subject_code','amount_cents','direction','summary','voucher_status'].map(k=>[k,e[k]??null]));
  async function refreshLive() {
    const fresh=await host.api.get(`/cards/${D.card.id}/workspace`);
    if(fresh.empty){manage('files');return;}
    Object.keys(D).forEach(k=>delete D[k]);Object.assign(D,fresh);
    txMap.clear();D.transactions.forEach(t=>txMap.set(t.id,t));entryMap.clear();D.entries.forEach(e=>entryMap.set(e.id,e));
    bookMap.clear();D.books.forEach(b=>bookMap.set(b.id,b));importMap.clear();D.imports.forEach(f=>importMap.set(f.id,f));
    months.splice(0,months.length,...D.months.map(m=>m.month));ledgerChoices.splice(0,ledgerChoices.length,...D.ledger.months.map(m=>m.month));latestMonth=months.at(-1);
    state.groups=D.groups||[];state.owners.clear();
    for(const id of state.selectedBank)if(!txMap.has(id)||txGroup(id))state.selectedBank.delete(id);
    for(const id of state.selectedEntries)if(!entryMap.has(id)||entryGroup(id))state.selectedEntries.delete(id);
  }
  // 写完之后**不再重拉整个工作台** —— 那份要 0.9 MB、一次往返一秒多，
  // 而「确认核对」本来就已经在服务器上算过一次工作台了。服务器把这一组
  // 原样交回来，页面直接打补丁，省掉整整一次往返。
  function patchGroups(groups){D.groups=groups||[];state.groups=D.groups;}
  function patchHistory(group){if(!group)return;D.history=[...(D.history||[]).filter(g=>Number(g.id)!==Number(group.id)),group];}
  async function liveWrite(operation, message, patch) {
    if(writing)return;writing=true;root.setAttribute('aria-busy','true');
    try{const result=await operation();if(patch){patch(result);}else{await refreshLive();}setMessage(message);}
    catch(error){try{await refreshLive();}catch{}setMessage(String(error?.message||error),true);}
    finally{writing=false;root.removeAttribute('aria-busy');render();}
  }
  function saveOwner(id, book) {if(book)return liveWrite(()=>host.api.post(`/cards/${D.card.id}/ownership`,{tx_id:id,book_id:Number(book)}),'归属已保存；原有归属证据完整保留。');}
  // **导出账面明细账**：对账单明细那一页删了，但这个导出是**卡级**的 ——
  // 导的是这张卡登记在各成员账套上的现金科目明细账，功能要留下。
  // 文件是宿主生成的真 xlsx（插件沙箱只能读、不能造 xlsx），这里拿到的是
  // base64，转成 Blob 交给浏览器下载。
  async function exportLedger(){
    try{
      const body=await host.api.get(`/cards/${D.card.id}/ledger-export`);
      const binary=atob(body.content_base64||'');
      const bytes=new Uint8Array(binary.length);
      for(let index=0;index<binary.length;index++)bytes[index]=binary.charCodeAt(index);
      const blob=new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
      const url=URL.createObjectURL(blob);
      const link=document.createElement('a');link.href=url;link.download=body.filename||'共享卡账面明细账.xlsx';
      document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);
      // **有话要说就说在下载之后**：某本账缺期间、某个科目不存在 ——
      // 这些话不能只藏在文件里。
      const notes=body.notes||[];
      setMessage(notes.length?notes.join('\n'):'已导出账面明细账。',notes.length>0);
      render();
    }catch(error){setMessage(String(error?.message||error),true);render();}
  }
  root.addEventListener('change',event=>{if(event.target.dataset.ownerTx){event.stopImmediatePropagation();saveOwner(event.target.dataset.ownerTx,event.target.value);}},true);
  root.addEventListener('click',event=>{
    const control=event.target.closest('[data-action]');if(!control||control.disabled)return;
    const action=control.dataset.action;
    if(!['confirm','revoke','resolve-owner','import','ledger-export','back','manage-files','manage-members'].includes(action))return;
    event.stopImmediatePropagation();event.preventDefault();
    if(action==='back'){manage('back');return;}
    if(action==='manage-files'){manage('files');return;}
    if(action==='manage-members'){manage('members');return;}
    if(action==='resolve-owner'){saveOwner(control.dataset.id,control.dataset.book);return;}
    if(action==='confirm'){
      const v=verdict();if(!v.ok){setMessage(v.text,true);render();return;}
      liveWrite(()=>host.api.post(`/cards/${D.card.id}/reconciliations`,{bank:v.bank.map(t=>t.id),entries:v.entries.map(e=>e.id),bank_snapshot:v.bank.map(bankSnapshot),entry_snapshot:v.entries.map(entrySnapshot)}),'核对组已保存；银行交易与账面分录已连接。',
        result=>{const group=result&&result.group;if(!group)return;patchGroups([...(D.groups||[]),group]);state.selectedBank.clear();state.selectedEntries.clear();});
    }else if(action==='revoke')liveWrite(()=>host.api.delete(`/cards/${D.card.id}/reconciliations/${control.dataset.group}`),'关联已撤销；两侧恢复待核对，原始资料和确认历史保留。',
        result=>{const id=Number(control.dataset.group);patchGroups((D.groups||[]).filter(g=>Number(g.id)!==id));patchHistory(result&&result.group);});
    else if(action==='ledger-export'){exportLedger();}
    else if(action==='import'){
      const input=document.createElement('input');input.type='file';input.accept='.xlsx,.xls,.csv,.pdf';
      input.addEventListener('change',()=>{const file=input.files?.[0];if(file)liveWrite(async()=>{const result=await host.api.upload(`/cards/${D.card.id}/statements`,file);if((result.diagnosis||[]).length)throw new Error('原件已接收，请在原始资料核实解析诊断：'+result.diagnosis.join('；'));},'对账单已导入，资金线和原件来源已更新。');});input.click();
    }
  },true);
'''


if __name__ == '__main__':
    build()
